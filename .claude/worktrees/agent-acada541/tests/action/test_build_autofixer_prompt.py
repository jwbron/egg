"""Tests for action/build-autofixer-prompt.sh."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Path to the script under test
PROJECT_ROOT = Path(__file__).parent.parent.parent
BUILD_AUTOFIXER_PROMPT = PROJECT_ROOT / "action" / "build-autofixer-prompt.sh"


def run_build_autofixer_prompt(
    pr_number: str,
    github_repository: str,
    failed_workflow: str = "",
    failed_run_id: str = "",
    runner_temp: str = "",
) -> tuple[int, str, str]:
    """Run build-autofixer-prompt.sh with the given environment variables.

    Returns (returncode, stdout, stderr).
    """
    env = os.environ.copy()
    env["PR_NUMBER"] = pr_number
    env["GITHUB_REPOSITORY"] = github_repository
    env["GITHUB_OUTPUT"] = "/dev/null"

    if failed_workflow:
        env["FAILED_WORKFLOW"] = failed_workflow
    if failed_run_id:
        env["FAILED_RUN_ID"] = failed_run_id

    if not runner_temp:
        runner_temp = tempfile.gettempdir()
    env["RUNNER_TEMP"] = runner_temp

    result = subprocess.run(
        ["bash", str(BUILD_AUTOFIXER_PROMPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=PROJECT_ROOT,
    )
    return result.returncode, result.stdout, result.stderr


def read_prompt_file(runner_temp: str, pr_number: str) -> str:
    """Read the generated autofixer prompt file."""
    prompt_file = Path(runner_temp) / f"autofixer-prompt-{pr_number}.txt"
    return prompt_file.read_text()


class TestBasicPromptGeneration:
    """Tests for basic autofixer prompt generation."""

    def test_generates_prompt_successfully(self) -> None:
        """Script runs and produces a prompt file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_autofixer_prompt(
                pr_number="42",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "42")
            assert "PR #42" in prompt
            assert "owner/repo" in prompt

    def test_includes_investigation_instructions(self) -> None:
        """Prompt includes instructions to investigate failures."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, _ = run_build_autofixer_prompt(
                pr_number="10",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "10")
            assert "gh pr checks" in prompt
            assert "Investigate" in prompt

    def test_includes_workflow_context(self) -> None:
        """Prompt includes failed workflow context when provided."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, _ = run_build_autofixer_prompt(
                pr_number="10",
                github_repository="owner/repo",
                failed_workflow="CI Tests",
                failed_run_id="12345",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "10")
            assert "CI Tests" in prompt
            assert "12345" in prompt

    def test_manual_workflow_excluded(self) -> None:
        """Manual workflow name is not included in context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, _ = run_build_autofixer_prompt(
                pr_number="10",
                github_repository="owner/repo",
                failed_workflow="manual",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "10")
            # "manual" workflow should be excluded from context
            assert "The **manual** workflow failed" not in prompt


class TestRequiredVariables:
    """Tests for required environment variables."""

    def test_fails_without_pr_number(self) -> None:
        """Script fails if PR_NUMBER is not set."""
        env = os.environ.copy()
        env["GITHUB_REPOSITORY"] = "owner/repo"
        env.pop("PR_NUMBER", None)

        result = subprocess.run(
            ["bash", str(BUILD_AUTOFIXER_PROMPT)],
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
            ["bash", str(BUILD_AUTOFIXER_PROMPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=PROJECT_ROOT,
        )

        assert result.returncode != 0
        assert "GITHUB_REPOSITORY" in result.stderr


class TestSharedCriteriaLoading:
    """Tests for loading autofixer rules from shared/prompts/ files."""

    def test_loads_from_shared_file(self) -> None:
        """Script loads autofixer rules from shared/prompts/autofixer-rules.md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_autofixer_prompt(
                pr_number="1",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "1")
            # Shared file markers
            assert "Auto-fixable" in prompt
            assert "Report only" in prompt
            assert "Lint errors" in prompt

    def test_user_override_takes_priority(self) -> None:
        """User .egg/autofixer-rules.md overrides shared criteria."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake repo with user override
            fake_repo = Path(tmpdir) / "repo"
            fake_repo.mkdir()
            egg_dir = fake_repo / ".egg"
            egg_dir.mkdir()
            (egg_dir / "autofixer-rules.md").write_text(
                "## Custom Autofixer Rules\nAlways fix everything."
            )

            # Copy the action script to fake repo
            action_dir = fake_repo / "action"
            action_dir.mkdir()
            shutil.copy(BUILD_AUTOFIXER_PROMPT, action_dir / "build-autofixer-prompt.sh")
            # Copy shared dir too
            shared_dir = fake_repo / "shared" / "prompts"
            shared_dir.mkdir(parents=True)
            shutil.copy(
                PROJECT_ROOT / "shared" / "prompts" / "autofixer-rules.md",
                shared_dir / "autofixer-rules.md",
            )

            env = os.environ.copy()
            env["PR_NUMBER"] = "1"
            env["GITHUB_REPOSITORY"] = "owner/repo"
            env["GITHUB_OUTPUT"] = "/dev/null"
            env["RUNNER_TEMP"] = tmpdir

            result = subprocess.run(
                ["bash", str(action_dir / "build-autofixer-prompt.sh")],
                capture_output=True,
                text=True,
                env=env,
                cwd=fake_repo,
            )

            assert result.returncode == 0
            prompt = read_prompt_file(tmpdir, "1")
            assert "Custom Autofixer Rules" in prompt
            assert "Always fix everything" in prompt

    def test_inline_fallback_when_no_shared_file(self) -> None:
        """Script falls back to inline defaults when shared file is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake repo with just the script, no shared files
            fake_repo = Path(tmpdir) / "repo"
            fake_repo.mkdir()
            action_dir = fake_repo / "action"
            action_dir.mkdir()
            shutil.copy(BUILD_AUTOFIXER_PROMPT, action_dir / "build-autofixer-prompt.sh")

            env = os.environ.copy()
            env["PR_NUMBER"] = "1"
            env["GITHUB_REPOSITORY"] = "owner/repo"
            env["GITHUB_OUTPUT"] = "/dev/null"
            env["RUNNER_TEMP"] = tmpdir

            result = subprocess.run(
                ["bash", str(action_dir / "build-autofixer-prompt.sh")],
                capture_output=True,
                text=True,
                env=env,
                cwd=fake_repo,
            )

            assert result.returncode == 0
            prompt = read_prompt_file(tmpdir, "1")
            # Inline fallback content
            assert "Auto-fixable" in prompt
            assert "Lint errors" in prompt
