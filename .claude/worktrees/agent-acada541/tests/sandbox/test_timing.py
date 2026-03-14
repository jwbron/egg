"""Tests for sandbox/egg_lib/timing.py - StartupTimer class."""

import json
import sys
import time
from pathlib import Path

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.timing import StartupTimer, _host_timer


class TestStartupTimerDisabled:
    """Tests for StartupTimer when disabled."""

    def test_init_disabled_by_default(self):
        """Timer is disabled by default."""
        timer = StartupTimer()
        assert not timer.enabled
        assert timer.timings == []

    def test_start_phase_noop_when_disabled(self):
        """start_phase does nothing when disabled."""
        timer = StartupTimer(enabled=False)
        timer.start_phase("test")
        assert timer._phase_name is None
        assert timer._phase_start is None

    def test_end_phase_noop_when_disabled(self):
        """end_phase does nothing when disabled."""
        timer = StartupTimer(enabled=False)
        timer.end_phase()
        assert timer.timings == []

    def test_to_json_returns_empty_when_disabled(self):
        """to_json returns empty string when disabled."""
        timer = StartupTimer(enabled=False)
        assert timer.to_json() == ""

    def test_print_summary_noop_when_disabled(self, capsys):
        """print_summary does nothing when disabled."""
        timer = StartupTimer(enabled=False)
        timer.print_summary()
        captured = capsys.readouterr()
        assert captured.out == ""


class TestStartupTimerEnabled:
    """Tests for StartupTimer when enabled."""

    def test_init_enabled(self):
        """Timer can be explicitly enabled."""
        timer = StartupTimer(enabled=True)
        assert timer.enabled
        assert timer.timings == []
        assert timer.start_time > 0

    def test_start_phase(self):
        """start_phase records phase name and start time."""
        timer = StartupTimer(enabled=True)
        timer.start_phase("docker_check")
        assert timer._phase_name == "docker_check"
        assert timer._phase_start is not None

    def test_end_phase(self):
        """end_phase records elapsed time and resets state."""
        timer = StartupTimer(enabled=True)
        timer.start_phase("docker_check")
        time.sleep(0.01)
        timer.end_phase()
        assert len(timer.timings) == 1
        assert timer.timings[0][0] == "docker_check"
        assert timer.timings[0][1] > 0
        assert timer._phase_name is None
        assert timer._phase_start is None

    def test_end_phase_without_start(self):
        """end_phase does nothing when no phase started."""
        timer = StartupTimer(enabled=True)
        timer.end_phase()
        assert timer.timings == []

    def test_phase_context_manager(self):
        """phase() context manager times the block."""
        timer = StartupTimer(enabled=True)
        with timer.phase("test_phase"):
            time.sleep(0.01)
        assert len(timer.timings) == 1
        assert timer.timings[0][0] == "test_phase"
        assert timer.timings[0][1] > 0

    def test_multiple_phases(self):
        """Multiple phases are tracked independently."""
        timer = StartupTimer(enabled=True)
        with timer.phase("phase_a"):
            pass
        with timer.phase("phase_b"):
            pass
        assert len(timer.timings) == 2
        assert timer.timings[0][0] == "phase_a"
        assert timer.timings[1][0] == "phase_b"

    def test_to_json_with_data(self):
        """to_json returns valid JSON with timing data."""
        timer = StartupTimer(enabled=True)
        with timer.phase("test"):
            pass
        result = timer.to_json()
        assert result != ""
        data = json.loads(result)
        assert "timings" in data
        assert "total_time" in data
        assert len(data["timings"]) == 1
        assert data["timings"][0][0] == "test"

    def test_to_json_empty_when_no_timings(self):
        """to_json returns empty string when no timings recorded."""
        timer = StartupTimer(enabled=True)
        assert timer.to_json() == ""

    def test_print_summary(self, capsys):
        """print_summary outputs timing table."""
        timer = StartupTimer(enabled=True)
        with timer.phase("docker_check"):
            pass
        with timer.phase("gateway_start"):
            pass
        timer.print_summary()
        captured = capsys.readouterr()
        assert "STARTUP TIMING SUMMARY" in captured.out
        assert "docker_check" in captured.out
        assert "gateway_start" in captured.out
        assert "TOTAL" in captured.out

    def test_print_summary_no_timings(self, capsys):
        """print_summary does nothing with no timings."""
        timer = StartupTimer(enabled=True)
        timer.print_summary()
        captured = capsys.readouterr()
        assert captured.out == ""


class TestGlobalTimer:
    """Tests for the module-level _host_timer."""

    def test_global_timer_exists(self):
        """_host_timer is a StartupTimer instance."""
        assert isinstance(_host_timer, StartupTimer)

    def test_global_timer_disabled(self):
        """_host_timer is disabled by default."""
        assert not _host_timer.enabled
