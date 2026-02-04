"""
Tests for the container entrypoint module.

Tests the container initialization logic:
- Config dataclass and property methods
- Logger with quiet mode
- Utility functions (run_cmd, chown_recursive)
- Setup functions with mocked filesystem

Note: Most setup functions require root and container environment,
so we focus on testing logic that can be unit tested.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Load the entrypoint module
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))
import entrypoint


class TestConfig:
    """Tests for the Config dataclass."""

    def test_default_values(self, monkeypatch):
        """Test default configuration values."""
        # Clear environment variables to test defaults
        monkeypatch.delenv("RUNTIME_UID", raising=False)
        monkeypatch.delenv("RUNTIME_GID", raising=False)
        monkeypatch.delenv("EGG_QUIET", raising=False)

        config = entrypoint.Config()

        assert config.container_user == "egg"  # Fixed user, not configurable
        assert config.runtime_uid == 1000
        assert config.runtime_gid == 1000
        assert config.quiet is False

    def test_environment_overrides(self, monkeypatch):
        """Test that environment variables override defaults."""
        # Note: container_user is fixed as "egg", only UID/GID can be overridden
        monkeypatch.setenv("RUNTIME_UID", "2000")
        monkeypatch.setenv("RUNTIME_GID", "2000")
        monkeypatch.setenv("EGG_QUIET", "1")

        config = entrypoint.Config()

        assert config.container_user == "egg"  # Always fixed
        assert config.runtime_uid == 2000
        assert config.runtime_gid == 2000
        assert config.quiet is True

    def test_api_keys_from_environment(self, monkeypatch):
        """Test that API keys are read from environment."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
        monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")

        config = entrypoint.Config()

        assert config.anthropic_api_key == "test-anthropic-key"
        assert config.github_token == "test-github-token"

    def test_user_home_property(self, monkeypatch):
        """Test the user_home property - always /home/egg with fixed user."""
        config = entrypoint.Config()

        assert config.user_home == Path("/home/egg")

    def test_repos_dir_property(self, monkeypatch):
        """Test the repos_dir property - always under /home/egg."""
        config = entrypoint.Config()

        assert config.repos_dir == Path("/home/egg/repos")

    def test_sharing_dir_property(self, monkeypatch):
        """Test the sharing_dir property - always under /home/egg."""
        config = entrypoint.Config()

        assert config.sharing_dir == Path("/home/egg/sharing")

    def test_derived_paths(self, monkeypatch):
        """Test all derived path properties - all under /home/egg."""
        config = entrypoint.Config()

        assert config.claude_dir == Path("/home/egg/.claude")


class TestLogger:
    """Tests for the Logger class."""

    def test_info_shown_when_not_quiet(self, capsys):
        """Test that info messages are shown when not quiet."""
        logger = entrypoint.Logger(quiet=False)

        logger.info("test message")
        captured = capsys.readouterr()

        assert "test message" in captured.out

    def test_info_hidden_when_quiet(self, capsys):
        """Test that info messages are hidden when quiet."""
        logger = entrypoint.Logger(quiet=True)

        logger.info("test message")
        captured = capsys.readouterr()

        assert captured.out == ""

    def test_success_shown_when_not_quiet(self, capsys):
        """Test that success messages are shown with checkmark."""
        logger = entrypoint.Logger(quiet=False)

        logger.success("task completed")
        captured = capsys.readouterr()

        assert "task completed" in captured.out

    def test_success_hidden_when_quiet(self, capsys):
        """Test that success messages are hidden when quiet."""
        logger = entrypoint.Logger(quiet=True)

        logger.success("task completed")
        captured = capsys.readouterr()

        assert captured.out == ""

    def test_warn_always_shown(self, capsys):
        """Test that warnings are always shown, even in quiet mode."""
        logger = entrypoint.Logger(quiet=True)

        logger.warn("warning message")
        captured = capsys.readouterr()

        assert "warning message" in captured.out

    def test_error_always_shown(self, capsys):
        """Test that errors are always shown to stderr."""
        logger = entrypoint.Logger(quiet=True)

        logger.error("error message")
        captured = capsys.readouterr()

        assert "error message" in captured.err


class TestRunCmd:
    """Tests for the run_cmd utility function."""

    @patch("subprocess.run")
    def test_run_cmd_basic(self, mock_run):
        """Test basic command execution."""
        mock_run.return_value = MagicMock(returncode=0)

        result = entrypoint.run_cmd(["echo", "hello"])

        mock_run.assert_called_once()
        assert result.returncode == 0

    @patch("subprocess.run")
    def test_run_cmd_with_gosu(self, mock_run):
        """Test command execution as different user via gosu."""
        mock_run.return_value = MagicMock(returncode=0)

        entrypoint.run_cmd(["echo", "hello"], as_user=(1000, 1000))

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "gosu"
        assert call_args[1] == "1000:1000"
        assert call_args[2:] == ["echo", "hello"]

    @patch("subprocess.run")
    def test_run_cmd_with_capture(self, mock_run):
        """Test command execution with output capture."""
        mock_run.return_value = MagicMock(returncode=0, stdout="output")

        entrypoint.run_cmd(["cat", "file"], capture=True)

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["capture_output"] is True
        assert call_kwargs["text"] is True

    @patch("subprocess.run")
    def test_run_cmd_timeout(self, mock_run):
        """Test command execution with custom timeout."""
        mock_run.return_value = MagicMock(returncode=0)

        entrypoint.run_cmd(["sleep", "1"], timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 60


class TestChownRecursive:
    """Tests for the chown_recursive utility function."""

    @patch.object(entrypoint, "run_cmd")
    def test_chown_recursive_calls_chown(self, mock_run_cmd):
        """Test that chown_recursive calls chown with correct args."""
        test_path = Path("/test/path")

        entrypoint.chown_recursive(test_path, 1000, 1000)

        mock_run_cmd.assert_called_once_with(["chown", "-R", "1000:1000", "/test/path"])


class TestSetupEnvironment:
    """Tests for the setup_environment function."""

    def test_sets_home_and_user(self, monkeypatch):
        """Test that HOME and USER are set correctly - always egg."""
        config = entrypoint.Config()

        entrypoint.setup_environment(config)

        assert os.environ["HOME"] == "/home/egg"
        assert os.environ["USER"] == "egg"

    def test_sets_python_flags(self, monkeypatch):
        """Test that Python environment flags are set."""
        config = entrypoint.Config()

        entrypoint.setup_environment(config)

        assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
        assert os.environ["PYTHONUNBUFFERED"] == "1"

    def test_sets_disable_autoupdater(self, monkeypatch):
        """Test that Claude autoupdater is disabled."""
        config = entrypoint.Config()

        entrypoint.setup_environment(config)

        assert os.environ["DISABLE_AUTOUPDATER"] == "1"

    def test_updates_path(self, monkeypatch):
        """Test that PATH is updated with egg runtime scripts and local bin."""
        monkeypatch.setenv("PATH", "/usr/bin")
        config = entrypoint.Config()

        entrypoint.setup_environment(config)

        assert "/opt/egg-runtime/sandbox/bin" in os.environ["PATH"]
        assert "/home/egg/.local/bin" in os.environ["PATH"]


class TestSetupSharing:
    """Tests for the setup_sharing function."""

    @patch.object(entrypoint, "chown_recursive")
    def test_creates_subdirectories(self, mock_chown, temp_dir, monkeypatch):
        """Test that subdirectories are created."""
        sharing_dir = temp_dir / "sharing"
        sharing_dir.mkdir()

        config = MagicMock()
        config.sharing_dir = sharing_dir
        config.user_home = temp_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=True)
        entrypoint.setup_sharing(config, logger)

        # Check subdirectories exist
        assert (sharing_dir / "tmp").exists()
        assert (sharing_dir / "context").exists()
        assert (sharing_dir / "tracking").exists()
        assert (sharing_dir / "traces").exists()
        assert (sharing_dir / "logs").exists()

    @patch.object(entrypoint, "chown_recursive")
    def test_creates_tmp_symlink(self, mock_chown, temp_dir, monkeypatch):
        """Test that ~/tmp symlink is created."""
        sharing_dir = temp_dir / "sharing"
        sharing_dir.mkdir()

        config = MagicMock()
        config.sharing_dir = sharing_dir
        config.user_home = temp_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=True)
        entrypoint.setup_sharing(config, logger)

        # Check symlink exists
        tmp_link = temp_dir / "tmp"
        assert tmp_link.is_symlink()
        assert tmp_link.resolve() == (sharing_dir / "tmp").resolve()


class TestSetupClaude:
    """Tests for setup_claude function."""

    @patch.object(entrypoint, "chown_recursive")
    @patch("os.chown")
    @patch("os.chmod")
    def test_handles_ebusy_with_fallback(
        self, mock_chmod, mock_chown, mock_chown_recursive, temp_dir, capsys
    ):
        """Test that EBUSY error falls back to direct file write."""
        import errno

        # Set up directories
        claude_dir = temp_dir / ".claude"
        claude_dir.mkdir()

        # Create an existing .claude.json (simulating bind mount scenario)
        user_state_file = temp_dir / ".claude.json"
        user_state_file.write_text('{"existingKey": "value"}')

        config = MagicMock()
        config.user_home = temp_dir
        config.claude_dir = claude_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000
        config.quiet = True

        logger = entrypoint.Logger(quiet=True)

        # Mock os.replace to raise EBUSY
        with patch("os.replace") as mock_replace:
            mock_replace.side_effect = OSError(errno.EBUSY, "Device or resource busy")

            entrypoint.setup_claude(config, logger)

        # Verify the file was still updated via fallback
        import json

        result = json.loads(user_state_file.read_text())
        assert result["hasCompletedOnboarding"] is True
        assert result["autoUpdates"] is False
        assert result["existingKey"] == "value"  # Original content preserved

        # Verify warning was logged (Logger.warn outputs to stdout, not stderr)
        captured = capsys.readouterr()
        assert "bind-mounted" in captured.out

    @patch.object(entrypoint, "chown_recursive")
    @patch("os.chown")
    @patch("os.chmod")
    def test_normal_atomic_write(self, mock_chmod, mock_chown, mock_chown_recursive, temp_dir):
        """Test normal atomic write path works."""
        # Set up directories
        claude_dir = temp_dir / ".claude"
        claude_dir.mkdir()

        config = MagicMock()
        config.user_home = temp_dir
        config.claude_dir = claude_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000
        config.quiet = True

        logger = entrypoint.Logger(quiet=True)

        entrypoint.setup_claude(config, logger)

        # Verify .claude.json was created with required settings
        import json

        user_state_file = temp_dir / ".claude.json"
        result = json.loads(user_state_file.read_text())
        assert result["hasCompletedOnboarding"] is True
        assert result["autoUpdates"] is False
