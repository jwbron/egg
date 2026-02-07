"""Integration tests for role-based enforcement."""

import pytest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))

from egg_contracts import (
    Contract,
    ContractValidator,
    Issue,
    Phase,
    Task,
    ValidationError,
    load_contract,
    save_contract,
)
from egg_contracts.models import PhaseStatus, PipelinePhase, TaskStatus
from egg_contracts.roles import Role, can_modify
from egg_contracts.validator import raise_if_not_allowed


@pytest.fixture
def temp_repo():
    """Create temporary repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        (repo_root / ".egg" / "contracts").mkdir(parents=True)
        yield repo_root


@pytest.fixture
def contract_with_tasks(temp_repo):
    """Create contract with tasks."""
    contract = Contract(
        issue=Issue(number=300, title="Test", url="https://example.com/300"),
        currentPhase=PipelinePhase.IMPLEMENT,
        phases=[
            Phase(
                id="phase-1",
                name="Test",
                status=PhaseStatus.IN_PROGRESS,
                tasks=[
                    Task(id="task-1", description="Test task", status=TaskStatus.PENDING),
                ],
            ),
        ],
    )
    save_contract(contract, temp_repo)
    return contract


class TestImplementerRole:
    """Tests for implementer role restrictions."""

    def test_can_modify_commit(self, contract_with_tasks):
        """Implementer can modify task commits."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.commit") is True

    def test_can_modify_notes(self, contract_with_tasks):
        """Implementer can modify task notes."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.notes") is True

    def test_cannot_modify_status(self, contract_with_tasks):
        """Implementer cannot modify task status."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.tasks.0.status") is False

    def test_cannot_modify_phase_status(self, contract_with_tasks):
        """Implementer cannot modify phase status."""
        assert can_modify(Role.IMPLEMENTER, "phases.0.status") is False

    def test_cannot_modify_current_phase(self, contract_with_tasks):
        """Implementer cannot modify current phase."""
        assert can_modify(Role.IMPLEMENTER, "currentPhase") is False

    def test_validator_blocks_status_change(self, contract_with_tasks):
        """Validator blocks implementer from changing status."""
        validator = ContractValidator(Role.IMPLEMENTER)
        result = validator.validate_mutation("phases.0.tasks.0.status", "complete")
        assert result.allowed is False
        assert "not authorized" in result.message

    def test_validator_allows_commit_change(self, contract_with_tasks):
        """Validator allows implementer to change commit."""
        validator = ContractValidator(Role.IMPLEMENTER)
        result = validator.validate_mutation("phases.0.tasks.0.commit", "abc1234")
        assert result.allowed is True


class TestReviewerRole:
    """Tests for reviewer role restrictions."""

    def test_can_modify_status(self, contract_with_tasks):
        """Reviewer can modify task status."""
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.status") is True

    def test_can_modify_feedback(self, contract_with_tasks):
        """Reviewer can modify task feedback."""
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.feedback") is True

    def test_can_modify_phase_status(self, contract_with_tasks):
        """Reviewer can modify phase status."""
        assert can_modify(Role.REVIEWER, "phases.0.status") is True

    def test_cannot_modify_commit(self, contract_with_tasks):
        """Reviewer cannot modify task commits."""
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.commit") is False

    def test_cannot_modify_notes(self, contract_with_tasks):
        """Reviewer cannot modify task notes."""
        assert can_modify(Role.REVIEWER, "phases.0.tasks.0.notes") is False

    def test_cannot_modify_current_phase(self, contract_with_tasks):
        """Reviewer cannot modify current phase."""
        assert can_modify(Role.REVIEWER, "currentPhase") is False

    def test_cannot_resolve_decisions(self, contract_with_tasks):
        """Reviewer cannot resolve HITL decisions."""
        assert can_modify(Role.REVIEWER, "decisions.0.resolved") is False


class TestHumanRole:
    """Tests for human role (unrestricted)."""

    def test_can_modify_current_phase(self, contract_with_tasks):
        """Human can modify current phase."""
        assert can_modify(Role.HUMAN, "currentPhase") is True

    def test_can_resolve_decisions(self, contract_with_tasks):
        """Human can resolve decisions."""
        assert can_modify(Role.HUMAN, "decisions.0.resolved") is True

    def test_can_modify_any_field(self, contract_with_tasks):
        """Human can modify any field."""
        fields = [
            "currentPhase",
            "phases.0.tasks.0.status",
            "phases.0.tasks.0.commit",
            "decisions.0.resolved",
            "issue.title",
        ]
        for field in fields:
            assert can_modify(Role.HUMAN, field) is True, f"Human should modify {field}"


class TestValidationError:
    """Tests for validation error handling."""

    def test_raise_on_blocked_mutation(self, contract_with_tasks):
        """Test that validation error is raised for blocked mutations."""
        with pytest.raises(ValidationError) as exc_info:
            raise_if_not_allowed(Role.IMPLEMENTER, "phases.0.tasks.0.status", "complete")

        error = exc_info.value
        assert error.role == "implementer"
        assert error.owner == "reviewer"
        assert "status" in error.field_path

    def test_error_to_dict(self, contract_with_tasks):
        """Test error serialization."""
        with pytest.raises(ValidationError) as exc_info:
            raise_if_not_allowed(Role.IMPLEMENTER, "currentPhase", "implement")

        error_dict = exc_info.value.to_dict()
        assert "error" in error_dict
        assert "field_path" in error_dict
        assert "role" in error_dict
        assert "owner" in error_dict


class TestBatchValidation:
    """Tests for batch mutation validation."""

    def test_all_valid_mutations(self, contract_with_tasks):
        """Test batch validation with all valid mutations."""
        validator = ContractValidator(Role.IMPLEMENTER)
        mutations = [
            ("phases.0.tasks.0.commit", "abc1234", None),
            ("phases.0.tasks.0.notes", "Done", None),
        ]
        report = validator.validate_mutations(mutations)
        assert report.valid is True
        assert len(report.errors) == 0

    def test_mixed_mutations(self, contract_with_tasks):
        """Test batch validation with mixed valid/invalid mutations."""
        validator = ContractValidator(Role.IMPLEMENTER)
        mutations = [
            ("phases.0.tasks.0.commit", "abc1234", None),  # Valid
            ("phases.0.tasks.0.status", "complete", None),  # Invalid
        ]
        report = validator.validate_mutations(mutations)
        assert report.valid is False
        assert len(report.errors) == 1

    def test_all_invalid_mutations(self, contract_with_tasks):
        """Test batch validation with all invalid mutations."""
        validator = ContractValidator(Role.IMPLEMENTER)
        mutations = [
            ("phases.0.tasks.0.status", "complete", None),
            ("currentPhase", "pr", None),
        ]
        report = validator.validate_mutations(mutations)
        assert report.valid is False
        assert len(report.errors) == 2


class TestSystemFields:
    """Tests for system-managed fields (ANY role)."""

    def test_any_role_can_modify_circuit_breaker(self, contract_with_tasks):
        """Any role can modify circuit breaker fields."""
        for role in [Role.IMPLEMENTER, Role.REVIEWER, Role.HUMAN]:
            assert can_modify(role, "circuit_breaker.total_cycles") is True
            assert can_modify(role, "circuit_breaker.status") is True

    def test_any_role_can_append_audit_log(self, contract_with_tasks):
        """Any role can append to audit log."""
        for role in [Role.IMPLEMENTER, Role.REVIEWER, Role.HUMAN]:
            assert can_modify(role, "audit_log") is True

    def test_any_role_can_modify_task_cycles(self, contract_with_tasks):
        """Any role can modify task review cycles (system-managed)."""
        for role in [Role.IMPLEMENTER, Role.REVIEWER, Role.HUMAN]:
            assert can_modify(role, "phases.0.tasks.0.review_cycles") is True
