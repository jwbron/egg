"""Executor reactions to a transient rate-limit outcome's transitions (#3364 PR C).

``ConcurrentPhaseExecutor._handle_rate_limited`` is wired as the
``JobSupervisor``'s ``rate_limited_notifier``. The supervisor already PACED the
respawn (the paced retry runs through the normal respawn gate, so completed
slices are never discarded — AC-C3); this handler owns the two operator-facing
reactions the supervisor deliberately does not take itself:

* ``threshold_crossed`` (cq-1 / AC-C5): emit an ``agent-rate-limited``
  OVERSEER_ALERT so an attended operator is informed WHILE auto-recovery
  continues — NO hard ceiling, and NO halt;
* ``deterministic_loop`` (AC-C4): emit a distinct ``rate-limit-deterministic-loop``
  alert AND halt the paced retry (mark the key exhausted) so the arms-exhausted
  HITL takes over instead of looping forever.

The halt targets only the looping arm's dedupe key, so a landed / sibling slice
is preserved (AC-C3).
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

import event_loop  # noqa: E402
from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus  # noqa: E402


class _ManualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class _Loop:
    """Minimal ``_event_loop`` holder exposing a real supervisor for the halt."""

    def __init__(self, supervisor) -> None:
        self.supervisor = supervisor


def _make_executor():
    from concurrent_executor import ConcurrentPhaseExecutor

    pipeline = Pipeline(
        id="issue-3364",
        repo="test/repo",
        issue_number=3364,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=PipelineConfig(),
    )
    return ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock(), slice_id="slice-2")


def _fingerprint():
    from supervision_policy import RateLimitFingerprint

    return RateLimitFingerprint(signature="rate_limited", progression="round-3")


def _call(executor, **overrides):
    kwargs = {
        "role": "coder",
        "action": "propose",
        "dedupe_key": "arm-1",
        "retry_count": 3,
        "cumulative_wait_seconds": 1800.0,
        "backoff_seconds": 900.0,
        "threshold_crossed": False,
        "deterministic_loop": False,
        "fingerprint": _fingerprint(),
    }
    kwargs.update(overrides)
    executor._handle_rate_limited(**kwargs)


class TestThresholdAlert:
    @patch("concurrent_executor.get_message_store")
    def test_threshold_crossed_emits_alert_and_does_not_halt(self, mock_store):
        from message_store import MessageType

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        executor = _make_executor()
        executor._event_loop = _Loop(supervisor)

        _call(executor, threshold_crossed=True, deterministic_loop=False)

        msg = mock_store.return_value.add_message.call_args[0][0]
        assert msg.message_type == MessageType.OVERSEER_ALERT
        assert msg.metadata["anomaly"] == "agent-rate-limited"
        # NOT a halt: the paced retry keeps going until the cap lifts.
        assert not supervisor.is_exhausted("arm-1")


class TestDeterministicLoopHalt:
    @patch("concurrent_executor.get_message_store")
    def test_deterministic_loop_emits_alert_and_halts_the_arm(self, mock_store):
        from message_store import MessageType

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        executor = _make_executor()
        executor._event_loop = _Loop(supervisor)

        _call(executor, threshold_crossed=False, deterministic_loop=True)

        msg = mock_store.return_value.add_message.call_args[0][0]
        assert msg.message_type == MessageType.OVERSEER_ALERT
        assert msg.metadata["anomaly"] == "rate-limit-deterministic-loop"
        # The looping arm is halted so the arms-exhausted HITL takes over.
        assert supervisor.is_exhausted("arm-1")

    @patch("concurrent_executor.get_message_store")
    def test_halt_preserves_sibling_slices(self, mock_store):
        # AC-C3: halting the deterministic-loop arm exhausts ONLY its dedupe
        # key — a landed / in-flight sibling slice's arm is untouched.
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        executor = _make_executor()
        executor._event_loop = _Loop(supervisor)

        _call(executor, dedupe_key="arm-1", deterministic_loop=True)

        assert supervisor.is_exhausted("arm-1")
        assert not supervisor.is_exhausted("arm-2-other-slice")


class TestNoTransition:
    @patch("concurrent_executor.get_message_store")
    def test_no_transition_neither_alerts_nor_halts(self, mock_store):
        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        executor = _make_executor()
        executor._event_loop = _Loop(supervisor)

        _call(executor, threshold_crossed=False, deterministic_loop=False)

        mock_store.return_value.add_message.assert_not_called()
        assert not supervisor.is_exhausted("arm-1")

    @patch("concurrent_executor.get_message_store")
    def test_both_transitions_emit_two_alerts_and_halt(self, mock_store):
        from message_store import MessageType

        supervisor = event_loop.JobSupervisor(clock=_ManualClock())
        executor = _make_executor()
        executor._event_loop = _Loop(supervisor)

        _call(executor, threshold_crossed=True, deterministic_loop=True)

        anomalies = [
            call.args[0].metadata["anomaly"]
            for call in mock_store.return_value.add_message.call_args_list
        ]
        assert "agent-rate-limited" in anomalies
        assert "rate-limit-deterministic-loop" in anomalies
        assert all(
            call.args[0].message_type == MessageType.OVERSEER_ALERT
            for call in mock_store.return_value.add_message.call_args_list
        )
        assert supervisor.is_exhausted("arm-1")
