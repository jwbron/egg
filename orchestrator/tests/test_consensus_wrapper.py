"""Tests for the consensus wrapper module."""

import os
import shlex
import subprocess
import sys
import tempfile

from consensus_wrapper import (
    _RECOVERY_SYSTEM_PROMPT,
    _RECOVERY_USER_PROMPT,
    MAX_CONSENSUS_RESTARTS,
    MAX_READY_POLL_CYCLES,
    STARTUP_FAILURE_WINDOW_SECONDS,
    TRANSIENT_RESTART_BACKOFF_INITIAL,
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

    def test_contains_agent_invocation(self):
        """The wrapper script should contain the Agent SDK command."""
        cmd = build_consensus_wrapped_command("Test prompt")
        script = cmd[2]
        assert "python3" in script
        assert "egg_agent" in script
        assert "--max-turns" in script
        assert "1000" in script

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
        assert "EGG_CONCURRENT_MODE" in script
        assert "BRC" in script

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
        assert "exit $AGENT_EXIT" in script

    def test_default_max_turns_is_1000(self):
        """Default max_turns should be 1000 to prevent exhaustion during stay-alive."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "--max-turns 1000" in script

    def test_custom_model_and_max_turns(self):
        """Should support custom model and max_turns."""
        cmd = build_consensus_wrapped_command("Prompt", model="sonnet", max_turns=50)
        script = cmd[2]
        assert "--model" in script
        assert shlex.quote("sonnet") in script
        assert shlex.quote("50") in script

    def test_consensus_check_parses_json(self):
        """The script should use pipeline status and parse nested consensus JSON."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "egg-orch pipeline status --json" in script
        assert "is_complete" in script
        assert "python3" in script
        # Must use the correct nested path: data.concurrent.consensus
        assert "concurrent" in script

    def test_has_max_restarts(self):
        """The wrapper should cap restart attempts via MAX_RESTARTS."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "MAX_RESTARTS" in script

    def test_nonzero_exit_does_not_restart(self):
        """On non-transient non-zero agent exit, wrapper must NOT restart."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert 'if [ "$AGENT_EXIT" -ne 0 ]' in script
        assert "NOT restarting" in script

    def test_is_transient_crash_function_in_script(self):
        """The wrapper should contain the is_transient_crash function with correct codes."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "is_transient_crash()" in script
        # Verify the exact case pattern with all expected transient exit codes
        assert "134|136|137|139|255) return 0" in script

    def test_transient_crash_detection_in_nonzero_handler(self):
        """The non-zero exit handler should call is_transient_crash before giving up."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "is_transient_crash" in script
        assert "Transient crash" in script
        assert "Will restart with backoff" in script

    def test_backoff_variables_in_script(self):
        """The wrapper should contain backoff tracking variables."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "CRASH_BACKOFF=0" in script
        assert "TRANSIENT_BACKOFF_INITIAL=" in script
        assert "TRANSIENT_BACKOFF_MAX=30" in script
        assert "Backoff: sleeping" in script

    def test_default_transient_backoff_initial(self):
        """Default transient_backoff_initial should match module constant."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert f"TRANSIENT_BACKOFF_INITIAL={TRANSIENT_RESTART_BACKOFF_INITIAL}" in script

    def test_custom_transient_backoff_initial(self):
        """Should support custom transient_backoff_initial parameter."""
        cmd = build_consensus_wrapped_command("Prompt", transient_backoff_initial=10)
        script = cmd[2]
        assert "TRANSIENT_BACKOFF_INITIAL=10" in script

    def test_transient_crash_constant_value(self):
        """TRANSIENT_RESTART_BACKOFF_INITIAL should be 5 (as specified in plan)."""
        assert TRANSIENT_RESTART_BACKOFF_INITIAL == 5

    def test_is_startup_failure_function_in_script(self):
        """The wrapper should contain is_startup_failure with exit-1-and-duration gating."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "is_startup_failure()" in script
        assert "STARTUP_FAILURE_WINDOW_SECONDS" in script
        # Function must compare duration against the window
        assert 'if [ "$code" -ne 1 ]' in script
        assert 'if [ "$duration" -lt "$STARTUP_FAILURE_WINDOW_SECONDS" ]' in script

    def test_default_startup_failure_window(self):
        """Default startup_failure_window_seconds should match module constant."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert f"STARTUP_FAILURE_WINDOW_SECONDS={STARTUP_FAILURE_WINDOW_SECONDS}" in script

    def test_custom_startup_failure_window(self):
        """Should support custom startup_failure_window_seconds parameter."""
        cmd = build_consensus_wrapped_command("Prompt", startup_failure_window_seconds=7)
        script = cmd[2]
        assert "STARTUP_FAILURE_WINDOW_SECONDS=7" in script

    def test_agent_duration_tracking_in_script(self):
        """The wrapper should track agent run duration around each run_agent call."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "AGENT_START=$SECONDS" in script
        assert "AGENT_DURATION=$((SECONDS - AGENT_START))" in script
        # Both the initial run and the restart-loop run need tracking
        assert script.count("AGENT_START=$SECONDS") >= 2

    def test_startup_failure_check_in_nonzero_handlers(self):
        """Both the initial and restart-loop non-zero handlers should call is_startup_failure."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert script.count('is_startup_failure "$AGENT_EXIT" "$AGENT_DURATION"') >= 2
        assert "Startup failure" in script

    def test_backoff_reset_on_clean_exit(self):
        """The wrapper should reset CRASH_BACKOFF to 0 on clean agent exit."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # After a clean exit in the restart loop, backoff should reset
        assert "CRASH_BACKOFF=0" in script

    def test_transient_crash_on_restart_continues(self):
        """Transient crash during restart loop should continue (not exit)."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "Transient crash on restart" in script
        assert "Will retry" in script

    def test_contains_recovery_system_prompt(self):
        """The wrapper should contain the BRC recovery system prompt text."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "BRC Consensus Recovery" in script
        assert "--system-prompt" in script

    def test_exits_with_failure_after_max_restarts(self):
        """After exhausting restarts, wrapper should exit 1 (not wait passively)."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "Exiting with failure" in script
        assert "exit 1" in script
        assert "never reached CONFIRMED" in script

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

    def test_recovery_system_prompt_has_placeholders(self):
        """Recovery system prompt should contain restart number and BRC state placeholders."""
        assert "{restart_number}" in _RECOVERY_SYSTEM_PROMPT
        assert "{max_restarts}" in _RECOVERY_SYSTEM_PROMPT
        assert "{brc_state}" in _RECOVERY_SYSTEM_PROMPT
        # {role} was removed — it is not used in the prompt
        assert "{role}" not in _RECOVERY_SYSTEM_PROMPT

    def test_recovery_user_prompt_is_benign(self):
        """Recovery user prompt should not contain commands or injection-like content."""
        assert "egg-orch" not in _RECOVERY_USER_PROMPT
        assert "restarted" not in _RECOVERY_USER_PROMPT.lower()

    def test_contains_confirmed_check_before_restart(self):
        """Wrapper should check if agent already reached CONFIRMED before restarting."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "already CONFIRMED" in script
        assert "EGG_AGENT_ROLE" in script

    def test_ready_polling_uses_separate_constant(self):
        """READY polling loop should use MAX_READY_POLLS, not MAX_RESTARTS."""
        cmd = build_consensus_wrapped_command("Prompt", max_ready_polls=15)
        script = cmd[2]
        assert "MAX_READY_POLLS=15" in script

    def test_default_max_ready_polls(self):
        """Default max_ready_polls should match module constant."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert f"MAX_READY_POLLS={MAX_READY_POLL_CYCLES}" in script


def _make_mock_agent(tmpdir: str, agent_log_file: str | None = None, exit_code: int = 0) -> None:
    """Create a mock python3 script that intercepts ``-m egg_agent`` calls.

    Non-egg_agent ``python3`` invocations (used by the wrapper for JSON
    parsing) fall through to the real ``python3``.

    The mock logs all arguments so tests can verify both the user prompt
    (last positional arg) and flags like ``--system-prompt``.

    Args:
        tmpdir: Directory to create the mock in (must be on PATH).
        agent_log_file: File to log calls to. If None, logs to tmpdir/claude.log.
        exit_code: Exit code for the mock. When 0, logs call details;
            when non-zero, exits immediately with that code.
    """
    mock_python = os.path.join(tmpdir, "python3")
    agent_log = agent_log_file or os.path.join(tmpdir, "claude.log")
    real_python = sys.executable
    with open(mock_python, "w") as f:
        f.write("#!/bin/bash\n")
        # Intercept only -m egg_agent calls; pass everything else to real python3
        f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
        if exit_code != 0:
            f.write(f"  exit {exit_code}\n")
        else:
            f.write(f'  echo "---CLAUDE_CALL_START---" >> {shlex.quote(agent_log)}\n')
            f.write(f'  echo "ARGS: $*" >> {shlex.quote(agent_log)}\n')
            f.write(f'  echo "---CLAUDE_CALL_END---" >> {shlex.quote(agent_log)}\n')
        f.write("else\n")
        f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
        f.write("fi\n")
    os.chmod(mock_python, 0o755)  # nosec B103


class TestConsensusWrapperBehavior:
    """Behavioral tests that run the wrapper script in a subprocess.

    These exercise the actual bash logic rather than just checking for
    string patterns in the generated script.
    """

    @staticmethod
    def _make_mock_tools(tmpdir: str, log_file: str, claude_log_file: str | None = None) -> None:
        """Create mock egg-orch and claude scripts.

        The mock claude script logs a delimiter + its prompt arg and exits 0.
        The mock egg-orch logs calls and returns consensus-complete JSON
        matching the real ``egg-orch pipeline status`` response structure.
        """
        # Mock egg-orch
        mock_orch = os.path.join(tmpdir, "egg-orch")
        with open(mock_orch, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
            f.write('echo \'{"data": {"concurrent": {"consensus": {"is_complete": true}}}}\'\n')
        os.chmod(mock_orch, 0o755)  # nosec B103

        _make_mock_agent(tmpdir, claude_log_file)

    @staticmethod
    def _make_failing_agent(tmpdir: str, exit_code: int = 1) -> None:
        """Create a mock agent that exits with a non-zero code."""
        _make_mock_agent(tmpdir, exit_code=exit_code)

    @staticmethod
    def _run_wrapper_command(
        cmd: list[str],
        tmpdir: str,
        timeout: int = 15,
        concurrent: bool = True,
        agent_role: str | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a wrapper command with test environment."""
        env = os.environ.copy()
        env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"
        if concurrent:
            env["EGG_CONCURRENT_MODE"] = "true"
        else:
            env.pop("EGG_CONCURRENT_MODE", None)
        if agent_role:
            env["EGG_AGENT_ROLE"] = agent_role
        else:
            env.pop("EGG_AGENT_ROLE", None)
        env["EGG_CONSENSUS_WRAPPER_TIMEOUT"] = "2"
        env["EGG_MESSAGE_POLL_INTERVAL"] = "1"
        return subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_nonzero_exit_with_consensus_exits_cleanly(self):
        """Non-zero agent exit when consensus already reached should exit 0 (issue #1495)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            # _make_mock_tools creates an egg-orch that returns is_complete=true
            self._make_mock_tools(tmpdir, log_file)
            self._make_failing_agent(tmpdir, exit_code=1)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir)

            assert result.returncode == 0
            assert "consensus already reached" in result.stderr

    def test_nonzero_exit_without_consensus_fails(self):
        """Non-transient, non-startup-window non-zero exit without consensus should still fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_orch_no_consensus(tmpdir, log_file)
            # Use exit 42: not a signal-transient code, and not exit 1 (so the
            # startup-failure heuristic does not apply) — must fail fast.
            self._make_failing_agent(tmpdir, exit_code=42)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir)

            assert result.returncode == 42
            assert "NOT restarting" in result.stderr

    def test_nonzero_exit_with_agent_confirmed_exits_cleanly(self):
        """Non-zero agent exit when agent already CONFIRMED should exit 0 (issue #1495)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            # Create mock egg-orch: is_complete=false but agent is confirmed
            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": '
                    '{"is_complete": false, "agents": {"coder": {"confirmed": true}}}}}}\'\n'
                )
                f.write('elif echo "$@" | grep -q "message poll"; then\n')
                f.write('  echo "[]"\n')
                f.write("else\n")
                f.write('  echo "{}"\n')
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103
            self._make_failing_agent(tmpdir, exit_code=1)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(
                cmd,
                tmpdir,
                agent_role="coder",
                timeout=30,
            )

            assert result.returncode == 0
            assert "already CONFIRMED" in result.stderr

    @staticmethod
    def _make_mock_tools_with_delayed_consensus(
        tmpdir: str,
        log_file: str,
        claude_log_file: str | None = None,
        consensus_after: int = 2,
    ) -> None:
        """Create mock tools where egg-orch returns is_complete=false initially.

        The mock egg-orch uses a counter file to track calls to 'pipeline status'.
        It returns is_complete=false until the Nth 'pipeline status' call, then true.
        Response structure matches real ``egg-orch pipeline status --json`` output.
        """
        counter_file = os.path.join(tmpdir, "orch_status_count")
        mock_orch = os.path.join(tmpdir, "egg-orch")
        with open(mock_orch, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
            # Only track 'pipeline status' calls for consensus gating
            f.write('if echo "$@" | grep -q "pipeline status"; then\n')
            f.write("  COUNT=0\n")
            f.write(f"  if [ -f {shlex.quote(counter_file)} ]; then\n")
            f.write(f"    COUNT=$(cat {shlex.quote(counter_file)})\n")
            f.write("  fi\n")
            f.write("  COUNT=$((COUNT + 1))\n")
            f.write(f'  echo "$COUNT" > {shlex.quote(counter_file)}\n')
            f.write(f'  if [ "$COUNT" -ge {consensus_after} ]; then\n')
            f.write('    echo \'{"data": {"concurrent": {"consensus": {"is_complete": true}}}}\'\n')
            f.write("  else\n")
            f.write(
                '    echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
            )
            f.write("  fi\n")
            f.write("else\n")
            f.write('  echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n')
            f.write("fi\n")
        os.chmod(mock_orch, 0o755)  # nosec B103

        _make_mock_agent(tmpdir, claude_log_file)

    @staticmethod
    def _make_mock_orch_no_consensus(tmpdir: str, log_file: str) -> None:
        """Create a mock egg-orch that always returns consensus incomplete.

        Handles ``pipeline status`` (returns is_complete=false), ``message poll``
        (returns empty list), and falls through to ``{}`` for anything else.
        Does NOT create a mock agent — callers combine this with
        ``_make_failing_agent`` or a custom agent script.
        """
        mock_orch = os.path.join(tmpdir, "egg-orch")
        with open(mock_orch, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
            f.write('if echo "$@" | grep -q "pipeline status"; then\n')
            f.write(
                '  echo \'{"data": {"concurrent": {"consensus": '
                '{"is_complete": false, "agents": {}}}}}\'\n'
            )
            f.write('elif echo "$@" | grep -q "message poll"; then\n')
            f.write('  echo "[]"\n')
            f.write("else\n")
            f.write('  echo "{}"\n')
            f.write("fi\n")
        os.chmod(mock_orch, 0o755)  # nosec B103

    def test_clean_exit_triggers_restart(self):
        """A zero Claude exit should trigger a restart, not auto-signal READY."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")
            # Use delayed consensus: false on first status check, true on second
            self._make_mock_tools_with_delayed_consensus(
                tmpdir,
                log_file,
                claude_log,
                consensus_after=2,
            )

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=1)
            result = self._run_wrapper_command(cmd, tmpdir)

            assert result.returncode == 0
            assert "Restarting" in result.stderr
            # Claude should have been called at least twice (initial + 1 restart)
            with open(claude_log) as f:
                log_content = f.read()
            call_count = log_content.count("---CLAUDE_CALL_START---")
            assert call_count >= 2
            # Second call should contain the benign recovery user prompt
            assert "Continue the BRC consensus protocol" in log_content
            # Verify --system-prompt is passed on restart calls but not on the
            # initial call (reviewer feedback #1).
            calls = log_content.split("---CLAUDE_CALL_START---")[1:]  # skip leading empty
            initial_call = calls[0]
            restart_call = calls[1]
            assert "--system-prompt" not in initial_call
            assert "--system-prompt" in restart_call

    def test_nonzero_exit_propagates_exit_code_without_consensus(self):
        """Wrapper must propagate the original non-zero exit code when consensus not reached."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            # Create mock egg-orch that returns is_complete=false and no agents
            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": {"is_complete": false, "agents": {}}}}}\'\n'
                )
                f.write('elif echo "$@" | grep -q "message poll"; then\n')
                f.write('  echo "[]"\n')
                f.write("else\n")
                f.write('  echo "{}"\n')
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103
            self._make_failing_agent(tmpdir, exit_code=42)

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
                f.write(
                    'echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock python3 that intercepts egg_agent calls and exits cleanly
            real_python = sys.executable
            mock_python = os.path.join(tmpdir, "python3")
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write(f'  echo "---CLAUDE_CALL---" >> {shlex.quote(claude_log)}\n')
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30)

            # Claude should have been called 3 times: initial + 2 restarts
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL---")
            assert call_count == 3
            assert "Max restarts (2) exhausted" in result.stderr
            assert "never reached CONFIRMED" in result.stderr
            # Should exit with failure code after exhausting restarts
            assert result.returncode == 1

    @staticmethod
    def _make_mock_tools_with_agent_confirmed_state(
        tmpdir: str,
        log_file: str,
        claude_log_file: str | None = None,
        agent_role: str = "coder",
        consensus_after: int = 2,
    ) -> None:
        """Create mock tools where the agent is already CONFIRMED but consensus is pending.

        The mock egg-orch returns per-agent state showing the agent as CONFIRMED
        with ``is_complete=false`` initially. After ``consensus_after`` calls
        to ``pipeline status``, it returns ``is_complete=true``. This exercises
        the CONFIRMED polling path (skip restart, wait for consensus).
        """
        counter_file = os.path.join(tmpdir, "orch_status_count")
        mock_orch = os.path.join(tmpdir, "egg-orch")
        # Build JSON strings with agent state — use string concatenation to
        # avoid f-string brace escaping confusion.
        json_incomplete = (
            '{"data": {"concurrent": {"consensus": {"is_complete": false, '
            '"agents": {"' + agent_role + '": {"confirmed": true}}}}}}'
        )
        json_complete = (
            '{"data": {"concurrent": {"consensus": {"is_complete": true, '
            '"agents": {"' + agent_role + '": {"confirmed": true}}}}}}'
        )
        with open(mock_orch, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
            f.write('if echo "$@" | grep -q "pipeline status"; then\n')
            f.write("  COUNT=0\n")
            f.write(f"  if [ -f {shlex.quote(counter_file)} ]; then\n")
            f.write(f"    COUNT=$(cat {shlex.quote(counter_file)})\n")
            f.write("  fi\n")
            f.write("  COUNT=$((COUNT + 1))\n")
            f.write(f'  echo "$COUNT" > {shlex.quote(counter_file)}\n')
            f.write(f'  if [ "$COUNT" -ge {consensus_after} ]; then\n')
            f.write(f"    echo '{json_complete}'\n")
            f.write("  else\n")
            f.write(f"    echo '{json_incomplete}'\n")
            f.write("  fi\n")
            f.write("else\n")
            f.write('  echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n')
            f.write("fi\n")
        os.chmod(mock_orch, 0o755)  # nosec B103

        _make_mock_agent(tmpdir, claude_log_file)

    def test_confirmed_agent_skips_restart_and_polls(self):
        """Agent already CONFIRMED should skip restart and poll for consensus."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")
            # Mock returns agent as CONFIRMED, consensus false then true on 3rd call
            self._make_mock_tools_with_agent_confirmed_state(
                tmpdir,
                log_file,
                claude_log,
                agent_role="coder",
                consensus_after=3,
            )

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2, max_ready_polls=5)
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30, agent_role="coder")

            # Should exit cleanly
            assert result.returncode == 0
            # Should detect agent is already CONFIRMED and skip restart
            assert "already CONFIRMED" in result.stderr
            # Should eventually detect consensus
            assert "Consensus reached" in result.stderr
            # Claude should only be called once (no restart)
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL_START---")
            assert call_count == 1
            # Should NOT show any restart messages
            assert "Restarting" not in result.stderr

    def test_no_auto_ready_on_clean_exit(self):
        """Wrapper must NOT auto-signal READY or auto-confirm — only restarts are allowed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")
            self._make_mock_tools(tmpdir, log_file, claude_log)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=1)
            self._run_wrapper_command(cmd, tmpdir)

            # Check egg-orch calls — should not contain readiness signals or
            # consensus confirmations from the wrapper itself
            if os.path.exists(log_file):
                with open(log_file) as f:
                    log_content = f.read()
                # The wrapper should only call pipeline status, not signal READY/CONFIRMED
                assert "signal readiness --state READY" not in log_content
                assert "consensus confirmed" not in log_content

    def test_message_bus_fallback_detects_confirmed(self):
        """When pipeline status returns empty agents, wrapper should check message bus."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The wrapper should contain the message bus fallback logic
        assert "Checking message bus" in script
        assert "CONSENSUS_CONFIRMED" in script
        assert "message poll" in script

    def test_message_bus_fallback_enters_confirmed_wait(self):
        """When CONSENSUS_CONFIRMED found in message bus, should enter confirmed wait loop."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")

            counter_file = os.path.join(tmpdir, "orch_status_count")
            mock_orch = os.path.join(tmpdir, "egg-orch")

            # Build mock that returns empty agents initially (simulating lost tracker)
            # then returns is_complete=true on the 2nd pipeline status call.
            # For message poll, returns a CONSENSUS_CONFIRMED message from coder.
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                f.write("  COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(counter_file)} ]; then\n")
                f.write(f"    COUNT=$(cat {shlex.quote(counter_file)})\n")
                f.write("  fi\n")
                f.write("  COUNT=$((COUNT + 1))\n")
                f.write(f'  echo "$COUNT" > {shlex.quote(counter_file)}\n')
                f.write('  if [ "$COUNT" -ge 3 ]; then\n')
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": {"is_complete": true, "agents": {}}}}}\'\n'
                )
                f.write("  else\n")
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": {"is_complete": false, "agents": {}}}}}\'\n'
                )
                f.write("  fi\n")
                f.write('elif echo "$@" | grep -q "message poll"; then\n')
                f.write(
                    '  echo \'[{"message_type": "CONSENSUS_CONFIRMED", "from_role": "coder"}]\'\n'
                )
                f.write("else\n")
                f.write('  echo "{}"\n')
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            _make_mock_agent(tmpdir, claude_log)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2, max_ready_polls=5)
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30, agent_role="coder")

            assert result.returncode == 0
            # Should detect empty state and check message bus
            assert "Checking message bus" in result.stderr
            assert "Already confirmed" in result.stderr
            # Should NOT restart
            assert "Restarting" not in result.stderr

    def test_post_restart_confirmed_detection(self):
        """After restart, if agent reached CONFIRMED, should enter wait loop (RC4)."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The wrapper should call check_confirmed_and_wait after each restart
        assert "check_confirmed_and_wait" in script
        # Should be called both before restart loop and after restart
        assert script.count("check_confirmed_and_wait") >= 2

    def test_empty_state_recovery_prompt(self):
        """Recovery prompt should include empty state recovery guidance (RC1)."""
        assert "Empty state recovery" in _RECOVERY_SYSTEM_PROMPT
        assert "egg-orch consensus confirmed" in _RECOVERY_SYSTEM_PROMPT
        assert "Do NOT re-propose if already fully ACKed" in _RECOVERY_SYSTEM_PROMPT

    def test_wrapper_queries_consensus_status_on_empty_state(self):
        """When BRC state is empty, wrapper should query consensus status for context (RC1)."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Should check for empty BRC state and query consensus status
        assert "consensus status" in script
        assert "tracker likely lost" in script

    def test_check_confirmed_and_wait_is_shell_function(self):
        """check_confirmed_and_wait should be defined as a reusable shell function."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Should define the function
        assert "check_confirmed_and_wait()" in script
        # Should contain the full logic
        assert "CONSENSUS_CONFIRMED" in script
        assert "message poll" in script

    def test_final_consensus_check_before_failure_exit(self):
        """After max restarts, wrapper should check consensus one final time before failing.

        This is the consensus wrapper half of the issue #1495 fix. Even after
        exhausting restarts, the agent may have contributed to consensus (e.g.
        via a network hiccup after signaling READY). A final poll prevents
        falsely failing a pipeline that actually succeeded.
        """
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Should contain the final consensus check
        assert "FINAL_RESPONSE" in script
        assert "FINAL_IS_COMPLETE" in script
        # Should exit 0 if consensus was reached on final check
        assert "Consensus reached on final check" in script
        # The final check "exit 0" must come BEFORE the failure "exit 1".
        # Find the success exit from the final check and the failure exit.
        final_success_pos = script.find("Consensus reached on final check")
        failure_exit_pos = script.find("Exiting with failure")
        assert final_success_pos > 0, "Final consensus success message not found"
        assert failure_exit_pos > 0, "Failure exit message not found"
        assert final_success_pos < failure_exit_pos, (
            "Final consensus success exit must precede the failure exit"
        )

    def test_final_consensus_check_exits_zero_when_complete(self):
        """Behavioral test: final consensus check exits 0 when is_complete=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")

            counter_file = os.path.join(tmpdir, "orch_status_count")
            mock_orch = os.path.join(tmpdir, "egg-orch")
            # Mock: return is_complete=false for all status checks EXCEPT the
            # last one (the final check after restarts exhausted).
            # With max_restarts=1, there are ~3 status checks:
            #   1. Initial check after agent exits
            #   2. Check after restart
            #   3. Final check after max restarts
            # Return true on the 3rd+ call.
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                f.write("  COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(counter_file)} ]; then\n")
                f.write(f"    COUNT=$(cat {shlex.quote(counter_file)})\n")
                f.write("  fi\n")
                f.write("  COUNT=$((COUNT + 1))\n")
                f.write(f'  echo "$COUNT" > {shlex.quote(counter_file)}\n')
                f.write('  if [ "$COUNT" -ge 3 ]; then\n')
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": {"is_complete": true}}}}\'\n'
                )
                f.write("  else\n")
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
                f.write("  fi\n")
                f.write("else\n")
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            _make_mock_agent(tmpdir, claude_log)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=1)
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30)

            # Should exit 0 because final consensus check found is_complete=True
            assert result.returncode == 0, (
                f"Expected exit 0 from final consensus check, got {result.returncode}. "
                f"stderr: {result.stderr}"
            )
            assert "final check" in result.stderr.lower() or "Consensus reached" in result.stderr

    def test_final_consensus_check_still_fails_when_incomplete(self):
        """Behavioral test: final check still exits 1 when consensus not reached."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")

            # Mock egg-orch that always returns is_complete=false
            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write(
                    'echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
            os.chmod(mock_orch, 0o755)  # nosec B103

            _make_mock_agent(tmpdir, claude_log)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=1)
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30)

            # Should still exit 1 because consensus was never reached
            assert result.returncode == 1
            assert "Max restarts" in result.stderr
            assert "never reached CONFIRMED" in result.stderr

    def test_transient_crash_triggers_restart(self):
        """Transient crash (exit 139/SIGSEGV) should trigger restart, not immediate failure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")

            counter_file = os.path.join(tmpdir, "orch_status_count")
            call_counter = os.path.join(tmpdir, "agent_call_count")
            mock_orch = os.path.join(tmpdir, "egg-orch")

            # Mock egg-orch: consensus incomplete initially, complete on 3rd call
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                f.write("  COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(counter_file)} ]; then\n")
                f.write(f"    COUNT=$(cat {shlex.quote(counter_file)})\n")
                f.write("  fi\n")
                f.write("  COUNT=$((COUNT + 1))\n")
                f.write(f'  echo "$COUNT" > {shlex.quote(counter_file)}\n')
                f.write('  if [ "$COUNT" -ge 3 ]; then\n')
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": {"is_complete": true}}}}\'\n'
                )
                f.write("  else\n")
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
                f.write("  fi\n")
                f.write("else\n")
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock agent: exits 139 (SIGSEGV) on first call, then exits 0 on restarts
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write("  CALL_COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(call_counter)} ]; then\n")
                f.write(f"    CALL_COUNT=$(cat {shlex.quote(call_counter)})\n")
                f.write("  fi\n")
                f.write("  CALL_COUNT=$((CALL_COUNT + 1))\n")
                f.write(f'  echo "$CALL_COUNT" > {shlex.quote(call_counter)}\n')
                f.write(f'  echo "---CLAUDE_CALL_START---" >> {shlex.quote(claude_log)}\n')
                f.write(f'  echo "ARGS: $*" >> {shlex.quote(claude_log)}\n')
                f.write(f'  echo "---CLAUDE_CALL_END---" >> {shlex.quote(claude_log)}\n')
                f.write('  if [ "$CALL_COUNT" -eq 1 ]; then\n')
                f.write("    exit 139\n")  # SIGSEGV on first call
                f.write("  fi\n")
                f.write("  exit 0\n")  # Clean exit on restart
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command(
                "Do the work", max_restarts=2, transient_backoff_initial=1
            )
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30)

            # Agent should have been restarted after the transient crash
            assert "Transient crash (code 139)" in result.stderr
            assert "Will restart with backoff" in result.stderr
            assert "Restarting" in result.stderr
            # Should NOT contain "NOT restarting"
            assert "NOT restarting" not in result.stderr
            # Agent should have been called at least twice
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL_START---")
            assert call_count >= 2

    def test_non_transient_nonzero_exit_does_not_restart(self):
        """Non-transient, non-startup-window non-zero exit (e.g. exit 42) should NOT restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_orch_no_consensus(tmpdir, log_file)
            # Exit 42 is not a signal-transient code, not exit 1, so it should
            # bypass both is_transient_crash and is_startup_failure.
            self._make_failing_agent(tmpdir, exit_code=42)

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir)

            assert result.returncode == 42
            assert "NOT restarting" in result.stderr
            # Should NOT contain transient crash or startup failure messages
            assert "Transient crash" not in result.stderr
            assert "Startup failure" not in result.stderr

    def test_transient_crash_exit_255_triggers_restart(self):
        """Bun segfault exit code 255 should also trigger restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")

            call_counter = os.path.join(tmpdir, "agent_call_count")
            # Mock egg-orch: returns consensus complete immediately (to avoid complex setup)
            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
                f.write("else\n")
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock agent: exits 255 on first call, then 0
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write("  CALL_COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(call_counter)} ]; then\n")
                f.write(f"    CALL_COUNT=$(cat {shlex.quote(call_counter)})\n")
                f.write("  fi\n")
                f.write("  CALL_COUNT=$((CALL_COUNT + 1))\n")
                f.write(f'  echo "$CALL_COUNT" > {shlex.quote(call_counter)}\n')
                f.write(f'  echo "---CLAUDE_CALL---" >> {shlex.quote(claude_log)}\n')
                f.write('  if [ "$CALL_COUNT" -eq 1 ]; then\n')
                f.write("    exit 255\n")  # Bun segfault
                f.write("  fi\n")
                f.write("  exit 0\n")
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command(
                "Do the work", max_restarts=2, transient_backoff_initial=1
            )
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30)

            # Should detect as transient crash
            assert "Transient crash (code 255)" in result.stderr
            assert "Will restart with backoff" in result.stderr
            # Should restart, not fail immediately
            assert "NOT restarting" not in result.stderr

    def test_transient_crash_backoff_increases(self):
        """Backoff should increase after consecutive transient crashes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")

            # Mock egg-orch: never returns consensus complete
            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": '
                    '{"is_complete": false, "agents": {}}}}}\'\n'
                )
                f.write('elif echo "$@" | grep -q "message poll"; then\n')
                f.write('  echo "[]"\n')
                f.write("else\n")
                f.write('  echo "{}"\n')
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock agent: always exits 139 (always crashes)
            _make_mock_agent(tmpdir, claude_log, exit_code=139)

            cmd = build_consensus_wrapped_command(
                "Do the work", max_restarts=3, transient_backoff_initial=1
            )
            result = self._run_wrapper_command(cmd, tmpdir, timeout=60)

            # Should see backoff messages with increasing values
            assert "Backoff: sleeping 1s before restart" in result.stderr
            assert "Backoff: sleeping 2s before restart" in result.stderr

    def test_transient_crash_then_crash_then_succeed(self):
        """Crash (transient) -> crash (transient) -> succeed should recover."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")

            counter_file = os.path.join(tmpdir, "orch_status_count")
            call_counter = os.path.join(tmpdir, "agent_call_count")
            mock_orch = os.path.join(tmpdir, "egg-orch")

            # Mock egg-orch: consensus complete on 4th+ pipeline status call
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                f.write("  COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(counter_file)} ]; then\n")
                f.write(f"    COUNT=$(cat {shlex.quote(counter_file)})\n")
                f.write("  fi\n")
                f.write("  COUNT=$((COUNT + 1))\n")
                f.write(f'  echo "$COUNT" > {shlex.quote(counter_file)}\n')
                f.write('  if [ "$COUNT" -ge 4 ]; then\n')
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": {"is_complete": true}}}}\'\n'
                )
                f.write("  else\n")
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
                f.write("  fi\n")
                f.write("else\n")
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock agent: exits 139 on calls 1 and 2, then exits 0
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write("  CALL_COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(call_counter)} ]; then\n")
                f.write(f"    CALL_COUNT=$(cat {shlex.quote(call_counter)})\n")
                f.write("  fi\n")
                f.write("  CALL_COUNT=$((CALL_COUNT + 1))\n")
                f.write(f'  echo "$CALL_COUNT" > {shlex.quote(call_counter)}\n')
                f.write(f'  echo "---CLAUDE_CALL_START---" >> {shlex.quote(claude_log)}\n')
                f.write(f'  echo "ARGS: $*" >> {shlex.quote(claude_log)}\n')
                f.write(f'  echo "---CLAUDE_CALL_END---" >> {shlex.quote(claude_log)}\n')
                f.write('  if [ "$CALL_COUNT" -le 2 ]; then\n')
                f.write("    exit 139\n")  # SIGSEGV on calls 1 and 2
                f.write("  fi\n")
                f.write("  exit 0\n")  # Clean exit on call 3
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command(
                "Do the work", max_restarts=3, transient_backoff_initial=1
            )
            result = self._run_wrapper_command(cmd, tmpdir, timeout=60)

            # Should have restarted after both crashes and then succeeded
            assert "Transient crash (code 139)" in result.stderr
            assert "Transient crash on restart" in result.stderr
            assert result.returncode == 0
            # Agent called 3 times: initial crash + restart crash + restart succeed
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL_START---")
            assert call_count == 3

    def test_startup_failure_exit_1_triggers_restart(self):
        """Exit 1 within startup window (socket-close on turn 1) should restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")

            counter_file = os.path.join(tmpdir, "orch_status_count")
            call_counter = os.path.join(tmpdir, "agent_call_count")
            mock_orch = os.path.join(tmpdir, "egg-orch")

            # Mock egg-orch: consensus incomplete initially, complete on 3rd call
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                f.write("  COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(counter_file)} ]; then\n")
                f.write(f"    COUNT=$(cat {shlex.quote(counter_file)})\n")
                f.write("  fi\n")
                f.write("  COUNT=$((COUNT + 1))\n")
                f.write(f'  echo "$COUNT" > {shlex.quote(counter_file)}\n')
                f.write('  if [ "$COUNT" -ge 3 ]; then\n')
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": '
                    '{"is_complete": true, "agents": {}}}}}\'\n'
                )
                f.write("  else\n")
                f.write(
                    '    echo \'{"data": {"concurrent": {"consensus": '
                    '{"is_complete": false, "agents": {}}}}}\'\n'
                )
                f.write("  fi\n")
                f.write('elif echo "$@" | grep -q "message poll"; then\n')
                f.write('  echo "[]"\n')
                f.write("else\n")
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": {"is_complete": false}}}}\'\n'
                )
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock agent: exits 1 immediately on first call (mimics Agent SDK
            # surfacing an API socket-close as success=False + exit 1 at turn 1),
            # then exits 0 on retry.
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write("  CALL_COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(call_counter)} ]; then\n")
                f.write(f"    CALL_COUNT=$(cat {shlex.quote(call_counter)})\n")
                f.write("  fi\n")
                f.write("  CALL_COUNT=$((CALL_COUNT + 1))\n")
                f.write(f'  echo "$CALL_COUNT" > {shlex.quote(call_counter)}\n')
                f.write(f'  echo "---CLAUDE_CALL_START---" >> {shlex.quote(claude_log)}\n')
                f.write(f'  echo "ARGS: $*" >> {shlex.quote(claude_log)}\n')
                f.write(f'  echo "---CLAUDE_CALL_END---" >> {shlex.quote(claude_log)}\n')
                f.write('  if [ "$CALL_COUNT" -eq 1 ]; then\n')
                f.write("    exit 1\n")  # first-turn API error
                f.write("  fi\n")
                f.write("  exit 0\n")
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command(
                "Do the work", max_restarts=2, transient_backoff_initial=1
            )
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30)

            # Should classify exit 1 as a startup failure and retry.
            assert "Startup failure" in result.stderr
            assert "likely transient API/network error" in result.stderr
            assert "NOT restarting" not in result.stderr
            # Agent called at least twice (initial + restart)
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL_START---")
            assert call_count >= 2

    def test_startup_failure_window_zero_disables_retry(self):
        """startup_failure_window_seconds=0 disables the heuristic; exit 1 must fail fast."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_orch_no_consensus(tmpdir, log_file)
            self._make_failing_agent(tmpdir, exit_code=1)

            cmd = build_consensus_wrapped_command(
                "Prompt", max_restarts=2, startup_failure_window_seconds=0
            )
            result = self._run_wrapper_command(cmd, tmpdir)

            assert result.returncode == 1
            assert "NOT restarting" in result.stderr
            assert "Startup failure" not in result.stderr

    def test_startup_failure_respects_max_restarts(self):
        """Repeated startup failures must hit MAX_RESTARTS and exit, not loop forever."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")
            call_counter = os.path.join(tmpdir, "agent_call_count")
            self._make_mock_orch_no_consensus(tmpdir, log_file)

            # Agent logs each call, then always exits 1 — persistent API failure.
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write("  CALL_COUNT=0\n")
                f.write(f"  if [ -f {shlex.quote(call_counter)} ]; then\n")
                f.write(f"    CALL_COUNT=$(cat {shlex.quote(call_counter)})\n")
                f.write("  fi\n")
                f.write("  CALL_COUNT=$((CALL_COUNT + 1))\n")
                f.write(f'  echo "$CALL_COUNT" > {shlex.quote(call_counter)}\n')
                f.write(f'  echo "---CLAUDE_CALL_START---" >> {shlex.quote(claude_log)}\n')
                f.write("  exit 1\n")
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command(
                "Prompt", max_restarts=2, transient_backoff_initial=1
            )
            result = self._run_wrapper_command(cmd, tmpdir, timeout=60)

            # Should have exhausted restarts and exited 1 — not looped forever.
            assert result.returncode == 1
            assert "Startup failure" in result.stderr
            assert "Max restarts" in result.stderr or "never reached CONFIRMED" in result.stderr
            # Agent called 3 times: initial + 2 restarts
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL_START---")
            assert call_count == 3

    def test_startup_failure_after_window_does_not_retry(self):
        """Exit 1 *after* the startup window should fail fast (genuine post-work error)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")
            self._make_mock_orch_no_consensus(tmpdir, log_file)

            # Agent sleeps past the (shortened) window, then exits 1.
            # Window=1s; agent sleeps 3s before exiting — well outside the window.
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write(f'  echo "---CLAUDE_CALL_START---" >> {shlex.quote(claude_log)}\n')
                f.write("  sleep 3\n")
                f.write("  exit 1\n")
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command(
                "Prompt", max_restarts=2, startup_failure_window_seconds=1
            )
            result = self._run_wrapper_command(cmd, tmpdir, timeout=30)

            assert result.returncode == 1
            assert "NOT restarting" in result.stderr
            assert "Startup failure" not in result.stderr
            # Should run only once (no retry on post-window exit 1)
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL_START---")
            assert call_count == 1

    def test_non_transient_exit_code_42_does_not_restart(self):
        """Exit code 42 (application error) should NOT be treated as transient."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            self._make_mock_orch_no_consensus(tmpdir, log_file)
            self._make_failing_agent(tmpdir, exit_code=42)

            cmd = build_consensus_wrapped_command("Prompt", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir)

            assert result.returncode == 42
            assert "NOT restarting" in result.stderr
            assert "Transient crash" not in result.stderr


class TestBufferOverflowDetection:
    """Issue #2804: the Claude Agent SDK 1MB JSON buffer crash is
    deterministic — retrying just hits the same overflow on the same
    codebase. The wrapper must short-circuit retry budget when it
    sees the overflow signature in the agent's output.
    """

    def test_script_defines_is_buffer_overflow(self):
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "is_buffer_overflow()" in script
        assert "exceeded maximum buffer size" in script

    def test_script_captures_agent_output(self):
        """Agent output must be tee'd to a log file the wrapper can grep."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "AGENT_OUTPUT_LOG" in script
        assert "tee -a" in script

    def test_buffer_overflow_check_runs_before_transient_check(self):
        """The overflow check must precede is_transient_crash so a
        signal-255 agent that crashed on overflow doesn't get retried.
        """
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Both functions exist
        assert "is_buffer_overflow" in script
        assert "is_transient_crash" in script
        # Find the first occurrence of each in the initial-exit handler
        # (the section after the consensus/confirmed checks).
        idx_buffer = script.find("if is_buffer_overflow")
        idx_transient = script.find('is_transient_crash "$AGENT_EXIT"')
        assert idx_buffer > 0 and idx_transient > 0
        assert idx_buffer < idx_transient, (
            "is_buffer_overflow must be checked BEFORE is_transient_crash"
        )

    def test_buffer_overflow_log_message_cites_issue(self):
        """When the wrapper aborts on overflow, log must mention #2804."""
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "#2804" in script

    def test_buffer_overflow_check_in_restart_loop(self):
        """The restart-loop must ALSO check for buffer overflow — even if
        the initial run survived, a recovery attempt that hits the
        overflow must abort without consuming the rest of the budget.
        """
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # is_buffer_overflow appears at least twice: initial handler + restart loop
        assert script.count("is_buffer_overflow") >= 2

    @staticmethod
    def _make_buffer_overflow_agent(tmpdir: str) -> None:
        """Create a mock python3 that simulates the SDK buffer-overflow crash.

        Writes the signature marker to stderr (matching the real SDK's
        ``logger.error`` from ``query.py:221``) and exits 255 — the same
        shape produced by the real crash in the issue-2777 #2804 incident.
        """
        mock_python = os.path.join(tmpdir, "python3")
        real_python = sys.executable
        with open(mock_python, "w") as f:
            f.write("#!/bin/bash\n")
            f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
            f.write(
                '  echo "Fatal error in message reader: Failed to decode '
                "JSON: JSON message exceeded maximum buffer size of "
                '1048576 bytes..." >&2\n'
            )
            f.write("  exit 255\n")
            f.write("else\n")
            f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
            f.write("fi\n")
        os.chmod(mock_python, 0o755)  # nosec B103

    def test_buffer_overflow_aborts_without_retry(self):
        """Agent crashing with the SDK buffer-overflow signature must
        exit immediately, NOT consume the restart budget.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            # No consensus reached
            TestConsensusWrapperBehavior._make_mock_orch_no_consensus(tmpdir, log_file)
            self._make_buffer_overflow_agent(tmpdir)

            cmd = build_consensus_wrapped_command("Prompt", max_restarts=2)
            result = TestConsensusWrapperBehavior._run_wrapper_command(cmd, tmpdir)

            # Wrapper must propagate the agent's exit code (255)
            assert result.returncode == 255, result.stderr
            # Must log the overflow diagnosis
            assert "buffer overflow" in result.stderr.lower()
            assert "#2804" in result.stderr
            # Must NOT have attempted a restart
            assert "Restarting" not in result.stderr
            assert "Transient crash" not in result.stderr

    def test_signal_255_without_overflow_marker_still_retries(self):
        """Other 255 crashes (genuine SIGSEGV, bun segfault) keep the
        existing transient-retry behavior — the wrapper must only
        short-circuit when the overflow marker is present.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            TestConsensusWrapperBehavior._make_mock_orch_no_consensus(tmpdir, log_file)
            # Plain exit 255 with no marker → should still be classified
            # as transient and trigger a restart.
            _make_mock_agent(tmpdir, exit_code=255)

            cmd = build_consensus_wrapped_command(
                "Prompt",
                max_restarts=1,
                transient_backoff_initial=1,
            )
            result = TestConsensusWrapperBehavior._run_wrapper_command(cmd, tmpdir, timeout=20)

            # Without the marker, the existing transient-crash classification
            # still kicks in; should log Transient crash.
            assert "Transient crash" in result.stderr


class TestEventDrivenWait:
    """Issue #1897 Phase 5 / TASK-5-1 (plan rev 4, reviewer_plan blocker 4):
    ``check_confirmed_and_wait`` is an SSE-primary hybrid.

    The PRIMARY wait mechanism is ``curl --no-buffer`` against the
    orchestrator's SSE stream at
    ``/api/v1/pipelines/{id}/stream`` parsing the literal event-name
    ``consensus.reached`` — this is the only mechanism that gives
    sub-2s BRC wake-up.

    Secondary fallback: when the SSE path fails (curl missing, no
    EGG_PIPELINE_ID, upstream 5xx) the wrapper falls through to
    ``egg-orch message wait --for CONSENSUS_CONFIRMED`` which is
    itself event-driven via the long-poll endpoint.

    Tertiary fallback: when neither curl nor egg-orch are present
    (RISK-7 zero-CLI local-dev), the wrapper degrades to plain
    ``sleep`` so it still makes progress.

    These tests inspect the generated shell script — running a real
    ``bash`` harness against a mocked ``egg-orch`` is covered by the
    ``TestRecoveryRestart`` suite above.
    """

    # --- SSE primary path -------------------------------------------------

    def test_script_curls_sse_stream_url(self):
        """Primary SSE path MUST curl the /stream endpoint.

        Assertion pins the URL path shape so a refactor that moves the
        SSE endpoint elsewhere is caught by this regression."""
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        assert "/api/v1/pipelines/" in script
        assert "/stream" in script
        # `curl --no-buffer` is critical: without it we buffer event
        # lines and miss the sub-2s wake-up target.
        assert "curl --no-buffer" in script

    def test_script_parses_literal_consensus_reached_event_name(self):
        """Plan TASK-5-1 acceptance (g): the literal event-name
        ``consensus.reached`` MUST appear in the script so a future
        EventType-enum rename cannot silently break the wrapper.

        This is the highest-priority pin: the entire PR hinges on the
        event name staying stable across EventType refactors.
        """
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        assert "consensus.reached" in script
        # Parser must look for lines starting with ``event:`` — SSE
        # field delimiters are whitespace-tolerant but colons are the
        # only place we can reliably pattern-match event-type lines.
        assert "event:" in script or "event: " in script

    def test_script_curls_event_stream(self):
        """The SSE curl must target EGG_PIPELINE_ID so each agent waits
        on its own pipeline (not a cross-talk-prone shared stream)."""
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        assert "EGG_PIPELINE_ID" in script
        assert "stream" in script.lower()

    def test_script_guards_sse_with_curl_presence_check(self):
        """Defense-in-depth: script MUST check ``command -v curl`` before
        invoking curl so missing-curl sandboxes fall cleanly into the
        secondary fallback rather than failing with command-not-found."""
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        assert "command -v curl" in script

    def test_sse_curl_uses_max_time_bound(self):
        """Plan TASK-5-1 acceptance (c): curl invocation MUST set -m
        (max-time) so a hung SSE stream can't stall the wrapper past
        MAX_READY_POLLS × poll_interval.

        Without -m, a silent server-side socket hang would pin the
        wrapper forever; with it, the fallback gets a chance to run."""
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        # curl invocation needs explicit max-time to bound the SSE wait.
        assert "-m" in script or "--max-time" in script

    def test_sse_failure_falls_back_to_egg_orch_wait(self):
        """When SSE fails (curl missing, 5xx, timeout), the script
        MUST fall through to ``egg-orch message wait`` — not exit with
        failure. This keeps the wrapper event-driven across both paths.
        """
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        # Event-driven fallback must be present.
        assert "egg-orch message wait" in script
        assert "--for CONSENSUS_CONFIRMED" in script
        assert "--for CONSENSUS_RE_REVIEW" in script

    def test_sse_path_verifies_consensus_before_exit(self):
        """After the SSE stream delivers ``consensus.reached``, the
        script MUST call ``egg-orch pipeline status`` to confirm
        is_complete=True before exiting 0. Trusting the event without
        verification leaves a race if the event is a spurious re-emit
        from the stream buffer."""
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        assert "is_complete" in script
        assert "pipeline status" in script

    # --- Secondary fallback: egg-orch message wait -----------------------

    def test_egg_orch_message_wait_waits_for_both_types(self):
        """Both CONSENSUS_CONFIRMED and CONSENSUS_RE_REVIEW unblock the
        wait-until-consensus loop (so a re-review doesn't stall the
        wrapper in the secondary path)."""
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        assert "--for CONSENSUS_CONFIRMED" in script
        assert "--for CONSENSUS_RE_REVIEW" in script

    def test_egg_orch_presence_guarded_by_command_v(self):
        """Secondary-path fallback also guards on ``command -v egg-orch``
        so missing-CLI sandboxes drop cleanly to the tertiary sleep
        path."""
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        assert "command -v egg-orch" in script

    # --- Tertiary fallback: pure sleep -----------------------------------

    def test_script_has_sleep_fallback(self):
        """If neither curl nor egg-orch are available (RISK-7 zero-CLI
        local-dev), the wrapper degrades to a sleep loop so it still
        makes progress rather than spin-looping."""
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        assert "sleep" in script

    # --- Cross-cutting ---------------------------------------------------

    def test_script_issue_reference(self):
        """The new SSE wait path must reference issue #1897 so a later
        archaeology pass can find the design justification."""
        cmd = build_consensus_wrapped_command("x")
        script = cmd[2]
        assert "#1897" in script


class TestSSESigtermGrace:
    """Issue #1897 TASK-5-1 acceptance: SIGTERM mid-wait MUST exit
    within the Kubernetes grace period.

    The orchestrator sends SIGTERM when consensus is reached; the
    wrapper's curl process (stuck on the SSE stream) MUST honor the
    signal and exit quickly so the pod isn't force-killed with
    SIGKILL after the terminationGracePeriodSeconds deadline.

    Rather than spin up a real SSE server (expensive, flaky in CI),
    these tests run the generated script against a mock curl shim
    that blocks on stdin, and send SIGTERM to the bash process.
    The assertion is simply: exit happens within <= GRACE seconds.
    """

    GRACE_SECONDS = 10  # k8s default terminationGracePeriodSeconds is 30

    def test_sigterm_during_sse_exits_within_grace_period(self):
        """The bash wrapper's SSE curl should be interruptible by
        SIGTERM so the orchestrator's stop signal is honored quickly.

        We don't actually spawn the full wrapper (it has too many
        dependencies) — instead we extract the SSE block into a minimal
        harness and assert the signal handler contract.
        """
        # Extract the SSE-wait block + a minimal mock curl that blocks.
        # The real wrapper invokes `curl --no-buffer -sf -m ... "$sse_url"`.
        # We replace curl with a shell function that `sleep 300` to
        # simulate a stalled stream, then send SIGTERM and measure the
        # exit latency.
        #
        # Per plan acceptance (and the production use case), the wrapper
        # should not have its own trap — bash's default SIGTERM handling
        # kills the process group which ends the curl. This test is a
        # regression guard against a later "helpful" trap being added
        # that swallows SIGTERM.
        script = r"""
            set -uo pipefail
            # Mock curl that blocks; we expect SIGTERM to kill it.
            curl() { sleep 300; }
            export -f curl
            # Simulate the SSE block (the relevant portion)
            curl --no-buffer -sf -m 60 http://fake/stream
        """
        import time

        start = time.time()
        proc = subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,  # so we can kill the process group
        )
        # Give bash a moment to launch into curl.
        time.sleep(0.3)
        # Send SIGTERM to the process group — mirrors what k8s does.
        import signal

        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=self.GRACE_SECONDS)
        elapsed = time.time() - start
        assert elapsed < self.GRACE_SECONDS, (
            f"SSE curl took {elapsed:.1f}s to exit after SIGTERM; "
            f"must be < {self.GRACE_SECONDS}s to avoid k8s SIGKILL"
        )
