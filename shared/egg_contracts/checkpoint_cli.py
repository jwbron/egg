#!/usr/bin/env python3
"""
Checkpoint CLI for browsing and querying agent checkpoints.

This CLI provides commands to list, show, and browse checkpoints that capture
agent session context. Supports both commit-triggered and session-end checkpoints
with rich multi-dimensional querying.

Checkpoints are stored in the egg/checkpoints/v2 branch and are captured
per-commit during git push operations and at session end. Transcript data is
extracted from the API proxy buffer, providing stable and format-independent
capture.

Reads checkpoint data via `git show` so it works both inside the egg container
(through the gateway sidecar) and outside with direct git access — no worktrees
required.

Note: Reviewer agents may not produce checkpoints if session-end triggers don't
fire (e.g., container eviction, OOM, or reviewer sessions that complete before
the capture hook runs). Use ``--agent-type reviewer`` results as a lower bound.

Commands:
    egg-checkpoint list [--branch <branch>] [--issue <number>] [--limit <n>]
                        [--trigger <type>] [--status <status>] [--agent-type <type>]
                        [--session <id>] [--pr <number>] [--phase <phase>]
                                            List checkpoints with metadata
    egg-checkpoint show <id-or-commit>      Display full checkpoint details
    egg-checkpoint browse --issue <number>  Filter checkpoints by issue
    egg-checkpoint search --text <text>     Search checkpoint transcripts
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Checkpoint branch name — shared constant to avoid divergence.
# Explicit re-export (as-alias) so mypy treats it as a public API.
from egg_config.constants import CHECKPOINT_BRANCH as CHECKPOINT_BRANCH

from .checkpoint_loader import (
    filter_checkpoints_v2,
    get_checkpoint_path,
)
from .checkpoints import (
    AgentType,
    CheckpointIndexV2,
    CheckpointSummaryV2,
    CheckpointV2,
    SessionStatus,
    TriggerType,
)

# Hint shown when checkpoint repo could not be resolved
_CHECKPOINT_REPO_HINT = (
    "Hint: If checkpoints are in a separate repo, use --checkpoint-repo OWNER/REPO "
    "or set EGG_CHECKPOINT_REPO"
)


# Validation pattern for checkpoint_repo values (must be "owner/repo" format)
_REPO_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")

# Composite BRC reviewer role names that map to AgentType.REVIEWER in the index
# but carry a more specific agent_role in SessionMetadata.
COMPOSITE_REVIEWER_ROLES: frozenset[str] = frozenset(
    {
        "reviewer_code",
        "reviewer_code_holistic",
        "reviewer_contract",
        "reviewer_agent_design",
        "reviewer_refine",
        "reviewer_plan",
    }
)

# All valid --agent-type choices: base AgentType values + composite reviewer names
_AGENT_TYPE_CHOICES: list[str] = sorted({a.value for a in AgentType} | COMPOSITE_REVIEWER_ROLES)


def _print_empty_result(
    checkpoint_repo: str | None,
    branch: str,
    json_mode: bool,
    message: str = "No checkpoints found matching filters",
    shape: Literal["list", "cost"] = "list",
    hint: bool = True,
) -> None:
    """Print a standardised empty-result message.

    Args:
        checkpoint_repo: The checkpoint repo that was searched (or None).
        branch: The checkpoint branch that was searched.
        json_mode: Whether ``--json`` was passed.
        message: Human-readable description printed to stderr.
        shape: ``"list"`` emits ``[]`` to stdout when *json_mode* is True;
               ``"cost"`` emits a structured empty cost object.
        hint: Whether to print the checkpoint-repo hint.
    """
    repo_label = checkpoint_repo or "(local)"
    print(f"Searched {repo_label} branch {branch}", file=sys.stderr)
    if not json_mode:
        print(message, file=sys.stderr)
    if hint:
        _print_repo_hint(checkpoint_repo)
    if json_mode:
        if shape == "cost":
            empty: dict[str, Any] = {
                "pipeline_id": None,
                "issue_number": None,
                "pr_number": None,
                "checkpoint_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0.0,
                "breakdown": [],
            }
            print(json.dumps(empty, indent=2))
        else:
            print("[]")


def _get_gateway_url() -> str | None:
    """Return the gateway URL if HTTP mode should be used.

    HTTP mode is enabled when GATEWAY_URL and EGG_SESSION_TOKEN are both
    set, allowing the CLI to query checkpoint data through the gateway API
    instead of direct git operations.
    """
    url = os.environ.get("GATEWAY_URL")
    token = os.environ.get("EGG_SESSION_TOKEN")
    if url and token:
        return url
    return None


def _http_get(base_url: str, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make an authenticated GET request to the gateway API.

    Uses urllib to avoid external dependencies. Authenticates with
    the session token from EGG_SESSION_TOKEN.

    Args:
        base_url: Gateway base URL (e.g. http://egg-gateway:<port>)
        endpoint: API path (e.g. /api/v1/checkpoints)
        params: Optional query parameters

    Returns:
        Parsed JSON response dict

    Raises:
        RuntimeError: On HTTP errors or connection failures
    """
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    url = f"{base_url}{endpoint}"
    if params:
        filtered = {k: str(v) for k, v in params.items() if v is not None}
        if filtered:
            url = f"{url}?{urlencode(filtered)}"

    session_token = os.environ.get("EGG_SESSION_TOKEN", "")

    try:
        req = Request(url, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {session_token}")
        with urlopen(req, timeout=120) as response:
            result: dict[str, Any] = json.loads(response.read().decode())
            return result
    except HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            msg = body.get("message", str(e))
        except Exception:
            msg = str(e)
        raise RuntimeError(f"Gateway request failed: {msg}") from e
    except URLError as e:
        raise RuntimeError(f"Cannot connect to gateway at {base_url}: {e.reason}") from e


def _validate_checkpoint_repo(checkpoint_repo: str) -> str:
    """Validate that checkpoint_repo matches 'owner/repo' format."""
    if not _REPO_PATTERN.match(checkpoint_repo):
        raise ValueError(
            f"Invalid checkpoint_repo format: {checkpoint_repo!r} (expected 'owner/repo')"
        )
    return checkpoint_repo


def _resolve_git_repo(path: str) -> str:
    """Resolve *path* to an actual git repository root.

    If *path* already contains a ``.git`` entry it is returned as-is.
    Otherwise the function walks up from ``cwd`` to find the nearest
    git root (handles the common case where ``EGG_REPO_PATH`` is the
    parent ``~/repos`` while ``cwd`` is inside an actual repo like
    ``~/repos/egg``).

    When no git root can be found, *path* is returned unchanged so
    callers always get a usable value.
    """
    if (Path(path) / ".git").exists():
        return path

    # Walk up from cwd looking for a .git entry
    current = Path.cwd()
    while current != current.parent:
        if (current / ".git").exists():
            return str(current)
        current = current.parent

    return path


def get_repo_path() -> str:
    """Get the repository path from environment or default.

    Resolves to the git toplevel directory when possible.  This handles
    the common sandbox case where ``EGG_REPO_PATH`` points to a parent
    directory (e.g. ``~/repos``) that contains one or more git repos
    rather than being a git repo itself.
    """
    candidate = os.environ.get("EGG_REPO_PATH", str(Path.cwd()))
    return _resolve_git_repo(candidate)


def run_git(
    args: list[str], cwd: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    # SECURITY: Disable all git hooks. This CLI may run git commands in
    # user repositories. Hooks must not execute in this trusted context.
    # See issue #58 for context on hook-based attacks.
    cmd = ["git", "-c", "core.hooksPath=/dev/null"] + args
    result = subprocess.run(
        cmd,
        cwd=cwd or get_repo_path(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Git command failed: {result.stderr}")
    return result


def _resolve_checkpoint_target(repo_path: str, checkpoint_repo: str | None = None) -> str:
    """Resolve the fetch/ls-remote target for the checkpoint branch.

    Args:
        repo_path: Path to the repository.
        checkpoint_repo: Optional "owner/repo" for an external checkpoint repo.

    Returns:
        A git remote name or HTTPS URL to use for fetching checkpoints.
    """
    if checkpoint_repo:
        _validate_checkpoint_repo(checkpoint_repo)
        return f"https://github.com/{checkpoint_repo}.git"
    return "origin"


def ensure_checkpoint_ref(repo_path: str, checkpoint_repo: str | None = None) -> str | None:
    """
    Ensure the checkpoint branch is fetched and return the git ref to read from.

    Args:
        repo_path: Path to the repository.
        checkpoint_repo: Optional "owner/repo" for an external checkpoint repo.

    Returns:
        A git ref string (e.g. "origin/egg/checkpoints/v2" or a resolved SHA),
        or None if the branch doesn't exist or access was denied.
    """
    target = _resolve_checkpoint_target(repo_path, checkpoint_repo)

    # Check if branch exists
    result = run_git(
        ["ls-remote", "--heads", target, CHECKPOINT_BRANCH],
        cwd=repo_path,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            # Surface gateway/auth errors instead of silently returning None
            print(f"Warning: failed to access checkpoint repo: {stderr}", file=sys.stderr)
        return None
    if not result.stdout.strip():
        return None

    # Fetch the branch
    fetch_result = run_git(["fetch", target, CHECKPOINT_BRANCH], cwd=repo_path, check=False)
    if fetch_result.returncode != 0:
        stderr = fetch_result.stderr.strip()
        if stderr:
            print(f"Warning: failed to fetch checkpoint branch: {stderr}", file=sys.stderr)
        return None

    if checkpoint_repo:
        # Resolve FETCH_HEAD to a stable SHA before returning.
        # FETCH_HEAD is overwritten by any subsequent git fetch.
        result = run_git(["rev-parse", "FETCH_HEAD"], cwd=repo_path, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    return f"origin/{CHECKPOINT_BRANCH}"


def read_git_file(ref: str, path: str, repo_path: str) -> str | None:
    """
    Read a file from a git ref via `git show`.

    Works through the gateway sidecar without requiring worktrees.

    Args:
        ref: Git ref to read from (e.g. "origin/egg/checkpoints/v2")
        path: Path relative to the ref root (e.g. "index.json")
        repo_path: Path to the repository.

    Returns:
        File contents as string, or None if the file doesn't exist.
    """
    result = run_git(
        ["show", f"{ref}:{path}"],
        cwd=repo_path,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def load_index_from_ref(ref: str, repo_path: str) -> CheckpointIndexV2 | None:
    """Load the checkpoint index from a git ref."""
    content = read_git_file(ref, "index.json", repo_path)
    if content is None:
        return None
    try:
        data = json.loads(content)
        return CheckpointIndexV2.model_validate(data)
    except (json.JSONDecodeError, Exception):
        return None


def load_checkpoint_from_ref(checkpoint_id: str, ref: str, repo_path: str) -> CheckpointV2 | None:
    """Load a full checkpoint from a git ref by ID."""
    # Reuse get_checkpoint_path logic for the subdirectory structure
    rel_path = get_checkpoint_path(Path("checkpoints"), checkpoint_id)
    content = read_git_file(ref, str(rel_path), repo_path)
    if content is None:
        return None
    try:
        data = json.loads(content)
        return CheckpointV2.model_validate(data)
    except (json.JSONDecodeError, Exception):
        return None


def format_timestamp(ts: datetime | str | None) -> str:
    """Format a timestamp for display."""
    if ts is None:
        return "N/A"
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ts
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def format_tokens(n: int) -> str:
    """Format token count for display."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _format_trigger(trigger_type: TriggerType | str) -> str:
    """Format trigger type for display."""
    value = trigger_type.value if isinstance(trigger_type, TriggerType) else trigger_type
    return {"commit": "commit", "session_end": "session-end"}.get(value, value)


def _format_status(status: SessionStatus | str | None) -> str:
    """Format session status for display."""
    if status is None:
        return ""
    return status.value if isinstance(status, SessionStatus) else status


def print_checkpoint_summary(
    checkpoint: CheckpointSummaryV2 | CheckpointV2 | dict[str, Any],
) -> None:
    """Print a one-line summary of a checkpoint."""
    if isinstance(checkpoint, (CheckpointSummaryV2, CheckpointV2)):
        data = checkpoint.model_dump()
    else:
        data = checkpoint

    cp_id = data.get("id", "unknown")
    commit = data.get("commit_sha")
    created = format_timestamp(data.get("created_at"))
    branch = data.get("branch", "")
    issue = data.get("issue_number")
    pr = data.get("pr_number")
    phase = data.get("pipeline_phase", "")
    trigger = data.get("trigger_type", "")
    status = data.get("session_status", "")
    agent = data.get("agent_type", "")
    repo = data.get("repo", "")

    # Extract metrics depending on model type
    if isinstance(checkpoint, CheckpointV2):
        msg_count = checkpoint.transcript.message_count if checkpoint.transcript else 0
        tool_count = len(checkpoint.tool_calls)
        tokens = checkpoint.token_usage.total_tokens if checkpoint.token_usage else 0
    elif isinstance(checkpoint, CheckpointSummaryV2):
        msg_count = checkpoint.message_count
        tool_count = checkpoint.tool_call_count
        tokens = checkpoint.total_tokens
    else:
        transcript = data.get("transcript", {})
        msg_count = data.get(
            "message_count", transcript.get("message_count", 0) if transcript else 0
        )
        tool_count = data.get("tool_call_count", len(data.get("tool_calls", [])))
        token_usage = data.get("token_usage", {})
        tokens = data.get("total_tokens", token_usage.get("total_tokens", 0) if token_usage else 0)

    # Build summary line
    parts = [f"{cp_id}"]
    if commit:
        parts.append(f"commit:{commit[:7]}")
    parts.append(f"trigger:{_format_trigger(trigger)}")
    if status:
        parts.append(f"status:{_format_status(status)}")
    if branch:
        parts.append(f"branch:{branch}")
    if issue:
        parts.append(f"issue:#{issue}")
    if pr:
        parts.append(f"pr:#{pr}")
    if phase:
        parts.append(f"phase:{phase}")
    if agent and agent != "unknown":
        parts.append(f"agent:{agent}")
    if repo:
        parts.append(f"repo:{repo}")
    parts.append(f"msgs:{msg_count}")
    parts.append(f"tools:{tool_count}")
    parts.append(f"tokens:{format_tokens(tokens)}")
    parts.append(f"@{created}")

    print("  " + " | ".join(parts))


def print_checkpoint_details(checkpoint: CheckpointV2 | dict[str, Any]) -> None:
    """Print detailed checkpoint information."""
    if isinstance(checkpoint, CheckpointV2):
        data = checkpoint.model_dump()
    else:
        data = checkpoint

    print(f"Checkpoint: {data.get('id')}")
    print(f"  Trigger: {_format_trigger(data.get('trigger_type', ''))}")
    if data.get("session_status"):
        print(f"  Session Status: {_format_status(data.get('session_status'))}")
    if data.get("commit_sha"):
        print(f"  Commit: {data.get('commit_sha')}")
    if data.get("push_sha"):
        print(f"  Push SHA: {data.get('push_sha')}")
    print(f"  Branch: {data.get('branch', 'N/A')}")
    print(f"  Created: {format_timestamp(data.get('created_at'))}")

    if data.get("issue_number"):
        print(f"  Issue: #{data.get('issue_number')}")
    if data.get("pr_number"):
        print(f"  PR: #{data.get('pr_number')}")
    if data.get("pipeline_phase"):
        print(f"  Phase: {data.get('pipeline_phase')}")
    if data.get("repo"):
        print(f"  Repo: {data.get('repo')}")

    agent = data.get("agent_type", "")
    if agent and agent != "unknown":
        print(f"  Agent Type: {agent}")

    # Session metadata
    print()
    print("Session:")
    print(f"  Session ID: {data.get('session_id', 'N/A')}")
    session = data.get("session", {})
    if session.get("container_id"):
        print(f"  Container: {session.get('container_id')}")
    if session.get("agent_role"):
        print(f"  Role: {session.get('agent_role')}")
    if session.get("model"):
        print(f"  Model: {session.get('model')}")
    print(
        f"  Started: {format_timestamp(data.get('session_started_at') or session.get('started_at'))}"
    )
    if data.get("session_ended_at") or session.get("ended_at"):
        print(
            f"  Ended: {format_timestamp(data.get('session_ended_at') or session.get('ended_at'))}"
        )
    if session.get("duration_seconds"):
        duration = session.get("duration_seconds")
        if duration >= 3600:
            print(f"  Duration: {duration / 3600:.1f} hours")
        elif duration >= 60:
            print(f"  Duration: {duration / 60:.1f} minutes")
        else:
            print(f"  Duration: {duration:.0f} seconds")

    # Token usage
    token_usage = data.get("token_usage", {})
    if token_usage:
        print()
        print("Token Usage:")
        print(f"  Input: {format_tokens(token_usage.get('input_tokens', 0))}")
        print(f"  Output: {format_tokens(token_usage.get('output_tokens', 0))}")
        print(f"  Cache Read: {format_tokens(token_usage.get('cache_read_tokens', 0))}")
        print(f"  Total: {format_tokens(token_usage.get('total_tokens', 0))}")
        if token_usage.get("estimated_cost_usd"):
            print(f"  Est. Cost: ${token_usage.get('estimated_cost_usd'):.4f}")

    # Transcript summary
    transcript = data.get("transcript", {})
    if transcript:
        print()
        print("Transcript:")
        print(f"  Messages: {transcript.get('message_count', 0)}")
        if transcript.get("truncated"):
            print(f"  Truncated: Yes ({transcript.get('truncation_reason', 'size limit')})")

    # Tool calls summary
    tool_calls = data.get("tool_calls", [])
    if tool_calls:
        print()
        print(f"Tool Calls: {len(tool_calls)}")
        # Group by tool name
        tool_counts: dict[str, int] = {}
        for tc in tool_calls:
            name = tc.get("name", "unknown")
            tool_counts[name] = tool_counts.get(name, 0) + 1
        for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
            print(f"  {name}: {count}")

    # Files touched summary
    files = data.get("files_touched", [])
    if files:
        print()
        print(f"Files Touched: {len(files)}")
        # Group by operation
        op_counts: dict[str, int] = {}
        for f in files:
            op = f.get("operation", "unknown")
            op_counts[op] = op_counts.get(op, 0) + 1
        for op, count in sorted(op_counts.items()):
            print(f"  {op}: {count}")

    # Inter-agent messages (concurrent execution mode)
    inter_agent_messages = data.get("inter_agent_messages", [])
    if inter_agent_messages:
        print()
        print(f"Inter-Agent Messages: {len(inter_agent_messages)}")
        sent = sum(1 for m in inter_agent_messages if m.get("direction") == "sent")
        received = sum(1 for m in inter_agent_messages if m.get("direction") == "received")
        print(f"  Sent: {sent}, Received: {received}")
        # Group by message type
        type_counts: dict[str, int] = {}
        for m in inter_agent_messages:
            msg_type = m.get("message_type", "unknown")
            type_counts[msg_type] = type_counts.get(msg_type, 0) + 1
        for msg_type, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"  {msg_type}: {count}")
        print()
        for m in inter_agent_messages:
            direction = m.get("direction", "?")
            arrow = "->" if direction == "sent" else "<-"
            other = m.get("to_role") if direction == "sent" else m.get("from_role")
            subject = m.get("subject", "")
            timestamp = m.get("timestamp", "")
            print(f"  {arrow} {other}: [{m.get('message_type', '')}] {subject} ({timestamp})")

    # Agent anchors (post-compaction state recovery)
    anchors = data.get("anchors", [])
    if anchors:
        print()
        print(f"Agent Anchors: {len(anchors)}")
        for anchor in anchors:
            agent_id = anchor.get("agent_id", "unknown")
            role = anchor.get("role", "unknown")
            status = anchor.get("status", "unknown")
            print(f"  {agent_id} ({role}) — {status}")

            task = anchor.get("task", {})
            if task:
                desc = task.get("description", "")
                phase = task.get("phase", "")
                if desc:
                    print(f"    Task: {desc}")
                if phase:
                    print(f"    Phase: {phase}")

            progress = anchor.get("progress", [])
            if progress:
                latest = progress[-1] if isinstance(progress[-1], dict) else {}
                step = latest.get("step", "")
                state = latest.get("state", "")
                if step:
                    print(f"    Latest Progress: {step} ({state})")
                print(f"    Progress Steps: {len(progress)}")

            brc = anchor.get("brc_state", {})
            if brc:
                brc_phase = brc.get("phase", "")
                if brc_phase:
                    print(f"    BRC Phase: {brc_phase}")
                acks = brc.get("acks", [])
                nacks = brc.get("nacks", [])
                if acks:
                    print(f"    ACKs: {', '.join(str(a) for a in acks)}")
                if nacks:
                    print(f"    NACKs: {len(nacks)}")

            decisions = anchor.get("decisions", [])
            if decisions:
                print(f"    Decisions: {len(decisions)}")

            files = anchor.get("files_modified", [])
            if files:
                print(f"    Files Modified: {len(files)}")

            errors = anchor.get("errors_encountered", [])
            if errors:
                print(f"    Errors: {len(errors)}")


def _get_source_repo(repo_path: str | None = None) -> str | None:
    """Extract source repo name (owner/repo) from git remote URL.

    This works in the sandbox even when repositories.yaml is not
    available — it only needs a local git repo with a remote.

    Args:
        repo_path: Optional repo path override.

    Returns:
        Source repo in "owner/repo" format, or None.
    """
    repo_path = repo_path or get_repo_path()
    try:
        result = run_git(["remote", "get-url", "origin"], cwd=repo_path, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        remote_url = result.stdout.strip()
        match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    except Exception:
        pass
    return None


def _get_checkpoint_repo_from_args(
    args: argparse.Namespace,
) -> tuple[str | None, str | None]:
    """Get checkpoint_repo from CLI args or repo config.

    Returns:
        Tuple of (checkpoint_repo, source_repo).  ``source_repo`` is
        always populated when it can be determined, even if
        ``checkpoint_repo`` resolution fails — this avoids duplicate
        ``git remote`` calls in callers that need the source_repo
        fallback.
    """
    checkpoint_repo: str | None = getattr(args, "checkpoint_repo", None)
    if checkpoint_repo:
        return _validate_checkpoint_repo(checkpoint_repo), None
    # Check environment variable
    env_repo = os.environ.get("EGG_CHECKPOINT_REPO")
    if env_repo:
        return _validate_checkpoint_repo(env_repo), None
    # Try to auto-detect from repo config by reading the git remote URL
    # and looking up checkpoint_repo in repositories.yaml.
    repo_path = args.repo_path or get_repo_path()
    source_repo = _get_source_repo(repo_path)
    if not source_repo:
        return None, None
    try:
        from config.repo_config import get_checkpoint_repo

        return get_checkpoint_repo(source_repo), source_repo
    except FileNotFoundError:
        logger.debug("repositories.yaml not found, cannot auto-detect checkpoint_repo")
    except Exception as e:
        logger.debug("Failed to auto-detect checkpoint_repo: %s", e)
    return None, source_repo


def _print_repo_hint(checkpoint_repo: str | None = None) -> None:
    """Print a hint to stderr about configuring checkpoint_repo.

    No-op when *checkpoint_repo* is already set (the hint is irrelevant).
    """
    if checkpoint_repo is not None:
        return
    print(_CHECKPOINT_REPO_HINT, file=sys.stderr)


def _add_checkpoint_resolution_params(params: dict[str, Any], args: argparse.Namespace) -> None:
    """Add checkpoint_repo and source_repo params for gateway resolution.

    When checkpoint_repo can be resolved locally (e.g. repositories.yaml
    is available), it is passed directly.  Otherwise, source_repo is
    passed so the gateway can perform the config lookup on its side.
    """
    checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
    if checkpoint_repo:
        params["checkpoint_repo"] = checkpoint_repo
    elif source_repo:
        # Can't resolve checkpoint_repo locally (e.g. sandbox without
        # repositories.yaml). Pass source_repo so the gateway can
        # look it up in its own config.
        params["source_repo"] = source_repo


def _decompose_composite_role(
    agent_type: str | None,
) -> tuple[str | None, str | None]:
    """Decompose a composite reviewer role for API queries.

    Returns ``(api_agent_type, composite_role)`` where *api_agent_type* is the
    base ``AgentType`` value suitable for the gateway API (e.g. ``"reviewer"``)
    and *composite_role* is the original composite name for client-side
    post-filtering.  When *agent_type* is not a composite role, it is returned
    unchanged and *composite_role* is ``None``.
    """
    if agent_type in COMPOSITE_REVIEWER_ROLES:
        return AgentType.REVIEWER.value, agent_type
    return agent_type, None


def _http_filter_composite_role(
    summaries: list[dict[str, Any]],
    composite_role: str,
    gateway_url: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Post-filter HTTP results by composite reviewer role (N+1 fetches).

    The gateway index only stores the base ``AgentType`` (e.g. ``"reviewer"``).
    To filter by a specific composite role (e.g. ``"reviewer_code"``), we must
    fetch each full checkpoint and inspect ``session.agent_role``.
    """
    filtered: list[dict[str, Any]] = []
    repo_path = params.get("repo_path", "")
    for s in summaries:
        cp_id = s.get("id", "")
        try:
            show_params: dict[str, Any] = {"repo_path": repo_path}
            if params.get("checkpoint_repo"):
                show_params["checkpoint_repo"] = params["checkpoint_repo"]
            elif params.get("source_repo"):
                show_params["source_repo"] = params["source_repo"]
            cp_result = _http_get(gateway_url, f"/api/v1/checkpoints/{cp_id}", show_params)
            cp_data = cp_result.get("data", {}).get("checkpoint", {})
            session = cp_data.get("session", {})
            if session.get("agent_role") == composite_role:
                filtered.append(s)
        except RuntimeError as exc:
            print(f"Warning: failed to fetch checkpoint {cp_id}: {exc}", file=sys.stderr)
            continue
    return filtered


def _build_list_params(args: argparse.Namespace) -> dict[str, Any]:
    """Build query parameters for checkpoint list from CLI args."""
    params: dict[str, Any] = {"limit": args.limit}
    if args.issue:
        params["issue"] = args.issue
    if getattr(args, "pr", None):
        params["pr"] = args.pr
    if getattr(args, "branch", None):
        params["branch"] = args.branch
    if getattr(args, "session", None):
        params["session"] = args.session
    if getattr(args, "trigger", None):
        params["trigger"] = args.trigger
    if getattr(args, "status", None):
        params["status"] = args.status
    if getattr(args, "agent_type", None):
        api_agent_type, _ = _decompose_composite_role(args.agent_type)
        params["agent_type"] = api_agent_type
    if getattr(args, "phase", None):
        params["phase"] = args.phase
    if getattr(args, "pipeline", None):
        params["pipeline"] = args.pipeline
    if getattr(args, "repo", None):
        params["repo"] = args.repo
    repo_path = args.repo_path or get_repo_path()
    params["repo_path"] = repo_path
    _add_checkpoint_resolution_params(params, args)
    return params


def collect_checkpoints(filters: dict[str, Any]) -> dict[str, Any]:
    """Return every checkpoint summary matching the filter set.

    Public helper shared by :func:`cmd_list` (via the CLI's direct-git
    fallback path) and
    :func:`egg_agent_tools.handlers.checkpoint.checkpoint_list` (MCP
    handler). Returns JSON-serialisable dicts rather than
    ``CheckpointSummaryV2`` Pydantic instances so MCP callers and
    external consumers do not need to import ``egg_contracts.checkpoints``.

    Args:
        filters: dict with any of ``repo_path``, ``checkpoint_repo``,
            ``branch``, ``issue``, ``pr``, ``session``, ``trigger``,
            ``status``, ``agent_type``, ``phase``, ``pipeline``,
            ``repo``, ``limit`` (upstream cap applied before the
            MCP-level page).

    Returns:
        ``{"checkpoints": [dict, ...], "composite_role": str|None,
           "ref": str|None, "checkpoint_repo": str|None}`` — ``ref``
        and ``checkpoints`` may be empty when no checkpoint branch
        exists. ``composite_role`` is non-None when the caller asked
        for a BRC composite reviewer role (``reviewer_code``,
        ``reviewer_contract``, etc.).
    """
    repo_path = filters.get("repo_path")
    if not repo_path:
        raise ValueError("'repo_path' is required on collect_checkpoints")
    checkpoint_repo = filters.get("checkpoint_repo")

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    composite_role: str | None = None
    if not ref:
        return {
            "checkpoints": [],
            "composite_role": composite_role,
            "ref": None,
            "checkpoint_repo": checkpoint_repo,
        }

    index = load_index_from_ref(ref, repo_path)
    if not index:
        return {
            "checkpoints": [],
            "composite_role": composite_role,
            "ref": ref,
            "checkpoint_repo": checkpoint_repo,
        }

    agent_type_filter, composite_role = _decompose_composite_role(filters.get("agent_type"))

    summaries = filter_checkpoints_v2(
        index,
        issue_number=filters.get("issue"),
        pr_number=filters.get("pr"),
        branch=filters.get("branch"),
        session_id=filters.get("session"),
        trigger_type=filters.get("trigger"),
        session_status=filters.get("status"),
        agent_type=agent_type_filter,
        pipeline_phase=filters.get("phase"),
        pipeline_id=filters.get("pipeline"),
        repo=filters.get("repo"),
        limit=filters.get("limit"),
    )

    if composite_role and summaries:
        filtered = []
        for s in summaries:
            cp = load_checkpoint_from_ref(s.id, ref, repo_path)
            if cp and cp.session and cp.session.agent_role == composite_role:
                filtered.append(s)
        summaries = filtered

    return {
        "checkpoints": [s.model_dump(mode="json") for s in summaries],
        "composite_role": composite_role,
        "ref": ref,
        "checkpoint_repo": checkpoint_repo,
    }


def load_checkpoint(
    identifier: str, repo_path: str, checkpoint_repo: str | None = None
) -> dict[str, Any] | None:
    """Load a single checkpoint by ID (``ckpt-...``) or commit SHA.

    Returns the ``model_dump``'d CheckpointV2 dict, or ``None`` when no
    matching checkpoint exists on the branch. Pure helper shared by
    :func:`cmd_show` and
    :func:`egg_agent_tools.handlers.checkpoint.checkpoint_show`.
    """
    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        return None

    cp: CheckpointV2 | None = None
    if identifier.startswith("ckpt-"):
        cp = load_checkpoint_from_ref(identifier, ref, repo_path)
    else:
        index = load_index_from_ref(ref, repo_path)
        if index:
            checkpoint_id = index.get_by_commit(identifier)
            if checkpoint_id:
                cp = load_checkpoint_from_ref(checkpoint_id, ref, repo_path)

    if cp is None:
        return None
    return cp.model_dump(mode="json")


def search_checkpoints(query: str, filters: dict[str, Any]) -> dict[str, Any]:
    """Search checkpoint transcripts for *query* across summaries matching *filters*.

    Public helper shared by :func:`cmd_search` and
    :func:`egg_agent_tools.handlers.checkpoint.checkpoint_search`.
    Returns ``{"matches": [{"summary": {...}, "snippets": [...]}, ...],
    "composite_role", "ref", "checkpoint_repo", "query"}``.
    """
    if not isinstance(query, str) or not query:
        raise ValueError("'query' is required")

    collected = collect_checkpoints(filters)
    summaries_dicts = collected["checkpoints"]
    ref = collected["ref"]
    checkpoint_repo = collected["checkpoint_repo"]
    composite_role = collected["composite_role"]

    if not ref or not summaries_dicts:
        return {
            "matches": [],
            "composite_role": composite_role,
            "ref": ref,
            "checkpoint_repo": checkpoint_repo,
            "query": query,
        }

    repo_path = filters["repo_path"]
    matches: list[dict[str, Any]] = []
    for summary_dict in summaries_dicts:
        cp = load_checkpoint_from_ref(summary_dict["id"], ref, repo_path)
        if cp is None:
            continue
        if composite_role and not (cp.session and cp.session.agent_role == composite_role):
            continue
        snippets = _search_checkpoint_transcript(cp, query)
        if snippets:
            matches.append({"summary": summary_dict, "snippets": snippets})

    return {
        "matches": matches,
        "composite_role": composite_role,
        "ref": ref,
        "checkpoint_repo": checkpoint_repo,
        "query": query,
    }


def _cmd_list_http(args: argparse.Namespace, gateway_url: str) -> int:
    """List checkpoints via gateway HTTP API."""
    params = _build_list_params(args)
    _, composite_role = _decompose_composite_role(getattr(args, "agent_type", None))
    result = _http_get(gateway_url, "/api/v1/checkpoints", params)
    summaries = result.get("data", {}).get("checkpoints", [])

    # Post-filter by composite reviewer role if needed
    if composite_role and summaries:
        summaries = _http_filter_composite_role(summaries, composite_role, gateway_url, params)

    if not summaries:
        checkpoint_repo = params.get("checkpoint_repo")
        _print_empty_result(
            checkpoint_repo, CHECKPOINT_BRANCH, args.json, hint=(checkpoint_repo is None)
        )
        return 0

    if args.json:
        print(json.dumps(summaries, indent=2))
    else:
        print(f"Checkpoints ({len(summaries)} found):")
        print()
        for s in summaries:
            print_checkpoint_summary(s)

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List checkpoints with metadata.

    Delegates to :func:`collect_checkpoints` (also used by the
    ``mcp__checkpoint__list`` MCP handler) so both dispatch paths
    share one helper. When a gateway is configured we still use the
    HTTP path for parity with legacy behaviour (live pipelines).
    """
    gateway_url = _get_gateway_url()
    if gateway_url:
        try:
            return _cmd_list_http(args, gateway_url)
        except RuntimeError as e:
            logger.debug("HTTP list failed, falling back to git: %s", e)
            print(f"Warning: gateway checkpoint query failed: {e}", file=sys.stderr)

    repo_path = args.repo_path or get_repo_path()
    try:
        checkpoint_repo, _ = _get_checkpoint_repo_from_args(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    filters: dict[str, Any] = {
        "repo_path": repo_path,
        "checkpoint_repo": checkpoint_repo,
        "branch": args.branch,
        "issue": args.issue,
        "pr": getattr(args, "pr", None),
        "session": getattr(args, "session", None),
        "trigger": getattr(args, "trigger", None),
        "status": getattr(args, "status", None),
        "agent_type": getattr(args, "agent_type", None),
        "phase": getattr(args, "phase", None),
        "pipeline": getattr(args, "pipeline", None),
        "repo": getattr(args, "repo", None),
        "limit": args.limit,
    }

    try:
        collected = collect_checkpoints(filters)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    items = collected["checkpoints"]
    if collected["ref"] is None:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints found (checkpoint branch does not exist)",
        )
        return 0
    if not items:
        _print_empty_result(checkpoint_repo, CHECKPOINT_BRANCH, args.json)
        return 0

    if args.json:
        print(json.dumps(items, indent=2))
    else:
        print(f"Checkpoints ({len(items)} found):")
        print()
        for summary_dict in items:
            print_checkpoint_summary(summary_dict)

    return 0


def _cmd_show_http(args: argparse.Namespace, gateway_url: str) -> int:
    """Show checkpoint via gateway HTTP API."""
    params: dict[str, Any] = {"repo_path": args.repo_path or get_repo_path()}
    _add_checkpoint_resolution_params(params, args)
    result = _http_get(gateway_url, f"/api/v1/checkpoints/{args.identifier}", params)

    if not result.get("success"):
        print(f"No checkpoint found for '{args.identifier}'", file=sys.stderr)
        return 1

    data = result.get("data", {}).get("checkpoint", {})
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_checkpoint_details(data)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Display full checkpoint details by checkpoint ID or commit SHA.

    Delegates to :func:`load_checkpoint` (also used by the
    ``mcp__checkpoint__show`` MCP handler) so both dispatch paths
    share one helper.
    """
    gateway_url = _get_gateway_url()
    if gateway_url:
        try:
            return _cmd_show_http(args, gateway_url)
        except RuntimeError as e:
            logger.debug("HTTP show failed, falling back to git: %s", e)
            print(f"Warning: gateway checkpoint query failed: {e}", file=sys.stderr)

    repo_path = args.repo_path or get_repo_path()
    identifier = args.identifier
    try:
        checkpoint_repo, _ = _get_checkpoint_repo_from_args(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Short-circuit the "no checkpoint branch" case for legacy stderr
    # parity — load_checkpoint() returns None without distinguishing.
    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        print("No checkpoints found (checkpoint branch does not exist)", file=sys.stderr)
        _print_repo_hint(checkpoint_repo)
        return 1

    checkpoint_dict = load_checkpoint(identifier, repo_path, checkpoint_repo)
    if checkpoint_dict is None:
        print(f"No checkpoint found for '{identifier}'", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(checkpoint_dict, indent=2))
    else:
        print_checkpoint_details(checkpoint_dict)
    return 0


def _cmd_browse_http(args: argparse.Namespace, gateway_url: str) -> int:
    """Browse checkpoints via gateway HTTP API."""
    params: dict[str, Any] = {
        "issue": args.issue,
        "limit": args.limit,
        "repo_path": args.repo_path or get_repo_path(),
    }
    if getattr(args, "repo", None):
        params["repo"] = args.repo
    _add_checkpoint_resolution_params(params, args)

    result = _http_get(gateway_url, "/api/v1/checkpoints", params)
    summaries = result.get("data", {}).get("checkpoints", [])

    if not summaries:
        checkpoint_repo = params.get("checkpoint_repo")
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message=f"No checkpoints found for issue #{args.issue}",
            hint=(checkpoint_repo is None),
        )
        return 0

    if args.json:
        print(json.dumps(summaries, indent=2))
    else:
        print(f"Checkpoints for Issue #{args.issue} ({len(summaries)} found):")
        print()

        # Group by session
        sessions: dict[str, list[dict[str, Any]]] = {}
        for s in summaries:
            sid = s.get("session_id", "unknown")
            if sid not in sessions:
                sessions[sid] = []
            sessions[sid].append(s)

        for sid, session_summaries in sessions.items():
            first = session_summaries[0]
            agent = first.get("agent_type", "unknown")
            triggers = {_format_trigger(s.get("trigger_type", "")) for s in session_summaries}
            print(
                f"Session: {sid[:12]}... (agent: {agent}, triggers: {', '.join(sorted(triggers))})"
            )
            for s in session_summaries:
                print_checkpoint_summary(s)
            print()

    return 0


def cmd_browse(args: argparse.Namespace) -> int:
    """Filter checkpoints by issue number."""
    gateway_url = _get_gateway_url()
    if gateway_url:
        try:
            return _cmd_browse_http(args, gateway_url)
        except RuntimeError as e:
            logger.debug("HTTP browse failed, falling back to git: %s", e)
            print(f"Warning: gateway checkpoint query failed: {e}", file=sys.stderr)

    repo_path = args.repo_path or get_repo_path()
    checkpoint_repo, _ = _get_checkpoint_repo_from_args(args)

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints found (checkpoint branch does not exist)",
        )
        return 0

    index = load_index_from_ref(ref, repo_path)
    if not index:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints found",
        )
        return 0

    summaries = filter_checkpoints_v2(
        index,
        issue_number=args.issue,
        repo=getattr(args, "repo", None),
        limit=args.limit,
    )

    if not summaries:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message=f"No checkpoints found for issue #{args.issue}",
        )
        return 0

    if args.json:
        output = [s.model_dump(mode="json") for s in summaries]
        print(json.dumps(output, indent=2))
    else:
        print(f"Checkpoints for Issue #{args.issue} ({len(summaries)} found):")
        print()

        # Group by session
        sessions: dict[str, list[CheckpointSummaryV2]] = {}
        for s in summaries:
            sid = s.session_id
            if sid not in sessions:
                sessions[sid] = []
            sessions[sid].append(s)

        for sid, session_summaries in sessions.items():
            first = session_summaries[0]
            agent = first.agent_type.value if first.agent_type != AgentType.UNKNOWN else "unknown"
            triggers = {_format_trigger(s.trigger_type) for s in session_summaries}
            print(
                f"Session: {sid[:12]}... (agent: {agent}, triggers: {', '.join(sorted(triggers))})"
            )
            for s in session_summaries:
                print_checkpoint_summary(s)
            print()

    return 0


def _cmd_context_http(args: argparse.Namespace, gateway_url: str) -> int:
    """Context summary via gateway HTTP API."""
    params: dict[str, Any] = {
        "limit": args.limit,
        "repo_path": args.repo_path or get_repo_path(),
    }
    if getattr(args, "pipeline", None):
        params["pipeline"] = args.pipeline
    if getattr(args, "issue", None):
        params["issue"] = args.issue
    composite_role: str | None = None
    if getattr(args, "agent_type", None):
        api_agent_type, composite_role = _decompose_composite_role(args.agent_type)
        params["agent_type"] = api_agent_type
    if getattr(args, "phase", None):
        params["phase"] = args.phase
    if getattr(args, "repo", None):
        params["repo"] = args.repo
    _add_checkpoint_resolution_params(params, args)

    result = _http_get(gateway_url, "/api/v1/checkpoints", params)
    summaries = result.get("data", {}).get("checkpoints", [])

    # Post-filter by composite reviewer role if needed
    if composite_role and summaries:
        summaries = _http_filter_composite_role(summaries, composite_role, gateway_url, params)

    if not summaries:
        checkpoint_repo = params.get("checkpoint_repo")
        _print_empty_result(
            checkpoint_repo, CHECKPOINT_BRANCH, args.json, hint=(checkpoint_repo is None)
        )
        return 0

    if args.json:
        # In HTTP mode with --files, fetch each full checkpoint
        if getattr(args, "files", False):
            enriched = []
            for s in summaries:
                cp_id = s.get("id", "")
                try:
                    show_params: dict[str, Any] = {"repo_path": params["repo_path"]}
                    if params.get("checkpoint_repo"):
                        show_params["checkpoint_repo"] = params["checkpoint_repo"]
                    elif params.get("source_repo"):
                        show_params["source_repo"] = params["source_repo"]
                    cp_result = _http_get(
                        gateway_url,
                        f"/api/v1/checkpoints/{cp_id}",
                        show_params,
                    )
                    cp_data = cp_result.get("data", {}).get("checkpoint", {})
                    entry = dict(s)
                    files_touched = cp_data.get("files_touched", [])
                    if files_touched:
                        entry["files"] = [
                            {"path": f.get("path"), "operation": f.get("operation")}
                            for f in files_touched
                        ]
                    enriched.append(entry)
                except RuntimeError as e:
                    logger.debug("HTTP checkpoint fetch failed: %s", e)
                    enriched.append(s)
            print(json.dumps(enriched, indent=2))
        else:
            print(json.dumps(summaries, indent=2))
    else:
        _print_context_summary_from_dicts(
            summaries,
            gateway_url,
            args,
            checkpoint_repo=params.get("checkpoint_repo"),
            source_repo=params.get("source_repo"),
        )

    return 0


def _print_context_summary_from_dicts(
    summaries: list[dict[str, Any]],
    gateway_url: str,
    args: argparse.Namespace,
    checkpoint_repo: str | None = None,
    source_repo: str | None = None,
) -> None:
    """Print hierarchical context summary from dict data (HTTP mode)."""
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for s in summaries:
        phase_key = s.get("pipeline_phase") or "(no phase)"
        agent_key = s.get("agent_type", "unknown")
        if phase_key not in groups:
            groups[phase_key] = {}
        if agent_key not in groups[phase_key]:
            groups[phase_key][agent_key] = []
        groups[phase_key][agent_key].append(s)

    print(f"Cross-Agent Context ({len(summaries)} checkpoints)")
    print()

    for phase, agents in sorted(groups.items()):
        print(f"Phase: {phase}")
        for agent, cps in sorted(agents.items()):
            total_msgs = sum(c.get("message_count", 0) for c in cps)
            total_tools = sum(c.get("tool_call_count", 0) for c in cps)
            total_tokens = sum(c.get("total_tokens", 0) for c in cps)
            total_files = sum(c.get("files_touched_count", 0) for c in cps)
            print(
                f"  {agent} ({len(cps)} checkpoints) | "
                f"msgs:{total_msgs} tools:{total_tools} "
                f"tokens:{format_tokens(total_tokens)} files:{total_files}"
            )
            for cp in cps:
                trigger = _format_trigger(cp.get("trigger_type", ""))
                parts = [f"    {cp.get('id', '')} | trigger:{trigger}"]
                if cp.get("commit_sha"):
                    parts.append(f"commit:{cp['commit_sha'][:7]}")
                parts.append(f"msgs:{cp.get('message_count', 0)}")
                parts.append(f"tools:{cp.get('tool_call_count', 0)}")
                parts.append(f"tokens:{format_tokens(cp.get('total_tokens', 0))}")
                parts.append(f"files:{cp.get('files_touched_count', 0)}")
                parts.append(f"@{format_timestamp(cp.get('created_at'))}")
                print(" | ".join(parts))

                if getattr(args, "files", False):
                    repo_path = args.repo_path or get_repo_path()
                    cp_id = cp.get("id", "")
                    try:
                        show_params: dict[str, Any] = {"repo_path": repo_path}
                        if checkpoint_repo:
                            show_params["checkpoint_repo"] = checkpoint_repo
                        elif source_repo:
                            show_params["source_repo"] = source_repo
                        cp_result = _http_get(
                            gateway_url,
                            f"/api/v1/checkpoints/{cp_id}",
                            show_params,
                        )
                        cp_data = cp_result.get("data", {}).get("checkpoint", {})
                        for f in cp_data.get("files_touched", []):
                            op = f.get("operation", "unknown")
                            print(f"      {op:6s} {f.get('path', '')}")
                    except RuntimeError as e:
                        logger.debug("HTTP checkpoint fetch failed: %s", e)

        print()


def cmd_context(args: argparse.Namespace) -> int:
    """Show cross-agent context summary for a pipeline or issue."""
    gateway_url = _get_gateway_url()
    if gateway_url:
        try:
            return _cmd_context_http(args, gateway_url)
        except RuntimeError as e:
            logger.debug("HTTP context failed, falling back to git: %s", e)
            print(f"Warning: gateway checkpoint query failed: {e}", file=sys.stderr)

    repo_path = args.repo_path or get_repo_path()
    checkpoint_repo, _ = _get_checkpoint_repo_from_args(args)

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints found (checkpoint branch does not exist)",
        )
        return 0

    index = load_index_from_ref(ref, repo_path)
    if not index:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints found",
        )
        return 0

    # Resolve composite reviewer roles to base AgentType for index lookup
    agent_type_filter, composite_role = _decompose_composite_role(getattr(args, "agent_type", None))

    summaries = filter_checkpoints_v2(
        index,
        pipeline_id=getattr(args, "pipeline", None),
        issue_number=getattr(args, "issue", None),
        agent_type=agent_type_filter,
        pipeline_phase=getattr(args, "phase", None),
        repo=getattr(args, "repo", None),
        limit=args.limit,
    )

    # Post-filter by composite reviewer role if requested
    if composite_role and summaries:
        filtered = []
        for s in summaries:
            cp = load_checkpoint_from_ref(s.id, ref, repo_path)
            if cp and cp.session and cp.session.agent_role == composite_role:
                filtered.append(s)
        summaries = filtered

    if not summaries:
        _print_empty_result(checkpoint_repo, CHECKPOINT_BRANCH, args.json)
        return 0

    if args.json:
        output = _build_context_json(summaries, ref, repo_path, args)
        print(json.dumps(output, indent=2))
    else:
        _print_context_summary(summaries, ref, repo_path, args)

    return 0


def _build_context_json(
    summaries: list[CheckpointSummaryV2],
    ref: str,
    repo_path: str,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    """Build JSON output for context command."""
    results = []
    for s in summaries:
        entry: dict[str, Any] = s.model_dump(mode="json")
        if getattr(args, "files", False):
            checkpoint = load_checkpoint_from_ref(s.id, ref, repo_path)
            if checkpoint:
                entry["files"] = [
                    {"path": f.path, "operation": f.operation.value}
                    for f in checkpoint.files_touched
                ]
        results.append(entry)
    return results


def _print_context_summary(
    summaries: list[CheckpointSummaryV2],
    ref: str,
    repo_path: str,
    args: argparse.Namespace,
) -> None:
    """Print hierarchical context summary grouped by phase and agent."""
    # Group by phase -> agent_type
    groups: dict[str, dict[str, list[CheckpointSummaryV2]]] = {}
    for s in summaries:
        phase_key = s.pipeline_phase or "(no phase)"
        agent_key = s.agent_type.value if s.agent_type != AgentType.UNKNOWN else "unknown"
        if phase_key not in groups:
            groups[phase_key] = {}
        if agent_key not in groups[phase_key]:
            groups[phase_key][agent_key] = []
        groups[phase_key][agent_key].append(s)

    print(f"Cross-Agent Context ({len(summaries)} checkpoints)")
    print()

    for phase, agents in sorted(groups.items()):
        print(f"Phase: {phase}")
        for agent, cps in sorted(agents.items()):
            total_msgs = sum(c.message_count for c in cps)
            total_tools = sum(c.tool_call_count for c in cps)
            total_tokens = sum(c.total_tokens for c in cps)
            total_files = sum(c.files_touched_count for c in cps)
            print(
                f"  {agent} ({len(cps)} checkpoints) | "
                f"msgs:{total_msgs} tools:{total_tools} "
                f"tokens:{format_tokens(total_tokens)} files:{total_files}"
            )
            for cp in cps:
                trigger = _format_trigger(cp.trigger_type)
                parts = [f"    {cp.id} | trigger:{trigger}"]
                if cp.commit_sha:
                    parts.append(f"commit:{cp.commit_sha[:7]}")
                parts.append(f"msgs:{cp.message_count}")
                parts.append(f"tools:{cp.tool_call_count}")
                parts.append(f"tokens:{format_tokens(cp.total_tokens)}")
                parts.append(f"files:{cp.files_touched_count}")
                parts.append(f"@{format_timestamp(cp.created_at)}")
                print(" | ".join(parts))

                # Optionally show file paths
                if getattr(args, "files", False):
                    checkpoint = load_checkpoint_from_ref(cp.id, ref, repo_path)
                    if checkpoint and checkpoint.files_touched:
                        for f in checkpoint.files_touched:
                            print(f"      {f.operation.value:6s} {f.path}")

        print()


def _cmd_cost_http(args: argparse.Namespace, gateway_url: str) -> int:
    """Cost breakdown via gateway HTTP API."""
    params: dict[str, Any] = {
        "limit": args.limit,
        "repo_path": args.repo_path or get_repo_path(),
    }
    if getattr(args, "pipeline", None):
        params["pipeline"] = args.pipeline
    if getattr(args, "issue", None):
        params["issue"] = args.issue
    if getattr(args, "pr", None):
        params["pr"] = args.pr
    _add_checkpoint_resolution_params(params, args)

    result = _http_get(gateway_url, "/api/v1/checkpoints/cost", params)
    data = result.get("data", {})

    if not data or data.get("checkpoint_count", 0) == 0:
        checkpoint_repo = params.get("checkpoint_repo")
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints with token usage data found",
            shape="cost",
            hint=(checkpoint_repo is None),
        )
        return 0

    pipeline_id = getattr(args, "pipeline", None)
    issue = getattr(args, "issue", None)
    pr = getattr(args, "pr", None)

    if args.json:
        data["pipeline_id"] = pipeline_id
        data["issue_number"] = issue
        data["pr_number"] = pr
        print(json.dumps(data, indent=2))
        return 0

    label = pipeline_id or (f"issue #{issue}" if issue else f"PR #{pr}" if pr else "all")
    print(f"Pipeline: {label}")
    print(f"Checkpoints: {data.get('checkpoint_count', 0)}")
    print()

    print(f"  {'Phase':<12s}  {'Agent':<14s}  {'Input':>8s}  {'Output':>8s}  {'Cost':>8s}")
    print(f"  {'─' * 12}  {'─' * 14}  {'─' * 8}  {'─' * 8}  {'─' * 8}")

    for row in data.get("breakdown", []):
        print(
            f"  {row['phase']:<12s}  {row['agent']:<14s}"
            f"  {format_tokens(row['input_tokens']):>8s}"
            f"  {format_tokens(row['output_tokens']):>8s}"
            f"  ${row['cost_usd']:>6.2f}"
        )

    total_input = data.get("total_input_tokens", 0)
    total_output = data.get("total_output_tokens", 0)
    total_cost = data.get("total_cost_usd", 0)

    print(f"  {'─' * 12}  {'─' * 14}  {'─' * 8}  {'─' * 8}  {'─' * 8}")
    print(
        f"  {'TOTAL':<12s}  {'':<14s}  {format_tokens(total_input):>8s}"
        f"  {format_tokens(total_output):>8s}  ${total_cost:>6.2f}"
    )

    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    """Show cost breakdown for a pipeline, issue, or PR."""
    gateway_url = _get_gateway_url()
    if gateway_url:
        try:
            return _cmd_cost_http(args, gateway_url)
        except RuntimeError as e:
            logger.debug("HTTP cost failed, falling back to git: %s", e)
            print(f"Warning: gateway checkpoint query failed: {e}", file=sys.stderr)

    from .usage import TokenCounts

    repo_path = args.repo_path or get_repo_path()
    checkpoint_repo, _ = _get_checkpoint_repo_from_args(args)

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints found (checkpoint branch does not exist)",
            shape="cost",
        )
        return 0

    index = load_index_from_ref(ref, repo_path)
    if not index:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints found",
            shape="cost",
        )
        return 0

    summaries = filter_checkpoints_v2(
        index,
        pipeline_id=args.pipeline,
        issue_number=args.issue,
        pr_number=args.pr,
        limit=args.limit,
    )

    if not summaries:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints found matching filters",
            shape="cost",
        )
        return 0

    # Load full checkpoints to get token_usage and model info
    rows: list[dict[str, Any]] = []
    for s in summaries:
        checkpoint = load_checkpoint_from_ref(s.id, ref, repo_path)
        if not checkpoint or not checkpoint.token_usage:
            continue

        tu = checkpoint.token_usage
        model = checkpoint.session.model if checkpoint.session else None
        tokens = TokenCounts(
            input_tokens=tu.input_tokens,
            output_tokens=tu.output_tokens,
            cache_read_tokens=tu.cache_read_tokens,
            cache_creation_tokens=tu.cache_creation_tokens,
        )
        cost = float(tokens.calculate_cost(model=model))

        phase = checkpoint.pipeline_phase or "(none)"
        agent = checkpoint.agent_type.value if checkpoint.agent_type else "unknown"

        rows.append(
            {
                "phase": phase,
                "agent": agent,
                "input_tokens": tu.input_tokens,
                "output_tokens": tu.output_tokens,
                "cost": cost,
                "model": model,
            }
        )

    if not rows:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints with token usage data found",
            shape="cost",
        )
        return 0

    # Aggregate by (phase, agent)
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["phase"], row["agent"])
        if key not in agg:
            agg[key] = {"input": 0, "output": 0, "cost": 0.0, "count": 0}
        agg[key]["input"] += row["input_tokens"]
        agg[key]["output"] += row["output_tokens"]
        agg[key]["cost"] += row["cost"]
        agg[key]["count"] += 1

    total_input = sum(v["input"] for v in agg.values())
    total_output = sum(v["output"] for v in agg.values())
    total_cost = sum(v["cost"] for v in agg.values())

    pipeline_id = args.pipeline
    issue = args.issue
    pr = args.pr

    if args.json:
        output = {
            "pipeline_id": pipeline_id,
            "issue_number": issue,
            "pr_number": pr,
            "checkpoint_count": len(rows),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": round(total_cost, 4),
            "breakdown": [
                {
                    "phase": k[0],
                    "agent": k[1],
                    "input_tokens": v["input"],
                    "output_tokens": v["output"],
                    "cost_usd": round(v["cost"], 4),
                    "checkpoint_count": v["count"],
                }
                for k, v in sorted(agg.items())
            ],
        }
        print(json.dumps(output, indent=2))
        return 0

    # Pretty print
    label = pipeline_id or (f"issue #{issue}" if issue else f"PR #{pr}" if pr else "all")
    print(f"Pipeline: {label}")
    print(f"Checkpoints: {len(rows)}")
    print()

    # Table header
    print(f"  {'Phase':<12s}  {'Agent':<14s}  {'Input':>8s}  {'Output':>8s}  {'Cost':>8s}")
    print(f"  {'─' * 12}  {'─' * 14}  {'─' * 8}  {'─' * 8}  {'─' * 8}")

    for (phase, agent), vals in sorted(agg.items()):
        print(
            f"  {phase:<12s}  {agent:<14s}  {format_tokens(vals['input']):>8s}"
            f"  {format_tokens(vals['output']):>8s}  ${vals['cost']:>6.2f}"
        )

    print(f"  {'─' * 12}  {'─' * 14}  {'─' * 8}  {'─' * 8}  {'─' * 8}")
    print(
        f"  {'TOTAL':<12s}  {'':<14s}  {format_tokens(total_input):>8s}"
        f"  {format_tokens(total_output):>8s}  ${total_cost:>6.2f}"
    )

    return 0


def _extract_snippet(searchable: str, text: str, text_lower: str) -> str | None:
    """Extract a context snippet around the first match of *text* in *searchable*.

    Returns ``None`` when *text* is not found (case-insensitive).
    """
    searchable_lower = searchable.lower()
    if text_lower not in searchable_lower:
        return None
    idx = searchable_lower.index(text_lower)
    start = max(0, idx - 80)
    end = min(len(searchable), idx + len(text) + 80)
    snippet = searchable[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(searchable):
        snippet = snippet + "..."
    return snippet


def _search_checkpoint_transcript(checkpoint: CheckpointV2, text: str) -> list[str]:
    """Search a checkpoint's transcript for matching text.

    Args:
        checkpoint: Full checkpoint with transcript data.
        text: Case-insensitive substring to search for.

    Returns:
        List of matching snippet strings with context.  Only the first
        occurrence per message is returned.
    """
    if not checkpoint.transcript or not checkpoint.transcript.messages:
        return []

    text_lower = text.lower()
    snippets: list[str] = []

    for msg in checkpoint.transcript.messages:
        searchable = msg.content or msg.content_summary or ""
        if not searchable:
            continue
        snippet = _extract_snippet(searchable, text, text_lower)
        if snippet is not None:
            snippets.append(f"[{msg.role.value}] {snippet}")

    return snippets


def _print_search_results(
    matches: list[tuple[CheckpointSummaryV2 | dict[str, Any], list[str]]],
    text: str,
    args: argparse.Namespace,
) -> None:
    """Print search results with checkpoint summaries and matching snippets."""
    if args.json:
        output = []
        for checkpoint_info, snippets in matches:
            if isinstance(checkpoint_info, CheckpointSummaryV2):
                entry = checkpoint_info.model_dump(mode="json")
            else:
                entry = dict(checkpoint_info)
            entry["matching_snippets"] = snippets
            output.append(entry)
        print(json.dumps(output, indent=2))
        return

    print(f"Search results for {text!r} ({len(matches)} checkpoints matched):")
    print()
    for checkpoint_info, snippets in matches:
        print_checkpoint_summary(checkpoint_info)
        for snippet in snippets[:3]:  # Show up to 3 snippets per checkpoint
            print(f"    {snippet}")
        if len(snippets) > 3:
            print(f"    ... and {len(snippets) - 3} more matches")
        print()


def _cmd_search_http(args: argparse.Namespace, gateway_url: str) -> int:
    """Search checkpoints via gateway HTTP API (N+1 requests)."""
    try:
        params = _build_list_params(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    _, composite_role = _decompose_composite_role(getattr(args, "agent_type", None))
    result = _http_get(gateway_url, "/api/v1/checkpoints", params)
    summaries = result.get("data", {}).get("checkpoints", [])

    # Post-filter by composite reviewer role before transcript search
    if composite_role and summaries:
        summaries = _http_filter_composite_role(summaries, composite_role, gateway_url, params)

    if not summaries:
        checkpoint_repo = params.get("checkpoint_repo")
        _print_empty_result(
            checkpoint_repo, CHECKPOINT_BRANCH, args.json, hint=(checkpoint_repo is None)
        )
        return 0

    matches: list[tuple[CheckpointSummaryV2 | dict[str, Any], list[str]]] = []
    text = args.text
    text_lower = text.lower()
    repo_path = args.repo_path or get_repo_path()

    # Resolve checkpoint repo once outside the loop (same for every iteration)
    checkpoint_repo = params.get("checkpoint_repo")
    source_repo = params.get("source_repo")

    for s in summaries:
        cp_id = s.get("id", "")
        try:
            show_params: dict[str, Any] = {"repo_path": repo_path}
            if checkpoint_repo:
                show_params["checkpoint_repo"] = checkpoint_repo
            elif source_repo:
                show_params["source_repo"] = source_repo
            cp_result = _http_get(
                gateway_url,
                f"/api/v1/checkpoints/{cp_id}",
                show_params,
            )
            cp_data = cp_result.get("data", {}).get("checkpoint", {})
        except RuntimeError as e:
            logger.debug("HTTP checkpoint fetch failed: %s", e)
            continue

        # Search transcript messages
        transcript = cp_data.get("transcript", {})
        messages = transcript.get("messages", []) if transcript else []
        snippets: list[str] = []
        for msg in messages:
            searchable = msg.get("content") or msg.get("content_summary") or ""
            if not searchable:
                continue
            snippet = _extract_snippet(searchable, text, text_lower)
            if snippet is not None:
                role = msg.get("role", "unknown")
                snippets.append(f"[{role}] {snippet}")

        if snippets:
            matches.append((s, snippets))

    if not matches:
        checkpoint_repo = params.get("checkpoint_repo")
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message=f"No checkpoints found with transcript matching {text!r}",
            hint=(checkpoint_repo is None),
        )
        return 0

    _print_search_results(matches, text, args)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Search checkpoint transcripts for matching text.

    Delegates to :func:`search_checkpoints` (also used by the
    ``mcp__checkpoint__search`` MCP handler) so both dispatch paths
    share one helper.
    """
    gateway_url = _get_gateway_url()
    if gateway_url:
        try:
            return _cmd_search_http(args, gateway_url)
        except RuntimeError as e:
            logger.debug("HTTP search failed, falling back to git: %s", e)
            print(f"Warning: gateway checkpoint query failed: {e}", file=sys.stderr)

    repo_path = args.repo_path or get_repo_path()
    try:
        checkpoint_repo, _ = _get_checkpoint_repo_from_args(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Short-circuit for legacy parity: the helper lumps "no branch" into
    # "no matches"; the CLI distinguishes them.
    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message="No checkpoints found (checkpoint branch does not exist)",
        )
        return 0

    filters: dict[str, Any] = {
        "repo_path": repo_path,
        "checkpoint_repo": checkpoint_repo,
        "branch": getattr(args, "branch", None),
        "issue": getattr(args, "issue", None),
        "pr": getattr(args, "pr", None),
        "session": getattr(args, "session", None),
        "trigger": getattr(args, "trigger", None),
        "status": getattr(args, "status", None),
        "agent_type": getattr(args, "agent_type", None),
        "phase": getattr(args, "phase", None),
        "pipeline": getattr(args, "pipeline", None),
        "repo": getattr(args, "repo", None),
        "limit": args.limit,
    }
    text = args.text

    try:
        result = search_checkpoints(text, filters)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    matches_raw = result["matches"]
    if not matches_raw:
        _print_empty_result(
            checkpoint_repo,
            CHECKPOINT_BRANCH,
            args.json,
            message=f"No checkpoints found with transcript matching {text!r}",
        )
        return 0

    matches: list[tuple[CheckpointSummaryV2 | dict[str, Any], list[str]]] = [
        (m["summary"], m["snippets"]) for m in matches_raw
    ]
    _print_search_results(matches, text, args)
    return 0


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""

    # Shared parent parser for subcommands.  Uses argparse.SUPPRESS as default
    # so that values set by the *main* parser (before the subcommand name) are
    # not overwritten by subparser defaults.  This lets users place
    # --checkpoint-repo / --repo-path before OR after the subcommand.
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--repo-path",
        default=argparse.SUPPRESS,
        help="Repository path (defaults to EGG_REPO_PATH or cwd)",
    )
    common_parser.add_argument(
        "--checkpoint-repo",
        default=argparse.SUPPRESS,
        help="External checkpoint repo in 'owner/repo' format "
        "(overrides EGG_CHECKPOINT_REPO env var and repo_settings config)",
    )

    parser = argparse.ArgumentParser(
        prog="egg-checkpoint",
        description=(
            "CLI for browsing and querying agent checkpoints.\n\n"
            "Note: Reviewer agents may not produce checkpoints if session-end\n"
            "triggers don't fire (e.g., container eviction or OOM). Use reviewer\n"
            "results as a lower bound."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Main-parser copies use default=None so the namespace always has the attr.
    parser.add_argument(
        "--repo-path",
        help="Repository path (defaults to EGG_REPO_PATH or cwd)",
    )
    parser.add_argument(
        "--checkpoint-repo",
        help="External checkpoint repo in 'owner/repo' format "
        "(overrides EGG_CHECKPOINT_REPO env var and repo_settings config)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    _agent_type_help = (
        "Filter by agent type. Accepts base types (coder, tester, reviewer, …) "
        "and composite BRC reviewer roles (reviewer_code, reviewer_contract, etc.). "
        "Composite roles are post-filtered via session metadata and work only in "
        "the direct-git path; they collapse to 'reviewer' when queried via the "
        "gateway HTTP API. Note: reviewer agents may not produce checkpoints if "
        "session-end triggers don't fire."
    )

    # list command
    list_parser = subparsers.add_parser(
        "list", help="List checkpoints with metadata", parents=[common_parser]
    )
    list_parser.add_argument("--branch", help="Filter by branch name")
    list_parser.add_argument("--issue", type=int, help="Filter by issue number")
    list_parser.add_argument("--pr", type=int, help="Filter by PR number")
    list_parser.add_argument("--session", help="Filter by session ID")
    list_parser.add_argument(
        "--trigger",
        choices=[t.value for t in TriggerType],
        help="Filter by trigger type",
    )
    list_parser.add_argument(
        "--status",
        choices=[s.value for s in SessionStatus],
        help="Filter by session status",
    )
    list_parser.add_argument(
        "--agent-type",
        choices=_AGENT_TYPE_CHOICES,
        help=_agent_type_help,
    )
    list_parser.add_argument(
        "--phase",
        choices=["refine", "plan", "implement", "pr"],
        help="Filter by pipeline phase",
    )
    list_parser.add_argument("--pipeline", help="Filter by pipeline run ID")
    list_parser.add_argument("--repo", help="Filter by source repository (owner/repo format)")
    list_parser.add_argument("--limit", type=int, default=50, help="Maximum checkpoints to show")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")
    list_parser.set_defaults(func=cmd_list)

    # show command
    show_parser = subparsers.add_parser(
        "show", help="Display full checkpoint details", parents=[common_parser]
    )
    show_parser.add_argument("identifier", help="Checkpoint ID (ckpt-...) or commit SHA")
    show_parser.add_argument("--json", action="store_true", help="Output as JSON")
    show_parser.set_defaults(func=cmd_show)

    # browse command
    browse_parser = subparsers.add_parser(
        "browse", help="Filter checkpoints by issue", parents=[common_parser]
    )
    browse_parser.add_argument("--issue", type=int, required=True, help="Issue number to browse")
    browse_parser.add_argument("--repo", help="Filter by source repository (owner/repo format)")
    browse_parser.add_argument("--limit", type=int, default=100, help="Maximum checkpoints to show")
    browse_parser.add_argument("--json", action="store_true", help="Output as JSON")
    browse_parser.set_defaults(func=cmd_browse)

    # context command
    context_parser = subparsers.add_parser(
        "context",
        help="Show cross-agent context summary for a pipeline or issue",
        parents=[common_parser],
    )
    context_parser.add_argument("--pipeline", help="Filter by pipeline run ID")
    context_parser.add_argument("--issue", type=int, help="Filter by issue number")
    context_parser.add_argument(
        "--agent-type",
        choices=_AGENT_TYPE_CHOICES,
        help=_agent_type_help,
    )
    context_parser.add_argument(
        "--phase",
        choices=["refine", "plan", "implement", "pr"],
        help="Filter by pipeline phase",
    )
    context_parser.add_argument(
        "--files", action="store_true", help="Show file paths touched by each checkpoint"
    )
    context_parser.add_argument("--repo", help="Filter by source repository (owner/repo format)")
    context_parser.add_argument(
        "--limit", type=int, default=100, help="Maximum checkpoints to show"
    )
    context_parser.add_argument("--json", action="store_true", help="Output as JSON")
    context_parser.set_defaults(func=cmd_context)

    # cost command
    cost_parser = subparsers.add_parser(
        "cost",
        help="Show cost breakdown for a pipeline, issue, or PR",
        parents=[common_parser],
    )
    cost_parser.add_argument("--pipeline", help="Filter by pipeline run ID")
    cost_parser.add_argument("--issue", type=int, help="Filter by issue number")
    cost_parser.add_argument("--pr", type=int, help="Filter by PR number")
    cost_parser.add_argument("--limit", type=int, default=500, help="Maximum checkpoints to load")
    cost_parser.add_argument("--json", action="store_true", help="Output as JSON")
    cost_parser.set_defaults(func=cmd_cost)

    # search command
    search_parser = subparsers.add_parser(
        "search",
        help="Search checkpoint transcripts for matching text",
        parents=[common_parser],
    )
    search_parser.add_argument(
        "--text", required=True, help="Text to search for in transcripts (case-insensitive)"
    )
    search_parser.add_argument("--branch", help="Filter by branch name")
    search_parser.add_argument("--issue", type=int, help="Filter by issue number")
    search_parser.add_argument("--pr", type=int, help="Filter by PR number")
    search_parser.add_argument("--session", help="Filter by session ID")
    search_parser.add_argument(
        "--trigger",
        choices=[t.value for t in TriggerType],
        help="Filter by trigger type",
    )
    search_parser.add_argument(
        "--status",
        choices=[s.value for s in SessionStatus],
        help="Filter by session status",
    )
    search_parser.add_argument(
        "--agent-type",
        choices=_AGENT_TYPE_CHOICES,
        help=_agent_type_help,
    )
    search_parser.add_argument(
        "--phase",
        choices=["refine", "plan", "implement", "pr"],
        help="Filter by pipeline phase",
    )
    search_parser.add_argument("--pipeline", help="Filter by pipeline run ID")
    search_parser.add_argument("--repo", help="Filter by source repository (owner/repo format)")
    search_parser.add_argument(
        "--limit", type=int, default=20, help="Maximum checkpoints to search"
    )
    search_parser.add_argument("--json", action="store_true", help="Output as JSON")
    search_parser.set_defaults(func=cmd_search)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        result: int = args.func(args)
        return result
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
