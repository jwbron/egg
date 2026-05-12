"""
Tests for progress event endpoints.

Covers emit and query endpoints for structured progress tracking,
including validation of required fields, invalid states, and filtering.
"""

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from progress_store import reset_progress_store
from routes.progress import progress_bp


@pytest.fixture
def app():
    """Create a test Flask app with the progress blueprint."""
    app = Flask(__name__)
    app.register_blueprint(progress_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset progress store singleton between tests."""
    reset_progress_store()
    yield
    reset_progress_store()


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines/<pipeline_id>/progress
# ---------------------------------------------------------------------------


class TestEmitProgress:
    """Test emitting structured progress events."""

    def test_emit_progress_valid(self, client):
        """Valid progress event is accepted and stored."""
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            data=json.dumps(
                {
                    "agent_role": "coder",
                    "step": "running tests",
                    "state": "working",
                    "detail": "pytest suite 3/5",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        event = data["data"]["event"]
        assert event["agent_role"] == "coder"
        assert event["step"] == "running tests"
        assert event["state"] == "working"
        assert event["detail"] == "pytest suite 3/5"
        assert event["id"] is not None
        assert event["pipeline_id"] == "issue-100"

    def test_emit_progress_blocked_with_blocker(self, client):
        """Blocked state with blocker field is accepted."""
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            data=json.dumps(
                {
                    "agent_role": "coder",
                    "step": "waiting for dependency",
                    "state": "blocked",
                    "blocker": "missing npm package",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        event = resp.get_json()["data"]["event"]
        assert event["state"] == "blocked"
        assert event["blocker"] == "missing npm package"

    def test_emit_progress_complete(self, client):
        """Complete state is accepted."""
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            data=json.dumps(
                {
                    "agent_role": "tester",
                    "step": "all tests passed",
                    "state": "complete",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["event"]["state"] == "complete"

    def test_emit_progress_invalid_state(self, client):
        """Invalid state value is rejected."""
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            data=json.dumps(
                {
                    "agent_role": "coder",
                    "step": "doing stuff",
                    "state": "invalid_state",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "invalid" in data["message"].lower() or "Invalid" in data["message"]

    def test_emit_progress_missing_agent_role(self, client):
        """Missing agent_role is rejected."""
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            data=json.dumps(
                {
                    "step": "doing stuff",
                    "state": "working",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "agent_role" in resp.get_json()["message"]

    def test_emit_progress_missing_step(self, client):
        """Missing step is rejected."""
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            data=json.dumps(
                {
                    "agent_role": "coder",
                    "state": "working",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "step" in resp.get_json()["message"]

    def test_emit_progress_missing_state(self, client):
        """Missing state is rejected."""
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            data=json.dumps(
                {
                    "agent_role": "coder",
                    "step": "doing stuff",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "state" in resp.get_json()["message"]

    def test_emit_progress_missing_body(self, client):
        """Missing request body is rejected."""
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_emit_progress_minimal_fields(self, client):
        """Only required fields produces a valid event."""
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            data=json.dumps(
                {
                    "agent_role": "coder",
                    "step": "starting",
                    "state": "working",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        event = resp.get_json()["data"]["event"]
        assert event["detail"] == ""
        assert event["blocker"] == ""

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true"],
        ids=["array", "string", "number", "bool"],
    )
    def test_emit_progress_non_object_body_returns_400(self, client, raw_body):
        """Fix for #2673: non-object JSON bodies must 400, not 500.

        Mirrors the #2656 fix on the decisions route. Without the guard,
        ``body.get("agent_role")`` raises ``AttributeError`` for a
        list/scalar body and the generic handler returns 500.
        """
        resp = client.post(
            "/api/v1/pipelines/issue-100/progress",
            data=raw_body,
            content_type="application/json",
        )
        assert resp.status_code == 400, resp.data
        body = resp.get_json()
        assert body["success"] is False


# ---------------------------------------------------------------------------
# GET /api/v1/pipelines/<pipeline_id>/progress
# ---------------------------------------------------------------------------


class TestQueryProgress:
    """Test querying progress events."""

    def _emit(self, client, pipeline_id="issue-100", **kwargs):
        """Helper to emit a progress event."""
        data = {
            "agent_role": kwargs.get("agent_role", "coder"),
            "step": kwargs.get("step", "working on task"),
            "state": kwargs.get("state", "working"),
        }
        data.update({k: v for k, v in kwargs.items() if k not in data})
        return client.post(
            f"/api/v1/pipelines/{pipeline_id}/progress",
            data=json.dumps(data),
            content_type="application/json",
        )

    def test_query_progress_empty(self, client):
        """Query with no events returns empty list."""
        resp = client.get("/api/v1/pipelines/issue-100/progress")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["data"]["events"] == []
        assert data["data"]["count"] == 0

    def test_query_progress_returns_events(self, client):
        """Query returns previously emitted events."""
        self._emit(client, step="step 1")
        self._emit(client, step="step 2")

        resp = client.get("/api/v1/pipelines/issue-100/progress")
        assert resp.status_code == 200
        events = resp.get_json()["data"]["events"]
        assert len(events) == 2

    def test_query_progress_filter_by_agent(self, client):
        """Filtering by agent_role returns only matching events."""
        self._emit(client, agent_role="coder", step="c1")
        self._emit(client, agent_role="tester", step="t1")
        self._emit(client, agent_role="coder", step="c2")

        resp = client.get("/api/v1/pipelines/issue-100/progress?agent_role=coder")
        events = resp.get_json()["data"]["events"]
        assert len(events) == 2
        assert all(e["agent_role"] == "coder" for e in events)

    def test_query_progress_filter_by_limit(self, client):
        """Limit parameter caps the number of results."""
        for i in range(5):
            self._emit(client, step=f"step-{i}")

        resp = client.get("/api/v1/pipelines/issue-100/progress?limit=2")
        events = resp.get_json()["data"]["events"]
        assert len(events) == 2

    def test_query_progress_different_pipelines_isolated(self, client):
        """Events from different pipelines are isolated."""
        self._emit(client, pipeline_id="issue-100", step="a")
        self._emit(client, pipeline_id="issue-200", step="b")

        resp = client.get("/api/v1/pipelines/issue-100/progress")
        events = resp.get_json()["data"]["events"]
        assert len(events) == 1
        assert events[0]["step"] == "a"

    def test_query_progress_invalid_limit(self, client):
        """Non-integer limit returns an error."""
        resp = client.get("/api/v1/pipelines/issue-100/progress?limit=abc")
        assert resp.status_code == 400

    def test_query_progress_with_all_filters(self, client):
        """Using agent_role and limit together works correctly."""
        for i in range(5):
            self._emit(client, agent_role="coder", step=f"c-{i}")
        self._emit(client, agent_role="tester", step="t-1")

        resp = client.get("/api/v1/pipelines/issue-100/progress?agent_role=coder&limit=3")
        events = resp.get_json()["data"]["events"]
        assert len(events) == 3
        assert all(e["agent_role"] == "coder" for e in events)
