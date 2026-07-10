"""Tests for the gate-side decision-ledger backstop helpers (#3390, #3462).

``_collect_decision_ledger_status`` summarizes the phase's decision ledger
for the phase_gate surface: "N registered" (from the contract), "explicitly
none" (from a producer's ``no_decisions_rationale`` propose attestation), or
MISSING (neither — the backstop-HITL trigger). ``_find_explicit_none_
attestation`` is the message-store scan behind the explicit-none case.
``_ledger_attestation_question`` / ``_ledger_attestation_confirmed`` back the
explicit-none confirmation decision the gate surfaces to the operator
(#3462).
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
        note, missing, explicit_none, summary = _collect_decision_ledger_status(
            tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
        )
        assert missing is False
        assert explicit_none is None
        assert "2 decision(s) registered" in note
        assert "cq-1" in note and "cq-2" in note and "cq-3" not in note
        assert "1 resolved" in note
        assert summary == {
            "registered": ["cq-1", "cq-2"],
            "resolved": 1,
            "explicit_none": False,
            "attested_by": None,
            "missing": False,
            "candidates_considered": [],
        }

    def test_zero_registered_with_explicit_none_attestation(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        _make_contract_file(tmp_path, "issue-42", decisions=[])
        messages = [
            _propose_message(
                attestation={"no_decisions_rationale": "mechanical change, no choices"}
            )
        ]
        with _patch_message_store(messages):
            note, missing, explicit_none, summary = _collect_decision_ledger_status(
                tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
            )
        assert missing is False
        assert explicit_none == ("refiner", "mechanical change, no choices", [])
        assert "explicitly none" in note
        assert "refiner" in note
        assert "mechanical change" in note
        assert summary["explicit_none"] is True
        assert summary["registered"] == []
        assert summary["attested_by"] == "refiner"
        # Uniform shape across branches (#3526 review): every key present.
        assert summary["missing"] is False
        assert set(summary) == {
            "registered",
            "resolved",
            "explicit_none",
            "attested_by",
            "missing",
            "candidates_considered",
        }

    def test_zero_registered_no_attestation_is_missing(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        _make_contract_file(tmp_path, "issue-42", decisions=[])
        with _patch_message_store([]):
            note, missing, explicit_none, summary = _collect_decision_ledger_status(
                tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
            )
        assert missing is True
        assert explicit_none is None
        assert "MISSING" in note
        assert summary["missing"] is True
        # Uniform shape across branches (#3526 review): every key present.
        assert summary["attested_by"] is None
        assert set(summary) == {
            "registered",
            "resolved",
            "explicit_none",
            "attested_by",
            "missing",
            "candidates_considered",
        }

    def test_contract_unloadable_falls_back_to_attestation(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        # No contract file written at all.
        messages = [_propose_message(attestation={"no_decisions_rationale": "none needed"})]
        with _patch_message_store(messages):
            note, missing, explicit_none, _summary = _collect_decision_ledger_status(
                tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
            )
        assert missing is False
        assert explicit_none == ("refiner", "none needed", [])
        assert "explicitly none" in note

    def test_message_store_outage_fails_closed(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        _make_contract_file(tmp_path, "issue-42", decisions=[])
        with patch("message_store.get_message_store", side_effect=RuntimeError("redis down")):
            note, missing, explicit_none, _summary = _collect_decision_ledger_status(
                tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
            )
        assert missing is True
        assert explicit_none is None
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
        assert found == ("risk_analyst", "risk register raises none", [])

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


class TestLedgerAttestationConfirmation:
    """The explicit-none confirmation decision surface (#3462).

    An explicit-none attestation is surfaced to the operator as its own
    confirmable decision. ``_ledger_attestation_question`` composes it;
    ``_ledger_attestation_confirmed`` decides whether a resolution
    confirms (proceed to the gate) or rejects (re-run the phase).
    """

    def test_question_quotes_role_phase_and_rationale(self):
        from routes.pipelines import _ledger_attestation_question

        question = _ledger_attestation_question(
            "refiner", "answers present in seeded context", "refine"
        )
        assert "refiner" in question
        assert "refine phase" in question
        assert "answers present in seeded context" in question
        assert "#3462" in question

    def test_question_is_stable_for_identical_claims(self):
        # Converge-round dedupe keys on question equality: the same
        # (role, rationale, phase) must compose the identical question.
        from routes.pipelines import _ledger_attestation_question

        a = _ledger_attestation_question("refiner", "no choices", "refine")
        b = _ledger_attestation_question("refiner", "no choices", "refine")
        assert a == b
        assert a != _ledger_attestation_question("refiner", "other claim", "refine")

    def test_bare_confirm_keyword_confirms(self):
        from routes.pipelines import _ledger_attestation_confirmed

        assert _ledger_attestation_confirmed("confirm") is True
        assert _ledger_attestation_confirmed("  Confirm  ") is True

    def test_full_option_label_confirms(self):
        from routes.pipelines import (
            _LEDGER_ATTESTATION_CONFIRM_OPTION,
            _ledger_attestation_confirmed,
        )

        assert _ledger_attestation_confirmed(_LEDGER_ATTESTATION_CONFIRM_OPTION) is True
        assert _ledger_attestation_confirmed(_LEDGER_ATTESTATION_CONFIRM_OPTION.upper()) is True

    def test_choice_envelope_unwrapped(self):
        # The SDLC HITL CLI resolves a choice as
        # {"action": "select", "selected": "<label>"} (#2978) — the
        # confirm match must unwrap it.
        from routes.pipelines import (
            _LEDGER_ATTESTATION_CONFIRM_OPTION,
            _ledger_attestation_confirmed,
        )

        envelope = json.dumps({"action": "select", "selected": _LEDGER_ATTESTATION_CONFIRM_OPTION})
        assert _ledger_attestation_confirmed(envelope) is True

    def test_rerun_option_and_free_text_reject(self):
        from routes.pipelines import (
            _LEDGER_BACKSTOP_RERUN_OPTION,
            _ledger_attestation_confirmed,
        )

        assert _ledger_attestation_confirmed(_LEDGER_BACKSTOP_RERUN_OPTION) is False
        # Free text is a re-run directive, even when it contains the
        # word "confirm" in a negating sense.
        assert _ledger_attestation_confirmed("do not confirm — register cq for deploy") is False
        assert _ledger_attestation_confirmed("") is False

    def test_envelope_with_rerun_selection_rejects(self):
        from routes.pipelines import (
            _LEDGER_BACKSTOP_RERUN_OPTION,
            _ledger_attestation_confirmed,
        )

        envelope = json.dumps({"action": "select", "selected": _LEDGER_BACKSTOP_RERUN_OPTION})
        assert _ledger_attestation_confirmed(envelope) is False


class TestLedgerAttestationRerunDirective:
    """The re-run directive built when an explicit-none attestation is
    rejected (#3462). Free-text resolutions ride along as an operator note;
    the bare re-run option does not.
    """

    def test_directive_names_phase_rationale_and_registration_rule(self):
        from routes.pipelines import _ledger_attestation_rerun_directive

        directive = _ledger_attestation_rerun_directive(
            "refine", "no choices this phase", "Re-run phase to register decisions"
        )
        assert "refine phase" in directive
        assert "no choices this phase" in directive
        assert "egg-contract add-decision" in directive
        assert "recommended disposition" in directive

    def test_bare_rerun_option_adds_no_operator_note(self):
        from routes.pipelines import (
            _LEDGER_BACKSTOP_RERUN_OPTION,
            _ledger_attestation_rerun_directive,
        )

        directive = _ledger_attestation_rerun_directive(
            "plan", "nothing to decide", _LEDGER_BACKSTOP_RERUN_OPTION
        )
        assert "Operator note:" not in directive

    def test_free_text_resolution_rides_along_as_operator_note(self):
        from routes.pipelines import _ledger_attestation_rerun_directive

        directive = _ledger_attestation_rerun_directive(
            "plan", "nothing to decide", "register the deploy-target choice"
        )
        assert "Operator note: register the deploy-target choice" in directive


class TestHandleExplicitNoneAttestationGate:
    """The orchestration around the explicit-none confirmation decision (#3462).

    ``_handle_explicit_none_attestation_gate`` queues the confirmable choice,
    waits on it, and either falls through to the phase gate (confirm / cancel
    fail-open) or re-runs the phase (reject). The dedup branches — a prior
    confirmation reused without re-asking, and a pending decision reused
    without re-emitting ``decision.created`` — are the net-new integration
    logic this covers.
    """

    _ROLE = "refiner"
    _RATIONALE = "requirements are unambiguous; no operator choices"

    def _fake_phase_execution(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            status=None,
            completed_at="2026-04-01T00:00:00+00:00",
            hitl_review_cycles=0,
        )

    def _fake_pipeline(self, decisions=None):
        from types import SimpleNamespace

        phase_exec = self._fake_phase_execution()
        return SimpleNamespace(
            decisions=list(decisions or []),
            status=None,
            config=SimpleNamespace(max_hitl_review_cycles=3),
            get_phase_execution=lambda _phase: phase_exec,
        )

    def _choice_decision(self, question, status, resolution=None):
        from types import SimpleNamespace

        from models import PipelinePhase

        return SimpleNamespace(
            decision_type="choice",
            phase=PipelinePhase.REFINE,
            question=question,
            status=status,
            resolution=resolution,
        )

    def _call(self, *, pipeline, dq, store=None, spawner=None):
        """Invoke the helper with all module-level collaborators patched."""
        from unittest.mock import MagicMock, patch

        from models import PipelinePhase
        from routes.pipelines import _handle_explicit_none_attestation_gate

        store = store or MagicMock()
        store.load_pipeline.return_value = pipeline
        spawner = spawner or MagicMock()

        with (
            patch("routes.pipelines.get_decision_queue", return_value=dq) as m_get_dq,
            patch("routes.pipelines.get_pipeline_state_lock", return_value=MagicMock()),
            patch("routes.pipelines.report_pipeline_status") as m_report,
            patch("routes.pipelines._emit_pipeline_event") as m_emit,
            patch("routes.pipelines._perform_hitl_phase_rerun") as m_rerun,
            patch("routes.pipelines._broadcast_hitl_nonconvergence_alert") as m_alert,
        ):
            rerun_requested, note, out_pipeline = _handle_explicit_none_attestation_gate(
                pipeline=pipeline,
                pipeline_id="pipeline-id",
                repo_path=Path("/tmp/repo"),
                current_phase=PipelinePhase.REFINE,
                ledger_note="Decision ledger: explicitly none — refiner attested: x",
                explicit_none=(self._ROLE, self._RATIONALE, []),
                store=store,
                spawner=spawner,
            )
        return {
            "rerun_requested": rerun_requested,
            "note": note,
            "pipeline": out_pipeline,
            "get_dq": m_get_dq,
            "report": m_report,
            "emit": m_emit,
            "rerun": m_rerun,
            "alert": m_alert,
            "store": store,
        }

    def _question(self):
        from models import PipelinePhase
        from routes.pipelines import _ledger_attestation_question

        return _ledger_attestation_question(self._ROLE, self._RATIONALE, PipelinePhase.REFINE.value)

    def test_confirm_falls_through_with_note(self):
        from unittest.mock import MagicMock

        from models import DecisionStatus

        dq = MagicMock()
        dq.queue_decision.return_value = MagicMock(id="attest-1")
        dq.wait_for_decision.return_value = MagicMock(
            status=DecisionStatus.RESOLVED, resolution="confirm"
        )
        pipeline = self._fake_pipeline()

        res = self._call(pipeline=pipeline, dq=dq)

        assert res["rerun_requested"] is False
        assert res["note"].endswith("Operator confirmed the attestation.")
        dq.queue_decision.assert_called_once()
        # A freshly-created decision is announced exactly once.
        assert res["emit"].call_count == 1
        res["rerun"].assert_not_called()

    def test_reject_rerun_option_triggers_phase_rerun(self):
        from unittest.mock import MagicMock

        from models import DecisionStatus, PipelineStatus
        from routes.pipelines import _LEDGER_BACKSTOP_RERUN_OPTION

        dq = MagicMock()
        dq.queue_decision.return_value = MagicMock(id="attest-1")
        dq.wait_for_decision.return_value = MagicMock(
            status=DecisionStatus.RESOLVED, resolution=_LEDGER_BACKSTOP_RERUN_OPTION
        )
        pipeline = self._fake_pipeline()

        res = self._call(pipeline=pipeline, dq=dq)

        assert res["rerun_requested"] is True
        res["rerun"].assert_called_once()
        directive = res["rerun"].call_args.kwargs["feedback_text"]
        assert "egg-contract add-decision" in directive
        assert "Operator note:" not in directive
        assert pipeline.status == PipelineStatus.RUNNING

    def test_reject_free_text_forwards_operator_note(self):
        from unittest.mock import MagicMock

        from models import DecisionStatus

        dq = MagicMock()
        dq.queue_decision.return_value = MagicMock(id="attest-1")
        dq.wait_for_decision.return_value = MagicMock(
            status=DecisionStatus.RESOLVED,
            resolution="you skipped the deploy-target choice",
        )
        pipeline = self._fake_pipeline()

        res = self._call(pipeline=pipeline, dq=dq)

        assert res["rerun_requested"] is True
        directive = res["rerun"].call_args.kwargs["feedback_text"]
        assert "Operator note: you skipped the deploy-target choice" in directive

    def test_prior_confirmation_is_reused_without_reasking(self):
        from unittest.mock import MagicMock

        from models import DecisionStatus

        prior = self._choice_decision(
            self._question(), DecisionStatus.RESOLVED, resolution="confirm"
        )
        pipeline = self._fake_pipeline(decisions=[prior])
        dq = MagicMock()

        res = self._call(pipeline=pipeline, dq=dq)

        assert res["rerun_requested"] is False
        assert res["note"].endswith("Operator confirmed the attestation.")
        # No new decision is queued and the queue is never even fetched.
        res["get_dq"].assert_not_called()
        res["emit"].assert_not_called()
        res["rerun"].assert_not_called()

    def test_pending_decision_reused_without_reemitting_created(self):
        from unittest.mock import MagicMock

        from models import DecisionStatus

        pending = self._choice_decision(self._question(), DecisionStatus.PENDING)
        pending.id = "attest-pending"
        pipeline = self._fake_pipeline(decisions=[pending])
        dq = MagicMock()
        dq.wait_for_decision.return_value = MagicMock(
            status=DecisionStatus.RESOLVED, resolution="confirm"
        )

        res = self._call(pipeline=pipeline, dq=dq)

        # The pending decision is reused, not re-queued...
        dq.queue_decision.assert_not_called()
        dq.wait_for_decision.assert_called_once_with("attest-pending")
        # ...and reusing it must not re-announce decision.created.
        res["emit"].assert_not_called()
        assert res["rerun_requested"] is False

    def test_cancel_fails_open_with_accurate_note(self):
        from unittest.mock import MagicMock

        from models import DecisionStatus

        dq = MagicMock()
        dq.queue_decision.return_value = MagicMock(id="attest-1")
        dq.wait_for_decision.return_value = MagicMock(
            status=DecisionStatus.CANCELLED, resolution=None
        )
        pipeline = self._fake_pipeline()

        res = self._call(pipeline=pipeline, dq=dq)

        # Fail open to the phase gate, but record the cancel accurately —
        # never claim a confirmation the operator did not give (#3462 review).
        assert res["rerun_requested"] is False
        assert "Operator confirmed the attestation." not in res["note"]
        assert "cancelled" in res["note"].lower()
        res["rerun"].assert_not_called()


class TestConsideredCandidates:
    """The structured considered-candidate surface (#3526).

    Explicit-none attestations enumerate the candidates they weighed;
    the gate renders them on the confirmation question, and refine's
    ``deferred_to_plan`` candidates are recoverable for the plan-phase
    handoff.
    """

    _CANDIDATES = [
        {
            "question": "Should the fallback cache be enabled by default?",
            "disposition": "deferred_to_plan",
            "why": "depends on the plan's storage design",
        },
        {
            "question": "Which retry helper to reuse?",
            "disposition": "not_operator_grade",
            "why": "internal design call",
        },
    ]

    def test_find_explicit_none_returns_candidates(self):
        from routes.pipelines import _find_explicit_none_attestation

        messages = [
            _propose_message(
                attestation={
                    "no_decisions_rationale": "prescriptive task",
                    "candidates_considered": self._CANDIDATES,
                }
            )
        ]
        with _patch_message_store(messages):
            found = _find_explicit_none_attestation("pipeline-id", "refine")
        assert found == ("refiner", "prescriptive task", self._CANDIDATES)

    def test_attestation_question_renders_candidates(self):
        from routes.pipelines import _ledger_attestation_question

        question = _ledger_attestation_question(
            "refiner", "prescriptive task", "refine", self._CANDIDATES
        )
        assert "Should the fallback cache be enabled by default?" in question
        assert "deferred to plan" in question
        assert "Which retry helper to reuse?" in question
        assert "not_operator_grade" in question

    def test_attestation_question_without_candidates_unchanged_shape(self):
        # Back-compat: pre-#3526 attestations (no candidates) still compose
        # a stable question so converge-round dedupe keeps keying on it.
        from routes.pipelines import _ledger_attestation_question

        a = _ledger_attestation_question("refiner", "no choices", "refine")
        b = _ledger_attestation_question("refiner", "no choices", "refine", [])
        assert a == b

    def test_ledger_note_counts_candidates(self, tmp_path: Path):
        from routes.pipelines import _collect_decision_ledger_status

        _make_contract_file(tmp_path, "issue-42", decisions=[])
        messages = [
            _propose_message(
                attestation={
                    "no_decisions_rationale": "prescriptive task",
                    "candidates_considered": self._CANDIDATES,
                }
            )
        ]
        with _patch_message_store(messages):
            note, _missing, explicit_none, summary = _collect_decision_ledger_status(
                tmp_path, "pipeline-id", "issue-42", PipelinePhase.REFINE
            )
        assert "2 candidate(s) considered" in note
        assert explicit_none == ("refiner", "prescriptive task", self._CANDIDATES)
        assert summary["candidates_considered"] == self._CANDIDATES

    def test_deferred_plan_candidates_found_from_refine_propose(self):
        from routes.pipelines import _find_deferred_plan_candidates

        messages = [
            _propose_message(
                phase="refine",
                attestation={
                    "decisions_registered": ["cq-1"],
                    "candidates_considered": self._CANDIDATES,
                },
            )
        ]
        with _patch_message_store(messages):
            deferred = _find_deferred_plan_candidates("pipeline-id")
        assert deferred == [self._CANDIDATES[0]]

    def test_deferred_candidates_ignore_plan_phase_proposals(self):
        from routes.pipelines import _find_deferred_plan_candidates

        messages = [
            _propose_message(
                phase="plan",
                from_role="architect",
                attestation={
                    "no_decisions_rationale": "x",
                    "candidates_considered": self._CANDIDATES,
                },
            )
        ]
        with _patch_message_store(messages):
            assert _find_deferred_plan_candidates("pipeline-id") == []

    def test_deferred_candidates_empty_on_store_outage(self):
        from routes.pipelines import _find_deferred_plan_candidates

        with patch("message_store.get_message_store", side_effect=RuntimeError("down")):
            assert _find_deferred_plan_candidates("pipeline-id") == []


class TestDeferredCandidatesPromptSection:
    """The plan-prompt injection of refine deferrals (#3526)."""

    _DEFERRED = [
        {
            "question": "Should the fallback cache be enabled by default?",
            "disposition": "deferred_to_plan",
            "why": "depends on the plan's storage design",
        }
    ]

    def test_section_renders_deferred_candidates(self):
        from routes.pipelines import _build_deferred_candidates_section

        with patch("routes.pipelines._find_deferred_plan_candidates", return_value=self._DEFERRED):
            section = _build_deferred_candidates_section("pipeline-id")
        text = "\n".join(section)
        assert "Deferred from refine" in text
        assert "Should the fallback cache be enabled by default?" in text
        assert "egg-contract add-decision" in text

    def test_section_renders_stable_dq_ids_and_echo_contract(self):
        # #3564: each candidate carries the content-derived dq-<hash> id the
        # propose-time coverage gate recomputes, plus the --deferred echo
        # syntax the producer must use. The rendered id and the gate's id
        # must come from the same helper or the exact match breaks.
        from egg_contracts.decisions import deferred_question_id
        from routes.pipelines import _build_deferred_candidates_section

        with patch("routes.pipelines._find_deferred_plan_candidates", return_value=self._DEFERRED):
            section = _build_deferred_candidates_section("pipeline-id")
        text = "\n".join(section)
        assert deferred_question_id(self._DEFERRED[0]["question"]) in text
        assert "--deferred" in text
        assert "registered :: cq-" in text
        assert "not_operator_grade" in text

    def test_section_empty_without_deferrals(self):
        from routes.pipelines import _build_deferred_candidates_section

        with patch("routes.pipelines._find_deferred_plan_candidates", return_value=[]):
            assert _build_deferred_candidates_section("pipeline-id") == []

    def test_section_empty_without_pipeline_id(self):
        from routes.pipelines import _build_deferred_candidates_section

        assert _build_deferred_candidates_section(None) == []

    def test_plan_phase_prompt_includes_deferred_section(self):
        from routes.pipelines import _build_phase_prompt

        with patch("routes.pipelines._find_deferred_plan_candidates", return_value=self._DEFERRED):
            prompt = _build_phase_prompt(
                phase="plan",
                pipeline_id="pipeline-id",
                pipeline_mode="issue",
                issue_number=42,
            )
        assert "Deferred from refine" in prompt
        assert "Should the fallback cache be enabled by default?" in prompt
        assert "Operator Decisions (plan phase)" in prompt

    def test_plan_phase_prompt_without_deferrals_has_protocol_only(self):
        from routes.pipelines import _build_phase_prompt

        with patch("routes.pipelines._find_deferred_plan_candidates", return_value=[]):
            prompt = _build_phase_prompt(
                phase="plan",
                pipeline_id="pipeline-id",
                pipeline_mode="issue",
                issue_number=42,
            )
        assert "Deferred from refine" not in prompt
        assert "Operator Decisions (plan phase)" in prompt
        assert "egg-contract add-decision" in prompt


class TestPersistDecisionLedgerSummary:
    """Gate-time persistence of the ledger summary (#3526)."""

    def test_summary_persisted_on_phase_execution(self):
        from unittest.mock import MagicMock

        from routes.pipelines import _persist_decision_ledger_summary

        phase_exec = MagicMock()
        pipeline = MagicMock()
        pipeline.get_phase_execution.return_value = phase_exec
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        summary = {"registered": ["cq-1"], "resolved": 0, "explicit_none": False}

        with patch("routes.pipelines.get_pipeline_state_lock", return_value=MagicMock()):
            out = _persist_decision_ledger_summary(
                store, "pipeline-id", PipelinePhase.REFINE, summary
            )

        assert out is pipeline
        assert phase_exec.decision_ledger == summary
        store.save_pipeline.assert_called_once_with(pipeline)

    def test_persistence_failure_is_non_blocking(self):
        from unittest.mock import MagicMock

        from routes.pipelines import _persist_decision_ledger_summary

        store = MagicMock()
        store.load_pipeline.side_effect = RuntimeError("state store down")

        with patch("routes.pipelines.get_pipeline_state_lock", return_value=MagicMock()):
            out = _persist_decision_ledger_summary(
                store, "pipeline-id", PipelinePhase.REFINE, {"registered": []}
            )

        assert out is None
