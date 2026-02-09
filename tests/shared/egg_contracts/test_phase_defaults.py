"""Tests for egg_contracts.phase_defaults module."""

from egg_contracts import (
    CheckDefinition,
    Contract,
    HumanReviewMechanism,
    IssueInfo,
    PhaseConfig,
    PipelinePhase,
)
from egg_contracts.phase_defaults import (
    get_default_phase_config,
    get_effective_phase_config,
)


class TestGetDefaultPhaseConfig:
    """Tests for get_default_phase_config function."""

    def test_refine_phase_defaults(self):
        """Test default config for refine phase."""
        config = get_default_phase_config(PipelinePhase.REFINE)
        assert isinstance(config, PhaseConfig)
        assert config.max_review_cycles == 3
        assert config.human_review_mechanism == HumanReviewMechanism.ISSUE_CHECKBOX
        # Should have draft validation check
        check_ids = [c.id for c in config.checks]
        assert "check-draft-validation" in check_ids

    def test_plan_phase_defaults(self):
        """Test default config for plan phase."""
        config = get_default_phase_config(PipelinePhase.PLAN)
        assert isinstance(config, PhaseConfig)
        assert config.max_review_cycles == 3
        assert config.human_review_mechanism == HumanReviewMechanism.ISSUE_CHECKBOX
        # Should have plan yaml check
        check_ids = [c.id for c in config.checks]
        assert "check-plan-yaml" in check_ids

    def test_implement_phase_defaults(self):
        """Test default config for implement phase."""
        config = get_default_phase_config(PipelinePhase.IMPLEMENT)
        assert isinstance(config, PhaseConfig)
        assert config.max_review_cycles == 3
        assert config.human_review_mechanism == HumanReviewMechanism.PR_REVIEW
        # Should have merge conflict, lint, test, and fixer checks
        check_ids = [c.id for c in config.checks]
        assert "check-merge-conflict" in check_ids
        assert "check-lint" in check_ids
        assert "check-test" in check_ids
        assert "check-fixer" in check_ids

    def test_pr_phase_defaults(self):
        """Test default config for PR phase."""
        config = get_default_phase_config(PipelinePhase.PR)
        assert isinstance(config, PhaseConfig)
        assert config.max_review_cycles == 3
        assert config.human_review_mechanism == HumanReviewMechanism.PR_REVIEW
        # PR phase has no default checks
        assert config.checks == []

    def test_all_phases_have_defaults(self):
        """Test that all pipeline phases have default configs."""
        for phase in PipelinePhase:
            config = get_default_phase_config(phase)
            assert isinstance(config, PhaseConfig)

    def test_check_definitions_are_valid(self):
        """Test that all default check definitions are valid."""
        for phase in PipelinePhase:
            config = get_default_phase_config(phase)
            for check in config.checks:
                assert isinstance(check, CheckDefinition)
                assert check.id.startswith("check-")
                assert len(check.name) > 0
                assert len(check.script) > 0


class TestGetEffectivePhaseConfig:
    """Tests for get_effective_phase_config function."""

    def test_returns_defaults_when_no_overrides(self):
        """Test that defaults are returned when contract has no overrides."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
        )
        config = get_effective_phase_config(contract, PipelinePhase.IMPLEMENT)
        default_config = get_default_phase_config(PipelinePhase.IMPLEMENT)

        assert config.max_review_cycles == default_config.max_review_cycles
        assert config.human_review_mechanism == default_config.human_review_mechanism
        assert len(config.checks) == len(default_config.checks)

    def test_returns_defaults_when_phase_not_in_overrides(self):
        """Test defaults returned when specific phase has no override."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
            phase_configs={
                PipelinePhase.REFINE: PhaseConfig(max_review_cycles=5),
            },
        )
        # Request IMPLEMENT phase which is not overridden
        config = get_effective_phase_config(contract, PipelinePhase.IMPLEMENT)
        default_config = get_default_phase_config(PipelinePhase.IMPLEMENT)

        assert config.max_review_cycles == default_config.max_review_cycles

    def test_contract_overrides_max_review_cycles(self):
        """Test that contract override for max_review_cycles is used."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
            phase_configs={
                PipelinePhase.IMPLEMENT: PhaseConfig(max_review_cycles=10),
            },
        )
        config = get_effective_phase_config(contract, PipelinePhase.IMPLEMENT)
        assert config.max_review_cycles == 10

    def test_contract_overrides_human_review_mechanism(self):
        """Test that contract override for human_review_mechanism is used."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
            phase_configs={
                PipelinePhase.REFINE: PhaseConfig(
                    human_review_mechanism=HumanReviewMechanism.PR_REVIEW,
                ),
            },
        )
        config = get_effective_phase_config(contract, PipelinePhase.REFINE)
        assert config.human_review_mechanism == HumanReviewMechanism.PR_REVIEW

    def test_contract_overrides_checks_completely(self):
        """Test that contract checks replace defaults entirely."""
        custom_check = CheckDefinition(
            id="check-custom",
            name="Custom Check",
            script="custom.py",
        )
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
            phase_configs={
                PipelinePhase.IMPLEMENT: PhaseConfig(
                    checks=[custom_check],
                ),
            },
        )
        config = get_effective_phase_config(contract, PipelinePhase.IMPLEMENT)

        # Should only have the custom check, not the defaults
        assert len(config.checks) == 1
        assert config.checks[0].id == "check-custom"

    def test_empty_checks_uses_defaults(self):
        """Test that empty checks list uses default checks."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
            phase_configs={
                PipelinePhase.IMPLEMENT: PhaseConfig(
                    checks=[],
                    max_review_cycles=5,
                ),
            },
        )
        config = get_effective_phase_config(contract, PipelinePhase.IMPLEMENT)
        default_config = get_default_phase_config(PipelinePhase.IMPLEMENT)

        # Should use default checks since override is empty
        assert len(config.checks) == len(default_config.checks)
        # But should use overridden max_review_cycles
        assert config.max_review_cycles == 5
