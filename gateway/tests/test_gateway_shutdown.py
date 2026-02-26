"""Tests for gateway graceful shutdown behavior."""

import signal
from unittest.mock import MagicMock, patch

import pytest


class TestGatewayShutdown:
    """Tests for SIGTERM graceful shutdown handler."""

    def test_sigterm_handler_registered(self):
        """Verify SIGTERM handler is registered in main()."""
        # The gateway module is loaded by conftest.py
        import gateway

        with (
            patch.object(gateway, "get_github_client") as mock_ghc,
            patch.object(gateway, "get_session_manager") as mock_sm,
            patch.object(gateway, "get_active_docker_containers", return_value=set()),
            patch.object(gateway, "startup_cleanup", return_value=0),
            patch.object(gateway, "get_launcher_secret", return_value="secret"),
            patch.object(gateway, "serve"),
            patch("signal.signal") as mock_signal,
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch.object(gateway.os, "getuid", return_value=1000),
        ):
            mock_args.return_value = MagicMock(host="0.0.0.0", port=9848, debug=False)
            mock_ghc.return_value = MagicMock(
                validate_user_mode_config=MagicMock(return_value=(True, "ok"))
            )
            mock_sm.return_value = MagicMock(
                prune_expired_sessions=MagicMock(return_value=0),
                list_sessions=MagicMock(return_value=[]),
            )

            gateway.main()

            # Find the SIGTERM registration call
            sigterm_calls = [c for c in mock_signal.call_args_list if c[0][0] == signal.SIGTERM]
            assert len(sigterm_calls) == 1
            handler = sigterm_calls[0][0][1]
            assert callable(handler)

    def test_graceful_shutdown_delays(self):
        """Verify the shutdown handler sleeps before exiting."""
        import gateway

        with (
            patch.object(gateway, "get_github_client") as mock_ghc,
            patch.object(gateway, "get_session_manager") as mock_sm,
            patch.object(gateway, "get_active_docker_containers", return_value=set()),
            patch.object(gateway, "startup_cleanup", return_value=0),
            patch.object(gateway, "get_launcher_secret", return_value="secret"),
            patch.object(gateway, "serve"),
            patch("signal.signal") as mock_signal,
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch.object(gateway.os, "getuid", return_value=1000),
        ):
            mock_args.return_value = MagicMock(host="0.0.0.0", port=9848, debug=False)
            mock_ghc.return_value = MagicMock(
                validate_user_mode_config=MagicMock(return_value=(True, "ok"))
            )
            mock_sm.return_value = MagicMock(
                prune_expired_sessions=MagicMock(return_value=0),
                list_sessions=MagicMock(return_value=[]),
            )

            gateway.main()

            # Extract the registered SIGTERM handler
            sigterm_calls = [c for c in mock_signal.call_args_list if c[0][0] == signal.SIGTERM]
            handler = sigterm_calls[0][0][1]

            # Invoke the handler and verify it sleeps 5s then exits
            with (
                patch.object(gateway.time, "sleep") as mock_sleep,
                pytest.raises(SystemExit) as exc_info,
            ):
                handler(signal.SIGTERM, None)

            mock_sleep.assert_called_once_with(5)
            assert exc_info.value.code == 0
