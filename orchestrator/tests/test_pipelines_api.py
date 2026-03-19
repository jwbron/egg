"""
Tests for pipeline API endpoints.

Covers branch cleanup during pipeline deletion.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
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


def _make_pipeline_with_containers(pipeline_id="test-pipeline"):
    """Create a Pipeline with container history across phases."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
    )
    pipeline.phases = {
        "plan": PhaseExecution(
            phase=PipelinePhase.PLAN,
            containers=[
                ContainerInfo(
                    container_id="container-aaa",
                    container_name="egg-test-aaa-coder",
                ),
            ],
        ),
        "implement": PhaseExecution(
            phase=PipelinePhase.IMPLEMENT,
            containers=[
                ContainerInfo(
                    container_id="container-bbb",
                    container_name="egg-test-bbb-coder",
                ),
                ContainerInfo(
                    container_id="container-ccc",
                    container_name="egg-test-ccc-tester",
                ),
            ],
        ),
    }
    return pipeline


class TestDeletePipelineBranchCleanup:
    """Tests for remote branch cleanup during pipeline deletion."""

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_cleans_up_remote_branches(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """Verify delete_remote_branch is called for each container's branch."""
        pipeline = _make_pipeline_with_containers("test-pipeline")

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw.delete_remote_branch.return_value = True
        mock_gw_fn.return_value = mock_gw

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Should have been called for each unique container
        assert mock_gw.delete_remote_branch.call_count == 3

        called_branches = {
            call.kwargs.get("branch") or call.args[2]
            for call in mock_gw.delete_remote_branch.call_args_list
        }
        assert called_branches == {
            "egg/container-aaa/work",
            "egg/container-bbb/work",
            "egg/container-ccc/work",
        }

        # Pipeline should still be deleted even after branch cleanup
        mock_store.delete_pipeline.assert_called_once_with("test-pipeline")

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_succeeds_when_branch_cleanup_fails(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """Pipeline deletion succeeds even if branch cleanup fails."""
        pipeline = _make_pipeline_with_containers("test-pipeline")

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw.delete_remote_branch.return_value = False
        mock_gw_fn.return_value = mock_gw

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Pipeline should still be deleted
        mock_store.delete_pipeline.assert_called_once_with("test-pipeline")

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_no_containers_skips_cleanup(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """No branch cleanup when pipeline has no containers."""
        pipeline = Pipeline(
            id="test-pipeline",
            issue_number=42,
            repo="owner/repo",
            branch="egg/test",
        )

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw_fn.return_value = mock_gw

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Should not have called delete_remote_branch
        mock_gw.delete_remote_branch.assert_not_called()

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_deduplicates_container_branches(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """Same container in multiple phases should only trigger one deletion."""
        pipeline = Pipeline(
            id="test-pipeline",
            issue_number=42,
            repo="owner/repo",
            branch="egg/test",
        )
        shared_container = ContainerInfo(
            container_id="container-shared",
            container_name="egg-test-shared-coder",
        )
        pipeline.phases = {
            "plan": PhaseExecution(
                phase=PipelinePhase.PLAN,
                containers=[shared_container],
            ),
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                containers=[shared_container],
            ),
        }

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw = MagicMock()
        mock_gw.delete_remote_branch.return_value = True
        mock_gw_fn.return_value = mock_gw

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Should only be called once despite the container appearing in two phases
        mock_gw.delete_remote_branch.assert_called_once()
        call_args = mock_gw.delete_remote_branch.call_args
        assert (call_args.kwargs.get("branch") or call_args.args[2]) == "egg/container-shared/work"

    @patch("routes.pipelines.get_gateway_client")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    def test_delete_pipeline_succeeds_when_gateway_client_raises(
        self, mock_resolve, mock_spawner_fn, mock_gw_fn, client
    ):
        """Pipeline deletion succeeds even if get_gateway_client() raises."""
        pipeline = _make_pipeline_with_containers("test-pipeline")

        mock_store = MagicMock()
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 0
        mock_spawner_fn.return_value = mock_spawner

        mock_gw_fn.side_effect = ConnectionError("gateway unreachable")

        response = client.delete("/api/v1/pipelines/test-pipeline")
        assert response.status_code == 200

        # Pipeline should still be deleted despite branch cleanup exception
        mock_store.delete_pipeline.assert_called_once_with("test-pipeline")


class TestCreatePipelineMultiRepo:
    """Tests that create_pipeline resolves repo paths in multi-repo setups (#1323)."""

    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_pipeline_uses_get_repo_path(self, mock_repo_path, mock_get_store, client):
        """create_pipeline should call get_repo_path() for all pipeline types."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "issue-42"
        mock_pipeline.model_dump.return_value = {"id": "issue-42"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "issue_number": 42,
                "repo": "Khan/webapp",
                "branch": "egg/issue-42",
            },
        )
        assert response.status_code == 200
        mock_repo_path.assert_called_once()
        mock_get_store.assert_called_once_with(Path("/home/egg/repos/webapp"))

    @patch("routes.pipelines.get_state_store")
    @patch("routes.pipelines.get_repo_path")
    def test_create_prompt_pipeline_uses_get_repo_path(
        self, mock_repo_path, mock_get_store, client
    ):
        """Prompt-driven pipelines (no issue_number) also use get_repo_path()."""
        mock_repo_path.return_value = Path("/home/egg/repos/webapp")
        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.id = "prompt-abc"
        mock_pipeline.model_dump.return_value = {"id": "prompt-abc"}
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        response = client.post(
            "/api/v1/pipelines",
            json={
                "repo": "Khan/webapp",
                "prompt": "Fix the login bug",
            },
        )
        assert response.status_code == 200
        mock_repo_path.assert_called_once()
        mock_get_store.assert_called_once_with(Path("/home/egg/repos/webapp"))

    def test_get_repo_path_returns_400_when_repo_not_found(self, client, tmp_path):
        """get_repo_path() returns 400 when repo subdir is missing in multi-repo setup."""
        with patch.dict(os.environ, {"EGG_REPO_PATH": str(tmp_path)}):
            response = client.post(
                "/api/v1/pipelines",
                json={
                    "issue_number": 42,
                    "repo": "Khan/webapp",
                    "branch": "egg/issue-42",
                },
            )
        assert response.status_code == 400
        body = response.get_json() or {}
        msg = body.get("message", "") or body.get("description", "")
        assert "webapp" in msg or "webapp" in response.get_data(as_text=True)


def _make_cancellable_pipeline(pipeline_id="test-pipeline"):
    """Create a pipeline with running containers and agents for cancellation tests."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.REFINE,
    )
    pipeline.phases = {
        "refine": PhaseExecution(
            phase=PipelinePhase.REFINE,
            status=PipelineStatus.RUNNING,
            containers=[
                ContainerInfo(
                    container_id="refiner-bbb",
                    container_name="egg-test-refiner",
                    agent_role=AgentRole.REFINER,
                    status=ContainerStatus.RUNNING,
                ),
            ],
            agents=[
                AgentExecution(
                    role=AgentRole.REFINER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id="refiner-bbb",
                ),
            ],
        ),
    }
    return pipeline


class TestTerminatedPipelineSyncsState:
    """Tests that terminating a pipeline marks running containers/agents as stopped."""

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cancel_marks_running_containers_as_removed(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        # Simulate update_pipeline setting status to cancelled
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 2
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        # All running containers should be marked REMOVED
        for container in pipeline.phases["refine"].containers:
            assert container.status == ContainerStatus.REMOVED
            assert container.exited_at is not None

        # All running agents should be marked FAILED with correct error
        for agent in pipeline.phases["refine"].agents:
            assert agent.status == AgentExecutionStatus.FAILED
            assert agent.completed_at is not None
            assert agent.error == "Pipeline cancelled"  # status-specific message

        # Pipeline state should have been saved
        mock_store.save_pipeline.assert_called_once_with(pipeline)

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_cancel_skips_already_completed_agents(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()
        # Mark one agent as already complete
        pipeline.phases["refine"].agents[0].status = AgentExecutionStatus.COMPLETE
        pipeline.phases["refine"].containers[0].status = ContainerStatus.EXITED

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.CANCELLED
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 1
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

        # Already-complete agent/container should NOT be overwritten
        assert pipeline.phases["refine"].agents[0].status == AgentExecutionStatus.COMPLETE
        assert pipeline.phases["refine"].containers[0].status == ContainerStatus.EXITED

    @patch("routes.pipelines.get_decision_queue")
    @patch("routes.pipelines.get_container_spawner")
    @patch("routes.pipelines._resolve_pipeline")
    @patch("routes.pipelines.get_repo_path")
    def test_failed_pipeline_uses_correct_error_message(
        self, mock_repo, mock_resolve, mock_spawner_fn, mock_dq_fn, client
    ):
        """When a pipeline transitions to FAILED, agent errors say 'Pipeline failed'."""
        mock_repo.return_value = "/repo"
        pipeline = _make_cancellable_pipeline()

        mock_store = MagicMock()
        mock_store.update_pipeline.return_value = pipeline
        mock_store.load_pipeline.return_value = pipeline
        pipeline.status = PipelineStatus.FAILED
        mock_resolve.return_value = (mock_store, pipeline)

        mock_spawner = MagicMock()
        mock_spawner.cleanup_pipeline.return_value = 2
        mock_spawner_fn.return_value = mock_spawner

        mock_dq = MagicMock()
        mock_dq.get_pending_decisions.return_value = []
        mock_dq_fn.return_value = mock_dq

        response = client.patch(
            "/api/v1/pipelines/test-pipeline",
            json={"status": "failed"},
        )
        assert response.status_code == 200

        # Agent errors should reflect the actual terminal status
        for agent in pipeline.phases["refine"].agents:
            assert agent.status == AgentExecutionStatus.FAILED
            assert agent.error == "Pipeline failed"  # not "Pipeline cancelled"
