"""
Contract loading and saving utilities.

Contracts are stored in .egg/contracts/{issue-number}.json in the repository.
"""

import json
from pathlib import Path

from .models import Contract


def get_contract_path(repo_root: Path | str, issue_number: int) -> Path:
    """
    Get the path to a contract file.

    Args:
        repo_root: Path to the repository root
        issue_number: GitHub issue number

    Returns:
        Path to the contract JSON file
    """
    repo_root = Path(repo_root)
    return repo_root / ".egg" / "contracts" / f"{issue_number}.json"


def load_contract(repo_root: Path | str, issue_number: int) -> Contract | None:
    """
    Load a contract from disk.

    Args:
        repo_root: Path to the repository root
        issue_number: GitHub issue number

    Returns:
        Contract object if found, None otherwise
    """
    path = get_contract_path(repo_root, issue_number)
    if not path.exists():
        return None

    with open(path) as f:
        data = json.load(f)

    return Contract.model_validate(data)


def save_contract(
    contract: Contract,
    repo_root: Path | str,
    *,
    create_dirs: bool = True,
) -> Path:
    """
    Save a contract to disk.

    Args:
        contract: Contract to save
        repo_root: Path to the repository root
        create_dirs: Whether to create parent directories

    Returns:
        Path to the saved contract file
    """
    path = get_contract_path(repo_root, contract.issue.number)

    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(contract.model_dump(mode="json", exclude_none=True), f, indent=2, default=str)
        f.write("\n")

    return path


def contract_exists(repo_root: Path | str, issue_number: int) -> bool:
    """
    Check if a contract exists.

    Args:
        repo_root: Path to the repository root
        issue_number: GitHub issue number

    Returns:
        True if contract file exists
    """
    return get_contract_path(repo_root, issue_number).exists()
