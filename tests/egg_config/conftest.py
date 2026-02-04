"""Pytest fixtures for jib_config tests."""

import pytest


@pytest.fixture
def temp_dir(tmp_path):
    """Alias for pytest's tmp_path fixture.

    Provides a temporary directory unique to the test invocation.
    """
    return tmp_path
