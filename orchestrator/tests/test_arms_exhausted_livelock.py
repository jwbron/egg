"""Tests for the exhausted-key livelock surfacing + in-band reset (#3496).

The incident: every ack arm of a slice exhausted its spawn budget
(``JobSupervisor._exhausted``), so the event loop refused every spawn while
the pipeline stayed ``running`` with an empty ``pending_decisions`` — a
silent livelock whose only exit was a full ``restart_phase``. These tests
pin the fix end-to-end:

* the supervisor records per-key termination history (category + pod exit
  detail) and exposes it via ``exhausted_report()``;
* ``reset_exhausted()`` / ``OrchestratorEventLoop.reset_exhausted_arms()``
  give exhausted keys a fresh budget (including re-alert on re-exhaustion);
* the loop detects the all-arms-exhausted wedge and fires
  ``arms_exhausted_notifier`` once per episode;
* the executor escalates the wedge as an OVERSEER_ALERT + a persisted HITL
  decision (deduped on the ``event_arms_exhausted`` context — the dedup gate
  suppresses BOTH surfaces so a re-armed latch does not re-broadcast);
* the escalation report is scoped to the currently-blocked keys (stale keys
  from superseded BRC rounds are filtered out);
* the loop auto-withdraws the stale HITL when the wedge clears by another
  route, guarded so a still-wedged sibling slice holds the shared decision;
* the resolve-decision dispatch executes "Retry arms" against the live-loop
  registry and "Restart phase" via the in-process restart route.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


# ---------------------------------------------------------------------------
# Test doubles (mirroring test_event_loop.py's conventions)
# ---------------------------------------------------------------------------


class _RecordingSpawner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def spawn_event(self, *, role, action, dedupe_key, payload=None):
        self.calls.append({"role": role, "action": action, "dedupe_key": dedupe_key})
        return dedupe_key


class _ManualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _AgentFreeRecorder:
    def __init__(self) -> None:
        self.handled: list[tuple[str, str]] = []

    def __call__(self, *, action, role, payload=None):
        self.handled.append((action, role))


class _FakeJobStatusView:
    """Scriptable Job-status observer with optional exit-detail support."""

    def __init__(self, exit_detail: str | None = None) -> None:
        import event_loop

        self._default = event_loop.JOB_OUTCOME_RUNNING
        self._outcomes: dict[str, str] = {}
        self._exit_detail = exit_detail
        self.reaped: list[str] = []

    def set(self, key: str, outcome: str) -> None:
        self._outcomes[key] = outcome

    def outcome_for(self, key: str) -> str:
        return self._outcomes.get(key, self._default)

    def exit_detail_for(self, key: str) -> str | None:
        return self._exit_detail

    def reap_terminated(self, key: str) -> int:
        self.reaped.append(key)
        return 1

    def reap(self, key: str) -> int:
        return 1


class _NotifierSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


class _ClearedSpy:
    """Zero-arg wedge-cleared notifier double (#3496 review)."""

    def __init__(self, *, raises: bool = False) -> None:
        self.count = 0
        self._raises = raises

    def __call__(self) -> None:
        self.count += 1
        if self._raises:
            raise RuntimeError("withdrawal boom")


_PROPOSE_PAYLOAD = {"producer": "coder"}
_ACK_PAYLOAD = {"pending_reviews": [{"producer": "coder", "proposal_commit_sha": "deadbeef1"}]}


def _script(monkeypatch, mapping):
    import event_loop

    def _fake_derive(tracker, role):
        return mapping.get(role, ("wait", None, "scripted-default"))

    monkeypatch.setattr(event_loop, "_derive_next_action", _fake_derive, raising=True)


def _make_loop(
    spawner,
    *,
    supervisor,
    clock=None,
    roles=None,
    notifier=None,
    cleared_notifier=None,
    status_view=None,
    agent_free_handler=None,
    pipeline_id="issue-3496",
    slice_id="slice-3",
):
    import event_loop

    return event_loop.OrchestratorEventLoop(
        tracker=object(),
        spawner=spawner,
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        phase="implement",
        clock=clock or _ManualClock(),
        agent_free_handler=agent_free_handler or _AgentFreeRecorder(),
        roles=roles or ["reviewer_code"],
        job_supervisor=supervisor,
        job_status_view=status_view,
        arms_exhausted_notifier=notifier,
        arms_exhausted_cleared_notifier=cleared_notifier,
    )


def _key_for(loop, role, action, payload):
    import event_loop

    identity = event_loop.event_identity(action, payload)
    return event_loop.compute_dedupe_key(
        loop.pipeline_id, loop.slice_id, loop.phase, role, action, identity
    )


def _exhaust(supervisor, key, action, role):
    import supervision_policy

    for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
        supervisor.record_abort(key, action, role)
    assert supervisor.is_exhausted(key)


# ---------------------------------------------------------------------------
# Supervisor: exit history, exhausted_report, reset_exhausted
# ---------------------------------------------------------------------------


class TestExitHistoryAndReport:
    def test_abort_records_category_and_detail(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        supervisor.record_abort("key-1", "ack", "reviewer_code", exit_detail="exit_code=137")
        entry = supervisor._exit_history["key-1"][0]
        assert entry["category"] == "abnormal"
        assert entry["detail"] == "exit_code=137"
        assert entry["at"]  # wall-clock stamp present

    def test_history_bounded(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        for n in range(event_loop.SUPERVISION_EXIT_HISTORY_MAX + 3):
            supervisor.record_abort("key-1", "ack", "reviewer_code", exit_detail=f"n={n}")
        history = supervisor._exit_history["key-1"]
        assert len(history) == event_loop.SUPERVISION_EXIT_HISTORY_MAX
        # Oldest entries were evicted; the newest survives.
        assert history[-1]["detail"] == f"n={event_loop.SUPERVISION_EXIT_HISTORY_MAX + 2}"

    def test_fatal_records_fatal_category(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        supervisor.record_fatal("key-f", "ack", "tester", exit_detail="exit_code=86")
        entry = supervisor._exit_history["key-f"][0]
        assert entry["category"] == "fatal"
        assert entry["detail"] == "exit_code=86"

    def test_success_and_retire_clear_history(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        supervisor.record_abort("key-1", "ack", "reviewer_code")
        supervisor.record_success("key-1")
        assert "key-1" not in supervisor._exit_history
        supervisor.record_abort("key-2", "ack", "reviewer_code")
        supervisor.retire("key-2")
        assert "key-2" not in supervisor._exit_history

    def test_exhausted_report_shape(self):
        import event_loop
        import supervision_policy

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        _exhaust(supervisor, "key-a", "ack", "reviewer_code")
        supervisor.record_fatal("key-b", "ack", "tester", exit_detail="exit_code=86")

        report = supervisor.exhausted_report()
        assert len(report) == 2
        by_key = {entry["dedupe_key"]: entry for entry in report}
        assert by_key["key-a"]["role"] == "reviewer_code"
        assert by_key["key-a"]["action"] == "ack"
        assert by_key["key-a"]["streak"] == supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT
        assert "abnormal" in by_key["key-a"]["exit_history_text"]
        assert "fatal (exit_code=86)" in by_key["key-b"]["exit_history_text"]

    def test_reset_exhausted_returns_keys_and_gives_fresh_budget(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        _exhaust(supervisor, "key-a", "ack", "reviewer_code")
        cleared = supervisor.reset_exhausted()
        assert cleared == ["key-a"]
        assert not supervisor.is_exhausted("key-a")
        assert supervisor.backoff_seconds("key-a") == 0
        assert supervisor.ready_to_respawn("key-a")

    def test_reexhaustion_after_reset_realerts(self):
        """The reset must clear the once-per-key alert latch: a retried arm
        that fails all the way back to exhaustion must re-exhaust and re-fire
        the alert, not retry forever behind a spent latch."""
        import event_loop

        alerts: list[dict] = []
        supervisor = event_loop.JobSupervisor(
            clock=_ManualClock(), overseer_alert=lambda **kw: alerts.append(kw)
        )
        _exhaust(supervisor, "key-a", "ack", "reviewer_code")
        assert len(alerts) == 1
        supervisor.reset_exhausted()
        _exhaust(supervisor, "key-a", "ack", "reviewer_code")
        assert supervisor.is_exhausted("key-a")
        assert len(alerts) == 2


# ---------------------------------------------------------------------------
# Loop: all-arms-exhausted wedge detection + in-band reset
# ---------------------------------------------------------------------------


class TestArmsExhaustedThroughLoop:
    def test_wedge_fires_notifier_once_with_report(self, monkeypatch):
        import event_loop

        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "x")})
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        notifier = _NotifierSpy()
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner, supervisor=supervisor, notifier=notifier)
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")

        decisions = loop.poll_once(["reviewer_code"])
        assert decisions[0].blocked == "exhausted"
        assert spawner.calls == []
        assert len(notifier.calls) == 1
        call = notifier.calls[0]
        assert call["blocked_arms"] == [("reviewer_code", "ack")]
        assert call["report"][0]["dedupe_key"] == key

        # Sticky latch: the wedge persists but the notifier fires once.
        loop.poll_once(["reviewer_code"])
        assert len(notifier.calls) == 1

    def test_reset_exhausted_arms_unblocks_and_rearms(self, monkeypatch):
        import event_loop

        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "x")})
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        notifier = _NotifierSpy()
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner, supervisor=supervisor, notifier=notifier)
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")
        loop.poll_once(["reviewer_code"])
        assert len(notifier.calls) == 1

        cleared = loop.reset_exhausted_arms()
        assert cleared == [key]

        # The blocked arm respawns on the next poll.
        decisions = loop.poll_once(["reviewer_code"])
        assert decisions[0].spawned is True
        assert [c["dedupe_key"] for c in spawner.calls] == [key]

        # A wedge that re-forms after the reset re-fires the notifier
        # (fresh episode, re-armed latch).
        loop._live_keys.discard(key)
        _exhaust(supervisor, key, "ack", "reviewer_code")
        loop.poll_once(["reviewer_code"])
        assert len(notifier.calls) == 2

    def test_report_excludes_stale_exhausted_keys(self, monkeypatch):
        """Only the currently-blocked arm's key reaches the notifier report;
        a stale exhausted key from a superseded round is filtered out (#3496
        review)."""
        import event_loop

        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "x")})
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        notifier = _NotifierSpy()
        loop = _make_loop(_RecordingSpawner(), supervisor=supervisor, notifier=notifier)
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")
        # A stale key from a superseded BRC round: exhausted but no longer the
        # dedupe key any derivable arm resolves to.
        _exhaust(supervisor, "stale-superseded-key", "ack", "reviewer_code")

        loop.poll_once(["reviewer_code"])
        assert len(notifier.calls) == 1
        reported = [e["dedupe_key"] for e in notifier.calls[0]["report"]]
        assert reported == [key]

    def test_wedge_clear_fires_cleared_notifier_once(self, monkeypatch):
        """The cleared-notifier fires exactly once on the wedged→clear edge
        (#3496 review) — the hook the executor uses to auto-withdraw the
        now-stale HITL."""
        import event_loop

        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "x")})
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        notifier = _NotifierSpy()
        cleared = _ClearedSpy()
        loop = _make_loop(
            _RecordingSpawner(),
            supervisor=supervisor,
            notifier=notifier,
            cleared_notifier=cleared,
        )
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")

        loop.poll_once(["reviewer_code"])
        assert len(notifier.calls) == 1
        assert cleared.count == 0

        # The wedge clears by another route: the role now derives a benign
        # wait (e.g. an unrelated decision re-keyed the arm).
        _script(monkeypatch, {})  # reviewer_code now derives wait
        loop.poll_once(["reviewer_code"])
        assert cleared.count == 1

        # Idempotent: staying clear does not re-fire.
        loop.poll_once(["reviewer_code"])
        assert cleared.count == 1

    def test_cleared_notifier_failure_is_swallowed(self, monkeypatch):
        """A withdrawal-side failure must never propagate into poll_once."""
        import event_loop

        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "x")})
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        cleared = _ClearedSpy(raises=True)
        loop = _make_loop(
            _RecordingSpawner(),
            supervisor=supervisor,
            notifier=_NotifierSpy(),
            cleared_notifier=cleared,
        )
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")
        loop.poll_once(["reviewer_code"])

        _script(monkeypatch, {})
        # Must not raise even though the notifier throws.
        loop.poll_once(["reviewer_code"])
        assert cleared.count == 1

    def test_reset_does_not_fire_cleared_notifier(self, monkeypatch):
        """An operator "Retry arms" reset clears the latch directly — the
        decision is already resolved, so no auto-withdrawal transition fires."""
        import event_loop

        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "x")})
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        cleared = _ClearedSpy()
        loop = _make_loop(
            _RecordingSpawner(),
            supervisor=supervisor,
            notifier=_NotifierSpy(),
            cleared_notifier=cleared,
        )
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")
        loop.poll_once(["reviewer_code"])

        loop.reset_exhausted_arms()
        # The reset re-armed the latch to False without a wedge-clear
        # transition; the next (now-spawning) poll must not fire the notifier.
        loop.poll_once(["reviewer_code"])
        assert cleared.count == 0

    def test_live_job_suppresses_wedge(self, monkeypatch):
        """An in-flight pod for any role means progress may still occur."""
        import event_loop

        _script(
            monkeypatch,
            {
                "coder": ("propose", _PROPOSE_PAYLOAD, "x"),
                "reviewer_code": ("ack", _ACK_PAYLOAD, "x"),
            },
        )
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        notifier = _NotifierSpy()
        loop = _make_loop(
            _RecordingSpawner(),
            supervisor=supervisor,
            notifier=notifier,
            roles=["coder", "reviewer_code"],
        )
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")

        # The coder propose spawns (live key) while the reviewer arm is
        # blocked — not a wedge.
        loop.poll_once(["coder", "reviewer_code"])
        assert notifier.calls == []

    def test_partial_exhaustion_does_not_fire(self, monkeypatch):
        """A non-exhausted spawnable arm (here: in backoff) is not a wedge."""
        import event_loop

        _script(
            monkeypatch,
            {
                "reviewer_code": ("ack", _ACK_PAYLOAD, "x"),
                "tester": ("ack", _ACK_PAYLOAD, "x"),
            },
        )
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        notifier = _NotifierSpy()
        loop = _make_loop(
            _RecordingSpawner(),
            supervisor=supervisor,
            notifier=notifier,
            clock=clock,
            roles=["reviewer_code", "tester"],
        )
        reviewer_key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        tester_key = _key_for(loop, "tester", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, reviewer_key, "ack", "reviewer_code")
        # The tester arm aborted once — backing off, NOT exhausted.
        supervisor.record_abort(tester_key, "ack", "tester")

        loop.poll_once(["reviewer_code", "tester"])
        assert notifier.calls == []

    def test_wait_only_roles_do_not_fire(self, monkeypatch):
        import event_loop

        _script(monkeypatch, {})  # every role derives wait
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        _exhaust(supervisor, "stale-key", "ack", "reviewer_code")
        notifier = _NotifierSpy()
        loop = _make_loop(_RecordingSpawner(), supervisor=supervisor, notifier=notifier)

        loop.poll_once(["reviewer_code"])
        assert notifier.calls == []

    def test_agent_free_progress_suppresses_wedge(self, monkeypatch):
        import event_loop

        _script(
            monkeypatch,
            {
                "coder": ("confirm", None, "x"),
                "reviewer_code": ("ack", _ACK_PAYLOAD, "x"),
            },
        )
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        notifier = _NotifierSpy()
        loop = _make_loop(
            _RecordingSpawner(),
            supervisor=supervisor,
            notifier=notifier,
            roles=["coder", "reviewer_code"],
        )
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")

        loop.poll_once(["coder", "reviewer_code"])
        assert notifier.calls == []

    def test_no_notifier_wired_is_harmless(self, monkeypatch):
        import event_loop

        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "x")})
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        loop = _make_loop(_RecordingSpawner(), supervisor=supervisor, notifier=None)
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")

        decisions = loop.poll_once(["reviewer_code"])
        assert decisions[0].blocked == "exhausted"

    def test_exit_detail_threaded_from_status_view(self, monkeypatch):
        """The abnormal observation branch reads the pod's exit detail before
        reaping and records it into the supervisor's history."""
        import event_loop

        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "x")})
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        view = _FakeJobStatusView(exit_detail="exit_code=137")
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner, supervisor=supervisor, clock=clock, status_view=view)
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)

        loop.poll_once(["reviewer_code"])  # spawn
        view.set(key, event_loop.JOB_OUTCOME_ABNORMAL)
        loop.poll_once(["reviewer_code"])  # observe abnormal

        entry = supervisor._exit_history[key][0]
        assert entry["category"] == "abnormal"
        assert entry["detail"] == "exit_code=137"


# ---------------------------------------------------------------------------
# Live-loop registry
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_registry():
    import event_loop

    with event_loop._LIVE_LOOPS_LOCK:
        event_loop._LIVE_LOOPS.clear()
    yield
    with event_loop._LIVE_LOOPS_LOCK:
        event_loop._LIVE_LOOPS.clear()


class TestLiveLoopRegistry:
    def test_start_registers_and_stop_unregisters(self, _clean_registry):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        loop = _make_loop(_RecordingSpawner(), supervisor=supervisor)
        try:
            loop.start()
            assert event_loop.get_live_event_loops("issue-3496") == [loop]
            assert event_loop.get_live_event_loops("issue-other") == []
        finally:
            loop.stop()
        assert event_loop.get_live_event_loops("issue-3496") == []

    def test_stale_unregister_does_not_evict_replacement(self, _clean_registry):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        loop_a = _make_loop(_RecordingSpawner(), supervisor=supervisor)
        loop_b = _make_loop(_RecordingSpawner(), supervisor=supervisor)
        event_loop._register_live_loop(loop_a)
        # A phase restart registers a fresh loop under the same key...
        event_loop._register_live_loop(loop_b)
        # ...and the superseded loop's late stop must not evict it.
        event_loop._unregister_live_loop(loop_a)
        assert event_loop.get_live_event_loops("issue-3496") == [loop_b]


# ---------------------------------------------------------------------------
# Executor escalation: OVERSEER_ALERT + persisted HITL decision
# ---------------------------------------------------------------------------


_REPORT = [
    {
        "dedupe_key": "abc123",
        "role": "reviewer_code",
        "action": "ack",
        "streak": 10,
        "exit_history": [],
        "exit_history_text": "2026-07-04T03:45:00+00:00 abnormal (exit_code=1)",
    }
]
_BLOCKED = [("reviewer_code", "ack")]


def _make_executor(slice_id="slice-3"):
    from concurrent_executor import ConcurrentPhaseExecutor
    from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus

    pipeline = Pipeline(
        id="issue-3496",
        repo="test/repo",
        issue_number=3496,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=PipelineConfig(),
    )
    return ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), slice_id=slice_id)


class TestExecutorEscalation:
    @patch("routes.pipelines._persist_hitl_decision")
    @patch("routes.get_state_store_for_pipeline")
    @patch("concurrent_executor.get_message_store")
    def test_emits_alert_and_persists_decision(self, mock_store, mock_get_state, mock_persist):
        from concurrent_executor import (
            ARMS_EXHAUSTED_ABORT_OPTION,
            ARMS_EXHAUSTED_HITL_CONTEXT,
            ARMS_EXHAUSTED_RESTART_OPTION,
            ARMS_EXHAUSTED_RETRY_OPTION,
        )
        from message_store import MessageType

        disk_pipeline = MagicMock()
        disk_pipeline.get_pending_decisions.return_value = []
        mock_get_state.return_value = (MagicMock(), disk_pipeline)
        mock_persist.return_value = MagicMock(id="decision-42")

        executor = _make_executor()
        executor._handle_arms_exhausted(report=_REPORT, blocked_arms=_BLOCKED)

        msg = mock_store.return_value.add_message.call_args[0][0]
        assert msg.message_type == MessageType.OVERSEER_ALERT
        assert msg.metadata["anomaly"] == "event-arms-exhausted"

        kwargs = mock_persist.call_args.kwargs
        assert kwargs["context"] == ARMS_EXHAUSTED_HITL_CONTEXT
        assert kwargs["options"] == [
            ARMS_EXHAUSTED_RETRY_OPTION,
            ARMS_EXHAUSTED_RESTART_OPTION,
            ARMS_EXHAUSTED_ABORT_OPTION,
        ]
        assert "reviewer_code/ack" in kwargs["question"]
        assert "exit_code=1" in kwargs["question"]

    @patch("routes.pipelines._persist_hitl_decision")
    @patch("routes.get_state_store_for_pipeline")
    @patch("concurrent_executor.get_message_store")
    def test_dedupes_on_pending_decision(self, mock_store, mock_get_state, mock_persist):
        from concurrent_executor import ARMS_EXHAUSTED_HITL_CONTEXT

        pending = MagicMock()
        pending.context = ARMS_EXHAUSTED_HITL_CONTEXT
        disk_pipeline = MagicMock()
        disk_pipeline.get_pending_decisions.return_value = [pending]
        mock_get_state.return_value = (MagicMock(), disk_pipeline)

        executor = _make_executor()
        executor._handle_arms_exhausted(report=_REPORT, blocked_arms=_BLOCKED)

        mock_persist.assert_not_called()
        # #3496 review: the dedup gate suppresses BOTH surfaces — a re-armed
        # latch must not re-broadcast the OVERSEER_ALERT either.
        mock_store.return_value.add_message.assert_not_called()

    @patch("routes.pipelines._persist_hitl_decision")
    @patch("routes.get_state_store_for_pipeline")
    @patch("concurrent_executor.get_message_store")
    def test_read_failure_still_alerts(self, mock_store, mock_get_state, mock_persist):
        """A pending-decisions read failure must not swallow the escalation:
        the alert still fires (the HITL cannot, without a store)."""
        from message_store import MessageType

        mock_get_state.side_effect = RuntimeError("store down")

        executor = _make_executor()
        executor._handle_arms_exhausted(report=_REPORT, blocked_arms=_BLOCKED)

        msg = mock_store.return_value.add_message.call_args[0][0]
        assert msg.message_type == MessageType.OVERSEER_ALERT
        mock_persist.assert_not_called()

    @patch("routes.get_state_store_for_pipeline", side_effect=RuntimeError("store down"))
    @patch("concurrent_executor.get_message_store")
    def test_persistence_failure_never_raises(self, mock_store, mock_get_state):
        executor = _make_executor()
        # Must not raise — escalation failure cannot wedge the event loop.
        executor._handle_arms_exhausted(report=_REPORT, blocked_arms=_BLOCKED)


# ---------------------------------------------------------------------------
# Executor: auto-withdrawal on wedge-clear (#3496 review)
# ---------------------------------------------------------------------------


class TestArmsExhaustedWithdrawal:
    @patch("routes.pipelines._withdraw_arms_exhausted_decisions", return_value=1)
    @patch("routes.get_state_store_for_pipeline")
    @patch("event_loop.get_live_event_loops", return_value=[])
    def test_withdraw_runs_when_no_sibling_wedged(
        self, mock_loops, mock_get_state, mock_withdraw
    ):
        mock_get_state.return_value = (MagicMock(), MagicMock())
        executor = _make_executor()
        executor._withdraw_arms_exhausted_hitl()

        mock_get_state.assert_called_once_with("issue-3496")
        mock_withdraw.assert_called_once()

    @patch("routes.pipelines._withdraw_arms_exhausted_decisions")
    @patch("routes.get_state_store_for_pipeline")
    @patch("event_loop.get_live_event_loops")
    def test_withdraw_skipped_when_sibling_still_wedged(
        self, mock_loops, mock_get_state, mock_withdraw
    ):
        sibling = MagicMock()
        sibling.arms_exhausted_escalated = True
        mock_loops.return_value = [sibling]

        executor = _make_executor()
        executor._withdraw_arms_exhausted_hitl()

        # A still-wedged sibling holds the shared decision in place.
        mock_withdraw.assert_not_called()
        mock_get_state.assert_not_called()

    @patch("routes.get_state_store_for_pipeline", side_effect=RuntimeError("store down"))
    @patch("event_loop.get_live_event_loops", return_value=[])
    def test_withdraw_never_raises(self, mock_loops, mock_get_state):
        executor = _make_executor()
        # Must not raise — a withdrawal failure cannot wedge the event loop.
        executor._withdraw_arms_exhausted_hitl()

    def test_withdraw_decisions_helper_cancels_only_matching(self):
        from models import DecisionStatus
        from routes.pipelines import _withdraw_arms_exhausted_decisions

        arms = _arms_decision(None)  # context = ARMS_EXHAUSTED_HITL_CONTEXT
        arms.status = DecisionStatus.PENDING
        other = _arms_decision(None, context="failed_role:coder")
        other.status = DecisionStatus.PENDING

        disk_pipeline = MagicMock()
        disk_pipeline.get_pending_decisions.return_value = [arms, other]
        store = MagicMock()
        store.load_pipeline.return_value = disk_pipeline

        withdrawn = _withdraw_arms_exhausted_decisions("issue-3496", store)

        assert withdrawn == 1
        assert arms.status == DecisionStatus.CANCELLED
        assert "auto-withdrawn" in (arms.resolution or "")
        assert other.status == DecisionStatus.PENDING
        store.save_pipeline.assert_called_once_with(disk_pipeline)

    def test_withdraw_decisions_helper_noop_when_none_pending(self):
        from routes.pipelines import _withdraw_arms_exhausted_decisions

        disk_pipeline = MagicMock()
        disk_pipeline.get_pending_decisions.return_value = []
        store = MagicMock()
        store.load_pipeline.return_value = disk_pipeline

        assert _withdraw_arms_exhausted_decisions("issue-3496", store) == 0
        store.save_pipeline.assert_not_called()


# ---------------------------------------------------------------------------
# Resolve-decision dispatch
# ---------------------------------------------------------------------------


def _arms_decision(resolution: str | None, context: str | None = None):
    from concurrent_executor import (
        ARMS_EXHAUSTED_ABORT_OPTION,
        ARMS_EXHAUSTED_HITL_CONTEXT,
        ARMS_EXHAUSTED_RESTART_OPTION,
        ARMS_EXHAUSTED_RETRY_OPTION,
    )
    from models import DecisionStatus, HITLDecision, PipelinePhase

    return HITLDecision(
        id="decision-9",
        question="Event loop wedged. How to proceed?",
        context=ARMS_EXHAUSTED_HITL_CONTEXT if context is None else context,
        options=[
            ARMS_EXHAUSTED_RETRY_OPTION,
            ARMS_EXHAUSTED_RESTART_OPTION,
            ARMS_EXHAUSTED_ABORT_OPTION,
        ],
        phase=PipelinePhase.IMPLEMENT,
        status=DecisionStatus.RESOLVED,
        resolution=resolution,
    )


class TestArmsExhaustedDispatch:
    def test_retry_arms_resets_live_loops(self):
        from concurrent_executor import ARMS_EXHAUSTED_RETRY_OPTION
        from routes.decisions import _maybe_dispatch_arms_exhausted_resolution

        loop = MagicMock()
        loop.slice_id = "slice-3"
        loop.reset_exhausted_arms.return_value = ["key-a", "key-b"]
        with patch("event_loop.get_live_event_loops", return_value=[loop]) as mock_get:
            result = _maybe_dispatch_arms_exhausted_resolution(
                "issue-3496",
                _arms_decision(ARMS_EXHAUSTED_RETRY_OPTION),
                ARMS_EXHAUSTED_RETRY_OPTION,
            )

        mock_get.assert_called_once_with("issue-3496")
        loop.reset_exhausted_arms.assert_called_once_with()
        assert result["action"] == "reset_exhausted_arms"
        assert result["success"] is True
        assert result["cleared_total"] == 2
        assert result["cleared_by_slice"] == {"slice-3": ["key-a", "key-b"]}

    def test_retry_arms_without_live_loop_fails_informatively(self):
        from concurrent_executor import ARMS_EXHAUSTED_RETRY_OPTION
        from routes.decisions import _maybe_dispatch_arms_exhausted_resolution

        with patch("event_loop.get_live_event_loops", return_value=[]):
            result = _maybe_dispatch_arms_exhausted_resolution(
                "issue-3496",
                _arms_decision(ARMS_EXHAUSTED_RETRY_OPTION),
                ARMS_EXHAUSTED_RETRY_OPTION,
            )

        assert result["action"] == "reset_exhausted_arms"
        assert result["success"] is False
        assert "Restart phase" in result["error"]

    @patch("routes.pipelines.restart_phase")
    def test_restart_phase_executes_in_process(self, mock_restart):
        from concurrent_executor import ARMS_EXHAUSTED_RESTART_OPTION
        from routes.decisions import _maybe_dispatch_arms_exhausted_resolution

        mock_restart.return_value = (MagicMock(), 200)
        result = _maybe_dispatch_arms_exhausted_resolution(
            "issue-3496",
            _arms_decision(ARMS_EXHAUSTED_RESTART_OPTION),
            ARMS_EXHAUSTED_RESTART_OPTION,
        )

        mock_restart.assert_called_once_with("issue-3496", "implement")
        assert result == {"action": "restart_phase", "phase": "implement", "success": True}

    def test_abort_resolves_with_explicit_note(self):
        from concurrent_executor import ARMS_EXHAUSTED_ABORT_OPTION
        from routes.decisions import _maybe_dispatch_arms_exhausted_resolution

        result = _maybe_dispatch_arms_exhausted_resolution(
            "issue-3496",
            _arms_decision(ARMS_EXHAUSTED_ABORT_OPTION),
            ARMS_EXHAUSTED_ABORT_OPTION,
        )

        assert result["action"] == "arms_exhausted_abort"
        assert result["success"] is True
        assert "cancel_task" in result["note"]

    def test_other_contexts_are_ignored(self):
        from concurrent_executor import ARMS_EXHAUSTED_RETRY_OPTION
        from routes.decisions import _maybe_dispatch_arms_exhausted_resolution

        result = _maybe_dispatch_arms_exhausted_resolution(
            "issue-3496",
            _arms_decision(ARMS_EXHAUSTED_RETRY_OPTION, context="failed_role:coder"),
            ARMS_EXHAUSTED_RETRY_OPTION,
        )
        assert result is None

    def test_free_form_resolution_is_ignored(self):
        from routes.decisions import _maybe_dispatch_arms_exhausted_resolution

        result = _maybe_dispatch_arms_exhausted_resolution(
            "issue-3496", _arms_decision("hmm"), "let me think"
        )
        assert result is None

    def test_end_to_end_retry_against_real_loop(self, _clean_registry, monkeypatch):
        """Full in-band recovery: a real wedged loop, reset through the real
        registry by the dispatch hook, respawns on the next poll."""
        import event_loop
        from concurrent_executor import ARMS_EXHAUSTED_RETRY_OPTION
        from routes.decisions import _maybe_dispatch_arms_exhausted_resolution

        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "x")})
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner, supervisor=supervisor)
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)
        _exhaust(supervisor, key, "ack", "reviewer_code")
        event_loop._register_live_loop(loop)

        assert loop.poll_once(["reviewer_code"])[0].blocked == "exhausted"

        result = _maybe_dispatch_arms_exhausted_resolution(
            "issue-3496",
            _arms_decision(ARMS_EXHAUSTED_RETRY_OPTION),
            ARMS_EXHAUSTED_RETRY_OPTION,
        )
        assert result["success"] is True
        assert result["cleared_by_slice"] == {"slice-3": [key]}

        decisions = loop.poll_once(["reviewer_code"])
        assert decisions[0].spawned is True
        assert [c["dedupe_key"] for c in spawner.calls] == [key]
