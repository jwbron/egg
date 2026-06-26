"""
Health check framework for pipeline failure detection.

Re-exports core types so consumers can write:
    from health_checks import HealthCheck, HealthResult, HealthStatus
"""

from health_checks.context import PipelineHealthContext
from health_checks.detection_plane import (
    DetectionPlane,
    Detector,
    EventStreamSnapshot,
    LifecycleOwner,
    PhaseStallDetector,
    RunningAgent,
    default_detection_plane,
    snapshot_from_health_context,
)
from health_checks.types import (
    Finding,
    FindingClass,
    HealthAction,
    HealthCheck,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
    Severity,
)

__all__ = [
    "DetectionPlane",
    "Detector",
    "EventStreamSnapshot",
    "Finding",
    "FindingClass",
    "HealthAction",
    "HealthCheck",
    "HealthResult",
    "HealthStatus",
    "HealthTier",
    "HealthTrigger",
    "LifecycleOwner",
    "PhaseStallDetector",
    "PipelineHealthContext",
    "RunningAgent",
    "Severity",
    "default_detection_plane",
    "snapshot_from_health_context",
]
