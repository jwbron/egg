"""
Tests for Phase Transition module.

Tests cover:
- Valid and invalid transitions
- Role-based transition authorization
- Transition result creation
- Audit entry generation
"""

from phase_filter import PipelinePhase
from phase_transition import (
    VALID_TRANSITIONS,
    TransitionRequest,
    TransitionResult,
    TransitionRole,
    can_transition_to,
    create_audit_entry,
    get_next_phase,
    validate_transition,
)


class TestTransitionResult:
    """Tests for TransitionResult class."""

    def test_allowed_result(self):
        """Create an allowed transition result."""
        result = TransitionResult.allowed(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
            transitioned_by="egg",
        )

        assert result.success is True
        assert result.from_phase == PipelinePhase.REFINE
        assert result.to_phase == PipelinePhase.PLAN
        assert result.transitioned_by == "egg"
        assert result.transitioned_at is not None

    def test_denied_result(self):
        """Create a denied transition result."""
        result = TransitionResult.denied(
            message="Role 'implementer' cannot exit phase 'refine'",
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
        )

        assert result.success is False
        assert "implementer" in result.message
        assert result.from_phase == PipelinePhase.REFINE
        assert result.to_phase == PipelinePhase.PLAN


class TestTransitionRequest:
    """Tests for TransitionRequest class."""

    def test_from_dict(self):
        """Create TransitionRequest from dictionary."""
        data = {
            "from_phase": "refine",
            "to_phase": "plan",
            "role": "human",
            "actor": "test-user",
            "reason": "Analysis complete",
        }
        request = TransitionRequest.from_dict(data)

        assert request.from_phase == PipelinePhase.REFINE
        assert request.to_phase == PipelinePhase.PLAN
        assert request.role == TransitionRole.HUMAN
        assert request.actor == "test-user"
        assert request.reason == "Analysis complete"

    def test_from_dict_minimal(self):
        """Create TransitionRequest with minimal fields."""
        data = {
            "from_phase": "implement",
            "to_phase": "pr",
            "role": "reviewer",
        }
        request = TransitionRequest.from_dict(data)

        assert request.from_phase == PipelinePhase.IMPLEMENT
        assert request.to_phase == PipelinePhase.PR
        assert request.actor == "unknown"
        assert request.reason is None


class TestValidTransitions:
    """Tests for the valid transitions graph."""

    def test_refine_to_plan(self):
        """Refine can only transition to plan."""
        assert PipelinePhase.PLAN in VALID_TRANSITIONS[PipelinePhase.REFINE]
        assert len(VALID_TRANSITIONS[PipelinePhase.REFINE]) == 1

    def test_plan_to_implement(self):
        """Plan can only transition to implement."""
        assert PipelinePhase.IMPLEMENT in VALID_TRANSITIONS[PipelinePhase.PLAN]
        assert len(VALID_TRANSITIONS[PipelinePhase.PLAN]) == 1

    def test_implement_to_pr(self):
        """Implement can only transition to PR."""
        assert PipelinePhase.PR in VALID_TRANSITIONS[PipelinePhase.IMPLEMENT]
        assert len(VALID_TRANSITIONS[PipelinePhase.IMPLEMENT]) == 1

    def test_pr_is_terminal(self):
        """PR phase has no outgoing transitions."""
        assert len(VALID_TRANSITIONS[PipelinePhase.PR]) == 0


class TestValidateTransition:
    """Tests for validate_transition function."""

    def test_valid_transition_with_human(self):
        """Human can transition between any valid phases."""
        request = TransitionRequest(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
            role=TransitionRole.HUMAN,
            actor="test-human",
        )
        result = validate_transition(request)

        assert result.success is True
        assert result.from_phase == PipelinePhase.REFINE
        assert result.to_phase == PipelinePhase.PLAN

    def test_invalid_transition_path(self):
        """Cannot skip phases in the pipeline."""
        request = TransitionRequest(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.IMPLEMENT,  # Invalid - must go through plan
            role=TransitionRole.HUMAN,
            actor="test-human",
        )
        result = validate_transition(request)

        assert result.success is False
        assert "Invalid transition" in result.message

    def test_backwards_transition_blocked(self):
        """Cannot transition backwards in the pipeline."""
        request = TransitionRequest(
            from_phase=PipelinePhase.IMPLEMENT,
            to_phase=PipelinePhase.PLAN,
            role=TransitionRole.HUMAN,
            actor="test-human",
        )
        result = validate_transition(request)

        assert result.success is False
        assert "Invalid transition" in result.message

    def test_implementer_cannot_exit_refine(self):
        """Implementer cannot exit refine phase (requires human)."""
        request = TransitionRequest(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
            role=TransitionRole.IMPLEMENTER,
            actor="egg",
        )
        result = validate_transition(request)

        assert result.success is False
        assert "cannot exit" in result.message.lower()

    def test_implementer_cannot_exit_plan(self):
        """Implementer cannot exit plan phase (requires human)."""
        request = TransitionRequest(
            from_phase=PipelinePhase.PLAN,
            to_phase=PipelinePhase.IMPLEMENT,
            role=TransitionRole.IMPLEMENTER,
            actor="egg",
        )
        result = validate_transition(request)

        assert result.success is False

    def test_reviewer_can_exit_implement(self):
        """Reviewer can exit implement phase."""
        request = TransitionRequest(
            from_phase=PipelinePhase.IMPLEMENT,
            to_phase=PipelinePhase.PR,
            role=TransitionRole.REVIEWER,
            actor="reviewer-agent",
        )
        result = validate_transition(request)

        assert result.success is True

    def test_implementer_cannot_exit_implement(self):
        """Implementer cannot exit implement phase (requires reviewer)."""
        request = TransitionRequest(
            from_phase=PipelinePhase.IMPLEMENT,
            to_phase=PipelinePhase.PR,
            role=TransitionRole.IMPLEMENTER,
            actor="egg",
        )
        result = validate_transition(request)

        assert result.success is False

    def test_transition_from_terminal_phase(self):
        """Cannot transition from PR phase (terminal)."""
        request = TransitionRequest(
            from_phase=PipelinePhase.PR,
            to_phase=PipelinePhase.IMPLEMENT,  # Trying to go back
            role=TransitionRole.HUMAN,
            actor="test-human",
        )
        result = validate_transition(request)

        assert result.success is False


class TestRoleHierarchy:
    """Tests for role hierarchy in transitions."""

    def test_human_can_satisfy_any_requirement(self):
        """Human role can satisfy any exit requirement."""
        # Human can exit refine (requires human)
        result = can_transition_to(PipelinePhase.REFINE, PipelinePhase.PLAN, TransitionRole.HUMAN)
        assert result.success is True

        # Human can exit implement (requires reviewer)
        result = can_transition_to(PipelinePhase.IMPLEMENT, PipelinePhase.PR, TransitionRole.HUMAN)
        assert result.success is True

    def test_reviewer_can_satisfy_reviewer_and_lower(self):
        """Reviewer role can satisfy reviewer and implementer requirements."""
        # Reviewer can exit implement (requires reviewer)
        result = can_transition_to(
            PipelinePhase.IMPLEMENT, PipelinePhase.PR, TransitionRole.REVIEWER
        )
        assert result.success is True

    def test_reviewer_cannot_satisfy_human_requirement(self):
        """Reviewer cannot satisfy human requirement."""
        result = can_transition_to(
            PipelinePhase.REFINE, PipelinePhase.PLAN, TransitionRole.REVIEWER
        )
        assert result.success is False

    def test_implementer_limited_permissions(self):
        """Implementer can only satisfy implementer requirement."""
        # No phase currently requires only implementer to exit
        # But the logic should work if one existed
        result = can_transition_to(
            PipelinePhase.IMPLEMENT, PipelinePhase.PR, TransitionRole.IMPLEMENTER
        )
        assert result.success is False


class TestGetNextPhase:
    """Tests for get_next_phase function."""

    def test_refine_next_is_plan(self):
        """Next phase after refine is plan."""
        assert get_next_phase(PipelinePhase.REFINE) == PipelinePhase.PLAN

    def test_plan_next_is_implement(self):
        """Next phase after plan is implement."""
        assert get_next_phase(PipelinePhase.PLAN) == PipelinePhase.IMPLEMENT

    def test_implement_next_is_pr(self):
        """Next phase after implement is PR."""
        assert get_next_phase(PipelinePhase.IMPLEMENT) == PipelinePhase.PR

    def test_pr_next_is_none(self):
        """PR has no next phase (terminal)."""
        assert get_next_phase(PipelinePhase.PR) is None


class TestCanTransitionTo:
    """Tests for can_transition_to convenience function."""

    def test_with_strings(self):
        """Function accepts string arguments."""
        result = can_transition_to("refine", "plan", "human", "test-actor")

        assert result.success is True
        assert result.from_phase == PipelinePhase.REFINE
        assert result.to_phase == PipelinePhase.PLAN

    def test_with_enums(self):
        """Function accepts enum arguments."""
        result = can_transition_to(
            PipelinePhase.IMPLEMENT,
            PipelinePhase.PR,
            TransitionRole.REVIEWER,
            "reviewer-agent",
        )

        assert result.success is True


class TestCreateAuditEntry:
    """Tests for create_audit_entry function."""

    def test_creates_valid_entry(self):
        """Create a valid audit entry from transition result."""
        result = TransitionResult.allowed(
            from_phase=PipelinePhase.REFINE,
            to_phase=PipelinePhase.PLAN,
            transitioned_by="egg",
        )
        entry = create_audit_entry(result, TransitionRole.HUMAN, "Analysis approved")

        assert entry["action"] == "transition"
        assert entry["field_path"] == "current_phase"
        assert entry["old_value"] == "refine"
        assert entry["new_value"] == "plan"
        assert entry["role"] == "human"
        assert entry["actor"] == "egg"
        assert entry["reason"] == "Analysis approved"
        assert "timestamp" in entry

    def test_handles_none_values(self):
        """Handle None values in result."""
        result = TransitionResult.denied("Test denial")
        entry = create_audit_entry(result, TransitionRole.IMPLEMENTER)

        assert entry["old_value"] is None
        assert entry["new_value"] is None
        assert entry["actor"] == "unknown"
        assert entry["reason"] is None
