"""Tests for the run-loop unresolved-gap finalize gate (#3300).

The implement phase is not in ``_HITL_GATE_PHASES``, so the autonomous
run loop would mark it complete and finalize with an open tester→coder
``TaskGap`` still on the contract — shipping it into the committed
contract and failing ``test_models_gaps.py`` red in CI on the
already-open PR (#3298 class 4). ``_await_unresolved_gap_gate`` surfaces
a blocking ``phase_gate`` decision and waits for the operator to resolve
the gap or explicitly override.

See: https://github.com/jwbron/egg/issues/3300
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from models import DecisionStatus, HITLDecision, Pipeline, PipelinePhase, PipelineStatus


def _contract(*, resolved: bool):
    """Build a Contract carrying a single tester→coder gap."""
    from egg_contracts.models import Contract

    return Contract(
        pipeline_id="issue-42",
        slices=[
            {
                "id": "slice-1",
                "name": "n",
                "tasks": [
                    {
                        "id": "task-1-2",
                        "description": "d",
                        "gaps": [
                            {
                                "id": "gap-1",
                                "from_role": "tester",
                                "to_role": "coder",
                                "description": "no error-path test",
                                "resolved": resolved,
                            }
                        ],
                    }
                ],
            }
        ],
    )


class _FakeQueue:
    """Resolves each queued decision with the next caller-provided value.

    A ``None`` resolution entry yields a still-PENDING decision (models a
    cancelled / abandoned gate).
    """

    def __init__(self, resolutions: list[str | None]) -> None:
        self._resolutions = list(resolutions)
        self.queued: list[HITLDecision] = []
        self._counter = 0

    def queue_decision(
        self,
        question: str,
        context: str = "",
        options: list[str] | None = None,
        decision_type: str = "choice",
        questions: list[dict[str, str]] | None = None,
        phase: PipelinePhase | None = None,
        content_changed: bool | None = None,
    ) -> HITLDecision:
        self._counter += 1
        res = self._resolutions.pop(0) if self._resolutions else ""
        decision = HITLDecision(
            id=f"orch-{self._counter}",
            question=question,
            context=context,
            options=options or [],
            decision_type=decision_type,  # type: ignore[arg-type]
            questions=questions or [],
            phase=phase,
            status=DecisionStatus.PENDING if res is None else DecisionStatus.RESOLVED,
            resolution=None if res is None else res,
        )
        self.queued.append(decision)
        return decision

    def wait_for_decision(self, decision_id: str) -> HITLDecision:
        for d in self.queued:
            if d.id == decision_id:
                return d
        raise AssertionError(f"decision {decision_id} not queued")


def _run_gate(dq: _FakeQueue, load_side_effect: list[Any]) -> tuple[bool, Pipeline]:
    """Invoke the gate with the queue + a scripted load_contract sequence."""
    from routes.pipelines import _await_unresolved_gap_gate

    pipeline = Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
    )
    pipeline.current_phase = PipelinePhase.IMPLEMENT
    store = MagicMock()
    store.load_pipeline.return_value = pipeline

    with (
        patch("routes.pipelines.get_decision_queue", return_value=dq),
        patch("routes.pipelines.get_pipeline_state_lock", return_value=nullcontext()),
        patch("routes.pipelines.report_pipeline_status"),
        patch("routes.pipelines._emit_event", None),
        patch("egg_contracts.loader.load_contract", side_effect=load_side_effect),
    ):
        gated = _await_unresolved_gap_gate(
            store,
            "issue-42",
            Path("/repo"),
            Path("/worktree"),
            42,
            PipelinePhase.IMPLEMENT,
        )
    return gated, pipeline


def test_clean_contract_no_gate() -> None:
    """A resolved gap (or no gap) must not surface a decision."""
    dq = _FakeQueue(resolutions=[])
    gated, _ = _run_gate(dq, load_side_effect=[_contract(resolved=True)])
    assert gated is False
    assert dq.queued == []


def test_open_gap_override_finalizes() -> None:
    """An open gap surfaces a phase_gate decision; 'override' ships it."""
    dq = _FakeQueue(resolutions=["override"])
    gated, pipeline = _run_gate(dq, load_side_effect=[_contract(resolved=False)])
    assert gated is True
    assert len(dq.queued) == 1
    assert dq.queued[0].decision_type == "phase_gate"
    assert dq.queued[0].options == ["approve", "override"]
    assert "task-1-2" in dq.queued[0].context
    # Status restored to RUNNING after the gate clears.
    assert pipeline.status == PipelineStatus.RUNNING


def test_open_gap_approve_then_clear() -> None:
    """Approval after the operator resolves the gap clears the gate with a
    single prompt (re-read finds the contract clean)."""
    dq = _FakeQueue(resolutions=["approve"])
    gated, _ = _run_gate(
        dq,
        load_side_effect=[_contract(resolved=False), _contract(resolved=True)],
    )
    assert gated is True
    assert len(dq.queued) == 1


def test_open_gap_approve_without_resolving_resurfaces() -> None:
    """A stale 'approve' while the gap is still open must NOT advance — the
    gate re-surfaces until resolved or overridden."""
    dq = _FakeQueue(resolutions=["approve", "override"])
    gated, _ = _run_gate(
        dq,
        load_side_effect=[_contract(resolved=False), _contract(resolved=False)],
    )
    assert gated is True
    # Two prompts: the ignored approve, then the override that ships it.
    assert len(dq.queued) == 2


def test_unresolved_decision_does_not_spin() -> None:
    """A cancelled/abandoned gate (non-RESOLVED) returns instead of looping."""
    dq = _FakeQueue(resolutions=[None])
    gated, _ = _run_gate(dq, load_side_effect=[_contract(resolved=False)])
    assert gated is True
    assert len(dq.queued) == 1
