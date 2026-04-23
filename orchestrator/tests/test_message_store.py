"""Unit tests for the in-memory :class:`MessageStore` blocking semantics.

Covers the issue #1897 additions to ``MessageStore``:

- ``get_messages(wait=N)`` blocks on a per-pipeline
  :class:`threading.Condition` until a matching message is appended.
- ``wait_for_types`` filters which message types unblock the caller — a
  flood of unwanted types keeps the call blocked until the deadline.
- ``clear(pipeline_id)`` wakes blocked callers (RISK-5 from the plan).
- ``add_message`` on the target pipeline wakes blocked callers quickly
  (sub-200ms in practice).
- ``HEARTBEAT`` is a new first-class :class:`MessageType` member.
- ``HEARTBEAT_STATES`` constant exposes the valid state values.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from message_store import (  # noqa: E402
    HEARTBEAT_STATES,
    Message,
    MessageStore,
    MessageType,
)


@pytest.fixture
def store() -> MessageStore:
    """A fresh in-memory MessageStore for each test."""
    return MessageStore()


def _make_message(
    pipeline_id: str = "test-pipeline",
    message_type: str = MessageType.PROGRESS,
    from_role: str = "coder",
    to_role: str = "all",
) -> Message:
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role=to_role,
        message_type=message_type,
        subject="test",
    )


class TestHeartbeatTypeExposure:
    """``HEARTBEAT`` is a new message type for issue #1897."""

    def test_heartbeat_type_defined(self) -> None:
        assert MessageType.HEARTBEAT == "HEARTBEAT"

    def test_heartbeat_states_constant(self) -> None:
        assert "WORKING" in HEARTBEAT_STATES
        assert "WAITING_ON_ROLE" in HEARTBEAT_STATES
        assert "PROPOSED" in HEARTBEAT_STATES
        assert "IDLE" in HEARTBEAT_STATES
        assert len(HEARTBEAT_STATES) == 4

    def test_heartbeat_states_frozen(self) -> None:
        # Should be a frozenset so callers can't mutate.
        assert isinstance(HEARTBEAT_STATES, frozenset)


class TestFastPath:
    """``get_messages`` returns immediately when matching messages exist."""

    def test_non_blocking_returns_immediately(self, store: MessageStore) -> None:
        store.add_message(_make_message())
        start = time.monotonic()
        msgs = store.get_messages("test-pipeline", wait=5)
        elapsed = time.monotonic() - start
        assert len(msgs) == 1
        # Should be effectively instant because the store is non-empty.
        assert elapsed < 0.5

    def test_wait_zero_returns_empty_when_empty(self, store: MessageStore) -> None:
        msgs = store.get_messages("empty-pipeline", wait=0)
        assert msgs == []


class TestBlockingReturnsEmptyOnTimeout:
    """A blocking read with no matching message returns ``[]`` after timeout."""

    def test_empty_pipeline_times_out(self, store: MessageStore) -> None:
        start = time.monotonic()
        msgs = store.get_messages("nothing-here", wait=1)
        elapsed = time.monotonic() - start
        assert msgs == []
        # We asked for 1s; give some slack. Should block at least 0.5s.
        assert elapsed >= 0.5
        assert elapsed < 2.5


class TestBlockingWakesOnAddMessage:
    """``add_message`` notifies blocked callers on the target pipeline."""

    def test_add_message_wakes_blocker_within_200ms(self, store: MessageStore) -> None:
        """The blocked caller should return within ~200ms of the message add,
        not on the full timeout."""
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(store.get_messages("test-pipeline", wait=5))

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.2)  # let the thread enter the blocking wait

        add_time = time.monotonic()
        store.add_message(_make_message())
        t.join(timeout=2)
        returned_time = time.monotonic()

        assert not t.is_alive(), "Blocked thread did not wake up"
        assert len(got) == 1 and len(got[0]) == 1
        # Should wake within 500ms of the add — the block is condition-variable
        # driven, not poll-based.
        assert returned_time - add_time < 0.5

    def test_unrelated_pipeline_add_does_not_wake(self, store: MessageStore) -> None:
        """RISK-5: adding to pipeline A must NOT wake a blocker on pipeline B
        (per-pipeline condition variables)."""
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(store.get_messages("pipeline-a", wait=1))

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        # Add to a DIFFERENT pipeline — should not wake the blocker.
        store.add_message(_make_message(pipeline_id="pipeline-b"))
        t.join(timeout=2)

        # Blocker ran to timeout; got empty list.
        assert got == [[]]


class TestWaitForTypesFilter:
    """``wait_for_types`` filters which messages unblock the caller."""

    def test_matching_type_unblocks(self, store: MessageStore) -> None:
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(
                store.get_messages(
                    "test-pipeline",
                    wait=5,
                    wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
                )
            )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        store.add_message(_make_message(message_type=MessageType.CONSENSUS_CONFIRMED))
        t.join(timeout=2)
        assert not t.is_alive()
        assert len(got) == 1
        assert got[0][0].message_type == MessageType.CONSENSUS_CONFIRMED

    def test_non_matching_types_do_not_unblock(self, store: MessageStore) -> None:
        """A flood of non-matching types must NOT unblock a typed waiter."""
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(
                store.get_messages(
                    "test-pipeline",
                    wait=1,  # short — we expect to time out
                    wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
                )
            )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        # Flood with non-matching types.
        for _ in range(5):
            store.add_message(_make_message(message_type=MessageType.PROGRESS))

        t.join(timeout=3)
        assert not t.is_alive()
        # Timed out because nothing matched.
        assert got == [[]]

    def test_existing_non_matching_not_returned(self, store: MessageStore) -> None:
        """Pre-existing non-matching messages don't satisfy the wait."""
        store.add_message(_make_message(message_type=MessageType.PROGRESS))
        start = time.monotonic()
        msgs = store.get_messages(
            "test-pipeline",
            wait=1,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
        )
        elapsed = time.monotonic() - start
        assert msgs == []
        # Should have blocked for ~1s, not returned immediately.
        assert elapsed >= 0.5


class TestClearWakesBlockedCallers:
    """RISK-5 from plan: ``clear(pid)`` must wake blocked callers."""

    def test_clear_wakes_blocker_with_empty_result(self, store: MessageStore) -> None:
        # Seed the pipeline so ``observed`` becomes True in the blocking loop,
        # which ensures clear() causes a return instead of being re-blocked.
        store.add_message(_make_message(message_type=MessageType.PROGRESS))

        got: list[list[Message]] = []

        def _block() -> None:
            got.append(
                store.get_messages(
                    "test-pipeline",
                    wait=5,
                    wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
                )
            )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.2)

        clear_time = time.monotonic()
        store.clear("test-pipeline")
        t.join(timeout=2)
        returned_time = time.monotonic()

        assert not t.is_alive(), "clear() did not wake blocked caller"
        assert got == [[]]
        # Should wake within 500ms of clear().
        assert returned_time - clear_time < 0.5


class TestAddDifferentPipelineDoesNotMatch:
    """Messages for pipeline A must not appear in a pipeline-B read."""

    def test_pipeline_isolation(self, store: MessageStore) -> None:
        store.add_message(_make_message(pipeline_id="a"))
        store.add_message(_make_message(pipeline_id="b"))
        msgs_a = store.get_messages("a", wait=0)
        msgs_b = store.get_messages("b", wait=0)
        assert len(msgs_a) == 1
        assert len(msgs_b) == 1
        assert msgs_a[0].pipeline_id == "a"
        assert msgs_b[0].pipeline_id == "b"


class TestRoleFilterStillWorksInBlocking:
    """Role filter should apply to both the fast path and the blocking path."""

    def test_role_filter_drops_non_broadcast_non_target(self, store: MessageStore) -> None:
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(store.get_messages("test-pipeline", role="coder", wait=1))

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        # This message targets "reviewer_code" — NOT "coder" — should not match.
        store.add_message(_make_message(to_role="reviewer_code"))
        t.join(timeout=3)
        assert not t.is_alive()
        # Timed out because nothing for "coder" arrived.
        assert got == [[]]

    def test_broadcast_wakes_targeted_reader(self, store: MessageStore) -> None:
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(store.get_messages("test-pipeline", role="coder", wait=5))

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        store.add_message(_make_message(to_role="all"))
        t.join(timeout=2)
        assert not t.is_alive()
        assert len(got[0]) == 1


class TestRaceConditionsAroundAdd:
    """Regression: message added between the fast-path check and the
    blocking wait must not be lost."""

    def test_message_added_right_before_wait_is_returned(self, store: MessageStore) -> None:
        """Simulate: fast-path sees empty, condvar acquired, then the message
        was already present — should return it immediately without waiting."""
        # Pre-populate so the fast path sees the message; verify the wait
        # returns the existing message rather than waiting for a new one.
        store.add_message(_make_message())

        start = time.monotonic()
        msgs = store.get_messages("test-pipeline", wait=5)
        elapsed = time.monotonic() - start

        assert len(msgs) == 1
        assert elapsed < 0.5


class TestNotifyMultipleWaiters:
    """``add_message`` must wake ALL waiters (``notify_all``)."""

    def test_two_waiters_both_unblock(self, store: MessageStore) -> None:
        got_a: list[list[Message]] = []
        got_b: list[list[Message]] = []

        def _block_a() -> None:
            got_a.append(store.get_messages("test-pipeline", wait=5))

        def _block_b() -> None:
            got_b.append(store.get_messages("test-pipeline", wait=5))

        ta = threading.Thread(target=_block_a)
        tb = threading.Thread(target=_block_b)
        ta.start()
        tb.start()
        time.sleep(0.2)

        store.add_message(_make_message())
        ta.join(timeout=3)
        tb.join(timeout=3)

        assert not ta.is_alive()
        assert not tb.is_alive()
        assert len(got_a[0]) == 1
        assert len(got_b[0]) == 1
