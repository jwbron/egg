"""Tests for BRC NACK iteration fixes (issue #1152).

Covers:
- evaluate() blocking on unresolved NACKs
- Polling loop returning failure when containers exit with unresolved NACKs
- Timeout returning failure when NACKs are unresolved
- Consensus wrapper NACK feedback extraction
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from peer_consensus import PeerConsensusTracker
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

# ---------- evaluate() tests ----------


@pytest.fixture
def simple_graph():
    """Simple 1-producer, 2-reviewer graph for testing."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("checker", "coder", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def tracker(simple_graph):
    t = PeerConsensusTracker("test-pipeline", simple_graph, cooldown_seconds=0)
    t.register_agent("coder")
    t.register_agent("reviewer_code")
    t.register_agent("checker")
    return t


class TestEvaluateWithNacks:
    """evaluate() must report unresolved NACKs and block is_complete."""

    def test_evaluate_reports_no_nacks_initially(self, tracker):
        state = tracker.evaluate()
        assert state["has_unresolved_nacks"] is False
        assert state["unresolved_nacks"] == []

    def test_evaluate_reports_nack_after_nack(self, tracker):
        tracker.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})
        tracker.handle_nack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["a.py"], "reason": "SQL injection in a.py:42"},
        )
        state = tracker.evaluate()
        assert state["has_unresolved_nacks"] is True
        assert state["is_complete"] is False
        assert len(state["unresolved_nacks"]) >= 1
        nack = state["unresolved_nacks"][0]
        assert nack["reviewer"] == "reviewer_code"
        assert nack["producer"] == "coder"
        assert "SQL injection" in nack["reason"]

    def test_evaluate_nack_blocks_completion_even_if_all_confirmed(self, tracker):
        """If all agents somehow end up in _confirmed but NACKs exist,
        is_complete must still be False."""
        tracker.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})

        # reviewer_code NACKs
        tracker.handle_nack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["a.py"], "reason": "bug"},
        )

        # checker ACKs
        tracker.handle_ack(
            "checker",
            "coder",
            {"artifact_references": ["a.py"]},
        )

        # Force agents into _confirmed set (simulating a bug where they
        # confirmed despite NACK)
        tracker._confirmed = {"coder", "reviewer_code", "checker"}

        state = tracker.evaluate()
        # Must still be incomplete because reviewer_code NACKed
        assert state["is_complete"] is False
        assert state["has_unresolved_nacks"] is True

    def test_evaluate_clears_nack_after_re_propose_and_ack(self, tracker):
        """After producer re-proposes and gets ACKed, NACKs should be resolved."""
        tracker.handle_propose("coder", {"summary": "v1", "artifacts": ["a.py"]})
        tracker.handle_nack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["a.py"], "reason": "bug"},
        )
        tracker.handle_ack(
            "checker",
            "coder",
            {"artifact_references": ["a.py"]},
        )

        # Re-propose
        tracker.handle_re_propose(
            "coder",
            {"summary": "v2", "artifacts": ["a.py"]},
            changed_artifacts=["a.py"],
        )

        # Both reviewers ACK the new version
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["a.py"]},
        )
        tracker.handle_ack(
            "checker",
            "coder",
            {"artifact_references": ["a.py"]},
        )

        state = tracker.evaluate()
        assert state["has_unresolved_nacks"] is False


# ---------- Polling loop tests ----------


def _make_concurrent_pipeline(pipeline_id="issue-999"):
    config = PipelineConfig()
    for key, val in {
        "concurrent_execution": True,
        "max_concurrent_agents": 4,
        "message_poll_hint_seconds": 30,
        "consensus_timeout_minutes": 30,
    }.items():
        try:
            setattr(config, key, val)
        except (AttributeError, ValueError):
            config.__dict__[key] = val

    return Pipeline(
        id=pipeline_id,
        issue_number=999,
        repo="owner/repo",
        branch="egg/issue-999",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_execution(role, container_id, status=AgentExecutionStatus.RUNNING):
    return AgentExecution(
        role=role,
        status=status,
        container_id=container_id,
        started_at=datetime.utcnow(),
    )


def _make_phase_execution():
    return PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )


def _base_mocks(executions, container_infos=None):
    pipeline = _make_concurrent_pipeline()
    phase_exec = _make_phase_execution()

    mock_store = MagicMock()
    mock_pipeline_state = MagicMock()
    mock_pipeline_state.get_phase_execution.return_value = phase_exec
    mock_store.load_pipeline.return_value = mock_pipeline_state

    mock_docker = MagicMock()

    if container_infos is None:
        container_infos = {}
        for e in executions:
            if e.container_id:
                container_infos[e.container_id] = ContainerInfo(
                    container_id=e.container_id,
                    container_name=f"issue-999-{e.role.value}",
                    status=ContainerStatus.RUNNING,
                    exit_code=None,
                )

    mock_docker.get_container_info.side_effect = lambda cid: container_infos[cid]

    mock_spawner = MagicMock()
    mock_spawner.docker = mock_docker
    mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

    return pipeline, mock_store, mock_spawner, mock_docker


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


class TestPollingLoopWithNacks:
    """When all containers exit cleanly but NACKs exist, return failure."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_containers_exit_with_unresolved_nacks_returns_failure(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """All containers exit code 0 but NACKs exist -> returns (1, ...)."""
        from routes.pipelines import _run_concurrent_phase

        mock_monotonic.return_value = 0.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.REVIEWER_CODE, "reviewer-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-999-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.utcnow(),
            ),
            "reviewer-1": ContainerInfo(
                container_id="reviewer-1",
                container_name="issue-999-reviewer_code",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.utcnow(),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(
            executions, container_infos=container_infos
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "has_unresolved_nacks": True,
            "unresolved_nacks": [
                {
                    "reviewer": "reviewer_code",
                    "producer": "coder",
                    "reason": "SQL injection in auth.py:42",
                    "version": 1,
                },
            ],
            "blocking_agents": ["coder", "reviewer_code"],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_add_decision = MagicMock(return_value=MagicMock(id="dec-1"))
        with patch.object(type(pipeline), "add_decision", mock_add_decision):
            exit_code, logs = _run_concurrent_phase(
                pipeline_id="issue-999",
                pipeline=pipeline,
                phase="implement",
                spawner=mock_spawner,
                store=mock_store,
                **_CALL_ARGS,
            )

        assert exit_code == 1
        assert "UNRESOLVED NACKs" in logs
        assert "SQL injection" in logs
        # HITL decision should have been created
        mock_add_decision.assert_called_once()

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_containers_exit_without_nacks_returns_success(
        self, MockExecutor, mock_prompt, mock_lock, mock_monotonic, mock_sleep
    ):
        """All containers exit code 0, no NACKs -> returns (0, ...)."""
        from routes.pipelines import _run_concurrent_phase

        mock_monotonic.return_value = 0.0

        executions = [_make_execution(AgentRole.CODER, "coder-1")]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-999-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.utcnow(),
            ),
        }

        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(
            executions, container_infos=container_infos
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "has_unresolved_nacks": False,
            "unresolved_nacks": [],
            "blocking_agents": ["coder"],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0


class TestTimeoutWithNacks:
    """Timeout path must return failure when NACKs are unresolved."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_timeout_with_unresolved_nacks_returns_failure(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """On timeout with unresolved NACKs, returns (1, ...) with NACK details."""
        from routes.pipelines import _run_concurrent_phase

        # Start past the timeout (30 min = 1800s)
        mock_monotonic.side_effect = [0.0, 1801.0]

        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions)

        mock_docker.wait_for_container.return_value = ContainerInfo(
            container_id="coder-1",
            container_name="issue-999-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.utcnow(),
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "has_unresolved_nacks": True,
            "unresolved_nacks": [
                {
                    "reviewer": "reviewer_code",
                    "producer": "coder",
                    "reason": "Missing error handling",
                    "version": 1,
                },
            ],
            "blocking_agents": ["coder"],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-999",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1
        assert "UNRESOLVED NACKs" in logs
        assert "Missing error handling" in logs


# ---------- Consensus wrapper tests ----------


class TestConsensusWrapperNackFeedback:
    """Consensus wrapper should include NACK feedback in recovery prompt."""

    def test_wrapper_includes_nack_feedback_placeholder(self):
        from consensus_wrapper import _RECOVERY_PROMPT

        assert "{nack_feedback}" in _RECOVERY_PROMPT

    def test_wrapper_recovery_prompt_mentions_nack_handling(self):
        from consensus_wrapper import _RECOVERY_PROMPT

        assert "NACKs" in _RECOVERY_PROMPT
        assert "re-propose" in _RECOVERY_PROMPT

    def test_wrapper_script_calls_get_nack_feedback(self):
        from consensus_wrapper import build_consensus_wrapped_command

        cmd = build_consensus_wrapped_command("test prompt")
        script = cmd[2]  # bash -c "script"
        assert "get_nack_feedback" in script
        assert "NACK_FEEDBACK" in script

    def test_wrapper_script_has_nack_feedback_function(self):
        from consensus_wrapper import _CONSENSUS_WRAPPER_TEMPLATE

        assert "get_nack_feedback()" in _CONSENSUS_WRAPPER_TEMPLATE
        assert "unresolved_nacks" in _CONSENSUS_WRAPPER_TEMPLATE
