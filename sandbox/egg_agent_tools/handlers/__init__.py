"""Pure handler functions backing the egg-orch / egg-contract CLIs.

Each handler takes a plain dict request and returns a plain dict response
(``{"ok": bool, ...}``).  Gateway / orchestrator errors are raised as
``GatewayError`` — handlers MUST NEVER call ``sys.exit``.  The CLI shim
in ``sandbox/egg_lib/{contract_cli,orch_cli}.py`` catches
``GatewayError`` and renders the existing stderr / exit-code surface.

Historical note: pre-#2908 slice-6 these handlers also backed the
in-process Claude Agent SDK ``@tool`` wrappers under the deleted
``egg_agent_tools.tools/`` subpackage.  Slice-6 retired that surface;
the CLI is the only consumer now.  See ``docs/reference/agent-tools.md``.
"""

from __future__ import annotations

from egg_agent_tools.handlers.errors import (  # noqa: F401
    GatewayError,
    HandlerError,
)

__all__ = ["GatewayError", "HandlerError"]
