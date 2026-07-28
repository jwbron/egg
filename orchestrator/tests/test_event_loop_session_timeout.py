"""Session-budget expiry is a boundary, not a crash (#3658).

The #3639 incident ended when two independent pods each hit their own 2 h mark.
Nothing about that was visible: the expiry returned an ordinary non-zero rc, the
orchestrator classified it ``abnormal``, and ``record_abort`` counted it toward
the >=10 ``agent-invocation-fail-streak`` halt and the propose-arm
``AGENT_FAILED`` escalation. A healthy agent that ran long was indistinguishable
from a crash loop.

This file covers the orchestrator half of the fix, across four seams:

* ``_EventJobStatusView.outcome_for`` maps ``EX_SESSION_TIMEOUT`` (124) to
  ``JOB_OUTCOME_TIMEOUT`` without disturbing the auth-fatal / rate-limit
  precedences above it;
* it reads that code from the *newest* terminated pod rather than the union
  across the dedupe key's history — the label is respawn-stable, so a stale 124
  would otherwise hand a crash a free boundary;
* ``_observe_jobs`` routes that outcome to ``record_session_timeout`` — not
  ``record_abort`` — reaps the Job, and keeps ``_key_meta`` so the respawn
  re-labels the same arm and continues in the same worktree;
* ``record_session_timeout`` leaves the abnormal streak untouched for the first
  ``SUPERVISION_SESSION_TIMEOUT_BUDGET`` expiries and then hands the key back to
  the abort path, so a permanently over-budget arm still terminates — alerting
  once on the last free boundary, so the budget is not spent silently.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

import event_loop  # noqa: E402
import supervision_policy  # noqa: E402
from egg_agent.auth_errors import (  # noqa: E402
    EX_AUTH_FATAL,
    EX_RATE_LIMITED,
    EX_SESSION_TIMEOUT,
)
from kubernetes_spawner import _EventJobStatusView  # noqa: E402
from kubernetes_spawner._models import _terminated_at  # noqa: E402
from models import ContainerStatus  # noqa: E402


def _dt(minute: int, *, aware: bool = False) -> datetime:
    """A terminated-at stamp; ``aware`` models the tz-aware half of the k8s API."""
    tz = UTC if aware else None
    return datetime(2026, 7, 27, 1, minute, 0, tzinfo=tz)


# ---------------------------------------------------------------------------
# Job-status classification
# ---------------------------------------------------------------------------


class _Job:
    def __init__(self, status: ContainerStatus) -> None:
        self.status = status


class _Container:
    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code


class _StubK8s:
    def __init__(self, *, jobs=None, containers=None, containers_raise=False) -> None:
        self._jobs = jobs if jobs is not None else []
        self._containers = containers if containers is not None else []
        self._containers_raise = containers_raise

    def list_jobs(self, namespace, label_selector=None):  # noqa: ANN001, ARG002
        return self._jobs

    def list_containers(self, labels=None):  # noqa: ANN001, ARG002
        if self._containers_raise:
            raise RuntimeError("kube-apiserver unreachable")
        return self._containers


class _StubSpawner:
    def __init__(self, k8s: _StubK8s) -> None:
        self.k8s = k8s
        self._namespace = "egg"


def _view(*, job_status, exit_code=None, containers_raise=False) -> _EventJobStatusView:
    containers = [] if exit_code is None else [_Container(exit_code)]
    return _EventJobStatusView(
        _StubSpawner(
            _StubK8s(
                jobs=[_Job(job_status)],
                containers=containers,
                containers_raise=containers_raise,
            )
        )
    )


class TestOutcomeForSessionTimeout:
    def test_session_timeout_exit_code_maps_to_timeout(self):
        view = _view(job_status=ContainerStatus.FAILED, exit_code=EX_SESSION_TIMEOUT)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_TIMEOUT

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (EX_AUTH_FATAL, "JOB_OUTCOME_FATAL"),
            (EX_RATE_LIMITED, "JOB_OUTCOME_RATE_LIMITED"),
        ],
    )
    def test_existing_precedences_are_undisturbed(self, code, expected):
        view = _view(job_status=ContainerStatus.FAILED, exit_code=code)
        assert view.outcome_for("k") == getattr(event_loop, expected)

    @pytest.mark.parametrize("code", [1, 2, 137, 143, 0])
    def test_stray_exit_code_still_falls_through_to_abnormal(self, code):
        """The new branch can never manufacture a spurious boundary."""
        view = _view(job_status=ContainerStatus.FAILED, exit_code=code)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_ABNORMAL

    def test_unreadable_exit_code_falls_back_to_abnormal(self):
        view = _view(job_status=ContainerStatus.FAILED, containers_raise=True)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_ABNORMAL

    def test_running_job_is_not_classified_timeout(self):
        view = _view(job_status=ContainerStatus.RUNNING, exit_code=EX_SESSION_TIMEOUT)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_RUNNING


class _StampedContainer:
    def __init__(self, exit_code: int | None, *, exited_at=None, started_at=None) -> None:
        self.exit_code = exit_code
        self.exited_at = exited_at
        self.started_at = started_at


def _view_over(containers) -> _EventJobStatusView:
    return _EventJobStatusView(
        _StubSpawner(_StubK8s(jobs=[_Job(ContainerStatus.FAILED)], containers=containers))
    )


class TestNewestTerminatedPodScoping:
    """The dedupe-key label is respawn-stable, so the selector sees history.

    Unioning exit codes across attempts lets a *stale* pod's code decide the
    current attempt's classification, and the two directions are not symmetric:
    a stale 77 fails closed (the arm halts loudly, an operator sees it), while a
    stale 124 fails OPEN — a free boundary granted for a crash, with the failure
    streak suppressed and nothing emitted at all.
    """

    def test_a_stale_boundary_cannot_excuse_the_current_crash(self):
        """The failure this scoping exists to prevent."""
        view = _view_over(
            [
                _StampedContainer(EX_SESSION_TIMEOUT, exited_at=_dt(1)),
                _StampedContainer(1, exited_at=_dt(2)),
            ]
        )

        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_ABNORMAL

    def test_the_current_boundary_is_still_honoured_over_a_stale_crash(self):
        view = _view_over(
            [
                _StampedContainer(1, exited_at=_dt(1)),
                _StampedContainer(EX_SESSION_TIMEOUT, exited_at=_dt(2)),
            ]
        )

        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_TIMEOUT

    def test_pod_order_from_the_api_does_not_decide_the_outcome(self):
        """k8s list order is not a guarantee; the timestamp is the ordering."""
        newest = _StampedContainer(1, exited_at=_dt(2))
        oldest = _StampedContainer(EX_SESSION_TIMEOUT, exited_at=_dt(1))

        assert _view_over([newest, oldest]).outcome_for("k") == event_loop.JOB_OUTCOME_ABNORMAL
        assert _view_over([oldest, newest]).outcome_for("k") == event_loop.JOB_OUTCOME_ABNORMAL

    def test_start_time_ranks_a_pod_whose_exit_time_never_landed(self):
        """A status read before the terminated state was populated still orders."""
        view = _view_over(
            [
                _StampedContainer(EX_SESSION_TIMEOUT, started_at=_dt(1)),
                _StampedContainer(1, started_at=_dt(2)),
            ]
        )

        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_ABNORMAL

    def test_exit_time_outranks_start_time_on_the_same_pod(self):
        assert _terminated_at(_StampedContainer(0, exited_at=_dt(2), started_at=_dt(1))) == _dt(2)
        assert _terminated_at(_StampedContainer(0, started_at=_dt(1))) == _dt(1)
        assert _terminated_at(_StampedContainer(0)) is None

    def test_unstamped_pods_fall_back_to_the_union(self):
        """Pre-#3658 behaviour, and no worse than it.

        With nothing to order by, declining to classify would silently drop a
        genuine boundary; the union at least still sees it.
        """
        view = _view_over([_StampedContainer(1), _StampedContainer(EX_SESSION_TIMEOUT)])

        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_TIMEOUT

    def test_unorderable_stamps_fall_back_to_the_union(self):
        """Naive and aware datetimes are not comparable.

        Falling back is honest; picking an arbitrary one would not be, and a
        raised TypeError here would turn a classification into a crash.
        """
        view = _view_over(
            [
                _StampedContainer(1, exited_at=_dt(1)),
                _StampedContainer(EX_SESSION_TIMEOUT, exited_at=_dt(2, aware=True)),
            ]
        )

        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_TIMEOUT

    def test_pods_with_no_exit_code_are_ignored_entirely(self):
        """A still-running sibling must not out-rank the pod that actually died."""
        view = _view_over(
            [
                _StampedContainer(EX_SESSION_TIMEOUT, exited_at=_dt(1)),
                _StampedContainer(None, started_at=_dt(9)),
            ]
        )

        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_TIMEOUT


# ---------------------------------------------------------------------------
# Supervisor semantics
# ---------------------------------------------------------------------------


class _ManualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestRecordSessionTimeout:
    def test_expiry_leaves_the_abnormal_streak_untouched(self):
        """The headline invariant: a boundary can never feed the halt."""
        sup = event_loop.JobSupervisor(clock=_ManualClock())

        sup.record_session_timeout("k", "propose", "coder")

        assert sup._streaks.get("k", 0) == 0
        assert "k" not in sup._last_abort_time
        assert not sup.is_exhausted("k")

    def test_expiry_imposes_no_backoff_on_the_respawn(self):
        """The arm already waited two hours; making it wait again is absurd."""
        sup = event_loop.JobSupervisor(clock=_ManualClock())

        sup.record_session_timeout("k", "propose", "coder")

        assert sup.ready_to_respawn("k") is True

    def test_a_full_budget_of_expiries_never_escalates(self):
        agent_failed_calls = []
        sup = event_loop.JobSupervisor(
            clock=_ManualClock(),
            agent_failed=lambda **kw: agent_failed_calls.append(kw),
        )

        for _ in range(supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET):
            sup.record_session_timeout("k", "propose", "coder")

        assert sup._streaks.get("k", 0) == 0
        assert agent_failed_calls == []
        assert not sup.is_exhausted("k")

    def test_past_the_budget_expiries_become_ordinary_aborts(self):
        """Bounded: an arm that ONLY ever times out must still terminate.

        The boundary treatment disables the machinery that stops a hopeless arm,
        so the budget is what hands the key back to it.
        """
        sup = event_loop.JobSupervisor(clock=_ManualClock())
        budget = supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET

        for _ in range(budget):
            sup.record_session_timeout("k", "propose", "coder")
        assert sup._streaks.get("k", 0) == 0

        sup.record_session_timeout("k", "propose", "coder")
        assert sup._streaks["k"] == 1

    def test_over_budget_expiries_eventually_exhaust_the_key(self):
        sup = event_loop.JobSupervisor(clock=_ManualClock())
        budget = supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET
        alert = supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT

        for _ in range(budget + alert):
            sup.record_session_timeout("k", "propose", "coder")

        assert sup.is_exhausted("k")

    def test_a_clean_completion_restores_a_full_budget(self):
        """A productive session is not charged for earlier boundaries."""
        sup = event_loop.JobSupervisor(clock=_ManualClock())
        budget = supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET

        for _ in range(budget):
            sup.record_session_timeout("k", "propose", "coder")
        sup.record_success("k", action="propose", role="coder")
        for _ in range(budget):
            sup.record_session_timeout("k", "propose", "coder")

        assert sup._streaks.get("k", 0) == 0

    def test_an_inherited_abort_stamp_does_not_hold_the_respawn(self):
        """ "No backoff" has to survive a key that aborted before it timed out.

        ``ready_to_respawn`` measures its window from ``_last_abort_time``, so an
        expiry that left an earlier crash's stamp in place would be held inside
        an inherited backoff — the boundary path would silently be no-backoff in
        tests and backed-off in the one situation that produced it.
        """
        clock = _ManualClock()
        sup = event_loop.JobSupervisor(clock=clock)
        sup.record_abort("k", "propose", "coder")
        assert sup.ready_to_respawn("k") is False

        sup.record_session_timeout("k", "propose", "coder")

        assert "k" not in sup._last_abort_time
        assert sup.ready_to_respawn("k") is True

    def test_dropping_the_stamp_does_not_shorten_a_later_crash_backoff(self):
        """Safe because the streak is untouched: a later abort re-stamps itself.

        The clearing is about *this* respawn, not about forgiving the arm's
        history — an abort after the boundary must still back off on the streak
        it had already accumulated.
        """
        sup = event_loop.JobSupervisor(clock=_ManualClock())
        sup.record_abort("k", "propose", "coder")
        streak_before = sup._streaks["k"]

        sup.record_session_timeout("k", "propose", "coder")
        sup.record_abort("k", "propose", "coder")

        assert sup._streaks["k"] == streak_before + 1
        assert sup.ready_to_respawn("k") is False

    def test_retire_drops_the_boundary_counter(self):
        sup = event_loop.JobSupervisor(clock=_ManualClock())
        sup.record_session_timeout("k", "propose", "coder")

        sup.retire("k")

        assert "k" not in sup._session_timeout_count


class TestBudgetConsumedAlert:
    """The budget must reach an operator while it is still cheap to act on.

    Everything below the budget is silent by construction, so an arm that burns
    the whole thing and then converts to an ordinary failing key would otherwise
    first surface as an ``agent-invocation-fail-streak`` several hours later,
    with nothing naming the wall-clock budget as the cause.
    """

    @staticmethod
    def _supervisor():
        alerts: list[dict] = []
        sup = event_loop.JobSupervisor(
            clock=_ManualClock(),
            overseer_alert=lambda **kw: alerts.append(kw),
        )
        return sup, alerts

    def test_boundaries_below_the_budget_are_silent(self):
        sup, alerts = self._supervisor()

        for _ in range(supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET - 1):
            sup.record_session_timeout("k", "propose", "coder")

        assert alerts == []

    def test_the_last_free_boundary_alerts(self):
        """On the last FREE boundary, not the first one past it.

        Past the budget the arm is already on the abort path, where the only
        remaining surface is the streak halt; here the operator can still raise
        the budget, narrow the event, or split the work.
        """
        sup, alerts = self._supervisor()

        for _ in range(supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET):
            sup.record_session_timeout("k", "propose", "coder")

        assert len(alerts) == 1
        assert alerts[0]["anomaly"] == "session-timeout-budget-consumed"
        # Deliberately not ``high``: nothing is wedged — every one of these
        # invocations was working when it was killed, and the tree is
        # checkpointed. Escalating it would dilute the alerts that mean "stuck".
        assert alerts[0]["priority"] == "medium"

    def test_the_alert_names_the_arm_and_the_budget(self):
        sup, alerts = self._supervisor()

        for _ in range(supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET):
            sup.record_session_timeout("k", "propose", "coder")

        detail = alerts[0]["detail"]
        assert "coder" in detail
        assert "propose" in detail
        assert str(supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET) in detail

    def test_further_expiries_do_not_repeat_the_alert(self):
        """Once per key: an over-budget arm times out every two hours forever."""
        sup, alerts = self._supervisor()

        for _ in range(supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET + 3):
            sup.record_session_timeout("k", "propose", "coder")

        assert len(alerts) == 1

    def test_a_clean_completion_re_arms_the_alert(self):
        """The latch shares the counter's lifecycle, so a fresh budget is loud."""
        sup, alerts = self._supervisor()
        budget = supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET

        for _ in range(budget):
            sup.record_session_timeout("k", "propose", "coder")
        sup.record_success("k", action="propose", role="coder")
        for _ in range(budget):
            sup.record_session_timeout("k", "propose", "coder")

        assert len(alerts) == 2

    def test_retire_and_reconcile_clear_the_latch(self):
        sup, _alerts = self._supervisor()
        for _ in range(supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET):
            sup.record_session_timeout("k", "propose", "coder")
        assert sup._alerted_session_timeout.get("k") is True

        sup.retire("k")
        assert "k" not in sup._alerted_session_timeout

        for _ in range(supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET):
            sup.record_session_timeout("k", "propose", "coder")
        sup.reconcile(set())

        assert sup._alerted_session_timeout == {}

    def test_no_alert_callback_is_not_a_crash(self):
        """Alerting is best-effort; the boundary treatment is not."""
        sup = event_loop.JobSupervisor(clock=_ManualClock())

        for _ in range(supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET):
            sup.record_session_timeout("k", "propose", "coder")

        assert sup._streaks.get("k", 0) == 0

    def test_the_expiry_is_named_in_the_termination_history(self):
        """The operator must be able to see WHY the arm died, not just that it did."""
        sup = event_loop.JobSupervisor(clock=_ManualClock())

        sup.record_session_timeout("k", "propose", "coder", exit_detail="exit_code=124")

        history = sup._exit_history["k"]
        assert history[-1]["category"] == "session_timeout"
        assert history[-1]["detail"] == "exit_code=124"

    def test_an_interleaved_crash_cannot_refill_the_budget(self):
        """``record_abort`` must not reset the counter — it is the fallthrough.

        A counter that reset on abort could never be spent, because the
        over-budget path records an abort itself.
        """
        sup = event_loop.JobSupervisor(clock=_ManualClock())
        budget = supervision_policy.SUPERVISION_SESSION_TIMEOUT_BUDGET

        for _ in range(budget):
            sup.record_session_timeout("k", "propose", "coder")
        sup.record_abort("k", "propose", "coder")
        streak_after_crash = sup._streaks["k"]

        sup.record_session_timeout("k", "propose", "coder")

        assert sup._streaks["k"] == streak_after_crash + 1


# ---------------------------------------------------------------------------
# Loop routing
# ---------------------------------------------------------------------------


class _RecordingSupervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def record_session_timeout(self, key, action, role, *, exit_detail=None):  # noqa: ANN001
        self.calls.append(("session_timeout", key))

    def record_abort(self, key, action, role, *, exit_detail=None):  # noqa: ANN001
        self.calls.append(("abort", key))

    def record_success(self, key, *, action="", role=""):  # noqa: ANN001
        self.calls.append(("success", key))

    def record_fatal(self, key, action, role, *, exit_detail=None):  # noqa: ANN001
        self.calls.append(("fatal", key))

    def record_rate_limited(self, key, action, role, *, exit_detail=None):  # noqa: ANN001
        self.calls.append(("rate_limited", key))

    def record_legitimate_outcome(self, key, outcome):  # noqa: ANN001
        self.calls.append(("legitimate", key))


class _TimeoutStatusView:
    def __init__(self) -> None:
        self.reaped: list[str] = []

    def outcome_for(self, dedupe_key):  # noqa: ANN001, ARG002
        return event_loop.JOB_OUTCOME_TIMEOUT

    def exit_detail_for(self, dedupe_key):  # noqa: ANN001, ARG002
        return "exit_code=124"

    def reap_terminated(self, dedupe_key):  # noqa: ANN001
        self.reaped.append(dedupe_key)
        return 1


def _loop_with_live_key(view, supervisor):
    loop = event_loop.OrchestratorEventLoop(
        tracker=object(),
        spawner=object(),
        pipeline_id="p1",
        slice_id=None,
        phase="implement",
        job_supervisor=supervisor,
        job_status_view=view,
    )
    loop._live_keys.add("k")
    loop._key_meta["k"] = ("propose", "coder")
    return loop


class TestObserveJobsRouting:
    def test_timeout_outcome_routes_to_record_session_timeout(self):
        supervisor = _RecordingSupervisor()
        loop = _loop_with_live_key(_TimeoutStatusView(), supervisor)

        loop._observe_jobs()

        assert supervisor.calls == [("session_timeout", "k")]

    def test_timeout_reaps_the_job_and_frees_the_key_for_respawn(self):
        view = _TimeoutStatusView()
        loop = _loop_with_live_key(view, _RecordingSupervisor())

        loop._observe_jobs()

        assert view.reaped == ["k"]
        assert "k" not in loop._live_keys

    def test_timeout_keeps_key_meta_so_the_respawn_re_labels_the_arm(self):
        loop = _loop_with_live_key(_TimeoutStatusView(), _RecordingSupervisor())

        loop._observe_jobs()

        assert loop._key_meta["k"] == ("propose", "coder")
