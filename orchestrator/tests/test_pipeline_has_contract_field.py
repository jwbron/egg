"""
Tests for the ``has_contract`` and ``pr_head_sha`` fields on the
``Pipeline`` model.

The tests pin:

- Backward-compatible defaults (``has_contract=True``, ``pr_head_sha=None``).
- Round-trip serialization via ``model_dump``/``model_validate``.
- Legacy JSON (without the new fields) still deserializes.
- The ``pr_number`` field rejects 0 and negative values (``ge=1``).
"""

import pytest
from models import Pipeline, PipelineMode
from pydantic import ValidationError


class TestHasContractDefault:
    """Default ``has_contract=True`` preserves backward compatibility."""

    def test_has_contract_defaults_to_true(self):
        pipeline = Pipeline(id="x", repo="o/r")
        assert pipeline.has_contract is True

    def test_has_contract_false_persists(self):
        pipeline = Pipeline(id="x", repo="o/r", has_contract=False)
        assert pipeline.has_contract is False


class TestHasContractRoundTrip:
    """``has_contract`` survives a ``model_dump`` / ``model_validate`` cycle."""

    def test_round_trip_preserves_true(self):
        original = Pipeline(id="x", repo="o/r", has_contract=True)
        data = original.model_dump()
        assert data["has_contract"] is True
        restored = Pipeline.model_validate(data)
        assert restored.has_contract is True

    def test_round_trip_preserves_false(self):
        original = Pipeline(id="x", repo="o/r", has_contract=False)
        data = original.model_dump()
        assert data["has_contract"] is False
        restored = Pipeline.model_validate(data)
        assert restored.has_contract is False

    def test_legacy_json_without_has_contract_defaults_to_true(self):
        """Legacy state files predating ``has_contract`` must still load."""
        restored = Pipeline.model_validate({"id": "x", "repo": "o/r"})
        assert restored.has_contract is True


class TestPrHeadSha:
    """``pr_head_sha`` defaults to None and round-trips as a string."""

    def test_pr_head_sha_defaults_to_none(self):
        pipeline = Pipeline(id="x", repo="o/r")
        assert pipeline.pr_head_sha is None

    def test_pr_head_sha_accepts_sha_and_round_trips(self):
        sha = "abc123def4567890abc123def4567890abc123de"
        original = Pipeline(id="x", repo="o/r", pr_head_sha=sha)
        assert original.pr_head_sha == sha

        data = original.model_dump()
        assert data["pr_head_sha"] == sha

        restored = Pipeline.model_validate(data)
        assert restored.pr_head_sha == sha


class TestPipelineModeCustomPr:
    """CUSTOM-mode pipelines with a PR target serialise correctly."""

    def test_pipeline_with_custom_mode_and_pr_number_serializes(self):
        pipeline = Pipeline(
            id="x",
            repo="o/r",
            mode=PipelineMode.CUSTOM,
            pr_number=42,
        )
        data = pipeline.model_dump()
        assert data["mode"] == "custom"
        assert data["pr_number"] == 42

        restored = Pipeline.model_validate(data)
        assert restored.mode == PipelineMode.CUSTOM
        assert restored.pr_number == 42


class TestPrNumberConstraint:
    """``pr_number`` must be >= 1 when provided."""

    def test_pr_number_zero_rejected(self):
        with pytest.raises(ValidationError):
            Pipeline(id="x", repo="o/r", pr_number=0)

    def test_pr_number_negative_rejected(self):
        with pytest.raises(ValidationError):
            Pipeline(id="x", repo="o/r", pr_number=-1)
