"""Tests for the consensus wrapper module."""

import shlex

from consensus_wrapper import build_consensus_wrapped_command


class TestBuildConsensusWrappedCommand:
    """Tests for build_consensus_wrapped_command()."""

    def test_returns_bash_command(self):
        """Command should be a bash -c invocation."""
        cmd = build_consensus_wrapped_command("Do something")
        assert cmd[0] == "bash"
        assert cmd[1] == "-c"
        assert len(cmd) == 3

    def test_contains_claude_invocation(self):
        """The wrapper script should contain the full claude command."""
        cmd = build_consensus_wrapped_command("Test prompt")
        script = cmd[2]
        assert "claude" in script
        assert "--dangerously-skip-permissions" in script
        assert "--print" in script
        assert "--max-turns" in script
        assert "200" in script

    def test_prompt_is_shell_escaped(self):
        """Prompts with special characters should be properly escaped."""
        prompt = 'Test "quotes" and $variables and $(commands)'
        cmd = build_consensus_wrapped_command(prompt)
        script = cmd[2]
        # The prompt should appear shell-quoted in the script
        escaped = shlex.quote(prompt)
        assert escaped in script

    def test_contains_consensus_wait_loop(self):
        """The wrapper should include the consensus polling loop."""
        cmd = build_consensus_wrapped_command("Do something")
        script = cmd[2]
        assert "egg-orch signal readiness" in script
        assert "READY" in script
        assert "egg-orch message poll" in script
        assert "EGG_CONCURRENT_MODE" in script

    def test_skips_consensus_when_not_concurrent(self):
        """Script should exit normally when EGG_CONCURRENT_MODE is not set."""
        cmd = build_consensus_wrapped_command("Do something")
        script = cmd[2]
        # Should check EGG_CONCURRENT_MODE and exit early if not set
        assert "EGG_CONCURRENT_MODE" in script
        assert "exit $CLAUDE_EXIT" in script

    def test_custom_model_and_max_turns(self):
        """Should support custom model and max_turns."""
        cmd = build_consensus_wrapped_command("Prompt", model="sonnet", max_turns=50)
        script = cmd[2]
        assert "--model" in script
        assert shlex.quote("sonnet") in script
        assert shlex.quote("50") in script

    def test_consensus_check_parses_json(self):
        """The script should parse JSON response to check is_complete."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "is_complete" in script
        assert "python3" in script

    def test_has_timeout(self):
        """The consensus wait loop should have a timeout."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "TIMEOUT" in script

    def test_nonzero_exit_does_not_signal_ready(self):
        """On non-zero Claude exit, wrapper must NOT signal READY."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The script should check CLAUDE_EXIT != 0 and exit early
        assert 'if [ "$CLAUDE_EXIT" -ne 0 ]' in script
        assert "NOT signaling READY" in script

    def test_clean_exit_signals_ready(self):
        """On zero Claude exit, wrapper should signal READY."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "Agent exited cleanly" in script
        assert "auto-signaling READY" in script

    def test_timeout_configurable_via_env_var(self):
        """Wrapper timeout should be configurable via EGG_CONSENSUS_WRAPPER_TIMEOUT."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "EGG_CONSENSUS_WRAPPER_TIMEOUT" in script
