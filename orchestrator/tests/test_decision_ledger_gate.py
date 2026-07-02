"""Tests for the gate-side decision-ledger backstop helpers (#3390).

``_collect_decision_ledger_status`` summarizes the phase's decision ledger
for the phase_gate surface: "N registered" (from the contract), "explicitly
none" (from a producer's ``no_decisions_rationale`` propose attestation), or
MISSING (neither — the backstop-HITL trigger). ``_find_explicit_none_
attestation`` is the message-store scan behind the explicit-none case.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from models import PipelinePhase


def _make_contract_file(
    repo: Path,
    identifier: str,
    *,
    decisions: list[dict[str, Any]] | None = None,
) -> Path:
    contracts_dir = repo / ".egg-state" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "pipeline_id": identifier,
        "issue": {"number": 42, "title": "t", "body": "b", "url": "https://example/42"},
        "pr": None,
        "branch": "egg/test",
        "current_phase": "refine",
        "phases": [],
        "decisions": decisions or [],
        "feedback": None,
        "audit_log": [],
        "agent_executions": [],
    }
    path = contracts_dir / f"{identifier}.json"
    path.write_text(json.dumps(payload))
    return path


def _decision(decision_id: str, *, phase: str = "refine", resolved: bool = False) -> dict:
    return {
        "id": decision_id,
        "question": f"Question for {decision_id}?",
        "type": "hitl",
        "phase": phase,
        "options": [],
        "resolved": resolved,
        "resolution": "done" if resolved else None,
        "resolved_by": "human" if resolved else None,
        "resolved_at": "2026-04-01T00:00:00+00:00" if resolved else None,
        "debounce_until": None,
    }


def _propose_message(
    *,
    from_role: str = "refiner",
    phase: str | None = "refine",
    attestation: dict | None = None,
    message_type: str | None = None,
):
    from message_store import Message, MessageType

    return Message(
        pipeline_id="pipeline-id",
        from_role=from_role,
        to_role="all",
        message_type=message_type or MessageType.CONSENSUS_PROPOSE,
        subject=f"Proposal from {from_role}",
        body="summary",
        phase=phase,
        metadata={"payload": {"summary": "s", "attestation": attestation or {}}},
    )


def _patch_message_store(messages):
    store = MagicMock()
    store.get_messages.return_value = list(messages)
    return patch("message_store.get_message_store", return_value=store)


class TestCollectDecisionLedgerStatus:
    def test_registered_decisions_counted(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        _make_contract_file(
            tmp_path,
            "issue-42",
            decisions=[
                _decision("cq-1"),
                _decision("cq-2", resolved=True),
                _decision("cq-3", phase="plan"),
            ],
        )
        note, missing = _collect_decision_ledger_status(
            tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
        )
        assert missing is False
        assert "2 decision(s) registered" in note
        assert "cq-1" in note and "cq-2" in note and "cq-3" not in note
        assert "1 resolved" in note

    def test_zero_registered_with_explicit_none_attestation(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        _make_contract_file(tmp_path, "issue-42", decisions=[])
        messages = [
            _propose_message(
                attestation={"no_decisions_rationale": "mechanical change, no choices"}
            )
        ]
        with _patch_message_store(messages):
            note, missing = _collect_decision_ledger_status(
                tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
            )
        assert missing is False
        assert "explicitly none" in note
        assert "refiner" in note
        assert "mechanical change" in note

    def test_zero_registered_no_attestation_is_missing(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        _make_contract_file(tmp_path, "issue-42", decisions=[])
        with _patch_message_store([]):
            note, missing = _collect_decision_ledger_status(
                tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
            )
        assert missing is True
        assert "MISSING" in note

    def test_contract_unloadable_falls_back_to_attestation(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        # No contract file written at all.
        messages = [_propose_message(attestation={"no_decisions_rationale": "none needed"})]
        with _patch_message_store(messages):
            note, missing = _collect_decision_ledger_status(
                tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
            )
        assert missing is False
        assert "explicitly none" in note

    def test_message_store_outage_fails_closed(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        _make_contract_file(tmp_path, "issue-42", decisions=[])
        with patch("message_store.get_message_store", side_effect=RuntimeError("redis down")):
            note, missing = _collect_decision_ledger_status(
                tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
            )
        assert missing is True
        assert "MISSING" in note


class TestFindExplicitNoneAttestation:
    def test_finds_rationale_from_propose(self):
        from routes.pipelines import _find_explicit_none_attestation

        messages = [
            _propose_message(attestation={"decisions_registered": ["cq-1"]}),
            _propose_message(
                from_role="risk_analyst",
                phase="plan",
                attestation={"no_decisions_rationale": "risk register raises none"},
            ),
        ]
        with _patch_message_store(messages):
            found = _find_explicit_none_attestation("pipeline-id", "plan")
        assert found == ("risk_analyst", "risk register raises none")

    def test_other_phase_attestation_ignored(self):
        from routes.pipelines import _find_explicit_none_attestation

        messages = [
            _propose_message(phase="refine", attestation={"no_decisions_rationale": "refine none"})
        ]
        with _patch_message_store(messages):
            assert _find_explicit_none_attestation("pipeline-id", "plan") is None

    def test_non_propose_messages_ignored(self):
        from message_store import MessageType
        from routes.pipelines import _find_explicit_none_attestation

        messages = [
            _propose_message(
                message_type=MessageType.CONSENSUS_ACK,
                attestation={"no_decisions_rationale": "not a propose"},
            )
        ]
        with _patch_message_store(messages):
            assert _find_explicit_none_attestation("pipeline-id", "refine") is None

    def test_registered_form_attestation_is_not_explicit_none(self):
        from routes.pipelines import _find_explicit_none_attestation

        messages = [_propose_message(attestation={"decisions_registered": ["cq-1"]})]
        with _patch_message_store(messages):
            assert _find_explicit_none_attestation("pipeline-id", "refine") is None
