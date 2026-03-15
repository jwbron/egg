"""Tests for ConcurrentPhaseExecutor with BRC protocol integration.

Verifies that the concurrent executor correctly sets up BRC environment
variables, creates the peer consensus tracker, and integrates review
graph information into agent configuration.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus


def _make_pipeline(pipeline_id: str = "issue-999") -> Pipeline:
    """Create a test pipeline with concurrent execution enabled."""
    config = PipelineConfig()
    try:
        config.concurrent_execution = True  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        config.__dict__["concurrent_execution"] = True

    return Pipeline(
        id=pipeline_id,
        repo="test/repo",
        issue_number=999,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


class TestBRCEnvironmentVariables:
    """Test that BRC-specific env vars are set for concurrent agents."""

    def test_producer_gets_brc_role_type(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        env = executor.get_agent_env(AgentRole.CODER)
        assert "EGG_BRC_ROLE_TYPE" in env
        assert "producer" in env["EGG_BRC_ROLE_TYPE"]

    def test_reviewer_gets_brc_role_type(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        env = executor.get_agent_env(AgentRole.REVIEWER_CODE)
        assert "EGG_BRC_ROLE_TYPE" in env
        assert "reviewer" in env["EGG_BRC_ROLE_TYPE"]

    def test_producer_gets_reviewer_list(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        env = executor.get_agent_env(AgentRole.CODER)
        assert "EGG_BRC_REVIEWERS" in env
        reviewers = env["EGG_BRC_REVIEWERS"].split(",")
        assert len(reviewers) > 0

    def test_reviewer_gets_producer_list(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        env = executor.get_agent_env(AgentRole.REVIEWER_CODE)
        assert "EGG_BRC_PRODUCERS" in env
        producers = env["EGG_BRC_PRODUCERS"].split(",")
        assert "coder" in producers

    def test_concurrent_mode_env_set(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        env = executor.get_agent_env(AgentRole.CODER)
        assert env["EGG_CONCURRENT_MODE"] == "true"

    def test_dual_role_tester_gets_both(self):
        """Tester is both producer and reviewer — should have both role types."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole
        from review_graph import get_default_implement_graph

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        graph = get_default_implement_graph()

        if graph.is_dual_role("tester"):
            env = executor.get_agent_env(AgentRole.TESTER)
            # Dual role should have both producer and reviewer
            assert "producer" in env.get("EGG_BRC_ROLE_TYPE", "")
            assert "reviewer" in env.get("EGG_BRC_ROLE_TYPE", "")


class TestAgentRoles:
    """Test agent role configuration for concurrent execution."""

    def test_implement_phase_roles(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        roles = executor.get_agent_roles()
        role_names = [r.value for r in roles]

        assert "coder" in role_names
        assert "tester" in role_names
        assert "reviewer_code" in role_names
        assert "reviewer_contract" in role_names

    def test_refine_phase_roles(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        pipeline.current_phase = PipelinePhase.REFINE
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        roles = executor.get_agent_roles()
        role_names = [r.value for r in roles]

        assert "refiner" in role_names
        assert "reviewer_refine" in role_names
        assert "reviewer_agent_design" in role_names
        assert "coder" not in role_names

    def test_plan_phase_roles(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        pipeline.current_phase = PipelinePhase.PLAN
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        roles = executor.get_agent_roles()
        role_names = [r.value for r in roles]

        assert "architect" in role_names
        assert "task_planner" in role_names
        assert "risk_analyst" in role_names
        assert "reviewer_plan" in role_names
        assert "coder" not in role_names

    def test_worktree_branch_from_pipeline(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        pipeline.branch = "egg/issue-999"
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        branch = executor.get_worktree_branch(AgentRole.CODER)
        assert branch == "egg/issue-999"


class TestSpawnCreatesTracker:
    """Test that spawn_all creates a PeerConsensusTracker."""

    def test_spawn_registers_agents(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        mock_spawn = MagicMock()
        mock_spawn.return_value = MagicMock(container_id="test-container")

        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        with (
            patch("concurrent_executor.create_peer_consensus_tracker") as mock_tracker,
            patch("concurrent_executor.emit_event"),
        ):
            mock_tracker_instance = MagicMock()
            mock_tracker.return_value = mock_tracker_instance

            executor.spawn_all()

            # Tracker should be created with the review graph
            mock_tracker.assert_called_once()
            # All agents should be registered
            assert mock_tracker_instance.register_agent.call_count == len(
                executor.get_agent_roles()
            )


class TestReviewGraphIntegration:
    """Test that the review graph is properly integrated."""

    def test_default_implement_graph_used(self):
        from review_graph import get_default_implement_graph, get_review_graph_for_phase

        graph = get_review_graph_for_phase("implement")
        default = get_default_implement_graph()

        # Should have the same edges
        assert len(graph.edges) == len(default.edges)

    def test_review_graph_has_coder_reviewers(self):
        from review_graph import get_default_implement_graph

        graph = get_default_implement_graph()
        reviewers = graph.reviewers_for("coder")

        # Coder should have at least reviewer_code and checker as reviewers
        assert "reviewer_code" in reviewers
        assert "checker" in reviewers

    def test_refine_graph_has_correct_edges(self):
        from review_graph import get_default_refine_graph, get_review_graph_for_phase

        graph = get_default_refine_graph()

        # refiner is the sole producer
        assert graph.is_producer("refiner")
        # Both reviewers review refiner
        reviewers = graph.reviewers_for("refiner")
        assert "reviewer_refine" in reviewers
        assert "reviewer_agent_design" in reviewers
        # Both are critical
        assert "reviewer_refine" in graph.critical_reviewers_for("refiner")
        assert "reviewer_agent_design" in graph.critical_reviewers_for("refiner")

        # Phase lookup returns same structure
        phase_graph = get_review_graph_for_phase("refine")
        assert len(phase_graph.edges) == len(graph.edges)

    def test_plan_graph_has_correct_edges(self):
        from review_graph import get_default_plan_graph, get_review_graph_for_phase

        graph = get_default_plan_graph()

        # Three producers
        assert graph.is_producer("architect")
        assert graph.is_producer("task_planner")
        assert graph.is_producer("risk_analyst")
        # reviewer_plan reviews all three
        assert "reviewer_plan" in graph.reviewers_for("architect")
        assert "reviewer_plan" in graph.reviewers_for("task_planner")
        assert "reviewer_plan" in graph.reviewers_for("risk_analyst")
        # architect and task_planner are critical, risk_analyst is advisory
        assert "reviewer_plan" in graph.critical_reviewers_for("architect")
        assert "reviewer_plan" in graph.critical_reviewers_for("task_planner")
        assert "reviewer_plan" in graph.advisory_reviewers_for("risk_analyst")

        # Phase lookup returns same structure
        phase_graph = get_review_graph_for_phase("plan")
        assert len(phase_graph.edges) == len(graph.edges)
