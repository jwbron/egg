"""Tests for egg_harness.tools.registry — ToolRegistry contract."""

from __future__ import annotations

from typing import Any

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.tools.registry")

from egg_harness.tools.registry import ToolDefinition, ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _echo_handler(tool_input: dict[str, Any]) -> ToolResult:
    """Simple handler that echoes back the 'text' field."""
    return ToolResult(output=tool_input.get("text", ""))


async def _failing_handler(tool_input: dict[str, Any]) -> ToolResult:
    """Handler that always raises."""
    raise RuntimeError("boom")


async def _large_output_handler(tool_input: dict[str, Any]) -> ToolResult:
    """Handler that returns output larger than a given size."""
    size = tool_input.get("size", 200_000)
    return ToolResult(output="x" * size)


# ---------------------------------------------------------------------------
# TestToolRegistryCreation
# ---------------------------------------------------------------------------


class TestToolRegistryCreation:
    """ToolRegistry constructor and basic properties."""

    def test_default_constructor(self):
        registry = ToolRegistry()
        assert registry is not None

    def test_custom_max_output_size(self):
        registry = ToolRegistry(max_output_size=50)
        assert registry is not None


# ---------------------------------------------------------------------------
# TestToolRegistry — core registration and execution
# ---------------------------------------------------------------------------


class TestToolRegistry:
    """Core registration and execution behaviour."""

    @pytest.mark.anyio
    async def test_register_and_execute_tool(self):
        """Registering a tool and executing it by name returns its output."""
        registry = ToolRegistry()
        defn = ToolDefinition(name="echo", description="Echo tool", input_schema={})
        registry.register(defn, _echo_handler)

        result = await registry.execute("echo", {"text": "hello"})

        assert not result.is_error
        assert result.output == "hello"

    def test_get_definitions_returns_all_tools(self):
        """After registering N tools, get_definitions returns exactly N items."""
        registry = ToolRegistry()
        for i in range(5):
            defn = ToolDefinition(name=f"tool_{i}", description=f"Tool {i}", input_schema={})
            registry.register(defn, _echo_handler)

        definitions = registry.get_definitions()

        assert len(definitions) == 5

    @pytest.mark.anyio
    async def test_execute_unknown_tool_returns_error(self):
        """Executing a tool name that was never registered returns an error result."""
        registry = ToolRegistry()

        result = await registry.execute("nonexistent", {})

        assert result.is_error

    @pytest.mark.anyio
    async def test_register_multiple_tools(self):
        """Multiple tools with different names can coexist."""
        registry = ToolRegistry()

        registry.register(
            ToolDefinition(name="alpha", description="A", input_schema={}), _echo_handler
        )
        registry.register(
            ToolDefinition(name="beta", description="B", input_schema={}), _echo_handler
        )
        registry.register(
            ToolDefinition(name="gamma", description="C", input_schema={}), _echo_handler
        )

        assert (await registry.execute("alpha", {"text": "a"})).output == "a"
        assert (await registry.execute("beta", {"text": "b"})).output == "b"
        assert (await registry.execute("gamma", {"text": "c"})).output == "c"

    @pytest.mark.anyio
    async def test_tool_handler_exception_returns_error(self):
        """When a handler raises an exception the result is an error, not a crash."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(name="bad", description="Bad tool", input_schema={}), _failing_handler
        )

        result = await registry.execute("bad", {})

        assert result.is_error
        assert "boom" in result.output or "RuntimeError" in result.output


# ---------------------------------------------------------------------------
# TestToolRegistryPermissions — set_permission_callback
# ---------------------------------------------------------------------------


class TestToolRegistryPermissions:
    """Permission callback gating."""

    @pytest.mark.anyio
    async def test_permission_callback_blocks_tool(self):
        """When the permission callback returns an error string the tool is NOT executed."""
        registry = ToolRegistry()
        registry.set_permission_callback(
            lambda name, inp: "forbidden: you shall not pass",
        )
        registry.register(
            ToolDefinition(name="guarded", description="Guarded", input_schema={}), _echo_handler
        )

        result = await registry.execute("guarded", {"text": "hi"})

        assert result.is_error
        assert "forbidden" in result.output.lower() or "shall not pass" in result.output

    @pytest.mark.anyio
    async def test_permission_callback_allows_tool(self):
        """When the permission callback returns None the tool executes normally."""
        registry = ToolRegistry()
        registry.set_permission_callback(lambda name, inp: None)
        registry.register(
            ToolDefinition(name="open", description="Open", input_schema={}), _echo_handler
        )

        result = await registry.execute("open", {"text": "allowed"})

        assert not result.is_error
        assert result.output == "allowed"

    @pytest.mark.anyio
    async def test_no_permission_callback_allows_all(self):
        """When no permission callback is set, all tools execute freely."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(name="free", description="Free", input_schema={}), _echo_handler
        )

        result = await registry.execute("free", {"text": "ok"})

        assert not result.is_error
        assert result.output == "ok"


# ---------------------------------------------------------------------------
# TestToolRegistryTruncation — output size limits
# ---------------------------------------------------------------------------


class TestToolRegistryTruncation:
    """Output truncation behaviour."""

    @pytest.mark.anyio
    async def test_output_truncation(self):
        """Output exceeding the default limit is truncated with a message."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(name="big", description="Big output", input_schema={}),
            _large_output_handler,
        )

        result = await registry.execute("big", {"size": 200_000})

        assert len(result.output) <= 100 * 1024 + 500  # allow for truncation message
        assert "truncat" in result.output.lower()

    @pytest.mark.anyio
    async def test_output_within_limit_not_truncated(self):
        """Output that fits within the limit is returned verbatim."""
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(name="small", description="Small", input_schema={}), _echo_handler
        )

        result = await registry.execute("small", {"text": "short"})

        assert result.output == "short"

    @pytest.mark.anyio
    async def test_custom_truncation_limit(self):
        """A custom truncation limit is honoured."""
        custom_limit = 50
        registry = ToolRegistry(max_output_size=custom_limit)
        registry.register(
            ToolDefinition(name="med", description="Med", input_schema={}),
            _large_output_handler,
        )

        result = await registry.execute("med", {"size": 200})

        assert len(result.output) <= custom_limit + 500
        assert "truncat" in result.output.lower()

    @pytest.mark.anyio
    async def test_exact_limit_output_not_truncated(self):
        """Output exactly at the limit boundary should not be truncated."""
        limit = 100
        registry = ToolRegistry(max_output_size=limit)

        async def exact_handler(inp: dict[str, Any]) -> ToolResult:
            return ToolResult(output="a" * limit)

        registry.register(
            ToolDefinition(name="exact", description="Exact", input_schema={}), exact_handler
        )

        result = await registry.execute("exact", {})

        assert result.output == "a" * limit
