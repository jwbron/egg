"""Tests for the network_mode module.

Tests per-container mode configuration.
Mode is determined from CLI flags with no persistent state.
Gateway always runs with locked Squid; mode is per-container via network selection.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.network_mode import (
    PrivateMode,
    ensure_gateway_mode,
    get_gateway_current_mode,
    get_private_mode_env_vars,
)


class TestPrivateMode:
    """Tests for PrivateMode enum."""

    def test_private_mode_value(self):
        """Test PRIVATE mode has correct value."""
        assert PrivateMode.PRIVATE.value == "private"

    def test_public_mode_value(self):
        """Test PUBLIC mode has correct value."""
        assert PrivateMode.PUBLIC.value == "public"


class TestGetPrivateModeEnvVars:
    """Tests for get_private_mode_env_vars function."""

    def test_public_mode_env_vars(self):
        """Test PUBLIC mode returns PRIVATE_MODE=false."""
        env_vars = get_private_mode_env_vars(PrivateMode.PUBLIC)
        assert env_vars == {"PRIVATE_MODE": "false"}

    def test_private_mode_env_vars(self):
        """Test PRIVATE mode returns PRIVATE_MODE=true."""
        env_vars = get_private_mode_env_vars(PrivateMode.PRIVATE)
        assert env_vars == {"PRIVATE_MODE": "true"}


class TestGetGatewayCurrentMode:
    """Tests for get_gateway_current_mode function."""

    def test_returns_private_when_gateway_reports_private(self):
        """Test returns PRIVATE when gateway health reports private_mode=true."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"private_mode": True}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("egg_lib.network_mode.urllib.request.urlopen", return_value=mock_response):
            result = get_gateway_current_mode()

        assert result == PrivateMode.PRIVATE

    def test_returns_public_when_gateway_reports_public(self):
        """Test returns PUBLIC when gateway health reports private_mode=false."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"private_mode": False}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("egg_lib.network_mode.urllib.request.urlopen", return_value=mock_response):
            result = get_gateway_current_mode()

        assert result == PrivateMode.PUBLIC

    def test_returns_none_when_gateway_not_reachable(self):
        """Test returns None when gateway is not reachable."""
        with patch(
            "egg_lib.network_mode.urllib.request.urlopen",
            side_effect=Exception("Connection refused"),
        ):
            result = get_gateway_current_mode()

        assert result is None


class TestEnsureGatewayMode:
    """Tests for ensure_gateway_mode function.

    The new architecture no longer restarts the gateway on mode switch.
    Mode is per-container via network selection, not gateway-wide.
    """

    def test_no_restart_when_gateway_running_private_mode(self):
        """Test does not restart gateway when it's running (private mode)."""
        with patch(
            "egg_lib.network_mode.get_gateway_current_mode",
            return_value=PrivateMode.PRIVATE,
        ):
            result = ensure_gateway_mode(PrivateMode.PRIVATE, quiet=True)

        assert result is True

    def test_no_restart_when_gateway_running_public_mode(self):
        """Test does not restart gateway when it's running (public mode requested)."""
        # Gateway always runs in private mode now, but we shouldn't restart
        # even when public mode is requested - mode is per-container
        with patch(
            "egg_lib.network_mode.get_gateway_current_mode",
            return_value=PrivateMode.PRIVATE,
        ):
            result = ensure_gateway_mode(PrivateMode.PUBLIC, quiet=True)

        # Should succeed without restart
        assert result is True

    def test_no_restart_on_mode_switch(self):
        """Test no gateway restart on mode switch (new architecture)."""
        # In the old architecture, switching modes would restart the gateway.
        # In the new architecture, no restart is needed - mode is per-container.
        with patch(
            "egg_lib.network_mode.get_gateway_current_mode",
            return_value=PrivateMode.PRIVATE,
        ):
            # Request public mode when gateway reports private
            result = ensure_gateway_mode(PrivateMode.PUBLIC, quiet=True)

        # Should succeed - no restart needed
        assert result is True

    def test_gateway_not_running_succeeds(self):
        """Test succeeds when gateway is not running (will be started by start_gateway_container)."""
        with patch("egg_lib.network_mode.get_gateway_current_mode", return_value=None):
            result = ensure_gateway_mode(PrivateMode.PRIVATE, quiet=True)

        assert result is True

    def test_gateway_not_running_public_mode_succeeds(self):
        """Test succeeds when gateway not running and public mode requested."""
        with patch("egg_lib.network_mode.get_gateway_current_mode", return_value=None):
            result = ensure_gateway_mode(PrivateMode.PUBLIC, quiet=True)

        assert result is True
