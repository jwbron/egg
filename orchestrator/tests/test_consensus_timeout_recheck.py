"""Tests for consensus recheck after timeout fallback (issue #1691).

When the consensus timeout fires (step 6) and the code falls back to
event-driven polling of container status, consensus may still be
reached during the wait window.  If containers are force-killed after
consensus was confirmed, the phase should succeed — not fail due to
exit_code=-1 from force-killed containers.
"""

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


def _make_concurrent_pipeline(pipeline_id: str = "issue-1691") -> Pipeline:
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
        issue_number=1691,
        repo="owner/repo",
        branch="egg/issue-1691",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


def _make_execution(role: AgentRole, container_id: str, status=AgentExecutionStatus.RUNNING):
    return AgentExecution(
        role=role,
        status=status,
        container_id=container_id,
        started_at=datetime.now(UTC),
    )


def _make_phase_execution():
    return PhaseExecution(
        phase=PipelinePhase.IMPLEMENT,
        status=PipelineStatus.RUNNING,
    )


_CALL_ARGS = {
    "repo_volumes": {},
    "gateway_mode": "public",
    "repos": ["owner/repo"],
    "sandbox_env": {},
    "certs_volume": None,
    "worktree_repo_path": Path("/tmp/test-repo"),
}


class TestConsensusTimeoutRecheck:
    """Consensus reached during the timeout wait should return exit 0."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_timeout_with_consensus_reached_returns_zero(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When consensus timeout fires but consensus is reached during the
        post-timeout polling wait, the phase should succeed (exit code 0).

        Reproduces the scenario from issue #1691:
        1. Timeout fires at 30 min (step 6)
        2. Containers polled in event-driven loop
        3. Some containers force-killed (exit -1)
        4. But consensus IS complete
        5. Recheck should detect consensus and return 0
        """
        # Simulate time: first call returns a value past the 30-min timeout
        # so the loop enters step 6 immediately after the first consensus check.
        call_count = [0]

        def _monotonic():
            call_count[0] += 1
            # First few calls: past the timeout threshold
            return call_count[0] * 2000.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
            _make_execution(AgentRole.REVIEWER_CODE, "reviewer-1"),
        ]

        # All containers initially RUNNING — they haven't exited yet when
        # the timeout fires.
        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1691-coder",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1691-tester",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
            "reviewer-1": ContainerInfo(
                container_id="reviewer-1",
                container_name="issue-1691-reviewer_code",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
        }

        pipeline = _make_concurrent_pipeline()
        phase_exec = _make_phase_execution()

        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_pipeline_state.status = PipelineStatus.RUNNING
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)
        mock_docker.stop_container.return_value = ContainerInfo(
            container_id="stopped",
            container_name="stopped",
            status=ContainerStatus.EXITED,
            exit_code=137,
        )

        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        # Consensus: NOT complete on first check (triggers timeout path),
        # then IS complete on the recheck after containers exit.
        consensus_call_count = [0]

        def _check_consensus():
            consensus_call_count[0] += 1
            if consensus_call_count[0] == 1:
                # First check: consensus not yet reached (triggers timeout)
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "blocking_agents": ["coder"],
                }
            # Subsequent checks (recheck after timeout wait): consensus reached
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
            pipeline_id="issue-1691",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # The fix: exit code should be 0 because consensus was reached
        assert exit_code == 0, (
            f"Expected exit code 0 (consensus reached during timeout wait), got {exit_code}. "
            f"Consensus was called {consensus_call_count[0]} times. Logs: {logs}"
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_timeout_without_consensus_still_fails(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When consensus timeout fires and consensus is NOT reached during
        the wait, the phase should still fail (exit code 1).

        Ensures the recheck doesn't accidentally make all timeouts succeed.
        """
        call_count = [0]

        def _monotonic():
            call_count[0] += 1
            return call_count[0] * 2000.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.TESTER, "tester-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1691-coder",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
            "tester-1": ContainerInfo(
                container_id="tester-1",
                container_name="issue-1691-tester",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
        }

        pipeline = _make_concurrent_pipeline()
        phase_exec = _make_phase_execution()

        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_pipeline_state.status = PipelineStatus.RUNNING
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)
        mock_docker.stop_container.return_value = ContainerInfo(
            container_id="stopped",
            container_name="stopped",
            status=ContainerStatus.EXITED,
            exit_code=137,
        )

        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        # Consensus never reached
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
            pipeline_id="issue-1691",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1, (
            f"Expected exit code 1 (no consensus after timeout), got {exit_code}. Logs: {logs}"
        )

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_timeout_stops_orphaned_containers(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """Containers that timeout during the wait should be explicitly stopped
        to prevent orphaned containers."""
        call_count = [0]

        def _monotonic():
            call_count[0] += 1
            return call_count[0] * 2000.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1691-coder",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
        }

        pipeline = _make_concurrent_pipeline()
        phase_exec = _make_phase_execution()

        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_pipeline_state.status = PipelineStatus.RUNNING
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)
        mock_docker.stop_container.return_value = ContainerInfo(
            container_id="coder-1",
            container_name="issue-1691-coder",
            status=ContainerStatus.EXITED,
            exit_code=137,
        )

        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": False,
            "has_objections": False,
            "blocking_agents": ["coder"],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        _run_concurrent_phase(
            pipeline_id="issue-1691",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        # Verify stop_container was called for the force-killed container
        mock_docker.stop_container.assert_called_with("coder-1", timeout=30)

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_timeout_with_consensus_and_unresolved_nacks_returns_failure(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When consensus is complete on timeout recheck but has unresolved
        NACKs, the phase should fail (exit code 1) and escalate to HITL.

        Mirrors the step 5 NACK guard — consensus "complete" by quorum does
        not mean all feedback was addressed.
        """
        call_count = [0]

        def _monotonic():
            call_count[0] += 1
            return call_count[0] * 2000.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.REVIEWER_CODE, "reviewer-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1691-coder",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
            "reviewer-1": ContainerInfo(
                container_id="reviewer-1",
                container_name="issue-1691-reviewer_code",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
        }

        pipeline = _make_concurrent_pipeline()
        phase_exec = _make_phase_execution()

        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_pipeline_state.status = PipelineStatus.RUNNING
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)
        mock_docker.stop_container.return_value = ContainerInfo(
            container_id="stopped",
            container_name="stopped",
            status=ContainerStatus.EXITED,
            exit_code=137,
        )

        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        # Consensus: NOT complete first, then complete WITH unresolved NACKs
        consensus_call_count = [0]

        def _check_consensus():
            consensus_call_count[0] += 1
            if consensus_call_count[0] == 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "blocking_agents": ["coder"],
                }
            return {
                "is_complete": True,
                "has_objections": False,
                "has_unresolved_nacks": True,
                "unresolved_nacks": [
                    {"reviewer": "reviewer_code", "producer": "coder", "reason": "Missing tests"},
                ],
                "blocking_agents": [],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1691",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1, (
            f"Expected exit code 1 (consensus complete but unresolved NACKs), got {exit_code}. "
            f"Logs: {logs}"
        )
        assert "UNRESOLVED NACKs" in logs

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_timeout_incomplete_consensus_with_unresolved_nacks_appends_summary(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When consensus is NOT complete on the has_failures recheck but has
        unresolved NACKs, the NACK summary should appear in the returned logs.

        Exercises the new code path at pipelines.py:8617-8630 where
        is_complete=False and has_unresolved_nacks=True — distinct from the
        sibling test where is_complete=True with NACKs.
        """
        call_count = [0]

        def _monotonic():
            call_count[0] += 1
            return call_count[0] * 2000.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.REVIEWER_CODE, "reviewer-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1691-coder",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
            "reviewer-1": ContainerInfo(
                container_id="reviewer-1",
                container_name="issue-1691-reviewer_code",
                status=ContainerStatus.RUNNING,
                exit_code=None,
            ),
        }

        pipeline = _make_concurrent_pipeline()
        phase_exec = _make_phase_execution()

        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_pipeline_state.status = PipelineStatus.RUNNING
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)
        mock_docker.stop_container.return_value = ContainerInfo(
            container_id="stopped",
            container_name="stopped",
            status=ContainerStatus.EXITED,
            exit_code=137,
        )

        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        # Consensus: never complete, but has unresolved NACKs on recheck
        consensus_call_count = [0]

        def _check_consensus():
            consensus_call_count[0] += 1
            if consensus_call_count[0] == 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "blocking_agents": ["coder"],
                }
            # Subsequent checks: still incomplete but with unresolved NACKs
            return {
                "is_complete": False,
                "has_objections": False,
                "has_unresolved_nacks": True,
                "unresolved_nacks": [
                    {
                        "reviewer": "reviewer_code",
                        "producer": "coder",
                        "reason": "Missing error handling",
                    },
                ],
                "blocking_agents": ["coder"],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1691",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1, (
            f"Expected exit code 1 (incomplete consensus with NACKs), got {exit_code}. Logs: {logs}"
        )
        # The NACK summary should be appended even when is_complete=False
        assert "UNRESOLVED NACKs" in logs
        assert "Missing error handling" in logs

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_step5_has_failures_with_consensus_and_nacks_returns_failure(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When containers exit with failures (step 5), consensus is complete on
        final recheck, but there are unresolved NACKs, the phase should fail
        (exit code 1) and escalate to HITL.

        This mirrors the timeout path NACK guard — step 5's has_failures branch
        must also check for unresolved NACKs before returning success.
        """
        # Use a short monotonic sequence so step 5 fires (containers exit)
        # before the consensus timeout (step 6).
        call_count = [0]

        def _monotonic():
            call_count[0] += 1
            # Stay well under the 30-min timeout so step 5 handles exit
            return call_count[0] * 5.0

        mock_monotonic.side_effect = _monotonic

        # Swap which container has the non-zero exit so the producer-death
        # short-circuit (#2806) does not preempt the step-5 NACK guard under
        # test. Reviewer fails non-zero; coder exits clean. The NACK target
        # ("reviewer_code" -> "coder") still encodes the consensus state the
        # guard is meant to detect.
        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
            _make_execution(AgentRole.REVIEWER_CODE, "reviewer-1"),
        ]

        container_infos = {
            "coder-1": ContainerInfo(
                container_id="coder-1",
                container_name="issue-1691-coder",
                status=ContainerStatus.EXITED,
                exit_code=0,
                exited_at=datetime.now(UTC),
            ),
            "reviewer-1": ContainerInfo(
                container_id="reviewer-1",
                container_name="issue-1691-reviewer_code",
                status=ContainerStatus.EXITED,
                exit_code=1,
                exited_at=datetime.now(UTC),
            ),
        }

        pipeline = _make_concurrent_pipeline()
        phase_exec = _make_phase_execution()

        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = phase_exec
        mock_pipeline_state.status = PipelineStatus.RUNNING
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_docker.get_container_info.side_effect = lambda cid: container_infos.get(cid)
        mock_docker.stop_container.return_value = ContainerInfo(
            container_id="stopped",
            container_name="stopped",
            status=ContainerStatus.EXITED,
            exit_code=137,
        )

        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        # Consensus: NOT complete on first check, then complete WITH
        # unresolved NACKs on final recheck (step 5 has_failures path).
        consensus_call_count = [0]

        def _check_consensus():
            consensus_call_count[0] += 1
            if consensus_call_count[0] == 1:
                return {
                    "is_complete": False,
                    "has_objections": False,
                    "blocking_agents": ["coder"],
                }
            return {
                "is_complete": True,
                "has_objections": False,
                "has_unresolved_nacks": True,
                "unresolved_nacks": [
                    {
                        "reviewer": "reviewer_code",
                        "producer": "coder",
                        "reason": "Security concern",
                    },
                ],
                "blocking_agents": [],
            }

        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.side_effect = _check_consensus
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        exit_code, logs = _run_concurrent_phase(
            pipeline_id="issue-1691",
            pipeline=pipeline,
            phase="implement",
            spawner=mock_spawner,
            store=mock_store,
            **_CALL_ARGS,
        )

        assert exit_code == 1, (
            f"Expected exit code 1 (consensus complete but unresolved NACKs in has_failures path), "
            f"got {exit_code}. Logs: {logs}"
        )
        assert "UNRESOLVED NACKs" in logs


class TestReconstructedNoShaFiltering:
    """The RECONSTRUCTED_NO_SHA sentinel must not leak into agent.commit."""

    @patch("routes.pipelines.time.sleep")
    @patch("routes.pipelines.time.monotonic")
    @patch("routes.pipelines._emit_event")
    @patch("routes.pipelines.get_pipeline_state_lock")
    @patch("routes.pipelines._build_agent_prompt", return_value="test prompt")
    @patch("concurrent_executor.ConcurrentPhaseExecutor", autospec=False)
    def test_reconstructed_no_sha_not_stored_as_commit(
        self, MockExecutor, mock_prompt, mock_lock, mock_emit, mock_monotonic, mock_sleep
    ):
        """When get_proposal_commit_sha returns RECONSTRUCTED_NO_SHA, agent.commit
        should remain empty — the sentinel must not be stored as a real SHA."""
        call_count = [0]

        def _monotonic():
            call_count[0] += 1
            return call_count[0] * 2000.0

        mock_monotonic.side_effect = _monotonic

        executions = [
            _make_execution(AgentRole.CODER, "coder-1"),
        ]

        pipeline = _make_concurrent_pipeline()

        # Create a mock agent with no commit set
        mock_agent = MagicMock()
        mock_agent.status = AgentExecutionStatus.RUNNING
        mock_agent.commit = None
        mock_agent.container_id = "coder-1"
        mock_agent.role = AgentRole.CODER
        mock_agent.completed_at = None

        mock_pe = MagicMock()
        mock_pe.agents = [mock_agent]
        mock_pe.containers = []

        mock_store = MagicMock()
        mock_pipeline_state = MagicMock()
        mock_pipeline_state.get_phase_execution.return_value = mock_pe
        mock_pipeline_state.status = PipelineStatus.RUNNING
        mock_store.load_pipeline.return_value = mock_pipeline_state

        mock_docker = MagicMock()
        mock_docker.get_container_info.return_value = ContainerInfo(
            container_id="coder-1",
            container_name="issue-1691-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.now(UTC),
        )
        mock_docker.wait_for_container.return_value = ContainerInfo(
            container_id="coder-1",
            container_name="issue-1691-coder",
            status=ContainerStatus.EXITED,
            exit_code=0,
            exited_at=datetime.now(UTC),
        )

        mock_spawner = MagicMock()
        mock_spawner.backend = mock_docker
        mock_spawner.docker = mock_docker
        mock_spawner.create_concurrent_spawn_fn.return_value = MagicMock()

        # BRC tracker returns RECONSTRUCTED_NO_SHA for the coder
        mock_brc = MagicMock()
        mock_brc.get_proposal_commit_sha.return_value = "RECONSTRUCTED_NO_SHA"

        # Consensus: complete on first check (container exited cleanly)
        mock_executor_instance = MagicMock()
        mock_executor_instance.spawn_all.return_value = executions
        mock_executor_instance.check_consensus.return_value = {
            "is_complete": True,
            "has_objections": False,
            "blocking_agents": [],
        }
        MockExecutor.return_value = mock_executor_instance

        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        with patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_brc):
            _run_concurrent_phase(
                pipeline_id="issue-1691",
                pipeline=pipeline,
                phase="implement",
                spawner=mock_spawner,
                store=mock_store,
                **_CALL_ARGS,
            )

        # Verify the BRC code path was actually exercised — the mock must have
        # been called, proving the sentinel filter ran (not a vacuous pass).
        mock_brc.get_proposal_commit_sha.assert_called_with(AgentRole.CODER.value)

        # The sentinel should NOT have been stored as agent.commit
        assert mock_agent.commit != "RECONSTRUCTED_NO_SHA", (
            "RECONSTRUCTED_NO_SHA sentinel was stored as agent.commit"
        )
        assert mock_agent.commit is None, (
            "agent.commit should remain None when only a sentinel SHA is available"
        )
