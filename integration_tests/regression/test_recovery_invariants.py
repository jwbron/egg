"""Gap-audit integration tests for additional recovery/teardown invariants.

Beyond the starting points from #2633 (#2420 live-pod guard, #2429
salvage), these tests cover invariants that span multiple modules and
have historical regression risk:

* **Live-pod filter parity** between ``routes/pipelines._LIVE_POD_STATUSES``
  and ``startup_reconciliation``'s filter. After #2650 the two literals
  were hoisted to a single ``models.LIVE_POD_STATUSES`` constant, so the
  parity test now checks identity. Behavioral assertions remain to catch
  a future regression where either call-site stops importing through the
  shared constant.

* **Crash-between-submit-and-spawn recovery** (#2009): pipelines that
  reached RUNNING but whose current phase never spawned should be
  marked FAILED at startup so operators see something actionable
  instead of an indefinitely frozen pipeline.

* **Recovery ref immutability per HEAD SHA**: the salvage ref name
  embeds the short SHA so a re-salvage of the same HEAD is a no-op
  fast-forward, and a re-salvage after new commits gets a *new* ref
  instead of force-overwriting the prior one. The cleanup sweep is
  built on this guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from agent_salvage import RECOVERY_BRANCH_PREFIX, auto_salvage_pipeline
from gateway_client import PushResult
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

from ._helpers import commit as _commit_file
from ._helpers import git as _git
from ._helpers import make_repo as _make_repo
from ._helpers import set_assigned_branch as _set_assigned_branch

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Live-pod filter parity (routes/pipelines ↔ startup_reconciliation)
# ---------------------------------------------------------------------------


def _running_pipeline_with_persisted_container(
    pipeline_id: str = "issue-2420",
    container_id: str = "agent-pod-1",
) -> Pipeline:
    """A pipeline whose persisted state records one RUNNING container."""
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=2420,
        repo="owner/repo",
        branch=f"egg/{pipeline_id}",
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
            container_name="egg-coder",
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


def _docker_client_returning(*pod_statuses: ContainerStatus) -> MagicMock:
    """A mock docker client whose label-scoped query returns one pod per
    status. The unscoped query (``all=False``) returns no live cluster-
    wide containers — irrelevant once the label query takes precedence
    in the #2411 path.
    """
    client = MagicMock()

    def _list(all=True, labels=None):
        if labels and "egg.pipeline.id" in labels:
            return [
                MagicMock(container_id=f"pod-{i}", status=status)
                for i, status in enumerate(pod_statuses)
            ]
        return []

    client.list_containers.side_effect = _list
    return client


class TestLivePodFilterParity:
    """``startup_reconciliation`` and ``_count_live_pods_for_pipeline``
    must agree on which container statuses count as "live."

    After #2650 both modules import ``models.LIVE_POD_STATUSES`` so the
    constant is shared by identity. The behavioral tests below stay in
    place to catch a future regression where either call-site stops
    using the shared constant — if e.g. someone re-inlines a literal
    that omits ``CREATING``, the per-status parametrize would catch it
    even though the import-identity check would still pass.
    """

    def test_constant_is_shared_by_identity(self):
        """Both call-sites must reference the *same* tuple object.

        The previous duplication relied on a comment to keep the two
        literals in sync — a drift caused #2411. Asserting ``is`` (not
        ``==``) catches a regression where someone copy-pastes the
        constant back into one site.
        """
        from models import LIVE_POD_STATUSES
        from routes.pipelines import _LIVE_POD_STATUSES as route_filter
        from startup_reconciliation import _LIVE_POD_STATUSES as startup_filter

        assert route_filter is LIVE_POD_STATUSES
        assert startup_filter is LIVE_POD_STATUSES

    @pytest.mark.parametrize(
        "status",
        [ContainerStatus.PENDING, ContainerStatus.CREATING, ContainerStatus.RUNNING],
    )
    def test_live_pod_keeps_pipeline_running(self, status):
        """Any pod in a live status leaves the pipeline RUNNING."""
        pipeline = _running_pipeline_with_persisted_container()
        store = MagicMock()
        store.list_pipelines.return_value = [pipeline.id]
        store.load_pipeline.return_value = pipeline
        docker = _docker_client_returning(status)

        recovered = reconcile_stale_containers(store, docker)

        assert recovered == 0
        assert pipeline.status == PipelineStatus.RUNNING
        store.save_pipeline.assert_not_called()

    @pytest.mark.parametrize(
        "status",
        [ContainerStatus.FAILED, ContainerStatus.EXITED, ContainerStatus.REMOVED],
    )
    def test_terminal_pod_does_not_mask_orphaned_pipeline(self, status):
        """A terminal-only pod listing falls through to the
        per-container-id check, which marks the pipeline FAILED
        because the persisted container id isn't in the live cluster
        set. Mirrors what ``_count_live_pods_for_pipeline`` would
        report as ``live=0`` so the start_pipeline guard would allow
        a reset.
        """
        pipeline = _running_pipeline_with_persisted_container()
        store = MagicMock()
        store.list_pipelines.return_value = [pipeline.id]
        store.load_pipeline.return_value = pipeline
        # Label query returns one Failed pod; unscoped returns nothing.
        docker = _docker_client_returning(status)

        recovered = reconcile_stale_containers(store, docker)

        assert recovered == 1
        assert pipeline.status == PipelineStatus.FAILED
        # The pipeline-level error message points at restart via the
        # /start route, matching the contract ``start_pipeline``
        # exposes (the same route #2420 added the guard to).
        assert "POST /pipelines/{id}/start" in (pipeline.error or "")

    def test_mixed_statuses_count_only_live(self):
        """One RUNNING + one FAILED pod => pipeline left RUNNING.

        The live filter is OR-of-statuses, not AND. A single live pod
        suffices to defer to the running orchestrator. Regression we
        guard against: a status set narrowed to require ALL pods to
        be live before deferring.
        """
        pipeline = _running_pipeline_with_persisted_container()
        store = MagicMock()
        store.list_pipelines.return_value = [pipeline.id]
        store.load_pipeline.return_value = pipeline
        docker = _docker_client_returning(
            ContainerStatus.RUNNING,
            ContainerStatus.FAILED,
        )

        recovered = reconcile_stale_containers(store, docker)
        assert recovered == 0
        assert pipeline.status == PipelineStatus.RUNNING


# ---------------------------------------------------------------------------
# Crash-between-submit-and-spawn recovery (#2009)
# ---------------------------------------------------------------------------


class TestCrashBetweenSubmitAndSpawnRecovery:
    """A pipeline that reached RUNNING but whose current phase never
    spawned (no ``started_at``, no agents, no containers) must be marked
    FAILED at startup. Otherwise it sits indefinitely RUNNING with
    nothing to drive it forward and operators have no programmatic path
    to recover it.
    """

    def test_pending_phase_with_no_state_marked_failed(self):
        pipeline = Pipeline(
            id="issue-2009",
            issue_number=2009,
            repo="owner/repo",
            branch="egg/issue-2009",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        # PENDING + no started_at + no containers + no agents — the
        # crash-between-submit-and-spawn signature.
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        assert phase.status == PipelineStatus.PENDING
        assert phase.started_at is None
        assert phase.containers == []
        assert phase.agents == []

        store = MagicMock()
        store.list_pipelines.return_value = [pipeline.id]
        store.load_pipeline.return_value = pipeline
        docker = MagicMock()
        docker.list_containers.return_value = []

        recovered = reconcile_stale_containers(store, docker)
        assert recovered == 1
        assert pipeline.status == PipelineStatus.FAILED
        # Distinguish from the container-loop's FAILED message so
        # operators reading logs can tell which path fired.
        assert "never spawned" in (pipeline.error or "")

    def test_unspawned_guard_does_not_fire_when_agents_present(self):
        """The un-spawned-phase guard must NOT preemptively mark FAILED
        once agent records exist — even if no containers were created.

        ``test_pending_phase_with_no_state_marked_failed`` above asserts
        the guard fires on the bare "no agents, no containers" shape.
        This test asserts it does NOT fire as soon as the spawn loop
        wrote any agent record. A future tightening that loosened the
        predicate to capture partially-spawned phases (agents present
        but no containers) would over-fire and flip live RUNNING
        pipelines to FAILED — caught here.
        """
        pipeline = Pipeline(
            id="issue-2009b",
            issue_number=2009,
            repo="owner/repo",
            branch="egg/issue-2009b",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        phase = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        # Agents exist but no started_at and no containers — the
        # spawn loop started but didn't finish.
        phase.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.PENDING,
                started_at=None,
            )
        )

        store = MagicMock()
        store.list_pipelines.return_value = [pipeline.id]
        store.load_pipeline.return_value = pipeline
        docker = MagicMock()
        docker.list_containers.return_value = []

        recovered = reconcile_stale_containers(store, docker)
        # Guard didn't fire — pipeline still RUNNING, no save happened.
        assert recovered == 0
        assert pipeline.status == PipelineStatus.RUNNING


# ---------------------------------------------------------------------------
# Recovery-ref immutability (idempotent salvage)
# ---------------------------------------------------------------------------


def _seed_worktree(
    base: Path,
    pipeline_id: str,
    role: str,
    assigned: str,
) -> tuple[Path, str]:
    """Build a worktree with one local commit ahead of the anchor.

    Uses the shared ``_helpers`` git plumbing so this suite and the
    salvage suite agree on the worktree shape they exercise.
    """
    wid = f"{pipeline_id}-{role}"
    local = f"egg/{wid}/work"
    repo = base / wid / "repo"
    anchor = _make_repo(repo, local)
    _set_assigned_branch(repo, local, assigned)
    _git("update-ref", f"refs/remotes/origin/{assigned}", anchor, cwd=repo)
    head = _commit_file(repo, "a.txt", "a\n", "first unpushed")
    return repo, head


class TestRecoveryRefImmutability:
    """The recovery ref name embeds the short HEAD SHA, so two salvages
    against the same head land at the same ref. The cleanup sweep
    (``agent_salvage_cleanup``) relies on this so deletions are safely
    keyed off the committed-at of the ref's tip — if the ref name were
    derived from a clock or counter, a re-salvage would force-overwrite
    and the sweep's age check would point at the wrong commit.
    """

    def test_resalvage_same_head_uses_same_ref_name(self, tmp_path):
        repo, head = _seed_worktree(tmp_path, "issue-2429", "coder", "egg/issue-2429/work")
        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            first = auto_salvage_pipeline(gateway, "issue-2429")
            second = auto_salvage_pipeline(gateway, "issue-2429")

        assert first[0].ok and second[0].ok
        assert first[0].recovery_ref == second[0].recovery_ref
        # The ref name carries the short SHA from the actual HEAD.
        assert first[0].recovery_ref == (f"{RECOVERY_BRANCH_PREFIX}/issue-2429/coder/{head[:12]}")
        # The gateway saw exactly two pushes — neither was forced.
        assert gateway.push_worktree_branch.call_count == 2
        for call in gateway.push_worktree_branch.call_args_list:
            assert call.kwargs["force"] is False

    def test_new_commit_after_salvage_produces_new_ref(self, tmp_path):
        repo, first_head = _seed_worktree(tmp_path, "issue-2429", "coder", "egg/issue-2429/work")
        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            first = auto_salvage_pipeline(gateway, "issue-2429")

        # Agent makes another commit before the next salvage.
        new_head = _commit_file(repo, "b.txt", "b\n", "second unpushed")

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            second = auto_salvage_pipeline(gateway, "issue-2429")

        # Different refs — the original ref is still untouched and points
        # at first_head, the new ref points at the new HEAD.
        assert first[0].recovery_ref != second[0].recovery_ref
        assert first[0].recovery_ref == (
            f"{RECOVERY_BRANCH_PREFIX}/issue-2429/coder/{first_head[:12]}"
        )
        assert second[0].recovery_ref == (
            f"{RECOVERY_BRANCH_PREFIX}/issue-2429/coder/{new_head[:12]}"
        )
