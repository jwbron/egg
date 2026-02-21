"""Tests for startup_reconciliation.reconcile_stale_containers()."""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    ContainerInfo,
    ContainerStatus,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from startup_reconciliation import reconcile_stale_containers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline_with_running_agent(container_id: str = "abc123") -> Pipeline:
    """Return a RUNNING pipeline with one RUNNING coder agent."""
    pipeline = Pipeline(
        id="issue-99",
        issue_number=99,
        repo="owner/repo",
        branch="egg/issue-99",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
    )
    phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
    phase.status = PipelineStatus.RUNNING
    phase.started_at = datetime.utcnow()

    phase.containers.append(
        ContainerInfo(
            container_id=container_id,
            container_name="egg-coder-issue-99",
            status=ContainerStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
    )
    phase.agents.append(
        AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.RUNNING,
            container_id=container_id,
            started_at=datetime.utcnow(),
        )
    )
    return pipeline


def _make_store(pipeline: Pipeline) -> MagicMock:
    store = MagicMock()
    store.list_pipelines.return_value = [pipeline.id]
    store.load_pipeline.return_value = pipeline
    return store


def _make_docker_client(live_ids: list[str]) -> MagicMock:
    docker_client = MagicMock()
    live_containers = [MagicMock(container_id=cid) for cid in live_ids]
    docker_client.list_containers.return_value = live_containers
    return docker_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReconcileStaleContainers:
    """Tests for reconcile_stale_containers()."""

    def test_returns_zero_when_no_pipelines(self):
        """No pipelines → returns 0, save never called."""
        store = MagicMock()
        store.list_pipelines.return_value = []
        docker_client = _make_docker_client([])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        store.save_pipeline.assert_not_called()

    def test_returns_zero_when_pipeline_not_running(self):
        """A COMPLETE pipeline with a stale container ID is left alone."""
        pipeline = _make_pipeline_with_running_agent("dead123")
        pipeline.status = PipelineStatus.COMPLETE

        store = _make_store(pipeline)
        docker_client = _make_docker_client([])  # container not live

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        store.save_pipeline.assert_not_called()

    def test_returns_zero_when_container_still_live(self):
        """A RUNNING pipeline whose container is still alive is not touched."""
        container_id = "live_container_abc"
        pipeline = _make_pipeline_with_running_agent(container_id)

        store = _make_store(pipeline)
        docker_client = _make_docker_client([container_id])  # still running

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        store.save_pipeline.assert_not_called()
        assert pipeline.status == PipelineStatus.RUNNING

    def test_marks_pipeline_failed_when_container_gone(self):
        """A RUNNING pipeline with a dead container is marked FAILED."""
        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)

        store = _make_store(pipeline)
        docker_client = _make_docker_client([])  # no live containers

        result = reconcile_stale_containers(store, docker_client)

        assert result == 1
        assert pipeline.status == PipelineStatus.FAILED
        assert pipeline.error is not None
        store.save_pipeline.assert_called_once_with(pipeline)

    def test_marks_agent_failed_when_container_gone(self):
        """The agent inside the stale phase is marked FAILED with an error."""
        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)

        store = _make_store(pipeline)
        docker_client = _make_docker_client([])

        reconcile_stale_containers(store, docker_client)

        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        agent = phase.agents[0]
        assert agent.status == AgentExecutionStatus.FAILED
        assert agent.error is not None
        assert agent.completed_at is not None

    def test_marks_container_info_failed_when_container_gone(self):
        """The ContainerInfo entry in the phase is marked FAILED with exit_code=-1."""
        container_id = "dead_container_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)

        store = _make_store(pipeline)
        docker_client = _make_docker_client([])

        reconcile_stale_containers(store, docker_client)

        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        ci = phase.containers[0]
        assert ci.status == ContainerStatus.FAILED
        assert ci.exit_code == -1
        assert ci.exited_at is not None

    def test_skips_pipeline_when_list_containers_raises(self):
        """If Docker is unreachable, returns 0 without crashing."""
        pipeline = _make_pipeline_with_running_agent("dead_xyz")
        store = _make_store(pipeline)

        docker_client = MagicMock()
        docker_client.list_containers.side_effect = Exception("Docker unavailable")

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        store.save_pipeline.assert_not_called()

    def test_skips_pipeline_when_list_pipelines_raises(self):
        """If the state store is unavailable, returns 0 without crashing."""
        docker_client = _make_docker_client([])

        store = MagicMock()
        store.list_pipelines.side_effect = Exception("State store unavailable")

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0

    def test_skips_individual_pipeline_on_load_error(self):
        """A load error for one pipeline does not prevent others from being checked."""
        pipeline = _make_pipeline_with_running_agent("dead_xyz")

        store = MagicMock()
        store.list_pipelines.return_value = ["bad-pipeline", pipeline.id]
        store.load_pipeline.side_effect = [
            Exception("corrupt state"),
            pipeline,
        ]
        docker_client = _make_docker_client([])  # no live containers

        result = reconcile_stale_containers(store, docker_client)

        # The second pipeline should be recovered
        assert result == 1

    def test_multiple_stale_pipelines_all_recovered(self):
        """All stale pipelines in one pass are counted in the return value."""
        p1 = _make_pipeline_with_running_agent("dead1")
        p1.id = "issue-1"
        p2 = _make_pipeline_with_running_agent("dead2")
        p2.id = "issue-2"

        store = MagicMock()
        store.list_pipelines.return_value = ["issue-1", "issue-2"]
        store.load_pipeline.side_effect = [p1, p2]
        docker_client = _make_docker_client([])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 2
        assert p1.status == PipelineStatus.FAILED
        assert p2.status == PipelineStatus.FAILED

    def test_alive_container_not_disturbed_alongside_dead_one(self):
        """Only stale containers/agents are touched; live ones are left alone."""
        pipeline = Pipeline(
            id="issue-mixed",
            issue_number=100,
            repo="owner/repo",
            branch="egg/issue-100",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING
        phase.started_at = datetime.utcnow()

        live_id = "live_abc"
        dead_id = "dead_xyz"

        for cid, cname in [(live_id, "live-cont"), (dead_id, "dead-cont")]:
            phase.containers.append(
                ContainerInfo(
                    container_id=cid,
                    container_name=cname,
                    status=ContainerStatus.RUNNING,
                    started_at=datetime.utcnow(),
                )
            )
            phase.agents.append(
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id=cid,
                    started_at=datetime.utcnow(),
                )
            )

        store = _make_store(pipeline)
        docker_client = _make_docker_client([live_id])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 1
        assert pipeline.status == PipelineStatus.FAILED

        live_ci = next(ci for ci in phase.containers if ci.container_id == live_id)
        dead_ci = next(ci for ci in phase.containers if ci.container_id == dead_id)
        assert live_ci.status == ContainerStatus.RUNNING
        assert dead_ci.status == ContainerStatus.FAILED

        live_agent = next(a for a in phase.agents if a.container_id == live_id)
        dead_agent = next(a for a in phase.agents if a.container_id == dead_id)
        assert live_agent.status == AgentExecutionStatus.RUNNING
        assert dead_agent.status == AgentExecutionStatus.FAILED
