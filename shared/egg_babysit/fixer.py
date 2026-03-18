"""Fixer agent spawner.

Spawns Claude agents to fix CI check failures, resolve merge conflicts,
and address review feedback. Also supports non-LLM fixes via shell commands.
"""

import logging
import os
import subprocess
from dataclasses import dataclass

from egg_agent import build_agent_command

from .config import BabysitConfig

logger = logging.getLogger(__name__)


@dataclass
class FixerResult:
    """Result of a fixer agent invocation.

    Attributes:
        success: Whether the fixer completed successfully.
        commit_sha: SHA of the commit created by the fixer, if any.
        error: Error message if the fixer failed.
    """

    success: bool
    commit_sha: str | None = None
    error: str | None = None


def run_fixer(
    prompt: str,
    config: BabysitConfig,
    step_name: str,
    elapsed: float = 0,
) -> FixerResult:
    """Spawn a fixer agent to address an issue.

    Constructs and runs an agent command via subprocess. After the agent
    completes, parses the latest git commit SHA to detect if a fix was
    committed.

    Args:
        prompt: The prompt to send to the fixer agent.
        config: Babysit configuration.
        step_name: Human-readable name for logging (e.g., "check_fix", "conflict").
        elapsed: Seconds already elapsed in the babysit loop.

    Returns:
        FixerResult with success status and optional commit SHA.
    """
    logger.info("Spawning fixer agent for step: %s", step_name)

    cmd = build_agent_command(prompt, model="sonnet", max_turns=200)

    # Record HEAD SHA before the agent runs so we can detect new commits.
    pre_sha = _get_head_sha(config)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_agent_timeout(config, elapsed),
            cwd=_repo_path(config),
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or f"Agent exited with code {result.returncode}"
            logger.warning("Fixer agent failed for %s: %s", step_name, error_msg)
            return FixerResult(success=False, error=error_msg)

        # Check if a new commit was created.
        post_sha = _get_head_sha(config)
        commit_sha = post_sha if post_sha and post_sha != pre_sha else None

        if commit_sha:
            logger.info("Fixer agent created commit %s for %s", commit_sha[:12], step_name)
        else:
            logger.info("Fixer agent completed %s without new commits", step_name)

        return FixerResult(success=True, commit_sha=commit_sha)

    except subprocess.TimeoutExpired:
        logger.error("Fixer agent timed out for %s", step_name)
        return FixerResult(success=False, error=f"Agent timed out for {step_name}")
    except Exception as exc:
        logger.error("Fixer agent error for %s: %s", step_name, exc)
        return FixerResult(success=False, error=str(exc))


def run_non_llm_fix(command: str, repo_path: str) -> bool:
    """Run a non-LLM fix command (shell script).

    Executes the command in the repo directory. Used for mechanical fixes
    like auto-formatting that do not require an LLM agent.

    Args:
        command: Shell command to execute.
        repo_path: Working directory for the command.

    Returns:
        True if the command succeeded (exit code 0).
    """
    effective_path = repo_path or os.environ.get("EGG_REPO_PATH", ".")
    logger.info("Running non-LLM fix in %s: %s", effective_path, command[:100])

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=effective_path,
        )

        if result.returncode == 0:
            logger.info("Non-LLM fix succeeded")
            return True
        else:
            logger.warning(
                "Non-LLM fix failed (exit %d): %s",
                result.returncode,
                result.stderr.strip()[:200],
            )
            return False

    except subprocess.TimeoutExpired:
        logger.error("Non-LLM fix timed out after 300s")
        return False
    except Exception as exc:
        logger.error("Non-LLM fix error: %s", exc)
        return False


def _get_head_sha(config: BabysitConfig) -> str | None:
    """Get the current HEAD SHA in the repo.

    Returns:
        Commit SHA string, or None on error.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=_repo_path(config),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _repo_path(config: BabysitConfig) -> str:
    """Resolve the repository working directory."""
    return os.environ.get("EGG_REPO_PATH", ".")


def _agent_timeout(config: BabysitConfig, elapsed: float = 0) -> int:
    """Calculate agent subprocess timeout.

    Uses half the remaining babysit timeout to leave room for other
    operations, with a minimum of 300 seconds.

    Args:
        config: Babysit configuration.
        elapsed: Seconds already elapsed in the babysit loop.

    Returns:
        Timeout in seconds for the agent subprocess.
    """
    remaining = max(0, config.timeout_seconds - int(elapsed))
    return max(300, remaining // 2)


__all__ = [
    "FixerResult",
    "run_fixer",
    "run_non_llm_fix",
]
