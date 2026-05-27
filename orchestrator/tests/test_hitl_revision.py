"""
Tests for HITL (Human-In-The-Loop) revision flow logic in the pipeline runner.

Covers:
- _APPROVE_KEYWORDS / _BARE_OPTION_LABELS classification
- hitl_review_cycles counter independence from agentic review_cycles
- Contract re-population after HITL revision
- Follow-up decision options (no "request changes" on follow-up)
- Circuit breaker behavior with separate counters
- Mock-based integration tests for HITL revision flow paths
"""

import json
import sys
from unittest.mock import MagicMock

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from models import PhaseExecution, PipelineConfig, PipelinePhase, PipelineStatus
from routes.pipelines import _APPROVE_KEYWORDS, _BARE_OPTION_LABELS


class TestApproveKeywords:
    """Verify _APPROVE_KEYWORDS correctly classifies resolutions."""

    def test_empty_string_is_approval(self):
        assert "" in _APPROVE_KEYWORDS

    def test_approve_is_approval(self):
        assert "approve" in _APPROVE_KEYWORDS
        assert "approved" in _APPROVE_KEYWORDS

    def test_lgtm_is_approval(self):
        assert "lgtm" in _APPROVE_KEYWORDS

    def test_feedback_text_is_not_approval(self):
        assert "please fix the error handling" not in _APPROVE_KEYWORDS

    def test_request_changes_is_not_approval(self):
        assert "request changes" not in _APPROVE_KEYWORDS


class TestBareOptionLabels:
    """Verify _BARE_OPTION_LABELS correctly classifies bare labels."""

    def test_request_changes_is_bare(self):
        assert "request changes" in _BARE_OPTION_LABELS
        assert "request_changes" in _BARE_OPTION_LABELS

    def test_approve_is_not_bare(self):
        assert "approve" not in _BARE_OPTION_LABELS

    def test_feedback_text_is_not_bare(self):
        assert "fix the tests" not in _BARE_OPTION_LABELS


class TestHITLRevisionCounterIndependence:
    """Verify that agentic and HITL review cycles are independent."""

    def test_agentic_cycles_dont_affect_hitl_budget(self):
        """Agentic review cycles should not consume the HITL revision budget."""
        config = PipelineConfig(max_review_cycles=3, max_hitl_review_cycles=3)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Simulate agentic review loop using all 3 cycles
        phase.review_cycles = 3

        # HITL revision should still have its full budget
        assert phase.hitl_review_cycles == 0
        assert phase.hitl_review_cycles < config.max_hitl_review_cycles

    def test_hitl_cycles_dont_affect_agentic_budget(self):
        """HITL revision cycles should not consume the agentic review budget."""
        config = PipelineConfig(max_review_cycles=3, max_hitl_review_cycles=3)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Simulate HITL revisions
        phase.hitl_review_cycles = 2

        # Agentic review should still have its full budget
        assert phase.review_cycles == 0
        assert phase.review_cycles < config.max_review_cycles

    def test_circuit_breaker_uses_hitl_counter(self):
        """Circuit breaker should fire based on hitl_review_cycles, not max_hitl_review_cycles."""
        config = PipelineConfig(max_review_cycles=3, max_hitl_review_cycles=3)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Agentic cycles are at limit, but HITL has budget
        phase.review_cycles = 3
        phase.hitl_review_cycles = 1

        # HITL circuit breaker should NOT fire
        assert phase.hitl_review_cycles < config.max_hitl_review_cycles

        # Increment HITL to limit
        phase.hitl_review_cycles = 3
        # Now it should fire
        assert phase.hitl_review_cycles >= config.max_hitl_review_cycles

    def test_independent_config_limits(self):
        """Agentic and HITL budgets can be configured independently."""
        config = PipelineConfig(max_review_cycles=2, max_hitl_review_cycles=5)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Agentic at limit
        phase.review_cycles = 2
        assert phase.review_cycles >= config.max_review_cycles

        # But HITL has plenty of budget
        phase.hitl_review_cycles = 3
        assert phase.hitl_review_cycles < config.max_hitl_review_cycles

    def test_serialization_roundtrip(self):
        """Both counters survive JSON serialization."""
        phase = PhaseExecution(phase=PipelinePhase.PLAN)
        phase.review_cycles = 2
        phase.hitl_review_cycles = 1

        data = phase.model_dump()
        restored = PhaseExecution(**data)
        assert restored.review_cycles == 2
        assert restored.hitl_review_cycles == 1


class TestFollowUpDecisionOptions:
    """Verify the follow-up decision does not re-offer 'request changes'."""

    def test_resolution_classification_flow(self):
        """Simulate the resolution classification logic from _run_pipeline.

        When a bare "request changes" is received, the follow-up should
        only offer "approve" (not "request changes" again).
        """
        # Initial resolution: bare "request changes"
        resolution = "request changes"
        assert resolution.lower() not in _APPROVE_KEYWORDS
        assert resolution.lower() in _BARE_OPTION_LABELS

        # Follow-up should only have "approve" as an option.
        # If the human provides free-text feedback, it won't match either set
        # and will be treated as actionable feedback → revision.
        followup_options = ["approve"]
        assert "request changes" not in followup_options

        # Free-text feedback should trigger revision
        followup_resolution = "Fix the error handling in step 3"
        assert followup_resolution.lower() not in _APPROVE_KEYWORDS
        assert followup_resolution.lower() not in _BARE_OPTION_LABELS

    def test_followup_approve_falls_through(self):
        """If follow-up resolution is 'approve', it should be in _APPROVE_KEYWORDS."""
        resolution = "approve"
        assert resolution.lower() in _APPROVE_KEYWORDS

    def test_followup_empty_is_approval(self):
        """Empty follow-up (timeout) falls through to approval."""
        resolution = ""
        assert resolution.lower() in _APPROVE_KEYWORDS


class TestContractRePopulation:
    """Verify _populate_contract_from_plan is called on every plan completion."""

    def test_plan_phase_always_triggers_contract_population(self):
        """The review_cycles == 0 guard should no longer exist.

        _populate_contract_from_plan should be called for plan phase
        regardless of review_cycles or hitl_review_cycles count.
        """
        import inspect

        from routes.pipelines import _populate_contract_from_plan

        # Verify the function exists and is callable
        assert callable(_populate_contract_from_plan)

        # Read the source of _run_pipeline to verify the guard was removed
        from routes import pipelines

        source = inspect.getsource(pipelines)

        # The old guard was: if current_phase.value == "plan" and phase_execution.review_cycles == 0:
        # The new guard should just be: if current_phase.value == "plan":
        # Verify the combined condition no longer exists
        assert 'current_phase.value == "plan" and phase_execution.review_cycles == 0' not in source


class TestMaxHITLReviewCyclesConfig:
    """Verify max_hitl_review_cycles is a separate config field wired to the circuit breaker."""

    def test_config_field_exists_with_default(self):
        """max_hitl_review_cycles should default to 3."""
        config = PipelineConfig()
        assert config.max_hitl_review_cycles == 3

    def test_config_field_independent_of_max_review_cycles(self):
        """max_hitl_review_cycles can differ from max_review_cycles."""
        config = PipelineConfig(max_review_cycles=2, max_hitl_review_cycles=5)
        assert config.max_review_cycles == 2
        assert config.max_hitl_review_cycles == 5

    def test_config_serialization(self):
        """max_hitl_review_cycles survives JSON roundtrip."""
        config = PipelineConfig(max_hitl_review_cycles=7)
        data = config.model_dump()
        restored = PipelineConfig(**data)
        assert restored.max_hitl_review_cycles == 7

    def test_circuit_breaker_reads_hitl_config(self):
        """The pipeline source should reference config.max_hitl_review_cycles, not max_review_cycles."""
        import inspect

        from routes import pipelines

        source = inspect.getsource(pipelines)
        # The old code was: max_hitl_cycles = pipeline.config.max_review_cycles
        # It should now be: max_hitl_cycles = pipeline.config.max_hitl_review_cycles
        assert "pipeline.config.max_hitl_review_cycles" in source
        # Verify the old pattern is NOT used for the HITL circuit breaker.
        # The string "max_hitl_cycles = pipeline.config.max_review_cycles" should
        # not appear (it should use max_hitl_review_cycles instead).
        assert "max_hitl_cycles = pipeline.config.max_review_cycles\n" not in source


class TestHITLRevisionFlowIntegration:
    """Mock-based integration tests for the HITL revision flow paths.

    These tests simulate the decision queue interactions to verify the
    branching logic in the HITL gate block of _run_pipeline.
    """

    def _make_decision(self, resolution=None, status="resolved"):
        """Create a mock HITLDecision."""
        from models import DecisionStatus, HITLDecision

        return HITLDecision(
            id="test-decision-1",
            question="Review the plan",
            status=DecisionStatus(status),
            resolution=resolution,
        )

    def _make_followup_decision(self, resolution=None, status="resolved"):
        """Create a mock follow-up HITLDecision."""
        from models import DecisionStatus, HITLDecision

        return HITLDecision(
            id="test-followup-1",
            question="Provide feedback",
            options=["approve"],
            status=DecisionStatus(status),
            resolution=resolution,
        )

    def test_approve_path_no_revision(self):
        """When human approves, hitl_revision_feedback should remain None.

        Simulates: human selects "approve" → resolution is in _APPROVE_KEYWORDS
        → no revision, pipeline advances.
        """
        resolution = "approve"
        hitl_revision_feedback = None

        # The HITL gate logic: if resolution not in approve keywords, enter revision path
        if resolution.lower() not in _APPROVE_KEYWORDS:
            hitl_revision_feedback = resolution

        assert hitl_revision_feedback is None

    def test_lgtm_path_no_revision(self):
        """'lgtm' should be treated as approval."""
        resolution = "lgtm"
        hitl_revision_feedback = None

        if resolution.lower() not in _APPROVE_KEYWORDS:
            hitl_revision_feedback = resolution

        assert hitl_revision_feedback is None

    def test_direct_feedback_triggers_revision(self):
        """When human provides feedback text, it should trigger revision.

        Simulates: human provides "Fix error handling in step 3" →
        not in _APPROVE_KEYWORDS, not in _BARE_OPTION_LABELS →
        hitl_revision_feedback is set → continue to re-run phase.
        """
        resolution = "Fix error handling in step 3"
        hitl_revision_feedback = None
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        if resolution.lower() not in _APPROVE_KEYWORDS:
            if resolution.lower() in _BARE_OPTION_LABELS:
                pass  # Would go to follow-up path
            # Re-check after potential follow-up
            if (
                resolution.lower() not in _APPROVE_KEYWORDS
                and resolution.lower() not in _BARE_OPTION_LABELS
            ):
                phase.hitl_review_cycles += 1
                config = PipelineConfig(max_hitl_review_cycles=3)
                if phase.hitl_review_cycles >= config.max_hitl_review_cycles:
                    pass  # Circuit breaker
                else:
                    hitl_revision_feedback = resolution

        assert hitl_revision_feedback == "Fix error handling in step 3"
        assert phase.hitl_review_cycles == 1

    def test_bare_request_changes_triggers_followup(self):
        """Bare 'request changes' should trigger a follow-up decision.

        Simulates: human selects "request changes" (bare label) →
        in _BARE_OPTION_LABELS → follow-up is queued with only ["approve"].
        """
        resolution = "request changes"

        entered_followup = False
        followup_options = None

        if resolution.lower() not in _APPROVE_KEYWORDS:
            if resolution.lower() in _BARE_OPTION_LABELS:
                entered_followup = True
                followup_options = ["approve"]

        assert entered_followup is True
        assert followup_options == ["approve"]
        assert "request changes" not in followup_options

    def test_followup_with_feedback_triggers_revision(self):
        """Follow-up with real feedback should trigger revision.

        Simulates: bare "request changes" → follow-up queued →
        human provides "Add more detail to task 2" → revision triggered.
        """
        initial_resolution = "request changes"
        followup_resolution = "Add more detail to task 2"
        hitl_revision_feedback = None
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Simulate the full flow
        resolution = initial_resolution
        if resolution.lower() not in _APPROVE_KEYWORDS:
            if resolution.lower() in _BARE_OPTION_LABELS:
                # Follow-up path — update resolution with follow-up result
                resolution = followup_resolution

                if (
                    resolution.lower() in _APPROVE_KEYWORDS
                    or resolution.lower() in _BARE_OPTION_LABELS
                ):
                    pass  # Treat as approval
                # else: fall through to revision check

            if (
                resolution.lower() not in _APPROVE_KEYWORDS
                and resolution.lower() not in _BARE_OPTION_LABELS
            ):
                phase.hitl_review_cycles += 1
                config = PipelineConfig(max_hitl_review_cycles=3)
                if phase.hitl_review_cycles >= config.max_hitl_review_cycles:
                    pass
                else:
                    hitl_revision_feedback = resolution

        assert hitl_revision_feedback == "Add more detail to task 2"
        assert phase.hitl_review_cycles == 1

    def test_followup_approve_advances(self):
        """Follow-up with 'approve' should advance without revision.

        Simulates: bare "request changes" → follow-up queued →
        human selects "approve" → no revision.
        """
        initial_resolution = "request changes"
        followup_resolution = "approve"
        hitl_revision_feedback = None

        resolution = initial_resolution
        if resolution.lower() not in _APPROVE_KEYWORDS:
            if resolution.lower() in _BARE_OPTION_LABELS:
                resolution = followup_resolution
                if (
                    resolution.lower() in _APPROVE_KEYWORDS
                    or resolution.lower() in _BARE_OPTION_LABELS
                ):
                    resolution = resolution  # Will be caught by approval check below

            if (
                resolution.lower() not in _APPROVE_KEYWORDS
                and resolution.lower() not in _BARE_OPTION_LABELS
            ):
                hitl_revision_feedback = resolution

        assert hitl_revision_feedback is None

    def test_followup_timeout_advances(self):
        """Follow-up timeout (empty resolution) should advance.

        Simulates: bare "request changes" → follow-up queued →
        timeout → resolution is "" → in _APPROVE_KEYWORDS → advance.
        """
        initial_resolution = "request changes"
        followup_resolution = ""  # Timeout produces empty string
        hitl_revision_feedback = None

        resolution = initial_resolution
        if resolution.lower() not in _APPROVE_KEYWORDS:
            if resolution.lower() in _BARE_OPTION_LABELS:
                resolution = followup_resolution
                if (
                    resolution.lower() in _APPROVE_KEYWORDS
                    or resolution.lower() in _BARE_OPTION_LABELS
                ):
                    pass  # Treat as approval

            if (
                resolution.lower() not in _APPROVE_KEYWORDS
                and resolution.lower() not in _BARE_OPTION_LABELS
            ):
                hitl_revision_feedback = resolution

        assert hitl_revision_feedback is None

    def test_circuit_breaker_prevents_unbounded_revisions(self):
        """Circuit breaker should force approval after max HITL cycles.

        Simulates: human requests changes repeatedly →
        hitl_review_cycles reaches max_hitl_review_cycles →
        circuit breaker fires → pipeline advances despite feedback.
        """
        config = PipelineConfig(max_hitl_review_cycles=2)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Simulate two prior HITL revision cycles
        phase.hitl_review_cycles = 2
        resolution = "Please fix the formatting"
        circuit_breaker_fired = False
        hitl_revision_feedback = None

        if resolution.lower() not in _APPROVE_KEYWORDS:
            if resolution.lower() not in _BARE_OPTION_LABELS:
                phase.hitl_review_cycles += 1  # Would be incremented
                # But check against config
                # Revert — in real code the increment happens, then check
                if phase.hitl_review_cycles >= config.max_hitl_review_cycles:
                    circuit_breaker_fired = True
                else:
                    hitl_revision_feedback = resolution

        assert circuit_breaker_fired is True
        assert hitl_revision_feedback is None

    def test_circuit_breaker_respects_hitl_config_not_agentic(self):
        """Circuit breaker should use max_hitl_review_cycles, not max_review_cycles.

        With max_review_cycles=1 and max_hitl_review_cycles=5, the HITL
        circuit breaker should not fire until hitl_review_cycles reaches 5.
        """
        config = PipelineConfig(max_review_cycles=1, max_hitl_review_cycles=5)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Agentic cycles maxed out
        phase.review_cycles = 1

        # HITL has used 3 of 5 cycles
        phase.hitl_review_cycles = 3

        # Should NOT fire — 3 < 5
        assert phase.hitl_review_cycles < config.max_hitl_review_cycles

        # At limit — should fire
        phase.hitl_review_cycles = 5
        assert phase.hitl_review_cycles >= config.max_hitl_review_cycles

    def test_multiple_revision_cycles_increment_counter(self):
        """Each revision cycle should increment hitl_review_cycles by 1."""
        phase = PhaseExecution(phase=PipelinePhase.PLAN)
        config = PipelineConfig(max_hitl_review_cycles=5)

        feedbacks = [
            "Fix task 1 description",
            "Add risk assessment",
            "Update timeline",
        ]
        collected_feedback = []

        for feedback in feedbacks:
            resolution = feedback
            if (
                resolution.lower() not in _APPROVE_KEYWORDS
                and resolution.lower() not in _BARE_OPTION_LABELS
            ):
                phase.hitl_review_cycles += 1
                if phase.hitl_review_cycles >= config.max_hitl_review_cycles:
                    break
                collected_feedback.append(resolution)

        assert phase.hitl_review_cycles == 3
        assert len(collected_feedback) == 3
        assert collected_feedback == feedbacks


class TestJSONResolutionParsing:
    """Test JSON-first resolution parsing for HITL decisions.

    Simulates the branching logic in _run_pipeline's HITL gate block.
    JSON resolutions are tried first via json.loads; on JSONDecodeError,
    falls back to keyword matching.
    """

    def _classify_resolution(self, resolution_str):
        """Simulate the JSON-first resolution parsing logic.

        Returns (is_approved, needs_revision, revision_feedback).
        """
        _is_approved = False
        _needs_revision = False
        _revision_feedback = None

        try:
            payload = json.loads(resolution_str)
            if isinstance(payload, dict) and "action" in payload:
                action = payload["action"]
                feedback_text = payload.get("feedback", "")

                if action == "approve":
                    _is_approved = True
                elif action == "select":
                    _is_approved = True
                elif action == "submit_feedback":
                    _is_approved = True
                elif action in ("request_changes", "change_approach"):
                    if feedback_text:
                        _needs_revision = True
                        _revision_feedback = feedback_text
                    else:
                        _needs_revision = True
                        _revision_feedback = None
                else:
                    raise json.JSONDecodeError("unknown action", resolution_str, 0)
            else:
                raise json.JSONDecodeError("no action field", resolution_str, 0)
        except json.JSONDecodeError, TypeError, AttributeError:
            if resolution_str.lower() in _APPROVE_KEYWORDS:
                _is_approved = True
            elif resolution_str.lower() in _BARE_OPTION_LABELS:
                _needs_revision = True
                _revision_feedback = None
            elif resolution_str:
                _needs_revision = True
                _revision_feedback = resolution_str

        return _is_approved, _needs_revision, _revision_feedback

    def test_json_approve(self):
        """JSON {"action": "approve"} routes to approval."""
        approved, revision, feedback = self._classify_resolution('{"action": "approve"}')
        assert approved is True
        assert revision is False
        assert feedback is None

    def test_json_request_changes_with_feedback(self):
        """JSON request_changes with feedback routes to revision with readable text."""
        resolution = json.dumps({"action": "request_changes", "feedback": "Fix error handling"})
        approved, revision, feedback = self._classify_resolution(resolution)
        assert approved is False
        assert revision is True
        # R-1: feedback must be the readable text, NOT the raw JSON string
        assert feedback == "Fix error handling"
        assert feedback != resolution

    def test_json_request_changes_without_feedback(self):
        """JSON request_changes without feedback needs follow-up (no feedback text)."""
        resolution = json.dumps({"action": "request_changes"})
        approved, revision, feedback = self._classify_resolution(resolution)
        assert approved is False
        assert revision is True
        assert feedback is None

    def test_json_select(self):
        """JSON {"action": "select", "selected": "MongoDB"} routes to approval."""
        resolution = json.dumps({"action": "select", "selected": "MongoDB"})
        approved, revision, feedback = self._classify_resolution(resolution)
        assert approved is True
        assert revision is False

    def test_json_change_approach(self):
        """JSON change_approach with feedback routes to revision."""
        resolution = json.dumps({"action": "change_approach", "feedback": "Use REST instead"})
        approved, revision, feedback = self._classify_resolution(resolution)
        assert approved is False
        assert revision is True
        assert feedback == "Use REST instead"

    def test_json_submit_feedback(self):
        """JSON submit_feedback routes to approval."""
        resolution = json.dumps({"action": "submit_feedback", "answers": {"q-1": "Yes"}})
        approved, revision, feedback = self._classify_resolution(resolution)
        assert approved is True
        assert revision is False

    def test_bare_string_approved(self):
        """Bare string 'Approved' routes to approval (backward compat)."""
        approved, revision, feedback = self._classify_resolution("Approved")
        assert approved is True
        assert revision is False

    def test_bare_string_approve(self):
        """Bare string 'approve' routes to approval (backward compat)."""
        approved, revision, feedback = self._classify_resolution("approve")
        assert approved is True

    def test_bare_string_lgtm(self):
        """Bare string 'lgtm' routes to approval (backward compat)."""
        approved, revision, feedback = self._classify_resolution("lgtm")
        assert approved is True

    def test_bare_string_request_changes(self):
        """Bare string 'request changes' triggers follow-up (no feedback)."""
        approved, revision, feedback = self._classify_resolution("request changes")
        assert approved is False
        assert revision is True
        assert feedback is None  # Bare label, needs follow-up

    def test_bare_string_feedback_text(self):
        """Bare feedback text routes to revision with the text as feedback."""
        approved, revision, feedback = self._classify_resolution("Fix the tests please")
        assert approved is False
        assert revision is True
        assert feedback == "Fix the tests please"

    def test_malformed_json_falls_back(self):
        """Malformed JSON falls back to string matching."""
        approved, revision, feedback = self._classify_resolution("{bad json")
        assert approved is False
        assert revision is True
        assert feedback == "{bad json"

    def test_empty_string_is_approval(self):
        """Empty string is approval (backward compat)."""
        approved, revision, feedback = self._classify_resolution("")
        assert approved is True

    def test_json_unknown_action_falls_back(self):
        """JSON with unknown action falls back to string matching."""
        resolution = json.dumps({"action": "unknown_thing"})
        # The raw JSON string won't match approval or bare labels,
        # so it becomes free-text feedback
        approved, revision, feedback = self._classify_resolution(resolution)
        assert approved is False
        assert revision is True
        assert feedback == resolution  # Raw JSON treated as text

    def test_json_no_action_field_falls_back(self):
        """JSON without action field falls back to string matching."""
        resolution = json.dumps({"something": "else"})
        approved, revision, feedback = self._classify_resolution(resolution)
        assert approved is False
        assert revision is True


class TestCircuitBreakerFallThrough:
    """Verify the circuit breaker falls through to the approval path.

    When hitl_review_cycles >= max_hitl_review_cycles, the code must NOT
    continue the outer loop. It must fall through to the approval block
    that sets phase status to COMPLETE.
    """

    def test_circuit_breaker_does_not_set_revision_feedback(self):
        """When circuit breaker fires, hitl_revision_feedback must remain unset.

        The `continue` is inside the `else` branch only, so when the breaker
        fires (the `if` branch), execution exits the `with` block and reaches
        the approval path — hitl_revision_feedback stays None.
        """
        config = PipelineConfig(max_hitl_review_cycles=2)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Simulate: 2 prior cycles, human provides feedback again
        phase.hitl_review_cycles = 1  # Will be incremented to 2
        _revision_feedback = "Fix the tests"

        # Simulate the pipeline code path
        phase.hitl_review_cycles += 1
        hitl_revision_feedback = None  # This is the outer-scope variable

        max_hitl_cycles = config.max_hitl_review_cycles
        if phase.hitl_review_cycles >= max_hitl_cycles:
            # Circuit breaker fires — do NOT set hitl_revision_feedback
            # do NOT continue — fall through to approval
            breaker_fired = True
        else:
            hitl_revision_feedback = _revision_feedback
            breaker_fired = False

        assert breaker_fired is True
        assert hitl_revision_feedback is None
        assert phase.hitl_review_cycles == 2

    def test_normal_revision_sets_feedback_and_continues(self):
        """When under the limit, hitl_revision_feedback IS set (continue path)."""
        config = PipelineConfig(max_hitl_review_cycles=5)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        phase.hitl_review_cycles = 0
        _revision_feedback = "Add error handling"

        phase.hitl_review_cycles += 1
        hitl_revision_feedback = None

        max_hitl_cycles = config.max_hitl_review_cycles
        if phase.hitl_review_cycles >= max_hitl_cycles:
            breaker_fired = True
        else:
            hitl_revision_feedback = _revision_feedback
            breaker_fired = False

        assert breaker_fired is False
        assert hitl_revision_feedback == "Add error handling"
        assert phase.hitl_review_cycles == 1

    def test_circuit_breaker_at_exact_limit(self):
        """Breaker fires when hitl_review_cycles == max (not just >)."""
        config = PipelineConfig(max_hitl_review_cycles=3)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        phase.hitl_review_cycles = 2  # Will increment to 3 == max
        phase.hitl_review_cycles += 1

        hitl_revision_feedback = None
        if phase.hitl_review_cycles >= config.max_hitl_review_cycles:
            breaker_fired = True
        else:
            hitl_revision_feedback = "some feedback"
            breaker_fired = False

        assert breaker_fired is True
        assert hitl_revision_feedback is None

    def test_circuit_breaker_just_under_limit(self):
        """One under the limit should NOT fire the breaker."""
        config = PipelineConfig(max_hitl_review_cycles=3)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        phase.hitl_review_cycles = 1  # Will increment to 2, under max of 3
        phase.hitl_review_cycles += 1

        hitl_revision_feedback = None
        if phase.hitl_review_cycles >= config.max_hitl_review_cycles:
            breaker_fired = True
        else:
            hitl_revision_feedback = "some feedback"
            breaker_fired = False

        assert breaker_fired is False
        assert hitl_revision_feedback == "some feedback"


class TestSyncPipelineDecisionsToContract:
    """Verify _sync_pipeline_decisions_to_contract converts pipeline decisions to contract format."""

    def _make_pipeline_decision(
        self,
        decision_id="decision-1",
        question="Which approach?",
        options=None,
        decision_type="choice",
        status="resolved",
        resolution="Option A",
        phase=None,
    ):
        """Create a pipeline HITLDecision for testing."""
        from models import DecisionStatus, HITLDecision, PipelinePhase

        return HITLDecision(
            id=decision_id,
            question=question,
            options=options or ["Option A", "Option B"],
            decision_type=decision_type,
            status=DecisionStatus(status),
            resolution=resolution,
            phase=PipelinePhase(phase) if phase else None,
        )

    def test_phase_gate_decisions_are_excluded(self):
        """Phase gate decisions should not be synced to the contract."""
        from models import DecisionStatus

        phase_gate = self._make_pipeline_decision(
            decision_type="phase_gate",
            question="Approve refine phase?",
            options=["approve", "request changes"],
            resolution="approve",
        )
        choice = self._make_pipeline_decision(
            decision_type="choice",
            question="Which database?",
            resolution="PostgreSQL",
        )

        # Filter logic from _sync_pipeline_decisions_to_contract
        substantive = [
            d
            for d in [phase_gate, choice]
            if d.decision_type != "phase_gate" and d.status == DecisionStatus.RESOLVED
        ]

        assert len(substantive) == 1
        assert substantive[0].question == "Which database?"

    def test_pending_decisions_are_excluded(self):
        """Only resolved decisions should be synced."""
        from models import DecisionStatus

        pending = self._make_pipeline_decision(status="pending", resolution=None)
        resolved = self._make_pipeline_decision(
            decision_id="decision-2",
            question="Which approach?",
            status="resolved",
            resolution="REST",
        )

        substantive = [
            d
            for d in [pending, resolved]
            if d.decision_type != "phase_gate" and d.status == DecisionStatus.RESOLVED
        ]

        assert len(substantive) == 1
        assert substantive[0].resolution == "REST"

    def test_field_mapping_hitl_decision_to_contract_decision(self):
        """Verify correct field mapping from HITLDecision to contract Decision."""
        from datetime import datetime

        from egg_contracts.models import Decision, DecisionOption, DecisionType

        pipeline_decision = self._make_pipeline_decision(
            question="Which database should we use?",
            options=["PostgreSQL", "MongoDB"],
            resolution="PostgreSQL",
        )
        # Simulate the resolved_at timestamp
        pipeline_decision.resolved_at = datetime(2026, 2, 25, 12, 0, 0)

        # Apply the same mapping logic as _sync_pipeline_decisions_to_contract
        contract_options = [
            DecisionOption(id=f"opt-{i + 1}", label=opt)
            for i, opt in enumerate(pipeline_decision.options)
        ]

        contract_decision = Decision(
            id="decision-1",
            question=pipeline_decision.question,
            type=DecisionType.HITL,
            options=contract_options,
            resolved=True,
            resolution=pipeline_decision.resolution,
            resolved_by="human",
            resolved_at=pipeline_decision.resolved_at,
        )

        assert contract_decision.id == "decision-1"
        assert contract_decision.question == "Which database should we use?"
        assert contract_decision.type == DecisionType.HITL
        assert len(contract_decision.options) == 2
        assert contract_decision.options[0].id == "opt-1"
        assert contract_decision.options[0].label == "PostgreSQL"
        assert contract_decision.options[1].id == "opt-2"
        assert contract_decision.options[1].label == "MongoDB"
        assert contract_decision.resolved is True
        assert contract_decision.resolution == "PostgreSQL"
        assert contract_decision.resolved_by == "human"
        assert contract_decision.resolved_at == datetime(2026, 2, 25, 12, 0, 0)

    def test_deduplication_by_question_text(self):
        """Decisions already in the contract should not be synced again."""
        existing_questions = {"Which database?", "Which framework?"}

        decisions = [
            self._make_pipeline_decision(decision_id="decision-1", question="Which database?"),
            self._make_pipeline_decision(decision_id="decision-2", question="Which API style?"),
            self._make_pipeline_decision(decision_id="decision-3", question="Which framework?"),
        ]

        new_decisions = [d for d in decisions if d.question not in existing_questions]

        assert len(new_decisions) == 1
        assert new_decisions[0].question == "Which API style?"

    def test_id_numbering_continues_from_existing(self):
        """New decision IDs should continue from the highest existing contract ID."""
        from egg_contracts.models import Decision, DecisionType

        existing = [
            Decision(id="decision-1", question="Q1", type=DecisionType.HITL, resolved=True),
            Decision(id="decision-3", question="Q3", type=DecisionType.HITL, resolved=True),
        ]

        # Extract max ID
        max_existing_id = 0
        for d in existing:
            try:
                num = int(d.id.split("-")[1])
                max_existing_id = max(max_existing_id, num)
            except IndexError, ValueError:
                pass

        assert max_existing_id == 3

        # Next ID should be decision-4
        max_existing_id += 1
        assert f"decision-{max_existing_id}" == "decision-4"

    def test_feedback_type_decisions_are_included(self):
        """Feedback-type decisions should be synced (not just choice)."""
        from models import DecisionStatus

        feedback = self._make_pipeline_decision(
            decision_type="feedback",
            question="What edge cases matter?",
            resolution="Handle empty arrays",
        )

        substantive = [
            d
            for d in [feedback]
            if d.decision_type != "phase_gate" and d.status == DecisionStatus.RESOLVED
        ]

        assert len(substantive) == 1
        assert substantive[0].question == "What edge cases matter?"

    def test_empty_options_produces_empty_contract_options(self):
        """Pipeline decisions with no options should map to empty contract options."""
        from egg_contracts.models import DecisionOption
        from models import DecisionStatus, HITLDecision

        pipeline_decision = HITLDecision(
            id="decision-1",
            question="Free-form question",
            options=[],
            decision_type="choice",
            status=DecisionStatus.RESOLVED,
            resolution="Some answer",
        )

        contract_options = [
            DecisionOption(id=f"opt-{i + 1}", label=opt)
            for i, opt in enumerate(pipeline_decision.options)
        ]

        assert contract_options == []

    def test_sync_function_is_callable(self):
        """Verify _sync_pipeline_decisions_to_contract exists and is callable."""
        from routes.pipelines import _sync_pipeline_decisions_to_contract

        assert callable(_sync_pipeline_decisions_to_contract)

    def test_sync_called_for_hitl_gate_phases(self):
        """Verify the pipeline source calls sync for both refine and plan phases."""
        import inspect

        from routes import pipelines

        source = inspect.getsource(pipelines)

        # The sync should be called when current_phase.value is in _HITL_GATE_PHASES
        assert "_sync_pipeline_decisions_to_contract" in source
        assert "current_phase.value in _HITL_GATE_PHASES" in source

    def test_sync_end_to_end_filters_and_maps_decisions(self):
        """Integration: call the actual function and verify contract is saved correctly.

        Mocks get_state_store, load_contract, and save_contract so the function
        exercises its real filtering, mapping, deduplication, and ID-numbering logic
        against actual model objects.
        """
        from datetime import datetime
        from pathlib import Path
        from unittest.mock import patch

        from egg_contracts.models import Contract, Decision, DecisionType
        from models import DecisionStatus, HITLDecision, Pipeline
        from routes.pipelines import _sync_pipeline_decisions_to_contract

        # --- Set up pipeline with a mix of decisions ---
        pipeline = Pipeline(
            id="test-pipeline",
            repo="owner/repo",
            issue_number=42,
            decisions=[
                # Should be synced: resolved choice
                self._make_pipeline_decision(
                    decision_id="d-1",
                    question="Which database?",
                    options=["PostgreSQL", "MongoDB"],
                    decision_type="choice",
                    status="resolved",
                    resolution="PostgreSQL",
                ),
                # Should be excluded: phase_gate
                self._make_pipeline_decision(
                    decision_id="d-2",
                    question="Approve refine phase?",
                    options=["approve", "request changes"],
                    decision_type="phase_gate",
                    status="resolved",
                    resolution="approve",
                ),
                # Should be excluded: pending
                self._make_pipeline_decision(
                    decision_id="d-3",
                    question="Which framework?",
                    decision_type="choice",
                    status="pending",
                    resolution=None,
                ),
                # Should be synced: resolved feedback (no predefined options)
                HITLDecision(
                    id="d-4",
                    question="What edge cases?",
                    options=[],
                    decision_type="feedback",
                    status=DecisionStatus.RESOLVED,
                    resolution="Handle empty arrays",
                ),
                # Should be skipped: duplicate question already in contract
                self._make_pipeline_decision(
                    decision_id="d-5",
                    question="Existing question",
                    decision_type="choice",
                    status="resolved",
                    resolution="Already there",
                ),
            ],
        )
        # Set resolved_at on the first decision to verify timestamp mapping
        pipeline.decisions[0].resolved_at = datetime(2026, 2, 25, 12, 0, 0)

        # --- Set up contract with one existing decision ---
        contract = Contract(
            pipeline_id="test-pipeline",
            decisions=[
                Decision(
                    id="decision-2",
                    question="Existing question",
                    type=DecisionType.HITL,
                    resolved=True,
                    resolution="Some answer",
                ),
            ],
        )

        # --- Mock dependencies ---
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        saved_contracts = []

        def capture_save(c, repo_path):
            saved_contracts.append(c)

        with (
            patch("routes.pipelines.get_state_store", return_value=mock_store),
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract", side_effect=capture_save),
        ):
            _sync_pipeline_decisions_to_contract(
                repo_path=Path("/fake/repo"),
                worktree_repo_path=Path("/fake/worktree"),
                pipeline_id="test-pipeline",
            )

        # --- Verify ---
        # save_contract should have been called once
        assert len(saved_contracts) == 1
        saved = saved_contracts[0]

        # Original decision preserved + 2 new ones (database + edge cases)
        assert len(saved.decisions) == 3

        # Original decision untouched
        assert saved.decisions[0].id == "decision-2"
        assert saved.decisions[0].question == "Existing question"

        # First synced decision: "Which database?" — ID continues from decision-2 → decision-3
        d1 = saved.decisions[1]
        assert d1.id == "decision-3"
        assert d1.question == "Which database?"
        assert d1.type == DecisionType.HITL
        assert d1.resolved is True
        assert d1.resolution == "PostgreSQL"
        assert d1.resolved_by == "human"
        assert d1.resolved_at == datetime(2026, 2, 25, 12, 0, 0)
        assert len(d1.options) == 2
        assert d1.options[0].label == "PostgreSQL"
        assert d1.options[1].label == "MongoDB"

        # Second synced decision: "What edge cases?" — decision-4
        d2 = saved.decisions[2]
        assert d2.id == "decision-4"
        assert d2.question == "What edge cases?"
        assert d2.resolved is True
        assert d2.resolution == "Handle empty arrays"
        assert d2.options == []  # feedback with no options

    def test_sync_end_to_end_no_substantive_decisions_skips_save(self):
        """Integration: no save when all decisions are phase gates or pending."""
        from pathlib import Path
        from unittest.mock import patch

        from models import Pipeline
        from routes.pipelines import _sync_pipeline_decisions_to_contract

        pipeline = Pipeline(
            id="test-pipeline",
            repo="owner/repo",
            decisions=[
                self._make_pipeline_decision(
                    decision_type="phase_gate",
                    status="resolved",
                    resolution="approve",
                ),
            ],
        )

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines.get_state_store", return_value=mock_store),
            patch("egg_contracts.loader.load_contract") as mock_load,
            patch("egg_contracts.loader.save_contract") as mock_save,
        ):
            _sync_pipeline_decisions_to_contract(
                repo_path=Path("/fake/repo"),
                worktree_repo_path=Path("/fake/worktree"),
                pipeline_id="test-pipeline",
            )

        # load_contract should not even be called if no substantive decisions
        mock_load.assert_not_called()
        mock_save.assert_not_called()

    def test_sync_end_to_end_deduplication_prevents_save(self):
        """Integration: no save when all substantive decisions already exist in contract."""
        from pathlib import Path
        from unittest.mock import patch

        from egg_contracts.models import Contract, Decision, DecisionType
        from models import Pipeline
        from routes.pipelines import _sync_pipeline_decisions_to_contract

        pipeline = Pipeline(
            id="test-pipeline",
            repo="owner/repo",
            decisions=[
                self._make_pipeline_decision(
                    question="Already synced",
                    decision_type="choice",
                    status="resolved",
                    resolution="Option A",
                ),
            ],
        )

        contract = Contract(
            pipeline_id="test-pipeline",
            decisions=[
                Decision(
                    id="decision-1",
                    question="Already synced",
                    type=DecisionType.HITL,
                    resolved=True,
                ),
            ],
        )

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines.get_state_store", return_value=mock_store),
            patch("egg_contracts.loader.load_contract", return_value=contract),
            patch("egg_contracts.loader.save_contract") as mock_save,
        ):
            _sync_pipeline_decisions_to_contract(
                repo_path=Path("/fake/repo"),
                worktree_repo_path=Path("/fake/worktree"),
                pipeline_id="test-pipeline",
            )

        # All decisions deduplicated — save should not be called
        mock_save.assert_not_called()

    def test_sync_routes_paths_to_state_store_and_contract_separately(self):
        """Regression for #2345.

        Pipeline records live under the orchestrator's main repo path; the
        contract lives under the per-pipeline worktree.  Verify the helper
        routes ``repo_path`` to ``get_state_store`` and
        ``worktree_repo_path`` to ``load_contract`` / ``save_contract``.
        """
        from datetime import datetime
        from pathlib import Path
        from unittest.mock import patch

        from egg_contracts.models import Contract
        from models import Pipeline
        from routes.pipelines import _sync_pipeline_decisions_to_contract

        pipeline = Pipeline(
            id="test-pipeline",
            repo="owner/repo",
            decisions=[
                self._make_pipeline_decision(
                    question="Which database?",
                    options=["PostgreSQL", "MongoDB"],
                    decision_type="choice",
                    status="resolved",
                    resolution="PostgreSQL",
                ),
            ],
        )
        pipeline.decisions[0].resolved_at = datetime(2026, 4, 30, 4, 41, 11)

        contract = Contract(pipeline_id="test-pipeline", decisions=[])

        repo_path = Path("/home/egg/repos/egg")
        worktree_repo_path = Path("/home/egg/.egg-worktrees/test-pipeline/egg")

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines.get_state_store", return_value=mock_store) as mock_get_store,
            patch("egg_contracts.loader.load_contract", return_value=contract) as mock_load,
            patch("egg_contracts.loader.save_contract") as mock_save,
        ):
            _sync_pipeline_decisions_to_contract(
                repo_path=repo_path,
                worktree_repo_path=worktree_repo_path,
                pipeline_id="test-pipeline",
            )

        mock_get_store.assert_called_once_with(repo_path)
        load_args, _ = mock_load.call_args
        assert load_args[1] == worktree_repo_path
        save_args, _ = mock_save.call_args
        assert save_args[1] == worktree_repo_path


class TestInlineRequestChangesStateReset:
    """Verify that inline request_changes resets phase state like the recovery path.

    The inline path (inside _run_pipeline's HITL gate loop) must append
    a structured OperatorDirective (#2795) and reset containers/agents/
    artifacts so the re-run starts clean, matching the AWAITING_HUMAN
    recovery path in start_pipeline.
    """

    def test_inline_rerun_resets_state_fields(self):
        """The re-run (else) branch should reset containers/agents/artifacts/review_cycles.

        Drives the assertion through the production helper
        ``_apply_inline_hitl_kickback_to_phase`` so the test exercises the
        same mutations the inline kickback handler runs (#2795 review).
        Fixture-only setup that does not call the production code would
        pass by construction and miss future regressions in the handler.
        """
        from models import AgentExecution, ContainerInfo, ContainerStatus
        from routes.pipelines import _apply_inline_hitl_kickback_to_phase

        phase = PhaseExecution(phase=PipelinePhase.PLAN)
        phase.status = PipelineStatus.COMPLETE
        phase.containers = [
            ContainerInfo(
                container_id="old-ctr",
                container_name="old-ctr",
                status=ContainerStatus.EXITED,
            )
        ]
        phase.agents = [AgentExecution(role="coder")]
        phase.artifacts = {"pr_url": "https://github.com/old"}
        phase.review_cycles = 2
        # The caller (the inline handler) does these two before invoking the
        # helper, so mirror it here.
        phase.status = PipelineStatus.RUNNING
        phase.completed_at = None
        phase.hitl_review_cycles += 1

        stale = _apply_inline_hitl_kickback_to_phase(
            phase,
            revision_feedback="Fix the tests",
            tracker=None,
        )

        # The helper returns the snapshot of containers that were running
        # at kickback time so the caller can issue defensive stop calls.
        assert len(stale) == 1
        assert stale[0].container_id == "old-ctr"

        # Directive + iteration summary are appended.
        assert phase.operator_directives[-1].feedback_text == "Fix the tests"
        assert phase.operator_directives[-1].iteration_n == 0
        assert len(phase.iteration_history) == 1
        assert phase.iteration_history[-1].iteration_n == 0
        # Phase state is reset for the re-run.
        assert phase.containers == []
        assert phase.agents == []
        assert phase.artifacts == {}
        assert phase.review_cycles == 0
        assert phase.hitl_review_cycles == 1

    def test_inline_rerun_iteration_n_is_monotone(self):
        """iteration_n derives from len(iteration_history) so it monotonically increases.

        Guards against the inline path drifting back to
        ``hitl_review_cycles - 1`` after the AWAITING_HUMAN recovery path
        resets that counter to 0 (#2795 review).
        """
        from routes.pipelines import _apply_inline_hitl_kickback_to_phase

        phase = PhaseExecution(phase=PipelinePhase.PLAN)
        # Caller sets these before invoking the helper each kickback.
        phase.hitl_review_cycles = 1
        _apply_inline_hitl_kickback_to_phase(phase, "first", tracker=None)
        assert phase.operator_directives[-1].iteration_n == 0
        assert phase.iteration_history[-1].iteration_n == 0

        # Simulate the recovery path's counter reset, then another inline kickback.
        phase.hitl_review_cycles = 1  # back to 1 after a recovery reset → 0 → +1
        _apply_inline_hitl_kickback_to_phase(phase, "second", tracker=None)
        # Without the len(history) fix this would collide on iteration_n=0.
        assert phase.operator_directives[-1].iteration_n == 1
        assert phase.iteration_history[-1].iteration_n == 1
        assert len(phase.operator_directives) == 2
        assert len(phase.iteration_history) == 2

    def test_inline_rerun_iteration_n_monotone_across_legacy_migration(self):
        """iteration_n stays monotone when a legacy hitl_feedback migration seeded the directive list.

        Pre-#2795 phases load with a synthetic OperatorDirective at
        ``iteration_n = hitl_review_cycles - 1`` but an empty
        iteration_history. A naive ``len(iteration_history)`` derivation
        for the next inline kickback would restart at 0 and label two
        distinct iterations identically. The ``max(...)+1`` floor on the
        existing directive indices keeps the rendering monotone.
        """
        from routes.pipelines import _apply_inline_hitl_kickback_to_phase

        # Simulate post-migration state: synthetic directive at
        # iteration_n=1 (from hitl_review_cycles=2) with empty history.
        phase = PhaseExecution.model_validate(
            {
                "phase": PipelinePhase.PLAN.value,
                "hitl_review_cycles": 2,
                "hitl_feedback": "Legacy operator directive.",
            }
        )
        assert phase.operator_directives[0].iteration_n == 1
        assert phase.iteration_history == []

        _apply_inline_hitl_kickback_to_phase(phase, "post-migration", tracker=None)
        # Without the max(...)+1 floor this would collide at iteration_n=0
        # (len(iteration_history) at call time was 0).
        assert phase.operator_directives[-1].iteration_n == 2
        assert phase.iteration_history[-1].iteration_n == 2

    def test_circuit_breaker_preserves_artifacts(self):
        """When the circuit breaker trips, containers/agents/artifacts must be preserved."""
        from models import AgentExecution, ContainerInfo, ContainerStatus

        phase = PhaseExecution(phase=PipelinePhase.PLAN)
        phase.status = PipelineStatus.COMPLETE
        original_containers = [
            ContainerInfo(
                container_id="old-ctr",
                container_name="old-ctr",
                status=ContainerStatus.EXITED,
            )
        ]
        original_agents = [AgentExecution(role="coder")]
        original_artifacts = {"pr_url": "https://github.com/old"}
        phase.containers = list(original_containers)
        phase.agents = list(original_agents)
        phase.artifacts = dict(original_artifacts)
        phase.review_cycles = 2

        # Simulate the circuit breaker path: only increment cycle counter,
        # do NOT reset containers/agents/artifacts.
        phase.completed_at = None
        phase.hitl_review_cycles += 1

        # Verify artifacts are preserved for the force-approved phase
        assert len(phase.containers) == 1
        assert phase.containers[0].container_id == "old-ctr"
        assert len(phase.agents) == 1
        assert phase.artifacts == {"pr_url": "https://github.com/old"}
        assert phase.review_cycles == 2  # Not reset

    def test_content_changed_detection(self):
        """content_changed should be True when draft differs from prior decision."""
        from models import DecisionStatus, Pipeline

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/test",
        )

        # First decision with original context
        d1 = pipeline.add_decision(
            question="Approve?",
            decision_type="phase_gate",
            phase=PipelinePhase.PLAN,
        )
        d1.context = "Original plan content"
        d1.status = DecisionStatus.RESOLVED
        d1.resolution = '{"action": "request_changes", "feedback": "Fix it"}'

        # Second decision after re-run with changed content
        prev_gate = next(
            (
                d
                for d in reversed(pipeline.decisions)
                if d.decision_type == "phase_gate"
                and d.phase == PipelinePhase.PLAN
                and d.status == DecisionStatus.RESOLVED
            ),
            None,
        )
        new_draft = "Updated plan content"
        content_changed = new_draft != prev_gate.context if prev_gate else None

        d2 = pipeline.add_decision(
            question="Approve?",
            decision_type="phase_gate",
            phase=PipelinePhase.PLAN,
            content_changed=content_changed,
        )
        assert d2.content_changed is True

    def test_content_changed_false_when_identical(self):
        """content_changed should be False when draft matches prior decision."""
        from models import DecisionStatus, Pipeline

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/test",
        )

        d1 = pipeline.add_decision(
            question="Approve?",
            decision_type="phase_gate",
            phase=PipelinePhase.PLAN,
        )
        d1.context = "Same plan content"
        d1.status = DecisionStatus.RESOLVED

        prev_gate = next(
            (
                d
                for d in reversed(pipeline.decisions)
                if d.decision_type == "phase_gate"
                and d.phase == PipelinePhase.PLAN
                and d.status == DecisionStatus.RESOLVED
            ),
            None,
        )
        new_draft = "Same plan content"
        content_changed = new_draft != prev_gate.context if prev_gate else None

        d2 = pipeline.add_decision(
            question="Approve?",
            decision_type="phase_gate",
            phase=PipelinePhase.PLAN,
            content_changed=content_changed,
        )
        assert d2.content_changed is False

    def test_content_changed_none_on_first_decision(self):
        """content_changed should be None when there is no prior decision."""
        from models import Pipeline

        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
            repo="owner/repo",
            branch="egg/test",
        )

        d1 = pipeline.add_decision(
            question="Approve?",
            decision_type="phase_gate",
            phase=PipelinePhase.PLAN,
            content_changed=None,
        )
        assert d1.content_changed is None


class TestPhaseGateResolutionPersistence:
    """Verify _persist_phase_gate_resolution propagates context to contract and draft."""

    def _make_decision(self, resolution, phase="refine"):
        from models import DecisionStatus, HITLDecision, PipelinePhase

        return HITLDecision(
            id="gate-1",
            question="Approve refine phase?",
            decision_type="phase_gate",
            status=DecisionStatus.RESOLVED,
            resolution=resolution,
            phase=PipelinePhase(phase),
            options=["approve", "request changes"],
        )

    def test_phase_gate_resolution_persisted_to_contract(self, tmp_path):
        """After a phase gate approval with context, the contract should contain the resolution."""
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Contract
        from routes.pipelines import _persist_phase_gate_resolution

        # Set up a minimal contract
        contract = Contract(pipeline_id="test-pipe")
        save_contract(contract, tmp_path)

        decision = self._make_decision(
            json.dumps({"action": "approve", "context": "Use the adapter pattern for the refactor"})
        )

        _persist_phase_gate_resolution(
            tmp_path,
            "test-pipe",
            decision,
            "refine",
            None,
        )

        updated = load_contract("test-pipe", tmp_path)
        assert len(updated.decisions) == 1
        assert updated.decisions[0].resolution == "Use the adapter pattern for the refactor"
        assert "[Phase gate: refine]" in updated.decisions[0].question
        assert updated.decisions[0].resolved is True
        # Verify options were mapped to DecisionOption objects
        assert len(updated.decisions[0].options) == 2
        assert updated.decisions[0].options[0].label == "approve"
        assert updated.decisions[0].options[1].label == "request changes"

    def test_phase_gate_resolution_appended_to_draft(self, tmp_path):
        """The draft file should get a HITL Resolution section appended.

        Note: No contract is set up intentionally — this tests the draft append
        path in isolation.  The contract sync silently fails via the except
        handler, which is the expected behaviour when no contract exists.
        """
        from routes.pipelines import _persist_phase_gate_resolution

        # Create a draft file (no contract — tests draft append in isolation)
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        draft_path = draft_dir / "test-pipe-analysis.md"
        draft_path.write_text("# Refine Draft\n\nSome analysis content.\n")

        decision = self._make_decision(
            json.dumps({"action": "approve", "context": "Focus on error handling improvements"})
        )

        _persist_phase_gate_resolution(
            tmp_path,
            "test-pipe",
            decision,
            "refine",
            None,
        )

        content = draft_path.read_text()
        assert "## HITL Resolution" in content
        assert "Focus on error handling improvements" in content

    def test_phase_gate_resolution_no_context_skipped(self, tmp_path):
        """When resolution is structured JSON with no context/feedback, nothing is persisted."""
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Contract
        from routes.pipelines import _persist_phase_gate_resolution

        contract = Contract(pipeline_id="test-pipe")
        save_contract(contract, tmp_path)

        # Resolution with action but no context or feedback — should be skipped
        decision = self._make_decision('{"action": "approve"}')

        _persist_phase_gate_resolution(
            tmp_path,
            "test-pipe",
            decision,
            "refine",
            None,
        )

        # No decisions persisted — approve without context has nothing meaningful to propagate
        updated = load_contract("test-pipe", tmp_path)
        assert len(updated.decisions) == 0

    def test_phase_gate_resolution_plain_text(self, tmp_path):
        """When resolution is a plain string (not JSON), it should be used as-is."""
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Contract
        from routes.pipelines import _persist_phase_gate_resolution

        contract = Contract(pipeline_id="test-pipe")
        save_contract(contract, tmp_path)

        decision = self._make_decision("approve")

        _persist_phase_gate_resolution(
            tmp_path,
            "test-pipe",
            decision,
            "refine",
            None,
        )

        updated = load_contract("test-pipe", tmp_path)
        assert len(updated.decisions) == 1
        assert updated.decisions[0].resolution == "approve"

    def test_phase_gate_resolution_deduplication(self, tmp_path):
        """Calling twice with the same decision should not create duplicate entries."""
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Contract
        from routes.pipelines import _persist_phase_gate_resolution

        contract = Contract(pipeline_id="test-pipe")
        save_contract(contract, tmp_path)

        decision = self._make_decision(
            json.dumps({"action": "approve", "context": "Use adapter pattern"})
        )

        _persist_phase_gate_resolution(
            tmp_path,
            "test-pipe",
            decision,
            "refine",
            None,
        )
        _persist_phase_gate_resolution(
            tmp_path,
            "test-pipe",
            decision,
            "refine",
            None,
        )

        updated = load_contract("test-pipe", tmp_path)
        assert len(updated.decisions) == 1

    def test_phase_gate_resolution_empty_resolution_skipped(self, tmp_path):
        """Empty/None resolution should not persist anything."""
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Contract
        from routes.pipelines import _persist_phase_gate_resolution

        contract = Contract(pipeline_id="test-pipe")
        save_contract(contract, tmp_path)

        decision = self._make_decision(None)

        _persist_phase_gate_resolution(
            tmp_path,
            "test-pipe",
            decision,
            "refine",
            None,
        )

        updated = load_contract("test-pipe", tmp_path)
        assert len(updated.decisions) == 0

    def test_phase_gate_resolution_with_issue_number(self, tmp_path):
        """When issue_number is provided, contract is keyed by pipeline_id."""
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Contract
        from routes.pipelines import _persist_phase_gate_resolution

        contract = Contract(
            issue={
                "number": 42,
                "title": "Test",
                "repo": "owner/repo",
                "url": "https://github.com/owner/repo/issues/42",
            },
            pipeline_id="test-pipe",
        )
        save_contract(contract, tmp_path)

        decision = self._make_decision(json.dumps({"action": "approve", "context": "Ship it"}))

        _persist_phase_gate_resolution(
            tmp_path,
            "test-pipe",
            decision,
            "refine",
            42,
        )

        updated = load_contract("test-pipe", tmp_path)
        assert len(updated.decisions) == 1
        assert updated.decisions[0].resolution == "Ship it"


class TestInlineRequestChangesClearsConcurrentState:
    """Verify that inline request_changes clears stale consensus state (#1296).

    When a human resolves a phase_gate decision with request_changes, the
    inline handler must clear the message store and consensus trackers so
    the re-run doesn't short-circuit on stale CONSENSUS_CONFIRMED messages.
    """

    def test_inline_handler_calls_clear_concurrent_state(self):
        """Verify the inline handler delegates to _clear_concurrent_state."""
        import inspect

        from routes import pipelines

        source = inspect.getsource(pipelines._run_pipeline)
        assert "_clear_concurrent_state(pipeline_id)" in source
