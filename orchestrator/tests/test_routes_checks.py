"""
Unit tests for orchestrator deployment check API endpoints.

Tests start/status/teardown endpoints with mocked DevserverManager.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from devserver import DevserverError


@pytest.fixture
def app():
    """Create a test Flask app with the checks blueprint."""
    from flask import Flask
    from routes.checks import _active_devservers, _starting_devservers, checks_bp

    app = Flask(__name__)
    app.register_blueprint(checks_bp)
    app.config["TESTING"] = True

    # Clean up active devservers between tests
    _active_devservers.clear()
    _starting_devservers.clear()

    yield app

    _active_devservers.clear()
    _starting_devservers.clear()


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


class TestStartDeploymentCheck:
    """Tests for POST /<pipeline_id>/deployment-check/start."""

    @patch("routes.checks.get_state_store")
    @patch("routes.checks.load_deployment_config")
    @patch("routes.checks.DevserverManager")
    @patch("routes.checks.get_repo_path")
    @patch("routes.checks.resolve_worktree_path")
    def test_start_success(
        self,
        mock_resolve_wt,
        mock_get_repo,
        mock_manager_cls,
        mock_load_config,
        mock_get_store,
        client,
    ):
        mock_get_repo.return_value = Path("/repo")
        mock_resolve_wt.return_value = Path("/worktree")
        mock_store = MagicMock()
        mock_get_store.return_value = mock_store

        mock_config = MagicMock()
        mock_load_config.return_value = mock_config

        mock_manager = MagicMock()
        mock_status = MagicMock()
        mock_status.to_dict.return_value = {"status": "healthy", "services": {}}
        mock_manager.start.return_value = mock_status
        mock_manager_cls.return_value = mock_manager

        resp = client.post("/api/v1/pipelines/issue-123/deployment-check/start")
        data = json.loads(resp.data)

        assert resp.status_code == 200
        assert data["success"] is True
        mock_manager.start.assert_called_once_with(mock_config)

    @patch("routes.checks.get_state_store")
    @patch("routes.checks.get_repo_path")
    def test_start_pipeline_not_found(self, mock_get_repo, mock_get_store, client):
        from state_store import PipelineNotFoundError

        mock_get_repo.return_value = Path("/repo")
        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = PipelineNotFoundError("not found")
        mock_get_store.return_value = mock_store

        resp = client.post("/api/v1/pipelines/nonexistent/deployment-check/start")
        data = json.loads(resp.data)

        assert resp.status_code == 404
        assert data["success"] is False

    @patch("routes.checks.get_state_store")
    @patch("routes.checks.load_deployment_config")
    @patch("routes.checks.get_repo_path")
    @patch("routes.checks.resolve_worktree_path")
    def test_start_no_deployment_config(
        self,
        mock_resolve_wt,
        mock_get_repo,
        mock_load_config,
        mock_get_store,
        client,
    ):
        mock_get_repo.return_value = Path("/repo")
        mock_resolve_wt.return_value = Path("/worktree")
        mock_store = MagicMock()
        mock_get_store.return_value = mock_store
        mock_load_config.return_value = None

        resp = client.post("/api/v1/pipelines/issue-123/deployment-check/start")
        data = json.loads(resp.data)

        assert resp.status_code == 422
        assert data["success"] is False
        assert "deployment config" in data["message"].lower()

    @patch("routes.checks.get_state_store")
    @patch("routes.checks.get_repo_path")
    def test_start_conflict_already_running(self, mock_get_repo, mock_get_store, client):
        from devserver import DevserverStatus, DevserverStatusValue
        from routes.checks import _active_devservers

        mock_get_repo.return_value = Path("/repo")
        mock_store = MagicMock()
        mock_get_store.return_value = mock_store

        mock_manager = MagicMock()
        mock_manager.status = DevserverStatus(status=DevserverStatusValue.HEALTHY)
        _active_devservers["issue-123"] = mock_manager

        resp = client.post("/api/v1/pipelines/issue-123/deployment-check/start")
        data = json.loads(resp.data)

        assert resp.status_code == 409
        assert data["success"] is False

    @patch("routes.checks.get_state_store")
    @patch("routes.checks.get_repo_path")
    def test_start_conflict_already_starting(self, mock_get_repo, mock_get_store, client):
        """409 returned when pipeline is already in _starting_devservers."""
        from routes.checks import _starting_devservers

        mock_get_repo.return_value = Path("/repo")
        mock_store = MagicMock()
        mock_get_store.return_value = mock_store

        _starting_devservers.add("issue-123")

        resp = client.post("/api/v1/pipelines/issue-123/deployment-check/start")
        data = json.loads(resp.data)

        assert resp.status_code == 409
        assert data["success"] is False
        assert "already being started" in data["message"].lower()

    @patch("routes.checks.get_state_store")
    @patch("routes.checks.load_deployment_config")
    @patch("routes.checks.DevserverManager")
    @patch("routes.checks.get_repo_path")
    @patch("routes.checks.resolve_worktree_path")
    def test_start_cleans_sentinel_on_devserver_error(
        self,
        mock_resolve_wt,
        mock_get_repo,
        mock_manager_cls,
        mock_load_config,
        mock_get_store,
        client,
    ):
        """Sentinel is cleaned up when manager.start() raises DevserverError."""
        from routes.checks import _starting_devservers

        mock_get_repo.return_value = Path("/repo")
        mock_resolve_wt.return_value = Path("/worktree")
        mock_store = MagicMock()
        mock_get_store.return_value = mock_store
        mock_load_config.return_value = MagicMock()

        mock_manager = MagicMock()
        mock_manager.start.side_effect = DevserverError("compose up failed")
        mock_manager_cls.return_value = mock_manager

        resp = client.post("/api/v1/pipelines/issue-123/deployment-check/start")

        assert resp.status_code == 500
        assert "issue-123" not in _starting_devservers
        mock_manager.teardown.assert_called_once()

    @patch("routes.checks.get_state_store")
    @patch("routes.checks.get_repo_path")
    @patch("routes.checks.resolve_worktree_path")
    def test_start_cleans_sentinel_on_unexpected_error(
        self,
        mock_resolve_wt,
        mock_get_repo,
        mock_get_store,
        client,
    ):
        """Sentinel is cleaned up even for non-DevserverError exceptions."""
        from routes.checks import _starting_devservers

        mock_get_repo.return_value = Path("/repo")
        mock_store = MagicMock()
        mock_get_store.return_value = mock_store
        mock_resolve_wt.side_effect = RuntimeError("boom")

        resp = client.post("/api/v1/pipelines/issue-123/deployment-check/start")

        assert resp.status_code == 500
        assert "issue-123" not in _starting_devservers


class TestGetDeploymentCheckStatus:
    """Tests for GET /<pipeline_id>/deployment-check/status."""

    def test_status_not_found(self, client):
        resp = client.get("/api/v1/pipelines/nonexistent/deployment-check/status")
        data = json.loads(resp.data)

        assert resp.status_code == 404
        assert data["success"] is False

    def test_status_found(self, client):
        from routes.checks import _active_devservers

        mock_manager = MagicMock()
        mock_manager.status.to_dict.return_value = {
            "status": "healthy",
            "services": {"api": {"healthy": True}},
        }
        _active_devservers["issue-123"] = mock_manager

        resp = client.get("/api/v1/pipelines/issue-123/deployment-check/status")
        data = json.loads(resp.data)

        assert resp.status_code == 200
        assert data["success"] is True
        assert data["status"]["status"] == "healthy"


class TestTeardownDeploymentCheck:
    """Tests for POST /<pipeline_id>/deployment-check/teardown."""

    def test_teardown_success(self, client):
        from routes.checks import _active_devservers

        mock_manager = MagicMock()
        _active_devservers["issue-123"] = mock_manager

        resp = client.post("/api/v1/pipelines/issue-123/deployment-check/teardown")
        data = json.loads(resp.data)

        assert resp.status_code == 200
        assert data["success"] is True
        mock_manager.teardown.assert_called_once()
        assert "issue-123" not in _active_devservers

    def test_teardown_idempotent(self, client):
        """Teardown when no devserver is running should succeed."""
        resp = client.post("/api/v1/pipelines/nonexistent/deployment-check/teardown")
        data = json.loads(resp.data)

        assert resp.status_code == 200
        assert data["success"] is True
