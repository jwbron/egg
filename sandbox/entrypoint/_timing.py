"""Startup-timing instrumentation (StartupTimer, timed_phase)."""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Generator
from typing import Any

from ._config import Logger
from ._core import _CONTAINER_START_TIME

ENABLE_STARTUP_TIMING = os.environ.get("EGG_TIMING", "0") == "1"


class StartupTimer:
    """Collects timing data for startup phases."""

    def __init__(self) -> None:
        self.timings: list[tuple[str, float]] = []
        self.start_time: float = time.perf_counter()
        self._phase_start: float | None = None
        self._phase_name: str | None = None
        self.host_timings: list[tuple[str, float]] = []
        self.host_total_time: float = 0.0
        self.docker_startup_time: float = 0.0  # Gap between host launch and container start
        # Capture time spent in Python init (imports) before this point
        # Uses wall clock since _CONTAINER_START_TIME is wall clock
        self.python_init_time: float = (time.time() - _CONTAINER_START_TIME) * 1000
        self._load_host_timing()

    def _load_host_timing(self) -> None:
        """Load host timing data from environment variable."""
        import json

        host_timing_json = os.environ.get("EGG_HOST_TIMING", "")
        if host_timing_json:
            try:
                data = json.loads(host_timing_json)
                self.host_timings = data.get("timings", [])
                self.host_total_time = data.get("total_time", 0.0)
            except json.JSONDecodeError, KeyError:
                pass

        # Calculate docker startup gap (time between host launching container and Python starting)
        host_launch_time_str = os.environ.get("EGG_HOST_LAUNCH_TIME", "")
        if host_launch_time_str:
            try:
                host_launch_time = float(host_launch_time_str)
                # Gap = container start time - host launch time (in milliseconds)
                self.docker_startup_time = (_CONTAINER_START_TIME - host_launch_time) * 1000
            except ValueError, TypeError:
                pass

    def start_phase(self, name: str) -> None:
        """Start timing a phase."""
        if not ENABLE_STARTUP_TIMING:
            return
        self._phase_name = name
        self._phase_start = time.perf_counter()

    def end_phase(self) -> None:
        """End timing the current phase."""
        if not ENABLE_STARTUP_TIMING or self._phase_start is None or self._phase_name is None:
            return
        elapsed = (time.perf_counter() - self._phase_start) * 1000  # ms
        self.timings.append((self._phase_name, elapsed))
        self._phase_name = None
        self._phase_start = None

    def phase(self, name: str) -> Any:
        """Context manager for timing a phase."""
        timer = self
        phase_name = name

        class PhaseContext:
            def __enter__(self) -> PhaseContext:
                timer.start_phase(phase_name)
                return self

            def __exit__(self, *args: Any) -> None:
                timer.end_phase()

        return PhaseContext()

    def print_summary(self) -> None:
        """Print combined timing summary (host + container phases)."""
        if not ENABLE_STARTUP_TIMING:
            return
        if not self.timings and not self.host_timings:
            return

        # Container total includes python_init (imports) + all phases
        phases_total = (time.perf_counter() - self.start_time) * 1000
        container_total = self.python_init_time + phases_total
        grand_total = self.host_total_time + self.docker_startup_time + container_total

        print("\n" + "=" * 60)
        print("STARTUP TIMING SUMMARY")
        print("=" * 60)
        print(f"{'Phase':<40} {'Time (ms)':>10} {'%':>6}")
        print("-" * 60)

        # Print host phases (% of grand total)
        if self.host_timings:
            print("HOST:")
            for name, elapsed in self.host_timings:
                pct = (elapsed / grand_total) * 100 if grand_total > 0 else 0
                bar = "█" * int(pct / 5)
                print(f"  {name:<38} {elapsed:>10.1f} {pct:>5.1f}% {bar}")
            print(f"  {'(host total)':<38} {self.host_total_time:>10.1f}")
            print()

        # Print docker startup gap (time from host launch to container Python starting)
        if self.docker_startup_time > 0:
            print("DOCKER:")
            pct = (self.docker_startup_time / grand_total) * 100 if grand_total > 0 else 0
            bar = "█" * int(pct / 5)
            print(
                f"  {'container_startup':<38} {self.docker_startup_time:>10.1f} {pct:>5.1f}% {bar}"
            )
            print()

        # Print container phases (% of container total for meaningful breakdown)
        if self.timings or self.python_init_time > 0:
            print("CONTAINER:")
            # Show python_init first (time for imports before StartupTimer was created)
            if self.python_init_time > 0:
                pct = (self.python_init_time / container_total) * 100 if container_total > 0 else 0
                bar = "█" * int(pct / 5)
                print(f"  {'python_init':<38} {self.python_init_time:>10.1f} {pct:>5.1f}% {bar}")
            for name, elapsed in self.timings:
                pct = (elapsed / container_total) * 100 if container_total > 0 else 0
                bar = "█" * int(pct / 5)
                print(f"  {name:<38} {elapsed:>10.1f} {pct:>5.1f}% {bar}")
            print(f"  {'(container total)':<38} {container_total:>10.1f}")

        print("-" * 60)
        print(f"{'GRAND TOTAL':<40} {grand_total:>10.1f}")
        print("=" * 60 + "\n")


_startup_timer = StartupTimer()


@contextlib.contextmanager
def timed_phase(name: str, logger: Logger) -> Generator[None]:
    """Context manager that combines startup timing with debug logging.

    Wraps both _startup_timer.phase() and logger.phase_start/phase_end
    to reduce repetition in the main startup sequence.
    """
    logger.phase_start(name)
    try:
        with _startup_timer.phase(name):
            yield
    finally:
        logger.phase_end(name)
