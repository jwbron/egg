"""Tests for egg_harness.client — high-level agent API."""

from __future__ import annotations

import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from egg_harness.client import DEFAULT_MODEL, _create_standard_tools, run_agent_async
from egg_harness.result import AgentResult
from egg_harness.tools import ToolDefinition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_result() -> AgentResult:
    return AgentResult(
        success=True, stdout="done", stderr="", returncode=0,
        cost_usd=0.01, num_turns=1, duration_ms=100,
    )


def _patch_providers_and_loop():
    """Context manager that patches the lazy-imported providers and AgentLoop.

    client.py lazy-imports AnthropicProvider and RetryProvider inside
    run_agent_async(). The providers package uses a custom __getattr__,
    so we pre-import the submodules then patch the classes on them.
    """
    # Force-import the submodules so they exist in sys.modules
    import egg_harness.providers.anthropic as ap_mod
    import egg_harness.providers.retry as rp_mod

    mock_ap = MagicMock()
    mock_rp = MagicMock()
    mock_loop_cls = MagicMock()
    mock_loop = MagicMock()
    mock_loop.run = AsyncMock(return_value=_mock_result())
    mock_loop_cls.return_value = mock_loop

    ctx = (
        patch.object(ap_mod, "AnthropicProvider", mock_ap),
        patch.object(rp_mod, "RetryProvider", mock_rp),
        patch("egg_harness.client.AgentLoop", mock_loop_cls),
    )

    class Ctx:
        def __init__(self):
            self.mock_ap = mock_ap
            self.mock_rp = mock_rp
            self.mock_loop_cls = mock_loop_cls
            self.mock_loop = mock_loop
            self._patches = ctx

        def __enter__(self):
            for p in self._patches:
                p.__enter__()
            return self

        def __exit__(self, *args):
            for p in reversed(self._patches):
                p.__exit__(*args)

    return Ctx()


# ---------------------------------------------------------------------------
# TestCreateStandardTools
# ---------------------------------------------------------------------------


class TestCreateStandardTools:

    def test_returns_eight_tool_pairs(self):
        tools = _create_standard_tools()
        assert len(tools) == 8

    def test_all_tool_names_present(self):
        tools = _create_standard_tools()
        names = {defn.name for defn, _handler in tools}
        expected = {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"}
        assert names == expected

    def test_each_pair_has_definition_and_handler(self):
        tools = _create_standard_tools()
        for defn, handler in tools:
            assert isinstance(defn, ToolDefinition)
            assert defn.name
            assert defn.input_schema
            assert callable(handler)

    def test_cwd_parameter_accepted(self):
        tools = _create_standard_tools(cwd="/tmp/workspace")
        assert len(tools) == 8


# ---------------------------------------------------------------------------
# TestRunAgentAsync
# ---------------------------------------------------------------------------


class TestRunAgentAsync:

    @pytest.mark.anyio
    async def test_default_model_is_opus(self):
        with _patch_providers_and_loop() as ctx:
            result = await run_agent_async("test prompt")
            assert result.success is True
            provider_config = ctx.mock_ap.call_args[0][0]
            assert "opus" in provider_config.model

    @pytest.mark.anyio
    async def test_custom_model_spec(self):
        with _patch_providers_and_loop() as ctx:
            await run_agent_async("test", model="sonnet[200k]")
            provider_config = ctx.mock_ap.call_args[0][0]
            assert "sonnet" in provider_config.model

    @pytest.mark.anyio
    async def test_provider_stack_has_retry_layer(self):
        with _patch_providers_and_loop() as ctx:
            await run_agent_async("test")
            ctx.mock_rp.assert_called_once_with(ctx.mock_ap.return_value)

    @pytest.mark.anyio
    async def test_private_mode_blocks_web_tools(self, monkeypatch):
        monkeypatch.setenv("EGG_PRIVATE_MODE", "true")
        with _patch_providers_and_loop() as ctx:
            await run_agent_async("test")
            loop_kwargs = ctx.mock_loop_cls.call_args[1]
            registry = loop_kwargs["tool_registry"]
            assert registry._permission_callback is not None

    @pytest.mark.anyio
    async def test_public_mode_no_permission_callback(self, monkeypatch):
        monkeypatch.delenv("EGG_PRIVATE_MODE", raising=False)
        with _patch_providers_and_loop() as ctx:
            await run_agent_async("test")
            loop_kwargs = ctx.mock_loop_cls.call_args[1]
            registry = loop_kwargs["tool_registry"]
            assert registry._permission_callback is None

    @pytest.mark.anyio
    async def test_on_output_callback_wired(self):
        output_chunks: list[str] = []
        with _patch_providers_and_loop() as ctx:
            await run_agent_async("test", on_output=output_chunks.append)
            loop_kwargs = ctx.mock_loop_cls.call_args[1]
            assert loop_kwargs["event_bus"] is not None

    @pytest.mark.anyio
    async def test_env_vars_for_endpoint(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gateway:8080")
        monkeypatch.delenv("GATEWAY_URL", raising=False)
        with _patch_providers_and_loop() as ctx:
            await run_agent_async("test")
            provider_config = ctx.mock_ap.call_args[0][0]
            assert provider_config.endpoint == "http://gateway:8080"

    @pytest.mark.anyio
    async def test_cwd_passed_to_config(self):
        with _patch_providers_and_loop() as ctx:
            await run_agent_async("test", cwd="/tmp/workspace")
            loop_kwargs = ctx.mock_loop_cls.call_args[1]
            config = loop_kwargs["config"]
            assert config.cwd == "/tmp/workspace"

    @pytest.mark.anyio
    async def test_max_turns_passed_to_config(self):
        with _patch_providers_and_loop() as ctx:
            await run_agent_async("test", max_turns=50)
            loop_kwargs = ctx.mock_loop_cls.call_args[1]
            config = loop_kwargs["config"]
            assert config.max_turns == 50

    @pytest.mark.anyio
    async def test_default_max_turns_is_200(self):
        with _patch_providers_and_loop() as ctx:
            await run_agent_async("test")
            loop_kwargs = ctx.mock_loop_cls.call_args[1]
            config = loop_kwargs["config"]
            assert config.max_turns == 200


# ---------------------------------------------------------------------------
# TestRunAgentSync
# ---------------------------------------------------------------------------


class TestRunAgentSync:

    def test_sync_wrapper_exists(self):
        from egg_harness.client import run_agent
        assert callable(run_agent)

    def test_default_model_constant(self):
        assert DEFAULT_MODEL == "opus[1m]"
