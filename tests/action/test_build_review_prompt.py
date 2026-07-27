"""Tests for action/build-review-prompt.sh."""

import os
import shutil
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
    base_ref: str = "",
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

    if base_ref:
        env["BASE_REF"] = base_ref

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
        """Re-review uses git log from last reviewed commit."""
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

    def test_includes_git_log_instruction(self) -> None:
        """Re-review instructs to use git log that excludes base-branch commits.

        See issue #1758: two-dot `git diff <sha>..HEAD` wrongly attributes
        base-branch merges to the PR author. The replacement `git log` with
        `--not origin/<base>` explicitly excludes base-branch commits.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            # New command form excludes base-branch commits
            assert "git log abc123def456..HEAD --not origin/main -p" in prompt
            # Shallow-checkout nudge
            assert "git fetch origin main" in prompt
            # Old two-dot diff form must not appear on the re-review path
            assert "git diff abc123def456..HEAD" not in prompt

    def test_custom_base_ref_threaded_through(self) -> None:
        """Non-default base ref (e.g. `develop`) is plumbed into the prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
                base_ref="develop",
            )

            assert returncode == 0, f"Script failed: {stderr}"
            prompt = read_prompt_file(tmpdir, "123")
            assert "git fetch origin develop" in prompt
            assert "git log abc123def456..HEAD --not origin/develop -p" in prompt
            # Default `main` shouldn't leak when an explicit non-main base is set
            assert "origin/main" not in prompt
            # Old form still absent
            assert "git diff abc123def456..HEAD" not in prompt

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
        """Re-review keeps the wider-context escape hatch, but requires justification.

        The reviewer may still pull the full diff when the delta alone cannot be
        judged; it must say why. Routine re-verification of untouched files is
        what drove the runaway re-review rounds (#3648, #3653).
        """
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
            # The prompt is hard-wrapped, so match against unwrapped text.
            unwrapped = " ".join(prompt.split())
            assert "why the delta alone was insufficient" in unwrapped
            assert "Do not re-verify files the delta does not touch" in unwrapped

    def test_rereview_is_blocking_only(self) -> None:
        """Re-review raises blocking issues only — advisory nits are out of scope.

        Advisory items on a re-review keep the feedback loop from converging:
        every round's fix supplies the next round's nit, so the loop runs to its
        round cap instead of merging.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "blocking issues only" in prompt.lower()
            assert "Do **not** raise advisory items on a re-review" in prompt
            # The re-review must not re-arm initial-review "find everything" framing.
            assert "Find ALL issues in the new code" not in prompt

    def test_rereview_neutralizes_every_surviving_thoroughness_mandate(self) -> None:
        """Each "find everything" mandate still in the prompt is named and overridden.

        ``${review_rules}`` (``shared/prompts/code-review-criteria.md``) and
        ``${conventions}`` (``action/review-conventions.md``) are shared with the
        initial review and appended to the re-review prompt verbatim, so their
        thoroughness mandates survive into it and cannot simply be asserted
        absent. The blocking-only floor holds only because the overrides section
        quotes each surviving mandate and scopes it to the initial review. If
        someone adds a new mandate to either shared file, this fails — which is
        the point.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            overrides = prompt.split("## Re-review Overrides", 1)
            assert len(overrides) == 2, "Re-review Overrides section is missing"
            # Both the overrides section and the shared text are hard-wrapped,
            # so compare against whitespace-normalized copies.
            overrides_body = " ".join(overrides[1].split())
            unwrapped = " ".join(prompt.split())

            # Every thoroughness mandate that survives from the shared files must
            # be quoted verbatim in the overrides section.
            for mandate in (
                "be extremely thorough",
                "identify ALL issues in the first pass",
                "report every issue you find",
                "be comprehensive",
            ):
                assert mandate.lower() in unwrapped.lower(), (
                    f"{mandate!r} no longer appears in the rendered prompt — "
                    "drop it from the overrides list too"
                )
                assert f'"{mandate}"' in overrides_body, (
                    f"{mandate!r} survives in the prompt but the overrides "
                    "section does not scope it to the initial review"
                )

            # The advisory severity category is the other half of the conflict.
            assert "**Non-blocking** (suggestions)" in unwrapped
            assert "**Non-blocking (suggestions)** severity category" in overrides_body

    def test_rereview_overrides_are_the_last_word(self) -> None:
        """The blocking-only floor must outrank the shared text, so it comes last.

        The floor sits near the top of the prompt, ~200 lines above
        ``${review_rules}`` and ``${conventions}``, which push the other way.
        The overrides section reconciles that by rendering after both — closest
        to the model's output.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "## Re-review Overrides" in prompt, "overrides section is missing"
            overrides_at = prompt.index("## Re-review Overrides")
            assert overrides_at > prompt.index("## Review Rules")
            assert overrides_at > prompt.index("## Review Conventions")

            # Stronger than "after those two headings": the overrides must be
            # the final section, so no later text can contradict them. Any new
            # `## ` section appended to the re-review branch fails here.
            tail = prompt[overrides_at + len("## Re-review Overrides") :]
            later_sections = [line for line in tail.splitlines() if line.startswith("## ")]
            assert not later_sections, (
                f"sections render after the overrides and take the last word: {later_sections}"
            )

    def test_rereview_prompt_never_emits_workflow_trigger_tokens(self) -> None:
        """The prompt must not hand the model the strings the trigger greps for.

        ``on-review-feedback.yml`` substring-matches ``verdict=approve-with-
        suggestions`` against the whole review body, and its ``issue_comment``
        arm regex-extracts the first ``verdict=<word>`` in the body. The `gh`
        wrapper appends the real marker to that same body, so a reviewer that
        echoes a ``verdict=`` token in prose can flip the trigger either way.
        Describe the mechanism; never quote the token.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "verdict=" not in prompt
            assert "approve-with-suggestions" not in prompt

    def test_rereview_closes_previously_approved_work(self) -> None:
        """Re-review must not re-open lines it already signed off on."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "Previously approved work is closed" in prompt
            assert "because you asked for" in prompt

    def test_rereview_directs_clean_approval_via_the_real_controls(self) -> None:
        """A clean approval must be described by the levers the reviewer actually has.

        The reviewer never writes the verdict token — `sandbox/scripts/gh`
        derives it from the posted review action and promotes an approval to
        the suggestions form whenever the body carries the `has-suggestions`
        marker, which re-triggers the feedback workflow. So the prompt has to
        name `--approve` plus marker omission, not the token.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                last_review_commit="abc123def456",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            unwrapped = " ".join(prompt.split())
            assert "approve cleanly" in unwrapped
            assert "gh pr review 123 --approve" in unwrapped
            assert "`has-suggestions` HTML comment" in unwrapped
            assert "the marker must never appear" in unwrapped

    def test_initial_review_still_allows_advisory_items(self) -> None:
        """The blocking-only floor applies to re-reviews, not the initial review."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "Find ALL issues" in prompt
            assert "Do **not** raise advisory items on a re-review" not in prompt

    def test_initial_review_carries_no_rereview_scope_text(self) -> None:
        """No re-review scoping may leak into the initial-review prompt.

        The re-review rules live in the re-review branch of the builder, not in
        the shared `review-conventions.md`, precisely so the initial review —
        where advisory feedback is wanted — never sees them.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "## Re-review Overrides" not in prompt
            assert "re-review" not in prompt.lower()

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


class TestSharedCriteriaLoading:
    """Tests for loading review criteria from shared/prompts/ files."""

    def test_loads_from_shared_file(self) -> None:
        """Script loads review rules from shared/prompts/code-review-criteria.md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout, stderr = run_build_review_prompt(
                pr_number="123",
                github_repository="owner/repo",
                runner_temp=tmpdir,
            )

            assert returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            # The shared file contains these markers from the merged criteria
            assert "Security" in prompt
            assert "Correctness" in prompt
            assert "Robustness" in prompt
            assert "How to Review" in prompt

    def test_user_override_takes_priority(self) -> None:
        """User .egg/review-rules.md overrides shared criteria."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake repo structure with user override
            fake_repo = Path(tmpdir) / "repo"
            fake_repo.mkdir()
            egg_dir = fake_repo / ".egg"
            egg_dir.mkdir()
            (egg_dir / "review-rules.md").write_text("## Custom User Rules\nMy custom rules here.")

            # Copy the action script and shared files to the fake repo
            action_dir = fake_repo / "action"
            action_dir.mkdir()
            shutil.copy(BUILD_REVIEW_PROMPT, action_dir / "build-review-prompt.sh")
            shared_dir = fake_repo / "shared" / "prompts"
            shared_dir.mkdir(parents=True)
            shutil.copy(
                PROJECT_ROOT / "shared" / "prompts" / "code-review-criteria.md",
                shared_dir / "code-review-criteria.md",
            )

            env = os.environ.copy()
            env["PR_NUMBER"] = "123"
            env["GITHUB_REPOSITORY"] = "owner/repo"
            env["GITHUB_OUTPUT"] = "/dev/null"
            env["RUNNER_TEMP"] = tmpdir

            result = subprocess.run(
                ["bash", str(action_dir / "build-review-prompt.sh")],
                capture_output=True,
                text=True,
                env=env,
                cwd=fake_repo,
            )

            assert result.returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            assert "Custom User Rules" in prompt
            assert "My custom rules here" in prompt

    def test_inline_fallback_when_no_shared_file(self) -> None:
        """Script falls back to inline defaults when shared file is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake repo with just the script, no shared files
            fake_repo = Path(tmpdir) / "repo"
            fake_repo.mkdir()
            action_dir = fake_repo / "action"
            action_dir.mkdir()
            shutil.copy(BUILD_REVIEW_PROMPT, action_dir / "build-review-prompt.sh")

            env = os.environ.copy()
            env["PR_NUMBER"] = "123"
            env["GITHUB_REPOSITORY"] = "owner/repo"
            env["GITHUB_OUTPUT"] = "/dev/null"
            env["RUNNER_TEMP"] = tmpdir

            result = subprocess.run(
                ["bash", str(action_dir / "build-review-prompt.sh")],
                capture_output=True,
                text=True,
                env=env,
                cwd=fake_repo,
            )

            assert result.returncode == 0
            prompt = read_prompt_file(tmpdir, "123")
            # Falls back to inline defaults
            assert "Security" in prompt
            assert "Correctness" in prompt
