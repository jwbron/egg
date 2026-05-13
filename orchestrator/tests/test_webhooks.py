"""
Tests for webhook routes (orchestrator/webhooks.py).
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


@pytest.fixture
def app():
    """Create a test Flask app with the webhooks blueprint."""
    from flask import Flask
    from webhooks import webhooks_bp

    app = Flask(__name__)
    app.register_blueprint(webhooks_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Test client for the webhooks blueprint."""
    return app.test_client()


class TestGitHubWebhookNonObjectBody:
    """Fix for #2673: non-object JSON bodies must 400, not 500.

    Without the guard, ``payload.get("action")`` raises
    ``AttributeError`` for a list/scalar payload and the generic
    handler returns 500. ``verify_github_signature`` only short-
    circuits the path when ``GITHUB_WEBHOOK_SECRET`` is configured;
    unconfigured deployments skipped straight to the AttributeError.
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
    def test_github_webhook_non_object_body_returns_400(self, client, monkeypatch, raw_body):
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        response = client.post(
            "/api/v1/webhooks/github",
            data=raw_body,
            content_type="application/json",
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "test-delivery-1",
            },
        )
        assert response.status_code == 400, response.data
        body = json.loads(response.data)
        assert body["success"] is False
        assert "json object" in body["message"].lower(), body


class TestManualTriggerNonObjectBody:
    """Fix for #2673: non-object JSON bodies must 400, not 500.

    ``manual_trigger`` is unauthenticated, so a misbehaving caller
    posting ``[1, 2, 3]`` previously fell past the ``if not data``
    truthiness check and tripped ``data.get("event")`` →
    AttributeError → 500.
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
    def test_manual_trigger_non_object_body_returns_400(self, client, raw_body):
        response = client.post(
            "/api/v1/webhooks/trigger",
            data=raw_body,
            content_type="application/json",
        )
        assert response.status_code == 400, response.data
        body = json.loads(response.data)
        assert body["success"] is False
        assert "json object" in body["message"].lower(), body
