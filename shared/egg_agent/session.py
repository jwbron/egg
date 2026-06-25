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
the whole behaviour on ``EGG_SESSION_RESUME`` *or* the slice-9 master
context-discipline flag (``egg_agent.context_discipline``); until one of them is
flipped on, the substrate is inert and the agent path is byte-for-byte the
legacy cold-start. The narrower ``EGG_SESSION_RESUME`` knob remains for
staged rollout, but the master flag subsumes it (#3200 slice-9, task-9-1): a
single ``EGG_CONTEXT_DISCIPLINE`` enables the whole discipline — the
queryable-environment split and this warm-resume substrate together — so no
component is enabled in isolation by the master switch.

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

from egg_agent._logging import resolve_logger
from egg_agent.context_discipline import context_discipline_enabled

# Structured logger, with a kwarg-dropping stdlib fallback outside the sandbox.
# The bare ``logging.getLogger`` fallback this replaced raised ``TypeError`` on
# the ``event_type=``/``error=`` kwargs below, which would have defeated the
# module's "never raise; every failure cold-starts" contract (#3200 review).
logger: Any = resolve_logger("egg-agent", __name__)

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

    Resume only ever happens when this returns ``True``. It returns ``True``
    when EITHER the narrower ``EGG_SESSION_RESUME`` staging knob is set OR the
    slice-9 master context-discipline flag
    (:func:`egg_agent.context_discipline.context_discipline_enabled`) is on — the
    master flag subsumes this narrower switch so a single ``EGG_CONTEXT_DISCIPLINE``
    enables the whole discipline (queryable-env split + this warm-resume
    substrate) together (#3200 slice-9, task-9-1). With both unset — the rollout
    default — a passed-in ``session_id`` is ignored and the agent cold-starts, so
    the substrate can ship before the slice-8 gate that drives it. Accepts the
    usual truthy spellings (``1/true/yes/on``, case-insensitive).
    """
    if os.environ.get(SESSION_RESUME_ENV, "").strip().lower() in _TRUTHY:
        return True
    return context_discipline_enabled()


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

    The benign "nothing to resume yet" cases — no path configured and the file
    not existing — stay quiet. The *anomalous* cases (an unreadable file or a
    record that exists but is corrupt) emit a ``logger.debug`` so an operator
    who turned ``EGG_SESSION_RESUME`` on but is silently cold-starting every
    event has a diagnostic trail. This mirrors the write side's warning.
    """
    resolved = resolve_session_state_path(path)
    if resolved is None:
        return None
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        # No file yet — the normal first-invocation / pre-write path. Stay quiet.
        return None
    except OSError as exc:
        logger.debug(
            "Session-state file is unreadable; cold-starting",
            event_type="system",
            event_subtype="session_state_unreadable",
            error=str(exc),
        )
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError, TypeError:
        logger.debug(
            "Session-state file holds malformed JSON; cold-starting",
            event_type="system",
            event_subtype="session_state_corrupt",
        )
        return None
    if not isinstance(data, dict):
        logger.debug(
            "Session-state payload is not a JSON object; cold-starting",
            event_type="system",
            event_subtype="session_state_corrupt",
        )
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        logger.debug(
            "Session-state record has no usable session_id; cold-starting",
            event_type="system",
            event_subtype="session_state_corrupt",
        )
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
