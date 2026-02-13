#!/usr/bin/env python3
"""
Checkpoint CLI for browsing and querying agent checkpoints.

This CLI provides commands to list, show, and browse checkpoints that capture
agent session context. Each commit has exactly one checkpoint, enabling precise
traceability between code changes and agent sessions.

Checkpoints are stored in the egg/checkpoints/v1 branch and are captured
per-commit during git push operations. Transcript data is extracted from the
API proxy buffer, providing stable and format-independent capture.

Commands:
    egg-checkpoint list [--branch <branch>] [--issue <number>] [--limit <n>]
                                            List checkpoints with metadata
    egg-checkpoint show <commit-sha>        Display full checkpoint details
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
    list_checkpoints,
    load_checkpoint_by_commit,
)
from .checkpoints import Checkpoint

# Checkpoint branch name
CHECKPOINT_BRANCH = "egg/checkpoints/v1"


def get_repo_path() -> str:
    """Get the repository path from environment or default."""
    return os.environ.get("EGG_REPO_PATH", str(Path.cwd()))


def run_git(args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
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


def print_checkpoint_summary(checkpoint: Checkpoint | dict[str, Any]) -> None:
    """Print a one-line summary of a checkpoint."""
    if isinstance(checkpoint, Checkpoint):
        data = checkpoint.model_dump()
    else:
        data = checkpoint

    cp_id = data.get("id", "unknown")
    commit = data.get("commit_sha", "unknown")[:7]
    created = format_timestamp(data.get("created_at"))
    branch = data.get("branch", "")
    issue = data.get("issue_number")
    phase = data.get("pipeline_phase", "")

    session = data.get("session", {})
    role = session.get("agent_role", "")

    transcript = data.get("transcript", {})
    msg_count = transcript.get("message_count", 0) if transcript else 0

    tool_calls = data.get("tool_calls", [])
    tool_count = len(tool_calls)

    token_usage = data.get("token_usage", {})
    tokens = token_usage.get("total_tokens", 0) if token_usage else 0

    # Build summary line
    parts = [f"{cp_id}", f"commit:{commit}"]
    if branch:
        parts.append(f"branch:{branch}")
    if issue:
        parts.append(f"issue:#{issue}")
    if phase:
        parts.append(f"phase:{phase}")
    if role:
        parts.append(f"role:{role}")
    parts.append(f"msgs:{msg_count}")
    parts.append(f"tools:{tool_count}")
    parts.append(f"tokens:{format_tokens(tokens)}")
    parts.append(f"@{created}")

    print("  " + " | ".join(parts))


def print_checkpoint_details(checkpoint: Checkpoint | dict[str, Any]) -> None:
    """Print detailed checkpoint information."""
    if isinstance(checkpoint, Checkpoint):
        data = checkpoint.model_dump()
    else:
        data = checkpoint

    print(f"Checkpoint: {data.get('id')}")
    print(f"  Commit: {data.get('commit_sha')}")
    print(f"  Branch: {data.get('branch', 'N/A')}")
    print(f"  Created: {format_timestamp(data.get('created_at'))}")

    if data.get("issue_number"):
        print(f"  Issue: #{data.get('issue_number')}")
    if data.get("pipeline_phase"):
        print(f"  Phase: {data.get('pipeline_phase')}")

    # Session metadata
    print()
    print("Session:")
    session = data.get("session", {})
    print(f"  Session ID: {session.get('session_id', 'N/A')}")
    if session.get("container_id"):
        print(f"  Container: {session.get('container_id')}")
    if session.get("agent_role"):
        print(f"  Role: {session.get('agent_role')}")
    if session.get("model"):
        print(f"  Model: {session.get('model')}")
    print(f"  Started: {format_timestamp(session.get('started_at'))}")
    if session.get("ended_at"):
        print(f"  Ended: {format_timestamp(session.get('ended_at'))}")
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

        # Load checkpoints
        checkpoints = list_checkpoints(
            checkpoints_dir,
            issue_number=args.issue,
            branch=args.branch,
            limit=args.limit,
        )

        if not checkpoints:
            filters = []
            if args.issue:
                filters.append(f"issue #{args.issue}")
            if args.branch:
                filters.append(f"branch '{args.branch}'")
            filter_str = f" matching {', '.join(filters)}" if filters else ""
            print(f"No checkpoints found{filter_str}")
            return 0

        if args.json:
            output = [cp.model_dump(mode="json") for cp in checkpoints]
            print(json.dumps(output, indent=2))
        else:
            print(f"Checkpoints ({len(checkpoints)} found):")
            print()
            for cp in checkpoints:
                print_checkpoint_summary(cp)

        return 0

    finally:
        cleanup_worktree(repo_path, worktree_path)


def cmd_show(args: argparse.Namespace) -> int:
    """Display full checkpoint details for a commit."""
    repo_path = args.repo_path or get_repo_path()
    commit_sha = args.commit

    # Checkout checkpoint branch
    worktree_path = checkout_checkpoint_branch(repo_path)
    if not worktree_path:
        print("No checkpoints found (checkpoint branch does not exist)")
        return 1

    try:
        checkpoints_dir = worktree_path / "checkpoints"
        index_path = worktree_path / "index.json"

        # Load checkpoint by commit
        checkpoint = load_checkpoint_by_commit(
            commit_sha,
            checkpoints_dir,
            index_path,
        )

        if not checkpoint:
            print(f"No checkpoint found for commit {commit_sha}")
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

        # Load checkpoints for issue
        checkpoints = list_checkpoints(
            checkpoints_dir,
            issue_number=args.issue,
            limit=args.limit,
        )

        if not checkpoints:
            print(f"No checkpoints found for issue #{args.issue}")
            return 0

        if args.json:
            output = [cp.model_dump(mode="json") for cp in checkpoints]
            print(json.dumps(output, indent=2))
        else:
            print(f"Checkpoints for Issue #{args.issue} ({len(checkpoints)} found):")
            print()

            # Group by session
            sessions: dict[str, list[Checkpoint]] = {}
            for cp in checkpoints:
                session_id = cp.session.session_id
                if session_id not in sessions:
                    sessions[session_id] = []
                sessions[session_id].append(cp)

            for session_id, session_checkpoints in sessions.items():
                first = session_checkpoints[0]
                role = first.session.agent_role or "unknown"
                print(f"Session: {session_id[:12]}... (role: {role})")
                for cp in session_checkpoints:
                    print_checkpoint_summary(cp)
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
    list_parser.add_argument("--limit", type=int, default=50, help="Maximum checkpoints to show")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")
    list_parser.set_defaults(func=cmd_list)

    # show command
    show_parser = subparsers.add_parser("show", help="Display full checkpoint details")
    show_parser.add_argument("commit", help="Commit SHA to show checkpoint for")
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
