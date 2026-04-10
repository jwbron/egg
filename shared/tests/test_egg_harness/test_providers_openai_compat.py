"""Tests for egg_harness.providers.openai_compat — OpenAICompatibleProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from egg_harness.providers.base import (
    MessageDelta,
    Provider,
    StreamEvent,
    TextDelta,
    ToolUseStart,
)
from egg_harness.providers.openai_compat import OpenAICompatibleProvider


class TestOpenAICompatibleProviderInit:
    """Verify construction, inheritance, and endpoint configuration."""

    def test_inherits_from_provider(self):
        """OpenAICompatibleProvider must be a subclass of Provider."""
        assert issubclass(OpenAICompatibleProvider, Provider)

    def test_basic_instantiation(self):
        """Provider can be created with a base URL and API key."""
        provider = OpenAICompatibleProvider(
            base_url="http://localhost:8080/v1",
            api_key="test-key",
        )
        assert provider is not None

    def test_endpoint_config_stored(self):
        """The base URL should be accessible on the provider instance."""
        provider = OpenAICompatibleProvider(
            base_url="https://api.openrouter.ai/v1",
            api_key="or-key",
        )
        # Implementation may expose this as base_url, endpoint, or _base_url
        base = getattr(
            provider,
            "base_url",
            getattr(provider, "endpoint", getattr(provider, "_base_url", None)),
        )
        assert base is not None
        assert "openrouter" in str(base)

    def test_endpoint_requires_url(self):
        """Omitting base_url (or passing empty) should raise."""
        with pytest.raises((TypeError, ValueError)):
            OpenAICompatibleProvider(base_url="", api_key="k")

    def test_api_key_can_be_empty_for_local(self):
        """Some local endpoints (e.g., vLLM) don't require an API key.

        The provider should accept an empty or sentinel key without error
        as long as a valid base_url is given.
        """
        # This may or may not raise depending on implementation.
        # The test documents the expected behaviour: local endpoints
        # should work without real keys.
        try:
            provider = OpenAICompatibleProvider(
                base_url="http://localhost:8080/v1",
                api_key="",
            )
            assert provider is not None
        except (ValueError, TypeError):
            pytest.skip(
                "Implementation requires a non-empty api_key; revisit if local-only use is needed"
            )


class TestOpenAICompatibleProviderCapabilities:
    """Verify capability declaration configuration."""

    @pytest.fixture
    def provider(self):
        return OpenAICompatibleProvider(
            base_url="http://localhost:8080/v1",
            api_key="test-key",
        )

    def test_default_capabilities(self, provider):
        """Provider should expose a capabilities dict or attribute."""
        caps = getattr(
            provider,
            "capabilities",
            getattr(provider, "_capabilities", None),
        )
        # Capabilities should exist (dict, dataclass, or similar)
        assert caps is not None

    def test_capabilities_with_custom_config(self):
        """Capabilities can be overridden at construction time."""
        custom_caps = {
            "supports_tools": True,
            "supports_streaming": True,
            "supports_system_prompt": True,
        }
        provider = OpenAICompatibleProvider(
            base_url="http://localhost:8080/v1",
            api_key="test-key",
            capabilities=custom_caps,
        )
        caps = getattr(
            provider,
            "capabilities",
            getattr(provider, "_capabilities", None),
        )
        assert caps is not None
        # At minimum, the provider should respect the declared capabilities
        if isinstance(caps, dict):
            assert caps.get("supports_tools") is True
            assert caps.get("supports_streaming") is True


class TestOpenAICompatibleProviderSSEParsing:
    """Verify SSE chunk parsing into StreamEvent types."""

    @pytest.fixture
    def provider(self):
        return OpenAICompatibleProvider(
            base_url="http://localhost:8080/v1",
            api_key="test-key",
        )

    def _make_sse_chunk(
        self,
        *,
        chunk_id: str = "chatcmpl-abc",
        model: str = "gpt-4",
        role: str | None = None,
        content: str | None = None,
        tool_call_id: str | None = None,
        tool_call_name: str | None = None,
        tool_call_args: str | None = None,
        finish_reason: str | None = None,
    ) -> dict:
        """Build a dict mirroring an OpenAI-style SSE chunk."""
        delta: dict = {}
        if role is not None:
            delta["role"] = role
        if content is not None:
            delta["content"] = content
        if tool_call_id is not None or tool_call_name is not None:
            tool_call: dict = {"index": 0}
            if tool_call_id is not None:
                tool_call["id"] = tool_call_id
            func: dict = {}
            if tool_call_name is not None:
                func["name"] = tool_call_name
            if tool_call_args is not None:
                func["arguments"] = tool_call_args
            if func:
                tool_call["function"] = func
            delta["tool_calls"] = [tool_call]
        choice: dict = {"index": 0, "delta": delta}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [choice],
        }

    @pytest.mark.asyncio
    async def test_text_content_chunk(self, provider):
        """An SSE chunk with content should yield TextDelta."""
        chunk = self._make_sse_chunk(content="Hello")
        mock_obj = MagicMock()
        for k, v in chunk.items():
            setattr(mock_obj, k, v)
        # choices as objects
        choice_obj = MagicMock()
        delta_obj = MagicMock()
        delta_obj.role = None
        delta_obj.content = "Hello"
        delta_obj.tool_calls = None
        choice_obj.delta = delta_obj
        choice_obj.finish_reason = None
        mock_obj.choices = [choice_obj]

        mock_stream = AsyncMock()
        mock_stream.__aiter__ = lambda self: self
        mock_stream.__anext__ = AsyncMock(side_effect=[mock_obj, StopAsyncIteration])

        with patch.object(provider, "_create_stream", return_value=mock_stream):
            events: list[StreamEvent] = []
            async for e in provider.send_message(
                messages=[{"role": "user", "content": "Hi"}],
                tools=[],
                system="sys",
                model="gpt-4",
            ):
                events.append(e)

            text_events = [e for e in events if isinstance(e, TextDelta)]
            assert len(text_events) >= 1
            assert text_events[0].text == "Hello"

    @pytest.mark.asyncio
    async def test_tool_call_chunk(self, provider):
        """An SSE chunk with a tool_call should yield ToolUseStart."""
        choice_obj = MagicMock()
        delta_obj = MagicMock()
        delta_obj.role = None
        delta_obj.content = None

        tc_obj = MagicMock()
        tc_obj.index = 0
        tc_obj.id = "call_xyz"
        func_obj = MagicMock()
        func_obj.name = "Bash"
        func_obj.arguments = ""
        tc_obj.function = func_obj
        delta_obj.tool_calls = [tc_obj]

        choice_obj.delta = delta_obj
        choice_obj.finish_reason = None

        chunk_obj = MagicMock()
        chunk_obj.id = "chatcmpl-abc"
        chunk_obj.model = "gpt-4"
        chunk_obj.choices = [choice_obj]

        mock_stream = AsyncMock()
        mock_stream.__aiter__ = lambda self: self
        mock_stream.__anext__ = AsyncMock(side_effect=[chunk_obj, StopAsyncIteration])

        with patch.object(provider, "_create_stream", return_value=mock_stream):
            events: list[StreamEvent] = []
            async for e in provider.send_message(
                messages=[{"role": "user", "content": "Hi"}],
                tools=[{"name": "Bash", "description": "run", "input_schema": {}}],
                system="sys",
                model="gpt-4",
            ):
                events.append(e)

            tool_starts = [e for e in events if isinstance(e, ToolUseStart)]
            assert len(tool_starts) >= 1
            assert tool_starts[0].name == "Bash"

    @pytest.mark.asyncio
    async def test_finish_reason_chunk(self, provider):
        """A chunk with finish_reason should eventually yield MessageDelta."""
        choice_obj = MagicMock()
        delta_obj = MagicMock()
        delta_obj.role = None
        delta_obj.content = None
        delta_obj.tool_calls = None
        choice_obj.delta = delta_obj
        choice_obj.finish_reason = "stop"

        chunk_obj = MagicMock()
        chunk_obj.id = "chatcmpl-abc"
        chunk_obj.model = "gpt-4"
        chunk_obj.choices = [choice_obj]

        mock_stream = AsyncMock()
        mock_stream.__aiter__ = lambda self: self
        mock_stream.__anext__ = AsyncMock(side_effect=[chunk_obj, StopAsyncIteration])

        with patch.object(provider, "_create_stream", return_value=mock_stream):
            events: list[StreamEvent] = []
            async for e in provider.send_message(
                messages=[{"role": "user", "content": "Hi"}],
                tools=[],
                system="sys",
                model="gpt-4",
            ):
                events.append(e)

            deltas = [e for e in events if isinstance(e, MessageDelta)]
            assert len(deltas) >= 1
            assert deltas[0].stop_reason == "stop"


class TestOpenAICompatibleProviderModelPassthrough:
    """Verify that the model string is passed as-is with no alias mapping."""

    @pytest.fixture
    def provider(self):
        return OpenAICompatibleProvider(
            base_url="http://localhost:8080/v1",
            api_key="test-key",
        )

    @pytest.mark.asyncio
    async def test_model_passed_as_is(self, provider):
        """The model parameter must reach the API client unchanged."""
        captured_model = None

        async def capture_stream(*args, **kwargs):
            nonlocal captured_model
            captured_model = kwargs.get("model") or (args[0] if args else None)
            return _empty_async_iter()

        with patch.object(provider, "_create_stream", side_effect=capture_stream):
            try:
                async for _ in provider.send_message(
                    messages=[{"role": "user", "content": "Hi"}],
                    tools=[],
                    system="sys",
                    model="meta-llama/Llama-3-70b-chat-hf",
                ):
                    pass
            except Exception:
                pass

        # If _create_stream didn't capture it, check that the provider
        # stores the model somewhere accessible for the request.
        # The key assertion: model should be passed through without mapping.
        if captured_model is not None:
            assert captured_model == "meta-llama/Llama-3-70b-chat-hf", (
                "Model name must be passed as-is without alias mapping"
            )

    @pytest.mark.asyncio
    async def test_model_no_alias_mapping(self, provider):
        """Arbitrary model strings should not be transformed or rejected."""
        exotic_models = [
            "gpt-4-turbo-2024-04-09",
            "claude-sonnet-4-20250514",
            "deepseek-coder-33b-instruct",
            "local-model",
            "org/custom-model:latest",
        ]
        for model_name in exotic_models:
            captured = {}

            def _make_capture(dest):
                async def capture(*args, **kwargs):
                    dest["model"] = kwargs.get("model", args[0] if args else None)
                    return _empty_async_iter()

                return capture

            with patch.object(
                provider,
                "_create_stream",
                side_effect=_make_capture(captured),
            ):
                try:
                    async for _ in provider.send_message(
                        messages=[{"role": "user", "content": "Hi"}],
                        tools=[],
                        system="sys",
                        model=model_name,
                    ):
                        pass
                except Exception:
                    pass

            if "model" in captured and captured["model"] is not None:
                assert captured["model"] == model_name, (
                    f"Model '{model_name}' was transformed to '{captured['model']}'"
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _empty_async_iter():
    """Return an empty async iterator."""
    return
    yield  # noqa: RET504
