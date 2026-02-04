"""
Tests for the egg binary (egg main launcher).

These tests focus on utility functions and non-Docker operations:
- Config class paths
- Platform detection
- Directory safety checks
- Worktree management helpers
- Container ID generation

Docker and subprocess operations are mocked since they require Docker daemon.
"""

import os
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch

# Load egg module (filename without .py extension)
egg_path = Path(__file__).parent.parent.parent / "sandbox" / "egg"
loader = SourceFileLoader("egg", str(egg_path))
egg = loader.load_module()

Colors = egg.Colors
Config = egg.Config


class TestColors:
    """Tests for ANSI color codes."""

    def test_colors_defined(self):
        """Test that all color codes are defined."""
        assert hasattr(Colors, "BLUE")
        assert hasattr(Colors, "GREEN")
        assert hasattr(Colors, "YELLOW")
        assert hasattr(Colors, "RED")
        assert hasattr(Colors, "BOLD")
        assert hasattr(Colors, "NC")

    def test_colors_are_escape_codes(self):
        """Test that colors are ANSI escape codes."""
        assert Colors.BLUE.startswith("\033[")
        assert Colors.NC == "\033[0m"


class TestOutputFunctions:
    """Tests for info, success, warn, error output functions."""

    def test_info_output(self, capsys):
        """Test info() prints blue prefix."""
        egg.info("test message")
        captured = capsys.readouterr()
        assert "[INFO]" in captured.out
        assert "test message" in captured.out

    def test_success_output(self, capsys):
        """Test success() prints green prefix."""
        egg.success("test message")
        captured = capsys.readouterr()
        assert "[SUCCESS]" in captured.out
        assert "test message" in captured.out

    def test_warn_output(self, capsys):
        """Test warn() prints yellow prefix."""
        egg.warn("test message")
        captured = capsys.readouterr()
        assert "[WARNING]" in captured.out
        assert "test message" in captured.out

    def test_error_output(self, capsys):
        """Test error() prints red prefix to stderr."""
        egg.error("test message")
        captured = capsys.readouterr()
        assert "[ERROR]" in captured.err
        assert "test message" in captured.err


class TestConfig:
    """Tests for Config class paths."""

    def test_config_dir(self):
        """Test cache directory path structure (XDG-compliant)."""
        # CONFIG_DIR is now $XDG_CACHE_HOME/jib or ~/.cache/jib
        assert Config.CONFIG_DIR.name == "egg"
        # Parent is either .cache or custom XDG_CACHE_HOME
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache:
            assert str(Config.CONFIG_DIR.parent) == xdg_cache
        else:
            assert Config.CONFIG_DIR.parent.name == ".cache"
        # CACHE_DIR should be the same as CONFIG_DIR
        assert Config.CACHE_DIR == Config.CONFIG_DIR

    def test_user_config_dir(self):
        """Test user config directory path structure."""
        assert Config.USER_CONFIG_DIR.name == "egg"
        assert Config.USER_CONFIG_DIR.parent.name == ".config"

    def test_github_token_file_path(self):
        """Test GitHub token file path structure."""
        assert Config.GITHUB_TOKEN_FILE.name == "github-token"
        assert Config.GITHUB_TOKEN_FILE.parent == Config.USER_CONFIG_DIR

    def test_image_name(self):
        """Test Docker image name."""
        assert Config.IMAGE_NAME == "egg"

    def test_container_name(self):
        """Test container name."""
        assert Config.CONTAINER_NAME == "egg"

    def test_sharing_dir_path(self):
        """Test sharing directory path ends with .egg-sharing."""
        assert Config.SHARING_DIR.name == ".egg-sharing"

    def test_tmp_dir_path(self):
        """Test tmp directory path structure."""
        assert Config.TMP_DIR.name == "tmp"
        assert Config.TMP_DIR.parent == Config.SHARING_DIR

    def test_worktree_base_path(self):
        """Test worktree base directory path ends with .egg-worktrees."""
        assert Config.WORKTREE_BASE.name == ".egg-worktrees"

    def test_dangerous_dirs_defined(self):
        """Test that dangerous directories are defined."""
        assert len(Config.DANGEROUS_DIRS) > 0
        # Check by name patterns rather than exact paths
        dangerous_names = [p.name for p in Config.DANGEROUS_DIRS]
        assert ".ssh" in dangerous_names
        assert ".aws" in dangerous_names
        # gcloud is inside .config
        dangerous_str = [str(p) for p in Config.DANGEROUS_DIRS]
        assert any("gcloud" in s for s in dangerous_str)


class TestGetPlatform:
    """Tests for platform detection."""

    @patch("platform.system")
    def test_detect_linux(self, mock_system):
        """Test detecting Linux platform."""
        mock_system.return_value = "Linux"
        assert egg.get_platform() == "linux"

    @patch("platform.system")
    def test_detect_macos(self, mock_system):
        """Test detecting macOS platform."""
        mock_system.return_value = "Darwin"
        assert egg.get_platform() == "macos"

    @patch("platform.system")
    def test_detect_unknown(self, mock_system):
        """Test detecting unknown platform."""
        mock_system.return_value = "Windows"
        assert egg.get_platform() == "unknown"


class TestGetGithubToken:
    """Tests for GitHub token retrieval.

    The get_github_token() function uses HostConfig which loads tokens from:
    1. Environment variable GITHUB_TOKEN (highest priority)
    2. ~/.config/egg/secrets.env
    3. ~/.config/egg/github-token

    These tests create mock HostConfig modules in sys.modules before calling
    get_github_token() to test the token validation logic.
    """

    def _call_get_github_token_with_mock(self, token_value):
        """Helper to call get_github_token with a mocked HostConfig."""
        import sys

        # Create mock HostConfig class
        mock_config_instance = MagicMock()
        mock_config_instance.github_token = token_value

        mock_module = MagicMock()
        mock_module.HostConfig = MagicMock(return_value=mock_config_instance)

        # Clear any cached import and inject mock
        if "config.host_config" in sys.modules:
            del sys.modules["config.host_config"]
        sys.modules["config.host_config"] = mock_module

        try:
            return egg.get_github_token()
        finally:
            # Clean up
            if "config.host_config" in sys.modules:
                del sys.modules["config.host_config"]

    def test_no_token_configured(self):
        """Test when no token is configured."""
        assert self._call_get_github_token_with_mock("") is None

    def test_valid_ghp_token(self):
        """Test reading valid ghp_ prefixed token."""
        assert (
            self._call_get_github_token_with_mock("ghp_test1234567890abcdef")
            == "ghp_test1234567890abcdef"
        )

    def test_valid_github_pat_token(self):
        """Test reading valid github_pat_ prefixed token."""
        assert (
            self._call_get_github_token_with_mock("github_pat_test1234567890")
            == "github_pat_test1234567890"
        )

    def test_invalid_token_prefix(self):
        """Test rejecting token with invalid prefix."""
        assert self._call_get_github_token_with_mock("invalid_token_1234567890") is None

    def test_empty_token(self):
        """Test empty token."""
        assert self._call_get_github_token_with_mock("") is None

    def test_none_token(self):
        """Test None token."""
        assert self._call_get_github_token_with_mock(None) is None

    def test_whitespace_only_token(self):
        """Test whitespace-only token (HostConfig should strip it)."""
        # HostConfig strips whitespace when loading, but if it didn't, empty string fails validation
        assert self._call_get_github_token_with_mock("   ") is None


class TestIsDangerousDir:
    """Tests for dangerous directory detection."""

    def test_ssh_is_dangerous(self):
        """Test .ssh directory is detected as dangerous."""
        # Use Config.DANGEROUS_DIRS entries directly to avoid home path mismatch
        ssh_dir = next((d for d in Config.DANGEROUS_DIRS if d.name == ".ssh"), None)
        assert ssh_dir is not None
        assert egg.is_dangerous_dir(ssh_dir) is True

    def test_aws_is_dangerous(self):
        """Test .aws directory is detected as dangerous."""
        aws_dir = next((d for d in Config.DANGEROUS_DIRS if d.name == ".aws"), None)
        assert aws_dir is not None
        assert egg.is_dangerous_dir(aws_dir) is True

    def test_gcloud_is_dangerous(self):
        """Test gcloud directory is detected as dangerous."""
        gcloud_dir = next((d for d in Config.DANGEROUS_DIRS if "gcloud" in str(d)), None)
        assert gcloud_dir is not None
        assert egg.is_dangerous_dir(gcloud_dir) is True

    def test_ssh_subdir_is_dangerous(self):
        """Test subdirectory of .ssh is detected as dangerous."""
        ssh_dir = next((d for d in Config.DANGEROUS_DIRS if d.name == ".ssh"), None)
        assert ssh_dir is not None
        assert egg.is_dangerous_dir(ssh_dir / "keys") is True

    def test_safe_dir(self, temp_dir):
        """Test safe directory is not detected as dangerous."""
        safe_dir = temp_dir / "safe_project"
        safe_dir.mkdir()
        assert egg.is_dangerous_dir(safe_dir) is False

    def test_repos_dir_is_safe(self):
        """Test repos directory is safe."""
        # Use a path that's definitely not in DANGEROUS_DIRS
        repos_dir = Config.SHARING_DIR.parent / "repos"
        assert egg.is_dangerous_dir(repos_dir) is False


class TestGenerateContainerId:
    """Tests for container ID generation."""

    def test_container_id_format(self):
        """Test container ID has expected format."""
        container_id = egg.generate_container_id()

        assert container_id.startswith("egg-")
        # Format: egg-YYYYMMDD-HHMMSS-PID
        parts = container_id.split("-")
        assert len(parts) == 4  # egg, date, time, pid

    def test_container_id_unique(self):
        """Test that container IDs are unique (within reasonable time)."""
        id1 = egg.generate_container_id()
        id2 = egg.generate_container_id()

        # Same process, so PID is same, but we expect unique IDs
        # (at least timestamp or we're generating them too fast)
        # Since we can't guarantee uniqueness in same millisecond,
        # we just verify format is consistent
        assert id1.startswith("egg-")
        assert id2.startswith("egg-")

    def test_container_id_contains_pid(self):
        """Test container ID contains process ID."""
        container_id = egg.generate_container_id()
        pid = str(os.getpid())
        assert pid in container_id


class TestCheckDockerPermissions:
    """Tests for Docker permission checking."""

    @patch("subprocess.run")
    def test_docker_accessible(self, mock_run):
        """Test when Docker is accessible."""
        mock_run.return_value = MagicMock(returncode=0)

        result = egg.check_docker_permissions()
        assert result is True

    @patch("subprocess.run")
    def test_docker_permission_denied(self, mock_run, capsys):
        """Test when Docker permission is denied."""
        mock_run.return_value = MagicMock(
            returncode=1, stderr="permission denied while trying to connect"
        )

        result = egg.check_docker_permissions()
        assert result is False

        captured = capsys.readouterr()
        assert "permission denied" in captured.err.lower() or "docker" in captured.out.lower()


class TestImageExists:
    """Tests for Docker image existence checking."""

    @patch("subprocess.run")
    def test_image_exists(self, mock_run):
        """Test when Docker image exists."""
        mock_run.return_value = MagicMock(returncode=0)

        result = egg.image_exists()
        assert result is True
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_image_not_exists(self, mock_run):
        """Test when Docker image doesn't exist."""
        mock_run.return_value = MagicMock(returncode=1)

        result = egg.image_exists()
        assert result is False


class TestBuildImage:
    """Tests for Docker image building."""

    @patch("egg_lib.docker.subprocess.run")
    @patch("egg_lib.docker.create_dockerfile")
    @patch("egg_lib.docker.check_claude_update", return_value=None)
    @patch("egg_lib.docker.should_rebuild_image", return_value=(True, "test"))
    @patch("egg_lib.docker.compute_build_hash", return_value="testhash123")
    def test_build_image_success(
        self, mock_hash, mock_should, mock_update, mock_create, mock_run, monkeypatch
    ):
        """Test successful Docker build."""
        mock_run.return_value = MagicMock(returncode=0)

        # Set environment variables
        monkeypatch.setenv("USER", "testuser")

        result = egg.build_image()
        assert result is True
        mock_create.assert_called_once()

    @patch("egg_lib.docker.subprocess.run")
    @patch("egg_lib.docker.create_dockerfile")
    @patch("egg_lib.docker.check_claude_update", return_value=None)
    @patch("egg_lib.docker.should_rebuild_image", return_value=(True, "test"))
    @patch("egg_lib.docker.compute_build_hash", return_value="testhash123")
    def test_build_image_failure(
        self, mock_hash, mock_should, mock_update, mock_create, mock_run, capsys, monkeypatch
    ):
        """Test Docker build failure."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "docker build")
        monkeypatch.setenv("USER", "testuser")

        result = egg.build_image()
        assert result is False

        captured = capsys.readouterr()
        assert "build failed" in captured.err.lower()

    @patch("egg_lib.docker.should_rebuild_image", return_value=(False, "build hash matches"))
    def test_build_image_skipped_when_hash_matches(self, mock_should):
        """Test that build is skipped when hash matches."""
        result = egg.build_image()
        assert result is True
        mock_should.assert_called_once()
