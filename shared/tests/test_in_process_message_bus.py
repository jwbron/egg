"""Tests for ``InProcessMessageBus`` (#2623 slice-1 task-1-3, task-1-8).

Acceptance criteria covered:

* ``InProcessMessageBus`` conforms to the ``MessageBus`` Protocol.
* INV-3 (stale-version ACK / NACK rejection) is preserved when the bus
  is used as the transport behind a ``PeerConsensusTracker``.  Oracle:
  ``orchestrator/tests/test_brc_content_validation.py``.
* INV-5 (open-NACK barrier — multi-reviewer aggregation barrier from
  issue #2142) is preserved when the bus is used as the transport.
  Oracle: ``orchestrator/tests/test_brc_open_nacks_barrier.py``.

The bus is documented to "subclass or delegate to
``orchestrator/message_store.py:200 MessageStore``" per the plan, so
the invariants are not enforced by the bus itself — they're enforced
by the orchestrator state machine the bus carries messages for.  This
test therefore verifies that wiring a tracker over the in-process bus
still yields the documented rejection on stale verdicts and multi-NACK
re-proposes.
"""

from __future__ import annotations

import pytest

substrate_pkg = pytest.importorskip(
    "substrate",
    reason="orchestrator/substrate/ package not present yet (task-1-1 pending)",
)
claude_code_pkg = pytest.importorskip(
    "substrate.claude_code",
    reason=(
        "orchestrator/substrate/claude_code/ package not present yet "
        "(task-1-3 pending)"
    ),
)
message_bus_mod = pytest.importorskip(
    "substrate.claude_code.message_bus",
    reason="substrate.claude_code.message_bus module not present yet (task-1-3)",
)


# ---------------------------------------------------------------------------
# Protocol conformance + minimal round-trip
# ---------------------------------------------------------------------------


def test_in_process_bus_class_is_exported() -> None:
    """``InProcessMessageBus`` is reachable via ``substrate.claude_code``."""
    bus_cls = getattr(message_bus_mod, "InProcessMessageBus", None)
    assert bus_cls is not None, (
        "substrate.claude_code.message_bus.InProcessMessageBus missing — "
        "task-1-3 AC"
    )
    assert hasattr(bus_cls, "add_message"), (
        "InProcessMessageBus.add_message required by MessageBus protocol"
    )
    assert hasattr(bus_cls, "get_messages"), (
        "InProcessMessageBus.get_messages required by MessageBus protocol"
    )


def test_in_process_bus_add_get_messages_round_trip() -> None:
    """Adding then reading messages by pipeline_id returns the same payload."""
    bus_cls = getattr(message_bus_mod, "InProcessMessageBus")
    bus = bus_cls()
    pipeline_id = "pipeline-deadbeef"
    bus.add_message(
        pipeline_id,
        {
            "message_type": "STATUS",
            "from_role": "tester",
            "to_role": "all",
            "body": "hi",
        },
    )
    msgs = bus.get_messages(pipeline_id)
    assert msgs, "added message must be visible via get_messages"
    assert any(m.get("from_role") == "tester" for m in msgs)


def test_in_process_bus_isolates_pipelines() -> None:
    """Messages for one pipeline_id do not leak into another pipeline's view."""
    bus_cls = getattr(message_bus_mod, "InProcessMessageBus")
    bus = bus_cls()
    bus.add_message(
        "pipeline-aaaa",
        {"message_type": "STATUS", "from_role": "tester", "to_role": "all"},
    )
    other = bus.get_messages("pipeline-bbbb")
    assert other == [], (
        "Message bus must isolate messages by pipeline_id (no cross-talk)"
    )


# ---------------------------------------------------------------------------
# INV-3: stale-version ACK rejection survives the bus surface
# ---------------------------------------------------------------------------


def test_inv3_stale_ack_rejected_when_routed_through_bus() -> None:
    """A reviewer ACK at a stale proposal version is rejected (INV-3).

    Mirrors
    ``test_brc_open_nacks_barrier::TestStaleVersionRejection::
    test_ack_against_stale_version_raises`` but with the
    ``InProcessMessageBus`` as the transport, so the invariant is
    structurally guaranteed across the substrate.
    """
    # TODO(tester): wire a ``PeerConsensusTracker`` over
    # ``bundle.bus`` (or a ``ClaudeCodeMessageStore`` exposed by the
    # bus) and re-run the oracle scenario. Fill in once task-1-3 lands
    # so the tracker-bus integration shape is concrete.
    pytest.skip(
        "INV-3 verification via in-process bus pending coder commit on "
        "tracker<->bus wiring (task-1-3)"
    )


# ---------------------------------------------------------------------------
# INV-5: multi-reviewer open-NACK aggregation barrier survives the bus
# ---------------------------------------------------------------------------


def test_inv5_open_nack_barrier_preserved_when_routed_through_bus() -> None:
    """Re-propose with ≥2 unresolved NACKs is rejected ``open_nacks_blocked`` (INV-5).

    Mirrors
    ``test_brc_open_nacks_barrier::TestOpenNacksBarrier::
    test_multi_reviewer_nack_first_re_propose_blocked`` but with the
    ``InProcessMessageBus`` as the transport.
    """
    pytest.skip(
        "INV-5 verification via in-process bus pending coder commit on "
        "tracker<->bus wiring (task-1-3)"
    )
