"""Runtime health-transition tracking.

Small, thread-safe utility used by the orchestrator and gateway to annotate
their ``/api/v1/health`` responses with readiness history so operators can
tell the difference between "has been healthy for a while" and "just now
transitioned to healthy after recent flapping/cold-start."

See GitHub issue #1855.
"""

from .tracker import HealthTracker

__all__ = ["HealthTracker"]
