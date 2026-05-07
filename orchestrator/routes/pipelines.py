"""
Pipeline CRUD endpoints for egg-orchestrator.
"""

import concurrent.futures
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import yaml

try:
    from docker.errors import DockerException
except ImportError:

    class DockerException(Exception):  # type: ignore[no-redef]
        pass


from flask import Blueprint, Response, jsonify, request, stream_with_context


class ForestValidationError(Exception):
    """Raised by ``_populate_contract_from_plan`` when slice DAG is non-forest.

    Added in #2137 (TASK-2-2). Any future Flask route that ingests a
    plan in-band can catch this and ``return jsonify({"errors":
    err.errors}), 422`` to surface the structured rejection per the
    plan's acceptance criteria. Internal callers (``_populate_contract
    _from_plan_safe`` and the pipeline run-loop helpers) catch this
    exception and log a warning — the ``plan_review_feedback`` stash
    placed on the contract by the populator is the durable signal
    the plan reviewer prompt reads from to NACK the planner.
    """

    def __init__(self, message: str, *, errors: list[str]) -> None:
        super().__init__(message)
        self.errors: list[str] = list(errors)
        self.status_code: int = 422

    def to_response(self) -> tuple[dict[str, object], int]:
        """Serialise into a Flask-compatible (body, status) tuple."""
        return ({"error": "forest_violation", "errors": self.errors}, 422)


# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Add config directory to path for repo_config module
_config_path = Path(__file__).parent.parent.parent / "config"
if _config_path.exists() and str(_config_path) not in sys.path:
    sys.path.insert(0, str(_config_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


try:
    from repo_config import get_repo_checks
except ImportError:

    def get_repo_checks(repo: str) -> list[dict[str, str]]:  # type: ignore[misc]
        return []


# Import orchestrator modules - try relative import first
try:
    from .. import agent_salvage
    from ..container_spawner import ContainerSpawnError, SpawnFailureError, get_container_spawner
    from ..decision_queue import get_decision_queue
    from ..docker_client import ContainerNotFoundError, ContainerOperationError, DockerClientError
    from ..gateway_client import GatewayError, _rebase_with_agent_output_autoresolve
    from ..kubernetes_client import (
        LABEL_PIPELINE_ID,
        JobOperationError,
        KubernetesClientError,
        PodNotFoundError,
    )
    from ..kubernetes_spawner import KubernetesSpawnError, get_kubernetes_spawner
    from ..models import (
        AgentExecutionStatus,
        AgentExitInfo,
        AgentRole,
        AggregatedReviewResult,
        ContainerStatus,
        CycleTiming,
        DecisionStatus,
        HITLDecision,
        Pipeline,
        PipelineMode,
        PipelinePhase,
        PipelineStatus,
        ReviewVerdict,
    )
    from ..slice_id_validation import SLICE_ID_PATTERN, extract_slice_id
    from ..state_store import (
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStore,
        StateStoreError,
        StateValidationError,
        get_pipeline_state_lock,
        get_state_store,
    )
except ImportError:
    import agent_salvage  # type: ignore[no-redef]
    from container_spawner import (  # type: ignore
        ContainerSpawnError,
        SpawnFailureError,
        get_container_spawner,
    )
    from decision_queue import get_decision_queue  # type: ignore
    from docker_client import (  # type: ignore
        ContainerNotFoundError,
        ContainerOperationError,
        DockerClientError,
    )
    from gateway_client import GatewayError, _rebase_with_agent_output_autoresolve  # type: ignore
    from kubernetes_client import (  # type: ignore
        LABEL_PIPELINE_ID,
        JobOperationError,
        KubernetesClientError,
        PodNotFoundError,
    )
    from kubernetes_spawner import (  # type: ignore
        KubernetesSpawnError,
        get_kubernetes_spawner,
    )
    from models import (  # type: ignore
        AgentExecutionStatus,
        AgentExitInfo,
        AgentRole,
        AggregatedReviewResult,
        ContainerStatus,
        CycleTiming,
        DecisionStatus,
        HITLDecision,
        Pipeline,
        PipelineMode,
        PipelinePhase,
        PipelineStatus,
        ReviewVerdict,
    )
    from slice_id_validation import SLICE_ID_PATTERN, extract_slice_id  # type: ignore
    from state_store import (  # type: ignore
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStore,
        StateStoreError,
        StateValidationError,
        get_pipeline_state_lock,
        get_state_store,
    )

from egg_git.default_branch import get_default_branch
from lifecycle_auth import require_lifecycle_secret

if TYPE_CHECKING:
    from egg_container import MountSpec

    try:
        from ..container_spawner import ContainerSpawner
    except ImportError:
        from container_spawner import ContainerSpawner  # type: ignore

logger = get_logger("orchestrator.pipelines")


# -----------------------------------------------------------------
# egg_inflight_host_waits metric (issue #1932 TASK-1-3).
#
# Gauge counting in-flight ``/status/wait`` route calls.  Paired with
# ``egg_inflight_long_polls`` from ``routes/messages.py`` — both draw
# against the same Waitress thread pool so operators alert on the
# sum when it approaches ``EGG_ORCH_WAITRESS_THREADS``.  The
# lame-duck daemon thread that keeps running after the route returns
# (up to ``wait`` seconds of ``message_store.get_messages``) is
# deliberately NOT counted against this gauge — the metric represents
# in-flight *route* calls, not in-flight store waits.
#
# Best-effort registration so missing-metrics-backend deployments
# degrade gracefully (matches the pattern at routes/messages.py:80-85).
# -----------------------------------------------------------------
try:
    from metrics import get_metrics_registry as _get_metrics_registry_for_host_wait

    _inflight_host_waits = _get_metrics_registry_for_host_wait().gauge(
        "egg_inflight_host_waits",
        labels={"endpoint": "pipelines.status_wait"},
    )
except Exception:  # pragma: no cover - metrics best-effort
    _inflight_host_waits = None


def _track_host_wait_start() -> None:
    if _inflight_host_waits is not None:
        try:
            _inflight_host_waits.inc()
        except Exception:  # pragma: no cover
            pass


def _track_host_wait_end() -> None:
    if _inflight_host_waits is not None:
        try:
            _inflight_host_waits.dec()
        except Exception:  # pragma: no cover
            pass


# -----------------------------------------------------------------
# Cursor protocol for /status/wait  (issue #1932 TASK-1-2).
#
# Opaque compound cursor "msg:<redis_stream_id>|evt:<sequence>":
#   * ``msg:<id>`` is the message-store tip ID from the prior call.
#     Either half may be empty when the corresponding source has not
#     emitted yet (e.g. ``msg:|evt:5`` = "no message seen, EventBus
#     tip at seq 5").
#   * ``evt:<sequence>`` is the EventBus per-bus monotonic sequence
#     (see ``Event.sequence`` added in TASK-1-1).  The sequence is
#     signed purely so malformed inputs with leading ``-`` are
#     accepted by the regex and handled gracefully by the parser.
#
# The regex is intentionally permissive — unknown halves degrade to
# ``None`` which the route maps to "snap to tip" (``from_tip`` on
# the message bus, ``current_sequence`` on the EventBus) so
# first-call semantics are race-free.
# -----------------------------------------------------------------
_STATUS_WAIT_CURSOR_RE = re.compile(r"^msg:([^|]*)\|evt:(-?\d*)$")

# Slice-or-phase id shape used when reading the parent edge from the
# contract for the restart route's ``base_branch`` derivation (#2439).
# ``Slice.id`` permits either ``slice-<N>`` (canonical) or ``phase-<N>``
# (legacy, pre-#2137) and the contract migration shim only normalises
# the typical case where the input has a top-level ``phases`` key. A
# directly-loaded ``slices`` field with legacy ids is rare but allowed
# by the model — accept either shape here so the gate doesn't false-
# reject a legitimate restart on a long-lived contract.
_SLICE_OR_PHASE_ID_PATTERN = re.compile(r"^(?:slice|phase)-[0-9]+$")

# Event allowlist for ``/status/wait`` (issue #1932 locked in
# refine HITL decision 2).  The route returns early when an event
# matching any of these types is published.  ``DECISION_RESOLVED``
# is intentionally excluded — it is the post-``provide_input``
# event and would cause the host to self-wake on an action it
# initiated.  Agent-lifecycle events are excluded because the host
# does not drive on them.  See
# docs/reference/agent-wait-patterns.md §7.
_STATUS_WAIT_EVENT_TYPES = frozenset(
    {
        "phase.started",
        "phase.completed",
        "decision.created",
        "pipeline.completed",
        "pipeline.failed",
        "pipeline.cancelled",
    }
)

# Message-type allowlist for ``/status/wait`` (same HITL decision).
# Wired to ``message_store.get_messages(wait_for_types=...)`` so a
# message of a non-matching type does NOT unblock the waiter.
_STATUS_WAIT_MESSAGE_TYPES = (
    "OVERSEER_ALERT",
    "CONSENSUS_CONFIRMED",
    "CONSENSUS_NACK",
    "CONSENSUS_RE_REVIEW",
)


def _parse_status_wait_cursor(
    raw: str | None,
) -> tuple[bool, str | None, int | None]:
    """Parse a ``/status/wait`` cursor.

    Returns ``(ok, msg_since_id, event_since_seq)`` where either half
    may be ``None`` (meaning "snap to tip on this source").  ``ok``
    is False only for a syntactically malformed cursor — the route
    returns 400 in that case.  An empty / missing cursor is treated
    as "snap to tip on both sources" (``ok=True, None, None``).
    """
    if raw is None or raw == "":
        return True, None, None
    match = _STATUS_WAIT_CURSOR_RE.match(raw)
    if not match:
        return False, None, None
    msg_part = match.group(1)
    evt_part = match.group(2)
    msg_since_id = msg_part if msg_part else None
    event_since_seq: int | None = None
    if evt_part:
        try:
            event_since_seq = int(evt_part)
        except ValueError:  # pragma: no cover — the regex guarantees digits/-
            event_since_seq = None
    return True, msg_since_id, event_since_seq


def _build_status_wait_cursor(
    msg_tip_id: str | None,
    event_tip_seq: int,
) -> str:
    """Format a cursor for a ``/status/wait`` response.

    Both halves are emitted — the consumer treats empty halves as
    "snap to tip" on the next call, matching ``_parse_status_wait_cursor``.
    """
    msg_part = msg_tip_id or ""
    return f"msg:{msg_part}|evt:{event_tip_seq}"


def _message_store_tip_id(pipeline_id: str) -> str | None:
    """Best-effort read of the message-store tip ID for a pipeline.

    Used to build the initial / terminal cursor when the route
    returns without matching a message.  Returns ``None`` when the
    store has no messages yet — the caller formats this as the
    empty ``msg:`` half of the compound cursor.

    Three distinct conditions all collapse to ``None`` here and
    callers cannot distinguish between them:

    1. **Store import failure** — the message-store module is not
       loadable in this process (test harness without Redis,
       packaging skew). Pre-PR / post-#2464: same behavior.
    2. **Transient ``get_latest_id`` failure** — e.g.,
       :class:`redis.RedisError` from ``XREVRANGE`` on a connection
       blip. ``RedisMessageStore.get_latest_id`` already catches
       this and returns ``None``, so we see "no tip". This conflates
       a transient error with a genuinely empty store; #2464's fix
       at the call site (``_message_store_tip_id() or msg_since_id``
       removal) drops the consumer's cursor on this transient as
       well, which is a small behavioral regression vs. pre-PR
       graceful-degradation behavior. Acceptable in practice
       because transient Redis errors degrade many other paths
       simultaneously, but worth knowing.
    3. **Empty store** — the ``/status/wait`` post-clear case the
       PR is fixing. Returning ``None`` lets the route emit an
       empty ``msg:`` half so the consumer doesn't re-feed the
       dead cursor.
    """
    try:
        store = _get_message_store()()
    except Exception:  # pragma: no cover — store may not be importable
        return None
    try:
        return store.get_latest_id(pipeline_id)
    except Exception:
        return None


def _build_minimal_status_envelope(
    pipeline: Pipeline,
    cursor: str,
) -> dict[str, Any]:
    """Compute the small envelope used on both wait paths.

    Ships ``current_phase`` / ``status`` / ``phase_elapsed_seconds``
    so dashboards can refresh cheaply on a timeout without paying
    for a second round-trip.  ``concurrent.consensus`` is also
    included (R5 mitigation from the refine phase) so the host
    does not miss a BRC state change during a quiet interval.
    """
    phase_key = pipeline.current_phase.value if pipeline.current_phase else ""
    phase_data = pipeline.phases.get(phase_key, None)
    envelope: dict[str, Any] = {
        "current_phase": phase_key,
        "status": pipeline.status.value if pipeline.status else "",
        "cursor": cursor,
    }
    if phase_data is not None:
        started_at = getattr(phase_data, "started_at", None)
        if started_at:
            try:
                if isinstance(started_at, str):
                    started_dt = datetime.fromisoformat(started_at)
                else:
                    started_dt = started_at
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=UTC)
                elapsed = int((datetime.now(UTC) - started_dt).total_seconds())
                envelope["phase_elapsed_seconds"] = max(0, elapsed)
            except ValueError, TypeError, AttributeError:
                pass

    concurrent_data = _get_concurrent_status(pipeline)
    if concurrent_data and "consensus" in concurrent_data:
        envelope["concurrent"] = {"consensus": concurrent_data["consensus"]}
    return envelope


def _check_and_respawn_overseer(
    *,
    spawner: "ContainerSpawner",  # noqa: UP037
    store: StateStore,
    pipeline_id: str,
    pipeline: Pipeline,
    overseer_container_id: str | None,
    overseer_respawn_count: int,
    max_overseer_respawns: int,
    gateway_mode: str,
    pipeline_repos: list | None,
    certs_volume: str | None,
    expected_run_epoch: datetime | None = None,
) -> tuple[str | None, int]:
    """Check overseer container liveness and respawn if it exited mid-pipeline.

    Returns (updated_container_id, updated_respawn_count).

    ``expected_run_epoch`` is the ``pipeline.run_epoch`` captured by the
    caller's ``_run_pipeline`` thread.  When ``advance_phase(force=true)``
    or ``restart_phase`` bumps ``run_epoch``, the old run's poll thread
    can outlive the transition and see its externally-stopped overseer
    as EXITED.  Without this guard the old and new runs would each
    respawn independently, producing parallel respawn chains (#1916).
    """
    if not overseer_container_id or overseer_respawn_count >= max_overseer_respawns:
        return overseer_container_id, overseer_respawn_count

    try:
        info = spawner.backend.get_container_info(overseer_container_id)
        needs_respawn = info.status in (
            ContainerStatus.EXITED,
            ContainerStatus.FAILED,
            ContainerStatus.REMOVED,
        )
        exit_code = info.exit_code
    except ContainerNotFoundError:
        # Container completely deleted from Docker daemon — treat as respawn trigger.
        needs_respawn = True
        exit_code = None
        logger.warning(
            "Overseer container not found in Docker, will check for respawn",
            pipeline_id=pipeline_id,
            container_id=overseer_container_id[:12],
        )
    except Exception as respawn_err:
        logger.warning(
            "Overseer liveness check error",
            pipeline_id=pipeline_id,
            error=str(respawn_err),
        )
        return overseer_container_id, overseer_respawn_count

    if needs_respawn:
        # Capture log tail from the old container before respawning (best-effort).
        log_tail = "unavailable"
        try:
            log_tail = spawner.backend.get_container_logs(overseer_container_id, tail=20)
        except Exception:
            # Container may already be purged — fall back to "unavailable".
            pass

        try:
            pipeline_check = store.load_pipeline(pipeline_id)

            # Skip respawn when this poll thread belongs to a stale
            # _run_pipeline that has been superseded.  Without this guard,
            # the old run and the new run each respawn the overseer on
            # their own counter, producing two parallel respawn chains
            # (#1916).
            if expected_run_epoch is not None:
                current_epoch = pipeline_check.run_epoch or pipeline_check.created_at
                if current_epoch != expected_run_epoch:
                    logger.info(
                        "Skipping overseer respawn — pipeline run_epoch "
                        "changed (force-advance or restart superseded "
                        "this run)",
                        pipeline_id=pipeline_id,
                        container_id=overseer_container_id[:12],
                        expected_epoch=expected_run_epoch.isoformat(),
                        current_epoch=current_epoch.isoformat(),
                    )
                    return overseer_container_id, overseer_respawn_count
            else:
                logger.debug(
                    "Epoch guard skipped — expected_run_epoch not provided",
                    pipeline_id=pipeline_id,
                    container_id=overseer_container_id[:12],
                )

            if pipeline_check.status in (PipelineStatus.RUNNING, PipelineStatus.AWAITING_HUMAN):
                logger.warning(
                    "Overseer exited mid-pipeline, respawning",
                    pipeline_id=pipeline_id,
                    exit_code=exit_code,
                    respawn_attempt=overseer_respawn_count + 1,
                    max_respawns=max_overseer_respawns,
                )
                new_result = spawner.spawn_overseer_container(
                    pipeline_id=pipeline_id,
                    issue_number=pipeline.issue_number,
                    mode=gateway_mode,
                    poll_interval=pipeline.config.overseer_poll_interval_seconds,
                    decision_model=pipeline.config.overseer_decision_maker_model,
                    max_turns=pipeline.config.overseer_max_turns,
                    repos=pipeline_repos if pipeline_repos else None,
                    certs_volume=certs_volume,
                )
                new_container_id = new_result.container_info.container_id
                overseer_respawn_count += 1
                logger.info(
                    "Overseer respawned successfully",
                    pipeline_id=pipeline_id,
                    container_id=new_container_id[:12],
                    respawn_attempt=overseer_respawn_count,
                )

                # Broadcast OVERSEER_ALERT with respawn diagnostics (best-effort).
                try:
                    from message_store import Message, MessageType

                    store_fn = _get_message_store()
                    if store_fn is not None:
                        msg_store = store_fn()
                        msg_store.add_message(
                            Message(
                                pipeline_id=pipeline_id,
                                from_role="orchestrator",
                                to_role="all",
                                message_type=MessageType.OVERSEER_ALERT,
                                subject="overseer_restart: overseer [info]",
                                body=(
                                    f"Overseer container was respawned. "
                                    f"Old container {overseer_container_id[:12]} exited "
                                    f"with code {exit_code}. "
                                    f"New container {new_container_id[:12]} is now running."
                                ),
                                metadata={
                                    "exit_code": exit_code,
                                    "old_container_id": overseer_container_id,
                                    "new_container_id": new_container_id,
                                    "log_tail": log_tail,
                                    "respawn_attempt": overseer_respawn_count,
                                    "max_respawns": max_overseer_respawns,
                                },
                                phase=pipeline_check.current_phase.value,
                            )
                        )
                except Exception as broadcast_err:
                    logger.warning(
                        "Failed to broadcast overseer respawn alert (non-fatal)",
                        pipeline_id=pipeline_id,
                        error=str(broadcast_err),
                    )

                return new_container_id, overseer_respawn_count
        except Exception as respawn_err:
            logger.warning(
                "Overseer respawn failed",
                pipeline_id=pipeline_id,
                error=str(respawn_err),
            )

    return overseer_container_id, overseer_respawn_count


def _send_brc_confirmation_nudge(
    escalation: dict[str, Any],
    pipeline_id: str,
    phase: str | None,
) -> bool:
    """Wake a producer stuck post-ACK with a directed OVERSEER_ALERT (#2079).

    Wired as an escalation callback for HealthMonitor's
    ``brc_confirmation_timeout`` alert.  The deterministic detector in
    ``check_brc_progress`` knows the exact remediation, so we deliver
    it directly to the stuck producer rather than relying on the
    overseer agent's discretion.

    Uses ``OVERSEER_ALERT`` (not ``STATUS`` or ``NUDGE``) because it
    appears in **both** the producer's pre-confirm wait_loop filter
    (``CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT``,
    post-#2531) and post-confirm wait_loop filter
    (``CONSENSUS_CONFIRMED,CONSENSUS_RE_REVIEW,OVERSEER_ALERT``) and has
    no protocol-specific semantics that would conflict with a producer
    nudge — ``CONSENSUS_RE_REVIEW`` is also in both filters but means
    "a peer re-proposed; re-review their artifact," not "you are
    wedged; confirm." ``STATUS`` is in the pre-confirm filter (it
    carries the orchestrator's *Ready to confirm* nudge) but not the
    post-confirm filter, so it wouldn't reach a producer wedged after
    a successful confirm. A wedged producer is in the
    ``fully_acked but not confirmed`` set, which means they are most
    likely blocked on the pre-confirm wait. The subject calls out
    that the alert originated from the orchestrator's deterministic
    detector rather than the overseer agent.

    Returns True when a message was posted, False otherwise (wrong
    alert type, missing fields, message store unavailable, send error).
    """
    if escalation.get("alert_type") != "brc_confirmation_timeout":
        return False

    producer = escalation.get("agent_id")
    if not producer:
        return False

    elapsed = escalation.get("elapsed_seconds")
    # check_brc_progress always populates elapsed_seconds; treat
    # missing or non-positive values as a malformed escalation rather
    # than rendering "have not confirmed in 0s" in the body.
    if elapsed is None or elapsed <= 0:
        return False

    store_fn = _get_message_store()
    if store_fn is None:
        return False

    # _get_message_store already verified the package is importable;
    # Message/MessageType live in the same module so a defensive
    # try/except here would only add per-call import overhead.
    from message_store import Message, MessageType

    body = (
        f"You are PROPOSED and fully ACKed but have not confirmed in "
        f"{elapsed}s. Call `mcp__brc__confirm` now. If it returns "
        "`status='pending_acks'`, read `message` for the guard reason and "
        "wait on the prerequisite events instead: `CONSENSUS_PROPOSE` if a "
        "producer hasn't proposed (`zero_proposal_producers`), "
        "`CONSENSUS_ACK` / `CONSENSUS_RE_REVIEW` if a reviewer's ACK is "
        "stale or unresolved. Then retry confirm."
    )

    try:
        msg_store = store_fn()
        # Bypass the POST /messages/send route on purpose: this is an
        # orchestrator-internal nudge, and we do not want HealthMonitor's
        # MESSAGE_SENT handler (rate-limit + HEARTBEAT tracking) to see it.
        # Future audit/observability subscribers should be aware this path
        # does not emit EventType.MESSAGE_SENT.
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role=producer,
                message_type=MessageType.OVERSEER_ALERT,
                subject="BRC confirmation timeout — call mcp__brc__confirm",
                body=body,
                phase=phase,
                metadata={
                    "alert_type": "brc_confirmation_timeout",
                    "elapsed_seconds": elapsed,
                    "source": "health_monitor",
                },
            )
        )
        logger.info(
            "Sent BRC confirmation-timeout nudge",
            pipeline_id=pipeline_id,
            producer=producer,
            elapsed_seconds=elapsed,
        )
        return True
    except Exception as send_err:
        logger.warning(
            "Failed to send BRC confirmation-timeout nudge (non-fatal)",
            pipeline_id=pipeline_id,
            producer=producer,
            error=str(send_err),
        )
        return False


def _teardown_phase_overseer(
    spawner: "ContainerSpawner",  # noqa: UP037
    container_id: str,
    pipeline_id: str,
    phase_label: str,
    reason: str,
) -> None:
    """Stop the phase-scoped overseer container.

    Caller is responsible for holding ``overseer_lock`` and setting
    ``phase_overseer_active = False`` before this call.
    """
    try:
        spawner.stop_agent_container(
            container_id,
            cleanup_session=True,
            timeout=10,
        )
        logger.info(
            f"Overseer container stopped ({reason})",
            pipeline_id=pipeline_id,
            phase=phase_label,
            container_id=container_id[:12],
        )
    except Exception as overseer_err:
        logger.debug(
            f"Failed to stop overseer container ({reason})",
            pipeline_id=pipeline_id,
            error=str(overseer_err),
        )


# Base directory where the gateway creates per-pipeline worktrees.
# Must match the gateway's WORKTREE_BASE_DIR and docker-compose volume mounts.
WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")

# Sentinel header used in tester gap summaries. Checked in prompt-building
# functions to adapt language when tester findings are present.
TESTER_FINDINGS_HEADER = "### tester findings"


def _ensure_pipeline_work_ref(branch: str | None) -> str | None:
    """Return the actual remote ref for an orchestrator-managed pipeline branch.

    The orchestrator pushes the pipeline tip to ``<branch>/work`` so the
    ``<branch>/`` namespace can hold slice integration branches as
    siblings (``<branch>/slice-N``) without git's ``directory file
    conflict`` rejection — see #2399. A leaf ref at ``<branch>`` and a
    child at ``<branch>/slice-N`` cannot coexist on origin, so the
    pipeline tip is moved one level deeper into the namespace.

    Idempotent and bounded to ``egg/<id>``-shaped branches:

    * ``None`` → ``None`` (prompt-driven; the caller generates a
      ``/work``-shaped branch later).
    * ``egg/<id>`` → ``egg/<id>/work`` (issue / CUSTOM submissions).
    * ``egg/<id>/work`` → unchanged (resubmission, internal callers).
    * non-``egg/`` (passed unchanged) — primarily babysit PR head refs;
      the route-level caller already skips BABYSIT before reaching this
      helper, so the only non-``egg/`` branch that lands here is a
      CUSTOM-mode pipeline pointed at a foreign branch (e.g.
      ``feature/foo``). CUSTOM-with-slices on a non-``egg/`` branch is
      not a guaranteed-safe shape and is intentionally not normalised
      here — the conflict would resurface at the slice push and is
      tracked separately.

    The trailing-``/work`` check is structural rather than a plain
    suffix match (``branch.count("/") >= 2 and branch.rsplit("/", 1)[1]
    == "work"``) so a degenerate input like ``egg/work`` — a single
    segment that *happens* to end in ``/work`` — gets normalised to
    ``egg/work/work`` (siblings ``egg/work/slice-N``) rather than
    treated as already-normalised. Trailing slashes are stripped first
    so ``egg/`` does not collapse to a double-slash ``egg//work``.
    """
    if branch is None:
        return None
    branch = branch.rstrip("/")
    if not branch.startswith("egg/"):
        return branch
    # Structural check: only treat ``egg/<id>/work`` (≥2 slashes, last
    # segment is ``work``) as already-normalised. ``egg/work`` looks
    # like a suffix match but is a single-segment id and still needs the
    # ``/work`` namespace deepening.
    if branch.count("/") >= 2 and branch.rsplit("/", 1)[1] == "work":
        return branch
    return f"{branch}/work"


def _slice_namespace_root(pipeline_branch: str) -> str:
    """Return the slice-integration-branch namespace root for a pipeline branch.

    Slice integration branches live as siblings of the pipeline tip
    under ``egg/<id>/`` (see :func:`_ensure_pipeline_work_ref`). The
    namespace root is the pipeline branch with the trailing ``/work``
    stripped — that's the prefix slice paths (``<root>/slice-N``) are
    built from. For legacy / non-normalised branches that do not end in
    ``/work``, the branch itself is the root.

    The trailing-``/work`` check mirrors the structural check in
    :func:`_ensure_pipeline_work_ref` (≥2 slashes, last segment is
    ``work``) so a degenerate single-segment input like ``egg/work``
    is treated as the root itself rather than collapsing to ``egg``.
    """
    if pipeline_branch.count("/") >= 2 and pipeline_branch.rsplit("/", 1)[1] == "work":
        return pipeline_branch.rsplit("/", 1)[0]
    return pipeline_branch


def _pipeline_identifier(
    issue_number: int | None,
    pipeline_id: str,
    mode: PipelineMode | None = None,
) -> int | str:
    """Derive the pipeline identifier used for namespaced .egg-state filenames.

    Prefers ``issue_number`` when available, falling back to ``pipeline_id``.

    CUSTOM-mode pipelines (#1762) ALWAYS key by ``pipeline_id`` even when
    an ``issue_number`` is supplied — a CUSTOM pipeline sharing an issue
    number with a concurrent ISSUE-mode pipeline must not collide on
    ``.egg-state/drafts/<N>-analysis.md``.

    Mode detection:
      1. If ``mode`` is explicitly :attr:`PipelineMode.CUSTOM`, use
         ``pipeline_id`` unconditionally.
      2. If ``mode`` is None BUT the ``pipeline_id`` matches the
         CUSTOM naming convention (``custom-<hex>`` or
         ``issue-<N>-<qualifier>`` where qualifier is not bare numeric),
         treat it as CUSTOM so callers that didn't thread ``mode`` still
         get the right keying. This is a deliberate belt-and-braces check
         so TASK-2-8 works across every existing call site, not just the
         two paths where ``pipeline`` was convenient to thread through.
      3. Otherwise keep the legacy behaviour (prefer ``issue_number``).

    Heuristic invariants (review suggestion 4, #1762):
      The name-based detection in step 2 relies on these CUSTOM pipeline
      ID patterns — if a new pattern is introduced, it MUST match one of:
        - ``custom-<hex>``        (synthetic ID from run_agent_task)
        - ``pr-<N>``              (PR-targeted CUSTOM, no issue)
        - ``issue-<N>-<qualifier>`` (issue-bound CUSTOM with qualifier)
      Bare ``issue-<N>`` is explicitly reserved for ISSUE-mode pipelines.
      See ``_handle_run_agent_task`` in ``mcp_tools.py`` for ID generation.
    """
    try:
        from models import PipelineMode as _PipelineMode
    except Exception:
        _PipelineMode = None  # type: ignore[assignment]
    is_custom = _PipelineMode is not None and mode is not None and mode == _PipelineMode.CUSTOM
    if not is_custom and pipeline_id and issue_number is not None:
        # Name-based CUSTOM detection: issue-<N>-<non-numeric-qualifier>
        # is a CUSTOM pipeline generated by run_agent_task (matches the
        # handler's `issue-<N>-custom` / `issue-<N>-<qualifier>` IDs).
        # Bare `issue-<N>` stays in the ISSUE-mode bucket.
        expected_issue_prefix = f"issue-{issue_number}"
        if pipeline_id == expected_issue_prefix:
            pass  # Bare issue-N — keep issue-number keying.
        elif pipeline_id.startswith(expected_issue_prefix + "-"):
            # A qualifier is present; treat this as a distinct (CUSTOM
            # or qualifier-differentiated) pipeline so concurrent runs
            # on the same issue do not collide on draft files.
            is_custom = True
    if is_custom:
        return pipeline_id or "unknown"
    if pipeline_id and pipeline_id.startswith(("custom-", "pr-")):
        # Synthetic or PR-targeted CUSTOM IDs never carry a
        # meaningful issue_number — fall through to pipeline_id.
        return pipeline_id
    return issue_number if issue_number is not None else pipeline_id


def _uses_per_role_staging(pipeline: Pipeline) -> bool:
    """Return True when a pipeline uses BABYSIT-style per-role staging branches.

    Broadened from the BABYSIT-only check in #1748 to cover CUSTOM-mode
    pipelines whose ``pr_number`` is set (see #1762 TASK-2-7). The two
    modes share staging-branch derivation, PR-diff-aware orient prompts,
    and ``has_contract=False`` semantics because a CUSTOM pipeline with a
    PR target is effectively BABYSIT under the hood — the only
    user-facing difference is the MCP tool name.
    """
    try:
        from models import PipelineMode as _PipelineMode
    except Exception:
        return False
    mode = getattr(pipeline, "mode", None)
    if mode is None:
        return False
    if mode == _PipelineMode.BABYSIT:
        return True
    if mode == _PipelineMode.CUSTOM and getattr(pipeline, "pr_number", None) is not None:
        return True
    return False


def _brc_history_identifier(pipeline) -> int | str:
    """Return the identifier used to namespace BRC-history artifacts.

    For issue-mode pipelines this mirrors :func:`_pipeline_identifier`
    (favouring the issue number).  For BABYSIT and CUSTOM+PR pipelines
    this returns ``pr-{pr_number}-{short_sha}`` so every one-off BRC
    cycle writes to a distinct history file — letting operators replay
    runs against the same PR without clobbering prior consensus
    transcripts.  Falls back to the generic identifier when either the
    PR number or the captured head SHA is missing.
    """
    try:
        from models import PipelineMode as _PipelineMode
    except Exception:
        _PipelineMode = None  # type: ignore[assignment]

    mode = getattr(pipeline, "mode", None)
    # BABYSIT and CUSTOM+PR both target a specific PR commit; use
    # SHA-based keys to preserve historical transcripts across re-runs
    # on the same PR (subsumption parity — #1762 review suggestion 2).
    if (
        _PipelineMode is not None
        and mode is not None
        and (
            mode == _PipelineMode.BABYSIT
            or (mode == _PipelineMode.CUSTOM and getattr(pipeline, "pr_number", None))
        )
    ):
        pr = getattr(pipeline, "pr_number", None)
        sha = getattr(pipeline, "pr_head_sha", None)
        if pr and isinstance(sha, str) and len(sha) >= 7:
            return f"pr-{pr}-{sha[:7]}"
    return _pipeline_identifier(
        getattr(pipeline, "issue_number", None),
        getattr(pipeline, "id", "") or "",
    )


# Network constants for sandbox container URLs
try:
    from egg_config import (
        ORCHESTRATOR_EXTERNAL_IP,
        ORCHESTRATOR_ISOLATED_IP,
        ORCHESTRATOR_PORT,
    )
except ImportError:
    ORCHESTRATOR_ISOLATED_IP = "172.32.0.3"
    ORCHESTRATOR_EXTERNAL_IP = "172.33.0.3"
    ORCHESTRATOR_PORT = 9849

try:
    from egg_config.validators import validate_checks
except ImportError:

    def validate_checks(checks: list) -> list[dict[str, str]]:  # type: ignore[misc]
        if not isinstance(checks, list):
            return []
        return [
            {"name": str(c["name"]), "command": str(c["command"])}
            for c in checks
            if isinstance(c, dict) and "name" in c and "command" in c
        ]


pipelines_bp = Blueprint("pipelines", __name__, url_prefix="/api/v1/pipelines")


# Runtime detection: use Kubernetes spawner when EGG_RUNTIME=kubernetes
_RUNTIME = os.environ.get("EGG_RUNTIME", "docker")


def _get_spawner():
    """Get the appropriate spawner for the current runtime.

    Returns KubernetesSpawner when EGG_RUNTIME=kubernetes, otherwise
    ContainerSpawner (Docker).
    """
    if _RUNTIME == "kubernetes":
        return get_kubernetes_spawner()
    return get_container_spawner()


# Container statuses that count as "live" for the purposes of the
# orphan guard — a pod whose status maps to one of these is still
# bound to the pipeline and would be orphaned by clearing
# ``containers`` / ``agents`` / ``artifacts``.  Pods in terminal
# phases (``Failed``/``Succeeded`` → ``ContainerStatus.FAILED`` /
# ``EXITED``) have already exited and the reset orphans no work.
# This matters because k8s Jobs default to ``ttlSecondsAfterFinished=600``
# so a Failed pod object survives in the cluster for up to 10 minutes
# after the pod itself has terminated — which is exactly the window
# where ``start_pipeline`` is most commonly called for recovery.
# ``startup_reconciliation.py`` applies the same filter so the two
# label-scoped pod checks agree on what "live" means.
_LIVE_POD_STATUSES: tuple[ContainerStatus, ...] = (
    ContainerStatus.PENDING,
    ContainerStatus.CREATING,
    ContainerStatus.RUNNING,
)


def _count_live_pods_for_pipeline(pipeline_id: str, *, quiet: bool = False) -> int | None:
    """Count live pods labeled to this pipeline (#2420).

    Live = ``ContainerStatus`` in :data:`_LIVE_POD_STATUSES` (Pending /
    Creating / Running). Pods in terminal phases (``Failed`` / ``Succeeded``
    → ``ContainerStatus.FAILED`` / ``EXITED``) are excluded — they have
    already exited and the start_pipeline reset orphans no work tied to
    them.

    Returns the number of live pods, or ``None`` if the label query failed —
    callers must distinguish "verified zero" from "unknown" because the
    start_pipeline reset would orphan any pods we couldn't see.

    ``quiet=True`` suppresses the helper-level warning when the label query
    fails. The guard's ``force=true`` branch passes this flag because it
    emits its own structured audit log on the ``live is None`` path; the
    helper's warning would just duplicate it.
    """
    try:
        spawner = _get_spawner()
        pods = spawner.backend.list_containers(
            labels={LABEL_PIPELINE_ID: pipeline_id},
        )
        return sum(1 for p in pods if p.status in _LIVE_POD_STATUSES)
    except Exception as e:
        if not quiet:
            logger.warning(
                "start_pipeline live-pod check failed",
                pipeline_id=pipeline_id,
                error=str(e),
            )
        return None


def _guard_live_pods_or_force(
    pipeline_id: str,
    force: bool,
    force_reason: str | None,
) -> tuple[Response, int] | None:
    """Refuse a phase reset that would orphan live pods (#2420).

    Returns ``None`` when the reset is safe to proceed (zero live pods, or
    ``force=true``). Returns a 409 ``(response, status)`` when live pods are
    present (or the label query failed) and the caller did not pass
    ``force=true``.
    """
    if force:
        # ``quiet=True`` because the ``live is None`` branch below emits
        # its own structured audit log; the helper-level warning would
        # just duplicate it on the override path.
        live = _count_live_pods_for_pipeline(pipeline_id, quiet=True)
        # Template the audit log so the static message reflects what the
        # override actually did. ``live == 0`` means the override was a
        # no-op — log at ``info`` so it doesn't read like a near-miss.
        if live is None:
            logger.warning(
                "start_pipeline force=true override; live-pod check failed, "
                "phase reset will proceed regardless",
                pipeline_id=pipeline_id,
                live_pod_count=None,
                force_reason=force_reason,
            )
        elif live > 0:
            logger.warning(
                "start_pipeline force=true override; phase reset will proceed "
                "and orphan live pods labeled to the pipeline",
                pipeline_id=pipeline_id,
                live_pod_count=live,
                force_reason=force_reason,
            )
        else:
            logger.info(
                "start_pipeline force=true override applied (no live pods present)",
                pipeline_id=pipeline_id,
                live_pod_count=0,
                force_reason=force_reason,
            )
        return None

    live = _count_live_pods_for_pipeline(pipeline_id)
    if live is None:
        return make_error_response(
            f"Could not verify live pod count for pipeline {pipeline_id}; "
            "the start_pipeline reset would orphan any pods labeled to it. "
            "Cancel them first via cancel_task(cleanup=true) or pass "
            "force=true to override.",
            status_code=409,
            reason="live_pod_check_failed",
        )
    if live > 0:
        return make_error_response(
            f"Pipeline {pipeline_id} has {live} live pod(s); the "
            "start_pipeline reset would orphan them. Cancel them first via "
            "cancel_task(cleanup=true) or pass force=true to override.",
            status_code=409,
            details={"live_pod_count": live},
            reason="live_pods_present",
        )
    return None


from routes import get_repo_path, resolve_worktree_repo_path  # noqa: E402 — shared helpers

try:
    from gateway_client import get_gateway_client
except ImportError:
    from orchestrator.gateway_client import get_gateway_client  # type: ignore

# Import status reporter for real-time updates
try:
    from status_reporter import get_status_reporter, report_pipeline_status
except ImportError:
    # Fallback if status_reporter not available
    def get_status_reporter():  # type: ignore[misc]
        return None

    def report_pipeline_status(pipeline, event_type=None, message=None):  # type: ignore[misc]
        pass


# Import event bus for SSE streaming.
# report_pipeline_status dispatches to StatusReporter handlers, but the
# SSE stream subscribes to the EventBus — a separate system.  We need to
# emit events to both so SSE clients see live updates.
try:
    from events import EventType
    from events import emit_event as _emit_event
except ImportError:
    _emit_event = None  # type: ignore[assignment]

# Map report_pipeline_status event_type strings to EventType enum values
_EVENT_TYPE_MAP: dict[str, EventType] = {}
if _emit_event is not None:
    _EVENT_TYPE_MAP = {
        "phase.started": EventType.PHASE_STARTED,
        "phase.completed": EventType.PHASE_COMPLETED,
        "phase.revision_requested": EventType.PHASE_STARTED,  # re-entering phase
        "pipeline.completed": EventType.PIPELINE_COMPLETED,
        "pipeline.failed": EventType.PIPELINE_FAILED,
        "decision.created": EventType.DECISION_CREATED,
    }


def _emit_pipeline_event(
    pipeline: Pipeline,
    event_type_str: str,
) -> None:
    """Emit a pipeline event to the EventBus for SSE streaming."""
    if _emit_event is None:
        return
    mapped = _EVENT_TYPE_MAP.get(event_type_str)
    if mapped is None:
        return
    _emit_event(
        mapped,
        pipeline.id,
        data={
            "status": pipeline.status.value,
            "phase": pipeline.current_phase.value,
        },
    )


# Import visualization modules for DAG endpoint
try:
    from dag_visualizer import (
        generate_status_report,
        render_compact_status,
        render_pipeline_dag,
        render_progress_bar,
    )

    _DAG_VISUALIZER_AVAILABLE = True
except ImportError:
    _DAG_VISUALIZER_AVAILABLE = False

# Import SSE streaming support
try:
    from sse import create_sse_stream

    _SSE_AVAILABLE = True
except ImportError:
    _SSE_AVAILABLE = False

# Import unified SSE streaming support
try:
    from unified_sse import create_unified_sse_stream

    _UNIFIED_SSE_AVAILABLE = True
except ImportError:
    _UNIFIED_SSE_AVAILABLE = False


def make_error_response(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
    reason: str | None = None,
) -> tuple[Response, int]:
    """Create an error response.

    ``reason`` is a stable, machine-readable enum-like code that disambiguates
    responses sharing the same HTTP status (especially 409, where distinct
    gates would otherwise collapse into one signal). Callers should switch on
    ``reason`` rather than parsing ``message``. See #1939.
    """
    response: dict[str, Any] = {"success": False, "message": message}
    if reason is not None:
        response["reason"] = reason
    if details:
        response["details"] = details
    return jsonify(response), status_code


def make_success_response(
    message: str,
    data: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200


def _resolve_pipeline(pipeline_id: str, base_path: Path) -> tuple[StateStore, Pipeline]:
    """Load a pipeline, resolving the correct repo subdirectory.

    Each repo has its own state store and worktree.  This function
    searches all repos under ``base_path`` to find the pipeline.

    Returns:
        (store, pipeline) tuple

    Raises:
        PipelineNotFoundError: if the pipeline cannot be found anywhere
        InvalidPipelineIdError: if the ID format is invalid
        GitOperationError: if the state-store worktree cannot be loaded
            (e.g. ``git worktree add`` contention).  Callers should
            surface this as 500, not 404 — it is recoverable
            infrastructure failure, not a missing pipeline.
    """
    from state_store import discover_repo_paths

    for repo_path in discover_repo_paths(base_path):
        try:
            store = get_state_store(repo_path)
            pipeline = store.load_pipeline(pipeline_id)
            return store, pipeline
        except PipelineNotFoundError:
            continue
        # NOTE: do NOT broaden this to ``StateStoreError``.  Swallowing
        # ``GitOperationError`` here re-raised every state-store wedge
        # as ``Pipeline not found`` and surfaced to operators as 404,
        # masking a recoverable git contention as a missing pipeline
        # (#2167).  Let infrastructure failures propagate so the route
        # can return 500 with the actual error.

    raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found") from None


def _collect_all_pipelines(base_path: Path) -> list:
    """Collect pipelines from all git repos under base_path.

    Each repo has its own state store and worktree. Pipelines are
    deduplicated by ID in case of overlapping stores.
    """
    from state_store import discover_repo_paths

    seen: set[str] = set()
    pipelines = []

    def _add_from_store(store):
        for pid in store.list_pipelines():
            if pid in seen:
                continue
            try:
                pipelines.append(store.load_pipeline(pid))
                seen.add(pid)
            except StateStoreError:
                continue

    for repo_path in discover_repo_paths(base_path):
        try:
            _add_from_store(get_state_store(repo_path))
        except StateStoreError:
            continue

    return pipelines


@pipelines_bp.route("", methods=["GET"])
def list_pipelines() -> tuple[Response, int]:
    """
    List all pipelines.

    Query params:
        repo_path: Path to repository (optional)
        active_only: Only return active pipelines (default: false)

    Response:
        {
            "success": true,
            "data": {
                "pipelines": [
                    {"id": "issue-123", "status": "running", ...},
                    ...
                ]
            }
        }
    """
    repo_path = get_repo_path()
    active_only = request.args.get("active_only", "false").lower() == "true"

    try:
        all_pipelines = _collect_all_pipelines(repo_path)

        if active_only:
            pipelines = [
                p
                for p in all_pipelines
                if p.status
                not in (
                    PipelineStatus.COMPLETE,
                    PipelineStatus.FAILED,
                    PipelineStatus.CANCELLED,
                )
            ]
        else:
            pipelines = all_pipelines

        # Convert to response format
        pipeline_data = [
            {
                "id": p.id,
                "issue_number": p.issue_number,
                "repo": p.repo,
                "branch": p.branch,
                "status": p.status.value,
                "current_phase": p.current_phase.value,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
            }
            for p in pipelines
        ]

        return make_success_response(
            f"Found {len(pipelines)} pipeline(s)",
            data={"pipelines": pipeline_data},
        )

    except StateStoreError as e:
        logger.error("Failed to list pipelines", error=str(e))
        return make_error_response(f"Failed to list pipelines: {e}", status_code=500)


@pipelines_bp.route("/<pipeline_id>", methods=["GET"])
def get_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Get a pipeline by ID.

    URL params:
        pipeline_id: Pipeline ID (e.g., "issue-123")

    Query params:
        repo_path: Path to repository (optional)

    Response:
        {
            "success": true,
            "data": {
                "pipeline": {...}
            }
        }
    """
    repo_path = get_repo_path()

    try:
        _store, pipeline = _resolve_pipeline(pipeline_id, repo_path)

        return make_success_response(
            "Pipeline retrieved",
            data={"pipeline": pipeline.model_dump(mode="json")},
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except StateValidationError as e:
        logger.error("Pipeline validation failed", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(
            f"Pipeline state is invalid: {e}",
            status_code=500,
        )


@pipelines_bp.route("", methods=["POST"])
@require_lifecycle_secret
def create_pipeline() -> tuple[Response, int]:
    """
    Create a new pipeline.

    Request body:
        {
            "issue_number": 123,
            "repo": "owner/name",
            "branch": "egg/issue-123",
            "config": {...}  // optional
        }

    Response:
        {
            "success": true,
            "message": "Pipeline created",
            "data": {
                "pipeline": {...}
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    network_mode = data.get("network_mode")
    if network_mode is not None and network_mode not in ("public", "private"):
        return make_error_response(
            f"Invalid network_mode: {network_mode!r} (must be 'public' or 'private')"
        )

    issue_number = data.get("issue_number")
    repo = data.get("repo")
    branch = data.get("branch")
    base_branch = data.get("base_branch")
    prompt = data.get("prompt")
    mode = data.get("mode", "issue")
    pr_number = data.get("pr_number")
    analysis = data.get("analysis")
    plan = data.get("plan")
    # CUSTOM-mode parameters (#1762 run_agent_task primitive)
    custom_phase = data.get("phase")
    custom_roles_raw = data.get("roles")
    source_branch = data.get("source_branch")
    if source_branch is not None:
        if not re.match(r"^[a-zA-Z0-9_./-]+$", source_branch) or ".." in source_branch:
            return make_error_response(
                f"Invalid source_branch: {source_branch!r}",
                status_code=400,
            )
    source_artifact_prefix = data.get("source_artifact_prefix")
    if source_artifact_prefix is not None:
        if not re.match(r"^[a-zA-Z0-9_.-]+$", source_artifact_prefix):
            return make_error_response(
                f"Invalid source_artifact_prefix: {source_artifact_prefix!r}",
                status_code=400,
            )

    # Validate mode
    valid_modes = {m.value for m in PipelineMode}
    if mode not in valid_modes:
        return make_error_response(f"Invalid mode: {mode!r} (must be one of {sorted(valid_modes)})")

    # Babysit mode requires pr_number
    if mode == PipelineMode.BABYSIT:
        if not pr_number:
            return make_error_response("Missing pr_number (required for babysit mode)")
        if not isinstance(pr_number, int) or pr_number < 1:
            return make_error_response("pr_number must be a positive integer")

    # CUSTOM mode: validate phase early so the remaining pipeline fits
    # on the rails shared with BABYSIT/ISSUE (#1762 TASK-2-1).
    _CUSTOM_ALLOWED_PHASES = {"refine", "plan", "implement"}
    if mode == PipelineMode.CUSTOM:
        if not custom_phase:
            return make_error_response(
                "Missing phase (required for custom mode)",
                status_code=400,
                details={"reason": "missing_phase"},
            )
        if custom_phase not in _CUSTOM_ALLOWED_PHASES:
            return make_error_response(
                f"Invalid phase: {custom_phase!r} "
                f"(must be one of {sorted(_CUSTOM_ALLOWED_PHASES)})",
                status_code=400,
                details={"reason": "invalid_phase"},
            )
        # If caller passed pr_number, validate it the same way BABYSIT does.
        if pr_number is not None:
            if not isinstance(pr_number, int) or pr_number < 1:
                return make_error_response(
                    "pr_number must be a positive integer",
                    status_code=400,
                    details={"reason": "invalid_pr_number"},
                )

    if not repo:
        return make_error_response("Missing repo")

    # Repo allowlist check — applies to every mode, but we surface a
    # CUSTOM-specific 400 reason when the mode is CUSTOM so callers
    # (run_agent_task) see the structured response. See risk_analyst R9.
    # First a lightweight shell-metacharacter sanity check; then we delegate
    # to the repo_config allowlist which backs repositories.yaml.
    if not re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$", repo):
        return make_error_response(
            f"Invalid repo format: {repo!r} (expected owner/name)",
            status_code=400,
            details={"reason": "repo_not_allowed"},
        )
    if mode == PipelineMode.CUSTOM:
        try:
            try:
                from config.repo_config import is_readable_repo as _is_readable_repo
                from config.repo_config import is_writable_repo as _is_writable_repo
            except ImportError:
                from repo_config import (
                    is_readable_repo as _is_readable_repo,  # type: ignore[no-redef]
                )
                from repo_config import (
                    is_writable_repo as _is_writable_repo,  # type: ignore[no-redef]
                )
            _allowed = False
            try:
                _allowed = bool(_is_writable_repo(repo) or _is_readable_repo(repo))
            except Exception:
                # Best-effort: if the allowlist helper raises (e.g. no
                # repositories.yaml at all), fall through to the
                # pre-existing behaviour of letting creation proceed.
                _allowed = True
            if not _allowed:
                return make_error_response(
                    f"Repository {repo!r} is not in the allowlist (repositories.yaml).",
                    status_code=400,
                    details={"reason": "repo_not_allowed", "repo": repo},
                )
        except Exception:
            # If the allowlist machinery itself blew up, do not surface a
            # 500 to the caller — the existing gateway-side check will
            # still catch unauthorised writes.
            logger.warning(
                "Repo allowlist check failed — falling through to gateway",
                repo=repo,
                exc_info=True,
            )

    # PR pre-flight — applied uniformly to BABYSIT and to CUSTOM pipelines
    # that supply a ``pr_number`` (#1762 TASK-2-6). Refuses merged/closed/
    # fork PRs and PRs with no diff before any container spawn. When gh is
    # unavailable the helper returns {} and we proceed — downstream agents
    # will surface the error organically.
    _needs_pr_preflight = (mode == PipelineMode.BABYSIT) or (
        mode == PipelineMode.CUSTOM and pr_number is not None
    )
    babysit_pr_state: dict[str, Any] | None = None
    if _needs_pr_preflight:
        babysit_pr_state = _fetch_pr_state(pr_number, repo=repo)
        if babysit_pr_state:
            pr_state = babysit_pr_state.get("state")
            if pr_state == "MERGED":
                return make_error_response(
                    f"PR #{pr_number} is already merged — cannot run against merged PRs.",
                    status_code=409,
                    details={"reason": "pr_merged", "pr_number": pr_number},
                )
            if pr_state == "CLOSED":
                return make_error_response(
                    f"PR #{pr_number} is closed — reopen it before running.",
                    status_code=409,
                    details={"reason": "pr_closed", "pr_number": pr_number},
                )
            if babysit_pr_state.get("is_fork"):
                head_repo = babysit_pr_state.get("head_repository_name_with_owner") or "fork"
                return make_error_response(
                    f"PR #{pr_number} is from a fork ({head_repo}). Only "
                    "same-repo PRs are supported because staging branches must "
                    "be pushable through the gateway.",
                    status_code=400,
                    details={"reason": "pr_from_fork", "pr_number": pr_number},
                )
            if not babysit_pr_state.get("changed_files"):
                return make_error_response(
                    f"PR #{pr_number} has no changed files — nothing to review.",
                    status_code=409,
                    details={"reason": "pr_empty_diff", "pr_number": pr_number},
                )
        # Auto-populate branch from PR head and base_branch from PR base when
        # the caller did not pass them explicitly.  The agents still need a
        # working branch to rebase against and to push staging branches from.
        if babysit_pr_state:
            if not branch and babysit_pr_state.get("head_ref"):
                branch = babysit_pr_state["head_ref"]
            if not base_branch and babysit_pr_state.get("base_ref"):
                base_branch = babysit_pr_state["base_ref"]

    # Validate branch and base_branch — reject values that could be
    # interpreted as git flags (e.g. "--upload-pack=...") or contain
    # path-traversal sequences.  Same regex used for source_branch above.
    for _ref_name, _ref_val in [("branch", branch), ("base_branch", base_branch)]:
        if _ref_val is not None:
            if not re.match(r"^[a-zA-Z0-9_./-]+$", _ref_val) or ".." in _ref_val:
                return make_error_response(
                    f"Invalid {_ref_name}: {_ref_val!r}",
                    status_code=400,
                )

    # Issue-driven or explicitly-named pipelines require a branch;
    # prompt-driven ones do not.
    pipeline_id = data.get("pipeline_id")
    if not pipeline_id and mode == PipelineMode.BABYSIT:
        pipeline_id = f"pr-{pr_number}"

    # CUSTOM mode: auto-generate a branch (``egg/custom-<pipeline_id>``) when
    # the caller did not supply one AND there is no PR to take the head
    # branch from. Every CUSTOM pipeline has a branch so producers always
    # have somewhere to push drafts (#1762 decision-7).
    if mode == PipelineMode.CUSTOM and not branch and pr_number is None:
        # pipeline_id may still be None at this point — fall back to a
        # synthetic identifier so the branch name is always valid.
        if not pipeline_id:
            pipeline_id = f"custom-{os.urandom(4).hex()}"
        # Avoid doubling the ``custom-`` prefix when pipeline_id already
        # starts with ``custom-`` (the synthetic-ID case above OR a
        # caller-supplied id like ``custom-foo``).
        if pipeline_id.startswith("custom-"):
            branch = f"egg/{pipeline_id}"
        else:
            branch = f"egg/custom-{pipeline_id}"

    if (
        (issue_number or pipeline_id)
        and not branch
        and mode not in (PipelineMode.BABYSIT, PipelineMode.CUSTOM)
    ):
        return make_error_response("Missing branch")

    # #2399 — push the pipeline tip to ``<branch>/work`` so slice
    # integration branches at ``<branch>/slice-N`` can coexist as
    # siblings under the same namespace (git rejects a leaf ref and
    # children of that ref's path with ``directory file conflict``).
    # Skipped for BABYSIT (the branch is an existing PR head we don't
    # own) and for non-``egg/`` branches.
    if mode != PipelineMode.BABYSIT:
        branch = _ensure_pipeline_work_ref(branch)

    # Wait for the gateway to be ready before any gateway-dependent work.
    # On fresh deploys / pod restarts the orchestrator can accept requests
    # while the gateway HTTP listener is still coming up; without this gate
    # the first submission proceeds, hits the gateway during pipeline-level
    # worktree creation or per-agent fan-out, and surfaces as a cascade of
    # generic per-agent ConnectionRefused / "Remote end closed connection"
    # errors that operators have to reverse-engineer.  See #1851.
    try:
        _ready_timeout = int(os.environ.get("EGG_GATEWAY_READY_TIMEOUT_SECONDS", "60"))
    except ValueError:
        _ready_timeout = 60
    _ready_timeout = max(0, _ready_timeout)
    if _ready_timeout > 0:
        _gw_ready = get_gateway_client()
        if not _gw_ready.wait_for_healthy(timeout_seconds=_ready_timeout):
            _last = _gw_ready.check_health()
            _resp, _status = make_error_response(
                f"Gateway not ready after {_ready_timeout}s "
                f"(status={_last.status}): {_last.error or 'unhealthy'}. "
                "Retry once the gateway has finished starting up.",
                status_code=503,
                details={
                    "reason": "gateway_not_ready",
                    "gateway_status": _last.status,
                    "gateway_error": _last.error,
                    "timeout_seconds": _ready_timeout,
                },
            )
            _resp.headers["Retry-After"] = str(_ready_timeout)
            return _resp, _status

    repo_path = get_repo_path()

    # Check that the target branch does not already exist on the remote.
    # This catches conflicts early (before spawning agents).  However,
    # allow branch reuse when the pipeline is in a terminal state
    # (CANCELLED/FAILED/COMPLETE) or doesn't exist at all — this lets
    # callers resubmit against the same branch after a prior run ended.
    if branch:
        try:
            gw = get_gateway_client()
            if gw.ls_remote_branch(
                pipeline_id=pipeline_id or f"branch-check-{uuid4().hex[:8]}",
                repo_path=str(repo_path),
                ref=f"refs/heads/{branch}",
            ):
                # Branch exists — only block if there is an active pipeline
                _branch_store = get_state_store(repo_path)
                _has_active_pipeline = False
                # When pipeline_id is None (auto-generated later), we skip
                # the existence check — we can't look up a pipeline that
                # hasn't been assigned an ID yet.  This is acceptable because
                # auto-generated IDs are unique and won't collide.
                if pipeline_id and _branch_store.pipeline_exists(pipeline_id):
                    try:
                        _existing = _branch_store.load_pipeline(pipeline_id)
                        _terminal = {
                            PipelineStatus.CANCELLED,
                            PipelineStatus.FAILED,
                            PipelineStatus.COMPLETE,
                        }
                        _has_active_pipeline = _existing.status not in _terminal
                    except Exception:
                        # If we can't load the pipeline, treat as no active pipeline
                        pass

                if _has_active_pipeline:
                    hint = ""
                    if pipeline_id:
                        hint = (
                            f" Use a qualifier to create a separate pipeline"
                            f" (e.g. '{pipeline_id}-<qualifier>')."
                        )
                    return make_error_response(
                        f"Branch '{branch}' already exists on remote.{hint}",
                        status_code=409,
                        details={"reason": "branch_exists", "branch": branch},
                    )
                else:
                    # No active pipeline, but the branch may carry commits
                    # from a prior failed/cancelled run.  Inheriting that
                    # state was the precondition for #2222 (stale
                    # pipeline-branch tip + advanced main → contaminated
                    # PR via the push-reconcile fallback).  Compare the
                    # branch tip to the configured base; only a fresh
                    # branch (tip == base) is safe to silently reuse.
                    #
                    # Resolve the default branch via ``_detect_default_branch``
                    # rather than hardcoding ``"main"`` so repos whose default
                    # is ``master`` / ``develop`` still get the stale-branch
                    # check (otherwise the ``origin/main`` lookup returns
                    # ``None``, the guard falls through, and the precondition
                    # check is silently disabled).
                    _resolved_base = base_branch or _detect_default_branch(repo_path)
                    _branch_sha = gw.get_remote_branch_sha(
                        pipeline_id=pipeline_id or f"branch-check-{uuid4().hex[:8]}",
                        repo_path=str(repo_path),
                        ref=f"refs/heads/{branch}",
                    )
                    _base_sha = gw.get_remote_branch_sha(
                        pipeline_id=pipeline_id or f"branch-check-{uuid4().hex[:8]}",
                        repo_path=str(repo_path),
                        ref=f"refs/heads/{_resolved_base}",
                    )
                    # When either lookup returns ``None`` the stale-branch
                    # check is bypassed.  ``get_remote_branch_sha`` swallows
                    # transient gateway errors and returns ``None`` (same
                    # value it returns when the ref legitimately doesn't
                    # exist), so we surface a warning here to make the
                    # silent skip visible to operators investigating a
                    # post-merge contamination — rather than letting the
                    # precondition fix vanish behind a transient hiccup.
                    if _branch_sha is None or _base_sha is None:
                        logger.warning(
                            "Stale-branch check skipped: SHA lookup returned None "
                            "(transient gateway error or ref missing — see #2222)",
                            branch=branch,
                            base_branch=_resolved_base,
                            branch_sha=_branch_sha,
                            base_sha=_base_sha,
                        )
                    if _branch_sha and _base_sha and _branch_sha != _base_sha:
                        logger.warning(
                            "Branch exists with prior-pipeline commits — refusing reuse (#2222)",
                            branch=branch,
                            base_branch=_resolved_base,
                            branch_sha=_branch_sha,
                            base_sha=_base_sha,
                        )
                        cleanup_hint = (
                            f" Run cancel_task(task_id='{pipeline_id}', cleanup=true) "
                            "to delete the stale branch and pipeline state, then "
                            "resubmit."
                            if pipeline_id
                            else (
                                " Delete the stale branch and any associated "
                                "pipeline state, then resubmit."
                            )
                        )
                        return make_error_response(
                            f"Branch '{branch}' exists with commits from a prior "
                            f"pipeline run (tip {_branch_sha[:8]} != "
                            f"origin/{_resolved_base} {_base_sha[:8]}). Starting a "
                            "new pipeline on top of it would inherit that history.",
                            status_code=409,
                            details={
                                "reason": "stale_branch",
                                "branch": branch,
                                "branch_sha": _branch_sha,
                                "base_sha": _base_sha,
                                "hint": cleanup_hint.strip(),
                            },
                        )
                    logger.info(
                        "Branch exists but no active pipeline — allowing reuse",
                        branch=branch,
                        pipeline_id=pipeline_id,
                        branch_sha=_branch_sha,
                        base_sha=_base_sha,
                    )
        except Exception as e:
            # Non-fatal — if we can't reach the gateway, let creation proceed
            # and fail later on push.
            logger.warning(
                "Branch existence check failed, proceeding anyway",
                branch=branch,
                error=str(e),
            )

    # Validate config before creating the pipeline so invalid config
    # returns a 400 instead of bubbling up as a 500.
    config = data.get("config")
    if config is not None:
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError as e:
                return make_error_response(f"Invalid config JSON: {e}")
        try:
            from models import PipelineConfig
            from pydantic import ValidationError

            PipelineConfig.model_validate(config)
        except ValidationError as e:
            errors = [
                {"field": ".".join(str(loc) for loc in err["loc"]), "message": err["msg"]}
                for err in e.errors()
            ]
            return make_error_response(
                f"Invalid pipeline config: {errors}",
                details={"validation_errors": errors},
            )

    # Validate analysis/plan size before creating the pipeline.
    _MAX_DRAFT_LEN = 200_000
    for field_name in ("analysis", "plan"):
        value = data.get(field_name)
        if isinstance(value, str) and len(value) > _MAX_DRAFT_LEN:
            return make_error_response(
                f"{field_name} exceeds maximum length ({len(value)} > {_MAX_DRAFT_LEN})"
            )

    # Babysit-pr pipelines run a one-off implement-phase BRC cycle against a
    # PR diff — no upstream SDLC contract exists, so reviewer_contract is
    # filtered out of the active roster. CUSTOM pipelines with a PR target
    # follow the same semantics (#1762 TASK-2-7). CUSTOM without a PR
    # computes has_contract from whether analysis/plan/issue_contract is
    # available (TASK-2-2).
    if mode == PipelineMode.BABYSIT:
        has_contract = False
    elif mode == PipelineMode.CUSTOM:
        # CUSTOM+PR: subsume BABYSIT — no upstream contract.
        if pr_number is not None:
            has_contract = False
        else:
            # CUSTOM without PR: has_contract is True when the caller
            # passed inline analysis / plan OR when an ISSUE-mode contract
            # file already exists for this issue number.
            _has_artifact = bool(analysis) or bool(plan)
            _has_issue_contract = False
            if not _has_artifact and issue_number:
                try:
                    _contract_path = (
                        repo_path / ".egg-state" / "contracts" / f"issue-{issue_number}.json"
                    )
                    _has_issue_contract = _contract_path.exists()
                except Exception:
                    _has_issue_contract = False
            has_contract = _has_artifact or _has_issue_contract
    else:
        has_contract = True
    pr_head_sha: str | None = None
    if _needs_pr_preflight and babysit_pr_state:
        _candidate_sha = babysit_pr_state.get("head_sha")
        if isinstance(_candidate_sha, str) and _candidate_sha:
            pr_head_sha = _candidate_sha

    # Resolve the roster override for CUSTOM mode (and for BABYSIT, whose
    # subsumption path persists the same list for runtime consistency —
    # #1762 TASK-4-1). For ISSUE-mode pipelines active_roles stays None so
    # the executor uses the full phase-default roster.
    active_roles_to_persist: list[str] | None = None
    if mode == PipelineMode.CUSTOM:
        # Only validate when the caller supplied a roles field; None /
        # missing means "use the default roster for the phase" and the
        # helper expands it on our behalf.
        try:
            from egg_contracts.agent_roles import (
                validate_roles_for_custom_phase as _validate_custom_roles,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to import validate_roles_for_custom_phase", error=str(exc))
            return make_error_response(
                "Internal error validating roles",
                status_code=500,
            )
        _resolved, _err = _validate_custom_roles(
            phase=custom_phase,
            requested_roles=(
                list(custom_roles_raw) if isinstance(custom_roles_raw, list) else None
            ),
            repo=repo,
            has_contract=has_contract,
        )
        if _err is not None or _resolved is None:
            return make_error_response(
                f"Invalid roles for phase {custom_phase!r}: {_err}",
                status_code=400,
                details={"reason": _err or "invalid_roles"},
            )
        active_roles_to_persist = [r.value for r in _resolved]
    elif mode == PipelineMode.BABYSIT:
        # TASK-4-1: populate active_roles on BABYSIT pipelines so the
        # CUSTOM+PR code path and BABYSIT share the same runtime plumbing.
        try:
            from egg_contracts.agent_roles import get_roles_for_phase as _get_roles

            _babysit_roles = _get_roles(
                "implement", include_reviewers=True, repo=repo, has_contract=False
            )
            active_roles_to_persist = [r.value for r in _babysit_roles]
        except Exception:
            # Defensive: if role resolution fails we leave active_roles as
            # None and fall back to the executor's default path.
            active_roles_to_persist = None

    try:
        store = get_state_store(repo_path)
        pipeline = store.create_pipeline(
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            base_branch=base_branch,
            config=config,
            prompt=prompt,
            network_mode=network_mode,
            pipeline_id=pipeline_id,
            mode=PipelineMode(mode) if mode != "issue" else None,
            pr_number=pr_number,
            analysis=analysis,
            plan=plan,
            source_branch=source_branch,
            source_artifact_prefix=source_artifact_prefix,
            has_contract=has_contract,
            pr_head_sha=pr_head_sha,
            active_roles=active_roles_to_persist,
            custom_phase=custom_phase if mode == PipelineMode.CUSTOM else None,
        )

        # Contract creation is deferred to _run_pipeline so it writes
        # into the per-pipeline worktree instead of the main repo.

        # When state_store replaces a terminal pipeline with the same id
        # (state_store.create_pipeline:850), the in-memory consensus
        # tracker / message-store entries for the prior run survive. Same
        # for Redis-backed message-store entries across orchestrator
        # restarts. Clear here so the new run starts with empty consensus
        # state regardless of how the prior run ended (#2053).
        #
        # This is the *primary* eviction site for auto-FAILED prior runs,
        # not just a defensive backstop: paths like restart_agent spawn
        # failure and _handle_pr_creation_failure call
        # store.update_pipeline / store.save_pipeline directly (bypassing
        # PATCH), so the PATCH-site clear never fires for them. Without
        # this POST-site clear, those auto-FAILED pipelines would leak
        # consensus + message-store state into the next run that reuses
        # the id.
        _clear_pipeline_runtime_state(pipeline.id, reason="pipeline_create")

        logger.info(
            "Pipeline created",
            pipeline_id=pipeline.id,
            issue_number=issue_number,
        )

        return make_success_response(
            "Pipeline created",
            data={"pipeline": pipeline.model_dump(mode="json")},
        )

    except StateStoreError as e:
        if "already exists" in str(e):
            # Include existing pipeline details so callers can decide
            # whether to cancel+resubmit or resume monitoring.
            details: dict[str, Any] = {}
            try:
                # Derive pipeline ID using the same logic as state_store
                pid = pipeline_id or (f"issue-{issue_number}" if issue_number else None)
                if pid:
                    existing = store.load_pipeline(pid)
                    details = {
                        "existing_pipeline_id": existing.id,
                        "existing_status": existing.status.value,
                        "existing_phase": existing.current_phase.value,
                    }
            except Exception:
                pass  # Best-effort enrichment
            return make_error_response(str(e), status_code=409, details=details)
        logger.error("Failed to create pipeline", error=str(e))
        return make_error_response(f"Failed to create pipeline: {e}", status_code=500)
    except Exception as e:
        # Catch non-StateStoreError exceptions (e.g., ValidationError,
        # OSError) that would otherwise produce a generic 500 from the
        # Flask error handler with no detail (#1396).
        logger.error(
            "Unexpected error creating pipeline",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        msg = f"{type(e).__name__}: {e}"
        return make_error_response(
            f"Failed to create pipeline: {msg[:500]}",
            status_code=500,
        )


def _clear_pipeline_runtime_state(pipeline_id: str, *, reason: str) -> None:
    """Evict per-pipeline runtime state that is keyed by pipeline_id alone.

    The peer-consensus tracker, the legacy consensus evaluator, and the
    inter-agent message store are all keyed by pipeline_id. Without a
    matching ``run_epoch`` namespace, a fresh pipeline that reuses an id
    from a prior terminal run (same branch, e.g. ``issue-1965``) will
    inherit the prior run's CONFIRMED consensus and message history. The
    leak surfaces in the ``/status/wait`` route's Path-B envelope, which
    would report ``concurrent.consensus.is_complete: true`` for a
    pipeline that has not spawned any agents yet (#2053).

    Called when a pipeline transitions to a terminal status, when its
    state file is deleted, and immediately after a fresh pipeline is
    created (covers paths that bypass PATCH/DELETE — auto-FAILED, and
    Redis-backed message-store entries that survived an orchestrator
    restart between cancel and resubmit).
    """
    try:
        try:
            from peer_consensus import remove_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (  # type: ignore[no-redef]
                remove_peer_consensus_tracker,
            )
        remove_peer_consensus_tracker(pipeline_id)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(
            "Failed to clear peer consensus tracker",
            pipeline_id=pipeline_id,
            reason=reason,
            error=str(e),
        )

    try:
        try:
            from consensus import get_consensus_evaluator
        except ImportError:
            from ..consensus import get_consensus_evaluator  # type: ignore[no-redef]
        get_consensus_evaluator().clear(pipeline_id)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(
            "Failed to clear legacy consensus state",
            pipeline_id=pipeline_id,
            reason=reason,
            error=str(e),
        )

    # Reconstruct-from-messages would otherwise replay the prior run's
    # CONSENSUS_* messages and rebuild a CONFIRMED tracker, defeating the
    # tracker eviction above.
    try:
        try:
            from message_store import get_message_store
        except ImportError:
            from ..message_store import get_message_store  # type: ignore[no-redef]
        get_message_store().clear(pipeline_id)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(
            "Failed to clear message store",
            pipeline_id=pipeline_id,
            reason=reason,
            error=str(e),
        )


def _mark_pipeline_records_terminated(
    store: StateStore,
    pipeline_id: str,
) -> Pipeline:
    """Mark all running containers and agents as stopped after pipeline termination.

    Called when a pipeline transitions to a terminal state (cancelled or failed).
    After Docker containers are force-removed, the pipeline state still shows
    them as "running". This reloads the latest state from the store (to avoid
    overwriting updates made between the status change and container
    cleanup), marks running records as stopped, and saves.

    Returns the updated pipeline so the caller can use it in the response.
    """
    pipeline = store.load_pipeline(pipeline_id)
    now = datetime.now(UTC)
    changed = False

    for phase_exec in pipeline.phases.values():
        for container in phase_exec.containers:
            if container.status in (
                ContainerStatus.PENDING,
                ContainerStatus.CREATING,
                ContainerStatus.RUNNING,
            ):
                container.status = ContainerStatus.REMOVED
                container.exited_at = now
                changed = True

        for agent in phase_exec.agents:
            if agent.status in (
                AgentExecutionStatus.PENDING,
                AgentExecutionStatus.RUNNING,
            ):
                agent.status = AgentExecutionStatus.FAILED
                agent.completed_at = now
                agent.error = f"Pipeline {pipeline.status.value}"
                changed = True

    if changed:
        store.save_pipeline(pipeline)
        logger.info(
            "Synced pipeline state after termination",
            pipeline_id=pipeline_id,
        )

    return pipeline


@pipelines_bp.route("/<pipeline_id>", methods=["PATCH"])
@require_lifecycle_secret
def update_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Update a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "status": "running",
            "current_phase": "plan",
            ...
        }

    Response:
        {
            "success": true,
            "data": {
                "pipeline": {...}
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    repo_path = get_repo_path()

    try:
        store, _pipeline = _resolve_pipeline(pipeline_id, repo_path)
        pipeline = store.update_pipeline(pipeline_id, data)

        # If pipeline is being cancelled or failed, clean up containers
        # and cancel any pending decisions so wait_for_decision() unblocks.
        if pipeline.status in (PipelineStatus.CANCELLED, PipelineStatus.FAILED):
            try:
                dq = get_decision_queue(pipeline_id, repo_path)
                pending = dq.get_pending_decisions()
                for decision in pending:
                    dq.cancel_decision(decision.id)
                if pending:
                    logger.info(
                        "Cancelled pending decisions after pipeline status change",
                        pipeline_id=pipeline_id,
                        decisions_cancelled=len(pending),
                    )
            except Exception as e:
                logger.warning(
                    "Failed to cancel pending decisions",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

            # Sync pipeline state: reload latest state (agents may have
            # written updates between status change and container cleanup),
            # mark all running records as stopped, and re-save.
            try:
                pipeline = _mark_pipeline_records_terminated(store, pipeline_id)
            except Exception as e:
                logger.warning(
                    "Failed to sync pipeline state after termination",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
                # Reload pipeline so the response reflects current state
                # rather than the stale pre-cleanup object.
                try:
                    pipeline = store.load_pipeline(pipeline_id)
                except Exception:
                    pass  # Use stale pipeline if reload also fails

            # Move container/worktree cleanup to a background daemon thread
            # so the PATCH response returns immediately.  The DELETE handler
            # already re-runs cleanup_pipeline() as a safety net, so it will
            # catch anything the background thread hasn't finished.
            #
            # Compute the salvage mode + base branch up front (in the
            # request thread, where ``pipeline`` is still in scope) so the
            # background thread can pass them to ``cleanup_pipeline``
            # without re-loading state. Using the wrong mode here would
            # mismatch the policy the rest of the pipeline ran under and
            # the launcher-auth push could be rejected — see #2429
            # review.
            _bg_salvage_mode, _ = _compute_gateway_mode(pipeline)
            _bg_salvage_base_branch = pipeline.base_branch

            def _background_cleanup(pid: str, status_value: str) -> None:
                try:
                    spawner = _get_spawner()
                    # Preserve worktrees for CANCELLED pipelines so that
                    # restart_phase/restart_agent can resume with local
                    # committed work intact (see #1725).
                    removed = spawner.cleanup_pipeline(
                        pid,
                        force=True,
                        preserve_worktrees=(status_value == "cancelled"),
                        salvage_mode=_bg_salvage_mode,
                        salvage_base_branch=_bg_salvage_base_branch,
                    )
                    if removed > 0:
                        logger.info(
                            "Cleaned up pipeline containers after status change",
                            pipeline_id=pid,
                            status=status_value,
                            containers_removed=removed,
                        )
                except (DockerClientError, DockerException, KubernetesClientError) as e:
                    logger.warning(
                        "Failed to clean up pipeline containers",
                        pipeline_id=pid,
                        error=str(e),
                    )
                except Exception as e:
                    logger.error(
                        "Unexpected error during pipeline container cleanup",
                        pipeline_id=pid,
                        error=str(e),
                        exc_info=True,
                    )

            cleanup_thread = threading.Thread(
                target=_background_cleanup,
                args=(pipeline_id, pipeline.status.value),
                daemon=True,
                name=f"cleanup-{pipeline_id}",
            )
            cleanup_thread.start()

            # Evict per-pipeline runtime state (consensus tracker, legacy
            # consensus evaluator, message store) so a future pipeline
            # that reuses this id (same branch) does not inherit this
            # run's CONFIRMED consensus or message history (#2053).
            _clear_pipeline_runtime_state(pipeline_id, reason=f"pipeline_{pipeline.status.value}")

        logger.info("Pipeline updated", pipeline_id=pipeline_id)

        response_data = {"pipeline": pipeline.model_dump(mode="json")}
        if pipeline.status in (PipelineStatus.CANCELLED, PipelineStatus.FAILED):
            response_data["cleanup_pending"] = True

        return make_success_response(
            "Pipeline updated",
            data=response_data,
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except StateValidationError as e:
        return make_error_response(
            f"Invalid update: {e}",
            status_code=400,
        )


def _compute_gateway_mode(
    pipeline: Pipeline,
) -> tuple[Literal["public", "private"], str | None]:
    """Compute gateway session mode from pipeline config and repo visibility.

    Uses the explicit ``network_mode`` if set, otherwise auto-detects from
    repository visibility via the gateway.  Defaults to ``"public"``.

    Returns:
        A ``(mode, visibility)`` tuple.  ``visibility`` is ``None`` when
        ``network_mode`` is explicit, the pipeline has no repo, or the
        gateway query failed.
    """
    if pipeline.network_mode:
        return pipeline.network_mode, None
    if pipeline.repo:
        vis = get_gateway_client().get_repo_visibility(pipeline.repo)
        if vis in ("private", "internal"):
            return "private", vis
        return "public", vis
    return "public", None


def _cleanup_remote_branches(
    pipeline_id: str,
    pipeline: Pipeline,
    repo_path: Path,
) -> None:
    """Best-effort cleanup of remote branches for a pipeline.

    Deletes the pipeline's shared branch (``pipeline.branch``, typically
    ``egg/{pipeline_id}/work`` since #2399) and every per-container
    worktree branch (``egg/{container_id}/work``).  Slice integration
    branches at ``egg/{pipeline_id}/slice-N`` are siblings of the
    pipeline tip and are NOT deleted here — see follow-up tracking on
    #2399 for full namespace cleanup.  Failures are logged as warnings
    and do not block pipeline deletion.
    """
    branches: set[str] = set()
    if pipeline.branch:
        branches.add(pipeline.branch)
    for phase_exec in pipeline.phases.values():
        for container in phase_exec.containers:
            branches.add(f"egg/{container.container_id}/work")

    if not branches:
        return

    gateway_client = get_gateway_client()
    repo_path_str = str(repo_path)
    mode, _vis = _compute_gateway_mode(pipeline)

    deleted = 0
    for branch in sorted(branches):
        result = gateway_client.delete_remote_branch(pipeline_id, repo_path_str, branch, mode=mode)
        # ``already_deleted`` means the desired state (branch absent on
        # remote) is satisfied — count it as success rather than churning a
        # warning every time a pipeline is cleaned up before any branch was
        # ever pushed.
        if result or result.category == "already_deleted":
            deleted += 1
        else:
            logger.warning(
                "Remote branch deletion failed during pipeline cleanup",
                pipeline_id=pipeline_id,
                branch=branch,
                category=result.category,
                detail=result.detail,
            )

    if deleted:
        logger.info(
            "Cleaned up remote branches",
            pipeline_id=pipeline_id,
            branches_deleted=deleted,
            branches_total=len(branches),
        )


@pipelines_bp.route("/<pipeline_id>", methods=["DELETE"])
@require_lifecycle_secret
def delete_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Delete a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Pipeline deleted"
        }
    """
    repo_path = get_repo_path()

    try:
        store, _pipeline = _resolve_pipeline(pipeline_id, repo_path)

        # Clean up any running containers for this pipeline
        try:
            spawner = _get_spawner()
            # Pass the running pipeline's gateway mode + base branch so the
            # auto-salvage hook in cleanup_pipeline pushes recovery refs
            # under the same policy the pipeline ran under (#2429 review).
            _delete_salvage_mode, _ = _compute_gateway_mode(_pipeline)
            removed = spawner.cleanup_pipeline(
                pipeline_id,
                force=True,
                salvage_mode=_delete_salvage_mode,
                salvage_base_branch=_pipeline.base_branch,
            )
            if removed > 0:
                logger.info(
                    "Cleaned up pipeline containers",
                    pipeline_id=pipeline_id,
                    containers_removed=removed,
                )
        except (DockerClientError, DockerException, KubernetesClientError) as e:
            logger.warning(
                "Failed to clean up pipeline containers",
                pipeline_id=pipeline_id,
                error=str(e),
            )
        except Exception as e:
            logger.error(
                "Unexpected error during pipeline container cleanup",
                pipeline_id=pipeline_id,
                error=str(e),
                exc_info=True,
            )

        # Clean up remote branches (best-effort)
        try:
            _cleanup_remote_branches(pipeline_id, _pipeline, repo_path)
        except Exception as e:
            logger.warning(
                "Failed to clean up remote branches",
                pipeline_id=pipeline_id,
                error=str(e),
            )

        # Clean up the message store stream/counters AND the in-memory
        # consensus tracker / legacy evaluator so a fresh pipeline that
        # later reuses this id starts with empty consensus state (#2053).
        _clear_pipeline_runtime_state(pipeline_id, reason="pipeline_delete")

        store.delete_pipeline(pipeline_id)

        logger.info("Pipeline deleted", pipeline_id=pipeline_id)

        return make_success_response("Pipeline deleted")

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


@pipelines_bp.route("/<pipeline_id>/agents/<agent_role>/restart", methods=["POST"])
@require_lifecycle_secret
def restart_agent(pipeline_id: str, agent_role: str) -> tuple[Response, int]:
    """Restart a single agent in a pipeline.

    Stops the existing container, resets its consensus state, and respawns
    it with the same configuration.  The agent's per-agent worktree is
    preserved so committed work is retained.

    URL params:
        pipeline_id: Pipeline ID
        agent_role: Agent role to restart (e.g. "coder", "tester")

    Query string (optional):
        slice_id: Slice scope (``slice-<N>``). When supplied, the
            slice-scoped Job and worktree are restarted, ``EGG_SLICE_ID``
            is propagated to the new Job, and consensus reset targets
            the per-slice tracker. Pipeline-level agents omit it.
            ``slice_id`` may also be supplied via the JSON body.

    Request body (optional):
        {
            "reason": "Human-readable reason for the restart",
            "slice_id": "slice-2"
        }

    Response:
        {
            "success": true,
            "data": {
                "container_id": "abc123...",
                "agent_role": "coder",
                "restart_count": 1
            }
        }
    """
    repo_path = get_repo_path()

    try:
        store, pipeline = _resolve_pipeline(pipeline_id, repo_path)
    except InvalidPipelineIdError:
        return make_error_response(f"Invalid pipeline ID format: {pipeline_id}", status_code=400)
    except PipelineNotFoundError:
        return make_error_response(f"Pipeline {pipeline_id} not found", status_code=404)

    # Validate agent role
    try:
        role = AgentRole(agent_role)
    except ValueError:
        return make_error_response(f"Invalid agent role: {agent_role}", status_code=400)

    # Validate pipeline is in a restartable state.  CANCELLED is included so
    # that a cancel_task(cleanup=false) pipeline can be resumed without a
    # full resubmission (see #1725).
    if pipeline.status not in (
        PipelineStatus.RUNNING,
        PipelineStatus.AWAITING_HUMAN,
        PipelineStatus.FAILED,
        PipelineStatus.CANCELLED,
    ):
        return make_error_response(
            f"Pipeline {pipeline_id} is not in a restartable state (status: {pipeline.status.value})",
            status_code=409,
        )

    body = request.get_json(silent=True) or {}
    reason = body.get("reason", "Manual restart via API")

    # Slice scope (#2410): query param wins over body so the URL
    # form is unambiguous; both forms validate against the canonical
    # ``slice-<N>`` shape via ``extract_slice_id``.
    raw_slice_id = request.args.get("slice_id")
    slice_payload = {"slice_id": raw_slice_id} if raw_slice_id is not None else body
    try:
        slice_id = extract_slice_id(slice_payload)
    except ValueError as e:
        return make_error_response(str(e), status_code=400)

    # Slice-existence check (#2421): a well-formed but unknown
    # ``slice_id`` would otherwise spawn an orphan Job + worktree
    # the rest of the system has no record of. The shape regex in
    # ``extract_slice_id`` only catches malformed values; only the
    # contract knows which slices the pipeline actually has.
    #
    # Pipelines without a contract (BABYSIT, CUSTOM+PR) are not
    # slice-aware, so any non-``None`` ``slice_id`` targeting them is
    # by definition unknown — reject outright. For contracted
    # pipelines, load the contract and check membership; fall through
    # silently if the contract can't be loaded (worktree pruned,
    # contract not yet populated, filesystem error) so we don't
    # regress legitimate restarts on the existing pipeline-level path.
    #
    # The contract is also consulted to resolve the slice's parent
    # edge (#2439) so the spawner's ``base_branch`` matches the
    # parent slice's integration branch on a worktree-absent restart.
    parent_slice_id: str | None = None
    # ``parent_branch_recorded`` captures ``Slice.parent_branch_at_creation``
    # — the literal branch the parent slice's integration branch was forked
    # off of when its worktree was provisioned (#2137 TASK-4-2). Preferring
    # the recorded value over reconstructing
    # ``f"{_issue_branch}/{parent_slice_id}"`` is more robust: if a future
    # qualifier-suffix or namespacing change lands in
    # ``_run_one_slice_inner`` but not here, the reconstruction would
    # silently drift while the recorded value would not (#2460 review).
    # ``None`` for slices whose worktree has not been provisioned yet, in
    # which case we fall through to reconstruction.
    parent_branch_recorded: str | None = None
    # ``parent_slice_complete`` is set when the parent slice has reached
    # ``SliceStatus.COMPLETE`` per the contract — i.e. its PR has plausibly
    # been merged. GitHub's standard branch-auto-cleanup deletes the head
    # branch on merge, so the gateway's per-repo ``git fetch origin
    # <parent_branch>`` would wedge the restart on a missing-branch fetch
    # error. When ``True``, we fall back to ``pipeline.base_branch`` rather
    # than depend on a (likely deleted) parent ref — matching the
    # contract-unloadable fall-through policy: prefer letting the restart
    # proceed over over-strict gating (#2470).
    parent_slice_complete: bool = False
    if slice_id is not None:
        if not pipeline.has_contract:
            return make_error_response(
                f"slice_id {slice_id!r} is invalid for pipeline "
                f"{pipeline_id} (pipeline has no contract; not slice-aware)",
                status_code=404,
                details={
                    "slice_id": slice_id,
                    "known_slices": [],
                },
            )
        try:
            from egg_contracts.loader import (
                ContractNotFoundError,
                ContractValidationError,
                load_contract,
            )
            from routes import resolve_worktree_path
        except ImportError:
            logger.warning(
                "Required modules unavailable; skipping slice_id existence check",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
            )
        else:
            contract = None
            try:
                worktree_path = resolve_worktree_path(pipeline_id, Path(repo_path))
                contract_id = _pipeline_identifier(pipeline.issue_number, pipeline_id)
                try:
                    contract = load_contract(contract_id, worktree_path)
                except ContractNotFoundError:
                    # Contract not yet populated — fall through silently
                    # (``contract`` already initialised to ``None`` above).
                    pass
            except (OSError, ValueError, ContractValidationError) as exc:
                # Worktree pruned, filesystem failure, or corrupt/invalid
                # contract JSON: log and fall through. The reviewer's #2421
                # ask was to catch the easy "wrong slice_id" case, not to
                # gate restarts on contract reachability. Programmer errors
                # (AttributeError, TypeError, NameError) are left to
                # propagate so they surface during development.
                logger.warning(
                    "Could not load contract for slice_id existence check; allowing restart",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    error=str(exc),
                )
            if contract is not None:
                # Single-pass slice lookup: the existence check and the
                # parent-edge read both need the same record, so do them
                # together (#2460 review observation 4).
                slice_obj = next((s for s in contract.slices if s.id == slice_id), None)
                if slice_obj is None:
                    return make_error_response(
                        f"slice_id {slice_id!r} does not match any slice in "
                        f"pipeline {pipeline_id}'s contract",
                        status_code=404,
                        details={
                            "slice_id": slice_id,
                            "known_slices": sorted(s.id for s in contract.slices),
                        },
                    )
                # Resolve the slice's parent edge (#2439). The forest
                # constraint enforced by contract ingestion guarantees
                # at most one DAG parent per slice, so taking the first
                # dependency is sufficient. Root slices (no parent)
                # leave ``parent_slice_id`` as ``None``.
                if slice_obj.dependencies:
                    parent_slice_id = slice_obj.dependencies[0]
                    parent_branch_recorded = slice_obj.parent_branch_at_creation
                    # #2470: if the parent slice is complete, its PR has
                    # plausibly been merged and its branch deleted by
                    # GitHub auto-cleanup. Detect that here so the
                    # base-branch selection below can fall back to
                    # ``pipeline.base_branch`` rather than wedge the
                    # restart on a missing-branch fetch.
                    from egg_contracts.models import SliceStatus

                    parent_obj = next(
                        (s for s in contract.slices if s.id == parent_slice_id),
                        None,
                    )
                    if parent_obj is not None and parent_obj.status == SliceStatus.COMPLETE:
                        parent_slice_complete = True

    # Restart the container via spawner
    spawner = _get_spawner()

    # Gather spawn parameters from pipeline state
    current_phase = pipeline.current_phase.value
    phase_exec = pipeline.phases.get(current_phase)

    # Early status update: transition FAILED/CANCELLED -> RUNNING before the
    # slow container restart so that get_status returns "running" immediately,
    # even if the MCP call times out (see #1594, #1725).
    if pipeline.status in (PipelineStatus.FAILED, PipelineStatus.CANCELLED):
        early_lock = get_pipeline_state_lock(pipeline_id)
        with early_lock:
            pipeline = store.load_pipeline(pipeline_id)
            if pipeline.status in (PipelineStatus.FAILED, PipelineStatus.CANCELLED):
                pipeline.status = PipelineStatus.RUNNING
                _phase_exec = pipeline.phases.get(current_phase)
                if _phase_exec is not None:
                    _phase_exec.status = PipelineStatus.RUNNING
                pipeline.updated_at = datetime.now(UTC)
                store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))

    # Compute gateway mode from pipeline config (not hardcoded "public")
    gateway_mode, _ = _compute_gateway_mode(pipeline)

    # Slice-aware restart branch (#2428). When the restart targets a
    # slice agent, the spawner must register the gateway session
    # against the slice integration branch (``<root>/<slice_id>``); the
    # pipeline tip (``<root>/work``) is the wrong target and every
    # subsequent push from the restarted agent would be rejected by
    # the gateway's branch allowlist. Mirror the slice scheduler's
    # derivation in :func:`_run_implement_phase_slices` so a restarted
    # slice agent lands on the same integration branch its peers are
    # using.
    #
    # The spawner's ``base_branch`` is the ref the per-agent worktree
    # is forked off of when the worktree must be (re)created (#2439).
    # ``pipeline.branch`` (the integration tip) is the wrong target —
    # if the worktree is absent at restart time, forking from the
    # pipeline tip pulls sibling slices' commits into the rebuilt
    # worktree. The right base depends on context:
    #   - Pipeline-level restart: ``pipeline.base_branch`` (matches
    #     the rest of the codebase: every other call site uses
    #     ``pipeline.base_branch`` for spawner ``base_branch``).
    #   - Slice restart with a parent in the contract's slice forest:
    #     the parent slice's integration branch
    #     (``<root>/<parent_slice_id>``), mirroring ``parent_branch``
    #     in :func:`_run_one_slice_inner`.
    #   - Root-slice restart, or slice restart when the contract
    #     can't be loaded: ``pipeline.base_branch`` (fallback).
    #
    # Note: this intentionally diverges from the initial-spawn path at
    # :func:`_run_concurrent_phase` (line ~12398), which always passes
    # ``base_branch=pipeline.base_branch`` regardless of slice forest
    # position. #2439 specifically asks for the parent-slice fork on
    # the *restart* path so a worktree-absent restart of a child slice
    # rebuilds atop its parent slice rather than re-forking from
    # ``pipeline.base_branch`` and losing the parent's commits. Don't
    # "fix" this asymmetry by aligning the two paths without first
    # re-reading #2439.
    if slice_id is not None:
        _pipeline_branch = pipeline.branch or (
            f"egg/issue-{pipeline.issue_number}/work"
            if pipeline.issue_number is not None
            else f"egg/{pipeline_id}/work"
        )
        _issue_branch = _slice_namespace_root(_pipeline_branch)
        # Defense-in-depth: re-validate the slice id shape before
        # embedding it in a git ref. ``extract_slice_id`` (above)
        # already enforces ``^slice-[0-9]+$`` on the request payload,
        # but the helper is part of the gateway-facing surface — a
        # future caller that forgets upstream validation must not be
        # able to smuggle path separators or shell metacharacters in
        # via this seam. Mirrors the check at
        # ``concurrent_executor.get_worktree_branch`` so both spawn
        # entrypoints share the same canonical pattern.
        if not SLICE_ID_PATTERN.fullmatch(slice_id):
            raise ValueError(
                f"slice_id={slice_id!r} does not match the canonical shape ``slice-<N>``"
            )
        agent_branch = f"{_issue_branch}/{slice_id}"
        if parent_slice_id is not None and parent_slice_complete:
            # #2470: parent slice's PR has plausibly been merged and its
            # branch deleted by GitHub auto-cleanup. Falling back to
            # ``pipeline.base_branch`` is safe: ``complete`` means the
            # parent's commits have been integrated upstream (either via
            # PR merge or via the cascade), and prefer letting the
            # restart proceed over wedging on a missing-branch fetch.
            base_branch_for_restart = pipeline.base_branch
        elif parent_slice_id is not None:
            if parent_branch_recorded:
                # Prefer the literal branch the parent slice was
                # provisioned against (#2460 review observation 2).
                # Set by ``_run_one_slice_inner`` at slice creation
                # time; per the docstring on
                # ``Slice.parent_branch_at_creation``, it is *the*
                # recorded fact about how the slice was provisioned.
                # We trust our own writer here — no extra ref-shape
                # validation, just the gateway's per-repo ``git
                # fetch`` will surface a malformed value.
                base_branch_for_restart = parent_branch_recorded
            else:
                # Fallback: contract has the dependency edge but no
                # provisioning record. Reconstruct from the slice
                # namespace root and the parent's id.
                #
                # Defense-in-depth: ``parent_slice_id`` came from the
                # contract loader (whose ``Slice.id`` regex permits
                # both canonical ``slice-<N>`` and legacy
                # ``phase-<N>``), so accept either shape before
                # embedding into a git ref. Anything outside that
                # envelope is a corrupt-contract smell — fail loudly
                # rather than synthesising a malformed ref the
                # gateway would reject anyway.
                if not _SLICE_OR_PHASE_ID_PATTERN.fullmatch(parent_slice_id):
                    raise ValueError(
                        f"parent_slice_id={parent_slice_id!r} does not match "
                        f"the canonical shape ``slice-<N>`` (or legacy ``phase-<N>``)"
                    )
                base_branch_for_restart = f"{_issue_branch}/{parent_slice_id}"
        else:
            base_branch_for_restart = pipeline.base_branch
    else:
        agent_branch = pipeline.branch
        base_branch_for_restart = pipeline.base_branch

    # Reconstruct command and extra_env for concurrent agents.
    # In concurrent mode, agents need a consensus-wrapped prompt command
    # and role-specific environment variables to function properly.
    command = None
    extra_env: dict[str, str] = {}
    try:
        try:
            from concurrent_executor import ConcurrentPhaseExecutor, is_concurrent_execution
        except ImportError:
            from ..concurrent_executor import ConcurrentPhaseExecutor, is_concurrent_execution

        if is_concurrent_execution(pipeline, phase=current_phase):
            # Reconstruct extra_env via ConcurrentPhaseExecutor
            executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=lambda **kw: None)  # type: ignore[arg-type]
            extra_env = executor.get_agent_env(role)

            # Reconstruct the agent prompt and wrap it for consensus
            try:
                env_path = os.environ.get("EGG_REPO_PATH", "/home/egg/repos")
                base_path = Path(env_path)
                repo_name = (pipeline.repo or "").split("/")[-1]
                worktree_repo_path = resolve_worktree_repo_path(base_path, repo_name)
                _resolved_base = None
                try:
                    _resolved_base = get_default_branch(worktree_repo_path)
                except Exception:
                    pass

                prompt_text = _build_agent_prompt(
                    role_value=agent_role,
                    phase=current_phase,
                    pipeline_id=pipeline_id,
                    pipeline_mode=pipeline.mode.value if pipeline.mode else "issue",
                    prompt=pipeline.prompt,
                    issue_number=pipeline.issue_number,
                    repo=pipeline.repo,
                    branch=pipeline.branch,
                    base_branch=_resolved_base,
                    repo_path=str(worktree_repo_path),
                    concurrent=True,
                    network_mode=gateway_mode,
                    mode=pipeline.mode,
                    pr_number=getattr(pipeline, "pr_number", None),
                )
                if prompt_text:
                    from consensus_wrapper import build_consensus_wrapped_command

                    command = build_consensus_wrapped_command(prompt_text)
            except Exception as prompt_err:
                logger.warning(
                    "Failed to reconstruct agent prompt for restart "
                    "(agent will start without a prompt command)",
                    pipeline_id=pipeline_id,
                    agent_role=agent_role,
                    error=str(prompt_err),
                )
    except ImportError:
        logger.debug("Concurrent executor not available for restart prompt reconstruction")
    except Exception as e:
        logger.warning(
            "Failed to reconstruct concurrent env for restart",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            error=str(e),
        )

    try:
        spawned = spawner.restart_agent_container(
            pipeline_id=pipeline_id,
            agent_role=role,
            issue_number=pipeline.issue_number,
            mode=gateway_mode,
            extra_env=extra_env or None,
            repos=[pipeline.repo] if pipeline.repo else None,
            phase=current_phase,
            command=command,
            branch=agent_branch,
            base_branch=base_branch_for_restart,
            reason=reason,
            spawn_max_retries=pipeline.config.spawn_max_retries,
            spawn_retry_initial_backoff_seconds=pipeline.config.spawn_retry_initial_backoff_seconds,
            slice_id=slice_id,
        )
    except (ContainerSpawnError, KubernetesSpawnError) as e:
        # Revert early status update — the agent is not actually running.
        # Consensus state is intentionally NOT reset here so a failed spawn
        # preserves the agent's prior consensus participation.
        revert_lock = get_pipeline_state_lock(pipeline_id)
        with revert_lock:
            pipeline = store.load_pipeline(pipeline_id)
            pipeline.status = PipelineStatus.FAILED
            _phase_exec = pipeline.phases.get(current_phase)
            if _phase_exec is not None:
                _phase_exec.status = PipelineStatus.FAILED
            pipeline.updated_at = datetime.now(UTC)
            store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))
        return make_error_response(f"Failed to restart agent: {e}", status_code=500)

    # Spawn succeeded — now reset consensus state for this agent.
    # If consensus reset fails, log a warning but don't fail the restart:
    # the restarted agent will re-enter consensus on its own.
    # Slice-scoped restarts (#2410) target the per-slice tracker; the
    # pipeline-level tracker has no record of the slice agent.
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (
                get_peer_consensus_tracker,  # type: ignore[import-not-found]
            )

        tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
        if tracker:
            tracker.remove_agent(agent_role)
            logger.info(
                "Reset consensus state for agent",
                pipeline_id=pipeline_id,
                agent_role=agent_role,
                slice_id=slice_id,
            )
    except ImportError:
        pass
    except Exception as e:
        logger.warning(
            "Failed to reset consensus state (agent will re-enter consensus)",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            error=str(e),
        )

    try:
        try:
            from consensus import get_consensus_evaluator
        except ImportError:
            from ..consensus import get_consensus_evaluator  # type: ignore[import-not-found]

        evaluator = get_consensus_evaluator()
        evaluator.remove_agent(pipeline_id, agent_role)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(
            "Failed to reset legacy consensus state",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            error=str(e),
        )

    # Reset health-monitor anchor so the pre-respawn _last_heartbeat does not
    # generate a stale-elapsed heartbeat_timeout alert against the fresh
    # container (issue #2084).
    try:
        try:
            from health_monitor import get_health_monitor
        except ImportError:
            from ..health_monitor import (
                get_health_monitor,  # type: ignore[import-not-found]
            )
        _hm = get_health_monitor()
        if _hm is not None:
            _hm.reset_agent(agent_role)
    except Exception as e:
        logger.warning(
            "Failed to reset health-monitor state for restarted agent",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            error=str(e),
        )

    # Update pipeline state with new container/agent info
    lock = get_pipeline_state_lock(pipeline_id)
    with lock:
        pipeline = store.load_pipeline(pipeline_id)
        if phase_exec is not None:
            # Re-fetch from the freshly loaded pipeline (the outer check gates
            # on "did the phase exist before the spawn?").
            fresh_phase_exec = pipeline.phases.get(current_phase)
            if fresh_phase_exec is not None:
                # Add new container info
                fresh_phase_exec.containers.append(spawned.container_info)

                # Update or add agent execution entry
                from models import AgentExecution  # type: ignore

                # Refresh ``started_at`` to the new container's spawn time so
                # ``_get_concurrent_status`` reports an ``elapsed_seconds``
                # anchored on the live container.  Without this the field
                # carries the original spawn timestamp and the overseer's
                # phase_minimum_working_window suppression on the
                # ``agent-heartbeat-stall`` trigger is structurally dead on
                # the ``restart_agent`` path (issue #2084).
                respawn_started_at = datetime.now(UTC)
                # Match on ``(role, slice_id)`` — without the slice tiebreaker
                # the first matching role wins, which on a multi-slice phase
                # mutates the wrong slice's record (#2422). ``slice_id`` is
                # the route-level scope already plumbed into the spawner and
                # consensus tracker above.
                found = False
                for agent in fresh_phase_exec.agents:
                    if not hasattr(agent, "role"):
                        continue
                    role_match = agent.role == role or (
                        hasattr(agent.role, "value") and agent.role.value == role.value
                    )
                    if not role_match:
                        continue
                    if getattr(agent, "slice_id", None) != slice_id:
                        continue
                    agent.container_id = spawned.container_info.container_id
                    agent.status = AgentExecutionStatus.RUNNING
                    agent.started_at = respawn_started_at
                    found = True
                    break
                if not found:
                    fresh_phase_exec.agents.append(
                        AgentExecution(
                            role=role,
                            container_id=spawned.container_info.container_id,
                            status=AgentExecutionStatus.RUNNING,
                            started_at=respawn_started_at,
                            slice_id=slice_id,
                        )
                    )

        pipeline.updated_at = datetime.now(UTC)
        store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))

    # Slice-scoped restarts (#2410) bumped the per-slice budget bucket
    # ``(pipeline_id, agent_role, slice_id)``; the pipeline-level
    # ``(pipeline_id, agent_role, None)`` bucket is untouched. Reading
    # without ``slice_id`` here would return the pipeline-level count
    # (typically zero) and the audit log + JSON response below would
    # misreport the operator's "you've burned N of M restarts" telemetry.
    restart_count = spawner.get_restart_count(pipeline_id, agent_role, slice_id=slice_id)

    logger.info(
        "Agent restarted",
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        container_id=spawned.container_info.container_id[:12],
        restart_count=restart_count,
        reason=reason,
    )

    return make_success_response(
        f"Agent {agent_role} restarted",
        data={
            "container_id": spawned.container_info.container_id,
            "agent_role": agent_role,
            "restart_count": restart_count,
        },
    )


@pipelines_bp.route("/<pipeline_id>/phases/<phase>/restart", methods=["POST"])
@require_lifecycle_secret
def restart_phase(pipeline_id: str, phase: str) -> tuple[Response, int]:
    """Restart all agents in a pipeline phase.

    Stops and removes all containers for the phase, resets consensus and
    review cycle state, and respawns all agents.  Prior phase artifacts
    (from earlier phases) are preserved.

    URL params:
        pipeline_id: Pipeline ID
        phase: Phase name to restart (e.g. "implement")

    Request body (optional):
        {
            "reason": "Human-readable reason for the restart"
        }

    Response:
        {
            "success": true,
            "data": {
                "phase": "implement",
                "agents_to_restart": ["coder", "tester", "documenter", ...]
            }
        }
    """
    repo_path = get_repo_path()

    try:
        store, pipeline = _resolve_pipeline(pipeline_id, repo_path)
    except InvalidPipelineIdError:
        return make_error_response(f"Invalid pipeline ID format: {pipeline_id}", status_code=400)
    except PipelineNotFoundError:
        return make_error_response(f"Pipeline {pipeline_id} not found", status_code=404)

    # Validate phase
    try:
        PipelinePhase(phase)
    except ValueError:
        return make_error_response(f"Invalid phase: {phase}", status_code=400)

    # Validate pipeline is in a restartable state.  CANCELLED is included so
    # that a cancel_task(cleanup=false) pipeline can be resumed without a
    # full resubmission (see #1725).
    if pipeline.status not in (
        PipelineStatus.RUNNING,
        PipelineStatus.AWAITING_HUMAN,
        PipelineStatus.FAILED,
        PipelineStatus.CANCELLED,
    ):
        return make_error_response(
            f"Pipeline {pipeline_id} is not in a restartable state (status: {pipeline.status.value})",
            status_code=409,
        )

    # Only the current phase can be restarted — restarting a completed or
    # future phase would corrupt pipeline state.
    if phase != pipeline.current_phase.value:
        return make_error_response(
            f"Phase {phase} is not the current phase (current: {pipeline.current_phase.value})",
            status_code=409,
        )

    phase_exec = pipeline.phases.get(phase)
    if phase_exec is None:
        return make_error_response(
            f"Phase {phase} not found in pipeline {pipeline_id}", status_code=404
        )

    body = request.get_json(silent=True) or {}
    reason = body.get("reason", "Manual phase restart via API")

    # Compute gateway mode from pipeline config (not hardcoded "public")
    gateway_mode, _ = _compute_gateway_mode(pipeline)

    spawner = _get_spawner()

    # Acquire the pipeline state lock to collect agent roles, snapshot
    # container IDs, and update pipeline status to RUNNING *before* the
    # slow container teardown.  This ensures that ``get_status`` returns
    # ``running`` immediately, even if the MCP call times out during
    # container stop/remove (see #1594).
    lock = get_pipeline_state_lock(pipeline_id)
    with lock:
        # Re-load pipeline under the lock so agent_roles reflects the
        # latest state (guards against concurrent modifications).
        pipeline = store.load_pipeline(pipeline_id)

        # Re-check current phase under the lock to prevent TOCTOU race:
        # the pipeline could have advanced between the earlier check and
        # lock acquisition.
        if phase != pipeline.current_phase.value:
            return make_error_response(
                f"Phase {phase} is not the current phase (current: {pipeline.current_phase.value})",
                status_code=409,
            )

        phase_exec = pipeline.phases.get(phase)
        if phase_exec is None:
            return make_error_response(
                f"Phase {phase} not found in pipeline {pipeline_id}", status_code=404
            )

        # 1. Collect agent roles for respawning. Prefer the runtime cache
        #    on ``phase_exec.agents`` since it reflects the roster from
        #    the most recent spawn, but fall back to the deterministic
        #    source the executor itself consults — ``pipeline.active_roles``
        #    first (CUSTOM-mode / BABYSIT overrides, #1762), then
        #    ``get_roles_for_phase`` for ISSUE-mode pipelines. Without
        #    this fallback a restart whose clear step ran
        #    (``phase_exec.agents = []`` below) but whose spawn step
        #    failed leaves the pipeline unrecoverable: every subsequent
        #    ``restart_phase`` 400s on the now-empty cache, and
        #    ``start_pipeline`` 409s on the CANCELLED state (#2515).
        agent_roles: list[AgentRole] = []
        for agent in phase_exec.agents:
            if hasattr(agent, "role"):
                role = agent.role if isinstance(agent.role, AgentRole) else AgentRole(agent.role)
                agent_roles.append(role)

        if not agent_roles:
            # Mirror ``_run_concurrent_phase`` exactly so the route's
            # response (and the downstream worktree-delete / health-
            # monitor reset) matches the roster the spawn will actually
            # produce: when ``active_roles`` is set, use it verbatim
            # and do NOT fall through to ``get_roles_for_phase``.
            # Otherwise the all-unknown-override edge case would have
            # the route promise a phase-default roster while the spawn
            # produced nothing.
            _roster_override = getattr(pipeline, "active_roles", None)
            if _roster_override:
                for r_value in _roster_override:
                    try:
                        agent_roles.append(AgentRole(r_value))
                    except ValueError:
                        # Unknown role from a newer schema — skip so
                        # BRC doesn't wait on an unspawnable agent.
                        continue
            else:
                try:
                    from egg_contracts.agent_roles import (
                        get_roles_for_phase as _get_roles_for_phase,
                    )

                    for r in _get_roles_for_phase(
                        phase,
                        include_reviewers=True,
                        repo=pipeline.repo,
                        has_contract=getattr(pipeline, "has_contract", True),
                    ):
                        try:
                            agent_roles.append(AgentRole(r.value))
                        except ValueError:
                            continue
                except Exception as exc:  # noqa: BLE001
                    # Catch derivation failures so the route returns 400
                    # rather than 500 — deliberate divergence from
                    # ``_run_concurrent_phase`` (``pipelines.py:12813-12840``),
                    # which lets the same failure propagate up the worker
                    # thread. In a synchronous HTTP context an honest 400
                    # ("No agents found") is more useful to the operator
                    # than a 500.
                    logger.warning(
                        "restart_phase: failed to derive default roster fallback",
                        pipeline_id=pipeline_id,
                        phase=phase,
                        error=str(exc),
                    )

            if not agent_roles:
                return make_error_response(
                    f"No agents found in phase {phase} to restart", status_code=400
                )

            logger.info(
                "restart_phase: phase_exec.agents empty, derived roster from pipeline config",
                pipeline_id=pipeline_id,
                phase=phase,
                agent_roles=[r.value for r in agent_roles],
            )

        # 2. Snapshot container IDs for teardown outside the lock
        old_container_ids = [c.container_id for c in phase_exec.containers]

        # 3. Fully reset phase execution state so the new _run_pipeline
        #    thread treats this as a fresh phase.  Set pipeline status to
        #    RUNNING and bump run_epoch so any lingering old _run_pipeline
        #    thread detects the restart and exits (see #1638).
        #    NOTE: artifacts are intentionally preserved — they may contain
        #    outputs from partial work useful as context for the retry.
        phase_exec.containers = []
        phase_exec.agents = []
        phase_exec.review_cycles = 0
        phase_exec.hitl_review_cycles = 0
        phase_exec.status = PipelineStatus.PENDING
        phase_exec.started_at = None
        phase_exec.work_started_at = None
        phase_exec.completed_at = None
        phase_exec.error = None
        phase_exec.cycle_timings = []
        pipeline.status = PipelineStatus.RUNNING
        pipeline.error = None
        pipeline.run_epoch = datetime.now(UTC)
        # ``updated_at`` is unconditionally set by ``StateStore.save_pipeline``
        # (which ``update_pipeline`` routes through).
        store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))

    # --- Outside the lock: slow, idempotent, best-effort operations ---

    # 4. Stop and remove old containers
    for container_id in old_container_ids:
        try:
            spawner.stop_agent_container(container_id, cleanup_session=True)
        except Exception as e:
            logger.warning(
                "Failed to stop container during phase restart",
                container_id=container_id[:12] if container_id else "?",
                error=str(e),
            )
        try:
            spawner.remove_agent_container(container_id, force=True, cleanup_session=False)
        except Exception as e:
            logger.warning(
                "Failed to remove container during phase restart",
                container_id=container_id[:12] if container_id else "?",
                error=str(e),
            )

    # 4b. Delete per-agent worktrees so respawned containers get fresh mounts.
    #     Without this, stale worktree directories (e.g. broken btrfs mounts)
    #     survive container removal and cause create_worktree to skip creation
    #     or fail.  Mirrors cleanup_pipeline's worktree deletion.  (#1723)
    #
    #     Enumerate from disk rather than guess names: slice-scoped worktrees
    #     are ``{pipeline_id}-slice-{N}-{role}``, not ``{pipeline_id}-{role}``,
    #     so a name-guess loop misses every per-slice worktree on a slice
    #     pipeline and leaves them behind.  (#2522)
    #
    #     ``validate_git=False`` so that broken/corrupted worktrees (missing
    #     or unreadable ``.git`` marker — exactly the #1723 btrfs failure
    #     class) still reach ``delete_worktrees``. The default
    #     ``validate_git=True`` is salvage-correct (you can't salvage a
    #     broken worktree) but cleanup-incorrect (you must still delete it).
    restart_role_values = {role.value for role in agent_roles}
    try:
        all_worktrees = agent_salvage.enumerate_agent_worktrees(pipeline_id, validate_git=False)
    except (OSError, ImportError, RuntimeError) as e:
        logger.warning(
            "Failed to enumerate per-agent worktrees during phase restart",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        all_worktrees = []
    worktrees_to_delete = [wt for wt in all_worktrees if wt.agent_role in restart_role_values]

    # Salvage unpushed agent commits before deleting worktrees (#2429).
    # Restart is *the* scenario where unpushed commits accumulate: an
    # operator hits this endpoint precisely because agents are wedged or
    # timed out — the same conditions that prevent pushes from landing on
    # ``origin/<assigned_branch>``. Without this hook, restart would be
    # the one orchestrator-side worktree-delete code path that bypasses
    # salvage and silently destroys recoverable work. Best-effort: any
    # failure logs and continues so cleanup cannot be blocked by salvage.
    if worktrees_to_delete:
        try:
            agent_salvage.auto_salvage_pipeline(
                spawner.gateway,
                pipeline_id,
                worktree_filter={wt.worktree_id for wt in worktrees_to_delete},
                mode=gateway_mode,
                base_branch=pipeline.base_branch,
            )
        except Exception as e:
            logger.warning(
                "Auto-salvage failed during phase restart; proceeding with worktree deletion",
                pipeline_id=pipeline_id,
                error=str(e),
            )

    for wt in worktrees_to_delete:
        log_extras: dict[str, str] = {}
        if wt.slice_id is not None:
            log_extras["slice_id"] = wt.slice_id
        try:
            spawner.gateway.delete_worktrees(container_id=wt.worktree_id, force=True)
            logger.info(
                "Deleted per-agent worktree during phase restart",
                agent_worktree_id=wt.worktree_id,
                pipeline_id=pipeline_id,
                **log_extras,
            )
        except Exception as e:
            logger.warning(
                "Failed to delete per-agent worktree during phase restart",
                agent_worktree_id=wt.worktree_id,
                pipeline_id=pipeline_id,
                error=str(e),
                **log_extras,
            )

    # 5. Reset consensus state
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (
                get_peer_consensus_tracker,  # type: ignore[import-not-found]
            )

        tracker = get_peer_consensus_tracker(pipeline_id)
        if tracker:
            tracker.clear()
            logger.info("Cleared peer consensus tracker", pipeline_id=pipeline_id)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(
            "Failed to clear peer consensus",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    try:
        try:
            from consensus import get_consensus_evaluator
        except ImportError:
            from ..consensus import get_consensus_evaluator  # type: ignore[import-not-found]

        evaluator = get_consensus_evaluator()
        evaluator.clear(pipeline_id)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(
            "Failed to clear legacy consensus",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    # 6. Reset restart counts for this pipeline
    spawner.reset_restart_counts(pipeline_id)

    # 6b. Drop health-monitor anchors for every respawned role so the Tier-1
    #     heartbeat clock does not survive the restart and fire stale-elapsed
    #     alerts that the overseer would faithfully escalate (issue #2084).
    try:
        try:
            from health_monitor import get_health_monitor
        except ImportError:
            from ..health_monitor import (
                get_health_monitor,  # type: ignore[import-not-found]
            )
        _hm = get_health_monitor()
        if _hm is not None:
            for role in agent_roles:
                _hm.reset_agent(role.value)
    except Exception as e:
        logger.warning(
            "Failed to reset health-monitor state during phase restart",
            pipeline_id=pipeline_id,
            phase=phase,
            error=str(e),
        )

    # 7. Launch a new _run_pipeline thread to monitor the restarted phase.
    #    Container spawning is handled by _run_concurrent_phase within the
    #    thread, matching the recovery pattern used by start_pipeline.
    #    See #1638: the original polling thread died when the pipeline
    #    failed; without this, consensus completion is never detected.
    agents_to_restart = [role.value for role in agent_roles]
    repo_path_for_thread = store.repo_path

    _spawn_pipeline_run_thread(pipeline_id, repo_path_for_thread, pipeline.run_epoch)

    logger.info(
        "Phase restarted",
        pipeline_id=pipeline_id,
        phase=phase,
        agents_to_restart=agents_to_restart,
        reason=reason,
    )

    return make_success_response(
        f"Phase {phase} restarted with {len(agents_to_restart)} agent(s)",
        data={
            "phase": phase,
            "agents_to_restart": agents_to_restart,
        },
    )


def _filter_salvage_worktrees(
    worktrees: list[Any],
    *,
    agent_role: str | None,
    slice_id: str | None,
) -> list[Any]:
    """Filter ``enumerate_agent_worktrees`` output by role / slice scope.

    ``agent_role`` and ``slice_id`` may both be ``None`` (return all) or
    set together to scope down to one specific worktree. ``agent_role``
    set with ``slice_id=None`` matches non-slice per-agent worktrees.
    The pipeline-level worktree (``agent_role=None`` on the worktree)
    is included only when the caller did not specify ``agent_role``.
    """
    out = []
    for wt in worktrees:
        if agent_role is not None and wt.agent_role != agent_role:
            continue
        if slice_id is not None and wt.slice_id != slice_id:
            continue
        out.append(wt)
    return out


def _serialize_commit_report(report: Any) -> dict[str, Any]:
    """Convert a ``WorktreeCommitReport`` to a JSON-safe dict."""
    return {
        "worktree_id": report.worktree.worktree_id,
        "agent_role": report.worktree.agent_role,
        "slice_id": report.worktree.slice_id,
        "local_branch": report.worktree.local_branch,
        "assigned_branch": report.assigned_branch,
        "anchor_ref": report.anchor_ref,
        "commits": [
            {
                "sha": c.sha,
                "summary": c.summary,
                "author": c.author,
                "authored_at": c.authored_at,
                "files_changed": c.files_changed,
            }
            for c in report.commits
        ],
        "error": report.error,
    }


def _serialize_salvage_result(result: Any) -> dict[str, Any]:
    """Convert a ``SalvageResult`` to a JSON-safe dict."""
    return {
        "worktree_id": result.worktree_id,
        "agent_role": result.agent_role,
        "slice_id": result.slice_id,
        "recovery_ref": result.recovery_ref,
        "head_sha": result.head_sha,
        "n_commits": result.n_commits,
        "ok": result.ok,
        "error": result.error,
    }


@pipelines_bp.route("/<pipeline_id>/local-commits", methods=["GET"])
def list_pipeline_local_commits(pipeline_id: str) -> tuple[Response, int]:
    """List unpushed commits across this pipeline's per-agent worktrees.

    Inspects every per-agent worktree on disk
    (``{pipeline_id}``, ``{pipeline_id}-{role}``,
    ``{pipeline_id}-slice-{N}-{role}``) and reports the commits on its
    local ``egg/{worktree_id}/work`` branch that are not reachable from
    ``origin/<assigned_branch>`` (or ``origin/<base_branch>`` as a
    fallback). Read-only — no fetch, no push.

    Query string (optional):
        agent_role: Filter to a single agent role (e.g. ``coder``).
        slice_id: Filter to a single slice scope (e.g. ``slice-2``).

    Response:
        {
            "success": true,
            "data": {
                "pipeline_id": "issue-2261-v9",
                "worktrees": [
                    {
                        "worktree_id": "issue-2261-v9-slice-2-coder",
                        "agent_role": "coder",
                        "slice_id": "slice-2",
                        "local_branch": "egg/issue-2261-v9-slice-2-coder/work",
                        "assigned_branch": "egg/issue-2261-v9/slice-2",
                        "anchor_ref": "refs/remotes/origin/egg/issue-2261-v9/slice-2",
                        "commits": [
                            {"sha": "...", "summary": "...", "author": "...",
                             "authored_at": "...", "files_changed": 3}
                        ],
                        "error": null
                    }
                ]
            }
        }
    """
    repo_path = get_repo_path()

    try:
        _store, pipeline = _resolve_pipeline(pipeline_id, repo_path)
    except InvalidPipelineIdError:
        return make_error_response(f"Invalid pipeline ID format: {pipeline_id}", status_code=400)
    except PipelineNotFoundError:
        return make_error_response(f"Pipeline {pipeline_id} not found", status_code=404)

    agent_role = request.args.get("agent_role") or None
    if agent_role is not None:
        try:
            AgentRole(agent_role)
        except ValueError:
            return make_error_response(f"Invalid agent role: {agent_role}", status_code=400)

    raw_slice_id = request.args.get("slice_id")
    try:
        slice_id = extract_slice_id({"slice_id": raw_slice_id} if raw_slice_id is not None else {})
    except ValueError as e:
        return make_error_response(str(e), status_code=400)

    worktrees = _filter_salvage_worktrees(
        agent_salvage.enumerate_agent_worktrees(pipeline_id),
        agent_role=agent_role,
        slice_id=slice_id,
    )
    reports = [
        agent_salvage.list_unpushed_commits(wt, base_branch=pipeline.base_branch)
        for wt in worktrees
    ]

    return make_success_response(
        f"Listed local commits for pipeline {pipeline_id}",
        data={
            "pipeline_id": pipeline_id,
            "worktrees": [_serialize_commit_report(r) for r in reports],
        },
    )


@pipelines_bp.route("/<pipeline_id>/salvage", methods=["POST"])
@require_lifecycle_secret
def salvage_pipeline_local_commits(pipeline_id: str) -> tuple[Response, int]:
    """Push unpushed agent commits to recovery refs (#2429).

    For every matching per-agent worktree, push its HEAD to
    ``egg/recovered/<pipeline_id>/<scope>/<short_sha>`` via the gateway's
    launcher-auth path. Launcher auth bypasses the agent-targeted
    branch-allowlist check so this works even when the agent's own
    pushes were rejected for the wrong-branch reason this verb exists
    to recover from.

    Query string (optional):
        agent_role: Salvage only this role's worktree.
        slice_id: Salvage only this slice scope.

    Response (always ``success: true`` when the request was well-formed
    — per-worktree failures are reported in ``data.results``):
        {
            "success": true,
            "data": {
                "pipeline_id": "issue-2261-v9",
                "results": [
                    {"worktree_id": "...", "agent_role": "coder", "slice_id": "slice-2",
                     "recovery_ref": "egg/recovered/issue-2261-v9/slice-2-coder/9665f37a6...",
                     "head_sha": "9665f37a6...", "n_commits": 14, "ok": true, "error": null}
                ]
            }
        }
    """
    repo_path = get_repo_path()

    try:
        _store, pipeline = _resolve_pipeline(pipeline_id, repo_path)
    except InvalidPipelineIdError:
        return make_error_response(f"Invalid pipeline ID format: {pipeline_id}", status_code=400)
    except PipelineNotFoundError:
        return make_error_response(f"Pipeline {pipeline_id} not found", status_code=404)

    agent_role = request.args.get("agent_role") or None
    if agent_role is not None:
        try:
            AgentRole(agent_role)
        except ValueError:
            return make_error_response(f"Invalid agent role: {agent_role}", status_code=400)

    raw_slice_id = request.args.get("slice_id")
    try:
        slice_id = extract_slice_id({"slice_id": raw_slice_id} if raw_slice_id is not None else {})
    except ValueError as e:
        return make_error_response(str(e), status_code=400)

    worktrees = _filter_salvage_worktrees(
        agent_salvage.enumerate_agent_worktrees(pipeline_id),
        agent_role=agent_role,
        slice_id=slice_id,
    )

    gateway_mode, _vis = _compute_gateway_mode(pipeline)
    gateway = get_gateway_client()

    results = []
    for wt in worktrees:
        try:
            result = agent_salvage.salvage_worktree(
                gateway,
                wt,
                base_branch=pipeline.base_branch,
                mode=gateway_mode,
            )
        except Exception as e:  # noqa: BLE001 — must always return a result row
            logger.warning(
                "Salvage raised unexpectedly",
                pipeline_id=pipeline_id,
                worktree_id=wt.worktree_id,
                error=str(e),
            )
            result = agent_salvage.SalvageResult(
                worktree_id=wt.worktree_id,
                agent_role=wt.agent_role,
                slice_id=wt.slice_id,
                recovery_ref=None,
                head_sha=None,
                n_commits=0,
                ok=False,
                error=str(e),
            )
        results.append(result)

    return make_success_response(
        f"Salvaged {sum(1 for r in results if r.ok and r.recovery_ref)} of "
        f"{len(results)} per-agent worktrees for pipeline {pipeline_id}",
        data={
            "pipeline_id": pipeline_id,
            "results": [_serialize_salvage_result(r) for r in results],
        },
    )


@pipelines_bp.route("/<pipeline_id>/status", methods=["GET"])
def get_pipeline_status(pipeline_id: str) -> tuple[Response, int]:
    """
    Get pipeline status summary.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "id": "issue-123",
                "status": "running",
                "current_phase": "implement",
                "pending_decisions": 0
            }
        }
    """
    repo_path = get_repo_path()

    try:
        _store, pipeline = _resolve_pipeline(pipeline_id, repo_path)

        pending = pipeline.get_pending_decisions()

        data = {
            "id": pipeline.id,
            "status": pipeline.status.value,
            "current_phase": pipeline.current_phase.value,
            "pending_decisions": len(pending),
            "updated_at": pipeline.updated_at.isoformat(),
        }

        # Include first pending decision details so the collaborator
        # doesn't need a second round-trip to fetch it
        if pending:
            d = pending[0]
            data["pending_decision"] = {
                "id": d.id,
                "question": d.question,
                "context": d.context,
                "options": d.options,
                "created_at": d.created_at.isoformat(),
            }

        # Include PR info once the PR phase has created a PR (#1625) so
        # monitoring clients don't need to scrape `gh pr list` by title.
        pr_url, pr_number = _get_pr_info(pipeline)
        if pr_url:
            data["pr_url"] = pr_url
            if pr_number is not None:
                data["pr_number"] = pr_number

        # Include concurrent execution monitoring when enabled
        concurrent_data = _get_concurrent_status(pipeline)
        if concurrent_data:
            data["concurrent"] = concurrent_data

        # Surface the orchestrator-process-wide slice-admission state
        # (#2241 gap 1) so operators can see when slices are queued
        # behind the global cap rather than wedged. The shape is
        # {cap, admitted, admitted_keys}; ``admitted_keys`` lists
        # ``"<pipeline_id>/<slice_id>"`` so the operator can tell
        # which slices currently hold the budget.
        try:
            try:
                from orchestrator import global_slice_admit
            except ImportError:
                import global_slice_admit  # type: ignore[no-redef]

            data["slice_admit"] = global_slice_admit.snapshot()
        except Exception:  # noqa: BLE001
            # Defensive: never let admit-state collection crash the
            # status endpoint — the cap is advisory, not load-bearing
            # for the pipeline's own progress.
            pass

        # Issue #1962 TASK-1-2: include the overseer-relevant config
        # subset in the status payload so the sandbox-side overseer
        # monitor can read PipelineConfig values (advisor model,
        # threshold knobs, host-detection flag) without a separate
        # endpoint. Only the new + load-bearing knobs are exposed
        # here to keep the response compact; full config is available
        # via the dedicated config endpoint.
        try:
            cfg = getattr(pipeline, "config", None)
            if cfg is not None:
                data["config"] = {
                    "overseer_advisor_model": getattr(cfg, "overseer_advisor_model", None),
                    "overseer_advisor_recent_log_bytes_cap": getattr(
                        cfg, "overseer_advisor_recent_log_bytes_cap", None
                    ),
                    "overseer_auto_file_issues_mode": getattr(
                        cfg, "overseer_auto_file_issues_mode", None
                    ),
                    "overseer_owns_host_detection": getattr(
                        cfg, "overseer_owns_host_detection", False
                    ),
                    "overseer_stuck_phase_transition_seconds": getattr(
                        cfg, "overseer_stuck_phase_transition_seconds", 180
                    ),
                    "overseer_agent_stall_seconds": getattr(
                        cfg, "overseer_agent_stall_seconds", 180
                    ),
                    "overseer_silent_agent_threshold_seconds": getattr(
                        cfg, "overseer_silent_agent_threshold_seconds", 600
                    ),
                    "overseer_long_running_phase_seconds": getattr(
                        cfg, "overseer_long_running_phase_seconds", 3600
                    ),
                    "overseer_nack_unresolved_seconds": getattr(
                        cfg, "overseer_nack_unresolved_seconds", 180
                    ),
                }
        except AttributeError, TypeError:
            # Defensive: never let a config-shape change crash the
            # status endpoint.
            pass

        return make_success_response("Status retrieved", data=data)

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


# -----------------------------------------------------------------
# GET /api/v1/pipelines/<pipeline_id>/status/wait (issue #1932)
#
# Event-driven host-side wait primitive.  Blocks up to ``wait``
# seconds until one of the allowlisted EventBus events or message
# types fires, then returns a small envelope the MCP handler
# enriches with a full status snapshot.  See
# docs/reference/agent-wait-patterns.md §7 for the end-to-end
# protocol.
# -----------------------------------------------------------------
@pipelines_bp.route("/<pipeline_id>/status/wait", methods=["GET"])
def wait_pipeline_status(pipeline_id: str) -> tuple[Response, int]:
    """Block up to ``wait`` seconds on the next pipeline-relevant event.

    Query params:
        wait: seconds to block, default 25, clamped to
              ``GET_STATUS_MAX_WAIT`` (25) so the caller stays
              safely inside the Claude Code MCP tool-call timeout.
        since: opaque cursor ``msg:<id>|evt:<seq>`` from a prior
              response.  An empty / missing cursor snaps to the tip
              on both sources (first-call semantics).  Returns 400
              if the cursor is syntactically malformed.

    Responses:
        200 — either a ``changed=true`` envelope (event or message
              fired before the timeout) or a ``changed=false,
              no_change=true`` envelope (timeout elapsed with no
              pipeline-relevant event).  Always carries ``cursor``
              so the caller can seed the next request.
        400 — malformed cursor or malformed ``wait``.
        404 — pipeline does not exist.

    Implementation:
        * ``queue.Queue(maxsize=16)`` coordinates the two sources:
          a wildcard EventBus handler (synchronous) and a daemon
          thread running ``message_store.get_messages(wait=...)``.
        * First source wins.  On return the EventBus handler is
          unsubscribed in ``finally``; the daemon thread is left
          lame-duck for up to ``wait`` seconds (accepted per plan
          risk R14 — bounded, non-blocking on shutdown).
        * ``egg_inflight_host_waits`` gauge is incremented at entry
          and decremented on return.

    Args:
        pipeline_id: Pipeline ID from the URL.
    """
    # Validate pipeline exists before doing any expensive setup.
    repo_path = get_repo_path()
    try:
        _store, pipeline = _resolve_pipeline(pipeline_id, repo_path)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    # Parse + clamp ``wait``.  ``GET_STATUS_MAX_WAIT`` lives in
    # ``mcp_server`` — importing it here keeps the cap in one place.
    try:
        from mcp_server import GET_STATUS_MAX_WAIT
    except ImportError:
        try:
            from ..mcp_server import GET_STATUS_MAX_WAIT  # type: ignore[no-redef]
        except ImportError:
            GET_STATUS_MAX_WAIT = 25  # conservative fallback
    try:
        requested_wait = int(request.args.get("wait", str(GET_STATUS_MAX_WAIT)))
    except ValueError, TypeError:
        return make_error_response(
            "Invalid 'wait' query parameter: must be an integer",
            status_code=400,
        )
    timeout = min(max(requested_wait, 1), GET_STATUS_MAX_WAIT)

    # Parse the opaque compound cursor.  ``ok=False`` is the only
    # 400 path here — unknown cursors on either source are tolerated
    # and degrade to "snap to tip".
    ok, msg_since_id, event_since_seq = _parse_status_wait_cursor(request.args.get("since"))
    if not ok:
        return make_error_response(
            "Invalid 'since' cursor — expected 'msg:<id>|evt:<seq>' (either half may be empty).",
            status_code=400,
        )

    # Lazy imports keep the route cheap to load at module import and
    # match the pattern used elsewhere in this file.  We compare events
    # against ``_STATUS_WAIT_EVENT_TYPES`` by the string value of
    # ``event.event_type`` — the ``EventType`` class itself is not
    # needed here.
    try:
        from events import get_event_bus
    except ImportError:  # pragma: no cover
        try:
            from ..events import get_event_bus  # type: ignore[no-redef]
        except ImportError:
            return make_error_response("Event bus not available", status_code=500)

    try:
        from routes.messages import _apply_delphi_filter as _delphi
    except ImportError:  # pragma: no cover
        try:
            from .messages import _apply_delphi_filter as _delphi  # type: ignore[no-redef]
        except ImportError:
            _delphi = None  # type: ignore[assignment]

    import queue as _queue

    event_bus = get_event_bus()

    # Synchronous up-front cursor-staleness probe (issue #2464). The route
    # used to silently keep re-emitting ``msg_since_id`` whenever the store
    # tip was empty (post-phase-clear), so a polling client kept feeding
    # the dead cursor back forever. Probe once at entry with ``wait=0`` so
    # we can both stop re-emitting it and surface ``since_id_stale: True``
    # in the envelope, letting consumers (sandbox CLI cursor file, agent
    # wait_loop) drop the stale cursor and re-snap to tip. Done before
    # the terminal short-circuit below so a request that arrives after
    # both a phase clear and pipeline completion still sees the flag.
    since_id_stale = False
    if msg_since_id is not None:
        try:
            store_fn = _get_message_store()
            store = store_fn()
            _msgs, meta = store.get_messages_with_meta(
                pipeline_id,
                since_id=msg_since_id,
                limit=1,
                wait=0,
                # Suppress the "since_id not found in store" warning on
                # this probe so a single ``/status/wait`` request that
                # hits a stale cursor doesn't double-log: the long-poll
                # daemon below makes its own ``get_messages`` call with
                # the same cursor and emits the warning once. Pre-PR
                # cadence was one warning per request; we preserve that.
                _suppress_stale_warning=True,
            )
            since_id_stale = meta.since_id_stale
        except Exception as exc:  # pragma: no cover
            logger.debug(
                "status_wait staleness probe error",
                pipeline_id=pipeline_id,
                error=str(exc),
            )

    # Late-subscriber short-circuit (issue #2378): if the pipeline is
    # already terminal at request time, the relevant ``pipeline.*``
    # event was emitted before this call could subscribe — and the
    # snap-to-tip below would cement that miss.  Synthesize a Path-A
    # envelope so callers don't loop until the 1-hour cap.  This covers
    # the common path where ``mark FAILED`` succeeds; the synthetic
    # emit at ``_run_pipeline``'s mark-FAILED-failed branch covers the
    # rarer case where the FAILED-mark itself raises.
    _TERMINAL_EVENT_TYPES = {
        PipelineStatus.COMPLETE: "pipeline.completed",
        PipelineStatus.FAILED: "pipeline.failed",
        PipelineStatus.CANCELLED: "pipeline.cancelled",
    }
    if pipeline.status in _TERMINAL_EVENT_TYPES:
        # Issue #2464: don't fall back to ``msg_since_id`` when the tip
        # is empty — that's exactly the post-clear state that perpetuates
        # the dead cursor.
        terminal_cursor = _build_status_wait_cursor(
            _message_store_tip_id(pipeline_id),
            event_bus.current_sequence(),
        )
        terminal_envelope = _build_minimal_status_envelope(pipeline, terminal_cursor)
        terminal_envelope.update(
            {
                "changed": True,
                "trigger": "event",
                "event_type": _TERMINAL_EVENT_TYPES[pipeline.status],
            }
        )
        if since_id_stale:
            terminal_envelope["since_id_stale"] = True
        return make_success_response("Pipeline already terminal", data=terminal_envelope)

    # Snap event_since_seq to the current tip on first call.  This
    # preserves the "events before the call are already seen"
    # semantic and matches the message-bus ``from_tip`` behaviour
    # used by ``/messages/wait`` (issue #1925).
    if event_since_seq is None:
        event_since_seq = event_bus.current_sequence()

    wake_q: _queue.Queue[tuple[str, Any]] = _queue.Queue(maxsize=16)

    def _on_event(event) -> None:  # pragma: no cover - exercised via tests
        if event.pipeline_id != pipeline_id:
            return
        if event.event_type.value not in _STATUS_WAIT_EVENT_TYPES:
            return
        if event.sequence <= event_since_seq:
            return
        try:
            wake_q.put_nowait(("event", event))
        except _queue.Full:
            logger.warning(
                "status_wait event queue full; dropping event",
                pipeline_id=pipeline_id,
                event_type=event.event_type.value,
            )

    def _on_message_store_wake() -> None:  # pragma: no cover - exercised via tests
        try:
            store_fn = _get_message_store()
            store = store_fn()
            messages = store.get_messages(
                pipeline_id,
                since_id=msg_since_id,
                limit=100,
                wait=timeout,
                wait_for_types=list(_STATUS_WAIT_MESSAGE_TYPES),
                from_tip=msg_since_id is None,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug(
                "status_wait daemon error",
                pipeline_id=pipeline_id,
                error=str(exc),
            )
            return
        if not messages:
            return
        try:
            wake_q.put_nowait(("message", messages))
        except _queue.Full:
            logger.warning(
                "status_wait message queue full; dropping message",
                pipeline_id=pipeline_id,
            )

    _track_host_wait_start()
    event_bus.subscribe(None, _on_event)
    daemon: threading.Thread | None = None
    try:
        daemon = threading.Thread(
            target=_on_message_store_wake,
            name=f"status-wait-msg-{pipeline_id}",
            daemon=True,
        )
        daemon.start()

        try:
            source, payload = wake_q.get(timeout=timeout)
        except _queue.Empty:
            source = None
            payload = None

        # Re-load the pipeline once here so both paths share a
        # consistent snapshot for the minimal envelope.
        try:
            _store2, fresh_pipeline = _resolve_pipeline(pipeline_id, repo_path)
        except InvalidPipelineIdError, PipelineNotFoundError:
            fresh_pipeline = pipeline

        if source == "event":
            event = payload
            # Issue #2464: never fall back to ``msg_since_id`` when the
            # tip is empty. After a phase-boundary clear the caller's
            # cursor is dead; re-emitting it here is what kept the
            # ``since_id not found in store`` warning firing on every
            # subsequent poll. ``since_id_stale: True`` in the envelope
            # tells the consumer to drop its cached cursor.
            tip_msg_id = _message_store_tip_id(pipeline_id)
            cursor = _build_status_wait_cursor(tip_msg_id, event.sequence)
            envelope = _build_minimal_status_envelope(fresh_pipeline, cursor)
            envelope.update(
                {
                    "changed": True,
                    "trigger": "event",
                    "event_type": event.event_type.value,
                }
            )
            if since_id_stale:
                envelope["since_id_stale"] = True
            return make_success_response("Event wake", data=envelope)

        if source == "message":
            messages = payload
            # Issue #2464: same as the event path — fall back to None
            # when the message half is unavailable rather than re-emitting
            # the stale ``msg_since_id``.
            last_id = messages[-1].id if messages else None
            # Delphi filter pass — currently a no-op for the host caller
            # (role=None returns messages unchanged) but plumbed here so a
            # future role parameter can enable reviewer-redaction (R13).
            if _delphi is not None:
                try:
                    messages = _delphi(pipeline_id, None, messages)
                except Exception:  # pragma: no cover
                    pass
            tip_evt_seq = event_bus.current_sequence()
            cursor = _build_status_wait_cursor(last_id, tip_evt_seq)
            envelope = _build_minimal_status_envelope(fresh_pipeline, cursor)
            envelope.update(
                {
                    "changed": True,
                    "trigger": "message",
                    "messages": [m.to_dict() for m in messages],
                }
            )
            if since_id_stale:
                envelope["since_id_stale"] = True
            return make_success_response("Message wake", data=envelope)

        # Timeout path — minimal envelope only.
        tip_msg_id = _message_store_tip_id(pipeline_id)
        tip_evt_seq = event_bus.current_sequence()
        cursor = _build_status_wait_cursor(tip_msg_id, tip_evt_seq)
        envelope = _build_minimal_status_envelope(fresh_pipeline, cursor)
        envelope.update({"changed": False, "no_change": True})
        if since_id_stale:
            envelope["since_id_stale"] = True
        return make_success_response("No change within wait window", data=envelope)
    finally:
        try:
            event_bus.unsubscribe(None, _on_event)
        except Exception:  # pragma: no cover — unsubscribe is best-effort
            pass
        _track_host_wait_end()
        # Daemon thread is deliberately left running — it exits on
        # its own when ``message_store.get_messages`` returns or the
        # timeout elapses (plan risk R14, accepted).


def _get_pr_info(pipeline: Pipeline) -> tuple[str | None, int | None]:
    """Extract PR URL and number from the PR phase artifacts.

    Returns ``(pr_url, pr_number)`` or ``(None, None)`` when no PR has
    been created. The single source of truth is
    ``phases["pr"].artifacts["pr_url"]``, written after ``_auto_create_pr``
    succeeds (see the PR phase completion path). ``pr_number`` is parsed
    from the URL; callers get ``None`` for unusually shaped URLs but
    ``pr_url`` is still returned so they can fall back gracefully.
    """
    pr_phase = pipeline.phases.get(PipelinePhase.PR.value)
    if not pr_phase or not pr_phase.artifacts:
        return None, None
    pr_url = pr_phase.artifacts.get("pr_url")
    if not pr_url:
        return None, None
    match = re.search(r"/pull/(\d+)", pr_url)
    pr_number = int(match.group(1)) if match else None
    return pr_url, pr_number


def _get_concurrent_status(pipeline: Pipeline) -> dict | None:
    """Get concurrent execution monitoring data for a pipeline.

    Returns None if concurrent execution is not enabled for this pipeline.
    Returns a dict with the following structure when concurrent mode is active::

        {
            "enabled": True,
            "max_concurrent_agents": int,
            "messages": {"total": int, "by_type": {"PROGRESS": int, ...}},
            "consensus": {
                "agents": {"coder": {"state": "READY", ...}, ...},
                "is_complete": bool,
                "blocking_agents": ["role", ...]  # agents not yet READY
            },
            "agents": [{"role": str, "status": str}, ...]  # from phase execution
        }

    Dependencies on other concurrent-mode modules (message_store, consensus) are
    imported lazily and degrade gracefully to empty structures when unavailable.
    """
    try:
        from concurrent_executor import is_concurrent_execution
    except ImportError:
        from ..concurrent_executor import is_concurrent_execution  # type: ignore[no-redef]

    current_phase = pipeline.current_phase.value if pipeline.current_phase else None
    if not is_concurrent_execution(pipeline, phase=current_phase):
        return None

    config = pipeline.config
    result: dict = {
        "enabled": True,
        "max_concurrent_agents": getattr(config, "max_concurrent_agents", 6),
    }

    # Message store provides aggregate counts of inter-agent messages by type.
    # This module is implemented in phase-1 of the concurrent execution feature;
    # ImportError is expected until that phase lands.
    try:
        from message_store import get_message_store
    except ImportError:
        try:
            from ..message_store import get_message_store  # type: ignore[no-redef]
        except ImportError:
            logger.debug("Message store not available for status")
            get_message_store = None  # type: ignore[assignment]

    if get_message_store is not None:
        store = get_message_store()
        msg_status = store.get_status(pipeline.id)
        result["messages"] = {
            "total": msg_status.get("total", 0),
            "by_type": msg_status.get("by_type", {}),
        }
    else:
        result["messages"] = {"total": 0, "by_type": {}}

    # Consensus evaluator tracks per-agent readiness states and determines
    # whether all agents agree the phase is complete. Implemented in phase-3;
    # blocking_agents lists roles that are not yet READY (WORKING or BLOCKED).
    # BRC peer consensus (preferred) or legacy readiness-based
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

        tracker = get_peer_consensus_tracker(pipeline.id)
        if not tracker:
            # Attempt lazy reconstruction from message store for concurrent pipelines
            try:
                from review_graph import get_review_graph_for_phase

                try:
                    from peer_consensus import reconstruct_tracker_from_messages
                except ImportError:
                    from ..peer_consensus import (
                        reconstruct_tracker_from_messages,  # type: ignore[no-redef]
                    )

                if is_concurrent_execution(pipeline, pipeline.current_phase):
                    graph = get_review_graph_for_phase(
                        pipeline.current_phase.value, repo=pipeline.repo
                    )
                    tracker = reconstruct_tracker_from_messages(pipeline.id, graph)
            except ImportError:
                pass  # Fall through to legacy evaluator
            except Exception as e:
                logger.warning(
                    "Tracker reconstruction failed",
                    error=str(e),
                    pipeline_id=pipeline.id,
                )
        if tracker:
            consensus_state = tracker.get_state()
        else:
            try:
                from consensus import get_consensus_evaluator
            except ImportError:
                from ..consensus import get_consensus_evaluator  # type: ignore[no-redef]

            evaluator = get_consensus_evaluator()
            consensus_state = evaluator.get_state(pipeline.id)
    except ImportError:
        try:
            try:
                from consensus import get_consensus_evaluator
            except ImportError:
                from ..consensus import get_consensus_evaluator  # type: ignore[no-redef]

            evaluator = get_consensus_evaluator()
            consensus_state = evaluator.get_state(pipeline.id)
        except ImportError:
            logger.debug("Consensus evaluator not available for status")
            consensus_state = None

    if consensus_state is not None:
        agents_data = {}
        for role, agent_info in consensus_state.get("agents", {}).items():
            if hasattr(agent_info, "state"):
                # Legacy AgentReadiness object
                agents_data[role] = {
                    "state": agent_info.state.value,
                    "reason": agent_info.reason,
                    "updated_at": agent_info.timestamp.isoformat()
                    if agent_info.timestamp
                    else None,
                }
            else:
                # BRC dict format
                agents_data[role] = agent_info
        result["consensus"] = {
            "agents": agents_data,
            "is_complete": consensus_state.get("is_complete", False),
            "blocking_agents": consensus_state.get("blocking_agents", []),
            "protocol": consensus_state.get("protocol", "readiness"),
        }
    else:
        # Don't populate consensus with empty placeholder — callers (e.g. the
        # MCP get_consensus_status tool) use truthiness to decide whether to
        # fall back to message-based inference.  An empty-but-truthy dict
        # prevents that fallback from triggering (see issue #1229).
        pass

    # Agent lifecycle info from the phase execution record — shows which agents
    # are spawned for the current phase and their container-level status.
    # Includes ``container_id`` and server-computed ``elapsed_seconds`` so the
    # sandboxed overseer can anchor stall-duration math on the live container's
    # ``started_at`` rather than pre-restart message-bus events (issue #2084).
    current_phase_name = pipeline.current_phase.value
    phase_exec = pipeline.phases.get(current_phase_name)
    if phase_exec and hasattr(phase_exec, "agents"):
        now = datetime.now(UTC)
        agents_info = []
        for agent in phase_exec.agents:
            if hasattr(agent, "role"):
                role = agent.role.value if hasattr(agent.role, "value") else str(agent.role)
            else:
                role = str(agent)
            if hasattr(agent, "status"):
                status = agent.status.value if hasattr(agent.status, "value") else "unknown"
            else:
                status = "unknown"

            entry: dict[str, Any] = {"role": role, "status": status}

            container_id = getattr(agent, "container_id", None)
            if isinstance(container_id, str) and container_id:
                entry["container_id"] = container_id

            started_at = getattr(agent, "started_at", None)
            started_dt: datetime | None = None
            if isinstance(started_at, datetime):
                started_dt = started_at
            elif isinstance(started_at, str) and started_at:
                try:
                    started_dt = datetime.fromisoformat(started_at)
                except ValueError:
                    started_dt = None
            if started_dt is not None:
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=UTC)
                entry["started_at"] = started_dt.isoformat()
                entry["elapsed_seconds"] = max(0, int((now - started_dt).total_seconds()))

            agents_info.append(entry)
        result["agents"] = agents_info

    return result


def _read_shared_criteria(
    filename: str,
    user_override: str | None = None,
    repo_path: str | None = None,
) -> str | None:
    """Read shared criteria from file, checking user override first.

    Search order:
    1. .egg/<user_override> in the repo (if user_override provided)
    2. shared/prompts/<filename> relative to source tree
    3. /app/prompts/<filename> (Docker container path)

    Returns the file content, or None if no file found (caller uses inline fallback).
    """
    # Check user override first
    if user_override and repo_path:
        override_path = Path(repo_path) / ".egg" / user_override
        if override_path.is_file() and override_path.stat().st_size > 0:
            return override_path.read_text()

    # Try source tree path (development / tests)
    source_path = Path(__file__).parent.parent.parent / "shared" / "prompts" / filename
    if source_path.is_file():
        return source_path.read_text()

    # Try Docker container path (production)
    docker_path = Path("/app/prompts") / filename
    if docker_path.is_file():
        return docker_path.read_text()

    return None


def _get_agent_design_criteria() -> str:
    """Return agent-mode design review criteria."""
    content = _read_shared_criteria("agent-design-criteria.md")
    if content is not None:
        return content
    logger.warning("Shared agent-design-criteria.md not found, using inline fallback")
    return (
        "Flag these **clear** anti-patterns:\n\n"
        "1. **Excessive pre-fetching** — Baking large diffs (10KB+) or full file contents "
        "into prompts instead of letting the agent fetch what it needs\n"
        "2. **Structured output for humans** — Requiring JSON when output goes directly "
        "to humans rather than machines\n"
        "3. **Post-processing pipelines** — Scripts that parse agent output to take actions "
        "the agent could take directly\n"
        "4. **Rigid procedures** — Micromanaging step-by-step procedures when objectives "
        "would suffice\n"
        "5. **Prompt-level security** — Using instructions for constraints that should be "
        "sandbox-enforced\n"
        "6. **Direct LLM API calls outside sandbox** — Calling the Anthropic API from "
        "orchestrator, gateway, or shared code instead of delegating to sandbox containers\n"
        "7. **Direct API calls bypassing the Agent SDK** — Using raw HTTP calls to the "
        "Anthropic API instead of run_agent() (in-sandbox) or build_agent_command() "
        "(orchestrator-spawned containers). Unlike item 6 (scoped to infra code), "
        "this applies everywhere including sandbox code.\n"
        "8. **Hardcoded model identifiers** — Using full model IDs (date-pinned or "
        "version-pinned) instead of short aliases (sonnet, opus, haiku)\n"
    )


def _get_code_review_criteria(repo_path: str | None = None) -> str:
    """Return code review criteria."""
    content = _read_shared_criteria(
        "code-review-criteria.md",
        user_override="review-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    logger.warning("Shared code-review-criteria.md not found, using inline fallback")
    return (
        "### Security (highest priority)\n"
        "- Injection vulnerabilities (SQL, command, XSS, LDAP, path traversal)\n"
        "- Authentication/authorization flaws\n"
        "- Credential exposure, hardcoded secrets\n"
        "- SSRF, open redirects, unsafe deserialization\n\n"
        "### Correctness\n"
        "- Logic errors, off-by-one, boundary conditions\n"
        "- Race conditions, deadlocks, concurrency bugs\n"
        "- Null/undefined handling, missing error paths\n"
        "- Resource leaks (connections, file handles, memory)\n"
        "- End-to-end feature functionality: verify new features work in their "
        "real execution environment\n\n"
        "### Robustness\n"
        "- Missing input validation at trust boundaries\n"
        "- Unhandled exceptions that could crash the system\n"
        "- Missing retry logic for transient failures\n"
        "- Inadequate timeouts for external calls\n\n"
        "### Design\n"
        "- Violations of existing codebase patterns\n"
        "- Breaking changes to public interfaces\n"
        "- Tight coupling that will hinder future changes\n\n"
        "### Severity Classification\n\n"
        "**Blocking** (request changes):\n"
        "- Security vulnerabilities\n"
        "- Non-functional features — the feature's core purpose does not work "
        "end-to-end\n"
        "- Logic errors that produce incorrect results\n"
        "- Breaking changes to existing functionality\n"
        "- Resource leaks or crashes\n"
        "- Pre-existing broken or inconsistent behavior in code the PR "
        "modifies\n\n"
        "**Non-blocking** (suggestions):\n"
        "- Code quality improvements (naming, structure, duplication)\n"
        "- Defense-in-depth additions\n"
        "- Missing edge case handling that doesn't affect the core feature\n"
        "- Documentation gaps\n"
        "- Style or convention deviations not caught by linters\n\n"
        "**Do not dismiss issues as 'not a regression'**: If a PR modifies "
        "code that has existing broken or inconsistent behavior, the issue is "
        "blocking even if the PR didn't introduce it. A PR that adds a new "
        "code path through already-inconsistent logic makes the inconsistency "
        "worse.\n\n"
        "**Beware of false analogies**: When comparing new code to existing "
        "patterns, verify the analogy holds at the execution-model level. "
        "Two features may look structurally similar in config but have "
        "completely different execution paths. If the existing pattern works "
        "via mechanism A but the new code relies on mechanism B that doesn't "
        "exist, the comparison is invalid — classify based on actual "
        "functionality, not superficial similarity.\n\n"
        "### Skip\n\n"
        "- Style issues handled by linters (formatting, import order)\n"
        "- Type annotation completeness (type checkers handle this)\n"
        "- Auto-generated files (migrations, lock files)\n"
        "- `.egg-state/` pipeline artifacts (contracts, drafts, BRC history "
        "— managed by the orchestrator)\n"
    )


def _get_contract_review_criteria(repo_path: str | None = None) -> str:
    """Return contract verification criteria."""
    content = _read_shared_criteria(
        "contract-review-criteria.md",
        user_override="contract-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    logger.warning("Shared contract-review-criteria.md not found, using inline fallback")
    return (
        "### Task Verification\n"
        "For each task in the contract, verify:\n"
        "1. The described functionality is present in the code\n"
        "2. The acceptance criteria for the task is satisfied\n"
        "3. If a commit is linked, verify it relates to the task\n"
        "4. Where applicable, tests cover the new functionality\n\n"
        "### Phase Consistency\n"
        "- All tasks in completed phases are actually implemented\n"
        "- Phase status matches task completion state\n"
        "- No orphaned code exists that isn't covered by any task\n\n"
        "### Acceptance Criteria Verification\n"
        "For each acceptance criterion:\n"
        "1. Examine the implementation to verify it meets the criterion\n"
        "2. Note any gaps in your review\n\n"
        "### Contract Integrity\n"
        "- No implementation changes violate previously verified criteria\n"
        "- New changes don't break existing contract compliance\n"
        "- All required files listed in tasks are present\n"
    )


def _get_refine_review_criteria() -> str:
    """Return review criteria for the dedicated refine reviewer."""
    return (
        "### 1. Problem Understanding\n"
        "- Does the analysis correctly identify the core problem or feature request?\n"
        "- Is the current behavior (if applicable) accurately described?\n"
        "- Are the goals and desired outcomes clear?\n\n"
        "### 2. Research Quality\n"
        "- Has the agent explored the relevant parts of the codebase?\n"
        "- Are existing patterns and conventions identified?\n"
        "- Is the technical context accurate and thorough?\n\n"
        "### 3. Options Analysis\n"
        "- Are the proposed options meaningfully different?\n"
        "- Are trade-offs clearly articulated for each option?\n"
        "- Is the reasoning logical and well-founded?\n\n"
        "### 4. Constraints and Dependencies\n"
        "- Are technical constraints identified (performance, compatibility, etc.)?\n"
        "- Are dependencies on other code or systems noted?\n"
        "- Are potential risks or complications surfaced?\n\n"
        "### 5. Open Questions\n"
        "- Are open questions specific enough for a human to answer?\n"
        "- Do questions address genuine ambiguities?\n"
        "- Are questions actionable?\n"
        "- Are ALL uncertainties and assumptions surfaced? The analysis should not "
        "proceed with unvalidated assumptions when it could ask the human instead.\n\n"
        "### 6. Recommendation Quality\n"
        "- Is there a clear recommended approach?\n"
        "- Is the recommendation justified with specific reasons?\n"
        "- Does the recommendation align with the analysis findings?\n\n"
        "### 7. HITL Decision Registration\n"
        "- Run `egg-contract show` and verify that contract decisions or feedback "
        "items exist for every open question in the analysis.\n"
        "- If open questions appear as prose text without corresponding "
        "`<!-- egg-hitl-decision ... -->` or `<!-- egg-hitl-feedback ... -->` "
        "markers (generated by `egg-contract`), flag as `needs_revision` — "
        "the agent must re-run `egg-contract add-decision` or "
        "`egg-contract add-feedback` for each question.\n"
        "- If there are zero open questions, verify that the requirements are "
        "genuinely unambiguous and no assumptions were made silently.\n"
    )


def _get_plan_review_criteria() -> str:
    """Return review criteria for the dedicated plan reviewer."""
    return (
        "### 1. Alignment with Analysis\n"
        "- Does the plan implement the recommended approach from the analysis?\n"
        "- If the plan deviates from the analysis, is the reason explained?\n"
        "- Are all requirements from the analysis addressed?\n\n"
        "### 2. Task Breakdown\n"
        "- Are tasks discrete, actionable, and properly scoped?\n"
        "- Is each task small enough to implement in a single pass?\n"
        "- Are task boundaries clear (no overlapping responsibilities)?\n\n"
        "### 3. Acceptance Criteria\n"
        "- Does each task have clear, testable acceptance criteria?\n"
        "- Are criteria specific enough to verify completion?\n"
        "- Do criteria cover both happy path and edge cases?\n\n"
        "### 4. Dependency Ordering\n"
        "- Are task dependencies correctly identified?\n"
        "- Is the ordering logical (foundations before features)?\n"
        "- Are there opportunities for parallelism that are missed?\n\n"
        "### 5. Risk Assessment\n"
        "- Are technical risks identified (security, performance, compatibility)?\n"
        "- Are mitigation strategies concrete and actionable?\n"
        "- Is the rollback plan realistic?\n\n"
        "### 6. Test Strategy\n"
        "- Is the test strategy appropriate for the scope of changes?\n"
        "- Are both unit and integration tests considered?\n"
        "- Are test scenarios aligned with acceptance criteria?\n\n"
        "### 7. Completeness\n"
        "- Does the plan cover all aspects of the original request?\n"
        "- Are documentation updates included where needed?\n"
        "- Are there any obvious gaps or missing tasks?\n"
    )


def _get_security_review_criteria(repo_path: str | None = None) -> str:
    """Return security-lens review criteria (issue #1965).

    The shared file inherits from ``code-review-criteria.md`` and adds
    lens-specific rules (cross-file allowlist mismatches,
    handler-vs-validator path mismatches, info-disclosure / authz bypass,
    uncommitted-artifact mismatches, secret leakage, OWASP cross-file
    patterns). Falls back to a short inline placeholder when the shared
    file isn't available.
    """
    content = _read_shared_criteria(
        "security-review-criteria.md",
        user_override="security-review-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    logger.warning("Shared security-review-criteria.md not found, using inline fallback")
    return (
        "Inherits from `code-review-criteria.md`; only lens-specific rules "
        "below override or extend it.\n\n"
        "### Security lens (focus areas)\n"
        "- **Cross-file allowlist mismatch** — handler in one file references "
        "a check defined / extended in a different file (the PR #1964 "
        "`^project$` pattern).\n"
        "- **Handler-vs-validator path mismatch** — verify the validator's "
        "regex / allowlist actually covers every code path the handler "
        "reaches.\n"
        "- Information-disclosure and authorization-bypass patterns at "
        "trust boundaries.\n"
        "- Uncommitted-artifact / Dockerfile-symlink mismatches (the PR "
        "#1964 `sandbox/scripts/jira` pattern).\n"
        "- Secret leakage via logs, error text, environment dumps, or "
        "version-controlled config.\n"
        "- OWASP top-10 patterns spanning more than one changed file.\n"
    )


def _get_code_review_holistic_criteria(repo_path: str | None = None) -> str:
    """Return holistic-lens review criteria (issue #2126).

    The shared file inherits from ``code-review-criteria.md`` and adds
    holistic-lens rules (end-to-end use-case walk, doc↔code symmetry,
    synthetic-key / sentinel cross-module audit, silent-fallback hunt).
    """
    content = _read_shared_criteria(
        "code-review-holistic-criteria.md",
        user_override="code-review-holistic-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    logger.warning("Shared code-review-holistic-criteria.md not found, using inline fallback")
    return (
        "Inherits from `code-review-criteria.md`; only holistic-lens rules "
        "below override or extend it.\n\n"
        "### Holistic lens (focus areas)\n"
        "- Walk the primary advertised use case end-to-end across the "
        "full diff. NACK silent dead-ends like the `__checkout__` bug "
        "on PR #2105.\n"
        "- Cross-check doc-claimed behaviour against what the code does. "
        "NACK doc-claimed inference / migration paths that do not exist.\n"
        "- Audit synthetic keys, sentinels, and magic values for "
        "cross-module agreement.\n"
        "- Hunt silent fallbacks that swallow operator-visible "
        "misconfiguration.\n"
        "- Defer line-by-line correctness to `reviewer_code`.\n"
    )


def _get_concurrency_review_criteria(repo_path: str | None = None) -> str:
    """Return concurrency-lens review criteria (issue #1965).

    The shared file inherits from ``code-review-criteria.md`` and adds
    lens-specific rules (race conditions, deadlocks, shared-state
    mutation, async-context leakage, retry storms, resource-cleanup
    ordering, BRC-protocol invariants).
    """
    content = _read_shared_criteria(
        "concurrency-review-criteria.md",
        user_override="concurrency-review-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    logger.warning("Shared concurrency-review-criteria.md not found, using inline fallback")
    return (
        "Inherits from `code-review-criteria.md`; only lens-specific rules "
        "below override or extend it.\n\n"
        "### Concurrency lens (focus areas)\n"
        "- Race conditions and deadlocks.\n"
        "- Shared-state mutation without proper synchronization.\n"
        "- Async-context leakage and retry-storm patterns.\n"
        "- Resource-cleanup ordering bugs.\n"
        "- BRC-protocol invariants (send→wait ordering, cursor threading "
        "per #1925, heartbeat-stall windows per #2012).\n"
    )


def _get_review_criteria_for_type(
    reviewer_type: str, phase: str, repo_path: str | None = None
) -> str:
    """Dispatch to the correct criteria function based on reviewer type."""
    if reviewer_type == "agent-design":
        return _get_agent_design_criteria()
    elif reviewer_type == "code":
        return _get_code_review_criteria(repo_path=repo_path)
    elif reviewer_type == "code-holistic":
        return _get_code_review_holistic_criteria(repo_path=repo_path)
    elif reviewer_type == "contract":
        return _get_contract_review_criteria(repo_path=repo_path)
    elif reviewer_type == "refine":
        return _get_refine_review_criteria()
    elif reviewer_type == "plan":
        return _get_plan_review_criteria()
    elif reviewer_type == "security":
        return _get_security_review_criteria(repo_path=repo_path)
    elif reviewer_type == "concurrency":
        return _get_concurrency_review_criteria(repo_path=repo_path)
    else:
        raise ValueError(f"Unknown reviewer type: {reviewer_type}")


def _get_reviewer_scope_preamble(reviewer_type: str, phase: str) -> str:
    """Return a scope preamble that tells the reviewer what to focus on."""
    if reviewer_type == "agent-design":
        return (
            "This is a specialized **agent-mode design review**. Focus ONLY on "
            "agent-mode design principles. Do NOT review general code quality, "
            "security, or correctness — other reviewers handle those.\n\n"
            "**Only flag issues if you find clear agent-mode design anti-patterns.** "
            "If the output has no agent-mode concerns, a brief approval is acceptable "
            "— you do not need to produce a lengthy analysis when there are no concerns."
        )
    elif reviewer_type == "code":
        return (
            "This is a **comprehensive code review**. Focus on security, correctness, "
            "and robustness. Agent-mode design alignment is handled by another reviewer.\n\n"
            "**Be direct.** Do not soften feedback. State issues clearly and explain "
            "why they matter.\n\n"
            "**Be thorough.** Find ALL issues on the first pass. Do not stop after "
            "identifying a few problems.\n\n"
            "**Analysis format:** Provide file-by-file analysis covering each changed "
            "file. For each file, note what changed, whether the change is correct, "
            "and any issues or observations."
        )
    elif reviewer_type == "code-holistic":
        return (
            "This is a CRITICAL **holistic code review** (issue #2126). "
            "You run alongside `reviewer_code` — your job is the "
            "cross-module coherence question line-by-line review does not "
            "own. **Don't verify every line; `reviewer_code` covers "
            "that.**\n\n"
            "**Lens scope:** read the diff once with the whole PR in mind, "
            "then run all four passes from the criteria below: (1) walk "
            "the primary advertised use case end-to-end (the `__checkout__` "
            "dead-end on PR #2105 is the canonical miss); (2) check that "
            "every doc-claimed behaviour is actually implemented and every "
            "user-facing code path is documented; (3) confirm synthetic "
            "keys / sentinels / magic values are recognised by every "
            "consumer in another module; (4) hunt silent fallbacks "
            "(`except Exception:`, swallowed `None`s, default no-op "
            "branches) where the operator would expect a signal.\n\n"
            "**Distinct CRITICAL role.** Your NACK gates consensus on its "
            "own — it is not averaged against `reviewer_code`'s "
            "verdict. If the architectural-coherence question fails, "
            "NACK even when the line-by-line review is clean.\n\n"
            "**Analysis format:** Name the pass that found the issue, the "
            "producer / consumer modules the asymmetry spans, and the "
            "user-visible failure shape. If all four passes come back "
            "clean a concise ACK is acceptable, but the BRC bus enforces "
            "a minimum content length on ACK / NACK bodies, so write at "
            "least a sentence or two summarising what you checked."
        )
    elif reviewer_type == "contract":
        return (
            "This is a **contract verification review**. Verify that the implementation "
            "matches the contract and all acceptance criteria are met. Do NOT review "
            "general code quality or security — other reviewers handle those.\n\n"
            "**Analysis format:** Provide a criterion-by-criterion verification — for each "
            "acceptance criterion, state whether it is met and cite the specific evidence."
        )
    elif reviewer_type == "refine":
        return (
            "This is a **refine phase review**. Focus on the quality and completeness "
            "of the analysis produced during the refine phase. Evaluate problem "
            "understanding, codebase research, options analysis, and the recommended "
            "approach. Agent-mode design alignment is handled by another reviewer.\n\n"
            "**Analysis format:** Provide section-by-section evaluation of the refine "
            "output — assess each major section for depth, accuracy, and completeness."
        )
    elif reviewer_type == "plan":
        return (
            "This is a **plan phase review**. Focus on the quality and completeness "
            "of the implementation plan. Evaluate task breakdown, acceptance criteria, "
            "dependency ordering, risk assessment, and test strategy. Agent-mode "
            "design alignment is handled by another reviewer.\n\n"
            "**Analysis format:** Provide section-by-section evaluation of the plan — "
            "assess task decomposition, acceptance criteria quality, dependency ordering, "
            "and risk coverage."
        )
    elif reviewer_type == "security":
        return (
            "This is a CRITICAL **security-lens review** (issue #2139). "
            "A NACK from this lens blocks consensus until the producer "
            "re-proposes. Focus ONLY on the security lens; defer code "
            "quality, performance, and non-security findings to "
            "`reviewer_code`.\n\n"
            "**Lens scope:** cross-file allowlist mismatches, "
            "handler-vs-validator path mismatches, information-disclosure / "
            "authorization-bypass patterns at trust boundaries, "
            "uncommitted-artifact / Dockerfile-symlink mismatches, secret "
            "leakage, and OWASP top-10 patterns that span more than one "
            "changed file. Be especially alert to allowlist-mismatch "
            "patterns where a handler in one file accepts traffic that a "
            "validator in another file was supposed to reject.\n\n"
            "**Analysis format:** Provide a finding-by-finding lens report. "
            "If the diff has no security concerns, a concise approval is "
            "acceptable — verbose reports without findings are not required, "
            "but the BRC bus enforces a minimum content length on ACK / "
            "NACK bodies, so write at least a sentence or two summarizing "
            'what you checked (not a single-word "LGTM").'
        )
    elif reviewer_type == "concurrency":
        return (
            "This is a CRITICAL **concurrency-lens review** (issue #2139). "
            "A NACK from this lens blocks consensus until the producer "
            "re-proposes. Focus ONLY on the concurrency lens; defer code "
            "quality, performance, and non-concurrency findings to "
            "`reviewer_code`.\n\n"
            "**Lens scope:** race conditions, deadlocks, shared-state "
            "mutation without synchronization, async-context leakage, "
            "retry-storm patterns, resource-cleanup ordering bugs, and "
            "BRC-protocol invariants (send→wait ordering, cursor "
            "threading per #1925, heartbeat-stall windows per #2012).\n\n"
            "**Analysis format:** Provide a finding-by-finding lens report. "
            "If the diff has no concurrency concerns, a concise approval is "
            "acceptable — verbose reports without findings are not required, "
            "but the BRC bus enforces a minimum content length on ACK / "
            "NACK bodies, so write at least a sentence or two summarizing "
            'what you checked (not a single-word "LGTM").'
        )
    else:
        raise ValueError(f"Unknown reviewer type: {reviewer_type}")


def _verdict_path_for_type(
    phase: str,
    reviewer_type: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> str:
    """Return the relative verdict file path for a given reviewer type.

    Uses issue_number as prefix when available, otherwise pipeline_id.
    """
    prefix = _pipeline_identifier(issue_number, pipeline_id or "unknown")
    return f".egg-state/reviews/{prefix}-{phase}-{reviewer_type}-review.json"


def _draft_filename(phase: str) -> str | None:
    """Return the draft filename for a phase, without any prefix.

    Centralises the phase-to-filename mapping so that
    ``_get_draft_path`` and ``_get_generic_draft_path`` stay in sync.
    """
    if phase == "refine":
        return "analysis.md"
    elif phase == "implement":
        return None
    else:
        return f"{phase}.md"


def _get_draft_path(
    phase: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
    mode: PipelineMode | None = None,
) -> str | None:
    """Return relative path to the draft file for a phase.

    Uses issue_number as prefix when available, otherwise pipeline_id.
    For CUSTOM-mode pipelines (#1762) always keys on pipeline_id so the
    file does not collide with a concurrent ISSUE-mode pipeline sharing
    the same ``issue_number``.
    """
    filename = _draft_filename(phase)
    if not filename:
        return None
    prefix = _pipeline_identifier(issue_number, pipeline_id or "unknown", mode=mode)
    return f".egg-state/drafts/{prefix}-{filename}"


def _cleanup_stale_generic_drafts(worktree_path: Path) -> bool:
    """Remove unprefixed generic draft files from a worktree.

    Legacy pipelines left behind ``analysis.md`` and ``plan.md`` (without
    an issue-number or pipeline-id prefix) in ``.egg-state/drafts/``.
    These stale files can confuse downstream draft-reading logic.  This
    helper deletes only the exact unprefixed filenames; prefixed files
    (e.g. ``1553-analysis.md``) are left untouched.

    Uses ``git rm`` so the deletions are staged and can be committed
    immediately.  Falls back to ``os.unlink`` if the file is untracked.

    Safe to call when the drafts directory does not exist (no-op).

    Returns ``True`` if a commit was made (i.e. tracked files were removed
    and committed), ``False`` otherwise.
    """
    drafts_dir = worktree_path / ".egg-state" / "drafts"
    if not drafts_dir.is_dir():
        return False

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_path}",
        "-C",
        str(worktree_path),
    ]
    removed = False

    stale_names = ("analysis.md", "plan.md")
    for name in stale_names:
        stale = drafts_dir / name
        if stale.exists():
            logger.info(
                "Removing stale generic draft",
                path=str(stale),
            )
            try:
                subprocess.run(
                    [*git_base, "rm", "-f", str(stale.relative_to(worktree_path))],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                removed = True
            except subprocess.CalledProcessError as exc:
                # File may be untracked — just delete it from disk.
                # Warn so that unexpected git rm failures (e.g. index
                # lock) are diagnosable.
                logger.warning(
                    "git rm failed for stale draft, falling back to unlink",
                    path=str(stale),
                    error=str(exc),
                )
                stale.unlink(missing_ok=True)

    if removed:
        try:
            subprocess.run(
                [
                    *git_base,
                    "commit",
                    "--no-verify",
                    "-m",
                    "Remove stale generic draft files",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            return True
        except subprocess.CalledProcessError as commit_err:
            logger.debug(
                "No changes to commit after stale draft cleanup",
                error=str(commit_err),
            )

    return False


def _get_generic_draft_path(phase: str) -> str | None:
    """Return the generic (unprefixed) draft path for a phase.

    Used as a fallback when the issue-specific draft file is missing.
    """
    filename = _draft_filename(phase)
    if not filename:
        return None
    return f".egg-state/drafts/{filename}"


def _git_show_draft(
    repo_path: Path,
    branch: str,
    rel_path: str,
    timeout: int = 15,
) -> str | None:
    """Read a file from ``origin/{branch}`` via ``git show``.

    Returns the file content as a string, or ``None`` if the file does
    not exist on the remote ref or the git command fails.  This is a
    read-only operation that does not modify the worktree.

    Note: this function does **not** ``git fetch`` itself.  The caller is
    responsible for ensuring ``origin/{branch}`` is fresh (e.g., by
    running ``git fetch origin {branch}`` before calling this helper).
    """
    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={repo_path}",
        "-C",
        str(repo_path),
    ]
    try:
        result = subprocess.run(
            [*git_base, "show", f"origin/{branch}:{rel_path}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        if result.returncode != 0:
            logger.debug(
                "git show returned non-zero",
                branch=branch,
                rel_path=rel_path,
                returncode=result.returncode,
                stderr=result.stderr.strip()[:200],
            )
    except Exception as exc:
        logger.debug(
            "git show failed for draft",
            branch=branch,
            rel_path=rel_path,
            error=str(exc),
        )
    return None


def _read_source_branch_artifacts(
    repo_path: Path,
    source_branch: str,
    issue_number: int | None,
    pipeline_id: str,
    store: Any,
    pipeline: Any,
    source_artifact_prefix: str | None = None,
    spawner: Any | None = None,
    gateway_mode: str = "public",
) -> bool:
    """Read plan and analysis artifacts from a source branch.

    Reads draft files from ``origin/<source_branch>`` via ``git show``.
    Only populates ``pipeline.plan`` and ``pipeline.analysis`` when they
    are not already set (inline values take precedence).

    Prefix resolution order for the exact-path lookup:

    1. ``source_artifact_prefix`` (explicit override, e.g. ``"issue-1570-v3"``)
    2. ``pipeline_id`` (includes qualifier, e.g. ``"issue-1570-v7"``)
    3. ``issue_number`` (bare issue number, e.g. ``1570``)

    Falls back to listing available files via ``git ls-tree`` when none
    of the prefixes match.

    Args:
        repo_path: Path to the repository (worktree or main).
        source_branch: Branch name to read artifacts from.
        issue_number: Pipeline issue number (for deriving prefix).
        pipeline_id: Pipeline ID (includes qualifier when present).
        store: StateStore instance for saving updated pipeline.
        pipeline: Pipeline model instance to populate.
        source_artifact_prefix: Explicit prefix override for draft
            filenames on the source branch (e.g. ``"issue-1570-v3"``).
            When set, only this prefix is tried before the ls-tree
            fallback.
        spawner: ContainerSpawner instance for gateway-authenticated git
            operations.  When provided, the fetch uses the gateway API
            (which injects GitHub credentials) instead of a raw
            ``git fetch`` that lacks auth in the sandboxed environment.
        gateway_mode: Network mode for the gateway session (``"public"``
            or ``"private"``).

    Returns:
        True if any artifacts were read, False otherwise.
    """
    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={repo_path}",
        "-C",
        str(repo_path),
    ]
    # Bare prefix is the issue number when available — used as a fallback
    # after the full pipeline_id prefix.  Do NOT use _pipeline_identifier()
    # here because it returns pipeline_id for CUSTOM-mode pipelines,
    # which defeats the fallback chain (pipeline_id → bare issue number).
    bare_prefix: int | str = issue_number if issue_number is not None else pipeline_id
    updated = False

    # Fetch the source branch so origin/{source_branch} is up-to-date.
    # Without this, git show fails because the remote ref isn't cached
    # locally.  Use the gateway-authenticated fetch when available —
    # raw git commands in the sandboxed environment lack GitHub
    # credentials (the gateway sidecar injects them).
    if spawner is not None:
        try:
            spawner.gateway.fetch_branch(
                pipeline_id=pipeline_id,
                repo_path=str(repo_path),
                args=[source_branch],
                mode=gateway_mode,
            )
        except Exception:
            logger.warning(
                "Gateway fetch of source branch failed (will try git show anyway)",
                source_branch=source_branch,
                pipeline_id=pipeline_id,
                exc_info=True,
            )
    else:
        # Fallback for tests or environments without a gateway.
        try:
            subprocess.run(
                [*git_base, "fetch", "origin", source_branch],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception:
            logger.debug(
                "Failed to fetch source branch (will try git show anyway)",
                source_branch=source_branch,
                exc_info=True,
            )

    # Build ordered list of prefixes to try.  Duplicates are removed so
    # we don't hit git show twice for the same path.
    if source_artifact_prefix is not None:
        # Explicit override — try only this prefix before ls-tree fallback.
        prefixes: list[str | int] = [source_artifact_prefix]
    else:
        # Default: try pipeline_id first (includes qualifier), then bare
        # issue number.  When pipeline_id == bare_prefix (e.g. no qualifier
        # and no issue number), the dedup below collapses them.
        prefixes = []
        if pipeline_id and str(pipeline_id) != str(bare_prefix):
            prefixes.append(pipeline_id)
        prefixes.append(bare_prefix)

    for field_name, suffix in [("analysis", "-analysis.md"), ("plan", "-plan.md")]:
        # Skip if already populated (inline values take precedence).
        # Use ``is not None`` so empty strings are not silently overwritten.
        if getattr(pipeline, field_name) is not None:
            continue

        drafts_prefix = ".egg-state/drafts/"
        content = None

        # Try each prefix in order (exact path lookup).
        for pfx in prefixes:
            expected_path = f"{drafts_prefix}{pfx}{suffix}"
            content = _git_show_draft(repo_path, source_branch, expected_path)
            if content:
                logger.info(
                    "Read artifact from source branch (exact prefix)",
                    field=field_name,
                    source_branch=source_branch,
                    path=expected_path,
                )
                break

        if content is None:
            # Fallback: list available files and find a match
            try:
                result = subprocess.run(
                    [
                        *git_base,
                        "ls-tree",
                        "--name-only",
                        f"origin/{source_branch}:{drafts_prefix.rstrip('/')}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    matches = [f for f in result.stdout.strip().splitlines() if f.endswith(suffix)]
                    # Filter by issue number to avoid picking up artifacts
                    # from other issues on the same branch (#1654).
                    if issue_number is not None:
                        issue_matches = [f for f in matches if f.startswith(f"{issue_number}-")]
                        if issue_matches:
                            matches = issue_matches
                        else:
                            logger.warning(
                                "No fallback match for issue number — skipping",
                                field=field_name,
                                issue_number=issue_number,
                                source_branch=source_branch,
                                available=matches,
                            )
                            continue
                    if len(matches) > 1:
                        logger.warning(
                            "Multiple fallback matches for artifact — using first",
                            field=field_name,
                            source_branch=source_branch,
                            matches=matches,
                        )
                    for filename in matches:
                        fallback_path = f"{drafts_prefix}{filename}"
                        content = _git_show_draft(repo_path, source_branch, fallback_path)
                        if content:
                            logger.info(
                                "Read artifact from source branch via fallback",
                                field=field_name,
                                source_branch=source_branch,
                                path=fallback_path,
                            )
                            break
            except Exception as exc:
                logger.debug(
                    "git ls-tree failed for source branch drafts",
                    source_branch=source_branch,
                    error=str(exc),
                )

        if content:
            setattr(pipeline, field_name, content)
            updated = True
            logger.info(
                "Read artifact from source branch",
                field=field_name,
                source_branch=source_branch,
                pipeline_id=pipeline_id,
                length=len(content),
            )

    if updated:
        # Clear source_branch after successful read to avoid re-reading on
        # pipeline restart (same pattern as plan/analysis clearing after
        # draft files are pushed).
        pipeline.source_branch = None
        pipeline.source_artifact_prefix = None
        store.save_pipeline(
            pipeline, message=f"Populate artifacts from source branch {source_branch}"
        )
    else:
        logger.warning(
            "No artifacts found on source branch",
            source_branch=source_branch,
            pipeline_id=pipeline_id,
            source_artifact_prefix=source_artifact_prefix,
        )

    return updated


def _pull_contract_from_source_branch(
    repo_path: Path,
    source_branch: str,
    issue_number: int | None,
    pipeline_id: str,
    spawner: Any | None = None,
    gateway_mode: str = "public",
) -> bool:
    """Load a persisted contract from ``origin/<source_branch>`` into the worktree.

    When ``submit_task`` is called with ``source_branch``, the source branch
    carries ``.egg-state/contracts/<pipeline>.json`` (with any resolved HITL
    decisions).  Without this helper, ``_run_pipeline`` calls
    ``create_contract()`` unconditionally and overwrites those decisions with
    a zero-state contract (#2035).  This helper fetches the source branch,
    reads the contract via ``git show``, rebinds its pipeline_id to the new
    pipeline, and writes it into the worktree so the caller can skip
    ``create_contract()`` and proceed to commit+push the pulled contract.

    Returns True when a contract was successfully pulled, False otherwise.
    Best-effort: missing, invalid, or unreachable source contracts all yield
    False so the caller falls back to ``create_contract()``.
    """
    from egg_contracts.loader import (
        ContractNotFoundError,
        ContractValidationError,
        load_contract_from_branch,
        save_contract,
    )

    # Fetch the source branch so origin/<source_branch> is current.  Mirrors
    # the pattern in _read_source_branch_artifacts — use the gateway when
    # available, fall back to raw git for tests / non-sandboxed callers.
    if spawner is not None:
        try:
            spawner.gateway.fetch_branch(
                pipeline_id=pipeline_id,
                repo_path=str(repo_path),
                args=[source_branch],
                mode=gateway_mode,
            )
        except Exception:
            logger.warning(
                "Gateway fetch of source branch failed (will try git show anyway)",
                source_branch=source_branch,
                pipeline_id=pipeline_id,
                exc_info=True,
            )
    else:
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    f"safe.directory={repo_path}",
                    "-C",
                    str(repo_path),
                    "fetch",
                    "origin",
                    source_branch,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception:
            logger.debug(
                "Failed to fetch source branch for contract pull",
                source_branch=source_branch,
                exc_info=True,
            )

    identifier: int | str = issue_number if issue_number is not None else pipeline_id

    try:
        contract = load_contract_from_branch(
            identifier,
            repo_path,
            branch=f"origin/{source_branch}",
        )
    except ContractNotFoundError:
        logger.debug(
            "No contract on source branch",
            pipeline_id=pipeline_id,
            source_branch=source_branch,
        )
        return False
    except ContractValidationError as e:
        logger.warning(
            "Contract on source branch failed validation, falling back to fresh contract",
            pipeline_id=pipeline_id,
            source_branch=source_branch,
            error=str(e),
        )
        return False
    except Exception:
        logger.warning(
            "Failed to load contract from source branch",
            pipeline_id=pipeline_id,
            source_branch=source_branch,
            exc_info=True,
        )
        return False

    # Rebind to the new pipeline_id so save_contract writes under the new
    # canonical key when the pipeline was forked with a qualifier
    # (e.g. source=issue-1965, new=issue-1965-v2).
    contract.pipeline_id = pipeline_id
    save_contract(contract, repo_path)

    logger.info(
        "Loaded contract from source branch",
        pipeline_id=pipeline_id,
        source_branch=source_branch,
        decision_count=len(contract.decisions),
        phase_count=len(contract.slices),
    )
    return True


def _read_phase_draft(
    repo_path: Path,
    phase: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
    max_chars: int = 32000,
    branch: str | None = None,
) -> str | None:
    """Read draft file contents. Truncates at max_chars.

    Returns None when the draft cannot be found (no path configured or
    file missing on disk).

    Attempts in order:

    1. Primary (issue-specific) path on disk
    2. Generic (unprefixed) path on disk
    3. Primary path via ``git show origin/{branch}:``
    4. Generic path via ``git show origin/{branch}:``

    The ``git show`` fallback (steps 3–4) handles cases where
    ``_sync_worktree_with_remote`` failed silently and the draft exists
    on the remote branch but not in the local checkout.
    """
    draft_rel = _get_draft_path(phase, issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        return None

    def _truncate(content: str) -> str:
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n... (truncated, {len(content)} chars total)"
        return content

    draft_path = repo_path / draft_rel
    generic_rel = _get_generic_draft_path(phase)

    # Try primary (issue-specific) path first.
    if draft_path.exists():
        return _truncate(draft_path.read_text(encoding="utf-8"))

    logger.debug(
        "Draft file not found",
        path=str(draft_path),
        phase=phase,
        issue_number=issue_number,
        pipeline_id=pipeline_id,
    )

    # Fallback: try the generic (unprefixed) path on disk.
    if generic_rel:
        generic_path = repo_path / generic_rel
        if generic_path.exists():
            logger.debug(
                "Using generic fallback draft path",
                primary_path=str(draft_path),
                fallback_path=str(generic_path),
                phase=phase,
            )
            return _truncate(generic_path.read_text(encoding="utf-8"))

    # Fallback: try reading from remote tracking ref via git show.
    # This handles cases where _sync_worktree_with_remote() failed
    # silently (fetch failure, detached HEAD, divergence, etc.) and
    # the draft exists on origin but not in the local checkout.
    if branch:
        content = _git_show_draft(repo_path, branch, draft_rel)
        if content is None and generic_rel:
            content = _git_show_draft(repo_path, branch, generic_rel)
        if content is not None:
            logger.info(
                "Read draft from remote tracking ref (local copy missing)",
                phase=phase,
                branch=branch,
            )
            return _truncate(content)

    return None


def _summarize_issue(prompt: str | None, issue_number: int | None = None) -> str:
    """Extract a 1-2 sentence summary from the issue title and first paragraph.

    Used to give execution agents (tester, documenter) a brief
    orientation without embedding the full issue body. Analysis agents
    (architect, task_planner, risk_analyst) still receive the full issue.

    Extracts the first markdown heading (or first non-empty line) as the title,
    then the first paragraph as supporting context.
    """
    if not prompt or not prompt.strip():
        return f"Working on issue #{issue_number}." if issue_number else ""

    lines = prompt.strip().splitlines()

    # Extract title: first markdown heading, or first non-empty line
    title = ""
    body_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            title = s.lstrip("# ").strip()
        else:
            title = s
        body_start = i + 1
        break

    # Extract first paragraph after title (up to ~300 chars)
    first_para_lines: list[str] = []
    for line in lines[body_start:]:
        s = line.strip()
        if not s:
            if first_para_lines:
                break
            continue
        first_para_lines.append(s)

    first_para = " ".join(first_para_lines)
    if len(first_para) > 300:
        first_para = first_para[:297] + "..."

    # Build summary
    issue_ref = f" (issue #{issue_number})" if issue_number else ""
    summary = f"**Background**: {title}{issue_ref}"
    if first_para:
        summary += f"\n\n{first_para}"

    return summary


def _extract_plan_overview(plan_text: str) -> str:
    """Extract the plan overview section (before individual phase details).

    Returns the summary/overview portion of the plan, stopping before
    individual phase task listings (### Phase N: ...) and the yaml-tasks
    appendix. This gives the coder high-level context without the full plan.
    """
    lines = plan_text.splitlines()
    overview_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Stop at individual phase headings
        if stripped.startswith("### Phase ") or stripped.startswith("### phase-"):
            break
        # Stop at the yaml-tasks appendix
        if "yaml-tasks" in stripped:
            break
        # Stop at structured task appendix
        if stripped.startswith("## Structured Task Appendix"):
            break
        # Stop at issue-to-task mapping (detailed reference section)
        if stripped.startswith("## Issue-to-Task Mapping"):
            break
        overview_lines.append(line)

    # Trim trailing blank lines
    while overview_lines and not overview_lines[-1].strip():
        overview_lines.pop()

    return "\n".join(overview_lines)


def _build_role_context(
    role_value: str,
    prompt: str | None,
    issue_number: int | None = None,
    phase_obj=None,
    all_phases=None,
    base_branch: str | None = None,
) -> str:
    """Build role-appropriate context to replace raw issue body embedding.

    Analysis roles (architect, task_planner, risk_analyst) receive the full
    issue body since they need it for problem understanding and planning.

    Execution roles (tester, documenter) receive a brief summary
    with structured task information and pointers to full context.

    Args:
        role_value: Agent role string
        prompt: Original task prompt (full issue body)
        issue_number: GitHub issue number
        phase_obj: Current plan phase object (phase context)
        all_phases: All contract phases (phase context)

    Returns:
        Role-appropriate context string to embed in the agent prompt
    """
    from egg_contracts.agent_roles import EXECUTION_ROLE_VALUES

    # Analysis roles need the full issue body for problem understanding
    if role_value in ("architect", "task_planner", "risk_analyst"):
        if prompt:
            return f"## Task Description\n\n{prompt}\n"
        return ""

    lines: list[str] = []

    # Brief summary for execution roles
    summary = _summarize_issue(prompt, issue_number)
    if summary:
        lines.append(f"## Background\n\n{summary}\n")

    # Phase-specific context
    if phase_obj is not None:
        lines.append(f"## Phase Scope: {phase_obj.name} ({phase_obj.id})\n")

        if role_value == "tester":
            lines.append(
                f"Focus your testing on code changed in plan phase `{phase_obj.id}`. "
                "The following tasks were implemented in this phase:\n"
            )
        elif role_value == "documenter":
            lines.append(
                f"Focus your documentation on changes from plan phase `{phase_obj.id}`. "
                "The following tasks were implemented in this phase:\n"
            )
        else:
            lines.append("The following tasks were implemented in this phase:\n")

        # Filter tasks by role for execution agents.
        # Only apply role-based filtering when at least one task has a role
        # assigned — legacy plans (all role=None) show all tasks to all agents,
        # preserving backward compatibility.
        _has_any_role = any(t.role is not None for t in phase_obj.tasks)
        if role_value in EXECUTION_ROLE_VALUES and _has_any_role:
            # Unassigned tasks (role=None) default to coder.
            filtered_tasks = [
                task
                for task in phase_obj.tasks
                if task.role == role_value or (task.role is None and role_value == "coder")
            ]
        else:
            filtered_tasks = list(phase_obj.tasks)

        for task in filtered_tasks:
            lines.append(f"- **{task.id}**: {task.description}")
            if getattr(task, "acceptance_criteria", None):
                lines.append(f"  - Acceptance: {task.acceptance_criteria}")
            if getattr(task, "files_affected", None):
                lines.append(f"  - Files: {', '.join(task.files_affected)}")
        lines.append("")

    if all_phases and phase_obj is not None and role_value in ("tester", "documenter"):
        # Brief orientation about other phases for context
        other_phases = [p for p in all_phases if p.id != phase_obj.id]
        if other_phases:
            lines.append("### Other Phases (for orientation)\n")
            for phase in other_phases:
                status = getattr(phase, "status", "unknown")
                lines.append(f"- {phase.id}: {phase.name} [{status}]")
            lines.append("")

    # Context pointers — agents can get more detail on demand
    lines.append("## For More Context\n")
    if issue_number:
        lines.append(f"- Full issue: `gh issue view {issue_number}`")
    _rc_base_ref = _resolve_origin_ref(base_branch)
    lines.append(f"- Changed files: `git diff {_rc_base_ref}...HEAD` or check handoff data")
    lines.append("- Coder output: check `EGG_HANDOFF_DATA` environment variable")
    lines.append(
        "- Prior agent sessions: `egg-checkpoint context --pipeline $EGG_PIPELINE_ID` "
        "(see checkpoint rule for details)"
    )
    lines.append("")

    return "\n".join(lines)


def _build_role_restrictions_section() -> str:
    """Build a prompt section describing file access restrictions per execution role.

    This section is injected into the task_planner prompt so that it can
    assign each task to the correct execution role (coder, tester, documenter)
    based on which files the task will modify.

    Returns:
        Formatted markdown string describing role file boundaries.
    """
    from egg_contracts.agent_roles import get_file_patterns

    lines: list[str] = [
        "## Execution Role File Restrictions",
        "",
        "Each task should include a `role` field (coder, tester, or documenter) "
        "indicating which agent should execute it. Assign roles based on the file "
        "access restrictions below. Tasks without a `role` field default to coder.",
        "",
    ]

    for role_name in ("coder", "tester", "documenter"):
        patterns = get_file_patterns(role_name)
        if patterns is None:
            continue
        lines.append(f"### {role_name}")
        if patterns.get("allowed"):
            lines.append(f"- **Allowed**: {', '.join(f'`{p}`' for p in patterns['allowed'])}")
        if patterns.get("blocked"):
            lines.append(f"- **Blocked**: {', '.join(f'`{p}`' for p in patterns['blocked'])}")
        lines.append("")

    lines.append(
        "Assign `role: tester` to tasks that only touch test files, "
        "`role: documenter` to tasks that only touch docs/README files, "
        "and `role: coder` (or omit the field) for everything else. "
        "If a task spans multiple roles, split it into separate tasks per role."
    )
    lines.append("")

    # Staging-dir convention for `.github/` (issue #2508).
    lines.append("### `.github/` changes — use the `.github-staging/` convention")
    lines.append("")
    lines.append(
        "Every producer role is blocked from writing under `.github/` "
        "(CI workflows, CODEOWNERS, dependabot config) — this is a "
        "branch-protection invariant, not a planner mistake. Tasks that "
        "need to modify those files must instead write the proposed "
        "end-state to top-level `.github-staging/`, mirroring the "
        "`.github/` structure (e.g. a proposed change to "
        "`.github/workflows/ci.yml` is staged at "
        "`.github-staging/workflows/ci.yml`). The orchestrator's PR "
        "builder auto-detects `.github-staging/` and emits a manual "
        "step asking the human reviewer to move the staged files into "
        "place before merge. Assign such tasks to `role: coder` and "
        "make the staging path explicit in the task's "
        "`files_affected`. `.github-staging/` must remain tracked by "
        "git (do not add it to `.gitignore`); otherwise the staged "
        "files won't be in the PR commit and the reviewer's `git mv` "
        "will fail."
    )
    lines.append("")

    # Runtime escape hatch (issue #2529).
    lines.append("### Impossible task? Use the runtime escape hatch — DO NOT invent workarounds")
    lines.append("")
    lines.append(
        "If you discover mid-execution that the task you've been "
        "assigned is structurally impossible (file restrictions block "
        "your role, the plan is buggy, an external dependency is "
        "missing), STOP. Do not invent a workaround like staging the "
        "files in another directory or asking another agent to do it "
        "via a freeform handoff document — past pipelines (#2474, "
        "#2529) wasted ~10+ min and triggered downstream NACKs that "
        "way."
    )
    lines.append("")
    lines.append("Instead, use the two MCP tools:")
    lines.append("")
    lines.append(
        '1. `mcp__sdlc__check_file_restriction({path: "..."})` — '
        "cheap pure-local read against `shared/egg_restrictions/"
        "patterns.py`. Confirms whether your role can write the path "
        "and returns `alternative_role` (the producer role that "
        "*can* write it, when exactly one covers it). Call this "
        "BEFORE exploring a file you suspect is outside your "
        "boundary."
    )
    lines.append("")
    lines.append(
        "2. `mcp__sdlc__report_impasse({category, reason, "
        "suggested_role, blocked_files})` — emits a typed Impasse "
        "signal and exits cleanly. The orchestrator detects the "
        "impasse post-phase and either delegates to "
        "``suggested_role`` (first attempt) or escalates to HITL "
        "(second attempt or no eligible role). Categories: "
        "``wrong_role`` (file restrictions; auto-delegateable), "
        "``plan_bug`` / ``external_blocker`` / ``unknown`` (always "
        "HITL). Once you've called this tool, do NOT commit code or "
        "call any other producer tool — just exit."
    )
    lines.append("")

    return "\n".join(lines)


def _render_contract_tasks(
    repo_path: str,
    pipeline_id: str,
    pipeline_mode: str,
    issue_number: int | None = None,
) -> str | None:
    """Load contract and render tasks as a markdown checklist.

    Returns None if the contract cannot be loaded.
    """
    try:
        from egg_contracts.loader import load_contract
        from egg_contracts.models import TaskStatus
    except ImportError:
        return None

    # Contracts are keyed by pipeline_id (loader's compat shim handles
    # legacy paths for in-flight pipelines that predate key unification).
    try:
        contract = load_contract(pipeline_id, Path(repo_path))
    except Exception:
        return None

    if not contract.slices:
        return None

    lines = ["## Contract Tasks\n"]
    for slice_ in contract.slices:
        if not slice_.tasks:
            continue
        lines.append(f"### {slice_.name}\n")
        for task in slice_.tasks:
            check = "x" if task.status == TaskStatus.COMPLETE else " "
            lines.append(f"- [{check}] **{task.id}**: {task.description}")
            if task.acceptance_criteria:
                lines.append(f"  - Acceptance: {task.acceptance_criteria}")
            if task.files_affected:
                lines.append(f"  - Files: {', '.join(task.files_affected)}")
        lines.append("")

    return "\n".join(lines) if len(lines) > 1 else None


def _build_review_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    reviewer_type: str = "code",
    issue_number: int | None = None,
    review_cycle: int = 1,
    prior_feedback: str | None = None,
    repo_path: str | None = None,
    last_reviewed_commit: str | None = None,
    base_branch: str | None = None,
    concurrent: bool = False,
) -> str:
    """Build a review prompt for the reviewer agent.

    In sequential mode, tells the reviewer to write a typed verdict JSON
    file to .egg-state/reviews/.  In concurrent (BRC) mode, the reviewer's
    ACK/NACK reason IS the review output — no verdict file is written.
    """
    draft_path = _get_draft_path(phase, issue_number=issue_number, pipeline_id=pipeline_id)

    verdict_path: str | None = None
    if not concurrent:
        verdict_path = _verdict_path_for_type(
            phase,
            reviewer_type,
            issue_number=issue_number,
            pipeline_id=pipeline_id,
        )

    lines = [
        f"You are reviewing the **{phase}** phase output of the SDLC pipeline "
        f"({reviewer_type} reviewer).\n",
        "## Scope\n",
        _get_reviewer_scope_preamble(reviewer_type, phase),
        "",
        "## Context\n",
        f"Pipeline ID: {pipeline_id}",
        f"Phase: {phase}",
        f"Reviewer: {reviewer_type}",
        f"Review cycle: {review_cycle}",
        "",
        "## Your Task\n",
    ]

    # Delta review: for re-reviews with a known last-reviewed commit,
    # instruct the reviewer to focus on the delta.
    #
    # Two-dot `git diff A..HEAD` would wrongly include any base-branch merges
    # landed between A and HEAD. `git log A..HEAD --not origin/<base> -p`
    # explicitly excludes commits reachable from the base branch, so the
    # reviewer sees only PR-authored work (issue #1758).
    is_delta_review = review_cycle > 1 and last_reviewed_commit and not draft_path
    _base_ref = _resolve_origin_ref(base_branch)
    _delta_base_branch = _base_ref.removeprefix("origin/")
    diff_command = (
        f"git log {last_reviewed_commit}..HEAD --not {_base_ref} -p"
        if is_delta_review
        else f"git diff {_base_ref}...HEAD"
    )

    if draft_path:
        lines.append(f"1. Read the draft at `{draft_path}`")
    elif is_delta_review:
        lines.append(
            f"1. First run `git fetch origin {_delta_base_branch}`, then review "
            f"the delta using `{diff_command}` (see **Delta Review** below)"
        )
    else:
        lines.append(
            f"1. Review the implementation using `git log --oneline -10` and `{diff_command}`"
        )

    # Add procedural steps for code reviewers (matching GHA reviewer thoroughness).
    # Both ``code`` and ``code-holistic`` get the same numbered procedural-step
    # scaffold, but steps 2 and 8 differ by lens: ``code`` reviews every file
    # systematically and evaluates against the code-review criteria, while
    # ``code-holistic`` skims the diff once and runs the four cross-module
    # passes from the holistic criteria file. See issue #2126 — the prior
    # unified wording told the holistic reviewer to "review every changed
    # file systematically", which contradicted the holistic criteria's
    # "don't verify every line".
    if reviewer_type in ("code", "code-holistic") and not draft_path:
        if reviewer_type == "code-holistic":
            lines.append(
                "2. **Skim the full diff once** to build a mental map of "
                "what the PR adds, who the user is, and what the user's "
                "primary path through the change looks like — do not "
                "re-verify every line; that is the code reviewer's job"
            )
        else:
            lines.append("2. Get the full diff and **review every changed file systematically**")
        lines.append(
            "3. Read surrounding context — check how changed code integrates with the rest of the codebase"
        )
        lines.append(
            "4. Trace data flow from input to output, especially for security-sensitive paths"
        )
        lines.append(
            "5. Verify end-to-end functionality — for new features, trace the complete "
            "execution path in the real deployment environment. Check that config files, "
            "environment variables, and dependencies are actually available where the code runs"
        )
        lines.append(
            "6. Research when uncertain — use WebSearch and WebFetch (when available) "
            "to look up library behavior, check official documentation, verify "
            "API usage patterns, and confirm the code follows current best practices"
        )
        lines.append("7. Consider edge cases the author may not have tested")
        if reviewer_type == "code-holistic":
            lines.append(
                "8. Run the four mandatory passes from the criteria below "
                "(end-to-end primary use case, doc ↔ code symmetry, "
                "synthetic-key / sentinel coordination, silent-fallback hunt)"
            )
        else:
            lines.append("8. Evaluate against the criteria below")
        if concurrent:
            lines.append(
                "9. Deliver your full review via ACK/NACK (see BRC protocol below). "
                "Your `--reason` IS your review — include all findings there."
            )
        else:
            lines.append(f"9. Write your verdict to `{verdict_path}` as JSON")
            lines.append("10. Commit the verdict file")
        lines.append("")
        lines.append(
            "**Find ALL issues on the first pass** — do not stop after identifying "
            "a few problems. You are the last line of defense before code reaches "
            "production."
        )
    elif draft_path:
        # Expanded procedural steps for draft-based (non-code) reviewers
        lines.append("2. Read the draft thoroughly — do not skim")
        lines.append(
            "3. Cross-reference each section of the draft against the review criteria below"
        )
        lines.append("4. Cite specific sections, quotes, or omissions as evidence in your analysis")
        lines.append("5. Evaluate completeness — identify any criteria not adequately addressed")
        lines.append("6. Assess overall quality and coherence of the draft")
        if concurrent:
            lines.append(
                "7. Deliver your full review via ACK/NACK (see BRC protocol below). "
                "Your `--reason` IS your review — include all findings there."
            )
        else:
            lines.append(f"7. Write your verdict to `{verdict_path}` as JSON")
            lines.append("8. Commit the verdict file")
    else:
        lines.append("2. Evaluate it against the criteria below")
        if concurrent:
            lines.append(
                "3. Deliver your full review via ACK/NACK (see BRC protocol below). "
                "Your `--reason` IS your review — include all findings there."
            )
        else:
            lines.append(f"3. Write your verdict to `{verdict_path}` as JSON")
            lines.append("4. Commit the verdict file")
    lines.append("")

    # Review criteria
    lines.append("## Review Criteria\n")
    lines.append(_get_review_criteria_for_type(reviewer_type, phase, repo_path=repo_path))
    lines.append("")

    # Review conventions — quality standards aligned with PR reviewer thoroughness
    lines.append("## Review Conventions\n")
    if reviewer_type in ("code", "code-holistic"):
        lines.append(
            "You are a critical part of the engineering infrastructure — the last line "
            "of defense before code reaches production. Your review must meet these "
            "quality standards:\n"
        )
    else:
        lines.append("Your review must meet these quality standards:\n")
    lines.append(
        "1. **Be comprehensive.** Review the entire scope, not just the obvious parts. "
        "Do not stop after finding the first few issues."
    )
    lines.append(
        "2. **Be specific.** Reference exact file paths, line numbers, function names, "
        "and code snippets. Vague feedback is not actionable."
    )
    lines.append(
        "3. **Be direct.** State issues plainly without hedging or softening language. "
        '"This will fail when X" not "you might want to consider X".'
    )
    lines.append(
        "4. **Suggest fixes.** When identifying a problem, include a concrete suggestion "
        "for how to resolve it."
    )
    lines.append(
        "5. **Provide context.** Explain *why* something is an issue — the impact, "
        "the risk, or the principle being violated."
    )
    lines.append("")

    # Verdict classification — only for code reviewers (aligned with review-conventions.md)
    # Non-code reviewers get appropriate guidance from their type-specific criteria
    # (e.g., _get_plan_review_criteria() already says "flag as needs_revision")
    if reviewer_type in ("code", "code-holistic"):
        _nack_label = "NACK" if concurrent else "`needs_revision`"
        _ack_label = "ACK" if concurrent else "`approved`"
        lines.append(f"### When to {_nack_label} vs {_ack_label}\n")
        lines.append(
            f"**{_nack_label} for**: Security vulnerabilities, logic errors, correctness "
            "issues, non-functional features (core purpose doesn't work end-to-end), missing "
            "error handling, resource leaks, breaking changes, violations of codebase patterns. "
            f"When in doubt, {_nack_label}."
        )
        lines.append(
            f"**{_ack_label} for**: No blocking issues found after thorough review. "
            "Non-blocking suggestions should still be included."
        )
        lines.append("")
        lines.append(
            "**Key distinction**: A feature that doesn't work is a correctness issue, not a "
            "style issue. If the feature's core functionality is broken — not just degraded or "
            f"missing edge cases — always {_nack_label}, even if the code structure looks "
            "reasonable or matches an existing pattern."
        )
        lines.append(
            "**Pre-existing issues are still blocking**: If the code being reviewed modifies "
            f"areas with existing broken or inconsistent behavior, {_nack_label} — do not "
            'dismiss it as "not a regression." The code is already being changed in that area, '
            "making it the natural place to fix the issue. Code that adds new paths through "
            "already-broken logic makes the problem worse."
        )
        lines.append("")

    # Delta review directive for re-reviews
    if is_delta_review:
        lines.append("## Delta Review\n")
        lines.append(
            f"This is review cycle {review_cycle}. Focus on new changes since your "
            f"last review. First run `git fetch origin {_delta_base_branch}` to "
            f"ensure the base branch is available, then use "
            f"`git log {last_reviewed_commit}..HEAD --not {_base_ref} -p` to see "
            "the delta — this excludes any base-branch commits that were merged "
            "in since your last review, so you only see PR-authored changes. "
            "Verify prior feedback was addressed AND review new code thoroughly."
        )
        lines.append("")

    # Prior feedback for re-reviews
    if review_cycle > 1 and prior_feedback:
        lines.append("## Prior Review Feedback\n")
        lines.append(
            "This is a re-review. The previous review found issues. "
            "Verify that the following feedback was addressed:\n"
        )
        lines.append(prior_feedback)
        lines.append("")

    # Verdict format — only for sequential (non-concurrent) reviewers.
    # In concurrent/BRC mode, the ACK/NACK reason IS the review output.
    if not concurrent:
        lines.append("## Verdict Format\n")
        lines.append(f"Write the following JSON to `{verdict_path}`:\n")
        lines.append("```json")
        lines.append("{")
        lines.append(f'  "reviewer": "{reviewer_type}",')
        lines.append('  "verdict": "approved" or "needs_revision",')
        lines.append('  "summary": "Brief summary of findings (1-2 sentences)",')
        lines.append('  "analysis": "Detailed analysis of the reviewed work (see below)",')
        lines.append('  "suggestions": "Non-blocking suggestions for improvement",')
        lines.append('  "feedback": "Blocking issues requiring revision before approval",')
        lines.append('  "timestamp": "ISO 8601 timestamp"')
        lines.append("}")
        lines.append("```\n")
        lines.append("**Field guidelines:**\n")
        lines.append(
            "- **analysis**: Always provide detailed analysis regardless of verdict. "
            "Describe what you reviewed, what you found, and your reasoning."
        )
        lines.append(
            "- **suggestions**: Non-blocking observations and improvement ideas. "
            "Include these even when approving — they help the team improve over time."
        )
        lines.append(
            "- **feedback**: Reserved for **blocking issues only** — problems that must "
            "be fixed before the work can be approved. Leave empty when approving."
        )
        lines.append(
            "\nIf the work meets all criteria, set verdict to `approved`. "
            "If significant issues remain, set verdict to `needs_revision` "
            "and provide actionable feedback in the `feedback` field."
        )

    # Phase restrictions for reviewers
    lines.append("")
    lines.append("## Phase Restrictions\n")
    lines.append("- You CAN read all source files and review artifacts")
    if not concurrent:
        lines.append("- You CAN write verdict files to `.egg-state/reviews/`")
    if reviewer_type == "contract":
        lines.append(
            "- You CAN update the contract in `.egg-state/contracts/` (e.g. marking items as done)"
        )
    lines.append("- You CANNOT push code (git push)")
    lines.append("- You CANNOT create or update PRs")
    lines.append("- You CANNOT modify source files (src/, lib/, docs/, tests/)")
    lines.append("")

    return "\n".join(lines)


def _read_review_verdict(
    repo_path: Path,
    phase: str,
    reviewer_type: str = "code",
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> ReviewVerdict | None:
    """Read a typed review verdict JSON from the repo.

    Returns None if the file is missing or malformed (treated as approved
    for graceful degradation).
    """
    verdict_rel = _verdict_path_for_type(
        phase,
        reviewer_type,
        issue_number=issue_number,
        pipeline_id=pipeline_id,
    )
    verdict_file = repo_path / verdict_rel

    if not verdict_file.exists():
        logger.warning(
            "Verdict file not found, treating as approved",
            path=str(verdict_file),
            reviewer_type=reviewer_type,
        )
        return None

    try:
        raw = verdict_file.read_text()
        data = json.loads(raw)
        return ReviewVerdict(**data)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(
            "Failed to parse verdict file, treating as approved",
            path=str(verdict_file),
            reviewer_type=reviewer_type,
            error=str(e),
        )
        return None


def _read_tester_gaps(
    repo_path: Path,
    identifier: int | str | None = None,
) -> str | None:
    """Read tester output and extract gap findings for feedback to the coder.

    Reads `.egg-state/agent-outputs/{identifier}-tester-output.json` (with
    fallback to `tester-output.json`) and formats any test failures and gaps
    found into a summary string.

    Falls back to scanning the `summary` field for failure keywords when
    `gaps_found` is not present (backwards compat with old tester outputs).

    Args:
        repo_path: Path to the repository.
        identifier: Pipeline/issue identifier for namespaced filenames.

    Returns:
        Formatted gap summary string, or None if no gaps found.
    """
    outputs_dir = repo_path / ".egg-state" / "agent-outputs"

    # Try prefixed filename first, fall back to old global filename
    tester_output_file = None
    if identifier is not None:
        prefixed = outputs_dir / f"{identifier}-tester-output.json"
        if prefixed.exists():
            tester_output_file = prefixed
    if tester_output_file is None:
        tester_output_file = outputs_dir / "tester-output.json"

    if not tester_output_file.exists():
        return None

    try:
        raw = tester_output_file.read_text()
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Failed to parse tester output file",
            path=str(tester_output_file),
            error=str(e),
        )
        return None

    if not isinstance(data, dict):
        return None

    sections: list[str] = []

    tests_failed = data.get("tests_failed", 0)
    if tests_failed:
        sections.append(f"- **{tests_failed}** test(s) failed")

    gaps_found = data.get("gaps_found")
    if gaps_found and isinstance(gaps_found, list):
        # Cap at 10 gaps to avoid prompt bloat
        capped = gaps_found[:10]
        for gap in capped:
            gap_str = str(gap)[:200]
            sections.append(f"- {gap_str}")
        if len(gaps_found) > 10:
            sections.append(f"- ... and {len(gaps_found) - 10} more gaps")
    elif not tests_failed:
        # Backwards compat: scan summary for failure keywords
        summary = data.get("summary", "")
        if isinstance(summary, str) and any(
            kw in summary.lower() for kw in ("fail", "gap", "missing", "error", "deficien")
        ):
            sections.append(f"- Tester summary: {summary}")

    if not sections:
        return None

    return f"{TESTER_FINDINGS_HEADER}\n" + "\n".join(sections)


def _aggregate_review_verdicts(
    verdicts: dict[str, ReviewVerdict | None],
) -> AggregatedReviewResult:
    """Aggregate multiple typed review verdicts into an overall result.

    Returns:
        AggregatedReviewResult with:
        - verdict: "approved" or "needs_revision" (any needs_revision → overall needs_revision)
        - blocking_feedback: combined feedback from needs_revision verdicts only
        - advisory_content: analysis and suggestions from ALL verdicts (including approved)

        Missing/None verdicts are skipped.
    """
    overall = "approved"
    feedback_sections: list[str] = []
    advisory_sections: list[str] = []

    for reviewer_type, verdict in verdicts.items():
        if verdict is None:
            continue

        # Collect blocking feedback from needs_revision verdicts
        if verdict.verdict == "needs_revision":
            overall = "needs_revision"
            section = f"### {reviewer_type} reviewer\n"
            if verdict.feedback:
                section += verdict.feedback
            elif verdict.summary:
                section += verdict.summary
            feedback_sections.append(section)

        # Collect analysis and suggestions from ALL verdicts (including approved)
        advisory_parts: list[str] = []
        if verdict.analysis:
            advisory_parts.append(verdict.analysis)
        if verdict.suggestions:
            advisory_parts.append(f"**Suggestions:** {verdict.suggestions}")
        if advisory_parts:
            advisory_sections.append(
                f"### {reviewer_type} reviewer\n" + "\n\n".join(advisory_parts)
            )

    blocking_feedback = "\n\n".join(feedback_sections) if feedback_sections else ""
    advisory_content = "\n\n".join(advisory_sections) if advisory_sections else ""
    return AggregatedReviewResult(
        verdict=overall,
        blocking_feedback=blocking_feedback,
        advisory_content=advisory_content,
    )


def _sync_worktree_with_remote(
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    worktree_repo_path: Path,
    prior_phase_succeeded: bool = True,
    gateway_mode: Literal["public", "private"] = "public",
    base_branch: str | None = None,
    *,
    pipeline_branch: str | None = None,
) -> None:
    """Sync a worktree with its remote branch (best-effort).

    After an orchestrator restart or a phase boundary, the local worktree
    branch may be behind the remote: commits pushed during previous phases
    (contracts, drafts, statefiles) exist on origin but not in the local
    checkout.  This function fetches those commits and reconciles the
    worktree so that all downstream code (contract loading, draft reading,
    populator, etc.) sees the full pipeline state.

    ``pipeline_branch`` is the **remote** branch name to reconcile against.
    Since #2399 the pipeline tip lives at ``egg/<pid>/work`` on origin so
    slice integration branches at ``egg/<pid>/slice-N`` can coexist as
    siblings; ``pipeline.branch`` already carries that ``/work`` suffix
    (set by :func:`_ensure_pipeline_work_ref` at submission time), so
    callers should pass ``pipeline_branch=pipeline.branch`` directly —
    the local worktree branch and the remote ref now match.  Without an
    explicit ``pipeline_branch``, the function reads
    ``git branch --show-current`` and looks up ``origin/<that-name>``,
    which always misses on real pipelines and exits at
    ``case=no_remote_tracking`` (#2367).  Callers with a pipeline in
    scope MUST pass ``pipeline_branch=pipeline.branch``.  When omitted,
    the function falls back to the local branch name for backward
    compatibility with non-pipeline scripts.

    When local is ahead of remote:
    - If the prior phase succeeded, push local commits to remote first,
      then reset to origin (preserves completed work).
    - If the prior phase failed or was killed, discard local commits and
      reset to remote (discards incomplete work).

    When local has diverged (ahead AND behind), rebase local commits onto
    ``origin/{pipeline_branch}`` via the same helper used by the
    gateway-side push-reject reconcile path.  ``--ff-only`` cannot
    reconcile real divergence by definition, so the pre-#2337
    implementation silently left the worktree stale and downstream
    populator/decision-sync paths consumed the stale state.

    Every return path emits at least one ``worktree_sync_outcome`` log
    line with a ``case`` discriminator so production logs name which
    path fired.  Paths that fall through to the step-4 reset
    (``local_ahead_push_failed``, ``local_ahead_discarded``,
    ``rev_list_failed``) emit a sequence — first a discriminator naming
    WHY we fell through, then the terminal ``reset_succeeded`` /
    ``reset_failed`` event.

    Safe to call on every pipeline start because it is idempotent when the
    local branch is already up to date.
    """
    base_branch_for_reconcile = base_branch
    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_repo_path}",
        "-C",
        str(worktree_repo_path),
    ]

    # Step 1: Authenticated fetch via gateway (gateway holds GitHub credentials)
    fetch_ok = spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    if not fetch_ok:
        logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            case="fetch_failed",
        )
        return

    # Step 2: Determine current branch
    try:
        result = subprocess.run(
            [*git_base, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        branch = result.stdout.strip()
        if not branch:
            logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                case="detached_head",
            )
            return
    except Exception as branch_err:
        logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            case="branch_detect_failed",
            error=str(branch_err),
        )
        return

    # ``branch`` is the **local** branch name (e.g. ``egg/<pid>/work`` on
    # orchestrator worktrees).  ``remote_branch`` is the remote-side name
    # we look up on origin and push/reset against.  When the caller
    # passes ``pipeline_branch`` (the canonical, agent-facing branch),
    # use it for every remote-side ref so the ``/work`` suffix mismatch
    # in #2367 cannot strand a pipeline in ``no_remote_tracking``.
    remote_branch = pipeline_branch or branch

    # Step 3: Verify remote tracking branch exists
    try:
        result = subprocess.run(
            [*git_base, "rev-parse", "--verify", f"origin/{remote_branch}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="no_remote_tracking",
            )
            return
    except Exception as rev_parse_err:
        logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="rev_parse_failed",
            error=str(rev_parse_err),
        )
        return

    # Step 3b: Check divergence between local and remote.
    local_ahead = 0
    remote_ahead = 0
    rev_list_ok = False
    try:
        result = subprocess.run(
            [
                *git_base,
                "rev-list",
                "--left-right",
                "--count",
                f"HEAD...origin/{remote_branch}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        parts = result.stdout.strip().split()
        if result.returncode == 0 and len(parts) == 2:
            local_ahead = int(parts[0])
            remote_ahead = int(parts[1])
            rev_list_ok = True
        else:
            logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="rev_list_failed",
                rc=result.returncode,
                stdout=result.stdout.strip()[:200],
            )
            # Fall through to reset (best-effort) — step 4 will emit its own outcome.
    except Exception as rev_list_err:
        logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="rev_list_failed",
            error=str(rev_list_err),
        )
        # Fall through to reset (best-effort) — step 4 will emit its own outcome.

    # Step 3c: Handle local-ahead commits.
    if local_ahead == 0 and remote_ahead == 0 and rev_list_ok:
        # Local and remote are already in sync — skip the no-op reset entirely
        # so the outcome is distinguishable from a true behind-remote sync.
        logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="already_in_sync",
            local_ahead=0,
            remote_ahead=0,
        )
        return

    if local_ahead > 0 and remote_ahead == 0:
        # Local is strictly ahead of remote (no divergence).
        if prior_phase_succeeded:
            # Prior phase completed successfully — push local work to remote
            # before resetting, so it's not lost.  Pushing to ``remote_branch``
            # (not the local ``/work`` name) so the agent-facing branch
            # receives the commits — the gateway builds
            # ``HEAD:refs/heads/{branch}`` from this argument.
            push_result = spawner.gateway.push_worktree_branch(
                pipeline_id=pipeline_id,
                repo_path=str(worktree_repo_path),
                branch=remote_branch,
                mode=gateway_mode,
                base_branch=base_branch_for_reconcile,
            )
            if push_result:
                # Push succeeded — local and remote are now in sync.
                # Re-fetch to update the remote tracking ref so that
                # origin/{remote_branch} reflects the pushed commits.
                spawner.gateway.fetch_worktree_branch(
                    pipeline_id=pipeline_id,
                    repo_path=str(worktree_repo_path),
                    mode=gateway_mode,
                )
                logger.info(
                    "worktree_sync_outcome",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    remote_branch=remote_branch,
                    case="local_ahead_pushed",
                    local_ahead=local_ahead,
                    remote_ahead=remote_ahead,
                )
                return
            else:
                logger.warning(
                    "worktree_sync_outcome",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    remote_branch=remote_branch,
                    case="local_ahead_push_failed",
                    local_ahead=local_ahead,
                    remote_ahead=remote_ahead,
                    category=push_result.category,
                    error=push_result.detail,
                )
        else:
            # Prior phase failed — incomplete local work will be discarded by
            # the step-4 reset. Emit a distinct case so operators can grep
            # this branch of the taxonomy without inferring it from
            # reset_succeeded with local_ahead > 0.
            logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="local_ahead_discarded",
                local_ahead=local_ahead,
                remote_ahead=remote_ahead,
            )
        # Fall through to reset (Step 4) — discards local work when prior phase
        # failed, or recovers via reset after a push failure.

    elif local_ahead > 0 and remote_ahead > 0:
        # True divergence.  Reconcile by rebasing local commits onto
        # origin/{branch} via the same helper used by the gateway-side
        # push-reject reconcile path (#2337).  --ff-only cannot reconcile
        # real divergence by definition, so the pre-#2337 implementation
        # silently left the worktree stale.
        #
        # ⚠️ When ``base_branch_for_reconcile`` is None,
        # ``_build_rebase_cmd`` falls back to the plain
        # ``git rebase origin/{branch}`` form — the same form that
        # triggered #2222 main-contamination on the gateway-side
        # push-reject path.  That fallback is the contamination vector:
        # with HEAD at current main and origin/{branch} on a stale
        # snapshot, the plain form replays merge-base..HEAD on the
        # stale tip, producing a PR full of duplicate-by-content
        # commits.  Callers should always thread ``pipeline.base_branch``
        # so the helper emits the safer
        # ``--onto origin/{branch} origin/{base_branch}`` form.  Logging
        # the None case so the next person debugging contamination has a
        # breadcrumb.
        if base_branch_for_reconcile is None:
            logger.warning(
                "worktree_sync divergence_rebase with base_branch=None — "
                "falling back to bare-rebase form (#2222 contamination risk)",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
            )
        logger.info(
            "Local and remote have diverged — rebasing local onto origin",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            local_ahead=local_ahead,
            remote_ahead=remote_ahead,
        )
        rebase_outcome = _rebase_with_agent_output_autoresolve(
            git_base=git_base,
            pipeline_id=pipeline_id,
            branch=remote_branch,
            base_branch=base_branch_for_reconcile,
        )
        if rebase_outcome.ok:
            logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="divergence_rebased",
                local_ahead=local_ahead,
                remote_ahead=remote_ahead,
            )
            return
        logger.error(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="divergence_rebase_failed",
            local_ahead=local_ahead,
            remote_ahead=remote_ahead,
            category=rebase_outcome.category,
            detail=rebase_outcome.detail,
        )
        return

    # Step 4: Reset local branch to remote.
    # This handles: local behind remote, post-push reset, and rev-list-failed
    # fall-through. (The already-in-sync case returns early above.)
    try:
        result = subprocess.run(
            [*git_base, "reset", "--hard", f"origin/{remote_branch}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="reset_failed",
                local_ahead=local_ahead,
                remote_ahead=remote_ahead,
                error=result.stderr.strip(),
            )
        else:
            logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="reset_succeeded",
                local_ahead=local_ahead,
                remote_ahead=remote_ahead,
            )
    except Exception as sync_err:
        logger.warning(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="reset_failed",
            local_ahead=local_ahead,
            remote_ahead=remote_ahead,
            error=str(sync_err),
        )


class StalePipelineBranchError(RuntimeError):
    """Raised when ``origin/<pipeline.branch>`` is behind base and the
    rebase to bring it up to date hit a conflict.

    Phase-startup callers convert this into a FAILED pipeline with a
    clear ``error`` so the operator knows to manually rebase or start
    fresh — vastly preferable to silently producing a PR with 70+
    cherry-picked-variant commits buried in it (#2098).
    """


def _rebase_pipeline_branch_onto_base(
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    worktree_repo_path: Path,
    pipeline_branch: str,
    base_branch: str,
    gateway_mode: Literal["public", "private"] = "public",
) -> None:
    """Rebase a stale ``origin/<pipeline_branch>`` onto ``origin/<base_branch>``.

    When ``submit_task`` resumes a pipeline whose branch has been sitting
    on the remote for days/weeks while ``main`` advanced, the existing
    pipeline branch tip carries old-SHA copies of commits that have since
    been rebased onto main.  Without this helper, the first orchestrator
    push hits non-fast-forward, the reconcile path rebases ``HEAD`` onto
    the stale tip, and every downstream commit inherits 70+ stale-from-
    main commits as ancestors — producing a final PR diff that buries
    the actual feature work under contamination (#2098).

    This helper runs on the orchestrator-side worktree and treats it as
    scratch space for the rebase:

    1. Skip when ``pipeline_branch`` doesn't exist on the remote (fresh
       run — there's nothing to rebase).
    2. Skip when ``origin/<pipeline_branch>`` is not behind
       ``origin/<base_branch>`` (already up to date).
    3. Skip when ``HEAD`` is an ancestor of *neither*
       ``origin/<pipeline_branch>`` *nor* ``origin/<base_branch>``.  Two
       real resume paths satisfy the ancestry check:

       (a) **Preserved worktree** (canonical #2098 case): the
           orchestrator-side worktree was kept across a cancel/resubmit,
           so ``HEAD`` carries state-file commits that were already
           pushed to ``origin/<branch>``.  ``HEAD`` is a strict ancestor
           of ``origin/<branch>``.
       (b) **Fresh worktree**: the worktree volume was wiped between
           cancel and resubmit (e.g. orchestrator redeploy onto a fresh
           PVC), so the gateway recreated it from ``origin/<base>``.
           ``HEAD == origin/<base>`` is a (trivial) ancestor of
           ``origin/<base>``; resetting to ``origin/<branch>`` discards
           no unique commits because every base commit is preserved as
           the rebase target.

       If neither ancestry holds, ``HEAD`` carries truly unpublished
       work and we defer rather than overwrite it.
    4. Reset the worktree to ``origin/<pipeline_branch>``, ``git rebase
       origin/<base_branch>``, and force-push the rebased tip.  Git's
       built-in cherry-pick-skip drops commits already content-equivalent
       to ones on the new base.
    5. On conflict: abort the rebase, restore the worktree to
       ``origin/<base_branch>``, and raise ``StalePipelineBranchError``
       so phase startup fails fast with an actionable error.

    Best-effort fetch+rev-list errors are logged and swallowed so a
    transient gateway hiccup doesn't block pipeline startup; only a
    rebase that *started* but couldn't finish raises.
    """
    if not pipeline_branch or not base_branch or pipeline_branch == base_branch:
        return

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_repo_path}",
        "-C",
        str(worktree_repo_path),
    ]

    def _run_git(
        args: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str] | None:
        """Run a git command and convert ``TimeoutExpired`` / ``OSError``
        into a ``None`` return so callers can decide what to do.

        Mirrors the defensive pattern in ``_sync_worktree_with_remote``.
        """
        try:
            return subprocess.run(
                [*git_base, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "rebase-on-resume: git command failed to run",
                pipeline_id=pipeline_id,
                branch=pipeline_branch,
                git_args=args,
                error=str(exc),
            )
            return None

    # Step 1: Fetch both refs through the gateway so we have current
    # origin/<branch> and origin/<base> tips locally.  fetch_worktree_branch
    # already runs `git fetch origin` (no refspec) which updates all
    # remote-tracking refs in one call.
    fetch_ok = spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    if not fetch_ok:
        logger.warning(
            "rebase-on-resume: fetch failed, skipping rebase check",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
        )
        return

    # Step 2: Verify origin/<pipeline_branch> exists.  Fresh pipelines
    # haven't pushed yet, so there's nothing to rebase.
    verify_branch = _run_git(["rev-parse", "--verify", f"origin/{pipeline_branch}"], timeout=10)
    if verify_branch is None or verify_branch.returncode != 0:
        return

    verify_base = _run_git(["rev-parse", "--verify", f"origin/{base_branch}"], timeout=10)
    if verify_base is None or verify_base.returncode != 0:
        logger.warning(
            "rebase-on-resume: origin/<base_branch> not resolvable, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
        )
        return

    # Step 3: Is the pipeline branch actually behind base?  If not, no-op.
    behind = _run_git(
        [
            "rev-list",
            "--count",
            f"origin/{pipeline_branch}..origin/{base_branch}",
        ],
        timeout=10,
    )
    if behind is None or behind.returncode != 0:
        logger.warning(
            "rebase-on-resume: rev-list failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            stderr=(behind.stderr.strip() if behind is not None else None),
        )
        return
    try:
        behind_count = int(behind.stdout.strip() or "0")
    except ValueError:
        behind_count = 0
    if behind_count == 0:
        return

    # Step 4: Confirm reset-to-origin/<branch> is lossless before we
    # overwrite HEAD.  Three worktree states are handled:
    #
    #   (a) Preserved-worktree resume (#2098 canonical): the orchestrator-
    #       side worktree was kept across a cancel/resubmit, so HEAD
    #       carries state-file commits that were already pushed to
    #       origin/<branch>.  HEAD is a strict ancestor of
    #       origin/<branch> — resetting drops nothing.
    #   (b) Fresh-worktree resume: the worktree volume was wiped between
    #       cancel and resubmit (e.g. orchestrator redeploy onto a fresh
    #       PVC, manual cleanup), so the gateway recreated the worktree
    #       from origin/<base>.  HEAD == origin/<base>; resetting to
    #       origin/<branch> discards no unique commits because every
    #       commit on origin/<base> is preserved as the rebase target.
    #   (c) Confused-HEAD resume (#2222): the worktree carries a local-
    #       only commit (e.g. a half-pushed statefiles commit) on top of
    #       a stale origin/<branch> tip — HEAD is on neither ref.  The
    #       previous behaviour was to "defer to push-reconcile", but the
    #       reconcile path's _build_rebase_cmd fallback is the
    #       contamination producer in #2222.  Recover by hard-resetting
    #       to origin/<base>: any local-only work is dropped (it would
    #       be re-created by agents on the next phase, vastly preferable
    #       to a contaminated PR).
    def _head_on(ref: str) -> bool:
        result = _run_git(["merge-base", "--is-ancestor", "HEAD", ref], timeout=10)
        return result is not None and result.returncode == 0

    if not (_head_on(f"origin/{pipeline_branch}") or _head_on(f"origin/{base_branch}")):
        logger.warning(
            "rebase-on-resume: HEAD on neither origin/<branch> nor origin/<base> — "
            "resetting to origin/<base> to avoid push-reconcile contamination (#2222)",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
            behind_base=behind_count,
        )
        # Step 4a: hard-reset to ``origin/<base>`` first.  Note that step 5
        # immediately overwrites HEAD again with ``reset --hard
        # origin/<branch>`` in the success path, so this reset's effect on
        # HEAD is short-lived — its purpose is to act as a safe-state floor:
        # if step 5 itself fails (network blip, ref vanishes), we leave the
        # worktree on a known-good ref (``origin/<base>``) instead of the
        # ambiguous pre-recovery state that prompted the rescue.  Don't
        # "simplify" by dropping this — the back-to-back hard resets are
        # intentional.
        recovery_reset = _run_git(["reset", "--hard", f"origin/{base_branch}"], timeout=30)
        if recovery_reset is None or recovery_reset.returncode != 0:
            logger.warning(
                "rebase-on-resume: recovery reset to origin/<base> failed, skipping",
                pipeline_id=pipeline_id,
                branch=pipeline_branch,
                base_branch=base_branch,
                stderr=(recovery_reset.stderr.strip() if recovery_reset is not None else None),
            )
            return

    logger.info(
        "rebase-on-resume: pipeline branch is behind base, attempting rebase",
        pipeline_id=pipeline_id,
        branch=pipeline_branch,
        base_branch=base_branch,
        behind_base=behind_count,
    )

    # Step 5: Reset the worktree to the stale pipeline branch tip so we
    # can rebase it onto current base.
    reset_to_branch = _run_git(["reset", "--hard", f"origin/{pipeline_branch}"], timeout=30)
    if reset_to_branch is None or reset_to_branch.returncode != 0:
        logger.warning(
            "rebase-on-resume: reset to pipeline branch failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            stderr=(reset_to_branch.stderr.strip() if reset_to_branch is not None else None),
        )
        return

    # Step 6: Rebase onto current base.  Plain ``git rebase
    # origin/<base>`` — git's cherry-pick-skip drops content-equivalent
    # commits already on base (the 70+ stale-variant commits in #2098).
    rebase = _run_git(["rebase", f"origin/{base_branch}"], timeout=120)
    if rebase is None or rebase.returncode != 0:
        # Conflict, timeout, or other rebase failure.  Abort the rebase,
        # restore the worktree to origin/<base> so it isn't left mid-
        # rebase for downstream callers, and raise so the operator gets
        # an actionable error rather than a contaminated PR.  ``rebase
        # is None`` covers the timeout case where ``_run_git`` already
        # logged the underlying exception.
        _run_git(["rebase", "--abort"], timeout=30)
        _run_git(["reset", "--hard", f"origin/{base_branch}"], timeout=30)
        stderr_text = rebase.stderr.strip() if rebase is not None else "rebase command timed out"
        logger.error(
            "rebase-on-resume: rebase failed — aborting pipeline start",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
            stderr=stderr_text,
            timed_out=rebase is None,
        )
        raise StalePipelineBranchError(
            f"origin/{pipeline_branch} is {behind_count} commits behind "
            f"origin/{base_branch} and rebasing it failed. "
            f"Manually rebase the branch (or delete it to start fresh) "
            f"and resubmit. Stderr: {stderr_text}"
        )

    # Git emits ``warning: skipped previously applied commit <sha>`` on
    # stderr for every cherry-pick-equivalent it dropped.  Counting them
    # gives operators a quick sanity check that the helper actually
    # discarded the stale-from-main commits (vs. e.g. silently no-op'd).
    skipped_via_rebase = sum(
        1 for line in rebase.stderr.splitlines() if "skipped previously applied commit" in line
    )

    # Step 7: Force-push the rebased branch.  ``force=True`` is required
    # because the rebased tip has different SHAs from origin/<branch>;
    # this is exactly the contamination we just removed, so overwriting
    # is the desired behavior.
    push_result = spawner.gateway.push_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        branch=pipeline_branch,
        mode=gateway_mode,
        base_branch=base_branch,
        force=True,
    )
    if not push_result.ok:
        # Restore HEAD to origin/<base> so the worktree is in a known
        # state for downstream callers (the rebased commits stay in the
        # local reflog if needed for recovery).
        _run_git(["reset", "--hard", f"origin/{base_branch}"], timeout=30)
        logger.error(
            "rebase-on-resume: force-push of rebased branch failed",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            category=push_result.category,
            detail=push_result.detail,
        )
        raise StalePipelineBranchError(
            f"Rebased {pipeline_branch} onto origin/{base_branch} but "
            f"force-push to remote failed ({push_result.category}): "
            f"{push_result.detail}"
        )

    # Re-fetch so origin/<pipeline_branch> reflects the rebased tip for
    # any subsequent rev-parse in the same pipeline-start path.
    spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    logger.info(
        "rebase-on-resume: rebased and force-pushed pipeline branch",
        pipeline_id=pipeline_id,
        branch=pipeline_branch,
        base_branch=base_branch,
        dropped_stale_commits=behind_count,
        skipped_via_rebase=skipped_via_rebase,
    )


def _refresh_pipeline_branch_against_current_base(
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    worktree_repo_path: Path,
    pipeline_branch: str,
    base_branch: str,
    gateway_mode: Literal["public", "private"] = "public",
) -> bool:
    """Rebase ``origin/<pipeline_branch>`` onto current ``origin/<base_branch>``
    immediately before opening the PR (#2224 PR 2).

    ``_rebase_pipeline_branch_onto_base`` runs at the start of each
    phase iteration to clean up stale branch state on resume.  Nothing
    between branch-cut and PR-open refreshes against
    ``origin/<base_branch>``; if ``base_branch`` advances *during* the
    PR phase's own work, the resulting PR is behind.  This helper
    closes that gap.

    The pipeline branch is the only ref this helper writes to: the
    rebase replays pipeline-branch commits onto current
    ``origin/<base_branch>``, and the force-push targets
    ``pipeline_branch``.  ``base_branch`` is read-only here — no
    commits are ever pushed to it, even when it happens to be
    ``main``.

    On success, force-pushes the rebased branch so the open PR's head
    SHA reflects the rebase.

    On *any* failure (rebase conflict, push rejection, transient gateway
    error), restores the worktree to ``origin/<pipeline_branch>``,
    logs at WARNING, and returns ``False`` — the caller still opens the
    PR against the un-rebased tip.  This is intentional: a merge conflict
    at PR-open time is better surfaced to the human reviewer than
    swallowed by failing the whole pipeline.

    Returns ``True`` when a rebase was performed and pushed; ``False``
    when no rebase was needed or any step failed (in which case the
    caller proceeds with the un-rebased tip).
    """
    if not pipeline_branch or not base_branch or pipeline_branch == base_branch:
        return False

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_repo_path}",
        "-C",
        str(worktree_repo_path),
    ]

    def _run_git(
        args: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                [*git_base, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "pr-open rebase: git command failed",
                pipeline_id=pipeline_id,
                branch=pipeline_branch,
                git_args=args,
                error=str(exc),
            )
            return None

    # Step 1: Fetch fresh refs.  Without this we'd rebase against the
    # base tip we saw at branch-cut, defeating the whole point.
    fetch_ok = spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    if not fetch_ok:
        logger.warning(
            "pr-open rebase: fetch failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
        )
        return False

    # Step 2: Verify both refs resolve.
    verify_branch = _run_git(["rev-parse", "--verify", f"origin/{pipeline_branch}"], timeout=10)
    if verify_branch is None or verify_branch.returncode != 0:
        return False
    verify_base = _run_git(["rev-parse", "--verify", f"origin/{base_branch}"], timeout=10)
    if verify_base is None or verify_base.returncode != 0:
        return False

    # Step 3: No-op when the branch is already up-to-date with current base
    # (no commits behind).  Saves a force-push when none is needed.
    behind = _run_git(
        [
            "rev-list",
            "--count",
            f"origin/{pipeline_branch}..origin/{base_branch}",
        ],
        timeout=10,
    )
    if behind is None or behind.returncode != 0:
        return False
    try:
        behind_count = int((behind.stdout or "0").strip() or "0")
    except ValueError:
        behind_count = 0
    if behind_count == 0:
        return False

    # Step 4: Compute the merge-base so we can use the safe
    # ``--onto <new_base> <upstream>`` form (HEAD is the implicit branch
    # being rebased after the step-5 reset).  The merge-base is the
    # commit where the branch diverged from base_branch; using it as
    # ``<upstream>`` tells git "replay only the commits unique to HEAD
    # onto <new_base>" — no base-branch commits get absorbed into the
    # branch's linear history, which is the contamination shape #2222
    # hardened against in the push-reconcile path.
    merge_base_proc = _run_git(
        ["merge-base", f"origin/{pipeline_branch}", f"origin/{base_branch}"],
        timeout=15,
    )
    if merge_base_proc is None or merge_base_proc.returncode != 0:
        logger.warning(
            "pr-open rebase: merge-base resolution failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
            stderr=(merge_base_proc.stderr.strip() if merge_base_proc is not None else None),
        )
        return False
    merge_base = (merge_base_proc.stdout or "").strip()
    if not merge_base:
        return False

    # Step 5: Reset the worktree to the current branch tip so the
    # rebase operates on the right starting state.  The reset target
    # is ``origin/<pipeline_branch>`` — fresh from fetch in step 1 —
    # so we are not rebasing on top of stale local state.
    #
    # Unlike ``_rebase_pipeline_branch_onto_base`` (resume-time helper),
    # there is no ``_head_on(...)`` ancestry guard before this reset.
    # That is intentional at the PR-open call site: if we got here,
    # ``_finalize_pr_phase_failed`` either left HEAD on
    # ``origin/<branch>`` (push_ok=True path → reset is a no-op) or
    # carries unpushed orchestrator housekeeping commits that are
    # already orphan-by-design per its docstring.  Either way, no
    # local-only work needs to be preserved here.
    reset = _run_git(["reset", "--hard", f"origin/{pipeline_branch}"], timeout=30)
    if reset is None or reset.returncode != 0:
        logger.warning(
            "pr-open rebase: reset to origin/<branch> failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            stderr=(reset.stderr.strip() if reset is not None else None),
        )
        return False

    # Step 6: Rebase using the safe ``--onto <new_base> <upstream>``
    # form.  HEAD is the implicit branch being rebased (set by the
    # step-5 reset above).  The closest argv-shape prior art is
    # ``gateway_client._build_rebase_cmd`` — that one rebases in the
    # opposite direction (replay HEAD onto a stale branch tip) but uses
    # the same explicit-upstream pattern that pins the replay range to
    # ``<upstream>..HEAD`` and so sidesteps the bare-form contamination
    # shape behind #2222.
    rebase = _run_git(
        [
            "rebase",
            "--onto",
            f"origin/{base_branch}",
            merge_base,
        ],
        timeout=120,
    )
    if rebase is None or rebase.returncode != 0:
        # Conflict, timeout, or any failure: abort cleanly and restore
        # to origin/<branch> so the caller can still open the PR
        # against the un-rebased tip.  Unlike the resume-time helper,
        # we *don't* raise here — pipeline failure for a merge conflict
        # at PR-open time is worse than a slightly-behind PR.
        _run_git(["rebase", "--abort"], timeout=30)
        _run_git(["reset", "--hard", f"origin/{pipeline_branch}"], timeout=30)
        stderr_text = rebase.stderr.strip() if rebase is not None else "rebase command timed out"
        logger.warning(
            "pr-open rebase: rebase failed, opening PR against un-rebased tip",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
            behind_base=behind_count,
            stderr=stderr_text,
            timed_out=rebase is None,
        )
        return False

    # Step 7: Force-push the rebased tip so origin/<branch> matches the
    # SHAs the PR will be opened against.
    push_result = spawner.gateway.push_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        branch=pipeline_branch,
        mode=gateway_mode,
        base_branch=base_branch,
        force=True,
    )
    if not push_result.ok:
        # Best-effort restore so the worktree state is predictable for
        # downstream callers; the PR still opens against the pre-rebase
        # remote tip (which is what origin/<branch> still reflects).
        _run_git(["reset", "--hard", f"origin/{pipeline_branch}"], timeout=30)
        logger.warning(
            "pr-open rebase: force-push of rebased branch failed, "
            "opening PR against un-rebased remote tip",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            category=push_result.category,
            detail=push_result.detail,
        )
        return False

    # Re-fetch so origin/<branch> reflects the pushed tip locally.
    spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    logger.info(
        "pr-open rebase: rebased and force-pushed pipeline branch",
        pipeline_id=pipeline_id,
        branch=pipeline_branch,
        base_branch=base_branch,
        behind_base_at_start=behind_count,
    )
    return True


def _commit_statefiles_to_worktree(
    worktree_path: Path,
    message: str,
    pipeline_identifier: int | str | None = None,
    *,
    pipeline_id: str | None = None,
) -> None:
    """Stage and commit ``.egg-state/`` files in *worktree_path*.

    When *pipeline_identifier* is provided, only files whose names start
    with the identifier (followed by ``.`` or ``-``) are staged.  This
    prevents concurrent pipelines from leaking each other's state files
    into unrelated PRs (see #1390).

    Most ``.egg-state/`` files are prefixed with the issue number (drafts,
    reviews, BRC history, agent-outputs), but contract files are keyed by
    ``pipeline_id`` (e.g. ``issue-1759-v3.json``) and don't share the
    issue-number prefix.  When *pipeline_id* is provided alongside
    *pipeline_identifier*, files matching either prefix are staged — this
    closes the gap where plan-phase contract updates were written to disk
    but never committed because the glob only saw the issue-number prefix
    (see #1829).

    Falls back to staging the entire ``.egg-state/`` directory when both
    *pipeline_identifier* and *pipeline_id* are ``None`` (backwards-compat).

    The commit is idempotent (skips when nothing is staged).
    Raises ``subprocess.CalledProcessError`` on git failure.
    Call sites decide whether to abort or continue.
    """
    state_dir = worktree_path / ".egg-state"
    logger.info(
        "_commit_statefiles_to_worktree: entering",
        worktree_path=str(worktree_path),
        pipeline_identifier=str(pipeline_identifier),
        pipeline_id=str(pipeline_id),
        commit_message=message,
    )
    if not state_dir.exists():
        logger.info(
            "_commit_statefiles_to_worktree: no .egg-state directory — exiting",
            worktree_path=str(worktree_path),
            pipeline_identifier=str(pipeline_identifier),
            pipeline_id=str(pipeline_id),
        )
        return  # Nothing to commit yet

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_path}",
        "-C",
        str(worktree_path),
    ]

    if pipeline_identifier is not None or pipeline_id is not None:
        # Scope to files belonging to this pipeline only (#1390).
        # Use prefix-anchored patterns with delimiter boundaries to avoid
        # substring false positives (e.g. pipeline 4 matching pipeline 42).
        # Union both prefixes so issue-number-prefixed files (drafts,
        # reviews, BRC history) and pipeline-id-keyed files (contracts)
        # are all staged (#1829).
        prefixes: list[str] = []
        if pipeline_identifier is not None:
            prefixes.append(str(pipeline_identifier))
        if pipeline_id is not None and pipeline_id not in prefixes:
            prefixes.append(pipeline_id)

        matched_set: set[str] = set()
        for pid in prefixes:
            escaped = glob.escape(pid)
            pattern_dot = str(state_dir / "**" / f"{escaped}.*")
            pattern_dash = str(state_dir / "**" / f"{escaped}-*")
            for f in glob.glob(pattern_dot, recursive=True) + glob.glob(
                pattern_dash, recursive=True
            ):
                if Path(f).is_file():
                    matched_set.add(f)
        matched = sorted(matched_set)
        logger.info(
            "_commit_statefiles_to_worktree: glob match results",
            pipeline_identifier=str(pipeline_identifier),
            pipeline_id=str(pipeline_id),
            prefixes=prefixes,
            match_count=len(matched),
            matched_paths=[str(Path(f).relative_to(worktree_path)) for f in matched[:20]],
            truncated=len(matched) > 20,
        )
        if not matched:
            return  # No state files for this pipeline yet

        rel_paths = [str(Path(f).relative_to(worktree_path)) for f in matched]
        subprocess.run(
            [*git_base, "add", "--force", "--"] + rel_paths,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    else:
        subprocess.run(
            [*git_base, "add", "--force", ".egg-state/"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

    # Only commit if there are staged changes (idempotent on re-runs)
    result = subprocess.run(
        [*git_base, "diff", "--cached", "--quiet", "--", ".egg-state/"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode == 0:
        logger.info(
            "_commit_statefiles_to_worktree: nothing staged — skipping commit",
            pipeline_identifier=str(pipeline_identifier),
            commit_message=message,
        )
        return  # Nothing to commit

    logger.info(
        "_commit_statefiles_to_worktree: staged changes detected — committing",
        pipeline_identifier=str(pipeline_identifier),
        commit_message=message,
    )
    subprocess.run(
        [*git_base, "commit", "--no-verify", "-m", message, "--", ".egg-state/"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    logger.info(
        "_commit_statefiles_to_worktree: commit succeeded",
        pipeline_identifier=str(pipeline_identifier),
        commit_message=message,
    )


def _cleanup_agent_outputs_for_pr(
    worktree_path: Path,
    pipeline_id: str,
) -> None:
    """Remove ``.egg-state/agent-outputs/`` from the PR branch at PR-phase entry.

    Files under ``.egg-state/agent-outputs/`` are coder→tester handoff
    artifacts (e.g. ``coder-test-changes.patch``) that the tester consumes
    and re-emits as real source/test files.  They are ephemeral: once the
    implement phase closes, nothing on the PR branch should reference them.

    Leaving them on the branch causes two problems:

    1. Concurrent pipelines can write different contents to the same path
       (e.g. two coder runs producing divergent patches), making the
       orchestrator's PR-phase worktree and ``origin/<branch>`` diverge in
       a way that merge/rebase reconcile cannot auto-resolve (see #1731).
    2. The PR itself then ships throwaway artifacts that add noise to
       reviewers' diffs.

    This helper runs once at PR-phase entry, unstages/removes any tracked
    agent-outputs, and commits the cleanup.  If nothing is tracked, it
    no-ops.  All subprocess errors are swallowed with a warning — cleanup
    is best-effort.
    """
    state_dir = worktree_path / ".egg-state" / "agent-outputs"
    logger.info(
        "_cleanup_agent_outputs_for_pr: entering",
        worktree_path=str(worktree_path),
        pipeline_id=pipeline_id,
        agent_outputs_exists=state_dir.exists(),
    )

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_path}",
        "-C",
        str(worktree_path),
    ]

    try:
        # Remove from both the index and the working tree.  ``--ignore-unmatch``
        # makes this a no-op when nothing is tracked under that path.
        # ``-r`` recurses; ``-f`` forces removal even if files were modified.
        subprocess.run(
            [
                *git_base,
                "rm",
                "-rf",
                "--ignore-unmatch",
                "--",
                ".egg-state/agent-outputs",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as rm_err:
        logger.warning(
            "_cleanup_agent_outputs_for_pr: git rm failed — continuing",
            pipeline_id=pipeline_id,
            stderr=rm_err.stderr,
        )
        return
    except subprocess.TimeoutExpired:
        logger.warning(
            "_cleanup_agent_outputs_for_pr: git rm timed out — continuing",
            pipeline_id=pipeline_id,
        )
        return

    # Only commit when the index actually changed (idempotent on re-runs).
    try:
        diff_result = subprocess.run(
            [*git_base, "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "_cleanup_agent_outputs_for_pr: git diff --cached timed out — continuing",
            pipeline_id=pipeline_id,
        )
        return
    if diff_result.returncode == 0:
        logger.info(
            "_cleanup_agent_outputs_for_pr: nothing tracked — skipping commit",
            pipeline_id=pipeline_id,
        )
        return

    try:
        subprocess.run(
            [
                *git_base,
                "commit",
                "--no-verify",
                "-m",
                "Remove ephemeral agent-output handoff artifacts (#1731)",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        logger.info(
            "_cleanup_agent_outputs_for_pr: commit succeeded",
            pipeline_id=pipeline_id,
        )
    except subprocess.CalledProcessError as commit_err:
        logger.warning(
            "_cleanup_agent_outputs_for_pr: commit failed — continuing",
            pipeline_id=pipeline_id,
            stderr=commit_err.stderr,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "_cleanup_agent_outputs_for_pr: commit timed out — continuing",
            pipeline_id=pipeline_id,
        )


def _ensure_statefiles_on_branch(
    worktree_repo_path: Path,
    pipeline: Pipeline,
) -> bool:
    """Verify the contract file exists in the worktree and re-create if missing.

    This is a safety net for short-flow pipelines where the initial contract
    push may have failed or where subsequent pushes diverged.

    Returns True if the contract exists (or was successfully restored),
    False if restoration failed.
    """
    from egg_contracts.loader import contract_exists, create_contract, get_contract_path

    # Contract lookup uses pipeline.id directly (canonical key).
    if contract_exists(pipeline.id, worktree_repo_path):
        return True

    canonical_path = get_contract_path(pipeline.id, worktree_repo_path)

    logger.warning(
        "Contract file missing from worktree — attempting restoration",
        pipeline_id=pipeline.id,
        expected_path=str(canonical_path),
    )

    try:
        if pipeline.issue_number is not None:
            issue_url = f"https://github.com/{pipeline.repo}/issues/{pipeline.issue_number}"
            create_contract(
                issue_number=pipeline.issue_number,
                title=f"Issue #{pipeline.issue_number}",
                url=issue_url,
                pipeline_id=pipeline.id,
                repo_root=worktree_repo_path,
            )
        else:
            create_contract(
                pipeline_id=pipeline.id,
                title=(pipeline.prompt or "")[:100],
                repo_root=worktree_repo_path,
            )

        # Restore plan/analysis drafts from remote if missing locally.
        # These were pushed during init but may be lost from the worktree
        # after agent activity during the implement phase (#1454).
        if pipeline.branch:
            git_base = [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                f"safe.directory={worktree_repo_path}",
                "-C",
                str(worktree_repo_path),
            ]
            # Ensure remote-tracking ref is fresh before reading from it.
            try:
                subprocess.run(
                    [*git_base, "fetch", "origin", pipeline.branch],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except Exception:
                pass  # Best-effort; git show may still work with cached ref
            for draft_phase in ("plan", "refine"):
                draft_rel = _get_draft_path(
                    draft_phase,
                    issue_number=pipeline.issue_number,
                    pipeline_id=pipeline.id,
                    mode=getattr(pipeline, "mode", None),
                )
                if not draft_rel:
                    continue
                draft_path = worktree_repo_path / draft_rel
                if draft_path.exists():
                    continue
                try:
                    result = subprocess.run(
                        [*git_base, "show", f"origin/{pipeline.branch}:{draft_rel}"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    if result.returncode == 0 and result.stdout:
                        draft_path.parent.mkdir(parents=True, exist_ok=True)
                        draft_path.write_text(result.stdout, encoding="utf-8")
                        logger.info(
                            "Restored draft from remote branch",
                            pipeline_id=pipeline.id,
                            draft_path=draft_rel,
                        )
                except Exception as e:
                    logger.warning(
                        "Could not restore draft from remote",
                        pipeline_id=pipeline.id,
                        draft_path=draft_rel,
                        error=str(e),
                    )

        # Final fallback: write plan/analysis from pipeline model if still
        # missing after remote restoration attempt.  This handles the case
        # where the draft was never pushed to the remote (#1460).
        for draft_phase, field_value in [("plan", pipeline.plan), ("refine", pipeline.analysis)]:
            if not field_value:
                continue
            draft_rel = _get_draft_path(
                draft_phase,
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline.id,
                mode=getattr(pipeline, "mode", None),
            )
            if not draft_rel:
                continue
            draft_path = worktree_repo_path / draft_rel
            if draft_path.exists():
                continue
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text(field_value, encoding="utf-8")
            logger.info(
                "Restored draft from pipeline model (remote unavailable)",
                pipeline_id=pipeline.id,
                draft_path=draft_rel,
            )

        # Re-populate tasks and PR metadata from plan draft if available.
        # Without this, recreated contracts lose the planner-generated PR
        # title/description and fall back to the generic pipeline ID title.
        # See: https://github.com/jwbron/egg/issues/1432
        _populate_contract_from_plan(
            worktree_repo_path,
            pipeline.id,
            pipeline.mode.value if pipeline.mode else "issue",
            pipeline.issue_number,
        )

        # File-staging identifier still uses _pipeline_identifier convention.
        identifier = _pipeline_identifier(pipeline.issue_number, pipeline.id)
        _commit_statefiles_to_worktree(
            worktree_repo_path,
            f"Restore missing contract for {identifier}",
            pipeline_identifier=identifier,
            pipeline_id=pipeline.id,
        )
        logger.info(
            "Contract file restored successfully",
            pipeline_id=pipeline.id,
        )
        return True
    except Exception as restore_err:
        logger.error(
            "Failed to restore contract file",
            pipeline_id=pipeline.id,
            error=str(restore_err),
        )
        return False


def _detect_default_branch(worktree_repo_path: Path) -> str:
    """Detect the remote's default branch from a worktree.

    Tries in order:
    1. origin/HEAD symbolic ref (most reliable)
    2. origin/main
    3. origin/master
    4. Fallback to "main"

    Returns:
        The branch name (e.g., "main" or "master"), without the "origin/" prefix.
    """
    # Try origin/HEAD symbolic ref
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            ref = result.stdout.strip()  # e.g. "origin/main"
            return ref.removeprefix("origin/")
    except Exception:
        pass

    # Try origin/main
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/main"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return "main"
    except Exception:
        pass

    # Try origin/master
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/master"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return "master"
    except Exception:
        pass

    logger.warning(
        "Could not detect default branch, falling back to 'main'",
        worktree_path=str(worktree_repo_path),
    )
    return "main"


def get_pr_base_branch(
    pr_number: int | None,
    repo: str | None = None,
    *,
    worktree_repo_path: Path | None = None,
) -> str:
    """Resolve the base branch for a PR, falling back to the repo's default branch.

    .. deprecated::
        Prefer :func:`_fetch_pr_state` for babysit-pr pipelines — it returns
        the full PR state (base_ref, head_ref, head_sha, is_fork) in a single
        ``gh`` call. This helper is kept as a thin single-field shim for
        callers that only need the base branch (and for backwards-compatible
        test coverage in ``orchestrator/tests/test_pr_base_branch.py``).

    Fallback order:
    1. If ``pr_number`` is provided, consult ``gh pr view <N> --json baseRefName``
       (optionally pinning ``--repo`` when ``repo`` is supplied).
    2. If ``worktree_repo_path`` is provided, delegate to
       :func:`_detect_default_branch` which probes ``origin/HEAD`` and then
       ``origin/main``/``origin/master``.
    3. Literal ``"main"`` as an absolute fallback.

    Args:
        pr_number: GitHub PR number. When ``None``, the PR lookup is skipped.
        repo: Repository in ``owner/name`` format. When provided, passed to
            ``gh`` via ``--repo`` so the lookup is unambiguous even from a
            worktree without a configured remote.
        worktree_repo_path: Path of a local clone to fall back to when no
            PR context is available. When ``None``, skips the local probe.

    Returns:
        The bare branch name (e.g. ``"main"`` or ``"develop"``), never prefixed
        with ``"origin/"``.
    """
    # Primary: ask GitHub via the gh CLI.
    if pr_number is not None:
        gh_cmd = ["gh", "pr", "view", str(pr_number), "--json", "baseRefName"]
        if repo:
            gh_cmd.extend(["--repo", repo])
        try:
            result = subprocess.run(
                gh_cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                    ref = data.get("baseRefName")
                    if isinstance(ref, str) and ref:
                        return ref
                except json.JSONDecodeError, ValueError:
                    logger.warning(
                        "get_pr_base_branch: gh output was not valid JSON; falling back",
                        pr_number=pr_number,
                        repo=repo,
                    )
            else:
                logger.warning(
                    "get_pr_base_branch: gh pr view failed; falling back",
                    pr_number=pr_number,
                    repo=repo,
                    returncode=result.returncode,
                    stderr=result.stderr.strip()[:200],
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "get_pr_base_branch: gh pr view raised; falling back",
                pr_number=pr_number,
                repo=repo,
                error=str(exc),
            )

    # Secondary: probe the local clone's default branch.
    if worktree_repo_path is not None:
        try:
            return _detect_default_branch(worktree_repo_path)
        except Exception:
            pass

    # Absolute fallback.
    return "main"


def _resolve_origin_ref(base_branch: str | None) -> str:
    """Return ``origin/<branch>``, falling back to ``origin/main``.

    Centralises the ``f"origin/{base_branch}" if base_branch else "origin/main"``
    pattern so every orient-prompt / diff-command call site honours the
    resolved base branch consistently.
    """
    ref = (base_branch or "main").strip() or "main"
    # Tolerate callers that already passed ``origin/<x>`` by mistake.
    if ref.startswith("origin/"):
        return ref
    return f"origin/{ref}"


def _verify_pr_head_unchanged(pipeline, worktree_repo_path: Path) -> tuple[bool, str | None]:
    """Return (ok, actual_sha) for the babysit-pr final-push head-move guard.

    Fetches ``origin`` and resolves ``origin/<pipeline.branch>`` (the PR
    head branch) inside ``worktree_repo_path``, then compares the remote
    tip against ``pipeline.pr_head_sha`` captured at pipeline creation.

    - Returns ``(True, <sha>)`` when the remote head still matches the
      stored SHA (safe to push).
    - Returns ``(True, None)`` when the stored SHA or branch is unknown —
      there is nothing to compare against, so we do not block (but
      callers may choose to still warn).
    - Returns ``(False, <actual_sha>)`` when the remote head has advanced.
      Callers should abort the final push and raise a HITL decision.

    The helper never raises.  Git/subprocess failures are retried once;
    if both attempts fail the function returns ``(False, None)`` so that
    callers treat the result as "unsafe" and escalate via HITL rather
    than silently allowing a push that might overwrite concurrent work.
    """
    stored_sha = getattr(pipeline, "pr_head_sha", None)
    branch = getattr(pipeline, "branch", None)
    if not stored_sha or not branch:
        return True, None

    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            fetch = subprocess.run(
                ["git", "-C", str(worktree_repo_path), "fetch", "origin", branch],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if fetch.returncode != 0:
                logger.warning(
                    "_verify_pr_head_unchanged: fetch failed (attempt %d/%d)",
                    attempt,
                    max_attempts,
                    pipeline_id=getattr(pipeline, "id", None),
                    branch=branch,
                    stderr=fetch.stderr.strip()[:200],
                )
                if attempt < max_attempts:
                    continue
                return False, None
            rev = subprocess.run(
                ["git", "-C", str(worktree_repo_path), "rev-parse", f"origin/{branch}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "_verify_pr_head_unchanged: git raised (attempt %d/%d)",
                attempt,
                max_attempts,
                pipeline_id=getattr(pipeline, "id", None),
                branch=branch,
                error=str(exc),
            )
            if attempt < max_attempts:
                continue
            return False, None
        if rev.returncode != 0:
            logger.warning(
                "_verify_pr_head_unchanged: rev-parse failed (attempt %d/%d)",
                attempt,
                max_attempts,
                pipeline_id=getattr(pipeline, "id", None),
                branch=branch,
                stderr=rev.stderr.strip()[:200],
            )
            if attempt < max_attempts:
                continue
            return False, None
        actual = rev.stdout.strip()
        if not actual:
            if attempt < max_attempts:
                continue
            return False, None
        return actual == stored_sha, actual

    return False, None  # pragma: no cover - unreachable but defensive


def _fetch_pr_state(pr_number: int, repo: str | None = None) -> dict[str, Any]:
    """Fetch PR state, base/head refs, and fork-hint via ``gh pr view``.

    Returns a dict with keys ``state`` (str, e.g. "OPEN"/"MERGED"/"CLOSED"),
    ``base_ref`` (str or None), ``head_ref`` (str or None), ``head_sha``
    (str or None), ``is_fork`` (bool), ``changed_files`` (int), and
    ``head_repository_name_with_owner`` (str or None).  Returns an empty
    dict when ``gh`` is unavailable or the PR cannot be looked up.
    """
    if pr_number is None:
        return {}
    fields = (
        "state,baseRefName,headRefName,headRefOid,isCrossRepository,"
        "changedFiles,headRepositoryOwner,headRepository"
    )
    cmd = ["gh", "pr", "view", str(pr_number), "--json", fields]
    if repo:
        cmd.extend(["--repo", repo])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "_fetch_pr_state: gh pr view raised",
            pr_number=pr_number,
            repo=repo,
            error=str(exc),
        )
        return {}
    if result.returncode != 0:
        logger.warning(
            "_fetch_pr_state: gh pr view failed",
            pr_number=pr_number,
            repo=repo,
            returncode=result.returncode,
            stderr=result.stderr.strip()[:200],
        )
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError, ValueError:
        return {}

    head_repo = data.get("headRepository") or {}
    head_owner = data.get("headRepositoryOwner") or {}
    head_repo_name = head_repo.get("name") if isinstance(head_repo, dict) else None
    head_owner_login = head_owner.get("login") if isinstance(head_owner, dict) else None
    head_repo_full = (
        f"{head_owner_login}/{head_repo_name}" if head_owner_login and head_repo_name else None
    )
    return {
        "state": data.get("state"),
        "base_ref": data.get("baseRefName"),
        "head_ref": data.get("headRefName"),
        "head_sha": data.get("headRefOid"),
        "is_fork": bool(data.get("isCrossRepository")),
        "changed_files": data.get("changedFiles") or 0,
        "head_repository_name_with_owner": head_repo_full,
    }


def _handle_pr_creation_failure(
    pipeline_id: str,
    current_phase: str,
    store,
    reason: str | None = None,
) -> None:
    """Mark a pipeline as FAILED after PR creation returns no URL.

    Extracted from ``_health_monitor_poll`` so this state-transition logic can
    be tested independently of the full polling loop.

    The error message attached to the pipeline tells the user exactly what
    happened and how to rescue the work.  The agents' commits are on
    ``origin/<pipeline.branch>`` regardless of the failure mode, so the
    rescue is always "open the PR manually against that branch" — we
    surface the exact ``gh pr create`` invocation to avoid forcing users
    to dig through orchestrator logs (see #1731).

    ``reason`` is a short phrase explaining *why* PR creation failed (e.g.
    ``"fetch+rebase reconcile failed"``).  When omitted, the generic
    ``"no PR URL returned"`` message is used for back-compat.
    """
    reason_text = reason or "no PR URL returned"
    error_msg = f"Auto PR creation failed: {reason_text}"
    logger.error(error_msg, pipeline_id=pipeline_id, reason=reason_text)
    with get_pipeline_state_lock(pipeline_id):
        pipeline = store.load_pipeline(pipeline_id)
        phase_execution = pipeline.get_phase_execution(current_phase)
        # Compose a user-facing message that includes the rescue hint,
        # using pipeline state we only have access to inside the lock.
        rescue_hint = _format_rescue_hint(pipeline)
        full_error = f"{error_msg}\n{rescue_hint}" if rescue_hint else error_msg
        phase_execution.status = PipelineStatus.FAILED
        phase_execution.error = full_error
        phase_execution.completed_at = datetime.now(UTC)
        pipeline.status = PipelineStatus.FAILED
        pipeline.error = full_error
        store.save_pipeline(pipeline)


def _format_rescue_hint(pipeline) -> str:
    """Build a user-facing rescue hint for a pipeline whose PR couldn't be auto-created.

    Returns an empty string when we don't have enough state to compose a
    useful hint (no repo or no branch on the pipeline) — in that case the
    error log + pipeline ID are the user's only handholds.
    """
    repo = getattr(pipeline, "repo", None)
    branch = getattr(pipeline, "branch", None)
    if not repo or not branch:
        return ""
    base = getattr(pipeline, "base_branch", None) or "main"
    return (
        f"Agent work is on origin/{branch} in {repo}. "
        f"To open the PR manually:\n"
        f"  gh pr create --repo '{repo}' --head '{branch}' --base '{base}' "
        f'--title "..." --body "..."'
    )


def _finalize_pr_phase_failed(
    pipeline,
    worktree_repo_path: Path,
    spawner,
    store,
    pipeline_id: str,
    current_phase: str,
    gateway_mode: Literal["public", "private"],
    push_ok: bool,
) -> bool:
    """Create the PR (possibly against a stale remote HEAD) and persist state.

    Called at the end of the auto-PR branch of the PR phase.  Factored out
    of ``_health_monitor_poll`` so the reconcile-failure / fallback
    behavior described in jwbron/egg#1731 can be unit-tested independently
    of the full polling loop.

    ``push_ok`` reflects whether the preceding
    :func:`GatewayClient.push_worktree_branch` call succeeded (the client
    reconciles non-fast-forward rejections internally via fetch+rebase+retry;
    see #1706/#1731/#1808).  Regardless,
    we call :func:`_auto_create_pr` — when ``push_ok`` is False the PR is
    opened against whatever is currently on ``origin/<pipeline.branch>``
    (the agents' work), dropping the orchestrator's housekeeping commits
    rather than failing the whole pipeline.

    Returns ``True`` when the phase failed (no PR URL), ``False`` when the
    PR was created successfully (URL captured).  The name explicitly
    encodes the return-value semantics: ``if _finalize_pr_phase_failed(...):``.
    Side effects: persists ``pr_url`` artifact on success, or marks the
    pipeline FAILED with a rescue hint on failure.
    """
    pr_url = _auto_create_pr(pipeline, worktree_repo_path, spawner, gateway_mode=gateway_mode)

    if pr_url:
        # Parse the PR number from the URL so downstream consumers
        # (overseer, get_pipeline_snapshot, babysit-worker handoffs) can
        # rely on ``pipeline.pr_number`` directly instead of re-deriving
        # it from the ``pr_url`` artifact.  Match mirrors ``_get_pr_info``.
        match = re.search(r"/pull/(\d+)", pr_url)
        parsed_pr_number = int(match.group(1)) if match else None
        # Best-effort lookup of the created PR's head SHA so we can also
        # populate ``pipeline.pr_head_sha``.  Failures here must not fail
        # the PR phase — leave ``pr_head_sha`` null and proceed.  The
        # read-from-gh (vs. push-intent) is correct because the #1731
        # fallback path may have opened the PR against the remote HEAD
        # rather than our locally-pushed commit.
        head_sha: str | None = None
        if parsed_pr_number is not None:
            # ``_fetch_pr_state`` already returns {} on any internal
            # failure (gh missing, JSON parse error, non-zero exit),
            # so we don't need an outer try/except wrapper here.
            pr_state = _fetch_pr_state(parsed_pr_number, pipeline.repo)
            candidate = pr_state.get("head_sha") if isinstance(pr_state, dict) else None
            if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{7,40}", candidate):
                head_sha = candidate
        with get_pipeline_state_lock(pipeline_id):
            reloaded = store.load_pipeline(pipeline_id)
            phase_execution = reloaded.get_phase_execution(current_phase)
            phase_execution.artifacts = {"pr_url": pr_url}
            if parsed_pr_number is not None:
                reloaded.pr_number = parsed_pr_number
            if head_sha is not None:
                reloaded.pr_head_sha = head_sha
            store.save_pipeline(reloaded)
        return False

    failure_reason = (
        "gateway push rejected and fetch+rebase reconcile failed, "
        "then fallback PR against remote HEAD also returned no URL"
        if not push_ok
        else "no PR URL returned"
    )
    _handle_pr_creation_failure(pipeline_id, current_phase, store, reason=failure_reason)
    return True


BRC_HISTORY_TYPES = frozenset(
    {
        "CONSENSUS_PROPOSE",
        "CONSENSUS_ACK",
        "CONSENSUS_NACK",
        "CONSENSUS_WITHDRAW",
        "CONSENSUS_CONFIRMED",
        "CONSENSUS_RE_REVIEW",
        # In-cycle conditional-ACK obligation resolution (#2338). Captured
        # in the BRC history file so the audit trail survives orchestrator
        # teardown — closes the gap that resolution was previously only
        # an in-memory event.
        "CONSENSUS_OBLIGATION_RESOLVED",
        "STATUS",
        "HANDOFF",
        "AGENT_FAILED",
        "NUDGE",
        "OVERSEER_ALERT",
        # HEARTBEAT (issue #1897) — structured per-agent state messages.
        "HEARTBEAT",
        # QUESTION removed per issue #1897 Phase 7.  The enum member
        # remains for backward-compat until the tester updates
        # test_brc_history / test_checkpoint fixtures; see
        # MessageType.QUESTION.
    }
)


def _get_message_store():
    """Import and return the message store factory function, or None if unavailable."""
    try:
        from message_store import get_message_store
    except ImportError:
        try:
            from ..message_store import get_message_store  # type: ignore[import-not-found]
        except ImportError:
            return None
    return get_message_store


def _write_brc_history(
    worktree_path: Path,
    pipeline_id: str,
    phase: str,
    identifier: int | str,
) -> None:
    """Write BRC consensus message history for a phase to .egg-state.

    Retrieves BRC-related messages for the given phase from the message store
    and writes them as a chronological markdown log to
    ``.egg-state/brc-history/{identifier}-{phase}.md``.
    No-ops gracefully when the message store is unavailable or contains no
    BRC messages for the pipeline and phase.

    Args:
        worktree_path: Path to the worktree repo directory
        pipeline_id: The pipeline ID to retrieve messages for
        phase: The pipeline phase name (e.g. "implement", "plan")
        identifier: The pipeline identifier for file naming
    """
    logger.info(
        "_write_brc_history: entering",
        pipeline_id=pipeline_id,
        phase=phase,
        identifier=str(identifier),
    )

    store_fn = _get_message_store()
    if store_fn is None:
        logger.info(
            "_write_brc_history: early return — message store unavailable",
            pipeline_id=pipeline_id,
            phase=phase,
        )
        return

    try:
        store = store_fn()
        messages = store.get_messages(pipeline_id, limit=10000)
    except Exception as e:
        logger.warning(
            "_write_brc_history: early return — failed to retrieve messages",
            pipeline_id=pipeline_id,
            phase=phase,
            error=str(e),
        )
        return

    if not messages:
        logger.info(
            "_write_brc_history: early return — no messages in store",
            pipeline_id=pipeline_id,
            phase=phase,
        )
        return

    brc_messages = [m for m in messages if m.message_type in BRC_HISTORY_TYPES and m.phase == phase]
    if not brc_messages:
        logger.info(
            "_write_brc_history: early return — no BRC messages for phase",
            pipeline_id=pipeline_id,
            phase=phase,
            total_messages=len(messages),
        )
        return

    # Format as markdown.  `Generated:` is derived from the latest
    # message timestamp (not wall-clock time) so regenerating the
    # file from the same message set produces byte-identical output.
    # This keeps the PR-phase safety-net rewrite
    # (_rewrite_brc_history_for_pr) idempotent: when no new BRC
    # messages arrived between phase completion and PR creation,
    # the rewritten file matches the previous commit and the
    # follow-up commit is skipped by _commit_statefiles_to_worktree.
    # See #1714.
    message_timestamps = [m.timestamp for m in brc_messages if m.timestamp is not None]
    if message_timestamps:
        generated_str = max(message_timestamps).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        generated_str = "unknown"
    lines: list[str] = []
    lines.append(f"# BRC Consensus History — {phase} phase")
    lines.append("")
    lines.append(f"Generated: {generated_str}")
    lines.append(f"Pipeline: {pipeline_id}")
    lines.append("")

    for msg in brc_messages:
        ts = msg.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if msg.timestamp else "unknown"
        # Include to_role for directed messages (not broadcast "all")
        if msg.to_role and msg.to_role != "all":
            header = (
                f"### [{ts}] {msg.from_role} → {msg.to_role} ({msg.message_type}): {msg.subject}"
            )
        else:
            header = f"### [{ts}] {msg.from_role} ({msg.message_type}): {msg.subject}"
        lines.append(header)
        if msg.body:
            lines.append("")
            lines.append(msg.body)

        # Emit a YAML metadata block with id, phase, and non-empty metadata
        meta_block: dict[str, Any] = {}
        if msg.id:
            meta_block["id"] = msg.id
        if msg.phase:
            meta_block["phase"] = msg.phase
        if msg.metadata:
            meta_block["metadata"] = msg.metadata
        if meta_block:
            lines.append("")
            lines.append("````yaml")
            lines.append(
                yaml.safe_dump(meta_block, sort_keys=False, default_flow_style=False).rstrip()
            )
            lines.append("````")
        lines.append("")

    history_dir = worktree_path / ".egg-state" / "brc-history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_file = history_dir / f"{identifier}-{phase}.md"

    # Write the markdown history file
    try:
        history_file.write_text("\n".join(lines))
    except Exception as md_err:
        logger.warning(
            "Failed to write BRC history markdown file",
            pipeline_id=pipeline_id,
            phase=phase,
            error=str(md_err),
        )

    # Write a JSON companion artifact containing the full message dicts
    json_file = history_dir / f"{identifier}-{phase}.json"
    try:
        json_data = [msg.to_dict() for msg in brc_messages]
        json_file.write_text(json.dumps(json_data, indent=2, default=str))
    except Exception as json_err:
        logger.warning(
            "Failed to write BRC history JSON companion file",
            pipeline_id=pipeline_id,
            phase=phase,
            error=str(json_err),
        )

    logger.info(
        "Wrote BRC history file",
        pipeline_id=pipeline_id,
        phase=phase,
        path=str(history_file),
        message_count=len(brc_messages),
    )


def _rewrite_brc_history_for_pr(
    worktree_path: Path,
    pipeline_id: str,
    pipeline_phases: dict,
    identifier: int | str,
) -> None:
    """Re-write BRC history for all completed phases before PR creation.

    Iterates ``pipeline_phases`` (a mapping of phase name → phase execution
    objects with a ``.status`` attribute) and calls :func:`_write_brc_history`
    for each phase whose status is ``PipelineStatus.COMPLETE``.

    Errors from individual phase writes are logged at warning level and
    do not prevent other phases from being processed.

    After re-writing history files, commits the results via
    :func:`_commit_statefiles_to_worktree`.  Commit failures are also
    logged and swallowed so the PR creation can proceed.
    """
    completed_phases = [
        name for name, ex in pipeline_phases.items() if ex.status == PipelineStatus.COMPLETE
    ]
    logger.info(
        "_rewrite_brc_history_for_pr: entering",
        pipeline_id=pipeline_id,
        total_phases=len(pipeline_phases),
        completed_phase_count=len(completed_phases),
        completed_phases=completed_phases,
    )
    for phase_name, phase_exec in pipeline_phases.items():
        if phase_exec.status == PipelineStatus.COMPLETE:
            try:
                _write_brc_history(
                    worktree_path,
                    pipeline_id,
                    phase_name,
                    identifier,
                )
            except Exception as brc_err:
                logger.warning(
                    "Failed to re-write BRC history for PR (continuing)",
                    pipeline_id=pipeline_id,
                    phase=phase_name,
                    error=str(brc_err),
                )
    try:
        _commit_statefiles_to_worktree(
            worktree_path,
            "Persist BRC history files for PR",
            pipeline_identifier=identifier,
            pipeline_id=pipeline_id,
        )
        logger.info(
            "_rewrite_brc_history_for_pr: commit step completed successfully",
            pipeline_id=pipeline_id,
        )
    except subprocess.CalledProcessError as git_err:
        logger.warning(
            "Failed to commit BRC history for PR (continuing)",
            pipeline_id=pipeline_id,
            error=str(git_err),
        )
    logger.info(
        "_rewrite_brc_history_for_pr: exiting",
        pipeline_id=pipeline_id,
    )


def _resolve_pipeline_worktree_path(pipeline: Pipeline, fallback: Path) -> Path:
    """Resolve the on-disk worktree path for *pipeline*.

    Prefers ``WORKTREE_BASE_DIR / pipeline.id / <repo_short>`` when it
    exists (the same layout _run_pipeline materialises at spawn time;
    see pipelines.py spawn block).  Falls back to *fallback* — typically
    the state store's ``repo_path`` — when no worktree is materialised.
    """
    repo_short = pipeline.repo.split("/")[-1] if pipeline.repo else None
    if repo_short:
        candidate = WORKTREE_BASE_DIR / pipeline.id / repo_short
        if candidate.exists():
            return candidate
    pipeline_wt_dir = WORKTREE_BASE_DIR / pipeline.id
    if pipeline_wt_dir.exists():
        # sorted() for deterministic selection when multiple subdirs exist
        for sub in sorted(pipeline_wt_dir.iterdir()):
            if sub.is_dir() and (sub / ".git").exists():
                return sub
    return fallback


def _persist_phase_brc_history(
    pipeline: Pipeline,
    store: StateStore,
    phase: str,
) -> None:
    """Persist BRC history for *phase* and commit it, best-effort.

    Mirrors the per-phase write+commit sequence that ``_run_pipeline``
    runs inline at phase completion, so external phase-transition paths
    (the ``complete_phase`` / ``advance_phase`` REST+MCP handlers) do
    not silently drop BRC transcripts when ``_clear_concurrent_state``
    wipes the message store.  See #1827.

    Note: this commits but does **not** push.  Callers must ensure a
    push happens downstream — in ``advance_phase`` the spawned
    ``_run_pipeline`` thread pushes the branch, carrying this commit
    along; in a standalone ``complete_phase`` the caller is expected to
    trigger a subsequent advance or push.
    """
    worktree_path = _resolve_pipeline_worktree_path(pipeline, store.repo_path)
    try:
        _write_brc_history(
            worktree_path,
            pipeline.id,
            phase,
            _brc_history_identifier(pipeline),
        )
    except Exception as brc_err:
        logger.warning(
            "Failed to persist BRC history before phase transition (continuing)",
            pipeline_id=pipeline.id,
            phase=phase,
            error=str(brc_err),
        )
        return

    try:
        _commit_statefiles_to_worktree(
            worktree_path,
            f"Persist statefiles after {phase} phase",
            pipeline_identifier=_pipeline_identifier(pipeline.issue_number, pipeline.id),
        )
    except subprocess.CalledProcessError as git_err:
        logger.warning(
            "Failed to commit BRC history before phase transition (continuing)",
            pipeline_id=pipeline.id,
            phase=phase,
            error=str(git_err),
        )


def _build_pre_merge_obligations_section(
    pipeline_id: str,
    contract_deferred_actions: list[Any] | None = None,
) -> str:
    """Render the "Pre-merge Obligations" section from active conditional ACKs.

    Two sources, in order of preference:

    1. ``contract_deferred_actions`` — ``DeferredAction`` objects (or legacy
       strings) previously persisted to ``contract.pr.deferred_actions`` when
       a human approved the conditional-ACK HITL gate (#2004). This is the
       durable path: the tracker may have been torn down by the time PR
       creation runs, and the contract survives.
    2. The live consensus tracker (#1998). Used when the contract has
       no deferred_actions — either because the gate landed before
       tracker teardown, or the gate was never required.

    The markdown composition (open vs. resolved sections, banner copy) is
    delegated to :mod:`orchestrator.pr_obligations` so the slice-DAG
    umbrella PR path (``GatewayClient.create_slice_pr``) renders the same
    section from the same input shape (#2354).

    Returns an empty string if neither source yields obligations, so callers
    can unconditionally append the result to the PR body.
    """
    try:
        from pr_obligations import render_obligations_section_from_normalized
    except ImportError:
        from ..pr_obligations import (  # type: ignore[import-not-found,no-redef]
            render_obligations_section_from_normalized,
        )
    obligations = _collect_pre_merge_obligations(pipeline_id, contract_deferred_actions)
    return render_obligations_section_from_normalized(obligations)


def _collect_pre_merge_obligations(
    pipeline_id: str,
    contract_deferred_actions: list[Any] | None,
) -> list[dict[str, str]]:
    """Normalize obligations from contract or live tracker into a uniform shape.

    Returns a list of ``{reviewer, condition, resolved_in_diff}`` dicts. The
    contract source takes precedence over the live tracker when present.

    .. note::

       The tracker fallback is functionally a no-op when called from the
       slice-DAG umbrella path (``_run_one_slice_inner`` →
       ``create_slice_pr``). ``get_peer_consensus_tracker(pipeline_id)``
       returns the **pipeline-level** tracker, but slice-mode BRC
       consensus runs on per-slice trackers keyed
       ``{pipeline_id}/{slice_id}`` (see ``peer_consensus._tracker_key``)
       — so any slice-BRC ACK obligations are written to the per-slice
       tracker and the pipeline-level tracker won't see them. In
       practice ``contract.pr.deferred_actions`` (populated by
       ``_persist_deferred_actions`` when HITL resolves) is therefore the
       only effective source for the slice umbrella PR. The fallback is
       still wired in for call-shape parity with the legacy
       ``_auto_create_pr`` path so future changes (e.g. aggregating
       slice obligations onto the pipeline tracker) get parity for
       free — see PR #2382 review observation A.
    """
    try:
        from pr_obligations import normalize_deferred_actions
    except ImportError:
        from ..pr_obligations import (  # type: ignore[import-not-found,no-redef]
            normalize_deferred_actions,
        )
    normalized = normalize_deferred_actions(contract_deferred_actions)
    if normalized:
        return normalized

    # Tier 2 — live tracker (pre-#2004 path; kept so conditions still
    # render if the HITL gate hasn't resolved yet, e.g. under force=true).
    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[import-not-found]
    tracker = get_peer_consensus_tracker(pipeline_id)
    if tracker is None:
        return []
    try:
        conditions = tracker.get_pre_merge_conditions()
    except Exception as e:  # defensive — never block PR creation on this
        logger.warning(
            "Failed to read pre-merge conditions from tracker",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return []

    tracker_normalized: list[dict[str, str]] = []
    for c in conditions:
        condition = str(c.get("condition", "")).strip()
        if not condition:
            continue
        tracker_normalized.append(
            {
                "reviewer": str(c.get("reviewer", "") or "").strip(),
                "condition": condition,
                "resolved_in_diff": str(c.get("resolved_in_diff", "") or "").strip(),
            }
        )
    return tracker_normalized


def _build_brc_history_link_line(
    worktree_repo_path: Path,
    identifier: int | str | None,
) -> str:
    """Build a one-line pointer to the committed BRC history transcripts.

    Scans ``.egg-state/brc-history/`` for ``{identifier}-<phase>.md`` files
    written by :func:`_write_brc_history` and returns a sentence linking
    each phase's transcript, ordered by canonical execution order
    (``refine`` → ``plan`` → ``implement`` → ``pr``; unknown names sorted
    alphabetically after).

    Returns an empty string when ``identifier`` is ``None`` or no
    transcripts exist on disk.
    """
    if identifier is None:
        return ""
    history_dir = worktree_repo_path / ".egg-state" / "brc-history"
    if not history_dir.is_dir():
        return ""
    prefix = f"{identifier}-"
    phases: list[str] = []
    for path in history_dir.glob(f"{prefix}*.md"):
        stem = path.stem
        if stem.startswith(prefix):
            phases.append(stem[len(prefix) :])
    if not phases:
        return ""

    canonical = [p.value for p in PipelinePhase]
    rank = {name: i for i, name in enumerate(canonical)}
    phases.sort(key=lambda name: (rank.get(name, len(canonical)), name))

    links = ", ".join(
        f"[`{phase}`](./.egg-state/brc-history/{identifier}-{phase}.md)" for phase in phases
    )
    return f"_Per-phase BRC transcripts: {links}._"


def _pr_metadata_from_plan_draft(
    worktree_repo_path: Path,
    issue_number: int | None,
    pipeline_id: str,
) -> tuple[str | None, str, str, str, list[str], str | None]:
    """Parse PR metadata from the plan draft on disk.

    Used as a fallback in ``_build_pr_body`` when ``contract.pr`` is not
    populated — e.g. when the plan-phase contract write did not reach the
    branch tip (see #1829). The plan draft itself is reliably on the
    branch even when the contract is not.

    Returns ``(title, description, test_plan, manual_steps, warnings,
    draft_rel_path)``. ``title`` is ``None`` when the draft is missing,
    unparseable, or has no ``pr:`` block, signalling the caller to fall
    through to the next tier. ``warnings`` is a list of
    human-readable parse warning strings collected from ``parse_plan``
    (empty when the parse was clean or the draft was absent); it is
    surfaced in the PR body when the caller falls through to the stub
    tier so reviewers can see what went wrong (see #1975).
    ``draft_rel_path`` is the relative path to the draft that was
    parsed, or ``None`` if no draft was attempted.
    """
    warnings_out: list[str] = []
    draft_rel = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        return None, "", "", "", warnings_out, None
    plan_path = worktree_repo_path / draft_rel
    if not plan_path.exists():
        warnings_out.append(f"Plan draft not found at {draft_rel}")
        return None, "", "", "", warnings_out, draft_rel
    try:
        from egg_contracts.plan_parser import parse_plan

        result = parse_plan(plan_path.read_text())
    except Exception as e:
        logger.debug(
            "Could not parse plan draft for PR metadata fallback",
            path=str(plan_path),
            error=str(e),
        )
        warnings_out.append(f"parse_plan raised: {e}")
        return None, "", "", "", warnings_out, draft_rel
    for w in result.warnings:
        msg = w.message
        if w.context:
            msg = f"{msg} ({w.context})"
        warnings_out.append(msg)
    if not result.pr_title:
        return None, "", "", "", warnings_out, draft_rel
    return (
        result.pr_title,
        result.pr_description or "",
        result.pr_test_plan or "",
        result.pr_manual_steps or "",
        warnings_out,
        draft_rel,
    )


def _build_github_staging_manual_step(worktree_repo_path: Path) -> str:
    """Render the auto manual-step for `.github-staging/` files (issue #2508).

    Producer agents (coder, etc.) cannot push to `.github/` because the
    gateway blocks the path as a branch-protection invariant.  When a
    plan calls for CI workflow or CODEOWNERS changes, the agent instead
    writes the proposed end-state to top-level `.github-staging/`,
    mirroring the `.github/` structure.  This helper scans that
    directory and returns a markdown step the human reviewer must
    complete before merge: review the staged files, move them into
    `.github/`, delete the staging dir, and push the resulting commit.

    Returns an empty string when `.github-staging/` is absent or empty.
    """
    staging_dir = worktree_repo_path / ".github-staging"
    # Drop the whole step when ``.github-staging`` itself is a symlink:
    # ``Path.is_dir()`` follows symlinks, so without this guard a
    # ``.github-staging -> /etc`` (or any other host path) would let
    # ``rglob`` enumerate the link target's files into the manual-step
    # file list, polluting the PR body with arbitrary host-filesystem
    # paths. Mirrors the per-entry symlink guard below.
    if staging_dir.is_symlink():
        return ""
    if not staging_dir.is_dir():
        return ""

    staged_paths: list[str] = []
    for path in sorted(staging_dir.rglob("*")):
        # Skip symlinks: ``Path.is_file()`` follows them, so without this
        # guard a staged ``.github-staging/evil.yml`` → ``/etc/passwd``
        # would be surfaced in the manual-step file list, the reviewer's
        # ``git mv`` would preserve it, and ``.github/evil.yml`` would
        # land in the repo as a symlink. The reviewer's only mitigation
        # would be the diff (where a symlink shows as a small mode
        # change that's easy to skim past). Drop staged symlinks here so
        # the helper is the choke point.
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(worktree_repo_path).as_posix()
        except ValueError:
            continue
        staged_paths.append(rel)

    if not staged_paths:
        return ""

    lines = [
        "### Move staged `.github/` changes (auto-generated, issue #2508)",
        "",
        "This PR includes proposed `.github/` changes under `.github-staging/`. "
        "Agent roles cannot push to `.github/` directly (CI workflow / CODEOWNERS "
        "branch-protection invariant), so the agent staged the proposed "
        "end-state for human review.",
        "",
        "Staged files:",
    ]
    for rel in staged_paths:
        lines.append(f"- `{rel}`")
    lines.extend(
        [
            "",
            "Before merging:",
            "",
            "1. Review each staged file for correctness — these are proposed "
            "CI / repo-config changes that bypass the agent's normal sandbox.",
            "2. Move each file from `.github-staging/<path>` to `.github/<path>`. For example:",
            "   ```",
            "   mkdir -p .github/workflows",
            "   git mv .github-staging/workflows/test-e2e.yml .github/workflows/test-e2e.yml",
            "   ```",
            "   After the `git mv`, `.github-staging/` is no longer tracked "
            "by git (git doesn't track empty directories). Run "
            "`rm -rf .github-staging` locally if you want to clear any "
            "leftover empty subdirectories from your worktree.",
            "3. Commit the move and push from a context with the GitHub "
            "`workflow` scope (a normal user push works; the bot token may "
            "not — see issue #2508 layer 2).",
        ]
    )
    return "\n".join(lines)


def _build_pr_body(
    pipeline: Pipeline,
    worktree_repo_path: Path,
) -> tuple[str, str, bool]:
    """Build a PR title and body from contract state.

    Uses the planner-generated PR metadata from the contract when available,
    falling back to the plan draft on disk (#1829) and then to the issue
    title.  Commit logs and diff stats are omitted because GitHub already
    displays them natively on the PR page, and including them caused
    body-size blowups (see #1374).

    Args:
        pipeline: The pipeline state
        worktree_repo_path: Path to the worktree repo directory

    Returns:
        Tuple of (title, body, used_stub_fallback).  ``used_stub_fallback``
        is True when neither the contract nor the plan draft produced a
        PR title and the implementation dropped through to the issue
        title / generic stub (see #1975).  Callers use this to mark the
        PR as draft so reviewers notice the planner metadata is missing.
    """
    identifier = _pipeline_identifier(pipeline.issue_number, pipeline.id)
    pr_title: str | None = None
    pr_description: str | None = None
    pr_test_plan: str = ""
    pr_manual_steps: str = ""
    pr_deferred_actions: list[Any] = []
    issue_title: str | None = None
    plan_draft_warnings: list[str] = []
    plan_draft_path: str | None = None
    parsed_plan_draft: bool = False

    # Tier 1: load PR metadata from the contract (populated by the plan agent).
    # Contracts are keyed by pipeline_id after key unification (#1773).
    try:
        from egg_contracts.loader import load_contract

        contract = load_contract(pipeline.id, worktree_repo_path)
        if contract.pr:
            pr_title = contract.pr.title
            pr_description = contract.pr.description
            pr_test_plan = contract.pr.test_plan
            pr_manual_steps = contract.pr.manual_steps
            pr_deferred_actions = list(contract.pr.deferred_actions)
        if contract.issue:
            issue_title = contract.issue.title
    except Exception as e:
        logger.debug(
            "Could not load contract for PR metadata",
            pipeline_id=pipeline.id,
            error=str(e),
        )

    # Tier 2: parse the plan draft directly when the contract has no PR
    # metadata.  The draft is reliably on the branch even when the
    # contract write didn't land (#1829).
    if not pr_title:
        parsed_plan_draft = True
        (
            draft_title,
            draft_desc,
            draft_test_plan,
            draft_manual_steps,
            plan_draft_warnings,
            plan_draft_path,
        ) = _pr_metadata_from_plan_draft(
            worktree_repo_path,
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
        )
        if draft_title:
            pr_title = draft_title
            pr_description = draft_desc
            pr_test_plan = draft_test_plan
            pr_manual_steps = draft_manual_steps

    # Tier 3: issue title, then generic stub
    used_stub_fallback = False
    if not pr_title:
        used_stub_fallback = True
        pr_title = issue_title or f"Implementation for pipeline {pipeline.id}"

    # Assemble body
    body_parts: list[str] = []

    # Fallback banner: when tier-3 fired, surface the failure loudly on
    # the PR itself so reviewers don't silently merge a PR whose title is
    # just "Issue #N" (see #1975). Parse warnings from the tier-2
    # attempt (if any) are listed verbatim so the reader can see the
    # specific yaml-tasks problem instead of only finding it in
    # orchestrator logs.
    if used_stub_fallback:
        banner_lines = [
            "> ⚠️ **Automated PR metadata fell back to the issue title.**",
            "> The plan draft's `pr:` block was missing or could not be parsed,",
            "> so this PR body is a stub. Opened as a draft to block merge.",
        ]
        if plan_draft_path:
            banner_lines.append(f"> Draft: `{plan_draft_path}`")
        if plan_draft_warnings:
            banner_lines.append("> Parse warnings:")
            for msg in plan_draft_warnings:
                banner_lines.append(f"> - {msg}")
        elif parsed_plan_draft and plan_draft_path:
            banner_lines.append("> No `pr.title` found in the plan draft's yaml-tasks block.")
        banner_lines.append("> Repair the plan draft and re-run `populate_contract` (see #1974).")
        body_parts.append("\n".join(banner_lines))

    if pr_description:
        body_parts.append(pr_description)
    elif pipeline.issue_number:
        body_parts.append(f"Closes #{pipeline.issue_number}")

    # Pre-merge obligations from conditional ACKs (issue #1998, #2004).
    # Rendered high in the body so the merger sees them before skimming
    # past the test plan. Prefer the contract-persisted list (written when
    # the #2004 HITL gate resolves as approve+accept) so obligations
    # survive tracker teardown; fall back to the live tracker for the
    # transitional case where the gate hasn't resolved yet.
    deferred_section = _build_pre_merge_obligations_section(
        pipeline.id,
        contract_deferred_actions=pr_deferred_actions,
    )
    if deferred_section:
        body_parts.append(deferred_section)

    # Test plan section (always present — placeholder if missing)
    if pr_test_plan:
        body_parts.append(f"## Test Plan\n\n{pr_test_plan}")
    else:
        body_parts.append("## Test Plan\n\n_No test plan provided by the planner._")

    # Auto-generated step for `.github-staging/` (issue #2508): the
    # gateway blocks every producer role from pushing to `.github/`,
    # so agents drop proposed CI workflow / CODEOWNERS changes into
    # top-level `.github-staging/` instead. Detect them here and
    # surface a step the human reviewer must complete before merge.
    github_staging_step = _build_github_staging_manual_step(worktree_repo_path)

    # Manual steps section: planner-supplied steps and the staging-dir
    # auto-step are rendered together so reviewers see one block.
    manual_step_chunks: list[str] = []
    if pr_manual_steps:
        manual_step_chunks.append(pr_manual_steps)
    if github_staging_step:
        manual_step_chunks.append(github_staging_step)
    if manual_step_chunks:
        body_parts.append("## Manual Steps\n\n" + "\n\n".join(manual_step_chunks))

    # Add pipeline context section
    if pipeline.id or pipeline.issue_number:
        context_parts = ["## Pipeline Context\n"]
        if pipeline.id:
            context_parts.append(f"Pipeline: `{pipeline.id}`")
        if pipeline.issue_number:
            context_parts.append(f"Issue: #{pipeline.issue_number}")
        body_parts.append("\n".join(context_parts))

    # One-line pointer to committed BRC history transcripts.  The full
    # per-phase record lives on the PR branch under .egg-state/brc-history/
    # (see #1828 for why the old inline BRC Consensus Summary was removed).
    brc_link_line = _build_brc_history_link_line(worktree_repo_path, identifier)
    if brc_link_line:
        body_parts.append(brc_link_line)

    body_parts.append("Authored-by: egg")

    body = "\n\n".join(body_parts)

    return pr_title, body, used_stub_fallback


def _auto_create_pr(
    pipeline: Pipeline,
    worktree_repo_path: Path,
    spawner: "ContainerSpawner",  # noqa: UP037
    gateway_mode: Literal["public", "private"] = "public",
) -> str | None:
    """Auto-create a PR for a pipeline without spawning an agent.

    Builds the PR title/body from contract state, then creates the PR
    via the gateway.

    Args:
        pipeline: The pipeline state
        worktree_repo_path: Path to the worktree repo directory
        spawner: Container spawner (used to access gateway client)
        gateway_mode: Session mode for the gateway ("public" or "private")

    Returns:
        PR URL if creation succeeded, None otherwise
    """
    if not pipeline.repo or not pipeline.branch:
        logger.warning(
            "Cannot auto-create PR: missing repo or branch",
            pipeline_id=pipeline.id,
        )
        return None

    # Resolve base branch: explicit > auto-detected from repo
    base = pipeline.base_branch
    if not base:
        base = get_default_branch(worktree_repo_path)

    title, body, used_stub_fallback = _build_pr_body(pipeline, worktree_repo_path)

    # Force draft when PR metadata fell through to the generic stub
    # (see #1975).  A draft PR is the loudest signal GitHub offers to
    # stop a human from silently merging a planner-broken PR whose
    # title is just "Issue #N".
    draft = (gateway_mode == "private") or used_stub_fallback
    if used_stub_fallback:
        logger.warning(
            "Auto PR opened as draft: planner metadata fallback used",
            pipeline_id=pipeline.id,
        )

    # Refresh the pipeline branch against current
    # ``origin/<base_branch>`` so the PR opens with a clean linear
    # diff (#2224 PR 2).  Phase-start rebases
    # (``_rebase_pipeline_branch_onto_base``) only run once per phase
    # iteration; if ``base_branch`` advanced *during* the PR phase,
    # the pipeline branch is now behind.  The helper is best-effort —
    # on any failure (rebase conflict, push reject, transient gateway
    # error) the PR still opens against the un-rebased tip and the
    # divergence becomes visible to the human reviewer.  Only
    # ``pipeline.branch`` is rewritten; ``base_branch`` is never
    # modified or pushed to.
    try:
        _refresh_pipeline_branch_against_current_base(
            spawner=spawner,
            pipeline_id=pipeline.id,
            worktree_repo_path=worktree_repo_path,
            pipeline_branch=pipeline.branch,
            base_branch=base,
            gateway_mode=gateway_mode,
        )
    except Exception as e:
        # Defensive — the helper already swallows its own errors, but a
        # bug in the helper itself must not block PR creation.
        logger.warning(
            "pr-open rebase helper raised; opening PR against un-rebased tip",
            pipeline_id=pipeline.id,
            error=str(e),
        )

    try:
        pr_url = spawner.gateway.create_pr(
            pipeline_id=pipeline.id,
            repo=pipeline.repo,
            title=title,
            body=body,
            head=pipeline.branch,
            base=base,
            issue_number=pipeline.issue_number,
            agent_role="orchestrator",
            mode=gateway_mode,
            draft=draft,
        )
        return pr_url
    except Exception as e:
        logger.error(
            "Auto PR creation failed",
            pipeline_id=pipeline.id,
            error=str(e),
        )
        return None


# Shared PR description guidance injected into planner prompts.
# Kept as a constant so both _build_phase_prompt and _build_agent_prompt
# stay in sync when the guidance evolves.
_PR_DESCRIPTION_GUIDANCE = [
    "**PR description quality**: The `pr.description` field becomes the PR body "
    "that reviewers read first. Write 2-3 paragraphs following this structure:",
    "1. **Context** — what problem exists and why it matters",
    "2. **Changes** — what this PR does, with specifics (e.g. numbered list of "
    "key changes with bold headers)",
    "3. **Impact** — what behavior changes for users or other components",
    "",
    "Do NOT write a one-liner — reviewers need enough detail to understand "
    "the problem, the approach, and why it was chosen without reading every file.",
]

_PR_DESCRIPTION_YAML_EXAMPLE = [
    "    Explain the problem or need this PR addresses and why it matters.",
    "",
    "    Describe the key changes, ideally as a numbered or bulleted list",
    "    with bold headers so reviewers can scan quickly. For each change,",
    "    explain what it does and why.",
    "",
    "    Summarize the impact — what changes for users, callers, or other",
    "    components as a result.",
]

# YAML safety guidance for planner prompts. Plain (unquoted) scalars break
# when they contain ``: `` sequences — e.g. "Add `sequence: int = 0` field"
# parses as a nested mapping and raises ScannerError. Block scalars (``|-``)
# take the whole indented block literally, so backticks, colons, quotes, and
# other punctuation are safe. See issue #1974.
_YAML_TASKS_SAFETY_GUIDANCE = [
    "**YAML safety**: Use block scalars (`|-`) for every prose field — "
    "`name`, `goal`, `description`, `acceptance`. Plain unquoted scalars "
    "break when the text contains `` `code: type` ``, colons in URLs, or "
    "other `: ` sequences, because PyYAML reads them as nested mappings "
    "and the parser drops back to markdown fallback (silently losing the "
    "`pr:` block). Follow the example above literally — do not inline these "
    "values on the same line as the key.",
]


def _build_phase_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    prompt: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    review_feedback: str | None = None,
    review_cycle: int = 0,
    repo_path: str | None = None,
) -> str:
    """Build a phase-specific prompt for the sandbox Claude invocation.

    Follows a structured prompt format:
    Context → Task → Restrictions → Completion.
    """
    # --- Context header ---
    lines = [f"You are in the **{phase}** phase of the SDLC pipeline.\n"]
    lines.append("## Context\n")
    lines.append(f"Pipeline ID: {pipeline_id}")
    lines.append(f"Phase: {phase}")
    if repo:
        lines.append(f"Repository: {repo}")
    if branch:
        lines.append(f"Branch: {branch}")
    if issue_number is not None:
        lines.append(f"Issue: #{issue_number}")
    lines.append("")

    # --- Prior review feedback (agentic revision cycles OR HITL phase reset) ---
    # Feedback can arrive on cycle 0 when a human rejects a phase_gate with
    # change_approach/request_changes — the HITL handler resets review_cycles
    # to 0 and stores the feedback in phase_execution.hitl_feedback, which
    # flows back here via the review_feedback parameter (#1915).
    if review_feedback:
        if review_cycle > 0:
            lines.append(f"## Prior Review Feedback (Cycle {review_cycle})\n")
        else:
            lines.append("## Prior Review Feedback\n")
        has_tester_findings = TESTER_FINDINGS_HEADER in review_feedback
        if phase == "implement":
            revision_action = "Address the feedback below and revise your implementation."
        else:
            revision_action = (
                "Address the feedback below and revise your draft **in-place** "
                "(overwrite the same file)."
            )
        if review_cycle == 0:
            consensus_override = (
                " Even if an existing draft appears "
                "to have reached consensus previously, that consensus is "
                "superseded — you must revise to address this feedback before "
                "proposing a new consensus."
            )
        else:
            consensus_override = ""
        if has_tester_findings:
            lines.append(
                "The reviewer and tester found issues with your previous work. "
                f"{revision_action}{consensus_override}\n"
            )
        else:
            preamble_noun = "implementation" if phase == "implement" else "draft"
            lines.append(
                f"The reviewer found issues with your previous {preamble_noun}. "
                f"{revision_action}{consensus_override}\n"
            )
        lines.append(review_feedback)
        lines.append("")

    # --- Task description ---
    # Skip re-embedding the full task description on revision cycles for
    # implement phase — the coder already knows the task from cycle 0.
    if prompt and not (phase == "implement" and review_cycle > 0):
        lines.append("## Task Description\n")
        lines.append(prompt)
        lines.append("")

    # --- Phase-specific instructions ---
    lines.append("## Your Task\n")

    # Get the correct draft path based on mode
    analysis_path = _get_draft_path("refine", issue_number=issue_number, pipeline_id=pipeline_id)
    plan_path = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)

    if phase == "refine":
        lines.extend(
            [
                "Analyze this issue and produce a structured analysis document. Your goal is to:\n",
                "1. Understand the problem or feature request",
                "2. Research the current codebase to understand existing patterns",
                "3. Research externally when the task involves third-party libraries, APIs, "
                "or integrations — use WebSearch and WebFetch (when available) to look up "
                "current documentation, best practices, and known issues. Skip external "
                "research for purely internal changes where codebase context is sufficient.",
                "4. Identify constraints and dependencies",
                "5. Consider multiple implementation approaches",
                "6. Recommend an approach with justification",
                "7. Surface **all** questions and uncertainties that need human input "
                "(do not self-limit — raise every ambiguity)",
                "",
                "**IMPORTANT**: Do NOT create an implementation plan, task breakdown, "
                "or phased rollout. That is the **plan** phase's job. Stay focused on "
                "**analysis**: understanding the problem, researching the codebase, "
                "evaluating options, and surfacing decisions for the human.",
                "",
                "## Output Format\n",
                "Create an analysis document following the template below. The "
                "fenced block is the **template literal** — copy it as-is and fill "
                "in the bracketed placeholders. The unfenced sections that follow "
                "(`## How to Populate Open Questions`, `## Complexity Assessment`) "
                "are **meta-guidance** — do **not** transcribe them into your "
                "analysis document.\n",
                "````markdown",
                "# Analysis: [Issue Title]\n",
                "> Issue: #[number] | Phase: refine\n",
                "## Problem Statement\n",
                "[Describe the problem or feature request. "
                "What is the current state? What is the desired outcome?]\n",
                "## Current Behavior\n",
                "[Describe how the system currently works in the relevant area. "
                "Include code references where helpful.]\n",
                "## Constraints\n",
                "- [Technical constraints (compatibility, performance, security)]",
                "- [Business constraints (timeline, scope)]",
                "- [Dependencies on other systems or features]\n",
                "## Options Considered\n",
                "### Option A: [Name]\n",
                "**Approach**: [Brief description]\n",
                "**Pros**:",
                "- [Advantage 1]\n",
                "**Cons**:",
                "- [Disadvantage 1]\n",
                "### Option B: [Name]\n",
                "**Approach**: [Brief description]\n",
                "**Pros**:",
                "- [Advantage 1]\n",
                "**Cons**:",
                "- [Disadvantage 1]\n",
                "## Recommended Approach\n",
                "[Which option is recommended and why. Reference the option above.]\n",
                "## Open Questions\n",
                "[Register every open question by following the protocol in "
                "`## How to Populate Open Questions` below the template, then paste "
                "the markdown output of each registration command into this section. "
                "Do **not** copy the protocol instructions themselves into this "
                "document.]\n",
                "---\n",
                "*Authored-by: egg*",
                "````\n",
                "",
                "## How to Populate Open Questions\n",
                "These instructions tell you how to handle the `## Open Questions` "
                "section of the template above. They are **meta-guidance**, not "
                "template content — do **not** transcribe this section into the "
                "analysis document you write.\n",
                "**Every open question MUST be registered as a contract decision or "
                "feedback item using `egg-contract`.** Do not just write questions "
                "as prose — they will not be seen by the human unless registered.\n",
                "**Skip already-resolved questions.** If the Task Description above "
                "includes an `## Additional Context` section, treat anything addressed "
                "there as already decided by the operator (those came from a pre-refine "
                "HITL round). Do NOT call `egg-contract add-decision` or "
                "`egg-contract add-feedback` for questions whose answers are already "
                "captured in `## Additional Context` — re-registering them wastes turns "
                "and produces no-op decisions. Read that section first; if it settles "
                "anything, list those items in a `### Resolved in Pre-Refine` "
                "subsection at the top of `## Open Questions` (one bullet per resolved "
                "item, citing the answer). Only register questions that go beyond what "
                "`## Additional Context` covers.\n",
                "Surface **all** uncertainties, ambiguities, and assumptions that need "
                "human input. Do not limit yourself to a small number — every genuine "
                "ambiguity, missing requirement, unstated assumption, or design choice "
                "that could go multiple ways should be raised here. It is far better to "
                "ask too many questions than to proceed with incorrect assumptions.\n",
                "**Multiple-choice questions** — RUN this command for each question "
                "where the human must pick from discrete options:",
                "```bash",
                'egg-contract add-decision --question "Which approach should we use?" \\',
                '  --options "Option A" "Option B" "Option C" --format markdown',
                "```",
                "Copy the markdown output into your analysis. The human can check "
                'a checkbox to select an option. An "Other (explain in reply)" '
                "option is auto-appended.\n",
                "**Open-ended questions** — EXECUTE this command for free-form "
                "questions where you need the human to provide text answers:",
                "```bash",
                "egg-contract add-feedback \\",
                '  --question "What is the expected request volume?" \\',
                '  --question "Are there any constraints on third-party dependencies?" \\',
                "  --format markdown",
                "```",
                "This creates a dedicated comment for the human to fill in answers. "
                'They edit the comment to add their responses and check "Submit '
                'feedback" when done. The pipeline will resume with the feedback '
                "available in the contract.\n",
                "**DO NOT:**",
                "- Write questions as plain markdown text without running "
                "`egg-contract add-decision` or `egg-contract add-feedback`",
                "- Use custom HTML comment markers like "
                "`<!-- DECISION: ... -->` instead of the contract CLI",
                "- Skip registration because you think the questions are minor — "
                "register every question",
                "- Transcribe this `## How to Populate Open Questions` section "
                "into your analysis document — it is meta-guidance, not template "
                "content\n",
                "",
            ]
        )
        lines.extend(
            [
                "## Complexity Assessment\n",
                "After completing your analysis, assess the task complexity:",
                "- **low**: Single-file change, straightforward bug fix, small config update, typo fix",
                "- **medium**: Multi-file change with clear scope, feature addition with known patterns",
                "- **high**: Architectural change, new subsystem, cross-cutting concern, "
                "many independent phases that could be parallelized",
                "",
            ]
        )
        lines.extend(
            [
                f"Write your analysis to `{analysis_path}`.",
                "Commit and push the draft when done.\n",
                "**IMPORTANT**: Do NOT post your analysis directly to the issue. "
                "The pipeline will have an internal reviewer check your analysis. "
                "If revisions are needed, you'll be re-invoked with feedback. "
                "Only after internal review passes will the analysis be posted "
                "for human approval.",
                "",
            ]
        )

    elif phase == "plan":
        lines.extend(
            [
                "Create a detailed implementation plan.",
                "",
                "**CRITICAL CONSTRAINT — One Issue = One Workflow = One PR.**",
                "All tasks belong to a single pull request. Use phases and commits to",
                "organise the work within that PR — do NOT propose multiple PRs.",
                "",
                "Steps:",
                "1. Review any prior analysis",
                "2. Break down the work into phases with discrete tasks",
                "3. Define clear acceptance criteria for each task",
                "4. Identify test strategy — what automated tests cover the changes, "
                "and what manual verification is needed",
                "5. Identify any manual pre-merge or post-merge steps "
                "(migrations, config changes, deployments)",
                "6. Consider rollback and risks",
                "",
                "## Output Format",
                "",
                "Write a markdown plan with a **yaml-tasks** structured appendix at the end.",
                "The prose section explains the approach; the appendix is machine-parsed.",
                "",
                *_PR_DESCRIPTION_GUIDANCE,
                "",
                "End your document with a fenced YAML block like this:",
                "",
                "````",
                "```yaml",
                "# yaml-tasks",
                "pr:",
                '  title: "Short imperative summary (≤70 chars)"',
                "  description: |",
                *_PR_DESCRIPTION_YAML_EXAMPLE,
                "  test_plan: |",
                "    - Automated: describe which tests cover the changes",
                "    - Manual: specific steps a reviewer should take to verify",
                "  manual_steps: |",
                "    Pre-merge: any required steps before merging",
                "    Post-merge: any required steps after merging",
                "phases:",
                "  - id: 1",
                "    name: |-",
                "      Phase Name",
                "    goal: |-",
                "      What this phase achieves",
                "    tasks:",
                "      - id: TASK-1-1",
                "        description: |-",
                "          What to do — safe to include `code: type` snippets,",
                "          URLs, and other punctuation inside a block scalar.",
                "        acceptance: |-",
                "          How to verify it is done",
                "        files:",
                "          - path/to/file.py",
                "```",
                "````",
                "",
                *_YAML_TASKS_SAFETY_GUIDANCE,
                "",
                "Do NOT use a `pr_plan` key or propose multiple PRs.",
                "",
                "The `test_plan` field is **required** — describe both automated test "
                "coverage and any manual verification steps. The `manual_steps` field "
                "should list any pre-merge or post-merge actions required by the reviewer "
                "or deployer; use an empty string if none.",
                "",
                f"Write your plan to `{plan_path}`.",
                "Commit and push the draft when done.",
                "",
            ]
        )

    elif phase == "implement":
        # Embed plan or analysis text directly on first cycle
        # (avoids file-I/O turns inside the sandbox).
        draft_embedded = False
        if repo_path and review_cycle == 0:
            draft_text = _read_phase_draft(
                Path(repo_path),
                "plan",
                issue_number=issue_number,
                pipeline_id=pipeline_id,
                branch=branch,
            )
            if draft_text:
                lines.append("## Plan\n")
                lines.append(f"```markdown\n{draft_text}\n```\n")
                draft_embedded = True

            # Embed contract task checklist on first cycle
            contract_tasks = _render_contract_tasks(
                repo_path, pipeline_id, pipeline_mode, issue_number
            )
            if contract_tasks:
                lines.append(contract_tasks)
                lines.append("")

        if review_cycle == 0:
            # Build numbered step list; only include the "review" step
            # when the draft wasn't already embedded above.
            lines.append("Implement the changes described in the task and plan:")
            lines.append("")

            steps: list[str] = []
            if not draft_embedded:
                steps.append("Review the plan (check `.egg-state/drafts/`)")
            steps.extend(
                [
                    "Implement the required changes — when working with third-party "
                    "libraries or APIs, use WebSearch and WebFetch (when available) to "
                    "look up current documentation, usage examples, and best practices",
                    "After completing each plan phase or task group, commit and push "
                    "immediately — do not batch all work into a final commit. Mark "
                    "tasks done: `egg-contract complete-task --task <id> --commit <sha>`",
                    "Run tests to verify correctness, then commit any fixes",
                ]
            )
            for i, step in enumerate(steps, 1):
                lines.append(f"{i}. {step}")
            lines.append("")

            lines.append("## Parallel Execution with Subagents\n")
            lines.append(
                "You have access to Claude Code's **Agent tool** for spawning subagents. "
                "Use it to parallelize independent work:\n"
            )
            lines.append(
                "- If the plan has multiple independent phases or task groups that don't touch "
                "overlapping files, implement them in parallel by launching one subagent per "
                "phase/group."
            )
            lines.append(
                "- Each subagent gets a clear, self-contained prompt describing its scope "
                "(files to modify, tasks to complete, acceptance criteria)."
            )
            lines.append(
                "- Subagents share your working directory and git state. Ensure parallel "
                "subagents work on **non-overlapping files** to avoid conflicts."
            )
            lines.append(
                "- Subagents should only edit files — do NOT stage or commit from subagents. "
                "After each group of parallel subagents completes, **immediately** commit and "
                "push their combined changes before launching the next group."
            )
            lines.append(
                "- After subagents complete, verify the combined changes compile, pass tests, "
                "and integrate correctly. Do NOT defer all commits to the end."
            )
            lines.append(
                "- For small or sequential tasks, just implement directly — don't over-parallelize."
            )
            lines.append("")
        else:
            # Revision cycle: slim delta-focused prompt.
            # Guard: if review_feedback is unexpectedly missing, fall
            # back to including the task description so the coder isn't
            # left with a nearly empty prompt.
            if not review_feedback:
                if prompt:
                    lines.append("## Task Description\n")
                    lines.append(prompt)
                    lines.append("")

            lines.append("## Revision Instructions\n")
            if review_feedback:
                has_tester_findings = TESTER_FINDINGS_HEADER in review_feedback
                if has_tester_findings:
                    lines.extend(
                        [
                            "The reviewer and tester found issues with your implementation. "
                            "Focus on addressing the specific feedback above.\n",
                            "1. Review the feedback in the **Prior Review Feedback** section above",
                            "2. Check `git diff` to understand the current state of changes",
                            f"3. Check `.egg-state/agent-outputs/"
                            f"{_pipeline_identifier(issue_number, pipeline_id)}"
                            f"-tester-output.json` for test failures and gaps",
                            "4. Fix the specific issues raised",
                            "5. Run tests to verify your fixes",
                            "6. Commit with descriptive messages",
                            "",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            "The reviewer found issues with your implementation. "
                            "Focus on addressing the specific feedback above.\n",
                            "1. Review the feedback in the **Prior Review Feedback** section above",
                            "2. Check `git diff` to understand the current state of changes",
                            "3. Fix the specific issues raised by the reviewer",
                            "4. Run tests to verify your fixes",
                            "5. Commit with descriptive messages",
                            "",
                        ]
                    )
            else:
                lines.extend(
                    [
                        "A revision was requested but no specific feedback was provided. "
                        "Review the task description above and check `git diff` for the current state.\n",
                        "1. Review the task description above and check `git diff`",
                        "2. Verify the implementation meets the requirements",
                        "3. Run tests to verify correctness",
                        "4. Commit with descriptive messages",
                        "",
                    ]
                )

        # Contract CLI instructions for both local and issue mode
        lines.extend(
            [
                "Use the contract CLI to track progress incrementally — update after "
                "each commit, not in a batch at the end:",
                "- `egg-contract show` — View current contract state",
                "- `egg-contract complete-task --task <id> --commit <sha>` — Mark task done and link commit",
                "- `egg-contract complete-phase --phase <id> --commit <sha>` — Mark phase done and link commit",
                "- `egg-contract add-commit --task <id> --commit <sha>` — Link commit to task without marking done",
                "",
            ]
        )

    else:
        lines.append(f"Execute the {phase} phase.\n")

    # --- Phase restrictions ---
    lines.append("## Phase Restrictions\n")
    if issue_number is None and phase in ("refine", "plan"):
        lines.extend(
            [
                "In this phase:",
                "- You CAN push state files to git (contracts, drafts, checkpoints)",
                "- You CAN create HITL decisions (egg-contract add-decision)",
                "- You CAN create feedback requests (egg-contract add-feedback)",
                "- You CANNOT push code changes",
                "- You CANNOT create PRs (gh pr create)",
                "- You CANNOT post comments to the GitHub issue (gh issue comment) — write reviews to `.egg-state/reviews/` instead",
                "- You CANNOT edit the GitHub issue (gh issue edit)",
                "- You CAN read and modify local files",
                "- You CAN run tests",
                "- You CAN commit locally",
                "",
            ]
        )
    elif issue_number is None and phase == "implement":
        lines.extend(
            [
                "In this phase:",
                "- You CAN push code changes to git",
                "- You CANNOT push .egg-state/ files (except checkpoints)",
                "- You CANNOT create PRs (gh pr create)",
                "- You CANNOT post comments to the GitHub issue (gh issue comment)",
                "- You CANNOT edit the GitHub issue (gh issue edit)",
                "- You CAN read and modify local files",
                "- You CAN run tests",
                "- You CAN commit locally",
                "",
            ]
        )
    else:
        if phase in ("refine", "plan"):
            lines.extend(
                [
                    "- You CAN write drafts to `.egg-state/drafts/`",
                    "- You CAN push draft files (git push)",
                    "- You CAN create HITL decisions (egg-contract add-decision)",
                    "- You CAN create feedback requests (egg-contract add-feedback)",
                    "- You CANNOT post comments to the GitHub issue (gh issue comment) — write reviews to `.egg-state/reviews/` instead",
                    "- You CANNOT edit the GitHub issue (gh issue edit)",
                    "- You CANNOT create PRs (gh pr create)",
                    "",
                ]
            )
        elif phase == "implement":
            lines.extend(
                [
                    "- You CAN push code (git push)",
                    "- You CAN link commits to tasks (egg-contract add-commit)",
                    "- You CANNOT create PRs (the pipeline manages the PR)",
                    "- You CANNOT post comments to the GitHub issue (gh issue comment)",
                    "- You CANNOT edit the GitHub issue (gh issue edit)",
                    "",
                ]
            )
    # --- Completion ---
    lines.append("## Phase Completion\n")
    if phase in ("refine", "plan"):
        lines.append(
            "When your draft is complete, commit and push it. "
            "The pipeline will have an internal reviewer evaluate your work. "
            "If revisions are needed, you'll be re-invoked with feedback. "
            "Only after internal review passes will the output be posted "
            "for human approval."
        )
    else:
        lines.append(
            "When you have completed your work for this phase, "
            "ensure everything is committed and exit successfully."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-agent execution helpers
# ---------------------------------------------------------------------------


def _build_brc_preamble(
    role_value: str,
    phase: str,
    repo: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    *,
    mode: PipelineMode | None = None,
    pr_number: int | None = None,
) -> str:
    """Build the BRC consensus lifecycle preamble for an agent.

    Returns a formatted string block that can be appended to any agent prompt
    to inject BRC protocol instructions. Used by both the coder/refiner path
    (which delegates to _build_phase_prompt) and the generic multi-agent path.

    Includes:
    - Agent roster showing all active agents and what they produce
    - Role-specific proactive preparation instructions
    - Full BRC lifecycle steps

    Args:
        mode: Pipeline execution mode. Forwarded to producer/reviewer orient
            builders so babysit-pr pipelines receive PR-diff-aware prompts.
        pr_number: GitHub PR number; forwarded with ``mode``.
    """
    try:
        from review_graph import get_review_graph_for_phase

        graph = get_review_graph_for_phase(phase, repo=repo)
        is_producer = graph.is_producer(role_value)
        is_reviewer = graph.is_reviewer(role_value)
        reviewers = graph.reviewers_for(role_value) if is_producer else []
        producers = graph.producers_for(role_value) if is_reviewer else []
        all_roles = sorted(graph.all_roles())
    except Exception:
        is_producer = role_value in (
            "coder",
            "tester",
            "documenter",
            "refiner",
            "architect",
            "task_planner",
            "risk_analyst",
        )
        is_reviewer = role_value in (
            "reviewer_code",
            "reviewer_code_holistic",
            "reviewer_contract",
            "tester",
            "reviewer_refine",
            "reviewer_agent_design",
            "reviewer_plan",
        )
        reviewers = []
        producers = []
        all_roles = []

    lines: list[str] = [
        "\n\n## CRITICAL: BRC Consensus Protocol\n",
        "You are running in CONCURRENT mode with the Broadcast-Review-Converge "
        "(BRC) protocol. Your job is NOT just your task — it is the **full "
        "BRC lifecycle**.\n",
    ]

    if is_producer and is_reviewer:
        role_type_desc = "PRODUCER and REVIEWER (dual role)"
    elif is_producer:
        role_type_desc = "PRODUCER"
    elif is_reviewer:
        role_type_desc = "REVIEWER"
    else:
        role_type_desc = "PARTICIPANT"

    lines.append(f"Your role type: **{role_type_desc}**")
    if reviewers:
        lines.append(f"Your reviewers: {', '.join(reviewers)}")
    if producers:
        lines.append(f"Your assigned producers: {', '.join(producers)}")
    lines.append("")

    # Agent roster: show all active agents and what they do
    if all_roles:
        roster = _build_agent_roster(all_roles, role_value, phase)
        if roster:
            lines.append(roster)

    if is_producer:
        lines.extend(
            [
                "### Producer Lifecycle",
                "1. **ORIENT**: Before starting work, "
                + _build_producer_orientation(
                    role_value,
                    phase,
                    reviewers,
                    branch=branch,
                    base_branch=base_branch,
                    mode=mode,
                    pr_number=pr_number,
                ),
                "2. **WORK**: Complete your assigned task (see Your Task below).",
                "3. **PROPOSE**: When done, run: "
                '`egg-orch consensus propose --summary "..." --artifacts "file1" "file2" '
                '--files-changed "f1.py" "f2.py" --tests-run "test_a" "test_b" '
                '--tasks "task-1-1" "task-1-2" --commit-sha $(git rev-parse HEAD)`. '
                "The `--summary` must be ≥50 chars of substantive content describing what was "
                "built, what was tested, and which contract tasks it satisfies. "
                "Boilerplate like 'looks good' or 'approved' will be rejected.",
                "4. **RESPOND TO REVIEWS**: Poll for ACK/NACK from reviewers with "
                "`egg-orch message wait-loop --for CONSENSUS_ACK "
                "--for CONSENSUS_NACK --for CONSENSUS_RE_REVIEW "
                "--for STATUS --for OVERSEER_ALERT`. Do **not** include "
                "`CONSENSUS_CONFIRMED` in this pre-confirm wait — your own "
                "confirm is part of what generates that signal, so the "
                "orchestrator rejects the wait with HTTP 400 "
                "(#2064, #2482); the confirmed event belongs only in step "
                "6 STAY ALIVE, after your confirm has succeeded. "
                "`STATUS` is required so the orchestrator's directed "
                "**Ready to confirm — all confirm preconditions satisfied** "
                "nudge wakes you (#2531): when every reviewer has already "
                "ACKed the current version, no further `CONSENSUS_ACK` / "
                "`CONSENSUS_NACK` will arrive, and the directed `STATUS` "
                "(metadata `ready_to_confirm: true`) is the only signal "
                "that the global confirm preconditions cleared. On wake, "
                "if the message is the directed *Ready to confirm* nudge, "
                "go straight to step 5 **CONFIRM**; other `STATUS` wakeups "
                "(e.g. *Producer X excused from consensus*) are "
                "informational — re-enter the wait. "
                "On the first NACK, start fixing "
                "immediately — don't wait. **Aggregation is enforced by the "
                "orchestrator, not by you (#2142):** when **two or more distinct "
                "reviewers** have NACKed the current version and you call "
                "`egg-orch consensus propose --changed-artifacts ...` (re-propose), "
                "the call is rejected with HTTP 409 and the response `details` "
                "inline every unresolved NACK (reviewer, reason, artifact_refs). "
                "A single-reviewer NACK does **not** trigger the barrier — there "
                "is nothing to aggregate, so re-propose proceeds normally. Read "
                "every NACK in the rejection, fix them all, and re-propose again "
                "— the retry succeeds once you've been informed of the full set. "
                "Don't re-propose addressing only one reviewer's NACK; the "
                "orchestrator will kick you back with the rest.",
                "5. **CONFIRM**: When all reviewers ACK: `egg-orch consensus confirmed`",
                "6. **STAY ALIVE**: Block on the next BRC event with "
                "`egg-orch message wait-loop --for CONSENSUS_CONFIRMED "
                "--for CONSENSUS_RE_REVIEW --for OVERSEER_ALERT "
                "--timeout 60` until the orchestrator stops you. "
                "**Don't** wrap this in a shell `for i in 1..N` loop; "
                "**don't** prefix it with `sleep N`.  The wait-loop "
                "blocks server-side and returns the moment a NEW BRC "
                "event arrives.  Exit code 0 means act on the returned "
                "message, 1 means the wrapper exhausted retries "
                "(surface it).  Cursor threading across re-entries is "
                "automatic (issue #2323): the CLI persists the response "
                "cursor under "
                "/tmp/egg-wait-cursor-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}-* so "
                "events that land between your call returning and the next "
                "call entering are still delivered, and the send→wait "
                "race is closed without manual `--since` anchoring.  "
                "See docs/reference/agent-wait-patterns.md.",
                "7. **HANDLE RE-REVIEW**: If you receive a `CONSENSUS_RE_REVIEW` message "
                "while staying alive, you MUST act on it — failure to do so will stall "
                "the entire pipeline. If you are a reviewer of the re-proposing producer, "
                "re-review and ACK/NACK the new proposal. Otherwise, re-confirm via "
                "`egg-orch consensus confirmed`. Do NOT ignore these messages.",
                "8. **RESOLVE OBLIGATIONS YOU SATISFY (#2338)**: If you "
                "land a commit that satisfies a *different* producer's "
                "conditional-ACK obligation in-cycle — typical pattern: "
                "the coder is gateway-blocked from a path under `tests/`, "
                "you (as tester) cherry-pick the satisfying commit onto "
                "the branch — call `mcp__brc__resolve_obligation "
                'reviewer_role="<reviewer>" producer_role="<other_producer>" '
                "commit_sha=$(git rev-parse HEAD)` after pushing. The "
                "matrix keeps the obligation text for audit but stops "
                "surfacing it on the PR body and HITL gate. Skip this "
                "for obligations that genuinely require a human at "
                "merge time (deploys, cross-repo flips) — those should "
                "remain visible to the merger. **Resolve before "
                "`complete_phase`**: once the HITL gate has fired and "
                "written the obligation to `contract.pr.deferred_actions`, "
                "calling `resolve_obligation` afterwards does *not* "
                "retroactively unpersist the entry — the obligation will "
                "still appear in the PR body until the next pipeline run. "
                "Resolve early. Producers cannot self-resolve their own "
                "obligations (the orchestrator rejects "
                "`resolver_role == producer_role`), since that would "
                "single-handedly bypass the reviewer's veto.\n",
            ]
        )

    if is_reviewer:
        lines.extend(
            [
                "### Reviewer Lifecycle",
                "1. **PREPARE** (while waiting): "
                + _build_reviewer_preparation(
                    role_value,
                    phase,
                    branch=branch,
                    base_branch=base_branch,
                    mode=mode,
                    pr_number=pr_number,
                ),
                "2. **POLL**: Block on `CONSENSUS_PROPOSE` from assigned producers "
                "with `egg-orch message wait-loop --for CONSENSUS_PROPOSE`.  "
                "`wait-loop` blocks server-side and returns exit 0 the moment "
                "a proposal arrives (stdout has it); exit 1 means a permanent "
                "error (surface it — do NOT retry).  It re-issues the inner "
                "long-poll internally so timeouts never surface to you.  "
                "**Re-enter the same command** after each ACK/NACK to wait "
                "for the next producer's proposal — cursor threading across "
                "these re-entries is automatic (issue #2323): the CLI "
                "persists the response cursor under "
                "/tmp/egg-wait-cursor-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}-* "
                "so a proposal "
                "that lands in the gap between your previous wait returning "
                "and the next one entering is still delivered.  Do NOT "
                "wrap this in a shell `for` loop, do NOT `sleep N`, and "
                "do NOT use bare `egg-orch message wait` here — a bare "
                "`wait` exits 1 on each timeout which the tool surface "
                "renders as an error and invites a tight retry loop "
                "(issue #1943).  Finish your preparation work from "
                "step 1 before entering the wait-loop.",
                "3. **SYNC**: Before reviewing, sync your worktree so you have the "
                "producer's commits: `git fetch origin && git merge "
                + _resolve_origin_ref(branch or base_branch)
                + " --no-edit`",
                "4. **REVIEW**: Once a proposal arrives, form independent judgment from "
                "the referenced code artifacts. Read the actual files — do not rely "
                "solely on the proposal summary.",
                "5. **ACK/NACK**: Your `--reason` IS your review. Put your **full analysis** "
                "there — this is what the producer reads and acts on. **Always "
                "pass `--ack-version` / `--nack-version`** with the producer's "
                "current proposal version (#2142) — read it from the "
                "`CONSENSUS_PROPOSE` message that triggered your review (the "
                "`version` field). The orchestrator rejects the verdict with "
                "`stale_version` if the producer has re-proposed since you "
                "started reviewing.\n"
                "\n"
                "   **NACK format** (use when blocking issues exist):\n"
                "   ```\n"
                '   egg-orch consensus nack <role> --files-reviewed "f1" "f2" '
                '--nack-version <N> --reason "\n'
                "   ### Blocking\n"
                "   1. **file.py:123** — Description of the issue. Fix: suggested fix.\n"
                "   2. **file.py:456** — Description of the issue. Fix: suggested fix.\n"
                "   ### Non-blocking\n"
                "   - **file.py:789** — Suggestion for improvement.\n"
                '   "\n'
                "   ```\n"
                "\n"
                "   **ACK format** (use when no blocking issues):\n"
                "   ```\n"
                '   egg-orch consensus ack <role> --files-reviewed "f1" "f2" '
                '--ack-version <N> --reason "\n'
                "   Reviewed [N files / specific areas]. Verified [what was checked].\n"
                "   [Specific observations about correctness, security, etc.]\n"
                "   ### Non-blocking\n"
                "   - **file.py:123** — Optional suggestions for improvement.\n"
                '   "\n'
                "   ```\n"
                "\n"
                "   **Conditional ACK** (issue #1998 — use when the work is "
                "correct but requires a human action at merge time that agents "
                "cannot perform themselves, e.g. a `git mv`, a secret "
                "rotation, a config flip in another repo): add "
                '`--pre-merge-condition "…"` to the ACK command. The '
                "obligation is recorded on the approval matrix and rendered "
                'as a "Pre-merge Obligations" section high up in the '
                "auto-created PR body so the merger cannot skim past it. Do "
                "NOT use this to smuggle blocking issues past the producer — "
                "if the producer could fix it, NACK instead.\n"
                "   Example:\n"
                "   ```\n"
                '   egg-orch consensus ack <role> --files-reviewed "f1" '
                '--ack-version <N> --reason "Code is correct but …" '
                '--pre-merge-condition "A human must `git mv legacy/x '
                'new/x` before merging — agents cannot push renames through"\n'
                "   ```\n"
                "\n"
                "   **Drop satisfied obligations on re-ACK (#2338).** When "
                "you re-ACK at a new proposal version and the conditioning "
                "work has landed in-cycle (another role cherry-picked the "
                "satisfying commit, the rename is now in the diff, the "
                "obligation is moot), drop the obligation: re-ACK without "
                "`--pre-merge-condition`. Do NOT re-attach the same "
                'obligation with a "Status: satisfied — manual '
                're-verification required" hedge — the PR body renders '
                "obligations verbatim under a `do not merge until "
                "complete` banner, and transcribing a satisfied obligation "
                "produces a self-contradicting PR body. If the satisfier "
                "has called `mcp__brc__resolve_obligation`, the matrix is "
                "already filtering it out, but the resolution resets when "
                "you re-ACK — dropping the obligation is the durable "
                "fix.\n"
                "\n"
                "   **Alternative — keep the obligation but mark it resolved "
                "(#2336).** If you want to preserve the audit trail of the "
                "obligation in the PR body (under a 'Resolved within this "
                "PR' subsection rather than the merge-blocking section), "
                "re-ACK with `--pre-merge-condition-resolved-in-diff <sha>` "
                "in addition to `--pre-merge-condition`. The renderer demotes "
                "the entry instead of dropping it. Prefer this when the "
                "obligation history is useful context for the merger; prefer "
                "the drop path above when the obligation is moot and "
                "transcribing it just adds noise.\n"
                "\n"
                "   `--reason` must be ≥50 chars of substantive content. "
                "Boilerplate like 'lgtm' or 'no issues' will be rejected.\n"
                "\n"
                "   **Stale-version rejection (#2142):** if the producer "
                "re-proposed while your verdict was in flight, your ACK / "
                "NACK is rejected with HTTP 409 and the response `details` "
                "inline the producer's current proposal snapshot "
                "(`current_proposal.version`, `artifacts`, `commit_sha`). "
                "Re-fetch (`git fetch && git merge`), re-review against the "
                "new commit (often a small diff against what you just read), "
                "and re-submit your verdict. Don't retry blindly with the "
                "same payload — the orchestrator will reject again until you "
                "review the current version.",
                "6. **CONFIRM**: When all assigned producers reviewed: "
                "`egg-orch consensus confirmed`",
                "7. **STAY ALIVE**: Block on the next BRC event with "
                "`egg-orch message wait-loop --for CONSENSUS_PROPOSE "
                "--for CONSENSUS_RE_REVIEW --for CONSENSUS_CONFIRMED "
                "--for OVERSEER_ALERT --timeout 60` until the "
                "orchestrator stops you. **Don't** wrap this in a "
                "shell `for i in 1..N` loop; **don't** prefix it with "
                "`sleep N`.  The wait-loop blocks server-side and "
                "returns the moment a NEW BRC event arrives.  Exit 0 "
                "means act on the returned event; exit 1 means the "
                "wrapper exhausted retries (surface it).  Cursor "
                "threading across re-entries is automatic (issue "
                "#2323): the CLI persists the response cursor under "
                "/tmp/egg-wait-cursor-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}-* so "
                "events that land between your call returning and the next "
                "call entering are still delivered, and the send→wait "
                "race is closed without manual `--since` anchoring.  "
                "See docs/reference/agent-wait-patterns.md.",
                "8. **HANDLE RE-REVIEW**: If you receive a `CONSENSUS_RE_REVIEW` message "
                "while staying alive, you MUST act on it — failure to do so will stall "
                "the entire pipeline. Re-review the re-proposing producer's new proposal "
                "and ACK/NACK it, then re-confirm via `egg-orch consensus confirmed`. "
                "Do NOT ignore these messages.\n",
            ]
        )

    # Directed coordination guidance — role-gated
    lines.append("### Directed Coordination")
    lines.append(
        "In addition to the BRC consensus flow (PROPOSE/ACK/NACK), you can send "
        "directed peer-to-peer messages to specific agents using "
        "`egg-orch message send --to <role> --type <TYPE>`. These directed messages "
        "are **supplementary** to BRC consensus — they do NOT replace the "
        "PROPOSE/ACK/NACK lifecycle and are never required for consensus to proceed.\n"
    )

    if is_producer:
        lines.extend(
            [
                "**As a producer**, use directed messages to coordinate handoffs and "
                "broadcast progress:",
                "- **HANDOFF**: When your work is ready for a specific peer to act on, "
                "send a HANDOFF message so they know to begin. For example, a coder "
                "notifying the tester that implementation is complete.",
                "  ```",
                '  egg-orch message send --to tester --type HANDOFF --subject "Auth module ready" '
                '--body "auth.py is complete, tests can begin"',
                "  ```",
                "- **STATUS**: Broadcast progress updates to all agents when you reach "
                "significant milestones (e.g., halfway through implementation, blocked "
                "on a dependency).",
                "  ```",
                '  egg-orch message send --to all --type STATUS --subject "Implementation 50% complete" '
                '--body "Core logic done, working on edge cases"',
                "  ```\n",
            ]
        )

    if is_reviewer:
        lines.extend(
            [
                "**As a reviewer**, when you need clarification before "
                "ACK/NACKing, put the question in your NACK `--reason` "
                "block under `### Non-blocking`.  The producer sees it "
                "atomically with the review verdict and the audit "
                "trail is preserved.  The legacy QUESTION message "
                "type was removed in issue #1897; off-protocol chatter "
                "is no longer advertised.  A follow-up issue will "
                "introduce a structured REQUEST/REPLY subsystem that "
                "names a target peer and times out.",
                "",
            ]
        )

    lines.extend(
        [
            "**If you exit before the orchestrator stops you, you have FAILED your role.** "
            "Completing your task is necessary but NOT sufficient — you must reach "
            "CONFIRMED state and remain alive until consensus.\n",
            "",
        ]
    )

    return "\n".join(lines)


# Role descriptions for agent roster — maps role names to (short description,
# what artifacts they produce).
_ROLE_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "coder": (
        "Implements code changes",
        "commits with source files, tests may be included",
    ),
    "tester": (
        "Writes comprehensive regression tests AND adversarially probes the "
        "coder's implementation for bugs and edge cases (dual role: also "
        "reviews coder)",
        "test files (including failing tests that demonstrate bugs), check "
        "results, gap reports back to the coder",
    ),
    "documenter": (
        "Updates documentation for changes",
        "doc files, README updates, inline documentation",
    ),
    "refiner": (
        "Refines implementation based on review feedback",
        "updated source files addressing review concerns",
    ),
    "architect": (
        "Designs architecture and component structure",
        "architecture analysis, component breakdown",
    ),
    "task_planner": (
        "Breaks work into implementation tasks",
        "task list with acceptance criteria",
    ),
    "risk_analyst": (
        "Assesses technical risks",
        "risk assessment with mitigations",
    ),
    "reviewer_code": (
        "Reviews code quality, correctness, and security",
        "ACK/NACK with file-level feedback",
    ),
    "reviewer_code_holistic": (
        "Holistic single-pass review for cross-module coherence "
        "(use-case end-to-end, doc↔code symmetry, synthetic-key audit, "
        "silent-fallback hunt)",
        "ACK/NACK with cross-module findings",
    ),
    "reviewer_contract": (
        "Verifies implementation matches contract/requirements",
        "ACK/NACK with task-level verification",
    ),
    "reviewer_refine": (
        "Reviews refinement changes",
        "ACK/NACK on refined implementation",
    ),
    "reviewer_agent_design": (
        "Reviews agent design and architecture decisions",
        "ACK/NACK on design choices",
    ),
    "reviewer_plan": (
        "Reviews plan phase outputs",
        "ACK/NACK on architecture, tasks, and risk assessment",
    ),
}


def _build_agent_roster(all_roles: list[str], current_role: str, phase: str) -> str:
    """Build a roster of all active agents for the current phase.

    Shows each agent's role, what they do, and what they produce so that
    every agent understands who else is running and what to expect.
    """
    roster_lines = ["### Active Agents in This Phase\n"]
    roster_lines.append(
        "The following agents are running **simultaneously**. "
        "Each must complete their task AND reach CONFIRMED via BRC.\n"
    )
    for role in all_roles:
        desc, artifacts = _ROLE_DESCRIPTIONS.get(
            role, ("Executes assigned role", "role-specific artifacts")
        )
        marker = " **(you)**" if role == current_role else ""
        roster_lines.append(f"- **{role}**{marker}: {desc}. Produces: {artifacts}.")
    roster_lines.append("")
    return "\n".join(roster_lines)


def _build_reviewer_preparation(
    role_value: str,
    phase: str,
    *,
    branch: str | None = None,
    base_branch: str | None = None,
    mode: PipelineMode | None = None,
    pr_number: int | None = None,
) -> str:
    """Build proactive preparation instructions for reviewer agents.

    Tells reviewers what to do while waiting for proposals — e.g., reading
    the contract, familiarizing themselves with the codebase, preparing
    review criteria. This avoids idle waiting and produces better reviews.

    Args:
        role_value: The reviewer role (e.g. ``reviewer_code``).
        phase: Pipeline phase name.
        branch: The pipeline's work branch, if any.
        base_branch: The resolved base branch for diff/log commands. Falls
            back to ``main`` when ``None``.
        mode: Pipeline execution mode. When :attr:`PipelineMode.BABYSIT`
            or :attr:`PipelineMode.CUSTOM` with a PR target (#1762),
            reviewer text instructs them to orient on the PR diff
            (``base...head``) before producers broadcast.
        pr_number: GitHub PR number (meaningful for BABYSIT and CUSTOM+PR).
    """
    base_ref = _resolve_origin_ref(base_branch)

    # Babysit or CUSTOM+PR: reviewers orient on the PR diff against its
    # configured base branch, as if it were a fresh proposal from the
    # producers (#1748, #1762).
    _is_pr_diff_aware = mode is not None and (
        mode == PipelineMode.BABYSIT or (mode == PipelineMode.CUSTOM and pr_number is not None)
    )
    if _is_pr_diff_aware and phase == "implement":
        pr_hint = f"PR #{pr_number}" if pr_number else "the PR under review"
        # Without an explicit PR-head checkout the worktree sits on the base
        # branch — ``git diff base...HEAD`` would be empty (#1748 reviewer_code
        # B1). ``gh pr checkout`` handles same-repo PRs; forks are already
        # rejected at pipeline-creation time.
        pr_checkout = f"gh pr checkout {pr_number}" if pr_number else "gh pr checkout <pr_number>"
        if role_value == "reviewer_code":
            return (
                f"You are reviewing an existing pull request ({pr_hint}). "
                "Start reviewing immediately: "
                f"(0) check out the PR head into your worktree — `{pr_checkout}` "
                "(required; otherwise the diff below will be empty because your "
                "worktree is on the base branch). "
                "(1) **read the PR diff** at "
                f"`git fetch origin && git diff {base_ref}...HEAD` and form "
                "independent concerns BEFORE producers broadcast. "
                "(a) Note the PR's stated intent (issue/description) for context. "
                "(b) Walk every changed file systematically; identify gaps in "
                "correctness, security, error handling, and test coverage. "
                "(c) Check how the changes integrate with surrounding code. "
                "(d) Draft your ACK/NACK criteria now so you can respond quickly "
                "once producers propose. When reviewing the tester's proposal, "
                "scrutinize the attestation for `tests_run` and "
                "`tests_execution_blocked`: `tests_execution_blocked: true` is a "
                "blocking concern unless clearly documented. "
                "If the tester reports `no_test_changes_needed: true`, walk the "
                "diff and confirm it is genuinely behavior-preserving (symbol "
                "moves, doc-only, etc.) before ACKing — the no-op propose path "
                "is only valid when the slice truly warrants no new tests (#2431). "
                "If the documenter reports `no_doc_changes_needed: true`, walk "
                "the diff and confirm there is genuinely no documented-surface "
                "impact (no public API signature change, no behavior change a "
                "user-facing doc describes, no new feature/flag in README or "
                "docs/, no docstring contract drift) before ACKing — the "
                "documenter's no-op path is only valid when the slice truly "
                "warrants no doc updates (#2444)."
            )
        if role_value == "reviewer_code_holistic":
            return (
                f"You are the holistic reviewer on an existing PR ({pr_hint}). "
                f"(0) Check out the PR head: `{pr_checkout}` (required — "
                "without this your worktree is on the base branch and the "
                "diff below will be empty). "
                "(1) **Skim the full diff once** at "
                f"`git fetch origin && git diff {base_ref}...HEAD` to build "
                "a mental map. Do not verify line-by-line — that is "
                "`reviewer_code`'s line-by-line work. "
                "(a) Note the PR's stated intent (issue / description) — "
                "this is the use case you will walk end-to-end. "
                "(b) Identify the producer / consumer module pairs the diff "
                "touches; you will audit them for synthetic-key and "
                "silent-fallback asymmetries. "
                "(c) Pull every doc claim into a checklist; you will grep "
                "for code that implements each. "
                "(d) Draft your ACK/NACK around the four passes (use case, "
                "doc symmetry, synthetic keys, silent fallbacks)."
            )
        if role_value == "tester":
            return (
                f"You are reviewing an existing pull request ({pr_hint}). "
                f"(0) Check out the PR head first: `{pr_checkout}` — without "
                "this step your worktree is sitting on the base branch and the "
                "diff below will be empty. "
                "(1) Read the PR diff: "
                f"`git fetch origin && git diff {base_ref}...HEAD`. "
                "(a) Identify edge cases and regressions the current tests miss. "
                "(b) Check the existing test infrastructure (frameworks, fixtures). "
                "(c) Draft tests that would lock the desired behaviour so you can "
                "finalize them against the producer's proposal when it arrives."
            )
        # reviewer_contract is filtered out in babysit mode; fall through for
        # any other implement-phase reviewers that land here.

    if phase == "implement":
        if role_value == "reviewer_code":
            return (
                "Start reviewing immediately — do not wait idle for proposals. "
                "(a) Read the contract with `egg-contract show` to understand "
                "what was planned. "
                "(b) Review the issue/PR description for context. "
                "(c) Check for commits on the branch: run "
                f"`git fetch origin && git log --oneline {base_ref}..origin/{branch or '$(git branch --show-current)'}` "
                "and if changes exist, begin reviewing the diff with "
                f"`git diff {base_ref}...HEAD`. "
                "(d) Note existing test patterns and code conventions. "
                "By the time a proposal arrives, you should already have "
                "a thorough understanding of the changes and be ready to "
                "ACK or NACK with specific, detailed feedback. "
                "When reviewing the tester's proposal, check whether tests were "
                "actually executed (look for `tests_run` and `tests_execution_blocked` "
                "in the attestation). If the tester reports `tests_execution_blocked: true`, "
                "this is a blocking concern — NACK unless the limitation is clearly "
                "documented and the tests are syntactically valid. "
                "Also scrutinize low `tests_run` counts relative to change scope — "
                "a multi-file change with only 1 test run warrants investigation. "
                "If the tester reports `no_test_changes_needed: true`, walk the diff "
                "and confirm it is genuinely behavior-preserving (symbol moves, "
                "doc-only, etc.) before ACKing — the no-op propose path is only "
                "valid when the slice truly warrants no new tests (#2431). "
                "If the documenter reports `no_doc_changes_needed: true`, walk "
                "the diff and confirm there is genuinely no documented-surface "
                "impact (no public API signature change, no behavior change a "
                "user-facing doc describes, no new feature/flag in README or "
                "docs/, no docstring contract drift) before ACKing — the "
                "documenter's no-op path is only valid when the slice truly "
                "warrants no doc updates (#2444)."
            )
        elif role_value == "reviewer_code_holistic":
            return (
                "Start preparing immediately — do not wait idle for proposals. "
                "(a) Read the contract with `egg-contract show` to extract "
                "the primary advertised use case (this is the path you will "
                "walk end-to-end once the producer proposes). "
                "(b) Review the issue / PR description and any doc files "
                "the contract names — collect the doc-claimed behaviours "
                "into a checklist for the symmetry pass. "
                "(c) Identify the producer / consumer module pairs the plan "
                "touches; these are where synthetic-key and silent-fallback "
                "asymmetries hide. "
                "(d) Once commits land "
                f"(`git fetch origin && git log --oneline {base_ref}..origin/{branch or '$(git branch --show-current)'}`), "
                f"skim `git diff {base_ref}...HEAD` once with the whole PR "
                "in mind — do not verify line-by-line; defer that to "
                "`reviewer_code`. Your job is the architectural-coherence "
                "question line-by-line review does not own."
            )
        elif role_value == "reviewer_contract":
            return (
                "While waiting for proposals, prepare by: "
                "(a) reading the contract with `egg-contract show` to understand "
                "every task and its acceptance criteria, "
                "(b) reviewing the issue description for original requirements, "
                "(c) noting which tasks are marked as must-have vs nice-to-have. "
                "When proposals arrive, you will verify each task's acceptance "
                "criteria is met — prepare a checklist now."
            )
        elif role_value == "tester":
            return (
                "While waiting for the coder's proposal, prepare by: "
                "(a) reading the contract with `egg-contract show` to understand "
                "what's being implemented, "
                "(b) identifying edge cases and boundary conditions from the "
                "requirements, "
                "(c) checking the existing test infrastructure (test frameworks, "
                "fixtures, test utilities). "
                "Start writing test scaffolding for known requirements while "
                "waiting — you can finalize once you see the actual implementation."
            )
    elif phase == "plan":
        if role_value == "reviewer_plan":
            return (
                "While waiting for proposals, prepare by: "
                "(a) reading the issue description to understand the original "
                "request, "
                "(b) exploring the codebase to understand the current architecture "
                "and components that may be affected, "
                "(c) identifying potential risks or constraints the planners "
                "should address. "
                "Form your own mental model of how you would approach this — "
                "then compare against the proposals when they arrive. "
                "\n\n"
                "**#2137 slice-DAG checks (mandatory):** "
                "(1) **Forest-violation NACK** — if the contract was "
                "rejected at plan ingestion with a "
                "``forest_violation`` log discriminator (or the contract's "
                "``plan_review_feedback`` carries a 'Plan ingestion REJECTED' "
                "block), NACK the planner and cite the structured errors "
                "verbatim. Instruct the planner to re-emit the plan with "
                "``serialized_chain_order`` populated on the downstream "
                "slice. "
                "(2) **Slice-sizing advisory (advisory only — never "
                "NACK)**: for each slice whose estimated LOC "
                "(count of ``files_affected`` × heuristic weight) is "
                ">1,000, surface a non-blocking advisory line in your ACK "
                "body. Tone scales with magnitude: 1,000–2,000 LOC: "
                "'consider splitting'; >2,000 LOC: 'this slice is well "
                "above the soft target — strongly consider splitting'. "
                "Per HITL decision-6 opt-2 the plan reviewer NEVER NACKs "
                "on size — the refiner/operator retains override "
                "authority."
            )
    elif phase == "refine":
        if role_value in ("reviewer_refine", "reviewer_agent_design"):
            return (
                "While waiting for the refiner's proposal, prepare by: "
                "(a) reading the prior review feedback that triggered this "
                "refinement cycle, "
                "(b) checking the current state of the code to understand "
                "what was already implemented, "
                "(c) verifying which review concerns are still outstanding. "
                "When the proposal arrives, focus on whether the specific "
                "feedback items were addressed."
            )

    # Generic fallback
    return (
        "While waiting for proposals, read the contract "
        "(`egg-contract show`), explore the codebase for context, "
        "and prepare your review criteria. "
        "Do NOT inspect producer artifacts before proposals arrive."
    )


def _build_producer_orientation(
    role_value: str,
    phase: str,
    reviewers: list[str],
    branch: str | None = None,
    *,
    base_branch: str | None = None,
    mode: PipelineMode | None = None,
    pr_number: int | None = None,
) -> str:
    """Build orientation instructions for producer agents.

    Tells producers what to research before starting work — understanding
    context, knowing what reviewers will check, and checking existing code
    patterns. This produces higher-quality first proposals and fewer NACKs.

    Args:
        role_value: Producer role (e.g. ``coder``).
        phase: Pipeline phase name.
        reviewers: Names of reviewers that will review this producer.
        branch: The pipeline's working branch, used for sync instructions.
        base_branch: Resolved base branch for rebase/merge targets. Falls
            back to the default branch when ``None``.
        mode: Pipeline execution mode. When :attr:`PipelineMode.BABYSIT`,
            implement-phase producer orient text instructs them to rebase
            the PR base into their worktree, resolve conflicts within their
            role's file scope, and escalate cross-role overlap to the
            on-demand ``conflict_resolver`` role (#1748).
        pr_number: GitHub PR number (only meaningful in babysit mode).
    """
    reviewer_awareness = ""
    if reviewers:
        reviewer_names = ", ".join(reviewers)
        reviewer_awareness = (
            f" Your work will be reviewed by **{reviewer_names}** — "
            "keep their review criteria in mind as you work."
        )

    # Babysit or CUSTOM+PR: producers rebase the PR's base branch into
    # their staging worktree, resolve conflicts only within their own
    # role's file scope, and escalate cross-role overlap via the
    # on-demand `conflict_resolver` role. A soft scope-expansion hint
    # discourages off-diff refactors (#1748, #1762).
    _producer_is_pr_diff_aware = mode is not None and (
        mode == PipelineMode.BABYSIT or (mode == PipelineMode.CUSTOM and pr_number is not None)
    )
    if _producer_is_pr_diff_aware and phase == "implement":
        base_ref = _resolve_origin_ref(base_branch)
        base_label = (base_branch or "the PR base branch").strip() or "the PR base branch"
        pr_hint = f"PR #{pr_number}" if pr_number else "the PR under review"
        # Step (0) is the PR-head checkout — without it the worktree is sitting
        # on the base branch and none of the PR's changes are visible (#1748
        # reviewer_code B1). ``gh pr checkout`` handles same-repo PRs; we
        # require gh to be present in the sandbox (it is for all roles).
        pr_checkout = f"gh pr checkout {pr_number}" if pr_number else "gh pr checkout <pr_number>"
        babysit_preamble = (
            f"you are working on {pr_hint} via a one-off BRC cycle against the "
            "PR diff. Orient in this order: "
            f"(0) **check out the PR head into your worktree** first — run "
            f"`{pr_checkout}`. Without this step your worktree is sitting on "
            "the base branch and **none of the PR's changes are present**; any "
            "work you do will be against the wrong tree. "
            f"(1) fetch the latest base: `git fetch origin {base_label}`, then "
            f"rebase (or merge, if rebase is unsafe) {base_ref} into your "
            "staging worktree. "
            "(2) Resolve any conflicts ONLY within your role's file scope — "
            "do not touch files outside your role's allowed_write patterns. "
            "If a conflict spans another role's files, stop and escalate by "
            "requesting the on-demand `conflict_resolver` role via "
            "`egg-orch message send --to orchestrator --type HANDOFF --subject "
            '"conflict_resolver needed" --body "..."`. '
            f"(3) Read the PR diff at `git diff {base_ref}...HEAD` and the PR "
            "description for intent. "
            "(4) Identify quality/consistency improvements within your role's "
            "scope (better tests, clearer docs, tighter code) — but "
            "**do not refactor outside the diff unless clearly needed** to "
            "land a correct change. Trust the existing PR scope. "
            "(5) Check existing patterns, conventions, and test infrastructure "
            "before making edits." + reviewer_awareness
        )
        return babysit_preamble

    if phase == "implement":
        if role_value == "coder":
            return (
                "read the contract (`egg-contract show`) to understand all tasks "
                "and acceptance criteria. Explore the codebase to find existing "
                "patterns, conventions, and the files you will modify. Check for "
                "existing tests that cover the areas you will change — do not "
                "break them." + reviewer_awareness
            )
        elif role_value == "tester":
            sync_note = ""
            if branch:
                sync_note = (
                    f" Before starting work, sync your worktree: "
                    f"`git fetch origin && git merge origin/{branch} --no-edit`."
                )
            return (
                "read the contract (`egg-contract show`) to understand what is "
                "being implemented. Check the existing test infrastructure — "
                "test frameworks, fixtures, conftest files, and naming conventions. "
                "Identify edge cases from the requirements before writing tests. "
                "**Your mandate is two-fold**: comprehensive regression "
                "coverage AND adversarial probing for bugs the coder missed "
                "— see the *Your Task* → mandate block for the full "
                "instruction (including the failing-test → NACK → HANDOFF "
                "workflow when you catch a coder-side bug). "
                "**Scaffold-first while the coder is producing**: draft test "
                "scaffolding from the plan alone — test file paths from "
                "`tasks[].files`, function signatures from each task's acceptance "
                "criteria, fixture imports, and mock-input scenarios from the YAML. "
                "Leave assertion bodies as TODOs. Do NOT call `wait-loop` for the "
                "coder's CONSENSUS_PROPOSE before drafting these scaffolds — the "
                "scaffold work does not depend on coder output and recovers "
                "downstream-producer time. Your propose-ready iteration should "
                "start at the coder's first commit, not their first propose. "
                "**You MUST propose** even when the slice warrants no new tests "
                "(pure refactor / doc-only / symbol moves with no behavior "
                "change): the BRC consensus blocks until every producer has "
                "proposed (#2431). For that case, run the configured checks "
                "against the coder's diff and use the no-op propose path — "
                "set `attestation.no_test_changes_needed=true` with a non-empty "
                "`no_test_changes_reason` and the usual `checks_passed` list. "
                "Do NOT just heartbeat indefinitely waiting for test work that "
                "isn't there — that deadlocks the slice." + sync_note + reviewer_awareness
            )
        elif role_value == "documenter":
            sync_note = ""
            if branch:
                sync_note = (
                    f" Before starting work, sync your worktree: "
                    f"`git fetch origin && git merge origin/{branch} --no-edit`."
                )
            return (
                "read the contract (`egg-contract show`) to understand what is "
                "being implemented. Check existing documentation structure — "
                "README files, doc directories, inline documentation patterns. "
                "Identify which docs will need updating once the implementation "
                "is complete. "
                "**You MUST propose** even when the slice warrants no doc "
                "updates (pure refactor / test-only / internal-only with no "
                "documented-surface impact): the BRC consensus blocks until "
                "every producer has proposed (#2444, mirror of #2431). For "
                "that case, walk the coder's diff to confirm there is no "
                "doc surface impacted, then use the no-op propose path — "
                "set `attestation.no_doc_changes_needed=true` with a "
                "non-empty `no_doc_changes_reason`. Do NOT just heartbeat "
                "indefinitely waiting for doc work that isn't there — that "
                "deadlocks the slice." + sync_note + reviewer_awareness
            )
    elif phase == "plan":
        if role_value == "architect":
            return (
                "read the issue/task description carefully. Explore the codebase "
                "to understand the current architecture, component boundaries, "
                "and dependencies. Identify the areas that will be affected by "
                "the proposed changes." + reviewer_awareness
            )
        elif role_value == "task_planner":
            return (
                "read the issue/task description carefully. Review the codebase "
                "structure to understand the scope of work. Break the work into "
                "tasks with clear acceptance criteria that reviewers can verify."
                + reviewer_awareness
            )
        elif role_value == "risk_analyst":
            return (
                "read the issue/task description carefully. Research the affected "
                "areas of the codebase for potential risks — security, "
                "performance, backwards compatibility, and third-party "
                "dependencies." + reviewer_awareness
            )
    elif phase == "refine":
        if role_value == "refiner":
            return (
                "read the prior review feedback carefully. Understand exactly "
                "what concerns were raised and what changes are expected. Check "
                "the current state of the code before making modifications." + reviewer_awareness
            )

    # Generic fallback
    return (
        "read the contract (`egg-contract show`) and explore the codebase "
        "to understand context, patterns, and conventions before starting." + reviewer_awareness
    )


def _build_file_boundary_section(role_value: str) -> str:
    """Build a file boundary section for an agent prompt.

    Reads the role's ``FileAccessPattern`` from ``egg_contracts.agent_roles``
    and formats it as a prompt section so the agent knows which files it can
    and cannot push *before* it starts writing files (#1431).

    Returns an empty string when no patterns are defined for the role.
    """
    try:
        from egg_contracts.agent_roles import get_role_definition

        role_def = get_role_definition(role_value)
    except ValueError, KeyError, ImportError:
        return ""

    if not role_def or not role_def.file_access:
        return ""

    fa = role_def.file_access
    if not fa.allowed_write and not fa.blocked_write:
        return ""

    lines = [
        "## File Boundaries (Gateway-Enforced)\n",
        f"Your role ({role_value.upper()}) can only push changes to files "
        "matching these patterns. The gateway will **reject your push** if it "
        "includes files outside your boundaries. Only create and modify files "
        "you are allowed to push.\n",
    ]
    if fa.allowed_write:
        lines.append("**Allowed:** " + ", ".join(f"`{p}`" for p in fa.allowed_write))
    if fa.blocked_write:
        lines.append("**Blocked:** " + ", ".join(f"`{p}`" for p in fa.blocked_write))

    # `.github/` staging-dir convention (issue #2508). Surfaced for the
    # coder role specifically because it's the producer that's expected
    # to initiate `.github/` work. The role-pattern check
    # (``startswith(".github/")``) doesn't match `.github-staging/`, so
    # autofixer / conflict_resolver allowlists technically reach the
    # staging path too — but those roles are reactive and aren't asked
    # to plan new `.github/` changes, so the convention's planning-time
    # guidance only needs to land for coder.
    if role_value == "coder":
        lines.append("")
        lines.append(
            "**`.github/` changes**: `.github/` is blocked above. If your "
            "task requires modifying CI workflows, CODEOWNERS, dependabot "
            "config, or anything else under `.github/`, write the proposed "
            "end-state to top-level `.github-staging/` instead, mirroring "
            "the `.github/` structure (e.g. stage "
            "`.github/workflows/test-e2e.yml` as "
            "`.github-staging/workflows/test-e2e.yml`). The PR builder "
            "auto-emits a manual step asking the human reviewer to move "
            "the staged files into place before merge — see issue #2508."
        )
    lines.append("")
    return "\n".join(lines)


def _build_agent_prompt(
    role_value: str,
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    prompt: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    review_feedback: str | None = None,
    review_cycle: int = 0,
    repo_path: str | None = None,
    phase_obj=None,
    all_phases=None,
    concurrent: bool = False,
    network_mode: str | None = None,
    *,
    mode: PipelineMode | None = None,
    pr_number: int | None = None,
) -> str:
    """Build a role-specific prompt for multi-agent execution.

    For the CODER role, delegates to the existing _build_phase_prompt().
    Other roles (TESTER, DOCUMENTER, ARCHITECT, etc.) get
    role-specific instructions.

    Execution roles (tester, documenter) receive a summarized
    background with structured task information instead of the full issue
    body. Analysis roles (architect, task_planner, risk_analyst) receive
    the full issue body.

    Note: Handoff data is passed via the EGG_HANDOFF_DATA environment
    variable, not via the prompt — prompts are built once before
    execution starts.

    Args:
        role_value: Agent role string (e.g. "coder", "tester")
        phase: Pipeline phase name
        pipeline_id: Pipeline ID
        pipeline_mode: "issue" or "local"
        prompt: Original task prompt
        issue_number: GitHub issue number
        repo: Repository name
        branch: Branch name
        review_feedback: Feedback from prior review cycle
        review_cycle: Current review cycle number
        repo_path: Filesystem path to repository (for user override lookup)
        phase_obj: Current plan phase object (optional)
        all_phases: All contract phases (optional)
        concurrent: Whether agent runs in concurrent multi-agent mode.
            When True, adds consensus lifecycle preamble instructing the
            agent to stay alive, poll messages, and participate in consensus.
        network_mode: Pipeline network mode ("public", "private", or None).
            When "private", injects warnings about blocked package downloads.

    Returns:
        Complete prompt string for the agent
    """
    # CODER and REFINER use the existing phase prompt (phase-specific
    # instructions are already tailored for refine vs implement etc.)
    if role_value in ("coder", "refiner"):
        base_prompt = _build_phase_prompt(
            phase=phase,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            prompt=prompt,
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            review_feedback=review_feedback,
            review_cycle=review_cycle,
            repo_path=repo_path,
        )
        # Surface file boundaries so agent knows what it can push (#1431).
        boundary_section = _build_file_boundary_section(role_value)
        if boundary_section:
            base_prompt += "\n" + boundary_section
        # In concurrent mode, inject BRC consensus preamble so the coder/refiner
        # knows to propose, respond to reviews, confirm, and stay alive.
        if concurrent:
            base_prompt += _build_brc_preamble(
                role_value,
                phase,
                repo=repo,
                branch=branch,
                base_branch=base_branch,
                mode=mode,
                pr_number=pr_number,
            )
        return base_prompt

    # Build context header (shared across all roles)
    lines = [f"You are the **{role_value.upper()}** agent in the **{phase}** phase.\n"]
    lines.append("## Context\n")
    lines.append(f"Pipeline ID: {pipeline_id}")
    lines.append(f"Phase: {phase}")
    lines.append(f"Mode: {pipeline_mode}")
    lines.append(f"Agent Role: {role_value}")
    if repo:
        lines.append(f"Repository: {repo}")
    if branch:
        lines.append(f"Branch: {branch}")
    if issue_number is not None:
        lines.append(f"Issue: #{issue_number}")
    lines.append("")

    # Concurrent mode: add BRC consensus lifecycle preamble so agents understand
    # they must stay alive and participate in Broadcast-Review-Converge consensus.
    if concurrent:
        lines.append(
            _build_brc_preamble(
                role_value,
                phase,
                repo=repo,
                branch=branch,
                base_branch=base_branch,
                mode=mode,
                pr_number=pr_number,
            )
        )

    # Include role-appropriate context instead of the raw issue body.
    # Analysis roles (architect, task_planner, risk_analyst) receive the full
    # issue body. Execution roles (tester, documenter) receive a
    # brief summary with structured task information and context pointers.
    role_context = _build_role_context(
        role_value=role_value,
        prompt=prompt,
        issue_number=issue_number,
        phase_obj=phase_obj,
        all_phases=all_phases,
        base_branch=base_branch,
    )
    if role_context:
        lines.append(role_context)

    # Review feedback from prior cycles
    if review_feedback:
        lines.append("## Review Feedback\n")
        lines.append(review_feedback)
        lines.append("")

    # Derive the pipeline identifier for namespaced output filenames.
    _identifier = _pipeline_identifier(issue_number, pipeline_id)

    # Role-specific instructions
    lines.append("## Your Task\n")

    if role_value == "tester":
        # Look up per-repo check commands from repositories.yaml
        repo_checks: list[dict[str, str]] = []
        if repo:
            try:
                repo_checks = get_repo_checks(repo)
            except FileNotFoundError:
                repo_checks = []

        lines.extend(
            [
                "**ROLE BOUNDARY: You are the TESTER, not the CODER.** "
                "Do NOT implement application logic, create source files, write configuration, "
                "or set up project infrastructure. Your job is to write tests for the CODER's "
                "implementation, run checks, and report gaps. If the coder hasn't committed yet, "
                "wait — do not implement the solution yourself.",
                "",
                "**Your mandate is two-fold**:",
                "",
                "1. **Comprehensive coverage** — write tests that prevent "
                "regressions, covering the happy path and realistic alternative "
                "paths through every changed area. New behavior gets new tests; "
                "modified behavior gets updated tests; nothing the coder changed "
                "should silently lose coverage.",
                "2. **Adversarial probing** — actively probe the coder's "
                "implementation for bugs and edge cases they missed. Treat the "
                "implementation as suspect until you have tried to break it. "
                "Write tests that target suspected weaknesses. When a test "
                "fails because of a coder-side bug, **the committed failing "
                "test is evidence — the NACK is the bug report**. Pair every "
                "failing test with an explicit NACK on the coder's proposal "
                "that names the failing test in its rationale; otherwise the "
                "bug is easy for the coder to miss. Also list the bug in "
                "`gaps_found` and HANDOFF to coder with the failure output. "
                "The coder owns the fix; you own surfacing the bug.",
                "",
                "You are also responsible for **lint/type-check validation**.",
                "",
                "### When the slice warrants no new tests (#2431)",
                "",
                "Pure refactors (symbol moves, decompositions with no behavior "
                "change), doc-only slices, and other no-test-work slices still "
                "require you to **propose** — BRC consensus blocks until every "
                "producer has proposed at least once. **Don't just heartbeat "
                "and wait for work that isn't coming.** Instead:",
                "",
                "1. Run **all** configured checks against the coder's diff "
                "(`make lint`, `make test`, etc.) and confirm they pass.",
                "2. Propose with the no-op attestation:",
                "   - `attestation.no_test_changes_needed: true`",
                "   - `attestation.no_test_changes_reason`: a concrete sentence "
                'explaining why no new tests are warranted (e.g. "slice-3 is '
                "a pure decomposition: symbol moves between submodules, no "
                "behavior change; the existing test suite covers the "
                're-exported barrel").',
                "   - `attestation.checks_passed`: the configured checks that "
                "actually ran and passed (`['lint', 'test']` etc.) — still "
                "required.",
                "   - `attestation.tests_run`: 0 is acceptable here; if you "
                "did run the existing suite, report the count.",
                '3. Make sure your propose `summary` says "no new tests '
                'warranted: <reason>" so reviewers can verify the diff '
                "really is behavior-preserving.",
                "",
                "If the slice **does** have new test work (real behavior "
                "changes, new edge cases, modified contracts), do NOT use the "
                "no-op path — author tests as usual.",
                "",
                "### Testing",
                "",
                "1. Review the changed files (available in handoff data or via git diff)",
                "2. Build coverage tests for the happy path and realistic "
                "alternative paths in every changed area",
                "3. **Adversarially probe** the implementation: identify "
                "suspected bugs and untested edge cases, then write tests that "
                "target them",
                "4. Run all tests. Tests that pass demonstrate coverage; "
                "**tests that fail demonstrate bugs you have found** — keep them",
                "5. For every failing test caused by a coder-side bug: "
                "commit the failing test AND **NACK the coder's proposal, "
                "explicitly naming the failing test in the NACK rationale**. "
                "The committed test alone is not sufficient — the NACK is "
                "what surfaces the bug to the coder. Also list the bug in "
                "`gaps_found` and HANDOFF to the coder with the failure "
                "output. Your `test` configured check will fail until the "
                "coder pushes a fix — that is expected; do NOT propose "
                "consensus until every configured check passes per the "
                "*Configured Checks* section below",
                "6. Commit all test files with descriptive messages",
                "",
                "Adversarial probing — actively try to break the implementation:",
                "- Missing error handling and input validation",
                "- Boundary conditions, off-by-one, empty/null/oversized inputs",
                "- Uncovered code paths and branches (especially error paths)",
                "- Concurrency: races, partial failures, retry behavior, ordering assumptions",
                "- Contract violations: does the code actually match the "
                "acceptance criteria, or just the happy path of them?",
                "- Integration gaps between components and unstated interface assumptions",
                "",
                "Gap-finding focus (still report these in `gaps_found` even "
                "when you cannot write a test for them):",
                "- Logic errors that would require design changes to fix",
                "- Inconsistencies between the implementation and the plan/contract",
                "- Missing test infrastructure that prevents adequate coverage",
                "",
                "### Configured Checks (MANDATORY)",
                "",
                "You MUST run **ALL** configured checks below and fix any failures "
                "before proposing consensus. Skipping checks (e.g., running tests but "
                "not lint) is a common failure mode — do not skip any.",
                "",
            ]
        )

        if repo_checks:
            # Inject explicit check commands from repositories.yaml
            lines.extend(
                [
                    "The following check commands are configured for this repository. "
                    "Run **every one** of them **in order**:",
                    "",
                ]
            )
            for i, check in enumerate(repo_checks, 1):
                name = check["name"].replace("\n", " ").strip()
                cmd = check["command"].replace("\n", " ").strip()
                lines.append(f"{i}. **{name}**: `{cmd}`")
            lines.extend(
                [
                    "",
                    "If ANY check fails in test files you wrote, fix the issue and re-run. "
                    "If failures are in source code, do NOT fix them — report them to the coder.",
                    "",
                    "After running all checks:",
                ]
            )
        else:
            # Fall back to auto-discovery
            lines.extend(
                [
                    "1. **Discover commands**: Look for Makefile, pyproject.toml, package.json, "
                    "setup.cfg, tox.ini, or similar build/test configuration files",
                    "2. **Run linters**: Execute linters (ruff, eslint, golangci-lint, etc.)",
                    "3. **Run type checkers**: Execute type checkers (mypy, pyright, tsc, etc.)",
                    "",
                    "After running all checks:",
                ]
            )

        lines.extend(
            [
                "- **Auto-fix test files only**: Fix auto-fixable issues in test files you wrote "
                "(formatting, import order, simple type errors)",
                "- **Repeat**: Re-run checks to verify fixes. Repeat up to 3 times.",
                "- **Commit test fixes**: Commit all test-file fixes together with a descriptive message",
                "",
                "Auto-fixable (in test files only — commit fixes directly):",
                "- Lint errors in test files (formatting, import order, code style)",
                "- Type errors in test files with clear fixes",
                "",
                "Report only (do NOT modify source code — NACK the coder and explain what's needed):",
                "- Lint or type errors in source code — tell the coder to fix these",
                "- Test failures caused by bugs in the coder's implementation — tell the coder to fix",
                "- Complex logic errors requiring design decisions",
                "- Security issues requiring architectural changes",
                "",
                "When testing third-party library integrations or unfamiliar frameworks, "
                "use WebSearch and WebFetch (when available) to look up testing patterns, "
                "known edge cases, and recommended test approaches for those libraries.",
                "",
                "Before writing tests, review the coder's session for context on what was changed and why:",
                "`egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement`",
                "",
                "## Parallel Execution with Subagents\n",
                "If the changes span multiple independent components or modules, you can use "
                "Claude Code's **Agent tool** to parallelize test writing. Launch one subagent "
                "per component to write and run tests concurrently. Each subagent should work "
                "on non-overlapping test files. Subagents should only write files — do NOT "
                "stage or commit from subagents. After all subagents complete, run the full "
                "test suite to verify everything passes together, then stage and commit yourself.",
                "",
            ]
        )

        # Test execution verification — prevents proposing consensus with
        # unverified tests (issue #1359).
        test_verify_lines = [
            "### Test Execution Verification (CRITICAL)\n",
            "You MUST actually execute the test suite (`go test`, `pytest`, `jest`, etc.). "
            "Passing gofmt, syntax checks, or linting alone does NOT count as tests run.\n",
            "If tests cannot run (e.g., dependency downloads blocked in private network mode, "
            "missing build tools), you MUST:",
            "1. Set `tests_execution_blocked: true` and provide `tests_execution_blocked_reason` "
            "in your attestation when proposing consensus",
            '2. Include an explicit **"TESTS UNVERIFIED"** warning in your proposal summary',
            '3. Do NOT claim your work is "complete" — state that tests are written but unverified',
            "",
            "**Picking between `tests_execution_blocked` and `no_test_changes_needed`** "
            "(see the no-op section above): if the slice warrants no new tests *and* "
            "the configured checks could not run, prefer the blocked path — "
            "`tests_execution_blocked` reports lower confidence than "
            "`no_test_changes_needed` and is the more conservative claim. The two "
            "flags are mutually exclusive; the orchestrator and pre-flight both "
            "reject a proposal that asserts both.",
            "",
        ]
        if network_mode == "private":
            test_verify_lines.extend(
                [
                    "**WARNING: Private network mode is active** — external package downloads "
                    "(go mod download, npm install, pip install, etc.) may be blocked. "
                    "If dependency installation fails, you cannot verify tests. "
                    "Follow the instructions above to flag tests as unverified.",
                    "",
                ]
            )
        lines.extend(test_verify_lines)

        # Check execution verification — prevents proposing consensus without
        # running all configured checks (issue #1414).
        check_verify_lines = [
            "### Check Execution Verification (CRITICAL)\n",
            "You MUST run **every** configured check command and ensure they **pass** "
            "before proposing consensus. Running tests alone is NOT sufficient — "
            "lint, type-check, and security checks must also pass. If you skip a "
            "check or propose with a failing check, the server will reject your "
            "proposal.\n",
            "Before proposing, verify:",
            "- [ ] All configured check commands have been executed",
            "- [ ] All checks pass (or failures have been auto-fixed and re-verified)",
            "- [ ] Any auto-fix commits have been pushed",
            "",
            # Source-failure handling — without this, agents have rationalised
            # inventing ad-hoc check names so their attestation passes, masking
            # red CI on the initial push (issue #1966).
            "### When Source-Code Checks Fail (CRITICAL)\n",
            "If a configured check fails because of the **coder's source code** "
            "(not test files you wrote), you have a binding choice: "
            "**do NOT propose consensus**. The role boundary above forbids you "
            "from fixing source code, and the rules below forbid you from "
            "papering over the failure. Instead:\n",
            "1. **Do NOT fix it yourself** — that crosses the tester role boundary.",
            "2. **Do NOT invent a narrower or renamed check** "
            "(e.g. `pytest-<your-suite>`, `ruff-check-tester-files`) and attest to "
            "*that* in `checks_passed`. Only the literal names from "
            "`repositories.yaml` (`lint`, `test`, `security`, etc.) are valid; "
            "the server will reject anything else, and substituting narrower names "
            "hides real CI failures from reviewers.",
            "3. **Send a HANDOFF message to the coder** describing the failing "
            "check, the command, and the diagnostic output, e.g.:",
            "   ```",
            "   egg-orch message send --to coder --type HANDOFF \\",
            '     --subject "lint failing on src/foo.py" \\',
            '     --body "make lint exits 1: mypy errors in src/foo.py:42 '
            '(incompatible types). Please fix and push; I will re-run lint."',
            "   ```",
            "   If you are also reviewing the coder's own consensus proposal, "
            "NACK it for the same reason — the two channels reinforce each other.",
            "4. **Wait** for the coder to push a fix, then **re-run every "
            "configured check** from scratch. Use `egg-orch message wait-loop` "
            "(see Producer Lifecycle) — do not spin in a shell `for` loop or "
            "prefix with `sleep`.",
            "5. **Only propose consensus once every configured check passes "
            "literally**, with the configured names in `checks_passed`.",
            "",
            "If the coder is unresponsive or the failure genuinely cannot be "
            "fixed within this phase, document it in `gaps_found` and let the "
            "orchestrator escalate via `OVERSEER_ALERT`. Do NOT work around the "
            "block by proposing with a partial or renamed `checks_passed` list.",
            "",
            "### Attestation: `checks_passed` (REQUIRED)\n",
            "When proposing consensus, your attestation MUST include a `checks_passed` "
            "list containing the **name** of every configured check that **passed**. "
            "Do NOT include checks that failed, and do NOT invent ad-hoc names "
            "(e.g. `pytest-<scope>`, `ruff-check-tester-files`) — only the literal "
            "names from `repositories.yaml`. "
            "For example, if the repo has `lint` and `test` checks and both pass, "
            'your attestation must include `"checks_passed": ["lint", "test"]`. '
            "The server will reject your proposal if any configured check is missing "
            "from this list (i.e. did not pass).",
            "",
        ]
        lines.extend(check_verify_lines)

    elif role_value == "documenter":
        lines.extend(
            [
                "Update documentation for the changes made by the CODER agent:",
                "",
                "1. Review the changed files (available in handoff data or via git diff)",
                "2. Update relevant documentation (READMEs, docstrings, API docs)",
                "3. Add or update inline code comments where helpful",
                "4. Commit documentation changes with descriptive messages",
                "",
                "Focus on:",
                "- Accurate descriptions of new features or changes",
                "- Updated usage examples if APIs changed",
                "- Clear explanation of any breaking changes",
                "",
                "When documenting third-party integrations or external APIs, use WebSearch "
                "and WebFetch (when available) to verify current API signatures, link to "
                "official documentation, and confirm usage examples are up to date.",
                "",
                "Find all changed files across agents:",
                "`egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files`",
                "",
                "### When the slice warrants no doc updates (#2444)",
                "",
                "Pure refactors (symbol moves, decompositions with no "
                "surfaced API change), test-only slices, and internal-only "
                "slices that don't touch any documented surface still "
                "require you to **propose** — BRC consensus blocks until "
                "every producer has proposed at least once. **Don't just "
                "heartbeat and wait for work that isn't coming.** Instead:",
                "",
                "1. Walk the coder's diff and confirm there is no "
                "documented-surface impact: no public API signature "
                "changes, no behavior changes a user-facing doc describes, "
                "no new feature or flag mentioned in README / docs/, no "
                "docstring contracts that drift.",
                "2. Propose with the no-op attestation:",
                "   - `attestation.no_doc_changes_needed: true`",
                "   - `attestation.no_doc_changes_reason`: a concrete "
                "sentence explaining why no doc updates are warranted "
                '(e.g. "slice-3 is a pure decomposition: symbol moves '
                "between submodules, no surfaced API change; no README / "
                'docs/ / docstring surface impacted").',
                '3. Make sure your propose `summary` says "no doc '
                'updates warranted: <reason>" so reviewers can verify '
                "the diff really has no doc impact.",
                "",
                "If the slice **does** have doc impact (any of the bullets "
                "above), do NOT use the no-op path — author doc changes as "
                "usual.",
                "",
            ]
        )
    elif role_value == "architect":
        lines.extend(
            [
                "Analyze the task and produce an architecture analysis:",
                "",
                "1. Understand the problem or feature request from the issue",
                "2. Research the current codebase to understand existing patterns",
                "3. Research externally when the task involves third-party libraries, APIs, "
                "or frameworks — use WebSearch and WebFetch (when available) to verify "
                "assumptions, check current documentation, review architectural patterns, "
                "and look up current best practices. Skip external research for purely "
                "internal changes.",
                "4. Identify key files, constraints, and dependencies",
                "5. Consider multiple implementation approaches",
                "6. Recommend an approach with justification and document technical decisions",
                "",
                f"Write your analysis to `.egg-state/agent-outputs/{_identifier}-architect-output.json`.",
                "",
                "### File Restrictions",
                "",
                f"You MUST only write to `.egg-state/agent-outputs/{_identifier}-architect-output.json`.",
                "Do NOT create or modify any other files. Specifically:",
                "- Do NOT modify analysis drafts (`.egg-state/drafts/*-analysis.md`) — "
                "these are finalized in the refine phase and are read-only",
                "- Do NOT create or modify contracts (`.egg-state/contracts/`)",
                "- Do NOT create or modify reviews (`.egg-state/reviews/`)",
                "- Do NOT create or modify plan drafts (`.egg-state/drafts/*-plan.md`)",
                "",
            ]
        )
    elif role_value == "task_planner":
        draft_path = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
        lines.extend(
            [
                "Decompose the architecture analysis into a single-PR implementation plan.",
                "",
                "**CRITICAL CONSTRAINT — One Issue = One Workflow = One PR.**",
                "All tasks belong to a single pull request. Use phases and commits to",
                "organise the work within that PR — do NOT propose multiple PRs.",
                "",
                "Steps:",
                "1. Review the architecture analysis from the ARCHITECT agent",
                "2. Break down the work into phases with discrete, actionable tasks",
                "3. Define clear acceptance criteria for each task",
                "4. Define dependency ordering between tasks",
                "5. Identify the test strategy — what automated tests cover the changes, "
                "and what manual verification is needed",
                "6. Identify any manual pre-merge or post-merge steps "
                "(migrations, config changes, deployments)",
                "",
                "## Output Format",
                "",
                "Write a markdown plan document with a **yaml-tasks** structured",
                "appendix at the end. The prose section should explain the approach;",
                "the appendix is machine-parsed for contract population.",
                "",
                *_PR_DESCRIPTION_GUIDANCE,
                "",
                "End your document with a fenced YAML block like this:",
                "",
                "````",
                "```yaml",
                "# yaml-tasks",
                "pr:",
                '  title: "Short imperative summary (≤70 chars)"',
                "  description: |",
                *_PR_DESCRIPTION_YAML_EXAMPLE,
                "  test_plan: |",
                "    - Automated: describe which tests cover the changes",
                "    - Manual: specific steps a reviewer should take to verify",
                "  manual_steps: |",
                "    Pre-merge: any required steps before merging",
                "    Post-merge: any required steps after merging",
                "phases:",
                "  - id: 1",
                "    name: |-",
                "      Phase Name",
                "    goal: |-",
                "      What this phase achieves",
                "    tasks:",
                "      - id: TASK-1-1",
                "        description: |-",
                "          What to do — safe to include `code: type` snippets,",
                "          URLs, and other punctuation inside a block scalar.",
                "        acceptance: |-",
                "          How to verify it is done",
                "        role: coder  # optional: coder (default), tester, or documenter",
                "        files:",
                "          - path/to/file.py",
                "```",
                "````",
                "",
                *_YAML_TASKS_SAFETY_GUIDANCE,
                "",
                "Do NOT use a `pr_plan` key or propose multiple PRs.",
                "",
                "The `test_plan` field is **required** — describe both automated test "
                "coverage and any manual verification steps. The `manual_steps` field "
                "should list any pre-merge or post-merge actions required by the reviewer "
                "or deployer; use an empty string if none.",
                "",
                # ----------------------------------------------------
                # #2137 — slice-DAG planner guidance
                # ----------------------------------------------------
                "## Slice-DAG guidance (#2137)",
                "",
                "The implement-phase pipeline now ships each plan **slice** "
                "(formerly **phase**) as its own stacked PR. The plan you "
                "emit drives that DAG; the planner rules below are mandatory.",
                "",
                "**Yaml key swap**: prefer the canonical ``slices:`` key in "
                "your ``# yaml-tasks`` block (the parser also accepts "
                "``phases:`` for backward compatibility with already-shipped "
                "planner prompts). New plans should use ``slices:``.",
                "",
                "**Slice-sizing guidance (soft, advisory only)**: target "
                "≤1,000 LOC per slice where possible. Slices estimated above "
                "1,000 LOC will be flagged as advisory by the plan reviewer "
                "but are NOT rejected. There is no hard size ceiling — the "
                "refiner/operator can override sizing concerns at any point. "
                "The plan reviewer never NACKs on size.",
                "",
                "**Forest constraint (HARD)**: every slice must have at most "
                "ONE DAG parent — the implement-phase pipeline ships every "
                "slice as a stacked PR with exactly one base branch. "
                "Multi-parent slices break the stacking invariant and are "
                "rejected at plan ingestion.",
                "",
                "**Auto-serialization rule for would-be multi-parent slices**: "
                "when you identify a slice that would naturally have >1 "
                "parents, you MUST serialise the upstream slices into a "
                "linear chain and record your chosen ordering on the "
                "downstream slice's ``serialized_chain_order`` field. The "
                "list names the upstream slice IDs in their chosen "
                "serialization order.",
                "",
                "Worked example: if ``slice-3`` would naturally have "
                "parents ``[slice-1, slice-2]``, instead emit:",
                "",
                "```yaml",
                "  - id: 1",
                "    name: |-",
                "      Foundations",
                "    # ... (root)",
                "  - id: 2",
                "    name: |-",
                "      Middle",
                "    dependencies:",
                "      - slice-1",
                "  - id: 3",
                "    name: |-",
                "      Downstream",
                "    dependencies:",
                "      - slice-2  # serialised — slice-2 is the only DAG parent",
                "    serialized_chain_order:",
                "      - slice-1",
                "      - slice-2  # records that you deliberately picked",
                "                 # slice-1 → slice-2 → slice-3",
                "```",
                "",
                "Your judgement is the source of truth. The fallback "
                "heuristic when you have no preference is: cluster "
                "would-be parents by ``files_affected`` Jaccard overlap "
                "(>0.3), then order by descending downstream fan-out.",
                "",
                f"Write your plan to `{draft_path}`.",
                "",
            ]
        )
        # Append role file restriction info so the planner assigns tasks correctly
        lines.append(_build_role_restrictions_section())
    elif role_value == "risk_analyst":
        lines.extend(
            [
                "Assess technical risks for the proposed implementation:",
                "",
                "1. Review the architecture analysis from the ARCHITECT agent",
                "2. Identify technical risks (security, performance, compatibility)",
                "3. Research externally when the change involves third-party dependencies — "
                "use WebSearch and WebFetch (when available) to check for known "
                "vulnerabilities, deprecation notices, and compatibility issues. "
                "Skip external research for purely internal changes.",
                "4. Assess impact and likelihood of each risk",
                "5. Propose mitigation strategies and rollback plans",
                "6. Flag areas that need human review",
                "",
                f"Write your risk assessment to `.egg-state/agent-outputs/{_identifier}-risk_analyst-output.json`.",
                "",
            ]
        )
    elif role_value.startswith("reviewer_"):
        # Delegate to the detailed review prompt with criteria and verdict format
        reviewer_type = role_value.replace("reviewer_", "", 1).replace("_", "-")
        review_prompt = _build_review_prompt(
            phase=phase,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            reviewer_type=reviewer_type,
            issue_number=issue_number,
            review_cycle=review_cycle + 1,
            prior_feedback=review_feedback,
            repo_path=repo_path,
            base_branch=base_branch,
            concurrent=concurrent,
        )
        if concurrent:
            review_prompt += "\n" + _build_brc_preamble(
                role_value,
                phase,
                repo=repo,
                branch=branch,
                base_branch=base_branch,
                mode=mode,
                pr_number=pr_number,
            )
        return review_prompt
    else:
        lines.extend(
            [
                f"Execute your role as {role_value} for this phase.",
                "",
            ]
        )

    # Phase restrictions
    _recovery_base_ref = _resolve_origin_ref(base_branch)
    lines.append("## Phase Restrictions\n")
    if phase == "implement":
        lines.extend(
            [
                "- You CAN push code changes to git (git push)",
                "- You CAN link commits to tasks (egg-contract add-commit)",
                "- You CANNOT push .egg-state/ files (except checkpoints)",
                "- You CANNOT create PRs (the pipeline manages the PR)",
                "",
                "### Push Recovery",
                "",
                "If your push is rejected due to restricted files on the branch, "
                f"create a clean branch from {_recovery_base_ref} and cherry-pick "
                "only your code commits:",
                "```",
                f"git checkout -b egg/<new-branch> {_recovery_base_ref}",
                "git cherry-pick <your-commit-hash>",
                "git push origin egg/<new-branch>",
                "```",
                "Do NOT retry the same push — fix the branch first.",
                "After pushing to the new branch, use `egg-contract add-commit` to "
                "link your commits so the pipeline can track them on the new branch.",
                "",
            ]
        )
    elif phase in ("refine", "plan"):
        lines.extend(
            [
                "- You CAN write to `.egg-state/drafts/` and `.egg-state/agent-outputs/`",
                "- You CAN push these state files to git (git push)",
                "- You CAN create HITL decisions (egg-contract add-decision)",
                "- You CAN create feedback requests (egg-contract add-feedback)",
                "- You CANNOT modify production code (src/, lib/, gateway/, sandbox/, "
                "action/, docs/, tests/, test/)",
                "- You CANNOT modify contracts (.egg-state/contracts/) or CI config (.github/)",
                "- You CANNOT create PRs (gh pr create)",
                "",
                "### Push Recovery",
                "",
                "If your push is rejected due to restricted files on the branch, "
                f"create a clean branch from {_recovery_base_ref} and cherry-pick "
                "only your state file commits:",
                "```",
                f"git checkout -b egg/<new-branch> {_recovery_base_ref}",
                "git cherry-pick <your-commit-hash>",
                "git push origin egg/<new-branch>",
                "```",
                "Do NOT retry the same push — fix the branch first.",
                "After pushing to the new branch, use `egg-contract add-commit` to "
                "link your commits so the pipeline can track them on the new branch.",
                "",
            ]
        )

    # File boundaries (#1431) — surface allowed/blocked patterns so
    # the agent avoids creating files the gateway will reject on push.
    boundary_section = _build_file_boundary_section(role_value)
    if boundary_section:
        lines.append(boundary_section)

    lines.append("## Phase Completion\n")
    if concurrent:
        lines.extend(
            [
                "When you have completed your primary work:\n",
                "1. Commit all changes",
                '2. Run: `egg-orch signal readiness --state READY --reason "Work complete"`',
                "3. Enter an **event-driven** stay-alive wait (issue #1897). "
                "Do NOT wrap `egg-orch` in a shell `for i in 1..N` loop, "
                "and do NOT `sleep N` — use the server-side blocking primitive:",
                "```bash",
                "egg-orch message wait-loop \\",
                "  --for CONSENSUS_CONFIRMED \\",
                "  --for CONSENSUS_RE_REVIEW \\",
                "  --for OVERSEER_ALERT \\",
                "  --timeout 60",
                "```",
                "`wait-loop` blocks server-side and loops forever until a "
                "NEW matching BRC event arrives (exit 0) or a permanent error "
                "occurs (exit 1).  There is no outer timeout — the wrapper "
                "owns the 0/1 contract.  Events that predate the call "
                "(including your own just-sent CONSENSUS_CONFIRMED) are "
                "skipped (issue #1925); if you need zero-drop semantics "
                "across a send→wait boundary, capture the ID of your "
                "send and pass `--since <id>`.  See "
                "`docs/reference/agent-wait-patterns.md` for the full "
                "exit-code contract and the five anti-patterns to avoid.",
                "4. If `wait-loop` returns with a message that affects your work, "
                "transition back to WORKING, address it, then signal READY again. "
                "**In particular, if you receive a `CONSENSUS_RE_REVIEW` message, "
                "you MUST re-confirm via `egg-orch consensus confirmed` (or "
                "re-review and ACK/NACK if you are a reviewer of the re-proposing "
                "producer). Ignoring this message will stall the pipeline.**",
                "5. **Do NOT exit.** The orchestrator will stop your container when consensus "
                "is reached.",
            ]
        )
    else:
        lines.append(
            "When you have completed your work, ensure everything is committed and exit successfully."
        )

    return "\n".join(lines)


def _format_nack_summary(nack_details: list[dict]) -> str:
    """Format unresolved NACK details into a human-readable summary string."""
    return "; ".join(
        f"{n['reviewer']} NACKed {n['producer']}: {n.get('reason') or 'no reason given'}"
        for n in nack_details
    )


def _incomplete_consensus_decision_text(
    final_consensus: dict,
    container_failure_count: int,
) -> tuple[str, str]:
    """Build (question, log_suffix) for incomplete-consensus HITL escalation.

    Distinguishes the two failure modes — unresolved NACKs vs. agents that
    never confirmed — so the operator sees actionable detail in `/sdlc`.
    """
    nacks = final_consensus.get("unresolved_nacks", []) or []
    blocking = final_consensus.get("blocking_agents", []) or []
    if container_failure_count:
        prefix = f"{container_failure_count} container(s) exited with non-zero code; "
    else:
        prefix = "All containers exited; "
    if nacks:
        summary = _format_nack_summary(nacks)
        question = (
            f"{prefix}consensus incomplete with {len(nacks)} unresolved NACK(s): "
            f"{summary}. Committed work is preserved on the per-role branch — "
            f"'Retry phase' restarts with artifacts intact. How to proceed?"
        )
        log_suffix = f"\n--- INCOMPLETE CONSENSUS / UNRESOLVED NACKs ({len(nacks)}) ---\n{summary}"
    else:
        agent_list = ", ".join(blocking) if blocking else "unknown"
        question = (
            f"{prefix}consensus incomplete; agents never confirmed: {agent_list}. "
            f"Committed work is preserved on the per-role branch — "
            f"'Retry phase' restarts with artifacts intact. How to proceed?"
        )
        log_suffix = (
            f"\n--- INCOMPLETE CONSENSUS / NO CONFIRMATION ---\nblocking_agents={agent_list}"
        )
    return question, log_suffix


def _persist_hitl_decision(
    pipeline_id: str,
    pipeline: Pipeline,
    store: StateStore,
    *,
    question: str,
    options: list[str],
    phase: PipelinePhase | None = None,
):
    """Create and persist an HITL decision under the pipeline state lock.

    `pipeline.add_decision()` only mutates an in-memory object.  The caller
    of `_run_concurrent_phase` reloads the pipeline fresh from disk before
    writing FAILED, so any in-memory decision is silently dropped — the
    on-disk state (which `/sdlc` reads via `pipeline.get_pending_decisions()`)
    never sees it.  This helper mirrors the *persistence half* of
    `DecisionQueue.queue_decision()` and the HITL-gate write at
    pipelines.py:13080-13089: load → mutate → save under the reentrant
    pipeline state lock.  Note: it intentionally does **not** invoke
    `_notify_handlers` — no production code currently registers a
    `DecisionHandler` and `/sdlc` reads from disk on each request, so
    notifications are not needed for the issue-2203 path.  The in-memory
    `pipeline` argument is also synced so callers observe consistent state.

    Returns the created decision, or None if persistence failed (logged;
    callers should not raise — losing an HITL decision is bad but losing
    the rest of the cleanup path is worse).
    """
    try:
        with get_pipeline_state_lock(pipeline_id):
            disk_pipeline = store.load_pipeline(pipeline_id)
            decision = disk_pipeline.add_decision(
                question=question,
                options=options,
                phase=phase or disk_pipeline.current_phase,
            )
            store.save_pipeline(disk_pipeline)
        # Defensive copy: avoid sharing the list reference with the
        # disk-loaded copy, which is local and goes out of scope.
        pipeline.decisions = list(disk_pipeline.decisions)
        return decision
    except Exception:
        logger.warning(
            "Failed to persist HITL decision",
            pipeline_id=pipeline_id,
            question=question[:100],
            exc_info=True,
        )
        return None


def _check_brc_progress_gate(
    pipeline_id: str,
    slice_id: str | None,
    active_role_names: list[str],
    gate_seconds: float,
) -> tuple[bool, str | None]:
    """Return (defer, reason) for the BRC consensus-timeout progress gate (#2243).

    Defers the consensus-timeout ``OVERSEER_ALERT`` (#2264; previously
    an auto-``choice`` HITL decision) when *any* of the following has
    fired within ``gate_seconds``:

    * The BRC tracker's most recent ``CONSENSUS_PROPOSE`` (producer
      proposal) timestamp.
    * The most recent ACK/NACK timestamp on the approval matrix.
    * The most recent container heartbeat for any role in
      ``active_role_names`` (filters out cross-phase pollution in the
      shared :class:`HealthMonitor` singleton).

    The gate is the operator-friendly half of the issue-2243 fix: at
    :data:`consensus_timeout_minutes` we previously opened a `choice`
    decision unconditionally, even when producers were minutes from
    their first commit. With the gate, the polling loop keeps polling
    while signals are alive; the alert is only published once the bus
    and containers have both gone quiet for ``gate_seconds``.

    ``gate_seconds <= 0`` disables the gate (returns ``(False, None)``).
    Failures in any signal source are logged at WARNING and treated as
    "no signal from that source" — never as a gate defer, since a
    crashed signal collector must not silently keep us off the alert
    surface.

    Heartbeat-cadence contract: the coder-mid-merge-conflict path
    (no ``CONSENSUS_PROPOSE`` yet, only container heartbeats — the
    original incident's ``decision-17`` flavour, pre-#2264) relies on
    container heartbeats firing at least every ``gate_seconds``. Sandbox
    heartbeats (see ``shared/egg_agent`` heartbeat scheduler and
    ``orchestrator/health_monitor.py``) cadence today is well under
    300s, but a long uninterruptible subprocess (e.g. ``git rebase``
    blocked on a merge driver) could starve them; once that happens
    the gate falls open and the pre-fix behaviour returns. Tracked as
    a follow-up under #2243.

    TODO(#2243 step 2): same-role cross-phase pollution. The role-name
    filter handles different-role ghosts (refiner heartbeat lingering
    during a coder phase) but not same-role ghosts: ``coder`` reappears
    across implement / implement-fix / fix-on-PR phases and
    ``HealthMonitor._last_heartbeat['coder']`` is only popped on
    ``clear_agent_state``. A phase boundary clear (or stamping the
    heartbeat key with the phase) would close it; per-phase timeouts
    in step 2 of the issue plan will likely subsume it.
    """
    if gate_seconds <= 0:
        return False, None

    # Two clocks, deliberately. ``now_dt`` is used for tracker
    # timestamps (datetime in UTC). ``now_wallclock`` is the float
    # epoch ``time.time()`` returns, matching the wall-clock values
    # ``HealthMonitor._last_heartbeat`` is populated with. Despite the
    # earlier name ``now_mono``, these are NOT monotonic — an NTP step
    # on the orchestrator host can make ``(now - latest_hb)`` negative
    # or skip the gate window. Acceptable today; revisit alongside the
    # per-phase-timeout follow-up.
    now_dt = datetime.now(UTC)
    now_wallclock = time.time()

    # 1. BRC bus signals (proposal + ACK/NACK timestamps).
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (
                get_peer_consensus_tracker,  # type: ignore[no-redef]
            )
        tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
        if tracker is not None:
            ts = tracker.get_latest_progress_timestamp()
            if ts is not None and (now_dt - ts).total_seconds() < gate_seconds:
                age = (now_dt - ts).total_seconds()
                return True, f"BRC bus active {age:.0f}s ago"
    except Exception as e:
        logger.warning(
            "BRC progress-gate tracker check failed",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    # 2. Container heartbeats. Filter by active roles so a stale
    # heartbeat from a prior phase in the singleton HealthMonitor
    # doesn't keep us out of the HITL surface forever. An empty
    # ``active_role_names`` means the caller has no live containers
    # to gate on, so match nothing rather than every stale heartbeat.
    if not active_role_names:
        return False, None
    try:
        from health_monitor import get_health_monitor

        hm = get_health_monitor()
        if hm is not None:
            active_set = set(active_role_names)
            latest_hb: float | None = None
            with hm._lock:  # noqa: SLF001 — read-only snapshot
                hb_snapshot = dict(hm._last_heartbeat)  # noqa: SLF001
            for agent_id, hb_time in hb_snapshot.items():
                if agent_id not in active_set:
                    continue
                if latest_hb is None or hb_time > latest_hb:
                    latest_hb = hb_time
            if latest_hb is not None and (now_wallclock - latest_hb) < gate_seconds:
                age = now_wallclock - latest_hb
                return True, f"container heartbeat {age:.0f}s ago"
    except Exception as e:
        logger.warning(
            "BRC progress-gate heartbeat check failed",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    return False, None


def _latest_active_role_heartbeat(active_role_names: list[str]) -> datetime | None:
    """Return the most recent heartbeat timestamp across ``active_role_names``.

    Mirrors the heartbeat half of :func:`_check_brc_progress_gate` so the
    consensus-timeout ``OVERSEER_ALERT`` carries a meaningful
    ``latest_heartbeat_at`` value. Filters by active role to avoid
    pollution from stale entries in the singleton ``HealthMonitor``.

    Returns ``None`` when no live heartbeat is available (no roles
    given, no health monitor, or any failure in the lookup — failures
    are logged at WARNING and treated as "no signal", consistent with
    the gate).
    """
    if not active_role_names:
        return None
    try:
        from health_monitor import get_health_monitor

        hm = get_health_monitor()
        if hm is None:
            return None
        active_set = set(active_role_names)
        latest_hb: float | None = None
        with hm._lock:  # noqa: SLF001 — read-only snapshot
            hb_snapshot = dict(hm._last_heartbeat)  # noqa: SLF001
        for agent_id, hb_time in hb_snapshot.items():
            if agent_id not in active_set:
                continue
            if latest_hb is None or hb_time > latest_hb:
                latest_hb = hb_time
        if latest_hb is None:
            return None
        return datetime.fromtimestamp(latest_hb, tz=UTC)
    except Exception as e:
        logger.warning(
            "Consensus-timeout alert heartbeat lookup failed",
            error=str(e),
            exc_info=True,
        )
        return None


def _publish_consensus_timeout_alert(
    pipeline: Pipeline,
    pipeline_id: str,
    consensus_timeout: float,
    blocking_agents: list[str],
    *,
    priority: str,
    latest_proposal_at: datetime | None,
    latest_heartbeat_at: datetime | None,
    slice_id: str | None,
) -> None:
    """Publish a consensus-timeout ``OVERSEER_ALERT`` (#2264).

    Replaces the old auto-``choice`` HITL decision the orchestrator
    used to open at ``consensus_timeout_minutes``. The SDLC skill's
    existing ``OVERSEER_ALERT`` flow surfaces this as a non-blocking
    notification (Check agent logs / Acknowledge / Cancel pipeline)
    rather than gating the pipeline on a binary choice.

    Best-effort: if the message store import or write fails, log at
    WARNING and return — the orchestrator log is the always-on
    fallback (mirrors the slice-cascade alert path).
    """
    timeout_minutes = int(consensus_timeout / 60)
    phase_value = (
        pipeline.current_phase.value
        if hasattr(pipeline.current_phase, "value")
        else str(pipeline.current_phase)
    )
    # Subject role slot follows the SDLC skill convention
    # ``<anomaly_type>: <agent_role> [<priority>]`` (skills/sdlc/SKILL.md
    # §"Overseer Alert Detection") so "Check agent logs" extracts a role
    # the host can pass to ``get_container_logs``. Fall back to the phase
    # only when no blocking role is reported — the phase still appears in
    # ``metadata.phase`` regardless.
    subject_role = blocking_agents[0] if blocking_agents else phase_value
    subject = f"consensus-timeout: {subject_role} [{priority}]"
    blockers_render = ", ".join(blocking_agents) if blocking_agents else "(none reported)"
    proposal_render = (
        latest_proposal_at.isoformat() if latest_proposal_at is not None else "no proposals seen"
    )
    heartbeat_render = (
        latest_heartbeat_at.isoformat()
        if latest_heartbeat_at is not None
        else "no recent heartbeat"
    )
    body = (
        f"BRC consensus has not converged after {timeout_minutes} minutes "
        f"in phase '{phase_value}'.\n"
        f"Blocking agents: {blockers_render}\n"
        f"Latest proposal: {proposal_render}\n"
        f"Latest heartbeat (active roles): {heartbeat_render}\n\n"
        "The pipeline continues to poll for convergence (up to ~60 min "
        "before still-running containers are force-killed). If you want "
        "to intervene, use `cancel_task` to stop the pipeline or "
        "`restart_phase` to retry."
    )
    metadata: dict[str, Any] = {
        "anomaly_type": "consensus-timeout",
        "phase": phase_value,
        "blocking_agents": list(blocking_agents),
        "latest_proposal_at": (
            latest_proposal_at.isoformat() if latest_proposal_at is not None else None
        ),
        "latest_heartbeat_at": (
            latest_heartbeat_at.isoformat() if latest_heartbeat_at is not None else None
        ),
        "consensus_timeout_minutes": timeout_minutes,
        "priority": priority,
    }
    if slice_id is not None:
        metadata["slice_id"] = slice_id

    try:
        try:
            from message_store import Message, MessageType
        except ImportError:
            from ..message_store import (  # type: ignore[no-redef]
                Message,
                MessageType,
            )
        store_fn = _get_message_store()
        if store_fn is None:
            logger.warning(
                "Consensus-timeout alert: message store unavailable",
                pipeline_id=pipeline_id,
            )
            return
        msg_store = store_fn()
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.OVERSEER_ALERT,
                subject=subject,
                body=body,
                metadata=metadata,
                phase=phase_value,
            )
        )
    except Exception as e:
        logger.warning(
            "Failed to publish consensus-timeout OVERSEER_ALERT",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )


# Pipeline-branch divergence alert (#2224 PR 3).
#
# Watches ``origin/<pipeline_branch>`` for the contamination shape from
# #2222: branch is more than ``BRANCH_DIVERGENCE_THRESHOLD`` commits
# ahead of ``origin/<base>`` AND those ahead-commits contain merged-PR
# subject signatures (``(#NNNN)``).  A real pipeline branch grows by
# refine/plan/implement/state-file commits authored by agents — none of
# those would carry a ``(#NNNN)`` suffix in the subject.  When that
# signature appears, the branch has absorbed merged-main commits, which
# is the exact failure mode #2222 fixed at the root.
#
# Detection latency: the polling thread checks every 30 s, but the
# orchestrator's local ``origin/<pipeline_branch>`` only refreshes
# when it fetches — which happens at pipeline start, phase
# boundaries, and a few resume / signal paths (the polling thread
# itself does not fetch).  Contamination introduced mid-phase is
# therefore detected at the next phase boundary's fetch, not within
# 30 s.  This is **phase-boundary granularity, not real time** —
# strictly better than detecting at PR open, but defense-in-depth
# only; PR 1 (#2282) remains the primary gate.
#
# The signature heuristic is intentionally cheap and false-positive-
# tolerant — per the issue, "we'd rather over-alert than miss another
# contaminated PR."
BRANCH_DIVERGENCE_THRESHOLD = 20
_BRANCH_DIVERGENCE_PR_RE = re.compile(r"\(#\d+\)")


def _check_branch_divergence_for_alert(
    pipeline_id: str,
    worktree_repo_path: Path,
    pipeline_branch: str,
    base_branch: str,
    threshold: int = BRANCH_DIVERGENCE_THRESHOLD,
) -> tuple[int, list[tuple[str, str]]]:
    """Return ``(ahead_count, offenders)``.

    ``offenders`` is the list of ahead-commits whose subjects look like
    merged-main PRs (``(#NNNN)``) when the pipeline branch is more
    than ``threshold`` commits ahead of base.  Returns ``(0, [])`` when
    the branch is not far enough ahead, no signatures match, or any
    git invocation fails (best-effort — observability must never
    block the pipeline).  The caller relies on ``ahead_count`` for
    the alert body and uses ``offenders`` to decide whether to fire.
    """
    if not pipeline_branch or not base_branch or pipeline_branch == base_branch:
        return 0, []

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_repo_path}",
        "-C",
        str(worktree_repo_path),
    ]

    def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                [*git_base, *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug(
                "branch-divergence: git command failed",
                pipeline_id=pipeline_id,
                git_args=args,
                error=str(exc),
            )
            return None

    count = _run(
        [
            "rev-list",
            "--count",
            f"origin/{base_branch}..origin/{pipeline_branch}",
        ]
    )
    if count is None or count.returncode != 0:
        return 0, []
    try:
        ahead = int((count.stdout or "0").strip() or "0")
    except ValueError:
        return 0, []
    if ahead <= threshold:
        return ahead, []

    log = _run(
        [
            "log",
            "--no-merges",
            "--pretty=format:%H%x09%s",
            f"origin/{base_branch}..origin/{pipeline_branch}",
        ]
    )
    if log is None or log.returncode != 0:
        return ahead, []

    offenders: list[tuple[str, str]] = []
    for line in (log.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, subject = line.partition("\t")
        if not sha or not subject:
            continue
        if _BRANCH_DIVERGENCE_PR_RE.search(subject):
            offenders.append((sha, subject))
    return ahead, offenders


def _publish_branch_divergence_alert(
    pipeline: Pipeline,
    pipeline_id: str,
    *,
    pipeline_branch: str,
    base_branch: str,
    ahead_count: int,
    offenders: list[tuple[str, str]],
) -> None:
    """Publish an ``OVERSEER_ALERT`` for branch-divergence contamination.

    Best-effort: import or write failures are logged at WARNING and
    swallowed — the orchestrator log is the always-on fallback.
    """
    phase_value = (
        pipeline.current_phase.value
        if hasattr(pipeline.current_phase, "value")
        else str(pipeline.current_phase)
    )
    subject = f"branch-divergence: {pipeline_branch} contains merged-main commits"
    offender_render = "\n".join(f"  {sha[:12]} {subj}" for sha, subj in offenders[:10])
    if len(offenders) > 10:
        offender_render += f"\n  ... and {len(offenders) - 10} more"
    body = (
        f"Pipeline branch ``origin/{pipeline_branch}`` is {ahead_count} commits "
        f"ahead of ``origin/{base_branch}`` and contains {len(offenders)} "
        f"commit(s) whose subjects look like merged-main PRs "
        f"(``(#NNNN)`` signature).  This is the contamination shape "
        f"investigated in #2222 (Phase 4 / #2224 detector).\n\n"
        f"Offending commits:\n{offender_render}\n\n"
        f"If this is real contamination, the resulting PR will show a "
        f"borked diff against current main — see #2222 recovery procedure "
        f"(rebase ``--onto`` the right base).  If this is a false positive "
        f"(e.g. an agent legitimately copied a ``(#NNNN)`` reference into "
        f"a commit subject), no action is required."
    )
    metadata: dict[str, Any] = {
        "anomaly_type": "branch-divergence",
        "phase": phase_value,
        "pipeline_branch": pipeline_branch,
        "base_branch": base_branch,
        "ahead_count": ahead_count,
        "offending_shas": [sha for sha, _ in offenders],
    }

    try:
        try:
            from message_store import Message, MessageType
        except ImportError:
            from ..message_store import (  # type: ignore[no-redef]
                Message,
                MessageType,
            )
        store_fn = _get_message_store()
        if store_fn is None:
            logger.warning(
                "Branch-divergence alert: message store unavailable",
                pipeline_id=pipeline_id,
            )
            return
        msg_store = store_fn()
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.OVERSEER_ALERT,
                subject=subject,
                body=body,
                metadata=metadata,
                phase=phase_value,
            )
        )
    except Exception as e:
        logger.warning(
            "Failed to publish branch-divergence OVERSEER_ALERT",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )


def _branch_divergence_tick(
    pipeline_id: str,
    worktree_repo_path: Path,
    store: StateStore,
    alerted_shas: set[str],
) -> None:
    """One iteration of the branch-divergence detector.

    Extracted from the ``_health_monitor_poll`` closure so the
    dedupe + reset behavior is unit-testable.  Mutates ``alerted_shas``
    in place: adds newly-fired SHAs, and clears the set when the
    contamination window goes empty so re-introduction (same SHA,
    e.g. agent re-runs a bad rebase) re-fires per the issue's
    "rather over-alert than miss" stance.

    All errors are logged-and-swallowed — observability must never
    block the pipeline.
    """
    try:
        pipeline = store.load_pipeline(pipeline_id)
        branch = pipeline.branch
        base = pipeline.base_branch
        if not branch or not base:
            return
        ahead, offenders = _check_branch_divergence_for_alert(
            pipeline_id=pipeline_id,
            worktree_repo_path=worktree_repo_path,
            pipeline_branch=branch,
            base_branch=base,
        )
        if not offenders and alerted_shas:
            # Note: transient git errors in ``_check_branch_divergence_for_alert``
            # also surface as ``offenders == []`` and therefore flush the dedupe
            # set; this is intentional per #2224's "rather over-alert than miss"
            # posture — a flaky git tick will re-fire on the next clean tick.
            alerted_shas.clear()
        new_offenders = [(sha, subj) for sha, subj in offenders if sha not in alerted_shas]
        if new_offenders:
            _publish_branch_divergence_alert(
                pipeline,
                pipeline_id,
                pipeline_branch=branch,
                base_branch=base,
                ahead_count=ahead,
                offenders=new_offenders,
            )
            alerted_shas.update(sha for sha, _ in new_offenders)
    except Exception as div_err:
        logger.debug(
            "Branch-divergence check failed",
            pipeline_id=pipeline_id,
            error=str(div_err),
        )


def _handle_brc_consensus_timeout(
    pipeline: Pipeline,
    pipeline_id: str,
    consensus_timeout: float,
    blocking_agents: list[str],
    store: StateStore,  # noqa: ARG001 — kept for call-site compatibility (#2264)
    slice_id: str | None = None,
    active_role_names: list[str] | None = None,
) -> None:
    # Extracted from _run_concurrent_phase so k3s-style top-level-module
    # layouts (and tests) can exercise this path in isolation — issue #1783.
    # ``slice_id`` is propagated so per-slice trackers (#2137) are looked
    # up under the nested ``{pipeline_id}/{slice_id}`` key.
    #
    # Issue #2264: the auto-``choice`` HITL decision this used to open
    # was the wrong protocol shape — the platform should not gate the
    # pipeline on a binary choice when the operator already has the
    # levers (`cancel_task`, `restart_phase`, `provide_input`).  The
    # two former decision paths now publish ``OVERSEER_ALERT`` messages
    # so the SDLC skill's existing alert flow surfaces them as
    # notifications rather than a blocking decision.
    _brc_handled = False
    _brc_timeout_result: dict | None = None
    _brc_tracker = None
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (
                get_peer_consensus_tracker,  # type: ignore[no-redef]
            )

        _brc_tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
        if _brc_tracker is not None:
            _brc_timeout_result = _brc_tracker.handle_timeout()
            _brc_handled = _brc_tracker.is_timeout_handled()
            logger.info(
                "BRC timeout handler result",
                pipeline_id=pipeline_id,
                action=(_brc_timeout_result.get("action") if _brc_timeout_result else None),
                brc_handled=_brc_handled,
            )
    except Exception as e:
        logger.warning(
            "BRC timeout check failed, falling back to OVERSEER_ALERT",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    latest_proposal_at: datetime | None = None
    if _brc_tracker is not None:
        try:
            latest_proposal_at = _brc_tracker.get_latest_proposal_timestamp()
        except Exception as e:
            logger.warning(
                "Consensus-timeout alert proposal lookup failed",
                pipeline_id=pipeline_id,
                error=str(e),
                exc_info=True,
            )
    latest_heartbeat_at = _latest_active_role_heartbeat(active_role_names or [])

    if (
        _brc_handled
        and _brc_timeout_result is not None
        and _brc_timeout_result.get("action") == "escalate"
    ):
        # Narrow the alert's blocking_agents to the *critical* blockers
        # the tracker just escalated on. The caller-supplied
        # ``blocking_agents`` is the full unconfirmed-roles set
        # (advisory + critical) from ``evaluate()`` — surfacing
        # advisory roles on a high-priority alert dilutes the signal.
        critical_entries = _brc_timeout_result.get("critical_blockers") or []
        critical_role_names: list[str] = []
        for entry in critical_entries:
            for role in (entry.get("reviewer_role"), entry.get("producer_role")):
                if role and role not in critical_role_names:
                    critical_role_names.append(role)
        escalate_blocking = critical_role_names or blocking_agents
        _publish_consensus_timeout_alert(
            pipeline,
            pipeline_id,
            consensus_timeout,
            escalate_blocking,
            priority="high",
            latest_proposal_at=latest_proposal_at,
            latest_heartbeat_at=latest_heartbeat_at,
            slice_id=slice_id,
        )
    elif not _brc_handled:
        if _emit_event is not None:
            _emit_event(
                EventType.CONSENSUS_TIMEOUT,
                pipeline_id,
                data={
                    "timeout_minutes": consensus_timeout / 60,
                    "blocking_agents": blocking_agents,
                },
            )
        _publish_consensus_timeout_alert(
            pipeline,
            pipeline_id,
            consensus_timeout,
            blocking_agents,
            priority="medium",
            latest_proposal_at=latest_proposal_at,
            latest_heartbeat_at=latest_heartbeat_at,
            slice_id=slice_id,
        )


def _start_stacked_pr_reconciler(
    pipeline_id: str,
    contract_loader: Callable[[], Any],
    gateway,
    pipeline,
    *,
    interval_seconds: float | None = None,
    worktree_repo_path: Path | None = None,
    repo: str | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Start the periodic stacked-PR reconciler as a daemon thread (#2137 TASK-5-3).

    Returns ``(thread, stop_event)``: caller calls ``stop_event.set()``
    when the implement phase is shutting down so the daemon exits
    cleanly. The daemon loops on the configured interval and invokes
    :func:`stacked_pr_reconciler.reconcile_once` with callables that
    decouple it from the gateway client.

    The list-callables (``list_open_prs`` and ``list_remote_branches``)
    forward to ``GatewayClient.list_open_prs`` /
    ``GatewayClient.list_remote_branches`` — both route through
    existing per-agent allowlists. The rebase callable forwards to
    ``GatewayClient.rebase_onto``, which performs the full local
    rebase + ``--force-with-lease`` push + ``gh api PATCH base=…``
    retarget so an orphaned child PR is fully healed on origin
    rather than just locally rewritten.
    """
    try:
        from orchestrator.env_config import get_stacked_pr_reconciler_interval_seconds
    except ImportError:
        from env_config import (  # type: ignore[no-redef]
            get_stacked_pr_reconciler_interval_seconds,
        )
    try:
        from orchestrator.stacked_pr_reconciler import reconcile_once
    except ImportError:
        from stacked_pr_reconciler import reconcile_once  # type: ignore[no-redef]

    if interval_seconds is None:
        try:
            interval_seconds = float(get_stacked_pr_reconciler_interval_seconds())
        except Exception:  # noqa: BLE001
            interval_seconds = 30.0

    stop_event = threading.Event()

    # ``repo_path`` must be a filesystem path the gateway's
    # ``validate_repo_path`` accepts (``/home/egg/repos/``,
    # ``/home/egg/.egg-worktrees/``, etc.) — NOT the git branch
    # name. Use the orchestrator-side worktree path that the
    # implement loop already owns.
    repo_path_str = str(worktree_repo_path) if worktree_repo_path is not None else ""
    pr_repo = repo or str(getattr(pipeline, "repo", "") or "")

    def _list_open_prs() -> list[dict[str, Any]]:
        # Lists open PRs in ``pr_repo`` so ``find_orphaned_child_prs``
        # can detect children whose base branch was deleted (parent
        # merged through the GitHub UI). Routes through the existing
        # per-agent ``gh pr list`` allowlist on the gateway — no new
        # privileged endpoint (decision-15).
        if not pr_repo:
            return []
        try:
            return list(gateway.list_open_prs(pipeline_id, pr_repo, agent_role="coder"))
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "stacked_pr_reconciler: list_open_prs raised — treating as empty",
                pipeline_id=pipeline_id,
                error=str(exc),
            )
            return []

    def _list_extant_branches() -> set[str]:
        # Lists remote branches via ``git ls-remote --heads origin``
        # so the reconciler can detect deleted parents. Routes through
        # the existing per-agent ``git ls-remote`` allowlist.
        if not repo_path_str:
            return set()
        try:
            return set(
                gateway.list_remote_branches(
                    pipeline_id,
                    repo_path_str,
                    agent_role="coder",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "stacked_pr_reconciler: list_remote_branches raised — treating as empty",
                pipeline_id=pipeline_id,
                error=str(exc),
            )
            return set()

    def _rebase_onto(orphan: Any) -> bool:
        # ``orphan`` is a ``stacked_pr_reconciler.OrphanedChildPR``;
        # avoid the import here so this module stays a pure consumer
        # of the reconciler's typed interface (the type checker at
        # the reconciler boundary already validates the shape).
        try:
            return bool(
                gateway.rebase_onto(
                    pipeline_id,
                    repo_path_str,
                    branch=orphan.branch,
                    new_base=orphan.intended_new_base,
                    old_base=orphan.deleted_base,
                    pr_number=orphan.pr_number,
                    repo=pr_repo or None,
                    agent_role="coder",
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "stacked_pr_reconciler: rebase_onto raised — counted as failure",
                pipeline_id=pipeline_id,
                branch=getattr(orphan, "branch", "?"),
            )
            return False

    def _loop() -> None:
        # Defensive: a slow tick must not pin this thread on a stale
        # sleep — Event.wait returns True the moment ``stop_event`` is
        # set, so shutdown is bounded by the configured interval.
        while not stop_event.wait(interval_seconds):
            try:
                contract = contract_loader()
                if contract is None:
                    continue
                reconcile_once(
                    contract,
                    list_open_prs=_list_open_prs,
                    list_extant_branches=_list_extant_branches,
                    rebase_onto=_rebase_onto,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "stacked_pr_reconciler tick raised — continuing",
                    pipeline_id=pipeline_id,
                    error=str(exc),
                )

    thread = threading.Thread(
        target=_loop,
        name=f"stacked-pr-reconciler-{pipeline_id}",
        daemon=True,
    )
    thread.start()
    return thread, stop_event


def _run_implement_phase_slices(
    pipeline_id: str,
    pipeline: Pipeline,
    spawner,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    sandbox_env: dict[str, str],
    store,
    certs_volume: str | None,
    worktree_repo_path: Path,
) -> tuple[int, str]:
    """Drive the implement phase as a DAG of independent slices (#2137).

    For each wave produced by :class:`SliceScheduler`, spawns a fresh
    BRC team per slice and waits for that slice's consensus before
    advancing the scheduler. Each slice runs through the existing
    :func:`_run_concurrent_phase` machinery with a slice-scoped tracker
    namespace (``{pipeline_id}/{slice_id}``) and slice-scoped per-role
    branches (``egg/issue-N/{slice_id}/{role}/work``).

    Per-slice PRs are opened via ``GatewayClient.create_slice_pr`` after
    each slice reaches CONSENSUS_CONFIRMED — root slices target the
    pipeline branch; child slices target their parent slice's
    integration branch. The stacked-PR reconciler runs in parallel as a
    daemon thread for the lifetime of this call.

    Returns ``(exit_code, logs)`` where ``exit_code == 0`` means every
    slice reached CONFIRMED; non-zero means at least one slice failed.
    """
    try:
        from orchestrator.slice_scheduler import SliceScheduler
    except ImportError:
        from slice_scheduler import SliceScheduler  # type: ignore[no-redef]

    try:
        from egg_contracts.loader import load_contract, save_contract
    except ImportError as exc:
        logger.error(
            "Slice loop: egg_contracts.loader unavailable — falling back",
            pipeline_id=pipeline_id,
            error=str(exc),
        )
        return 1, "slice loop bootstrap failed"

    contract = load_contract(pipeline_id, worktree_repo_path)
    slices = list(getattr(contract, "slices", []) or [])
    if not slices:
        logger.warning(
            "Slice loop: contract has no slices, falling back to monolithic implement",
            pipeline_id=pipeline_id,
        )
        return 1, "no slices in contract"

    pipeline_branch = pipeline.branch or (
        f"egg/issue-{pipeline.issue_number}/work"
        if pipeline.issue_number is not None
        else f"egg/{pipeline_id}/work"
    )
    issue_number = pipeline.issue_number
    # Slice integration branches stack as siblings of the pipeline tip
    # under ``egg/<id>/`` (see :func:`_ensure_pipeline_work_ref` for the
    # ``/work`` namespace decision in #2399). The namespace root drops the
    # trailing ``/work`` so slice paths build to ``<root>/slice-M`` rather
    # than ``<root>/work/slice-M``. The qualifier suffix (``-v3``,
    # ``-backend``) is preserved through ``pipeline.branch`` so two
    # qualified pipelines for the same issue do not collide on
    # ``egg/issue-N/slice-M`` (#2368).
    issue_branch = _slice_namespace_root(pipeline_branch)

    # Wrap scheduler construction so the run loop doesn't crash if the
    # contract bypassed plan-ingestion validation and reaches the
    # scheduler with a multi-parent / cyclic forest. ``SliceScheduler``
    # raises ``ValueError`` with the structured forest errors; surface
    # them to the operator via the existing return path so the run
    # loop can route to HITL escalation rather than wedge the pipeline.
    try:
        scheduler = SliceScheduler(contract)
    except ValueError as exc:
        logger.error(
            "Slice loop: scheduler refused to start (forest validation failed)",
            pipeline_id=pipeline_id,
            error=str(exc),
        )
        return 1, f"slice scheduler validation failed: {exc}"

    def _contract_loader() -> Any:
        try:
            return load_contract(pipeline_id, worktree_repo_path)
        except Exception:  # noqa: BLE001
            return None

    reconciler_thread, reconciler_stop = _start_stacked_pr_reconciler(
        pipeline_id,
        _contract_loader,
        spawner.gateway,
        pipeline,
        worktree_repo_path=worktree_repo_path,
        repo=getattr(pipeline, "repo", None),
    )

    aggregate_logs: list[str] = []
    overall_exit = 0
    poll_interval = 5.0

    try:
        from orchestrator import global_slice_admit
    except ImportError:
        import global_slice_admit  # type: ignore[no-redef]

    try:
        while not scheduler.all_done():
            # 1. Snapshot ready slices for this tick.
            ready_batch = list(scheduler.iter_ready())
            if not ready_batch:
                # 2. Drain cascades whose grace window expired so the
                #    descendants are visibly BLOCKED in the runtime view
                #    and we don't busy-spin.
                events = scheduler.poll_cascades()
                for event in events:
                    logger.warning(
                        "Slice cascade fired",
                        pipeline_id=pipeline_id,
                        failed_slice=event.failed_slice_id,
                        blocked=event.blocked_subtree,
                    )
                    try:
                        from orchestrator.gateway_client import (
                            get_gateway_client as _get_gateway_client,
                        )

                        _ = _get_gateway_client  # noqa: F841 — kept for symmetry
                    except Exception:  # noqa: BLE001
                        pass
                if scheduler.all_done():
                    break
                time.sleep(poll_interval)
                continue

            # Run every ready slice in this wave in parallel
            # (#2137 TASK-4-4 + decision-5: unbounded). The
            # ``max_parallel_slices`` cap from ``iter_ready`` already
            # bounds ``ready_batch`` so the executor's worker pool
            # mirrors that cap. Each slice runs through the existing
            # ``_run_concurrent_phase`` machinery in its own thread.
            # Per-slice failure / completion events are recorded back
            # on the scheduler from inside ``_run_one_slice`` so the
            # cascade machinery sees the same wall-clock as the run
            # loop.
            try:
                from orchestrator.peer_consensus import (
                    remove_peer_consensus_tracker,
                )
            except ImportError:
                from peer_consensus import (  # type: ignore[no-redef]
                    remove_peer_consensus_tracker,
                )

            try:
                from state_store import get_pipeline_state_lock
            except ImportError:
                from orchestrator.state_store import (  # type: ignore[no-redef]
                    get_pipeline_state_lock,
                )

            def _run_one_slice(slice_id: str, parent_slice_id: str | None) -> tuple[int, str]:
                # Release the global-admission slot when the slice
                # exits, regardless of how (consensus, failure, raised
                # exception). Idempotent — safe even if a future
                # codepath calls release() somewhere else (#2241 gap 1).
                try:
                    return _run_one_slice_inner(slice_id, parent_slice_id)
                finally:
                    global_slice_admit.release(pipeline_id, slice_id)

            def _run_one_slice_inner(slice_id: str, parent_slice_id: str | None) -> tuple[int, str]:
                # Resolve parent branch for stacking.
                if parent_slice_id is None:
                    parent_branch = pipeline_branch
                else:
                    parent_branch = f"{issue_branch}/{parent_slice_id}"
                integration_branch = f"{issue_branch}/{slice_id}"

                # Persist the parent-branch reference on the contract
                # under the per-pipeline state lock so a concurrent
                # tester / documenter contract write doesn't race with
                # ours (reviewer_code v4 #5).
                try:
                    with get_pipeline_state_lock(pipeline_id):
                        contract_local = load_contract(pipeline_id, worktree_repo_path)
                        for s in contract_local.slices:
                            if s.id == slice_id:
                                s.parent_branch_at_creation = parent_branch
                                break
                        save_contract(contract_local, worktree_repo_path)
                except Exception as save_err:  # noqa: BLE001
                    logger.warning(
                        "Failed to persist parent_branch_at_creation",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        error=str(save_err),
                    )

                # #2137 TASK-4-2: create the slice integration branch
                # on origin BEFORE spawning containers. Push
                # ``parent_branch:refs/heads/integration_branch``
                # through the existing per-agent push allowlist. Agents
                # then push their commits directly to the slice's
                # integration branch (``egg/issue-N/slice-M``) so the
                # slice PR's diff is non-empty when ``gh pr create``
                # runs. On failure, mark the slice failed so the
                # cascade machinery can surface the missing-parent
                # error to the operator instead of silently spawning
                # agents that would push to a missing parent.
                if pipeline.repo:
                    try:
                        branch_ok = bool(
                            spawner.gateway.create_slice_integration_branch(
                                pipeline_id,
                                str(worktree_repo_path),
                                integration_branch=integration_branch,
                                parent_branch=parent_branch,
                                agent_role="coder",
                                mode=gateway_mode,  # type: ignore[arg-type]
                            )
                        )
                    except Exception as branch_err:  # noqa: BLE001
                        logger.error(
                            "Slice integration branch creation raised",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            error=str(branch_err),
                        )
                        branch_ok = False
                    if not branch_ok:
                        logger.error(
                            "Slice integration branch creation failed; "
                            "marking slice failed (agents not spawned)",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            parent_branch=parent_branch,
                            integration_branch=integration_branch,
                        )
                        scheduler.record_failure(slice_id)
                        return 1, (
                            f"slice {slice_id}: integration branch "
                            f"{integration_branch} could not be created from "
                            f"{parent_branch}"
                        )

                logger.info(
                    "Slice spawn",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    parent_branch=parent_branch,
                    integration_branch=integration_branch,
                )

                exit_code_inner, logs_inner = _run_concurrent_phase_with_impasse_retry(
                    pipeline_id=pipeline_id,
                    pipeline=pipeline,
                    phase="implement",
                    spawner=spawner,
                    repo_volumes=repo_volumes,
                    gateway_mode=gateway_mode,
                    repos=repos,
                    sandbox_env=sandbox_env,
                    store=store,
                    certs_volume=certs_volume,
                    worktree_repo_path=worktree_repo_path,
                    slice_id=slice_id,
                )

                if exit_code_inner != 0:
                    scheduler.record_failure(slice_id)
                    logger.warning(
                        "Slice failed",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        exit_code=exit_code_inner,
                    )
                    return exit_code_inner, logs_inner

                # Slice consensus reached — snapshot the slice's PR
                # data under the per-pipeline state lock, then RELEASE
                # the lock before the gateway HTTP round-trip so we
                # don't serialise other contract writers for the
                # gateway timeout (~30 s). The lock only needs to
                # cover the contract read.
                slice_pr_data: dict[str, Any] | None = None
                try:
                    with get_pipeline_state_lock(pipeline_id):
                        contract_post = load_contract(pipeline_id, worktree_repo_path)
                        slice_obj = next(
                            (s for s in contract_post.slices if s.id == slice_id),
                            None,
                        )
                        if slice_obj is not None and pipeline.repo:
                            # Identify the terminal slice(s) of the
                            # forest: any slice no other slice lists
                            # as a dependency. Pick a single terminal
                            # so non-terminal slices can flag it via
                            # ``terminal_slice_id`` (the gateway uses
                            # that signal to switch the title shape:
                            # bare ``program_title`` for the terminal
                            # vs. ``[<slice-id>] <program_title>`` for
                            # non-terminals). When the forest has
                            # multiple terminal slices we take the
                            # last one in declared order — arbitrary
                            # but stable. For multi-tree forests this
                            # means non-terminal slices in non-chosen
                            # trees flag a leaf outside their own
                            # subtree; see the "Stacked-PR creation"
                            # section of
                            # ``docs/architecture/slice-dag.md`` for
                            # the rationale.
                            depended_on: set[str] = {
                                dep for s in contract_post.slices for dep in s.dependencies
                            }
                            terminal_ids = [
                                s.id for s in contract_post.slices if s.id not in depended_on
                            ]
                            chosen_terminal = terminal_ids[-1] if terminal_ids else None
                            is_terminal = slice_id == chosen_terminal
                            # #2538: every slice — terminal or not —
                            # carries the planner-authored narrative on
                            # its PR so reviewers see context on whichever
                            # slice they open first. Per-merge obligations
                            # remain terminal-only (the merge gate is the
                            # last-to-merge PR in the stack).
                            program_pr = contract_post.pr
                            # ``terminal_slice_id`` is the gateway's
                            # title-shape signal: None on the terminal
                            # itself (or when no umbrella narrative
                            # exists), the terminal id otherwise. We
                            # only flag non-terminals when the umbrella
                            # actually has a planner-authored title;
                            # otherwise both terminal and non-terminal
                            # fall back to the deterministic
                            # ``slice {id}: {name}`` form.
                            umbrella_has_program_block = bool(
                                program_pr and program_pr.title and program_pr.title.strip()
                            )
                            terminal_pointer = (
                                None
                                if is_terminal or not umbrella_has_program_block
                                else chosen_terminal
                            )
                            slice_pr_data = {
                                "slice_name": slice_obj.name or slice_id,
                                "slice_tasks": [
                                    {"id": t.id, "description": t.description}
                                    for t in (slice_obj.tasks or [])
                                ],
                                "program_title": (program_pr.title if program_pr else None),
                                "program_description": (
                                    program_pr.description if program_pr else None
                                ),
                                "program_test_plan": (program_pr.test_plan if program_pr else None),
                                "program_manual_steps": (
                                    program_pr.manual_steps if program_pr else None
                                ),
                                # Snapshot the obligations list under the
                                # state lock alongside the rest of the
                                # program-* fields. Threaded into
                                # ``create_slice_pr`` only for the
                                # terminal slice; non-terminal slices
                                # receive ``None`` so the umbrella is the
                                # single place reviewers see them (#2354).
                                #
                                # Collect via ``_collect_pre_merge_obligations``
                                # rather than passing raw ``DeferredAction``
                                # objects so the umbrella picks up the live
                                # peer_consensus tracker fallback when the
                                # contract list is empty — exact parity with
                                # the legacy ``_auto_create_pr`` path
                                # (#2354 review item 2).
                                "program_deferred_actions": (
                                    (
                                        _collect_pre_merge_obligations(
                                            pipeline_id,
                                            list(program_pr.deferred_actions),
                                        )
                                        or None
                                    )
                                    if program_pr and is_terminal
                                    else None
                                ),
                                "terminal_slice_id": terminal_pointer,
                            }
                except Exception as load_err:  # noqa: BLE001
                    logger.warning(
                        "Slice PR pre-load failed (continuing)",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        error=str(load_err),
                    )

                pr_created = True
                if slice_pr_data is not None and pipeline.repo:
                    try:
                        spawner.gateway.create_slice_pr(
                            pipeline_id=pipeline_id,
                            repo=pipeline.repo,
                            slice_id=slice_id,
                            slice_name=slice_pr_data["slice_name"],
                            slice_tasks=slice_pr_data["slice_tasks"],
                            head=integration_branch,
                            base=parent_branch,
                            issue_number=issue_number,
                            agent_role="orchestrator",
                            mode=gateway_mode,  # type: ignore[arg-type]
                            program_title=slice_pr_data["program_title"],
                            program_description=slice_pr_data["program_description"],
                            program_test_plan=slice_pr_data["program_test_plan"],
                            program_manual_steps=slice_pr_data["program_manual_steps"],
                            program_deferred_actions=slice_pr_data["program_deferred_actions"],
                            terminal_slice_id=slice_pr_data["terminal_slice_id"],
                        )
                    except Exception as pr_err:  # noqa: BLE001
                        logger.error(
                            "Slice PR creation failed",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            error=str(pr_err),
                        )
                        pr_created = False

                if not pr_created:
                    scheduler.record_failure(slice_id)
                    return 1, (
                        f"slice {slice_id}: PR creation failed (head={integration_branch}, "
                        f"base={parent_branch})"
                    )

                scheduler.record_complete(slice_id)
                try:
                    remove_peer_consensus_tracker(pipeline_id, slice_id)
                except Exception:  # noqa: BLE001
                    pass
                return exit_code_inner, logs_inner

            # Gate every ready slice through the orchestrator-process-wide
            # admission counter (#2241 gap 1). Slices the global cap
            # rejects stay in READY and re-yield next tick — the per-
            # pipeline ``iter_ready`` accounting is unaffected because
            # we admit BEFORE ``mark_spawned``. If the entire batch is
            # rejected, sleep one poll interval before re-checking so
            # we don't burn CPU spinning on iter_ready.
            admitted_batch: list[tuple[str, str | None]] = [
                (slice_id, parent_slice_id)
                for slice_id, parent_slice_id in ready_batch
                if global_slice_admit.try_admit(pipeline_id, slice_id)
            ]
            if not admitted_batch:
                logger.info(
                    "Slice wave deferred behind global cap",
                    pipeline_id=pipeline_id,
                    ready=[s for s, _ in ready_batch],
                    admit=global_slice_admit.snapshot(),
                )
                time.sleep(poll_interval)
                continue

            # Mark admitted slices as spawned BEFORE submitting them to
            # the executor so a subsequent ``iter_ready`` from any other
            # thread sees the in-flight count correctly.
            for slice_id, _parent in admitted_batch:
                scheduler.mark_spawned(slice_id)

            max_workers = max(1, len(admitted_batch))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f"slice-wave-{pipeline_id}",
            ) as wave_pool:
                futures: dict[concurrent.futures.Future, str] = {}
                for slice_id, parent_slice_id in admitted_batch:
                    fut = wave_pool.submit(_run_one_slice, slice_id, parent_slice_id)
                    futures[fut] = slice_id

                for fut in concurrent.futures.as_completed(futures):
                    slice_id_done = futures[fut]
                    try:
                        exit_code, logs = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        scheduler.record_failure(slice_id_done)
                        exit_code = 1
                        logs = f"slice {slice_id_done} raised: {exc!r}"
                        logger.error(
                            "Slice worker raised",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id_done,
                            error=str(exc),
                        )
                    aggregate_logs.append(f"--- slice {slice_id_done} ---\n{logs}")
                    if exit_code != 0:
                        overall_exit = exit_code

            # Drain cascades after each wave so descendants of a
            # failed slice are visibly BLOCKED before the next
            # iteration computes ready slices. Emit an
            # OVERSEER_ALERT per cascade so the human operator sees
            # the blocked subtree (#2137 TASK-3-4 emission path).
            events = scheduler.poll_cascades()
            for event in events:
                logger.warning(
                    "Slice cascade fired",
                    pipeline_id=pipeline_id,
                    failed_slice=event.failed_slice_id,
                    blocked=event.blocked_subtree,
                )
                # Emit OVERSEER_ALERT directly through the in-process
                # message store so the human operator's overseer
                # surface picks up the cascade-block event (TASK-3-4
                # emission path).
                try:
                    try:
                        from message_store import Message, get_message_store
                    except ImportError:
                        from orchestrator.message_store import (  # type: ignore[no-redef]
                            Message,
                            get_message_store,
                        )
                    msg = Message(
                        pipeline_id=pipeline_id,
                        from_role="orchestrator",
                        to_role="all",
                        message_type="OVERSEER_ALERT",
                        subject=f"slice-cascade-block: {event.failed_slice_id}",
                        body=(
                            f"Slice {event.failed_slice_id} failed; "
                            f"downstream subtree {event.blocked_subtree} marked "
                            "BLOCKED_ON_FAILED_DEPENDENCY (60 s grace expired). "
                            "HITL resolution required to restart the failed slice."
                        ),
                        metadata={
                            "anomaly": "slice-cascade-block",
                            "priority": "high",
                            "failed_slice_id": event.failed_slice_id,
                            "blocked_subtree": list(event.blocked_subtree),
                        },
                        phase="implement",
                    )
                    get_message_store().add_message(msg)
                except Exception:  # noqa: BLE001
                    # Best-effort: the log line above is the
                    # always-on fallback so the operator still sees
                    # the cascade in the orchestrator log.
                    pass
    finally:
        reconciler_stop.set()
        try:
            reconciler_thread.join(timeout=5.0)
        except Exception:  # noqa: BLE001
            pass

    aggregated = "\n".join(aggregate_logs) if aggregate_logs else "Slice loop completed."
    return overall_exit, aggregated


def _run_concurrent_phase_with_impasse_retry(
    pipeline_id: str,
    pipeline: Pipeline,
    phase: str,
    spawner,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    sandbox_env: dict[str, str],
    store,
    certs_volume: str | None,
    worktree_repo_path: Path,
    review_feedback: str | None = None,
    slice_id: str | None = None,
) -> tuple[int, str]:
    """Run a concurrent phase, auto-delegating impasses once before HITL.

    Wraps :func:`_run_concurrent_phase` with the runtime escape-hatch
    introduced in #2529:

    1. Run the BRC cycle as usual.
    2. After it exits, scan each producer's ``AgentOutput`` for a typed
       :class:`egg_contracts.Impasse`.
    3. For ``WRONG_ROLE`` impasses with a single eligible alternative
       producer role and ``task.delegation_attempts == 0``, mutate
       ``task.role`` to the suggested role and re-run the BRC cycle
       once. The new spawn picks up the role flip when
       ``_build_agent_prompt`` re-reads the contract.
    4. For everything else (second impasse, non-WRONG_ROLE category,
       no eligible alternative role, unresolvable task_id) the helper
       creates a HITL decision on the contract and the slice exits
       so the operator can choose between cancel / re-plan / manual
       resolution. ``feedback_no_auto_hitl.md``: the orchestrator
       creates the decision; surfacing to the user is the operator
       layer's job.

    Pipeline-level (non-sliced) callers can pass ``slice_id=None``;
    the routing helper falls back to a contract-wide search for the
    impassed task.
    """
    try:
        from impasse_routing import (
            ImpasseAction,
            collect_impasses,
            route_impasses,
        )
    except ImportError:
        from orchestrator.impasse_routing import (  # type: ignore[no-redef]
            ImpasseAction,
            collect_impasses,
            route_impasses,
        )
    try:
        from egg_contracts.agent_roles import AgentRole as ContractAgentRoleEnum
    except ImportError:  # pragma: no cover - import seam parity
        from shared.egg_contracts.agent_roles import (  # type: ignore[no-redef]
            AgentRole as ContractAgentRoleEnum,
        )

    # Two attempts max: original + at most one delegated retry. The
    # ``delegation_attempts`` counter on the contract task enforces the
    # same bound when the slice is restarted out-of-band by an
    # operator, so a long-lived pipeline can never escape this gate.
    MAX_IMPASSE_ATTEMPTS = 2

    last_exit = 0
    last_logs = ""
    for attempt in range(MAX_IMPASSE_ATTEMPTS):
        last_exit, last_logs = _run_concurrent_phase(
            pipeline_id=pipeline_id,
            pipeline=pipeline,
            phase=phase,
            spawner=spawner,
            repo_volumes=repo_volumes,
            gateway_mode=gateway_mode,
            repos=repos,
            sandbox_env=sandbox_env,
            store=store,
            certs_volume=certs_volume,
            worktree_repo_path=worktree_repo_path,
            review_feedback=review_feedback,
            slice_id=slice_id,
        )

        # Producer roles only — impasses are a producer concept;
        # reviewers don't author tasks. Mirrors the producer trio in
        # ``shared/egg_restrictions/patterns.py``.
        producer_roles = [
            ContractAgentRoleEnum.CODER,
            ContractAgentRoleEnum.TESTER,
            ContractAgentRoleEnum.DOCUMENTER,
        ]

        try:
            impasses = collect_impasses(
                Path(worktree_repo_path),
                pipeline_id,
                producer_roles,
            )
        except Exception as scan_err:  # noqa: BLE001
            logger.warning(
                "Impasse scan raised; continuing without delegation",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(scan_err),
            )
            return last_exit, last_logs

        if not impasses:
            return last_exit, last_logs

        try:
            decisions = route_impasses(
                repo_path=Path(worktree_repo_path),
                pipeline_id=pipeline_id,
                contract_identifier=pipeline_id,
                impasses=impasses,
                slice_id=slice_id,
            )
        except Exception as route_err:  # noqa: BLE001
            logger.error(
                "Impasse routing raised; surfacing slice failure",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(route_err),
            )
            return last_exit, last_logs

        all_delegated = decisions and all(d.action == ImpasseAction.DELEGATE for d in decisions)
        if not all_delegated:
            # Any escalation, or an empty decision list, means the
            # operator gates the next move. Don't auto-retry.
            for d in decisions:
                logger.info(
                    "Impasse decision",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    action=d.action.value,
                    role=d.role,
                    task_id=d.task_id,
                    new_role=d.new_role,
                    reason=d.reason,
                    hitl_decision_id=d.hitl_decision_id,
                )
            return last_exit, last_logs

        # All impasses delegated cleanly — the contract has been
        # mutated, log the swap and let the loop respawn with the new
        # roles. Last attempt falls through and returns whatever the
        # second BRC cycle produced.
        for d in decisions:
            logger.info(
                "Impasse delegated; retrying slice with new role",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                attempt=attempt + 1,
                from_role=d.role,
                to_role=d.new_role,
                task_id=d.task_id,
            )

    return last_exit, last_logs


def _run_concurrent_phase(
    pipeline_id: str,
    pipeline: Pipeline,
    phase: str,
    spawner,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    sandbox_env: dict[str, str],
    store,
    certs_volume: str | None,
    worktree_repo_path: Path,
    review_feedback: str | None = None,
    slice_id: str | None = None,
) -> tuple[int, str]:
    """Run a phase using concurrent all-agents-at-once execution.

    Creates a ConcurrentPhaseExecutor that spawns all agents simultaneously,
    all sharing the pipeline branch. Each container receives a role-specific
    prompt built via ``_build_agent_prompt``. After spawning, waits for all
    containers to exit and records their state in the pipeline store.

    Returns:
        (exit_code, logs) — 0 on success.

    Raises:
        SpawnFailureError: If any agent fails to spawn. Survivors are stopped
            and their pipeline-state records are marked FAILED before the
            exception propagates. Distinguishes spawn failures from container
            exits so the outer caller's ``pipeline.error`` is accurate.
    """
    from models import (
        AgentExecution as StateAgentExecution,
    )
    from models import (
        AgentExecutionStatus as StateAgentStatus,
    )
    from models import (
        ContainerInfo,
        ContainerStatus,
        PipelinePhase,
        resolve_consensus_timeout_minutes,
    )

    try:
        from concurrent_executor import (
            ConcurrentPhaseExecutor,
            _is_transient_agent_error,
        )
    except ImportError:
        from ..concurrent_executor import (  # type: ignore
            ConcurrentPhaseExecutor,
            _is_transient_agent_error,
        )

    phase_str = phase if isinstance(phase, str) else phase.value
    pipeline_mode = "issue" if pipeline.issue_number is not None else "prompt"

    # Slice-aware sandbox env (#2137 TASK-4-3 / #2403): when running a
    # per-slice team, the spawner exposes the slice id via
    # ``EGG_SLICE_ID`` and leaves ``EGG_PIPELINE_ID`` as the bare
    # pipeline id. An earlier shape encoded the slice into
    # ``EGG_PIPELINE_ID`` itself (``{pipeline_id}/{slice_id}``) so the
    # orchestrator's ``_tracker_key`` would route CONSENSUS_* to the
    # slice tracker without an extra signal-level field. That broke
    # every agent → orchestrator round-trip:
    #
    #   * the orchestrator-side ``PIPELINE_ID_PATTERN`` and the agent
    #     handler validator (``[a-zA-Z0-9_-]+``) both reject the slash,
    #   * Flask's default URL converter doesn't allow ``/``, so every
    #     ``POST /api/v1/pipelines/{pid}/...`` route 404s — i.e. all
    #     of progress, BRC, heartbeat, message, phase, decision, etc.
    #
    # Slice routing is plumbed explicitly instead: the BRC handlers
    # pull ``EGG_SLICE_ID`` and forward it on the signal payload, and
    # the orchestrator's signal handlers feed it into
    # ``get_peer_consensus_tracker(pipeline_id, slice_id)``. CONSENSUS_*
    # isolation is preserved; HEARTBEAT and OVERSEER_ALERT are not
    # tracker-scoped at all — ``handle_heartbeat_signal`` is a no-op
    # ACK with no tracker lookup, and OVERSEER_ALERT flows through the
    # message bus (``MessageType.OVERSEER_ALERT``) rather than the
    # consensus tracker. So per-slice scoping doesn't apply to either,
    # and operator telemetry stays pipeline-wide as before. The
    # pipeline-level fan-out for OVERSEER_ALERT mentioned in earlier
    # comments here is tracked alongside the per-slice MCP control
    # verbs in #2199.
    #
    # Single source of truth (#2410 v2 review): ``EGG_SLICE_ID`` is
    # injected by ``KubernetesSpawner.spawn_agent_job`` from the same
    # ``slice_id`` parameter that drives Job naming and worktree id, so
    # there is no need to also stuff it into ``sandbox_env`` here. The
    # key is in ``_PROTECTED_ENV_KEYS`` so any future caller that does
    # supply a value via ``extra_env`` is logged and overridden.

    # Build per-role prompts for concurrent phase execution.
    from egg_contracts.agent_roles import get_roles_for_phase as _get_roles_for_phase

    roles: list[AgentRole] = []
    _roster_override = getattr(pipeline, "active_roles", None)
    if _roster_override:
        # CUSTOM-mode (#1762) and BABYSIT pipelines persist their resolved
        # roster on pipeline.active_roles. Use it verbatim so in-flight
        # pipelines survive role-roster version bumps.
        for r_value in _roster_override:
            try:
                roles.append(AgentRole(r_value))
            except ValueError:
                # Role value from a newer schema the orchestrator can't
                # spawn yet — skip it so BRC doesn't wait on an unspawnable
                # agent.
                continue
    else:
        for r in _get_roles_for_phase(
            phase_str,
            include_reviewers=True,
            repo=pipeline.repo,
            has_contract=getattr(pipeline, "has_contract", True),
        ):
            try:
                roles.append(AgentRole(r.value))
            except ValueError:
                # New roles not yet in orchestrator AgentRole — skip
                continue

    # Build a review graph filtered to only active roles so consensus
    # tracking doesn't wait for unspawned agents.
    from review_graph import ReviewGraph
    from review_graph import get_review_graph_for_phase as _get_graph

    full_graph = _get_graph(phase_str, repo=pipeline.repo)
    active_role_names = {r.value for r in roles}
    filtered_edges = [
        e
        for e in full_graph.edges
        if e.reviewer_role in active_role_names and e.producer_role in active_role_names
    ]
    filtered_graph = ReviewGraph(filtered_edges)

    # Resolve base branch for diff commands in agent prompts.
    _resolved_base_branch = pipeline.base_branch
    if not _resolved_base_branch:
        try:
            _resolved_base_branch = get_default_branch(worktree_repo_path)
        except Exception:
            _resolved_base_branch = None

    agent_prompts: dict[AgentRole, str] = {}
    for role in roles:
        prompt = _build_agent_prompt(
            role_value=role.value,
            phase=phase_str,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            prompt=pipeline.prompt,
            issue_number=pipeline.issue_number,
            repo=pipeline.repo,
            branch=pipeline.branch,
            base_branch=_resolved_base_branch,
            repo_path=str(worktree_repo_path),
            concurrent=True,
            review_feedback=review_feedback,
            network_mode=gateway_mode,
            mode=pipeline.mode,
            pr_number=getattr(pipeline, "pr_number", None),
        )
        agent_prompts[role] = prompt

    # Create spawn function and executor.
    spawn_fn = spawner.create_concurrent_spawn_fn(
        pipeline_id=pipeline_id,
        issue_number=pipeline.issue_number,
        repo_volumes=repo_volumes,
        mode=gateway_mode,
        repos=repos,
        phase=phase_str,
        sandbox_env=sandbox_env,
        certs_volume=certs_volume,
        base_branch=pipeline.base_branch,
        spawn_max_retries=pipeline.config.spawn_max_retries,
        spawn_retry_initial_backoff_seconds=pipeline.config.spawn_retry_initial_backoff_seconds,
        slice_id=slice_id,
    )

    max_concurrent = getattr(pipeline.config, "max_concurrent_agents", 6)
    executor = ConcurrentPhaseExecutor(
        pipeline=pipeline,
        spawn_fn=spawn_fn,
        max_concurrent=max_concurrent,
        review_graph=filtered_graph,
        roles=roles,
        slice_id=slice_id,
    )

    # Spawn all agents with their prompts.
    executions = executor.spawn_all(agent_prompts=agent_prompts)

    # Phase-level retry for transient spawn failures (#1879).  Per-role
    # retries in kubernetes_spawner handle short blips (~7s budget); this
    # outer budget bridges longer outages like a gateway cold start by
    # respawning only the failed roles while survivors wait idle.  BRC can
    # not start without the full cohort anyway, so leaving survivors alone
    # during the retry window does not risk correctness.
    phase_max_retries = getattr(pipeline.config, "phase_spawn_max_retries", 2)
    phase_initial_backoff = getattr(
        pipeline.config, "phase_spawn_retry_initial_backoff_seconds", 30.0
    )
    _PHASE_RETRY_BACKOFF_MULTIPLIER = 3.0
    for attempt in range(phase_max_retries):
        failed = [e for e in executions if e.status.value == "failed"]
        if not failed:
            break
        transient_failed = [e for e in failed if _is_transient_agent_error(e.error)]
        if not transient_failed:
            # All remaining failures are permanent — retrying would just
            # burn the budget for no benefit.
            break

        delay = phase_initial_backoff * (_PHASE_RETRY_BACKOFF_MULTIPLIER**attempt)
        failed_roles = [e.role for e in failed]
        logger.warning(
            "Phase-level spawn retry scheduled",
            pipeline_id=pipeline_id,
            phase=phase_str,
            attempt=attempt + 1,
            max_attempts=phase_max_retries,
            delay_seconds=delay,
            failed_roles=[r.value for r in failed_roles],
            transient_roles=[e.role.value for e in transient_failed],
        )
        time.sleep(delay)

        # Clear any half-created gateway worktree state for failed roles
        # so the retry sees a clean slate.  Survivors' worktrees use
        # different container_ids and are untouched.
        for role in failed_roles:
            agent_worktree_id = f"{pipeline_id}-{role.value}"
            try:
                spawner.gateway.delete_worktrees(
                    container_id=agent_worktree_id,
                    force=True,
                )
            except Exception as clear_err:
                logger.warning(
                    "Failed to clear partial worktree before retry",
                    pipeline_id=pipeline_id,
                    agent_worktree_id=agent_worktree_id,
                    error=str(clear_err),
                )

        retry_executions = executor.spawn_specific_roles(failed_roles, agent_prompts=agent_prompts)
        by_role = {e.role: e for e in retry_executions}
        executions = [
            by_role.get(e.role, e) if e.status.value == "failed" else e for e in executions
        ]

        still_failed = [e for e in executions if e.status.value == "failed"]
        logger.info(
            "Phase-level spawn retry outcome",
            pipeline_id=pipeline_id,
            phase=phase_str,
            attempt=attempt + 1,
            recovered_roles=[
                r.value for r in failed_roles if r not in {e.role for e in still_failed}
            ],
            still_failed_roles=[e.role.value for e in still_failed],
        )

    # Record spawned containers/agents in pipeline state.
    if store is not None:
        try:
            with get_pipeline_state_lock(pipeline_id):
                pip = store.load_pipeline(pipeline_id)
                phase_execution = pip.get_phase_execution(PipelinePhase(phase_str))
                for exec_info in executions:
                    if exec_info.container_id:
                        spawn_info = exec_info.container_info
                        if spawn_info is not None:
                            # Preserve backend-specific fields (pod_name,
                            # namespace, job_name on k8s) from the spawner
                            # while overriding the live bookkeeping fields.
                            container_info = spawn_info.model_copy(
                                update={
                                    "status": ContainerStatus.RUNNING,
                                    "started_at": datetime.now(UTC),
                                    "agent_role": exec_info.role,
                                }
                            )
                        else:
                            container_info = ContainerInfo(
                                container_id=exec_info.container_id,
                                container_name=f"{pipeline_id}-{exec_info.role.value}",
                                status=ContainerStatus.RUNNING,
                                started_at=datetime.now(UTC),
                                agent_role=exec_info.role,
                            )
                        phase_execution.containers.append(container_info)

                    agent_state = StateAgentExecution(
                        role=exec_info.role,
                        status=(
                            StateAgentStatus.RUNNING
                            if exec_info.status == StateAgentStatus.RUNNING
                            else StateAgentStatus.FAILED
                        ),
                        container_id=exec_info.container_id,
                        started_at=datetime.now(UTC),
                        slice_id=slice_id,
                    )
                    phase_execution.agents.append(agent_state)
                store.save_pipeline(pip)
        except Exception as track_err:
            logger.warning(
                "Failed to record concurrent agents in pipeline state",
                pipeline_id=pipeline_id,
                error=str(track_err),
            )

    # Check for spawn failures before waiting.  Stop successfully-spawned
    # containers so they don't continue running after the phase is aborted,
    # then write their terminal status back to pipeline state so get_status
    # agrees with list_containers (kubernetes_monitor won't reconcile a
    # non-RUNNING pipeline, so we must finalize here).
    spawn_failures = [e for e in executions if e.status.value == "failed"]
    if spawn_failures:
        survivor_container_ids: set[str] = set()
        for e in executions:
            if e.container_id and e.status.value != "failed":
                survivor_container_ids.add(e.container_id)
                try:
                    spawner.backend.stop_container(e.container_id, timeout=10)
                except Exception:
                    pass

        if store is not None:
            try:
                with get_pipeline_state_lock(pipeline_id):
                    pip = store.load_pipeline(pipeline_id)
                    phase_execution = pip.get_phase_execution(PipelinePhase(phase_str))
                    abort_error = "Aborted during spawn-failure cleanup"
                    now = datetime.now(UTC)
                    for agent_state in phase_execution.agents:
                        if (
                            agent_state.container_id in survivor_container_ids
                            and agent_state.status == StateAgentStatus.RUNNING
                        ):
                            agent_state.status = StateAgentStatus.FAILED
                            agent_state.error = abort_error
                            agent_state.completed_at = now
                    for container_info in phase_execution.containers:
                        if (
                            container_info.container_id in survivor_container_ids
                            and container_info.status == ContainerStatus.RUNNING
                        ):
                            container_info.status = ContainerStatus.FAILED
                            container_info.exited_at = now
                    store.save_pipeline(pip)
            except Exception as cleanup_err:
                logger.warning(
                    "Failed to record spawn-failure cleanup in pipeline state",
                    pipeline_id=pipeline_id,
                    error=str(cleanup_err),
                )

        raise SpawnFailureError([(e.role.value, e.error) for e in spawn_failures])

    # Consensus-driven polling loop with container-exit fallback.
    #
    # The loop periodically checks consensus via executor.check_consensus().
    # When all agents signal READY, the phase completes immediately without
    # waiting for containers to exit.  If consensus is never reached (timeout
    # or all containers exit first), fall back to exit-code-based completion.
    active_executions = [e for e in executions if e.container_id]
    docker_client = spawner.backend
    all_logs: list[str] = []
    has_failures = [False]  # Mutable container for closure access
    # Lock kept for forward-compat; the polling loop is single-threaded
    # after the #1921 refactor but _record_container_exit uses the lock
    # and is called from multiple code paths.
    _logs_lock = threading.Lock()

    poll_interval = 5  # seconds
    raw_timeout = resolve_consensus_timeout_minutes(pipeline.config, phase_str)
    consensus_timeout = max(raw_timeout, 1) * 60  # minimum 1 minute
    start_time = time.monotonic()
    objection_decision_created = False

    # Track which containers have exited and their results.
    exited_containers: dict[str, ContainerInfo] = {}

    def _record_container_exit(exec_info: StateAgentExecution, final_info: ContainerInfo) -> None:
        """Capture logs and update pipeline state for an exited container."""
        container_logs = ""
        if final_info.exit_code != 0:
            try:
                container_logs = docker_client.get_container_logs(
                    exec_info.container_id,
                    tail=200,
                )
            except Exception:
                pass

        with _logs_lock:
            # 143 (SIGTERM) is orchestrator-initiated teardown, not a
            # failure — match the K8s monitor's classifier (#2210) so
            # the two layers don't disagree about what 143 means.
            if final_info.exit_code not in (0, 143):
                has_failures[0] = True
            all_logs.append(
                f"--- {exec_info.role.value} (exit={final_info.exit_code}) ---\n{container_logs}"
            )

        if store is not None:
            try:
                with get_pipeline_state_lock(pipeline_id):
                    pip = store.load_pipeline(pipeline_id)
                    pe = pip.get_phase_execution(PipelinePhase(phase_str))

                    for ci in pe.containers:
                        if ci.container_id == exec_info.container_id:
                            ci.status = final_info.status
                            ci.exited_at = final_info.exited_at
                            ci.exit_code = final_info.exit_code
                            break

                    for agent in pe.agents:
                        if agent.container_id == exec_info.container_id:
                            agent.completed_at = datetime.now(UTC)
                            if final_info.exit_code in (0, 143):
                                agent.status = StateAgentStatus.COMPLETE
                            else:
                                agent.status = StateAgentStatus.FAILED
                                agent.error = f"Container exited with code {final_info.exit_code}"
                            break

                    # Cap each tail line at 4096 chars: containers that print
                    # large JSON blobs on one line could otherwise persist
                    # multi-MB lines into pipeline state on every chatty exit.
                    last_lines = (
                        [ln[:4096] for ln in container_logs.splitlines()[-200:]]
                        if container_logs
                        else []
                    )
                    pe.agent_exits.append(
                        AgentExitInfo(
                            role=exec_info.role,
                            exit_code=final_info.exit_code,
                            last_lines=last_lines,
                            terminated_at=datetime.now(UTC),
                            container_id=exec_info.container_id,
                        )
                    )

                    store.save_pipeline(pip)
            except Exception as track_err:
                logger.warning(
                    "Failed to update concurrent agent state",
                    container_id=exec_info.container_id,
                    error=str(track_err),
                )

    def _stop_running_containers() -> None:
        """Gracefully stop all containers that haven't exited yet."""
        for e in active_executions:
            if e.container_id not in exited_containers:
                try:
                    docker_client.stop_container(e.container_id, timeout=30)
                except Exception:
                    pass

    # Import peer_consensus tracker once at function scope so we don't
    # re-run import machinery inside _update_agents_complete under the lock.
    _get_brc_tracker = None
    try:
        from peer_consensus import get_peer_consensus_tracker as _get_brc_tracker
    except ImportError:
        from ..peer_consensus import (
            get_peer_consensus_tracker as _get_brc_tracker,  # type: ignore[no-redef]
        )

    def _latest_proposal_ts(_pid: str, _sid: str | None) -> datetime | None:
        """Return the latest CONSENSUS_PROPOSE timestamp from the BRC tracker.

        Used by the post-consensus-timeout poll loop (#2245) to rebaseline
        the per-iteration budget on producer progress.  Returns ``None`` if
        the tracker is unavailable, has no proposals, or any lookup raises —
        callers treat ``None`` as "no progress signal yet" and proceed
        without a rebaseline.
        """
        if _get_brc_tracker is None:
            return None
        try:
            _t = _get_brc_tracker(_pid, _sid)
        except Exception:
            return None
        if _t is None:
            return None
        try:
            return _t.get_latest_proposal_timestamp()
        except Exception:
            return None

    def _update_agents_complete() -> None:
        """Mark all running agents as COMPLETE in pipeline state (consensus path)."""
        if store is None:
            return
        try:
            with get_pipeline_state_lock(pipeline_id):
                pip = store.load_pipeline(pipeline_id)
                pe = pip.get_phase_execution(PipelinePhase(phase_str))
                completed_container_ids: set[str] = set()

                # Look up proposal commit SHAs from the BRC tracker so we can
                # populate agent.commit (issue #1691). The lookup is slice-
                # aware (#2137) — when ``slice_id`` is set the tracker key
                # is the nested ``{pipeline_id}/{slice_id}`` form.
                _brc = None
                if _get_brc_tracker is not None:
                    try:
                        _brc = _get_brc_tracker(pipeline_id, slice_id)
                    except TypeError:
                        # Older tracker import-shim without slice_id support.
                        try:
                            _brc = _get_brc_tracker(pipeline_id)
                        except Exception:
                            pass
                    except Exception:
                        pass

                # Filter to this slice's agents — without the filter, slice-2
                # BRC completing flips slice-3's still-running agents to
                # COMPLETE because they share ``pe.agents`` (#2422). For
                # pipeline-level (non-sliced) phases ``slice_id`` is ``None``
                # and we still match all agents whose ``slice_id`` is ``None``.
                for agent in pe.agents:
                    if getattr(agent, "slice_id", None) != slice_id:
                        continue
                    if agent.status in (StateAgentStatus.RUNNING, StateAgentStatus.FAILED):
                        agent.status = StateAgentStatus.COMPLETE
                        agent.completed_at = datetime.now(UTC)
                        if agent.container_id:
                            completed_container_ids.add(agent.container_id)
                    # Populate commit SHA from the consensus tracker's proposal
                    # records.  Only producers have SHAs; reviewers get "".
                    if _brc is not None and not agent.commit:
                        sha = _brc.get_proposal_commit_sha(agent.role.value)
                        if sha and sha != "RECONSTRUCTED_NO_SHA":
                            agent.commit = sha
                        elif sha is None or sha == "RECONSTRUCTED_NO_SHA":
                            # Diagnostic only (#1911): log when the BRC
                            # tracker returns null or the
                            # RECONSTRUCTED_NO_SHA sentinel for a role
                            # so we can see on real runs whether the
                            # three-role implement phase
                            # (coder/tester/documenter) wiring misses
                            # SHAs.  Deliberately no auto-fallback —
                            # that would mask the real bug.  Empty
                            # string is the expected reviewer default
                            # (reviewers never propose) — do NOT warn
                            # for that case or the signal drowns in
                            # noise.
                            logger.warning(
                                "BRC tracker returned no commit sha for completed agent",
                                pipeline_id=pipeline_id,
                                phase=phase_str,
                                role=agent.role.value,
                                brc_value=sha,
                            )

                # Also mark containers as exited so the container monitor
                # doesn't find stale RUNNING entries and mark pipeline FAILED.
                # See issue #1294.
                for ci in pe.containers:
                    if (
                        ci.container_id in completed_container_ids
                        and ci.status == ContainerStatus.RUNNING
                    ):
                        ci.status = ContainerStatus.EXITED
                        # Synthetic: container will be stopped next, but 0
                        # reflects successful consensus completion.
                        ci.exit_code = 0
                        ci.exited_at = datetime.now(UTC)
                store.save_pipeline(pip)
        except Exception as track_err:
            logger.warning(
                "Failed to update agents to COMPLETE after consensus",
                pipeline_id=pipeline_id,
                error=str(track_err),
            )

    _demoted_agents: set[str] = set()

    # #2243 progress-gate state: log on first defer + first un-defer only
    # so the polling loop doesn't spam at every iteration once we cross
    # ``consensus_timeout``.
    _progress_gate_deferring = False

    while True:
        elapsed = time.monotonic() - start_time

        # 1. Check consensus
        try:
            consensus = executor.check_consensus()
        except Exception as e:
            logger.warning(
                "Consensus check failed, continuing poll",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            consensus = {"is_complete": False, "has_objections": False, "blocking_agents": []}

        # 2. Consensus reached — stop containers and return
        if consensus.get("is_complete"):
            # Recover pipeline if externally marked FAILED (issue #1273).
            # The container_monitor reconciliation thread may have marked the
            # pipeline FAILED while we were polling.  Now that consensus is
            # confirmed complete, restore the pipeline to RUNNING so stored
            # state matches the successful outcome.
            #
            # NOTE: consensus staleness is acceptable here.  The `consensus`
            # dict was fetched earlier in this loop iteration and is not
            # re-evaluated under the lock.  If consensus regressed between
            # the outer check and lock acquisition (extremely unlikely), the
            # next iteration of this monitoring loop will re-evaluate and
            # self-correct.
            if store is not None:
                try:
                    _current_pip = store.load_pipeline(pipeline_id)
                    if _current_pip.status == PipelineStatus.FAILED:
                        logger.warning(
                            "Pipeline externally marked FAILED but consensus is complete — recovering",
                            pipeline_id=pipeline_id,
                        )
                        with get_pipeline_state_lock(pipeline_id):
                            _current_pip = store.load_pipeline(pipeline_id)
                            if _current_pip.status == PipelineStatus.FAILED:
                                _current_pip.status = PipelineStatus.RUNNING
                                _current_pip.error = None
                                store.save_pipeline(_current_pip)
                except Exception as recovery_err:
                    logger.warning(
                        "External FAILED recovery check failed",
                        pipeline_id=pipeline_id,
                        error=str(recovery_err),
                    )

            if _emit_event is not None:
                _emit_event(
                    EventType.CONSENSUS_REACHED,
                    pipeline_id,
                    data={"elapsed_seconds": elapsed},
                )
            logger.info(
                "Consensus reached, stopping containers",
                pipeline_id=pipeline_id,
                elapsed_seconds=round(elapsed, 1),
                has_failures=has_failures[0],
            )
            _update_agents_complete()
            _stop_running_containers()
            combined_logs = (
                "\n".join(all_logs) if all_logs else "Consensus reached; phase complete."
            )
            # Consensus is the authoritative success signal.  When all agents
            # have confirmed (is_complete=True), container-level failures
            # (e.g. OOM kills that happened *before* the surviving agents
            # reached agreement) should not override the consensus result.
            # Any pending HITL decisions from handle_agent_failure remain
            # active for human review, but the pipeline itself succeeds.
            if has_failures[0]:
                logger.warning(
                    "Container failures detected but consensus is complete — treating as success",
                    pipeline_id=pipeline_id,
                    has_failures=has_failures[0],
                )
            return 0, combined_logs

        # 3. Handle objections (create HITL decision once).
        #    The decision is fire-and-forget: resolution is processed by the
        #    orchestrator's decision queue (outside this function).  If the
        #    human selects "Override objections", the orchestrator updates
        #    agent readiness, which is picked up by check_consensus() on
        #    the next poll iteration.  "Abort phase" triggers pipeline
        #    cancellation via a separate control path.
        if consensus.get("has_objections") and not objection_decision_created:
            decision = _persist_hitl_decision(
                pipeline_id,
                pipeline,
                store,
                question="Agent(s) objecting to phase completion. How to proceed?",
                options=["Override objections", "Wait for resolution", "Abort phase"],
                phase=pipeline.current_phase,
            )
            if decision is not None:
                objection_decision_created = True
                logger.info(
                    "Objection detected, HITL decision created",
                    pipeline_id=pipeline_id,
                    blocking_agents=consensus.get("blocking_agents", []),
                )

        # 3b. RC3: Stall demotion for dual-role agents.
        # If a dual-role agent has missed heartbeats for 5+ minutes,
        # demote its reviewer edges to ADVISORY so other agents can proceed.
        try:
            from health_monitor import get_health_monitor

            _hm = get_health_monitor()
            if _hm is not None:
                try:
                    from peer_consensus import get_peer_consensus_tracker
                except ImportError:
                    from ..peer_consensus import (
                        get_peer_consensus_tracker,  # type: ignore[no-redef]
                    )

                # Slice-aware tracker lookup (#2137): per-slice trackers
                # are namespaced ``{pipeline_id}/{slice_id}`` so the
                # stall-demotion check fires against the correct scope.
                try:
                    _brc_tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
                except TypeError:
                    _brc_tracker = get_peer_consensus_tracker(pipeline_id)
                if _brc_tracker is not None:
                    heartbeat_actions = _hm.check_heartbeats()
                    for hb_action in heartbeat_actions:
                        stalled_agent = hb_action.get("agent_id", "")
                        stall_elapsed = hb_action.get("elapsed_seconds", 0)
                        if (
                            stall_elapsed >= 300
                            and stalled_agent not in _demoted_agents
                            and _brc_tracker.graph.is_dual_role(stalled_agent)
                        ):
                            try:
                                _brc_tracker.handle_stall_demotion(
                                    stalled_agent,
                                    reason=f"Missed heartbeats for {stall_elapsed}s",
                                )
                                _demoted_agents.add(stalled_agent)
                            except Exception as demote_err:
                                logger.debug(
                                    "Stall demotion skipped",
                                    agent=stalled_agent,
                                    error=str(demote_err),
                                )
        except Exception as stall_err:
            logger.debug(
                "Stall demotion check failed",
                pipeline_id=pipeline_id,
                error=str(stall_err),
            )

        # 4. Non-blocking check for exited containers
        for exec_info in active_executions:
            if exec_info.container_id in exited_containers:
                continue
            try:
                info = docker_client.get_container_info(exec_info.container_id)
            except (
                ContainerNotFoundError,
                ContainerOperationError,
                PodNotFoundError,
                JobOperationError,
            ) as e:
                logger.warning(
                    "Container lost during poll",
                    container_id=exec_info.container_id,
                    role=exec_info.role.value,
                    error=str(e),
                )
                info = ContainerInfo(
                    container_id=exec_info.container_id,
                    container_name=f"{pipeline_id}-{exec_info.role.value}",
                    status=ContainerStatus.FAILED,
                    exit_code=-1,
                    exited_at=datetime.now(UTC),
                )

            if info.status in (
                ContainerStatus.EXITED,
                ContainerStatus.FAILED,
                ContainerStatus.REMOVED,
            ):
                exited_containers[exec_info.container_id] = info
                _record_container_exit(exec_info, info)

                # Handle non-clean exit as agent failure.  0 = normal,
                # 143 = orchestrator-initiated SIGTERM (#2210) — both
                # are classified as clean here to match the K8s monitor's
                # _classify_exit, so the two layers can't race to write
                # contradictory agent.status values.
                if info.exit_code not in (0, 143):
                    try:
                        executor.handle_agent_failure(
                            role=exec_info.role.value,
                            error=f"Container exited with code {info.exit_code}",
                        )
                    except Exception as e:
                        logger.warning(
                            "handle_agent_failure error",
                            role=exec_info.role.value,
                            error=str(e),
                        )
                else:
                    # Clean exit (0 or 143): the consensus wrapper inside
                    # the container handles restarts if the agent didn't
                    # signal READY. We do NOT auto-register READY here —
                    # agents must explicitly participate in consensus.
                    logger.info(
                        "Container exited cleanly, wrapper handles consensus",
                        pipeline_id=pipeline_id,
                        role=exec_info.role.value,
                        exit_code=info.exit_code,
                    )

        # 5. All containers exited — fall back to exit-code-based result
        if len(exited_containers) >= len(active_executions):
            combined_logs = "\n".join(all_logs)
            if has_failures[0]:
                # Final consensus recheck: consensus may have completed between
                # the step-2 check and now (race window while containers were
                # shutting down).  Re-query before giving up.
                try:
                    final_consensus = executor.check_consensus()
                except Exception as e:
                    logger.warning(
                        "Final consensus recheck failed, treating as incomplete",
                        pipeline_id=pipeline_id,
                        error=str(e),
                    )
                    final_consensus = {"is_complete": False}

                if final_consensus.get("is_complete"):
                    # Guard: consensus may be "complete" by quorum but still
                    # have unresolved NACKs — mirror the step 5 no-failure
                    # NACK check and the timeout path NACK check.
                    if final_consensus.get("has_unresolved_nacks"):
                        nack_details = final_consensus.get("unresolved_nacks", [])
                        nack_summary = _format_nack_summary(nack_details)
                        logger.warning(
                            "Consensus complete on final recheck but unresolved NACKs remain (has_failures path)",
                            pipeline_id=pipeline_id,
                            nack_count=len(nack_details),
                            nack_summary=nack_summary,
                        )
                        _persist_hitl_decision(
                            pipeline_id,
                            pipeline,
                            store,
                            question=(
                                f"Consensus reached but {len(nack_details)} NACK(s) "
                                f"remain unresolved: {nack_summary}. How to proceed?"
                            ),
                            options=["Retry phase", "Accept current state", "Abort phase"],
                            phase=pipeline.current_phase,
                        )
                        combined_logs += (
                            f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                        )
                        return 1, combined_logs

                    # Consensus reached after all — recover pipeline if needed
                    if store is not None:
                        try:
                            _current_pip = store.load_pipeline(pipeline_id)
                            if _current_pip.status == PipelineStatus.FAILED:
                                logger.warning(
                                    "Pipeline externally marked FAILED but consensus is complete — recovering",
                                    pipeline_id=pipeline_id,
                                )
                                with get_pipeline_state_lock(pipeline_id):
                                    _current_pip = store.load_pipeline(pipeline_id)
                                    if _current_pip.status == PipelineStatus.FAILED:
                                        _current_pip.status = PipelineStatus.RUNNING
                                        _current_pip.error = None
                                        store.save_pipeline(_current_pip)
                        except Exception as recovery_err:
                            logger.warning(
                                "External FAILED recovery check failed",
                                pipeline_id=pipeline_id,
                                error=str(recovery_err),
                            )

                    _elapsed_final = time.monotonic() - start_time
                    if _emit_event is not None:
                        _emit_event(
                            EventType.CONSENSUS_REACHED,
                            pipeline_id,
                            data={"elapsed_seconds": _elapsed_final},
                        )
                    logger.info(
                        "Consensus reached on final recheck, stopping containers",
                        pipeline_id=pipeline_id,
                        elapsed_seconds=round(_elapsed_final, 1),
                        has_failures=has_failures[0],
                    )
                    _update_agents_complete()
                    _stop_running_containers()
                    return 0, combined_logs

                # Incomplete consensus + container failures: surface an HITL
                # decision so the operator can drive recovery (issue #2203).
                # Without this, the phase fails terminally with no signal —
                # the agent's committed work is still on the per-role branch
                # and `restart_phase` would recover, but the operator has no
                # way to know that without out-of-band investigation.
                #
                # If an objection HITL was created earlier in the polling loop
                # this is intentionally a *second* pending decision: it
                # carries different options ("Retry phase" / "Accept current
                # state" / "Abort phase" vs the objection set) and conveys a
                # different operator action.  The test
                # `test_objection_dedup_distinct_from_incomplete_consensus_hitl`
                # locks in the two-decision UX.
                failure_count = sum(1 for info in exited_containers.values() if info.exit_code != 0)
                question, log_suffix = _incomplete_consensus_decision_text(
                    final_consensus, container_failure_count=failure_count
                )
                logger.warning(
                    "Incomplete consensus with container failures — escalating to HITL",
                    pipeline_id=pipeline_id,
                    failure_count=failure_count,
                    blocking_agents=final_consensus.get("blocking_agents", []),
                    nack_count=len(final_consensus.get("unresolved_nacks", []) or []),
                )
                _persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=question,
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                )
                combined_logs += log_suffix
                return 1, combined_logs

            # Before returning success, check the BRC approval matrix for
            # unresolved NACKs.  If reviewers NACKed but producers exited
            # without iterating, we must NOT report success — escalate to
            # HITL so a human can decide how to proceed.
            if consensus.get("has_unresolved_nacks"):
                nack_details = consensus.get("unresolved_nacks", [])
                nack_summary = _format_nack_summary(nack_details)
                logger.warning(
                    "All containers exited with unresolved NACKs",
                    pipeline_id=pipeline_id,
                    nack_count=len(nack_details),
                    nack_summary=nack_summary,
                )
                _persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=(
                        f"All agents exited but {len(nack_details)} NACK(s) remain "
                        f"unresolved: {nack_summary}. How to proceed?"
                    ),
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                )
                combined_logs += f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                return 1, combined_logs

            # Final consensus completeness check: all containers exited
            # cleanly (no failures, no NACKs) but consensus may not have
            # been reached.  Mirror the has_failures branch pattern to
            # prevent advancing without confirmed BRC consensus.
            try:
                final_consensus = executor.check_consensus()
            except Exception as e:
                logger.warning(
                    "Final consensus recheck failed on clean exit, treating as incomplete",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
                final_consensus = {"is_complete": False}

            if not final_consensus.get("is_complete"):
                # Symmetric to the has_failures path: clean exits with no
                # consensus also need an HITL decision so the operator can
                # drive recovery (issue #2203).
                question, log_suffix = _incomplete_consensus_decision_text(
                    final_consensus, container_failure_count=0
                )
                logger.warning(
                    "All containers exited cleanly but consensus not reached — escalating to HITL",
                    pipeline_id=pipeline_id,
                    elapsed_seconds=round(elapsed, 1),
                    blocking_agents=final_consensus.get("blocking_agents", []),
                )
                _persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=question,
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                )
                combined_logs += log_suffix
                return 1, combined_logs

            # Consensus confirmed on clean exit — mirror the has_failures
            # success path: emit event, update agent state, stop containers.
            if _emit_event is not None:
                _emit_event(
                    EventType.CONSENSUS_REACHED,
                    pipeline_id,
                    data={"elapsed_seconds": elapsed},
                )
            logger.info(
                "Consensus reached on final recheck, stopping containers",
                pipeline_id=pipeline_id,
                elapsed_seconds=round(elapsed, 1),
                has_failures=has_failures[0],
            )
            _update_agents_complete()
            _stop_running_containers()
            return 0, combined_logs

        # 6. Consensus timeout
        if elapsed >= consensus_timeout:
            # #2243 progress gate: keep polling instead of publishing
            # the consensus-timeout alert while producer/reviewer
            # activity is still live on the BRC bus or in container
            # heartbeats. Without this gate, the historical decision-15
            # / decision-17 misfires on ``issue-1557-v2`` (now
            # ``OVERSEER_ALERT`` post-#2264) fired minutes before the
            # next commit landed.
            _gate_seconds = max(
                0,
                int(getattr(pipeline.config, "brc_consensus_progress_gate_seconds", 300)),
            )
            _gate_defer, _gate_reason = _check_brc_progress_gate(
                pipeline_id,
                slice_id,
                [e.role.value for e in active_executions],
                _gate_seconds,
            )
            if _gate_defer:
                if not _progress_gate_deferring:
                    logger.info(
                        "Consensus timeout deferred by progress gate",
                        pipeline_id=pipeline_id,
                        elapsed_seconds=round(elapsed, 1),
                        gate_seconds=_gate_seconds,
                        reason=_gate_reason,
                    )
                    _progress_gate_deferring = True
                time.sleep(poll_interval)
                continue
            if _progress_gate_deferring:
                logger.info(
                    "Consensus timeout proceeding — progress gate window elapsed",
                    pipeline_id=pipeline_id,
                    elapsed_seconds=round(elapsed, 1),
                    gate_seconds=_gate_seconds,
                )
                _progress_gate_deferring = False

            logger.warning(
                "Consensus timeout reached, falling back to container exit",
                pipeline_id=pipeline_id,
                timeout_minutes=consensus_timeout / 60,
            )
            _handle_brc_consensus_timeout(
                pipeline,
                pipeline_id,
                consensus_timeout,
                consensus.get("blocking_agents", []),
                store,
                slice_id=slice_id,
                active_role_names=[e.role.value for e in active_executions],
            )

            # Fall back: event-driven wait for remaining containers.
            #
            # Issue #1921: the previous implementation used a
            # ThreadPoolExecutor with a blocking
            # wait_for_container(timeout=3600) per container.  During
            # that hour the polling loop was blind to BRC progress —
            # a NACK → re-propose → ACK cycle completing in the final
            # minute could still be force-killed.  Now we poll
            # container status in short steps and re-check consensus
            # between steps, early-returning on completion before
            # force-killing anything.
            #
            # Issue #2245: the per-iteration budget rebaselines on
            # producer progress.  Each new CONSENSUS_PROPOSE (initial
            # or NACK→re-propose) resets ``last_progress_at`` so the
            # producer's next iteration gets a clean clock instead of
            # inheriting the prior iterations' wall-clock spend.  An
            # absolute cap (``post_consensus_max_total_seconds``)
            # bounds the total wait so an unbounded propose churn
            # can't stall the pipeline indefinitely.
            remaining = [e for e in active_executions if e.container_id not in exited_containers]
            if remaining:
                post_timeout_iteration_budget = (
                    pipeline.config.post_consensus_iteration_budget_seconds
                )
                post_timeout_max_total = pipeline.config.post_consensus_max_total_seconds
                post_timeout_poll_interval = 30  # seconds between checks
                post_timeout_start = time.monotonic()
                last_progress_at = post_timeout_start

                # Snapshot the latest proposal timestamp at entry so we
                # only count *new* proposals as progress signals.  ``None``
                # is fine: the rebaseline check at the bottom of the loop
                # short-circuits on ``last_seen_proposal_ts is None``
                # before any datetime comparison runs.
                last_seen_proposal_ts = _latest_proposal_ts(pipeline_id, slice_id)

                while remaining:
                    now_monotonic = time.monotonic()
                    total_elapsed = now_monotonic - post_timeout_start
                    iteration_elapsed = now_monotonic - last_progress_at
                    if total_elapsed >= post_timeout_max_total:
                        logger.warning(
                            "Post-consensus-timeout absolute cap reached",
                            pipeline_id=pipeline_id,
                            total_elapsed_seconds=round(total_elapsed, 1),
                            max_total_seconds=post_timeout_max_total,
                        )
                        break
                    if iteration_elapsed >= post_timeout_iteration_budget:
                        logger.warning(
                            "Post-consensus-timeout iteration budget exhausted",
                            pipeline_id=pipeline_id,
                            iteration_elapsed_seconds=round(iteration_elapsed, 1),
                            iteration_budget_seconds=post_timeout_iteration_budget,
                            total_elapsed_seconds=round(total_elapsed, 1),
                        )
                        break

                    # A. Re-check consensus; if agents converged during
                    # the wait, stop containers and return success
                    # before force-killing them.
                    try:
                        _wait_consensus = executor.check_consensus()
                    except Exception as _wait_consensus_err:
                        logger.warning(
                            "Consensus recheck during post-timeout wait failed",
                            pipeline_id=pipeline_id,
                            error=str(_wait_consensus_err),
                        )
                        _wait_consensus = None

                    if (
                        _wait_consensus
                        and _wait_consensus.get("is_complete")
                        and not _wait_consensus.get("has_unresolved_nacks")
                    ):
                        combined_logs = "\n".join(all_logs)
                        _total_elapsed = time.monotonic() - start_time
                        if _emit_event is not None:
                            _emit_event(
                                EventType.CONSENSUS_REACHED,
                                pipeline_id,
                                data={"elapsed_seconds": _total_elapsed},
                            )
                        logger.info(
                            "Consensus reached during post-timeout wait",
                            pipeline_id=pipeline_id,
                            elapsed_post_timeout_seconds=round(total_elapsed, 1),
                            total_elapsed_seconds=round(_total_elapsed, 1),
                        )
                        _update_agents_complete()
                        _stop_running_containers()
                        return 0, combined_logs

                    # A'. Rebaseline the iteration clock on producer
                    # progress (#2245).  A fresh CONSENSUS_PROPOSE
                    # timestamp means a producer just landed work
                    # (initial propose or NACK→re-propose) — the next
                    # round of reviews deserves its own iteration
                    # budget, not whatever's left of the prior round's.
                    current_proposal_ts = _latest_proposal_ts(pipeline_id, slice_id)
                    if current_proposal_ts is not None and (
                        last_seen_proposal_ts is None or current_proposal_ts > last_seen_proposal_ts
                    ):
                        logger.info(
                            "Post-consensus-timeout clock rebaselined on producer progress",
                            pipeline_id=pipeline_id,
                            iteration_elapsed_seconds=round(iteration_elapsed, 1),
                            total_elapsed_seconds=round(total_elapsed, 1),
                            proposal_timestamp=current_proposal_ts.isoformat(),
                        )
                        last_seen_proposal_ts = current_proposal_ts
                        last_progress_at = time.monotonic()

                    # B. Non-blocking container status check; record
                    # any that have exited naturally.
                    still_running = []
                    for exec_info in remaining:
                        try:
                            info = docker_client.get_container_info(exec_info.container_id)
                        except (
                            ContainerNotFoundError,
                            ContainerOperationError,
                            PodNotFoundError,
                            JobOperationError,
                        ) as _wait_status_err:
                            logger.warning(
                                "Container lost during post-timeout wait",
                                container_id=exec_info.container_id,
                                role=exec_info.role.value,
                                error=str(_wait_status_err),
                            )
                            info = ContainerInfo(
                                container_id=exec_info.container_id,
                                container_name=f"{pipeline_id}-{exec_info.role.value}",
                                status=ContainerStatus.FAILED,
                                exit_code=-1,
                                exited_at=datetime.now(UTC),
                            )

                        if info.status in (
                            ContainerStatus.EXITED,
                            ContainerStatus.FAILED,
                            ContainerStatus.REMOVED,
                        ):
                            exited_containers[exec_info.container_id] = info
                            _record_container_exit(exec_info, info)
                        else:
                            still_running.append(exec_info)

                    remaining = still_running
                    if not remaining:
                        break

                    time.sleep(post_timeout_poll_interval)

                # Budget exhausted with containers still running —
                # force-kill so they don't orphan (issue #1691).
                for exec_info in remaining:
                    try:
                        docker_client.stop_container(exec_info.container_id, timeout=30)
                    except Exception:
                        pass
                    final_info = ContainerInfo(
                        container_id=exec_info.container_id,
                        container_name=f"{pipeline_id}-{exec_info.role.value}",
                        status=ContainerStatus.FAILED,
                        exit_code=-1,
                        exited_at=datetime.now(UTC),
                    )
                    exited_containers[exec_info.container_id] = final_info
                    _record_container_exit(exec_info, final_info)

            combined_logs = "\n".join(all_logs)
            if has_failures[0]:
                # Consensus recheck: consensus may have completed right as the
                # post-timeout budget elapsed and containers were force-killed
                # (issue #1691).  The in-loop consensus check covers the common
                # case; this recheck catches the narrow race where consensus
                # completed between the last in-loop check and force-kill.
                try:
                    _timeout_consensus = executor.check_consensus()
                except Exception as e:
                    logger.warning(
                        "Consensus recheck after timeout failed, treating as incomplete",
                        pipeline_id=pipeline_id,
                        error=str(e),
                    )
                    _timeout_consensus = {"is_complete": False}

                if _timeout_consensus.get("is_complete"):
                    # Guard: consensus may be "complete" by quorum but still
                    # have unresolved NACKs — mirror the step 5 NACK check.
                    if _timeout_consensus.get("has_unresolved_nacks"):
                        nack_details = _timeout_consensus.get("unresolved_nacks", [])
                        nack_summary = _format_nack_summary(nack_details)
                        logger.warning(
                            "Consensus complete on timeout recheck but unresolved NACKs remain",
                            pipeline_id=pipeline_id,
                            nack_count=len(nack_details),
                            nack_summary=nack_summary,
                        )
                        _persist_hitl_decision(
                            pipeline_id,
                            pipeline,
                            store,
                            question=(
                                f"Consensus reached after timeout but {len(nack_details)} NACK(s) "
                                f"remain unresolved: {nack_summary}. How to proceed?"
                            ),
                            options=["Retry phase", "Accept current state", "Abort phase"],
                            phase=pipeline.current_phase,
                        )
                        combined_logs += (
                            f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                        )
                        return 1, combined_logs

                    # Consensus reached during the wait — recover pipeline
                    if store is not None:
                        try:
                            _current_pip = store.load_pipeline(pipeline_id)
                            if _current_pip.status == PipelineStatus.FAILED:
                                logger.warning(
                                    "Pipeline externally marked FAILED but consensus is complete — recovering (timeout path)",
                                    pipeline_id=pipeline_id,
                                )
                                with get_pipeline_state_lock(pipeline_id):
                                    _current_pip = store.load_pipeline(pipeline_id)
                                    if _current_pip.status == PipelineStatus.FAILED:
                                        _current_pip.status = PipelineStatus.RUNNING
                                        _current_pip.error = None
                                        store.save_pipeline(_current_pip)
                        except Exception as recovery_err:
                            logger.warning(
                                "External FAILED recovery check failed (timeout path)",
                                pipeline_id=pipeline_id,
                                error=str(recovery_err),
                            )

                    _elapsed_timeout = time.monotonic() - start_time
                    if _emit_event is not None:
                        _emit_event(
                            EventType.CONSENSUS_REACHED,
                            pipeline_id,
                            data={"elapsed_seconds": _elapsed_timeout},
                        )
                    logger.info(
                        "Consensus reached on recheck after timeout, treating as success",
                        pipeline_id=pipeline_id,
                        elapsed_seconds=round(_elapsed_timeout, 1),
                        has_failures=has_failures[0],
                    )
                    _update_agents_complete()
                    _stop_running_containers()
                    return 0, combined_logs

                # Consensus not complete on recheck.  Mirror the non-failure
                # branch's NACK summary so operators see which reviewer edges
                # are still blocking, even when containers had non-zero exits.
                if _timeout_consensus.get("has_unresolved_nacks"):
                    nack_details = _timeout_consensus.get("unresolved_nacks", [])
                    nack_summary = _format_nack_summary(nack_details)
                    logger.warning(
                        "Timeout with unresolved NACKs (has_failures path)",
                        pipeline_id=pipeline_id,
                        nack_count=len(nack_details),
                    )
                    combined_logs += (
                        f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                    )
                return 1, combined_logs

            # After timeout, check the BRC approval matrix for unresolved
            # NACKs before declaring success.  Producers that exited without
            # addressing reviewer feedback should not be treated as passing.
            try:
                _final_consensus = executor.check_consensus()
            except Exception:
                logger.warning("Failed to check consensus at timeout", exc_info=True)
                _final_consensus = {}
            if _final_consensus.get("has_unresolved_nacks"):
                nack_details = _final_consensus.get("unresolved_nacks", [])
                nack_summary = _format_nack_summary(nack_details)
                logger.warning(
                    "Timeout with unresolved NACKs — returning failure",
                    pipeline_id=pipeline_id,
                    nack_count=len(nack_details),
                )
                combined_logs += f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                return 1, combined_logs

            return 0, combined_logs

        # 7. Sleep before next poll
        time.sleep(poll_interval)


def _spawn_and_wait(
    spawner,
    pipeline_id: str,
    agent_role: AgentRole,
    issue_number: int | None,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    phase: str,
    sandbox_env: dict[str, str],
    sandbox_command: list[str],
    timeout: int = 3600,
    store=None,
    certs_volume: str | None = None,
    branch: str | None = None,
    extra_mounts: list["MountSpec"] | None = None,  # noqa: UP037
    spawn_max_retries: int | None = None,
    spawn_retry_initial_backoff_seconds: float | None = None,
) -> tuple[int, str]:
    """Spawn a container, wait for it to exit, clean up, return (exit_code, logs).

    If ``store`` is provided, the container is recorded in the phase execution
    state so that the status endpoint can report it while it runs.

    The container is launched via the shared ``build_sandbox_config()`` path,
    which handles GATEWAY_URL, proxy vars, DNS lockdown, extra_hosts, and
    .git shadow mounts automatically.

    Args:
        repo_volumes: Mapping of repo_name -> host_path for volume mounts.
            Each entry is mounted at /home/egg/repos/<name> in the container,
            with .git shadowed by /dev/null bind mounts to force gateway git operations.
        certs_volume: Docker named volume for gateway CA certs (mounted at
            /shared/certs read-only). If None, certs are not mounted.
        spawn_max_retries: Override for spawn retry attempts (None uses spawner default).
        spawn_retry_initial_backoff_seconds: Override for initial backoff (None uses spawner default).

    Returns:
        (exit_code, container_logs) — logs are captured before cleanup on failure.
    """
    from models import ContainerInfo, ContainerStatus, PipelinePhase

    retry_kwargs: dict = {}
    if spawn_max_retries is not None:
        retry_kwargs["spawn_max_retries"] = spawn_max_retries
    if spawn_retry_initial_backoff_seconds is not None:
        retry_kwargs["spawn_retry_initial_backoff_seconds"] = spawn_retry_initial_backoff_seconds

    spawned = spawner.spawn_agent_job(
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        issue_number=issue_number,
        mode=gateway_mode,
        wait_for_gateway=False,
        repos=repos,
        phase=phase,
        extra_env=sandbox_env,
        command=sandbox_command,
        repo_volumes=repo_volumes,
        branch=branch,
        extra_mounts=extra_mounts,
        jira_ticket=(sandbox_env.get("EGG_JIRA_TICKET") or None),
        **retry_kwargs,
    )

    # Record container and agent in phase execution state
    if store is not None:
        try:
            from models import AgentExecution, AgentExecutionStatus

            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(PipelinePhase(phase))

                # Track container — preserve backend-specific fields
                # (pod_name, namespace, job_name on K8s) from the spawner.
                container_info = spawned.container_info.model_copy(
                    update={
                        "status": ContainerStatus.RUNNING,
                        "started_at": datetime.now(UTC),
                        "agent_role": agent_role,
                    }
                )
                phase_execution.containers.append(container_info)

                # Track agent execution.
                #
                # ``slice_id`` is explicitly ``None`` because this helper has
                # no production callers today and is reachable only from
                # tests that mock-patch it. If a future change resurrects
                # this path for a sliced spawn, the caller MUST plumb a
                # ``slice_id`` through here — otherwise the new
                # ``(role, slice_id)`` walks added in #2422 will not see
                # the record. See PR #2435 review thread.
                agent_execution = AgentExecution(
                    role=agent_role,
                    status=AgentExecutionStatus.RUNNING,
                    container_id=spawned.container_info.container_id,
                    slice_id=None,
                    started_at=datetime.now(UTC),
                )
                phase_execution.agents.append(agent_execution)

                store.save_pipeline(pipeline)
        except Exception as track_err:
            logger.warning(
                "Failed to record container/agent in pipeline state",
                container_id=spawned.container_info.container_id[:12],
                error=str(track_err),
            )

    backend = spawner.backend
    try:
        final_info = backend.wait_for_container(
            spawned.container_info.container_id,
            timeout=timeout,
        )
    except (
        ContainerNotFoundError,
        ContainerOperationError,
        PodNotFoundError,
        JobOperationError,
    ) as e:
        logger.warning(
            "Container lost during wait, marking failed",
            container_id=spawned.container_info.container_id,
            error=str(e),
        )
        final_info = ContainerInfo(
            container_id=spawned.container_info.container_id,
            container_name=spawned.container_info.container_name,
            status=ContainerStatus.FAILED,
            exit_code=-1,
            exited_at=datetime.now(UTC),
        )

    container_logs = ""
    if final_info.exit_code != 0:
        try:
            container_logs = backend.get_container_logs(
                spawned.container_info.container_id,
                tail=200,
            )
        except Exception:
            pass

    # Update container and agent status in phase execution
    if store is not None:
        try:
            from models import AgentExecutionStatus

            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(PipelinePhase(phase))

                # Update container status
                for ci in phase_execution.containers:
                    if ci.container_id == spawned.container_info.container_id:
                        ci.status = final_info.status
                        ci.exited_at = final_info.exited_at
                        ci.exit_code = final_info.exit_code
                        break

                # Update agent status
                for agent in phase_execution.agents:
                    if agent.container_id == spawned.container_info.container_id:
                        agent.completed_at = datetime.now(UTC)
                        if final_info.exit_code == 0:
                            agent.status = AgentExecutionStatus.COMPLETE
                        else:
                            agent.status = AgentExecutionStatus.FAILED
                            agent.error = f"Container exited with code {final_info.exit_code}"
                        break

                store.save_pipeline(pipeline)
        except Exception as track_err:
            logger.warning(
                "Failed to update container/agent status in pipeline state",
                container_id=spawned.container_info.container_id[:12],
                error=str(track_err),
            )

    # Always clean up the container
    try:
        spawner.remove_agent_container(
            spawned.container_info.container_id,
            force=True,
            cleanup_session=True,
        )
    except Exception as cleanup_err:
        logger.warning(
            "Failed to clean up container",
            container_id=spawned.container_info.container_id[:12],
            error=str(cleanup_err),
        )

    return final_info.exit_code, container_logs


# Phases that pause for human approval before advancing (HITL gates)
_HITL_GATE_PHASES = {"refine", "plan"}

# Keywords that indicate human approval at HITL gates
_APPROVE_KEYWORDS = {"approved", "approve", "lgtm", "yes", ""}

# Bare option labels that indicate "request changes" without actionable feedback
_BARE_OPTION_LABELS = {"request changes", "request_changes"}


def _parse_resolution(resolution: str | None) -> tuple[bool, str | None]:
    """Parse a HITL phase_gate resolution into (is_approved, feedback).

    Handles both JSON-structured resolutions and legacy bare-string formats.
    Used by the AWAITING_HUMAN recovery path in start_pipeline.

    Returns:
        (is_approved, feedback): is_approved is True for approve/select/submit_feedback
        actions, False for request_changes/change_approach. feedback contains the
        revision feedback text (if any) for non-approved resolutions.
    """
    if not resolution:
        return True, None

    resolution = resolution.strip()

    # JSON-first: try structured payload
    try:
        payload = json.loads(resolution)
        if isinstance(payload, dict) and "action" in payload:
            action = payload["action"]
            feedback_text = payload.get("feedback", "") or None

            if action in ("approve", "select", "submit_feedback"):
                return True, None
            elif action in ("request_changes", "change_approach"):
                return False, feedback_text
            # Unknown action — fall through to legacy matching
    except json.JSONDecodeError, TypeError, AttributeError:
        pass

    # Legacy bare-string resolution
    if resolution.lower() in _APPROVE_KEYWORDS:
        return True, None
    elif resolution.lower() in _BARE_OPTION_LABELS:
        return False, None
    elif resolution:
        # Free-text feedback — treat as request_changes
        return False, resolution

    return True, None


# Minimum characters of non-heading content required for a synthesized plan
# draft to be written.  This prevents writing near-empty drafts that contain
# only section headings (e.g. when agents produced no meaningful output).
# A short but valid single-section output like "No architectural risks
# identified." is ~40 chars, so 50 provides a small buffer while still
# catching truly empty drafts.
_MIN_PLAN_DRAFT_CONTENT_LENGTH = 50


def _synthesize_plan_draft(
    repo_path: Path,
    pipeline_id: str,
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
) -> None:
    """Synthesize a plan draft from multi-agent plan outputs.

    In multi-agent plan mode, ARCHITECT and RISK_ANALYST write to
    .egg-state/agent-outputs/.  TASK_PLANNER writes the plan draft
    directly to .egg-state/drafts/{id}-plan.md.  This function combines
    the remaining agent outputs into the plan draft (if the task_planner
    has not already written one) so that _populate_contract_from_plan()
    and the HITL gate can find it.
    """
    draft_rel = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        logger.debug(
            "No draft path for plan phase, skipping synthesis",
            pipeline_id=pipeline_id,
        )
        return

    draft_path = repo_path / draft_rel
    if draft_path.exists():
        # Draft already written (e.g. by a single-agent run) — don't overwrite.
        return

    outputs_dir = repo_path / ".egg-state" / "agent-outputs"
    if not outputs_dir.is_dir():
        logger.warning(
            "No agent-outputs directory, cannot synthesize plan draft",
            pipeline_id=pipeline_id,
        )
        return

    # Derive the pipeline identifier for namespaced output filenames.
    _synth_id = _pipeline_identifier(issue_number, pipeline_id)

    sections: list[str] = []
    agent_files = [
        ("architect-output.json", "Architecture Analysis"),
        ("risk_analyst-output.json", "Risk Assessment"),
    ]

    for filename, heading in agent_files:
        # Try prefixed filename first, fall back to old global filename
        prefixed_file = outputs_dir / f"{_synth_id}-{filename}"
        if prefixed_file.exists():
            output_file = prefixed_file
        else:
            output_file = outputs_dir / filename
        if not output_file.exists():
            continue
        try:
            raw = output_file.read_text()
            data = json.loads(raw)
            # Agent outputs may contain a "content" or "output" key with
            # the main text, or may be the full JSON blob.
            content = data.get("content") or data.get("output") or json.dumps(data, indent=2)
        except json.JSONDecodeError:
            # Fall back to raw text if not valid JSON
            content = raw
        except Exception as e:
            logger.warning(
                "Failed to read agent output for plan draft",
                pipeline_id=pipeline_id,
                file=filename,
                error=str(e),
            )
            continue

        # Skip empty or whitespace-only outputs
        if not content or not content.strip():
            logger.warning(
                "Agent output is empty, skipping from plan draft",
                pipeline_id=pipeline_id,
                file=filename,
            )
            continue

        sections.append(f"## {heading}\n\n{content}")

    if not sections:
        logger.warning(
            "No agent outputs found to synthesize plan draft",
            pipeline_id=pipeline_id,
        )
        return

    draft_content = "\n\n".join(sections) + "\n"

    # Guard against a draft that has section headings but no real content.
    stripped = draft_content
    for _, heading in agent_files:
        stripped = stripped.replace(f"## {heading}", "")
    if len(stripped.strip()) < _MIN_PLAN_DRAFT_CONTENT_LENGTH:
        logger.warning(
            "Synthesized plan draft has insufficient content, not writing",
            pipeline_id=pipeline_id,
            content_length=len(stripped.strip()),
        )
        return

    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(draft_content, encoding="utf-8")
    logger.info(
        "Synthesized plan draft from agent outputs",
        pipeline_id=pipeline_id,
        path=str(draft_path),
        sections=len(sections),
    )


def _slice_gate_block_monolithic_demotion(
    worktree_repo_path: Path,
    pipeline_id: str,
    issue_number: int | None,
) -> str | None:
    """#2337 defensive recheck for the slice-loop gate.

    Called only when ``contract.slices`` is empty at implement-phase entry.
    Returns a non-None failure message when the on-disk plan draft parses
    to N>1 slices — the exact contract+plan mismatch that demoted
    issue-2261's 15-slice plan to a monolithic slice-1 PR (#2337).  When
    this fires the implement phase should be marked FAILED rather than
    silently routed through ``_run_concurrent_phase``.

    Returns ``None`` when:
    * The plan draft is missing on local — there's nothing to parse, and
      the populator's own ``plan_draft_missing`` warning already covers
      that case (with ``source="plan_complete"`` it raises so we wouldn't
      reach this gate at all).
    * The plan parses to 0 or 1 slice — single-slice/no-slice contracts
      legitimately use the monolithic path.
    * Plan parsing fails — defensive: don't block on a parser regression,
      just log and let the gate fall through to monolithic.
    """
    draft_rel = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        return None
    draft_path = worktree_repo_path / draft_rel
    if not draft_path.exists():
        return None
    try:
        from egg_contracts.plan_parser import parse_plan as _parse_plan_for_gate

        plan_text = draft_path.read_text()
        parsed = _parse_plan_for_gate(plan_text)
        if not parsed.success:
            return None
        draft_slice_count = len(parsed.to_contract_slices())
    except Exception as parse_err:  # noqa: BLE001
        logger.debug(
            "Slice-loop gate: draft re-parse failed",
            pipeline_id=pipeline_id,
            error=str(parse_err),
        )
        return None
    if draft_slice_count <= 1:
        return None
    return (
        f"plan draft parses to {draft_slice_count} slices but contract.slices "
        f"is empty — populator silently failed earlier (#2337); refusing to "
        f"demote to monolithic implement"
    )


class PlanDraftMissingOnLocalError(RuntimeError):
    """Raised by the natural plan-completion populator path when the plan
    draft is missing from the local worktree but present on origin.

    This is the silent-failure mode behind #2337: a multi-slice plan-phase
    pipeline whose populator returned without slices because
    ``_sync_worktree_with_remote`` left agents' plan-phase commits on
    origin.  Surfacing as an exception lets the natural call site mark
    the pipeline FAILED instead of silently demoting to monolithic
    implement.  The force-advance call site (#1941) keeps swallowing.
    """


def _origin_has_plan_draft(repo_path: Path, branch: str, draft_rel: str) -> bool:
    """Return True if ``origin/{branch}:{draft_rel}`` resolves locally.

    Uses ``git cat-file -e`` against the local refs to origin (the
    immediately preceding ``_sync_worktree_with_remote`` call has already
    fetched), so this is a cheap on-disk check, not a network round-trip.
    A False return means either origin really doesn't have the draft, or
    the cat-file query itself failed — caller should treat both as
    "couldn't confirm origin has it" and fall through to the warn-and-
    continue path.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                f"safe.directory={repo_path}",
                "-C",
                str(repo_path),
                "cat-file",
                "-e",
                f"origin/{branch}:{draft_rel}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _populate_contract_from_plan_safe(
    repo_path: Path,
    pipeline_id: str,
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
    *,
    source: Literal["plan_complete", "advance_phase_force"] = "advance_phase_force",
    branch: str | None = None,
    current_phase: PipelinePhase | None = None,
) -> None:
    """Run :func:`_populate_contract_from_plan` without propagating failures.

    Shared call path for the two code sites that run the populate step when a
    pipeline leaves the ``plan`` phase: ``_run_pipeline``'s post-complete
    block (``source="plan_complete"``) and ``advance_phase`` (used by the
    MCP ``advance_phase`` tool, especially with ``force=true`` —
    ``source="advance_phase_force"``).  Blocking the phase transition on
    a populate failure would defeat the purpose of the advance hammer — see
    #1941 — so the force-advance call site keeps the swallow-everything
    behaviour.

    The natural plan-completion call site (``source="plan_complete"``)
    additionally raises :class:`PlanDraftMissingOnLocalError` when the
    draft is missing from local but present on origin — the exact
    silent-failure mode behind #2337.  Caller is expected to mark the
    pipeline FAILED so the operator can intervene rather than implement
    silently demoting to monolithic.
    """
    if source == "plan_complete" and branch is not None:
        draft_rel = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
        if draft_rel is not None:
            local_path = repo_path / draft_rel
            if not local_path.exists() and _origin_has_plan_draft(repo_path, branch, draft_rel):
                logger.error(
                    "OVERSEER_ALERT plan_draft_missing_on_local_but_present_on_origin",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    draft_rel=draft_rel,
                    note=(
                        "_sync_worktree_with_remote returned without bringing "
                        "agents' plan-phase commits into the local worktree; "
                        "blocking phase advance to avoid silent demotion to "
                        "monolithic implement (#2337)"
                    ),
                )
                raise PlanDraftMissingOnLocalError(
                    f"plan draft {draft_rel} missing on local but present on "
                    f"origin/{branch} — refusing to advance plan phase"
                )

    try:
        _populate_contract_from_plan(
            repo_path,
            pipeline_id,
            pipeline_mode,
            issue_number,
            current_phase=current_phase,
        )
    except ForestValidationError as forest_err:
        # Forest-validation rejection is the expected #2137 NACK
        # path — log structurally so the discriminator shows up in
        # operator audit, but don't propagate to the wrapper's
        # caller (the populator already stashed the structured
        # errors on contract.plan_review_feedback so the plan
        # reviewer prompt can NACK the planner).
        logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="forest_violation",
            source="safe_wrapper",
            errors=forest_err.errors,
        )
    except Exception as pop_err:
        logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="unexpected_exception",
            source="safe_wrapper",
            error=str(pop_err),
            exc_info=True,
        )


def _populate_contract_from_plan(
    repo_path: Path,
    pipeline_id: str,
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
    *,
    current_phase: PipelinePhase | None = None,
) -> None:
    """Read the plan draft and populate the contract with tasks.

    Extracts task structure from markdown headers in the plan draft
    and writes tasks + acceptance criteria to the contract.

    When ``current_phase`` is provided, the contract's
    ``current_phase`` is advanced to that value **only if it would move
    the phase forward** (REFINE → PLAN → IMPLEMENT → PR).  Backward
    transitions are silently ignored so a respawn of the safety-net
    populator (e.g. when a ``start_phase=implement`` pipeline progresses
    to PR and re-enters ``_run_pipeline``) cannot demote the contract.
    The advance also appends a ``create_transition_entry`` audit log
    entry so operators inspecting the audit trail see the transition.

    This parameter is needed because the natural plan-completion path
    advances ``pipeline.current_phase`` (orchestrator-side) but leaves
    ``contract.current_phase`` for the reviewer agent / gateway phase
    API to advance via ``apply_mutation``.  When ``start_phase=implement``
    no plan reviewer runs, so the populator nudges the contract itself
    (#2427 sub-bug).
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
    except ImportError:
        logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="egg_contracts_unavailable",
        )
        return

    # Resolve draft path
    draft_rel = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="no_draft_path",
        )
        return

    plan_path = repo_path / draft_rel
    if not plan_path.exists():
        logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="plan_draft_missing",
            path=str(plan_path),
        )
        return

    try:
        contract = load_contract(pipeline_id, repo_path)
    except Exception as load_err:
        logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="contract_load_failed",
            error=str(load_err),
        )
        return

    try:
        from egg_contracts.plan_parser import parse_plan

        plan_text = plan_path.read_text()
        result = parse_plan(plan_text)

        if not result.success:
            logger.warning(
                "contract_phases_ingest_failed",
                pipeline_id=pipeline_id,
                reason="parse_failed",
                error=result.error,
            )
            return

        for warning in result.warnings:
            logger.warning(
                "Plan parse warning",
                pipeline_id=pipeline_id,
                warning_message=warning.message,
                warning_context=warning.context,
            )

        contract_slices = result.to_contract_slices()
        changed = False

        if contract_slices:
            # Forest validation (#2137 TASK-2-2): the slice DAG must be
            # a forest (every slice has ≤1 DAG parent). Multi-parent
            # slices break the stacked-PR invariant and are rejected
            # at ingestion so the plan reviewer NACKs the planner.
            #
            # ``parse_plan`` was already imported unconditionally above,
            # so we don't guard ``validate_forest`` import — if the
            # parser module is unavailable the populator has already
            # failed; silently defaulting ``forest_errors = []`` would
            # let a broken-import multi-parent contract slip past the
            # gate (reviewer_code_holistic v2 finding #5).
            from egg_contracts.plan_parser import validate_forest

            forest_errors = validate_forest(contract_slices)

            if forest_errors:
                # Stash the structured errors onto the contract's
                # ``plan_review_feedback`` so the plan reviewer's
                # prompt picks them up and NACKs the planner with the
                # error verbatim. The slices are NOT written to the
                # contract — leaving ``contract.slices`` empty makes
                # downstream phases visibly broken so the violation
                # cannot silently leak through.
                logger.warning(
                    "contract_phases_ingest_failed",
                    pipeline_id=pipeline_id,
                    reason="forest_violation",
                    errors=forest_errors,
                )
                feedback_lines = [
                    "Plan ingestion REJECTED: the slice DAG is not a forest.",
                    "",
                    "Each slice must have at most one DAG parent. The "
                    "implement phase ships every slice as a stacked PR with "
                    "exactly one base branch — multi-parent slices break "
                    "this invariant. Re-emit the plan with "
                    "``serialized_chain_order`` populated on the downstream "
                    "slice (see issue #2137 plan TASK-2-3 for the rule).",
                    "",
                    "Structured errors:",
                ]
                feedback_lines.extend(f"- {e}" for e in forest_errors)
                contract.plan_review_feedback = "\n".join(feedback_lines)
                save_contract(contract, repo_path)
                # Raise a structured ForestValidationError so any
                # caller running this in an HTTP context (e.g. a
                # plan-ingestion API endpoint) can surface a 422 with
                # the inlined errors. Internal callers
                # (``_populate_contract_from_plan_safe`` and the
                # pipeline run-loop) catch and log instead — the
                # ``plan_review_feedback`` stash above is the durable
                # signal the reviewer prompt picks up either way.
                raise ForestValidationError("slice DAG is not a forest", errors=forest_errors)
            contract.slices = contract_slices
            changed = True

        # Populate PR metadata from plan if available
        if result.pr_title:
            from egg_contracts.models import PRMetadata

            contract.pr = PRMetadata(
                title=result.pr_title,
                description=result.pr_description or "",
                test_plan=result.pr_test_plan or "",
                manual_steps=result.pr_manual_steps or "",
            )
            changed = True

        if current_phase is not None and contract.current_phase != current_phase:
            # Forward-only: never demote.  Without this guard a respawn
            # of _run_pipeline (e.g. when a start_phase=implement pipeline
            # progresses to the PR phase and re-enters the safety-net
            # call site) would silently roll contract.current_phase back
            # from PR/IMPLEMENT to whatever the call site hardcoded.
            _phase_order = (
                PipelinePhase.REFINE,
                PipelinePhase.PLAN,
                PipelinePhase.IMPLEMENT,
                PipelinePhase.PR,
            )
            if (
                contract.current_phase in _phase_order
                and current_phase in _phase_order
                and _phase_order.index(current_phase) > _phase_order.index(contract.current_phase)
            ):
                from egg_contracts.audit import create_transition_entry
                from egg_contracts.models import AuditRole

                old_phase = contract.current_phase
                contract.audit_log.append(
                    create_transition_entry(
                        actor="orchestrator",
                        role=AuditRole.SYSTEM,
                        from_phase=old_phase.value,
                        to_phase=current_phase.value,
                        reason=(
                            "populator advanced contract.current_phase "
                            "(no apply_mutation caller for this pipeline; #2427)"
                        ),
                    )
                )
                contract.current_phase = current_phase
                changed = True

        if changed:
            save_contract(contract, repo_path)
            task_count = sum(len(s.tasks) for s in contract.slices)
            logger.info(
                "contract_phases_populated",
                pipeline_id=pipeline_id,
                phase_count=len(contract.slices),
                task_count=task_count,
                has_pr_metadata=contract.pr is not None,
            )
        else:
            # Parse succeeded but yielded neither phases nor PR metadata —
            # this is the #1931 failure mode (empty contract with no error).
            # Emit a discriminator so the gap is visible in audit logs.
            logger.warning(
                "contract_phases_ingest_failed",
                pipeline_id=pipeline_id,
                reason="empty_result",
                warning_count=len(result.warnings),
            )

    except ForestValidationError:
        # Re-raise so callers with HTTP context (or the safe wrapper)
        # can surface the structured errors. The populator already
        # stashed feedback on contract.plan_review_feedback before
        # raising.
        raise
    except Exception as e:
        logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="unexpected_exception",
            source="parse_save",
            error=str(e),
            exc_info=True,
        )


def _sync_pipeline_decisions_to_contract(
    repo_path: Path,
    worktree_repo_path: Path,
    pipeline_id: str,
) -> None:
    """Sync resolved non-phase-gate pipeline decisions to the contract.

    Converts HITLDecision objects from pipeline state into contract Decision
    objects so that implement-phase agents can see what was decided during
    refine/plan phases.

    Only syncs decisions with decision_type != "phase_gate" (substantive
    choices, not process-control gates).  Skips decisions already present
    in the contract (matched by question text) to avoid duplicates on
    re-runs after HITL revision cycles.

    Args:
        repo_path: Orchestrator's main repo path — root for the state
            store that owns pipeline records.
        worktree_repo_path: Pipeline's per-run worktree path — root for
            the contract under ``<worktree>/.egg-state/contracts/``.
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Decision, DecisionOption, DecisionType
    except ImportError:
        logger.warning("egg_contracts not available, skipping decision sync")
        return

    # Load pipeline from the orchestrator's state store, NOT the per-run
    # worktree.  Pipeline records live under ``repo_path``'s persistent
    # state-store worktree; the per-run worktree has none.  Conflating
    # the two silently no-op'd this helper for every issue-mode pipeline
    # since #950 (#2345).
    store = get_state_store(repo_path)
    try:
        pipeline = store.load_pipeline(pipeline_id)
    except Exception as exc:
        logger.warning(
            "decision_sync_pipeline_load_failed",
            pipeline_id=pipeline_id,
            state_store_repo_path=str(repo_path),
            error=str(exc),
        )
        return

    # Filter to resolved, non-phase-gate decisions
    substantive_decisions = [
        d
        for d in pipeline.decisions
        if d.decision_type != "phase_gate" and d.status == DecisionStatus.RESOLVED
    ]

    if not substantive_decisions:
        logger.debug("No substantive decisions to sync", pipeline_id=pipeline_id)
        return

    try:
        contract = load_contract(pipeline_id, worktree_repo_path)
    except Exception:
        logger.warning(
            "Contract not found, skipping decision sync",
            pipeline_id=pipeline_id,
        )
        return

    # Build set of existing contract decision questions for deduplication
    existing_questions = {d.question for d in contract.decisions}

    # Determine next decision ID (continue numbering after existing ones)
    max_existing_id = 0
    for d in contract.decisions:
        # Extract numeric suffix from "decision-N"
        try:
            num = int(d.id.split("-")[1])
            max_existing_id = max(max_existing_id, num)
        except IndexError, ValueError:
            pass

    synced_count = 0
    for pipeline_decision in substantive_decisions:
        if pipeline_decision.question in existing_questions:
            continue

        max_existing_id += 1
        decision_id = f"decision-{max_existing_id}"

        # Convert pipeline options (list[str]) to contract DecisionOption objects
        contract_options = [
            DecisionOption(id=f"opt-{i + 1}", label=opt)
            for i, opt in enumerate(pipeline_decision.options)
        ]

        contract_decision = Decision(
            id=decision_id,
            question=pipeline_decision.question,
            type=DecisionType.HITL,
            options=contract_options,
            resolved=True,
            resolution=pipeline_decision.resolution,
            resolved_by="human",
            resolved_at=pipeline_decision.resolved_at,
        )
        contract.decisions.append(contract_decision)
        existing_questions.add(pipeline_decision.question)
        synced_count += 1

    if synced_count > 0:
        save_contract(contract, worktree_repo_path)
        logger.info(
            "Synced pipeline decisions to contract",
            pipeline_id=pipeline_id,
            synced_count=synced_count,
            total_contract_decisions=len(contract.decisions),
        )


def _queue_and_await_contract_decisions(
    dq: Any,
    worktree_repo_path: Path,
    pipeline_id: str,
    pipeline_identifier: int | str,
    phase: PipelinePhase,
) -> None:
    """Promote unresolved contract decisions/feedback into the orchestrator queue.

    Agents register architectural questions via ``egg-contract add-decision``
    and ``add-feedback``.  Those writes only touch ``.egg-state/contracts/
    {identifier}.json`` — the orchestrator's decision queue never sees them,
    so approving the phase_gate silently drops the questions and the next
    phase's agents have to guess (issue #1889).

    This helper bridges contract-scoped questions for the current phase into
    the orchestrator queue after phase_gate approval, so HTTP/MCP callers
    (e.g. the ``/sdlc`` skill's Phase 4 handler) surface them as individual
    ``choice`` / ``feedback`` decisions.  Resolutions are written back to
    the contract so implement-phase agents see the human's answers.

    All pending decisions (plus the feedback entry, if any) are queued up
    front before any ``wait_for_decision`` call, so ``get_status`` surfaces
    them as a single batch.  Callers can then prompt for up to 4 at a time
    and submit answers in parallel, collapsing what was previously N prompts
    and N polling cycles into ~⌈N/4⌉ prompts and one cycle (issue #1956).
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
    except ImportError:
        logger.warning(
            "egg_contracts not available, skipping contract decision bridge",
            pipeline_id=pipeline_id,
        )
        return

    try:
        contract = load_contract(pipeline_identifier, worktree_repo_path)
    except Exception as e:
        logger.debug(
            "Contract not loadable, skipping contract decision bridge",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return

    phase_value = phase.value
    pending_decisions = [
        d
        for d in contract.decisions
        if not d.resolved
        and getattr(d.type, "value", d.type) == "hitl"
        and (d.phase is None or getattr(d.phase, "value", d.phase) == phase_value)
    ]
    fb = contract.feedback
    pending_feedback = None
    if fb is not None and not fb.submitted:
        fb_phase_val = getattr(fb.phase, "value", fb.phase) if fb.phase is not None else None
        if fb_phase_val is None or fb_phase_val == phase_value:
            pending_feedback = fb

    if not pending_decisions and pending_feedback is None:
        return

    logger.info(
        "Bridging contract decisions/feedback into orchestrator queue",
        pipeline_id=pipeline_id,
        phase=phase_value,
        decision_count=len(pending_decisions),
        has_feedback=pending_feedback is not None,
    )

    def _save_contract_update(mutator: Callable[[Any], bool]) -> None:
        try:
            latest = load_contract(pipeline_identifier, worktree_repo_path)
        except Exception as e:
            logger.warning(
                "Could not reload contract to persist bridged resolution",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            return
        if not mutator(latest):
            return
        try:
            save_contract(latest, worktree_repo_path)
        except Exception as e:
            logger.warning(
                "Failed to save contract after bridged resolution",
                pipeline_id=pipeline_id,
                error=str(e),
            )

    # Pass 1: queue every pending decision + feedback up front.
    queued_decisions: list[tuple[str, Any]] = []
    for contract_decision in pending_decisions:
        options_labels = [opt.label for opt in contract_decision.options]
        queued = dq.queue_decision(
            question=contract_decision.question,
            context=(
                f"Open contract question {contract_decision.id}, "
                f"registered by an agent during the {phase_value} phase."
            ),
            options=options_labels,
            decision_type="choice",
            phase=phase,
        )
        queued_decisions.append((contract_decision.id, queued))

    queued_feedback: HITLDecision | None = None
    if pending_feedback is not None:
        questions_payload = [
            {"id": q.id, "question": q.question, "answer": ""} for q in pending_feedback.questions
        ]
        queued_feedback = dq.queue_decision(
            question=f"Open feedback request {pending_feedback.id}",
            context=(
                f"Open contract feedback {pending_feedback.id}, "
                f"registered by an agent during the {phase_value} phase."
            ),
            options=[],
            decision_type="feedback",
            questions=questions_payload,
            phase=phase,
        )

    # Pass 2: wait for each to resolve and persist back to the contract.
    for contract_id, queued in queued_decisions:
        resolved = dq.wait_for_decision(queued.id)
        if resolved.status != DecisionStatus.RESOLVED:
            continue
        resolution_str = (resolved.resolution or "").strip()

        def _apply(latest: Any, _cd_id: str = contract_id, _res: str = resolution_str) -> bool:
            for d in latest.decisions:
                if d.id == _cd_id:
                    d.resolved = True
                    d.resolution = _res
                    d.resolved_by = "human"
                    d.resolved_at = datetime.now(UTC)
                    return True
            return False

        _save_contract_update(_apply)

    if queued_feedback is not None and pending_feedback is not None:
        resolved = dq.wait_for_decision(queued_feedback.id)
        if resolved.status == DecisionStatus.RESOLVED:
            answers: dict[str, str] = {}
            try:
                payload = json.loads(resolved.resolution or "")
                if isinstance(payload, dict):
                    raw_answers = payload.get("answers")
                    if isinstance(raw_answers, dict):
                        answers = {str(k): str(v) for k, v in raw_answers.items()}
            except json.JSONDecodeError, TypeError:
                pass

            fb_id = pending_feedback.id

            def _apply_fb(
                latest: Any, _fb_id: str = fb_id, _answers: dict[str, str] = answers
            ) -> bool:
                if latest.feedback is None or latest.feedback.id != _fb_id:
                    return False
                for q in latest.feedback.questions:
                    if q.id in _answers:
                        q.answer = _answers[q.id]
                # Always mark submitted after resolution — even if
                # individual answers didn't parse, the human responded
                # and shouldn't be asked again.
                latest.feedback.submitted = True
                latest.feedback.submitted_by = "human"
                latest.feedback.submitted_at = datetime.now(UTC)
                return True

            _save_contract_update(_apply_fb)


def _persist_phase_gate_resolution(
    repo_path: Path,
    pipeline_id: str,
    decision: HITLDecision,
    phase: str,
    issue_number: int | None = None,
) -> None:
    """Persist a phase-gate resolution to the contract and draft.

    After a human approves a phase gate, the resolution context needs to be
    visible to agents in the next phase.  This function:

    1. Adds the resolution as a HITL decision in the contract so next-phase
       agents see it when they load the contract.
    2. Appends a ``## HITL Resolution`` section to the phase draft file so
       agents reading the draft also see the human's decisions.

    See: #1295
    """
    # Extract structured context from JSON resolution, or use raw string
    resolution_context: str = ""
    raw = (decision.resolution or "").strip()
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                resolution_context = payload.get("context", "") or payload.get("feedback", "")
                if not resolution_context:
                    logger.debug(
                        "Phase gate approved without context, nothing to persist",
                        pipeline_id=pipeline_id,
                        phase=phase,
                    )
                    return
            else:
                resolution_context = raw
        except json.JSONDecodeError, TypeError:
            resolution_context = raw

    if not resolution_context:
        logger.debug(
            "Phase gate resolution has no context to persist",
            pipeline_id=pipeline_id,
            phase=phase,
        )
        return

    # --- 1. Sync to contract ---
    try:
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Decision, DecisionOption, DecisionType

        contract = load_contract(pipeline_id, repo_path)

        existing_questions = {d.question for d in contract.decisions}
        question_text = f"[Phase gate: {phase}] {decision.question}"

        if question_text not in existing_questions:
            # Determine next decision ID
            max_existing_id = 0
            for d in contract.decisions:
                try:
                    num = int(d.id.split("-")[1])
                    max_existing_id = max(max_existing_id, num)
                except IndexError, ValueError:
                    pass

            contract_options = [
                DecisionOption(id=f"opt-{i + 1}", label=opt)
                for i, opt in enumerate(decision.options)
            ]

            contract_decision = Decision(
                id=f"decision-{max_existing_id + 1}",
                question=question_text,
                type=DecisionType.HITL,
                options=contract_options,
                resolved=True,
                resolution=resolution_context,
                resolved_by="human",
                resolved_at=decision.resolved_at,
            )
            contract.decisions.append(contract_decision)
            save_contract(contract, repo_path)
            logger.info(
                "Persisted phase gate resolution to contract",
                pipeline_id=pipeline_id,
                phase=phase,
            )
    except ImportError:
        logger.warning("egg_contracts not available, skipping phase gate contract sync")
    except Exception:
        logger.warning(
            "Failed to persist phase gate resolution to contract (continuing)",
            pipeline_id=pipeline_id,
            phase=phase,
            exc_info=True,
        )

    # --- 2. Append to draft ---
    try:
        draft_rel = _get_draft_path(phase, issue_number, pipeline_id)
        if draft_rel:
            draft_path = repo_path / draft_rel
            if draft_path.exists():
                existing = draft_path.read_text(encoding="utf-8")
                if "## HITL Resolution" not in existing:
                    section = (
                        f"\n\n## HITL Resolution\n\n"
                        f"The following was approved by a human reviewer at the "
                        f"{phase} phase gate:\n\n{resolution_context}\n"
                    )
                    draft_path.write_text(existing + section, encoding="utf-8")
                    logger.info(
                        "Appended HITL resolution to draft",
                        pipeline_id=pipeline_id,
                        phase=phase,
                        draft=draft_rel,
                    )
    except Exception:
        logger.warning(
            "Failed to append phase gate resolution to draft (continuing)",
            pipeline_id=pipeline_id,
            phase=phase,
            exc_info=True,
        )


def _spawn_pipeline_run_thread(
    pipeline_id: str,
    repo_path: Path,
    run_epoch: datetime,
) -> threading.Thread:
    """Spawn a fresh ``_run_pipeline`` driver thread.

    Callers (all use the ``pipeline-{id}-{epoch}`` naming scheme):

    - ``advance_phase`` (manual phase advance via REST)
    - ``restart_phase`` (manual phase restart via REST)
    - the auto-advance block in ``_run_pipeline`` (#2165)

    The other ``_run_pipeline`` thread spawn sites — ``start_pipeline``'s
    initial-spawn and AWAITING_HUMAN-recovery paths, plus the spurious-PNFE
    respawn inside ``_run_pipeline`` — use different naming or take extra
    kwargs (e.g. ``_respawn_attempt``) and are deliberately left inline.

    Without a fresh thread per phase, a mid-execution exception in the new
    phase's first iteration takes down the whole pipeline (#2165).
    """
    thread = threading.Thread(
        target=_run_pipeline,
        args=(pipeline_id, repo_path),
        daemon=True,
        name=f"pipeline-{pipeline_id}-{int(run_epoch.timestamp())}",
    )
    thread.start()
    return thread


# Tunables for the spurious-PipelineNotFoundError recovery path in
# ``_run_pipeline``.  The verify retry covers the empty-file race window
# during a ``git commit`` truncate-and-rewrite on the state worktree
# (typical: <100ms); 3 × 200ms gives ~600ms of total slack.  The respawn
# cap bounds how aggressively a persistent transient can leak threads,
# overseer containers, and state-branch commits before we fail the
# pipeline outright.  See #2155.
_PNFE_VERIFY_ATTEMPTS = 3
_PNFE_VERIFY_INTERVAL = 0.2  # seconds between verify retries
_PNFE_RESPAWN_MAX_ATTEMPTS = 5  # cap on respawn cascade
_PNFE_RESPAWN_BACKOFF_CAP = 30  # seconds, exponential backoff ceiling


def _run_pipeline(
    pipeline_id: str,
    repo_path: Path,
    _respawn_attempt: int = 0,
) -> None:
    """Run a pipeline by spawning containers for each phase.

    This runs in a background thread. For each phase it:
    1. Spawns agent containers via concurrent BRC execution
       (_run_concurrent_phase) for all phases.
    2. For reviewed phases (refine, implement, plan): reviewers participate
       in the BRC consensus protocol alongside workers, then the phase
       loops back with feedback if revision is needed.
    3. Advances to the next phase once approved.

    Args:
        pipeline_id: Pipeline ID
        repo_path: Path to repository
        _respawn_attempt: Internal — counts how many times this thread
            has been respawned by the spurious-PNFE recovery path.
            Bounded by ``_PNFE_RESPAWN_MAX_ATTEMPTS`` to prevent a
            persistent transient from cascading into an unbounded
            thread/overseer/commit storm.
    """
    from routes.phases import PHASE_TRANSITIONS

    # Track which run of the pipeline this thread owns.  If the pipeline
    # is deleted and recreated with the same ID while we're still running,
    # the new run creates its own worktrees under the same path.  Without
    # this guard, our finally block would delete the *new* run's worktrees.
    run_epoch: datetime | None = None
    overseer_container_id: str | None = None
    phase_overseer_active: bool = False
    overseer_lock = threading.Lock()
    health_monitor_instance = None
    health_monitor_timer: threading.Event | None = None
    poll_thread: threading.Thread | None = None

    try:
        store = get_state_store(repo_path)
        spawner = _get_spawner()
        pipeline = store.load_pipeline(pipeline_id)
        run_epoch = pipeline.run_epoch or pipeline.created_at
        pipeline_mode = "issue" if pipeline.issue_number is not None else "prompt"
        transitions = PHASE_TRANSITIONS

        # Map pipeline to gateway session mode.
        gateway_mode, detected_visibility = _compute_gateway_mode(pipeline)
        if not pipeline.network_mode and pipeline.repo:
            if detected_visibility is not None:
                logger.info(
                    "Auto-detected network mode from repo visibility",
                    repo=pipeline.repo,
                    visibility=detected_visibility,
                    gateway_mode=gateway_mode,
                )
            else:
                logger.warning(
                    "Could not detect repo visibility, defaulting to public mode",
                    repo=pipeline.repo,
                )

        # Parse host repo map for volume mounts.  When the orchestrator
        # runs inside Docker, EGG_REPO_PATH is the *container* path but
        # volume mounts need *host* paths (since the Docker socket
        # operates on the host daemon).  EGG_HOST_REPO_MAP provides a
        # JSON mapping of repo_name -> host_path, auto-generated from
        # repositories.yaml by the egg launcher.
        host_repo_map_raw = os.environ.get("EGG_HOST_REPO_MAP", "{}")
        try:
            host_repo_map: dict[str, str] = json.loads(host_repo_map_raw)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse EGG_HOST_REPO_MAP — no repos will be mounted in sandbox containers",
                raw_value=host_repo_map_raw,
            )
            raise ValueError(
                f"EGG_HOST_REPO_MAP contains invalid JSON: {host_repo_map_raw!r}"
            ) from exc

        # Create a pipeline-level worktree via the gateway.  This worktree
        # is used by the orchestrator for reading/writing contracts, drafts,
        # and state files.  Individual agents get their own per-agent
        # worktrees at spawn time (created in container_spawner.py) so
        # concurrent agents cannot stomp on each other's uncommitted work.
        # See #1481 for the per-agent worktree isolation design.
        #
        # We use the pipeline_id as the worktree container_id for the
        # orchestrator-side worktree.  Agent worktrees use
        # "{pipeline_id}-{role}" as their container_id.
        worktree_id = pipeline_id
        repo_volumes: dict[str, str] = {}
        worktree_repo_path = repo_path  # default; overridden when worktrees exist
        host_uid = int(os.environ.get("HOST_UID", 1000))
        host_gid = int(os.environ.get("HOST_GID", 1000))
        pipeline_repos = [pipeline.repo] if pipeline.repo else []

        if host_repo_map:
            try:
                # Request repos in owner/repo format if available, else bare names
                wt_repos = pipeline_repos if pipeline_repos else list(host_repo_map.keys())
                # When the pipeline specifies a base_branch, pass it through
                # so the worktree is branched from that ref instead of the
                # repo's default branch.  Otherwise let the gateway resolve
                # the remote default branch per-repo (see #860).
                # Retry worktree creation on transient gateway errors
                # (e.g., 500s from concurrent pipeline starts contending
                # on per-repo locks).  See #1386.
                wt_max_attempts = 3
                wt_backoff = 2.0
                wt_result = None
                for wt_attempt in range(1, wt_max_attempts + 1):
                    try:
                        wt_result = spawner.gateway.create_worktrees(
                            container_id=worktree_id,
                            repos=wt_repos,
                            uid=host_uid,
                            gid=host_gid,
                            base_branch=pipeline.base_branch,
                        )
                        break  # Success — exit retry loop
                    except GatewayError as gw_err:
                        is_transient = gw_err.status_code is None or gw_err.status_code >= 500
                        if not is_transient or wt_attempt == wt_max_attempts:
                            # Surface gw_err.details so per-repo failures
                            # captured by the gateway aren't dropped.  See
                            # #2186.
                            logger.error(
                                "Worktree creation failed permanently",
                                pipeline_id=pipeline_id,
                                attempts=wt_attempt,
                                status_code=gw_err.status_code,
                                error_message=gw_err.message,
                                details=gw_err.details,
                            )
                            detail_suffix = (
                                f" (details: {gw_err.details})" if gw_err.details else ""
                            )
                            raise RuntimeError(
                                f"Failed to create worktrees for pipeline {pipeline_id} "
                                f"after {wt_max_attempts} attempts: "
                                f"{gw_err.message}{detail_suffix}"
                            ) from gw_err
                        logger.warning(
                            "Worktree creation failed, retrying",
                            pipeline_id=pipeline_id,
                            attempt=wt_attempt,
                            max_attempts=wt_max_attempts,
                            error=str(gw_err),
                            details=gw_err.details,
                        )
                        time.sleep(wt_backoff)
                        wt_backoff *= 2

                if wt_result and wt_result.success and wt_result.worktrees:
                    # Gateway returns worktrees keyed by repo name only (e.g., "egg"),
                    # stripping the owner prefix from "owner/repo" format. This matches
                    # the container mount target at /home/egg/repos/<name>.
                    repo_volumes = wt_result.worktrees

                    # Derive the orchestrator-accessible worktree path.
                    # Reviewer containers write verdict/draft/check files into
                    # the worktree, so the orchestrator must read from there.
                    # Match against pipeline.repo explicitly to avoid picking
                    # the wrong repo in multi-repo pipelines.
                    repo_short = pipeline.repo.split("/")[-1] if pipeline.repo else None
                    matched = False
                    if repo_short and repo_short in wt_result.worktrees:
                        candidate = WORKTREE_BASE_DIR / worktree_id / repo_short
                        if candidate.exists():
                            worktree_repo_path = candidate
                            matched = True
                    if not matched:
                        # Fallback: take the first existing worktree path
                        for name in wt_result.worktrees:
                            candidate = WORKTREE_BASE_DIR / worktree_id / name
                            if candidate.exists():
                                worktree_repo_path = candidate
                                break

                    logger.info(
                        "Worktrees created for pipeline",
                        pipeline_id=pipeline_id,
                        worktrees=list(repo_volumes.keys()),
                    )
                else:
                    raise RuntimeError(
                        f"Worktree creation returned no worktrees for pipeline {pipeline_id}: "
                        f"errors={wt_result.errors}"
                    )

                if wt_result.errors:
                    for err in wt_result.errors:
                        logger.warning("Worktree error", pipeline_id=pipeline_id, error=err)

            except RuntimeError:
                raise  # Re-raise our own RuntimeError
            except Exception as wt_err:
                raise RuntimeError(
                    f"Failed to create worktrees for pipeline {pipeline_id}: {wt_err}"
                ) from wt_err

        if not repo_volumes:
            raise RuntimeError(
                f"No repo volumes available for pipeline {pipeline_id} — "
                f"worktree creation is required"
            )

        # Sync worktree with remote before starting pipeline phases.  After an
        # orchestrator restart, the local worktree branch may be behind origin:
        # commits pushed by agents in previous phases (contracts, drafts,
        # statefiles) exist on the remote but not in the local checkout.
        # Fetching and resetting ensures downstream code (contract loading,
        # draft reading) sees the full pipeline state from prior phases.
        if worktree_repo_path != repo_path:
            # Determine whether the most recent prior phase completed
            # successfully — this controls whether local-ahead commits are
            # pushed (success) or discarded (failure).
            prior_phase_succeeded = True
            current_phase = pipeline.current_phase
            phase_order = [
                PipelinePhase.REFINE,
                PipelinePhase.PLAN,
                PipelinePhase.IMPLEMENT,
                PipelinePhase.PR,
            ]
            current_idx = phase_order.index(current_phase) if current_phase in phase_order else 0
            if current_idx > 0:
                prior_phase = phase_order[current_idx - 1]
                prior_exec = pipeline.phases.get(prior_phase.value)
                if prior_exec and prior_exec.status in (
                    PipelineStatus.FAILED,
                    PipelineStatus.CANCELLED,
                ):
                    prior_phase_succeeded = False

            _sync_worktree_with_remote(
                spawner,
                pipeline_id,
                worktree_repo_path,
                prior_phase_succeeded=prior_phase_succeeded,
                gateway_mode=gateway_mode,
                base_branch=pipeline.base_branch,
                pipeline_branch=pipeline.branch,
            )

            # When resuming a stale pipeline branch (cancelled run from
            # days/weeks ago), rebase origin/<branch> onto origin/<base>
            # before any orchestrator/agent commits land — otherwise the
            # final PR carries 70+ stale-from-main commits as ancestors
            # (#2098).  No-op for fresh pipelines and for branches already
            # caught up with base.
            if pipeline.branch and pipeline.base_branch:
                try:
                    _rebase_pipeline_branch_onto_base(
                        spawner,
                        pipeline_id,
                        worktree_repo_path,
                        pipeline_branch=pipeline.branch,
                        base_branch=pipeline.base_branch,
                        gateway_mode=gateway_mode,
                    )
                except StalePipelineBranchError as stale_err:
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        pipeline.status = PipelineStatus.FAILED
                        pipeline.error = str(stale_err)
                        store.save_pipeline(pipeline)
                    return

            # Remove legacy unprefixed draft files (analysis.md, plan.md)
            # that may have been left by earlier pipelines on this branch.
            # Uses git rm so deletions are committed directly.  See #1559.
            cleanup_committed = _cleanup_stale_generic_drafts(worktree_repo_path)
            if cleanup_committed and pipeline.branch:
                try:
                    spawner.gateway.push_worktree_branch(
                        pipeline_id=pipeline_id,
                        repo_path=str(worktree_repo_path),
                        branch=pipeline.branch,
                        mode=gateway_mode,
                        base_branch=pipeline.base_branch,
                    )
                except Exception:
                    logger.warning(
                        "Failed to push stale draft cleanup (continuing)",
                        pipeline_id=pipeline_id,
                    )

        # Resolve the certs named volume for gateway CA trust.
        # The docker-compose stack creates ${COMPOSE_PROJECT_NAME:-egg}-certs.
        certs_volume_raw = os.environ.get(
            "EGG_CERTS_VOLUME",
            os.environ.get("COMPOSE_PROJECT_NAME", "egg") + "-certs",
        )
        # Validate volume name: Docker allows [a-zA-Z0-9][a-zA-Z0-9_.-]*
        # We use a permissive check that rejects obvious shell metacharacters.
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", certs_volume_raw):
            logger.warning(
                "Invalid certs volume name, using default",
                raw_name=certs_volume_raw,
            )
            certs_volume = "egg-certs"
        else:
            certs_volume = certs_volume_raw

        # Capture source_branch before _read_source_branch_artifacts clears
        # it on success — the contract-pull path below (#2035) runs inside
        # the contract_synced block and otherwise wouldn't see the value.
        source_branch_for_contract_pull = pipeline.source_branch

        # Read artifacts from source branch if specified and inline values
        # were not provided.  This populates pipeline.plan and
        # pipeline.analysis so the contract creation block below can use them.
        if pipeline.source_branch and not (
            pipeline.plan is not None and pipeline.analysis is not None
        ):
            # source_branch is cleared inside _read_source_branch_artifacts
            # when artifacts are actually found.
            try:
                _read_source_branch_artifacts(
                    repo_path=worktree_repo_path,
                    source_branch=pipeline.source_branch,
                    issue_number=pipeline.issue_number,
                    pipeline_id=pipeline_id,
                    store=store,
                    pipeline=pipeline,
                    source_artifact_prefix=pipeline.source_artifact_prefix,
                    spawner=spawner,
                    gateway_mode=gateway_mode,
                )
            except Exception:
                logger.warning(
                    "Failed to read artifacts from source branch",
                    source_branch=pipeline.source_branch,
                    pipeline_id=pipeline_id,
                    exc_info=True,
                )

            # Write source-branch artifacts to disk so the safety-net
            # _populate_contract_from_plan() call below can find them.
            # The inline-plan path writes drafts inside the contract_synced
            # block, but that block is skipped on pipeline restarts
            # (contract already synced).  Writing here ensures the draft
            # files exist regardless of contract_synced state.
            if pipeline.plan is not None or pipeline.analysis is not None:
                drafts_dir = worktree_repo_path / ".egg-state" / "drafts"
                drafts_dir.mkdir(parents=True, exist_ok=True)

                if pipeline.plan is not None:
                    plan_rel = _get_draft_path(
                        "plan",
                        issue_number=pipeline.issue_number,
                        pipeline_id=pipeline_id,
                    )
                    if plan_rel:
                        plan_path = worktree_repo_path / plan_rel
                        plan_path.write_text(pipeline.plan, encoding="utf-8")
                        logger.info(
                            "Wrote source-branch plan draft to worktree",
                            pipeline_id=pipeline_id,
                            path=plan_rel,
                        )

                if pipeline.analysis is not None:
                    analysis_rel = _get_draft_path(
                        "refine",
                        issue_number=pipeline.issue_number,
                        pipeline_id=pipeline_id,
                    )
                    if analysis_rel:
                        analysis_path = worktree_repo_path / analysis_rel
                        analysis_path.write_text(pipeline.analysis, encoding="utf-8")
                        logger.info(
                            "Wrote source-branch analysis draft to worktree",
                            pipeline_id=pipeline_id,
                            path=analysis_rel,
                        )

        # Create companion contract in the worktree (deferred from pipeline
        # creation so it doesn't pollute the main repo working directory).
        if not pipeline.contract_synced:
            try:
                from egg_contracts.loader import create_contract

                # When source_branch is set, try to carry over the contract
                # (with any resolved HITL decisions) from there instead of
                # overwriting with a fresh zero-state contract (#2035).
                pulled_contract = False
                if source_branch_for_contract_pull:
                    try:
                        pulled_contract = _pull_contract_from_source_branch(
                            repo_path=worktree_repo_path,
                            source_branch=source_branch_for_contract_pull,
                            issue_number=pipeline.issue_number,
                            pipeline_id=pipeline.id,
                            spawner=spawner,
                            gateway_mode=gateway_mode,
                        )
                    except Exception:
                        logger.warning(
                            "Unexpected error pulling contract from source branch — falling back to fresh contract",
                            pipeline_id=pipeline_id,
                            source_branch=source_branch_for_contract_pull,
                            exc_info=True,
                        )
                        pulled_contract = False

                if not pulled_contract:
                    if pipeline.issue_number is not None:
                        issue_url = (
                            f"https://github.com/{pipeline.repo}/issues/{pipeline.issue_number}"
                        )
                        create_contract(
                            issue_number=pipeline.issue_number,
                            title=f"Issue #{pipeline.issue_number}",
                            url=issue_url,
                            pipeline_id=pipeline.id,
                            repo_root=worktree_repo_path,
                        )
                    else:
                        create_contract(
                            pipeline_id=pipeline.id,
                            title=(pipeline.prompt or "")[:100],
                            repo_root=worktree_repo_path,
                        )

                # Write pre-generated drafts for short-flow pipelines so the
                # existing plan parser can populate the contract with tasks.
                if pipeline.analysis or pipeline.plan:
                    drafts_dir = worktree_repo_path / ".egg-state" / "drafts"
                    drafts_dir.mkdir(parents=True, exist_ok=True)

                    if pipeline.analysis:
                        analysis_rel = _get_draft_path(
                            "refine",
                            issue_number=pipeline.issue_number,
                            pipeline_id=pipeline_id,
                        )
                        if analysis_rel:
                            (worktree_repo_path / analysis_rel).write_text(
                                pipeline.analysis, encoding="utf-8"
                            )
                            logger.info(
                                "Wrote pre-generated analysis draft",
                                pipeline_id=pipeline_id,
                                path=analysis_rel,
                            )

                    if pipeline.plan:
                        plan_rel = _get_draft_path(
                            "plan",
                            issue_number=pipeline.issue_number,
                            pipeline_id=pipeline_id,
                        )
                        if plan_rel:
                            (worktree_repo_path / plan_rel).write_text(
                                pipeline.plan, encoding="utf-8"
                            )
                            logger.info(
                                "Wrote pre-generated plan draft",
                                pipeline_id=pipeline_id,
                                path=plan_rel,
                            )

                            # Populate the contract from the plan's yaml-tasks appendix
                            _populate_contract_from_plan(
                                worktree_repo_path,
                                pipeline_id,
                                pipeline_mode,
                                pipeline.issue_number,
                            )

                # Commit all .egg-state/ files so they're on the feature branch
                issue_ref = (
                    f"issue #{pipeline.issue_number}"
                    if pipeline.issue_number is not None
                    else f"pipeline {pipeline_id}"
                )
                try:
                    _commit_statefiles_to_worktree(
                        worktree_repo_path,
                        f"Initialize SDLC contract for {issue_ref}",
                        pipeline_identifier=_pipeline_identifier(
                            pipeline.issue_number, pipeline_id
                        ),
                        pipeline_id=pipeline_id,
                    )
                except Exception as git_err:
                    # Catch broadly so TimeoutExpired/OSError also produce
                    # an explicit FAILED state rather than silently
                    # propagating to the outer handler (#2219).
                    logger.error(
                        "Failed to commit initial statefiles — aborting pipeline",
                        pipeline_id=pipeline_id,
                        error=str(git_err),
                    )
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        pipeline.status = PipelineStatus.FAILED
                        pipeline.contract_synced = False
                        pipeline.error = f"Failed to commit initial statefiles: {git_err}"
                        store.save_pipeline(pipeline)
                    return

                # Push contract statefiles to remote so agents see them.
                # This MUST succeed before agents start — otherwise agents'
                # diffs will include .egg-state/ files they can't push (#1431).
                push_succeeded = False
                # For prompt-driven pipelines, pipeline.branch is None at this
                # point — the branch name is only persisted later when the
                # agent container is spawned (line ~6279).  Derive it here so
                # the push actually happens.  The worktree was already created
                # on this branch by the gateway.
                push_branch = pipeline.branch or f"egg/{pipeline_id}/work"
                if not pipeline.branch:
                    pipeline.branch = push_branch
                    with get_pipeline_state_lock(pipeline_id):
                        p = store.load_pipeline(pipeline_id)
                        if not p.branch:
                            p.branch = push_branch
                            store.save_pipeline(p)
                            logger.info(
                                "Recorded generated branch on pipeline (pre-push)",
                                pipeline_id=pipeline_id,
                                branch=push_branch,
                            )
                if worktree_repo_path != repo_path:
                    push_err_msg = ""
                    # push_worktree_branch reconciles non-fast-forward
                    # rejections internally (fetch+rebase+retry), so a
                    # single call is sufficient — no outer retry needed.
                    try:
                        push_result = spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=push_branch,
                            mode=gateway_mode,
                            base_branch=pipeline.base_branch,
                        )
                        push_succeeded = bool(push_result)
                        if not push_succeeded:
                            push_err_msg = push_result.describe()
                    except Exception as push_err:
                        push_succeeded = False
                        push_err_msg = str(push_err)

                    if not push_succeeded:
                        logger.error(
                            "Contract init push failed after retry — aborting pipeline",
                            pipeline_id=pipeline_id,
                            error=push_err_msg,
                        )
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            pipeline.status = PipelineStatus.FAILED
                            pipeline.contract_synced = False
                            pipeline.error = (
                                f"Failed to push contract init to remote: {push_err_msg}"
                            )
                            store.save_pipeline(pipeline)
                        return
                else:
                    logger.warning(
                        "Skipped contract init push — worktree path equals repo path",
                        pipeline_id=pipeline_id,
                        worktree_repo_path=str(worktree_repo_path),
                        repo_path=str(repo_path),
                    )

                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.contract_synced = push_succeeded
                    store.save_pipeline(pipeline, commit=False)
                logger.info(
                    "Pipeline contract created in worktree",
                    pipeline_id=pipeline_id,
                    mode=pipeline_mode,
                )
            except Exception as contract_err:
                logger.error(
                    "Failed to create contract in worktree",
                    pipeline_id=pipeline_id,
                    error=str(contract_err),
                )
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.error = f"Failed to create contract: {contract_err}"
                    store.save_pipeline(pipeline)
                return

        # Safety net: when start_phase=implement, the plan phase is
        # skipped so the plan-completion hook at the end of the phase loop
        # never fires.  The inline-plan path above calls
        # _populate_contract_from_plan inside the contract_synced block,
        # but that block is skipped on pipeline restarts (contract already
        # synced) and when _read_source_branch_artifacts writes the draft
        # file to the worktree without going through the inline-plan
        # branch.  This catch-all ensures the contract has phases and
        # tasks before agents spawn when the plan phase was skipped.
        # When start_phase=plan, the plan phase runs normally and the
        # plan-completion hook populates the contract, so no safety net
        # is needed.
        if pipeline.config.start_phase == "implement":
            plan_draft_rel = _get_draft_path(
                "plan",
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline.id,
            )
            if plan_draft_rel and (worktree_repo_path / plan_draft_rel).exists():
                # Advance contract.current_phase alongside slice/PR
                # ingestion.  In the natural flow contract.current_phase
                # is mutated by the plan reviewer agent (or the gateway
                # phase API) via apply_mutation; with start_phase=implement
                # no such reviewer ever runs, so the contract would stay
                # at REFINE forever (#2427 sub-bug).  We pass
                # pipeline.current_phase rather than a hardcoded literal
                # so the right value follows automatically if start_phase
                # ever supports values other than 'implement'.  The
                # populator enforces forward-only advancement, so a
                # respawn during the PR phase cannot demote the contract.
                # Note: the *outer* guard above remains hardcoded to
                # ``"implement"``; widening it to other start_phase values
                # is a two-line change (this guard plus the matching
                # ``initial_phase`` mapping in start_pipeline).
                _populate_contract_from_plan(
                    worktree_repo_path,
                    pipeline_id,
                    pipeline_mode,
                    pipeline.issue_number,
                    current_phase=pipeline.current_phase,
                )

        # Check for feedback preserved by the recovery path in start_pipeline
        # or by the inline request_changes handler.  When either stores
        # reviewer feedback in phase_execution.hitl_feedback, we read it
        # here so it can be forwarded to the re-running agents.
        _hitl_review_feedback: str | None = None
        try:
            with get_pipeline_state_lock(pipeline_id):
                _recovery_pipeline = store.load_pipeline(pipeline_id)
                _recovery_phase = _recovery_pipeline.get_phase_execution(
                    _recovery_pipeline.current_phase
                )
                if _recovery_phase.hitl_feedback:
                    _hitl_review_feedback = _recovery_phase.hitl_feedback
                    _recovery_phase.hitl_feedback = None
                    store.save_pipeline(_recovery_pipeline)
        except Exception as e:
            logger.debug("Failed to read hitl_feedback from recovery path", error=str(e))

        # Initialize the Tier 1 health monitor so deterministic tripwires
        # (heartbeat timeout, container exit, repeated errors, message rate,
        # progress stall) fire during pipeline execution.  The monitor
        # subscribes to EventBus events reactively, but check_heartbeats()
        # and check_progress() need periodic polling.
        try:
            from events import get_event_bus
            from health_monitor import init_health_monitor

            health_monitor_instance = init_health_monitor(
                get_event_bus(), pipeline_id, pipeline.config
            )
            # Sync the phase-aware threshold with the current pipeline phase
            health_monitor_instance.set_current_phase(pipeline.current_phase.value)

            # Wake stuck producers directly when check_brc_progress fires
            # so the deterministic detector actually drives remediation
            # instead of relying on the overseer agent's discretion (#2079).
            # The closure reads the monitor's current phase at fire time so
            # the message records the phase the producer is actually in.
            def _on_health_escalation(escalation: dict[str, Any]) -> None:
                phase = health_monitor_instance.get_current_phase()
                _send_brc_confirmation_nudge(escalation, pipeline_id, phase)

            health_monitor_instance.on_escalation(_on_health_escalation)

            # Start a background polling thread for time-based tripwires
            health_monitor_timer = threading.Event()

            overseer_respawn_count = 0
            max_overseer_respawns = pipeline.config.overseer_max_respawns

            # SHAs we've already raised a branch-divergence alert for
            # (#2224 PR 3).  Per-pipeline dedupe so we fire once per
            # offending commit, not once per 30s tick.
            divergence_alerted_shas: set[str] = set()

            def _health_monitor_poll(monitor, stop_event: threading.Event, interval: float = 30.0):
                nonlocal overseer_container_id, overseer_respawn_count, phase_overseer_active
                while not stop_event.is_set():
                    try:
                        # Tier 1 no longer sends nudges directly — it raises
                        # alerts and fires escalation callbacks internally.
                        # The overseer (Tier 2) decides whether to nudge.
                        monitor.check_tripwires()
                    except Exception as poll_err:
                        logger.debug(
                            "Health monitor poll error",
                            pipeline_id=pipeline_id,
                            error=str(poll_err),
                        )

                    # Branch-divergence detector (#2224 PR 3).  Helper
                    # re-loads pipeline state each tick so a
                    # base_branch / branch update mid-pipeline is
                    # picked up.  Dedupe set is mutated in place.
                    _branch_divergence_tick(
                        pipeline_id=pipeline_id,
                        worktree_repo_path=worktree_repo_path,
                        store=store,
                        alerted_shas=divergence_alerted_shas,
                    )

                    # Check overseer liveness and respawn if it exited mid-phase.
                    # Only check when phase_overseer_active is True — the overseer
                    # is intentionally absent between phases, and respawning it
                    # there would waste resources.
                    # The lock prevents a race with the main thread's phase-boundary
                    # teardown: without it, the poll thread could see the container
                    # as EXITED (because the main thread just stopped it) and respawn
                    # an orphaned overseer that nobody will clean up.
                    with overseer_lock:
                        if phase_overseer_active:
                            overseer_container_id, overseer_respawn_count = (
                                _check_and_respawn_overseer(
                                    spawner=spawner,
                                    store=store,
                                    pipeline_id=pipeline_id,
                                    pipeline=pipeline,
                                    overseer_container_id=overseer_container_id,
                                    overseer_respawn_count=overseer_respawn_count,
                                    max_overseer_respawns=max_overseer_respawns,
                                    gateway_mode=gateway_mode,
                                    pipeline_repos=pipeline_repos,
                                    certs_volume=certs_volume,
                                    expected_run_epoch=run_epoch,
                                )
                            )

                    stop_event.wait(interval)

            poll_thread = threading.Thread(
                target=_health_monitor_poll,
                args=(health_monitor_instance, health_monitor_timer),
                daemon=True,
                name=f"health-monitor-{pipeline_id[:8]}",
            )
            poll_thread.start()
            logger.info(
                "Health monitor initialized",
                pipeline_id=pipeline_id,
            )
        except Exception as hm_err:
            # Non-fatal: pipeline can run without Tier 1 monitoring
            logger.warning(
                "Failed to initialize health monitor (continuing without Tier 1 monitoring)",
                pipeline_id=pipeline_id,
                error=str(hm_err),
            )

        while True:
            try:
                pipeline = store.load_pipeline(pipeline_id)
            except Exception:
                # Pipeline was deleted — exit quietly
                logger.info(
                    "Pipeline no longer exists, exiting thread",
                    pipeline_id=pipeline_id,
                )
                return

            # Detect recreation/restart: another run now owns this pipeline ID
            _current_epoch = pipeline.run_epoch or pipeline.created_at
            if _current_epoch != run_epoch:
                logger.info(
                    "Pipeline was recreated, exiting old thread",
                    pipeline_id=pipeline_id,
                )
                return

            if pipeline.status in (PipelineStatus.FAILED, PipelineStatus.CANCELLED):
                logger.info(
                    "Pipeline stopped", pipeline_id=pipeline_id, status=pipeline.status.value
                )
                break

            current_phase = pipeline.current_phase

            # Start the current phase
            phase_execution = pipeline.get_phase_execution(current_phase)
            if phase_execution.status == PipelineStatus.PENDING:
                # Record branch tip SHA for completion signal verification.
                # This allows the completion handler to detect if a commit
                # was pushed to a different branch than expected.
                # NOTE: Intentional TOCTOU — the SHA is captured before
                # acquiring the state lock, so a push between rev-parse and
                # lock acquisition could make it stale.  Acceptable because
                # phase_start_sha is only used for advisory "no new commits"
                # logging, not for correctness decisions.
                phase_start_sha: str | None = None
                try:
                    _sha_result = subprocess.run(
                        ["git", "rev-parse", f"origin/{pipeline.branch}"],
                        capture_output=True,
                        text=True,
                        cwd=str(worktree_repo_path),
                        timeout=10,
                        check=False,
                    )
                    if _sha_result.returncode == 0:
                        phase_start_sha = _sha_result.stdout.strip()
                except Exception:
                    pass  # Non-fatal — verification is best-effort

                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.RUNNING
                    phase_execution.started_at = datetime.now(UTC)
                    phase_execution.phase_start_sha = phase_start_sha
                    pipeline.status = PipelineStatus.RUNNING
                    store.save_pipeline(pipeline)

                # Report phase start to collaborator
                report_pipeline_status(
                    pipeline,
                    event_type="phase.started",
                    message=f"Phase {current_phase.value} started",
                )
                _emit_pipeline_event(pipeline, "phase.started")

            # Spawn overseer container for this phase's health monitoring.
            # The overseer is phase-scoped: spawned at phase start and torn
            # down at phase completion/advance/failure.  Each phase gets a
            # fresh overseer instance with no accumulated state.
            if pipeline.config.overseer_enabled:
                try:
                    overseer_result = spawner.spawn_overseer_container(
                        pipeline_id=pipeline_id,
                        issue_number=pipeline.issue_number,
                        mode=gateway_mode,
                        poll_interval=pipeline.config.overseer_poll_interval_seconds,
                        decision_model=pipeline.config.overseer_decision_maker_model,
                        max_turns=pipeline.config.overseer_max_turns,
                        repos=pipeline_repos if pipeline_repos else None,
                        certs_volume=certs_volume,
                    )
                    with overseer_lock:
                        overseer_container_id = overseer_result.container_info.container_id
                        phase_overseer_active = True
                        overseer_respawn_count = 0
                    logger.info(
                        "Overseer container spawned for phase",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        container_id=overseer_container_id[:12],
                    )
                except (ContainerSpawnError, KubernetesSpawnError) as e:
                    # Non-fatal: pipeline can run without overseer monitoring
                    logger.warning(
                        "Failed to spawn overseer container (continuing without monitoring)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(e),
                    )

            # Common sandbox environment for all containers in this phase.
            # GATEWAY_URL, RUNTIME_UID/GID, proxy vars, DNS lockdown, and
            # extra_hosts are now handled by the shared build_sandbox_config()
            # inside spawn_agent_container().  Only pipeline-specific vars go here.
            if gateway_mode == "private":
                orchestrator_ip = ORCHESTRATOR_ISOLATED_IP
            else:
                orchestrator_ip = ORCHESTRATOR_EXTERNAL_IP
            orchestrator_url = f"http://{orchestrator_ip}:{ORCHESTRATOR_PORT}"
            sandbox_env: dict[str, str] = {
                "EGG_PIPELINE_ID": pipeline_id,
                "EGG_PIPELINE_PHASE": current_phase.value,
                "EGG_PIPELINE_MODE": pipeline_mode,
                "EGG_ORCHESTRATOR_URL": orchestrator_url,
                "EGG_ORCHESTRATOR_MODE": "distributed",
            }
            # ``EGG_BRANCH`` is intentionally NOT set here. The spawner
            # is the single source of truth for the agent's assigned
            # branch (#2428): ``KubernetesSpawner.spawn_agent_job``
            # derives ``EGG_BRANCH`` from its ``branch`` parameter,
            # which the slice scheduler populates with the slice
            # integration branch via
            # ``ConcurrentPhaseExecutor.get_worktree_branch``. Stuffing
            # ``pipeline.branch`` into ``sandbox_env`` here used to be
            # threaded through ``extra_env``, where the spawner's
            # override loop runs after the default-from-``branch``
            # assignment — deterministic precedence, not a race — so
            # the pipeline-level value silently won and slice agents
            # were downgraded to the pipeline tip, breaking every
            # slice-coder push. The branch persistence below is the
            # only side-effect the run loop still needs.
            if not pipeline.branch:
                generated_branch = f"egg/{pipeline_id}/work"
                # Persist the generated branch so the PR phase can use it
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    if not pipeline.branch:
                        pipeline.branch = generated_branch
                        store.save_pipeline(pipeline)
                        logger.info(
                            "Recorded generated branch on pipeline",
                            pipeline_id=pipeline_id,
                            branch=generated_branch,
                        )
            if pipeline.prompt:
                sandbox_env["EGG_PIPELINE_PROMPT"] = pipeline.prompt

            if pipeline.repo:
                repos = [pipeline.repo]
                sandbox_env["EGG_REPO"] = pipeline.repo
            else:
                repos = []

            # Jira ticket advisory env vars (issue #1556).  These give sandbox
            # agents a stable handle for the ticket the pipeline is working
            # against (``jira ticket get "$EGG_JIRA_TICKET"``) without
            # hard-coding the key.  They are ADVISORY — the gateway's project
            # allowlist is the only hard boundary, and we never export
            # Atlassian credentials (JIRA_BASE_URL / JIRA_USERNAME /
            # JIRA_API_TOKEN) to the sandbox.  An empty string is exported
            # when no ticket is configured so agent wrappers can rely on
            # variable presence.
            jira_ticket_value = getattr(pipeline, "jira_ticket", None) or ""
            sandbox_env["EGG_JIRA_TICKET"] = jira_ticket_value
            if jira_ticket_value and "-" in jira_ticket_value:
                sandbox_env["EGG_JIRA_PROJECT"] = jira_ticket_value.split("-", 1)[0]
            else:
                sandbox_env["EGG_JIRA_PROJECT"] = ""

            phase_failed = False
            tester_gap_summary: str | None = None

            # --- Auto PR creation: skip agent spawn for PR phase ---
            if current_phase.value == "pr":
                is_babysit_mode = getattr(pipeline, "mode", None) == PipelineMode.BABYSIT
                logger.info(
                    "Auto-creating PR (skipping agent spawn)"
                    if not is_babysit_mode
                    else "Finalising babysit-pr cycle (skipping PR creation)",
                    pipeline_id=pipeline_id,
                    mode=getattr(getattr(pipeline, "mode", None), "value", None),
                )

                # Record phase timing so metrics are accurate even without agent spawn
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.work_started_at = datetime.now(UTC)
                    store.save_pipeline(pipeline)

                # Babysit-pr final-push head-move guard (#1748): the PR already
                # exists, so a remote HEAD move on the PR branch since pipeline
                # creation means either a human pushed to the PR mid-cycle or
                # a concurrent babysit run landed first.  Either way we should
                # NOT push the orchestrator's housekeeping commits — aborting
                # preserves the existing PR state and escalates via HITL.
                # Skip the normal push+PR-create path when the guard trips OR
                # when we are in babysit mode (the PR already exists).
                skip_pr_creation = False
                if is_babysit_mode:
                    head_ok, actual_sha = _verify_pr_head_unchanged(pipeline, worktree_repo_path)
                    if not head_ok:
                        stored_sha = getattr(pipeline, "pr_head_sha", None) or "unknown"
                        actual_display = actual_sha or "unknown"
                        error_msg = (
                            f"babysit-pr aborted: PR head moved from "
                            f"{stored_sha[:7]} to {actual_display[:7]} on "
                            f"origin/{pipeline.branch} during the cycle. "
                            "Refusing to push orchestrator housekeeping commits — "
                            "re-run babysit-pr against the current PR head or "
                            "resolve the conflict manually."
                        )
                        logger.error(
                            "babysit-pr head-move guard tripped",
                            pipeline_id=pipeline_id,
                            stored_sha=stored_sha,
                            actual_sha=actual_sha,
                            branch=pipeline.branch,
                        )
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            phase_execution.status = PipelineStatus.FAILED
                            phase_execution.error = error_msg
                            phase_execution.completed_at = datetime.now(UTC)
                            pipeline.status = PipelineStatus.FAILED
                            pipeline.error = error_msg
                            store.save_pipeline(pipeline)
                        phase_failed = True
                        skip_pr_creation = True
                    else:
                        # Guard passed — still skip gh pr create because
                        # the PR already exists in babysit mode.  The
                        # push below updates the PR head with the cycle's
                        # consensus output.
                        skip_pr_creation = True

                # Ensure contract and statefiles exist before PR creation
                # (safety net for short-flow pipelines where initial push
                # may have failed).  Skip when the pipeline already failed
                # (e.g. head-move guard tripped) — these are wasted work
                # against a failed pipeline and could have side effects.
                if not phase_failed:
                    if not _ensure_statefiles_on_branch(worktree_repo_path, pipeline):
                        logger.warning(
                            "Contract reconciliation failed — PR may be missing contract",
                            pipeline_id=pipeline_id,
                        )

                    # Commit any uncommitted contract mutations before
                    # opening the PR.  Under the orchestrator-owned
                    # contract model (#1781), late-phase agent mutations
                    # land directly in the shared worktree file and may
                    # not yet be on the branch; this ensures the contract
                    # is captured in git history as part of the PR.
                    try:
                        _commit_statefiles_to_worktree(
                            worktree_repo_path,
                            "Persist contract before PR creation",
                            pipeline_identifier=_pipeline_identifier(
                                pipeline.issue_number, pipeline_id
                            ),
                            pipeline_id=pipeline_id,
                        )
                    except Exception as git_err:
                        # Catch broadly: see #2219.
                        logger.warning(
                            "Pre-PR statefile commit failed (continuing)",
                            pipeline_id=pipeline_id,
                            error=str(git_err),
                        )

                    # Safety net: re-write BRC history for all completed phases.
                    # Per-phase writes happen at phase completion, but pushes
                    # can fail silently — re-writing here guarantees the files
                    # are on the branch before the PR is created.
                    # Babysit-pr pipelines use pr-{N}-{short-sha} so repeated
                    # babysit runs against the same PR do not clobber each
                    # other's history (#1748).
                    identifier = _brc_history_identifier(pipeline)

                    # Drop .egg-state/agent-outputs/ before any other PR-phase
                    # commits.  Those paths hold ephemeral coder→tester handoff
                    # patches (e.g. coder-test-changes.patch) that the tester
                    # has already consumed; leaving them on the branch pollutes
                    # the PR diff and causes reconcile conflicts when concurrent
                    # pipelines write divergent contents to the same filename
                    # (see #1731).
                    _cleanup_agent_outputs_for_pr(worktree_repo_path, pipeline_id)

                    _rewrite_brc_history_for_pr(
                        worktree_repo_path,
                        pipeline_id,
                        pipeline.phases,
                        identifier,
                    )

                # Pipeline draft files (.egg-state/drafts/{id}-*.md) are
                # intentionally *preserved* on the PR branch so that analysis
                # and plan artifacts remain reviewable alongside the code
                # (see issue #1713).

                # Push latest commits before creating PR.  If the push fails
                # (e.g. the remote advanced while the PR-phase worktree was
                # adding BRC commits), push_worktree_branch reconciles via
                # fetch+rebase and retries once internally (#1706/#1731/#1808).
                # Babysit-pr with a tripped head-move guard skips the push
                # entirely to preserve the existing PR state (#1748).
                push_ok = True
                if phase_failed and is_babysit_mode:
                    push_ok = False
                elif pipeline.branch and worktree_repo_path != repo_path:
                    commits_ahead = "unknown"
                    try:
                        ahead_result = subprocess.run(
                            [
                                "git",
                                "-C",
                                str(worktree_repo_path),
                                "rev-list",
                                "--count",
                                f"origin/{pipeline.branch}..HEAD",
                            ],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=10,
                        )
                        commits_ahead = (
                            ahead_result.stdout.strip()
                            if ahead_result.returncode == 0
                            else "unknown"
                        )
                    except Exception:
                        commits_ahead = "unknown"

                    push_ok = spawner.gateway.push_worktree_branch(
                        pipeline_id=pipeline_id,
                        repo_path=str(worktree_repo_path),
                        branch=pipeline.branch,
                        mode=gateway_mode,
                        base_branch=pipeline.base_branch,
                    )
                    if push_ok:
                        logger.info(
                            "PR-phase push succeeded",
                            pipeline_id=pipeline_id,
                            branch=pipeline.branch,
                            commits_ahead_pre_reconcile=commits_ahead,
                        )
                    else:
                        # Fall back to creating the PR against the current
                        # remote HEAD.  The agents' commits are already on
                        # origin; only the orchestrator's housekeeping
                        # commits (BRC history rewrite, cleanup) are being
                        # dropped by the failed push.  Better to ship a PR
                        # without the housekeeping than to fail the whole
                        # pipeline and force manual rescue (see #1731).
                        logger.warning(
                            "PR-phase push failed after reconcile — falling back to "
                            "PR against remote HEAD; orchestrator housekeeping commits dropped",
                            pipeline_id=pipeline_id,
                            branch=pipeline.branch,
                            commits_ahead_pre_reconcile=commits_ahead,
                        )
                else:
                    logger.info(
                        "PR-phase push skipped",
                        pipeline_id=pipeline_id,
                        branch=pipeline.branch,
                        reason="worktree_repo_path == repo_path"
                        if worktree_repo_path == repo_path
                        else "no branch set",
                    )

                # Create the PR.  When ``push_ok`` is False we still try —
                # the PR opens against whatever is on origin/<branch>
                # (the agents' work), dropping orchestrator housekeeping
                # commits rather than failing the whole pipeline (#1731).
                # Babysit-pr mode already has a PR — skip PR creation.
                if skip_pr_creation:
                    logger.info(
                        "Skipping PR creation (babysit-pr already has a PR)",
                        pipeline_id=pipeline_id,
                        pr_number=getattr(pipeline, "pr_number", None),
                    )
                elif _finalize_pr_phase_failed(
                    pipeline,
                    worktree_repo_path,
                    spawner,
                    store,
                    pipeline_id,
                    current_phase,
                    gateway_mode,
                    push_ok,
                ):
                    phase_failed = True

                # Fall through to phase completion below (skip inner review cycle)

            # --- Inner review cycle (skipped when auto-creating PR) ---
            else:
                while True:
                    # Reset tester gaps each cycle so stale findings don't accumulate
                    tester_gap_summary = None

                    # Reload to get latest review_cycles count
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        review_cycle = phase_execution.review_cycles

                        # Reset status to RUNNING at cycle start so that a
                        # previous cycle's FAILED status doesn't persist and
                        # cause _derive_subphase_status() to misreport (see
                        # issue #1178).
                        phase_execution.status = PipelineStatus.RUNNING
                        pipeline.status = PipelineStatus.RUNNING

                        # Record when actual agent work begins (excludes sandbox setup
                        # and HITL waiting time from the phase duration).
                        phase_execution.work_started_at = datetime.now(UTC)

                        # Capture HEAD commit for delta reviews: reviewers in
                        # subsequent cycles can diff against this to see only
                        # the changes made since the last review.
                        cycle_commit_sha: str | None = None
                        try:
                            _git_result = subprocess.run(
                                ["git", "rev-parse", "HEAD"],
                                capture_output=True,
                                text=True,
                                cwd=str(worktree_repo_path),
                                timeout=10,
                            )
                            if _git_result.returncode == 0:
                                cycle_commit_sha = _git_result.stdout.strip()
                        except Exception:
                            pass  # Non-fatal — delta review is best-effort

                        phase_execution.cycle_timings.append(
                            CycleTiming(
                                cycle=review_cycle,
                                started_at=phase_execution.work_started_at,
                                commit_sha=cycle_commit_sha,
                            )
                        )
                        store.save_pipeline(pipeline)

                    # 1. Spawn workers — always use concurrent BRC execution.
                    logger.info(
                        "Spawning concurrent phase execution",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        review_cycle=review_cycle,
                        mode=gateway_mode,
                    )

                    # Read HITL feedback stored by the inline request_changes
                    # handler or the AWAITING_HUMAN recovery path, and clear it
                    # so it's only forwarded once.
                    _phase_review_feedback: str | None = None
                    if _hitl_review_feedback:
                        _phase_review_feedback = _hitl_review_feedback
                        _hitl_review_feedback = None
                    else:
                        # Re-read from persisted state in case the inline path
                        # stored feedback and looped back via continue.
                        try:
                            with get_pipeline_state_lock(pipeline_id):
                                _fb_pipeline = store.load_pipeline(pipeline_id)
                                _fb_phase = _fb_pipeline.get_phase_execution(current_phase)
                                if _fb_phase.hitl_feedback:
                                    _phase_review_feedback = _fb_phase.hitl_feedback
                                    _fb_phase.hitl_feedback = None
                                    store.save_pipeline(_fb_pipeline)
                        except Exception as e:
                            logger.debug("Failed to read hitl_feedback for phase", error=str(e))

                    # #2137: route the implement phase through the slice
                    # DAG iterator when the contract has more than one
                    # slice. Single-slice and no-slice contracts continue
                    # to use the legacy monolithic path so existing
                    # pipelines are unaffected.
                    _use_slice_loop = False
                    _slice_gate_failure: str | None = None
                    if current_phase.value == "implement":
                        try:
                            from egg_contracts.loader import (
                                load_contract as _load_contract_for_slice_check,
                            )

                            _check_contract = _load_contract_for_slice_check(
                                pipeline_id, worktree_repo_path
                            )
                            _slice_count = len(getattr(_check_contract, "slices", []) or [])
                            _use_slice_loop = _slice_count > 1

                            # #2337 defensive recheck: if the contract has no
                            # slices but the on-disk plan draft parses to N>1
                            # slices, the populator silently failed earlier.
                            # Refuse to demote to monolithic.
                            if _slice_count == 0:
                                _slice_gate_failure = _slice_gate_block_monolithic_demotion(
                                    worktree_repo_path,
                                    pipeline_id,
                                    pipeline.issue_number,
                                )
                        except Exception as _slice_check_err:  # noqa: BLE001
                            logger.debug(
                                "Slice-loop gate: contract load failed, falling back to monolithic",
                                pipeline_id=pipeline_id,
                                error=str(_slice_check_err),
                            )

                    if _slice_gate_failure is not None:
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            if phase_execution.cycle_timings:
                                phase_execution.cycle_timings[-1].completed_at = datetime.now(UTC)
                            phase_execution.status = PipelineStatus.FAILED
                            phase_execution.error = _slice_gate_failure
                            phase_execution.completed_at = datetime.now(UTC)
                            pipeline.status = PipelineStatus.FAILED
                            pipeline.error = _slice_gate_failure
                            store.save_pipeline(pipeline)
                        logger.error(
                            "OVERSEER_ALERT slice_gate_blocked_monolithic_demotion",
                            pipeline_id=pipeline_id,
                            error=_slice_gate_failure,
                        )
                        phase_failed = True
                        break

                    try:
                        if _use_slice_loop:
                            exit_code, container_logs = _run_implement_phase_slices(
                                pipeline_id=pipeline_id,
                                pipeline=pipeline,
                                spawner=spawner,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=repos,
                                sandbox_env=sandbox_env,
                                store=store,
                                certs_volume=certs_volume,
                                worktree_repo_path=worktree_repo_path,
                            )
                        else:
                            exit_code, container_logs = _run_concurrent_phase(
                                pipeline_id=pipeline_id,
                                pipeline=pipeline,
                                phase=current_phase,
                                spawner=spawner,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=repos,
                                sandbox_env=sandbox_env,
                                store=store,
                                certs_volume=certs_volume,
                                worktree_repo_path=worktree_repo_path,
                                review_feedback=_phase_review_feedback,
                            )
                    except (ContainerSpawnError, KubernetesSpawnError) as e:
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            if phase_execution.cycle_timings:
                                phase_execution.cycle_timings[-1].completed_at = datetime.now(UTC)
                            phase_execution.status = PipelineStatus.FAILED
                            phase_execution.error = str(e)
                            phase_execution.completed_at = datetime.now(UTC)
                            pipeline.status = PipelineStatus.FAILED
                            pipeline.error = str(e)
                            store.save_pipeline(pipeline)
                        logger.error(
                            "Failed to spawn concurrent containers",
                            pipeline_id=pipeline_id,
                            error=str(e),
                        )
                        phase_failed = True
                        break

                    if exit_code != 0:
                        # Check if pipeline was restarted while this thread
                        # was running (e.g. restart_phase bumped run_epoch).
                        # If so, a new _run_pipeline thread owns this pipeline
                        # — exit without marking the phase FAILED.  See #1638.
                        _check_pip = store.load_pipeline(pipeline_id)
                        _check_epoch = _check_pip.run_epoch or _check_pip.created_at
                        if _check_epoch != run_epoch:
                            logger.info(
                                "Pipeline was restarted during phase execution, exiting old thread",
                                pipeline_id=pipeline_id,
                            )
                            return

                        error_msg = f"Container exited with code {exit_code}"
                        if container_logs:
                            log_lines = container_logs.strip().splitlines()
                            tail = "\n".join(log_lines[-10:])
                            error_msg += f"\n--- container logs (last 10 lines) ---\n{tail}"

                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            if phase_execution.cycle_timings:
                                phase_execution.cycle_timings[-1].completed_at = datetime.now(UTC)
                            phase_execution.status = PipelineStatus.FAILED
                            phase_execution.error = error_msg
                            phase_execution.completed_at = datetime.now(UTC)
                            pipeline.status = PipelineStatus.FAILED
                            pipeline.error = error_msg
                            store.save_pipeline(pipeline)
                        logger.error(
                            "Phase failed",
                            pipeline_id=pipeline_id,
                            phase=current_phase,
                            exit_code=exit_code,
                            container_logs=container_logs[-2000:] if container_logs else "",
                        )
                        phase_failed = True
                        break

                    # 2. Read tester gap findings (concurrent phases include a tester).
                    # Only read when the phase succeeded — a failed phase may
                    # have left stale output from a previous cycle on disk.
                    if not phase_failed:
                        tester_gap_summary = _read_tester_gaps(
                            worktree_repo_path,
                            identifier=_pipeline_identifier(pipeline.issue_number, pipeline_id),
                        )
                        if tester_gap_summary:
                            logger.info(
                                "Tester found gaps",
                                pipeline_id=pipeline_id,
                                phase=current_phase,
                            )

                    # Reviewers are handled within the BRC consensus protocol
                    # (see issue #1178) — advance to next phase.
                    break

            # If the phase failed, emit the failure event so the SSE stream
            # terminates, then break out of the outer loop.
            if phase_failed:
                # Stop the phase-scoped overseer on failure.
                # Hold the lock to prevent the poll thread from seeing the
                # container as EXITED and respawning it.
                with overseer_lock:
                    if overseer_container_id and phase_overseer_active:
                        phase_overseer_active = False
                        _teardown_phase_overseer(
                            spawner,
                            overseer_container_id,
                            pipeline_id,
                            phase_label=str(current_phase),
                            reason="phase failed",
                        )

                # report_pipeline_status is a stub (no-op) unless status_reporter
                # is installed.  The actual SSE emission is _emit_pipeline_event
                # below.  Kept for consistency with the except block at the
                # bottom of this function.
                report_pipeline_status(
                    pipeline,
                    event_type="pipeline.failed",
                    message=f"Pipeline failed: {(pipeline.error or 'unknown')[:100]}",
                )
                _emit_pipeline_event(pipeline, "pipeline.failed")

                # Best-effort: push worktree branch to remote so work is backed up
                if pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                            mode=gateway_mode,
                            base_branch=pipeline.base_branch,
                        )
                    except Exception as push_err:
                        logger.warning(
                            "Best-effort push on failure failed",
                            pipeline_id=pipeline_id,
                            error=str(push_err),
                        )

                break

            # Phase succeeded — mark complete and advance
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.status = PipelineStatus.COMPLETE
                phase_execution.completed_at = datetime.now(UTC)

                store.save_pipeline(pipeline)  # Persist phase completion before HITL gate

            # Report phase completion to collaborator
            report_pipeline_status(
                pipeline,
                event_type="phase.completed",
                message=f"Phase {current_phase.value} completed",
            )
            _emit_pipeline_event(pipeline, "phase.completed")

            # Commit any uncommitted ``.egg-state/`` writes the agents
            # made during the phase BEFORE the worktree sync runs.
            # ``register_open_question`` / ``request_feedback`` mutate the
            # contract live in the shared pipeline worktree (see
            # ``orchestrator/contract_store.py``); those writes are
            # uncommitted on disk.  The ``git reset --hard`` step inside
            # ``_sync_worktree_with_remote`` discards them, leaving the
            # bridge below with an empty ``contract.decisions`` and
            # silently dropping the operator-bound questions (#2488).
            # Committing first lets the sync's rebase reconcile them
            # against agent-pushed drafts cleanly.
            try:
                _commit_statefiles_to_worktree(
                    worktree_repo_path,
                    f"Persist agent statefile writes before {current_phase.value} sync",
                    pipeline_identifier=_pipeline_identifier(pipeline.issue_number, pipeline_id),
                    pipeline_id=pipeline_id,
                )
            except Exception as git_err:
                logger.warning(
                    "Failed to commit pre-sync agent statefiles (continuing)",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                    error=str(git_err),
                )

            # Sync worktree with remote before post-phase modifications
            # so that agent-pushed commits (including plan drafts) are
            # incorporated.  This must run BEFORE _populate_contract_from_plan
            # and _sync_pipeline_decisions_to_contract — otherwise
            # git reset --hard in _sync_worktree_with_remote would revert
            # their on-disk modifications.  Running the sync first also
            # ensures _populate_contract_from_plan can read agent-produced
            # draft files that only exist on the remote.
            if pipeline.branch and worktree_repo_path != repo_path:
                # Best-effort: a sync failure must not strand the
                # auto-advance.  Without this guard, a gateway HTTP error
                # or git subprocess failure inside the helper propagates
                # to the outer Exception handler and (if marking FAILED
                # also fails) leaves the pipeline wedged with phase
                # COMPLETE but no successor (#2219).
                try:
                    _sync_worktree_with_remote(
                        spawner,
                        pipeline_id,
                        worktree_repo_path,
                        gateway_mode=gateway_mode,
                        base_branch=pipeline.base_branch,
                        pipeline_branch=pipeline.branch,
                    )
                except Exception as sync_err:
                    logger.warning(
                        "Failed to sync worktree with remote after phase (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(sync_err),
                    )

            # After plan phase: populate contract with task structure.
            # NOTE: worktree_repo_path is used for both draft reads and
            # contract load/save inside _populate_contract_from_plan.
            # The contract was created at worktree_repo_path above, so
            # both operations must use the same path.
            # Called on every successful plan completion (including after
            # HITL revision) so the contract reflects the latest approved
            # plan, not a previously rejected draft.
            #
            # Routed through _populate_contract_from_plan_safe so a raised
            # exception here cannot skip the HITL gate below (#1890).  The
            # same helper is invoked from advance_phase so force-advances
            # out of plan see the same populate step (#1941).
            #
            # ``source="plan_complete"`` makes the wrapper raise
            # PlanDraftMissingOnLocalError when the draft is missing locally
            # but present on origin — the silent demotion-to-monolithic
            # failure mode behind #2337.  We catch it below and mark the
            # pipeline FAILED so the operator can intervene rather than
            # implement silently shipping slice-1 alone.
            if current_phase.value == "plan":
                try:
                    _populate_contract_from_plan_safe(
                        worktree_repo_path,
                        pipeline_id,
                        pipeline_mode,
                        pipeline.issue_number,
                        source="plan_complete",
                        branch=pipeline.branch,
                    )
                except PlanDraftMissingOnLocalError as missing_err:
                    # Mirror the slice-gate failure handler at the
                    # implement-phase entry: mark FAILED in state,
                    # then run the same cleanup sequence as the
                    # ``if phase_failed:`` block above (teardown phase
                    # overseer, report pipeline status, best-effort push
                    # for backup) so both load-bearing failure paths
                    # have a uniform cleanup story.  Re #2337 review.
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        phase_execution.status = PipelineStatus.FAILED
                        phase_execution.error = str(missing_err)
                        phase_execution.completed_at = datetime.now(UTC)
                        pipeline.status = PipelineStatus.FAILED
                        pipeline.error = str(missing_err)
                        store.save_pipeline(pipeline)
                    logger.error(
                        "Pipeline FAILED: plan draft missing on local but present on origin",
                        pipeline_id=pipeline_id,
                        error=str(missing_err),
                    )
                    # Stop the phase-scoped overseer on failure.
                    # Hold the lock to prevent the poll thread from seeing
                    # the container as EXITED and respawning it.
                    with overseer_lock:
                        if overseer_container_id and phase_overseer_active:
                            phase_overseer_active = False
                            _teardown_phase_overseer(
                                spawner,
                                overseer_container_id,
                                pipeline_id,
                                phase_label=str(current_phase),
                                reason="plan draft missing on local",
                            )
                    report_pipeline_status(
                        pipeline,
                        event_type="pipeline.failed",
                        message=f"Pipeline failed: {(pipeline.error or 'unknown')[:100]}",
                    )
                    _emit_pipeline_event(pipeline, "pipeline.failed")
                    # Best-effort: push worktree branch to remote so work
                    # is backed up before the pipeline exits.
                    if pipeline.branch and worktree_repo_path != repo_path:
                        try:
                            spawner.gateway.push_worktree_branch(
                                pipeline_id=pipeline_id,
                                repo_path=str(worktree_repo_path),
                                branch=pipeline.branch,
                                mode=gateway_mode,
                                base_branch=pipeline.base_branch,
                            )
                        except Exception as push_err:
                            logger.warning(
                                "Best-effort push on failure failed",
                                pipeline_id=pipeline_id,
                                error=str(push_err),
                            )
                    break

            # After refine and plan phases: sync substantive HITL decisions
            # (non-phase-gate) to the contract so implement-phase agents
            # can see what was decided.  Called for both refine and plan
            # phases — refine decisions inform the plan, plan decisions
            # inform the implementation.
            if current_phase.value in _HITL_GATE_PHASES:
                try:
                    _sync_pipeline_decisions_to_contract(
                        repo_path,
                        worktree_repo_path,
                        pipeline_id,
                    )
                except Exception as sync_err:
                    logger.warning(
                        "Failed to sync pipeline decisions to contract (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(sync_err),
                    )

            # Write BRC consensus history for this phase before committing
            # statefiles so the history file is included in the commit.
            # Babysit-pr pipelines use pr-{N}-{short-sha} to avoid clobbering
            # prior runs against the same PR (#1748).
            try:
                _write_brc_history(
                    worktree_repo_path,
                    pipeline_id,
                    current_phase.value,
                    _brc_history_identifier(pipeline),
                )
            except Exception as brc_err:
                logger.debug(
                    "Failed to write BRC history (continuing)",
                    pipeline_id=pipeline_id,
                    phase=current_phase,
                    error=str(brc_err),
                )

            # Commit any .egg-state/ files produced during this phase
            # (drafts, reviews, check results, contract updates).  Mirrors
            # the GHA workflow's `git add .egg-state/` at phase boundaries.
            try:
                _commit_statefiles_to_worktree(
                    worktree_repo_path,
                    f"Persist statefiles after {current_phase.value} phase",
                    pipeline_identifier=_pipeline_identifier(pipeline.issue_number, pipeline_id),
                    pipeline_id=pipeline_id,
                )
            except Exception as git_err:
                # Catch broadly: the helper does ``subprocess.run(check=True,
                # timeout=30)`` which can raise ``TimeoutExpired`` (not a
                # CalledProcessError) and ``glob.glob`` which can raise
                # ``OSError``.  A narrow ``except`` here let either escape
                # to the outer handler and stranded the pipeline (#2219).
                logger.warning(
                    "Failed to commit statefiles after phase (continuing)",
                    pipeline_id=pipeline_id,
                    phase=current_phase,
                    error=str(git_err),
                )

            # Push statefiles to remote so the next phase's agents
            # don't have unpushed .egg-state/ files in their diff.
            if pipeline.branch and worktree_repo_path != repo_path:
                try:
                    spawner.gateway.push_worktree_branch(
                        pipeline_id=pipeline_id,
                        repo_path=str(worktree_repo_path),
                        branch=pipeline.branch,
                        mode=gateway_mode,
                        base_branch=pipeline.base_branch,
                    )
                except Exception as push_err:
                    logger.warning(
                        "Failed to push statefiles after phase (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        error=str(push_err),
                    )

            # --- HITL gate: pause for human approval ---
            if pipeline.config.hitl_gates and current_phase.value in _HITL_GATE_PHASES:
                # Check for an existing pending phase_gate decision for this
                # phase.  A prior agent-exit event may
                # have already created one — creating a duplicate confuses the
                # human reviewer.  See #1152.
                existing_pending_gate = any(
                    d.decision_type == "phase_gate"
                    and d.phase == current_phase
                    and d.status == DecisionStatus.PENDING
                    for d in pipeline.decisions
                )

                if existing_pending_gate:
                    logger.info(
                        "HITL gate: reusing existing pending phase_gate decision",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                    )
                    # Find the existing decision to wait on
                    dq = get_decision_queue(pipeline_id, repo_path)
                    decision = next(
                        d
                        for d in reversed(pipeline.decisions)
                        if d.decision_type == "phase_gate"
                        and d.phase == current_phase
                        and d.status == DecisionStatus.PENDING
                    )
                else:
                    draft_content = _read_phase_draft(
                        worktree_repo_path,
                        current_phase.value,
                        issue_number=pipeline.issue_number,
                        pipeline_id=pipeline_id,
                        branch=pipeline.branch,
                    )
                    phase_label = (
                        "analysis" if current_phase.value == "refine" else current_phase.value
                    )

                    # Warn if draft is missing — the agent may not have written
                    # it to the expected path.  See #1016.
                    if draft_content is None:
                        logger.warning(
                            "HITL gate: draft not found on work branch",
                            pipeline_id=pipeline_id,
                            phase=current_phase.value,
                            worktree_path=str(worktree_repo_path),
                        )
                        draft_content = (
                            f"**Warning**: No {phase_label} draft was found on the "
                            f"work branch. The agent may not have written the output "
                            f"to the expected path."
                        )

                    question = (
                        f"The {current_phase.value} phase has completed. "
                        f"Please review the {phase_label} and approve to continue, "
                        f"or provide feedback to request changes."
                    )

                    # Detect whether the draft changed compared to the
                    # previous phase_gate decision for this phase (if any).
                    _content_changed: bool | None = None
                    _prev_gate = next(
                        (
                            d
                            for d in reversed(pipeline.decisions)
                            if d.decision_type == "phase_gate"
                            and d.phase == current_phase
                            and d.status == DecisionStatus.RESOLVED
                        ),
                        None,
                    )
                    if _prev_gate is not None:
                        _content_changed = draft_content != _prev_gate.context

                    dq = get_decision_queue(pipeline_id, repo_path)
                    decision = dq.queue_decision(
                        question=question,
                        context=draft_content,
                        options=["approve", "request changes"],
                        decision_type="phase_gate",
                        phase=current_phase,
                        content_changed=_content_changed,
                    )

                # Reload pipeline to pick up the decision persisted by queue_decision(),
                # otherwise the stale local object overwrites it with an empty decisions list.
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.AWAITING_HUMAN
                    # Also mark the phase as awaiting human so the DAG visualization
                    # shows the HITL gate on the correct phase box.
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.AWAITING_HUMAN
                    store.save_pipeline(pipeline)

                # Report HITL gate to collaborator
                report_pipeline_status(
                    pipeline,
                    event_type="decision.created",
                    message=f"Awaiting human approval for {current_phase.value} phase",
                )
                _emit_pipeline_event(pipeline, "decision.created")

                dq.wait_for_decision(decision.id)

                # Check resolution — did the human approve or request changes?
                resolved_decision = dq.get_decision(decision.id)
                resolution = (resolved_decision.resolution or "").strip()

                # JSON-first resolution parsing: try structured payload before
                # falling back to keyword matching for legacy bare-string resolutions.
                _is_approved = False
                _needs_revision = False
                _revision_feedback: str | None = None

                try:
                    payload = json.loads(resolution)
                    if isinstance(payload, dict) and "action" in payload:
                        action = payload["action"]
                        feedback_text = payload.get("feedback", "")

                        if action == "approve":
                            _is_approved = True
                        elif action == "select":
                            # Selection from a choice menu — treat as approval
                            _is_approved = True
                        elif action == "submit_feedback":
                            # Feedback submission — treat as approval (info collected)
                            _is_approved = True
                        elif action in ("request_changes", "change_approach"):
                            if feedback_text:
                                # R-1: Extract readable feedback, not raw JSON
                                _needs_revision = True
                                _revision_feedback = feedback_text
                            else:
                                # JSON request_changes without feedback — same as bare label
                                _needs_revision = True
                                _revision_feedback = None
                        else:
                            # Unknown action — fall through to legacy matching
                            raise json.JSONDecodeError("unknown action", resolution, 0)
                    else:
                        # Valid JSON but no action field — fall through to legacy
                        raise json.JSONDecodeError("no action field", resolution, 0)
                except json.JSONDecodeError, TypeError, AttributeError:
                    # Legacy bare-string resolution — existing keyword matching
                    if resolution.lower() in _APPROVE_KEYWORDS:
                        _is_approved = True
                    elif resolution.lower() in _BARE_OPTION_LABELS:
                        # Bare "request changes" without feedback
                        _needs_revision = True
                        _revision_feedback = None
                    elif resolution:
                        # Free-text feedback
                        _needs_revision = True
                        _revision_feedback = resolution

                if _needs_revision and _revision_feedback is None:
                    # Bare request without actionable feedback — ask for specifics.
                    # This handles both legacy "request changes" and JSON
                    # {"action":"request_changes"} without feedback text.
                    logger.info(
                        "HITL gate: bare option label without feedback, requesting specifics",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        resolution=resolution,
                    )
                    # Extract a human-friendly label from the resolution for the
                    # follow-up prompt (avoid displaying raw JSON to the user).
                    try:
                        _parsed = json.loads(resolution)
                        display_resolution = (
                            _parsed.get("action", resolution).replace("_", " ")
                            if isinstance(_parsed, dict)
                            else resolution
                        )
                    except json.JSONDecodeError, TypeError, AttributeError:
                        display_resolution = resolution
                    followup = dq.queue_decision(
                        question=(
                            f'You selected "{display_resolution}" but didn\'t provide specific feedback. '
                            f"Please describe what changes you'd like to see in the {phase_label}, "
                            f"or approve to continue."
                        ),
                        context=draft_content,
                        options=["approve"],
                        decision_type="phase_gate",
                        phase=current_phase,
                    )
                    dq.wait_for_decision(followup.id)
                    resolved_followup = dq.get_decision(followup.id)
                    followup_resolution = (resolved_followup.resolution or "").strip()

                    # Parse follow-up resolution (also JSON-first)
                    try:
                        fp = json.loads(followup_resolution)
                        if isinstance(fp, dict) and "action" in fp:
                            fa = fp["action"]
                            if fa == "approve":
                                _is_approved = True
                                _needs_revision = False
                            elif fa in ("request_changes", "change_approach"):
                                ft = fp.get("feedback", "")
                                if ft:
                                    _revision_feedback = ft
                                else:
                                    _is_approved = True
                                    _needs_revision = False
                            else:
                                raise json.JSONDecodeError("unknown", followup_resolution, 0)
                        else:
                            raise json.JSONDecodeError("no action", followup_resolution, 0)
                    except json.JSONDecodeError, TypeError, AttributeError:
                        if (
                            followup_resolution.lower() in _APPROVE_KEYWORDS
                            or followup_resolution.lower() in _BARE_OPTION_LABELS
                        ):
                            logger.info(
                                "HITL follow-up: no actionable feedback, treating as approval",
                                pipeline_id=pipeline_id,
                                phase=current_phase,
                            )
                            _is_approved = True
                            _needs_revision = False
                        elif followup_resolution:
                            _revision_feedback = followup_resolution

                if _needs_revision and _revision_feedback:
                    # Human provided feedback — re-run the phase with corrections
                    logger.info(
                        "HITL gate: changes requested, re-running phase",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        feedback_preview=_revision_feedback[:200],
                    )
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        pipeline.status = PipelineStatus.RUNNING
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        phase_execution.status = PipelineStatus.RUNNING
                        phase_execution.completed_at = None  # Reset — phase is re-running
                        phase_execution.hitl_review_cycles += 1

                        # Circuit breaker: don't allow unbounded HITL revision loops.
                        # Uses a dedicated counter so agentic review cycles don't
                        # consume the human's revision budget.
                        max_hitl_cycles = pipeline.config.max_hitl_review_cycles
                        if phase_execution.hitl_review_cycles >= max_hitl_cycles:
                            logger.warning(
                                "HITL revision circuit breaker — advancing despite feedback",
                                pipeline_id=pipeline_id,
                                phase=current_phase,
                                hitl_review_cycles=phase_execution.hitl_review_cycles,
                                max_hitl_review_cycles=max_hitl_cycles,
                            )
                            store.save_pipeline(pipeline)
                            # Fall through to the approval path below
                        else:
                            # Store feedback so the re-running agents receive it.
                            phase_execution.hitl_feedback = _revision_feedback

                            # Reset containers/agents/artifacts so the re-run
                            # starts clean, resetting the same container/agent/
                            # artifact fields that the recovery path resets.
                            phase_execution.containers = []
                            phase_execution.agents = []
                            phase_execution.artifacts = {}
                            phase_execution.review_cycles = 0

                            # Clear message store and consensus tracker so the
                            # re-run doesn't short-circuit on stale CONSENSUS_CONFIRMED
                            # messages from the previous run (issue #1296).
                            from routes.phases import _clear_concurrent_state

                            _clear_concurrent_state(pipeline_id)

                            store.save_pipeline(pipeline)
                            report_pipeline_status(
                                pipeline,
                                event_type="phase.revision_requested",
                                message=f"Human requested changes to {current_phase.value}",
                            )
                            _emit_pipeline_event(pipeline, "phase.revision_requested")
                            continue  # Re-enter outer loop → re-run phase with feedback

                # Before advancing, surface any contract-scoped decisions /
                # feedback the phase's agents registered via ``egg-contract``.
                # Without this bridge, approving the phase_gate silently
                # discards them (#1889).  Wrapped in try/except so a bug
                # here can never strand the pipeline.
                try:
                    _queue_and_await_contract_decisions(
                        dq,
                        worktree_repo_path,
                        pipeline_id,
                        _pipeline_identifier(pipeline.issue_number, pipeline_id),
                        current_phase,
                    )
                except Exception as bridge_err:
                    logger.warning(
                        "Contract decision bridge failed (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(bridge_err),
                    )

                # Approved — resume and advance
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.RUNNING
                    # Restore phase status to COMPLETE now that the HITL gate is cleared
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.COMPLETE
                    if phase_execution.completed_at is None:
                        phase_execution.completed_at = datetime.now(UTC)
                    store.save_pipeline(pipeline)

                # Persist phase gate resolution to contract and draft so
                # next-phase agents can see the human's decisions.  #1295
                _persist_phase_gate_resolution(
                    worktree_repo_path,
                    pipeline_id,
                    resolved_decision,
                    current_phase.value,
                    pipeline.issue_number,
                )

                # Commit and push updated statefiles (contract + draft with resolution)
                try:
                    _commit_statefiles_to_worktree(
                        worktree_repo_path,
                        f"Persist HITL resolution after {current_phase.value} phase gate",
                        pipeline_identifier=_pipeline_identifier(
                            pipeline.issue_number, pipeline_id
                        ),
                        pipeline_id=pipeline_id,
                    )
                except Exception as git_err:
                    # Catch broadly: see #2219.  The helper raises
                    # ``TimeoutExpired`` and ``OSError`` paths that a
                    # ``CalledProcessError``-only handler did not catch.
                    logger.warning(
                        "Failed to commit statefiles after phase gate resolution (continuing)",
                        pipeline_id=pipeline_id,
                        error=str(git_err),
                    )

                if pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                            mode=gateway_mode,
                            base_branch=pipeline.base_branch,
                        )
                    except Exception as push_err:
                        logger.warning(
                            "Failed to push statefiles after phase gate resolution (continuing)",
                            pipeline_id=pipeline_id,
                            error=str(push_err),
                        )

            # Tear down the phase-scoped overseer before advancing.
            # Each phase gets a fresh overseer instance — no state carries
            # over between phases.
            # Hold the lock to prevent the poll thread from seeing the
            # container as EXITED and respawning it.
            with overseer_lock:
                if overseer_container_id and phase_overseer_active:
                    phase_overseer_active = False
                    _teardown_phase_overseer(
                        spawner,
                        overseer_container_id,
                        pipeline_id,
                        phase_label=current_phase.value,
                        reason="phase ended",
                    )

            # Determine next phase
            next_phases = transitions.get(current_phase, [])

            # CUSTOM-mode pipelines run exactly one phase and then
            # terminate — no auto-advance (#1762 TASK-2-9 / decision-9).
            # Mirrors BABYSIT's effective single-phase semantics (BABYSIT
            # starts at IMPLEMENT and the PR step is a no-op).
            _is_custom_mode = getattr(pipeline, "mode", None) == PipelineMode.CUSTOM

            if not next_phases or _is_custom_mode:
                # Terminal phase — pipeline complete
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.COMPLETE
                    store.save_pipeline(pipeline, force_commit=(pipeline.issue_number is None))

                # Report pipeline completion to collaborator
                report_pipeline_status(
                    pipeline,
                    event_type="pipeline.completed",
                    message="Pipeline completed successfully",
                )
                _emit_pipeline_event(pipeline, "pipeline.completed")
                logger.info(
                    "Pipeline complete",
                    pipeline_id=pipeline_id,
                    custom_mode=_is_custom_mode,
                )
                break

            # TEST_MARKER: auto_advance_block (load-bearing: brackets the
            # block for TestAutoAdvanceRespawnsThread; do not remove without
            # updating that test class).
            # Advance to next phase by respawning a fresh _run_pipeline
            # thread, mirroring advance_phase (#2165).  Bumping run_epoch
            # makes this thread's finally cleanup detect itself as superseded
            # and skip worktree teardown; the new thread drives the next
            # phase from clean local state.  Without this, any exception in
            # the new phase's first iteration takes the whole pipeline down.
            next_phase = next_phases[0]
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.current_phase = next_phase
                pipeline.run_epoch = datetime.now(UTC)
                # ``updated_at`` is unconditionally set by ``StateStore.save_pipeline``.
                store.save_pipeline(pipeline, force_commit=(pipeline.issue_number is None))

            # Drop the previous phase's in-memory consensus tracker and
            # message-store entries (#2502).  The other phase-transition
            # paths -- ``advance_phase`` REST handler, HITL-revision
            # re-run, and the ``recover_pipeline`` resume path -- all
            # call this; the auto-advance path used to skip it, leaving
            # a stale plan-phase tracker keyed under the bare
            # ``pipeline_id`` for ``_get_concurrent_status`` to find and
            # report as ``is_complete: True`` long after the implement
            # phase had started.  ``_write_brc_history`` already ran
            # earlier in this iteration (line ~16753) so the BRC
            # transcript is already on disk by the time we wipe the
            # message store here.
            from routes.phases import _clear_concurrent_state

            _clear_concurrent_state(pipeline_id)

            logger.info(
                "Phase advanced (auto), respawning driver thread",
                pipeline_id=pipeline_id,
                from_phase=current_phase.value,
                to_phase=next_phase.value,
            )

            _spawn_pipeline_run_thread(pipeline_id, repo_path, pipeline.run_epoch)
            return

    except PipelineNotFoundError as pnf_err:
        # `PipelineNotFoundError` can be raised either because the pipeline
        # was actually deleted or because of a transient state-store read
        # (e.g., empty content while a concurrent commit on the state
        # worktree races with the read).  Re-verify before treating it as
        # deletion: if the pipeline is still on disk after retry, the
        # original exception was spurious — bump ``run_epoch`` so the
        # finally cleanup detects this thread as superseded and skips the
        # destructive worktree teardown, then relaunch ``_run_pipeline`` so
        # the next phase keeps making progress.  See #2155.
        pipeline_still_exists = False
        _verify_store = None
        try:
            _verify_store = get_state_store(repo_path)
        except Exception as verify_store_err:
            # Couldn't even open the state store — treat as transient
            # (corrupt-but-present > deletion) so we skip the respawn
            # rather than amplifying an infrastructure blip.  Note: with
            # ``_verify_store=None`` the bump path below short-circuits,
            # so worktree preservation depends on whether ``run_epoch``
            # was set before the initial PNFE — this path avoids the
            # cascade but does not unconditionally preserve worktrees.
            logger.warning(
                "Failed to obtain state store after PipelineNotFoundError; "
                "treating as transient infrastructure failure and skipping respawn",
                pipeline_id=pipeline_id,
                error=str(verify_store_err),
            )
            pipeline_still_exists = True

        if _verify_store is not None:
            for _attempt in range(_PNFE_VERIFY_ATTEMPTS):
                time.sleep(_PNFE_VERIFY_INTERVAL)
                try:
                    _verify_store.load_pipeline(pipeline_id)
                    pipeline_still_exists = True
                    break
                except PipelineNotFoundError:
                    continue
                except StateValidationError:
                    # Corrupt JSON or schema mismatch means the file
                    # exists but is unreadable right now — that's not
                    # deletion.  Treat as transient: better to risk a
                    # wasted respawn than to nuke the worktrees on a
                    # transient corruption.
                    pipeline_still_exists = True
                    break
                except StateStoreError as verify_err:
                    # Other state-store failures (transient git read
                    # errors, etc.) are also not evidence of deletion.
                    logger.warning(
                        "State-store error verifying pipeline existence; "
                        "treating as transient and preserving worktrees",
                        pipeline_id=pipeline_id,
                        error=str(verify_err),
                    )
                    pipeline_still_exists = True
                    break

        if pipeline_still_exists:
            # Cap the respawn cascade so a persistent transient can't
            # leak threads, overseer containers, and state-branch
            # commits without bound.  The recovery code is what runs
            # exactly when the system is misbehaving — it must not
            # amplify the misbehaviour.
            if _respawn_attempt >= _PNFE_RESPAWN_MAX_ATTEMPTS:
                logger.error(
                    "Spurious-PipelineNotFoundError recovery exhausted "
                    "respawn budget; marking pipeline FAILED so an "
                    "operator can investigate via restart_phase",
                    pipeline_id=pipeline_id,
                    attempts=_respawn_attempt,
                    exc_info=pnf_err,
                )
                if _verify_store is not None:
                    try:
                        with get_pipeline_state_lock(pipeline_id):
                            _failed_pipeline = _verify_store.load_pipeline(pipeline_id)
                            _failed_pipeline.status = PipelineStatus.FAILED
                            _failed_pipeline.error = (
                                "Transient PipelineNotFoundError recovery "
                                f"exhausted after {_respawn_attempt} respawns"
                            )
                            _verify_store.save_pipeline(_failed_pipeline)
                    except Exception as fail_err:
                        logger.warning(
                            "Failed to mark pipeline FAILED after exhausting respawn budget",
                            pipeline_id=pipeline_id,
                            error=str(fail_err),
                        )
            else:
                # Recoverable transient — log at warning so it doesn't
                # trip error-rate dashboards every time it self-heals.
                logger.warning(
                    "Spurious PipelineNotFoundError during execution — "
                    "pipeline still exists after retry; relaunching driver "
                    "thread and preserving worktrees",
                    pipeline_id=pipeline_id,
                    attempt=_respawn_attempt,
                    exc_info=pnf_err,
                )
                # Bump run_epoch so the finally cleanup observes this
                # thread as superseded (mirrors the advance_phase
                # pattern) and skips worktree teardown.  Capture the
                # pre-bump epoch into the local ``run_epoch`` so the
                # finally guard works even when the *initial* load
                # raised PNFE (in that case run_epoch was never set
                # at line 11393).
                bump_succeeded = False
                if _verify_store is not None:
                    try:
                        with get_pipeline_state_lock(pipeline_id):
                            _bumped = _verify_store.load_pipeline(pipeline_id)
                            run_epoch = _bumped.run_epoch or _bumped.created_at
                            _bumped.run_epoch = datetime.now(UTC)
                            _verify_store.save_pipeline(_bumped)
                            bump_succeeded = True
                    except Exception as bump_err:
                        logger.warning(
                            "Failed to bump run_epoch during spurious-PNFE "
                            "recovery; skipping respawn so the existing "
                            "finally cleanup runs without racing a new thread",
                            pipeline_id=pipeline_id,
                            error=str(bump_err),
                        )

                if bump_succeeded:
                    # Exponential backoff between respawn attempts so a
                    # tight cascade can't fire dozens of respawns per
                    # second.  attempt=0 → 1s, 1 → 2s, 2 → 4s, 3 → 8s,
                    # 4 → 16s, capped at _PNFE_RESPAWN_BACKOFF_CAP.
                    _backoff = min(2**_respawn_attempt, _PNFE_RESPAWN_BACKOFF_CAP)
                    time.sleep(_backoff)
                    threading.Thread(
                        target=_run_pipeline,
                        args=(pipeline_id, repo_path),
                        kwargs={"_respawn_attempt": _respawn_attempt + 1},
                        daemon=True,
                        name=(
                            f"pipeline-{pipeline_id}-respawn-"
                            f"{_respawn_attempt + 1}-{time.monotonic_ns()}"
                        ),
                    ).start()
        else:
            logger.info(
                "Pipeline was deleted during execution, exiting",
                pipeline_id=pipeline_id,
                exc_info=pnf_err,
            )
    except Exception as e:
        logger.error(
            "Pipeline execution failed", pipeline_id=pipeline_id, error=str(e), exc_info=True
        )
        persisted_ok = False
        try:
            store = get_state_store(repo_path)
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)

                # Don't corrupt a recreated pipeline's state
                _fail_epoch = pipeline.run_epoch or pipeline.created_at
                if run_epoch and _fail_epoch != run_epoch:
                    logger.info(
                        "Pipeline was recreated, not marking new run as failed",
                        pipeline_id=pipeline_id,
                    )
                else:
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.error = str(e)
                    store.save_pipeline(pipeline)
                    persisted_ok = True

                # Report pipeline failure to collaborator
                report_pipeline_status(
                    pipeline,
                    event_type="pipeline.failed",
                    message=f"Pipeline failed: {str(e)[:100]}",
                )
                _emit_pipeline_event(pipeline, "pipeline.failed")
        except Exception as fail_err:
            # If FAILED-marking itself fails (state-store contention, lock
            # timeout, etc.), the pipeline stays at ``running`` with no
            # error recorded — exactly the silent-wedge symptom in #2219.
            # Log so the next occurrence is visible in the orchestrator
            # log instead of vanishing.
            logger.error(
                "Failed to mark pipeline FAILED after exception",
                pipeline_id=pipeline_id,
                original_error=str(e),
                mark_error=str(fail_err),
                exc_info=True,
            )
            # Surface a synthetic ``pipeline.failed`` to the EventBus even
            # though the mark-FAILED block raised.  Without this, hosts
            # blocked on ``/status/wait`` (whose event allowlist requires
            # pipeline.failed/completed/cancelled) wait forever on a dead
            # runner — the zombie symptom in #2234.  ``persisted`` carries
            # whether ``save_pipeline`` actually flushed FAILED to disk
            # before the inner block raised: True means disk state matches
            # the event, False means consumers should treat the event as
            # the only authoritative source.
            if _emit_event is not None:
                try:
                    _emit_event(
                        EventType.PIPELINE_FAILED,
                        pipeline_id,
                        data={
                            "status": PipelineStatus.FAILED.value,
                            "persisted": persisted_ok,
                            "original_error": str(e),
                            "mark_error": str(fail_err),
                        },
                    )
                except Exception as emit_err:
                    logger.warning(
                        "Failed to emit synthetic pipeline.failed event",
                        pipeline_id=pipeline_id,
                        error=str(emit_err),
                    )
    finally:
        # Stop health monitor polling and unsubscribe from events
        if health_monitor_timer is not None:
            health_monitor_timer.set()
        if poll_thread is not None:
            poll_thread.join(timeout=5)
        if health_monitor_instance is not None:
            try:
                health_monitor_instance.stop()
                logger.info("Health monitor stopped", pipeline_id=pipeline_id)
            except Exception as hm_stop_err:
                logger.debug(
                    "Failed to stop health monitor",
                    pipeline_id=pipeline_id,
                    error=str(hm_stop_err),
                )

        # Clean up progress store for this pipeline
        try:
            from progress_store import get_progress_store

            progress_store = get_progress_store()
            if progress_store is not None:
                progress_store.clear(pipeline_id)
        except Exception as ps_err:
            logger.debug(
                "Failed to clear progress store",
                pipeline_id=pipeline_id,
                error=str(ps_err),
            )

        # Stop overseer container if it was spawned
        if overseer_container_id:
            try:
                _spawner = _get_spawner()
                _spawner.stop_agent_job(
                    overseer_container_id,
                    cleanup_session=True,
                    timeout=10,
                )
                logger.info(
                    "Overseer container stopped",
                    pipeline_id=pipeline_id,
                    container_id=overseer_container_id[:12],
                )
            except Exception as overseer_err:
                logger.debug(
                    "Failed to stop overseer container (may have already exited)",
                    pipeline_id=pipeline_id,
                    error=str(overseer_err),
                )

        # Clean up pipeline-level worktrees unless the pipeline has been
        # recreated (delete + create with the same ID).  In that case the
        # new run owns the worktrees and we must not remove them.
        try:
            _spawner = _get_spawner()
            _store = get_state_store(repo_path)
            skip_cleanup = False
            pipeline_was_restarted = False
            try:
                current = _store.load_pipeline(pipeline_id)
                _cleanup_epoch = current.run_epoch or current.created_at
                if run_epoch and _cleanup_epoch != run_epoch:
                    skip_cleanup = True
                    pipeline_was_restarted = True
                    logger.info(
                        "Pipeline was recreated/restarted, skipping worktree cleanup",
                        pipeline_id=pipeline_id,
                        old_epoch=run_epoch.isoformat(),
                        new_epoch=_cleanup_epoch.isoformat(),
                    )
                elif current.status == PipelineStatus.FAILED:
                    skip_cleanup = True
                    logger.info(
                        "Pipeline failed, preserving worktrees for retry",
                        pipeline_id=pipeline_id,
                    )
            except Exception:
                # Pipeline was deleted and not recreated — safe to clean up
                pass

            if not skip_cleanup:
                try:
                    _spawner.gateway.delete_worktrees(
                        container_id=pipeline_id,
                        force=True,
                    )
                    logger.info("Pipeline worktrees cleaned up", pipeline_id=pipeline_id)
                except Exception as pipeline_wt_err:
                    logger.warning(
                        "Failed to clean up pipeline worktrees",
                        pipeline_id=pipeline_id,
                        error=str(pipeline_wt_err),
                    )

                # Also clean up per-agent session worktrees.  Each agent
                # registers a gateway session under container_id
                # "egg-{pipeline_id}-{role}" and session_create creates a
                # worktree keyed to that name.  The per-agent cleanup path
                # calls delete_session_by_container with the Docker container
                # hash (not the session container_id), so those worktrees are
                # never removed via the normal per-container cleanup.  Sweep
                # them here as a safety net.  delete_worktrees is a no-op for
                # container IDs that have no worktree directory.
                #
                # NOTE: This uses the "egg-{pipeline_id}-{role}" naming for
                # session-created worktrees.  Per-agent worktrees from #1481
                # use "{pipeline_id}-{role}" (no "egg-" prefix) and are
                # cleaned up by cleanup_pipeline() which scans both container
                # labels and the filesystem.
                for role in AgentRole:
                    agent_container_id = f"egg-{pipeline_id}-{role.value}"
                    try:
                        _spawner.gateway.delete_worktrees(
                            container_id=agent_container_id,
                            force=True,
                        )
                    except Exception as agent_wt_err:
                        logger.warning(
                            "Failed to clean up agent worktrees",
                            pipeline_id=pipeline_id,
                            agent_container_id=agent_container_id,
                            error=str(agent_wt_err),
                        )

        except Exception as wt_err:
            logger.warning(
                "Failed to clean up worktrees",
                pipeline_id=pipeline_id,
                error=str(wt_err),
            )

        # Safety-net: clean up any orphaned containers for this pipeline.
        # If the pipeline failed during startup or cleanup timed out, Docker
        # containers may persist.  This is a no-op when no containers exist.
        # Skip when the pipeline was restarted (run_epoch changed) so the
        # new thread's containers are not killed.  See #1386, #1638.
        if not pipeline_was_restarted:
            try:
                # ``gateway_mode`` is the mode this pipeline ran under;
                # the auto-salvage hook needs it to push recovery refs
                # under the same policy (#2429 review).
                removed = _spawner.cleanup_pipeline(
                    pipeline_id,
                    force=True,
                    preserve_worktrees=skip_cleanup,
                    salvage_mode=gateway_mode,
                    salvage_base_branch=pipeline.base_branch,
                )
                if removed > 0:
                    logger.info(
                        "Safety-net cleanup removed orphaned containers",
                        pipeline_id=pipeline_id,
                        containers_removed=removed,
                    )
            except Exception as cleanup_err:
                logger.warning(
                    "Safety-net container cleanup failed",
                    pipeline_id=pipeline_id,
                    error=str(cleanup_err),
                )


@pipelines_bp.route("/<pipeline_id>/start", methods=["POST"])
@require_lifecycle_secret
def start_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Start pipeline execution.

    Spawns containers for each phase in sequence, advancing through
    the phase DAG until completion or failure. Runs in a background thread.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Pipeline started",
            "data": {
                "pipeline_id": "local-a1b2c3d4",
                "status": "running"
            }
        }
    """
    repo_path = get_repo_path()

    # Parse force / force_reason from body. ``force=true`` skips the
    # live-pod orphan guard before the phase reset (#2420). force_reason
    # is recorded in the structured warning log, mirroring the
    # complete_phase audit pattern.
    body = request.get_json(silent=True) or {}
    # Strict boolean — `body.get("force") is True` rather than
    # `bool(body.get("force"))` so non-boolean truthy values
    # (`"false"`, `[]`, `{}`, `1`) don't silently flip the predicate.
    force = body.get("force") is True
    force_reason = body.get("force_reason")
    if force_reason is not None and not isinstance(force_reason, str):
        return make_error_response(
            "force_reason must be a string",
            status_code=400,
            reason="invalid_force_reason",
        )
    if isinstance(force_reason, str) and not force_reason.strip():
        force_reason = None

    try:
        store, pipeline = _resolve_pipeline(pipeline_id, repo_path)
        # Use the store's repo_path so _run_pipeline operates on the correct directory
        repo_path = store.repo_path

        # Compute gateway mode for session operations in the recovery path
        _gw_mode, _gw_vis = _compute_gateway_mode(pipeline)

        if pipeline.status == PipelineStatus.RUNNING:
            return make_error_response(
                f"Pipeline {pipeline_id} is already running",
                status_code=409,
            )

        if pipeline.status == PipelineStatus.AWAITING_HUMAN:
            # No pending decisions — the polling thread died (e.g. restart)
            # but the human already resolved everything.  Recover based on
            # the latest phase_gate decision's resolution.
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)

                # Re-validate status after acquiring the lock — another
                # concurrent start_pipeline call may have already recovered
                # this pipeline.
                if pipeline.status != PipelineStatus.AWAITING_HUMAN:
                    return make_error_response(
                        f"Pipeline {pipeline_id} status changed to "
                        f"{pipeline.status.value} (concurrent recovery)",
                        status_code=409,
                    )

                pending = pipeline.get_pending_decisions()
                if len(pending) > 0:
                    return make_error_response(
                        f"Pipeline {pipeline_id} is awaiting human approval "
                        f"({len(pending)} pending decision(s))",
                        status_code=409,
                    )

                # Find the latest resolved phase_gate decision
                phase_gate_decisions = [
                    d
                    for d in reversed(pipeline.decisions)
                    if d.decision_type == "phase_gate" and d.status.value == "resolved"
                ]
                latest_resolution = (
                    phase_gate_decisions[0].resolution if phase_gate_decisions else None
                )

                # Determine if approved or request_changes using the shared
                # parser (handles approve, select, submit_feedback,
                # request_changes, change_approach, and legacy bare strings).
                is_approved, revision_feedback = _parse_resolution(latest_resolution)

                if is_approved:
                    # Mark current phase COMPLETE and advance
                    phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
                    phase_execution.status = PipelineStatus.COMPLETE
                    if phase_execution.completed_at is None:
                        phase_execution.completed_at = datetime.now(UTC)

                    # Persist phase gate resolution so next-phase agents see it.  #1295
                    #
                    # The contract and phase draft both live under the
                    # per-pipeline worktree (``<worktree>/.egg-state/``),
                    # not the orchestrator's main repo. Resolve the
                    # worktree explicitly here — the inline path inside
                    # ``_run_pipeline`` already has ``worktree_repo_path``
                    # in scope, but this recovery branch only has the
                    # main ``repo_path``. Passing ``repo_path`` would
                    # silently no-op the contract write and draft append
                    # (#2357, same shape as #2345).
                    if phase_gate_decisions:
                        worktree_repo_path = _resolve_pipeline_worktree_path(pipeline, repo_path)
                        if worktree_repo_path == repo_path:
                            # No materialised worktree — recovery degrades to
                            # the pre-fix shape (contract write typically
                            # no-ops via ContractNotFoundError, draft append
                            # skipped). The contract write *may* succeed if
                            # the orchestrator's main repo happens to carry a
                            # contract for this pipeline, but it would land
                            # against the wrong tree. Surface this either way
                            # so operators can correlate missing next-phase
                            # context with worktree-cleanup races.
                            logger.warning(
                                "No materialised worktree found for phase gate "
                                "persistence; falling back to main repo path. "
                                "Contract write may silently no-op.",
                                pipeline_id=pipeline_id,
                                phase=pipeline.current_phase.value,
                            )
                        _persist_phase_gate_resolution(
                            worktree_repo_path,
                            pipeline_id,
                            phase_gate_decisions[0],
                            pipeline.current_phase.value,
                            pipeline.issue_number,
                        )

                        # Commit statefiles so worktrees created by _run_pipeline
                        # include the contract/draft changes.
                        try:
                            _commit_statefiles_to_worktree(
                                worktree_repo_path,
                                f"Persist HITL resolution after {pipeline.current_phase.value} phase gate",
                                pipeline_identifier=_pipeline_identifier(
                                    pipeline.issue_number, pipeline_id
                                ),
                                pipeline_id=pipeline_id,
                            )
                        except Exception as git_err:
                            # Catch broadly: see #2219.  The helper raises
                            # ``TimeoutExpired`` and ``OSError`` paths that a
                            # ``CalledProcessError``-only handler did not catch.
                            logger.warning(
                                "Failed to commit statefiles after phase gate resolution (continuing)",
                                pipeline_id=pipeline_id,
                                error=str(git_err),
                            )

                        # Push if this repo tracks a remote branch and a
                        # worktree was materialised. Mirrors the inline
                        # path's guard at pipelines.py:16044 — pushing from
                        # the orchestrator's main repo would target the
                        # wrong working tree.
                        if pipeline.branch and worktree_repo_path != repo_path:
                            try:
                                _spawner = _get_spawner()
                                _spawner.gateway.push_worktree_branch(
                                    pipeline_id=pipeline_id,
                                    repo_path=str(worktree_repo_path),
                                    branch=pipeline.branch,
                                    mode=_gw_mode,
                                    base_branch=pipeline.base_branch,
                                )
                            except Exception as push_err:
                                logger.warning(
                                    "Failed to push statefiles after phase gate resolution (continuing)",
                                    pipeline_id=pipeline_id,
                                    error=str(push_err),
                                )

                    from routes.phases import PHASE_TRANSITIONS

                    transitions = PHASE_TRANSITIONS
                    current_phase = pipeline.current_phase
                    next_phases = transitions.get(current_phase, [])
                    # CUSTOM-mode pipelines complete after their single
                    # phase — no auto-advance (#1762 TASK-2-9).
                    _is_custom_mode = getattr(pipeline, "mode", None) == PipelineMode.CUSTOM

                    if not next_phases or _is_custom_mode:
                        # Terminal phase — pipeline complete.
                        # Bump run_epoch so any lingering old _run_pipeline
                        # thread (e.g. stuck in its finally block) detects the
                        # recreation and exits without double-cleaning up.
                        pipeline.status = PipelineStatus.COMPLETE
                        pipeline.run_epoch = datetime.now(UTC)
                        store.save_pipeline(pipeline)
                        return make_success_response(
                            "Pipeline recovered and completed",
                            data={
                                "pipeline_id": pipeline_id,
                                "status": "complete",
                                "current_phase": pipeline.current_phase.value,
                            },
                        )

                    # Advance to next phase
                    next_phase = next_phases[0]
                    pipeline.current_phase = next_phase

                    # Update health monitor phase threshold before agents spawn
                    try:
                        from health_monitor import get_health_monitor

                        _hm_instance = get_health_monitor()
                        if _hm_instance is not None:
                            _hm_instance.set_current_phase(next_phase.value)
                    except ImportError:
                        pass

                else:
                    # request_changes/change_approach — reset phase for re-run
                    phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
                    if phase_execution.status in (
                        PipelineStatus.COMPLETE,
                        PipelineStatus.FAILED,
                        PipelineStatus.RUNNING,
                        PipelineStatus.AWAITING_HUMAN,
                    ):
                        # Refuse to clear containers/agents/artifacts when
                        # pods labeled to this pipeline are still alive —
                        # the reset would orphan them (#2420).
                        guard = _guard_live_pods_or_force(pipeline_id, force, force_reason)
                        if guard is not None:
                            return guard
                        phase_execution.status = PipelineStatus.PENDING
                        phase_execution.started_at = None
                        phase_execution.work_started_at = None
                        phase_execution.completed_at = None
                        phase_execution.error = None
                        phase_execution.review_cycles = 0
                        phase_execution.hitl_review_cycles = 0
                        phase_execution.containers = []
                        phase_execution.agents = []
                        phase_execution.artifacts = {}

                    # Clear stale consensus state so re-run doesn't
                    # short-circuit (issue #1296).
                    from routes.phases import _clear_concurrent_state

                    _clear_concurrent_state(pipeline_id)

                    # Preserve the reviewer's feedback so the re-launched
                    # _run_pipeline thread can pass it to the agent.
                    if revision_feedback:
                        phase_execution.hitl_feedback = revision_feedback

                pipeline.error = None
                pipeline.run_epoch = datetime.now(UTC)
                pipeline.status = PipelineStatus.RUNNING
                store.save_pipeline(pipeline)

            # TEST_MARKER: recover_advance_clear (load-bearing: brackets
            # the post-lock clear for TestRecoverPipelineClearsConcurrentState;
            # do not remove without updating that test class).
            # Drop the previous phase's in-memory consensus tracker on
            # cross-phase advance (#2502).  The request_changes /
            # change_approach branch above already cleared inside the
            # lock for same-phase re-runs (#1296); the advance branch
            # needs its own post-lock clear so persisted state lands
            # before the tracker is wiped, matching the persist-then-
            # clear-then-spawn order used by ``advance_phase`` and the
            # auto-advance block.
            if is_approved:
                from routes.phases import _clear_concurrent_state

                _clear_concurrent_state(pipeline_id)

            # Launch runner thread
            thread = threading.Thread(
                target=_run_pipeline,
                args=(pipeline_id, repo_path),
                daemon=True,
                name=f"pipeline-{pipeline_id}",
            )
            thread.start()

            logger.info(
                "Pipeline recovered from AWAITING_HUMAN",
                pipeline_id=pipeline_id,
                recovery_action="advance" if is_approved else "rerun",
            )

            return make_success_response(
                "Pipeline recovered and started",
                data={
                    "pipeline_id": pipeline_id,
                    "status": "running",
                    "current_phase": pipeline.current_phase.value,
                },
            )

        if pipeline.status == PipelineStatus.COMPLETE:
            return make_error_response(
                f"Pipeline {pipeline_id} is already complete",
                status_code=409,
            )

        if pipeline.status == PipelineStatus.CANCELLED:
            return make_error_response(
                f"Pipeline {pipeline_id} is cancelled",
                status_code=409,
            )

        with get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

            if pipeline.status == PipelineStatus.FAILED:
                # Reset the failed phase so it can be re-run.
                # Also reset phases stuck in RUNNING — a pipeline-level exception
                # sets the pipeline to FAILED without updating the phase status.
                phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
                if phase_execution.status in (PipelineStatus.FAILED, PipelineStatus.RUNNING):
                    # Refuse to clear containers/agents/artifacts when pods
                    # labeled to this pipeline are still alive — the reset
                    # would orphan them (#2420).
                    guard = _guard_live_pods_or_force(pipeline_id, force, force_reason)
                    if guard is not None:
                        return guard
                    prev_status = phase_execution.status.value
                    phase_execution.status = PipelineStatus.PENDING
                    phase_execution.started_at = None
                    phase_execution.work_started_at = None
                    phase_execution.completed_at = None
                    phase_execution.error = None
                    phase_execution.review_cycles = 0
                    phase_execution.hitl_review_cycles = 0
                    phase_execution.containers = []
                    phase_execution.agents = []
                    phase_execution.artifacts = {}
                    logger.info(
                        "Resetting phase for restart",
                        pipeline_id=pipeline_id,
                        phase=pipeline.current_phase.value,
                        previous_phase_status=prev_status,
                    )
                pipeline.error = None

                # Bump run_epoch so the old _run_pipeline thread's finally block
                # detects the restart and skips worktree cleanup.
                pipeline.run_epoch = datetime.now(UTC)

            # Mark pipeline as running
            pipeline.status = PipelineStatus.RUNNING
            store.save_pipeline(pipeline)

        # Run the pipeline in a background thread
        thread = threading.Thread(
            target=_run_pipeline,
            args=(pipeline_id, repo_path),
            daemon=True,
            name=f"pipeline-{pipeline_id}",
        )
        thread.start()

        logger.info("Pipeline started", pipeline_id=pipeline_id)

        return make_success_response(
            "Pipeline started",
            data={
                "pipeline_id": pipeline_id,
                "status": "running",
                "current_phase": pipeline.current_phase.value,
            },
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


@pipelines_bp.route("/<pipeline_id>/visualization", methods=["GET"])
def get_pipeline_visualization(pipeline_id: str) -> tuple[Response, int]:
    """
    Get pipeline DAG visualization.

    URL params:
        pipeline_id: Pipeline ID

    Query params:
        format: Output format - "full" (default), "compact", "text", "json"
        ascii: Use ASCII-only characters (default: false)

    Response:
        {
            "success": true,
            "data": {
                "pipeline_id": "issue-123",
                "visualization": {
                    "dag": "...",  // Full DAG visualization
                    "compact": "...",  // Single-line status
                    "progress": "..."  // Progress bar
                },
                "phases": {...},  // Phase status summary
                "status": "running",
                "current_phase": "implement"
            }
        }
    """
    # Check if visualization module is available (imported at module level)
    if not _DAG_VISUALIZER_AVAILABLE:
        return make_error_response(
            "Visualization module not available",
            status_code=500,
        )

    repo_path = get_repo_path()
    output_format = request.args.get("format", "full")
    use_ascii = request.args.get("ascii", "false").lower() == "true"

    try:
        _store, pipeline = _resolve_pipeline(pipeline_id, repo_path)

        if output_format == "json":
            # Return structured JSON report
            report = generate_status_report(pipeline, use_ascii=use_ascii)
            return make_success_response(
                "Visualization generated",
                data=report,
            )

        elif output_format == "text":
            # Return plain text DAG
            dag_text = render_pipeline_dag(pipeline, use_ascii=use_ascii)
            return Response(
                dag_text,
                mimetype="text/plain",
                status=200,
            )

        elif output_format == "compact":
            # Return compact single-line status
            compact = render_compact_status(pipeline, use_ascii=use_ascii)
            progress = render_progress_bar(pipeline, use_ascii=use_ascii)
            return make_success_response(
                "Visualization generated",
                data={
                    "pipeline_id": pipeline.id,
                    "compact": compact,
                    "progress": progress,
                    "status": pipeline.status.value,
                    "current_phase": pipeline.current_phase.value,
                },
            )

        else:
            # Full format with all visualizations
            report = generate_status_report(pipeline, use_ascii=use_ascii)
            return make_success_response(
                "Visualization generated",
                data=report,
            )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


@pipelines_bp.route("/stream", methods=["GET"])
def stream_all_pipelines() -> Response:
    """
    Stream unified events for all pipelines via Server-Sent Events (SSE).

    Provides real-time updates for ALL pipeline state changes in a single
    SSE connection. Unlike the per-pipeline stream, terminal events for
    individual pipelines do not end the stream.

    Query params:
        ascii: Use ASCII-only characters (default: false)
        active_only: Only include active pipelines (default: true)
        full_dag: Include full DAG visualization (default: false)

    Response:
        text/event-stream with the following event types:
        - snapshot: Initial state of all active pipelines
        - pipeline.*: Pipeline lifecycle events
        - phase.*: Phase transition events
        - agent.*: Agent lifecycle events
        - decision.*: HITL decision events
        - done: Stream is ending (timeout)
    """
    if not _UNIFIED_SSE_AVAILABLE:
        return make_error_response(
            "Unified SSE streaming module not available",
            status_code=500,
        )

    use_ascii = request.args.get("ascii", "false").lower() == "true"
    active_only = request.args.get("active_only", "true").lower() == "true"
    full_dag = request.args.get("full_dag", "false").lower() == "true"

    repo_path = get_repo_path()

    return Response(
        stream_with_context(
            create_unified_sse_stream(
                repo_path=repo_path,
                use_ascii=use_ascii,
                active_only=active_only,
                full_dag=full_dag,
            )
        ),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@pipelines_bp.route("/<pipeline_id>/stream", methods=["GET"])
def stream_pipeline(pipeline_id: str) -> Response:
    """
    Stream pipeline events via Server-Sent Events (SSE).

    Provides real-time updates for pipeline state changes including
    phase transitions, agent lifecycle, and DAG visualization.

    URL params:
        pipeline_id: Pipeline ID

    Query params:
        ascii: Use ASCII-only characters (default: false)

    Response:
        text/event-stream with the following event types:
        - snapshot: Initial pipeline state
        - pipeline.*: Pipeline lifecycle events
        - phase.*: Phase transition events
        - agent.*: Agent lifecycle events
        - decision.*: HITL decision events
        - done: Stream is ending (terminal state or timeout)
        - error: An error occurred

    The stream automatically closes when the pipeline reaches a
    terminal state (completed, failed, cancelled) or after the
    maximum connection time (1 hour).
    """
    if not _SSE_AVAILABLE:
        return make_error_response(
            "SSE streaming module not available",
            status_code=500,
        )

    use_ascii = request.args.get("ascii", "false").lower() == "true"

    # Validate pipeline exists before starting stream
    repo_path = get_repo_path()
    try:
        _resolve_pipeline(pipeline_id, repo_path)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    return Response(
        stream_with_context(
            create_sse_stream(pipeline_id, repo_path=repo_path, use_ascii=use_ascii)
        ),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
