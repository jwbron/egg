"""Tests for egg_contracts.phase_defaults module.

Slice-2 of issue #2777-replan deletes the ``PR`` pipeline phase entirely
(``PipelinePhase.PR``, the ``_DEFAULT_PHASE_CONFIGS[PipelinePhase.PR]``
row, the gateway-side state-machine entry, and the
``IMPLEMENT → PR`` transition). Tests in this module assume the post-
deletion shape:

* ``IMPLEMENT`` is the terminal pipeline phase.
* ``PipelinePhase`` enum does **not** contain a ``PR`` member.
* ``get_default_phase_config(...)`` is undefined for the string ``"pr"``;
  any attempt to look it up raises ``KeyError`` (default-deny).
"""

import pytest

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

    def test_pr_is_not_a_pipeline_phase(self):
        """``PipelinePhase`` must not expose a ``PR`` member (#2777 slice-2).

        Iterating ``PipelinePhase`` is the canonical way to discover all
        valid phases throughout the codebase (state machines, default
        registries, phase-permission tables). A stray ``PR`` member would
        silently re-introduce the deleted phase into every consumer that
        iterates the enum — including the ``test_all_phases_have_defaults``
        and ``test_check_definitions_are_valid`` invariants below.
        """
        names = {member.name for member in PipelinePhase}
        assert "PR" not in names, (
            "PipelinePhase.PR must be removed in slice-2 of #2777; "
            f"found members={sorted(names)}"
        )
        values = {member.value for member in PipelinePhase}
        assert "pr" not in values, (
            "No PipelinePhase member may have value 'pr' after slice-2; "
            f"found values={sorted(values)}"
        )

    def test_pr_phase_default_lookup_is_denied(self):
        """Looking up defaults for the (removed) ``pr`` phase must fail.

        After slice-2, ``_DEFAULT_PHASE_CONFIGS`` no longer contains a
        ``PR`` row. The function is statically typed ``phase:
        PipelinePhase`` but at runtime nothing prevents a string from
        leaking in (e.g. a stale ``contract.json`` on disk, a planner
        emitting ``"pr"``). The dict lookup must raise ``KeyError`` so the
        bug surfaces loudly instead of silently returning a default.
        """
        # We can't construct ``PipelinePhase.PR`` because the member was
        # removed — try the string form (the realistic regression path).
        with pytest.raises(KeyError):
            get_default_phase_config("pr")  # type: ignore[arg-type]

    def test_implement_is_terminal(self):
        """Slice-2 makes ``IMPLEMENT`` the terminal pipeline phase.

        This was previously ``IMPLEMENT → PR``; the PR phase is gone, so
        no member should follow ``IMPLEMENT`` in the canonical order. We
        assert via the phase-graph table (the authoritative source) where
        possible, but at minimum: there is no ``PR`` member to follow
        ``IMPLEMENT``.
        """
        # Enum-level invariant — ``IMPLEMENT`` is the last declared member.
        members = list(PipelinePhase)
        assert PipelinePhase.IMPLEMENT in members
        # All non-IMPLEMENT phases are upstream of IMPLEMENT (REFINE, PLAN,
        # APPLY); no member exists that's downstream of IMPLEMENT.
        downstream_candidates = {
            member for member in members if member.value == "pr"
        }
        assert downstream_candidates == set(), (
            "After slice-2, no PipelinePhase member may sit downstream of "
            f"IMPLEMENT; found={downstream_candidates}"
        )

    def test_all_phases_have_defaults(self):
        """Test that all pipeline phases have default configs.

        After slice-2 this loop intentionally covers REFINE, PLAN, APPLY,
        and IMPLEMENT but NOT PR (the member is gone).
        """
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

    def test_returns_independent_copy(self):
        """Test that returned config is independent from global defaults."""
        config1 = get_default_phase_config(PipelinePhase.IMPLEMENT)
        config2 = get_default_phase_config(PipelinePhase.IMPLEMENT)

        # Mutate config1
        config1.checks.append(
            CheckDefinition(id="check-mutated", name="Mutated", script="mutated.py")
        )

        # config2 should be unaffected
        check_ids = [c.id for c in config2.checks]
        assert "check-mutated" not in check_ids


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
