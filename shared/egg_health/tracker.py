"""HealthTracker: record observed healthy/unhealthy samples and summarise transitions.

The tracker is updated every time the owning service evaluates its health
(typically each time its ``/api/v1/health`` endpoint is hit). From the
recorded observations it computes:

- ``healthy_since`` — timestamp of the most recent transition to healthy.
  If the service has been healthy since the first observation, this is the
  process start time (matching the "or process start if never unhealthy
  this run" semantics spelled out in issue #1855).
- ``last_unhealthy_at`` — most recent unhealthy sample, ``None`` if none
  have been observed.
- ``recent_transitions`` — bounded ring buffer of the last N transitions.

The observation-driven model (vs. a background poller) keeps the
implementation lightweight: in production the endpoint is hit by k8s
readiness probes on a regular cadence; locally it is hit whenever an
operator pokes it.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any


class HealthTracker:
    """Thread-safe tracker for healthy/unhealthy transitions of a single service."""

    def __init__(self, *, max_transitions: int = 10) -> None:
        self._lock = threading.Lock()
        self._process_start: datetime = datetime.now(UTC)
        self._current_healthy: bool | None = None
        self._healthy_since: datetime | None = None
        self._last_unhealthy_at: datetime | None = None
        self._transitions: deque[dict[str, str]] = deque(maxlen=max_transitions)

    def record(self, is_healthy: bool, *, now: datetime | None = None) -> None:
        """Record a new health observation and update derived state."""
        ts = now if now is not None else datetime.now(UTC)
        with self._lock:
            if self._current_healthy is None:
                # First observation.
                if is_healthy:
                    # "process start if never unhealthy this run"
                    self._healthy_since = self._process_start
                else:
                    self._last_unhealthy_at = ts
                self._transitions.append(
                    {"ts": ts.isoformat(), "state": "healthy" if is_healthy else "unhealthy"}
                )
            elif self._current_healthy and not is_healthy:
                self._last_unhealthy_at = ts
                self._healthy_since = None
                self._transitions.append({"ts": ts.isoformat(), "state": "unhealthy"})
            elif not self._current_healthy and is_healthy:
                self._healthy_since = ts
                self._transitions.append({"ts": ts.isoformat(), "state": "healthy"})
            else:
                # still unhealthy — refresh last_unhealthy_at but don't record
                # a transition (it wasn't one).
                self._last_unhealthy_at = ts
            self._current_healthy = is_healthy

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of the tracker state."""
        with self._lock:
            return {
                "process_start_time": self._process_start.isoformat(),
                "healthy_since": (self._healthy_since.isoformat() if self._healthy_since else None),
                "last_unhealthy_at": (
                    self._last_unhealthy_at.isoformat() if self._last_unhealthy_at else None
                ),
                "recent_transitions": list(self._transitions),
            }
