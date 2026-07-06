"""
Pipeline CRUD endpoints for egg-orchestrator.
"""

import concurrent.futures  # noqa: F401 — retained for _pkg.concurrent re-export
import functools  # noqa: F401 — retained for _pkg.functools re-export
import json  # noqa: F401 — retained for _pkg re-export / patch seam
import os
import re
import subprocess  # noqa: F401 — retained for _pkg re-export / patch seam
import sys
import threading  # noqa: F401 — retained for _pkg re-export / patch seam
import time  # noqa: F401 — retained for _pkg re-export / patch seam
from collections.abc import Callable  # noqa: F401 — retained for _pkg re-export / patch seam
from datetime import UTC, datetime  # noqa: F401 — retained for _pkg re-export / patch seam
from enum import StrEnum
from pathlib import Path
from typing import (  # noqa: F401 — NamedTuple retained for _pkg base-class re-export (_populate)
    Any,
    Literal,
    NamedTuple,
)
from uuid import uuid4  # noqa: F401 — retained for _pkg re-export / patch seam

try:
    from docker.errors import DockerException
except ImportError:

    class DockerException(Exception):  # type: ignore[no-redef]
        pass


from flask import (  # noqa: F401 — retained for _pkg re-export / patch seam
    Blueprint,
    Response,
    jsonify,
    request,
    stream_with_context,
)

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


# Completion bases a caller may declare when it has positive, verified
# evidence a slice finished even though not every task is marked COMPLETE
# on the contract (the crash-recovery / merged-skip paths). See
# :class:`SliceCompletionInvariantError`.
_VERIFIED_SLICE_COMPLETION_BASES = frozenset({"merged", "consensus_complete"})


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
        ContainerStatus,  # noqa: F401 — retained for _pkg re-export / patch seam
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
        StateStore,  # noqa: F401 — retained for _pkg re-export / patch seam
        StateStoreError,
        StateValidationError,
        get_pipeline_state_lock,
        get_state_store,
    )
except ImportError:
    import agent_salvage  # type: ignore[no-redef]  # noqa: F401 — retained for _pkg re-export / patch seam
    from container_spawner import (  # type: ignore
        ContainerSpawnError,  # noqa: F401 — retained for _pkg re-export / patch seam
        SpawnFailureError,  # noqa: F401 — retained for _pkg re-export / patch seam
        get_container_spawner,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from decision_queue import (
        get_decision_queue,  # type: ignore  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from docker_client import (  # type: ignore
        ContainerNotFoundError,  # noqa: F401 — retained for _pkg re-export / patch seam
        ContainerOperationError,  # noqa: F401 — retained for _pkg re-export / patch seam
        DockerClientError,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from gateway_client import (  # type: ignore
        GatewayError,  # noqa: F401 — retained for _pkg re-export / patch seam
        _rebase_with_agent_output_autoresolve,  # noqa: F401
    )
    from kubernetes_client import (  # type: ignore
        LABEL_AGENT_ROLE,  # noqa: F401 — retained for _pkg re-export / patch seam
        LABEL_PIPELINE_ID,  # noqa: F401 — retained for _pkg re-export / patch seam
        LABEL_SLICE_ID,  # noqa: F401 — retained for _pkg re-export / patch seam
        JobOperationError,  # noqa: F401 — retained for _pkg re-export / patch seam
        KubernetesClientError,  # noqa: F401 — retained for _pkg re-export / patch seam
        PodNotFoundError,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from kubernetes_spawner import (  # type: ignore
        KubernetesSpawnError,  # noqa: F401 — retained for _pkg re-export / patch seam
        get_kubernetes_spawner,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from models import (  # type: ignore
        LIVE_POD_STATUSES,
        AgentExecutionStatus,  # noqa: F401 — retained for _pkg re-export / patch seam
        AgentExitInfo,  # noqa: F401 — retained for _pkg re-export / patch seam
        AgentRole,  # noqa: F401 — retained for _pkg re-export / patch seam
        ContainerInfo,  # noqa: F401 — retained for _pkg re-export / patch seam
        ContainerStatus,  # noqa: F401 — retained for _pkg re-export / patch seam
        CycleTiming,  # noqa: F401 — retained for _pkg re-export / patch seam
        DecisionStatus,  # noqa: F401 — retained for _pkg re-export / patch seam
        HITLDecision,  # noqa: F401 — retained for _pkg re-export / patch seam
        IterationSummary,  # noqa: F401 — retained for _pkg re-export / patch seam
        OperatorDirective,  # noqa: F401 — retained for _pkg re-export / patch seam
        PhaseExecution,  # noqa: F401 — retained for _pkg re-export / patch seam
        Pipeline,  # noqa: F401 — retained for _pkg re-export / patch seam
        PipelineMode,  # noqa: F401 — retained for _pkg re-export / patch seam
        PipelinePhase,  # noqa: F401 — retained for _pkg re-export / patch seam
        PipelineStatus,  # noqa: F401 — retained for _pkg re-export / patch seam
        RepoSpec,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from slice_id_validation import (
        extract_slice_id,  # type: ignore  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from state_store import (  # type: ignore
        InvalidPipelineIdError,  # noqa: F401 — retained for _pkg re-export / patch seam
        PipelineNotFoundError,  # noqa: F401 — retained for _pkg re-export / patch seam
        StateStore,  # noqa: F401 — retained for _pkg re-export / patch seam
        StateStoreError,  # noqa: F401 — retained for _pkg re-export / patch seam
        StateValidationError,  # noqa: F401 — retained for _pkg re-export / patch seam
        get_pipeline_state_lock,  # noqa: F401 — retained for _pkg re-export / patch seam
        get_state_store,  # noqa: F401 — retained for _pkg re-export / patch seam
    )

from egg_contracts.orchestrator import (  # noqa: F401 — retained for _pkg re-export / patch seam
    load_agent_output,
    save_agent_output,
)
from egg_git.default_branch import (
    get_default_branch,  # noqa: F401 — retained for _pkg re-export / patch seam
)
from lifecycle_auth import require_lifecycle_secret

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
        # Slice-DAG close (issue #3364) — a long-haul monitor threads on
        # slice completes/failures via ``/status/wait``, so the allowlist
        # must let ``slice.closed`` through instead of filtering it out.
        "slice.closed",
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


from routes import get_repo_path  # noqa: E402,F401 — shared helper, retained for _pkg re-export

try:
    from gateway_client import get_gateway_client
except ImportError:
    from orchestrator.gateway_client import (
        get_gateway_client,  # type: ignore  # noqa: F401 — retained for _pkg re-export / patch seam
    )

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


# Import visualization modules for DAG endpoint
try:
    from dag_visualizer import (
        generate_status_report,  # noqa: F401 — retained for _pkg re-export / patch seam
        render_compact_status,  # noqa: F401 — retained for _pkg re-export / patch seam
        render_pipeline_dag,  # noqa: F401 — retained for _pkg re-export / patch seam
        render_progress_bar,  # noqa: F401 — retained for _pkg re-export / patch seam
    )

    _DAG_VISUALIZER_AVAILABLE = True
except ImportError:
    _DAG_VISUALIZER_AVAILABLE = False

# Import SSE streaming support
try:
    from sse import create_sse_stream  # noqa: F401 — retained for _pkg re-export / patch seam

    _SSE_AVAILABLE = True
except ImportError:
    _SSE_AVAILABLE = False

# Import unified SSE streaming support
try:
    from unified_sse import (
        create_unified_sse_stream,  # noqa: F401 — retained for _pkg re-export / patch seam
    )

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


@pipelines_bp.route("", methods=["GET"])
def list_pipelines() -> tuple[Response, int]:
    return _list_pipelines_body()


@pipelines_bp.route("/<pipeline_id>", methods=["GET"])
def get_pipeline(pipeline_id: str) -> tuple[Response, int]:
    return _get_pipeline_body(pipeline_id)


@pipelines_bp.route("", methods=["POST"])
@require_lifecycle_secret
def create_pipeline() -> tuple[Response, int]:
    return _create_pipeline_body()


@pipelines_bp.route("/<pipeline_id>", methods=["PATCH"])
@require_lifecycle_secret
def update_pipeline(pipeline_id: str) -> tuple[Response, int]:
    return _update_pipeline_body(pipeline_id)


# Config keys the live config-update route accepts. Deliberately a tight
# allowlist (#3174): most of PipelineConfig is consumed at submit time or
# mid-phase in ways a partial update could corrupt. Two families qualify:
#
# * ``agent_models`` is re-resolved from a fresh store load before every
#   spawn (the run loop reloads the pipeline at the top of each cycle, and
#   the restart_agent / restart_phase paths load fresh state), so mutating
#   it on a live pipeline is honored by construction.
# * ``consensus_timeout_minutes*`` is re-resolved from a fresh store load
#   by the phase poll loop right before the consensus wall fires (#3490),
#   so a widened window takes effect on a running slice without a restart.
#
# Widen only after verifying the same fresh-reload guarantee holds for the
# new key.
_CONSENSUS_TIMEOUT_CONFIG_KEYS = (
    "consensus_timeout_minutes",
    "consensus_timeout_minutes_refine",
    "consensus_timeout_minutes_plan",
    "consensus_timeout_minutes_implement",
)
_MUTABLE_CONFIG_KEYS = frozenset({"agent_models", *_CONSENSUS_TIMEOUT_CONFIG_KEYS})


@pipelines_bp.route("/<pipeline_id>/config", methods=["PATCH"])
@require_lifecycle_secret
def update_pipeline_config(pipeline_id: str) -> tuple[Response, int]:
    return _update_pipeline_config_body(pipeline_id)


@pipelines_bp.route("/<pipeline_id>", methods=["DELETE"])
@require_lifecycle_secret
def delete_pipeline(pipeline_id: str) -> tuple[Response, int]:
    return _delete_pipeline_body(pipeline_id)


@pipelines_bp.route("/<pipeline_id>/agents/<agent_role>/restart", methods=["POST"])
@require_lifecycle_secret
def restart_agent(pipeline_id: str, agent_role: str) -> tuple[Response, int]:
    return _restart_agent_body(pipeline_id, agent_role)


@pipelines_bp.route("/<pipeline_id>/phases/<phase>/restart", methods=["POST"])
@require_lifecycle_secret
def restart_phase(pipeline_id: str, phase: str) -> tuple[Response, int]:
    return _restart_phase_body(pipeline_id, phase)


@pipelines_bp.route("/<pipeline_id>/local-commits", methods=["GET"])
def list_pipeline_local_commits(pipeline_id: str) -> tuple[Response, int]:
    return _list_pipeline_local_commits_body(pipeline_id)


@pipelines_bp.route("/<pipeline_id>/salvage", methods=["POST"])
@require_lifecycle_secret
def salvage_pipeline_local_commits(pipeline_id: str) -> tuple[Response, int]:
    return _salvage_pipeline_local_commits_body(pipeline_id)


@pipelines_bp.route("/<pipeline_id>/status", methods=["GET"])
def get_pipeline_status(pipeline_id: str) -> tuple[Response, int]:
    return _get_pipeline_status_body(pipeline_id)


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
    return _wait_pipeline_status_body(pipeline_id)
    # Daemon thread is deliberately left running — it exits on
    # its own when ``message_store.get_messages`` returns or the
    # timeout elapses (plan risk R14, accepted).


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


# ---------------------------------------------------------------------------
# Multi-agent execution helpers
# ---------------------------------------------------------------------------


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


# Phases that pause for human approval before advancing (HITL gates)
_HITL_GATE_PHASES = {"refine", "plan"}

# Keywords that indicate human approval at HITL gates
_APPROVE_KEYWORDS = {"approved", "approve", "lgtm", "yes", ""}

# Bare option labels that indicate "request changes" without actionable feedback
_BARE_OPTION_LABELS = {"request changes", "request_changes"}


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


@pipelines_bp.route("/<pipeline_id>/start", methods=["POST"])
@require_lifecycle_secret
def start_pipeline(pipeline_id: str) -> tuple[Response, int]:
    return _start_pipeline_body(pipeline_id)


@pipelines_bp.route("/<pipeline_id>/visualization", methods=["GET"])
def get_pipeline_visualization(pipeline_id: str) -> tuple[Response, int]:
    return _get_pipeline_visualization_body(pipeline_id)


@pipelines_bp.route("/stream", methods=["GET"])
def stream_all_pipelines() -> Response:
    return _stream_all_pipelines_body()


@pipelines_bp.route("/<pipeline_id>/stream", methods=["GET"])
def stream_pipeline(pipeline_id: str) -> Response:
    return _stream_pipeline_body(pipeline_id)


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
    _withdraw_arms_exhausted_decisions,
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
from ._first_principles import (  # noqa: E402,F401
    _restart_refine_phase,
    apply_first_principles_redirect,
)
from ._hitl_rerun import (  # noqa: E402,F401
    _apply_inline_hitl_kickback_to_phase,
    _broadcast_hitl_nonconvergence_alert,
    _build_iteration_summary_from_tracker,
    _build_phase_iteration_context,
    _perform_hitl_phase_rerun,
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
from ._lifecycle_helpers import (  # noqa: E402,F401
    _assert_repo_set_uniform,
    _cleanup_remote_branches,
    _clear_pipeline_runtime_state,
    _compute_gateway_mode,
    _mark_pipeline_records_terminated,
    _normalize_submission_repos,
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
from ._prompt_agent import (  # noqa: E402,F401
    _build_agent_prompt,
    _build_file_boundary_section,
)
from ._prompt_phase import (  # noqa: E402,F401
    _build_brc_preamble,
    _build_phase_prompt,
    _contract_enforcer_role_names,
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
from ._prompt_reviewer import (  # noqa: E402,F401
    _build_agent_roster,
    _build_producer_orientation,
    _build_reviewer_preparation,
    _re_review_priming_block,
)
from ._resolve import (  # noqa: E402,F401
    _brc_history_identifier,
    _collect_all_pipelines,
    _emit_pipeline_event,
    _ensure_pipeline_work_ref,
    _pipeline_identifier,
    _resolve_pipeline,
    _slice_namespace_root,
)

# reviews helpers live in _reviews.py (#3312 slice-4); re-exported here.
from ._reviews import (  # noqa: E402,F401
    _aggregate_review_verdicts,
    _read_review_verdict,
    _read_tester_gaps,
)
from ._routes_crud import (  # noqa: E402,F401
    _create_pipeline_body,
    _delete_pipeline_body,
    _update_pipeline_body,
    _update_pipeline_config_body,
)
from ._routes_lifecycle import (  # noqa: E402,F401
    _list_pipeline_local_commits_body,
    _salvage_pipeline_local_commits_body,
    _start_pipeline_body,
)
from ._routes_read import (  # noqa: E402,F401
    _get_pipeline_body,
    _list_pipelines_body,
)
from ._routes_restart import (  # noqa: E402,F401
    _restart_agent_body,
    _restart_phase_body,
)
from ._routes_status import (  # noqa: E402,F401
    _get_pipeline_status_body,
    _get_pipeline_visualization_body,
    _wait_pipeline_status_body,
)
from ._routes_stream import (  # noqa: E402,F401
    _stream_all_pipelines_body,
    _stream_pipeline_body,
)
from ._run_concurrent import _run_concurrent_phase  # noqa: E402,F401
from ._run_concurrent_retry import (  # noqa: E402,F401
    _run_concurrent_phase_with_impasse_retry,
)
from ._run_concurrent_support import (  # noqa: E402,F401
    _latest_proposal_ts_impl,
    _record_container_exit_impl,
    _record_spawned_agents_impl,
    _retry_transient_spawn_failures_impl,
    _stop_running_containers_impl,
    _superseded_by_restart_impl,
    _update_agents_complete_impl,
)
from ._run_hitl_gate import (  # noqa: E402,F401
    _run_hitl_gate_converge,
)
from ._run_implement import (  # noqa: E402,F401
    _run_implement_phase_slices,
)
from ._run_implement_support import (  # noqa: E402,F401
    _commit_and_push_slice_statefiles_impl,
    _contract_loader_impl,
    _persist_slice_status_complete_impl,
)
from ._run_phase import (  # noqa: E402,F401
    _run_phase_execution,
)
from ._run_phase_blocks import (  # noqa: E402,F401
    _run_implement_advance,
    _run_pending_phase_init,
    _run_plan_advance,
)
from ._run_pipeline import (  # noqa: E402,F401
    _run_pipeline,
)
from ._run_pipeline_setup import (  # noqa: E402,F401  # noqa: E402,F401  # noqa: E402,F401  # noqa: E402,F401  # noqa: E402,F401
    _map_host_repos,
    _resolve_worktree_repo,
    _start_phase_setup,
    _sync_contract_setup,
    _sync_source_branch_drafts,
)
from ._run_pipeline_support import (  # noqa: E402,F401
    _health_monitor_poll_impl,
    _on_health_escalation_impl,
)
from ._run_support import (  # noqa: E402,F401
    _clear_stale_impasses_for_producers,
    _parse_resolution,
    _pipeline_superseded_by_restart,
    _spawn_and_wait,
)
from ._salvage import (  # noqa: E402,F401
    _filter_salvage_worktrees,
    _serialize_commit_report,
    _serialize_salvage_result,
)
from ._slice_completion import (  # noqa: E402,F401
    SliceCompletionInvariantError,
    _slice_produced_commits,
    _validate_slice_completion_basis,
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
from ._stacked_pr import (  # noqa: E402,F401
    _start_stacked_pr_reconciler,
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
from ._status_view import (  # noqa: E402,F401
    _build_slice_diff_summary,
    _consensus_block,
    _get_concurrent_status,
    _get_pr_info,
)
from ._status_wait import (  # noqa: E402,F401
    _build_minimal_status_envelope,
    _build_status_wait_cursor,
    _message_store_tip_id,
    _parse_status_wait_cursor,
    _track_host_wait_end,
    _track_host_wait_start,
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
