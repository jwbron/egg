"""Tests for llm.config module."""

from llm.config import BaseConfig, ClaudeConfig, LLMConfig


class TestLLMConfigAliases:
    """Tests for backward compatibility aliases in llm.config."""

    def test_llm_config_is_claude_config(self):
        """LLMConfig is an alias for ClaudeConfig."""
        assert LLMConfig is ClaudeConfig

    def test_base_config_is_claude_config(self):
        """BaseConfig is an alias for ClaudeConfig."""
        assert BaseConfig is ClaudeConfig
