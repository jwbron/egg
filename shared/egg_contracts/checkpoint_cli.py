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

Commands:
    egg-checkpoint list [--branch <branch>] [--issue <number>] [--limit <n>]
                        [--trigger <type>] [--status <status>] [--agent-type <type>]
                        [--session <id>] [--pr <number>] [--phase <phase>]
                                            List checkpoints with metadata
    egg-checkpoint show <id-or-commit>      Display full checkpoint details
    egg-checkpoint browse --issue <number>  Filter checkpoints by issue
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

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

# Checkpoint branch name
CHECKPOINT_BRANCH = "egg/checkpoints/v2"

# Validation pattern for checkpoint_repo values (must be "owner/repo" format)
_REPO_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")


def _validate_checkpoint_repo(checkpoint_repo: str) -> str:
    """Validate that checkpoint_repo matches 'owner/repo' format."""
    if not _REPO_PATTERN.match(checkpoint_repo):
        raise ValueError(
            f"Invalid checkpoint_repo format: {checkpoint_repo!r} "
            f"(expected 'owner/repo')"
        )
    return checkpoint_repo


def get_repo_path() -> str:
    """Get the repository path from environment or default."""
    return os.environ.get("EGG_REPO_PATH", str(Path.cwd()))


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


def ensure_checkpoint_ref(
    repo_path: str, checkpoint_repo: str | None = None
) -> str | None:
    """
    Ensure the checkpoint branch is fetched and return the git ref to read from.

    Args:
        repo_path: Path to the repository.
        checkpoint_repo: Optional "owner/repo" for an external checkpoint repo.

    Returns:
        A git ref string (e.g. "origin/egg/checkpoints/v2" or a resolved SHA),
        or None if the branch doesn't exist.
    """
    target = _resolve_checkpoint_target(repo_path, checkpoint_repo)

    # Check if branch exists
    result = run_git(
        ["ls-remote", "--heads", target, CHECKPOINT_BRANCH],
        cwd=repo_path,
        check=False,
    )
    if not result.stdout.strip():
        return None

    # Fetch the branch
    run_git(["fetch", target, CHECKPOINT_BRANCH], cwd=repo_path, check=False)

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


def load_checkpoint_from_ref(
    checkpoint_id: str, ref: str, repo_path: str
) -> CheckpointV2 | None:
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
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ts
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


def _get_checkpoint_repo_from_args(args: argparse.Namespace) -> str | None:
    """Get checkpoint_repo from CLI args or repo config."""
    checkpoint_repo = getattr(args, "checkpoint_repo", None)
    if checkpoint_repo:
        return checkpoint_repo
    # Try to auto-detect from repo config
    repo_path = args.repo_path or get_repo_path()
    try:
        from checkpoint_handler import _get_checkpoint_repo_for_path

        return _get_checkpoint_repo_for_path(repo_path)
    except ImportError:
        pass
    return None


def cmd_list(args: argparse.Namespace) -> int:
    """List checkpoints with metadata."""
    repo_path = args.repo_path or get_repo_path()
    checkpoint_repo = _get_checkpoint_repo_from_args(args)

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        print("No checkpoints found (checkpoint branch does not exist)")
        return 0

    index = load_index_from_ref(ref, repo_path)
    if not index:
        print("No checkpoints found")
        return 0

    summaries = filter_checkpoints_v2(
        index,
        issue_number=args.issue,
        pr_number=getattr(args, "pr", None),
        branch=args.branch,
        session_id=getattr(args, "session", None),
        trigger_type=getattr(args, "trigger", None),
        session_status=getattr(args, "status", None),
        agent_type=getattr(args, "agent_type", None),
        pipeline_phase=getattr(args, "phase", None),
        pipeline_id=getattr(args, "pipeline", None),
        repo=getattr(args, "repo", None),
        limit=args.limit,
    )

    if not summaries:
        print("No checkpoints found matching filters")
        return 0

    if args.json:
        output = [s.model_dump(mode="json") for s in summaries]
        print(json.dumps(output, indent=2))
    else:
        print(f"Checkpoints ({len(summaries)} found):")
        print()
        for s in summaries:
            print_checkpoint_summary(s)

    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Display full checkpoint details by checkpoint ID or commit SHA."""
    repo_path = args.repo_path or get_repo_path()
    identifier = args.identifier
    checkpoint_repo = _get_checkpoint_repo_from_args(args)

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        print("No checkpoints found (checkpoint branch does not exist)")
        return 1

    checkpoint: CheckpointV2 | None = None

    if identifier.startswith("ckpt-"):
        checkpoint = load_checkpoint_from_ref(identifier, ref, repo_path)
    else:
        # Look up commit SHA in the index
        index = load_index_from_ref(ref, repo_path)
        if index:
            checkpoint_id = index.get_by_commit(identifier)
            if checkpoint_id:
                checkpoint = load_checkpoint_from_ref(checkpoint_id, ref, repo_path)

    if not checkpoint:
        print(f"No checkpoint found for '{identifier}'")
        return 1

    if args.json:
        print(json.dumps(checkpoint.model_dump(mode="json"), indent=2))
    else:
        print_checkpoint_details(checkpoint)

    return 0


def cmd_browse(args: argparse.Namespace) -> int:
    """Filter checkpoints by issue number."""
    repo_path = args.repo_path or get_repo_path()
    checkpoint_repo = _get_checkpoint_repo_from_args(args)

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        print("No checkpoints found (checkpoint branch does not exist)")
        return 0

    index = load_index_from_ref(ref, repo_path)
    if not index:
        print("No checkpoints found")
        return 0

    summaries = filter_checkpoints_v2(
        index,
        issue_number=args.issue,
        repo=getattr(args, "repo", None),
        limit=args.limit,
    )

    if not summaries:
        print(f"No checkpoints found for issue #{args.issue}")
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
            agent = (
                first.agent_type.value if first.agent_type != AgentType.UNKNOWN else "unknown"
            )
            triggers = {_format_trigger(s.trigger_type) for s in session_summaries}
            print(
                f"Session: {sid[:12]}... (agent: {agent}, triggers: {', '.join(sorted(triggers))})"
            )
            for s in session_summaries:
                print_checkpoint_summary(s)
            print()

    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """Show cross-agent context summary for a pipeline or issue."""
    repo_path = args.repo_path or get_repo_path()
    checkpoint_repo = _get_checkpoint_repo_from_args(args)

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        print("No checkpoints found (checkpoint branch does not exist)")
        return 0

    index = load_index_from_ref(ref, repo_path)
    if not index:
        print("No checkpoints found")
        return 0

    summaries = filter_checkpoints_v2(
        index,
        pipeline_id=getattr(args, "pipeline", None),
        issue_number=getattr(args, "issue", None),
        agent_type=getattr(args, "agent_type", None),
        pipeline_phase=getattr(args, "phase", None),
        repo=getattr(args, "repo", None),
        limit=args.limit,
    )

    if not summaries:
        print("No checkpoints found matching filters")
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


def cmd_cost(args: argparse.Namespace) -> int:
    """Show cost breakdown for a pipeline, issue, or PR."""
    from .usage import TokenCounts

    repo_path = args.repo_path or get_repo_path()
    checkpoint_repo = _get_checkpoint_repo_from_args(args)

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        print("No checkpoints found (checkpoint branch does not exist)")
        return 0

    index = load_index_from_ref(ref, repo_path)
    if not index:
        print("No checkpoints found")
        return 0

    summaries = filter_checkpoints_v2(
        index,
        pipeline_id=args.pipeline,
        issue_number=args.issue,
        pr_number=args.pr,
        limit=args.limit,
    )

    if not summaries:
        print("No checkpoints found matching filters")
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

        rows.append({
            "phase": phase,
            "agent": agent,
            "input_tokens": tu.input_tokens,
            "output_tokens": tu.output_tokens,
            "cost": cost,
            "model": model,
        })

    if not rows:
        print("No checkpoints with token usage data found")
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
    print(
        f"  {'Phase':<12s}  {'Agent':<14s}  {'Input':>8s}  {'Output':>8s}  {'Cost':>8s}"
    )
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


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="egg-checkpoint",
        description="CLI for browsing and querying agent checkpoints",
    )
    parser.add_argument(
        "--repo-path",
        help="Repository path (defaults to EGG_REPO_PATH or cwd)",
    )
    parser.add_argument(
        "--checkpoint-repo",
        help="External checkpoint repo in 'owner/repo' format (overrides repo_settings config)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # list command
    list_parser = subparsers.add_parser("list", help="List checkpoints with metadata")
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
        choices=[a.value for a in AgentType],
        help="Filter by agent type",
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
    show_parser = subparsers.add_parser("show", help="Display full checkpoint details")
    show_parser.add_argument("identifier", help="Checkpoint ID (ckpt-...) or commit SHA")
    show_parser.add_argument("--json", action="store_true", help="Output as JSON")
    show_parser.set_defaults(func=cmd_show)

    # browse command
    browse_parser = subparsers.add_parser("browse", help="Filter checkpoints by issue")
    browse_parser.add_argument("--issue", type=int, required=True, help="Issue number to browse")
    browse_parser.add_argument("--repo", help="Filter by source repository (owner/repo format)")
    browse_parser.add_argument("--limit", type=int, default=100, help="Maximum checkpoints to show")
    browse_parser.add_argument("--json", action="store_true", help="Output as JSON")
    browse_parser.set_defaults(func=cmd_browse)

    # context command
    context_parser = subparsers.add_parser(
        "context", help="Show cross-agent context summary for a pipeline or issue"
    )
    context_parser.add_argument("--pipeline", help="Filter by pipeline run ID")
    context_parser.add_argument("--issue", type=int, help="Filter by issue number")
    context_parser.add_argument(
        "--agent-type",
        choices=[a.value for a in AgentType],
        help="Filter by agent type",
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
    context_parser.add_argument("--limit", type=int, default=100, help="Maximum checkpoints to show")
    context_parser.add_argument("--json", action="store_true", help="Output as JSON")
    context_parser.set_defaults(func=cmd_context)

    # cost command
    cost_parser = subparsers.add_parser(
        "cost", help="Show cost breakdown for a pipeline, issue, or PR"
    )
    cost_parser.add_argument("--pipeline", help="Filter by pipeline run ID")
    cost_parser.add_argument("--issue", type=int, help="Filter by issue number")
    cost_parser.add_argument("--pr", type=int, help="Filter by PR number")
    cost_parser.add_argument("--limit", type=int, default=500, help="Maximum checkpoints to load")
    cost_parser.add_argument("--json", action="store_true", help="Output as JSON")
    cost_parser.set_defaults(func=cmd_cost)

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
