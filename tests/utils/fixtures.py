"""
Additional pytest fixtures for testing.

These fixtures complement the fixtures in tests/conftest.py with more
specialized setups for gateway, session, and policy testing.
"""

import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_github_client():
    """Create a mock GitHub client with common methods.

    Returns a MagicMock configured with the GitHubClient interface.
    """
    client = MagicMock()

    # Default return values for common methods
    client.get_pr_info.return_value = None
    client.list_prs_for_branch.return_value = []
    client.branch_exists.return_value = True

    return client


@pytest.fixture
def session_persistence_file(tmp_path):
    """Create a temporary file path for session persistence testing."""
    return tmp_path / "sessions.json"


@pytest.fixture
def gateway_env(monkeypatch):
    """Set up gateway environment variables for testing.

    Sets required environment variables for gateway policy and session
    management. Returns a dict of the configured values.
    """
    config = {
        "GATEWAY_BOT_NAME": "egg",
        "GATEWAY_BOT_BRANCH_PREFIX": "egg",
        "GATEWAY_TRUSTED_USERS": "",
        "EGG_LAUNCHER_SECRET": "test-launcher-secret",
    }

    for key, value in config.items():
        monkeypatch.setenv(key, value)

    return config


@pytest.fixture
def user_mode_config(tmp_path, monkeypatch):
    """Set up a minimal user mode configuration for testing.

    Creates a temporary repositories.yaml config file and sets the
    EGG_REPO_CONFIG environment variable.

    Returns the path to the config file.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "repositories.yaml"

    config_content = """
github:
  username: test-user
  writable:
    - test-user/test-repo
    - owner/repo
  default_reviewer: reviewer
"""
    config_file.write_text(config_content)
    monkeypatch.setenv("EGG_REPO_CONFIG", str(config_file))

    return config_file


@pytest.fixture
def isolated_policy_caches(monkeypatch):
    """Reset policy engine caches for isolated testing.

    This ensures each test starts with fresh caches and doesn't
    leak state between tests.
    """
    # Import and reset the caches
    try:
        from policy import _reset_bot_config_caches

        _reset_bot_config_caches()
    except ImportError:
        pass

    yield

    # Reset again after test
    try:
        from policy import _reset_bot_config_caches

        _reset_bot_config_caches()
    except ImportError:
        pass


@pytest.fixture
def clean_session_manager():
    """Create a fresh SessionManager instance with isolated state.

    Returns a factory function that creates new SessionManager instances
    with a unique temporary persistence file.
    """
    temp_files = []

    def _create_manager(**kwargs):
        import tempfile
        from pathlib import Path

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        temp_files.append(path)

        # Import here to use the conftest-loaded module
        from session_manager import SessionManager

        return SessionManager(persistence_file=Path(path), **kwargs)

    yield _create_manager

    # Cleanup temp files
    for path in temp_files:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def mock_container_env(monkeypatch, tmp_path):
    """Set up a mock container environment for entrypoint testing.

    Creates a mock filesystem structure and environment variables
    that simulate the container environment.
    """
    # Create directory structure
    home = tmp_path / "home" / "egg"
    home.mkdir(parents=True)
    (home / "repos").mkdir()
    (home / ".claude").mkdir()
    (home / ".config" / "claude-code").mkdir(parents=True)
    (home / "sharing" / "notifications").mkdir(parents=True)

    # Set environment
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "egg")
    monkeypatch.setenv("RUNTIME_UID", "1000")
    monkeypatch.setenv("RUNTIME_GID", "1000")

    return {
        "home": home,
        "repos": home / "repos",
        "claude_dir": home / ".claude",
    }
