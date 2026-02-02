"""
Repository URL and path parsing utilities.

Extracts owner/repo information from various formats:
- GitHub URLs (HTTPS, SSH, git protocol)
- Local worktree paths
- Git remote URLs

Used by Private Repo Mode to determine which repository an operation targets.

Security Note:
    URLs are normalized before parsing to prevent bypass attempts via:
    - URL-encoded characters
    - Authentication credentials in URLs
    - Unusual port numbers
    - Double slashes or path traversal
"""

import os
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from shared.egg_logging import get_logger

logger = get_logger("gateway.repo-parser")


@dataclass
class RepoInfo:
    """Parsed repository information."""

    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        """Get the full repository name (owner/repo)."""
        return f"{self.owner}/{self.repo}"

    def __str__(self) -> str:
        return self.full_name


# Regex patterns for parsing GitHub URLs
GITHUB_URL_PATTERNS = [
    # HTTPS: https://github.com/owner/repo.git or https://github.com/owner/repo
    re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"),
    # SSH: git@github.com:owner/repo.git or git@github.com:owner/repo
    re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$"),
    # SSH with protocol: ssh://git@github.com/owner/repo.git
    re.compile(r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?$"),
    # Git protocol: git://github.com/owner/repo.git
    re.compile(r"^git://github\.com/([^/]+)/([^/]+?)(?:\.git)?$"),
]

# Pattern for owner/repo format (without URL)
OWNER_REPO_PATTERN = re.compile(r"^([^/\s]+)/([^/\s]+)$")


def normalize_github_url(url: str) -> str:
    """Normalize a GitHub URL before parsing.

    Handles potential bypass attempts via:
    - URL-encoded characters (e.g., %6f%77%6e%65%72 -> owner)
    - Authentication credentials in URL (e.g., user:pass@github.com)
    - Unusual port numbers (e.g., github.com:443)
    - Double slashes in path (e.g., github.com//owner/repo)
    - Trailing slashes and whitespace
    """
    if not url:
        return ""

    # Strip whitespace
    url = url.strip()

    # Decode URL-encoded characters (handle double-encoding too)
    for _ in range(3):
        decoded = unquote(url)
        if decoded == url:
            break
        url = decoded

    # For HTTP(S) URLs, use urlparse for robust handling
    if url.startswith(("http://", "https://")):
        try:
            parsed = urlparse(url)

            # Rebuild URL without credentials
            host = parsed.hostname or parsed.netloc
            if host and host.lower() == "github.com":
                # Normalize path: remove double slashes, strip trailing slash
                path = parsed.path
                while "//" in path:
                    path = path.replace("//", "/")
                path = path.rstrip("/")

                # Rebuild clean URL
                url = f"https://github.com{path}"
        except Exception:
            pass

    # Normalize double slashes in path (for all URL types)
    if "://" in url:
        protocol, rest = url.split("://", 1)
        while "//" in rest:
            rest = rest.replace("//", "/")
        url = f"{protocol}://{rest}"

    return url


def parse_github_url(url: str) -> RepoInfo | None:
    """Parse a GitHub URL to extract owner and repo.

    Supports:
    - https://github.com/owner/repo.git
    - https://github.com/owner/repo
    - git@github.com:owner/repo.git
    - ssh://git@github.com/owner/repo.git
    - git://github.com/owner/repo.git
    """
    if not url:
        return None

    # Normalize URL to prevent bypass attempts
    url = normalize_github_url(url)

    if not url:
        return None

    for pattern in GITHUB_URL_PATTERNS:
        match = pattern.match(url)
        if match:
            owner, repo = match.groups()
            # Additional validation
            if ".." in owner or ".." in repo:
                logger.warning(
                    "Suspicious path traversal in parsed URL",
                    owner=owner,
                    repo=repo,
                )
                return None
            return RepoInfo(owner=owner, repo=repo)

    return None


def parse_owner_repo(repo_str: str) -> RepoInfo | None:
    """Parse an owner/repo string."""
    if not repo_str:
        return None

    repo_str = repo_str.strip()

    # Try owner/repo format
    match = OWNER_REPO_PATTERN.match(repo_str)
    if match:
        return RepoInfo(owner=match.group(1), repo=match.group(2))

    # Try as URL
    return parse_github_url(repo_str)


def get_remote_url(repo_path: str, remote: str = "origin") -> str | None:
    """Get the remote URL for a git repository."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "-c",
                "safe.directory=*",
                "remote",
                "get-url",
                remote,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        if result.returncode == 0:
            return result.stdout.strip()

        logger.debug(
            "Could not get remote URL",
            repo_path=repo_path,
            remote=remote,
            stderr=result.stderr[:200] if result.stderr else "",
        )
        return None

    except subprocess.TimeoutExpired:
        logger.warning("Git command timed out", repo_path=repo_path, remote=remote)
        return None
    except Exception as e:
        logger.warning("Git command failed", repo_path=repo_path, error=str(e))
        return None


def parse_repo_from_path(repo_path: str, remote: str = "origin") -> RepoInfo | None:
    """Extract repository info from a local path by reading its git remote."""
    remote_url = get_remote_url(repo_path, remote)
    if remote_url:
        return parse_github_url(remote_url)
    return None


def parse_worktree_path(
    path: str,
    worktree_base: str = "~/.egg-worktrees",
) -> tuple[str | None, str | None]:
    """Parse a worktree path to extract container ID and repo name.

    Expected format: {worktree_base}/{container_id}/{repo_name}
    """
    if not path:
        return None, None

    # Normalize path
    path = os.path.realpath(path).rstrip("/")

    # Expected base path
    worktree_base_path = os.path.realpath(os.path.expanduser(worktree_base))

    if not path.startswith(worktree_base_path + "/"):
        return None, None

    # Extract relative path
    relative = path[len(worktree_base_path) + 1 :]
    parts = relative.split("/")

    if len(parts) >= 2:
        container_id = parts[0]
        repo_name = parts[1]
        return container_id, repo_name

    return None, None


def extract_repo_from_request(
    repo: str | None = None,
    repo_path: str | None = None,
    url: str | None = None,
    remote: str = "origin",
) -> RepoInfo | None:
    """Extract repository info from various sources.

    Tries in order:
    1. repo (if owner/repo format)
    2. url (if GitHub URL)
    3. repo_path (by reading git remote)
    """
    # Try repo parameter first (most explicit)
    if repo:
        parsed = parse_owner_repo(repo)
        if parsed:
            return parsed

    # Try URL
    if url:
        parsed = parse_github_url(url)
        if parsed:
            return parsed

    # Try repo_path
    if repo_path:
        parsed = parse_repo_from_path(repo_path, remote)
        if parsed:
            return parsed

    return None


def is_github_url(url: str) -> bool:
    """Check if a URL is a GitHub URL."""
    if not url:
        return False
    return parse_github_url(url) is not None


def normalize_repo_name(name: str) -> str:
    """Normalize a repository name by removing .git suffix."""
    if name.endswith(".git"):
        return name[:-4]
    return name
