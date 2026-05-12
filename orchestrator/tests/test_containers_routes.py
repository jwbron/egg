"""Tests for container API routes (orchestrator/routes/containers.py).

The container-spawner / monitor / backend integrations are covered by
``test_container_spawner*.py`` and ``test_container_backend.py``. This
file covers route-level input validation that runs before the backend
is touched — specifically the #2656 sweep landed in PR #2645.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


@pytest.fixture
def app():
    """Create a test Flask app with the containers blueprint."""
    from flask import Flask
    from routes.containers import containers_bp

    app = Flask(__name__)
    app.register_blueprint(containers_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client. Lifecycle auth is injected by the
    orchestrator-level ``_inject_lifecycle_auth`` autouse fixture."""
    return app.test_client()


class TestNonObjectJsonBodyReturns400:
    """Sweep of the #2656 fix into the containers routes (PR #2645).

    ``spawn_container`` and ``stop_container`` previously did
    ``data = request.get_json() or {}`` then ``data.get(...)``. When the
    body was syntactically-valid JSON but not an object (list / scalar),
    ``.get`` raised ``AttributeError`` and the handler's generic
    exception mapper returned 500. Both handlers now reject non-dict
    bodies with ``400 Request body must be a JSON object`` before any
    ``.get`` call, mirroring the original decisions-route fix.

    Both routes sit behind ``@require_lifecycle_secret``; the
    autouse ``_inject_lifecycle_auth`` fixture in
    ``orchestrator/tests/conftest.py`` injects the bearer token. The
    backend (``_get_backend``) is patched per-test so the body-validation
    rejection is never racing a real Docker / Kubernetes call.
    """

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true", "[]", "0", "false", '""'],
        ids=[
            "array",
            "string",
            "number",
            "bool",
            "empty-array",
            "zero",
            "false",
            "empty-string",
        ],
    )
    def test_spawn_non_object_json_body_returns_400(self, client, raw_body):
        """POST /pipelines/<id>/spawn with non-object JSON body → 400."""
        with patch("routes.containers._get_backend") as mock_get_backend:
            response = client.post(
                "/api/v1/pipelines/test-pipeline/spawn",
                content_type="application/json",
                data=raw_body,
            )
        assert response.status_code == 400, response.data
        body = response.get_json()
        assert body["success"] is False
        assert "json object" in body["message"].lower(), body
        # Body validation must run before backend dispatch — the
        # backend should never have been asked for a handle.
        mock_get_backend.assert_not_called()

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true", "[]", "0", "false", '""'],
        ids=[
            "array",
            "string",
            "number",
            "bool",
            "empty-array",
            "zero",
            "false",
            "empty-string",
        ],
    )
    def test_stop_non_object_json_body_returns_400(self, client, raw_body):
        """POST /pipelines/<id>/containers/<cid>/stop with non-object JSON body → 400."""
        with patch("routes.containers._get_backend") as mock_get_backend:
            response = client.post(
                "/api/v1/pipelines/test-pipeline/containers/abc123/stop",
                content_type="application/json",
                data=raw_body,
            )
        assert response.status_code == 400, response.data
        body = response.get_json()
        assert body["success"] is False
        assert "json object" in body["message"].lower(), body
        mock_get_backend.assert_not_called()
