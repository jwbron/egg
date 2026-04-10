"""Tests for egg_harness.providers.base — StreamEvent types and Provider ABC."""

from __future__ import annotations

import dataclasses
import inspect
import types
import typing
from abc import ABC
from collections.abc import AsyncIterator

import pytest
from egg_harness.providers.base import (
    MessageDelta,
    MessageEnd,
    MessageStart,
    Provider,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)

ALL_EVENT_CLASSES = (
    TextDelta,
    ToolUseStart,
    ToolUseInputDelta,
    ToolUseEnd,
    ThinkingDelta,
    MessageStart,
    MessageDelta,
    MessageEnd,
)


class TestStreamEvents:
    """Verify StreamEvent dataclass creation and field contracts."""

    def test_text_delta_creation(self):
        event = TextDelta(text="hello world")
        assert event.text == "hello world"

    def test_tool_use_start_creation(self):
        event = ToolUseStart(tool_use_id="tu_001", name="Bash")
        assert event.tool_use_id == "tu_001"
        assert event.name == "Bash"

    def test_tool_use_input_delta_creation(self):
        event = ToolUseInputDelta(
            tool_use_id="tu_001",
            partial_json='{"command": "ls',
        )
        assert event.tool_use_id == "tu_001"
        assert event.partial_json == '{"command": "ls'

    def test_tool_use_end_creation(self):
        event = ToolUseEnd(tool_use_id="tu_001")
        assert event.tool_use_id == "tu_001"

    def test_thinking_delta_creation(self):
        event = ThinkingDelta(text="Let me think...")
        assert event.text == "Let me think..."

    def test_message_start_creation(self):
        event = MessageStart(
            message_id="msg_abc",
            model="claude-sonnet-4-20250514",
            role="assistant",
        )
        assert event.message_id == "msg_abc"
        assert event.model == "claude-sonnet-4-20250514"
        assert event.role == "assistant"

    def test_message_delta_creation(self):
        usage = {"input_tokens": 10, "output_tokens": 20}
        event = MessageDelta(stop_reason="end_turn", usage=usage)
        assert event.stop_reason == "end_turn"
        assert event.usage == usage

    def test_message_end_creation(self):
        usage = {"input_tokens": 10, "output_tokens": 50}
        event = MessageEnd(usage=usage)
        assert event.usage == usage

    def test_message_delta_optional_fields(self):
        """stop_reason and usage on MessageDelta can both be None."""
        event = MessageDelta(stop_reason=None, usage=None)
        assert event.stop_reason is None
        assert event.usage is None

    def test_message_end_optional_fields(self):
        """usage on MessageEnd can be None."""
        event = MessageEnd(usage=None)
        assert event.usage is None

    def test_all_event_types_are_dataclasses(self):
        for cls in ALL_EVENT_CLASSES:
            assert dataclasses.is_dataclass(cls), f"{cls.__name__} must be a dataclass"

    def test_stream_event_union_type(self):
        """StreamEvent should be a Union containing all 8 event dataclasses."""
        origin = typing.get_origin(StreamEvent)
        assert origin is types.UnionType or origin is typing.Union, (
            "StreamEvent must be a Union type"
        )

        args = set(typing.get_args(StreamEvent))
        expected = set(ALL_EVENT_CLASSES)
        assert args == expected, (
            f"StreamEvent union members mismatch.\n"
            f"  Missing: {expected - args}\n"
            f"  Extra:   {args - expected}"
        )


class TestProvider:
    """Verify Provider abstract base class contract."""

    def test_provider_is_abstract(self):
        """Provider cannot be instantiated directly."""
        with pytest.raises(TypeError):
            Provider()  # type: ignore[abstract]

    def test_provider_subclass_must_implement_send_message(self):
        """A subclass that does not implement send_message cannot be instantiated."""

        class IncompleteProvider(Provider):
            pass

        with pytest.raises(TypeError):
            IncompleteProvider()  # type: ignore[abstract]

    def test_provider_subclass_with_send_message(self):
        """A subclass implementing send_message can be instantiated."""

        class ConcreteProvider(Provider):
            async def send_message(
                self,
                messages: list,
                tools: list,
                system: str,
                model: str,
            ) -> AsyncIterator[StreamEvent]:
                yield TextDelta(text="hi")

        provider = ConcreteProvider()
        assert isinstance(provider, Provider)
        assert isinstance(provider, ABC)

    def test_send_message_returns_async_iterator(self):
        """send_message type annotation should return AsyncIterator[StreamEvent]."""
        hints = typing.get_type_hints(Provider.send_message)
        ret = hints.get("return")
        assert ret is not None, "send_message must have a return type annotation"

        origin = typing.get_origin(ret)
        assert origin is AsyncIterator or origin is typing.AsyncIterator, (
            f"Return type origin should be AsyncIterator, got {origin}"
        )

        args = typing.get_args(ret)
        assert len(args) == 1, "AsyncIterator should be parameterised with StreamEvent"
        # The arg should resolve to StreamEvent (the union itself)
        assert args[0] is StreamEvent or set(typing.get_args(args[0])) == set(ALL_EVENT_CLASSES), (
            "AsyncIterator must be parameterised with StreamEvent"
        )

    def test_provider_inherits_from_abc(self):
        """Provider must inherit from ABC."""
        assert issubclass(Provider, ABC)

    def test_send_message_is_abstract(self):
        """send_message must be declared as an abstract method."""
        assert getattr(Provider.send_message, "__isabstractmethod__", False), (
            "send_message must be decorated with @abstractmethod"
        )

    def test_send_message_signature(self):
        """send_message should accept messages, tools, system, model params."""
        sig = inspect.signature(Provider.send_message)
        param_names = list(sig.parameters.keys())
        assert "self" in param_names
        assert "messages" in param_names
        assert "tools" in param_names
        assert "system" in param_names
        assert "model" in param_names
