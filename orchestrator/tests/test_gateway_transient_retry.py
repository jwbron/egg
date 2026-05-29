"""Tests for bounded transient-connection retry on gateway calls (#2869).

A brief DNS/connection blip during spawn-time session registration (and
slice integration-branch creation) used to hard-fail the whole pipeline
with no retry.  ``_make_request`` now classifies connection-level
failures as :class:`GatewayConnectionError` (a :class:`GatewayError`
subclass), and the spawn-critical call sites opt into
``_retry_transient`` — bounded retry-with-backoff that retries *only*
that subclass, leaving permanent failures (4xx/5xx, auth, timeouts) to
propagate on the first attempt.
"""

from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import gateway_client as gc
import pytest
from gateway_client import (
    GatewayClient,
    GatewayConnectionError,
    GatewayError,
    SessionInfo,
)


@pytest.fixture
def gateway_client():
    return GatewayClient(
        gateway_host="localhost",
        gateway_port=19848,
        launcher_secret="test-secret",
        timeout=5,
    )


@pytest.fixture(autouse=True)
def _no_sleep():
    """Make backoff instantaneous so the retry tests stay fast."""
    with patch.object(gc.time, "sleep", return_value=None):
        yield


def _session_payload() -> dict:
    now = datetime.now()
    return {
        "success": True,
        "data": {
            "session_token": "tok-1",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
        },
    }


class TestMakeRequestClassification:
    def test_url_error_raises_connection_error(self, gateway_client):
        """A connection-level failure (DNS/refused/unreachable) surfaces
        as the transient ``GatewayConnectionError`` subclass."""
        with patch.object(
            gc, "urlopen", side_effect=URLError("[Errno -3] Temporary failure in name resolution")
        ):
            with pytest.raises(GatewayConnectionError) as exc_info:
                gateway_client._make_request("/api/v1/health")
        # Still an instance of the broad base type so existing handlers work.
        assert isinstance(exc_info.value, GatewayError)
        assert "Failed to connect to gateway" in str(exc_info.value)

    def test_http_error_stays_permanent_gateway_error(self, gateway_client):
        """A 4xx/5xx response is permanent — NOT a transient connection
        error — so it must not be retried."""
        http_err = HTTPError(url="http://x", code=403, msg="Forbidden", hdrs=None, fp=None)
        with patch.object(gc, "urlopen", side_effect=http_err):
            with pytest.raises(GatewayError) as exc_info:
                gateway_client._make_request("/api/v1/health")
        assert not isinstance(exc_info.value, GatewayConnectionError)
        assert exc_info.value.status_code == 403

    def test_timeout_is_not_classified_transient(self, gateway_client):
        """A timeout may have reached the gateway and been processed, so
        it stays a plain ``GatewayError`` (not retried) to avoid
        duplicating a non-idempotent operation."""
        with patch.object(gc, "urlopen", side_effect=TimeoutError("slow")):
            with pytest.raises(GatewayError) as exc_info:
                gateway_client._make_request("/api/v1/health")
        assert not isinstance(exc_info.value, GatewayConnectionError)

    def test_response_phase_disconnect_wrapped_but_not_transient(self, gateway_client):
        """A response-phase disconnect (http.client.RemoteDisconnected, a
        ``ConnectionResetError`` — an ``OSError`` but NOT a ``URLError``)
        is wrapped as a plain ``GatewayError`` so callers' ``except
        GatewayError`` handlers catch it, but is NOT classified transient:
        the gateway may have already processed the request, so it must not
        be blindly retried."""
        from http.client import RemoteDisconnected

        with patch.object(
            gc, "urlopen", side_effect=RemoteDisconnected("Remote end closed connection")
        ):
            with pytest.raises(GatewayError) as exc_info:
                gateway_client._make_request("/api/v1/health")
        assert not isinstance(exc_info.value, GatewayConnectionError)
        assert "Gateway connection error" in str(exc_info.value)

    def test_incomplete_read_wrapped_but_not_transient(self, gateway_client):
        """A connection drop *during* ``response.read()`` surfaces as
        http.client.IncompleteRead — an ``HTTPException``, NOT an
        ``OSError`` — so it must be wrapped as a plain ``GatewayError``
        (caught by callers' ``except GatewayError`` handlers) rather than
        propagating raw.  It is NOT classified transient: a partial read
        means the gateway already received the request, so it must not be
        blindly retried."""
        from http.client import IncompleteRead

        with patch.object(gc, "urlopen", side_effect=IncompleteRead(b"partial")):
            with pytest.raises(GatewayError) as exc_info:
                gateway_client._make_request("/api/v1/health")
        assert not isinstance(exc_info.value, GatewayConnectionError)
        assert "Gateway response error" in str(exc_info.value)


class TestRetryTransientHelper:
    def test_retries_then_succeeds(self, gateway_client):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise GatewayConnectionError("blip")
            return "ok"

        result = gateway_client._retry_transient(flaky, operation="test")
        assert result == "ok"
        assert calls["n"] == 3

    def test_reraises_original_connection_error_after_exhaustion(self, gateway_client):
        calls = {"n": 0}

        def always_blip():
            calls["n"] += 1
            raise GatewayConnectionError("persistent blip")

        with pytest.raises(GatewayConnectionError) as exc_info:
            gateway_client._retry_transient(always_blip, operation="test")
        # Bounded: exactly _TRANSIENT_MAX_ATTEMPTS tries, no more.
        assert calls["n"] == gc._TRANSIENT_MAX_ATTEMPTS
        assert "persistent blip" in str(exc_info.value)

    def test_does_not_retry_permanent_gateway_error(self, gateway_client):
        calls = {"n": 0}

        def permanent():
            calls["n"] += 1
            raise GatewayError("forbidden", status_code=403)

        with pytest.raises(GatewayError) as exc_info:
            gateway_client._retry_transient(permanent, operation="test")
        assert not isinstance(exc_info.value, GatewayConnectionError)
        assert calls["n"] == 1, "permanent failures must not be retried"


class TestRegisterSessionRetry:
    def test_retries_transient_then_registers(self, gateway_client):
        attempts = {"n": 0}

        def fake_make_request(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise GatewayConnectionError("Failed to connect to gateway: name resolution")
            return _session_payload()

        with patch.object(gateway_client, "_make_request", side_effect=fake_make_request):
            info = gateway_client.register_session(
                container_id="egg-agent-pipe-1-coder",
                pipeline_id="pipe-1",
                retry_transient=True,
            )
        assert isinstance(info, SessionInfo)
        assert info.session_token == "tok-1"
        assert attempts["n"] == 2, "should retry once then succeed"

    def test_default_does_not_retry(self, gateway_client):
        attempts = {"n": 0}

        def fake_make_request(*args, **kwargs):
            attempts["n"] += 1
            raise GatewayConnectionError("blip")

        with patch.object(gateway_client, "_make_request", side_effect=fake_make_request):
            with pytest.raises(GatewayConnectionError):
                gateway_client.register_session(
                    container_id="abc",
                    pipeline_id="pipe-1",
                )
        assert attempts["n"] == 1, "retry must be opt-in (default off)"


class TestCreateSliceIntegrationBranchRetry:
    def _session_info(self) -> SessionInfo:
        now = datetime.now()
        return SessionInfo(
            session_token="synthetic-tok",
            container_id="temp",
            container_ip=None,
            mode="public",
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )

    def test_push_retries_on_transient_then_succeeds(self, gateway_client):
        """A transient connection blip on the integration-branch push is
        retried rather than failing the slice."""
        push_attempts = {"n": 0}

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_attempts["n"] += 1
                if push_attempts["n"] == 1:
                    raise GatewayConnectionError("Failed to connect to gateway: refused")
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=self._session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(gateway_client, "get_remote_branch_sha", return_value="deadbeef" * 5),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2869/slice-1",
                parent_branch="egg/issue-2869/work",
            )

        assert ok is True
        assert push_attempts["n"] == 2, "push should retry once after the transient blip"

    def test_permanent_push_rejection_still_fails_without_retry(self, gateway_client):
        """A non-fast-forward rejection (permanent 5xx) is not retried —
        it surfaces as ``ok is False`` on the first attempt."""
        push_attempts = {"n": 0}

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_attempts["n"] += 1
                raise GatewayError(
                    "git push failed",
                    status_code=500,
                    details={"returncode": 1, "stderr": "! [rejected] (non-fast-forward)"},
                )
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=self._session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(gateway_client, "get_remote_branch_sha", return_value="feedface" * 5),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2869/slice-1",
                parent_branch="egg/issue-2869/work",
            )

        assert ok is False
        assert push_attempts["n"] == 1, "permanent rejection must not be retried"
