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
    DecisionStatus,
    HITLDecision,
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

    def test_reconciles_running_container_in_completed_phase(self):
        """A RUNNING agent in the current phase (marked COMPLETE) with a dead container is FAILED.

        Reviewers run inside phases already marked complete. The reconciler
        must still check the current phase even if its status is COMPLETE,
        because reviewers may still have running containers.
        """
        container_id = "reviewer_dead_xyz"
        pipeline = _make_pipeline_with_running_agent(container_id)
        # Phase is complete, but reviewer container is still RUNNING
        # current_phase is still IMPLEMENT (the phase being checked)
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.COMPLETE

        store = _make_store(pipeline)
        docker_client = _make_docker_client([])  # container gone

        result = reconcile_stale_containers(store, docker_client)

        assert result == 1
        assert pipeline.status == PipelineStatus.FAILED
        agent = phase.agents[0]
        assert agent.status == AgentExecutionStatus.FAILED
        assert agent.completed_at is not None

    def test_dead_containers_in_prior_phase_not_marked_failed(self):
        """Dead containers in a completed prior phase do NOT trigger FAILED.

        When a pipeline has moved past a phase (e.g. refine → plan),
        containers from the prior phase are intentionally terminated.
        Only the current phase should be checked.
        """
        pipeline = Pipeline(
            id="issue-200",
            issue_number=200,
            repo="owner/repo",
            branch="egg/issue-200",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.PLAN,
        )
        # Prior phase (refine) has a dead container — expected after phase transition
        refine_phase = pipeline.get_phase_execution(PipelinePhase.REFINE)
        refine_phase.status = PipelineStatus.COMPLETE
        refine_phase.started_at = datetime.utcnow()
        refine_phase.containers.append(
            ContainerInfo(
                container_id="refine_dead_abc",
                container_name="egg-coder-refine",
                status=ContainerStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
        )
        refine_phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="refine_dead_abc",
                started_at=datetime.utcnow(),
            )
        )

        # Current phase (plan) has a live container
        plan_phase = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_phase.status = PipelineStatus.RUNNING
        plan_phase.started_at = datetime.utcnow()
        plan_phase.containers.append(
            ContainerInfo(
                container_id="plan_live_xyz",
                container_name="egg-coder-plan",
                status=ContainerStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
        )
        plan_phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="plan_live_xyz",
                started_at=datetime.utcnow(),
            )
        )

        store = _make_store(pipeline)
        docker_client = _make_docker_client(["plan_live_xyz"])  # only plan container alive

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        assert pipeline.status == PipelineStatus.RUNNING
        # Prior phase containers/agents are untouched
        assert refine_phase.containers[0].status == ContainerStatus.RUNNING
        assert refine_phase.agents[0].status == AgentExecutionStatus.RUNNING

    def test_dead_containers_in_current_phase_marked_failed(self):
        """Dead containers in the current phase DO trigger FAILED.

        When the current phase has dead containers (both plan live and dead),
        the pipeline should be marked FAILED.
        """
        pipeline = Pipeline(
            id="issue-201",
            issue_number=201,
            repo="owner/repo",
            branch="egg/issue-201",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.PLAN,
        )
        # Prior phase (refine) has a dead container — should be ignored
        refine_phase = pipeline.get_phase_execution(PipelinePhase.REFINE)
        refine_phase.status = PipelineStatus.COMPLETE
        refine_phase.started_at = datetime.utcnow()
        refine_phase.containers.append(
            ContainerInfo(
                container_id="refine_dead_abc",
                container_name="egg-coder-refine",
                status=ContainerStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
        )
        refine_phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="refine_dead_abc",
                started_at=datetime.utcnow(),
            )
        )

        # Current phase (plan) has a dead container — should trigger FAILED
        plan_phase = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_phase.status = PipelineStatus.RUNNING
        plan_phase.started_at = datetime.utcnow()
        plan_phase.containers.append(
            ContainerInfo(
                container_id="plan_dead_xyz",
                container_name="egg-coder-plan",
                status=ContainerStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
        )
        plan_phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="plan_dead_xyz",
                started_at=datetime.utcnow(),
            )
        )

        store = _make_store(pipeline)
        docker_client = _make_docker_client([])  # no live containers

        result = reconcile_stale_containers(store, docker_client)

        assert result == 1
        assert pipeline.status == PipelineStatus.FAILED
        # Current phase containers/agents ARE marked failed
        assert plan_phase.containers[0].status == ContainerStatus.FAILED
        assert plan_phase.agents[0].status == AgentExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# AWAITING_HUMAN reconciliation tests
# ---------------------------------------------------------------------------


def _make_awaiting_human_pipeline(
    pending_decisions: int = 0,
    resolved_decisions: int = 1,
) -> Pipeline:
    """Return an AWAITING_HUMAN pipeline with configurable decision counts."""
    pipeline = Pipeline(
        id="issue-77",
        issue_number=77,
        repo="owner/repo",
        branch="egg/issue-77",
        mode="issue",
        status=PipelineStatus.AWAITING_HUMAN,
        current_phase=PipelinePhase.REFINE,
    )
    # Add resolved decisions
    for i in range(resolved_decisions):
        pipeline.decisions.append(
            HITLDecision(
                id=f"decision-{i + 1}",
                question="Approve phase?",
                decision_type="phase_gate",
                status=DecisionStatus.RESOLVED,
                resolution='{"action": "approve"}',
            )
        )
    # Add pending decisions
    for i in range(pending_decisions):
        pipeline.decisions.append(
            HITLDecision(
                id=f"decision-pending-{i + 1}",
                question="Approve phase?",
                decision_type="phase_gate",
                status=DecisionStatus.PENDING,
            )
        )
    return pipeline


class TestReconcileAwaitingHuman:
    """Tests for AWAITING_HUMAN reconciliation at startup."""

    def test_awaiting_human_zero_pending_marked_failed(self):
        """AWAITING_HUMAN with 0 pending decisions is marked FAILED."""
        pipeline = _make_awaiting_human_pipeline(pending_decisions=0, resolved_decisions=1)
        store = _make_store(pipeline)
        docker_client = _make_docker_client([])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 1
        assert pipeline.status == PipelineStatus.FAILED
        assert "AWAITING_HUMAN" in pipeline.error
        store.save_pipeline.assert_called_once_with(pipeline)

    def test_awaiting_human_with_pending_left_alone(self):
        """AWAITING_HUMAN with pending decisions is not modified."""
        pipeline = _make_awaiting_human_pipeline(pending_decisions=1, resolved_decisions=0)
        store = _make_store(pipeline)
        docker_client = _make_docker_client([])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        assert pipeline.status == PipelineStatus.AWAITING_HUMAN
        store.save_pipeline.assert_not_called()

    def test_awaiting_human_recovery_counted_in_return_value(self):
        """Multiple AWAITING_HUMAN recoveries are counted."""
        p1 = _make_awaiting_human_pipeline(pending_decisions=0)
        p1.id = "issue-1"
        p2 = _make_awaiting_human_pipeline(pending_decisions=0)
        p2.id = "issue-2"

        store = MagicMock()
        store.list_pipelines.return_value = ["issue-1", "issue-2"]
        store.load_pipeline.side_effect = [p1, p2]
        docker_client = _make_docker_client([])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 2
        assert p1.status == PipelineStatus.FAILED
        assert p2.status == PipelineStatus.FAILED
