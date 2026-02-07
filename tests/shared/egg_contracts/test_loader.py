"""Tests for egg_contracts.loader module."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from egg_contracts.loader import (
    ContractNotFoundError,
    ContractValidationError,
    contract_exists,
    create_contract,
    delete_contract,
    export_contract,
    get_contract_path,
    list_contracts,
    load_contract,
    load_contract_from_branch,
    save_contract,
)
from egg_contracts.models import (
    AuditEntry,
    AuditRole,
    AuditAction,
    Contract,
    IssueInfo,
    Phase,
    PipelinePhase,
    Task,
)
from datetime import datetime, UTC


class TestGetContractPath:
    """Tests for get_contract_path function."""

    def test_returns_correct_path(self, tmp_path):
        """Test that correct path is returned."""
        path = get_contract_path(123, tmp_path)
        assert path == tmp_path / ".egg-state" / "contracts" / "123.json"

    def test_defaults_to_cwd(self):
        """Test that it defaults to current working directory."""
        path = get_contract_path(456)
        assert path.name == "456.json"
        assert ".egg-state" in str(path)
        assert "contracts" in str(path)


class TestLoadContract:
    """Tests for load_contract function."""

    def test_loads_valid_contract(self, tmp_path):
        """Test loading a valid contract."""
        # Create contract file
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "123.json"
        contract_data = {
            "schemaVersion": "1.0",
            "issue": {
                "number": 123,
                "title": "Test Issue",
                "url": "https://github.com/owner/repo/issues/123",
            },
            "current_phase": "refine",
            "phases": [],
            "decisions": [],
            "acceptance_criteria": [],
            "circuit_breaker": {
                "total_cycles": 0,
                "max_total_cycles": 10,
                "status": "closed",
            },
            "audit_log": [],
        }
        contract_file.write_text(json.dumps(contract_data))

        contract = load_contract(123, tmp_path)
        assert contract.issue.number == 123
        assert contract.issue.title == "Test Issue"

    def test_raises_not_found_error(self, tmp_path):
        """Test that ContractNotFoundError is raised for missing contracts."""
        with pytest.raises(ContractNotFoundError) as exc_info:
            load_contract(999, tmp_path)
        assert exc_info.value.issue_number == 999
        assert "999" in str(exc_info.value)

    def test_raises_validation_error_for_invalid_json(self, tmp_path):
        """Test that ContractValidationError is raised for invalid JSON."""
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "123.json"
        contract_file.write_text("{ invalid json }")

        with pytest.raises(ContractValidationError) as exc_info:
            load_contract(123, tmp_path)
        assert exc_info.value.issue_number == 123
        assert "Invalid JSON" in str(exc_info.value)

    def test_raises_validation_error_for_invalid_data(self, tmp_path):
        """Test that ContractValidationError is raised for invalid data."""
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "123.json"
        # Missing required fields
        contract_file.write_text(json.dumps({"invalid": "data"}))

        with pytest.raises(ContractValidationError) as exc_info:
            load_contract(123, tmp_path)
        assert exc_info.value.issue_number == 123


class TestSaveContract:
    """Tests for save_contract function."""

    def test_saves_contract_atomically(self, tmp_path):
        """Test that contract is saved atomically."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test Issue",
                url="https://github.com/owner/repo/issues/123",
            ),
        )

        path = save_contract(contract, tmp_path)

        assert path.exists()
        assert path.name == "123.json"

        # Verify content
        data = json.loads(path.read_text())
        assert data["issue"]["number"] == 123

    def test_creates_directory_if_missing(self, tmp_path):
        """Test that directory is created if missing."""
        contract = Contract(
            issue=IssueInfo(
                number=456,
                title="Test",
                url="https://example.com",
            ),
        )

        path = save_contract(contract, tmp_path)
        assert path.parent.exists()

    def test_overwrites_existing_contract(self, tmp_path):
        """Test that existing contract is overwritten."""
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "123.json"
        contract_file.write_text('{"old": "data"}')

        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Updated",
                url="https://example.com",
            ),
        )

        save_contract(contract, tmp_path)

        data = json.loads(contract_file.read_text())
        assert data["issue"]["title"] == "Updated"

    def test_cleans_up_temp_file_on_failure(self, tmp_path):
        """Test that temp file is cleaned up on write failure."""
        contract = Contract(
            issue=IssueInfo(
                number=789,
                title="Test",
                url="https://example.com",
            ),
        )

        # Create directory so we get past mkdir
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)

        # Mock json.dump to raise an exception after temp file is created
        with patch("egg_contracts.loader.json.dump", side_effect=IOError("Write failed")):
            with pytest.raises(IOError):
                save_contract(contract, tmp_path)

        # Verify no temp files are left behind
        temp_files = list(contract_dir.glob("*.tmp"))
        assert len(temp_files) == 0

    def test_handles_unlink_failure_gracefully(self, tmp_path):
        """Test that OSError during temp file cleanup is silently ignored."""
        import os

        contract = Contract(
            issue=IssueInfo(
                number=999,
                title="Test",
                url="https://example.com",
            ),
        )

        # Create directory so we get past mkdir
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)

        # Mock json.dump to raise, and os.unlink to also raise OSError
        with patch("egg_contracts.loader.json.dump", side_effect=IOError("Write failed")):
            with patch("os.unlink", side_effect=OSError("Permission denied")):
                with pytest.raises(IOError, match="Write failed"):
                    save_contract(contract, tmp_path)


class TestContractExists:
    """Tests for contract_exists function."""

    def test_returns_true_when_exists(self, tmp_path):
        """Test that True is returned when contract exists."""
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "123.json").write_text("{}")

        assert contract_exists(123, tmp_path) is True

    def test_returns_false_when_not_exists(self, tmp_path):
        """Test that False is returned when contract doesn't exist."""
        assert contract_exists(999, tmp_path) is False


class TestCreateContract:
    """Tests for create_contract function."""

    def test_creates_new_contract(self, tmp_path):
        """Test creating a new contract."""
        contract = create_contract(
            issue_number=789,
            title="New Feature",
            url="https://github.com/owner/repo/issues/789",
            repo_root=tmp_path,
        )

        assert contract.issue.number == 789
        assert contract.issue.title == "New Feature"
        assert contract.current_phase == PipelinePhase.REFINE

        # Verify it was saved
        assert contract_exists(789, tmp_path)

    def test_creates_with_custom_initial_phase(self, tmp_path):
        """Test creating with custom initial phase."""
        contract = create_contract(
            issue_number=100,
            title="Test",
            url="https://example.com",
            repo_root=tmp_path,
            initial_phase=PipelinePhase.IMPLEMENT,
        )

        assert contract.current_phase == PipelinePhase.IMPLEMENT


class TestLoadContractFromBranch:
    """Tests for load_contract_from_branch function."""

    def test_loads_from_current_checkout_when_branch_is_none(self, tmp_path):
        """Test that it loads from current checkout when branch is None."""
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_data = {
            "schemaVersion": "1.0",
            "issue": {
                "number": 123,
                "title": "Test",
                "url": "https://example.com",
            },
            "current_phase": "refine",
            "phases": [],
            "decisions": [],
            "acceptance_criteria": [],
            "circuit_breaker": {
                "total_cycles": 0,
                "max_total_cycles": 10,
                "status": "closed",
            },
            "audit_log": [],
        }
        (contract_dir / "123.json").write_text(json.dumps(contract_data))

        contract = load_contract_from_branch(123, tmp_path, branch=None)
        assert contract.issue.number == 123

    def test_loads_from_specific_branch(self, tmp_path):
        """Test loading from a specific git branch."""
        contract_data = {
            "schemaVersion": "1.0",
            "issue": {
                "number": 456,
                "title": "From Branch",
                "url": "https://example.com",
            },
            "current_phase": "implement",
            "phases": [],
            "decisions": [],
            "acceptance_criteria": [],
            "circuit_breaker": {
                "total_cycles": 0,
                "max_total_cycles": 10,
                "status": "closed",
            },
            "audit_log": [],
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(contract_data),
                returncode=0,
            )

            contract = load_contract_from_branch(
                456, tmp_path, branch="feature-branch"
            )

            assert contract.issue.number == 456
            assert contract.issue.title == "From Branch"
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert "git" in call_args[0][0]
            assert "show" in call_args[0][0]
            assert "feature-branch" in call_args[0][0][2]

    def test_raises_not_found_on_git_error(self, tmp_path):
        """Test that ContractNotFoundError is raised on git error."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=128,
                cmd=["git", "show"],
            )

            with pytest.raises(ContractNotFoundError):
                load_contract_from_branch(999, tmp_path, branch="nonexistent")

    def test_raises_validation_error_on_invalid_json_from_branch(self, tmp_path):
        """Test that ContractValidationError is raised for invalid JSON."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="{ invalid json }",
                returncode=0,
            )

            with pytest.raises(ContractValidationError) as exc_info:
                load_contract_from_branch(123, tmp_path, branch="bad-branch")
            assert "Invalid JSON" in str(exc_info.value)


class TestListContracts:
    """Tests for list_contracts function."""

    def test_returns_empty_list_when_no_contracts(self, tmp_path):
        """Test that empty list is returned when no contracts exist."""
        result = list_contracts(tmp_path)
        assert result == []

    def test_returns_empty_list_when_directory_missing(self, tmp_path):
        """Test that empty list is returned when contracts directory is missing."""
        result = list_contracts(tmp_path)
        assert result == []

    def test_returns_sorted_issue_numbers(self, tmp_path):
        """Test that issue numbers are returned sorted."""
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "300.json").write_text("{}")
        (contract_dir / "100.json").write_text("{}")
        (contract_dir / "200.json").write_text("{}")

        result = list_contracts(tmp_path)
        assert result == [100, 200, 300]

    def test_ignores_non_numeric_files(self, tmp_path):
        """Test that non-numeric filenames are ignored."""
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "123.json").write_text("{}")
        (contract_dir / "readme.json").write_text("{}")
        (contract_dir / "test.txt").write_text("")

        result = list_contracts(tmp_path)
        assert result == [123]

    def test_defaults_to_cwd(self, tmp_path, monkeypatch):
        """Test that it defaults to current working directory."""
        # Change to tmp_path
        monkeypatch.chdir(tmp_path)

        # Create contracts in cwd
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        (contract_dir / "456.json").write_text("{}")

        # Call without repo_root
        result = list_contracts()
        assert result == [456]


class TestDeleteContract:
    """Tests for delete_contract function."""

    def test_deletes_existing_contract(self, tmp_path):
        """Test deleting an existing contract."""
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "123.json"
        contract_file.write_text("{}")

        result = delete_contract(123, tmp_path)

        assert result is True
        assert not contract_file.exists()

    def test_returns_false_when_not_exists(self, tmp_path):
        """Test that False is returned when contract doesn't exist."""
        result = delete_contract(999, tmp_path)
        assert result is False


class TestExportContract:
    """Tests for export_contract function."""

    def test_exports_full_contract(self):
        """Test exporting a contract with all data."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
            phases=[
                Phase(id="phase-1", name="Setup"),
            ],
            audit_log=[
                AuditEntry(
                    timestamp=datetime.now(UTC),
                    actor="egg",
                    role=AuditRole.IMPLEMENTER,
                    action=AuditAction.UPDATE,
                    field_path="test",
                ),
            ],
        )

        data = export_contract(contract)

        assert data["issue"]["number"] == 123
        assert len(data["phases"]) == 1
        assert "audit_log" in data
        assert len(data["audit_log"]) == 1

    def test_exports_without_audit_log(self):
        """Test exporting a contract without audit log."""
        contract = Contract(
            issue=IssueInfo(
                number=456,
                title="Test",
                url="https://example.com",
            ),
            audit_log=[
                AuditEntry(
                    timestamp=datetime.now(UTC),
                    actor="egg",
                    role=AuditRole.IMPLEMENTER,
                    action=AuditAction.UPDATE,
                    field_path="test",
                ),
            ],
        )

        data = export_contract(contract, include_audit_log=False)

        assert data["issue"]["number"] == 456
        assert "audit_log" not in data


class TestContractNotFoundError:
    """Tests for ContractNotFoundError."""

    def test_error_attributes(self):
        """Test error has correct attributes."""
        path = Path("/some/path/123.json")
        error = ContractNotFoundError(123, path)

        assert error.issue_number == 123
        assert error.path == path
        assert "123" in str(error)
        assert str(path) in str(error)


class TestContractValidationError:
    """Tests for ContractValidationError."""

    def test_error_attributes(self):
        """Test error has correct attributes."""
        errors = ["Field 'x' is required", "Invalid value for 'y'"]
        error = ContractValidationError(456, errors)

        assert error.issue_number == 456
        assert error.errors == errors
        assert "456" in str(error)
        assert "Field 'x' is required" in str(error)
