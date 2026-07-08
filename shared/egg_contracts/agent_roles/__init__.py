"""
Agent role definitions for multi-agent orchestration.

This module is the **canonical source of truth** for agent roles and their
definitions.  All other modules (``orchestrator/models.py``,
``shared/egg_orchestrator/types.py``, ``gateway/agent_restrictions.py``)
must import or mirror the ``AgentRole`` enum defined here.

Agent role categories:
- EXECUTION: Agents that produce code or artifacts (coder, tester, documenter)
- ANALYSIS: Agents that analyze and plan (architect, task_planner, risk_analyst, refiner)
- REVIEW: Agents that review work products (reviewer_*)
- UTILITY: Cross-cutting support agents (autofixer, conflict_resolver)
- INTERFACE: Agents that interact with external systems (overseer)

The orchestrator uses these definitions to:
1. Determine execution order based on dependencies
2. Enforce file access restrictions via the gateway
3. Build role-specific prompts with focused context
4. Compose dynamic agent teams via category queries

Decomposition note (#3543): the pre-split single-file module is now a
sub-package following the #3312 pattern (see ``plan_parser``). This
``__init__.py`` is the **stable public API barrel**; every symbol that was
importable as a module global on the single-file module re-exports here,
so ``from egg_contracts.agent_roles import X`` keeps resolving unchanged.
The implementation lives in underscore-prefixed private submodules:

- ``_core``            -- ``AgentCategory`` / ``AgentRole`` / ``AgentStatus``
  enums, ``FileAccessPattern`` / ``AgentRoleDefinition`` / ``AgentExecution``
  dataclasses, and the shared reviewer blocked-write list
- ``_refine_roles``    -- refine-phase producers and reviewers
- ``_plan_roles``      -- plan-phase producers and reviewer, plus the
  apply-phase applier (#1557)
- ``_implement_roles`` -- implement-phase producers and reviewers
- ``_oversight_roles`` -- overseer plus on-demand utility roles
- ``_registry``        -- ``AGENT_ROLES`` registry, phase maps, contract-role
  mapping, and query helpers

Pure refactor, no behaviour change: every re-exported definition is
AST-identical to the pre-split module (the only edit is the relative-import
depth of ``dependency_graph`` inside ``detect_write_overlaps``).
"""

from egg_restrictions.matchers import match_pattern

from ..roles import Role
from ._core import (
    _REVIEWER_BLOCKED_WRITE,
    AgentCategory,
    AgentExecution,
    AgentRole,
    AgentRoleDefinition,
    AgentStatus,
    FileAccessPattern,
)
from ._implement_roles import (
    _REVIEWER_CONTRACT_BLOCKED_WRITE,
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
    _PLAN_AGENT_BLOCKED_WRITE,
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
from ._registry import (
    _PHASE_REVIEWERS,
    _PHASE_ROLES,
    AGENT_ROLE_TO_CONTRACT_ROLE,
    AGENT_ROLES,
    CONTRACT_ENFORCER_ROLE_NAMES,
    CONTRACT_ENFORCER_ROLES,
    EGG_ONLY_REVIEWER_NAMES,
    EGG_ONLY_REVIEWERS,
    EGG_REPO,
    EXECUTION_ROLE_VALUES,
    MODEL_OVERRIDE_ROLES,
    REVIEWER_CHECKOUT_ROLE_VALUES,
    can_run_in_parallel,
    create_execution_for_role,
    detect_write_overlaps,
    get_all_roles,
    get_contract_role,
    get_file_patterns,
    get_role_definition,
    get_role_dependencies,
    get_roles_by_category,
    get_roles_for_phase,
)

# ``__all__`` lists the full re-export surface of the barrel. The pre-split
# module had no ``__all__``; the entries below are the symbols that were
# importable as module globals on the single-file module (including the
# underscore-prefixed pattern lists and phase maps, plus ``Role`` and
# ``match_pattern``, which external code and tests import by name from this
# module path). No consumer used ``from egg_contracts.agent_roles import *``.
__all__ = (
    # Core types
    "AgentCategory",
    "AgentExecution",
    "AgentRole",
    "AgentRoleDefinition",
    "AgentStatus",
    "FileAccessPattern",
    # Refine-phase role definitions
    "FIRST_PRINCIPLES_REVIEWER_ROLE",
    "REFINER_ROLE",
    "REVIEWER_AGENT_DESIGN_ROLE",
    "REVIEWER_REFINE_ROLE",
    "SIMPLIFIER_ROLE",
    # Plan/apply-phase role definitions
    "APPLIER_ROLE",
    "ARCHITECT_ROLE",
    "REVIEWER_PLAN_ROLE",
    "RISK_ANALYST_ROLE",
    "TASK_PLANNER_ROLE",
    # Implement-phase role definitions
    "CODER_ROLE",
    "DOCUMENTER_ROLE",
    "REVIEWER_CODE_HOLISTIC_ROLE",
    "REVIEWER_CODE_ROLE",
    "REVIEWER_CONCURRENCY_ROLE",
    "REVIEWER_CONTRACT_ROLE",
    "REVIEWER_SECURITY_ROLE",
    "TESTER_ROLE",
    # Oversight / utility role definitions
    "AUTOFIXER_ROLE",
    "CONFLICT_RESOLVER_ROLE",
    "EVIDENCE_GATHERER_ROLE",
    "OVERSEER_ROLE",
    # Registry, phase maps, and role sets
    "AGENT_ROLES",
    "AGENT_ROLE_TO_CONTRACT_ROLE",
    "CONTRACT_ENFORCER_ROLES",
    "CONTRACT_ENFORCER_ROLE_NAMES",
    "EGG_ONLY_REVIEWERS",
    "EGG_ONLY_REVIEWER_NAMES",
    "EGG_REPO",
    "EXECUTION_ROLE_VALUES",
    "MODEL_OVERRIDE_ROLES",
    "REVIEWER_CHECKOUT_ROLE_VALUES",
    # Query helpers
    "can_run_in_parallel",
    "create_execution_for_role",
    "detect_write_overlaps",
    "get_all_roles",
    "get_contract_role",
    "get_file_patterns",
    "get_role_definition",
    "get_role_dependencies",
    "get_roles_by_category",
    "get_roles_for_phase",
    # Module globals retained from the single-file module
    "Role",
    "match_pattern",
    "_PHASE_REVIEWERS",
    "_PHASE_ROLES",
    "_PLAN_AGENT_BLOCKED_WRITE",
    "_REVIEWER_BLOCKED_WRITE",
    "_REVIEWER_CONTRACT_BLOCKED_WRITE",
)
