"""Tests for the new ``PhaseConfig.reviewer_code.parallel`` knob.

Covers TASK-3-2 of issue #1965:

- ``ReviewerCodeConfig.parallel`` defaults to ``True``.
- ``PhaseConfig`` accepts an explicit ``reviewer_code=ReviewerCodeConfig(parallel=False)``.
- ``PhaseConfig()`` (no ``reviewer_code``) still validates — backward-compat.
- A ``Contract`` with the new field round-trips through
  ``model_dump_json`` / ``model_validate_json`` cleanly.
- A legacy contract JSON without the field still loads.
- ``get_reviewer_code_parallel(contract)`` returns:
    * ``True`` when ``phase_configs`` is ``None``.
    * ``True`` when implement-phase config is missing.
    * ``True`` when ``reviewer_code`` is ``None`` on the implement config.
    * ``True`` when explicit ``parallel=True``.
    * ``False`` when explicit ``parallel=False``.
"""

from __future__ import annotations

from egg_contracts.models import (
    Contract,
    IssueInfo,
    PhaseConfig,
    PipelinePhase,
    ReviewerCodeConfig,
    get_reviewer_code_parallel,
)


def _build_contract(
    phase_configs: dict[PipelinePhase, PhaseConfig] | None = None,
) -> Contract:
    return Contract(
        issue=IssueInfo(
            number=1965,
            title="dummy",
            url="https://github.com/jwbron/egg/issues/1965",
        ),
        pipeline_id="issue-1965",
        phase_configs=phase_configs,
    )


# ---------------------------------------------------------------------------
# ReviewerCodeConfig
# ---------------------------------------------------------------------------


class TestReviewerCodeConfig:
    def test_default_parallel_true(self) -> None:
        cfg = ReviewerCodeConfig()
        assert cfg.parallel is True

    def test_explicit_parallel_false(self) -> None:
        cfg = ReviewerCodeConfig(parallel=False)
        assert cfg.parallel is False


# ---------------------------------------------------------------------------
# PhaseConfig schema
# ---------------------------------------------------------------------------


class TestPhaseConfigReviewerCodeField:
    def test_phase_config_default_is_none(self) -> None:
        cfg = PhaseConfig()
        assert cfg.reviewer_code is None

    def test_phase_config_accepts_explicit_reviewer_code(self) -> None:
        cfg = PhaseConfig(reviewer_code=ReviewerCodeConfig(parallel=False))
        assert cfg.reviewer_code is not None
        assert cfg.reviewer_code.parallel is False

    def test_phase_config_accepts_dict_reviewer_code(self) -> None:
        """Pydantic should coerce a plain dict into the nested model."""
        cfg = PhaseConfig.model_validate({"reviewer_code": {"parallel": False}})
        assert cfg.reviewer_code is not None
        assert cfg.reviewer_code.parallel is False


# ---------------------------------------------------------------------------
# Contract round-trip
# ---------------------------------------------------------------------------


class TestContractRoundTripWithReviewerCodeField:
    def test_round_trip_with_explicit_false(self) -> None:
        contract = _build_contract(
            phase_configs={
                PipelinePhase.IMPLEMENT: PhaseConfig(
                    reviewer_code=ReviewerCodeConfig(parallel=False),
                )
            }
        )
        serialized = contract.model_dump_json()
        restored = Contract.model_validate_json(serialized)
        assert restored.phase_configs is not None
        impl = restored.phase_configs[PipelinePhase.IMPLEMENT]
        assert impl.reviewer_code is not None
        assert impl.reviewer_code.parallel is False

    def test_round_trip_with_default(self) -> None:
        contract = _build_contract(
            phase_configs={
                PipelinePhase.IMPLEMENT: PhaseConfig(
                    reviewer_code=ReviewerCodeConfig(),
                )
            }
        )
        serialized = contract.model_dump_json()
        restored = Contract.model_validate_json(serialized)
        impl = restored.phase_configs[PipelinePhase.IMPLEMENT]
        assert impl.reviewer_code is not None
        assert impl.reviewer_code.parallel is True

    def test_legacy_contract_without_field_still_validates(self) -> None:
        """JSON written by an older orchestrator (no ``reviewer_code`` field)."""
        legacy_payload = {
            "schemaVersion": "1.0",
            "issue": {
                "number": 1234,
                "title": "legacy",
                "url": "https://github.com/jwbron/egg/issues/1234",
            },
            "pipeline_id": "issue-1234",
            "current_phase": "implement",
            "acceptance_criteria": [],
            "phases": [],
            "decisions": [],
            "phase_configs": {
                "implement": {
                    "checks": [],
                    "max_review_cycles": 3,
                    "human_review_mechanism": "ISSUE_CHECKBOX",
                    # No reviewer_code field — legacy.
                }
            },
        }
        contract = Contract.model_validate(legacy_payload)
        impl = contract.phase_configs[PipelinePhase.IMPLEMENT]
        assert impl.reviewer_code is None

    def test_contract_with_no_phase_configs_validates(self) -> None:
        """``phase_configs=None`` is still valid."""
        contract = _build_contract(phase_configs=None)
        assert contract.phase_configs is None


# ---------------------------------------------------------------------------
# get_reviewer_code_parallel accessor
# ---------------------------------------------------------------------------


class TestGetReviewerCodeParallelAccessor:
    def test_returns_true_when_phase_configs_is_none(self) -> None:
        contract = _build_contract(phase_configs=None)
        assert get_reviewer_code_parallel(contract) is True

    def test_returns_true_when_implement_config_missing(self) -> None:
        contract = _build_contract(phase_configs={PipelinePhase.PLAN: PhaseConfig()})
        assert get_reviewer_code_parallel(contract) is True

    def test_returns_true_when_reviewer_code_is_none(self) -> None:
        contract = _build_contract(
            phase_configs={PipelinePhase.IMPLEMENT: PhaseConfig(reviewer_code=None)}
        )
        assert get_reviewer_code_parallel(contract) is True

    def test_returns_true_for_explicit_true(self) -> None:
        contract = _build_contract(
            phase_configs={
                PipelinePhase.IMPLEMENT: PhaseConfig(
                    reviewer_code=ReviewerCodeConfig(parallel=True)
                )
            }
        )
        assert get_reviewer_code_parallel(contract) is True

    def test_returns_false_for_explicit_false(self) -> None:
        contract = _build_contract(
            phase_configs={
                PipelinePhase.IMPLEMENT: PhaseConfig(
                    reviewer_code=ReviewerCodeConfig(parallel=False)
                )
            }
        )
        assert get_reviewer_code_parallel(contract) is False
