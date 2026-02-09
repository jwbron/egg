"""Default phase configurations for the SDLC pipeline.

This module provides pre-configured PhaseConfig instances for each pipeline phase
(refine, plan, implement). These defaults can be used when initializing contracts
or when the pipeline needs to set up a phase configuration.

The check DAG for the implement phase follows this order:
1. merge-conflict-check (first, handles merge conflicts before other checks)
2. lint-check and test-check (in parallel, after merge conflicts resolved)
3. check-fixer (last, attempts to fix any failures)
"""

from .models import CheckDefinition, HumanGateType, PhaseConfig, PipelinePhase


def get_refine_phase_config() -> PhaseConfig:
    """Get default configuration for the refine phase.

    The refine phase:
    - Analyzes the issue and gathers requirements
    - Produces an analysis document
    - Has no automated checks (analysis is subjective)
    - Requires human approval via issue checkbox
    """
    return PhaseConfig(
        phase=PipelinePhase.REFINE,
        work_prompt_script="action/build-sdlc-prompt.sh",
        review_prompt_script="action/build-refine-review-prompt.sh",
        checks=[
            CheckDefinition(
                id="check-draft-validation",
                name="Validate draft analysis",
                script=".github/scripts/checks/draft-validation-check.sh",
                required=True,
            ),
        ],
        max_cycles=3,
        human_gate=HumanGateType.ISSUE_CHECKBOX,
        draft_file_pattern=".egg-state/drafts/{issue_number}-analysis.md",
        review_file_pattern=".egg-state/reviews/{issue_number}-refine-review.json",
    )


def get_plan_phase_config() -> PhaseConfig:
    """Get default configuration for the plan phase.

    The plan phase:
    - Creates an implementation plan based on the analysis
    - Produces a plan document with YAML task definitions
    - Validates the plan structure and YAML
    - Requires human approval via issue checkbox
    """
    return PhaseConfig(
        phase=PipelinePhase.PLAN,
        work_prompt_script="action/build-sdlc-prompt.sh",
        review_prompt_script="action/build-plan-review-prompt.sh",
        checks=[
            CheckDefinition(
                id="check-draft-validation",
                name="Validate draft plan",
                script=".github/scripts/checks/draft-validation-check.sh",
                required=True,
            ),
            CheckDefinition(
                id="check-plan-yaml",
                name="Validate plan YAML",
                script=".github/scripts/checks/plan-yaml-check.sh",
                required=True,
                dependencies=["check-draft-validation"],
            ),
        ],
        max_cycles=3,
        human_gate=HumanGateType.ISSUE_CHECKBOX,
        draft_file_pattern=".egg-state/drafts/{issue_number}-plan.md",
        review_file_pattern=".egg-state/reviews/{issue_number}-plan-review.json",
    )


def get_implement_phase_config() -> PhaseConfig:
    """Get default configuration for the implement phase.

    The implement phase:
    - Implements the tasks defined in the plan
    - Runs merge conflict detection and resolution first
    - Runs linters and tests in parallel
    - Runs check fixer if any checks fail
    - Requires human approval via PR review
    """
    return PhaseConfig(
        phase=PipelinePhase.IMPLEMENT,
        work_prompt_script="action/build-sdlc-prompt.sh",
        review_prompt_script="action/build-review-prompt.sh",
        checks=[
            # First: Handle merge conflicts before anything else
            CheckDefinition(
                id="check-merge-conflict",
                name="Check merge conflicts",
                script=".github/scripts/checks/merge-conflict-check.sh",
                required=True,
                fixer_script=".github/scripts/checks/merge-conflict-fixer.sh",
            ),
            # Second tier: Lint and test run in parallel after merge conflicts
            CheckDefinition(
                id="check-lint",
                name="Run linters",
                script=".github/scripts/checks/lint-check.sh",
                required=True,
                dependencies=["check-merge-conflict"],
                fixer_script=".github/scripts/checks/lint-fixer.sh",
            ),
            CheckDefinition(
                id="check-test",
                name="Run tests",
                script=".github/scripts/checks/test-check.sh",
                required=True,
                dependencies=["check-merge-conflict"],
                retry_count=1,  # Allow one retry for flaky tests
            ),
            # Last: Run fixer for any remaining failures
            CheckDefinition(
                id="check-fixer",
                name="Attempt automatic fixes",
                script=".github/scripts/checks/check-fixer.sh",
                required=False,  # Fixer is best-effort
                dependencies=["check-lint", "check-test"],
            ),
        ],
        max_cycles=5,  # More cycles for implementation due to complexity
        human_gate=HumanGateType.PR_REVIEW,
        draft_file_pattern=".egg-state/drafts/{issue_number}-implementation.md",
        review_file_pattern=".egg-state/reviews/{issue_number}-implement-review.json",
    )


def get_phase_config(phase: PipelinePhase) -> PhaseConfig:
    """Get the default configuration for a given pipeline phase.

    Args:
        phase: The pipeline phase to get configuration for.

    Returns:
        The default PhaseConfig for the specified phase.

    Raises:
        ValueError: If the phase is not supported (e.g., PR phase).
    """
    config_map = {
        PipelinePhase.REFINE: get_refine_phase_config,
        PipelinePhase.PLAN: get_plan_phase_config,
        PipelinePhase.IMPLEMENT: get_implement_phase_config,
    }

    if phase not in config_map:
        raise ValueError(
            f"No default configuration for phase '{phase}'. "
            f"Supported phases: {list(config_map.keys())}"
        )

    return config_map[phase]()


# Pre-built configs for convenience
DEFAULT_REFINE_CONFIG = get_refine_phase_config()
DEFAULT_PLAN_CONFIG = get_plan_phase_config()
DEFAULT_IMPLEMENT_CONFIG = get_implement_phase_config()

# Map of phase to default config
DEFAULT_PHASE_CONFIGS: dict[PipelinePhase, PhaseConfig] = {
    PipelinePhase.REFINE: DEFAULT_REFINE_CONFIG,
    PipelinePhase.PLAN: DEFAULT_PLAN_CONFIG,
    PipelinePhase.IMPLEMENT: DEFAULT_IMPLEMENT_CONFIG,
}
