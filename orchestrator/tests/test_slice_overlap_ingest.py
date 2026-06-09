"""Plan-ingestion rejection for slice file-overlap violations (#3046).

Two slices that touch overlapping ``files_affected`` with no dependency
ordering between them are a slice-DAG structural defect: their integration
branches fork independently off the shared base and collide at
integration (the #3023 modify/delete conflict). The populator
(:func:`_populate_contract_from_plan`) rejects them with the same
NACK-the-architect handling as a forest violation, but a distinct
``slice_overlap_violation`` reason so operator-facing prose names the
actual defect.

These tests cover the orchestrator wiring around
``egg_contracts.validate_slice_file_overlap`` (the validator's own unit
tests live in ``shared/egg_contracts/tests/test_validate_slice_file_overlap.py``):

* End-to-end: a plan whose slices overlap without ordering raises
  ``ForestValidationError(reason="slice_overlap_violation")``, stashes a
  'Plan ingestion REJECTED' block on ``plan_review_feedback``, and leaves
  ``contract.slices`` empty.
* The safe wrapper maps that reason onto
  ``PopulateOutcome.SLICE_OVERLAP_VIOLATION``.
* ``ForestValidationError.to_response`` keys the 422 body on the reason.
* The empty-contract HITL question uses overlap-specific prose.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from egg_contracts.models import Slice, Task


def _overlapping_roots() -> list[Slice]:
    """Two root slices that both touch ``shared.py`` with no edge."""
    wrapper = "orchestrator/consensus_wrapper.py"
    return [
        Slice(
            id="slice-1",
            name="A",
            dependencies=[],
            tasks=[Task(id="task-1", description="d", files_affected=[wrapper, "a.py"])],
        ),
        Slice(
            id="slice-2",
            name="B",
            dependencies=[],
            tasks=[Task(id="task-2", description="d", files_affected=[wrapper, "b.py"])],
        ),
    ]


class TestPopulatorRejectsOverlap:
    def test_overlap_raises_and_stashes_feedback(self, tmp_path):
        from egg_contracts.loader import create_contract, load_contract
        from routes.pipelines import (
            ForestValidationError,
            _populate_contract_from_plan,
        )

        pipeline_id = "issue-overlap"
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        (drafts_dir / f"{pipeline_id}-plan.md").write_text("# Plan\n")

        fake_result = MagicMock()
        fake_result.success = True
        fake_result.warnings = []
        fake_result.to_contract_slices.return_value = _overlapping_roots()
        fake_result.pr_title = None

        with patch("egg_contracts.plan_parser.parse_plan", return_value=fake_result):
            with pytest.raises(ForestValidationError) as exc_info:
                _populate_contract_from_plan(tmp_path, pipeline_id, "local")

        assert exc_info.value.reason == "slice_overlap_violation"
        assert exc_info.value.errors  # structured errors carried for the NACK

        # The slices were NOT written; the NACK feedback was stashed.
        contract = load_contract(pipeline_id, tmp_path)
        assert list(contract.slices) == []
        assert contract.plan_review_feedback is not None
        assert "Plan ingestion REJECTED" in contract.plan_review_feedback
        assert "overlapping files" in contract.plan_review_feedback

    def test_safe_wrapper_maps_overlap_reason_to_outcome(self, tmp_path):
        from routes.pipelines import (
            ForestValidationError,
            PopulateOutcome,
            _populate_contract_from_plan_safe,
        )

        with patch(
            "routes.pipelines._populate_contract_from_plan",
            side_effect=ForestValidationError(
                "overlap", errors=["bad pair"], reason="slice_overlap_violation"
            ),
        ):
            result = _populate_contract_from_plan_safe(tmp_path, "p-overlap", "local")
        assert result.outcome == PopulateOutcome.SLICE_OVERLAP_VIOLATION

    def test_safe_wrapper_still_maps_forest_reason(self, tmp_path):
        # Regression guard: the default reason still routes to FOREST_VIOLATION.
        from routes.pipelines import (
            ForestValidationError,
            PopulateOutcome,
            _populate_contract_from_plan_safe,
        )

        with patch(
            "routes.pipelines._populate_contract_from_plan",
            side_effect=ForestValidationError("forest", errors=["bad"]),
        ):
            result = _populate_contract_from_plan_safe(tmp_path, "p-forest", "local")
        assert result.outcome == PopulateOutcome.FOREST_VIOLATION


class TestForestValidationErrorResponse:
    def test_to_response_keys_on_reason(self):
        from routes.pipelines import ForestValidationError

        overlap = ForestValidationError("overlap", errors=["e1"], reason="slice_overlap_violation")
        body, status = overlap.to_response()
        assert status == 422
        assert body == {"error": "slice_overlap_violation", "errors": ["e1"]}

    def test_default_reason_preserves_forest_violation_key(self):
        from routes.pipelines import ForestValidationError

        forest = ForestValidationError("forest", errors=["e1"])
        body, _ = forest.to_response()
        assert body["error"] == "forest_violation"


class TestEmptyContractHitlOverlapProse:
    def test_overlap_violation_uses_specific_wording(self):
        from routes.pipelines import _empty_contract_hitl_question

        question = _empty_contract_hitl_question(
            pipeline_id="p-overlap",
            reason="slice_overlap_violation",
            draft_slice_count=None,
            gate="plan_complete",
        )
        assert "overlapping files" in question
        assert "missing, unparseable, or yielded no tasks" not in question
        assert "slice_overlap_violation" in question
