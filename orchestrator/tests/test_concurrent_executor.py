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

    def test_phase_idle_budget_owner_env_injected_for_every_role(self):
        """#3023 slice-1 task-1-3 AC: EGG_PHASE_IDLE_BUDGET_OWNER=orchestrator
        appears in the spawn env from _spawn_agent for every role.

        Coexistence guard for the wrapper-side ``check_idle_budget``
        emitter (consensus_wrapper.py): the wrapper short-circuits when
        the orchestrator is the authoritative emitter of the
        ``stuck-phase-transition`` alert. Without this env var the
        operator would be paged twice per phase (once per role from
        the wrapper + once per phase from the orchestrator).
        """
        from concurrent_executor import ConcurrentPhaseExecutor
        from egg_orchestrator.types import AgentRole

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        for role in (
            AgentRole.CODER,
            AgentRole.REVIEWER_CODE,
            AgentRole.TESTER,
            AgentRole.DOCUMENTER,
            AgentRole.REVIEWER_CONTRACT,
            AgentRole.REVIEWER_SECURITY,
        ):
            env = executor.get_agent_env(role)
            assert env.get("EGG_PHASE_IDLE_BUDGET_OWNER") == "orchestrator", (
                f"role={role!r} must carry EGG_PHASE_IDLE_BUDGET_OWNER="
                "orchestrator (slice-1 task-1-3 coexistence guard)."
            )

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

    def test_spawn_all_with_roles_override_only_registers_overridden_roles(self):
        """Post-#3023 slice-3 (TASK-3-2): ``spawn_all`` no longer spawns
        long-lived wrapper pods — every spawn is on-demand from the
        orchestrator tick. The roles-override invariant is preserved at
        the tracker-registration level: only the overridden roles are
        registered with the consensus tracker, so reviewers ACK against
        the correct agent set.
        """
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

            # No wrapper-pod spawns (TASK-3-2 retirement).
            assert mock_spawn.call_count == 0
            # Only 2 agents should be registered (coder + reviewer_code).
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

        # #2809 — risk_analyst is dual-role: CRITICAL reviewer of
        # architect and task_planner in addition to producing the
        # risk register.
        assert graph.is_reviewer("risk_analyst")
        assert graph.is_dual_role("risk_analyst")
        assert "risk_analyst" in graph.reviewers_for("architect")
        assert "risk_analyst" in graph.reviewers_for("task_planner")
        assert "risk_analyst" in graph.critical_reviewers_for("architect")
        assert "risk_analyst" in graph.critical_reviewers_for("task_planner")

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


# --------------------------------------------------------------------------- #
# Issue #3023 slice-3, TASK-3-2 TDD scaffold
# --------------------------------------------------------------------------- #
#
# Two test classes pin the slice-3 retirement of the in-pod event-pump
# wrapper as it affects ``concurrent_executor.py``:
#
#   * ``TestSpawnAllRetiresWrapperPodSpawn`` — pins the planned
#     disposition of ``spawn_all`` (plan §slice-3 / TASK-3-2): the
#     long-lived per-role wrapper pod is no longer spawned. After the
#     coder lands TASK-3-2, ``spawn_all`` collapses to tracker
#     registration + auto-ACK pre-seed only — **no call to
#     ``_spawn_roles``** — and ``record_phase_start`` from the new
#     ``OnDemandSpawner`` (slice-2 / TASK-2-8) handles per-phase
#     session+PVC pre-warm. The orchestrator's tick handles every
#     subsequent spawn on demand.
#
#   * ``TestSpawnAgentNoWrapperCommand`` — pins TASK-3-2's
#     ``concurrent_executor.py:489`` change: ``_spawn_agent`` no longer
#     calls ``build_consensus_wrapped_command``; the on-demand command
#     constructed in slice-2 / TASK-2-4 is now the only path. The
#     symbol must be gone from the module (it lives in
#     ``orchestrator/consensus_wrapper.py``, which TASK-3-1 deletes).
#
# Both classes use the same skip-vs-assert dance the slice-1 TDD
# scaffold (`test_consensus_wrapper.py:TestPhaseIdleBudgetOwnerEnvInjection`)
# uses so ``make test`` stays green during parallel-producer BRC: tests
# skip cleanly until the coder lands TASK-3-1+TASK-3-2, then assert
# against the real surface. The race window flagged by reviewer_concurrency
# (tracker registration vs. the absent ``_spawn_roles`` spawn) is pinned
# here at the call-graph level — any future regression that re-introduces
# a spawn from ``spawn_all`` would re-open the race.


def _task_3_2_landed() -> bool:
    """Return True once TASK-3-2 (and its TASK-3-1 dependency on
    ``consensus_wrapper.py`` deletion) has landed.

    The signal we key off is the absence of the wrapper-only symbol
    from ``concurrent_executor``. While TASK-3-2 is still pending the
    module still imports ``build_consensus_wrapped_command`` at line
    ``concurrent_executor.py:37``; once TASK-3-1+TASK-3-2 land, the
    import is gone and the spawn path no longer references the wrapper.
    """
    import concurrent_executor as _ce

    return not hasattr(_ce, "build_consensus_wrapped_command")


class TestSpawnAllRetiresWrapperPodSpawn:
    """Slice-3 / TASK-3-2 acceptance line: ``spawn_all`` no longer calls
    ``_spawn_roles`` — the long-lived per-role wrapper pod is no longer
    spawned. ``record_phase_start`` from the on-demand spawner handles
    session+PVC pre-warm; every subsequent spawn is on-demand from the
    orchestrator tick.

    The skip-vs-assert pattern is the same one the slice-1 scaffold uses
    so a parallel-producer BRC cycle can keep ``make test`` green.
    """

    def _assert_or_skip(self, *, condition: bool, reason: str) -> None:
        if not _task_3_2_landed():
            pytest.skip(
                "TASK-3-2 (concurrent_executor.spawn_all collapse, "
                "build_consensus_wrapped_command removal) not yet landed; "
                "test will assert once the coder's commit lands."
            )
        assert condition, reason

    def test_spawn_all_does_not_call_spawn_roles(self):
        """After TASK-3-2 lands, ``spawn_all`` MUST NOT invoke
        ``_spawn_roles`` — the wrapper pod is retired.

        Pre-TASK-3-2 the test skips cleanly. Post-TASK-3-2 the spy on
        ``ConcurrentPhaseExecutor._spawn_roles`` must record zero calls.
        Re-introducing a ``_spawn_roles`` call from ``spawn_all`` would
        re-open the tracker-registration-vs-spawn race window
        reviewer_concurrency flagged on the v1 NACK.
        """
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        with (
            patch("concurrent_executor.create_peer_consensus_tracker") as mock_tracker,
            patch("concurrent_executor.emit_event"),
            patch.object(
                ConcurrentPhaseExecutor,
                "_spawn_roles",
                autospec=True,
                return_value=[],
            ) as spy_spawn_roles,
        ):
            mock_tracker.return_value = MagicMock()
            executor.spawn_all()

        self._assert_or_skip(
            condition=spy_spawn_roles.call_count == 0,
            reason=(
                "TASK-3-2 acceptance: spawn_all must NOT call _spawn_roles "
                f"after slice-3 lands (observed call_count={spy_spawn_roles.call_count}). "
                "The long-lived per-role wrapper pod is retired; "
                "record_phase_start handles per-phase session+PVC pre-warm "
                "and the orchestrator tick handles every per-event spawn."
            ),
        )

    def test_spawn_all_still_registers_tracker_agents(self):
        """The tracker-registration side of ``spawn_all`` is unchanged by
        TASK-3-2: every role must still be registered with the tracker so
        the BRC matrix is materialised and reviewers can ACK against a
        known set of agents. This pin guards against accidentally
        deleting the registration loop along with the spawn call.
        """
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        with (
            patch("concurrent_executor.create_peer_consensus_tracker") as mock_tracker,
            patch("concurrent_executor.emit_event"),
            patch.object(
                ConcurrentPhaseExecutor,
                "_spawn_roles",
                autospec=True,
                return_value=[],
            ),
        ):
            tracker_instance = MagicMock()
            mock_tracker.return_value = tracker_instance
            executor.spawn_all()

        # The tracker side is asserted unconditionally — it is the
        # invariant that must hold for both pre- and post-TASK-3-2
        # code paths. Skipping here would mask a regression.
        assert tracker_instance.register_agent.call_count == len(executor.get_agent_roles()), (
            "spawn_all must register every role with the consensus tracker "
            "regardless of TASK-3-2's collapse of the spawn call. A regression "
            "here would leave reviewers ACKing against an unknown agent set."
        )

    def test_register_agents_completes_before_auto_ack_preseed(self):
        """Direct rebuttal to reviewer_concurrency v2 item 4 (TASK-3-2
        concurrency-lens concern): "concurrent_executor.spawn_all
        tracker-registration + auto-ACK pre-seed race window needs pins".

        Pin the ordering invariant at
        ``concurrent_executor.py:344-374``: every ``register_agent`` call
        for every role MUST complete BEFORE
        ``seed_auto_ack_for_empty_pure_producers`` is invoked. If a
        future refactor were to interleave them — e.g. registering one
        role, calling the seeder, then registering the next — a
        pure-producer role still pending registration could be silently
        skipped by the seeder, leaving the BRC matrix in a half-seeded
        state. The skip-vs-assert pattern is unnecessary here because
        the ordering is already present pre-TASK-3-2 (it is the existing
        loop at lines 351-352 followed by the seeder at line 365) and
        TASK-3-2 only removes the trailing ``_spawn_roles`` call. The
        pin must hold both pre- and post-TASK-3-2 so the race window
        cannot re-open in either direction.
        """
        from concurrent_executor import ConcurrentPhaseExecutor

        # Pre-seed by enabling the auto-ACK path: ``spawn_all`` only
        # invokes the seeder when ``_producer_roles_with_tasks is not
        # None`` (see concurrent_executor.py:364). Pass an empty set
        # so every pure-producer role is a candidate for auto-ACK
        # pre-seed; the seeder fires unconditionally and the ordering
        # invariant is exercised.
        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(
            pipeline,
            spawn_fn=MagicMock(),
            producer_roles_with_tasks=set(),
        )

        call_log: list[str] = []
        tracker_instance = MagicMock()

        def _record_register_agent(role: str) -> None:
            call_log.append(f"register_agent:{role}")

        def _record_seed_auto_ack(producer_roles_with_tasks: set[str]) -> list[str]:
            call_log.append("seed_auto_ack_for_empty_pure_producers")
            return []

        tracker_instance.register_agent.side_effect = _record_register_agent
        tracker_instance.seed_auto_ack_for_empty_pure_producers.side_effect = _record_seed_auto_ack

        with (
            patch("concurrent_executor.create_peer_consensus_tracker") as mock_tracker,
            patch("concurrent_executor.emit_event"),
            patch.object(
                ConcurrentPhaseExecutor,
                "_spawn_roles",
                autospec=True,
                return_value=[],
            ),
        ):
            mock_tracker.return_value = tracker_instance
            executor.spawn_all()

        # Find the seeder index. If the seeder did not fire at all the
        # test would silently pass without exercising the invariant —
        # fail loudly so a future regression that drops the seeder is
        # caught here.
        assert "seed_auto_ack_for_empty_pure_producers" in call_log, (
            "Auto-ACK pre-seed did not fire under ProducerTasksSentinel; "
            "test cannot pin the ordering invariant without the seeder "
            "actually being called. Check that spawn_all still invokes "
            "seed_auto_ack_for_empty_pure_producers when "
            "_producer_roles_with_tasks is not None."
        )

        seeder_idx = call_log.index("seed_auto_ack_for_empty_pure_producers")
        register_idxs = [
            i for i, label in enumerate(call_log) if label.startswith("register_agent:")
        ]

        # Every register_agent index must be < the seeder index.
        # Equivalent to: the seeder is the LAST entry among
        # {register_agent x N, seeder}.
        assert all(idx < seeder_idx for idx in register_idxs), (
            "spawn_all ordering invariant violated: at least one "
            "register_agent call landed AFTER "
            "seed_auto_ack_for_empty_pure_producers. The seeder must run "
            "AFTER every role has been registered, otherwise an "
            "as-yet-unregistered pure-producer role is silently skipped "
            "by the seeder (leaving the BRC matrix half-seeded). "
            f"Observed call order: {call_log}"
        )

        # Sanity: register_agent fired at least once. Without this the
        # all() above would trivially pass on an empty list.
        assert register_idxs, (
            "spawn_all did not register any agents — test cannot pin "
            "the ordering invariant against a zero-call baseline."
        )


class TestSpawnAgentNoWrapperCommand:
    """Slice-3 / TASK-3-2 acceptance line: ``concurrent_executor.py:489``
    no longer builds the wrapper command. The wrapper-only symbol
    ``build_consensus_wrapped_command`` MUST disappear from the module
    namespace once TASK-3-1 deletes ``consensus_wrapper.py``.

    The grep-level acceptance (``grep -rn build_consensus_wrapped_command
    orchestrator/`` returns no matches) is pinned here at the symbol
    level so a regression that re-introduces the import is caught with
    a clean failure on this file.
    """

    def test_wrapper_symbol_absent_after_task_3_2(self):
        if not _task_3_2_landed():
            pytest.skip(
                "TASK-3-1 (consensus_wrapper.py delete) + TASK-3-2 "
                "(build_consensus_wrapped_command call-site removal) not "
                "yet landed; the wrapper symbol is still imported at "
                "concurrent_executor.py:37. Test will assert once the "
                "coder's commits land."
            )

        import concurrent_executor as _ce

        assert not hasattr(_ce, "build_consensus_wrapped_command"), (
            "TASK-3-2 acceptance: build_consensus_wrapped_command MUST NOT "
            "be importable from concurrent_executor after slice-3 lands. "
            "The wrapper module is deleted by TASK-3-1; any lingering "
            "import re-opens the wrapper-vs-on-demand coexistence the "
            "plan retired."
        )

    def test_source_grep_no_wrapper_reference(self):
        """The grep-level AC for TASK-3-2: ``grep -rn
        build_consensus_wrapped_command orchestrator/`` returns no
        matches outside the deletion commit. We pin this at the file
        level (``concurrent_executor.py`` source) so the test catches a
        regression that re-introduces the call.
        """
        if not _task_3_2_landed():
            pytest.skip(
                "Pre-TASK-3-2 the wrapper symbol is still referenced; "
                "source-grep assertion will pin once slice-3 lands."
            )

        source_path = Path(__file__).parent.parent / "concurrent_executor.py"
        source = source_path.read_text(encoding="utf-8")
        assert "build_consensus_wrapped_command" not in source, (
            "TASK-3-2 acceptance: concurrent_executor.py must not reference "
            "build_consensus_wrapped_command after slice-3. Found a lingering "
            "reference — re-check both the import at line 37 and the call "
            "site at line 489."
        )
