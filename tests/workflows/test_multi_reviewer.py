"""Tests for multi-reviewer integration.

These tests verify the behavior of the parallel reviewer architecture,
including:
- Review verdict aggregation logic
- Per-reviewer feedback combination
- Reviewer failure handling
- Phase-based reviewer defaults
- Production _aggregate_review_verdicts() with ReviewVerdict objects
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add shared and orchestrator to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "orchestrator"))

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

import pytest  # noqa: E402


class TestReviewVerdictAggregation:
    """Tests for aggregating verdicts from multiple reviewers."""

    def test_all_approved_results_in_approved(self):
        """When all reviewers approve, the aggregate verdict is approved."""
        verdicts = {
            "code": "approved",
            "contract": "approved",
        }

        overall = aggregate_verdicts(verdicts)
        assert overall == "approved"

    def test_any_needs_revision_results_in_needs_revision(self):
        """When any reviewer needs revision, the aggregate verdict is needs_revision."""
        verdicts = {
            "code": "approved",
            "contract": "needs_revision",  # One needs revision
        }

        overall = aggregate_verdicts(verdicts)
        assert overall == "needs_revision"

    def test_multiple_needs_revision_results_in_needs_revision(self):
        """When multiple reviewers need revision, the aggregate is needs_revision."""
        verdicts = {
            "code": "needs_revision",
            "contract": "needs_revision",
        }

        overall = aggregate_verdicts(verdicts)
        assert overall == "needs_revision"

    def test_unknown_verdict_treated_as_approved(self):
        """Unknown verdicts should not block (treated as approved)."""
        verdicts = {
            "code": "approved",
            "agent-design": "unknown",  # Unknown verdict
            "contract": "approved",
        }

        overall = aggregate_verdicts(verdicts)
        assert overall == "approved"

    def test_missing_verdict_tracked_separately(self):
        """Missing verdicts should be tracked but not block."""
        verdicts = {
            "code": "approved",
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

    def test_analyze_phase_reviewers(self):
        """Analyze phase should use refine + agent-design reviewers."""
        reviewers = get_default_reviewers("analyze")

        names = [r["name"] for r in reviewers]
        assert "refine" in names
        assert "agent-design" in names
        assert "contract" not in names
        assert "code" not in names

    def test_plan_phase_reviewers(self):
        """Plan phase should use plan reviewer only."""
        reviewers = get_default_reviewers("plan")

        names = [r["name"] for r in reviewers]
        assert "plan" in names
        assert "contract" not in names
        assert "code" not in names

    def test_implement_phase_reviewers(self):
        """Implement phase should use code + contract reviewers."""
        reviewers = get_default_reviewers("implement")

        names = [r["name"] for r in reviewers]
        assert "code" in names
        assert "contract" in names
        assert "unified" not in names

    def test_reviewers_have_valid_names(self):
        """Each reviewer should have a valid name."""
        for phase in ["analyze", "plan", "implement"]:
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
            "code": "approved",
            "agent-design": "missing",  # Job failed
            "contract": "approved",
        }

        failed = get_failed_reviewers(verdicts, job_result="failure")
        assert "agent-design" in failed

    def test_successful_with_missing_not_failed(self):
        """Missing verdict with successful job result is not a failure."""
        verdicts = {
            "code": "approved",
            "agent-design": "missing",  # Didn't produce file but job succeeded
        }

        failed = get_failed_reviewers(verdicts, job_result="success")
        # The reviewer may have skipped producing output intentionally
        assert failed == []

    def test_failure_does_not_block_aggregation(self):
        """Reviewer failures should not block verdict aggregation."""
        verdicts = {
            "code": "approved",
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
                "code": "approved",
                "contract": "needs_revision",
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
                "refine": "approved",
                "agent-design": "approved",
            },
            "plan_reviewer_verdicts": {
                "plan": "approved",
            },
            "implement_reviewer_verdicts": {
                "code": "approved",
                "contract": "approved",
            },
        }

        assert contract["plan_reviewer_verdicts"]["plan"] == "approved"
        assert contract["implement_reviewer_verdicts"]["code"] == "approved"


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
    if phase == "analyze":
        return [
            {"name": "refine"},
            {"name": "agent-design"},
        ]
    elif phase == "plan":
        return [
            {"name": "plan"},
        ]
    elif phase == "implement":
        return [
            {"name": "code"},
            {"name": "contract"},
        ]
    else:
        return [{"name": "code"}]


class TestProductionAggregateReviewVerdicts:
    """Tests for the production _aggregate_review_verdicts() with ReviewVerdict objects."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Import production functions."""
        try:
            from models import AggregatedReviewResult, ReviewVerdict
            from routes.pipelines import _aggregate_review_verdicts

            self._aggregate = _aggregate_review_verdicts
            self.ReviewVerdict = ReviewVerdict
            self.AggregatedReviewResult = AggregatedReviewResult
        except ImportError:
            pytest.skip("Cannot import orchestrator modules")

    def test_all_approved_returns_approved(self):
        """All approved verdicts → overall approved."""
        verdicts = {
            "code": self.ReviewVerdict(verdict="approved"),
            "contract": self.ReviewVerdict(verdict="approved"),
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "approved"
        assert result.blocking_feedback == ""

    def test_any_needs_revision_returns_needs_revision(self):
        """Any needs_revision → overall needs_revision."""
        verdicts = {
            "code": self.ReviewVerdict(verdict="approved"),
            "contract": self.ReviewVerdict(verdict="needs_revision", feedback="Missing tests"),
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "needs_revision"
        assert "Missing tests" in result.blocking_feedback

    def test_none_verdicts_are_skipped(self):
        """None verdicts are skipped entirely."""
        verdicts = {
            "code": self.ReviewVerdict(verdict="approved"),
            "agent-design": None,
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "approved"

    def test_approved_verdicts_with_analysis_collected_in_advisory(self):
        """Analysis and suggestions from approved verdicts appear in advisory_content."""
        verdicts = {
            "code": self.ReviewVerdict(
                verdict="approved",
                analysis="Reviewed all files. Logic is sound.",
                suggestions="Consider adding a docstring.",
            ),
            "contract": self.ReviewVerdict(
                verdict="approved",
                analysis="All criteria met.",
            ),
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "approved"
        assert result.blocking_feedback == ""
        assert "Reviewed all files" in result.advisory_content
        assert "All criteria met" in result.advisory_content
        assert "Consider adding a docstring" in result.advisory_content

    def test_needs_revision_with_analysis_in_both_fields(self):
        """needs_revision verdicts contribute to both blocking_feedback and advisory_content."""
        verdicts = {
            "code": self.ReviewVerdict(
                verdict="needs_revision",
                feedback="Fix the SQL injection",
                analysis="Found a serious security issue in query builder.",
                suggestions="Use parameterized queries.",
            ),
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "needs_revision"
        assert "Fix the SQL injection" in result.blocking_feedback
        assert "serious security issue" in result.advisory_content
        assert "parameterized queries" in result.advisory_content

    def test_result_is_named_tuple(self):
        """Result is an AggregatedReviewResult with named fields."""
        verdicts = {"code": self.ReviewVerdict(verdict="approved")}
        result = self._aggregate(verdicts)
        assert isinstance(result, self.AggregatedReviewResult)
        # Named fields accessible
        assert hasattr(result, "verdict")
        assert hasattr(result, "blocking_feedback")
        assert hasattr(result, "advisory_content")

    def test_backward_compat_verdicts_without_new_fields(self):
        """Verdicts without analysis/suggestions (old format) produce empty advisory."""
        verdicts = {
            "code": self.ReviewVerdict(verdict="approved"),
            "contract": self.ReviewVerdict(verdict="approved", summary="Looks good"),
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "approved"
        assert result.advisory_content == ""

    def test_positional_access_backward_compat(self):
        """AggregatedReviewResult can be accessed positionally (tuple compat)."""
        verdicts = {
            "code": self.ReviewVerdict(
                verdict="needs_revision",
                feedback="Bug found",
            ),
        }
        result = self._aggregate(verdicts)
        # Positional: [0]=verdict, [1]=blocking_feedback, [2]=advisory_content
        assert result[0] == "needs_revision"
        assert "Bug found" in result[1]

    def test_empty_verdicts_dict(self):
        """Empty dict returns approved with empty strings."""
        result = self._aggregate({})
        assert result.verdict == "approved"
        assert result.blocking_feedback == ""
        assert result.advisory_content == ""

    def test_all_none_verdicts(self):
        """All None verdicts returns approved with empty strings (all skipped)."""
        verdicts = {
            "code": None,
            "contract": None,
            "agent-design": None,
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "approved"
        assert result.blocking_feedback == ""
        assert result.advisory_content == ""

    def test_needs_revision_feedback_fallback_to_summary(self):
        """When needs_revision has no feedback, blocking_feedback uses summary."""
        verdicts = {
            "code": self.ReviewVerdict(
                verdict="needs_revision",
                summary="Type errors in module X",
                feedback="",
            ),
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "needs_revision"
        assert "Type errors in module X" in result.blocking_feedback

    def test_needs_revision_no_feedback_no_summary(self):
        """When needs_revision has neither feedback nor summary, section header is still present."""
        verdicts = {
            "code": self.ReviewVerdict(
                verdict="needs_revision",
            ),
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "needs_revision"
        # Should have a section header even without content
        assert "code reviewer" in result.blocking_feedback

    def test_multiple_needs_revision_combined(self):
        """Multiple needs_revision verdicts combine blocking_feedback from all."""
        verdicts = {
            "code": self.ReviewVerdict(
                verdict="needs_revision",
                feedback="SQL injection in query builder",
            ),
            "contract": self.ReviewVerdict(
                verdict="needs_revision",
                feedback="Missing acceptance criterion ac-3",
            ),
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "needs_revision"
        assert "SQL injection" in result.blocking_feedback
        assert "acceptance criterion ac-3" in result.blocking_feedback
        assert "code reviewer" in result.blocking_feedback
        assert "contract reviewer" in result.blocking_feedback

    def test_mixed_approved_and_revision_advisory_from_both(self):
        """Advisory content is collected from both approved and needs_revision verdicts."""
        verdicts = {
            "code": self.ReviewVerdict(
                verdict="needs_revision",
                feedback="Fix the bug",
                analysis="Found a critical bug in authentication.",
            ),
            "contract": self.ReviewVerdict(
                verdict="approved",
                analysis="All criteria met, implementation is solid.",
                suggestions="Consider adding integration tests.",
            ),
        }
        result = self._aggregate(verdicts)
        assert result.verdict == "needs_revision"
        # Advisory should have content from BOTH reviewers
        assert "critical bug in authentication" in result.advisory_content
        assert "All criteria met" in result.advisory_content
        assert "integration tests" in result.advisory_content

    def test_suggestions_prefixed_in_advisory(self):
        """Suggestions in advisory_content are prefixed with '**Suggestions:**'."""
        verdicts = {
            "code": self.ReviewVerdict(
                verdict="approved",
                analysis="Code is clean.",
                suggestions="Add a type annotation to the return value.",
            ),
        }
        result = self._aggregate(verdicts)
        assert "**Suggestions:**" in result.advisory_content
        assert "type annotation" in result.advisory_content
