"""Tests for phase_state.expected_duration_s + raw.runtime population (#3596, task-1-12).

Verifies that:
1. phase_state.expected_duration_s is populated from PipelineConfig
2. raw.runtime fields are populated from driver_heartbeat
3. detect_duration_drift receives expected_duration_s
4. detect_run_pipeline_thread_liveness receives runtime liveness fields
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AC-1: phase_state.expected_duration_s populated from PipelineConfig
# ---------------------------------------------------------------------------


class TestExpectedDuration:
    """phase_state.expected_duration_s must be populated from PipelineConfig."""

    def test_expected_duration_populated_from_config(self):
        """phase_state.expected_duration_s must be populated from pipeline config."""
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "implement"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        snap = snapshot_from_health_context(ctx)

        assert hasattr(snap, "phase_state")
        assert snap.phase_state is not None
        assert "expected_duration_s" in snap.phase_state
        # Should have a default for "implement" phase
        assert snap.phase_state["expected_duration_s"] is not None

    def test_expected_duration_uses_phase_defaults(self):
        """phase_state.expected_duration_s must use phase-specific defaults
        when no explicit config is available."""
        from health_checks.detection_plane import snapshot_from_health_context

        for phase, expected_default in [
            ("refine", 600.0),
            ("plan", 900.0),
            ("apply", 300.0),
            ("implement", 3600.0),
        ]:
            pipeline = MagicMock()
            pipeline.id = "test-pipeline"
            pipeline.phases = {}
            pipeline.base_branch = "main"
            pipeline.repo = "owner/repo"
            pipeline.decisions = []
            # Set config to None so defaults are used
            pipeline.config = None

            ctx = MagicMock()
            ctx.pipeline = pipeline
            ctx.pipeline_id = "test-pipeline"
            ctx.current_phase = MagicMock()
            ctx.current_phase.value = phase
            ctx.phase_started_age_s = 3600.0
            ctx.awaiting_spawn = False
            ctx.event_loop_owner = None
            ctx.lifecycle_owner = None
            ctx.live_container_ids = set()
            ctx.repo_path = "/tmp/repo"

            snap = snapshot_from_health_context(ctx)

            assert snap.phase_state.get("expected_duration_s") == expected_default, (
                f"Phase {phase} should have default duration {expected_default}s, "
                f"got {snap.phase_state.get('expected_duration_s')}"
            )

    def test_expected_duration_null_for_unknown_phase(self):
        """phase_state.expected_duration_s must be None for unknown phases."""
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []
        pipeline.config = None

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "unknown_phase"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        snap = snapshot_from_health_context(ctx)

        assert snap.phase_state.get("expected_duration_s") is None


# ---------------------------------------------------------------------------
# AC-2: raw.runtime fields populated from driver_heartbeat
# ---------------------------------------------------------------------------


class TestRawRuntime:
    """raw.runtime fields must be populated from driver_heartbeat."""

    def test_raw_runtime_has_run_pipeline_thread_alive(self):
        """raw.runtime must include run_pipeline_thread_alive."""
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "implement"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        with patch("driver_heartbeat.tick_age_seconds", return_value=5.0):
            with patch("driver_heartbeat.spawn_age_seconds", return_value=30.0):
                snap = snapshot_from_health_context(ctx)

        assert hasattr(snap, "raw")
        assert snap.raw is not None
        assert "runtime" in snap.raw
        assert "run_pipeline_thread_alive" in snap.raw["runtime"]
        assert snap.raw["runtime"]["run_pipeline_thread_alive"] is True

    def test_raw_runtime_has_thread_last_tick_age_s(self):
        """raw.runtime must include thread_last_tick_age_s."""
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "implement"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        with patch("driver_heartbeat.tick_age_seconds", return_value=5.0):
            with patch("driver_heartbeat.spawn_age_seconds", return_value=30.0):
                snap = snapshot_from_health_context(ctx)

        assert snap.raw.get("runtime", {}).get("thread_last_tick_age_s") == 5.0

    def test_raw_runtime_has_spawn_age_s(self):
        """raw.runtime must include spawn_age_s."""
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "implement"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        with patch("driver_heartbeat.tick_age_seconds", return_value=5.0):
            with patch("driver_heartbeat.spawn_age_seconds", return_value=30.0):
                snap = snapshot_from_health_context(ctx)

        assert snap.raw.get("runtime", {}).get("spawn_age_s") == 30.0

    def test_raw_runtime_degrades_when_driver_heartbeat_unavailable(self):
        """raw.runtime must degrade gracefully when driver_heartbeat is unavailable."""
        from health_checks.detection_plane import snapshot_from_health_context

        pipeline = MagicMock()
        pipeline.id = "test-pipeline"
        pipeline.phases = {}
        pipeline.base_branch = "main"
        pipeline.repo = "owner/repo"
        pipeline.decisions = []

        ctx = MagicMock()
        ctx.pipeline = pipeline
        ctx.pipeline_id = "test-pipeline"
        ctx.current_phase = MagicMock()
        ctx.current_phase.value = "implement"
        ctx.phase_started_age_s = 3600.0
        ctx.awaiting_spawn = False
        ctx.event_loop_owner = None
        ctx.lifecycle_owner = None
        ctx.live_container_ids = set()
        ctx.repo_path = "/tmp/repo"

        with patch("driver_heartbeat.tick_age_seconds", side_effect=ImportError):
            with patch("driver_heartbeat.spawn_age_seconds", side_effect=ImportError):
                snap = snapshot_from_health_context(ctx)

        # raw.runtime should be empty dict or absent, not crash
        assert hasattr(snap, "raw")
        assert snap.raw is not None


# ---------------------------------------------------------------------------
# AC-3: detect_duration_drift receives expected_duration_s
# ---------------------------------------------------------------------------


class TestDurationDriftDetector:
    """detect_duration_drift must receive expected_duration_s."""

    def test_detect_duration_drift_fires_on_drift(self):
        """detect_duration_drift must fire when phase exceeds expected duration
        by more than the drift factor (default 2.0×)."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.runtime_liveness import detect_duration_drift

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={
                "status": "RUNNING",
                "started_age_s": 9000,  # 2.5× the 3600s expected — exceeds 2.0× factor
                "expected_duration_s": 3600.0,
            },
            running_agents=(),
        )

        finding = detect_duration_drift(snap)

        assert finding is not None, "detect_duration_drift must fire on drift"
        assert finding.finding_class == "duration_drift"

    def test_detect_duration_drift_silent_within_duration(self):
        """detect_duration_drift must not fire when within expected duration."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.runtime_liveness import detect_duration_drift

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={
                "status": "RUNNING",
                "started_age_s": 600,  # 10 minutes — within 3600s
                "expected_duration_s": 3600.0,
            },
            running_agents=(),
        )

        finding = detect_duration_drift(snap)

        assert finding is None, "detect_duration_drift must not fire within duration"

    def test_detect_duration_drift_silent_without_expected_duration(self):
        """detect_duration_drift must not fire when expected_duration_s is absent."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.runtime_liveness import detect_duration_drift

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={
                "status": "RUNNING",
                "started_age_s": 7200,
                "expected_duration_s": None,
            },
            running_agents=(),
        )

        finding = detect_duration_drift(snap)

        assert finding is None


# ---------------------------------------------------------------------------
# AC-4: detect_run_pipeline_thread_liveness receives runtime liveness fields
# ---------------------------------------------------------------------------


class TestRunPipelineThreadLivenessDetector:
    """detect_run_pipeline_thread_liveness must receive runtime liveness fields."""

    def test_detect_run_pipeline_thread_liveness_fires_on_dead_thread(self):
        """detect_run_pipeline_thread_liveness must fire when the driver thread
        is not alive (no recent heartbeat)."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.runtime_liveness import detect_run_pipeline_thread_liveness

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            raw={
                "runtime": {
                    "run_pipeline_thread_alive": False,
                    "thread_last_tick_age_s": 600,  # 10 minutes since last tick
                }
            },
        )

        finding = detect_run_pipeline_thread_liveness(snap)

        assert finding is not None, "detect_run_pipeline_thread_liveness must fire on dead thread"
        assert finding.finding_class == "runtime_thread_dead"

    def test_detect_run_pipeline_thread_liveness_silent_on_alive_thread(self):
        """detect_run_pipeline_thread_liveness must not fire when thread is alive."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.runtime_liveness import detect_run_pipeline_thread_liveness

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            raw={
                "runtime": {
                    "run_pipeline_thread_alive": True,
                    "thread_last_tick_age_s": 5,
                }
            },
        )

        finding = detect_run_pipeline_thread_liveness(snap)

        assert finding is None

    def test_detect_run_pipeline_thread_liveness_silent_without_runtime(self):
        """detect_run_pipeline_thread_liveness must not fire when raw.runtime is absent."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.runtime_liveness import detect_run_pipeline_thread_liveness

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            phase_state={"status": "RUNNING"},
            running_agents=(),
            raw={},
        )

        finding = detect_run_pipeline_thread_liveness(snap)

        assert finding is None
