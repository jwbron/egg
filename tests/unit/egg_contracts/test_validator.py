"""Tests for contract mutation validation."""

import pytest
import sys
from pathlib import Path

# Add shared to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_contracts.validator import (
    ContractValidator,
    MutationResult,
    ValidationError,
    ValidationReport,
    raise_if_not_allowed,
    validate_contract_mutation,
)
from egg_contracts.roles import Role


class TestContractValidator:
    """Tests for ContractValidator class."""

    def test_implementer_commit_mutation_allowed(self):
        validator = ContractValidator(Role.IMPLEMENTER)
        result = validator.validate_mutation(
            "phases.0.tasks.0.commit",
            "abc1234",
        )
        assert result.allowed is True
        assert result.role == "implementer"
        assert result.owner == "implementer"

    def test_implementer_status_mutation_blocked(self):
        validator = ContractValidator(Role.IMPLEMENTER)
        result = validator.validate_mutation(
            "phases.0.tasks.0.status",
            "complete",
        )
        assert result.allowed is False
        assert "not authorized" in result.message
        assert result.owner == "reviewer"

    def test_reviewer_status_mutation_allowed(self):
        validator = ContractValidator(Role.REVIEWER)
        result = validator.validate_mutation(
            "phases.0.tasks.0.status",
            "complete",
        )
        assert result.allowed is True
        assert result.role == "reviewer"

    def test_reviewer_commit_mutation_blocked(self):
        validator = ContractValidator(Role.REVIEWER)
        result = validator.validate_mutation(
            "phases.0.tasks.0.commit",
            "abc1234",
        )
        assert result.allowed is False
        assert "not authorized" in result.message

    def test_human_can_mutate_anything(self):
        validator = ContractValidator(Role.HUMAN)

        # Test various fields
        for path in [
            "currentPhase",
            "decisions.0.resolved",
            "phases.0.tasks.0.status",
            "phases.0.tasks.0.commit",
        ]:
            result = validator.validate_mutation(path, "value")
            assert result.allowed is True, f"Human should be able to modify {path}"


class TestValidateMutations:
    """Tests for batch mutation validation."""

    def test_all_mutations_valid(self):
        validator = ContractValidator(Role.IMPLEMENTER)
        mutations = [
            ("phases.0.tasks.0.commit", "abc1234", None),
            ("phases.0.tasks.0.notes", "Implementation done", None),
        ]
        report = validator.validate_mutations(mutations)
        assert report.valid is True
        assert len(report.mutations) == 2
        assert len(report.errors) == 0

    def test_some_mutations_blocked(self):
        validator = ContractValidator(Role.IMPLEMENTER)
        mutations = [
            ("phases.0.tasks.0.commit", "abc1234", None),  # allowed
            ("phases.0.tasks.0.status", "complete", None),  # blocked
            ("phases.0.tasks.0.notes", "Notes", None),  # allowed
        ]
        report = validator.validate_mutations(mutations)
        assert report.valid is False
        assert len(report.errors) == 1
        assert "status" in report.errors[0]

    def test_all_mutations_blocked(self):
        validator = ContractValidator(Role.IMPLEMENTER)
        mutations = [
            ("phases.0.tasks.0.status", "complete", None),
            ("currentPhase", "implement", None),
        ]
        report = validator.validate_mutations(mutations)
        assert report.valid is False
        assert len(report.errors) == 2


class TestConvenienceMethods:
    """Tests for convenience validation methods."""

    def test_check_task_commit(self):
        validator = ContractValidator(Role.IMPLEMENTER)
        result = validator.check_task_commit("task-1", "abc1234")
        assert result.allowed is True

    def test_check_task_status_as_implementer(self):
        validator = ContractValidator(Role.IMPLEMENTER)
        result = validator.check_task_status("task-1", "complete")
        assert result.allowed is False

    def test_check_task_status_as_reviewer(self):
        validator = ContractValidator(Role.REVIEWER)
        result = validator.check_task_status("task-1", "complete")
        assert result.allowed is True


class TestValidateContractMutation:
    """Tests for standalone validation function."""

    def test_validate_allowed_mutation(self):
        result = validate_contract_mutation(
            Role.IMPLEMENTER,
            "phases.0.tasks.0.commit",
            "abc1234",
        )
        assert result.allowed is True

    def test_validate_blocked_mutation(self):
        result = validate_contract_mutation(
            Role.IMPLEMENTER,
            "phases.0.tasks.0.status",
            "complete",
        )
        assert result.allowed is False


class TestRaiseIfNotAllowed:
    """Tests for exception-raising validation."""

    def test_no_exception_when_allowed(self):
        # Should not raise
        raise_if_not_allowed(
            Role.IMPLEMENTER,
            "phases.0.tasks.0.commit",
            "abc1234",
        )

    def test_raises_when_blocked(self):
        with pytest.raises(ValidationError) as exc_info:
            raise_if_not_allowed(
                Role.IMPLEMENTER,
                "phases.0.tasks.0.status",
                "complete",
            )
        error = exc_info.value
        assert error.field_path == "phases.*.tasks.*.status"
        assert error.role == "implementer"
        assert error.owner == "reviewer"

    def test_validation_error_to_dict(self):
        with pytest.raises(ValidationError) as exc_info:
            raise_if_not_allowed(
                Role.IMPLEMENTER,
                "phases.0.tasks.0.status",
            )
        error_dict = exc_info.value.to_dict()
        assert "error" in error_dict
        assert "field_path" in error_dict
        assert "role" in error_dict
        assert "owner" in error_dict


class TestMutationResult:
    """Tests for MutationResult dataclass."""

    def test_allowed_result(self):
        result = MutationResult(
            allowed=True,
            field_path="test.path",
            role="implementer",
            owner="implementer",
            message="Allowed",
            old_value="old",
            new_value="new",
        )
        assert result.allowed is True
        assert result.old_value == "old"
        assert result.new_value == "new"

    def test_blocked_result(self):
        result = MutationResult(
            allowed=False,
            field_path="test.path",
            role="implementer",
            owner="reviewer",
            message="Blocked",
        )
        assert result.allowed is False
        assert result.old_value is None
        assert result.new_value is None


class TestValidationReport:
    """Tests for ValidationReport dataclass."""

    def test_empty_report(self):
        report = ValidationReport(valid=True)
        assert report.valid is True
        assert len(report.mutations) == 0
        assert len(report.errors) == 0

    def test_add_allowed_mutation(self):
        report = ValidationReport(valid=True)
        result = MutationResult(
            allowed=True,
            field_path="test",
            role="implementer",
            owner="implementer",
            message="OK",
        )
        report.add_mutation(result)
        assert report.valid is True
        assert len(report.mutations) == 1

    def test_add_blocked_mutation_invalidates_report(self):
        report = ValidationReport(valid=True)
        result = MutationResult(
            allowed=False,
            field_path="test",
            role="implementer",
            owner="reviewer",
            message="Blocked",
        )
        report.add_mutation(result)
        assert report.valid is False
        assert len(report.errors) == 1
