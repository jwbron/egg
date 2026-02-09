"""Tests for egg_contracts.phase_defaults module."""

import pytest
from egg_contracts.models import HumanGateType, PipelinePhase
from egg_contracts.phase_defaults import (
    DEFAULT_IMPLEMENT_CONFIG,
    DEFAULT_PHASE_CONFIGS,
    DEFAULT_PLAN_CONFIG,
    DEFAULT_REFINE_CONFIG,
    get_implement_phase_config,
    get_phase_config,
    get_plan_phase_config,
    get_refine_phase_config,
)


class TestRefinePhaseConfig:
    """Tests for refine phase configuration."""

    def test_refine_config_phase(self):
        """Test that refine config has correct phase."""
        config = get_refine_phase_config()
        assert config.phase == PipelinePhase.REFINE

    def test_refine_config_human_gate(self):
        """Test that refine uses issue checkbox for approval."""
        config = get_refine_phase_config()
        assert config.human_gate == HumanGateType.ISSUE_CHECKBOX

    def test_refine_config_prompts(self):
        """Test that refine config has correct prompt scripts."""
        config = get_refine_phase_config()
        assert "build-sdlc-prompt.sh" in config.work_prompt_script
        assert "build-refine-review-prompt.sh" in config.review_prompt_script

    def test_refine_config_draft_pattern(self):
        """Test that refine config has analysis draft pattern."""
        config = get_refine_phase_config()
        assert "analysis" in config.draft_file_pattern


class TestPlanPhaseConfig:
    """Tests for plan phase configuration."""

    def test_plan_config_phase(self):
        """Test that plan config has correct phase."""
        config = get_plan_phase_config()
        assert config.phase == PipelinePhase.PLAN

    def test_plan_config_human_gate(self):
        """Test that plan uses issue checkbox for approval."""
        config = get_plan_phase_config()
        assert config.human_gate == HumanGateType.ISSUE_CHECKBOX

    def test_plan_config_has_yaml_check(self):
        """Test that plan config includes YAML validation check."""
        config = get_plan_phase_config()
        check_ids = [c.id for c in config.checks]
        assert "check-plan-yaml" in check_ids

    def test_plan_yaml_check_depends_on_draft(self):
        """Test that YAML check runs after draft validation."""
        config = get_plan_phase_config()
        yaml_check = next(c for c in config.checks if c.id == "check-plan-yaml")
        assert "check-draft-validation" in yaml_check.dependencies


class TestImplementPhaseConfig:
    """Tests for implement phase configuration."""

    def test_implement_config_phase(self):
        """Test that implement config has correct phase."""
        config = get_implement_phase_config()
        assert config.phase == PipelinePhase.IMPLEMENT

    def test_implement_config_human_gate(self):
        """Test that implement uses PR review for approval."""
        config = get_implement_phase_config()
        assert config.human_gate == HumanGateType.PR_REVIEW

    def test_implement_config_max_cycles(self):
        """Test that implement has higher max cycles."""
        config = get_implement_phase_config()
        assert config.max_cycles >= 5

    def test_implement_config_check_dag_order(self):
        """Test that implement checks follow correct DAG order."""
        config = get_implement_phase_config()

        # Get checks by ID
        checks = {c.id: c for c in config.checks}

        # Merge conflict check has no dependencies (runs first)
        assert checks["check-merge-conflict"].dependencies == []

        # Lint and test depend on merge conflict
        assert "check-merge-conflict" in checks["check-lint"].dependencies
        assert "check-merge-conflict" in checks["check-test"].dependencies

        # Fixer depends on lint and test
        assert "check-lint" in checks["check-fixer"].dependencies
        assert "check-test" in checks["check-fixer"].dependencies

    def test_implement_config_has_fixers(self):
        """Test that implement checks have fixer scripts where appropriate."""
        config = get_implement_phase_config()
        checks = {c.id: c for c in config.checks}

        # Merge conflict and lint have fixers
        assert checks["check-merge-conflict"].fixer_script is not None
        assert checks["check-lint"].fixer_script is not None

    def test_implement_config_test_retry(self):
        """Test that test check has retry configured."""
        config = get_implement_phase_config()
        test_check = next(c for c in config.checks if c.id == "check-test")
        assert test_check.retry_count >= 1


class TestGetPhaseConfig:
    """Tests for get_phase_config function."""

    def test_get_refine_config(self):
        """Test getting refine config by phase."""
        config = get_phase_config(PipelinePhase.REFINE)
        assert config.phase == PipelinePhase.REFINE

    def test_get_plan_config(self):
        """Test getting plan config by phase."""
        config = get_phase_config(PipelinePhase.PLAN)
        assert config.phase == PipelinePhase.PLAN

    def test_get_implement_config(self):
        """Test getting implement config by phase."""
        config = get_phase_config(PipelinePhase.IMPLEMENT)
        assert config.phase == PipelinePhase.IMPLEMENT

    def test_get_pr_config_raises(self):
        """Test that PR phase raises ValueError."""
        with pytest.raises(ValueError, match="No default configuration"):
            get_phase_config(PipelinePhase.PR)


class TestDefaultPhaseConfigs:
    """Tests for pre-built default configs."""

    def test_default_refine_config_exists(self):
        """Test that DEFAULT_REFINE_CONFIG is available."""
        assert DEFAULT_REFINE_CONFIG.phase == PipelinePhase.REFINE

    def test_default_plan_config_exists(self):
        """Test that DEFAULT_PLAN_CONFIG is available."""
        assert DEFAULT_PLAN_CONFIG.phase == PipelinePhase.PLAN

    def test_default_implement_config_exists(self):
        """Test that DEFAULT_IMPLEMENT_CONFIG is available."""
        assert DEFAULT_IMPLEMENT_CONFIG.phase == PipelinePhase.IMPLEMENT

    def test_default_phase_configs_map(self):
        """Test that DEFAULT_PHASE_CONFIGS map is complete."""
        assert PipelinePhase.REFINE in DEFAULT_PHASE_CONFIGS
        assert PipelinePhase.PLAN in DEFAULT_PHASE_CONFIGS
        assert PipelinePhase.IMPLEMENT in DEFAULT_PHASE_CONFIGS
        # PR phase should not be in the map
        assert PipelinePhase.PR not in DEFAULT_PHASE_CONFIGS
