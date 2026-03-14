"""
Checkpoint loader with atomic write support.

Provides functions for reading and writing checkpoint data with atomic
operations to prevent corruption from concurrent access or interrupted writes.
"""

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .checkpoints import (
    AgentType,
    CheckpointIndexV2,
    CheckpointSummaryV2,
    CheckpointV2,
    SessionStatus,
    TriggerType,
)


class CheckpointLoadError(Exception):
    """Error loading a checkpoint."""

    pass


class CheckpointSaveError(Exception):
    """Error saving a checkpoint."""

    pass


def generate_checkpoint_id_from_commit(
    commit_sha: str,
    session_id: str,
    timestamp: datetime | None = None,
) -> str:
    """
    Generate a deterministic checkpoint ID from commit, session, and timestamp.

    This ensures unique checkpoint IDs even for multiple checkpoints from the
    same session pushing to the same commit (e.g., amended commits).

    Args:
        commit_sha: The commit SHA
        session_id: The session ID
        timestamp: Optional timestamp for uniqueness (defaults to now)

    Returns:
        A checkpoint ID in the format ckpt-{16 hex chars}
    """
    if timestamp is None:
        timestamp = datetime.now(UTC)
    # Include timestamp for uniqueness across rapid successive pushes
    content = f"{commit_sha}:{session_id}:{timestamp.isoformat()}"
    # Use 8 bytes (64 bits) for better collision resistance
    hash_bytes = hashlib.sha256(content.encode()).digest()[:8]
    hex_str = hash_bytes.hex()
    return f"ckpt-{hex_str}"


def get_checkpoint_filename(checkpoint_id: str) -> str:
    """
    Get the filename for a checkpoint.

    Args:
        checkpoint_id: The checkpoint ID

    Returns:
        Filename for the checkpoint (e.g., ckpt-abc123def456.json)
    """
    return f"{checkpoint_id}.json"


def get_checkpoint_path(base_dir: Path, checkpoint_id: str) -> Path:
    """
    Get the full path for a checkpoint file.

    Checkpoints are organized by the first 2 characters of the ID
    to prevent too many files in a single directory.

    Args:
        base_dir: Base directory for checkpoints
        checkpoint_id: The checkpoint ID

    Returns:
        Full path to the checkpoint file
    """
    # Extract prefix from ID (skip 'ckpt-' prefix)
    prefix = checkpoint_id[5:7] if len(checkpoint_id) > 6 else "00"
    return base_dir / prefix / get_checkpoint_filename(checkpoint_id)


# ==============================================================================
# Checkpoint v2 Loader Functions
#
# V2 checkpoints support session-end triggers (no commit_sha required) and
# multi-dimensional indexing. Stored on the egg/checkpoints/v2 branch.
# ==============================================================================


def generate_checkpoint_id_v2(
    session_id: str,
    timestamp: datetime | None = None,
) -> str:
    """
    Generate a deterministic checkpoint ID for session-end checkpoints.

    Unlike commit-based IDs, these are derived from session_id + timestamp
    since no commit_sha is available.

    Args:
        session_id: The session/container ID
        timestamp: Optional timestamp for uniqueness (defaults to now)

    Returns:
        A checkpoint ID in the format ckpt-{16 hex chars}
    """
    if timestamp is None:
        timestamp = datetime.now(UTC)
    content = f"session:{session_id}:{timestamp.isoformat()}"
    hash_bytes = hashlib.sha256(content.encode()).digest()[:8]
    hex_str = hash_bytes.hex()
    return f"ckpt-{hex_str}"


def save_checkpoint_v2(checkpoint: CheckpointV2, path: Path) -> None:
    """
    Save a v2 checkpoint to a JSON file atomically.

    Uses the same temp file + rename pattern as v1 for crash safety.

    Args:
        checkpoint: The v2 checkpoint to save
        path: Path to save the checkpoint to

    Raises:
        CheckpointSaveError: If the checkpoint cannot be saved
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        data = checkpoint.model_dump(mode="json")
        json_str = json.dumps(data, indent=2, sort_keys=True)

        fd, temp_path = tempfile.mkstemp(suffix=".tmp", prefix=".checkpoint_", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json_str)
                f.flush()
                os.fsync(f.fileno())

            os.chmod(temp_path, 0o644)
            os.rename(temp_path, path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    except Exception as e:
        msg = f"Error saving v2 checkpoint to {path}: {e}"
        raise CheckpointSaveError(msg) from e


def load_checkpoint_v2(path: Path) -> CheckpointV2:
    """
    Load a v2 checkpoint from a JSON file.

    Args:
        path: Path to the checkpoint JSON file

    Returns:
        The loaded CheckpointV2

    Raises:
        CheckpointLoadError: If the file cannot be loaded or parsed
    """
    try:
        with open(path) as f:
            data = json.load(f)
        return CheckpointV2.model_validate(data)
    except FileNotFoundError as e:
        msg = f"V2 checkpoint file not found: {path}"
        raise CheckpointLoadError(msg) from e
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in v2 checkpoint file {path}: {e}"
        raise CheckpointLoadError(msg) from e
    except Exception as e:
        msg = f"Error loading v2 checkpoint from {path}: {e}"
        raise CheckpointLoadError(msg) from e


def load_checkpoint_index_v2(path: Path) -> CheckpointIndexV2:
    """
    Load the v2 checkpoint index from a JSON file.

    Returns an empty index if the file doesn't exist.

    Args:
        path: Path to the index JSON file

    Returns:
        The loaded CheckpointIndexV2
    """
    try:
        with open(path) as f:
            data = json.load(f)
        return CheckpointIndexV2.model_validate(data)
    except FileNotFoundError:
        return CheckpointIndexV2(last_updated=datetime.now(UTC))
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in v2 index file {path}: {e}"
        raise CheckpointLoadError(msg) from e
    except Exception as e:
        msg = f"Error loading v2 checkpoint index from {path}: {e}"
        raise CheckpointLoadError(msg) from e


def save_checkpoint_index_v2(index: CheckpointIndexV2, path: Path) -> None:
    """
    Save the v2 checkpoint index to a JSON file atomically.

    Args:
        index: The v2 checkpoint index to save
        path: Path to save the index to

    Raises:
        CheckpointSaveError: If the index cannot be saved
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        data = index.model_dump(mode="json")
        json_str = json.dumps(data, indent=2, sort_keys=True)

        fd, temp_path = tempfile.mkstemp(suffix=".tmp", prefix=".index_", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json_str)
                f.flush()
                os.fsync(f.fileno())

            os.chmod(temp_path, 0o644)
            os.rename(temp_path, path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    except Exception as e:
        msg = f"Error saving v2 checkpoint index to {path}: {e}"
        raise CheckpointSaveError(msg) from e


def add_checkpoint_to_index_v2(
    checkpoint: CheckpointV2,
    index_path: Path,
) -> CheckpointIndexV2:
    """
    Add a v2 checkpoint to the index with multi-dimensional index updates.

    Updates the primary checkpoint list and all secondary indices. The final
    file write uses atomic rename, but the read-modify-write cycle is not
    locked — concurrent callers must coordinate externally (e.g., via git
    push non-fast-forward rejection) to avoid lost updates.
    Deduplicates by checkpoint ID.

    Args:
        checkpoint: The v2 checkpoint to add
        index_path: Path to the index file

    Returns:
        The updated CheckpointIndexV2

    Raises:
        CheckpointSaveError: If the index cannot be saved
    """
    index = load_checkpoint_index_v2(index_path)

    # Deduplicate by checkpoint ID
    existing_ids = {cp.id for cp in index.checkpoints}
    if checkpoint.id in existing_ids:
        return index

    # Create summary and add to primary list
    summary = CheckpointSummaryV2.from_checkpoint(checkpoint)
    index.checkpoints.append(summary)

    # Update secondary indices
    _append_to_index(index.by_session, checkpoint.session_id, checkpoint.id)
    _append_to_index(index.by_trigger, checkpoint.trigger_type.value, checkpoint.id)

    if checkpoint.issue_number is not None:
        _append_to_index(index.by_issue, str(checkpoint.issue_number), checkpoint.id)

    if checkpoint.pr_number is not None:
        _append_to_index(index.by_pr, str(checkpoint.pr_number), checkpoint.id)

    if checkpoint.commit_sha is not None:
        index.by_commit[checkpoint.commit_sha] = checkpoint.id

    _append_to_index(index.by_agent_type, checkpoint.agent_type.value, checkpoint.id)

    if checkpoint.pipeline_phase is not None:
        _append_to_index(index.by_phase, checkpoint.pipeline_phase, checkpoint.id)

    if checkpoint.pipeline_id is not None:
        _append_to_index(index.by_pipeline, checkpoint.pipeline_id, checkpoint.id)

    if checkpoint.repo is not None:
        _append_to_index(index.by_repo, checkpoint.repo, checkpoint.id)

    if checkpoint.session_status is not None:
        _append_to_index(index.by_status, checkpoint.session_status.value, checkpoint.id)

    index.last_updated = datetime.now(UTC)

    save_checkpoint_index_v2(index, index_path)

    return index


def _append_to_index(index_dict: dict[str, list[str]], key: str, value: str) -> None:
    """Append value to index dict list, deduplicating."""
    if key not in index_dict:
        index_dict[key] = []
    if value not in index_dict[key]:
        index_dict[key].append(value)


def load_checkpoint_by_id_v2(
    checkpoint_id: str,
    checkpoints_dir: Path,
) -> CheckpointV2 | None:
    """
    Load a v2 checkpoint by its ID.

    Args:
        checkpoint_id: The checkpoint ID (e.g., ckpt-abc123def456)
        checkpoints_dir: Directory containing checkpoint files

    Returns:
        The CheckpointV2 if found, None otherwise
    """
    checkpoint_path = get_checkpoint_path(checkpoints_dir, checkpoint_id)
    if not checkpoint_path.exists():
        return None
    try:
        return load_checkpoint_v2(checkpoint_path)
    except CheckpointLoadError:
        return None


def load_checkpoint_by_commit_v2(
    commit_sha: str,
    checkpoints_dir: Path,
    index_path: Path,
) -> CheckpointV2 | None:
    """
    Load a v2 checkpoint by commit SHA using the v2 index.

    Args:
        commit_sha: The commit SHA to find
        checkpoints_dir: Directory containing checkpoint files
        index_path: Path to the v2 index file

    Returns:
        The CheckpointV2 if found, None otherwise
    """
    try:
        index = load_checkpoint_index_v2(index_path)
    except CheckpointLoadError:
        return None

    checkpoint_id = index.get_by_commit(commit_sha)
    if not checkpoint_id:
        return None

    return load_checkpoint_by_id_v2(checkpoint_id, checkpoints_dir)


def filter_checkpoints_v2(
    index: CheckpointIndexV2,
    issue_number: int | None = None,
    pr_number: int | None = None,
    branch: str | None = None,
    session_id: str | None = None,
    trigger_type: str | None = None,
    session_status: str | None = None,
    agent_type: str | None = None,
    pipeline_phase: str | None = None,
    pipeline_id: str | None = None,
    repo: str | None = None,
    limit: int | None = None,
) -> list[CheckpointSummaryV2]:
    """
    Filter v2 checkpoint summaries from a pre-loaded index.

    Uses multi-dimensional index lookups. Filters are intersected (AND logic).

    Args:
        index: Pre-loaded checkpoint index
        issue_number: Filter by issue number
        pr_number: Filter by PR number
        branch: Filter by branch name
        session_id: Filter by session ID
        trigger_type: Filter by trigger type value
        session_status: Filter by session status value
        agent_type: Filter by agent type value
        pipeline_phase: Filter by pipeline phase
        pipeline_id: Filter by pipeline run ID
        repo: Filter by source repository (owner/repo format)
        limit: Maximum number of results

    Returns:
        List of CheckpointSummaryV2, sorted by created_at descending
    """
    if not index.checkpoints:
        return []

    # Build set of matching checkpoint IDs using index lookups
    # Start with None (meaning "all") and intersect with each filter
    matching_ids: set[str] | None = None

    def _intersect(ids: list[str]) -> None:
        nonlocal matching_ids
        id_set = set(ids)
        if matching_ids is None:
            matching_ids = id_set
        else:
            matching_ids &= id_set

    if issue_number is not None:
        _intersect(index.get_by_issue(issue_number))

    if pr_number is not None:
        _intersect(index.get_by_pr(pr_number))

    if session_id is not None:
        _intersect(index.get_by_session(session_id))

    if trigger_type is not None:
        _intersect(index.get_by_trigger(TriggerType(trigger_type)))

    if session_status is not None:
        _intersect(index.get_by_status(SessionStatus(session_status)))

    if agent_type is not None:
        _intersect(index.get_by_agent_type(AgentType(agent_type)))

    if pipeline_phase is not None:
        _intersect(index.get_by_phase(pipeline_phase))

    if pipeline_id is not None:
        _intersect(index.get_by_pipeline(pipeline_id))

    if repo is not None:
        _intersect(index.get_by_repo(repo))

    # Filter summaries
    results = []
    for summary in index.checkpoints:
        if matching_ids is not None and summary.id not in matching_ids:
            continue
        if branch is not None and summary.branch != branch:
            continue
        results.append(summary)

    # Sort by created_at descending
    results.sort(key=lambda cp: cp.created_at, reverse=True)

    if limit is not None:
        results = results[:limit]

    return results


def list_checkpoints_v2(
    checkpoints_dir: Path,
    index_path: Path,
    issue_number: int | None = None,
    pr_number: int | None = None,
    branch: str | None = None,
    session_id: str | None = None,
    trigger_type: str | None = None,
    session_status: str | None = None,
    agent_type: str | None = None,
    pipeline_phase: str | None = None,
    pipeline_id: str | None = None,
    repo: str | None = None,
    limit: int | None = None,
) -> list[CheckpointSummaryV2]:
    """
    List v2 checkpoint summaries using the index, with multi-dimensional filtering.

    Uses the v2 index for fast lookups. Filters are intersected (AND logic).

    Args:
        checkpoints_dir: Directory containing checkpoint files (unused, kept for API symmetry)
        index_path: Path to the v2 index file
        issue_number: Filter by issue number
        pr_number: Filter by PR number
        branch: Filter by branch name
        session_id: Filter by session ID
        trigger_type: Filter by trigger type value
        session_status: Filter by session status value
        agent_type: Filter by agent type value
        pipeline_phase: Filter by pipeline phase
        pipeline_id: Filter by pipeline run ID
        repo: Filter by source repository (owner/repo format)
        limit: Maximum number of results

    Returns:
        List of CheckpointSummaryV2, sorted by created_at descending
    """
    try:
        index = load_checkpoint_index_v2(index_path)
    except CheckpointLoadError:
        return []

    return filter_checkpoints_v2(
        index,
        issue_number=issue_number,
        pr_number=pr_number,
        branch=branch,
        session_id=session_id,
        trigger_type=trigger_type,
        session_status=session_status,
        agent_type=agent_type,
        pipeline_phase=pipeline_phase,
        pipeline_id=pipeline_id,
        repo=repo,
        limit=limit,
    )
