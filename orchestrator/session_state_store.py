"""Redis-backed cross-pod session-state store for the BRC warm-resume substrate (#3278).

Under the orchestrator-owned event loop (#3164) each BRC event is a fresh one-shot
pod, so the Claude Code session a role accumulates — its ``session_id`` + cumulative
``window_occupancy`` (the slice-8 gate input) AND the **session transcript** that
``claude --resume`` reads back — must survive pod death and be readable by the next
event pod for the same ``(pipeline, slice, role)``. ``shared/egg_agent/session.py``
already persists the small pointer to a *local* file; that file dies with the pod.
This module is the durable, off-pod home keyed ``(pipeline, slice, role)``.

**Why Redis, and why the orchestrator owns it.** The durable copy must be written
by the orchestrator/gateway, never the sandbox (the sandbox has no direct write
access to host state — it reaches this store only through the orchestrator route
that wraps this module). Session state is inherently transient and TTL-shaped, so
Redis — the orchestrator's existing ephemeral store — fits: a single key holds the
pointer **and** the transcript together (no split-brain between a pointer and a
separately-stored transcript), and the TTL reaps abandoned state automatically
rather than leaking disk per ``(pipeline, slice, role)``.

**Bias to reseed on any failure.** Every read/write failure (Redis down, oversized
transcript, malformed payload) collapses to ``None`` / ``False`` rather than
raising, so a lost record degrades to a safe cold reseed — exactly the
``egg_agent.session`` substrate's existing contract. A transcript larger than
:data:`MAX_TRANSCRIPT_BYTES` is dropped (not stored), so a pathological session can
never wedge Redis; the next event simply reseeds.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("orchestrator.session_state_store")

__all__ = [
    "MAX_TRANSCRIPT_BYTES",
    "SESSION_STATE_TTL_SECONDS",
    "SessionStateRecord",
    "SessionStateStore",
    "get_session_state_store",
    "reset_session_state_store",
    "set_session_state_store",
]

# Key prefix for the per-(pipeline, slice, role) record. The slice segment is
# ``none`` for pipeline-level (non-sliced) spawns so the key shape is uniform.
_KEY_PREFIX = "session-state"

# TTL on each record. Session state is live only for the duration of a slice's
# BRC cycle; a generous ceiling reaps abandoned state (cancelled pipeline, crashed
# slice) without a separate sweeper, while comfortably outlasting any real cycle.
SESSION_STATE_TTL_SECONDS = 6 * 60 * 60  # 6 hours

# Hard cap on a stored transcript. A reseed bounds occupancy below ~400K tokens
# (a few MB of JSONL), so a record far above that is anomalous; rather than push
# an unbounded blob into Redis we drop it and let the next event reseed. Chosen
# well above the expected ceiling so it only ever trips on pathological input.
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024  # 32 MiB


class SessionStateRecord:
    """A durable warm-resume record: pointer (``session_id`` + ``occupancy``)
    plus the Claude Code session ``transcript`` (the JSONL ``--resume`` reads).

    ``transcript`` may be ``None`` — a pointer-only record still lets the slice-8
    gate read occupancy, but resume only actually re-enters the session when the
    transcript is present to be re-materialised into the next pod.
    """

    __slots__ = ("session_id", "window_occupancy", "transcript")

    def __init__(
        self,
        session_id: str,
        window_occupancy: int | None = None,
        transcript: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.window_occupancy = window_occupancy
        self.transcript = transcript

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "window_occupancy": self.window_occupancy,
            "transcript": self.transcript,
        }


def _coerce_occupancy(value: Any) -> int | None:
    """Return ``value`` as an occupancy int, or ``None`` (bools are not ints here)."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


class SessionStateStore:
    """Redis-backed CRUD over per-(pipeline, slice, role) warm-resume records.

    Constructed with an injected redis client (real in production via
    :func:`get_session_state_store`, ``fakeredis.FakeRedis()`` in tests). Stores
    bytes (``decode_responses=False``) and owns its own JSON (de)serialisation.
    Every method is best-effort: a Redis/serialisation failure logs and returns
    the cold-start sentinel (``None`` / ``False``) — it never raises into the
    caller's request path.
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    @staticmethod
    def _key(pipeline_id: str, slice_id: str | None, role: str) -> str:
        return f"{_KEY_PREFIX}:{pipeline_id}:{slice_id or 'none'}:{role}"

    def put(
        self,
        pipeline_id: str,
        slice_id: str | None,
        role: str,
        *,
        session_id: str | None,
        window_occupancy: int | None = None,
        transcript: str | None = None,
    ) -> bool:
        """Persist (overwrite) the record under a fresh TTL; return whether it stored.

        Returns ``False`` (storing nothing) when ``session_id`` is empty or the
        transcript exceeds :data:`MAX_TRANSCRIPT_BYTES` — both degrade safely to a
        cold reseed on the next event.
        """
        sid = (session_id or "").strip()
        if not sid:
            return False
        if transcript is not None and len(transcript.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
            logger.warning(
                "Session-state transcript exceeds %d bytes; not persisting "
                "(pipeline=%s slice=%s role=%s) — next event reseeds",
                MAX_TRANSCRIPT_BYTES,
                pipeline_id,
                slice_id,
                role,
            )
            transcript = None
            # A pointer-only record is still useful (occupancy for the gate), so
            # fall through and store it without the oversized transcript.
        record = SessionStateRecord(sid, _coerce_occupancy(window_occupancy), transcript)
        try:
            payload = json.dumps(record.to_dict()).encode("utf-8")
            self._redis.setex(
                self._key(pipeline_id, slice_id, role),
                SESSION_STATE_TTL_SECONDS,
                payload,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort; never raise into the route
            logger.warning(
                "Failed to persist session state (pipeline=%s slice=%s role=%s): %s",
                pipeline_id,
                slice_id,
                role,
                exc,
            )
            return False

    def get(self, pipeline_id: str, slice_id: str | None, role: str) -> SessionStateRecord | None:
        """Read the record, or ``None`` — never raising.

        ``None`` covers every benign and anomalous miss alike (no record, Redis
        unreachable, malformed payload, no usable ``session_id``), so the caller's
        decision collapses to a safe cold reseed.
        """
        try:
            raw = self._redis.get(self._key(pipeline_id, slice_id, role))
        except Exception as exc:  # noqa: BLE001 — best-effort read
            logger.warning(
                "Failed to read session state (pipeline=%s slice=%s role=%s): %s",
                pipeline_id,
                slice_id,
                role,
                exc,
            )
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError, TypeError:
            logger.warning(
                "Malformed session-state payload (pipeline=%s slice=%s role=%s); ignoring",
                pipeline_id,
                slice_id,
                role,
            )
            return None
        if not isinstance(data, dict):
            return None
        sid = data.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            return None
        transcript = data.get("transcript")
        if transcript is not None and not isinstance(transcript, str):
            transcript = None
        return SessionStateRecord(
            sid.strip(),
            _coerce_occupancy(data.get("window_occupancy")),
            transcript,
        )


_store: SessionStateStore | None = None


def get_session_state_store() -> SessionStateStore:
    """Return the process-wide store, building a Redis client on first use.

    Reads ``REDIS_HOST`` / ``REDIS_PORT`` / ``REDIS_DB`` exactly like
    ``message_store.get_message_store`` so the session-state store lands on the
    same Redis the rest of the orchestrator uses. ``decode_responses=False`` —
    this module owns its bytes↔JSON handling.
    """
    global _store
    if _store is None:
        import redis

        host = os.environ.get("REDIS_HOST", "localhost")
        port = int(os.environ.get("REDIS_PORT", "6379"))
        db = int(os.environ.get("REDIS_DB", "0"))
        client = redis.Redis(host=host, port=port, db=db, decode_responses=False)
        _store = SessionStateStore(client)
    return _store


def reset_session_state_store() -> None:
    """Reset the process-wide store (tests inject a fakeredis-backed store)."""
    global _store
    _store = None


def set_session_state_store(store: SessionStateStore | None) -> None:
    """Install a store instance (tests inject ``SessionStateStore(fakeredis.FakeRedis())``)."""
    global _store
    _store = store
