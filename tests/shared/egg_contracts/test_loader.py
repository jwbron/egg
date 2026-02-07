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
    Contract,
    IssueInfo,
    PipelinePhase,
)


def _make_contract(
    issue_number: int = 42,
    title: str = "Test issue",
    url: str = "https://github.com/owner/repo/issues/42",
    phase: PipelinePhase = PipelinePhase.REFINE,
) -> Contract:
    """Helper to create a minimal contract for testing."""
    return Contract(
        issue=IssueInfo(number=issue_number, title=title, url=url),
        current_phase=phase,
    )


class TestContractNotFoundError:
    """Tests for ContractNotFoundError exception."""

    def test_attributes(self):
        """Test that issue_number and path are stored."""
        path = Path("/repo/.egg-state/contracts/99.json")
        err = ContractNotFoundError(99, path)
        assert err.issue_number == 99
        assert err.path == path

    def test_message_format(self):
        """Test the error message contains issue number and path."""
        path = Path("/repo/.egg-state/contracts/5.json")
        err = ContractNotFoundError(5, path)
        assert "#5" in str(err)
        assert str(path) in str(err)


class TestContractValidationError:
    """Tests for ContractValidationError exception."""

    def test_attributes(self):
        """Test that issue_number and errors are stored."""
        err = ContractValidationError(10, ["bad field", "missing value"])
        assert err.issue_number == 10
        assert err.errors == ["bad field", "missing value"]

    def test_message_format(self):
        """Test the error message contains issue number and joined errors."""
        err = ContractValidationError(10, ["error A", "error B"])
        msg = str(err)
        assert "#10" in msg
        assert "error A" in msg
        assert "error B" in msg


class TestGetContractPath:
    """Tests for get_contract_path function."""

    def test_with_repo_root(self, tmp_path):
        """Test path construction with explicit repo_root."""
        path = get_contract_path(42, repo_root=tmp_path)
        assert path == tmp_path / ".egg-state" / "contracts" / "42.json"

    def test_without_repo_root_uses_cwd(self):
        """Test path defaults to cwd when repo_root is None."""
        path = get_contract_path(7)
        expected = Path.cwd() / ".egg-state" / "contracts" / "7.json"
        assert path == expected

    def test_different_issue_numbers(self, tmp_path):
        """Test that different issue numbers produce different filenames."""
        path_1 = get_contract_path(1, repo_root=tmp_path)
        path_2 = get_contract_path(999, repo_root=tmp_path)
        assert path_1.name == "1.json"
        assert path_2.name == "999.json"
        assert path_1 != path_2


class TestLoadContract:
    """Tests for load_contract function."""

    def test_load_valid_contract(self, tmp_path):
        """Test loading a valid contract from disk."""
        contract = _make_contract()
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        contract_path = contracts_dir / "42.json"
        contract_path.write_text(json.dumps(contract.model_dump(mode="json"), indent=2))

        loaded = load_contract(42, repo_root=tmp_path)
        assert loaded.issue.number == 42
        assert loaded.issue.title == "Test issue"
        assert loaded.current_phase == PipelinePhase.REFINE

    def test_load_not_found_raises(self, tmp_path):
        """Test that loading a missing contract raises ContractNotFoundError."""
        with pytest.raises(ContractNotFoundError) as exc_info:
            load_contract(999, repo_root=tmp_path)
        assert exc_info.value.issue_number == 999

    def test_load_invalid_json_raises(self, tmp_path):
        """Test that invalid JSON raises ContractValidationError."""
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        contract_path = contracts_dir / "42.json"
        contract_path.write_text("{not valid json!!!")

        with pytest.raises(ContractValidationError) as exc_info:
            load_contract(42, repo_root=tmp_path)
        assert exc_info.value.issue_number == 42
        assert any("Invalid JSON" in e for e in exc_info.value.errors)

    def test_load_validation_error_raises(self, tmp_path):
        """Test that valid JSON with invalid schema raises ContractValidationError."""
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        contract_path = contracts_dir / "42.json"
        # Valid JSON but missing required 'issue' field
        contract_path.write_text(json.dumps({"schemaVersion": "1.0"}))

        with pytest.raises(ContractValidationError) as exc_info:
            load_contract(42, repo_root=tmp_path)
        assert exc_info.value.issue_number == 42


class TestSaveContract:
    """Tests for save_contract function."""

    def test_save_creates_file(self, tmp_path):
        """Test that save_contract creates a valid JSON file."""
        contract = _make_contract()
        path = save_contract(contract, repo_root=tmp_path)

        assert path.exists()
        assert path.name == "42.json"

        # Verify contents are valid JSON that round-trips
        data = json.loads(path.read_text())
        loaded = Contract.model_validate(data)
        assert loaded.issue.number == 42

    def test_save_creates_directories(self, tmp_path):
        """Test that save_contract creates parent directories if missing."""
        contract = _make_contract()
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        assert not contracts_dir.exists()

        save_contract(contract, repo_root=tmp_path)
        assert contracts_dir.exists()

    def test_save_overwrites_existing(self, tmp_path):
        """Test that saving to the same path overwrites the old file."""
        contract_v1 = _make_contract(title="Version 1")
        save_contract(contract_v1, repo_root=tmp_path)

        contract_v2 = _make_contract(title="Version 2")
        save_contract(contract_v2, repo_root=tmp_path)

        loaded = load_contract(42, repo_root=tmp_path)
        assert loaded.issue.title == "Version 2"

    def test_save_returns_correct_path(self, tmp_path):
        """Test that save_contract returns the expected path."""
        contract = _make_contract(issue_number=77)
        path = save_contract(contract, repo_root=tmp_path)
        expected = tmp_path / ".egg-state" / "contracts" / "77.json"
        assert path == expected

    def test_save_file_has_trailing_newline(self, tmp_path):
        """Test that saved file ends with a newline."""
        contract = _make_contract()
        path = save_contract(contract, repo_root=tmp_path)
        content = path.read_text()
        assert content.endswith("\n")

    def test_save_atomic_write_cleans_up_on_failure(self, tmp_path):
        """Test that temp file is cleaned up if write fails."""
        contract = _make_contract()
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)

        with patch("egg_contracts.loader.json.dump", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                save_contract(contract, repo_root=tmp_path)

        # No temp files should remain
        tmp_files = list(contracts_dir.glob("*.tmp"))
        assert tmp_files == []


class TestContractExists:
    """Tests for contract_exists function."""

    def test_exists_returns_true(self, tmp_path):
        """Test contract_exists returns True when contract file is present."""
        contract = _make_contract()
        save_contract(contract, repo_root=tmp_path)
        assert contract_exists(42, repo_root=tmp_path) is True

    def test_exists_returns_false(self, tmp_path):
        """Test contract_exists returns False when file is missing."""
        assert contract_exists(999, repo_root=tmp_path) is False


class TestCreateContract:
    """Tests for create_contract function."""

    def test_creates_and_saves_contract(self, tmp_path):
        """Test that create_contract creates a contract and persists it."""
        contract = create_contract(
            issue_number=55,
            title="New feature",
            url="https://github.com/owner/repo/issues/55",
            repo_root=tmp_path,
        )

        assert contract.issue.number == 55
        assert contract.issue.title == "New feature"
        assert contract.current_phase == PipelinePhase.REFINE

        # Verify it was saved to disk
        assert contract_exists(55, repo_root=tmp_path)
        loaded = load_contract(55, repo_root=tmp_path)
        assert loaded.issue.number == 55

    def test_create_with_custom_phase(self, tmp_path):
        """Test creating a contract with a non-default initial phase."""
        contract = create_contract(
            issue_number=56,
            title="Urgent fix",
            url="https://github.com/owner/repo/issues/56",
            repo_root=tmp_path,
            initial_phase=PipelinePhase.IMPLEMENT,
        )
        assert contract.current_phase == PipelinePhase.IMPLEMENT

    def test_create_default_phase_is_refine(self, tmp_path):
        """Test that the default initial phase is REFINE."""
        contract = create_contract(
            issue_number=57,
            title="Test",
            url="https://github.com/owner/repo/issues/57",
            repo_root=tmp_path,
        )
        assert contract.current_phase == PipelinePhase.REFINE


class TestLoadContractFromBranch:
    """Tests for load_contract_from_branch function."""

    def test_none_branch_falls_back_to_load_contract(self, tmp_path):
        """Test that branch=None delegates to load_contract."""
        contract = _make_contract()
        save_contract(contract, repo_root=tmp_path)

        loaded = load_contract_from_branch(42, tmp_path, branch=None)
        assert loaded.issue.number == 42

    def test_with_branch_uses_git_show(self, tmp_path):
        """Test that specifying a branch calls git show."""
        contract = _make_contract()
        contract_json = json.dumps(contract.model_dump(mode="json"))

        mock_result = MagicMock()
        mock_result.stdout = contract_json

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            loaded = load_contract_from_branch(42, tmp_path, branch="feature/test")

        mock_run.assert_called_once_with(
            ["git", "show", "feature/test:.egg-state/contracts/42.json"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert loaded.issue.number == 42

    def test_git_error_raises_contract_not_found(self, tmp_path):
        """Test that a git error raises ContractNotFoundError."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git show"),
        ):
            with pytest.raises(ContractNotFoundError) as exc_info:
                load_contract_from_branch(42, tmp_path, branch="nonexistent")
            assert exc_info.value.issue_number == 42

    def test_invalid_json_from_branch_raises_validation_error(self, tmp_path):
        """Test that invalid JSON from git show raises ContractValidationError."""
        mock_result = MagicMock()
        mock_result.stdout = "not valid json"

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(ContractValidationError) as exc_info:
                load_contract_from_branch(42, tmp_path, branch="bad-branch")
            assert exc_info.value.issue_number == 42
            assert any("Invalid JSON" in e for e in exc_info.value.errors)


class TestListContracts:
    """Tests for list_contracts function."""

    def test_no_contracts_dir(self, tmp_path):
        """Test list_contracts returns empty list when directory doesn't exist."""
        result = list_contracts(repo_root=tmp_path)
        assert result == []

    def test_empty_contracts_dir(self, tmp_path):
        """Test list_contracts returns empty list when directory is empty."""
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        result = list_contracts(repo_root=tmp_path)
        assert result == []

    def test_lists_contract_numbers_sorted(self, tmp_path):
        """Test that contracts are listed as sorted issue numbers."""
        create_contract(100, "Issue 100", "https://example.com/100", repo_root=tmp_path)
        create_contract(5, "Issue 5", "https://example.com/5", repo_root=tmp_path)
        create_contract(42, "Issue 42", "https://example.com/42", repo_root=tmp_path)

        result = list_contracts(repo_root=tmp_path)
        assert result == [5, 42, 100]

    def test_skips_non_numeric_filenames(self, tmp_path):
        """Test that non-numeric JSON files are ignored."""
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)

        # Create a valid contract
        create_contract(10, "Issue 10", "https://example.com/10", repo_root=tmp_path)

        # Create non-numeric JSON files that should be skipped
        (contracts_dir / "notes.json").write_text("{}")
        (contracts_dir / "README.json").write_text("{}")

        result = list_contracts(repo_root=tmp_path)
        assert result == [10]


class TestDeleteContract:
    """Tests for delete_contract function."""

    def test_delete_existing_contract(self, tmp_path):
        """Test deleting an existing contract returns True."""
        create_contract(42, "Test", "https://example.com/42", repo_root=tmp_path)
        assert contract_exists(42, repo_root=tmp_path)

        result = delete_contract(42, repo_root=tmp_path)
        assert result is True
        assert not contract_exists(42, repo_root=tmp_path)

    def test_delete_nonexistent_contract(self, tmp_path):
        """Test deleting a missing contract returns False."""
        result = delete_contract(999, repo_root=tmp_path)
        assert result is False


class TestExportContract:
    """Tests for export_contract function."""

    def test_export_with_audit_log(self):
        """Test export includes audit_log by default."""
        contract = _make_contract()
        data = export_contract(contract)

        assert isinstance(data, dict)
        assert "audit_log" in data
        assert data["issue"]["number"] == 42

    def test_export_without_audit_log(self):
        """Test export excludes audit_log when include_audit_log=False."""
        contract = _make_contract()
        data = export_contract(contract, include_audit_log=False)

        assert "audit_log" not in data
        assert data["issue"]["number"] == 42

    def test_export_preserves_all_fields(self):
        """Test that export includes all contract fields."""
        contract = _make_contract()
        data = export_contract(contract)

        assert "schemaVersion" in data
        assert "issue" in data
        assert "current_phase" in data
        assert "phases" in data
        assert "decisions" in data
        assert "circuit_breaker" in data
        assert "acceptance_criteria" in data
