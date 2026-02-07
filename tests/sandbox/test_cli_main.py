"""Tests for sandbox/egg_lib/cli.py - CLI entry point."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.cli import main


def _cli_patches(*extra_patches):
    """Common patches needed for all CLI tests.

    main() always calls ensure_gateway_mode() before Docker checks,
    and set_force_rebuild(). These must be mocked in every test.
    """
    base = [
        patch("egg_lib.cli.init_statusbar"),
        patch("egg_lib.cli.ensure_gateway_mode", return_value=True),
        patch("egg_lib.cli.set_force_rebuild"),
        patch("egg_lib.cli.set_quiet_mode"),
    ]
    return base + list(extra_patches)


class TestMain:
    """Tests for main() CLI entry point."""

    def test_setup_flag(self):
        """--setup flag triggers setup flow."""
        with patch("sys.argv", ["egg", "--setup"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.setup", return_value=True),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 0
            finally:
                for p in patches:
                    p.stop()

    def test_setup_failure(self):
        """--setup returns 1 on failure."""
        with patch("sys.argv", ["egg", "--setup"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.setup", return_value=False),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 1
            finally:
                for p in patches:
                    p.stop()

    def test_ensure_gateway_mode_failure(self):
        """Returns 1 when ensure_gateway_mode fails."""
        with patch("sys.argv", ["egg"]):
            with patch("egg_lib.cli.init_statusbar"):
                with patch("egg_lib.cli.set_force_rebuild"):
                    with patch("egg_lib.cli.set_quiet_mode"):
                        with patch("egg_lib.cli.ensure_gateway_mode", return_value=False):
                            result = main()
                            assert result == 1

    def test_docker_check_failure(self):
        """Returns 1 when Docker check fails."""
        with patch("sys.argv", ["egg"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=False),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 1
            finally:
                for p in patches:
                    p.stop()

    def test_docker_permissions_failure(self):
        """Returns 1 when Docker permissions check fails."""
        with patch("sys.argv", ["egg"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=False),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 1
            finally:
                for p in patches:
                    p.stop()

    def test_host_setup_check(self):
        """Checks host setup before running."""
        with patch("sys.argv", ["egg"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.run_claude", return_value=True),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 0
            finally:
                for p in patches:
                    p.stop()

    def test_exec_mode(self):
        """--exec flag triggers exec_in_new_container."""
        with patch("sys.argv", ["egg", "--exec", "echo", "hello"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.exec_in_new_container", return_value=True),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 0
            finally:
                for p in patches:
                    p.stop()

    def test_private_mode_flag(self):
        """--private flag sets private mode."""
        with patch("sys.argv", ["egg", "--private"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.run_claude", return_value=True),
            )
            started = []
            for p in patches:
                started.append(p.start())
            try:
                result = main()
                assert result == 0
                # run_claude is the last patch started
                mock_run = started[-1]
                call_kwargs = mock_run.call_args
                assert "private" in str(call_kwargs)
            finally:
                for p in patches:
                    p.stop()

    def test_public_mode_flag(self):
        """--public flag sets public mode."""
        with patch("sys.argv", ["egg", "--public"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.run_claude", return_value=True),
            )
            started = []
            for p in patches:
                started.append(p.start())
            try:
                result = main()
                assert result == 0
                mock_run = started[-1]
                call_kwargs = mock_run.call_args
                assert "public" in str(call_kwargs)
            finally:
                for p in patches:
                    p.stop()

    def test_rebuild_flag(self):
        """--rebuild flag forces rebuild."""
        with patch("sys.argv", ["egg", "--rebuild"]):
            mock_rebuild = MagicMock()
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.run_claude", return_value=True),
            )
            # Replace set_force_rebuild with our tracked mock
            patches[2] = patch("egg_lib.cli.set_force_rebuild", mock_rebuild)
            for p in patches:
                p.start()
            try:
                main()
                mock_rebuild.assert_called_once_with(True)
            finally:
                for p in patches:
                    p.stop()

    def test_verbose_flag(self):
        """--verbose flag does not crash."""
        with patch("sys.argv", ["egg", "-v"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.run_claude", return_value=True),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 0
            finally:
                for p in patches:
                    p.stop()

    def test_host_setup_failure(self):
        """Returns 1 when host setup check fails."""
        with patch("sys.argv", ["egg"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=False),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 1
            finally:
                for p in patches:
                    p.stop()

    def test_exec_mode_failure(self):
        """--exec returns 1 on failure."""
        with patch("sys.argv", ["egg", "--exec", "false"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.exec_in_new_container", return_value=False),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 1
            finally:
                for p in patches:
                    p.stop()

    def test_reset_flag(self):
        """--reset flag resets configuration."""
        with patch("sys.argv", ["egg", "--reset"]):
            mock_config = MagicMock()
            mock_config.CONFIG_DIR.exists.return_value = False
            mock_config.USER_CONFIG_DIR.exists.return_value = False
            patches = _cli_patches(
                patch("egg_lib.cli.Config", mock_config),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 0
            finally:
                for p in patches:
                    p.stop()

    def test_run_claude_failure(self):
        """Returns 1 when run_claude returns False."""
        with patch("sys.argv", ["egg"]):
            patches = _cli_patches(
                patch("egg_lib.cli.check_docker", return_value=True),
                patch("egg_lib.cli.check_docker_permissions", return_value=True),
                patch("egg_lib.cli.check_host_setup", return_value=True),
                patch("egg_lib.cli.run_claude", return_value=False),
            )
            for p in patches:
                p.start()
            try:
                result = main()
                assert result == 1
            finally:
                for p in patches:
                    p.stop()
