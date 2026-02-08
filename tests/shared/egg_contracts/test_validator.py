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


class TestArrayAppend:
    """Tests for array append functionality in _set_value."""

    @pytest.fixture
    def contract_with_decisions(self):
        """Create a contract with an empty decisions list."""
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
            decisions=[],
        )

    def test_append_to_empty_array(self, contract_with_decisions):
        """Test appending to an empty decisions array."""
        decision = {
            "id": "decision-1",
            "question": "Test question",
            "resolved": False,
        }
        result = apply_mutation(
            contract=contract_with_decisions,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="decisions.0",
            new_value=decision,
            reason="Created decision",
        )
        assert result.success is True
        assert len(result.contract.decisions) == 1
        assert result.contract.decisions[0]["id"] == "decision-1"

    def test_append_to_existing_array(self, contract_with_decisions):
        """Test appending a second item to an array."""
        # First add one decision
        decision1 = {"id": "decision-1", "question": "Q1", "resolved": False}
        contract_with_decisions.decisions.append(decision1)

        # Now append another
        decision2 = {"id": "decision-2", "question": "Q2", "resolved": False}
        result = apply_mutation(
            contract=contract_with_decisions,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="decisions.1",
            new_value=decision2,
            reason="Created second decision",
        )
        assert result.success is True
        assert len(result.contract.decisions) == 2
        assert result.contract.decisions[1]["id"] == "decision-2"

    def test_overwrite_existing_array_element(self, contract_with_decisions):
        """Test overwriting an existing array element."""
        # Add a decision
        decision1 = {"id": "decision-1", "question": "Q1", "resolved": False}
        contract_with_decisions.decisions.append(decision1)

        # Overwrite it (IMPLEMENTER can modify decisions.*)
        updated = {"id": "decision-1", "question": "Updated Q1", "resolved": False}
        result = apply_mutation(
            contract=contract_with_decisions,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="decisions.0",
            new_value=updated,
            reason="Updated decision",
        )
        assert result.success is True
        assert result.contract.decisions[0]["question"] == "Updated Q1"

    def test_append_with_gap_fails(self, contract_with_decisions):
        """Test that appending with a gap in indices fails."""
        decision = {"id": "decision-2", "question": "Q2", "resolved": False}
        result = apply_mutation(
            contract=contract_with_decisions,
            role=Role.IMPLEMENTER,
            actor="egg",
            field_path="decisions.2",  # Gap: no index 0 or 1
            new_value=decision,
            reason="Created decision",
        )
        assert result.success is False
        assert "out of range" in result.message.lower()
