"""Tests for the consensus wrapper module."""

import os
import shlex
import subprocess
import tempfile

from consensus_wrapper import _CONSENSUS_WRAPPER_TEMPLATE, build_consensus_wrapped_command


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


class TestConsensusWrapperBehavior:
    """Behavioral tests that run the wrapper script in a subprocess.

    These exercise the actual bash logic rather than just checking for
    string patterns in the generated script.
    """

    @staticmethod
    def _make_mock_egg_orch(tmpdir: str, log_file: str) -> str:
        """Create a mock egg-orch script that logs calls and returns consensus JSON."""
        mock_path = os.path.join(tmpdir, "egg-orch")
        with open(mock_path, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
            # Return JSON with is_complete=true so the polling loop exits fast
            f.write('echo \'{"data": {"consensus": {"is_complete": true}}}\'\n')
        os.chmod(mock_path, 0o755)
        return mock_path

    @staticmethod
    def _run_wrapper(
        claude_command: str, tmpdir: str, timeout: int = 10
    ) -> subprocess.CompletedProcess:
        """Run the wrapper script with a substituted claude command."""
        script = _CONSENSUS_WRAPPER_TEMPLATE.format(claude_command=claude_command)
        env = os.environ.copy()
        env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"
        env["EGG_CONCURRENT_MODE"] = "true"
        env["EGG_CONSENSUS_WRAPPER_TIMEOUT"] = "2"
        env["EGG_MESSAGE_POLL_INTERVAL"] = "1"
        return subprocess.run(
            ["bash", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_nonzero_exit_does_not_call_readiness(self):
        """A non-zero Claude exit must not invoke egg-orch signal readiness."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_egg_orch(tmpdir, log_file)

            # Use 'false' (returns 1) instead of 'exit 1' which would exit the shell
            result = self._run_wrapper("false", tmpdir)

            assert result.returncode == 1
            # egg-orch should never have been called at all
            assert not os.path.exists(log_file), (
                f"egg-orch was called on non-zero exit: "
                f"{open(log_file).read() if os.path.exists(log_file) else ''}"
            )
            assert "NOT signaling READY" in result.stdout

    def test_clean_exit_calls_readiness(self):
        """A zero Claude exit must invoke egg-orch signal readiness."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_egg_orch(tmpdir, log_file)

            # Use 'true' (returns 0) instead of 'exit 0' which would exit the shell
            result = self._run_wrapper("true", tmpdir)

            assert result.returncode == 0
            assert os.path.exists(log_file)
            with open(log_file) as f:
                log_content = f.read()
            assert "signal readiness --state READY" in log_content
            assert "Auto-signaling READY" in result.stdout

    def test_nonzero_exit_propagates_exit_code(self):
        """Wrapper must propagate the original non-zero exit code."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_egg_orch(tmpdir, log_file)

            # Use a subshell to produce a specific exit code
            result = self._run_wrapper("(exit 42)", tmpdir)

            assert result.returncode == 42

    def test_non_concurrent_mode_skips_consensus(self):
        """Without EGG_CONCURRENT_MODE=true, wrapper exits without consensus logic."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_egg_orch(tmpdir, log_file)

            # Use 'true' instead of 'exit 0' which would exit the shell
            script = _CONSENSUS_WRAPPER_TEMPLATE.format(claude_command="true")
            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"
            env.pop("EGG_CONCURRENT_MODE", None)

            result = subprocess.run(
                ["bash", "-c", script],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            assert result.returncode == 0
            # No egg-orch calls should have been made
            assert not os.path.exists(log_file)
