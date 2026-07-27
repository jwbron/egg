"""Tests for #3665 priority 4: alert evidence bundling and snapshot enrichment.

Verifies that:
- _build_alert_evidence in concurrent_executor.py aggregates agent activity
  ages and BRC consensus state into structured evidence fields.
- _emit_supervision_alert merges evidence into OVERSEER_ALERT metadata.
- snapshot_from_health_context populates last_tool_call_age_s /
  last_heartbeat_age_s on RunningAgent from the health monitor.
- _extract_tool_calls_by_role reads from session_state_store keyed by role name.
- _context_slice_id extracts slice_id from the pipeline's phases dict.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestBuildAlertEvidence:
    """Tests for ConcurrentPhaseExecutor._build_alert_evidence (#3665 priority 4)."""

    def test_evidence_includes_agent_activity_ages(self) -> None:
        """The evidence dict should carry latest_heartbeat_age_s and
        latest_tool_call_age_s from the health monitor."""
        from concurrent_executor import ConcurrentPhaseExecutor

        executor = MagicMock(spec=ConcurrentPhaseExecutor)
        executor.pipeline = MagicMock()
        executor.pipeline.id = "issue-99"
        executor._slice_id = None

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {
            "coder": {
                "last_heartbeat_age_s": 10.0,
                "last_progress_age_s": 30.0,
                "last_activity_age_s": 5.0,
            }
        }
        hm._agents = {"coder": MagicMock(last_progress_data={})}

        with patch("health_monitor.get_health_monitor", return_value=hm):
            evidence = ConcurrentPhaseExecutor._build_alert_evidence(executor)

        assert evidence["agent_role"] == "coder"
        assert evidence["latest_heartbeat_age_s"] == 10.0
        assert evidence["latest_tool_call_age_s"] == 5.0
        assert evidence["latest_progress_age_s"] == 30.0

    def test_evidence_includes_brc_consensus_state(self) -> None:
        """The evidence dict should carry blocking_agents and consensus_state."""
        from concurrent_executor import ConcurrentPhaseExecutor

        executor = MagicMock(spec=ConcurrentPhaseExecutor)
        executor.pipeline = MagicMock()
        executor.pipeline.id = "issue-99"
        executor._slice_id = None

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {
            "coder": {
                "last_heartbeat_age_s": 10.0,
                "last_progress_age_s": None,
                "last_activity_age_s": None,
            }
        }
        hm._agents = {"coder": MagicMock(last_progress_data={})}

        tracker = MagicMock()
        tracker.evaluate.return_value = {
            "is_complete": False,
            "blocking_agents": ["reviewer_code", "tester"],
        }
        tracker._producer_phases = {"coder": "WORKING"}
        tracker._reviewer_phases = {"reviewer_code": "REVIEWING", "tester": "REVIEWING"}

        with (
            patch("health_monitor.get_health_monitor", return_value=hm),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        ):
            evidence = ConcurrentPhaseExecutor._build_alert_evidence(executor)

        assert evidence["blocking_agents"] == ["reviewer_code", "tester"]
        assert evidence["consensus_state"]["is_complete"] is False
        assert evidence["consensus_state"]["producer_phases"] == {"coder": "WORKING"}
        assert evidence["consensus_state"]["reviewer_phases"] == {
            "reviewer_code": "REVIEWING",
            "tester": "REVIEWING",
        }

    def test_evidence_is_best_effort_on_failure(self) -> None:
        """If the health monitor or consensus tracker is unavailable,
        _build_alert_evidence should still return a dict (possibly empty)
        rather than raising."""
        from concurrent_executor import ConcurrentPhaseExecutor

        executor = MagicMock(spec=ConcurrentPhaseExecutor)
        executor.pipeline = MagicMock()
        executor.pipeline.id = "issue-99"
        executor._slice_id = None

        with (
            patch("health_monitor.get_health_monitor", return_value=None),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
        ):
            evidence = ConcurrentPhaseExecutor._build_alert_evidence(executor)

        assert isinstance(evidence, dict)

    def test_evidence_picks_most_recently_active_agent(self) -> None:
        """When multiple agents are tracked, the evidence should pick the
        one with the most recent heartbeat."""
        from concurrent_executor import ConcurrentPhaseExecutor

        executor = MagicMock(spec=ConcurrentPhaseExecutor)
        executor.pipeline = MagicMock()
        executor.pipeline.id = "issue-99"
        executor._slice_id = None

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {
            "coder": {
                "last_heartbeat_age_s": 100.0,
                "last_progress_age_s": None,
                "last_activity_age_s": None,
            },
            "tester": {
                "last_heartbeat_age_s": 10.0,
                "last_progress_age_s": None,
                "last_activity_age_s": None,
            },
        }
        hm._agents = {
            "coder": MagicMock(last_progress_data={}),
            "tester": MagicMock(last_progress_data={}),
        }

        with patch("health_monitor.get_health_monitor", return_value=hm):
            evidence = ConcurrentPhaseExecutor._build_alert_evidence(executor)

        # tester has the most recent heartbeat (10s < 100s)
        assert evidence["agent_role"] == "tester"
        assert evidence["latest_heartbeat_age_s"] == 10.0


class TestEmitSupervisionAlert:
    """Tests for ConcurrentPhaseExecutor._emit_supervision_alert."""

    def test_alert_includes_evidence_in_metadata(self) -> None:
        """The OVERSEER_ALERT message metadata should carry the evidence dict."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from message_store import MessageType

        executor = MagicMock(spec=ConcurrentPhaseExecutor)
        executor.pipeline = MagicMock()
        executor.pipeline.id = "issue-99"
        executor._slice_id = None
        executor.pipeline.current_phase = MagicMock()
        executor.pipeline.current_phase.value = "implement"
        # Set up _build_alert_evidence to return real evidence
        executor._build_alert_evidence.return_value = {
            "agent_role": "coder",
            "latest_heartbeat_age_s": 10.0,
        }

        captured = {}

        def mock_add_message(msg):
            captured["msg"] = msg

        with patch("concurrent_executor.get_message_store") as mock_get_store:
            mock_store = MagicMock()
            mock_store.add_message = mock_add_message
            mock_get_store.return_value = mock_store
            ConcurrentPhaseExecutor._emit_supervision_alert(
                executor,
                anomaly="convergence_stall",
                priority="high",
                summary="test alert",
                detail="test detail",
            )

        assert "msg" in captured
        msg = captured["msg"]
        assert msg.message_type == MessageType.OVERSEER_ALERT
        assert "evidence" in msg.metadata
        assert msg.metadata["evidence"]["agent_role"] == "coder"
        assert msg.metadata["evidence"]["latest_heartbeat_age_s"] == 10.0

    def test_alert_uses_caller_evidence_when_provided(self) -> None:
        """When evidence is passed explicitly, it should be used as-is."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from message_store import MessageType

        executor = MagicMock(spec=ConcurrentPhaseExecutor)
        executor.pipeline = MagicMock()
        executor.pipeline.id = "issue-99"
        executor._slice_id = None
        executor.pipeline.current_phase = MagicMock()
        executor.pipeline.current_phase.value = "implement"

        captured = {}

        def mock_add_message(msg):
            captured["msg"] = msg

        caller_evidence = {"custom_field": "custom_value"}

        with patch("concurrent_executor.get_message_store") as mock_get_store:
            mock_store = MagicMock()
            mock_store.add_message = mock_add_message
            mock_get_store.return_value = mock_store
            ConcurrentPhaseExecutor._emit_supervision_alert(
                executor,
                anomaly="test_anomaly",
                priority="medium",
                summary="test alert",
                detail="test detail",
                evidence=caller_evidence,
            )

        assert "msg" in captured
        msg = captured["msg"]
        assert msg.message_type == MessageType.OVERSEER_ALERT
        assert msg.metadata["evidence"] == caller_evidence


class TestSnapshotEnrichment:
    """Tests for #3665 snapshot enrichment in detection_plane.py."""

    def test_running_agent_has_activity_ages(self) -> None:
        """RunningAgent should carry last_heartbeat_age_s and
        last_tool_call_age_s from the health monitor."""
        from health_checks.detection_plane import snapshot_from_health_context

        context = MagicMock()
        context.pipeline_id = "issue-99"
        context.current_phase = MagicMock()
        context.current_phase.value = "implement"
        context.live_container_ids = {"container-1"}
        context.live_container_roles = {"container-1": "coder"}
        context.phase_started_age_s = 100.0
        context.awaiting_spawn = False
        context.event_loop_owner = None
        context.lifecycle_owner = None

        pipeline = MagicMock()
        pipeline.id = "issue-99"
        pipeline.current_phase = context.current_phase
        pipeline.phases = {}
        pipeline.event_loop_owner = None
        pipeline.lifecycle_owner = None
        pipeline.base_branch = "main"
        context.pipeline = pipeline

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {
            "coder": {
                "last_heartbeat_age_s": 10.0,
                "last_progress_age_s": 30.0,
                "last_activity_age_s": 5.0,
            }
        }

        with patch("health_monitor.get_health_monitor", return_value=hm):
            snapshot = snapshot_from_health_context(context)

        assert len(snapshot.running_agents) == 1
        agent = snapshot.running_agents[0]
        assert agent.role == "coder"
        assert agent.last_heartbeat_age_s == 10.0
        assert agent.last_tool_call_age_s == 5.0

    def test_running_agent_role_uses_role_name_not_container_id(self) -> None:
        """RunningAgent.role should be the role name (from live_container_roles),
        not the container ID."""
        from health_checks.detection_plane import snapshot_from_health_context

        context = MagicMock()
        context.pipeline_id = "issue-99"
        context.current_phase = MagicMock()
        context.current_phase.value = "implement"
        context.live_container_ids = {"container-abc"}
        context.live_container_roles = {"container-abc": "tester"}
        context.phase_started_age_s = 100.0
        context.awaiting_spawn = False
        context.event_loop_owner = None
        context.lifecycle_owner = None

        pipeline = MagicMock()
        pipeline.id = "issue-99"
        pipeline.current_phase = context.current_phase
        pipeline.phases = {}
        pipeline.event_loop_owner = None
        pipeline.lifecycle_owner = None
        pipeline.base_branch = "main"
        context.pipeline = pipeline

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {}

        with patch("health_monitor.get_health_monitor", return_value=hm):
            snapshot = snapshot_from_health_context(context)

        assert len(snapshot.running_agents) == 1
        assert snapshot.running_agents[0].role == "tester"

    def test_snapshot_raw_includes_slice_id(self) -> None:
        """The snapshot's raw field should include slice_id when available."""
        from health_checks.detection_plane import snapshot_from_health_context

        context = MagicMock()
        context.pipeline_id = "issue-99"
        context.current_phase = MagicMock()
        context.current_phase.value = "implement"
        context.live_container_ids = set()
        context.live_container_roles = {}
        context.phase_started_age_s = 100.0
        context.awaiting_spawn = False
        context.event_loop_owner = None
        context.lifecycle_owner = None

        pipeline = MagicMock()
        pipeline.id = "issue-99"
        pipeline.current_phase = context.current_phase
        pipeline.phases = {"implement": MagicMock(slice_id="slice-2")}
        pipeline.event_loop_owner = None
        pipeline.lifecycle_owner = None
        pipeline.base_branch = "main"
        context.pipeline = pipeline

        hm = MagicMock()
        hm.get_agent_activity_ages.return_value = {}

        with patch("health_monitor.get_health_monitor", return_value=hm):
            snapshot = snapshot_from_health_context(context)

        assert snapshot.raw.get("slice_id") == "slice-2"


class TestExtractToolCallsByRole:
    """Tests for _extract_tool_calls_by_role in detection_plane.py."""

    def test_reads_from_session_state_store(self) -> None:
        """Should read transcripts from session_state_store.get() keyed by
        role name, not container ID."""
        from health_checks.detection_plane import _extract_tool_calls_by_role

        record = MagicMock()
        record.transcript = (
            "> Bash: ls -la /tmp\n> Bash: ls -la /tmp\n> Bash: ls -la /tmp\n> Bash: ls -la /tmp"
        )

        store = MagicMock()
        store.get.return_value = record

        with patch("session_state_store.get_session_state_store", return_value=store):
            result = _extract_tool_calls_by_role("issue-99", "slice-1", {"coder"})

        assert "coder" in result
        assert len(result["coder"]) == 4
        # Verify store.get was called with role name, not container ID
        store.get.assert_called_once_with("issue-99", "slice-1", "coder")

    def test_returns_empty_dict_when_store_unavailable(self) -> None:
        """If the session state store raises, return an empty dict."""
        from health_checks.detection_plane import _extract_tool_calls_by_role

        with patch("session_state_store.get_session_state_store", side_effect=ImportError):
            result = _extract_tool_calls_by_role("issue-99", "slice-1", {"coder"})
        assert result == {}

    def test_returns_empty_dict_when_no_roles(self) -> None:
        """If role_names is empty, return an empty dict without calling the store."""
        from health_checks.detection_plane import _extract_tool_calls_by_role

        store = MagicMock()
        with patch("session_state_store.get_session_state_store", return_value=store):
            result = _extract_tool_calls_by_role("issue-99", "slice-1", set())
        assert result == {}
        store.get.assert_not_called()

    def test_skips_roles_with_no_transcript(self) -> None:
        """Roles with no transcript should be skipped."""
        from health_checks.detection_plane import _extract_tool_calls_by_role

        record_with_transcript = MagicMock()
        record_with_transcript.transcript = "> Bash: ls\n> Bash: ls\n> Bash: ls\n> Bash: ls"
        record_without_transcript = MagicMock()
        record_without_transcript.transcript = None

        store = MagicMock()
        # Use a dict-based side_effect to handle unordered set iteration
        store.get.side_effect = lambda pipeline_id, slice_id, role: (
            record_with_transcript if role == "coder" else record_without_transcript
        )

        with patch("session_state_store.get_session_state_store", return_value=store):
            result = _extract_tool_calls_by_role("issue-99", "slice-1", {"coder", "tester"})

        assert "coder" in result
        assert "tester" not in result


class TestDetectHeartbeatStallRegistration:
    """Tests that detect_heartbeat_stall is properly registered (#3665 NACK fix)."""

    def test_detector_key_attribute(self) -> None:
        """detect_heartbeat_stall must carry detector_key and name attributes
        so it satisfies the Detector protocol and can be registered."""
        from health_checks.tier1.consensus_stall import detect_heartbeat_stall

        assert hasattr(detect_heartbeat_stall, "detector_key")
        assert detect_heartbeat_stall.detector_key == "heartbeat_stall"
        assert hasattr(detect_heartbeat_stall, "name")
        assert detect_heartbeat_stall.name == "heartbeat_stall_detector"

    def test_registered_in_default_detection_plane(self) -> None:
        """detect_heartbeat_stall must be registered in the default plane."""
        from health_checks.detection_plane import DetectionPlane
        from health_checks.tier1.consensus_stall import detect_heartbeat_stall

        plane = DetectionPlane.default()
        assert "heartbeat_stall" in plane.detectors
        assert plane.detectors["heartbeat_stall"] is detect_heartbeat_stall
