"""Cross-view alignment of the ``.github/`` block (issue #2532).

The planner prompt reads ``shared/egg_contracts/agent_roles.py`` via
``get_file_patterns()``; the gateway reads ``shared/egg_restrictions/patterns.py``
via ``AgentFilePattern.can_write()``. Earlier work (#2508, #2514, #2521,
#2525) added an explicit ``.github/`` block to several roles in lockstep
across both files. Issue #2532 closes the remaining drift for plan-side
and reviewer roles.

A "load-bearing" test in the style of #2525 (a path the role's allowlist
matches that only the new ``.github/`` block stops) is not constructible
here: every affected role's allowlist is confined to ``.egg-state/...``,
which never collides with ``.github/``. The block is therefore benign
today — but the same forward-compat argument from #2521 applies. These
tests assert the two views agree, so any future allowlist widening
cannot silently bypass the branch-protection invariant.
"""

from __future__ import annotations

import pytest
from egg_contracts.agent_roles import AgentRole, get_file_patterns
from egg_restrictions import get_agent_pattern

# Roles whose ``blocked_write`` / ``blocked_patterns`` must include
# ``.github/`` per #2532. The list is intentionally hand-maintained:
# adding a new role here is a deliberate decision, not an automatic
# consequence of role registration.
ROLES_WITH_GITHUB_BLOCK = [
    # Plan-side (#2532).
    AgentRole.ARCHITECT,
    AgentRole.TASK_PLANNER,
    AgentRole.RISK_ANALYST,
    # Reviewers sharing _REVIEWER_BLOCKED_WRITE / _REVIEWER_BLOCKED (#2532).
    AgentRole.REVIEWER_CODE,
    AgentRole.REVIEWER_CODE_HOLISTIC,
    AgentRole.REVIEWER_AGENT_DESIGN,
    AgentRole.REVIEWER_REFINE,
    AgentRole.REVIEWER_PLAN,
    AgentRole.REVIEWER_SECURITY,
    AgentRole.REVIEWER_CONCURRENCY,
    # Reviewer with its own blocked list (#2532).
    AgentRole.REVIEWER_CONTRACT,
]


@pytest.mark.parametrize("role", ROLES_WITH_GITHUB_BLOCK, ids=lambda r: r.value)
def test_agent_roles_view_blocks_github(role: AgentRole) -> None:
    """``agent_roles.py`` (planner-prompt view) blocks ``.github/``."""
    patterns = get_file_patterns(role.value)
    assert patterns is not None, f"{role.value} has no file_access patterns"
    assert ".github/" in patterns["blocked"], (
        f"{role.value} blocked_write is missing '.github/' — "
        "see issue #2532 for the drift this test guards against."
    )


@pytest.mark.parametrize("role", ROLES_WITH_GITHUB_BLOCK, ids=lambda r: r.value)
def test_patterns_view_blocks_github(role: AgentRole) -> None:
    """``patterns.py`` (gateway view) blocks ``.github/``."""
    pattern = get_agent_pattern(role.value)
    assert pattern is not None, f"{role.value} not registered in AGENT_PATTERNS"
    assert ".github/" in pattern.blocked_patterns, (
        f"{role.value} blocked_patterns is missing '.github/' — "
        "see issue #2532 for the drift this test guards against."
    )


@pytest.mark.parametrize("role", ROLES_WITH_GITHUB_BLOCK, ids=lambda r: r.value)
def test_two_views_agree_on_github(role: AgentRole) -> None:
    """The planner-prompt view and the gateway view must agree on ``.github/``."""
    contracts_patterns = get_file_patterns(role.value)
    restrictions_pattern = get_agent_pattern(role.value)
    assert contracts_patterns is not None
    assert restrictions_pattern is not None
    contracts_blocks_github = ".github/" in contracts_patterns["blocked"]
    restrictions_blocks_github = ".github/" in restrictions_pattern.blocked_patterns
    assert contracts_blocks_github == restrictions_blocks_github, (
        f"{role.value}: agent_roles.py and patterns.py disagree on '.github/' — "
        f"agent_roles={contracts_blocks_github}, patterns={restrictions_blocks_github}. "
        "See issue #2532."
    )
