"""Tests for egg_babysit.types — dataclasses, enums, and computed properties."""

import pytest
from egg_babysit.types import (
    BabysitExitReason,
    BabysitResult,
    BabysitStep,
    CICheckResult,
    CICheckStatus,
    LoopState,
    PRState,
    ReviewVerdict,
)


class TestBabysitStep:
    """Test BabysitStep enum values."""

    def test_all_values(self):
        assert set(BabysitStep) == {
            BabysitStep.CHECK_CONFLICTS,
            BabysitStep.WAIT_CI,
            BabysitStep.FIX_CHECKS,
            BabysitStep.REVIEW,
            BabysitStep.ADDRESS_FEEDBACK,
            BabysitStep.DONE,
        }

    def test_string_values(self):
        assert BabysitStep.CHECK_CONFLICTS == "check_conflicts"
        assert BabysitStep.WAIT_CI == "wait_ci"
        assert BabysitStep.DONE == "done"

    def test_is_str(self):
        """BabysitStep is a StrEnum so it should be usable as a string."""
        assert isinstance(BabysitStep.DONE, str)


class TestBabysitExitReason:
    """Test BabysitExitReason enum values."""

    def test_all_values(self):
        assert set(BabysitExitReason) == {
            BabysitExitReason.MERGED,
            BabysitExitReason.TIMEOUT,
            BabysitExitReason.MAX_ITERATIONS,
            BabysitExitReason.ESCALATED,
            BabysitExitReason.ERROR,
            BabysitExitReason.CANCELLED,
        }


class TestCICheckStatus:
    """Test CICheckStatus enum values."""

    def test_all_values(self):
        assert set(CICheckStatus) == {
            CICheckStatus.PENDING,
            CICheckStatus.PASSING,
            CICheckStatus.FAILING,
            CICheckStatus.STALE,
        }


class TestReviewVerdict:
    """Test ReviewVerdict enum values."""

    def test_all_values(self):
        assert set(ReviewVerdict) == {
            ReviewVerdict.APPROVED,
            ReviewVerdict.CHANGES_REQUESTED,
            ReviewVerdict.COMMENTED,
            ReviewVerdict.PENDING,
        }


class TestCICheckResult:
    """Test CICheckResult dataclass."""

    def test_basic_creation(self):
        result = CICheckResult(
            name="lint",
            status=CICheckStatus.PASSING,
            conclusion="SUCCESS",
        )
        assert result.name == "lint"
        assert result.status == CICheckStatus.PASSING
        assert result.conclusion == "SUCCESS"
        assert result.url == ""  # Default

    def test_with_url(self):
        result = CICheckResult(
            name="test",
            status=CICheckStatus.FAILING,
            conclusion="FAILURE",
            url="https://example.com/run/1",
        )
        assert result.url == "https://example.com/run/1"


class TestPRState:
    """Test PRState dataclass and computed properties."""

    def _make_pr_state(self, **overrides):
        defaults = {
            "number": 42,
            "title": "Test PR",
            "state": "open",
            "merged": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "head_sha": "abc123",
            "base_branch": "main",
            "head_branch": "feature",
        }
        defaults.update(overrides)
        return PRState(**defaults)

    def test_basic_creation(self):
        pr = self._make_pr_state()
        assert pr.number == 42
        assert pr.title == "Test PR"
        assert pr.ci_checks == []
        assert pr.review_verdict == ReviewVerdict.PENDING
        assert pr.review_comments == []

    def test_has_conflicts_dirty(self):
        pr = self._make_pr_state(mergeable_state="dirty")
        assert pr.has_conflicts is True

    def test_has_conflicts_clean(self):
        pr = self._make_pr_state(mergeable_state="clean")
        assert pr.has_conflicts is False

    def test_has_conflicts_blocked(self):
        pr = self._make_pr_state(mergeable_state="blocked")
        assert pr.has_conflicts is False

    def test_ci_status_no_checks(self):
        pr = self._make_pr_state()
        assert pr.ci_status == CICheckStatus.PENDING

    def test_ci_status_all_passing(self):
        checks = [
            CICheckResult(name="lint", status=CICheckStatus.PASSING, conclusion="SUCCESS"),
            CICheckResult(name="test", status=CICheckStatus.PASSING, conclusion="SUCCESS"),
        ]
        pr = self._make_pr_state(ci_checks=checks)
        assert pr.ci_status == CICheckStatus.PASSING

    def test_ci_status_some_failing(self):
        checks = [
            CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE"),
            CICheckResult(name="test", status=CICheckStatus.PASSING, conclusion="SUCCESS"),
        ]
        pr = self._make_pr_state(ci_checks=checks)
        assert pr.ci_status == CICheckStatus.FAILING

    def test_ci_status_all_failing(self):
        checks = [
            CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE"),
            CICheckResult(name="test", status=CICheckStatus.FAILING, conclusion="FAILURE"),
        ]
        pr = self._make_pr_state(ci_checks=checks)
        assert pr.ci_status == CICheckStatus.FAILING

    def test_ci_status_mixed_pending_and_passing(self):
        checks = [
            CICheckResult(name="lint", status=CICheckStatus.PASSING, conclusion="SUCCESS"),
            CICheckResult(name="test", status=CICheckStatus.PENDING, conclusion=""),
        ]
        pr = self._make_pr_state(ci_checks=checks)
        assert pr.ci_status == CICheckStatus.PENDING

    def test_ci_status_failing_takes_precedence_over_pending(self):
        checks = [
            CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE"),
            CICheckResult(name="test", status=CICheckStatus.PENDING, conclusion=""),
        ]
        pr = self._make_pr_state(ci_checks=checks)
        assert pr.ci_status == CICheckStatus.FAILING

    def test_failed_checks_empty(self):
        pr = self._make_pr_state()
        assert pr.failed_checks == []

    def test_failed_checks_filters_correctly(self):
        failing = CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE")
        passing = CICheckResult(name="test", status=CICheckStatus.PASSING, conclusion="SUCCESS")
        pr = self._make_pr_state(ci_checks=[failing, passing])
        assert pr.failed_checks == [failing]

    def test_failed_checks_multiple(self):
        fail1 = CICheckResult(name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE")
        fail2 = CICheckResult(name="test", status=CICheckStatus.FAILING, conclusion="ERROR")
        pr = self._make_pr_state(ci_checks=[fail1, fail2])
        assert len(pr.failed_checks) == 2


class TestLoopState:
    """Test LoopState dataclass defaults."""

    def test_default_values(self):
        state = LoopState()
        assert state.iteration == 0
        assert state.current_step == BabysitStep.CHECK_CONFLICTS
        assert state.last_head_sha == ""
        assert state.retry_counts == {}
        assert state.feedback_rounds == 0
        assert state.started_at == ""
        assert state.last_activity_at == ""

    def test_mutable_retry_counts(self):
        state = LoopState()
        state.retry_counts["lint"] = 2
        assert state.retry_counts["lint"] == 2

    def test_custom_values(self):
        state = LoopState(
            iteration=5,
            current_step=BabysitStep.REVIEW,
            last_head_sha="abc",
            feedback_rounds=2,
        )
        assert state.iteration == 5
        assert state.current_step == BabysitStep.REVIEW
        assert state.feedback_rounds == 2


class TestBabysitResult:
    """Test BabysitResult dataclass."""

    def test_basic_creation(self):
        result = BabysitResult(
            exit_reason=BabysitExitReason.MERGED,
            iterations=3,
            duration_seconds=120.5,
            last_step=BabysitStep.DONE,
        )
        assert result.exit_reason == BabysitExitReason.MERGED
        assert result.iterations == 3
        assert result.duration_seconds == 120.5
        assert result.last_step == BabysitStep.DONE
        assert result.message == ""

    def test_with_message(self):
        result = BabysitResult(
            exit_reason=BabysitExitReason.ERROR,
            iterations=1,
            duration_seconds=5.0,
            last_step=BabysitStep.CHECK_CONFLICTS,
            message="Something went wrong",
        )
        assert result.message == "Something went wrong"

    def test_timeout_result(self):
        result = BabysitResult(
            exit_reason=BabysitExitReason.TIMEOUT,
            iterations=10,
            duration_seconds=14400.0,
            last_step=BabysitStep.WAIT_CI,
            message="Loop timed out",
        )
        assert result.exit_reason == BabysitExitReason.TIMEOUT


class TestBabysitConfig:
    """Test BabysitConfig frozen dataclass."""

    def test_frozen_immutability(self):
        from egg_babysit.config import BabysitConfig

        config = BabysitConfig(pr_number=42, repo="owner/repo")
        with pytest.raises(AttributeError):
            config.pr_number = 99

    def test_default_values(self):
        from egg_babysit.config import BabysitConfig

        config = BabysitConfig(pr_number=1, repo="o/r")
        assert config.timeout_seconds == 14400
        assert config.max_iterations == 10
        assert config.poll_interval_seconds == 30
        assert config.max_retries_per_job == 3
        assert config.max_feedback_rounds == 5
        assert config.check_fixers_path == ""
        assert config.orchestrator_url == ""
        assert config.pipeline_id == ""

    def test_custom_values(self):
        from egg_babysit.config import BabysitConfig

        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            timeout_seconds=3600,
            max_iterations=5,
            poll_interval_seconds=60,
        )
        assert config.pr_number == 42
        assert config.repo == "owner/repo"
        assert config.timeout_seconds == 3600
