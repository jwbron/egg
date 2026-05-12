"""Unit tests for container API routes (orchestrator/routes/containers.py).

Covers the body-validation guard on the lifecycle-authed POST routes
(``/spawn``, ``/containers/<id>/stop``) — the #2673 sweep of the
``request.get_json() or {}`` + ``.get(...)`` pattern that was leaking
500s for non-object JSON bodies.
"""

import json
import sys
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


_LIFECYCLE_SECRET = "test-secret"


@pytest.fixture
def app(monkeypatch):
    """Create a test Flask app with the containers blueprint."""
    monkeypatch.setenv("EGG_LIFECYCLE_SECRET", _LIFECYCLE_SECRET)

    from flask import Flask
    from routes.containers import containers_bp

    app = Flask(__name__)
    app.register_blueprint(containers_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


@pytest.fixture
def auth_headers():
    """Bearer header valid for the lifecycle decorator under test."""
    return {"Authorization": f"Bearer {_LIFECYCLE_SECRET}"}


class TestNonObjectJsonBodyReturns400:
    """Fix for #2673: non-object JSON bodies must 400, not 500.

    Mirrors the #2656 fix on the decisions route. Previously
    ``data = request.get_json() or {}`` left a list / scalar in
    ``data`` and ``data.get(...)`` raised ``AttributeError`` →
    the generic exception handler returned 500.
    """

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true"],
        ids=["array", "string", "number", "bool"],
    )
    def test_spawn_non_object_body_returns_400(self, client, auth_headers, raw_body):
        response = client.post(
            "/api/v1/pipelines/test-pipeline/spawn",
            data=raw_body,
            content_type="application/json",
            headers=auth_headers,
        )
        assert response.status_code == 400, response.data
        body = json.loads(response.data)
        assert body["success"] is False

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true"],
        ids=["array", "string", "number", "bool"],
    )
    def test_stop_non_object_body_returns_400(self, client, auth_headers, raw_body):
        response = client.post(
            "/api/v1/pipelines/test-pipeline/containers/abc123/stop",
            data=raw_body,
            content_type="application/json",
            headers=auth_headers,
        )
        assert response.status_code == 400, response.data
        body = json.loads(response.data)
        assert body["success"] is False
