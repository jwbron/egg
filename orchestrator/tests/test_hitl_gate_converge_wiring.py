"""Gate-level tests for ``_run_hitl_gate_converge`` resolution wiring (#3636).

``test_hitl_gate_resolution_parsing.py`` covers the pure classifier. This file
covers the wiring *inside the gate*, which is where the #3636 defect actually
lived and which nothing else exercises:

* the primary gate's bare-string branch, and that an approve-with-context
  answer advances instead of being fed back to producers as revision feedback;
* the "you didn't provide specifics" follow-up, including that an
  ``approve\\n\\n<note>`` answer there is an approval too;
* ``_bare_approve_context`` → ``_approve_context`` → the ``Operator note at
  the gate:`` line threaded into the decisions-resolved re-run;
* that a JSON payload with no ``action`` field (e.g. a list) is read as free
  text and reaches producers verbatim;
* ``record_resolution_outcome`` on both the primary and the follow-up
  decision, and that its failure cannot strand the gate;
* that a non-string ``context`` / ``feedback`` in an otherwise-valid
  ``{"action": ...}`` payload cannot fail the pipeline;
* the reuse-an-existing-pending-gate arm reaching the follow-up block without
  an ``UnboundLocalError``.
"""

import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies that the pipelines package imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from models import (  # noqa: E402
    DecisionStatus,
    HITLDecision,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import _run_hitl_gate_converge  # noqa: E402


class FakeDecisionQueue:
    """Records queued decisions and replays a scripted resolution for each."""

    def __init__(self, resolutions):
        # resolutions: list of raw resolution strings, one per queued decision
        self._resolutions = list(resolutions)
        self.queued = []
        self.recorded_outcomes = []
        self.record_error = None

    def queue_decision(self, *, question, context, options, decision_type, phase, **kwargs):
        decision = HITLDecision(
            id=f"d{len(self.queued) + 1}",
            question=question,
            context=context,
            options=options,
            decision_type=decision_type,
            phase=phase,
            status=DecisionStatus.PENDING,
        )
        self.queued.append(decision)
        return decision

    def wait_for_decision(self, decision_id):
        """Resolve the decision with the next scripted answer, as an operator would."""
        decision = self.get_decision(decision_id)
        if decision.resolution is None:
            decision.resolution = self._resolutions.pop(0) if self._resolutions else "approve"
            decision.status = DecisionStatus.RESOLVED
        return decision

    def get_decision(self, decision_id):
        return next(d for d in self.queued if d.id == decision_id)

    def record_resolution_outcome(self, decision_id, outcome):
        if self.record_error is not None:
            raise self.record_error
        self.recorded_outcomes.append((decision_id, outcome))


def _make_pipeline():
    pipeline = Pipeline(
        id="issue-3636",
        issue_number=3636,
        repo="jwbron/egg",
        branch="egg/3636",
    )
    pipeline.current_phase = PipelinePhase.REFINE
    pipeline.config.hitl_gates = True
    pipeline.get_phase_execution(PipelinePhase.REFINE).status = PipelineStatus.COMPLETE
    return pipeline


@contextmanager
def _null_lock(_pipeline_id):
    yield


@pytest.fixture
def gate_env(tmp_path):
    """Patch the gate's collaborators down to the resolution-parsing path."""
    pipeline = _make_pipeline()
    store = MagicMock()
    store.load_pipeline.return_value = pipeline
    spawner = MagicMock()

    rerun_calls = []

    def _capture_rerun(**kwargs):
        rerun_calls.append(kwargs)

    patches = {
        "get_pipeline_state_lock": _null_lock,
        "_collect_decision_ledger_status": lambda *a, **k: ("2 registered", False, None, {}),
        "_persist_decision_ledger_summary": lambda *a, **k: None,
        "_read_phase_draft": lambda *a, **k: "draft body",
        "_read_human_phase_draft": lambda *a, **k: None,
        "_get_draft_path": lambda *a, **k: None,
        "report_pipeline_status": lambda *a, **k: None,
        "_emit_pipeline_event": lambda *a, **k: None,
        "_persist_phase_gate_resolution": lambda *a, **k: None,
        "_commit_statefiles_to_worktree": lambda *a, **k: None,
        "_broadcast_hitl_nonconvergence_alert": lambda *a, **k: None,
        "_perform_hitl_phase_rerun": _capture_rerun,
    }

    with (
        patch.multiple("routes.pipelines", **patches),
        patch("routes.pipelines._queue_and_await_contract_decisions", return_value=0) as bridge,
    ):
        yield {
            "pipeline": pipeline,
            "store": store,
            "spawner": spawner,
            "rerun_calls": rerun_calls,
            "bridge": bridge,
            "repo_path": tmp_path,
        }


def _run(env, dq):
    with patch("routes.pipelines.get_decision_queue", return_value=dq):
        return _run_hitl_gate_converge(
            env["pipeline"],
            current_phase=PipelinePhase.REFINE,
            gateway_mode="local",
            pipeline_id="issue-3636",
            repo_path=env["repo_path"],
            spawner=env["spawner"],
            store=env["store"],
            worktree_repo_path=env["repo_path"],
        )


APPROVE_WITH_NOTE = (
    "approve\n\nApproved. The analysis is sound in its conclusion and its scope; advance to plan."
)


class TestPrimaryGateBareStringBranch:
    """The #3636 shape, driven through the gate rather than the classifier."""

    def test_approve_with_justification_advances(self, gate_env):
        dq = FakeDecisionQueue([APPROVE_WITH_NOTE])

        _pipeline, action = _run(gate_env, dq)

        # Advanced, not re-run: no phase rerun and no "continue" signal.
        assert action is None
        assert gate_env["rerun_calls"] == []

    def test_approval_is_not_handed_to_producers_as_feedback(self, gate_env):
        dq = FakeDecisionQueue([APPROVE_WITH_NOTE])

        _run(gate_env, dq)

        assert gate_env["rerun_calls"] == [], (
            "the operator's approval was fed back as revision feedback — #3636"
        )

    def test_free_text_still_re_runs_the_phase_with_that_feedback(self, gate_env):
        dq = FakeDecisionQueue(["The risk section omits the rollback path."])

        _pipeline, action = _run(gate_env, dq)

        assert action == "continue"
        assert len(gate_env["rerun_calls"]) == 1
        assert (
            gate_env["rerun_calls"][0]["feedback_text"]
            == "The risk section omits the rollback path."
        )

    def test_outcome_is_recorded_on_the_gate_decision(self, gate_env):
        dq = FakeDecisionQueue([APPROVE_WITH_NOTE])

        _run(gate_env, dq)

        assert dq.recorded_outcomes == [("d1", "approved")]

    def test_needs_revision_outcome_is_recorded(self, gate_env):
        dq = FakeDecisionQueue(["Split slice 3 before advancing."])

        _run(gate_env, dq)

        assert dq.recorded_outcomes == [("d1", "needs_revision")]

    def test_outcome_write_failure_cannot_strand_the_gate(self, gate_env):
        """``_save_pipeline`` re-raises raw ``OSError`` (ENOSPC/EROFS), which is
        not a ``StateStoreError``; an observability write must never propagate.
        """
        dq = FakeDecisionQueue([APPROVE_WITH_NOTE])
        dq.record_error = OSError(28, "No space left on device")

        _pipeline, action = _run(gate_env, dq)

        assert action is None


class TestSpecificsFollowUp:
    """Bare "request changes" → asked for specifics → answered."""

    def test_bare_request_changes_queues_a_follow_up(self, gate_env):
        dq = FakeDecisionQueue(["request changes", "approve"])

        _run(gate_env, dq)

        assert len(dq.queued) == 2
        assert dq.queued[1].options == ["approve"]
        assert "didn't provide specific feedback" in dq.queued[1].question

    def test_approve_with_note_on_the_follow_up_advances(self, gate_env):
        dq = FakeDecisionQueue(["request changes", APPROVE_WITH_NOTE])

        _pipeline, action = _run(gate_env, dq)

        assert action is None
        assert gate_env["rerun_calls"] == []

    def test_follow_up_specifics_re_run_the_phase(self, gate_env):
        dq = FakeDecisionQueue(["request changes", "The rollback path is missing."])

        _pipeline, action = _run(gate_env, dq)

        assert action == "continue"
        assert gate_env["rerun_calls"][0]["feedback_text"] == "The rollback path is missing."

    def test_outcome_is_recorded_on_both_decisions(self, gate_env):
        """The follow-up answer produced the branch, so an audit of the primary
        decision alone reads ``request changes`` with ``outcome: approved``."""
        dq = FakeDecisionQueue(["request changes", APPROVE_WITH_NOTE])

        _run(gate_env, dq)

        assert dq.recorded_outcomes == [("d1", "approved"), ("d2", "approved")]

    def test_punctuation_only_answer_does_not_approve(self, gate_env):
        """#3636 inverted: a stray "." first line must not read as approval."""
        dq = FakeDecisionQueue([".\nThe plan double-counts slice 2. Do not advance."])

        _pipeline, action = _run(gate_env, dq)

        assert action == "continue"
        assert gate_env["rerun_calls"][0]["feedback_text"].startswith(".")


class TestApproveContextThreading:
    """``_bare_approve_context`` reaches the decisions-resolved re-run."""

    def test_bare_approve_note_becomes_the_operator_note(self, gate_env):
        """The note reaches producers; the option word that selected it does not."""
        gate_env["bridge"].return_value = 2
        dq = FakeDecisionQueue(["approve\n\nWatch the migration ordering in slice 3."])

        _pipeline, action = _run(gate_env, dq)

        assert action == "continue"
        feedback = gate_env["rerun_calls"][0]["feedback_text"]
        assert "Operator note at the gate: Watch the migration ordering in slice 3." in feedback
        assert "approve" not in feedback.lower(), (
            "the bare option word was handed to producers as feedback — #3636"
        )

    def test_follow_up_approve_note_is_preferred_over_the_stale_original(self, gate_env):
        gate_env["bridge"].return_value = 1
        dq = FakeDecisionQueue(["request changes", "approve\n\nThe scope is right now."])

        _run(gate_env, dq)

        feedback = gate_env["rerun_calls"][0]["feedback_text"]
        assert "Operator note at the gate: The scope is right now." in feedback
        assert "request changes" not in feedback
        assert "approve" not in feedback.lower()

    def test_json_approve_context_still_wins(self, gate_env):
        gate_env["bridge"].return_value = 1
        dq = FakeDecisionQueue(['{"action": "approve", "context": "Watch the migration."}'])

        _run(gate_env, dq)

        assert (
            "Operator note at the gate: Watch the migration."
            in gate_env["rerun_calls"][0]["feedback_text"]
        )

    def test_json_list_resolution_is_free_text_and_reaches_producers_verbatim(self, gate_env):
        """A JSON *list* has no ``action`` field, so the JSON-first parse
        raises and the classifier reads it as free text: a change request
        whose feedback is the raw string.

        This never reaches the approve-context extractor — an approval is
        either an ``{"action": ...}`` mapping or is not valid JSON at all —
        which is why that extractor's non-mapping arm is a defensive
        fallthrough rather than a branch with a reachable trigger.
        """
        gate_env["bridge"].return_value = 1
        dq = FakeDecisionQueue(["[1, 2, 3]"])

        _pipeline, action = _run(gate_env, dq)

        assert action == "continue"
        assert gate_env["rerun_calls"][0]["feedback_text"] == "[1, 2, 3]"


class TestNonStringPayloadFields:
    """``context`` / ``feedback`` are untyped, and both reach a ``str`` op.

    Nothing on the resolution path type-checks them: the gate accepts any
    ``{"action": ...}`` mapping, so a non-string value reaches ``.strip()``
    on the approve-context path and ``feedback[:200]`` on the revision path.
    Either raises out of ``_run_hitl_gate_converge``, which the phase loop
    calls unguarded — the pipeline is marked FAILED at the moment the
    operator answered, with the decision already resolved and its outcome
    already stamped (#3636 review).
    """

    def test_dict_context_on_an_approve_does_not_fail_the_pipeline(self, gate_env):
        gate_env["bridge"].return_value = 1
        dq = FakeDecisionQueue(['{"action": "approve", "context": {"note": "ship it"}}'])

        _pipeline, action = _run(gate_env, dq)

        assert action == "continue"
        assert dq.recorded_outcomes == [("d1", "approved")]
        # Serialised rather than dropped: the operator's content still reaches
        # the producers.
        feedback = gate_env["rerun_calls"][0]["feedback_text"]
        assert 'Operator note at the gate: {"note": "ship it"}' in feedback

    def test_numeric_feedback_on_an_approve_does_not_fail_the_pipeline(self, gate_env):
        """``context`` absent, ``feedback`` non-string — the ``or`` chain
        yields the number and it lands on the same ``.strip()``."""
        gate_env["bridge"].return_value = 1
        dq = FakeDecisionQueue(['{"action": "approve", "feedback": 42}'])

        _pipeline, action = _run(gate_env, dq)

        assert action == "continue"
        assert "Operator note at the gate: 42" in gate_env["rerun_calls"][0]["feedback_text"]

    def test_dict_feedback_on_a_change_request_does_not_fail_the_pipeline(self, gate_env):
        """The revision path's ``_revision_feedback[:200]`` raised
        ``KeyError: slice(None, 200, None)`` on a non-string feedback."""
        dq = FakeDecisionQueue(['{"action": "request_changes", "feedback": {"x": 1}}'])

        _pipeline, action = _run(gate_env, dq)

        assert action == "continue"
        assert gate_env["rerun_calls"][0]["feedback_text"] == '{"x": 1}'
        assert dq.recorded_outcomes == [("d1", "needs_revision")]

    def test_dict_feedback_on_the_follow_up_does_not_fail_the_pipeline(self, gate_env):
        """The follow-up parse reads ``feedback`` from its own payload."""
        dq = FakeDecisionQueue(
            ["request changes", '{"action": "request_changes", "feedback": [1, 2]}']
        )

        _pipeline, action = _run(gate_env, dq)

        assert action == "continue"
        assert gate_env["rerun_calls"][0]["feedback_text"] == "[1, 2]"

    def test_null_feedback_on_a_change_request_asks_for_specifics(self, gate_env):
        """``None`` coerces to ``""``, matching the absent-field default, so
        the bare-label follow-up still fires rather than re-running the phase
        against the string ``"None"``."""
        dq = FakeDecisionQueue(['{"action": "request_changes", "feedback": null}', "approve"])

        _run(gate_env, dq)

        assert len(dq.queued) == 2
        assert "didn't provide specific feedback" in dq.queued[1].question


class TestExistingPendingGateReuse:
    """The reuse arm (#1152) must reach the follow-up block intact."""

    def test_bare_request_changes_on_a_reused_gate_does_not_crash(self, gate_env):
        """``phase_label`` / ``draft_content`` were bound only on the
        create-a-new-decision arm, so this path raised ``UnboundLocalError``
        and killed the driver mid-gate with the decision already resolved."""
        pipeline = gate_env["pipeline"]
        pending = HITLDecision(
            id="pre-existing",
            question="The refine phase has completed.",
            context="gate content from the earlier decision",
            options=["approve", "request changes"],
            decision_type="phase_gate",
            phase=PipelinePhase.REFINE,
            status=DecisionStatus.PENDING,
        )
        pipeline.decisions.append(pending)

        # The reuse arm pulls the decision off the pipeline, not the queue, so
        # seed the queue's view with it; the scripted answers then land on it
        # and on the follow-up in order.
        dq = FakeDecisionQueue(["request changes.", "approve"])
        dq.queued.append(pending)

        _pipeline, action = _run(gate_env, dq)

        # Follow-up was queued (not crashed) and carried the reused decision's
        # own context, and the gate advanced on the "approve" answer.
        assert len(dq.queued) == 2
        assert dq.queued[1].context == "gate content from the earlier decision"
        assert "in the analysis" in dq.queued[1].question
        assert action is None

    @pytest.mark.parametrize(
        "resolution", ["request changes.", "Request Changes!", "request changes\n"]
    )
    def test_punctuated_request_changes_reaches_the_follow_up(self, gate_env, resolution):
        """The first-line fix widened the set of answers that produce
        "no actionable feedback", so these must not crash either."""
        dq = FakeDecisionQueue([resolution, "approve"])

        _pipeline, action = _run(gate_env, dq)

        assert len(dq.queued) == 2
        assert action is None
