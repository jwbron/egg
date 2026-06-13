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


def compose_task_description(
    description: str | None = None,
    issue_number: int | None = None,
    issue_url: str | None = None,
    jira_ticket: str | None = None,
) -> str | None:
    """Compose ``contract.task_description`` uniformly for every entry path.

    Every contract writer (initial creation, source-branch carry-over,
    mid-run restore) goes through this helper so the task-anchoring
    invariant is structural rather than re-implemented per call site
    (#3163): a pipeline with a GitHub issue or JIRA ticket always gets
    a non-empty task statement, with the task's identity stated first
    and the operator's submit-time description (if any) below it.

    Before #3163, GitHub-issue pipelines deliberately got ``None`` here
    (the #3042 "agents fetch the live body" rationale), which left the
    #3123 binding prompt section empty for the most common pipeline
    type — observed live as a refiner adopting the *previous* pipeline's
    stale draft as its task. The anchor names the issue explicitly and
    still directs agents to the live body, so the staleness concern that
    motivated the exclusion doesn't apply.

    Args:
        description: The operator's submit-time description
            (``pipeline.prompt``). For free-text pipelines this is the
            whole statement; for issue/JIRA pipelines it carries
            operator directives alongside the identity anchor.
        issue_number: GitHub issue number, when issue-backed.
        issue_url: Full URL of the GitHub issue, when known.
        jira_ticket: JIRA ticket key (e.g. ``PROJ-1234``), when
            JIRA-driven. Ignored if ``issue_number`` is set.

    Returns:
        The composed statement, or ``None`` when there is nothing to
        say (no issue, no ticket, blank description).
    """
    # NB: the GitHub-issue anchor below is mirrored by
    # ``orchestrator.routes.event_prompt._issue_anchor_fallback`` for
    # pre-#3163 contracts that lack ``task_description``. Keep the two in
    # sync — they cannot share a helper because event_prompt runs
    # standalone under the wrapper bash and cannot import this package.
    parts: list[str] = []
    if issue_number is not None:
        anchor = f"This pipeline's task is GitHub issue #{issue_number}"
        if issue_url:
            anchor += f" — {issue_url}"
        anchor += (
            f". Fetch the live issue body (`gh issue view {issue_number}`) "
            "before structural decisions. Worktree artifacts (drafts, "
            "agent outputs) that reference any other issue or pipeline "
            "are leftovers from previous runs — they are NOT your task."
        )
        parts.append(anchor)
    elif jira_ticket:
        parts.append(
            f"This pipeline's task is JIRA ticket {jira_ticket}. The "
            "description below is a snapshot taken at submit time; the "
            f"live ticket is available via `jira ticket get {jira_ticket}`."
        )
    if description and description.strip():
        parts.append(description.strip())
    return "\n\n".join(parts) if parts else None


def create_contract(
    issue_number: int | None = None,
    title: str = "",
    url: str | None = None,
    pipeline_id: str | None = None,
    repo_root: Path | None = None,
    initial_phase: PipelinePhase = PipelinePhase.REFINE,
    task_description: str | None = None,
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
        task_description: Full, untruncated task/problem statement —
            compose it with :func:`compose_task_description` so every
            entry path (GitHub issue, JIRA, free-text) anchors the task
            the same way (#3163). The event-pump model does not deliver
            the orchestrator-built spawn prompt to the agent, so this
            field is the reliable channel for the complete task
            (``egg-contract show`` + the #3123 per-event prompt
            section); ``issue.title`` is only a 100-char label. See
            #3033/#3042 for the channel's history.

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
        task_description=task_description,
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
        List of identifiers. Unqualified issue pipelines (``issue-42.json``
        or legacy ``42.json``) are returned as ``int``. Qualified pipelines
        (e.g., ``issue-42-v2.json``) and non-issue-prefixed stems (e.g.,
        local pipeline contracts) are returned as ``str``. Callers filtering
        on ``isinstance(_, int)`` will only see unqualified issue pipelines.
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
