"""Credential-helper lifecycle + per-repo token resolution.

Extracted verbatim from the pre-split ``gateway/git_client.py``
(#3312 slice-11). AST-identical to the originals — pure refactor.
"""

import contextlib
import os
import tempfile

from repo_config import get_auth_mode

# ``github_client`` is a sibling top-level gateway module, not part of this
# sub-package — the dual import covers both packaged (``gateway.git_client``)
# and flat (container ``git_client``) execution, matching the pre-split
# module-level import it replaces.
try:
    from ..github_client import get_github_client
except ImportError:
    from github_client import get_github_client  # type: ignore[no-redef,import-untyped]


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
