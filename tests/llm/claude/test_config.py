"""Tests for llm.claude.config module."""

from pathlib import Path

from llm.claude.config import AgentConfig, ClaudeConfig


class TestClaudeConfig:
    """Tests for ClaudeConfig dataclass."""

    def test_defaults(self):
        """Default values are sensible."""
        config = ClaudeConfig()
        assert config.cwd is None
        assert config.timeout == 7200
        assert config.allowed_tools == []

    def test_custom_cwd_path(self):
        """Accept Path as cwd."""
        config = ClaudeConfig(cwd=Path("/tmp/work"))
        assert config.cwd == Path("/tmp/work")

    def test_custom_cwd_string(self):
        """Accept string as cwd."""
        config = ClaudeConfig(cwd="/tmp/work")
        assert config.cwd == "/tmp/work"

    def test_custom_timeout(self):
        """Custom timeout."""
        config = ClaudeConfig(timeout=3600)
        assert config.timeout == 3600

    def test_allowed_tools(self):
        """Specify allowed tools list."""
        config = ClaudeConfig(allowed_tools=["Read", "Write", "Bash"])
        assert config.allowed_tools == ["Read", "Write", "Bash"]

    def test_allowed_tools_independent(self):
        """Each instance has independent tools list."""
        c1 = ClaudeConfig()
        c2 = ClaudeConfig()
        c1.allowed_tools.append("Read")
        assert c2.allowed_tools == []

    def test_backward_compat_alias(self):
        """AgentConfig is an alias for ClaudeConfig."""
        assert AgentConfig is ClaudeConfig
