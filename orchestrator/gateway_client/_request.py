"""Gateway HTTP request plumbing + health checks (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

import json
import random
import time
from collections.abc import Callable
from http.client import HTTPException
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request

import gateway_client as _pkg
from gateway_client import GatewayConnectionError, GatewayError
from gateway_client._models import GatewayHealth

T = TypeVar("T")

# Bounded retry budget for transient gateway connection failures (#2869).
_TRANSIENT_MAX_ATTEMPTS = 4
_TRANSIENT_BASE_DELAY = 1.0
_TRANSIENT_MAX_DELAY = 8.0
_TRANSIENT_JITTER = 0.2


def _make_request(
    self,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    use_launcher_auth: bool = False,
    bearer_token: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Make an HTTP request to the gateway.

    Args:
        endpoint: API endpoint path
        method: HTTP method
        data: Request body data
        use_launcher_auth: Use launcher secret for auth
        bearer_token: Explicit bearer token (takes precedence over launcher auth)
        timeout: Per-request timeout in seconds (overrides client default)

    Returns:
        Response JSON data

    Raises:
        GatewayError: On request failure
    """
    url = f"{self.base_url}{endpoint}"
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    elif use_launcher_auth and self.launcher_secret:
        headers["Authorization"] = f"Bearer {self.launcher_secret}"

    body = json.dumps(data).encode() if data else None
    effective_timeout = timeout if timeout is not None else self.timeout

    try:
        request = Request(url, data=body, headers=headers, method=method)
        with _pkg.urlopen(request, timeout=effective_timeout) as response:
            result: dict[str, Any] = json.loads(response.read().decode())
            return result
    except HTTPError as e:
        try:
            error_data = json.loads(e.read().decode())
            # ``make_error`` on the gateway side puts error details
            # under ``"data"`` (via ``make_response(success=False,
            # ..., data=details, ...)``). Read ``"data"`` first and
            # fall back to ``"details"`` for callers / routes that
            # emit the key directly (e.g. ``mode_gate`` private-mode
            # 403). Without this fallback, the downstream
            # ``exc.details`` is always ``None`` for /api/v1/git/execute
            # failures and the ``returncode != 1`` warning gate in
            # ``merge_base`` / ``_sha_is_ancestor`` fires noisily on
            # every legitimate exit-1 (no common ancestor / not-an-
            # ancestor) case. Reviewer feedback on PR #2895.
            raise GatewayError(
                error_data.get("message", str(e)),
                status_code=e.code,
                details=error_data.get("data") or error_data.get("details"),
            )
        except json.JSONDecodeError:
            raise GatewayError(str(e), status_code=e.code) from e
    except URLError as e:
        # Connection-level failure: DNS resolution failure
        # ([Errno -3]), connection refused, host unreachable, send-phase
        # OSError, etc.  These correspond to the request not being
        # delivered/processed, so it is safe to retry regardless of
        # HTTP-method idempotency.  Note this is specifically the
        # ``URLError`` family — a response-phase disconnect surfaces as
        # http.client.RemoteDisconnected (a ConnectionResetError, NOT a
        # URLError) and deliberately does NOT land here (see the trailing
        # ``except OSError`` below), because such a disconnect may mean
        # the gateway already processed the request.  Raise the
        # GatewayConnectionError subclass so callers that opt into
        # transient retry (spawn-time session registration,
        # integration-branch creation — #2869) can distinguish these
        # from permanent failures; ``except GatewayError`` handlers
        # still catch it unchanged.
        raise GatewayConnectionError(f"Failed to connect to gateway: {e.reason}") from e
    except TimeoutError as e:
        # A timeout is NOT classified as transient-retryable: unlike a
        # connection-level failure, the request may have reached the
        # gateway and been processed, so a blind retry could duplicate
        # a non-idempotent operation (e.g. a second session).
        raise GatewayError("Gateway request timed out") from e
    except OSError as e:
        # Defense-in-depth for connection-level OSErrors that are NOT a
        # URLError — most notably http.client.RemoteDisconnected
        # (ConnectionResetError) from a response-phase disconnect.
        # Without this, such an error would propagate raw and slip past
        # callers' ``except GatewayError`` handlers (e.g. the spawner's
        # KubernetesSpawnError wrap).  Wrap it as a plain GatewayError —
        # deliberately NOT a GatewayConnectionError, since a
        # response-phase disconnect may mean the gateway already
        # processed the request and so must not be blindly retried.
        # Must stay LAST among the OSError-derived branches: URLError
        # and TimeoutError both subclass OSError and are handled by
        # their dedicated branches above.
        raise GatewayError(f"Gateway connection error: {e}") from e
    except HTTPException as e:
        # Defense-in-depth for response-phase protocol errors that are
        # NOT an OSError — most notably http.client.IncompleteRead, when
        # the connection drops mid-``response.read()`` (line above).
        # ``IncompleteRead``/``BadStatusLine`` subclass HTTPException,
        # not OSError, so without this branch they would propagate raw
        # and slip past callers' ``except GatewayError`` handlers (e.g.
        # the spawner's KubernetesSpawnError wrap).  Wrap as a plain
        # GatewayError — deliberately NOT a GatewayConnectionError, since
        # a partial response means the gateway already received and may
        # have processed the request, so it must not be blindly retried.
        # (http.client.RemoteDisconnected is also an OSError and so is
        # caught by the branch above before reaching here.)
        raise GatewayError(f"Gateway response error: {e}") from e


def _retry_transient(  # noqa: UP047  -- verbatim move; UP047 fires only now that this is a module-level fn (exempt as a method pre-split)
    self,
    fn: Callable[[], T],
    *,
    operation: str,
) -> T:
    """Run ``fn`` with bounded retry-with-backoff on transient gateway
    connection failures (#2869).

    Only :class:`GatewayConnectionError` (connection refused, DNS
    resolution failure, host unreachable — the request never landed)
    is retried; permanent failures (4xx/5xx :class:`GatewayError`,
    auth, timeouts) propagate on the first attempt.  After the retry
    budget is exhausted the original ``GatewayConnectionError`` is
    re-raised so existing ``except GatewayError`` handlers keep
    working.

    Args:
        fn: Zero-argument callable performing the gateway request.
        operation: Short label for logging (e.g. ``"register session"``).
    """
    last_err: GatewayConnectionError | None = None
    for attempt in range(_TRANSIENT_MAX_ATTEMPTS):
        try:
            return fn()
        except GatewayConnectionError as e:
            last_err = e
            if attempt + 1 < _TRANSIENT_MAX_ATTEMPTS:
                delay = min(
                    _TRANSIENT_BASE_DELAY * (2**attempt),
                    _TRANSIENT_MAX_DELAY,
                )
                # Symmetric jitter to avoid synchronized retries when
                # several concurrent spawns hit the same blip.
                delay += delay * _TRANSIENT_JITTER * random.uniform(-1, 1)
                delay = max(0.0, delay)
                _pkg.logger.warning(
                    "Transient gateway connection failure; retrying",
                    operation=operation,
                    attempt=attempt + 1,
                    max_attempts=_TRANSIENT_MAX_ATTEMPTS,
                    delay_seconds=round(delay, 2),
                    error=str(e),
                )
                time.sleep(delay)
            else:
                _pkg.logger.error(
                    "Transient gateway connection failure; retries exhausted",
                    operation=operation,
                    attempts=attempt + 1,
                    error=str(e),
                )
    # Loop only exits without returning when every attempt raised.
    assert last_err is not None  # narrows the type for the re-raise
    raise last_err


def check_health(self) -> GatewayHealth:
    """Check gateway health status.

    Returns:
        GatewayHealth with status information
    """
    try:
        result = self._make_request("/api/v1/health")

        return GatewayHealth(
            healthy=result.get("status") == "healthy",
            status=result.get("status", "unknown"),
            version=result.get("version"),
            uptime_seconds=result.get("uptime_seconds"),
        )
    except GatewayError as e:
        return GatewayHealth(
            healthy=False,
            status="unhealthy",
            error=str(e),
        )
    except Exception as e:
        return GatewayHealth(
            healthy=False,
            status="unreachable",
            error=str(e),
        )


def wait_for_healthy(
    self,
    timeout_seconds: int = 60,
    check_interval: float = 2.0,
) -> bool:
    """Wait for gateway to become healthy.

    Args:
        timeout_seconds: Maximum time to wait
        check_interval: Time between checks

    Returns:
        True if gateway became healthy, False if timeout
    """
    import time

    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        health = self.check_health()
        if health.healthy:
            _pkg.logger.info("Gateway is healthy", version=health.version)
            return True

        _pkg.logger.debug(
            "Waiting for gateway",
            status=health.status,
            error=health.error,
        )
        time.sleep(check_interval)

    _pkg.logger.warning("Gateway health check timed out")
    return False
