"""``_EventJobStatusView.outcome_for`` maps EX_RATE_LIMITED -> rate_limited (#3364 PR C).

The event-loop supervision view classifies a FAILED one-shot event Job into the
loop's outcome vocabulary by reading the terminated pod's exit code. PR C adds a
branch — checked AFTER auth-fatal, so a weekly-cap-as-77 still wins — that maps
``egg_agent.auth_errors.EX_RATE_LIMITED`` (69) to ``JOB_OUTCOME_RATE_LIMITED`` so
the supervisor paces the respawn across the cap window instead of counting the
throttle toward the abnormal fail-streak halt.

These tests drive the real ``_EventJobStatusView`` with a stub spawner whose k8s
client returns scripted Job statuses + container exit codes, and pin:

* EX_RATE_LIMITED -> ``rate_limited``;
* EX_AUTH_FATAL (77) still -> ``fatal`` (AC-C6 regression: auth-fatal precedence
  is unchanged, checked before the new branch);
* any stray exit code -> ``abnormal`` (today's behaviour — the new branch can
  never manufacture a spurious rate-limit), except SIGTERM (143), which #3665
  maps to ``legitimate`` so a 2-hour timeout kill does not consume the streak;
* a best-effort read failure falls back to ``abnormal``;
* live/exited Jobs are never misclassified.
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
from egg_agent.auth_errors import EX_AUTH_FATAL, EX_RATE_LIMITED  # noqa: E402
from kubernetes_spawner import _EventJobStatusView  # noqa: E402
from models import ContainerStatus  # noqa: E402


class _Job:
    def __init__(self, status: ContainerStatus) -> None:
        self.status = status


class _Container:
    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code


class _StubK8s:
    """Scriptable k8s client: fixed Job statuses + container exit codes."""

    def __init__(self, *, jobs=None, containers=None, containers_raise=False) -> None:
        self._jobs = jobs if jobs is not None else []
        self._containers = containers if containers is not None else []
        self._containers_raise = containers_raise
        self.list_jobs_calls = 0
        self.list_containers_calls = 0

    def list_jobs(self, namespace, label_selector=None):  # noqa: ANN001, ARG002
        self.list_jobs_calls += 1
        return self._jobs

    def list_containers(self, labels=None):  # noqa: ANN001, ARG002
        self.list_containers_calls += 1
        if self._containers_raise:
            raise RuntimeError("kube-apiserver unreachable")
        return self._containers


class _StubSpawner:
    def __init__(self, k8s: _StubK8s) -> None:
        self.k8s = k8s
        self._namespace = "egg"


def _view(*, job_status, exit_code=None, containers_raise=False) -> _EventJobStatusView:
    containers = [] if exit_code is None else [_Container(exit_code)]
    k8s = _StubK8s(
        jobs=[_Job(job_status)],
        containers=containers,
        containers_raise=containers_raise,
    )
    return _EventJobStatusView(_StubSpawner(k8s))


class TestOutcomeForRateLimited:
    def test_rate_limited_exit_code_maps_to_rate_limited(self):
        view = _view(job_status=ContainerStatus.FAILED, exit_code=EX_RATE_LIMITED)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_RATE_LIMITED

    def test_auth_fatal_still_wins_over_rate_limited(self):
        # AC-C6 regression: auth-fatal (77) is checked FIRST, so a weekly cap
        # delivered as 77 stays ``fatal`` and never falls into the new branch.
        view = _view(job_status=ContainerStatus.FAILED, exit_code=EX_AUTH_FATAL)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_FATAL

    @pytest.mark.parametrize("code", [1, 2, 137, 0])
    def test_stray_exit_code_falls_through_to_abnormal(self, code):
        view = _view(job_status=ContainerStatus.FAILED, exit_code=code)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_ABNORMAL

    def test_sigterm_is_legitimate_not_abnormal(self):
        # #3665: exit 143 (SIGTERM) is the sandbox's 2-hour agent timeout or an
        # orchestrator-initiated teardown — a lifecycle termination, not a crash,
        # so it must not consume the abnormal fail-streak budget. Checked AFTER
        # auth-fatal and rate-limited, so neither precedence above changes.
        # Full coverage lives in ``test_timeout_sigterm.py``.
        view = _view(job_status=ContainerStatus.FAILED, exit_code=143)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_LEGITIMATE

    def test_no_containers_falls_back_to_abnormal(self):
        # A FAILED Job with no readable container exit code is neither fatal nor
        # rate-limited — it stays on today's abnormal path.
        view = _view(job_status=ContainerStatus.FAILED, exit_code=None)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_ABNORMAL

    def test_container_read_failure_falls_back_to_abnormal(self):
        # Best-effort: a list_containers error can never manufacture a spurious
        # rate-limit — it degrades to abnormal.
        view = _view(job_status=ContainerStatus.FAILED, containers_raise=True)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_ABNORMAL

    def test_running_job_is_not_classified_rate_limited(self):
        view = _view(job_status=ContainerStatus.RUNNING, exit_code=EX_RATE_LIMITED)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_RUNNING

    def test_exited_job_is_success_not_rate_limited(self):
        view = _view(job_status=ContainerStatus.EXITED)
        assert view.outcome_for("k") == event_loop.JOB_OUTCOME_SUCCESS
