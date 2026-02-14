#!/usr/bin/env python3
"""
Usage CLI for querying token usage across sessions, issues, workflows, and PRs.

This CLI provides commands to query and display token usage aggregates that
are stored in the egg/checkpoints/v2 branch alongside checkpoint data.

Commands:
    egg-usage summary                           Show overall usage summary
    egg-usage --issue <number>                  Show usage for an issue
    egg-usage --session <id>                    Show usage for a session
    egg-usage --pr <number>                     Show usage for a PR
    egg-usage --workflow <id>                   Show usage for a workflow
    egg-usage backfill-pr <pr> [--issue <n>]    Backfill PR number to checkpoints
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .usage import (
    IssueUsage,
    PRUsage,
    SessionUsage,
    TokenCounts,
    UsageIndex,
    WorkflowUsage,
)
from .usage_loader import (
    backfill_pr_usage,
    get_usage_summary,
    query_usage_by_issue,
    query_usage_by_pr,
    query_usage_by_session,
    query_usage_by_workflow,
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
        return f"https://github.com/{checkpoint_repo}.git"
    return "origin"


def checkout_checkpoint_branch(
    repo_path: str, checkpoint_repo: str | None = None
) -> Path | None:
    """
    Checkout the checkpoint branch to a temporary directory.

    Args:
        repo_path: Path to the repository.
        checkpoint_repo: Optional "owner/repo" for an external checkpoint repo.
            When set, fetches checkpoints from this repo instead of origin.

    Returns the path to the checkout, or None if branch doesn't exist.
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

    # Create temp directory and checkout
    temp_dir = tempfile.mkdtemp(prefix="usage_browse_")
    temp_path = Path(temp_dir)

    # Determine the local ref to checkout from
    if checkpoint_repo:
        checkout_ref = "FETCH_HEAD"
    else:
        checkout_ref = f"origin/{CHECKPOINT_BRANCH}"

    try:
        run_git(
            [
                "worktree",
                "add",
                "--detach",
                str(temp_path),
                checkout_ref,
            ],
            cwd=repo_path,
        )
        return temp_path
    except Exception:
        # Cleanup on failure
        if temp_path.exists():
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
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_cost(cost: float) -> str:
    """Format cost for display."""
    if cost >= 1.0:
        return f"${cost:.2f}"
    if cost >= 0.01:
        return f"${cost:.3f}"
    return f"${cost:.4f}"


def print_token_counts(counts: TokenCounts, indent: str = "  ") -> None:
    """Print token counts in a formatted way."""
    print(f"{indent}Input tokens:  {format_tokens(counts.input_tokens)}")
    print(f"{indent}Output tokens: {format_tokens(counts.output_tokens)}")
    if counts.cache_read_tokens > 0:
        print(f"{indent}Cache read:    {format_tokens(counts.cache_read_tokens)}")
    if counts.cache_creation_tokens > 0:
        print(f"{indent}Cache write:   {format_tokens(counts.cache_creation_tokens)}")
    print(f"{indent}Total tokens:  {format_tokens(counts.total_tokens())}")


def print_usage_summary(index: UsageIndex) -> None:
    """Print the overall usage summary."""
    print("Usage Summary")
    print("=" * 50)
    print()
    print("Totals:")
    print(f"  Sessions:   {index.total_sessions}")
    print(f"  Issues:     {index.total_issues}")
    print(f"  PRs:        {index.total_prs}")
    print(f"  Workflows:  {index.total_workflows}")
    print()
    print("Token Usage:")
    print_token_counts(index.total_tokens)
    print()
    print(f"Estimated Cost: {format_cost(index.total_cost_usd)}")
    print()
    print(f"Last Updated: {format_timestamp(index.last_updated)}")


def print_session_usage(usage: SessionUsage) -> None:
    """Print session usage details."""
    print(f"Session Usage: {usage.session_id}")
    print("=" * 50)
    print()
    print("Session Info:")
    if usage.container_id:
        print(f"  Container:  {usage.container_id}")
    if usage.agent_role:
        print(f"  Role:       {usage.agent_role}")
    if usage.model:
        print(f"  Model:      {usage.model}")
    if usage.issue_number:
        print(f"  Issue:      #{usage.issue_number}")
    if usage.pr_number:
        print(f"  PR:         #{usage.pr_number}")
    print()
    print("Token Usage:")
    print_token_counts(usage.tokens)
    print()
    print(f"Estimated Cost: {format_cost(usage.estimated_cost_usd)}")
    print()
    print(f"Checkpoints: {usage.checkpoint_count}")
    if usage.first_checkpoint_at:
        print(f"  First: {format_timestamp(usage.first_checkpoint_at)}")
    if usage.last_checkpoint_at:
        print(f"  Last:  {format_timestamp(usage.last_checkpoint_at)}")
    print()
    print(f"Last Updated: {format_timestamp(usage.last_updated)}")


def print_issue_usage(usage: IssueUsage) -> None:
    """Print issue usage details."""
    print(f"Issue Usage: #{usage.issue_number}")
    print("=" * 50)
    print()
    print("Issue Info:")
    if usage.pr_number:
        print(f"  PR:       #{usage.pr_number}")
    if usage.branch:
        print(f"  Branch:   {usage.branch}")
    if usage.pipeline_phases:
        print(f"  Phases:   {', '.join(usage.pipeline_phases)}")
    print(f"  Sessions: {len(usage.session_ids)}")
    print()
    print("Token Usage:")
    print_token_counts(usage.tokens)
    print()
    print(f"Estimated Cost: {format_cost(usage.estimated_cost_usd)}")
    print()
    print(f"Checkpoints: {usage.checkpoint_count}")
    if usage.first_checkpoint_at:
        print(f"  First: {format_timestamp(usage.first_checkpoint_at)}")
    if usage.last_checkpoint_at:
        print(f"  Last:  {format_timestamp(usage.last_checkpoint_at)}")
    print()
    print(f"Last Updated: {format_timestamp(usage.last_updated)}")


def print_pr_usage(usage: PRUsage) -> None:
    """Print PR usage details."""
    print(f"PR Usage: #{usage.pr_number}")
    print("=" * 50)
    print()
    print("PR Info:")
    if usage.issue_number:
        print(f"  Issue:      #{usage.issue_number}")
    if usage.branch:
        print(f"  Branch:     {usage.branch}")
    if usage.base_branch:
        print(f"  Base:       {usage.base_branch}")
    if usage.pipeline_phases:
        print(f"  Phases:     {', '.join(usage.pipeline_phases)}")
    print(f"  Sessions:   {len(usage.session_ids)}")
    print()
    print("Token Usage:")
    print_token_counts(usage.tokens)
    print()
    print(f"Estimated Cost: {format_cost(usage.estimated_cost_usd)}")
    print()
    print(f"Checkpoints: {usage.checkpoint_count}")
    if usage.first_checkpoint_at:
        print(f"  First: {format_timestamp(usage.first_checkpoint_at)}")
    if usage.last_checkpoint_at:
        print(f"  Last:  {format_timestamp(usage.last_checkpoint_at)}")
    print()
    print(f"Last Updated: {format_timestamp(usage.last_updated)}")


def print_workflow_usage(usage: WorkflowUsage) -> None:
    """Print workflow usage details."""
    print(f"Workflow Usage: {usage.workflow_id}")
    print("=" * 50)
    print()
    print("Workflow Info:")
    if usage.workflow_name:
        print(f"  Name:     {usage.workflow_name}")
    if usage.job_name:
        print(f"  Job:      {usage.job_name}")
    if usage.trigger_event:
        print(f"  Trigger:  {usage.trigger_event}")
    if usage.issue_number:
        print(f"  Issue:    #{usage.issue_number}")
    if usage.pr_number:
        print(f"  PR:       #{usage.pr_number}")
    print(f"  Sessions: {len(usage.session_ids)}")
    print()
    print("Token Usage:")
    print_token_counts(usage.tokens)
    print()
    print(f"Estimated Cost: {format_cost(usage.estimated_cost_usd)}")
    print()
    print(f"Checkpoints: {usage.checkpoint_count}")
    if usage.first_checkpoint_at:
        print(f"  First: {format_timestamp(usage.first_checkpoint_at)}")
    if usage.last_checkpoint_at:
        print(f"  Last:  {format_timestamp(usage.last_checkpoint_at)}")
    print()
    print(f"Last Updated: {format_timestamp(usage.last_updated)}")


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


def cmd_summary(args: argparse.Namespace) -> int:
    """Show overall usage summary."""
    repo_path = args.repo_path or get_repo_path()

    # Checkout checkpoint branch
    checkpoint_repo = _get_checkpoint_repo_from_args(args)
    worktree_path = checkout_checkpoint_branch(repo_path, checkpoint_repo=checkpoint_repo)
    if not worktree_path:
        print("No usage data found (checkpoint branch does not exist)")
        return 0

    try:
        index = get_usage_summary(worktree_path)

        if args.json:
            print(json.dumps(index.model_dump(mode="json"), indent=2))
        else:
            print_usage_summary(index)

        return 0

    finally:
        cleanup_worktree(repo_path, worktree_path)


def cmd_issue(args: argparse.Namespace) -> int:
    """Show usage for an issue."""
    repo_path = args.repo_path or get_repo_path()
    issue_number = args.issue

    # Checkout checkpoint branch
    checkpoint_repo = _get_checkpoint_repo_from_args(args)
    worktree_path = checkout_checkpoint_branch(repo_path, checkpoint_repo=checkpoint_repo)
    if not worktree_path:
        print("No usage data found (checkpoint branch does not exist)")
        return 1

    try:
        usage = query_usage_by_issue(worktree_path, issue_number)

        if not usage:
            print(f"No usage data found for issue #{issue_number}")
            return 1

        if args.json:
            print(json.dumps(usage.model_dump(mode="json"), indent=2))
        else:
            print_issue_usage(usage)

        return 0

    finally:
        cleanup_worktree(repo_path, worktree_path)


def cmd_session(args: argparse.Namespace) -> int:
    """Show usage for a session."""
    repo_path = args.repo_path or get_repo_path()
    session_id = args.session

    # Checkout checkpoint branch
    checkpoint_repo = _get_checkpoint_repo_from_args(args)
    worktree_path = checkout_checkpoint_branch(repo_path, checkpoint_repo=checkpoint_repo)
    if not worktree_path:
        print("No usage data found (checkpoint branch does not exist)")
        return 1

    try:
        usage = query_usage_by_session(worktree_path, session_id)

        if not usage:
            print(f"No usage data found for session {session_id}")
            return 1

        if args.json:
            print(json.dumps(usage.model_dump(mode="json"), indent=2))
        else:
            print_session_usage(usage)

        return 0

    finally:
        cleanup_worktree(repo_path, worktree_path)


def cmd_pr(args: argparse.Namespace) -> int:
    """Show usage for a PR."""
    repo_path = args.repo_path or get_repo_path()
    pr_number = args.pr

    # Checkout checkpoint branch
    checkpoint_repo = _get_checkpoint_repo_from_args(args)
    worktree_path = checkout_checkpoint_branch(repo_path, checkpoint_repo=checkpoint_repo)
    if not worktree_path:
        print("No usage data found (checkpoint branch does not exist)")
        return 1

    try:
        usage = query_usage_by_pr(worktree_path, pr_number)

        if not usage:
            print(f"No usage data found for PR #{pr_number}")
            return 1

        if args.json:
            print(json.dumps(usage.model_dump(mode="json"), indent=2))
        else:
            print_pr_usage(usage)

        return 0

    finally:
        cleanup_worktree(repo_path, worktree_path)


def cmd_workflow(args: argparse.Namespace) -> int:
    """Show usage for a workflow."""
    repo_path = args.repo_path or get_repo_path()
    workflow_id = args.workflow

    # Checkout checkpoint branch
    checkpoint_repo = _get_checkpoint_repo_from_args(args)
    worktree_path = checkout_checkpoint_branch(repo_path, checkpoint_repo=checkpoint_repo)
    if not worktree_path:
        print("No usage data found (checkpoint branch does not exist)")
        return 1

    try:
        usage = query_usage_by_workflow(worktree_path, workflow_id)

        if not usage:
            print(f"No usage data found for workflow {workflow_id}")
            return 1

        if args.json:
            print(json.dumps(usage.model_dump(mode="json"), indent=2))
        else:
            print_workflow_usage(usage)

        return 0

    finally:
        cleanup_worktree(repo_path, worktree_path)


def cmd_backfill_pr(args: argparse.Namespace) -> int:
    """Backfill PR number to existing checkpoints."""
    repo_path = args.repo_path or get_repo_path()
    pr_number = args.pr_number
    issue_number = args.issue
    branch = args.branch

    # Checkout checkpoint branch
    checkpoint_repo = _get_checkpoint_repo_from_args(args)
    worktree_path = checkout_checkpoint_branch(repo_path, checkpoint_repo=checkpoint_repo)
    if not worktree_path:
        print("No checkpoint branch found - nothing to backfill")
        return 1

    push_target = _resolve_checkpoint_target(repo_path, checkpoint_repo)

    try:
        updated = backfill_pr_usage(
            worktree_path, pr_number, issue_number=issue_number, branch=branch
        )

        if args.json:
            print(json.dumps({"pr_number": pr_number, "sessions_updated": updated}))
        else:
            print(f"Backfilled PR #{pr_number}")
            print(f"  Sessions updated: {updated}")

        # Push changes back to checkpoint branch
        if updated > 0:
            try:
                run_git(["add", "-A"], cwd=str(worktree_path))
                run_git(
                    ["commit", "--no-verify", "-m", f"Backfill PR #{pr_number} to usage data"],
                    cwd=str(worktree_path),
                    check=False,
                )
                run_git(
                    ["push", push_target, f"HEAD:{CHECKPOINT_BRANCH}"],
                    cwd=str(worktree_path),
                )
                print("  Changes pushed to checkpoint branch")
            except Exception as e:
                print(f"  Warning: Failed to push changes: {e}", file=sys.stderr)

        return 0

    finally:
        cleanup_worktree(repo_path, worktree_path)


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="egg-usage",
        description="CLI for querying token usage across sessions, issues, workflows, and PRs",
    )
    parser.add_argument(
        "--repo-path",
        help="Repository path (defaults to EGG_REPO_PATH or cwd)",
    )
    parser.add_argument(
        "--checkpoint-repo",
        help="External checkpoint repo in 'owner/repo' format (overrides repo_settings config)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # summary command
    summary_parser = subparsers.add_parser("summary", help="Show overall usage summary")
    summary_parser.set_defaults(func=cmd_summary)

    # issue command
    issue_parser = subparsers.add_parser("issue", help="Show usage for an issue")
    issue_parser.add_argument("issue", type=int, help="Issue number")
    issue_parser.set_defaults(func=cmd_issue)

    # session command
    session_parser = subparsers.add_parser("session", help="Show usage for a session")
    session_parser.add_argument("session", help="Session ID")
    session_parser.set_defaults(func=cmd_session)

    # pr command
    pr_parser = subparsers.add_parser("pr", help="Show usage for a PR")
    pr_parser.add_argument("pr", type=int, help="PR number")
    pr_parser.set_defaults(func=cmd_pr)

    # workflow command
    workflow_parser = subparsers.add_parser("workflow", help="Show usage for a workflow")
    workflow_parser.add_argument("workflow", help="Workflow ID")
    workflow_parser.set_defaults(func=cmd_workflow)

    # backfill-pr command
    backfill_parser = subparsers.add_parser(
        "backfill-pr", help="Backfill PR number to existing checkpoints"
    )
    backfill_parser.add_argument("pr_number", type=int, help="PR number to backfill")
    backfill_parser.add_argument(
        "--issue", type=int, help="Associated issue number (helps find checkpoints)"
    )
    backfill_parser.add_argument("--branch", help="Branch name (helps find checkpoints)")
    backfill_parser.set_defaults(func=cmd_backfill_pr)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        # Default to summary
        args.func = cmd_summary

    try:
        result: int = args.func(args)
        return result
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
