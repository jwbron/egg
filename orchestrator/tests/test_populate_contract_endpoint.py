"""Tests for the POST /<pipeline_id>/phase/populate-contract endpoint.

This endpoint wraps the internal _populate_contract_from_plan() function,
resolving the worktree path and returning phase/task counts.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import Pipeline, PipelinePhase
from routes.phases import phases_bp
from state_store import InvalidPipelineIdError, PipelineNotFoundError


@pytest.fixture
def app():
    """Create a test Flask app with the phases blueprint."""
    app = Flask(__name__)
    app.register_blueprint(phases_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


def _make_pipeline(
    pipeline_id="issue-42",
    phase=PipelinePhase.IMPLEMENT,
    issue_number=42,
):
    """Create a minimal Pipeline for testing."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo="owner/repo",
        branch="egg/issue-42",
    )
    pipeline.current_phase = phase
    return pipeline


class TestPopulateContractEndpoint:
    """Tests for POST /<pipeline_id>/phase/populate-contract."""

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pipeline_not_found(self, mock_get_store, client):
        """Returns 404 when pipeline doesn't exist."""
        mock_get_store.side_effect = PipelineNotFoundError("Pipeline not found")

        resp = client.post("/api/v1/pipelines/issue-999/phase/populate-contract")

        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "not found" in data["message"]

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_invalid_pipeline_id(self, mock_get_store, client):
        """Returns 400 for invalid pipeline ID format."""
        mock_get_store.side_effect = InvalidPipelineIdError("Invalid ID format")

        resp = client.post("/api/v1/pipelines/bad!!id/phase/populate-contract")

        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_pipeline_mode_from_pipeline_not_config(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """pipeline.mode (not pipeline.config.mode) is used for pipeline_mode.

        Regression test: originally pipeline.config.mode was used but
        PipelineConfig has no mode attribute — mode lives on Pipeline directly.
        """
        pipeline = _make_pipeline()
        # pipeline.mode defaults to 'issue' from PipelineMode.ISSUE
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/tmp/wt")

        with patch(
            "egg_contracts.loader.load_contract",
            side_effect=Exception("skip"),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        call_kwargs = mock_populate.call_args[1]
        assert call_kwargs["pipeline_mode"] == "issue"

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_populate_function_exception(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """Returns 500 when the populate function raises an exception."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_populate.side_effect = RuntimeError("plan draft not found")

        resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 500
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "plan draft not found" in data["message"]

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_success_with_counts_and_issue_number(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """Successful populate returns phase/task counts and forwards issue_number."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")

        # Mock the contract read-back
        mock_phase_obj = MagicMock()
        mock_phase_obj.tasks = [MagicMock(), MagicMock()]
        mock_contract = MagicMock()
        mock_contract.slices = [mock_phase_obj]

        with patch("egg_contracts.loader.load_contract", return_value=mock_contract):
            resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["data"]["phase_count"] == 1
        assert data["data"]["task_count"] == 2

        # Verify populate was called with pipeline.mode (defaults to 'issue')
        call_kwargs = mock_populate.call_args[1]
        assert str(call_kwargs["pipeline_mode"]) == "issue"
        assert call_kwargs["issue_number"] == 42

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_success_count_readback_fails_still_succeeds(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """When contract read-back fails, still return success without counts."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")

        with patch(
            "egg_contracts.loader.load_contract",
            side_effect=Exception("contract not found"),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert "data" not in data  # No counts on fallback

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_worktree_path_passed_to_populate(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """Verify worktree path (not raw repo path) is passed to populate."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)

        worktree_path = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_resolve_wt.return_value = worktree_path

        with patch(
            "egg_contracts.loader.load_contract",
            side_effect=Exception("skip"),
        ):
            client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        # Verify resolve_worktree_path was called correctly
        mock_resolve_wt.assert_called_once_with("issue-42", Path("/home/egg/repos/egg"))
        # Verify populate got the resolved worktree path, not the raw repo path
        mock_populate.assert_called_once()
        call_kwargs = mock_populate.call_args[1]
        assert call_kwargs["repo_path"] == worktree_path
