"""
Tests for pipeline API endpoints.

Covers branch cleanup during pipeline deletion.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import ContainerInfo, PhaseExecution, Pipeline, PipelinePhase
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
