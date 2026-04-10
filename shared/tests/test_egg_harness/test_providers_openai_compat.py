"""Tests for egg_harness.providers.openai_compat — OpenAICompatibleProvider."""

from __future__ import annotations

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.providers.openai_compat")

from egg_harness.config import ProviderConfig
from egg_harness.providers.base import (
    Provider,
)
from egg_harness.providers.openai_compat import OpenAICompatibleProvider


def _make_config(**overrides) -> ProviderConfig:
    """Build a ProviderConfig for the OpenAI-compatible provider."""
    defaults = {
        "provider_type": "openai_compatible",
        "model": "gpt-4",
        "endpoint": "http://localhost:8080/v1",
    }
    defaults.update(overrides)
    return ProviderConfig(**defaults)


class TestOpenAICompatibleProviderInit:
    """Verify construction, inheritance, and endpoint configuration."""

    def test_inherits_from_provider(self):
        """OpenAICompatibleProvider must be a subclass of Provider."""
        assert issubclass(OpenAICompatibleProvider, Provider)

    def test_basic_instantiation(self):
        """Provider can be created with a valid ProviderConfig."""
        config = _make_config()
        provider = OpenAICompatibleProvider(config=config)
        assert provider is not None

    def test_endpoint_config_stored(self):
        """The endpoint should be accessible on the provider instance."""
        config = _make_config(endpoint="https://api.openrouter.ai/v1")
        provider = OpenAICompatibleProvider(config=config)
        # The provider stores the endpoint internally
        endpoint = getattr(
            provider,
            "_endpoint",
            getattr(provider, "endpoint", getattr(provider, "_base_url", None)),
        )
        assert endpoint is not None
        assert "openrouter" in str(endpoint)

    def test_endpoint_required(self):
        """Omitting endpoint should raise."""
        config = _make_config(endpoint=None)
        with pytest.raises((TypeError, ValueError)):
            OpenAICompatibleProvider(config=config)

    def test_empty_endpoint_raises(self):
        """Empty endpoint string should raise."""
        config = _make_config(endpoint="")
        with pytest.raises((TypeError, ValueError)):
            OpenAICompatibleProvider(config=config)

    def test_provider_name_is_openai_compatible(self):
        """The name property should return 'openai_compatible'."""
        config = _make_config()
        provider = OpenAICompatibleProvider(config=config)
        assert provider.name == "openai_compatible"

    def test_send_message_is_async_generator(self):
        """send_message should be an async generator method."""
        import inspect

        assert inspect.isasyncgenfunction(OpenAICompatibleProvider.send_message) or (
            inspect.iscoroutinefunction(OpenAICompatibleProvider.send_message)
        )

    def test_send_message_signature_has_keyword_only_params(self):
        """send_message should accept messages, tools, system, model as keyword-only."""
        import inspect

        sig = inspect.signature(OpenAICompatibleProvider.send_message)
        params = sig.parameters
        assert "messages" in params
        assert "tools" in params
        assert "system" in params
        assert "model" in params
        assert params["messages"].kind == inspect.Parameter.KEYWORD_ONLY


class TestOpenAICompatibleProviderModelPassthrough:
    """Verify that the model string is stored from config."""

    def test_model_stored_from_config(self):
        """The model from the config should be accessible internally."""
        config = _make_config(model="meta-llama/Llama-3-70b-chat-hf")
        provider = OpenAICompatibleProvider(config=config)
        model = getattr(provider, "_model", getattr(provider, "model", None))
        assert model == "meta-llama/Llama-3-70b-chat-hf"

    def test_arbitrary_model_strings_accepted(self):
        """Arbitrary model strings should not be transformed or rejected."""
        exotic_models = [
            "gpt-4-turbo-2024-04-09",
            "deepseek-coder-33b-instruct",
            "local-model",
            "org/custom-model:latest",
        ]
        for model_name in exotic_models:
            config = _make_config(model=model_name)
            provider = OpenAICompatibleProvider(config=config)
            stored_model = getattr(provider, "_model", getattr(provider, "model", None))
            assert stored_model == model_name, (
                f"Model '{model_name}' was transformed to '{stored_model}'"
            )


class TestOpenAICompatibleProviderExtraHeaders:
    """Verify extra_headers from config are handled."""

    def test_extra_headers_from_config(self):
        """Extra headers in ProviderConfig should be accessible."""
        config = _make_config(extra_headers={"X-Custom": "value"})
        provider = OpenAICompatibleProvider(config=config)
        # The provider should store the config which contains extra_headers
        assert provider._config.extra_headers == {"X-Custom": "value"}

    def test_api_key_env_from_config(self):
        """api_key_env in ProviderConfig should be accessible."""
        config = _make_config(api_key_env="MY_API_KEY")
        provider = OpenAICompatibleProvider(config=config)
        assert provider._config.api_key_env == "MY_API_KEY"
