"""Event-loop transient rate-limit path: routing + supervisor semantics (#3364 PR C).

Covers the orchestrator side of the EX_RATE_LIMITED mirror:

* ``_observe_jobs`` routes a ``rate_limited`` Job outcome to
  ``record_rate_limited`` — NOT ``record_abort`` — reaps the terminated Job, and
  drops the key from the live set while KEEPING ``_key_meta`` so the paced
  respawn re-labels the same arm;
* ``record_rate_limited`` leaves the abnormal ``_streaks`` / ``_exhausted``
  state ENTIRELY untouched, so a persistent cap wall can never trip the
  ``agent-invocation-fail-streak`` halt (AC-C1);
* ``ready_to_respawn`` paces the respawn across the per-key rate-limit window;
* ``record_success`` / ``retire`` clear the paced state so a later cap wall on
  the same key starts fresh;
* the deterministic-loop guard reports ``deterministic_loop`` when the identical
  fingerprint reproduces at the SAME progression point past the guard threshold,
  and RESETS when the progression advances (AC-C4, both directions);
* the cq-1 cumulative-wait ``threshold_crossed`` latches once (AC-C5);
* AC-C6 regression: ``record_abort`` still trips the streak halt at the ALERT
  threshold and the abnormal path is untouched by the new rate-limit path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

import event_loop  # noqa: E402
import supervision_policy  # noqa: E402


class _ManualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _RecordingSpawner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def spawn_event(self, *, role, action, dedupe_key, payload=None):
        self.calls.append({"role": role, "action": action, "dedupe_key": dedupe_key})
        return dedupe_key


class _AgentFreeRecorder:
    def __init__(self) -> None:
        self.handled: list[tuple[str, str]] = []

    def __call__(self, *, action, role, payload=None):
        self.handled.append((action, role))


class _FakeJobStatusView:
    def __init__(self) -> None:
        self._default = event_loop.JOB_OUTCOME_RUNNING
        self._outcomes: dict[str, str] = {}
        self.reaped: list[str] = []

    def set(self, key: str, outcome: str) -> None:
        self._outcomes[key] = outcome

    def outcome_for(self, key: str) -> str:
        return self._outcomes.get(key, self._default)

    def reap_terminated(self, key: str) -> int:
        self.reaped.append(key)
        return 1


def _make_loop(spawner, *, supervisor, status_view, clock):
    return event_loop.OrchestratorEventLoop(
        tracker=object(),
        spawner=spawner,
        pipeline_id="issue-3364",
        slice_id="slice-2",
        phase="implement",
        clock=clock,
        agent_free_handler=_AgentFreeRecorder(),
        roles=["coder"],
        job_supervisor=supervisor,
        job_status_view=status_view,
    )


# ---------------------------------------------------------------------------
# _observe_jobs routing
# ---------------------------------------------------------------------------


class TestObserveRoutesRateLimited:
    def test_rate_limited_outcome_routes_to_record_rate_limited(self):
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        view = _FakeJobStatusView()
        loop = _make_loop(_RecordingSpawner(), supervisor=supervisor, status_view=view, clock=clock)

        key = "k-rl"
        loop._live_keys.add(key)
        loop._key_meta[key] = ("propose", "coder")
        view.set(key, event_loop.JOB_OUTCOME_RATE_LIMITED)

        loop._observe_jobs()

        # Recorded on the paced path, NOT the abnormal streak path.
        assert supervisor._rate_limit_count.get(key) == 1
        assert key not in supervisor._streaks
        assert not supervisor.is_exhausted(key)
        # Terminated Job reaped; key dropped from the live set...
        assert key in view.reaped
        assert key not in loop._live_keys
        # ...but _key_meta kept so the paced respawn re-labels the same arm.
        assert loop._key_meta.get(key) == ("propose", "coder")

    def test_rate_limited_does_not_take_the_abnormal_branch(self):
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        view = _FakeJobStatusView()
        loop = _make_loop(_RecordingSpawner(), supervisor=supervisor, status_view=view, clock=clock)

        key = "k-rl"
        loop._live_keys.add(key)
        loop._key_meta[key] = ("propose", "coder")
        view.set(key, event_loop.JOB_OUTCOME_RATE_LIMITED)
        loop._observe_jobs()

        # The abnormal branch records an abort timestamp; the rate-limit branch
        # deliberately does not (it uses a SEPARATE paced anchor).
        assert key not in supervisor._last_abort_time
        assert supervisor._rate_limit_last_time.get(key) == clock.t


# ---------------------------------------------------------------------------
# record_rate_limited: no streak halt (AC-C1) + AC-C6 regression
# ---------------------------------------------------------------------------


class TestNoStreakHalt:
    def test_repeated_rate_limits_never_trip_the_fail_streak_halt(self):
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        # Far past the abnormal ALERT threshold (10) — a real cap wall could
        # persist for many polls.
        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT * 2):
            supervisor.record_rate_limited("k", "propose", "coder")
        assert not supervisor.is_exhausted("k")
        assert "k" not in supervisor._streaks
        assert supervisor._rate_limit_count["k"] == (
            supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT * 2
        )

    def test_abnormal_path_still_trips_the_halt_and_is_untouched(self):
        # AC-C6: the abnormal streak-to-ALERT exhaustion is byte-for-byte the
        # pre-#3364 behaviour, and the new rate-limit path leaves no residue.
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            supervisor.record_abort("k", "ack", "reviewer_code")
        assert supervisor.is_exhausted("k")
        assert "k" not in supervisor._rate_limit_count


# ---------------------------------------------------------------------------
# ready_to_respawn pacing + state clearing
# ---------------------------------------------------------------------------


class TestPacingAndClearing:
    def test_ready_to_respawn_paces_across_the_rate_limit_window(self):
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        supervisor.record_rate_limited("k", "propose", "coder")  # backoff = 1*30 = 30s
        backoff = supervisor._rate_limit_backoff["k"]
        assert backoff > 0

        # Inside the paced window: respawn is held.
        assert supervisor.ready_to_respawn("k") is False
        # Window elapsed: respawn is allowed (no abort anchor gates it).
        clock.advance(backoff + 1)
        assert supervisor.ready_to_respawn("k") is True

    def test_record_success_clears_paced_state(self):
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        supervisor.record_rate_limited("k", "propose", "coder")
        assert "k" in supervisor._rate_limit_count
        supervisor.record_success("k")
        assert "k" not in supervisor._rate_limit_count
        assert "k" not in supervisor._rate_limit_last_time

    def test_retire_clears_paced_state(self):
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        supervisor.record_rate_limited("k", "propose", "coder")
        supervisor.retire("k")
        assert "k" not in supervisor._rate_limit_count
        assert "k" not in supervisor._rate_limit_fingerprint


# ---------------------------------------------------------------------------
# Deterministic-loop guard (AC-C4) + cq-1 threshold alert (AC-C5) via notifier
# ---------------------------------------------------------------------------


class _NotifierSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


class TestLoopGuardAndThreshold:
    def test_identical_fingerprint_escalates_once_past_guard_threshold(self):
        progression = ["same"]
        spy = _NotifierSpy()
        supervisor = event_loop.JobSupervisor(
            clock=_ManualClock(),
            brc_probe=lambda: progression[0],
            rate_limited_notifier=spy,
        )
        repeats = supervision_policy.SUPERVISION_RATE_LIMIT_LOOP_GUARD_REPEATS

        # Identical exit_detail + identical progression => identical fingerprint.
        for _ in range(repeats):
            supervisor.record_rate_limited("k", "propose", "coder", exit_detail="rate_limited")

        loops = [c for c in spy.calls if c["deterministic_loop"]]
        assert len(loops) == 1
        assert spy.calls[repeats - 1]["deterministic_loop"] is True
        # Sticky: a further identical retry does not re-escalate.
        supervisor.record_rate_limited("k", "propose", "coder", exit_detail="rate_limited")
        assert spy.calls[-1]["deterministic_loop"] is False

    def test_advancing_progression_resets_the_guard(self):
        progression = ["p0"]
        spy = _NotifierSpy()
        supervisor = event_loop.JobSupervisor(
            clock=_ManualClock(),
            brc_probe=lambda: progression[0],
            rate_limited_notifier=spy,
        )
        repeats = supervision_policy.SUPERVISION_RATE_LIMIT_LOOP_GUARD_REPEATS

        # Every retry advances the consensus progression => a DIFFERENT
        # fingerprint each time => the repeat counter never accumulates.
        for i in range(repeats + 3):
            progression[0] = f"p{i}"
            supervisor.record_rate_limited("k", "propose", "coder", exit_detail="rate_limited")

        assert not any(c["deterministic_loop"] for c in spy.calls)

    def test_cumulative_wait_threshold_alert_latches_once(self):
        progression = ["p"]
        spy = _NotifierSpy()
        supervisor = event_loop.JobSupervisor(
            clock=_ManualClock(),
            brc_probe=lambda: progression[0],
            rate_limited_notifier=spy,
        )
        threshold = supervision_policy.SUPERVISION_RATE_LIMIT_ALERT_THRESHOLD_SECONDS

        # A parseable reset hint paces each retry near the pacing cap so the
        # cumulative wait crosses the threshold in a few retries; advance the
        # progression so the loop-guard stays orthogonal (never escalates).
        for i in range(10):
            progression[0] = f"p{i}"
            supervisor.record_rate_limited(
                "k", "propose", "coder", exit_detail="retry after 900 seconds"
            )
            if supervisor._rate_limit_wait_total["k"] >= threshold:
                break
        crossings = [c for c in spy.calls if c["threshold_crossed"]]
        assert len(crossings) == 1
        assert crossings[0]["cumulative_wait_seconds"] >= threshold
        # Orthogonality: the threshold crossing did not also flag a loop.
        assert crossings[0]["deterministic_loop"] is False
