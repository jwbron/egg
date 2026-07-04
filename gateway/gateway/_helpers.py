"""Gateway helpers cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import UTC, datetime
from typing import Any

from flask import Response, jsonify, request

try:
    from .._module_loader import (
        load_sibling_gateway_module as _load_sibling_gateway_module,
    )
except ImportError:  # flat/container import mode
    from _module_loader import (  # type: ignore[no-redef, import-untyped]
        load_sibling_gateway_module as _load_sibling_gateway_module,
    )


def _b() -> Any:
    """Return the gateway barrel for call-time lookup of patched symbols.

    Seam getters/validators and gateway-local helpers are patched by tests at
    ``gateway.gateway.<name>``; resolving them on the barrel at call time keeps
    those patches effective after the split.
    """
    import sys

    return sys.modules.get("gateway.gateway") or sys.modules["gateway"]


class _BarrelLogger:
    """Proxy to the barrel ``logger`` so tests patching ``gateway.logger``
    observe log calls emitted from this submodule."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_b().logger, name)


logger: Any = _BarrelLogger()


def _lookup_commit_observer_fn(name: str) -> Any:
    """Return a callable from ``commit_observer`` without relative imports."""
    mod = _load_sibling_gateway_module("commit_observer")
    if mod is None:
        return None
    return getattr(mod, name, None)


def make_response(
    success: bool,
    message: str,
    data: dict[str, Any] | None = None,
    status_code: int = 200,
) -> tuple[Response, int]:
    """Create a standardized JSON response."""
    response = {"success": success, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), status_code


def make_error(
    message: str, status_code: int = 400, details: dict[str, Any] | None = None
) -> tuple[Response, int]:
    """Create an error response."""
    return make_response(False, message, details, status_code)


def make_success(message: str, data: dict[str, Any] | None = None) -> tuple[Response, int]:
    """Create a success response."""
    return make_response(True, message, data, 200)


def make_worktree_not_found_error(container_id: str) -> tuple[Response, int]:
    """Return a 500 error when a container's worktree cannot be found.

    This prevents the silent fallback to the main repo that caused #1497:
    agents could not see their own file changes because git ran against
    the main repo instead of the agent's worktree.
    """
    return make_error(
        f"Worktree not found for container '{container_id}'. "
        "The per-agent worktree may not have been created. "
        "Git operations require a valid worktree.",
        status_code=500,
    )


def audit_log(
    event_type: str,
    operation: str,
    success: bool,
    details: dict[str, Any] | None = None,
) -> None:
    """Log an audit event in structured format."""
    log_data: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "gateway_operation",
        "operation": operation,
        "source_ip": request.remote_addr,
        "success": success,
    }
    if details:
        log_data.update(details)

    if success:
        logger.info(f"Audit: {event_type}", **log_data)
    else:
        logger.warning(f"Audit: {event_type}", **log_data)


def _check_orchestrator_connectivity() -> dict[str, Any]:
    """Check orchestrator connectivity if configured.

    Returns:
        Dictionary with orchestrator status. Contains {"configured": False}
        if orchestrator URL is not set, otherwise includes reachability info.
    """
    orchestrator_url = os.environ.get("EGG_ORCHESTRATOR_URL")
    if not orchestrator_url:
        return {"configured": False}

    try:
        # Use a short timeout for health checks
        import urllib.request

        health_url = f"{orchestrator_url}/api/v1/health"
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode())
            return {
                "configured": True,
                "reachable": True,
                "url": orchestrator_url,
                "status": data.get("status", "unknown"),
            }
    except Exception as e:
        return {
            "configured": True,
            "reachable": False,
            "error": str(e),
        }


def _check_squid_health() -> dict[str, Any]:
    """Check if Squid proxy is running and listening on port 3129.

    Returns a dict with squid health info:
        running: bool - True if the squid process is alive
        listening: bool - True if port 3129 is accepting connections
    """
    result: dict[str, Any] = {"running": False, "listening": False}

    # Check if squid process is running (not zombie)
    try:
        proc = subprocess.run(
            ["pgrep", "-x", "squid"],
            capture_output=True,
            timeout=5,
        )
        result["running"] = proc.returncode == 0
    except subprocess.TimeoutExpired, FileNotFoundError:
        pass

    # Check if squid is actually accepting connections on port 3129.
    # We use a direct TCP connect instead of 'squid -k check' because the
    # latter re-parses squid.conf and fails when run as non-root (can't read
    # the SSL private key), even though Squid itself is running fine.
    try:
        with socket.create_connection(("127.0.0.1", 3129), timeout=2):
            result["listening"] = True
    except OSError:
        pass

    return result
