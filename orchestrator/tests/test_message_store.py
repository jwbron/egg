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
    GetMessagesMeta,
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
        assert "WAITING_FOR_EVENT" in HEARTBEAT_STATES
        assert "PROPOSED" in HEARTBEAT_STATES
        assert "IDLE" in HEARTBEAT_STATES
        assert len(HEARTBEAT_STATES) == 5

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


class TestClearRemovesConditionVariable:
    """Plan non-blocking (reviewer_code NACK): ``clear()`` MUST pop the
    per-pipeline condition variable in addition to the message list.

    Without this, long-lived orchestrators accumulate stale ``_cond``
    entries indefinitely (each distinct pipeline_id leaks one Condition
    + one underlying lock reference) — a slow memory leak that would
    eventually matter on a fleet handling thousands of pipelines.
    """

    def test_clear_pops_condition_variable(self, store: MessageStore) -> None:
        """After clear(), the _cond dict should not have the pipeline_id
        key — a fresh blocking read will lazily re-create it."""
        # Seed a blocking read so the cv gets created.
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(store.get_messages("pipe-cv-test", wait=5))

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.2)
        # Confirm cv was created (whitebox assertion — the test is on
        # memory-leak prevention, which is inherently a whitebox concern).
        assert "pipe-cv-test" in store._cond

        store.clear("pipe-cv-test")
        t.join(timeout=2)

        # cv should be popped after clear() (RISK-5 memory-leak fix).
        assert "pipe-cv-test" not in store._cond, (
            "clear() did not pop _cond[pipe-cv-test]; long-lived orchestrators "
            "will accumulate stale condition variables"
        )

    def test_fresh_wait_after_clear_lazily_recreates_cv(self, store: MessageStore) -> None:
        """A new blocking read AFTER clear() must lazily re-create the
        cv without any stale-state side effects.

        This is the invariant that makes cv-cleanup safe: we don't break
        the next wait because the wait handler creates one on demand.
        """
        store.add_message(_make_message(pipeline_id="pipe-recreate"))
        store.clear("pipe-recreate")
        # cv must be gone.
        assert "pipe-recreate" not in store._cond

        # A fresh wait must succeed (times out cleanly — no stale signal).
        start = time.monotonic()
        msgs = store.get_messages("pipe-recreate", wait=1)
        elapsed = time.monotonic() - start

        assert msgs == []
        assert 0.8 <= elapsed <= 2.0, (
            f"Fresh wait after clear took {elapsed:.2f}s; expected ~1s (normal timeout path)"
        )


class TestFromTipSemantics:
    """``from_tip=True`` snaps the starting cursor to the current length
    so only messages added after the call unblock the wait.

    Backs the ``/messages/wait`` endpoint fix for issue #1925: without
    from_tip, repeated cursor-less wait-loop invocations re-matched the
    same already-seen event and returned instantly instead of blocking.
    """

    def test_pre_existing_matching_message_does_not_match(self, store: MessageStore) -> None:
        """A matching message added BEFORE the wait must be ignored."""
        store.add_message(_make_message(message_type=MessageType.CONSENSUS_CONFIRMED))

        start = time.monotonic()
        msgs = store.get_messages(
            "test-pipeline",
            wait=1,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
            from_tip=True,
        )
        elapsed = time.monotonic() - start

        assert msgs == []
        # Must have actually blocked — ~1s not ~0s.
        assert elapsed >= 0.5, (
            f"from_tip=True returned in {elapsed:.2f}s; expected to block ~1s "
            "because no NEW matching message arrived"
        )

    def test_post_call_message_unblocks_from_tip_wait(self, store: MessageStore) -> None:
        """A matching message added AFTER the wait begins unblocks it."""
        # Seed a pre-existing matching message that from_tip must skip.
        store.add_message(_make_message(message_type=MessageType.CONSENSUS_CONFIRMED))

        got: list[list[Message]] = []

        def _block() -> None:
            got.append(
                store.get_messages(
                    "test-pipeline",
                    wait=5,
                    wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
                    from_tip=True,
                )
            )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.2)  # let the thread enter the blocking wait

        store.add_message(
            _make_message(message_type=MessageType.CONSENSUS_CONFIRMED, from_role="later")
        )
        t.join(timeout=2)

        assert not t.is_alive()
        assert len(got) == 1 and len(got[0]) == 1
        # Returned the NEW message (from_role='later'), not the pre-seeded one.
        assert got[0][0].from_role == "later"

    def test_from_tip_ignored_when_wait_zero(self, store: MessageStore) -> None:
        """With ``wait=0`` there's nothing to block on; from_tip is a no-op
        (prevents a footgun where from_tip + non-blocking would silently
        return empty regardless of stream state)."""
        store.add_message(_make_message(message_type=MessageType.CONSENSUS_CONFIRMED))

        msgs = store.get_messages(
            "test-pipeline",
            wait=0,
            from_tip=True,
        )
        # No wait_for_types + wait=0 + from_tip: same as plain non-blocking read.
        assert len(msgs) == 1

    def test_explicit_since_id_disables_from_tip(self, store: MessageStore) -> None:
        """When both are set, since_id wins and from_tip is ignored."""
        anchor = store.add_message(_make_message(message_type=MessageType.PROGRESS))
        store.add_message(_make_message(message_type=MessageType.CONSENSUS_CONFIRMED))

        msgs = store.get_messages(
            "test-pipeline",
            wait=1,
            wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
            since_id=anchor.id,
            from_tip=True,
        )
        assert len(msgs) == 1
        assert msgs[0].message_type == MessageType.CONSENSUS_CONFIRMED


class TestStaleSinceIdInBlockingWait:
    """Regression coverage for issue #2454.

    A stale ``since_id`` (e.g., a cursor that survived a phase-boundary
    ``clear()``) used to log ``since_id not found in store`` from inside
    ``_filter`` — which the blocking branch invokes once per ``notify``,
    producing one log line per concurrent message arrival during a long
    poll. With multiple consumers polling and heartbeats fanning in, the
    same warning could be emitted dozens of times for a single 25 s
    wait. Resolution moved into the fast-path lock, so the warning is
    bound to one emission per call regardless of the number of notifies
    that fire while the caller is blocked.
    """

    def test_stale_cursor_logs_warning_once_per_call(
        self,
        store: MessageStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A stale ``since_id`` that survives a flood of intervening
        notifies must produce exactly one warning, not one per notify."""
        # Seed and clear the store — anchor.id is now a "valid-looking"
        # cursor that the store no longer indexes.
        anchor = store.add_message(_make_message(message_type=MessageType.PROGRESS))
        store.clear("test-pipeline")

        # Re-seed so the blocking branch's ``observed`` flag stays True
        # after the clear (otherwise the cv-detach branch returns early
        # before any flood of notifies can run through ``_filter``).
        store.add_message(_make_message(message_type=MessageType.PROGRESS))

        got: list[list[Message]] = []

        def _block() -> None:
            with caplog.at_level("WARNING", logger="orchestrator.message_store"):
                got.append(
                    store.get_messages(
                        "test-pipeline",
                        wait=2,
                        wait_for_types=[MessageType.CONSENSUS_CONFIRMED],
                        since_id=anchor.id,
                    )
                )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)  # let the thread enter the blocking wait

        # Flood with non-matching adds — every add fires notify_all and
        # the pre-fix code would log the warning on each wake-up.
        for _ in range(8):
            store.add_message(_make_message(message_type=MessageType.PROGRESS))
            time.sleep(0.01)

        t.join(timeout=4)
        assert not t.is_alive()

        warnings = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "since_id not found" in r.message
        ]
        assert len(warnings) == 1, (
            f"expected exactly one stale-cursor warning across the wait, got {len(warnings)}"
        )
        # Pin the wait-for-types contract too: none of the eight intervening
        # adds were CONSENSUS_CONFIRMED, so the wait must time out empty.
        # Catches a future regression where wait_for_types filtering breaks
        # while the warning suppression still works.
        assert got == [[]], (
            f"expected empty result on timeout (no CONSENSUS_CONFIRMED arrived), got {got}"
        )

    def test_stale_cursor_still_returns_full_history(self, store: MessageStore) -> None:
        """The contract from
        ``test_stale_since_id_returns_all_messages`` (HTTP layer) still
        holds at the store level: an unknown ``since_id`` falls back to
        full-history replay rather than empty, so a directed message
        sent before the stale cursor is still delivered."""
        store.add_message(_make_message(message_type=MessageType.PROGRESS))
        store.add_message(_make_message(message_type=MessageType.CONSENSUS_CONFIRMED))

        msgs = store.get_messages(
            "test-pipeline",
            since_id="nonexistent-cursor-xyz",
        )
        assert len(msgs) == 2


class TestGetMessagesWithMeta:
    """``get_messages_with_meta`` (issue #2464) returns a structured
    staleness signal alongside the message list so consumers can drop
    cached cursors instead of re-passing dead values forever.
    """

    def test_meta_signals_stale_cursor(self, store: MessageStore) -> None:
        """An unknown ``since_id`` produces ``since_id_stale=True`` and
        the message list still falls back to full history (existing
        contract)."""
        store.add_message(_make_message(message_type=MessageType.PROGRESS))

        msgs, meta = store.get_messages_with_meta(
            "test-pipeline",
            since_id="cursor-the-store-never-saw",
        )
        assert isinstance(meta, GetMessagesMeta)
        assert meta.since_id_stale is True
        # Full-history fallback still in effect — the bug-fix preserves
        # delivery, only changes whether we *advertise* the staleness.
        assert len(msgs) == 1

    def test_meta_clean_when_cursor_known(self, store: MessageStore) -> None:
        """A resolvable ``since_id`` produces ``since_id_stale=False``
        and only newer messages come back."""
        anchor = store.add_message(_make_message(message_type=MessageType.PROGRESS))
        store.add_message(_make_message(message_type=MessageType.CONSENSUS_CONFIRMED))

        msgs, meta = store.get_messages_with_meta(
            "test-pipeline",
            since_id=anchor.id,
        )
        assert meta.since_id_stale is False
        assert len(msgs) == 1
        assert msgs[0].message_type == MessageType.CONSENSUS_CONFIRMED

    def test_meta_clean_when_no_since_id(self, store: MessageStore) -> None:
        """No ``since_id`` passed → never stale (there is no cursor to
        resolve)."""
        store.add_message(_make_message(message_type=MessageType.PROGRESS))

        _msgs, meta = store.get_messages_with_meta("test-pipeline")
        assert meta.since_id_stale is False

    def test_meta_signals_stale_after_phase_clear(self, store: MessageStore) -> None:
        """Pin the original-bug shape: a cursor that resolved fine
        before a ``clear()`` is reported stale on the next call. This
        is the exact post-phase-boundary path #2464 unblocks."""
        anchor = store.add_message(_make_message(message_type=MessageType.PROGRESS))
        # Cursor is currently fine.
        _, meta_before = store.get_messages_with_meta("test-pipeline", since_id=anchor.id)
        assert meta_before.since_id_stale is False

        store.clear("test-pipeline")
        store.add_message(_make_message(message_type=MessageType.PROGRESS))

        _, meta_after = store.get_messages_with_meta("test-pipeline", since_id=anchor.id)
        assert meta_after.since_id_stale is True

    def test_get_messages_drops_meta_for_legacy_callers(self, store: MessageStore) -> None:
        """``get_messages`` is now a thin wrapper around the meta variant
        so existing callers see no signature change."""
        store.add_message(_make_message(message_type=MessageType.PROGRESS))
        result = store.get_messages("test-pipeline", since_id="bogus")
        # Just a list of messages, no tuple.
        assert isinstance(result, list)
        assert len(result) == 1

    def test_suppress_stale_warning_silences_log_but_keeps_meta(
        self, store: MessageStore, caplog
    ) -> None:
        """``_suppress_stale_warning=True`` (used by ``/status/wait``'s
        synchronous probe) silences the ``since_id not found in store``
        log line on a stale cursor but still populates the structured
        ``meta.since_id_stale`` flag — reviewer note #2 on PR #2485."""
        import logging

        store.add_message(_make_message(message_type=MessageType.PROGRESS))

        # Probe-shape call: limit=1, wait=0, with the suppression flag.
        with caplog.at_level(logging.WARNING, logger="orchestrator.message_store"):
            _msgs, meta = store.get_messages_with_meta(
                "test-pipeline",
                since_id="cursor-the-store-never-saw",
                limit=1,
                wait=0,
                _suppress_stale_warning=True,
            )

        # Structured signal still set — consumers can still detect.
        assert meta.since_id_stale is True
        # Warning log line is silenced.
        assert not any("since_id not found in store" in rec.getMessage() for rec in caplog.records)

    def test_default_emits_stale_warning(self, store: MessageStore, caplog) -> None:
        """Pin the regression boundary: without the suppression flag
        the warning still fires (the daemon's later call relies on this
        to preserve pre-PR cadence of one warning per request)."""
        import logging

        store.add_message(_make_message(message_type=MessageType.PROGRESS))

        with caplog.at_level(logging.WARNING, logger="orchestrator.message_store"):
            _msgs, meta = store.get_messages_with_meta(
                "test-pipeline",
                since_id="cursor-the-store-never-saw",
            )

        assert meta.since_id_stale is True
        assert any("since_id not found in store" in rec.getMessage() for rec in caplog.records)


class TestGetLatestId:
    """Tests for ``MessageStore.get_latest_id``."""

    def test_empty_pipeline_returns_none(self, store: MessageStore) -> None:
        assert store.get_latest_id("nonexistent-pipeline") is None

    def test_single_message(self, store: MessageStore) -> None:
        msg = _make_message(pipeline_id="p1")
        store.add_message(msg)
        assert store.get_latest_id("p1") == msg.id

    def test_returns_most_recent(self, store: MessageStore) -> None:
        m1 = _make_message(pipeline_id="p1")
        m2 = _make_message(pipeline_id="p1")
        store.add_message(m1)
        store.add_message(m2)
        assert store.get_latest_id("p1") == m2.id

    def test_pipeline_isolation(self, store: MessageStore) -> None:
        m1 = _make_message(pipeline_id="p1")
        m2 = _make_message(pipeline_id="p2")
        store.add_message(m1)
        store.add_message(m2)
        assert store.get_latest_id("p1") == m1.id
        assert store.get_latest_id("p2") == m2.id

    def test_concurrent_add_during_read(self, store: MessageStore) -> None:
        """get_latest_id returns a consistent result even when messages
        are appended concurrently from another thread."""
        msg = _make_message(pipeline_id="p1")
        store.add_message(msg)

        ids: list[str | None] = []

        def reader() -> None:
            for _ in range(50):
                ids.append(store.get_latest_id("p1"))

        def writer() -> None:
            for _ in range(50):
                store.add_message(_make_message(pipeline_id="p1"))

        t_read = threading.Thread(target=reader)
        t_write = threading.Thread(target=writer)
        t_read.start()
        t_write.start()
        t_read.join()
        t_write.join()

        # Every read must have returned a valid id (never None after the
        # initial message was added).
        assert all(i is not None for i in ids)


def _make_message_with_slice(
    pipeline_id: str = "p1",
    message_type: str = MessageType.CONSENSUS_PROPOSE,
    from_role: str = "coder",
    to_role: str = "all",
    slice_id: str | None = None,
) -> Message:
    """Build a CONSENSUS-shaped Message with optional slice metadata.

    Used by the #2725 filter tests where ``metadata.slice_id`` drives the
    new slice-scoped wait filter.
    """
    metadata: dict[str, object] = {}
    if slice_id is not None:
        metadata["slice_id"] = slice_id
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role=to_role,
        message_type=message_type,
        subject="test",
        metadata=metadata,
    )


class TestSliceFilter:
    """``slice_id`` filter: only match the requested slice OR null (#2725).

    Null on the message is a pipeline-level passthrough so OVERSEER_ALERT
    and global phase signals continue to wake slice-scoped waiters.
    """

    def test_fast_path_drops_wrong_slice(self, store: MessageStore) -> None:
        store.add_message(_make_message_with_slice(slice_id="slice-2"))
        msgs = store.get_messages("p1", slice_id="slice-1", wait=0)
        assert msgs == []

    def test_fast_path_keeps_matching_slice(self, store: MessageStore) -> None:
        store.add_message(_make_message_with_slice(slice_id="slice-1"))
        msgs = store.get_messages("p1", slice_id="slice-1", wait=0)
        assert len(msgs) == 1

    def test_fast_path_keeps_null_slice_message(self, store: MessageStore) -> None:
        """A pipeline-level message (null slice_id) passes the slice filter.

        OVERSEER_ALERT is the canonical example — it has no slice scope and
        must wake every slice-filtered waiter.
        """
        store.add_message(
            _make_message_with_slice(
                message_type=MessageType.OVERSEER_ALERT,
                from_role="overseer",
                slice_id=None,
            )
        )
        msgs = store.get_messages("p1", slice_id="slice-1", wait=0)
        assert len(msgs) == 1
        assert msgs[0].message_type == MessageType.OVERSEER_ALERT

    def test_wrong_slice_does_not_unblock(self, store: MessageStore) -> None:
        """Wrong-slice messages must not unblock a slice-filtered wait —
        otherwise the filter would just delay the wake-storm by one round-trip."""
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(
                store.get_messages(
                    "p1",
                    slice_id="slice-1",
                    wait=1,
                    wait_for_types=[MessageType.CONSENSUS_PROPOSE],
                )
            )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        # Flood with wrong-slice events. Each notifies the cv but must not
        # be returned to the slice-1 waiter.
        for _ in range(5):
            store.add_message(_make_message_with_slice(slice_id="slice-2"))

        t.join(timeout=3)
        assert not t.is_alive()
        assert got == [[]]

    def test_null_slice_unblocks_filtered_wait(self, store: MessageStore) -> None:
        """Null-slice (pipeline-level) message unblocks a slice-filtered wait."""
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(
                store.get_messages(
                    "p1",
                    slice_id="slice-1",
                    wait=2,
                    wait_for_types=[MessageType.OVERSEER_ALERT],
                )
            )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        store.add_message(
            _make_message_with_slice(
                message_type=MessageType.OVERSEER_ALERT,
                from_role="overseer",
                slice_id=None,
            )
        )

        t.join(timeout=3)
        assert not t.is_alive()
        assert len(got[0]) == 1


class TestFromRolesFilter:
    """``from_roles`` allowlist filter: only match named senders (#2725).

    ``from_role`` (singular) wins when both are supplied so legacy callers
    see no behavior change.
    """

    def test_fast_path_keeps_allowed_sender(self, store: MessageStore) -> None:
        store.add_message(_make_message_with_slice(from_role="coder"))
        msgs = store.get_messages("p1", from_roles=["coder", "tester"], wait=0)
        assert len(msgs) == 1

    def test_fast_path_drops_disallowed_sender(self, store: MessageStore) -> None:
        store.add_message(_make_message_with_slice(from_role="documenter"))
        msgs = store.get_messages("p1", from_roles=["coder", "tester"], wait=0)
        assert msgs == []

    def test_wrong_sender_does_not_unblock(self, store: MessageStore) -> None:
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(
                store.get_messages(
                    "p1",
                    from_roles=["coder", "tester"],
                    wait=1,
                    wait_for_types=[MessageType.CONSENSUS_PROPOSE],
                )
            )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        for _ in range(5):
            store.add_message(_make_message_with_slice(from_role="documenter"))

        t.join(timeout=3)
        assert not t.is_alive()
        assert got == [[]]

    def test_singular_from_role_wins_over_set(self, store: MessageStore) -> None:
        """When both are supplied, ``from_role`` (singular) wins.

        This preserves single-sender back-compat — a caller that already
        passes ``from_role="X"`` and adds ``from_roles=["X","Y"]`` for some
        reason gets exactly the X-only matches the singular form has
        always returned.
        """
        store.add_message(_make_message_with_slice(from_role="coder"))
        store.add_message(_make_message_with_slice(from_role="tester"))
        msgs = store.get_messages(
            "p1",
            from_role="coder",
            from_roles=["coder", "tester"],
            wait=0,
        )
        assert [m.from_role for m in msgs] == ["coder"]

    def test_empty_set_is_no_filter(self, store: MessageStore) -> None:
        """An empty ``from_roles`` set is treated as no filter at the
        store layer — the route layer rejects this at request time so an
        empty list never reaches the store from network callers."""
        store.add_message(_make_message_with_slice(from_role="documenter"))
        msgs = store.get_messages("p1", from_roles=[], wait=0)
        assert len(msgs) == 1


class TestSliceAndFromRolesCombined:
    """Filters compose — both must accept the message for it to match."""

    def test_both_match(self, store: MessageStore) -> None:
        store.add_message(_make_message_with_slice(from_role="coder", slice_id="slice-1"))
        msgs = store.get_messages(
            "p1",
            slice_id="slice-1",
            from_roles=["coder", "tester"],
            wait=0,
        )
        assert len(msgs) == 1

    def test_wrong_slice_right_sender_drops(self, store: MessageStore) -> None:
        store.add_message(_make_message_with_slice(from_role="coder", slice_id="slice-2"))
        msgs = store.get_messages(
            "p1",
            slice_id="slice-1",
            from_roles=["coder", "tester"],
            wait=0,
        )
        assert msgs == []

    def test_right_slice_wrong_sender_drops(self, store: MessageStore) -> None:
        store.add_message(_make_message_with_slice(from_role="documenter", slice_id="slice-1"))
        msgs = store.get_messages(
            "p1",
            slice_id="slice-1",
            from_roles=["coder", "tester"],
            wait=0,
        )
        assert msgs == []

    def test_overseer_alert_bypasses_slice_filter(self, store: MessageStore) -> None:
        """Combined filter still respects the null-slice passthrough so
        system senders included in the allowlist (overseer / orchestrator)
        keep waking slice-filtered waiters."""
        store.add_message(
            _make_message_with_slice(
                message_type=MessageType.OVERSEER_ALERT,
                from_role="overseer",
                slice_id=None,
            )
        )
        msgs = store.get_messages(
            "p1",
            slice_id="slice-1",
            from_roles=["coder", "tester", "overseer", "orchestrator"],
            wait=0,
        )
        assert len(msgs) == 1

    def test_orchestrator_re_review_wakes_tightly_filtered_reviewer(
        self, store: MessageStore
    ) -> None:
        """Negative-conformance pin (#2725): an orchestrator-emitted
        CONSENSUS_RE_REVIEW targeted at this reviewer must wake even a
        tight slice + producer-allowlist filter — otherwise the filter
        silently sleeps the reviewer through a legitimate cross-graph
        cascade, which is the failure mode worse than the wake-storm.

        Constructed to mirror the orchestrator's signal-handler shape
        (routes/signals.py:1373-1393): from_role="orchestrator", to_role
        targeted at the reviewer, metadata.slice_id matching the
        reviewer's slice. The spawner-built allowlist always includes
        ``orchestrator`` so this works without rubric edits.
        """
        got: list[list[Message]] = []

        def _block() -> None:
            got.append(
                store.get_messages(
                    "p1",
                    role="reviewer_code",
                    slice_id="slice-1",
                    from_roles=["coder", "tester", "overseer", "orchestrator"],
                    wait=2,
                    wait_for_types=[MessageType.CONSENSUS_RE_REVIEW],
                )
            )

        t = threading.Thread(target=_block)
        t.start()
        time.sleep(0.1)

        store.add_message(
            Message(
                pipeline_id="p1",
                from_role="orchestrator",
                to_role="reviewer_code",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="Re-review required: coder submitted new proposal v2",
                metadata={"slice_id": "slice-1", "producer_role": "coder"},
            )
        )

        t.join(timeout=3)
        assert not t.is_alive()
        assert len(got[0]) == 1
        assert got[0][0].from_role == "orchestrator"
        assert got[0][0].message_type == MessageType.CONSENSUS_RE_REVIEW
