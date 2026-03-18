"""CLI entry point for babysit-pr.

Provides ``main()`` which parses arguments, auto-detects configuration,
and runs the babysit loop. Can be invoked directly or via
``python -m egg_babysit``.
"""

import argparse
import logging
import os
import subprocess
import sys

from .config import BabysitConfig
from .loop import babysit
from .types import BabysitExitReason

logger = logging.getLogger(__name__)


def main() -> None:
    """CLI entry point for babysit-pr."""
    parser = argparse.ArgumentParser(
        prog="egg-babysit",
        description="Babysit a GitHub PR through CI, review, and merge.",
    )
    parser.add_argument(
        "pr_number",
        type=int,
        help="GitHub PR number to babysit.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default="",
        help="Repository in owner/repo format. Auto-detected from git remote if not provided.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=14400,
        help="Maximum wall-clock time in seconds (default: 14400 = 4 hours).",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=10,
        help="Maximum fix-check-review iterations (default: 10).",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between CI status polls (default: 30).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Default max retries per failing CI job (default: 3).",
    )
    parser.add_argument(
        "--max-feedback-rounds",
        type=int,
        default=5,
        help="Maximum review feedback addressing rounds (default: 5).",
    )
    parser.add_argument(
        "--check-fixers",
        type=str,
        default="",
        help="Path to check-fixers.yml config.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args()

    # Configure logging.
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Auto-detect repo from git remote.
    repo = args.repo or _detect_repo()
    if not repo:
        logger.error("Could not detect repository. Use --repo owner/repo.")
        sys.exit(1)

    # Auto-detect orchestrator URL.
    orchestrator_url = os.environ.get("EGG_ORCHESTRATOR_URL", "")

    # Auto-generate pipeline ID.
    pipeline_id = os.environ.get("EGG_PIPELINE_ID", f"pr-{args.pr_number}")

    config = BabysitConfig(
        pr_number=args.pr_number,
        repo=repo,
        timeout_seconds=args.timeout,
        max_iterations=args.max_iterations,
        poll_interval_seconds=args.poll_interval,
        max_retries_per_job=args.max_retries,
        max_feedback_rounds=args.max_feedback_rounds,
        check_fixers_path=args.check_fixers,
        orchestrator_url=orchestrator_url,
        pipeline_id=pipeline_id,
    )

    logger.info("Babysitting PR #%d in %s", config.pr_number, config.repo)
    logger.info("Config: timeout=%ds, max_iter=%d, poll=%ds", config.timeout_seconds, config.max_iterations, config.poll_interval_seconds)

    # Register pipeline with orchestrator (best-effort).
    _register_pipeline(config)

    # Run the babysit loop.
    result = babysit(config)

    # Print result summary.
    print(f"\n{'=' * 60}")
    print(f"Babysit Result: {result.exit_reason}")
    print(f"  Iterations: {result.iterations}")
    print(f"  Duration: {result.duration_seconds:.0f}s")
    print(f"  Last step: {result.last_step}")
    if result.message:
        print(f"  Message: {result.message}")
    print(f"{'=' * 60}")

    # Exit with appropriate code.
    if result.exit_reason in (BabysitExitReason.MERGED,):
        sys.exit(0)
    elif result.exit_reason in (BabysitExitReason.ESCALATED, BabysitExitReason.CANCELLED):
        sys.exit(0)  # Escalation is a valid exit; human takes over.
    else:
        sys.exit(1)


def _detect_repo() -> str:
    """Auto-detect repository from git remote.

    Parses the output of ``git remote -v`` to extract the owner/repo
    format. Supports both HTTPS and SSH remote URLs.

    Returns:
        Repository in owner/repo format, or empty string on failure.
    """
    repo_path = os.environ.get("EGG_REPO_PATH", ".")
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_path,
        )
        if result.returncode != 0:
            return ""

        for line in result.stdout.splitlines():
            if "(fetch)" not in line:
                continue
            # Parse HTTPS URL: https://github.com/owner/repo.git
            if "github.com/" in line:
                parts = line.split("github.com/")
                if len(parts) >= 2:
                    repo = parts[1].split()[0]
                    repo = repo.removesuffix(".git")
                    # Handle SSH format: git@github.com:owner/repo.git
                    repo = repo.lstrip(":")
                    if "/" in repo:
                        return repo

    except Exception as exc:
        logger.debug("Failed to detect repo from git remote: %s", exc)

    return ""


def _register_pipeline(config: BabysitConfig) -> None:
    """Register the babysit pipeline with the orchestrator (best-effort).

    Args:
        config: Babysit configuration.
    """
    if not config.orchestrator_url:
        return

    try:
        subprocess.run(
            [
                "egg-orch", "progress", "emit",
                "--step", "babysit_start",
                "--state", "working",
                "--detail", f"Babysitting PR #{config.pr_number} in {config.repo}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        logger.debug("Registered babysit pipeline with orchestrator")
    except FileNotFoundError:
        logger.debug("egg-orch not available, skipping pipeline registration")
    except Exception as exc:
        logger.debug("Failed to register pipeline: %s", exc)


__all__ = [
    "main",
]
