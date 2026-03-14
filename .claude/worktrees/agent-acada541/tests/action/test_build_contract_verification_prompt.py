"""Tests for action/build-contract-verification-prompt.sh."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Path to the script under test
PROJECT_ROOT = Path(__file__).parent.parent.parent
BUILD_CONTRACT_PROMPT = PROJECT_ROOT / "action" / "build-contract-verification-prompt.sh"


def run_build_contract_prompt(
    pr_number: str,
    github_repository: str,
    last_review_commit: str = "",
    runner_temp: str = "",
) -> tuple[int, str, str]:
    """Run build-contract-verification-prompt.sh with the given environment variables.

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
        ["bash", str(BUILD_CONTRACT_PROMPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=PROJECT_ROOT,
    )
    return result.returncode, result.stdout, result.stderr


def read_prompt_file(runner_temp: str, pr_number: str) -> str:
    """Read the generated contract verification prompt file."""
    prompt_file = Path(runner_temp) / f"contract-verification-prompt-{pr_number}.txt"
    return prompt_file.read_text()


class TestInitialVerification:
    """Tests for initial contract verification (no LAST_REVIEW_COMMIT)."""

    def test_generates_initial_prompt(self) -> None:
        """Script generates an initial contract verification prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_contract_prompt(
                pr_number="50",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "50")
            assert "PR #50" in prompt
            assert "owner/repo" in prompt
            assert (
                "comprehensive contract verification" in prompt.lower()
                or "Verify contract" in prompt
            )

    def test_includes_egg_contract_cli(self) -> None:
        """Prompt includes egg-contract CLI usage instructions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, _ = run_build_contract_prompt(
                pr_number="50",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "50")
            assert "egg-contract show" in prompt
            assert "egg-contract verify-criterion" in prompt

    def test_includes_review_marker(self) -> None:
        """Prompt includes review marker for tracking."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, _ = run_build_contract_prompt(
                pr_number="50",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "50")
            assert "egg-automated-review" in prompt
            assert "contract-verification" in prompt

    def test_not_a_rereview(self) -> None:
        """Initial verification does not contain re-review markers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, _ = run_build_contract_prompt(
                pr_number="50",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            assert "type=initial" in stdout
            prompt = read_prompt_file(tmpdir, "50")
            assert "re-review" not in prompt.lower() or "Re-verify" not in prompt


class TestReVerification:
    """Tests for re-verification (with LAST_REVIEW_COMMIT)."""

    def test_generates_rereview_prompt(self) -> None:
        """Re-verification uses git diff from last reviewed commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_contract_prompt(
                pr_number="50",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            assert "re-review" in stdout

            prompt = read_prompt_file(tmpdir, "50")
            assert "Re-verify" in prompt or "re-review" in prompt.lower()
            assert "abc123def456" in prompt

    def test_includes_git_diff_instruction(self) -> None:
        """Re-verification instructs to use git diff from last commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, _ = run_build_contract_prompt(
                pr_number="50",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "50")
            assert "git diff abc123def456..HEAD" in prompt


class TestRequiredVariables:
    """Tests for required environment variables."""

    def test_fails_without_pr_number(self) -> None:
        """Script fails if PR_NUMBER is not set."""
        env = os.environ.copy()
        env["GITHUB_REPOSITORY"] = "owner/repo"
        env.pop("PR_NUMBER", None)

        result = subprocess.run(
            ["bash", str(BUILD_CONTRACT_PROMPT)],
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
            ["bash", str(BUILD_CONTRACT_PROMPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=PROJECT_ROOT,
        )

        assert result.returncode != 0
        assert "GITHUB_REPOSITORY" in result.stderr


class TestSharedCriteriaLoading:
    """Tests for loading contract criteria from shared/prompts/ files."""

    def test_loads_from_shared_file(self) -> None:
        """Script loads contract rules from shared/prompts/contract-review-criteria.md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, _, stderr = run_build_contract_prompt(
                pr_number="1",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "1")
            # Shared file markers
            assert "Task Verification" in prompt
            assert "Phase Consistency" in prompt
            assert "Contract Integrity" in prompt

    def test_user_override_takes_priority(self) -> None:
        """User .egg/contract-rules.md overrides shared criteria."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake repo with user override
            fake_repo = Path(tmpdir) / "repo"
            fake_repo.mkdir()
            egg_dir = fake_repo / ".egg"
            egg_dir.mkdir()
            (egg_dir / "contract-rules.md").write_text(
                "## Custom Contract Rules\nVerify everything twice."
            )

            # Copy the action script
            action_dir = fake_repo / "action"
            action_dir.mkdir()
            shutil.copy(BUILD_CONTRACT_PROMPT, action_dir / "build-contract-verification-prompt.sh")
            # Copy shared dir
            shared_dir = fake_repo / "shared" / "prompts"
            shared_dir.mkdir(parents=True)
            shutil.copy(
                PROJECT_ROOT / "shared" / "prompts" / "contract-review-criteria.md",
                shared_dir / "contract-review-criteria.md",
            )

            env = os.environ.copy()
            env["PR_NUMBER"] = "1"
            env["GITHUB_REPOSITORY"] = "owner/repo"
            env["GITHUB_OUTPUT"] = "/dev/null"
            env["RUNNER_TEMP"] = tmpdir

            result = subprocess.run(
                ["bash", str(action_dir / "build-contract-verification-prompt.sh")],
                capture_output=True,
                text=True,
                env=env,
                cwd=fake_repo,
            )

            assert result.returncode == 0
            prompt = read_prompt_file(tmpdir, "1")
            assert "Custom Contract Rules" in prompt
            assert "Verify everything twice" in prompt

    def test_inline_fallback_when_no_shared_file(self) -> None:
        """Script falls back to inline defaults when shared file is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake repo with just the script
            fake_repo = Path(tmpdir) / "repo"
            fake_repo.mkdir()
            action_dir = fake_repo / "action"
            action_dir.mkdir()
            shutil.copy(BUILD_CONTRACT_PROMPT, action_dir / "build-contract-verification-prompt.sh")

            env = os.environ.copy()
            env["PR_NUMBER"] = "1"
            env["GITHUB_REPOSITORY"] = "owner/repo"
            env["GITHUB_OUTPUT"] = "/dev/null"
            env["RUNNER_TEMP"] = tmpdir

            result = subprocess.run(
                ["bash", str(action_dir / "build-contract-verification-prompt.sh")],
                capture_output=True,
                text=True,
                env=env,
                cwd=fake_repo,
            )

            assert result.returncode == 0
            prompt = read_prompt_file(tmpdir, "1")
            # Inline fallback content
            assert "Task Verification" in prompt
            assert "Contract Integrity" in prompt
