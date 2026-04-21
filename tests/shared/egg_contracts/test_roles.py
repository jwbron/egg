"""Tests for egg_contracts.roles module."""

from egg_contracts.roles import (
    FIELD_OWNERSHIP,
    Role,
    _role_matches,
    can_modify,
    get_field_owner,
    get_role_permissions,
    normalize_path,
)


class TestNormalizePath:
    """Tests for normalize_path function."""

    def test_numeric_indices_replaced(self):
        """Test that numeric indices are replaced with wildcards."""
        assert normalize_path("phases.0.tasks.1.status") == "phases.*.tasks.*.status"

    def test_no_indices(self):
        """Test paths without indices unchanged."""
        assert normalize_path("issue.number") == "issue.number"

    def test_mixed_path(self):
        """Test path with mixed indices and names."""
        assert normalize_path("phases.0.name") == "phases.*.name"
        assert normalize_path("decisions.5.resolved") == "decisions.*.resolved"

    def test_empty_path(self):
        """Test empty path."""
        assert normalize_path("") == ""

    def test_single_component(self):
        """Test single component path."""
        assert normalize_path("schemaVersion") == "schemaVersion"


class TestGetFieldOwner:
    """Tests for get_field_owner function."""

    def test_implementer_fields(self):
        """Test fields owned by implementer."""
        assert get_field_owner("phases.0.tasks.0.commit") == Role.IMPLEMENTER
        assert get_field_owner("phases.1.tasks.5.notes") == Role.IMPLEMENTER

    def test_shared_fields(self):
        """Test fields with shared ownership (implementer + reviewer)."""
        task_status_owner = get_field_owner("phases.0.tasks.0.status")
        assert isinstance(task_status_owner, frozenset)
        assert Role.IMPLEMENTER in task_status_owner
        assert Role.REVIEWER in task_status_owner

        phase_status_owner = get_field_owner("phases.0.status")
        assert isinstance(phase_status_owner, frozenset)
        assert Role.IMPLEMENTER in phase_status_owner
        assert Role.REVIEWER in phase_status_owner

    def test_feedback_shared_ownership(self):
        """Top-level feedback field is shared between implementer and reviewer."""
        feedback_owner = get_field_owner("feedback")
        assert isinstance(feedback_owner, frozenset)
        assert feedback_owner == frozenset({Role.IMPLEMENTER, Role.REVIEWER})

        nested_owner = get_field_owner("feedback.questions.0.answer")
        assert isinstance(nested_owner, frozenset)
        assert nested_owner == frozenset({Role.IMPLEMENTER, Role.REVIEWER})

    def test_feedback_submission_human_only(self):
        """Feedback submission fields are human-only, mirroring decisions.*.resolved."""
        assert get_field_owner("feedback.submitted") == Role.HUMAN
        assert get_field_owner("feedback.submitted_by") == Role.HUMAN
        assert get_field_owner("feedback.submitted_at") == Role.HUMAN

    def test_reviewer_fields(self):
        """Test fields owned exclusively by reviewer."""
        assert get_field_owner("acceptance_criteria.0.verified") == Role.REVIEWER

    def test_human_fields(self):
        """Test fields owned by human."""
        assert get_field_owner("decisions.0.resolved") == Role.HUMAN
        assert get_field_owner("decisions.0.resolution") == Role.HUMAN
        assert get_field_owner("decisions.0.resolved_by") == Role.HUMAN

    def test_system_fields(self):
        """Test fields with default system ownership."""
        assert get_field_owner("issue.number") == Role.SYSTEM
        assert get_field_owner("schemaVersion") == Role.SYSTEM

    def test_current_phase_reviewer_owned(self):
        """Test current_phase is owned by reviewer for phase transitions."""
        # current_phase is owned by reviewer to allow implement→pr advancement
        assert get_field_owner("current_phase") == Role.REVIEWER


class TestCanModify:
    """Tests for can_modify function."""

    def test_implementer_can_modify_own_fields(self):
        """Test implementer can modify implementer fields."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.commit") is True
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.notes") is True

    def test_implementer_can_modify_shared_fields(self):
        """Test implementer can modify shared ownership fields (task/phase status)."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.status") is True
        assert can_modify(Role.IMPLEMENTER, "phases.0.status") is True

    def test_implementer_cannot_modify_reviewer_only_fields(self):
        """Test implementer cannot modify reviewer-only fields."""
        assert can_modify(Role.IMPLEMENTER, "acceptance_criteria.0.verified") is False

    def test_implementer_cannot_modify_human_fields(self):
        """Test implementer cannot modify human fields."""
        assert can_modify(Role.IMPLEMENTER, "decisions.0.resolved") is False

    def test_reviewer_can_modify_own_fields(self):
        """Test reviewer can modify reviewer fields."""
        assert can_modify(Role.REVIEWER, "acceptance_criteria.0.verified") is True

    def test_reviewer_can_modify_shared_fields(self):
        """Test reviewer can modify shared ownership fields (task/phase status)."""
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.status") is True
        assert can_modify(Role.REVIEWER, "phases.0.status") is True

    def test_reviewer_cannot_modify_implementer_fields(self):
        """Test reviewer cannot modify implementer fields."""
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.commit") is False
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.notes") is False

    def test_reviewer_cannot_modify_human_fields(self):
        """Test reviewer cannot modify human fields."""
        assert can_modify(Role.REVIEWER, "decisions.0.resolved") is False

    def test_human_can_modify_everything(self):
        """Test human can modify all fields."""
        assert can_modify(Role.HUMAN, "phases.0.tasks.0.commit") is True
        assert can_modify(Role.HUMAN, "phases.0.tasks.0.status") is True
        assert can_modify(Role.HUMAN, "decisions.0.resolved") is True
        assert can_modify(Role.HUMAN, "issue.number") is True

    def test_system_can_only_modify_system_fields(self):
        """Test system can only modify system-owned fields."""
        assert can_modify(Role.SYSTEM, "issue.number") is True
        assert can_modify(Role.SYSTEM, "schemaVersion") is True
        assert can_modify(Role.SYSTEM, "phases.0.tasks.0.status") is False
        assert can_modify(Role.SYSTEM, "phases.0.tasks.0.commit") is False

    def test_agents_can_modify_feedback(self):
        """Implementer and reviewer can write the top-level feedback field.

        Regression guard for #1768: without an explicit FIELD_OWNERSHIP entry,
        `feedback` falls through to DEFAULT_OWNER (SYSTEM) and blocks every
        agent role from calling `egg-contract add-feedback`.
        """
        assert can_modify(Role.IMPLEMENTER, "feedback") is True
        assert can_modify(Role.REVIEWER, "feedback") is True
        assert can_modify(Role.IMPLEMENTER, "feedback.questions.0.answer") is True
        assert can_modify(Role.REVIEWER, "feedback.questions.0.answer") is True

    def test_agents_cannot_modify_feedback_submission(self):
        """Agents cannot mark feedback as submitted — human-only."""
        for field in ("feedback.submitted", "feedback.submitted_by", "feedback.submitted_at"):
            assert can_modify(Role.IMPLEMENTER, field) is False
            assert can_modify(Role.REVIEWER, field) is False

    def test_system_cannot_modify_feedback(self):
        """System no longer implicitly owns feedback after #1768."""
        assert can_modify(Role.SYSTEM, "feedback") is False


class TestGetRolePermissions:
    """Tests for get_role_permissions function."""

    def test_implementer_permissions(self):
        """Test implementer permission summary."""
        perms = get_role_permissions(Role.IMPLEMENTER)
        assert "phases.*.tasks.*.commit" in perms["can_modify"]
        assert "phases.*.tasks.*.notes" in perms["can_modify"]
        # Shared fields are accessible to implementer
        assert "phases.*.tasks.*.status" in perms["can_modify"]
        assert "phases.*.status" in perms["can_modify"]

    def test_reviewer_permissions(self):
        """Test reviewer permission summary."""
        perms = get_role_permissions(Role.REVIEWER)
        # Shared fields are accessible to reviewer
        assert "phases.*.tasks.*.status" in perms["can_modify"]
        assert "phases.*.status" in perms["can_modify"]
        assert "phases.*.tasks.*.commit" in perms["cannot_modify"]

    def test_human_permissions(self):
        """Test human permission summary."""
        perms = get_role_permissions(Role.HUMAN)
        assert perms["can_modify"] == ["*"]
        assert perms["cannot_modify"] == []


class TestFieldOwnershipConfiguration:
    """Tests for FIELD_OWNERSHIP configuration."""

    def test_all_ownership_entries_valid(self):
        """Test that all field ownership entries use valid roles or frozensets of roles."""
        for _path, owner in FIELD_OWNERSHIP.items():
            if isinstance(owner, frozenset):
                for role in owner:
                    assert isinstance(role, Role)
            else:
                assert isinstance(owner, Role)

    def test_implementer_ownership_patterns(self):
        """Test expected implementer ownership patterns exist."""
        implementer_paths = [
            p for p, r in FIELD_OWNERSHIP.items() if _role_matches(Role.IMPLEMENTER, r)
        ]
        assert "phases.*.tasks.*.commit" in implementer_paths
        assert "phases.*.tasks.*.notes" in implementer_paths
        assert "phases.*.commit" in implementer_paths

    def test_shared_ownership_patterns(self):
        """Test fields with shared implementer+reviewer ownership."""
        task_status = FIELD_OWNERSHIP["phases.*.tasks.*.status"]
        assert isinstance(task_status, frozenset)
        assert task_status == frozenset({Role.IMPLEMENTER, Role.REVIEWER})

        phase_status = FIELD_OWNERSHIP["phases.*.status"]
        assert isinstance(phase_status, frozenset)
        assert phase_status == frozenset({Role.IMPLEMENTER, Role.REVIEWER})

        feedback = FIELD_OWNERSHIP["feedback"]
        assert isinstance(feedback, frozenset)
        assert feedback == frozenset({Role.IMPLEMENTER, Role.REVIEWER})

    def test_reviewer_ownership_patterns(self):
        """Test expected reviewer-only ownership patterns exist."""
        reviewer_only_paths = [
            p
            for p, r in FIELD_OWNERSHIP.items()
            if r == Role.REVIEWER  # Exact match (not shared)
        ]
        assert "phases.*.review_feedback" in reviewer_only_paths

    def test_human_ownership_patterns(self):
        """Test expected human ownership patterns exist."""
        human_paths = [p for p, r in FIELD_OWNERSHIP.items() if r == Role.HUMAN]
        assert "decisions.*.resolved" in human_paths
        assert "decisions.*.resolution" in human_paths
        assert "feedback.submitted" in human_paths
        assert "feedback.submitted_by" in human_paths
        assert "feedback.submitted_at" in human_paths
