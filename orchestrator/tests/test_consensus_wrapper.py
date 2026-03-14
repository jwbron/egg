"""Tests for the consensus wrapper module."""

import os
import shlex
import subprocess
import tempfile

from consensus_wrapper import (
    _RECOVERY_PROMPT,
    MAX_CONSENSUS_RESTARTS,
    build_consensus_wrapped_command,
)


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

    def test_contains_restart_logic(self):
        """The wrapper should include restart logic, not auto-READY."""
        cmd = build_consensus_wrapped_command("Do something")
        script = cmd[2]
        assert "Restarting" in script
        assert "RESTART_COUNT" in script
        assert "MAX_RESTARTS" in script
        assert "egg-orch message poll" in script
        assert "EGG_CONCURRENT_MODE" in script

    def test_does_not_auto_signal_ready(self):
        """The wrapper must NOT auto-signal READY on clean exit."""
        cmd = build_consensus_wrapped_command("Do something")
        script = cmd[2]
        assert "Auto-signaling READY" not in script

    def test_skips_consensus_when_not_concurrent(self):
        """Script should exit normally when EGG_CONCURRENT_MODE is not set."""
        cmd = build_consensus_wrapped_command("Do something")
        script = cmd[2]
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

    def test_nonzero_exit_does_not_restart(self):
        """On non-zero Claude exit, wrapper must NOT restart."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert 'if [ "$CLAUDE_EXIT" -ne 0 ]' in script
        assert "NOT restarting" in script

    def test_contains_recovery_prompt(self):
        """The wrapper should contain the recovery prompt text."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "CONSENSUS RECOVERY" in script
        assert "You were restarted" in script

    def test_timeout_configurable_via_env_var(self):
        """Wrapper timeout should be configurable via EGG_CONSENSUS_WRAPPER_TIMEOUT."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "EGG_CONSENSUS_WRAPPER_TIMEOUT" in script

    def test_custom_max_restarts(self):
        """Should support custom max_restarts parameter."""
        cmd = build_consensus_wrapped_command("Prompt", max_restarts=5)
        script = cmd[2]
        assert "MAX_RESTARTS=5" in script

    def test_default_max_restarts(self):
        """Default max_restarts should match module constant."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert f"MAX_RESTARTS={MAX_CONSENSUS_RESTARTS}" in script

    def test_recovery_prompt_has_placeholders(self):
        """Recovery prompt should contain restart number placeholders."""
        assert "{restart_number}" in _RECOVERY_PROMPT
        assert "{max_restarts}" in _RECOVERY_PROMPT


class TestConsensusWrapperBehavior:
    """Behavioral tests that run the wrapper script in a subprocess.

    These exercise the actual bash logic rather than just checking for
    string patterns in the generated script.
    """

    @staticmethod
    def _make_mock_tools(tmpdir: str, log_file: str, claude_log_file: str | None = None) -> None:
        """Create mock egg-orch and claude scripts.

        The mock claude script logs a delimiter + its prompt arg and exits 0.
        The mock egg-orch logs calls and returns consensus JSON.
        """
        # Mock egg-orch
        mock_orch = os.path.join(tmpdir, "egg-orch")
        with open(mock_orch, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
            f.write('echo \'{"data": {"consensus": {"is_complete": true}}}\'\n')
        os.chmod(mock_orch, 0o755)  # nosec B103

        # Mock claude (logs delimiter + prompt to file, exits 0)
        mock_claude = os.path.join(tmpdir, "claude")
        claude_log = claude_log_file or os.path.join(tmpdir, "claude.log")
        with open(mock_claude, "w") as f:
            f.write("#!/bin/bash\n")
            # Log delimiter then prompt (last arg) to the claude log
            f.write(f'echo "---CLAUDE_CALL_START---" >> {shlex.quote(claude_log)}\n')
            f.write(f'echo "${{@: -1}}" >> {shlex.quote(claude_log)}\n')
            f.write(f'echo "---CLAUDE_CALL_END---" >> {shlex.quote(claude_log)}\n')
        os.chmod(mock_claude, 0o755)  # nosec B103

    @staticmethod
    def _make_failing_claude(tmpdir: str, exit_code: int = 1) -> None:
        """Create a mock claude that exits with a non-zero code."""
        mock_claude = os.path.join(tmpdir, "claude")
        with open(mock_claude, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f"exit {exit_code}\n")
        os.chmod(mock_claude, 0o755)  # nosec B103

    @staticmethod
    def _run_wrapper_command(
        cmd: list[str], tmpdir: str, timeout: int = 15, concurrent: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a wrapper command with test environment."""
        env = os.environ.copy()
        env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"
        if concurrent:
            env["EGG_CONCURRENT_MODE"] = "true"
        else:
            env.pop("EGG_CONCURRENT_MODE", None)
        env["EGG_CONSENSUS_WRAPPER_TIMEOUT"] = "2"
        env["EGG_MESSAGE_POLL_INTERVAL"] = "1"
        return subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_nonzero_exit_does_not_restart(self):
        """A non-zero Claude exit must not trigger restart or egg-orch calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_tools(tmpdir, log_file)
            self._make_failing_claude(tmpdir, exit_code=1)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir)

            assert result.returncode == 1
            assert "NOT restarting" in result.stdout

    def test_clean_exit_triggers_restart(self):
        """A zero Claude exit should trigger a restart, not auto-signal READY."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")
            self._make_mock_tools(tmpdir, log_file, claude_log)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=1)
            result = self._run_wrapper_command(cmd, tmpdir)

            assert result.returncode == 0
            assert "Restarting" in result.stdout
            # Claude should have been called at least twice (initial + 1 restart)
            with open(claude_log) as f:
                log_content = f.read()
            call_count = log_content.count("---CLAUDE_CALL_START---")
            assert call_count >= 2
            # Second call should contain recovery prompt content
            assert "CONSENSUS RECOVERY" in log_content

    def test_nonzero_exit_propagates_exit_code(self):
        """Wrapper must propagate the original non-zero exit code."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_tools(tmpdir, log_file)
            self._make_failing_claude(tmpdir, exit_code=42)

            cmd = build_consensus_wrapped_command("Prompt", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir)

            assert result.returncode == 42

    def test_non_concurrent_mode_skips_consensus(self):
        """Without EGG_CONCURRENT_MODE=true, wrapper exits without restart logic."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")
            self._make_mock_tools(tmpdir, log_file, claude_log)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir, concurrent=False)

            assert result.returncode == 0
            # Claude should only have been called once (no restart)
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL_START---")
            assert call_count == 1
            # No egg-orch calls
            assert not os.path.exists(log_file)

    def test_max_restarts_respected(self):
        """Wrapper should not restart more than max_restarts times."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")

            # Mock egg-orch that never reports consensus complete
            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('echo \'{"data": {"consensus": {"is_complete": false}}}\'\n')
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock claude that always exits cleanly — uses a delimiter to count calls
            mock_claude = os.path.join(tmpdir, "claude")
            with open(mock_claude, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "---CLAUDE_CALL---" >> {shlex.quote(claude_log)}\n')
            os.chmod(mock_claude, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30)

            # Claude should have been called 3 times: initial + 2 restarts
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL---")
            assert call_count == 3
            assert "Max restarts (2) exhausted" in result.stdout

    def test_no_auto_ready_on_clean_exit(self):
        """Wrapper must NOT auto-signal READY — only restarts are allowed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")
            self._make_mock_tools(tmpdir, log_file, claude_log)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=1)
            self._run_wrapper_command(cmd, tmpdir)

            # Check egg-orch calls — should not contain "signal readiness --state READY"
            # from the wrapper itself (only the agent inside Claude should signal READY)
            if os.path.exists(log_file):
                with open(log_file) as f:
                    log_content = f.read()
                # The wrapper should only call message poll/status, not signal READY
                assert "signal readiness --state READY" not in log_content
