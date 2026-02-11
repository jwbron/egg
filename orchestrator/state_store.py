"""
Git-backed state persistence for pipeline state.

Stores pipeline state in .egg-state/pipelines/{id}.json on the work branch.
State survives orchestrator restarts by reading from git.
"""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from models import Pipeline, PipelineStatus


# Valid pipeline ID format: issue-{number} where number is 1+ digits
PIPELINE_ID_PATTERN = re.compile(r'^issue-[0-9]+$')


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

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command in the repository.

        Args:
            args: Git command arguments
            check: Whether to check return code

        Returns:
            CompletedProcess result

        Raises:
            GitOperationError: If command fails and check=True
        """
        cmd = ["git", "-C", str(self.repo_path)] + list(args)
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

        if commit:
            self._commit_state(pipeline, message)

        return path

    def _commit_state(self, pipeline: Pipeline, message: str | None = None) -> str:
        """Commit pipeline state changes.

        Args:
            pipeline: Pipeline being saved
            message: Optional commit message

        Returns:
            Commit SHA

        Raises:
            GitOperationError: If commit fails
        """
        path = self._get_pipeline_path(pipeline.id)
        rel_path = path.relative_to(self.repo_path)

        # Stage the file
        self._run_git("add", str(rel_path))

        # Check if there are changes to commit
        result = self._run_git("diff", "--cached", "--quiet", check=False)
        if result.returncode == 0:
            # No changes
            return self._get_current_commit()

        # Generate commit message
        if not message:
            message = self._generate_commit_message(pipeline)

        # Commit
        self._run_git("commit", "-m", message)

        return self._get_current_commit()

    def _get_current_commit(self) -> str:
        """Get the current HEAD commit SHA."""
        result = self._run_git("rev-parse", "HEAD")
        return result.stdout.strip()

    def _generate_commit_message(self, pipeline: Pipeline) -> str:
        """Generate a commit message for pipeline state update."""
        return f"Update pipeline state: {pipeline.id} ({pipeline.status.value})"

    def create_pipeline(
        self,
        issue_number: int,
        repo: str,
        branch: str,
        config: dict[str, Any] | None = None,
    ) -> Pipeline:
        """Create a new pipeline.

        Args:
            issue_number: GitHub issue number
            repo: Repository in owner/name format
            branch: Work branch name
            config: Optional pipeline configuration

        Returns:
            Created pipeline

        Raises:
            StateStoreError: If pipeline already exists
        """
        pipeline_id = f"issue-{issue_number}"

        if self.pipeline_exists(pipeline_id):
            raise StateStoreError(f"Pipeline {pipeline_id} already exists")

        pipeline = Pipeline(
            id=pipeline_id,
            issue_number=issue_number,
            repo=repo,
            branch=branch,
        )

        if config:
            from models import PipelineConfig

            pipeline.config = PipelineConfig.model_validate(config)

        self.save_pipeline(pipeline, message=f"Create pipeline for issue #{issue_number}")
        return pipeline

    def delete_pipeline(self, pipeline_id: str, commit: bool = True) -> None:
        """Delete a pipeline.

        Args:
            pipeline_id: Pipeline ID to delete
            commit: Whether to commit the deletion

        Raises:
            PipelineNotFoundError: If pipeline doesn't exist
        """
        path = self._get_pipeline_path(pipeline_id)
        if not path.exists():
            raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found")

        path.unlink()

        if commit:
            rel_path = path.relative_to(self.repo_path)
            self._run_git("add", str(rel_path))

            result = self._run_git("diff", "--cached", "--quiet", check=False)
            if result.returncode != 0:
                self._run_git("commit", "-m", f"Delete pipeline: {pipeline_id}")

    def list_pipelines(self) -> list[str]:
        """List all pipeline IDs.

        Returns:
            List of pipeline IDs
        """
        if not self.pipelines_dir.exists():
            return []

        return [
            p.stem
            for p in self.pipelines_dir.glob("*.json")
            if p.is_file()
        ]

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
