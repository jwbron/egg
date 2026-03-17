"""
Tests for anchor API routes (orchestrator/routes/anchors.py).
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


def _make_valid_anchor_data(agent_id="coder-abc12345", role="coder"):
    """Create valid anchor data for API requests."""
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc).isoformat()
    return {
        "_meta": {
            "schema_version": "1.0",
            "created_at": now,
            "updated_at": now,
            "sequence": 1,
        },
        "agent_id": agent_id,
        "role": role,
        "team": [],
        "task": {"id": "task-1", "description": "Test task", "phase": "implement"},
        "status": "working",
        "pipeline_id": "issue-1032",
        "progress": [],
        "decisions": [],
        "brc_state": {"phase": "orient", "acks": [], "nacks": []},
        "key_context": [],
        "errors_encountered": [],
        "files_modified": [],
    }


@pytest.fixture
def mock_redis():
    """Create a mock Redis client that behaves like a simple dict store."""
    store: dict[str, str] = {}
    mock = MagicMock()

    def mock_set(key, value, **kwargs):
        store[key] = value
        return True

    def mock_get(key):
        return store.get(key)

    def mock_delete(key):
        if key in store:
            del store[key]
            return 1
        return 0

    def mock_scan_iter(pattern):
        import fnmatch
        return [k for k in store if fnmatch.fnmatch(k, pattern)]

    def mock_expire(key, ttl):
        return key in store

    mock.set = MagicMock(side_effect=mock_set)
    mock.get = MagicMock(side_effect=mock_get)
    mock.delete = MagicMock(side_effect=mock_delete)
    mock.scan_iter = MagicMock(side_effect=mock_scan_iter)
    mock.expire = MagicMock(side_effect=mock_expire)
    return mock


@pytest.fixture
def app(mock_redis):
    """Create a test Flask app with the anchors blueprint."""
    # Reset the module-level Redis client
    import routes.anchors as anchors_mod
    original_client = anchors_mod._redis_client
    anchors_mod._redis_client = mock_redis

    from flask import Flask
    from routes.anchors import anchors_bp

    app = Flask(__name__)
    app.register_blueprint(anchors_bp)
    app.config["TESTING"] = True
    yield app

    anchors_mod._redis_client = original_client


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


class TestCreateAnchor:
    """Tests for POST /api/v1/anchors/{agent_id}."""

    def test_create_anchor(self, client):
        """POST creates a new anchor."""
        data = _make_valid_anchor_data()
        response = client.post(
            "/api/v1/anchors/coder-abc12345",
            data=json.dumps(data),
            content_type="application/json",
        )
        assert response.status_code in (200, 201)

    def test_create_missing_body(self, client):
        """POST without body returns 400 or 415."""
        response = client.post("/api/v1/anchors/coder-abc12345")
        assert response.status_code in (400, 415)

    def test_create_invalid_schema_returns_400(self, client):
        """POST with invalid schema returns 400."""
        response = client.post(
            "/api/v1/anchors/coder-abc12345",
            data=json.dumps({"invalid": "data"}),
            content_type="application/json",
        )
        assert response.status_code == 400


class TestGetAnchor:
    """Tests for GET /api/v1/anchors/{agent_id}."""

    def test_get_existing_anchor(self, client):
        """GET retrieves an existing anchor."""
        data = _make_valid_anchor_data()
        client.post(
            "/api/v1/anchors/coder-abc12345",
            data=json.dumps(data),
            content_type="application/json",
        )
        response = client.get(
            "/api/v1/anchors/coder-abc12345?pipeline_id=issue-1032"
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["data"]["anchor"]["agent_id"] == "coder-abc12345"

    def test_get_nonexistent_returns_404(self, client):
        """GET for nonexistent anchor returns 404."""
        response = client.get(
            "/api/v1/anchors/nonexistent-agent?pipeline_id=issue-1032"
        )
        assert response.status_code == 404


class TestDeleteAnchor:
    """Tests for DELETE /api/v1/anchors/{agent_id}."""

    def test_delete_existing_anchor(self, client):
        """DELETE removes an existing anchor."""
        data = _make_valid_anchor_data()
        client.post(
            "/api/v1/anchors/coder-abc12345",
            data=json.dumps(data),
            content_type="application/json",
        )
        response = client.delete(
            "/api/v1/anchors/coder-abc12345?pipeline_id=issue-1032"
        )
        assert response.status_code in (200, 204)

    def test_delete_nonexistent_anchor(self, client):
        """DELETE for nonexistent anchor returns 404."""
        response = client.delete(
            "/api/v1/anchors/nonexistent-agent?pipeline_id=issue-1032"
        )
        assert response.status_code == 404


class TestTeamAnchor:
    """Tests for GET /api/v1/anchors/team/{pipeline_id}."""

    def test_team_anchor_empty(self, client):
        """Team anchor with no agents returns empty list."""
        response = client.get("/api/v1/anchors/team/issue-1032")
        assert response.status_code == 200
        body = response.get_json()
        assert body["data"]["team_anchor"]["agent_count"] == 0

    def test_team_anchor_with_agents(self, client):
        """Team anchor includes all pipeline agents."""
        # Create two anchors
        data1 = _make_valid_anchor_data("coder-abc12345", "coder")
        data2 = _make_valid_anchor_data("tester-def67890", "tester")
        data2["agent_id"] = "tester-def67890"
        data2["role"] = "tester"

        client.post(
            "/api/v1/anchors/coder-abc12345",
            data=json.dumps(data1),
            content_type="application/json",
        )
        client.post(
            "/api/v1/anchors/tester-def67890",
            data=json.dumps(data2),
            content_type="application/json",
        )

        response = client.get("/api/v1/anchors/team/issue-1032")
        assert response.status_code == 200
        body = response.get_json()
        assert body["data"]["team_anchor"]["agent_count"] == 2


class TestGCAnchors:
    """Tests for POST /api/v1/anchors/gc/{pipeline_id}."""

    def test_gc_completed_pipeline(self, client):
        """GC deletes anchors for completed pipelines."""
        data = _make_valid_anchor_data()
        client.post(
            "/api/v1/anchors/coder-abc12345",
            data=json.dumps(data),
            content_type="application/json",
        )

        response = client.post(
            "/api/v1/anchors/gc/issue-1032",
            data=json.dumps({"status": "completed"}),
            content_type="application/json",
        )
        assert response.status_code == 200

    def test_gc_failed_pipeline_sets_ttl(self, client, mock_redis):
        """GC sets TTL for failed pipeline anchors."""
        data = _make_valid_anchor_data()
        client.post(
            "/api/v1/anchors/coder-abc12345",
            data=json.dumps(data),
            content_type="application/json",
        )

        response = client.post(
            "/api/v1/anchors/gc/issue-1032",
            data=json.dumps({"status": "failed"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        mock_redis.expire.assert_called()
