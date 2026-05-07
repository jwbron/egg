"""Tests for handle_consensus_confirmed_signal idempotency.

The ``egg-orch consensus confirmed`` CLI is invoked by agents after they
ACK all peers, often inside a retry loop.  Before #1890, every invocation
wrote a fresh CONSENSUS_CONFIRMED message to the bus — a single agent
could flood the bus with dozens of duplicates.  The signal handler now
short-circuits duplicate writes when the role has already emitted a
CONFIRMED for the current phase.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from message_store import MessageType


@pytest.fixture
def app():
    a = Flask(__name__)
    return a


@pytest.fixture
def mock_pipeline():
    pip = MagicMock()
    pip.current_phase = MagicMock()
    pip.current_phase.value = "implement"
    pip.config = MagicMock()
    pip.config.repo = None
    return pip


def _fake_message(
    from_role: str,
    phase: str,
    *,
    pending_acks: bool = False,
    slice_id: str | None = None,
) -> MagicMock:
    m = MagicMock()
    m.from_role = from_role
    m.phase = phase
    m.message_type = str(MessageType.CONSENSUS_CONFIRMED)
    base = {"pending_acks": True} if pending_acks else {"consensus_reached": True}
    if slice_id is not None:
        base["slice_id"] = slice_id
    m.metadata = base
    return m


def test_tracker_path_is_idempotent_for_final_confirmed(app, mock_pipeline):
    """Second CONFIRMED from same role in same phase is not written."""
    mock_store = MagicMock()
    mock_store.load_pipeline.return_value = mock_pipeline

    tracker = MagicMock()
    tracker.handle_confirmed.return_value = {
        "status": "confirmed",
        "consensus_reached": True,
    }

    msg_store = MagicMock()
    msg_store.get_messages.return_value = [
        _fake_message("coder", "implement"),
    ]

    with (
        app.app_context(),
        patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
        patch("routes.signals._write_consensus_confirmed_marker"),
        patch("routes.signals.get_state_store", return_value=mock_store),
        patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        patch("message_store.get_message_store", return_value=msg_store),
    ):
        from routes.signals import handle_consensus_confirmed_signal

        response, status_code = handle_consensus_confirmed_signal(
            "issue-42", {"agent_role": "coder"}, Path("/tmp/repo")
        )

    assert status_code == 200
    data = json.loads(response.data)
    assert data["data"].get("idempotent") is True
    msg_store.add_message.assert_not_called()


def test_tracker_path_writes_first_confirmed(app, mock_pipeline):
    """First CONFIRMED from a role is still recorded."""
    mock_store = MagicMock()
    mock_store.load_pipeline.return_value = mock_pipeline

    tracker = MagicMock()
    tracker.handle_confirmed.return_value = {
        "status": "confirmed",
        "consensus_reached": True,
    }

    msg_store = MagicMock()
    msg_store.get_messages.return_value = []

    with (
        app.app_context(),
        patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
        patch("routes.signals._write_consensus_confirmed_marker"),
        patch("routes.signals.get_state_store", return_value=mock_store),
        patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        patch("message_store.get_message_store", return_value=msg_store),
    ):
        from routes.signals import handle_consensus_confirmed_signal

        response, status_code = handle_consensus_confirmed_signal(
            "issue-42", {"agent_role": "coder"}, Path("/tmp/repo")
        )

    assert status_code == 200
    msg_store.add_message.assert_called_once()


def test_slice_two_first_confirmed_not_idempotent_when_slice_one_already_confirmed(
    app, mock_pipeline
):
    """Regression for #2535 follow-up: slice-2's first CONFIRMED must not
    be marked ``idempotent`` by slice-1's CONFIRMED in the bus.

    Before the fix, ``_existing_confirmed_for_role`` scanned the bus
    without slice scope; any slice-1 ``coder`` CONFIRMED message would
    trip ``has_final=True`` for the slice-2 coder, suppressing both the
    new CONFIRMED message write and the consensus-confirmed marker
    (#1473). The slice-2 coder's auto-commit would then push unreviewed
    WIP. The new ``slice_id``-scoped check confines the lookup.
    """
    mock_store = MagicMock()
    mock_store.load_pipeline.return_value = mock_pipeline

    tracker = MagicMock()
    tracker.handle_confirmed.return_value = {
        "status": "confirmed",
        "consensus_reached": True,
    }

    msg_store = MagicMock()
    # Bus state: slice-1's coder already CONFIRMED. The slice-2 coder
    # call must NOT see this as a same-role prior CONFIRMED.
    msg_store.get_messages.return_value = [
        _fake_message("coder", "implement", slice_id="slice-1"),
    ]

    with (
        app.app_context(),
        patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
        patch("routes.signals._write_consensus_confirmed_marker") as marker_mock,
        patch("routes.signals.get_state_store", return_value=mock_store),
        patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        patch("message_store.get_message_store", return_value=msg_store),
    ):
        from routes.signals import handle_consensus_confirmed_signal

        response, status_code = handle_consensus_confirmed_signal(
            "issue-2535",
            {"agent_role": "coder", "slice_id": "slice-2"},
            Path("/tmp/repo"),
        )

    assert status_code == 200
    data = json.loads(response.data)
    # Must NOT report idempotent — this is slice-2's first CONFIRMED.
    assert data["data"].get("idempotent") is not True
    # New CONFIRMED message MUST be written, with slice_id metadata so the
    # next slice-2 call can dedupe correctly.
    msg_store.add_message.assert_called_once()
    written = msg_store.add_message.call_args[0][0]
    assert written.metadata.get("slice_id") == "slice-2"
    # The consensus-confirmed marker MUST be written so auto-commit
    # doesn't push unreviewed WIP from slice-2 (#1473).
    marker_mock.assert_called_once()


def test_slice_idempotency_within_same_slice(app, mock_pipeline):
    """Within a single slice, the second CONFIRMED is still deduped."""
    mock_store = MagicMock()
    mock_store.load_pipeline.return_value = mock_pipeline

    tracker = MagicMock()
    tracker.handle_confirmed.return_value = {
        "status": "confirmed",
        "consensus_reached": True,
    }

    msg_store = MagicMock()
    # Bus state: slice-2's coder already CONFIRMED.
    msg_store.get_messages.return_value = [
        _fake_message("coder", "implement", slice_id="slice-2"),
    ]

    with (
        app.app_context(),
        patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
        patch("routes.signals._write_consensus_confirmed_marker"),
        patch("routes.signals.get_state_store", return_value=mock_store),
        patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        patch("message_store.get_message_store", return_value=msg_store),
    ):
        from routes.signals import handle_consensus_confirmed_signal

        response, status_code = handle_consensus_confirmed_signal(
            "issue-2535",
            {"agent_role": "coder", "slice_id": "slice-2"},
            Path("/tmp/repo"),
        )

    assert status_code == 200
    data = json.loads(response.data)
    assert data["data"].get("idempotent") is True
    msg_store.add_message.assert_not_called()


def test_pipeline_scoped_call_ignores_slice_scoped_prior_confirms(app, mock_pipeline):
    """Pipeline-scoped (no slice_id) callers must not be deduped by
    slice-scoped CONFIRMED messages.

    A pipeline-scoped CONFIRMED and a slice-scoped CONFIRMED are
    semantically separate. The legacy non-slice fallback path must keep
    matching only its own kind so an old non-slice pipeline isn't
    accidentally short-circuited by a slice-mode CONFIRMED that landed
    in the bus from a sibling concern.
    """
    mock_store = MagicMock()
    mock_store.load_pipeline.return_value = mock_pipeline

    tracker = MagicMock()
    tracker.handle_confirmed.return_value = {
        "status": "confirmed",
        "consensus_reached": True,
    }

    msg_store = MagicMock()
    msg_store.get_messages.return_value = [
        _fake_message("coder", "implement", slice_id="slice-1"),
    ]

    with (
        app.app_context(),
        patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
        patch("routes.signals._write_consensus_confirmed_marker"),
        patch("routes.signals.get_state_store", return_value=mock_store),
        patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        patch("message_store.get_message_store", return_value=msg_store),
    ):
        from routes.signals import handle_consensus_confirmed_signal

        response, status_code = handle_consensus_confirmed_signal(
            "issue-2535", {"agent_role": "coder"}, Path("/tmp/repo")
        )

    assert status_code == 200
    data = json.loads(response.data)
    # Pipeline-scoped first CONFIRMED — not idempotent against slice-1.
    assert data["data"].get("idempotent") is not True
    msg_store.add_message.assert_called_once()
    written = msg_store.add_message.call_args[0][0]
    # Pipeline-scoped writes MUST NOT carry a slice_id tag.
    assert "slice_id" not in (written.metadata or {})


def test_pending_acks_path_is_idempotent(app, mock_pipeline):
    """Repeated pending_acks invocations don't write duplicates."""
    mock_store = MagicMock()
    mock_store.load_pipeline.return_value = mock_pipeline

    tracker = MagicMock()
    tracker.handle_confirmed.return_value = {
        "status": "pending_acks",
        "message": "waiting for re-ACKs",
    }

    msg_store = MagicMock()
    msg_store.get_messages.return_value = [
        _fake_message("architect", "plan", pending_acks=True),
    ]

    with (
        app.app_context(),
        patch("routes.signals._resolve_pipeline_phase", return_value="plan"),
        patch("routes.signals._write_consensus_confirmed_marker"),
        patch("routes.signals.get_state_store", return_value=mock_store),
        patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        patch("message_store.get_message_store", return_value=msg_store),
    ):
        from routes.signals import handle_consensus_confirmed_signal

        response, status_code = handle_consensus_confirmed_signal(
            "issue-42", {"agent_role": "architect"}, Path("/tmp/repo")
        )

    assert status_code == 202
    msg_store.add_message.assert_not_called()


def test_pending_to_final_transition_still_writes(app, mock_pipeline):
    """Transition from pending_acks to final CONFIRMED still records a message."""
    mock_store = MagicMock()
    mock_store.load_pipeline.return_value = mock_pipeline

    tracker = MagicMock()
    tracker.handle_confirmed.return_value = {
        "status": "confirmed",
        "consensus_reached": True,
    }

    msg_store = MagicMock()
    # Only a pending_acks exists — the role hasn't emitted a final yet.
    msg_store.get_messages.return_value = [
        _fake_message("architect", "plan", pending_acks=True),
    ]

    with (
        app.app_context(),
        patch("routes.signals._resolve_pipeline_phase", return_value="plan"),
        patch("routes.signals._write_consensus_confirmed_marker"),
        patch("routes.signals.get_state_store", return_value=mock_store),
        patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        patch("message_store.get_message_store", return_value=msg_store),
    ):
        from routes.signals import handle_consensus_confirmed_signal

        response, status_code = handle_consensus_confirmed_signal(
            "issue-42", {"agent_role": "architect"}, Path("/tmp/repo")
        )

    assert status_code == 200
    msg_store.add_message.assert_called_once()
