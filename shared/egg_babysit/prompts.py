"""Prompt builders for babysit-pr sub-agents.

Constructs prompts for check-fixer, reviewer, conflict-resolution, and
feedback-addressing agents. Loads the check-fixers.yml configuration
to determine non-LLM fix commands and per-job retry limits.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Default paths to search for check-fixers.yml.
_CHECK_FIXERS_SEARCH_PATHS = [
    ".egg/check-fixers.yml",  # Repo-local override.
]

# Fallback path within the egg shared directory.
_SHARED_CHECK_FIXERS = Path(__file__).parent.parent / "check-fixers.yml"


def load_check_fixers_config(path: str = "", base_branch: str = "main") -> dict[str, Any]:
    """Load and parse check-fixers.yml configuration.

    Searches for the config in the following order:
    1. Explicit ``path`` argument.
    2. Repo-local ``.egg/check-fixers.yml`` **from the base branch** (via
       ``git show``). This prevents a malicious PR from injecting arbitrary
       shell commands through a modified check-fixers.yml on the PR branch.
    3. Shared ``shared/check-fixers.yml`` (bundled with egg).

    Args:
        path: Explicit path to check-fixers.yml. If empty, auto-detect.
        base_branch: Base branch to read repo-local config from (default "main").

    Returns:
        Parsed YAML as a dict. Returns empty dict on load failure.
    """
    if path:
        config_path = Path(path)
        if config_path.is_file():
            return _load_yaml(config_path)
        logger.warning("check-fixers.yml not found at %s", path)
        return {}

    # Read repo-local config from the base branch to prevent command
    # injection from untrusted PR branches.
    repo_path = os.environ.get("EGG_REPO_PATH", "")
    if repo_path:
        for relative in _CHECK_FIXERS_SEARCH_PATHS:
            content = _read_from_base_branch(relative, base_branch, repo_path)
            if content is not None:
                logger.debug("Using check-fixers from %s:%s", base_branch, relative)
                return _parse_yaml_string(content, f"{base_branch}:{relative}")

    # Fallback to shared config.
    if _SHARED_CHECK_FIXERS.is_file():
        logger.debug("Using shared check-fixers: %s", _SHARED_CHECK_FIXERS)
        return _load_yaml(_SHARED_CHECK_FIXERS)

    logger.info("No check-fixers.yml found, using empty config")
    return {}


def _read_from_base_branch(relative_path: str, base_branch: str, repo_path: str) -> str | None:
    """Read a file from the base branch using git show.

    Returns the file contents as a string, or None if the file does not
    exist on the base branch or git is not available.
    """
    try:
        result = subprocess.run(
            ["git", "show", f"origin/{base_branch}:{relative_path}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_path,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception as exc:
        logger.debug("Failed to read %s from %s: %s", relative_path, base_branch, exc)
    return None


def _parse_yaml_string(content: str, source: str = "") -> dict[str, Any]:
    """Parse a YAML string, returning empty dict on error."""
    try:
        data = yaml.safe_load(content)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to parse YAML from %s: %s", source, exc)
        return {}


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning empty dict on error."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return {}


def get_non_llm_fix_command(
    workflow: str,
    job: str,
    config: dict[str, Any],
) -> str | None:
    """Get the non-LLM fix command for a workflow/job, if configured.

    Args:
        workflow: Workflow name (e.g., "Lint").
        job: Job name within the workflow (e.g., "Python").
        config: Parsed check-fixers.yml config.

    Returns:
        Shell command string, or None if no non-LLM fix is configured.
    """
    workflows = config.get("workflows", {})
    workflow_config = workflows.get(workflow, {})
    job_config = workflow_config.get(job, {})

    if isinstance(job_config, dict):
        command = job_config.get("non_llm_fix")
        if command and isinstance(command, str):
            return str(command.strip())
    return None


def get_max_retries(
    workflow: str,
    job: str,
    config: dict[str, Any],
) -> int:
    """Get the max retries for a workflow/job.

    Checks job-level, then defaults section.

    Args:
        workflow: Workflow name.
        job: Job name.
        config: Parsed check-fixers.yml config.

    Returns:
        Maximum retry count.
    """
    defaults = config.get("defaults", {})
    default_retries = int(defaults.get("max_retries", 3))

    workflows = config.get("workflows", {})
    workflow_config = workflows.get(workflow, {})
    job_config = workflow_config.get(job, {})

    if isinstance(job_config, dict):
        return int(job_config.get("max_retries", default_retries))
    return default_retries


def build_check_fixer_prompt(
    pr_number: int,
    repo: str,
    failed_jobs: list[str],
    repo_path: str = "",
) -> str:
    """Build a prompt for the check-fixer agent.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.
        failed_jobs: List of failing CI job names.
        repo_path: Local path to the repository checkout.

    Returns:
        Complete prompt string for the fixer agent.
    """
    effective_repo_path = repo_path or os.environ.get("EGG_REPO_PATH", "~/repos")
    jobs_list = "\n".join(f"  - {job}" for job in failed_jobs)

    return f"""\
You are a CI check fixer agent. Your job is to fix failing CI checks on PR #{pr_number}
in the {repo} repository.

## Failing Checks

The following CI jobs are failing:
{jobs_list}

## Instructions

1. Investigate ALL failing checks - make a complete list before fixing anything.
2. For each failing check, examine the logs and error messages.
3. Fix all auto-fixable issues without committing first.
4. Run checks locally to verify fixes work:
   - Look for a Makefile, package.json scripts, or pyproject.toml for project-specific commands.
   - Common commands: `make lint`, `make test`, `make build`.
5. Only after ALL checks pass locally: commit all fixes together.

## Autofixer Rules

- Fix ALL issues before committing. Investigate every failure first, then fix them all together.
- Never skip a failure because it is "pre-existing." Make all checks green on this branch.
- Auto-fix mechanical issues (formatting, imports, type annotations) directly.
- For complex issues requiring design decisions, report what is needed instead of guessing.
- Run ALL checks locally before committing. Repeat fix-and-verify until all pass.

## Repository

Working directory: {effective_repo_path}
Repository: {repo}
PR: #{pr_number}

After fixing, commit changes with a clear message describing the fixes and push.
"""


def build_review_prompt(
    pr_number: int,
    repo: str,
    repo_path: str = "",
) -> str:
    """Build a prompt for the reviewer agent.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.
        repo_path: Local path to the repository checkout.

    Returns:
        Complete prompt string for the reviewer agent.
    """
    effective_repo_path = repo_path or os.environ.get("EGG_REPO_PATH", "~/repos")

    return f"""\
You are a code reviewer agent. Review PR #{pr_number} in the {repo} repository.

## Instructions

1. Examine every changed file systematically. Do not skim.
2. Read surrounding context - check how changed code integrates with the rest of the codebase.
3. Trace data flow from input to output, especially for security-sensitive paths.
4. Consider edge cases the author may not have tested.

## What to Review

**Security** (highest priority):
- Injection vulnerabilities, authentication/authorization flaws
- Credential exposure, hardcoded secrets
- SSRF, open redirects, unsafe deserialization

**Correctness**:
- Logic errors, off-by-one, boundary conditions
- Race conditions, null handling, missing error paths
- Resource leaks

**Robustness**:
- Missing input validation at trust boundaries
- Unhandled exceptions, missing retry logic, inadequate timeouts

## Severity

**Blocking** (request changes): Security vulnerabilities, logic errors producing incorrect results,
breaking changes, resource leaks.

**Non-blocking** (suggestions): Code quality, naming, documentation gaps, style deviations.

## Output

Post your review using `gh pr review {pr_number} --repo {repo}` with one of:
- `--approve` if the PR is ready to merge
- `--request-changes --body "<issues>"` if there are blocking issues
- `--comment --body "<feedback>"` if you have non-blocking suggestions only

Working directory: {effective_repo_path}
"""


def build_conflict_resolution_prompt(
    pr_number: int,
    repo: str,
) -> str:
    """Build a prompt for the conflict resolution agent.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.

    Returns:
        Complete prompt string for the conflict resolution agent.
    """
    return f"""\
You are a merge conflict resolution agent. PR #{pr_number} in {repo} has merge conflicts
that need to be resolved.

## Instructions

1. Fetch the latest base branch: `git fetch origin main` (or the appropriate base branch).
2. Attempt to merge the base branch into the PR branch: `git merge origin/main`.
3. For each conflicted file:
   - Examine both sides of the conflict carefully.
   - Understand the intent of both changes.
   - Resolve in a way that preserves both changes where possible.
   - If changes are incompatible, prefer the PR's changes but ensure correctness.
4. After resolving all conflicts, run tests to verify nothing is broken.
5. Commit the merge resolution and push.

## Important

- Do NOT force-push or rebase. Use merge commits.
- If conflicts are too complex to resolve safely, report the issue instead of guessing.
- Verify the build and tests pass after resolution.

Repository: {repo}
PR: #{pr_number}
"""


def build_feedback_fixer_prompt(
    pr_number: int,
    repo: str,
    review_comments: list[str],
) -> str:
    """Build a prompt for the feedback-addressing agent.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.
        review_comments: List of review comment bodies to address.

    Returns:
        Complete prompt string for the feedback fixer agent.
    """
    comments_section = "\n\n---\n\n".join(
        f"**Comment {i + 1}:**\n{comment}" for i, comment in enumerate(review_comments)
    )

    return f"""\
You are a feedback-addressing agent. PR #{pr_number} in {repo} has received review feedback
that needs to be addressed.

## Review Comments

{comments_section}

## Instructions

1. Read each review comment carefully.
2. For each comment:
   - If the reviewer requests a code change, make the change.
   - If the reviewer asks a question, add a code comment or improve documentation.
   - If you disagree with a suggestion, note your reasoning (but still make the change if it
     is a blocking issue).
3. After addressing all comments, run tests to verify nothing is broken.
4. Commit all changes together with a message summarizing what was addressed.
5. Push the changes.

## Important

- Address ALL comments, not just some.
- If a comment is unclear, make your best interpretation and note it in the commit message.
- Run tests and linters before committing.

Repository: {repo}
PR: #{pr_number}
"""


__all__ = [
    "build_check_fixer_prompt",
    "build_conflict_resolution_prompt",
    "build_feedback_fixer_prompt",
    "build_review_prompt",
    "get_max_retries",
    "get_non_llm_fix_command",
    "load_check_fixers_config",
]
