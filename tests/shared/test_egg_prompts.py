"""Tests for the egg_prompts shared module."""

from pathlib import Path

from egg_prompts import load_conventions, load_rules
from egg_prompts.builders import (
    build_agent_design_review_prompt,
    build_autofixer_prompt,
    build_conflict_prompt,
    build_contract_verification_prompt,
    build_doc_updater_prompt,
    build_feedback_prompt,
    build_review_prompt,
)


class TestLoadConventions:
    """Tests for the load_conventions function."""

    def test_loads_default_review_conventions(self):
        """Loads review-conventions.md from action/ directory."""
        result = load_conventions("review")
        assert result
        assert "Review Conventions" in result
        assert "--body-file" in result

    def test_loads_default_autofixer_conventions(self):
        """Loads autofixer-conventions.md from action/ directory."""
        result = load_conventions("autofixer")
        assert result
        assert "Single-Pass" in result

    def test_loads_default_conflict_conventions(self):
        """Loads conflict-conventions.md from action/ directory."""
        result = load_conventions("conflict")
        assert result
        assert "Merge Workflow" in result

    def test_returns_empty_for_nonexistent(self):
        """Returns empty string for nonexistent conventions file."""
        result = load_conventions("nonexistent-xyz")
        assert result == ""

    def test_repo_specific_override(self, temp_dir):
        """Repo-specific override takes priority over default."""
        egg_dir = temp_dir / ".egg"
        egg_dir.mkdir()
        (egg_dir / "review-conventions.md").write_text("# Custom Review Rules")

        result = load_conventions("review", repo_path=temp_dir)
        assert result == "# Custom Review Rules"

    def test_falls_back_to_default_when_no_repo_override(self, temp_dir):
        """Falls back to default when repo has no override file."""
        result = load_conventions("review", repo_path=temp_dir)
        assert "Review Conventions" in result


class TestLoadRules:
    """Tests for the load_rules function."""

    def test_returns_empty_when_no_repo_override(self):
        """Returns empty string when no repo-specific rules exist."""
        result = load_rules("review")
        assert result == ""

    def test_loads_repo_specific_rules(self, temp_dir):
        """Loads rules from repo-specific .egg/ directory."""
        egg_dir = temp_dir / ".egg"
        egg_dir.mkdir()
        (egg_dir / "review-rules.md").write_text("# Custom Review Rules\nBe strict.")

        result = load_rules("review", repo_path=temp_dir)
        assert result == "# Custom Review Rules\nBe strict."


class TestBuildReviewPrompt:
    """Tests for the build_review_prompt function."""

    def test_initial_review_prompt(self):
        """Initial review prompt includes PR number and diff command."""
        prompt, model = build_review_prompt(123, "owner/repo")

        assert model == "opus"
        assert "Review PR #123" in prompt
        assert "gh pr diff 123" in prompt
        assert "Re-review" not in prompt

    def test_rereview_prompt(self):
        """Re-review prompt includes delta diff command."""
        prompt, model = build_review_prompt(123, "owner/repo", last_review_commit="abc123")

        assert model == "opus"
        assert "Re-review PR #123" in prompt
        assert "git diff abc123..HEAD" in prompt
        assert "previous review" in prompt.lower()

    def test_includes_review_rules(self):
        """Review prompt includes review rules section."""
        prompt, _ = build_review_prompt(123, "owner/repo")
        assert "Security" in prompt
        assert "Correctness" in prompt

    def test_includes_conventions(self):
        """Review prompt includes conventions from file."""
        prompt, _ = build_review_prompt(123, "owner/repo")
        assert "Review Conventions" in prompt

    def test_emphasizes_thoroughness(self):
        """Review prompt emphasizes thorough review."""
        prompt, _ = build_review_prompt(123, "owner/repo")
        assert "thorough" in prompt.lower()
        assert "ALL issues" in prompt or "all issues" in prompt.lower()

    def test_instructs_direct_feedback(self):
        """Review prompt instructs direct feedback."""
        prompt, _ = build_review_prompt(123, "owner/repo")
        assert "direct" in prompt.lower() or "Direct" in prompt


class TestBuildAutofixerPrompt:
    """Tests for the build_autofixer_prompt function."""

    def test_includes_pr_number(self):
        """Autofixer prompt includes PR number."""
        prompt, model = build_autofixer_prompt(456, "owner/repo")

        assert model == "opus"
        assert "PR #456" in prompt

    def test_includes_check_commands(self):
        """Autofixer prompt includes check investigation commands."""
        prompt, _ = build_autofixer_prompt(456, "owner/repo")
        assert "gh pr checks" in prompt
        assert "gh run" in prompt

    def test_includes_conventions(self):
        """Autofixer prompt includes autofixer conventions."""
        prompt, _ = build_autofixer_prompt(456, "owner/repo")
        assert "Autofixer Conventions" in prompt


class TestBuildContractVerificationPrompt:
    """Tests for the build_contract_verification_prompt function."""

    def test_includes_pr_number(self):
        """Contract verification prompt includes PR number."""
        prompt, model = build_contract_verification_prompt(789, "owner/repo")

        assert model == "opus"
        assert "PR #789" in prompt

    def test_includes_verification_rules(self):
        """Contract verification prompt includes verification rules."""
        prompt, _ = build_contract_verification_prompt(789, "owner/repo")
        assert "Task Verification" in prompt
        assert "acceptance criteria" in prompt.lower()


class TestBuildConflictPrompt:
    """Tests for the build_conflict_prompt function."""

    def test_includes_pr_number(self):
        """Conflict prompt includes PR number."""
        prompt, model = build_conflict_prompt(101, "owner/repo")

        assert model == "opus"
        assert "PR #101" in prompt

    def test_uses_merge_not_rebase(self):
        """Conflict prompt instructs merge, not rebase."""
        prompt, _ = build_conflict_prompt(101, "owner/repo")
        assert "merge" in prompt.lower()
        assert "not rebase" in prompt.lower()

    def test_includes_escalation(self):
        """Conflict prompt includes escalation guidance."""
        prompt, _ = build_conflict_prompt(101, "owner/repo")
        assert "escalat" in prompt.lower() or "abort" in prompt.lower()

    def test_custom_base_ref(self):
        """Conflict prompt uses custom base ref."""
        prompt, _ = build_conflict_prompt(101, "owner/repo", base_ref="develop")
        assert "origin/develop" in prompt

    def test_includes_conventions(self):
        """Conflict prompt includes conflict conventions."""
        prompt, _ = build_conflict_prompt(101, "owner/repo")
        assert "Conflict Resolution Conventions" in prompt


class TestBuildAgentDesignReviewPrompt:
    """Tests for the build_agent_design_review_prompt function."""

    def test_initial_review(self):
        """Agent design review prompt for initial review."""
        prompt, model = build_agent_design_review_prompt(202, "owner/repo")

        assert model == "opus"
        assert "PR #202" in prompt
        assert "agent-mode" in prompt.lower()

    def test_includes_anti_patterns(self):
        """Agent design review includes anti-pattern list."""
        prompt, _ = build_agent_design_review_prompt(202, "owner/repo")
        assert "pre-fetching" in prompt.lower()
        assert "Structured output" in prompt

    def test_rereview(self):
        """Agent design re-review includes delta diff."""
        prompt, _ = build_agent_design_review_prompt(
            202, "owner/repo", last_review_commit="def456"
        )
        assert "Re-review" in prompt
        assert "git diff def456..HEAD" in prompt


class TestBuildFeedbackPrompt:
    """Tests for the build_feedback_prompt function."""

    def test_includes_pr_number(self):
        """Feedback prompt includes PR number."""
        prompt, model = build_feedback_prompt(303, "owner/repo")

        assert model == "opus"
        assert "PR #303" in prompt

    def test_includes_feedback_reading_commands(self):
        """Feedback prompt includes commands to read feedback."""
        prompt, _ = build_feedback_prompt(303, "owner/repo")
        assert "gh pr view 303 --comments" in prompt
        assert "gh api" in prompt

    def test_includes_decision_framework(self):
        """Feedback prompt includes fix/respond/skip framework."""
        prompt, _ = build_feedback_prompt(303, "owner/repo")
        assert "Fix" in prompt
        assert "Respond" in prompt
        assert "Skip" in prompt


class TestBuildDocUpdaterPrompt:
    """Tests for the build_doc_updater_prompt function."""

    def test_basic_prompt(self):
        """Doc updater prompt includes context and instructions."""
        prompt, model = build_doc_updater_prompt(
            github_repository="owner/repo",
            changed_files="src/app.py\nsrc/utils.py",
            commit_messages="abc1234 Add new feature",
        )

        assert model == "sonnet"
        assert "src/app.py" in prompt
        assert "Add new feature" in prompt

    def test_dry_run_mode(self):
        """Doc updater in dry run mode includes dry run notice."""
        prompt, _ = build_doc_updater_prompt(
            github_repository="owner/repo",
            changed_files="src/app.py",
            commit_messages="Fix bug",
            dry_run=True,
        )

        assert "Dry Run" in prompt
        assert "do NOT create" in prompt

    def test_high_risk_flags(self):
        """Doc updater includes high-risk instructions when flagged."""
        prompt, _ = build_doc_updater_prompt(
            github_repository="owner/repo",
            changed_files="src/app.py",
            commit_messages="Fix bug",
            high_risk_flags="README_CLI",
            high_risk_instructions="- Check CLI reference tables",
        )

        assert "high-risk" in prompt.lower()
        assert "Check CLI reference tables" in prompt
