"""Environment/config resolution, ID parsers, and validators.

Pure helpers extracted verbatim from the monolithic ``contract_cli.py``
(#3312, slice-1). No behaviour change.
"""

import argparse
import os
import re
from pathlib import Path

from egg_lib.config import GATEWAY_PORT

# Regex for validating git commit SHAs (7-40 hex characters)
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def get_gateway_url() -> str:
    """Get the gateway URL from environment or default."""
    return os.environ.get("GATEWAY_URL", f"http://egg-gateway:{GATEWAY_PORT}")


def get_issue_number() -> int | None:
    """Get the current issue number from environment."""
    issue_str = os.environ.get("EGG_ISSUE_NUMBER")
    if issue_str:
        try:
            return int(issue_str)
        except ValueError:
            return None
    return None


def get_pipeline_id() -> str | None:
    """Get the pipeline ID from environment.

    Used when running in pipeline mode with JIRA tickets
    instead of GitHub issues.
    """
    return os.environ.get("EGG_PIPELINE_ID") or None


def get_contract_identifier(args: argparse.Namespace) -> int | str | None:
    """Resolve the contract identifier from args and environment.

    Priority (highest to lowest):
    1. --issue flag (int, for backward compatibility)
    2. --pipeline-id flag (str)
    3. EGG_PIPELINE_ID env var (str) — preferred because contracts are
       keyed by pipeline_id on disk; this also covers qualified pipelines
       (e.g. ``issue-1759-v2``) where the bare issue number can't
       disambiguate between multiple pipelines for the same issue.
    4. EGG_ISSUE_NUMBER env var (int) — legacy fallback.

    Returns:
        int for issue numbers, str for pipeline IDs, None if nothing found
    """
    issue_arg: int | None = args.issue
    if issue_arg is not None:
        return issue_arg
    pipeline_id_arg: str | None = getattr(args, "pipeline_id", None)
    if pipeline_id_arg is not None:
        return pipeline_id_arg
    pipeline_id = get_pipeline_id()
    if pipeline_id is not None:
        return pipeline_id
    return get_issue_number()


def get_repo_path() -> str:
    """Get the repository path from environment or default."""
    return os.environ.get("EGG_REPO_PATH", str(Path.cwd()))


def get_session_token() -> str | None:
    """Get the session token for gateway authentication."""
    # Try environment variable first
    token = os.environ.get("EGG_SESSION_TOKEN")
    if token:
        return token

    # Try reading from file (used in container)
    token_file = Path.home() / ".egg-session-token"
    if token_file.exists():
        return token_file.read_text().strip()

    return None


def get_container_id() -> str:
    """Get the container ID from environment."""
    return os.environ.get("CONTAINER_ID", "")


def _container_id_field() -> dict[str, str]:
    """Return a dict with container_id only when the env var is set.

    Used with ``**`` unpacking in POST data dicts so that an empty
    container_id is never sent over the wire, matching the conditional
    GET-parameter pattern used elsewhere in this module.
    """
    cid = get_container_id()
    return {"container_id": cid} if cid else {}


def parse_task_id(task_id: str) -> tuple[int, int]:
    """Parse task ID and return (phase_idx, task_idx).

    Args:
        task_id: Task ID in format "task-N" or "task-P-T"

    Returns:
        Tuple of (phase_idx, task_idx) as 0-based indices

    Raises:
        ValueError: If task ID format is invalid or numbers are out of range
    """
    lower = task_id.lower()
    stripped = lower.removeprefix("task-")
    if stripped == lower:
        raise ValueError(f"Invalid task ID '{task_id}': expected format 'task-N' or 'task-P-T'")
    task_parts = stripped.split("-")
    try:
        if len(task_parts) == 1:
            # Simple format: task-N (assumes phase-1)
            phase_idx = 0
            task_idx = int(task_parts[0]) - 1
        elif len(task_parts) == 2:
            # Full format: task-P-T
            phase_idx = int(task_parts[0]) - 1
            task_idx = int(task_parts[1]) - 1
        else:
            raise ValueError(f"Invalid task ID format: {task_id}")

        if phase_idx < 0 or task_idx < 0:
            raise ValueError(f"Task/phase numbers must be >= 1: {task_id}")
        return phase_idx, task_idx
    except ValueError as e:
        if "Invalid task ID" in str(e) or "must be >= 1" in str(e):
            raise
        raise ValueError(f"Invalid task ID '{task_id}': expected numeric values") from e


def parse_criterion_id(criterion_id: str) -> int:
    """Parse criterion ID and return criterion_idx.

    Args:
        criterion_id: Criterion ID in format "ac-N"

    Returns:
        Criterion index as 0-based

    Raises:
        ValueError: If criterion ID format is invalid or number is out of range
    """
    try:
        criterion_num = int(criterion_id.lower().replace("ac-", ""))
        if criterion_num < 1:
            raise ValueError(f"Criterion number must be >= 1: {criterion_id}")
        return criterion_num - 1
    except ValueError as e:
        if "must be >= 1" in str(e):
            raise
        raise ValueError(f"Invalid criterion ID '{criterion_id}': expected format 'ac-N'") from e


def parse_phase_id(phase_id: str) -> int:
    """Parse phase ID and return phase_idx.

    Args:
        phase_id: Phase ID in format "phase-N"

    Returns:
        Phase index as 0-based

    Raises:
        ValueError: If phase ID format is invalid or number is out of range
    """
    lower = phase_id.lower()
    stripped = lower.removeprefix("phase-")
    if stripped == lower:
        # prefix was not present
        raise ValueError(f"Invalid phase ID '{phase_id}': expected format 'phase-N'")
    try:
        phase_num = int(stripped)
        if phase_num < 1:
            raise ValueError(f"Phase number must be >= 1: {phase_id}")
        return phase_num - 1
    except ValueError as e:
        if "must be >= 1" in str(e):
            raise
        raise ValueError(f"Invalid phase ID '{phase_id}': expected format 'phase-N'") from e


def validate_commit_sha(commit: str) -> str:
    """Validate a git commit SHA.

    Args:
        commit: Git commit SHA (7-40 hex characters)

    Returns:
        The validated commit SHA

    Raises:
        ValueError: If the commit SHA format is invalid
    """
    if not COMMIT_SHA_PATTERN.match(commit):
        raise ValueError(f"Invalid commit SHA '{commit}': expected 7-40 hexadecimal characters")
    return commit
