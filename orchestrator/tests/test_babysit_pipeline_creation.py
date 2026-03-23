"""
Tests for babysit pipeline creation via the pipelines API.

Validates the POST /api/v1/pipelines endpoint with mode=babysit,
including happy path, missing pr_number, invalid pr_number, and
duplicate pipeline ID.
"""

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import Pipeline, PipelineMode
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


class TestBabysitPipelineCreation:
    """Tests for POST /api/v1/pipelines with mode=babysit."""

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_babysit_happy_path(self, mock_get_store, mock_get_repo_path, client):
        """Babysit pipeline creation with valid pr_number succeeds."""
        mock_store = MagicMock()
        mock_pipeline = Pipeline(
            id="pr-42",
            repo="owner/repo",
            mode=PipelineMode.BABYSIT,
            pr_number=42,
        )
        mock_store.create_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={
                "mode": "babysit",
                "pr_number": 42,
                "repo": "owner/repo",
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True

        # Verify create_pipeline was called with mode and pr_number
        call_kwargs = mock_store.create_pipeline.call_args[1]
        assert call_kwargs["pipeline_id"] == "pr-42"
        assert call_kwargs["pr_number"] == 42
        assert call_kwargs["mode"] == PipelineMode.BABYSIT

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_babysit_missing_pr_number(self, mock_get_store, mock_get_repo_path, client):
        """Babysit mode without pr_number returns an error."""
        response = client.post(
            "/api/v1/pipelines",
            json={
                "mode": "babysit",
                "repo": "owner/repo",
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "pr_number" in data["message"].lower()

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_babysit_invalid_pr_number_negative(self, mock_get_store, mock_get_repo_path, client):
        """Babysit mode with negative pr_number returns an error."""
        response = client.post(
            "/api/v1/pipelines",
            json={
                "mode": "babysit",
                "pr_number": -1,
                "repo": "owner/repo",
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "positive integer" in data["message"].lower()

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_babysit_invalid_pr_number_zero(self, mock_get_store, mock_get_repo_path, client):
        """Babysit mode with pr_number=0 returns an error."""
        response = client.post(
            "/api/v1/pipelines",
            json={
                "mode": "babysit",
                "pr_number": 0,
                "repo": "owner/repo",
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_babysit_invalid_pr_number_string(self, mock_get_store, mock_get_repo_path, client):
        """Babysit mode with string pr_number returns an error."""
        response = client.post(
            "/api/v1/pipelines",
            json={
                "mode": "babysit",
                "pr_number": "not-a-number",
                "repo": "owner/repo",
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_babysit_duplicate_pipeline_id(self, mock_get_store, mock_get_repo_path, client):
        """Babysit pipeline with existing active pipeline returns 409."""
        from state_store import StateStoreError

        mock_store = MagicMock()
        mock_store.create_pipeline.side_effect = StateStoreError("Pipeline pr-42 already exists")
        # load_pipeline is called for enrichment in the 409 response;
        # its return value must be JSON-serializable.
        existing = MagicMock()
        existing.id = "pr-42"
        existing.status.value = "running"
        existing.current_phase.value = "implement"
        mock_store.load_pipeline.return_value = existing
        mock_get_store.return_value = mock_store
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={
                "mode": "babysit",
                "pr_number": 42,
                "repo": "owner/repo",
            },
        )

        assert response.status_code == 409
        data = response.get_json()
        assert data["success"] is False
        assert "already exists" in data["message"]

    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_babysit_missing_repo(self, mock_get_store, mock_get_repo_path, client):
        """Babysit mode without repo returns an error."""
        response = client.post(
            "/api/v1/pipelines",
            json={
                "mode": "babysit",
                "pr_number": 42,
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "repo" in data["message"].lower()
