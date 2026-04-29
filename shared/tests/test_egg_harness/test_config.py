"""Tests for egg_harness.config — ProviderConfig, HarnessConfig, model alias resolution."""

from __future__ import annotations

import dataclasses

import pytest
from egg_harness.config import (
    HarnessConfig,
    ProviderConfig,
    get_context_window,
    parse_model_spec,
    resolve_model,
)


class TestResolveModel:
    """Test model alias resolution to full model IDs."""

    def test_opus_resolves_to_claude_opus_4_6(self):
        assert resolve_model("opus") == "claude-opus-4-6"

    def test_sonnet_resolves_to_claude_sonnet_4_5(self):
        assert resolve_model("sonnet") == "claude-sonnet-4-5-20250514"

    def test_haiku_resolves_to_claude_haiku_4_5(self):
        """Haiku alias must resolve to haiku-4-5, NOT deprecated haiku-3."""
        result = resolve_model("haiku")
        assert result == "claude-haiku-4-5"
        assert "3" not in result

    def test_unknown_alias_passes_through_as_full_model_id(self):
        """An unrecognized alias should be treated as a literal model ID."""
        assert resolve_model("claude-sonnet-4-5-20250514") == ("claude-sonnet-4-5-20250514")

    def test_custom_model_id_passes_through(self):
        """Arbitrary strings not matching aliases pass through unchanged."""
        assert resolve_model("my-custom-model-v3") == "my-custom-model-v3"

    def test_empty_string(self):
        """Empty model string should pass through or raise — not crash."""
        # Implementation may either return "" or raise ValueError.
        # We accept both, but it must not raise an unexpected exception.
        try:
            result = resolve_model("")
            assert result == "" or result is not None
        except ValueError:
            pass  # Acceptable to reject empty string

    def test_case_sensitivity(self):
        """Aliases should be case-sensitive (lowercase only)."""
        # "Opus" (capitalized) is not a recognized alias
        result = resolve_model("Opus")
        assert result != "claude-opus-4-6" or result == "Opus"


class TestParseModelSpec:
    """Test parse_model_spec for model + optional max_tokens suffix."""

    def test_opus_with_1m_suffix(self):
        """parse_model_spec('opus[1m]') returns model + max_tokens=1_000_000."""
        model, max_tokens = parse_model_spec("opus[1m]")
        assert model == "claude-opus-4-6"
        assert max_tokens == 1_000_000

    def test_sonnet_without_suffix(self):
        """parse_model_spec('sonnet') returns model with no max_tokens override."""
        model, max_tokens = parse_model_spec("sonnet")
        assert model == "claude-sonnet-4-5-20250514"
        assert max_tokens is None

    def test_haiku_with_100k_suffix(self):
        model, max_tokens = parse_model_spec("haiku[100k]")
        assert model == "claude-haiku-4-5"
        assert max_tokens == 100_000

    def test_opus_with_500k_suffix(self):
        model, max_tokens = parse_model_spec("opus[500k]")
        assert model == "claude-opus-4-6"
        assert max_tokens == 500_000

    def test_full_model_id_with_suffix(self):
        """Full model IDs should also support the suffix syntax."""
        model, max_tokens = parse_model_spec("claude-opus-4-6[1m]")
        assert model == "claude-opus-4-6"
        assert max_tokens == 1_000_000

    def test_full_model_id_without_suffix(self):
        model, max_tokens = parse_model_spec("claude-haiku-4-5")
        assert model == "claude-haiku-4-5"
        assert max_tokens is None

    def test_invalid_suffix_format_raises(self):
        """Invalid suffix like 'opus[invalid]' should raise ValueError."""
        with pytest.raises(ValueError):
            parse_model_spec("opus[invalid]")

    def test_empty_brackets_raises(self):
        """Empty brackets like 'opus[]' should raise ValueError."""
        with pytest.raises(ValueError):
            parse_model_spec("opus[]")

    def test_malformed_brackets_raises(self):
        """Mismatched brackets should raise ValueError."""
        with pytest.raises(ValueError):
            parse_model_spec("opus[100k")

    def test_numeric_suffix_without_unit(self):
        """A bare number like 'opus[1000]' — implementation decides behavior."""
        # Either parse as raw token count or raise.
        try:
            model, max_tokens = parse_model_spec("opus[1000]")
            assert model == "claude-opus-4-6"
            assert max_tokens == 1000
        except ValueError:
            pass  # Also acceptable


class TestGetContextWindow:
    """Test get_context_window for known and unknown models."""

    def test_opus_context_window(self):
        """claude-opus-4-6 should return its context window size."""
        window = get_context_window("claude-opus-4-6")
        assert isinstance(window, int)
        assert window > 0
        # Opus 4 has 200k context
        assert window == 200_000

    def test_sonnet_context_window(self):
        window = get_context_window("claude-sonnet-4-5-20250514")
        assert isinstance(window, int)
        assert window > 0

    def test_haiku_context_window(self):
        window = get_context_window("claude-haiku-4-5")
        assert isinstance(window, int)
        assert window > 0

    def test_unknown_model_handles_gracefully(self):
        """Unknown model should not crash — may return default or raise."""
        try:
            window = get_context_window("unknown-model-xyz")
            # If it returns a value, it should be a reasonable default
            assert isinstance(window, int)
            assert window > 0
        except ValueError, KeyError:
            pass  # Also acceptable to raise for unknown models


class TestProviderConfig:
    """Test ProviderConfig dataclass."""

    def test_required_fields(self):
        """ProviderConfig requires provider_type and model."""
        config = ProviderConfig(provider_type="anthropic", model="opus")
        assert config.provider_type == "anthropic"
        assert config.model == "opus"

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(ProviderConfig)

    def test_default_endpoint_is_none(self):
        config = ProviderConfig(provider_type="anthropic", model="opus")
        assert config.endpoint is None

    def test_custom_provider_and_model(self):
        config = ProviderConfig(provider_type="openai_compatible", model="gpt-4")
        assert config.provider_type == "openai_compatible"
        assert config.model == "gpt-4"

    def test_openai_compatible_with_endpoint(self):
        config = ProviderConfig(
            provider_type="openai_compatible",
            model="local-llm",
            endpoint="http://localhost:8080/v1",
        )
        assert config.provider_type == "openai_compatible"
        assert config.endpoint == "http://localhost:8080/v1"

    def test_anthropic_provider_no_endpoint_needed(self):
        """Anthropic provider should work without explicit endpoint."""
        config = ProviderConfig(provider_type="anthropic", model="sonnet")
        assert config.endpoint is None

    def test_api_key_env_default_is_none(self):
        config = ProviderConfig(provider_type="anthropic", model="opus")
        assert config.api_key_env is None

    def test_extra_headers_default_is_none(self):
        config = ProviderConfig(provider_type="anthropic", model="opus")
        assert config.extra_headers is None

    def test_extra_headers_can_be_set(self):
        config = ProviderConfig(
            provider_type="anthropic",
            model="opus",
            extra_headers={"X-Custom": "value"},
        )
        assert config.extra_headers == {"X-Custom": "value"}


class TestHarnessConfig:
    """Test HarnessConfig dataclass and defaults."""

    def _make_provider(self) -> ProviderConfig:
        return ProviderConfig(provider_type="anthropic", model="opus")

    def test_default_values(self):
        """HarnessConfig defaults with required provider."""
        config = HarnessConfig(provider=self._make_provider())
        assert config.max_turns == 200
        assert config.timeout == 7200
        assert config.cwd is None
        assert config.compaction_threshold == 0.8
        assert config.keep_recent_tokens == 20_000

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(HarnessConfig)

    def test_default_timeout(self):
        config = HarnessConfig(provider=self._make_provider())
        assert isinstance(config.timeout, int)
        assert config.timeout > 0

    def test_default_max_turns(self):
        config = HarnessConfig(provider=self._make_provider())
        assert isinstance(config.max_turns, int)
        assert config.max_turns > 0

    def test_custom_timeout(self):
        config = HarnessConfig(provider=self._make_provider(), timeout=300)
        assert config.timeout == 300

    def test_custom_max_turns(self):
        config = HarnessConfig(provider=self._make_provider(), max_turns=50)
        assert config.max_turns == 50

    def test_disallowed_tools_default_is_none(self):
        config = HarnessConfig(provider=self._make_provider())
        assert config.disallowed_tools is None

    def test_disallowed_tools_can_be_set(self):
        config = HarnessConfig(
            provider=self._make_provider(),
            disallowed_tools=["WebFetch", "WebSearch"],
        )
        assert config.disallowed_tools == ["WebFetch", "WebSearch"]

    def test_custom_values_override_defaults(self):
        config = HarnessConfig(
            provider=self._make_provider(),
            max_turns=10,
            timeout=600,
            compaction_threshold=0.9,
            keep_recent_tokens=50_000,
        )
        assert config.max_turns == 10
        assert config.timeout == 600
        assert config.compaction_threshold == 0.9
        assert config.keep_recent_tokens == 50_000

    def test_intercept_tools_default_is_true(self):
        config = HarnessConfig(provider=self._make_provider())
        assert config.intercept_tools is True

    def test_env_default_is_none(self):
        config = HarnessConfig(provider=self._make_provider())
        assert config.env is None
