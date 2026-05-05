"""Slice-aware spawn env + signal routing (#2403).

Pins the wire shape that lets per-slice agents reach the orchestrator:

  * The slice-spawn path leaves ``EGG_PIPELINE_ID`` as the bare
    pipeline id (passes ``state_store.PIPELINE_ID_PATTERN``) and
    exposes the slice via ``EGG_SLICE_ID``. An earlier shape jammed
    ``{pipeline_id}/{slice_id}`` into ``EGG_PIPELINE_ID`` itself,
    which 4xx'd every agent → orchestrator round-trip (the validator
    rejects ``/`` and Flask's URL converter doesn't allow it either).
  * Consensus signal handlers read ``slice_id`` from the request body
    and route the tracker lookup to ``get_peer_consensus_tracker(
    pipeline_id, slice_id)`` so per-slice CONSENSUS_* lands on the
    slice's tracker, not the pipeline-level one.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from routes.pipelines import _run_concurrent_phase
from state_store import PIPELINE_ID_PATTERN


def _make_pipeline() -> Pipeline:
    config = PipelineConfig()
    config.concurrent_execution = True
    config.max_concurrent_agents = 4
    config.consensus_timeout_minutes = 30
    return Pipeline(
        id="issue-2403",
        issue_number=2403,
        repo="owner/repo",
        branch="egg/issue-2403/work",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_execution(role: AgentRole, container_id: str) -> AgentExecution:
    return AgentExecution(
        role=role,
        status=AgentExecutionStatus.RUNNING,
        container_id=container_id,
        started_at=datetime.now(UTC),
    )


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


def _setup_spawn(executions: list[AgentExecution]):
    pipeline = _make_pipeline()
    phase_exec = PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )
    mock_store = MagicMock()
    mock_pipeline_state = MagicMock()
    mock_pipeline_state.get_phase_execution.return_value = phase_exec
    mock_store.load_pipeline.return_value = mock_pipeline_state

    mock_docker = MagicMock()
    mock_docker.get_container_info.side_effect = lambda cid: ContainerInfo(
        container_id=cid,
        container_name=cid,
        status=ContainerStatus.EXITED,
        exit_code=0,
        exited_at=datetime.now(UTC),
    )
    mock_spawner = MagicMock()
    mock_spawner.backend = mock_docker
    mock_spawner.docker = mock_docker
    mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()
    return pipeline, mock_store, mock_spawner


class TestSliceSpawnEnvShape:
    """``EGG_PIPELINE_ID`` stays canonical; slice scope rides on ``EGG_SLICE_ID``."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic", return_value=0.0)
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_slice_scope_sets_egg_slice_id_and_keeps_egg_pipeline_id_bare(
        self, MockExecutor, mock_prompt, mock_lock, _mono, _sleep
    ):
        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner = _setup_spawn(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        _run_concurrent_phase(
            pipeline_id="issue-2403",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            sandbox_env={"PRESERVED": "yes"},
            slice_id="slice-2",
            **_CALL_ARGS,
        )

        # ``create_concurrent_spawn_fn`` is the seam where the env is
        # frozen for the spawn closure.
        kwargs = mock_spawner.create_concurrent_spawn_fn.call_args.kwargs
        assert kwargs["pipeline_id"] == "issue-2403"
        env = kwargs["sandbox_env"]
        # The bare pipeline id MUST pass the orchestrator's validator —
        # otherwise every ``/api/v1/pipelines/{pid}/...`` round-trip 404s.
        assert env.get("EGG_SLICE_ID") == "slice-2"
        # Agent CLIs read EGG_PIPELINE_ID via ``get_pipeline_id`` — only
        # set if the caller seeded it. We assert here that the function
        # didn't smuggle a slashed value into it.
        if "EGG_PIPELINE_ID" in env:
            assert PIPELINE_ID_PATTERN.match(env["EGG_PIPELINE_ID"]) is not None
        # Pre-existing keys must survive the slice-aware mutation.
        assert env["PRESERVED"] == "yes"

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic", return_value=0.0)
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_no_slice_scope_does_not_set_egg_slice_id(
        self, MockExecutor, mock_prompt, mock_lock, _mono, _sleep
    ):
        executions = [_make_execution(AgentRole.CODER, "coder-1")]
        pipeline, mock_store, mock_spawner = _setup_spawn(executions)

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        _run_concurrent_phase(
            pipeline_id="issue-2403",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            sandbox_env={},
            slice_id=None,
            **_CALL_ARGS,
        )

        env = mock_spawner.create_concurrent_spawn_fn.call_args.kwargs["sandbox_env"]
        assert "EGG_SLICE_ID" not in env


class TestConsensusSignalSliceRouting:
    """``handle_consensus_*`` look up the slice tracker when ``slice_id`` is supplied."""

    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_routes_to_slice_tracker(self, mock_get_tracker, app):
        from routes.signals import handle_consensus_propose_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            handle_consensus_propose_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "slice-2",
                    "payload": {
                        "summary": (
                            "Implemented slice-2 work with thorough commit "
                            "message and substantive description over fifty chars"
                        ),
                        "artifacts": ["src/a.py"],
                    },
                },
                Path("/tmp/repo"),
            )

        # The tracker lookup MUST forward slice_id so consensus messages
        # land on the per-slice tracker (#2403).
        call = mock_get_tracker.call_args
        slice_arg = (
            call.kwargs.get("slice_id")
            if "slice_id" in call.kwargs
            else (call.args[1] if len(call.args) >= 2 else None)
        )
        assert call.args[0] == "issue-2403"
        assert slice_arg == "slice-2"

    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_propose_without_slice_falls_back_to_pipeline_tracker(self, mock_get_tracker, app):
        from routes.signals import handle_consensus_propose_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        with app.app_context():
            handle_consensus_propose_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": (
                            "Implemented work with substantive description "
                            "over fifty chars to satisfy the validator"
                        ),
                        "artifacts": ["src/a.py"],
                    },
                },
                Path("/tmp/repo"),
            )

        # Pipeline-level callers (no slice_id) keep the bare-tracker
        # semantics — ``get_peer_consensus_tracker(pipeline_id, None)``
        # is the same key as the legacy single-arg lookup.
        call = mock_get_tracker.call_args
        assert call.args[0] == "issue-2403"
        # slice_id positional or kwarg must be None / absent.
        positional_slice = call.args[1] if len(call.args) >= 2 else None
        kwarg_slice = call.kwargs.get("slice_id")
        assert positional_slice is None and kwarg_slice is None

    def test_propose_rejects_malformed_slice_id(self, app):
        from routes.signals import handle_consensus_propose_signal

        with app.app_context():
            response, status = handle_consensus_propose_signal(
                "issue-2403",
                {
                    "agent_role": "coder",
                    "slice_id": "../etc/passwd",
                    "payload": {
                        "summary": (
                            "Implemented work with substantive description "
                            "over fifty chars to satisfy the validator"
                        ),
                        "artifacts": ["src/a.py"],
                    },
                },
                Path("/tmp/repo"),
            )
        assert status == 400
        assert "slice_id" in response.get_json()["message"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


import pytest  # noqa: E402


@pytest.fixture
def app():
    from flask import Flask
    from routes.signals import signals_bp

    app = Flask(__name__)
    app.register_blueprint(signals_bp)
    return app
