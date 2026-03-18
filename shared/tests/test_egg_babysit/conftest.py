"""Shared fixtures for egg_babysit tests."""

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.types import CICheckResult, CICheckStatus, PRState


@pytest.fixture
def default_config():
    """Default BabysitConfig for testing."""
    return BabysitConfig(pr_number=42, repo="owner/repo")


@pytest.fixture
def fast_config():
    """BabysitConfig with short timeouts for fast tests."""
    return BabysitConfig(
        pr_number=42,
        repo="owner/repo",
        timeout_seconds=5,
        max_iterations=2,
        poll_interval_seconds=1,
        max_retries_per_job=2,
        max_feedback_rounds=3,
    )


@pytest.fixture
def open_pr_state():
    """An open PR with no conflicts and no CI checks."""
    return PRState(
        number=42,
        title="Add feature X",
        state="open",
        merged=False,
        mergeable=True,
        mergeable_state="clean",
        head_sha="abc123def456",
        base_branch="main",
        head_branch="feature-x",
    )


@pytest.fixture
def merged_pr_state():
    """A merged PR."""
    return PRState(
        number=42,
        title="Add feature X",
        state="merged",
        merged=True,
        mergeable=True,
        mergeable_state="clean",
        head_sha="abc123def456",
        base_branch="main",
        head_branch="feature-x",
    )


@pytest.fixture
def conflicting_pr_state():
    """A PR with merge conflicts."""
    return PRState(
        number=42,
        title="Add feature X",
        state="open",
        merged=False,
        mergeable=False,
        mergeable_state="dirty",
        head_sha="abc123def456",
        base_branch="main",
        head_branch="feature-x",
    )


@pytest.fixture
def passing_ci_checks():
    """All CI checks passing."""
    return [
        CICheckResult(
            name="lint",
            status=CICheckStatus.PASSING,
            conclusion="SUCCESS",
            url="https://github.com/owner/repo/actions/runs/1",
        ),
        CICheckResult(
            name="test",
            status=CICheckStatus.PASSING,
            conclusion="SUCCESS",
            url="https://github.com/owner/repo/actions/runs/2",
        ),
    ]


@pytest.fixture
def failing_ci_checks():
    """CI checks with lint failing."""
    return [
        CICheckResult(
            name="lint",
            status=CICheckStatus.FAILING,
            conclusion="FAILURE",
            url="https://github.com/owner/repo/actions/runs/1",
        ),
        CICheckResult(
            name="test",
            status=CICheckStatus.PASSING,
            conclusion="SUCCESS",
            url="https://github.com/owner/repo/actions/runs/2",
        ),
    ]


@pytest.fixture
def pending_ci_checks():
    """CI checks still pending."""
    return [
        CICheckResult(
            name="lint",
            status=CICheckStatus.PENDING,
            conclusion="IN_PROGRESS",
            url="https://github.com/owner/repo/actions/runs/1",
        ),
        CICheckResult(
            name="test",
            status=CICheckStatus.PENDING,
            conclusion="QUEUED",
            url="https://github.com/owner/repo/actions/runs/2",
        ),
    ]


# --- Sample gh CLI JSON output fixtures ---


@pytest.fixture
def sample_pr_view_json():
    """Sample JSON from `gh pr view --json ...`."""
    return {
        "number": 42,
        "title": "Add feature X",
        "state": "OPEN",
        "headRefName": "feature-x",
        "baseRefName": "main",
        "headRefOid": "abc123def456",
        "merged": False,
        "mergeable": "MERGEABLE",
        "mergeableState": "clean",
        "reviewDecision": "",
    }


@pytest.fixture
def sample_pr_view_merged_json(sample_pr_view_json):
    """Sample JSON for a merged PR."""
    return {**sample_pr_view_json, "state": "MERGED", "merged": True}


@pytest.fixture
def sample_pr_view_conflicting_json(sample_pr_view_json):
    """Sample JSON for a PR with merge conflicts."""
    return {**sample_pr_view_json, "mergeable": "CONFLICTING", "mergeableState": "dirty"}


@pytest.fixture
def sample_pr_checks_all_pass_json():
    """Sample JSON from `gh pr checks --json ...` with all passing."""
    return [
        {
            "name": "lint",
            "state": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://github.com/owner/repo/actions/runs/1",
        },
        {
            "name": "test",
            "state": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://github.com/owner/repo/actions/runs/2",
        },
    ]


@pytest.fixture
def sample_pr_checks_failing_json():
    """Sample JSON for failing CI checks."""
    return [
        {
            "name": "lint",
            "state": "COMPLETED",
            "conclusion": "FAILURE",
            "detailsUrl": "https://github.com/owner/repo/actions/runs/1",
        },
        {
            "name": "test",
            "state": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://github.com/owner/repo/actions/runs/2",
        },
    ]


@pytest.fixture
def sample_pr_checks_pending_json():
    """Sample JSON for pending CI checks."""
    return [
        {
            "name": "lint",
            "state": "IN_PROGRESS",
            "conclusion": "",
            "detailsUrl": "https://github.com/owner/repo/actions/runs/1",
        },
        {
            "name": "test",
            "state": "QUEUED",
            "conclusion": "",
            "detailsUrl": "https://github.com/owner/repo/actions/runs/2",
        },
    ]
