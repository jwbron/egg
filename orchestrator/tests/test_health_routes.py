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

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true"],
        ids=["array", "string", "number", "bool"],
    )
    @patch("health_monitor.get_health_monitor")
    def test_non_object_body_returns_400(self, mock_get_monitor, client, raw_body):
        """Fix for #2673: non-object JSON bodies must 400, not 500.

        Mirrors the #2656 fix on the decisions route. Without the guard,
        ``data.get("agent_id")`` raises ``AttributeError`` for a
        list/scalar body and Flask's default handler returns 500.
        """
        mock_get_monitor.return_value = MagicMock()

        response = client.post(
            "/api/v1/pipelines/test-pipeline/health/alerts/resolve",
            data=raw_body,
            content_type="application/json",
        )
        assert response.status_code == 400, response.data
        data = response.get_json()
        assert data["success"] is False


class TestHealthEndpointIsolationFromMessageStore:
    """Issue #1897 TASK-4-3 (regression lock): ``GET /api/v1/health`` MUST
    NOT import or invoke any ``MessageStore.*`` method.

    Motivation: the health endpoint is the operator's fast path to detect
    that the orchestrator is up. If it ever starts touching the message
    store, a Redis outage or a saturated long-poll gauge would silently
    make the pipeline "look unhealthy" to load balancers and trigger
    cascading restarts. Separating the two concerns (liveness vs
    application data plane) is a well-known SRE pattern; this test is
    the regression lock so a future "helpful" refactor can't drop the
    separation silently.

    The test patches ``get_message_store`` to raise on ANY call and
    confirms /api/v1/health still returns 200. If the endpoint is
    refactored to call into MessageStore, the patched exception will
    propagate and this test will fail — which is exactly the regression
    signal we want.
    """

    @pytest.fixture
    def client(self):
        """Fresh test client with the health blueprint."""
        from flask import Flask
        from routes.health import health_bp

        app = Flask(__name__)
        app.register_blueprint(health_bp)
        app.config["TESTING"] = True
        return app.test_client()

    def test_health_endpoint_does_not_touch_message_store(self, client):
        """Patch MessageStore.* to raise; /api/v1/health must still 200.

        Uses ``side_effect=RuntimeError`` on the singleton accessor so
        any attempt to call into the message store (whether via
        ``get_message_store()`` at module level or a direct
        ``MessageStore()`` construction) surfaces as a crash.
        """
        err = RuntimeError(
            "MessageStore MUST NOT be called from /api/v1/health — "
            "see plan TASK-4-3 and test_health_endpoint_does_not_touch_message_store."
        )
        with (
            patch("message_store.get_message_store", side_effect=err),
            patch("message_store.MessageStore", side_effect=err),
        ):
            response = client.get("/api/v1/health")
            assert response.status_code == 200, (
                f"Health endpoint returned {response.status_code}; "
                "it should not be affected by MessageStore failures."
            )
            # Body should be the standard liveness shape.
            data = response.get_json()
            assert data is not None
            # At minimum a status field — exact shape is owned by the
            # health route and may evolve; the invariant we're locking
            # is "health works regardless of MessageStore".
            assert "status" in data or "success" in data

    def test_health_endpoint_does_not_block_on_inflight_long_polls(self, client):
        """Defense in depth (plan TASK-4-3 related): /api/v1/health
        must not depend on the in-flight-long-polls metric either.

        If a load balancer probes /health every 5s but the metric gauge
        is blocked waiting for a lock held by a long-poll, the whole
        orchestrator would look unhealthy to load balancers. The health
        endpoint must be fully independent of the long-poll tracking
        path.
        """
        with patch(
            "routes.messages._track_long_poll_start",
            side_effect=RuntimeError("_track_long_poll_start MUST NOT be called from /health"),
        ):
            response = client.get("/api/v1/health")
            assert response.status_code == 200


class TestProbeEndpointsAvoidStateStoreOnRequestPath:
    """Issue #2191 regression lock: the kubelet-targeted probe paths
    (``/api/v1/live`` and ``/api/v1/ready``) MUST NOT invoke the
    state-store probe, ``get_state_store``, or any ``subprocess`` call
    on the request path.

    Motivation: under burst BRC load the state-store probe ran inline
    inside ``/api/v1/health`` and queued behind waitress workers held
    by long-polls, pushing tail latency past the 3 s readinessProbe
    timeout and flapping the pod to unhealthy. The fix moves the probe
    to a background thread (``state_store_probe.StateStoreProbe``) so
    request handlers serve cached values only. These tests lock that
    invariant: any future refactor that re-introduces inline probing
    surfaces here as a clear regression signal.
    """

    @pytest.fixture
    def client(self):
        from flask import Flask
        from routes.health import health_bp

        app = Flask(__name__)
        app.register_blueprint(health_bp)
        app.config["TESTING"] = True
        return app.test_client()

    @pytest.fixture(autouse=True)
    def _reset_state_store_probe(self):
        """Each test starts with an empty probe cache."""
        from state_store_probe import reset_state_store_probe_for_test

        reset_state_store_probe_for_test()
        try:
            yield
        finally:
            reset_state_store_probe_for_test()

    def test_live_does_not_invoke_state_store(self, client):
        """``/api/v1/live`` must not call ``get_state_store`` even
        indirectly — it's a pure liveness signal."""
        err = RuntimeError("get_state_store MUST NOT be called from /api/v1/live — see #2191.")
        with patch("state_store.get_state_store", side_effect=err):
            response = client.get("/api/v1/live")
        assert response.status_code == 200
        assert response.get_json() == {"alive": True}

    def test_live_does_not_run_subprocess(self, client):
        """Belt-and-braces check: no ``git`` (or any other subprocess)
        is spawned by ``/api/v1/live``."""
        err = RuntimeError("subprocess MUST NOT be invoked from /api/v1/live — see #2191.")
        with patch("subprocess.run", side_effect=err):
            response = client.get("/api/v1/live")
        assert response.status_code == 200

    def test_ready_does_not_invoke_state_store(self, client):
        """``/api/v1/ready`` reads the cache only; the underlying probe
        runs in a background thread, never on the request path."""
        err = RuntimeError("get_state_store MUST NOT be called from /api/v1/ready — see #2191.")
        with patch("state_store.get_state_store", side_effect=err):
            # Empty cache → 503, but importantly: no exception.
            response = client.get("/api/v1/ready")
        assert response.status_code in (200, 503)

    def test_ready_does_not_run_subprocess(self, client):
        """``/api/v1/ready`` must not spawn ``git`` on the request path."""
        err = RuntimeError("subprocess MUST NOT be invoked from /api/v1/ready — see #2191.")
        with patch("subprocess.run", side_effect=err):
            response = client.get("/api/v1/ready")
        assert response.status_code in (200, 503)

    def test_health_does_not_invoke_state_store_on_request_path(self, client):
        """``/api/v1/health`` must read the cache populated by the BG
        thread, not invoke ``get_state_store`` on the request itself.
        Regression lock against re-introducing inline probing under
        time pressure (which is exactly how #2191 came back after the
        #2167 self-heal fix landed)."""
        err = RuntimeError("get_state_store MUST NOT be called from /api/v1/health — see #2191.")
        with patch("state_store.get_state_store", side_effect=err):
            response = client.get("/api/v1/health")
        assert response.status_code == 200
