"""
Egg Contracts Library.

This module provides the core functionality for managing SDLC contracts
that enforce structurally-verified agent checkpoints and verification gates.

Key concepts:
- Contract: JSON document tracking issue progress through SDLC phases
- Roles: Implementer, Reviewer, Human - each with specific permissions
- Mutations: All contract changes are validated against role permissions
- Audit Log: All modifications are tracked for accountability

Usage:
    from egg_contracts import Contract, Role, load_contract, save_contract
    from egg_contracts import validate_mutation, apply_mutation

    # Load a contract
    contract = load_contract(issue_number=133, repo_root=Path("/path/to/repo"))

    # Validate a mutation
    result = validate_mutation(
        role=Role.IMPLEMENTER,
        field_path="phases.0.tasks.0.commit",
        new_value="abc1234",
    )

    # Apply a mutation
    result = apply_mutation(
        contract=contract,
        role=Role.IMPLEMENTER,
        actor="egg",
        field_path="phases.0.tasks.0.commit",
        new_value="abc1234",
    )
"""

from .agent_recovery import (
    AgentCircuitBreaker,
    AgentRetryConfig,
    AgentRetryManager,
    CircuitBreakerConfig,
    CircuitState,
    ConflictDetector,
    ConflictInfo,
    RetryDecision,
    RetryPolicy,
    create_circuit_breaker,
    create_retry_manager,
)
from .agent_roles import (
    AGENT_ROLES,
    CODER_ROLE,
    DOCUMENTER_ROLE,
    INTEGRATOR_ROLE,
    TESTER_ROLE,
    AgentExecution,
    AgentRole,
    AgentRoleDefinition,
    AgentStatus,
    FileAccessPattern,
    can_run_in_parallel,
    create_execution_for_role,
    get_all_roles,
    get_role_definition,
    get_role_dependencies,
)
from .audit import (
    create_audit_entry,
    create_transition_entry,
    create_update_entry,
    format_audit_log,
)
from .dependency_graph import (
    DependencyGraph,
    DependencyNode,
    ExecutionPlan,
    ExecutionWave,
    build_dependency_graph,
    compute_execution_plan,
    format_execution_plan,
    get_parallel_groups,
)
from .feedback import (
    FeedbackQuestionInput,
    ParsedFeedbackResponse,
    calculate_feedback_debounce_remaining,
    generate_feedback_comment,
    generate_feedback_id,
    parse_feedback_comment,
    should_process_feedback,
    start_feedback_debounce,
    update_feedback_with_countdown,
)
from .hitl import (
    DEFAULT_DEBOUNCE_SECONDS,
    HitlCheckboxState,
    HitlDecisionCategory,
    HitlOption,
    HitlOptionId,
    calculate_debounce_remaining,
    generate_checkbox_block,
    generate_debounce_notice,
    generate_full_hitl_block,
    parse_checkbox_state,
    should_process_decision,
    start_debounce,
    update_comment_with_countdown,
)
from .loader import (
    ContractNotFoundError,
    ContractValidationError,
    contract_exists,
    create_contract,
    delete_contract,
    export_contract,
    get_contract_path,
    list_contracts,
    load_contract,
    load_contract_from_branch,
    save_contract,
)
from .models import (
    AcceptanceCriterion,
    AgentExecutionModel,
    AgentExecutionStatus,
    AgentRoleType,
    AuditAction,
    AuditEntry,
    AuditRole,
    CheckDefinition,
    CheckResult,
    CheckStatus,
    Contract,
    Decision,
    DecisionOption,
    DecisionType,
    Feedback,
    FeedbackQuestion,
    HumanReviewMechanism,
    IssueInfo,
    MultiAgentConfig,
    Phase,
    PhaseConfig,
    PhaseStatus,
    PipelinePhase,
    PRMetadata,
    ReviewFeedback,
    Task,
    TaskStatus,
)
from .orchestration import (
    AgentHandoff,
    OrchestrationState,
    can_agent_run,
    get_next_wave,
    get_runnable_agents,
    initialize_orchestration,
    update_contract_orchestration,
)
from .orchestrator import (
    AgentResult,
    DispatchDecision,
    Orchestrator,
    collect_handoff_data,
    create_orchestrator,
    format_dispatch_for_workflow,
    get_dispatch_for_contract,
    load_agent_output,
    save_agent_output,
)
from .phase_defaults import (
    get_default_phase_config,
    get_effective_phase_config,
)
from .plan_parser import (
    ParsedPhase,
    ParsedTask,
    ParseResult,
    ParseWarning,
    format_warnings_for_comment,
    parse_plan,
    parse_plan_file,
)
from .resilience import (
    CheckpointState,
    RateLimitHandler,
    RateLimitInfo,
    RetryableError,
    RetryConfig,
    TimeoutCheckpoint,
    calculate_backoff_delay,
    create_timeout_checkpoint,
    parse_rate_limit_headers,
    retry_with_backoff,
    should_checkpoint_now,
)
from .roles import (
    FIELD_OWNERSHIP,
    Role,
    can_modify,
    get_field_owner,
    get_role_permissions,
    normalize_path,
)
from .validator import (
    MutationResult,
    ValidationResult,
    apply_mutation,
    validate_mutation,
    validate_phase_mutation,
    validate_task_mutation,
)

__all__ = [
    # Models
    "AcceptanceCriterion",
    "AgentExecutionModel",
    "AgentExecutionStatus",
    "AgentRoleType",
    "AuditAction",
    "AuditEntry",
    "AuditRole",
    "CheckDefinition",
    "CheckResult",
    "CheckStatus",
    "Contract",
    "ContractNotFoundError",
    "ContractValidationError",
    "Decision",
    "DecisionOption",
    "DecisionType",
    "HumanReviewMechanism",
    "MultiAgentConfig",
    "PhaseConfig",
    # Roles
    "FIELD_OWNERSHIP",
    "IssueInfo",
    "MutationResult",
    "Phase",
    "PhaseStatus",
    "PipelinePhase",
    "ReviewFeedback",
    "Role",
    "Task",
    "TaskStatus",
    "ValidationResult",
    "apply_mutation",
    "can_modify",
    "contract_exists",
    # Loader
    "create_contract",
    "create_audit_entry",
    "create_transition_entry",
    # Audit
    "create_update_entry",
    "delete_contract",
    "export_contract",
    "format_audit_log",
    "get_contract_path",
    "get_field_owner",
    "get_role_permissions",
    "list_contracts",
    "load_contract",
    "load_contract_from_branch",
    "normalize_path",
    "save_contract",
    "validate_mutation",
    "validate_phase_mutation",
    # Validator
    "validate_task_mutation",
    # Plan Parser
    "ParsedPhase",
    "ParsedTask",
    "ParseResult",
    "ParseWarning",
    "format_warnings_for_comment",
    "parse_plan",
    "parse_plan_file",
    # Phase Defaults
    "get_default_phase_config",
    "get_effective_phase_config",
    # HITL
    "DEFAULT_DEBOUNCE_SECONDS",
    "HitlCheckboxState",
    "HitlDecisionCategory",
    "HitlOption",
    "HitlOptionId",
    "calculate_debounce_remaining",
    "generate_checkbox_block",
    "generate_debounce_notice",
    "generate_full_hitl_block",
    "parse_checkbox_state",
    "should_process_decision",
    "start_debounce",
    "update_comment_with_countdown",
    # Feedback
    "Feedback",
    "FeedbackQuestion",
    "FeedbackQuestionInput",
    "ParsedFeedbackResponse",
    "PRMetadata",
    "calculate_feedback_debounce_remaining",
    "generate_feedback_comment",
    "generate_feedback_id",
    "parse_feedback_comment",
    "should_process_feedback",
    "start_feedback_debounce",
    "update_feedback_with_countdown",
    # Resilience
    "CheckpointState",
    "RateLimitHandler",
    "RateLimitInfo",
    "RetryableError",
    "RetryConfig",
    "TimeoutCheckpoint",
    "calculate_backoff_delay",
    "create_timeout_checkpoint",
    "parse_rate_limit_headers",
    "retry_with_backoff",
    "should_checkpoint_now",
    # Agent Roles
    "AGENT_ROLES",
    "AgentExecution",
    "AgentRole",
    "AgentRoleDefinition",
    "AgentStatus",
    "CODER_ROLE",
    "DOCUMENTER_ROLE",
    "FileAccessPattern",
    "INTEGRATOR_ROLE",
    "TESTER_ROLE",
    "can_run_in_parallel",
    "create_execution_for_role",
    "get_all_roles",
    "get_role_definition",
    "get_role_dependencies",
    # Orchestration
    "AgentHandoff",
    "OrchestrationState",
    "can_agent_run",
    "get_next_wave",
    "get_runnable_agents",
    "initialize_orchestration",
    "update_contract_orchestration",
    # Dependency Graph
    "DependencyGraph",
    "DependencyNode",
    "ExecutionPlan",
    "ExecutionWave",
    "build_dependency_graph",
    "compute_execution_plan",
    "format_execution_plan",
    "get_parallel_groups",
    # Orchestrator
    "AgentResult",
    "DispatchDecision",
    "Orchestrator",
    "collect_handoff_data",
    "create_orchestrator",
    "format_dispatch_for_workflow",
    "get_dispatch_for_contract",
    "load_agent_output",
    "save_agent_output",
    # Agent Recovery
    "AgentCircuitBreaker",
    "AgentRetryConfig",
    "AgentRetryManager",
    "CircuitBreakerConfig",
    "CircuitState",
    "ConflictDetector",
    "ConflictInfo",
    "RetryDecision",
    "RetryPolicy",
    "create_circuit_breaker",
    "create_retry_manager",
]
