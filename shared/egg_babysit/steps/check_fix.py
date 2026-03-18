"""Check fixer step.

Fixes failing CI checks by first attempting non-LLM fixes (shell commands
from check-fixers.yml) and falling back to spawning an LLM fixer agent.
Tracks per-job retry counts and escalates after max retries.
"""

import logging
import os
from typing import Any

from ..config import BabysitConfig
from ..fixer import run_fixer, run_non_llm_fix
from ..prompts import (
    build_check_fixer_prompt,
    get_max_retries,
    get_non_llm_fix_command,
    load_check_fixers_config,
)
from ..types import CICheckResult
from .conflict import StepResult

logger = logging.getLogger(__name__)


def fix_failed_checks(
    config: BabysitConfig,
    failed_checks: list[CICheckResult],
    retry_counts: dict[str, int],
) -> StepResult:
    """Fix failing CI checks.

    For each failing check:
    1. Check retry count against max retries. Escalate if exceeded.
    2. Try non-LLM fix command from check-fixers.yml (if configured).
    3. If non-LLM fix fails or is unavailable, spawn LLM fixer agent.
    4. Increment retry count for the job.

    Args:
        config: Babysit configuration.
        failed_checks: List of failing CI check results.
        retry_counts: Mutable dict of job_name -> retry count. Updated in place.

    Returns:
        StepResult indicating success, failure, or escalation.
    """
    if not failed_checks:
        return StepResult(success=True, message="No failing checks to fix")

    check_fixers_config = load_check_fixers_config(config.check_fixers_path)
    repo_path = os.environ.get("EGG_REPO_PATH", "")
    jobs_exceeding_retries: list[str] = []
    jobs_fixed: list[str] = []
    jobs_failed: list[str] = []

    for check in failed_checks:
        job_name = check.name
        current_retries = retry_counts.get(job_name, 0)

        # Determine max retries for this job.
        # Try to match the job name against workflow/job in config.
        workflow_name, job_key = _match_job(job_name, check_fixers_config)
        max_retries = (
            get_max_retries(workflow_name, job_key, check_fixers_config)
            if workflow_name
            else config.max_retries_per_job
        )

        if current_retries >= max_retries:
            logger.warning(
                "Job '%s' exceeded max retries (%d/%d), escalating",
                job_name,
                current_retries,
                max_retries,
            )
            jobs_exceeding_retries.append(job_name)
            continue

        # Increment retry count.
        retry_counts[job_name] = current_retries + 1
        logger.info(
            "Attempting fix for '%s' (retry %d/%d)",
            job_name,
            retry_counts[job_name],
            max_retries,
        )

        # Try non-LLM fix first.
        fixed = False
        if workflow_name:
            non_llm_cmd = get_non_llm_fix_command(workflow_name, job_key, check_fixers_config)
            if non_llm_cmd:
                logger.info("Trying non-LLM fix for '%s'", job_name)
                if run_non_llm_fix(non_llm_cmd, repo_path):
                    # Non-LLM fix succeeded; need to commit changes.
                    commit_result = _commit_non_llm_fix(job_name, repo_path)
                    if commit_result:
                        jobs_fixed.append(job_name)
                        fixed = True
                    else:
                        logger.info("Non-LLM fix produced no changes for '%s'", job_name)

        # Fall back to LLM fixer if non-LLM fix was not available or failed.
        if not fixed:
            logger.info("Using LLM fixer for '%s'", job_name)
            prompt = build_check_fixer_prompt(
                config.pr_number,
                config.repo,
                [job_name],
                repo_path=repo_path,
            )
            result = run_fixer(prompt, config, step_name=f"check_fix:{job_name}")
            if result.success:
                jobs_fixed.append(job_name)
            else:
                jobs_failed.append(job_name)

    # Build result summary.
    if jobs_exceeding_retries:
        escalate_msg = f"Jobs exceeding max retries: {', '.join(jobs_exceeding_retries)}"
        if jobs_failed:
            escalate_msg += f"; Jobs still failing: {', '.join(jobs_failed)}"
        return StepResult(
            success=False,
            message=escalate_msg,
            escalate=True,
        )

    if jobs_failed:
        return StepResult(
            success=False,
            message=f"Fix attempts failed for: {', '.join(jobs_failed)}",
        )

    return StepResult(
        success=True,
        message=f"Fixed {len(jobs_fixed)} check(s): {', '.join(jobs_fixed)}",
    )


def _match_job(
    job_name: str,
    config: dict[str, Any],
) -> tuple[str, str]:
    """Match a CI job name to a workflow/job pair in check-fixers config.

    Uses case-insensitive substring matching since GitHub Actions job
    names may differ from the config keys.

    Args:
        job_name: GitHub Actions job name.
        config: Parsed check-fixers.yml config.

    Returns:
        Tuple of (workflow_name, job_key). Both empty if no match found.
    """
    workflows = config.get("workflows", {})
    job_lower = job_name.lower()

    for workflow_name, jobs in workflows.items():
        if not isinstance(jobs, dict):
            continue
        for job_key in jobs:
            if job_key.lower() in job_lower or job_lower in job_key.lower():
                return workflow_name, job_key

    return "", ""


def _commit_non_llm_fix(job_name: str, repo_path: str) -> bool:
    """Commit changes from a non-LLM fix.

    Stages all changes and commits with a descriptive message. Returns
    False if there are no changes to commit.

    Args:
        job_name: Name of the fixed job (for commit message).
        repo_path: Repository working directory.

    Returns:
        True if a commit was created.
    """
    import subprocess

    effective_path = repo_path or os.environ.get("EGG_REPO_PATH", ".")

    try:
        # Check for changes.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=effective_path,
        )
        if not status.stdout.strip():
            return False

        # Stage only tracked modified files (git add -u) to avoid
        # accidentally staging sensitive untracked files like .env.
        subprocess.run(
            ["git", "add", "-u"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=effective_path,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"fix: auto-fix {job_name} check"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=effective_path,
            check=True,
        )
        # Push the fix.
        subprocess.run(
            ["git", "push"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=effective_path,
            check=True,
        )
        logger.info("Committed and pushed non-LLM fix for '%s'", job_name)
        return True

    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to commit non-LLM fix for '%s': %s", job_name, exc)
        return False
    except Exception as exc:
        logger.warning("Error committing non-LLM fix: %s", exc)
        return False


__all__ = [
    "fix_failed_checks",
]
