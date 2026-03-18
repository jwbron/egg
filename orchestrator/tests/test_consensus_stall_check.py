"""Tests for ConsensusStallCheck health check."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from health_checks.context import PipelineHealthContext
from health_checks.tier1.consensus_stall import ConsensusStallCheck
from health_checks.types import (
    HealthAction,
    HealthCheck,
    HealthStatus,
)
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    status: PipelineStatus = PipelineStatus.RUNNING,
    phase: PipelinePhase = PipelinePhase.IMPLEMENT,
) -> Pipeline:
    return Pipeline(
        id="issue-1014",
        issue_number=1014,
        repo="owner/repo",
        branch="egg/issue-1014",
        mode="issue",
        status=status,
        current_phase=phase,
    )


def _make_concurrent_pipeline(
    phase_started_seconds_ago: float = 120,
) -> Pipeline:
    """Return a RUNNING pipeline in a concurrent implement phase."""
    pipeline = _make_pipeline()
    phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
    phase_exec.status = PipelineStatus.RUNNING
    phase_exec.started_at = datetime.utcnow() - timedelta(seconds=phase_started_seconds_ago)

    for role in [AgentRole.CODER, AgentRole.TESTER]:
        phase_exec.containers.append(
            ContainerInfo(
                container_id=f"container-{role.value}",
                container_name=f"egg-{role.value}-issue-1014",
                status=ContainerStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
        )
        phase_exec.agents.append(
            AgentExecution(
                role=role,
                status=AgentExecutionStatus.RUNNING,
                container_id=f"container-{role.value}",
                started_at=datetime.utcnow(),
            )
        )
    return pipeline


def _make_context(
    pipeline: Pipeline,
    trigger: str = "runtime_tick",
) -> PipelineHealthContext:
    return PipelineHealthContext(
        pipeline=pipeline,
        repo_path=Path("/tmp/test-repo"),
        trigger=trigger,
        docker_client=MagicMock(),
    )


# ===========================================================================
# Tests: ConsensusStallCheck
# ===========================================================================


class TestConsensusStallCheck:
    def test_conforms_to_protocol(self):
        assert isinstance(ConsensusStallCheck(), HealthCheck)

    def test_healthy_when_not_running(self):
        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        ctx = _make_context(pipeline)
        result = ConsensusStallCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_when_not_concurrent(self):
        """Non-concurrent phases should be skipped."""
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)

        mock_module = MagicMock()
        mock_module.is_concurrent_execution.return_value = False
        with patch.dict("sys.modules", {"concurrent_executor": mock_module}):
            result = ConsensusStallCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_when_phase_not_running(self):
        """Phase execution not started yet."""
        pipeline = _make_pipeline()
        # Don't set phase_exec to RUNNING — leave it PENDING
        ctx = _make_context(pipeline)

        mock_module = MagicMock()
        mock_module.is_concurrent_execution.return_value = True
        with patch.dict("sys.modules", {"concurrent_executor": mock_module}):
            result = ConsensusStallCheck().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_within_grace_period(self):
        """Phase started recently — within grace period."""
        pipeline = _make_concurrent_pipeline(phase_started_seconds_ago=10)
        ctx = _make_context(pipeline)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True
        with patch.dict("sys.modules", {"concurrent_executor": mock_ce}):
            result = ConsensusStallCheck(consensus_stall_grace_seconds=60).run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "grace period" in result.reasoning

    def test_healthy_when_consensus_not_complete(self):
        """Consensus not yet reached — no stall."""
        pipeline = _make_concurrent_pipeline(phase_started_seconds_ago=120)
        ctx = _make_context(pipeline)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        mock_tracker = MagicMock()
        mock_tracker.evaluate.return_value = {"is_complete": False}

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = mock_tracker

        with patch.dict(
            "sys.modules",
            {
                "concurrent_executor": mock_ce,
                "peer_consensus": mock_pc,
            },
        ):
            result = ConsensusStallCheck(consensus_stall_grace_seconds=60).run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "not complete" in result.reasoning

    def test_degraded_when_consensus_complete_beyond_grace(self):
        """Consensus complete and past grace period — DEGRADED with recovery action."""
        pipeline = _make_concurrent_pipeline(phase_started_seconds_ago=120)
        ctx = _make_context(pipeline)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        mock_tracker = MagicMock()
        mock_tracker.evaluate.return_value = {"is_complete": True}

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = mock_tracker

        with patch.dict(
            "sys.modules",
            {
                "concurrent_executor": mock_ce,
                "peer_consensus": mock_pc,
            },
        ):
            result = ConsensusStallCheck(consensus_stall_grace_seconds=60).run(ctx)

        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT
        assert result.details["recovery_action"] == "drive_phase_transition"
        assert result.details["pipeline_id"] == "issue-1014"
        assert "tracker_reconstructed" not in result.details

    def test_degraded_no_reconstruction_side_effect(self):
        """Health check does not call reconstruct_tracker_from_messages (moved to recovery)."""
        pipeline = _make_concurrent_pipeline(phase_started_seconds_ago=120)
        ctx = _make_context(pipeline)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None

        # Message fallback shows consensus complete
        mock_msg_store = MagicMock()
        mock_message = MagicMock()
        mock_message.message_type = "CONSENSUS_CONFIRMED"
        mock_message.from_role = "coder"
        mock_msg_store.get_messages.return_value = [mock_message]

        mock_msg_store_mod = MagicMock()
        mock_msg_store_mod.get_message_store.return_value = mock_msg_store

        mock_graph = MagicMock()
        mock_graph.all_roles.return_value = {"coder"}
        mock_graph_mod = MagicMock()
        mock_graph_mod.get_review_graph_for_phase.return_value = mock_graph

        with patch.dict(
            "sys.modules",
            {
                "concurrent_executor": mock_ce,
                "peer_consensus": mock_pc,
                "message_store": mock_msg_store_mod,
                "review_graph": mock_graph_mod,
            },
        ):
            result = ConsensusStallCheck(consensus_stall_grace_seconds=60).run(ctx)

        assert result.status == HealthStatus.DEGRADED
        # Health check should NOT have called reconstruct (no side effects)
        mock_pc.reconstruct_tracker_from_messages.assert_not_called()
        assert "tracker_reconstructed" not in result.details

    def test_idempotent(self):
        """Running the check twice produces the same result without side effects."""
        pipeline = _make_concurrent_pipeline(phase_started_seconds_ago=120)
        ctx = _make_context(pipeline)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        mock_tracker = MagicMock()
        mock_tracker.evaluate.return_value = {"is_complete": True}

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = mock_tracker

        with patch.dict(
            "sys.modules",
            {
                "concurrent_executor": mock_ce,
                "peer_consensus": mock_pc,
            },
        ):
            check = ConsensusStallCheck(consensus_stall_grace_seconds=60)
            result1 = check.run(ctx)
            result2 = check.run(ctx)

        assert result1.status == result2.status == HealthStatus.DEGRADED
        assert result1.details == result2.details

    def test_attributes(self):
        check = ConsensusStallCheck()
        assert check.name == "consensus_stall"
        assert check.tier.value == "tier1"
        from health_checks.types import HealthTrigger

        assert HealthTrigger.RUNTIME_TICK in check.triggers
        assert HealthTrigger.ON_DEMAND in check.triggers

    def test_custom_grace_seconds(self):
        check = ConsensusStallCheck(consensus_stall_grace_seconds=300)
        assert check._grace_seconds == 300


# ===========================================================================
# Tests: _handle_consensus_stall_recovery (container_monitor)
# ===========================================================================


def _make_degraded_result(pipeline_id: str = "issue-1014", phase: str = "implement"):
    """Create a DEGRADED health result for consensus stall."""
    from health_checks.types import HealthAction, HealthResult, HealthStatus, HealthTier

    return HealthResult(
        status=HealthStatus.DEGRADED,
        check_name="consensus_stall",
        tier=HealthTier.PROGRAMMATIC,
        reasoning="BRC consensus is complete but phase execution has not advanced.",
        action=HealthAction.ALERT,
        details={
            "recovery_action": "drive_phase_transition",
            "pipeline_id": pipeline_id,
            "phase": phase,
        },
    )


def _make_healthy_result():
    from health_checks.types import HealthResult, HealthStatus, HealthTier

    return HealthResult(
        status=HealthStatus.HEALTHY,
        check_name="consensus_stall",
        tier=HealthTier.PROGRAMMATIC,
        reasoning="No stall.",
    )


def _make_monitor():
    """Create a ContainerMonitor with a mocked Docker client."""
    from container_monitor import ContainerMonitor

    mock_docker = MagicMock()
    return ContainerMonitor(docker_client=mock_docker)


class TestHandleConsensusStallRecovery:
    def test_skips_non_consensus_results(self):
        """Non-consensus results are ignored."""
        from health_checks.types import HealthResult, HealthStatus, HealthTier

        monitor = _make_monitor()
        other_result = HealthResult(
            status=HealthStatus.DEGRADED,
            check_name="container_liveness",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="Some other check.",
        )
        mock_store = MagicMock()
        pipeline = _make_concurrent_pipeline()
        # Should not raise or call save_pipeline
        monitor._handle_consensus_stall_recovery([other_result], pipeline, mock_store)
        mock_store.save_pipeline.assert_not_called()

    def test_skips_healthy_consensus_results(self):
        """HEALTHY consensus results are ignored."""
        monitor = _make_monitor()
        mock_store = MagicMock()
        pipeline = _make_concurrent_pipeline()
        monitor._handle_consensus_stall_recovery([_make_healthy_result()], pipeline, mock_store)
        mock_store.save_pipeline.assert_not_called()

    def test_tracker_reconstruction_success_skips_aggressive(self):
        """When tracker reconstruction succeeds, aggressive recovery is not performed."""
        monitor = _make_monitor()
        mock_store = MagicMock()
        pipeline = _make_concurrent_pipeline()
        result = _make_degraded_result()

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None
        mock_reconstructed = MagicMock()
        mock_pc.reconstruct_tracker_from_messages.return_value = mock_reconstructed

        mock_graph_mod = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": mock_pc,
                "review_graph": mock_graph_mod,
            },
        ):
            monitor._handle_consensus_stall_recovery([result], pipeline, mock_store)

        # Tracker was reconstructed — save_pipeline should NOT be called
        mock_store.save_pipeline.assert_not_called()

    def test_aggressive_recovery_sets_completed_at(self):
        """Aggressive recovery sets completed_at on agents and phase."""
        monitor = _make_monitor()
        pipeline = _make_concurrent_pipeline()
        result = _make_degraded_result()

        # Mock store: load_pipeline returns a fresh copy
        fresh_pipeline = _make_concurrent_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = fresh_pipeline

        # Tracker reconstruction fails
        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None
        mock_pc.reconstruct_tracker_from_messages.return_value = None

        mock_graph_mod = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": mock_pc,
                "review_graph": mock_graph_mod,
            },
        ):
            monitor._handle_consensus_stall_recovery([result], pipeline, mock_store)

        # Verify save was called with expected_version
        mock_store.save_pipeline.assert_called_once()
        call_kwargs = mock_store.save_pipeline.call_args
        assert call_kwargs[1]["expected_version"] == fresh_pipeline.version

        # Verify completed_at is set on all agents and phase
        phase_exec = fresh_pipeline.phases.get("implement")
        assert phase_exec is not None
        assert phase_exec.status == PipelineStatus.COMPLETE
        assert phase_exec.completed_at is not None
        for agent in phase_exec.agents:
            assert agent.status == AgentExecutionStatus.COMPLETE
            assert agent.completed_at is not None

    def test_aggressive_recovery_uses_optimistic_locking(self):
        """save_pipeline is called with expected_version for optimistic locking."""
        monitor = _make_monitor()
        pipeline = _make_concurrent_pipeline()
        result = _make_degraded_result()

        fresh_pipeline = _make_concurrent_pipeline()
        fresh_pipeline.version = 5
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = fresh_pipeline

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None
        mock_pc.reconstruct_tracker_from_messages.return_value = None
        mock_graph_mod = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": mock_pc,
                "review_graph": mock_graph_mod,
            },
        ):
            monitor._handle_consensus_stall_recovery([result], pipeline, mock_store)

        mock_store.save_pipeline.assert_called_once()
        assert mock_store.save_pipeline.call_args[1]["expected_version"] == 5

    def test_version_conflict_handled_gracefully(self):
        """VersionConflictError is caught and handled without raising."""
        from state_store import VersionConflictError

        monitor = _make_monitor()
        pipeline = _make_concurrent_pipeline()
        result = _make_degraded_result()

        fresh_pipeline = _make_concurrent_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = fresh_pipeline
        mock_store.save_pipeline.side_effect = VersionConflictError("conflict")

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None
        mock_pc.reconstruct_tracker_from_messages.return_value = None
        mock_graph_mod = MagicMock()

        # Should not raise
        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": mock_pc,
                "review_graph": mock_graph_mod,
            },
        ):
            monitor._handle_consensus_stall_recovery([result], pipeline, mock_store)

    def test_skips_when_phase_already_transitioned(self):
        """If phase already completed, aggressive recovery is skipped."""
        monitor = _make_monitor()
        pipeline = _make_concurrent_pipeline()
        result = _make_degraded_result()

        # Fresh pipeline where phase is already COMPLETE
        fresh_pipeline = _make_concurrent_pipeline()
        phase_exec = fresh_pipeline.phases.get("implement")
        phase_exec.status = PipelineStatus.COMPLETE
        phase_exec.completed_at = datetime.utcnow()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = fresh_pipeline

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None
        mock_pc.reconstruct_tracker_from_messages.return_value = None
        mock_graph_mod = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "peer_consensus": mock_pc,
                "review_graph": mock_graph_mod,
            },
        ):
            monitor._handle_consensus_stall_recovery([result], pipeline, mock_store)

        # save_pipeline should NOT be called since phase already transitioned
        mock_store.save_pipeline.assert_not_called()
