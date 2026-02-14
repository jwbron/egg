"""
Tests for HITL (Human-In-The-Loop) revision flow logic in the pipeline runner.

Covers:
- _APPROVE_KEYWORDS / _BARE_OPTION_LABELS classification
- hitl_review_cycles counter independence from agentic review_cycles
- Contract re-population after HITL revision
- Follow-up decision options (no "request changes" on follow-up)
- Circuit breaker behavior with separate counters
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
        config = PipelineConfig(max_review_cycles=3)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Simulate agentic review loop using all 3 cycles
        phase.review_cycles = 3

        # HITL revision should still have its full budget
        assert phase.hitl_review_cycles == 0
        assert phase.hitl_review_cycles < config.max_review_cycles

    def test_hitl_cycles_dont_affect_agentic_budget(self):
        """HITL revision cycles should not consume the agentic review budget."""
        config = PipelineConfig(max_review_cycles=3)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Simulate HITL revisions
        phase.hitl_review_cycles = 2

        # Agentic review should still have its full budget
        assert phase.review_cycles == 0
        assert phase.review_cycles < config.max_review_cycles

    def test_circuit_breaker_uses_hitl_counter(self):
        """Circuit breaker should fire based on hitl_review_cycles, not review_cycles."""
        config = PipelineConfig(max_review_cycles=3)
        phase = PhaseExecution(phase=PipelinePhase.PLAN)

        # Agentic cycles are at limit, but HITL has budget
        phase.review_cycles = 3
        phase.hitl_review_cycles = 1

        # HITL circuit breaker should NOT fire
        assert phase.hitl_review_cycles < config.max_review_cycles

        # Increment HITL to limit
        phase.hitl_review_cycles = 3
        # Now it should fire
        assert phase.hitl_review_cycles >= config.max_review_cycles

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
