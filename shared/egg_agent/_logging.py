"""Shared logger resolution for ``egg_agent`` modules.

Every module here logs through egg's structured logger (``event_type=``,
``event_subtype=``, ``error=`` kwargs). Outside the sandbox ``egg_logging``
may be absent, so the fallback must still accept those kwargs — a bare
``logging.Logger`` raises ``TypeError`` on the first unknown keyword, which
would defeat the "never raise" contract of modules like
:mod:`egg_agent.session`. ``_StdlibLoggerAdapter`` drops the structured
kwargs the stdlib doesn't understand; :func:`resolve_logger` wires it up.
"""

from __future__ import annotations

import logging
from typing import Any


class _StdlibLoggerAdapter:
    """Thin adapter so a stdlib logger ignores structured-log kwargs."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, msg: str, **kwargs: Any) -> None:
        # Drop structured kwargs that stdlib doesn't understand
        self._logger.log(level, msg)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, **kwargs)


def resolve_logger(structured_name: str, fallback_name: str) -> Any:
    """Return egg's structured logger, or a kwarg-dropping stdlib fallback.

    ``structured_name`` is the logical name passed to ``egg_logging.get_logger``
    inside the sandbox; ``fallback_name`` names the stdlib logger used when
    ``egg_logging`` is unavailable. The fallback is wrapped in
    :class:`_StdlibLoggerAdapter` so structured-log kwargs never raise.
    """
    try:
        from egg_logging import get_logger

        return get_logger(structured_name)
    except ImportError:  # pragma: no cover - stdlib fallback outside the sandbox
        return _StdlibLoggerAdapter(fallback_name)
