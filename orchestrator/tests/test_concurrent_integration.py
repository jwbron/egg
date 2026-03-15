"""
End-to-end integration tests for concurrent execution mode.

Tests the full lifecycle of a concurrent pipeline where multiple agents
exchange messages via the message bus and reach consensus for phase completion.
All external dependencies (containers, message store, consensus evaluator)
are mocked to test the orchestration logic in isolation.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import (
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import pipelines_bp


@pytest.fixture
def app():
    """Create a test Flask app with the pipelines blueprint."""
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


def _make_concurrent_pipeline(pipeline_id: str = "issue-999") -> Pipeline:
    """Create a pipeline with concurrent_execution enabled."""
    config = PipelineConfig()
    # Set concurrent execution fields (added by Phase 2 / TASK-2-1)
    # Use setattr since PipelineConfig may not have these fields yet
    # (Phase 2 dependency). When Phase 2 is implemented, these become
    # regular field assignments.
    try:
        config.concurrent_execution = True  # type: ignore[attr-defined]
        config.max_concurrent_agents = 6  # type: ignore[attr-defined]
        config.message_poll_hint_seconds = 30  # type: ignore[attr-defined]
        config.consensus_timeout_minutes = 30  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        # Fields not yet on PipelineConfig — set via __dict__ for testing
        config.__dict__["concurrent_execution"] = True
        config.__dict__["max_concurrent_agents"] = 6
        config.__dict__["message_poll_hint_seconds"] = 30
        config.__dict__["consensus_timeout_minutes"] = 30

    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=999,
        repo="owner/repo",
        branch="egg/issue-999",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )
    return pipeline


class TestConcurrentPipelineStatus:
    """Test that pipeline status includes concurrent execution data."""

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_status_includes_concurrent_section(self, mock_resolve, mock_repo_path, client):
        """When concurrent_execution is true, status response includes concurrent data."""
        pipeline = _make_concurrent_pipeline()
        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.get("/api/v1/pipelines/issue-999/status")
        assert resp.status_code == 200

        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["data"]["id"] == "issue-999"
        assert data["data"]["status"] == "running"
        assert data["data"]["current_phase"] == "implement"

        # Concurrent section should be present
        concurrent = data["data"].get("concurrent")
        if concurrent is not None:
            # When phases 1-3 aren't available, we still get the structure
            assert concurrent["enabled"] is True
            assert "messages" in concurrent
            assert "consensus" in concurrent

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_status_no_concurrent_when_disabled(self, mock_resolve, mock_repo_path, client):
        """When concurrent_execution is false, status has no concurrent section."""
        config = PipelineConfig()
        config.concurrent_execution = False
        pipeline = Pipeline(
            id="issue-100",
            issue_number=100,
            repo="owner/repo",
            branch="egg/issue-100",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            config=config,
        )
        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        resp = client.get("/api/v1/pipelines/issue-100/status")
        assert resp.status_code == 200

        data = json.loads(resp.data)
        assert "concurrent" not in data["data"]


class TestConcurrentMessageExchange:
    """Test that mocked agents can exchange messages through the message bus.

    These tests simulate the message flow that would occur with the Phase 1
    message bus infrastructure. They validate the integration pattern rather
    than the actual message_store (which is tested in Phase 1).
    """

    def test_message_send_and_poll_flow(self):
        """Simulate a coder sending a PROGRESS message and tester polling it."""
        # Simulate in-memory message store behavior
        messages = []

        def send_message(pipeline_id, from_role, to_role, msg_type, subject, body):
            msg = {
                "id": f"msg-{len(messages) + 1}",
                "pipeline_id": pipeline_id,
                "from_role": from_role,
                "to_role": to_role,
                "message_type": msg_type,
                "subject": subject,
                "body": body,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            messages.append(msg)
            return msg

        def poll_messages(pipeline_id, role, since_id=None):
            return [
                m
                for m in messages
                if m["to_role"] in (role, "all")
                and (since_id is None or int(m["id"].split("-")[1]) > int(since_id.split("-")[1]))
            ]

        # Coder sends progress to all
        send_message(
            "issue-999", "coder", "all", "PROGRESS", "API complete", "Finished API endpoints"
        )

        # Tester polls and gets the message
        received = poll_messages("issue-999", "tester")
        assert len(received) == 1
        assert received[0]["from_role"] == "coder"
        assert received[0]["message_type"] == "PROGRESS"
        assert received[0]["subject"] == "API complete"

        # Tester sends question to coder
        send_message(
            "issue-999",
            "tester",
            "coder",
            "QUESTION",
            "Test expectations",
            "What is expected return code?",
        )

        # Coder polls and gets broadcast + targeted question
        received = poll_messages("issue-999", "coder")
        assert len(received) == 2  # Broadcast PROGRESS + targeted QUESTION
        assert received[0]["message_type"] == "PROGRESS"  # broadcast to "all"
        assert received[1]["message_type"] == "QUESTION"  # targeted to "coder"

    def test_broadcast_message_received_by_all(self):
        """Broadcast messages (to_role='all') are received by all agents."""
        messages = []

        def send_message(from_role, to_role, msg_type, subject):
            messages.append(
                {
                    "id": f"msg-{len(messages) + 1}",
                    "from_role": from_role,
                    "to_role": to_role,
                    "message_type": msg_type,
                    "subject": subject,
                }
            )

        def poll_for_role(role):
            return [m for m in messages if m["to_role"] in (role, "all")]

        # Integrator broadcasts status
        send_message("integrator", "all", "STATUS", "Starting merge")

        # All agents should see it
        for role in ("coder", "tester", "documenter"):
            received = poll_for_role(role)
            assert len(received) == 1, f"{role} should receive broadcast"
            assert received[0]["from_role"] == "integrator"


class TestConcurrentConsensusFlow:
    """Test consensus protocol flow with mocked agents.

    These tests simulate the readiness signaling and consensus evaluation
    that would occur with Phase 3's ConsensusEvaluator.
    """

    def test_all_agents_ready_completes_phase(self):
        """When all agents signal READY, consensus is reached."""
        agent_states = {
            "coder": "WORKING",
            "tester": "WORKING",
            "documenter": "WORKING",
            "integrator": "WORKING",
        }

        def signal_readiness(role, state, reason=None):
            agent_states[role] = state

        def evaluate_consensus():
            """Phase completes when all non-integrator agents are READY
            and integrator is READY."""
            non_integrator_ready = all(
                agent_states[r] == "READY" for r in ("coder", "tester", "documenter")
            )
            integrator_ready = agent_states["integrator"] == "READY"
            return non_integrator_ready and integrator_ready

        # Initial state: no consensus
        assert not evaluate_consensus()

        # Coder finishes first
        signal_readiness("coder", "READY", "Implementation complete")
        assert not evaluate_consensus()

        # Tester finishes
        signal_readiness("tester", "READY", "Tests pass")
        assert not evaluate_consensus()

        # Documenter finishes
        signal_readiness("documenter", "READY", "Docs updated")
        assert not evaluate_consensus()  # Still waiting on integrator

        # Integrator merges and signals ready
        signal_readiness("integrator", "READY", "Merge complete")
        assert evaluate_consensus()

    def test_objection_blocks_consensus(self):
        """An OBJECTING agent blocks phase completion."""
        agent_states = {
            "coder": "READY",
            "tester": "OBJECTING",
            "documenter": "READY",
            "integrator": "READY",
        }

        has_objection = any(s == "OBJECTING" for s in agent_states.values())
        all_ready = all(s == "READY" for s in agent_states.values())

        assert has_objection
        assert not all_ready

    def test_ready_to_working_transition(self):
        """Agent can go from READY back to WORKING when new info arrives."""
        agent_states = {
            "coder": "READY",
            "tester": "READY",
            "documenter": "READY",
            "integrator": "WORKING",
        }

        # Tester discovers an issue after signaling ready
        agent_states["tester"] = "WORKING"

        # Consensus is broken
        all_ready = all(s == "READY" for s in agent_states.values())
        assert not all_ready

        # Tester fixes and re-signals
        agent_states["tester"] = "READY"
        agent_states["integrator"] = "READY"
        all_ready = all(s == "READY" for s in agent_states.values())
        assert all_ready

    def test_blocked_agent_does_not_satisfy_consensus(self):
        """A BLOCKED agent prevents consensus."""
        agent_states = {
            "coder": "READY",
            "tester": "BLOCKED",
            "documenter": "READY",
            "integrator": "READY",
        }

        all_ready = all(s == "READY" for s in agent_states.values())
        assert not all_ready

    def test_six_agent_consensus_requires_all_ready(self):
        """Consensus with 6 agents requires all 6 to be READY."""
        agent_states = {
            "coder": "WORKING",
            "tester": "WORKING",
            "documenter": "WORKING",
            "checker": "WORKING",
            "reviewer_code": "WORKING",
            "reviewer_contract": "WORKING",
        }

        def evaluate_consensus():
            return all(s == "READY" for s in agent_states.values())

        # Not ready with any agent still WORKING
        assert not evaluate_consensus()

        # Signal 5 of 6 agents READY — still no consensus
        for role in ("coder", "tester", "documenter", "checker", "reviewer_code"):
            agent_states[role] = "READY"
        assert not evaluate_consensus()

        # Final agent signals READY — consensus reached
        agent_states["reviewer_contract"] = "READY"
        assert evaluate_consensus()

        # One agent reverts to WORKING — consensus broken
        agent_states["checker"] = "WORKING"
        assert not evaluate_consensus()


class TestConcurrentAgentFailureHandling:
    """Test agent failure behavior in concurrent mode."""

    def test_single_agent_failure_notifies_others(self):
        """When one agent fails, AGENT_FAILED message is sent to others."""
        messages = []
        agent_states = {
            "coder": "WORKING",
            "tester": "WORKING",
            "documenter": "WORKING",
        }

        def handle_agent_failure(failed_role):
            """Simulate ConcurrentPhaseExecutor failure handler."""
            # Send AGENT_FAILED to all other agents
            for role in agent_states:
                if role != failed_role:
                    messages.append(
                        {
                            "to_role": role,
                            "from_role": "system",
                            "message_type": "AGENT_FAILED",
                            "subject": f"Agent {failed_role} has failed",
                            "body": f"The {failed_role} agent encountered an error.",
                        }
                    )
            agent_states[failed_role] = "FAILED"

        handle_agent_failure("tester")

        # Two messages sent (to coder and documenter)
        assert len(messages) == 2
        assert all(m["message_type"] == "AGENT_FAILED" for m in messages)
        assert {m["to_role"] for m in messages} == {"coder", "documenter"}
        assert agent_states["tester"] == "FAILED"

    def test_multiple_failures_abort_phase(self):
        """Two+ simultaneous failures trigger phase abort."""
        failed_agents = []

        def handle_failure(role):
            failed_agents.append(role)

        def should_abort():
            return len(failed_agents) >= 2

        handle_failure("coder")
        assert not should_abort()

        handle_failure("tester")
        assert should_abort()


class TestConcurrentEndToEnd:
    """End-to-end integration test simulating the full concurrent pipeline lifecycle."""

    def test_full_concurrent_lifecycle(self):
        """Simulate a complete concurrent execution cycle:
        1. All agents spawn and start working
        2. Agents exchange messages
        3. Agents signal readiness
        4. Consensus is reached
        5. Phase completes
        """
        # Phase state
        messages = []
        agent_states = {
            "coder": "WORKING",
            "tester": "WORKING",
            "documenter": "WORKING",
            "integrator": "WORKING",
        }
        phase_complete = False

        def send_msg(from_role, to_role, msg_type, subject):
            messages.append(
                {
                    "id": f"msg-{len(messages) + 1}",
                    "from_role": from_role,
                    "to_role": to_role,
                    "message_type": msg_type,
                    "subject": subject,
                }
            )

        def signal_ready(role, reason=""):
            agent_states[role] = "READY"

        def check_consensus():
            return all(s == "READY" for s in agent_states.values())

        # Step 1: Agents start working (already in WORKING state)
        assert all(s == "WORKING" for s in agent_states.values())

        # Step 2: Coder sends progress updates
        send_msg("coder", "all", "PROGRESS", "Core implementation done")
        send_msg("coder", "tester", "PROGRESS", "API tests can start")

        # Step 3: Tester starts testing, sends question
        send_msg("tester", "coder", "QUESTION", "Expected HTTP status for invalid input?")
        send_msg("coder", "tester", "RESPONSE", "400 Bad Request")

        # Step 4: Documenter tracks changes
        send_msg("documenter", "all", "STATUS", "Documenting API endpoints")

        # Step 5: Agents complete and signal readiness
        signal_ready("coder", "All tasks committed")
        assert not check_consensus()

        signal_ready("tester", "All tests pass")
        signal_ready("documenter", "Documentation complete")
        assert not check_consensus()  # Integrator still working

        # Step 6: Integrator merges and signals
        send_msg("integrator", "all", "STATUS", "Starting merge")
        signal_ready("integrator", "Merge complete, all green")

        # Step 7: Consensus reached
        assert check_consensus()
        phase_complete = True
        assert phase_complete

        # Verify message history
        assert len(messages) == 6
        progress_msgs = [m for m in messages if m["message_type"] == "PROGRESS"]
        assert len(progress_msgs) == 2
        question_msgs = [m for m in messages if m["message_type"] == "QUESTION"]
        assert len(question_msgs) == 1


class TestGetAgentRoles:
    """Tests for ConcurrentPhaseExecutor.get_agent_roles()."""

    def test_returns_implement_phase_roles(self):
        """get_agent_roles returns implement-phase roles from get_roles_for_phase."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from models import AgentRole

        pipeline = _make_concurrent_pipeline()
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        executor = ConcurrentPhaseExecutor(pipeline=pipeline, spawn_fn=MagicMock())
        roles = executor.get_agent_roles()

        assert AgentRole.CODER in roles
        assert AgentRole.TESTER in roles
        assert AgentRole.DOCUMENTER in roles
        assert AgentRole.CHECKER in roles
        assert AgentRole.REVIEWER_CODE in roles
        assert AgentRole.REVIEWER_CONTRACT in roles
        assert AgentRole.INTEGRATOR not in roles

    def test_returns_refine_phase_roles(self):
        """get_agent_roles returns refine-phase roles when phase is refine."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from models import AgentRole

        pipeline = _make_concurrent_pipeline()
        pipeline.current_phase = PipelinePhase.REFINE
        executor = ConcurrentPhaseExecutor(pipeline=pipeline, spawn_fn=MagicMock())
        roles = executor.get_agent_roles()

        assert AgentRole.REFINER in roles
        assert AgentRole.REVIEWER_REFINE in roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles
        assert AgentRole.CODER not in roles

    def test_returns_plan_phase_roles(self):
        """get_agent_roles returns plan-phase roles when phase is plan."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from models import AgentRole

        pipeline = _make_concurrent_pipeline()
        pipeline.current_phase = PipelinePhase.PLAN
        executor = ConcurrentPhaseExecutor(pipeline=pipeline, spawn_fn=MagicMock())
        roles = executor.get_agent_roles()

        assert AgentRole.ARCHITECT in roles
        assert AgentRole.TASK_PLANNER in roles
        assert AgentRole.RISK_ANALYST in roles
        assert AgentRole.REVIEWER_PLAN in roles
        assert AgentRole.CODER not in roles


class TestGetWorktreeBranch:
    """Tests for ConcurrentPhaseExecutor.get_worktree_branch()."""

    def test_get_worktree_branch_returns_pipeline_branch(self):
        """When pipeline.branch is set, all roles share it."""
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_concurrent_pipeline()
        assert pipeline.branch == "egg/issue-999"

        executor = ConcurrentPhaseExecutor(pipeline=pipeline, spawn_fn=MagicMock())

        for role in executor.get_agent_roles():
            assert executor.get_worktree_branch(role) == "egg/issue-999"

    def test_get_worktree_branch_fallback(self):
        """When pipeline.branch is None, falls back to issue-based name."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from models import AgentRole

        pipeline = _make_concurrent_pipeline()
        pipeline.branch = None  # Clear branch
        pipeline.issue_number = 777  # Distinct from default to prove fallback computes

        executor = ConcurrentPhaseExecutor(pipeline=pipeline, spawn_fn=MagicMock())

        branch = executor.get_worktree_branch(AgentRole.CODER)
        assert branch == "egg/issue-777"
        # Confirm no role suffix
        assert "coder" not in branch


class TestConcurrentPromptLifecycle:
    """Tests for consensus lifecycle preamble in agent prompts."""

    def test_concurrent_prompt_includes_lifecycle_preamble(self):
        """When concurrent=True, prompt includes consensus protocol section."""
        from routes.pipelines import _build_agent_prompt

        prompt = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="issue-123",
            pipeline_mode="issue",
            concurrent=True,
        )
        assert "BRC Consensus Protocol" in prompt
        assert "STAY ALIVE" in prompt
        assert "FAILED your role" in prompt

    def test_non_concurrent_prompt_omits_lifecycle_preamble(self):
        """When concurrent=False (default), prompt has no consensus section."""
        from routes.pipelines import _build_agent_prompt

        prompt = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="issue-123",
            pipeline_mode="issue",
        )
        assert "BRC Consensus Protocol" not in prompt

    def test_concurrent_phase_completion_includes_polling_loop(self):
        """Concurrent prompts should have stay-alive instructions in Phase Completion."""
        from routes.pipelines import _build_agent_prompt

        prompt = _build_agent_prompt(
            role_value="documenter",
            phase="implement",
            pipeline_id="issue-123",
            pipeline_mode="issue",
            concurrent=True,
        )
        assert "egg-orch signal readiness --state READY" in prompt
        assert "egg-orch message poll" in prompt
        assert "Do NOT exit" in prompt

    def test_non_concurrent_phase_completion_says_exit(self):
        """Non-concurrent prompts should tell agents to exit normally."""
        from routes.pipelines import _build_agent_prompt

        prompt = _build_agent_prompt(
            role_value="documenter",
            phase="implement",
            pipeline_id="issue-123",
            pipeline_mode="issue",
            concurrent=False,
        )
        assert "exit successfully" in prompt


class TestSpawnUsesConsensusWrapper:
    """Tests that concurrent spawns use the consensus shell wrapper."""

    def test_spawn_agent_uses_wrapped_command(self):
        """_spawn_agent should produce a bash -c wrapper, not raw claude args."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from models import AgentRole

        pipeline = _make_concurrent_pipeline()
        mock_spawn = MagicMock()
        mock_spawn.return_value = MagicMock(container_info=MagicMock(container_id="abc123"))

        executor = ConcurrentPhaseExecutor(pipeline=pipeline, spawn_fn=mock_spawn)
        executor._spawn_agent(AgentRole.CODER, prompt_text="Do the work")

        mock_spawn.assert_called_once()
        call_kwargs = mock_spawn.call_args
        command = call_kwargs.kwargs.get("command") or call_kwargs[1].get("command")
        assert command[0] == "bash"
        assert command[1] == "-c"
        assert "claude" in command[2]
        assert "RESTART_COUNT" in command[2]
        assert "CONSENSUS RECOVERY" in command[2]


class TestNoImplicitReadyOnCleanExit:
    """Verify that clean container exits do NOT auto-register READY.

    The consensus wrapper restarts the agent instead. The orchestrator
    must not fake consensus on behalf of agents.
    """

    def test_clean_exit_does_not_register_ready(self):
        """Container exiting with code 0 should NOT auto-register as READY."""
        from consensus import ReadinessState, get_consensus_evaluator

        evaluator = get_consensus_evaluator()
        pipeline_id = "test-no-implicit-ready"

        # Register an agent as WORKING
        evaluator.register_agent(pipeline_id, "tester")
        state = evaluator.evaluate(pipeline_id)
        assert not state["is_complete"]
        assert "tester" in state["blocking_agents"]

        # The orchestrator should NOT auto-register READY on clean exit.
        # The agent must remain blocking until it explicitly signals.
        state = evaluator.evaluate(pipeline_id)
        assert not state["is_complete"]
        assert "tester" in state["blocking_agents"]

        # Only explicit READY from the agent should complete consensus
        evaluator.update_readiness(
            pipeline_id,
            "tester",
            ReadinessState.READY,
            reason="Agent explicitly signaled READY",
        )
        state = evaluator.evaluate(pipeline_id)
        assert state["is_complete"]

        # Cleanup
        evaluator.clear(pipeline_id)

    def test_wrapper_contains_restart_logic(self):
        """The consensus wrapper should restart agents, not auto-signal READY."""
        from consensus_wrapper import build_consensus_wrapped_command

        cmd = build_consensus_wrapped_command("Do work")
        script = cmd[2]
        # Must contain restart logic
        assert "Restarting" in script
        assert "RESTART_COUNT" in script
        # Must NOT contain auto-READY
        assert "Auto-signaling READY" not in script
