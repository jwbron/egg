"""Push subcommand for egg-orch CLI with scope-based file filtering."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from egg_restrictions.patterns import AgentFilePattern


def _matches_pattern(file_path: str, pattern: str) -> bool:
    """Check if a file path matches a glob-like pattern.

    Delegates to AgentFilePattern._matches_pattern to avoid duplicating
    logic (see shared/egg_restrictions/patterns.py).
    """
    return AgentFilePattern._matches_pattern(file_path, pattern)


def _matches_any_pattern(file_path: str, patterns: list[str]) -> bool:
    """Check if a file path matches any of the given glob patterns."""
    return any(_matches_pattern(file_path, p) for p in patterns)


def _filter_files(
    files: list[str],
    allowed: list[str],
    blocked: list[str],
    block_exempt: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Filter files by allowed/blocked patterns.

    A file is kept if it matches at least one allowed pattern AND does not
    match any blocked pattern (unless it matches a block_exempt pattern).
    Blocked patterns are checked first (security takes precedence),
    consistent with AgentFilePattern.can_write.

    Args:
        files: List of file paths to filter.
        allowed: Glob patterns for files the agent CAN write to.
        blocked: Glob patterns for files the agent CANNOT write to.
        block_exempt: Glob patterns that carve out exceptions from blocked
            patterns (e.g. functional .md files in agent-config/).

    Returns:
        Tuple of (kept_files, removed_files).
    """
    exempt = block_exempt or []
    kept: list[str] = []
    removed: list[str] = []
    for f in files:
        if _matches_any_pattern(f, blocked):
            # Blocked, but check if exempt
            if _matches_any_pattern(f, exempt):
                # Exempt from block — still needs to match allowed
                if _matches_any_pattern(f, allowed):
                    kept.append(f)
                else:
                    removed.append(f)
            else:
                removed.append(f)
        elif _matches_any_pattern(f, allowed):
            kept.append(f)
        else:
            removed.append(f)
    return kept, removed


def _run_git(*args: str, _recovery_hint: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result.

    Raises SystemExit with the git return code on failure.  When
    ``_recovery_hint`` is a commit SHA, a restore command is printed to
    stderr before exiting so the user can undo a partial rewrite.
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        if _recovery_hint:
            sys.stderr.write(
                f"\nTo restore your original commits: git reset --hard {_recovery_hint}\n"
            )
        sys.exit(result.returncode)
    return result


def _get_current_branch() -> str:
    """Return the current branch name."""
    result = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def _get_merge_base(branch: str) -> str:
    """Return the merge-base commit between the branch and its upstream.

    Falls back to HEAD~1 if no upstream tracking branch exists.
    """
    # Try to find the upstream tracking branch
    upstream_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"],
        capture_output=True,
        text=True,
    )
    if upstream_result.returncode == 0:
        upstream = upstream_result.stdout.strip()
        mb_result = subprocess.run(
            ["git", "merge-base", upstream, "HEAD"],
            capture_output=True,
            text=True,
        )
        if mb_result.returncode == 0:
            return mb_result.stdout.strip()

    # Fallback: try origin/<branch>
    mb_result = subprocess.run(
        ["git", "merge-base", f"origin/{branch}", "HEAD"],
        capture_output=True,
        text=True,
    )
    if mb_result.returncode == 0:
        return mb_result.stdout.strip()

    # Fallback: on work branches, try origin/$EGG_BRANCH (the assigned
    # pipeline branch) — the work branch won't exist on the remote but
    # the assigned branch will.
    assigned = os.environ.get("EGG_BRANCH", "").strip()
    if assigned and assigned != branch:
        mb_result = subprocess.run(
            ["git", "merge-base", f"origin/{assigned}", "HEAD"],
            capture_output=True,
            text=True,
        )
        if mb_result.returncode == 0:
            return mb_result.stdout.strip()

    # Last resort: single commit
    return "HEAD~1"


def _retarget_refspec(current_branch: str) -> str | None:
    """Return ``HEAD:<assigned>`` when the push must target the pipeline's assigned branch.

    Pipeline agents run on per-agent work branches (``egg/<pid>-<role>/work``)
    but the gateway locks the session to the pipeline's assigned branch
    (``egg/<pid>``).  When ``EGG_BRANCH`` is set and differs from
    *current_branch*, the push must use ``HEAD:<assigned>`` so the refspec
    target matches the gateway's push-target check.

    Returns ``None`` when no retargeting is needed.
    """
    assigned = os.environ.get("EGG_BRANCH", "").strip()
    if assigned and assigned != current_branch:
        return f"HEAD:{assigned}"
    return None


def _resolve_push_args(current_branch: str) -> list[str]:
    """Build ``git push`` args, retargeting to the assigned pipeline branch."""
    refspec = _retarget_refspec(current_branch)
    if refspec:
        return ["git", "push", "origin", refspec]
    return ["git", "push", "origin", current_branch]


def cmd_push(args: argparse.Namespace) -> None:
    """Handle the push subcommand."""

    # Without --scope-filter, just passthrough to git push, but retarget the
    # refspec to the assigned pipeline branch when running on a per-agent
    # work branch.
    if not args.scope_filter:
        refspec = _retarget_refspec(_get_current_branch())
        if refspec:
            result = subprocess.run(["git", "push", "origin", refspec], text=True)
        else:
            result = subprocess.run(["git", "push"], text=True)
        sys.exit(result.returncode)

    # --scope-filter requires EGG_AGENT_FILE_PATTERNS
    raw = os.environ.get("EGG_AGENT_FILE_PATTERNS")
    if not raw:
        print(
            "Error: EGG_AGENT_FILE_PATTERNS environment variable is not set. "
            "Cannot apply scope filter.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        patterns = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"Error: EGG_AGENT_FILE_PATTERNS is not valid JSON: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    allowed: list[str] = patterns.get("allowed", [])
    blocked: list[str] = patterns.get("blocked", [])
    block_exempt: list[str] = patterns.get("block_exempt", [])

    branch = _get_current_branch()

    # Determine the upstream tracking point.  We compare against the remote
    # tracking branch so we capture *all* unpushed commits, not just the last
    # one (addresses the multi-commit edge case).
    merge_base = _get_merge_base(branch)

    # Get list of files across all unpushed commits
    diff_result = _run_git("diff", "--name-only", merge_base, "HEAD")
    commit_files = [f for f in diff_result.stdout.strip().splitlines() if f]

    if not commit_files:
        print(
            "No changed files found in unpushed commits. Nothing to push.",
            file=sys.stderr,
        )
        sys.exit(1)

    kept, removed = _filter_files(commit_files, allowed, blocked, block_exempt)

    # All files filtered out
    if not kept:
        print(
            "All files in the unpushed commits are out of scope for your role. Nothing to push.",
            file=sys.stderr,
        )
        sys.exit(1)

    # If nothing was removed, push as-is — no rewriting needed.
    if not removed:
        result = subprocess.run(_resolve_push_args(branch), text=True)
        sys.exit(result.returncode)

    # Some files removed — inform the user and rewrite the commits.
    print("The following files are out of scope and will be excluded:")
    for f in removed:
        print(f"  - {f}")
    print()

    # Squash all unpushed commits into one, keeping only allowed files.
    # Save original HEAD so the user can recover if the rewrite fails midway.
    orig_head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    orig_head = orig_head_result.stdout.strip() if orig_head_result.returncode == 0 else None

    # Step 1: soft-reset to the merge base so we can re-stage selectively.
    _run_git("reset", "--soft", merge_base, _recovery_hint=orig_head)

    # Step 2: un-stage everything, then re-add only allowed files.
    _run_git("reset", "HEAD", "--", ".", _recovery_hint=orig_head)
    _run_git("add", "--", *kept, _recovery_hint=orig_head)

    # Step 3: verify we actually have something staged (safety check).
    staged_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        capture_output=True,
        text=True,
    )
    if staged_check.returncode == 0:
        # Nothing staged — this shouldn't happen since kept is non-empty,
        # but guard against it anyway.
        print(
            "Error: no changes remain after filtering. The commit would be empty.",
            file=sys.stderr,
        )
        if orig_head:
            sys.stderr.write(f"\nTo restore your original commits: git reset --hard {orig_head}\n")
        sys.exit(1)

    # Step 4: recommit with the original HEAD commit message.
    _run_git("commit", "-C", "ORIG_HEAD", _recovery_hint=orig_head)

    # Step 5: push.
    push_result = subprocess.run(
        _resolve_push_args(branch),
        text=True,
    )
    if push_result.returncode != 0 and orig_head:
        sys.stderr.write(
            f"\nPush failed after commit rewrite. To restore your original commits: "
            f"git reset --hard {orig_head}\n"
        )
    sys.exit(push_result.returncode)


def register_push_subcommand(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the push subcommand on the given subparsers."""
    push_parser = subparsers.add_parser(
        "push",
        help="Push the current branch, optionally filtering files by agent scope.",
    )
    push_parser.add_argument(
        "--scope-filter",
        action="store_true",
        default=False,
        help=(
            "Filter the commit to only include files allowed by "
            "EGG_AGENT_FILE_PATTERNS before pushing."
        ),
    )
    push_parser.set_defaults(func=cmd_push)
