"""Tests for egg_contracts.roles module."""

from egg_contracts.roles import (
    FIELD_OWNERSHIP,
    Role,
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

    def test_reviewer_fields(self):
        """Test fields owned by reviewer."""
        assert get_field_owner("phases.0.tasks.0.status") == Role.REVIEWER
        assert get_field_owner("phases.0.status") == Role.REVIEWER
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
        assert get_field_owner("current_phase") == Role.SYSTEM


class TestCanModify:
    """Tests for can_modify function."""

    def test_implementer_can_modify_own_fields(self):
        """Test implementer can modify implementer fields."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.commit") is True
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.notes") is True

    def test_implementer_cannot_modify_reviewer_fields(self):
        """Test implementer cannot modify reviewer fields."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.status") is False
        assert can_modify(Role.IMPLEMENTER, "phases.0.status") is False

    def test_implementer_cannot_modify_human_fields(self):
        """Test implementer cannot modify human fields."""
        assert can_modify(Role.IMPLEMENTER, "decisions.0.resolved") is False

    def test_reviewer_can_modify_own_fields(self):
        """Test reviewer can modify reviewer fields."""
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.status") is True
        assert can_modify(Role.REVIEWER, "phases.0.status") is True
        assert can_modify(Role.REVIEWER, "acceptance_criteria.0.verified") is True

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


class TestGetRolePermissions:
    """Tests for get_role_permissions function."""

    def test_implementer_permissions(self):
        """Test implementer permission summary."""
        perms = get_role_permissions(Role.IMPLEMENTER)
        assert "phases.*.tasks.*.commit" in perms["can_modify"]
        assert "phases.*.tasks.*.notes" in perms["can_modify"]
        assert "phases.*.tasks.*.status" in perms["cannot_modify"]

    def test_reviewer_permissions(self):
        """Test reviewer permission summary."""
        perms = get_role_permissions(Role.REVIEWER)
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
        """Test that all field ownership entries use valid roles."""
        for _path, role in FIELD_OWNERSHIP.items():
            assert isinstance(role, Role)

    def test_implementer_ownership_patterns(self):
        """Test expected implementer ownership patterns exist."""
        implementer_paths = [p for p, r in FIELD_OWNERSHIP.items() if r == Role.IMPLEMENTER]
        assert "phases.*.tasks.*.commit" in implementer_paths
        assert "phases.*.tasks.*.notes" in implementer_paths

    def test_reviewer_ownership_patterns(self):
        """Test expected reviewer ownership patterns exist."""
        reviewer_paths = [p for p, r in FIELD_OWNERSHIP.items() if r == Role.REVIEWER]
        assert "phases.*.tasks.*.status" in reviewer_paths
        assert "phases.*.status" in reviewer_paths

    def test_human_ownership_patterns(self):
        """Test expected human ownership patterns exist."""
        human_paths = [p for p, r in FIELD_OWNERSHIP.items() if r == Role.HUMAN]
        assert "decisions.*.resolved" in human_paths
        assert "decisions.*.resolution" in human_paths
