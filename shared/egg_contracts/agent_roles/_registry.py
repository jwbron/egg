"""Role registry, phase maps, and lookup helpers (#3543 decomposition).

The ``AGENT_ROLES`` registry, the fine-to-coarse ``Role`` mapping, the
phase-to-role maps, and every query helper (``get_role_definition``,
``get_roles_for_phase``, ``detect_write_overlaps``, ...).
"""

from ..roles import Role
from ._core import (
    AgentCategory,
    AgentExecution,
    AgentRole,
    AgentRoleDefinition,
)
from ._implement_roles import (
    CODER_ROLE,
    DOCUMENTER_ROLE,
    REVIEWER_CODE_HOLISTIC_ROLE,
    REVIEWER_CODE_ROLE,
    REVIEWER_CONCURRENCY_ROLE,
    REVIEWER_CONTRACT_ROLE,
    REVIEWER_SECURITY_ROLE,
    TESTER_ROLE,
)
from ._oversight_roles import (
    AUTOFIXER_ROLE,
    CONFLICT_RESOLVER_ROLE,
    EVIDENCE_GATHERER_ROLE,
    OVERSEER_ROLE,
)
from ._plan_roles import (
    APPLIER_ROLE,
    ARCHITECT_ROLE,
    REVIEWER_PLAN_ROLE,
    RISK_ANALYST_ROLE,
    TASK_PLANNER_ROLE,
)
from ._refine_roles import (
    FIRST_PRINCIPLES_REVIEWER_ROLE,
    REFINER_ROLE,
    REVIEWER_AGENT_DESIGN_ROLE,
    REVIEWER_REFINE_ROLE,
    SIMPLIFIER_ROLE,
)

# Registry of all agent roles
AGENT_ROLES: dict[AgentRole, AgentRoleDefinition] = {
    # Execution roles
    AgentRole.CODER: CODER_ROLE,
    AgentRole.TESTER: TESTER_ROLE,
    AgentRole.DOCUMENTER: DOCUMENTER_ROLE,
    AgentRole.APPLIER: APPLIER_ROLE,
    # Analysis roles
    AgentRole.ARCHITECT: ARCHITECT_ROLE,
    AgentRole.TASK_PLANNER: TASK_PLANNER_ROLE,
    AgentRole.RISK_ANALYST: RISK_ANALYST_ROLE,
    AgentRole.REFINER: REFINER_ROLE,
    AgentRole.SIMPLIFIER: SIMPLIFIER_ROLE,
    # Review roles
    AgentRole.REVIEWER_CODE: REVIEWER_CODE_ROLE,
    AgentRole.REVIEWER_CODE_HOLISTIC: REVIEWER_CODE_HOLISTIC_ROLE,
    AgentRole.REVIEWER_CONTRACT: REVIEWER_CONTRACT_ROLE,
    AgentRole.REVIEWER_AGENT_DESIGN: REVIEWER_AGENT_DESIGN_ROLE,
    AgentRole.REVIEWER_REFINE: REVIEWER_REFINE_ROLE,
    AgentRole.FIRST_PRINCIPLES_REVIEWER: FIRST_PRINCIPLES_REVIEWER_ROLE,
    AgentRole.REVIEWER_PLAN: REVIEWER_PLAN_ROLE,
    AgentRole.REVIEWER_SECURITY: REVIEWER_SECURITY_ROLE,
    AgentRole.REVIEWER_CONCURRENCY: REVIEWER_CONCURRENCY_ROLE,
    # Utility roles
    AgentRole.AUTOFIXER: AUTOFIXER_ROLE,
    AgentRole.CONFLICT_RESOLVER: CONFLICT_RESOLVER_ROLE,
    AgentRole.EVIDENCE_GATHERER: EVIDENCE_GATHERER_ROLE,
    # Interface roles
    AgentRole.OVERSEER: OVERSEER_ROLE,
}


# Canonical set of execution role string values — used by the schema,
# plan parser, and orchestrator for role validation and filtering.
EXECUTION_ROLE_VALUES = frozenset({AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER})


# Reviewer roles that must have the peer's proposal *merged into their
# working tree* to do their job — i.e. they EXECUTE the proposal (run
# the test suite / build against the merged tree) rather than only
# reading its diff. The BRC event-pump wrapper's ``sync_to_proposals``
# gate (#3216, WS1 of #3209) runs ``git merge`` only for these roles on
# a review (``ack``/``nack``) arm; every other reviewer reads peer
# artifacts via the per-event-prompt ``git show`` / ``egg-artifact``
# served reads, so merging the peer's whole tree into their worktree
# only risks the dual-role criss-cross propagation that corrupts shared
# drafts (#3208). The ``tester`` is the only reviewer that runs the
# proposed tree; reviewer_code and the refine/plan reviewers read diffs.
#
# Residual: the ``tester`` is itself a dual-role agent (producer of test
# code + reviewer) and must keep merging to execute ``make test`` against
# the proposed tree, so the #3208 merge-then-commit-on-top criss-cross
# shape stays *reachable via tester* in the implement phase. This gate
# therefore does NOT fully close #3208 — it removes the shape for every
# read-only reviewer (the plan-phase ``risk_analyst`` / ``plan.md``
# corruption that motivated #3216). The remaining tester surface is an
# accepted residual of WS1·1a, tracked under the parent #3209 (which
# supersedes #3208); do not assume #3208 is closed by this set alone.
REVIEWER_CHECKOUT_ROLE_VALUES = frozenset({AgentRole.TESTER})


# Maps the fine-grained ``AgentRole`` shipped in agent sessions to the
# coarse-grained ``Role`` enum used for contract field-ownership checks.
# The gateway's contract and phase APIs consult this to authorize an
# agent's request — without it, a session with ``agent_role="refiner"``
# fails ``Role("refiner")`` and the API responds 403 (#1766).
AGENT_ROLE_TO_CONTRACT_ROLE: dict[AgentRole, Role] = {
    # Execution: produce code, own implementer-owned contract fields
    AgentRole.CODER: Role.IMPLEMENTER,
    AgentRole.TESTER: Role.IMPLEMENTER,
    AgentRole.DOCUMENTER: Role.IMPLEMENTER,
    # Applier (issue #1557): mutates Task.jira_* lifecycle fields on the
    # contract during the apply phase; same contract privileges as other
    # execution producers.
    AgentRole.APPLIER: Role.IMPLEMENTER,
    # Analysis: draft plans and analyses; write the same contract fields
    # an implementer does (commits, notes, decisions).
    AgentRole.ARCHITECT: Role.IMPLEMENTER,
    AgentRole.TASK_PLANNER: Role.IMPLEMENTER,
    AgentRole.RISK_ANALYST: Role.IMPLEMENTER,
    AgentRole.REFINER: Role.IMPLEMENTER,
    # Simplifier: produces the human-focused companion draft; writes the
    # same draft/agent-output contract fields as the other analysis
    # producers.
    AgentRole.SIMPLIFIER: Role.IMPLEMENTER,
    # Review: verdicts and phase-status/current_phase mutations
    AgentRole.REVIEWER_CODE: Role.REVIEWER,
    AgentRole.REVIEWER_CODE_HOLISTIC: Role.REVIEWER,
    AgentRole.REVIEWER_CONTRACT: Role.REVIEWER,
    AgentRole.REVIEWER_AGENT_DESIGN: Role.REVIEWER,
    AgentRole.REVIEWER_REFINE: Role.REVIEWER,
    # Pure reviewer; gains decision-create via the broadened decisions.*
    # ownership in roles.py (it surfaces seed redirects as HITL decisions).
    AgentRole.FIRST_PRINCIPLES_REVIEWER: Role.REVIEWER,
    AgentRole.REVIEWER_PLAN: Role.REVIEWER,
    AgentRole.REVIEWER_SECURITY: Role.REVIEWER,
    AgentRole.REVIEWER_CONCURRENCY: Role.REVIEWER,
    # Utility: apply code fixes, share implementer privileges
    AgentRole.AUTOFIXER: Role.IMPLEMENTER,
    AgentRole.CONFLICT_RESOLVER: Role.IMPLEMENTER,
    # Read-only evidence gatherer: an observer like the overseer, never a
    # contract author — SYSTEM structurally denies it verdict/contract writes.
    AgentRole.EVIDENCE_GATHERER: Role.SYSTEM,
    # Interface: observers, not contract authors
    AgentRole.OVERSEER: Role.SYSTEM,
}


def get_contract_role(role: AgentRole | str) -> Role | None:
    """Translate a fine-grained ``AgentRole`` to the coarse contract ``Role``.

    The gateway stores the fine role in session metadata but the contract
    API enforces field ownership against the coarse ``Role`` enum. Returns
    ``None`` when the input does not name a known fine role.
    """
    if isinstance(role, str):
        try:
            role = AgentRole(role)
        except ValueError:
            return None
    return AGENT_ROLE_TO_CONTRACT_ROLE.get(role)


def get_role_definition(
    role: AgentRole | str,
) -> AgentRoleDefinition:
    """Get the definition for an agent role.

    Args:
        role: The role to get (string or AgentRole enum)

    Returns:
        The AgentRoleDefinition for this role

    Raises:
        KeyError: If the role is not defined
    """
    if isinstance(role, str):
        role = AgentRole(role)
    return AGENT_ROLES[role]


def get_all_roles() -> list[AgentRoleDefinition]:
    """Get all defined agent roles.

    Returns:
        List of all AgentRoleDefinition objects
    """
    return list(AGENT_ROLES.values())


def get_file_patterns(role_value: str) -> dict[str, list[str]] | None:
    """Return file write patterns for an agent role, or None if not defined.

    Returns:
        ``{"allowed": [...], "blocked": [...]}`` or ``None`` if the role is
        unknown or has no file access patterns.
    """
    try:
        role_def = get_role_definition(role_value)
    except ValueError, KeyError:
        return None
    if not role_def or not role_def.file_access:
        return None
    fa = role_def.file_access
    if not fa.allowed_write and not fa.blocked_write:
        return None
    return {"allowed": fa.allowed_write, "blocked": fa.blocked_write}


def get_roles_by_category(category: AgentCategory) -> list[AgentRole]:
    """Get all roles belonging to a given category.

    Args:
        category: The category to filter by

    Returns:
        List of AgentRole values with the given category
    """
    return [role for role, defn in AGENT_ROLES.items() if defn.category == category]


def get_role_dependencies(role: AgentRole | str) -> list[AgentRole]:
    """Get the dependencies for a role.

    Args:
        role: The role to get dependencies for

    Returns:
        List of roles this role depends on
    """
    definition = get_role_definition(role)
    return definition.dependencies


def can_run_in_parallel(role1: AgentRole | str, role2: AgentRole | str) -> bool:
    """Check if two roles can run in parallel.

    Two roles can run in parallel if:
    1. Neither depends on the other
    2. Both have can_run_in_parallel set to True

    Args:
        role1: First role
        role2: Second role

    Returns:
        True if the roles can run concurrently
    """
    def1 = get_role_definition(role1)
    def2 = get_role_definition(role2)

    # Check mutual dependencies
    if def1.depends_on(def2.role) or def2.depends_on(def1.role):
        return False

    # Both must allow parallel execution
    return def1.can_run_in_parallel and def2.can_run_in_parallel


# Phase-to-role mappings for multi-agent execution
# Note: Utility roles (AUTOFIXER, CONFLICT_RESOLVER, EVIDENCE_GATHERER) are
# excluded by design — they are spawned on-demand, not as part of standard
# phase execution. EVIDENCE_GATHERER in particular runs ahead of a review wave
# as a read-only pass (see EVIDENCE_GATHERER_ROLE in _oversight_roles.py).

_PHASE_ROLES: dict[str, list[AgentRole]] = {
    "implement": [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER],
    "plan": [
        AgentRole.ARCHITECT,
        AgentRole.TASK_PLANNER,
        AgentRole.RISK_ANALYST,
        AgentRole.SIMPLIFIER,
    ],
    "refine": [AgentRole.REFINER, AgentRole.SIMPLIFIER],
    # Apply phase (issue #1557): single producer (APPLIER) reviewed by
    # REVIEWER_CONTRACT on contract-state convergence. Inserted between
    # PLAN and IMPLEMENT only for epic pipelines — the orchestrator
    # scheduler skips this phase when ``Pipeline.is_epic == False``.
    "apply": [AgentRole.APPLIER],
}

_PHASE_REVIEWERS: dict[str, list[AgentRole]] = {
    "implement": [
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CODE_HOLISTIC,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_SECURITY,
        AgentRole.REVIEWER_CONCURRENCY,
    ],
    "plan": [
        AgentRole.REVIEWER_PLAN,
    ],
    "refine": [
        AgentRole.REVIEWER_REFINE,
        AgentRole.REVIEWER_AGENT_DESIGN,
        AgentRole.FIRST_PRINCIPLES_REVIEWER,
    ],
    # Apply phase reviewer (issue #1557 — architect's slice-3 design +
    # risk_analyst R1 mitigation). REVIEWER_CONTRACT ACKs on
    # contract-state convergence (every Task with jira_action='create'
    # has a non-null jira_key matching ^[A-Z][A-Z0-9_]*-[0-9]+$, every
    # Task has jira_action_status in {'applied', 'failed'}, no in-flight
    # child mutated without the 'in-flight-confirmed' marker). The
    # reviewer ACKs on contract state, NOT on prompt-output text
    # quality.
    "apply": [
        AgentRole.REVIEWER_CONTRACT,
    ],
}


# Roles whose per-pipeline ``PipelineConfig.agent_models`` override is
# actually honored. ``orchestrator.agent_model_resolution.resolve_agent_model``
# is consulted only by the concurrent-executor spawn path and the
# ``restart_agent`` route, which between them spawn every phase producer
# and reviewer in the two maps above. Utility roles (AUTOFIXER,
# CONFLICT_RESOLVER) spawn through dedicated paths that never call the
# resolver. The interface role (OVERSEER) now resolves its base model
# through ``resolve_overseer_model`` -> ``resolve_agent_model`` (#2270 §1),
# but is not yet promoted into ``MODEL_OVERRIDE_ROLES``. Either way an
# ``agent_models`` entry naming one of these roles is not honored today, so
# ``PipelineConfig``'s validator rejects such keys up front. See #2769.
MODEL_OVERRIDE_ROLES: frozenset[AgentRole] = frozenset(
    role
    for role_group in (*_PHASE_ROLES.values(), *_PHASE_REVIEWERS.values())
    for role in role_group
)


EGG_REPO = "jwbron/egg"

# Reviewer roles that only apply to the egg repo itself
EGG_ONLY_REVIEWERS: set[AgentRole] = {AgentRole.REVIEWER_AGENT_DESIGN}

# String values for use by review_graph and other modules
EGG_ONLY_REVIEWER_NAMES: set[str] = {r.value for r in EGG_ONLY_REVIEWERS}

# Reviewer roles that enforce contract-task completeness at consensus
# time (#3114). The orchestrator rejects an enforcer's ACK of a producer
# whose contract task rows in the active slice are not ``complete``, and
# rejects its CONFIRM while any slice row is incomplete — making the
# contract reviewer the structural gate that holds a slice's consensus
# open until the contract is actually delivered. Capability set rather
# than a hardcoded role string so a future enforcer role inherits the
# gate without orchestrator changes.
CONTRACT_ENFORCER_ROLES: frozenset[AgentRole] = frozenset({AgentRole.REVIEWER_CONTRACT})

# String values for use by the orchestrator signal routes.
CONTRACT_ENFORCER_ROLE_NAMES: frozenset[str] = frozenset(r.value for r in CONTRACT_ENFORCER_ROLES)


def get_roles_for_phase(
    phase: str,
    include_reviewers: bool = True,
    include_overseer: bool = False,
    repo: str | None = None,
    has_contract: bool = True,
) -> list[AgentRole]:
    """Return the agent roles for a given pipeline phase.

    Args:
        phase: Pipeline phase name (e.g., "implement", "plan")
        include_reviewers: Whether to include reviewer roles (default True)
        include_overseer: Whether to include the overseer role (default False).
            The overseer is cross-phase, so it's opt-in.
        repo: Repository in owner/name format. When provided, egg-specific
            reviewer roles (e.g., reviewer_agent_design) are excluded for
            non-egg repos.
        has_contract: Whether the pipeline has an upstream SDLC contract
            (default True). When False, reviewers whose upstream artifacts
            are absent are filtered out — currently ``reviewer_contract``.
            ISSUE-mode pipelines set this to False when they haven't yet
            produced a contract draft.

    Returns:
        List of AgentRole values for that phase.

    Raises:
        ValueError: If phase has no defined roles.
    """
    roles = _PHASE_ROLES.get(phase)
    if roles is None:
        raise ValueError(f"No agent roles defined for phase: {phase}")
    result = list(roles)
    if include_reviewers:
        reviewers = _PHASE_REVIEWERS.get(phase, [])
        if repo is not None and repo != EGG_REPO:
            reviewers = [r for r in reviewers if r not in EGG_ONLY_REVIEWERS]
        if not has_contract:
            # reviewer_contract has no artifacts to verify without a contract;
            # filter it out so BRC doesn't wait on an agent that cannot ACK.
            reviewers = [r for r in reviewers if r != AgentRole.REVIEWER_CONTRACT]
        result.extend(reviewers)
    if include_overseer:
        result.append(AgentRole.OVERSEER)
    return result


def detect_write_overlaps(
    roles: list[AgentRole],
) -> list[tuple[AgentRole, AgentRole, list[str]]]:
    """Detect overlapping write patterns between agents that may run in parallel.

    Only checks roles that can run in the same wave (share no dependency edge).

    Returns:
        List of (role1, role2, overlapping_patterns) tuples.
    """
    from ..dependency_graph import build_dependency_graph

    graph = build_dependency_graph(roles)
    waves = graph.compute_waves()

    overlaps = []
    for wave in waves:
        if len(wave) < 2:
            continue
        for i, role1 in enumerate(wave):
            for role2 in wave[i + 1 :]:
                role1_def = get_role_definition(role1)
                role2_def = get_role_definition(role2)
                # Find common write patterns
                common = []
                for p1 in role1_def.file_access.allowed_write:
                    for p2 in role2_def.file_access.allowed_write:
                        if p1 == p2:
                            common.append(p1)
                        elif p1.endswith("/") and p2.startswith(p1):
                            common.append(p1)
                        elif p2.endswith("/") and p1.startswith(p2):
                            common.append(p2)
                if common:
                    overlaps.append((role1, role2, common))
    return overlaps


def create_execution_for_role(role: AgentRole | str) -> AgentExecution:
    """Create a new AgentExecution for a role.

    Args:
        role: The role to create execution tracking for

    Returns:
        A new AgentExecution in PENDING status
    """
    if isinstance(role, str):
        role = AgentRole(role)
    return AgentExecution(role=role)
