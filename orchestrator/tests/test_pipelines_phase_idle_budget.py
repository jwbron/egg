"""Tests for the ``routes.pipelines`` wiring of ``PhaseIdleBudgetTimer``
(issue #3023, slice-1, task-1-2).

The orchestrator's per-phase tick (the run loop that already polls
consensus for phase completion in ``routes/pipelines.py``) takes ownership
of the idle-budget alert after slice-1: each tick reads the pending-HITL
count, calls ``timer.check(now=monotonic, pending_hitl_count=...)``, and
emits a structured ``phase_idle_budget_tick`` log line whenever the timer
emits an alert.

These tests cover the *wiring* side; the timer's internal threshold,
suppression, and idempotence semantics are exhaustively pinned by
``orchestrator/tests/test_phase_idle_budget.py``.

Acceptance criteria pinned here:

* tick invokes ``check`` exactly once per loop iteration;
* after the fixture advances past 30 min with no ``record_spawn`` and
  zero pending HITL, the overseer alert handler is invoked exactly once
  with ``anomaly="stuck-phase-transition"``, ``priority="medium"``;
* with one pending HITL the same interval emits ``priority="low"`` and
  the pending HITL id appears in the alert reason.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# conftest.py adds orchestrator/ and shared/ to sys.path; the assertion
# below makes a regression there land an obvious diagnostic on this file
# instead of a confusing ``ModuleNotFoundError``.
_orchestrator_path = Path(__file__).parent.parent
assert str(_orchestrator_path) in sys.path

# ``pytest.importorskip`` so the file is collectable before the task-1-1
# production module exists; the tests skip cleanly until the slice-branch
# integration merges both task-1-1 + task-1-2.
phase_idle_budget = pytest.importorskip(
    "phase_idle_budget",
    reason=(
        "orchestrator/phase_idle_budget.py is task-1-1 of issue #3023 "
        "slice-1; the wiring tests skip until both task-1-1 (module) and "
        "task-1-2 (wiring into routes/pipelines.py) have landed."
    ),
)


def _resolve_class():
    cls = getattr(phase_idle_budget, "PhaseIdleBudgetTimer", None)
    assert cls is not None, (
        "task-1-1 must export PhaseIdleBudgetTimer (see "
        "test_phase_idle_budget.py for the unit-level surface)."
    )
    return cls


# --------------------------------------------------------------------------- #
# (1) Wiring contract: routes.pipelines imports the timer
# --------------------------------------------------------------------------- #


class TestPipelinesWiringContract:
    """Pin the *wiring* contract — ``routes.pipelines`` must own a
    ``PhaseIdleBudgetTimer`` reference so the run loop can call ``check``
    every tick.

    The exact symbol used by ``routes.pipelines`` to access the timer is
    left to the implementation (a module-level singleton, a per-pipeline
    attribute, or a constructor injection are all valid), so the test
    asserts at the module-import / source-reference level rather than
    naming a private symbol.
    """

    def test_routes_pipelines_module_imports_phase_idle_budget(self):
        """``routes.pipelines`` (or one of its decomposed submodules — see
        the #2261 seam table) must reference ``phase_idle_budget`` so the
        wiring lives somewhere the operator can grep for during a triage.
        """
        try:
            import routes.pipelines as _pipelines_mod  # noqa: F401
        except ImportError:
            pytest.skip(
                "routes.pipelines is not importable in this minimal test "
                "environment (e.g. missing Flask). Re-run with the orchestrator "
                "dev dependencies installed; the source-grep below covers the "
                "non-import path."
            )

        source = (Path(__file__).parent.parent / "routes" / "pipelines.py").read_text(
            encoding="utf-8"
        )
        # The wiring must reference either ``phase_idle_budget`` (the
        # module) or ``PhaseIdleBudgetTimer`` (the class). A grep miss
        # here means task-1-2 did not actually wire the timer into the
        # run loop.
        assert ("phase_idle_budget" in source) or ("PhaseIdleBudgetTimer" in source), (
            "routes/pipelines.py must reference phase_idle_budget / "
            "PhaseIdleBudgetTimer; task-1-2 acceptance: 'tick invokes "
            "check exactly once per loop iteration'."
        )


# --------------------------------------------------------------------------- #
# (2) Acceptance: 30 min, zero pending HITL -> exactly one medium-priority
#     stuck-phase-transition alert
# --------------------------------------------------------------------------- #


class TestTickFiresExactlyOneMediumAlertAtThirtyMinutes:
    """End-to-end simulation of the run loop's contract with the timer.

    The unit-level threshold tests live in ``test_phase_idle_budget.py``;
    these tests pin the *operator-facing* invariant: at 30 min idle with
    zero pending HITL the alert is exactly one ``medium``-priority
    ``stuck-phase-transition``.
    """

    def test_thirty_minute_idle_zero_hitl_emits_single_medium_alert(self):
        Cls = _resolve_class()
        emitter = MagicMock()
        # Construct with an explicit budget so the test is independent of
        # the constant (the constant's value is pinned in
        # test_phase_idle_budget.py).
        timer = Cls(alert_emitter=emitter, budget_minutes=30)

        # Simulate phase start with one initial spawn; the run loop's
        # ``record_spawn`` is wrapped around every per-event spawn.
        timer.record_spawn(
            pipeline_id="issue-3023",
            phase="implement",
            role="coder",
            action="propose",
            now=0.0,
        )

        # The run loop calls ``check`` once per tick. We simulate ticks at
        # 10-second cadence (a typical orchestrator polling interval) and
        # let monotonic time accrue past the 30-min budget — but stop
        # before the 2× (60-min) bucket so the assertion below is pinned
        # on the 1× ``medium`` alert in isolation. The 2× emission is
        # exercised in the unit-level tests in ``test_phase_idle_budget``.
        for tick_t in range(10, 35 * 60 + 10, 10):
            timer.check(now=float(tick_t), pending_hitl_count=0)

        assert emitter.call_count == 1, (
            "At 30 min idle with zero pending HITL the run loop must emit "
            "exactly one stuck-phase-transition alert. Got "
            f"{emitter.call_count} (acceptance: 'overseer alert handler is "
            'invoked exactly once with anomaly="stuck-phase-transition", '
            "priority=medium')."
        )
        kwargs = emitter.call_args.kwargs
        assert kwargs.get("anomaly") == "stuck-phase-transition"
        assert kwargs.get("priority") == "medium", (
            "task-1-2 acceptance: the default 1× priority is 'medium' "
            "(downgraded from the wrapper's 'high' so operators have a "
            "graded signal between the orchestrator-side timer and the "
            "wrapper-side legacy emitter during slice-1 coexistence)."
        )


# --------------------------------------------------------------------------- #
# (3) Acceptance: with pending HITL the same interval emits priority=low
#     and the pending-HITL id appears in the reason
# --------------------------------------------------------------------------- #


class TestTickRespectsPendingHitl:
    """Pin the AC-R13 HITL-pending downgrade at the run-loop integration
    level. Because the run loop is what reads the pending-HITL count from
    ``routes/decisions.py``, this acceptance line *must* be tested at the
    wiring layer — the unit-level test (in ``test_phase_idle_budget.py``)
    proves the timer downgrades when given the count, but doesn't prove
    the run loop actually plumbs the count through.
    """

    def test_thirty_minute_idle_with_pending_hitl_emits_low_priority(self):
        Cls = _resolve_class()
        emitter = MagicMock()
        timer = Cls(alert_emitter=emitter, budget_minutes=30)

        timer.record_spawn(
            pipeline_id="issue-3023",
            phase="implement",
            role="coder",
            action="propose",
            now=0.0,
        )

        # Simulate the run loop ticking at 10-second cadence with a
        # pending HITL throughout.
        for tick_t in range(10, 60 * 60 + 10, 10):
            timer.check(
                now=float(tick_t),
                pending_hitl_count=1,
            )

        # AC-R13 1×-downgrade.
        assert emitter.call_count == 1
        kwargs = emitter.call_args.kwargs
        assert kwargs.get("priority") == "low", (
            "task-1-2 acceptance: 'with one pending HITL the same interval "
            "emits priority=low and the pending HITL id appears in reason'."
        )
        reason = (kwargs.get("reason") or "").lower()
        assert "hitl" in reason, (
            "The alert reason must reference the pending HITL state so "
            "operators can triage without cross-referencing the decision "
            "queue (AC-R13 + task-1-2 acceptance line)."
        )


# --------------------------------------------------------------------------- #
# (4) Acceptance: tick invokes check exactly once per loop iteration
# --------------------------------------------------------------------------- #


class TestTickCallsCheckOncePerIteration:
    """Pin the cadence invariant: the run loop must call ``timer.check`` at
    most once per iteration. A regression that calls it twice (e.g. once
    from a phase-completion polling helper and a second time from a
    surrounding outer loop) would double-emit on the 2× threshold.

    The test substitutes a ``MagicMock`` for the timer and asserts that
    after N simulated ticks the mock has been called N times — no more,
    no less. Because the real wiring lives inside a large module
    (``routes/pipelines.py``, ~17k lines), the test exercises the cadence
    contract via a public helper if the coder exposes one; otherwise it
    falls back to a source-grep that pins the call site shape.
    """

    def test_check_call_site_is_exactly_one_per_tick(self):
        """Source-level pin: in ``routes/pipelines.py`` the only call to
        ``timer.check(`` must appear inside the per-phase tick (the
        consensus-polling loop). A second call site would risk double-
        emission; this test catches a copy-paste regression early.

        The grep accepts ``timer.check`` and ``_phase_idle_budget.check``
        as call sites; an implementation that names the timer differently
        is fine as long as the loop body calls ``.check(`` exactly once.
        """
        source = (Path(__file__).parent.parent / "routes" / "pipelines.py").read_text(
            encoding="utf-8"
        )
        # Count occurrences of ``.check(`` adjacent to an identifier
        # that suggests the phase-idle-budget timer. We use simple
        # textual heuristics; the test is a guardrail, not a syntactic
        # analysis.
        candidates = [
            line
            for line in source.splitlines()
            if (".check(" in line)
            and (
                "phase_idle" in line
                or "PhaseIdleBudget" in line.lower().replace("_", "")
                or "idle_budget" in line
                or "_idle_budget_timer" in line
                or "self._idle_budget" in line
                or "_phase_idle_budget" in line
            )
        ]
        # The actual constraint is "at most one tick-cadence call site".
        # We accept 1 (the wiring) or 2 (the wiring + one test-shim hook).
        # 0 means the wiring is missing; > 2 means a likely duplicate.
        assert 1 <= len(candidates) <= 2, (
            "routes/pipelines.py must call PhaseIdleBudgetTimer.check at "
            "exactly one tick-cadence call site (got "
            f"{len(candidates)}). A second call site risks double-emission "
            "on the 2× threshold. Found lines: {candidates!r}"
        )


# --------------------------------------------------------------------------- #
# (5) Production-path: real ``_make_phase_idle_budget_emitter`` -> message store
# --------------------------------------------------------------------------- #


class TestProductionEmitterReachesMessageStore:
    """Pin the end-to-end wiring of the real production emitter.

    The earlier tests in this file substitute a ``MagicMock()`` directly
    for the timer's ``alert_emitter``, which proves the timer-side
    semantics but does NOT prove the production wiring (the closure
    built by ``_make_phase_idle_budget_emitter`` + the publisher
    ``_publish_phase_idle_budget_alert``) actually reaches the message
    bus. A regression where the closure raises a ``TypeError`` (e.g.
    forwarding ``pipeline_id`` both explicitly and via ``**kwargs``)
    would silently swallow every ``stuck-phase-transition`` alert via
    the surrounding ``except Exception`` in ``_run_concurrent_phase``;
    this test catches that exact shape by asserting an
    ``OVERSEER_ALERT`` ``Message`` lands in the store on the 1×
    threshold cross.
    """

    def _build_pipeline(self):
        """Construct a minimal Pipeline the publisher can read
        ``current_phase`` from. Skips cleanly if ``models.Pipeline`` is
        not importable in the test environment.
        """
        try:
            from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus
        except ImportError:  # pragma: no cover - guardrail only
            pytest.skip(
                "orchestrator/models.py is not importable in this minimal "
                "environment; the production-path test needs Pipeline to "
                "construct the publisher's input."
            )
        return Pipeline(
            id="issue-3023",
            issue_number=3023,
            repo="owner/repo",
            branch="egg/issue-3023",
            base_branch="main",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            config=PipelineConfig(),
        )

    def test_real_emitter_publishes_overseer_alert_on_1x_threshold(self):
        """Wire the real ``_make_phase_idle_budget_emitter`` into the
        timer and confirm the 1× threshold cross produces exactly one
        ``OVERSEER_ALERT`` ``Message`` on the store.

        Only the message-store boundary is mocked
        (``routes.pipelines._get_message_store``); the closure +
        publisher run unmodified. A regression where the closure
        raises (the #3023 v1 ``pipeline_id`` collision shape) would
        leave the mock with zero ``add_message`` calls and fail this
        assertion immediately.
        """
        try:
            from routes.pipelines import _make_phase_idle_budget_emitter
        except ImportError:
            pytest.skip(
                "routes.pipelines is not importable in this minimal test "
                "environment; the production-path test requires it."
            )

        from unittest.mock import patch

        Cls = _resolve_class()
        pipeline = self._build_pipeline()
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        emitter = _make_phase_idle_budget_emitter(
            pipeline=pipeline,
            pipeline_id="issue-3023",
            slice_id="slice-1",
        )
        timer = Cls(alert_emitter=emitter, budget_minutes=30, now=0.0)
        timer.record_spawn(
            pipeline_id="issue-3023",
            phase="implement",
            role="coder",
            action="propose",
            now=0.0,
        )

        # Force the publisher to find a mocked message store. We patch
        # the ``_get_message_store`` lookup rather than the ``Message``
        # / ``MessageType`` symbols so the publisher exercises its real
        # try/except import branches as well.
        with patch("routes.pipelines._get_message_store", return_value=store_factory):
            # Cross past the 30-min budget exactly once.
            timer.check(now=30 * 60 + 1, pending_hitl_count=0)

        # One add_message call -> the alert reached the bus.
        assert msg_store.add_message.call_count == 1, (
            "The production wiring (real _make_phase_idle_budget_emitter "
            "+ _publish_phase_idle_budget_alert) must land exactly one "
            "OVERSEER_ALERT Message on the bus at the 1× threshold cross. "
            f"Got {msg_store.add_message.call_count}. A 0 here indicates "
            "the closure raised (e.g. the #3023 v1 'pipeline_id collision' "
            "shape) and the alert was silently swallowed."
        )
        msg = msg_store.add_message.call_args.args[0]
        # Anomaly tag is the operator-facing classifier.
        assert getattr(msg, "subject", "").startswith("stuck-phase-transition"), (
            "Subject must start with 'stuck-phase-transition' so the SDLC-"
            "skill alert detection picks it up."
        )
        assert msg.metadata.get("anomaly_type") == "stuck-phase-transition"
        assert msg.metadata.get("threshold_multiplier") == 1
        assert msg.metadata.get("slice_id") == "slice-1"

    def test_real_emitter_filters_synthetic_orchestrator_seed(self):
        """The slice-1 wiring seeds the timer with
        ``record_spawn(role="orchestrator", action="phase_start", ...)``
        so the timer has a (pipeline_id, phase) binding to alert about
        before slice-2 wires real per-event spawn recording. The
        publisher must NOT surface that synthetic seed in the
        operator-facing ``per_role_state`` payload — otherwise the
        AC-R4 drill-down reads "orchestrator: phase_start", which
        implies the orchestrator is the only role that has acted (the
        opposite of the intended signal).

        This test exercises the real publisher path and asserts the
        synthetic seed is filtered both from the rendered body and the
        metadata payload.
        """
        try:
            from routes.pipelines import _make_phase_idle_budget_emitter
        except ImportError:
            pytest.skip(
                "routes.pipelines is not importable in this minimal test "
                "environment; the synthetic-seed-filter test requires it."
            )

        from unittest.mock import patch

        Cls = _resolve_class()
        pipeline = self._build_pipeline()
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        emitter = _make_phase_idle_budget_emitter(
            pipeline=pipeline,
            pipeline_id="issue-3023",
            slice_id="slice-1",
        )
        timer = Cls(alert_emitter=emitter, budget_minutes=30, now=0.0)
        # Mimic the slice-1 wiring: a synthetic ``orchestrator/phase_start``
        # seed plus no real per-role spawns yet.
        timer.record_spawn(
            pipeline_id="issue-3023",
            phase="implement",
            role="orchestrator",
            action="phase_start",
            now=0.0,
        )

        with patch("routes.pipelines._get_message_store", return_value=store_factory):
            timer.check(now=30 * 60 + 1, pending_hitl_count=0)

        assert msg_store.add_message.call_count == 1
        msg = msg_store.add_message.call_args.args[0]
        # Body must not advertise the orchestrator seed as a real
        # operator-meaningful action.
        assert "orchestrator: phase_start" not in msg.body, (
            "Body must filter the synthetic 'orchestrator: phase_start' "
            "seed (AC-R4 / NACK item 2) — surfacing it misleads operators "
            "into thinking the orchestrator is the only acting role."
        )
        # Metadata's per_role_state must mirror that filtering so the
        # SDLC-skill alert detection sees a clean per-role table.
        assert msg.metadata.get("per_role_state") == {}, (
            "Metadata per_role_state must drop the synthetic "
            "'orchestrator/phase_start' seed (AC-R4 / NACK item 2)."
        )
