"""Reviewer agent spawner.

Spawns a Claude agent in read-only mode to review a pull request and
post a GitHub review via the ``gh`` CLI. Captures the review verdict
from PR state after the agent completes.

Note: Status comments summarising review results are managed by the
review step (``steps/review.py``) rather than this module. This module
is responsible only for spawning the reviewer agent and capturing its
raw output and verdict.
"""

import logging
import os
import subprocess
from dataclasses import dataclass

from egg_agent import build_agent_command

from .config import BabysitConfig
from .pr_state import fetch_pr_state
from .types import ReviewVerdict

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    """Result of a reviewer agent invocation.

    Attributes:
        verdict: Review verdict posted by the agent.
        comments: List of review comment bodies.
        error: Error message if the reviewer failed.
    """

    verdict: ReviewVerdict
    comments: list[str]
    error: str | None = None


def run_reviewer(
    prompt: str,
    config: BabysitConfig,
) -> ReviewResult:
    """Spawn a reviewer agent to review the PR.

    The reviewer agent runs in read-only mode (no git push permissions)
    and posts a GitHub review via ``gh pr review``. After the agent
    completes, the review verdict is captured from the PR state.

    Args:
        prompt: The review prompt for the agent.
        config: Babysit configuration.

    Returns:
        ReviewResult with verdict and comments.
    """
    logger.info("Spawning reviewer agent for PR #%d", config.pr_number)

    # Build agent command. Reviewer uses sonnet for cost efficiency.
    # Add read-only instruction to the prompt to enforce no-push behavior.
    readonly_prompt = (
        "IMPORTANT: You are running in READ-ONLY review mode. "
        "Do NOT run git push, git commit, or modify any files. "
        "Your only job is to review code and post a GitHub review via "
        "`gh pr review`.\n\n" + prompt
    )
    cmd = build_agent_command(readonly_prompt, model="sonnet", max_turns=100)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_reviewer_timeout(config),
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or f"Reviewer exited with code {result.returncode}"
            logger.warning("Reviewer agent failed: %s", error_msg)
            return ReviewResult(
                verdict=ReviewVerdict.PENDING,
                comments=[],
                error=error_msg,
            )

        # Fetch updated PR state to capture the review verdict.
        try:
            pr_state = fetch_pr_state(config.pr_number, config.repo)
            verdict = pr_state.review_verdict
            logger.info("Review verdict for PR #%d: %s", config.pr_number, verdict)
        except Exception as exc:
            logger.warning("Failed to fetch review verdict: %s", exc)
            verdict = ReviewVerdict.COMMENTED

        # Extract any review comments from agent output.
        comments = _extract_review_comments(result.stdout)

        return ReviewResult(
            verdict=verdict,
            comments=comments,
        )

    except subprocess.TimeoutExpired:
        logger.error("Reviewer agent timed out for PR #%d", config.pr_number)
        return ReviewResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            error="Reviewer agent timed out",
        )
    except Exception as exc:
        logger.error("Reviewer agent error: %s", exc)
        return ReviewResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            error=str(exc),
        )


def run_brc_reviewer(
    prompt: str,
    config: BabysitConfig,
) -> ReviewResult:
    """Spawn a BRC-wrapped reviewer agent.

    Like :func:`run_reviewer` but uses the consensus wrapper for concurrent
    BRC execution. The agent will participate in the BRC consensus
    protocol after completing its review.

    Args:
        prompt: The review prompt for the agent.
        config: Babysit configuration.

    Returns:
        ReviewResult with verdict and comments.
    """
    from orchestrator.consensus_wrapper import build_consensus_wrapped_command

    logger.info("Spawning BRC reviewer agent for PR #%d", config.pr_number)

    readonly_prompt = (
        "IMPORTANT: You are running in READ-ONLY review mode. "
        "Do NOT run git push, git commit, or modify any files. "
        "Your only job is to review code and post a GitHub review via "
        "`gh pr review`.\n\n" + prompt
    )
    cmd = build_consensus_wrapped_command(readonly_prompt, model="sonnet", max_turns=100)

    brc_env = os.environ.copy()
    brc_env.update(
        {
            "EGG_CONCURRENT_MODE": "true",
            "EGG_BRC_ROLE_TYPE": "reviewer",
            "EGG_AGENT_ROLE": "babysit_reviewer",
        }
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_reviewer_timeout(config),
            env=brc_env,
        )

        if result.returncode != 0:
            error_msg = (
                result.stderr.strip() or f"BRC reviewer exited with code {result.returncode}"
            )
            logger.warning("BRC reviewer agent failed: %s", error_msg)
            return ReviewResult(
                verdict=ReviewVerdict.PENDING,
                comments=[],
                error=error_msg,
            )

        # Fetch updated PR state to capture the review verdict.
        try:
            pr_state = fetch_pr_state(config.pr_number, config.repo)
            verdict = pr_state.review_verdict
            logger.info("BRC review verdict for PR #%d: %s", config.pr_number, verdict)
        except Exception as exc:
            logger.warning("Failed to fetch review verdict: %s", exc)
            verdict = ReviewVerdict.COMMENTED

        comments = _extract_review_comments(result.stdout)

        return ReviewResult(
            verdict=verdict,
            comments=comments,
        )

    except subprocess.TimeoutExpired:
        logger.error("BRC reviewer agent timed out for PR #%d", config.pr_number)
        return ReviewResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            error="BRC reviewer agent timed out",
        )
    except Exception as exc:
        logger.error("BRC reviewer agent error: %s", exc)
        return ReviewResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            error=str(exc),
        )


def _extract_review_comments(stdout: str) -> list[str]:
    """Extract review comments from agent output.

    Looks for structured review content in the agent's stdout. Falls
    back to treating the entire output as a single comment.

    Args:
        stdout: Agent standard output.

    Returns:
        List of comment strings.
    """
    if not stdout.strip():
        return []
    # Return non-empty lines as individual comments for downstream processing.
    # In practice, the agent posts reviews via gh, so stdout is informational.
    return [stdout.strip()]


def _reviewer_timeout(config: BabysitConfig) -> int:
    """Calculate reviewer subprocess timeout.

    Reviewers are read-only and should complete faster than fixers.
    Uses a quarter of the babysit timeout with a minimum of 300 seconds.
    """
    return max(300, config.timeout_seconds // 4)


__all__ = [
    "ReviewResult",
    "run_brc_reviewer",
    "run_reviewer",
]
