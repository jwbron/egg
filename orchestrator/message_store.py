"""Message types and singleton accessor for the inter-agent message bus.

Defines the shared message envelope (:class:`Message`, :class:`MessageType`,
:class:`GetMessagesMeta`) exchanged between agents during concurrent phase
execution, and the :func:`get_message_store` singleton accessor for the
Redis Streams backend (:class:`redis_message_store.RedisMessageStore`).

Redis Streams is the only backend. The in-memory ``MessageStore`` that used
to live here — along with the ``auto``/``memory`` values of
``EGG_MESSAGE_STORE_BACKEND`` and the auto→memory fail-loud fallback
machinery from #3077 slice-6 — was removed in #3159 after #2662 / PR #3153
pinned every deployment to explicit redis mode. Messages are wiped at phase
transitions by design (the persisted ``.egg-state/brc-history/`` log is the
audit trail); see ``docs/architecture/coordination-state.md``.
"""

import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from redis_message_store import RedisMessageStore

logger = logging.getLogger("orchestrator.message_store")


class GetMessagesMeta(NamedTuple):
    """Side-channel metadata returned alongside ``get_messages_with_meta``.

    Issue #2464. The store advertises "fall back to full history" when a
    caller passes a ``since_id`` it doesn't recognize, but consumers had
    no structured way to *know* this happened — they just saw whatever
    messages came back and kept threading the same cursor forward, which
    produced the chronic ``since_id not found in store`` warning cadence
    described in #2454.

    ``since_id_stale`` is the explicit signal: ``True`` iff the caller
    passed a non-None ``since_id`` that did not resolve to any message
    in the store. Consumers (the route layer, the sandbox CLI cursor
    file) treat ``True`` as "your cursor is dead — drop it and re-snap
    to tip".
    """

    since_id_stale: bool


class MessageType:
    """Standard message types for inter-agent communication.

    Note on QUESTION (issue #1897 Phase 7): the QUESTION message type was
    removed. It is no longer a valid enum member. Inbound messages that
    still carry ``message_type="QUESTION"`` (e.g. replayed from an older
    checkpoint) are coerced to ``MessageType.PROGRESS`` by
    :func:`coerce_deprecated_message_type` below so they still appear in
    history and downstream pipelines don't crash on unknown types.  A
    follow-up issue will introduce a structured REQUEST/REPLY peer-Q&A
    subsystem that names a target peer and times out.
    """

    PROGRESS = "PROGRESS"
    STATUS = "STATUS"
    AGENT_FAILED = "AGENT_FAILED"
    HANDOFF = "HANDOFF"
    # Structured per-agent state heartbeat (issue #1897).
    # ``metadata`` is a JSON object with
    # ``{"state": ..., "waiting_on": ..., "since": ...}``;
    # ``body`` is a short human-readable summary (or empty string).
    # See docs/reference/agent-wait-patterns.md.
    HEARTBEAT = "HEARTBEAT"
    # Consensus protocol (BRC)
    CONSENSUS_PROPOSE = "CONSENSUS_PROPOSE"
    CONSENSUS_ACK = "CONSENSUS_ACK"
    CONSENSUS_NACK = "CONSENSUS_NACK"
    CONSENSUS_WITHDRAW = "CONSENSUS_WITHDRAW"
    CONSENSUS_CONFIRMED = "CONSENSUS_CONFIRMED"
    CONSENSUS_RE_REVIEW = "CONSENSUS_RE_REVIEW"
    # In-cycle conditional-ACK obligation resolution (#2338). Persisted so
    # ``reconstruct_tracker_from_messages`` can replay the resolution after
    # an orchestrator restart — without it, a satisfied obligation
    # re-emerges from replay and the HITL gate asks the operator about
    # work that was already done.
    CONSENSUS_OBLIGATION_RESOLVED = "CONSENSUS_OBLIGATION_RESOLVED"
    # Confirmed-producer reopen (#3124): a contract task was reassigned to
    # a producer after it CONFIRMED, so the orchestrator reopened its
    # consensus participation (CONFIRMED → WORKING). Persisted so
    # ``reconstruct_tracker_from_messages`` can replay the transition —
    # without it, replay would reject the producer's post-reopen proposal
    # (the propose guard requires WORKING) and a restart would resurrect
    # the deadlock the reopen resolved. ``to_role`` carries the producer.
    CONSENSUS_REOPENED = "CONSENSUS_REOPENED"
    # Overseer anomaly broadcasts (issue #1413)
    OVERSEER_ALERT = "OVERSEER_ALERT"
    # Tier 1 health monitor nudge messages (issue #1428)
    NUDGE = "NUDGE"


# Valid HEARTBEAT states (issue #1897). Validated server-side in routes/messages.py.
# ``WAITING_FOR_EVENT`` (issue #2036) is emitted by ``mcp__brc__wait_loop`` while
# it is blocking on a message filter. It is a liveness signal, not a state
# transition, so the dedup layer lets duplicates through for this state.
HEARTBEAT_STATES: frozenset[str] = frozenset(
    {
        "WORKING",
        "WAITING_ON_ROLE",
        "WAITING_FOR_EVENT",
        "PROPOSED",
        "IDLE",
    }
)

# Deprecated-in-#1897 message types that are tolerated on inbound/replay
# paths so existing on-disk brc-history files and in-flight pipelines
# don't crash on a now-unknown type. These map to a still-valid type
# that preserves the audit trail without reintroducing the deprecated
# channel.
_DEPRECATED_TYPE_COERCIONS: dict[str, str] = {
    # QUESTION became a PROGRESS-tier status message in #1897. Kept here
    # only for replay / deserialization safety; no code should emit
    # QUESTION at write time. See module docstring and reviewer_contract
    # blocker 2 on #1897.
    "QUESTION": "PROGRESS",
}


def coerce_deprecated_message_type(raw_type: str) -> str:
    """Normalise a deprecated message_type to its live replacement.

    Used by the Redis deserialization path so replayed messages whose
    ``message_type`` no longer exists on this version of the
    orchestrator still land in a :class:`Message` with a valid type.
    Unknown-but-not-deprecated types pass through unchanged — the rest
    of the pipeline treats unknown types as opaque.

    Returns the coerced type (e.g. ``"PROGRESS"``) if ``raw_type`` is
    in the deprecation map, otherwise the original ``raw_type``.
    """
    return _DEPRECATED_TYPE_COERCIONS.get(raw_type, raw_type)


class Message(BaseModel):
    """A message exchanged between agents via the orchestrator message bus."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:16])
    pipeline_id: str = Field(..., description="Pipeline this message belongs to")
    from_role: str = Field(..., description="Sender agent role")
    to_role: str = Field(default="all", description="Target role or 'all' for broadcast")
    message_type: str = Field(..., description="Message type (e.g., PROGRESS, HEARTBEAT)")
    subject: str = Field(default="", description="Message subject line")
    body: str = Field(default="", description="Message body content")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: str | None = Field(default=None, description="Pipeline phase when sent")

    # Issue #1962 OVERSEER_ALERT extension. These optional first-class
    # fields are populated only on OVERSEER_ALERT messages produced by
    # the advisor-gated path (TASK-3-3). Legacy callers that don't set
    # them serialize identically to today (None / 1) — the
    # backwards-compat regression test in TASK-7-1 asserts a pre-#1962
    # alert payload (no recommendation field) round-trips through the
    # message store and renders in /sdlc's alert-display path verbatim.
    recommendation: str | None = Field(
        default=None,
        description=(
            "Structured advisor recommendation (issue #1962). Currently "
            "the only legal value is 'file_issue'; the human gates the "
            "actual filing via the existing pending_decisions HITL flow. "
            "None for non-OVERSEER_ALERT messages and for legacy alerts."
        ),
    )
    recommendation_payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Opaque payload carrying the advisor's composed issue_title "
            "+ issue_body + priority + anomaly_signature when "
            "recommendation == 'file_issue'."
        ),
    )
    schema_version: int = Field(
        default=1,
        description=(
            "OVERSEER_ALERT schema version (issue #1962). 1 = pre-#1962 "
            "implicit (no recommendation fields); 2 = post-#1962 with "
            "recommendation / recommendation_payload populated. "
            "Defaults to 1 so legacy callers continue to round-trip."
        ),
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        result = {
            "id": self.id,
            "pipeline_id": self.pipeline_id,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "message_type": self.message_type,
            "subject": self.subject,
            "body": self.body,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
            "phase": self.phase,
        }
        # Issue #1962: emit the new fields only when populated so legacy
        # consumers see byte-identical output for pre-#1962 messages.
        if self.recommendation is not None:
            result["recommendation"] = self.recommendation
        if self.recommendation_payload is not None:
            result["recommendation_payload"] = self.recommendation_payload
        if self.schema_version != 1:
            result["schema_version"] = self.schema_version
        return result


# Singleton
_message_store: RedisMessageStore | None = None
_store_lock = threading.Lock()


def get_message_store() -> RedisMessageStore:
    """Get the singleton Redis Streams message store.

    Creation fails loudly on a bad ``EGG_MESSAGE_STORE_BACKEND`` value or
    an unreachable Redis — there is no fallback backend (#3159). Unit
    tests run against a fakeredis-backed store installed by the autouse
    fixture in ``orchestrator/tests/conftest.py``.
    """
    global _message_store
    if _message_store is None:
        with _store_lock:
            if _message_store is None:
                _message_store = _create_message_store()
    return _message_store


def _create_message_store() -> RedisMessageStore:
    """Create the Redis Streams message store — the only backend.

    The multi-backend selection (``EGG_MESSAGE_STORE_BACKEND`` =
    ``memory`` / ``redis`` / ``auto`` with auto→memory fallback) was
    removed in #3159: #2662 / PR #3153 pinned every deployment to
    explicit redis mode, so the in-memory backend could only ever be
    reached again by accident — re-introducing the #3076
    mid-phase-restart message-loss risk. ``redis`` and unset (there is
    nothing else the variable could mean) select Redis; any other value
    is stale multi-backend-era configuration and raises rather than
    silently meaning something different than it used to. A Redis
    connection failure also raises — no fallback.
    """
    import os

    backend = os.environ.get("EGG_MESSAGE_STORE_BACKEND", "redis")
    if backend != "redis":
        raise RuntimeError(
            f"Unsupported EGG_MESSAGE_STORE_BACKEND={backend!r}: the in-memory "
            "message-store backend was removed in #3159; 'redis' is the only "
            "backend. Unset the variable or set it to 'redis' and point "
            "REDIS_HOST/REDIS_PORT at a reachable Redis (the k8s manifests "
            "deploy one — see k8s/base/redis-deployment.yaml; local dev can "
            "run any redis-server). Unit tests use a fakeredis-backed store "
            "(see orchestrator/tests/conftest.py)."
        )

    from redis_message_store import get_redis_message_store

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    redis_db = int(os.environ.get("REDIS_MESSAGE_DB", "1"))  # Separate DB from other Redis usage
    store = get_redis_message_store(host=redis_host, port=redis_port, db=redis_db)
    logger.info(
        "Using Redis Streams message store",
        extra={"host": redis_host, "port": redis_port, "db": redis_db},
    )
    return store


def reset_message_store() -> None:
    """Reset the singleton message store (for testing)."""
    global _message_store
    _message_store = None
