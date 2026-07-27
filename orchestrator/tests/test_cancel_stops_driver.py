"""A cancelled pipeline must stop spawning agents (issue #3633).

``cancel_task`` set the status to CANCELLED, tore down the pipeline's
containers, and cleared its runtime state — but it never stopped the thing
that *creates* containers. The ``_run_pipeline`` driver thread and each
slice's BRC event loop kept running in-process, so the next poll re-derived
its arms and spawned again: ``issue-3596-v2`` was cancelled at 20:48Z and
spawned slice-3 agents at 22:55Z, complete with a fresh integration branch.

These tests pin the five layers of the fix:

1. the cancel route stops every live BRC event loop, and does it *before*
   container cleanup (cleanup that races a live loop is removing pods the
   loop is entitled to replace);
2. a loop stopped mid-tick refuses the spawn it was about to request;
3. the concurrent-phase poll loop re-reads the persisted status and bails
   (without escalating, and without rewriting CANCELLED to FAILED);
4. the implement-phase slice loop refuses to admit another slice;
5. the refine/plan HITL gate — the one path that *overwrites* the persisted
   status the four layers above key on — bails on its own signal (a cancelled
   decision) instead of reading the operator's cancel as an approval.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    DecisionStatus,
    HITLDecision,
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
            # thread's epoch through, so pass it rather than leaning on the
            # ``None`` default. It is not what this test exercises: layer 4
            # keys on ``_pipeline_cancelled`` alone and bails before
            # ``_run_concurrent_phase_with_impasse_retry``, where the epoch
            # arm lives. Supersession is covered by
            # ``test_phase_bail_reason_still_reports_supersession``.
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
            # As above: production-faithful, but not the epoch arm under
            # test — ``all_done()`` short-circuits before the retry wrapper.
            run_epoch=running.run_epoch or running.created_at,
        )

    assert scheduler.iter_ready_calls >= 1, "the guard stopped a RUNNING pipeline"


# ---------------------------------------------------------------------------
# Layer 5 — the HITL gate bails instead of reading a cancel as an approval
# ---------------------------------------------------------------------------
#
# The four layers above all key on the *persisted* status. The refine/plan
# gate is the one path that overwrites that status before they read it: it
# parks at AWAITING_HUMAN, blocks in ``wait_for_decision``, and the operator's
# cancel unblocks it by cancelling the decision — leaving no ``resolution``.
# An unset resolution reads as an approval (``"" in _APPROVE_KEYWORDS``), so
# the gate took its "Approved — resume and advance" branch, wrote RUNNING over
# the operator's CANCELLED, and let the driver advance into the next phase and
# mint a fresh cohort: #3633 verbatim, through the one door the
# persisted-status layers cannot watch.


def _gate_pipeline(status=PipelineStatus.AWAITING_HUMAN) -> Pipeline:
    """A pipeline parked at the plan gate with a pending phase_gate decision.

    The pending decision routes the gate down its ``existing_pending_gate``
    branch, so the test reaches ``wait_for_decision`` without touching draft
    reads or the decision queue's create path.
    """
    pipeline = Pipeline(
        id=PIPELINE_ID,
        issue_number=3633,
        repo="owner/repo",
        branch=f"egg/{PIPELINE_ID}/work",
        status=status,
        current_phase=PipelinePhase.PLAN,
    )
    pipeline.decisions = [
        HITLDecision(
            id="gate-1",
            question="Approve the plan?",
            decision_type="phase_gate",
            phase=PipelinePhase.PLAN,
            status=DecisionStatus.PENDING,
        )
    ]
    return pipeline


def _gate_decision(status, resolution=None) -> HITLDecision:
    return HITLDecision(
        id="gate-1",
        question="Approve the plan?",
        decision_type="phase_gate",
        phase=PipelinePhase.PLAN,
        status=status,
        resolution=resolution,
    )


class _StatusCell:
    """The pipeline status the fake store persists, mutable mid-run.

    A cancel is not a value the gate is handed — it is a write that lands on
    the store while the gate is parked in ``wait_for_decision``. Tests for the
    later bail sites flip this from inside a hook to model exactly that.
    """

    def __init__(self, status):
        self.status = status

    def cancel(self):
        self.status = PipelineStatus.CANCELLED


def _run_gate(
    resolved_decision,
    *,
    persisted_status=None,
    cell=None,
    pipeline=None,
    ledger_status=("", False, None, {}),
    attestation=None,
    bridge=None,
    on_wait=None,
    on_draft=None,
    dq=None,
):
    """Drive ``_run_hitl_gate_converge`` to its phase-gate wait and back.

    ``load_pipeline`` hands out a *fresh* object per call, as the real store
    does: the gate mutates what it loads, and a shared MagicMock return value
    would let its own ``AWAITING_HUMAN`` write mask the persisted status the
    bail re-reads.

    ``save_pipeline`` writes the status back into the cell, which is what makes
    the lost-update mode testable at all: in the real store a park write
    *becomes* the persisted status, so a gate that parks AWAITING_HUMAN over a
    cancel and then re-reads would read back its own write and sail on. A fake
    that only records saves without applying them can never reproduce that.

    The hooks steer the gate onto whichever branch's bail is under test:
    ``ledger_status`` is the ``_collect_decision_ledger_status`` 4-tuple (set
    ``missing=True`` for the backstop, ``explicit_none`` for the attestation
    gate), ``attestation`` / ``bridge`` stand in for the two helpers that block
    on their own waits, and ``on_wait`` fires on every ``wait_for_decision``.
    Each receives the ``_StatusCell`` so it can cancel from inside the wait.

    ``on_draft`` fires from ``_read_phase_draft``, which is the *pre*-wait
    window: it sits after the phase's own cancel bail and before both the
    create arm's ``queue_decision`` and the park write, so it models a cancel
    landing during the seconds-to-minutes of git IO the gate does before it
    ever blocks — the window the post-wait checks cannot see.

    Pass ``dq`` to supply your own decision-queue mock when a test needs to
    assert on what the gate did — or did not — queue.
    """
    cell = cell if cell is not None else _StatusCell(persisted_status)
    saved: list[PipelineStatus] = []
    store = MagicMock()
    store.load_pipeline.side_effect = lambda _pid: _gate_pipeline(status=cell.status)

    def _save(p, *_args, **_kwargs):
        saved.append(p.status)
        cell.status = p.status

    store.save_pipeline.side_effect = _save

    waits: list[str] = []

    def _wait(decision_id):
        waits.append(decision_id)
        if on_wait is not None:
            on_wait(len(waits), cell)
        return resolved_decision

    dq = dq if dq is not None else MagicMock()
    dq.wait_for_decision.side_effect = _wait
    dq.get_decision.return_value = resolved_decision

    def _attestation(**kwargs):
        if attestation is not None:
            attestation(cell)
        return False, "", kwargs["pipeline"]

    def _bridge(*args, **kwargs):
        if bridge is not None:
            return bridge(cell)
        return 0

    def _draft(*_args, **_kwargs):
        if on_draft is not None:
            on_draft(cell)
        return "draft body"

    with (
        patch.object(
            pipelines_pkg,
            "_collect_decision_ledger_status",
            return_value=ledger_status,
        ),
        patch.object(pipelines_pkg, "_persist_decision_ledger_summary", return_value=None),
        patch.object(
            pipelines_pkg,
            "_handle_explicit_none_attestation_gate",
            side_effect=_attestation,
        ),
        patch.object(pipelines_pkg, "get_decision_queue", return_value=dq),
        patch.object(pipelines_pkg, "get_pipeline_state_lock"),
        patch.object(pipelines_pkg, "report_pipeline_status"),
        patch.object(pipelines_pkg, "_emit_pipeline_event"),
        patch.object(pipelines_pkg, "_read_phase_draft", side_effect=_draft),
        patch.object(pipelines_pkg, "_read_human_phase_draft", return_value=None),
        patch.object(pipelines_pkg, "_queue_and_await_contract_decisions", side_effect=_bridge),
        patch.object(pipelines_pkg, "_persist_phase_gate_resolution"),
        patch.object(pipelines_pkg, "_commit_statefiles_to_worktree"),
    ):
        _pipeline, action = pipelines_pkg._run_hitl_gate_converge(
            pipeline if pipeline is not None else _gate_pipeline(),
            current_phase=PipelinePhase.PLAN,
            gateway_mode="public",
            pipeline_id=PIPELINE_ID,
            repo_path=Path("/repo"),
            spawner=MagicMock(),
            store=store,
            worktree_repo_path=Path("/tmp/egg-worktree"),
        )
    return action, saved


def test_gate_bails_when_the_cancel_route_cancels_its_decision():
    """The headline regression: cancelling a pipeline parked at a HITL gate
    must not resurrect it to RUNNING and advance the phase."""
    # What ``cancel_decision`` leaves behind: CANCELLED, no resolution.
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.CANCELLED),
        persisted_status=PipelineStatus.CANCELLED,
    )

    assert action == "break", "the gate must exit the driver loop on a cancel"
    assert PipelineStatus.RUNNING not in saved, (
        "the gate rewrote the operator's CANCELLED back to RUNNING"
    )
    # Stronger than "no RUNNING": the gate must not persist *any* non-terminal
    # status over CANCELLED. The park write is a status write too, and because
    # the fake now applies saves back onto the cell, an AWAITING_HUMAN park here
    # would be read back by the post-wait check as "not cancelled" and the gate
    # would advance — the lost-update mode the in-lock park check closes.
    assert saved == [], f"the gate persisted {saved} over the operator's CANCELLED"


def test_gate_bails_on_a_cancelled_pipeline_even_if_the_decision_resolved():
    """A cancel that lands after the decision-queue sweep — or one racing an
    operator who resolved the gate — must still bail: the persisted status is
    checked alongside the decision's own."""
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.RESOLVED, "approve"),
        persisted_status=PipelineStatus.CANCELLED,
    )

    assert action == "break"
    assert PipelineStatus.RUNNING not in saved


def test_gate_still_advances_a_genuine_approval():
    """Control: a real approval on a live pipeline still advances. Without
    this, ``return "break"`` unconditionally would pass the two above."""
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.RESOLVED, "approve"),
        persisted_status=PipelineStatus.AWAITING_HUMAN,
    )

    assert action is None, "an approved gate must fall through and advance"
    assert PipelineStatus.RUNNING in saved


def test_gate_still_advances_when_only_the_decision_was_cancelled():
    """A *lone* decision cancel on a live pipeline is not a pipeline cancel.

    ``routes/decisions/_lifecycle.py`` exposes a standalone endpoint that
    cancels one decision without touching the pipeline. Keying the bail on the
    returned decision's status would read that as a stop, exit the driver, and
    strand a live pipeline at AWAITING_HUMAN with no waiter — which is why the
    bail consults the persisted pipeline status only.
    """
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.CANCELLED),
        persisted_status=PipelineStatus.AWAITING_HUMAN,
    )

    assert action is None, "a lone decision cancel must not break the driver loop"
    assert PipelineStatus.RUNNING in saved


def test_gate_bails_when_cancelled_at_the_ledger_backstop():
    """The decision-ledger backstop (#3390) blocks on its own wait before the
    phase gate is ever queued. A cancel landing there must not fall through to
    the gate — the ``proceed`` branch treats a non-RESOLVED backstop as an
    operator override and walks straight into the phase gate below."""
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.CANCELLED),
        persisted_status=PipelineStatus.AWAITING_HUMAN,
        ledger_status=("no ledger", True, None, {}),
        on_wait=lambda n, cell: cell.cancel(),
    )

    assert action == "break"
    assert PipelineStatus.RUNNING not in saved


def test_gate_bails_when_cancelled_at_the_attestation_gate():
    """The explicit-none attestation gate (#3462) "fails open to the phase
    gate" on any non-RESOLVED status — safe for a cancelled attestation, not
    safe for a cancelled pipeline. Falling through would queue a *fresh*
    phase_gate decision minted after the cancel route already swept the queue,
    so nothing would ever cancel it and the wait below would never return."""
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.RESOLVED, "approve"),
        persisted_status=PipelineStatus.AWAITING_HUMAN,
        ledger_status=("attested none", False, ("coder", "abc1234", []), {}),
        attestation=lambda cell: cell.cancel(),
    )

    assert action == "break"
    assert PipelineStatus.RUNNING not in saved


def test_gate_bails_when_cancelled_at_the_followup_specifics():
    """A bare "request changes" queues a follow-up asking for specifics and
    blocks on it. That wait is swept by a cancel exactly like the gate's own,
    and an unset follow-up resolution reads as an approval."""
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.RESOLVED, "request changes"),
        persisted_status=PipelineStatus.AWAITING_HUMAN,
        # Wait 1 is the phase gate (still live); wait 2 is the follow-up.
        on_wait=lambda n, cell: cell.cancel() if n == 2 else None,
    )

    assert action == "break"
    assert PipelineStatus.RUNNING not in saved


def test_gate_bails_when_cancelled_bridging_contract_decisions():
    """The contract-decision bridge (#1889) is the longest human-latency
    window in the gate — one blocking wait per contract question. A cancel
    there leaves every answer unresolved, so the converge branch is skipped and
    control falls through to "Approved — resume and advance"."""
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.RESOLVED, "approve"),
        persisted_status=PipelineStatus.AWAITING_HUMAN,
        bridge=lambda cell: (cell.cancel(), 0)[1],
    )

    assert action == "break"
    assert PipelineStatus.RUNNING not in saved, (
        "the gate advanced the phase after the operator cancelled mid-bridge"
    )


def _gate_pipeline_without_a_pending_gate() -> Pipeline:
    """A pipeline at the plan gate with no pending decision — the *create* arm.

    The reuse arm waits on a decision the cancel route's sweep already reached;
    the create arm mints a new one, which is the arm that can leave an orphan.
    """
    pipeline = _gate_pipeline()
    pipeline.decisions = []
    return pipeline


def test_gate_never_mints_a_decision_for_a_cancelled_pipeline():
    """The create arm must not queue a gate for a run the operator stopped.

    The cancel route's sweep of pending decisions is a one-time snapshot, so a
    decision minted after it runs is never cancelled — and
    ``DecisionQueue.wait_for_decision`` is a ``while True`` poll with no
    timeout. Queueing here parks the driver thread for the lifetime of the
    process: ``_run_pipeline``'s ``finally`` never runs, so there is no
    container cleanup and no ``skip_cleanup`` worktree preservation, and the
    operator's PATCH already returned 200. That is strictly worse than the
    pre-#3633 behaviour on the same input, where the gate at least fell
    through — which is why the check goes *before* ``queue_decision``.
    """
    dq = MagicMock()
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.CANCELLED),
        persisted_status=PipelineStatus.AWAITING_HUMAN,
        pipeline=_gate_pipeline_without_a_pending_gate(),
        # The cancel lands while the gate is reading the draft — after the
        # phase's own bail, before the queue.
        on_draft=lambda cell: cell.cancel(),
        dq=dq,
    )

    assert action == "break"
    assert dq.queue_decision.call_args_list == [], (
        "the gate minted a decision nothing will ever cancel"
    )
    dq.wait_for_decision.assert_not_called()
    assert saved == []


def test_gate_park_does_not_overwrite_a_cancel_that_lands_before_it():
    """The reuse arm's park write must not clobber the operator's CANCELLED.

    This is #3633 verbatim, through the half of the window the post-wait checks
    cannot see. The park write is unconditional on both arms, so a cancel
    landing before it is overwritten with ``AWAITING_HUMAN``; the decision was
    pending at sweep time so the wait returns at once with no resolution; the
    post-wait check re-reads the store and sees the gate's *own* write, not the
    cancel; and ``"" in _APPROVE_KEYWORDS`` sends the run down "Approved —
    resume and advance". Hence the in-lock check in
    ``_park_at_gate_unless_cancelled``: ``StateStore.update_pipeline`` takes
    the same per-pipeline lock, so checking inside it is what makes the read
    and the write atomic against the cancel route.
    """
    dq = MagicMock()
    action, saved = _run_gate(
        # A resolution that *would* advance the phase if the bail were missed.
        _gate_decision(DecisionStatus.RESOLVED, "approve"),
        persisted_status=PipelineStatus.AWAITING_HUMAN,
        on_draft=lambda cell: cell.cancel(),
        dq=dq,
    )

    assert action == "break"
    assert saved == [], f"the park wrote {saved} over the operator's CANCELLED"
    assert dq.wait_for_decision.call_args_list == [], (
        "the gate blocked on a decision belonging to a cancelled run"
    )


def test_park_at_gate_unless_cancelled_checks_inside_the_lock():
    """Unit coverage for the shared park helper.

    The ordering assertion is the point: a check that merely runs *just before*
    the write still races ``update_pipeline``, which holds the same
    per-pipeline lock. Only a check taken after the lock is acquired and before
    the save is atomic against the cancel route.
    """
    events: list[str] = []

    lock = MagicMock()
    lock.__enter__.side_effect = lambda: events.append("enter")
    lock.__exit__.side_effect = lambda *_a: events.append("exit")

    def _store_for(pipeline):
        store = MagicMock()
        store.load_pipeline.side_effect = lambda _pid: (events.append("load"), pipeline)[1]
        store.save_pipeline.side_effect = lambda *_a, **_k: events.append("save")
        return store

    live = _store_for(_cancellable_pipeline())
    with patch.object(pipelines_pkg, "get_pipeline_state_lock", return_value=lock):
        pipeline, cancelled = pipelines_pkg._park_at_gate_unless_cancelled(
            live, PIPELINE_ID, PipelinePhase.PLAN
        )

    assert cancelled is False
    assert pipeline.status == PipelineStatus.AWAITING_HUMAN
    assert events == ["enter", "load", "save", "exit"], (
        "the status read must sit inside the lock that performs the write"
    )

    events.clear()
    dead = _store_for(_cancellable_pipeline(status=PipelineStatus.CANCELLED))
    with patch.object(pipelines_pkg, "get_pipeline_state_lock", return_value=lock):
        pipeline, cancelled = pipelines_pkg._park_at_gate_unless_cancelled(
            dead, PIPELINE_ID, PipelinePhase.PLAN
        )

    assert cancelled is True
    assert pipeline.status == PipelineStatus.CANCELLED
    assert events == ["enter", "load", "exit"], "a cancelled run must not be written to"


def test_bare_request_changes_on_a_reused_gate_reaches_the_followup():
    """Regression for the reuse path: ``draft_content`` / ``phase_label`` used
    to be bound only on the create-a-new-gate arm, so resuming onto an existing
    pending gate and answering with a bare "request changes" raised
    ``UnboundLocalError`` before the follow-up could be queued."""
    waits: list[int] = []
    action, saved = _run_gate(
        _gate_decision(DecisionStatus.RESOLVED, "request changes"),
        persisted_status=PipelineStatus.AWAITING_HUMAN,
        on_wait=lambda n, cell: waits.append(n),
    )

    assert waits == [1, 2], "the follow-up asking for specifics was never queued"
    # The follow-up came back bare too, which the gate reads as an approval.
    assert action is None
    assert PipelineStatus.RUNNING in saved


def test_gate_wait_cancelled_helper():
    """Unit coverage for the seam itself, including the store-hiccup
    tolerance it inherits from ``_pipeline_cancelled``."""
    live = _store_returning(_cancellable_pipeline())
    dead = _store_returning(_cancellable_pipeline(status=PipelineStatus.CANCELLED))

    # A cancelled pipeline bails. Both real cancel paths persist CANCELLED
    # before sweeping the decision queue, so a swept wait always sees it.
    assert pipelines_pkg._gate_wait_cancelled(dead, PIPELINE_ID) is True
    # A live pipeline proceeds.
    assert pipelines_pkg._gate_wait_cancelled(live, PIPELINE_ID) is False
    # FAILED is not a cancel (#1273): container_monitor can mark a live
    # pipeline FAILED mid-gate and the consensus-complete path recovers it.
    assert (
        pipelines_pkg._gate_wait_cancelled(
            _store_returning(_cancellable_pipeline(status=PipelineStatus.FAILED)),
            PIPELINE_ID,
        )
        is False
    )
    # A store hiccup must never invent a cancel and strand an approved gate.
    broken = MagicMock()
    broken.load_pipeline.side_effect = RuntimeError("state branch locked")
    assert pipelines_pkg._gate_wait_cancelled(broken, PIPELINE_ID) is False


# ---------------------------------------------------------------------------
# The pre-spawn guard sits between executor construction and spawn_all
# ---------------------------------------------------------------------------


def test_pre_spawn_guard_runs_before_spawn_all():
    """A cancel landing in the prompt-build/session-setup window must stop the
    cohort being minted at all.

    That window is tens of seconds wide, and a cancel inside it runs the
    route's teardown BEFORE these Jobs exist — nothing would reap them, since
    no reconciler acts on CANCELLED and ``cleanup_pipeline`` only re-runs on
    an operator DELETE. So the guard has to be the last thing before
    ``spawn_all``, not merely present somewhere in the function.
    """
    cancelled = _cancellable_pipeline(status=PipelineStatus.CANCELLED)
    cancelled.base_branch = "main"
    cancelled.current_phase = PipelinePhase.PLAN
    store = MagicMock()
    store.load_pipeline.return_value = cancelled

    executor = MagicMock()
    spawner = MagicMock()

    with (
        patch("concurrent_executor.ConcurrentPhaseExecutor", return_value=executor),
        patch.object(pipelines_pkg, "_build_agent_prompt", return_value="prompt"),
    ):
        exit_code, logs = pipelines_pkg._run_concurrent_phase(
            PIPELINE_ID,
            cancelled,
            "plan",
            spawner,
            {},
            "public",
            ["owner/repo"],
            {},
            store,
            None,
            Path("/tmp/egg-worktree"),
        )

    assert exit_code == 1
    assert "pipeline_cancelled" in logs
    executor.spawn_all.assert_not_called()
    # The executor owns a live event loop the moment it is constructed, so
    # bailing without stopping it would leak the very thing layer 2 stops.
    executor.stop_event_loop.assert_called_once()


# ---------------------------------------------------------------------------
# Layer 6 — the unresolved-gap gate's bail has to reach the driver
# ---------------------------------------------------------------------------
#
# The gap gate is the one park-and-resume block whose caller, not the block
# itself, decides the pipeline's fate. Skipping the RUNNING write leaves the
# operator's CANCELLED intact for exactly as long as it takes control to
# return to ``_run_pipeline``: IMPLEMENT is terminal
# (``PHASE_TRANSITIONS[IMPLEMENT] == []``), so an unstopped driver walks
# straight into its "pipeline complete" branch, writes COMPLETE over the
# cancel, broadcasts "Pipeline completed successfully", and — now that the
# ``finally`` no longer sees CANCELLED — deletes the worktrees ``restart_phase``
# resumes from. The gate returning ``gated=True`` cannot be told apart from an
# ordinary gating, so the stop has to be propagated explicitly (#3633 review
# round 2). These tests drive the containing functions, not the gate alone.


def _gap_contract(*, resolved: bool):
    """A contract carrying one tester→coder gap, resolved or not."""
    from egg_contracts.models import Contract

    return Contract(
        pipeline_id=PIPELINE_ID,
        slices=[
            {
                "id": "slice-1",
                "name": "n",
                "tasks": [
                    {
                        "id": "task-1-2",
                        "description": "d",
                        "gaps": [
                            {
                                "id": "gap-1",
                                "from_role": "tester",
                                "to_role": "coder",
                                "description": "no error-path test",
                                "resolved": resolved,
                            }
                        ],
                    }
                ],
            }
        ],
    )


def _implement_pipeline(status=PipelineStatus.RUNNING) -> Pipeline:
    pipeline = Pipeline(
        id=PIPELINE_ID,
        issue_number=3633,
        repo="owner/repo",
        branch=f"egg/{PIPELINE_ID}/work",
        base_branch="main",
        status=status,
        current_phase=PipelinePhase.IMPLEMENT,
    )
    return pipeline


def _run_implement_advance(*, cancel_during_wait: bool, resolution: str | None):
    """Drive ``_run_implement_advance`` over the *real* gap gate.

    ``cancel_during_wait`` models the operator's cancel as a store write that
    lands while the gate is parked in ``wait_for_decision`` — the shape the
    PATCH route produces (persist CANCELLED, then sweep the queue).
    """
    cell = _StatusCell(PipelineStatus.RUNNING)
    saved: list[PipelineStatus] = []

    decision = HITLDecision(
        id="gap-gate-1",
        question="Resolve the gap?",
        decision_type="phase_gate",
        phase=PipelinePhase.IMPLEMENT,
        status=(DecisionStatus.CANCELLED if resolution is None else DecisionStatus.RESOLVED),
        resolution=resolution,
    )

    def _wait(_decision_id):
        if cancel_during_wait:
            cell.cancel()
        return decision

    dq = MagicMock()
    dq.queue_decision.return_value = decision
    dq.wait_for_decision.side_effect = _wait

    def _load(_pipeline_id):
        # A fresh object per load, as the real store does — a shared one would
        # let the gate's own AWAITING_HUMAN write mask the persisted cancel.
        return _implement_pipeline(status=cell.status)

    store = MagicMock()
    store.load_pipeline.side_effect = _load
    store.save_pipeline.side_effect = lambda p, *a, **k: saved.append(p.status)

    spawner = MagicMock()

    with (
        patch.object(pipelines_pkg, "get_decision_queue", return_value=dq),
        patch.object(pipelines_pkg, "get_pipeline_state_lock"),
        patch.object(pipelines_pkg, "report_pipeline_status"),
        patch.object(pipelines_pkg, "_emit_event", None),
        patch.object(pipelines_pkg, "_commit_statefiles_to_worktree", return_value=True),
        patch(
            "egg_contracts.loader.load_contract",
            side_effect=[_gap_contract(resolved=False), _gap_contract(resolved=True)],
        ),
    ):
        pipeline, action = pipelines_pkg._run_implement_advance(
            _implement_pipeline(),
            current_phase=PipelinePhase.IMPLEMENT,
            gateway_mode="public",
            pipeline_id=PIPELINE_ID,
            repo_path=Path("/repo"),
            spawner=spawner,
            store=store,
            worktree_repo_path=Path("/tmp/egg-worktree"),
        )
    return action, saved, spawner


def test_implement_advance_stops_the_driver_on_a_cancel_at_the_gap_gate():
    """The gate's bail is only half the fix — its caller has to stop the driver.

    Returning ``gated`` alone is indistinguishable from an ordinary gating, so
    ``_run_pipeline`` fell through to the terminal-phase branch and overwrote
    the operator's CANCELLED with COMPLETE.
    """
    action, saved, spawner = _run_implement_advance(cancel_during_wait=True, resolution=None)

    assert action == "break", (
        "a cancel inside the gap gate must stop the driver, not just skip the "
        "gate's own RUNNING write"
    )
    assert PipelineStatus.RUNNING not in saved, (
        "the gap gate rewrote the operator's CANCELLED back to RUNNING"
    )
    # A cancelled pipeline must not keep mutating the remote work branch.
    spawner.gateway.push_worktree_branch.assert_not_called()


def test_implement_advance_still_advances_after_a_genuine_gap_resolution():
    """The bail must not swallow the ordinary path: an operator who resolves
    the gap and approves gets the post-gate commit+push and a fall-through."""
    action, saved, spawner = _run_implement_advance(cancel_during_wait=False, resolution="approve")

    assert action is None, "a resolved gap gate must let the driver advance"
    assert PipelineStatus.RUNNING in saved
    spawner.gateway.push_worktree_branch.assert_called_once()
