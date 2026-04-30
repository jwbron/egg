"""
Checkpoint handler for the gateway sidecar.

Captures agent session context on git push operations and session termination,
storing checkpoints in a dedicated branch (egg/checkpoints/v2). Supports two
trigger types:
- COMMIT: Created on git push (one checkpoint per push)
- SESSION_END: Created when a session terminates (graceful, expired, or crashed)

Architecture Overview:
    ┌──────────────────┐     ┌───────────────────────┐
    │   gateway.py     │────>│  checkpoint_handler   │
    │   (push handler) │     │  (this module)        │
    └──────────────────┘     └───────────────────────┘
                                        │
            ┌───────────────────────────┼────────────────────────┐
            │                           │                        │
            v                           v                        v
    ┌───────────────────┐   ┌───────────────────┐   ┌───────────────────┐
    │ transcript_buffer │   │  transcript_      │   │ checkpoint_loader │
    │ (API capture)     │   │  extractor        │   │ (storage)         │
    └───────────────────┘   └───────────────────┘   └───────────────────┘

Checkpoint Flow (push):
    1. gateway.py receives git push, forwards to GitHub
    2. On success, calls capture_and_store_checkpoints_for_push()
    3. Captures one v2 checkpoint for the push tip (new_sha) with trigger_type=COMMIT
    4. Stores checkpoint in egg/checkpoints/v2 branch (async)

    Note: One checkpoint is created per push regardless of how many commits it
    contains. Batched multi-commit pushes would produce identical checkpoint
    content (the transcript buffer state at push time), so per-commit creation
    would only generate duplicates.

Checkpoint Flow (session end):
    1. Session deletion/expiry/crash triggers capture_session_end_checkpoint()
    2. Extracts transcript from proxy buffer (if available)
    3. Creates v2 checkpoint with trigger_type=SESSION_END
    4. Stores checkpoint asynchronously with buffer preservation

Transcript Source:
    Transcripts are extracted from the API proxy buffer at
    /tmp/egg-transcripts/{container_id}.jsonl.

Integration points:
- Called from gateway.py after successful push
- Called from session_manager.py on session deletion/expiry
- Called from worktree_manager.py on orphan cleanup
- Uses checkpoint_loader for atomic writes to checkpoint branch
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))

from egg_contracts.checkpoint_loader import (
    add_checkpoint_to_index_v2,
    generate_checkpoint_id_from_commit,
    generate_checkpoint_id_v2,
    get_checkpoint_path,
    save_checkpoint_v2,
)
from egg_contracts.checkpoints import (
    AgentType,
    CheckpointIndexV2,
    CheckpointV2,
    FileOperation,
    InterAgentMessage,
    SessionMetadata,
    SessionStatus,
    ToolCall,
    Transcript,
    TriggerType,
)
from egg_contracts.redactor import Redactor, RedactorConfig
from egg_contracts.transcript_extractor import (
    TranscriptExtractError,
    extract_transcript_from_proxy_buffer,
    get_proxy_buffer_path,
)
from egg_contracts.usage_loader import (
    USAGE_TRACKING_ENABLED,
    UsageLoadError,
    UsageSaveError,
    update_usage_from_checkpoint,
)
from egg_logging import get_logger

try:
    from .git_client import (
        cleanup_credential_helper,
        create_credential_helper,
        get_authenticated_remote_target,
        get_token_for_repo,
        resolve_remote_url,
    )
    from .session_manager import Session
except ImportError:
    from git_client import (  # type: ignore[no-redef, import-untyped]
        cleanup_credential_helper,
        create_credential_helper,
        get_authenticated_remote_target,
        get_token_for_repo,
        resolve_remote_url,
    )
    from session_manager import Session  # type: ignore[no-redef, import-untyped]

logger = get_logger("gateway.checkpoint-handler")

# Checkpoint branch name — shared constant (also used by gateway.py for push bypass).
from egg_config.constants import CHECKPOINT_BRANCH

# Index file name
INDEX_FILE = "index.json"

# Maximum transcript size before truncation (bytes)
# Set to 3MB to capture more complete context from longer sessions,
# especially for session-end checkpoints that span entire agent sessions.
MAX_TRANSCRIPT_SIZE = 3_000_000  # 3MB

# Timeout for async session-end checkpoint capture (seconds).
# Buffer cleanup is blocked until capture completes or this timeout elapses.
SESSION_END_CAPTURE_TIMEOUT = 180

# Feature flag for enabling/disabling checkpoints
CHECKPOINT_ENABLED = os.environ.get("CHECKPOINT_ENABLED", "true").lower() == "true"

# Validation pattern for checkpoint_repo values (must be "owner/repo" format)
_REPO_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")


def _fetch_inter_agent_messages(
    pipeline_id: str | None,
    agent_role: str | None,
) -> list[InterAgentMessage]:
    """Fetch inter-agent messages from the orchestrator message bus.

    Returns messages sent and received by this agent during concurrent execution.
    Returns an empty list if the orchestrator is unreachable or concurrent mode
    is not active (no message endpoints available).
    """
    if not pipeline_id or not agent_role:
        return []

    orchestrator_url = os.environ.get("EGG_ORCHESTRATOR_URL", "http://egg-orchestrator:9849")
    concurrent_mode = os.environ.get("EGG_CONCURRENT_MODE", "false").lower() == "true"
    if not concurrent_mode:
        return []

    messages: list[InterAgentMessage] = []
    try:
        import json as json_mod
        import urllib.request

        # Fetch all messages involving this agent (sent, received, and broadcast).
        # The limit=1000 cap is intentionally high to capture the full message
        # history for a single pipeline phase — typical runs exchange <100 messages.
        url = (
            f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/messages"
            f"?role={agent_role}&limit=1000"
        )
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        # 5-second timeout: checkpoint capture is best-effort; if the orchestrator
        # is slow or unreachable, we skip messages rather than blocking the checkpoint.
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json_mod.loads(resp.read())
            for msg in data.get("data", {}).get("messages", []):
                direction = "received"
                if msg.get("from_role") == agent_role:
                    direction = "sent"
                messages.append(
                    InterAgentMessage(
                        id=msg.get("id", ""),
                        pipeline_id=pipeline_id,
                        from_role=msg.get("from_role", ""),
                        to_role=msg.get("to_role", "all"),
                        message_type=msg.get("message_type", ""),
                        subject=msg.get("subject", ""),
                        body=msg.get("body", ""),
                        timestamp=datetime.fromisoformat(msg["timestamp"])
                        if "timestamp" in msg
                        else datetime.now(UTC),
                        direction=direction,
                    )
                )
    except Exception as e:
        logger.debug(
            "Could not fetch inter-agent messages for checkpoint",
            error=str(e),
            pipeline_id=pipeline_id,
            agent_role=agent_role,
        )

    return messages


def _validate_checkpoint_repo(checkpoint_repo: str) -> str:
    """Validate that checkpoint_repo matches 'owner/repo' format.

    Raises:
        ValueError: If the format is invalid.
    """
    if not _REPO_PATTERN.match(checkpoint_repo):
        raise ValueError(
            f"Invalid checkpoint_repo format: {checkpoint_repo!r} (expected 'owner/repo')"
        )
    return checkpoint_repo


def _resolve_checkpoint_target(
    checkpoint_repo: str | None,
    remote: str,
    repo_path: str,
) -> str:
    """Resolve the git target for checkpoint fetch/push operations.

    If *checkpoint_repo* is given, validates it and returns an HTTPS URL.
    Otherwise resolves the remote URL and rewrites SSH to HTTPS so the
    gateway credential helper can authenticate.  See #1767.
    """
    if checkpoint_repo:
        _validate_checkpoint_repo(checkpoint_repo)
        target = f"https://github.com/{checkpoint_repo}.git"
        logger.info(
            "Using external checkpoint repo",
            checkpoint_repo=checkpoint_repo,
            target=target,
        )
        return target

    remote_url, err = resolve_remote_url(remote, repo_path)
    if err:
        logger.warning(
            "Failed to resolve remote URL, falling back to remote name",
            remote=remote,
            error=err,
        )
        return remote
    return get_authenticated_remote_target(remote, remote_url)


# Mapping from agent_role strings to AgentType enum values
_ROLE_TO_AGENT_TYPE = {
    "coder": AgentType.CODER,
    "tester": AgentType.TESTER,
    "documenter": AgentType.DOCUMENTER,
    "reviewer": AgentType.REVIEWER,
    "reviewer_code": AgentType.REVIEWER,
    "reviewer_contract": AgentType.REVIEWER,
    "reviewer_agent_design": AgentType.REVIEWER,
    "reviewer_refine": AgentType.REVIEWER,
    "reviewer_plan": AgentType.REVIEWER,
    "architect": AgentType.ARCHITECT,
    "task_planner": AgentType.TASK_PLANNER,
    "risk_analyst": AgentType.RISK_ANALYST,
    "refiner": AgentType.REFINER,
}


def _extract_repo_from_remote(repo_path: str) -> str | None:
    """Extract owner/repo from the git remote URL of a repository.

    Args:
        repo_path: Path to the git repository

    Returns:
        Repository in "owner/repo" format, or None if extraction fails.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        remote_url = result.stdout.strip()
        match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    except Exception:
        pass
    return None


def _resolve_agent_type(agent_role: str | None) -> AgentType:
    """Map an agent_role string to the AgentType enum."""
    if not agent_role:
        return AgentType.UNKNOWN
    return _ROLE_TO_AGENT_TYPE.get(agent_role.lower(), AgentType.UNKNOWN)


def _resolve_github_token(repo_path: str) -> str | None:
    """Resolve a fresh GitHub token for a repository.

    Extracts owner/repo from the git remote URL and calls get_token_for_repo()
    to obtain a fresh installation token. This avoids relying on stale tokens
    stored on the singleton CheckpointHandler.

    Args:
        repo_path: Path to the git repository

    Returns:
        A fresh GitHub token string, or None if unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.debug(
                "Could not get remote URL for token resolution",
                repo_path=repo_path,
            )
            return None

        remote_url = result.stdout.strip()
        match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url)
        if not match:
            logger.debug(
                "Could not extract owner/repo from remote URL",
                remote_url=remote_url,
            )
            return None

        repo = f"{match.group(1)}/{match.group(2)}"
        token_str, _auth_mode, error_msg = get_token_for_repo(repo)
        if not token_str:
            logger.warning(
                "Could not get token for repo — checkpoint may not be stored",
                repo=repo,
                error=error_msg,
            )
            return None
        return token_str
    except Exception as e:
        logger.warning(
            "GitHub token resolution failed — checkpoint may not be stored",
            error=str(e),
            repo_path=repo_path,
        )
        return None


class CheckpointError(Exception):
    """Error during checkpoint capture."""

    pass


class CheckpointHandler:
    """
    Handles checkpoint capture and storage for agent sessions.

    Supports two checkpoint types:
    - COMMIT: Created on git push (one checkpoint per push)
    - SESSION_END: Created when a session terminates
    """

    def __init__(
        self,
        github_token: str | None = None,
        redactor_config: RedactorConfig | None = None,
    ):
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
    ) -> CheckpointV2 | None:
        """
        Capture a v2 checkpoint for a commit (trigger_type=COMMIT).

        Args:
            repo_path: Path to the repository
            commit_sha: The commit SHA to checkpoint
            branch: The branch where the commit was made
            session: Optional Session object with container/role info
            issue_number: Optional GitHub issue number
            pipeline_phase: Optional SDLC pipeline phase
            push_sha: Optional tip commit SHA of the push

        Returns:
            CheckpointV2 object if successful, None if capture fails
        """
        if not CHECKPOINT_ENABLED:
            logger.debug("Checkpoint capture disabled by CHECKPOINT_ENABLED=false")
            return None

        try:
            container_id = session.container_id if session else None
            repo = self._resolve_repo(repo_path, session)

            if not container_id:
                logger.debug(
                    "No container ID available for checkpoint",
                    repo_path=repo_path,
                    commit_sha=commit_sha[:7],
                )
                return self._create_minimal_checkpoint_v2(
                    trigger_type=TriggerType.COMMIT,
                    commit_sha=commit_sha,
                    branch=branch,
                    session=session,
                    issue_number=issue_number,
                    pipeline_phase=pipeline_phase,
                    push_sha=push_sha,
                    repo=repo,
                )

            buffer_path = get_proxy_buffer_path(container_id)

            if not buffer_path.exists():
                logger.warning(
                    "Proxy buffer missing for active session — transcript data will be lost. "
                    "Buffer may not have been created yet or was cleaned up prematurely.",
                    container_id=container_id,
                    buffer_path=str(buffer_path),
                    commit_sha=commit_sha[:7],
                )
                return self._create_minimal_checkpoint_v2(
                    trigger_type=TriggerType.COMMIT,
                    commit_sha=commit_sha,
                    branch=branch,
                    session=session,
                    issue_number=issue_number,
                    pipeline_phase=pipeline_phase,
                    push_sha=push_sha,
                    repo=repo,
                )

            try:
                (
                    session_metadata,
                    transcript,
                    tool_calls,
                    file_operations,
                    token_usage,
                ) = extract_transcript_from_proxy_buffer(buffer_path, container_id)
            except TranscriptExtractError as e:
                logger.warning(
                    "Failed to extract transcript from proxy buffer",
                    error=str(e),
                    buffer_path=str(buffer_path),
                )
                return self._create_minimal_checkpoint_v2(
                    trigger_type=TriggerType.COMMIT,
                    commit_sha=commit_sha,
                    branch=branch,
                    session=session,
                    issue_number=issue_number,
                    pipeline_phase=pipeline_phase,
                    push_sha=push_sha,
                    repo=repo,
                )

            # Apply redaction
            transcript = self._redact_transcript(transcript)
            tool_calls = self._redact_tool_calls(tool_calls)

            # Merge session info
            if session:
                session_metadata.container_id = session.container_id
                session_metadata.agent_role = session.agent_role
                session_metadata.claude_code_version = session.claude_code_version

            # Resolve issue/PR/phase/pipeline from session or env
            issue_number = self._resolve_issue_number(issue_number, session)
            pipeline_phase = self._resolve_pipeline_phase(pipeline_phase, session)
            pr_number = self._resolve_pr_number(session)
            pipeline_id = self._resolve_pipeline_id(session)
            agent_type = _resolve_agent_type(session.agent_role if session else None)

            checkpoint_id = generate_checkpoint_id_from_commit(
                commit_sha, session_metadata.session_id
            )

            now = datetime.now(UTC)

            # Fetch inter-agent messages for concurrent execution mode
            agent_role = session.agent_role if session else None
            inter_agent_messages = _fetch_inter_agent_messages(pipeline_id, agent_role)

            checkpoint = CheckpointV2(
                id=checkpoint_id,
                trigger_type=TriggerType.COMMIT,
                commit_sha=commit_sha,
                push_sha=push_sha or commit_sha,
                branch=branch,
                session_id=session_metadata.session_id,
                issue_number=issue_number,
                pr_number=pr_number,
                agent_type=agent_type,
                pipeline_phase=pipeline_phase,
                pipeline_id=pipeline_id,
                repo=repo,
                session=session_metadata,
                transcript=transcript,
                files_touched=file_operations,
                tool_calls=tool_calls,
                token_usage=token_usage,
                inter_agent_messages=inter_agent_messages,
                created_at=now,
                session_started_at=session_metadata.started_at,
                session_ended_at=session_metadata.ended_at,
            )

            logger.info(
                "Checkpoint captured",
                checkpoint_id=checkpoint_id,
                trigger_type="commit",
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
            return None

    def capture_session_end_checkpoint(
        self,
        session: Session,
        session_status: SessionStatus,
        repo_path: str | None = None,
    ) -> CheckpointV2 | None:
        """
        Capture a checkpoint when a session terminates.

        Creates a checkpoint with trigger_type=SESSION_END regardless of whether
        the session pushed any commits. This ensures every session is captured.

        Args:
            session: The Session object being terminated
            session_status: Why the session ended (COMPLETED, EXPIRED, FAILED)
            repo_path: Optional repo path (for finding repo context)

        Returns:
            CheckpointV2 object if successful, None if capture fails
        """
        if not CHECKPOINT_ENABLED:
            return None

        container_id = session.container_id

        try:
            now = datetime.now(UTC)
            agent_type = _resolve_agent_type(session.agent_role)
            session_id = container_id

            # Try to extract transcript from proxy buffer
            transcript = None
            tool_calls: list[ToolCall] = []
            file_operations: list[FileOperation] = []
            token_usage = None
            session_metadata = SessionMetadata(
                session_id=session_id,
                container_id=container_id,
                agent_role=session.agent_role,
                claude_code_version=session.claude_code_version,
                started_at=session.created_at,
                ended_at=now,
                duration_seconds=(now - session.created_at).total_seconds(),
            )

            try:
                buffer_path = get_proxy_buffer_path(container_id)
                if not buffer_path.exists():
                    duration = (now - session.created_at).total_seconds()
                    if duration > 10:
                        logger.warning(
                            "Proxy buffer missing for session-end checkpoint — transcript data lost",
                            container_id=container_id,
                            duration_seconds=duration,
                            buffer_path=str(buffer_path),
                        )
                if buffer_path.exists():
                    (
                        extracted_metadata,
                        transcript,
                        tool_calls,
                        file_operations,
                        token_usage,
                    ) = extract_transcript_from_proxy_buffer(buffer_path, container_id)

                    # Merge extracted metadata with session info
                    extracted_metadata.container_id = container_id
                    extracted_metadata.agent_role = session.agent_role
                    extracted_metadata.claude_code_version = session.claude_code_version
                    session_metadata = extracted_metadata

                    # Apply redaction
                    transcript = self._redact_transcript(transcript)
                    tool_calls = self._redact_tool_calls(tool_calls)
            except TranscriptExtractError, ValueError:
                logger.debug(
                    "No transcript available for session-end checkpoint",
                    container_id=container_id,
                )

            # Mark transcript as truncated for crashed sessions
            if session_status == SessionStatus.FAILED and transcript is not None:
                transcript = transcript.model_copy(
                    update={
                        "truncated": True,
                        "truncation_reason": "container_crash",
                    }
                )

            checkpoint_id = generate_checkpoint_id_v2(session_id, now)

            pipeline_id = self._resolve_pipeline_id(session)
            repo = self._resolve_repo(repo_path, session)

            # Fetch inter-agent messages for concurrent execution mode
            inter_agent_messages = _fetch_inter_agent_messages(pipeline_id, session.agent_role)

            checkpoint = CheckpointV2(
                id=checkpoint_id,
                trigger_type=TriggerType.SESSION_END,
                session_status=session_status,
                commit_sha=session.auto_commit_sha,
                branch=session.last_branch,
                session_id=session_id,
                issue_number=session.issue_number,
                pr_number=session.pr_number,
                agent_type=agent_type,
                pipeline_phase=session.phase,
                pipeline_id=pipeline_id,
                repo=repo,
                session=session_metadata,
                transcript=transcript,
                files_touched=file_operations,
                tool_calls=tool_calls,
                token_usage=token_usage,
                inter_agent_messages=inter_agent_messages,
                created_at=now,
                session_started_at=session.created_at,
                session_ended_at=now,
            )

            logger.info(
                "Session-end checkpoint captured",
                checkpoint_id=checkpoint_id,
                container_id=container_id,
                session_status=session_status.value,
                agent_type=agent_type.value,
                message_count=transcript.message_count if transcript else 0,
            )

            return checkpoint

        except Exception as e:
            logger.error(
                "Session-end checkpoint capture failed",
                error=str(e),
                container_id=container_id,
                session_status=session_status.value,
            )
            return None

    def _create_minimal_checkpoint_v2(
        self,
        trigger_type: TriggerType,
        commit_sha: str | None = None,
        branch: str | None = None,
        session: Session | None = None,
        issue_number: int | None = None,
        pipeline_phase: str | None = None,
        push_sha: str | None = None,
        session_status: SessionStatus | None = None,
        repo: str | None = None,
    ) -> CheckpointV2:
        """Create a minimal v2 checkpoint without transcript data."""
        session_id = session.container_id if session else "unknown"
        now = datetime.now(UTC)

        if trigger_type == TriggerType.COMMIT and commit_sha:
            checkpoint_id = generate_checkpoint_id_from_commit(commit_sha, session_id)
        else:
            checkpoint_id = generate_checkpoint_id_v2(session_id, now)

        session_metadata = SessionMetadata(
            session_id=session_id,
            container_id=session.container_id if session else None,
            agent_role=session.agent_role if session else None,
            claude_code_version=session.claude_code_version if session else None,
            started_at=session.created_at if session else now,
        )

        agent_type = _resolve_agent_type(session.agent_role if session else None)
        pipeline_id = self._resolve_pipeline_id(session)

        return CheckpointV2(
            id=checkpoint_id,
            trigger_type=trigger_type,
            session_status=session_status,
            commit_sha=commit_sha,
            push_sha=push_sha or commit_sha,
            branch=branch,
            session_id=session_id,
            issue_number=issue_number,
            pr_number=session.pr_number if session else None,
            agent_type=agent_type,
            pipeline_phase=pipeline_phase,
            pipeline_id=pipeline_id,
            repo=repo,
            session=session_metadata,
            created_at=now,
            session_started_at=session.created_at if session else now,
        )

    def _resolve_repo(self, repo_path: str | None, session: Session | None) -> str | None:
        """Resolve source repository in owner/repo format.

        Resolution order:
        1. Extract from git remote URL of repo_path
        2. Fall back to session.last_repo_path (if available)
        """
        paths_to_try = []
        if repo_path:
            paths_to_try.append(repo_path)
        if session and session.last_repo_path and session.last_repo_path != repo_path:
            paths_to_try.append(session.last_repo_path)

        for path in paths_to_try:
            repo = _extract_repo_from_remote(path)
            if repo:
                return repo

        return None

    def _resolve_issue_number(
        self, issue_number: int | None, session: Session | None
    ) -> int | None:
        """Resolve issue number from args, session, or environment."""
        if issue_number is not None:
            return issue_number
        if session and session.issue_number is not None:
            return session.issue_number
        issue_str = os.environ.get("EGG_ISSUE_NUMBER")
        if issue_str:
            try:
                return int(issue_str)
            except ValueError:
                pass
        return None

    def _resolve_pipeline_phase(
        self, pipeline_phase: str | None, session: Session | None
    ) -> str | None:
        """Resolve pipeline phase from args, session, or environment."""
        if pipeline_phase is not None:
            return pipeline_phase
        if session and session.phase is not None:
            return session.phase
        return os.environ.get("EGG_PIPELINE_PHASE")

    def _resolve_pipeline_id(self, session: Session | None) -> str | None:
        """Resolve pipeline ID from session or environment."""
        if session and session.pipeline_id is not None:
            return session.pipeline_id
        return os.environ.get("EGG_PIPELINE_ID") or None

    def _resolve_pr_number(self, session: Session | None) -> int | None:
        """Resolve PR number from session or environment."""
        if session and session.pr_number is not None:
            return session.pr_number
        pr_str = os.environ.get("EGG_PR_NUMBER")
        if pr_str:
            try:
                return int(pr_str)
            except ValueError:
                pass
        return None

    def _redact_transcript(self, transcript: Transcript) -> Transcript:
        """Redact sensitive data from transcript messages."""
        redacted_messages = []
        for msg in transcript.messages:
            redacted_content = None
            if msg.content:
                redacted_content = self._redactor.redact_text(msg.content)

            redacted_messages.append(msg.model_copy(update={"content": redacted_content}))

        return transcript.model_copy(update={"messages": redacted_messages})

    def _redact_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        """Redact sensitive data from tool calls."""
        redacted_calls = []
        for tc in tool_calls:
            redacted_params = self._redactor.redact_dict(tc.parameters)

            redacted_result = None
            if tc.result_summary:
                redacted_result = self._redactor.redact_text(tc.result_summary)

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

    def store_checkpoint_v2(
        self,
        checkpoint: CheckpointV2,
        repo_path: str,
        remote: str = "origin",
        checkpoint_repo: str | None = None,
        github_token: str | None = None,
    ) -> bool:
        """
        Store a v2 checkpoint in the checkpoint branch.

        Writes the checkpoint JSON and updates the v2 multi-dimensional index
        on the egg/checkpoints/v2 branch.

        Args:
            checkpoint: The v2 checkpoint to store
            repo_path: Path to the repository
            remote: Git remote name
            checkpoint_repo: Optional "owner/repo" for a separate checkpoint
                destination. When set, checkpoints are pushed to this repo
                instead of the source repo's remote. Useful for keeping
                checkpoint data (transcripts, tool calls) private.
            github_token: Optional fresh GitHub token for git operations.
                When provided, used instead of self._github_token.

        Returns:
            True if successful, False otherwise
        """
        if not CHECKPOINT_ENABLED:
            return False

        target = _resolve_checkpoint_target(checkpoint_repo, remote, repo_path)
        repo_lock = _get_repo_lock(repo_path)

        try:
            with (
                repo_lock,
                tempfile.TemporaryDirectory(
                    prefix="checkpoint_", ignore_cleanup_errors=True
                ) as temp_dir,
            ):
                temp_path = Path(temp_dir)

                branch_exists = self._branch_exists(
                    repo_path, target, CHECKPOINT_BRANCH, github_token=github_token
                )

                # Wrap the worktree-add and the checkpoint work so that
                # `worktree remove` and `worktree prune` always run, even
                # if `worktree add` itself raises (which is exactly the
                # failure mode #2069 reports). Without this, a partially-
                # created `.git/worktrees/<basename>` entry would persist
                # until the next successful store's cleanup pruned it.
                try:
                    if branch_exists:
                        # Force-update the local branch to match the remote.
                        # The + prefix handles the case where the local branch
                        # has diverged (e.g., from a different remote or
                        # concurrent checkpoint pushes).
                        # Retry with backoff: network fetches can be slow or
                        # transiently fail, especially during container shutdown.
                        fetch_args = [
                            "fetch",
                            target,
                            f"+{CHECKPOINT_BRANCH}:{CHECKPOINT_BRANCH}",
                        ]
                        max_fetch_attempts = 3
                        fetch_timeout = 45
                        for attempt in range(1, max_fetch_attempts + 1):
                            try:
                                self._run_git(
                                    repo_path,
                                    fetch_args,
                                    timeout=fetch_timeout,
                                    github_token=github_token,
                                )
                                break
                            except (
                                subprocess.TimeoutExpired,
                                CheckpointError,
                            ) as exc:
                                if attempt < max_fetch_attempts:
                                    backoff = 2**attempt
                                    logger.warning(
                                        "Checkpoint fetch failed, retrying",
                                        attempt=attempt,
                                        max_attempts=max_fetch_attempts,
                                        backoff_seconds=backoff,
                                        checkpoint_id=checkpoint.id,
                                        error=str(exc),
                                    )
                                    time.sleep(backoff)
                                else:
                                    logger.error(
                                        "Checkpoint fetch failed after all retries",
                                        attempts=max_fetch_attempts,
                                        checkpoint_id=checkpoint.id,
                                        error=str(exc),
                                    )
                                    raise
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
                        # Delete any stale local branch before creating orphan.
                        # The branch may exist locally from a different remote
                        # (e.g., checkpoints were previously stored in the source
                        # repo before migrating to an external checkpoint repo).
                        self._run_git(
                            repo_path,
                            ["branch", "-D", CHECKPOINT_BRANCH],
                            check=False,
                        )
                        self._run_git(
                            repo_path,
                            ["worktree", "add", "--detach", str(temp_path)],
                        )
                        self._run_git(
                            str(temp_path),
                            ["checkout", "--orphan", CHECKPOINT_BRANCH],
                        )
                        self._run_git(str(temp_path), ["rm", "-rf", "."], check=False)

                    checkpoint_path = get_checkpoint_path(temp_path / "checkpoints", checkpoint.id)

                    save_checkpoint_v2(checkpoint, checkpoint_path)

                    index_path = temp_path / INDEX_FILE
                    add_checkpoint_to_index_v2(checkpoint, index_path)

                    self._run_git(
                        str(temp_path),
                        ["add", str(checkpoint_path.relative_to(temp_path)), INDEX_FILE],
                    )

                    # Build a descriptive commit message
                    if checkpoint.trigger_type == TriggerType.COMMIT:
                        commit_desc = (
                            f"commit {checkpoint.commit_sha[:7]}"
                            if checkpoint.commit_sha
                            else "unknown commit"
                        )
                    else:
                        status = (
                            checkpoint.session_status.value
                            if checkpoint.session_status
                            else "unknown"
                        )
                        commit_desc = f"session-end ({status})"

                    commit_msg = f"Add checkpoint {checkpoint.id} for {commit_desc}"
                    self._run_git(
                        str(temp_path),
                        ["commit", "--no-verify", "-m", commit_msg],
                    )

                    # Retry push with pull-rebase on non-fast-forward rejection.
                    # Multiple checkpoint stores can race: they fetch the same base,
                    # commit locally, then the second push fails because the first
                    # already advanced the remote branch.
                    max_push_attempts = 3
                    for push_attempt in range(1, max_push_attempts + 1):
                        try:
                            self._run_git(
                                str(temp_path),
                                ["push", target, f"HEAD:{CHECKPOINT_BRANCH}"],
                                timeout=120,
                                github_token=github_token,
                            )
                            break
                        except CheckpointError as push_err:
                            if "non-fast-forward" not in str(push_err):
                                raise
                            if push_attempt >= max_push_attempts:
                                raise
                            logger.warning(
                                "Checkpoint push rejected (non-fast-forward), rebasing and retrying",
                                attempt=push_attempt,
                                max_attempts=max_push_attempts,
                                checkpoint_id=checkpoint.id,
                            )
                            time.sleep(1)
                            # Pull remote changes and rebase our commit on top
                            self._run_git(
                                str(temp_path),
                                ["fetch", target, f"+{CHECKPOINT_BRANCH}:{CHECKPOINT_BRANCH}"],
                                timeout=60,
                                github_token=github_token,
                            )
                            self._run_git(
                                str(temp_path),
                                ["rebase", CHECKPOINT_BRANCH],
                            )

                    logger.info(
                        "Checkpoint stored",
                        checkpoint_id=checkpoint.id,
                        branch=CHECKPOINT_BRANCH,
                        trigger_type=checkpoint.trigger_type.value,
                        checkpoint_repo=checkpoint_repo or "(same repo)",
                        push_attempts=push_attempt,
                    )

                    # Update usage aggregates (graceful degradation on failure)
                    if USAGE_TRACKING_ENABLED:
                        try:
                            update_usage_from_checkpoint(temp_path, checkpoint)
                            logger.debug(
                                "Usage aggregates updated",
                                checkpoint_id=checkpoint.id,
                            )
                        except (UsageLoadError, UsageSaveError) as e:
                            logger.warning(
                                "Failed to update usage aggregates",
                                error=str(e),
                                checkpoint_id=checkpoint.id,
                            )

                    return True

                finally:
                    self._run_git(
                        repo_path,
                        ["worktree", "remove", "--force", str(temp_path)],
                        check=False,
                    )
                    # Drop any stale .git/worktrees/<basename> entry so a
                    # failed remove (or a failed worktree add) doesn't
                    # leak across attempts.
                    #
                    # IMPORTANT: surgically remove this worktree's admin
                    # dir; do NOT call ``git worktree prune``.  The
                    # gateway pod cannot see other pods' worktree paths
                    # (orchestrator's state worktree lives on a per-pod
                    # ``emptyDir`` mount while the bare repo is shared
                    # via ``hostPath``), so prune treats those entries
                    # as ``prunable`` and removes their admin dirs —
                    # taking down the orchestrator's state-store
                    # operations until they self-heal (#2324).  Same
                    # rationale documented at
                    # ``worktree_manager.py``'s manual-cleanup branch.
                    #
                    # Building the admin-dir path directly from
                    # ``temp_path.name`` (rather than verifying the
                    # admin dir's ``gitdir`` pointer like
                    # ``worktree_manager._find_worktree_git_dir`` does)
                    # is safe here because ``temp_path`` comes from
                    # ``tempfile.TemporaryDirectory(prefix="checkpoint_")``
                    # — the basename is randomly generated and cannot
                    # collide with another worktree's admin dir.  Do
                    # NOT copy this shortcut into a context where the
                    # worktree basename is predictable (e.g.,
                    # container worktrees keyed by repo name).
                    admin_dir = Path(repo_path) / ".git" / "worktrees" / temp_path.name
                    shutil.rmtree(admin_dir, ignore_errors=True)

        except Exception as e:
            logger.error(
                "Failed to store checkpoint",
                error=str(e),
                checkpoint_id=checkpoint.id,
                checkpoint_repo=checkpoint_repo,
            )
            return False

    # Keep store_checkpoint as alias for backward compatibility
    store_checkpoint = store_checkpoint_v2

    def fetch_and_read_index(
        self,
        repo_path: str,
        checkpoint_repo: str | None = None,
        github_token: str | None = None,
    ) -> CheckpointIndexV2 | None:
        """Fetch the checkpoint branch and read the index.

        Fetches from the checkpoint repo (external or origin), then reads
        index.json via git show. Uses authenticated fetch when a token
        is available.

        Args:
            repo_path: Path to a local git repository (used as fetch cwd).
            checkpoint_repo: External checkpoint repo ("owner/repo"), or
                None to use origin.
            github_token: Optional GitHub token; resolved automatically if
                not provided.

        Returns:
            The parsed checkpoint index, or None if unavailable.
        """
        if github_token is None:
            github_token = _resolve_github_token(repo_path)

        target = _resolve_checkpoint_target(checkpoint_repo, "origin", repo_path)

        if not self._branch_exists(repo_path, target, CHECKPOINT_BRANCH, github_token=github_token):
            return None

        # Fetch the branch
        self._run_git(
            repo_path,
            ["fetch", target, CHECKPOINT_BRANCH],
            check=False,
            github_token=github_token,
        )

        # Resolve a stable ref
        if checkpoint_repo:
            result = self._run_git(repo_path, ["rev-parse", "FETCH_HEAD"], check=False)
            if result.returncode != 0:
                return None
            ref = result.stdout.strip()
        else:
            ref = f"origin/{CHECKPOINT_BRANCH}"

        content = self._read_git_file(repo_path, ref, INDEX_FILE)
        if content is None:
            return None

        try:
            import json as json_mod

            data = json_mod.loads(content)
            return CheckpointIndexV2.model_validate(data)
        except Exception:
            return None

    def read_checkpoint(
        self,
        repo_path: str,
        checkpoint_id: str,
        ref: str,
    ) -> CheckpointV2 | None:
        """Read a full checkpoint from a git ref.

        Args:
            repo_path: Path to the local git repository.
            checkpoint_id: Checkpoint ID (ckpt-...).
            ref: Git ref to read from (SHA or origin/branch).

        Returns:
            Parsed CheckpointV2, or None if not found.
        """
        rel_path = get_checkpoint_path(Path("checkpoints"), checkpoint_id)
        content = self._read_git_file(repo_path, ref, str(rel_path))
        if content is None:
            return None

        try:
            import json as json_mod

            data = json_mod.loads(content)
            return CheckpointV2.model_validate(data)
        except Exception:
            return None

    def ensure_ref(
        self,
        repo_path: str,
        checkpoint_repo: str | None = None,
        github_token: str | None = None,
    ) -> str | None:
        """Fetch the checkpoint branch and return a stable git ref.

        Args:
            repo_path: Path to a local git repository.
            checkpoint_repo: External checkpoint repo ("owner/repo").
            github_token: Optional GitHub token.

        Returns:
            A git ref string, or None if the branch doesn't exist.
        """
        if github_token is None:
            github_token = _resolve_github_token(repo_path)

        target = _resolve_checkpoint_target(checkpoint_repo, "origin", repo_path)

        if not self._branch_exists(repo_path, target, CHECKPOINT_BRANCH, github_token=github_token):
            return None

        self._run_git(
            repo_path,
            ["fetch", target, CHECKPOINT_BRANCH],
            check=False,
            github_token=github_token,
        )

        if checkpoint_repo:
            result = self._run_git(repo_path, ["rev-parse", "FETCH_HEAD"], check=False)
            if result.returncode != 0:
                return None
            return result.stdout.strip()
        return f"origin/{CHECKPOINT_BRANCH}"

    def _read_git_file(
        self,
        repo_path: str,
        ref: str,
        path: str,
    ) -> str | None:
        """Read a file from a git ref via git show.

        Args:
            repo_path: Path to the git repository.
            ref: Git ref (SHA or branch name).
            path: Path relative to the ref root.

        Returns:
            File contents, or None if not found.
        """
        result = self._run_git(
            repo_path,
            ["show", f"{ref}:{path}"],
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    def _branch_exists(
        self,
        repo_path: str,
        remote: str,
        branch: str,
        github_token: str | None = None,
    ) -> bool:
        """Check if a branch exists on the remote."""
        try:
            result = self._run_git(
                repo_path,
                ["ls-remote", "--heads", remote, branch],
                check=False,
                timeout=30,
                github_token=github_token,
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
        github_token: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command.

        Args:
            cwd: Working directory for the command
            args: Git command arguments (without 'git' prefix)
            check: If True, raise on non-zero exit code
            timeout: Command timeout in seconds
            github_token: Optional token to use instead of self._github_token
        """
        env = os.environ.copy()
        credential_helper_path = None
        token = github_token or self._github_token

        try:
            if token:
                credential_helper_path, env = create_credential_helper(token, env)

            # SECURITY: Disable all git hooks. The checkpoint handler runs git commands
            # internally for bookkeeping (storing checkpoints to the checkpoint branch).
            # Hooks from user repos must not execute in the gateway's trusted environment.
            cmd = ["git", "-c", "core.hooksPath=/dev/null"] + args

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
        finally:
            cleanup_credential_helper(credential_helper_path)


# Global checkpoint handler instance
_checkpoint_handler: CheckpointHandler | None = None
_handler_lock = threading.Lock()

# Per-repo_path locks serializing concurrent checkpoint stores.
# Concurrent threads operating on the same source repo race on
# .git/worktrees state and ref locks under repo_path/.git/, producing
# 'worktree add' failures and stale worktree directories. The lock is
# keyed by repo_path only — the target (origin vs. external checkpoint
# repo) does not affect the local .git state being contended.
# Not trimmed: the set of distinct repo_paths in a gateway process is
# small and stable in practice (one entry per source repo seen).
# See #2069.
_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def _get_repo_lock(repo_path: str) -> threading.Lock:
    with _repo_locks_guard:
        lock = _repo_locks.get(repo_path)
        if lock is None:
            lock = threading.Lock()
            _repo_locks[repo_path] = lock
        return lock


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
    checkpoint_repo: str | None = None,
) -> CheckpointV2 | None:
    """
    Capture and store a v2 checkpoint for a commit.

    This is the main entry point for checkpoint capture from gateway.py.
    By default, storage is done asynchronously to not block the push response.

    Args:
        checkpoint_repo: Optional "owner/repo" for a separate checkpoint
            destination. When set, checkpoints are pushed to this repo
            instead of the source repo.
    """
    if not CHECKPOINT_ENABLED:
        return None

    handler = get_checkpoint_handler(github_token)

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

    if async_store:

        def _store_with_error_handling() -> None:
            try:
                handler.store_checkpoint_v2(
                    checkpoint,
                    repo_path,
                    checkpoint_repo=checkpoint_repo,
                    github_token=github_token,
                )
            except Exception as e:
                logger.error(
                    "Async checkpoint storage failed",
                    error=str(e),
                    checkpoint_id=checkpoint.id,
                    commit_sha=commit_sha[:7],
                )

        # Push-triggered checkpoints are best-effort fire-and-forget.
        # Use daemon=True so they don't block process shutdown.
        thread = threading.Thread(
            target=_store_with_error_handling,
            daemon=True,
        )
        thread.start()
    else:
        handler.store_checkpoint_v2(
            checkpoint,
            repo_path,
            checkpoint_repo=checkpoint_repo,
            github_token=github_token,
        )

    return checkpoint


def capture_and_store_checkpoints_for_push(
    repo_path: str,
    new_sha: str,
    branch: str,
    session: Session | None = None,
    issue_number: int | None = None,
    pipeline_phase: str | None = None,
    github_token: str | None = None,
    async_store: bool = True,
    checkpoint_repo: str | None = None,
) -> list[CheckpointV2]:
    """
    Capture and store a v2 checkpoint for a push.

    Creates exactly one checkpoint per push for the tip commit (new_sha) with
    trigger_type=COMMIT. When an agent batches multiple commits into a single
    push, only one checkpoint is created — the transcript buffer reflects the
    state at push time, not the state at each individual commit, so creating
    per-commit checkpoints would just produce identical duplicates.

    Args:
        checkpoint_repo: Optional "owner/repo" for a separate checkpoint
            destination. When set, checkpoints are pushed to this repo
            instead of the source repo.
    """
    if not CHECKPOINT_ENABLED:
        return []

    logger.info(
        "Creating checkpoint for push",
        push_sha=new_sha[:7],
        branch=branch,
    )

    checkpoint = capture_and_store_checkpoint(
        repo_path=repo_path,
        commit_sha=new_sha,
        branch=branch,
        session=session,
        issue_number=issue_number,
        pipeline_phase=pipeline_phase,
        push_sha=new_sha,
        github_token=github_token,
        async_store=async_store,
        checkpoint_repo=checkpoint_repo,
    )

    if checkpoint is None:
        logger.debug("No checkpoint captured for push", push_sha=new_sha[:7])
        return []

    return [checkpoint]


def _get_checkpoint_repo_for_path(repo_path: str) -> str | None:
    """Determine the checkpoint_repo config for a given repo path.

    Extracts the owner/repo from the git remote URL and looks up the
    checkpoint_repo setting in repo_config.

    Args:
        repo_path: Path to the git repository

    Returns:
        Checkpoint repo in "owner/repo" format, or None.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.debug(
                "Could not get remote URL for checkpoint repo auto-detection",
                repo_path=repo_path,
                returncode=result.returncode,
                stderr=result.stderr.strip() if result.stderr else "",
            )
            return None

        remote_url = result.stdout.strip()

        # Import here to avoid circular imports at module level
        try:
            from config.repo_config import get_checkpoint_repo

            # Extract owner/repo from URL
            match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url)
            if match:
                repo = f"{match.group(1)}/{match.group(2)}"
                return get_checkpoint_repo(repo)
            else:
                logger.debug(
                    "Could not extract owner/repo from remote URL",
                    remote_url=remote_url,
                )
        except (ImportError, FileNotFoundError) as e:
            logger.debug(
                "Config not available for checkpoint repo auto-detection",
                error=str(e),
            )
    except Exception as e:
        logger.debug(
            "Checkpoint repo auto-detection failed",
            error=str(e),
            repo_path=repo_path,
        )
    return None


def capture_session_end_checkpoint(
    session: Session,
    session_status: SessionStatus,
    repo_path: str | None = None,
    github_token: str | None = None,
    async_store: bool = True,
    checkpoint_repo: str | None = None,
) -> tuple[CheckpointV2 | None, threading.Event | None]:
    """
    Capture and store a session-end checkpoint.

    Returns a (checkpoint, completion_event) tuple. The completion_event
    is set when async storage finishes (or immediately if async_store=False).
    Callers should wait on the event before cleaning up the transcript buffer.

    Args:
        session: The Session being terminated
        session_status: Terminal status (COMPLETED, EXPIRED, FAILED)
        repo_path: Optional repo path for context
        github_token: Optional GitHub token for pushing
        async_store: If True, store asynchronously (default)
        checkpoint_repo: Optional "owner/repo" for a separate checkpoint
            destination. If not provided and repo_path is available, will
            attempt to look up the config from repo settings.

    Returns:
        Tuple of (checkpoint, completion_event). completion_event is None
        if async_store is False or checkpoint capture failed.
    """
    if not CHECKPOINT_ENABLED:
        return None, None

    handler = get_checkpoint_handler(github_token)

    checkpoint = handler.capture_session_end_checkpoint(
        session=session,
        session_status=session_status,
        repo_path=repo_path,
    )

    if not checkpoint:
        return None, None

    # Determine repo_path for storage
    if repo_path is None:
        # Try to find a repo path from known locations
        repos_base = Path("/home/egg/repos")
        if repos_base.exists():
            for repo_dir in repos_base.iterdir():
                if repo_dir.is_dir() and (repo_dir / ".git").exists():
                    repo_path = str(repo_dir)
                    break

    if repo_path is None:
        logger.warning(
            "No repo path available for session-end checkpoint storage",
            container_id=session.container_id,
        )
        return checkpoint, None

    # Auto-detect checkpoint_repo from repo config if not explicitly provided
    if checkpoint_repo is None:
        checkpoint_repo = _get_checkpoint_repo_for_path(repo_path)

    # Resolve a fresh GitHub token if none was provided. Session-end
    # checkpoints are often captured long after the handler singleton was
    # created, so the token stored on self._github_token may be stale
    # (GitHub App installation tokens expire after 1 hour).
    if github_token is None:
        github_token = _resolve_github_token(repo_path)

    if async_store:
        completion_event = threading.Event()

        def _store_with_event() -> None:
            try:
                handler.store_checkpoint_v2(
                    checkpoint,
                    repo_path,
                    checkpoint_repo=checkpoint_repo,
                    github_token=github_token,
                )
            except Exception as e:
                logger.error(
                    "Async session-end checkpoint storage failed",
                    error=str(e),
                    checkpoint_id=checkpoint.id,
                    container_id=session.container_id,
                )
            finally:
                completion_event.set()

        thread = threading.Thread(
            target=_store_with_event,
            daemon=False,
        )
        thread.start()
        return checkpoint, completion_event
    else:
        handler.store_checkpoint_v2(
            checkpoint,
            repo_path,
            checkpoint_repo=checkpoint_repo,
            github_token=github_token,
        )
        return checkpoint, None
