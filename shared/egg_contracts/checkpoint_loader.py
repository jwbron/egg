"""
Checkpoint loader with atomic write support.

Provides functions for reading and writing checkpoint data with atomic
operations to prevent corruption from concurrent access or interrupted writes.
"""

import hashlib
import json
import os
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .checkpoints import Checkpoint, CheckpointIndex, CheckpointSummary


class CheckpointLoadError(Exception):
    """Error loading a checkpoint."""

    pass


class CheckpointSaveError(Exception):
    """Error saving a checkpoint."""

    pass


def generate_checkpoint_id() -> str:
    """
    Generate a unique checkpoint ID.

    Returns:
        A checkpoint ID in the format ckpt-{12 hex chars}
    """
    random_bytes = secrets.token_bytes(6)
    hex_str = random_bytes.hex()
    return f"ckpt-{hex_str}"


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


def load_checkpoint(path: Path) -> Checkpoint:
    """
    Load a checkpoint from a JSON file.

    Args:
        path: Path to the checkpoint JSON file

    Returns:
        The loaded Checkpoint

    Raises:
        CheckpointLoadError: If the file cannot be loaded or parsed
    """
    try:
        with open(path) as f:
            data = json.load(f)
        return Checkpoint.model_validate(data)
    except FileNotFoundError as e:
        msg = f"Checkpoint file not found: {path}"
        raise CheckpointLoadError(msg) from e
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in checkpoint file {path}: {e}"
        raise CheckpointLoadError(msg) from e
    except Exception as e:
        msg = f"Error loading checkpoint from {path}: {e}"
        raise CheckpointLoadError(msg) from e


def save_checkpoint(checkpoint: Checkpoint, path: Path) -> None:
    """
    Save a checkpoint to a JSON file atomically.

    Uses the temp file + rename pattern to ensure atomic writes:
    1. Write to a temporary file in the same directory
    2. Sync to disk
    3. Atomically rename to the target path

    This prevents corruption if the process is interrupted during write.

    Args:
        checkpoint: The checkpoint to save
        path: Path to save the checkpoint to

    Raises:
        CheckpointSaveError: If the checkpoint cannot be saved
    """
    try:
        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize checkpoint to JSON
        data = checkpoint.model_dump(mode="json")
        json_str = json.dumps(data, indent=2, sort_keys=True)

        # Write to temp file in same directory (for atomic rename)
        fd, temp_path = tempfile.mkstemp(suffix=".tmp", prefix=".checkpoint_", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json_str)
                f.flush()
                os.fsync(f.fileno())

            # Set restrictive permissions
            os.chmod(temp_path, 0o644)

            # Atomic rename
            os.rename(temp_path, path)
        except Exception:
            # Clean up temp file on error
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    except Exception as e:
        msg = f"Error saving checkpoint to {path}: {e}"
        raise CheckpointSaveError(msg) from e


def load_checkpoint_index(path: Path) -> CheckpointIndex:
    """
    Load the checkpoint index from a JSON file.

    Args:
        path: Path to the index JSON file

    Returns:
        The loaded CheckpointIndex

    Raises:
        CheckpointLoadError: If the file cannot be loaded or parsed
    """
    try:
        with open(path) as f:
            data = json.load(f)
        return CheckpointIndex.model_validate(data)
    except FileNotFoundError:
        # Return empty index if file doesn't exist
        return CheckpointIndex(last_updated=datetime.now(UTC), checkpoints=[])
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in index file {path}: {e}"
        raise CheckpointLoadError(msg) from e
    except Exception as e:
        msg = f"Error loading checkpoint index from {path}: {e}"
        raise CheckpointLoadError(msg) from e


def save_checkpoint_index(index: CheckpointIndex, path: Path) -> None:
    """
    Save the checkpoint index to a JSON file atomically.

    Args:
        index: The checkpoint index to save
        path: Path to save the index to

    Raises:
        CheckpointSaveError: If the index cannot be saved
    """
    try:
        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize index to JSON
        data = index.model_dump(mode="json")
        json_str = json.dumps(data, indent=2, sort_keys=True)

        # Write to temp file in same directory (for atomic rename)
        fd, temp_path = tempfile.mkstemp(suffix=".tmp", prefix=".index_", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json_str)
                f.flush()
                os.fsync(f.fileno())

            # Set restrictive permissions
            os.chmod(temp_path, 0o644)

            # Atomic rename
            os.rename(temp_path, path)
        except Exception:
            # Clean up temp file on error
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    except Exception as e:
        msg = f"Error saving checkpoint index to {path}: {e}"
        raise CheckpointSaveError(msg) from e


def add_checkpoint_to_index(
    checkpoint: Checkpoint,
    index_path: Path,
) -> CheckpointIndex:
    """
    Add a checkpoint to the index and save it.

    This is a convenience function that:
    1. Loads the existing index (or creates a new one)
    2. Adds the checkpoint summary
    3. Saves the updated index atomically

    Args:
        checkpoint: The checkpoint to add
        index_path: Path to the index file

    Returns:
        The updated CheckpointIndex

    Raises:
        CheckpointSaveError: If the index cannot be saved
    """
    # Load existing index or create new one
    index = load_checkpoint_index(index_path)

    # Check if checkpoint already exists in index
    existing = index.get_by_commit(checkpoint.commit_sha)
    if existing and existing.id == checkpoint.id:
        # Already in index with same ID, no update needed
        return index

    # Remove any existing entry for this commit (in case of re-checkpoint)
    index.checkpoints = [cp for cp in index.checkpoints if cp.commit_sha != checkpoint.commit_sha]

    # Add new checkpoint summary
    summary = CheckpointSummary.from_checkpoint(checkpoint)
    index.checkpoints.append(summary)

    # Update timestamp
    index.last_updated = datetime.now(UTC)

    # Save atomically
    save_checkpoint_index(index, index_path)

    return index


def load_checkpoint_by_commit(
    commit_sha: str,
    checkpoints_dir: Path,
    index_path: Path | None = None,
) -> Checkpoint | None:
    """
    Load a checkpoint by commit SHA.

    If an index path is provided, uses the index for fast lookup.
    Otherwise, scans the checkpoint directory.

    Args:
        commit_sha: The commit SHA to find
        checkpoints_dir: Directory containing checkpoint files
        index_path: Optional path to the checkpoint index

    Returns:
        The Checkpoint if found, None otherwise
    """
    # Try using index for fast lookup
    if index_path and index_path.exists():
        try:
            index = load_checkpoint_index(index_path)
            summary = index.get_by_commit(commit_sha)
            if summary:
                checkpoint_path = get_checkpoint_path(checkpoints_dir, summary.id)
                if checkpoint_path.exists():
                    return load_checkpoint(checkpoint_path)
        except CheckpointLoadError:
            pass  # Fall through to scan

    # Fallback: scan checkpoint directory
    if not checkpoints_dir.exists():
        return None

    for subdir in checkpoints_dir.iterdir():
        if not subdir.is_dir():
            continue
        for checkpoint_file in subdir.glob("ckpt-*.json"):
            try:
                checkpoint = load_checkpoint(checkpoint_file)
                if checkpoint.commit_sha == commit_sha or commit_sha.startswith(
                    checkpoint.commit_sha[:7]
                ):
                    return checkpoint
            except CheckpointLoadError:
                continue

    return None


def list_checkpoints(
    checkpoints_dir: Path,
    issue_number: int | None = None,
    branch: str | None = None,
    limit: int | None = None,
) -> list[Checkpoint]:
    """
    List checkpoints, optionally filtered by issue or branch.

    Args:
        checkpoints_dir: Directory containing checkpoint files
        issue_number: Optional issue number to filter by
        branch: Optional branch to filter by
        limit: Optional maximum number of checkpoints to return

    Returns:
        List of Checkpoint objects, sorted by created_at descending
    """
    if not checkpoints_dir.exists():
        return []

    checkpoints = []
    for subdir in checkpoints_dir.iterdir():
        if not subdir.is_dir():
            continue
        for checkpoint_file in subdir.glob("ckpt-*.json"):
            try:
                checkpoint = load_checkpoint(checkpoint_file)

                # Apply filters
                if issue_number is not None and checkpoint.issue_number != issue_number:
                    continue
                if branch is not None and checkpoint.branch != branch:
                    continue

                checkpoints.append(checkpoint)
            except CheckpointLoadError:
                continue

    # Sort by created_at descending (most recent first)
    checkpoints.sort(key=lambda cp: cp.created_at, reverse=True)

    # Apply limit
    if limit is not None:
        checkpoints = checkpoints[:limit]

    return checkpoints
