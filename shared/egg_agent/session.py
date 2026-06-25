"""Session-state round-trip for the BRC event-pump warm-resume substrate (#3200, slice-6).

The BRC event-pump invokes a one-shot ``python3 -m egg_agent`` process per
event (``orchestrator/consensus_wrapper.py``). Each invocation is a fresh
process, so to RE-ENTER the prior Claude Code session on the next event the
``session_id`` (already surfaced on :class:`~egg_agent.result.AgentResult`)
must survive *between* processes. This module persists the small session-state
record — ``session_id`` plus the ``window_occupancy`` the slice-8 gate reads —
to a JSON file and reads it back, and exposes the opt-in enable flag.

**Substrate only — no decision here.** This module makes a warm resume
*possible* (write the id out, read it back, gate it behind an enable flag) but
takes NO resume-vs-reseed decision. Reading a record here never implies
"resume": the occupancy-vs-threshold gate that decides whether to actually
resume is slice-8 (``task-8-1``). Keeping the decision out of the substrate is
deliberate so the substrate can ship dark.

**Opt-in, default OFF (staged rollout).** ``session_resume_enabled()`` gates
the whole behaviour on ``EGG_SESSION_RESUME``; until an operator flips it on,
the substrate is inert and the agent path is byte-for-byte the legacy
cold-start. (Slice-9's master context-discipline flag may later subsume this
narrower switch; for now it is the single, documented staging knob.)

**Cold-start fallback — never a hard failure.** A missing/empty/corrupt state
file, an unset path, or a record without a usable ``session_id`` all resolve to
``None`` rather than raising. The first invocation, an expired session, a
consensus reset and a pod death therefore land on the same safe path: no
record -> a fresh session seeded from the protected root. Writes are equally
defensive — a persistence failure returns ``False`` and never crashes the agent
run it is bookkeeping for.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from egg_logging import get_logger

    logger: Any = get_logger("egg-agent")
except ImportError:  # pragma: no cover - stdlib fallback outside the sandbox
    import logging

    logger = logging.getLogger(__name__)

__all__ = [
    "SESSION_RESUME_ENV",
    "SESSION_STATE_FILE_ENV",
    "SessionState",
    "read_session_state",
    "resolve_session_state_path",
    "session_resume_enabled",
    "write_session_state",
]

# Opt-in enable flag for the warm-resume substrate (default OFF). Read in one
# place so the staged-rollout switch — and the eventual slice-9 fold-in — has a
# single authoritative home.
SESSION_RESUME_ENV = "EGG_SESSION_RESUME"

# Location of the cross-invocation session-state file. The event-pump wrapper
# (or the slice-8 gate) sets this per pipeline/role/slice; when unset the
# round-trip is a no-op (the substrate stays inert), which is exactly the
# default-OFF behaviour.
SESSION_STATE_FILE_ENV = "EGG_SESSION_STATE_FILE"

_TRUTHY = {"1", "true", "yes", "on"}


def session_resume_enabled() -> bool:
    """Return whether warm-session resume is enabled (opt-in, default OFF).

    Resume only ever happens when this returns ``True``. With the flag unset —
    the rollout default — a passed-in ``session_id`` is ignored and the agent
    cold-starts, so the substrate can ship before the slice-8 gate that drives
    it. Accepts the usual truthy spellings (``1/true/yes/on``, case-insensitive).
    """
    return os.environ.get(SESSION_RESUME_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class SessionState:
    """A persisted warm-resume record.

    ``session_id`` is the Claude session to re-enter. ``window_occupancy`` is
    the prior turn's cumulative occupancy (``cache_read + cache_creation +
    input``, #3200 slice-1), carried so the slice-8 resume-vs-reseed gate can
    read it without re-running the agent; ``None`` when the SDK reported no
    usage (bias the gate to a safe reseed).
    """

    session_id: str
    window_occupancy: int | None = None


def _coerce_occupancy(value: Any) -> int | None:
    """Return ``value`` as an occupancy int, or ``None`` (bools are not ints here)."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def resolve_session_state_path(
    explicit: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Resolve the session-state file path: explicit arg, else ``$EGG_SESSION_STATE_FILE``.

    Returns ``None`` when neither is set (round-trip disabled / no-op). A blank
    or whitespace-only value is treated as unset so an empty env var cannot
    point the round-trip at the current directory.
    """
    if explicit is not None:
        raw = str(explicit).strip()
    else:
        raw = os.environ.get(SESSION_STATE_FILE_ENV, "").strip()
    if not raw:
        return None
    return Path(raw)


def read_session_state(
    path: str | os.PathLike[str] | None = None,
) -> SessionState | None:
    """Read the persisted session state, or ``None`` — never raising.

    Every failure mode collapses to ``None`` (the cold-start signal): no path
    configured, missing file, empty file, malformed JSON, non-object payload,
    or a record without a usable ``session_id``. Reading a record NEVER implies
    a resume decision — the slice-8 gate owns that.
    """
    resolved = resolve_session_state_path(path)
    if resolved is None:
        return None
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return SessionState(
        session_id=session_id.strip(),
        window_occupancy=_coerce_occupancy(data.get("window_occupancy")),
    )


def write_session_state(
    session_id: str | None,
    window_occupancy: int | None = None,
    *,
    path: str | os.PathLike[str] | None = None,
) -> bool:
    """Persist the warm-resume record atomically; return whether it was written.

    Returns ``False`` (and writes nothing) when no path is configured or when
    ``session_id`` is empty — both are normal, not errors. The write is atomic
    (temp file + ``os.replace``) so a concurrent reader never sees a partial
    record, and any OS-level failure is swallowed (``False``) rather than
    crashing the agent run this is merely bookkeeping for.
    """
    resolved = resolve_session_state_path(path)
    if resolved is None:
        return False
    sid = (session_id or "").strip()
    if not sid:
        return False
    payload = {
        "session_id": sid,
        "window_occupancy": _coerce_occupancy(window_occupancy),
    }
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(resolved.parent),
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle)
            tmp_path = Path(handle.name)
        tmp_path.replace(resolved)
    except OSError as exc:
        logger.warning(
            "Failed to persist session state; next invocation cold-starts",
            event_type="system",
            event_subtype="session_state_write_failed",
            error=str(exc),
        )
        return False
    return True
