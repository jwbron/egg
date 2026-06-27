"""Remote-URL helpers + the validated ``git`` argv builder.

Extracted verbatim from the pre-split ``gateway/git_client.py``
(#3312 slice-11). AST-identical to the originals — pure refactor.
"""

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
    - gc.auto=0: Prevent automatic garbage collection. git gc --auto runs
      git worktree prune as part of cleanup, which can delete worktree admin
      directories if their paths appear temporarily inaccessible (e.g., during
      Docker mount races). Disabling auto-gc prevents mid-session admin dir
      deletion. Garbage collection still runs at gateway startup via the
      explicit prune_stale_worktrees() call.
    """
    return [
        GIT_CLI,
        "-c",
        "safe.directory=*",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "gc.auto=0",
        *args,
    ]


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


def is_url_remote(remote: str) -> bool:
    """Check if remote is a URL (not a named remote like 'origin').

    Only accepts HTTPS, git@, and ssh:// URLs. Plain http:// is rejected
    because the gateway uses HTTPS with token auth — accepting HTTP would
    risk exposing credentials over an unencrypted connection.
    """
    return remote.startswith(("https://", "git@", "ssh://"))


def resolve_remote_url(remote: str, exec_path: str) -> tuple[str, str | None]:
    """Resolve a git remote to its URL.

    If ``remote`` is already a URL (https://, git@, ssh://), returns it
    directly without calling ``git remote get-url``.  Otherwise runs
    ``git remote get-url <remote>`` in ``exec_path``.

    Returns:
        (remote_url, error) — on success error is None; on failure
        remote_url is empty and error contains the message.
    """
    import subprocess

    if is_url_remote(remote):
        return remote, None

    try:
        result = subprocess.run(
            git_cmd("remote", "get-url", remote),
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return "", f"Failed to get remote URL: {result.stderr}"
        return result.stdout.strip(), None
    except Exception as e:
        return "", f"Failed to get remote URL: {e}"


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
