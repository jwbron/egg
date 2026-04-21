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

    def __init__(self, identifier: int | str, path: Path) -> None:
        self.identifier = identifier
        self.path = path
        super().__init__(f"Contract for {identifier} not found at {path}")


class ContractValidationError(Exception):
    """Raised when a contract fails validation."""

    def __init__(self, identifier: int | str, errors: list[str]) -> None:
        self.identifier = identifier
        self.errors = errors
        super().__init__(f"Contract for {identifier} is invalid: {'; '.join(errors)}")


def _canonical_key(identifier: int | str) -> str:
    """Convert a caller-provided identifier to its canonical pipeline-id string.

    Integer issue numbers are mapped to ``issue-<N>`` so that every contract
    is keyed by a pipeline-id-shaped string on disk. String identifiers are
    returned unchanged.
    """
    if isinstance(identifier, int):
        return f"issue-{identifier}"
    return identifier


def _legacy_contract_path(identifier: int | str, repo_root: Path | None = None) -> Path | None:
    """Return the pre-unification contract path for an identifier, if one exists.

    Before contract keys were unified, issue-driven pipelines stored the
    contract at ``{issue_number}.json`` (bare integer stem). This helper
    returns that legacy path so read paths can fall back when a contract
    hasn't been migrated to the new ``issue-<N>.json`` shape yet.

    Returns ``None`` when the identifier has no distinct legacy form (e.g.,
    non-issue string pipeline IDs).
    """
    if repo_root is None:
        repo_root = Path.cwd()
    if isinstance(identifier, int):
        return repo_root / DEFAULT_CONTRACTS_DIR / f"{identifier}.json"
    if identifier.startswith("issue-"):
        suffix = identifier[len("issue-") :]
        if suffix.isdigit():
            return repo_root / DEFAULT_CONTRACTS_DIR / f"{suffix}.json"
    return None


def get_contract_path(identifier: int | str, repo_root: Path | None = None) -> Path:
    """
    Get the canonical path to a contract file.

    Args:
        identifier: Pipeline identifier — either a string (``issue-<N>``,
            ``issue-<N>-<qualifier>``, or a JIRA ticket) or an integer issue
            number (which is canonicalized to ``issue-<N>``).
        repo_root: Optional repository root path. Defaults to current directory.

    Returns:
        Path to the contract JSON file under ``.egg-state/contracts/``.
    """
    if repo_root is None:
        repo_root = Path.cwd()
    return repo_root / DEFAULT_CONTRACTS_DIR / f"{_canonical_key(identifier)}.json"


def load_contract(identifier: int | str, repo_root: Path | None = None) -> Contract:
    """
    Load a contract from disk.

    Args:
        identifier: Pipeline identifier — either a string (``issue-<N>``,
            ``issue-<N>-<qualifier>``, or a JIRA ticket) or an integer issue
            number (canonicalized to ``issue-<N>``).
        repo_root: Optional repository root path

    Returns:
        The loaded Contract

    Raises:
        ContractNotFoundError: If the contract doesn't exist
        ContractValidationError: If the contract is invalid

    Note:
        Falls back to the pre-unification ``{issue_number}.json`` filename
        when the canonical ``issue-<N>.json`` is absent, so in-flight
        pipelines created before the key unification continue to resolve.
    """
    path = get_contract_path(identifier, repo_root)

    if not path.exists():
        legacy = _legacy_contract_path(identifier, repo_root)
        if legacy is not None and legacy.exists():
            path = legacy
        else:
            raise ContractNotFoundError(identifier, path)

    try:
        with open(path) as f:
            data = json.load(f)
        return Contract.model_validate(data)
    except json.JSONDecodeError as e:
        raise ContractValidationError(identifier, [f"Invalid JSON: {e}"]) from e
    except Exception as e:
        raise ContractValidationError(identifier, [str(e)]) from e


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
    path = get_contract_path(contract.contract_key, repo_root)

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

    # Remove any legacy pre-unification file (e.g. "1759.json") once the
    # contract has been successfully rewritten under the canonical key.
    # This prevents two contract files from coexisting for one pipeline.
    legacy = _legacy_contract_path(contract.contract_key, repo_root)
    if legacy is not None and legacy != path and legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass

    return path


def contract_exists(identifier: int | str, repo_root: Path | None = None) -> bool:
    """
    Check if a contract exists.

    Args:
        identifier: Pipeline identifier (int issue number or str pipeline_id)
        repo_root: Optional repository root path

    Returns:
        True if the contract exists at either the canonical or legacy path.
    """
    if get_contract_path(identifier, repo_root).exists():
        return True
    legacy = _legacy_contract_path(identifier, repo_root)
    return legacy is not None and legacy.exists()


def create_contract(
    issue_number: int | None = None,
    title: str = "",
    url: str | None = None,
    pipeline_id: str | None = None,
    repo_root: Path | None = None,
    initial_phase: PipelinePhase = PipelinePhase.REFINE,
) -> Contract:
    """
    Create a new contract for a pipeline.

    Args:
        issue_number: Optional GitHub issue number (retained as contract
            metadata; the on-disk filename is driven by ``pipeline_id``).
        title: Issue/task title
        url: Optional issue URL
        pipeline_id: Canonical pipeline ID. When ``issue_number`` is given
            without an explicit ``pipeline_id``, defaults to ``issue-<N>``.
        repo_root: Optional repository root path
        initial_phase: Initial pipeline phase

    Returns:
        The newly created Contract
    """
    issue = IssueInfo(number=issue_number, title=title, url=url or "") if issue_number else None
    if pipeline_id is None and issue_number is not None:
        pipeline_id = f"issue-{issue_number}"
    contract = Contract(
        issue=issue,
        pipeline_id=pipeline_id,
        current_phase=initial_phase,
    )

    save_contract(contract, repo_root)
    return contract


def load_contract_from_branch(
    identifier: int | str,
    repo_path: Path,
    branch: str | None = None,
) -> Contract:
    """
    Load a contract from a specific git branch.

    This is useful when the gateway needs to load a contract from the
    agent's working branch rather than the current checkout.

    Args:
        identifier: Issue number (int) or pipeline ID (str)
        repo_path: Path to the repository
        branch: Optional branch name. If None, uses current checkout.

    Returns:
        The loaded Contract

    Note:
        If branch is specified, this function shells out to git to read
        the file contents from that branch.
    """
    if branch is None:
        return load_contract(identifier, repo_path)

    # Read file from specific branch using git show.
    # Try the canonical path first, then fall back to the pre-unification
    # path so contracts on legacy in-flight branches still resolve.
    import subprocess

    canonical_rel = f"{DEFAULT_CONTRACTS_DIR}/{_canonical_key(identifier)}.json"
    candidates = [canonical_rel]
    legacy = _legacy_contract_path(identifier, Path("."))
    if legacy is not None:
        legacy_rel = f"{DEFAULT_CONTRACTS_DIR}/{legacy.name}"
        if legacy_rel != canonical_rel:
            candidates.append(legacy_rel)

    last_error: subprocess.CalledProcessError | None = None
    for rel in candidates:
        try:
            result = subprocess.run(
                ["git", "show", f"{branch}:{rel}"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            last_error = e
            continue
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise ContractValidationError(identifier, [f"Invalid JSON: {e}"]) from e
        return Contract.model_validate(data)

    raise ContractNotFoundError(identifier, repo_path / canonical_rel) from last_error


def list_contracts(repo_root: Path | None = None) -> list[int | str]:
    """
    List all contract identifiers in the repository.

    Args:
        repo_root: Optional repository root path

    Returns:
        List of identifiers (int for issue contracts, str for local pipeline contracts)
    """
    if repo_root is None:
        repo_root = Path.cwd()

    contracts_dir = repo_root / DEFAULT_CONTRACTS_DIR
    if not contracts_dir.exists():
        return []

    seen: dict[int | str, None] = {}
    for path in contracts_dir.glob("*.json"):
        stem = path.stem
        # Recognize both the legacy bare-integer shape ("1759.json") and the
        # canonical ``issue-<N>.json`` shape as issue-driven pipelines so
        # callers that filter on ``isinstance(_, int)`` keep working.
        if stem.isdigit():
            # Skip legacy file when the canonical version exists — avoids
            # duplicate entries during the migration window.
            canonical = contracts_dir / f"issue-{stem}.json"
            if canonical.exists():
                continue
            seen[int(stem)] = None
        elif stem.startswith("issue-") and stem[len("issue-") :].isdigit():
            seen[int(stem[len("issue-") :])] = None
        else:
            seen[stem] = None

    return sorted(seen.keys(), key=str)


def delete_contract(identifier: int | str, repo_root: Path | None = None) -> bool:
    """
    Delete a contract from disk.

    Args:
        identifier: Pipeline identifier (int issue number or str pipeline_id)
        repo_root: Optional repository root path

    Returns:
        True if at least one contract file was deleted (canonical and/or
        legacy), False if no file was found.
    """
    deleted = False
    for candidate in (
        get_contract_path(identifier, repo_root),
        _legacy_contract_path(identifier, repo_root),
    ):
        if candidate is not None and candidate.exists():
            candidate.unlink()
            deleted = True
    return deleted


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
