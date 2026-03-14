"""
Core types for the health check framework.

Defines the HealthCheck protocol, HealthResult dataclass, and supporting enums.
All health checks — programmatic (Tier 1) and semantic (Tier 2) — produce
HealthResult values and conform to the HealthCheck protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from health_checks.context import PipelineHealthContext


class HealthStatus(StrEnum):
    """Outcome of a health check."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class HealthTier(StrEnum):
    """Which tier a check belongs to."""

    PROGRAMMATIC = "tier1"
    AGENT = "tier2"


class HealthTrigger(StrEnum):
    """Lifecycle event that triggers health checks."""

    STARTUP = "startup"
    RUNTIME_TICK = "runtime_tick"
    WAVE_COMPLETE = "wave_complete"
    PHASE_COMPLETE = "phase_complete"
    ON_DEMAND = "on_demand"


class HealthAction(StrEnum):
    """Suggested action based on health check outcome."""

    CONTINUE = "continue"
    FAIL_PIPELINE = "fail_pipeline"
    ALERT = "alert"


@dataclass(frozen=True)
class HealthResult:
    """Result produced by a health check.

    Attributes:
        status: Overall health status.
        check_name: Name of the check that produced this result.
        tier: Which tier the check belongs to.
        reasoning: Human-readable explanation of the result.
        action: Suggested action for the caller.
        details: Arbitrary structured data for debugging.
        timestamp: When the check ran.
    """

    status: HealthStatus
    check_name: str
    tier: HealthTier
    reasoning: str
    action: HealthAction = HealthAction.CONTINUE
    details: dict[str, object] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, object]:
        """Serialize for JSON / event payloads."""
        return {
            "status": self.status.value,
            "check_name": self.check_name,
            "tier": self.tier.value,
            "reasoning": self.reasoning,
            "action": self.action.value,
            "details": self.details,
            "timestamp": self.timestamp.replace(tzinfo=None).isoformat() + "Z",
        }


@runtime_checkable
class HealthCheck(Protocol):
    """Structural protocol that all health checks must satisfy (DD-1).

    Any class (or even a plain object) that has these three attributes
    and a ``run`` method with the correct signature is a valid health
    check — no inheritance required.
    """

    name: str
    tier: HealthTier
    triggers: frozenset[HealthTrigger]

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Execute the check and return a result.

        Implementations must never raise — they should catch internal
        errors and return a HealthResult with appropriate status.
        """
        ...
