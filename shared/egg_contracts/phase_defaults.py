"""
Default phase configurations for the SDLC pipeline.

This module provides default check configurations for each pipeline phase,
with the ability to merge contract-specific overrides.
"""

from .models import (
    CheckDefinition,
    Contract,
    HumanReviewMechanism,
    PhaseConfig,
    PipelinePhase,
)

# Default checks for the refine phase
_REFINE_CHECKS: list[CheckDefinition] = [
    CheckDefinition(
        id="check-draft-validation",
        name="Draft Validation",
        script="draft_validation_check.py",
        required=True,
        retry_on_fail=False,
        max_retries=0,
    ),
]

# Default checks for the plan phase
_PLAN_CHECKS: list[CheckDefinition] = [
    CheckDefinition(
        id="check-plan-yaml",
        name="Plan YAML Validation",
        script="plan_yaml_check.py",
        required=True,
        retry_on_fail=False,
        max_retries=0,
    ),
]

# Default checks for the implement phase
_IMPLEMENT_CHECKS: list[CheckDefinition] = [
    CheckDefinition(
        id="check-merge-conflict",
        name="Merge Conflict Check",
        script="merge_conflict_check.py",
        required=True,
        retry_on_fail=False,
        max_retries=0,
    ),
    CheckDefinition(
        id="check-lint",
        name="Lint Check",
        script="lint_check.py",
        required=True,
        retry_on_fail=True,
        max_retries=1,
    ),
    CheckDefinition(
        id="check-test",
        name="Test Check",
        script="test_check.py",
        required=True,
        retry_on_fail=False,
        max_retries=0,
    ),
    CheckDefinition(
        id="check-fixer",
        name="Auto-fix Check",
        script="check_fixer.py",
        required=False,
        retry_on_fail=False,
        max_retries=0,
    ),
]

# Default checks for the PR phase (empty by default)
_PR_CHECKS: list[CheckDefinition] = []

# Default phase configurations
_DEFAULT_PHASE_CONFIGS: dict[PipelinePhase, PhaseConfig] = {
    PipelinePhase.REFINE: PhaseConfig(
        checks=_REFINE_CHECKS,
        max_review_cycles=3,
        human_review_mechanism=HumanReviewMechanism.ISSUE_CHECKBOX,
    ),
    PipelinePhase.PLAN: PhaseConfig(
        checks=_PLAN_CHECKS,
        max_review_cycles=3,
        human_review_mechanism=HumanReviewMechanism.ISSUE_CHECKBOX,
    ),
    PipelinePhase.IMPLEMENT: PhaseConfig(
        checks=_IMPLEMENT_CHECKS,
        max_review_cycles=3,
        human_review_mechanism=HumanReviewMechanism.PR_REVIEW,
    ),
    PipelinePhase.PR: PhaseConfig(
        checks=_PR_CHECKS,
        max_review_cycles=3,
        human_review_mechanism=HumanReviewMechanism.PR_REVIEW,
    ),
}


def get_default_phase_config(phase: PipelinePhase) -> PhaseConfig:
    """Get the default configuration for a pipeline phase.

    Args:
        phase: The pipeline phase to get configuration for.

    Returns:
        A copy of the default PhaseConfig for the given phase.
        Returns a deep copy to prevent accidental mutation of global defaults.
    """
    return _DEFAULT_PHASE_CONFIGS[phase].model_copy(deep=True)


def get_effective_phase_config(contract: Contract, phase: PipelinePhase) -> PhaseConfig:
    """Get the effective configuration for a phase, merging contract overrides with defaults.

    Contract-specific values take precedence over defaults. For the checks list,
    if the contract specifies any checks, they completely replace the defaults.

    Args:
        contract: The contract to get configuration from.
        phase: The pipeline phase to get configuration for.

    Returns:
        The effective PhaseConfig, with contract overrides applied.
    """
    default_config = get_default_phase_config(phase)

    # If no phase_configs in contract, return defaults
    if contract.phase_configs is None:
        return default_config

    # If this phase has no override, return defaults
    contract_config = contract.phase_configs.get(phase)
    if contract_config is None:
        return default_config

    # Merge: contract values take precedence
    # For checks, if contract specifies any, they replace defaults entirely
    checks = contract_config.checks if contract_config.checks else default_config.checks
    max_review_cycles = contract_config.max_review_cycles
    human_review_mechanism = contract_config.human_review_mechanism

    return PhaseConfig(
        checks=checks,
        max_review_cycles=max_review_cycles,
        human_review_mechanism=human_review_mechanism,
    )
