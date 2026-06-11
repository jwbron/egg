"""Tests for the confirmed-producer reopen + operator remediation (#3124).

Covers:

* ``PeerConsensusTracker.reopen_producer`` — CONFIRMED→WORKING reopen,
  idempotent no-op, non-producer rejection, and the downstream
  re-propose cascade (#1411 stale-confirm discard,
  ``_un_confirm_stale_reviewers``).
* ``reconstruct_tracker_from_messages`` — the ``CONSENSUS_REOPENED``
  replay arm: without it, replaying the post-reopen proposal would be
  rejected (propose guard requires WORKING) and a restart would
  resurrect the deadlock.
* ``routes.consensus._maybe_reopen_confirmed_producer`` via the
  next-action route — a confirmed producer that owns incomplete
  contract rows is flipped back to ``propose``; kill switch and
  no-incomplete-rows cases stay ``wait``.
* ``routes.signals._existing_confirmed_for_role`` — a CONFIRMED that
  predates the latest CONSENSUS_REOPENED no longer dedupes the
  re-confirm message write.
* ``operator_actions.complete_task_as_operator`` + the lifecycle-
  guarded REST route — the in-band operator path that replaces
  pod-exec role impersonation.
* ``routes.decisions._maybe_complete_task_from_resolution`` — the
  executable ``Mark task <id> complete`` resolution.
* ``impasse_routing._build_hitl_decision`` — escalations expose the
  executable completion option.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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

from egg_orchestrator.types import ConsensusPhase  # noqa: E402
from peer_consensus import (  # noqa: E402
    PeerConsensusTracker,
    _trackers,
    _trackers_lock,
    reconstruct_tracker_from_messages,
)
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph  # noqa: E402

PIPELINE_ID = "pipeline-3124-reopen"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _graph() -> ReviewGraph:
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
        ]
    )


def _tracker(pipeline_id: str = PIPELINE_ID) -> PeerConsensusTracker:
    t = PeerConsensusTracker(pipeline_id, _graph(), cooldown_seconds=0)
    t.register_agent("coder")
    t.register_agent("reviewer_code")
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


def _confirm_all(tracker: PeerConsensusTracker) -> None:
    """coder proposes, reviewer ACKs, both confirm."""
    _propose(tracker)
    _ack(tracker, "reviewer_code", "coder")
    tracker.handle_confirmed("coder")
    tracker.handle_confirmed("reviewer_code")


# ---------------------------------------------------------------------------
# Tracker unit: reopen_producer
# ---------------------------------------------------------------------------


class TestReopenProducer:
    def test_reopen_confirmed_producer_returns_to_working(self):
        tracker = _tracker()
        _confirm_all(tracker)
        assert "coder" in tracker.confirmed_roles

        result = tracker.reopen_producer("coder", reason="task reassigned")

        assert result["status"] == "reopened"
        assert "coder" not in tracker.confirmed_roles
        assert tracker._producer_phases["coder"] == ConsensusPhase.WORKING

    def test_reopened_producer_can_repropose_and_cascade_unconfirms_reviewer(self):
        """The re-propose path after a reopen is the existing machinery:
        the new version invalidates the reviewer's stale confirm
        (``_un_confirm_stale_reviewers``) so the slice re-converges on
        the new deliverable instead of closing over the old one.
        """
        tracker = _tracker()
        _confirm_all(tracker)
        tracker.reopen_producer("coder", reason="task reassigned")

        _propose(tracker)  # v2 — must not raise (WORKING again)

        assert tracker.matrix.get_proposal_version("coder") == 2
        assert "reviewer_code" not in tracker.confirmed_roles  # stale confirm cleared

        # Full re-convergence works end to end.
        _ack(tracker, "reviewer_code", "coder", version=2)
        tracker.handle_confirmed("coder")
        tracker.handle_confirmed("reviewer_code")
        assert tracker.evaluate()["is_complete"]

    def test_reopen_unconfirmed_producer_is_noop(self):
        tracker = _tracker()
        _propose(tracker)
        result = tracker.reopen_producer("coder")
        assert result["status"] == "noop"
        assert tracker._producer_phases["coder"] == ConsensusPhase.PROPOSED

    def test_reopen_non_producer_raises(self):
        tracker = _tracker()
        with pytest.raises(ValueError, match="not a producer"):
            tracker.reopen_producer("reviewer_code")


# ---------------------------------------------------------------------------
# Reconstruction: CONSENSUS_REOPENED replay arm
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


class TestReconstructionReplaysReopen:
    def test_post_reopen_proposal_survives_replay(self):
        pid = "pipeline-3124-reconstruct"
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
        ack_payload = {
            "payload": {
                "artifact_references": ["a.py"],
                "reason": (
                    "Substantive review verdict satisfying the ≥50 char "
                    "content gate enforced by _validate_brc_content."
                ),
                "ack_version": 1,
            }
        }
        messages = [
            _FakeMessage(
                "CONSENSUS_PROPOSE", "coder", metadata=propose_payload, timestamp=t0, msg_id="m1"
            ),
            _FakeMessage(
                "CONSENSUS_ACK",
                "reviewer_code",
                to_role="coder",
                metadata=ack_payload,
                timestamp=t0 + timedelta(seconds=1),
                msg_id="m2",
            ),
            _FakeMessage(
                "CONSENSUS_CONFIRMED",
                "coder",
                timestamp=t0 + timedelta(seconds=2),
                msg_id="m3",
            ),
            _FakeMessage(
                "CONSENSUS_CONFIRMED",
                "reviewer_code",
                timestamp=t0 + timedelta(seconds=3),
                msg_id="m4",
            ),
            # Task reassigned post-confirm → orchestrator reopened coder.
            _FakeMessage(
                "CONSENSUS_REOPENED",
                "orchestrator",
                to_role="coder",
                timestamp=t0 + timedelta(seconds=4),
                msg_id="m5",
            ),
            # coder's post-reopen proposal — without the replay arm the
            # propose guard rejects this (phase CONFIRMED, not WORKING).
            _FakeMessage(
                "CONSENSUS_PROPOSE",
                "coder",
                metadata=propose_payload,
                timestamp=t0 + timedelta(seconds=5),
                msg_id="m6",
            ),
        ]

        try:
            tracker = reconstruct_tracker_from_messages(
                pid, _graph(), message_store=_FakeMessageStore(messages)
            )
            assert tracker is not None
            # The v2 proposal replayed — version advanced past the reopen.
            assert tracker.matrix.get_proposal_version("coder") == 2
            assert "coder" not in tracker.confirmed_roles
            assert tracker._producer_phases["coder"] == ConsensusPhase.PROPOSED
        finally:
            with _trackers_lock:
                _trackers.pop(pid, None)


# ---------------------------------------------------------------------------
# Route: next-action reopen
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from flask import Flask
    from routes.consensus import consensus_bp

    app = Flask(__name__)
    app.register_blueprint(consensus_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _incomplete_row(task_id: str = "task-2-2") -> dict:
    return {"id": task_id, "role": "coder", "status": "pending", "commit": None}


def _next_action_with_reopen_env(
    client,
    tracker,
    *,
    rows,
    gate_enabled: bool = True,
    role: str = "coder",
):
    """POST next-action with the contract/state plumbing patched so the
    reopen helper sees an implement-phase pipeline whose contract has
    ``rows`` incomplete for ``role``."""
    pipeline_state = SimpleNamespace(
        current_phase=SimpleNamespace(value="implement"),
        issue_number=None,
    )
    state_store = MagicMock()
    state_store.load_pipeline.return_value = pipeline_state
    msg_store = MagicMock()

    with (
        patch("routes.consensus.get_peer_consensus_tracker", return_value=tracker),
        patch("routes.consensus.get_state_store", return_value=state_store),
        patch("routes.get_repo_path", return_value=Path("/tmp/unused")),
        patch("routes.resolve_repo_path_for_pipeline", side_effect=lambda pid, p: p),
        patch("routes.resolve_worktree_path", return_value=Path("/tmp/unused-worktree")),
        patch("contract_completeness.gate_enabled", return_value=gate_enabled),
        patch("contract_completeness.load_live_contract", return_value=object()),
        patch("contract_completeness.incomplete_tasks", return_value=rows),
        patch("message_store.get_message_store", return_value=msg_store),
    ):
        resp = client.post(
            f"/api/v1/pipelines/{PIPELINE_ID}/consensus/next-action",
            data=json.dumps({"role": role}),
            content_type="application/json",
        )
    return resp, msg_store


def _action(resp) -> tuple[str, dict]:
    assert resp.status_code == 200, resp.data
    data = json.loads(resp.data)
    payload = data.get("data", data)
    return payload["action"], payload


class TestNextActionReopen:
    def test_confirmed_producer_with_incomplete_task_gets_propose(self, client):
        tracker = _tracker()
        _confirm_all(tracker)

        resp, msg_store = _next_action_with_reopen_env(client, tracker, rows=[_incomplete_row()])

        action, payload = _action(resp)
        assert action == "propose"
        assert payload["event_payload"]["reopened_after_confirm"] is True
        assert payload["event_payload"]["incomplete_tasks"] == [_incomplete_row()]
        # Tracker state actually flipped.
        assert "coder" not in tracker.confirmed_roles
        assert tracker._producer_phases["coder"] == ConsensusPhase.WORKING
        # The reopen was persisted for replay.
        sent = msg_store.add_message.call_args[0][0]
        assert sent.message_type == "CONSENSUS_REOPENED"
        assert sent.to_role == "coder"

    def test_confirmed_producer_with_no_incomplete_tasks_stays_wait(self, client):
        tracker = _tracker()
        _confirm_all(tracker)

        resp, msg_store = _next_action_with_reopen_env(client, tracker, rows=[])

        action, _ = _action(resp)
        # Consensus is globally complete here, so the confirmed
        # short-circuit reports completion — the point is it does NOT
        # reopen or propose.
        assert action in ("wait", "complete")
        assert "coder" in tracker.confirmed_roles
        msg_store.add_message.assert_not_called()

    def test_kill_switch_disables_reopen(self, client):
        tracker = _tracker()
        _confirm_all(tracker)

        resp, msg_store = _next_action_with_reopen_env(
            client, tracker, rows=[_incomplete_row()], gate_enabled=False
        )

        action, _ = _action(resp)
        assert action in ("wait", "complete")
        assert "coder" in tracker.confirmed_roles
        msg_store.add_message.assert_not_called()

    def test_unconfirmed_producer_skips_contract_read(self, client):
        """The cheap pre-check must keep contract IO off the hot path."""
        tracker = _tracker()
        _propose(tracker)  # PROPOSED, not confirmed

        with (
            patch("routes.consensus.get_peer_consensus_tracker", return_value=tracker),
            patch("contract_completeness.load_live_contract") as load_mock,
        ):
            resp = client.post(
                f"/api/v1/pipelines/{PIPELINE_ID}/consensus/next-action",
                data=json.dumps({"role": "coder"}),
                content_type="application/json",
            )
        action, _ = _action(resp)
        assert action == "wait"
        load_mock.assert_not_called()


# ---------------------------------------------------------------------------
# signals: _existing_confirmed_for_role reopen-awareness
# ---------------------------------------------------------------------------


def _confirmed_msg(role: str, ts: datetime, *, pending: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        from_role=role,
        to_role="all",
        message_type="CONSENSUS_CONFIRMED",
        phase="implement",
        metadata={"pending_acks": True} if pending else {"consensus_reached": True},
        timestamp=ts,
    )


def _reopened_msg(role: str, ts: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        from_role="orchestrator",
        to_role=role,
        message_type="CONSENSUS_REOPENED",
        phase="implement",
        metadata={},
        timestamp=ts,
    )


class TestExistingConfirmedReopenAware:
    def _probe(self, messages):
        from routes.signals import _existing_confirmed_for_role

        store = MagicMock()
        store.get_messages.return_value = messages
        with patch("message_store.get_message_store", return_value=store):
            return _existing_confirmed_for_role("pid", "coder", "implement")

    def test_confirmed_before_reopen_is_stale(self):
        t0 = datetime.now(UTC)
        has_final, has_pending = self._probe(
            [
                _confirmed_msg("coder", t0),
                _reopened_msg("coder", t0 + timedelta(seconds=10)),
            ]
        )
        assert has_final is False
        assert has_pending is False

    def test_reconfirm_after_reopen_counts(self):
        t0 = datetime.now(UTC)
        has_final, _ = self._probe(
            [
                _confirmed_msg("coder", t0),
                _reopened_msg("coder", t0 + timedelta(seconds=10)),
                _confirmed_msg("coder", t0 + timedelta(seconds=20)),
            ]
        )
        assert has_final is True

    def test_reopen_for_other_role_does_not_invalidate(self):
        t0 = datetime.now(UTC)
        has_final, _ = self._probe(
            [
                _confirmed_msg("coder", t0),
                _reopened_msg("tester", t0 + timedelta(seconds=10)),
            ]
        )
        assert has_final is True

    def test_no_reopen_behaves_as_before(self):
        t0 = datetime.now(UTC)
        has_final, has_pending = self._probe(
            [
                _confirmed_msg("coder", t0),
                _confirmed_msg("coder", t0 + timedelta(seconds=1), pending=True),
            ]
        )
        assert has_final is True
        assert has_pending is True


# ---------------------------------------------------------------------------
# operator_actions.complete_task_as_operator
# ---------------------------------------------------------------------------


def _contract_dict() -> dict:
    return {
        "schemaVersion": "1.0",
        "pipeline_id": "pid-3124",
        "issue": {"number": 42, "title": "reopen test", "url": "http://example"},
        "phases": [
            {
                "id": "slice-1",
                "name": "only",
                "tasks": [
                    {
                        "id": "task-1-1",
                        "description": "pending coder work",
                        "role": "coder",
                        "status": "pending",
                    },
                ],
            },
        ],
    }


@pytest.fixture
def contract_worktree(tmp_path: Path) -> Path:
    contracts_dir = tmp_path / ".egg-state" / "contracts"
    contracts_dir.mkdir(parents=True)
    (contracts_dir / "pid-3124.json").write_text(json.dumps(_contract_dict()))
    return tmp_path


class TestOperatorCompleteTask:
    def test_completes_task_with_commit_evidence(self, contract_worktree: Path):
        from egg_contracts import load_contract
        from operator_actions import complete_task_as_operator

        with patch("contract_store.resolve_pipeline_worktree", return_value=contract_worktree):
            result = complete_task_as_operator(
                "pid-3124",
                "task-1-1",
                commit="c0ffee123",
                reason="confirmed-producer deadlock remediation",
                actor="operator:test",
            )

        assert result["status"] == "complete"
        assert result["prior_status"] == "pending"
        assert result["commit"] == "c0ffee123"

        contract = load_contract("pid-3124", contract_worktree)
        task = contract.slices[0].tasks[0]
        assert str(task.status) == "complete"
        assert task.commit == "c0ffee123"
        # Audited as the operator, not an agent role.
        audit_actors = {e.actor for e in contract.audit_log}
        assert "operator:test" in audit_actors

    def test_unknown_task_raises_404(self, contract_worktree: Path):
        from operator_actions import OperatorActionError, complete_task_as_operator

        with (
            patch("contract_store.resolve_pipeline_worktree", return_value=contract_worktree),
            pytest.raises(OperatorActionError) as exc_info,
        ):
            complete_task_as_operator("pid-3124", "task-9-9")
        assert exc_info.value.status_code == 404

    def test_missing_worktree_raises_404(self):
        from operator_actions import OperatorActionError, complete_task_as_operator

        with (
            patch("contract_store.resolve_pipeline_worktree", return_value=None),
            pytest.raises(OperatorActionError) as exc_info,
        ):
            complete_task_as_operator("pid-gone", "task-1-1")
        assert exc_info.value.status_code == 404


class TestOperatorCompleteTaskRoute:
    @pytest.fixture
    def route_client(self, monkeypatch):
        from flask import Flask
        from routes.contracts import contracts_bp

        monkeypatch.setenv("EGG_LIFECYCLE_SECRET", "test-secret")
        app = Flask(__name__)
        app.register_blueprint(contracts_bp)
        app.config["TESTING"] = True
        return app.test_client()

    def test_route_requires_lifecycle_secret(self, route_client):
        resp = route_client.post("/api/v1/contracts/pid-3124/tasks/task-1-1/complete")
        assert resp.status_code == 401

    def test_route_executes_operator_completion(self, route_client, contract_worktree):
        with patch("contract_store.resolve_pipeline_worktree", return_value=contract_worktree):
            resp = route_client.post(
                "/api/v1/contracts/pid-3124/tasks/task-1-1/complete",
                headers={"Authorization": "Bearer test-secret"},
                data=json.dumps({"commit": "c0ffee123", "reason": "operator attests"}),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.data
        data = json.loads(resp.data)
        assert data["data"]["status"] == "complete"
        assert data["data"]["task_id"] == "task-1-1"


# ---------------------------------------------------------------------------
# decisions: executable "Mark task <id> complete" resolution
# ---------------------------------------------------------------------------


class TestDecisionResolutionDispatch:
    def _dispatch(self, resolution, **patch_kwargs):
        from routes.decisions import _maybe_complete_task_from_resolution

        with patch("operator_actions.complete_task_as_operator", **patch_kwargs) as complete_mock:
            result = _maybe_complete_task_from_resolution("pid", "cq-3", resolution)
        return result, complete_mock

    def test_non_matching_resolution_is_ignored(self):
        result, complete_mock = self._dispatch("Cancel the slice and re-plan")
        assert result is None
        complete_mock.assert_not_called()

    def test_matching_resolution_executes_completion(self):
        result, complete_mock = self._dispatch(
            "Mark task TASK-2-3 complete",
            return_value={"task_id": "TASK-2-3", "status": "complete"},
        )
        assert result["success"] is True
        complete_mock.assert_called_once()
        assert complete_mock.call_args[0] == ("pid", "TASK-2-3")
        assert complete_mock.call_args[1]["commit"] is None

    def test_backticks_and_commit_evidence_parse(self):
        result, complete_mock = self._dispatch(
            "Mark task ``TASK-2-3`` complete, commit abc1234def",
            return_value={"task_id": "TASK-2-3", "status": "complete"},
        )
        assert result["success"] is True
        assert complete_mock.call_args[1]["commit"] == "abc1234def"

    def test_execution_failure_is_surfaced_not_silent(self):
        from operator_actions import OperatorActionError

        result, _ = self._dispatch(
            "Mark task TASK-2-3 complete",
            side_effect=OperatorActionError("contract gone", status_code=404),
        )
        assert result["success"] is False
        assert "contract gone" in result["error"]


# ---------------------------------------------------------------------------
# impasse escalation exposes the executable option
# ---------------------------------------------------------------------------


class TestImpasseEscalationOption:
    def test_escalation_with_task_offers_executable_completion(self):
        from egg_contracts.impasse import Impasse, ImpasseCategory
        from egg_contracts.models import Contract
        from impasse_routing import _build_hitl_decision

        contract = Contract.model_validate(_contract_dict())
        slice_obj = contract.slices[0]
        task = slice_obj.tasks[0]
        impasse = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="deliverable path is coder-owned per the file-restriction model",
            task_id=task.id,
            suggested_role="coder",
        )

        _field_path, decision = _build_hitl_decision(
            contract, slice_obj, task, impasse, "documenter", "delegation limit reached"
        )

        labels = [o.label for o in decision.options]
        assert f"Mark task {task.id} complete" in labels
        # The executable option precedes the catch-all "Other".
        assert labels[-1] == "Other (explain in reply)"

    def test_escalation_without_task_omits_completion_option(self):
        from egg_contracts.impasse import Impasse, ImpasseCategory
        from egg_contracts.models import Contract
        from impasse_routing import _build_hitl_decision

        contract = Contract.model_validate(_contract_dict())
        impasse = Impasse(
            category=ImpasseCategory.WRONG_ROLE,
            reason="could not resolve the task from the contract state",
        )

        _field_path, decision = _build_hitl_decision(
            contract, None, None, impasse, "documenter", "could not resolve task_id"
        )

        labels = [o.label for o in decision.options]
        assert not any(label.startswith("Mark task") for label in labels)
