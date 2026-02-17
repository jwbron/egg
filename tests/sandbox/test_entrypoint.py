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

    def test_sets_egg_repo_path_when_not_set(self, monkeypatch):
        """Test that EGG_REPO_PATH is set to ~/repos when not already set."""
        monkeypatch.delenv("EGG_REPO_PATH", raising=False)
        config = entrypoint.Config()

        entrypoint.setup_environment(config)

        assert os.environ["EGG_REPO_PATH"] == "/home/egg/repos"

    def test_preserves_existing_egg_repo_path(self, monkeypatch):
        """Test that EGG_REPO_PATH is not overridden if already set."""
        monkeypatch.setenv("EGG_REPO_PATH", "/custom/path")
        config = entrypoint.Config()

        entrypoint.setup_environment(config)

        assert os.environ["EGG_REPO_PATH"] == "/custom/path"


class TestSetupClaude:
    """Tests for setup_claude function."""

    @patch.object(entrypoint, "chown_recursive")
    @patch("os.chown")
    @patch("os.chmod")
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_handles_ebusy_with_fallback(
        self, mock_which, mock_chmod, mock_chown, mock_chown_recursive, temp_dir, capsys
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
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_normal_atomic_write(
        self, mock_which, mock_chmod, mock_chown, mock_chown_recursive, temp_dir
    ):
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

    @patch("shutil.which", return_value=None)
    def test_exits_when_claude_binary_not_found(self, mock_which, temp_dir, capsys):
        """Test that setup_claude calls sys.exit(1) when claude binary is missing."""
        config = MagicMock()
        config.user_home = temp_dir

        logger = entrypoint.Logger(quiet=False)

        with pytest.raises(SystemExit) as exc_info:
            entrypoint.setup_claude(config, logger)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Claude Code CLI not found in PATH" in captured.err


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

    @patch("os.lchown")
    @patch("os.chown")
    def test_includes_all_rules_in_any_session(
        self, mock_chown, mock_lchown, temp_dir, monkeypatch
    ):
        """All rules including CLI tools are included regardless of pipeline mode."""
        monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)

        # Create mock rules directory
        rules_dir = temp_dir / "opt-claude-rules"
        rules_dir.mkdir()
        all_rules = [
            "mission.md",
            "environment.md",
            "code-standards.md",
            "test-workflow.md",
            "pr-descriptions.md",
            "orchestrator.md",
            "contract.md",
            "checkpoint.md",
        ]
        for f in all_rules:
            (rules_dir / f).write_text(f"# {f} content")

        # Create repos dir for symlink
        repos_dir = temp_dir / "repos"
        repos_dir.mkdir()

        config = MagicMock()
        config.user_home = temp_dir
        config.repos_dir = repos_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=True)

        # Patch Path("/opt/claude-rules") to point to our temp rules dir
        original_path_init = Path.__new__

        def patched_path_new(cls, *args, **kwargs):
            result = original_path_init(cls, *args, **kwargs)
            if str(result) == "/opt/claude-rules":
                return rules_dir
            return result

        with patch.object(Path, "__new__", patched_path_new):
            entrypoint.setup_agent_rules(config, logger)

        claude_md = temp_dir / "CLAUDE.md"
        content = claude_md.read_text()
        for f in all_rules:
            assert f"{f} content" in content, f"Missing rule: {f}"

    @patch("os.lchown")
    @patch("os.chown")
    def test_core_rules_order_preserved(self, mock_chown, mock_lchown, temp_dir, monkeypatch):
        """All rules are included in the expected order."""
        monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)

        rules_dir = temp_dir / "opt-claude-rules"
        rules_dir.mkdir()
        core_rules = [
            "mission.md",
            "environment.md",
            "code-standards.md",
            "test-workflow.md",
            "pr-descriptions.md",
            "orchestrator.md",
            "contract.md",
            "checkpoint.md",
        ]
        for f in core_rules:
            (rules_dir / f).write_text(f"## {f} marker")

        repos_dir = temp_dir / "repos"
        repos_dir.mkdir()

        config = MagicMock()
        config.user_home = temp_dir
        config.repos_dir = repos_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=True)

        original_path_init = Path.__new__

        def patched_path_new(cls, *args, **kwargs):
            result = original_path_init(cls, *args, **kwargs)
            if str(result) == "/opt/claude-rules":
                return rules_dir
            return result

        with patch.object(Path, "__new__", patched_path_new):
            entrypoint.setup_agent_rules(config, logger)

        claude_md = temp_dir / "CLAUDE.md"
        content = claude_md.read_text()

        # Verify all core rules present and in order
        positions = []
        for f in core_rules:
            marker = f"## {f} marker"
            pos = content.find(marker)
            assert pos >= 0, f"Missing rule: {f}"
            positions.append(pos)
        assert positions == sorted(positions), "Core rules are not in expected order"

    @patch("os.lchown")
    @patch("os.chown")
    def test_missing_optional_rule_file_skipped(
        self, mock_chown, mock_lchown, temp_dir, monkeypatch
    ):
        """Missing individual rule files are gracefully skipped."""
        monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)

        rules_dir = temp_dir / "opt-claude-rules"
        rules_dir.mkdir()
        # Only create mission.md and code-standards.md, skip the rest
        (rules_dir / "mission.md").write_text("# Mission")
        (rules_dir / "code-standards.md").write_text("# Code Standards")

        repos_dir = temp_dir / "repos"
        repos_dir.mkdir()

        config = MagicMock()
        config.user_home = temp_dir
        config.repos_dir = repos_dir
        config.runtime_uid = 1000
        config.runtime_gid = 1000

        logger = entrypoint.Logger(quiet=True)

        original_path_init = Path.__new__

        def patched_path_new(cls, *args, **kwargs):
            result = original_path_init(cls, *args, **kwargs)
            if str(result) == "/opt/claude-rules":
                return rules_dir
            return result

        with patch.object(Path, "__new__", patched_path_new):
            entrypoint.setup_agent_rules(config, logger)

        claude_md = temp_dir / "CLAUDE.md"
        content = claude_md.read_text()
        assert "# Mission" in content
        assert "# Code Standards" in content

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


class TestOrchestratorMode:
    """Tests for orchestrator mode support in entrypoint."""

    def test_config_orchestrator_mode_default(self, monkeypatch):
        """Default orchestrator mode is None (not in orchestrator mode)."""
        monkeypatch.delenv("EGG_ORCHESTRATOR_MODE", raising=False)
        monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)
        monkeypatch.delenv("EGG_ORCHESTRATOR_URL", raising=False)

        config = entrypoint.Config()
        assert config.is_orchestrator_mode is False

    def test_config_orchestrator_mode_enabled(self, monkeypatch):
        """Orchestrator mode is enabled when env vars are set."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "distributed")
        monkeypatch.setenv("EGG_PIPELINE_ID", "issue-123")
        monkeypatch.setenv("EGG_AGENT_ROLE", "coder")

        config = entrypoint.Config()
        assert config.is_orchestrator_mode is True
        assert config.orchestrator_mode == "distributed"
        assert config.pipeline_id == "issue-123"
        assert config.agent_role == "coder"


class TestRunInteractiveSubprocess:
    """Tests for run_interactive using subprocess.Popen() with stderr capture."""

    @patch("subprocess.Popen")
    def test_run_interactive_captures_stderr(self, mock_popen, monkeypatch, tmp_path):
        """run_interactive captures stderr to log file while passing through."""
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stderr.readline.return_value = b""
        mock_popen.return_value = mock_process

        monkeypatch.setenv("RUNTIME_UID", "1000")
        monkeypatch.setenv("RUNTIME_GID", "1000")

        config = entrypoint.Config()
        config._repos_dir = Path("/tmp/test-repos")
        logger = entrypoint.Logger(quiet=True)

        with patch.object(Path, "exists", return_value=True):
            with patch("os.chdir"):
                exit_code = entrypoint.run_interactive(config, logger)

        assert exit_code == 0
        # Verify Popen was called with stderr=PIPE for capture
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["stderr"] == subprocess.PIPE

    @patch("subprocess.Popen")
    def test_run_interactive_returns_exit_code(self, mock_popen, monkeypatch):
        """run_interactive returns subprocess exit code."""
        mock_process = MagicMock()
        mock_process.returncode = 42
        mock_process.stderr.readline.return_value = b""
        mock_popen.return_value = mock_process

        monkeypatch.setenv("RUNTIME_UID", "1000")
        monkeypatch.setenv("RUNTIME_GID", "1000")

        config = entrypoint.Config()
        config._repos_dir = Path("/tmp/test-repos")
        logger = entrypoint.Logger(quiet=True)

        with patch.object(Path, "exists", return_value=True):
            with patch("os.chdir"):
                exit_code = entrypoint.run_interactive(config, logger)

        assert exit_code == 42


class TestRunExecSubprocess:
    """Tests for run_exec using subprocess.Popen() with stderr capture."""

    @patch("subprocess.Popen")
    def test_run_exec_captures_stderr(self, mock_popen, monkeypatch):
        """run_exec captures stderr to log file while passing through."""
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stderr.readline.return_value = b""
        mock_popen.return_value = mock_process

        monkeypatch.setenv("RUNTIME_UID", "1000")
        monkeypatch.setenv("RUNTIME_GID", "1000")

        config = entrypoint.Config()
        logger = entrypoint.Logger(quiet=True)

        exit_code = entrypoint.run_exec(config, logger, ["echo", "test"])

        assert exit_code == 0
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["stderr"] == subprocess.PIPE

    @patch("subprocess.Popen")
    def test_run_exec_returns_exit_code(self, mock_popen, monkeypatch):
        """run_exec returns subprocess exit code."""
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stderr.readline.return_value = b""
        mock_popen.return_value = mock_process

        monkeypatch.setenv("RUNTIME_UID", "1000")
        monkeypatch.setenv("RUNTIME_GID", "1000")

        config = entrypoint.Config()
        logger = entrypoint.Logger(quiet=True)

        exit_code = entrypoint.run_exec(config, logger, ["false"])

        assert exit_code == 1


class TestSignalOrchestratorCompletion:
    """Tests for signal_orchestrator_completion function."""

    def test_no_signal_when_not_orchestrator_mode(self, monkeypatch):
        """Does not signal when not in orchestrator mode."""
        monkeypatch.delenv("EGG_ORCHESTRATOR_MODE", raising=False)
        monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)

        config = entrypoint.Config()
        logger = entrypoint.Logger(quiet=True)

        # Should not raise and not call any orchestrator code
        with patch("egg_orchestrator.OrchestratorClient") as mock_client:
            entrypoint.signal_orchestrator_completion(config, logger, exit_code=0)
            mock_client.assert_not_called()

    def test_signal_complete_on_success(self, monkeypatch):
        """Signals complete when exit code is 0."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "distributed")
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator:8080")
        monkeypatch.setenv("EGG_PIPELINE_ID", "issue-123")
        monkeypatch.setenv("EGG_AGENT_ROLE", "coder")

        config = entrypoint.Config()
        logger = entrypoint.Logger(quiet=True)

        mock_response = MagicMock()
        mock_response.success = True
        mock_response.message = ""

        with patch("egg_orchestrator.OrchestratorClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.signal_complete.return_value = mock_response
            MockClient.return_value = mock_client_instance

            entrypoint.signal_orchestrator_completion(config, logger, exit_code=0)

            mock_client_instance.signal_complete.assert_called_once_with(
                pipeline_id="issue-123",
                agent_role="coder",
            )
            mock_client_instance.signal_error.assert_not_called()

    def test_signal_error_on_failure(self, monkeypatch):
        """Signals error when exit code is non-zero."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "distributed")
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator:8080")
        monkeypatch.setenv("EGG_PIPELINE_ID", "issue-456")
        monkeypatch.setenv("EGG_AGENT_ROLE", "tester")

        config = entrypoint.Config()
        logger = entrypoint.Logger(quiet=True)

        mock_response = MagicMock()
        mock_response.success = True
        mock_response.message = ""

        with patch("egg_orchestrator.OrchestratorClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.signal_error.return_value = mock_response
            MockClient.return_value = mock_client_instance

            entrypoint.signal_orchestrator_completion(config, logger, exit_code=1)

            mock_client_instance.signal_error.assert_called_once()
            call_kwargs = mock_client_instance.signal_error.call_args[1]
            assert call_kwargs["pipeline_id"] == "issue-456"
            assert call_kwargs["agent_role"] == "tester"
            assert "exit" in call_kwargs["error"].lower() or "1" in call_kwargs["error"]
            assert call_kwargs["recoverable"] is False
            mock_client_instance.signal_complete.assert_not_called()

    def test_signal_error_with_custom_message(self, monkeypatch):
        """Signals error with custom error message."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "distributed")
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator:8080")
        monkeypatch.setenv("EGG_PIPELINE_ID", "issue-789")
        monkeypatch.setenv("EGG_AGENT_ROLE", "reviewer")

        config = entrypoint.Config()
        logger = entrypoint.Logger(quiet=True)

        mock_response = MagicMock()
        mock_response.success = True
        mock_response.message = ""

        with patch("egg_orchestrator.OrchestratorClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.signal_error.return_value = mock_response
            MockClient.return_value = mock_client_instance

            entrypoint.signal_orchestrator_completion(
                config, logger, exit_code=128, error_message="SIGTERM received"
            )

            call_kwargs = mock_client_instance.signal_error.call_args[1]
            assert call_kwargs["error"] == "SIGTERM received"

    def test_handles_signal_failure_gracefully(self, monkeypatch, capsys):
        """Handles orchestrator signaling failure without crashing."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "distributed")
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator:8080")
        monkeypatch.setenv("EGG_PIPELINE_ID", "issue-999")
        monkeypatch.setenv("EGG_AGENT_ROLE", "coder")

        config = entrypoint.Config()
        logger = entrypoint.Logger(quiet=False)

        with patch("egg_orchestrator.OrchestratorClient") as MockClient:
            mock_client_instance = MagicMock()
            mock_client_instance.signal_complete.side_effect = Exception("Connection failed")
            MockClient.return_value = mock_client_instance

            # Should not raise
            entrypoint.signal_orchestrator_completion(config, logger, exit_code=0)

        captured = capsys.readouterr()
        assert "failed" in captured.out.lower() or "Connection failed" in captured.out


class TestCleanupOnExitSignaling:
    """Tests for cleanup_on_exit orchestrator signaling integration."""

    def test_cleanup_calls_signal_on_success(self, monkeypatch):
        """cleanup_on_exit calls signal_orchestrator_completion on success."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "distributed")
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator:8080")
        monkeypatch.setenv("EGG_PIPELINE_ID", "issue-100")
        monkeypatch.setenv("EGG_AGENT_ROLE", "coder")

        config = entrypoint.Config()
        logger = entrypoint.Logger(quiet=True)

        with patch.object(entrypoint, "signal_orchestrator_completion") as mock_signal:
            entrypoint.cleanup_on_exit(config, logger, exit_code=0)

            mock_signal.assert_called_once_with(config, logger, 0)

    def test_cleanup_calls_signal_on_error(self, monkeypatch):
        """cleanup_on_exit calls signal_orchestrator_completion on error."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "distributed")
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator:8080")
        monkeypatch.setenv("EGG_PIPELINE_ID", "issue-200")
        monkeypatch.setenv("EGG_AGENT_ROLE", "tester")

        config = entrypoint.Config()
        logger = entrypoint.Logger(quiet=True)

        with patch.object(entrypoint, "signal_orchestrator_completion") as mock_signal:
            entrypoint.cleanup_on_exit(config, logger, exit_code=1)

            mock_signal.assert_called_once_with(config, logger, 1)


class TestResolveGidConflict:
    """Tests for _resolve_gid_conflict helper (macOS GID collision fix)."""

    @patch("subprocess.run")
    def test_renames_conflicting_group(self, mock_run):
        """Renames a conflicting group when target GID is already taken."""
        mock_run.return_value = MagicMock(returncode=0)
        logger = entrypoint.Logger(quiet=True)

        # Simulate: egg group has GID 1000, but target GID 20 is held by "dialout"
        mock_grp_egg = MagicMock(gr_gid=1000, gr_name="egg")
        mock_grp_dialout = MagicMock(gr_gid=20, gr_name="dialout")

        def getgrnam_side_effect(name):
            if name == "egg":
                return mock_grp_egg
            if name == "_orig_dialout":
                raise KeyError("not found")
            raise KeyError(f"no group: {name}")

        with patch("grp.getgrnam", side_effect=getgrnam_side_effect):
            with patch("grp.getgrgid", return_value=mock_grp_dialout):
                entrypoint._resolve_gid_conflict(20, "egg", logger)

        # Should have called groupmod to rename dialout
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args == ["groupmod", "-n", "_orig_dialout", "dialout"]

    @patch("subprocess.run")
    def test_no_rename_when_no_conflict(self, mock_run):
        """Does nothing when target GID is not taken."""
        logger = entrypoint.Logger(quiet=True)

        mock_grp_egg = MagicMock(gr_gid=1000, gr_name="egg")

        with patch("grp.getgrnam", return_value=mock_grp_egg):
            with patch("grp.getgrgid", side_effect=KeyError("not found")):
                entrypoint._resolve_gid_conflict(20, "egg", logger)

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_no_rename_when_already_matching(self, mock_run):
        """Does nothing when egg group already has the target GID."""
        logger = entrypoint.Logger(quiet=True)

        mock_grp_egg = MagicMock(gr_gid=20, gr_name="egg")

        with patch("grp.getgrnam", return_value=mock_grp_egg):
            entrypoint._resolve_gid_conflict(20, "egg", logger)

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_no_rename_when_getgrgid_returns_our_group(self, mock_run):
        """Does nothing when getgrgid returns our own group name."""
        logger = entrypoint.Logger(quiet=True)

        mock_grp_egg_by_name = MagicMock(gr_gid=1000, gr_name="egg")
        mock_grp_egg_by_gid = MagicMock(gr_gid=20, gr_name="egg")

        with patch("grp.getgrnam", return_value=mock_grp_egg_by_name):
            with patch("grp.getgrgid", return_value=mock_grp_egg_by_gid):
                entrypoint._resolve_gid_conflict(20, "egg", logger)

        mock_run.assert_not_called()


class TestResolveUidConflict:
    """Tests for _resolve_uid_conflict helper (macOS UID collision fix)."""

    @patch("subprocess.run")
    def test_reassigns_conflicting_user(self, mock_run):
        """Reassigns a conflicting user to a high UID."""
        mock_run.return_value = MagicMock(returncode=0)
        logger = entrypoint.Logger(quiet=True)

        mock_pwd_egg = MagicMock(pw_uid=1000, pw_name="egg")
        mock_pwd_other = MagicMock(pw_uid=501, pw_name="ubuntu")

        def getpwuid_side_effect(uid):
            if uid == 501:
                return mock_pwd_other
            raise KeyError(f"no user with uid {uid}")

        with patch("pwd.getpwnam", return_value=mock_pwd_egg):
            with patch("pwd.getpwuid", side_effect=getpwuid_side_effect):
                entrypoint._resolve_uid_conflict(501, "egg", logger)

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args == ["usermod", "-u", "60501", "ubuntu"]

    @patch("subprocess.run")
    def test_no_reassign_when_no_conflict(self, mock_run):
        """Does nothing when target UID is not taken."""
        logger = entrypoint.Logger(quiet=True)

        mock_pwd_egg = MagicMock(pw_uid=1000, pw_name="egg")

        with patch("pwd.getpwnam", return_value=mock_pwd_egg):
            with patch("pwd.getpwuid", side_effect=KeyError("not found")):
                entrypoint._resolve_uid_conflict(501, "egg", logger)

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_no_reassign_when_already_matching(self, mock_run):
        """Does nothing when egg user already has the target UID."""
        logger = entrypoint.Logger(quiet=True)

        mock_pwd_egg = MagicMock(pw_uid=501, pw_name="egg")

        with patch("pwd.getpwnam", return_value=mock_pwd_egg):
            entrypoint._resolve_uid_conflict(501, "egg", logger)

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_no_reassign_when_getpwuid_returns_our_user(self, mock_run):
        """Does nothing when getpwuid returns our own user name."""
        logger = entrypoint.Logger(quiet=True)

        mock_pwd_egg_by_name = MagicMock(pw_uid=1000, pw_name="egg")
        mock_pwd_egg_by_uid = MagicMock(pw_uid=501, pw_name="egg")

        with patch("pwd.getpwnam", return_value=mock_pwd_egg_by_name):
            with patch("pwd.getpwuid", return_value=mock_pwd_egg_by_uid):
                entrypoint._resolve_uid_conflict(501, "egg", logger)

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_container_user_not_found(self, mock_run):
        """Proceeds when container_user doesn't exist yet (KeyError path)."""
        mock_run.return_value = MagicMock(returncode=0)
        logger = entrypoint.Logger(quiet=True)

        mock_pwd_other = MagicMock(pw_uid=501, pw_name="ubuntu")

        def getpwuid_side_effect(uid):
            if uid == 501:
                return mock_pwd_other
            raise KeyError(f"no user with uid {uid}")

        with patch("pwd.getpwnam", side_effect=KeyError("not found")):
            with patch("pwd.getpwuid", side_effect=getpwuid_side_effect):
                entrypoint._resolve_uid_conflict(501, "egg", logger)

        # Should still reassign the conflicting user
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "usermod"

    @patch("subprocess.run")
    def test_usermod_failure_raises_runtime_error(self, mock_run):
        """Raises RuntimeError with actionable message when usermod fails."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "usermod")
        logger = entrypoint.Logger(quiet=True)

        mock_pwd_egg = MagicMock(pw_uid=1000, pw_name="egg")
        mock_pwd_other = MagicMock(pw_uid=501, pw_name="ubuntu")

        def getpwuid_side_effect(uid):
            if uid == 501:
                return mock_pwd_other
            raise KeyError(f"no user with uid {uid}")

        with patch("pwd.getpwnam", return_value=mock_pwd_egg):
            with patch("pwd.getpwuid", side_effect=getpwuid_side_effect):
                with pytest.raises(RuntimeError, match="Failed to resolve UID conflict"):
                    entrypoint._resolve_uid_conflict(501, "egg", logger)

    @patch("subprocess.run")
    def test_uid_collision_uses_next_free(self, mock_run):
        """Uses _find_free_uid to skip already-taken UIDs."""
        mock_run.return_value = MagicMock(returncode=0)
        logger = entrypoint.Logger(quiet=True)

        mock_pwd_egg = MagicMock(pw_uid=1000, pw_name="egg")
        mock_pwd_other = MagicMock(pw_uid=501, pw_name="ubuntu")

        def getpwuid_side_effect(uid):
            if uid == 501:
                return mock_pwd_other
            # 60501 is taken (e.g. by nobody or another user)
            if uid == 60501:
                return MagicMock(pw_uid=60501, pw_name="someuser")
            raise KeyError(f"no user with uid {uid}")

        with patch("pwd.getpwnam", return_value=mock_pwd_egg):
            with patch("pwd.getpwuid", side_effect=getpwuid_side_effect):
                entrypoint._resolve_uid_conflict(501, "egg", logger)

        # Should use 60502 (skipped 60501 which was taken)
        call_args = mock_run.call_args[0][0]
        assert call_args == ["usermod", "-u", "60502", "ubuntu"]


class TestFindFreeUid:
    """Tests for _find_free_uid helper."""

    def test_returns_start_when_free(self):
        """Returns start UID when it's not taken."""
        with patch("pwd.getpwuid", side_effect=KeyError("not found")):
            assert entrypoint._find_free_uid(60501) == 60501

    def test_skips_taken_uids(self):
        """Skips UIDs that are already in use."""
        def getpwuid_side_effect(uid):
            if uid in (60501, 60502):
                return MagicMock(pw_uid=uid)
            raise KeyError(f"no user with uid {uid}")

        with patch("pwd.getpwuid", side_effect=getpwuid_side_effect):
            assert entrypoint._find_free_uid(60501) == 60503

    def test_raises_after_100_attempts(self):
        """Raises RuntimeError if all 100 UIDs are taken."""
        with patch("pwd.getpwuid", return_value=MagicMock()):
            with pytest.raises(RuntimeError, match="No free UID found"):
                entrypoint._find_free_uid(60000)


class TestResolveGidConflictEdgeCases:
    """Additional edge case tests for _resolve_gid_conflict."""

    @patch("subprocess.run")
    def test_container_user_not_found(self, mock_run):
        """Proceeds when container_user group doesn't exist yet (KeyError path)."""
        mock_run.return_value = MagicMock(returncode=0)
        logger = entrypoint.Logger(quiet=True)

        mock_grp_dialout = MagicMock(gr_gid=20, gr_name="dialout")

        with patch("grp.getgrnam", side_effect=KeyError("not found")):
            with patch("grp.getgrgid", return_value=mock_grp_dialout):
                entrypoint._resolve_gid_conflict(20, "egg", logger)

        # Should still rename the conflicting group
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args == ["groupmod", "-n", "_orig_dialout", "dialout"]

    @patch("subprocess.run")
    def test_groupmod_failure_raises_runtime_error(self, mock_run):
        """Raises RuntimeError with actionable message when groupmod fails."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "groupmod")
        logger = entrypoint.Logger(quiet=True)

        mock_grp_egg = MagicMock(gr_gid=1000, gr_name="egg")
        mock_grp_dialout = MagicMock(gr_gid=20, gr_name="dialout")

        with patch("grp.getgrnam", return_value=mock_grp_egg):
            with patch("grp.getgrgid", return_value=mock_grp_dialout):
                with pytest.raises(RuntimeError, match="Failed to resolve GID conflict"):
                    entrypoint._resolve_gid_conflict(20, "egg", logger)

    @patch("subprocess.run")
    def test_rename_collision_appends_gid(self, mock_run):
        """Falls back to _orig_{name}_{gid} when _orig_{name} already exists."""
        mock_run.return_value = MagicMock(returncode=0)
        logger = entrypoint.Logger(quiet=True)

        mock_grp_egg = MagicMock(gr_gid=1000, gr_name="egg")
        mock_grp_dialout = MagicMock(gr_gid=20, gr_name="dialout")
        mock_grp_orig = MagicMock(gr_gid=999, gr_name="_orig_dialout")

        def getgrnam_side_effect(name):
            if name == "egg":
                return mock_grp_egg
            if name == "_orig_dialout":
                return mock_grp_orig
            raise KeyError(f"no group named {name}")

        with patch("grp.getgrnam", side_effect=getgrnam_side_effect):
            with patch("grp.getgrgid", return_value=mock_grp_dialout):
                entrypoint._resolve_gid_conflict(20, "egg", logger)

        call_args = mock_run.call_args[0][0]
        assert call_args == ["groupmod", "-n", "_orig_dialout_20", "dialout"]


class TestSetupUserConflictResolution:
    """Integration tests for setup_user conflict resolution orchestration."""

    @patch("subprocess.run")
    def test_calls_conflict_resolution_before_adjustment(self, mock_run):
        """Verifies conflict resolution runs before groupmod/usermod adjustment."""
        mock_run.return_value = MagicMock(returncode=0)
        logger = entrypoint.Logger(quiet=True)

        config = MagicMock()
        config.container_user = "egg"
        config.runtime_uid = 501
        config.runtime_gid = 20
        config.user_home = Path("/home/egg")

        # GID 20 held by "dialout", UID 501 held by "ubuntu"
        mock_grp_dialout = MagicMock(gr_gid=20, gr_name="dialout")
        mock_pwd_ubuntu = MagicMock(pw_uid=501, pw_name="ubuntu")
        mock_grp_egg = MagicMock(gr_gid=1000, gr_name="egg")
        mock_pwd_egg = MagicMock(pw_uid=1000, pw_name="egg")

        def getgrnam_side_effect(name):
            if name == "egg":
                return mock_grp_egg
            if name == "_orig_dialout":
                raise KeyError("not found")
            raise KeyError(f"no group: {name}")

        def getpwnam_side_effect(name):
            if name == "egg":
                return mock_pwd_egg
            raise KeyError(f"no user: {name}")

        def getgrgid_side_effect(gid):
            if gid == 20:
                return mock_grp_dialout
            raise KeyError(f"no group with gid {gid}")

        def getpwuid_side_effect(uid):
            if uid == 501:
                return mock_pwd_ubuntu
            # _find_free_uid checks 60501 — it's free
            raise KeyError(f"no user with uid {uid}")

        with patch("pwd.getpwnam", side_effect=getpwnam_side_effect), \
             patch("grp.getgrnam", side_effect=getgrnam_side_effect), \
             patch("grp.getgrgid", side_effect=getgrgid_side_effect), \
             patch("pwd.getpwuid", side_effect=getpwuid_side_effect), \
             patch.object(entrypoint, "chown_recursive"):
            entrypoint.setup_user(config, logger)

        # Should have 4 calls:
        # 1. gid conflict rename, 2. uid conflict reassign,
        # 3. gid adjust, 4. uid adjust
        assert mock_run.call_count == 4
        calls = [c[0][0] for c in mock_run.call_args_list]
        # First: rename dialout group (conflict resolution)
        assert calls[0] == ["groupmod", "-n", "_orig_dialout", "dialout"]
        # Second: reassign ubuntu user UID (conflict resolution)
        assert calls[1] == ["usermod", "-u", "60501", "ubuntu"]
        # Third: adjust egg group GID
        assert calls[2] == ["groupmod", "-g", "20", "egg"]
        # Fourth: adjust egg user UID
        assert calls[3] == ["usermod", "-u", "501", "egg"]

    @patch("subprocess.run")
    def test_skips_conflict_resolution_when_ids_match(self, mock_run):
        """Skips conflict resolution when UID/GID already match."""
        logger = entrypoint.Logger(quiet=True)

        config = MagicMock()
        config.container_user = "egg"
        config.runtime_uid = 1000
        config.runtime_gid = 1000
        config.user_home = Path("/home/egg")

        mock_pwd_egg = MagicMock(pw_uid=1000, pw_name="egg")
        mock_grp_egg = MagicMock(gr_gid=1000, gr_name="egg")

        with patch("pwd.getpwnam", return_value=mock_pwd_egg), \
             patch("grp.getgrnam", return_value=mock_grp_egg):
            entrypoint.setup_user(config, logger)

        # No commands should have been run — IDs already match
        mock_run.assert_not_called()
