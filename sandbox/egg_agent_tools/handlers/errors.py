"""Exception types raised by egg_agent_tools handlers.

Handlers MUST NEVER call :func:`sys.exit`.  They raise :class:`HandlerError`
(or :class:`GatewayError` for gateway / orchestrator transport failures).
"""

from __future__ import annotations

from typing import Any


class HandlerError(Exception):
    """Base class for handler-level failures (bad input, invalid state, …).

    Carries a human-readable ``message`` and an optional ``details`` dict
    suitable for surfacing to the caller verbatim.
    """

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code


class GatewayError(HandlerError):
    """Raised when a gateway or orchestrator HTTP request fails.

    Mirrors the prior ``make_gateway_request``/``api_request_or_exit``
    stderr output so the CLI shim can render it byte-for-byte.  When
    ``status_code`` is set the failure came from the server; otherwise it
    is a connection / timeout error.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.status_code = status_code
        self.hint = hint
