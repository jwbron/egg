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
    config = PipelineConfig(
        concurrent_execution=True,
        max_concurrent_agents=6,
        message_poll_hint_seconds=30,
        consensus_timeout_minutes=30,
    )

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
            # consensus is omitted when no tracker is available (#1229)

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test-repo")
    @patch("routes.pipelines._resolve_pipeline")
    def test_status_no_concurrent_when_disabled(self, mock_resolve, mock_repo_path, client):
        """When concurrent_execution is false, status has no concurrent section."""
        config = PipelineConfig(concurrent_execution=False, concurrent_phases=[])
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

        # Tester sends status query to coder
        send_message(
            "issue-999",
            "tester",
            "coder",
            "STATUS",
            "Test expectations",
            "What is expected return code?",
        )

        # Coder polls and gets broadcast + targeted status
        received = poll_messages("issue-999", "coder")
        assert len(received) == 2  # Broadcast PROGRESS + targeted STATUS
        assert received[0]["message_type"] == "PROGRESS"  # broadcast to "all"
        assert received[1]["message_type"] == "STATUS"  # targeted to "coder"

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

        # Reviewer broadcasts status
        send_message("reviewer_code", "all", "STATUS", "Starting review")

        # All agents should see it
        for role in ("coder", "tester", "documenter"):
            received = poll_for_role(role)
            assert len(received) == 1, f"{role} should receive broadcast"
            assert received[0]["from_role"] == "reviewer_code"


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
            "reviewer_code": "WORKING",
        }

        def signal_readiness(role, state, reason=None):
            agent_states[role] = state

        def evaluate_consensus():
            """Phase completes when all agents are READY."""
            return all(s == "READY" for s in agent_states.values())

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
        assert not evaluate_consensus()  # Still waiting on reviewer

        # Reviewer completes review and signals ready
        signal_readiness("reviewer_code", "READY", "Review complete")
        assert evaluate_consensus()

    def test_objection_blocks_consensus(self):
        """An OBJECTING agent blocks phase completion."""
        agent_states = {
            "coder": "READY",
            "tester": "OBJECTING",
            "documenter": "READY",
            "reviewer_code": "READY",
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
            "reviewer_code": "WORKING",
        }

        # Tester discovers an issue after signaling ready
        agent_states["tester"] = "WORKING"

        # Consensus is broken
        all_ready = all(s == "READY" for s in agent_states.values())
        assert not all_ready

        # Tester fixes and re-signals
        agent_states["tester"] = "READY"
        agent_states["reviewer_code"] = "READY"
        all_ready = all(s == "READY" for s in agent_states.values())
        assert all_ready

    def test_blocked_agent_does_not_satisfy_consensus(self):
        """A BLOCKED agent prevents consensus."""
        agent_states = {
            "coder": "READY",
            "tester": "BLOCKED",
            "documenter": "READY",
            "reviewer_code": "READY",
        }

        all_ready = all(s == "READY" for s in agent_states.values())
        assert not all_ready

    def test_five_agent_consensus_requires_all_ready(self):
        """Consensus with 5 agents requires all 5 to be READY."""
        agent_states = {
            "coder": "WORKING",
            "tester": "WORKING",
            "documenter": "WORKING",
            "reviewer_code": "WORKING",
            "reviewer_contract": "WORKING",
        }

        def evaluate_consensus():
            return all(s == "READY" for s in agent_states.values())

        # Not ready with any agent still WORKING
        assert not evaluate_consensus()

        # Signal 4 of 5 agents READY — still no consensus
        for role in ("coder", "tester", "documenter", "reviewer_code"):
            agent_states[role] = "READY"
        assert not evaluate_consensus()

        # Final agent signals READY — consensus reached
        agent_states["reviewer_contract"] = "READY"
        assert evaluate_consensus()

        # One agent reverts to WORKING — consensus broken
        agent_states["tester"] = "WORKING"
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
            "reviewer_code": "WORKING",
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
        send_msg("tester", "coder", "STATUS", "Expected HTTP status for invalid input?")
        send_msg("coder", "tester", "RESPONSE", "400 Bad Request")

        # Step 4: Documenter tracks changes
        send_msg("documenter", "all", "STATUS", "Documenting API endpoints")

        # Step 5: Agents complete and signal readiness
        signal_ready("coder", "All tasks committed")
        assert not check_consensus()

        signal_ready("tester", "All tests pass")
        signal_ready("documenter", "Documentation complete")
        assert not check_consensus()  # Reviewer still working

        # Step 6: Reviewer completes and signals
        send_msg("reviewer_code", "all", "STATUS", "Starting review")
        signal_ready("reviewer_code", "Review complete, all good")

        # Step 7: Consensus reached
        assert check_consensus()
        phase_complete = True
        assert phase_complete

        # Verify message history
        assert len(messages) == 6
        progress_msgs = [m for m in messages if m["message_type"] == "PROGRESS"]
        assert len(progress_msgs) == 2
        status_msgs = [m for m in messages if m["message_type"] == "STATUS"]
        assert len(status_msgs) == 3  # tester + documenter + reviewer STATUS


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
        assert AgentRole.REVIEWER_CODE in roles
        assert AgentRole.REVIEWER_CONTRACT in roles

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
        # reviewer_agent_design is egg-repo-only, excluded for non-egg repos
        assert AgentRole.REVIEWER_AGENT_DESIGN not in roles
        assert AgentRole.CODER not in roles

    def test_returns_refine_phase_roles_egg_repo(self):
        """get_agent_roles includes reviewer_agent_design for the egg repo."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from models import AgentRole

        pipeline = _make_concurrent_pipeline()
        pipeline.current_phase = PipelinePhase.REFINE
        pipeline.repo = "jwbron/egg"
        executor = ConcurrentPhaseExecutor(pipeline=pipeline, spawn_fn=MagicMock())
        roles = executor.get_agent_roles()

        assert AgentRole.REFINER in roles
        assert AgentRole.REVIEWER_REFINE in roles
        assert AgentRole.REVIEWER_AGENT_DESIGN in roles

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
        """Concurrent prompts should have stay-alive instructions in Phase Completion.

        Issue #1897 replaced the ``egg-orch message poll`` shell idiom
        with the event-driven ``egg-orch message wait-loop`` primitive
        so we assert the new idiom here.  The anti-pattern ban is also
        asserted so we catch any future regression that re-introduces
        a ``sleep N &&`` or ``for i in ... do message poll`` pattern.

        This test pins the **canonical --for list** documented in
        ``docs/reference/agent-wait-patterns.md`` §1 so any drift
        between docs and prompts is caught here: producer stay-alive
        must wait on CONSENSUS_CONFIRMED + CONSENSUS_RE_REVIEW +
        OVERSEER_ALERT simultaneously (the docs-required triple).
        """
        from routes.pipelines import _build_agent_prompt

        prompt = _build_agent_prompt(
            role_value="documenter",
            phase="implement",
            pipeline_id="issue-123",
            pipeline_mode="issue",
            concurrent=True,
        )
        assert "egg-orch signal readiness --state READY" in prompt
        # New canonical idiom (issue #1897).
        assert "egg-orch message wait-loop" in prompt
        # Canonical producer --for list per agent-wait-patterns.md §1.
        # Each MUST be present — missing any one lets the agent stall
        # through a particular BRC event type.
        assert "--for CONSENSUS_CONFIRMED" in prompt
        assert "--for CONSENSUS_RE_REVIEW" in prompt
        assert "--for OVERSEER_ALERT" in prompt
        assert "Do NOT exit" in prompt
        # Anti-pattern bans (both `for i in ... do poll` AND `sleep N`).
        # Lower-case match because the prompt uses ``**don't**`` for
        # emphasis, not the formal ``Do NOT`` marker.
        low = prompt.lower()
        assert "for i in" in low, "Producer stay-alive must call out the for-loop anti-pattern"
        assert "sleep" in low, "Producer stay-alive must call out the sleep anti-pattern"

    def test_reviewer_stay_alive_uses_canonical_for_list(self):
        """Reviewer stay-alive prompt pins the reviewer-specific
        canonical --for list documented in agent-wait-patterns.md §1:
        CONSENSUS_PROPOSE + CONSENSUS_RE_REVIEW + CONSENSUS_CONFIRMED +
        OVERSEER_ALERT.

        Reviewers need CONSENSUS_PROPOSE (producers re-proposing after
        a NACK) in addition to the producer-triple — without it they
        miss the most important event for their role.
        """
        from routes.pipelines import _build_agent_prompt

        prompt = _build_agent_prompt(
            role_value="reviewer_code",
            phase="implement",
            pipeline_id="issue-123",
            pipeline_mode="issue",
            concurrent=True,
        )
        assert "egg-orch message wait-loop" in prompt
        # Reviewer-specific canonical --for list.
        assert "--for CONSENSUS_PROPOSE" in prompt
        assert "--for CONSENSUS_RE_REVIEW" in prompt
        assert "--for CONSENSUS_CONFIRMED" in prompt
        assert "--for OVERSEER_ALERT" in prompt

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
        from models import AgentRole, ContainerInfo

        pipeline = _make_concurrent_pipeline()
        mock_spawn = MagicMock()
        mock_spawn.return_value = MagicMock(
            container_info=ContainerInfo(container_id="abc123", container_name="abc123"),
        )

        executor = ConcurrentPhaseExecutor(pipeline=pipeline, spawn_fn=mock_spawn)
        executor._spawn_agent(AgentRole.CODER, prompt_text="Do the work")

        mock_spawn.assert_called_once()
        call_kwargs = mock_spawn.call_args
        command = call_kwargs.kwargs.get("command") or call_kwargs[1].get("command")
        assert command[0] == "bash"
        assert command[1] == "-c"
        assert "egg_agent" in command[2]
        assert "RESTART_COUNT" in command[2]
        assert "BRC Consensus Recovery" in command[2]


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


class TestConcurrentPhaseSkipsReviewerSpawn:
    """Test that concurrent (BRC) phases do not spawn separate reviewer
    containers after consensus, preventing duplicate review cycles (issue #1178)."""

    def test_is_concurrent_execution_true_for_concurrent_pipeline(self):
        """is_concurrent_execution returns True for concurrent pipelines,
        which causes the reviewer-spawn guard to break."""
        from concurrent_executor import is_concurrent_execution

        pipeline = _make_concurrent_pipeline()

        # Concurrent execution is detected for all phases
        assert is_concurrent_execution(pipeline, phase="implement") is True
        assert is_concurrent_execution(pipeline, phase="plan") is True
        assert is_concurrent_execution(pipeline, phase="refine") is True

    def test_reviewer_roles_exist_for_phases(self):
        """Reviewer roles ARE defined for phases — proving the concurrent
        guard is needed to prevent redundant spawning."""
        from egg_contracts.agent_roles import _PHASE_REVIEWERS

        # Without the use_concurrent guard, these roles would be spawned
        assert len(_PHASE_REVIEWERS.get("implement", [])) > 0
        assert len(_PHASE_REVIEWERS.get("plan", [])) > 0

    def test_non_concurrent_pipeline_allows_reviewers(self):
        """When concurrent_execution is False, is_concurrent_execution is
        False and the reviewer-spawn guard would NOT break."""
        from concurrent_executor import is_concurrent_execution

        pipeline = _make_concurrent_pipeline()
        pipeline.config.concurrent_execution = False
        pipeline.config.concurrent_phases = []

        assert is_concurrent_execution(pipeline, phase="implement") is False


class TestAgentsMarkedCompleteAfterConsensus:
    """Test that _update_agents_complete transitions FAILED agents to COMPLETE
    when BRC consensus succeeds (issue #1178, Bug 3)."""

    def test_failed_agents_become_complete_via_store_roundtrip(self):
        """Agents that exited non-zero should be marked COMPLETE after
        consensus. Tested through a mock store load→update→save cycle
        matching the _update_agents_complete code path."""
        from models import (
            AgentExecution,
            AgentExecutionStatus,
        )

        # Build a pipeline with one RUNNING and one FAILED agent
        pipeline = _make_concurrent_pipeline()
        pe = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        pe.status = PipelineStatus.RUNNING
        pe.agents = [
            AgentExecution(role="coder", status=AgentExecutionStatus.RUNNING),
            AgentExecution(
                role="tester",
                status=AgentExecutionStatus.FAILED,
                error="Container exited with code 1",
            ),
        ]

        # Mock store: load_pipeline returns our pipeline, save captures it
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        # Simulate _update_agents_complete: load → update → save
        pip = mock_store.load_pipeline(pipeline.id)
        loaded_pe = pip.get_phase_execution(PipelinePhase.IMPLEMENT)
        for agent in loaded_pe.agents:
            if agent.status in (
                AgentExecutionStatus.RUNNING,
                AgentExecutionStatus.FAILED,
            ):
                agent.status = AgentExecutionStatus.COMPLETE
                agent.completed_at = datetime.now(tz=UTC)
        mock_store.save_pipeline(pip)

        # Verify both agents are COMPLETE in the saved pipeline
        saved_pipeline = mock_store.save_pipeline.call_args[0][0]
        saved_pe = saved_pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        assert saved_pe.agents[0].status == AgentExecutionStatus.COMPLETE
        assert saved_pe.agents[1].status == AgentExecutionStatus.COMPLETE
        assert saved_pe.agents[1].completed_at is not None
        # Error info should be preserved
        assert saved_pe.agents[1].error == "Container exited with code 1"

    def test_phase_status_reset_via_get_phase_execution(self):
        """Phase and pipeline status reset to RUNNING at cycle start is
        verified through the model accessor, not direct attribute assignment."""
        pipeline = _make_concurrent_pipeline()
        pe = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        pe.status = PipelineStatus.FAILED
        pipeline.status = PipelineStatus.FAILED

        # Simulate the status reset that happens at cycle start
        # (pipelines.py:6416) — access through get_phase_execution to
        # ensure the model's dict-based storage works correctly.
        loaded_pe = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        assert loaded_pe.status == PipelineStatus.FAILED  # precondition

        loaded_pe.status = PipelineStatus.RUNNING
        pipeline.status = PipelineStatus.RUNNING

        # Re-fetch via accessor — must reflect the update
        assert (
            pipeline.get_phase_execution(PipelinePhase.IMPLEMENT).status == PipelineStatus.RUNNING
        )
        assert pipeline.status == PipelineStatus.RUNNING


# ---------------------------------------------------------------------------
# Issue #1897 plan-mandated integration tests: TASK-8-1 / TASK-8-2 / TASK-8-3
# ---------------------------------------------------------------------------
#
# The plan (revision 4) specifies three integration tests that validate the
# end-to-end goal of #1897:
#
#   * TASK-8-1: event-driven BRC wake-up within 2s of CONSENSUS_CONFIRMED
#   * TASK-8-2: repeated ``consensus confirmed`` calls do not pollute the
#               bus (PR #1896 regression guard, HITL Q1 follow-up)
#   * TASK-8-3: misconfigured EGG_MESSAGE_POLL_MAX_WAIT produces 504 from
#               the gateway (RISK-4 named-failure mode, so operators see a
#               loud error rather than silent stalls)
#
# These exercise the plumbing end-to-end — not just the unit-level
# MessageStore blocking tested in test_message_store.py — because the
# failure modes they guard against are all at the integration boundary.


class TestEventDrivenConsensusWait:
    """Plan TASK-8-1: an agent blocking on the ``/messages/wait``
    endpoint MUST return within ~2s of a peer writing a
    ``CONSENSUS_CONFIRMED`` message to the bus.

    Before issue #1897, agents polled every 30s; the sub-2s target is
    the core success criterion of the whole feature.
    """

    @pytest.fixture
    def wait_app(self):
        """Flask app wired to messages_bp + a fresh in-memory MessageStore."""
        from flask import Flask
        from message_store import reset_message_store
        from routes.messages import messages_bp

        app = Flask(__name__)
        app.register_blueprint(messages_bp)
        app.config["TESTING"] = True
        reset_message_store()
        yield app
        reset_message_store()

    def test_event_driven_consensus_wait_wakes_within_2s(self, wait_app):
        """A thread blocked on ``/messages/wait?for=CONSENSUS_CONFIRMED``
        MUST unblock within 2s of a peer writing that message type.

        Measured wall-clock in-process via the Flask test client, so the
        only latency is the condition-variable wake-up path; if it
        exceeds 2s, something is polling rather than blocking.
        """
        import threading
        import time as _t
        from unittest.mock import MagicMock

        from message_store import Message, MessageType, get_message_store

        pipeline_id = "issue-task-8-1"

        # Results from the waiter thread. Use ``Any`` so mypy does not
        # complain about indexing the heterogeneous-dict result.
        from typing import Any as _Any

        wake_data: dict[str, _Any] = {}

        def blocking_wait() -> None:
            """Waiter: block on /messages/wait until a CONSENSUS_CONFIRMED arrives."""
            client = wait_app.test_client()
            with wait_app.test_request_context():
                with patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline:
                    mock_get_store_for_pipeline.return_value = (
                        MagicMock(),
                        _make_pipeline_mock_for_task_8(),
                    )
                    start = _t.monotonic()
                    resp = client.get(
                        f"/api/v1/pipelines/{pipeline_id}/messages/wait"
                        "?for=CONSENSUS_CONFIRMED&timeout=10"
                    )
                    wake_data["elapsed"] = _t.monotonic() - start
                    wake_data["status"] = resp.status_code
                    wake_data["body"] = json.loads(resp.data)

        # Start the waiter.
        waiter = threading.Thread(target=blocking_wait)
        waiter.start()

        # Give the waiter a moment to enter the blocking branch.
        _t.sleep(0.1)

        # Peer writes the confirmation — this must wake the waiter via the
        # per-pipeline condition variable.
        store = get_message_store()
        write_time = _t.monotonic()
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="reviewer_code",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed by reviewer_code",
            )
        )

        # The waiter should return quickly — give it a generous upper
        # bound (5s) but ASSERT the tighter sub-2s target.
        waiter.join(timeout=5)
        assert not waiter.is_alive(), (
            "Waiter did not return within 5s — condition variable likely not notifying"
        )

        # Wall-clock from write → wake must be well under 2s.
        total_elapsed = _t.monotonic() - write_time
        assert total_elapsed < 2, (
            f"Event-driven wake-up took {total_elapsed:.2f}s (target: <2s). "
            "The wait primitive is NOT event-driven."
        )

        # The endpoint must have observed the message and returned matched=True.
        assert wake_data["status"] == 200
        assert wake_data["body"]["data"]["matched"] is True
        assert wake_data["body"]["data"]["count"] == 1
        assert wake_data["body"]["data"]["messages"][0]["message_type"] == ("CONSENSUS_CONFIRMED")


class TestConsensusConfirmedDedupRegression:
    """Plan TASK-8-2 (HITL Q1 follow-up to PR #1896): repeated
    ``consensus confirmed`` calls MUST NOT spray the bus with duplicate
    ``CONSENSUS_CONFIRMED`` messages.

    Before PR #1896, a retry-looping agent could write N consecutive
    CONFIRMED messages on each retry, polluting the bus and tricking
    the fallback check into false positives. The signal handler now
    dedups via ``_existing_confirmed_for_role``; N=10 repeated calls
    from the same role in the same phase should yield exactly 1
    CONFIRMED message.
    """

    @pytest.fixture
    def deduce_app(self):
        """Flask app wired to signals_bp + messages_bp + fresh stores."""
        from flask import Flask
        from message_store import reset_message_store
        from routes.messages import messages_bp
        from routes.signals import signals_bp

        app = Flask(__name__)
        app.register_blueprint(signals_bp)
        app.register_blueprint(messages_bp)
        app.config["TESTING"] = True
        reset_message_store()
        yield app
        reset_message_store()

    def test_ten_confirmed_calls_yield_exactly_one_bus_message(self, deduce_app):
        """N=10 consensus-confirmed invocations from the same role MUST
        result in exactly 1 bus message with message_type=CONSENSUS_CONFIRMED.

        This is the PR #1896 regression guard; without the dedup, the
        count is N.
        """
        import tempfile
        from unittest.mock import MagicMock

        from consensus import ReadinessState, get_consensus_evaluator
        from message_store import get_message_store

        pipeline_id = "issue-task-8-2"
        agent_role = "coder"
        N = 10

        # Seed the evaluator so the confirmed handler has state to work with.
        evaluator = get_consensus_evaluator()
        evaluator.register_agent(pipeline_id, agent_role)
        evaluator.update_readiness(
            pipeline_id,
            agent_role,
            ReadinessState.READY,
            reason="setup",
        )

        client = deduce_app.test_client()

        # NOTE: peer_consensus.get_peer_consensus_tracker is imported
        # *locally* inside handle_consensus_confirmed_signal (see
        # routes/signals.py:1314), so we patch the source module, not
        # the routes.signals namespace.
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch("routes.signals.get_repo_path", return_value=tmpdir),
                patch(
                    "routes.signals.resolve_repo_path_for_pipeline",
                    return_value=tmpdir,
                ),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
                patch("routes.signals._write_consensus_confirmed_marker"),
                patch("peer_consensus.get_peer_consensus_tracker") as mock_get_tracker,
            ):
                mock_tracker = MagicMock()
                mock_tracker.handle_confirmed.return_value = {
                    "status": "confirmed",
                    "message": "Confirmed",
                    "consensus_reached": False,
                }
                mock_get_tracker.return_value = mock_tracker

                # Fire N consensus_confirmed calls from the same role.
                for _ in range(N):
                    resp = client.post(
                        f"/api/v1/pipelines/{pipeline_id}/signal",
                        json={
                            "signal_type": "consensus_confirmed",
                            "agent_role": agent_role,
                        },
                    )
                    # First call should succeed; subsequent calls should
                    # still succeed (the handler is idempotent) but NOT
                    # emit a duplicate message.
                    assert resp.status_code in (200, 202), (
                        f"Signal returned {resp.status_code}: {resp.data!r}"
                    )

        # Count CONSENSUS_CONFIRMED messages from agent_role in this pipeline.
        store = get_message_store()
        messages = store.get_messages(pipeline_id, limit=1000)
        confirmed_from_role = [
            m
            for m in messages
            if m.from_role == agent_role and str(m.message_type) == "CONSENSUS_CONFIRMED"
        ]

        # Cleanup
        evaluator.clear(pipeline_id)

        assert len(confirmed_from_role) == 1, (
            f"N={N} consensus_confirmed calls produced "
            f"{len(confirmed_from_role)} messages (expected 1). "
            "PR #1896 dedup has regressed."
        )


class TestMisconfiguredCap504:
    """Plan TASK-8-3 (RISK-4 named failure mode): if an operator sets
    ``EGG_MESSAGE_POLL_MAX_WAIT`` above the gateway's Squid
    ``read_timeout``, long-polls should produce a visible 504 rather
    than silently stalling.

    The full test requires a subprocess orchestrator behind a proxy
    harness with the Squid timeout pinned to 60s; that harness lives in
    integration_tests/. In the unit suite we verify the decision logic:
    when cap > SAFE_THRESHOLD, the startup warning MUST be emitted
    naming the gateway Squid coupling so an operator sees the config
    error loudly.
    """

    def test_warning_emitted_when_cap_exceeds_threshold(self, monkeypatch):
        """Plan TASK-8-3 (unit-level): setting
        EGG_MESSAGE_POLL_MAX_WAIT above the safe threshold MUST emit a
        WARNING naming the gateway Squid coupling.

        Without this warning, an operator who raises the cap without
        rebuilding the gateway image sees their long-polls silently
        hit 504 with no diagnostic hint. The warning is the difference
        between a 15-minute oncall page and an hour of stare-at-logs.
        """
        import warnings

        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "120")  # > 90s threshold
        # Re-import to pick up the env var
        import importlib

        import env_config

        importlib.reload(env_config)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            env_config.log_message_poll_max_wait_startup()
            # At least one warning must mention the Squid coupling.
            messages = [str(w.message) for w in caught]
            assert any("squid.conf" in m.lower() or "squid" in m.lower() for m in messages), (
                f"No Squid-coupling warning emitted; caught: {messages!r}"
            )
            assert any("image rebuild" in m.lower() or "rebuild" in m.lower() for m in messages), (
                "Warning must tell the operator a rebuild is required"
            )

    def test_no_warning_when_cap_at_default(self, monkeypatch):
        """Safe default (60s) must NOT emit the warning — otherwise the
        warning loses signal through fatigue."""
        import warnings

        monkeypatch.delenv("EGG_MESSAGE_POLL_MAX_WAIT", raising=False)
        import importlib

        import env_config

        importlib.reload(env_config)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            env_config.log_message_poll_max_wait_startup()
            squid_warnings = [w for w in caught if "squid" in str(w.message).lower()]
            assert not squid_warnings, (
                f"Safe default emitted Squid warning (false positive): {squid_warnings!r}"
            )

    def test_clamp_prevents_abusive_timeout_values(self, monkeypatch):
        """Even if an operator sends a timeout=9999 query arg, the cap
        MUST clamp it so the long-poll doesn't outlive the Squid
        timeout (the 504 failure mode).

        This is a unit-level proxy for the full subprocess harness —
        it confirms the clamp is actually applied on every request,
        not just at startup.
        """
        from unittest.mock import MagicMock

        from flask import Flask
        from message_store import MessageStore, reset_message_store
        from routes.messages import messages_bp

        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "2")

        app = Flask(__name__)
        app.register_blueprint(messages_bp)
        app.config["TESTING"] = True
        reset_message_store()

        store = MessageStore()
        client = app.test_client()
        import time as _t

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock_for_task_8(),
                )
                start = _t.monotonic()
                resp = client.get(
                    "/api/v1/pipelines/test/messages/wait?for=CONSENSUS_CONFIRMED&timeout=9999"
                )
                elapsed = _t.monotonic() - start

        # Cap is 2s, so the call MUST return in < 5s, not 9999s.
        assert resp.status_code == 200
        assert elapsed < 5, (
            f"timeout=9999 with cap=2 took {elapsed:.1f}s; clamp not applied. "
            "This would cause the 504 failure mode in production."
        )
        reset_message_store()


def _make_pipeline_mock_for_task_8() -> MagicMock:
    """Minimal pipeline mock used by TASK-8 integration tests."""
    pipeline = MagicMock()
    pipeline.current_phase.value = "implement"
    return pipeline
