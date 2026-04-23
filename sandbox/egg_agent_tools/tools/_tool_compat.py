"""claude_agent_sdk.tool shim for host-side environments.

The sandbox image ships claude-agent-sdk and uses the real
``@tool`` decorator; unit tests that run in host-side Python (no SDK
installed) still need to import the tools modules to collect the
registry.  We provide a single compat-stub so every tools/*.py module
imports ``tool`` the same way and mypy sees one type surface.

Using one shared module also means we have exactly one place to update
when the SDK's ``tool`` signature changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class _ToolDecorator(Protocol):
    """Callable shape of :func:`claude_agent_sdk.tool` + host-stub."""

    def __call__(  # noqa: D401
        self,
        name: str,
        description: str,
        input_schema: Any,
        annotations: Any = ...,
    ) -> Callable[[Callable[..., Any]], Any]: ...


try:  # pragma: no cover - resolved at import time
    from claude_agent_sdk import tool as _real_tool

    tool: _ToolDecorator = _real_tool
except ImportError:  # pragma: no cover - host-side only

    def _stub_tool(
        name: str,
        description: str,
        input_schema: Any,
        annotations: Any = None,
    ) -> Callable[[Callable[..., Any]], Any]:
        """Stand-in for :func:`claude_agent_sdk.tool` used in host tests.

        The stub preserves the same parameter shape as the SDK so mypy
        reports one consistent signature across all consumers.
        """

        def _decorator(handler: Callable[..., Any]) -> Any:
            class _StubTool:
                def __init__(self) -> None:
                    self.name = name
                    self.description = description
                    self.input_schema = input_schema
                    self.handler = handler
                    self.annotations = annotations

            return _StubTool()

        return _decorator

    tool = _stub_tool


__all__ = ["tool"]
