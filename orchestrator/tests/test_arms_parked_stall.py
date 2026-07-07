"""Tests for the all-arms-parked stall surfacing + restart-arm invalidation (#3548).

The incident: after a mid-slice orchestrator restart, the producers'
propose arms no-op-parked (their one-shot agents kept exiting cleanly with
zero BRC progress) while the round could not converge (the zero-proposal
guard rejects every confirm until every producer proposes). The event loop
sat silent for the full 30-minute park heartbeat with ``pending_decisions``
empty, and ``restart_agent`` reported "respawn delegated to event loop"
while the loop's ``_live_keys`` dedupe + supervisor latches guaranteed no
respawn would ever derive. These tests pin the fixes:

* the park early-return tags its decision ``blocked="parked"`` so the
  wedge detection can see it;
* the supervisor exposes ``noop_park_report()`` / ``reset_noop_parks()``
  (the park twins of the #3496 exhausted primitives);
* the loop detects the all-arms-parked wedge (including mixed
  parked+exhausted rounds, which fall outside the #3496 detector) and
  fires ``arms_parked_notifier`` once per episode, with a cleared
  notifier on the wedged→clear transition;
* the executor escalates the wedge as an OVERSEER_ALERT + a persisted
  HITL decision deduped on the ``event_arms_parked`` context;
* the resolve-decision dispatch executes "Retry arms (release no-op
  parks)" against the live-loop registry;
* ``OrchestratorEventLoop.invalidate_role_arms`` drops a restarted role's
  keys from ``_live_keys`` / ``_key_meta`` and retires their supervisor
  state, so the restart route's delegation claim is actually true.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


# ---------------------------------------------------------------------------
# Test doubles (mirroring test_arms_exhausted_livelock.py's conventions)
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


class _NotifierSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


class _ClearedSpy:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


_ACK_PAYLOAD = {"pending_reviews": [{"producer": "documenter", "proposal_commit_sha": "d4df6c7"}]}
_PROPOSE_PAYLOAD = {"producer": "coder"}


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
    parked_notifier=None,
    parked_cleared_notifier=None,
    exhausted_notifier=None,
    pipeline_id="issue-3548",
    slice_id="slice-8",
):
    import event_loop

    return event_loop.OrchestratorEventLoop(
        tracker=object(),
        spawner=spawner,
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        phase="implement",
        clock=clock or _ManualClock(),
        roles=roles or ["coder"],
        job_supervisor=supervisor,
        arms_exhausted_notifier=exhausted_notifier,
        arms_parked_notifier=parked_notifier,
        arms_parked_cleared_notifier=parked_cleared_notifier,
    )


def _key_for(loop, role, action, payload):
    import event_loop

    identity = event_loop.event_identity(action, payload)
    return event_loop.compute_dedupe_key(
        loop.pipeline_id, loop.slice_id, loop.phase, role, action, identity
    )


def _park(supervisor, key, action, role):
    import supervision_policy

    for _ in range(supervision_policy.SUPERVISION_NOOP_STREAK_PARK):
        supervisor.record_success(key, action=action, role=role)


def _exhaust(supervisor, key, action, role):
    import supervision_policy

    for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
        supervisor.record_abort(key, action, role)
    assert supervisor.is_exhausted(key)


# ---------------------------------------------------------------------------
# Supervisor: noop_park_report + reset_noop_parks
# ---------------------------------------------------------------------------


class TestNoopParkPrimitives:
    def test_park_report_shape(self):
        import event_loop
        import supervision_policy

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        _park(supervisor, "key-a", "propose", "coder")
        supervisor.record_success("key-b", action="ack", role="reviewer_code")  # not parked

        report = supervisor.noop_park_report()
        assert len(report) == 1
        entry = report[0]
        assert entry["dedupe_key"] == "key-a"
        assert entry["role"] == "coder"
        assert entry["action"] == "propose"
        assert entry["noop_streak"] == supervision_policy.SUPERVISION_NOOP_STREAK_PARK

    def test_reset_noop_parks_clears_and_returns_keys(self):
        import event_loop

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        _park(supervisor, "key-a", "propose", "coder")
        assert supervisor.noop_parked("key-a")

        cleared = supervisor.reset_noop_parks()
        assert cleared == ["key-a"]
        assert not supervisor.noop_parked("key-a")
        assert supervisor.noop_park_report() == []

    def test_reset_noop_parks_ignores_sub_threshold_streaks(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        supervisor.record_success("key-b", action="ack", role="reviewer_code")
        assert supervisor.reset_noop_parks() == []


# ---------------------------------------------------------------------------
# Loop: park tagging + all-parked wedge detection
# ---------------------------------------------------------------------------


class TestArmsParkedThroughLoop:
    def test_park_decision_tagged_blocked_parked(self, monkeypatch):
        import event_loop

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner, supervisor=supervisor, clock=clock)
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "scripted")})

        key = _key_for(loop, "coder", "propose", _PROPOSE_PAYLOAD)
        _park(supervisor, key, "propose", "coder")

        decisions = loop.poll_once(loop._roles)
        assert len(decisions) == 1
        assert decisions[0].blocked == "parked"
        assert decisions[0].spawned is False
        assert spawner.calls == []

    def test_all_parked_wedge_fires_notifier_once(self, monkeypatch):
        import event_loop

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        spawner = _RecordingSpawner()
        notifier = _NotifierSpy()
        loop = _make_loop(
            spawner,
            supervisor=supervisor,
            clock=clock,
            roles=["coder", "tester"],
            parked_notifier=notifier,
        )
        _script(
            monkeypatch,
            {
                "coder": ("propose", _PROPOSE_PAYLOAD, "scripted"),
                "tester": ("propose", {"producer": "tester"}, "scripted"),
            },
        )
        for role, payload in (("coder", _PROPOSE_PAYLOAD), ("tester", {"producer": "tester"})):
            _park(supervisor, _key_for(loop, role, "propose", payload), "propose", role)

        loop.poll_once(loop._roles)
        assert len(notifier.calls) == 1
        call = notifier.calls[0]
        assert set(call["blocked_arms"]) == {("coder", "propose"), ("tester", "propose")}
        roles = {entry["role"] for entry in call["report"]}
        assert roles == {"coder", "tester"}

        # Sticky latch: a second wedged tick does not re-fire.
        loop.poll_once(loop._roles)
        assert len(notifier.calls) == 1

    def test_mixed_parked_and_exhausted_counts_as_wedged(self, monkeypatch):
        """A round with one parked and one exhausted arm falls outside the
        #3496 all-exhausted predicate; the parked detector must own it."""
        import event_loop

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        spawner = _RecordingSpawner()
        parked_notifier = _NotifierSpy()
        exhausted_notifier = _NotifierSpy()
        loop = _make_loop(
            spawner,
            supervisor=supervisor,
            clock=clock,
            roles=["coder", "reviewer_code"],
            parked_notifier=parked_notifier,
            exhausted_notifier=exhausted_notifier,
        )
        _script(
            monkeypatch,
            {
                "coder": ("propose", _PROPOSE_PAYLOAD, "scripted"),
                "reviewer_code": ("ack", _ACK_PAYLOAD, "scripted"),
            },
        )
        _park(supervisor, _key_for(loop, "coder", "propose", _PROPOSE_PAYLOAD), "propose", "coder")
        _exhaust(
            supervisor, _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD), "ack", "reviewer_code"
        )

        loop.poll_once(loop._roles)
        assert len(parked_notifier.calls) == 1
        assert len(exhausted_notifier.calls) == 0
        call = parked_notifier.calls[0]
        assert {entry["role"] for entry in call["report"]} == {"coder"}
        assert {entry["role"] for entry in call["exhausted_report"]} == {"reviewer_code"}

    def test_not_wedged_when_an_arm_spawns(self, monkeypatch):
        import event_loop

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        spawner = _RecordingSpawner()
        notifier = _NotifierSpy()
        loop = _make_loop(
            spawner,
            supervisor=supervisor,
            clock=clock,
            roles=["coder", "tester"],
            parked_notifier=notifier,
        )
        _script(
            monkeypatch,
            {
                "coder": ("propose", _PROPOSE_PAYLOAD, "scripted"),
                "tester": ("propose", {"producer": "tester"}, "scripted"),
            },
        )
        # Only coder is parked; tester spawns freely.
        _park(supervisor, _key_for(loop, "coder", "propose", _PROPOSE_PAYLOAD), "propose", "coder")

        loop.poll_once(loop._roles)
        assert notifier.calls == []
        assert len(spawner.calls) == 1

    def test_cleared_notifier_fires_on_transition(self, monkeypatch):
        import event_loop

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        spawner = _RecordingSpawner()
        notifier = _NotifierSpy()
        cleared = _ClearedSpy()
        loop = _make_loop(
            spawner,
            supervisor=supervisor,
            clock=clock,
            parked_notifier=notifier,
            parked_cleared_notifier=cleared,
        )
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "scripted")})
        key = _key_for(loop, "coder", "propose", _PROPOSE_PAYLOAD)
        _park(supervisor, key, "propose", "coder")

        loop.poll_once(loop._roles)
        assert len(notifier.calls) == 1
        assert loop.arms_parked_escalated

        # The wedge clears by another route (the role's derived action
        # changes — e.g. the cohort progressed and there is nothing to
        # spawn): the stale HITL is auto-withdrawn exactly once.
        _script(monkeypatch, {"coder": ("wait", None, "scripted")})
        loop.poll_once(loop._roles)
        assert cleared.count == 1
        assert not loop.arms_parked_escalated

        # An operator reset, by contrast, resolves the wedge in-band and
        # clears the latch itself — no auto-withdrawal fires for it.
        assert loop.reset_parked_arms() == [key]
        loop.poll_once(loop._roles)
        assert cleared.count == 1

    def test_reset_parked_arms_rearms_latch_for_reescalation(self, monkeypatch):
        import event_loop
        import supervision_policy

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        spawner = _RecordingSpawner()
        notifier = _NotifierSpy()
        loop = _make_loop(spawner, supervisor=supervisor, clock=clock, parked_notifier=notifier)
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "scripted")})
        key = _key_for(loop, "coder", "propose", _PROPOSE_PAYLOAD)
        _park(supervisor, key, "propose", "coder")

        loop.poll_once(loop._roles)
        assert len(notifier.calls) == 1
        loop.reset_parked_arms()

        # The arm re-parks after another no-op streak; the wedge re-escalates.
        loop.poll_once(loop._roles)  # spawns the probe
        loop._live_keys.discard(key)
        for _ in range(supervision_policy.SUPERVISION_NOOP_STREAK_PARK):
            supervisor.record_success(key, action="propose", role="coder")
        loop.poll_once(loop._roles)
        assert len(notifier.calls) == 2


# ---------------------------------------------------------------------------
# Loop: invalidate_role_arms (the restart_agent companion)
# ---------------------------------------------------------------------------


class TestInvalidateRoleArms:
    def test_live_key_invalidated_and_respawned(self, monkeypatch):
        import event_loop

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner, supervisor=supervisor, clock=clock, roles=["reviewer_code"])
        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "scripted")})
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)

        loop.poll_once(loop._roles)
        assert len(spawner.calls) == 1
        # The incident shape: the Job was deleted out-of-band, but the key
        # stays live (a missing Job maps to "running"), so every subsequent
        # poll dedupes.
        loop.poll_once(loop._roles)
        assert len(spawner.calls) == 1

        invalidated = loop.invalidate_role_arms("reviewer_code")
        assert invalidated == [key]
        assert key not in loop._live_keys
        assert key not in loop._key_meta

        loop.poll_once(loop._roles)
        assert len(spawner.calls) == 2
        assert spawner.calls[-1]["dedupe_key"] == key

    def test_invalidation_clears_supervisor_latches(self, monkeypatch):
        import event_loop

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner, supervisor=supervisor, clock=clock, roles=["reviewer_code"])
        _script(monkeypatch, {"reviewer_code": ("ack", _ACK_PAYLOAD, "scripted")})
        key = _key_for(loop, "reviewer_code", "ack", _ACK_PAYLOAD)

        loop.poll_once(loop._roles)
        loop._live_keys.discard(key)
        _park(supervisor, key, "ack", "reviewer_code")
        assert loop.poll_once(loop._roles)[0].blocked == "parked"

        loop.invalidate_role_arms("reviewer_code")
        assert not supervisor.noop_parked(key)

        loop.poll_once(loop._roles)
        assert len(spawner.calls) == 2

    def test_other_roles_untouched(self, monkeypatch):
        import event_loop

        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        spawner = _RecordingSpawner()
        loop = _make_loop(
            spawner, supervisor=supervisor, clock=clock, roles=["reviewer_code", "coder"]
        )
        _script(
            monkeypatch,
            {
                "reviewer_code": ("ack", _ACK_PAYLOAD, "scripted"),
                "coder": ("propose", _PROPOSE_PAYLOAD, "scripted"),
            },
        )
        loop.poll_once(loop._roles)
        assert len(loop._live_keys) == 2

        loop.invalidate_role_arms("reviewer_code")
        assert len(loop._live_keys) == 1
        remaining_key = next(iter(loop._live_keys))
        assert loop._key_meta[remaining_key] == ("propose", "coder")

    def test_no_keys_returns_empty(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        loop = _make_loop(_RecordingSpawner(), supervisor=supervisor)
        assert loop.invalidate_role_arms("reviewer_code") == []


# ---------------------------------------------------------------------------
# Executor: arms-parked escalation + withdrawal
# ---------------------------------------------------------------------------


_PARK_REPORT = [
    {
        "dedupe_key": "key-coder",
        "role": "coder",
        "action": "propose",
        "noop_streak": 3,
    }
]
_EXHAUSTED_REPORT = [
    {
        "dedupe_key": "key-rc",
        "role": "reviewer_code",
        "action": "ack",
        "streak": 10,
        "exit_history": [],
        "exit_history_text": "2026-07-07T21:00:00 abnormal (exit_code=1)",
    }
]
_BLOCKED = [("coder", "propose"), ("reviewer_code", "ack")]


def _make_executor(slice_id="slice-8"):
    from concurrent_executor import ConcurrentPhaseExecutor
    from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus

    pipeline = Pipeline(
        id="issue-3548",
        repo="test/repo",
        issue_number=3548,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=PipelineConfig(),
    )
    return ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), slice_id=slice_id)


class TestExecutorParkedEscalation:
    @patch("routes.pipelines._persist_hitl_decision")
    @patch("routes.get_state_store_for_pipeline")
    @patch("concurrent_executor.get_message_store")
    def test_emits_alert_and_persists_decision(self, mock_store, mock_get_state, mock_persist):
        from concurrent_executor import (
            ARMS_EXHAUSTED_ABORT_OPTION,
            ARMS_EXHAUSTED_RESTART_OPTION,
            ARMS_PARKED_HITL_CONTEXT,
            ARMS_PARKED_RETRY_OPTION,
        )
        from message_store import MessageType

        disk_pipeline = MagicMock()
        disk_pipeline.get_pending_decisions.return_value = []
        mock_get_state.return_value = (MagicMock(), disk_pipeline)
        mock_persist.return_value = MagicMock(id="decision-48")

        executor = _make_executor()
        executor._handle_arms_parked(
            report=_PARK_REPORT, exhausted_report=_EXHAUSTED_REPORT, blocked_arms=_BLOCKED
        )

        msg = mock_store.return_value.add_message.call_args[0][0]
        assert msg.message_type == MessageType.OVERSEER_ALERT
        assert msg.metadata["anomaly"] == "event-arms-parked"

        kwargs = mock_persist.call_args.kwargs
        assert kwargs["context"] == ARMS_PARKED_HITL_CONTEXT
        assert kwargs["options"] == [
            ARMS_PARKED_RETRY_OPTION,
            ARMS_EXHAUSTED_RESTART_OPTION,
            ARMS_EXHAUSTED_ABORT_OPTION,
        ]
        assert "coder/propose" in kwargs["question"]
        assert "3 consecutive no-op completions" in kwargs["question"]
        assert "exit_code=1" in kwargs["question"]

    @patch("routes.pipelines._persist_hitl_decision")
    @patch("routes.get_state_store_for_pipeline")
    @patch("concurrent_executor.get_message_store")
    def test_dedupes_on_pending_decision(self, mock_store, mock_get_state, mock_persist):
        from concurrent_executor import ARMS_PARKED_HITL_CONTEXT

        pending = MagicMock()
        pending.context = ARMS_PARKED_HITL_CONTEXT
        disk_pipeline = MagicMock()
        disk_pipeline.get_pending_decisions.return_value = [pending]
        mock_get_state.return_value = (MagicMock(), disk_pipeline)

        executor = _make_executor()
        executor._handle_arms_parked(
            report=_PARK_REPORT, exhausted_report=[], blocked_arms=_BLOCKED
        )

        mock_persist.assert_not_called()
        mock_store.return_value.add_message.assert_not_called()

    @patch("routes.get_state_store_for_pipeline", side_effect=RuntimeError("store down"))
    @patch("concurrent_executor.get_message_store")
    def test_read_failure_still_alerts_and_never_raises(self, mock_store, mock_get_state):
        from message_store import MessageType

        executor = _make_executor()
        executor._handle_arms_parked(
            report=_PARK_REPORT, exhausted_report=[], blocked_arms=_BLOCKED
        )
        msg = mock_store.return_value.add_message.call_args[0][0]
        assert msg.message_type == MessageType.OVERSEER_ALERT

    @patch("routes.pipelines._withdraw_arms_parked_decisions", return_value=1)
    @patch("routes.get_state_store_for_pipeline")
    @patch("event_loop.get_live_event_loops", return_value=[])
    def test_withdrawal_runs_when_no_loop_escalated(
        self, mock_loops, mock_get_state, mock_withdraw
    ):
        mock_get_state.return_value = (MagicMock(), MagicMock())
        executor = _make_executor()
        executor._withdraw_arms_parked_hitl()
        mock_withdraw.assert_called_once()

    @patch("routes.pipelines._withdraw_arms_parked_decisions")
    @patch("routes.get_state_store_for_pipeline")
    @patch("event_loop.get_live_event_loops")
    def test_withdrawal_guarded_by_sibling_slice(self, mock_loops, mock_get_state, mock_withdraw):
        sibling = MagicMock()
        sibling.arms_parked_escalated = True
        mock_loops.return_value = [sibling]
        executor = _make_executor()
        executor._withdraw_arms_parked_hitl()
        mock_withdraw.assert_not_called()


# ---------------------------------------------------------------------------
# Decisions dispatch: "Retry arms (release no-op parks)"
# ---------------------------------------------------------------------------


class TestArmsParkedDispatch:
    def _decision(self):
        from concurrent_executor import ARMS_PARKED_HITL_CONTEXT

        decision = MagicMock()
        decision.context = ARMS_PARKED_HITL_CONTEXT
        decision.id = "decision-48"
        return decision

    def test_retry_arms_releases_parks_on_live_loops(self):
        from concurrent_executor import ARMS_PARKED_RETRY_OPTION
        from routes.decisions import _maybe_dispatch_arms_parked_resolution

        loop = MagicMock()
        loop.slice_id = "slice-8"
        loop.reset_parked_arms.return_value = ["key-coder"]
        with patch("event_loop.get_live_event_loops", return_value=[loop]):
            result = _maybe_dispatch_arms_parked_resolution(
                "issue-3548", self._decision(), ARMS_PARKED_RETRY_OPTION
            )
        assert result["action"] == "reset_parked_arms"
        assert result["success"] is True
        assert result["cleared_by_slice"] == {"slice-8": ["key-coder"]}

    def test_retry_arms_without_live_loop_fails_informatively(self):
        from concurrent_executor import ARMS_PARKED_RETRY_OPTION
        from routes.decisions import _maybe_dispatch_arms_parked_resolution

        with patch("event_loop.get_live_event_loops", return_value=[]):
            result = _maybe_dispatch_arms_parked_resolution(
                "issue-3548", self._decision(), ARMS_PARKED_RETRY_OPTION
            )
        assert result["success"] is False
        assert "no live event loop" in result["error"]

    def test_non_parked_context_is_ignored(self):
        from concurrent_executor import ARMS_PARKED_RETRY_OPTION
        from routes.decisions import _maybe_dispatch_arms_parked_resolution

        decision = MagicMock()
        decision.context = "something_else"
        assert (
            _maybe_dispatch_arms_parked_resolution("issue-3548", decision, ARMS_PARKED_RETRY_OPTION)
            is None
        )

    def test_abort_is_recorded_only(self):
        from concurrent_executor import ARMS_EXHAUSTED_ABORT_OPTION
        from routes.decisions import _maybe_dispatch_arms_parked_resolution

        result = _maybe_dispatch_arms_parked_resolution(
            "issue-3548", self._decision(), ARMS_EXHAUSTED_ABORT_OPTION
        )
        assert result["action"] == "arms_parked_abort"
        assert "cancel_task" in result["note"]
