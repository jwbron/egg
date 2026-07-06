"""
Pipeline CRUD endpoints for egg-orchestrator.
"""

import concurrent.futures  # noqa: F401 — retained for _pkg.concurrent re-export
import functools  # noqa: F401 — retained for _pkg.functools re-export
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
        ContainerSpawnError,
        SpawnFailureError,  # noqa: F401 — retained for _pkg re-export / patch seam
        get_container_spawner,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from decision_queue import get_decision_queue  # type: ignore
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
        KubernetesSpawnError,
        get_kubernetes_spawner,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from models import (  # type: ignore
        LIVE_POD_STATUSES,
        AgentExecutionStatus,  # noqa: F401 — retained for _pkg re-export / patch seam
        AgentExitInfo,  # noqa: F401 — retained for _pkg re-export / patch seam
        AgentRole,
        ContainerInfo,  # noqa: F401 — retained for _pkg re-export / patch seam
        ContainerStatus,  # noqa: F401 — retained for _pkg re-export / patch seam
        CycleTiming,
        DecisionStatus,
        HITLDecision,  # noqa: F401 — retained for _pkg re-export / patch seam
        IterationSummary,
        OperatorDirective,
        PhaseExecution,  # noqa: F401 — retained for _pkg re-export / patch seam
        Pipeline,  # noqa: F401 — retained for _pkg re-export / patch seam
        PipelineMode,  # noqa: F401 — retained for _pkg re-export / patch seam
        PipelinePhase,
        PipelineStatus,
        RepoSpec,  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from slice_id_validation import (
        extract_slice_id,  # type: ignore  # noqa: F401 — retained for _pkg re-export / patch seam
    )
    from state_store import (  # type: ignore
        InvalidPipelineIdError,  # noqa: F401 — retained for _pkg re-export / patch seam
        PipelineNotFoundError,
        StateStore,  # noqa: F401 — retained for _pkg re-export / patch seam
        StateStoreError,
        StateValidationError,
        get_pipeline_state_lock,
        get_state_store,
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

        repo_volumes, worktree_repo_path = _map_host_repos(
            pipeline,
            host_gid=host_gid,
            host_repo_map=host_repo_map,
            host_uid=host_uid,
            pipeline_id=pipeline_id,
            pipeline_repos=pipeline_repos,
            spawner=spawner,
            worktree_id=worktree_id,
            repo_volumes=repo_volumes,
            worktree_repo_path=worktree_repo_path,
        )

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
        _sync_source_branch_drafts(
            gateway_mode=gateway_mode,
            pipeline=pipeline,
            pipeline_id=pipeline_id,
            spawner=spawner,
            store=store,
            worktree_repo_path=worktree_repo_path,
        )

        # Create companion contract in the worktree (deferred from pipeline
        # creation so it doesn't pollute the main repo working directory).
        pipeline, _contract_setup_done = _sync_contract_setup(
            pipeline,
            gateway_mode=gateway_mode,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            repo_path=repo_path,
            source_branch_for_contract_pull=source_branch_for_contract_pull,
            spawner=spawner,
            store=store,
            worktree_repo_path=worktree_repo_path,
        )
        if _contract_setup_done:
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
        pipeline, _start_phase_done = _start_phase_setup(
            pipeline,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            store=store,
            worktree_repo_path=worktree_repo_path,
        )
        if _start_phase_done:
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
from ._run_implement import (  # noqa: E402,F401
    _run_implement_phase_slices,
)
from ._run_implement_support import (  # noqa: E402,F401
    _commit_and_push_slice_statefiles_impl,
    _contract_loader_impl,
    _persist_slice_status_complete_impl,
)
from ._run_pipeline_setup import (  # noqa: E402,F401  # noqa: E402,F401  # noqa: E402,F401  # noqa: E402,F401
    _map_host_repos,
    _start_phase_setup,
    _sync_contract_setup,
    _sync_source_branch_drafts,
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
