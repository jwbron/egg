"""Pytest configuration and fixtures for egg tests."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure the egg package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))


# Set up test environment variables before imports that might use them
TEST_LAUNCHER_SECRET = "test-launcher-secret-12345"
os.environ.setdefault("EGG_LAUNCHER_SECRET", TEST_LAUNCHER_SECRET)

# Create a minimal test repositories.yaml config if not set
if "EGG_REPO_CONFIG" not in os.environ:
    _test_config_dir = tempfile.mkdtemp()
    _test_config_path = Path(_test_config_dir) / "repositories.yaml"
    _test_config_path.write_text(
        """
github:
  username: test-user
  writable:
    - test-user/test-repo
    - owner/repo
  default_reviewer: reviewer
"""
    )
    os.environ["EGG_REPO_CONFIG"] = str(_test_config_path)


@pytest.fixture
def sample_config() -> dict:
    """Provide a sample configuration for testing."""
    return {
        "egg": {
            "name": "test-sandbox",
            "git": {
                "branch_prefix": "egg/",
                "protected_branches": ["main", "master"],
                "allow_force_push": False,
                "merge_blocking": True,
            },
            "logging": {
                "level": "INFO",
                "format": "json",
                "output": "stdout",
            },
        }
    }


@pytest.fixture
def mock_github_client():
    """Create a mock GitHub client for testing."""
    client = MagicMock()
    # Set up default return values
    client.get_pr_info.return_value = None
    client.list_prs_for_branch.return_value = []
    client.branch_exists.return_value = False
    return client


@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory for tests."""
    return tmp_path


@pytest.fixture
def temp_repos_dir(tmp_path):
    """Provide a temporary repos directory for testing."""
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    return repos_dir


@pytest.fixture
def temp_worktree_dir(tmp_path):
    """Provide a temporary worktree base directory for testing."""
    worktree_dir = tmp_path / ".egg-worktrees"
    worktree_dir.mkdir()
    return worktree_dir


@pytest.fixture
def mock_session():
    """Create a mock session for testing."""
    from datetime import UTC, datetime, timedelta

    from gateway.session_manager import Session

    now = datetime.now(UTC)
    return Session(
        session_token="test-session-token",
        session_token_hash="test-hash",
        container_id="test-container",
        container_ip="172.18.0.5",
        mode="private",
        created_at=now,
        last_seen=now,
        expires_at=now + timedelta(hours=24),
    )


@pytest.fixture
def mock_token_refresher():
    """Create a mock token refresher for testing."""
    refresher = MagicMock()
    refresher.get_token.return_value = "ghs_test_token_12345"
    refresher.get_token_info.return_value = MagicMock(
        token="ghs_test_token_12345",
        is_expired=False,
        source="refresher",
    )
    return refresher


@pytest.fixture(autouse=True)
def clean_environment():
    """Clean up global state before and after each test."""
    # Store original values
    import gateway.private_repo_policy as prp
    import gateway.repo_visibility as rv
    import gateway.session_manager as sm

    original_session_manager = getattr(sm, "_session_manager", None)
    original_policy = getattr(prp, "_policy", None)
    original_checker = getattr(rv, "_checker", None)

    yield

    # Restore original values after test
    sm._session_manager = original_session_manager
    prp._policy = original_policy
    rv._checker = original_checker


# Common test data
TEST_OWNER = "test-owner"
TEST_REPO = "test-repo"
TEST_BRANCH = "egg/test-branch"
TEST_PR_NUMBER = 123


@pytest.fixture
def test_repo_info():
    """Provide standard test repo info."""
    return {
        "owner": TEST_OWNER,
        "repo": TEST_REPO,
        "full_name": f"{TEST_OWNER}/{TEST_REPO}",
    }
