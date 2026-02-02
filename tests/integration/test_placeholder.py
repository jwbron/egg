"""Placeholder integration tests.

Integration tests require Docker containers to be built and running.
These will be implemented as the gateway and sandbox components are built.
"""

import pytest


@pytest.mark.skip(reason="Integration tests pending gateway/sandbox implementation")
def test_gateway_health_check():
    """Test that the gateway responds to health checks."""
    pass


@pytest.mark.skip(reason="Integration tests pending gateway/sandbox implementation")
def test_sandbox_git_wrapper():
    """Test that git commands in sandbox route through gateway."""
    pass
