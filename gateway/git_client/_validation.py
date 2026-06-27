"""Repo-path + git-argument validation logic.

Extracted verbatim from the pre-split ``gateway/git_client.py``
(#3312 slice-11). Policy tables live in ``_policy``; this module holds
the functions that consult them. AST-identical to the originals.
"""

import os
import re

from ._policy import (
    _CHECKOUT_FILE_FLAGS,
    ALLOWED_FLAG_VALUES,
    ALLOWED_REPO_PATHS,
    BLOCKED_GIT_FLAGS,
    FLAG_NORMALIZATION,
    GIT_ALLOWED_COMMANDS,
    REPOS_PARENT_DIRECTORIES,
)


def is_repos_parent_directory(path: str) -> bool:
    """
    Check if a path is a "repos parent" directory - a directory that contains
    repos but is not itself a git repository.

    Git operations like `rev-parse` are commonly run to detect if a directory
    is a repo. When run in these parent directories, they are expected to fail.
    This function helps identify such cases to avoid noisy warning logs.

    Args:
        path: The path to check

    Returns:
        True if the path is a repos parent directory (not an actual repo)
    """
    if not path:
        return False

    try:
        real_path = os.path.realpath(path).rstrip("/")
        return any(real_path == parent_dir.rstrip("/") for parent_dir in REPOS_PARENT_DIRECTORIES)
    except Exception:
        return False


def validate_repo_path(path: str) -> tuple[bool, str]:
    """
    Validate that repo_path is within allowed directories.

    Prevents path traversal attacks by ensuring the resolved path
    starts with an allowed prefix.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not path:
        return False, "repo_path is required"

    try:
        # Resolve to absolute path, following symlinks
        real_path = os.path.realpath(path)

        # Check if path is within allowed directories
        # Normalize paths for comparison (ensure trailing slash for prefix matching)
        for allowed in ALLOWED_REPO_PATHS:
            allowed_base = allowed.rstrip("/")
            # Allow exact match (e.g., /home/egg/repos) or subpath (e.g., /home/egg/repos/foo)
            if real_path == allowed_base or real_path.startswith(allowed_base + "/"):
                return True, ""

        return False, f"repo_path must be within allowed directories: {ALLOWED_REPO_PATHS}"
    except Exception as e:
        return False, f"Invalid repo_path: {e}"


def normalize_flag(flag: str, operation: str | None = None) -> str:
    """
    Normalize short flags to long form for consistent validation.

    Uses per-subcommand mappings so that the same short flag (e.g. ``-u``)
    normalizes to different long forms depending on the git subcommand.

    Args:
        flag: The flag to normalize (e.g., "-a" or "--all")
        operation: The git subcommand (e.g., "push", "stash").  When *None*,
            no normalization is applied (the flag is returned as-is).

    Returns:
        The normalized long-form flag, or original if not found
    """
    mapping = FLAG_NORMALIZATION.get(operation, {}) if operation else {}
    # Handle -X=value format
    if "=" in flag:
        base, value = flag.split("=", 1)
        normalized = mapping.get(base, base)
        return f"{normalized}={value}"
    # Handle combined short-flag+value form (e.g., -Xtheirs → --strategy-option=theirs)
    # Git allows single-char flags to have values appended without a space.
    # Only apply for flags that take restricted values (in ALLOWED_FLAG_VALUES),
    # not boolean flags like -f/-q/-v where appended text is always invalid.
    if len(flag) > 2 and flag[0] == "-" and flag[1] != "-" and flag[:2] in mapping:
        short_flag = flag[:2]
        value = flag[2:]
        normalized = mapping[short_flag]
        if normalized in ALLOWED_FLAG_VALUES:
            return f"{normalized}={value}"
    return mapping.get(flag, flag)


def validate_git_args(operation: str, args: list[str]) -> tuple[bool, str, list[str]]:
    """
    Validate git arguments against per-operation allowlist.

    Uses explicit allowlists instead of blocklists for better security.
    Unknown flags are rejected by default.

    Args:
        operation: The git operation (fetch, ls-remote, push)
        args: List of arguments to validate

    Returns:
        Tuple of (is_valid, error_message, normalized_args)
    """
    # Validate operation first (before checking args)
    op_config = GIT_ALLOWED_COMMANDS.get(operation)
    if not op_config:
        return False, f"Unknown operation: {operation}", []

    if not args:
        return True, "", []

    allowed_flags = set(op_config["allowed_flags"])
    normalized = []

    i = 0
    while i < len(args):
        arg = args[i]

        # Ensure arg is a string (not a nested structure)
        if not isinstance(arg, str):
            return False, f"Invalid argument type: {type(arg)}", []

        # Skip non-flag arguments (refs, branch names, etc.)
        if not arg.startswith("-"):
            normalized.append(arg)
            i += 1
            continue

        # Allow '--' separator (used to separate flags from pathspecs)
        if arg == "--":
            normalized.append(arg)
            i += 1
            continue

        # Handle numeric flags like -3, -10 (shorthand for --max-count=N)
        # These are valid for 'log', 'reflog', and 'format-patch' operations.
        # reflog is internally a log walker and accepts -<N> natively in real
        # git, so the boilerplate-burn argument from issue #2480 applies
        # symmetrically to both -n N and -<N> forms.
        if re.match(r"^-\d+$", arg):
            if operation in ("log", "reflog") and "--max-count" in allowed_flags:
                # Convert -N to --max-count=N for consistency
                normalized.append(f"--max-count={arg[1:]}")
                i += 1
                continue
            elif operation == "format-patch":
                # format-patch uses -N to specify number of commits
                normalized.append(arg)
                i += 1
                continue
            else:
                return (
                    False,
                    f"Numeric flag '{arg}' is not allowed for git {operation}",
                    [],
                )

        # Handle `-n N`, `-n=N`, `-nN` for git log/reflog (alias for
        # --max-count=N). Per `git log --help`, `-n` is the canonical short form
        # for limiting commit count, so agents naturally emit it; without this
        # special case they retry with `--max-count` and burn an audit-log
        # entry. Issue #2480. reflog is internally a log walker and shares the
        # same `-n` semantics. Scoped to these two because `-n` means very
        # different things elsewhere (--dry-run for push, --no-commit for
        # cherry-pick, --show-number for blame, --numbered for format-patch).
        if operation in ("log", "reflog") and "--max-count" in allowed_flags:
            n_match = re.match(r"^-n(?:=(\d+)|(\d+))?$", arg)
            if n_match:
                count = n_match.group(1) or n_match.group(2)
                if count is None:
                    # Bare `-n`; consume the next arg if it's a number.
                    if i + 1 < len(args) and re.match(r"^\d+$", args[i + 1]):
                        count = args[i + 1]
                        normalized.append(f"--max-count={count}")
                        i += 2
                        continue
                else:
                    normalized.append(f"--max-count={count}")
                    i += 1
                    continue

        # Normalize short flags to long form (per-subcommand)
        normalized_flag = normalize_flag(arg, operation)

        # Check for explicitly blocked flags first
        flag_base = normalized_flag.split("=")[0] if "=" in normalized_flag else normalized_flag
        for blocked in BLOCKED_GIT_FLAGS:
            if flag_base.lower() == blocked.lower():
                return False, f"Flag '{arg}' is not allowed for git {operation}", []

        # Check against allowlist
        if flag_base not in allowed_flags:
            return (
                False,
                f"Flag '{arg}' is not allowed for git {operation}. "
                f"Allowed flags: {', '.join(sorted(allowed_flags))}",
                [],
            )

        # Validate flag values for flags with restricted allowed values
        if flag_base in ALLOWED_FLAG_VALUES:
            allowed_values = ALLOWED_FLAG_VALUES[flag_base]
            if "=" in normalized_flag:
                # Value is inline: --strategy-option=theirs
                value = normalized_flag.split("=", 1)[1]
            elif i + 1 < len(args) and not args[i + 1].startswith("-"):
                # Value is the next argument: -X theirs
                # NOTE: This heuristic assumes allowed values never start with "-".
                # All current values (ours, theirs, patience, etc.) satisfy this.
                value = args[i + 1]
                i += 1  # consume the value argument
                normalized_flag = f"{flag_base}={value}"
            else:
                return (
                    False,
                    f"Flag '{arg}' requires a value. "
                    f"Allowed values: {', '.join(sorted(allowed_values))}",
                    [],
                )
            if value not in allowed_values:
                return (
                    False,
                    f"Value '{value}' is not allowed for {flag_base}. "
                    f"Allowed values: {', '.join(sorted(allowed_values))}",
                    [],
                )

        normalized.append(normalized_flag)
        i += 1

    return True, "", normalized


def is_branch_switching_checkout(args: list[str]) -> bool:
    """
    Determine if a ``git checkout`` invocation is switching branches.

    In worktree sessions, agents must stay on their assigned branch.
    ``git checkout`` is dual-purpose: it can switch branches *or* restore files.
    This function distinguishes the two so the gateway can block only the
    branch-switching form.

    Rules:
    - ``-b`` / ``-B`` / ``--track`` present → branch-related → **switch** (True)
    - ``--`` separator present → everything after is pathspecs → **file** (False)
    - ``--ours`` / ``--theirs`` / ``--merge`` present → merge conflict resolution
      → **file** (False)
    - Positional (non-flag) args exist without ``--`` → ambiguous, assume branch
      → **switch** (True)
    - No positional args and no branch flags → harmless no-op → **file** (False)

    Note: ``-t`` is normalized to ``--track`` for checkout via
    FLAG_NORMALIZATION, so this function sees ``--track`` (handled above).

    Note: ``--detach`` is not currently in checkout's allowed_flags. If it is
    ever added, this function would need to handle it (it detaches HEAD at a
    ref without creating a branch, so it should be treated as branch-switching).

    Args:
        args: The validated/normalized argument list for ``git checkout``.

    Returns:
        True if the command would switch branches; False if it is a file operation.
    """
    has_branch_flag = False
    has_double_dash = False
    has_file_flag = False
    positional_args: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]

        if arg == "--":
            has_double_dash = True
            break  # Everything after -- is pathspecs

        if arg.startswith("-"):
            # Detect branch-creation/switching flags
            if arg in ("-b", "-B", "--track"):
                has_branch_flag = True
            # Detect file-operation flags
            if arg.split("=")[0] in _CHECKOUT_FILE_FLAGS:
                has_file_flag = True
        else:
            positional_args.append(arg)

        i += 1

    # Explicit branch creation/switching
    if has_branch_flag:
        return True

    # Explicit file operation (-- separator or merge conflict flags)
    if has_double_dash or has_file_flag:
        return False

    # Positional args without -- could be a branch name
    if positional_args:
        return True

    # No positional args, no branch flags — bare `git checkout` is a no-op
    return False


def is_branch_switching_operation(operation: str, args: list[str]) -> bool:
    """
    Check if a git operation would change the active branch.

    Used by the gateway to enforce branch isolation in worktree sessions.
    Agents in worktrees must stay on their assigned branch; they should use
    ``git restore`` for file operations instead of ``git checkout``.

    Args:
        operation: The git sub-command (e.g., "checkout", "switch").
        args: The validated/normalized argument list.

    Returns:
        True if the operation would switch or create a branch.
    """
    if operation == "switch":
        # git switch is always branch-related — no file-restore form.
        # Bare `git switch` with no args just prints the current branch
        # (similar to `git branch --show-current`), so treat it as a no-op.
        if not args:
            return False
        return True

    if operation == "checkout":
        return is_branch_switching_checkout(args)

    return False
