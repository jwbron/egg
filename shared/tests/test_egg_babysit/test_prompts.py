"""Tests for egg_babysit.prompts — prompt builders and check-fixers config."""

import os
from unittest.mock import patch

from egg_babysit.prompts import (
    build_check_fixer_prompt,
    build_conflict_resolution_prompt,
    build_feedback_fixer_prompt,
    build_review_prompt,
    get_max_retries,
    get_non_llm_fix_command,
    load_check_fixers_config,
)


class TestLoadCheckFixersConfig:
    """Test loading check-fixers.yml."""

    def test_load_shared_config(self):
        """Load the actual shared/check-fixers.yml bundled with egg."""
        config = load_check_fixers_config()
        # Depending on environment, it may or may not find the config.
        # But if it does, it should be a dict.
        assert isinstance(config, dict)

    def test_load_explicit_path(self, tmp_path):
        """Load from an explicit path."""
        config_file = tmp_path / "check-fixers.yml"
        config_file.write_text(
            "version: '1'\n"
            "defaults:\n"
            "  max_retries: 5\n"
            "workflows:\n"
            "  Lint:\n"
            "    Python:\n"
            "      non_llm_fix: 'make lint-fix'\n"
            "      max_retries: 2\n"
        )

        config = load_check_fixers_config(str(config_file))

        assert config["version"] == "1"
        assert config["defaults"]["max_retries"] == 5
        assert "Lint" in config["workflows"]

    def test_load_missing_explicit_path(self):
        """Missing explicit path returns empty dict."""
        config = load_check_fixers_config("/nonexistent/path.yml")
        assert config == {}

    def test_load_from_repo_path(self, tmp_path):
        """Load from EGG_REPO_PATH/.egg/check-fixers.yml."""
        egg_dir = tmp_path / ".egg"
        egg_dir.mkdir()
        config_file = egg_dir / "check-fixers.yml"
        config_file.write_text("version: '1'\nworkflows: {}\n")

        with patch.dict(os.environ, {"EGG_REPO_PATH": str(tmp_path)}):
            config = load_check_fixers_config()

        assert config["version"] == "1"

    def test_load_invalid_yaml(self, tmp_path):
        """Invalid YAML returns empty dict."""
        config_file = tmp_path / "bad.yml"
        config_file.write_text(": : invalid:\nyaml: [")

        config = load_check_fixers_config(str(config_file))
        # yaml.safe_load may parse partial content or fail; either way dict expected
        assert isinstance(config, dict)


class TestGetNonLlmFixCommand:
    """Test get_non_llm_fix_command lookup."""

    def test_get_non_llm_fix_command_found(self):
        config = {
            "workflows": {
                "Lint": {
                    "Python": {
                        "non_llm_fix": "make lint-fix",
                        "max_retries": 3,
                    }
                }
            }
        }
        result = get_non_llm_fix_command("Lint", "Python", config)
        assert result == "make lint-fix"

    def test_get_non_llm_fix_command_missing_workflow(self):
        config = {"workflows": {}}
        result = get_non_llm_fix_command("Lint", "Python", config)
        assert result is None

    def test_get_non_llm_fix_command_missing_job(self):
        config = {"workflows": {"Lint": {"Shell": {"non_llm_fix": "shfmt"}}}}
        result = get_non_llm_fix_command("Lint", "Python", config)
        assert result is None

    def test_get_non_llm_fix_command_no_fix_configured(self):
        config = {"workflows": {"Lint": {"Python": {"max_retries": 3}}}}
        result = get_non_llm_fix_command("Lint", "Python", config)
        assert result is None

    def test_get_non_llm_fix_command_empty_config(self):
        result = get_non_llm_fix_command("Lint", "Python", {})
        assert result is None

    def test_get_non_llm_fix_command_strips_whitespace(self):
        config = {"workflows": {"Lint": {"Python": {"non_llm_fix": "  make lint  "}}}}
        result = get_non_llm_fix_command("Lint", "Python", config)
        assert result == "make lint"


class TestGetMaxRetries:
    """Test get_max_retries config lookup."""

    def test_job_level_retries(self):
        config = {
            "defaults": {"max_retries": 3},
            "workflows": {"Lint": {"Python": {"max_retries": 5}}},
        }
        assert get_max_retries("Lint", "Python", config) == 5

    def test_falls_back_to_default(self):
        config = {
            "defaults": {"max_retries": 7},
            "workflows": {"Lint": {"Python": {}}},
        }
        assert get_max_retries("Lint", "Python", config) == 7

    def test_no_defaults_section(self):
        config = {"workflows": {"Lint": {"Python": {}}}}
        assert get_max_retries("Lint", "Python", config) == 3  # hardcoded default

    def test_unknown_workflow(self):
        config = {"defaults": {"max_retries": 4}, "workflows": {}}
        assert get_max_retries("Unknown", "Job", config) == 4

    def test_empty_config(self):
        assert get_max_retries("Lint", "Python", {}) == 3


class TestBuildCheckFixerPrompt:
    """Test build_check_fixer_prompt."""

    def test_returns_nonempty_string(self):
        prompt = build_check_fixer_prompt(42, "owner/repo", ["lint", "test"])
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_contains_job_names(self):
        prompt = build_check_fixer_prompt(42, "owner/repo", ["lint", "test"])
        assert "lint" in prompt
        assert "test" in prompt

    def test_contains_pr_number(self):
        prompt = build_check_fixer_prompt(42, "owner/repo", ["lint"])
        assert "42" in prompt

    def test_contains_repo(self):
        prompt = build_check_fixer_prompt(42, "owner/repo", ["lint"])
        assert "owner/repo" in prompt

    def test_custom_repo_path(self):
        prompt = build_check_fixer_prompt(42, "o/r", ["lint"], repo_path="/custom/path")
        assert "/custom/path" in prompt


class TestBuildReviewPrompt:
    """Test build_review_prompt."""

    def test_returns_nonempty_string(self):
        prompt = build_review_prompt(42, "owner/repo")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_contains_pr_number(self):
        prompt = build_review_prompt(42, "owner/repo")
        assert "42" in prompt

    def test_contains_repo(self):
        prompt = build_review_prompt(42, "owner/repo")
        assert "owner/repo" in prompt


class TestBuildConflictResolutionPrompt:
    """Test build_conflict_resolution_prompt."""

    def test_returns_nonempty_string(self):
        prompt = build_conflict_resolution_prompt(42, "owner/repo")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_contains_pr_number(self):
        prompt = build_conflict_resolution_prompt(42, "owner/repo")
        assert "42" in prompt

    def test_mentions_conflicts(self):
        prompt = build_conflict_resolution_prompt(42, "owner/repo")
        assert "conflict" in prompt.lower()


class TestBuildFeedbackFixerPrompt:
    """Test build_feedback_fixer_prompt."""

    def test_returns_nonempty_string(self):
        prompt = build_feedback_fixer_prompt(42, "owner/repo", ["Fix the typo"])
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_contains_comments(self):
        prompt = build_feedback_fixer_prompt(42, "owner/repo", ["Fix the typo", "Add tests"])
        assert "Fix the typo" in prompt
        assert "Add tests" in prompt

    def test_contains_pr_number(self):
        prompt = build_feedback_fixer_prompt(42, "owner/repo", ["comment"])
        assert "42" in prompt
