"""Shared exception types for the contract CLI sub-package.

Re-exports :class:`GatewayError` / :class:`HandlerError` from
``egg_agent_tools.handlers.errors`` when available, with local fallback
definitions for partial bootstraps. Extracted verbatim from the monolithic
``contract_cli.py`` during the #3312 (slice-1) decomposition; behaviour is
unchanged.
"""

from typing import Any

# Shared exception types so handlers (and this CLI) can raise instead of
# calling sys.exit.  See sandbox/egg_agent_tools/handlers/errors.py.
try:
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError
except ImportError:  # pragma: no cover - only during partial bootstraps

    class HandlerError(Exception):  # type: ignore[no-redef]
        def __init__(self, message: str, *, details: Any = None, exit_code: int = 1) -> None:
            super().__init__(message)
            self.message = message
            self.details = details or {}
            self.exit_code = exit_code

    class GatewayError(HandlerError):  # type: ignore[no-redef]
        def __init__(
            self,
            message: str,
            *,
            status_code: int | None = None,
            details: Any = None,
            hint: str | None = None,
        ) -> None:
            super().__init__(message, details=details)
            self.status_code = status_code
            self.hint = hint


__all__ = ["GatewayError", "HandlerError"]
