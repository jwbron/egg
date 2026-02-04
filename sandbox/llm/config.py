"""
Claude Code configuration.

This module re-exports ClaudeConfig for backward compatibility.
All configuration is handled by llm.claude.config.
"""

from llm.claude.config import ClaudeConfig

# Backward compatibility aliases
LLMConfig = ClaudeConfig
BaseConfig = ClaudeConfig

__all__ = ["BaseConfig", "ClaudeConfig", "LLMConfig"]
