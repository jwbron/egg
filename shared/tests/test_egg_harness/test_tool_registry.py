"""Tests for egg_harness.tools.registry — ToolRegistry contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from egg_harness.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeToolDefinition:
    """Minimal stand-in for ToolDefinition so tests are self-contained.

    The real ToolDefinition may be richer; we only need *name* and
    *input_schema* for registry bookkeeping.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None


def _echo_handler(tool_input: dict[str, Any]) -> str:
    """Simple handler that echoes back the 'text' field."""
    return tool_input.get("text", "")


def _failing_handler(tool_input: dict[str, Any]) -> str:
    """Handler that always raises."""
    raise RuntimeError("boom")


def _large_output_handler(tool_input: dict[str, Any]) -> str:
    """Handler that returns output larger than a given size."""
    size = tool_input.get("size", 200_000)
    return "x" * size


# ---------------------------------------------------------------------------
# TestToolRegistry — core registration and execution
# ---------------------------------------------------------------------------


class TestToolRegistry:
    """Core registration and execution behaviour."""

    def test_register_and_execute_tool(self):
        """Registering a tool and executing it by name returns its output."""
        registry = ToolRegistry()
        defn = _FakeToolDefinition(name="echo")
        registry.register(defn, _echo_handler)

        result = registry.execute("echo", {"text": "hello"})

        assert not result.is_error
        assert result.output == "hello"

    def test_get_definitions_returns_all_tools(self):
        """After registering N tools, get_definitions returns exactly N items."""
        registry = ToolRegistry()
        for i in range(5):
            defn = _FakeToolDefinition(name=f"tool_{i}")
            registry.register(defn, _echo_handler)

        definitions = registry.get_definitions()

        assert len(definitions) == 5
        names = {d.name for d in definitions}
        assert names == {f"tool_{i}" for i in range(5)}

    def test_execute_unknown_tool_returns_error(self):
        """Executing a tool name that was never registered returns an error result."""
        registry = ToolRegistry()

        result = registry.execute("nonexistent", {})

        assert result.is_error
        assert "nonexistent" in result.output.lower() or "unknown" in result.output.lower()

    def test_register_multiple_tools(self):
        """Multiple tools with different names can coexist."""
        registry = ToolRegistry()

        registry.register(_FakeToolDefinition(name="alpha"), _echo_handler)
        registry.register(_FakeToolDefinition(name="beta"), _echo_handler)
        registry.register(_FakeToolDefinition(name="gamma"), _echo_handler)

        assert registry.execute("alpha", {"text": "a"}).output == "a"
        assert registry.execute("beta", {"text": "b"}).output == "b"
        assert registry.execute("gamma", {"text": "c"}).output == "c"

    def test_tool_handler_exception_returns_error(self):
        """When a handler raises an exception the result is an error, not a crash."""
        registry = ToolRegistry()
        registry.register(_FakeToolDefinition(name="bad"), _failing_handler)

        result = registry.execute("bad", {})

        assert result.is_error
        assert "boom" in result.output or "RuntimeError" in result.output


# ---------------------------------------------------------------------------
# TestToolRegistryPermissions — can_use_tool callback
# ---------------------------------------------------------------------------


class TestToolRegistryPermissions:
    """Permission callback (can_use_tool) gating."""

    def test_permission_callback_blocks_tool(self):
        """When the permission callback returns an error string the tool is NOT executed."""
        handler = MagicMock(return_value="should not run")
        registry = ToolRegistry(
            can_use_tool=lambda name, inp: "forbidden: you shall not pass",
        )
        registry.register(_FakeToolDefinition(name="guarded"), handler)

        result = registry.execute("guarded", {"text": "hi"})

        assert result.is_error
        assert "forbidden" in result.output.lower() or "shall not pass" in result.output
        handler.assert_not_called()

    def test_permission_callback_allows_tool(self):
        """When the permission callback returns None the tool executes normally."""
        registry = ToolRegistry(can_use_tool=lambda name, inp: None)
        registry.register(_FakeToolDefinition(name="open"), _echo_handler)

        result = registry.execute("open", {"text": "allowed"})

        assert not result.is_error
        assert result.output == "allowed"

    def test_no_permission_callback_allows_all(self):
        """When no permission callback is set, all tools execute freely."""
        registry = ToolRegistry()
        registry.register(_FakeToolDefinition(name="free"), _echo_handler)

        result = registry.execute("free", {"text": "ok"})

        assert not result.is_error
        assert result.output == "ok"


# ---------------------------------------------------------------------------
# TestToolRegistryTruncation — output size limits
# ---------------------------------------------------------------------------


class TestToolRegistryTruncation:
    """Output truncation behaviour."""

    def test_output_truncation(self):
        """Output exceeding the default limit (100 KB) is truncated with a message."""
        registry = ToolRegistry()
        registry.register(_FakeToolDefinition(name="big"), _large_output_handler)

        result = registry.execute("big", {"size": 200_000})

        assert len(result.output) <= 100 * 1024 + 500  # allow for truncation message
        assert "truncat" in result.output.lower()

    def test_output_within_limit_not_truncated(self):
        """Output that fits within the limit is returned verbatim."""
        registry = ToolRegistry()
        registry.register(_FakeToolDefinition(name="small"), _echo_handler)

        result = registry.execute("small", {"text": "short"})

        assert result.output == "short"

    def test_custom_truncation_limit(self):
        """A custom truncation limit is honoured."""
        custom_limit = 50
        registry = ToolRegistry(max_output_bytes=custom_limit)
        registry.register(_FakeToolDefinition(name="med"), _large_output_handler)

        result = registry.execute("med", {"size": 200})

        # The output should be capped near the custom limit (plus a truncation notice).
        assert len(result.output) <= custom_limit + 500
        assert "truncat" in result.output.lower()

    def test_exact_limit_output_not_truncated(self):
        """Output exactly at the limit boundary should not be truncated."""
        limit = 100
        registry = ToolRegistry(max_output_bytes=limit)

        def exact_handler(inp: dict[str, Any]) -> str:
            return "a" * limit

        registry.register(_FakeToolDefinition(name="exact"), exact_handler)

        result = registry.execute("exact", {})

        assert result.output == "a" * limit
