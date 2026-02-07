"""
Authentication decorators and utilities for gateway endpoints.

This module is separate from gateway.py to avoid circular imports when
contract_api.py needs the require_session_auth decorator.
"""

import functools
import logging
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from flask import Response, g, jsonify, request

F = TypeVar("F", bound=Callable[..., Any])

# Set up logging - use egg_logging if available, otherwise standard logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger

    _logger = get_logger("gateway.auth")
except ImportError:
    _logger = logging.getLogger("gateway.auth")  # type: ignore[assignment]

# Import session validation lazily to avoid circular imports at module load time
# These are imported as modules so that tests can patch session_manager.validate_session_for_request
_session_manager: types.ModuleType | None = None
_rate_limiter: types.ModuleType | None = None


def _get_session_manager() -> types.ModuleType:
    """Lazy import of session_manager module."""
    global _session_manager
    if _session_manager is None:
        try:
            from . import session_manager as sm

            _session_manager = sm
        except ImportError:
            import session_manager as sm  # type: ignore[no-redef, import-not-found]

            _session_manager = sm
    return _session_manager


def _get_rate_limiter() -> types.ModuleType:
    """Lazy import of rate_limiter module."""
    global _rate_limiter
    if _rate_limiter is None:
        try:
            from . import rate_limiter as rl

            _rate_limiter = rl
        except ImportError:
            import rate_limiter as rl  # type: ignore[no-redef, import-not-found]

            _rate_limiter = rl
    return _rate_limiter


def make_auth_error(message: str, status_code: int = 401) -> tuple[Response, int]:
    """Create an authentication error response."""
    return jsonify({"success": False, "message": message}), status_code


def require_session_auth(f: F) -> F:
    """
    Decorator that validates session tokens in request handlers.

    - Extracts session token from Authorization header
    - Validates token via session_manager
    - Stores validated session and mode in Flask's g object for handler use
    - Returns 401 on validation failure

    All containers must have a valid session. There is no legacy fallback.
    """

    @functools.wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            _logger.warning(
                "Session auth failed - missing Authorization header",
                endpoint=request.path,
                source_ip=request.remote_addr,
            )
            return make_auth_error("Missing or invalid Authorization header", status_code=401)

        token = auth_header[7:]  # Remove "Bearer " prefix
        source_ip = request.remote_addr

        # Validate session via session_manager (call via module to allow patching in tests)
        session_manager = _get_session_manager()
        result = session_manager.validate_session_for_request(token, source_ip)
        if not result.valid:
            # Record failed lookup for rate limiting
            rate_limiter = _get_rate_limiter()
            rate_limiter.record_failed_lookup(source_ip or "")
            _logger.warning(
                "Session auth failed - invalid token",
                endpoint=request.path,
                source_ip=source_ip,
                error=result.error,
            )
            return make_auth_error(
                result.error or "Invalid or expired session token", status_code=401
            )

        # Set session context from validation result
        g.session = result.session
        g.session_mode = result.session.mode if result.session else None

        return f(*args, **kwargs)

    return decorated  # type: ignore[return-value]
