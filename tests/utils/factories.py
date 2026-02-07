"""
Factory functions for creating test objects.

These factories reduce boilerplate and ensure consistent test object creation
across the test suite. Each factory provides sensible defaults while allowing
full customization.
"""

from datetime import UTC, datetime, timedelta
from typing import Any


def make_session(
    *,
    session_token: str | None = "test-token-abc123",
    session_token_hash: str | None = None,
    container_id: str = "test-container-001",
    container_ip: str = "172.18.0.5",
    mode: str = "private",
    created_at: datetime | None = None,
    last_seen: datetime | None = None,
    expires_at: datetime | None = None,
    ttl_hours: int = 24,
    expired: bool = False,
) -> dict[str, Any]:
    """Create a session data dictionary for testing.

    This returns a dict suitable for constructing a Session object or mocking
    session data in tests.

    Args:
        session_token: Raw session token (default: "test-token-abc123")
        session_token_hash: Token hash (computed from token if None)
        container_id: Docker container ID
        container_ip: Container IP address
        mode: Session mode ("private" or "public")
        created_at: Creation timestamp (default: now)
        last_seen: Last activity timestamp (default: now)
        expires_at: Expiration timestamp (default: now + ttl_hours)
        ttl_hours: Hours until expiration (used if expires_at not set)
        expired: If True, set expires_at in the past

    Returns:
        Dictionary with session data suitable for Session construction.
    """
    import hashlib

    now = datetime.now(UTC)

    if session_token_hash is None and session_token is not None:
        session_token_hash = hashlib.sha256(session_token.encode()).hexdigest()

    if created_at is None:
        created_at = now

    if last_seen is None:
        last_seen = now

    if expires_at is None:
        if expired:
            expires_at = now - timedelta(seconds=1)
        else:
            expires_at = now + timedelta(hours=ttl_hours)

    return {
        "session_token": session_token,
        "session_token_hash": session_token_hash,
        "container_id": container_id,
        "container_ip": container_ip,
        "mode": mode,
        "created_at": created_at,
        "last_seen": last_seen,
        "expires_at": expires_at,
    }


def make_policy_context(
    *,
    repo: str = "owner/repo",
    branch: str = "feature-branch",
    auth_mode: str = "bot",
    pr_number: int | None = None,
    pr_author: str | None = None,
    pr_state: str = "open",
    configured_user: str | None = None,
    trusted_users: list[str] | None = None,
) -> dict[str, Any]:
    """Create a policy evaluation context for testing.

    Args:
        repo: Repository in "owner/repo" format
        branch: Branch name being pushed to
        auth_mode: Authentication mode ("bot" or "user")
        pr_number: PR number if a PR exists on this branch
        pr_author: PR author login if PR exists
        pr_state: PR state ("open", "closed", "merged")
        configured_user: Configured user for user mode
        trusted_users: List of trusted GitHub usernames

    Returns:
        Dictionary with policy context data.
    """
    context = {
        "repo": repo,
        "branch": branch,
        "auth_mode": auth_mode,
        "configured_user": configured_user,
        "trusted_users": trusted_users or [],
    }

    if pr_number is not None:
        context["pr"] = {
            "number": pr_number,
            "author": pr_author or "unknown",
            "state": pr_state,
        }

    return context


def make_git_command(
    *,
    command: str = "push",
    args: list[str] | None = None,
    remote: str = "origin",
    branch: str | None = None,
    force: bool = False,
    delete: bool = False,
) -> list[str]:
    """Create a git command for testing.

    Args:
        command: Git subcommand (push, pull, fetch, etc.)
        args: Additional arguments
        remote: Remote name for push/pull
        branch: Branch name (added to push/pull commands)
        force: Add --force flag
        delete: Add --delete flag

    Returns:
        List representing the git command.
    """
    cmd = ["git", command]

    if force:
        cmd.append("--force")

    if delete:
        cmd.append("--delete")

    if command in ("push", "pull", "fetch"):
        cmd.append(remote)
        if branch:
            cmd.append(branch)

    if args:
        cmd.extend(args)

    return cmd


def make_pr_info(
    *,
    number: int = 123,
    author: str | dict[str, Any] = "egg",
    state: str = "open",
    head_branch: str = "feature-branch",
    base_branch: str = "main",
    title: str = "Test PR",
    body: str = "Test PR description",
) -> dict[str, Any]:
    """Create PR info response data for mocking GitHub API.

    Args:
        number: PR number
        author: Author login string or dict with "login" key
        state: PR state ("open", "closed", "merged")
        head_branch: Head branch name (headRefName in GraphQL)
        base_branch: Base branch name
        title: PR title
        body: PR body

    Returns:
        Dictionary matching GitHub API PR info response format.
    """
    if isinstance(author, str):
        author_dict = {"login": author}
    else:
        author_dict = author

    return {
        "number": number,
        "author": author_dict,
        "state": state,
        "headRefName": head_branch,
        "baseRefName": base_branch,
        "title": title,
        "body": body,
    }


def make_cached_pr_info(
    *,
    pr_number: int = 123,
    author: str = "egg",
    state: str = "open",
    head_branch: str = "feature-branch",
    fetched_at: float | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    """Create CachedPRInfo data for testing cache behavior.

    Args:
        pr_number: PR number
        author: Author login
        state: PR state
        head_branch: Head branch name
        fetched_at: Timestamp when fetched (default: now)
        stale: If True, set fetched_at to make cache stale (> 5 min old)

    Returns:
        Dictionary suitable for CachedPRInfo construction.
    """
    if fetched_at is None:
        if stale:
            # 10 minutes ago - definitely stale (TTL is 5 min)
            fetched_at = datetime.now(UTC).timestamp() - 600
        else:
            fetched_at = datetime.now(UTC).timestamp()

    return {
        "pr_number": pr_number,
        "author": author,
        "state": state,
        "head_branch": head_branch,
        "fetched_at": fetched_at,
    }
