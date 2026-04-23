"""Structured HEARTBEAT message helpers (issue #1897).

Provides server-side HEARTBEAT handling primitives that are independent
of routing/HTTP concerns:

* Sliding-window rate limiter keyed by ``(pipeline_id, role)`` honouring
  ``EGG_HEARTBEAT_RATE_LIMIT`` (default 20/min per role).
* Per-role dedup: two consecutive identical ``(state, waiting_on)``
  tuples from the same role produce only one bus message.

See plan TASK-3-2 / TASK-3-4 and
docs/reference/agent-wait-patterns.md §5.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class RateLimitDecision:
    """Outcome of a rate-limit check."""

    allowed: bool
    retry_after_seconds: int = 0


class HeartbeatCoordinator:
    """Per-pipeline/per-role HEARTBEAT rate limiter + dedup tracker.

    Thread-safe.  One instance lives as an orchestrator-process-global
    singleton (``get_heartbeat_coordinator``).
    """

    def __init__(self, window_seconds: int = 60) -> None:
        self._window = float(window_seconds)
        self._lock = threading.Lock()
        # (pipeline_id, role) -> deque of epoch-seconds timestamps
        self._windows: dict[tuple[str, str], deque[float]] = {}
        # (pipeline_id, role) -> last-seen (state, waiting_on)
        self._last_state: dict[tuple[str, str], tuple[str, str]] = {}

    def check_rate_limit(
        self,
        pipeline_id: str,
        role: str,
        limit_per_minute: int,
    ) -> RateLimitDecision:
        """Decide whether to allow the next HEARTBEAT.

        If allowed, records the new timestamp in the window.  If not
        allowed, returns a decision carrying the seconds until the
        oldest timestamp in the window expires (a caller can surface
        this via the ``retry_after`` body field of an HTTP 429).
        """
        key = (pipeline_id, role)
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            window = self._windows.setdefault(key, deque())
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= limit_per_minute:
                oldest = window[0]
                retry_after = int(max(1, (oldest + self._window) - now))
                return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)
            window.append(now)
        return RateLimitDecision(allowed=True)

    def is_duplicate(
        self,
        pipeline_id: str,
        role: str,
        state: str,
        waiting_on: str | None,
    ) -> bool:
        """Check whether this ``(state, waiting_on)`` equals the last.

        Returns True (duplicate) if the role's most-recent heartbeat
        tuple is identical; False (fresh) otherwise.  Does NOT record
        the new tuple — the caller should call ``record_state`` after
        successfully delivering the heartbeat.
        """
        key = (pipeline_id, role)
        prev = self._last_state.get(key)
        cur = (state, waiting_on or "")
        return prev is not None and prev == cur

    def record_state(
        self,
        pipeline_id: str,
        role: str,
        state: str,
        waiting_on: str | None,
    ) -> None:
        """Store this role's most-recent ``(state, waiting_on)``."""
        key = (pipeline_id, role)
        with self._lock:
            self._last_state[key] = (state, waiting_on or "")

    def clear(self, pipeline_id: str) -> None:
        """Drop all state for a pipeline (on phase transition)."""
        with self._lock:
            for key in list(self._windows):
                if key[0] == pipeline_id:
                    del self._windows[key]
            for key in list(self._last_state):
                if key[0] == pipeline_id:
                    del self._last_state[key]


_coordinator: HeartbeatCoordinator | None = None
_coord_lock = threading.Lock()


def get_heartbeat_coordinator() -> HeartbeatCoordinator:
    """Return the singleton heartbeat coordinator."""
    global _coordinator
    if _coordinator is None:
        with _coord_lock:
            if _coordinator is None:
                _coordinator = HeartbeatCoordinator()
    return _coordinator


def reset_heartbeat_coordinator() -> None:
    """Reset for tests."""
    global _coordinator
    _coordinator = None
