"""Tests for action/build-agent-mode-design-review-prompt.sh."""

import os
import subprocess
import tempfile
from pathlib import Path

# Path to the script under test
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

    # Use a temp directory if not specified
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
    prompt_file = Path(runner_temp) / f"review-prompt-{pr_number}.txt"
    return prompt_file.read_text()


class TestInitialReview:
    """Tests for initial review (no LAST_REVIEW_COMMIT)."""

    def test_generates_initial_review_prompt(self) -> None:
        """Initial review uses gh pr diff."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "type=initial" in stdout

            prompt = read_prompt_file(tmpdir, "123")
            assert "Check PR #123" in prompt
            assert "agent-mode design anti-patterns" in prompt
            assert "gh pr diff 123" in prompt
            assert "re-review" not in prompt.lower()

    def test_includes_agent_mode_design_focus(self) -> None:
        """Initial review includes agent-mode design focus."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="456",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "456")
            assert "## What to Look For" in prompt
            assert "docs/guides/agent-mode-design.md" in prompt
            assert "Pre-fetching" in prompt
            assert "How vs what" in prompt

    def test_includes_review_conventions(self) -> None:
        """Initial review includes review conventions section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="789",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "789")
            assert "## Posting Your Review" in prompt
            assert "gh pr review" in prompt

    def test_is_specialized_not_general_review(self) -> None:
        """Prompt explicitly states this is specialized, not general review."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            # Must include scope section explaining specialization
            assert "## Scope" in prompt
            assert "specialized design review" in prompt
            assert "NOT a general code review" in prompt
            # Must include what to skip
            assert "## What to Skip" in prompt
            assert "base review bot covers this" in prompt


class TestReReview:
    """Tests for re-review (with LAST_REVIEW_COMMIT)."""

    def test_generates_rereview_prompt(self) -> None:
        """Re-review uses git diff from last reviewed commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "re-review" in stdout
            assert "abc123d" in stdout  # Short SHA in output

            prompt = read_prompt_file(tmpdir, "123")
            assert "Check PR #123" in prompt
            assert "agent-mode design anti-patterns" in prompt
            assert "This is a **re-review**" in prompt
            assert "abc123def456" in prompt

    def test_includes_git_diff_instruction(self) -> None:
        """Re-review instructs to use git diff from last commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "git diff abc123def456..HEAD" in prompt

    def test_includes_agent_mode_design_focus(self) -> None:
        """Re-review includes agent-mode design focus."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "## What to Look For" in prompt
            assert "docs/guides/agent-mode-design.md" in prompt


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


class TestEmptyLastReviewCommit:
    """Tests for edge cases with LAST_REVIEW_COMMIT."""

    def test_empty_last_review_commit_is_initial(self) -> None:
        """Empty LAST_REVIEW_COMMIT is treated as initial review."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="",  # Explicitly empty
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "type=initial" in stdout

            prompt = read_prompt_file(tmpdir, "123")
            assert "Re-review" not in prompt
