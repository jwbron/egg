"""Cross-pod warm-resume session sync for the BRC event pump (#3278).

Each BRC event is a one-shot pod whose Claude Code session store
(``$CLAUDE_CONFIG_DIR/projects/<cwd-slug>/<session_id>.jsonl`` — the transcript
``claude --resume`` reads) lives on the pod's ephemeral filesystem and dies with
the pod. The durable copy is orchestrator-owned (Redis, keyed
``(pipeline, slice, role)``); the sandbox reaches it only through the orchestrator
route — it never writes host state. This module is the sandbox side of that
round-trip, invoked by ``egg-orch session-state pull|push`` around the agent:

- **pull** (before the agent runs): fetch the prior session and re-materialise the
  transcript into this pod's ephemeral store + write the pointer file the slice-8
  gate reads, so ``--resume`` finds a real transcript.
- **push** (after the agent runs): ship the updated transcript + pointer back.

The network calls live in ``orch_cli``; the slug math + file I/O here are pure and
unit-tested. Everything is best-effort: a miss or failure degrades to a cold
reseed (the ``egg_agent.session`` substrate's existing contract).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

__all__ = [
    "claude_project_slug",
    "is_safe_session_id",
    "read_state_for_push",
    "resolve_config_dir",
    "resolve_repo_path",
    "transcript_path",
    "write_pulled_state",
]

# Claude Code derives a session's project directory from the absolute cwd by
# replacing every non-alphanumeric character with ``-`` (so ``/``, ``.`` and
# ``_`` all map to ``-``; letters/digits/existing ``-`` are preserved). Verified
# empirically against the installed build, e.g.
# ``/home/egg/repos/My_Repo.v2`` -> ``-home-egg-repos-My-Repo-v2``.
_SLUG_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")

# A Claude session id is a UUID (the SDK mints it), so it can only contain
# hex/dash. We never interpolate anything outside this class into a filesystem
# path: a ``../``-bearing, slash-bearing, or empty-after-strip value would
# escape ``…/projects/<slug>/`` when joined. Guarding the path build is cheap
# defense-in-depth and hardens the Redis→pull direction (where the value
# originates off-pod) without coupling to the exact UUID layout.
_SAFE_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def is_safe_session_id(session_id: str) -> bool:
    """Return whether *session_id* is safe to interpolate into a filesystem path.

    Rejects empty / path-traversal (``..``) / separator-bearing values; accepts
    the UUID/token shape the Claude SDK actually produces.
    """
    return bool(_SAFE_SESSION_ID.fullmatch(session_id))


def claude_project_slug(repo_path: str | os.PathLike[str]) -> str:
    """Return the Claude Code projects-dir slug for *repo_path* (the agent's cwd)."""
    return _SLUG_NON_ALNUM.sub("-", os.path.abspath(str(repo_path)))


def resolve_config_dir(explicit: str | None = None) -> Path:
    """Resolve the Claude config dir: explicit arg, else ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``."""
    raw = (explicit or os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".claude"


def resolve_repo_path(explicit: str | None = None) -> str:
    """Resolve the agent cwd: explicit arg, else ``$EGG_REPO_PATH``, else the process cwd."""
    raw = (explicit or os.environ.get("EGG_REPO_PATH") or "").strip()
    return raw or os.getcwd()


def transcript_path(
    config_dir: str | os.PathLike[str],
    repo_path: str | os.PathLike[str],
    session_id: str,
) -> Path:
    """Return ``<config_dir>/projects/<cwd-slug>/<session_id>.jsonl``.

    Raises ``ValueError`` if *session_id* is not path-safe (see
    :func:`is_safe_session_id`) so a malformed value can never escape the
    project dir; callers treat the raise as "nothing to resume" and reseed.
    """
    if not is_safe_session_id(session_id):
        raise ValueError(f"unsafe session_id for transcript path: {session_id!r}")
    slug = claude_project_slug(repo_path)
    return Path(config_dir) / "projects" / slug / f"{session_id}.jsonl"


def write_pulled_state(
    record: dict[str, Any],
    *,
    repo_path: str,
    config_dir: str | os.PathLike[str],
    session_state_file: str,
) -> bool:
    """Materialise a pulled record into this pod; return whether resume is set up.

    Writes the pointer file (``session_id`` + ``window_occupancy``, the format
    ``egg_agent.session.read_session_state`` expects) and, when present, the
    transcript JSONL at the path ``--resume`` will look for. Returns ``True`` only
    when both the pointer and a transcript landed (a warm resume is actually
    possible); a pointer-only record returns ``False`` (the gate can still read
    occupancy, but there is nothing to resume — the agent cold-starts).

    Best-effort: any I/O failure returns ``False`` rather than raising.
    """
    session_id = str(record.get("session_id") or "").strip()
    if not session_id or not is_safe_session_id(session_id):
        return False
    occupancy = record.get("window_occupancy")
    if isinstance(occupancy, bool) or not isinstance(occupancy, int):
        occupancy = None
    transcript = record.get("transcript")
    if not isinstance(transcript, str):
        transcript = None

    try:
        # Pointer file (consumed by decide_resume_session in the agent).
        pointer = Path(session_state_file)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps({"session_id": session_id, "window_occupancy": occupancy}),
            encoding="utf-8",
        )
        if transcript is None:
            return False
        # Transcript file at the exact path --resume reads.
        tpath = transcript_path(config_dir, repo_path, session_id)
        tpath.parent.mkdir(parents=True, exist_ok=True)
        tpath.write_text(transcript, encoding="utf-8")
        return True
    except OSError:
        return False


def read_state_for_push(
    *,
    repo_path: str,
    config_dir: str | os.PathLike[str],
    session_state_file: str,
) -> dict[str, Any] | None:
    """Build the push body from this pod's post-run state, or ``None`` if nothing to push.

    Reads the pointer file the agent wrote (``write_session_state``) to learn the
    current ``session_id`` + ``window_occupancy``, then reads the matching
    transcript JSONL. Returns ``None`` when there is no usable session_id (the
    agent produced no session — nothing to persist). A missing transcript still
    yields a pointer-only body (occupancy survives for the next gate even if the
    transcript could not be read).

    Best-effort: any read failure collapses to ``None`` / a pointer-only body.
    """
    try:
        raw = Path(session_state_file).read_text(encoding="utf-8")
        pointer = json.loads(raw)
    except OSError, ValueError, TypeError:
        return None
    if not isinstance(pointer, dict):
        return None
    session_id = str(pointer.get("session_id") or "").strip()
    if not session_id or not is_safe_session_id(session_id):
        return None
    occupancy = pointer.get("window_occupancy")
    if isinstance(occupancy, bool) or not isinstance(occupancy, int):
        occupancy = None

    body: dict[str, Any] = {"session_id": session_id, "window_occupancy": occupancy}
    try:
        body["transcript"] = transcript_path(config_dir, repo_path, session_id).read_text(
            encoding="utf-8"
        )
    except OSError:
        body["transcript"] = None
    return body
