"""Review step.

Spawns a reviewer agent to review the PR and returns the verdict
and any comments.
"""

import logging
from dataclasses import dataclass

from ..config import BabysitConfig
from ..prompts import build_review_prompt
from ..reviewer import run_reviewer
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


def run_review(config: BabysitConfig) -> ReviewStepResult:
    """Run the review step.

    Spawns a reviewer agent that examines the PR diff and posts a
    GitHub review. The review verdict and comments are captured from
    the PR state after the agent completes.

    Args:
        config: Babysit configuration.

    Returns:
        ReviewStepResult with verdict and comments.
    """
    logger.info("Starting review step for PR #%d", config.pr_number)

    prompt = build_review_prompt(config.pr_number, config.repo)
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

    return ReviewStepResult(
        verdict=result.verdict,
        comments=result.comments,
        success=True,
        message=f"Review completed with verdict: {result.verdict}",
    )


__all__ = [
    "ReviewStepResult",
    "run_review",
]
