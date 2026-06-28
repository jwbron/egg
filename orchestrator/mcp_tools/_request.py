"""PipelineToolHandler orchestrator + gateway HTTP request plumbing (#3312 slice-13).

Method bodies extracted verbatim from the pre-split
``orchestrator/mcp_tools.py`` and bound onto ``PipelineToolHandler``
in the package barrel (``orchestrator/mcp_tools/__init__.py``). They
take ``self`` explicitly and are AST-identical to the originals.
Barrel globals (``logger`` etc.) are imported from the package so
they stay a single binding.
"""

from __future__ import annotations

import os
from typing import Any

from mcp_tools import GATEWAY_PORT


def _make_request(
    self,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Make HTTP request to orchestrator.

    Always attaches ``Authorization: Bearer <EGG_LIFECYCLE_SECRET>`` and
    ``X-Egg-Source: mcp`` when the secret is configured. The in-process
    MCP server runs inside the orchestrator Deployment, so it reads the
    same env var as the lifecycle-secret decorator.
    """
    import json
    from urllib.request import ProxyHandler, Request, build_opener

    url = f"{self.orchestrator_url}{endpoint}"
    headers = {"Content-Type": "application/json"}
    lifecycle_secret = os.environ.get("EGG_LIFECYCLE_SECRET")
    if lifecycle_secret:
        headers["Authorization"] = f"Bearer {lifecycle_secret}"
        headers["X-Egg-Source"] = "mcp"
    # Always send a JSON body for non-GET requests: Content-Type:
    # application/json with an empty body makes Flask's get_json() raise
    # BadRequest(400). See #1787.
    if method == "GET":
        body = json.dumps(data).encode() if data else None
    else:
        body = json.dumps(data if data is not None else {}).encode()

    opener = build_opener(ProxyHandler({}))
    req = Request(url, data=body, headers=headers, method=method)

    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _get_gateway_client(self, **kwargs: Any) -> "GatewayClient":  # noqa: F821, UP037
    """Create a GatewayClient from the configured gateway URL.

    Extra kwargs are forwarded to the GatewayClient constructor
    (e.g. launcher_secret).
    """
    from urllib.parse import urlparse

    try:
        from orchestrator.gateway_client import GatewayClient
    except ImportError:
        from gateway_client import GatewayClient

    parsed = urlparse(self.gateway_url)
    host = parsed.hostname or "egg-gateway"
    port = parsed.port or GATEWAY_PORT
    return GatewayClient(gateway_host=host, gateway_port=port, **kwargs)


def _ensure_gateway_session(self) -> str:
    """Ensure we have a valid gateway session token, creating one if needed."""
    if self._gateway_session_token:
        return self._gateway_session_token

    launcher_secret = os.environ.get("EGG_LAUNCHER_SECRET")
    if not launcher_secret:
        raise RuntimeError("EGG_LAUNCHER_SECRET required for gateway session registration")

    client = self._get_gateway_client(launcher_secret=launcher_secret)
    session = client.register_session(
        container_id="mcp-server",
        container_ip=client.self_ip,
        mode="public",
        pipeline_id="mcp-server",
    )
    self._gateway_session_token = session.session_token
    return session.session_token


def _make_gateway_request(
    self,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Make HTTP request to the gateway with session auth.

    Automatically registers a session if needed and retries once on 401.
    """
    import json
    from urllib.error import HTTPError
    from urllib.request import ProxyHandler, Request, build_opener

    def _do_request(token: str) -> dict[str, Any]:
        url = f"{self.gateway_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        # Same GET/non-GET split as _make_request — see #1787.
        if method == "GET":
            body = json.dumps(data).encode() if data else None
        else:
            body = json.dumps(data if data is not None else {}).encode()
        opener = build_opener(ProxyHandler({}))
        req = Request(url, data=body, headers=headers, method=method)
        with opener.open(req, timeout=timeout) as response:
            return json.loads(response.read().decode())

    token = self._ensure_gateway_session()
    try:
        return _do_request(token)
    except HTTPError as e:
        if e.code == 401:
            # Session expired — clear cache and retry once
            self._gateway_session_token = None
            token = self._ensure_gateway_session()
            return _do_request(token)
        raise
