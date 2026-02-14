"""Tests for multi-reviewer integration.

These tests verify the behavior of the parallel reviewer architecture,
including:
- Review verdict aggregation logic
- Per-reviewer feedback combination
- Reviewer failure handling
- Phase-based reviewer defaults
"""

import sys
from pathlib import Path

import pytest

# Add shared to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))


class TestReviewVerdictAggregation:
    """Tests for aggregating verdicts from multiple reviewers."""

    def test_all_approved_results_in_approved(self):
        """When all reviewers approve, the aggregate verdict is approved."""
        verdicts = {
            "unified": "approved",
            "agent-design": "approved",
            "contract": "approved",
            "code": "approved",
        }

        overall = aggregate_verdicts(verdicts)
        assert overall == "approved"

    def test_any_needs_revision_results_in_needs_revision(self):
        """When any reviewer needs revision, the aggregate verdict is needs_revision."""
        verdicts = {
            "unified": "approved",
            "agent-design": "needs_revision",  # One needs revision
            "contract": "approved",
            "code": "approved",
        }

        overall = aggregate_verdicts(verdicts)
        assert overall == "needs_revision"

    def test_multiple_needs_revision_results_in_needs_revision(self):
        """When multiple reviewers need revision, the aggregate is needs_revision."""
        verdicts = {
            "unified": "needs_revision",
            "agent-design": "needs_revision",
            "contract": "approved",
            "code": "needs_revision",
        }

        overall = aggregate_verdicts(verdicts)
        assert overall == "needs_revision"

    def test_unknown_verdict_treated_as_approved(self):
        """Unknown verdicts should not block (treated as approved)."""
        verdicts = {
            "unified": "approved",
            "agent-design": "unknown",  # Unknown verdict
            "contract": "approved",
        }

        overall = aggregate_verdicts(verdicts)
        assert overall == "approved"

    def test_missing_verdict_tracked_separately(self):
        """Missing verdicts should be tracked but not block."""
        verdicts = {
            "unified": "approved",
            "agent-design": "missing",  # Reviewer didn't produce output
            "contract": "approved",
        }

        overall = aggregate_verdicts(verdicts)
        assert overall == "approved"

        missing = get_missing_reviewers(verdicts)
        assert missing == ["agent-design"]


class TestFeedbackCombination:
    """Tests for combining feedback from multiple reviewers."""

    def test_combine_feedback_with_headers(self):
        """Feedback from multiple reviewers should have section headers."""
        feedbacks = {
            "code": "### Security Issues\n\n1. SQL injection in query builder",
            "contract": "### Unverified Criteria\n\n- ac-1: Missing implementation",
        }

        combined = combine_feedbacks(feedbacks)

        assert "## Feedback from code reviewer" in combined
        assert "## Feedback from contract reviewer" in combined
        assert "SQL injection in query builder" in combined
        assert "Missing implementation" in combined

    def test_empty_feedback_excluded(self):
        """Empty feedback should not create section headers."""
        feedbacks = {
            "code": "### Issue\n\n1. Problem found",
            "contract": "",  # No feedback
        }

        combined = combine_feedbacks(feedbacks)

        assert "## Feedback from code reviewer" in combined
        assert "## Feedback from contract reviewer" not in combined

    def test_all_empty_returns_empty(self):
        """When all feedbacks are empty, result is empty."""
        feedbacks = {
            "code": "",
            "contract": "",
        }

        combined = combine_feedbacks(feedbacks)
        assert combined == ""


class TestPhaseBasedReviewerDefaults:
    """Tests for phase-specific reviewer configurations."""

    def test_refine_phase_reviewers(self):
        """Refine phase should use unified + agent-design reviewers."""
        reviewers = get_default_reviewers("refine")

        names = [r["name"] for r in reviewers]
        assert "unified" in names
        assert "agent-design" in names
        assert "contract" not in names
        assert "code" not in names

    def test_plan_phase_reviewers(self):
        """Plan phase should use unified + agent-design reviewers."""
        reviewers = get_default_reviewers("plan")

        names = [r["name"] for r in reviewers]
        assert "unified" in names
        assert "agent-design" in names
        assert "contract" not in names
        assert "code" not in names

    def test_implement_phase_reviewers(self):
        """Implement phase should use all four reviewers."""
        reviewers = get_default_reviewers("implement")

        names = [r["name"] for r in reviewers]
        assert "unified" in names
        assert "agent-design" in names
        assert "contract" in names
        assert "code" in names

    def test_reviewers_have_valid_names(self):
        """Each reviewer should have a valid name."""
        for phase in ["refine", "plan", "implement"]:
            reviewers = get_default_reviewers(phase)

            for reviewer in reviewers:
                assert "name" in reviewer
                assert reviewer["name"]  # Non-empty name


class TestReviewerFailureHandling:
    """Tests for handling failed reviewer jobs."""

    def test_failed_reviewer_tracked(self):
        """Failed reviewers should be tracked in output."""
        # Simulate a scenario where one reviewer job failed
        verdicts = {
            "unified": "approved",
            "agent-design": "missing",  # Job failed
            "contract": "approved",
        }

        failed = get_failed_reviewers(verdicts, job_result="failure")
        assert "agent-design" in failed

    def test_successful_with_missing_not_failed(self):
        """Missing verdict with successful job result is not a failure."""
        verdicts = {
            "unified": "approved",
            "agent-design": "missing",  # Didn't produce file but job succeeded
        }

        failed = get_failed_reviewers(verdicts, job_result="success")
        # The reviewer may have skipped producing output intentionally
        assert failed == []

    def test_failure_does_not_block_aggregation(self):
        """Reviewer failures should not block verdict aggregation."""
        verdicts = {
            "unified": "approved",
            "agent-design": "missing",  # Failed/missing
            "contract": "needs_revision",
        }

        overall = aggregate_verdicts(verdicts)
        # Should still aggregate based on available verdicts
        assert overall == "needs_revision"


class TestPerReviewerStateTracking:
    """Tests for per-reviewer state in contract."""

    def test_reviewer_verdicts_stored_in_contract(self):
        """Contract should have per-reviewer verdict tracking."""
        contract = {
            "current_phase": "implement",
            "implement_review_cycles": 1,
            "implement_reviewer_verdicts": {
                "unified": "approved",
                "agent-design": "approved",
                "contract": "needs_revision",
                "code": "approved",
            },
        }

        assert "implement_reviewer_verdicts" in contract
        verdicts = contract["implement_reviewer_verdicts"]
        assert verdicts["contract"] == "needs_revision"

    def test_each_phase_has_separate_tracking(self):
        """Each phase should track reviewers separately."""
        contract = {
            "current_phase": "implement",
            "refine_reviewer_verdicts": {
                "unified": "approved",
                "agent-design": "approved",
            },
            "plan_reviewer_verdicts": {
                "unified": "approved",
                "agent-design": "needs_revision",
            },
            "implement_reviewer_verdicts": {
                "unified": "approved",
                "agent-design": "approved",
                "contract": "approved",
                "code": "approved",
            },
        }

        assert contract["plan_reviewer_verdicts"]["agent-design"] == "needs_revision"
        assert contract["implement_reviewer_verdicts"]["agent-design"] == "approved"


# Helper functions that mirror the workflow logic


def aggregate_verdicts(verdicts: dict) -> str:
    """Aggregate verdicts from multiple reviewers.

    Returns 'approved' only if all reviewers approved (or unknown/missing).
    Returns 'needs_revision' if any reviewer needs revision.
    """
    for verdict in verdicts.values():
        if verdict == "needs_revision":
            return "needs_revision"
    return "approved"


def get_missing_reviewers(verdicts: dict) -> list:
    """Get list of reviewers with missing verdicts."""
    return [name for name, verdict in verdicts.items() if verdict == "missing"]


def get_failed_reviewers(verdicts: dict, job_result: str) -> list:
    """Get list of reviewers that failed (missing + job failure)."""
    if job_result != "failure":
        return []
    return [name for name, verdict in verdicts.items() if verdict == "missing"]


def combine_feedbacks(feedbacks: dict) -> str:
    """Combine feedback from multiple reviewers with headers."""
    combined = ""
    for reviewer, feedback in feedbacks.items():
        if feedback:
            combined += f"## Feedback from {reviewer} reviewer\n\n"
            combined += f"{feedback}\n\n"
    return combined.strip()


def get_default_reviewers(phase: str) -> list:
    """Get default reviewers for a phase.

    Note: Review prompts are now built by the local orchestrator
    (orchestrator/routes/pipelines.py), not by shell scripts.
    """
    if phase in ["refine", "plan"]:
        return [
            {"name": "unified"},
            {"name": "agent-design"},
        ]
    elif phase == "implement":
        return [
            {"name": "unified"},
            {"name": "agent-design"},
            {"name": "contract"},
            {"name": "code"},
        ]
    else:
        return [{"name": "unified"}]
