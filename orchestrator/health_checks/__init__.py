"""
Health check framework for pipeline failure detection.

Re-exports core types so consumers can write:
    from health_checks import HealthCheck, HealthResult, HealthStatus
"""

from health_checks.context import PipelineHealthContext
from health_checks.types import (
    HealthAction,
    HealthCheck,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)

__all__ = [
    "HealthAction",
    "HealthCheck",
    "HealthResult",
    "HealthStatus",
    "HealthTier",
    "HealthTrigger",
    "PipelineHealthContext",
]
