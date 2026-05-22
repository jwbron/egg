"""Tests for ConcurrentPhaseExecutor with BRC protocol integration.

Verifies that the concurrent executor correctly sets up BRC environment
variables, creates the peer consensus tracker, and integrates review
graph information into agent configuration.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401  # used in slice-2 (#2769) test additions below

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
    except AttributeError, ValueError:
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


class TestSpawnPropagatesContainerInfo:
    """Test that _spawn_agent carries the full ContainerInfo from the spawner
    onto AgentExecution, preserving backend-specific fields like K8s
    pod_name/namespace/job_name (issue #1841)."""

    def test_spawn_agent_propagates_k8s_container_info(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole
        from kubernetes_spawner import SpawnedContainer
        from models import ContainerInfo, ContainerStatus

        pipeline = _make_pipeline()
        k8s_info = ContainerInfo(
            container_id="uid-abc123",
            container_name="issue-999-coder",
            status=ContainerStatus.PENDING,
            namespace="egg-sandbox",
            job_name="issue-999-coder",
        )
        spawn_result = SpawnedContainer(
            container_info=k8s_info,
            session_info=None,
            agent_role=AgentRole.CODER,
            pipeline_id="issue-999",
            environment={},
        )
        mock_spawn = MagicMock(return_value=spawn_result)

        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)
        execution = executor._spawn_agent(AgentRole.CODER)

        assert execution.container_id == "uid-abc123"
        assert execution.container_info is not None
        assert execution.container_info.namespace == "egg-sandbox"
        assert execution.container_info.job_name == "issue-999-coder"
        assert execution.container_info.container_id == "uid-abc123"


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


class TestIsTransientAgentError:
    """_is_transient_agent_error classifies spawn error strings for #1879 retry."""

    def test_connection_refused_is_transient(self):
        from concurrent_executor import _is_transient_agent_error

        assert _is_transient_agent_error("GatewayError: Connection refused")

    def test_remote_end_closed_is_transient(self):
        from concurrent_executor import _is_transient_agent_error

        assert _is_transient_agent_error("Remote end closed connection without response")

    def test_connection_reset_is_transient(self):
        from concurrent_executor import _is_transient_agent_error

        assert _is_transient_agent_error(
            "ConnectionResetError: [Errno 104] Connection reset by peer"
        )

    def test_timeout_is_transient(self):
        from concurrent_executor import _is_transient_agent_error

        assert _is_transient_agent_error("HTTPConnectionPool: Read timed out")

    def test_service_unavailable_is_transient(self):
        from concurrent_executor import _is_transient_agent_error

        assert _is_transient_agent_error("503 Service Unavailable")

    def test_failed_to_create_any_worktrees_is_transient(self):
        from concurrent_executor import _is_transient_agent_error

        assert _is_transient_agent_error(
            "KubernetesSpawnError: Failed to create any worktrees for container"
        )

    def test_permanent_errors_not_transient(self):
        from concurrent_executor import _is_transient_agent_error

        # Typical permanent failures
        assert not _is_transient_agent_error("Repository not found")
        assert not _is_transient_agent_error("HTTP 403 Forbidden")
        assert not _is_transient_agent_error("2 validation errors for AgentExecution container_id")

    def test_none_is_not_transient(self):
        from concurrent_executor import _is_transient_agent_error

        assert not _is_transient_agent_error(None)
        assert not _is_transient_agent_error("")


class TestSpawnSpecificRoles:
    """spawn_specific_roles respawns only the requested roles."""

    def test_only_listed_roles_are_spawned(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        mock_spawn = MagicMock()
        mock_spawn.return_value = MagicMock(container_id="new-container")

        # Executor initialised with a full cohort, but the retry call
        # should only hit the subset we pass in.
        executor = ConcurrentPhaseExecutor(
            pipeline,
            spawn_fn=mock_spawn,
            roles=[AgentRole.CODER, AgentRole.REVIEWER_CODE, AgentRole.TESTER],
        )

        with patch("concurrent_executor.emit_event"):
            executions = executor.spawn_specific_roles(
                [AgentRole.CODER, AgentRole.TESTER],
                agent_prompts={
                    AgentRole.CODER: "coder prompt",
                    AgentRole.TESTER: "tester prompt",
                },
            )

        assert mock_spawn.call_count == 2
        spawned_roles = {call.kwargs["role"] for call in mock_spawn.call_args_list}
        assert spawned_roles == {AgentRole.CODER, AgentRole.TESTER}
        assert {e.role for e in executions} == {AgentRole.CODER, AgentRole.TESTER}

    def test_does_not_register_tracker_agents(self):
        """spawn_specific_roles must not touch the consensus tracker — the
        original spawn_all already registered every role."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        mock_spawn = MagicMock()
        mock_spawn.return_value = MagicMock(container_id="new-container")

        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn, roles=[AgentRole.CODER])

        with (
            patch("concurrent_executor.create_peer_consensus_tracker") as mock_tracker,
            patch("concurrent_executor.emit_event"),
        ):
            executor.spawn_specific_roles([AgentRole.CODER], agent_prompts={})

        mock_tracker.assert_not_called()


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
    """Post-#1882: EGG_AGENT_FILE_PATTERNS is no longer emitted.

    Originally added in #1431 so ``sandbox/egg_lib/cli_push.py
    --scope-filter`` could read the pushing role's patterns and strip
    disallowed files client-side.  #1882 moves that responsibility to
    the gateway's auto-filter and deletes both the env-var injection
    (here) and the ``--scope-filter`` consumer (in sandbox).  These
    tests now assert the env var is NOT emitted so a re-introduction
    fails loudly.
    """

    def test_coder_does_not_get_file_patterns_env(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        env = executor.get_agent_env(AgentRole.CODER)
        assert "EGG_AGENT_FILE_PATTERNS" not in env

    def test_tester_does_not_get_file_patterns_env(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        env = executor.get_agent_env(AgentRole.TESTER)
        assert "EGG_AGENT_FILE_PATTERNS" not in env

    def test_documenter_does_not_get_file_patterns_env(self):
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())
        env = executor.get_agent_env(AgentRole.DOCUMENTER)
        assert "EGG_AGENT_FILE_PATTERNS" not in env


class TestCheckConsensusMessageBusFallback:
    """Test the message-bus fallback in check_consensus (#1615).

    When the tracker's evaluate() returns is_complete=False but all roles
    have CONSENSUS_CONFIRMED messages in the store, check_consensus should
    override and return is_complete=True.
    """

    def test_fallback_detects_consensus_from_messages(self):
        """Message-bus fallback should detect consensus when tracker disagrees."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from message_store import Message, MessageType
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )

        pipeline = _make_pipeline("KORE-1234")
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), review_graph=graph)

        # Create a tracker that says consensus is NOT complete
        from peer_consensus import (
            create_peer_consensus_tracker,
            remove_peer_consensus_tracker,
        )

        try:
            tracker = create_peer_consensus_tracker("KORE-1234", graph)
            tracker.register_agent("coder")
            tracker.register_agent("reviewer_code")
            # Don't call handle_propose/ack/confirmed — tracker thinks incomplete

            # But the message store has CONFIRMED messages for all roles
            confirmed_messages = [
                Message(
                    pipeline_id="KORE-1234",
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="Confirmed by coder",
                ),
                Message(
                    pipeline_id="KORE-1234",
                    from_role="reviewer_code",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="Confirmed by reviewer_code",
                ),
            ]

            mock_store = MagicMock()
            mock_store.get_messages.return_value = confirmed_messages

            with patch("message_store.get_message_store", return_value=mock_store):
                result = executor.check_consensus()

            assert result["is_complete"] is True
            assert result.get("fallback") == "message_bus"
        finally:
            remove_peer_consensus_tracker("KORE-1234")

    def test_fallback_does_not_fire_when_roles_missing(self):
        """Message-bus fallback should not fire when not all roles confirmed."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from message_store import Message, MessageType
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )

        pipeline = _make_pipeline("KORE-1234")
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), review_graph=graph)

        from peer_consensus import create_peer_consensus_tracker, remove_peer_consensus_tracker

        try:
            tracker = create_peer_consensus_tracker("KORE-1234", graph)
            tracker.register_agent("coder")
            tracker.register_agent("reviewer_code")

            # Only one role confirmed in messages
            confirmed_messages = [
                Message(
                    pipeline_id="KORE-1234",
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="Confirmed by coder",
                ),
            ]

            mock_store = MagicMock()
            mock_store.get_messages.return_value = confirmed_messages

            with patch("message_store.get_message_store", return_value=mock_store):
                result = executor.check_consensus()

            assert result["is_complete"] is False
            assert "fallback" not in result
        finally:
            remove_peer_consensus_tracker("KORE-1234")

    def test_fallback_excludes_pending_acks_messages(self):
        """Message-bus fallback must not count pending_acks messages as genuine confirmations."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from message_store import Message, MessageType
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )

        pipeline = _make_pipeline("KORE-1234")
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), review_graph=graph)

        from peer_consensus import create_peer_consensus_tracker, remove_peer_consensus_tracker

        try:
            tracker = create_peer_consensus_tracker("KORE-1234", graph)
            tracker.register_agent("coder")
            tracker.register_agent("reviewer_code")

            # All roles have CONSENSUS_CONFIRMED messages, but all have pending_acks=True
            pending_messages = [
                Message(
                    pipeline_id="KORE-1234",
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="Confirmed by coder (pending_acks)",
                    metadata={"pending_acks": True},
                ),
                Message(
                    pipeline_id="KORE-1234",
                    from_role="reviewer_code",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="Confirmed by reviewer_code (pending_acks)",
                    metadata={"pending_acks": True},
                ),
            ]

            mock_store = MagicMock()
            mock_store.get_messages.return_value = pending_messages

            with patch("message_store.get_message_store", return_value=mock_store):
                result = executor.check_consensus()

            # pending_acks messages excluded when tracker hasn't confirmed the role
            assert result["is_complete"] is False
            assert "fallback" not in result
        finally:
            remove_peer_consensus_tracker("KORE-1234")

    def test_tracker_confirmed_safety_net_preempts_message_bus(self):
        """When all roles are in confirmed_roles, the tracker safety net fires before message-bus fallback (#1671)."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from message_store import Message, MessageType
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )

        pipeline = _make_pipeline("KORE-1234")
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), review_graph=graph)

        from peer_consensus import create_peer_consensus_tracker, remove_peer_consensus_tracker

        try:
            tracker = create_peer_consensus_tracker("KORE-1234", graph)
            tracker.register_agent("coder")
            tracker.register_agent("reviewer_code")

            # Simulate: tracker accepted confirmations (via retries) but
            # only pending_acks messages exist in the store.
            tracker._confirmed.add("coder")
            tracker._confirmed.add("reviewer_code")

            # Force evaluate() to return incomplete (e.g. stale NACK edge)
            # by patching evaluate to return a custom result.
            original_evaluate = tracker.evaluate

            def _evaluate_incomplete():
                result = original_evaluate()
                result["is_complete"] = False
                result["blocking_agents"] = ["coder"]
                return result

            tracker.evaluate = _evaluate_incomplete

            pending_messages = [
                Message(
                    pipeline_id="KORE-1234",
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="Confirmed by coder (pending_acks)",
                    metadata={"pending_acks": True},
                ),
                Message(
                    pipeline_id="KORE-1234",
                    from_role="reviewer_code",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="Confirmed by reviewer_code (pending_acks)",
                    metadata={"pending_acks": True},
                ),
            ]

            mock_store = MagicMock()
            mock_store.get_messages.return_value = pending_messages

            with patch("message_store.get_message_store", return_value=mock_store):
                result = executor.check_consensus()

            # The tracker-confirmed safety net should fire first since
            # all roles are in confirmed_roles.
            assert result["is_complete"] is True
            assert result.get("fallback") == "tracker_confirmed"
        finally:
            remove_peer_consensus_tracker("KORE-1234")

    def test_tracker_confirmed_overrides_stale_nacks(self):
        """All roles in confirmed_roles but evaluate() says False due to stale NACKs → override (#1671)."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )

        pipeline = _make_pipeline("KORE-1234")
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), review_graph=graph)

        from peer_consensus import create_peer_consensus_tracker, remove_peer_consensus_tracker

        try:
            tracker = create_peer_consensus_tracker("KORE-1234", graph)
            tracker.register_agent("coder")
            tracker.register_agent("reviewer_code")

            # Manually set all roles as confirmed in the tracker
            tracker._confirmed.add("coder")
            tracker._confirmed.add("reviewer_code")

            # Simulate stale NACK edges by patching evaluate()
            original_evaluate = tracker.evaluate

            def _evaluate_with_stale_nacks():
                result = original_evaluate()
                result["is_complete"] = False
                result["has_unresolved_nacks"] = True
                result["blocking_agents"] = []
                return result

            tracker.evaluate = _evaluate_with_stale_nacks

            result = executor.check_consensus()

            assert result["is_complete"] is True
            assert result["fallback"] == "tracker_confirmed"
        finally:
            remove_peer_consensus_tracker("KORE-1234")

    def test_message_bus_fallback_with_mixed_pending_and_clean(self):
        """Fallback counts roles with clean messages even when others have pending_acks only."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from message_store import Message, MessageType
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )

        pipeline = _make_pipeline("KORE-1234")
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), review_graph=graph)

        from peer_consensus import create_peer_consensus_tracker, remove_peer_consensus_tracker

        try:
            tracker = create_peer_consensus_tracker("KORE-1234", graph)
            tracker.register_agent("coder")
            tracker.register_agent("reviewer_code")

            # Coder confirmed in tracker, reviewer_code not confirmed
            tracker._confirmed.add("coder")

            original_evaluate = tracker.evaluate

            def _evaluate_incomplete():
                result = original_evaluate()
                result["is_complete"] = False
                result["blocking_agents"] = ["reviewer_code"]
                return result

            tracker.evaluate = _evaluate_incomplete

            # Coder has pending_acks only (but tracker confirmed), reviewer has clean message
            messages = [
                Message(
                    pipeline_id="KORE-1234",
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="Confirmed by coder (pending_acks)",
                    metadata={"pending_acks": True},
                ),
                Message(
                    pipeline_id="KORE-1234",
                    from_role="reviewer_code",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="Confirmed by reviewer_code",
                ),
            ]

            mock_store = MagicMock()
            mock_store.get_messages.return_value = messages

            with patch("message_store.get_message_store", return_value=mock_store):
                result = executor.check_consensus()

            assert result["is_complete"] is True
            assert result["fallback"] == "message_bus"
        finally:
            remove_peer_consensus_tracker("KORE-1234")


# =============================================================================
# Per-agent model wiring — slice-2 of issue #2769 (TASK-2-4 / TASK-2-7)
# =============================================================================
#
# Slice 2 threads ``resolve_agent_model(role, pipeline.config, pipeline.repo)``
# through the spawn path so:
#
# - ``build_consensus_wrapped_command`` receives the resolved
#   Claude-Code-facing alias as ``model=`` (e.g. always ``"opus"`` on
#   LiteLLM-routed agents, per cq-5).
# - ``spawn_fn`` (which dispatches to ``KubernetesSpawner.spawn_agent``
#   → ``GatewayClient.register_session``) receives ``upstream=`` and
#   ``upstream_model=`` so the gateway session is registered with the
#   right per-agent routing decision.
#
# Default-config pipelines (``agent_models == {}``) MUST still hit the
# old call shape — no new register_session kwargs, no change to the
# ``--model`` flag.  That's the slice-2 no-op invariant.
# =============================================================================


def _kubernetes_spawn_result(role_value: str = "coder"):
    """Build a minimal ``SpawnedContainer`` so ``_spawn_agent`` is happy."""
    from kubernetes_spawner import SpawnedContainer
    from models import ContainerInfo, ContainerStatus

    info = ContainerInfo(
        container_id=f"uid-{role_value}",
        container_name=f"issue-999-{role_value}",
        status=ContainerStatus.PENDING,
        namespace="egg-sandbox",
        job_name=f"issue-999-{role_value}",
    )
    return SpawnedContainer(
        container_info=info,
        session_info=None,
        agent_role=None,
        pipeline_id="issue-999",
        environment={},
    )


def _slice_2_available() -> bool:
    """Return True if the slice-2 resolver has landed.

    Tests below skip when False — the coder hasn't pushed yet.
    """
    try:
        import agent_model_resolution  # type: ignore[import-not-found]  # noqa: F401

        return True
    except ImportError:
        return False


class TestSpawnDefaultAgentModelPath:
    """Regression guard: default-config pipelines spawn EXACTLY as
    before slice-2 — same ``build_consensus_wrapped_command`` args, no
    new ``register_session`` kwargs."""

    def test_default_config_passes_opus_to_consensus_wrapper(self):
        """``agent_models == {}`` → coder spawn passes ``model="opus"``
        (or whatever today's built-in default is) to
        ``build_consensus_wrapped_command``.
        """
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        # No agent_models, no repo default → built-in path.
        captured: dict[str, object] = {}

        def _capture_command(prompt_text, **kwargs):
            captured["prompt_text"] = prompt_text
            # No default — the test must distinguish "model='opus' was
            # passed" from "model was not passed at all".
            captured["model"] = kwargs.get("model")
            return ["bash", "-c", "true"]

        mock_spawn = MagicMock(return_value=_kubernetes_spawn_result())
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        with patch(
            "concurrent_executor.build_consensus_wrapped_command",
            side_effect=_capture_command,
        ):
            executor._spawn_agent(AgentRole.CODER, prompt_text="run task")

        assert captured.get("model") == "opus", (
            f"Default-config spawn MUST pass model='opus' to the "
            f"consensus wrapper; got {captured.get('model')!r}"
        )

    def test_default_config_omits_upstream_kwargs_on_spawn_fn(self):
        """The spawn_fn must NOT receive upstream/upstream_model kwargs
        when ``agent_models == {}`` — that would be a wire-shape change
        on the default Claude path (regression guard).
        """
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        mock_spawn = MagicMock(return_value=_kubernetes_spawn_result())
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        with patch(
            "concurrent_executor.build_consensus_wrapped_command",
            return_value=["bash", "-c", "true"],
        ):
            executor._spawn_agent(AgentRole.CODER, prompt_text="run task")

        # spawn_fn was called once
        assert mock_spawn.call_count == 1, mock_spawn.call_args_list
        _args, kwargs = mock_spawn.call_args

        # Slice-2 invariant: when no override is configured, the
        # default-Anthropic case omits the new kwargs entirely OR
        # passes them as None — either is acceptable wire-shape;
        # what's NOT acceptable is sending ``upstream="litellm"`` or
        # a non-None ``upstream_model``.
        assert kwargs.get("upstream") in (None, "anthropic"), (
            f"Default config should not send upstream='litellm'; got {kwargs.get('upstream')!r}"
        )
        assert kwargs.get("upstream_model") is None, (
            f"Default config MUST NOT send upstream_model; got {kwargs.get('upstream_model')!r}"
        )


class TestSpawnLiteLLMConfiguredPath:
    """``agent_models={"refiner": "qwen3-coder-30b"}`` MUST:

    - Pass ``model="opus"`` (cq-5 mitigation) to the consensus wrapper.
    - Pass ``upstream="litellm"`` and
      ``upstream_model="qwen3-coder-30b"`` to the spawn_fn.

    Other roles (e.g. coder) in the same pipeline MUST keep the
    default Anthropic shape — the override is per-role.
    """

    def test_refiner_override_passes_opus_to_consensus_wrapper(self):
        if not _slice_2_available():
            pytest.skip("agent_model_resolution not yet implemented")

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        try:
            pipeline.config.agent_models = {"refiner": "qwen3-coder-30b"}
        except AttributeError, ValueError:
            pipeline.config.__dict__["agent_models"] = {"refiner": "qwen3-coder-30b"}

        captured: dict[str, object] = {}

        def _capture_command(prompt_text, **kwargs):
            captured["model"] = kwargs.get("model")
            return ["bash", "-c", "true"]

        mock_spawn = MagicMock(return_value=_kubernetes_spawn_result("refiner"))
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        with patch(
            "concurrent_executor.build_consensus_wrapped_command",
            side_effect=_capture_command,
        ):
            executor._spawn_agent(AgentRole.REFINER, prompt_text="run task")

        # cq-5: Claude Code's ``--model`` flag MUST be a recognized
        # Claude alias, NEVER the LiteLLM-side upstream model name.
        assert captured.get("model") == "opus", (
            f"LiteLLM-routed refiner MUST present model='opus' to "
            f"Claude Code (cq-5); got {captured.get('model')!r}"
        )

    def test_refiner_override_passes_upstream_to_spawn_fn(self):
        if not _slice_2_available():
            pytest.skip("agent_model_resolution not yet implemented")

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        try:
            pipeline.config.agent_models = {"refiner": "qwen3-coder-30b"}
        except AttributeError, ValueError:
            pipeline.config.__dict__["agent_models"] = {"refiner": "qwen3-coder-30b"}

        mock_spawn = MagicMock(return_value=_kubernetes_spawn_result("refiner"))
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        with patch(
            "concurrent_executor.build_consensus_wrapped_command",
            return_value=["bash", "-c", "true"],
        ):
            executor._spawn_agent(AgentRole.REFINER, prompt_text="run task")

        _args, kwargs = mock_spawn.call_args
        assert kwargs.get("upstream") == "litellm", (
            f"refiner spawn_fn MUST receive upstream='litellm'; got {kwargs.get('upstream')!r}"
        )
        assert kwargs.get("upstream_model") == "qwen3-coder-30b", (
            f"refiner spawn_fn MUST receive upstream_model='qwen3-coder-30b'; "
            f"got {kwargs.get('upstream_model')!r}"
        )

    def test_refiner_override_does_not_affect_coder_spawn(self):
        """Per-role override — coder spawn MUST stay on the default
        Anthropic shape even when refiner is overridden.
        """
        if not _slice_2_available():
            pytest.skip("agent_model_resolution not yet implemented")

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        try:
            pipeline.config.agent_models = {"refiner": "qwen3-coder-30b"}
        except AttributeError, ValueError:
            pipeline.config.__dict__["agent_models"] = {"refiner": "qwen3-coder-30b"}

        captured: dict[str, object] = {}

        def _capture_command(prompt_text, **kwargs):
            captured["model"] = kwargs.get("model")
            return ["bash", "-c", "true"]

        mock_spawn = MagicMock(return_value=_kubernetes_spawn_result("coder"))
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        with patch(
            "concurrent_executor.build_consensus_wrapped_command",
            side_effect=_capture_command,
        ):
            executor._spawn_agent(AgentRole.CODER, prompt_text="run task")

        # Coder is not overridden → built-in default → "opus" alias
        # to Anthropic.
        assert captured.get("model") == "opus"
        _args, kwargs = mock_spawn.call_args
        assert kwargs.get("upstream") in (None, "anthropic"), (
            f"Coder spawn_fn must not carry upstream='litellm' when "
            f"only refiner is overridden; got {kwargs.get('upstream')!r}"
        )
        assert kwargs.get("upstream_model") is None, (
            f"Coder spawn_fn must have upstream_model=None when only "
            f"refiner is overridden; got {kwargs.get('upstream_model')!r}"
        )


class TestSpawnClaudeAliasOverride:
    """Override to a different Claude alias (e.g. ``"sonnet"``) — the
    upstream stays Anthropic but ``--model`` gets the new alias.
    """

    def test_sonnet_override_passes_sonnet_to_consensus_wrapper(self):
        if not _slice_2_available():
            pytest.skip("agent_model_resolution not yet implemented")

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        try:
            pipeline.config.agent_models = {"coder": "sonnet"}
        except AttributeError, ValueError:
            pipeline.config.__dict__["agent_models"] = {"coder": "sonnet"}

        captured: dict[str, object] = {}

        def _capture_command(prompt_text, **kwargs):
            captured["model"] = kwargs.get("model")
            return ["bash", "-c", "true"]

        mock_spawn = MagicMock(return_value=_kubernetes_spawn_result("coder"))
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        with patch(
            "concurrent_executor.build_consensus_wrapped_command",
            side_effect=_capture_command,
        ):
            executor._spawn_agent(AgentRole.CODER, prompt_text="run task")

        # Anthropic-classified — alias passes through as-is.
        assert captured.get("model") == "sonnet"
        _args, kwargs = mock_spawn.call_args
        # Upstream is Anthropic → no LiteLLM routing decision.
        assert kwargs.get("upstream") in (None, "anthropic")
        assert kwargs.get("upstream_model") is None


class TestSpawnResolverFailureFallback:
    """If ``resolve_agent_model`` ever raises at spawn time, the spawner
    MUST degrade to the built-in opus / anthropic default instead of
    bringing down the pipeline (defensive guard added in slice-2 v2).

    Mirrors the existing restart-path fallback at
    ``routes/pipelines.py:2683-2699``.
    """

    def test_resolver_exception_falls_back_to_opus_anthropic(self):
        if not _slice_2_available():
            pytest.skip("agent_model_resolution not yet implemented")

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        captured: dict[str, object] = {}

        def _capture_command(prompt_text, **kwargs):
            captured["model"] = kwargs.get("model")
            return ["bash", "-c", "true"]

        mock_spawn = MagicMock(return_value=_kubernetes_spawn_result("coder"))
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        # Force the resolver to raise.  The spawner's defensive wrap
        # MUST catch this and fall back to the built-in opus default.
        with (
            patch(
                "concurrent_executor.resolve_agent_model",
                side_effect=RuntimeError("simulated resolver bug"),
            ),
            patch(
                "concurrent_executor.build_consensus_wrapped_command",
                side_effect=_capture_command,
            ),
        ):
            # MUST NOT raise — the defensive guard catches and falls back.
            execution = executor._spawn_agent(AgentRole.CODER, prompt_text="run task")

        # Spawn completed → execution returned.
        assert execution is not None
        # Fallback decision is built-in opus / anthropic; no kwargs added.
        assert captured.get("model") == "opus", (
            f"Resolver-failure fallback MUST pass model='opus' to the "
            f"consensus wrapper; got {captured.get('model')!r}"
        )
        _args, kwargs = mock_spawn.call_args
        # No new wire kwargs on the fallback path — preserves the
        # pre-#2769 spawn_fn signature for legacy spawners.
        assert kwargs.get("upstream") in (None, "anthropic"), (
            f"Resolver-failure fallback added upstream='{kwargs.get('upstream')!r}' "
            f"to spawn_fn — must omit on the default-Anthropic path"
        )
        assert kwargs.get("upstream_model") is None

    def test_resolver_exception_still_calls_spawn_fn(self):
        """Defensive guard MUST NOT short-circuit the spawn — the
        agent's container/job must still come up on the built-in
        default. (Adversarial probe: a broken `except Exception: raise`
        path here would silently break every spawn.)
        """
        if not _slice_2_available():
            pytest.skip("agent_model_resolution not yet implemented")

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        mock_spawn = MagicMock(return_value=_kubernetes_spawn_result("coder"))
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        with (
            patch(
                "concurrent_executor.resolve_agent_model",
                side_effect=ImportError("simulated lazy-import bug"),
            ),
            patch(
                "concurrent_executor.build_consensus_wrapped_command",
                return_value=["bash", "-c", "true"],
            ),
        ):
            executor._spawn_agent(AgentRole.CODER, prompt_text="run task")

        assert mock_spawn.call_count == 1, (
            f"Resolver failure MUST NOT short-circuit spawn; spawn_fn "
            f"called {mock_spawn.call_count} times (expected 1)"
        )


class TestResolverMissingRepoConfigDoesNotCrash:
    """Adversarial probe: the v1 NACK called out a FileNotFoundError
    leak when ``repositories.yaml`` is absent.  v2 ``get_default_agent_model``
    catches that and returns ``None`` so default pipelines still spawn.

    This test exercises the path end-to-end via ``_spawn_agent`` with
    a test pipeline that has a non-None ``repo`` (the v1 trigger
    condition) and confirms no exception propagates.
    """

    def test_spawn_with_missing_repositories_yaml_does_not_raise(self):
        if not _slice_2_available():
            pytest.skip("agent_model_resolution not yet implemented")

        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        # ``test/repo`` is the _make_pipeline default; the resolver
        # would call get_default_agent_model("test/repo") which used
        # to raise.  After v2 the helper returns None on FileNotFoundError.
        assert pipeline.repo == "test/repo"

        mock_spawn = MagicMock(return_value=_kubernetes_spawn_result("coder"))
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=mock_spawn)

        with patch(
            "concurrent_executor.build_consensus_wrapped_command",
            return_value=["bash", "-c", "true"],
        ):
            # MUST NOT raise FileNotFoundError — the v2 fix in
            # config.repo_config.get_default_agent_model catches it.
            executor._spawn_agent(AgentRole.CODER, prompt_text="run task")

        assert mock_spawn.call_count == 1
