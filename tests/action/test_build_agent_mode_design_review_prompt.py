"""Tests for action/build-agent-mode-design-review-prompt.sh."""

import os
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
BUILD_PROMPT = PROJECT_ROOT / "action" / "build-agent-mode-design-review-prompt.sh"


def run_build_prompt(
    pr_number: str,
    github_repository: str,
    last_review_commit: str = "",
    runner_temp: str = "",
) -> tuple[int, str, str]:
    """Run build-agent-mode-design-review-prompt.sh with the given environment variables.

    Returns (returncode, stdout, stderr).
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

    result = subprocess.run(
        ["bash", str(BUILD_PROMPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=PROJECT_ROOT,
    )
    return result.returncode, result.stdout, result.stderr


def read_prompt_file(runner_temp: str, pr_number: str) -> str:
    """Read the generated prompt file."""
    prompt_file = Path(runner_temp) / f"agent-mode-design-prompt-{pr_number}.txt"
    return prompt_file.read_text()


class TestAgentModeDesignReview:
    """Tests for agent-mode design review bot."""

    def test_generates_prompt(self) -> None:
        """Generates a valid prompt file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "agent-mode-design prompt built" in stdout

            prompt = read_prompt_file(tmpdir, "123")
            assert "PR #123" in prompt
            assert "owner/repo" in prompt

    def test_includes_five_guidelines(self) -> None:
        """Prompt references the five agent-mode design guidelines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="456",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "456")

            # Check for the five guidelines
            assert "Pre-fetching" in prompt
            assert "output format" in prompt.lower()
            assert "Post-processing" in prompt
            assert "what, not how" in prompt.lower() or "what instead of how" in prompt.lower()
            assert "explore" in prompt.lower() or "judgment" in prompt.lower()

    def test_includes_anti_patterns(self) -> None:
        """Prompt mentions anti-patterns to look for."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="789",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "789")

            # Check for anti-pattern guidance
            assert "anti-pattern" in prompt.lower()

    def test_references_guidelines_doc(self) -> None:
        """Prompt references the agent-mode-design.md document."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")

            assert "agent-mode-design.md" in prompt

    def test_task_description_focuses_on_design_issues(self) -> None:
        """Task description focuses on agent-mode design, not general review."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")

            # Should mention skipping general code quality
            assert (
                "Skip general code quality" in prompt
                or "Skip general" in prompt
                or "skip" in prompt.lower()
            )

    def test_supports_rereview(self) -> None:
        """Re-review mode works with agent-mode design bot."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "re-review" in stdout

            prompt = read_prompt_file(tmpdir, "123")
            assert "Re-review" in prompt
            assert "abc123def456" in prompt
            assert "git diff abc123def456..HEAD" in prompt


class TestRequiredVariables:
    """Tests for required environment variables."""

    def test_fails_without_pr_number(self) -> None:
        """Script fails if PR_NUMBER is not set."""
        env = os.environ.copy()
        env["GITHUB_REPOSITORY"] = "owner/repo"
        env.pop("PR_NUMBER", None)

        result = subprocess.run(
            ["bash", str(BUILD_PROMPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=PROJECT_ROOT,
        )

        assert result.returncode != 0
        assert "PR_NUMBER" in result.stderr

    def test_fails_without_github_repository(self) -> None:
        """Script fails if GITHUB_REPOSITORY is not set."""
        env = os.environ.copy()
        env["PR_NUMBER"] = "123"
        env.pop("GITHUB_REPOSITORY", None)

        result = subprocess.run(
            ["bash", str(BUILD_PROMPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=PROJECT_ROOT,
        )

        assert result.returncode != 0
        assert "GITHUB_REPOSITORY" in result.stderr
