"""Babysit-PR loop: automated PR lifecycle management.

The babysit-pr loop monitors a GitHub pull request through its entire
lifecycle, automatically handling:

- **Merge conflict resolution**: Detects conflicts and spawns an agent
  to resolve them.
- **CI check monitoring**: Polls CI status and waits for all checks
  to complete.
- **CI failure fixing**: Attempts non-LLM fixes (auto-formatters) first,
  then spawns an LLM agent for complex failures.
- **Code review**: Spawns a reviewer agent to post a GitHub review.
- **Feedback addressing**: Spawns a fixer agent to address review comments.
- **HITL escalation**: Escalates to a human when the loop cannot make
  progress (max retries, complex conflicts, etc.).

Usage::

    from egg_babysit import babysit, BabysitConfig

    config = BabysitConfig(pr_number=42, repo="owner/repo")
    result = babysit(config)
    print(result.exit_reason)

Or from the command line::

    python -m egg_babysit 42 --repo owner/repo
"""

from .config import BabysitConfig
from .loop import BabysitLoop, babysit
from .types import (
    BabysitExitReason,
    BabysitResult,
    BabysitStep,
    CICheckResult,
    CICheckStatus,
    LoopState,
    PRState,
    ReviewVerdict,
)

__all__ = [
    "BabysitConfig",
    "BabysitExitReason",
    "BabysitLoop",
    "BabysitResult",
    "BabysitStep",
    "CICheckResult",
    "CICheckStatus",
    "LoopState",
    "PRState",
    "ReviewVerdict",
    "babysit",
]
