"""Tests for egg_lib.cli_push module.

Tests the scope-filter push functionality that strips out-of-scope files
from commits before pushing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Add shared and sandbox to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.cli_push import (
    _filter_files,
    _matches_any_pattern,
    _matches_pattern,
    cmd_push,
    register_push_subcommand,
)

# ---------------------------------------------------------------------------
# _matches_pattern tests
# ---------------------------------------------------------------------------


class TestMatchesPattern:
    """Tests for the _matches_pattern helper."""

    def test_exact_match(self):
        assert _matches_pattern("foo/bar.py", "foo/bar.py") is True

    def test_exact_mismatch(self):
        assert _matches_pattern("foo/bar.py", "foo/baz.py") is False

    def test_prefix_match(self):
        """Directory patterns (trailing slash) match any file inside."""
        assert _matches_pattern("docs/guide.md", "docs/") is True

    def test_prefix_match_nested(self):
        assert _matches_pattern("docs/arch/deep.md", "docs/") is True

    def test_prefix_no_match(self):
        assert _matches_pattern("src/docs/file.py", "docs/") is False

    def test_wildcard_extension(self):
        assert _matches_pattern("foo.py", "*.py") is True

    def test_wildcard_extension_mismatch(self):
        assert _matches_pattern("foo.js", "*.py") is False

    def test_double_wildcard_extension(self):
        """**/*.py matches .py files at any depth."""
        assert _matches_pattern("a/b/c.py", "**/*.py") is True

    def test_double_wildcard_top_level(self):
        assert _matches_pattern("main.py", "**/*.py") is True

    def test_double_wildcard_with_prefix(self):
        """Pattern with prefix and **."""
        assert (
            _matches_pattern("sandbox/agent-config/rules/foo.md", "sandbox/agent-config/rules/*.md")
            is True
        )

    def test_double_wildcard_no_match_wrong_ext(self):
        assert _matches_pattern("a/b/c.js", "**/*.py") is False

    def test_leading_dot_slash_stripped(self):
        """Leading ./ is stripped from both path and pattern."""
        assert _matches_pattern("./foo.py", "./foo.py") is True
        assert _matches_pattern("./foo.py", "foo.py") is True

    def test_prefix_exact_dir(self):
        """Edge case: path is exactly the dir without trailing content."""
        # The implementation considers "docs" to start with "docs/" -> False
        # but "docs" + "/" == "docs/" -> True, so it matches
        assert _matches_pattern("docs", "docs/") is True
        assert _matches_pattern("docs/", "docs/") is True

    def test_test_file_patterns(self):
        """Test typical test file patterns."""
        assert _matches_pattern("tests/test_foo.py", "tests/") is True
        # Note: _matches_pattern for **/tests/ only matches if file starts with
        # the ** prefix. For directory patterns under **, the suffix is a dir
        # pattern which doesn't match individual files in some cases.
        assert _matches_pattern("test_foo.py", "**/test_*.py") is True
        assert _matches_pattern("foo/test_bar.py", "**/test_*.py") is True


# ---------------------------------------------------------------------------
# _matches_any_pattern tests
# ---------------------------------------------------------------------------


class TestMatchesAnyPattern:
    """Tests for _matches_any_pattern."""

    def test_matches_one_of_many(self):
        assert _matches_any_pattern("foo.py", ["*.js", "*.py", "*.ts"]) is True

    def test_matches_none(self):
        assert _matches_any_pattern("foo.py", ["*.js", "*.ts"]) is False

    def test_empty_patterns(self):
        assert _matches_any_pattern("foo.py", []) is False


# ---------------------------------------------------------------------------
# _filter_files tests
# ---------------------------------------------------------------------------


class TestFilterFiles:
    """Tests for _filter_files."""

    def test_all_allowed(self):
        """Files matching allowed and not blocked are kept."""
        kept, removed = _filter_files(
            ["src/main.py", "src/util.py"],
            allowed=["**/*.py"],
            blocked=["docs/"],
        )
        assert kept == ["src/main.py", "src/util.py"]
        assert removed == []

    def test_all_blocked(self):
        """Files matching blocked patterns are removed."""
        kept, removed = _filter_files(
            ["docs/guide.md", "docs/arch.md"],
            allowed=["**/*.md"],
            blocked=["docs/"],
        )
        assert kept == []
        assert removed == ["docs/guide.md", "docs/arch.md"]

    def test_mixed_allowed_and_blocked(self):
        """Mixed file list correctly separates kept/removed."""
        kept, removed = _filter_files(
            ["src/main.py", "docs/guide.md", "tests/test_foo.py"],
            allowed=["**/*.py"],
            blocked=["docs/", "tests/"],
        )
        assert kept == ["src/main.py"]
        assert removed == ["docs/guide.md", "tests/test_foo.py"]

    def test_not_in_any_pattern(self):
        """Files matching neither allowed nor blocked are removed."""
        kept, removed = _filter_files(
            ["random.xyz"],
            allowed=["**/*.py"],
            blocked=["docs/"],
        )
        assert kept == []
        assert removed == ["random.xyz"]

    def test_blocked_takes_precedence(self):
        """Blocked patterns are checked before allowed patterns."""
        kept, removed = _filter_files(
            ["tests/test_foo.py"],
            allowed=["**/*.py"],
            blocked=["tests/"],
        )
        assert kept == []
        assert removed == ["tests/test_foo.py"]

    def test_empty_files(self):
        kept, removed = _filter_files([], allowed=["**/*.py"], blocked=["docs/"])
        assert kept == []
        assert removed == []

    def test_empty_allowed_and_blocked(self):
        """With no patterns, all files are removed (not in any allowed)."""
        kept, removed = _filter_files(["foo.py"], allowed=[], blocked=[])
        assert kept == []
        assert removed == ["foo.py"]

    def test_realistic_tester_scope(self):
        """Simulates a tester agent scope filtering a mixed commit."""
        tester_allowed = ["tests/", "**/tests/", "**/*_test.py", "**/test_*.py"]
        tester_blocked = ["docs/", "**/*.md", ".egg-state/contracts/"]

        files = [
            "tests/test_foo.py",
            "gateway/tests/test_bar.py",
            "docs/architecture/design.md",
            "src/main.py",
            ".egg-state/contracts/1527.json",
        ]
        kept, removed = _filter_files(files, tester_allowed, tester_blocked)
        assert kept == ["tests/test_foo.py", "gateway/tests/test_bar.py"]
        assert "docs/architecture/design.md" in removed
        assert ".egg-state/contracts/1527.json" in removed
        assert "src/main.py" in removed


# ---------------------------------------------------------------------------
# cmd_push tests
# ---------------------------------------------------------------------------


def _make_args(scope_filter: bool = False) -> argparse.Namespace:
    return argparse.Namespace(scope_filter=scope_filter)


class TestCmdPushNoScopeFilter:
    """Tests for cmd_push without --scope-filter (passthrough to git push)."""

    @patch("egg_lib.cli_push.subprocess.run")
    def test_passthrough_to_git_push(self, mock_run):
        """Without --scope-filter, just runs git push."""
        mock_run.return_value = MagicMock(returncode=0)
        with pytest.raises(SystemExit) as exc_info:
            cmd_push(_make_args(scope_filter=False))
        assert exc_info.value.code == 0
        mock_run.assert_called_once_with(["git", "push"], text=True)

    @patch("egg_lib.cli_push.subprocess.run")
    def test_passthrough_failure(self, mock_run):
        """Passthrough preserves git push's exit code on failure."""
        mock_run.return_value = MagicMock(returncode=128)
        with pytest.raises(SystemExit) as exc_info:
            cmd_push(_make_args(scope_filter=False))
        assert exc_info.value.code == 128


class TestCmdPushScopeFilter:
    """Tests for cmd_push with --scope-filter."""

    def test_missing_env_var(self):
        """Exits with error when EGG_AGENT_FILE_PATTERNS is not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the env var if it exists
            os.environ.pop("EGG_AGENT_FILE_PATTERNS", None)
            with pytest.raises(SystemExit) as exc_info:
                cmd_push(_make_args(scope_filter=True))
            assert exc_info.value.code == 1

    def test_invalid_json_env_var(self):
        """Exits with error when EGG_AGENT_FILE_PATTERNS is invalid JSON."""
        with patch.dict(os.environ, {"EGG_AGENT_FILE_PATTERNS": "not json"}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_push(_make_args(scope_filter=True))
            assert exc_info.value.code == 1

    @patch("egg_lib.cli_push._run_git")
    def test_no_files_in_commit(self, mock_run_git):
        """Exits with error when commit has no files."""
        patterns = json.dumps({"allowed": ["**/*.py"], "blocked": ["docs/"]})
        with patch.dict(os.environ, {"EGG_AGENT_FILE_PATTERNS": patterns}):
            mock_run_git.return_value = MagicMock(stdout="\n", returncode=0)
            with pytest.raises(SystemExit) as exc_info:
                cmd_push(_make_args(scope_filter=True))
            assert exc_info.value.code == 1

    @patch("egg_lib.cli_push._run_git")
    def test_all_files_out_of_scope(self, mock_run_git):
        """Exits with error when all commit files are out of scope."""
        patterns = json.dumps({"allowed": ["**/*.py"], "blocked": ["docs/"]})
        with patch.dict(os.environ, {"EGG_AGENT_FILE_PATTERNS": patterns}):
            mock_run_git.return_value = MagicMock(stdout="docs/guide.md\n", returncode=0)
            with pytest.raises(SystemExit) as exc_info:
                cmd_push(_make_args(scope_filter=True))
            assert exc_info.value.code == 1

    @patch("egg_lib.cli_push.subprocess.run")
    @patch("egg_lib.cli_push._run_git")
    def test_no_files_removed_pushes_directly(self, mock_run_git, mock_subprocess_run):
        """When all files are in scope, pushes without rewriting."""
        patterns = json.dumps({"allowed": ["**/*.py"], "blocked": ["docs/"]})
        with patch.dict(os.environ, {"EGG_AGENT_FILE_PATTERNS": patterns}):
            mock_run_git.return_value = MagicMock(stdout="src/main.py\nsrc/util.py\n", returncode=0)
            mock_subprocess_run.return_value = MagicMock(returncode=0)
            with pytest.raises(SystemExit) as exc_info:
                cmd_push(_make_args(scope_filter=True))
            assert exc_info.value.code == 0
            mock_subprocess_run.assert_called_once_with(["git", "push"], text=True)

    @patch("egg_lib.cli_push._get_current_branch", return_value="egg/my-branch")
    @patch("egg_lib.cli_push.subprocess.run")
    @patch("egg_lib.cli_push._run_git")
    def test_rewrite_commit_and_push(self, mock_run_git, mock_subprocess_run, mock_get_branch):
        """When some files removed, rewrites commit and pushes."""
        patterns = json.dumps({"allowed": ["**/*.py"], "blocked": ["docs/"]})
        with patch.dict(os.environ, {"EGG_AGENT_FILE_PATTERNS": patterns}):
            # First call: diff returns mixed files
            diff_result = MagicMock(stdout="src/main.py\ndocs/guide.md\n", returncode=0)
            # Subsequent calls: reset, add, commit succeed
            mock_run_git.side_effect = [
                diff_result,  # diff --name-only
                MagicMock(returncode=0),  # reset HEAD~1
                MagicMock(returncode=0),  # add -- src/main.py
                MagicMock(returncode=0),  # commit -C ORIG_HEAD
            ]
            # staged check: something staged (returncode != 0)
            mock_subprocess_run.side_effect = [
                MagicMock(returncode=1),  # diff --cached --quiet -> has staged
                MagicMock(returncode=0),  # push
            ]
            with pytest.raises(SystemExit) as exc_info:
                cmd_push(_make_args(scope_filter=True))
            assert exc_info.value.code == 0

            # Verify the git operations sequence
            calls = mock_run_git.call_args_list
            assert calls[0] == call("diff", "--name-only", "HEAD~1", "HEAD")
            assert calls[1] == call("reset", "HEAD~1")
            assert calls[2] == call("add", "--", "src/main.py")
            assert calls[3] == call("commit", "-C", "ORIG_HEAD")

            # Verify push was to the correct branch
            push_call = mock_subprocess_run.call_args_list[1]
            assert push_call == call(["git", "push", "origin", "egg/my-branch"], text=True)

    @patch("egg_lib.cli_push._get_current_branch", return_value="egg/test")
    @patch("egg_lib.cli_push.subprocess.run")
    @patch("egg_lib.cli_push._run_git")
    def test_empty_staging_after_filter_exits(
        self, mock_run_git, mock_subprocess_run, mock_get_branch
    ):
        """Safety check: exits if staging is empty after filtering."""
        patterns = json.dumps({"allowed": ["**/*.py"], "blocked": ["docs/"]})
        with patch.dict(os.environ, {"EGG_AGENT_FILE_PATTERNS": patterns}):
            # diff returns one .py file (kept) and one doc file (removed)
            diff_result = MagicMock(stdout="src/main.py\ndocs/guide.md\n", returncode=0)
            mock_run_git.side_effect = [
                diff_result,  # diff --name-only
                MagicMock(returncode=0),  # reset HEAD~1
                MagicMock(returncode=0),  # add -- src/main.py
            ]
            # staged check: nothing staged (returncode 0)
            mock_subprocess_run.return_value = MagicMock(returncode=0)
            with pytest.raises(SystemExit) as exc_info:
                cmd_push(_make_args(scope_filter=True))
            assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# register_push_subcommand tests
# ---------------------------------------------------------------------------


class TestRegisterPushSubcommand:
    """Tests for register_push_subcommand."""

    def test_registers_push_parser(self):
        """Verifies the push subcommand is properly registered."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register_push_subcommand(subparsers)

        args = parser.parse_args(["push", "--scope-filter"])
        assert args.scope_filter is True

    def test_scope_filter_default_false(self):
        """--scope-filter defaults to False."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register_push_subcommand(subparsers)

        args = parser.parse_args(["push"])
        assert args.scope_filter is False


# ---------------------------------------------------------------------------
# Edge case / gap tests
# ---------------------------------------------------------------------------


class TestFilterFilesEdgeCases:
    """Tests for edge cases and gap scenarios in _filter_files."""

    def test_file_matches_both_allowed_and_blocked(self):
        """When a file matches both allowed AND blocked, blocked wins."""
        kept, removed = _filter_files(
            ["tests/conftest.py"],
            allowed=["**/*.py"],
            blocked=["tests/"],
        )
        assert kept == []
        assert removed == ["tests/conftest.py"]

    def test_block_exempt_not_considered(self):
        """_filter_files does NOT support block_exempt_patterns.

        This is a known gap: the CLI-side filter doesn't handle block
        exemptions. If the env var patterns include agent-config .md
        files in 'allowed' but also have '**/*.md' in 'blocked', the
        CLI filter will block them — unlike AgentFilePattern.can_write().
        """
        # Simulates coder patterns where agent-config .md files should be exempt
        kept, removed = _filter_files(
            ["sandbox/agent-config/rules/push.md"],
            allowed=["**/*.md", "sandbox/agent-config/rules/*.md"],
            blocked=["**/*.md"],
        )
        # The CLI filter blocks this because blocked is checked first.
        # This differs from AgentFilePattern.can_write() which has
        # block_exempt_patterns support.
        assert removed == ["sandbox/agent-config/rules/push.md"]

    def test_preserves_order(self):
        """Output preserves input order."""
        files = ["c.py", "a.py", "b.md"]
        kept, removed = _filter_files(files, allowed=["**/*.py"], blocked=[])
        assert kept == ["c.py", "a.py"]
        assert removed == ["b.md"]
