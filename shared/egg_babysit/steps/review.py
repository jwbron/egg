"""Review step.

Spawns a reviewer agent to review the PR and returns the verdict
and any comments. Posts a status comment summarising the review result.
"""

import json
import logging
import subprocess
from dataclasses import dataclass

from ..comments import post_status_comment
from ..config import BabysitConfig
from ..prompts import build_review_prompt
from ..reviewer import ReviewResult, run_reviewer
from ..types import ReviewVerdict

logger = logging.getLogger(__name__)


@dataclass
class ReviewStepResult:
    """Result of the review step.

    Attributes:
        verdict: Review verdict from the reviewer agent.
        comments: Review comments from the agent.
        success: Whether the review step completed without errors.
        message: Human-readable summary.
    """

    verdict: ReviewVerdict
    comments: list[str]
    success: bool = True
    message: str = ""


def _fetch_pr_labels(pr_number: int, repo: str) -> list[str]:
    """Fetch PR labels via the gh CLI.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.

    Returns:
        List of label name strings. Empty list on failure.
    """
    try:
        raw = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "labels",
                "--jq",
                "[.labels[].name]",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if raw.returncode == 0 and raw.stdout.strip():
            return list(json.loads(raw.stdout))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
        logger.debug("Failed to fetch PR labels for #%d: %s", pr_number, exc)
    return []


def _fetch_changed_files(pr_number: int, repo: str) -> list[str]:
    """Fetch list of files changed in a PR via the gh CLI.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.

    Returns:
        List of changed file paths. Empty list on failure.
    """
    try:
        raw = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "files",
                "--jq",
                "[.files[].path]",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if raw.returncode == 0 and raw.stdout.strip():
            return list(json.loads(raw.stdout))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
        logger.debug("Failed to fetch changed files for PR #%d: %s", pr_number, exc)
    return []


def run_review(config: BabysitConfig) -> ReviewStepResult:
    """Run the review step.

    Spawns a reviewer agent that examines the PR diff and posts a
    GitHub review. The review verdict and comments are captured from
    the PR state after the agent completes.

    Fetches PR labels and changed files to enable conditional review
    criteria (contract verification for ``sdlc:pr`` labelled PRs,
    agent-design review for agent infrastructure changes).

    Args:
        config: Babysit configuration.

    Returns:
        ReviewStepResult with verdict and comments.
    """
    logger.info("Starting review step for PR #%d", config.pr_number)

    # Fetch labels and changed files for conditional review criteria.
    labels = _fetch_pr_labels(config.pr_number, config.repo)
    changed_files = _fetch_changed_files(config.pr_number, config.repo)

    prompt = build_review_prompt(
        config.pr_number,
        config.repo,
        labels=labels,
        changed_files=changed_files,
        concurrent_mode=config.concurrent_mode,
    )
    result = run_reviewer(prompt, config)

    if result.error:
        logger.warning("Review step error for PR #%d: %s", config.pr_number, result.error)
        return ReviewStepResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            success=False,
            message=f"Review failed: {result.error}",
        )

    logger.info(
        "Review complete for PR #%d: verdict=%s, comments=%d",
        config.pr_number,
        result.verdict,
        len(result.comments),
    )

    # Post status comment with review results
    if result.verdict != ReviewVerdict.PENDING:
        _post_review_status(config, result)

    return ReviewStepResult(
        verdict=result.verdict,
        comments=result.comments,
        success=True,
        message=f"Review completed with verdict: {result.verdict}",
    )


def _post_review_status(config: BabysitConfig, result: ReviewResult) -> None:
    """Post a status comment summarizing the review.

    Fetches the current HEAD SHA from the PR for deduplication, then
    posts a markdown summary of the review verdict and top comments.
    """
    verdict_emoji = {
        ReviewVerdict.APPROVED: "\u2705",
        ReviewVerdict.CHANGES_REQUESTED: "\U0001f504",
        ReviewVerdict.COMMENTED: "\U0001f4ac",
    }
    emoji = verdict_emoji.get(result.verdict, "\u2753")

    body = f"## {emoji} Review Complete\n\n"
    body += f"**Verdict**: {result.verdict.value}\n\n"
    if result.comments:
        body += "### Comments\n\n"
        for comment in result.comments[:5]:  # Limit to avoid huge comments
            body += f"- {comment[:200]}\n"

    try:
        sha_result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(config.pr_number),
                "--repo",
                config.repo,
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        head_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else ""
    except Exception:
        head_sha = ""

    post_status_comment(config.pr_number, config.repo, body, head_sha=head_sha)


__all__ = [
    "ReviewStepResult",
    "run_review",
]
