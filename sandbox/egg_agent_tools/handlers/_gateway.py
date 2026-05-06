"""Shared gateway/orchestrator HTTP helpers for handlers.

These are raise-on-failure variants of the helpers in
``sandbox/egg_lib/contract_cli.py`` and ``sandbox/egg_lib/orch_cli.py``.
They are used by pure handler functions and by the CLI shims; the CLI
shims catch :class:`GatewayError` and render today's stderr / exit-code
surface.

Keeping these helpers here (not in ``egg_lib``) means handlers are
self-contained and do not accidentally pull in the CLI's print-and-exit
path.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

try:
    from egg_config.constants import GATEWAY_PORT, ORCHESTRATOR_PORT
except ImportError:  # pragma: no cover - env without egg_config
    GATEWAY_PORT = 9848  # noqa: EGG002
    ORCHESTRATOR_PORT = 9849  # noqa: EGG002

from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_SLICE_ID_PATTERN = re.compile(r"^slice-[0-9]+$")

# Bypass any HTTP(S)_PROXY for internal egg-network requests.
_opener = build_opener(ProxyHandler({}))


def get_gateway_url() -> str:
    """Base URL for the gateway API."""
    url = os.environ.get("GATEWAY_URL")
    if url:
        return url.rstrip("/")
    return f"http://egg-gateway:{GATEWAY_PORT}"


def get_orchestrator_url() -> str:
    """Base URL for the orchestrator API."""
    url = os.environ.get("EGG_ORCHESTRATOR_URL")
    if url:
        return url.rstrip("/")
    return f"http://egg-orchestrator:{ORCHESTRATOR_PORT}"


def get_session_token() -> str | None:
    """Session token for gateway auth (env var or ~/.egg-session-token)."""
    token = os.environ.get("EGG_SESSION_TOKEN")
    if token:
        return token
    token_file = Path.home() / ".egg-session-token"
    if token_file.exists():
        return token_file.read_text().strip()
    return None


def get_container_id() -> str:
    """Current container ID (empty string if unset)."""
    return os.environ.get("CONTAINER_ID", "")


def container_id_field() -> dict[str, str]:
    """Dict with ``container_id`` only when the env var is set.

    Used with ``**`` unpacking in POST bodies so an empty container_id is
    never sent over the wire, matching the pattern in the CLI.
    """
    cid = get_container_id()
    return {"container_id": cid} if cid else {}


def get_repo_path() -> str:
    """Repository path for contract mutations (env var or CWD)."""
    return os.environ.get("EGG_REPO_PATH", str(Path.cwd()))


def get_pipeline_id() -> str | None:
    """Pipeline ID from env (``EGG_PIPELINE_ID``)."""
    return os.environ.get("EGG_PIPELINE_ID") or None


def get_slice_id() -> str | None:
    """Slice ID from env (``EGG_SLICE_ID``).

    Set on agents spawned for a per-slice BRC team (#2403). When
    present, BRC handlers forward it on the signal payload so the
    orchestrator routes ``CONSENSUS_*`` to the slice's tracker
    (see ``orchestrator.peer_consensus._tracker_key``). Pipeline-level
    agents leave it unset and route to the bare pipeline tracker.
    """
    return os.environ.get("EGG_SLICE_ID") or None


def maybe_attach_slice_id(req: dict[str, Any], data: dict[str, Any]) -> None:
    """Forward ``slice_id`` from the request or env onto a signal body.

    Single source of truth for the BRC / progress / heartbeat handlers
    (#2451 follow-up). Per-slice agents set ``EGG_SLICE_ID`` so the
    orchestrator can route their ``CONSENSUS_*`` to the slice tracker
    (#2403) and refresh the slice-scoped gateway session container_id
    (#2451). Callers can also pass ``slice_id`` on ``req`` to override
    (e.g. tests, or operator tooling acting on a specific slice).
    Validation matches the canonical ``slice-<N>`` regex enforced at
    the orchestrator seam so a malformed value cannot smuggle path
    separators into a tracker key or container_id.
    """
    slice_id = req.get("slice_id") or get_slice_id()
    if not slice_id:
        return
    if not isinstance(slice_id, str) or not _SLICE_ID_PATTERN.fullmatch(slice_id):
        raise HandlerError(f"Invalid slice_id {slice_id!r}: must match 'slice-<N>'")
    data["slice_id"] = slice_id


def resolve_slice_id(req: dict[str, Any]) -> str | None:
    """Return a validated ``slice_id`` (from ``req`` or env), or ``None``.

    Same validation as :func:`maybe_attach_slice_id` but returns the
    value instead of mutating a payload dict. Used by callers that
    thread ``slice_id`` through their own data structures (e.g. the
    wait-loop heartbeat emitter, which builds the payload itself).
    """
    slice_id = req.get("slice_id") or get_slice_id()
    if not slice_id:
        return None
    if not isinstance(slice_id, str) or not _SLICE_ID_PATTERN.fullmatch(slice_id):
        raise HandlerError(f"Invalid slice_id {slice_id!r}: must match 'slice-<N>'")
    return slice_id


def get_issue_number() -> int | None:
    """Issue number from env (``EGG_ISSUE_NUMBER``)."""
    raw = os.environ.get("EGG_ISSUE_NUMBER")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_agent_role() -> str | None:
    """Agent role from env (``EGG_AGENT_ROLE``)."""
    return os.environ.get("EGG_AGENT_ROLE") or None


def get_contract_identifier() -> int | str | None:
    """Resolve the contract identifier used by the contract gateway.

    Priority (mirrors ``contract_cli.get_contract_identifier``):
    1. ``EGG_PIPELINE_ID`` env var
    2. ``EGG_ISSUE_NUMBER`` env var
    """
    pid = get_pipeline_id()
    if pid is not None:
        return pid
    return get_issue_number()


def get_phase() -> str | None:
    """Current pipeline phase from env (``EGG_PHASE``)."""
    return os.environ.get("EGG_PHASE") or None


def _parse_http_error(exc: HTTPError) -> GatewayError:
    """Convert an HTTPError to a GatewayError preserving server fields."""
    try:
        body = json.loads(exc.read().decode())
        message = body.get("message", str(exc))
        details = body.get("details") or body.get("data") or {}
    except Exception:
        message = str(exc)
        details = {}
    return GatewayError(
        message,
        status_code=getattr(exc, "code", None),
        details=details if isinstance(details, dict) else {"raw": details},
    )


def gateway_request(
    endpoint: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 30,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Make an authenticated request against the gateway API.

    Raises:
        GatewayError: On HTTP, URL, or timeout failure.  The ``message``
            mirrors the stderr text previously printed by
            ``contract_cli.make_gateway_request`` before it called
            ``sys.exit``.
    """
    base = get_gateway_url()
    url = f"{base}{endpoint}"
    if params:
        url += "?" + urlencode(params)

    req_headers: dict[str, str] = {"Content-Type": "application/json"}
    token = get_session_token()
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    if headers:
        req_headers.update(headers)

    body = json.dumps(data).encode() if data is not None else None
    try:
        request = Request(url, data=body, headers=req_headers, method=method)
        with _opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode())  # type: ignore[no-any-return]
    except HTTPError as exc:
        raise _parse_http_error(exc) from exc
    except URLError as exc:
        raise GatewayError(
            f"connecting to gateway: {exc.reason}",
            hint="Is the gateway running?",
        ) from exc
    except TimeoutError as exc:
        raise GatewayError("Request to gateway timed out") from exc


def orchestrator_request(
    endpoint: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Make a request against the orchestrator API.

    Attaches the lifecycle secret when present, matching
    ``orch_cli.orch_request``.  Raises :class:`GatewayError` on failure.
    """
    base = get_orchestrator_url()
    url = f"{base}{endpoint}"

    req_headers: dict[str, str] = {"Content-Type": "application/json"}
    lifecycle_secret = os.environ.get("EGG_LIFECYCLE_SECRET")
    if lifecycle_secret:
        req_headers["Authorization"] = f"Bearer {lifecycle_secret}"
        req_headers["X-Egg-Source"] = "cli"
    if headers:
        req_headers.update(headers)

    body = json.dumps(data).encode() if data is not None else None
    try:
        request = Request(url, data=body, headers=req_headers, method=method)
        with _opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode())  # type: ignore[no-any-return]
    except HTTPError as exc:
        raise _parse_http_error(exc) from exc
    except URLError as exc:
        raise GatewayError(f"Connection error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GatewayError(f"Request timed out: {url}") from exc
