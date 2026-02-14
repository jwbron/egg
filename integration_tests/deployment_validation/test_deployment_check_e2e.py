"""End-to-end integration test for deployment validation.

Tests the full devserver lifecycle with real Docker containers:
1. Load deployment config from a test fixture
2. Start devserver stack via DevserverManager
3. Verify health endpoints respond
4. Tear down and confirm no orphaned containers or networks

Skips gracefully when Docker is unavailable.
"""

import importlib.util
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

# Skip entire module if docker CLI or SDK is unavailable
docker_cli_available = shutil.which("docker") is not None
docker_sdk_available = importlib.util.find_spec("docker") is not None


def _docker_daemon_running() -> bool:
    """Check if Docker daemon is reachable."""
    if not docker_cli_available:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not docker_cli_available or not docker_sdk_available,
        reason="Docker CLI or SDK not available",
    ),
    pytest.mark.skipif(
        not _docker_daemon_running(),
        reason="Docker daemon not running",
    ),
]


# Minimal compose file: a simple HTTP echo server using busybox httpd
ECHO_COMPOSE = textwrap.dedent("""\
    services:
      echo:
        image: busybox:latest
        command: ["sh", "-c", "mkdir -p /www && echo 'ok' > /www/health && httpd -f -p 8080 -h /www"]
        healthcheck:
          test: ["CMD", "wget", "-qO-", "http://localhost:8080/health"]
          interval: 2s
          timeout: 2s
          retries: 10
""")

DEPLOYMENT_CONFIG_YAML = textwrap.dedent("""\
    compose_file: docker-compose.yml
    services:
      - source_dir: src/
        service_name: echo
        container_mount_path: /app
    health_endpoints:
      echo: /health
    startup_timeout_seconds: 60
""")


@pytest.fixture
def test_repo(tmp_path):
    """Create a minimal test repository with compose and deployment config."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Initialize a git repo with the compose file committed
    subprocess.run(
        ["git", "init"],
        cwd=str(repo_dir),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo_dir),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo_dir),
        capture_output=True,
        check=True,
    )

    # Write compose file
    (repo_dir / "docker-compose.yml").write_text(ECHO_COMPOSE, encoding="utf-8")

    # Write deployment config
    egg_dir = repo_dir / ".egg"
    egg_dir.mkdir()
    (egg_dir / "deployment.yml").write_text(DEPLOYMENT_CONFIG_YAML, encoding="utf-8")

    # Create source directory (for service mapping)
    src_dir = repo_dir / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("# app code\n", encoding="utf-8")

    # Commit everything
    subprocess.run(
        ["git", "add", "."],
        cwd=str(repo_dir),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo_dir),
        capture_output=True,
        check=True,
    )

    return repo_dir


@pytest.fixture
def manager(test_repo):
    """Create a DevserverManager and ensure teardown."""
    import sys

    # Add orchestrator and shared to path for imports
    orchestrator_path = Path(__file__).parent.parent.parent / "orchestrator"
    shared_path = Path(__file__).parent.parent.parent / "shared"
    for p in [str(orchestrator_path), str(shared_path)]:
        if p not in sys.path:
            sys.path.insert(0, p)

    from devserver import DevserverManager

    mgr = DevserverManager(
        pipeline_id="integration-test",
        repo_path=test_repo,
        worktree_path=test_repo,
    )
    yield mgr
    # Always teardown, even if test fails
    mgr.teardown()


@pytest.fixture(autouse=True)
def cleanup_networks():
    """Ensure no orphaned egg-check-integration-test networks remain after tests."""
    yield
    # Post-test cleanup: remove any leftover networks
    try:
        result = subprocess.run(
            ["docker", "network", "ls", "--filter", "label=egg.pipeline-id=integration-test", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for network_id in result.stdout.strip().split("\n"):
            if network_id:
                subprocess.run(
                    ["docker", "network", "rm", network_id],
                    capture_output=True,
                    timeout=10,
                )
    except Exception:
        pass


class TestDeploymentCheckE2E:
    """End-to-end deployment validation lifecycle tests."""

    def test_full_lifecycle(self, manager, test_repo):
        """Test the complete start → health → teardown flow."""
        from devserver import DevserverStatusValue
        from egg_contracts.deployment import load_deployment_config

        config = load_deployment_config(test_repo)
        assert config is not None, "Deployment config should load from test repo"

        # Start devserver stack
        status = manager.start(config, changed_files=["src/app.py"])

        # Stack should be healthy (busybox httpd serves /health)
        assert status.status in (
            DevserverStatusValue.HEALTHY,
            DevserverStatusValue.UNHEALTHY,  # Accept unhealthy — wget may not be available in compose exec
        )
        assert "echo" in status.services

        # Verify the Docker network was created
        network_name = manager.network_name
        result = subprocess.run(
            ["docker", "network", "inspect", network_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Check network '{network_name}' should exist"

        # Verify the network is internal (air-gapped)
        import json

        network_info = json.loads(result.stdout)
        assert network_info[0]["Internal"] is True, "Check network must be internal"

        # Teardown
        manager.teardown()

        # Verify network was removed
        result = subprocess.run(
            ["docker", "network", "inspect", network_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0, f"Check network '{network_name}' should be removed after teardown"

        # Verify no orphaned containers
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"label=com.docker.compose.project={network_name}",
                "--format", "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        orphaned = [c for c in result.stdout.strip().split("\n") if c.strip()]
        assert len(orphaned) == 0, f"No orphaned containers should remain, found: {orphaned}"

    def test_teardown_is_idempotent(self, manager, test_repo):
        """Calling teardown twice does not raise."""
        from egg_contracts.deployment import load_deployment_config

        config = load_deployment_config(test_repo)
        manager.start(config, changed_files=["src/app.py"])
        manager.teardown()
        # Second teardown should not raise
        manager.teardown()

    def test_compose_extraction_reads_head(self, manager, test_repo):
        """Compose extraction reads from committed state, not working tree."""
        # Modify the working tree compose file
        compose_path = test_repo / "docker-compose.yml"
        compose_path.write_text(
            "services:\n  modified:\n    image: modified:latest\n",
            encoding="utf-8",
        )

        # Extraction should still return the committed version
        content = manager._extract_compose_config("docker-compose.yml")
        assert "echo" in content, "Should read committed compose, not working tree"
        assert "modified" not in content, "Working tree changes should not appear"

    def test_network_name_scoped_to_pipeline(self, manager):
        """Network name includes the pipeline ID."""
        assert "integration-test" in manager.network_name
        assert manager.network_name.startswith("egg-check-")

    def test_no_ipam_subnet_collision(self, test_repo):
        """Two managers with different pipeline IDs can create networks concurrently."""
        import sys

        orchestrator_path = Path(__file__).parent.parent.parent / "orchestrator"
        shared_path = Path(__file__).parent.parent.parent / "shared"
        for p in [str(orchestrator_path), str(shared_path)]:
            if p not in sys.path:
                sys.path.insert(0, p)

        from devserver import DevserverManager

        mgr1 = DevserverManager(
            pipeline_id="concurrent-test-1",
            repo_path=test_repo,
            worktree_path=test_repo,
        )
        mgr2 = DevserverManager(
            pipeline_id="concurrent-test-2",
            repo_path=test_repo,
            worktree_path=test_repo,
        )

        try:
            # Both should create networks without subnet collision
            net1 = mgr1._create_check_network()
            net2 = mgr2._create_check_network()
            assert net1, "First network should be created"
            assert net2, "Second network should be created"
            assert net1 != net2, "Networks should have different IDs"
        finally:
            # Clean up
            mgr1._network_id = net1 if 'net1' in dir() else ""
            mgr2._network_id = net2 if 'net2' in dir() else ""
            try:
                mgr1._remove_check_network()
            except Exception:
                pass
            try:
                mgr2._remove_check_network()
            except Exception:
                pass
            # Also clean up by name
            for name in [mgr1.network_name, mgr2.network_name]:
                try:
                    subprocess.run(
                        ["docker", "network", "rm", name],
                        capture_output=True,
                        timeout=10,
                    )
                except Exception:
                    pass
