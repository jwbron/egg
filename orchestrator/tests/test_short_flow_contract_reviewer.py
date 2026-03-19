"""Tests for contract reviewer inclusion in short flow pipelines.

When start_phase=implement, refine/plan phases are skipped.  The contract
reviewer must still be present to validate the contract before code is written.

See: https://github.com/jwbron/egg/issues/1339
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from models import (
    AgentRole,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import _run_concurrent_phase


def _make_short_flow_pipeline(
    implement_roles: list[str] | None = None,
) -> Pipeline:
    """Create a pipeline with start_phase=implement."""
    config = PipelineConfig(
        start_phase="implement",
        implement_roles=implement_roles,
        concurrent_execution=True,
        max_concurrent_agents=6,
        message_poll_hint_seconds=30,
        consensus_timeout_minutes=30,
    )
    return Pipeline(
        id="pipeline-short-1",
        issue_number=100,
        repo="owner/repo",
        branch="egg/issue-100",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


class TestShortFlowContractReviewer:
    """Contract reviewer must be present in short flow pipelines."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic", return_value=10.0)
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_contract_reviewer_added_when_missing_from_implement_roles(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When implement_roles omits reviewer_contract, it is auto-added."""
        pipeline = _make_short_flow_pipeline(
            implement_roles=["coder", "reviewer_code"],
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = []
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_spawner = MagicMock()
        mock_spawner.docker = MagicMock()
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        _run_concurrent_phase(
            pipeline_id="pipeline-short-1",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # The executor should have been constructed with roles that include
        # the contract reviewer.
        call_kwargs = MockExecutor.call_args
        roles = call_kwargs.kwargs.get("roles", [])
        assert AgentRole.REVIEWER_CONTRACT in roles, (
            f"reviewer_contract not in spawned roles: {roles}"
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic", return_value=10.0)
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_contract_reviewer_not_duplicated_when_already_present(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When implement_roles already includes reviewer_contract, no duplicate."""
        pipeline = _make_short_flow_pipeline(
            implement_roles=["coder", "reviewer_code", "reviewer_contract"],
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = []
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_spawner = MagicMock()
        mock_spawner.docker = MagicMock()
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        _run_concurrent_phase(
            pipeline_id="pipeline-short-1",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        call_kwargs = MockExecutor.call_args
        roles = call_kwargs.kwargs.get("roles", [])
        # Count reviewer_contract — should appear exactly once
        contract_count = sum(1 for r in roles if r == AgentRole.REVIEWER_CONTRACT)
        assert contract_count == 1, f"reviewer_contract appeared {contract_count} times"

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic", return_value=10.0)
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_no_auto_add_when_not_short_flow(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When start_phase is not implement, implement_roles is respected as-is."""
        # Full flow pipeline with implement_roles override (start_phase=None)
        config = PipelineConfig(
            start_phase=None,
            implement_roles=["coder", "reviewer_code"],
            concurrent_execution=True,
            max_concurrent_agents=6,
            message_poll_hint_seconds=30,
            consensus_timeout_minutes=30,
        )
        pipeline = Pipeline(
            id="pipeline-full-1",
            issue_number=200,
            repo="owner/repo",
            branch="egg/issue-200",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            config=config,
        )

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = []
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_spawner = MagicMock()
        mock_spawner.docker = MagicMock()
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        _run_concurrent_phase(
            pipeline_id="pipeline-full-1",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        call_kwargs = MockExecutor.call_args
        roles = call_kwargs.kwargs.get("roles", [])
        # In full flow, contract was already validated during refine/plan,
        # so implement_roles override is respected without auto-adding.
        assert AgentRole.REVIEWER_CONTRACT not in roles, (
            f"reviewer_contract should not be auto-added in full flow: {roles}"
        )
