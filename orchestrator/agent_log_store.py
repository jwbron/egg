"""Redis-backed store for one-shot agent pod logs captured at removal (#3547).

In the event-driven model most agent runs last seconds to minutes, and the
backing Job is reaped moments after the loop observes its exit (the #3181
observe-once sweep, the #3337 superseded-sibling teardown, pipeline cleanup)
or by the Job's own ``ttlSecondsAfterFinished``. Once the pod is gone,
``get_container_logs`` returns 404 and the stdout evidence of *why* an agent
exited is unrecoverable; during incident response operators had to race a
respawn to pull logs while the pod was briefly live.

This store is the durable home for that evidence: ``remove_agent_job``
best-effort snapshots the pod's log tail (plus its identifying labels and
exit code) here before deleting the Job, and the container-logs read path
falls back to it when the live pod is gone. Records are keyed
``agent-logs:{pipeline_id}:{job_name}`` and reaped by TTL.

Mirrors ``session_state_store``: same Redis, same best-effort contract -
every failure logs and degrades (capture skipped, fallback misses) rather
than raising into the removal or request path.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("orchestrator.agent_log_store")

__all__ = [
    "AGENT_LOG_TTL_SECONDS",
    "MAX_LOG_BYTES",
    "AgentLogStore",
    "get_agent_log_store",
    "reset_agent_log_store",
    "set_agent_log_store",
]

_KEY_PREFIX = "agent-logs"

# TTL on each captured log. Sized for incident response; long enough that an
# operator diagnosing a stall hours later still has the evidence, short enough
# that Redis never accumulates a pipeline's whole history.
AGENT_LOG_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Cap on a stored log. A one-shot agent run's stdout is small (its transcript
# lives in the session-state store); when a pathological pod exceeds this, the
# *tail* is kept; the exit evidence is at the end.
MAX_LOG_BYTES = 1 * 1024 * 1024  # 1 MiB


class AgentLogStore:
    """Redis-backed CRUD over per-(pipeline, job) captured-log records.

    Constructed with an injected redis client (real in production via
    :func:`get_agent_log_store`, ``fakeredis.FakeRedis()`` in tests). Stores
    bytes (``decode_responses=False``) and owns its own JSON (de)serialisation.
    Every method is best-effort: a Redis/serialisation failure logs and returns
    the miss sentinel (``None`` / ``False`` / ``[]``); it never raises into
    the caller's removal or request path.
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    @staticmethod
    def _key(pipeline_id: str, job_name: str) -> str:
        return f"{_KEY_PREFIX}:{pipeline_id}:{job_name}"

    def put(
        self,
        pipeline_id: str,
        job_name: str,
        *,
        logs: str,
        agent_role: str | None = None,
        slice_id: str | None = None,
        exit_code: int | None = None,
        captured_at: str | None = None,
    ) -> bool:
        """Persist (overwrite) a captured log under a fresh TTL; return whether stored.

        An oversized log is tail-truncated to :data:`MAX_LOG_BYTES`; the exit
        evidence lives at the end; rather than dropped.
        """
        if not pipeline_id or not job_name:
            return False
        encoded = logs.encode("utf-8")
        truncated = False
        if len(encoded) > MAX_LOG_BYTES:
            logs = encoded[-MAX_LOG_BYTES:].decode("utf-8", errors="replace")
            truncated = True
        record = {
            "job_name": job_name,
            "agent_role": agent_role,
            "slice_id": slice_id,
            "exit_code": exit_code,
            "captured_at": captured_at or datetime.now(UTC).isoformat(),
            "truncated": truncated,
            "logs": logs,
        }
        try:
            payload = json.dumps(record).encode("utf-8")
            self._redis.setex(self._key(pipeline_id, job_name), AGENT_LOG_TTL_SECONDS, payload)
            return True
        except Exception as exc:  # noqa: BLE001; best-effort; never block removal
            logger.warning(
                "Failed to persist agent logs (pipeline=%s job=%s): %s",
                pipeline_id,
                job_name,
                exc,
            )
            return False

    def get(self, pipeline_id: str, job_name: str) -> dict[str, Any] | None:
        """Read a captured-log record, or ``None``; never raising."""
        try:
            raw = self._redis.get(self._key(pipeline_id, job_name))
        except Exception as exc:  # noqa: BLE001; best-effort read
            logger.warning(
                "Failed to read agent logs (pipeline=%s job=%s): %s",
                pipeline_id,
                job_name,
                exc,
            )
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError, TypeError:
            logger.warning(
                "Malformed agent-log payload (pipeline=%s job=%s); ignoring",
                pipeline_id,
                job_name,
            )
            return None
        return data if isinstance(data, dict) else None

    def list_records(self, pipeline_id: str, *, include_logs: bool = False) -> list[dict[str, Any]]:
        """Enumerate the pipeline's captured logs, newest first.

        Metadata only by default (``logs`` replaced with ``log_bytes``) so the
        index stays cheap to return over MCP; ``include_logs=True`` keeps the
        bodies for callers that want the newest record in one pass.
        """
        try:
            keys = list(self._redis.scan_iter(match=f"{_KEY_PREFIX}:{pipeline_id}:*".encode()))
        except Exception as exc:  # noqa: BLE001; best-effort index
            logger.warning("Failed to scan agent-log keys (pipeline=%s): %s", pipeline_id, exc)
            return []
        entries: list[dict[str, Any]] = []
        for key in keys:
            key_str = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else key
            job_name = key_str.rsplit(":", 1)[-1]
            record = self.get(pipeline_id, job_name)
            if record is None:
                continue
            if not include_logs:
                logs = record.pop("logs", "") or ""
                record["log_bytes"] = len(logs.encode("utf-8"))
            entries.append(record)
        entries.sort(key=lambda r: r.get("captured_at") or "", reverse=True)
        return entries


_store: AgentLogStore | None = None


def get_agent_log_store() -> AgentLogStore:
    """Return the process-wide store, building a Redis client on first use.

    Reads ``REDIS_HOST`` / ``REDIS_PORT`` / ``REDIS_DB`` exactly like
    ``session_state_store.get_session_state_store`` so captured logs land on
    the same Redis the rest of the orchestrator uses.
    """
    global _store
    if _store is None:
        import redis

        host = os.environ.get("REDIS_HOST", "localhost")
        port = int(os.environ.get("REDIS_PORT", "6379"))
        db = int(os.environ.get("REDIS_DB", "0"))
        client = redis.Redis(host=host, port=port, db=db, decode_responses=False)
        _store = AgentLogStore(client)
    return _store


def reset_agent_log_store() -> None:
    """Reset the process-wide store (tests inject a fakeredis-backed store)."""
    global _store
    _store = None


def set_agent_log_store(store: AgentLogStore | None) -> None:
    """Install a store instance (tests inject ``AgentLogStore(fakeredis.FakeRedis())``)."""
    global _store
    _store = store
