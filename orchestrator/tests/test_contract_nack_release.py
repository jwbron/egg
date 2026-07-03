"""Tests for the contract-blocked NACK release (#3470).

The mark-after-NACK deadlock: the #3114 ACK-guard rejects a contract
enforcer's ACK while the producer owns incomplete contract rows, so the
enforcer NACKs citing the rows. The producer then repairs the cited
blocker via ``mcp__task__complete`` — but a task-status mutation moves no
BRC state, so the reviewer keeps deriving ``wait`` (its verdict on the
current version stands) while the producer cannot re-propose (zero new
commits → the unchanged-re-propose guard rejects it). Observed as an ~8h
slice-5 deadlock on ``pipeline-dcdad92d`` requiring an operator
``restart_agent``.

Covers:

* ``ApprovalMatrix.invalidate_nack`` — NACKED→PENDING with the verdict
  version stepped back; no-op on non-NACKED entries.
* ``PeerConsensusTracker.release_contract_nack`` — release + re-derived
  ``ack`` for the reviewer, idempotent no-op, missing-edge rejection,
  and the full mark-after-NACK convergence (release → re-ACK → confirm)
  without any producer re-propose.
* ``reconstruct_tracker_from_messages`` — the
  ``CONSENSUS_NACK_INVALIDATED`` replay arm: without it a restart
  resurrects the NACK and the deadlock it caused.
* The mutate-route trigger — a ``phases.*.tasks.*.status`` → complete
  mutation durably persists the contract (#3427 pattern extended) and
  releases enforcer NACKs that cited contract incompleteness.
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

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing route modules that depend on it.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

import contract_store  # noqa: E402
from approval_matrix import ApprovalState  # noqa: E402
from egg_orchestrator.types import ConsensusPhase  # noqa: E402
from peer_consensus import (  # noqa: E402
    PeerConsensusTracker,
    _tracker_key,
    _trackers,
    _trackers_lock,
    reconstruct_tracker_from_messages,
)
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph  # noqa: E402
from routes.consensus import _derive_next_action  # noqa: E402

PIPELINE_ID = "pipeline-3470-release"

NACK_CONTRACT_REASON = (
    "contract_incomplete: task-1-1 is not marked complete in the contract; "
    "deliver the work or mark finished work complete via mcp__task__complete."
)
NACK_DEFECT_REASON = (
    "The retry loop in a.py never backs off, so the gateway is hammered on "
    "every failure; please add exponential backoff before I can approve."
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _graph() -> ReviewGraph:
    return ReviewGraph(
        [
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
        ]
    )


def _tracker(pipeline_id: str = PIPELINE_ID) -> PeerConsensusTracker:
    t = PeerConsensusTracker(pipeline_id, _graph(), cooldown_seconds=0)
    t.register_agent("coder")
    t.register_agent("reviewer_contract")
    return t


def _propose(tracker: PeerConsensusTracker, role: str = "coder") -> None:
    tracker.handle_propose(
        role,
        {
            "summary": (
                "Proposal with substantive content describing the work, "
                "tests run, and tasks satisfied for review."
            ),
            "artifacts": ["a.py"],
            "commit_sha": "abc1234",
        },
    )


def _nack(
    tracker: PeerConsensusTracker,
    reviewer: str,
    producer: str,
    *,
    reason: str = NACK_CONTRACT_REASON,
    version: int = 1,
) -> None:
    tracker.handle_nack(
        reviewer,
        producer,
        {
            "artifact_references": ["a.py"],
            "reason": reason,
            "nack_version": version,
        },
    )


def _ack(tracker: PeerConsensusTracker, reviewer: str, producer: str, *, version: int = 1) -> None:
    tracker.handle_ack(
        reviewer,
        producer,
        {
            "artifact_references": ["a.py"],
            "reason": (
                "Substantive review verdict satisfying the ≥50 char content "
                "gate enforced by _validate_brc_content."
            ),
            "ack_version": version,
        },
    )


# ---------------------------------------------------------------------------
# Matrix unit: invalidate_nack
# ---------------------------------------------------------------------------


class TestInvalidateNack:
    def test_nacked_entry_returns_to_pending_with_version_stepped(self):
        tracker = _tracker()
        _propose(tracker)
        _nack(tracker, "reviewer_contract", "coder")

        assert tracker.matrix.invalidate_nack("reviewer_contract", "coder") is True

        entry = tracker.matrix.get_entry("reviewer_contract", "coder")
        assert entry.state == ApprovalState.PENDING
        # Below the current proposal version → pending-review derivation
        # sees an un-verdicted current proposal.
        assert entry.version < tracker.matrix.get_proposal_version("coder")
        assert entry.reason == ""
        assert entry.nack_artifact_refs == []

    def test_non_nacked_entry_is_noop(self):
        tracker = _tracker()
        _propose(tracker)
        assert tracker.matrix.invalidate_nack("reviewer_contract", "coder") is False

        _ack(tracker, "reviewer_contract", "coder")
        assert tracker.matrix.invalidate_nack("reviewer_contract", "coder") is False
        entry = tracker.matrix.get_entry("reviewer_contract", "coder")
        assert entry.state == ApprovalState.ACKED


# ---------------------------------------------------------------------------
# Tracker unit: release_contract_nack
# ---------------------------------------------------------------------------


class TestReleaseContractNack:
    def test_release_re_derives_ack_for_reviewer(self):
        tracker = _tracker()
        _propose(tracker)
        _nack(tracker, "reviewer_contract", "coder")

        # Deadlock state: reviewer holds a standing verdict → wait; the
        # producer derives propose (address NACKs) but has nothing new
        # to commit.
        action, _, _ = _derive_next_action(tracker, "reviewer_contract")
        assert action == "wait"

        result = tracker.release_contract_nack(
            "reviewer_contract", "coder", "rows complete (#3470)"
        )
        assert result["status"] == "released"

        # The reviewer now derives an actionable re-review.
        action, payload, _ = _derive_next_action(tracker, "reviewer_contract")
        assert action == "ack"
        assert payload["pending_reviews"][0]["producer"] == "coder"

        # The producer no longer derives a doomed re-propose.
        action, _, _ = _derive_next_action(tracker, "coder")
        assert action == "wait"

    def test_release_is_idempotent(self):
        tracker = _tracker()
        _propose(tracker)
        _nack(tracker, "reviewer_contract", "coder")

        assert tracker.release_contract_nack("reviewer_contract", "coder")["status"] == "released"
        assert tracker.release_contract_nack("reviewer_contract", "coder")["status"] == "noop"

    def test_release_without_edge_raises(self):
        tracker = _tracker()
        with pytest.raises(ValueError, match="No review edge"):
            tracker.release_contract_nack("reviewer_contract", "documenter")

    def test_mark_after_nack_converges_without_re_propose(self):
        """The #3470 acceptance sequence: NACK for contract_incomplete →
        producer marks rows complete (no new commits, no re-propose) →
        release → reviewer re-ACKs the SAME version → consensus completes.
        """
        tracker = _tracker()
        _propose(tracker)
        _nack(tracker, "reviewer_contract", "coder")

        tracker.release_contract_nack("reviewer_contract", "coder", "rows complete")

        # Re-verdict on the same proposal version — no producer re-propose.
        _ack(tracker, "reviewer_contract", "coder", version=1)
        assert tracker.matrix.get_proposal_version("coder") == 1

        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_contract")
        assert tracker.evaluate()["is_complete"]
        assert tracker._producer_phases["coder"] == ConsensusPhase.CONFIRMED

    def test_release_does_not_restore_proposed_for_withdrawn_producer(self):
        """A producer that WITHDREW after the NACK keeps its proposal
        retracted (#3470): release must not restore PROPOSED, or the freed
        reviewer could re-review and ACK withdrawn work before the producer
        re-proposes. The producer stays WORKING until its next (re-)propose.
        """
        tracker = _tracker()
        _propose(tracker)
        _nack(tracker, "reviewer_contract", "coder")

        # Producer withdraws the proposal after the NACK (both leave it
        # WORKING, so the restore must distinguish them).
        tracker.handle_withdraw("coder", "retracting to rework the approach")
        assert tracker._producer_phases["coder"] == ConsensusPhase.WORKING

        result = tracker.release_contract_nack("reviewer_contract", "coder", "rows complete")
        assert result["status"] == "released"

        # PROPOSED is NOT restored — the withdrawn proposal is not resurfaced.
        assert tracker._producer_phases["coder"] == ConsensusPhase.WORKING

        # The freed reviewer therefore does not derive an ACK against the
        # retracted work; it re-arms only when the producer re-proposes.
        action, _, _ = _derive_next_action(tracker, "reviewer_contract")
        assert action != "ack"

        # A subsequent re-propose (new commit SHA, so the unchanged-tree
        # guard allows it) clears the withdrawn state and the normal flow
        # resumes: the reviewer derives an actionable re-review.
        tracker.handle_propose(
            "coder",
            {
                "summary": (
                    "Reworked proposal with substantive content describing the "
                    "revised work, tests run, and tasks satisfied for review."
                ),
                "artifacts": ["a.py"],
                "commit_sha": "def5678",
            },
        )
        assert tracker._producer_phases["coder"] == ConsensusPhase.PROPOSED
        assert "coder" not in tracker._withdrawn_producers
        action, _, _ = _derive_next_action(tracker, "reviewer_contract")
        assert action == "ack"


# ---------------------------------------------------------------------------
# Reconstruction: CONSENSUS_NACK_INVALIDATED replay arm
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(
        self,
        message_type: str,
        from_role: str,
        to_role: str = "all",
        metadata: dict | None = None,
        timestamp: datetime | None = None,
        msg_id: str = "",
    ) -> None:
        self.id = msg_id or f"msg-{message_type.lower()}-{from_role}"
        self.message_type = message_type
        self.from_role = from_role
        self.to_role = to_role
        self.body = ""
        self.phase = "implement"
        self.metadata = metadata or {}
        self.timestamp = timestamp or datetime.now(UTC)


class _FakeMessageStore:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = list(messages)

    def get_messages(self, pipeline_id: str, *, limit: int = 100) -> list[_FakeMessage]:
        return list(self._messages)


class TestReconstructionReplaysRelease:
    def _messages(self) -> list[_FakeMessage]:
        t0 = datetime.now(UTC)
        propose_payload = {
            "payload": {
                "summary": (
                    "Proposal with substantive content describing the work, "
                    "tests run, and tasks satisfied for review."
                ),
                "artifacts": ["a.py"],
                "commit_sha": "abc1234",
            }
        }
        nack_payload = {
            "payload": {
                "artifact_references": ["a.py"],
                "reason": NACK_CONTRACT_REASON,
                "nack_version": 1,
            }
        }
        return [
            _FakeMessage(
                "CONSENSUS_PROPOSE", "coder", metadata=propose_payload, timestamp=t0, msg_id="m1"
            ),
            _FakeMessage(
                "CONSENSUS_NACK",
                "reviewer_contract",
                to_role="coder",
                metadata=nack_payload,
                timestamp=t0 + timedelta(seconds=1),
                msg_id="m2",
            ),
            # Producer marked its rows complete → orchestrator released
            # the contract-blocked NACK.
            _FakeMessage(
                "CONSENSUS_NACK_INVALIDATED",
                "orchestrator",
                to_role="reviewer_contract",
                metadata={"reviewer_role": "reviewer_contract", "producer_role": "coder"},
                timestamp=t0 + timedelta(seconds=2),
                msg_id="m3",
            ),
        ]

    def test_release_survives_replay(self):
        pid = "pipeline-3470-reconstruct"
        store = _FakeMessageStore(self._messages())
        try:
            tracker = reconstruct_tracker_from_messages(
                pid, _graph(), message_store=store, phase="implement"
            )
            assert tracker is not None

            entry = tracker.matrix.get_entry("reviewer_contract", "coder")
            assert entry.state == ApprovalState.PENDING
            assert not tracker.matrix.has_unresolved_nacks_as_producer("coder")

            # The reviewer re-derives an actionable re-review post-replay,
            # so a restart does not resurrect the deadlock.
            action, _, _ = _derive_next_action(tracker, "reviewer_contract")
            assert action == "ack"
        finally:
            with _trackers_lock:
                _trackers.pop(_tracker_key(pid), None)

    def test_release_message_with_missing_roles_is_skipped(self):
        pid = "pipeline-3470-reconstruct-skip"
        messages = self._messages()
        messages[2].metadata = {}
        messages[2].to_role = "all"
        store = _FakeMessageStore(messages)
        try:
            tracker = reconstruct_tracker_from_messages(
                pid, _graph(), message_store=store, phase="implement"
            )
            assert tracker is not None
            # Release skipped → the NACK stands (replay stays faithful to
            # what the malformed message can prove).
            entry = tracker.matrix.get_entry("reviewer_contract", "coder")
            assert entry.state == ApprovalState.NACKED
        finally:
            with _trackers_lock:
                _trackers.pop(_tracker_key(pid), None)


# ---------------------------------------------------------------------------
# Mutate-route trigger: durable persist + NACK release
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from api import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def fake_worktree(tmp_path: Path, monkeypatch):
    """Fake pipeline worktree with a one-slice contract (see
    ``test_contracts_routes.py`` for the pattern)."""
    pipeline_id = "pipeline-3470-route"
    worktrees_base = tmp_path / "worktrees"
    worktree = worktrees_base / pipeline_id / "egg"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", worktrees_base)

    contracts_dir = worktree / ".egg-state" / "contracts"
    contracts_dir.mkdir(parents=True)
    (contracts_dir / f"{pipeline_id}.json").write_text(
        json.dumps(
            {
                "schemaVersion": "1.0",
                "pipeline_id": pipeline_id,
                "issue": {"number": 3470, "title": "release test", "url": "http://example"},
                "phases": [
                    {
                        "id": "slice-1",
                        "name": "only slice",
                        "tasks": [
                            {
                                "id": "task-1-1",
                                "description": "coder deliverable",
                                "role": "coder",
                                "status": "pending",
                            },
                        ],
                    },
                ],
            }
        )
    )
    return pipeline_id, worktree


@pytest.fixture
def slice_tracker(fake_worktree):
    """A registered slice tracker in the NACKed-producer deadlock state."""
    pipeline_id, _ = fake_worktree
    key = _tracker_key(pipeline_id, "slice-1")
    tracker = PeerConsensusTracker(key, _graph(), cooldown_seconds=0)
    tracker.register_agent("coder")
    tracker.register_agent("reviewer_contract")
    _propose(tracker)
    _nack(tracker, "reviewer_contract", "coder")
    with _trackers_lock:
        _trackers[key] = tracker
    yield tracker
    with _trackers_lock:
        _trackers.pop(key, None)


def _complete_task(client, pipeline_id: str):
    return client.post(
        f"/api/v1/contracts/{pipeline_id}/mutate",
        json={
            "pipeline_id": pipeline_id,
            "repo": "egg",
            "field_path": "phases.0.tasks.0.status",
            "new_value": "complete",
        },
        headers={"X-Egg-Role": "implementer"},
    )


class TestMutateRouteTrigger:
    def test_task_status_mutation_triggers_durable_persist(self, client, fake_worktree):
        """#3470: a task-status write must commit+push at write time —
        otherwise the phase-(re)start ``git reset --hard`` reverts the
        completion and the #3114 ACK-guard re-rejects reviewer ACKs
        against already-delivered work.
        """
        pipeline_id, worktree = fake_worktree
        with patch("routes.pipelines.persist_contract_statefiles") as persist_mock:
            response = _complete_task(client, pipeline_id)
        assert response.status_code == 200, response.data
        persist_mock.assert_called_once()
        args = persist_mock.call_args[0]
        assert args[0] == pipeline_id
        assert args[1] == worktree

    def test_task_commit_mutation_triggers_durable_persist(self, client, fake_worktree):
        pipeline_id, _ = fake_worktree
        with patch("routes.pipelines.persist_contract_statefiles") as persist_mock:
            response = client.post(
                f"/api/v1/contracts/{pipeline_id}/mutate",
                json={
                    "pipeline_id": pipeline_id,
                    "repo": "egg",
                    "field_path": "phases.0.tasks.0.commit",
                    "new_value": "abc1234",
                },
                headers={"X-Egg-Role": "implementer"},
            )
        assert response.status_code == 200, response.data
        persist_mock.assert_called_once()

    def test_mark_after_nack_releases_enforcer_nack(self, client, fake_worktree, slice_tracker):
        """The #3470 regression: after the enforcer NACKed for
        contract_incomplete, marking the producer's rows complete must
        re-derive the reviewer (release the NACK) without any operator
        intervention or producer re-propose.
        """
        pipeline_id, _ = fake_worktree
        fake_store = MagicMock()
        with (
            patch("routes.pipelines.persist_contract_statefiles"),
            patch("message_store.get_message_store", return_value=fake_store),
        ):
            response = _complete_task(client, pipeline_id)
        assert response.status_code == 200, response.data

        entry = slice_tracker.matrix.get_entry("reviewer_contract", "coder")
        assert entry.state == ApprovalState.PENDING

        # Replay parity: the release was persisted to the bus first.
        fake_store.add_message.assert_called_once()
        message = fake_store.add_message.call_args[0][0]
        assert message.message_type == "CONSENSUS_NACK_INVALIDATED"
        assert message.metadata["reviewer_role"] == "reviewer_contract"
        assert message.metadata["producer_role"] == "coder"
        assert message.metadata["slice_id"] == "slice-1"

        # The reviewer re-derives an actionable re-review; the producer is
        # no longer pushed toward a doomed zero-commit re-propose.
        action, _, _ = _derive_next_action(slice_tracker, "reviewer_contract")
        assert action == "ack"
        action, _, _ = _derive_next_action(slice_tracker, "coder")
        assert action == "wait"

    def test_defect_nack_is_not_released(self, client, fake_worktree, slice_tracker):
        """A NACK citing a real artifact defect (no contract-incompleteness
        vocabulary) must survive the task completion — only the blocker the
        reviewer actually cited is repaired by a status flip.
        """
        pipeline_id, _ = fake_worktree
        slice_tracker.matrix.invalidate_nack("reviewer_contract", "coder")
        _nack(slice_tracker, "reviewer_contract", "coder", reason=NACK_DEFECT_REASON)

        fake_store = MagicMock()
        with (
            patch("routes.pipelines.persist_contract_statefiles"),
            patch("message_store.get_message_store", return_value=fake_store),
        ):
            response = _complete_task(client, pipeline_id)
        assert response.status_code == 200, response.data

        entry = slice_tracker.matrix.get_entry("reviewer_contract", "coder")
        assert entry.state == ApprovalState.NACKED
        fake_store.add_message.assert_not_called()

    def test_gate_kill_switch_disables_release(
        self, client, fake_worktree, slice_tracker, monkeypatch
    ):
        """With the #3114 gate off, consensus can close over incomplete
        rows, so there is no gate-minted blocker to release."""
        pipeline_id, _ = fake_worktree
        monkeypatch.setenv("EGG_CONTRACT_ACK_GATE", "off")

        fake_store = MagicMock()
        with (
            patch("routes.pipelines.persist_contract_statefiles"),
            patch("message_store.get_message_store", return_value=fake_store),
        ):
            response = _complete_task(client, pipeline_id)
        assert response.status_code == 200, response.data

        entry = slice_tracker.matrix.get_entry("reviewer_contract", "coder")
        assert entry.state == ApprovalState.NACKED
        fake_store.add_message.assert_not_called()

    def test_incomplete_sibling_row_defers_release(self, client, fake_worktree, slice_tracker):
        """The release fires only when the producer owes no more rows in
        the slice — completing one of two owned rows keeps the NACK."""
        pipeline_id, worktree = fake_worktree
        contract_path = worktree / ".egg-state" / "contracts" / f"{pipeline_id}.json"
        data = json.loads(contract_path.read_text())
        data["phases"][0]["tasks"].append(
            {
                "id": "task-1-2",
                "description": "second coder deliverable",
                "role": "coder",
                "status": "pending",
            }
        )
        contract_path.write_text(json.dumps(data))

        fake_store = MagicMock()
        with (
            patch("routes.pipelines.persist_contract_statefiles"),
            patch("message_store.get_message_store", return_value=fake_store),
        ):
            response = _complete_task(client, pipeline_id)
        assert response.status_code == 200, response.data

        entry = slice_tracker.matrix.get_entry("reviewer_contract", "coder")
        assert entry.state == ApprovalState.NACKED
        fake_store.add_message.assert_not_called()
