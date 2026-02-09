"""
Git Client - Wraps git CLI with validation and credential management.

Provides:
- Path validation (prevent traversal attacks)
- Argument validation with per-operation allowlists
- Credential helper management for authenticated git operations
"""

import contextlib
import os
import posixpath
import re
import sys
import tempfile
from pathlib import Path

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

# Import repo_config for auth mode support
_config_path = Path(__file__).parent.parent / "config"
if _config_path.exists() and str(_config_path) not in sys.path:
    sys.path.insert(0, str(_config_path))
from repo_config import get_auth_mode

# Import using try/except for both module and standalone script mode
try:
    from .github_client import get_github_client
except ImportError:
    from github_client import get_github_client  # type: ignore[no-redef, import-not-found]


logger = get_logger("gateway.git-client")

GIT_CLI = "/usr/bin/git"


def git_cmd(*args: str) -> list[str]:
    """
    Build a git command with security and ownership configurations.

    Configurations applied:
    - safe.directory=*: Allow operating on worktree paths. The gateway runs on the
      host but operates on paths inside egg container worktrees (e.g.,
      ~/.egg-worktrees/<container-id>/repo). Git's ownership check would reject
      these as "dubious ownership" without safe.directory=*.
    - core.hooksPath=/dev/null: SECURITY - Disable all git hooks. Git hooks are
      scripts in .git/hooks/ that execute automatically during certain operations.
      A malicious repo could include hooks that execute arbitrary code on the gateway
      (outside the sandbox). By pointing hooksPath to /dev/null (not a directory),
      git will never find any hooks to execute. See issue #58.
    """
    return [GIT_CLI, "-c", "safe.directory=*", "-c", "core.hooksPath=/dev/null", *args]


def ssh_url_to_https(url: str) -> str:
    """
    Convert SSH git URL to HTTPS URL.

    The gateway doesn't have SSH keys - it uses HTTPS with token auth.
    This converts SSH URLs so pushes work via HTTPS authentication.

    Supports:
    - git@github.com:owner/repo.git -> https://github.com/owner/repo.git
    - ssh://git@github.com/owner/repo.git -> https://github.com/owner/repo.git

    Returns the original URL if it's already HTTPS or doesn't match SSH patterns.
    """
    import re

    # Pattern 1: git@github.com:owner/repo.git
    match = re.match(r"^git@github\.com:(.+?)(?:\.git)?$", url)
    if match:
        return f"https://github.com/{match.group(1)}.git"

    # Pattern 2: ssh://git@github.com/owner/repo.git
    match = re.match(r"^ssh://git@github\.com/(.+?)(?:\.git)?$", url)
    if match:
        return f"https://github.com/{match.group(1)}.git"

    # Already HTTPS or unknown format - return as-is
    return url


def is_ssh_url(url: str) -> bool:
    """Check if a URL is an SSH git URL."""
    return url.startswith(("git@", "ssh://"))


def get_authenticated_remote_target(remote: str, remote_url: str) -> str:
    """
    Get the target to use for an authenticated git remote operation.

    The gateway uses HTTPS with token authentication via a credential helper.
    SSH URLs won't work with the credential helper, so they must be converted
    to HTTPS.

    Args:
        remote: The remote name (e.g., "origin")
        remote_url: The actual URL of the remote

    Returns:
        The HTTPS URL if the remote uses SSH, otherwise the remote name.
        Using the HTTPS URL directly ensures the credential helper is invoked.
    """
    if is_ssh_url(remote_url):
        return ssh_url_to_https(remote_url)
    return remote


# =============================================================================
# Path Validation
# =============================================================================

# Allowed base paths for repo_path validation
# These are the only directories where git operations are permitted
ALLOWED_REPO_PATHS = [
    "/home/egg/repos/",
    "/home/egg/.egg-worktrees/",
    "/repos/",  # Legacy path
]

# Directories that contain repos but are NOT repos themselves
# Git operations in these directories are expected to fail
REPOS_PARENT_DIRECTORIES = [
    "/home/egg/repos",
    "/home/egg/.egg-worktrees",
    "/repos",
]


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


# =============================================================================
# Argument Validation
# =============================================================================

# Explicitly dangerous git flags - never allowed regardless of operation
# These could be used for command injection or config override attacks
BLOCKED_GIT_FLAGS = [
    "--upload-pack",  # Can specify arbitrary command
    "--exec",  # Can specify arbitrary command
    "-u",  # Short for --upload-pack (blocked here, but -u for --set-upstream is normalized)
    "-c",  # Config override (could disable security)
    "--config",  # Config override
    "--receive-pack",  # Arbitrary command execution
]

# Per-operation allowlist of flags that are permitted
# This is more secure than a blocklist - unknown flags are rejected by default
GIT_ALLOWED_COMMANDS = {
    # === Network operations (require authentication) ===
    "fetch": {
        "allowed_flags": [
            "--all",
            "--tags",
            "--prune",
            "--depth",
            "--shallow-since",
            "--shallow-exclude",
            "--jobs",
            "--no-tags",
            "--force",
            "--verbose",
            "--quiet",
            "--dry-run",
            "--recurse-submodules",
            "--progress",
            "--no-progress",
        ],
    },
    "ls-remote": {
        "allowed_flags": [
            "--heads",
            "--tags",
            "--refs",
            "--quiet",
            "--exit-code",
            "--get-url",
            "--sort",
            "--symref",
        ],
    },
    "push": {
        "allowed_flags": [
            "--force",
            "--force-with-lease",
            "--tags",
            "--delete",
            "--set-upstream",
            "--verbose",
            "--quiet",
            "--dry-run",
            "--no-verify",
        ],
    },
    # === Local read operations ===
    "status": {
        "allowed_flags": [
            "--porcelain",
            "--short",
            "--branch",
            "--show-stash",
            "--long",
            "--verbose",
            "--untracked-files",
            "--ignored",
            "--no-ahead-behind",
            "-sb",
            "-s",
        ],
    },
    "log": {
        "allowed_flags": [
            "--oneline",
            "--graph",
            "--all",
            "--decorate",
            "--stat",
            "--name-only",
            "--name-status",
            "--format",
            "--pretty",
            "--abbrev-commit",
            "--no-merges",
            "--merges",
            "--first-parent",
            "--reverse",
            "--max-count",
            # Note: -n is NOT included because FLAG_NORMALIZATION maps -n → --dry-run globally.
            # Users should use --max-count=N or -3 (numeric shorthand) instead.
            "--since",
            "--until",
            "--author",
            "--grep",
            "--follow",
            "--diff-filter",
        ],
    },
    "diff": {
        "allowed_flags": [
            "--cached",
            "--staged",
            "--stat",
            "--numstat",
            "--shortstat",
            "--name-only",
            "--name-status",
            "--diff-filter",
            "--color",
            "--no-color",
            "--word-diff",
            "--ignore-space-change",
            "--ignore-all-space",
            "--ignore-blank-lines",
            "--no-index",
            "--unified",
            "-U",
        ],
    },
    "show": {
        "allowed_flags": [
            "--stat",
            "--name-only",
            "--name-status",
            "--format",
            "--pretty",
            "--abbrev-commit",
            "--no-patch",
            "-s",
        ],
    },
    "branch": {
        "allowed_flags": [
            "--list",
            "--all",
            "--remotes",
            "--verbose",
            "--merged",
            "--no-merged",
            "--contains",
            "--sort",
            "--format",
            "--show-current",
            "-a",
            "-r",
            "-v",
            "-vv",
        ],
    },
    "rev-parse": {
        "allowed_flags": [
            "--abbrev-ref",
            "--short",
            "--verify",
            "--symbolic-full-name",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
            "--is-inside-work-tree",
            "--is-bare-repository",
        ],
    },
    "ls-tree": {
        "allowed_flags": [
            "--name-only",
            "--name-status",
            "--full-name",
            "--full-tree",
            "--long",
            "-r",
            "-t",
            "-d",
            "-l",
        ],
    },
    "remote": {
        "allowed_flags": [
            "--verbose",
            "-v",
        ],
    },
    "worktree": {
        "allowed_flags": [
            "--porcelain",
            "--verbose",
            "-v",
        ],
    },
    "ls-files": {
        "allowed_flags": [
            "--cached",
            "--deleted",
            "--modified",
            "--others",
            "--ignored",
            "--stage",
            "--unmerged",
            "--killed",
            "--full-name",
            "--error-unmatch",
            "--exclude-standard",
            "-c",
            "-d",
            "-m",
            "-o",
            "-i",
            "-s",
            "-u",
            "-k",
        ],
    },
    # === Local write operations ===
    "add": {
        "allowed_flags": [
            "--all",
            "--update",
            "--force",
            "--dry-run",
            "--verbose",
            "--patch",
            "--intent-to-add",
            "-A",
            "-u",
            "-f",
            "-n",
            "-v",
            "-p",
            "-N",
        ],
    },
    "update-index": {
        "allowed_flags": [
            "--chmod",
            "--refresh",
            "--really-refresh",
            "--verbose",
            "--quiet",
            "-q",
        ],
    },
    "commit": {
        "allowed_flags": [
            "--message",
            "--all",
            "--amend",
            "--no-edit",
            "--allow-empty",
            "--allow-empty-message",
            "--author",
            "--date",
            "--dry-run",
            "--verbose",
            "--quiet",
            "--signoff",
            "-m",
            "-a",
            "-v",
            "-q",
            "-s",
        ],
    },
    "checkout": {
        "allowed_flags": [
            "--force",
            "--ours",
            "--theirs",
            "--merge",
            "--quiet",
            "--track",
            "--no-track",
            "-b",
            "-B",
            "-f",
            "-q",
            "-t",
        ],
    },
    "switch": {
        "allowed_flags": [
            "--create",
            "--force-create",
            "--detach",
            "--quiet",
            "--track",
            "--no-track",
            "-c",
            "-C",
            "-d",
            "-q",
            "-t",
        ],
    },
    "reset": {
        "allowed_flags": [
            "--soft",
            "--mixed",
            "--hard",
            "--merge",
            "--keep",
            "--quiet",
            "-q",
        ],
    },
    "restore": {
        "allowed_flags": [
            "--staged",
            "--worktree",
            "--source",
            "--quiet",
            "-S",
            "-W",
            "-s",
            "-q",
        ],
    },
    "stash": {
        "allowed_flags": [
            "--keep-index",
            "--include-untracked",
            "--all",
            "--quiet",
            "--message",
            "-k",
            "-u",
            "-a",
            "-q",
            "-m",
        ],
    },
    "merge": {
        "allowed_flags": [
            "--no-commit",
            "--no-ff",
            "--ff-only",
            "--squash",
            "--abort",
            "--continue",
            "--quit",
            "--message",
            "--no-edit",
            "--verbose",
            "--quiet",
            "-m",
            "-v",
            "-q",
        ],
    },
    "rebase": {
        "allowed_flags": [
            "--onto",
            "--abort",
            "--continue",
            "--skip",
            "--quit",
            "--interactive",
            "--verbose",
            "--quiet",
            "-i",
            "-v",
            "-q",
        ],
    },
    "cherry-pick": {
        "allowed_flags": [
            "--abort",
            "--continue",
            "--skip",
            "--quit",
            "--no-commit",
            "--edit",
            "--mainline",
            "-n",
            "-e",
            "-m",
        ],
    },
    "am": {
        "allowed_flags": [
            "--abort",
            "--continue",
            "--skip",
            "--quit",
            "--resolved",
            "--no-verify",
            "--keep",
            "--keep-non-patch",
            "--message-id",
            "--scissors",
            "--no-scissors",
            "--ignore-whitespace",
            "--whitespace",
            "--directory",
            "--3way",
            "--quiet",
            "-k",
            "-m",
            "-s",
            "-3",
            "-q",
        ],
    },
    "apply": {
        "allowed_flags": [
            "--stat",
            "--numstat",
            "--summary",
            "--check",
            "--index",
            "--cached",
            "--3way",
            "--reverse",
            "--reject",
            "--verbose",
            "--quiet",
            "--whitespace",
            "--directory",
            "--unidiff-zero",
            "-p",
            "-v",
            "-R",
            "-3",
            "-q",
        ],
    },
    "format-patch": {
        "allowed_flags": [
            "--stdout",
            "--output-directory",
            "--numbered",
            "--start-number",
            "--no-numbered",
            "--keep-subject",
            "--signoff",
            "--cover-letter",
            "--quiet",
            "--no-stat",
            "--stat",
            "--notes",
            "--base",
            "-o",
            # Note: -n is NOT included because FLAG_NORMALIZATION maps -n → --dry-run globally.
            # Users should use --numbered instead.
            "-N",
            "-k",
            "-s",
            "-q",
        ],
    },
    "tag": {
        "allowed_flags": [
            "--list",
            "--delete",
            "--annotate",
            "--message",
            "--force",
            "--sign",
            "--verify",
            "-l",
            "-d",
            "-a",
            "-m",
            "-f",
            "-s",
            "-v",
        ],
    },
    "clean": {
        "allowed_flags": [
            "--force",
            "--dry-run",
            "--quiet",
            "-d",
            "-f",
            "-n",
            "-q",
            "-x",
            "-X",
        ],
    },
    "rm": {
        "allowed_flags": [
            "--force",
            "--dry-run",
            "--cached",
            "--quiet",
            "-f",
            "-n",
            "-r",
            "-q",
        ],
    },
    "mv": {
        "allowed_flags": [
            "--force",
            "--dry-run",
            "--verbose",
            "-f",
            "-n",
            "-v",
            "-k",
        ],
    },
    "blame": {
        "allowed_flags": [
            "--line-porcelain",
            "--porcelain",
            "--incremental",
            "--show-stats",
            "--show-name",
            "--show-number",
            "--show-email",
            "-L",
            "-l",
            "-t",
            "-w",
            "-e",
            "-n",
            "-s",
            "-f",
        ],
    },
    "reflog": {
        "allowed_flags": [
            "--all",
            "--date",
            "--format",
            "--oneline",
            "--max-count",
            # Note: -n is NOT included because FLAG_NORMALIZATION maps -n → --dry-run globally.
            # Users should use --max-count=N instead.
        ],
    },
    "describe": {
        "allowed_flags": [
            "--tags",
            "--all",
            "--long",
            "--abbrev",
            "--always",
            "--dirty",
            "--broken",
            "--match",
            "--exclude",
            "--first-parent",
            "--contains",
        ],
    },
    "config": {
        "allowed_flags": [
            "--get",
            "--get-all",
            "--list",
            "--local",
            "--global",
            "-l",
        ],
    },
    "merge-base": {
        "allowed_flags": [
            "--all",
            "--octopus",
            "--independent",
            "--is-ancestor",
            "--fork-point",
        ],
    },
}

# Flag normalization: map short flags to long form for consistent validation
FLAG_NORMALIZATION = {
    # fetch/ls-remote
    "-a": "--all",
    "-t": "--tags",
    "-p": "--prune",
    "-v": "--verbose",
    "-q": "--quiet",
    "-j": "--jobs",
    # push
    "-f": "--force",
    "-d": "--delete",
    "-u": "--set-upstream",
    "-n": "--dry-run",
}


def normalize_flag(flag: str) -> str:
    """
    Normalize short flags to long form for consistent validation.

    Args:
        flag: The flag to normalize (e.g., "-a" or "--all")

    Returns:
        The normalized long-form flag, or original if not found
    """
    # Handle -X=value format
    if "=" in flag:
        base, value = flag.split("=", 1)
        normalized = FLAG_NORMALIZATION.get(base, base)
        return f"{normalized}={value}"
    return FLAG_NORMALIZATION.get(flag, flag)


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
        # These are valid for 'log' and 'format-patch' operations
        if re.match(r"^-\d+$", arg):
            if operation == "log" and "--max-count" in allowed_flags:
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

        # Normalize short flags to long form
        normalized_flag = normalize_flag(arg)

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

        normalized.append(normalized_flag)
        i += 1

    return True, "", normalized


# =============================================================================
# Credential Helper Management
# =============================================================================

# Credential helper script template for GIT_ASKPASS
_ASKPASS_SCRIPT = """#!/bin/bash
if [[ "$1" == *"Username"* ]]; then
    echo "$GIT_USERNAME"
elif [[ "$1" == *"Password"* ]]; then
    echo "$GIT_PASSWORD"
fi
"""


def create_credential_helper(token_str: str, env: dict[str, str]) -> tuple[str, dict[str, str]]:
    """
    Create a temporary credential helper script for git authentication.

    Creates a GIT_ASKPASS script that provides credentials from environment
    variables. The script is written to a temp file with restrictive permissions.

    Args:
        token_str: The GitHub token to use for authentication
        env: The environment dict to update

    Returns:
        Tuple of (credential_helper_path, updated_env)

    Note:
        Caller MUST clean up the credential file using cleanup_credential_helper()
        in a finally block to ensure the token is never left on disk.
    """
    # Update environment with credential info
    env = env.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_USERNAME"] = "x-access-token"
    env["GIT_PASSWORD"] = token_str

    # Create temp file with restrictive permissions BEFORE writing
    fd, path = tempfile.mkstemp(suffix=".sh", prefix="git-askpass-")
    try:
        os.fchmod(fd, 0o700)  # Set permissions on fd before writing
        os.write(fd, _ASKPASS_SCRIPT.encode())
    finally:
        os.close(fd)

    env["GIT_ASKPASS"] = path
    return path, env


def cleanup_credential_helper(path: str | None) -> None:
    """
    Safely clean up a credential helper file.

    Args:
        path: Path to the credential helper file, or None if not created yet
    """
    if path and os.path.exists(path):
        with contextlib.suppress(OSError):
            os.unlink(path)


# =============================================================================
# Changed Files Detection
# =============================================================================


def get_changed_files_in_push(
    repo_path: str, remote: str, branch: str
) -> tuple[list[str], str | None]:
    """
    Get the list of files that would be changed by a push.

    Compares local branch to remote tracking branch to determine what
    files are being modified in the commits being pushed.

    SECURITY: This function is used for security-critical file restriction checks.
    If it returns an error, the caller MUST treat it as a security failure and
    block the push. Never fail open on git diff errors.

    Args:
        repo_path: Path to the git repository
        remote: Remote name (e.g., "origin")
        branch: Branch name being pushed

    Returns:
        Tuple of (changed_files, error_message)
        - changed_files: List of file paths that are changed
        - error_message: Error string if the check failed, None on success
    """
    import subprocess

    # Get the merge base between local and remote
    # This shows what commits are being pushed (local commits not on remote)
    # Using two-dot (..) syntax to get the actual diff, not three-dot (...)
    # which uses the merge-base and may miss files if remote has been updated
    try:
        # First, try to get files changed between remote tracking branch and local
        # Using --name-only with git diff to get just file names
        result = subprocess.run(
            git_cmd(
                "diff",
                "--name-only",
                f"{remote}/{branch}..HEAD",
            ),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        if result.returncode == 0:
            files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
            return files, None

        # If remote branch doesn't exist yet, get all files in all commits on the branch
        # that aren't on the default branch
        result = subprocess.run(
            git_cmd(
                "diff",
                "--name-only",
                f"{remote}/main..HEAD",
            ),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        if result.returncode == 0:
            files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
            return files, None

        # Try master if main doesn't exist
        result = subprocess.run(
            git_cmd(
                "diff",
                "--name-only",
                f"{remote}/master..HEAD",
            ),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        if result.returncode == 0:
            files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
            return files, None

        # SECURITY: If we cannot determine what files are being pushed, we MUST
        # fail closed to prevent bypass of file restrictions. An attacker could
        # intentionally cause git diff to fail (e.g., corrupt refs, timeout) to
        # bypass protection. This follows the codebase's fail-closed security pattern.
        logger.error(
            "Could not determine changed files in push - failing closed for security",
            repo_path=repo_path,
            remote=remote,
            branch=branch,
            stderr=result.stderr,
        )
        return [], "Could not determine changed files - push blocked for security"

    except subprocess.TimeoutExpired:
        return [], "Timeout determining changed files"
    except Exception as e:
        return [], f"Error determining changed files: {e}"


def get_token_for_repo(repo: str) -> tuple[str | None, str, str]:
    """
    Get the authentication token for a repository.

    Determines the auth mode (bot vs user) for the repo and retrieves
    the appropriate token.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        Tuple of (token_str, auth_mode, error_message)
        - token_str is None if token unavailable (error_message explains why)
        - auth_mode is "bot" or "user"
        - error_message is empty string on success
    """
    auth_mode = get_auth_mode(repo)
    github = get_github_client(mode=auth_mode)

    if auth_mode == "user":
        token_str = github.get_user_token()
        if not token_str:
            return (
                None,
                auth_mode,
                "User token not available. Set GITHUB_USER_TOKEN environment variable.",
            )
    else:
        token = github.get_token()
        if not token:
            return None, auth_mode, "GitHub token not available"
        token_str = token.token

    return token_str, auth_mode, ""
