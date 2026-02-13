"""
Usage loader with atomic write support.

Provides functions for reading and writing usage aggregate data with atomic
operations to prevent corruption from concurrent access or interrupted writes.

Directory structure in the checkpoint branch:
    usage/
        by-session/
            {session_id}.json
        by-issue/
            {issue_number}.json
        by-workflow/
            {workflow_id}.json
        by-pr/
            {pr_number}.json
        index.json
"""

import json
import logging
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from .checkpoints import Checkpoint, TokenUsage
from .usage import (
    CheckpointReference,
    IssueUsage,
    PRUsage,
    SessionUsage,
    TokenCounts,
    UsageIndex,
    WorkflowUsage,
)

logger = logging.getLogger(__name__)

# Type variable for generic usage loading
T = TypeVar("T", SessionUsage, IssueUsage, WorkflowUsage, PRUsage, UsageIndex)

# Maximum retry attempts for concurrent access
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.1

# Feature flag for usage tracking
USAGE_TRACKING_ENABLED = os.environ.get("USAGE_TRACKING_ENABLED", "true").lower() == "true"


class UsageLoadError(Exception):
    """Error loading usage data."""

    pass


class UsageSaveError(Exception):
    """Error saving usage data."""

    pass


def get_usage_base_path(base_dir: Path) -> Path:
    """Get the base path for usage data."""
    return base_dir / "usage"


def get_session_usage_path(base_dir: Path, session_id: str) -> Path:
    """Get the path for a session usage file."""
    return get_usage_base_path(base_dir) / "by-session" / f"{session_id}.json"


def get_issue_usage_path(base_dir: Path, issue_number: int) -> Path:
    """Get the path for an issue usage file."""
    return get_usage_base_path(base_dir) / "by-issue" / f"{issue_number}.json"


def get_workflow_usage_path(base_dir: Path, workflow_id: str) -> Path:
    """Get the path for a workflow usage file."""
    return get_usage_base_path(base_dir) / "by-workflow" / f"{workflow_id}.json"


def get_pr_usage_path(base_dir: Path, pr_number: int) -> Path:
    """Get the path for a PR usage file."""
    return get_usage_base_path(base_dir) / "by-pr" / f"{pr_number}.json"


def get_usage_index_path(base_dir: Path) -> Path:
    """Get the path for the usage index file."""
    return get_usage_base_path(base_dir) / "index.json"


def _atomic_write(path: Path, data: dict) -> None:
    """
    Write data to a file atomically using temp file + rename pattern.

    Args:
        path: Path to write to
        data: Dictionary data to write as JSON

    Raises:
        UsageSaveError: If the write fails
    """
    try:
        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize to JSON
        json_str = json.dumps(data, indent=2, sort_keys=True, default=str)

        # Write to temp file in same directory (for atomic rename)
        fd, temp_path = tempfile.mkstemp(suffix=".tmp", prefix=".usage_", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json_str)
                f.flush()
                os.fsync(f.fileno())

            # Set permissions
            os.chmod(temp_path, 0o644)

            # Atomic rename
            os.rename(temp_path, path)
        except Exception:
            # Clean up temp file on error
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    except Exception as e:
        msg = f"Error saving usage data to {path}: {e}"
        raise UsageSaveError(msg) from e


def load_session_usage(base_dir: Path, session_id: str) -> SessionUsage | None:
    """
    Load session usage data.

    Args:
        base_dir: Base directory for usage data
        session_id: Session ID to load

    Returns:
        SessionUsage if found, None otherwise

    Raises:
        UsageLoadError: If the file exists but cannot be parsed
    """
    path = get_session_usage_path(base_dir, session_id)
    if not path.exists():
        return None

    try:
        with open(path) as f:
            data = json.load(f)
        return SessionUsage.model_validate(data)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in session usage file {path}: {e}"
        raise UsageLoadError(msg) from e
    except Exception as e:
        msg = f"Error loading session usage from {path}: {e}"
        raise UsageLoadError(msg) from e


def save_session_usage(base_dir: Path, usage: SessionUsage) -> None:
    """
    Save session usage data atomically.

    Args:
        base_dir: Base directory for usage data
        usage: SessionUsage to save

    Raises:
        UsageSaveError: If the save fails
    """
    path = get_session_usage_path(base_dir, usage.session_id)
    data = usage.model_dump(mode="json")
    _atomic_write(path, data)


def load_issue_usage(base_dir: Path, issue_number: int) -> IssueUsage | None:
    """
    Load issue usage data.

    Args:
        base_dir: Base directory for usage data
        issue_number: Issue number to load

    Returns:
        IssueUsage if found, None otherwise

    Raises:
        UsageLoadError: If the file exists but cannot be parsed
    """
    path = get_issue_usage_path(base_dir, issue_number)
    if not path.exists():
        return None

    try:
        with open(path) as f:
            data = json.load(f)
        return IssueUsage.model_validate(data)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in issue usage file {path}: {e}"
        raise UsageLoadError(msg) from e
    except Exception as e:
        msg = f"Error loading issue usage from {path}: {e}"
        raise UsageLoadError(msg) from e


def save_issue_usage(base_dir: Path, usage: IssueUsage) -> None:
    """
    Save issue usage data atomically.

    Args:
        base_dir: Base directory for usage data
        usage: IssueUsage to save

    Raises:
        UsageSaveError: If the save fails
    """
    path = get_issue_usage_path(base_dir, usage.issue_number)
    data = usage.model_dump(mode="json")
    _atomic_write(path, data)


def load_workflow_usage(base_dir: Path, workflow_id: str) -> WorkflowUsage | None:
    """
    Load workflow usage data.

    Args:
        base_dir: Base directory for usage data
        workflow_id: Workflow ID to load

    Returns:
        WorkflowUsage if found, None otherwise

    Raises:
        UsageLoadError: If the file exists but cannot be parsed
    """
    path = get_workflow_usage_path(base_dir, workflow_id)
    if not path.exists():
        return None

    try:
        with open(path) as f:
            data = json.load(f)
        return WorkflowUsage.model_validate(data)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in workflow usage file {path}: {e}"
        raise UsageLoadError(msg) from e
    except Exception as e:
        msg = f"Error loading workflow usage from {path}: {e}"
        raise UsageLoadError(msg) from e


def save_workflow_usage(base_dir: Path, usage: WorkflowUsage) -> None:
    """
    Save workflow usage data atomically.

    Args:
        base_dir: Base directory for usage data
        usage: WorkflowUsage to save

    Raises:
        UsageSaveError: If the save fails
    """
    path = get_workflow_usage_path(base_dir, usage.workflow_id)
    data = usage.model_dump(mode="json")
    _atomic_write(path, data)


def load_pr_usage(base_dir: Path, pr_number: int) -> PRUsage | None:
    """
    Load PR usage data.

    Args:
        base_dir: Base directory for usage data
        pr_number: PR number to load

    Returns:
        PRUsage if found, None otherwise

    Raises:
        UsageLoadError: If the file exists but cannot be parsed
    """
    path = get_pr_usage_path(base_dir, pr_number)
    if not path.exists():
        return None

    try:
        with open(path) as f:
            data = json.load(f)
        return PRUsage.model_validate(data)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in PR usage file {path}: {e}"
        raise UsageLoadError(msg) from e
    except Exception as e:
        msg = f"Error loading PR usage from {path}: {e}"
        raise UsageLoadError(msg) from e


def save_pr_usage(base_dir: Path, usage: PRUsage) -> None:
    """
    Save PR usage data atomically.

    Args:
        base_dir: Base directory for usage data
        usage: PRUsage to save

    Raises:
        UsageSaveError: If the save fails
    """
    path = get_pr_usage_path(base_dir, usage.pr_number)
    data = usage.model_dump(mode="json")
    _atomic_write(path, data)


def load_usage_index(base_dir: Path) -> UsageIndex:
    """
    Load the usage index.

    Args:
        base_dir: Base directory for usage data

    Returns:
        UsageIndex (empty if not found)

    Raises:
        UsageLoadError: If the file exists but cannot be parsed
    """
    path = get_usage_index_path(base_dir)
    if not path.exists():
        return UsageIndex(last_updated=datetime.now(UTC))

    try:
        with open(path) as f:
            data = json.load(f)
        return UsageIndex.model_validate(data)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON in usage index file {path}: {e}"
        raise UsageLoadError(msg) from e
    except Exception as e:
        msg = f"Error loading usage index from {path}: {e}"
        raise UsageLoadError(msg) from e


def save_usage_index(base_dir: Path, index: UsageIndex) -> None:
    """
    Save the usage index atomically.

    Args:
        base_dir: Base directory for usage data
        index: UsageIndex to save

    Raises:
        UsageSaveError: If the save fails
    """
    path = get_usage_index_path(base_dir)
    data = index.model_dump(mode="json")
    _atomic_write(path, data)


def _token_usage_to_counts(token_usage: TokenUsage | None) -> TokenCounts:
    """Convert checkpoint TokenUsage to usage TokenCounts."""
    if token_usage is None:
        return TokenCounts()
    return TokenCounts(
        input_tokens=token_usage.input_tokens,
        output_tokens=token_usage.output_tokens,
        cache_read_tokens=token_usage.cache_read_tokens,
        cache_creation_tokens=token_usage.cache_creation_tokens,
    )


def update_usage_from_checkpoint(
    base_dir: Path,
    checkpoint: Checkpoint,
    retry_attempts: int = MAX_RETRY_ATTEMPTS,
) -> None:
    """
    Update usage aggregates from a checkpoint.

    This function updates:
    - Session usage (always)
    - Issue usage (if issue_number is set)
    - PR usage (if pr_number is set)
    - Usage index

    Uses optimistic locking with retry for concurrent access.

    Args:
        base_dir: Base directory for usage data
        checkpoint: Checkpoint to extract usage from
        retry_attempts: Maximum retry attempts for concurrent access

    Raises:
        UsageSaveError: If updates fail after all retries
    """
    if not USAGE_TRACKING_ENABLED:
        logger.debug("Usage tracking disabled by USAGE_TRACKING_ENABLED=false")
        return

    now = datetime.now(UTC)
    token_counts = _token_usage_to_counts(checkpoint.token_usage)
    checkpoint_ref = CheckpointReference(
        checkpoint_id=checkpoint.id,
        commit_sha=checkpoint.commit_sha,
        created_at=checkpoint.created_at,
    )

    # Update session usage
    for attempt in range(retry_attempts):
        try:
            _update_session_usage(base_dir, checkpoint, token_counts, checkpoint_ref, now)
            break
        except (UsageLoadError, UsageSaveError) as e:
            if attempt == retry_attempts - 1:
                logger.error(f"Failed to update session usage after {retry_attempts} attempts: {e}")
                raise
            time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

    # Update issue usage if applicable
    if checkpoint.issue_number:
        for attempt in range(retry_attempts):
            try:
                _update_issue_usage(base_dir, checkpoint, token_counts, now)
                break
            except (UsageLoadError, UsageSaveError) as e:
                if attempt == retry_attempts - 1:
                    logger.error(
                        f"Failed to update issue usage after {retry_attempts} attempts: {e}"
                    )
                    raise
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

    # Update PR usage if applicable
    if checkpoint.pr_number:
        for attempt in range(retry_attempts):
            try:
                _update_pr_usage(base_dir, checkpoint, token_counts, now)
                break
            except (UsageLoadError, UsageSaveError) as e:
                if attempt == retry_attempts - 1:
                    logger.error(f"Failed to update PR usage after {retry_attempts} attempts: {e}")
                    raise
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

    # Update index
    for attempt in range(retry_attempts):
        try:
            _update_usage_index(base_dir, checkpoint, token_counts, now)
            break
        except (UsageLoadError, UsageSaveError) as e:
            if attempt == retry_attempts - 1:
                logger.error(f"Failed to update usage index after {retry_attempts} attempts: {e}")
                raise
            time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))


def _update_session_usage(
    base_dir: Path,
    checkpoint: Checkpoint,
    token_counts: TokenCounts,
    checkpoint_ref: CheckpointReference,
    now: datetime,
) -> None:
    """Update session usage from a checkpoint."""
    session_id = checkpoint.session.session_id
    usage = load_session_usage(base_dir, session_id)

    if usage is None:
        # Create new session usage
        usage = SessionUsage(
            session_id=session_id,
            container_id=checkpoint.session.container_id,
            agent_role=checkpoint.session.agent_role,
            model=checkpoint.session.model,
            issue_number=checkpoint.issue_number,
            pr_number=checkpoint.pr_number,
            tokens=token_counts,
            checkpoint_count=1,
            first_checkpoint_at=checkpoint.created_at,
            last_checkpoint_at=checkpoint.created_at,
            last_updated=now,
            checkpoints=[checkpoint_ref],
        )
    else:
        # Check if checkpoint already recorded
        existing_ids = {cp.checkpoint_id for cp in usage.checkpoints}
        if checkpoint.id in existing_ids:
            logger.debug(f"Checkpoint {checkpoint.id} already in session {session_id}")
            return

        # Update existing session usage
        usage.tokens = usage.tokens.add(token_counts)
        usage.checkpoint_count += 1
        usage.checkpoints.append(checkpoint_ref)

        if usage.first_checkpoint_at is None or checkpoint.created_at < usage.first_checkpoint_at:
            usage.first_checkpoint_at = checkpoint.created_at
        if usage.last_checkpoint_at is None or checkpoint.created_at > usage.last_checkpoint_at:
            usage.last_checkpoint_at = checkpoint.created_at

        # Update PR number if newly available
        if checkpoint.pr_number and not usage.pr_number:
            usage.pr_number = checkpoint.pr_number

        usage.last_updated = now

    usage.update_cost()
    save_session_usage(base_dir, usage)


def _update_issue_usage(
    base_dir: Path,
    checkpoint: Checkpoint,
    token_counts: TokenCounts,
    now: datetime,
) -> None:
    """Update issue usage from a checkpoint."""
    issue_number = checkpoint.issue_number
    if issue_number is None:
        return

    usage = load_issue_usage(base_dir, issue_number)

    if usage is None:
        # Create new issue usage
        usage = IssueUsage(
            issue_number=issue_number,
            pr_number=checkpoint.pr_number,
            session_ids=[checkpoint.session.session_id],
            branch=checkpoint.branch,
            pipeline_phases=[checkpoint.pipeline_phase] if checkpoint.pipeline_phase else [],
            tokens=token_counts,
            checkpoint_count=1,
            first_checkpoint_at=checkpoint.created_at,
            last_checkpoint_at=checkpoint.created_at,
            last_updated=now,
        )
    else:
        # Update existing issue usage
        usage.tokens = usage.tokens.add(token_counts)
        usage.checkpoint_count += 1

        if checkpoint.session.session_id not in usage.session_ids:
            usage.session_ids.append(checkpoint.session.session_id)

        if checkpoint.pipeline_phase and checkpoint.pipeline_phase not in usage.pipeline_phases:
            usage.pipeline_phases.append(checkpoint.pipeline_phase)

        if usage.first_checkpoint_at is None or checkpoint.created_at < usage.first_checkpoint_at:
            usage.first_checkpoint_at = checkpoint.created_at
        if usage.last_checkpoint_at is None or checkpoint.created_at > usage.last_checkpoint_at:
            usage.last_checkpoint_at = checkpoint.created_at

        # Update PR number if newly available
        if checkpoint.pr_number and not usage.pr_number:
            usage.pr_number = checkpoint.pr_number

        # Update branch if not set
        if checkpoint.branch and not usage.branch:
            usage.branch = checkpoint.branch

        usage.last_updated = now

    usage.update_cost()
    save_issue_usage(base_dir, usage)


def _update_pr_usage(
    base_dir: Path,
    checkpoint: Checkpoint,
    token_counts: TokenCounts,
    now: datetime,
) -> None:
    """Update PR usage from a checkpoint."""
    pr_number = checkpoint.pr_number
    if pr_number is None:
        return

    usage = load_pr_usage(base_dir, pr_number)

    if usage is None:
        # Create new PR usage
        usage = PRUsage(
            pr_number=pr_number,
            issue_number=checkpoint.issue_number,
            branch=checkpoint.branch,
            session_ids=[checkpoint.session.session_id],
            pipeline_phases=[checkpoint.pipeline_phase] if checkpoint.pipeline_phase else [],
            tokens=token_counts,
            checkpoint_count=1,
            first_checkpoint_at=checkpoint.created_at,
            last_checkpoint_at=checkpoint.created_at,
            last_updated=now,
        )
    else:
        # Update existing PR usage
        usage.tokens = usage.tokens.add(token_counts)
        usage.checkpoint_count += 1

        if checkpoint.session.session_id not in usage.session_ids:
            usage.session_ids.append(checkpoint.session.session_id)

        if checkpoint.pipeline_phase and checkpoint.pipeline_phase not in usage.pipeline_phases:
            usage.pipeline_phases.append(checkpoint.pipeline_phase)

        if usage.first_checkpoint_at is None or checkpoint.created_at < usage.first_checkpoint_at:
            usage.first_checkpoint_at = checkpoint.created_at
        if usage.last_checkpoint_at is None or checkpoint.created_at > usage.last_checkpoint_at:
            usage.last_checkpoint_at = checkpoint.created_at

        # Update issue number if newly available
        if checkpoint.issue_number and not usage.issue_number:
            usage.issue_number = checkpoint.issue_number

        usage.last_updated = now

    usage.update_cost()
    save_pr_usage(base_dir, usage)


def _update_usage_index(
    base_dir: Path,
    checkpoint: Checkpoint,
    token_counts: TokenCounts,
    now: datetime,
) -> None:
    """Update the usage index from a checkpoint."""
    index = load_usage_index(base_dir)

    # Update totals
    index.total_tokens = index.total_tokens.add(token_counts)
    cost = token_counts.calculate_cost()
    index.total_cost_usd += float(cost)

    # Track session
    if checkpoint.session.session_id not in index.session_ids:
        index.session_ids.append(checkpoint.session.session_id)
        index.total_sessions += 1

    # Track issue
    if checkpoint.issue_number and checkpoint.issue_number not in index.issue_numbers:
        index.issue_numbers.append(checkpoint.issue_number)
        index.total_issues += 1

    # Track PR
    if checkpoint.pr_number and checkpoint.pr_number not in index.pr_numbers:
        index.pr_numbers.append(checkpoint.pr_number)
        index.total_prs += 1

    index.last_updated = now
    save_usage_index(base_dir, index)


def backfill_pr_usage(
    base_dir: Path,
    pr_number: int,
    issue_number: int | None = None,
    branch: str | None = None,
) -> int:
    """
    Backfill PR number into existing checkpoints and create PR usage aggregate.

    This function is called when a PR is created to associate existing
    checkpoints (that were created before the PR) with the PR number.

    Args:
        base_dir: Base directory for usage data
        pr_number: PR number to backfill
        issue_number: Associated issue number (for finding related checkpoints)
        branch: Branch name (for finding related checkpoints)

    Returns:
        Number of sessions updated

    Note:
        This updates session-level usage records, not individual checkpoints.
        Checkpoint files themselves are not modified.
    """
    if not USAGE_TRACKING_ENABLED:
        return 0

    now = datetime.now(UTC)
    updated_count = 0

    # Load or create PR usage
    pr_usage = load_pr_usage(base_dir, pr_number)
    if pr_usage is None:
        pr_usage = PRUsage(
            pr_number=pr_number,
            issue_number=issue_number,
            branch=branch,
            last_updated=now,
        )

    # If we have an issue number, load the issue usage to find sessions
    if issue_number:
        issue_usage = load_issue_usage(base_dir, issue_number)
        if issue_usage:
            # Update issue with PR number
            if not issue_usage.pr_number:
                issue_usage.pr_number = pr_number
                issue_usage.last_updated = now
                save_issue_usage(base_dir, issue_usage)

            # Update each session with PR number
            for session_id in issue_usage.session_ids:
                session_usage = load_session_usage(base_dir, session_id)
                if session_usage and not session_usage.pr_number:
                    session_usage.pr_number = pr_number
                    session_usage.last_updated = now
                    save_session_usage(base_dir, session_usage)
                    updated_count += 1

                    # Add session to PR usage if not already there
                    if session_id not in pr_usage.session_ids:
                        pr_usage.session_ids.append(session_id)
                        pr_usage.tokens = pr_usage.tokens.add(session_usage.tokens)
                        pr_usage.checkpoint_count += session_usage.checkpoint_count

                        if pr_usage.first_checkpoint_at is None or (
                            session_usage.first_checkpoint_at
                            and session_usage.first_checkpoint_at < pr_usage.first_checkpoint_at
                        ):
                            pr_usage.first_checkpoint_at = session_usage.first_checkpoint_at
                        if pr_usage.last_checkpoint_at is None or (
                            session_usage.last_checkpoint_at
                            and session_usage.last_checkpoint_at > pr_usage.last_checkpoint_at
                        ):
                            pr_usage.last_checkpoint_at = session_usage.last_checkpoint_at

    # Update and save PR usage
    pr_usage.last_updated = now
    pr_usage.update_cost()
    save_pr_usage(base_dir, pr_usage)

    # Update index with PR
    index = load_usage_index(base_dir)
    if pr_number not in index.pr_numbers:
        index.pr_numbers.append(pr_number)
        index.total_prs += 1
        index.last_updated = now
        save_usage_index(base_dir, index)

    return updated_count


def query_usage_by_issue(base_dir: Path, issue_number: int) -> IssueUsage | None:
    """
    Query usage by issue number.

    Args:
        base_dir: Base directory for usage data
        issue_number: Issue number to query

    Returns:
        IssueUsage if found, None otherwise
    """
    return load_issue_usage(base_dir, issue_number)


def query_usage_by_session(base_dir: Path, session_id: str) -> SessionUsage | None:
    """
    Query usage by session ID.

    Args:
        base_dir: Base directory for usage data
        session_id: Session ID to query

    Returns:
        SessionUsage if found, None otherwise
    """
    return load_session_usage(base_dir, session_id)


def query_usage_by_pr(base_dir: Path, pr_number: int) -> PRUsage | None:
    """
    Query usage by PR number.

    Args:
        base_dir: Base directory for usage data
        pr_number: PR number to query

    Returns:
        PRUsage if found, None otherwise
    """
    return load_pr_usage(base_dir, pr_number)


def query_usage_by_workflow(base_dir: Path, workflow_id: str) -> WorkflowUsage | None:
    """
    Query usage by workflow ID.

    Args:
        base_dir: Base directory for usage data
        workflow_id: Workflow ID to query

    Returns:
        WorkflowUsage if found, None otherwise
    """
    return load_workflow_usage(base_dir, workflow_id)


def get_usage_summary(base_dir: Path) -> UsageIndex:
    """
    Get a summary of all usage.

    Args:
        base_dir: Base directory for usage data

    Returns:
        UsageIndex with totals
    """
    return load_usage_index(base_dir)
