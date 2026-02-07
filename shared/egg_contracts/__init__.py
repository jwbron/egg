"""
Egg Contracts - Role-based contract management for SDLC pipelines.

This package provides:
- Contract models (Pydantic) for type-safe contract manipulation
- Role-based field access enforcement
- Contract loading/saving utilities
- Audit logging for all modifications
"""

from .audit import create_audit_entry
from .loader import get_contract_path, load_contract, save_contract
from .models import (
    AcceptanceCriterion,
    AuditAction,
    AuditEntry,
    CircuitBreaker,
    CircuitBreakerStatus,
    Contract,
    Decision,
    DecisionOption,
    DecisionType,
    Issue,
    Phase,
    PhaseStatus,
    PipelinePhase,
    ReviewFeedback,
    Task,
    TaskStatus,
)
from .plan_parser import (
    ParsedPhase,
    ParsedPlan,
    ParsedTask,
    extract_tasks_to_contract,
    load_plan_from_file,
    parse_plan_document,
    sync_contract_from_plan,
)
from .roles import FieldAccess, Role, get_field_owner
from .validator import ContractValidator, ValidationError

__all__ = [
    # Models
    "AcceptanceCriterion",
    "AuditAction",
    "AuditEntry",
    "CircuitBreaker",
    "CircuitBreakerStatus",
    "Contract",
    "Decision",
    "DecisionOption",
    "DecisionType",
    "Issue",
    "Phase",
    "PhaseStatus",
    "PipelinePhase",
    "ReviewFeedback",
    "Task",
    "TaskStatus",
    # Roles
    "FieldAccess",
    "Role",
    "get_field_owner",
    # Loader
    "load_contract",
    "save_contract",
    "get_contract_path",
    # Validator
    "ContractValidator",
    "ValidationError",
    # Audit
    "create_audit_entry",
    # Plan Parser
    "ParsedPlan",
    "ParsedPhase",
    "ParsedTask",
    "extract_tasks_to_contract",
    "load_plan_from_file",
    "parse_plan_document",
    "sync_contract_from_plan",
]
