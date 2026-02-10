"""Tests for the network_mode module.

Tests per-container mode configuration.
Mode is determined from CLI flags with no persistent state.
Gateway always runs with locked Squid; mode is per-container via network selection.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.network_mode import (
    PrivateMode,
    ensure_gateway_mode,
    is_gateway_running,
)


class TestPrivateMode:
    """Tests for PrivateMode enum."""

    def test_private_mode_value(self):
        """Test PRIVATE mode has correct value."""
        assert PrivateMode.PRIVATE.value == "private"

    def test_public_mode_value(self):
        """Test PUBLIC mode has correct value."""
        assert PrivateMode.PUBLIC.value == "public"


class TestIsGatewayRunning:
    """Tests for is_gateway_running function."""

    def test_returns_true_when_gateway_reachable(self):
        """Test returns True when gateway health endpoint returns 200."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("egg_lib.network_mode.urllib.request.urlopen", return_value=mock_response):
            result = is_gateway_running()

        assert result is True

    def test_returns_false_when_gateway_not_reachable(self):
        """Test returns False when gateway is not reachable."""
        with patch(
            "egg_lib.network_mode.urllib.request.urlopen",
            side_effect=Exception("Connection refused"),
        ):
            result = is_gateway_running()

        assert result is False


class TestEnsureGatewayMode:
    """Tests for ensure_gateway_mode function.

    Mode is per-container via network selection, not gateway-wide.
    Gateway is started by start_gateway_container() if needed.
    """

    def test_returns_true_when_gateway_running(self):
        """Test returns True when gateway is running."""
        with patch("egg_lib.network_mode.is_gateway_running", return_value=True):
            result = ensure_gateway_mode(PrivateMode.PRIVATE, quiet=True)

        assert result is True

    def test_returns_true_when_gateway_running_public_mode(self):
        """Test returns True when gateway running and public mode requested."""
        with patch("egg_lib.network_mode.is_gateway_running", return_value=True):
            result = ensure_gateway_mode(PrivateMode.PUBLIC, quiet=True)

        assert result is True

    def test_returns_true_when_gateway_not_running(self):
        """Test returns True when gateway not running (will be started later)."""
        with patch("egg_lib.network_mode.is_gateway_running", return_value=False):
            result = ensure_gateway_mode(PrivateMode.PRIVATE, quiet=True)

        assert result is True

    def test_returns_true_when_gateway_not_running_public_mode(self):
        """Test returns True when gateway not running and public mode requested."""
        with patch("egg_lib.network_mode.is_gateway_running", return_value=False):
            result = ensure_gateway_mode(PrivateMode.PUBLIC, quiet=True)

        assert result is True
