"""Unit tests for CLI runtime module."""

from unittest.mock import MagicMock, patch

from cli.runtime import (
    RuntimeConfig,
    check_docker,
    container_exists,
    container_running,
    network_exists,
)


class TestRuntimeConfig:
    """Tests for RuntimeConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = RuntimeConfig()
        assert config.gateway_container == "egg-gateway"
        assert config.sandbox_container == "egg-sandbox"
        assert config.gateway_image == "egg-gateway:latest"
        assert config.sandbox_image == "egg-sandbox:latest"
        assert config.network_name == "egg-isolated"
        assert config.gateway_port == 9847
        assert config.proxy_port == 3128


class TestCheckDocker:
    """Tests for check_docker function."""

    def test_docker_not_installed(self):
        """Test when docker is not installed."""
        with patch("cli.runtime.shutil.which", return_value=None):
            result = check_docker()
            assert result is False

    def test_docker_installed_but_not_running(self):
        """Test when docker is installed but daemon not running."""
        with (
            patch("cli.runtime.shutil.which", return_value="/usr/bin/docker"),
            patch("cli.runtime.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1)
            result = check_docker()
            assert result is False

    def test_docker_installed_and_running(self):
        """Test when docker is installed and running."""
        with (
            patch("cli.runtime.shutil.which", return_value="/usr/bin/docker"),
            patch("cli.runtime.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = check_docker()
            assert result is True


class TestContainerHelpers:
    """Tests for container helper functions."""

    def test_container_running_true(self):
        """Test container_running when container is running."""
        with patch("cli.runtime.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="true\n",
            )
            result = container_running("test-container")
            assert result is True

    def test_container_running_false(self):
        """Test container_running when container is not running."""
        with patch("cli.runtime.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="false\n",
            )
            result = container_running("test-container")
            assert result is False

    def test_container_running_not_found(self):
        """Test container_running when container doesn't exist."""
        with patch("cli.runtime.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = container_running("test-container")
            assert result is False

    def test_container_exists_true(self):
        """Test container_exists when container exists."""
        with patch("cli.runtime.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = container_exists("test-container")
            assert result is True

    def test_container_exists_false(self):
        """Test container_exists when container doesn't exist."""
        with patch("cli.runtime.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = container_exists("test-container")
            assert result is False

    def test_network_exists_true(self):
        """Test network_exists when network exists."""
        with patch("cli.runtime.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = network_exists("test-network")
            assert result is True

    def test_network_exists_false(self):
        """Test network_exists when network doesn't exist."""
        with patch("cli.runtime.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = network_exists("test-network")
            assert result is False
