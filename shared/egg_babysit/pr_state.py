"""PR state polling via the gh CLI.

Fetches pull request metadata, CI check statuses, and review verdicts
from GitHub using subprocess calls to ``gh``. All parsing handles the
JSON output format from the GitHub CLI.
"""

import json
import logging
import subprocess
from typing import Any

from .types import CICheckResult, CICheckStatus, PRState, ReviewVerdict

logger = logging.getLogger(__name__)

# Mapping from GitHub API check state to our enum.
_CHECK_STATE_MAP: dict[str, CICheckStatus] = {
    "SUCCESS": CICheckStatus.PASSING,
    "NEUTRAL": CICheckStatus.PASSING,
    "SKIPPED": CICheckStatus.PASSING,
    "FAILURE": CICheckStatus.FAILING,
    "ERROR": CICheckStatus.FAILING,
    "CANCELLED": CICheckStatus.FAILING,
    "TIMED_OUT": CICheckStatus.FAILING,
    "ACTION_REQUIRED": CICheckStatus.FAILING,
    "STALE": CICheckStatus.STALE,
    "PENDING": CICheckStatus.PENDING,
    "QUEUED": CICheckStatus.PENDING,
    "IN_PROGRESS": CICheckStatus.PENDING,
    "WAITING": CICheckStatus.PENDING,
    "REQUESTED": CICheckStatus.PENDING,
    "STARTUP_FAILURE": CICheckStatus.FAILING,
}

# Mapping from GitHub API review decision to our enum.
_REVIEW_DECISION_MAP: dict[str, ReviewVerdict] = {
    "APPROVED": ReviewVerdict.APPROVED,
    "CHANGES_REQUESTED": ReviewVerdict.CHANGES_REQUESTED,
    "REVIEW_REQUIRED": ReviewVerdict.PENDING,
}


def _run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Run a gh CLI command and return stdout.

    Args:
        args: Arguments to pass to ``gh``.
        timeout: Command timeout in seconds.

    Returns:
        Standard output as a string.

    Raises:
        subprocess.CalledProcessError: If the command exits non-zero.
        subprocess.TimeoutExpired: If the command exceeds the timeout.
    """
    cmd = ["gh", *args]
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    # Log rate limit warnings from stderr.
    if result.stderr:
        stderr_lower = result.stderr.lower()
        if "rate limit" in stderr_lower or "api rate" in stderr_lower:
            logger.warning("GitHub rate limit detected: %s", result.stderr.strip())
        else:
            logger.debug("gh stderr: %s", result.stderr.strip())
    return result.stdout


def _parse_json(raw: str, context: str = "") -> Any:
    """Parse JSON output from gh CLI.

    Args:
        raw: Raw JSON string.
        context: Description of what was being parsed (for error messages).

    Returns:
        Parsed JSON data.

    Raises:
        ValueError: If the JSON is invalid.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON from %s: %s", context or "gh output", exc)
        raise ValueError(f"Invalid JSON from {context or 'gh'}: {exc}") from exc


def _map_check_status(state: str, conclusion: str) -> CICheckStatus:
    """Map GitHub check state and conclusion to our CICheckStatus.

    GitHub checks have a ``state`` (the run status) and a ``conclusion``
    (the outcome). We prefer the conclusion when available since it is
    more specific.

    Args:
        state: Check run state (e.g., "completed", "in_progress").
        conclusion: Check run conclusion (e.g., "success", "failure").

    Returns:
        Mapped CICheckStatus enum value.
    """
    # Prefer conclusion when the check has completed.
    key = conclusion.upper() if conclusion else state.upper()
    return _CHECK_STATE_MAP.get(key, CICheckStatus.PENDING)


def fetch_pr_state(pr_number: int, repo: str) -> PRState:
    """Fetch PR metadata from GitHub.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.

    Returns:
        PRState with metadata fields populated (no CI checks).

    Raises:
        subprocess.CalledProcessError: If gh command fails.
        ValueError: If response JSON is malformed.
    """
    fields = (
        "number,title,state,merged,mergeable,"
        "mergeableState,headRefOid,baseRefName,headRefName,reviewDecision"
    )
    raw = _run_gh(
        [
            "pr",
            "view",
            "--json",
            fields,
            "--repo",
            repo,
            str(pr_number),
        ]
    )
    data = _parse_json(raw, context=f"pr view #{pr_number}")

    # Map mergeable string to bool.
    mergeable_raw = data.get("mergeable", "UNKNOWN")
    mergeable = mergeable_raw == "MERGEABLE"

    # Map mergeable state.
    mergeable_state = (data.get("mergeableState") or "unknown").lower()

    # Map review decision.
    review_decision_raw = (data.get("reviewDecision") or "").upper()
    review_verdict = _REVIEW_DECISION_MAP.get(review_decision_raw, ReviewVerdict.PENDING)

    return PRState(
        number=data.get("number", pr_number),
        title=data.get("title", ""),
        state=(data.get("state") or "open").lower(),
        merged=bool(data.get("merged", False)),
        mergeable=mergeable,
        mergeable_state=mergeable_state,
        head_sha=data.get("headRefOid", ""),
        base_branch=data.get("baseRefName", ""),
        head_branch=data.get("headRefName", ""),
        review_verdict=review_verdict,
    )


def fetch_ci_checks(pr_number: int, repo: str) -> list[CICheckResult]:
    """Fetch CI check results for a PR.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.

    Returns:
        List of CICheckResult for each check run.

    Raises:
        subprocess.CalledProcessError: If gh command fails.
        ValueError: If response JSON is malformed.
    """
    raw = _run_gh(
        [
            "pr",
            "checks",
            "--json",
            "name,state,conclusion,detailsUrl",
            "--repo",
            repo,
            str(pr_number),
        ]
    )
    data = _parse_json(raw, context=f"pr checks #{pr_number}")

    if not isinstance(data, list):
        logger.warning("Expected list from pr checks, got %s", type(data).__name__)
        return []

    results: list[CICheckResult] = []
    for check in data:
        name = check.get("name", "unknown")
        state = check.get("state", "")
        conclusion = check.get("conclusion", "")
        url = check.get("detailsUrl", "")

        status = _map_check_status(state, conclusion)
        results.append(
            CICheckResult(
                name=name,
                status=status,
                conclusion=conclusion or state,
                url=url,
            )
        )

    return results


def fetch_review_comments(pr_number: int, repo: str) -> list[str]:
    """Fetch review comments on a PR.

    Retrieves the body text of all review comments. Used for building
    feedback-addressing prompts.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.

    Returns:
        List of comment body strings.
    """
    try:
        raw = _run_gh(
            [
                "api",
                f"repos/{repo}/pulls/{pr_number}/reviews",
                "--jq",
                "[.[].body]",
            ]
        )
        bodies = json.loads(raw)
        return [b.strip() for b in bodies if isinstance(b, str) and b.strip()]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("Failed to fetch review comments for PR #%d: %s", pr_number, exc)
        return []


def get_full_pr_state(pr_number: int, repo: str) -> PRState:
    """Fetch complete PR state including CI checks and review comments.

    Combines ``fetch_pr_state``, ``fetch_ci_checks``, and
    ``fetch_review_comments`` into a single PRState snapshot.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.

    Returns:
        Fully populated PRState.
    """
    pr_state = fetch_pr_state(pr_number, repo)
    pr_state.ci_checks = fetch_ci_checks(pr_number, repo)
    pr_state.review_comments = fetch_review_comments(pr_number, repo)
    return pr_state


def detect_head_sha_change(old_sha: str, new_state: PRState) -> bool:
    """Detect if the PR HEAD has changed (concurrent push detection).

    Args:
        old_sha: Previously observed HEAD SHA.
        new_state: Current PR state.

    Returns:
        True if the HEAD SHA has changed.
    """
    if not old_sha:
        return False
    changed = old_sha != new_state.head_sha
    if changed:
        logger.info(
            "HEAD SHA changed: %s -> %s (concurrent push detected)",
            old_sha[:12],
            new_state.head_sha[:12],
        )
    return changed


__all__ = [
    "detect_head_sha_change",
    "fetch_ci_checks",
    "fetch_pr_state",
    "fetch_review_comments",
    "get_full_pr_state",
]
