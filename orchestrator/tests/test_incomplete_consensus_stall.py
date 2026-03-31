"""Tests for IncompleteConsensusStallCheck health check (#1471)."""

import sys
from datetime import UTC, datetime, timedelta
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
from health_checks.tier1.incomplete_consensus_stall import IncompleteConsensusStallCheck
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
        id="issue-1471",
        issue_number=1471,
        repo="owner/repo",
        branch="egg/issue-1471",
        mode="issue",
        status=status,
        current_phase=phase,
    )


def _make_concurrent_pipeline(
    phase_started_seconds_ago: float = 600,
) -> Pipeline:
    """Return a RUNNING pipeline in a concurrent implement phase."""
    pipeline = _make_pipeline()
    phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
    phase_exec.status = PipelineStatus.RUNNING
    phase_exec.started_at = datetime.now(UTC) - timedelta(seconds=phase_started_seconds_ago)

    for role in [AgentRole.CODER, AgentRole.TESTER]:
        phase_exec.containers.append(
            ContainerInfo(
                container_id=f"container-{role.value}",
                container_name=f"egg-{role.value}-issue-1471",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
        phase_exec.agents.append(
            AgentExecution(
                role=role,
                status=AgentExecutionStatus.RUNNING,
                container_id=f"container-{role.value}",
                started_at=datetime.now(UTC),
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


def _make_check(**kwargs) -> IncompleteConsensusStallCheck:
    """Create check with short defaults for testing."""
    defaults = {"grace_seconds": 60, "stall_tick_threshold": 3}
    defaults.update(kwargs)
    return IncompleteConsensusStallCheck(**defaults)


# ===========================================================================
# Tests
# ===========================================================================


class TestIncompleteConsensusStallCheck:
    def test_conforms_to_protocol(self):
        assert isinstance(IncompleteConsensusStallCheck(), HealthCheck)

    def test_healthy_when_not_running(self):
        pipeline = _make_pipeline(status=PipelineStatus.COMPLETE)
        ctx = _make_context(pipeline)
        result = _make_check().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_when_not_concurrent(self):
        pipeline = _make_pipeline()
        ctx = _make_context(pipeline)

        mock_module = MagicMock()
        mock_module.is_concurrent_execution.return_value = False
        with patch.dict("sys.modules", {"concurrent_executor": mock_module}):
            result = _make_check().run(ctx)
        assert result.status == HealthStatus.HEALTHY

    def test_healthy_within_grace_period(self):
        pipeline = _make_concurrent_pipeline(phase_started_seconds_ago=10)
        ctx = _make_context(pipeline)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True
        with patch.dict("sys.modules", {"concurrent_executor": mock_ce}):
            result = _make_check(grace_seconds=60).run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "grace period" in result.reasoning

    def test_healthy_when_consensus_complete(self):
        """No blocking agents when consensus is complete."""
        pipeline = _make_concurrent_pipeline()
        ctx = _make_context(pipeline)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        mock_tracker = MagicMock()
        mock_tracker.evaluate.return_value = {
            "is_complete": True,
            "blocking_agents": [],
        }

        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = mock_tracker

        with patch.dict(
            "sys.modules",
            {"concurrent_executor": mock_ce, "peer_consensus": mock_pc},
        ):
            result = _make_check().run(ctx)
        assert result.status == HealthStatus.HEALTHY
        assert "No blocking agents" in result.reasoning

    def test_healthy_when_blocking_agents_change(self):
        """Blocking set changed — counter resets."""
        pipeline = _make_concurrent_pipeline()
        ctx = _make_context(pipeline)
        check = _make_check(stall_tick_threshold=2)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        def _run_with_blocking(blocking):
            mock_tracker = MagicMock()
            mock_tracker.evaluate.return_value = {
                "is_complete": False,
                "blocking_agents": blocking,
            }
            mock_pc = MagicMock()
            mock_pc.get_peer_consensus_tracker.return_value = mock_tracker
            with patch.dict(
                "sys.modules",
                {"concurrent_executor": mock_ce, "peer_consensus": mock_pc},
            ):
                return check.run(ctx)

        # Tick 1: documenter blocking
        result = _run_with_blocking(["documenter"])
        assert result.status == HealthStatus.HEALTHY

        # Tick 2: now tester is blocking instead — counter resets
        result = _run_with_blocking(["tester"])
        assert result.status == HealthStatus.HEALTHY
        assert "changed" in result.reasoning

    def test_healthy_below_threshold(self):
        """Same blocking agents but not enough consecutive ticks."""
        pipeline = _make_concurrent_pipeline()
        ctx = _make_context(pipeline)
        check = _make_check(stall_tick_threshold=5)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True
        mock_tracker = MagicMock()
        mock_tracker.evaluate.return_value = {
            "is_complete": False,
            "blocking_agents": ["documenter"],
        }
        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = mock_tracker

        with patch.dict(
            "sys.modules",
            {"concurrent_executor": mock_ce, "peer_consensus": mock_pc},
        ):
            # Run 3 ticks — threshold is 5
            for _ in range(3):
                result = check.run(ctx)
            assert result.status == HealthStatus.HEALTHY
            assert "3/5 ticks" in result.reasoning

    def test_degraded_after_threshold(self):
        """Same blocking agents for enough ticks — DEGRADED."""
        pipeline = _make_concurrent_pipeline()
        ctx = _make_context(pipeline)
        check = _make_check(stall_tick_threshold=3)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True
        mock_tracker = MagicMock()
        mock_tracker.evaluate.return_value = {
            "is_complete": False,
            "blocking_agents": ["documenter"],
        }
        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = mock_tracker

        with patch.dict(
            "sys.modules",
            {"concurrent_executor": mock_ce, "peer_consensus": mock_pc},
        ):
            for _ in range(3):
                result = check.run(ctx)

        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT
        assert "documenter" in result.reasoning
        assert result.details["blocking_agents"] == ["documenter"]
        assert result.details["recovery_action"] == "escalate_to_overseer"

    def test_degraded_with_multiple_blocking_agents(self):
        """Multiple agents blocking consensus."""
        pipeline = _make_concurrent_pipeline()
        ctx = _make_context(pipeline)
        check = _make_check(stall_tick_threshold=2)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True
        mock_tracker = MagicMock()
        mock_tracker.evaluate.return_value = {
            "is_complete": False,
            "blocking_agents": ["documenter", "tester"],
        }
        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = mock_tracker

        with patch.dict(
            "sys.modules",
            {"concurrent_executor": mock_ce, "peer_consensus": mock_pc},
        ):
            for _ in range(2):
                result = check.run(ctx)

        assert result.status == HealthStatus.DEGRADED
        assert result.details["blocking_agents"] == ["documenter", "tester"]

    def test_message_fallback_when_tracker_unavailable(self):
        """Falls back to message store when tracker is not available."""
        pipeline = _make_concurrent_pipeline()
        ctx = _make_context(pipeline)
        check = _make_check(stall_tick_threshold=2)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        # No tracker
        mock_pc = MagicMock()
        mock_pc.get_peer_consensus_tracker.return_value = None

        # Message store shows documenter hasn't confirmed
        mock_msg = MagicMock()
        mock_msg.message_type = "CONSENSUS_CONFIRMED"
        mock_msg.from_role = "coder"

        mock_store = MagicMock()
        mock_store.get_messages.return_value = [mock_msg]

        mock_ms = MagicMock()
        mock_ms.get_message_store.return_value = mock_store

        mock_graph = MagicMock()
        mock_graph.all_roles.return_value = {"coder", "documenter"}

        mock_rg = MagicMock()
        mock_rg.get_review_graph_for_phase.return_value = mock_graph

        with patch.dict(
            "sys.modules",
            {
                "concurrent_executor": mock_ce,
                "peer_consensus": mock_pc,
                "message_store": mock_ms,
                "review_graph": mock_rg,
            },
        ):
            for _ in range(2):
                result = check.run(ctx)

        assert result.status == HealthStatus.DEGRADED
        assert "documenter" in result.details["blocking_agents"]

    def test_resets_after_consensus_resolves(self):
        """Counter resets when blocking agents are cleared."""
        pipeline = _make_concurrent_pipeline()
        ctx = _make_context(pipeline)
        check = _make_check(stall_tick_threshold=3)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        def _run_with_blocking(blocking, is_complete=False):
            mock_tracker = MagicMock()
            mock_tracker.evaluate.return_value = {
                "is_complete": is_complete,
                "blocking_agents": blocking,
            }
            mock_pc = MagicMock()
            mock_pc.get_peer_consensus_tracker.return_value = mock_tracker
            with patch.dict(
                "sys.modules",
                {"concurrent_executor": mock_ce, "peer_consensus": mock_pc},
            ):
                return check.run(ctx)

        # 2 ticks with documenter blocking
        _run_with_blocking(["documenter"])
        _run_with_blocking(["documenter"])

        # Consensus resolves
        result = _run_with_blocking([], is_complete=True)
        assert result.status == HealthStatus.HEALTHY

        # New blocking — counter should be reset
        result = _run_with_blocking(["documenter"])
        assert result.status == HealthStatus.HEALTHY
        # Should not be at threshold yet (only 1 tick after reset)

    def test_multi_pipeline_isolation(self):
        """Per-pipeline state prevents cross-contamination between pipelines."""
        check = _make_check(stall_tick_threshold=2)

        # Pipeline A — documenter blocking
        pipeline_a = _make_concurrent_pipeline()
        pipeline_a.id = "pipeline-A"
        ctx_a = _make_context(pipeline_a)

        # Pipeline B — tester blocking
        pipeline_b = _make_concurrent_pipeline()
        pipeline_b.id = "pipeline-B"
        ctx_b = _make_context(pipeline_b)

        mock_ce = MagicMock()
        mock_ce.is_concurrent_execution.return_value = True

        def _run(ctx, blocking):
            mock_tracker = MagicMock()
            mock_tracker.evaluate.return_value = {
                "is_complete": False,
                "blocking_agents": blocking,
            }
            mock_pc = MagicMock()
            mock_pc.get_peer_consensus_tracker.return_value = mock_tracker
            with patch.dict(
                "sys.modules",
                {"concurrent_executor": mock_ce, "peer_consensus": mock_pc},
            ):
                return check.run(ctx)

        # Tick 1: both pipelines see their first blocking set
        _run(ctx_a, ["documenter"])
        _run(ctx_b, ["tester"])

        # Tick 2: pipeline A reaches threshold, pipeline B also reaches threshold
        result_a = _run(ctx_a, ["documenter"])
        result_b = _run(ctx_b, ["tester"])

        assert result_a.status == HealthStatus.DEGRADED
        assert result_a.details["blocking_agents"] == ["documenter"]
        assert result_a.details["pipeline_id"] == "pipeline-A"

        assert result_b.status == HealthStatus.DEGRADED
        assert result_b.details["blocking_agents"] == ["tester"]
        assert result_b.details["pipeline_id"] == "pipeline-B"
