"""Push subcommand for egg-orch CLI with scope-based file filtering."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys


def _matches_pattern(file_path: str, pattern: str) -> bool:
    """Check if a file path matches a glob-like pattern.

    Mirrors the matching logic in AgentFilePattern._matches_pattern from
    shared/egg_restrictions/patterns.py.

    Supports:
    - Exact match: "foo/bar.py"
    - Prefix match: "foo/" (matches any file under foo/)
    - Wildcard: "*.py" (matches files ending in .py)
    - Double wildcard: "**/*.py" (matches .py files at any depth)
    """
    # Normalize both paths
    file_path = file_path.lstrip("./")
    pattern = pattern.lstrip("./")

    # Prefix match (directory pattern)
    if pattern.endswith("/"):
        return file_path.startswith(pattern) or file_path + "/" == pattern

    # Handle ** patterns for recursive matching
    if "**" in pattern:
        parts = pattern.split("**")
        if len(parts) == 2:
            prefix, suffix = parts
            prefix_match = not prefix or file_path.startswith(prefix.rstrip("/"))
            suffix = suffix.lstrip("/")
            suffix_match = not suffix or fnmatch.fnmatch(
                file_path.split("/")[-1], suffix
            )
            if prefix_match and suffix_match:
                if suffix.startswith("*"):
                    return fnmatch.fnmatch(file_path, "*" + suffix)
                return True

    # Standard fnmatch for simple wildcards
    return fnmatch.fnmatch(file_path, pattern)


def _matches_any_pattern(file_path: str, patterns: list[str]) -> bool:
    """Check if a file path matches any of the given glob patterns."""
    return any(_matches_pattern(file_path, p) for p in patterns)


def _filter_files(
    files: list[str],
    allowed: list[str],
    blocked: list[str],
) -> tuple[list[str], list[str]]:
    """Filter files by allowed/blocked patterns.

    A file is kept if it matches at least one allowed pattern AND does not
    match any blocked pattern.  Blocked patterns are checked first (security
    takes precedence), consistent with AgentFilePattern.can_write.

    Returns:
        Tuple of (kept_files, removed_files).
    """
    kept: list[str] = []
    removed: list[str] = []
    for f in files:
        if _matches_any_pattern(f, blocked):
            removed.append(f)
        elif _matches_any_pattern(f, allowed):
            kept.append(f)
        else:
            removed.append(f)
    return kept, removed


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result.

    Raises SystemExit with the git return code on failure.
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    return result


def _get_current_branch() -> str:
    """Return the current branch name."""
    result = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def cmd_push(args) -> None:
    """Handle the push subcommand."""

    # Without --scope-filter, just passthrough to git push.
    if not args.scope_filter:
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

    # Get list of files in the current commit
    diff_result = _run_git("diff", "--name-only", "HEAD~1", "HEAD")
    commit_files = [f for f in diff_result.stdout.strip().splitlines() if f]

    if not commit_files:
        print(
            "No files found in the current commit. Nothing to push.",
            file=sys.stderr,
        )
        sys.exit(1)

    kept, removed = _filter_files(commit_files, allowed, blocked)

    # All files filtered out
    if not kept:
        print(
            "All files in the commit are out of scope for your role. "
            "Nothing to push.",
            file=sys.stderr,
        )
        sys.exit(1)

    # If nothing was removed, push as-is — no rewriting needed.
    if not removed:
        result = subprocess.run(["git", "push"], text=True)
        sys.exit(result.returncode)

    # Some files removed — inform the user and rewrite the commit.
    print("The following files are out of scope and will be excluded:")
    for f in removed:
        print(f"  - {f}")
    print()

    branch = _get_current_branch()

    # Step 1: soft-reset the last commit so we can re-stage selectively.
    _run_git("reset", "HEAD~1")

    # Step 2: stage only the allowed files.
    _run_git("add", "--", *kept)

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
            "Error: no changes remain after filtering. "
            "The commit would be empty.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Step 4: recommit with the original commit message.
    _run_git("commit", "-C", "ORIG_HEAD")

    # Step 5: push.
    push_result = subprocess.run(
        ["git", "push", "origin", branch],
        text=True,
    )
    sys.exit(push_result.returncode)


def register_push_subcommand(subparsers) -> None:
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
