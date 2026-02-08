"""
Contract loader and persistence.

This module handles loading, saving, and initializing contracts from
the .egg-state/contracts/ directory.

Note: The .egg/ directory (containing schemas/) holds the contract library —
shared schema definitions committed to main. The .egg-state/ directory holds
contract instances — per-issue runtime state committed only to feature branches.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import Contract, IssueInfo, PipelinePhase

# Default contracts directory relative to repo root
# Uses .egg-state/ to distinguish contract instances (per-branch runtime state)
# from .egg/schemas/ which holds the contract schema library
DEFAULT_CONTRACTS_DIR = ".egg-state/contracts"


class ContractNotFoundError(Exception):
    """Raised when a contract doesn't exist."""

    def __init__(self, issue_number: int, path: Path) -> None:
        self.issue_number = issue_number
        self.path = path
        super().__init__(f"Contract for issue #{issue_number} not found at {path}")


class ContractValidationError(Exception):
    """Raised when a contract fails validation."""

    def __init__(self, issue_number: int, errors: list[str]) -> None:
        self.issue_number = issue_number
        self.errors = errors
        super().__init__(f"Contract for issue #{issue_number} is invalid: {'; '.join(errors)}")


def get_contract_path(issue_number: int, repo_root: Path | None = None) -> Path:
    """
    Get the path to a contract file.

    Args:
        issue_number: The GitHub issue number
        repo_root: Optional repository root path. Defaults to current directory.

    Returns:
        Path to the contract JSON file
    """
    if repo_root is None:
        repo_root = Path.cwd()
    return repo_root / DEFAULT_CONTRACTS_DIR / f"{issue_number}.json"


def load_contract(issue_number: int, repo_root: Path | None = None) -> Contract:
    """
    Load a contract from disk.

    Args:
        issue_number: The GitHub issue number
        repo_root: Optional repository root path

    Returns:
        The loaded Contract

    Raises:
        ContractNotFoundError: If the contract doesn't exist
        ContractValidationError: If the contract is invalid
    """
    path = get_contract_path(issue_number, repo_root)

    if not path.exists():
        raise ContractNotFoundError(issue_number, path)

    try:
        with open(path) as f:
            data = json.load(f)
        return Contract.model_validate(data)
    except json.JSONDecodeError as e:
        raise ContractValidationError(issue_number, [f"Invalid JSON: {e}"]) from e
    except Exception as e:
        raise ContractValidationError(issue_number, [str(e)]) from e


def save_contract(contract: Contract, repo_root: Path | None = None) -> Path:
    """
    Save a contract to disk atomically.

    Uses a write-to-temp-then-rename pattern to prevent corruption if the
    process crashes mid-write.

    Args:
        contract: The contract to save
        repo_root: Optional repository root path

    Returns:
        Path where the contract was saved
    """
    path = get_contract_path(contract.issue.number, repo_root)

    # Ensure directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file first, then atomically rename
    # Using dir=path.parent ensures the temp file is on the same filesystem
    # so os.rename() is atomic
    fd, temp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(contract.model_dump(mode="json"), f, indent=2, default=str)
            f.write("\n")  # Trailing newline
        # Atomic rename
        os.rename(temp_path, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    return path


def contract_exists(issue_number: int, repo_root: Path | None = None) -> bool:
    """
    Check if a contract exists.

    Args:
        issue_number: The GitHub issue number
        repo_root: Optional repository root path

    Returns:
        True if the contract exists
    """
    path = get_contract_path(issue_number, repo_root)
    return path.exists()


def create_contract(
    issue_number: int,
    title: str,
    url: str,
    repo_root: Path | None = None,
    initial_phase: PipelinePhase = PipelinePhase.REFINE,
) -> Contract:
    """
    Create a new contract for an issue.

    Args:
        issue_number: The GitHub issue number
        title: Issue title
        url: Issue URL
        repo_root: Optional repository root path
        initial_phase: Initial pipeline phase

    Returns:
        The newly created Contract
    """
    contract = Contract(
        issue=IssueInfo(
            number=issue_number,
            title=title,
            url=url,
        ),
        current_phase=initial_phase,
    )

    save_contract(contract, repo_root)
    return contract


def load_contract_from_branch(
    issue_number: int,
    repo_path: Path,
    branch: str | None = None,
) -> Contract:
    """
    Load a contract from a specific git branch.

    This is useful when the gateway needs to load a contract from the
    agent's working branch rather than the current checkout.

    Args:
        issue_number: The GitHub issue number
        repo_path: Path to the repository
        branch: Optional branch name. If None, uses current checkout.

    Returns:
        The loaded Contract

    Note:
        If branch is specified, this function shells out to git to read
        the file contents from that branch.
    """
    if branch is None:
        return load_contract(issue_number, repo_path)

    # Read file from specific branch using git show
    import subprocess

    contract_rel_path = f"{DEFAULT_CONTRACTS_DIR}/{issue_number}.json"

    try:
        result = subprocess.run(
            ["git", "show", f"{branch}:{contract_rel_path}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        return Contract.model_validate(data)
    except subprocess.CalledProcessError as e:
        raise ContractNotFoundError(issue_number, repo_path / contract_rel_path) from e
    except json.JSONDecodeError as e:
        raise ContractValidationError(issue_number, [f"Invalid JSON: {e}"]) from e


def list_contracts(repo_root: Path | None = None) -> list[int]:
    """
    List all contract issue numbers in the repository.

    Args:
        repo_root: Optional repository root path

    Returns:
        List of issue numbers with contracts
    """
    if repo_root is None:
        repo_root = Path.cwd()

    contracts_dir = repo_root / DEFAULT_CONTRACTS_DIR
    if not contracts_dir.exists():
        return []

    issue_numbers = []
    for path in contracts_dir.glob("*.json"):
        try:
            issue_num = int(path.stem)
            issue_numbers.append(issue_num)
        except ValueError:
            continue

    return sorted(issue_numbers)


def delete_contract(issue_number: int, repo_root: Path | None = None) -> bool:
    """
    Delete a contract from disk.

    Args:
        issue_number: The GitHub issue number
        repo_root: Optional repository root path

    Returns:
        True if the contract was deleted, False if it didn't exist
    """
    path = get_contract_path(issue_number, repo_root)

    if path.exists():
        path.unlink()
        return True
    return False


def export_contract(
    contract: Contract,
    include_audit_log: bool = True,
) -> dict[str, Any]:
    """
    Export a contract as a dictionary for API responses.

    Args:
        contract: The contract to export
        include_audit_log: Whether to include the audit log

    Returns:
        Dictionary representation of the contract
    """
    data = contract.model_dump(mode="json")
    if not include_audit_log:
        data.pop("audit_log", None)
    return data
