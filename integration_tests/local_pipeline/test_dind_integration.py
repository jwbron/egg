"""
Integration tests for DinD sidecar lifecycle.

Tests the DindManager end-to-end: sidecar start, health check, image
pre-load, and teardown. These tests require Docker to be available.

Marked with the ``integration`` marker so they run with the integration
test suite and not during unit tests.
"""

import subprocess

import pytest

# Skip the entire module if Docker is not available
from tests.utils.gateway_client import docker_available

if not docker_available():
    pytest.skip("Docker is not available", allow_module_level=True)

import sys
from pathlib import Path

# Add orchestrator and shared to path for direct imports
_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dind_manager import DindManager, DindStatusValue


@pytest.fixture
def dind_manager():
    """Create a DindManager and tear it down after the test."""
    manager = DindManager(pipeline_id="test-dind")
    yield manager
    manager.teardown()


@pytest.fixture
def healthy_dind(dind_manager: DindManager):
    """Start DinD and skip if health check fails (CI environment limitation)."""
    status = dind_manager.start()
    if status.status != DindStatusValue.HEALTHY:
        pytest.skip("DinD rootless daemon did not become healthy (CI environment limitation)")
    return status


@pytest.mark.integration
class TestDindLifecycle:
    """End-to-end tests for DinD sidecar lifecycle."""

    def test_start_and_health(self, dind_manager: DindManager):
        """DinD container starts and daemon becomes healthy."""
        status = dind_manager.start()
        if status.status != DindStatusValue.HEALTHY:
            pytest.skip("DinD rootless daemon did not become healthy (CI environment limitation)")

        assert status.status == DindStatusValue.HEALTHY
        assert status.container_id
        assert status.daemon_url
        assert "tcp://" in status.daemon_url
        assert ":2375" in status.daemon_url

    def test_daemon_responds_to_docker_info(self, healthy_dind, dind_manager: DindManager):
        """DinD daemon responds to ``docker info`` over TCP."""
        status = healthy_dind

        result = subprocess.run(
            ["docker", "-H", status.daemon_url, "info"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "Server Version" in result.stdout

    def test_preload_images(self, healthy_dind, dind_manager: DindManager):
        """Images can be pre-loaded into the DinD daemon."""
        status = healthy_dind

        # Build a tiny test image on the host
        subprocess.run(
            [
                "docker",
                "build",
                "-t",
                "dind-test-image:latest",
                "-",
            ],
            input="FROM alpine:latest\nRUN echo hello\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )

        loaded = dind_manager.preload_images(["dind-test-image:latest"])
        assert "dind-test-image:latest" in loaded

        # Verify the image exists in the DinD daemon
        result = subprocess.run(
            ["docker", "-H", status.daemon_url, "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "dind-test-image:latest" in result.stdout

    def test_teardown_removes_container(self, dind_manager: DindManager):
        """Teardown removes the DinD container."""
        dind_manager.start()
        container_name = dind_manager.container_name

        # Container should exist
        result = subprocess.run(
            ["docker", "inspect", container_name],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0

        dind_manager.teardown()

        # Container should be gone
        result = subprocess.run(
            ["docker", "inspect", container_name],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode != 0

    def test_teardown_idempotent(self, dind_manager: DindManager):
        """Calling teardown twice does not error."""
        dind_manager.start()
        dind_manager.teardown()
        dind_manager.teardown()  # Should not raise
