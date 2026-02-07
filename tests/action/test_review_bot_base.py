"""Tests for action/review-bot-base.sh framework."""

import os
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent


def run_base_with_bot_config(
    bot_name: str,
    bot_default_rules: str,
    bot_task_description: str,
    pr_number: str = "123",
    github_repository: str = "owner/repo",
    last_review_commit: str = "",
    runner_temp: str = "",
    bot_conventions_file: str = "",
    bot_default_conventions: str = "",
) -> tuple[int, str, str, str]:
    """Run a bot script that uses review-bot-base.sh.

    Returns (returncode, stdout, stderr, prompt_content).
    """
    env = os.environ.copy()
    env["PR_NUMBER"] = pr_number
    env["GITHUB_REPOSITORY"] = github_repository
    env["GITHUB_OUTPUT"] = "/dev/null"

    if last_review_commit:
        env["LAST_REVIEW_COMMIT"] = last_review_commit

    if not runner_temp:
        runner_temp = tempfile.gettempdir()
    env["RUNNER_TEMP"] = runner_temp

    # Build a temporary bot script that uses the base
    script = f"""#!/usr/bin/env bash
set -euo pipefail
source "{PROJECT_ROOT}/action/review-bot-base.sh"
BOT_NAME="{bot_name}"
BOT_DEFAULT_RULES='{bot_default_rules}'
BOT_TASK_DESCRIPTION='{bot_task_description}'
"""
    if bot_conventions_file:
        script += f'BOT_CONVENTIONS_FILE="{bot_conventions_file}"\n'
    if bot_default_conventions:
        script += f"BOT_DEFAULT_CONVENTIONS='{bot_default_conventions}'\n"
    script += "build_bot_prompt\n"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        os.chmod(script_path, 0o755)
        result = subprocess.run(
            ["bash", script_path],
            capture_output=True,
            text=True,
            env=env,
            cwd=PROJECT_ROOT,
        )

        # Read the generated prompt file
        prompt_file = Path(runner_temp) / f"{bot_name}-prompt-{pr_number}.txt"
        prompt_content = ""
        if prompt_file.exists():
            prompt_content = prompt_file.read_text()

        return result.returncode, result.stdout, result.stderr, prompt_content
    finally:
        os.unlink(script_path)


class TestBasicPromptGeneration:
    """Tests for basic prompt generation."""

    def test_generates_prompt_with_bot_name(self) -> None:
        """Bot name is used in prompt file name and output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr, prompt = run_base_with_bot_config(
                bot_name="test-bot",
                bot_default_rules="Test rules here",
                bot_task_description="Test task description",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "test-bot prompt built" in stdout
            assert "Test task description" in prompt

    def test_includes_default_rules(self) -> None:
        """Default rules are included in prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr, prompt = run_base_with_bot_config(
                bot_name="rules-test",
                bot_default_rules="Custom default rules for testing",
                bot_task_description="Task description",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "Custom default rules for testing" in prompt

    def test_includes_task_description(self) -> None:
        """Task description is included in Your Task section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr, prompt = run_base_with_bot_config(
                bot_name="task-test",
                bot_default_rules="Rules",
                bot_task_description="Perform specific analysis on the code",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "## Your Task" in prompt
            assert "Perform specific analysis on the code" in prompt

    def test_includes_pr_context(self) -> None:
        """PR number and repository are included in prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr, prompt = run_base_with_bot_config(
                bot_name="context-test",
                bot_default_rules="Rules",
                bot_task_description="Task",
                pr_number="456",
                github_repository="myorg/myrepo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "PR #456" in prompt
            assert "myorg/myrepo" in prompt


class TestReReviewSupport:
    """Tests for re-review functionality."""

    def test_initial_review_format(self) -> None:
        """Initial review includes gh pr diff instruction."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr, prompt = run_base_with_bot_config(
                bot_name="initial-test",
                bot_default_rules="Rules",
                bot_task_description="Task",
                pr_number="789",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "type=initial" in stdout
            assert "gh pr diff 789" in prompt
            assert "Re-review" not in prompt

    def test_rereview_format(self) -> None:
        """Re-review includes git diff and prior context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr, prompt = run_base_with_bot_config(
                bot_name="rereview-test",
                bot_default_rules="Rules",
                bot_task_description="Task",
                pr_number="123",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "re-review" in stdout
            assert "Re-review PR #123" in prompt
            assert "git diff abc123def456..HEAD" in prompt
            assert "prior review" in prompt.lower()

    def test_rereview_includes_check_previous_feedback(self) -> None:
        """Re-review instructs to check previous comments."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr, prompt = run_base_with_bot_config(
                bot_name="feedback-test",
                bot_default_rules="Rules",
                bot_task_description="Task",
                last_review_commit="abc123",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "gh pr view 123 --comments" in prompt
            assert "previous" in prompt.lower()


class TestConventions:
    """Tests for conventions handling."""

    def test_uses_default_conventions_when_no_file(self) -> None:
        """Default conventions are used when no file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr, prompt = run_base_with_bot_config(
                bot_name="conventions-test",
                bot_default_rules="Rules",
                bot_task_description="Task",
                bot_conventions_file="/nonexistent/path",
                bot_default_conventions="Custom default conventions here",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "Custom default conventions here" in prompt

    def test_uses_builtin_default_conventions(self) -> None:
        """Built-in default conventions are used when nothing specified."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr, prompt = run_base_with_bot_config(
                bot_name="builtin-test",
                bot_default_rules="Rules",
                bot_task_description="Task",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            # Check for the built-in default convention text
            assert "gh pr review" in prompt
            assert "Authored by egg" in prompt


class TestRequiredVariables:
    """Tests for required variable validation."""

    def test_fails_without_pr_number(self) -> None:
        """Script fails if PR_NUMBER is not set."""
        env = os.environ.copy()
        env["GITHUB_REPOSITORY"] = "owner/repo"
        env.pop("PR_NUMBER", None)

        script = f"""#!/usr/bin/env bash
set -euo pipefail
source "{PROJECT_ROOT}/action/review-bot-base.sh"
BOT_NAME="test"
BOT_DEFAULT_RULES="rules"
BOT_TASK_DESCRIPTION="task"
build_bot_prompt
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "PR_NUMBER" in result.stderr

    def test_fails_without_github_repository(self) -> None:
        """Script fails if GITHUB_REPOSITORY is not set."""
        env = os.environ.copy()
        env["PR_NUMBER"] = "123"
        env.pop("GITHUB_REPOSITORY", None)

        script = f"""#!/usr/bin/env bash
set -euo pipefail
source "{PROJECT_ROOT}/action/review-bot-base.sh"
BOT_NAME="test"
BOT_DEFAULT_RULES="rules"
BOT_TASK_DESCRIPTION="task"
build_bot_prompt
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "GITHUB_REPOSITORY" in result.stderr

    def test_fails_without_bot_name(self) -> None:
        """Script fails if BOT_NAME is not set."""
        env = os.environ.copy()
        env["PR_NUMBER"] = "123"
        env["GITHUB_REPOSITORY"] = "owner/repo"

        script = f"""#!/usr/bin/env bash
set -euo pipefail
source "{PROJECT_ROOT}/action/review-bot-base.sh"
BOT_DEFAULT_RULES="rules"
BOT_TASK_DESCRIPTION="task"
build_bot_prompt
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode != 0
        assert "BOT_NAME" in result.stderr
