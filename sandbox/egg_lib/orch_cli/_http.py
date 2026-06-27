"""Core HTTP/transport infra for the egg-orch CLI: API request helpers, URL/env/token resolution, ID validation, and JSON output.

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import json
import os
import re
import sys
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

try:
    from egg_config.constants import (
        GATEWAY_PORT,
        ORCHESTRATOR_PORT,
    )
except ImportError:
    ORCHESTRATOR_PORT = 9849  # noqa: EGG002
    GATEWAY_PORT = 9848  # noqa: EGG002

# Validation pattern for IDs used in URL path segments
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

# Canonical ``slice-<N>`` shape — mirrors orchestrator's
# ``slice_id_validation.SLICE_ID_PATTERN`` and the handler-side regex in
# ``egg_agent_tools.handlers.{brc,progress}``. Kept inline rather than
# importing from the orchestrator package because ``egg_lib`` ships in
# the sandbox and must not depend on orchestrator code.
_SLICE_ID_PATTERN = re.compile(r"^slice-[0-9]+$")


class ApiError(Exception):
    """Error from an API request."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details


def _proposal_version_type(raw: str) -> int:
    """argparse type for ``--ack-version`` / ``--nack-version``.

    Mirrors the handler-side ``_require_version_int`` constraint at parse time
    so the error surfaces in ``--help`` and the rejection lands before the
    request is built.  v0 is meaningless because it predates the producer's
    first ``CONSENSUS_PROPOSE``.
    """
    try:
        version = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer; got {raw!r}") from exc
    if version < 1:
        raise argparse.ArgumentTypeError(
            f"must be >= 1; got {version} (v0 means no proposal exists yet)"
        )
    return version


def validate_id(value: str, name: str) -> str:
    """Validate that an ID is safe for use in URL paths.

    Accepts UUIDs and alphanumeric strings with hyphens, underscores, and dots.
    """
    if not value:
        print(f"Error: {name} cannot be empty", file=sys.stderr)
        sys.exit(1)
    if not _SAFE_ID_PATTERN.match(value):
        print(
            f"Error: Invalid {name} '{value}': must contain only "
            "alphanumeric characters, hyphens, underscores, and dots",
            file=sys.stderr,
        )
        sys.exit(1)
    return quote(value, safe="")


def get_orchestrator_url() -> str:
    """Get the orchestrator base URL.

    Uses hostname instead of IP so the CLI works from both egg-isolated
    and egg-external Docker networks.
    """
    url = os.environ.get("EGG_ORCHESTRATOR_URL")
    if url:
        return url.rstrip("/")
    return f"http://egg-orchestrator:{ORCHESTRATOR_PORT}"


def get_gateway_url() -> str:
    """Get the gateway base URL."""
    url = os.environ.get("GATEWAY_URL")
    if url:
        return url.rstrip("/")
    return f"http://egg-gateway:{GATEWAY_PORT}"


def get_pipeline_id_from_env() -> str | None:
    """Get pipeline ID from environment if set."""
    return os.environ.get("EGG_PIPELINE_ID")


def get_slice_id_from_env() -> str | None:
    """Get slice ID from environment (``EGG_SLICE_ID``) if set.

    Set on agents spawned for a per-slice BRC team (#2403). When
    present, consensus signal commands forward it on the request body
    so the orchestrator routes ``CONSENSUS_*`` to the slice's tracker.

    Prefer :func:`resolve_slice_id` for CLI commands that forward the
    value — that helper validates against the canonical ``slice-<N>``
    shape so a misconfigured env var fails fast locally instead of
    round-tripping a 400 through the orchestrator.
    """
    return os.environ.get("EGG_SLICE_ID") or None


def resolve_slice_id() -> str | None:
    """Validated counterpart of :func:`get_slice_id_from_env` (#2473).

    Returns the canonical ``slice-<N>`` value, or ``None`` when unset.
    A non-empty value that fails the regex prints to stderr and exits
    with status 1, so CLI commands surface a misconfigured
    ``EGG_SLICE_ID`` locally rather than letting the orchestrator
    reject the request via ``slice_id_validation.extract_slice_id``.
    """
    raw = os.environ.get("EGG_SLICE_ID") or None
    if raw is None:
        return None
    if not _SLICE_ID_PATTERN.fullmatch(raw):
        print(
            f"Error: Invalid EGG_SLICE_ID {raw!r}: must match 'slice-<N>'",
            file=sys.stderr,
        )
        sys.exit(1)
    return raw


def get_agent_role_from_env() -> str | None:
    """Get agent role from environment if set."""
    return os.environ.get("EGG_AGENT_ROLE")


def get_issue_number() -> int | None:
    """Get the current issue number from environment."""
    issue_str = os.environ.get("EGG_ISSUE_NUMBER")
    if issue_str:
        try:
            return int(issue_str)
        except ValueError:
            return None
    return None


def get_session_token() -> str | None:
    """Get session token for gateway auth."""
    from pathlib import Path

    token = os.environ.get("EGG_SESSION_TOKEN")
    if token:
        return token
    token_file = Path.home() / ".egg-session-token"
    if token_file.exists():
        return token_file.read_text().strip()
    return None


# Bypass proxy for internal network requests
_opener = build_opener(ProxyHandler({}))


def api_request(
    base_url: str,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Make an HTTP request to an internal API.

    Args:
        base_url: Base URL (orchestrator or gateway)
        endpoint: API path
        method: HTTP method
        data: JSON body data
        timeout: Request timeout in seconds
        headers: Additional headers

    Returns:
        Response JSON

    Raises:
        ApiError: On request failure
    """
    url = f"{base_url}{endpoint}"
    req_headers: dict[str, str] = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    body = json.dumps(data).encode() if data is not None else None

    try:
        request = Request(url, data=body, headers=req_headers, method=method)
        with _opener.open(request, timeout=timeout) as response:
            result: dict[str, Any] = json.loads(response.read().decode())
            return result
    except HTTPError as e:
        error_body = e.read().decode()
        try:
            error_data = json.loads(error_body)
            raise ApiError(
                error_data.get("message", str(e)),
                status_code=e.code,
                details=error_data.get("details"),
            ) from e
        except json.JSONDecodeError:
            raise ApiError(f"{e}: {error_body}", status_code=e.code) from e
    except URLError as e:
        raise ApiError(f"Connection error: {e.reason}") from e
    except TimeoutError as e:
        raise ApiError(f"Request timed out: {url}") from e
    except HTTPException as e:
        # ``http.client.RemoteDisconnected`` and friends — raised when the
        # peer closes the connection mid-flight (e.g. orch pod restart during
        # a long-poll). Not wrapped by urllib, so without this branch they
        # propagate raw past every caller's ``except ApiError`` (issue #2412).
        raise ApiError(f"HTTP protocol error: {url}: {e}") from e
    except OSError as e:
        # ``ConnectionResetError``, ``ConnectionRefusedError``, and other
        # socket-level errors that bypass the ``URLError`` wrapper.
        raise ApiError(f"Network error: {url}: {e}") from e


def api_request_or_exit(
    base_url: str,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Make an API request, printing errors and exiting on failure."""
    try:
        return api_request(base_url, endpoint, method, data, timeout, headers)
    except ApiError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        if e.status_code:
            print(f"Status: {e.status_code}", file=sys.stderr)
        if e.details:
            print(f"Details: {json.dumps(e.details, indent=2)}", file=sys.stderr)
        sys.exit(1)


def orch_request(
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Make a request to the orchestrator API.

    Attaches ``Authorization: Bearer <EGG_LIFECYCLE_SECRET>`` and
    ``X-Egg-Source: cli`` when the env var is present. Lifecycle-control
    endpoints (HITL resolve, pipeline CRUD, phase overrides, container
    spawn/stop) require this header. Agents don't get the env var, so
    they'll 401; humans running ``egg-orch`` from their shell will pass.
    """
    headers: dict[str, str] = {}
    lifecycle_secret = os.environ.get("EGG_LIFECYCLE_SECRET")
    if lifecycle_secret:
        headers["Authorization"] = f"Bearer {lifecycle_secret}"
        headers["X-Egg-Source"] = "cli"
    return api_request_or_exit(
        get_orchestrator_url(), endpoint, method, data, timeout, headers or None
    )


def gateway_request(
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Make a request to the gateway API with session auth."""
    headers: dict[str, str] = {}
    token = get_session_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return api_request_or_exit(get_gateway_url(), endpoint, method, data, timeout, headers)


def print_json(data: Any) -> None:
    """Pretty-print JSON data."""
    print(json.dumps(data, indent=2))


def require_pipeline_id(args: argparse.Namespace) -> str:
    """Get pipeline_id from args or environment, validate, and return URL-safe value."""
    pid = getattr(args, "pipeline_id", None) or get_pipeline_id_from_env()
    if not pid:
        print(
            "Error: pipeline_id required. Provide as argument or set EGG_PIPELINE_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    return validate_id(pid, "pipeline_id")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
