"""Tests for the conditional-ACK path (issue #1998).

Conditional ACKs let a reviewer approve a proposal while attaching an
obligation that a human must perform at merge time — e.g. a ``git mv``
that agents cannot push through the gateway. These tests cover:

- Schema validation on ``ReviewPayload``
- Persistence + scoping on ``ApprovalMatrix``
- Carry-through via ``PeerConsensusTracker.handle_ack``
- Rendering of the Pre-merge Obligations section in the PR body
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from approval_matrix import ApprovalMatrix, ApprovalState
from attestation_schemas import ReviewPayload
from peer_consensus import (
    PeerConsensusTracker,
    _trackers,
    _trackers_lock,
    reconstruct_tracker_from_messages,
)
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

# --- Schema -----------------------------------------------------------------


class TestReviewPayloadCondition:
    def test_ack_accepts_condition(self):
        payload = ReviewPayload(
            verdict="ACK",
            artifact_references=["src/a.py"],
            pre_merge_condition="git mv legacy/x new/x before merge",
        )
        assert payload.pre_merge_condition.startswith("git mv")

    def test_ack_defaults_to_no_condition(self):
        payload = ReviewPayload(verdict="ACK", artifact_references=["src/a.py"])
        assert payload.pre_merge_condition == ""

    def test_nack_rejects_condition(self):
        # A conditional NACK is nonsensical — NACK already blocks the
        # producer, so there's nothing to defer.
        with pytest.raises(ValueError, match="only valid on ACK"):
            ReviewPayload(
                verdict="NACK",
                artifact_references=["src/a.py"],
                reason="broken",
                pre_merge_condition="do something",
            )

    def test_resolution_without_condition_rejected(self):
        """A resolution SHA on a plain ACK has nothing to attach to (#2336)."""
        with pytest.raises(ValueError, match="requires a non-empty"):
            ReviewPayload(
                verdict="ACK",
                artifact_references=["src/a.py"],
                pre_merge_condition_resolved_in_diff="abc1234",
            )

    def test_resolution_with_condition_accepted(self):
        """A resolution SHA alongside an obligation is the supported shape (#2336)."""
        payload = ReviewPayload(
            verdict="ACK",
            artifact_references=["src/a.py"],
            pre_merge_condition="verify migration in prod",
            pre_merge_condition_resolved_in_diff="abc1234",
        )
        assert payload.pre_merge_condition_resolved_in_diff == "abc1234"

    def test_resolution_rejects_non_hex_characters(self):
        """SHA shape validation prevents newline injection bending PR markdown (#2336)."""
        with pytest.raises(ValueError):
            ReviewPayload(
                verdict="ACK",
                artifact_references=["src/a.py"],
                pre_merge_condition="verify migration in prod",
                pre_merge_condition_resolved_in_diff="abc1234\n## Injected heading",
            )


# --- Matrix ----------------------------------------------------------------


@pytest.fixture
def matrix_graph():
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
        ]
    )


class TestApprovalMatrixCondition:
    def test_record_ack_stores_condition(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            artifact_refs=["src/a.py"],
            pre_merge_condition="human must git mv X Y",
        )
        entry = matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.pre_merge_condition == "human must git mv X Y"

    def test_whitespace_only_condition_normalized_to_empty(self, matrix_graph):
        """Whitespace-only conditions are stripped at the source (#1998 review)."""
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            artifact_refs=["src/a.py"],
            pre_merge_condition="   ",
        )
        entry = matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.pre_merge_condition == ""
        # Should not appear in active conditions either.
        assert matrix.get_pre_merge_conditions() == []

    def test_nack_clears_condition(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            artifact_refs=["src/a.py"],
            pre_merge_condition="obligation",
        )
        matrix.record_nack(
            "reviewer_code",
            "coder",
            version=1,
            reason="changed my mind",
            artifact_refs=["src/a.py"],
        )
        entry = matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.pre_merge_condition == ""

    def test_invalidate_ack_clears_condition(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            pre_merge_condition="obligation",
        )
        matrix.invalidate_ack("reviewer_code", "coder")
        entry = matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.state == ApprovalState.PENDING
        assert entry.pre_merge_condition == ""

    def test_get_pre_merge_conditions_returns_current(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            pre_merge_condition="A",
        )
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            # No condition on this ACK — should not appear in the list.
        )
        conditions = matrix.get_pre_merge_conditions()
        assert len(conditions) == 1
        assert conditions[0]["reviewer"] == "reviewer_code"
        assert conditions[0]["producer"] == "coder"
        assert conditions[0]["condition"] == "A"
        assert conditions[0]["version"] == 1

    def test_get_pre_merge_conditions_skips_stale(self, matrix_graph):
        """Condition recorded against version 1 vanishes after re-propose."""
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")  # v1
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            pre_merge_condition="A",
        )
        matrix.record_proposal("coder")  # v2 — supersedes v1
        # The ACK on v1 is now stale — the reviewer has not re-asserted
        # the obligation on v2, so we must not render it as an active
        # obligation.
        assert matrix.get_pre_merge_conditions() == []

    def test_round_trip_persistence(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_code",
            "coder",
            version=1,
            pre_merge_condition="mv thing",
        )
        data = matrix.to_dict()
        restored = ApprovalMatrix.from_dict(data, matrix_graph)
        entry = restored.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.pre_merge_condition == "mv thing"


# --- Tracker ---------------------------------------------------------------


class TestTrackerCondition:
    def test_handle_ack_persists_condition(self, matrix_graph):
        tracker = PeerConsensusTracker("test-pid", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")

        tracker.handle_propose(
            "coder",
            {
                "summary": "impl",
                "artifacts": ["src/a.py"],
                "commit_sha": "abc123",
            },
        )
        result = tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": "git mv X Y before merge",
            },
        )
        assert result["status"] == "acked"
        assert result["pre_merge_condition"] == "git mv X Y before merge"

        conditions = tracker.get_pre_merge_conditions()
        assert len(conditions) == 1
        assert conditions[0]["condition"] == "git mv X Y before merge"

    def test_handle_ack_whitespace_condition_excluded_from_event_and_result(self, matrix_graph):
        """Whitespace-only condition should not appear in the return value or
        event data — it must be consistent with the matrix normalization."""
        tracker = PeerConsensusTracker("test-pid", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")

        tracker.handle_propose(
            "coder",
            {
                "summary": "impl",
                "artifacts": ["src/a.py"],
                "commit_sha": "abc123",
            },
        )
        result = tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": "   ",
            },
        )
        # Whitespace-only condition should be treated as no condition.
        assert "pre_merge_condition" not in result
        assert tracker.get_pre_merge_conditions() == []

    def test_handle_ack_without_condition_is_unchanged(self, matrix_graph):
        tracker = PeerConsensusTracker("test-pid", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        result = tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/a.py"]},
        )
        assert "pre_merge_condition" not in result
        assert tracker.get_pre_merge_conditions() == []


# --- evaluate() surfaces conditions ----------------------------------------


class TestEvaluateSurfacesConditions:
    """``tracker.evaluate()`` must include ``pre_merge_conditions`` so the
    status endpoint (and CLI renderer) can show them while the pipeline is
    still live (#2006)."""

    def test_evaluate_includes_conditions_when_present(self, matrix_graph):
        tracker = PeerConsensusTracker("pid-eval-1", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": "git mv legacy/x new/x",
            },
        )
        state = tracker.evaluate()
        assert "pre_merge_conditions" in state
        conds = state["pre_merge_conditions"]
        assert len(conds) == 1
        assert conds[0]["reviewer"] == "reviewer_code"
        assert conds[0]["producer"] == "coder"
        assert conds[0]["condition"] == "git mv legacy/x new/x"

    def test_evaluate_empty_list_when_no_conditions(self, matrix_graph):
        tracker = PeerConsensusTracker("pid-eval-2", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/a.py"]},
        )
        state = tracker.evaluate()
        assert state["pre_merge_conditions"] == []


# --- PR body rendering -----------------------------------------------------


class TestPrBodyRendering:
    def test_section_rendered_when_conditions_exist(self, matrix_graph):
        tracker = PeerConsensusTracker("pipeline-X", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": "git mv legacy/x new/x",
            },
        )

        # Patch the tracker lookup to return our in-memory tracker, since
        # the module-level registry uses its own dict that may be empty
        # in some test environments.
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            from routes import pipelines as p

            section = p._build_pre_merge_obligations_section("pipeline-X")

        assert "Pre-merge Obligations" in section
        assert "reviewer_code" in section
        assert "git mv legacy/x new/x" in section

    def test_section_empty_when_no_tracker(self):
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=None,
        ):
            from routes import pipelines as p

            assert p._build_pre_merge_obligations_section("missing-pipeline") == ""

    def test_section_empty_when_no_conditions(self, matrix_graph):
        tracker = PeerConsensusTracker("pipeline-Y", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/a.py"]},
        )
        with patch(
            "peer_consensus.get_peer_consensus_tracker",
            return_value=tracker,
        ):
            from routes import pipelines as p

            assert p._build_pre_merge_obligations_section("pipeline-Y") == ""


# --- Shared helpers for integration tests ---------------------------------


def _make_two_reviewer_graph():
    """Graph with two CRITICAL reviewers → one coder, shared by integration tests."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
        ]
    )


# --- Signal-path integration ----------------------------------------------


_SIGNAL_PIPELINE_ID = "pipeline-signal-integration"


class TestSignalPathIntegration:
    """End-to-end: signal → tracker → matrix → PR body.

    Pins the chain that `ReviewPayload`, `PeerConsensusTracker`,
    `ApprovalMatrix`, and the PR-body renderer are individually covered by
    unit tests, but that nothing previously asserted runs end-to-end
    through `handle_consensus_ack_signal`. A silent breakage anywhere in
    between (e.g. the signal handler dropping `pre_merge_condition` from
    the payload) would not be caught by the existing unit tests.
    """

    def setup_method(self):
        with _trackers_lock:
            _trackers.pop(_SIGNAL_PIPELINE_ID, None)

    def teardown_method(self):
        with _trackers_lock:
            _trackers.pop(_SIGNAL_PIPELINE_ID, None)

    def _register_tracker_with_proposal(self):
        from peer_consensus import create_peer_consensus_tracker

        tracker = create_peer_consensus_tracker(
            _SIGNAL_PIPELINE_ID, _make_two_reviewer_graph(), cooldown_seconds=0
        )
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc123"},
        )
        return tracker

    # `make_error_response` / `make_success_response` in the signal handler
    # call Flask's `jsonify`, which needs an app context. A minimal Flask
    # app is enough — no blueprint registration required.
    _SUBSTANTIVE_REASON = (
        "Read src/a.py lines 10-42 and confirmed the token validation flow "
        "is correct; merge-time rename is the only remaining gap."
    )
    _SUBSTANTIVE_CONDITION = (
        "git mv legacy/x new/x before merge — rename required for module path alignment"
    )

    def _flask_app_context(self):
        from flask import Flask

        return Flask(__name__).app_context()

    def test_signal_ack_propagates_condition_to_pr_body(self):
        from message_store import MessageStore
        from routes import pipelines as p
        from routes.signals import handle_consensus_ack_signal

        self._register_tracker_with_proposal()
        live_store = MessageStore()

        with (
            self._flask_app_context(),
            patch("message_store.get_message_store", return_value=live_store),
            patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
        ):
            response, status_code = handle_consensus_ack_signal(
                _SIGNAL_PIPELINE_ID,
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "payload": {
                        "artifact_references": ["src/a.py"],
                        "reason": self._SUBSTANTIVE_REASON,
                        "pre_merge_condition": self._SUBSTANTIVE_CONDITION,
                    },
                },
                Path("/tmp/repo"),
            )

            assert status_code == 200
            body = json.loads(response.data)
            assert body["success"] is True
            assert body["data"]["pre_merge_condition"] == self._SUBSTANTIVE_CONDITION

        section = p._build_pre_merge_obligations_section(_SIGNAL_PIPELINE_ID)
        assert "Pre-merge Obligations" in section
        assert "reviewer_code" in section
        assert self._SUBSTANTIVE_CONDITION in section

        stored = live_store.get_messages(_SIGNAL_PIPELINE_ID, limit=10)
        ack_msgs = [m for m in stored if m.message_type == "CONSENSUS_ACK"]
        assert len(ack_msgs) == 1
        assert ack_msgs[0].metadata["payload"]["pre_merge_condition"] == self._SUBSTANTIVE_CONDITION

    def test_signal_ack_without_condition_renders_no_section(self):
        from message_store import MessageStore
        from routes import pipelines as p
        from routes.signals import handle_consensus_ack_signal

        self._register_tracker_with_proposal()

        with (
            self._flask_app_context(),
            patch("message_store.get_message_store", return_value=MessageStore()),
            patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
        ):
            response, status_code = handle_consensus_ack_signal(
                _SIGNAL_PIPELINE_ID,
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "payload": {
                        "artifact_references": ["src/a.py"],
                        "reason": self._SUBSTANTIVE_REASON,
                    },
                },
                Path("/tmp/repo"),
            )

            assert status_code == 200
            assert "pre_merge_condition" not in json.loads(response.data)["data"]

        assert p._build_pre_merge_obligations_section(_SIGNAL_PIPELINE_ID) == ""


# --- Reconstruction from message store ------------------------------------


_RECONSTRUCT_PIPELINE_ID = "pipeline-reconstruct-condition"


class _FakeMessage:
    """Minimal message shape expected by reconstruct_tracker_from_messages."""

    def __init__(
        self, message_type, from_role, to_role="all", metadata=None, timestamp=None, msg_id=None
    ):
        self.id = msg_id or f"msg-{message_type.lower()}"
        self.message_type = message_type
        self.from_role = from_role
        self.to_role = to_role
        self.body = ""
        self.metadata = metadata or {}
        self.timestamp = timestamp or datetime.now(UTC)


class _FakeMessageStore:
    def __init__(self, messages):
        self._messages = list(messages)

    def get_messages(self, pipeline_id, *, limit=100):
        return list(self._messages)


class TestReconstructionSurvivesCondition:
    """After an orchestrator restart, the tracker is rebuilt by replaying
    messages from Redis. `reconstruct_tracker_from_messages` reads
    `metadata["payload"]` verbatim into `tracker.handle_ack`, so
    `pre_merge_condition` should round-trip — but nothing pinned it.
    """

    def setup_method(self):
        with _trackers_lock:
            _trackers.pop(_RECONSTRUCT_PIPELINE_ID, None)

    def teardown_method(self):
        with _trackers_lock:
            _trackers.pop(_RECONSTRUCT_PIPELINE_ID, None)

    def test_condition_survives_message_store_replay(self):
        base = datetime.now(UTC)
        messages = [
            _FakeMessage(
                "CONSENSUS_PROPOSE",
                "coder",
                "all",
                metadata={
                    "payload": {
                        "summary": "impl",
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc123",
                    }
                },
                timestamp=base,
                msg_id="msg-propose",
            ),
            _FakeMessage(
                "CONSENSUS_ACK",
                "reviewer_code",
                "coder",
                metadata={
                    "payload": {
                        "reason": "code is correct; merge-time rename required",
                        "artifact_references": ["src/a.py"],
                        "pre_merge_condition": "git mv legacy/x new/x",
                    }
                },
                timestamp=base + timedelta(seconds=1),
                msg_id="msg-ack",
            ),
        ]

        tracker = reconstruct_tracker_from_messages(
            _RECONSTRUCT_PIPELINE_ID,
            _make_two_reviewer_graph(),
            message_store=_FakeMessageStore(messages),
        )

        assert tracker is not None
        conditions = tracker.get_pre_merge_conditions()
        assert len(conditions) == 1
        assert conditions[0]["reviewer"] == "reviewer_code"
        assert conditions[0]["producer"] == "coder"
        assert conditions[0]["condition"] == "git mv legacy/x new/x"

    def test_resolution_survives_message_store_replay(self):
        """An obligation resolved before an orchestrator restart must
        stay resolved after replay — without a persisted
        ``CONSENSUS_OBLIGATION_RESOLVED`` message, the matrix would re-
        emerge with ``obligation_resolved=False`` and the HITL gate
        would queue a decision for work that was already done (#2338
        blocking-1)."""
        base = datetime.now(UTC)
        messages = [
            _FakeMessage(
                "CONSENSUS_PROPOSE",
                "coder",
                "all",
                metadata={
                    "payload": {
                        "summary": "impl",
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc123",
                    }
                },
                timestamp=base,
                msg_id="msg-propose",
            ),
            _FakeMessage(
                "CONSENSUS_ACK",
                "reviewer_code",
                "coder",
                metadata={
                    "payload": {
                        "reason": "code is correct; tester picks up the rename",
                        "artifact_references": ["src/a.py"],
                        "pre_merge_condition": "git mv legacy/x new/x",
                    }
                },
                timestamp=base + timedelta(seconds=1),
                msg_id="msg-ack",
            ),
            _FakeMessage(
                "CONSENSUS_OBLIGATION_RESOLVED",
                "tester",
                "coder",
                metadata={
                    "reviewer_role": "reviewer_code",
                    "producer_role": "coder",
                    "resolver_role": "tester",
                    "commit_sha": "def4567",
                    "note": "cherry-picked from coder branch",
                    "version": 1,
                    "condition": "git mv legacy/x new/x",
                },
                timestamp=base + timedelta(seconds=2),
                msg_id="msg-resolve",
            ),
            _FakeMessage(
                "CONSENSUS_CONFIRMED",
                "coder",
                "all",
                metadata={},
                timestamp=base + timedelta(seconds=3),
                msg_id="msg-confirmed",
            ),
        ]

        tracker = reconstruct_tracker_from_messages(
            _RECONSTRUCT_PIPELINE_ID,
            _make_two_reviewer_graph(),
            message_store=_FakeMessageStore(messages),
        )

        assert tracker is not None
        # The obligation was resolved in-cycle — get_pre_merge_conditions
        # must filter it out after replay.
        assert tracker.get_pre_merge_conditions() == []
        # Audit fields survive too.
        entry = tracker.matrix.get_entry("reviewer_code", "coder")
        assert entry is not None
        assert entry.obligation_resolved is True
        assert entry.obligation_resolved_by == "tester"
        assert entry.obligation_resolved_commit == "def4567"
        assert entry.obligation_resolved_note == "cherry-picked from coder branch"


# --- BRC history transcript -----------------------------------------------


class TestBrcHistoryExposesCondition:
    """The committed `.egg-state/brc-history/{id}-{phase}.{md,json}` files
    are the long-term audit trail for BRC. A conditional ACK must leave a
    trace there — otherwise a reviewer could attach an obligation that
    disappears once the live tracker is cleaned up.

    `_write_brc_history` already serializes `msg.metadata` verbatim into
    the YAML meta-block of the .md file and into the .json companion via
    `msg.to_dict()`, so the condition rides along today without a
    dedicated field. This test pins that behavior.
    """

    def _conditional_ack_message(self):
        # Import lazily because message_store imports are heavy and some
        # tests above mock the module.
        from message_store import Message, MessageType

        return Message(
            pipeline_id="issue-42",
            from_role="reviewer_code",
            to_role="coder",
            message_type=MessageType.CONSENSUS_ACK,
            subject="ACK from reviewer_code for coder",
            body="code is correct; merge-time rename required",
            phase="implement",
            timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            metadata={
                "payload": {
                    "verdict": "ACK",
                    "artifact_references": ["src/a.py"],
                    "reason": "code is correct; merge-time rename required",
                    "pre_merge_condition": "git mv legacy/x new/x before merge",
                },
                "version": 1,
            },
        )

    def test_condition_appears_in_markdown_transcript(self, tmp_path):
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock()
        mock_store.get_messages.return_value = [self._conditional_ack_message()]

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        md_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.md"
        assert md_path.exists()
        content = md_path.read_text()
        assert "pre_merge_condition" in content
        assert "git mv legacy/x new/x before merge" in content

    def test_condition_appears_in_json_companion(self, tmp_path):
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock()
        mock_store.get_messages.return_value = [self._conditional_ack_message()]

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        json_path = tmp_path / ".egg-state" / "brc-history" / "42-implement.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert len(data) == 1
        payload = data[0]["metadata"]["payload"]
        assert payload["pre_merge_condition"] == "git mv legacy/x new/x before merge"


# --- In-cycle obligation resolution (#2338) --------------------------------


class TestObligationResolutionMatrix:
    """``ApprovalMatrix.mark_obligation_resolved`` filters resolved
    obligations out of ``get_pre_merge_conditions`` so the PR body builder
    and HITL gate stop surfacing them. The flag is per-version and resets
    on every fresh ACK / NACK / invalidate (#2338)."""

    def test_mark_obligation_resolved_filters_from_conditions(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            artifact_refs=["src/a.py"],
            pre_merge_condition="tester must commit X",
        )
        # Sanity: obligation present before resolution.
        assert len(matrix.get_pre_merge_conditions()) == 1

        entry = matrix.mark_obligation_resolved(
            "reviewer_contract",
            "coder",
            resolved_by="tester",
            commit_sha="abc1234",
            note="cherry-picked from coder branch",
        )
        # The obligation text is preserved on the entry for audit.
        assert entry.pre_merge_condition == "tester must commit X"
        assert entry.obligation_resolved is True
        assert entry.obligation_resolved_by == "tester"
        assert entry.obligation_resolved_commit == "abc1234"
        assert entry.obligation_resolved_note == "cherry-picked from coder branch"
        # …but it no longer surfaces as an active condition.
        assert matrix.get_pre_merge_conditions() == []

    def test_mark_obligation_resolved_strips_audit_fields(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            pre_merge_condition="X",
        )
        entry = matrix.mark_obligation_resolved(
            "reviewer_contract",
            "coder",
            resolved_by="  tester  ",
            commit_sha="  abc  ",
            note="  picked it up  ",
        )
        assert entry.obligation_resolved_by == "tester"
        assert entry.obligation_resolved_commit == "abc"
        assert entry.obligation_resolved_note == "picked it up"

    def test_mark_obligation_resolved_unknown_edge(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        with pytest.raises(ValueError, match="No review edge"):
            matrix.mark_obligation_resolved("reviewer_unknown", "coder")

    def test_mark_obligation_resolved_non_acked_edge(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        # Edge is PENDING — never ACKed.
        with pytest.raises(ValueError, match="not ACKED"):
            matrix.mark_obligation_resolved("reviewer_contract", "coder")

    def test_mark_obligation_resolved_no_active_obligation(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            # No pre_merge_condition — unconditional ACK.
        )
        with pytest.raises(ValueError, match="No active obligation"):
            matrix.mark_obligation_resolved("reviewer_contract", "coder")

    def test_record_ack_resets_resolved_flag(self, matrix_graph):
        """A fresh ACK at a new version starts un-resolved — the satisfier
        must re-call resolve_obligation if the reviewer re-attaches the
        same obligation. (Per-version resolution, by design.)"""
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")  # v1
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            pre_merge_condition="X",
        )
        matrix.mark_obligation_resolved("reviewer_contract", "coder")
        assert matrix.get_pre_merge_conditions() == []

        # Producer re-proposes; reviewer re-ACKs with the same obligation.
        matrix.record_proposal("coder")  # v2
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=2,
            pre_merge_condition="X",
        )
        # Resolution did not carry forward — the obligation is live again.
        entry = matrix.get_entry("reviewer_contract", "coder")
        assert entry is not None
        assert entry.obligation_resolved is False
        assert entry.obligation_resolved_by == ""
        assert len(matrix.get_pre_merge_conditions()) == 1

    def test_record_nack_resets_resolved_flag(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            pre_merge_condition="X",
        )
        matrix.mark_obligation_resolved("reviewer_contract", "coder")
        matrix.record_nack(
            "reviewer_contract",
            "coder",
            version=1,
            reason="changed my mind",
            artifact_refs=["src/a.py"],
        )
        entry = matrix.get_entry("reviewer_contract", "coder")
        assert entry is not None
        assert entry.obligation_resolved is False
        assert entry.obligation_resolved_by == ""

    def test_invalidate_ack_resets_resolved_flag(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            pre_merge_condition="X",
        )
        matrix.mark_obligation_resolved("reviewer_contract", "coder")
        matrix.invalidate_ack("reviewer_contract", "coder")
        entry = matrix.get_entry("reviewer_contract", "coder")
        assert entry is not None
        assert entry.obligation_resolved is False
        assert entry.obligation_resolved_by == ""

    def test_round_trip_persists_resolution_state(self, matrix_graph):
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            pre_merge_condition="X",
        )
        matrix.mark_obligation_resolved(
            "reviewer_contract",
            "coder",
            resolved_by="tester",
            commit_sha="abc1234",
            note="picked up",
        )
        data = matrix.to_dict()
        restored = ApprovalMatrix.from_dict(data, matrix_graph)
        entry = restored.get_entry("reviewer_contract", "coder")
        assert entry is not None
        assert entry.obligation_resolved is True
        assert entry.obligation_resolved_by == "tester"
        assert entry.obligation_resolved_commit == "abc1234"
        assert entry.obligation_resolved_note == "picked up"
        # Resolution timestamp round-trips so audit logs can sequence
        # the resolution against any later re-ACK that resets the flag.
        assert entry.obligation_resolved_at is not None
        assert restored.get_pre_merge_conditions() == []

    def test_resolution_records_timestamp(self, matrix_graph):
        """``obligation_resolved_at`` is populated on resolution and
        cleared by every ``record_ack`` / ``record_nack`` /
        ``invalidate_ack`` (#2338 audit-trail follow-up)."""
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            pre_merge_condition="X",
        )
        before = datetime.now(UTC)
        entry = matrix.mark_obligation_resolved(
            "reviewer_contract",
            "coder",
            resolved_by="tester",
        )
        after = datetime.now(UTC)
        assert entry.obligation_resolved_at is not None
        assert before <= entry.obligation_resolved_at <= after

        # A fresh ACK clears the timestamp.
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=2,
            pre_merge_condition="X",
        )
        entry = matrix.get_entry("reviewer_contract", "coder")
        assert entry is not None
        assert entry.obligation_resolved_at is None

    def test_producer_cannot_self_resolve(self, matrix_graph):
        """The producer cannot resolve an obligation attached to their
        own edge — that would single-handedly bypass the reviewer's veto.
        The reviewer must drop the condition on re-ACK or another role
        must resolve (#2338 authorization gate)."""
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            pre_merge_condition="X",
        )
        with pytest.raises(ValueError, match="cannot self-resolve"):
            matrix.mark_obligation_resolved(
                "reviewer_contract",
                "coder",
                resolved_by="coder",
            )
        # The obligation is still live — the resolution did not partially
        # apply before the check fired.
        assert len(matrix.get_pre_merge_conditions()) == 1
        entry = matrix.get_entry("reviewer_contract", "coder")
        assert entry is not None
        assert entry.obligation_resolved is False

    def test_third_role_resolution_allowed(self, matrix_graph):
        """A role that is neither the producer nor the reviewer (e.g.
        the tester picking up gateway-blocked work) is the documented
        use case and must succeed."""
        matrix = ApprovalMatrix(matrix_graph)
        matrix.record_proposal("coder")
        matrix.record_ack(
            "reviewer_contract",
            "coder",
            version=1,
            pre_merge_condition="X",
        )
        entry = matrix.mark_obligation_resolved(
            "reviewer_contract",
            "coder",
            resolved_by="tester",
        )
        assert entry.obligation_resolved is True
        assert entry.obligation_resolved_by == "tester"


class TestObligationResolutionTracker:
    def test_handle_resolve_obligation_filters_conditions(self, matrix_graph):
        tracker = PeerConsensusTracker("pid-resolve-1", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_contract",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": "tester must commit patch-path rewrites",
            },
        )
        assert len(tracker.get_pre_merge_conditions()) == 1

        result = tracker.handle_resolve_obligation(
            resolver_role="tester",
            reviewer_role="reviewer_contract",
            producer_role="coder",
            commit_sha="abc1234",
            note="cherry-picked from coder branch",
        )
        assert result["status"] == "resolved"
        assert result["reviewer"] == "reviewer_contract"
        assert result["producer"] == "coder"
        assert result["resolver"] == "tester"
        assert result["condition"] == "tester must commit patch-path rewrites"
        assert result["remaining_pre_merge_conditions"] == []
        assert tracker.get_pre_merge_conditions() == []

    def test_handle_resolve_obligation_unknown_edge_raises(self, matrix_graph):
        tracker = PeerConsensusTracker("pid-resolve-2", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_contract")
        with pytest.raises(ValueError, match="No review edge"):
            tracker.handle_resolve_obligation(
                resolver_role="tester",
                reviewer_role="reviewer_unknown",
                producer_role="coder",
            )

    def test_handle_resolve_obligation_no_active_obligation(self, matrix_graph):
        tracker = PeerConsensusTracker("pid-resolve-3", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        # Unconditional ACK — no obligation to resolve.
        tracker.handle_ack(
            "reviewer_contract",
            "coder",
            {"artifact_references": ["src/a.py"]},
        )
        with pytest.raises(ValueError, match="No active obligation"):
            tracker.handle_resolve_obligation(
                resolver_role="tester",
                reviewer_role="reviewer_contract",
                producer_role="coder",
            )


class TestResolveObligationSignalHandler:
    """``handle_consensus_resolve_obligation_signal`` dispatches to the
    tracker and returns a structured response. The signal handler is a
    thin wrapper around ``tracker.handle_resolve_obligation`` — these
    tests cover the routing layer specifically (#2338)."""

    @pytest.fixture
    def app(self):
        from flask import Flask
        from routes.signals import signals_bp

        app = Flask(__name__)
        app.register_blueprint(signals_bp)
        app.config["TESTING"] = True
        return app

    def _set_tracker(self, matrix_graph):
        """Build a tracker with one live conditional ACK and register it
        in the global tracker map under ``test-pid-signal``."""
        tracker = PeerConsensusTracker("test-pid-signal", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_contract",
            "coder",
            {
                "artifact_references": ["src/a.py"],
                "pre_merge_condition": "tester must commit X",
            },
        )
        with _trackers_lock:
            _trackers["test-pid-signal"] = tracker
        return tracker

    def _clear_tracker(self):
        with _trackers_lock:
            _trackers.pop("test-pid-signal", None)

    def test_resolves_active_obligation(self, app, matrix_graph):
        from message_store import MessageStore

        tracker = self._set_tracker(matrix_graph)
        live_store = MessageStore()
        try:
            from routes.signals import handle_consensus_resolve_obligation_signal

            with (
                app.app_context(),
                patch("message_store.get_message_store", return_value=live_store),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status = handle_consensus_resolve_obligation_signal(
                    "test-pid-signal",
                    {
                        "agent_role": "tester",
                        "reviewer_role": "reviewer_contract",
                        "producer_role": "coder",
                        "commit_sha": "abc1234",
                        "note": "cherry-picked",
                    },
                    Path("/tmp/repo"),
                )

            assert status == 200
            body = response.get_json()
            assert body["success"] is True
            assert body["data"]["status"] == "resolved"
            assert body["data"]["resolver"] == "tester"
            assert body["data"]["remaining_pre_merge_conditions"] == []
            assert tracker.get_pre_merge_conditions() == []

            # The signal handler must persist a CONSENSUS_OBLIGATION_RESOLVED
            # message so reconstruct_tracker_from_messages can replay the
            # resolution after an orchestrator restart. Without persistence the
            # matrix re-emerges with obligation_resolved=False and the HITL
            # gate fires for work that was already done (#2338 blocking-1).
            stored = live_store.get_messages("test-pid-signal", limit=10)
            resolved_msgs = [m for m in stored if m.message_type == "CONSENSUS_OBLIGATION_RESOLVED"]
            assert len(resolved_msgs) == 1
            msg = resolved_msgs[0]
            assert msg.from_role == "tester"
            assert msg.to_role == "coder"
            assert msg.metadata["reviewer_role"] == "reviewer_contract"
            assert msg.metadata["producer_role"] == "coder"
            assert msg.metadata["resolver_role"] == "tester"
            assert msg.metadata["commit_sha"] == "abc1234"
            assert msg.metadata["note"] == "cherry-picked"
        finally:
            self._clear_tracker()

    def test_producer_self_resolve_returns_400(self, app, matrix_graph):
        """The matrix rejects ``resolved_by == producer`` to prevent a
        producer from erasing a reviewer's veto on their own commit. The
        signal handler translates the matrix ``ValueError`` into a 400 and
        does not persist a CONSENSUS_OBLIGATION_RESOLVED message — both
        properties matter, the latter so a stale rejection cannot leak into
        replay (#2338 blocking-2)."""
        from message_store import MessageStore

        tracker = self._set_tracker(matrix_graph)
        live_store = MessageStore()
        try:
            from routes.signals import handle_consensus_resolve_obligation_signal

            with (
                app.app_context(),
                patch("message_store.get_message_store", return_value=live_store),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                response, status = handle_consensus_resolve_obligation_signal(
                    "test-pid-signal",
                    {
                        "agent_role": "coder",
                        "reviewer_role": "reviewer_contract",
                        "producer_role": "coder",
                        "commit_sha": "abc1234",
                        "note": "trying to self-resolve",
                    },
                    Path("/tmp/repo"),
                )

            assert status == 400
            body = response.get_json()
            assert body["success"] is False
            assert "self-resolve" in body.get("message", "")

            # Tracker state was not mutated.
            assert len(tracker.get_pre_merge_conditions()) == 1

            # No CONSENSUS_OBLIGATION_RESOLVED message was persisted — the
            # rejection happens before add_message in the signal handler.
            stored = live_store.get_messages("test-pid-signal", limit=10)
            assert not [m for m in stored if m.message_type == "CONSENSUS_OBLIGATION_RESOLVED"]
        finally:
            self._clear_tracker()

    def test_missing_fields_return_400(self, app, matrix_graph):
        self._set_tracker(matrix_graph)
        try:
            from routes.signals import handle_consensus_resolve_obligation_signal

            with app.app_context():
                # Missing reviewer_role.
                _, status = handle_consensus_resolve_obligation_signal(
                    "test-pid-signal",
                    {"agent_role": "tester", "producer_role": "coder"},
                    Path("/tmp/repo"),
                )
                assert status == 400

                # Missing producer_role.
                _, status = handle_consensus_resolve_obligation_signal(
                    "test-pid-signal",
                    {"agent_role": "tester", "reviewer_role": "reviewer_contract"},
                    Path("/tmp/repo"),
                )
                assert status == 400

                # Missing agent_role.
                _, status = handle_consensus_resolve_obligation_signal(
                    "test-pid-signal",
                    {
                        "reviewer_role": "reviewer_contract",
                        "producer_role": "coder",
                    },
                    Path("/tmp/repo"),
                )
                assert status == 400
        finally:
            self._clear_tracker()

    def test_no_active_obligation_returns_400(self, app, matrix_graph):
        # Build a tracker with an unconditional ACK only.
        tracker = PeerConsensusTracker("test-pid-signal-empty", matrix_graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")
        tracker.register_agent("reviewer_contract")
        tracker.handle_propose(
            "coder",
            {"summary": "impl", "artifacts": ["src/a.py"], "commit_sha": "abc"},
        )
        tracker.handle_ack(
            "reviewer_contract",
            "coder",
            {"artifact_references": ["src/a.py"]},
        )
        with _trackers_lock:
            _trackers["test-pid-signal-empty"] = tracker
        try:
            from routes.signals import handle_consensus_resolve_obligation_signal

            with app.app_context():
                response, status = handle_consensus_resolve_obligation_signal(
                    "test-pid-signal-empty",
                    {
                        "agent_role": "tester",
                        "reviewer_role": "reviewer_contract",
                        "producer_role": "coder",
                    },
                    Path("/tmp/repo"),
                )
                assert status == 400
                body = response.get_json()
                assert body["success"] is False
                assert "No active obligation" in body.get("message", "")
        finally:
            with _trackers_lock:
                _trackers.pop("test-pid-signal-empty", None)

    def test_missing_tracker_returns_404(self, app):
        from routes.signals import handle_consensus_resolve_obligation_signal

        with app.app_context():
            response, status = handle_consensus_resolve_obligation_signal(
                "no-such-pipeline",
                {
                    "agent_role": "tester",
                    "reviewer_role": "reviewer_contract",
                    "producer_role": "coder",
                },
                Path("/tmp/repo"),
            )
            assert status == 404
            body = response.get_json()
            assert body["success"] is False
