"""Session-budget expiry is a boundary, not a crash (#3658).

The #3639 incident ended when two independent pods each hit their own 2 h mark.
Nothing about that was visible: the expiry returned an ordinary non-zero rc, the
orchestrator classified it ``abnormal``, and ``record_abort`` counted it toward
the >=10 ``agent-invocation-fail-streak`` halt and the propose-arm
``AGENT_FAILED`` escalation. A healthy agent that ran long was indistinguishable
from a crash loop.

This file covers the orchestrator half of the fix, across three seams:

* ``_EventJobStatusView.outcome_for`` maps ``EX_SESSION_TIMEOUT`` (124) to
  ``JOB_OUTCOME_TIMEOUT`` without disturbing the auth-fatal / rate-limit
  precedences above it;
* ``_observe_jobs`` routes that outcome to ``record_session_timeout`` — not
  ``record_abort`` — reaps the Job, and keeps ``_key_meta`` so the respawn
  re-labels the same arm and continues in the same worktree;
* ``record_session_timeout`` leaves the abnormal streak untouched for the first
  ``SUPERVISION_SESSION_TIMEOUT_BUDGET`` expiries and then hands the key back to
  the abort path, so a permanently over-budget arm still terminates.
"""

from __future__ import annotations

import sys
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
from models import ContainerStatus  # noqa: E402

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

    def test_retire_drops_the_boundary_counter(self):
        sup = event_loop.JobSupervisor(clock=_ManualClock())
        sup.record_session_timeout("k", "propose", "coder")

        sup.retire("k")

        assert "k" not in sup._session_timeout_count

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
