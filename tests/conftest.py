"""Pytest configuration and fixtures for egg tests."""

import sys
from pathlib import Path

import pytest

# Ensure the egg package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))


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
