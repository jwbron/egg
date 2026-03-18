"""Conflict resolution step.

Detects merge conflicts on a PR and spawns a fixer agent to resolve
them. Verifies resolution by re-fetching PR state.
"""

import logging
from dataclasses import dataclass

from ..config import BabysitConfig
from ..fixer import run_fixer
from ..pr_state import fetch_pr_state
from ..prompts import build_conflict_resolution_prompt
from ..types import PRState

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    """Result of a babysit step.

    Attributes:
        success: Whether the step completed successfully.
        message: Human-readable description of what happened.
        escalate: Whether to escalate to a human.
    """

    success: bool
    message: str
    escalate: bool = False


def resolve_conflicts(
    config: BabysitConfig,
    pr_state: PRState,
) -> StepResult:
    """Resolve merge conflicts on the PR.

    Checks if the PR has conflicts (``mergeable_state == "dirty"``),
    spawns a fixer agent with a conflict resolution prompt, and verifies
    that conflicts are resolved after the fixer completes.

    Args:
        config: Babysit configuration.
        pr_state: Current PR state snapshot.

    Returns:
        StepResult indicating success, failure, or escalation.
    """
    if not pr_state.has_conflicts:
        return StepResult(success=True, message="No merge conflicts detected")

    logger.info(
        "PR #%d has merge conflicts (mergeable_state=%s), attempting resolution",
        config.pr_number,
        pr_state.mergeable_state,
    )

    prompt = build_conflict_resolution_prompt(config.pr_number, config.repo)
    result = run_fixer(prompt, config, step_name="conflict_resolution")

    if not result.success:
        logger.warning(
            "Conflict resolution failed for PR #%d: %s",
            config.pr_number,
            result.error,
        )
        return StepResult(
            success=False,
            message=f"Conflict resolution failed: {result.error}",
            escalate=True,
        )

    # Verify conflicts are resolved by re-fetching PR state.
    try:
        updated_state = fetch_pr_state(config.pr_number, config.repo)
        if updated_state.has_conflicts:
            logger.warning("Conflicts persist after resolution attempt on PR #%d", config.pr_number)
            return StepResult(
                success=False,
                message="Conflicts persist after resolution attempt",
                escalate=True,
            )
    except Exception as exc:
        logger.warning("Failed to verify conflict resolution: %s", exc)
        # Assume success if we cannot re-fetch; the next CI wait will catch issues.
        return StepResult(
            success=True,
            message=f"Conflict resolution completed but verification failed: {exc}",
        )

    logger.info("Conflicts resolved successfully for PR #%d", config.pr_number)
    return StepResult(success=True, message="Merge conflicts resolved")


__all__ = [
    "StepResult",
    "resolve_conflicts",
]
