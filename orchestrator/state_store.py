"""
Git-backed state persistence for pipeline state.

Stores pipeline state in .egg-state/pipelines/{id}.json.  Files are written
to disk for fast reads and committed to a dedicated ``egg/pipeline-state``
orphan branch so state history is preserved without polluting main.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from models import Pipeline, PipelineStatus
from pydantic import ValidationError

# Valid pipeline ID format: issue-{number} or local-{8 hex chars}
PIPELINE_ID_PATTERN = re.compile(r"^(issue-[0-9]+|local-[0-9a-f]{8})$")

# Dedicated branch for pipeline state (orphan, never merged into main)
STATE_BRANCH = "egg/pipeline-state"


class StateStoreError(Exception):
    """Base exception for state store errors."""

    pass


class PipelineNotFoundError(StateStoreError):
    """Pipeline state not found."""

    pass


class StateValidationError(StateStoreError):
    """Pipeline state validation failed."""

    pass


class GitOperationError(StateStoreError):
    """Git operation failed."""

    pass


class InvalidPipelineIdError(StateStoreError):
    """Invalid pipeline ID format."""

    pass


class VersionConflictError(StateStoreError):
    """Optimistic locking version conflict."""

    pass


def _validate_pipeline_id(pipeline_id: str) -> None:
    """Validate pipeline ID format to prevent path traversal attacks.

    Args:
        pipeline_id: Pipeline ID to validate

    Raises:
        InvalidPipelineIdError: If pipeline ID format is invalid
    """
    if not pipeline_id or not PIPELINE_ID_PATTERN.match(pipeline_id):
        raise InvalidPipelineIdError(f"Invalid pipeline ID format: {pipeline_id}")


class StateStore:
    """Git-backed state store for pipeline state.

    Stores state in .egg-state/pipelines/{id}.json within the repository.
    Changes are committed to preserve history.
    """

    PIPELINES_DIR = ".egg-state/pipelines"

    def __init__(self, repo_path: Path):
        """Initialize state store for a repository.

        Args:
            repo_path: Path to the git repository
        """
        self.repo_path = repo_path
        self.pipelines_dir = repo_path / self.PIPELINES_DIR

    def _get_pipeline_path(self, pipeline_id: str) -> Path:
        """Get the file path for a pipeline's state.

        Args:
            pipeline_id: Pipeline ID

        Returns:
            Path to the pipeline state file

        Raises:
            InvalidPipelineIdError: If pipeline ID format is invalid
        """
        _validate_pipeline_id(pipeline_id)
        path = self.pipelines_dir / f"{pipeline_id}.json"
        # Additional safety: ensure the resolved path stays within pipelines_dir
        resolved = path.resolve()
        if not resolved.is_relative_to(self.pipelines_dir.resolve()):
            raise InvalidPipelineIdError(f"Path traversal detected in pipeline ID: {pipeline_id}")
        return path

    def _ensure_dir(self) -> None:
        """Ensure the pipelines directory exists."""
        self.pipelines_dir.mkdir(parents=True, exist_ok=True)

    def _run_git(
        self,
        *args: str,
        check: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a git command in the repository.

        Args:
            args: Git command arguments
            check: Whether to check return code
            cwd: Working directory (default: self.repo_path)

        Returns:
            CompletedProcess result

        Raises:
            GitOperationError: If command fails and check=True
        """
        work_dir = str(cwd) if cwd else str(self.repo_path)
        cmd = ["git", "-C", work_dir] + list(args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=check,
            )
            return result
        except subprocess.CalledProcessError as e:
            raise GitOperationError(f"Git command failed: {e.stderr}") from e

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
            with path.open() as f:
                data = json.load(f)
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
        force_commit: bool = False,
    ) -> Path:
        """Save pipeline state to disk with optimistic locking.

        Args:
            pipeline: Pipeline state to save
            commit: Whether to commit the change (ignored for local pipelines unless force_commit=True)
            message: Commit message (auto-generated if not provided)
            expected_version: If provided, checks that current version matches
                              before saving (optimistic locking)
            force_commit: If True, commit even for local pipelines. Use at phase
                          boundaries to ensure state is persisted to git.

        Returns:
            Path to saved file

        Raises:
            GitOperationError: If git operations fail
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
        pipeline.updated_at = datetime.utcnow()
        pipeline.version = (pipeline.version or 0) + 1

        # Write state
        with path.open("w") as f:
            f.write(pipeline.model_dump_json(indent=2))

        # For local pipelines, only commit if force_commit is True (phase boundaries)
        # For issue pipelines, always commit when commit=True
        is_local = getattr(pipeline, "mode", "issue") == "local"
        should_commit = commit and (not is_local or force_commit)
        if should_commit:
            self._commit_state(pipeline, message)

        return path

    def _commit_state(self, pipeline: Pipeline, message: str | None = None) -> str:
        """Commit pipeline state to the dedicated state branch.

        Uses a temporary git worktree so the main checkout is unaffected.

        Args:
            pipeline: Pipeline being saved
            message: Optional commit message

        Returns:
            Commit SHA (on the state branch)

        Raises:
            GitOperationError: If commit fails
        """
        if not message:
            message = self._generate_commit_message(pipeline)

        src_path = self._get_pipeline_path(pipeline.id)
        rel_path = str(src_path.relative_to(self.repo_path))

        return self._commit_to_state_branch(
            files={rel_path: src_path},
            message=message,
        )

    def _state_branch_exists(self) -> bool:
        """Check if the state branch exists locally."""
        result = self._run_git("rev-parse", "--verify", f"refs/heads/{STATE_BRANCH}", check=False)
        return result.returncode == 0

    def _commit_to_state_branch(
        self,
        files: dict[str, Path],
        message: str,
        delete_paths: list[str] | None = None,
    ) -> str:
        """Commit files to the dedicated state branch via a temp worktree.

        Args:
            files: Mapping of relative paths -> source file paths to copy
            message: Commit message
            delete_paths: Relative paths to remove from the branch

        Returns:
            Commit SHA of the new commit
        """
        temp_dir = tempfile.mkdtemp(prefix="egg_state_")
        temp_path = Path(temp_dir)

        try:
            branch_exists = self._state_branch_exists()

            if branch_exists:
                self._run_git("worktree", "add", "--detach", str(temp_path), STATE_BRANCH)
            else:
                # Create orphan branch via detached worktree
                self._run_git("worktree", "add", "--detach", str(temp_path))
                self._run_git("checkout", "--orphan", STATE_BRANCH, cwd=temp_path)
                # Clear any inherited files from the index
                self._run_git("rm", "-rf", "--cached", ".", cwd=temp_path, check=False)
                # Remove inherited working directory files
                for item in temp_path.iterdir():
                    if item.name == ".git":
                        continue
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()

            # Copy files into the worktree
            for rel, src in files.items():
                if not src.exists():
                    continue
                dest = temp_path / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

            # Handle deletions
            if delete_paths:
                for rel in delete_paths:
                    target = temp_path / rel
                    if target.exists():
                        target.unlink()
                    self._run_git("rm", "--cached", rel, cwd=temp_path, check=False)

            # Stage and commit
            paths_to_add = [r for r in files if (temp_path / r).exists()]
            if paths_to_add:
                self._run_git("add", *paths_to_add, cwd=temp_path)

            result = self._run_git("diff", "--cached", "--quiet", cwd=temp_path, check=False)
            if result.returncode == 0:
                # No changes
                return self._run_git("rev-parse", "HEAD", cwd=temp_path).stdout.strip()

            self._run_git("commit", "--no-verify", "-m", message, cwd=temp_path)

            commit_sha = self._run_git("rev-parse", "HEAD", cwd=temp_path).stdout.strip()

            # Update the state branch ref to the new commit
            self._run_git("update-ref", f"refs/heads/{STATE_BRANCH}", commit_sha)

            return commit_sha

        finally:
            # Clean up worktree
            self._run_git("worktree", "remove", "--force", str(temp_path), check=False)
            # Belt-and-suspenders: remove temp dir if worktree remove failed
            if temp_path.exists():
                shutil.rmtree(temp_path, ignore_errors=True)

    def _get_current_commit(self) -> str:
        """Get the current HEAD commit SHA."""
        result = self._run_git("rev-parse", "HEAD")
        return result.stdout.strip()

    def _generate_commit_message(self, pipeline: Pipeline) -> str:
        """Generate a commit message for pipeline state update."""
        return f"Update pipeline state: {pipeline.id} ({pipeline.status.value})"

    def create_pipeline(
        self,
        issue_number: int | None = None,
        repo: str | None = None,
        branch: str | None = None,
        config: dict[str, Any] | None = None,
        mode: str = "issue",
        prompt: str | None = None,
        pipeline_id: str | None = None,
    ) -> Pipeline:
        """Create a new pipeline.

        Args:
            issue_number: GitHub issue number (required for issue mode)
            repo: Repository in owner/name format (required for issue mode)
            branch: Work branch name (required for issue mode)
            config: Optional pipeline configuration
            mode: Pipeline mode - "issue" or "local"
            prompt: User prompt (required for local mode)
            pipeline_id: Explicit pipeline ID (auto-generated if not provided)

        Returns:
            Created pipeline

        Raises:
            StateStoreError: If pipeline already exists
        """
        if mode == "local":
            if not pipeline_id:
                pipeline_id = f"local-{os.urandom(4).hex()}"
        else:
            if not issue_number:
                raise StateStoreError("issue_number is required for issue-mode pipelines")
            pipeline_id = pipeline_id or f"issue-{issue_number}"

        if self.pipeline_exists(pipeline_id):
            raise StateStoreError(f"Pipeline {pipeline_id} already exists")

        pipeline = Pipeline(
            id=pipeline_id,
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            mode=mode,
            prompt=prompt,
            # Contract is created separately — mark as unsynced until verified
            contract_synced=False,
        )

        if config:
            from models import PipelineConfig

            pipeline.config = PipelineConfig.model_validate(config)

        commit_msg = (
            f"Create local pipeline {pipeline_id}"
            if mode == "local"
            else f"Create pipeline for issue #{issue_number}"
        )
        self.save_pipeline(pipeline, message=commit_msg)
        return pipeline

    def delete_pipeline(
        self, pipeline_id: str, commit: bool = True, force_commit: bool = False
    ) -> None:
        """Delete a pipeline.

        Args:
            pipeline_id: Pipeline ID to delete
            commit: Whether to commit the deletion (ignored for local unless force_commit)
            force_commit: If True, commit deletion even for local pipelines

        Raises:
            PipelineNotFoundError: If pipeline doesn't exist
        """
        path = self._get_pipeline_path(pipeline_id)
        if not path.exists():
            raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found")

        path.unlink()

        # For local pipelines, only commit if force_commit is True
        # For issue pipelines, always commit when commit=True
        is_local = pipeline_id.startswith("local-")
        should_commit = commit and (not is_local or force_commit)
        if should_commit:
            rel_path = str(path.relative_to(self.repo_path))
            self._commit_to_state_branch(
                files={},
                message=f"Delete pipeline: {pipeline_id}",
                delete_paths=[rel_path],
            )

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
        terminal_statuses = {
            PipelineStatus.COMPLETE,
            PipelineStatus.FAILED,
            PipelineStatus.CANCELLED,
        }

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

    def update_pipeline(
        self,
        pipeline_id: str,
        updates: dict[str, Any],
        commit: bool = True,
    ) -> Pipeline:
        """Update pipeline state with partial updates.

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


def get_state_store(repo_path: Path | str) -> StateStore:
    """Get a state store for a repository.

    Args:
        repo_path: Path to the repository

    Returns:
        StateStore instance
    """
    if isinstance(repo_path, str):
        repo_path = Path(repo_path)
    return StateStore(repo_path)
