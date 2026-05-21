"""Tests for ``PipelineMode.CUSTOM`` + ``Pipeline.active_roles`` (#1762).

Phase 1 of the ``run_agent_task`` work added:

1. ``PipelineMode.CUSTOM`` enum value (``orchestrator/models.py``).
2. ``Pipeline.active_roles: list[str] | None`` field with a validator
   that rejects empty lists, unknown AgentRole values, and
   reviewer-only rosters.

This module locks in the model-layer contract. It is intentionally
independent of routes/MCP tools — those land in Phase 2/3 and get their
own test modules.
"""

from __future__ import annotations

import pytest
from models import AgentRole, Pipeline, PipelineMode
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# PipelineMode.CUSTOM enum value
# ---------------------------------------------------------------------------


class TestPipelineModeCustom:
    def test_custom_value(self):
        """The enum value is the literal string ``"custom"`` — persisted
        on the Pipeline JSON and matched by the route handler."""
        assert PipelineMode.CUSTOM.value == "custom"

    def test_custom_is_str_enum(self):
        """StrEnum membership — a serialised PipelineMode.CUSTOM round-trips
        through plain strings."""
        assert str(PipelineMode.CUSTOM) == "custom"
        assert PipelineMode("custom") is PipelineMode.CUSTOM

    def test_issue_still_defined(self):
        """Prior values must still exist."""
        assert PipelineMode.ISSUE.value == "issue"

    def test_all_expected_modes_present(self):
        values = {m.value for m in PipelineMode}
        assert {"issue", "custom"} <= values


# ---------------------------------------------------------------------------
# Default active_roles behaviour (backward compat)
# ---------------------------------------------------------------------------


class TestActiveRolesDefault:
    def test_default_is_none(self):
        """Existing ISSUE-mode pipelines must continue to work with no
        ``active_roles`` set — this is the #1762 backward-compat
        guarantee."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        assert pipeline.active_roles is None

    def test_serialised_default_round_trips(self):
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        payload = pipeline.model_dump(mode="json")
        assert payload["active_roles"] is None
        restored = Pipeline.model_validate(payload)
        assert restored.active_roles is None

    def test_legacy_pipeline_json_without_field_deserialises(self):
        """Pipelines serialised before #1762 shipped don't have
        ``active_roles`` in their JSON. Deserialising must succeed
        and default to None."""
        legacy = {
            "id": "issue-1",
            "issue_number": 1,
            "repo": "owner/repo",
            "branch": "egg/issue-1",
        }
        pipeline = Pipeline.model_validate(legacy)
        assert pipeline.active_roles is None


# ---------------------------------------------------------------------------
# Happy-path active_roles
# ---------------------------------------------------------------------------


class TestActiveRolesValid:
    def test_single_producer_accepted(self):
        pipeline = Pipeline(
            id="custom-abc",
            repo="owner/repo",
            branch="egg/custom-abc",
            mode=PipelineMode.CUSTOM,
            active_roles=[AgentRole.CODER.value],
        )
        assert pipeline.active_roles == ["coder"]

    def test_producer_plus_reviewer_accepted(self):
        pipeline = Pipeline(
            id="custom-abc",
            repo="owner/repo",
            branch="egg/custom-abc",
            mode=PipelineMode.CUSTOM,
            active_roles=[
                AgentRole.CODER.value,
                AgentRole.REVIEWER_CODE.value,
            ],
        )
        assert pipeline.active_roles == ["coder", "reviewer_code"]

    def test_multiple_producers_no_reviewer_accepted(self):
        """The validator only requires one producer — multiple producers
        alone are legal (single-commit short-circuit at consensus time)."""
        pipeline = Pipeline(
            id="custom-abc",
            repo="owner/repo",
            branch="egg/custom-abc",
            mode=PipelineMode.CUSTOM,
            active_roles=[
                AgentRole.CODER.value,
                AgentRole.TESTER.value,
                AgentRole.DOCUMENTER.value,
            ],
        )
        assert set(pipeline.active_roles) == {"coder", "tester", "documenter"}

    def test_active_roles_round_trips_through_json(self):
        roles = [AgentRole.CODER.value, AgentRole.REVIEWER_CODE.value]
        pipeline = Pipeline(
            id="custom-abc",
            repo="owner/repo",
            branch="egg/custom-abc",
            mode=PipelineMode.CUSTOM,
            active_roles=roles,
        )
        as_json = pipeline.model_dump_json()
        restored = Pipeline.model_validate_json(as_json)
        assert restored.active_roles == roles
        assert restored.mode == PipelineMode.CUSTOM


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


class TestActiveRolesValidation:
    def test_empty_list_rejected(self):
        """Empty lists are ambiguous vs. None — always reject at the
        model layer so the route never persists them."""
        with pytest.raises(ValidationError) as exc:
            Pipeline(
                id="custom-abc",
                repo="owner/repo",
                branch="egg/custom-abc",
                mode=PipelineMode.CUSTOM,
                active_roles=[],
            )
        assert "non-empty" in str(exc.value)

    def test_unknown_role_rejected(self):
        with pytest.raises(ValidationError) as exc:
            Pipeline(
                id="custom-abc",
                repo="owner/repo",
                branch="egg/custom-abc",
                mode=PipelineMode.CUSTOM,
                active_roles=["bogus_role"],
            )
        assert "unknown" in str(exc.value).lower() or "agentrole" in str(exc.value).lower()

    def test_multiple_unknown_roles_listed(self):
        """Error message must list every invalid entry, not just the
        first, so operators debugging a bad API call see them all."""
        with pytest.raises(ValidationError) as exc:
            Pipeline(
                id="custom-abc",
                repo="owner/repo",
                branch="egg/custom-abc",
                mode=PipelineMode.CUSTOM,
                active_roles=["bogus_one", "bogus_two", AgentRole.CODER.value],
            )
        msg = str(exc.value)
        assert "bogus_one" in msg
        assert "bogus_two" in msg

    def test_reviewer_only_roster_rejected(self):
        """BRC deadlock guard — at least one non-reviewer role is
        required."""
        with pytest.raises(ValidationError) as exc:
            Pipeline(
                id="custom-abc",
                repo="owner/repo",
                branch="egg/custom-abc",
                mode=PipelineMode.CUSTOM,
                active_roles=[AgentRole.REVIEWER_CODE.value],
            )
        assert "producer" in str(exc.value).lower()

    def test_multiple_reviewers_no_producer_rejected(self):
        with pytest.raises(ValidationError):
            Pipeline(
                id="custom-abc",
                repo="owner/repo",
                branch="egg/custom-abc",
                mode=PipelineMode.CUSTOM,
                active_roles=[
                    AgentRole.REVIEWER_CODE.value,
                    AgentRole.REVIEWER_CONTRACT.value,
                ],
            )

    def test_none_accepted(self):
        """None is explicitly allowed — this is the ISSUE-mode default."""
        p = Pipeline(
            id="issue-1",
            issue_number=1,
            repo="owner/repo",
            branch="egg/issue-1",
            active_roles=None,
        )
        assert p.active_roles is None


# ---------------------------------------------------------------------------
# Field validator fires on assignment, not only on construction
# ---------------------------------------------------------------------------


class TestActiveRolesAssignment:
    """Pydantic v2 ``field_validator`` should fire both on creation and
    on later assignment (``pipeline.active_roles = ...``) when the model
    is configured with ``validate_assignment=True``. If that's not on,
    at least construction must validate — and that's the primary API."""

    def test_cannot_construct_with_bad_value(self):
        with pytest.raises(ValidationError):
            Pipeline(
                id="custom-abc",
                repo="owner/repo",
                branch="egg/custom-abc",
                mode=PipelineMode.CUSTOM,
                active_roles=[AgentRole.REVIEWER_CODE.value],
            )

    def test_custom_mode_with_none_active_roles_is_legal(self):
        """CUSTOM pipelines are usually created with active_roles set,
        but the model does NOT enforce that coupling — the route is
        responsible for filling it. Here we just confirm that the
        combination is legal at the model layer."""
        p = Pipeline(
            id="custom-abc",
            repo="owner/repo",
            branch="egg/custom-abc",
            mode=PipelineMode.CUSTOM,
            active_roles=None,
        )
        assert p.mode == PipelineMode.CUSTOM
        assert p.active_roles is None


# ---------------------------------------------------------------------------
# JSON stability — no new required fields on Pipeline
# ---------------------------------------------------------------------------


class TestSchemaCompatibility:
    def test_active_roles_not_required_in_schema(self):
        """The new field must not become required — every existing
        caller that doesn't supply it must continue to work."""
        schema = Pipeline.model_json_schema()
        required = set(schema.get("required", []))
        assert "active_roles" not in required

    def test_active_roles_accepts_null_in_schema(self):
        schema = Pipeline.model_json_schema()
        props = schema.get("properties", {})
        assert "active_roles" in props
        # Pydantic v2 renders `list[str] | None` as `anyOf` with one
        # branch including {"type": "null"}.
        entry = props["active_roles"]
        any_of = entry.get("anyOf")
        assert any_of is not None, "expected anyOf for Optional[list[str]]"
        has_null = any(sub.get("type") == "null" for sub in any_of)
        assert has_null
