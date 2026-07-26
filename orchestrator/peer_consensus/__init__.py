"""BRC (Broadcast-Review-Converge) peer consensus tracker.

Replaces the ConsensusEvaluator READY-tallying with a structured
peer consensus protocol. Agents propose, review, and confirm
through an asymmetric review graph.

State machine per producer:
    WORKING -> PROPOSED -> CONFIRMED
        ^         |
        └─────────┘  (NACK received -> address -> re-propose)

State machine per reviewer:
    WORKING -> REVIEWING -> CONFIRMED
                |   ^
                └───┘  (producer re-proposes -> re-review)
"""

import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from approval_matrix import ApprovalMatrix
from attestation_schemas import AttestationStrictness
from egg_orchestrator.types import ConsensusPhase
from review_graph import ReviewGraph

logger = get_logger("orchestrator.peer_consensus")


# Default configuration
DEFAULT_COOLDOWN_SECONDS = 30
DEFAULT_MAX_FLIP_FLOPS = 3
DEFAULT_MAX_REVISION_ROUNDS = 2


from . import (
    _confirm,
    _proposals,
    _queries,
    _recovery,
    _state,
)


class PeerConsensusTracker:
    """Tracks BRC consensus state for a single pipeline phase.

    Manages per-agent ConsensusPhase, the ReviewGraph, and the
    ApprovalMatrix. Handles proposals, ACKs, NACKs, withdrawals,
    and confirmations.
    """

    def __init__(
        self,
        pipeline_id: str,
        graph: ReviewGraph,
        *,
        attestation_strictness: AttestationStrictness = AttestationStrictness.STRICT,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        max_flip_flops: int = DEFAULT_MAX_FLIP_FLOPS,
        max_revision_rounds: int = DEFAULT_MAX_REVISION_ROUNDS,
        auto_repropose_debounce_seconds: int = 60,
        max_auto_repropose: int = 5,
        enable_invariant_checks: bool = False,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.graph = graph
        self.matrix = ApprovalMatrix(graph)
        self.attestation_strictness = attestation_strictness
        self.cooldown_seconds = cooldown_seconds
        self.max_flip_flops = max_flip_flops
        self.max_revision_rounds = max_revision_rounds
        self.auto_repropose_debounce_seconds = auto_repropose_debounce_seconds
        self.max_auto_repropose = max_auto_repropose
        self.enable_invariant_checks = enable_invariant_checks

        self._lock = threading.RLock()

        # Per-agent BRC phase (producer state machine)
        self._producer_phases: dict[str, ConsensusPhase] = {}
        # Per-agent BRC phase (reviewer state machine)
        self._reviewer_phases: dict[str, ConsensusPhase] = {}
        # Per-agent confirmed status (both state machines must confirm)
        self._confirmed: set[str] = set()
        # Producers whose latest action was a withdraw (proposal retracted,
        # #3470). Set on handle_withdraw, cleared on the next propose /
        # re-propose. release_contract_nack reads this to avoid restoring
        # PROPOSED for a producer that retracted its proposal — restoring
        # would let a released reviewer ACK withdrawn work. Derived purely
        # from replayed WITHDRAW / PROPOSE messages, so it survives
        # reconstruct_tracker_from_messages.
        self._withdrawn_producers: set[str] = set()
        # Timestamp of last proposal per producer (for cooldown)
        self._proposal_timestamps: dict[str, datetime] = {}
        # Flip-flop counter per producer (proposal -> withdraw cycles)
        self._flip_flop_counts: dict[str, int] = {}
        # Track proposal artifacts per producer (for scoped re-evaluation)
        self._proposal_artifacts: dict[str, list[str]] = {}
        # Track proposal commit SHAs per producer (#1473)
        self._proposal_commit_shas: dict[str, str] = {}
        # Per-version commit SHA history per producer (#2887). Unlike
        # ``_proposal_commit_shas`` (overwritten each propose, holds only
        # the current version) this accumulates ``{version: commit_sha}``
        # across re-proposes so a re-review notice can resolve the commit
        # a given reviewer last verdicted at (``entry.version`` →
        # commit_sha) and emit an authoritative per-reviewer delta range
        # (``<last_sha>..HEAD``) instead of a hardcoded v1→v2 anchor.
        self._proposal_commit_sha_history: dict[str, dict[int, str]] = {}
        # Whether handle_timeout() has already processed the timeout
        self._timeout_handled: bool = False
        # Auto re-propose safety: debounce timestamps and counters
        self._last_auto_repropose_timestamp: dict[str, datetime] = {}
        self._auto_repropose_counts: dict[str, int] = {}
        # Consecutive explicit re-proposes rejected for carrying the same
        # commit SHA as the current proposal (#3395). Informational — the
        # unchanged-tree guard rejects every unchanged-tree attempt on both
        # the handle_re_propose and handle_propose paths (#3415); the count
        # is surfaced in the rejection envelope. Reset when a re-propose
        # with a new SHA lands.
        self._unchanged_repropose_counts: dict[str, int] = {}
        # Track when producers explicitly propose (via handle_propose, NOT via
        # auto-repropose).  Used by check_auto_repropose to suppress redundant
        # auto-reproposals when a push arrives shortly after an explicit proposal.
        self._last_explicit_propose_timestamp: dict[str, datetime] = {}
        # Highest proposal version for which a producer has already received a
        # "ready to confirm" nudge.  A new proposal bumps the version and
        # naturally re-arms the nudge — see _collect_newly_ready_producers.
        # In-memory only by design: an orchestrator restart re-nudges any
        # still-ready producer on the next ACK/PROPOSE, which is harmless
        # because handle_confirmed is idempotent under check_confirm_guard.
        self._nudged_versions: dict[str, int] = {}
        # Open-NACK barrier (#2142): the latest NACK timestamp that the
        # producer has been informed of via a re_propose rejection.  Reset on
        # successful re_propose (version advances; prior NACKs are historical).
        # Forces aggregation in multi-reviewer concurrent BRC: a producer
        # cannot advance the proposal version while NACKs against the current
        # version remain undelivered.
        self._open_nack_notified_at: dict[str, datetime] = {}

    @property
    def confirmed_roles(self) -> frozenset[str]:
        """Read-only view of roles that have completed the full confirmation flow."""
        with self._lock:
            return frozenset(self._confirmed)

    # -- method bindings (bodies in private submodules, #3312 slice-10) --
    # _state
    get_current_proposal_snapshot = _state.get_current_proposal_snapshot
    _current_proposal_snapshots = _state._current_proposal_snapshots
    register_agent = _state.register_agent
    release_nudge = _state.release_nudge
    _rearm_nudge_on_guard_rejection = _state._rearm_nudge_on_guard_rejection
    _collect_newly_ready_producers = _state._collect_newly_ready_producers
    _run_invariant_checks = _state._run_invariant_checks
    remove_agent = _state.remove_agent
    clear = _state.clear
    _un_confirm_stale_reviewers = _state._un_confirm_stale_reviewers
    _invalidate_pre_proposal_acks = _state._invalidate_pre_proposal_acks
    _check_consensus = _state._check_consensus

    # _proposals
    handle_propose = _proposals.handle_propose
    _handle_propose_inner = _proposals._handle_propose_inner
    handle_ack = _proposals.handle_ack
    handle_nack = _proposals.handle_nack
    handle_withdraw = _proposals.handle_withdraw
    handle_resolve_obligation = _proposals.handle_resolve_obligation

    # _confirm
    handle_confirmed = _confirm.handle_confirmed
    handle_re_propose = _confirm.handle_re_propose
    _open_nacks_barrier_response = _confirm._open_nacks_barrier_response
    _unchanged_tree_guard_response = _confirm._unchanged_tree_guard_response
    check_auto_repropose = _confirm.check_auto_repropose
    handle_producer_push = _confirm.handle_producer_push

    # _recovery
    validate_invariants = _recovery.validate_invariants
    handle_agent_crash = _recovery.handle_agent_crash
    handle_stall_demotion = _recovery.handle_stall_demotion
    excuse_reviewer = _recovery.excuse_reviewer
    excuse_producer = _recovery.excuse_producer
    reopen_producer = _recovery.reopen_producer
    release_contract_nack = _recovery.release_contract_nack
    is_timeout_handled = _recovery.is_timeout_handled
    handle_timeout = _recovery.handle_timeout

    # _queries
    get_proposal_commit_sha = _queries.get_proposal_commit_sha
    get_all_proposal_commit_shas = _queries.get_all_proposal_commit_shas
    get_commit_sha_for_version = _queries.get_commit_sha_for_version
    get_pre_merge_conditions = _queries.get_pre_merge_conditions
    consensus_state_fingerprint = _queries.consensus_state_fingerprint
    get_latest_proposal_timestamp = _queries.get_latest_proposal_timestamp
    get_latest_progress_timestamp = _queries.get_latest_progress_timestamp
    evaluate = _queries.evaluate
    get_state = _queries.get_state
    is_producer_pending_confirm = _queries.is_producer_pending_confirm
    are_all_producers_working = _queries.are_all_producers_working
    get_earliest_proposal_time = _queries.get_earliest_proposal_time
    get_fully_acked_producers = _queries.get_fully_acked_producers
    get_agent_phase = _queries.get_agent_phase


# --- Pipeline-level tracker management ---

_trackers: dict[str, PeerConsensusTracker] = {}
_trackers_lock = threading.Lock()


def _resolve_run_epoch(pipeline_id: str, run_epoch: str | None = None) -> str:
    """Resolve a run_epoch string for key composition.

    When ``run_epoch`` is explicitly supplied (e.g. from a Pipeline object's
    ``run_epoch`` field), use it directly. When ``None``, fall back to
    ``pipeline_id`` itself as the epoch marker — this preserves backward
    compatibility for callers that have not yet been migrated to pass
    ``run_epoch`` explicitly, and for fresh pipelines where ``run_epoch``
    has not been set yet (the create-path clear will wipe all epochs
    anyway).

    The returned string is used as a namespace component in tracker keys
    and message-stream keys, so a resumed pipeline (which gets a fresh
    ``run_epoch`` on CANCELLED→RUNNING) starts with a clean namespace and
    cannot replay pre-cancel CONSENSUS_* messages (#3632).
    """
    if run_epoch is not None:
        return run_epoch
    # Fallback: use pipeline_id as the epoch marker for backward compat.
    # This is safe because:
    # 1. The create-path clear wipes ALL epochs for a pipeline_id.
    # 2. A fresh pipeline with run_epoch=None has no prior messages to replay.
    # 3. Callers that pass run_epoch=None are operating on a pipeline that
    #    hasn't been resumed yet (no stale-state replay hazard).
    return pipeline_id


def _tracker_key(
    pipeline_id: str,
    slice_id: str | None = None,
    run_epoch: str | None = None,
) -> str:
    """Compose the tracker registry key.

    Namespaced by ``(pipeline_id, run_epoch, slice_id)`` so that a resumed
    pipeline (fresh ``run_epoch``) gets a clean consensus namespace and
    cannot replay pre-cancel CONSENSUS_* messages (#3632).

    Slice-aware (#2137 TASK-4-3, refine-phase decision-14 hybrid):

    * When ``slice_id`` is supplied, the key is the nested form
      ``{pipeline_id}:{epoch}/{slice_id}``. Each slice's BRC consensus has
      its own tracker, completely isolated from siblings.
    * When ``slice_id`` is ``None``, the key is
      ``{pipeline_id}:{epoch}`` — preserving the pre-slicing
      single-tracker semantics so cross-slice telemetry (HEARTBEAT,
      OVERSEER_ALERT) keeps flowing through the pipeline-scoped tracker.

    The function is idempotent on already-nested ids (callers that
    have constructed ``"issue-N/slice-M"`` themselves don't get a
    double prefix).
    """
    epoch = _resolve_run_epoch(pipeline_id, run_epoch)
    if slice_id is None:
        return f"{pipeline_id}:{epoch}"
    if "/" in pipeline_id and pipeline_id.endswith(f"/{slice_id}"):
        return f"{pipeline_id}:{epoch}"
    return f"{pipeline_id}:{epoch}/{slice_id}"


def get_peer_consensus_tracker(
    pipeline_id: str,
    slice_id: str | None = None,
    run_epoch: str | None = None,
) -> PeerConsensusTracker | None:
    """Get the tracker for a pipeline (or per-slice tracker), if one exists.

    ``run_epoch`` namespaces the tracker so a resumed pipeline (fresh
    ``run_epoch``) gets a clean tracker that cannot be polluted by
    pre-cancel CONSENSUS_* messages (#3632).
    """
    return _trackers.get(_tracker_key(pipeline_id, slice_id, run_epoch))


def get_slice_trackers(pipeline_id: str) -> dict[str, PeerConsensusTracker]:
    """Return the live slice-scoped trackers for a pipeline, keyed by slice_id.

    Observability accessor (#3481): during a slice-DAG implement phase
    the registry holds per-slice trackers under ``{pipeline_id}:{epoch}/{slice_id}``
    and nothing under the bare pipeline id, so a slice-id-less status
    query cannot see any consensus state. This enumerates the registry
    so callers (``_get_concurrent_status``) can serve each active
    slice's real tracker snapshot, explicitly keyed by slice; NOT a
    merged cross-slice view (#2761's "soup" concern only applies to
    mingling slices into one tracker). In-memory registry only: after
    an orchestrator restart slices reappear here as their trackers are
    reconstructed on first slice-scoped access.
    """
    # Keys are now ``{pipeline_id}:{epoch}/{slice_id}`` or
    # ``{pipeline_id}:{epoch}``. Match any key starting with
    # ``{pipeline_id}:`` so we catch all epochs and slices.
    prefix = f"{pipeline_id}:"
    with _trackers_lock:
        result: dict[str, PeerConsensusTracker] = {}
        for key, tracker in _trackers.items():
            if not key.startswith(prefix):
                continue
            # Strip the pipeline_id:epoch prefix to get the slice_id
            remainder = key[len(prefix) :]
            if "/" in remainder:
                slice_id_part = remainder.split("/", 1)[1]
                result[slice_id_part] = tracker
        return result


def create_peer_consensus_tracker(
    pipeline_id: str,
    graph: ReviewGraph,
    *,
    slice_id: str | None = None,
    run_epoch: str | None = None,
    **kwargs: Any,
) -> PeerConsensusTracker:
    """Create and register a tracker for a pipeline (or per-slice scope).

    When ``slice_id`` is supplied the tracker's logical pipeline_id
    is the nested ``{pipeline_id}:{epoch}/{slice_id}`` so CONSENSUS_*
    messages naturally route to the per-slice tracker. The bare
    pipeline-level tracker (without slice_id) keeps existing
    single-tracker pipelines working unchanged.

    ``run_epoch`` namespaces the tracker so a resumed pipeline (fresh
    ``run_epoch``) gets a clean tracker that cannot be polluted by
    pre-cancel CONSENSUS_* messages (#3632).
    """
    key = _tracker_key(pipeline_id, slice_id, run_epoch)
    with _trackers_lock:
        tracker = PeerConsensusTracker(key, graph, **kwargs)
        _trackers[key] = tracker
    return tracker


def remove_peer_consensus_tracker(
    pipeline_id: str,
    slice_id: str | None = None,
    run_epoch: str | None = None,
) -> None:
    """Remove a tracker for a pipeline (or per-slice scope).

    When ``run_epoch`` is ``None``, removes ALL epoch namespaces for the
    given ``pipeline_id`` (and optional ``slice_id``). This is used by
    the DELETE and CREATE paths to defend #2053: a new pipeline reusing
    a terminal pipeline's id must not inherit any prior run's
    CONFIRMED consensus.

    When ``run_epoch`` is supplied, removes only that specific epoch
    namespace — used by phase transitions and FAILED clears.
    """
    if run_epoch is None:
        # Remove all epoch namespaces for this pipeline_id (and optional slice).
        prefix = f"{pipeline_id}:"
        with _trackers_lock:
            keys_to_remove = [
                k for k in _trackers
                if k.startswith(prefix)
                and (slice_id is None or k.endswith(f"/{slice_id}") or "/" not in k[len(prefix):])
            ]
            for key in keys_to_remove:
                tracker = _trackers.pop(key, None)
                if tracker:
                    tracker.clear()
    else:
        key = _tracker_key(pipeline_id, slice_id, run_epoch)
        with _trackers_lock:
            tracker = _trackers.pop(key, None)
            if tracker:
                tracker.clear()


def _message_slice_id(message: Any) -> str | None:
    """Return the ``slice_id`` a consensus message was tagged with, or ``None``.

    Every ``CONSENSUS_*`` message written by a per-slice agent carries
    ``slice_id`` in ``metadata`` (the ``_slice_meta`` spread in
    ``orchestrator/routes/signals.py``); pipeline-level messages omit it.
    Reconstruction reads it from there so a per-slice replay never
    mingles sibling slices' messages into one tracker.
    """
    metadata = getattr(message, "metadata", None) or {}
    return metadata.get("slice_id")


def reconstruct_tracker_from_messages(
    pipeline_id: str,
    graph: ReviewGraph,
    *,
    message_store: Any = None,
    slice_id: str | None = None,
    phase: str | None = None,
    run_epoch: str | None = None,
) -> PeerConsensusTracker | None:
    """Reconstruct a consensus tracker by replaying messages from the message store.

    Called when the in-memory tracker is lost (e.g. after orchestrator restart)
    but consensus messages are preserved in Redis. Replays PROPOSE, ACK, NACK,
    WITHDRAW, and CONFIRMED messages in timestamp order to rebuild state.

    Args:
        pipeline_id: Pipeline ID to reconstruct. Always the *bare* id — the
            message store keys messages by ``(pipeline_id, run_epoch)``
            regardless of slice scope.
        graph: ReviewGraph for the pipeline's current phase.
        message_store: Optional message store override (for testing).
        slice_id: When set, replay only messages tagged with this slice
            (``metadata['slice_id']``) and register the tracker under the
            nested ``{pipeline_id}:{epoch}/{slice_id}`` key. When ``None``,
            replay only pipeline-level messages (those with no ``slice_id``
            tag) — so a slice-DAG pipeline queried without a slice scope
            does not silently reconstruct a cross-slice tracker (#2761).
        phase: When set, replay only messages from this pipeline phase.
            Guards against replaying an earlier phase's consensus (e.g.
            refine/plan) into the current phase's review graph. A
            message with ``phase is None`` is treated as matching any
            phase — symmetric with the CONSENSUS_CONFIRMED idempotency
            probe in ``routes/signals.py`` (a null phase is the
            conservative match: every emitter sets it, but if one
            doesn't, include rather than drop).
        run_epoch: When set, namespace the reconstruction to this epoch's
            message stream and register the tracker under
            ``{pipeline_id}:{epoch}``. This is the key safety mechanism
            for #3632: a resumed pipeline gets a fresh ``run_epoch``, so
            ``reconstruct_tracker_from_messages`` reads only the new
            epoch's messages and cannot replay pre-cancel CONSENSUS_*
            messages into the reset round. When ``None``, falls back to
            the pipeline_id as the epoch marker (backward compat for
            callers that haven't been migrated).

    Returns:
        Reconstructed tracker registered in the global tracker dict,
        or None if no matching consensus messages were found.
    """
    if message_store is None:
        try:
            from message_store import get_message_store

            message_store = get_message_store()
        except ImportError:
            logger.warning("Cannot reconstruct tracker: message_store unavailable")
            return None

    # Fetch all messages for this pipeline (generous limit for reconstruction)
    messages = message_store.get_messages(pipeline_id, limit=10000, run_epoch=run_epoch)

    # Filter to consensus-related message types
    consensus_types = {
        "CONSENSUS_PROPOSE",
        "CONSENSUS_ACK",
        "CONSENSUS_NACK",
        "CONSENSUS_WITHDRAW",
        "CONSENSUS_CONFIRMED",
        # In-cycle conditional-ACK obligation resolution (#2338). Replayed
        # so the resolved flag survives orchestrator restarts — without
        # this, a satisfied obligation re-emerges and the HITL gate
        # asks the operator about work that was already done.
        "CONSENSUS_OBLIGATION_RESOLVED",
        # Confirmed-producer reopen after task reassignment (#3124).
        # Replayed so the CONFIRMED→WORKING transition survives restarts —
        # without it, replay would reject the producer's post-reopen
        # proposal (the propose guard requires WORKING) and resurrect the
        # deadlock the reopen resolved.
        "CONSENSUS_REOPENED",
        # Contract-blocked NACK release (#3470). Replayed so the
        # NACKED→PENDING re-review transition survives restarts — without
        # it, replay would resurrect the NACK against a producer that can
        # no longer re-propose (zero new commits) and restore the
        # deadlock the release resolved.
        "CONSENSUS_NACK_INVALIDATED",
    }
    consensus_msgs = [m for m in messages if m.message_type in consensus_types]

    # Scope the replay to the requested slice and phase. Without the
    # slice filter a slice-DAG pipeline's messages — all keyed under the
    # bare pipeline_id but tagged per-slice in metadata — would replay
    # into one tracker and reach a meaningless cross-slice state (#2761).
    #
    # Null-phase semantics mirror the CONSENSUS_CONFIRMED idempotency
    # probe in ``routes/signals.py`` (see comment at
    # ``_handle_consensus_confirmed_idempotency_probe``): a message with
    # ``phase is None`` is treated as matching any phase filter. In
    # practice every CONSENSUS_* message that ``routes/signals.py``
    # emits sets a phase, but if one somehow doesn't, the conservative
    # choice is to *include* it rather than drop it — symmetric with the
    # probe, where the divergence would otherwise let one path replay a
    # null-phase message that the other path silently skipped.
    consensus_msgs = [
        m
        for m in consensus_msgs
        if _message_slice_id(m) == slice_id
        and (
            phase is None or getattr(m, "phase", None) is None or getattr(m, "phase", None) == phase
        )
    ]

    if not consensus_msgs:
        return None

    tracker_key = _tracker_key(pipeline_id, slice_id, run_epoch)

    # Create tracker with relaxed attestation and no cooldown for replaying
    # historical messages. RELAXED mode is kept for the tracker's remaining
    # lifetime because: (1) reconstructed trackers are near end-of-life —
    # consensus is typically already reached or close to it, and (2) any new
    # proposals post-reconstruction will still be validated by the review
    # graph structure (required reviewers, quorum), just not by attestation
    # signature checks.
    tracker = PeerConsensusTracker(
        tracker_key,
        graph,
        attestation_strictness=AttestationStrictness.RELAXED,
        cooldown_seconds=0,
    )

    # Discover and register agents from message from_role and to_role fields
    discovered_roles: set[str] = set()
    for msg in consensus_msgs:
        discovered_roles.add(msg.from_role)
        if msg.to_role and msg.to_role != "all":
            discovered_roles.add(msg.to_role)

    # Only register roles that exist in the review graph
    all_graph_roles = graph.all_roles()
    for role in discovered_roles:
        if role in all_graph_roles:
            tracker.register_agent(role)

    # Sort by timestamp for deterministic replay.  Use message sequence
    # number as tiebreaker when timestamps match, ensuring stable replay
    # order for auto-re-propose deduplication.
    consensus_msgs.sort(key=lambda m: (m.timestamp, getattr(m, "id", "")))

    # Track auto-re-propose timestamps per producer for debounce during
    # replay — prevents redundant version inflation from rapid auto-re-
    # propose messages within the debounce window.
    _replay_auto_repropose_ts: dict[str, datetime] = {}
    _auto_repropose_debounce = 60  # seconds — match default debounce

    # Replay messages
    for msg in consensus_msgs:
        try:
            if msg.message_type == "CONSENSUS_PROPOSE":
                metadata = msg.metadata or {}
                payload = metadata.get("payload", {})
                if not payload:
                    # Minimal payload for reconstruction
                    payload = {"summary": msg.body or "reconstructed", "artifacts": []}
                # Ensure commit_sha is present for reconstruction (#1473).
                # Historical messages may pre-date this requirement.
                # Use an explicit sentinel so callers of
                # get_proposal_commit_sha() can distinguish it from a real SHA.
                #
                # Skipped for a no-op propose (#3027): a no-op carries no
                # commit_sha by design — ``ProposalPayload.validate_commit_sha_present``
                # is bypassed for it — so injecting the sentinel would write
                # misleading audit data (``RECONSTRUCTED_NO_SHA`` against a
                # producer that never had a commit to point at) into the
                # commit-sha history. Leave it empty.
                if not payload.get("commit_sha") and not payload.get("no_changes_needed"):
                    payload["commit_sha"] = "RECONSTRUCTED_NO_SHA"

                # Debounce auto-re-propose messages during replay:
                # If this is an auto-triggered re-propose (trigger=auto_push),
                # check the debounce window to avoid inflating proposal versions
                # from rapid pushes during a single development session.
                is_auto = metadata.get("auto_re_propose") or metadata.get("trigger") == "auto_push"
                if is_auto:
                    producer = msg.from_role
                    last_auto_ts = _replay_auto_repropose_ts.get(producer)
                    if last_auto_ts is not None:
                        elapsed = (msg.timestamp - last_auto_ts).total_seconds()
                        if elapsed < _auto_repropose_debounce:
                            logger.debug(
                                "Skipping debounced auto-re-propose during reconstruction",
                                producer=producer,
                                elapsed_seconds=elapsed,
                                pipeline_id=pipeline_id,
                            )
                            continue
                    _replay_auto_repropose_ts[producer] = msg.timestamp

                tracker.handle_propose(msg.from_role, payload)

            elif msg.message_type == "CONSENSUS_ACK":
                producer_role = msg.to_role
                payload = msg.metadata.get("payload", {})
                if not payload:
                    payload = {"reason": msg.body or "reconstructed"}
                # Ensure artifact_references is non-empty (ReviewPayload validates this)
                if not payload.get("artifact_references"):
                    payload["artifact_references"] = ["reconstructed"]
                tracker.handle_ack(msg.from_role, producer_role, payload)

            elif msg.message_type == "CONSENSUS_NACK":
                producer_role = msg.to_role
                payload = msg.metadata.get("payload", {})
                if not payload:
                    payload = {"reason": msg.metadata.get("reason", msg.body or "reconstructed")}
                if not payload.get("artifact_references"):
                    payload["artifact_references"] = ["reconstructed"]
                tracker.handle_nack(msg.from_role, producer_role, payload)

            elif msg.message_type == "CONSENSUS_WITHDRAW":
                reason = msg.body or ""
                tracker.handle_withdraw(msg.from_role, reason)

            elif msg.message_type == "CONSENSUS_CONFIRMED":
                tracker.handle_confirmed(msg.from_role)

            elif msg.message_type == "CONSENSUS_REOPENED":
                # Emitted by the orchestrator (next-action route) when a
                # contract task was reassigned to an already-confirmed
                # producer (#3124). ``to_role`` carries the producer;
                # ``from_role`` is the orchestrator and is deliberately
                # not replayed as a participant. reopen_producer is
                # idempotent, so a duplicate message is harmless.
                reopened_producer = msg.to_role
                if reopened_producer and reopened_producer != "all":
                    tracker.reopen_producer(reopened_producer, reason="replay")

            elif msg.message_type == "CONSENSUS_NACK_INVALIDATED":
                # Emitted by the orchestrator (contract mutate route) when a
                # producer repaired the contract_incomplete blocker a
                # reviewer's NACK cited (#3470). Metadata carries the roles;
                # ``to_role`` doubles as the reviewer for older messages.
                # ``from_role`` is the orchestrator and is deliberately not
                # replayed as a participant. release_contract_nack is
                # idempotent, so a duplicate message is harmless.
                metadata = msg.metadata or {}
                released_reviewer = metadata.get("reviewer_role") or msg.to_role
                released_producer = metadata.get("producer_role")
                if not released_reviewer or released_reviewer == "all" or not released_producer:
                    logger.debug(
                        "Skipping NACK-invalidation message with missing roles",
                        pipeline_id=pipeline_id,
                        message_id=msg.id,
                    )
                    continue
                tracker.release_contract_nack(released_reviewer, released_producer, reason="replay")

            elif msg.message_type == "CONSENSUS_OBLIGATION_RESOLVED":
                # Metadata carries the participant roles plus optional
                # commit_sha / note for audit. The tracker raises
                # ValueError when the edge is missing or has no active
                # obligation; the outer try/except logs and skips so a
                # stale resolution message can't blow up reconstruction.
                metadata = msg.metadata or {}
                resolver_role = metadata.get("resolver_role") or msg.from_role
                reviewer_role = metadata.get("reviewer_role")
                producer_role = metadata.get("producer_role") or msg.to_role
                if not reviewer_role or not producer_role:
                    logger.debug(
                        "Skipping resolution message with missing roles",
                        pipeline_id=pipeline_id,
                        message_id=msg.id,
                    )
                    continue
                tracker.handle_resolve_obligation(
                    resolver_role=resolver_role,
                    reviewer_role=reviewer_role,
                    producer_role=producer_role,
                    commit_sha=metadata.get("commit_sha", ""),
                    note=metadata.get("note", ""),
                )

        except Exception as e:
            # Best-effort reconstruction: log and skip messages that fail
            logger.warning(
                "Skipping message during tracker reconstruction",
                pipeline_id=pipeline_id,
                message_id=msg.id,
                message_type=msg.message_type,
                from_role=msg.from_role,
                error=str(e),
            )

    # Register the reconstructed tracker globally, but avoid overwriting
    # a tracker that was created by a concurrent reconstruction or live
    # messages. Slice-scoped trackers register under the nested
    # ``{pipeline_id}/{slice_id}`` key so they never collide with the
    # bare pipeline tracker or a sibling slice.
    with _trackers_lock:
        if tracker_key not in _trackers:
            _trackers[tracker_key] = tracker
            was_registered = True
        else:
            tracker = _trackers[tracker_key]
            was_registered = False

    if was_registered:
        logger.info(
            "Reconstructed consensus tracker from messages",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            messages_replayed=len(consensus_msgs),
            confirmed_roles=sorted(tracker.confirmed_roles),
        )
    else:
        logger.info(
            "Reconstruction discarded: tracker already exists",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
        )

    return tracker
