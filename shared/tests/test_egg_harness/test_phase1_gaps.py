"""Gap tests for Phase 1 egg_harness modules.

Targets edge cases, boundary conditions, and uncovered branches in:
- config.py (model resolution, parsing, context windows)
- cost.py (negative inputs, incremental return values, token accumulation)
- events.py (callback argument edge cases, concurrency)
- providers/base.py (frozen dataclass immutability, Provider ABC)
- result.py (field validation, serialization)
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import pytest
from egg_harness.config import (
    CONTEXT_WINDOWS,
    MODEL_ALIASES,
    HarnessConfig,
    ProviderConfig,
    get_context_window,
    parse_model_spec,
    resolve_model,
)
from egg_harness.cost import TOKEN_RATES, CostTracker
from egg_harness.events import EventBus
from egg_harness.providers.base import (
    MessageDelta,
    MessageStart,
    Provider,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseStart,
)
from egg_harness.result import AgentResult

# ============================================================================
# config.py gap tests
# ============================================================================


class TestResolveModelGaps:
    """Edge cases for resolve_model."""

    def test_empty_string_passes_through(self):
        """Empty string is not a known alias, so it should pass through."""
        result = resolve_model("")
        assert result == ""

    def test_case_sensitive_aliases(self):
        """Aliases are case-sensitive — uppercase versions should pass through."""
        assert resolve_model("OPUS") == "OPUS"
        assert resolve_model("Opus") == "Opus"
        assert resolve_model("HAIKU") == "HAIKU"

    def test_all_known_aliases_resolve(self):
        """All entries in MODEL_ALIASES should resolve correctly."""
        for alias, expected in MODEL_ALIASES.items():
            assert resolve_model(alias) == expected

    def test_full_model_id_passes_through(self):
        """Full model IDs should not be re-resolved."""
        assert resolve_model("claude-opus-4-6") == "claude-opus-4-6"
        assert resolve_model("claude-sonnet-4-5-20250514") == "claude-sonnet-4-5-20250514"

    def test_haiku_resolves_to_haiku_4_5_not_deprecated_3(self):
        """Critical: haiku must map to claude-haiku-4-5, not any deprecated version."""
        resolved = resolve_model("haiku")
        assert "haiku-4-5" in resolved
        assert "haiku-3" not in resolved


class TestParseModelSpecGaps:
    """Edge cases for parse_model_spec."""

    def test_zero_size_suffix(self):
        """opus[0k] should parse to 0 tokens."""
        model, size = parse_model_spec("opus[0k]")
        assert model == "claude-opus-4-6"
        assert size == 0

    def test_large_suffix_value(self):
        """Extremely large size values should be parsed correctly."""
        model, size = parse_model_spec("opus[999m]")
        assert size == 999_000_000

    def test_empty_spec_raises(self):
        """Empty string should raise ValueError."""
        with pytest.raises(ValueError):
            parse_model_spec("")

    def test_brackets_only_raises(self):
        """Just brackets with no model name should raise."""
        with pytest.raises(ValueError):
            parse_model_spec("[1m]")

    def test_empty_brackets_raises(self):
        """Model with empty brackets should raise."""
        with pytest.raises(ValueError):
            parse_model_spec("opus[]")

    def test_suffix_without_unit_raises(self):
        """Numeric suffix without k/m unit should raise."""
        with pytest.raises(ValueError):
            parse_model_spec("opus[100]")

    def test_nested_brackets_raises(self):
        """Nested brackets should not match."""
        with pytest.raises(ValueError):
            parse_model_spec("opus[[1m]]")

    def test_multiple_brackets_raises(self):
        """Multiple bracket suffixes should not match."""
        with pytest.raises(ValueError):
            parse_model_spec("opus[1m][2k]")

    def test_1k_suffix(self):
        """opus[1k] should be 1000 tokens."""
        _, size = parse_model_spec("opus[1k]")
        assert size == 1000

    def test_1m_suffix(self):
        """opus[1m] should be 1,000,000 tokens."""
        _, size = parse_model_spec("opus[1m]")
        assert size == 1_000_000

    def test_full_model_id_with_suffix(self):
        """Full model ID with suffix should work."""
        model, size = parse_model_spec("claude-opus-4-6[1m]")
        assert model == "claude-opus-4-6"
        assert size == 1_000_000

    def test_model_with_dots_and_dashes(self):
        """Model names with dots and dashes should parse."""
        model, size = parse_model_spec("my-model.v2[200k]")
        assert model == "my-model.v2"
        assert size == 200_000


class TestGetContextWindowGaps:
    """Edge cases for get_context_window."""

    def test_unknown_model_returns_default(self):
        """Unknown models should return 128,000 (the default)."""
        assert get_context_window("completely-unknown-model") == 128_000

    def test_all_known_models_return_positive(self):
        for model in CONTEXT_WINDOWS:
            window = get_context_window(model)
            assert window > 0
            assert isinstance(window, int)

    def test_alias_name_returns_default(self):
        """Aliases themselves are NOT canonical — get_context_window expects resolved names."""
        # "opus" is an alias, not a canonical model name in CONTEXT_WINDOWS
        window = get_context_window("opus")
        assert window == 128_000  # Falls back to default since "opus" != "claude-opus-4-6"

    def test_empty_string_returns_default(self):
        assert get_context_window("") == 128_000


class TestProviderConfigGaps:
    """Edge cases for ProviderConfig."""

    def test_missing_required_fields_raises(self):
        """ProviderConfig without provider_type and model should raise."""
        with pytest.raises(TypeError):
            ProviderConfig()  # type: ignore[call-arg]

    def test_custom_api_key_env(self):
        config = ProviderConfig(
            provider_type="openai_compatible",
            model="gpt-4",
            api_key_env="OPENAI_API_KEY",
        )
        assert config.api_key_env == "OPENAI_API_KEY"


class TestHarnessConfigGaps:
    """Edge cases for HarnessConfig."""

    def _make_provider(self) -> ProviderConfig:
        return ProviderConfig(provider_type="anthropic", model="opus")

    def test_missing_provider_raises(self):
        """HarnessConfig without required provider field should raise."""
        with pytest.raises(TypeError):
            HarnessConfig()  # type: ignore[call-arg]

    def test_compaction_threshold_boundaries(self):
        """Compaction threshold can be set to 0 or 1 without error."""
        config_zero = HarnessConfig(provider=self._make_provider(), compaction_threshold=0.0)
        assert config_zero.compaction_threshold == 0.0

        config_one = HarnessConfig(provider=self._make_provider(), compaction_threshold=1.0)
        assert config_one.compaction_threshold == 1.0

    def test_zero_max_turns(self):
        """Zero max_turns creates a config — loop should handle it."""
        config = HarnessConfig(provider=self._make_provider(), max_turns=0)
        assert config.max_turns == 0

    def test_zero_timeout(self):
        """Zero timeout creates a config — loop should handle it."""
        config = HarnessConfig(provider=self._make_provider(), timeout=0)
        assert config.timeout == 0

    def test_cwd_can_be_set(self):
        config = HarnessConfig(provider=self._make_provider(), cwd="/tmp/test")
        assert config.cwd == "/tmp/test"


# ============================================================================
# cost.py gap tests
# ============================================================================


class TestCostTrackerGaps:
    """Edge cases for CostTracker."""

    def test_add_usage_returns_incremental_cost(self):
        """add_usage should return the cost of just this call, not the running total."""
        tracker = CostTracker()
        first = tracker.add_usage(input_tokens=1000, output_tokens=500, model="claude-opus-4-6")
        assert first > 0
        second = tracker.add_usage(input_tokens=1000, output_tokens=500, model="claude-opus-4-6")
        assert second == pytest.approx(first, rel=1e-6)
        assert tracker.total_cost_usd == pytest.approx(first + second, rel=1e-6)

    def test_unknown_model_returns_zero_cost_still_accumulates_tokens(self):
        """Unknown model: cost is 0 but tokens still accumulated."""
        tracker = CostTracker()
        cost = tracker.add_usage(input_tokens=1000, output_tokens=500, model="unknown-model")
        assert cost == 0.0
        assert tracker.total_cost_usd == 0.0
        assert tracker.total_input_tokens == 1000
        assert tracker.total_output_tokens == 500

    def test_token_totals_accumulate(self):
        """Verify total_input_tokens and total_output_tokens accumulate."""
        tracker = CostTracker()
        tracker.add_usage(input_tokens=100, output_tokens=50, model="claude-opus-4-6")
        tracker.add_usage(input_tokens=200, output_tokens=100, model="claude-opus-4-6")
        assert tracker.total_input_tokens == 300
        assert tracker.total_output_tokens == 150

    def test_cache_token_totals_accumulate(self):
        """Verify total_cache_read_tokens and total_cache_write_tokens accumulate."""
        tracker = CostTracker()
        tracker.add_usage(
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=500,
            cache_write_tokens=100,
            model="claude-opus-4-6",
        )
        tracker.add_usage(
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=300,
            cache_write_tokens=200,
            model="claude-opus-4-6",
        )
        assert tracker.total_cache_read_tokens == 800
        assert tracker.total_cache_write_tokens == 300

    def test_cache_read_rate_is_cheaper_than_input(self):
        """Cache reads should cost less per token than regular input."""
        for model_name, rates in TOKEN_RATES.items():
            assert rates["cache_read"] < rates["input"], (
                f"{model_name}: cache_read rate should be less than input rate"
            )

    def test_cache_write_rate_is_more_than_input(self):
        """Cache writes should cost more per token than regular input."""
        for model_name, rates in TOKEN_RATES.items():
            assert rates["cache_write"] > rates["input"], (
                f"{model_name}: cache_write rate should be more than input rate"
            )

    def test_cost_precision_with_small_tokens(self):
        """Single-token costs should maintain precision."""
        tracker = CostTracker()
        cost = tracker.add_usage(input_tokens=1, output_tokens=1, model="claude-opus-4-6")
        # 1 * 15/1M + 1 * 75/1M = 90/1M = 0.00009
        assert cost == pytest.approx(90.0 / 1_000_000, rel=1e-6)

    def test_all_models_in_rate_table_have_all_rate_types(self):
        """Every model in TOKEN_RATES should have input, output, cache_read, cache_write."""
        for model_name, rates in TOKEN_RATES.items():
            assert "input" in rates, f"{model_name} missing 'input' rate"
            assert "output" in rates, f"{model_name} missing 'output' rate"
            assert "cache_read" in rates, f"{model_name} missing 'cache_read' rate"
            assert "cache_write" in rates, f"{model_name} missing 'cache_write' rate"


# ============================================================================
# events.py gap tests
# ============================================================================


class TestEventBusGaps:
    """Edge cases for EventBus."""

    def test_compaction_callback_receives_all_three_args(self):
        """on_compaction callback should receive (summary, tokens_before, tokens_after)."""
        bus = EventBus()
        received = []
        bus.on_compaction(lambda s, b, a: received.append({"s": s, "b": b, "a": a}))
        bus.emit_compaction("Summarized goals and progress", 150_000, 20_000)
        assert len(received) == 1
        assert received[0]["s"] == "Summarized goals and progress"
        assert received[0]["b"] == 150_000
        assert received[0]["a"] == 20_000

    def test_turn_complete_callback_receives_turn_and_usage(self):
        """on_turn_complete callback should receive (turn_number, usage_dict)."""
        bus = EventBus()
        received = []
        bus.on_turn_complete(lambda t, u: received.append((t, u)))
        usage = {"input_tokens": 1500, "output_tokens": 300}
        bus.emit_turn_complete(5, usage)
        assert received == [(5, usage)]

    def test_tool_call_callback_receives_dict_input(self):
        """Tool call callback should receive the full input dict."""
        bus = EventBus()
        received = []
        bus.on_tool_call(lambda name, inp: received.append((name, inp)))
        bus.emit_tool_call("Read", {"file_path": "/tmp/test.py", "offset": 0})
        assert received[0] == ("Read", {"file_path": "/tmp/test.py", "offset": 0})

    def test_multiple_different_event_types_register_independently(self):
        """Registering callbacks for different events should not interfere."""
        bus = EventBus()
        output_calls = []
        tool_calls = []
        error_calls = []

        bus.on_output(lambda t: output_calls.append(t))
        bus.on_tool_call(lambda n, i: tool_calls.append(n))
        bus.on_error(lambda e: error_calls.append(e))

        bus.emit_output("text")
        assert len(output_calls) == 1
        assert len(tool_calls) == 0
        assert len(error_calls) == 0

    def test_callback_receives_none_values(self):
        """Callbacks should handle None-like values without crashing."""
        bus = EventBus()
        received = []
        bus.on_output(lambda t: received.append(t))
        bus.emit_output("")  # Empty string
        assert received == [""]

    def test_many_callbacks_on_same_event(self):
        """Registering 100 callbacks should all fire."""
        bus = EventBus()
        counter = [0]

        for _ in range(100):
            bus.on_output(lambda t, c=counter: c.__setitem__(0, c[0] + 1))

        bus.emit_output("test")
        assert counter[0] == 100

    def test_failing_callback_followed_by_working_callback(self):
        """A failing callback should not prevent subsequent callbacks."""
        bus = EventBus()
        results = []

        def bad(text: str) -> None:
            raise ValueError("boom")

        def good(text: str) -> None:
            results.append(text)

        bus.on_output(bad)
        bus.on_output(good)
        bus.emit_output("hello")
        assert results == ["hello"]


# ============================================================================
# providers/base.py gap tests
# ============================================================================


class TestStreamEventImmutability:
    """Verify that frozen=True on StreamEvent dataclasses prevents mutation."""

    def test_text_delta_is_frozen(self):
        event = TextDelta(text="hello")
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.text = "modified"  # type: ignore[misc]

    def test_tool_use_start_is_frozen(self):
        event = ToolUseStart(id="tu_001", name="Bash")
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.name = "Read"  # type: ignore[misc]

    def test_message_delta_is_frozen(self):
        event = MessageDelta(stop_reason="end_turn", usage={"input_tokens": 10})
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.stop_reason = "tool_use"  # type: ignore[misc]

    def test_thinking_delta_is_frozen(self):
        event = ThinkingDelta(text="thinking...")
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.text = "different"  # type: ignore[misc]

    def test_message_start_is_frozen(self):
        event = MessageStart(message_id="msg_1", model="opus", role="assistant")
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.model = "sonnet"  # type: ignore[misc]


class TestStreamEventSlots:
    """Verify that slots=True is set on StreamEvent dataclasses."""

    def test_text_delta_uses_slots(self):
        event = TextDelta(text="hello")
        assert not hasattr(event, "__dict__")

    def test_tool_use_end_uses_slots(self):
        event = ToolUseEnd(id="tu_001", name="Bash", input={"command": "ls"})
        assert not hasattr(event, "__dict__")


class TestProviderABCGaps:
    """Edge cases for Provider ABC."""

    def test_incomplete_provider_missing_name_raises(self):
        """Provider subclass without name property should not be instantiable."""

        class NoNameProvider(Provider):
            async def send_message(
                self, *, messages: list, **kwargs: Any
            ) -> AsyncIterator[StreamEvent]:
                yield TextDelta(text="hi")

        with pytest.raises(TypeError, match="name"):
            NoNameProvider()  # type: ignore[abstract]

    def test_incomplete_provider_missing_send_message_raises(self):
        """Provider subclass without send_message should not be instantiable."""

        class NoSendProvider(Provider):
            @property
            def name(self) -> str:
                return "test"

        with pytest.raises(TypeError, match="send_message"):
            NoSendProvider()  # type: ignore[abstract]

    def test_name_is_abstract_property(self):
        """name should be declared as an abstract property."""
        assert isinstance(Provider.__dict__["name"], property), "name should be a property"


class TestToolUseEndFields:
    """ToolUseEnd carries the complete parsed input dict."""

    def test_tool_use_end_has_name_and_input(self):
        event = ToolUseEnd(id="tu_1", name="Bash", input={"command": "ls -la"})
        assert event.name == "Bash"
        assert event.input == {"command": "ls -la"}

    def test_tool_use_end_empty_input(self):
        event = ToolUseEnd(id="tu_1", name="Read", input={})
        assert event.input == {}


# ============================================================================
# result.py gap tests
# ============================================================================


class TestAgentResultGaps:
    """Edge cases for AgentResult."""

    def test_negative_returncode(self):
        """Negative return codes are valid (e.g., killed by signal)."""
        result = AgentResult(success=False, stdout="", stderr="killed", returncode=-9)
        assert result.returncode == -9

    def test_large_stdout(self):
        """Large stdout content should be accepted."""
        big = "x" * 1_000_000
        result = AgentResult(success=True, stdout=big, stderr="", returncode=0)
        assert len(result.stdout) == 1_000_000

    def test_fields_match_egg_agent_result(self):
        """egg_harness.result.AgentResult should have all fields from egg_agent.result.AgentResult."""
        from egg_agent.result import AgentResult as OriginalResult

        original_fields = {f.name for f in dataclasses.fields(OriginalResult)}
        new_fields = {f.name for f in dataclasses.fields(AgentResult)}
        # New result should have all original fields plus compaction_count
        assert original_fields.issubset(new_fields), (
            f"Missing fields: {original_fields - new_fields}"
        )
        assert "compaction_count" in new_fields

    def test_asdict_roundtrip(self):
        """asdict should produce a dict with all fields."""
        result = AgentResult(
            success=True,
            stdout="output",
            stderr="",
            returncode=0,
            cost_usd=1.5,
            num_turns=10,
            duration_ms=5000,
            session_id="sess_123",
            compaction_count=2,
        )
        d = dataclasses.asdict(result)
        assert d["success"] is True
        assert d["cost_usd"] == 1.5
        assert d["compaction_count"] == 2

    def test_metadata_dict_is_mutable(self):
        """metadata dict should be mutable (not frozen dataclass)."""
        result = AgentResult(
            success=True, stdout="", stderr="", returncode=0, metadata={"key": "value"}
        )
        result.metadata["key2"] = "value2"  # type: ignore[index]
        assert result.metadata["key2"] == "value2"  # type: ignore[index]
