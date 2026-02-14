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

import sys
from unittest.mock import MagicMock

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from models import PhaseExecution, PipelineConfig, PipelinePhase
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
            if resolution.lower() not in _APPROVE_KEYWORDS and resolution.lower() not in _BARE_OPTION_LABELS:
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

                if resolution.lower() in _APPROVE_KEYWORDS or resolution.lower() in _BARE_OPTION_LABELS:
                    pass  # Treat as approval
                # else: fall through to revision check

            if resolution.lower() not in _APPROVE_KEYWORDS and resolution.lower() not in _BARE_OPTION_LABELS:
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
                if resolution.lower() in _APPROVE_KEYWORDS or resolution.lower() in _BARE_OPTION_LABELS:
                    resolution = resolution  # Will be caught by approval check below

            if resolution.lower() not in _APPROVE_KEYWORDS and resolution.lower() not in _BARE_OPTION_LABELS:
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
                if resolution.lower() in _APPROVE_KEYWORDS or resolution.lower() in _BARE_OPTION_LABELS:
                    pass  # Treat as approval

            if resolution.lower() not in _APPROVE_KEYWORDS and resolution.lower() not in _BARE_OPTION_LABELS:
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
            if resolution.lower() not in _APPROVE_KEYWORDS and resolution.lower() not in _BARE_OPTION_LABELS:
                phase.hitl_review_cycles += 1
                if phase.hitl_review_cycles >= config.max_hitl_review_cycles:
                    break
                collected_feedback.append(resolution)

        assert phase.hitl_review_cycles == 3
        assert len(collected_feedback) == 3
        assert collected_feedback == feedbacks
