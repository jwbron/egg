"""
Pydantic models for orchestrator pipeline state.

These models represent the orchestrator's view of pipeline execution,
including container state, HITL decisions, and agent coordination.

----

Decomposed into a sub-package (#3312, slice-1; closes #3450) following the
canonical domain-split pattern (docs/guides/decomposition-pattern.md). This
barrel is the **stable public API**: every model / enum / helper keeps its
identity reachable on the ``models`` module path, and every external symbol
(``from models import X`` — the dominant importer style, ~197 files) resolves
through here. The definitions live in domain-grouped, underscore-prefixed
submodules; each submodule imports its dependencies from its siblings, so the
split is a pure refactor with no forward-reference breakage:

* ``_enums``     — status ``StrEnum``s, the live-pod-status set, ``AgentRole`` re-export
* ``_decisions`` — ``HITLDecision`` / ``OperatorDirective`` / ``IterationSummary``
* ``_execution`` — review + container / agent / phase execution models
* ``_config``    — ``PipelineConfig`` + consensus-timeout resolution
* ``_pipeline``  — ``RepoSpec`` / ``Pipeline`` + ``resolve_slice_repo``
* ``_events``    — ``PipelineEvent`` / ``ProgressEvent``
"""

# Re-exported sibling-package symbols that consumers pull through ``models``
# (e.g. ``from models import PipelinePhase`` — a load-bearing back-compat seam,
# used with an ``as`` alias in the routes/ loop). Keep them resolving here.
from agent_model_resolution import OVERSEER_TIER_MODELS  # noqa: F401 — re-export
from egg_contracts.agent_roles import AgentRole  # noqa: F401 — re-export
from egg_contracts.models import PipelinePhase, Slice  # noqa: F401 — re-export
from slice_id_validation import SLICE_ID_PATTERN  # noqa: F401 — re-export

from ._config import (  # noqa: F401 — re-export
    PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN,
    PipelineConfig,
    resolve_consensus_timeout_minutes,
)
from ._decisions import (  # noqa: F401 — re-export
    HITLDecision,
    IterationSummary,
    OperatorDirective,
)
from ._enums import (  # noqa: F401 — re-export
    LIVE_POD_STATUSES,
    AgentExecutionStatus,
    ContainerStatus,
    DecisionStatus,
    PipelineMode,
    PipelineStatus,
    ProgressState,
    ReviewerType,
)
from ._events import PipelineEvent, ProgressEvent  # noqa: F401 — re-export
from ._execution import (  # noqa: F401 — re-export
    AgentExecution,
    AgentExitInfo,
    AggregatedReviewResult,
    ContainerInfo,
    CycleTiming,
    PhaseExecution,
    ReviewVerdict,
)
from ._pipeline import (  # noqa: F401 — re-export
    Pipeline,
    RepoSpec,
    resolve_slice_repo,
)

__all__ = [
    "LIVE_POD_STATUSES",
    "OVERSEER_TIER_MODELS",
    "PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN",
    "SLICE_ID_PATTERN",
    "AgentExecution",
    "AgentExecutionStatus",
    "AgentExitInfo",
    "AgentRole",
    "AggregatedReviewResult",
    "ContainerInfo",
    "ContainerStatus",
    "CycleTiming",
    "DecisionStatus",
    "HITLDecision",
    "IterationSummary",
    "OperatorDirective",
    "PhaseExecution",
    "Pipeline",
    "PipelineConfig",
    "PipelineEvent",
    "PipelineMode",
    "PipelinePhase",
    "PipelineStatus",
    "ProgressEvent",
    "ProgressState",
    "RepoSpec",
    "ReviewVerdict",
    "ReviewerType",
    "Slice",
    "resolve_consensus_timeout_minutes",
    "resolve_slice_repo",
]
