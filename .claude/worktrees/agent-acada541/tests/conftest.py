"""
Pytest configuration and shared fixtures for egg tests.
"""

import sys
import tempfile
from pathlib import Path

import pytest

# Add project paths to sys.path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "shared"))
sys.path.insert(0, str(PROJECT_ROOT / "sandbox"))
sys.path.insert(0, str(PROJECT_ROOT / "sandbox" / "tools"))
sys.path.insert(0, str(PROJECT_ROOT / "config"))


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_home(temp_dir, monkeypatch):
    """Mock the home directory for tests."""
    monkeypatch.setenv("HOME", str(temp_dir))

    # Create expected directory structure
    (temp_dir / "sharing" / "notifications").mkdir(parents=True)
    (temp_dir / "sharing" / "tracking").mkdir(parents=True)
    (temp_dir / "sharing" / "incoming").mkdir(parents=True)

    return temp_dir


@pytest.fixture
def notifications_dir(mock_home):
    """Return the mocked notifications directory."""
    return mock_home / "sharing" / "notifications"


@pytest.fixture
def mock_env(monkeypatch):
    """Set up common environment variables for testing."""
    monkeypatch.setenv("EGG_TEST_MODE", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
