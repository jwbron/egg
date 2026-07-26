"""A cancelled pipeline must stop spawning agents (issue #3633).

``cancel_task`` set the status to CANCELLED, tore down the pipeline's
containers, and cleared its runtime state — but it never stopped the thing
that *creates* containers. The ``_run_pipeline`` driver thread and each
slice's BRC event loop kept running in-process, so the next poll re-derived
its arms and spawned again: ``issue-3596-v2`` was cancelled at 20:48Z and
spawned slice-3 agents at 22:55Z, complete with a fresh integration branch.

These tests pin the four layers of the fix:

1. the cancel route stops every live BRC event loop, and does it *before*
   container cleanup (cleanup that races a live loop is removing pods the
   loop is entitled to replace);
2. a loop stopped mid-tick refuses the spawn it was about to request;
3. the concurrent-phase poll loop re-reads the persisted status and bails
   (without escalating, and without rewriting CANCELLED to FAILED);
4. the implement-phase slice loop refuses to admit another slice.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import routes.pipelines as pipelines_pkg
from event_loop import (
    _LIVE_LOOPS,
    OrchestratorEventLoop,
    _register_live_loop,
    _unregister_live_loop,
)
from flask import Flask
from models import (
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import pipelines_bp
from slice_scheduler import SchedulerSliceState

PIPELINE_ID = "issue-3633"


# ---------------------------------------------------------------------------
# Layer 1 — the cancel route stops live event loops, before cleanup
# ---------------------------------------------------------------------------


class _FakeLoop:
    """Minimal stand-in for a live ``OrchestratorEventLoop`` registry entry."""

    def __init__(self, pipeline_id: str, slice_id: str | None, log: list[str]) -> None:
        self.pipeline_id = pipeline_id
        self.slice_id = slice_id
        self._log = log
        self.stop_calls: list[float | None] = []

    def stop(self, *, join_timeout: float | None = 5.0) -> None:
        self.stop_calls.append(join_timeout)
        self._log.append(f"stop:{self.slice_id}")


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def live_loops():
    """Register fake loops for two concurrent slices; always clean up."""
    log: list[str] = []
    loops = [
        _FakeLoop(PIPELINE_ID, "slice-1", log),
        _FakeLoop(PIPELINE_ID, "slice-2", log),
        # A different pipeline's loop must be left alone.
        _FakeLoop("issue-other", "slice-1", log),
    ]
    for loop in loops:
        _register_live_loop(loop)
    try:
        yield loops, log
    finally:
        for loop in loops:
            _unregister_live_loop(loop)


def _cancellable_pipeline(status=PipelineStatus.RUNNING) -> Pipeline:
    return Pipeline(
        id=PIPELINE_ID,
        issue_number=3633,
        repo="owner/repo",
        branch=f"egg/{PIPELINE_ID}/work",
        status=status,
        current_phase=PipelinePhase.IMPLEMENT,
    )


def _patch_cancel_route(pipeline, cleanup_log, *, prev_status=PipelineStatus.RUNNING):
    """Patch the collaborators the PATCH-cancel path reaches out to.

    ``_resolve_pipeline`` yields the *pre-update* pipeline (whose status the
    route reads as ``prev_status``) and ``update_pipeline`` yields the
    post-update one — the transition the route gates on.
    """
    before = _cancellable_pipeline(status=prev_status)
    store = MagicMock()
    store.update_pipeline.return_value = pipeline
    store.load_pipeline.return_value = pipeline

    spawner = MagicMock()

    def _cleanup(pipeline_id, **kwargs):
        cleanup_log.append(f"cleanup:{pipeline_id}")
        return 0

    spawner.cleanup_pipeline.side_effect = _cleanup

    dq = MagicMock()
    dq.get_pending_decisions.return_value = []

    return (
        patch("routes.pipelines.get_repo_path", return_value="/repo"),
        patch("routes.pipelines._resolve_pipeline", return_value=(store, before)),
        patch("routes.pipelines.get_container_spawner", return_value=spawner),
        patch("routes.pipelines.get_decision_queue", return_value=dq),
    )


def test_cancel_stops_live_event_loops_before_container_cleanup(client, live_loops):
    """The regression: cancel must stop the spawner, not just its output.

    Both of this pipeline's slice loops are signalled, another pipeline's
    loop is untouched, and the stops are ordered ahead of the container
    cleanup so the teardown does not race a loop still entitled to spawn.
    """
    loops, log = live_loops
    pipeline = _cancellable_pipeline()
    pipeline.status = PipelineStatus.CANCELLED
    patches = _patch_cancel_route(pipeline, log)

    with patches[0], patches[1], patches[2], patches[3]:
        response = client.patch(
            f"/api/v1/pipelines/{PIPELINE_ID}",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200
        # Cleanup runs on a daemon thread; give it a moment to land.
        for _ in range(50):
            if any(entry.startswith("cleanup:") for entry in log):
                break
            threading.Event().wait(0.02)

    assert loops[0].stop_calls, "slice-1's event loop was never stopped"
    assert loops[1].stop_calls, "slice-2's event loop was never stopped"
    assert not loops[2].stop_calls, "another pipeline's event loop must not be stopped"

    # join_timeout=0.0: the PATCH runs in the request thread and must not
    # block the operator on a daemon thread's wind-down.
    assert loops[0].stop_calls == [0.0]

    stop_indices = [i for i, entry in enumerate(log) if entry.startswith("stop:")]
    cleanup_indices = [i for i, entry in enumerate(log) if entry.startswith("cleanup:")]
    assert cleanup_indices, "container cleanup never ran"
    assert max(stop_indices) < min(cleanup_indices), (
        "event loops must be stopped BEFORE container cleanup; cleanup that "
        "races a live loop just removes pods the loop will respawn"
    )


def test_cancel_evicts_loops_from_the_live_registry(client, live_loops):
    """A stopped loop is gone from the registry, so a re-cancel is a no-op."""
    loops, log = live_loops
    pipeline = _cancellable_pipeline()
    pipeline.status = PipelineStatus.CANCELLED
    patches = _patch_cancel_route(pipeline, log)

    # Real loops unregister inside stop(); the fakes do not, so drive the
    # real registry contract directly here.
    real = OrchestratorEventLoop(
        MagicMock(), MagicMock(), pipeline_id=PIPELINE_ID, slice_id="slice-real", phase="implement"
    )
    _register_live_loop(real)
    try:
        with patches[0], patches[1], patches[2], patches[3]:
            client.patch(f"/api/v1/pipelines/{PIPELINE_ID}", json={"status": "cancelled"})
        assert (PIPELINE_ID, "slice-real") not in _LIVE_LOOPS
    finally:
        _unregister_live_loop(real)


def test_recancel_of_an_already_cancelled_pipeline_does_not_re_stop(client, live_loops):
    """Gated on the transition: an idempotent re-cancel has nothing live left."""
    loops, log = live_loops
    pipeline = _cancellable_pipeline(status=PipelineStatus.CANCELLED)
    patches = _patch_cancel_route(pipeline, log, prev_status=PipelineStatus.CANCELLED)

    with patches[0], patches[1], patches[2], patches[3]:
        # ``prev_status`` is read off the resolved pipeline, which is already
        # CANCELLED — so this PATCH is not a transition.
        response = client.patch(
            f"/api/v1/pipelines/{PIPELINE_ID}",
            json={"status": "cancelled"},
        )
        assert response.status_code == 200

    assert not loops[0].stop_calls
    assert not loops[1].stop_calls


def test_failed_transition_leaves_the_event_loop_alone(client, live_loops):
    """FAILED is excluded: ``container_monitor`` marks live pipelines FAILED
    mid-phase and the poll loop recovers them to RUNNING (#1273). Tearing the
    loop down there would convert a recoverable transient into a dead run."""
    loops, log = live_loops
    pipeline = _cancellable_pipeline()
    pipeline.status = PipelineStatus.FAILED
    patches = _patch_cancel_route(pipeline, log)

    with patches[0], patches[1], patches[2], patches[3]:
        response = client.patch(
            f"/api/v1/pipelines/{PIPELINE_ID}",
            json={"status": "failed"},
        )
        assert response.status_code == 200

    assert not loops[0].stop_calls
    assert not loops[1].stop_calls


# ---------------------------------------------------------------------------
# Layer 2 — a loop stopped mid-tick refuses the spawn it was about to make
# ---------------------------------------------------------------------------


def _loop_with_stub_spawner(spawn_log):
    spawner = SimpleNamespace(
        spawn_event=lambda **kwargs: spawn_log.append(kwargs) or SimpleNamespace(container_id="c1")
    )
    tracker = MagicMock()
    return OrchestratorEventLoop(
        tracker,
        spawner,
        pipeline_id=PIPELINE_ID,
        slice_id="slice-3",
        phase="implement",
        roles=["coder"],
    )


def test_stopped_loop_refuses_to_spawn_mid_tick():
    """``stop()`` lands from another thread (the cancel route), so a tick
    already in flight must re-check before requesting a Job."""
    spawn_log: list[dict] = []
    loop = _loop_with_stub_spawner(spawn_log)

    with patch(
        "event_loop._derive_next_action",
        return_value=("propose", {"version": 1}, "reason"),
    ):
        # Sanity: with the loop running, this derivation *does* spawn.
        decision = loop._handle_role("coder")
        assert decision.spawned is True
        assert len(spawn_log) == 1

        loop._stop.set()
        blocked = loop._handle_role("coder")

    assert blocked.spawned is False
    assert blocked.blocked == "stopped"
    assert len(spawn_log) == 1, "a stopped loop spawned another one-shot Job"


def test_stopped_block_does_not_read_as_an_operator_wedge():
    """``blocked="stopped"`` is a teardown, not something an operator can
    resolve — it must not trip the arms-exhausted / arms-parked alerts."""
    spawn_log: list[dict] = []
    loop = _loop_with_stub_spawner(spawn_log)
    exhausted_alerts: list[tuple] = []
    parked_alerts: list[tuple] = []
    loop._arms_exhausted_notifier = lambda **kw: exhausted_alerts.append(kw)
    loop._arms_parked_notifier = lambda **kw: parked_alerts.append(kw)
    loop._stop.set()

    with patch(
        "event_loop._derive_next_action",
        return_value=("propose", {"version": 1}, "reason"),
    ):
        decisions = loop.poll_once(["coder"])

    assert [d.blocked for d in decisions] == ["stopped"]
    assert not exhausted_alerts
    assert not parked_alerts


# ---------------------------------------------------------------------------
# Layer 3 — the phase poll loop re-reads the persisted status
# ---------------------------------------------------------------------------


def _store_returning(pipeline):
    store = MagicMock()
    store.load_pipeline.return_value = pipeline
    return store


def test_phase_bail_reason_reports_cancellation():
    pipeline = _cancellable_pipeline(status=PipelineStatus.CANCELLED)
    epoch = pipeline.run_epoch or pipeline.created_at
    assert (
        pipelines_pkg._phase_bail_reason_impl(
            store=_store_returning(pipeline), pipeline_id=PIPELINE_ID, run_epoch=epoch
        )
        == "pipeline_cancelled"
    )


def test_phase_bail_reason_still_reports_supersession():
    """The pre-existing #3315 condition keeps working through the same load."""
    pipeline = _cancellable_pipeline()
    pipeline.run_epoch = datetime.now(UTC)
    stale = pipeline.run_epoch - timedelta(hours=1)
    assert (
        pipelines_pkg._phase_bail_reason_impl(
            store=_store_returning(pipeline), pipeline_id=PIPELINE_ID, run_epoch=stale
        )
        == "superseded_by_restart"
    )


def test_phase_bail_reason_is_none_for_a_healthy_run():
    pipeline = _cancellable_pipeline()
    epoch = pipeline.run_epoch or pipeline.created_at
    assert (
        pipelines_pkg._phase_bail_reason_impl(
            store=_store_returning(pipeline), pipeline_id=PIPELINE_ID, run_epoch=epoch
        )
        is None
    )


def test_phase_bail_reason_ignores_failed():
    """#1273: reconciliation can mark a live pipeline FAILED mid-poll and the
    consensus-complete branch recovers it. Bailing would break that."""
    pipeline = _cancellable_pipeline(status=PipelineStatus.FAILED)
    epoch = pipeline.run_epoch or pipeline.created_at
    assert (
        pipelines_pkg._phase_bail_reason_impl(
            store=_store_returning(pipeline), pipeline_id=PIPELINE_ID, run_epoch=epoch
        )
        is None
    )


def test_phase_bail_reason_tolerates_a_store_hiccup():
    """A transient load failure must never tear down a running phase."""
    store = MagicMock()
    store.load_pipeline.side_effect = OSError("state branch locked")
    assert (
        pipelines_pkg._phase_bail_reason_impl(
            store=store, pipeline_id=PIPELINE_ID, run_epoch=datetime.now(UTC)
        )
        is None
    )
    assert (
        pipelines_pkg._phase_bail_reason_impl(
            store=None, pipeline_id=PIPELINE_ID, run_epoch=datetime.now(UTC)
        )
        is None
    )


def test_pipeline_cancelled_helper():
    for status, expected in (
        (PipelineStatus.CANCELLED, True),
        (PipelineStatus.RUNNING, False),
        (PipelineStatus.FAILED, False),
    ):
        pipeline = _cancellable_pipeline(status=status)
        assert (
            pipelines_pkg._pipeline_cancelled(_store_returning(pipeline), PIPELINE_ID) is expected
        )

    broken = MagicMock()
    broken.load_pipeline.side_effect = RuntimeError("boom")
    assert pipelines_pkg._pipeline_cancelled(broken, PIPELINE_ID) is False
    assert pipelines_pkg._pipeline_cancelled(None, PIPELINE_ID) is False


# ---------------------------------------------------------------------------
# Layer 3b — a cancelled phase must not be rewritten to FAILED
# ---------------------------------------------------------------------------


def test_cancelled_phase_is_not_marked_failed():
    """``_run_concurrent_phase`` returns non-zero on the cancel bail. Falling
    through to the failure path would overwrite the operator's CANCELLED with
    FAILED — losing their intent and the CANCELLED-only worktree preservation
    (#1725) that ``restart_phase`` resumes from."""
    pipeline = _cancellable_pipeline()
    epoch = pipeline.run_epoch or pipeline.created_at

    # A real store re-reads from disk on every call, so each load yields a
    # fresh object carrying the persisted CANCELLED the cancel route wrote.
    # (Returning one shared object would let the phase-start "reset status to
    # RUNNING" write at the top of the cycle mask the cancel.)
    def _load(_pipeline_id):
        cancelled = _cancellable_pipeline(status=PipelineStatus.CANCELLED)
        cancelled.run_epoch = epoch
        return cancelled

    store = MagicMock()
    store.load_pipeline.side_effect = _load

    with patch.object(
        pipelines_pkg,
        "_run_concurrent_phase",
        return_value=(1, "Phase monitor thread exited: pipeline_cancelled."),
    ):
        _pipeline, _phase_exec, phase_failed, action = pipelines_pkg._run_phase_execution(
            pipeline,
            pipeline.get_phase_execution(PipelinePhase.PLAN),
            False,
            certs_volume=None,
            current_phase=PipelinePhase.PLAN,
            gateway_mode="public",
            pipeline_id=PIPELINE_ID,
            pipeline_mode="issue",
            repo_volumes={},
            repos=["owner/repo"],
            run_epoch=epoch,
            sandbox_env={},
            spawner=MagicMock(),
            store=store,
            worktree_repo_path=pipelines_pkg.Path("/tmp/does-not-matter"),
        )

    assert action == "return", "the driver thread must exit cleanly on a cancel"
    assert phase_failed is False
    saved_statuses = [call.args[0].status for call in store.save_pipeline.call_args_list]
    assert PipelineStatus.FAILED not in saved_statuses, (
        "a cancelled phase must not be persisted as FAILED"
    )


# ---------------------------------------------------------------------------
# Layer 4 — the slice loop stops admitting slices
# ---------------------------------------------------------------------------


def _count_ready(scheduler) -> list:
    """Record that the loop read the ready set, and hand back an empty wave
    so the control test never enters the spawn machinery."""
    scheduler.iter_ready_calls += 1
    return []


def _pending_slice():
    """The un-admitted slice the cancelled pipeline must never reach."""
    from egg_contracts.models import Slice

    return Slice(
        id="slice-3",
        name="slice-3",
        goal="the slice a cancelled pipeline must not admit",
        dependencies=["slice-1"],
    )


class _StubScheduler:
    """Scheduler with one un-admitted slice left — the #3633 shape."""

    def __init__(self, *_a, **_kw) -> None:
        self.iter_ready_calls = 0
        self.spawned: list[str] = []

    def all_done(self) -> bool:
        return False

    def iter_ready(self):
        self.iter_ready_calls += 1
        return iter([("slice-3", "slice-1")])

    def mark_spawned(self, slice_id: str) -> None:
        self.spawned.append(slice_id)

    def record_complete(self, slice_id: str) -> None:  # pragma: no cover - unused
        pass

    def list_slices(self):
        # Real ``SchedulerSliceState``, not the bare string: the guard's
        # ``rt.state != SchedulerSliceState.COMPLETE`` comprehension must be
        # exercised against the enum production actually yields.
        return [SimpleNamespace(slice_id="slice-3", state=SchedulerSliceState.READY)]

    def poll_cascades(self):  # pragma: no cover - unused
        return []


def test_slice_loop_admits_nothing_after_a_cancel():
    """The reported failure, end to end: a pipeline cancelled mid-implement
    with an un-admitted slice remaining must not admit it, must not create
    its integration branch, and must not spawn its agent cohort."""
    scheduler = _StubScheduler()
    contract = SimpleNamespace(slices=[_pending_slice()])
    pipeline = SimpleNamespace(
        # ``repo=None`` skips the origin-side bootstrap probe so the test
        # never reaches the gateway.
        repo=None,
        branch=f"egg/{PIPELINE_ID}/work",
        issue_number=3633,
        current_phase=PipelinePhase.IMPLEMENT,
        config=SimpleNamespace(max_parallel_slices=2),
    )
    cancelled = _cancellable_pipeline(status=PipelineStatus.CANCELLED)
    store = MagicMock()
    store.load_pipeline.return_value = cancelled

    spawner = MagicMock()
    reconciler_stop = threading.Event()

    with (
        patch("orchestrator.slice_scheduler.SliceScheduler", lambda *a, **kw: scheduler),
        patch("slice_scheduler.SliceScheduler", lambda *a, **kw: scheduler),
        patch("egg_contracts.loader.load_contract", return_value=contract),
        patch("egg_contracts.loader.save_contract"),
        patch.object(pipelines_pkg, "_open_context_pr_safety_net_impl", return_value=None),
        patch.object(pipelines_pkg, "_classify_non_complete_slice", return_value="fresh"),
        patch.object(
            pipelines_pkg,
            "_start_stacked_pr_reconciler",
            return_value=(MagicMock(), reconciler_stop),
        ),
    ):
        exit_code, logs = pipelines_pkg._run_implement_phase_slices(
            PIPELINE_ID,
            pipeline,
            spawner=spawner,
            repo_volumes={},
            gateway_mode="public",
            repos=[],
            sandbox_env={},
            store=store,
            certs_volume=None,
            worktree_repo_path=pipelines_pkg.Path("/tmp/does-not-matter"),
            # Production (``_run_phase.py``) always threads the owning
            # thread's epoch through; pass it so this covers the real
            # configuration rather than the ``None`` default.
            run_epoch=cancelled.run_epoch or cancelled.created_at,
        )

    assert exit_code == 1
    assert "pipeline cancelled" in logs
    assert scheduler.iter_ready_calls == 0, "a cancelled pipeline read the ready set"
    assert scheduler.spawned == [], "a cancelled pipeline admitted a slice"
    assert spawner.gateway.create_slice_integration_branch.call_count == 0
    assert spawner.spawn_agent_job.call_count == 0
    assert reconciler_stop.is_set(), "the stacked-PR reconciler must be torn down"


def test_slice_loop_keeps_running_while_the_pipeline_is_running():
    """Control: the guard must not stop a healthy run."""
    scheduler = _StubScheduler()
    contract = SimpleNamespace(slices=[_pending_slice()])
    pipeline = SimpleNamespace(
        repo=None,
        branch=f"egg/{PIPELINE_ID}/work",
        issue_number=3633,
        current_phase=PipelinePhase.IMPLEMENT,
        config=SimpleNamespace(max_parallel_slices=2),
    )
    running = _cancellable_pipeline()
    store = MagicMock()
    store.load_pipeline.return_value = running

    # Let the loop reach the ready-set read once, then claim completion so
    # it exits without running a slice through the spawn machinery.
    calls = {"n": 0}

    def _all_done() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    scheduler.all_done = _all_done  # type: ignore[method-assign]
    scheduler.iter_ready = lambda: iter(_count_ready(scheduler))  # type: ignore[method-assign]

    with (
        patch("orchestrator.slice_scheduler.SliceScheduler", lambda *a, **kw: scheduler),
        patch("slice_scheduler.SliceScheduler", lambda *a, **kw: scheduler),
        patch("egg_contracts.loader.load_contract", return_value=contract),
        patch("egg_contracts.loader.save_contract"),
        patch.object(pipelines_pkg, "_open_context_pr_safety_net_impl", return_value=None),
        patch.object(pipelines_pkg, "_classify_non_complete_slice", return_value="fresh"),
        patch.object(
            pipelines_pkg,
            "_start_stacked_pr_reconciler",
            return_value=(MagicMock(), threading.Event()),
        ),
    ):
        pipelines_pkg._run_implement_phase_slices(
            PIPELINE_ID,
            pipeline,
            spawner=MagicMock(),
            repo_volumes={},
            gateway_mode="public",
            repos=[],
            sandbox_env={},
            store=store,
            certs_volume=None,
            worktree_repo_path=pipelines_pkg.Path("/tmp/does-not-matter"),
            run_epoch=running.run_epoch or running.created_at,
        )

    assert scheduler.iter_ready_calls >= 1, "the guard stopped a RUNNING pipeline"
