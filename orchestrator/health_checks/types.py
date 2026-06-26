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


class Severity(StrEnum):
    """Finding severity, mirroring the overseer alert vocabulary (#2270 §4).

    Kept a ``StrEnum`` so a :class:`Finding` compares equal to the plain-string
    severities the calibration corpus asserts against (``Severity.HIGH ==
    "high"``), letting the production type plug straight into the slice-1
    harness without the corpus importing it.
    """

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FindingClass(StrEnum):
    """The detection-plane finding classes delivered across #2270 slices 4/7/8.

    The detection plane matches a detector's output structurally on the raw
    string, so a detector MAY emit a class not listed here (slice-8 extends the
    coverage-gap survey) without breaking matching. This enum names the classes
    pinned by the slice-1 calibration corpus for type-safety at the call sites
    that construct them.
    """

    OVERSEER_SELF_INJECTION = "overseer_self_injection"
    ALERT_REFLECTION = "alert_reflection"
    PHASE_STALL = "phase_stall"
    HEARTBEAT_STALL = "heartbeat_stall"
    BRANCH_DIVERGENCE = "branch_divergence"
    CONTAINER_DEATH = "container_death"


@dataclass(frozen=True)
class Finding:
    """A deterministic detection-plane finding (#2270 §-core, slice-4).

    The orchestrator-side detection plane runs detectors over an
    ``EventStreamSnapshot`` and each detector returns ``Optional[Finding]``.
    Routine findings carry ``requires_adjudication=False`` and are handled by
    the bounded corrective vocabulary (slice-6) without ever invoking an LLM;
    only an *ambiguous / high-stakes* finding sets ``requires_adjudication`` and
    triggers the on-demand OVERSEER adjudicator (slice-4 escalation path).

    The field set is the slice-1 corpus contract: ``finding_class``,
    ``severity``, ``evidence``, ``recommended_action``, ``requires_adjudication``.

    Attributes:
        finding_class: Stable class string (see :class:`FindingClass`).
        severity: One of :class:`Severity`.
        evidence: Structured, JSON-serialisable evidence the detector observed.
        recommended_action: Human/operator-facing next step.
        requires_adjudication: Whether this finding must be escalated to the
            on-demand OVERSEER adjudicator before any corrective action.
        detector_key: The detector that produced it (for routing / audit).
        timestamp: When the finding was produced.
    """

    finding_class: str
    severity: str
    evidence: dict[str, object] = field(default_factory=dict)
    recommended_action: str = ""
    requires_adjudication: bool = False
    detector_key: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, object]:
        """Serialize for JSON / event payloads."""
        return {
            "finding_class": str(self.finding_class),
            "severity": str(self.severity),
            "evidence": self.evidence,
            "recommended_action": self.recommended_action,
            "requires_adjudication": self.requires_adjudication,
            "detector_key": self.detector_key,
            "timestamp": self.timestamp.isoformat(),
        }


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
            "timestamp": self.timestamp.isoformat(),
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
