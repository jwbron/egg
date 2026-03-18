"""HITL escalation for the babysit-pr loop.

Provides mechanisms to escalate issues to humans: orchestrator HITL
decisions, GitHub PR comments, and Slack notifications.
"""

import logging
import subprocess
from datetime import UTC

from .config import BabysitConfig

logger = logging.getLogger(__name__)


def escalate(
    config: BabysitConfig,
    reason: str,
    context: str,
) -> None:
    """Escalate an issue to a human via all available channels.

    Attempts each escalation channel independently. Failures in one
    channel do not prevent attempts on other channels.

    Args:
        config: Babysit configuration.
        reason: Short reason for escalation (used as title/subject).
        context: Detailed context (used as body/description).
    """
    logger.info(
        "Escalating PR #%d: %s",
        config.pr_number,
        reason,
    )

    # Post GitHub PR comment (most reliable channel).
    comment_body = (
        f"## Babysit Escalation\n\n"
        f"**Reason:** {reason}\n\n"
        f"**Context:**\n{context}\n\n"
        f"---\n"
        f"*This PR requires human attention. The automated babysit loop "
        f"has reached a state it cannot resolve autonomously.*"
    )
    post_pr_comment(config.pr_number, config.repo, comment_body)

    # Create HITL decision via orchestrator (best-effort).
    _escalate_via_orchestrator(config, reason, context)

    # Send Slack notification (best-effort).
    _escalate_via_slack(config, reason)


def post_pr_comment(pr_number: int, repo: str, body: str) -> bool:
    """Post a comment on a GitHub PR.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.
        body: Comment body (Markdown).

    Returns:
        True if the comment was posted successfully.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "comment",
                str(pr_number),
                "--repo",
                repo,
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("Posted escalation comment on PR #%d", pr_number)
            return True
        else:
            logger.warning(
                "Failed to post PR comment (exit %d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return False
    except Exception as exc:
        logger.warning("Error posting PR comment: %s", exc)
        return False


def _escalate_via_orchestrator(
    config: BabysitConfig,
    reason: str,
    context: str,
) -> None:
    """Create a HITL decision via the orchestrator API.

    Uses the egg-orch CLI to create a decision request. This is
    best-effort; failures are logged but not raised.

    Args:
        config: Babysit configuration.
        reason: Escalation reason.
        context: Detailed context.
    """
    if not config.orchestrator_url:
        logger.debug("No orchestrator URL configured, skipping HITL decision")
        return

    try:
        # Use egg-contract for HITL decision if available.
        subprocess.run(
            [
                "egg-contract",
                "add-decision",
                "--question",
                f"Babysit escalation: {reason}",
                "--options",
                "Resolve manually",
                "Retry babysit",
                "Close PR",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        logger.info("Created HITL decision for escalation")
    except FileNotFoundError:
        logger.debug("egg-contract not available, skipping HITL decision")
    except Exception as exc:
        logger.debug("Failed to create HITL decision: %s", exc)


def _escalate_via_slack(config: BabysitConfig, reason: str) -> None:
    """Send a Slack notification about the escalation.

    Uses the file-based notification mechanism. This is best-effort.

    Args:
        config: Babysit configuration.
        reason: Escalation reason.
    """
    import os
    from datetime import datetime
    from pathlib import Path

    notifications_dir = Path(os.path.expanduser("~/sharing/notifications"))
    if not notifications_dir.is_dir():
        logger.debug("Notifications directory not found, skipping Slack notification")
        return

    try:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        filename = f"{timestamp}-babysit-escalation.md"
        notification_path = notifications_dir / filename

        content = (
            f"# Babysit Escalation: PR #{config.pr_number}\n\n"
            f"**Repo:** {config.repo}\n"
            f"**Reason:** {reason}\n\n"
            f"PR requires human attention.\n"
        )
        notification_path.write_text(content)
        logger.info("Created Slack notification file: %s", filename)
    except Exception as exc:
        logger.debug("Failed to create Slack notification: %s", exc)


__all__ = [
    "escalate",
    "post_pr_comment",
]
