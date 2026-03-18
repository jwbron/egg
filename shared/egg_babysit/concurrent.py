"""Concurrent BRC executor for babysit-pr review/feedback phase.

Runs fixer and reviewer agents concurrently using the BRC consensus
protocol. The fixer acts as a producer (proposes fixes) and the
reviewer acts as a reviewer (ACKs/NACKs proposals).
"""

import logging
import os
import subprocess
from dataclasses import dataclass

from .config import BabysitConfig
from .pr_state import fetch_pr_state, fetch_review_comments
from .prompts import build_feedback_fixer_prompt, build_review_prompt
from .types import BabysitAgentRole, ReviewVerdict

logger = logging.getLogger(__name__)


@dataclass
class ConcurrentReviewResult:
    """Result of a concurrent review/feedback phase.

    Attributes:
        verdict: Review verdict after concurrent execution.
        comments: Review comments collected from the reviewer agent.
        consensus_reached: Whether BRC consensus was reached.
        rounds_used: Number of BRC rounds used.
        escalated: Whether the result requires human escalation.
        message: Human-readable summary message.
    """

    verdict: ReviewVerdict
    comments: list[str]
    consensus_reached: bool
    rounds_used: int
    escalated: bool = False
    message: str = ""


def run_concurrent_review(
    config: BabysitConfig,
    elapsed: float = 0,
) -> ConcurrentReviewResult:
    """Run fixer and reviewer concurrently using BRC consensus.

    The fixer examines review feedback and proposes fixes, then signals
    via ``egg-orch consensus propose``. The reviewer reviews the code and
    ACKs or NACKs via ``egg-orch consensus ack/nack``. They iterate until
    consensus or the flip-flop cap is reached.

    Args:
        config: Babysit configuration.
        elapsed: Seconds already elapsed in the babysit loop.

    Returns:
        ConcurrentReviewResult with verdict and consensus status.
    """
    repo_path = os.environ.get("EGG_REPO_PATH", ".")

    # Build prompts for both agents.
    review_prompt = build_review_prompt(config.pr_number, config.repo, repo_path=repo_path)

    # Fetch current review comments for the fixer.
    comments = fetch_review_comments(config.pr_number, config.repo)
    if comments:
        fixer_prompt = build_feedback_fixer_prompt(config.pr_number, config.repo, comments)
    else:
        # No feedback yet — fixer waits for reviewer to post feedback.
        fixer_prompt = _build_initial_fixer_prompt(config)

    # Calculate timeouts.
    remaining = max(300, config.timeout_seconds - int(elapsed))
    consensus_timeout = min(config.consensus_timeout_minutes * 60, remaining)

    # Lazy import to avoid pulling orchestrator into sandbox at module load.
    from orchestrator.consensus_wrapper import build_consensus_wrapped_command

    # Build consensus-wrapped commands for both agents.
    fixer_cmd = build_consensus_wrapped_command(
        fixer_prompt,
        model="sonnet",
        max_turns=200,
        max_restarts=2,
    )

    reviewer_cmd = build_consensus_wrapped_command(
        review_prompt,
        model="sonnet",
        max_turns=100,
        max_restarts=2,
    )

    # Set BRC environment variables.
    brc_env = os.environ.copy()

    fixer_env = {
        **brc_env,
        "EGG_CONCURRENT_MODE": "true",
        "EGG_BRC_ROLE_TYPE": "producer",
        "EGG_AGENT_ROLE": BabysitAgentRole.BABYSIT_FIXER,
        "EGG_BRC_REVIEWERS": BabysitAgentRole.BABYSIT_REVIEWER,
    }

    reviewer_env = {
        **brc_env,
        "EGG_CONCURRENT_MODE": "true",
        "EGG_BRC_ROLE_TYPE": "reviewer",
        "EGG_AGENT_ROLE": BabysitAgentRole.BABYSIT_REVIEWER,
        "EGG_BRC_PRODUCERS": BabysitAgentRole.BABYSIT_FIXER,
    }

    # Spawn both agents concurrently.
    logger.info(
        "Starting concurrent BRC review for PR #%d (timeout=%ds, max_rounds=%d)",
        config.pr_number,
        consensus_timeout,
        config.max_consensus_rounds,
    )

    fixer_proc = subprocess.Popen(
        fixer_cmd,
        env=fixer_env,
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    reviewer_proc = subprocess.Popen(
        reviewer_cmd,
        env=reviewer_env,
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for both to complete concurrently using threads so that
    # one process blocking does not prevent the other from being
    # collected (or timed out) in a timely manner.
    import threading

    fixer_result: dict[str, str] = {}
    reviewer_result: dict[str, str] = {}
    fixer_exc: BaseException | None = None
    reviewer_exc: BaseException | None = None

    def _communicate_fixer() -> None:
        nonlocal fixer_exc
        try:
            out, err = fixer_proc.communicate(timeout=consensus_timeout)
            fixer_result["stdout"] = out
            fixer_result["stderr"] = err
        except BaseException as exc:
            fixer_exc = exc

    def _communicate_reviewer() -> None:
        nonlocal reviewer_exc
        try:
            out, err = reviewer_proc.communicate(timeout=consensus_timeout)
            reviewer_result["stdout"] = out
            reviewer_result["stderr"] = err
        except BaseException as exc:
            reviewer_exc = exc

    fixer_thread = threading.Thread(target=_communicate_fixer, daemon=True)
    reviewer_thread = threading.Thread(target=_communicate_reviewer, daemon=True)
    fixer_thread.start()
    reviewer_thread.start()
    fixer_thread.join(timeout=consensus_timeout + 30)
    reviewer_thread.join(timeout=consensus_timeout + 30)

    # Handle timeout or exception from either process.
    timed_out = False
    if fixer_exc is not None or reviewer_exc is not None:
        timed_out = isinstance(fixer_exc, subprocess.TimeoutExpired) or isinstance(
            reviewer_exc, subprocess.TimeoutExpired
        )

    if timed_out:
        logger.warning("Concurrent review timed out for PR #%d", config.pr_number)
        fixer_proc.kill()
        reviewer_proc.kill()
        fixer_proc.wait()
        reviewer_proc.wait()
        return ConcurrentReviewResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            consensus_reached=False,
            rounds_used=0,
            escalated=True,
            message="Concurrent review timed out — escalating to human",
        )

    _fixer_stdout = fixer_result.get("stdout", "")  # noqa: F841
    fixer_stderr = fixer_result.get("stderr", "")
    reviewer_stdout = reviewer_result.get("stdout", "")
    reviewer_stderr = reviewer_result.get("stderr", "")

    # Check exit codes.
    fixer_ok = fixer_proc.returncode == 0
    reviewer_ok = reviewer_proc.returncode == 0

    if not fixer_ok:
        logger.warning(
            "Fixer agent failed (code %d): %s",
            fixer_proc.returncode,
            fixer_stderr[:500] if fixer_stderr else "",
        )
    if not reviewer_ok:
        logger.warning(
            "Reviewer agent failed (code %d): %s",
            reviewer_proc.returncode,
            reviewer_stderr[:500] if reviewer_stderr else "",
        )

    # Fetch updated PR state to get the review verdict.
    try:
        pr_state = fetch_pr_state(config.pr_number, config.repo)
        verdict = pr_state.review_verdict
    except Exception as exc:
        logger.warning("Failed to fetch review verdict after concurrent review: %s", exc)
        verdict = ReviewVerdict.PENDING

    # Both agents exiting 0 means the consensus wrapper completed successfully.
    consensus_reached = fixer_ok and reviewer_ok

    comments_out: list[str] = []
    if reviewer_stdout and reviewer_stdout.strip():
        comments_out.append(reviewer_stdout.strip())

    logger.info(
        "Concurrent review complete for PR #%d: verdict=%s, consensus=%s",
        config.pr_number,
        verdict,
        consensus_reached,
    )

    return ConcurrentReviewResult(
        verdict=verdict,
        comments=comments_out,
        consensus_reached=consensus_reached,
        rounds_used=1,  # BRC handles rounds internally.
        message=f"Concurrent review completed: verdict={verdict}",
    )


def _build_initial_fixer_prompt(config: BabysitConfig) -> str:
    """Build a prompt for the fixer when there is no feedback yet.

    The fixer will wait for the reviewer to post feedback, then
    address it.

    Args:
        config: Babysit configuration.

    Returns:
        Prompt string for the fixer agent.
    """
    repo_path = os.environ.get("EGG_REPO_PATH", ".")
    return f"""\
You are a code fixer agent for PR #{config.pr_number} in {config.repo}.

You are running concurrently with a reviewer agent. The reviewer will post
feedback on the PR. Your job is to:

1. Wait for the reviewer to post their review via `gh pr review`.
2. Read the review feedback.
3. Address all issues raised by the reviewer.
4. Commit and push your fixes.
5. When done, propose consensus via `egg-orch consensus propose`.

If the reviewer NACKs your proposal, address their concerns, fix the issues,
and re-propose.

Working directory: {repo_path}
Repository: {config.repo}
PR: #{config.pr_number}
"""


__all__ = [
    "ConcurrentReviewResult",
    "run_concurrent_review",
]
