"""Startup timing utilities for jib.

This module provides the StartupTimer class for debugging
host-side startup performance.
"""

import time


class StartupTimer:
    """Collects timing data for startup phases (host-side)."""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.timings: list[tuple[str, float]] = []
        self.start_time: float = time.perf_counter()
        self._phase_start: float | None = None
        self._phase_name: str | None = None

    def start_phase(self, name: str) -> None:
        """Start timing a phase."""
        if not self.enabled:
            return
        self._phase_name = name
        self._phase_start = time.perf_counter()

    def end_phase(self) -> None:
        """End timing the current phase."""
        if not self.enabled or self._phase_start is None:
            return
        elapsed = (time.perf_counter() - self._phase_start) * 1000  # ms
        self.timings.append((self._phase_name, elapsed))
        self._phase_name = None
        self._phase_start = None

    def phase(self, name: str):
        """Context manager for timing a phase."""
        timer = self
        phase_name = name

        class PhaseContext:
            def __enter__(self):
                timer.start_phase(phase_name)
                return self

            def __exit__(self, *args):
                timer.end_phase()

        return PhaseContext()

    def to_json(self) -> str:
        """Serialize timings to JSON for passing to container."""
        import json

        if not self.enabled or not self.timings:
            return ""
        return json.dumps(
            {"timings": self.timings, "total_time": (time.perf_counter() - self.start_time) * 1000}
        )

    def print_summary(self) -> None:
        """Print timing summary."""
        if not self.enabled or not self.timings:
            return

        total_time = (time.perf_counter() - self.start_time) * 1000

        print("\n" + "=" * 60)
        print("HOST-SIDE STARTUP TIMING SUMMARY")
        print("=" * 60)
        print(f"{'Phase':<35} {'Time (ms)':>10} {'%':>6}")
        print("-" * 60)

        for name, elapsed in self.timings:
            pct = (elapsed / total_time) * 100 if total_time > 0 else 0
            bar = "█" * int(pct / 5)  # Simple bar graph
            print(f"{name:<35} {elapsed:>10.1f} {pct:>5.1f}% {bar}")

        print("-" * 60)
        print(f"{'TOTAL':<35} {total_time:>10.1f}")
        print("=" * 60 + "\n")


# Global timer instance (disabled by default, enabled via --time flag)
_host_timer = StartupTimer(enabled=False)
