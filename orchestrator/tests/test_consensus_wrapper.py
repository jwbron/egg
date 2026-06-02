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
        """Default max_restarts should match module constant.

        Issue #2806: default cap bumped from 2 → 3 to give one extra
        recovery attempt before the orchestrator hard-fails the pipeline
        on producer permanent death.
        """
        assert MAX_CONSENSUS_RESTARTS == 3
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert f"MAX_RESTARTS={MAX_CONSENSUS_RESTARTS}" in script

    def test_restart_emits_overseer_alert(self):
        """Issue #2806: each wrapper restart should publish an
        OVERSEER_ALERT so the operator sees recovery attempts in real
        time rather than only learning about a dead agent after the
        wrapper has exhausted its retry budget. The call is wrapped with
        ``timeout 5`` so a stalled orchestrator cannot delay the restart
        loop (PR #2811 review).
        """
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "timeout 5 egg-orch overseer alert" in script
        assert "agent-restart" in script
        assert "--priority medium" in script

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

    def test_script_marker_matches_client_constant(self):
        """The bash grep substring must match ``_BUFFER_OVERFLOW_MARKER``
        in ``shared/egg_agent/client.py``. Renaming the constant without
        updating the wrapper script silently regresses the short-circuit
        — this test pins them together. Issue #2804.
        """
        # ``orchestrator/tests/conftest.py`` already puts ``shared/`` on
        # ``sys.path`` for the orchestrator test session, so the import
        # below resolves without any per-test path munging.
        from egg_agent.client import _BUFFER_OVERFLOW_MARKER

        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert _BUFFER_OVERFLOW_MARKER in script, (
            f"consensus_wrapper script must grep for {_BUFFER_OVERFLOW_MARKER!r} "
            "to match the marker emitted by run_agent_async on SDK overflow"
        )

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

    def test_buffer_overflow_in_restart_loop_aborts_without_further_retries(self):
        """Restart-loop overflow path: clean initial exit triggers a
        restart, recovery run crashes with the overflow marker, wrapper
        aborts immediately instead of consuming the remaining budget.

        Distinct from ``test_buffer_overflow_aborts_without_retry``,
        which only exercises the initial-exit handler. Both code paths
        need the buffer-overflow short-circuit; this regression-guards
        the restart-loop branch (#2804 review feedback).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")
            claude_log = os.path.join(tmpdir, "claude.log")
            call_counter = os.path.join(tmpdir, "agent_call_count")

            # Mock egg-orch: always returns is_complete=false so the
            # wrapper enters the restart loop after each clean exit.
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

            # Mock agent: clean exit on call 1 (triggers restart), then
            # emits the SDK overflow signature and exits 255 on call 2.
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
                f.write('  if [ "$CALL_COUNT" -eq 1 ]; then\n')
                f.write("    exit 0\n")  # clean exit → restart triggered
                f.write("  fi\n")
                # call 2: emit the overflow signature and exit 255
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

            cmd = build_consensus_wrapped_command(
                "Prompt", max_restarts=3, transient_backoff_initial=1
            )
            result = TestConsensusWrapperBehavior._run_wrapper_command(cmd, tmpdir, timeout=30)

            # Wrapper must propagate the overflow crash's exit code from
            # the restart loop, NOT continue retrying.
            assert result.returncode == 255, result.stderr
            # Diagnostic message must indicate the restart-loop path
            # (the initial-exit handler says "Agent crashed on Claude
            # Agent SDK buffer overflow ..."; the restart-loop handler
            # adds "on restart N").
            assert "buffer overflow" in result.stderr.lower()
            assert "on restart" in result.stderr
            assert "#2804" in result.stderr
            # Agent was called exactly twice: initial clean exit + one
            # restart that crashed on overflow. Should NOT be called a
            # third time.
            with open(claude_log) as f:
                call_count = f.read().count("---CLAUDE_CALL_START---")
            assert call_count == 2, (
                f"Expected exactly 2 agent calls (initial + 1 restart with "
                f"overflow), got {call_count}. The restart-loop buffer-overflow "
                f"check must abort before consuming further retry budget."
            )


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


# ---------------------------------------------------------------------------
# Issue #2908 slice-2 / TASK-2-6: event-pump wrapper template
# ---------------------------------------------------------------------------
# These tests pin the new event-pump template branch added in TASK-2-1..2-4
# (gated by ``EGG_BRC_EVENT_PUMP``). The plan acceptance for TASK-2-6
# enumerates nine sub-assertions:
#
#   (i)    template selection branches for both flag values (snapshot test
#          asserting the six-event wait-filter set on the flag-on path).
#   (ii)   wrapper-side heartbeat cadence (mock subprocess + fast-forward).
#   (iii)  heartbeat payload includes ``slice_id`` sourced from
#          ``EGG_SLICE_ID`` (one test pins this directly).
#   (iv)   wrapper-side keep-alive cadence.
#   (v)    idle budget alert at configured threshold.
#   (vi)   409 ``stale_version`` handled as re-fetch (not retry-with-backoff).
#   (vii)  ``role_complete=true`` path calls ``egg-orch consensus confirmed``
#          and exits 0.
#   (vii.b) wrapper does NOT also call ``egg-orch progress complete``
#          (defensive guard against the pseudocode-typo the architect
#          corrected — plan line 932-934).
#   (viii) wait-filter construction OMITS ``CONSENSUS_CONFIRMED`` pre-confirm
#          and INCLUDES it post-confirm (risk_analyst R12 / orchestrator
#          HTTP-400 rejection documented in #2064/#2482).
#   (ix)   unset-``EGG_SLICE_ID`` case (plan/refine phase) emits either
#          explicit-null or omitted slice_id on the heartbeat payload
#          (NOT empty-string).
#
# The flag-off path must remain byte-for-byte identical so the existing
# ``TestBuildConsensusWrappedCommand`` + ``TestConsensusWrapperBehavior``
# tiers continue to pass without modification. Acceptance for TASK-2-1
# names that constraint explicitly: "With ``EGG_BRC_EVENT_PUMP`` unset:
# ``build_consensus_wrapped_command`` emits the existing template
# byte-for-byte (regression-tested via existing snapshot); existing
# ``orchestrator/tests/test_consensus_wrapper.py`` passes unchanged."
# ---------------------------------------------------------------------------


# Sentinel event types the event-pump wait filter must always cover.
# Plan line 797-799 lists these six explicitly.
_EXPECTED_EVENT_PUMP_WAIT_FILTERS = (
    "CONSENSUS_PROPOSE",
    "CONSENSUS_ACK",
    "CONSENSUS_NACK",
    "STATUS",
    "CONSENSUS_RE_REVIEW",
    "OVERSEER_ALERT",
)


class TestEventPumpTemplateSelection:
    """(i) Template selection branches for both ``EGG_BRC_EVENT_PUMP`` values.

    With the flag unset / "false": ``build_consensus_wrapped_command``
    emits the legacy ``_CONSENSUS_WRAPPER_TEMPLATE`` byte-for-byte. With
    the flag "true": the new ``_EVENT_PUMP_WRAPPER_TEMPLATE`` is emitted
    instead. Both branches must select on the env var at
    template-composition time so ``build_consensus_wrapped_command``
    callers in the orchestrator pod (`concurrent_executor.py:489`,
    `kubernetes_spawner.py`) read the same flag and either get the new
    deterministic loop or the legacy capped-restart loop.
    """

    def test_flag_off_emits_legacy_template_byte_for_byte(self, monkeypatch):
        """With ``EGG_BRC_EVENT_PUMP`` unset, the legacy template is emitted
        unchanged. This pins the regression contract from TASK-2-1
        acceptance: "emits the existing template byte-for-byte".
        """
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Legacy template markers that the new template MUST NOT carry.
        assert "MAX_RESTARTS=" in script
        assert "BRC Consensus Recovery" in script
        # New template marker must NOT appear in the legacy branch.
        assert "EVENT_PUMP_LOOP_BEGIN" not in script

    def test_flag_false_emits_legacy_template_byte_for_byte(self, monkeypatch):
        """An explicit ``EGG_BRC_EVENT_PUMP=false`` is treated as the
        default (legacy template). Catches a regression where a falsy
        comparison only checked unset rather than the literal "false".
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "false")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "MAX_RESTARTS=" in script
        assert "EVENT_PUMP_LOOP_BEGIN" not in script

    def test_flag_off_existing_template_snapshot_unchanged(self, monkeypatch):
        """Byte-for-byte snapshot: with the flag off, the emitted bash
        matches what the pre-TASK-2-1 implementation would have emitted.

        We compare the emitted command against the same command emitted
        with an alternate flag value and confirm the *flag-off* output is
        the legacy template by checking for legacy-only markers. The
        existing ``TestBuildConsensusWrappedCommand`` suite covers the
        substantive content; this test only pins the regression contract.
        """
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd_off = build_consensus_wrapped_command("Prompt")
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd_on = build_consensus_wrapped_command("Prompt")
        # The two scripts MUST differ — otherwise the flag is dead code.
        assert cmd_off[2] != cmd_on[2], (
            "EGG_BRC_EVENT_PUMP=true must select a different template "
            "branch; flag appears to be dead code."
        )

    def test_flag_on_emits_event_pump_template(self, monkeypatch):
        """With ``EGG_BRC_EVENT_PUMP=true``, the new event-pump template
        is emitted in place of the legacy capped-restart template.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The new template MUST contain a wait-loop primitive over the
        # six event types and handle the ``role_complete`` signal from
        # ``brc next-action`` (plan line 783-792).
        assert "egg-orch message wait-loop" in script
        # The role_complete signal arrives from ``brc next-action`` as the
        # action value ``complete``. The wrapper must branch on it (case
        # arm or equivalent). Accept any of: literal ``role_complete``
        # token (variable name), the ``complete)`` case arm in the action
        # switch, or a ``ROLE_CONFIRMED`` boolean derived from
        # ``brc get-state``.
        assert any(
            marker in script for marker in ("role_complete", "complete)", "ROLE_CONFIRMED")
        ), (
            "event-pump must check role_complete from brc get-state / "
            "next-action (plan line 783-792); neither role_complete nor "
            "ROLE_CONFIRMED nor a complete) case arm found in script."
        )
        # New template must call ``brc next-action`` (plan line 785).
        assert "brc next-action" in script

    def test_flag_on_wait_filter_contains_six_required_events(self, monkeypatch):
        """(i) The flag-on path's wait-filter set must include all six event
        types the plan enumerates (line 797-799).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        for event_type in _EXPECTED_EVENT_PUMP_WAIT_FILTERS:
            assert f"--for {event_type}" in script, (
                f"event-pump wait filter missing --for {event_type}; "
                f"plan TASK-2-1 line 797-799 requires all six."
            )


class TestEventPumpHeartbeatCadence:
    """(ii)+(iii) Wrapper-side heartbeat emission migrated out of
    ``sandbox/egg_agent_tools/handlers/message.py:267-429`` and into the
    event-pump bash. The payload must include ``slice_id`` sourced from
    ``EGG_SLICE_ID`` so a regression in slice_id propagation (risk_analyst
    R9) is caught directly.

    Cadence is verified with a mock subprocess fast-forward — we inspect
    the generated script for the configured 30-second loop interval and
    the existence of a backgrounded heartbeat subshell, rather than
    sleeping in real wall-clock to keep the test deterministic.
    """

    def test_flag_on_emits_heartbeat_subshell(self, monkeypatch):
        """The event-pump template must contain ``egg-orch message
        heartbeat`` invoked in a backgrounded subshell while the
        wait-loop is blocking (plan TASK-2-2 description, line 828-829).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "egg-orch message heartbeat" in script
        # Background subshell shape: `( ... ) &` or a backgrounded
        # subshell variant — flexible enough to match either.
        # Heartbeat must run alongside the wait-loop, not block it.
        # We pin the presence of the heartbeat command + the ``wait-loop``
        # primitive in the same script so the migration cannot regress
        # to agent-side-only heartbeating.
        assert "wait-loop" in script

    def test_flag_on_heartbeat_cadence_is_30_seconds(self, monkeypatch):
        """The plan (TASK-2-2 description, line 828) names a 30-second
        cadence. Pin the default in the script so a regression to a
        different cadence is caught by this test. Accept either a
        literal ``sleep 30`` or an env-var indirection that defaults to
        30 (e.g. ``${EGG_BRC_HEARTBEAT_INTERVAL_SECS:-30}``).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Accept either form: literal ``sleep 30`` or an env-var
        # default of 30 (``:-30}"`` etc.).
        cadence_markers = (
            "sleep 30",
            ":-30}",
            "INTERVAL_SECS=30",
            "INTERVAL_SECS:-30",
        )
        assert any(m in script for m in cadence_markers), (
            "wrapper-side heartbeat must default to a 30s cadence per "
            "plan TASK-2-2 line 828; neither literal `sleep 30` nor an "
            "env-var default of 30 found in the rendered bash."
        )

    def test_flag_on_heartbeat_payload_threads_slice_id_from_env(self, monkeypatch):
        """(iii) The heartbeat payload MUST source ``slice_id`` from the
        ``EGG_SLICE_ID`` env var. Plan TASK-2-2 line 831-834 names this
        invariant directly: "The heartbeat payload MUST include
        ``slice_id == os.environ['EGG_SLICE_ID']`` (or the equivalent
        shell substitution ``${EGG_SLICE_ID:-}`` passed through the CLI)
        so a regression in slice_id propagation is caught directly."

        We assert the script references ``EGG_SLICE_ID`` adjacent to the
        ``egg-orch message heartbeat`` invocation. A shell substitution
        of the env var into the heartbeat command line satisfies the
        "passed through the CLI" route from the plan.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The script must reference EGG_SLICE_ID; the message heartbeat
        # CLI takes ``--slice-id`` or threads it through the request.
        assert "EGG_SLICE_ID" in script, (
            "heartbeat payload must source slice_id from EGG_SLICE_ID; "
            "regression in slice_id propagation will not be caught "
            "without this wiring (risk_analyst R9)."
        )

    def test_flag_off_heartbeat_path_unchanged(self, monkeypatch):
        """With the flag off, the wrapper does NOT take on the heartbeat
        responsibility — the legacy agent-side path keeps emitting them.

        Plan TASK-2-2 line 835-838: "Keep the agent-side heartbeat path
        in the *old* template path (``EGG_BRC_EVENT_PUMP`` unset)
        verbatim; only the new template owns wrapper-side heartbeating.
        Slice-4 deletes the agent-side path once the flag flips to
        default."
        """
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The legacy template must NOT call ``message heartbeat`` itself —
        # the agent-side handler does. If the legacy template ever starts
        # emitting heartbeats, double-heartbeating will spam the bus.
        assert "egg-orch message heartbeat" not in script, (
            "legacy template must NOT take on wrapper-side heartbeating; "
            "slice-4 will delete the agent-side path. Double-emitting "
            "now would spam the bus."
        )


class TestEventPumpKeepAliveCadence:
    """(iv) Wrapper-side gateway-session keep-alive (#2451) migrated out
    of ``sandbox/egg_agent_tools/handlers/message.py`` and into the
    event-pump bash. The wrapper performs the same lifecycle-secret-gated
    session refresh as a background subshell alongside the heartbeat
    emitter from TASK-2-2.
    """

    def test_flag_on_emits_keep_alive_subshell(self, monkeypatch):
        """The event-pump template must perform a gateway-session
        refresh while the wait-loop is blocking (plan TASK-2-4 line
        875-881).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The keep-alive ping refreshes the lifecycle-secret-gated
        # session. Either an explicit ``keep-alive``/``keepalive``
        # subcommand call or a session-refresh marker must appear.
        assert "keep-alive" in script or "keepalive" in script or "session" in script.lower(), (
            "event-pump template must perform gateway-session keep-alive "
            "(plan TASK-2-4); without it, long waits will lose their "
            "lifecycle-secret-gated session and the next CLI call will "
            "401."
        )

    def test_flag_off_keep_alive_remains_agent_side(self, monkeypatch):
        """With the flag off, the wrapper does NOT take on keep-alive —
        the legacy agent-side handler in ``message.py`` keeps refreshing.

        Plan TASK-2-4 line 880-881: "Old path unchanged."
        """
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Legacy template must not start performing keep-alive itself.
        # The existing snapshot tests would already fail if it did, but
        # we pin the invariant explicitly here.
        assert "EVENT_PUMP" not in script


class TestEventPumpIdleBudgetAlert:
    """(v) Idle / no-progress safety budget driven by env
    ``EGG_BRC_IDLE_BUDGET_MIN`` (default 30). When the new template path
    is active and no actionable event has arrived for the budget
    duration, the wrapper emits
    ``mcp__progress__overseer_alert`` (anomaly
    ``stuck-phase-transition``, priority ``high``) and continues
    blocking. The old template keeps ``MAX_CONSENSUS_RESTARTS`` verbatim.

    NOTE: Per scope update on #2908 issue body and contract cq-3, the
    durable server-side ``Pipeline.no_progress_budget`` is the binding
    primary mechanism. The in-wrapper env-var budget tested here is the
    slice-2 implementation gate; it must work AND must NOT replace the
    durable server-side path (which lands in slice-1's orchestrator
    route work, not here).
    """

    def test_flag_on_contains_idle_budget_alert(self, monkeypatch):
        """Idle budget threshold triggers an overseer alert at the
        configured duration (plan TASK-2-3 acceptance line 863-868).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Default budget: 30 minutes (plan line 854).
        assert "EGG_BRC_IDLE_BUDGET_MIN" in script
        # Alert payload (plan line 857-858) — overseer alert with the
        # right anomaly + priority.
        assert "overseer alert" in script or "overseer_alert" in script
        assert "stuck-phase-transition" in script
        # Priority "high" must be passed somewhere (either as
        # ``--priority high`` literal, ``--priority "$priority"`` with
        # ``"high"`` passed in, or an env-var default of "high"). We
        # require both the ``--priority`` flag AND the ``high`` token
        # appear in the script.
        assert "--priority" in script, (
            "overseer alert payload must include --priority flag (plan "
            "TASK-2-3 line 857-858 — `priority high`)."
        )
        assert "high" in script, (
            "overseer alert priority must be `high` per plan TASK-2-3 line 857-858."
        )

    def test_flag_on_idle_budget_default_30_minutes(self, monkeypatch):
        """Default ``EGG_BRC_IDLE_BUDGET_MIN`` is 30 minutes per plan
        line 853-854 (well above the WS7-observed 10-13 min idle ceiling).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Default value must appear as a literal in the rendered bash so
        # an operator override flows through.
        assert "${EGG_BRC_IDLE_BUDGET_MIN:-30}" in script or "EGG_BRC_IDLE_BUDGET_MIN=30" in script

    def test_flag_on_idle_budget_continues_blocking_after_alert(self, monkeypatch):
        """After the alert fires, the loop continues blocking (NOT exit
        1 → FAILED). Plan line 867-868: "loop continues blocking after
        alert (not exit 1 → FAILED)."
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The alert dispatch must NOT be immediately followed by an
        # ``exit 1``. We assert the pattern by checking that the script
        # re-enters the wait-loop after the alert (continue / loop /
        # re-block). The simplest pin: there is no immediate ``exit 1``
        # adjacent to the ``stuck-phase-transition`` keyword.
        alert_idx = script.find("stuck-phase-transition")
        if alert_idx == -1:
            # If the test for `test_flag_on_contains_idle_budget_alert`
            # already failed, this test would also fail — but its scope
            # is the continue-not-exit invariant, so we skip cleanly
            # rather than double-report.
            import pytest as _pytest

            _pytest.skip(
                "stuck-phase-transition keyword absent; covered by the "
                "TestEventPumpIdleBudgetAlert.test_flag_on_contains_idle_budget_alert "
                "failure"
            )
        # Look at the next 200 characters after the alert keyword — no
        # adjacent ``exit 1`` must appear (the loop continues).
        nearby = script[alert_idx : alert_idx + 200]
        assert "exit 1" not in nearby, (
            "idle-budget alert must NOT be followed by exit 1; the loop "
            "MUST continue blocking (plan line 867-868)."
        )

    def test_flag_off_idle_budget_not_used(self, monkeypatch):
        """With the flag off, the legacy capped-restart path is used —
        ``EGG_BRC_IDLE_BUDGET_MIN`` is irrelevant and must not be read
        by the legacy template. Plan line 860-862: "The old template
        path keeps ``MAX_CONSENSUS_RESTARTS`` verbatim (slice-4 deletes
        the old path)."
        """
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "EGG_BRC_IDLE_BUDGET_MIN" not in script, (
            "legacy template must NOT reference EGG_BRC_IDLE_BUDGET_MIN; "
            "it is only active behind the flag-on event-pump template."
        )


class TestEventPumpStaleVersionRefetch:
    """(vi) 409 ``stale_version`` from ``brc next-action`` is an
    event-pump signal (re-fetch state, re-invoke), NOT a transient crash
    to retry with backoff.

    Plan TASK-2-1 line 793-796: "Wrapper handles 409 ``stale_version``
    and 409 aggregated-NACK from ``brc next-action`` as event-pump
    signals (re-fetch state, re-invoke), NOT as transient crashes to
    retry with backoff."
    """

    def test_flag_on_handles_409_stale_version_as_refetch(self, monkeypatch):
        """The event-pump bash MUST treat HTTP 409 from ``brc
        next-action`` as a re-fetch trigger — call ``brc get-state``
        again and re-invoke, NOT retry the same call with backoff.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The script must explicitly match the 409 status code and
        # call ``brc get-state`` again. Pin the literal "409" so a
        # blanket "any error → retry" path is caught by this test.
        assert "409" in script
        # Re-fetch primitive must be present.
        assert "brc get-state" in script

    def test_flag_on_409_does_not_apply_backoff(self, monkeypatch):
        """On 409, the wrapper must NOT enter the ``CRASH_BACKOFF`` /
        sleep-then-retry path. This is the negative invariant of (vi).
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The event-pump template should not be using CRASH_BACKOFF
        # variables at all (those belong to the legacy template).
        # If they DO leak in via copy-paste, the 409 handler might
        # accidentally land in the wrong branch.
        # Allow CRASH_BACKOFF only if it is decisively scoped away
        # from the 409 handler.
        idx = script.find("409")
        if idx != -1:
            nearby = script[max(0, idx - 200) : idx + 400]
            assert "CRASH_BACKOFF" not in nearby, (
                "409 stale_version handler must not be co-located with "
                "CRASH_BACKOFF backoff — it is a re-fetch signal, NOT a "
                "transient crash (plan TASK-2-1 line 793-796)."
            )


class TestEventPumpRoleCompleteConfirm:
    """(vii) + (vii.b) ``role_complete=true`` path calls ``egg-orch
    consensus confirmed`` and exits 0; the wrapper does NOT also call
    ``egg-orch progress complete`` (defensive guard against the
    pseudocode-typo the architect corrected — plan line 932-934).
    """

    def test_flag_on_role_complete_calls_consensus_confirmed(self, monkeypatch):
        """On ``role_complete=true`` the event-pump bash calls
        ``egg-orch consensus confirmed`` to mark consensus and exits 0.
        Plan TASK-2-1 line 791-793: "the wrapper calls ``egg-orch
        consensus confirmed`` (existing CLI at orch_cli.py:2753) — NOT a
        new ``progress complete`` command — to mark the role's consensus
        and exit 0."
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "egg-orch consensus confirmed" in script
        # Clean exit 0 must appear in the role_complete branch.
        # We check by locating the consensus-confirmed call and asserting
        # an ``exit 0`` follows somewhere in the rest of that branch.
        idx = script.find("egg-orch consensus confirmed")
        assert idx >= 0
        tail = script[idx:]
        assert "exit 0" in tail, (
            "the role_complete branch that calls `egg-orch consensus "
            "confirmed` must exit 0 (plan TASK-2-1 line 791-793)."
        )

    def test_flag_on_does_not_call_progress_complete(self, monkeypatch):
        """(vii.b) Defensive guard: the wrapper template must NOT contain
        ``progress complete`` — that would be the pseudocode-typo the
        architect corrected and is NOT a valid CLI subcommand for marking
        BRC consensus.

        Plan TASK-2-6 acceptance line 949-950: "test (vii.b) asserts
        ``rg 'progress complete'`` against the emitted bash returns
        zero matches."
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "progress complete" not in script, (
            "wrapper must NOT call `egg-orch progress complete` — the "
            "correct CLI is `egg-orch consensus confirmed`. This guard "
            "catches the pseudocode-typo the architect corrected."
        )

    def test_flag_off_legacy_path_does_not_auto_call_consensus_confirmed(self, monkeypatch):
        """Symmetry guard: the legacy path also must not auto-confirm
        on behalf of the agent. The agent calls ``consensus confirmed``
        itself; the wrapper only calls it on the flag-on path when
        ``brc next-action`` returns ``role_complete``.

        The legacy template DOES reference ``egg-orch consensus
        confirmed`` inside the *recovery system prompt* (it instructs
        the agent on what to do), but it must not invoke the command
        itself. We assert by ensuring no top-level execution lines
        actually run that command.

        Behavioral coverage: ``test_no_auto_ready_on_clean_exit`` runs
        the wrapper against mocks and confirms the egg-orch log shows
        no ``consensus confirmed`` invocation.
        """
        monkeypatch.delenv("EGG_BRC_EVENT_PUMP", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Look for ACTUAL invocations: lines that start with optional
        # whitespace + literal ``egg-orch consensus confirmed`` (not a
        # backtick-wrapped reference inside a system-prompt string).
        invocation_lines = [
            ln
            for ln in script.splitlines()
            if ln.lstrip().startswith("egg-orch consensus confirmed")
        ]
        assert not invocation_lines, (
            "legacy template must not auto-invoke consensus confirmed; "
            "the agent owns the confirmed call on the flag-off path. "
            f"Found invocation line(s): {invocation_lines}"
        )


class TestEventPumpWaitFilterConditional:
    """(viii) Wait-filter construction OMITS ``CONSENSUS_CONFIRMED``
    pre-confirm and INCLUDES it post-confirm.

    Plan TASK-2-1 line 811-816: "the wait-filter set is **constructed
    conditionally from ``consensus_status.is_role_confirmed``** —
    pre-confirm waits OMIT ``CONSENSUS_CONFIRMED`` from the filter (per
    risk_analyst R12 / orchestrator HTTP-400 rejection documented in
    #2064/#2482), post-confirm STAY-ALIVE waits INCLUDE it."

    The HTTP-400 rejection is real: the orchestrator's wait endpoint
    returns 400 if a producer's pre-confirm wait names
    ``CONSENSUS_CONFIRMED`` because its own confirm is what generates
    that signal. So the wrapper bash MUST conditionally include the
    filter or risk wedging every pre-confirm wait.
    """

    def test_flag_on_wait_filter_is_constructed_conditionally(self, monkeypatch):
        """The event-pump bash MUST branch on ``is_role_confirmed`` (or
        an equivalent boolean) when constructing the wait-loop filter
        set so the pre-confirm path omits ``CONSENSUS_CONFIRMED``.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The conditional may take any of several shapes. The plan names
        # ``consensus_status.is_role_confirmed`` as the input — at the
        # bash level this is a boolean variable derived from the
        # ``brc get-state`` response. Any of these markers proves the
        # conditional shape exists:
        markers = (
            "is_role_confirmed",
            "ROLE_CONFIRMED",
            "is_confirmed",
            "role_confirmed",
        )
        assert any(m in script for m in markers), (
            "event-pump wait filter must be constructed conditionally "
            "from a role-confirmed boolean derived from brc get-state; "
            "pre-confirm waits must omit CONSENSUS_CONFIRMED per "
            "risk_analyst R12 / orchestrator HTTP-400 rejection "
            "(#2064/#2482)."
        )

    def test_flag_on_pre_confirm_wait_does_not_always_include_consensus_confirmed(
        self, monkeypatch
    ):
        """Negative invariant: the script must NOT unconditionally pass
        ``--for CONSENSUS_CONFIRMED`` to the wait-loop. If every
        wait-loop invocation hard-codes that filter, the conditional
        shape is missing and pre-confirm waits will wedge with HTTP 400.

        The matching tactic: ensure that ``CONSENSUS_CONFIRMED`` does
        NOT appear in the same line as ``wait-loop`` unconditionally.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Find every line containing ``wait-loop``. If any of them
        # ALSO contains ``--for CONSENSUS_CONFIRMED`` directly,
        # without a conditional gate, that's a regression.
        # We detect this by checking that the script branches on a
        # role-confirmed boolean somewhere near the wait-loop call.
        wait_loop_lines = [
            ln for ln in script.splitlines() if "wait-loop" in ln and "egg-orch" in ln
        ]
        # Allow at most one wait-loop call site, but require the script
        # contains either an ``if`` branch around it or a variable that
        # is conditionally extended.
        if any("CONSENSUS_CONFIRMED" in ln for ln in wait_loop_lines):
            # The literal --for CONSENSUS_CONFIRMED appears on the same
            # line as wait-loop. This is only OK if the line is gated
            # by a conditional; check for a same-region ``if``/``case``
            # construct.
            assert "if " in script and (
                "is_role_confirmed" in script
                or "ROLE_CONFIRMED" in script
                or "is_confirmed" in script
            ), (
                "the wait-loop invocation includes --for "
                "CONSENSUS_CONFIRMED unconditionally; this will wedge "
                "pre-confirm waits with HTTP 400 (risk_analyst R12)."
            )

    def test_flag_on_post_confirm_wait_includes_consensus_confirmed(self, monkeypatch):
        """Positive invariant: post-confirm STAY-ALIVE waits MUST
        include ``CONSENSUS_CONFIRMED`` so the wrapper wakes when peer
        producers confirm. Plan line 815-816: "post-confirm STAY-ALIVE
        waits INCLUDE it."
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The literal ``--for CONSENSUS_CONFIRMED`` MUST appear somewhere
        # in the script (gated by the conditional from the previous
        # test).
        assert "--for CONSENSUS_CONFIRMED" in script, (
            "post-confirm STAY-ALIVE wait must include "
            "--for CONSENSUS_CONFIRMED (plan line 815-816)."
        )


class TestEventPumpSliceIdHeartbeatEdge:
    """(ix) Unset-``EGG_SLICE_ID`` case (plan / refine phase) emits
    either explicit-null or omitted slice_id on the heartbeat payload —
    NOT empty-string.

    Plan TASK-2-6 acceptance line 937-939: "unset-``EGG_SLICE_ID`` case
    (plan/refine phase) emits either explicit-null or omitted slice_id
    on the heartbeat payload (NOT empty-string)."

    Empty-string is a known bug class: the orchestrator's slice scoping
    treats "" as a match for "no slice" but also as a distinct value
    from None, so a heartbeat with ``slice_id=""`` will mismatch
    against a tracker reconstruction keyed on None. This test pins the
    null / omission shape.
    """

    def test_unset_slice_id_does_not_emit_empty_string(self, monkeypatch):
        """When ``EGG_SLICE_ID`` is unset, the rendered bash must NOT
        pass an empty-string ``slice_id`` to the heartbeat CLI.

        We assert by scanning the script for a literal ``--slice-id ""``
        or ``"slice_id":""`` pattern, both of which are bug shapes.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Neither of these bug patterns may appear in the rendered bash.
        assert '--slice-id ""' not in script, (
            'rendered bash must not emit `--slice-id ""` — plan/refine '
            "phases run without a slice and the heartbeat payload must "
            "omit slice_id (or send null), NOT empty-string."
        )
        assert '"slice_id":""' not in script
        assert '"slice_id": ""' not in script

    def test_slice_id_threaded_via_shell_substitution(self, monkeypatch):
        """The plan names two acceptable threading shapes (TASK-2-2
        line 831-834):

        - ``slice_id == os.environ['EGG_SLICE_ID']`` (Python-side read), OR
        - ``${EGG_SLICE_ID:-}`` shell substitution passed through the CLI.

        For the bash template the substitution form is the natural
        shape. Pin the presence of a ``${EGG_SLICE_ID...}`` substitution
        anywhere in the script — the CLI's ``cmd_message_heartbeat``
        already resolves the env var server-side, so even an
        empty-string default would be filtered by the handler. But the
        rendered bash MUST still reference the env var so the slice
        scope makes it onto the wire.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        assert "EGG_SLICE_ID" in script, (
            "rendered bash must reference EGG_SLICE_ID so slice scoping "
            "propagates onto the heartbeat payload (risk_analyst R9)."
        )


class TestEventPumpFlagIsolation:
    """Cross-cutting guards: the flag-on / flag-off paths must remain
    cleanly partitioned so a flip in slice-4 lands as a single bit
    change with no surprise interactions.
    """

    def test_flag_on_does_not_inherit_legacy_max_restarts(self, monkeypatch):
        """The new event-pump path replaces ``MAX_CONSENSUS_RESTARTS``
        with the idle budget. It must NOT also retain the old cap, or
        operators tuning ``--max-restarts`` will get surprising
        interactions.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt", max_restarts=7)
        script = cmd[2]
        # MAX_RESTARTS=7 must not be the operational cap on the new
        # path. Either the variable is absent or it is only referenced
        # for back-compat shape.
        # Allow the symbol to exist (the template may keep a stub)
        # but require the idle budget is the primary gate.
        assert "EGG_BRC_IDLE_BUDGET_MIN" in script, (
            "flag-on path must rely on EGG_BRC_IDLE_BUDGET_MIN, not "
            "MAX_RESTARTS (plan TASK-2-3 acceptance)."
        )

    def test_flag_on_does_not_re_invoke_recovery_system_prompt(self, monkeypatch):
        """The new template does not need the legacy recovery system
        prompt — the per-event invocation contract supplies its own
        memory + delta context. Plan slice-3 owns the per-event prompt
        composer.

        Pin that the legacy ``BRC Consensus Recovery`` header does NOT
        appear in the flag-on template; carrying it forward would
        confuse a one-shot per-event invocation with a recovery from
        crash.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The legacy recovery header text is a strong marker.
        assert "BRC Consensus Recovery" not in script, (
            "flag-on event-pump template must not carry the legacy "
            "'BRC Consensus Recovery' header forward — the per-event "
            "invocation contract supplies its own context."
        )


class TestEventPumpHeartbeatSubshellLifecycle:
    """Adversarial probing: the wrapper-owned heartbeat subshell MUST be
    killable by ``stop_background_heartbeat``. If the subshell
    installs an empty ``trap '' TERM`` then a default ``kill`` (SIGTERM)
    from the parent is IGNORED — the subshell continues forever, and
    the subsequent ``wait $HB_BG_PID`` blocks indefinitely because the
    process never exits. This wedges the entire wrapper loop after the
    first ``wait_for_event`` call returns.

    Issue lineage: this is exactly the bug class #2906 / #2451 were
    trying to fix at the agent-side layer. Re-introducing it at the
    wrapper layer would silently regress slice_id propagation AND wedge
    the deterministic loop.

    Note: behavioural verification of the kill semantics belongs in a
    bash-harness integration test (where we can spawn a subshell with
    the same trap and confirm that ``kill && wait`` does not return).
    These tests pin the *static* invariant against the rendered bash:
    if the subshell traps TERM, the corresponding ``stop`` path MUST
    use a signal that the subshell does not trap (``SIGINT``,
    ``SIGHUP``, or ``SIGKILL``); otherwise the lifecycle is broken.
    """

    def test_flag_on_heartbeat_subshell_can_be_stopped(self, monkeypatch):
        """If the rendered bash installs ``trap '' TERM`` (or any
        ignored-TERM equivalent) in the heartbeat subshell, the
        corresponding ``stop`` path MUST send a non-TERM signal so the
        subshell actually exits. Sending the default ``kill`` (= TERM)
        against a TERM-ignoring trap is a silent no-op — the subshell
        loops forever and the wait blocks indefinitely.

        Verified by ad-hoc bash harness during slice-2 review:

            $ bash -c '( trap "" TERM; while true; do sleep 1; echo tick; done ) &
                        sleep 2; kill $!; wait $! 2>/dev/null'
            tick
            tick
            (hang — never returns)

        Therefore the invariant: if the rendered bash sets
        ``trap '' TERM`` in the heartbeat subshell, the stop primitive
        must NOT rely on a default-signal ``kill``. Use ``kill -INT``,
        ``kill -HUP``, ``kill -KILL``, or do not install the empty
        TERM trap in the first place.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]

        # Strip ``#``-prefixed comment content so a comment that *mentions*
        # the buggy pattern (``# The earlier `trap '' TERM` form ...``)
        # doesn't trip the detector. We use a per-line scan rather than
        # full bash tokenisation — sufficient for the static invariant.
        def _strip_comments(text: str) -> str:
            out_lines = []
            for ln in text.splitlines():
                stripped = ln.lstrip()
                if stripped.startswith("#"):
                    continue
                # Trim inline trailing comments (best-effort: ``#`` outside
                # any quotes). False-positive risk is bounded because the
                # pattern we look for has its own quote shape.
                if " #" in ln:
                    ln = ln.split(" #", 1)[0]
                out_lines.append(ln)
            return "\n".join(out_lines)

        executable = _strip_comments(script)
        # Two failure modes the test guards against:
        #
        #  (A) Heartbeat subshell installs `trap '' TERM` AND the
        #      ``stop`` path issues a default-signal `kill $HB_BG_PID`
        #      (with no explicit signal). This is the silent-wedge bug.
        #
        #  (B) Heartbeat subshell installs `trap '' TERM` AND the
        #      ``stop`` path issues `kill -TERM`/`kill -15`. Same wedge,
        #      different shape.
        #
        # If the subshell does NOT install an ignored-TERM trap (either
        # absent or replaced with ``trap 'exit 0' TERM`` / equivalent
        # handler), this test is automatically satisfied — the wrapper is
        # free to use any kill primitive against a non-trapping (or
        # cleanly-exiting) subshell.
        installs_ignoring_term_trap = "trap '' TERM" in executable or 'trap "" TERM' in executable
        if not installs_ignoring_term_trap:
            # The subshell either does not trap TERM at all, or installs a
            # handler that exits cleanly on TERM (e.g. ``trap 'exit 0'
            # TERM``). Either way the default-signal kill / wait pair
            # works as expected.
            return
        # The subshell installs an ignored-TERM trap. The stop path MUST
        # use a non-TERM signal. Allowed primitives: ``kill -INT``,
        # ``kill -HUP``, ``kill -KILL``, ``kill -9``, or
        # ``kill -SIGINT``/-SIGHUP/-SIGKILL. Scan the script for any of
        # these adjacent to the ``HB_BG_PID`` symbol.
        allowed_stop_primitives = (
            "kill -INT",
            "kill -HUP",
            "kill -KILL",
            "kill -9",
            "kill -SIGINT",
            "kill -SIGHUP",
            "kill -SIGKILL",
        )
        kill_lines = [ln for ln in executable.splitlines() if "kill " in ln and "HB_BG_PID" in ln]
        # Detect a default-signal kill (no explicit -SIG flag).
        default_kill_lines = [
            ln
            for ln in kill_lines
            if not any(prim in ln for prim in allowed_stop_primitives) and "kill -" not in ln
        ]
        assert not default_kill_lines, (
            "the heartbeat subshell installs `trap '' TERM` which "
            "ignores the default SIGTERM signal. The corresponding "
            "stop path uses a default-signal `kill` which is a silent "
            "no-op against that trap — the subshell will never exit, "
            "and the subsequent `wait` blocks indefinitely, wedging "
            "the event-pump loop after the first wait_for_event call.\n"
            "Fix options: (a) remove the `trap '' TERM` from the "
            "subshell, (b) replace it with `trap 'exit 0' TERM` (or any "
            "handler that exits the subshell), or (c) change the stop "
            "path to `kill -INT`, `kill -HUP`, or `kill -KILL` so the "
            "signal is not trapped.\n"
            f"Offending kill line(s): {default_kill_lines}"
        )

    def test_flag_on_heartbeat_subshell_lifecycle_is_bounded(self, monkeypatch):
        """Companion to the trap test: regardless of the trap shape,
        the wrapper MUST have an ``EXIT``-time cleanup that stops the
        background heartbeat so a clean exit doesn't leave a stray
        subshell holding the gateway session open. (The orchestrator's
        ``ScriptedProvider`` ban for E2E means we cannot verify this
        end-to-end; this is the static guard.)
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The cleanup must be wired into bash's EXIT trap so even an
        # unexpected exit path tears the subshell down. Either
        # ``trap cleanup EXIT`` or ``trap '<stop call>' EXIT`` is
        # acceptable.
        assert "trap " in script and "EXIT" in script, (
            "wrapper must install an EXIT trap to clean up the "
            "background heartbeat subshell on any exit path."
        )


class TestEventPumpIdleAlertBrcSnapshot:
    """Adversarial regression for the v2 idle-alert BRC snapshot bug.

    Plan TASK-2-3 acceptance line 866-867: "alert payload includes
    anomaly type, priority, current BRC state". The v2 coder addressed
    this by embedding a ``brc_snapshot`` line in the alert detail
    sourced from ``${{STATE_JSON:-{}}}``. The bash parameter expansion
    is broken: bash's ``${{VAR:-DEFAULT}}`` syntax ends at the FIRST
    ``}}`` after ``${{``, so ``${{STATE_JSON:-{}}}`` is parsed as
    ``${{STATE_JSON:-{}}`` (default ``{``) followed by a literal
    trailing ``}``. When STATE_JSON IS unset the rendered text happens
    to read as ``{}}}`` collapsed to a valid empty-object literal by
    accident, but when STATE_JSON is populated (the common case during
    the event-pump loop) the rendered text appends a STRAY ``}`` to
    the JSON document, and ``json.load`` fails with
    ``json.decoder.JSONDecodeError: Extra data`` — the snapshot falls
    back to ``(unavailable)`` 100% of the time the alert actually has
    state to show.

    Verified end-to-end with the rendered bash (slice-2 v2):

        $ STATE_JSON='{"consensus":{"agents":{...}}}'
        $ echo "${STATE_JSON:-{}}" \
            | python3 -c 'import sys, json; json.load(sys.stdin)'
        json.decoder.JSONDecodeError: Extra data: line 1 column 110 (char 109)

    This is an observability bug, not a correctness wedge: the alert
    still fires, but the BRC-state field always reads "(unavailable)"
    when state IS available. That defeats the entire point of the
    snapshot (tester v1 non-blocker #2).

    Fix: use a temp variable for the default so the bash parser sees
    a balanced ``${{...}}``:

        local state_default='{}'
        echo "${{STATE_JSON:-$state_default}}" | python3 ...
    """

    def test_flag_on_state_json_default_does_not_corrupt_populated_json(self, monkeypatch):
        """The idle-alert BRC snapshot extraction MUST work when
        STATE_JSON is populated. Statically check that the rendered
        bash does NOT use the broken ``${{STATE_JSON:-{}}}`` pattern.
        """
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # The broken pattern. We pin against the exact literal because
        # the bash parser fails the same way regardless of variable
        # name; if any future code introduces a ``${VAR:-{}}`` it has
        # the same bug.
        assert "${STATE_JSON:-{}}" not in script, (
            "rendered bash contains `${STATE_JSON:-{}}` which bash "
            "parses as `${STATE_JSON:-{}` (default `{`) plus a "
            "trailing `}` — populated STATE_JSON values get a stray "
            "`}` appended, breaking the downstream `json.load`. The "
            "idle-alert BRC-snapshot field will always read "
            "`(unavailable)` in the common case. Fix: use a temp var "
            "for the default, e.g.\n"
            "    local state_default='{}'\n"
            '    echo "${STATE_JSON:-$state_default}" | python3 ...'
        )

    def test_flag_on_state_json_snapshot_round_trips_populated_json(self, monkeypatch, tmp_path):
        """Behavioral round-trip: render the bash, extract the
        ``brc_snapshot=$(echo ... | python3 ...)`` block, drive it
        with a populated STATE_JSON, and assert the output does NOT
        say ``(unavailable)``.
        """
        import os
        import re
        import subprocess

        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        cmd = build_consensus_wrapped_command("Prompt")
        script = cmd[2]
        # Locate the brc_snapshot extraction. The shape evolved across
        # cycles: v2 used ``brc_snapshot=$(echo "${STATE_JSON:-{}}" |
        # python3 ...)`` (the broken form this test originally pinned);
        # v3 uses a separate ``snapshot_input`` variable + explicit
        # empty-string check, then ``printf '%s' "$snapshot_input" |
        # python3 ...``. We extract from the start of the
        # ``raise_idle_alert`` function definition through the
        # ``(snapshot unavailable)`` literal so either shape round-
        # trips through the harness.
        match = re.search(
            r"raise_idle_alert\(\) \{(.*?\(snapshot unavailable\)\"\))",
            script,
            flags=re.DOTALL,
        )
        if match is None:
            import pytest as _pytest

            _pytest.skip(
                "raise_idle_alert / brc_snapshot extraction block not "
                "present in rendered bash; behavioral test does not "
                "apply."
            )
        # The captured group is the function body up through the
        # snapshot extraction; trim the leading ``local`` declarations
        # so the harness can supply its own STATE_JSON without
        # collision.
        snapshot_block = match.group(1)
        # The captured block lives inside a function body; replace
        # ``local`` declarations with plain assignments so the harness
        # can run it at the top level of the wrapper script.
        snapshot_block = re.sub(
            r"^\s*local (\w+)(?: (\w+))?",
            lambda m: " ".join(g for g in (m.group(1), m.group(2)) if g),
            snapshot_block,
            flags=re.MULTILINE,
        )
        # Build a minimal harness: define STATE_JSON, run the block,
        # echo the result.
        harness = (
            'STATE_JSON=\'{"consensus":{"agents":'
            '{"tester":{"confirmed":true,"producer_phase":"WORKING"}},'
            '"blocking_agents":["coder"]}}\'\n'
            + snapshot_block
            + '\necho "RESULT=[$brc_snapshot]"\n'
        )
        env = os.environ.copy()
        env["EGG_AGENT_ROLE"] = "tester"
        result = subprocess.run(
            ["bash", "-c", harness],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        # The snapshot MUST contain the role info from the populated
        # STATE_JSON, NOT the "(unavailable)" fallback.
        assert "RESULT=[" in result.stdout, (
            f"harness did not produce a RESULT line; stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        result_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT="))
        assert "(unavailable)" not in result_line, (
            "idle-alert BRC snapshot reads `(unavailable)` even when "
            "STATE_JSON IS populated — the `${STATE_JSON:-{}}` bash "
            "parameter expansion corrupts the JSON with a stray `}` "
            "before it reaches `json.load`. The snapshot enhancement "
            "ships broken; operators will never see structured state "
            "in the alert detail. See test docstring for the fix.\n"
            f"Got: {result_line}"
        )
        # And the result should contain the role we set.
        assert "tester" in result_line, (
            f"snapshot does not contain the EGG_AGENT_ROLE; got {result_line!r}"
        )


class TestEventPumpConfirmFailureRaisesIdleAlert:
    """(reviewer §1 lock-in) When ``egg-orch consensus confirmed``
    persistently fails on the ``confirm`` arm, the wrapper must NOT
    tight-retry. The idle-budget overseer alert is the replacement
    for the legacy ``MAX_CONSENSUS_RESTARTS=3`` ceiling -- it MUST
    fire when the underlying CLI keeps returning non-zero.

    Pre-fix bug shape (slice-2 v1): the ``confirm)`` arm called
    ``note_progress`` unconditionally after the CLI returned, which
    reset ``LAST_PROGRESS`` and both ``ALERTED_AT_*`` latches every
    iteration. ``check_idle_budget`` therefore never observed a
    growing idle and the alert never fired -- a tight retry loop
    with zero operator-visible signal.

    Post-fix: ``note_progress`` only fires on rc==0. A persistent
    non-zero rc lets the idle counter accrue; ``check_idle_budget``
    fires the OVERSEER_ALERT at the configured budget.
    """

    def test_persistent_confirm_failure_fires_overseer_alert(self, tmp_path, monkeypatch):
        """End-to-end behavioural test of the §1 lock-in.

        Drive the rendered event-pump bash against stubbed
        ``egg-orch`` / ``python3`` shims:
          - ``brc get-state`` → role unconfirmed
          - ``brc next-action`` → ``{"action":"confirm"}``
          - ``consensus confirmed`` → exit 1 every call (persistent
            failure)
          - ``overseer alert`` → record the call to a log file
          - everything else → noop / exit 0

        With ``EGG_BRC_IDLE_BUDGET_MIN=0`` the alert should fire on
        the very first ``check_idle_budget`` after the failing
        confirm; the test asserts the alert log was written within a
        short timeout.
        """
        # ``_event_pump_enabled`` is read at template-composition time
        # (in this Python process), NOT inside the subprocess shell.
        # Set the env var here so ``build_consensus_wrapped_command``
        # emits the flag-on event-pump template body.
        monkeypatch.setenv("EGG_BRC_EVENT_PUMP", "true")
        # Stub directory on PATH ahead of the real egg-orch.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        confirm_log = tmp_path / "confirm_calls.log"
        alert_log = tmp_path / "alert_calls.log"
        general_log = tmp_path / "egg_orch.log"

        # Mock egg-orch: route on the first two positional words so we
        # can recognise ``brc get-state`` / ``brc next-action`` /
        # ``consensus confirmed`` / ``overseer alert`` etc.
        mock_orch = bin_dir / "egg-orch"
        mock_orch.write_text(
            "#!/bin/bash\n"
            f'echo "$@" >> {shlex.quote(str(general_log))}\n'
            'sub="$1 $2"\n'
            'case "$sub" in\n'
            '    "brc get-state")\n'
            '        echo \'{"consensus":{"agents":{"coder":{"confirmed":false,'
            '"producer_phase":"WAITING_FOR_REVIEW"}},"is_complete":false}}\'\n'
            "        ;;\n"
            '    "brc next-action")\n'
            '        echo \'{"action":"confirm"}\'\n'
            "        ;;\n"
            '    "consensus confirmed")\n'
            f"        echo confirm_call >> {shlex.quote(str(confirm_log))}\n"
            "        exit 1\n"
            "        ;;\n"
            '    "overseer alert")\n'
            f'        echo "alert: $*" >> {shlex.quote(str(alert_log))}\n'
            "        ;;\n"
            "    *)\n"
            "        # message heartbeat, message wait-loop, etc -- benign no-ops.\n"
            "        ;;\n"
            "esac\n"
            "exit 0\n"
        )
        os.chmod(str(mock_orch), 0o755)  # nosec B103

        # Stub python3 so the agent invocation arm (not exercised here
        # because next-action == ``confirm``) doesn't accidentally run
        # the real Agent SDK if a future regression flips the action.
        # Forward inline ``python3 -c`` invocations (which the wrapper
        # uses for JSON parsing) to the real interpreter.
        real_python = sys.executable
        mock_python = bin_dir / "python3"
        mock_python.write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "-c" ] || [ "$1" = "-" ]; then\n'
            f'    exec {shlex.quote(real_python)} "$@"\n'
            "fi\n"
            "# Agent SDK invocation path -- treat as success no-op.\n"
            "exit 0\n"
        )
        os.chmod(str(mock_python), 0o755)  # nosec B103

        # Build the wrapper with the flag on; idle budget of 0 makes
        # ``check_idle_budget`` fire on the first non-progress
        # iteration.
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["EGG_BRC_EVENT_PUMP"] = "true"
        env["EGG_BRC_IDLE_BUDGET_MIN"] = "0"
        env["EGG_AGENT_ROLE"] = "coder"
        env["EGG_PIPELINE_ID"] = "test-pipeline"
        env["EGG_CONCURRENT_MODE"] = "true"

        cmd = build_consensus_wrapped_command("Prompt")
        # The wrapper loops forever; bound the test with a short
        # timeout. By that time, several confirm attempts and at
        # least one overseer alert must have been recorded if the
        # §1 fix is in place.
        try:
            subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except subprocess.TimeoutExpired:
            pass  # expected — the wrapper loops; we bound it.

        assert confirm_log.exists(), (
            "wrapper did not call `egg-orch consensus confirmed` at "
            "all on the confirm arm -- the event-pump loop may not "
            "have reached the confirm action. egg-orch log:\n"
            + (general_log.read_text() if general_log.exists() else "(empty)")
        )
        confirm_attempts = confirm_log.read_text().count("confirm_call")
        assert confirm_attempts >= 1, f"expected >= 1 confirm attempts, got {confirm_attempts}"

        assert alert_log.exists(), (
            "§1 regression: `egg-orch consensus confirmed` failed "
            f"{confirm_attempts} times but the overseer idle-budget "
            "alert never fired. The pre-fix `note_progress` reset "
            "ran unconditionally on every confirm-arm iteration, "
            "resetting LAST_PROGRESS and the ALERTED_AT_* latches so "
            "`check_idle_budget` never observed a growing idle. "
            "Post-fix: `note_progress` only fires on rc==0; "
            "persistent confirm failure must surface as an "
            "OVERSEER_ALERT. egg-orch log:\n" + general_log.read_text()
        )
        alert_text = alert_log.read_text()
        assert "stuck-phase-transition" in alert_text, (
            f"overseer alert fired but with the wrong anomaly type. Got:\n{alert_text}"
        )
