"""Gap tests for egg_babysit.prompts — YAML parsing edge cases, base branch reading."""

import os
import subprocess
from unittest.mock import MagicMock, patch

from egg_babysit.prompts import (
    _parse_yaml_string,
    _read_from_base_branch,
    build_check_fixer_prompt,
    build_feedback_fixer_prompt,
    build_review_prompt,
    get_non_llm_fix_command,
    load_check_fixers_config,
)


class TestParseYamlString:
    """Test _parse_yaml_string edge cases."""

    def test_valid_yaml(self):
        result = _parse_yaml_string("key: value\nfoo: bar")
        assert result == {"key": "value", "foo": "bar"}

    def test_non_dict_result_returns_empty(self):
        """YAML that parses to a list returns empty dict."""
        result = _parse_yaml_string("- item1\n- item2")
        assert result == {}

    def test_yaml_parses_to_string_returns_empty(self):
        """YAML that parses to a plain string returns empty dict."""
        result = _parse_yaml_string("just a string")
        assert result == {}

    def test_yaml_parses_to_none_returns_empty(self):
        """Empty YAML (None) returns empty dict."""
        result = _parse_yaml_string("")
        assert result == {}

    def test_invalid_yaml_returns_empty(self):
        """Malformed YAML returns empty dict."""
        result = _parse_yaml_string(": : [\ninvalid")
        assert result == {}

    def test_source_included_in_log(self):
        """Source parameter is for logging only, doesn't affect parsing."""
        result = _parse_yaml_string("key: value", source="test.yml")
        assert result == {"key": "value"}


class TestReadFromBaseBranch:
    """Test _read_from_base_branch git show integration."""

    @patch("egg_babysit.prompts.subprocess.run")
    def test_reads_file_from_base_branch(self, mock_run):
        """Reads file content from base branch via git show."""
        mock_run.return_value = MagicMock(returncode=0, stdout="version: '1'\nworkflows: {}\n")

        result = _read_from_base_branch(".egg/check-fixers.yml", "main", "/repo")

        assert result is not None
        assert "version" in result
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "git" in call_args
        assert "show" in call_args
        assert "origin/main:.egg/check-fixers.yml" in call_args

    @patch("egg_babysit.prompts.subprocess.run")
    def test_file_not_on_branch(self, mock_run):
        """File doesn't exist on base branch → returns None."""
        mock_run.return_value = MagicMock(returncode=128, stdout="")

        result = _read_from_base_branch(".egg/check-fixers.yml", "main", "/repo")

        assert result is None

    @patch("egg_babysit.prompts.subprocess.run")
    def test_git_timeout(self, mock_run):
        """Git command timeout returns None."""
        mock_run.side_effect = subprocess.TimeoutExpired("git", 10)

        result = _read_from_base_branch(".egg/check-fixers.yml", "main", "/repo")

        assert result is None

    @patch("egg_babysit.prompts.subprocess.run")
    def test_custom_base_branch(self, mock_run):
        """Uses custom base branch in git show command."""
        mock_run.return_value = MagicMock(returncode=0, stdout="data: true\n")

        _read_from_base_branch(".egg/check-fixers.yml", "develop", "/repo")

        call_args = mock_run.call_args[0][0]
        assert "origin/develop:.egg/check-fixers.yml" in call_args


class TestLoadCheckFixersConfigEdgeCases:
    """Additional edge cases for check-fixers config loading."""

    @patch.dict(os.environ, {"EGG_REPO_PATH": ""}, clear=False)
    def test_empty_repo_path_env(self):
        """Empty EGG_REPO_PATH falls through to shared config."""
        # Should not error, will try shared config
        config = load_check_fixers_config()
        assert isinstance(config, dict)

    def test_explicit_path_with_invalid_yaml(self, tmp_path):
        """Explicit path with invalid YAML returns empty dict."""
        bad_file = tmp_path / "bad.yml"
        bad_file.write_text(": [\nbad yaml")

        config = load_check_fixers_config(str(bad_file))
        assert isinstance(config, dict)

    def test_explicit_path_with_list_yaml(self, tmp_path):
        """Explicit path with YAML that parses to list returns empty dict."""
        list_file = tmp_path / "list.yml"
        list_file.write_text("- item1\n- item2\n")

        config = load_check_fixers_config(str(list_file))
        assert config == {}


class TestGetNonLlmFixCommandEdgeCases:
    """Additional edge cases for get_non_llm_fix_command."""

    def test_empty_string_fix_command(self):
        """Empty string non_llm_fix returns None."""
        config = {"workflows": {"Lint": {"Python": {"non_llm_fix": ""}}}}
        result = get_non_llm_fix_command("Lint", "Python", config)
        assert result is None

    def test_non_string_fix_command(self):
        """Non-string non_llm_fix returns None."""
        config = {"workflows": {"Lint": {"Python": {"non_llm_fix": 42}}}}
        result = get_non_llm_fix_command("Lint", "Python", config)
        assert result is None

    def test_non_dict_job_config(self):
        """Non-dict job config returns None."""
        config = {"workflows": {"Lint": {"Python": "not-a-dict"}}}
        result = get_non_llm_fix_command("Lint", "Python", config)
        assert result is None


class TestBuildPromptsEdgeCases:
    """Edge cases for prompt builders."""

    def test_check_fixer_empty_jobs_list(self):
        """Empty failed jobs list still produces a valid prompt."""
        prompt = build_check_fixer_prompt(42, "owner/repo", [])
        assert isinstance(prompt, str)
        assert "42" in prompt

    @patch.dict(os.environ, {"EGG_REPO_PATH": "/custom/path"})
    def test_check_fixer_uses_env_path(self):
        """Prompt uses EGG_REPO_PATH when repo_path not specified."""
        prompt = build_check_fixer_prompt(42, "owner/repo", ["lint"])
        assert "/custom/path" in prompt

    @patch.dict(os.environ, {"EGG_REPO_PATH": "/custom/path"})
    def test_review_uses_env_path(self):
        """Review prompt uses EGG_REPO_PATH when repo_path not specified."""
        prompt = build_review_prompt(42, "owner/repo")
        assert "/custom/path" in prompt

    def test_feedback_empty_comments(self):
        """Feedback prompt with empty comments list."""
        prompt = build_feedback_fixer_prompt(42, "owner/repo", [])
        assert isinstance(prompt, str)
        assert "42" in prompt

    def test_feedback_comments_with_special_chars(self):
        """Comments with special characters don't break the prompt."""
        comments = [
            'Fix: `def foo() -> dict[str, "bar"]` has wrong type',
            "Use {braces} and <angles> properly",
        ]
        prompt = build_feedback_fixer_prompt(42, "owner/repo", comments)
        assert "{braces}" in prompt
        assert "<angles>" in prompt
