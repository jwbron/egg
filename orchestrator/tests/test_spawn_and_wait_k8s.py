"""Tests for _spawn_and_wait K8s metadata preservation.

Verifies that the sequential spawn path (plan, refine, etc.) preserves
backend-specific ContainerInfo fields (namespace, job_name, pod_name)
through the bookkeeping block, matching the concurrent-path fix (#1841).
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from models import (
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import _spawn_and_wait


def _make_phase_execution() -> PhaseExecution:
    return PhaseExecution(
        phase=PipelinePhase.PLAN,
        status=PipelineStatus.RUNNING,
    )


class TestSpawnAndWaitK8sMetadata:
    """_spawn_and_wait must preserve K8s fields on the recorded ContainerInfo."""

    @patch("routes.pipelines.get_pipeline_state_lock")
    def test_recorded_container_preserves_k8s_metadata(self, mock_state_lock):
        """When the spawner returns a ContainerInfo with K8s fields, the
        recorded phase_execution.containers[] entry preserves namespace,
        job_name, and pod_name instead of rebuilding a minimal ContainerInfo."""
        phase_exec = _make_phase_execution()

        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_state_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_state_lock.return_value.__exit__ = MagicMock(return_value=False)

        # Spawner returns a K8s-style ContainerInfo with namespace/job_name/pod_name.
        k8s_info = ContainerInfo(
            container_id="uid-abc123",
            container_name="issue-999-coder",
            status=ContainerStatus.PENDING,
            namespace="egg-sandbox",
            job_name="issue-999-coder",
            pod_name="issue-999-coder-xyz",
        )
        spawned = MagicMock()
        spawned.container_info = k8s_info

        mock_spawner = MagicMock()
        mock_spawner.spawn_agent_job.return_value = spawned

        # wait_for_container returns EXITED with exit_code=0.
        mock_backend = MagicMock()
        mock_backend.wait_for_container.return_value = ContainerInfo(
            container_id="uid-abc123",
            container_name="issue-999-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.now(UTC),
        )
        mock_spawner.backend = mock_backend

        exit_code, logs = _spawn_and_wait(
            spawner=mock_spawner,
            pipeline_id="issue-999",
            agent_role=AgentRole.CODER,
            issue_number=999,
            repo_volumes={},
            gateway_mode="public",
            repos=["owner/repo"],
            phase="plan",
            sandbox_env={},
            sandbox_command=["bash", "-c", "echo hi"],
            store=mock_store,
        )

        assert exit_code == 0
        assert len(phase_exec.containers) == 1
        recorded = phase_exec.containers[0]
        assert recorded.container_id == "uid-abc123"
        assert recorded.namespace == "egg-sandbox"
        assert recorded.job_name == "issue-999-coder"
        assert recorded.pod_name == "issue-999-coder-xyz"
        assert recorded.agent_role == AgentRole.CODER
        assert recorded.started_at is not None
        # model_copy overrides status to RUNNING, then the update loop
        # sets it to EXITED after wait_for_container returns.
        assert recorded.status == ContainerStatus.EXITED
