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
        assert "documenter" in role_names
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
        # reviewer_agent_design is egg-repo-only, not included for test/repo
        assert "reviewer_agent_design" not in role_names
        assert "coder" not in role_names

    def test_refine_phase_roles_egg_repo(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        pipeline.current_phase = PipelinePhase.REFINE
        pipeline.repo = "jwbron/egg"
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        roles = executor.get_agent_roles()
        role_names = [r.value for r in roles]

        assert "refiner" in role_names
        assert "reviewer_refine" in role_names
        assert "reviewer_agent_design" in role_names

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


class TestRolesOverride:
    """Test that the roles override is respected by the executor."""

    def test_get_agent_roles_with_override(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        override = [AgentRole.CODER, AgentRole.REVIEWER_CODE]
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), roles=override)

        roles = executor.get_agent_roles()
        assert roles == [AgentRole.CODER, AgentRole.REVIEWER_CODE]

    def test_get_agent_roles_without_override_uses_defaults(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        roles = executor.get_agent_roles()
        # Without override, should return all default implement roles
        assert len(roles) > 2

    def test_spawn_all_with_roles_override_only_spawns_overridden_roles(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        mock_spawn = MagicMock()
        mock_spawn.return_value = MagicMock(container_id="test-container")

        override = [AgentRole.CODER, AgentRole.REVIEWER_CODE]
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn, roles=override)

        with (
            patch("concurrent_executor.create_peer_consensus_tracker") as mock_tracker,
            patch("concurrent_executor.emit_event"),
        ):
            mock_tracker_instance = MagicMock()
            mock_tracker.return_value = mock_tracker_instance

            executor.spawn_all()

            # Only 2 agents should be spawned and registered (coder + reviewer_code)
            assert mock_spawn.call_count == 2
            assert mock_tracker_instance.register_agent.call_count == 2
            registered_roles = [
                call.args[0] for call in mock_tracker_instance.register_agent.call_args_list
            ]
            assert set(registered_roles) == {"coder", "reviewer_code"}


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

        # Coder should have at least reviewer_code as reviewer
        assert "reviewer_code" in reviewers

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

    def test_review_graph_roles_subset_of_spawned_roles(self):
        """Review graph all_roles() must be a subset of spawned roles for each phase."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from review_graph import get_review_graph_for_phase

        for phase in [PipelinePhase.IMPLEMENT, PipelinePhase.REFINE, PipelinePhase.PLAN]:
            pipeline = _make_pipeline()
            pipeline.current_phase = phase
            executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

            roles = executor.get_agent_roles()
            role_names = {r.value for r in roles}
            graph = get_review_graph_for_phase(phase.value, repo=pipeline.repo)
            graph_roles = graph.all_roles()

            assert graph_roles.issubset(role_names), (
                f"Phase {phase.value}: graph has roles {graph_roles - role_names} "
                f"not in spawned roles {role_names}"
            )


class TestConcurrentPhasesGeneralization:
    """Verify all phases can be concurrent — no hardcoded phase gates."""

    def test_is_concurrent_for_all_configured_phases(self):
        """is_concurrent_execution returns True for any phase in concurrent_phases."""
        from concurrent_executor import is_concurrent_execution

        pipeline = _make_pipeline()
        # Ensure concurrent_execution is False — rely on concurrent_phases config
        pipeline.config.__dict__["concurrent_execution"] = False

        for phase in ["refine", "plan", "implement"]:
            assert is_concurrent_execution(pipeline, phase), (
                f"is_concurrent_execution should return True for {phase}"
            )

    def test_is_concurrent_false_for_unconfigured_phase(self):
        """is_concurrent_execution returns False for a phase not in concurrent_phases."""
        from concurrent_executor import is_concurrent_execution

        pipeline = _make_pipeline()
        pipeline.config.__dict__["concurrent_execution"] = False

        # "pr" is not in default concurrent_phases
        assert not is_concurrent_execution(pipeline, "pr")

    def test_concurrent_execution_true_overrides_phase_list(self):
        """When concurrent_execution is True, all phases are concurrent."""
        from concurrent_executor import is_concurrent_execution

        pipeline = _make_pipeline()
        # concurrent_execution=True already set in _make_pipeline

        for phase in ["refine", "plan", "implement", "pr"]:
            assert is_concurrent_execution(pipeline, phase), (
                f"concurrent_execution=True should make {phase} concurrent"
            )

    def test_get_agent_env_sets_concurrent_mode(self):
        """get_agent_env sets EGG_CONCURRENT_MODE=true for all concurrent agents."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        for phase in [PipelinePhase.IMPLEMENT, PipelinePhase.REFINE, PipelinePhase.PLAN]:
            pipeline = _make_pipeline()
            pipeline.current_phase = phase
            executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

            env = executor.get_agent_env(AgentRole.CODER)
            assert env.get("EGG_CONCURRENT_MODE") == "true", (
                f"EGG_CONCURRENT_MODE should be 'true' for {phase.value}"
            )


class TestFilePatternEnvVar:
    """Test that EGG_AGENT_FILE_PATTERNS is set for concurrent agents (#1431)."""

    def test_coder_gets_file_patterns(self):
        import json

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        env = executor.get_agent_env(AgentRole.CODER)
        assert "EGG_AGENT_FILE_PATTERNS" in env
        patterns = json.loads(env["EGG_AGENT_FILE_PATTERNS"])
        assert "allowed" in patterns
        assert "blocked" in patterns
        assert any("*.py" in p for p in patterns["allowed"])
        # Coder's blocked list includes docs and contracts
        assert any("docs/" in p for p in patterns["blocked"])

    def test_tester_gets_file_patterns(self):
        import json

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        env = executor.get_agent_env(AgentRole.TESTER)
        assert "EGG_AGENT_FILE_PATTERNS" in env
        patterns = json.loads(env["EGG_AGENT_FILE_PATTERNS"])
        assert any("tests/" in p or "test/" in p for p in patterns["allowed"])

    def test_documenter_gets_file_patterns(self):
        import json

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        env = executor.get_agent_env(AgentRole.DOCUMENTER)
        assert "EGG_AGENT_FILE_PATTERNS" in env
        patterns = json.loads(env["EGG_AGENT_FILE_PATTERNS"])
        assert any("*.md" in p for p in patterns["allowed"])
        assert any("*.py" in p for p in patterns["blocked"])
