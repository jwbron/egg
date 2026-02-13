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
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .checkpoint_loader import (
    list_checkpoints_v2,
    load_checkpoint_by_commit_v2,
    load_checkpoint_by_id_v2,
)
from .checkpoints import (
    AgentType,
    CheckpointSummaryV2,
    CheckpointV2,
    SessionStatus,
    TriggerType,
)

# Checkpoint branch name
CHECKPOINT_BRANCH = "egg/checkpoints/v2"


def get_repo_path() -> str:
    """Get the repository path from environment or default."""
    return os.environ.get("EGG_REPO_PATH", str(Path.cwd()))


def run_git(
    args: list[str], cwd: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a git command."""
    cmd = ["git"] + args
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


def checkout_checkpoint_branch(repo_path: str) -> Path | None:
    """
    Checkout the checkpoint branch to a temporary directory.

    Returns the path to the checkout, or None if branch doesn't exist.
    """
    # Check if branch exists
    result = run_git(
        ["ls-remote", "--heads", "origin", CHECKPOINT_BRANCH],
        cwd=repo_path,
        check=False,
    )
    if not result.stdout.strip():
        return None

    # Fetch the branch
    run_git(["fetch", "origin", CHECKPOINT_BRANCH], cwd=repo_path, check=False)

    # Create temp directory and checkout
    temp_dir = tempfile.mkdtemp(prefix="checkpoint_browse_")
    temp_path = Path(temp_dir)

    try:
        run_git(
            ["worktree", "add", "--detach", str(temp_path), f"origin/{CHECKPOINT_BRANCH}"],
            cwd=repo_path,
        )
        return temp_path
    except Exception:
        # Cleanup on failure
        if temp_path.exists():
            import shutil

            shutil.rmtree(temp_path, ignore_errors=True)
        return None


def cleanup_worktree(repo_path: str, worktree_path: Path) -> None:
    """Clean up a worktree."""
    run_git(
        ["worktree", "remove", "--force", str(worktree_path)],
        cwd=repo_path,
        check=False,
    )
    # Also try to remove the directory if worktree remove failed
    if worktree_path.exists():
        import shutil

        shutil.rmtree(worktree_path, ignore_errors=True)


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


def cmd_list(args: argparse.Namespace) -> int:
    """List checkpoints with metadata."""
    repo_path = args.repo_path or get_repo_path()

    # Checkout checkpoint branch
    worktree_path = checkout_checkpoint_branch(repo_path)
    if not worktree_path:
        print("No checkpoints found (checkpoint branch does not exist)")
        return 0

    try:
        checkpoints_dir = worktree_path / "checkpoints"
        index_path = worktree_path / "index.json"

        summaries = list_checkpoints_v2(
            checkpoints_dir,
            index_path,
            issue_number=args.issue,
            pr_number=getattr(args, "pr", None),
            branch=args.branch,
            session_id=getattr(args, "session", None),
            trigger_type=getattr(args, "trigger", None),
            session_status=getattr(args, "status", None),
            agent_type=getattr(args, "agent_type", None),
            pipeline_phase=getattr(args, "phase", None),
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

    finally:
        cleanup_worktree(repo_path, worktree_path)


def cmd_show(args: argparse.Namespace) -> int:
    """Display full checkpoint details by checkpoint ID or commit SHA."""
    repo_path = args.repo_path or get_repo_path()
    identifier = args.identifier

    # Checkout checkpoint branch
    worktree_path = checkout_checkpoint_branch(repo_path)
    if not worktree_path:
        print("No checkpoints found (checkpoint branch does not exist)")
        return 1

    try:
        checkpoints_dir = worktree_path / "checkpoints"
        index_path = worktree_path / "index.json"

        checkpoint: CheckpointV2 | None = None

        # Try as checkpoint ID first (ckpt-... prefix)
        if identifier.startswith("ckpt-"):
            checkpoint = load_checkpoint_by_id_v2(identifier, checkpoints_dir)
        else:
            # Try as commit SHA
            checkpoint = load_checkpoint_by_commit_v2(identifier, checkpoints_dir, index_path)

        if not checkpoint:
            print(f"No checkpoint found for '{identifier}'")
            return 1

        if args.json:
            print(json.dumps(checkpoint.model_dump(mode="json"), indent=2))
        else:
            print_checkpoint_details(checkpoint)

        return 0

    finally:
        cleanup_worktree(repo_path, worktree_path)


def cmd_browse(args: argparse.Namespace) -> int:
    """Filter checkpoints by issue number."""
    repo_path = args.repo_path or get_repo_path()

    # Checkout checkpoint branch
    worktree_path = checkout_checkpoint_branch(repo_path)
    if not worktree_path:
        print("No checkpoints found (checkpoint branch does not exist)")
        return 0

    try:
        checkpoints_dir = worktree_path / "checkpoints"
        index_path = worktree_path / "index.json"

        summaries = list_checkpoints_v2(
            checkpoints_dir,
            index_path,
            issue_number=args.issue,
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

    finally:
        cleanup_worktree(repo_path, worktree_path)


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
    browse_parser.add_argument("--limit", type=int, default=100, help="Maximum checkpoints to show")
    browse_parser.add_argument("--json", action="store_true", help="Output as JSON")
    browse_parser.set_defaults(func=cmd_browse)

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
