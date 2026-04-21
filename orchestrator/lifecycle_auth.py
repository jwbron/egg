"""Bearer-token auth for orchestrator lifecycle-control endpoints.

The orchestrator exposes two classes of HTTP endpoints on port 9849:

- **Agent-facing**: heartbeats, signals, progress, messages, anchors, and
  reads. Agents legitimately call these; they are left unauthenticated so
  the existing in-cluster NetworkPolicy (allow-agent-to-orchestrator) is
  sufficient.
- **Lifecycle-control**: HITL decision resolve/cancel, pipeline
  create/update/delete/start, manual phase overrides, container spawn/stop,
  agent and phase restarts. These change pipeline state in ways an agent
  should never trigger — the HITL auto-approval incident (#1769) showed
  that leaving them open lets any in-cluster caller bypass human gates.

This module provides the single decorator ``require_lifecycle_secret``
that guards those endpoints with a shared bearer token read from
``EGG_LIFECYCLE_SECRET``. MCP (in-process) and the host-side CLIs
(egg-sdlc, egg-orch) carry the same secret and set
``Authorization: Bearer <secret>``. Agent pods never receive the env var.
"""

import functools
import os
import secrets as _secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from flask import jsonify, request

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs: Any):  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.auth")

LIFECYCLE_SECRET_ENV = "EGG_LIFECYCLE_SECRET"
SOURCE_HEADER = "X-Egg-Source"


class LifecycleSecretNotConfiguredError(RuntimeError):
    """Raised when EGG_LIFECYCLE_SECRET is missing at startup."""


def _configured_secret() -> str:
    """Read the configured secret each call so tests can patch env."""
    return os.environ.get(LIFECYCLE_SECRET_ENV, "")


def _caller_source() -> str:
    """Advisory source tag for audit logs (mcp, local-cli, etc.)."""
    raw = request.headers.get(SOURCE_HEADER, "").strip()
    return raw or "unknown"


def require_lifecycle_secret[F: Callable[..., Any]](f: F) -> F:
    """Require a valid ``Authorization: Bearer <EGG_LIFECYCLE_SECRET>``.

    Responds 503 if the orchestrator has no secret configured (fail-closed;
    the deployment is misconfigured). Responds 401 on a missing or wrong
    header. On success, attaches ``request.egg_source`` to the request so
    handlers can include it in audit logs.
    """

    @functools.wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        expected = _configured_secret()
        if not expected:
            logger.error(
                "Lifecycle endpoint rejected: EGG_LIFECYCLE_SECRET not set",
                endpoint=request.path,
            )
            return (
                jsonify(
                    {
                        "success": False,
                        "message": ("Server misconfigured: EGG_LIFECYCLE_SECRET is not set"),
                    }
                ),
                503,
            )

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(
                "Lifecycle endpoint rejected: missing bearer token",
                endpoint=request.path,
                source_ip=request.remote_addr,
            )
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Missing or invalid Authorization header",
                    }
                ),
                401,
            )

        provided = auth_header[len("Bearer ") :]
        if not _secrets.compare_digest(provided, expected):
            logger.warning(
                "Lifecycle endpoint rejected: invalid bearer token",
                endpoint=request.path,
                source_ip=request.remote_addr,
                claimed_source=_caller_source(),
            )
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Invalid lifecycle authorization token",
                    }
                ),
                401,
            )

        # Stash the source on the request for handlers that want to log it.
        request.egg_source = _caller_source()  # type: ignore[attr-defined]
        return f(*args, **kwargs)

    return cast(F, decorated)
