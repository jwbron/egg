"""Tests for agent anchor schema and size validation."""

import json
from datetime import datetime, timezone

import pytest

from egg_anchor.constants import (
    ANCHOR_HARD_LIMIT_BYTES,
    ANCHOR_SOFT_LIMIT_BYTES,
    ANCHOR_TEAM_HARD_LIMIT_BYTES,
    ANCHOR_TEAM_SOFT_LIMIT_BYTES,
)
from egg_anchor.models import AgentAnchor
from egg_anchor.validator import SizeBudgetResult, check_size_budget, validate_anchor


def _make_anchor_dict(**overrides):
    """Create a minimal valid anchor dict."""
    now = datetime(2026, 3, 17, 10, 0, 0, tzinfo=timezone.utc).isoformat()
    defaults = {
        "_meta": {
            "schema_version": "1.0",
            "created_at": now,
            "updated_at": now,
            "sequence": 1,
        },
        "agent_id": "coder-abc123",
        "role": "coder",
        "team": ["tester-def456"],
        "task": {
            "id": "task-1",
            "description": "Implement feature X",
            "phase": "implement",
        },
        "status": "working",
        "pipeline_id": "pipeline-xyz",
        "progress": [],
        "decisions": [],
        "brc_state": {"phase": "orient", "acks": [], "nacks": []},
        "key_context": [],
        "errors_encountered": [],
        "files_modified": [],
    }
    defaults.update(overrides)
    return defaults


def _make_anchor(**overrides):
    """Create a minimal valid AgentAnchor model."""
    return AgentAnchor.model_validate(_make_anchor_dict(**overrides))


class TestValidateAnchor:
    """Test JSON Schema validation."""

    def test_valid_anchor_no_errors(self):
        data = _make_anchor_dict()
        errors = validate_anchor(data)
        assert errors == []

    def test_valid_anchor_model_no_errors(self):
        anchor = _make_anchor()
        errors = validate_anchor(anchor)
        assert errors == []

    def test_missing_required_field(self):
        data = _make_anchor_dict()
        del data["agent_id"]
        errors = validate_anchor(data)
        assert len(errors) > 0
        assert any("agent_id" in e for e in errors)

    def test_invalid_status_enum(self):
        data = _make_anchor_dict()
        data["status"] = "invalid_status"
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_invalid_brc_phase(self):
        data = _make_anchor_dict()
        data["brc_state"]["phase"] = "invalid_phase"
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_invalid_progress_state(self):
        now = datetime.now(tz=timezone.utc).isoformat()
        data = _make_anchor_dict(
            progress=[{"step": "test", "state": "invalid", "timestamp": now}]
        )
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_too_many_progress_items(self):
        now = datetime.now(tz=timezone.utc).isoformat()
        data = _make_anchor_dict(
            progress=[
                {"step": f"s{i}", "state": "pending", "timestamp": now}
                for i in range(11)
            ]
        )
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_too_many_decisions(self):
        now = datetime.now(tz=timezone.utc).isoformat()
        data = _make_anchor_dict(
            decisions=[
                {"id": f"d-{i}", "question": f"Q{i}?", "timestamp": now}
                for i in range(9)
            ]
        )
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_too_many_files(self):
        data = _make_anchor_dict(
            files_modified=[f"file{i}.py" for i in range(16)]
        )
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_key_context_label_too_long(self):
        data = _make_anchor_dict(
            key_context=[{"label": "x" * 51, "value": "ok"}]
        )
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_key_context_value_too_long(self):
        data = _make_anchor_dict(
            key_context=[{"label": "ok", "value": "x" * 501}]
        )
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_additional_properties_rejected(self):
        data = _make_anchor_dict()
        data["unknown_field"] = "should fail"
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_fully_populated_valid_anchor(self):
        now = datetime.now(tz=timezone.utc).isoformat()
        data = _make_anchor_dict(
            status="proposed",
            progress=[
                {"step": "implement", "state": "complete", "timestamp": now},
            ],
            decisions=[
                {
                    "id": "d-1",
                    "question": "Use Redis?",
                    "answer": "Yes",
                    "decided_by": "human",
                    "timestamp": now,
                }
            ],
            brc_state={
                "phase": "proposed",
                "proposed_at": now,
                "acks": ["tester-1"],
                "nacks": [],
                "last_message_id": "msg-1",
            },
            key_context=[{"label": "branch", "value": "egg/test"}],
            errors_encountered=[
                {"error": "flake", "resolution": "retry", "timestamp": now}
            ],
            files_modified=["models.py"],
        )
        errors = validate_anchor(data)
        assert errors == []


class TestCheckSizeBudget:
    """Test size budget checking."""

    def test_small_anchor_within_budget(self):
        anchor = _make_anchor()
        result = check_size_budget(anchor)
        assert result.within_budget is True
        assert result.size_bytes > 0
        assert result.warnings == []
        assert result.errors == []

    def test_uses_individual_limits_by_default(self):
        anchor = _make_anchor()
        result = check_size_budget(anchor)
        assert result.soft_limit == ANCHOR_SOFT_LIMIT_BYTES
        assert result.hard_limit == ANCHOR_HARD_LIMIT_BYTES

    def test_uses_team_limits_when_specified(self):
        anchor = _make_anchor()
        result = check_size_budget(anchor, is_team=True)
        assert result.soft_limit == ANCHOR_TEAM_SOFT_LIMIT_BYTES
        assert result.hard_limit == ANCHOR_TEAM_HARD_LIMIT_BYTES

    def test_exceeds_soft_limit_warns(self):
        """Anchor above soft limit should have warning but be within budget."""
        data = _make_anchor_dict(
            key_context=[
                {"label": f"key{i}", "value": "x" * 400}
                for i in range(5)
            ],
        )
        result = check_size_budget(data)
        assert result.within_budget is True
        assert len(result.warnings) > 0
        assert "soft limit" in result.warnings[0]

    def test_exceeds_hard_limit_fails(self):
        """Anchor above hard limit should not be within budget."""
        now = datetime.now(tz=timezone.utc).isoformat()
        data = _make_anchor_dict(
            key_context=[
                {"label": f"key{i}", "value": "x" * 500}
                for i in range(5)
            ],
            progress=[
                {"step": "x" * 100, "state": "pending", "detail": "x" * 100, "timestamp": now}
                for _ in range(10)
            ],
            files_modified=[f"very/long/path/to/file_{i}.py" for i in range(15)],
        )
        result = check_size_budget(data)
        assert result.within_budget is False
        assert len(result.errors) > 0
        assert "hard limit" in result.errors[0]

    def test_accepts_dict_input(self):
        data = _make_anchor_dict()
        result = check_size_budget(data)
        assert result.within_budget is True

    def test_accepts_model_input(self):
        anchor = _make_anchor()
        result = check_size_budget(anchor)
        assert result.within_budget is True

    def test_size_bytes_matches_compact_json(self):
        """Size should be calculated from compact JSON (no spaces)."""
        data = _make_anchor_dict()
        result = check_size_budget(data)
        expected = len(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        assert result.size_bytes == expected


class TestSizeBudgetResult:
    """Test the SizeBudgetResult data class."""

    def test_defaults(self):
        result = SizeBudgetResult(
            within_budget=True,
            size_bytes=100,
            soft_limit=2048,
            hard_limit=3072,
        )
        assert result.warnings == []
        assert result.errors == []

    def test_with_warnings(self):
        result = SizeBudgetResult(
            within_budget=True,
            size_bytes=2500,
            soft_limit=2048,
            hard_limit=3072,
            warnings=["over soft limit"],
        )
        assert len(result.warnings) == 1

    def test_with_errors(self):
        result = SizeBudgetResult(
            within_budget=False,
            size_bytes=4000,
            soft_limit=2048,
            hard_limit=3072,
            errors=["over hard limit"],
        )
        assert not result.within_budget
        assert len(result.errors) == 1


# === GAP TESTS: Additional validation edge cases ===


class TestValidateAnchorGaps:
    """Gap tests for validation edge cases."""

    def test_missing_meta_field(self):
        """Missing _meta entirely should fail."""
        data = _make_anchor_dict()
        del data["_meta"]
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_missing_task_field(self):
        """Missing task should fail."""
        data = _make_anchor_dict()
        del data["task"]
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_missing_pipeline_id(self):
        """Missing pipeline_id should fail."""
        data = _make_anchor_dict()
        del data["pipeline_id"]
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_missing_brc_state_required_fields(self):
        """BRC state missing required acks/nacks should fail."""
        data = _make_anchor_dict()
        data["brc_state"] = {"phase": "orient"}  # Missing acks, nacks
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_meta_missing_sequence(self):
        """Meta missing sequence should fail."""
        data = _make_anchor_dict()
        del data["_meta"]["sequence"]
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_nested_additional_properties(self):
        """Additional properties in nested objects should fail."""
        data = _make_anchor_dict()
        data["_meta"]["unknown_field"] = "should fail"
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_error_description_at_max_length(self):
        """Error description at exactly max length (200) should pass."""
        now = datetime.now(tz=timezone.utc).isoformat()
        data = _make_anchor_dict(
            errors_encountered=[
                {"error": "x" * 200, "timestamp": now}
            ]
        )
        errors = validate_anchor(data)
        assert errors == []

    def test_error_description_over_max_length(self):
        """Error description over max length (200) should fail."""
        now = datetime.now(tz=timezone.utc).isoformat()
        data = _make_anchor_dict(
            errors_encountered=[
                {"error": "x" * 201, "timestamp": now}
            ]
        )
        errors = validate_anchor(data)
        assert len(errors) > 0

    def test_empty_anchor_validates(self):
        """Minimal anchor with all empty arrays should validate."""
        data = _make_anchor_dict()
        errors = validate_anchor(data)
        assert errors == []


class TestSizeBudgetGaps:
    """Gap tests for size budget edge cases."""

    def test_team_hard_limit_larger_than_individual(self):
        """Team hard limit should be larger than individual."""
        assert ANCHOR_TEAM_HARD_LIMIT_BYTES > ANCHOR_HARD_LIMIT_BYTES

    def test_team_soft_limit_larger_than_individual(self):
        """Team soft limit should be larger than individual."""
        assert ANCHOR_TEAM_SOFT_LIMIT_BYTES > ANCHOR_SOFT_LIMIT_BYTES

    def test_soft_limit_less_than_hard_limit(self):
        """Soft limit should be less than hard limit."""
        assert ANCHOR_SOFT_LIMIT_BYTES < ANCHOR_HARD_LIMIT_BYTES
        assert ANCHOR_TEAM_SOFT_LIMIT_BYTES < ANCHOR_TEAM_HARD_LIMIT_BYTES
