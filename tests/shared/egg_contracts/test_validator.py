"""Tests for egg_contracts.validator module."""

import pytest
from egg_contracts.models import (
    Contract,
    IssueInfo,
    Phase,
    Task,
    TaskStatus,
)
from egg_contracts.roles import Role
from egg_contracts.validator import (
    apply_mutation,
    validate_mutation,
    validate_phase_mutation,
    validate_task_mutation,
)


class TestValidateMutation:
    """Tests for validate_mutation function."""

    def test_valid_implementer_mutation(self):
        """Test that implementer can modify allowed fields."""
        result = validate_mutation(
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.valid is True
        assert result.message == "Mutation allowed"

    def test_invalid_implementer_mutation(self):
        """Test that implementer cannot modify status."""
        result = validate_mutation(
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.valid is False
        assert "implementer" in result.message.lower()
        assert "reviewer" in result.message.lower()
        assert result.required_role == "reviewer"

    def test_valid_reviewer_mutation(self):
        """Test that reviewer can modify status."""
        result = validate_mutation(
            role=Role.REVIEWER,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.valid is True

    def test_invalid_reviewer_mutation(self):
        """Test that reviewer cannot modify commit."""
        result = validate_mutation(
            role=Role.REVIEWER,
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.valid is False
        assert result.required_role == "implementer"

    def test_human_can_modify_anything(self):
        """Test that human role can modify any field."""
        # Implementer field
        result = validate_mutation(
            role=Role.HUMAN,
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.valid is True

        # Reviewer field
        result = validate_mutation(
            role=Role.HUMAN,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.valid is True

        # Human field
        result = validate_mutation(
            role=Role.HUMAN,
            field_path="decisions.0.resolved",
            new_value=True,
        )
        assert result.valid is True


class TestApplyMutation:
    """Tests for apply_mutation function."""

    @pytest.fixture
    def sample_contract(self):
        """Create a sample contract for testing."""
        return Contract(
            issue=IssueInfo(
                number=133,
                title="Test",
                url="https://example.com",
            ),
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[
                        Task(
                            id="task-1",
                            description="First task",
                            status=TaskStatus.PENDING,
                        ),
                    ],
                ),
            ],
        )

    def test_apply_valid_mutation(self, sample_contract):
        """Test applying a valid mutation."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
            reason="Implementation complete",
        )
        assert result.success is True
        assert result.contract is not None
        assert result.contract.phases[0].tasks[0].commit == "abc1234"
        assert result.audit_entry is not None
        assert result.audit_entry.actor == "egg"
        assert result.audit_entry.reason == "Implementation complete"

    def test_apply_invalid_mutation_rejected(self, sample_contract):
        """Test that invalid mutations are rejected."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.success is False
        assert "implementer" in result.message.lower()
        assert result.contract is None
        assert result.audit_entry is None

    def test_apply_reviewer_mutation(self, sample_contract):
        """Test reviewer can mark task complete."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.REVIEWER,
            actor="reviewer-agent",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE,
        )
        assert result.success is True
        assert result.contract.phases[0].tasks[0].status == TaskStatus.COMPLETE

    def test_audit_log_appended(self, sample_contract):
        """Test that audit log entry is appended."""
        initial_log_len = len(sample_contract.audit_log)

        result = apply_mutation(
            contract=sample_contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.notes",
            new_value="Added implementation notes",
        )

        assert result.success is True
        assert len(result.contract.audit_log) == initial_log_len + 1
        assert result.contract.audit_log[-1].field_path == "phases.0.tasks.0.notes"

    def test_old_value_captured(self, sample_contract):
        """Test that old value is captured in audit log."""
        # First set a value
        sample_contract.phases[0].tasks[0].notes = "Original notes"

        result = apply_mutation(
            contract=sample_contract,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="phases.0.tasks.0.notes",
            new_value="Updated notes",
        )

        assert result.success is True
        assert result.audit_entry.old_value == "Original notes"
        assert result.audit_entry.new_value == "Updated notes"


class TestValidateTaskMutation:
    """Tests for validate_task_mutation helper."""

    def test_implementer_commit_allowed(self):
        """Test implementer can set commit."""
        result = validate_task_mutation(
            role=Role.IMPLEMENTER,
            field="commit",
            new_value="abc1234",
        )
        assert result.valid is True

    def test_implementer_status_denied(self):
        """Test implementer cannot set status."""
        result = validate_task_mutation(
            role=Role.IMPLEMENTER,
            field="status",
            new_value="complete",
        )
        assert result.valid is False
        assert result.required_role == "reviewer"


class TestValidatePhaseMutation:
    """Tests for validate_phase_mutation helper."""

    def test_reviewer_status_allowed(self):
        """Test reviewer can set phase status."""
        result = validate_phase_mutation(
            role=Role.REVIEWER,
            field="status",
            new_value="complete",
        )
        assert result.valid is True

    def test_implementer_status_denied(self):
        """Test implementer cannot set phase status."""
        result = validate_phase_mutation(
            role=Role.IMPLEMENTER,
            field="status",
            new_value="complete",
        )
        assert result.valid is False


class TestErrorMessages:
    """Tests for error message formatting."""

    def test_clear_error_message_format(self):
        """Test that error messages are clear and helpful."""
        result = validate_mutation(
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.valid is False
        # Check error message contains key information
        assert "phases.*.tasks.*.status" in result.message
        assert "implementer" in result.message.lower()
        assert "reviewer" in result.message.lower()

    def test_decision_field_error_message(self):
        """Test error message for decision field."""
        result = validate_mutation(
            role=Role.REVIEWER,
            field_path="decisions.0.resolved",
            new_value=True,
        )
        assert result.valid is False
        assert "human" in result.message.lower()


class TestApplyMutationErrors:
    """Tests for error handling in apply_mutation."""

    @pytest.fixture
    def sample_contract(self):
        """Create a sample contract for testing."""
        return Contract(
            issue=IssueInfo(
                number=133,
                title="Test",
                url="https://example.com",
            ),
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[
                        Task(
                            id="task-1",
                            description="First task",
                            status=TaskStatus.PENDING,
                        ),
                    ],
                ),
            ],
        )

    def test_apply_mutation_with_invalid_path(self, sample_contract):
        """Test that invalid path returns failure."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.HUMAN,
            actor="test",
            field_path="nonexistent.path.field",
            new_value="value",
        )
        assert result.success is False
        assert "Failed to apply mutation" in result.message

    def test_apply_mutation_with_invalid_array_index(self, sample_contract):
        """Test that out-of-bounds array index returns failure."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.HUMAN,
            actor="test",
            field_path="phases.99.tasks.0.status",
            new_value="complete",
        )
        assert result.success is False
        assert "Failed to apply mutation" in result.message


class TestGetValueHelper:
    """Tests for _get_value helper function."""

    def test_get_value_from_list(self):
        """Test getting value from list."""
        from egg_contracts.validator import _get_value

        contract = Contract(
            issue=IssueInfo(number=1, title="Test", url="https://example.com"),
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[Task(id="task-1", description="First")],
                ),
            ],
        )
        value = _get_value(contract, "phases.0.tasks.0.description")
        assert value == "First"

    def test_get_value_from_dict(self):
        """Test getting value from dict."""
        from egg_contracts.validator import _get_value

        obj = {"level1": {"level2": "value"}}
        value = _get_value(obj, "level1.level2")
        assert value == "value"

    def test_get_value_raises_key_error(self):
        """Test that KeyError is raised for invalid path."""
        from egg_contracts.validator import _get_value

        obj = {"a": 1}
        with pytest.raises(KeyError):
            _get_value(obj, "a.b.c")

    def test_get_value_raises_index_error(self):
        """Test that IndexError is raised for out-of-bounds index."""
        from egg_contracts.validator import _get_value

        obj = {"data": [1, 2, 3]}
        with pytest.raises(IndexError):
            _get_value(obj, "data.99")


class TestSetValueHelper:
    """Tests for _set_value helper function."""

    def test_set_value_on_list(self):
        """Test setting value on list."""
        from egg_contracts.validator import _set_value

        contract = Contract(
            issue=IssueInfo(number=1, title="Test", url="https://example.com"),
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[Task(id="task-1", description="First")],
                ),
            ],
        )
        _set_value(contract, "phases.0.tasks.0.description", "Updated")
        assert contract.phases[0].tasks[0].description == "Updated"

    def test_set_value_on_dict(self):
        """Test setting value on dict."""
        from egg_contracts.validator import _set_value

        obj = {"level1": {"level2": "old"}}
        _set_value(obj, "level1.level2", "new")
        assert obj["level1"]["level2"] == "new"

    def test_set_value_on_list_item(self):
        """Test setting value directly on list item."""
        from egg_contracts.validator import _set_value

        obj = {"data": ["a", "b", "c"]}
        _set_value(obj, "data.1", "updated")
        assert obj["data"][1] == "updated"

    def test_set_value_raises_key_error_for_invalid_path(self):
        """Test that KeyError is raised for invalid path."""
        from egg_contracts.validator import _set_value

        obj = {"a": 1}
        with pytest.raises(KeyError):
            _set_value(obj, "a.b.c", "value")

    def test_set_value_raises_index_error(self):
        """Test that IndexError is raised for out-of-bounds index."""
        from egg_contracts.validator import _set_value

        obj = {"data": [1, 2, 3]}
        with pytest.raises(IndexError):
            _set_value(obj, "data.99", "value")

    def test_set_value_raises_key_error_for_non_container(self):
        """Test that KeyError is raised for non-container final path."""
        from egg_contracts.validator import _set_value

        obj = {"a": 123}  # Integer, not a container
        with pytest.raises(KeyError):
            _set_value(obj, "a.b", "value")
