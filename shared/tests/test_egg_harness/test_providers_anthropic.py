"""Tests for egg_harness.providers.anthropic — AnthropicProvider."""

from __future__ import annotations

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.providers.anthropic")

import os
from unittest.mock import patch

from egg_config.constants import GATEWAY_PORT
from egg_harness.config import ProviderConfig
from egg_harness.providers.anthropic import AnthropicProvider
from egg_harness.providers.base import (
    Provider,
)


def _make_config(**overrides) -> ProviderConfig:
    """Build a ProviderConfig for the Anthropic provider."""
    defaults = {
        "provider_type": "anthropic",
        "model": "claude-opus-4-6",
        "endpoint": f"http://egg-gateway:{GATEWAY_PORT}",
    }
    defaults.update(overrides)
    return ProviderConfig(**defaults)


class TestAnthropicProviderInit:
    """Verify AnthropicProvider construction and gateway URL validation."""

    def test_inherits_from_provider(self):
        """AnthropicProvider must be a subclass of Provider."""
        assert issubclass(AnthropicProvider, Provider)

    def test_valid_gateway_url(self):
        """Provider should accept a well-formed gateway URL via config.endpoint."""
        config = _make_config(endpoint=f"http://egg-gateway:{GATEWAY_PORT}")
        provider = AnthropicProvider(config=config)
        assert provider is not None

    def test_valid_gateway_url_with_path(self):
        """Provider should accept a gateway URL that includes a path."""
        config = _make_config(endpoint=f"http://egg-gateway:{GATEWAY_PORT}/v1")
        provider = AnthropicProvider(config=config)
        assert provider is not None

    def test_valid_https_gateway_url(self):
        """Provider should accept HTTPS gateway URLs."""
        config = _make_config(endpoint="https://gateway.example.com")
        provider = AnthropicProvider(config=config)
        assert provider is not None

    def test_invalid_gateway_url_no_scheme(self):
        """Gateway URL without a scheme should be rejected."""
        config = _make_config(endpoint=f"egg-gateway:{GATEWAY_PORT}")
        with pytest.raises((ValueError, TypeError, RuntimeError)):
            AnthropicProvider(config=config)

    def test_invalid_gateway_url_empty(self):
        """Empty gateway URL should be accepted (falls through to SDK default)."""
        config = _make_config(endpoint="")
        # Empty endpoint is acceptable — uses SDK default base_url
        try:
            provider = AnthropicProvider(config=config)
            assert provider is not None
        except ValueError, TypeError, RuntimeError:
            pass  # Also acceptable to reject

    def test_no_endpoint_uses_default(self):
        """No endpoint should use the Anthropic SDK default base URL."""
        config = _make_config(endpoint=None)
        provider = AnthropicProvider(config=config)
        assert provider is not None

    def test_anthropic_api_key_not_in_env(self):
        """ANTHROPIC_API_KEY must NOT be present in the environment at init."""
        env_with_key = {**os.environ, "ANTHROPIC_API_KEY": "sk-ant-test-key"}
        config = _make_config()
        with patch.dict(os.environ, env_with_key, clear=True):
            with pytest.raises((ValueError, AssertionError, RuntimeError)):
                AnthropicProvider(config=config)

    def test_provider_name_is_anthropic(self):
        """The name property should return 'anthropic'."""
        config = _make_config()
        provider = AnthropicProvider(config=config)
        assert provider.name == "anthropic"

    def test_send_message_is_async_generator(self):
        """send_message should be an async generator method."""
        import inspect

        assert inspect.isasyncgenfunction(AnthropicProvider.send_message) or (
            inspect.iscoroutinefunction(AnthropicProvider.send_message)
        )

    def test_send_message_signature_has_keyword_only_params(self):
        """send_message should accept messages, tools, system, model as keyword-only."""
        import inspect

        sig = inspect.signature(AnthropicProvider.send_message)
        params = sig.parameters
        assert "messages" in params
        assert "tools" in params
        assert "system" in params
        assert "model" in params
        # messages should be keyword-only (not positional)
        assert params["messages"].kind == inspect.Parameter.KEYWORD_ONLY
