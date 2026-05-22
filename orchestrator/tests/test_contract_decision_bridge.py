"""Tests for the contract-decision bridge at phase_gate approval.

When an agent registers questions via ``egg-contract add-decision`` /
``egg-contract add-feedback`` during refine/plan, those entries live only
in the contract JSON — the orchestrator's decision queue is blind to
them.  ``_queue_and_await_contract_decisions`` promotes those contract
questions to orchestrator decisions after phase_gate approval so HTTP/MCP
callers surface them individually.

See: https://github.com/jwbron/egg/issues/1889
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from models import DecisionStatus, HITLDecision, PipelinePhase


def _make_contract_file(
    repo: Path,
    identifier: str,
    *,
    decisions: list[dict[str, Any]] | None = None,
    feedback: dict[str, Any] | None = None,
) -> Path:
    """Write a minimal valid contract JSON for tests."""
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
        "feedback": feedback,
        "audit_log": [],
        "agent_executions": [],
    }
    path = contracts_dir / f"{identifier}.json"
    path.write_text(json.dumps(payload))
    return path


class _FakeQueue:
    """Minimal stand-in for DecisionQueue used in tests.

    Auto-resolves every queued decision with a caller-provided resolution,
    mirroring the shape of DecisionQueue.queue_decision / wait_for_decision.
    """

    def __init__(self, resolutions: list[str]) -> None:
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
        resolution = self._resolutions.pop(0) if self._resolutions else ""
        decision = HITLDecision(
            id=f"orch-{self._counter}",
            question=question,
            context=context,
            options=options or [],
            decision_type=decision_type,  # type: ignore[arg-type]
            questions=questions or [],
            phase=phase,
            status=DecisionStatus.RESOLVED,
            resolution=resolution,
        )
        self.queued.append(decision)
        return decision

    def wait_for_decision(self, decision_id: str) -> HITLDecision:
        for d in self.queued:
            if d.id == decision_id:
                return d
        raise AssertionError(f"decision {decision_id} not queued")


def test_bridge_promotes_unresolved_hitl_decisions(tmp_path: Path) -> None:
    from routes.pipelines import _queue_and_await_contract_decisions

    identifier = "issue-42"
    _make_contract_file(
        tmp_path,
        identifier,
        decisions=[
            {
                "id": "decision-1",
                "question": "Which database?",
                "type": "hitl",
                "phase": "refine",
                "options": [
                    {"id": "opt-1", "label": "Postgres", "description": None},
                    {"id": "opt-2", "label": "SQLite", "description": None},
                ],
                "resolved": False,
                "resolution": None,
                "resolved_by": None,
                "resolved_at": None,
                "debounce_until": None,
            },
            {
                "id": "decision-2",
                "question": "Already answered",
                "type": "hitl",
                "phase": "refine",
                "options": [],
                "resolved": True,
                "resolution": "done",
                "resolved_by": "human",
                "resolved_at": "2026-04-01T00:00:00+00:00",
                "debounce_until": None,
            },
        ],
    )
    dq = _FakeQueue(resolutions=["Postgres"])

    _queue_and_await_contract_decisions(
        dq,
        tmp_path,
        "pipeline-id",
        identifier,
        PipelinePhase.REFINE,
    )

    # Exactly one promotion — the already-resolved one is skipped.
    assert len(dq.queued) == 1
    assert dq.queued[0].question == "Which database?"
    assert dq.queued[0].options == ["Postgres", "SQLite"]
    assert dq.queued[0].decision_type == "choice"

    data = json.loads((tmp_path / ".egg-state/contracts/issue-42.json").read_text())
    decision_1 = next(d for d in data["decisions"] if d["id"] == "decision-1")
    assert decision_1["resolved"] is True
    assert decision_1["resolution"] == "Postgres"
    assert decision_1["resolved_by"] == "human"
    assert decision_1["resolved_at"] is not None


def test_bridge_promotes_unsubmitted_feedback(tmp_path: Path) -> None:
    from routes.pipelines import _queue_and_await_contract_decisions

    identifier = "issue-42"
    _make_contract_file(
        tmp_path,
        identifier,
        feedback={
            "id": "feedback-1",
            "phase": "refine",
            "questions": [
                {"id": "Q1", "question": "What volume?", "answer": None},
                {"id": "Q2", "question": "Legacy support?", "answer": None},
            ],
            "submitted": False,
            "submitted_by": None,
            "submitted_at": None,
            "comment_id": None,
            "debounce_until": None,
        },
    )
    dq = _FakeQueue(
        resolutions=[
            json.dumps(
                {
                    "action": "submit_feedback",
                    "answers": {"Q1": "~10 RPS", "Q2": "no"},
                }
            )
        ]
    )

    _queue_and_await_contract_decisions(
        dq,
        tmp_path,
        "pipeline-id",
        identifier,
        PipelinePhase.REFINE,
    )

    assert len(dq.queued) == 1
    assert dq.queued[0].decision_type == "feedback"
    assert {q["id"] for q in dq.queued[0].questions} == {"Q1", "Q2"}

    data = json.loads((tmp_path / ".egg-state/contracts/issue-42.json").read_text())
    fb = data["feedback"]
    assert fb["submitted"] is True
    assert fb["submitted_by"] == "human"
    answers = {q["id"]: q["answer"] for q in fb["questions"]}
    assert answers == {"Q1": "~10 RPS", "Q2": "no"}


def test_bridge_skips_decisions_for_other_phases(tmp_path: Path) -> None:
    from routes.pipelines import _queue_and_await_contract_decisions

    identifier = "issue-42"
    _make_contract_file(
        tmp_path,
        identifier,
        decisions=[
            {
                "id": "decision-1",
                "question": "For plan phase only",
                "type": "hitl",
                "phase": "plan",
                "options": [],
                "resolved": False,
                "resolution": None,
                "resolved_by": None,
                "resolved_at": None,
                "debounce_until": None,
            }
        ],
    )
    dq = _FakeQueue(resolutions=[])

    _queue_and_await_contract_decisions(
        dq,
        tmp_path,
        "pipeline-id",
        identifier,
        PipelinePhase.REFINE,
    )

    # A plan-scoped decision must not be surfaced at the refine phase_gate.
    assert dq.queued == []


def test_bridge_skips_auto_decisions(tmp_path: Path) -> None:
    """AUTO decisions must not be promoted — only HITL ones."""
    from routes.pipelines import _queue_and_await_contract_decisions

    identifier = "issue-42"
    _make_contract_file(
        tmp_path,
        identifier,
        decisions=[
            {
                "id": "decision-auto",
                "question": "Auto-resolved question",
                "type": "auto",
                "phase": "refine",
                "options": [],
                "resolved": False,
                "resolution": None,
                "resolved_by": None,
                "resolved_at": None,
                "debounce_until": None,
            },
        ],
    )
    dq = _FakeQueue(resolutions=[])

    _queue_and_await_contract_decisions(
        dq,
        tmp_path,
        "pipeline-id",
        identifier,
        PipelinePhase.REFINE,
    )

    # AUTO decisions must not be surfaced as human choice decisions.
    assert dq.queued == []


def test_bridge_marks_feedback_submitted_on_unparseable_resolution(tmp_path: Path) -> None:
    """Feedback is marked submitted even when the resolution JSON doesn't parse."""
    from routes.pipelines import _queue_and_await_contract_decisions

    identifier = "issue-42"
    _make_contract_file(
        tmp_path,
        identifier,
        feedback={
            "id": "feedback-1",
            "phase": "refine",
            "questions": [
                {"id": "Q1", "question": "What volume?", "answer": None},
            ],
            "submitted": False,
            "submitted_by": None,
            "submitted_at": None,
            "comment_id": None,
            "debounce_until": None,
        },
    )
    # Resolution is a plain string, not the expected {"answers": {...}} JSON.
    dq = _FakeQueue(resolutions=["just a freeform string"])

    _queue_and_await_contract_decisions(
        dq,
        tmp_path,
        "pipeline-id",
        identifier,
        PipelinePhase.REFINE,
    )

    data = json.loads((tmp_path / ".egg-state/contracts/issue-42.json").read_text())
    fb = data["feedback"]
    # Feedback must be marked submitted even though answers didn't parse —
    # the human responded and shouldn't be asked again.
    assert fb["submitted"] is True
    assert fb["submitted_by"] == "human"
    # Individual question answers remain None since the format didn't match.
    assert fb["questions"][0]["answer"] is None


def test_bridge_is_noop_when_contract_missing(tmp_path: Path) -> None:
    from routes.pipelines import _queue_and_await_contract_decisions

    dq = _FakeQueue(resolutions=[])
    _queue_and_await_contract_decisions(
        dq,
        tmp_path,
        "pipeline-id",
        "issue-999",
        PipelinePhase.REFINE,
    )
    assert dq.queued == []


class _OrderTrackingQueue(_FakeQueue):
    """FakeQueue that records queue/wait call order (for #1956)."""

    def __init__(self, resolutions: list[str]) -> None:
        super().__init__(resolutions)
        self.events: list[tuple[str, str]] = []

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
        decision = super().queue_decision(
            question=question,
            context=context,
            options=options,
            decision_type=decision_type,
            questions=questions,
            phase=phase,
            content_changed=content_changed,
        )
        self.events.append(("queue", decision.id))
        return decision

    def wait_for_decision(self, decision_id: str) -> HITLDecision:
        self.events.append(("wait", decision_id))
        return super().wait_for_decision(decision_id)


def test_bridge_queues_all_decisions_before_waiting(tmp_path: Path) -> None:
    """All queue_decision calls must precede the first wait_for_decision.

    Regression test for #1956: previously the bridge queued and waited on
    decisions one at a time, so ``get_status`` only ever surfaced a single
    pending decision even when the contract had many. The fix batches the
    queue pass so the skill can prompt for up to 4 decisions at once.
    """
    from routes.pipelines import _queue_and_await_contract_decisions

    identifier = "issue-42"
    _make_contract_file(
        tmp_path,
        identifier,
        decisions=[
            {
                "id": f"decision-{i}",
                "question": f"Question {i}?",
                "type": "hitl",
                "phase": "refine",
                "options": [
                    {"id": "opt-a", "label": "A", "description": None},
                    {"id": "opt-b", "label": "B", "description": None},
                ],
                "resolved": False,
                "resolution": None,
                "resolved_by": None,
                "resolved_at": None,
                "debounce_until": None,
            }
            for i in range(1, 4)
        ],
        feedback={
            "id": "feedback-1",
            "phase": "refine",
            "questions": [{"id": "Q1", "question": "Notes?", "answer": None}],
            "submitted": False,
            "submitted_by": None,
            "submitted_at": None,
            "comment_id": None,
            "debounce_until": None,
        },
    )
    dq = _OrderTrackingQueue(
        resolutions=[
            "A",
            "B",
            "A",
            json.dumps({"action": "submit_feedback", "answers": {"Q1": "ok"}}),
        ]
    )

    _queue_and_await_contract_decisions(
        dq,
        tmp_path,
        "pipeline-id",
        identifier,
        PipelinePhase.REFINE,
    )

    # 3 choice decisions + 1 feedback decision → 4 queue events, 4 wait events
    queue_events = [e for e in dq.events if e[0] == "queue"]
    wait_events = [e for e in dq.events if e[0] == "wait"]
    assert len(queue_events) == 4
    assert len(wait_events) == 4

    # Every queue must happen before the first wait — that's what lets
    # ``get_status`` surface all pending decisions as a single batch.
    first_wait_idx = next(i for i, e in enumerate(dq.events) if e[0] == "wait")
    for e in dq.events[:first_wait_idx]:
        assert e[0] == "queue", f"queue-before-wait invariant broken: {dq.events}"

    # Verify contract persistence: all choice decisions resolved with correct
    # values, and feedback marked submitted with the expected answer.
    data = json.loads((tmp_path / ".egg-state/contracts/issue-42.json").read_text())
    for i, expected_res in enumerate(["A", "B", "A"], start=1):
        d = next(d for d in data["decisions"] if d["id"] == f"decision-{i}")
        assert d["resolved"] is True, f"decision-{i} not resolved"
        assert d["resolution"] == expected_res, f"decision-{i} resolution mismatch"
    fb = data["feedback"]
    assert fb["submitted"] is True
    assert fb["questions"][0]["answer"] == "ok"


def test_bridge_emits_decision_created_event(tmp_path: Path, monkeypatch: Any) -> None:
    """The bridged batch must emit a ``decision.created`` EventBus event.

    ``DecisionQueue.queue_decision`` emits no event, so without an explicit
    emission the operator's ``wait-status`` monitor never wakes on the
    bridged decisions and only finds them via a manual ``get_status``
    (regression test for #2770).
    """
    import routes.pipelines as rp
    from events import EventType

    captured: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        rp,
        "_emit_event",
        lambda event_type, pipeline_id, data=None, source="orchestrator": captured.append(
            (event_type, pipeline_id)
        ),
    )

    identifier = "issue-42"
    _make_contract_file(
        tmp_path,
        identifier,
        decisions=[
            {
                "id": "decision-1",
                "question": "Which database?",
                "type": "hitl",
                "phase": "refine",
                "options": [{"id": "opt-1", "label": "Postgres", "description": None}],
                "resolved": False,
                "resolution": None,
                "resolved_by": None,
                "resolved_at": None,
                "debounce_until": None,
            },
        ],
    )
    dq = _FakeQueue(resolutions=["Postgres"])

    rp._queue_and_await_contract_decisions(
        dq,
        tmp_path,
        "pipeline-id",
        identifier,
        PipelinePhase.REFINE,
    )

    assert (EventType.DECISION_CREATED, "pipeline-id") in captured


def test_bridge_emits_no_event_when_nothing_queued(tmp_path: Path, monkeypatch: Any) -> None:
    """No ``decision.created`` event fires when there is nothing to bridge."""
    import routes.pipelines as rp
    from events import EventType

    captured: list[Any] = []
    monkeypatch.setattr(
        rp,
        "_emit_event",
        lambda event_type, pipeline_id, data=None, source="orchestrator": captured.append(
            event_type
        ),
    )

    identifier = "issue-42"
    # A plan-scoped decision — nothing to bridge at the refine gate.
    _make_contract_file(
        tmp_path,
        identifier,
        decisions=[
            {
                "id": "decision-1",
                "question": "For plan phase only",
                "type": "hitl",
                "phase": "plan",
                "options": [],
                "resolved": False,
                "resolution": None,
                "resolved_by": None,
                "resolved_at": None,
                "debounce_until": None,
            }
        ],
    )
    dq = _FakeQueue(resolutions=[])

    rp._queue_and_await_contract_decisions(
        dq,
        tmp_path,
        "pipeline-id",
        identifier,
        PipelinePhase.REFINE,
    )

    assert EventType.DECISION_CREATED not in captured
