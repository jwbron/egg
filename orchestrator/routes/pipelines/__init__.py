"""
Pipeline CRUD endpoints for egg-orchestrator.
"""

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import (  # noqa: F401 — NamedTuple retained for _pkg base-class re-export (_populate)
    TYPE_CHECKING,
    Any,
    Literal,
    NamedTuple,
)
from uuid import uuid4

try:
    from docker.errors import DockerException
except ImportError:

    class DockerException(Exception):  # type: ignore[no-redef]
        pass


from flask import Blueprint, Response, jsonify, request, stream_with_context

# Re-export the slice-3 per-event prompt composer so callers can still
# import it via ``orchestrator.routes.pipelines.compose_event_prompt``
# (the contract assigns this file in TASK-3-1) even though the body
# lives in a sibling module to keep this file under the orchestrator
# decomposition cap (#2261). The slice-3 plan acceptance is satisfied
# by either import path; tests bind on
# ``orchestrator.routes.event_prompt`` directly.
from ..event_prompt import compose_event_prompt  # noqa: F401


# Closed enumeration of ``ContextPrCreationError.reason`` values
# (#2777). Producer and downstream tests (TASK-3-8) bind on these
# strings so a single source of truth avoids the synthetic-key
# divergence reviewer_code_holistic flagged. New reasons MUST be
# added here AND to ``ContextPrCreationReason`` so the type narrows.
class ContextPrCreationReason(StrEnum):
    """Closed set of typed reasons for :class:`ContextPrCreationError` (#2777)."""

    UNKNOWN = "unknown"
    # Lookup of the pipeline / store / spawner failed before any
    # gateway call could be attempted.
    PIPELINE_LOAD_FAILED = "pipeline_load_failed"
    ROUTES_UNAVAILABLE = "routes_unavailable"
    LOADER_UNAVAILABLE = "loader_unavailable"
    # Pipeline misconfiguration. ``base_branch`` left unset alongside a
    # ``repo`` is NOT a misconfiguration — it is the normal "auto-detect
    # the repo's default branch" state (#3031), so the opener resolves it
    # rather than raising. The remaining genuine misconfigurations are a
    # ``base_branch`` with no ``repo`` to open a PR against
    # (``missing_repo``) and a remote pipeline with no work branch
    # (``missing_branch``).
    MISSING_BRANCH = "missing_branch"
    MISSING_REPO = "missing_repo"
    # Contract / PR-metadata failures encountered after the pipeline
    # passed the misconfiguration check.
    CONTRACT_LOAD_FAILED = "contract_load_failed"
    MISSING_PR_METADATA = "missing_pr_metadata"
    SAVE_FAILED = "save_failed"
    # Gateway-layer failures wrapping ``lookup_open_pr`` /
    # ``create_pr`` outcomes.
    LOOKUP_FAILED = "lookup_failed"
    GATEWAY_ERROR = "gateway_error"
    GATEWAY_NO_URL = "gateway_no_url"
    GATEWAY_BAD_URL = "gateway_bad_url"


class ContextPrCreationError(Exception):
    """Raised by :func:`_open_context_pr_at_implement_start` when the
    hard-required up-front context PR cannot be opened (#2777, cq-4).

    Replaces the soft-fail ``return None`` swallow path that the legacy
    ``_maybe_open_base_pr_for_plan_to_implement`` wrapper used before
    slice-2 deleted it. Under cq-4 the context PR is hard-required at
    the plan→implement boundary; a gateway failure here must surface to
    the BRC NACK / 422 surface rather than silently strand the slice
    stack on ``/work``.

    Attributes:
        reason: Machine-readable reason drawn from
            :class:`ContextPrCreationReason`. Tests assert on these
            constants so producer and tests share one source of
            truth; passing an unknown string is a programming error
            caught here. The instance attribute is exposed as the
            underlying ``str`` value (matching ``.value`` of the
            enum) so existing JSON-serialization callers continue to
            work without change.
        cause: The original exception, if any, that triggered the
            error. Preserved so logs and the BRC NACK body show the
            gateway/contract failure rather than only this wrapper's
            text.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str | ContextPrCreationReason = ContextPrCreationReason.UNKNOWN,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        # Coerce-and-validate the reason against the closed
        # enumeration. Passing a string that is not a known reason
        # would normally raise ``ValueError`` from the ``StrEnum``
        # constructor — but the four ``except ContextPrCreationError``
        # handlers at every call site would not match that
        # ``ValueError``, so a typo would surface as a 500 instead of
        # the typed 422 the handlers contract on
        # (egg-reviewer non-blocking #4). Catch and coerce to
        # ``UNKNOWN`` so the typed-exception contract holds, and log
        # the bad reason loudly so the typo is still visible in the
        # operator's logs and CI grep — silent coercion would hide
        # the programming error.
        try:
            self.reason: str = ContextPrCreationReason(reason).value
        except ValueError:
            logger.warning(
                "ContextPrCreationError received unknown reason; "
                "coercing to UNKNOWN (#2777, egg-reviewer non-blocking #4)",
                bad_reason=repr(reason),
                error_message=message,
            )
            self.reason = ContextPrCreationReason.UNKNOWN.value
        self.cause: BaseException | None = cause


class ForestValidationError(Exception):
    """Raised by ``_populate_contract_from_plan`` on slice-DAG structural rejection.

    Added in #2137 (TASK-2-2) for the forest-shape violation (a slice
    with >1 DAG parent). Generalised in #3046 to also signal the
    file-overlap-ordering violation (two slices touching the same file
    with no dependency edge between them — see
    ``egg_contracts.validate_slice_file_overlap``). Both are slice-DAG
    structural defects surfaced at plan ingestion with identical
    handling: the slices are NOT written to the contract, the structured
    errors are stashed on ``plan_review_feedback`` so the plan reviewer
    NACKs the architect, and the exception is raised so HTTP callers can
    return a 422.

    The ``reason`` discriminator (``"forest_violation"`` or
    ``"slice_overlap_violation"``) selects the operator-facing prose and
    the :class:`PopulateOutcome` the safe wrapper maps to. Any future
    Flask route that ingests a plan in-band can catch this and
    ``body, status = err.to_response(); return jsonify(body), status``
    to surface the structured rejection. Internal callers
    (``_populate_contract_from_plan_safe`` and the pipeline run-loop
    helpers) catch it and log a warning — the ``plan_review_feedback``
    stash is the durable NACK signal either way.
    """

    def __init__(
        self, message: str, *, errors: list[str], reason: str = "forest_violation"
    ) -> None:
        super().__init__(message)
        self.errors: list[str] = list(errors)
        self.reason: str = reason
        self.status_code: int = 422

    def to_response(self) -> tuple[dict[str, object], int]:
        """Serialise into a Flask-compatible (body, status) tuple."""
        return ({"error": self.reason, "errors": self.errors}, 422)


class SliceCompletionInvariantError(RuntimeError):
    """Raised when a slice would be persisted ``COMPLETE`` without a valid
    completion basis (#3214).

    The #3214 wedge traced to an interior forest node (``slice-3`` on
    pipeline ``issue-3200``) persisted as ``SliceStatus.COMPLETE`` while
    its only task was still ``pending``, it had no integration branch, and
    it carried its *parent's* commit SHA. ``_persist_slice_status_complete``
    wrote that contradictory state with no validation, so the slice-DAG
    driver skipped real work and the chain wedged with no successor — and
    it hung ~9h silently because nothing failed loud at the moment of the
    bad write.

    A slice has a valid completion basis when ANY of these execution
    signals is present:

    * a slice PR is recorded / supplied (``pr_number``); or
    * the caller declares a verified ``basis`` — ``"merged"`` (the
      integration branch was ancestry-verified merged into its parent) or
      ``"consensus_complete"`` (BRC consensus reached, PR not yet opened
      or its URL unparseable); or
    * the slice forked an integration branch (``integration_base_sha`` is
      set — #2871); or
    * every task is ``TaskStatus.COMPLETE``.

    The predicate accepts any one signal so it can only flag the slice-3
    state where *all* are absent — a slice marked COMPLETE with zero
    evidence it ran. We raise here so that corrupt write fails loud at its
    source instead of wedging the forest a phase later.

    #3253 refinement: ``basis="merged"`` is no longer an unconditional
    pass. A merged slice went through a PR and left commits its producers
    recorded; a ``basis="merged"`` write with **no PR and no produced task
    commit** is an empty / never-implemented branch that origin ancestry
    mis-detected as merged (the slice-10 case — producers exhausted before
    committing, so the integration branch's tip is still its fork base and
    is trivially an ancestor of the advanced parent). Such a write is
    rejected so the slice is re-run rather than false-completed.
    """


# Completion bases a caller may declare when it has positive, verified
# evidence a slice finished even though not every task is marked COMPLETE
# on the contract (the crash-recovery / merged-skip paths). See
# :class:`SliceCompletionInvariantError`.
_VERIFIED_SLICE_COMPLETION_BASES = frozenset({"merged", "consensus_complete"})


def _slice_produced_commits(slice_obj: Any) -> bool:
    """Return True iff any of the slice's tasks recorded a commit SHA.

    This is the base-SHA-independent "a producer actually committed work"
    signal (#3253). It reads *task* commits only — a slice whose producers
    all failed before committing has every ``task.commit`` ``None`` (the
    AC-4 measurement in the issue-3200 slice-10 incident). It deliberately
    ignores ``Slice.commit``: that field can carry the *parent's* SHA on a
    false-complete (the #3214 slice-3 carryover), so it is not trustworthy
    evidence the slice itself produced anything.

    An empty integration branch (tip still at its fork base, so trivially
    an ancestor of an advanced parent) is indistinguishable from a merged
    one by origin ancestry alone once the recorded fork base is missing or
    stale (#3245). The contract's task-commit record is the durable signal
    that survives that ambiguity: no task commit + no slice PR ⇒ the slice
    never ran and must be re-run, not completed.

    A slice with *no tasks* returns ``False`` here (``any([])``). Paired with
    "origin-detected merged, no PR" that would force such a slice to re-run
    indefinitely — but a zero-task slice is unreachable in practice:
    plan-derived slices always carry at least one task. The safe direction is
    re-run over silently-dropped work, so the edge needs no special-casing
    (#3253).
    """
    tasks = getattr(slice_obj, "tasks", None) or []
    return any(getattr(t, "commit", None) for t in tasks)


def _validate_slice_completion_basis(
    slice_obj: Any,
    *,
    pr_number: int | None = None,
    basis: str | None = None,
) -> str | None:
    """Return ``None`` when ``slice_obj`` may legitimately be marked
    ``SliceStatus.COMPLETE``, else a human-readable reason it may not.

    Shared by the write chokepoint (``_persist_slice_status_complete``,
    which raises :class:`SliceCompletionInvariantError` on a reason) and
    the Layer-A bootstrap read-trust point (which alerts and declines to
    trust a contradictory contract-recorded COMPLETE rather than
    propagating it into the scheduler). See
    :class:`SliceCompletionInvariantError` for the basis rules (#3214).
    """
    has_pr = pr_number is not None or getattr(slice_obj, "pr_number", None) is not None
    # #3253 — a ``basis="merged"`` slice with no PR and no produced task
    # commits is not a merged slice; it is an empty / never-implemented
    # integration branch (tip still at its fork base) that origin ancestry
    # mis-detected as merged. A genuine merge went through a PR and left
    # commits the producers recorded. Reject so the restart re-runs the
    # slice instead of false-completing the pipeline with its work missing.
    # This guard fires *before* the verified-basis / forked free-passes
    # below so a recorded (possibly stale) fork base cannot rescue it.
    if basis == "merged" and not has_pr and not _slice_produced_commits(slice_obj):
        return (
            f"slice {getattr(slice_obj, 'id', '?')} would be marked COMPLETE "
            f"basis='merged' with no slice PR and no produced task commits — an "
            f"empty / never-implemented integration branch is not a merged one "
            f"(#3253)"
        )
    verified_basis = basis in _VERIFIED_SLICE_COMPLETION_BASES
    # A slice that actually forked its integration branch recorded a base
    # SHA (#2871). Its absence — together with no PR, no verified basis,
    # and no completed tasks — is the slice-3 false-complete signature: a
    # slice marked COMPLETE with zero evidence it ever ran. The predicate
    # accepts ANY single execution signal so it can only flag that
    # genuinely-contradictory state, never a legitimately-completed slice
    # whose other signals happen to be absent (e.g. an unparseable PR URL
    # leaves ``pr_number`` None but the slice still forked and reached
    # consensus). ``tasks_all_complete`` is the canonical model-side
    # predicate so this can't drift from the contract's own notion of
    # "work finished".
    forked = getattr(slice_obj, "integration_base_sha", None) is not None
    if has_pr or verified_basis or forked or slice_obj.tasks_all_complete:
        return None
    return (
        f"slice {getattr(slice_obj, 'id', '?')} would be marked COMPLETE with no "
        f"evidence it ran: no slice PR, no verified merge/consensus basis "
        f"(basis={basis!r}), no integration-branch fork base, and tasks not all "
        f"complete"
    )


# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Add config directory to path for repo_config module
_config_path = Path(__file__).parent.parent.parent.parent / "config"
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
    from ..gateway_client import (
        GatewayError,
        _rebase_with_agent_output_autoresolve,  # noqa: F401
    )
    from ..kubernetes_client import (
        LABEL_AGENT_ROLE,
        LABEL_PIPELINE_ID,
        LABEL_SLICE_ID,
        JobOperationError,
        KubernetesClientError,
        PodNotFoundError,
    )
    from ..kubernetes_spawner import KubernetesSpawnError, get_kubernetes_spawner
    from ..models import (
        LIVE_POD_STATUSES,
        AgentExecutionStatus,
        AgentExitInfo,
        AgentRole,
        ContainerInfo,
        ContainerStatus,
        CycleTiming,
        DecisionStatus,
        HITLDecision,
        IterationSummary,
        OperatorDirective,
        PhaseExecution,
        Pipeline,
        PipelineMode,
        PipelinePhase,
        PipelineStatus,
        RepoSpec,
    )
    from ..slice_id_validation import extract_slice_id
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
        get_container_spawner,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from decision_queue import get_decision_queue  # type: ignore
    from docker_client import (  # type: ignore
        ContainerNotFoundError,
        ContainerOperationError,
        DockerClientError,
    )
    from gateway_client import (  # type: ignore
        GatewayError,
        _rebase_with_agent_output_autoresolve,  # noqa: F401
    )
    from kubernetes_client import (  # type: ignore
        LABEL_AGENT_ROLE,
        LABEL_PIPELINE_ID,
        LABEL_SLICE_ID,
        JobOperationError,
        KubernetesClientError,
        PodNotFoundError,
    )
    from kubernetes_spawner import (  # type: ignore
        KubernetesSpawnError,
        get_kubernetes_spawner,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from models import (  # type: ignore
        LIVE_POD_STATUSES,
        AgentExecutionStatus,
        AgentExitInfo,
        AgentRole,
        ContainerInfo,
        ContainerStatus,
        CycleTiming,
        DecisionStatus,
        HITLDecision,  # noqa: F401 — retained for _pkg re-export / patch seam
        IterationSummary,
        OperatorDirective,
        PhaseExecution,
        Pipeline,
        PipelineMode,
        PipelinePhase,
        PipelineStatus,
        RepoSpec,
    )
    from slice_id_validation import extract_slice_id  # type: ignore
    from state_store import (  # type: ignore
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStore,
        StateStoreError,
        StateValidationError,
        get_pipeline_state_lock,
        get_state_store,
    )

from egg_contracts.orchestrator import load_agent_output, save_agent_output
from egg_git.default_branch import get_default_branch
from lifecycle_auth import require_lifecycle_secret

if TYPE_CHECKING:
    from egg_container import MountSpec
    from egg_contracts.agent_roles import AgentRole as ContractAgentRole

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


# ---------------------------------------------------------------------------
# Overseer authority plane (#2270 slice-6, §4) — the orchestrator-side seams the
# CorrectiveExecutor dispatches to. The overseer ADVISES (returns a verdict); the
# control plane EXECUTES exactly three bounded actions. Agents — including the
# overseer — cannot reach these directly: the gateway file patterns deny agents
# from contract writes (the "403"), and the executor only runs control-plane-side.
# The seams are invoked by CorrectiveExecutor with keyword arguments. See
# orchestrator/overseer/corrective.py and gateway/agent_restrictions.py.
# ---------------------------------------------------------------------------


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
    * ``egg/<id>`` → ``egg/<id>/work`` (issue submissions).
    * ``egg/<id>/work`` → unchanged (resubmission, internal callers).
    * non-``egg/`` (passed unchanged) — a pipeline pointed at a foreign
      branch (e.g. ``feature/foo``). Slices on a non-``egg/`` branch are
      not a guaranteed-safe shape and are intentionally not normalised
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
) -> int | str:
    """Derive the pipeline identifier used for namespaced .egg-state filenames.

    Prefers ``issue_number`` when available, falling back to ``pipeline_id``.

    A pipeline whose id carries a qualifier beyond the bare ``issue-<N>``
    form (e.g. ``issue-1557-v2`` for a versioned re-run) keys by
    ``pipeline_id`` instead, so concurrent pipelines on the same issue
    don't collide on ``.egg-state/drafts/<N>-analysis.md``.
    """
    if pipeline_id and issue_number is not None:
        expected_issue_prefix = f"issue-{issue_number}"
        if pipeline_id.startswith(expected_issue_prefix + "-"):
            # A qualifier is present beyond the bare ``issue-<N>`` form;
            # key by pipeline_id so concurrent runs on the same issue do
            # not collide on draft files.
            return pipeline_id
    return issue_number if issue_number is not None else pipeline_id


def _brc_history_identifier(pipeline) -> int | str:
    """Return the identifier used to namespace BRC-history artifacts.

    Mirrors :func:`_pipeline_identifier` (favouring the issue number).
    """
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


# Live-pod status filter (#2420). Hoisted to ``models.LIVE_POD_STATUSES``
# in #2650 so ``startup_reconciliation`` and this module can't drift;
# this alias preserves the historical underscore-prefixed name used by
# existing tests and prose references.
_LIVE_POD_STATUSES = LIVE_POD_STATUSES


from routes import get_repo_path  # noqa: E402 — shared helpers

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
        "pipeline.cancelled": EventType.PIPELINE_CANCELLED,
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


def _normalize_submission_repos(
    repos_arg: Any,
) -> tuple[str | None, list[dict[str, str | None]], str | None, str | None]:
    """Validate + normalize a multi-repo submission list (#3393).

    Accepts the ``repos`` payload from ``POST /api/v1/pipelines`` — a list of
    ``{repo, base_branch?, primary?}`` entries (a bare ``"owner/name"`` string
    is tolerated as ``{repo: ...}``). Returns
    ``(error, entries, primary_repo, primary_base_branch)``:

    * ``error`` — a human-readable message when validation fails (the other
      fields are meaningless in that case), else ``None``.
    * ``entries`` — normalized ``{"repo", "base_branch"}`` dicts, reordered so
      the primary is ``entries[0]`` (the ``Pipeline`` validator mirrors
      ``repos[0]`` onto the legacy singleton and ``primary_repo``).

    Per-entry repo/base_branch formats are validated with the same regexes the
    single-repo path uses. Same-name repos under different owners are NOT
    rejected here — they are distinct full ``owner/name`` slugs (operator
    ruling #6; the owner/repo re-key lands in slice 3).
    """
    if not isinstance(repos_arg, list) or not repos_arg:
        return ("repos must be a non-empty list of {repo, base_branch} entries", [], None, None)
    entries: list[dict[str, str | None]] = []
    primary_index = 0
    seen_primary = False
    for idx, raw in enumerate(repos_arg):
        entry = {"repo": raw} if isinstance(raw, str) else raw
        if not isinstance(entry, dict) or not entry.get("repo"):
            return (f"repos[{idx}] must be an object with a 'repo' field", [], None, None)
        repo_val = entry["repo"]
        if not re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$", repo_val):
            return (
                f"Invalid repo format in repos[{idx}]: {repo_val!r} (expected owner/name)",
                [],
                None,
                None,
            )
        base_val = entry.get("base_branch")
        if base_val is not None and (
            not re.match(r"^[a-zA-Z0-9_./-]+$", base_val) or ".." in base_val
        ):
            return (f"Invalid base_branch in repos[{idx}]: {base_val!r}", [], None, None)
        entries.append({"repo": repo_val, "base_branch": base_val})
        if entry.get("primary"):
            if seen_primary:
                return ("At most one repos entry may set 'primary'", [], None, None)
            seen_primary = True
            primary_index = idx
    # Reorder so the primary is first: the Pipeline model mirrors repos[0]
    # onto the legacy repo/base_branch singleton and exposes it as
    # ``primary_repo``.
    if primary_index != 0:
        entries.insert(0, entries.pop(primary_index))
    primary = entries[0]
    return (None, entries, primary["repo"], primary["base_branch"])


def _assert_repo_set_uniform(repos: list[str]) -> str | None:
    """Reject mixed-visibility / mixed-auth repo sets at submission (#3393, task-2-2).

    A pipeline-wide private-mode posture (context filtering, egress rules)
    requires every repo in one run to be uniformly private or uniformly public,
    and — for v1 — to share a single auth mode. Returns an actionable,
    repo-naming error string when the set diverges on either dimension, or
    ``None`` when it is uniform. A single repo (after de-duplication) is
    trivially uniform and short-circuits before any lookup, so N=1 pipelines
    pay no cost and make no gateway round-trip.

    Runtime note (container boundary): the orchestrator image bundles
    ``config/repo_config.py`` but NOT ``gateway/``, so the per-repo lookups are
    reached the way the orchestrator already reaches them — auth via
    ``repo_config.assert_uniform_auth`` (imported directly, the same callable the
    gateway's ``validate_auth_mode_uniformity`` delegates to) and visibility via
    ``GatewayClient.get_repo_visibility`` over HTTP (the gateway holds the
    tokens; mirrors ``_compute_gateway_mode``). ``internal`` counts as private.
    The visibility comparison below is the HTTP-boundary twin of
    ``gateway.repo_visibility.validate_visibility_uniformity`` (which the
    orchestrator cannot import); keep the two in step.
    """
    unique = list(dict.fromkeys(repos))
    if len(unique) <= 1:
        return None

    # Auth-mode uniformity — repo_config is bundled into the orchestrator image.
    try:
        from repo_config import assert_uniform_auth

        assert_uniform_auth(unique)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:  # pragma: no cover - defensive (config read failure)
        # Fail CLOSED for consistency with the visibility boundary below
        # (reviewer_security v1): a config-read failure means we cannot prove a
        # uniform auth mode, so we must not admit the set. repo_config is a
        # local, bundled read — this path is genuinely exceptional, not a
        # transient network hiccup.
        logger.warning("Auth-mode uniformity check errored; failing closed", error=str(exc))
        return (
            "Could not determine the auth mode for the pipeline's repos, so a "
            "uniform bot/user auth mode cannot be verified. Resubmit once repo "
            "configuration is resolvable."
        )

    # Visibility uniformity — resolved via the gateway (the orchestrator's only
    # visibility source). FAIL CLOSED on an indeterminate lookup (reviewer_security
    # v1): for a multi-repo set (we only reach here when len(unique) > 1) a repo
    # whose visibility cannot be resolved to a known bucket means the uniform
    # private/public posture cannot be PROVEN — and this is a confidentiality
    # boundary (a mixed set that slips through would let private-repo content
    # flow through shared plan/contract/PR surfaces into a public repo, with no
    # downstream re-check: _compute_gateway_mode derives the network mode from
    # the PRIMARY repo only). N=1 short-circuits above, so the common case pays
    # nothing. This mirrors gateway.repo_visibility.validate_visibility_uniformity;
    # keep the two in step. Unrecognized (non-None) labels are treated as
    # indeterminate too — only the known {public|private|internal} contract admits.
    gw = get_gateway_client()
    posture: dict[str, list[str]] = {}
    for repo in unique:
        vis = gw.get_repo_visibility(repo)
        if vis in ("private", "internal"):
            bucket = "private"
        elif vis == "public":
            bucket = "public"
        else:
            return (
                f"Could not determine repository visibility for {repo!r}; cannot "
                "verify a uniform private/public posture across the pipeline's "
                "repos (a run must be uniformly private or uniformly public so "
                "private-repo content cannot leak through shared plan/contract/PR "
                "surfaces). Resubmit once the repo's visibility is resolvable."
            )
        posture.setdefault(bucket, []).append(repo)
    if len(posture) > 1:
        groups = "; ".join(f"{b}: {', '.join(sorted(rs))}" for b, rs in sorted(posture.items()))
        return (
            "Mixed repository visibility across the pipeline's repos is not allowed "
            "(a run must be uniformly private or uniformly public, so private-repo "
            f"content cannot leak through shared plan/PR surfaces). Diverging repos — {groups}."
        )
    return None


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
    if data is None:
        return make_error_response("Missing request body")
    if not isinstance(data, dict):
        return make_error_response("Request body must be a JSON object")

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

    # #3393 (multi-repo): a submission may carry a ``repos`` list instead of
    # (or in addition to) the single ``repo``. Normalize it up front and derive
    # the primary onto the legacy ``repo``/``base_branch`` scalars so the
    # single-repo plumbing below (naming, base-branch detection, branch checks)
    # keeps working and a direct HTTP submission — one that bypasses the
    # submit_task MCP tool that would otherwise mirror the primary — is
    # supported. ``repos_entries`` is None for a single-repo submission.
    repos_entries: list[dict[str, str | None]] | None = None
    repos_arg = data.get("repos")
    if repos_arg is not None:
        _repos_err, repos_entries, _primary_repo, _primary_base = _normalize_submission_repos(
            repos_arg
        )
        if _repos_err:
            return make_error_response(
                _repos_err, status_code=400, details={"reason": "invalid_repos"}
            )
        if repo and _primary_repo and repo != _primary_repo:
            return make_error_response(
                f"Conflicting repo {repo!r} and repos primary {_primary_repo!r}; "
                "pass one or the other.",
                status_code=400,
                details={"reason": "repo_repos_conflict"},
            )
        if not repo:
            repo = _primary_repo
        if not base_branch:
            base_branch = _primary_base
    mode = data.get("mode", "issue")
    analysis = data.get("analysis")
    plan = data.get("plan")
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

    # Issue #1557: Jira-epic SDLC parameters. ``jira_ticket`` is the
    # Atlassian key; ``epic_mode`` is the operator's override
    # (``'auto' | 'fresh' | 'reassess'``). The MCP submit_task tool
    # normalises ``jira_ticket`` to upper-case before forwarding.
    jira_ticket_arg = data.get("jira_ticket")
    epic_mode_arg = data.get("epic_mode")
    if jira_ticket_arg is not None:
        if not isinstance(jira_ticket_arg, str) or not re.fullmatch(
            r"[A-Z][A-Z0-9_]*-\d+", jira_ticket_arg
        ):
            return make_error_response(
                f"Invalid jira_ticket: {jira_ticket_arg!r} (expected <PROJECT>-<number>)",
                status_code=400,
                details={"reason": "invalid_jira_ticket"},
            )
    if epic_mode_arg is not None:
        if epic_mode_arg not in ("auto", "fresh", "reassess"):
            return make_error_response(
                f"Invalid epic_mode: {epic_mode_arg!r} (must be 'auto' / 'fresh' / 'reassess')",
                status_code=400,
                details={"reason": "invalid_epic_mode"},
            )
        if not jira_ticket_arg:
            return make_error_response(
                "epic_mode requires jira_ticket",
                status_code=400,
                details={"reason": "epic_mode_without_ticket"},
            )

    # Validate mode
    valid_modes = {m.value for m in PipelineMode}
    if mode not in valid_modes:
        return make_error_response(f"Invalid mode: {mode!r} (must be one of {sorted(valid_modes)})")

    if not repo:
        return make_error_response("Missing repo")

    # Repo format sanity check — a lightweight shell-metacharacter guard.
    # The repo_config allowlist (repositories.yaml) is enforced gateway-side.
    if not re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$", repo):
        return make_error_response(
            f"Invalid repo format: {repo!r} (expected owner/name)",
            status_code=400,
            details={"reason": "repo_not_allowed"},
        )

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

    if (issue_number or pipeline_id) and not branch:
        return make_error_response("Missing branch")

    # #2399 — push the pipeline tip to ``<branch>/work`` so slice
    # integration branches at ``<branch>/slice-N`` can coexist as
    # siblings under the same namespace (git rejects a leaf ref and
    # children of that ref's path with ``directory file conflict``).
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

    # #3038: resolve the repo's default branch ONCE at submit time and
    # persist it on the pipeline record, so every downstream consumer
    # (the context-PR opener, the restart/spawn paths, the gateway
    # ``register_session`` base, the spawner ``EGG_BASE_BRANCH`` export)
    # reads a concrete base off the record instead of re-deriving it on
    # every invocation. Re-deriving each time opened a narrow race the
    # #3035 reviewer flagged: a single flaky ``git symbolic-ref
    # origin/HEAD`` read drops the opener into the ``origin/main →
    # origin/master → "main"`` fallback chain, which can pick the wrong
    # default on a ``master`` repo and 422 a second ``create_pr``.
    # Persisting closes the race because the consumers' ``base_branch or
    # _detect_default_branch(...)`` short-circuits on the stored value and
    # never reaches the subprocess. ``_detect_default_branch`` is the
    # local/fast helper (``git symbolic-ref``) and is the same resolution
    # the stale-branch reuse check below already performs.
    #
    # An explicit ``base_branch`` (validated above) is passed through
    # untouched; ``repo`` is already guaranteed non-empty by the early
    # ``Missing repo`` guard, so only the ``base_branch`` side needs a
    # check here.
    if not base_branch:
        base_branch = _detect_default_branch(repo_path)

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

    # Issue #1557: epic detection. Before persisting, resolve
    # is_epic + pipeline_mode against the gateway when a jira_ticket
    # was supplied. Failures are non-fatal (the helper fails open) —
    # we surface them as warnings in the API response but always
    # proceed with the pipeline creation.
    epic_warnings: list[str] = []
    is_epic_resolved = False
    pipeline_mode_resolved: str | None = None
    if jira_ticket_arg:
        try:
            from jira_epic import resolve_epic_mode
        except ImportError:  # pragma: no cover - defensive
            try:
                from orchestrator.jira_epic import resolve_epic_mode  # type: ignore[no-redef]
            except ImportError:
                resolve_epic_mode = None  # type: ignore[assignment]
        if resolve_epic_mode is not None:
            try:
                is_epic_resolved, pipeline_mode_resolved, epic_warnings = resolve_epic_mode(
                    ticket=jira_ticket_arg,
                    epic_mode_arg=epic_mode_arg,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Epic detection raised; treating as non-epic",
                    pipeline_id=pipeline_id,
                    ticket=jira_ticket_arg,
                    error=str(exc),
                )
            # Both explicit overrides (``reassess`` and ``fresh``) against
            # a non-epic ticket are operator errors: the operator
            # specifically asked for epic-mode treatment but the ticket
            # doesn't qualify. Surface as HTTP 400 rather than the
            # silent demotion ``resolve_epic_mode`` returns
            # (is_epic=False with a warning). ``mode='auto'`` continues
            # to demote silently to standard ticket mode — that's the
            # whole point of auto.
            if epic_mode_arg in {"reassess", "fresh"} and not is_epic_resolved:
                return make_error_response(
                    f"epic_mode={epic_mode_arg!r} but Jira ticket {jira_ticket_arg!r} is not an Epic",
                    status_code=400,
                    details={
                        "reason": f"{epic_mode_arg}_not_epic",
                        "warnings": epic_warnings,
                    },
                )

    # #3393 (multi-repo): enforce uniform visibility + auth across the run's
    # repos before creating the pipeline. Single-repo submissions are trivially
    # uniform and short-circuit without a gateway round-trip. Runs after the
    # gateway-ready gate above so the visibility lookup can reach the gateway.
    _uniform_repos = (
        [e["repo"] for e in repos_entries] if repos_entries else ([repo] if repo else [])
    )
    _uniformity_err = _assert_repo_set_uniform([r for r in _uniform_repos if r])
    if _uniformity_err:
        return make_error_response(
            _uniformity_err,
            status_code=400,
            details={"reason": "non_uniform_repo_set"},
        )

    # Assemble the full list-shaped repo set persisted onto the Pipeline. The
    # primary (entries[0]) carries the resolved ``base_branch`` (detected above
    # when absent); secondary repos keep their submitted base_branch (None ⇒
    # auto-detected downstream). For a single-repo submission we leave
    # ``repos_specs`` as None and let the Pipeline validator synthesize a
    # one-element list from the legacy singleton (N=1 back-compat).
    repos_specs: list[RepoSpec] | None = None
    if repos_entries is not None:
        repos_specs = [
            RepoSpec(
                repo=entry["repo"],
                base_branch=(base_branch if idx == 0 else entry["base_branch"]),
            )
            for idx, entry in enumerate(repos_entries)
        ]

    try:
        store = get_state_store(repo_path)
        pipeline = store.create_pipeline(
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            base_branch=base_branch,
            repos=repos_specs,
            config=config,
            prompt=prompt,
            network_mode=network_mode,
            pipeline_id=pipeline_id,
            analysis=analysis,
            plan=plan,
            source_branch=source_branch,
            source_artifact_prefix=source_artifact_prefix,
            has_contract=True,
            jira_ticket=jira_ticket_arg,
            is_epic=is_epic_resolved,
            pipeline_mode=pipeline_mode_resolved,
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
        # failure call store.update_pipeline / store.save_pipeline directly
        # (bypassing PATCH), so the PATCH-site clear never fires for them.
        # Without this POST-site clear, those auto-FAILED pipelines would
        # leak consensus + message-store state into the next run that
        # reuses the id.
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
    if data is None:
        return make_error_response("Missing request body")
    if not isinstance(data, dict):
        return make_error_response("Request body must be a JSON object")

    repo_path = get_repo_path()

    try:
        store, _pipeline = _resolve_pipeline(pipeline_id, repo_path)
        prev_status = _pipeline.status
        pipeline = store.update_pipeline(pipeline_id, data)

        # Emit the terminal event before kicking off cleanup so /status/wait
        # long-pollers wake immediately on cancellation rather than waiting
        # for the late-subscriber synth path on their next poll (#2663). The
        # run loop emits pipeline.completed / pipeline.failed from its own
        # terminal transitions; the PATCH path is the only place the
        # CANCELLED transition originates, so we emit it here. Gate on the
        # status *transition* (not equality) so idempotent retries against an
        # already-cancelled pipeline don't re-wake long-pollers.
        if pipeline.status == PipelineStatus.CANCELLED and prev_status != PipelineStatus.CANCELLED:
            _emit_pipeline_event(pipeline, "pipeline.cancelled")

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


# Config keys the live config-update route accepts. Deliberately a tight
# allowlist (#3174): most of PipelineConfig is consumed at submit time or
# mid-phase in ways a partial update could corrupt, whereas
# ``agent_models`` is re-resolved from a fresh store load before every
# spawn (the run loop reloads the pipeline at the top of each cycle, and
# the restart_agent / restart_phase paths load fresh state), so mutating
# it on a live pipeline is honored by construction. Widen only after
# verifying the same fresh-reload guarantee holds for the new key.
_MUTABLE_CONFIG_KEYS = frozenset({"agent_models"})


@pipelines_bp.route("/<pipeline_id>/config", methods=["PATCH"])
@require_lifecycle_secret
def update_pipeline_config(pipeline_id: str) -> tuple[Response, int]:
    """Update the safely-mutable subset of a live pipeline's config (#3174).

    Currently that subset is ``agent_models`` only. Semantics are a
    per-role merge with the pipeline's existing override map: roles
    absent from the request keep their current value, a string value
    sets that role's override, and an explicit ``null`` clears it (the
    role falls back to the repository default / built-in tiers).

    The updated config takes effect at the next agent spawn — currently
    running agents keep the model they were started with. Pair with
    ``restart_phase`` / ``restart_agent`` to apply the change to a
    running phase. Model *values* are not validated against a registry
    here (any non-Claude string routes to LiteLLM, mirroring submit-time
    behavior); a typo surfaces as a model-not-found error at spawn.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "agent_models": {
                "coder": "deepseek-v4-pro",
                "tester": null
            }
        }

    Response:
        {
            "success": true,
            "data": {
                "pipeline_id": "issue-123",
                "agent_models": {...},      # effective map after the merge
                "updated_roles": {...},     # roles set by this request
                "cleared_roles": [...]      # roles cleared by this request
            }
        }
    """
    data = request.get_json()
    if data is None:
        return make_error_response("Missing request body")
    if not isinstance(data, dict):
        return make_error_response("Request body must be a JSON object")

    unsupported = sorted(set(data) - _MUTABLE_CONFIG_KEYS)
    if unsupported:
        return make_error_response(
            f"Unsupported config keys: {unsupported}. This endpoint updates "
            f"only the safely-mutable config subset: {sorted(_MUTABLE_CONFIG_KEYS)}",
            status_code=400,
        )

    agent_models = data.get("agent_models")
    if not isinstance(agent_models, dict) or not agent_models:
        return make_error_response(
            "agent_models must be a non-empty object mapping role -> model "
            "(use null as the model to clear a role's override)",
            status_code=400,
        )

    # Pre-validate role keys against MODEL_OVERRIDE_ROLES so the operator
    # gets the same actionable message as PipelineConfig's field validator
    # instead of a wrapped pydantic StateValidationError. Lazy import
    # mirrors models._validate_agent_models_roles.
    from egg_contracts.agent_roles import MODEL_OVERRIDE_ROLES

    valid_roles = {role.value for role in MODEL_OVERRIDE_ROLES}
    invalid_roles = sorted(role for role in agent_models if role not in valid_roles)
    if invalid_roles:
        return make_error_response(
            f"Invalid agent_models role keys: {invalid_roles}. agent_models "
            f"is honored only for SDLC phase producer and reviewer roles: "
            f"{sorted(valid_roles)}",
            status_code=400,
        )
    invalid_values = sorted(
        role
        for role, model in agent_models.items()
        if model is not None and (not isinstance(model, str) or not model.strip())
    )
    if invalid_values:
        return make_error_response(
            f"Invalid agent_models values for roles {invalid_values}: each "
            f"value must be a non-empty model string, or null to clear the "
            f"role's override",
            status_code=400,
        )

    repo_path = get_repo_path()

    try:
        store, _pipeline = _resolve_pipeline(pipeline_id, repo_path)

        # Merge under the pipeline state lock so a concurrent writer
        # (another config update, the run loop persisting state) can't
        # interleave between our load and the store's load-modify-save.
        # The per-pipeline lock is an RLock, so update_pipeline's own
        # acquisition nests cleanly.
        with get_pipeline_state_lock(pipeline_id):
            current = store.load_pipeline(pipeline_id)

            # Reject mutations on terminal pipelines (#3174 review). No future
            # spawn consumes ``agent_models`` once a pipeline is COMPLETE /
            # FAILED / CANCELLED, so the merge would be a silent no-op; a 409
            # gives the operator a clear signal and matches restart_phase's
            # terminal-state precondition style. Checked under the lock against
            # freshly-loaded state so a concurrent terminal transition can't
            # slip a mutation through.
            if current.status in PipelineStatus.terminal():
                return make_error_response(
                    f"Pipeline {pipeline_id} is in terminal state "
                    f"{current.status.value}; agent_models cannot be updated "
                    "(no future spawn would consume the change).",
                    status_code=409,
                )

            merged = dict(current.config.agent_models)
            updated_roles: dict[str, str] = {}
            cleared_roles: list[str] = []
            for role_key, model in agent_models.items():
                if model is None:
                    if merged.pop(role_key, None) is not None:
                        cleared_roles.append(role_key)
                else:
                    merged[role_key] = model.strip()
                    updated_roles[role_key] = model.strip()
            pipeline = store.update_pipeline(pipeline_id, {"config.agent_models": merged})

        logger.info(
            "Pipeline agent_models updated",
            pipeline_id=pipeline_id,
            updated_roles=updated_roles,
            cleared_roles=cleared_roles,
        )

        return make_success_response(
            "Pipeline config updated",
            data={
                "pipeline_id": pipeline.id,
                "agent_models": pipeline.config.agent_models,
                "updated_roles": updated_roles,
                "cleared_roles": cleared_roles,
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
    """Restart a single agent in a pipeline (orchestrator-native).

    After #3164 the orchestrator unconditionally owns the BRC event
    loop: agent work runs as one-shot Jobs spawned per actionable
    event by the event loop, and the in-pod wait arm is gone. A
    resident pod spawned here without ``EGG_EVENT_ACTION`` would
    immediately log FATAL and ``exit 64``, so ``restart_agent`` no
    longer spawns anything itself. Instead it:

      0. Enforces the per-(pipeline, role, slice) restart budget
         (``check_and_increment_restart_count``); a request over budget is
         rejected with HTTP 429 before any state is mutated (#3244).
      1. Best-effort deletes the role's live one-shot Job(s) (to kill a
         stuck pod). One-shot Jobs carry an event-discriminator suffix
         in their name, so they are found by label
         (``LABEL_PIPELINE_ID`` + ``LABEL_AGENT_ROLE`` [+ ``LABEL_SLICE_ID``
         when slice-scoped]), not by name.
      2. Resets the role's consensus state and health-monitor anchor.
      3. Marks the agent record RUNNING with ``container_id = None``.

    For a pipeline that is already RUNNING, the live event loop (polling
    ~every 5s during the concurrent phase) spawns a fresh one-shot pod once
    the role's consensus state is reset — that is the respawn. For a pipeline
    that was FAILED/CANCELLED the event loop and its ``_run_pipeline`` driver
    thread are already dead, so the route also relaunches a fresh driver
    thread (mirroring ``restart_phase``) to restart the event loop; otherwise
    the reset would leave the pipeline RUNNING-but-idle with nothing to
    respawn it (#3244). The agent's per-agent worktree is preserved so
    committed work is retained.

    URL params:
        pipeline_id: Pipeline ID
        agent_role: Agent role to restart (e.g. "coder", "tester")

    Query string (optional):
        slice_id: Slice scope (``slice-<N>``). When supplied, the
            slice-scoped Job and worktree are restarted, ``EGG_SLICE_ID``
            is propagated to the new Job, and consensus reset targets
            the per-slice tracker. ``slice_id`` may also be supplied via
            the JSON body. When omitted for a role that runs as a
            per-slice agent, it is derived from the phase's agent records
            (#2759): if exactly one slice has a non-complete record for
            the role, that slice is used; otherwise the request is
            rejected with the candidate list rather than spawning an
            unscoped agent. The scan is scoped to ``pipeline.current_phase``
            only — if the pipeline has advanced past the slice's phase
            (e.g. to ``pr`` or a later iteration) no current-phase records
            will name the role, derivation falls through, and the operator
            should supply ``slice_id`` explicitly. This is operator guidance,
            not a code-enforced precondition: the fall-through branch
            proceeds to a pipeline-level spawn rather than rejecting.
            Genuinely pipeline-level agents (no per-slice records for the
            role) omit ``slice_id``.

    Request body (optional):
        {
            "reason": "Human-readable reason for the restart",
            "slice_id": "slice-2"
        }

    Response:
        {
            "success": true,
            "data": {
                "agent_role": "coder",
                "slice_id": "slice-2",
                "respawn": "delegated to orchestrator event loop",
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

    # Slice auto-derivation (#2759). A slice-mode restart that omits
    # ``slice_id`` would otherwise spawn the agent pipeline-level:
    # ``EGG_SLICE_ID`` is set by the spawner only when ``slice_id`` is
    # non-None, so the respawned agent's BRC signals route to the bare
    # pipeline tracker instead of the slice's tracker. The slice's own
    # tracker keeps the dead agent registered while the live one ACKs
    # into the wrong tracker — the slice's consensus then wedges with no
    # message-bus recovery path. Since ``restart_agent`` is the
    # operator's normal tool for recovering a failed container, the
    # omission must not silently produce an unscoped agent.
    #
    # When the role runs as a per-slice agent (it has slice-scoped
    # records in the current phase), derive the slice: the k8s monitor
    # marks a cleanly-exited agent COMPLETE and a crashed one FAILED, so
    # a single non-COMPLETE record isolates the slice that needs the
    # restart. If the choice is ambiguous — multiple non-COMPLETE
    # records, or none at all — reject with the candidate list so the
    # operator re-issues with an explicit ``slice_id``.
    if slice_id is None:
        derive_phase_exec = pipeline.phases.get(pipeline.current_phase.value)
        if derive_phase_exec is not None:
            role_records = [
                a
                for a in derive_phase_exec.agents
                if hasattr(a, "role")
                and (a.role == role or (hasattr(a.role, "value") and a.role.value == role.value))
            ]
            sliced_records = [a for a in role_records if getattr(a, "slice_id", None)]
            if sliced_records:
                known_slices = sorted({a.slice_id for a in sliced_records})
                restart_candidates = sorted(
                    {
                        a.slice_id
                        for a in sliced_records
                        if a.status != AgentExecutionStatus.COMPLETE
                    }
                )
                if len(restart_candidates) == 1:
                    slice_id = restart_candidates[0]
                    logger.info(
                        "restart_agent: derived slice_id from phase agent records",
                        pipeline_id=pipeline_id,
                        agent_role=agent_role,
                        slice_id=slice_id,
                    )
                else:
                    detail = (
                        "no slice has a non-complete agent record for this role"
                        if not restart_candidates
                        else f"{len(restart_candidates)} slices have a non-complete record"
                    )
                    return make_error_response(
                        f"Agent role {agent_role!r} runs as a per-slice agent in "
                        f"pipeline {pipeline_id}; restart_agent could not derive "
                        f"slice_id ({detail}). Re-issue with an explicit slice_id.",
                        status_code=400,
                        details={
                            "agent_role": agent_role,
                            "known_slices": known_slices,
                            "restart_candidates": restart_candidates,
                        },
                        reason="slice_id_required",
                    )

    # Slice-existence check (#2421): a well-formed but unknown
    # ``slice_id`` would otherwise spawn an orphan Job + worktree
    # the rest of the system has no record of. The shape regex in
    # ``extract_slice_id`` only catches malformed values; only the
    # contract knows which slices the pipeline actually has.
    #
    # Pipelines without a contract are not
    # slice-aware, so any non-``None`` ``slice_id`` targeting them is
    # by definition unknown — reject outright. For contracted
    # pipelines, load the contract and check membership; fall through
    # silently if the contract can't be loaded (worktree pruned,
    # contract not yet populated, filesystem error) so we don't
    # regress legitimate restarts on the existing pipeline-level path.
    #
    # After #3164 ``restart_agent`` no longer spawns a worktree itself,
    # so the slice's parent-edge / base-branch resolution that used to
    # feed the spawn is gone. Only the existence check below remains.
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

    spawner = _get_spawner()

    current_phase = pipeline.current_phase.value
    phase_exec = pipeline.phases.get(current_phase)

    # Enforce the per-(pipeline, role, slice) restart budget BEFORE any
    # destructive action (#3244 review). Pre-#3164 this cap lived inside
    # ``restart_agent_job``, which the route no longer calls — without
    # re-enforcing it here an operator/overseer could call ``restart_agent``
    # without bound, each call resetting consensus and actively preventing a
    # live phase from converging. ``check_and_increment_restart_count`` raises
    # when the budget is exhausted; reject loudly (429) instead of flipping
    # status / resetting consensus and returning a misleading success. The
    # returned count is the source of truth for the ``restart_count``
    # telemetry below (the old read-only ``get_restart_count`` read always
    # reported 0 on this path since nothing incremented it).
    try:
        new_restart_count = spawner.check_and_increment_restart_count(
            pipeline_id, role, slice_id=slice_id
        )
    except KubernetesSpawnError as budget_err:
        logger.warning(
            "restart_agent rejected: restart budget exhausted",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            error=str(budget_err),
        )
        return make_error_response(str(budget_err), status_code=429)

    # Early status update: transition FAILED/CANCELLED -> RUNNING so that
    # get_status returns "running" immediately. Unlike a RUNNING pipeline —
    # whose live event loop picks up the consensus reset below and respawns
    # within one poll — a FAILED/CANCELLED pipeline has NO live event loop:
    # ``_run_concurrent_phase`` already returned and ``stop_event_loop()``
    # tore the loop down on its way out, and the ``_run_pipeline`` driver
    # thread has exited. Resetting consensus alone would leave the pipeline
    # RUNNING-but-idle with nothing to respawn it (#3244 review). So when we
    # make this transition we record it and relaunch a fresh ``_run_pipeline``
    # driver thread at the end of the route (mirroring ``restart_phase`` step
    # 7) — that restarts the event loop, which then performs the respawn.
    pipeline_was_inactive = pipeline.status in (
        PipelineStatus.FAILED,
        PipelineStatus.CANCELLED,
    )
    if pipeline_was_inactive:
        early_lock = get_pipeline_state_lock(pipeline_id)
        with early_lock:
            pipeline = store.load_pipeline(pipeline_id)
            if pipeline.status in (PipelineStatus.FAILED, PipelineStatus.CANCELLED):
                pipeline.status = PipelineStatus.RUNNING
                _phase_exec = pipeline.phases.get(current_phase)
                if _phase_exec is not None:
                    _phase_exec.status = PipelineStatus.RUNNING
                # Bump run_epoch so the relaunched driver thread (below) owns a
                # fresh epoch namespace and any stale thread that observes the
                # transition detects itself as superseded (mirrors
                # ``restart_phase`` / ``advance_phase``).
                pipeline.run_epoch = datetime.now(UTC)
                pipeline.updated_at = datetime.now(UTC)
                store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))
            else:
                # Lost the race — another writer already moved it off
                # FAILED/CANCELLED, so its driver thread / event loop is
                # live and will own the respawn. Don't relaunch a duplicate.
                pipeline_was_inactive = False

    # #3164: ``restart_agent`` no longer spawns a resident pod. The
    # orchestrator event loop owns the BRC respawn — once the role's
    # consensus state is reset (below), it spawns a fresh one-shot pod
    # within one ~5s poll. Here we only (1) kill any live one-shot Job
    # for the role so a stuck pod is torn down, then (2) reset consensus
    # + health so the event loop reschedules.

    # Delete the role's live one-shot Job(s), best-effort. One-shot
    # event Jobs carry an event-discriminator SUFFIX in their name (one
    # Job per actionable BRC event), so they can't be addressed by a
    # deterministic name — find them by LABEL. Match on pipeline +
    # role (and slice when scoped). Zero matches is fine (the role may
    # have already exited cleanly); the event loop will respawn either
    # way once consensus is reset. Wrap broadly so a k8s/list failure
    # never fails the restart.
    job_labels = {
        LABEL_PIPELINE_ID: pipeline_id,
        # The role label value is the underscore form (e.g.
        # ``reviewer_code``), which is exactly ``agent_role`` / ``role.value``.
        LABEL_AGENT_ROLE: role.value,
    }
    if slice_id is not None:
        job_labels[LABEL_SLICE_ID] = slice_id
    try:
        live_jobs = spawner.k8s.list_containers(labels=job_labels)
        removed_jobs = 0
        for job in live_jobs:
            try:
                # Mirror the cleanup call sites: prefer the explicit
                # ``job_name`` (already Job-prefixed), fall back to the
                # container id which ``remove_agent_job`` -> ``remove_container``
                # resolves to a Job name.
                spawner.remove_agent_job(job.job_name or job.container_id, force=True)
                removed_jobs += 1
            except Exception as job_err:  # noqa: BLE001 - best-effort teardown
                logger.warning(
                    "Failed to delete live one-shot Job during restart (best-effort)",
                    pipeline_id=pipeline_id,
                    agent_role=agent_role,
                    slice_id=slice_id,
                    job_name=getattr(job, "job_name", None),
                    error=str(job_err),
                )
        logger.info(
            "restart_agent: deleted live one-shot Job(s) for role",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            removed=removed_jobs,
        )
    except Exception as list_err:  # noqa: BLE001 - best-effort teardown
        logger.warning(
            "Failed to list live one-shot Jobs during restart (best-effort)",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            error=str(list_err),
        )

    # Reset consensus state for this agent so the event loop reschedules
    # a fresh one-shot pod for it. If consensus reset fails, log a
    # warning but don't fail the restart: the agent will re-enter
    # consensus on its own. Slice-scoped restarts (#2410) target the
    # per-slice tracker; the pipeline-level tracker has no record of the
    # slice agent.
    # Slice-scoped restarts (#2410) target the per-slice tracker; the
    # pipeline-level tracker has no record of the slice agent.
    #
    # INVARIANT (#3200 task-7-1, mid-phase BRC record survival): this reset
    # clears the *peer consensus tracker* (the ephemeral ACK/NACK/proposal
    # bookkeeping the restarted agent rebuilds by re-proposing) but MUST NOT
    # clear the *Redis message store* (``pipeline:{id}:messages``). That store
    # is the durable BRC message record — CONSENSUS_PROPOSE/ACK/NACK and the
    # conditional-ACK obligations — and a mid-phase restart deliberately
    # preserves it so the reseeded/resumed session can re-pull it via
    # ``GET /<pipeline_id>/brc-transcript`` + ``read_peer_artifact`` and
    # re-derive the #3189 deterministic anchors. The store is cleared only at
    # phase transitions (``_clear_concurrent_state``) and pipeline
    # create/delete (``_clear_pipeline_runtime_state``), never here. Do NOT
    # add ``get_message_store().clear()`` / ``_clear_concurrent_state`` to the
    # restart path — that would lose the record across the restart boundary.
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

    # Reset health-monitor anchor so the pre-respawn _last_heartbeat does not
    # generate a stale-elapsed heartbeat_timeout alert against the fresh
    # container (issue #2084).
    #
    # #2270 slice-5 (restart hygiene): ``reset_agent`` also drops the agent's
    # accumulated per-agent escalation state (escalation flags, error counts,
    # active alerts). Clearing it on restart is what stops a freshly-restarted
    # agent from inheriting a stale redirect/escalation history that would push
    # it straight to HITL on its first post-restart stall. The Tier-2 overseer's
    # own escalation-history clear + generation reset live on
    # ``OverseerMonitor`` (overseer/monitor.py:reset_escalation_history /
    # reset_generation), which the on-demand adjudicator constructs fresh.
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

    # Update pipeline state. No resident container is spawned (#3164) —
    # the event loop will respawn a one-shot pod within one poll once the
    # consensus reset above takes effect. We mark the agent RUNNING with
    # ``container_id = None`` (the live pod is set by the event loop) and
    # refresh ``started_at`` so the overseer's
    # phase_minimum_working_window suppression on the
    # ``agent-heartbeat-stall`` trigger anchors on the restart (#2084).
    lock = get_pipeline_state_lock(pipeline_id)
    with lock:
        pipeline = store.load_pipeline(pipeline_id)
        if phase_exec is not None:
            # Re-fetch from the freshly loaded pipeline (the outer check gates
            # on "did the phase exist before the restart?").
            fresh_phase_exec = pipeline.phases.get(current_phase)
            if fresh_phase_exec is not None:
                from models import AgentExecution  # type: ignore

                respawn_started_at = datetime.now(UTC)
                # Match on ``(role, slice_id)`` — without the slice tiebreaker
                # the first matching role wins, which on a multi-slice phase
                # mutates the wrong slice's record (#2422). ``slice_id`` is
                # the route-level scope already plumbed into the consensus
                # tracker above.
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
                    agent.container_id = None
                    agent.status = AgentExecutionStatus.RUNNING
                    agent.started_at = respawn_started_at
                    found = True
                    break
                if not found:
                    fresh_phase_exec.agents.append(
                        AgentExecution(
                            role=role,
                            container_id=None,
                            status=AgentExecutionStatus.RUNNING,
                            started_at=respawn_started_at,
                            slice_id=slice_id,
                        )
                    )

        pipeline.updated_at = datetime.now(UTC)
        store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))

    # ``restart_count`` is the value just incremented by
    # ``check_and_increment_restart_count`` above (#3244). It is scoped to the
    # same ``(pipeline_id, agent_role, slice_id)`` bucket the cap is enforced
    # on, so it correctly reports the operator's "you've burned N of M
    # restarts" telemetry — the pre-fix read-only ``get_restart_count`` read
    # always reported 0 here because nothing on this path incremented it.
    response_data: dict[str, object] = {
        "agent_role": agent_role,
        "slice_id": slice_id,
        "respawn": "delegated to orchestrator event loop",
        "restart_count": new_restart_count,
    }

    # When the pipeline was FAILED/CANCELLED its event loop and driver thread
    # are dead (see the early-status comment above), so the consensus reset
    # alone has nothing to act on it. Relaunch a fresh ``_run_pipeline`` driver
    # thread — exactly as ``restart_phase`` step 7 does — to restart the event
    # loop, which then respawns the role's one-shot Job within one poll. For a
    # pipeline that was already RUNNING we skip this: its live event loop owns
    # the respawn and a second driver thread would race it (#3244 review).
    if pipeline_was_inactive:
        _spawn_pipeline_run_thread(pipeline_id, store.repo_path, pipeline.run_epoch)
        logger.info(
            "restart_agent: relaunched driver thread for inactive pipeline",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            run_epoch=pipeline.run_epoch.isoformat() if pipeline.run_epoch else None,
        )

    logger.info(
        "Agent restart requested (respawn delegated to event loop)",
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        slice_id=slice_id,
        restart_count=response_data.get("restart_count"),
        reason=reason,
    )

    return make_success_response(
        f"Agent {agent_role} restarted",
        data=response_data,
    )


@pipelines_bp.route("/<pipeline_id>/phases/<phase>/restart", methods=["POST"])
@require_lifecycle_secret
def restart_phase(pipeline_id: str, phase: str) -> tuple[Response, int]:
    """Restart all agents in a pipeline phase.

    Stops and removes all containers for the phase, resets consensus and
    review cycle state, and respawns all agents.  Prior phase artifacts
    (from earlier phases) are preserved.

    Preservation semantics (#3080): per-agent worktrees AND their local
    branches are deleted, so per-role branch tips do not survive a phase
    restart.  Unpushed commits are salvaged to ``egg/recovered/*`` refs
    on a best-effort basis (#2429) — ``auto_salvage_pipeline``
    re-enumerates worktrees with ``validate_git=True``, so worktrees
    with a corrupted ``.git`` marker (the #1723 failure class) may be
    skipped without salvage.  The respawned agents' fresh worktrees
    re-fork from the shared work branch tip (``origin/<assigned_branch>``,
    base-branch fallback when unpushed — see #3068).  Anything that
    lived only on a per-role branch (e.g. a reviewer's merge history)
    is therefore discarded from agent trees; only state pushed to the
    shared work branch is re-materialised on respawn.  Operators needing
    per-worktree retention should use ``restart_agent`` instead.

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
        #    source the executor itself consults — ``get_roles_for_phase``.
        #    Without this fallback a restart whose clear step ran
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
            # produce.
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
                # ``_run_concurrent_phase``, which lets the same failure
                # propagate up the worker thread. In a synchronous HTTP
                # context an honest 400 ("No agents found") is more
                # useful to the operator than a 500.
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

    # 3b. Persist the in-flight phase's BRC message record to disk BEFORE the
    #     destructive container/worktree teardown (#3200 task-7-1, mid-phase
    #     BRC record survival). Today ``_write_brc_history`` runs only at phase
    #     transitions (``_persist_phase_brc_history`` in complete/advance_phase,
    #     #1827); a mid-phase restart never wrote the durable on-disk
    #     transcript.
    #
    #     PRIMARY mechanism is option (a), the live Redis stream: it survives a
    #     bare restart (the store is cleared only at phase transitions /
    #     pipeline create+delete, never here — see step 5), so a reseeded
    #     session re-pulls the in-flight record from Redis via
    #     ``/brc-transcript`` + ``read_peer_artifact``. The slice-scoped
    #     CONSENSUS_PROPOSE/ACK/NACK records of an in-flight implement slice
    #     rely on (a) for survival.
    #
    #     This disk persist (option (b)) is a belt-and-suspenders ADD-ON with a
    #     deliberately NARROW durability scope — do not overstate it. It calls
    #     ``_persist_phase_brc_history`` -> ``_write_brc_history(
    #     write_per_slice=False)``. For a slice-aware implement phase that path
    #     writes ONLY the ``{id}-implement-unattributed.{md,json}`` sibling
    #     (non-CONSENSUS BRC types: HEARTBEAT/STATUS/HANDOFF/AGENT_FAILED/
    #     NUDGE/OVERSEER_ALERT) and SKIPS the per-slice bucket loop; the
    #     slice's CONSENSUS_* proposals/verdicts/open-NACKs are NOT written to
    #     disk here (write_per_slice=False avoids the #2755 add/add conflict on
    #     ``work``; per-slice files are owned by the slice integration branch).
    #     So across a FULL Redis loss (orchestrator pod death, the cold-start
    #     case task-6-1 covers) the in-flight slice record does NOT survive on
    #     disk — only (a) preserves it. What (b) does buy: for non-slice phases
    #     (plan/refine/pr) and non-slice implement runs the aggregate
    #     ``{id}-{phase}.{md,json}`` transcript IS written, and for slice runs
    #     the unattributed audit sibling is captured — extending the #1827
    #     persist-before-clear invariant to the restart path for everything
    #     except the per-slice CONSENSUS buckets. Best-effort and front-running
    #     teardown: a transcript-write hiccup must never block recovery of a
    #     wedged phase (mirrors the salvage step below).
    try:
        _persist_phase_brc_history(pipeline, store, phase)
    except Exception as brc_persist_err:  # noqa: BLE001
        logger.warning(
            "Failed to persist in-flight BRC history during phase restart (continuing)",
            pipeline_id=pipeline_id,
            phase=phase,
            error=str(brc_persist_err),
        )

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

    # 5. Reset consensus state.
    #    Slice-4 TASK-4-1: mirror the slice-aware semantics of
    #    ``restart_agent`` (line ~2859) — clear BOTH the pipeline-level
    #    tracker AND every per-slice tracker keyed
    #    ``f"{pipeline_id}/{slice_id}"`` (see
    #    ``peer_consensus._tracker_key``). Phase-level restart wipes
    #    the entire phase, so any per-slice consensus state that
    #    survived the restart is stale and would deadlock the new run
    #    if left in place.
    #
    #    INVARIANT (#3200 task-7-1, mid-phase BRC record survival): like
    #    ``restart_agent`` above, this clears the *peer consensus tracker*
    #    (ephemeral ACK/NACK state) but MUST NOT clear the *Redis message
    #    store* (``pipeline:{id}:messages``). That store is the durable BRC
    #    message record; a mid-phase phase restart preserves it so the
    #    reseeded session can re-pull it (``/brc-transcript`` +
    #    ``read_peer_artifact``) and re-derive the #3189 anchors. The store is
    #    cleared only at phase transitions / pipeline create+delete, never on
    #    restart. Do NOT add ``get_message_store().clear()`` here.
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

        # Per-slice trackers. Best-effort contract load: if the
        # contract cannot be read (corrupt on disk, etc.), the
        # pipeline-level clear above still ran, and the slice
        # trackers will be reconstructed lazily on next consensus
        # activity — preserving the historical pipeline-level-only
        # behaviour as a fallback rather than blocking the restart.
        # **Worktree-path resolution (reviewer_code v1 blocker 2)**:
        # active pipelines' contracts live in the per-pipeline
        # worktree at ``/home/egg/.egg-worktrees/<pipeline_id>/<repo>/``
        # — NOT under ``store.repo_path`` (the main orchestrator repo).
        # Without ``resolve_worktree_path`` the ``load_contract`` call
        # below silently fails with ``ContractNotFoundError`` for every
        # active pipeline, the per-slice loop never iterates, and the
        # whole per-slice clear becomes a no-op. Pattern mirrors
        # ``routes/signals.py:709`` and ``routes/pipelines.py:10017``.
        try:
            from egg_contracts.loader import load_contract
        except ImportError:
            load_contract = None  # type: ignore[assignment]
        if load_contract is not None:
            try:
                from routes import resolve_worktree_path
            except ImportError:
                try:
                    from .. import (
                        resolve_worktree_path,  # type: ignore[no-redef]
                    )
                except ImportError:
                    resolve_worktree_path = None  # type: ignore[assignment]
            try:
                if resolve_worktree_path is not None:
                    _contract_repo_path = resolve_worktree_path(pipeline_id, Path(store.repo_path))
                else:
                    _contract_repo_path = Path(store.repo_path)
                _contract = load_contract(pipeline_id, _contract_repo_path)
            except Exception as load_err:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Could not load contract to enumerate slice trackers "
                    "during phase restart; per-slice consensus state may "
                    "be left stale until lazy reconstruction",
                    pipeline_id=pipeline_id,
                    error=str(load_err),
                )
                _contract = None
            if _contract is not None and getattr(_contract, "slices", None):
                for _s in _contract.slices:
                    _slice_tracker = get_peer_consensus_tracker(pipeline_id, slice_id=_s.id)
                    if _slice_tracker:
                        _slice_tracker.clear()
                        logger.info(
                            "Cleared per-slice peer consensus tracker",
                            pipeline_id=pipeline_id,
                            slice_id=_s.id,
                        )
    except ImportError:
        pass
    except Exception as e:
        logger.warning(
            "Failed to clear peer consensus",
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


def apply_first_principles_redirect(
    pipeline_id: str,
    new_task_description: str,
    *,
    reason: str,
) -> list[str]:
    """Adopt a first-principles redirect: rewrite the seed and re-run refine.

    Called in-process from the decision-resolve hook when an operator adopts a
    redirect raised by the ``first_principles_reviewer``. Two durable steps:

    1. **Rewrite the seed** via the operator-grade
       ``rewrite_task_description_as_operator`` (audited, ``Role.HUMAN``), then
       commit+push the worktree to the work branch so the refine restart's
       re-fork (which forks fresh worktrees from ``origin/<branch>``) sees the
       rewritten ``task_description`` rather than the old one.
    2. **Re-run refine** via :func:`_restart_refine_phase`.

    Returns the role values respawned. Raises on failure; the caller logs and
    leaves the decision resolved (the operator's intent is recorded regardless).
    """
    from operator_actions import rewrite_task_description_as_operator

    repo_path = get_repo_path()
    store, pipeline = _resolve_pipeline(pipeline_id, repo_path)
    issue_number = getattr(pipeline, "issue_number", None)
    gateway_mode, _ = _compute_gateway_mode(pipeline)
    spawner = _get_spawner()

    rewrite = rewrite_task_description_as_operator(
        pipeline_id,
        new_task_description,
        reason=reason,
        actor="operator:first-principles-redirect",
        issue_number=issue_number,
    )

    # Durably land the rewritten seed on the work branch. The refine restart
    # below deletes per-agent worktrees and re-forks fresh ones from
    # ``origin/<branch>``; without this push the re-fork would re-materialise
    # the OLD seed and the redirect would be silently lost (#3080 re-fork
    # semantics).
    worktree = Path(rewrite["worktree"])
    identifier = _pipeline_identifier(issue_number, pipeline_id)
    try:
        committed = _commit_statefiles_to_worktree(
            worktree,
            f"first-principles redirect: rewrite seed — {reason}"[:200],
            identifier,
            pipeline_id=pipeline_id,
        )
        if committed and pipeline.branch:
            spawner.gateway.push_worktree_branch(
                pipeline_id=pipeline_id,
                repo_path=str(worktree),
                branch=pipeline.branch,
                mode=gateway_mode,
                base_branch=pipeline.base_branch,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; restart still proceeds
        logger.warning(
            "Failed to push rewritten seed to work branch; refine restart may "
            "re-fork the prior seed (first-principles redirect)",
            pipeline_id=pipeline_id,
            error=str(exc),
        )

    return _restart_refine_phase(
        pipeline_id, store, reason=reason, spawner=spawner, gateway_mode=gateway_mode
    )


def _restart_refine_phase(
    pipeline_id: str,
    store: Any,
    *,
    reason: str,
    spawner: Any,
    gateway_mode: str,
) -> list[str]:
    """Re-run the refine phase in-process (non-route sibling of ``restart_phase``).

    Mirrors ``restart_phase``'s essential steps for the refine phase so the
    first-principles accept-path can re-run refine from the decision-resolve
    hook (no Flask request). Refine has no slices, so the per-slice tracker
    loop in ``restart_phase`` is intentionally omitted. Raises ``ValueError``
    if the pipeline is not currently parked at the refine phase.
    """
    phase = PipelinePhase.REFINE.value
    lock = get_pipeline_state_lock(pipeline_id)
    with lock:
        pipeline = store.load_pipeline(pipeline_id)
        if pipeline.current_phase.value != phase:
            raise ValueError(
                f"_restart_refine_phase: pipeline {pipeline_id} is not at the "
                f"refine phase (current: {pipeline.current_phase.value})"
            )
        phase_exec = pipeline.phases.get(phase)
        if phase_exec is None:
            raise ValueError(f"Refine phase not found in pipeline {pipeline_id}")

        agent_roles: list[AgentRole] = []
        for agent in phase_exec.agents:
            if hasattr(agent, "role"):
                role = agent.role if isinstance(agent.role, AgentRole) else AgentRole(agent.role)
                agent_roles.append(role)
        if not agent_roles:
            from egg_contracts.agent_roles import get_roles_for_phase as _grfp

            for r in _grfp(
                phase,
                include_reviewers=True,
                repo=pipeline.repo,
                has_contract=getattr(pipeline, "has_contract", True),
            ):
                try:
                    agent_roles.append(AgentRole(r.value))
                except ValueError:
                    continue

        old_container_ids = [c.container_id for c in phase_exec.containers]
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
        store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))

    # --- Outside the lock: slow, idempotent, best-effort teardown ---
    for container_id in old_container_ids:
        try:
            spawner.stop_agent_container(container_id, cleanup_session=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to stop container during refine redirect restart",
                container_id=container_id[:12] if container_id else "?",
                error=str(e),
            )
        try:
            spawner.remove_agent_container(container_id, force=True, cleanup_session=False)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to remove container during refine redirect restart",
                container_id=container_id[:12] if container_id else "?",
                error=str(e),
            )

    restart_role_values = {role.value for role in agent_roles}
    try:
        all_worktrees = agent_salvage.enumerate_agent_worktrees(pipeline_id, validate_git=False)
    except (OSError, ImportError, RuntimeError) as e:
        logger.warning(
            "Failed to enumerate per-agent worktrees during refine redirect restart",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        all_worktrees = []
    worktrees_to_delete = [wt for wt in all_worktrees if wt.agent_role in restart_role_values]
    if worktrees_to_delete:
        try:
            agent_salvage.auto_salvage_pipeline(
                spawner.gateway,
                pipeline_id,
                worktree_filter={wt.worktree_id for wt in worktrees_to_delete},
                mode=gateway_mode,
                base_branch=pipeline.base_branch,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Auto-salvage failed during refine redirect restart; proceeding",
                pipeline_id=pipeline_id,
                error=str(e),
            )
    for wt in worktrees_to_delete:
        try:
            spawner.gateway.delete_worktrees(container_id=wt.worktree_id, force=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to delete per-agent worktree during refine redirect restart",
                agent_worktree_id=wt.worktree_id,
                pipeline_id=pipeline_id,
                error=str(e),
            )

    try:
        from peer_consensus import get_peer_consensus_tracker

        tracker = get_peer_consensus_tracker(pipeline_id)
        if tracker:
            tracker.clear()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Failed to clear peer consensus during refine redirect restart",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    spawner.reset_restart_counts(pipeline_id)
    try:
        from health_monitor import get_health_monitor

        _hm = get_health_monitor()
        if _hm is not None:
            for role in agent_roles:
                _hm.reset_agent(role.value)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Failed to reset health-monitor state during refine redirect restart",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    _spawn_pipeline_run_thread(pipeline_id, store.repo_path, pipeline.run_epoch)
    agents_to_restart = [role.value for role in agent_roles]
    logger.info(
        "Refine phase re-run for first-principles redirect",
        pipeline_id=pipeline_id,
        reason=reason,
        agents_to_restart=agents_to_restart,
    )
    return agents_to_restart


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

    # Validate ``slice_id`` BEFORE the StateStore disk read in
    # ``_resolve_pipeline`` — a malformed value is going to 400 anyway,
    # and the read is wasted (#2764 review). ``InvalidPipelineIdError``
    # / ``PipelineNotFoundError`` from ``_resolve_pipeline`` still
    # naturally take precedence on the happy path: this validator only
    # fires when a slice scope is supplied at all.
    raw_slice_id = request.args.get("slice_id")
    try:
        status_slice_id = extract_slice_id(
            {"slice_id": raw_slice_id} if raw_slice_id is not None else {}
        )
    except ValueError as e:
        return make_error_response(str(e), status_code=400)

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

        # Include concurrent execution monitoring when enabled. The
        # ``?slice_id=`` query param (validated above before the
        # StateStore read) scopes the consensus block to one slice's
        # BRC tracker in a slice-DAG implement phase (#2761); without
        # it, only pipeline-level consensus is reported.
        concurrent_data = _get_concurrent_status(pipeline, slice_id=status_slice_id)
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
            from ..messages import _apply_delphi_filter as _delphi  # type: ignore[no-redef]
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
    """Extract context-PR URL and number from the pipeline contract.

    Returns ``(pr_url, pr_number)`` or ``(None, None)`` when no PR has
    been opened. Under #2777 the PR phase was removed and the context
    PR opens up-front via ``_open_context_pr_at_implement_start`` which
    persists ``context_pr_number`` to ``contract.pr.context_pr_number``;
    we read that directly. ``pr_url`` is also persisted on the pipeline
    record by ``_open_context_pr_at_implement_start`` for downstream
    consumers (the JIRA reassess sweep at ``jira_reassess.py``).
    """
    # ``Pipeline.pr_url`` / ``Pipeline.pr_number`` are populated by the
    # up-front opener; they are the canonical surface for callers that
    # used to read ``phases["pr"].artifacts["pr_url"]``.
    pr_url = getattr(pipeline, "pr_url", None)
    pr_number = getattr(pipeline, "pr_number", None)
    if not pr_url:
        return None, None
    if pr_number is None:
        match = re.search(r"/pull/(\d+)", pr_url)
        pr_number = int(match.group(1)) if match else None
    return pr_url, pr_number


def _consensus_block(consensus_state: dict) -> dict:
    """Slim a tracker ``get_state()`` snapshot down to the status payload.

    Keeps the fields operators act on (per-role phases + confirmed
    flags, the blocking set, and the unresolved-NACK details: who
    NACKed whom, on which version, and why; #3481) and drops the
    bulky ``approval_matrix`` / ``review_graph`` dumps.

    BRC trackers only emit dict-format agent entries (the legacy
    AgentReadiness object came from the now-deleted ConsensusEvaluator,
    cq-5 of #2777).
    """
    return {
        "agents": dict(consensus_state.get("agents", {})),
        "is_complete": consensus_state.get("is_complete", False),
        "blocking_agents": consensus_state.get("blocking_agents", []),
        "has_unresolved_nacks": consensus_state.get("has_unresolved_nacks", False),
        "unresolved_nacks": consensus_state.get("unresolved_nacks", []),
        "protocol": consensus_state.get("protocol", "brc"),
    }


def _get_concurrent_status(pipeline: Pipeline, slice_id: str | None = None) -> dict | None:
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

    ``slice_id``: in a slice-DAG implement phase each slice runs its own
    BRC consensus, keyed ``{pipeline_id}/{slice_id}``. The bare pipeline
    id has no tracker, so a non-slice lookup reported a misleading
    cross-slice reconstruction (#2761). Callers querying a per-slice
    agent's consensus must pass that agent's ``slice_id``; the consensus
    block then reflects exactly that slice's tracker. When omitted, only
    pipeline-level (non-slice) consensus is reported in ``consensus``;
    a slice-DAG pipeline queried without a slice yields no ``consensus``
    block rather than a fabricated one. Instead, live slice-scoped
    trackers are surfaced under ``slice_consensus`` keyed by slice_id
    (#3481), so operators still see each active round's real state.
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

        tracker = get_peer_consensus_tracker(pipeline.id, slice_id)
        if not tracker:
            # Attempt lazy reconstruction from message store for concurrent
            # pipelines. ``slice_id`` scopes the replay to one slice's
            # tracker; without it, only pipeline-level messages replay so a
            # slice-DAG pipeline does not reconstruct cross-slice (#2761).
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
                    tracker = reconstruct_tracker_from_messages(
                        pipeline.id,
                        graph,
                        slice_id=slice_id,
                        phase=pipeline.current_phase.value,
                    )
            except ImportError:
                pass  # Fall through to legacy evaluator
            except Exception as e:
                logger.warning(
                    "Tracker reconstruction failed",
                    error=str(e),
                    pipeline_id=pipeline.id,
                    slice_id=slice_id,
                )
        if tracker:
            consensus_state = tracker.get_state()
        else:
            # No BRC tracker available (slice-scoped query for a slice with
            # no tracker yet, or a non-concurrent pipeline). The legacy
            # ConsensusEvaluator was removed under cq-5 of #2777, so there
            # is no fallback evaluator to consult. Report no consensus
            # block; callers (e.g. the MCP get_consensus_status tool) fall
            # back to message-based inference per the existing #1229 path.
            consensus_state = None
    except ImportError:
        logger.debug("Peer consensus tracker not available for status")
        consensus_state = None

    if consensus_state is not None:
        result["consensus"] = _consensus_block(consensus_state)
    else:
        # Don't populate consensus with empty placeholder — callers (e.g. the
        # MCP get_consensus_status tool) use truthiness to decide whether to
        # fall back to message-based inference.  An empty-but-truthy dict
        # prevents that fallback from triggering (see issue #1229).
        pass

    # Slice-id-less observability (#3481): in a slice-DAG implement phase
    # the live trackers are keyed ``{pipeline_id}/{slice_id}``, so the
    # pipeline-level lookup above finds nothing and an operator querying
    # without a slice scope saw no structured consensus at all; the only
    # way to see tracker state was tailing orchestrator pod logs. Surface
    # each active slice's real snapshot, explicitly keyed by slice. This
    # is NOT the #2761 cross-slice "soup" (that was mingling every
    # slice's messages into ONE inferred tracker); the pipeline-level
    # ``consensus`` block above still never reflects a slice tracker.
    if slice_id is None:
        try:
            try:
                from peer_consensus import get_slice_trackers
            except ImportError:
                from ..peer_consensus import get_slice_trackers  # type: ignore[no-redef]

            slice_trackers = get_slice_trackers(pipeline.id)
        except ImportError:
            slice_trackers = {}
        slice_consensus: dict[str, dict] = {}
        for sid in sorted(slice_trackers):
            try:
                slice_consensus[sid] = _consensus_block(slice_trackers[sid].get_state())
            except Exception as e:  # noqa: BLE001 - one bad slice must not hide the rest
                logger.warning(
                    "Slice consensus snapshot failed",
                    pipeline_id=pipeline.id,
                    slice_id=sid,
                    error=str(e),
                )
        if slice_consensus:
            result["slice_consensus"] = slice_consensus

    # Agent lifecycle info from the phase execution record — shows which agents
    # are spawned for the current phase and their container-level status.
    # Includes ``container_id`` and server-computed ``elapsed_seconds`` so the
    # sandboxed overseer can anchor stall-duration math on the live container's
    # ``started_at`` rather than pre-restart message-bus events (issue #2084).
    current_phase_name = pipeline.current_phase.value
    phase_exec = pipeline.phases.get(current_phase_name)
    agents_info: list[dict[str, Any]] = []
    if phase_exec and hasattr(phase_exec, "agents"):
        now = datetime.now(UTC)
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

        # When the persisted phase-agent list is empty, backfill the
        # running-pod view from live Job labels (#3230). Under the
        # orchestrator-owned event loop (#3164) on-demand one-shot pods are
        # never persisted into ``phase_exec.agents``, so without this the
        # overseer's stall-duration math and the dashboard see "0 running
        # agents" while role pods are demonstrably ``Running``. Empty stays
        # empty when no pod is live, so legitimate between-spawn quiescence is
        # not misreported as a cohort.
        if not agents_info:
            agents_info = _live_event_agents(pipeline.id, slice_id)
        result["agents"] = agents_info

    return result


# Human-focused companion drafts (mandatory, produced by the simplifier).
# Resolved through the same artifact-spec registry as the agent drafts so
# the path knowledge lives in exactly one place. Kept separate from
# ``_get_draft_path`` (which is pinned byte-for-byte by a consistency test
# and switches on the real phase value) rather than overloading its phase
# argument with a synthetic ``refine-human`` key.


# Subset of BRC_HISTORY_TYPES that the orchestrator's CONSENSUS_* signal
# handlers tag with ``metadata['slice_id']`` for slice-aware implement
# pipelines (#2548). The implement-phase BRC writer treats a missing
# ``slice_id`` on these as a contract violation (drop with WARNING),
# while the remaining BRC_HISTORY_TYPES (HEARTBEAT, STATUS, HANDOFF,
# AGENT_FAILED, NUDGE, OVERSEER_ALERT) come from emitters that do not
# uniformly carry slice scope — those are routed to the unattributed
# sibling file rather than dropped, so the audit trail stays complete.


# --- #3393 slice-5: cross-repo merge-sequencing HITL holds -------------------
# Stable discriminator prefix on the cross-repo-hold Decision question so
# (a) the poll can idempotently detect an already-registered hold for a
# gate across reconciler ticks / orchestrator restarts, and (b) a future
# dispatch handler in ``routes/decisions.py`` can route on the literal
# substring without a separate context field on the contract Decision.
_CROSS_REPO_HOLD_MARKER_PREFIX = "[#3393 cross-repo-hold"


_CROSS_REPO_HOLD_REASON_TEXT = {
    "closed_unmerged": (
        "the upstream cross-repo PR was CLOSED without merging, so the "
        "automated merge-state hold cannot auto-ready this slice's PR"
    ),
    "timeout": (
        "the upstream cross-repo PR did not merge within the poll bound, so "
        "the automated merge-state hold timed out rather than leaving this "
        "slice's PR draft indefinitely"
    ),
    "beyond_merge_state": (
        "the plan declared this cross-repo dependency a beyond-merge-state "
        "condition (release/publish, version-pin, or cannot-continue block), "
        "which is released by human decision, never automated detection"
    ),
}


# The two operator-selectable options on a cross-repo hold Decision. The
# RELEASE option readies the PR; the KEEP option leaves it draft for manual
# handling. Kept as constants so the registration (options list) and the
# resolution reader agree on one shape.
_CROSS_REPO_HOLD_RELEASE_OPTION_ID = "opt-release"
_CROSS_REPO_HOLD_RELEASE_OPTION_LABEL = "Release the hold and mark the PR ready"
_CROSS_REPO_HOLD_KEEP_OPTION_ID = "opt-keep"
_CROSS_REPO_HOLD_KEEP_OPTION_LABEL = "Keep the PR held for manual handling"


def _build_slice_diff_summary(
    pipeline,
    spawner: "ContainerSpawner",  # noqa: UP037
    worktree_repo_path: Path,
    integration_branch: str,
    parent_branch: str,
    gateway_mode: Literal["public", "private"] = "public",
) -> tuple[list[str] | None, str | None]:
    """Compute commit subjects + diffstat for a slice PR body (#3115).

    The slice PR body's task list is plan-derived — it describes intent,
    not what the pushed branch actually contains. This helper reads the
    real git state so ``create_slice_pr`` can render a ``## What's in
    this PR`` section: the slice's commit subjects
    (``git log origin/<parent>..origin/<head>``) and a diffstat against
    the merge base (``git diff --stat origin/<parent>...origin/<head>``,
    three-dot to match GitHub's PR diff semantics).

    Both remote-tracking refs are refreshed first via
    ``GatewayClient.fetch_branch`` — the slice's agents push directly to
    origin, so the orchestrator worktree's tracking refs may lag (same
    pattern as :func:`_commit_slice_brc_history_to_integration_branch`,
    which runs immediately before this in the slice loop). ``gateway_mode``
    must be threaded from the pipeline-computed mode at the call site;
    defaulting to ``public`` against a private/internal repo causes the
    gateway to refuse the session and the whole diff section silently
    no-ops.

    Strictly best-effort: returns ``(None, None)`` on any failure
    (fetch, git error, timeout) and never raises — a missing diff
    summary must not block slice PR creation.
    """
    pipeline_id = pipeline.id
    try:
        for branch in (parent_branch, integration_branch):
            # ``fetch_branch`` swallows exceptions and returns False;
            # a stale parent ref degrades the diffstat, it doesn't
            # break it, so we just continue.
            spawner.gateway.fetch_branch(
                pipeline_id,
                str(worktree_repo_path),
                args=[f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                mode=gateway_mode,
            )

        git_base = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            f"safe.directory={worktree_repo_path}",
            "-C",
            str(worktree_repo_path),
        ]
        span = f"origin/{parent_branch}..origin/{integration_branch}"
        log_proc = subprocess.run(
            [*git_base, "log", "--no-merges", "--format=%s", span],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        commit_subjects = (
            [line.strip() for line in log_proc.stdout.splitlines() if line.strip()]
            if log_proc.returncode == 0
            else None
        )
        # ``--stat=100,80,40``: 100-col output, then git truncates past
        # 40 entries with an ellipsis line — a slice touching hundreds
        # of files must not produce a body longer than the task dump
        # this section exists to displace.
        diff_proc = subprocess.run(
            [
                *git_base,
                "diff",
                "--stat=100,80,40",
                f"origin/{parent_branch}...origin/{integration_branch}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        diffstat = diff_proc.stdout.strip() if diff_proc.returncode == 0 else None
        if not commit_subjects and not diffstat:
            return None, None
        return commit_subjects or None, diffstat or None
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "Slice diff summary failed (slice PR opens without it) (#3115)",
            pipeline_id=pipeline_id,
            integration_branch=integration_branch,
            parent_branch=parent_branch,
            error=str(err),
        )
        return None, None


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

# Permissive subagent-exploration guidance for producer prompts (#2814).
# Producers may delegate deep grep/Read exploration to the Claude Code
# `general-purpose` subagent so large tool-result payloads don't accumulate
# in the producer's main context window. Mitigates the failure surface of
# #2804 (Agent SDK 1MB JSON buffer overflow). Reused across all seven
# producer prompts so the wording stays uniform.
#
# `general-purpose` is the only subagent the Agent SDK ships out of the
# box; we deliberately do not name `Explore` here because the sandbox
# runtime does not register an `Explore` AgentDefinition (no `agents=`
# on ClaudeAgentOptions and no filesystem `.claude/agents/Explore.md`),
# so the example would burn a turn on an unknown-subagent retry.
_EXPLORATION_SUBAGENT_HEADER = "## Subagent use for exploration"
_EXPLORATION_SUBAGENT_GUIDANCE = [
    f"{_EXPLORATION_SUBAGENT_HEADER}\n",
    "You **may** use the Agent tool (`subagent_type: general-purpose`) "
    "when exploration would otherwise dominate your context window. Use "
    "your judgment — a one-off grep or short read doesn't need a "
    "subagent; deep investigation of a large file or many call sites "
    "usually does. The producer's main context stays lean for synthesis; "
    "the subagent returns a focused summary.\n",
    "Example signals where subagent use often pays off:",
    "- More than ~3 grep/read calls on the same target file or directory.",
    "- Walking a primitive's call sites — delegate; ask for `file:line` "
    "citations + a few lines of context.",
    "- Reading large files (> ~500 lines) — get a subagent summary first; "
    "only `Read` the main file yourself if the summary identifies specific "
    "line ranges you need to author at.\n",
    "Subagent summaries are part of your authoritative work. Verify "
    "critical claims (e.g. `file:line` citations) before committing them "
    "to your output.",
    "",
]


def _build_phase_iteration_context(
    operator_directives: list[OperatorDirective] | None,
    iteration_history: list[IterationSummary] | None,
) -> str:
    """Render operator directives + prior iteration history as a prompt section.

    Issued in iteration N+1 prompts (for **both** producers and reviewers)
    after one or more HITL phase-gate kickbacks. Replaces the unstructured
    ``## Review Feedback`` rendering that previously squatted on the
    agentic-cycle feedback channel — operator directives now have their own
    section with explicit precedence prose so reviewers cannot faithfully
    NACK a directive-driven change against a stale default rubric (#2795).

    Returns an empty string when there are no directives and no history
    so the caller can unconditionally append the result.
    """
    directives = operator_directives or []
    history = iteration_history or []
    if not directives and not history:
        return ""

    lines: list[str] = ["## Phase Iteration Context\n"]
    if directives:
        lines.append(
            "The operator has kicked this phase back through HITL one or "
            "more times. The directives below **override prompt-template "
            "defaults**. If a rubric item in your role's instructions "
            "conflicts with a directive, the directive wins. Later "
            "directives override earlier ones.\n"
        )
        lines.append("### Operator Directives (chronological)\n")
        for idx, directive in enumerate(directives, start=1):
            ts = directive.created_at.isoformat()
            lines.append(f"**Directive {idx}** (iteration {directive.iteration_n}, {ts}):")
            lines.append("")
            lines.append(directive.feedback_text.rstrip())
            lines.append("")

    if history:
        lines.append("### Prior Iteration History\n")
        lines.append(
            "Each entry below is a frozen snapshot of a previously kicked-"
            "back iteration's BRC outcome — what the reviewers concluded "
            "and why. Use it to see which rubric items tripped last round "
            "so you do not repeat the same NACKs.\n"
        )
        for summary in history:
            ts = summary.completed_at.isoformat()
            lines.append(f"**Iteration {summary.iteration_n}** (completed {ts}):")
            if summary.final_proposal_commit:
                # SHAs are pre-filtered by _build_iteration_summary_from_tracker
                # (empty + RECONSTRUCTED_NO_SHA dropped before the dict is
                # populated), so every value here is a real commit.
                commit_parts = [
                    f"{producer}={sha[:12]}"
                    for producer, sha in sorted(summary.final_proposal_commit.items())
                ]
                lines.append(f"- Final proposal commits: {', '.join(commit_parts)}")
            if summary.verdict_matrix:
                verdicts = "; ".join(
                    f"{edge}: {state}" for edge, state in sorted(summary.verdict_matrix.items())
                )
                lines.append(f"- Verdict matrix: {verdicts}")
            if summary.nack_reasons:
                lines.append(f"- NACK reasons ({len(summary.nack_reasons)}):")
                for reason in summary.nack_reasons:
                    lines.append(f"  - {reason}")
            if summary.artifacts_snapshot:
                arts = ", ".join(sorted(summary.artifacts_snapshot.keys()))
                lines.append(f"- Artifacts at iteration close: {arts}")
            lines.append("")

    return "\n".join(lines)


def _build_iteration_summary_from_tracker(
    tracker: Any,
    iteration_n: int,
    artifacts: dict[str, str] | None = None,
    completed_at: datetime | None = None,
) -> IterationSummary:
    """Capture an :class:`IterationSummary` from a live BRC tracker.

    Called by the HITL kickback handler **before** ``_clear_concurrent_state``
    wipes the tracker so the iteration N+1 prompt can render what tripped
    iteration N. Tolerates a ``None`` tracker — returns a summary with only
    the iteration index + completion timestamp populated, which still lets
    downstream prompts mention that a kickback occurred without claiming
    false verdict detail.
    """
    completion = completed_at or datetime.now(UTC)
    summary = IterationSummary(
        iteration_n=iteration_n,
        completed_at=completion,
        artifacts_snapshot=dict(artifacts or {}),
    )
    if tracker is None:
        return summary

    try:
        matrix = getattr(tracker, "matrix", None)
        if matrix is None:
            return summary
        # Snapshot the matrix entries + commit SHAs under the tracker's
        # lock so concurrent mutations from a still-live tracker can't
        # tear the read. RLock means re-entry is safe if callers already
        # hold it. Iteration below runs on the local copies.
        lock = getattr(tracker, "_lock", None)
        commits_snapshot: dict[str, str] = {}
        if lock is not None:
            with lock:
                entries_snapshot = list(getattr(matrix, "_entries", {}).items())
                commits_snapshot = dict(getattr(tracker, "_proposal_commit_shas", {}))
        else:
            entries_snapshot = list(getattr(matrix, "_entries", {}).items())
            commits_snapshot = dict(getattr(tracker, "_proposal_commit_shas", {}))

        verdict_matrix: dict[str, str] = {}
        nack_reasons: list[str] = []
        for (reviewer, producer), entry in entries_snapshot:
            state = getattr(entry, "state", None)
            state_val = state.value if state is not None else "unknown"
            verdict_matrix[f"{reviewer}->{producer}"] = state_val
            if state_val == "nacked" and getattr(entry, "reason", ""):
                nack_reasons.append(f"{reviewer}→{producer}: {entry.reason}")
        summary.verdict_matrix = verdict_matrix
        summary.nack_reasons = nack_reasons

        producers = {producer for _, producer in (k for k, _ in entries_snapshot)}
        commits: dict[str, str] = {}
        for producer in producers:
            sha = commits_snapshot.get(producer, "")
            if sha and sha != "RECONSTRUCTED_NO_SHA":
                commits[producer] = sha
        summary.final_proposal_commit = commits
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "Failed to snapshot iteration summary from tracker",
            iteration_n=iteration_n,
            error=str(e),
        )
    return summary


def _apply_inline_hitl_kickback_to_phase(
    phase_execution: PhaseExecution,
    revision_feedback: str,
    tracker: Any = None,
) -> list[ContainerInfo]:
    """Apply the inline HITL kickback's phase-state mutations.

    Extracted from the inline ``request_changes`` handler so tests can
    drive the assertion through production code rather than constructing
    a fixture by hand (#2795 review). The caller is still responsible for
    the wrapping concerns: clearing the message store + consensus tracker
    via ``_clear_concurrent_state``, persisting the pipeline via
    ``store.save_pipeline``, and stopping the stale containers returned
    here (the K8s delete is asynchronous so an explicit stop is required
    to avoid iteration N+1 racing iteration N's still-terminating pods).

    Returns the snapshot of containers that were running at kickback
    time, for the caller to issue the defensive stop on.
    """
    # Monotone across the legacy-hitl_feedback migration boundary: a
    # pre-#2795 phase migrates with iteration_history empty but a
    # synthetic OperatorDirective carrying iteration_n derived from
    # hitl_review_cycles. ``len(iteration_history)`` alone would
    # restart at 0 and label two distinct iterations identically; use
    # one past the maximum existing directive index as the floor so
    # the displayed "iteration X" labels stay monotone.
    iteration_n = max(
        len(phase_execution.iteration_history),
        max(
            (d.iteration_n for d in phase_execution.operator_directives),
            default=-1,
        )
        + 1,
    )
    phase_execution.operator_directives.append(
        OperatorDirective(
            iteration_n=iteration_n,
            feedback_text=revision_feedback,
        )
    )
    phase_execution.iteration_history.append(
        _build_iteration_summary_from_tracker(
            tracker,
            iteration_n=iteration_n,
            artifacts=phase_execution.artifacts,
        )
    )
    stale_containers = list(phase_execution.containers)
    phase_execution.containers = []
    phase_execution.agents = []
    phase_execution.artifacts = {}
    phase_execution.review_cycles = 0
    return stale_containers


def _broadcast_hitl_nonconvergence_alert(
    pipeline_id: str,
    pipeline: Pipeline,
    current_phase: PipelinePhase,
    cycles: int,
    threshold: int,
) -> None:
    """Non-fatal overseer alert when the HITL converge loop runs long (#3392).

    The converge-before-advance loop is human-gated every round (the
    operator resolves decisions before each re-run), so a long-running loop
    cannot burn compute silently and is never force-advanced. After
    ``threshold`` rounds we surface an ``OVERSEER_ALERT`` so a pathological
    non-convergence — a real carry-forward bug, or a genuinely churning
    design — is visible. Best-effort: a broadcast failure never blocks the
    re-run.
    """
    try:
        from message_store import Message, MessageType

        store_fn = _get_message_store()
        if store_fn is None:
            return
        msg_store = store_fn()
        phase = current_phase.value if current_phase else None
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.OVERSEER_ALERT,
                subject="hitl_nonconvergence: orchestrator [medium]",
                body=(
                    f"The {phase} phase HITL converge-before-advance loop has run "
                    f"{cycles} rounds (>= {threshold}) without reaching a fixpoint. "
                    f"Each round is human-gated, so this is surfaced for visibility, "
                    f"not force-advanced. Investigate whether a decision keeps "
                    f"re-surfacing (carry-forward bug) or the design is genuinely "
                    f"churning. See #3392."
                ),
                metadata={"reason": "hitl_nonconvergence", "cycles": cycles},
                phase=phase,
            )
        )
    except Exception as alert_err:  # noqa: BLE001
        logger.warning(
            "Failed to broadcast HITL non-convergence alert (non-fatal)",
            pipeline_id=pipeline_id,
            error=str(alert_err),
        )


def _perform_hitl_phase_rerun(
    *,
    store: Any,
    spawner: Any,
    pipeline: Pipeline,
    phase_execution: PhaseExecution,
    pipeline_id: str,
    current_phase: PipelinePhase,
    feedback_text: str,
    event_message: str,
) -> None:
    """Tear down the current phase iteration and arm a re-run (#3392).

    Shared by the two re-run triggers in the converge-before-advance HITL
    loop: the operator-feedback kickback (``request_changes`` /
    ``change_approach``) and the decision-driven re-run that folds resolved
    HITL answers back into the phase documents. Snapshots the BRC tracker
    for the next iteration's prompt, appends the operator directive +
    iteration summary (#2795), clears concurrent state so the re-run does
    not short-circuit on stale ``CONSENSUS_CONFIRMED`` messages (#1296),
    persists, and stops the stale containers (the K8s delete is async, so an
    explicit idempotent stop prevents iteration N+1 racing iteration N's
    still-terminating pods).

    The caller must already hold the pipeline state lock, have set the
    pipeline/phase status back to RUNNING, and incremented
    ``phase_execution.hitl_review_cycles``. The caller issues the
    ``continue`` that re-enters the outer loop.
    """
    # Capture the BRC tracker state BEFORE _clear_concurrent_state drops
    # it — that's our only chance to snapshot this iteration's verdicts for
    # the next iteration's prompt.
    rerun_tracker = None
    try:
        from peer_consensus import get_peer_consensus_tracker as _gpct

        rerun_tracker = _gpct(pipeline_id)
    except Exception as tracker_err:  # noqa: BLE001
        logger.debug(
            "Tracker lookup failed during HITL re-run snapshot",
            pipeline_id=pipeline_id,
            error=str(tracker_err),
        )

    stale_containers = _apply_inline_hitl_kickback_to_phase(
        phase_execution,
        feedback_text,
        tracker=rerun_tracker,
    )

    from routes.phases import _clear_concurrent_state

    _clear_concurrent_state(pipeline_id)

    store.save_pipeline(pipeline)

    for _ctr in stale_containers:
        if _ctr.container_id and _ctr.status == ContainerStatus.RUNNING:
            try:
                spawner.backend.stop_container(_ctr.container_id, timeout=10)
            except Exception as stop_err:  # noqa: BLE001
                logger.debug(
                    "Best-effort HITL re-run teardown failed",
                    pipeline_id=pipeline_id,
                    container_id=_ctr.container_id,
                    error=str(stop_err),
                )

    report_pipeline_status(
        pipeline,
        event_type="phase.revision_requested",
        message=event_message,
    )
    _emit_pipeline_event(pipeline, "phase.revision_requested")


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
    operator_directives: list[OperatorDirective] | None = None,
    iteration_history: list[IterationSummary] | None = None,
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

    # --- Phase iteration context (HITL kickbacks) ---
    # Operator directives have their own section with explicit precedence
    # prose so reviewers cannot faithfully NACK a directive-driven change
    # against a stale default rubric. See issue #2795.
    iteration_context = _build_phase_iteration_context(operator_directives, iteration_history)
    if iteration_context:
        lines.append(iteration_context)

    # --- Prior review feedback (agentic revision cycles only) ---
    # Scoped to agentic-cycle review feedback since #2795 — HITL kickback
    # feedback now flows through ``operator_directives`` / the iteration
    # context section above.
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
                "7. Surface the questions and uncertainties that genuinely need a "
                "human to answer — see `## How to Populate Open Questions` below for "
                "the filter (slice/PR packaging, implementation strategy, and "
                "API/schema details belong to the planner, not the refiner)",
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
                "`## Additional Context` covers. This skip rule is NARROW: it covers "
                "only answers THIS pipeline's operator recorded in "
                "`## Additional Context`. It never covers decisions the task "
                "description names as operator-owned, and never answers inherited "
                "from a prior or cancelled run's seeded context — register those "
                "(see the next rule).\n",
                "**Task-named decisions are non-optional (#3462).** If the task "
                "description or contract names specific decisions as the operator's "
                "to make — or contains any directive to surface decisions as HITL "
                "questions — you MUST register each one via "
                "`egg-contract add-decision`, even when you believe prior context "
                "already resolves it, or that it is non-blocking or deferred. "
                "Belief about resolution is a *recommended disposition*, not a "
                "reason to skip registration: make your recommended answer the "
                "first option (suffix its label with `(recommended)`) and cite the "
                "resolving context in that option's description, so the operator "
                "can confirm in one click while retaining the authority to choose "
                "differently. Documenting a decision in draft prose is a "
                "supplement to registration, never a substitute — unregistered "
                "decisions never reach the operator's decision surface.\n",
                "Surface uncertainties, ambiguities, and assumptions **that genuinely "
                "need a human to answer**. Filter ruthlessly: a good open question is "
                "one the operator must answer because the answer changes what we're "
                "building. A bad open question is one the planner phase will decide on "
                "its own once it sees the analysis — those waste the operator's "
                "attention and pre-anchor the planner. Err toward registering questions "
                "about *what the problem actually is* and *what's in or out of scope* "
                "rather than *how to build it*.\n",
                "**Out of scope for refine open questions** — do NOT register decisions "
                "about:\n"
                "- **Work decomposition / slice-DAG shape / PR packaging** — "
                "**Slice / PR packaging is NOT a refine-phase decision.** The "
                "plan phase owns slice-DAG construction (see "
                "`docs/architecture/slice-dag.md`) and the operator approves the "
                "proposed slice shape at the plan HITL gate. Do not register "
                "`add-decision` items asking how the work should be sliced, how "
                "many PRs to ship, or which parts should run in parallel. If "
                "the task obviously spans multiple parts, name them in Problem "
                "Statement or Constraints — the planner will propose a shape "
                "from the analysis it reads.\n"
                "- **Implementation strategy choices** that the planner can decide "
                'from Problem Statement + Constraints (e.g. "which migration '
                'approach", "which fallback design", "which detector shape"). '
                "Surface these as Options Considered / Recommended Approach in the "
                "analysis prose, not as `add-decision` items.\n"
                "- **API / schema details** the planner phase will work out once it "
                "starts designing. If the operator must constrain the API shape, "
                "frame it as a *constraint* in `## Constraints`, not an open question.\n"
                "Register questions when the answer is a fact only the human knows "
                "(product intent, scope boundaries, external commitments, "
                "user-visible behavior) — not when the answer is a design call the "
                "planner will make.\n",
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
                "**Advisory seam-listing is fine** — if the task obviously spans "
                "independently-implementable parts, you MAY name them in Problem "
                'Statement or Constraints (e.g. "the change touches the gateway, '
                'the orchestrator, and the sandbox") so the planner has the seam '
                "information. Make it **explicitly advisory**: the planner is free "
                "to slice differently if it sees a better seam. Do not pre-number "
                "parts as `slice-1 / slice-2`, do not draw a DAG, and do not pick "
                "a 1-PR-vs-3-PR shape — those choices belong to the planner.\n",
                "**DO NOT:**",
                "- Write questions as plain markdown text without running "
                "`egg-contract add-decision` or `egg-contract add-feedback`",
                "- Use custom HTML comment markers like "
                "`<!-- DECISION: ... -->` instead of the contract CLI",
                "- Skip registration because you think the questions are minor — "
                "register every question",
                "- Skip registration because you believe a decision is already "
                "resolved, non-blocking, or deferred — register it with your "
                "recommended disposition instead (#3462)",
                "- Attest `no_decisions_rationale` when the task names decisions "
                "to surface — the attestation is presented to the operator as its "
                "own confirmable decision, and a rejected 'none' sends the phase "
                "back for a re-run (#3462)",
                "- Transcribe this `## How to Populate Open Questions` section "
                "into your analysis document — it is meta-guidance, not template "
                "content\n",
                "**Attest your decision ledger when proposing (#3390).** Your "
                "consensus propose is REJECTED unless its attestation carries "
                "the ledger: `--decisions-registered cq-1 cq-2 ...` (every id "
                "you registered this phase) or "
                '`--no-decisions-rationale "<why>"` when you deliberately '
                "registered none. Attested ids must exist on the contract for "
                "this phase, and the draft must cite each one — the "
                "`--format markdown` output you copied above embeds the id, so "
                "the registration flow satisfies the citation automatically. "
                "If your only open questions went into an `add-feedback` "
                "request (no `cq-N` decisions), attest the rationale form and "
                "name the feedback request in it. This is what lets the "
                "operator trust that an empty gate means *deliberately no "
                "decisions*, not *forgot to register*. The explicit-none form "
                "is not a shortcut (#3462): the orchestrator surfaces it to "
                "the operator as its own confirmable decision before the "
                "phase gate, and it is only valid when the phase genuinely "
                "raises no meaningful decision — never when the task names "
                "decisions to surface, and never as a substitute for "
                "registering a decision you believe is already resolved.\n",
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
        lines.extend(_EXPLORATION_SUBAGENT_GUIDANCE)
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
                "Create a detailed implementation plan, decomposing the work into "
                "slices per the slice-DAG guidance at the end of this section. The "
                "implement-phase pipeline ships each slice as its own stacked PR. "
                "**Slice shape is your call.** A single-slice plan is fine when the "
                "work is cohesive; pick a multi-slice shape when the work has clean "
                "seams that ship independently. If the refine analysis sketched a "
                "decomposition (e.g. naming the components touched), treat it as "
                "**advisory context** — you are free to slice differently if a "
                "better seam exists. The only thing that binds your slice shape is "
                "an explicit slice-DAG HITL decision recorded by the operator on "
                "the contract; if you believe such a decision is wrong, raise it as "
                "an open question in your plan rather than silently overriding.",
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
                "slices:",
                "  - id: 1",
                "    name: |-",
                "      Slice Name",
                "    goal: |-",
                "      What this slice achieves, written for a reviewer of the",
                "      target repo. This text is rendered verbatim as the lead",
                "      paragraph of the slice's PR body (#3115), so keep it 1-3",
                "      plain-language sentences with no plan-internal",
                "      cross-references (reviewer codes, section numbers, draft",
                "      version markers).",
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
                "Do NOT use a `pr_plan` key — slice packaging is owned by the "
                "slice-DAG section below, not by an ad-hoc PR list.",
                "",
                "The `test_plan` field is **required** — describe both automated test "
                "coverage and any manual verification steps. The `manual_steps` field "
                "should list any pre-merge or post-merge actions required by the reviewer "
                "or deployer; use an empty string if none.",
                "",
                # ----------------------------------------------------
                # #2137 — slice-DAG planner guidance (mirrors the
                # concurrent task_planner block; keep the two paths
                # aligned so the slice-shape rules behave the same way
                # regardless of which planner runs).
                # ----------------------------------------------------
                "## Slice-DAG guidance (#2137)",
                "",
                "The implement-phase pipeline ships each plan **slice** (formerly "
                "**phase**) as its own stacked PR. The plan you emit drives that "
                "DAG; the rules below are mandatory.",
                "",
                "**Yaml key swap**: prefer the canonical ``slices:`` key in your "
                "``# yaml-tasks`` block (the parser also accepts ``phases:`` for "
                "backward compatibility). New plans should use ``slices:``.",
                "",
                "**Slice-sizing NACK (hard, judgment-based — #2809)**: the plan "
                "reviewer will hard-NACK an oversized slice. Use judgment when "
                "shaping — no fixed LOC budget, but avoid bundling more than ~3 "
                "distinct file-categories in one slice, avoid combining "
                "deletion-heavy work with new-API-introduction work, avoid "
                "slices that would require >3–4 commit-propose-revise cycles, "
                "and avoid bundling independent task groups with no internal "
                "dependency. Subdivide along those seams up front rather than "
                "earning a NACK.",
                "",
                "**Forest constraint (HARD)**: every slice must have at most ONE "
                "DAG parent — the implement-phase pipeline ships every slice as a "
                "stacked PR with exactly one base branch. Multi-parent slices "
                "break the stacking invariant and are rejected at plan ingestion.",
                "",
                "**Auto-serialization rule for would-be multi-parent slices**: "
                "when a slice would naturally have >1 parents, serialise the "
                "upstream slices into a linear chain and record the chosen "
                "ordering on the downstream slice's ``serialized_chain_order`` "
                "field. The list names the upstream slice IDs in their chosen "
                "serialization order.",
                "",
                "**File-overlap rule (HARD, enforced at plan ingestion — "
                "#3046)**: two slices that touch the SAME file must be ordered "
                "on one dependency chain — one a transitive ``dependencies`` "
                "ancestor of the other — never left as parallel roots or "
                "siblings. The implement phase cuts each slice's branch off "
                "its dependency parent, so an unordered overlapping pair forks "
                "independently off the shared base and its edits to the shared "
                "file collide at integration (a guaranteed modify/delete "
                "conflict). Deletion/retirement slices are the classic trap: a "
                "slice that removes a file must depend on every slice that "
                "modifies it. Slices with disjoint file sets stay parallel.",
                "",
                "**Test co-location rule (HARD — #3411)**: a slice that "
                "removes, renames, or rewrites code carries the matching "
                "test updates (skip-guards, deletions, rewrites) in the SAME "
                "slice — never a later one — with the test files listed in "
                "that slice's task ``files:``. Each cumulative slice tip "
                "must be independently green: the per-slice green gate "
                "(#3398) runs the repo's checks at the slice tip before the "
                "PR opens and blocks while any check is red, so deferring "
                "test obsolescence to a later slice guarantees a blocked "
                "slice. Discover the tests that statically reach the "
                "changed files with the changeset-aware selector where the "
                "repo ships it (this repo: ``python3 "
                "scripts/select_tests/__main__.py --impacted-tests "
                "<file>...``; exit 2 = closure unavailable — fall back to "
                "grepping the removed symbols in the test trees).",
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
                "Your judgement is the source of truth. The fallback heuristic "
                "when you have no preference is: cluster would-be parents by "
                "``files_affected`` Jaccard overlap (>0.3), then order by "
                "descending downstream fan-out.",
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
            lines.extend(_EXPLORATION_SUBAGENT_GUIDANCE)
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


def _contract_enforcer_role_names() -> frozenset[str]:
    """Roles whose ACK/CONFIRM is gated on contract-task completeness (#3114).

    Lazy wrapper so the preamble builder keys its enforcer-specific
    instructions off the same capability set the orchestrator's signal
    gate enforces (``egg_contracts.agent_roles.CONTRACT_ENFORCER_ROLES``)
    — prose and enforcement stay in lockstep.
    """
    from egg_contracts.agent_roles import CONTRACT_ENFORCER_ROLE_NAMES

    return CONTRACT_ENFORCER_ROLE_NAMES


def _build_brc_preamble(
    role_value: str,
    phase: str,
    repo: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
) -> str:
    """Build the BRC consensus lifecycle preamble for an agent.

    Returns a formatted string block that can be appended to any agent prompt
    to inject BRC protocol instructions. Used by both the coder/refiner path
    (which delegates to _build_phase_prompt) and the generic multi-agent path.

    Includes:
    - Agent roster showing all active agents and what they produce
    - Role-specific proactive preparation instructions
    - Full BRC lifecycle steps (including the generic no-op propose path,
      #3027, for a producer that finds it has no work in this slice)
    """
    try:
        from review_graph import get_review_graph_for_phase

        graph = get_review_graph_for_phase(phase, repo=repo)
        is_producer = graph.is_producer(role_value)
        is_reviewer = graph.is_reviewer(role_value)
        reviewers = graph.reviewers_for(role_value) if is_producer else []
        producers = graph.producers_for(role_value) if is_reviewer else []
        wake_only_producers = graph.wake_only_producers_for(role_value)
        all_roles = sorted(graph.all_roles())
        graph_available = True
    except Exception:
        is_producer = role_value in (
            "coder",
            "tester",
            "documenter",
            "refiner",
            "architect",
            "task_planner",
            "risk_analyst",
            "simplifier",
        )
        is_reviewer = role_value in (
            "reviewer_code",
            "reviewer_code_holistic",
            "reviewer_contract",
            "tester",
            "reviewer_refine",
            "reviewer_agent_design",
            # first_principles_reviewer is a genuine refine-phase reviewer: it
            # casts a real ACK verdict on the refiner (CRITICAL edge), so the
            # degraded fallback keeps its Reviewer Lifecycle block. It never
            # NACKs (redirects go to the operator as HITL decisions), but it
            # DOES vote, so — unlike the simplifier — it is a real verdict and
            # ``casts_real_verdicts`` (raw ``is_reviewer`` here) stays True.
            "first_principles_reviewer",
            "reviewer_plan",
            # risk_analyst is a genuine dual-role reviewer in the plan graph
            # (CRITICAL reviewer of architect + task_planner, #2809) as well as
            # a producer of the risk register. Listed here so the degraded
            # fallback path keeps its Reviewer Lifecycle / "As a reviewer"
            # block instead of stripping it to producer-only — mirroring the
            # live plan graph. Unlike the simplifier its edges are real
            # verdicts, so ``casts_real_verdicts`` (raw ``is_reviewer`` in the
            # degraded path) correctly stays True for it.
            "risk_analyst",
            # simplifier retains a wake_only advisory edge over the upstream
            # refine/plan producer, so the graph reports it as a reviewer —
            # but it casts no verdict (#3381) and is rendered PRODUCER-ONLY
            # below (the wake_only edge is excluded from the real-reviewer
            # determination). Listed here so the degraded fallback path still
            # recognizes it; producer-only rendering is handled uniformly.
            "simplifier",
        )
        reviewers = []
        producers = []
        wake_only_producers = set()
        all_roles = []
        graph_available = False

    lines: list[str] = [
        "\n\n## CRITICAL: BRC Consensus Protocol\n",
        "You are running in CONCURRENT mode with the Broadcast-Review-Converge "
        "(BRC) protocol. Your job is NOT just your task — it is the **full "
        "BRC lifecycle**.\n",
    ]

    is_dual_role = is_producer and is_reviewer

    # A role whose only reviewed producers are reached via wake_only edges
    # (the de-roled simplifier, #3381) casts no verdict on anyone, so it is a
    # PRODUCER in every behavioural sense — render it as one. We keep the
    # graph-level ``is_dual_role`` flag intact for the banner dispatch below;
    # only the rendered role-type label and the "assigned producers" line
    # exclude wake_only producers, so the preamble does not contradict the
    # producer-only execution banner the simplifier receives.
    real_producers = [p for p in producers if p not in wake_only_producers]
    if graph_available:
        casts_real_verdicts = bool(real_producers)
    else:
        # Degraded path: the graph load failed, so ``producers == []`` for every
        # role and we cannot distinguish wake_only edges from real ones. Fall
        # back to raw ``is_reviewer`` so we don't silently strip the Reviewer
        # Lifecycle / "As a reviewer" coordination block from a *genuine*
        # reviewer (reviewer_code/refine/plan) when the graph is unavailable —
        # pre-#3381 this path gated those blocks on raw ``is_reviewer``. The
        # simplifier — the only wake_only role — stays producer-only: it is
        # excluded here, and is independently rendered producer-only by the
        # ``is_dual_role and role_value == "simplifier"`` banner dispatch, which
        # still fires in the fallback.
        casts_real_verdicts = is_reviewer and role_value != "simplifier"

    if is_producer and casts_real_verdicts:
        role_type_desc = "PRODUCER and REVIEWER (dual role)"
    elif is_producer:
        role_type_desc = "PRODUCER"
    elif casts_real_verdicts:
        role_type_desc = "REVIEWER"
    else:
        role_type_desc = "PARTICIPANT"

    lines.append(f"Your role type: **{role_type_desc}**")
    if reviewers:
        lines.append(f"Your reviewers: {', '.join(reviewers)}")
    if real_producers:
        lines.append(f"Your assigned producers: {', '.join(real_producers)}")
    lines.append("")

    # Agent roster: show all active agents and what they do
    if all_roles:
        roster = _build_agent_roster(all_roles, role_value, phase)
        if roster:
            lines.append(roster)

    # Dual-role ordering banner (#2749, updated for coder-owns-tests). A
    # dual-role agent (today: only TESTER in the implement graph) receives
    # both the Producer and Reviewer Lifecycle blocks below. The coder now
    # authors its own tests; the tester's job is to review-and-harden them
    # after the coder proposes. So the tester's producer WORK legitimately
    # depends on the coder's ``CONSENSUS_PROPOSE`` — it orients up-front,
    # exits after ORIENT, and is re-invoked by the event-pump wrapper when
    # the coder proposes, at which point it hardens + proposes + ACK/NACKs
    # in one pass. This does not reintroduce the f4c7d780 / 8b81ed32
    # self-block (where the tester idled on a reviewer wait-loop before
    # proposing its own scaffolded work): the coder proposes independently
    # and does not wait on the tester, so the coder's propose is the
    # trigger, and the tester proposes right after. The tester therefore
    # has TWO reviewer rendezvous points, both surfaced as fresh wrapper
    # invocations under the event-pump model: (a) the coder's first
    # ``CONSENSUS_PROPOSE`` re-invokes the tester so it has something to
    # harden; (b) subsequent re-proposes and peer-producer proposals
    # (after the tester has proposed) likewise re-invoke the tester to
    # handle the Reviewer Lifecycle for those events.
    if is_dual_role and role_value == "simplifier":
        # The simplifier is a PRODUCER ONLY (#3381). It is woken to write the
        # companion by the ordinary producer propose-arm (it self-gates on the
        # upstream draft existing), NOT by its advisory edge over the upstream
        # — that edge is wake_only and casts no verdict, so it is inert in
        # consensus derivation (see review_graph.ReviewEdge.wake_only). It is
        # NOT a reviewer in any behavioural sense: it issues no verdict, casts
        # no ACK/NACK, and never critiques the draft. Treating it as a reviewer
        # is what made the companion come out as a review/critique memo instead
        # of a plain-language summary. So it gets a PRODUCER-ONLY banner here
        # and must NOT inherit the tester's review-and-harden banner below.
        lines.append(
            "### Execution Order (READ FIRST — simplifier)\n\n"
            "You are a **producer only**: your single job is to write a "
            "plain-language, human-focused companion to the upstream "
            "producer's draft. You do **not** review, critique, score, or "
            "vote on that draft — you never issue an ACK or a NACK. (An "
            "internal wake-wire re-invokes you when the upstream proposes so "
            "you know its draft is ready; consensus never waits on a verdict "
            "from you, so there is nothing to respond to.)\n\n"
            "**Execute in this order:**\n\n"
            "1. **ORIENT (FIRST).** Read the contract and orient. Your work "
            "depends on the upstream producer's draft existing, so you begin "
            "writing only once that producer issues `CONSENSUS_PROPOSE` — the "
            "event-pump wrapper re-invokes you carrying that proposal. Do not "
            "race ahead before the draft exists.\n"
            "2. **On the upstream producer's PROPOSE**, the wrapper re-invokes "
            "you with the proposal in your event payload. SYNC the worktree, "
            "read the draft, then write and PROPOSE the human-focused "
            "companion (see Producer role below). That is the whole job — "
            "the companion is a simplified summary written *for humans to "
            "read*, never a review of the draft, a list of constraints the "
            "draft should satisfy, or an ACK/NACK rationale.\n"
        )
    elif is_dual_role:
        lines.append(
            "### Dual-Role Execution Order (READ FIRST — #2749, updated for "
            "coder-owns-tests)\n\n"
            "You are both PRODUCER and REVIEWER (TESTER). **The BRC round "
            "cannot close until every producer (including you) has issued "
            "`mcp__brc__propose` / `egg-orch consensus propose`** — so you "
            "MUST eventually propose, and if you never propose your own "
            "hardening you self-block the round. But your producer WORK "
            "(reviewing and **hardening the coder's tests**) genuinely "
            "depends on the coder's proposed tests existing, so unlike a "
            "normal producer you start that work at the coder's PROPOSE, "
            "not before. This does not deadlock: the coder proposes "
            "independently and does **not** wait on you, so its "
            "`CONSENSUS_PROPOSE` is the trigger that unblocks your work; "
            "the event-pump wrapper re-invokes you carrying that PROPOSE "
            "in your event payload, and you propose right after.\n\n"
            "**Execute the lifecycles in this strict order:**\n\n"
            "1. **Producer ORIENT (step 1) comes FIRST.** Run ORIENT now "
            "to load context. **Your role-specific orientation tells you "
            "whether Producer WORK (step 2) runs immediately or is gated "
            "on an upstream producer's `CONSENSUS_PROPOSE`** — e.g. the "
            "implement-phase tester reviews-and-hardens the coder's tests, "
            "so its WORK begins after the coder proposes (#2936). Do not "
            "race ahead of the role-specific orientation. While you are in "
            "ORIENT, you may *opportunistically* do the Reviewer "
            "Lifecycle's `1. PREPARE` work — read the contract, scan the "
            "upstream producer's commits as they land on the branch — but "
            "do NOT start producing artifacts your role-specific "
            "orientation gates on an upstream PROPOSE. Do NOT block on a "
            "reviewer wait as your scheduling primitive: the event-pump "
            "wrapper invokes you again when the upstream producer's "
            "`CONSENSUS_PROPOSE` arrives, at which point you handle the "
            "review AND (if your WORK was gated on it) start producing.\n"
            "2. **On an upstream producer's PROPOSE**, the wrapper "
            "re-invokes you with the proposal in your event payload. "
            "SYNC the worktree, then do your Producer WORK (read the "
            "coder's tests; add the missing regression + adversarial "
            "cases yourself — you share the test scope with the coder; "
            "run the tests) and **PROPOSE** your hardening. In the same "
            "invocation, issue your reviewer verdict on the coder: ACK "
            "if coverage is sound, or NACK naming the specific failing "
            "test / coverage gap.\n"
            "3. **Subsequent invocations** (re-proposes from any "
            "producer — `CONSENSUS_PROPOSE` version > 1 — and "
            "`CONSENSUS_RE_REVIEW` events) surface as new wrapper "
            "invocations. Each one is a fresh review against the new "
            "delta; the per-event prompt includes the full "
            "`git log {last_reviewed_commit_sha}..HEAD --not "
            "origin/{base_branch} -p` so you can audit the change. "
            "Fall through to Reviewer Lifecycle step 3 (SYNC) → step 4 "
            "(REVIEW) → step 5 (ACK/NACK), then exit. Do NOT skip step "
            "4 (REVIEW) — reading the actual referenced files and "
            "forming independent judgment from them is what keeps "
            "re-reviews from becoming rubber-stamps.\n"
        )

    if is_producer:
        producer_lifecycle: list[str] = ["### Producer Lifecycle"]
        # The no-op propose path (#3027) is only valid in the implement
        # phase. In refine/plan the producer's draft is mandatory and the
        # orchestrator rejects no-op explicitly — so don't even surface the
        # affordance to refine/plan producers (architect, refiner,
        # task_planner, risk_analyst), keeping prose and enforcement in
        # lockstep (review feedback on #3029).
        propose_line = (
            "3. **PROPOSE**: When done, run: "
            '`egg-orch consensus propose --summary "..." --artifacts "file1" "file2" '
            '--files-changed "f1.py" "f2.py" --tests-run "test_a" "test_b" '
            '--tasks "task-1-1" "task-1-2" --commit-sha $(git rev-parse HEAD)`. '
            "The `--summary` must be ≥50 chars of substantive content describing what was "
            "built, what was tested, and which contract tasks it satisfies. "
            "Boilerplate like 'looks good' or 'approved' will be rejected."
        )
        if phase in ("refine", "plan") and role_value in (
            # Keep in lockstep with ``_DECISION_ATTESTING_ROLES`` in
            # ``routes/signals/_validation.py`` — the enforcement side of
            # this prose (#3390).
            "refiner",
            "task_planner",
            "architect",
            "risk_analyst",
        ):
            propose_line += (
                "\n\n"
                "   **Attest your decision ledger (#3390 — MANDATORY).** The "
                "orchestrator REJECTS your propose unless its attestation "
                "carries your HITL decision ledger. Pass "
                "`--decisions-registered cq-1 cq-2 ...` listing every decision "
                "you registered this phase (via `egg-contract add-decision` / "
                "`mcp__sdlc__register_open_question`), or "
                '`--no-decisions-rationale "<why>"` when the phase '
                "deliberately raises none — an explicit empty ledger, never an "
                "omission. (Via MCP: the `attestation` arg of "
                "`mcp__brc__propose`, fields `decisions_registered` / "
                "`no_decisions_rationale`.) Attested ids are cross-checked "
                "against the contract, and your draft must cite each attested "
                "`cq-N` (copying the `--format markdown` output into the "
                "draft satisfies this). A decision your draft commits to "
                "without a registered `cq-N` is a reviewer NACK — register "
                "it or remove the unilateral commitment. The rationale form "
                "is not a shortcut (#3462): the operator is asked to confirm "
                "it as its own decision before the phase gate, and a rejected "
                "'none' re-runs the phase. If the task names decisions to "
                "surface — or you believe a decision is already resolved by "
                "prior context — register it with your recommended answer as "
                "the first option instead of attesting none."
            )
        if phase == "implement":
            propose_line += (
                "\n\n"
                "   **Mark your contract tasks complete (#3114).** Record each "
                "delivered task with `mcp__task__complete` (link the commit) — "
                "the contract reviewer's ACK is gated on your rows being "
                "`complete`, so finished-but-unrecorded work blocks the slice. "
                "A task waiting on a peer's work: note it in your proposal and "
                "deliver after the dependency lands; the gate holds the slice "
                "open until then."
            )
            propose_line += (
                "\n\n"
                "   **No work for you in this slice? Submit a no-op propose (#3027).** "
                "If after ORIENT you find your role has no assigned task here AND "
                "nothing to contribute (e.g. a documenter on a code-only slice, a "
                "tester on a doc-only slice, your domain is not impacted by the "
                "diff), do NOT skip silently and do NOT invent busywork — run "
                "`egg-orch consensus propose --no-changes-needed --no-changes-reason "
                '"<why you have no work here>"` (no artifacts or commit-sha needed). '
                "This counts as proposing, so consensus is not blocked waiting on "
                "you; reviewers accept it as a non-blocking no-op (they will not "
                "NACK it). Then CONFIRM (step 5) as normal once peers have proposed. "
                "Reach for a real propose instead the moment you do find work "
                "(e.g. the coder's diff turns out to need docs). Rejected while "
                "you still own incomplete contract tasks here (#3114)."
            )
        producer_lifecycle.extend(
            [
                "1. **ORIENT**: Before starting work, "
                + _build_producer_orientation(
                    role_value,
                    phase,
                    reviewers,
                    branch=branch,
                ),
                "2. **WORK**: Complete your assigned task (see Your Task below).",
                propose_line,
                "4. **RESPOND TO REVIEWS**: When a reviewer NACKs your "
                "proposal you will be re-invoked to address it. Read every "
                "NACK in the event payload, fix all named blockers, and "
                "re-propose with `--changed-artifacts`. **Aggregation is "
                "enforced by the orchestrator (#2142):** when two or more "
                "distinct reviewers have NACKed the current version, the "
                "re-propose call returns HTTP 409 with the full set "
                "(reviewer, reason, artifact_refs) inline in `details`; "
                "address every NACK then retry. A single-reviewer NACK "
                "does not trigger the barrier — re-propose proceeds "
                "normally.\n\n"
                "   **A NACK naming new findings on your re-propose is "
                "legitimate adversarial review, not goalpost-moving.** "
                "Reviewers re-review v2+ as a fresh delta; \"that's not "
                "what you NACK'd last time\" is not a valid objection. "
                "**You can and should push back on a NACK on its merits** — "
                "if the reviewer misread the code or the concern does not "
                "apply, contest it via a directed message with evidence "
                "(file:line, test, doc reference). What is *not* productive "
                "is contesting a NACK you know is correct — re-reviews are "
                "cheap by design, so when the finding is real, fix it and "
                "re-propose.",
                "5. **CONFIRM**: When all reviewers ACK, run "
                "`egg-orch consensus confirmed` to mark your role's "
                "consensus.",
                "6. **HANDLE RE-REVIEW**: When you are re-invoked with a "
                "`CONSENSUS_RE_REVIEW` event"
                + (
                    " (or a `CONSENSUS_PROPOSE` for a re-propose — "
                    "version > 1, after you NACKed a prior version; "
                    "dual-role agents handle both — see Reviewer "
                    "Lifecycle step 7 for the adversarial re-review "
                    "framing)"
                    if is_dual_role and casts_real_verdicts
                    else ""
                )
                + ", act on it — failure to respond stalls the pipeline. "
                + (
                    "If you are a reviewer of the re-proposing producer, "
                    "re-review and ACK/NACK the new proposal (dual-role "
                    "agents: see Reviewer Lifecycle step 7 below for the "
                    "adversarial re-review framing that applies to this "
                    "case). Otherwise, re-confirm via "
                    "`egg-orch consensus confirmed`."
                    if casts_real_verdicts
                    else "Re-confirm via `egg-orch consensus confirmed`."
                ),
                "7. **RESOLVE OBLIGATIONS YOU SATISFY (#2338)**: If you "
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
        lines.extend(producer_lifecycle)

    # Gate the Reviewer Lifecycle on ``casts_real_verdicts``, not raw
    # ``is_reviewer`` (#3381). A role whose only reviewed producers are
    # reached via ``wake_only`` edges (the de-roled simplifier) issues no
    # ACK/NACK and must NOT receive the reviewer playbook — REVIEW, ACK/NACK,
    # CONFIRM, adversarial re-review — which would directly contradict its
    # producer-only execution banner. This mirrors the producer-only invariant
    # already asserted for the coder (``test_producer_only_no_sync_step``): a
    # producer-only role gets no ``### Reviewer Lifecycle`` at all. A pure
    # reviewer (``reviewer_refine``) and the dual-role tester both cast real
    # verdicts, so they keep it.
    if is_reviewer and casts_real_verdicts:
        lines.extend(
            [
                "### Reviewer Lifecycle",
                "1. **PREPARE** (while waiting): "
                + _build_reviewer_preparation(
                    role_value,
                    phase,
                    branch=branch,
                    base_branch=base_branch,
                ),
                "2. **INVOKED PER EVENT**: The orchestrator's event-pump "
                "wrapper invokes you one-shot per actionable event. When a "
                "`CONSENSUS_PROPOSE` arrives for an assigned producer, "
                "you are spawned with the proposal in your event payload. "
                "Do your preparation work from step 1 on the first "
                "invocation; subsequent invocations land you directly at "
                "step 3 (SYNC) with the proposal already in context."
                + (
                    "\n\n   **Dual-role agents (you)** — per the "
                    "*Dual-Role Execution Order* banner above (updated "
                    "for coder-owns-tests): your first invocation does "
                    "ORIENT/PREPARE only. On the coder's "
                    "`CONSENSUS_PROPOSE` the wrapper re-invokes you with "
                    "the proposal in your event payload; SYNC, do your "
                    "Producer WORK (review + harden the coder's tests), "
                    "then PROPOSE your hardening and ACK/NACK the coder "
                    "in the same invocation (fall through to step 3 "
                    "(SYNC) → step 4 (REVIEW) → step 5 (ACK/NACK) here). "
                    "Subsequent invocations (re-proposes — "
                    "`CONSENSUS_PROPOSE` version > 1 — and peer-producer "
                    "proposals) are fresh reviews against the new delta, "
                    "not continuations."
                    if is_dual_role
                    else ""
                ),
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
                "   **Conditional ACK (#1998)** — use when the work is "
                "correct but a human action is needed at merge time "
                "(`git mv`, secret rotation, cross-repo flip): add "
                '`--pre-merge-condition "…"` to the ACK. The obligation '
                "renders as a `Pre-merge Obligations` block in the PR "
                "body. Do NOT use this to smuggle blocking issues past "
                "the producer — if the producer could fix it, NACK "
                "instead.\n"
                "\n"
                "   **Drop satisfied obligations on re-ACK (#2338).** When "
                "you re-ACK at a new proposal version and the conditioning "
                "work has landed in-cycle (the rename is in the diff, the "
                "obligation is moot), drop the obligation: re-ACK without "
                "`--pre-merge-condition`. Do NOT re-attach it with a "
                'self-contradicting "satisfied" hedge — the PR body '
                "renders obligations verbatim under a `do not merge` "
                "banner. To preserve the audit trail instead of dropping, "
                "re-ACK with `--pre-merge-condition-resolved-in-diff <sha>` "
                "alongside `--pre-merge-condition` so the renderer "
                "demotes (not drops) the entry (#2336).\n"
                "\n"
                "   `--reason` must be ≥50 chars of substantive content. "
                "Boilerplate like 'lgtm' or 'no issues' will be rejected.\n"
                "\n"
                "   **Stale-version rejection (#2142):** if the producer "
                "re-proposed while your verdict was in flight, the ACK / "
                "NACK is rejected with HTTP 409 inlining the current "
                "proposal snapshot (version, artifacts, commit_sha). "
                "`git fetch && git merge`, re-review against the new "
                "commit, and re-submit — don't retry the same payload."
                + (
                    "\n\n"
                    "   **Contract-enforcer gate (#3114) — applies to you.** "
                    "Your ACK of a producer is structurally gated on the "
                    "contract: the orchestrator REJECTS it (409 "
                    "`contract_incomplete`) while any task row owned by that "
                    "producer in this slice is not `status=complete`. Read "
                    "the live task records with `mcp__sdlc__show_contract` — "
                    "the `.egg-state/contracts/` copy in your checkout is an "
                    "init-time snapshot; do not trust it. When rows are "
                    "incomplete, NACK the producer citing the exact task "
                    "ids: either the work is missing (it must deliver) or it "
                    "landed unrecorded (it must run `mcp__task__complete`). "
                    "When all rows are complete, your ACK MUST carry "
                    '`attestation={"tasks_verified": ["task-…", …]}` on '
                    "`mcp__brc__ack`, covering every task id the producer "
                    "owns in this slice — absent or non-covering lists are "
                    "rejected (`attestation_required` / "
                    "`attestation_mismatch`). Your CONFIRM is likewise "
                    "rejected while ANY row in the slice is incomplete. A "
                    "producer's declared deferral (\"will land in later "
                    'proposals") is an open obligation, not an end-state — '
                    "hold consensus open until the rows are delivered or a "
                    "human descopes them."
                    if phase == "implement" and role_value in _contract_enforcer_role_names()
                    else ""
                ),
                "6. **CONFIRM**: When all assigned producers reviewed: "
                "`egg-orch consensus confirmed`",
                "7. **HANDLE RE-REVIEW**: When you are re-invoked with a "
                "`CONSENSUS_RE_REVIEW` event (or a `CONSENSUS_PROPOSE` for "
                "a re-propose — version > 1, after you NACKed a prior "
                "version), act on it — failure to respond stalls the "
                "pipeline. Re-review the re-proposing producer's new "
                "proposal and ACK/NACK it, then re-confirm via "
                "`egg-orch consensus confirmed`.\n\n"
                "   **This is adversarial re-review, not blocker-verification.** "
                "Your re-review has TWO equal-weight mandates: (1) verify the "
                "blockers from your prior NACK were addressed AND (2) audit the "
                "delta since your last review — the commits landed since the "
                "version you last verdicted (per REVIEWER-SYNC.md: `git log "
                "{last_reviewed_commit}..HEAD --not origin/{base_branch} -p`) — "
                "as a fresh reviewer with no NACK history, bounded to that "
                "delta, NOT the whole accumulated surface. Both must pass to "
                "ACK. The orchestrator's adversarial re-prime in the event "
                "body carries the full framing; this is a pointer. New issues "
                "outside your prior NACK's scope are blocking; **NACK without "
                "hesitance** — re-reviews are cheap by design, and the "
                "downstream GitHub reviewer should find nothing in your "
                "re-reviewed deltas.\n",
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

    # Same gate as the Reviewer Lifecycle above (#3381): a wake-only,
    # verdict-free role (de-roled simplifier) gets no reviewer-coordination
    # guidance, since it never ACK/NACKs.
    if is_reviewer and casts_real_verdicts:
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
            "**Event-handler contract (#2908):** The orchestrator's "
            "event-pump wrapper drives your lifecycle. You are invoked "
            "one-shot per actionable BRC event: handle the event per the "
            "lifecycle above, update durable BRC memory (writes happen "
            "automatically inside `egg-orch consensus ack` / `nack` "
            "handlers), then exit naturally. The wrapper polls "
            "`egg-orch brc next-action` and re-invokes you with the next "
            "event. You do NOT block on `egg-orch message wait-loop` "
            "yourself; the wrapper owns the wait and the heartbeat.\n",
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
        "Documents the current state of the code",
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
    "first_principles_reviewer": (
        "Adversarially reviews the seed and the refiner's direction from "
        "first principles; surfaces significant redirects to the operator as "
        "HITL decisions and never NACKs the refiner",
        "an ACK on the refiner plus any HITL redirect decisions",
    ),
    "reviewer_agent_design": (
        "Reviews agent design and architecture decisions",
        "ACK/NACK on design choices",
    ),
    "reviewer_plan": (
        "Reviews plan phase outputs",
        "ACK/NACK on architecture, tasks, and risk assessment",
    ),
    "simplifier": (
        "Distills the producer's draft into a jargon-free, human-focused "
        "companion summary (depends on the producer's pushed draft)",
        "a simplified `*-human.md` companion to the analysis/plan",
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
    """
    base_ref = _resolve_origin_ref(base_branch)

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
                "If a producer has no work in this slice it submits a generic "
                "no-op propose (`no_changes_needed=true`, #3027): the orchestrator "
                "treats that as a non-blocking no-op and will not surface it to "
                "you for review — there is nothing to ACK or NACK, and it does "
                "not block consensus."
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
                "block), NACK the architect and cite the structured errors "
                "verbatim. Instruct the architect to re-emit the slice "
                "scaffold with ``serialized_chain_order`` populated on the "
                "downstream slice. The SAME NACK applies to a "
                "``slice_overlap_violation`` rejection (#3046 — a 'Plan "
                "ingestion REJECTED: slices touch overlapping files' block): "
                "two or more slices touch the same file with no dependency "
                "ordering, so their branches fork independently off the shared "
                "base and collide at integration. Instruct the architect to "
                "serialise the overlapping cluster into one linear "
                "``dependencies`` chain (or merge the slices) so each later "
                "slice's branch is cut from the earlier one. "
                "(2) **Slice-sizing NACK (hard, judgment-based — #2809)**: "
                "slice composition is owned by the **architect**, not the "
                "task_planner. You ARE empowered and required to hard-NACK "
                "the architect on ``slice_size`` when a slice is oversized "
                "for one BRC cycle. Use judgment — no fixed tasks-per-slice "
                "or LOC budget. NACK when a slice bundles more than ~3 "
                "distinct file-categories, combines deletion-heavy with "
                "new-API-introduction work, would require >3–4 "
                "commit-propose-revise cycles, or contains independent "
                "task groups with no internal dependency. Name the seam in "
                "your NACK so the architect's re-propose is actionable. "
                "See criteria §11 for the full rubric and examples."
                "\n\n"
                "**Human-focused plan companion (the simplifier's "
                "``*-plan-human.md``):** the simplifier produces a simplified, "
                "plain-language companion to the plan for a **broad audience — "
                "engineers, PMs, and managers**. You review it (CRITICAL). "
                "**Read it side-by-side with the full plan** and ACK only when "
                "it (a) faithfully captures the plan's essence, (b) is "
                "materially lighter and more digestible than the full plan — "
                "not a near-copy, (c) is readable by a non-engineer, and (d) "
                "is free of egg-internal jargon (no "
                "BRC/consensus/slice-DAG/contract/role terms). NACK the "
                "**simplifier** (not the task_planner) if it misrepresents the "
                "plan, leaks pipeline jargon, omits a material point, merely "
                "duplicates the full plan, or — critically — reads as a "
                "**review/critique** of the plan rather than a summary of it "
                '(ACK/NACK language, "should commit to", "anti-pattern to '
                'reject", constraint lists) or buries the reader in '
                "implementation detail (`file:line` refs, function/struct/field "
                "names). A missing or empty companion is a NACK — the companion "
                "is mandatory."
            )
    elif phase == "refine":
        if role_value in ("reviewer_refine", "reviewer_agent_design"):
            base = (
                "While waiting for the refiner's proposal, prepare by: "
                "(a) reading the prior review feedback that triggered this "
                "refinement cycle, "
                "(b) checking the current state of the code to understand "
                "what was already implemented, "
                "(c) verifying which review concerns are still outstanding. "
                "When the proposal arrives, focus on whether the specific "
                "feedback items were addressed."
            )
            if role_value == "reviewer_refine":
                base += (
                    "\n\n"
                    "**Human-focused analysis companion (the simplifier's "
                    "``*-analysis-human.md``):** the simplifier produces a "
                    "simplified, plain-language companion to the analysis for a "
                    "**broad audience — engineers, PMs, and managers**. You "
                    "review it (CRITICAL). **Read it side-by-side with the full "
                    "analysis** and ACK only when it (a) faithfully captures the "
                    "analysis's essence, (b) is materially lighter and more "
                    "digestible than the full draft — not a near-copy, (c) is "
                    "readable by a non-engineer, and (d) is free of "
                    "egg-internal jargon. NACK the **simplifier** (not the "
                    "refiner) if it misrepresents the analysis, leaks pipeline "
                    "jargon, omits a material point, merely duplicates the full "
                    "draft, or — critically — reads as a **review/critique** of "
                    "the analysis rather than a summary of it (ACK/NACK "
                    'language, "should commit to", "anti-pattern to reject", '
                    "constraint lists) or buries the reader in implementation "
                    "detail (`file:line` refs, function/struct/field names). A "
                    "missing or empty companion is a NACK — it is mandatory."
                )
            return base
        if role_value == "first_principles_reviewer":
            return (
                "While waiting for the refiner's proposal, prepare your "
                "first-principles pass: (a) read the seed — `egg-contract "
                "show` and the linked issue — and restate, in your own words, "
                "the problem it claims to solve and why; (b) explore the "
                "codebase to test that premise against reality (does the thing "
                "already exist? is the problem already handled? is there a far "
                "simpler path?); (c) form your own view of whether this is the "
                "right direction and what a materially better one would be. "
                "When the refiner proposes, you are checking the *premise and "
                "direction*, not the analysis quality — surface any concrete "
                "redirect as a phase-scoped HITL decision for the operator and "
                "ACK the refiner. Never NACK on first-principles grounds."
            )

    # Generic fallback
    return (
        "While waiting for proposals, read the contract "
        "(`egg-contract show`), explore the codebase for context, "
        "and prepare your review criteria. "
        "Do NOT inspect producer artifacts before proposals arrive."
    )


def _re_review_priming_block(
    *,
    version: int | None = None,
    delta_range: str | None = None,
) -> str:
    """Adversarial re-prime injected at the moment of every re-review.

    Counter-anchors the persistent reviewer against the "verify named
    blockers got fixed" framing that long-lived context naturally
    biases toward (see #2724 post-mortem: slice-1 v2 was ACK'd despite
    the v2 delta introducing a non-executable inline `python3 -c`
    snippet that a downstream GitHub-bot reviewer caught immediately).

    Three design choices worth flagging:

    - **Delta-scoped, not exploration-forcing.** The block tells the
      reviewer to re-read *the delta since their own last review*
      adversarially, not to re-traverse the codebase. The amortized
      exploration from cycle-1 is the feature; re-Reading every
      referenced file on every cycle would throw away BRC's cost
      advantage.
    - **Per-reviewer delta, not a fixed version pair (#2887).** The
      block was originally hardcoded to the v1→v2 transition and took
      no arguments, yet was appended verbatim to every re-review (v3,
      v4, …). On N>2 cycles the stale "audit the v2 delta as a fresh
      reviewer, ignore your v1 NACK history" prose read as "re-audit
      the whole accumulated surface," widening scope each cycle and
      blocking multi-round convergence. The block is now parameterized
      by the current proposal version (``vN`` / its prior ``v(N-1)``)
      and, on per-reviewer ``CONSENSUS_RE_REVIEW`` notices, anchored to
      that reviewer's own ``<last_reviewed_sha>..HEAD`` ``delta_range``
      (resolved orchestrator-side from the reviewer's last-verdicted
      version). When ``delta_range`` is absent (the broadcast
      ``CONSENSUS_PROPOSE`` body, ``to_role=all`` — one text for
      reviewers sitting at different last-reviewed versions) the block
      references the reviewer-self-tracked range from REVIEWER-SYNC.md
      (``git log {last_reviewed_commit}..HEAD --not origin/{base} -p``)
      instead.
    - **Economic framing is explicit.** "Re-reviews are cheap / NACK
      without hesitance" is load-bearing — without it, persistent
      reviewers naturally optimize for convergence (ACK to end the
      cycle) over rigor. The orchestrator absorbs the cost of extra
      cycles; the reviewer should not be carrying it.

    The block is appended to ``CONSENSUS_RE_REVIEW`` message bodies
    (signals.py, both withdrawal/re-propose and push-after-propose
    paths) and to ``CONSENSUS_PROPOSE`` bodies when the producer is
    re-proposing (version > 1, ``changed_artifacts`` set). Reviewers
    who NACK'd the prior version receive ``CONSENSUS_PROPOSE`` rather
    than ``CONSENSUS_RE_REVIEW`` on a re-propose, so both surfaces need
    the re-prime to reach every reviewer.

    Args:
        version: The current (re-proposed) proposal version ``N``. When
            ``None`` (legacy / defensive callers) the block falls back
            to generic "current" / "prior" wording without numbered
            anchors.
        delta_range: A concrete ``<sha>..HEAD`` git range scoping this
            reviewer's mandate-2 audit to the commits landed since their
            own last verdict. Only available on the per-reviewer
            ``CONSENSUS_RE_REVIEW`` path; omitted on the broadcast
            ``CONSENSUS_PROPOSE`` body.
    """
    # Adjective placed before "review"/"verdict" ("Your v6 review" /
    # "Your current review"); and the prior-version qualifier placed
    # before "blockers"/"NACK history" ("named v5 blockers" / "named
    # prior blockers"). Both read naturally with or without a version.
    vN = f"v{version}" if version is not None else "current"
    vNm1 = f"v{version - 1}" if version is not None and version >= 2 else "prior"
    # Mandate-2's delta anchor. On the per-reviewer path we have an
    # authoritative range; on the broadcast path we point at the
    # reviewer-self-tracked range REVIEWER-SYNC.md already defines, so
    # each reviewer scopes to the commits since *their* last review
    # rather than the whole accumulated surface.
    if delta_range:
        delta_clause = (
            f"the delta since your last review (`git log {delta_range} "
            "--not origin/<base> -p` — the commits landed since the "
            "version you last verdicted)"
        )
        delta_short = f"this delta (`{delta_range}`)"
    else:
        # NOTE: `{last_reviewed_commit}` and `{base_branch}` here are
        # *literal* braces, deliberately matching the placeholder names
        # the reviewer agent already learned from REVIEWER-SYNC.md
        # (shared/prompts/REVIEWER-SYNC.md:110) — the agent substitutes
        # them at read-time from its own bookkeeping. Do NOT convert this
        # string to an f-string: there are no Python locals named
        # `last_reviewed_commit` / `base_branch` here, so f-stringifying
        # would raise `NameError` at call time. The per-reviewer branch
        # above uses `<base>` instead because that path embeds a
        # concrete, orchestrator-resolved range — only `<base>` remains
        # for the reviewer to fill in, so the angle-bracket convention
        # makes the (already-resolved vs. still-to-resolve) distinction
        # visible at a glance.
        delta_clause = (
            "the delta since your last review (per REVIEWER-SYNC.md: "
            "`git log {last_reviewed_commit}..HEAD --not "
            "origin/{base_branch} -p` — the commits landed since the "
            "version you last verdicted, NOT the whole accumulated "
            "proposal surface)"
        )
        delta_short = "this delta (the commits since your last review)"
    return (
        "\n\n**Adversarial re-review**\n\n"
        f"**Your {vN} review has TWO equal-weight mandates:**\n\n"
        f"1. **Verify named {vNm1} blockers were addressed** — confirm "
        "the producer fixed what you NACK'd.\n"
        f"2. **Audit {delta_clause} as a fresh reviewer** — ignore your "
        f"{vNm1} NACK history. Read that diff as if you'd never seen the "
        "prior version. Apply your lens (security threat-model, "
        "concurrency races, contract AC, line-by-line bugs, "
        "silent-fallback shapes — whichever your role owns) to the "
        "delta itself, not to whether your previous concerns were "
        "satisfied. **Mandate 2 is bounded to this delta** — it does "
        "NOT ask you to re-traverse the whole accumulated surface from "
        "earlier cycles; that work was amortized when you first "
        "reviewed those commits.\n\n"
        "Both mandates have equal weight. If (1) passes but (2) finds new "
        "issues, you NACK. ACK requires both pass.\n\n"
        "**The named-blockers anchor is a known trap. Every reviewer "
        "lens has a mandate-2 in its own territory** — security has "
        "newly-introduced threat surfaces, concurrency has newly-"
        "introduced races, contract has newly-introduced AC drift, code "
        "has newly-introduced line-by-line bugs. The four issues that "
        "escaped PR #2724 to the GitHub bot were all of code-lens shape "
        "(`${ANSWER}` as bare Python, deprecated `datetime.utcnow()`, "
        "non-atomic write, bare `except: pass`) — the persistent "
        'reviewer correctly answered mandate 1 ("did prior issues get '
        'fixed? yes") and skipped mandate 2 ("does this delta introduce '
        'new issues? actually yes"). The shape generalizes: whatever '
        "your lens, this delta can introduce issues your prior NACK "
        "didn't name. Watching the producer deliver a targeted fix "
        'pulls strongly toward "verify my fix-request landed → ACK." '
        "Recognize the pull and do mandate 2 anyway.\n\n"
        "**How to execute mandate 2:**\n\n"
        "- Read each new hunk as an operator who's about to copy-paste / "
        "run / integrate it. Would this code execute as written? Would "
        "these docs send a copy-paster down a working path?\n"
        "- Apply every rubric pass to the new hunks. New issues outside "
        "the scope of your prior NACK are blocking; your prior NACK does "
        "not bound this re-review.\n"
        "- **Fresh-reviewer simulation.** Before issuing your "
        f"{vN} verdict, ask: would a reviewer who has only seen "
        f"{delta_short} with no NACK history ACK this? If you can't "
        "argue yes from that diff alone, NACK.\n"
        "- **External-bot anchor.** Imagine `egg-reviewer[bot]` reads "
        f"only {delta_short} with no NACK context. What would it flag? "
        "Anything it'd flag, you should NACK first.\n\n"
        f"**Your {vN} verdict must enumerate both halves** so mandate 2 "
        "doesn't silently disappear from the record:\n\n"
        f"- (a) Which {vNm1} blockers you verified-fixed (mandate 1).\n"
        "- (b) What new issues you audited-and-did-not-find (mandate 2). "
        'Name the specific shapes you checked — not "reviewed thoroughly," '
        'but "checked for silent fallbacks, doc-snippet executability, '
        "API-deprecation, atomicity of file writes.\" If you can't "
        "enumerate (b), you haven't done mandate 2.\n\n"
        "**Re-reviews are cheap by design.** Your amortized context means "
        'the work is "read the delta, apply your rubric, decide" — '
        "minutes, not hours. NACK without hesitance; the orchestrator "
        "absorbs cycles. Two NACKs on the same producer where the second "
        "names new findings is the correct trajectory, not "
        "goalpost-moving. The downstream GitHub reviewer should find "
        "nothing in this delta. Anything it catches that lives in this "
        "cycle's diff is a miss attributable to this re-review."
    )


def _build_producer_orientation(
    role_value: str,
    phase: str,
    reviewers: list[str],
    branch: str | None = None,
) -> str:
    """Build orientation instructions for producer agents.

    Tells producers what to research before starting work — understanding
    context, knowing what reviewers will check, and checking existing code
    patterns. This produces higher-quality first proposals and fewer NACKs.

    A producer that orients and finds it has no work in this slice takes the
    generic no-op propose path described in the Producer Lifecycle (#3027) —
    no special orientation text is needed.

    Args:
        role_value: Producer role (e.g. ``coder``).
        phase: Pipeline phase name.
        reviewers: Names of reviewers that will review this producer.
        branch: The pipeline's working branch, used for sync instructions.
    """
    reviewer_awareness = ""
    if reviewers:
        reviewer_names = ", ".join(reviewers)
        reviewer_awareness = (
            f" Your work will be reviewed by **{reviewer_names}** — "
            "keep their review criteria in mind as you work."
        )

    # The simplifier runs in both the refine and plan phases as a PRODUCER
    # ONLY (the human-focused companion). It carries an advisory review edge
    # over the upstream producer purely as the event-pump wake-wire — that is
    # what re-invokes it on the upstream's PROPOSE — but it issues no verdict
    # and never reviews the draft (#3381). Its work depends on the upstream
    # producer's draft existing, so — like the implement-phase tester — it
    # orients up-front and starts producing only once the upstream proposes.
    if role_value == "simplifier":
        if phase == "plan":
            upstream, draft_desc = "task_planner", "the implementation plan"
        else:  # refine
            upstream, draft_desc = "refiner", "the refine analysis"
        sync_note = ""
        if branch:
            sync_note = (
                f" When re-invoked on the PROPOSE, sync your worktree first: "
                f"`git fetch origin && git merge origin/{branch} --no-edit`."
            )
        return (
            f"your WORK depends on **{upstream}**'s draft of {draft_desc} "
            "existing — do NOT write your companion before it is pushed. ORIENT "
            "now (read the contract and the issue/task description so you "
            "understand the subject), then exit; the event pump re-invokes you "
            f"when **{upstream}** issues `CONSENSUS_PROPOSE`, carrying that "
            "proposal in your event payload. On that invocation: read the "
            "upstream draft, then write a simplified, higher-level companion "
            "that captures its essence in plain, jargon-free language for a "
            "broad audience (engineers, PMs, and managers) — a summary, NOT a "
            "review of the draft — and PROPOSE it. That is the whole job: you "
            f"do NOT review **{upstream}**'s draft and you issue no ACK or NACK "
            "on it." + sync_note + reviewer_awareness
        )

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
                "proposed. For that case, submit a generic no-op propose "
                "(#3027) — `egg-orch consensus propose --no-changes-needed "
                "--no-changes-reason '<why: e.g. pure refactor, existing tests "
                "cover>'`. It is accepted as a non-blocking no-op (reviewers do "
                "not review or NACK it). Do NOT just heartbeat indefinitely "
                "waiting for test work that isn't there — that deadlocks the "
                "slice." + sync_note + reviewer_awareness
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
                "Identify which docs describe the surfaces this work touches, so "
                "you can fold the resulting state into them as a snapshot of "
                "current behavior once the implementation is complete. "
                "**You MUST propose** even when the slice warrants no doc "
                "updates (pure refactor / test-only / internal-only with no "
                "documented-surface impact): the BRC consensus blocks until "
                "every producer has proposed. For that case, submit a generic "
                "no-op propose (#3027) — `egg-orch consensus propose "
                "--no-changes-needed --no-changes-reason '<why: e.g. no "
                "documented surface impacted by the coder's diff>'`. It is "
                "accepted as a non-blocking no-op (reviewers do not review or "
                "NACK it). Do NOT just heartbeat indefinitely waiting for doc "
                "work that isn't there — that deadlocks the slice." + sync_note + reviewer_awareness
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
                "the current state of the code before making modifications. "
                "When the draft you are refining is an analysis or plan, "
                "surface every runtime-primitive assumption explicitly at the "
                "phase_gate (see #2594) — name each class, function, route, "
                "env var, ConfigMap key, fixture, CLI flag, or decorator the "
                "downstream plan will depend on, with `file:line` evidence "
                "and execution-context scope (in-sandbox-agent vs "
                "trusted-CI-runner vs human-operator). This makes the "
                "plan-phase Primitive-Existence and Trust-Boundary audits "
                "cheap." + reviewer_awareness
            )

    # Generic fallback
    return (
        "read the contract (`egg-contract show`) and explore the codebase "
        "to understand context, patterns, and conventions before starting." + reviewer_awareness
    )


def _build_file_boundary_section(role_value: str, repo: str | None = None) -> str:
    """Build a file boundary section for an agent prompt.

    Sources the role's allowed/blocked patterns from
    ``egg_restrictions.patterns.build_agent_patterns`` so the prompt
    matches what the gateway will actually enforce on push — including
    per-repo ``role_patterns:`` overrides from ``repositories.yaml``
    (#2528). The legacy ``egg_contracts.agent_roles`` patterns were
    Python-only and didn't honour the per-repo knobs, which created a
    contradictory message for non-Python repos: the gateway would
    enforce Go conventions while the prompt told the agent the boundary
    was Python.

    Returns an empty string when no patterns are defined for the role.
    """
    try:
        from egg_restrictions.patterns import get_agent_pattern_for_repo
    except ImportError:
        return ""

    pattern = get_agent_pattern_for_repo(role_value, repo=repo)
    if pattern is None:
        return ""

    if (
        not pattern.allowed_patterns
        and not pattern.blocked_patterns
        and not pattern.hard_blocked_patterns
    ):
        return ""

    lines = [
        "## File Boundaries (Gateway-Enforced)\n",
        f"Your role ({role_value.upper()}) can only push changes to files "
        "matching these patterns. The gateway will **reject your push** if it "
        "includes files outside your boundaries. Only create and modify files "
        "you are allowed to push.\n",
    ]
    if pattern.allowed_patterns:
        lines.append("**Allowed:** " + ", ".join(f"`{p}`" for p in pattern.allowed_patterns))
    if pattern.blocked_patterns:
        lines.append("**Blocked:** " + ", ".join(f"`{p}`" for p in pattern.blocked_patterns))
    # Hard blocks are a stricter tier: they are rejected even when they would
    # otherwise match your allow list or a docs/fixture exemption (#3396). The
    # agent must see them, or it will author a hard-blocked path (e.g.
    # `.egg-state/contracts/fixtures/x.json`, `.github/actions/x/testdata/`),
    # hit a gateway 403, and have no way to understand why.
    if pattern.hard_blocked_patterns:
        hard_line = "**Hard-blocked (never pushable, no exemption applies):** " + ", ".join(
            f"`{p}`" for p in pattern.hard_blocked_patterns
        )
        if pattern.hard_block_exempt_patterns:
            hard_line += " — except " + ", ".join(
                f"`{p}`" for p in pattern.hard_block_exempt_patterns
            )
        lines.append(hard_line)

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
            "`.github-staging/workflows/test-e2e.yml`). Call out the "
            "staged files explicitly in your PR body so the human reviewer "
            "knows to move them into `.github/` before merge — see issue "
            "#2508."
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
    operator_directives: list[OperatorDirective] | None = None,
    iteration_history: list[IterationSummary] | None = None,
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
            operator_directives=operator_directives,
            iteration_history=iteration_history,
        )
        # Surface file boundaries so agent knows what it can push (#1431).
        # Pass repo so the rendered patterns match per-repo overrides
        # (#2528) the gateway will enforce on push.
        boundary_section = _build_file_boundary_section(role_value, repo=repo)
        if boundary_section:
            base_prompt += "\n" + boundary_section
        # Producer escape hatch (#2529) — coder is one of the impassing
        # producer roles, so it must see the actionable
        # check_file_restriction / report_impasse guidance instead of
        # inventing workarounds. Refiner runs in the refine phase and
        # never owns implement-phase tasks, so it doesn't need this.
        if role_value == "coder":
            base_prompt += "\n" + _build_impasse_escape_hatch_section()
        # In concurrent mode, inject BRC consensus preamble so the coder/refiner
        # knows to propose, respond to reviews, confirm, and stay alive.
        if concurrent:
            base_prompt += _build_brc_preamble(
                role_value,
                phase,
                repo=repo,
                branch=branch,
                base_branch=base_branch,
            )
        return base_prompt

    if role_value.startswith("reviewer_"):
        # Reviewer prompts are fully built by _build_review_prompt with its
        # own criteria/verdict format + iteration-context wiring; we don't
        # accumulate the role-shared ``lines`` block for them. Dispatching
        # here (rather than mid-function with an early return) prevents
        # future drift where a "must always be included" line is added to
        # the accumulation and silently never reaches reviewers (#2795).
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
            operator_directives=operator_directives,
            iteration_history=iteration_history,
        )
        if concurrent:
            review_prompt += "\n" + _build_brc_preamble(
                role_value,
                phase,
                repo=repo,
                branch=branch,
                base_branch=base_branch,
            )
        return review_prompt

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

    # Phase iteration context: operator directives + prior iteration history.
    # Rendered for all roles (producers AND reviewers) so reviewers cannot
    # NACK a directive-driven change against a stale default rubric (#2795).
    iteration_context = _build_phase_iteration_context(operator_directives, iteration_history)
    if iteration_context:
        lines.append(iteration_context)

    # Review feedback from prior agentic cycles (scoped to agentic NACKs
    # since #2795 — HITL kickbacks render via the iteration context above).
    if review_feedback:
        lines.append("## Review Feedback\n")
        lines.append(review_feedback)
        lines.append("")

    # Derive the pipeline identifier for namespaced output filenames.
    _identifier = _pipeline_identifier(issue_number, pipeline_id)

    # Spec-driven agent-output paths (#3077 slice-3): resolve each path
    # via the artifact registry so the prompt prose, the propose-time
    # validator (signals._validate_producer_artifacts), and the gateway
    # artifact-read endpoint (slice-4) all share one source of truth.
    # The slice-2 mandatory consistency test
    # (TestConsistencyC in shared/egg_contracts/tests/test_artifact_spec.py)
    # pins these call sites to the registry; a future row rename
    # surfaces here as a missing prompt path instead of as #3016-style
    # drift between spec and rendered prose.
    from egg_contracts.artifact_spec import resolve_artifact_path as _resolve_artifact_path

    _architect_output_path = _resolve_artifact_path("architect-output", _identifier)
    _architect_slices_path = _resolve_artifact_path("architect-slices", _identifier)
    _risk_analyst_output_path = _resolve_artifact_path("risk-analyst-output", _identifier)
    # Human-focused companion drafts the simplifier produces (one per phase).
    _analysis_human_path = _resolve_artifact_path("analysis-draft-human", _identifier)
    _plan_human_path = _resolve_artifact_path("plan-draft-human", _identifier)

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
                "### When the slice warrants no new tests (#3027)",
                "",
                "Pure refactors (symbol moves, decompositions with no behavior "
                "change), doc-only slices, and other no-test-work slices still "
                "require you to **propose** — BRC consensus blocks until every "
                "producer has proposed at least once. **Don't just heartbeat "
                "and wait for work that isn't coming.** Instead submit a "
                "generic no-op propose:",
                "",
                "1. (Optional but encouraged) run the configured checks against "
                "the coder's diff (`make lint`, `make test`, etc.) to confirm "
                "the slice really is behavior-preserving.",
                "2. Propose a no-op: `egg-orch consensus propose "
                "--no-changes-needed --no-changes-reason '<concrete reason, "
                "e.g. slice-3 is a pure decomposition: symbol moves between "
                "submodules, no behavior change; existing suite covers the "
                "re-exported barrel>'`. No artifacts or commit-sha are needed.",
                "",
                "The no-op counts as proposing (so consensus is not blocked on "
                "you) and is accepted as a non-blocking no-op — reviewers do not "
                "review or NACK it. If the slice **does** have new test work "
                "(real behavior changes, new edge cases, modified contracts), do "
                "NOT use the no-op path — author tests and propose as usual.",
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
                "## Parallel Execution with Subagents\n",
                "If the changes span multiple independent components or modules, you can use "
                "Claude Code's **Agent tool** to parallelize test writing. Launch one subagent "
                "per component to write and run tests concurrently. Each subagent should work "
                "on non-overlapping test files. Subagents should only write files — do NOT "
                "stage or commit from subagents. After all subagents complete, run the full "
                "test suite to verify everything passes together, then stage and commit yourself.",
                "",
                *_EXPLORATION_SUBAGENT_GUIDANCE,
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
            "**Distinguish `tests_execution_blocked` from a no-op propose** "
            "(see the no-op section above): set `tests_execution_blocked=true` "
            "when you DID author / intend tests but the configured checks could "
            "not run (blocked downloads, missing tools) — that is a real "
            "proposal with lower confidence. Use the generic no-op propose "
            "(`--no-changes-needed`) only when the slice genuinely warrants no "
            "new tests at all. Don't conflate the two.",
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
                "Document the CURRENT STATE of the code after this change. "
                "Write as if the code has always worked this way — the "
                "slice/pipeline machinery that produced the change does not "
                "belong in the documentation:",
                "",
                "1. Review the changed files (available in handoff data or via git diff)",
                "2. Update relevant documentation (READMEs, docstrings, API docs) so it "
                "describes how the system works now",
                "3. Add or update inline code comments where they clarify current behavior",
                "4. Commit documentation changes with descriptive messages",
                "",
                "Write snapshots, not changelogs:",
                "- Describe what the code does now, not what changed or when it changed.",
                "- NEVER reference SDLC artifacts — slice numbers, TASK-N ids, phase or "
                "HITL iteration numbers — in any doc, docstring, or inline comment you write.",
                '- Include historical context (issue links, "previously X" rationale, '
                "migration notes) ONLY when it is tangibly valuable to a reader of the "
                'current system, and prefer rationale ("why it is this way") over '
                'chronology ("what it used to be / when it changed").',
                "- When updating an existing doc, fold the new state into the snapshot and "
                "REMOVE now-stale ledger or historical entries rather than appending "
                "another layer.",
                "",
                "When documenting third-party integrations or external APIs, use WebSearch "
                "and WebFetch (when available) to verify current API signatures, link to "
                "official documentation, and confirm usage examples are up to date.",
                "",
                "### When the slice warrants no doc updates (#3027)",
                "",
                "Pure refactors (symbol moves, decompositions with no "
                "surfaced API change), test-only slices, and internal-only "
                "slices that don't touch any documented surface still "
                "require you to **propose** — BRC consensus blocks until "
                "every producer has proposed at least once. **Don't just "
                "heartbeat and wait for work that isn't coming.** Instead "
                "submit a generic no-op propose:",
                "",
                "1. Walk the coder's diff and confirm there is no "
                "documented-surface impact: no public API signature "
                "changes, no behavior changes a user-facing doc describes, "
                "no new feature or flag mentioned in README / docs/, no "
                "docstring contracts that drift.",
                "2. Propose a no-op: `egg-orch consensus propose "
                "--no-changes-needed --no-changes-reason '<concrete reason, "
                "e.g. a pure decomposition: symbol moves between "
                "submodules, no surfaced API change; no README / docs/ / "
                "docstring surface impacted>'`. No artifacts or commit-sha "
                "are needed.",
                "",
                "The no-op counts as proposing (so consensus is not blocked "
                "on you) and is accepted as a non-blocking no-op — reviewers "
                "do not review or NACK it. If the slice **does** have doc "
                "impact (any of the bullets above), do NOT use the no-op "
                "path — author doc changes and propose as usual.",
                "",
                *_EXPLORATION_SUBAGENT_GUIDANCE,
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
                "7. **Surface runtime-primitive assumptions explicitly (see #2594).** "
                "When your analysis mentions a class, function, HTTP route, env var, "
                "ConfigMap key, test fixture, CLI flag, or decorator, cite it with "
                "`file:line` evidence (`grep -rn` is enough). Call out scope on "
                "**both** of the following orthogonal axes when either matters: "
                "(a) **purpose** — is the primitive unit-test-only (e.g. a test "
                "double like `ScriptedProvider`) vs deployed-pod / production "
                "code; (b) **execution context** — does the consumer run as "
                "`in-sandbox-agent` (agent pod, reaches gateway via `GATEWAY_URL`) "
                "vs `trusted-CI-runner` (pytest from outside the cluster, sees "
                "`orchestrator_url` / lifecycle-secret-gated routes / kubectl). A "
                "primitive can be unit-test-only but invoked from either runner, "
                "or deployed-pod-only but called from either runner — these are "
                "independent dimensions, so spell out whichever applies. Buried "
                "runtime assumptions are the dominant cause of expensive "
                "implement-phase NACKs; surfacing them here makes the plan-phase "
                "audit cheap.",
                "",
                f"Write your analysis to `{_architect_output_path}`.",
                "",
                # ----------------------------------------------------
                # #2809 — architect owns slice composition
                # ----------------------------------------------------
                "## Slice composition authority (#2809)",
                "",
                "**You are the sole authority for slice composition in the "
                "plan phase.** ``task_planner`` enumerates tasks within the "
                "slices you define; ``risk_analyst`` surfaces risks that "
                "feed your design. Neither owns slice shape — you do. "
                "Specifically, you own:",
                "",
                "- **Slice count.** Treat the operator's ``cq-1`` (or "
                "equivalent refine-phase complexity answer) as a coarse "
                "top-level hint, not a literal slice count. Subdivide "
                "further when the natural slice DAG calls for it.",
                "- **Slice boundaries.** Which work goes into which slice, "
                "anchored on design seams.",
                "- **Slice DAG shape.** Parent/child dependencies between "
                "slices. The forest constraint (every slice has at most "
                "ONE DAG parent) is HARD — multi-parent slices break the "
                "stacked-PR invariant. If a slice would naturally have >1 "
                "parents, serialise the upstream slices into a linear "
                "chain and record the chosen ordering on the downstream "
                "slice's ``serialized_chain_order`` field. See "
                "``docs/architecture/slice-dag.md``.",
                "- **File-overlap ⇒ dependency edge (HARD — #3046).** Any two "
                "slices that touch the same file MUST be ordered on one "
                "dependency chain (express the order in ``dependencies`` — a "
                "single-parent id per slice — not just in "
                "``serialized_chain_order``, which the scheduler does not read "
                "for branch topology). Slices that edit a shared file but are "
                "left as parallel roots/siblings fork independently off the "
                "shared base and collide at integration — plan ingestion "
                "hard-rejects this. A slice that deletes or retires a file "
                "must depend on every slice that modifies it. Keep slices with "
                "disjoint file sets parallel so they still run concurrently.",
                "- **Test co-location (HARD — #3411).** A slice that removes, "
                "renames, or rewrites code must carry the matching updates to "
                "the tests exercising that code — skip-guards, deletions, "
                "rewrites — in the SAME slice, never a later one. Every "
                "cumulative slice tip must be independently green: the "
                "per-slice green gate (#3398) runs the repo's checks at each "
                "slice tip and blocks the PR while any check is red, so a "
                "plan that parks test obsolescence in a later slice "
                "guarantees a blocked slice and repair-loop churn on slices "
                "whose only sin is plan topology. In repos that ship the "
                "changeset-aware selector (this repo's "
                "``scripts/select_tests``), the affected tests are "
                "statically discoverable with the same import graph ``make "
                "test`` narrowing uses: ``python3 "
                "scripts/select_tests/__main__.py --impacted-tests "
                "<file>...`` prints every test file that transitively "
                "imports the named files (exit 2 = closure unavailable — "
                "fall back to grepping the removed symbols in the test "
                "trees). Write the removing slice's ``goal`` so it "
                "explicitly includes those test updates; ``task_planner`` "
                "enumerates them as tasks in that slice.",
                "- **Sub-slicing.** When one slice would be too coarse, "
                "subdivide it. Right-size slices for a single BRC cycle: "
                "avoid bundling distinct file-category groups (e.g. "
                "orchestrator + gateway + schema + tests + docs all in "
                "one slice), avoid bundling deletion-heavy work with "
                "new-API-introduction work, and avoid bundling task "
                "groups that have no internal dependency — those are "
                "natural seams for parallel sub-slices. If a slice would "
                "require the implementing producer to "
                "commit-propose-revise more than 3–4 times to converge, "
                "subdivide it.",
                "",
                "Emit the slice scaffold as a YAML file alongside your "
                "JSON analysis. ``task_planner`` will copy this scaffold "
                "**verbatim** into the plan document's ``# yaml-tasks`` "
                "appendix and fill in ``tasks:`` under each slice — the "
                "scaffold is binding. If ``reviewer_plan`` NACKs on "
                "``slice_size`` or the structural lens calls a "
                "sub-division, you re-propose with the updated scaffold; "
                "task_planner re-consumes the new scaffold on the next "
                "BRC cycle.",
                "",
                f"Write the slice scaffold to `{_architect_slices_path}`:",
                "",
                "```yaml",
                "slices:",
                "  - id: 1",
                "    name: |-",
                "      <slice name>",
                "    goal: |-",
                "      <what this slice achieves>",
                "    # root slice — omit ``dependencies``",
                "  - id: 2",
                "    name: |-",
                "      <slice name>",
                "    goal: |-",
                "      <what this slice achieves>",
                "    dependencies: slice-1",
                "```",
                "",
                "Omit ``dependencies`` for root slices; for every non-root "
                "slice set ``dependencies`` to its single parent's "
                "``slice-<id>`` (e.g. ``slice-1``). ``dependencies`` is the "
                "canonical ordering key the plan parser reads (per "
                "`.egg/schemas/yaml-tasks.schema.json`) — the slice DAG is a "
                "forest, so each slice has at most one parent (one id, not a "
                "list). Do NOT include ``tasks:`` in the scaffold — that is "
                "task_planner's job. Keep ``name`` and ``goal`` concise "
                "enough that task_planner can copy them without rewording.",
                "",
                "### File Restrictions",
                "",
                "You MUST only write to:",
                f"- `{_architect_output_path}`",
                f"- `{_architect_slices_path}`",
                "",
                "Do NOT create or modify any other files. Specifically:",
                "- Do NOT modify analysis drafts (`.egg-state/drafts/*-analysis.md`) — "
                "these are finalized in the refine phase and are read-only",
                "- Do NOT create or modify contracts (`.egg-state/contracts/`)",
                "- Do NOT create or modify reviews (`.egg-state/reviews/`)",
                "- Do NOT create or modify plan drafts (`.egg-state/drafts/*-plan.md`)",
                "",
                *_EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )
    elif role_value == "task_planner":
        draft_path = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
        # Spec-driven (#3077 slice-3) — reuses the helper-resolved path above
        # so the task_planner prose and the architect prompt cannot drift.
        architect_slices_path = _architect_slices_path
        lines.extend(
            [
                "Decompose the architecture analysis into a slice-DAG implementation "
                "plan. The implement-phase pipeline ships each slice as its own "
                "stacked PR.",
                "",
                "**Slice composition is NOT your call (#2809).** ``architect`` owns "
                "slice count, slice boundaries, slice DAG shape, and sub-slicing — "
                f"and emits the binding scaffold at `{architect_slices_path}`. Your job "
                "is to enumerate ``tasks:`` within those slices, **not to re-shape "
                "them**. Copy the architect's scaffold verbatim into the "
                "``# yaml-tasks`` appendix (preserving slice ``id``, ``name``, "
                "``goal``, and ``dependencies``) and add ``tasks:`` under each "
                "slice with task IDs of the form ``TASK-<slice_id>-<n>``.",
                "",
                "If a slice has too many tasks for one BRC cycle, or you discover a "
                "natural sub-seam the architect missed, that is a **slicing problem "
                "the architect must fix** — surface it as NACK pressure (your peer "
                "reviewer ``risk_analyst`` and the structural reviewer "
                "``reviewer_plan`` will NACK ``architect`` on ``slice_size`` when "
                "evidence supports it; you can also flag the concern in your plan "
                "prose so the reviewers pick it up). **Do NOT silently re-shape "
                "slices.** Re-propose against the architect's revised scaffold "
                "once it lands.",
                "",
                "**Test co-location (HARD — #3411).** When a slice removes, "
                "renames, or rewrites code, enumerate the matching test "
                "updates (skip-guard, deletion, rewrite) as tasks IN THAT "
                "SLICE — never in a later slice — and list the test files in "
                "those tasks' ``files:``. Every cumulative slice tip must be "
                "independently green: the per-slice green gate (#3398) "
                "blocks a slice PR while any repo check is red at its tip, "
                "so a test that still imports a symbol removed two slices "
                "earlier blocks the whole stack. Discover the affected "
                "tests with the same import graph ``make test`` narrowing "
                "uses, where the repo ships it (this repo: ``python3 "
                "scripts/select_tests/__main__.py --impacted-tests "
                "<file>...``; exit 2 = closure unavailable — fall back to "
                "grepping the removed symbols in the test trees).",
                "",
                "Steps:",
                f"1. Read the architecture analysis AND the slice scaffold at `{architect_slices_path}`",
                "2. Copy the architect's slice scaffold verbatim into the "
                "``# yaml-tasks`` appendix (same ``id`` / ``name`` / ``goal`` / "
                "``dependencies`` values, in the same order)",
                "3. Enumerate ``tasks:`` under each slice — discrete, "
                "actionable, with clear acceptance criteria and dependency ordering "
                "between tasks",
                "4. Identify the test strategy — what automated tests cover the "
                "changes, and what manual verification is needed",
                "5. Identify any manual pre-merge or post-merge steps "
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
                "slices:",
                "  - id: 1",
                "    name: |-",
                "      Slice Name",
                "    goal: |-",
                "      What this slice achieves, written for a reviewer of the",
                "      target repo. This text is rendered verbatim as the lead",
                "      paragraph of the slice's PR body (#3115), so keep it 1-3",
                "      plain-language sentences with no plan-internal",
                "      cross-references (reviewer codes, section numbers, draft",
                "      version markers).",
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
                "Do NOT use a `pr_plan` key — slice packaging is owned by the "
                "slice-DAG section below, not by an ad-hoc PR list.",
                "",
                "The `test_plan` field is **required** — describe both automated test "
                "coverage and any manual verification steps. The `manual_steps` field "
                "should list any pre-merge or post-merge actions required by the reviewer "
                "or deployer; use an empty string if none.",
                "",
                # ----------------------------------------------------
                # #2594 — primitives audit (cheap plan-phase NACK)
                # ----------------------------------------------------
                "## Primitives audit (#2594)",
                "",
                "Plan-phase NACKs are cheap; implement-phase NACKs on missing "
                "primitives are expensive (8+ pod spawns per slice, ~60–90 min "
                "per cycle). Make the audit cheap by **pre-citing every "
                "primitive your tasks depend on**. For each named class, "
                "function, HTTP route, env var, ConfigMap key, test fixture, "
                "CLI flag, or decorator your plan references:",
                "",
                "1. **Cite existence** with `file:line` (use `grep -rn` to "
                "verify *before* writing the task). If the primitive does not "
                "exist yet because the task itself will create it, mark it "
                "`(NEW — task TASK-X-Y)` so the plan reviewer doesn't NACK on "
                "missing-primitive evidence. When you mark a primitive "
                "`(NEW — task TASK-X-Y)`, you MUST also: (a) ensure the "
                "referenced task's acceptance criteria actually produce that "
                "primitive in the form the plan uses (right kind, right "
                'module, right scope — not just "adds the feature"), and '
                "(b) order downstream tasks that consume the primitive "
                "**after** the creating task in the slice DAG. The plan "
                "reviewer's §9 exception verifies both; mismatches NACK.",
                "2. **Cite trust-boundary scope.** Some primitives exist but "
                "are unavailable in the execution context the task assumes. "
                "Canonical example: `ScriptedProvider` is unit-test-only; "
                "deployed agent pods run the real provider. Likewise the "
                "`integration_tests/` fixture tiering — the only "
                "`gateway_url` pytest fixture lives at "
                "`integration_tests/local_pipeline/conftest.py:261` and is "
                "kubectl-gated via `local_pipeline_stack`. The parent "
                "`integration_tests/conftest.py` does **not** expose "
                "`gateway_url` as a fixture; it exposes `gateway_url` as an "
                "attribute on the `EggStack` dataclass "
                "(`integration_tests/conftest.py:78`), accessed as "
                "`egg_stack.gateway_url`, not as a fixture-injectable "
                "parameter. `orchestrator_url` and lifecycle-secret-gated "
                "routes are also `local_pipeline/`-only. **No pytest fixture "
                "in `integration_tests/` is `in-sandbox-agent`-runnable "
                "today** — every fixture transitively depends on `egg_stack` "
                "or `local_pipeline_stack`, both of which `pytest.skip` when "
                "`_kubectl_available()` returns `False`. Tasks that need any "
                "of `gateway_url` / `orchestrator_url` as a pytest fixture "
                "MUST live under (or below) `local_pipeline/` or an "
                "equivalent trusted directory. Verify with "
                "`grep -rn 'def gateway_url' integration_tests/` — exactly "
                "one hit. The agent-runtime `GATEWAY_URL` env is a "
                "**separate surface** from pytest fixtures; production code "
                "an agent writes can reach the gateway sidecar through it, "
                "but that is not a pytest test. See "
                "`docs/architecture/integration-test-trust-boundary.md`.",
                "",
                "Recommended shape: a short `## Primitives` section in the "
                "prose with one row per primitive (name, `file:line`, "
                "execution-context scope). The plan reviewer will run the "
                "Primitive-Existence Audit (criteria §9) and Trust-Boundary "
                "Audit (criteria §10) against this table; both are hard "
                "NACKs when a named primitive has no grep hit or is used "
                "outside its scope.",
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
                "**Slice sizing is the architect's call (#2809).** Slice "
                "count, boundaries, and DAG shape come from the architect's "
                "scaffold — copy them verbatim. ``reviewer_plan`` will hard "
                "NACK ``architect`` on ``slice_size`` when a slice is "
                "oversized for one BRC cycle (judgment-based — see the "
                "reviewer's §11 rubric); do NOT silently re-shape slices "
                "to dodge a size concern. Raise it as NACK pressure on "
                "architect instead (see the surfacing guidance above).",
                "",
                "**Forest constraint (HARD, enforced at plan ingestion)**: "
                "every slice must have at most ONE DAG parent — the "
                "implement-phase pipeline ships every slice as a stacked "
                "PR with exactly one base branch. The architect's scaffold "
                "encodes this via a single-parent ``dependencies`` id "
                "(``slice-<N>``); preserve it.",
                "",
                "**Auto-serialization for would-be multi-parent slices**: "
                "the architect is responsible for serialising would-be "
                "multi-parent slices and populating "
                "``serialized_chain_order`` on the downstream slice. "
                "Preserve that field verbatim from the scaffold.",
                "",
                "**File-overlap ⇒ ordering (HARD — #3046)**: you fill in each "
                "slice's tasks and their ``files_affected``, so you see the "
                "file sets first. If you find yourself assigning the SAME file "
                "to two slices that the architect left unordered (parallel "
                "roots or siblings), do NOT silently proceed — plan ingestion "
                "hard-rejects overlapping slices with no dependency edge, "
                "because their branches fork independently off the shared base "
                "and collide at integration. Raise NACK pressure on the "
                "architect (via the plan prose) to serialise the overlapping "
                "cluster into one ``dependencies`` chain — or to merge the "
                "slices. Do not re-shape the slice DAG yourself.",
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
                *_EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )
        # Append role file restriction info so the planner assigns tasks correctly.
        # Pass the pipeline's repo so per-repo role_patterns from
        # repositories.yaml are rendered (#2528) — keeps planner-prompt
        # boundaries in sync with the gateway's push-time enforcement.
        lines.append(_build_role_restrictions_section(repo=repo or None))
    elif role_value == "risk_analyst":
        lines.extend(
            [
                "**You are dual-role (producer AND reviewer) in this phase "
                "(#2809).** You produce the risk register AND you review "
                "``architect`` and ``task_planner`` through the risk lens — "
                "your NACK blocks plan-phase consensus until the upstream "
                "producer re-proposes addressing the concern. This mirrors "
                "the implement-phase ``tester`` dual-role pattern (#2749); "
                "the *Dual-Role Execution Order* banner in your BRC "
                "preamble is the authoritative ordering — read it first.",
                "",
                "## Producer role (risk register)",
                "",
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
                "7. **Flag runtime-primitive and trust-boundary risks (see "
                "#2594).** Plans that depend on classes, fixtures, routes, "
                "or env vars which don't exist in the form the plan assumes "
                "— or which exist but only in a different execution context "
                "than the task uses (e.g. unit-test-only `ScriptedProvider` "
                "vs deployed agent pods; `orchestrator_url` fixture defined "
                "only in `integration_tests/local_pipeline/conftest.py` vs "
                "in-sandbox-agent tests) — are a recurring high-impact "
                "failure mode (see #2474). Call these out explicitly so the "
                "plan reviewer can audit them.",
                "",
                f"Write your risk assessment to `{_risk_analyst_output_path}`.",
                "",
                "## Reviewer role (risk lens on architect + task_planner)",
                "",
                "When ``architect`` or ``task_planner`` proposes (their "
                "``CONSENSUS_PROPOSE`` will wake you via the dual-role "
                "augmentation on your producer waits — see the banner), "
                "review their work through the risk lens and emit ACK or "
                "NACK. ``blocking_concerns`` are NACK-shaped: they block "
                "plan-phase consensus and force the upstream producer to "
                "re-propose addressing them.",
                "",
                "Use this verdict shape in your producer artifact "
                "(risk-register JSON) **and** mirror the verdict / "
                "feedback in your ``egg-orch consensus ack`` / "
                "``egg-orch consensus nack`` ``--reason`` body so the "
                "upstream producer can act on it:",
                "",
                "```json",
                "{",
                '  "verdict": "ACK" | "NACK",',
                '  "risks": [...],',
                '  "top_3_risks": [...],',
                '  "blocking_concerns": [...],',
                '  "feedback": "concrete revision instructions for architect / task_planner (empty on ACK)"',
                "}",
                "```",
                "",
                "NACK when a risk is severe enough that shipping the plan "
                "as-proposed would invite a known-class failure (security "
                "regression, data loss, compliance break, runtime-primitive "
                "or trust-boundary mismatch that would surface as an "
                "expensive implement-phase NACK). ACK when risks are real "
                "but mitigated, or low enough that the plan can ship and "
                "the risks belong in the register as forward-looking "
                "notes. Be specific in ``feedback`` — name the file, "
                "the slice, the missing mitigation — so the upstream "
                "producer's re-propose is actionable.",
                "",
                *_EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )
    elif role_value == "simplifier":
        if phase == "plan":
            _upstream = "task_planner"
            _upstream_draft = "the implementation plan"
            _human_path = _plan_human_path
            _essence = (
                "what will be built, the major steps/phases, the test strategy "
                "in brief, and the key risks"
            )
        else:  # refine
            _upstream = "refiner"
            _upstream_draft = "the refine analysis"
            _human_path = _analysis_human_path
            _essence = "the problem, the recommended approach, and the key trade-offs"
        lines.extend(
            [
                "**You are a producer only in this phase.** You produce a "
                f"human-focused companion to {_upstream_draft}. You do NOT "
                f"review **{_upstream}**'s draft, and you issue no ACK or NACK "
                "on it: an internal wake-wire re-invokes you when it proposes "
                "so you know its draft is ready, and consensus never waits on a "
                "verdict from you. The *Execution Order* banner in your BRC "
                "preamble is the authoritative ordering — read it first.",
                "",
                "## Producer role (human-focused companion)",
                "",
                f"Your WORK depends on **{_upstream}**'s draft existing. ORIENT "
                f"now, then start producing only once **{_upstream}** issues "
                "`CONSENSUS_PROPOSE` (the event pump re-invokes you carrying that "
                "proposal). On that invocation:",
                "",
                f"1. Read **{_upstream}**'s draft of {_upstream_draft}.",
                f"2. Write a HUMAN-FOCUSED companion to `{_human_path}`. This is a "
                "simplified, higher-level summary for a **broad audience — "
                "engineers, PMs, and managers** — not a peer review. Capture the "
                f"essence: {_essence}.",
                "",
                "   Rules:",
                "   - **Broad, mixed audience.** Write so a non-engineer "
                "(PM, manager) can follow *what is changing and why it matters*, "
                "while staying accurate enough for an engineer. Explain any "
                "unavoidable technical term in plain language.",
                "   - **No egg-internal jargon.** Do not mention BRC, consensus, "
                "propose/ACK/NACK, slices / slice-DAG, contracts, phases, "
                "`serialized_chain_order`, Jaccard, or agent-role names. Describe "
                "independently-shippable pieces in plain terms if you must "
                "reference them at all.",
                "   - **No implementation minutiae.** No `file:line` references, "
                "no function / struct / field / type names or other code "
                "identifiers, no per-field enumerations. Describe behaviour and "
                "impact, not the code.",
                "   - **This is NOT a review.** Do not critique, score, or gate "
                'the upstream draft. No ACK/NACK language, no "the draft should '
                'commit to …", no "anti-pattern to reject", no constraint '
                "lists. You have no critique to record anywhere — your only "
                "output is this plain-language summary.",
                f"   - **Exactly one file.** Commit ONLY `{_human_path}`. Do "
                "NOT create any other `.egg-state/drafts/` file — no separate "
                "`*-simplifier-*.md` constraints/guardrails/verification "
                "companion. Any review reasoning goes in the BRC channel "
                "(your verdict), never a second persisted document. A "
                "proposal that introduces a second draft is rejected at "
                "propose time.",
                "   - **Much shorter and more digestible** than the upstream "
                "draft — plain prose and short lists, not exhaustive enumeration.",
                "   - **Faithful** — reflect the upstream draft accurately; "
                "introduce no new scope, claims, or recommendations.",
                "",
                f"3. Commit and push `{_human_path}`, then PROPOSE it via "
                "`egg-orch consensus propose`. The companion is **mandatory** — "
                "always write at least a one-paragraph summary; do NOT take the "
                "no-op propose path. That completes your work for this phase.",
                "",
                *_EXPLORATION_SUBAGENT_GUIDANCE,
            ]
        )
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
    # Pass repo so the rendered patterns match per-repo overrides
    # (#2528) the gateway will enforce on push.
    boundary_section = _build_file_boundary_section(role_value, repo=repo)
    if boundary_section:
        lines.append(boundary_section)

    # Producer escape hatch (#2529) — tester/documenter are the other
    # two impassing producer roles (coder is handled in the early-return
    # branch above). They need the actionable
    # check_file_restriction / report_impasse guidance so they don't
    # invent workarounds when their assigned task is structurally
    # impossible.
    if role_value in ("tester", "documenter"):
        lines.append(_build_impasse_escape_hatch_section())

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


# Bound on how many times the worktree-divergence reconcile will pause
# for the operator before giving up and failing the pipeline (#2979).
# A small budget guards against an operator repeatedly choosing
# "Reconciled — resume" without actually reconciling the worktree, which
# would otherwise re-pause forever.
_MAX_DIVERGENCE_RECONCILE_PAUSES = 3

_DIVERGENCE_RECONCILE_RESUME = "Reconciled — resume"
_DIVERGENCE_RECONCILE_ABORT = "Abort pipeline"
_DIVERGENCE_RECONCILE_HITL_OPTIONS = [
    _DIVERGENCE_RECONCILE_RESUME,
    _DIVERGENCE_RECONCILE_ABORT,
]

# Stable string discriminator set on persisted reconcile HITLs (#2979).  The
# non-blocking ``populate_contract`` route uses this to dedupe: when an
# operator re-POSTs against an already-paused pipeline (e.g. an automated
# retry or a refresh through ``/sdlc`` before resolving the prior HITL), the
# route surfaces the existing pending decision rather than appending a fresh
# one.
_DIVERGENCE_RECONCILE_HITL_CONTEXT = "divergence_reconcile_unacked"

# Stable string discriminator set on the consensus-timeout / incomplete-
# consensus HITL the orchestrator opens when a phase times out without
# converging (the "Consensus timed out; consensus incomplete …"
# Retry/Accept/Abort decision).  The convergence-success path uses this to
# auto-withdraw the decision once the phase reaches genuine consensus, so an
# operator is never left disposing of a decision the system already obsoleted
# (#3315 facet c — happens when a superseded thread opens the decision and a
# restarted thread then converges).
_CONSENSUS_TIMEOUT_HITL_CONTEXT = "consensus_timeout_incomplete"


# Pipeline-branch divergence alert (#2224 PR 3; #2270 §2 calibration).
#
# Watches ``origin/<pipeline_branch>`` for the contamination shape from
# #2222: the branch has absorbed already-merged main commits (a bad
# rebase / merge re-introduces commits that already live in
# ``origin/<base>``).  The original detector keyed on a ``(#NNNN)``
# subject regex, which both *false-positives* (an agent legitimately
# references a PR number in a commit subject) and *false-negatives* (a
# reabsorbed commit whose subject was rewritten).  The #2270 calibration
# replaces that brittle heuristic with a git-history signal: an
# ahead-commit is contamination when its **patch-id matches a commit
# already in ``origin/<base>``** (it is a reabsorbed merged-main commit),
# or — at branch granularity — the branch is neither an ancestor of base
# nor patch-id-equivalent to it.  The scan window is capped
# (``_BRANCH_DIVERGENCE_SCAN_CAP``) so a long-lived branch / deep base
# history cannot make the tick unbounded.
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
BRANCH_DIVERGENCE_THRESHOLD = 20
# Cap on how many commits we patch-id on each side of the comparison. The
# contamination we care about is recent (a bad rebase during this pipeline),
# so bounding the window keeps the per-tick git work flat regardless of how
# far the branch / base have grown.
_BRANCH_DIVERGENCE_SCAN_CAP = 200


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
    ``GatewayClient.list_remote_branches``. ``list_open_prs`` routes
    through the launcher-authed control-plane route
    ``/api/v1/gh/list_open_prs`` (#2925); ``list_remote_branches`` routes
    through the existing per-agent ``git ls-remote`` allowlist. The rebase
    callable forwards to
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
    # #3393 slice-5: the cross-repo merge-sequencing gate rides the SAME
    # reconciler cadence (no new scheduler subsystem). Imported here (not
    # top-level) to keep this helper's import surface minimal, mirroring
    # the ``reconcile_once`` import above.
    try:
        import orchestrator.cross_repo_merge_gate as cross_repo_merge_gate
    except ImportError:
        import cross_repo_merge_gate  # type: ignore[no-redef]
    try:
        from orchestrator.env_config import get_cross_repo_merge_gate_max_attempts
    except ImportError:
        from env_config import (  # type: ignore[no-redef]
            get_cross_repo_merge_gate_max_attempts,
        )
    try:
        from orchestrator.models import resolve_slice_repo
    except ImportError:
        from models import resolve_slice_repo  # type: ignore[no-redef]

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

    # #3393 slice-5: only multi-repo pipelines can have cross-repo
    # dependency edges, so the merge gate is a strict no-op for N=1 —
    # skip it entirely rather than burning a contract scan per tick.
    _gate_enabled = len(getattr(pipeline, "repos", None) or []) > 1
    # Per-run gate bookkeeping (attempts / hold-registered / resolved),
    # keyed by dependent slice id; persists across reconciler ticks.
    _gate_state: dict[str, Any] = {}
    try:
        _gate_max_attempts = int(get_cross_repo_merge_gate_max_attempts())
    except Exception:  # noqa: BLE001
        _gate_max_attempts = cross_repo_merge_gate.DEFAULT_MAX_POLL_ATTEMPTS
    _gate_current_phase = getattr(pipeline, "current_phase", None)

    def _poll_cross_repo_merge_gate(contract: Any) -> None:
        # Drive one cross-repo merge-sequencing pass on the reconciler
        # cadence (#3393 slice-5, task-5-1 / task-5-2). Reads upstream PR
        # merge-state and auto-readies a dependent draft PR on merge
        # (Tier A); registers a HITL hold on the closed-unmerged / timeout
        # terminals and for plan-declared beyond-merge-state edges (Tier
        # B). All gateway/contract effects are funnelled through the
        # injected callables so the gate logic stays pure + unit-tested.
        if not _gate_enabled:
            return
        cross_repo_merge_gate.poll_once(
            contract,
            resolve_repo=lambda s: resolve_slice_repo(s, pipeline),
            get_merge_state=lambda repo_slug, pr_num: gateway.get_pr_merge_state(
                pipeline_id, repo_slug, pr_number=pr_num
            ),
            mark_ready=lambda repo_slug, pr_num: bool(
                gateway.mark_pr_ready(pipeline_id, repo_slug, pr_number=pr_num)
            ),
            register_hold=lambda gate, reason: _register_cross_repo_hold(
                pipeline_id=pipeline_id,
                slice_id=gate.slice_id,
                repo=gate.repo,
                pr_number=gate.pr_number,
                reason=reason,
                worktree_repo_path=worktree_repo_path,
                current_phase=_gate_current_phase,
            ),
            hold_resolution=lambda gate: _cross_repo_hold_resolution(contract, gate.slice_id),
            state=_gate_state,
            max_attempts=_gate_max_attempts,
        )

    def _list_open_prs() -> list[dict[str, Any]]:
        # Lists open PRs in ``pr_repo`` so ``find_orphaned_child_prs``
        # can detect children whose base branch was deleted (parent
        # merged through the GitHub UI). Routes through the launcher-authed
        # control-plane endpoint ``/api/v1/gh/list_open_prs`` — the
        # orchestrator is the server that manages pipelines, not an agent,
        # so it does not register a synthetic agent session or impersonate
        # a role (#2922 / #2925).
        if not pr_repo:
            return []
        try:
            return list(gateway.list_open_prs(pipeline_id, pr_repo))
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
        # the existing per-agent ``git ls-remote`` allowlist. The
        # synthetic session uses ``agent_role="orchestrator"`` so this
        # orchestrator-driven ls-remote is attributed to the orchestrator
        # in the audit log instead of a phantom coder (#2919).
        if not repo_path_str:
            return set()
        try:
            return set(
                gateway.list_remote_branches(
                    pipeline_id,
                    repo_path_str,
                    agent_role="orchestrator",
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
                    # Orchestrator-driven heal (rebase + force-push +
                    # pr-edit); attribute to the orchestrator, not a
                    # phantom coder (#2919). The force-push targets the
                    # slice integration branch on a synthetic session, so
                    # the slice-integration exemption admits it regardless
                    # of role.
                    agent_role="orchestrator",
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
                # #3393 slice-5: drive the cross-repo merge-sequencing
                # gate on the same tick + same freshly-loaded contract.
                # No-op for N=1 pipelines. Wrapped in its own try so a
                # gate failure never disrupts stacked-PR reconciliation.
                try:
                    _poll_cross_repo_merge_gate(contract)
                except Exception as gate_exc:  # noqa: BLE001
                    logger.debug(
                        "cross_repo_merge_gate tick raised — continuing",
                        pipeline_id=pipeline_id,
                        error=str(gate_exc),
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
    run_epoch: datetime | None = None,
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
        from slice_scheduler import SliceScheduler

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
        scheduler = SliceScheduler(
            contract,
            max_parallel_slices=pipeline.config.max_parallel_slices,
        )
    except ValueError as exc:
        logger.error(
            "Slice loop: scheduler refused to start (forest validation failed)",
            pipeline_id=pipeline_id,
            error=str(exc),
        )
        return 1, f"slice scheduler validation failed: {exc}"

    # Defensive idempotent context-PR opener (#2777 cq-4). The
    # canonical advance_phase REST path enforces hard-required, but
    # the runner-driven entries (auto-advance, implement-entry,
    # HITL-resume, this slice-loop entry) must also fire it to avoid
    # silent strands on ``egg/<id>/work``. Soft-fail on transient
    # gateway errors here — the canonical site already enforces the
    # 422 contract.
    try:
        # Pass the main repo path (``store.repo_path``) — not
        # ``worktree_repo_path`` — so all four opener call sites of
        # ``_open_context_pr_at_implement_start`` read identically.
        # The opener rederives its own per-pipeline worktree internally
        # via ``resolve_worktree_path(pipeline_id, store.repo_path)``.
        _open_context_pr_at_implement_start(pipeline_id, repo_path=Path(store.repo_path))
    except ContextPrCreationError as ctx_err:
        logger.warning(
            "Context PR opener: slice-loop entry safety net failed "
            "(continuing — hard-require enforced at advance_phase and "
            "the implement-start plan pre-flight gate) (#2777, #3100)",
            pipeline_id=pipeline_id,
            reason=ctx_err.reason,
            error=str(ctx_err),
        )
    except Exception as safety_err:  # noqa: BLE001
        # Defence in depth: import / lookup failures must not strand
        # the slice loop.
        logger.warning(
            "Context PR opener: slice-loop entry safety net outer "
            "wrapper raised (continuing) (#2777)",
            pipeline_id=pipeline_id,
            error=str(safety_err),
        )

    def _contract_loader() -> Any:
        try:
            return load_contract(pipeline_id, worktree_repo_path)
        except Exception:  # noqa: BLE001
            # Best-effort loader for callers that just need "current
            # contract or None". Catches loader validation errors,
            # OSError on the contract file read, and any pydantic
            # re-serialisation failure.
            return None

    # Stacked-PR reconciler starts after the bootstrap pass below so
    # an unhandled bootstrap exception cannot leak its daemon thread
    # (the ``finally`` at the bottom of the run loop owns teardown).
    aggregate_logs: list[str] = []
    overall_exit = 0
    poll_interval = 5.0

    from egg_contracts.models import SliceStatus

    try:
        from orchestrator import global_slice_admit
    except ImportError:
        import global_slice_admit  # type: ignore[no-redef]
    try:
        from orchestrator.peer_consensus import remove_peer_consensus_tracker
    except ImportError:
        from peer_consensus import remove_peer_consensus_tracker  # type: ignore[no-redef]
    try:
        from orchestrator.state_store import get_pipeline_state_lock
    except ImportError:
        from state_store import get_pipeline_state_lock  # type: ignore[no-redef]

    def _commit_and_push_slice_statefiles(message: str) -> None:
        """Commit + push pipeline-scoped ``.egg-state/`` writes to the work branch.

        Contract mutations — agent task-record updates via
        ``mutate_contract`` and the ``slice.status`` flips below — land
        on the shared pipeline worktree's disk copy only. Without a
        slice-boundary commit, the work branch's contract file stays
        frozen at the init-time "Initialize SDLC contract" commit for
        the entire implement phase, and a mid-phase orchestrator crash
        or worktree prune loses every accumulated task record (#3117).
        The phase-boundary commit at the end of the run loop is too
        coarse for multi-slice phases.

        Scope (per #3117): this closes durability for the post-prune
        audit record, operator/PR-side review of mid-phase contract
        state, and orchestrator-restart resume at slice granularity.
        It is deliberately NOT the read path for live agents — agents
        read the contract via ``mcp__sdlc__show_contract`` against the
        orchestrator's in-memory state, never from their checkout's
        ``.egg-state/contracts/`` file (#3077).

        Best-effort: slice completion must not block on statefile
        durability; failures are logged and the next boundary (later
        slice close or phase completion) carries the writes. The commit
        runs under the per-pipeline state lock to serialise concurrent
        slice-close threads against the shared worktree's git index;
        the push runs outside the lock. The expected case is a linear
        fast-forward (lock-serialised commits stack), and a no-op FF
        of the same SHA from two threads is harmless. The residual
        hazard is ``_reconcile_and_retry_push`` on a non-FF rejection
        (``gateway_client.py:1361``): two threads both fetching+rebasing
        in the shared worktree can interleave ``.git/index.lock``.
        Within the implement phase no other writer pushes to
        ``pipeline.branch`` so non-FF shouldn't fire in normal
        operation; an external push (operator hand-fix, stale
        concurrent orchestrator) is the only known trigger.
        """
        try:
            with get_pipeline_state_lock(pipeline_id):
                committed = _commit_statefiles_to_worktree(
                    worktree_repo_path,
                    message,
                    _pipeline_identifier(issue_number, pipeline_id),
                    pipeline_id=pipeline_id,
                )
        except Exception as commit_err:  # noqa: BLE001
            # The helper raises CalledProcessError / TimeoutExpired
            # from subprocess.run and OSError from glob (#2219 family).
            logger.warning(
                "Failed to commit slice statefiles to work branch (continuing) (#3117)",
                pipeline_id=pipeline_id,
                commit_message=message,
                error=str(commit_err),
            )
            return
        if not committed or not pipeline.branch or worktree_repo_path == store.repo_path:
            return
        try:
            spawner.gateway.push_worktree_branch(
                pipeline_id=pipeline_id,
                repo_path=str(worktree_repo_path),
                branch=pipeline.branch,
                mode=gateway_mode,  # type: ignore[arg-type]
                base_branch=pipeline.base_branch,
            )
        except Exception as push_err:  # noqa: BLE001
            # Gateway HTTP push (GatewayError / OSError). The commit is
            # already on the local work branch; the next successful
            # push carries it.
            logger.warning(
                "Failed to push slice statefiles to work branch (continuing) (#3117)",
                pipeline_id=pipeline_id,
                commit_message=message,
                error=str(push_err),
            )

    def _persist_slice_status_complete(
        slice_id: str,
        *,
        pr_number: int | None = None,
        pr_url: str | None = None,
        basis: str | None = None,
        commit_to_branch: bool = True,
    ) -> None:
        """Mark ``slice_id`` as ``SliceStatus.COMPLETE`` on the contract.

        Durable signal so the bootstrap reconciliation pass below and
        the ``restart_agent`` parent-complete fallback can skip the
        slice without a GitHub round-trip (#2549, #2470). Best-effort:
        on save failure the in-memory scheduler state still reflects
        completion for this pass and the next ``start_pipeline``
        re-detects via the merged-detection helper.

        With *commit_to_branch* (the default), the saved contract —
        along with any other uncommitted pipeline statefiles, e.g.
        agent task-record mutations made during the slice — is
        committed and pushed to the pipeline work branch so the durable
        copy tracks the live one (#3117). The bootstrap reconciliation
        passes set it to ``False`` and batch a single commit after the
        loop instead of one per reconciled slice.

        Called only after a slice successfully closes (BRC consensus
        reached + PR opened, or merged-skip / bootstrap-COMPLETE
        reconciliation). Failed slices — ``exit_code_inner != 0``
        (#16410) or ``pr_created == False`` (#16588) — return early
        without calling this helper, so their accumulated task-record
        mutations remain uncommitted in the worktree until the next
        successful slice's commit (the pipeline-scoped glob picks them
        up) or the phase-boundary commit, whichever fires first.

        ``basis`` lets a caller declare *why* the slice is complete when
        not every task is marked COMPLETE on the contract: ``"merged"``
        (integration branch ancestry-verified merged into its parent) or
        ``"consensus_complete"`` (BRC consensus reached pre-restart, PR
        not yet opened). The PR-open caller passes ``pr_number`` instead.
        Absent any of these — and with tasks still pending — the write is
        a #3214 false-complete and :func:`_validate_slice_completion_basis`
        raises :class:`SliceCompletionInvariantError` rather than persist
        a slice as done that never ran.

        When the caller just opened the slice's PR it passes
        ``pr_number`` / ``pr_url`` so the linkage lands in the same
        contract write (#3122) — the context-PR body refresh and any
        later stack consumer read them from ``Slice.pr_number``.
        ``None`` (the merged-skip and bootstrap callers) leaves any
        previously recorded linkage untouched.

        TODO(#3122): the three ``None`` callers — bootstrap layer-A
        (contract-recorded COMPLETE), bootstrap layer-B (merged on
        origin), and the run-loop merged-skip — do not recover the
        slice PR number from GitHub (`gh pr list --head … --state
        merged`), so on a resume past those points the slice-table
        entries for merged slices stay unlinked. Acceptable for v1
        because the per-slice ``— #N`` link is most useful while the
        stack is live, but worth backfilling if reviewers ask for
        complete cross-linkage on archived stacks.
        """
        try:
            with get_pipeline_state_lock(pipeline_id):
                contract_local = load_contract(pipeline_id, worktree_repo_path)
                for s in contract_local.slices:
                    if s.id == slice_id:
                        # #3214 — refuse to persist a contradictory COMPLETE.
                        # An interior forest node marked COMPLETE without a
                        # valid basis (tasks pending, no PR, no verified
                        # merge/consensus) skips a slice that never ran and
                        # wedges the chain a phase later. Fail loud here, at
                        # the source of the bad write, instead.
                        invalid = _validate_slice_completion_basis(
                            s, pr_number=pr_number, basis=basis
                        )
                        if invalid is not None:
                            logger.error(
                                "Refusing to persist slice.status=COMPLETE — "
                                "invalid completion basis (#3214)",
                                pipeline_id=pipeline_id,
                                slice_id=slice_id,
                                reason=invalid,
                            )
                            raise SliceCompletionInvariantError(invalid)
                        s.status = SliceStatus.COMPLETE
                        if pr_number is not None:
                            s.pr_number = pr_number
                        if pr_url is not None:
                            s.pr_url = pr_url
                        logger.info(
                            "Slice marked COMPLETE",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            basis=(
                                basis
                                or (
                                    "pr"
                                    if (pr_number is not None or s.pr_number is not None)
                                    else "tasks_complete"
                                )
                            ),
                            pr_number=pr_number if pr_number is not None else s.pr_number,
                        )
                        break
                save_contract(contract_local, worktree_repo_path)
        except SliceCompletionInvariantError:
            # Fail loud — never swallow the completion invariant into the
            # best-effort save handler below (#3214).
            raise
        except Exception as save_err:  # noqa: BLE001
            # Contract load/save under per-pipeline state lock.
            # Catches loader validation errors, atomic-rename / fdopen
            # I/O failures, and pydantic re-serialisation errors.
            # Best-effort: the in-memory scheduler still reflects
            # COMPLETE for this pass; next start_pipeline re-detects.
            logger.warning(
                "Failed to persist slice.status=COMPLETE",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(save_err),
            )
            return
        if commit_to_branch:
            _commit_and_push_slice_statefiles(
                f"Persist contract after slice {slice_id} completion (#3117)"
            )

    # Bootstrap reconciliation pass (#2549). Before the run loop ticks,
    # fold in two sources of "this slice is already done" state that
    # the scheduler (a pure rebuild from ``contract.slices``) cannot
    # see on its own:
    #
    # (A) Slices the contract already records as
    #     ``SliceStatus.COMPLETE`` — trusted directly, no I/O.
    # (B) Slices whose integration branch on origin is reachable from
    #     their parent's tip (PR merged). On a hit, also persist (A)
    #     so subsequent restarts skip the GitHub round-trip.
    #
    # Without this pass the scheduler would re-yield merged slices as
    # READY and ``create_slice_integration_branch`` would
    # non-fast-forward-reject. Best-effort: failure falls through to
    # the run loop.
    bootstrap_complete: list[str] = []
    bootstrap_merged: list[str] = []

    # Layer (A): cheap, no I/O. Trust contract-recorded COMPLETE status —
    # but verify the recorded COMPLETE is not itself a #3214 false-complete
    # (an interior forest node persisted COMPLETE with pending tasks, no
    # PR, no merge). Blindly trusting a corrupt contract here is how the
    # false-complete propagated into the scheduler and wedged the chain.
    # On an invalid record, alert and decline to trust it — route the
    # slice through Layer-B/C so it is re-evaluated and (re-)run rather
    # than silently skipped.
    #
    # Note: a COMPLETE slice that recorded *no* durable evidence (no
    # pr_number, no integration_base_sha — e.g. a legacy pre-#2871
    # contract from before integration_base_sha existed) is distrusted
    # here on every restart, even when it was genuinely merged. That is
    # intentional, not a bug: such a slice falls through to Layer-B,
    # where origin-side merge detection re-confirms it and re-marks it
    # COMPLETE. The outcome stays correct; the only cost is one extra
    # GitHub round-trip per restart. A slice that forked under current
    # code *usually* records integration_base_sha and is trusted here
    # directly — but that write is best-effort (the get_remote_branch_sha
    # call at slice spawn swallows failures and degrades to ancestor-only
    # detection), so a current-code slice whose base-SHA write failed also
    # falls through to Layer-B and self-corrects identically to the legacy
    # case above.
    #
    # Known limitation (#3253): a slice that pre-fix code *already* persisted
    # COMPLETE basis="merged" with a stale ``integration_base_sha`` and no
    # produced commits / PR is still trusted here — Layer-A validates with no
    # ``basis`` (the #3253 merged-empty guard keys on ``basis == "merged"``,
    # which Layer-A never supplies), so the ``forked`` free-pass below accepts
    # the stale fork base. This is deliberately *not* fixed by broadening the
    # guard to the basis-less path: a legitimate ``basis="consensus_complete"``
    # slice can also have no PR and no recorded task commit (best-effort agent
    # recording + ``pr_number`` None on an unparseable PR URL, #3122), so
    # re-running on "no commit + no PR" alone here would re-run genuinely
    # completed work. The #3253 fix prevents the corrupt write going forward;
    # a pipeline already wedged by this exact bug *before* the upgrade needs a
    # manual contract touch-up (clear the slice's COMPLETE status) rather than
    # self-healing on restart.
    layer_b_candidates = []
    for s in slices:
        if s.status == SliceStatus.COMPLETE:
            invalid = _validate_slice_completion_basis(s, pr_number=s.pr_number)
            if invalid is not None:
                logger.error(
                    "Contract records slice COMPLETE but the completion basis is "
                    "invalid — NOT trusting it; re-evaluating the slice (#3214)",
                    pipeline_id=pipeline_id,
                    slice_id=s.id,
                    reason=invalid,
                )
                layer_b_candidates.append(s)
                continue
            scheduler.record_complete(s.id)
            bootstrap_complete.append(s.id)
            continue
        layer_b_candidates.append(s)

    # Layer (B): origin-side detection for slices not yet recorded as
    # COMPLETE on the contract. Each helper call uses its own synthetic
    # gateway session, so we parallelise across slices to keep startup
    # latency bounded as forests grow. Cap workers so a large forest
    # doesn't burst against the gateway.
    if pipeline.repo and layer_b_candidates:

        def _bootstrap_check_one(slice_obj: Any) -> tuple[str, bool]:
            # Prefer the parent branch the slice was actually forked
            # off of (recorded by ``_run_one_slice_inner``). Falls back
            # to the dependency-derived parent for slices that never
            # made it through ``_run_one_slice_inner`` (e.g. fresh
            # contract on first run). Both should agree today, but a
            # future re-plan that mutates ``dependencies`` post-creation
            # would diverge — preferring the recorded value future-
            # proofs the check.
            if slice_obj.parent_branch_at_creation:
                parent_branch_for_check = slice_obj.parent_branch_at_creation
            elif slice_obj.dependencies:
                parent_branch_for_check = f"{issue_branch}/{slice_obj.dependencies[0]}"
            else:
                parent_branch_for_check = pipeline_branch
            integration_branch_for_check = f"{issue_branch}/{slice_obj.id}"
            try:
                merged = spawner.gateway.is_slice_branch_merged_into_parent(
                    pipeline_id,
                    str(worktree_repo_path),
                    integration_branch=integration_branch_for_check,
                    parent_branch=parent_branch_for_check,
                    # #2871 — pass the recorded fork base so an empty
                    # (un-started) slice branch whose tip is still at its
                    # creation base is not mistaken for merged work.
                    integration_base_sha=slice_obj.integration_base_sha,
                    # Read-only ancestry check run by the orchestrator's
                    # slice-loop scheduler; attribute to the orchestrator
                    # in the audit log, not a phantom coder (#2919).
                    agent_role="orchestrator",
                    mode=gateway_mode,  # type: ignore[arg-type]
                )
            except Exception as detect_err:  # noqa: BLE001
                # Gateway `is_slice_branch_merged_into_parent` call.
                # Catches gateway HTTP/timeout errors (GatewayError),
                # low-level socket / DNS errors (OSError), and any
                # rare argument-shape errors. Default to "not merged"
                # so the slice can still spawn fresh.
                logger.warning(
                    "Bootstrap merged-detection raised; treating slice as not-merged",
                    pipeline_id=pipeline_id,
                    slice_id=slice_obj.id,
                    error=str(detect_err),
                )
                return slice_obj.id, False
            # #3253 — guard against a false-positive merged result. A slice
            # whose producers never committed (no produced task commit) and
            # that has no slice PR has an empty integration branch: its tip
            # is still the fork base, so it is trivially an ancestor of an
            # advanced parent and the origin ancestry check reports it
            # merged. Marking it COMPLETE basis=merged silently drops the
            # slice and lets the pipeline complete with its work missing —
            # the restart-to-retry failure mode (#3138 producer exhaustion →
            # operator restart → false-complete). Override to not-merged so
            # the slice falls through to Layer-C and re-runs. A genuine merge
            # has produced commits or a recorded PR, so this never overrides
            # a real merge.
            if (
                merged
                and not _slice_produced_commits(slice_obj)
                and getattr(slice_obj, "pr_number", None) is None
            ):
                logger.warning(
                    "Bootstrap merged-detection overridden: origin ancestry "
                    "reports merged but the slice has no produced task commit "
                    "and no PR — empty/un-started branch, re-running rather than "
                    "false-completing as merged (#3253)",
                    pipeline_id=pipeline_id,
                    slice_id=slice_obj.id,
                )
                return slice_obj.id, False
            return slice_obj.id, bool(merged)

        max_workers = min(len(layer_b_candidates), 8)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"slice-bootstrap-{pipeline_id}",
        ) as bootstrap_pool:
            results = list(bootstrap_pool.map(_bootstrap_check_one, layer_b_candidates))

        for slice_id, already_merged in results:
            if already_merged:
                scheduler.record_complete(slice_id)
                _persist_slice_status_complete(slice_id, basis="merged", commit_to_branch=False)
                bootstrap_merged.append(slice_id)

    # Layer (C): non-COMPLETE slice classification (slice-4 TASK-4-4).
    # After layers A (contract-recorded COMPLETE) and B (merged on
    # origin), classify the remaining slices per the 5-way matrix
    # so crash recovery does not respawn agents for a slice that is
    # already running, silently advance a slice whose HITL is still
    # pending, or treat a corrupt status enum as a benign default:
    #
    #   (1) IN_PROGRESS, no commits on integration branch → no Layer-C
    #       action; the scheduler will re-yield the slice as READY and
    #       the run loop spawns fresh agents.
    #   (2) IN_PROGRESS, commits on integration branch, consensus
    #       NOT reached → call ``scheduler.mark_spawned`` so the run
    #       loop does NOT respawn. Per-slice tracker reconstruction
    #       is handled at orchestrator boot by
    #       startup_reconciliation.py (slice-4 TASK-4-5); the
    #       producer pods (if alive) or the lazy spawn-on-need path
    #       carry the slice forward.
    #   (3) IN_PROGRESS, commits on integration branch, consensus
    #       REACHED, slice PR NOT opened → mark COMPLETE so the
    #       slice-PR opener path (with TASK-3-2 idempotency
    #       pre-flight) fires on the next loop iteration; do not
    #       respawn agents.
    #   (4) BLOCKED (HITL pending) → preserve the BLOCKED status.
    #       Verify the HITL decision is still on the contract; if
    #       not, surface an OVERSEER_ALERT so a human investigates.
    #   (5) Unknown / corrupt state (impossible status enum value)
    #       → surface an OVERSEER_ALERT instead of silently
    #       re-yielding as READY.
    bootstrap_resumed: list[str] = []
    bootstrap_consensus_complete: list[str] = []
    bootstrap_blocked: list[str] = []
    bootstrap_corrupt: list[str] = []
    bootstrap_reclassified_fresh: list[str] = []  # resume-but-dead → fresh (#2914)
    layer_b_marked_complete = set(bootstrap_merged)
    for s in layer_b_candidates:
        if s.id in layer_b_marked_complete:
            continue
        classification = _classify_non_complete_slice(
            pipeline_id=pipeline_id,
            slice_obj=s,
            issue_branch=issue_branch,
            pipeline_repo=pipeline.repo,
            worktree_repo_path=worktree_repo_path,
            gateway=spawner.gateway,
            gateway_mode=gateway_mode,
            consensus_tracker_lookup=_lookup_peer_consensus_tracker_or_none,
        )
        if classification == "consensus_complete":
            # Case 3 — louder than fresh-spawn but quieter than
            # case-4/5 HITL. A warning here makes the non-trivial
            # recovery (consensus reached pre-crash, PR not opened)
            # auditable in operator logs without paging anyone
            # (reviewer_code v1 non-blocking).
            logger.warning(
                "Layer-C case 3 — slice consensus reached pre-restart but "
                "slice PR was never opened; marking COMPLETE so the next "
                "loop iteration runs the slice-PR opener (slice-4 TASK-4-4)",
                pipeline_id=pipeline_id,
                slice_id=s.id,
            )
            scheduler.record_complete(s.id)
            _persist_slice_status_complete(s.id, basis="consensus_complete", commit_to_branch=False)
            bootstrap_consensus_complete.append(s.id)
            continue
        if classification == "resume":
            # Verify agents are actually live before marking as spawned (#2914).
            # On restart_phase, agents were torn down but contract still shows
            # IN_PROGRESS with commits — we must not mark_spawned when cohort
            # is absent, or the pipeline wedges with no agents running.
            if _slice_agents_alive(spawner, pipeline_id, s.id):
                scheduler.mark_spawned(s.id)
                bootstrap_resumed.append(s.id)
            else:
                logger.warning(
                    "Layer-C resume classification but no live agents; "
                    "treating as fresh to force re-spawn (#2914)",
                    pipeline_id=pipeline_id,
                    slice_id=s.id,
                )
                bootstrap_reclassified_fresh.append(s.id)
            continue
        if classification == "blocked":
            bootstrap_blocked.append(s.id)
            continue
        if classification == "corrupt":
            bootstrap_corrupt.append(s.id)
            continue
        # "fresh" → no Layer-C action, scheduler re-yields READY.

    # The bootstrap passes above persist with ``commit_to_branch=False``
    # — one batched commit+push here covers every reconciled slice
    # (Layer B merged-detection + Layer-C case 3) instead of a commit
    # per slice (#3117).
    if bootstrap_merged or bootstrap_consensus_complete:
        _commit_and_push_slice_statefiles(
            "Persist slice completion statuses after bootstrap reconciliation (#3117)"
        )

    if bootstrap_complete or bootstrap_merged:
        logger.info(
            "Slice bootstrap reconciliation marked slices complete",
            pipeline_id=pipeline_id,
            already_complete_on_contract=bootstrap_complete,
            detected_merged_on_origin=bootstrap_merged,
        )
    if (
        bootstrap_resumed
        or bootstrap_consensus_complete
        or bootstrap_blocked
        or bootstrap_corrupt
        or bootstrap_reclassified_fresh
    ):
        # NOTE: include ``bootstrap_blocked`` in the gate (reviewer_code
        # v3 NACK fix) — a bootstrap pass whose only Layer-C activity is
        # BLOCKED slices was previously suppressing the audit-trail line
        # entirely. Case-4 escalation still fires, but operators need
        # the structured "we saw a blocked slice" log to spot
        # pending-HITL backlogs without grepping for the side-effect.
        #
        # Also include ``bootstrap_reclassified_fresh`` (#2914) — resume-
        # classified slices that were re-verified against k8s and found
        # to have no live agents. Surfacing the reclassification here
        # gives operators a structured audit trail for the
        # ``restart_phase``-recovery path.
        logger.info(
            "Slice bootstrap reconciliation classified non-COMPLETE slices (slice-4 TASK-4-4)",
            pipeline_id=pipeline_id,
            resumed=bootstrap_resumed,
            consensus_complete_unrecorded=bootstrap_consensus_complete,
            blocked=bootstrap_blocked,
            corrupt=bootstrap_corrupt,
            reclassified_fresh=bootstrap_reclassified_fresh,
        )
    # Case 5 — escalate via HITL so the pipeline pauses until the
    # operator picks an option (reviewer_contract / reviewer_code v1
    # blocker). OVERSEER_ALERT alone is too weak — it surfaces but
    # does not gate progress. The Decision lands on the contract via
    # ``_escalate_corrupt_slice_to_hitl`` so ``/sdlc`` reads it on
    # the next poll.
    _current_phase = getattr(pipeline, "current_phase", None)
    for _corrupt_slice_id in bootstrap_corrupt:
        try:
            _escalate_corrupt_slice_to_hitl(
                pipeline_id=pipeline_id,
                slice_id=_corrupt_slice_id,
                worktree_repo_path=worktree_repo_path,
                current_phase=_current_phase,
            )
        except Exception as escalate_err:  # noqa: BLE001
            logger.warning(
                "Failed to escalate corrupt-state slice to HITL during "
                "bootstrap (slice-4 TASK-4-4 case 5)",
                pipeline_id=pipeline_id,
                slice_id=_corrupt_slice_id,
                error=str(escalate_err),
            )
    # Case 4 — symmetric HITL escalation for BLOCKED-without-HITL.
    for _blocked_slice_id, _escalate_reason in [
        (sid, "no pending HITL decision found on contract")
        for sid in bootstrap_blocked
        if not _slice_has_pending_decision(sid, getattr(contract, "decisions", None) or [])
    ]:
        try:
            _escalate_blocked_slice_to_hitl(
                pipeline_id=pipeline_id,
                slice_id=_blocked_slice_id,
                reason=_escalate_reason,
                worktree_repo_path=worktree_repo_path,
                current_phase=_current_phase,
            )
        except Exception as escalate_err:  # noqa: BLE001
            logger.warning(
                "Failed to escalate blocked-without-HITL slice to HITL "
                "during bootstrap (slice-4 TASK-4-4 case 4)",
                pipeline_id=pipeline_id,
                slice_id=_blocked_slice_id,
                error=str(escalate_err),
            )

    reconciler_thread, reconciler_stop = _start_stacked_pr_reconciler(
        pipeline_id,
        _contract_loader,
        spawner.gateway,
        pipeline,
        worktree_repo_path=worktree_repo_path,
        repo=getattr(pipeline, "repo", None),
    )

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
                    except ImportError:
                        # Symmetry-only import; the module not being
                        # available means the cascade alert path can't
                        # call the gateway, but the warning above is
                        # the always-on fallback.
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

            def _run_one_slice(slice_id: str, parent_slice_id: str | None) -> tuple[int, str]:
                # Release the global-admission slot when the slice
                # exits, regardless of how (consensus, failure, raised
                # exception). Idempotent — safe even if a future
                # codepath calls release() somewhere else (#2241 gap 1).
                try:
                    return _run_one_slice_inner(slice_id, parent_slice_id)
                finally:
                    global_slice_admit.release(pipeline_id, slice_id)

            def _run_one_slice_inner(
                slice_id: str,
                parent_slice_id: str | None,  # noqa: ARG001 — kept for caller compat; resolver reads contract
            ) -> tuple[int, str]:
                # Resolve parent branch for stacking via
                # :func:`_resolve_slice_base_branch` (#2777, cq-2 / cq-4 /
                # cq-9 / cq-10). The helper handles both:
                #
                # * eager-persisted ``parent_branch_at_creation`` (the
                #   primary path post-slice-4 TASK-4-2), and
                # * fresh-pipeline derivation from
                #   ``slice.dependencies[0]`` (the path #2777's slice-2
                #   takes before slice-4 lands).
                #
                # The legacy ``egg/<id>/context`` branch was removed in
                # cq-4 so slice-1 (the root) now stacks on
                # ``pipeline_branch`` like every other root slice — the
                # work-branch context PR's diff already encompasses the
                # slice-1 integration branch via ancestry.
                # #2928: wire a parent-branch-existence probe so the
                # resolver can tell a FRESH non-root slice (whose
                # dependency parent branch is still on origin → stack
                # on it) apart from an orphaned one (parent merged
                # into ``work`` and cascade-deleted → base on
                # ``pipeline_branch``). This replaces the pre-#2928
                # merge-base probe, which probed the slice's OWN
                # integration branch — non-existent on a first run —
                # and so mis-routed every fresh non-root slice onto
                # ``work`` whenever ``work`` had advanced ahead of the
                # parent. Repoless test scaffolds short-circuit to
                # ``True`` (no origin to check; the derived parent is
                # the correct DAG target), mirroring the resolver's
                # conservative "assume parent exists" default.
                #
                # IMPORTANT: this wrapper calls the STRICT ls-remote
                # variant (``ls_remote_branch_strict``) so a gateway /
                # network / policy failure RAISES into the resolver's
                # ``try/except`` instead of being collapsed to
                # ``False``. The lenient ``ls_remote_branch`` /
                # ``get_remote_branch_sha`` helpers swallow all
                # exceptions and return ``False`` / ``None`` for both
                # "branch absent" AND "gateway error" — using either
                # here would silently route a real slice onto
                # ``pipeline_branch`` on a flaky gateway, re-creating
                # the #2928 wedge that this PR claims to fix.
                def _probe_parent_branch_exists(parent_branch: str) -> bool:
                    if not pipeline.repo:
                        return True
                    return spawner.gateway.ls_remote_branch_strict(
                        pipeline_id,
                        str(worktree_repo_path),
                        f"refs/heads/{parent_branch}",
                        mode=gateway_mode,  # type: ignore[arg-type]
                    )

                parent_branch = _resolve_slice_base_branch(
                    contract,
                    slice_id,
                    pipeline_id=pipeline_id,
                    pipeline_branch=pipeline_branch,
                    parent_branch_exists=_probe_parent_branch_exists,
                )
                integration_branch = f"{issue_branch}/{slice_id}"

                # Persist the parent-branch reference on the contract
                # under the per-pipeline state lock so a concurrent
                # tester / documenter contract write doesn't race with
                # ours (reviewer_code v4 #5). While we hold the contract,
                # also read back any integration_base_sha recorded on a
                # prior run (#2871) — on a restart this lets the race
                # check below tell an empty branch apart from a merged
                # one. It is ``None`` on a slice's first run (recorded
                # only after the branch is created, just below).
                recorded_base_sha: str | None = None
                # #3253 — capture whether the slice has any produced task
                # commit / PR while we hold the contract, so the race-merged
                # skip below cannot mistake an empty / un-started branch for
                # a merged one (see the merged-acceptance guard below).
                slice_produced_work = False
                try:
                    with get_pipeline_state_lock(pipeline_id):
                        contract_local = load_contract(pipeline_id, worktree_repo_path)
                        for s in contract_local.slices:
                            if s.id == slice_id:
                                s.parent_branch_at_creation = parent_branch
                                # Slice-4 TASK-4-2: flip PENDING →
                                # IN_PROGRESS in the SAME contract write
                                # that persists parent_branch_at_creation
                                # (cq-9). Crash recovery (TASK-4-4
                                # Layer C) now has a single signal to
                                # distinguish a fresh slice from one
                                # whose run was interrupted between
                                # status flip and branch creation.
                                # Idempotent on re-entry (e.g. orphan
                                # reconciler): only PENDING is flipped;
                                # COMPLETE / BLOCKED / IN_PROGRESS are
                                # left untouched.
                                if s.status == SliceStatus.PENDING:
                                    s.status = SliceStatus.IN_PROGRESS
                                recorded_base_sha = s.integration_base_sha
                                slice_produced_work = (
                                    _slice_produced_commits(s) or s.pr_number is not None
                                )
                                break
                        save_contract(contract_local, worktree_repo_path)
                except Exception as save_err:  # noqa: BLE001
                    # Contract load/save under per-pipeline state lock.
                    # Same exception surface as the COMPLETE-persist
                    # site above (loader validation, atomic-rename
                    # I/O, pydantic re-serialisation). Best-effort.
                    logger.warning(
                        "Failed to persist parent_branch_at_creation",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        error=str(save_err),
                    )

                # Race protection: a slice's PR can be merged between
                # bootstrap reconciliation and this spawn. Detect and
                # skip to COMPLETE so the create-branch push below
                # doesn't non-fast-forward (#2549).
                if pipeline.repo:
                    try:
                        already_merged = spawner.gateway.is_slice_branch_merged_into_parent(
                            pipeline_id,
                            str(worktree_repo_path),
                            integration_branch=integration_branch,
                            parent_branch=parent_branch,
                            integration_base_sha=recorded_base_sha,
                            # Read-only ancestry check run by the
                            # orchestrator's slice-loop scheduler; attribute
                            # to the orchestrator, not a phantom coder (#2919).
                            agent_role="orchestrator",
                            mode=gateway_mode,  # type: ignore[arg-type]
                        )
                    except Exception as detect_err:  # noqa: BLE001
                        # Same `is_slice_branch_merged_into_parent`
                        # surface as the bootstrap pass above
                        # (GatewayError + OSError). Default to "not
                        # merged" so the slice can still spawn.
                        logger.warning(
                            "Slice merged-detection raised; treating as not-merged",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            error=str(detect_err),
                        )
                        already_merged = False
                    # #3253 — a slice with no produced task commit and no PR
                    # has an empty integration branch (tip still at the fork
                    # base); origin ancestry reports it merged because that
                    # base is trivially an ancestor of an advanced parent.
                    # Don't skip it as merged — spawn so it actually runs.
                    if already_merged and not slice_produced_work:
                        logger.info(
                            "Slice merged-detection ignored: no produced task commit "
                            "and no PR — empty/un-started branch, spawning instead of "
                            "skipping as merged (#3253)",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            integration_branch=integration_branch,
                            parent_branch=parent_branch,
                        )
                        already_merged = False
                    if already_merged:
                        logger.info(
                            "Slice already merged into parent on origin — skipping spawn (#2549)",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            integration_branch=integration_branch,
                            parent_branch=parent_branch,
                        )
                        scheduler.record_complete(slice_id)
                        _persist_slice_status_complete(slice_id, basis="merged")
                        try:
                            remove_peer_consensus_tracker(pipeline_id, slice_id)
                        except Exception:  # noqa: BLE001
                            # In-memory dict pop under a lock; only
                            # programming errors (KeyError, AttributeError)
                            # could fire. Bare-except keeps the slice
                            # COMPLETE/return path crash-proof.
                            pass
                        return 0, (
                            f"slice {slice_id}: already merged into "
                            f"{parent_branch} on origin — skipped"
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
                        # #3185 — the helper now returns the fork-base
                        # SHA it pushed the integration branch at (the
                        # parent tip resolved inside the call), or None
                        # on failure. Recording that SHA directly here
                        # replaces a prior best-effort
                        # ``get_remote_branch_sha`` re-fetch that could
                        # silently fail (no ``retry_transient``) and
                        # leave ``integration_base_sha`` unset — arming
                        # the empty-pre-created-branch trap on the next
                        # restart.
                        created_base_sha = spawner.gateway.create_slice_integration_branch(
                            pipeline_id,
                            str(worktree_repo_path),
                            integration_branch=integration_branch,
                            parent_branch=parent_branch,
                            # #2947 — hand the slice's recorded fork
                            # base to the gateway so a crash/restart
                            # over a branch that already carries this
                            # slice's commits (with an additively
                            # advanced parent) resumes in place
                            # instead of non-fast-forward-failing.
                            integration_base_sha=recorded_base_sha,
                            # Orchestrator pre-creates the slice
                            # integration branch on a synthetic session
                            # before agents spawn; attribute to the
                            # orchestrator, not a phantom coder (#2919).
                            # The push rides the slice-integration
                            # exemption (synthetic + branch shape), not a
                            # role gate.
                            agent_role="orchestrator",
                            mode=gateway_mode,  # type: ignore[arg-type]
                        )
                    except Exception as branch_err:  # noqa: BLE001
                        # Gateway `create_slice_integration_branch`
                        # call. Catches GatewayError (HTTP/timeout)
                        # and OSError (DNS / socket). Treat as failure
                        # so the cascade machinery surfaces a
                        # missing-parent error.
                        logger.error(
                            "Slice integration branch creation raised",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            error=str(branch_err),
                        )
                        created_base_sha = None
                    if created_base_sha is None:
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

                    # #2871 / #3185 — record the integration branch's fork
                    # base exactly once, on first creation. The branch was
                    # just pushed at the parent's tip and no agent has been
                    # spawned yet, so its origin tip still equals its base.
                    # Persisting it now lets a later restart's bootstrap
                    # reconciliation (and the race check above) tell an
                    # *empty* slice branch — tip still at this base, hence
                    # a trivial ancestor of an advanced parent — apart from
                    # a genuinely *merged* one whose tip moved past it. We
                    # only write it when unset so a restart over a branch
                    # that already carries slice commits (#2512 recovery)
                    # keeps its original base rather than the advanced tip.
                    # ``created_base_sha`` is the SHA the create call
                    # returned (no extra round-trip); it is an empty string
                    # on the unreachable no-op path
                    # (``integration_branch == parent_branch``), which we
                    # skip here.
                    if recorded_base_sha is None and created_base_sha:
                        try:
                            with get_pipeline_state_lock(pipeline_id):
                                contract_local = load_contract(pipeline_id, worktree_repo_path)
                                for s in contract_local.slices:
                                    if s.id == slice_id:
                                        s.integration_base_sha = created_base_sha
                                        break
                                save_contract(contract_local, worktree_repo_path)
                            recorded_base_sha = created_base_sha
                        except Exception as base_err:  # noqa: BLE001
                            # Contract load/save under per-pipeline state
                            # lock. Catches loader validation, atomic-
                            # rename I/O, and pydantic re-serialisation
                            # errors. Best-effort: the fork base is no
                            # longer a round-trip failure (the SHA came
                            # from the create call itself), so this now
                            # only fires on a contract-write failure — a
                            # transient the next run repairs on the same
                            # create path.
                            logger.warning(
                                "Failed to persist slice integration_base_sha "
                                "(#2871); a future restart re-records it on the "
                                "create path",
                                pipeline_id=pipeline_id,
                                slice_id=slice_id,
                                integration_branch=integration_branch,
                                error=str(base_err),
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
                    run_epoch=run_epoch,
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

                # Slice consensus reached — load the contract ONCE
                # under the per-pipeline state lock and reuse the same
                # snapshot for the #3125 evidence-reachability gate
                # AND the slice's PR data snapshot below. Both readers
                # previously took the lock independently; collapsing
                # them eliminates one file read + lock acquire per
                # slice close (#3125 review).
                #
                # The slice_pr_data block below originally documented
                # the lock as covering only the contract read so the
                # gateway HTTP round-trip wouldn't serialise other
                # writers — the same posture applies here: we release
                # the lock before the gateway call inside the gate.
                contract_post: Any | None = None
                try:
                    with get_pipeline_state_lock(pipeline_id):
                        contract_post = load_contract(pipeline_id, worktree_repo_path)
                except Exception as load_err:  # noqa: BLE001
                    logger.warning(
                        "Slice close: contract load failed (continuing) (#3125)",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        error=str(load_err),
                    )

                # #3125 — evidence-reachability gate: every commit SHA
                # cited by this slice's contract task records must be
                # an ancestor of the integration branch tip, or the
                # slice PR would ship without a deliverable the task
                # record claims is done (the post-confirmation
                # ``complete-task --commit`` unblock flow, #3124).
                # Fails the slice BEFORE any close side effect so the
                # cascade + HITL machinery surfaces the gap loudly.
                # ``contract_post`` may be None if the load above
                # raised — the gate falls back to its own load in that
                # case (and skips gracefully if that fails too).
                if pipeline.repo:
                    evidence_failure = _check_slice_evidence_reachability(
                        pipeline_id,
                        spawner,
                        worktree_repo_path,
                        slice_id,
                        integration_branch,
                        gateway_mode=gateway_mode,  # type: ignore[arg-type]
                        contract=contract_post,
                    )
                    if evidence_failure is not None:
                        scheduler.record_failure(slice_id)
                        return 1, evidence_failure

                # #3398 — per-slice green gate: execute the repo's
                # configured checks (repositories.yaml, via
                # get_repo_checks) against the integration-branch tip
                # in a sandboxed one-shot runner, and refuse to open
                # the slice PR while any check is red. Closes the
                # trust-vs-verify gap in the propose-time
                # checks_passed self-report. Same posture as the
                # evidence gate above: fail-open on infra errors,
                # fail-closed only on a definitive red verdict;
                # EGG_SLICE_GREEN_GATE is the operator switch
                # (off during rollout / log / on).
                if pipeline.repo:
                    try:
                        import slice_green_gate as _green_gate
                    except ImportError:
                        from .. import slice_green_gate as _green_gate  # type: ignore[no-redef]

                    green_gate_failure = _green_gate.run_slice_green_gate(
                        pipeline_id,
                        spawner,
                        slice_id,
                        integration_branch,
                        pipeline.repo,
                        gateway_mode=gateway_mode,  # type: ignore[arg-type]
                    )
                    if green_gate_failure is not None:
                        scheduler.record_failure(slice_id)
                        return 1, green_gate_failure

                # Snapshot the slice's PR data from the same loaded
                # contract — no second lock acquire, no second file
                # read.
                slice_pr_data: dict[str, Any] | None = None
                try:
                    if contract_post is not None:
                        slice_obj = next(
                            (s for s in contract_post.slices if s.id == slice_id),
                            None,
                        )
                        if slice_obj is not None and pipeline.repo:
                            # #2538: every slice carries the
                            # planner-authored narrative on its PR so
                            # reviewers see context on whichever slice
                            # they open first. Pre-#2777 cq-6 the
                            # terminal slice additionally carried a
                            # program-level rollup (test plan + manual
                            # steps + pre-merge obligations) and a
                            # ``[merge-gate]`` title marker. Under cq-4
                            # the merge gate is the up-front context
                            # PR (``egg/<id>/work → main``) opened by
                            # ``_open_context_pr_at_implement_start``,
                            # so every slice PR — terminal or not —
                            # now uses the same lean shape and the
                            # terminal-slice computation is gone.
                            program_pr = contract_post.pr
                            # #2745: derive 1-based slice position +
                            # total slice count from declared contract
                            # order so the slice PR title can carry
                            # ``[slice-N/M]``.
                            slice_count = len(contract_post.slices)
                            slice_index_lookup = next(
                                (
                                    i + 1
                                    for i, s in enumerate(contract_post.slices)
                                    if s.id == slice_id
                                ),
                                None,
                            )
                            # Union of ``task.files_affected`` across the
                            # slice's tasks; rendered under
                            # ``## This slice`` so reviewers see what
                            # this slice actually touches without
                            # opening the diff (#2745).
                            slice_files_affected_list: list[str] = []
                            seen_paths: set[str] = set()
                            for t in slice_obj.tasks or []:
                                for path in t.files_affected or []:
                                    if path and path not in seen_paths:
                                        seen_paths.add(path)
                                        slice_files_affected_list.append(path)
                            # #3393 slice-4 / task-4-1: route this slice's
                            # PR to its OWN repo (``resolve_slice_repo`` →
                            # ``slice.repo`` else the pipeline primary) and
                            # gather CROSS-repo coordination references for
                            # the PR body. Same-repo relationships are left
                            # to ``## Stack``, so for an N=1 pipeline
                            # ``slice_repo`` is the single repo and both
                            # ref sets are empty — behaviour is unchanged.
                            try:
                                from models import (  # type: ignore[no-redef]
                                    resolve_slice_repo,
                                )
                            except ImportError:
                                from ..models import (  # type: ignore[no-redef]
                                    resolve_slice_repo,
                                )
                            slice_repo = resolve_slice_repo(slice_obj, pipeline) or pipeline.repo
                            sibling_pr_refs: list[dict[str, Any]] = []
                            for other in contract_post.slices:
                                if other.id == slice_id:
                                    continue
                                other_repo = resolve_slice_repo(other, pipeline) or pipeline.repo
                                if other_repo and other_repo != slice_repo and other.pr_number:
                                    sibling_pr_refs.append(
                                        {"repo": other_repo, "number": other.pr_number}
                                    )
                            # Dependent-slice upstream PR — surfaced only
                            # when the upstream slice is in a DIFFERENT repo
                            # (a same-repo parent is the stack base already
                            # rendered by ``## Stack``).
                            upstream_pr_ref: dict[str, Any] | None = None
                            upstream_ids = slice_obj.dependencies or []
                            if upstream_ids:
                                upstream = next(
                                    (s for s in contract_post.slices if s.id == upstream_ids[0]),
                                    None,
                                )
                                if upstream is not None and upstream.pr_number:
                                    upstream_repo = (
                                        resolve_slice_repo(upstream, pipeline) or pipeline.repo
                                    )
                                    if upstream_repo and upstream_repo != slice_repo:
                                        upstream_pr_ref = {
                                            "repo": upstream_repo,
                                            "number": upstream.pr_number,
                                        }
                            # #3393 slice-5 / task-5-1: a slice with a
                            # CROSS-repo dependency opens its PR as a DRAFT
                            # — cross-repo edges can't stack, so the
                            # dependent slice is developed in parallel and
                            # only its PR *ready* transition waits on the
                            # merge gate (auto draft→ready when the upstream
                            # merges, else a HITL hold). A dep is cross-repo
                            # iff the upstream slice resolves to a DIFFERENT
                            # repo; same-repo-only deps and N=1 pipelines
                            # stay non-draft (behaviour unchanged). Checks
                            # ALL deps so any cross-repo upstream holds it.
                            cross_repo_draft = False
                            for _dep_id in slice_obj.dependencies or []:
                                _dep = next(
                                    (s for s in contract_post.slices if s.id == _dep_id),
                                    None,
                                )
                                if _dep is None:
                                    continue
                                _dep_repo = resolve_slice_repo(_dep, pipeline) or pipeline.repo
                                if _dep_repo and _dep_repo != slice_repo:
                                    cross_repo_draft = True
                                    break
                            slice_pr_data = {
                                # #3393 slice-4: the repo this slice's PR is
                                # opened in + its cross-repo coordination
                                # references (empty for N=1).
                                "slice_repo": slice_repo,
                                # #3393 slice-5: open draft when this slice
                                # has a cross-repo dependency (see above).
                                "cross_repo_draft": cross_repo_draft,
                                "sibling_pr_refs": sibling_pr_refs,
                                "upstream_pr_ref": upstream_pr_ref,
                                "slice_name": slice_obj.name or slice_id,
                                # Planner's reviewer-facing summary —
                                # rendered as the slice PR body's lead
                                # paragraph (#3115). Empty for
                                # pre-#3115 contracts.
                                "slice_goal": getattr(slice_obj, "goal", "") or None,
                                "slice_tasks": [
                                    {
                                        "id": t.id,
                                        "description": t.description,
                                        "acceptance_criteria": t.acceptance_criteria,
                                    }
                                    for t in (slice_obj.tasks or [])
                                ],
                                "slice_index": slice_index_lookup,
                                "slice_count": slice_count,
                                "slice_files_affected": slice_files_affected_list or None,
                                # ``context_pr_number`` is populated by
                                # ``_open_context_pr_at_implement_start``
                                # at the plan→implement boundary (#2777
                                # cq-4). When the contract linkage is
                                # missing (e.g. ``contract.pr`` is None
                                # on an implement-start resume, #3100),
                                # fall back to ``pipeline.pr_number`` —
                                # the pipeline-level mirror written by
                                # ``_persist_context_pr_number`` whose
                                # sole post-#2777 writer is the same
                                # opener — so the slice PR still links
                                # its base PR (#3115). When both are
                                # None — should be unreachable under
                                # the hard-required opener but kept as
                                # defence-in-depth — ``create_slice_pr``
                                # falls back to the pre-#2745 inline-
                                # narrative body so the slice PR stays
                                # reviewable as a standalone diff
                                # against ``/work``.
                                "context_pr_number": (
                                    (program_pr.context_pr_number if program_pr else None)
                                    or pipeline.pr_number
                                ),
                                "program_title": (program_pr.title if program_pr else None),
                                "program_description": (
                                    program_pr.description if program_pr else None
                                ),
                                "program_test_plan": (program_pr.test_plan if program_pr else None),
                                "program_manual_steps": (
                                    program_pr.manual_steps if program_pr else None
                                ),
                            }
                except Exception as attr_err:  # noqa: BLE001
                    # Nested attribute traversal on slice/program PR
                    # objects (the contract load was lifted out to the
                    # block above). Surface is AttributeError /
                    # KeyError on partially-populated PR rollup
                    # fields. Continue without slice_pr_data (the
                    # gateway PR creation just below is gated on it
                    # being non-None).
                    logger.warning(
                        "Slice PR pre-load failed (continuing)",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        error=str(attr_err),
                    )

                # Persist this slice's per-slice BRC consensus history
                # onto its integration branch as the final
                # orchestrator-authored commit before the slice PR is
                # opened, so reviewers see the consensus transcript in
                # the PR diff (#2548). Best-effort + idempotent on
                # retry; per-slice files live ONLY on the integration
                # branch.
                if pipeline.repo:
                    try:
                        _commit_slice_brc_history_to_integration_branch(
                            pipeline,
                            spawner,
                            worktree_repo_path,
                            slice_id,
                            integration_branch,
                            gateway_mode=gateway_mode,  # type: ignore[arg-type]
                        )
                    except Exception as brc_commit_err:  # noqa: BLE001
                        # Per-slice BRC commit helper calls into the
                        # full git/gateway/message-store machinery
                        # — the exception surface is unbounded
                        # (gateway push failures, git plumbing
                        # errors, message-store reads, file I/O).
                        # Best-effort: the BRC transcript commit is
                        # non-essential to slice consensus.
                        logger.warning(
                            "Per-slice BRC commit raised (continuing) (#2548)",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            error=str(brc_commit_err),
                        )

                pr_created = True
                slice_pr_url: str | None = None
                slice_pr_number: int | None = None
                if slice_pr_data is not None and pipeline.repo:
                    # Best-effort real-diff summary for the PR body
                    # (#3115) — commit subjects + diffstat from the
                    # pushed integration branch. (None, None) on any
                    # failure; the PR opens without the section.
                    commit_subjects, diffstat = _build_slice_diff_summary(
                        pipeline,
                        spawner,
                        worktree_repo_path,
                        integration_branch,
                        parent_branch,
                        gateway_mode=gateway_mode,  # type: ignore[arg-type]
                    )
                    try:
                        slice_pr_url = spawner.gateway.create_slice_pr(
                            pipeline_id=pipeline_id,
                            # #3393 slice-4 / task-4-1: route to the slice's
                            # own repo (falls back to the pipeline primary
                            # when ``slice.repo`` is absent — the N=1 case).
                            repo=slice_pr_data["slice_repo"] or pipeline.repo,
                            slice_id=slice_id,
                            slice_name=slice_pr_data["slice_name"],
                            slice_tasks=slice_pr_data["slice_tasks"],
                            head=integration_branch,
                            base=parent_branch,
                            issue_number=issue_number,
                            agent_role="orchestrator",
                            mode=gateway_mode,  # type: ignore[arg-type]
                            # #3393 slice-5 / task-5-1: draft when this
                            # slice has a cross-repo dependency; the merge
                            # gate marks it ready on upstream merge (or a
                            # HITL hold releases it). False for N=1.
                            draft=slice_pr_data["cross_repo_draft"],
                            program_title=slice_pr_data["program_title"],
                            program_description=slice_pr_data["program_description"],
                            program_test_plan=slice_pr_data["program_test_plan"],
                            program_manual_steps=slice_pr_data["program_manual_steps"],
                            slice_index=slice_pr_data["slice_index"],
                            slice_count=slice_pr_data["slice_count"],
                            slice_files_affected=slice_pr_data["slice_files_affected"],
                            context_pr_number=slice_pr_data["context_pr_number"],
                            slice_goal=slice_pr_data["slice_goal"],
                            diffstat=diffstat,
                            commit_subjects=commit_subjects,
                            sibling_pr_refs=slice_pr_data["sibling_pr_refs"],
                            upstream_pr_ref=slice_pr_data["upstream_pr_ref"],
                        )
                    except Exception as pr_err:  # noqa: BLE001
                        # Single `gateway.create_slice_pr` HTTP call.
                        # Catches GatewayError (HTTP) and OSError
                        # (DNS / socket). Mark pr_created=False so
                        # the cascade machinery fires.
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

                # Parse the slice PR number from the returned URL
                # (#3122) — same trailing-boundary pattern the context-
                # PR opener uses, narrowed to ``[1-9]\d*`` so a
                # malformed ``/pull/0/...`` URL doesn't make it as far
                # as ``Slice.pr_number``'s ``ge=1`` validator (which
                # would silently downgrade to a warning log via the
                # save try/except in ``_persist_slice_status_complete``).
                # Best-effort: an unparseable URL just means the
                # linkage isn't recorded this pass; the idempotent
                # ``create_slice_pr`` re-yields it on a resume.
                if slice_pr_url:
                    pr_match = re.search(r"/pull/([1-9]\d*)(?:[/?#]|$)", slice_pr_url)
                    if pr_match:
                        slice_pr_number = int(pr_match.group(1))

                # Hold the per-pipeline state lock across both the
                # contract-write (``_persist_slice_status_complete``
                # itself reacquires this RLock) and the context-PR
                # body refresh (load + compose + push). Without the
                # outer lock, two slices in the same wave could
                # interleave between persist and push so the slice
                # whose refresh starts earlier but lands later
                # clobbers the body that already included both links
                # — and because no later slice fires a refresh, the
                # final slice's ``— #N`` link would stay missing
                # forever. Serializing here bounds the per-slice tail
                # latency by one gateway PATCH per concurrent slice
                # rather than racing them.
                with get_pipeline_state_lock(pipeline_id):
                    scheduler.record_complete(slice_id)
                    # Reaching here means ``_run_concurrent_phase`` returned
                    # success (BRC consensus) AND ``pr_created`` gated above —
                    # a verified completion independent of whether the PR URL
                    # parsed to a number (#3122 stub URLs leave
                    # ``slice_pr_number`` None). Declare the consensus basis so
                    # the #3214 invariant accepts it; ``pr_number`` is still
                    # passed for the slice-table linkage.
                    _persist_slice_status_complete(
                        slice_id,
                        pr_number=slice_pr_number,
                        pr_url=slice_pr_url if slice_pr_number else None,
                        basis="consensus_complete",
                    )

                    # Refresh the context PR body so its slice table
                    # links the PR that just opened (#3122). Strictly
                    # cosmetic and best-effort: every failure path
                    # inside logs + returns False without raising, and
                    # the slice outcome below never depends on it.
                    if slice_pr_number:
                        _refresh_context_pr_body(
                            pipeline_id,
                            pipeline=pipeline,
                            spawner=spawner,
                            worktree_repo_path=worktree_repo_path,
                            identifier=_pipeline_identifier(pipeline.issue_number, pipeline_id),
                            gateway_mode=gateway_mode,
                        )

                try:
                    remove_peer_consensus_tracker(pipeline_id, slice_id)
                except Exception:  # noqa: BLE001
                    # In-memory dict pop under a lock; same crash-proof
                    # defence-in-depth as the merged-skip branch above.
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
                        # fut.result() re-raises whatever the slice
                        # worker raised. Workers call into the full
                        # implement-phase machinery (gateway, contract,
                        # spawner, message store, docker) so the
                        # exception surface is unbounded; mark the
                        # slice failed and continue rather than tearing
                        # down the whole wave.
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
                        from ..message_store import (  # type: ignore[no-redef]
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
        except RuntimeError:
            # Thread.join only raises RuntimeError (e.g. joining the
            # current thread). Other failures are silent timeouts.
            pass

    aggregated = "\n".join(aggregate_logs) if aggregate_logs else "Slice loop completed."
    return overall_exit, aggregated


def _clear_stale_impasses_for_producers(
    repo_path: Path,
    pipeline_id: str,
    producer_roles: "list[ContractAgentRole]",  # noqa: UP037
    *,
    cleanup_reason: str,
) -> None:
    """Drop the ``impasse`` field from each producer's per-pipeline
    agent-output file before the next BRC cycle.

    ``save_agent_output`` writes with ``mode="w"`` so a producer that
    respawns and reaches its handoff write will overwrite the stale
    impasse on its own. But if a producer crashes before writing in the
    next iteration (or if the implement roster ever becomes
    contract-task-driven, in which case a producer with no remaining
    tasks won't spawn at all), the iter-N impasse file would persist
    into iter-N+1's ``collect_impasses`` scan and re-trigger routing on
    a stale signal — which the ``delegation_attempts`` counter would
    then translate into a spurious "second impasse on same task" HITL
    escalation.

    Pre-clearing the field keeps ``collect_impasses`` honest about what
    came out of the *current* iteration only. Other top-level fields on
    the agent output (``handoff_data``, ``role``, anything else) are
    preserved.
    """
    for role_enum in producer_roles:
        try:
            existing = load_agent_output(repo_path, role_enum, identifier=pipeline_id)
        except Exception as exc:  # noqa: BLE001
            # Best-effort agent-output file read. Catches OSError on
            # the file read, json.JSONDecodeError on parse, and
            # pydantic.ValidationError on the role-specific shape.
            # Continue (no impasse to clear if the file is unreadable).
            logger.debug(
                "Could not pre-load agent output to clear stale impasse",
                pipeline_id=pipeline_id,
                role=role_enum.value,
                error=str(exc),
            )
            continue
        if not isinstance(existing, dict) or "impasse" not in existing:
            continue
        cleaned = {k: v for k, v in existing.items() if k != "impasse"}
        try:
            save_agent_output(
                repo_path,
                role_enum,
                cleaned,
                identifier=pipeline_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Atomic file write of JSON-serialisable dict. Catches
            # OSError (write/rename), TypeError/ValueError (non-
            # serialisable value sneaking in). Continue — the stale
            # impasse will re-trigger routing but the delegation
            # counter still bounds the retry.
            logger.warning(
                "Failed to clear stale impasse from agent output",
                pipeline_id=pipeline_id,
                role=role_enum.value,
                error=str(exc),
            )
            continue
        logger.info(
            "Cleared stale impasse from agent output",
            pipeline_id=pipeline_id,
            role=role_enum.value,
            cleanup_reason=cleanup_reason,
        )


def _pipeline_superseded_by_restart(store, pipeline_id: str, run_epoch: datetime | None) -> bool:
    """True if a newer ``run_epoch`` means another thread now owns this pipeline.

    Reloads pipeline state and compares its ``run_epoch`` against the epoch the
    caller runs under (#3315 facet a). Best-effort: a missing epoch or a load
    failure returns ``False`` so a transient store hiccup never tears down a
    legitimately-running phase. Shared by the ``_run_concurrent_phase`` poll
    loop and the slice-path impasse-retry wrapper so the "no escalation when
    superseded" property holds on both routes.
    """
    if store is None or run_epoch is None:
        return False
    try:
        _epoch_pip = store.load_pipeline(pipeline_id)
    except Exception as _epoch_err:  # noqa: BLE001 — never wedge the caller
        logger.debug(
            "Epoch supersession check failed; continuing",
            pipeline_id=pipeline_id,
            error=str(_epoch_err),
        )
        return False
    current_epoch = _epoch_pip.run_epoch or _epoch_pip.created_at
    return current_epoch != run_epoch


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
    operator_directives: list[OperatorDirective] | None = None,
    iteration_history: list[IterationSummary] | None = None,
    run_epoch: datetime | None = None,
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
        from orchestrator.impasse_routing import (
            ImpasseAction,
            collect_impasses,
            route_impasses,
        )
    except ImportError:
        from impasse_routing import (  # type: ignore[no-redef]
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

    # Producer roles only — impasses are a producer concept; reviewers
    # don't author tasks. Mirrors the producer trio in
    # ``shared/egg_restrictions/patterns.py``.
    producer_roles = [
        ContractAgentRoleEnum.CODER,
        ContractAgentRoleEnum.TESTER,
        ContractAgentRoleEnum.DOCUMENTER,
    ]

    last_exit = 0
    last_logs = ""
    for attempt in range(MAX_IMPASSE_ATTEMPTS):
        is_terminal = attempt + 1 == MAX_IMPASSE_ATTEMPTS

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
            operator_directives=operator_directives,
            iteration_history=iteration_history,
            run_epoch=run_epoch,
        )

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

        # Defense-in-depth (#3315 facet a, slice path): if a restart bumped
        # ``run_epoch`` while this thread was running, a stale producer-written
        # impasse file could otherwise drive ``route_impasses`` into a HITL
        # against the freshly-restarted phase. The poll loop in
        # ``_run_concurrent_phase`` already bails on supersession before any
        # escalation; mirror that here so the "no escalation when superseded"
        # property holds on the slice path too — return the (superseded) result
        # without routing.
        if _pipeline_superseded_by_restart(store, pipeline_id, run_epoch):
            logger.info(
                "Restart superseded this thread before impasse routing; "
                "skipping route_impasses to avoid escalating against a "
                "freshly-restarted phase",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
            )
            return last_exit, last_logs

        try:
            # On the terminal iteration we have no remaining BRC cycle
            # to respawn with a new role, so a delegation made here
            # would silently dangle (review feedback #2 on PR #2553).
            # Force every impasse onto the escalate path instead.
            decisions = route_impasses(
                repo_path=Path(worktree_repo_path),
                pipeline_id=pipeline_id,
                contract_identifier=pipeline_id,
                impasses=impasses,
                slice_id=slice_id,
                force_escalate=is_terminal,
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

        # Drop the now-routed impasse signals before the next BRC
        # cycle, so a producer that crashes pre-handoff in iter-N+1
        # cannot resurrect this iteration's impasse via a stale file.
        _clear_stale_impasses_for_producers(
            Path(worktree_repo_path),
            pipeline_id,
            producer_roles,
            cleanup_reason="post-delegation cleanup",
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
    operator_directives: list[OperatorDirective] | None = None,
    iteration_history: list[IterationSummary] | None = None,
    run_epoch: datetime | None = None,
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

    # Scope the per-slice team to the slice's repo (#3393 task-6-1).
    #
    # Every slice maps to exactly one repo (slice ↔ repo, 1:1). For a
    # multi-repo pipeline the slice's work, worktree, test gate, reviewer
    # diff and PR all live in ITS repo — not necessarily the pipeline
    # primary. We resolve the slice's repo via ``resolve_slice_repo`` and
    # thread the slice-scoped repo / worktree / base-branch into the agent
    # prompts (which drive ``get_repo_checks`` for the tester's configured
    # checks, the file-boundary patterns, and the reviewer's
    # ``git diff origin/<base>...HEAD``) and the spawn (via ``base_branch``
    # → ``EGG_BASE_BRANCH`` and a slice-primary-first ``repos`` ordering so
    # the spawner sets the agent cwd / ``EGG_REPO_PATH`` to the slice's
    # repo worktree).
    #
    # N=1 stays byte-identical: a single-repo pipeline has one RepoSpec, so
    # the block below is skipped entirely (``len(pipeline.repos) <= 1``),
    # leaving ``slice_repo == pipeline.repo``, ``worktree_repo_path``, and
    # the pipeline base branch exactly as before — no extra contract read.
    slice_repo = pipeline.repo
    slice_repo_path = worktree_repo_path
    slice_repos = repos
    slice_base_branch: str | None = None
    if slice_id and len(getattr(pipeline, "repos", None) or []) > 1:
        from egg_contracts.loader import load_contract

        slice_obj = None
        try:
            _contract = load_contract(pipeline_id, worktree_repo_path)
            slice_obj = next((s for s in _contract.slices if s.id == slice_id), None)
        except Exception as contract_err:  # noqa: BLE001
            # Best-effort: a contract load/parse failure degrades to the
            # pipeline-primary repo (today's behaviour), it does not block
            # the spawn. The slice still runs, just against the primary.
            logger.warning(
                "Slice-repo scoping: contract load failed; using pipeline primary repo (#3393)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(contract_err),
            )

        # Single gate-repo accessor (shared with the tester's task-6-2
        # TestSliceGateRepoAccessor): the repo the whole slice team scopes to.
        resolved = _resolve_slice_gate_repo(slice_obj, pipeline) if slice_obj else None
        if resolved and resolved != pipeline.repo:
            slice_repo = resolved
            slice_repo_path = _resolve_slice_worktree_path(pipeline, resolved, worktree_repo_path)
            # Per-repo base branch from the pipeline's RepoSpec list.
            for spec in pipeline.repos or []:
                if getattr(spec, "repo", None) == resolved:
                    slice_base_branch = getattr(spec, "base_branch", None)
                    break
            # Order the slice's repo first so the spawner treats it as the
            # effective repo for this per-slice team (cwd / EGG_REPO_PATH).
            # ``repo_volumes`` already carries every repo owner/repo-keyed
            # (slice-3), so only the ordering changes here.
            slice_repos = [resolved, *[r for r in repos if r != resolved]]
            logger.info(
                "Slice scoped to secondary repo (#3393 task-6-1)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                slice_repo=slice_repo,
                slice_worktree=str(slice_repo_path),
            )

    # Resolve base branch for diff commands in agent prompts. Prefer the
    # slice repo's own base (its RepoSpec.base_branch) over the pipeline
    # singleton, then fall back to auto-detecting the default branch in the
    # slice's worktree (#3393 task-6-1). For N=1 this is the pipeline base /
    # pipeline worktree exactly as before.
    _resolved_base_branch = slice_base_branch or pipeline.base_branch
    if not _resolved_base_branch:
        try:
            _resolved_base_branch = get_default_branch(slice_repo_path)
        except Exception:
            _resolved_base_branch = None

    # A producer with no work in this slice is no longer pre-seeded (#3027
    # retired the #2581 pre-seed). It stays spawned and, if it finds it has
    # nothing to contribute, submits a generic no-op propose
    # (``no_changes_needed=true``) — the prompts below tell every producer
    # about that path. The consensus protocol accepts the no-op durably, so
    # no orchestrator-side roster pre-classification is needed.
    agent_prompts: dict[AgentRole, str] = {}
    for role in roles:
        prompt = _build_agent_prompt(
            role_value=role.value,
            phase=phase_str,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            prompt=pipeline.prompt,
            issue_number=pipeline.issue_number,
            # Slice-scoped repo / worktree (#3393 task-6-1): drives the
            # tester's ``get_repo_checks`` (per-repo configured checks),
            # the role file-boundary patterns, and the reviewer diff base —
            # all resolve from the slice's repo, not the pipeline primary.
            # N=1 ⇒ these equal ``pipeline.repo`` / ``worktree_repo_path``.
            repo=slice_repo,
            branch=pipeline.branch,
            base_branch=_resolved_base_branch,
            repo_path=str(slice_repo_path),
            concurrent=True,
            review_feedback=review_feedback,
            network_mode=gateway_mode,
            operator_directives=operator_directives,
            iteration_history=iteration_history,
        )
        agent_prompts[role] = prompt

    # Create spawn function and executor.
    spawn_fn = spawner.create_concurrent_spawn_fn(
        pipeline_id=pipeline_id,
        issue_number=pipeline.issue_number,
        repo_volumes=repo_volumes,
        mode=gateway_mode,
        # Slice's repo first (#3393 task-6-1): the spawner derives the agent
        # cwd / EGG_REPO_PATH from the primary (first) repo, so ordering the
        # slice's repo first sets the working directory to that repo's
        # worktree. N=1 / primary-repo slices leave ``repos`` unchanged.
        repos=slice_repos,
        phase=phase_str,
        sandbox_env=sandbox_env,
        certs_volume=certs_volume,
        # Pass the *resolved* base branch (above) rather than the raw
        # ``pipeline.base_branch`` so a ``None`` (auto-detect) base still
        # reaches the spawner as a concrete branch name. The spawner exports
        # it as ``EGG_BASE_BRANCH`` for the BRC event-pump's per-producer
        # ``git log --not origin/<base>`` delta (#2967); without a concrete
        # value the wrapper + composer fall back to ``origin/main`` and the
        # delta errors out on every non-``main`` repo. Worktree creation is
        # unaffected: the gateway resolves the same default branch when handed
        # ``None``, so resolving one layer up here is equivalent.
        base_branch=_resolved_base_branch,
        spawn_max_retries=pipeline.config.spawn_max_retries,
        spawn_retry_initial_backoff_seconds=pipeline.config.spawn_retry_initial_backoff_seconds,
        slice_id=slice_id,
    )

    max_concurrent = getattr(pipeline.config, "max_concurrent_agents", 6)
    # #3064 slice-3: in orchestrator-ownership mode the event loop watches
    # one-shot Job termination to drive failure supervision (backoff /
    # respawn / OVERSEER_ALERT). Hand it a Job-status observer when the
    # spawner can provide one (the kubernetes spawner); spawners without it
    # leave supervision observation dormant (pod mode is unaffected either way).
    event_status_view = None
    _make_status_view = getattr(spawner, "create_event_job_status_view", None)
    if callable(_make_status_view):
        event_status_view = _make_status_view()
    executor = ConcurrentPhaseExecutor(
        pipeline=pipeline,
        spawn_fn=spawn_fn,
        max_concurrent=max_concurrent,
        review_graph=filtered_graph,
        roles=roles,
        slice_id=slice_id,
        event_status_view=event_status_view,
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
                        # Carry the per-agent resolved model through the
                        # reconstruction (#3174). ``_spawn_agent`` stamps this on
                        # the in-memory execution, but the persisted record is
                        # rebuilt from scratch here — without this copy the field
                        # dead-ends at None and both operator confirmation
                        # channels (get_status, list_containers), which read from
                        # persisted state, surface ``resolved_model: null`` for
                        # every concurrent-phase agent (initial spawn and
                        # restart_phase respawn alike).
                        resolved_model=exec_info.resolved_model,
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

    # ``run_epoch`` is the authoritative epoch the owning ``_run_pipeline``
    # thread captured at start (#1638). The poll loop uses it to detect a
    # ``restart_phase`` (or any restart that bumps ``run_epoch``) that
    # superseded this thread (#3315). ``start_time`` is a fresh monotonic
    # clock per call, but a parked-then-restarted phase leaves the *old*
    # ``_run_concurrent_phase`` thread alive in its poll loop with a
    # ``start_time`` from the original phase start; once its ``elapsed``
    # crosses ``consensus_timeout`` it would fire a spurious consensus-timeout
    # OVERSEER_ALERT + HITL decision against the freshly-restarted phase. The
    # new ``_run_pipeline`` thread owns the pipeline now, so this stale thread
    # must bail before escalating. When ``run_epoch`` is not supplied (legacy
    # / direct-call callers) the guard is dormant — behaviour is unchanged.

    def _superseded_by_restart() -> bool:
        """True if a newer run_epoch means another thread owns this pipeline.

        Reloads pipeline state and compares its ``run_epoch`` against the
        epoch this thread runs under. Mirrors the post-return epoch check
        (#1638) but runs *inside* the poll loop so a superseded thread stops
        polling before it can fire stale escalations. Best-effort: a load
        failure returns ``False`` so a transient store hiccup never tears
        down a legitimately-running phase.
        """
        return _pipeline_superseded_by_restart(store, pipeline_id, run_epoch)

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

                # Auto-withdraw any stale consensus-timeout HITL a superseded
                # thread opened before this phase converged (#3315 facet c).
                # Folded into this already-locked load→save so it costs no
                # extra lock and rides every consensus-success path.
                _withdrawn = _cancel_consensus_timeout_decisions(pip)
                if _withdrawn:
                    logger.info(
                        "Auto-withdrew stale consensus-timeout HITL decision(s) on convergence",
                        pipeline_id=pipeline_id,
                        phase=phase_str,
                        withdrawn=_withdrawn,
                    )

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

    # #3426 HITL-gate state: same log-once discipline for the
    # operator-gated suspension of the consensus timeout.
    _hitl_gate_deferring = False

    while True:
        elapsed = time.monotonic() - start_time

        # 0. Bail if a restart superseded this thread (#3315). A parked phase
        #    that is restarted after the consensus-timeout budget elapsed
        #    leaves this old thread polling with a stale ``start_time``; the
        #    new ``_run_pipeline`` thread already owns the pipeline. Exit
        #    cleanly — stop this executor's event loop so it stops requesting
        #    one-shot spawns — WITHOUT firing the timeout escalation. Return a
        #    NON-zero exit so the caller never mistakes this for success and
        #    advances the phase; the post-return epoch check (#1638) at the
        #    call site re-confirms the restart and exits the old thread without
        #    marking the phase FAILED.
        if _superseded_by_restart():
            logger.info(
                "Phase superseded by restart (run_epoch changed) — exiting stale "
                "_run_concurrent_phase thread without escalation",
                pipeline_id=pipeline_id,
                phase=phase,
                slice_id=slice_id,
            )
            executor.stop_event_loop()
            return 1, "Phase superseded by restart; stale monitor thread exited."

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
            # Orchestrator mode (#3064): tear down the BRC event loop now that
            # the slice has converged so it stops requesting one-shot spawns.
            # No-op in pod mode.
            executor.stop_event_loop()
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
                    # Issue #2806 (Option A): a producer's consensus-wrapper
                    # exhausting its retry budget is unrecoverable — the
                    # slice state machine cannot replace a permanently dead
                    # producer, and the surviving reviewers will heartbeat
                    # forever waiting on a proposal that will never come.
                    # Detect this case and short-circuit the polling loop
                    # with a non-zero return so the caller transitions the
                    # pipeline (or slice) to FAILED. Reviewer-only deaths
                    # still flow through ``handle_agent_failure`` because
                    # peer-review redistribution can recover them.
                    role_value = exec_info.role.value
                    if filtered_graph.is_producer(role_value):
                        # Race window guard: a producer can legitimately
                        # exit non-zero after CONFIRMED (wrapper cleanup
                        # crash) — between step 1 (consensus check) and
                        # step 4 (exit detection) the producer could have
                        # written CONFIRMED and then died. Re-query
                        # consensus before hard-failing; if it has
                        # completed, fall through and let the next
                        # iteration's step 1/2 return success.
                        try:
                            recheck = executor.check_consensus()
                        except Exception as recheck_err:
                            logger.warning(
                                "Producer-death consensus recheck failed",
                                pipeline_id=pipeline_id,
                                role=role_value,
                                error=str(recheck_err),
                            )
                            recheck = {"is_complete": False}
                        if recheck.get("is_complete"):
                            logger.info(
                                "Producer container exited non-zero but consensus already complete — skipping hard-fail",
                                pipeline_id=pipeline_id,
                                role=role_value,
                                exit_code=info.exit_code,
                            )
                            # Consensus completed in the race window before
                            # the producer's wrapper-cleanup crash. Step 5
                            # (or the next iteration's step 1/2) will return
                            # success; skip handle_agent_failure (reviewer
                            # recovery path, not applicable to producers).
                            continue
                        _emit_producer_death_alert(
                            pipeline_id=pipeline_id,
                            role=role_value,
                            phase=phase_str,
                            slice_id=slice_id,
                            exit_code=info.exit_code,
                        )
                        logger.error(
                            "Producer agent died permanently — failing phase",
                            pipeline_id=pipeline_id,
                            phase=phase_str,
                            slice_id=slice_id,
                            role=role_value,
                            exit_code=info.exit_code,
                        )
                        _stop_running_containers()
                        combined_logs = "\n".join(
                            all_logs
                            + [
                                "--- PRODUCER PERMANENT DEATH ---",
                                (
                                    f"Producer '{role_value}' container exited with code "
                                    f"{info.exit_code} after the consensus-wrapper exhausted "
                                    f"its retry budget. Pipeline failing (issue #2806)."
                                ),
                            ]
                        )
                        return 1, combined_logs
                    try:
                        executor.handle_agent_failure(
                            role=role_value,
                            error=f"Container exited with code {info.exit_code}",
                        )
                    except Exception as e:
                        logger.warning(
                            "handle_agent_failure error",
                            role=role_value,
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

        # 5. All containers exited — fall back to exit-code-based result.
        #
        # Guarded on a non-empty ``active_executions`` so an empty set is
        # never misread as "everything exited" (``0 >= 0``).  In orchestrator
        # mode (#3064) ``spawn_all`` returns ``[]`` by design — the
        # orchestrator owns the BRC loop and spawns one-shot pods per event,
        # so there are no up-front containers to track.  Completion is driven
        # purely off ``check_consensus()`` (step 2) and the consensus timeout
        # (step 6); a zero-container fallback here would otherwise fail the
        # phase on the first poll, before any event-driven pod ran.
        if active_executions and len(exited_containers) >= len(active_executions):
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
                        # Tag with the consensus-timeout context so "Retry
                        # phase" dispatches through restart_phase on resolve
                        # (#3421), for symmetry with the incomplete-consensus
                        # sites below.  This question is hand-built and does not
                        # promise restart copy, but restart_phase is the correct
                        # "Retry phase" action regardless.  Like its siblings
                        # this pod-mode path is unreachable today (spawn_all
                        # returns [] post-#3164, so active_executions is always
                        # empty); tagging keeps the dispatch honest if pod mode
                        # is ever revived.
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
                            context=_CONSENSUS_TIMEOUT_HITL_CONTEXT,
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
                # Tag with the consensus-timeout context so "Retry phase"
                # dispatches through restart_phase on resolve (#3421), matching
                # the restart semantics `_incomplete_consensus_decision_text`
                # promises.  This pod-mode container-exit path is unreachable
                # today (spawn_all returns [] post-#3164, so active_executions
                # is always empty), but tagging keeps the copy honest if pod
                # mode is ever revived.
                _persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=question,
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                    context=_CONSENSUS_TIMEOUT_HITL_CONTEXT,
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
                # Same as the unresolved-NACK site above: tag with the
                # consensus-timeout context so "Retry phase" dispatches through
                # restart_phase (#3421) for symmetry.  Hand-built question, no
                # restart copy, but restart_phase is the right action here too.
                # Dead pod-mode path today; tagging is cheap insurance.
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
                    context=_CONSENSUS_TIMEOUT_HITL_CONTEXT,
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
                # Same as the container-failure path above: tag with the
                # consensus-timeout context so "Retry phase" dispatches through
                # restart_phase (#3421) and honors the restart copy.  Also a
                # dead pod-mode path today; tagging is cheap insurance.
                _persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=question,
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                    context=_CONSENSUS_TIMEOUT_HITL_CONTEXT,
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
            # #3426 HITL gate: while an unresolved operator HITL decision
            # (contract ``cq-N``) gates the running phase, the slice is
            # provably operator-gated — a reviewer withholding its ACK
            # pending a human ruling is the system working as designed, not
            # a convergence failure. Suspend the timeout (keep polling, no
            # alert, no failure) until the operator answers. On release,
            # reset the convergence clock so the agents folding in the
            # resolution get a full fresh window instead of a clock that
            # already expired while the human was thinking.
            _hitl_ids = _unresolved_contract_hitl_ids(pipeline_id, pipeline, phase_str)
            if _hitl_ids:
                if not _hitl_gate_deferring:
                    logger.info(
                        "Consensus timeout suspended — phase is operator-gated "
                        "on unresolved HITL decision(s)",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        elapsed_seconds=round(elapsed, 1),
                        decision_ids=_hitl_ids,
                    )
                    _hitl_gate_deferring = True
                time.sleep(poll_interval)
                continue
            if _hitl_gate_deferring:
                _hitl_gate_deferring = False
                start_time = time.monotonic()
                logger.info(
                    "Consensus timeout clock reset — operator HITL decision(s) resolved",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    suspended_after_seconds=round(elapsed, 1),
                )
                continue

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
            # Orchestrator mode (#3064): we are giving up on convergence, so
            # stop the BRC event loop before the fallback wait so it does not
            # keep spawning one-shot pods past the deadline.  No-op in pod
            # mode.  (The progress-gate ``continue`` above is taken before
            # this point, so a deferral never reaches here and the loop keeps
            # running across the deferral window.)
            executor.stop_event_loop()
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
                        # Tag with the consensus-timeout context so "Retry
                        # phase" dispatches through restart_phase on resolve
                        # (#3421), for symmetry with the incomplete-consensus
                        # sites above.  This question is hand-built and does not
                        # promise restart copy, but restart_phase is the correct
                        # "Retry phase" action regardless.  Like its siblings
                        # this pod-mode path is unreachable today: has_failures[0]
                        # is only set in _record_container_exit, called solely for
                        # active_executions / remaining members, which are always
                        # empty in orchestrator mode (spawn_all returns []
                        # post-#3164).  Tagging keeps the dispatch honest if pod
                        # mode is ever revived.
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
                            context=_CONSENSUS_TIMEOUT_HITL_CONTEXT,
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

            # Orchestrator-owned event loop: this timeout fallthrough is the
            # dominant non-convergence terminal.  spawn_all returns [] by
            # design, so step 5's "all containers exited" path is guarded off
            # (it requires a non-empty active set) and a slice that never
            # converged — producer never proposed, a reviewer pod failed to
            # ACK, reviews pending with no NACK — lands here with no NACKs.
            # Unlike pod mode, where a clean all-exited phase already routed
            # through step 5's is_complete check, nothing upstream has verified
            # consensus completeness on this path.  Mirror step 5: when the
            # orchestrator owns the loop and consensus is incomplete, escalate
            # an HITL and fail rather than reporting a non-converged slice as
            # success (a bare `return 0` here would advance the phase toward PR
            # creation past the BRC consensus gate).
            if executor.owns_event_loop() and not _final_consensus.get("is_complete"):
                question, log_suffix = _incomplete_consensus_decision_text(
                    _final_consensus, container_failure_count=0, orchestrator_mode=True
                )
                logger.warning(
                    "Consensus timed out and is incomplete (orchestrator-owned loop) — escalating to HITL",
                    pipeline_id=pipeline_id,
                    blocking_agents=_final_consensus.get("blocking_agents", []),
                )
                _persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=question,
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                    context=_CONSENSUS_TIMEOUT_HITL_CONTEXT,
                )
                combined_logs += log_suffix
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

    try:
        from agent_model_resolution import DEFAULT_AGENT_MODEL
    except ImportError:
        from ..agent_model_resolution import (  # type: ignore[import-not-found, no-redef]
            DEFAULT_AGENT_MODEL,
        )

    retry_kwargs: dict = {}
    if spawn_max_retries is not None:
        retry_kwargs["spawn_max_retries"] = spawn_max_retries
    if spawn_retry_initial_backoff_seconds is not None:
        retry_kwargs["spawn_retry_initial_backoff_seconds"] = spawn_retry_initial_backoff_seconds

    # NOTE: this helper only supports the default Anthropic auth path. It
    # does not forward ``upstream``/``upstream_model``, so ``spawn_agent_job``
    # falls back to the Anthropic branch and injects the session-token
    # placeholder into ``CLAUDE_CODE_OAUTH_TOKEN`` (#2817). It has no
    # production callers today (only test references). If this path is ever
    # revived for a LiteLLM agent, plumb ``upstream``/``upstream_model``
    # through here — otherwise Claude Code would send ``x-api-key`` (api_key
    # auth) while the placeholder lands in the OAuth header, leaving the
    # credential header empty and the session unresolvable.
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
                # This helper hard-codes the default Anthropic auth path (see
                # the NOTE above ``spawn_agent_job``), so the resolved model is
                # always the built-in default alias. Stamp it for parity with
                # ``_run_concurrent_phase`` / ``restart_agent`` (#3174) — if this
                # test-only path is ever resurrected for production it will not
                # silently regress resolved-model visibility.
                agent_execution = AgentExecution(
                    role=agent_role,
                    status=AgentExecutionStatus.RUNNING,
                    container_id=spawned.container_info.container_id,
                    slice_id=None,
                    started_at=datetime.now(UTC),
                    resolved_model=DEFAULT_AGENT_MODEL,
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


# Recovery options offered by the dedicated empty-contract HITL emitted
# from the slice-gate, start_phase=implement safety net, and plan-complete
# paths.  Plain "Retry phase" would respawn into the same empty-contract
# state (#2627 incident); these options map each choice to a concrete
# operator action that actually changes state.
_EMPTY_CONTRACT_HITL_OPTIONS = [
    "Repopulate contract from plan draft and retry",
    "Restart plan phase",
    "Abort pipeline",
]


# Per-reason divergence prose used by :func:`_empty_contract_hitl_question`
# when no parsed-slice count is available.  The generic fallback wording
# ("draft is missing, unparseable, or yielded no tasks") was written when
# only ``EMPTY_RESULT`` / ``PARSE_FAILED`` / ``DRAFT_MISSING`` / ``NO_DRAFT_PATH``
# routed through this HITL.  The widened
# :func:`_populate_result_is_empty_contract` check now also routes
# ``FOREST_VIOLATION`` / ``CONTRACT_LOAD_FAILED`` /
# ``EGG_CONTRACTS_UNAVAILABLE`` / ``UNEXPECTED_EXCEPTION`` plus the
# orthogonal ``populated_but_empty_slices`` case through here, where
# the operator would otherwise read a contradictory message: the
# prose says "draft missing/unparseable/yielded no tasks" while
# ``reason=forest_violation`` says the draft parsed fine but the slice
# DAG was rejected (#2627 review).  Reasons NOT in this dict
# (``empty_result``, ``parse_failed``, ``draft_missing``,
# ``no_draft_path``, ``plan_draft_missing_on_local``,
# ``plan_draft_missing_on_local_and_origin``) fall through to the
# generic line, which describes them accurately.
_DIVERGENCE_LINE_BY_REASON: dict[str, str] = {
    "forest_violation": (
        "contract.slices is empty because the plan slice DAG was rejected as not a forest"
    ),
    "slice_overlap_violation": (
        "contract.slices is empty because the plan slice DAG was rejected: two or more "
        "slices touch overlapping files with no dependency ordering between them (#3046)"
    ),
    "contract_load_failed": (
        "contract.slices is empty because the parsed contract on disk failed to deserialize"
    ),
    "egg_contracts_unavailable": (
        "contract.slices is empty because the egg-contracts library could "
        "not be imported during populate"
    ),
    "unexpected_exception": (
        "contract.slices is empty because the populator raised an unexpected exception"
    ),
    "populated_but_empty_slices": (
        "contract.slices is empty because the populator ran but produced 0 slices/tasks"
    ),
}


# NOTE: _FOREST_REASON_TO_OUTCOME (the ForestValidationError.reason ->
# PopulateOutcome table) moved to _populate.py alongside the PopulateOutcome
# enum it references at definition time (#3312 slice-4); it re-exports through
# the barrel, so _pkg._FOREST_REASON_TO_OUTCOME still resolves.


# Recovery options offered by the dedicated plan-preflight HITL emitted
# from the ``start_phase=implement`` safety net (#3100).  Plain "Retry
# phase" would respawn into the same metadata-less state; each option
# maps to a concrete operator action that actually changes state.
_PLAN_PREFLIGHT_HITL_OPTIONS = [
    "Fix the plan draft's pr: block and restart implement",
    "Restart plan phase",
    "Abort pipeline",
]


# Decision-ledger backstop options (#3390). Bare labels matched
# case-insensitively on the resolution, mirroring the phase_gate's
# keyword handling.
_LEDGER_BACKSTOP_RERUN_OPTION = "Re-run phase to register decisions"
_LEDGER_BACKSTOP_PROCEED_OPTION = "Proceed without a decision ledger"

# Explicit-none attestation confirmation option (#3462). Paired with
# ``_LEDGER_BACKSTOP_RERUN_OPTION`` on the confirmation decision; only a
# resolution that IS a confirmation (the bare keyword or the full label)
# proceeds — any other text is treated as a re-run directive, mirroring
# the phase_gate's "bare approve advances, notes request changes" posture.
_LEDGER_ATTESTATION_CONFIRM_OPTION = "Confirm — no open decisions this phase"


# ---------------------------------------------------------------------------
# Jira-epic SDLC scheduling helpers (issue #1557 — task-1-4 / task-2-7)
# ---------------------------------------------------------------------------


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

        def _make_overseer_teardown_hook(
            *,
            reason: str,
            container_id: str | None,
            phase: PipelinePhase,
        ) -> Callable[[], None]:
            """Build a pre_event_hook that tears down the per-phase overseer.

            ``container_id`` and ``phase`` are snapshotted as function
            parameters (frozen per-call), so the returned closure binds
            the loop-iteration values that were current when the
            post-phase cleanup branch fired — late binding would race a
            subsequent loop iteration.  ``reason`` differs between the
            doubly-failed and hard-reset-recovered call sites and is
            forwarded to :func:`_teardown_phase_overseer`.

            #2797 follow-up: collapses the two duplicated closure
            definitions at the two post-phase hard-reset emission sites
            into one shared factory.  The closure remains inside
            ``_run_pipeline`` because the ``phase_overseer_active``
            bool is a local nonlocal of this function.
            """

            def _hook() -> None:
                nonlocal phase_overseer_active
                with overseer_lock:
                    if container_id and phase_overseer_active:
                        phase_overseer_active = False
                        _teardown_phase_overseer(
                            spawner,
                            container_id,
                            pipeline_id,
                            phase_label=str(phase),
                            reason=reason,
                        )

            return _hook

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
                    # Gateway returns worktrees keyed by the full ``owner/repo``
                    # slug (#3393 slice-3, operator ruling #6). The on-disk
                    # worktree directory (and the container mount target) is
                    # still the bare repo name at /home/egg/repos/<name>, so
                    # the path reconstruction below strips the owner prefix
                    # from each key.
                    repo_volumes = wt_result.worktrees

                    # Derive the orchestrator-accessible worktree path.
                    # Reviewer containers write verdict/draft/check files into
                    # the worktree, so the orchestrator must read from there.
                    # Match against pipeline.repo (full owner/repo slug, which
                    # is now the map key) explicitly to avoid picking the wrong
                    # repo in multi-repo pipelines.
                    matched = False
                    if pipeline.repo and pipeline.repo in wt_result.worktrees:
                        repo_short = pipeline.repo.split("/")[-1]
                        candidate = WORKTREE_BASE_DIR / worktree_id / repo_short
                        if candidate.exists():
                            worktree_repo_path = candidate
                            matched = True
                    if not matched:
                        # Fallback: take the first existing worktree path.
                        # Keys are ``owner/repo``; the on-disk dir is the bare
                        # leaf, so strip the owner prefix before joining.
                        for owner_repo in wt_result.worktrees:
                            candidate = WORKTREE_BASE_DIR / worktree_id / owner_repo.split("/")[-1]
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

            # #2979: sync the worktree, pausing for a manual reconcile if
            # it diverges and the rebase autoresolve can't reconcile it.
            # The helper blocks (AWAITING_HUMAN) on a reconcile HITL and
            # resumes the phase start once the operator acks — nothing is
            # discarded and the pipeline is never failed for a recoverable
            # divergence.
            phase_start_sync_outcome, phase_start_sync_aborted = (
                _sync_worktree_reconciling_divergence(
                    spawner,
                    pipeline_id,
                    store,
                    repo_path,
                    worktree_repo_path=worktree_repo_path,
                    phase=current_phase,
                    gateway_mode=gateway_mode,
                    base_branch=pipeline.base_branch,
                    pipeline_branch=pipeline.branch,
                    prior_phase_succeeded=prior_phase_succeeded,
                )
            )
            if phase_start_sync_aborted:
                # Operator aborted the manual reconcile (or the pause
                # budget was exhausted).  Fail the pipeline; the local
                # commits remain pinned under the backup ref for offline
                # recovery — nothing was discarded.
                _fail_pipeline_after_divergence_abort(
                    pipeline_id,
                    store,
                    phase=current_phase,
                    backup_ref=phase_start_sync_outcome.backup_ref,
                    local_only_commit_shas=phase_start_sync_outcome.local_only_commit_shas,
                )
                return

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
                from egg_contracts.loader import compose_task_description, create_contract

                # Every entry path (GitHub issue, JIRA, free-text) anchors
                # the task the same way (#3163): identity first, then the
                # operator's submit description. Before #3163 issue
                # pipelines deliberately got ``None`` here (#3042 "agents
                # fetch the live body"), which left the #3123 binding
                # prompt section empty for the most common pipeline type.
                issue_url = (
                    f"https://github.com/{pipeline.repo}/issues/{pipeline.issue_number}"
                    if pipeline.issue_number is not None
                    else None
                )
                task_description = compose_task_description(
                    description=pipeline.prompt,
                    issue_number=pipeline.issue_number,
                    issue_url=issue_url,
                    jira_ticket=pipeline.jira_ticket,
                )

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
                            task_description=task_description,
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
                        create_contract(
                            issue_number=pipeline.issue_number,
                            title=f"Issue #{pipeline.issue_number}",
                            url=issue_url or "",
                            pipeline_id=pipeline.id,
                            repo_root=worktree_repo_path,
                            task_description=task_description,
                        )
                    else:
                        # ``pipeline.issue_number is None`` covers both
                        # free-text submits and JIRA-driven pipelines
                        # (``pipeline.jira_ticket`` set). The event-pump
                        # never delivers the orchestrator-built spawn
                        # prompt to the agent, so the contract (read via
                        # ``egg-contract show`` + the #3123 prompt
                        # section) is the reliable channel for the
                        # complete task; the ``title`` arg is only used
                        # for the ``IssueInfo`` label and is dropped
                        # without an ``issue_number``, so it is not a
                        # substitute (#3033).
                        create_contract(
                            pipeline_id=pipeline.id,
                            title=(pipeline.prompt or "")[:100],
                            task_description=task_description,
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
                            _inline_plan_populate_result = _populate_contract_from_plan(
                                worktree_repo_path,
                                pipeline_id,
                                pipeline_mode,
                                pipeline.issue_number,
                            )
                            # #2627 follow-up: warn-and-continue on non-POPULATED.
                            # This is the initial-contract creation path (a
                            # pre-generated plan handed to ``start_pipeline``);
                            # failing here would block legitimate pipelines that
                            # recover via the natural plan-phase populator a few
                            # blocks later.  We only attach the structured
                            # outcome as audit signal.
                            if _inline_plan_populate_result.outcome != PopulateOutcome.POPULATED:
                                logger.warning(
                                    "Pre-generated plan populate produced non-POPULATED outcome",
                                    pipeline_id=pipeline_id,
                                    outcome=_inline_plan_populate_result.outcome.value,
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
                # Catch ``ForestValidationError`` here so a malformed
                # plan landing at the safety-net path lands on the
                # dedicated empty-contract HITL — the same recovery
                # surface the natural plan-complete path uses via
                # :func:`_populate_contract_from_plan_safe`'s
                # forest-violation translation.  Without this catch the
                # safety net (which calls the inner directly so the
                # ``PlanDraftMissing*`` raises don't fire here) would
                # propagate the exception to the outer pipeline
                # ``except`` and the operator would see a generic
                # ``status: failed`` instead of the actionable
                # repopulate/restart-plan/abort decision (#2627 review).
                try:
                    _safety_net_populate_result = _populate_contract_from_plan(
                        worktree_repo_path,
                        pipeline_id,
                        pipeline_mode,
                        pipeline.issue_number,
                        current_phase=pipeline.current_phase,
                    )
                except ForestValidationError as forest_err:
                    # #3046 — overlap violations map to their own outcome so
                    # the empty-contract HITL prose matches the discriminator.
                    logger.warning(
                        "contract_phases_ingest_failed",
                        pipeline_id=pipeline_id,
                        reason=forest_err.reason,
                        source="safety_net",
                        errors=forest_err.errors,
                    )
                    _safety_net_populate_result = PopulateResult(
                        _forest_error_to_outcome(forest_err)
                    )
                # #2627 follow-up: fail-fast whenever the safety-net populate
                # did not produce a contract with tasks.  Without this guard
                # the implement phase spawns into the same empty-contract
                # state that #2627 surfaced — the slice-gate at
                # implement-phase entry would eventually catch it, but at
                # that point the pipeline has already advanced and the
                # operator sees the empty-contract divergence after the
                # loop is running.  Catching it here is earlier and cheaper.
                #
                # Routes through :func:`_populate_result_is_empty_contract`
                # so the two empty-contract call sites (this safety net
                # and the natural plan-complete handler below) can't drift
                # out of agreement.  See that helper's docstring for the
                # full discriminator rules.
                if _populate_result_is_empty_contract(_safety_net_populate_result):
                    # Reason dispatch shared with the plan-complete handler
                    # via :func:`_populate_outcome_to_hitl_reason` so the
                    # POPULATED → "populated_but_empty_slices" translation
                    # (and any future special-cased outcome) can't drift
                    # between the two call sites (#2627 review follow-up).
                    _safety_net_reason = _populate_outcome_to_hitl_reason(
                        _safety_net_populate_result.outcome
                    )
                    if _safety_net_populate_result.outcome == PopulateOutcome.POPULATED:
                        _safety_net_error = (
                            "start_phase=implement safety-net populate "
                            "completed but produced 0 slices/tasks — refusing "
                            "to spawn implement-phase agents on an empty "
                            "contract (#2627)"
                        )
                    else:
                        _safety_net_error = (
                            f"start_phase=implement safety-net populate produced "
                            f"{_safety_net_populate_result.outcome.value} outcome — "
                            f"refusing to spawn implement-phase agents on an "
                            f"empty contract (#2627)"
                        )
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        pipeline.status = PipelineStatus.FAILED
                        pipeline.error = _safety_net_error
                        store.save_pipeline(pipeline)
                    # Emit the dedicated empty-contract HITL inline so the
                    # operator sees an actionable decision instead of a
                    # generic ``status: failed`` with no recovery path
                    # other than ``restart_phase implement`` (which would
                    # respawn into the same empty-contract state).
                    _emit_empty_contract_hitl(
                        pipeline_id,
                        pipeline,
                        store,
                        reason=_safety_net_reason,
                        draft_slice_count=None,
                        gate="start_phase_implement_safety_net",
                        phase=pipeline.current_phase,
                    )
                    logger.error(
                        "OVERSEER_ALERT start_phase_implement_safety_net_empty_contract",
                        pipeline_id=pipeline_id,
                        outcome=_safety_net_populate_result.outcome.value,
                        slice_count=_safety_net_populate_result.slice_count,
                        reason=_safety_net_reason,
                    )
                    report_pipeline_status(
                        pipeline,
                        event_type="pipeline.failed",
                        message=f"Pipeline failed: {_safety_net_error[:100]}",
                    )
                    _emit_pipeline_event(pipeline, "pipeline.failed")
                    return

                # #3100: the natural plan→implement path enforces the
                # #2777 plan pre-flight (``validate_plan_preflight``) at
                # the advance_phase site; implement-start submits skip
                # that site entirely, so a plan draft without a ``pr:``
                # block previously entered the implement phase and every
                # context-PR opener backstop soft-failed with
                # ``missing_pr_metadata`` forever.  Enforce the same
                # validator here — after the empty-contract gate so the
                # #2627 HITL routing above is unchanged.
                if _enforce_implement_start_plan_preflight(
                    pipeline_id,
                    pipeline,
                    store,
                    worktree_repo_path,
                    plan_draft_rel,
                ):
                    return

        # Operator directives + prior iteration history are persisted on
        # ``PhaseExecution`` and accumulate across HITL kickbacks (#2795).
        # They are read directly off the phase below each loop iteration —
        # no separate "read once and clear" stash is needed.

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

            # SHAs we've already raised a branch-divergence alert for
            # (#2224 PR 3).  Per-pipeline dedupe so we fire once per
            # offending commit, not once per 30s tick.
            divergence_alerted_shas: set[str] = set()

            def _health_monitor_poll(monitor, stop_event: threading.Event, interval: float = 30.0):
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

                    # NOTE (#2270 slice-5): the standing-pod overseer respawn loop
                    # was removed here. The overseer is no longer a respawned
                    # standing pod — orchestrator-side detection (slice-4
                    # ``health_checks.detection_plane``) runs in-process and the
                    # only agent spawned is the on-demand adjudicator. Any
                    # surviving restart need is served by the general
                    # agent-restart machinery (``restart_agent``), not a bespoke
                    # overseer respawn. This also means a multi-hour zero-agent
                    # HITL park spawns nothing from this loop (§3).

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

                # #2777 (cq-4, TASK-1-2) — implement-phase entry
                # backstop. Calls the new
                # ``_open_context_pr_at_implement_start`` opener for
                # the runner-driven paths that bypass
                # ``advance_phase`` REST (inline ``_run_pipeline``
                # auto-advance and the HITL-approval recovery in
                # ``start_pipeline`` both leave
                # ``phase_execution.status`` as PENDING and spawn the
                # runner directly; the backstop catches both per
                # #2593). The opener is idempotent so re-firing here
                # after a successful advance_phase call is a one-
                # round-trip ``gh pr list`` no-op.
                #
                # reviewer_code_holistic blocker 1 fix: v1 deleted
                # this site under the (incorrect) "single canonical
                # site" plan AC; the four soft-fail call sites are in
                # fact the only context-PR opener calls on the
                # runner-driven paths, so the deletion silently
                # stranded slice stacks on ``egg/<id>/work``.
                # Restored under the new idempotent opener.
                if current_phase == PipelinePhase.IMPLEMENT:
                    try:
                        _open_context_pr_at_implement_start(pipeline_id, repo_path=repo_path)
                    except ContextPrCreationError as ctx_err:
                        logger.warning(
                            "Context PR opener: implement-entry backstop "
                            "failed (continuing — hard-require enforced at "
                            "advance_phase and the implement-start plan "
                            "pre-flight gate) (#2777, #3100)",
                            pipeline_id=pipeline_id,
                            reason=ctx_err.reason,
                            error=str(ctx_err),
                        )
                    except Exception as backstop_err:  # noqa: BLE001
                        logger.warning(
                            "Context PR opener: implement-entry backstop "
                            "outer wrapper raised (continuing) (#2777)",
                            pipeline_id=pipeline_id,
                            error=str(backstop_err),
                        )

            # Spawn overseer container for this phase's health monitoring.
            # The overseer is phase-scoped: spawned at phase start and torn
            # down at phase completion/advance/failure.  Each phase gets a
            # fresh overseer instance with no accumulated state.
            #
            # #2270 slice-5: gate overseer presence on "agents actually
            # running". During a zero-agent HITL park the pipeline has no phase
            # agents in flight, so spawning an overseer there is pure churn
            # (§3). The respawn loop that used to keep it alive across such
            # parks was removed; this gate stops the phase-start spawn from
            # doing the same thing. The agent count is the deterministic phase
            # roster the concurrent executor itself consults — the cohort this
            # phase is about to run.
            _phase_agent_count = _count_phase_agents(pipeline, current_phase)
            if pipeline.config.overseer_enabled and _overseer_should_be_present(
                running_agent_count=_phase_agent_count,
                pipeline_status=pipeline.status,
            ):
                try:
                    overseer_result = _spawn_overseer_agent(
                        spawner=spawner,
                        pipeline_id=pipeline_id,
                        issue_number=pipeline.issue_number,
                        gateway_mode=gateway_mode,
                        pipeline_repos=pipeline_repos if pipeline_repos else None,
                        max_turns=pipeline.config.overseer_max_turns,
                        decision_model=pipeline.config.overseer_decision_maker_model,
                    )
                    with overseer_lock:
                        overseer_container_id = overseer_result.container_info.container_id
                        phase_overseer_active = True
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

            # Jira-epic SDLC support (issue #1557). Export ``EGG_IS_EPIC``
            # (bool-string) and ``EGG_EPIC_MODE`` (one of
            # 'epic-fresh', 'epic-reassess', 'ticket', 'github_issue')
            # so the refiner / task-planner / applier prompts can select
            # the right mode block. Mapping is derived via
            # ``prompt_loader.derive_pipeline_mode`` so the orchestrator
            # and any auxiliary callers agree on the canonical rule.
            #
            # Note: ``EGG_PIPELINE_MODE`` is already taken (PipelineMode:
            # 'issue' — set above at L19349).
            # ``EGG_EPIC_MODE`` is the orthogonal Jira-epic dimension.
            try:
                from prompt_loader import derive_pipeline_mode
            except ImportError:  # pragma: no cover - defensive
                derive_pipeline_mode = None  # type: ignore[assignment]
            _is_epic_flag = bool(getattr(pipeline, "is_epic", False))
            _pipeline_mode_attr = getattr(pipeline, "pipeline_mode", None)
            sandbox_env["EGG_IS_EPIC"] = "true" if _is_epic_flag else "false"
            if derive_pipeline_mode is not None:
                sandbox_env["EGG_EPIC_MODE"] = derive_pipeline_mode(
                    is_epic=_is_epic_flag,
                    pipeline_mode=_pipeline_mode_attr,
                    jira_ticket=jira_ticket_value or None,
                )
            else:
                sandbox_env["EGG_EPIC_MODE"] = "github_issue" if not jira_ticket_value else "ticket"

            # Issue #1557 reviewer_code v1 finding #4: run the reassess
            # sweep before the planner / applier spawn on reassess-mode
            # epic pipelines so the task-planner prompt's ``[mode: epic-
            # reassess]`` branch and the applier's in-flight refusal
            # have the children classification on disk.  The sweep
            # writes two JSON files under ``.egg-state/agent-outputs/``;
            # we export both paths into the sandbox env so the prompts
            # read them by env var rather than re-querying the gateway.
            # Fail-open: a sweep failure logs a warning but never aborts
            # the phase — the planner falls back to fresh-mode treatment
            # of the children (which is safe because every action carries
            # an explicit ``jira_action`` and the applier's in-flight
            # refusal hinges on the sweep file's presence).
            if (
                _is_epic_flag
                and _pipeline_mode_attr == "reassess"
                and current_phase.value in ("plan", "apply")
                and jira_ticket_value
            ):
                try:
                    from jira_reassess import (
                        run_reassess_sweep,
                        serialise_sweep_to_disk,
                    )
                except ImportError:  # pragma: no cover - defensive
                    run_reassess_sweep = None  # type: ignore[assignment]
                    serialise_sweep_to_disk = None  # type: ignore[assignment]
                if run_reassess_sweep is not None and serialise_sweep_to_disk is not None:
                    try:
                        sweep_result = run_reassess_sweep(
                            epic_key=jira_ticket_value,
                            state_store=store,
                        )
                        agent_outputs_dir = (
                            Path(worktree_repo_path) / ".egg-state" / "agent-outputs"
                        )
                        sweep_path, done_path = serialise_sweep_to_disk(
                            result=sweep_result,
                            agent_outputs_dir=agent_outputs_dir,
                            pipeline_id=pipeline_id,
                        )
                        sandbox_env["EGG_REASSESS_SWEEP_PATH"] = str(sweep_path)
                        sandbox_env["EGG_DONE_CHILDREN_PATH"] = str(done_path)
                        logger.info(
                            "Reassess sweep complete",
                            pipeline_id=pipeline_id,
                            epic_key=jira_ticket_value,
                            child_count=len(sweep_result.children),
                            done_count=len(sweep_result.done),
                            warnings=sweep_result.warnings,
                        )
                    except Exception as sweep_err:  # noqa: BLE001 — fail-open
                        logger.warning(
                            "Reassess sweep failed (continuing without sweep handoff)",
                            pipeline_id=pipeline_id,
                            epic_key=jira_ticket_value,
                            error=str(sweep_err),
                        )

            phase_failed = False
            tester_gap_summary: str | None = None

            # --- Inner review cycle ---
            # NOTE: the legacy PR phase (and its auto-PR / slice-DAG-skip
            # branches) was deleted in #2777 (cq-4 / TASK-2-2). The context
            # PR now opens up-front via ``_open_context_pr_at_implement_start``
            # at the plan→implement boundary, slice PRs stack on it, and
            # IMPLEMENT is the terminal phase — no per-phase auto-PR creation
            # logic is reachable here for ``current_phase.value == "pr"``.
            if True:
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

                    # Read structured operator directives + prior iteration
                    # history off the phase so iteration N+1 prompts can render
                    # them with precedence prose (#2795). These lists accumulate
                    # across kickbacks and are never cleared, so no read-and-
                    # clear stash is needed.
                    _phase_operator_directives: list[OperatorDirective] = []
                    _phase_iteration_history: list[IterationSummary] = []
                    try:
                        with get_pipeline_state_lock(pipeline_id):
                            _fb_pipeline = store.load_pipeline(pipeline_id)
                            _fb_phase = _fb_pipeline.get_phase_execution(current_phase)
                            _phase_operator_directives = list(_fb_phase.operator_directives)
                            _phase_iteration_history = list(_fb_phase.iteration_history)
                    except Exception as e:
                        logger.debug("Failed to read operator directives for phase", error=str(e))

                    # #2137: route the implement phase through the slice
                    # DAG iterator when the contract has more than one
                    # slice. Single-slice and no-slice contracts continue
                    # to use the legacy monolithic path so existing
                    # pipelines are unaffected.
                    _use_slice_loop = False
                    _slice_gate_failure: SliceGateMonolithicBlock | None = None
                    if current_phase.value == "implement":
                        try:
                            from egg_contracts.loader import (
                                load_contract as _load_contract_for_slice_check,
                            )

                            _check_contract = _load_contract_for_slice_check(
                                pipeline_id, worktree_repo_path
                            )
                            _slice_count = len(getattr(_check_contract, "slices", []) or [])
                            # #2777 cq-10 — route through ``_is_slice_dag_mode``
                            # so the "what counts as slice-DAG" definition has
                            # a single source of truth. Local ``_slice_count``
                            # is still used by the defensive recheck below for
                            # the structured log when the populator dropped
                            # slices (#2337).
                            _use_slice_loop = _is_slice_dag_mode(_check_contract)

                            # #2915: Auto-populate contract if empty at implement start
                            # This fills the gap where start_phase=implement doesn't trigger
                            # the plan-completion populate path, leaving agents with nothing to do.
                            if _slice_count == 0:
                                _slice_count = _auto_populate_contract_at_implement_start(
                                    worktree_repo_path,
                                    pipeline_id,
                                    pipeline_mode,
                                    pipeline.issue_number,
                                    pipeline.current_phase,
                                    pipeline.branch,
                                    gateway=spawner.gateway,
                                    gateway_mode=gateway_mode,
                                    base_branch=pipeline.base_branch,
                                )
                                if _slice_count > 0:
                                    # Reload contract after successful populate
                                    _check_contract = _load_contract_for_slice_check(
                                        pipeline_id, worktree_repo_path
                                    )
                                    _use_slice_loop = _is_slice_dag_mode(_check_contract)

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
                        _slice_gate_msg = _slice_gate_failure.message
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            if phase_execution.cycle_timings:
                                phase_execution.cycle_timings[-1].completed_at = datetime.now(UTC)
                            phase_execution.status = PipelineStatus.FAILED
                            phase_execution.error = _slice_gate_msg
                            phase_execution.completed_at = datetime.now(UTC)
                            pipeline.status = PipelineStatus.FAILED
                            pipeline.error = _slice_gate_msg
                            store.save_pipeline(pipeline)
                        # #2627 follow-up: emit a dedicated HITL naming the
                        # empty-contract root cause inline.  The generic
                        # post-failure Retry/Accept/Abort decision respawns
                        # implement into the same empty-contract state; this
                        # HITL's options map to repopulate / restart-plan /
                        # abort so the operator has a recovery path that
                        # actually changes state.
                        _emit_empty_contract_hitl(
                            pipeline_id,
                            pipeline,
                            store,
                            reason="slice_gate_blocked_monolithic_demotion",
                            draft_slice_count=_slice_gate_failure.draft_slice_count,
                            gate="slice_gate",
                            phase=current_phase,
                        )
                        logger.error(
                            "OVERSEER_ALERT slice_gate_blocked_monolithic_demotion",
                            pipeline_id=pipeline_id,
                            error=_slice_gate_msg,
                            draft_slice_count=_slice_gate_failure.draft_slice_count,
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
                                run_epoch=run_epoch,
                            )
                        else:
                            # Pre-#2137 monolithic-implement fallback. The
                            # impasse-retry wrapper deliberately wraps only
                            # the slice-loop call site (#2529): impasse
                            # delegation rewires a *task* between producer
                            # roles, which only makes sense per-slice.
                            # Pipelines that don't use the slice loop are
                            # legacy / single-PR-shape, so an impasse here
                            # surfaces as a normal slice failure and the
                            # operator handles it via the existing
                            # phase-failure HITL path.
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
                                operator_directives=_phase_operator_directives,
                                iteration_history=_phase_iteration_history,
                                run_epoch=run_epoch,
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
            # and _sync_pipeline_decisions_to_contract so the autoresolve
            # rebase inside _sync_worktree_with_remote lands the remote
            # state before the populate step reads ``.egg-state/`` —
            # otherwise populate would read a stale local view and either
            # produce an empty contract or overwrite agent-pushed drafts
            # that only exist on origin.  (Before #2979 the helper also
            # issued ``git reset --hard`` on a doubly-failed divergence,
            # which would have reverted local on-disk modifications; that
            # destructive path is gone, so the modern rationale is purely
            # about the autoresolve rebase, not a hard reset.)
            post_phase_sync_outcome: WorktreeSyncOutcome | None = None
            post_phase_sync_aborted = False
            if pipeline.branch and worktree_repo_path != repo_path:
                # Best-effort for transient failures: a sync failure must
                # not strand the auto-advance.  Without this guard, a
                # gateway HTTP error or git subprocess failure inside the
                # helper propagates to the outer Exception handler and (if
                # marking FAILED also fails) leaves the pipeline wedged with
                # phase COMPLETE but no successor (#2219).
                #
                # #2979: on an unreconciled divergence the helper pauses
                # (AWAITING_HUMAN) on a reconcile HITL and blocks until the
                # operator acks, then re-runs the sync — nothing is
                # discarded and the pipeline is NOT failed for a recoverable
                # post-consensus sync.  Only an operator abort (or an
                # exhausted reconcile budget) returns aborted=True.
                try:
                    post_phase_sync_outcome, post_phase_sync_aborted = (
                        _sync_worktree_reconciling_divergence(
                            spawner,
                            pipeline_id,
                            store,
                            repo_path,
                            worktree_repo_path=worktree_repo_path,
                            phase=current_phase,
                            gateway_mode=gateway_mode,
                            base_branch=pipeline.base_branch,
                            pipeline_branch=pipeline.branch,
                        )
                    )
                except Exception as sync_err:
                    logger.warning(
                        "Failed to sync worktree with remote after phase (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(sync_err),
                    )

            # #2979: operator aborted the manual reconcile (or the pause
            # budget was exhausted).  Fail the pipeline; nothing was
            # discarded — the local commits remain pinned under the backup
            # ref for offline recovery.  ``pre_event_hook`` tears down the
            # per-phase overseer under its own lock before the public
            # ``pipeline.failed`` event, matching the prior ordering.
            if post_phase_sync_aborted and post_phase_sync_outcome is not None:
                _fail_pipeline_after_divergence_abort(
                    pipeline_id,
                    store,
                    phase=current_phase,
                    backup_ref=post_phase_sync_outcome.backup_ref,
                    local_only_commit_shas=post_phase_sync_outcome.local_only_commit_shas,
                    pre_event_hook=_make_overseer_teardown_hook(
                        reason="worktree divergence reconcile aborted",
                        container_id=overseer_container_id,
                        phase=current_phase,
                    ),
                )
                break

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
            # ``source="plan_complete"`` makes the wrapper raise:
            #   * PlanDraftMissingOnLocalError — draft missing on local
            #     but present on origin (#2337 silent demotion).
            #   * PlanDraftMissingOnLocalAndOriginError — draft missing on BOTH local
            #     and origin (#2627 silent advance to empty contract).
            # We catch either below and mark the pipeline FAILED so the
            # operator can intervene rather than implement silently
            # shipping slice-1 alone (#2337) or strand 8 agents on an
            # empty contract (#2627).
            if current_phase.value == "plan":
                try:
                    _plan_complete_populate_result = _populate_contract_from_plan_safe(
                        worktree_repo_path,
                        pipeline_id,
                        pipeline_mode,
                        pipeline.issue_number,
                        source="plan_complete",
                        branch=pipeline.branch,
                    )
                    # #2627 follow-up: populate-succeeded-but-empty is the
                    # orthogonal failure mode flagged in the issue.  The
                    # draft existed (so neither PlanDraftMissing variant
                    # fired) but the populator did not produce a contract
                    # with tasks the implement-phase agents can act on.
                    # Synthesize a raise so the same FAILED-cleanup
                    # handler below runs.
                    #
                    # Routes through
                    # :func:`_populate_result_is_empty_contract` so the two
                    # empty-contract call sites (this handler and the
                    # ``start_phase=implement`` safety net) can't drift out
                    # of agreement.  This widens the original
                    # ``EMPTY_RESULT`` / ``PARSE_FAILED`` check to cover
                    # every non-success outcome plus the POPULATED-with-no-
                    # slices case (#2627 review).
                    if _populate_result_is_empty_contract(_plan_complete_populate_result):
                        # Pre-raise OVERSEER_ALERT mirroring the two
                        # ``PlanDraftMissing*`` wrapper-side emits at
                        # :func:`_populate_contract_from_plan_safe` so the
                        # discriminator the FAILED-cleanup logger uses
                        # (``OVERSEER_ALERT plan_populate_produced_empty_contract``)
                        # is also emitted before the raise.  Without this
                        # the third fail-loud branch had no pre-raise log
                        # while the two draft-missing branches did,
                        # asymmetric audit (#2627 review).
                        logger.error(
                            "OVERSEER_ALERT plan_populate_produced_empty_contract",
                            pipeline_id=pipeline_id,
                            branch=pipeline.branch,
                            outcome=_plan_complete_populate_result.outcome.value,
                            slice_count=_plan_complete_populate_result.slice_count,
                            note=(
                                "plan populate did not produce a contract with "
                                "tasks the implement-phase agents can act on; "
                                "blocking phase advance (#2627)"
                            ),
                        )
                        raise PopulateProducedEmptyContractError(
                            _plan_complete_populate_result.outcome,
                            slice_count=_plan_complete_populate_result.slice_count,
                        )
                except (
                    PlanDraftMissingOnLocalError,
                    PlanDraftMissingOnLocalAndOriginError,
                    PopulateProducedEmptyContractError,
                ) as missing_err:
                    # Mirror the slice-gate failure handler at the
                    # implement-phase entry: mark FAILED in state,
                    # then run the same cleanup sequence as the
                    # ``if phase_failed:`` block above (teardown phase
                    # overseer, report pipeline status, best-effort push
                    # for backup) so both load-bearing failure paths
                    # have a uniform cleanup story.  Re #2337 / #2627
                    # reviews.
                    teardown_reason, log_event = _empty_contract_failure_metadata(missing_err)
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        phase_execution.status = PipelineStatus.FAILED
                        phase_execution.error = str(missing_err)
                        phase_execution.completed_at = datetime.now(UTC)
                        pipeline.status = PipelineStatus.FAILED
                        pipeline.error = str(missing_err)
                        store.save_pipeline(pipeline)
                    # #2627 follow-up: emit the dedicated empty-contract
                    # HITL so the operator sees an actionable decision
                    # (repopulate / restart-plan / abort) inline with the
                    # FAILED status, instead of having to dig through
                    # pipeline.error and the generic consensus-timeout
                    # decision.
                    _hitl_reason = _empty_contract_hitl_reason(missing_err)
                    _emit_empty_contract_hitl(
                        pipeline_id,
                        pipeline,
                        store,
                        reason=_hitl_reason,
                        draft_slice_count=None,
                        gate="plan_complete",
                        phase=current_phase,
                    )
                    logger.error(
                        log_event,
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
                                reason=teardown_reason,
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
            try:
                _write_brc_history(
                    worktree_repo_path,
                    pipeline_id,
                    current_phase.value,
                    _brc_history_identifier(pipeline),
                    # Per-slice implement-phase files are owned by each
                    # slice's integration branch; committing them onto
                    # ``work`` here would conflict with the slice
                    # branches' add of the same paths and break slice
                    # PR merges (#2755). The parameter is a no-op for
                    # non-implement phases.
                    write_per_slice=False,
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

            # --- Unresolved-gap gate (#3300) ---
            # Block finalize while the contract carries an unresolved
            # tester→coder TaskGap. Runs after the worktree sync above so
            # the contract reflects the agents' final writes, and BEFORE
            # the phase_gate / advance / finalize below so the gap can't
            # ship into the committed contract (which would fail
            # test_models_gaps.py red in CI on the already-open PR —
            # #3298 class 4). Scoped to IMPLEMENT, where gaps are written;
            # no-ops on a clean contract. On a fully-autonomous pipeline
            # (hitl_gates=False) the gate surfaces the escalation but does
            # not block — both options need a human, so blocking would
            # stall the pipeline indefinitely; the reactive CI check stays
            # the backstop there.
            if current_phase == PipelinePhase.IMPLEMENT:
                try:
                    gap_gated = _await_unresolved_gap_gate(
                        store,
                        pipeline_id,
                        repo_path,
                        worktree_repo_path,
                        _pipeline_identifier(pipeline.issue_number, pipeline_id),
                        current_phase,
                        pipeline.config.hitl_gates,
                    )
                    pipeline = store.load_pipeline(pipeline_id)
                    # The gate ran after the statefile commit+push above, so
                    # when it changed the contract (operator resolved a gap,
                    # or the override audit landed) the resolution is still
                    # uncommitted in the worktree. Re-commit + push so the
                    # work branch tree CI sees reflects the post-gate
                    # contract, not the open-gap snapshot pushed earlier.
                    if gap_gated:
                        gate_committed = False
                        try:
                            gate_committed = _commit_statefiles_to_worktree(
                                worktree_repo_path,
                                f"Persist contract after {current_phase.value} gap gate",
                                pipeline_identifier=_pipeline_identifier(
                                    pipeline.issue_number, pipeline_id
                                ),
                                pipeline_id=pipeline_id,
                            )
                        except Exception as git_err:
                            logger.warning(
                                "Failed to commit statefiles after gap gate (continuing)",
                                pipeline_id=pipeline_id,
                                phase=current_phase.value,
                                error=str(git_err),
                            )
                        # Skip the follow-up push when nothing was committed
                        # (e.g. the override path leaves the contract
                        # unchanged) — it would be a no-op fast-forward
                        # (#2548).
                        if gate_committed and pipeline.branch and worktree_repo_path != repo_path:
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
                                    "Failed to push statefiles after gap gate (continuing)",
                                    pipeline_id=pipeline_id,
                                    phase=current_phase.value,
                                    error=str(push_err),
                                )
                except Exception as gap_gate_err:  # noqa: BLE001
                    # Never let a gate bug strand the pipeline — the
                    # reactive test_models_gaps.py CI check remains the
                    # backstop if this fails open.
                    logger.warning(
                        "Unresolved-gap gate raised (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(gap_gate_err),
                    )

            # --- HITL gate: pause for human approval ---
            # Refine/plan are gated by the converge-before-advance loop
            # (#3392): it resolves decisions with a human each round, which is
            # what lets us drop the force-advance backstop — a human is present
            # to resolve and approve.
            #
            # But a fully-autonomous pipeline (``hitl_gates is False``) has no
            # human to resolve or approve, and ``wait_for_decision`` polls
            # indefinitely — so unconditionally gating here would convert that
            # explicitly-chosen, first-class config into an indefinite hang
            # with no operator-facing signal that the flag was ignored. Mirror
            # the unresolved-gap gate's autonomous escape (#3300): when
            # ``hitl_gates is False`` we *surface* the gate (event + loud
            # warning) but do not block, advancing autonomously instead.
            # ``hitl_gates`` therefore still governs refine/plan, but only by
            # toggling between the human-gated converge loop and an autonomous
            # advance — never an indefinite stall.
            if current_phase.value in _HITL_GATE_PHASES and not pipeline.config.hitl_gates:
                report_pipeline_status(
                    pipeline,
                    event_type="phase.gate_skipped",
                    message=(
                        f"{current_phase.value} phase gate skipped "
                        f"(hitl_gates=False) — advancing autonomously"
                    ),
                )
                logger.warning(
                    "HITL gate: refine/plan gate on an autonomous pipeline "
                    "(hitl_gates=False); surfacing but not blocking — advancing "
                    "without human approval (the converge-before-advance loop "
                    "requires a human, so it cannot run unattended)",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                )
                # Decision-ledger visibility on the autonomous path (#3390):
                # no human is present to resolve a backstop HITL, so mirror
                # the gate-skip posture — surface a missing ledger loudly
                # (event + warning) but never block.
                try:
                    _ledger_note, _ledger_missing, _ledger_explicit_none = (
                        _collect_decision_ledger_status(
                            worktree_repo_path,
                            pipeline_id,
                            _pipeline_identifier(pipeline.issue_number, pipeline_id),
                            current_phase,
                        )
                    )
                    if _ledger_missing:
                        logger.warning(
                            "Decision ledger missing at autonomous gate skip (#3390)",
                            pipeline_id=pipeline_id,
                            phase=current_phase.value,
                        )
                        report_pipeline_status(
                            pipeline,
                            event_type="phase.decision_ledger_missing",
                            message=(
                                f"{current_phase.value} phase advanced autonomously "
                                f"with no decision ledger — {_ledger_note}"
                            ),
                        )
                    elif _ledger_explicit_none is not None:
                        # No human is present to confirm the attestation
                        # (#3462) — mirror the gate-skip posture: surface
                        # loudly, never block.
                        report_pipeline_status(
                            pipeline,
                            event_type="phase.decision_ledger_explicit_none",
                            message=(
                                f"{current_phase.value} phase advanced autonomously "
                                f"on an unconfirmed no-decisions attestation — "
                                f"{_ledger_note}"
                            ),
                        )
                except Exception as ledger_err:  # noqa: BLE001
                    logger.warning(
                        "Decision-ledger check raised on autonomous path (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(ledger_err),
                    )
            elif current_phase.value in _HITL_GATE_PHASES:
                # --- Decision-ledger backstop (#3390) ---
                # Propose-time validation guarantees every refine/plan
                # producer attested its ledger, so reaching this gate with
                # zero registered decisions AND no explicit-none attestation
                # means a path bypassed consensus (force-advance, resume) or
                # the claim was lost. Never silently advance past that:
                # surface a dedicated HITL whose default remedy is a phase
                # re-run (the converge loop's standard corrective), with an
                # explicit operator override to proceed.
                _ledger_note = ""
                _ledger_missing = False
                _ledger_explicit_none: tuple[str, str] | None = None
                try:
                    _ledger_note, _ledger_missing, _ledger_explicit_none = (
                        _collect_decision_ledger_status(
                            worktree_repo_path,
                            pipeline_id,
                            _pipeline_identifier(pipeline.issue_number, pipeline_id),
                            current_phase,
                        )
                    )
                except Exception as ledger_err:  # noqa: BLE001
                    # Never let a helper bug strand the pipeline — the
                    # propose-time hard gate remains the primary enforcement.
                    logger.warning(
                        "Decision-ledger status check raised (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(ledger_err),
                    )

                if _ledger_missing:
                    dq = get_decision_queue(pipeline_id, repo_path)
                    _backstop = dq.queue_decision(
                        question=(
                            f"The {current_phase.value} phase reached its gate "
                            f"without a decision ledger (#3390). {_ledger_note}\n\n"
                            f"Re-running the phase lets its agents register the "
                            f"decisions the drafts should have surfaced (or attest "
                            f"an explicit empty ledger); proceeding accepts the "
                            f"unverified ledger and presents the normal phase gate."
                        ),
                        context=_ledger_note,
                        options=[
                            _LEDGER_BACKSTOP_RERUN_OPTION,
                            _LEDGER_BACKSTOP_PROCEED_OPTION,
                        ],
                        decision_type="choice",
                        phase=current_phase,
                    )
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        pipeline.status = PipelineStatus.AWAITING_HUMAN
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        phase_execution.status = PipelineStatus.AWAITING_HUMAN
                        store.save_pipeline(pipeline)
                    report_pipeline_status(
                        pipeline,
                        event_type="decision.created",
                        message=(
                            f"Decision ledger missing for {current_phase.value} "
                            f"phase — awaiting operator direction"
                        ),
                    )
                    _emit_pipeline_event(pipeline, "decision.created")

                    _backstop_resolved = dq.wait_for_decision(_backstop.id)
                    _backstop_resolution = str(
                        getattr(_backstop_resolved, "resolution", None) or ""
                    ).strip()
                    _proceed = (
                        _backstop_resolved.status != DecisionStatus.RESOLVED
                        or "proceed" in _backstop_resolution.lower()
                    )
                    if not _proceed:
                        # Default remedy: re-run the phase so producers can
                        # register (or explicitly attest) the ledger. Any
                        # free-text resolution rides along as the directive.
                        _rerun_directive = (
                            f"The {current_phase.value} phase reached its gate "
                            f"without a decision ledger: no HITL decisions were "
                            f"registered and no producer attested an explicit "
                            f"empty ledger (#3390). Review your draft for "
                            f"operator-grade choices; register each via "
                            f"`egg-contract add-decision` and cite its cq-N in "
                            f"the draft, or attest `no_decisions_rationale` when "
                            f"proposing if the phase genuinely raises none."
                        )
                        if _backstop_resolution.lower() != (_LEDGER_BACKSTOP_RERUN_OPTION.lower()):
                            _rerun_directive += f"\n\nOperator note: {_backstop_resolution}"
                        logger.info(
                            "Decision-ledger backstop: re-running phase (#3390)",
                            pipeline_id=pipeline_id,
                            phase=current_phase.value,
                        )
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            pipeline.status = PipelineStatus.RUNNING
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            phase_execution.status = PipelineStatus.RUNNING
                            phase_execution.completed_at = None
                            phase_execution.hitl_review_cycles += 1
                            _alert_threshold = pipeline.config.max_hitl_review_cycles
                            if phase_execution.hitl_review_cycles >= _alert_threshold:
                                _broadcast_hitl_nonconvergence_alert(
                                    pipeline_id,
                                    pipeline,
                                    current_phase,
                                    phase_execution.hitl_review_cycles,
                                    _alert_threshold,
                                )
                            _perform_hitl_phase_rerun(
                                store=store,
                                spawner=spawner,
                                pipeline=pipeline,
                                phase_execution=phase_execution,
                                pipeline_id=pipeline_id,
                                current_phase=current_phase,
                                feedback_text=_rerun_directive,
                                event_message=(
                                    f"Re-running {current_phase.value}: decision "
                                    f"ledger missing (#3390)"
                                ),
                            )
                        continue  # Re-enter outer loop → re-run phase
                    logger.warning(
                        "Decision-ledger backstop: operator chose to proceed "
                        "without a ledger (#3390)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        resolution=_backstop_resolution[:200],
                    )
                elif _ledger_explicit_none is not None:
                    # --- Explicit-none attestation confirmation (#3462) ---
                    # The producer's claim that this phase raises no operator
                    # decisions bypasses the entire register → bridge →
                    # resolve chain, and is itself a judgment call the HITL
                    # contract assigns to the operator. Surface it as its own
                    # confirmable decision (see the helper): confirming records
                    # the operator's endorsement on the ledger note; rejecting
                    # re-runs the phase to register cq-N entries.
                    _rerun_requested, _ledger_note, pipeline = (
                        _handle_explicit_none_attestation_gate(
                            pipeline=pipeline,
                            pipeline_id=pipeline_id,
                            repo_path=repo_path,
                            current_phase=current_phase,
                            ledger_note=_ledger_note,
                            explicit_none=_ledger_explicit_none,
                            store=store,
                            spawner=spawner,
                        )
                    )
                    if _rerun_requested:
                        continue  # Re-enter outer loop → re-run phase

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
                    # Auditability (#3390): make "N registered" vs "explicitly
                    # none" vs "MISSING (operator overrode)" readable at the
                    # gate without a get_contract round-trip.
                    if _ledger_note:
                        question += f"\n\n{_ledger_note}"

                    # Lead the gate comment with the simplifier's human-focused
                    # companion (simplified, jargon-free) when present, and link
                    # the full agent draft for depth. Falls back to the full
                    # draft inline when no companion exists (older pipelines,
                    # or the companion failed to land).
                    human_content = _read_human_phase_draft(
                        worktree_repo_path,
                        current_phase.value,
                        issue_number=pipeline.issue_number,
                        pipeline_id=pipeline_id,
                        branch=pipeline.branch,
                    )
                    gate_context = draft_content
                    if human_content:
                        full_draft_link = ""
                        draft_rel = _get_draft_path(
                            current_phase.value,
                            issue_number=pipeline.issue_number,
                            pipeline_id=pipeline_id,
                        )
                        if pipeline.repo and pipeline.branch and draft_rel:
                            blob = f"https://github.com/{pipeline.repo}/blob/{pipeline.branch}"
                            full_draft_link = (
                                f"\n\n[View the full detailed {phase_label} draft]"
                                f"({blob}/{draft_rel})"
                            )
                        gate_context = f"{human_content}{full_draft_link}"

                    # Detect whether the gate content changed compared to the
                    # previous phase_gate decision for this phase (if any).
                    #
                    # NB: this compares ``gate_context``, which leads with the
                    # simplifier's human-focused summary when a companion
                    # exists. That summary is intentionally high-level and
                    # lossy, so a re-refinement that materially changes the
                    # detailed agent draft *without* altering the summary will
                    # report ``content_changed=False``. The flag only feeds the
                    # overseer's no-op-rerun health heuristic
                    # (``overseer/monitor.py`` ``_check_rerun_anomaly``) — it
                    # never gates re-prompting — so a missed change here is at
                    # worst a suppressed advisory alert, not a correctness
                    # issue. We compare the gate content (not the full draft)
                    # deliberately so the heuristic tracks what the operator
                    # actually sees at the gate.
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
                        _content_changed = gate_context != _prev_gate.context

                    dq = get_decision_queue(pipeline_id, repo_path)
                    decision = dq.queue_decision(
                        question=question,
                        context=gate_context,
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

                # Holds the operator's resolution from the "bare request →
                # asked for specifics → approve-with-context" follow-up path,
                # if that path is taken. When set, it (not the original
                # ``resolution``) carries any context attached to the final
                # gate approval, so the convergence re-run below must thread it
                # rather than the stale original resolution (#3392 review).
                followup_resolution: str | None = None

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

                        # No force-advance (#3392). The converge-before-advance
                        # loop is human-gated every round, so an unbounded loop
                        # cannot burn compute silently and we must never advance
                        # with the operator's feedback unaddressed. After the
                        # configured number of rounds, emit a non-fatal overseer
                        # alert for visibility, then always re-run. The
                        # ``max_hitl_review_cycles`` config is now this alert
                        # threshold, not a force-advance budget.
                        _alert_threshold = pipeline.config.max_hitl_review_cycles
                        if phase_execution.hitl_review_cycles >= _alert_threshold:
                            _broadcast_hitl_nonconvergence_alert(
                                pipeline_id,
                                pipeline,
                                current_phase,
                                phase_execution.hitl_review_cycles,
                                _alert_threshold,
                            )
                        # #2795: the directive + frozen iteration summary
                        # accumulate across kickbacks so iteration N+1's prompts
                        # render them with explicit precedence prose.
                        _perform_hitl_phase_rerun(
                            store=store,
                            spawner=spawner,
                            pipeline=pipeline,
                            phase_execution=phase_execution,
                            pipeline_id=pipeline_id,
                            current_phase=current_phase,
                            feedback_text=_revision_feedback,
                            event_message=f"Human requested changes to {current_phase.value}",
                        )
                    continue  # Re-enter outer loop → re-run phase with feedback

                # Before advancing, surface any contract-scoped decisions /
                # feedback the phase's agents registered via ``egg-contract``.
                # Without this bridge, approving the phase_gate silently
                # discards them (#1889).  Wrapped in try/except so a bug
                # here can never strand the pipeline.
                _decisions_resolved_this_round = 0
                try:
                    _decisions_resolved_this_round = _queue_and_await_contract_decisions(
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

                # Converge-before-advance (#3392): if the operator just
                # resolved one or more decisions, re-run the phase so the
                # documents reflect those resolutions and any decision the
                # resolutions induce is surfaced in the next round. Re-asks of
                # already-answered questions are suppressed by carry-forward
                # (find_resolved_question), so the open-decision set shrinks
                # toward a fixpoint; we advance only on a round that resolved
                # nothing new. The phase gate is re-presented after the re-run.
                if _decisions_resolved_this_round and current_phase.value in _HITL_GATE_PHASES:
                    # Preserve any operator context attached to the approve so
                    # the re-run's agents see it (the bridge already persisted
                    # the decision answers themselves; this carries the gate
                    # prose that would otherwise be dropped on a re-run round).
                    # When the operator went through the "bare request → asked
                    # for specifics → approve-with-context" follow-up path, the
                    # context lives in ``followup_resolution`` (the final
                    # answer), not the stale original ``resolution`` — prefer
                    # it so that context is not silently dropped (#3392 review).
                    _context_source = (
                        followup_resolution if followup_resolution is not None else resolution
                    )
                    _approve_context = ""
                    try:
                        _ap = json.loads(_context_source)
                        if isinstance(_ap, dict):
                            _approve_context = (
                                _ap.get("context") or _ap.get("feedback") or ""
                            ).strip()
                    except json.JSONDecodeError, TypeError, AttributeError:
                        _approve_context = ""

                    _rerun_feedback = (
                        f"The operator resolved {_decisions_resolved_this_round} HITL "
                        f"decision(s) for the {current_phase.value} phase. Update the "
                        f"{current_phase.value} document(s) to reflect the resolved "
                        f"decisions (read them from the contract's `decisions`), and "
                        f"register any new decisions the resolutions induce."
                    )
                    if _approve_context:
                        _rerun_feedback += f"\n\nOperator note at the gate: {_approve_context}"

                    logger.info(
                        "HITL gate: decisions resolved, re-running phase to fold them in",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        resolved_count=_decisions_resolved_this_round,
                    )
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        pipeline.status = PipelineStatus.RUNNING
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        phase_execution.status = PipelineStatus.RUNNING
                        phase_execution.completed_at = None
                        phase_execution.hitl_review_cycles += 1
                        _alert_threshold = pipeline.config.max_hitl_review_cycles
                        if phase_execution.hitl_review_cycles >= _alert_threshold:
                            _broadcast_hitl_nonconvergence_alert(
                                pipeline_id,
                                pipeline,
                                current_phase,
                                phase_execution.hitl_review_cycles,
                                _alert_threshold,
                            )
                        _perform_hitl_phase_rerun(
                            store=store,
                            spawner=spawner,
                            pipeline=pipeline,
                            phase_execution=phase_execution,
                            pipeline_id=pipeline_id,
                            current_phase=current_phase,
                            feedback_text=_rerun_feedback,
                            event_message=(
                                f"Folding {_decisions_resolved_this_round} resolved "
                                f"decision(s) into {current_phase.value}"
                            ),
                        )
                    continue  # Re-enter outer loop → re-run phase, re-surface gate

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

            # ----------------------------------------------------------
            # #2777 (cq-4, TASK-1-2) — inline ``_run_pipeline``
            # auto-advance plan→implement transition. Calls the new
            # idempotent ``_open_context_pr_at_implement_start``
            # opener directly; auto-advance does NOT route through
            # ``routes/phases.py:advance_phase``, so without this call
            # site a natural plan-exit (no operator REST call) would
            # never get a context PR opened, leaving the slice stack
            # stranded on ``egg/<id>/work`` (the #2593 / #2769
            # symptom). reviewer_code_holistic blocker 1 fix:
            # restored after v1's incorrect "single canonical site"
            # deletion. The opener's ``gh pr list`` pre-flight makes
            # a redundant call from any other transition path a one-
            # round-trip no-op.
            # ----------------------------------------------------------
            if current_phase.value == "plan":
                try:
                    _open_context_pr_at_implement_start(pipeline_id, repo_path=repo_path)
                except ContextPrCreationError as ctx_err:
                    logger.warning(
                        "Context PR opener: _run_pipeline auto-advance "
                        "failed (continuing — hard-require enforced at "
                        "advance_phase and the implement-start plan "
                        "pre-flight gate) (#2777, #3100)",
                        pipeline_id=pipeline_id,
                        reason=ctx_err.reason,
                        error=str(ctx_err),
                    )
                except Exception as autoadvance_err:  # noqa: BLE001
                    logger.warning(
                        "Context PR opener: _run_pipeline auto-advance "
                        "outer wrapper raised (continuing) (#2777)",
                        pipeline_id=pipeline_id,
                        error=str(autoadvance_err),
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

            # Determine next phase.  Issue #1557: epic-mode pipelines
            # route through the new APPLY phase between PLAN and
            # IMPLEMENT so the APPLIER role can drive Jira mutations on
            # HITL approval.  ``_next_phases_for_epic`` returns
            # ``transitions.get(current_phase, [])`` unchanged for
            # non-epic pipelines so the pre-#1557 scheduling is
            # preserved bit-for-bit.
            next_phases = _next_phases_for_epic(
                pipeline,
                current_phase,
                transitions.get(current_phase, []),
            )

            if not next_phases:
                # Terminal phase — pipeline complete
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.COMPLETE
                    store.save_pipeline(pipeline)

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

            # Issue #1557: when the just-completed phase is PLAN and the
            # pipeline is_epic, we are advancing into APPLY.  Write the
            # applier handoff JSON now (before respawning the driver
            # thread) so the APPLIER container can read it on its
            # first wakeup.  ``approved_phase='plan'`` so the applier
            # drives plan-apply (Task.jira_action walk → child create /
            # edit / link, Won't-Do handoff for the orchestrator drain).
            if (
                getattr(pipeline, "is_epic", False)
                and current_phase == PipelinePhase.PLAN
                and next_phase == PipelinePhase.APPLY
            ):
                _write_apply_phase_handoff(
                    pipeline,
                    worktree_repo_path,
                    approved_phase="plan",
                )

            # Issue #1557 task-2-7: when the just-completed phase is
            # APPLY (BRC consensus confirmed), drain the Won't-Do
            # handoff JSON before advancing to IMPLEMENT.  The drain
            # runs out-of-band from the HITL approve POST so a slow
            # Jira API never extends that handler's latency.
            if current_phase == PipelinePhase.APPLY:
                _drain_wontdo_batch_after_apply(pipeline, worktree_repo_path)
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.current_phase = next_phase
                pipeline.run_epoch = datetime.now(UTC)
                # ``updated_at`` is unconditionally set by ``StateStore.save_pipeline``.
                store.save_pipeline(pipeline)

            # Drop the previous phase's in-memory consensus tracker and
            # message-store entries (#2502).  The other phase-transition
            # paths -- ``advance_phase`` REST handler, HITL-revision
            # re-run, and the ``recover_pipeline`` resume path -- all
            # call this; the auto-advance path used to skip it, leaving
            # a stale plan-phase tracker keyed under the bare
            # ``pipeline_id`` for ``_get_concurrent_status`` to find and
            # report as ``is_complete: True`` long after the implement
            # phase had started.  ``_write_brc_history`` runs at the
            # bottom of each phase iteration with
            # ``write_per_slice=False`` (see #2755), so per-slice
            # implement-phase transcripts are on the slice integration
            # branches, and the work commit picks up only the
            # unattributed sibling plus whatever aggregate the writer
            # still emits — refine/plan/pr aggregates, and the
            # non-slice-implement aggregate that any implement-phase
            # run without slice scope lands on work via the ``not
            # buckets`` branch — before we wipe the message store here.
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
            #
            # #2593 review issue 1 — initialised before the lock so the
            # post-lock deferred context-PR opener invocation has a
            # stable name to read regardless of which branch inside the
            # lock executes.
            _hitl_open_context_pr_after_lock: bool = False
            _hitl_pr_worktree_path: Path | None = None
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
                    # Issue #1557 — route epic pipelines through APPLY
                    # between PLAN and IMPLEMENT.  Non-epic pipelines
                    # see the default transition unchanged.
                    next_phases = _next_phases_for_epic(
                        pipeline,
                        current_phase,
                        transitions.get(current_phase, []),
                    )
                    # #2593 — populate contract from the plan draft when
                    # the HITL recovery is advancing the pipeline out
                    # of the plan phase.  Without this, contract.pr is
                    # empty (so the PR phase falls back to placeholder
                    # title/body and the context PR hook short-circuits
                    # on "contract has no pr block"), and the slice
                    # stack ends up rooted on ``/work`` with no PR to
                    # ``main`` — exactly the symptom reported on the
                    # in-flight #2474 pipeline.  Mirrors the plan-exit
                    # logic in ``advance_phase`` (routes/phases.py)
                    # and the auto-advance path in ``_run_pipeline``.
                    # Best-effort: failures warn and continue so a
                    # transient infra problem cannot strand the HITL
                    # recovery.  The actual context-PR open is
                    # deferred until after the lock is released
                    # (#2593 review issue 1) so the multi-second
                    # gateway sequence does not extend the
                    # per-pipeline state lock's hold time.
                    _next_phase_peek = next_phases[0] if next_phases else None
                    if (
                        current_phase == PipelinePhase.PLAN
                        and _next_phase_peek == PipelinePhase.IMPLEMENT
                    ):
                        _hitl_worktree_path = _resolve_pipeline_worktree_path(pipeline, repo_path)
                        try:
                            _pipeline_mode = pipeline.mode.value if pipeline.mode else "issue"
                            _hitl_populate_result = _populate_contract_from_plan_safe(
                                _hitl_worktree_path,
                                pipeline_id,
                                _pipeline_mode,
                                pipeline.issue_number,
                                source="hitl_plan_gate_approval",
                            )
                            # #1941: HITL plan-gate approval is a recovery
                            # hammer like force-advance — blocking it on a
                            # populate failure defeats the purpose.  We log
                            # the structured outcome but never raise.
                            if _hitl_populate_result.outcome != PopulateOutcome.POPULATED:
                                logger.warning(
                                    "HITL plan-gate approval populate produced non-POPULATED outcome",
                                    pipeline_id=pipeline_id,
                                    outcome=_hitl_populate_result.outcome.value,
                                )
                            try:
                                _commit_statefiles_to_worktree(
                                    _hitl_worktree_path,
                                    "Populate contract from plan on HITL plan-gate approval",
                                    pipeline_identifier=_pipeline_identifier(
                                        pipeline.issue_number, pipeline_id
                                    ),
                                    pipeline_id=pipeline_id,
                                )
                            except Exception as _hitl_commit_err:  # noqa: BLE001
                                logger.warning(
                                    "Failed to commit populated contract on HITL plan-gate approval (continuing) (#2593)",
                                    pipeline_id=pipeline_id,
                                    error=str(_hitl_commit_err),
                                )

                            # #2593 review issue 5 — the earlier
                            # ``push_worktree_branch`` at line ~20598
                            # ran *before* this populate commit, so
                            # the populated ``contract.pr`` only
                            # exists locally until the IMPLEMENT
                            # phase's next phase-boundary sync.  Push
                            # again now so any slice-agent container
                            # that materialises a fresh worktree from
                            # origin before that sync still sees
                            # ``contract.pr``.  Mirrors the
                            # auto-advance flow's pre-context-PR push
                            # in ``_run_pipeline``.
                            if pipeline.branch and _hitl_worktree_path != repo_path:
                                try:
                                    _get_spawner().gateway.push_worktree_branch(
                                        pipeline_id=pipeline_id,
                                        repo_path=str(_hitl_worktree_path),
                                        branch=pipeline.branch,
                                        mode=_gw_mode,
                                        base_branch=pipeline.base_branch,
                                    )
                                except Exception as _hitl_push_err:  # noqa: BLE001
                                    logger.warning(
                                        "Failed to push populated contract on HITL plan-gate approval (continuing) (#2593)",
                                        pipeline_id=pipeline_id,
                                        error=str(_hitl_push_err),
                                    )
                        except Exception as _hitl_pop_err:  # noqa: BLE001
                            logger.warning(
                                "Failed to run plan-exit populate on HITL recovery (continuing) (#2593)",
                                pipeline_id=pipeline_id,
                                error=str(_hitl_pop_err),
                            )

                        # Defer the context-PR open until after the
                        # per-pipeline state lock is released — see
                        # ``_open_context_pr_at_implement_start``'s
                        # idempotency docstring on why this multi-
                        # second network sequence (one ``gh pr list``
                        # + maybe one ``gh pr create``) must not run
                        # under the lock (#2593 review issue 1).
                        _hitl_open_context_pr_after_lock = True
                        _hitl_pr_worktree_path = _hitl_worktree_path

                    if not next_phases:
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

                    # Issue #1557: PLAN → APPLY transition on epic
                    # pipelines (mirrors auto-advance path).  Write the
                    # applier handoff JSON before the next _run_pipeline
                    # thread is respawned so the APPLIER container's
                    # first read finds it on disk.
                    if (
                        getattr(pipeline, "is_epic", False)
                        and current_phase == PipelinePhase.PLAN
                        and next_phase == PipelinePhase.APPLY
                    ):
                        _hitl_apply_worktree = _resolve_pipeline_worktree_path(pipeline, repo_path)
                        _write_apply_phase_handoff(
                            pipeline,
                            _hitl_apply_worktree,
                            approved_phase="plan",
                        )

                    # Issue #1557 task-2-7: when the resolved phase was
                    # APPLY (BRC consensus confirmed via HITL recovery
                    # path), drain the Won't-Do handoff before advancing.
                    if current_phase == PipelinePhase.APPLY:
                        _hitl_drain_worktree = _resolve_pipeline_worktree_path(pipeline, repo_path)
                        _drain_wontdo_batch_after_apply(pipeline, _hitl_drain_worktree)

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
                    # #2795: derive iteration_n monotonically. The
                    # ``max(len(iteration_history), max(directive_idx) + 1)``
                    # form does not depend on ``hitl_review_cycles``, so
                    # this expression is safe to evaluate either before
                    # or after ``_clear_concurrent_state`` resets the
                    # per-phase counter. What *is* order-sensitive is
                    # the tracker snapshot a few lines below: the BRC
                    # tracker is in-memory only and gets wiped by
                    # ``_clear_concurrent_state``, so the snapshot MUST
                    # happen first. On a crash-recovery resolution the
                    # snapshot will typically have empty verdict detail,
                    # but the iteration index + artifacts are still
                    # useful context for iteration N+1's prompts.
                    # The ``max(...) + 1`` floor ensures a legacy-
                    # hitl_feedback migration (which synthesises a
                    # directive but leaves iteration_history empty)
                    # doesn't restart the index at 0.
                    _recovery_iteration_n = max(
                        len(phase_execution.iteration_history),
                        max(
                            (d.iteration_n for d in phase_execution.operator_directives),
                            default=-1,
                        )
                        + 1,
                    )
                    _recovery_tracker = None
                    try:
                        from peer_consensus import (
                            get_peer_consensus_tracker as _gpct_recovery,
                        )

                        _recovery_tracker = _gpct_recovery(pipeline_id)
                    except Exception as tracker_err:  # noqa: BLE001
                        logger.debug(
                            "Tracker lookup failed during recovery snapshot",
                            pipeline_id=pipeline_id,
                            error=str(tracker_err),
                        )
                    _recovery_summary = _build_iteration_summary_from_tracker(
                        _recovery_tracker,
                        iteration_n=_recovery_iteration_n,
                        artifacts=phase_execution.artifacts,
                    )

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

                    # #2795: append the operator directive + iteration
                    # summary so iteration N+1 prompts can render them
                    # with precedence prose. Both lists accumulate
                    # across kickbacks (no clear).
                    if revision_feedback:
                        phase_execution.operator_directives.append(
                            OperatorDirective(
                                iteration_n=_recovery_iteration_n,
                                feedback_text=revision_feedback,
                            )
                        )
                        phase_execution.iteration_history.append(_recovery_summary)

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

            # #2593 review issue 1 — context-PR open moved out of the
            # per-pipeline state lock so the multi-second gateway
            # sequence does not hold the lock and block concurrent
            # ``advance_phase`` / status reads.
            #
            # #2777 (cq-4, TASK-1-2) — HITL-recovery context-PR site
            # calls the new idempotent
            # ``_open_context_pr_at_implement_start`` opener directly.
            # HITL recovery in ``start_pipeline`` does NOT route
            # through ``advance_phase`` REST (the runner thread is
            # spawned inline below), so without this call site an
            # operator-resumed pipeline would silently strand its
            # slice stack on ``egg/<id>/work``. The opener's
            # ``gh pr list`` pre-flight makes a redundant call from a
            # later ``advance_phase`` invocation a one-round-trip
            # no-op (reviewer_code_holistic blocker 1 fix; v1 deleted
            # this site under the incorrect "single canonical site"
            # plan AC).
            if _hitl_open_context_pr_after_lock and _hitl_pr_worktree_path is not None:
                try:
                    _open_context_pr_at_implement_start(pipeline_id, repo_path=repo_path)
                except ContextPrCreationError as ctx_err:
                    logger.warning(
                        "Context PR opener: HITL-resume failed "
                        "(continuing — hard-require enforced at "
                        "advance_phase and the implement-start plan "
                        "pre-flight gate) (#2777, #3100)",
                        pipeline_id=pipeline_id,
                        reason=ctx_err.reason,
                        error=str(ctx_err),
                    )
                except Exception as hitl_err:  # noqa: BLE001
                    logger.warning(
                        "Context PR opener: HITL-resume outer wrapper raised (continuing) (#2777)",
                        pipeline_id=pipeline_id,
                        error=str(hitl_err),
                    )

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


# Review-criteria builders live in _criteria.py (#3312 slice-4); re-exported
# here so `from routes.pipelines import X` and patch("routes.pipelines.X")
# keep resolving through the barrel.
# context_pr helpers live in _context_pr.py (#3312 slice-4); re-exported here.
# brc_history helpers live in _brc_history.py (#3312 slice-4); re-exported here.
# alerts helpers live in _alerts.py (#3312 slice-4); re-exported here.
from ._alerts import (  # noqa: E402,F401
    _branch_divergence_tick,
    _check_branch_divergence_for_alert,
    _check_brc_progress_gate,
    _emit_divergence_reconcile_hitl,
    _emit_empty_contract_hitl,
    _emit_producer_death_alert,
    _fail_pipeline_after_divergence_abort,
    _handle_brc_consensus_timeout,
    _latest_active_role_heartbeat,
    _publish_branch_divergence_alert,
    _publish_consensus_timeout_alert,
    _sync_worktree_reconciling_divergence,
    _unresolved_contract_hitl_ids,
    detect_branch_divergence,
)
from ._brc_history import (  # noqa: E402,F401
    BRC_HISTORY_TYPES,
    CONSENSUS_BRC_TYPES,
    _commit_slice_brc_history_to_integration_branch,
    _get_message_store,
    _persist_phase_brc_history,
    _render_brc_history_markdown,
    _rewrite_brc_history_for_pr,
    _write_brc_history,
    _write_brc_history_file,
)
from ._context_pr import (  # noqa: E402,F401
    _build_brc_history_link_line,
    _build_pre_merge_obligations_section,
    _collect_pre_merge_obligations,
    _compose_context_pr_body,
    _maybe_open_secondary_context_prs,
    _open_context_pr_at_implement_start,
    _open_secondary_context_prs,
    _persist_context_pr_number,
    _refresh_context_pr_body,
    _repos_with_slices,
)
from ._criteria import (  # noqa: E402,F401
    _get_agent_design_criteria,
    _get_code_review_criteria,
    _get_code_review_holistic_criteria,
    _get_concurrency_review_criteria,
    _get_contract_review_criteria,
    _get_first_principles_review_criteria,
    _get_plan_review_criteria,
    _get_refine_review_criteria,
    _get_review_criteria_for_type,
    _get_reviewer_scope_preamble,
    _get_security_review_criteria,
    _human_companion_review_criteria,
    _read_shared_criteria,
)
from ._decisions import (  # noqa: E402,F401
    _cancel_consensus_timeout_decisions,
    _divergence_reconcile_hitl_question,
    _divergence_reconcile_is_abort,
    _find_pending_divergence_reconcile_decision,
    _format_nack_summary,
    _incomplete_consensus_decision_text,
    _persist_hitl_decision,
)

# drafts helpers live in _drafts.py (#3312 slice-4); re-exported here.
from ._drafts import (  # noqa: E402,F401
    _HUMAN_SPEC_BY_PHASE,
    _cleanup_stale_generic_drafts,
    _draft_filename,
    _get_draft_path,
    _get_generic_draft_path,
    _get_human_draft_path,
    _git_show_draft,
    _pull_contract_from_source_branch,
    _read_human_phase_draft,
    _read_phase_draft,
    _read_source_branch_artifacts,
    _verdict_path_for_type,
)
from ._drivers import (  # noqa: E402,F401
    _broadcast_orphaned_driver_alert,
    _spawn_pipeline_run_thread,
    has_live_pipeline_driver,
    maybe_revive_orphaned_awaiting_human_driver,
    relaunch_driverless_running_pipelines,
)
from ._ledger import (  # noqa: E402,F401
    _await_unresolved_gap_gate,
    _collect_decision_ledger_status,
    _drain_wontdo_batch_after_apply,
    _find_explicit_none_attestation,
    _handle_explicit_none_attestation_gate,
    _ledger_attestation_confirmed,
    _ledger_attestation_question,
    _ledger_attestation_rerun_directive,
    _next_phases_for_epic,
    _persist_phase_gate_resolution,
    _queue_and_await_contract_decisions,
    _sync_pipeline_decisions_to_contract,
    _unwrap_choice_resolution,
    _write_apply_phase_handoff,
)
from ._overseer import (  # noqa: E402,F401
    _build_overseer_corrective_executor,
    _consume_adjudicator_verdict,
    _corrective_nudge_agent,
    _corrective_open_operator_hitl,
    _corrective_respawn_cohort,
    _count_phase_agents,
    _escalate_finding_to_adjudicator,
    _execute_overseer_verdicts,
    _overseer_should_be_present,
    _run_overseer_detection_plane,
    _send_brc_confirmation_nudge,
    _spawn_overseer_agent,
    _teardown_phase_overseer,
)
from ._pod_liveness import (  # noqa: E402,F401
    _count_live_pods_for_pipeline,
    _get_spawner,
    _guard_live_pods_or_force,
    _live_event_agents,
    _slice_agents_alive,
)
from ._populate import (  # noqa: E402,F401
    _FOREST_REASON_TO_OUTCOME,
    PlanDraftMissingOnLocalAndOriginError,
    PlanDraftMissingOnLocalError,
    PopulateOutcome,
    PopulateProducedEmptyContractError,
    PopulateResult,
    SliceGateMonolithicBlock,
    _auto_populate_contract_at_implement_start,
    _empty_contract_failure_metadata,
    _empty_contract_hitl_question,
    _empty_contract_hitl_reason,
    _enforce_implement_start_plan_preflight,
    _forest_error_to_outcome,
    _merge_preserved_slice_runtime,
    _origin_has_plan_draft,
    _plan_preflight_hitl_question,
    _populate_contract_from_plan,
    _populate_contract_from_plan_safe,
    _populate_outcome_to_hitl_reason,
    _populate_result_is_empty_contract,
    _slice_gate_block_monolithic_demotion,
    _synthesize_plan_draft,
)
from ._prompt_review import (  # noqa: E402,F401
    _build_impasse_escape_hatch_section,
    _build_review_prompt,
    _build_role_context,
    _build_role_restrictions_section,
    _extract_plan_overview,
    _render_contract_tasks,
    _summarize_issue,
)

# reviews helpers live in _reviews.py (#3312 slice-4); re-exported here.
from ._reviews import (  # noqa: E402,F401
    _aggregate_review_verdicts,
    _read_review_verdict,
    _read_tester_gaps,
)
from ._slice_state import (  # noqa: E402,F401
    _check_slice_evidence_reachability,
    _classify_non_complete_slice,
    _cross_repo_hold_marker,
    _cross_repo_hold_resolution,
    _escalate_blocked_slice_to_hitl,
    _escalate_corrupt_slice_to_hitl,
    _escalate_layer_c_hitl,
    _is_slice_dag_mode,
    _lookup_peer_consensus_tracker_or_none,
    _register_cross_repo_hold,
    _resolve_pipeline_worktree_path,
    _resolve_slice_base_branch,
    _resolve_slice_gate_repo,
    _resolve_slice_worktree_path,
    _slice_has_pending_decision,
)

# statefiles helpers live in _statefiles.py (#3312 slice-4); re-exported here.
from ._statefiles import (  # noqa: E402,F401
    _commit_statefiles_to_worktree,
    _detect_default_branch,
    _ensure_statefiles_on_branch,
    _fetch_pr_state,
    _resolve_origin_ref,
    persist_contract_statefiles,
)

# worktree_sync helpers live in _worktree_sync.py (#3312 slice-4); re-exported here.
from ._worktree_sync import (  # noqa: E402,F401
    StalePipelineBranchError,
    WorktreeSyncOutcome,
    _build_sync_recovery_backup_ref,
    _collect_local_only_commits,
    _create_sync_recovery_backup_ref,
    _read_tree_head,
    _rebase_pipeline_branch_onto_base,
    _refresh_pipeline_branch_against_current_base,
    _restore_missing_state_files_from_head,
    _sync_worktree_with_remote,
)
