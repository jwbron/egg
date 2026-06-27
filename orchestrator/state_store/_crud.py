"""Pipeline CRUD + lifecycle for ``StateStore`` (#3312).

Method bodies extracted verbatim from the pre-split ``state_store.py`` as
module-level functions taking ``self`` explicitly. The barrel binds these onto
the ``StateStore`` class.

``get_pipeline_state_lock`` / ``release_pipeline_state_lock`` are resolved
through the package barrel (``_pkg``) so the pre-split module-global patch seam
(``patch("state_store.get_pipeline_state_lock")``) keeps taking effect inside
``create_pipeline`` / ``update_pipeline`` / ``delete_pipeline``.
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import state_store as _pkg
from models import Pipeline, PipelineMode, PipelinePhase, PipelineStatus
from pydantic import ValidationError

from . import logger
from ._errors import (
    GitOperationError,
    PipelineNotFoundError,
    StateStoreError,
    StateValidationError,
    VersionConflictError,
)


def pipeline_exists(self, pipeline_id: str) -> bool:
    """Check if a pipeline exists.

    Args:
        pipeline_id: Pipeline ID to check

    Returns:
        True if pipeline exists
    """
    return self._get_pipeline_path(pipeline_id).exists()


def load_pipeline(self, pipeline_id: str) -> Pipeline:
    """Load pipeline state from disk.

    Args:
        pipeline_id: Pipeline ID to load

    Returns:
        Pipeline state

    Raises:
        PipelineNotFoundError: If pipeline doesn't exist
        StateValidationError: If state is invalid
    """
    path = self._get_pipeline_path(pipeline_id)
    if not path.exists():
        raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found")

    try:
        content = path.read_text()
        if not content.strip():
            logger.warning(
                "Pipeline state file is empty, treating as missing: %s",
                path,
            )
            raise PipelineNotFoundError(
                f"Pipeline {pipeline_id} state file is empty (likely corrupt)"
            )
        data = json.loads(content)
        return Pipeline.model_validate(data)
    except json.JSONDecodeError as e:
        raise StateValidationError(f"Invalid JSON in pipeline state: {e}") from e
    except ValidationError as e:
        raise StateValidationError(f"Pipeline state validation failed: {e}") from e


def save_pipeline(
    self,
    pipeline: Pipeline,
    commit: bool = True,
    message: str | None = None,
    expected_version: int | None = None,
) -> Path:
    """Save pipeline state to disk with optimistic locking.

    Args:
        pipeline: Pipeline state to save
        commit: Whether to commit the change
        message: Commit message (auto-generated if not provided)
        expected_version: If provided, checks that current version matches
                          before saving (optimistic locking)

    Returns:
        Path to saved file

    Raises:
        GitOperationError: If non-commit git operations fail (e.g., directory
            setup). Commit failures are caught and logged — the file is saved
            on disk but may not be committed to git until the next save.
        VersionConflictError: If expected_version doesn't match current version
    """
    self._ensure_dir()
    path = self._get_pipeline_path(pipeline.id)

    # Optimistic locking check
    if expected_version is not None and path.exists():
        try:
            current = self.load_pipeline(pipeline.id)
            if current.version != expected_version:
                raise VersionConflictError(
                    f"Version conflict for pipeline {pipeline.id}: "
                    f"expected version {expected_version}, but current is {current.version}"
                )
        except PipelineNotFoundError:
            pass  # New pipeline, no conflict possible

    # Update timestamp and increment version
    pipeline.updated_at = datetime.now(UTC)
    pipeline.version = (pipeline.version or 0) + 1

    # Write state atomically: write to temp file, then rename.
    # Using dir=path.parent ensures same-filesystem rename (atomic on POSIX).
    json_data = pipeline.model_dump_json(indent=2)
    fd, temp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json_data)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    # Commit unconditionally: the on-disk file alone is not durable. The
    # state worktree may sit on a pod-lifetime volume, so any save that
    # skips the commit exists only until the next pod recreation — that is
    # how prompt-driven pipelines parked at a HITL gate vanished in #3070
    # (the old gate committed them only on phase advance/completion).
    if commit:
        try:
            self._commit_state(pipeline, message)
        except GitOperationError:
            logger.error(
                "Failed to commit pipeline state for %s; file is saved on disk, "
                "commit will be retried on next save",
                pipeline.id,
                exc_info=True,
            )

    return path


def create_pipeline(
    self,
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    config: dict[str, Any] | None = None,
    prompt: str | None = None,
    pipeline_id: str | None = None,
    network_mode: str | None = None,
    mode: PipelineMode | None = None,
    analysis: str | None = None,
    plan: str | None = None,
    source_branch: str | None = None,
    source_artifact_prefix: str | None = None,
    has_contract: bool = True,
    jira_ticket: str | None = None,
    is_epic: bool = False,
    pipeline_mode: str | None = None,
) -> Pipeline:
    """Create a new pipeline.

    Args:
        issue_number: GitHub issue number (optional)
        repo: Repository in owner/name format
        branch: Work branch name
        base_branch: Base branch for PR creation (optional, auto-detected if not set)
        config: Optional pipeline configuration
        prompt: User prompt (for prompt-driven pipelines)
        pipeline_id: Explicit pipeline ID (auto-generated if not provided)
        network_mode: Network mode for spawned containers ("public", "private", or None)
        mode: Pipeline mode. Defaults to ISSUE.
        analysis: Pre-generated analysis markdown for short flow pipelines (optional).
        plan: Pre-generated plan markdown with yaml-tasks appendix (optional).
        source_branch: Source branch to read prior-run artifacts from (optional).
        source_artifact_prefix: Explicit prefix for draft filenames on
            the source branch (e.g. ``"issue-1570-v3"``).  Overrides
            the default pipeline_id-based prefix when reading artifacts.

    Returns:
        Created pipeline

    Raises:
        StateStoreError: If an active pipeline with the same ID already exists
    """
    if not pipeline_id:
        if issue_number:
            pipeline_id = f"issue-{issue_number}"
        else:
            pipeline_id = f"pipeline-{os.urandom(4).hex()}"

    with _pkg.get_pipeline_state_lock(pipeline_id):
        if self.pipeline_exists(pipeline_id):
            existing = self.load_pipeline(pipeline_id)
            if existing.status in PipelineStatus.terminal():
                logger.info(
                    "Replacing terminal pipeline %s (status=%s)",
                    pipeline_id,
                    existing.status.value,
                )
                self.delete_pipeline(pipeline_id, commit=True, cleanup_lock=False)
            else:
                raise StateStoreError(f"Pipeline {pipeline_id} already exists")

        pipeline_kwargs: dict[str, Any] = {
            "id": pipeline_id,
            "issue_number": issue_number,
            "repo": repo,
            "branch": branch,
            "base_branch": base_branch,
            "prompt": prompt,
            "network_mode": network_mode,
            # Contract is created separately — mark as unsynced until verified
            "contract_synced": False,
            "analysis": analysis,
            "plan": plan,
            "source_branch": source_branch,
            "source_artifact_prefix": source_artifact_prefix,
            "has_contract": has_contract,
        }
        if mode is not None:
            pipeline_kwargs["mode"] = mode
        # Issue #1557: persist Jira-epic SDLC fields on the Pipeline.
        if jira_ticket is not None:
            pipeline_kwargs["jira_ticket"] = jira_ticket
        if is_epic:
            pipeline_kwargs["is_epic"] = True
        if pipeline_mode is not None:
            pipeline_kwargs["pipeline_mode"] = pipeline_mode
        pipeline = Pipeline(**pipeline_kwargs)

        if config:
            from models import PipelineConfig

            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except json.JSONDecodeError as e:
                    raise StateStoreError(f"Invalid config JSON: {e}") from e
            pipeline.config = PipelineConfig.model_validate(config)

        # Honor start_phase: set current_phase at creation time so
        # get_status never returns a stale phase before _run_pipeline
        # gets to update it.
        if pipeline.config.start_phase:
            pipeline.current_phase = PipelinePhase(pipeline.config.start_phase)

        commit_msg = f"Create pipeline {pipeline_id}"
        self.save_pipeline(pipeline, message=commit_msg)
        return pipeline


def delete_pipeline(
    self,
    pipeline_id: str,
    commit: bool = True,
    cleanup_lock: bool = True,
) -> None:
    """Delete a pipeline.

    Args:
        pipeline_id: Pipeline ID to delete
        commit: Whether to commit the deletion
        cleanup_lock: Whether to release the per-pipeline state lock.
            Set to False when called from within a lock (e.g. create_pipeline
            replacing a terminal pipeline) to avoid removing the lock while
            the caller still holds it.

    Raises:
        PipelineNotFoundError: If pipeline doesn't exist
    """
    path = self._get_pipeline_path(pipeline_id)
    if not path.exists():
        raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found")

    rel_path = str(path.relative_to(self.worktree))
    path.unlink()

    # Clean up the per-pipeline state lock to prevent unbounded growth
    if cleanup_lock:
        _pkg.release_pipeline_state_lock(pipeline_id)

    # Commit unconditionally (same durability invariant as save_pipeline,
    # #3070): a deletion that only happens on disk resurrects the pipeline
    # on the next pod recreation.
    if commit:
        wt = self.worktree
        with self._git_op():
            self._run_git("add", rel_path, cwd=wt)

            result = self._run_git("diff", "--cached", "--quiet", cwd=wt, check=False)
            if result.returncode != 0:
                self._run_git(
                    "commit",
                    "--no-verify",
                    "-m",
                    f"Delete pipeline: {pipeline_id}",
                    cwd=wt,
                )
                # Sync deletion to remote (mirrors _commit_state behavior)
                self._sync_to_remote_async()


def list_pipelines(self) -> list[str]:
    """List all pipeline IDs.

    Returns:
        List of pipeline IDs
    """
    if not self.pipelines_dir.exists():
        return []

    return [p.stem for p in self.pipelines_dir.glob("*.json") if p.is_file()]


def get_active_pipelines(self) -> list[Pipeline]:
    """Get all active (non-terminal) pipelines.

    Returns:
        List of active pipelines
    """
    terminal_statuses = PipelineStatus.terminal()

    pipelines = []
    for pipeline_id in self.list_pipelines():
        try:
            pipeline = self.load_pipeline(pipeline_id)
            if pipeline.status not in terminal_statuses:
                pipelines.append(pipeline)
        except StateStoreError:
            # Skip invalid pipelines
            continue

    return pipelines


def pipelines_for_jira_ticket(self, ticket: str) -> list[Pipeline]:
    """Reverse-index lookup: pipelines whose ``jira_ticket`` matches.

    Added for issue #1557 slice-2 — the reassess sweep's in-flight
    classifier checks every existing child Jira key against this
    index to find prior egg pipelines that already opened a PR for
    the same child. A non-empty result (with at least one entry
    whose ``pr_url`` is set) implies "in-flight" and the planner
    refuses to mutate the ticket without a per-ticket HITL marker.

    The implementation is a straight scan over the on-disk pipeline
    index. It's intentionally simple — most repos hold a few dozen
    active pipelines at a time and the sweep runs at most once per
    epic per reassess pass. If the active-pipeline count grows past
    a few hundred a per-ticket secondary index can be layered on
    top without changing this public signature.

    Args:
        ticket: Atlassian Jira ticket key (e.g. ``"ENG-1234"``).
            Comparison is case-insensitive — the canonical Pipeline
            shape uppercases the project segment.

    Returns:
        List of ``Pipeline`` objects whose ``jira_ticket`` equals
        ``ticket`` (after case-folding), in undefined order. Empty
        list when no pipelines reference the ticket.
    """
    if not ticket or not isinstance(ticket, str):
        return []
    target = ticket.strip().upper()
    if not target:
        return []

    result: list[Pipeline] = []
    for pipeline_id in self.list_pipelines():
        try:
            pipeline = self.load_pipeline(pipeline_id)
        except StateStoreError:
            # Corrupt index entries are ignored — the sweep is
            # best-effort and a missing pipeline is equivalent to
            # the index never having seen it.
            continue
        jira = getattr(pipeline, "jira_ticket", None)
        if jira and isinstance(jira, str) and jira.upper() == target:
            result.append(pipeline)
    return result


def update_pipeline(
    self,
    pipeline_id: str,
    updates: dict[str, Any],
    commit: bool = True,
) -> Pipeline:
    """Update pipeline state with partial updates.

    Uses a per-pipeline lock to make the load-modify-save cycle atomic,
    preventing concurrent writers (e.g. DecisionQueue.resolve_decision)
    from having their changes silently overwritten.

    Args:
        pipeline_id: Pipeline ID to update
        updates: Dictionary of field updates
        commit: Whether to commit the change

    Returns:
        Updated pipeline

    Raises:
        PipelineNotFoundError: If pipeline doesn't exist
        StateValidationError: If updates are invalid
    """
    with _pkg.get_pipeline_state_lock(pipeline_id):
        pipeline = self.load_pipeline(pipeline_id)

        # Apply updates
        data = pipeline.model_dump()
        for key, value in updates.items():
            if "." in key:
                # Nested update
                parts = key.split(".")
                target = data
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
            else:
                data[key] = value

        # Validate and save
        try:
            pipeline = Pipeline.model_validate(data)
        except ValidationError as e:
            raise StateValidationError(f"Update validation failed: {e}") from e

        self.save_pipeline(pipeline, commit=commit)
        return pipeline
