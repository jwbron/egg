"""Tests for startup_reconciliation.reconcile_stale_containers()."""

import sys
from datetime import UTC, datetime
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
from peer_consensus import _trackers, _trackers_lock
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
    phase.started_at = datetime.now(UTC)

    phase.containers.append(
        ContainerInfo(
            container_id=container_id,
            container_name="egg-coder-issue-99",
            status=ContainerStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
    )
    phase.agents.append(
        AgentExecution(
            role=AgentRole.CODER,
            status=AgentExecutionStatus.RUNNING,
            container_id=container_id,
            started_at=datetime.now(UTC),
        )
    )
    return pipeline


def _make_store(pipeline: Pipeline) -> MagicMock:
    store = MagicMock()
    store.list_pipelines.return_value = [pipeline.id]
    store.load_pipeline.return_value = pipeline
    return store


def _make_docker_client(
    live_ids: list[str],
    pipeline_live_map: dict[str, list[str]] | None = None,
    pipeline_live_status: ContainerStatus = ContainerStatus.RUNNING,
) -> MagicMock:
    """Build a mock docker client.

    Args:
        live_ids: Cluster-wide live container IDs returned for the un-scoped
            ``list_containers(all=False)`` call.
        pipeline_live_map: Per-pipeline mapping from pipeline_id to the list
            of live container IDs returned for label-scoped queries
            (``list_containers(labels={"egg.pipeline.id": <id>})``).  When
            omitted, label-scoped queries default to ``[]`` for any
            pipeline_id — matching real k8s, where label scoping is a
            strict subset of the un-scoped query and an unknown label
            value yields an empty list.  Tests that need a pipeline to
            be observed as having live pods must set this explicitly.
        pipeline_live_status: ContainerStatus assigned to the mocked pods
            returned by label-scoped queries.  Defaults to ``RUNNING`` —
            ``startup_reconciliation`` filters to "live" statuses
            (``Pending``/``Creating``/``Running``) so terminal pods inside
            the Job's TTL window are not mistaken for live work (#2420).
            Tests exercising terminal-phase pods pass ``FAILED`` / ``EXITED``
            explicitly.
    """
    docker_client = MagicMock()

    def _dispatch(all=True, labels=None):
        if labels and "egg.pipeline.id" in labels:
            pid = labels["egg.pipeline.id"]
            ids = (pipeline_live_map or {}).get(pid, [])
            return [MagicMock(container_id=cid, status=pipeline_live_status) for cid in ids]
        return [MagicMock(container_id=cid) for cid in live_ids]

    docker_client.list_containers.side_effect = _dispatch
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
        docker_client = _make_docker_client(
            live_ids=[container_id],
            pipeline_live_map={pipeline.id: [container_id]},
        )

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

    def test_pipeline_with_any_live_pod_left_running(self):
        """A pipeline with at least one live pod (per label query) stays RUNNING.

        Pre-#2411 behavior was to mark the whole pipeline FAILED whenever any
        in-memory record had a stale container_id, even if other records were
        still backed by live pods. That false-positive divorced the
        orchestrator from healthy pipelines after a restart, since persisted
        container_ids can drift from the new orch process's view of the
        cluster (#2411).
        """
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
        phase.started_at = datetime.now(UTC)

        live_id = "live_abc"
        dead_id = "dead_xyz"

        for cid, cname in [(live_id, "live-cont"), (dead_id, "dead-cont")]:
            phase.containers.append(
                ContainerInfo(
                    container_id=cid,
                    container_name=cname,
                    status=ContainerStatus.RUNNING,
                    started_at=datetime.now(UTC),
                )
            )
            phase.agents.append(
                AgentExecution(
                    role=AgentRole.CODER,
                    status=AgentExecutionStatus.RUNNING,
                    container_id=cid,
                    started_at=datetime.now(UTC),
                )
            )

        store = _make_store(pipeline)
        docker_client = _make_docker_client(
            live_ids=[live_id],
            pipeline_live_map={pipeline.id: [live_id]},
        )

        result = reconcile_stale_containers(store, docker_client)

        # Pipeline has at least one live pod under its label, so the whole
        # pipeline is left RUNNING.  Stale records aren't touched here —
        # the running orchestrator's reconciliation handles record drift,
        # not startup.
        assert result == 0
        assert pipeline.status == PipelineStatus.RUNNING
        store.save_pipeline.assert_not_called()
        for ci in phase.containers:
            assert ci.status == ContainerStatus.RUNNING
        for agent in phase.agents:
            assert agent.status == AgentExecutionStatus.RUNNING

    def test_record_drift_does_not_fail_pipeline_when_pods_alive(self):
        """Persisted container_id drift after orch restart leaves pipeline RUNNING.

        The exact #2411 scenario: after the orch pod restarts, agents have
        been re-recorded under new container_ids that don't match what was
        persisted before the restart.  The label-scoped k8s query reflects
        ground truth (alive pods exist for the pipeline), so the pipeline
        stays RUNNING — even though no in-memory ``container_id`` matches
        the global live-id set.
        """
        pipeline = _make_pipeline_with_running_agent("stale-id-from-before-restart")
        # k8s reports a different container_id for the same pipeline
        # (e.g. pod was recreated and got a new uid).
        new_pod_id = "new-pod-uid-after-restart"

        store = _make_store(pipeline)
        docker_client = _make_docker_client(
            live_ids=[new_pod_id],
            pipeline_live_map={pipeline.id: [new_pod_id]},
        )

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        assert pipeline.status == PipelineStatus.RUNNING
        store.save_pipeline.assert_not_called()

    def test_label_query_failure_leaves_pipeline_running(self):
        """If the per-pipeline label query raises, fail-safe and leave RUNNING.

        Pre-#2411 behavior was to mark the pipeline FAILED whenever any
        in-memory record had a stale container_id.  After #2411 we trust
        the label-scoped query as ground truth, but that means a failure
        in *just* the label query would silently fall back to the buggy
        behavior the PR is fixing.  The chosen trade-off is to fail-safe
        on the rare case where the global query succeeded but the
        label-scoped query errored — defer to the running orchestrator's
        reconciliation rather than re-introduce the #2411 false-positive
        on a misbehaving cluster.
        """
        pipeline = _make_pipeline_with_running_agent("dead_xyz")
        store = _make_store(pipeline)

        docker_client = MagicMock()
        global_live = [MagicMock(container_id="some_other_pipeline_id")]

        def _list_containers(all=True, labels=None):
            if labels and "egg.pipeline.id" in labels:
                raise RuntimeError("simulated k8s flake on label query")
            return global_live

        docker_client.list_containers.side_effect = _list_containers

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        assert pipeline.status == PipelineStatus.RUNNING
        store.save_pipeline.assert_not_called()

    def test_label_query_failure_with_id_drift_does_not_mark_failed(self):
        """The exact #2411 drift case under label-query failure stays RUNNING.

        Persisted ``container_id=stale-from-before-restart`` and the global
        live-id set contains only the new post-restart pod uid (i.e. the
        records have drifted).  If the per-pipeline label query then
        errors, the pre-#2411 fallback would have marked the pipeline
        FAILED — exactly the bug this PR exists to prevent.  Pin the
        fail-safe behavior with a test so a future maintainer cannot
        accidentally re-introduce the bait-and-switch.
        """
        pipeline = _make_pipeline_with_running_agent("stale-from-before-restart")
        new_pod_id = "new-pod-uid-after-restart"
        store = _make_store(pipeline)

        docker_client = MagicMock()
        global_live = [MagicMock(container_id=new_pod_id)]

        def _list_containers(all=True, labels=None):
            if labels and "egg.pipeline.id" in labels:
                raise RuntimeError("simulated k8s flake on label query")
            return global_live

        docker_client.list_containers.side_effect = _list_containers

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        assert pipeline.status == PipelineStatus.RUNNING
        store.save_pipeline.assert_not_called()

    def test_label_query_returns_empty_marks_pipeline_failed(self):
        """When k8s reports zero pods for the pipeline, it is marked FAILED.

        This is the genuinely-orphaned case: the pipeline shows RUNNING in
        the persisted state, but no pod with ``egg.pipeline.id=<id>`` is
        alive in the cluster.  The pipeline is correctly marked FAILED so
        operators see something actionable.
        """
        pipeline = _make_pipeline_with_running_agent("dead_xyz")
        store = _make_store(pipeline)
        # Other pipelines have live pods globally, but none are labeled to
        # this pipeline.
        docker_client = _make_docker_client(
            live_ids=["other_pipeline_pod"],
            pipeline_live_map={pipeline.id: []},
        )

        result = reconcile_stale_containers(store, docker_client)

        assert result == 1
        assert pipeline.status == PipelineStatus.FAILED

    def test_terminal_phase_pods_do_not_mask_orphaned_pipeline(self):
        """Pods in terminal phases (Failed/Succeeded) inside the Job's TTL
        window must NOT be counted as live (#2420 review item 1).

        Without the filter, a Failed pod still surviving in the cluster within
        ``ttlSecondsAfterFinished`` (default 600s) would be returned by the
        label query and cause the reconciler to leave a genuinely-orphaned
        pipeline RUNNING.  The filter must classify it as terminal so the
        pipeline is correctly marked FAILED.
        """
        pipeline = _make_pipeline_with_running_agent("dead_xyz")
        store = _make_store(pipeline)
        docker_client = _make_docker_client(
            live_ids=[],
            # Label query returns a pod, but that pod is in a terminal phase
            # — the mock helper assigns `pipeline_live_status` as the
            # ContainerStatus.  This simulates the post-failure-within-TTL
            # window the reviewer flagged.
            pipeline_live_map={pipeline.id: ["dead_xyz"]},
            pipeline_live_status=ContainerStatus.FAILED,
        )

        result = reconcile_stale_containers(store, docker_client)

        # The terminal-phase pod must NOT mask the orphan: pipeline gets FAILED.
        assert result == 1
        assert pipeline.status == PipelineStatus.FAILED

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
        refine_phase.started_at = datetime.now(UTC)
        refine_phase.containers.append(
            ContainerInfo(
                container_id="refine_dead_abc",
                container_name="egg-coder-refine",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
        refine_phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="refine_dead_abc",
                started_at=datetime.now(UTC),
            )
        )

        # Current phase (plan) has a live container
        plan_phase = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_phase.status = PipelineStatus.RUNNING
        plan_phase.started_at = datetime.now(UTC)
        plan_phase.containers.append(
            ContainerInfo(
                container_id="plan_live_xyz",
                container_name="egg-coder-plan",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
        plan_phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="plan_live_xyz",
                started_at=datetime.now(UTC),
            )
        )

        store = _make_store(pipeline)
        docker_client = _make_docker_client(
            live_ids=["plan_live_xyz"],
            pipeline_live_map={pipeline.id: ["plan_live_xyz"]},
        )

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
        refine_phase.started_at = datetime.now(UTC)
        refine_phase.containers.append(
            ContainerInfo(
                container_id="refine_dead_abc",
                container_name="egg-coder-refine",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
        refine_phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="refine_dead_abc",
                started_at=datetime.now(UTC),
            )
        )

        # Current phase (plan) has a dead container — should trigger FAILED
        plan_phase = pipeline.get_phase_execution(PipelinePhase.PLAN)
        plan_phase.status = PipelineStatus.RUNNING
        plan_phase.started_at = datetime.now(UTC)
        plan_phase.containers.append(
            ContainerInfo(
                container_id="plan_dead_xyz",
                container_name="egg-coder-plan",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
        plan_phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="plan_dead_xyz",
                started_at=datetime.now(UTC),
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
# Orphaned PENDING phase reconciliation (#2009)
# ---------------------------------------------------------------------------


def _make_pipeline_with_unspawned_phase(
    phase: PipelinePhase = PipelinePhase.REFINE,
) -> Pipeline:
    """Return a RUNNING pipeline whose current phase never reached spawn.

    Reproduces the state seen in #2009: the orchestrator created the pipeline
    + initial phase row, then crashed before `_run_pipeline` reached
    `executor.spawn_all`.  The phase row is left PENDING with no timestamps
    and no containers/agents.
    """
    pipeline = Pipeline(
        id="issue-2009",
        issue_number=2009,
        repo="owner/repo",
        branch="egg/issue-2009",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )
    phase_exec = pipeline.get_phase_execution(phase)
    # Defaults already match: status=PENDING, started_at=None, no containers/agents.
    assert phase_exec.status == PipelineStatus.PENDING
    assert phase_exec.started_at is None
    assert phase_exec.containers == []
    assert phase_exec.agents == []
    return pipeline


class TestReconcileOrphanedPendingPhase:
    """Tests for the #2009 startup recovery branch."""

    def test_orphaned_pending_phase_marked_failed(self):
        """RUNNING pipeline whose current phase never spawned is marked FAILED."""
        pipeline = _make_pipeline_with_unspawned_phase()
        store = _make_store(pipeline)
        docker_client = _make_docker_client([])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 1
        assert pipeline.status == PipelineStatus.FAILED
        assert pipeline.error is not None
        assert "never spawned" in pipeline.error
        assert "refine" in pipeline.error
        store.save_pipeline.assert_called_once_with(pipeline)

    def test_running_phase_with_started_at_left_alone(self):
        """A phase with `started_at` set is mid-spawn, not orphaned — leave alone."""
        pipeline = _make_pipeline_with_unspawned_phase()
        phase_exec = pipeline.get_phase_execution(pipeline.current_phase)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.started_at = datetime.now(UTC)

        store = _make_store(pipeline)
        docker_client = _make_docker_client([])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        assert pipeline.status == PipelineStatus.RUNNING
        store.save_pipeline.assert_not_called()

    def test_pending_phase_with_containers_left_to_container_loop(self):
        """If containers exist, the existing container-stale check handles it."""
        pipeline = _make_pipeline_with_unspawned_phase()
        phase_exec = pipeline.get_phase_execution(pipeline.current_phase)
        phase_exec.containers.append(
            ContainerInfo(
                container_id="live_abc",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )

        store = _make_store(pipeline)
        docker_client = _make_docker_client(
            live_ids=["live_abc"],
            pipeline_live_map={pipeline.id: ["live_abc"]},
        )

        result = reconcile_stale_containers(store, docker_client)

        # Not the orphaned-pending branch (containers exist), and the live
        # container check finds nothing stale → pipeline left alone.
        assert result == 0
        assert pipeline.status == PipelineStatus.RUNNING
        store.save_pipeline.assert_not_called()

    def test_orphaned_pending_phase_works_for_any_phase(self):
        """The recovery branch fires regardless of which phase is current."""
        pipeline = _make_pipeline_with_unspawned_phase(phase=PipelinePhase.IMPLEMENT)
        store = _make_store(pipeline)
        docker_client = _make_docker_client([])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 1
        assert pipeline.status == PipelineStatus.FAILED
        assert "implement" in pipeline.error

    def test_save_failure_does_not_increment_recovered(self):
        """If save_pipeline raises, recovered count is not incremented."""
        pipeline = _make_pipeline_with_unspawned_phase()
        store = _make_store(pipeline)
        store.save_pipeline.side_effect = Exception("disk full")
        docker_client = _make_docker_client([])

        result = reconcile_stale_containers(store, docker_client)

        assert result == 0
        store.save_pipeline.assert_called_once()


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


# ---------------------------------------------------------------------------
# Consensus reconstruction on startup
# ---------------------------------------------------------------------------

from unittest.mock import patch


class TestStartupConsensusReconstruction:
    """Tests for consensus tracker reconstruction during startup reconciliation."""

    def setup_method(self):
        with _trackers_lock:
            _trackers.pop("issue-concurrent", None)

    def teardown_method(self):
        with _trackers_lock:
            _trackers.pop("issue-concurrent", None)

    def test_reconstructs_tracker_for_running_concurrent_pipeline(self):
        """Startup should reconstruct consensus tracker for RUNNING concurrent pipelines."""

        pipeline = Pipeline(
            id="issue-concurrent",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        pipeline.config.concurrent_execution = True

        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING
        phase.started_at = datetime.now(UTC)
        phase.containers.append(
            ContainerInfo(
                container_id="live123",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
        phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="live123",
                started_at=datetime.now(UTC),
            )
        )

        store = _make_store(pipeline)
        docker_client = _make_docker_client(
            live_ids=["live123"],
            pipeline_live_map={pipeline.id: ["live123"]},
        )

        # The startup_reconciliation imports these functions locally, so we
        # patch them at their source modules.
        with (
            patch("peer_consensus.reconstruct_tracker_from_messages") as mock_reconstruct,
            patch("concurrent_executor.is_concurrent_execution", return_value=True),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
        ):
            mock_tracker = MagicMock()
            mock_tracker.evaluate.return_value = {"is_complete": False}
            mock_reconstruct.return_value = mock_tracker

            reconcile_stale_containers(store, docker_client)

            mock_reconstruct.assert_called_once()
            call_args = mock_reconstruct.call_args
            assert call_args[0][0] == "issue-concurrent"

    def test_marks_phase_complete_when_consensus_already_done(self):
        """If reconstructed tracker shows consensus complete, phase is marked COMPLETE."""

        pipeline = Pipeline(
            id="issue-concurrent",
            issue_number=42,
            repo="owner/repo",
            branch="egg/issue-42",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        pipeline.config.concurrent_execution = True

        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase.status = PipelineStatus.RUNNING
        phase.started_at = datetime.now(UTC)
        phase.containers.append(
            ContainerInfo(
                container_id="live123",
                container_name="egg-coder",
                status=ContainerStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
        )
        phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.RUNNING,
                container_id="live123",
                started_at=datetime.now(UTC),
            )
        )

        store = _make_store(pipeline)
        docker_client = _make_docker_client(
            live_ids=["live123"],
            pipeline_live_map={pipeline.id: ["live123"]},
        )

        mock_tracker = MagicMock()
        mock_tracker.evaluate.return_value = {"is_complete": True}

        with (
            patch("peer_consensus.reconstruct_tracker_from_messages") as mock_reconstruct,
            patch("concurrent_executor.is_concurrent_execution", return_value=True),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
        ):
            mock_reconstruct.return_value = mock_tracker

            reconcile_stale_containers(store, docker_client)

        # Phase should be marked COMPLETE with completed_at set
        assert phase.status == PipelineStatus.COMPLETE
        assert phase.completed_at is not None
        # Agent should be marked COMPLETE with completed_at set
        assert phase.agents[0].status == AgentExecutionStatus.COMPLETE
        assert phase.agents[0].completed_at is not None
        # save_pipeline should have been called
        store.save_pipeline.assert_called()
