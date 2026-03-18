"""Feedback addressing step.

Spawns a fixer agent to address review feedback comments on a PR.
Caps the number of feedback rounds to prevent infinite loops.
"""

import logging

from ..config import BabysitConfig
from ..fixer import run_fixer
from ..prompts import build_feedback_fixer_prompt
from .conflict import StepResult

logger = logging.getLogger(__name__)


def address_feedback(
    config: BabysitConfig,
    review_comments: list[str],
    round_number: int,
) -> StepResult:
    """Address review feedback on the PR.

    Spawns a fixer agent with a feedback-addressing prompt built from
    the review comments. Enforces the maximum feedback rounds limit.

    Args:
        config: Babysit configuration.
        review_comments: List of review comment bodies to address.
        round_number: Current feedback round (1-indexed).

    Returns:
        StepResult indicating success, failure, or escalation.
    """
    if not review_comments:
        return StepResult(success=True, message="No review comments to address")

    if round_number > config.max_feedback_rounds:
        logger.warning(
            "Feedback round %d exceeds max (%d) for PR #%d, escalating",
            round_number,
            config.max_feedback_rounds,
            config.pr_number,
        )
        return StepResult(
            success=False,
            message=(
                f"Exceeded max feedback rounds ({config.max_feedback_rounds}). "
                f"Human intervention required."
            ),
            escalate=True,
        )

    logger.info(
        "Addressing feedback round %d/%d for PR #%d (%d comments)",
        round_number,
        config.max_feedback_rounds,
        config.pr_number,
        len(review_comments),
    )

    prompt = build_feedback_fixer_prompt(
        config.pr_number,
        config.repo,
        review_comments,
    )
    result = run_fixer(prompt, config, step_name=f"feedback_round_{round_number}")

    if not result.success:
        logger.warning(
            "Feedback addressing failed for PR #%d round %d: %s",
            config.pr_number,
            round_number,
            result.error,
        )
        return StepResult(
            success=False,
            message=f"Feedback addressing failed: {result.error}",
        )

    logger.info(
        "Feedback round %d addressed for PR #%d",
        round_number,
        config.pr_number,
    )
    return StepResult(
        success=True,
        message=f"Addressed feedback round {round_number} ({len(review_comments)} comments)",
    )


__all__ = [
    "address_feedback",
]
