"""Tests for sandbox egg_lib timing module."""

import json
import time

from egg_lib.timing import StartupTimer


class TestStartupTimerDisabled:
    """Tests for StartupTimer when disabled."""

    def test_disabled_by_default(self):
        """Timer is disabled by default."""
        timer = StartupTimer()
        assert timer.enabled is False

    def test_disabled_start_phase_noop(self):
        """start_phase is a no-op when disabled."""
        timer = StartupTimer(enabled=False)
        timer.start_phase("test")
        assert timer._phase_start is None

    def test_disabled_end_phase_noop(self):
        """end_phase is a no-op when disabled."""
        timer = StartupTimer(enabled=False)
        timer.end_phase()
        assert len(timer.timings) == 0

    def test_disabled_to_json_empty(self):
        """to_json returns empty string when disabled."""
        timer = StartupTimer(enabled=False)
        assert timer.to_json() == ""

    def test_disabled_print_summary_noop(self, capsys):
        """print_summary outputs nothing when disabled."""
        timer = StartupTimer(enabled=False)
        timer.print_summary()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_disabled_phase_context_manager(self):
        """Phase context manager is a no-op when disabled."""
        timer = StartupTimer(enabled=False)
        with timer.phase("test"):
            pass
        assert len(timer.timings) == 0


class TestStartupTimerEnabled:
    """Tests for StartupTimer when enabled."""

    def test_enabled_flag(self):
        """Timer can be enabled."""
        timer = StartupTimer(enabled=True)
        assert timer.enabled is True

    def test_start_and_end_phase(self):
        """Start and end a phase records timing."""
        timer = StartupTimer(enabled=True)
        timer.start_phase("test_phase")
        time.sleep(0.01)  # Small sleep to ensure measurable time
        timer.end_phase()
        assert len(timer.timings) == 1
        name, elapsed = timer.timings[0]
        assert name == "test_phase"
        assert elapsed > 0  # Should be positive

    def test_multiple_phases(self):
        """Multiple phases are recorded in order."""
        timer = StartupTimer(enabled=True)
        timer.start_phase("phase1")
        timer.end_phase()
        timer.start_phase("phase2")
        timer.end_phase()
        assert len(timer.timings) == 2
        assert timer.timings[0][0] == "phase1"
        assert timer.timings[1][0] == "phase2"

    def test_phase_context_manager(self):
        """Context manager records timing."""
        timer = StartupTimer(enabled=True)
        with timer.phase("context_phase"):
            time.sleep(0.01)
        assert len(timer.timings) == 1
        assert timer.timings[0][0] == "context_phase"
        assert timer.timings[0][1] > 0

    def test_to_json(self):
        """to_json serializes timings."""
        timer = StartupTimer(enabled=True)
        timer.start_phase("json_phase")
        timer.end_phase()
        result = timer.to_json()
        assert result != ""
        data = json.loads(result)
        assert "timings" in data
        assert "total_time" in data
        assert len(data["timings"]) == 1
        assert data["timings"][0][0] == "json_phase"

    def test_to_json_no_timings(self):
        """to_json returns empty when no timings recorded."""
        timer = StartupTimer(enabled=True)
        assert timer.to_json() == ""

    def test_print_summary(self, capsys):
        """print_summary outputs timing table."""
        timer = StartupTimer(enabled=True)
        timer.start_phase("summary_phase")
        timer.end_phase()
        timer.print_summary()
        captured = capsys.readouterr()
        assert "STARTUP TIMING SUMMARY" in captured.out
        assert "summary_phase" in captured.out
        assert "TOTAL" in captured.out

    def test_print_summary_no_timings(self, capsys):
        """print_summary outputs nothing when no timings."""
        timer = StartupTimer(enabled=True)
        timer.print_summary()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_end_phase_without_start(self):
        """end_phase without start is a no-op."""
        timer = StartupTimer(enabled=True)
        timer.end_phase()  # Should not raise
        assert len(timer.timings) == 0

    def test_timing_in_milliseconds(self):
        """Timings are in milliseconds."""
        timer = StartupTimer(enabled=True)
        timer.start_phase("ms_test")
        time.sleep(0.05)  # 50ms
        timer.end_phase()
        # Should be around 50ms, allow wide margin
        assert timer.timings[0][1] > 10
        assert timer.timings[0][1] < 500

    def test_phase_clears_state(self):
        """After end_phase, internal state is cleared."""
        timer = StartupTimer(enabled=True)
        timer.start_phase("clear_test")
        timer.end_phase()
        assert timer._phase_name is None
        assert timer._phase_start is None
