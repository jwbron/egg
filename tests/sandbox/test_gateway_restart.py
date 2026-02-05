"""Tests for gateway restart confirmation logic.

Covers _confirm_gateway_restart helper and the three restart paths in
start_gateway_container that must prompt before killing the gateway.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.gateway import _confirm_gateway_restart, start_gateway_container

MODULE = "egg_lib.gateway"


class TestConfirmGatewayRestart:
    """Tests for the _confirm_gateway_restart helper."""

    @patch(f"{MODULE}._get_running_egg_containers", return_value=["egg-abc123"])
    @patch("builtins.input", return_value="y")
    def test_interactive_user_accepts(self, mock_input, mock_containers):
        assert _confirm_gateway_restart(interactive=True, reason="test reason") is True
        mock_input.assert_called_once()

    @patch(f"{MODULE}._get_running_egg_containers", return_value=["egg-abc123"])
    @patch("builtins.input", return_value="n")
    def test_interactive_user_declines(self, mock_input, mock_containers):
        assert _confirm_gateway_restart(interactive=True, reason="test reason") is False

    @patch(f"{MODULE}._get_running_egg_containers", return_value=[])
    @patch("builtins.input", return_value="y")
    def test_interactive_no_sessions_accepts(self, mock_input, mock_containers):
        assert _confirm_gateway_restart(interactive=True, reason="test reason") is True

    @patch(f"{MODULE}._get_running_egg_containers", return_value=["egg-abc123"])
    @patch("builtins.input", side_effect=EOFError)
    def test_interactive_eof_returns_false(self, mock_input, mock_containers):
        assert _confirm_gateway_restart(interactive=True, reason="test reason") is False

    @patch(f"{MODULE}._get_running_egg_containers", return_value=["egg-abc123"])
    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_interactive_keyboard_interrupt_returns_false(self, mock_input, mock_containers):
        assert _confirm_gateway_restart(interactive=True, reason="test reason") is False

    @patch(f"{MODULE}._get_running_egg_containers", return_value=["egg-abc123"])
    def test_non_interactive_returns_false(self, mock_containers):
        assert _confirm_gateway_restart(interactive=False, reason="test reason") is False

    @patch(f"{MODULE}._get_running_egg_containers", return_value=[])
    def test_non_interactive_no_sessions_returns_false(self, mock_containers):
        assert _confirm_gateway_restart(interactive=False, reason="test reason") is False


class TestStartGatewayContainerApiUnhealthy:
    """Path 1: Gateway running but API health check fails."""

    @patch(f"{MODULE}.get_force_rebuild", return_value=False)
    @patch(f"{MODULE}._confirm_gateway_restart", return_value=False)
    @patch(f"{MODULE}.wait_for_gateway_health", return_value=False)
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_api_unhealthy_non_interactive_skips_restart(
        self, mock_running, mock_health, mock_confirm, mock_force
    ):
        """When API health fails and user declines, return False."""
        result = start_gateway_container(interactive=False)
        assert result is False
        mock_confirm.assert_called_once_with(False, "gateway running but API not healthy")

    @patch(f"{MODULE}.subprocess")
    @patch(f"{MODULE}._prepare_gateway_config", return_value=([], []))
    @patch(f"{MODULE}.build_gateway_image", return_value=True)
    @patch("egg_lib.docker.ensure_gateway_networks", return_value=True)
    @patch(f"{MODULE}.get_force_rebuild", return_value=False)
    @patch(f"{MODULE}._confirm_gateway_restart", return_value=True)
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_api_unhealthy_interactive_accepts_restarts(
        self,
        mock_running,
        mock_health,
        mock_confirm,
        mock_force,
        mock_networks,
        mock_build,
        mock_config,
        mock_subprocess,
    ):
        """When API health fails and user accepts, proceed to rebuild."""
        # First call (API check) returns False, subsequent calls return True
        mock_health.side_effect = [False, True]
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="container-id")
        result = start_gateway_container(interactive=True)
        assert result is True
        mock_confirm.assert_called_once_with(True, "gateway running but API not healthy")

    @patch(f"{MODULE}.subprocess")
    @patch(f"{MODULE}._prepare_gateway_config", return_value=([], []))
    @patch(f"{MODULE}.build_gateway_image", return_value=True)
    @patch("egg_lib.docker.ensure_gateway_networks", return_value=True)
    @patch(f"{MODULE}._confirm_gateway_restart")
    @patch(f"{MODULE}.get_force_rebuild", return_value=True)
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_api_unhealthy_force_rebuild_skips_prompt(
        self,
        mock_running,
        mock_health,
        mock_force,
        mock_confirm,
        mock_networks,
        mock_build,
        mock_config,
        mock_subprocess,
    ):
        """When --rebuild is set, skip prompt and proceed directly."""
        mock_health.side_effect = [False, True]
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="container-id")
        result = start_gateway_container(interactive=False)
        assert result is True
        mock_confirm.assert_not_called()


class TestStartGatewayContainerProxyNotResponding:
    """Path 2: Gateway running, API healthy, but proxy not responding."""

    @patch(f"{MODULE}.get_force_rebuild", return_value=False)
    @patch(f"{MODULE}._confirm_gateway_restart", return_value=False)
    @patch(f"{MODULE}.should_rebuild_gateway", return_value=(False, ""))
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_proxy_broken_non_interactive_skips(
        self, mock_running, mock_health, mock_rebuild, mock_confirm, mock_force
    ):
        """When proxy fails and user declines, return False."""
        # First call (API check) returns True, second (proxy check) returns False
        mock_health.side_effect = [True, False]
        result = start_gateway_container(interactive=False)
        assert result is False
        mock_confirm.assert_called_once_with(False, "API healthy but proxy not responding")

    @patch(f"{MODULE}.subprocess")
    @patch(f"{MODULE}._prepare_gateway_config", return_value=([], []))
    @patch(f"{MODULE}.build_gateway_image", return_value=True)
    @patch("egg_lib.docker.ensure_gateway_networks", return_value=True)
    @patch(f"{MODULE}.get_force_rebuild", return_value=False)
    @patch(f"{MODULE}._confirm_gateway_restart", return_value=True)
    @patch(f"{MODULE}.should_rebuild_gateway", return_value=(False, ""))
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_proxy_broken_interactive_accepts_restarts(
        self,
        mock_running,
        mock_health,
        mock_rebuild,
        mock_confirm,
        mock_force,
        mock_networks,
        mock_build,
        mock_config,
        mock_subprocess,
    ):
        """When proxy fails and user accepts, proceed to rebuild."""
        # API check True, proxy check False, post-rebuild health True
        mock_health.side_effect = [True, False, True]
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="container-id")
        result = start_gateway_container(interactive=True)
        assert result is True
        mock_confirm.assert_called_once_with(True, "API healthy but proxy not responding")

    @patch(f"{MODULE}.subprocess")
    @patch(f"{MODULE}._prepare_gateway_config", return_value=([], []))
    @patch(f"{MODULE}.build_gateway_image", return_value=True)
    @patch("egg_lib.docker.ensure_gateway_networks", return_value=True)
    @patch(f"{MODULE}._confirm_gateway_restart")
    @patch(f"{MODULE}.get_force_rebuild", return_value=True)
    @patch(f"{MODULE}.should_rebuild_gateway", return_value=(False, ""))
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_proxy_broken_force_rebuild_skips_prompt(
        self,
        mock_running,
        mock_health,
        mock_rebuild,
        mock_force,
        mock_confirm,
        mock_networks,
        mock_build,
        mock_config,
        mock_subprocess,
    ):
        """When --rebuild is set, skip prompt and proceed directly."""
        mock_health.side_effect = [True, False, True]
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="container-id")
        result = start_gateway_container(interactive=False)
        assert result is True
        mock_confirm.assert_not_called()


class TestStartGatewayContainerRebuildNeeded:
    """Path 3 (existing): Rebuild needed - uses _confirm_gateway_restart."""

    @patch(f"{MODULE}._confirm_gateway_restart", return_value=False)
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.get_force_rebuild", return_value=False)
    @patch(f"{MODULE}.should_rebuild_gateway", return_value=(True, "config changed"))
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_rebuild_declined_healthy_gateway_continues(
        self, mock_running, mock_rebuild, mock_force, mock_health, mock_confirm
    ):
        """When rebuild declined but gateway is healthy, return True."""
        # API check True, proxy health check after decline True
        mock_health.side_effect = [True, True]
        result = start_gateway_container(interactive=True)
        assert result is True
        mock_confirm.assert_called_once_with(True, "config changed", action="rebuild")

    @patch(f"{MODULE}._confirm_gateway_restart", return_value=False)
    @patch(f"{MODULE}.wait_for_gateway_health")
    @patch(f"{MODULE}.get_force_rebuild", return_value=False)
    @patch(f"{MODULE}.should_rebuild_gateway", return_value=(True, "config changed"))
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_rebuild_declined_unhealthy_gateway_fails(
        self, mock_running, mock_rebuild, mock_force, mock_health, mock_confirm
    ):
        """When rebuild declined and gateway unhealthy, return False."""
        # API check True, proxy health check after decline False
        mock_health.side_effect = [True, False]
        result = start_gateway_container(interactive=False)
        assert result is False


class TestStartGatewayContainerHealthyPath:
    """Verify the happy path still works — no prompt when everything is fine."""

    @patch(f"{MODULE}._confirm_gateway_restart")
    @patch(f"{MODULE}.should_rebuild_gateway", return_value=(False, ""))
    @patch(f"{MODULE}.wait_for_gateway_health", return_value=True)
    @patch(f"{MODULE}.is_gateway_running", return_value=True)
    def test_healthy_gateway_returns_true_no_prompt(
        self, mock_running, mock_health, mock_rebuild, mock_confirm
    ):
        """Healthy gateway returns True without prompting."""
        result = start_gateway_container(interactive=True)
        assert result is True
        mock_confirm.assert_not_called()
