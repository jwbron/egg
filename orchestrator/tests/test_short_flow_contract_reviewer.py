"""Tests for short flow pipelines using full default implement roles.

When start_phase=implement, refine/plan phases are skipped.  The implement
phase must run all default roles (coder, tester, documenter, reviewer_code,
reviewer_contract) so that validation normally done in earlier phases still
happens.

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


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}

_DEFAULT_IMPLEMENT_ROLES = {
    AgentRole.CODER,
    AgentRole.TESTER,
    AgentRole.DOCUMENTER,
    AgentRole.REVIEWER_CODE,
    AgentRole.REVIEWER_CONTRACT,
}

_COMMON_PATCHES = [
    patch("routes.pipelines.time.sleep"),
    patch("routes.pipelines.time.monotonic", return_value=10.0),
    patch("routes.pipelines._emit_event"),
    patch("routes.pipelines.get_pipeline_state_lock"),
    patch("routes.pipelines._build_agent_prompt", return_value="test prompt"),
]


def _run_with_mocks(pipeline: Pipeline) -> list[AgentRole]:
    """Run _run_concurrent_phase with standard mocks and return spawned roles."""
    with patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False) as MockExecutor:
        for p in _COMMON_PATCHES:
            p.start()

        try:
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

            mock_spawner = MagicMock()
            mock_spawner.docker = MagicMock()
            mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

            _run_concurrent_phase(
                pipeline_id=pipeline.id,
                pipeline=pipeline,
                phase="implement",
                spawner=mock_spawner,
                store=mock_store,
                **_CALL_ARGS,
            )

            return MockExecutor.call_args.kwargs.get("roles", [])
        finally:
            for p in _COMMON_PATCHES:
                p.stop()


class TestShortFlowImplementRoles:
    """Short flow pipelines must use full default implement roles."""

    def test_short_flow_uses_all_default_roles(self):
        """When start_phase=implement, all default implement roles are spawned."""
        config = PipelineConfig(
            start_phase="implement",
            concurrent_execution=True,
            max_concurrent_agents=6,
            message_poll_hint_seconds=30,
            consensus_timeout_minutes=30,
        )
        pipeline = Pipeline(
            id="pipeline-short-1",
            issue_number=100,
            repo="owner/repo",
            branch="egg/issue-100",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            config=config,
        )
        roles = _run_with_mocks(pipeline)
        assert set(roles) == _DEFAULT_IMPLEMENT_ROLES, (
            f"Short flow should use all default roles, got: {roles}"
        )
