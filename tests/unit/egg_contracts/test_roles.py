"""Tests for role-based field access control."""

import sys
from pathlib import Path

# Add shared to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_contracts.roles import (
    FieldAccess,
    Role,
    can_modify,
    get_allowed_fields,
    get_field_owner,
    normalize_path,
)


class TestNormalizePath:
    """Tests for path normalization."""

    def test_simple_path(self):
        assert normalize_path("currentPhase") == "currentPhase"

    def test_path_with_index(self):
        assert normalize_path("phases.0.status") == "phases.*.status"

    def test_path_with_multiple_indices(self):
        assert normalize_path("phases.0.tasks.1.commit") == "phases.*.tasks.*.commit"

    def test_path_with_named_fields(self):
        assert normalize_path("issue.number") == "issue.number"


class TestGetFieldOwner:
    """Tests for field ownership lookup."""

    def test_current_phase_owned_by_human(self):
        owner = get_field_owner("currentPhase")
        assert owner == FieldAccess.HUMAN

    def test_task_commit_owned_by_implementer(self):
        owner = get_field_owner("phases.0.tasks.0.commit")
        assert owner == FieldAccess.IMPLEMENTER

    def test_task_status_owned_by_reviewer(self):
        owner = get_field_owner("phases.0.tasks.0.status")
        assert owner == FieldAccess.REVIEWER

    def test_task_notes_owned_by_implementer(self):
        owner = get_field_owner("phases.0.tasks.0.notes")
        assert owner == FieldAccess.IMPLEMENTER

    def test_phase_status_owned_by_reviewer(self):
        owner = get_field_owner("phases.0.status")
        assert owner == FieldAccess.REVIEWER

    def test_phase_review_feedback_owned_by_reviewer(self):
        owner = get_field_owner("phases.0.review_feedback")
        assert owner == FieldAccess.REVIEWER

    def test_decision_resolved_owned_by_human(self):
        owner = get_field_owner("decisions.0.resolved")
        assert owner == FieldAccess.HUMAN

    def test_circuit_breaker_any_role(self):
        owner = get_field_owner("circuit_breaker.status")
        assert owner == FieldAccess.ANY

    def test_audit_log_any_role(self):
        owner = get_field_owner("audit_log")
        assert owner == FieldAccess.ANY

    def test_unknown_field_defaults_to_human(self):
        owner = get_field_owner("unknown.nested.field")
        assert owner == FieldAccess.HUMAN


class TestCanModify:
    """Tests for role-based modification checks."""

    # Implementer role tests
    def test_implementer_can_modify_commit(self):
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.commit") is True

    def test_implementer_can_modify_notes(self):
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.notes") is True

    def test_implementer_cannot_modify_status(self):
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.status") is False

    def test_implementer_cannot_modify_phase_status(self):
        assert can_modify(Role.IMPLEMENTER, "phases.0.status") is False

    def test_implementer_cannot_modify_decision(self):
        assert can_modify(Role.IMPLEMENTER, "decisions.0.resolved") is False

    def test_implementer_cannot_modify_current_phase(self):
        assert can_modify(Role.IMPLEMENTER, "currentPhase") is False

    # Reviewer role tests
    def test_reviewer_can_modify_task_status(self):
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.status") is True

    def test_reviewer_can_modify_phase_status(self):
        assert can_modify(Role.REVIEWER, "phases.0.status") is True

    def test_reviewer_can_modify_task_feedback(self):
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.feedback") is True

    def test_reviewer_cannot_modify_commit(self):
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.commit") is False

    def test_reviewer_cannot_modify_notes(self):
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.notes") is False

    def test_reviewer_cannot_modify_decision(self):
        assert can_modify(Role.REVIEWER, "decisions.0.resolved") is False

    def test_reviewer_cannot_modify_current_phase(self):
        assert can_modify(Role.REVIEWER, "currentPhase") is False

    # Human role tests
    def test_human_can_modify_anything(self):
        # Human can modify all fields
        assert can_modify(Role.HUMAN, "currentPhase") is True
        assert can_modify(Role.HUMAN, "decisions.0.resolved") is True
        assert can_modify(Role.HUMAN, "phases.0.tasks.0.status") is True
        assert can_modify(Role.HUMAN, "phases.0.tasks.0.commit") is True
        assert can_modify(Role.HUMAN, "unknown.field") is True

    # ANY fields
    def test_any_role_can_modify_circuit_breaker(self):
        assert can_modify(Role.IMPLEMENTER, "circuit_breaker.status") is True
        assert can_modify(Role.REVIEWER, "circuit_breaker.total_cycles") is True

    def test_any_role_can_modify_audit_log(self):
        assert can_modify(Role.IMPLEMENTER, "audit_log") is True
        assert can_modify(Role.REVIEWER, "audit_log") is True


class TestGetAllowedFields:
    """Tests for getting allowed field patterns by role."""

    def test_implementer_allowed_fields(self):
        allowed = get_allowed_fields(Role.IMPLEMENTER)
        assert "phases.*.tasks.*.commit" in allowed
        assert "phases.*.tasks.*.notes" in allowed
        assert "audit_log" in allowed
        assert "phases.*.tasks.*.status" not in allowed

    def test_reviewer_allowed_fields(self):
        allowed = get_allowed_fields(Role.REVIEWER)
        assert "phases.*.tasks.*.status" in allowed
        assert "phases.*.status" in allowed
        assert "phases.*.review_feedback" in allowed
        assert "phases.*.tasks.*.commit" not in allowed

    def test_human_allowed_fields(self):
        allowed = get_allowed_fields(Role.HUMAN)
        # Human can modify everything
        assert "currentPhase" in allowed
        assert "decisions.*.resolved" in allowed
        assert "phases.*.tasks.*.status" in allowed
        assert "phases.*.tasks.*.commit" in allowed
