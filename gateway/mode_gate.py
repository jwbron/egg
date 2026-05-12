"""
Network-mode gating decorators for gateway routes.

Currently exposes ``require_private_mode`` — a decorator that refuses a
request unless the authenticated session's ``mode`` is ``"private"``.
Applied AFTER ``@require_session_auth`` so the session is guaranteed to be
present when this decorator fires.

The decorator also tags the wrapped view function with the attribute
``__egg_requires_private_mode__ = True`` so automated regression tests can
walk ``app.url_map`` and assert that every Jira route carries the marker.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from flask import Response, g, jsonify, request

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_logging import get_logger

logger = get_logger("gateway.mode-gate")


# Marker attribute stamped onto wrapped view functions.  Route-enumeration
# regression tests walk ``app.url_map`` for ``/api/v1/jira/*`` and assert this
# attribute is True on every matched view function — risk analysis R4.
PRIVATE_MODE_MARKER_ATTR = "__egg_requires_private_mode__"


def _make_private_mode_error(operation: str) -> tuple[Response, int]:
    """Return the canonical 403 response for a public-mode request."""
    return (
        jsonify(
            {
                "success": False,
                "message": "endpoint requires private network mode",
                "details": {
                    "required_mode": "private",
                    "endpoint": request.path,
                    "operation": operation,
                },
            }
        ),
        403,
    )


def require_private_mode[F: Callable[..., Any]](f: F) -> F:
    """Refuse the request unless ``g.session_mode == "private"``.

    Must be applied *after* ``@require_session_auth`` so that
    ``g.session_mode`` is populated (otherwise the check falls through to the
    public-mode deny branch, which is still fail-closed).

    Also records a structured audit-log line on refusal via
    ``gateway.gateway.audit_log``; the import is deferred to request time to
    avoid a circular import at module load.
    """

    @functools.wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        session_mode = getattr(g, "session_mode", None)
        # Issue #1557 reviewer_code v1 finding #1: routes that use
        # ``@require_session_or_launcher_auth`` may set
        # ``g.auth_actor='launcher'`` and leave ``session_mode=None``
        # — the orchestrator-internal call path. The launcher secret
        # is held only by the orchestrator (mounted at
        # ``/secrets/launcher-secret``), so a request that authenticated
        # with it is by definition not coming from a sandboxed agent
        # and the private-mode gate is not the correct guard. Accept
        # the launcher path unconditionally; the route's own
        # project-allowlist + idempotency guards remain in force.
        auth_actor = getattr(g, "auth_actor", None)
        if auth_actor == "launcher":
            return f(*args, **kwargs)
        if session_mode != "private":
            # Lazy import — gateway.py imports this module near the top, so a
            # module-level import would be circular.
            try:
                from .gateway import audit_log
            except ImportError:
                try:
                    from gateway import audit_log  # type: ignore[no-redef, attr-defined]
                except ImportError:
                    audit_log = None  # type: ignore[assignment]

            operation = f.__name__
            # ``audit_log`` dereferences ``request.remote_addr`` so we gate it
            # on ``has_request_context()`` for defensiveness even though this
            # decorator always runs inside a Flask request today.
            from flask import has_request_context

            if audit_log is not None and has_request_context():
                try:
                    audit_log(
                        "private_mode_required",
                        operation,
                        success=False,
                        details={
                            "endpoint": request.path,
                            "session_mode": session_mode,
                        },
                    )
                except Exception:  # pragma: no cover – defensive
                    # Audit must never break the deny path.
                    logger.exception("audit_log failed in require_private_mode")
            else:  # pragma: no cover — gateway module unavailable / no request
                logger.warning(
                    "private_mode_required",
                    endpoint=getattr(request, "path", None),
                    session_mode=session_mode,
                )

            return _make_private_mode_error(operation)

        return f(*args, **kwargs)

    # Stamp the marker onto the wrapper so regression tests can verify that
    # every /api/v1/jira/* view function enforces private mode (R4).
    setattr(decorated, PRIVATE_MODE_MARKER_ATTR, True)
    return cast(F, decorated)
