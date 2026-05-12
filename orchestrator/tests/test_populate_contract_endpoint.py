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

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_success_with_counts_and_issue_number(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """Successful populate returns phase/task counts and forwards issue_number."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_commit.return_value = True
        mock_gw_mode.return_value = ("public", None)
        push_result = MagicMock()
        push_result.__bool__.return_value = True
        mock_spawner.return_value.gateway.push_worktree_branch.return_value = push_result

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
        assert data["data"]["pushed_to_origin"] is True

        # Verify populate was called with pipeline.mode (defaults to 'issue')
        call_kwargs = mock_populate.call_args[1]
        assert str(call_kwargs["pipeline_mode"]) == "issue"
        assert call_kwargs["issue_number"] == 42

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_success_count_readback_fails_still_succeeds(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """When contract read-back fails, still return success with pushed_to_origin."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_commit.return_value = True
        mock_gw_mode.return_value = ("public", None)
        push_result = MagicMock()
        push_result.__bool__.return_value = True
        mock_spawner.return_value.gateway.push_worktree_branch.return_value = push_result

        with patch(
            "egg_contracts.loader.load_contract",
            side_effect=Exception("contract not found"),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        # Counts are absent (read-back failed) but the persist-result is
        # still surfaced so the caller can tell whether agents will see
        # the populated contract on respawn (#2629).
        assert data["data"] == {"pushed_to_origin": True}

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_success_commits_and_pushes_contract_to_origin(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """populate_contract commits and pushes the contract so fresh agent
        spawns see the populated state on origin (#2629)."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        worktree_path = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_resolve_wt.return_value = worktree_path
        mock_commit.return_value = True
        mock_gw_mode.return_value = ("public", None)
        push_result = MagicMock()
        push_result.__bool__.return_value = True
        gateway = mock_spawner.return_value.gateway
        gateway.push_worktree_branch.return_value = push_result

        with patch(
            "egg_contracts.loader.load_contract",
            side_effect=Exception("skip"),
        ):
            client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        mock_commit.assert_called_once()
        commit_kwargs = mock_commit.call_args.kwargs
        assert commit_kwargs["pipeline_identifier"] == 42
        assert commit_kwargs["pipeline_id"] == "issue-42"

        gateway.push_worktree_branch.assert_called_once_with(
            pipeline_id="issue-42",
            repo_path=str(worktree_path),
            branch="egg/issue-42",
            mode="public",
            base_branch=pipeline.base_branch,
        )

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_push_failure_reports_pushed_to_origin_false(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """When the push fails, populate still returns success but reports
        ``pushed_to_origin=False`` so the operator knows the contract is
        only on the orchestrator's local worktree (#2629)."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_commit.return_value = True
        mock_gw_mode.return_value = ("public", None)
        gateway = mock_spawner.return_value.gateway
        gateway.push_worktree_branch.side_effect = RuntimeError("network down")

        mock_phase_obj = MagicMock()
        mock_phase_obj.tasks = [MagicMock()]
        mock_contract = MagicMock()
        mock_contract.slices = [mock_phase_obj]

        with patch("egg_contracts.loader.load_contract", return_value=mock_contract):
            resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["data"]["pushed_to_origin"] is False
        # Counts still come back so the caller can confirm populate worked.
        assert data["data"]["task_count"] == 1

    @patch("routes.pipelines._get_spawner")
    @patch("routes.pipelines._compute_gateway_mode")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_commit_noop_skips_push(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_gw_mode,
        mock_spawner,
        client,
    ):
        """When the commit short-circuits (nothing staged), skip the push —
        contents on origin already match (#2629)."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/home/egg/.egg-worktrees/issue-42/egg")
        mock_commit.return_value = False
        mock_gw_mode.return_value = ("public", None)
        gateway = mock_spawner.return_value.gateway

        with patch(
            "egg_contracts.loader.load_contract",
            side_effect=Exception("skip"),
        ):
            resp = client.post("/api/v1/pipelines/issue-42/phase/populate-contract")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["data"]["pushed_to_origin"] is False
        gateway.push_worktree_branch.assert_not_called()

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
