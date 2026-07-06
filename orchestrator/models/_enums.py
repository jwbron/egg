"""Status enums for orchestrator pipeline models.

Extracted from the monolithic ``models.py`` (#3450, slice-1 of #3312's
decomposition program) following the canonical domain-split pattern
(docs/guides/decomposition-pattern.md). Every symbol re-exports through the
``models`` barrel, which stays the stable public API.
"""

from enum import StrEnum

# Import AgentRole from the canonical source in egg_contracts.
# Re-exported here (and through the ``models`` barrel) for backward compatibility.
from egg_contracts.agent_roles import AgentRole  # noqa: F401 — re-export


class PipelineStatus(StrEnum):
    """Overall status of a pipeline."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls) -> frozenset[PipelineStatus]:
        """Statuses indicating the pipeline has reached a terminal state.

        A terminal pipeline spawns no further agents and transitions to no
        other status. Centralized here so callers share one definition
        rather than redefining the set and drifting (#3174 review).
        """
        return frozenset({cls.COMPLETE, cls.FAILED, cls.CANCELLED})


class PipelineMode(StrEnum):
    """Pipeline execution mode."""

    ISSUE = "issue"  # Standard issue-driven SDLC pipeline


class AgentExecutionStatus(StrEnum):
    """Status of an individual agent execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class ContainerStatus(StrEnum):
    """Status of a sandbox container."""

    PENDING = "pending"
    CREATING = "creating"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"
    REMOVED = "removed"


# Single source of truth for "which container statuses count as live"
# (#2420). Both ``routes/pipelines._count_live_pods_for_pipeline`` and
# ``startup_reconciliation.reconcile_stale_containers`` import this so
# the two label-scoped pod checks can't drift — drift would reintroduce
# the #2411 false-positive class (live pipelines marked FAILED at startup
# while the start_pipeline guard still treats their pods as live).
#
# Pending / Creating / Running are the only statuses that map to a pod
# whose work is still in flight. Terminal phases (Failed / Succeeded →
# ``ContainerStatus.FAILED`` / ``EXITED``) are deliberately excluded:
# k8s keeps such pod objects around for the ``ttlSecondsAfterFinished``
# window (600s in our Job specs) after the container exits, so a naive
# "any pod with this label" check would treat a recently-finished pod as
# live and mask a wedged pipeline whose work has actually stopped. The
# guard's contract is "is there anything still doing work?" — terminal
# pod objects within the TTL window do not count.
LIVE_POD_STATUSES: tuple[ContainerStatus, ...] = (
    ContainerStatus.PENDING,
    ContainerStatus.CREATING,
    ContainerStatus.RUNNING,
)


class DecisionStatus(StrEnum):
    """Status of a HITL decision."""

    PENDING = "pending"
    RESOLVED = "resolved"
    TIMEOUT = "timeout"  # Vestigial: kept for backwards compatibility with persisted pipeline state
    CANCELLED = "cancelled"


class ReviewerType(StrEnum):
    """Reviewer specialization types matching GHA reviewer matrix."""

    AGENT_DESIGN = "agent-design"
    CODE = "code"
    CONTRACT = "contract"


class ProgressState(StrEnum):
    """State of a structured progress event."""

    WORKING = "working"
    BLOCKED = "blocked"
    COMPLETE = "complete"
