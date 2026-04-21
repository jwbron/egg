"""Tests for the POST /<pipeline_id>/phase/complete endpoint.

Regressions for #1755:
- Empty body must be treated as an empty object, not 400.
- Non-dict artifacts must be rejected at the boundary, not written to
  disk (PhaseExecution.artifacts is typed as dict[str, str] but does
  not validate on assignment, so a poisoned value would break every
  subsequent read).
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import Pipeline, PipelinePhase, PipelineStatus
from routes.phases import phases_bp


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(phases_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_pipeline(phase=PipelinePhase.IMPLEMENT):
    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
    )
    pipeline.current_phase = phase
    return pipeline


class TestCompletePhaseEndpoint:
    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_empty_body_returns_200(self, mock_get_store, _mock_clear, client):
        """POST with Content-Type: application/json and empty body must not 400.

        Regression: Flask's default get_json() raises BadRequest for an
        empty body with a JSON content type, which previously made the
        optional `artifacts` field effectively required.
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            data=b"",
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert data["data"]["phase"] == "implement"
        assert data["data"]["next_phase"] == "pr"

        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        assert phase_exec.status == PipelineStatus.COMPLETE
        assert phase_exec.artifacts == {}

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_no_body_returns_200(self, mock_get_store, _mock_clear, client):
        """POST with no body at all also succeeds."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post("/api/v1/pipelines/issue-42/phase/complete")

        assert resp.status_code == 200

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_string_artifacts_returns_400(self, mock_get_store, _mock_clear, client):
        """A stringified-JSON artifacts value is rejected without mutating state.

        Previously the string was assigned to PhaseExecution.artifacts
        (typed dict[str, str]) without validation, persisted to disk,
        and then broke every subsequent read with a 500 when pydantic
        re-validated the stored pipeline.
        """
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"artifacts": "{}"},
        )

        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["success"] is False
        assert "artifacts" in data["message"]

        # Phase must not have been mutated or saved.
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        assert phase_exec.status != PipelineStatus.COMPLETE
        mock_store.save_pipeline.assert_not_called()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_list_artifacts_returns_400(self, mock_get_store, _mock_clear, client):
        """Non-dict artifacts of any kind are rejected."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"artifacts": ["a", "b"]},
        )

        assert resp.status_code == 400
        mock_store.save_pipeline.assert_not_called()

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_dict_artifacts_stored(self, mock_get_store, _mock_clear, client):
        """Valid dict artifacts are stored on the phase execution."""
        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            json={"artifacts": {"commit_sha": "abc123"}},
        )

        assert resp.status_code == 200
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        assert phase_exec.artifacts == {"commit_sha": "abc123"}
