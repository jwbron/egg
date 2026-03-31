"""
Integration tests for health route endpoints.

Covers the resolve_alerts endpoint input validation and error handling.
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


@pytest.fixture
def app():
    """Create a test Flask app with the health blueprint."""
    from flask import Flask
    from routes.health import health_bp

    app = Flask(__name__)
    app.register_blueprint(health_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


class TestResolveAlertsEndpoint:
    """Tests for POST /pipelines/<id>/health/alerts/resolve."""

    @patch("health_monitor.get_health_monitor")
    def test_missing_agent_id_returns_400(self, mock_get_monitor, client):
        """Missing agent_id in request body returns 400."""
        mock_get_monitor.return_value = MagicMock()

        response = client.post(
            "/api/v1/pipelines/test-pipeline/health/alerts/resolve",
            data=json.dumps({"alert_type": "heartbeat_timeout"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "agent_id" in data["error"]

    @patch("health_monitor.get_health_monitor")
    def test_missing_alert_type_returns_400(self, mock_get_monitor, client):
        """Missing alert_type in request body returns 400."""
        mock_get_monitor.return_value = MagicMock()

        response = client.post(
            "/api/v1/pipelines/test-pipeline/health/alerts/resolve",
            data=json.dumps({"agent_id": "coder"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False

    @patch("health_monitor.get_health_monitor", return_value=None)
    def test_monitor_not_initialized_returns_503(self, mock_get_monitor, client):
        """Health monitor not initialized returns 503."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/health/alerts/resolve",
            data=json.dumps({"agent_id": "coder", "alert_type": "heartbeat_timeout"}),
            content_type="application/json",
        )

        assert response.status_code == 503
        data = response.get_json()
        assert data["success"] is False
        assert "not initialized" in data["error"]

    @patch("health_monitor.get_health_monitor")
    def test_happy_path_returns_200(self, mock_get_monitor, client):
        """Valid request resolves alerts and returns 200."""
        mock_monitor = MagicMock()
        mock_get_monitor.return_value = mock_monitor

        response = client.post(
            "/api/v1/pipelines/test-pipeline/health/alerts/resolve",
            data=json.dumps({"agent_id": "coder", "alert_type": "heartbeat_timeout"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["resolved"] is True
        mock_monitor.resolve_alerts.assert_called_once_with("coder", "heartbeat_timeout")
