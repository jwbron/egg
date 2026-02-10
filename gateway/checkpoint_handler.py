"""
Checkpoint handler for the gateway sidecar.

Captures agent session context on successful git push operations and stores
checkpoints in a dedicated branch (egg/checkpoints/v1).

Integration points:
- Called from gateway.py after successful push
- Uses SessionManager for session context
- Uses transcript_extractor for Claude Code session data
- Uses checkpoint_loader for atomic writes
"""

import os
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))

from egg_contracts.checkpoint_loader import (
    CheckpointSaveError,
    generate_checkpoint_id_from_commit,
    get_checkpoint_path,
    save_checkpoint,
)
from egg_contracts.checkpoints import (
    Checkpoint,
    CheckpointSummary,
    FileOperation,
    SessionMetadata,
    TokenUsage,
    ToolCall,
    Transcript,
)
from egg_contracts.redactor import Redactor, RedactorConfig
from egg_contracts.transcript_extractor import (
    TranscriptExtractError,
    extract_transcript_from_jsonl,
    find_session_file,
)
from egg_logging import get_logger

from .session_manager import Session

logger = get_logger("gateway.checkpoint-handler")

# Checkpoint branch name
CHECKPOINT_BRANCH = "egg/checkpoints/v1"

# Index file name
INDEX_FILE = "index.json"

# Maximum transcript size before truncation (bytes)
MAX_TRANSCRIPT_SIZE = 1_000_000  # 1MB

# Default Claude projects directory
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Feature flag for enabling/disabling checkpoints
CHECKPOINT_ENABLED = os.environ.get("CHECKPOINT_ENABLED", "true").lower() == "true"


class CheckpointError(Exception):
    """Error during checkpoint capture."""

    pass


class CheckpointHandler:
    """
    Handles checkpoint capture and storage for agent sessions.

    This class is called after successful git push operations to capture
    the session context and store it in the checkpoint branch.
    """

    def __init__(
        self,
        github_token: str | None = None,
        redactor_config: RedactorConfig | None = None,
    ):
        """
        Initialize the checkpoint handler.

        Args:
            github_token: GitHub token for pushing to checkpoint branch
            redactor_config: Optional redactor configuration
        """
        self._github_token = github_token
        self._redactor = Redactor(redactor_config)

    def capture_checkpoint(
        self,
        repo_path: str,
        commit_sha: str,
        branch: str,
        session: Session | None = None,
        issue_number: int | None = None,
        pipeline_phase: str | None = None,
        push_sha: str | None = None,
    ) -> Checkpoint | None:
        """
        Capture a checkpoint for a commit.

        This method:
        1. Finds the current Claude Code session file
        2. Extracts transcript, tool calls, and token usage
        3. Applies redaction for sensitive data
        4. Creates and returns a Checkpoint object

        Args:
            repo_path: Path to the repository
            commit_sha: The commit SHA to checkpoint
            branch: The branch where the commit was made
            session: Optional Session object with container/role info
            issue_number: Optional GitHub issue number
            pipeline_phase: Optional SDLC pipeline phase
            push_sha: Optional tip commit SHA of the push

        Returns:
            Checkpoint object if successful, None if capture fails

        Raises:
            CheckpointError: If checkpoint capture fails critically
        """
        if not CHECKPOINT_ENABLED:
            logger.debug("Checkpoint capture disabled by CHECKPOINT_ENABLED=false")
            return None

        try:
            # Find the session file
            project_path = Path(repo_path)
            session_file = find_session_file(
                project_path=project_path,
                claude_projects_dir=CLAUDE_PROJECTS_DIR,
            )

            if not session_file:
                logger.debug(
                    "No session file found for checkpoint",
                    repo_path=repo_path,
                    commit_sha=commit_sha[:7],
                )
                # Create a minimal checkpoint without transcript
                return self._create_minimal_checkpoint(
                    commit_sha=commit_sha,
                    branch=branch,
                    session=session,
                    issue_number=issue_number,
                    pipeline_phase=pipeline_phase,
                    push_sha=push_sha,
                )

            # Extract transcript data
            try:
                (
                    session_metadata,
                    transcript,
                    tool_calls,
                    file_operations,
                    token_usage,
                ) = extract_transcript_from_jsonl(session_file)
            except TranscriptExtractError as e:
                logger.warning(
                    "Failed to extract transcript",
                    error=str(e),
                    session_file=str(session_file),
                )
                return self._create_minimal_checkpoint(
                    commit_sha=commit_sha,
                    branch=branch,
                    session=session,
                    issue_number=issue_number,
                    pipeline_phase=pipeline_phase,
                    push_sha=push_sha,
                )

            # Apply redaction
            transcript = self._redact_transcript(transcript)
            tool_calls = self._redact_tool_calls(tool_calls)

            # Merge session info if provided
            if session:
                session_metadata.container_id = session.container_id
                session_metadata.agent_role = session.agent_role

            # Get issue number from environment if not provided
            if issue_number is None:
                issue_str = os.environ.get("EGG_ISSUE_NUMBER")
                if issue_str:
                    try:
                        issue_number = int(issue_str)
                    except ValueError:
                        pass

            # Get pipeline phase from environment if not provided
            if pipeline_phase is None:
                pipeline_phase = os.environ.get("EGG_PIPELINE_PHASE")

            # Generate checkpoint ID
            checkpoint_id = generate_checkpoint_id_from_commit(
                commit_sha, session_metadata.session_id
            )

            # Create checkpoint
            checkpoint = Checkpoint(
                id=checkpoint_id,
                commit_sha=commit_sha,
                session=session_metadata,
                transcript=transcript,
                files_touched=file_operations,
                tool_calls=tool_calls,
                token_usage=token_usage,
                issue_number=issue_number,
                pipeline_phase=pipeline_phase,
                branch=branch,
                created_at=datetime.now(UTC),
                push_sha=push_sha or commit_sha,
            )

            logger.info(
                "Checkpoint captured",
                checkpoint_id=checkpoint_id,
                commit_sha=commit_sha[:7],
                message_count=transcript.message_count,
                tool_call_count=len(tool_calls),
                total_tokens=token_usage.total_tokens,
            )

            return checkpoint

        except Exception as e:
            logger.error(
                "Checkpoint capture failed",
                error=str(e),
                commit_sha=commit_sha[:7],
            )
            # Don't raise - checkpoint failure should not block push
            return None

    def _create_minimal_checkpoint(
        self,
        commit_sha: str,
        branch: str,
        session: Session | None = None,
        issue_number: int | None = None,
        pipeline_phase: str | None = None,
        push_sha: str | None = None,
    ) -> Checkpoint:
        """Create a minimal checkpoint without transcript data."""
        session_id = session.container_id if session else "unknown"
        checkpoint_id = generate_checkpoint_id_from_commit(commit_sha, session_id)

        session_metadata = SessionMetadata(
            session_id=session_id,
            container_id=session.container_id if session else None,
            agent_role=session.agent_role if session else None,
            started_at=datetime.now(UTC),
        )

        return Checkpoint(
            id=checkpoint_id,
            commit_sha=commit_sha,
            session=session_metadata,
            issue_number=issue_number,
            pipeline_phase=pipeline_phase,
            branch=branch,
            created_at=datetime.now(UTC),
            push_sha=push_sha or commit_sha,
        )

    def _redact_transcript(self, transcript: Transcript) -> Transcript:
        """Redact sensitive data from transcript messages."""
        redacted_messages = []
        for msg in transcript.messages:
            redacted_content = None
            if msg.content:
                redacted_content = self._redactor.redact_text(msg.content)

            redacted_messages.append(
                msg.model_copy(update={"content": redacted_content})
            )

        return transcript.model_copy(update={"messages": redacted_messages})

    def _redact_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        """Redact sensitive data from tool calls."""
        redacted_calls = []
        for tc in tool_calls:
            # Redact parameters
            redacted_params = self._redactor.redact_dict(tc.parameters)

            # Redact result summary
            redacted_result = None
            if tc.result_summary:
                redacted_result = self._redactor.redact_text(tc.result_summary)

            # Special handling for Bash commands
            if tc.name == "Bash" and "command" in redacted_params:
                redacted_params["command"] = self._redactor.redact_command(
                    str(redacted_params["command"])
                )

            redacted_calls.append(
                tc.model_copy(
                    update={
                        "parameters": redacted_params,
                        "result_summary": redacted_result,
                    }
                )
            )

        return redacted_calls

    def store_checkpoint(
        self,
        checkpoint: Checkpoint,
        repo_path: str,
        remote: str = "origin",
    ) -> bool:
        """
        Store a checkpoint in the checkpoint branch.

        This method:
        1. Creates the checkpoint branch if it doesn't exist
        2. Writes the checkpoint JSON to the branch
        3. Updates the index
        4. Pushes to the remote

        Args:
            checkpoint: The checkpoint to store
            repo_path: Path to the repository
            remote: Git remote name

        Returns:
            True if successful, False otherwise
        """
        if not CHECKPOINT_ENABLED:
            return False

        try:
            # Create a temporary directory for the checkout
            with tempfile.TemporaryDirectory(prefix="checkpoint_") as temp_dir:
                temp_path = Path(temp_dir)

                # Check if checkpoint branch exists
                branch_exists = self._branch_exists(repo_path, remote, CHECKPOINT_BRANCH)

                if branch_exists:
                    # Fetch the branch
                    self._run_git(
                        repo_path,
                        ["fetch", remote, f"{CHECKPOINT_BRANCH}:{CHECKPOINT_BRANCH}"],
                    )
                    # Checkout the branch to temp directory
                    self._run_git(
                        repo_path,
                        [
                            "worktree",
                            "add",
                            "--detach",
                            str(temp_path),
                            CHECKPOINT_BRANCH,
                        ],
                    )
                else:
                    # Create orphan branch
                    self._run_git(
                        repo_path,
                        ["worktree", "add", "--detach", str(temp_path)],
                    )
                    # Initialize as orphan
                    self._run_git(
                        str(temp_path),
                        ["checkout", "--orphan", CHECKPOINT_BRANCH],
                    )
                    # Remove any files
                    self._run_git(str(temp_path), ["rm", "-rf", "."], check=False)

                try:
                    # Get checkpoint path
                    checkpoint_path = get_checkpoint_path(
                        temp_path / "checkpoints", checkpoint.id
                    )

                    # Save checkpoint
                    save_checkpoint(checkpoint, checkpoint_path)

                    # Stage the file
                    self._run_git(
                        str(temp_path),
                        ["add", str(checkpoint_path.relative_to(temp_path))],
                    )

                    # Commit
                    commit_msg = (
                        f"Add checkpoint {checkpoint.id} for commit {checkpoint.commit_sha[:7]}"
                    )
                    self._run_git(
                        str(temp_path),
                        ["commit", "-m", commit_msg],
                    )

                    # Push
                    self._run_git(
                        str(temp_path),
                        ["push", remote, f"HEAD:{CHECKPOINT_BRANCH}"],
                    )

                    logger.info(
                        "Checkpoint stored",
                        checkpoint_id=checkpoint.id,
                        branch=CHECKPOINT_BRANCH,
                    )

                    return True

                finally:
                    # Clean up worktree
                    self._run_git(
                        repo_path,
                        ["worktree", "remove", "--force", str(temp_path)],
                        check=False,
                    )

        except Exception as e:
            logger.error(
                "Failed to store checkpoint",
                error=str(e),
                checkpoint_id=checkpoint.id,
            )
            return False

    def _branch_exists(self, repo_path: str, remote: str, branch: str) -> bool:
        """Check if a branch exists on the remote."""
        try:
            result = subprocess.run(
                ["git", "ls-remote", "--heads", remote, branch],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def _run_git(
        self,
        cwd: str,
        args: list[str],
        check: bool = True,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command."""
        env = os.environ.copy()

        # Add GitHub token if available
        if self._github_token:
            env["GIT_ASKPASS"] = "echo"
            env["GIT_USERNAME"] = "x-access-token"
            env["GIT_PASSWORD"] = self._github_token

        cmd = ["git"] + args

        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )

        if check and result.returncode != 0:
            raise CheckpointError(f"Git command failed: {result.stderr}")

        return result


# Global checkpoint handler instance
_checkpoint_handler: CheckpointHandler | None = None
_handler_lock = threading.Lock()


def get_checkpoint_handler(github_token: str | None = None) -> CheckpointHandler:
    """Get the global checkpoint handler instance."""
    global _checkpoint_handler
    if _checkpoint_handler is None:
        with _handler_lock:
            if _checkpoint_handler is None:
                _checkpoint_handler = CheckpointHandler(github_token=github_token)
    return _checkpoint_handler


def capture_and_store_checkpoint(
    repo_path: str,
    commit_sha: str,
    branch: str,
    session: Session | None = None,
    issue_number: int | None = None,
    pipeline_phase: str | None = None,
    push_sha: str | None = None,
    github_token: str | None = None,
    async_store: bool = True,
) -> Checkpoint | None:
    """
    Convenience function to capture and store a checkpoint.

    This is the main entry point for checkpoint capture from gateway.py.
    By default, storage is done asynchronously to not block the push response.

    Args:
        repo_path: Path to the repository
        commit_sha: The commit SHA to checkpoint
        branch: The branch where the commit was made
        session: Optional Session object with container/role info
        issue_number: Optional GitHub issue number
        pipeline_phase: Optional SDLC pipeline phase
        push_sha: Optional tip commit SHA of the push
        github_token: Optional GitHub token for pushing
        async_store: If True, store checkpoint asynchronously

    Returns:
        Checkpoint object if capture successful, None otherwise
    """
    if not CHECKPOINT_ENABLED:
        return None

    handler = get_checkpoint_handler(github_token)

    # Capture checkpoint
    checkpoint = handler.capture_checkpoint(
        repo_path=repo_path,
        commit_sha=commit_sha,
        branch=branch,
        session=session,
        issue_number=issue_number,
        pipeline_phase=pipeline_phase,
        push_sha=push_sha,
    )

    if not checkpoint:
        return None

    # Store checkpoint (async or sync)
    if async_store:
        # Store in background thread to not block push response
        thread = threading.Thread(
            target=handler.store_checkpoint,
            args=(checkpoint, repo_path),
            daemon=True,
        )
        thread.start()
    else:
        handler.store_checkpoint(checkpoint, repo_path)

    return checkpoint
