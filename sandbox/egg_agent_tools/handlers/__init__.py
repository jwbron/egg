"""Pure handler functions shared by the shell CLI and MCP @tool wrappers.

Each handler takes a plain dict request and returns a plain dict response
(``{"ok": bool, ...}``).  Gateway / orchestrator errors are raised as
``GatewayError`` — handlers MUST NEVER call ``sys.exit``.  The CLI shim
in ``sandbox/egg_lib/{contract_cli,orch_cli}.py`` catches
``GatewayError`` and renders the existing stderr / exit-code surface;
the ``@tool`` wrappers in ``egg_agent_tools.tools`` catch it and return
an SDK-structured ``is_error: True`` tool result.
"""

from __future__ import annotations

from egg_agent_tools.handlers.errors import (  # noqa: F401
    GatewayError,
    HandlerError,
)

__all__ = ["GatewayError", "HandlerError"]
