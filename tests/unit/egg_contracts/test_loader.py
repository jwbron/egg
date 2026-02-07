"""Tests for contract loading and saving."""

import json
import pytest
import sys
from pathlib import Path

# Add shared to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_contracts.loader import (
    contract_exists,
    get_contract_path,
    load_contract,
    save_contract,
)
from egg_contracts.models import Contract, Issue, Phase, Task, PipelinePhase


class TestGetContractPath:
    """Tests for contract path resolution."""

    def test_get_contract_path(self, temp_repo):
        path = get_contract_path(temp_repo, 123)
        assert path == temp_repo / ".egg" / "contracts" / "123.json"

    def test_get_contract_path_with_string(self, temp_repo):
        path = get_contract_path(str(temp_repo), 456)
        assert path == temp_repo / ".egg" / "contracts" / "456.json"


class TestSaveAndLoadContract:
    """Tests for saving and loading contracts."""

    def test_save_and_load_roundtrip(self, temp_repo, sample_contract):
        # Save the contract
        path = save_contract(sample_contract, temp_repo)
        assert path.exists()

        # Load it back
        loaded = load_contract(temp_repo, sample_contract.issue.number)
        assert loaded is not None
        assert loaded.issue.number == sample_contract.issue.number
        assert loaded.issue.title == sample_contract.issue.title
        assert len(loaded.phases) == len(sample_contract.phases)

    def test_load_nonexistent_contract(self, temp_repo):
        loaded = load_contract(temp_repo, 999)
        assert loaded is None

    def test_save_creates_directories(self, temp_repo):
        # Remove the contracts directory
        contracts_dir = temp_repo / ".egg" / "contracts"
        contracts_dir.rmdir()
        (temp_repo / ".egg").rmdir()

        contract = Contract(
            issue=Issue(number=1, title="Test", url="https://example.com/1")
        )
        path = save_contract(contract, temp_repo, create_dirs=True)
        assert path.exists()

    def test_save_with_all_fields(self, temp_repo):
        contract = Contract(
            schemaVersion="1.0",
            issue=Issue(number=42, title="Full Test", url="https://example.com/42"),
            currentPhase=PipelinePhase.IMPLEMENT,
            branch="egg/issue-42",
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[
                        Task(
                            id="task-1",
                            description="Do something",
                            commit="abc1234",
                            notes="Done",
                        )
                    ],
                )
            ],
            audit_log=[],
        )
        path = save_contract(contract, temp_repo)

        # Read raw JSON to verify structure
        with open(path) as f:
            data = json.load(f)

        assert data["schemaVersion"] == "1.0"
        assert data["currentPhase"] == "implement"
        assert data["branch"] == "egg/issue-42"
        assert len(data["phases"]) == 1
        assert data["phases"][0]["tasks"][0]["commit"] == "abc1234"


class TestContractExists:
    """Tests for contract existence check."""

    def test_contract_exists_true(self, temp_repo, sample_contract):
        save_contract(sample_contract, temp_repo)
        assert contract_exists(temp_repo, sample_contract.issue.number) is True

    def test_contract_exists_false(self, temp_repo):
        assert contract_exists(temp_repo, 999) is False


class TestContractValidation:
    """Tests for contract schema validation on load."""

    def test_load_invalid_json(self, temp_repo):
        # Write invalid JSON
        path = get_contract_path(temp_repo, 100)
        with open(path, "w") as f:
            f.write("not valid json")

        with pytest.raises(json.JSONDecodeError):
            load_contract(temp_repo, 100)

    def test_load_invalid_schema(self, temp_repo):
        # Write JSON that doesn't match schema
        path = get_contract_path(temp_repo, 101)
        with open(path, "w") as f:
            json.dump({"invalid": "schema"}, f)

        # Pydantic should raise validation error
        with pytest.raises(Exception):  # ValidationError from pydantic
            load_contract(temp_repo, 101)
