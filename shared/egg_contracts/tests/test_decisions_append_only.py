"""Whole-entry ``decisions[]`` writes are append-only (#3427).

Every in-tree writer mints its target index as ``len(decisions)`` and
appends. A whole-entry write landing on an *existing* index means the
caller minted against a stale read (a TOCTOU race across writers) and
would silently replace another writer's decision — including a resolved
one, erasing the operator's answer (the #3427 clobber). ``apply_mutation``
must reject such writes with ``error_kind="conflict"`` so the caller
re-reads the contract and re-mints; sub-field writes and true appends are
unaffected.
"""

from __future__ import annotations

from egg_contracts.models import (
    Contract,
    Decision,
    DecisionType,
    IssueInfo,
    PipelinePhase,
)
from egg_contracts.roles import Role
from egg_contracts.validator import apply_mutation


def _contract(decisions: list[Decision] | None = None) -> Contract:
    return Contract(
        schemaVersion="1.2",
        issue=IssueInfo(number=3427, title="#3427", url=""),
        pipeline_id="issue-3427",
        current_phase=PipelinePhase.IMPLEMENT,
        slices=[],
        decisions=decisions or [],
    )


def _decision(id_: str, *, resolved: bool = False) -> Decision:
    return Decision(
        id=id_,
        question=f"question for {id_}",
        type=DecisionType.HITL,
        phase=PipelinePhase.IMPLEMENT,
        resolved=resolved,
        resolution="answered" if resolved else None,
    )


def _new_entry(id_: str) -> dict:
    """The gateway JSON shape ``register_open_question`` sends."""
    return {
        "id": id_,
        "question": "a freshly minted question",
        "type": "hitl",
        "phase": "implement",
        "options": [],
        "resolved": False,
        "resolution": None,
    }


class TestDecisionsAppendOnly:
    def test_overwrite_existing_index_rejected_as_conflict(self) -> None:
        contract = _contract([_decision("cq-1")])
        result = apply_mutation(
            contract,
            role=Role.IMPLEMENTER,
            actor="agent",
            field_path="decisions.0",
            new_value=_new_entry("cq-1"),
        )
        assert result.success is False
        assert result.error_kind == "conflict"
        assert "already exists" in result.message
        # The existing entry must be untouched.
        assert contract.decisions[0].question == "question for cq-1"

    def test_overwrite_of_resolved_entry_flags_resolved(self) -> None:
        contract = _contract([_decision("cq-1", resolved=True)])
        result = apply_mutation(
            contract,
            role=Role.IMPLEMENTER,
            actor="agent",
            field_path="decisions.0",
            new_value=_new_entry("cq-2"),
        )
        assert result.success is False
        assert result.error_kind == "conflict"
        assert "(resolved)" in result.message
        assert contract.decisions[0].resolved is True
        assert contract.decisions[0].resolution == "answered"

    def test_append_at_len_still_succeeds(self) -> None:
        contract = _contract([_decision("cq-1")])
        result = apply_mutation(
            contract,
            role=Role.IMPLEMENTER,
            actor="agent",
            field_path="decisions.1",
            new_value=_new_entry("cq-2"),
        )
        assert result.success is True, result.message
        assert len(contract.decisions) == 2

    def test_past_the_end_index_remains_value_error(self) -> None:
        """Indices beyond ``len`` are not conflicts — they stay the
        pre-existing out-of-range ``value`` rejection.
        """
        contract = _contract([_decision("cq-1")])
        result = apply_mutation(
            contract,
            role=Role.IMPLEMENTER,
            actor="agent",
            field_path="decisions.5",
            new_value=_new_entry("cq-2"),
        )
        assert result.success is False
        assert result.error_kind == "value"

    def test_subfield_write_on_existing_entry_not_blocked(self) -> None:
        """Resolution sub-field writes (``decisions.N.<field>``, HUMAN-owned)
        are not whole-entry writes and must keep working.
        """
        contract = _contract([_decision("cq-1")])
        result = apply_mutation(
            contract,
            role=Role.HUMAN,
            actor="operator",
            field_path="decisions.0.resolved",
            new_value=True,
        )
        assert result.success is True, result.message
        assert contract.decisions[0].resolved is True
