"""Tests for action/build-conflict-prompt.sh."""

import os
import subprocess
import tempfile
from pathlib import Path

# Path to the script under test
PROJECT_ROOT = Path(__file__).parent.parent.parent
BUILD_CONFLICT_PROMPT = PROJECT_ROOT / "action" / "build-conflict-prompt.sh"


def run_build_conflict_prompt(
    pr_number: str,
    github_repository: str,
    base_ref: str = "main",
    runner_temp: str = "",
    mock_gh: bool = True,
) -> tuple[int, str, str]:
    """Run build-conflict-prompt.sh with the given environment variables.

    When mock_gh=True, we replace gh with a mock that returns empty JSON to avoid
    API calls. The script handles empty/failed API responses gracefully.

    Returns (returncode, stdout, stderr).
    """
    env = os.environ.copy()
    env["PR_NUMBER"] = pr_number
    env["GITHUB_REPOSITORY"] = github_repository
    env["BASE_REF"] = base_ref
    env["GITHUB_OUTPUT"] = "/dev/null"

    # Use a temp directory if not specified
    if not runner_temp:
        runner_temp = tempfile.gettempdir()
    env["RUNNER_TEMP"] = runner_temp

    # Track mock directory for cleanup
    mock_dir_obj = None
    if mock_gh:
        # Create a mock gh command that returns empty JSON.
        # Use TemporaryDirectory for automatic cleanup.
        mock_dir_obj = tempfile.TemporaryDirectory()
        mock_dir = mock_dir_obj.name
        mock_gh_path = Path(mock_dir) / "gh"
        mock_gh_path.write_text('#!/bin/bash\necho "{}"')
        mock_gh_path.chmod(0o755)
        env["PATH"] = f"{mock_dir}:{env.get('PATH', '')}"

    try:
        result = subprocess.run(
            ["bash", str(BUILD_CONFLICT_PROMPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=PROJECT_ROOT,
        )
        return result.returncode, result.stdout, result.stderr
    finally:
        # Clean up mock directory if created
        if mock_dir_obj is not None:
            mock_dir_obj.cleanup()


def read_prompt_file(runner_temp: str, pr_number: str) -> str:
    """Read the generated prompt file."""
    prompt_file = Path(runner_temp) / f"conflict-prompt-{pr_number}.txt"
    return prompt_file.read_text()


class TestConflictPromptGeneration:
    """Tests for conflict prompt generation."""

    def test_generates_conflict_prompt(self) -> None:
        """Script generates a conflict resolution prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_conflict_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "Conflict resolution prompt built" in stdout

            prompt = read_prompt_file(tmpdir, "123")
            assert "Resolve merge conflicts on PR #123" in prompt
            assert "owner/repo" in prompt

    def test_uses_merge_not_rebase(self) -> None:
        """Prompt instructs to use merge, not rebase."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="456",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "456")

            # Should use merge
            assert "git merge" in prompt
            assert "merge (not rebase)" in prompt.lower()

            # Should NOT use rebase or instruct force-push as the push method
            assert "git rebase" not in prompt
            # The prompt explicitly prohibits force pushing
            assert "Do NOT use `--force` or `--force-with-lease`" in prompt

    def test_includes_conflict_categorization(self) -> None:
        """Prompt includes conflict categorization guidance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="789",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "789")

            # Check for categorization types
            assert "Additive" in prompt
            assert "Lock file" in prompt
            assert "Formatting" in prompt
            assert "Semantic" in prompt
            assert "Security-sensitive" in prompt

    def test_includes_preview_step(self) -> None:
        """Prompt instructs to preview merge with --no-commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="101",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "101")

            assert "git merge --no-commit" in prompt
            assert "preview" in prompt.lower()

    def test_includes_revert_instructions(self) -> None:
        """Prompt includes instructions for reverting failed merges."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="202",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "202")

            assert "git revert -m 1 HEAD" in prompt
            assert "resolution was wrong" in prompt.lower()

    def test_includes_summary_comment_format(self) -> None:
        """Prompt includes format for summary comment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="303",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "303")

            assert "Conflict Resolution Summary" in prompt
            assert "| File | Category | Resolution |" in prompt

    def test_includes_semantic_analysis_guidance(self) -> None:
        """Prompt includes semantic analysis guidance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="404",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "404")

            assert "Semantic Analysis" in prompt
            assert "trying to accomplish" in prompt.lower()

    def test_uses_base_ref_parameter(self) -> None:
        """Prompt uses the provided BASE_REF."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="505",
                github_repository="owner/repo",
                base_ref="develop",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "505")

            assert "develop" in prompt
            assert "git fetch origin develop" in prompt
            assert "git merge --no-commit origin/develop" in prompt

    def test_uses_opus_model(self) -> None:
        """Conflict resolution uses opus model for reasoning capability."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_conflict_prompt(
                pr_number="606",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "model=opus" in stdout


class TestConflictRules:
    """Tests for conflict resolution rules."""

    def test_includes_default_rules(self) -> None:
        """Prompt includes default conflict resolution rules."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="707",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "707")

            # Check for default rules
            assert "Auto-resolvable" in prompt
            assert "Escalate to human" in prompt
            assert "Lock files" in prompt
            assert "Additive changes" in prompt
            assert "Semantic conflicts" in prompt

    def test_escalation_cases(self) -> None:
        """Prompt mentions cases that should be escalated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="808",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "808")

            # Key escalation cases
            assert "security" in prompt.lower()
            assert "api" in prompt.lower() or "API" in prompt
            assert "database" in prompt.lower() or "migration" in prompt.lower()


class TestMergeAbort:
    """Tests for merge abort behavior."""

    def test_includes_merge_abort(self) -> None:
        """Prompt includes git merge --abort for when resolution fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_conflict_prompt(
                pr_number="909",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "909")

            assert "git merge --abort" in prompt
            assert "cannot resolve" in prompt.lower()
