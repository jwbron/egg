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

from .audit import (
    create_audit_entry,
    create_transition_entry,
    create_update_entry,
    format_audit_log,
)
from .circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerResult,
    check_all_thresholds,
    check_phase_threshold,
    check_pipeline_threshold,
    check_task_threshold,
    close_circuit_breaker,
    get_escalation_summary,
    increment_phase_cycle,
    increment_pipeline_cycle,
    increment_task_cycle,
    open_circuit_breaker,
    should_escalate,
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
    AuditAction,
    AuditEntry,
    AuditRole,
    CircuitBreaker,
    CircuitBreakerStatus,
    Contract,
    Decision,
    DecisionOption,
    DecisionType,
    IssueInfo,
    Phase,
    PhaseStatus,
    PipelinePhase,
    ReviewFeedback,
    Task,
    TaskStatus,
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
    "AuditAction",
    "AuditEntry",
    "AuditRole",
    "CircuitBreaker",
    "CircuitBreakerStatus",
    "Contract",
    "ContractNotFoundError",
    "ContractValidationError",
    "Decision",
    "DecisionOption",
    "DecisionType",
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
    # Circuit Breaker
    "CircuitBreakerConfig",
    "CircuitBreakerResult",
    "check_all_thresholds",
    "check_phase_threshold",
    "check_pipeline_threshold",
    "check_task_threshold",
    "close_circuit_breaker",
    "get_escalation_summary",
    "increment_phase_cycle",
    "increment_pipeline_cycle",
    "increment_task_cycle",
    "open_circuit_breaker",
    "should_escalate",
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
]
