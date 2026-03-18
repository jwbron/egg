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

    @patch("health_checks.tier1.consensus_stall.is_concurrent_execution", create=True)
    def test_healthy_when_not_concurrent(self, mock_is_concurrent):
        """Non-concurrent phases should be skipped."""
        mock_is_concurrent.return_value = False
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)

        with patch.dict("sys.modules", {"concurrent_executor": MagicMock()}):
            with patch(
                "health_checks.tier1.consensus_stall.ConsensusStallCheck.run",
                wraps=ConsensusStallCheck().run,
            ):
                check = ConsensusStallCheck()
                # Patch the import inside the run method
                mock_module = MagicMock()
                mock_module.is_concurrent_execution.return_value = False
                with patch.dict("sys.modules", {"concurrent_executor": mock_module}):
                    result = check.run(ctx)
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
        mock_pc.reconstruct_tracker_from_messages.return_value = mock_tracker

        with patch.dict(
            "sys.modules",
            {
                "concurrent_executor": mock_ce,
                "peer_consensus": mock_pc,
                "review_graph": MagicMock(),
            },
        ):
            result = ConsensusStallCheck(consensus_stall_grace_seconds=60).run(ctx)

        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT
        assert result.details["recovery_action"] == "drive_phase_transition"
        assert result.details["pipeline_id"] == "issue-1014"

    def test_degraded_with_tracker_reconstruction_success(self):
        """When tracker is missing but reconstruction succeeds."""
        pipeline = _make_concurrent_pipeline(phase_started_seconds_ago=120)
        ctx = _make_context(pipeline)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        # No existing tracker, but messages show consensus complete
        call_count = [0]

        def mock_get_tracker(pid):
            call_count[0] += 1
            # First call (in _check_consensus_complete) returns None
            # Second call (in _attempt_tracker_reconstruction) also returns None
            return None

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.side_effect = mock_get_tracker

        # Reconstruction succeeds
        reconstructed = MagicMock()
        mock_pc.reconstruct_tracker_from_messages.return_value = reconstructed

        mock_msg_store = MagicMock()
        mock_message = MagicMock()
        mock_message.message_type = "CONSENSUS_CONFIRMED"
        mock_message.from_role = "coder"
        mock_msg_store_mod = MagicMock()
        mock_msg_store_mod.get_message_store.return_value = mock_msg_store

        mock_graph = MagicMock()
        mock_graph_mod = MagicMock()
        mock_graph_mod.get_review_graph_for_phase.return_value = mock_graph

        # Make message fallback show consensus complete
        mock_msg_store.get_messages.return_value = [mock_message]
        mock_graph.all_roles.return_value = {"coder"}

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
        assert result.details["tracker_reconstructed"] is True

    def test_degraded_with_tracker_reconstruction_failure(self):
        """When tracker is missing and reconstruction also fails."""
        pipeline = _make_concurrent_pipeline(phase_started_seconds_ago=120)
        ctx = _make_context(pipeline)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None
        mock_pc.reconstruct_tracker_from_messages.return_value = None

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
        assert result.details["tracker_reconstructed"] is False

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
        mock_pc.reconstruct_tracker_from_messages.return_value = mock_tracker

        with patch.dict(
            "sys.modules",
            {
                "concurrent_executor": mock_ce,
                "peer_consensus": mock_pc,
                "review_graph": MagicMock(),
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
