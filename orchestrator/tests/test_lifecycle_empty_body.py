"""Hardening tests for lifecycle endpoints receiving empty-body POSTs.

Regression for #1787: MCP lifecycle tools previously sent no body at all
when their optional fields were omitted, while still sending
``Content-Type: application/json``. Flask's default ``get_json()`` raises
BadRequest(400) for an empty body with a JSON content type, which made
``complete_phase`` and similar tools unreachable via MCP without artifacts.

The sender fix lives in ``mcp_tools._make_request`` (always send ``{}``).
These tests harden the receivers so the same mistake from any future
caller does not reintroduce the opaque 400.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

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


def _make_pipeline(phase=PipelinePhase.PLAN, phase_status=PipelineStatus.COMPLETE):
    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )
    phase_exec = pipeline.get_phase_execution(phase)
    phase_exec.status = phase_status
    return pipeline


class TestEmptyBodyDoesNotCrashLifecycleRoutes:
    """Empty body with Content-Type: application/json must not return a JSON
    parse error (400 with no message). Each route either succeeds on empty
    body (no required fields) or returns a clear domain-level 400.
    """

    @patch("routes.phases._clear_concurrent_state")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_complete_phase_empty_body(self, mock_get_store, _mock_clear, client):
        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        mock_get_store.return_value = (MagicMock(), pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/complete",
            data=b"",
            content_type="application/json",
        )

        assert resp.status_code == 200

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_phase_empty_body_returns_clear_error(self, mock_get_store, client):
        """advance_phase requires target_phase — empty body must return the
        'Missing target_phase' domain error, not a JSON parse 400.
        """
        pipeline = _make_pipeline()
        mock_get_store.return_value = (MagicMock(), pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase",
            data=b"",
            content_type="application/json",
        )

        assert resp.status_code == 400
        assert b"target_phase" in resp.data
        # State store must not be touched when the request is rejected.
        mock_get_store.assert_not_called()

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_fail_phase_empty_body_returns_clear_error(self, mock_get_store, client):
        """fail_phase requires error — empty body must return the
        'Missing error message' domain error, not a JSON parse 400.
        """
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT, phase_status=PipelineStatus.RUNNING
        )
        mock_get_store.return_value = (MagicMock(), pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/fail",
            data=b"",
            content_type="application/json",
        )

        assert resp.status_code == 400
        assert b"error" in resp.data.lower()

    @patch("routes.phases.get_state_store_for_pipeline")
    def test_start_phase_empty_body(self, mock_get_store, client):
        """start_phase takes no body — empty body must not 400."""
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT, phase_status=PipelineStatus.PENDING
        )
        mock_store = MagicMock()
        mock_get_store.return_value = (mock_store, pipeline)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/start",
            data=b"",
            content_type="application/json",
        )

        assert resp.status_code == 200

    @patch("routes.pipelines._populate_contract_from_plan")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_populate_contract_empty_body(
        self, mock_get_store, mock_resolve_wt, mock_populate, client
    ):
        """populate_contract takes no body — empty body must not 400.

        Note: this route never calls get_json(), so it is not vulnerable to
        the #1787 bug directly. This is a structural smoke test confirming
        that sending an empty body doesn't cause unexpected failures.
        """
        from routes.pipelines import PopulateOutcome, PopulateResult

        pipeline = _make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_get_store.return_value = (mock_store, pipeline)
        mock_resolve_wt.return_value = Path("/tmp/wt")
        mock_populate.return_value = PopulateResult(PopulateOutcome.POPULATED)

        resp = client.post(
            "/api/v1/pipelines/issue-42/phase/populate-contract",
            data=b"",
            content_type="application/json",
        )

        assert resp.status_code == 200
