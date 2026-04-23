"""Shared helpers for @tool wrappers (handler invocation + error wrapping).

Each wrapper is a two-liner: describe the tool and call
``invoke_handler(handler, args)``.  The helper is responsible for

1. Running the synchronous handler via :func:`asyncio.to_thread` so the
   agent's event loop is never blocked by gateway I/O.
2. Catching :class:`GatewayError` / :class:`HandlerError` / generic
   ``Exception`` and translating them to the SDK's structured
   ``{is_error: True, content: [...]}`` tool-result.
3. Serialising the handler's dict response as JSON under a single
   ``text`` content block, as documented by the SDK ``@tool`` contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from egg_agent_tools.handlers.errors import GatewayError, HandlerError

# Prefer the structured logger used by shared/egg_agent/client.py so
# tracebacks land in the checkpoint-browser's structured-event view
# alongside other agent events.  Fall back to stdlib logging when
# egg_logging is unavailable (host-side tooling, unit tests).
try:
    from egg_logging import get_logger  # type: ignore[import-not-found]

    _logger: Any = get_logger("egg_agent_tools.tool")
except ImportError:  # pragma: no cover - host-side fallback
    _logger = logging.getLogger("egg_agent_tools.tool")


def _format_error(exc: BaseException) -> str:
    """Render an exception as a short single-line string."""
    if isinstance(exc, GatewayError):
        parts = [str(exc.message or exc)]
        if exc.status_code:
            parts.append(f"(status {exc.status_code})")
        if exc.hint:
            parts.append(exc.hint)
        if exc.details:
            try:
                parts.append(f"details={json.dumps(exc.details)[:500]}")
            except Exception:
                pass
        return " ".join(parts)
    if isinstance(exc, HandlerError):
        return exc.message or str(exc)
    return f"{type(exc).__name__}: {exc}"


def _success_payload(response: dict[str, Any]) -> dict[str, Any]:
    try:
        text = json.dumps(response, default=str)
    except Exception:
        text = str(response)
    return {"content": [{"type": "text", "text": text}]}


def _error_payload(exc: BaseException) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _format_error(exc)}],
        "is_error": True,
    }


async def invoke_handler(
    handler: Callable[[dict[str, Any]], dict[str, Any]],
    args: dict[str, Any],
) -> dict[str, Any]:
    """Run ``handler(args)`` in a thread and wrap the result for the SDK.

    The handler is expected to be synchronous (most of ours are thin
    wrappers around urllib).  If the handler raises, the error is
    serialised into an ``is_error`` SDK tool-result rather than
    propagated — a gateway flake must never crash the agent loop.
    """
    try:
        response = await asyncio.to_thread(handler, args or {})
    except GatewayError as exc:
        return _error_payload(exc)
    except HandlerError as exc:
        return _error_payload(exc)
    except Exception as exc:  # pragma: no cover - defensive
        # Log full traceback so operators can diagnose unknown faults
        # from checkpoint logs; the structured tool-result only carries
        # the message.
        _logger.exception(
            "Unhandled handler exception in %s: %s",
            getattr(handler, "__name__", "<unknown>"),
            exc,
        )
        return _error_payload(exc)
    return _success_payload(response)
