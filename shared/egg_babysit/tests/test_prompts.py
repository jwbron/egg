"""Unit tests for prompt builders with BRC and conditional review criteria.

Tests BRC consensus instruction injection, conditional review criteria
inclusion based on labels and changed files, and backward compatibility
when concurrent mode is disabled.
"""

from egg_babysit.prompts import (
    build_check_fixer_prompt,
    build_conflict_resolution_prompt,
    build_feedback_fixer_prompt,
    build_review_prompt,
)

# ---------------------------------------------------------------------------
# BRC consensus instructions
# ---------------------------------------------------------------------------


class TestBRCInstructionsInPrompts:
    """Test BRC consensus protocol instructions in prompts."""

    def test_check_fixer_no_brc_by_default(self) -> None:
        prompt = build_check_fixer_prompt(1, "o/r", ["Lint"])
        assert "egg-orch consensus" not in prompt

    def test_check_fixer_with_brc(self) -> None:
        prompt = build_check_fixer_prompt(1, "o/r", ["Lint"], concurrent_mode=True)
        assert "egg-orch consensus propose" in prompt
        assert "BRC Consensus Protocol" in prompt

    def test_review_no_brc_by_default(self) -> None:
        prompt = build_review_prompt(1, "o/r")
        assert "egg-orch consensus" not in prompt

    def test_review_with_brc(self) -> None:
        prompt = build_review_prompt(1, "o/r", concurrent_mode=True)
        assert "egg-orch consensus ack" in prompt
        assert "egg-orch consensus nack" in prompt
        assert "BRC Consensus Protocol" in prompt

    def test_conflict_no_brc_by_default(self) -> None:
        prompt = build_conflict_resolution_prompt(1, "o/r")
        assert "egg-orch consensus" not in prompt

    def test_conflict_with_brc(self) -> None:
        prompt = build_conflict_resolution_prompt(1, "o/r", concurrent_mode=True)
        assert "egg-orch consensus propose" in prompt

    def test_feedback_no_brc_by_default(self) -> None:
        prompt = build_feedback_fixer_prompt(1, "o/r", ["Fix this"])
        assert "egg-orch consensus" not in prompt

    def test_feedback_with_brc(self) -> None:
        prompt = build_feedback_fixer_prompt(1, "o/r", ["Fix this"], concurrent_mode=True)
        assert "egg-orch consensus propose" in prompt


# ---------------------------------------------------------------------------
# Conditional review criteria
# ---------------------------------------------------------------------------


class TestConditionalReviewCriteria:
    """Test conditional review criteria inclusion based on labels and files."""

    def test_base_review_always_included(self) -> None:
        prompt = build_review_prompt(1, "o/r")
        assert "Security" in prompt or "security" in prompt
        assert "Correctness" in prompt or "correctness" in prompt

    def test_contract_criteria_with_sdlc_label(self) -> None:
        prompt = build_review_prompt(1, "o/r", labels=["sdlc:pr"])
        assert "contract" in prompt.lower() or "Contract" in prompt

    def test_no_contract_criteria_without_label(self) -> None:
        prompt = build_review_prompt(1, "o/r", labels=["bug", "enhancement"])
        assert "Task Verification" not in prompt

    def test_agent_design_with_action_files(self) -> None:
        prompt = build_review_prompt(
            1, "o/r", changed_files=["action/entrypoint.sh", "src/main.py"]
        )
        assert "agent" in prompt.lower() or "Agent" in prompt

    def test_agent_design_with_workflow_files(self) -> None:
        prompt = build_review_prompt(1, "o/r", changed_files=[".github/workflows/test.yml"])
        assert "agent" in prompt.lower() or "Agent" in prompt

    def test_agent_design_with_sandbox_files(self) -> None:
        prompt = build_review_prompt(1, "o/r", changed_files=["sandbox/Dockerfile"])
        assert "agent" in prompt.lower() or "Agent" in prompt

    def test_agent_design_with_prompts_files(self) -> None:
        prompt = build_review_prompt(
            1, "o/r", changed_files=["shared/prompts/code-review-criteria.md"]
        )
        assert "agent" in prompt.lower() or "Agent" in prompt

    def test_no_agent_design_for_unrelated_files(self) -> None:
        prompt = build_review_prompt(1, "o/r", changed_files=["src/main.py", "tests/test_main.py"])
        assert "Agent-Mode Design" not in prompt

    def test_all_criteria_combined(self) -> None:
        prompt = build_review_prompt(
            1, "o/r", labels=["sdlc:pr"], changed_files=["action/entrypoint.sh"]
        )
        assert "Security" in prompt or "security" in prompt
        assert "contract" in prompt.lower() or "Contract" in prompt
        assert "agent" in prompt.lower() or "Agent" in prompt


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestPromptBackwardCompatibility:
    """Test that existing prompt builder signatures still work."""

    def test_check_fixer_minimal_args(self) -> None:
        prompt = build_check_fixer_prompt(1, "o/r", ["Test"])
        assert "Test" in prompt
        assert "#1" in prompt

    def test_review_minimal_args(self) -> None:
        prompt = build_review_prompt(1, "o/r")
        assert "#1" in prompt

    def test_conflict_minimal_args(self) -> None:
        prompt = build_conflict_resolution_prompt(1, "o/r")
        assert "#1" in prompt

    def test_feedback_minimal_args(self) -> None:
        prompt = build_feedback_fixer_prompt(1, "o/r", ["Fix bug"])
        assert "Fix bug" in prompt

    def test_review_with_repo_path(self) -> None:
        prompt = build_review_prompt(1, "o/r", repo_path="/tmp/repo")
        assert "/tmp/repo" in prompt
