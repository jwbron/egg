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


def _run_gate(
    dq: _FakeQueue,
    load_side_effect: list[Any],
    *,
    hitl_gates: bool = True,
) -> tuple[bool, Pipeline, MagicMock]:
    """Invoke the gate with the queue + a scripted load_contract sequence.

    Returns ``(gated, pipeline, report_mock)`` — ``report_mock`` is the
    patched ``report_pipeline_status`` so callers can assert the
    escalation was surfaced.
    """
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
        patch("routes.pipelines.report_pipeline_status") as report_mock,
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
            hitl_gates,
        )
    return gated, pipeline, report_mock


def test_clean_contract_no_gate() -> None:
    """A resolved gap (or no gap) must not surface a decision."""
    dq = _FakeQueue(resolutions=[])
    gated, _, _ = _run_gate(dq, load_side_effect=[_contract(resolved=True)])
    assert gated is False
    assert dq.queued == []


def test_open_gap_override_finalizes() -> None:
    """An open gap surfaces a phase_gate decision; 'override' ships it and
    records the override on the frozen phase artifacts for audit parity
    with the complete_phase endpoint's force path (#3300 review)."""
    dq = _FakeQueue(resolutions=["override"])
    gated, pipeline, _ = _run_gate(dq, load_side_effect=[_contract(resolved=False)])
    assert gated is True
    assert len(dq.queued) == 1
    assert dq.queued[0].decision_type == "phase_gate"
    assert dq.queued[0].options == ["approve", "override"]
    assert "task-1-2" in dq.queued[0].context
    # Status restored to RUNNING after the gate clears.
    assert pipeline.status == PipelineStatus.RUNNING
    # Durable override audit on the phase box, not just a log line.
    import json

    phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
    assert phase_exec.artifacts["force_completed_gaps"] == json.dumps(["task-1-2/gap-1"])


def test_open_gap_marks_phase_box_awaiting_human() -> None:
    """While blocking, both the pipeline and the implement phase box must
    read AWAITING_HUMAN so the DAG renders the gate on the right phase
    (#3300 review). A FakeQueue captures status at queue time."""
    captured: dict[str, Any] = {}

    class _CapturingQueue(_FakeQueue):
        def __init__(self, pipeline_ref: list[Pipeline], resolutions):
            super().__init__(resolutions)
            self._pipeline_ref = pipeline_ref

        def wait_for_decision(self, decision_id: str):
            # Status is set to AWAITING_HUMAN just before the wait.
            pl = self._pipeline_ref[0]
            captured["pipeline"] = pl.status
            captured["phase"] = pl.get_phase_execution(PipelinePhase.IMPLEMENT).status
            return super().wait_for_decision(decision_id)

    pipeline_ref: list[Pipeline] = []
    dq = _CapturingQueue(pipeline_ref, resolutions=["override"])

    from routes.pipelines import _await_unresolved_gap_gate

    pipeline = Pipeline(id="issue-42", issue_number=42, repo="owner/repo", branch="egg/issue-42")
    pipeline.current_phase = PipelinePhase.IMPLEMENT
    pipeline_ref.append(pipeline)
    store = MagicMock()
    store.load_pipeline.return_value = pipeline

    with (
        patch("routes.pipelines.get_decision_queue", return_value=dq),
        patch("routes.pipelines.get_pipeline_state_lock", return_value=nullcontext()),
        patch("routes.pipelines.report_pipeline_status"),
        patch("routes.pipelines._emit_event", None),
        patch("egg_contracts.loader.load_contract", side_effect=[_contract(resolved=False)]),
    ):
        _await_unresolved_gap_gate(
            store,
            "issue-42",
            Path("/repo"),
            Path("/worktree"),
            42,
            PipelinePhase.IMPLEMENT,
            True,
        )

    assert captured["pipeline"] == PipelineStatus.AWAITING_HUMAN
    assert captured["phase"] == PipelineStatus.AWAITING_HUMAN


def test_autonomous_open_gap_surfaces_but_does_not_block() -> None:
    """On a fully-autonomous pipeline (hitl_gates=False) an open gap must
    NOT queue a blocking decision — both options need a human, so blocking
    would stall forever. The reactive CI check stays the backstop (#3300
    review). The escalation is still surfaced (report_pipeline_status)."""
    dq = _FakeQueue(resolutions=[])
    gated, pipeline, report_mock = _run_gate(
        dq,
        load_side_effect=[_contract(resolved=False)],
        hitl_gates=False,
    )
    assert gated is False
    assert dq.queued == []
    # Never parked in AWAITING_HUMAN — the loop proceeds to finalize.
    assert pipeline.status != PipelineStatus.AWAITING_HUMAN
    # The escalation is still surfaced even though the gate doesn't block.
    assert report_mock.called


def test_open_gap_approve_then_clear() -> None:
    """Approval after the operator resolves the gap clears the gate with a
    single prompt (re-read finds the contract clean)."""
    dq = _FakeQueue(resolutions=["approve"])
    gated, _, _ = _run_gate(
        dq,
        load_side_effect=[_contract(resolved=False), _contract(resolved=True)],
    )
    assert gated is True
    assert len(dq.queued) == 1


def test_open_gap_approve_without_resolving_resurfaces() -> None:
    """A stale 'approve' while the gap is still open must NOT advance — the
    gate re-surfaces until resolved or overridden."""
    dq = _FakeQueue(resolutions=["approve", "override"])
    gated, _, _ = _run_gate(
        dq,
        load_side_effect=[_contract(resolved=False), _contract(resolved=False)],
    )
    assert gated is True
    # Two prompts: the ignored approve, then the override that ships it.
    assert len(dq.queued) == 2


def test_unresolved_decision_does_not_spin() -> None:
    """A cancelled/abandoned gate (non-RESOLVED) returns instead of looping."""
    dq = _FakeQueue(resolutions=[None])
    gated, _, _ = _run_gate(dq, load_side_effect=[_contract(resolved=False)])
    assert gated is True
    assert len(dq.queued) == 1
