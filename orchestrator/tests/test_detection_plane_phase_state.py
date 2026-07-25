"""Tests for phase_state.expected_duration_s + raw.runtime population (#3596, task-1-12).

Verifies that:
1. phase_state.expected_duration_s is populated from PipelineConfig
2. raw.runtime fields are populated from driver_heartbeat
3. detect_duration_drift receives expected_duration_s
4. detect_run_pipeline_thread_liveness receives runtime liveness fields
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# AC-1: phase_state.expected_duration_s populated from PipelineConfig
# ---------------------------------------------------------------------------


def _phase_state_ctx(phase: str, config=None):
    """A health context whose snapshot exercises only the phase_state builder."""
    pipeline = MagicMock()
    pipeline.id = "test-pipeline"
    pipeline.phases = {}
    pipeline.base_branch = "main"
    pipeline.repo = "owner/repo"
    pipeline.decisions = []
    pipeline.config = config

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
    return ctx


class TestExpectedDuration:
    """phase_state.expected_duration_s must be populated from PipelineConfig.

    The budget is the operator-settable per-phase BRC consensus timeout, so
    these pin the real ``resolve_consensus_timeout_minutes`` precedence rather
    than a module-local constant nobody can tune (#3596 task-1-11).
    """

    def test_expected_duration_from_per_phase_override(self):
        """A ``consensus_timeout_minutes_<phase>`` override wins, in seconds."""
        from health_checks.detection_plane import snapshot_from_health_context
        from models import PipelineConfig

        config = PipelineConfig(consensus_timeout_minutes_implement=45)
        snap = snapshot_from_health_context(_phase_state_ctx("implement", config))

        assert snap.phase_state["expected_duration_s"] == 45 * 60.0

    def test_expected_duration_from_global_override(self):
        """With no per-phase field set, the global timeout is used."""
        from health_checks.detection_plane import snapshot_from_health_context
        from models import PipelineConfig

        config = PipelineConfig(consensus_timeout_minutes=20)
        snap = snapshot_from_health_context(_phase_state_ctx("plan", config))

        assert snap.phase_state["expected_duration_s"] == 20 * 60.0

    def test_per_phase_override_beats_global(self):
        """The per-phase field takes precedence over the global one."""
        from health_checks.detection_plane import snapshot_from_health_context
        from models import PipelineConfig

        config = PipelineConfig(
            consensus_timeout_minutes=20,
            consensus_timeout_minutes_refine=75,
        )
        snap = snapshot_from_health_context(_phase_state_ctx("refine", config))

        assert snap.phase_state["expected_duration_s"] == 75 * 60.0

    def test_expected_duration_uses_phase_defaults(self):
        """With no config the shared per-phase consensus defaults are used.

        These are the same numbers the consensus timeout itself defaults to, so
        ``detect_duration_drift``'s 2× factor puts the implement bar at 12h —
        not the 2h the old hardcoded 3600s produced, which every multi-slice
        BRC implement phase tripped on every tick.
        """
        from health_checks.detection_plane import snapshot_from_health_context
        from models import PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN

        for phase, expected_minutes in PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN.items():
            snap = snapshot_from_health_context(_phase_state_ctx(phase))

            assert snap.phase_state.get("expected_duration_s") == expected_minutes * 60.0, (
                f"Phase {phase} should default to {expected_minutes}min, "
                f"got {snap.phase_state.get('expected_duration_s')}"
            )

    def test_implement_default_is_not_the_old_one_hour_constant(self):
        """Regression pin for the drift false-positive storm (#3596 task-1-11)."""
        from health_checks.detection_plane import snapshot_from_health_context

        snap = snapshot_from_health_context(_phase_state_ctx("implement"))

        assert snap.phase_state["expected_duration_s"] > 3600.0

    def test_expected_duration_null_for_unknown_phase(self):
        """phase_state.expected_duration_s must be None for unknown phases."""
        from health_checks.detection_plane import snapshot_from_health_context

        snap = snapshot_from_health_context(_phase_state_ctx("unknown_phase"))

        assert snap.phase_state.get("expected_duration_s") is None

    def test_expected_duration_null_for_terminal_phases(self):
        """Phases with no consensus budget get no drift bar at all.

        A pipeline parked in ``pr`` / ``done`` has no expected duration, and
        inventing one would make it accrue a drift finding on every tick for
        as long as it sits there.
        """
        from health_checks.detection_plane import snapshot_from_health_context
        from models import PipelineConfig

        for phase in ("pr", "done", "apply"):
            snap = snapshot_from_health_context(
                _phase_state_ctx(phase, PipelineConfig(consensus_timeout_minutes=20))
            )

            assert snap.phase_state.get("expected_duration_s") is None, (
                f"Phase {phase} has no consensus budget and must not get a drift bar"
            )


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

    def test_restart_propagation_populated_from_event_loop_supervisor(self):
        """raw.runtime.restart_propagation feeds detect_agent_restart_propagation.

        Without it the detector falls back to phase_state and stays starved
        (#3596 task-1-11).
        """
        from health_checks.detection_plane import snapshot_from_health_context

        report = {
            "deadline_exceeded": True,
            "age_s": 900.0,
            "deadline_s": 300.0,
            "role": "coder",
            "action": "propose",
            "dedupe_key": "key-1",
        }
        loop = MagicMock()
        loop.live_dedupe_keys.return_value = set()
        loop.supervisor.restart_propagation_report.return_value = report

        with patch("event_loop.get_live_event_loops", return_value=[loop]):
            snap = snapshot_from_health_context(_phase_state_ctx("implement"))

        assert snap.raw["runtime"]["restart_propagation"] == report

    def test_restart_propagation_reports_worst_overdue_loop(self):
        """Across slice loops the oldest overdue arm wins — one finding, worst case."""
        from health_checks.detection_plane import snapshot_from_health_context

        def _loop(age_s):
            loop = MagicMock()
            loop.live_dedupe_keys.return_value = set()
            loop.supervisor.restart_propagation_report.return_value = {
                "deadline_exceeded": True,
                "age_s": age_s,
                "deadline_s": 300.0,
                "role": f"role-{age_s:.0f}",
            }
            return loop

        with patch("event_loop.get_live_event_loops", return_value=[_loop(400.0), _loop(9000.0)]):
            snap = snapshot_from_health_context(_phase_state_ctx("implement"))

        assert snap.raw["runtime"]["restart_propagation"]["role"] == "role-9000"

    def test_restart_propagation_not_exceeded_when_no_loop_reports(self):
        """A clean report must not read as a finding."""
        from health_checks.detection_plane import snapshot_from_health_context

        loop = MagicMock()
        loop.live_dedupe_keys.return_value = set()
        loop.supervisor.restart_propagation_report.return_value = {"deadline_exceeded": False}

        with patch("event_loop.get_live_event_loops", return_value=[loop]):
            snap = snapshot_from_health_context(_phase_state_ctx("implement"))

        assert snap.raw["runtime"]["restart_propagation"] == {"deadline_exceeded": False}

    def test_restart_propagation_absent_when_event_loop_unavailable(self):
        """No key at all, so the detector keeps its legacy phase_state fallback.

        Writing ``deadline_exceeded: False`` here would assert a negative the
        orchestrator cannot actually observe.
        """
        from health_checks.detection_plane import snapshot_from_health_context

        with patch("event_loop.get_live_event_loops", side_effect=RuntimeError("boom")):
            snap = snapshot_from_health_context(_phase_state_ctx("implement"))

        assert "restart_propagation" not in snap.raw.get("runtime", {})

    def test_restart_propagation_survives_one_bad_loop(self):
        """One raising loop must not blind the plane to the others."""
        from health_checks.detection_plane import snapshot_from_health_context

        bad = MagicMock()
        bad.live_dedupe_keys.side_effect = RuntimeError("boom")
        good = MagicMock()
        good.live_dedupe_keys.return_value = set()
        good.supervisor.restart_propagation_report.return_value = {
            "deadline_exceeded": True,
            "age_s": 900.0,
            "deadline_s": 300.0,
            "role": "tester",
        }

        with patch("event_loop.get_live_event_loops", return_value=[bad, good]):
            snap = snapshot_from_health_context(_phase_state_ctx("implement"))

        assert snap.raw["runtime"]["restart_propagation"]["role"] == "tester"

    def test_detector_fires_on_populated_restart_propagation(self):
        """End-to-end: the populated field drives the detector's primary path."""
        from health_checks.detection_plane import EventStreamSnapshot
        from health_checks.tier1.runtime_liveness import detect_agent_restart_propagation

        snap = EventStreamSnapshot(
            snapshot_id="test:implement",
            pipeline_id="test-pipeline",
            phase="implement",
            raw={
                "runtime": {
                    "restart_propagation": {
                        "deadline_exceeded": True,
                        "age_s": 900.0,
                        "deadline_s": 300.0,
                        "role": "coder",
                    }
                }
            },
        )

        finding = detect_agent_restart_propagation(snap)

        assert finding is not None
        assert finding.finding_class == "agent_restart_propagation"
        assert finding.evidence["age_s"] == 900.0
        assert finding.evidence["deadline_s"] == 300.0
        assert finding.requires_adjudication is False


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
