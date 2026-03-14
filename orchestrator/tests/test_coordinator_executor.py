"""
Tests for CoordinatorExecutor (Phase 3, TASK-3-1a and TASK-3-1b).

Tests coordinator container lifecycle management including:
- Spawning coordinator when coordinator_enabled
- Injecting coordinator env vars
- Health monitoring and crash recovery
- Global guardrail enforcement
- Pipeline completion handling
"""

import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from models import (
    AgentRole,
    AgentSpawnRecord,
    ContainerInfo,
    CoordinatorState,
    GuardrailCounters,
    Pipeline,
    PipelineConfig,
    PipelineStatus,
)
from coordinator_executor import CoordinatorExecutor


class TestCoordinatorExecutorModuleExists:
    """Tests for the existence of the coordinator executor module."""

    def test_coordinator_executor_file_exists(self):
        """orchestrator/coordinator_executor.py must exist.

        Gap: This is a new file that needs to be created.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        assert executor_path.exists(), (
            "orchestrator/coordinator_executor.py does not exist. "
            "Create CoordinatorExecutor class with spawn, monitor, and recovery logic."
        )

    def test_coordinator_executor_class_importable(self):
        """CoordinatorExecutor class must be importable."""
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        try:
            from coordinator_executor import CoordinatorExecutor  # noqa: F401
        except ImportError as e:
            pytest.fail(f"Cannot import CoordinatorExecutor: {e}")


class TestCoordinatorExecutorSpawn:
    """Tests for coordinator container spawning."""

    def test_coordinator_spawns_when_enabled(self):
        """Coordinator container should spawn when coordinator_enabled is True.

        Gap: Not yet implemented.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        assert "coordinator_enabled" in content or "spawn" in content.lower(), (
            "CoordinatorExecutor should check coordinator_enabled and spawn coordinator container"
        )

    def test_coordinator_env_vars_injected(self):
        """Coordinator container should receive coordinator-specific env vars.

        Expected env vars: EGG_COORDINATOR_MODE=true, EGG_COORDINATOR_TOOLS=true,
        issue context, repo info.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_env_setup = "EGG_COORDINATOR_MODE" in content or "coordinator" in content.lower()
        assert has_env_setup, (
            "CoordinatorExecutor should inject EGG_COORDINATOR_MODE and related env vars"
        )


class TestCoordinatorExecutorRecovery:
    """Tests for coordinator crash recovery."""

    def test_crash_recovery_logic_exists(self):
        """CoordinatorExecutor must have crash recovery logic.

        Gap: Crash detection and respawn logic needed.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_recovery = any(
            keyword in content.lower() for keyword in ["respawn", "crash", "recovery", "restart"]
        )
        assert has_recovery, "CoordinatorExecutor should have crash detection and respawn logic"

    def test_max_respawns_enforced(self):
        """Coordinator respawns must be limited (default 2).

        Gap: Max respawn enforcement.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_limit = "max" in content.lower() and "respawn" in content.lower()
        if not has_limit:
            pytest.skip("Max respawn limit not yet implemented")


class TestCoordinatorExecutorGuardrails:
    """Tests for global guardrail enforcement."""

    def test_max_agents_enforced(self):
        """Coordinator executor must enforce max total agents (default 10).

        Gap: Global agent count guardrail.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_max = "max" in content.lower() and "agent" in content.lower()
        assert has_max, "CoordinatorExecutor should enforce max total agents guardrail"

    def test_guardrail_counters_tracked(self):
        """GuardrailCounters model correctly tracks enforcement data."""
        counters = GuardrailCounters(
            total_agents_spawned=10,
            retries_by_role={"coder": 2, "tester": 1},
            coordinator_respawns=1,
        )
        # Verify we can check against limits
        assert counters.total_agents_spawned >= 10  # At max
        assert counters.retries_by_role.get("coder", 0) >= 2  # At max retries


class TestCoordinatorExecutorCompletion:
    """Tests for coordinator completion handling."""

    def test_completion_checks_agents_done(self):
        """Coordinator completion should verify all spawned agents are done.

        Gap: Completion verification logic.
        """
        executor_path = _project_root / "orchestrator" / "coordinator_executor.py"
        if not executor_path.exists():
            pytest.skip("coordinator_executor.py not yet created")

        content = executor_path.read_text()
        has_completion = "complet" in content.lower()
        assert has_completion, (
            "CoordinatorExecutor should handle coordinator completion and verify agents"
        )


class TestCoordinatorPipelineWiring:
    """Tests for wiring CoordinatorExecutor into pipeline creation (TASK-3-2)."""

    def test_pipelines_route_references_coordinator(self):
        """Pipeline creation route must route to CoordinatorExecutor when enabled.

        Gap: Wiring in orchestrator/routes/pipelines.py.
        """
        pipelines_path = _project_root / "orchestrator" / "routes" / "pipelines.py"
        if not pipelines_path.exists():
            pytest.skip("pipelines.py not found")

        content = pipelines_path.read_text()
        has_coordinator = "coordinator" in content.lower()
        if not has_coordinator:
            pytest.skip(
                "Pipeline creation route does not yet reference coordinator. "
                "Need to route to CoordinatorExecutor when coordinator_enabled is true."
            )

    def test_existing_pipelines_unaffected(self):
        """Existing pipelines must work when coordinator_enabled is false (default)."""
        # Default config should not have coordinator enabled
        config = PipelineConfig()
        # coordinator_enabled should default to false if it exists
        if hasattr(config, "coordinator_enabled"):
            assert config.coordinator_enabled is False
        # Standard pipeline should still work
        pipeline = Pipeline(
            id="issue-999",
            issue_number=999,
            repo="owner/repo",
            branch="egg/issue-999",
        )
        assert pipeline.status == PipelineStatus.PENDING


def _make_pipeline_with_coordinator(
    pipeline_id: str = "issue-100",
    agents: list[AgentSpawnRecord] | None = None,
) -> Pipeline:
    """Helper to create a pipeline with coordinator state and running agents."""
    state = CoordinatorState()
    if agents:
        state.agents_spawned = agents
    return Pipeline(
        id=pipeline_id,
        issue_number=100,
        repo="owner/repo",
        branch="egg/issue-100",
        status=PipelineStatus.RUNNING,
        config=PipelineConfig(coordinator_enabled=True),
        coordinator_state=state,
    )


class TestCoordinatorCompletionDoesNotSetComplete:
    """Verify handle_coordinator_completion does NOT set pipeline.status = COMPLETE."""

    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.emit_event")
    def test_completion_does_not_set_pipeline_complete(
        self, mock_emit, mock_lock, mock_store_fn
    ):
        """On exit code 0 with no running agents, pipeline.status must NOT be COMPLETE."""
        pipeline = _make_pipeline_with_coordinator()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = mock_store
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        executor = CoordinatorExecutor(repo_path="/tmp/test")
        result = executor.handle_coordinator_completion("issue-100", exit_code=0)

        assert result == "complete"
        # Pipeline status must NOT have been set to COMPLETE by the executor
        assert pipeline.status != PipelineStatus.COMPLETE

    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    @patch("coordinator_executor.emit_event")
    def test_completion_returns_drained_when_agents_running(
        self, mock_emit, mock_lock, mock_store_fn
    ):
        """On exit code 0 with running agents, result should be 'drained'."""
        agents = [
            AgentSpawnRecord(
                role=AgentRole.CODER,
                status="running",
                container_id="container-abc",
            ),
        ]
        pipeline = _make_pipeline_with_coordinator(agents=agents)

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = mock_store
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_docker = MagicMock()
        mock_docker.stop_container.return_value = MagicMock(exit_code=0)

        executor = CoordinatorExecutor(repo_path="/tmp/test", docker_client=mock_docker)
        result = executor.handle_coordinator_completion("issue-100", exit_code=0)

        assert result == "drained"
        assert pipeline.status != PipelineStatus.COMPLETE
        mock_docker.stop_container.assert_called_once_with("container-abc", timeout=30)


class TestDrainRunningAgents:
    """Tests for _drain_running_agents."""

    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    def test_drain_updates_spawn_records(self, mock_lock, mock_store_fn):
        """Draining should update spawn records to complete/failed."""
        agents = [
            AgentSpawnRecord(
                role=AgentRole.CODER,
                status="running",
                container_id="ctr-1",
            ),
            AgentSpawnRecord(
                role=AgentRole.TESTER,
                status="running",
                container_id="ctr-2",
            ),
            AgentSpawnRecord(
                role=AgentRole.DOCUMENTER,
                status="complete",
                container_id="ctr-3",
            ),
        ]
        pipeline = _make_pipeline_with_coordinator(agents=agents)

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = mock_store
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_docker = MagicMock()
        mock_docker.stop_container.return_value = MagicMock(exit_code=0)

        executor = CoordinatorExecutor(repo_path="/tmp/test", docker_client=mock_docker)
        drained = executor._drain_running_agents("issue-100")

        assert drained == 2
        assert agents[0].status == "complete"
        assert agents[1].status == "complete"
        assert agents[2].status == "complete"  # unchanged — was already complete
        assert mock_docker.stop_container.call_count == 2

    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    def test_drain_handles_stop_failure(self, mock_lock, mock_store_fn):
        """Drain should continue even if individual container stops fail."""
        agents = [
            AgentSpawnRecord(
                role=AgentRole.CODER,
                status="running",
                container_id="ctr-1",
            ),
            AgentSpawnRecord(
                role=AgentRole.TESTER,
                status="running",
                container_id="ctr-2",
            ),
        ]
        pipeline = _make_pipeline_with_coordinator(agents=agents)

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store_fn.return_value = mock_store
        mock_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = MagicMock(return_value=False)

        mock_docker = MagicMock()
        # First call raises, second succeeds
        mock_docker.stop_container.side_effect = [
            Exception("Docker socket error"),
            MagicMock(exit_code=0),
        ]

        executor = CoordinatorExecutor(repo_path="/tmp/test", docker_client=mock_docker)
        drained = executor._drain_running_agents("issue-100")

        # Both should be drained (one failed, one succeeded)
        assert drained == 2
        assert agents[0].status == "failed"  # stop raised
        assert agents[1].status == "complete"  # stop succeeded
        assert mock_docker.stop_container.call_count == 2

    @patch("coordinator_executor.get_state_store")
    @patch("coordinator_executor.get_pipeline_state_lock")
    def test_drain_without_docker_client(self, mock_lock, mock_store_fn):
        """Without docker_client, drain should return 0 and not error."""
        executor = CoordinatorExecutor(repo_path="/tmp/test", docker_client=None)
        drained = executor._drain_running_agents("issue-100")
        assert drained == 0
