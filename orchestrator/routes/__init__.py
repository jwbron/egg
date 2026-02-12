"""
REST API routes for egg-orchestrator.

Each module provides a Flask Blueprint for a logical group of endpoints.
"""

import os
from pathlib import Path

from flask import request


def get_repo_path() -> Path:
    """Get the repository path from environment or request.

    When EGG_REPO_PATH is a parent directory containing multiple repos
    (e.g. /home/egg/repos), this resolves to the specific repo
    subdirectory using the ``repo`` field from the request body
    (e.g. ``owner/name`` -> ``/home/egg/repos/name``).

    Returns:
        Path to the repository
    """
    # Check request args first
    repo_path = request.args.get("repo_path")
    if repo_path:
        return Path(repo_path)

    # Check JSON body
    data = request.get_json(silent=True) or {}
    if data.get("repo_path"):
        return Path(data["repo_path"])

    # Check environment
    env_path = os.environ.get("EGG_REPO_PATH")
    if env_path:
        base = Path(env_path)
        # EGG_REPO_PATH may be a parent dir containing repo subdirectories.
        # If it's not itself a git repo, resolve using the repo name from
        # the request body (e.g. "owner/name" -> base / "name").
        if not (base / ".git").exists():
            repo = data.get("repo", "") or request.args.get("repo", "")
            if repo:
                repo_name = repo.split("/")[-1]
                candidate = base / repo_name
                if (candidate / ".git").exists():
                    return candidate
        return base

    # Default to current working directory
    return Path.cwd()
