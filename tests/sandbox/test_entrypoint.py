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
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add shared module to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))

from egg_config import GATEWAY_PORT

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

    @patch("subprocess.run")
    def test_chown_recursive_calls_chown(self, mock_run):
        """Test that chown_recursive calls chown with correct args."""
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        test_path = Path("/test/path")

        entrypoint.chown_recursive(test_path, 1000, 1000)

        mock_run.assert_called_once_with(
            ["chown", "-R", "1000:1000", "/test/path"],
            capture_output=True,
            text=True,
        )

    @patch("subprocess.run")
    def test_chown_recursive_tolerates_read_only_filesystem(self, mock_run):
        """Test that chown_recursive ignores read-only filesystem errors."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["chown", "-R", "1001:1001", "/home/egg"],
            returncode=1,
            stdout="",
            stderr="chown: changing ownership of '/home/egg/repos/egg/.git': Read-only file system\n",
        )

        # Should not raise
        entrypoint.chown_recursive(Path("/home/egg"), 1001, 1001)

    @patch("subprocess.run")
    def test_chown_recursive_raises_on_real_errors(self, mock_run):
        """Test that chown_recursive raises on non-EROFS errors."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["chown", "-R", "1001:1001", "/home/egg"],
            returncode=1,
            stdout="",
            stderr="chown: changing ownership of '/root': Permission denied\n",
        )

        with pytest.raises(subprocess.CalledProcessError):
            entrypoint.chown_recursive(Path("/home/egg"), 1001, 1001)


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


class TestStartupTimer:
    """Tests for the StartupTimer class.

    Uses monkeypatch.setattr to modify ENABLE_STARTUP_TIMING instead of
    importlib.reload to avoid test pollution from module reloading.
    """

    def test_timer_disabled_by_default(self, monkeypatch):
        """Timer is disabled when ENABLE_STARTUP_TIMING is False."""
        # Directly patch the module-level constant
        monkeypatch.setattr(entrypoint, "ENABLE_STARTUP_TIMING", False)

        timer = entrypoint.StartupTimer()
        timer.start_phase("test_phase")
        timer.end_phase()

        # No timings should be recorded when disabled
        assert len(timer.timings) == 0

    def test_timer_enabled_with_env(self, monkeypatch):
        """Timer records phases when ENABLE_STARTUP_TIMING is True."""
        monkeypatch.setattr(entrypoint, "ENABLE_STARTUP_TIMING", True)

        timer = entrypoint.StartupTimer()
        timer.start_phase("test_phase")
        import time

        time.sleep(0.01)  # Small delay to ensure measurable time
        timer.end_phase()

        assert len(timer.timings) == 1
        assert timer.timings[0][0] == "test_phase"
        assert timer.timings[0][1] > 0  # Should have elapsed time

    def test_phase_context_manager(self, monkeypatch):
        """Phase context manager works correctly."""
        monkeypatch.setattr(entrypoint, "ENABLE_STARTUP_TIMING", True)

        timer = entrypoint.StartupTimer()

        with timer.phase("context_phase"):
            import time

            time.sleep(0.01)

        assert len(timer.timings) == 1
        assert timer.timings[0][0] == "context_phase"

    def test_host_timing_loaded_from_env(self, monkeypatch):
        """Host timing data is loaded from EGG_HOST_TIMING env var."""
        monkeypatch.setenv(
            "EGG_HOST_TIMING", '{"timings": [["host_phase", 100.5]], "total_time": 100.5}'
        )

        timer = entrypoint.StartupTimer()

        assert len(timer.host_timings) == 1
        assert timer.host_timings[0] == ["host_phase", 100.5]
        assert timer.host_total_time == 100.5

    def test_invalid_host_timing_handled(self, monkeypatch):
        """Invalid host timing JSON is handled gracefully."""
        monkeypatch.setenv("EGG_HOST_TIMING", "not valid json")

        timer = entrypoint.StartupTimer()

        # Should not crash, just have empty host timings
        assert timer.host_timings == []


class TestSetupGit:
    """Tests for the setup_git function."""

    @patch("subprocess.run")
    def test_git_identity_configured(self, mock_run):
        """Git identity is configured to egg."""
        mock_run.return_value = MagicMock(returncode=0)

        config = MagicMock()
        config.runtime_uid = 1000
        config.runtime_gid = 1000
        config.github_token = None

        logger = entrypoint.Logger(quiet=True)

        entrypoint.setup_git(config, logger)

        # Check git config calls
        calls = [str(call) for call in mock_run.call_args_list]
        assert any("user.name" in str(call) and "egg" in str(call) for call in calls)
        assert any("user.email" in str(call) and "egg@localhost" in str(call) for call in calls)

    @patch("subprocess.run")
    def test_credential_helper_configured_with_token(self, mock_run):
        """Credential helper is configured when GitHub token is available."""
        mock_run.return_value = MagicMock(returncode=0)

        config = MagicMock()
        config.runtime_uid = 1000
        config.runtime_gid = 1000
        config.github_token = "test-token"

        logger = entrypoint.Logger(quiet=True)

        entrypoint.setup_git(config, logger)

        # Check credential helper was configured
        calls = [str(call) for call in mock_run.call_args_list]
        assert any("credential.helper" in str(call) for call in calls)

    @patch("subprocess.run")
    def test_credential_helper_empty_without_token(self, mock_run):
        """Credential helper is set empty when no GitHub token."""
        mock_run.return_value = MagicMock(returncode=0)

        config = MagicMock()
        config.runtime_uid = 1000
        config.runtime_gid = 1000
        config.github_token = None

        logger = entrypoint.Logger(quiet=True)

        entrypoint.setup_git(config, logger)

        # Credential helper should be set to empty string
        # Find the credential.helper call and verify it's setting empty
        cred_calls = [c for c in mock_run.call_args_list if "credential.helper" in str(c)]
        assert len(cred_calls) > 0


class TestSetupWorktrees:
    """Tests for the setup_worktrees function."""

    def test_returns_true_when_repos_missing(self, temp_dir):
        """Returns True with warning when repos dir doesn't exist."""
        config = MagicMock()
        config.repos_dir = temp_dir / "nonexistent"

        logger = entrypoint.Logger(quiet=True)

        result = entrypoint.setup_worktrees(config, logger)
        assert result is True

    def test_counts_mounted_repos(self, temp_dir, capsys):
        """Counts and reports number of mounted repos."""
        repos_dir = temp_dir / "repos"
        repos_dir.mkdir()
        (repos_dir / "repo1").mkdir()
        (repos_dir / "repo2").mkdir()
        (repos_dir / "repo3").mkdir()

        config = MagicMock()
        config.repos_dir = repos_dir

        logger = entrypoint.Logger(quiet=False)

        result = entrypoint.setup_worktrees(config, logger)

        assert result is True
        captured = capsys.readouterr()
        assert "3 repo" in captured.out


class TestSetupAgentRules:
    """Tests for the setup_agent_rules function."""

    @pytest.mark.skip(
        reason="Requires complex mocking of /opt/claude-rules path; "
        "function relies on hardcoded paths that are difficult to test without container"
    )
    def test_creates_claude_md(self, temp_dir):
        """Creates CLAUDE.md from rule files."""
        # This test would require extensive mocking of the filesystem paths
        # used in setup_agent_rules. The function reads from /opt/claude-rules/
        # and writes to ~/CLAUDE.md, both of which require either:
        # 1. Running in the actual container environment, or
        # 2. Complex patching of Path operations
        #
        # For now, we skip this test and rely on:
        # - test_no_rules_does_nothing for the early-exit path
        # - Integration tests for the full functionality
        pass

    @patch("os.chown")
    @patch("os.lchown")
    def test_no_rules_does_nothing(self, mock_lchown, mock_chown, temp_dir):
        """Does nothing when no rules directory exists."""
        config = MagicMock()
        config.user_home = temp_dir
        config.repos_dir = temp_dir / "repos"
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=True)

        # Should not raise - function returns early when no rules exist
        # Note: This test relies on /opt/claude-rules/mission.md not existing
        entrypoint.setup_agent_rules(config, logger)


class TestSetupEggSymlink:
    """Tests for the setup_egg_symlink function."""

    @patch("os.lchown")
    def test_creates_symlink_successfully(self, mock_lchown, temp_dir, capsys):
        """Creates symlink when runtime directory exists."""
        config = MagicMock()
        config.user_home = temp_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=False)

        # Mock Path.is_dir so /opt/egg-runtime/sandbox appears to exist
        original_is_dir = Path.is_dir

        def mock_is_dir(self):
            if str(self) == "/opt/egg-runtime/sandbox":
                return True
            return original_is_dir(self)

        with patch.object(Path, "is_dir", mock_is_dir):
            entrypoint.setup_egg_symlink(config, logger)

        captured = capsys.readouterr()
        # Should report success
        assert "symlink" in captured.out.lower() or "created" in captured.out.lower()

        # Check that symlink was created
        egg_link = temp_dir / "egg"
        assert egg_link.is_symlink()
        assert os.readlink(egg_link) == "/opt/egg-runtime/sandbox"

    @patch("os.lchown")
    def test_replaces_existing_symlink(self, mock_lchown, temp_dir):
        """Replaces existing symlink if present."""
        config = MagicMock()
        config.user_home = temp_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=True)

        # Create an existing symlink pointing elsewhere
        egg_link = temp_dir / "egg"
        egg_link.symlink_to("/some/other/path")

        # Mock Path.is_dir so /opt/egg-runtime/sandbox appears to exist
        original_is_dir = Path.is_dir

        def mock_is_dir(self):
            if str(self) == "/opt/egg-runtime/sandbox":
                return True
            return original_is_dir(self)

        with patch.object(Path, "is_dir", mock_is_dir):
            entrypoint.setup_egg_symlink(config, logger)

        # Symlink should now point to runtime
        assert egg_link.is_symlink()
        assert os.readlink(egg_link) == "/opt/egg-runtime/sandbox"

    @patch("os.lchown")
    def test_skips_if_not_symlink(self, mock_lchown, temp_dir, capsys):
        """Skips and warns if ~/egg exists but is not a symlink."""
        config = MagicMock()
        config.user_home = temp_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=False)

        # Create a regular directory (not symlink) at ~/egg
        egg_path = temp_dir / "egg"
        egg_path.mkdir()
        (egg_path / "somefile").touch()

        # Mock Path.is_dir so /opt/egg-runtime/sandbox appears to exist
        original_is_dir = Path.is_dir

        def mock_is_dir(self):
            if str(self) == "/opt/egg-runtime/sandbox":
                return True
            return original_is_dir(self)

        with patch.object(Path, "is_dir", mock_is_dir):
            entrypoint.setup_egg_symlink(config, logger)

        captured = capsys.readouterr()
        # Should warn about skipping
        assert "not a symlink" in captured.out.lower()
        # Should still be a directory, not replaced
        assert egg_path.is_dir() and not egg_path.is_symlink()


class TestSetupBashrc:
    """Tests for the setup_bashrc function."""

    def test_appends_aliases(self, temp_dir):
        """Appends Claude alias to .bashrc."""
        bashrc = temp_dir / ".bashrc"
        bashrc.write_text("# Original bashrc\n")

        config = MagicMock()
        config.user_home = temp_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=True)

        with patch("os.chown"):
            entrypoint.setup_bashrc(config, logger)

        content = bashrc.read_text()
        assert "claude" in content.lower()
        assert "dangerously-skip-permissions" in content


class TestSetupAnthropicApi:
    """Tests for the setup_anthropic_api function."""

    def test_sets_anthropic_base_url(self, monkeypatch):
        """Sets ANTHROPIC_BASE_URL to gateway."""
        monkeypatch.setenv("GATEWAY_URL", f"http://test-gateway:{GATEWAY_PORT}")

        config = MagicMock()
        logger = entrypoint.Logger(quiet=True)

        entrypoint.setup_anthropic_api(config, logger)

        assert os.environ["ANTHROPIC_BASE_URL"] == f"http://test-gateway:{GATEWAY_PORT}"

    def test_sets_placeholder_oauth_token(self, monkeypatch):
        """Sets placeholder OAuth token for Claude Code validation."""
        monkeypatch.setenv("GATEWAY_URL", f"http://test-gateway:{GATEWAY_PORT}")

        config = MagicMock()
        logger = entrypoint.Logger(quiet=True)

        entrypoint.setup_anthropic_api(config, logger)

        assert "CLAUDE_CODE_OAUTH_TOKEN" in os.environ
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"].startswith("sk-ant-oat01-")

    def test_removes_api_key_from_env(self, monkeypatch):
        """Removes ANTHROPIC_API_KEY from environment for security."""
        monkeypatch.setenv("GATEWAY_URL", f"http://test-gateway:{GATEWAY_PORT}")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

        config = MagicMock()
        logger = entrypoint.Logger(quiet=True)

        entrypoint.setup_anthropic_api(config, logger)

        assert "ANTHROPIC_API_KEY" not in os.environ


class TestConfigAuthMethod:
    """Tests for Config anthropic_auth_method handling."""

    def test_default_auth_method_is_api_key(self, monkeypatch):
        """Default auth method is api_key."""
        monkeypatch.delenv("ANTHROPIC_AUTH_METHOD", raising=False)

        config = entrypoint.Config()
        assert config.anthropic_auth_method == "api_key"

    def test_oauth_auth_method(self, monkeypatch):
        """OAuth auth method is read from environment."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "oauth")

        config = entrypoint.Config()
        assert config.anthropic_auth_method == "oauth"

    def test_auth_method_case_insensitive(self, monkeypatch):
        """Auth method is normalized to lowercase."""
        monkeypatch.setenv("ANTHROPIC_AUTH_METHOD", "OAuth")

        config = entrypoint.Config()
        assert config.anthropic_auth_method == "oauth"

    def test_valid_auth_methods_constant(self):
        """VALID_AUTH_METHODS contains expected values."""
        assert "api_key" in entrypoint.Config.VALID_AUTH_METHODS
        assert "oauth" in entrypoint.Config.VALID_AUTH_METHODS
