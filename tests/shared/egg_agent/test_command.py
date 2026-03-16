"""Tests for egg_agent.command module."""

from egg_agent.command import build_agent_command


class TestBuildAgentCommand:
    """Tests for build_agent_command function."""

    def test_default_args(self):
        """Test command with default arguments."""
        cmd = build_agent_command("Hello world")
        assert cmd == [
            "python3",
            "-m",
            "egg_agent",
            "--model",
            "opus",
            "--max-turns",
            "200",
            "Hello world",
        ]

    def test_custom_model(self):
        """Test command with custom model."""
        cmd = build_agent_command("test", model="sonnet")
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "sonnet"

    def test_custom_max_turns(self):
        """Test command with custom max_turns."""
        cmd = build_agent_command("test", max_turns=50)
        assert "--max-turns" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "50"

    def test_system_prompt(self):
        """Test command with system prompt."""
        cmd = build_agent_command("test", system_prompt="Be helpful")
        assert "--system-prompt" in cmd
        idx = cmd.index("--system-prompt")
        assert cmd[idx + 1] == "Be helpful"

    def test_no_system_prompt_by_default(self):
        """Test that --system-prompt is not included by default."""
        cmd = build_agent_command("test")
        assert "--system-prompt" not in cmd

    def test_prompt_is_last_element(self):
        """Test that prompt is always the last element."""
        cmd = build_agent_command("my prompt", model="sonnet", max_turns=100)
        assert cmd[-1] == "my prompt"

    def test_prompt_with_system_prompt_is_last(self):
        """Test that prompt is last even with system prompt."""
        cmd = build_agent_command("my prompt", system_prompt="sys")
        assert cmd[-1] == "my prompt"
