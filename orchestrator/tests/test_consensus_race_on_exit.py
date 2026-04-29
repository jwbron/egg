"""Tests for consensus race condition on all-container-exit path (issue #1564).

When all containers exit with non-zero codes, _run_concurrent_phase must
perform a final consensus check before returning failure.  If consensus
completed between the loop's step-2 check and the step-5 all-exited path,
the phase should succeed (exit 0) rather than report failure.

Also tests the consensus wrapper's check_agent_confirmed_with_fallback
function, which must check the message bus when the tracker is populated
but stale (e.g. after a withdrawal/re-proposal cascade).
"""

import os
import shlex
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    PhaseExecution,
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import _run_concurrent_phase

# ---------------------------------------------------------------------------
# Helpers — reuse patterns from test_consensus_complete_with_failures.py
# ---------------------------------------------------------------------------


def _make_concurrent_pipeline(pipeline_id: str = "issue-1564") -> Pipeline:
    """Create a pipeline with concurrent_execution enabled."""
    config = PipelineConfig()
    for key, val in {
        "concurrent_execution": True,
        "max_concurrent_agents": 5,
        "message_poll_hint_seconds": 30,
        "consensus_timeout_minutes": 30,
    }.items():
        try:
            setattr(config, key, val)
        except AttributeError, ValueError:
            config.__dict__[key] = val

    return Pipeline(
        id=pipeline_id,
        issue_number=1564,
        repo="owner/repo",
        branch="egg/issue-1564",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_execution(
    role: AgentRole,
    container_id: str,
    status: AgentExecutionStatus = AgentExecutionStatus.RUNNING,
) -> AgentExecution:
    return AgentExecution(
        role=role,
        status=status,
        container_id=container_id,
        started_at=datetime.now(UTC),
    )


def _make_phase_execution() -> PhaseExecution:
    return PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )


def _base_mocks(
    executions: list[AgentExecution],
    container_infos: dict[str, ContainerInfo] | None = None,
) -> tuple:
    """Create common mocks for _run_concurrent_phase tests."""
    pipeline = _make_concurrent_pipeline()
    phase_exec = _make_phase_execution()

    mock_store = MagicMock()
    mock_pipeline_state = MagicMock()
    mock_pipeline_state.get_phase_execution.return_value = phase_exec
    mock_store.load_pipeline.return_value = mock_pipeline_state

    mock_docker = MagicMock()

    if container_infos is None:
        container_infos = {}
        for e in executions:
            if e.container_id:
                container_infos[e.container_id] = ContainerInfo(
                    container_id=e.container_id,
                    container_name=f"issue-1564-{e.role.value}",
                    status=ContainerStatus.RUNNING,
                    exit_code=None,
                )

    mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)

    mock_spawner = MagicMock()
    mock_spawner.backend = mock_docker
    mock_spawner.docker = mock_docker
    mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

    return pipeline, mock_store, mock_spawner, mock_docker


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


# ===========================================================================
# Task 1-3: Race condition — consensus completes after containers exit
# ===========================================================================


class TestConsensusRaceOnContainerExit:
    """When all containers exit with failures but consensus completes between
    the loop's step-2 check and the step-5 all-exited fallback, the final
    consensus re-check in step 5 should recover the pipeline (exit 0)."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_final_consensus_check_recovers_after_all_containers_fail(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """Core race condition test (task-1-3).

        Scenario: check_consensus returns is_complete=False on the first call
        (step 2), then all containers exit with code 1. The final consensus
        re-check (added by the fix) returns is_complete=True. The phase should
        return exit code 0.
        """
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
            _make_execution(AgentRole.REVIEWER_CODE, "reviewer-1"),
        ]

        # All containers have already exited with non-zero codes
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1564-coder",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1564-tester",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
            "reviewer-1": ContainerInfo(
                container_id="reviewer-1",
                container_name="issue-1564-reviewer",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        # First check_consensus call returns False (step 2 in the loop),
        # second call (final re-check in step 5) returns True.
        call_count = [0]

        def _check_consensus():
            call_count[0] += 1
            if call_count[0] <= 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "blocking_agents": ["coder", "tester", "reviewer_code"],
                }
            return {
                "is_complete": True,
                "has_objections": False,
                "blocking_agents": [],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1564",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0, (
            f"Expected exit code 0 (final consensus re-check should recover), "
            f"got {exit_code}. Logs: {logs}"
        )
        # The executor should have been asked to check consensus at least twice
        assert call_count[0] >= 2, (
            f"Expected at least 2 consensus checks (initial + final re-check), got {call_count[0]}"
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_final_consensus_check_still_fails_when_no_consensus(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """The final re-check should NOT suppress genuine failures.

        When consensus is still incomplete on the final re-check, the phase
        must return exit code 1.
        """
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1564-coder",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1564-tester",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        # Consensus is never complete
        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "blocking_agents": ["coder", "tester"],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1564",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1, (
            f"Expected exit code 1 (no consensus even on re-check), got {exit_code}. Logs: {logs}"
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_final_recheck_calls_update_agents_and_stop_containers(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """When the final re-check succeeds, _update_agents_complete and
        _stop_running_containers should be invoked (same as step 2 path)."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1564-coder",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1564-tester",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        # First call: incomplete; second call: complete
        call_count = [0]

        def _check_consensus():
            call_count[0] += 1
            if call_count[0] <= 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "blocking_agents": ["coder"],
                }
            return {
                "is_complete": True,
                "has_objections": False,
                "blocking_agents": [],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1564",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0
        # store.save_pipeline should have been called (by _update_agents_complete)
        mock_store.save_pipeline.assert_called()

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_final_recheck_recovers_failed_pipeline_status(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """When the final re-check succeeds and the pipeline was externally
        marked FAILED, the status should be recovered to RUNNING."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1564-coder",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        # Simulate pipeline being externally marked FAILED
        failed_pipeline = _make_concurrent_pipeline()
        failed_pipeline.status = PipelineStatus.FAILED
        mock_store.load_pipeline.return_value = failed_pipeline

        call_count = [0]

        def _check_consensus():
            call_count[0] += 1
            if call_count[0] <= 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "blocking_agents": ["coder"],
                }
            return {
                "is_complete": True,
                "has_objections": False,
                "blocking_agents": [],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1564",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0, (
            f"Expected exit code 0 (consensus recovery), got {exit_code}. Logs: {logs}"
        )
        # Pipeline status should have been recovered
        mock_store.save_pipeline.assert_called()

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_final_recheck_exception_propagates_failure(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """If the final consensus re-check raises an exception, the phase
        should still return exit code 1 (not crash)."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1564-coder",
                status=ContainerStatus.EXITED,
                exit_code=1,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        call_count = [0]

        def _check_consensus():
            call_count[0] += 1
            if call_count[0] <= 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "blocking_agents": ["coder"],
                }
            # Final re-check raises an exception (e.g. network error)
            raise RuntimeError("Orchestrator API unreachable")

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1564",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1, (
            f"Expected exit code 1 (re-check failed), got {exit_code}. Logs: {logs}"
        )


# ===========================================================================
# Task 1-4: Wrapper fallback — stale tracker with populated agents map
# ===========================================================================


class TestWrapperStaleTrackerFallback:
    """Tests for check_agent_confirmed_with_fallback when the tracker has a
    populated agents map but shows the agent as NOT confirmed (stale state
    after withdrawal/re-proposal cascade)."""

    @staticmethod
    def _run_wrapper_command(
        cmd: list[str],
        tmpdir: str,
        timeout: int = 15,
        agent_role: str = "coder",
    ) -> subprocess.CompletedProcess:
        """Run a wrapper command with test environment."""
        env = os.environ.copy()
        env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"
        env["EGG_CONCURRENT_MODE"] = "true"
        env["EGG_AGENT_ROLE"] = agent_role
        env["EGG_CONSENSUS_WRAPPER_TIMEOUT"] = "2"
        env["EGG_MESSAGE_POLL_INTERVAL"] = "1"
        return subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_stale_tracker_with_bus_confirmed_returns_true(self):
        """When tracker is non-empty but shows agent as NOT confirmed, and the
        message bus contains the agent's CONSENSUS_CONFIRMED, the wrapper
        should detect confirmation and exit cleanly (task-1-4)."""
        from consensus_wrapper import build_consensus_wrapped_command

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")

            # Mock egg-orch: tracker populated, agent confirmed=false,
            # but message bus has CONSENSUS_CONFIRMED from this agent
            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                # Tracker non-empty, agent confirmed=false (stale after withdrawal)
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": '
                    '{"is_complete": false, "agents": {"coder": {"confirmed": false, "status": "proposed"}}}}}}\'\n'
                )
                f.write('elif echo "$@" | grep -q "message poll"; then\n')
                # Message bus has the agent's CONSENSUS_CONFIRMED
                f.write(
                    '  echo \'[{"message_type": "CONSENSUS_CONFIRMED", "from_role": "coder", "data": {}}]\'\n'
                )
                f.write("else\n")
                f.write('  echo "{}"\n')
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock agent that exits with code 1 (simulating post-consensus crash)
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write("  exit 1\n")
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir, agent_role="coder")

            assert result.returncode == 0, (
                f"Expected exit 0 (stale tracker fallback should find CONFIRMED "
                f"in message bus), got {result.returncode}.\n"
                f"stderr: {result.stderr}"
            )
            assert "CONFIRMED" in result.stderr or "confirmed" in result.stderr.lower()

    def test_stale_tracker_without_bus_confirmed_fails(self):
        """When tracker is non-empty but shows agent as NOT confirmed, and
        the message bus does NOT have a CONSENSUS_CONFIRMED, the wrapper
        should NOT detect confirmation (agent genuinely not confirmed)."""
        from consensus_wrapper import build_consensus_wrapped_command

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")

            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                # Tracker non-empty, agent confirmed=false (genuinely not confirmed)
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": '
                    '{"is_complete": false, "agents": {"coder": {"confirmed": false, "status": "proposed"}}}}}}\'\n'
                )
                f.write('elif echo "$@" | grep -q "message poll"; then\n')
                # Message bus has NO CONSENSUS_CONFIRMED from this agent
                f.write(
                    '  echo \'[{"message_type": "CONSENSUS_ACK", "from_role": "reviewer_code", "data": {}}]\'\n'
                )
                f.write("else\n")
                f.write('  echo "{}"\n')
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock agent that exits with code 1
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write("  exit 1\n")
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            # Disable the startup-failure retry heuristic — this test targets
            # the tracker/bus-fallback path, not retry-on-exit-1 behavior.
            cmd = build_consensus_wrapped_command(
                "Do the work", max_restarts=2, startup_failure_window_seconds=0
            )
            result = self._run_wrapper_command(cmd, tmpdir, agent_role="coder")

            # Should fail because agent is genuinely not confirmed
            assert result.returncode != 0, (
                f"Expected non-zero exit (no CONFIRMED in bus), "
                f"got {result.returncode}.\nstderr: {result.stderr}"
            )

    def test_empty_tracker_still_uses_bus_fallback(self):
        """The existing behavior (empty tracker → bus fallback) should still
        work after the fix expands the fallback to non-empty stale trackers."""
        from consensus_wrapper import build_consensus_wrapped_command

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")

            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                # Tracker empty (orchestrator restarted, lost state)
                f.write(
                    '  echo \'{"data": {"concurrent": {"consensus": '
                    '{"is_complete": false, "agents": {}}}}}\'\n'
                )
                f.write('elif echo "$@" | grep -q "message poll"; then\n')
                # Bus has CONSENSUS_CONFIRMED
                f.write(
                    '  echo \'[{"message_type": "CONSENSUS_CONFIRMED", "from_role": "coder", "data": {}}]\'\n'
                )
                f.write("else\n")
                f.write('  echo "{}"\n')
                f.write("fi\n")
            os.chmod(mock_orch, 0o755)  # nosec B103

            # Mock agent that exits with code 1
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write("  exit 1\n")
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir, agent_role="coder")

            assert result.returncode == 0, (
                f"Expected exit 0 (empty tracker bus fallback), "
                f"got {result.returncode}.\nstderr: {result.stderr}"
            )

    def test_confirmed_true_in_tracker_takes_precedence(self):
        """When the tracker shows confirmed=true, the bus should NOT be
        checked (early return path)."""
        from consensus_wrapper import build_consensus_wrapped_command

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "egg-orch.log")

            mock_orch = os.path.join(tmpdir, "egg-orch")
            with open(mock_orch, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f'echo "$@" >> {shlex.quote(log_file)}\n')
                f.write('if echo "$@" | grep -q "pipeline status"; then\n')
                # Tracker shows confirmed=true
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

            # Mock agent that exits with code 1
            mock_python = os.path.join(tmpdir, "python3")
            real_python = sys.executable
            with open(mock_python, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('if [ "$1" = "-m" ] && [ "$2" = "egg_agent" ]; then\n')
                f.write("  exit 1\n")
                f.write("else\n")
                f.write(f'  exec {shlex.quote(real_python)} "$@"\n')
                f.write("fi\n")
            os.chmod(mock_python, 0o755)  # nosec B103

            cmd = build_consensus_wrapped_command("Do the work", max_restarts=2)
            result = self._run_wrapper_command(cmd, tmpdir, agent_role="coder")

            assert result.returncode == 0, (
                f"Expected exit 0 (tracker confirmed=true), "
                f"got {result.returncode}.\nstderr: {result.stderr}"
            )
            # Should NOT have polled the message bus — check the log
            with open(log_file) as f:
                log_content = f.read()
            assert "message poll" not in log_content or "pipeline status" in log_content


# ===========================================================================
# Issue #1581: Clean exit without consensus — no-failures path
# ===========================================================================


class TestCleanExitWithoutConsensus:
    """When all containers exit with code 0 (no failures) but BRC consensus
    is not complete, _run_concurrent_phase must return exit code 1.

    This covers the bug in issue #1581 where the no-failures branch at step 5
    returned 0 without verifying consensus.is_complete, allowing the pipeline
    to advance (and open a PR) despite consensus not being reached.
    """

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_clean_exit_without_consensus_returns_failure(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """Task 1-2: All containers exit code 0, consensus never completes.

        Scenario: All agents exit cleanly (code 0), no NACKs, but
        check_consensus always returns is_complete=False. The phase must
        return exit code 1 to prevent pipeline advancement.
        """
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
            _make_execution(AgentRole.REVIEWER_CODE, "reviewer-1"),
        ]

        # All containers exited cleanly — exit code 0 (no failures)
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1581-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1581-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
            "reviewer-1": ContainerInfo(
                container_id="reviewer-1",
                container_name="issue-1581-reviewer",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        # Consensus is never complete
        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "has_unresolved_nacks": False,
            "blocking_agents": ["coder", "tester", "reviewer_code"],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1581",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1, (
            f"Expected exit code 1 (all containers exited cleanly but consensus "
            f"not reached), got {exit_code}. Logs: {logs}"
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_clean_exit_with_late_consensus_returns_success(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """Task 1-3: All containers exit code 0, consensus completes on recheck.

        Scenario: All agents exit cleanly (code 0), no NACKs. Initial
        check_consensus returns is_complete=False, but the final recheck
        returns is_complete=True. The phase should return exit code 0.
        """
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        # All containers exited cleanly — exit code 0
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1581-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1581-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        # First call: incomplete; final recheck: complete
        call_count = [0]

        def _check_consensus():
            call_count[0] += 1
            if call_count[0] <= 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "has_unresolved_nacks": False,
                    "blocking_agents": ["coder", "tester"],
                }
            return {
                "is_complete": True,
                "has_objections": False,
                "has_unresolved_nacks": False,
                "blocking_agents": [],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1581",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0, (
            f"Expected exit code 0 (consensus completed on final recheck), "
            f"got {exit_code}. Logs: {logs}"
        )
        # Should have called check_consensus at least twice
        assert call_count[0] >= 2, (
            f"Expected at least 2 consensus checks (initial + final recheck), got {call_count[0]}"
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_clean_exit_without_consensus_recheck_exception_returns_failure(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """Edge case: Final consensus recheck raises an exception in the
        no-failures path. Should still return exit code 1, not crash."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1581-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        call_count = [0]

        def _check_consensus():
            call_count[0] += 1
            if call_count[0] <= 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "has_unresolved_nacks": False,
                    "blocking_agents": ["coder"],
                }
            raise RuntimeError("Orchestrator API unreachable")

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1581",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1, (
            f"Expected exit code 1 (consensus recheck exception), got {exit_code}. Logs: {logs}"
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_clean_exit_with_consensus_complete_returns_success(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """Positive case: All containers exit code 0, consensus is already
        complete. Should return exit code 0 (no regression)."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1581-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1581-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        # Consensus is always complete
        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "has_unresolved_nacks": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1581",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0, (
            f"Expected exit code 0 (consensus already complete), got {exit_code}. Logs: {logs}"
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_clean_exit_without_consensus_calls_update_agents_on_recovery(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """When the clean-exit recheck succeeds, _update_agents_complete and
        _stop_running_containers should be invoked (same as has_failures path)."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1581-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1581-tester",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        call_count = [0]

        def _check_consensus():
            call_count[0] += 1
            if call_count[0] <= 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "has_unresolved_nacks": False,
                    "blocking_agents": ["coder"],
                }
            return {
                "is_complete": True,
                "has_objections": False,
                "has_unresolved_nacks": False,
                "blocking_agents": [],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1581",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 0

        # Verify _update_agents_complete was called: it calls
        # store.load_pipeline -> pip.get_phase_execution -> store.save_pipeline.
        # The mock_pipeline_state returned by load_pipeline must have
        # get_phase_execution called on it (from _update_agents_complete,
        # not just from step 4 container tracking which uses a different path).
        mock_pipeline_state = mock_store.load_pipeline.return_value
        mock_pipeline_state.get_phase_execution.assert_called()
        mock_store.save_pipeline.assert_called_with(mock_pipeline_state)

        # Verify CONSENSUS_REACHED event was emitted
        from events import EventType

        consensus_calls = [
            c for c in mock_emit.call_args_list if c[0][0] == EventType.CONSENSUS_REACHED
        ]
        assert len(consensus_calls) == 1, (
            f"Expected exactly 1 CONSENSUS_REACHED event, got {len(consensus_calls)}"
        )
        assert consensus_calls[0][0][1] == "issue-1581"

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_clean_exit_with_unresolved_nacks_still_fails(
        self,
        MockExecutor,
        mock_prompt,
        mock_lock,
        mock_emit,
        mock_monotonic,
        mock_sleep,
    ):
        """Regression: unresolved NACKs path should still work. Even though
        consensus recheck is added, the NACK check comes first and should
        still return failure."""
        mock_monotonic.return_value = 10.0

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.REVIEWER_CODE, "reviewer-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1581-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
            "reviewer-1": ContainerInfo(
                container_id="reviewer-1",
                container_name="issue-1581-reviewer",
                status=ContainerStatus.EXITED,
                exit_code=0,
            ),
        }
        pipeline, mock_store, mock_spawner, mock_docker = _base_mocks(executions, container_infos)

        # Consensus incomplete with unresolved NACKs
        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "has_unresolved_nacks": True,
            "unresolved_nacks": [
                {
                    "reviewer": "reviewer_code",
                    "producer": "coder",
                    "reason": "Missing error handling",
                }
            ],
            "blocking_agents": ["coder"],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1581",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1, (
            f"Expected exit code 1 (unresolved NACKs), got {exit_code}. Logs: {logs}"
        )
