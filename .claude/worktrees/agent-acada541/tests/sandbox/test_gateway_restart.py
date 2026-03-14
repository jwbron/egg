"""Tests for gateway restart/rebuild paths in start_gateway_container.

Since sessions survive gateway restarts (persistent session storage),
the gateway proceeds with restarts and rebuilds without prompting the
user for confirmation.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.gateway import start_gateway_container

MODULE = "egg_lib.gateway"


class TestStartGatewayContainerHealthyPath:
    """Verify the happy path — no restart when everything is fine."""

    @patch(f"{MODULE}.should_rebuild_gateway", return_value=(False, ""))
    @patch(f"{MODULE}.wait_for_gateway_health", return_value=True)
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_healthy_gateway_returns_true(self, mock_running, mock_health, mock_rebuild):
        """Healthy gateway returns True without restarting."""
        result = start_gateway_container()
        assert result is True


class TestStartGatewayContainerApiUnhealthy:
    """Path: Gateway running but API health check fails."""

    @patch(f"{MODULE}.subprocess")
    @patch(f"{MODULE}._prepare_gateway_config", return_value=([], []))
    @patch(f"{MODULE}.build_gateway_image", return_value=True)
    @patch("egg_lib.docker.ensure_gateway_networks", return_value=True)
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_api_unhealthy_proceeds_to_restart(
        self,
        mock_running,
        mock_health,
        mock_networks,
        mock_build,
        mock_config,
        mock_subprocess,
    ):
        """When API health fails, proceed to restart without prompting."""
        # First call (API check) returns False, post-rebuild health True
        mock_health.side_effect = [False, True]
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="container-id")
        result = start_gateway_container()
        assert result is True


class TestStartGatewayContainerProxyNotResponding:
    """Path: Gateway running, API healthy, but proxy not responding."""

    @patch(f"{MODULE}.subprocess")
    @patch(f"{MODULE}._prepare_gateway_config", return_value=([], []))
    @patch(f"{MODULE}.build_gateway_image", return_value=True)
    @patch("egg_lib.docker.ensure_gateway_networks", return_value=True)
    @patch(f"{MODULE}.should_rebuild_gateway", return_value=(False, ""))
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_proxy_broken_proceeds_to_restart(
        self,
        mock_running,
        mock_health,
        mock_rebuild,
        mock_networks,
        mock_build,
        mock_config,
        mock_subprocess,
    ):
        """When proxy fails, proceed to restart without prompting."""
        # API check True, proxy check False, post-rebuild health True
        mock_health.side_effect = [True, False, True]
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="container-id")
        result = start_gateway_container()
        assert result is True


class TestStartGatewayContainerRebuildNeeded:
    """Path: Rebuild needed due to config/image change."""

    @patch(f"{MODULE}.subprocess")
    @patch(f"{MODULE}._prepare_gateway_config", return_value=([], []))
    @patch(f"{MODULE}.build_gateway_image", return_value=True)
    @patch("egg_lib.docker.ensure_gateway_networks", return_value=True)
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.should_rebuild_gateway", return_value=(True, "config changed"))
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_rebuild_needed_proceeds_without_prompt(
        self,
        mock_running,
        mock_rebuild,
        mock_health,
        mock_networks,
        mock_build,
        mock_config,
        mock_subprocess,
    ):
        """When rebuild is needed, proceed without prompting."""
        # API check True, post-rebuild health True
        mock_health.side_effect = [True, True]
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="container-id")
        result = start_gateway_container()
        assert result is True


class TestStartGatewayContainerNotRunning:
    """Path: Gateway is not running at all."""

    @patch(f"{MODULE}.subprocess")
    @patch(f"{MODULE}._prepare_gateway_config", return_value=([], []))
    @patch(f"{MODULE}.build_gateway_image", return_value=True)
    @patch("egg_lib.docker.ensure_gateway_networks", return_value=True)
    @patch(f"{MODULE}.wait_for_gateway_health", return_value=True)
    @patch(f"{MODULE}.is_gateway_running", return_value=False)
    def test_not_running_starts_fresh(
        self,
        mock_running,
        mock_health,
        mock_networks,
        mock_build,
        mock_config,
        mock_subprocess,
    ):
        """When gateway is not running, start it fresh."""
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="container-id")
        result = start_gateway_container()
        assert result is True
