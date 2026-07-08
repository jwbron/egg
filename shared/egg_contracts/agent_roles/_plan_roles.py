"""Plan- and apply-phase agent role definitions (#3543 decomposition).

Plan producers (architect, task_planner, risk_analyst), the plan-phase
reviewer, and the apply-phase producer (applier, issue #1557) that runs
between plan approval and implement for epic pipelines.
"""

from ._core import (
    _REVIEWER_BLOCKED_WRITE,
    AgentCategory,
    AgentRole,
    AgentRoleDefinition,
    FileAccessPattern,
)

# Plan-phase agent role definitions
# Plan-phase agents (architect, task_planner, risk_analyst) share a
# blocked_write list mirroring ``_PLAN_AGENT_BLOCKED`` in
# ``shared/egg_restrictions/patterns.py``. Keeping the two views in
# lockstep is the invariant issue #2532 closed; using a shared constant
# eliminates one future drift surface within ``agent_roles.py`` itself.
_PLAN_AGENT_BLOCKED_WRITE = [
    "**/*.py",
    "**/*.ts",
    "**/*.tsx",
    "**/*.js",
    "**/*.jsx",
    "**/*.go",
    "**/*.java",
    ".egg-state/contracts/",
    # Issue #2532: parity with _PLAN_AGENT_BLOCKED in patterns.py — see #2508 / #2521.
    ".github/",
]

ARCHITECT_ROLE = AgentRoleDefinition(
    role=AgentRole.ARCHITECT,
    description="Analyzes the task and produces architecture analysis",
    category=AgentCategory.ANALYSIS,
    responsibilities=[
        "Understand the problem or feature request",
        "Research the codebase to understand existing patterns",
        "Identify key files, constraints, and dependencies",
        "Recommend an approach with justification",
        "Document technical decisions",
    ],
    dependencies=[],  # Architect runs first in plan phase
    file_access=FileAccessPattern(
        allowed_read=[],  # Can read all files
        allowed_write=[
            ".egg-state/drafts/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_PLAN_AGENT_BLOCKED_WRITE,
    ),
    produces_outputs=["architecture_analysis", "technical_decisions"],
    requires_inputs=[],
)

TASK_PLANNER_ROLE = AgentRoleDefinition(
    role=AgentRole.TASK_PLANNER,
    description="Decomposes architecture analysis into discrete tasks",
    category=AgentCategory.ANALYSIS,
    responsibilities=[
        "Review the architecture analysis from the architect",
        "Break down work into phases with discrete tasks",
        "Define clear acceptance criteria for each task",
        "Define dependency ordering between tasks",
        "Identify the test strategy",
    ],
    dependencies=[AgentRole.ARCHITECT],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/drafts/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_PLAN_AGENT_BLOCKED_WRITE,
    ),
    produces_outputs=["task_breakdown", "acceptance_criteria"],
    requires_inputs=["architecture_analysis"],
)

RISK_ANALYST_ROLE = AgentRoleDefinition(
    role=AgentRole.RISK_ANALYST,
    description="Assesses technical risks for the proposed implementation",
    category=AgentCategory.ANALYSIS,
    responsibilities=[
        "Review the architecture analysis from the architect",
        "Identify technical risks (security, performance, compatibility)",
        "Assess impact and likelihood of each risk",
        "Propose mitigation strategies and rollback plans",
        "Flag areas that need human review",
    ],
    dependencies=[AgentRole.ARCHITECT],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/drafts/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_PLAN_AGENT_BLOCKED_WRITE,
    ),
    can_run_in_parallel=True,  # Can run in parallel with task_planner
    produces_outputs=["risk_assessment", "mitigation_plan"],
    requires_inputs=["architecture_analysis"],
)


# Jira-epic SDLC support (issue #1557). The APPLIER drives Jira
# mutations after the refine/plan HITL gates resolve. It reads the
# contract + relevant draft and calls the agent-facing gateway Jira
# routes (``ticket/edit``, ``ticket/create``, ``issue-link/create``).
# ``Won't Do`` transitions are **not** in the applier's purview — the
# applier produces a handoff JSON that the orchestrator drains via the
# orchestrator-only ``/transition`` route. Restricted to write only the
# agent-outputs handoff directory; the applier never edits source.
APPLIER_ROLE = AgentRoleDefinition(
    role=AgentRole.APPLIER,
    description=(
        "Applies Jira mutations (epic Description writes, child "
        "create/edit/link, Won't-Do handoff) on operator approval of "
        "refine/plan HITL gates for epic-mode pipelines."
    ),
    category=AgentCategory.EXECUTION,
    responsibilities=[
        "Read EGG_EPIC_MODE + the just-approved phase + contract path",
        "For refine-apply: write the analysis to the epic Description",
        "For plan-apply: walk Task.jira_action and dispatch per-action",
        "Write jira_action_status='in_flight' before each call; flip to "
        "'applied' or 'failed' after",
        "Emit a Won't-Do handoff JSON for the orchestrator to drain",
        "Refuse to mutate in-flight children without the override marker",
    ],
    dependencies=[],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/agent-outputs/",
        ],
        blocked_write=[
            "src/",
            "lib/",
            "shared/",
            "gateway/",
            "sandbox/",
            "action/",
            "orchestrator/",
            "plugins/",
            "docs/",
            "tests/",
            "test/",
            ".egg-state/contracts/",
            ".egg-state/drafts/",
            ".github/",
        ],
    ),
    can_run_in_parallel=False,
    produces_outputs=["jira_apply_report", "wontdo_handoff"],
    requires_inputs=["analysis_draft", "task_breakdown"],
)


REVIEWER_PLAN_ROLE = AgentRoleDefinition(
    role=AgentRole.REVIEWER_PLAN,
    description="Reviews plan phase output quality and completeness",
    category=AgentCategory.REVIEW,
    responsibilities=[
        "Verify task breakdown is discrete, actionable, and properly scoped",
        "Assess acceptance criteria clarity and testability",
        "Evaluate dependency ordering between tasks",
        "Check that risks and mitigations are identified",
        "Validate test strategy coverage",
    ],
    dependencies=[AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST],
    file_access=FileAccessPattern(
        allowed_read=[],
        allowed_write=[
            ".egg-state/reviews/",
            ".egg-state/agent-outputs/",
        ],
        blocked_write=_REVIEWER_BLOCKED_WRITE,
    ),
    produces_outputs=["review_verdict"],
    requires_inputs=["task_breakdown", "risk_assessment"],
)
