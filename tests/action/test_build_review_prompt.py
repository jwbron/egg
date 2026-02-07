"""Tests for action/build-review-prompt.sh."""

import os
import subprocess
import tempfile
from pathlib import Path

# Path to the script under test
PROJECT_ROOT = Path(__file__).parent.parent.parent
BUILD_REVIEW_PROMPT = PROJECT_ROOT / "action" / "build-review-prompt.sh"


def run_build_review_prompt(
    pr_number: str,
    github_repository: str,
    last_review_commit: str = "",
    runner_temp: str = "",
) -> tuple[int, str, str]:
    """Run build-review-prompt.sh with the given environment variables.

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
        ["bash", str(BUILD_REVIEW_PROMPT)],
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
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "type=initial" in stdout

            prompt = read_prompt_file(tmpdir, "123")
            assert "Review PR #123" in prompt
            assert "gh pr diff 123" in prompt
            assert "Re-review" not in prompt
            assert "LAST_REVIEW_COMMIT" not in prompt

    def test_includes_review_rules(self) -> None:
        """Initial review includes review rules section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="456",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "456")
            assert "## Review Rules" in prompt
            assert "Security" in prompt

    def test_emphasizes_thoroughness(self) -> None:
        """Initial review emphasizes thorough, comprehensive review."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="456",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "456")
            # Check for thoroughness emphasis
            assert "thorough" in prompt.lower()
            assert "ALL issues" in prompt or "all issues" in prompt.lower()
            # Check for systematic review instructions
            assert "every" in prompt.lower()
            # Check for research/context instructions
            assert "context" in prompt.lower()

    def test_instructs_direct_feedback(self) -> None:
        """Initial review instructs direct, unsoftened feedback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="456",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "456")
            assert "direct" in prompt.lower() or "Direct" in prompt

    def test_includes_review_conventions(self) -> None:
        """Initial review includes review conventions section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="789",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "789")
            assert "## Review Conventions" in prompt
            assert "gh pr review" in prompt


class TestReReview:
    """Tests for re-review (with LAST_REVIEW_COMMIT)."""

    def test_generates_rereview_prompt(self) -> None:
        """Re-review uses git diff from last reviewed commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "re-review" in stdout
            assert "abc123d" in stdout  # Short SHA in output

            prompt = read_prompt_file(tmpdir, "123")
            assert "Re-review PR #123" in prompt
            assert "This is a **re-review**" in prompt
            assert "abc123def456" in prompt

    def test_includes_git_diff_instruction(self) -> None:
        """Re-review instructs to use git diff from last commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "git diff abc123def456..HEAD" in prompt

    def test_includes_check_previous_feedback(self) -> None:
        """Re-review instructs to check previous review comments."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "Check previous feedback" in prompt
            assert "gh pr view 123 --comments" in prompt

    def test_includes_verify_issues_addressed(self) -> None:
        """Re-review instructs to verify previous issues were addressed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "Verify issues addressed" in prompt
            assert "previous review" in prompt

    def test_includes_focus_on_delta(self) -> None:
        """Re-review instructs to focus on the delta."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "Review the delta" in prompt
            assert "changed since your last review" in prompt

    def test_includes_full_pr_context_fallback(self) -> None:
        """Re-review includes fallback to full PR diff if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "gh pr diff 123" in prompt
            assert "full pr context" in prompt.lower()

    def test_rereview_emphasizes_thoroughness(self) -> None:
        """Re-review also emphasizes thorough review of new changes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            # Check for thoroughness emphasis
            assert "thorough" in prompt.lower()
            assert "ALL issues" in prompt or "all issues" in prompt.lower()

    def test_rereview_instructs_direct_feedback(self) -> None:
        """Re-review instructs direct, unsoftened feedback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "direct" in prompt.lower() or "Direct" in prompt


class TestRequiredVariables:
    """Tests for required environment variables."""

    def test_fails_without_pr_number(self) -> None:
        """Script fails if PR_NUMBER is not set."""
        env = os.environ.copy()
        env["GITHUB_REPOSITORY"] = "owner/repo"
        env.pop("PR_NUMBER", None)

        result = subprocess.run(
            ["bash", str(BUILD_REVIEW_PROMPT)],
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
            ["bash", str(BUILD_REVIEW_PROMPT)],
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
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="",  # Explicitly empty
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "type=initial" in stdout

            prompt = read_prompt_file(tmpdir, "123")
            assert "Re-review" not in prompt
