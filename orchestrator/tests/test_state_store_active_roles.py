"""Tests for ``StateStore.create_pipeline(active_roles=...)`` (#1762).

Phase 1 threaded a new ``active_roles`` kwarg through the state-store
pipeline factory so that the Phase 2 route can persist the resolved
role roster on the Pipeline record without directly instantiating the
model.

This module checks:
  - The kwarg is optional (backward compat).
  - When supplied, it round-trips through save/load.
  - When omitted/None, the pipeline has ``active_roles=None``.
  - Invalid values fail at pipeline-construction time (not silently
    discarded).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from models import AgentRole, PipelineMode
from pydantic import ValidationError
from state_store import StateStore


@pytest.fixture
def mock_git():
    with patch.object(StateStore, "_run_git") as mock:
        mock.return_value = MagicMock(stdout="abc1234\n", returncode=0)
        yield mock


@pytest.fixture
def state_store(tmp_path, mock_git):
    store = StateStore(tmp_path, worktree_dir=tmp_path)
    store._worktree = tmp_path
    return store


# ---------------------------------------------------------------------------
# Default behaviour: kwarg optional, legacy callers unaffected
# ---------------------------------------------------------------------------


class TestActiveRolesDefault:
    def test_omitted_kwarg_results_in_none(self, state_store):
        pipeline = state_store.create_pipeline(
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        assert pipeline.active_roles is None

    def test_explicit_none_results_in_none(self, state_store):
        pipeline = state_store.create_pipeline(
            issue_number=497,
            repo="owner/repo",
            branch="egg/issue-497",
            active_roles=None,
        )
        assert pipeline.active_roles is None


# ---------------------------------------------------------------------------
# Happy path: kwarg stored on returned pipeline and persisted to disk
# ---------------------------------------------------------------------------


class TestActiveRolesPersistence:
    def test_set_active_roles_is_on_returned_pipeline(self, state_store):
        pipeline = state_store.create_pipeline(
            pipeline_id="pipeline-aabbccdd",
            repo="owner/repo",
            branch="egg/custom-aabbccdd",
            mode=PipelineMode.CUSTOM,
            active_roles=[AgentRole.CODER.value],
        )
        assert pipeline.active_roles == ["coder"]
        assert pipeline.mode == PipelineMode.CUSTOM

    def test_active_roles_persisted_and_reloaded(self, state_store):
        state_store.create_pipeline(
            pipeline_id="pipeline-deadbeef",
            repo="owner/repo",
            branch="egg/custom-deadbeef",
            mode=PipelineMode.CUSTOM,
            active_roles=[
                AgentRole.CODER.value,
                AgentRole.REVIEWER_CODE.value,
            ],
        )
        reloaded = state_store.load_pipeline("pipeline-deadbeef")
        assert reloaded.active_roles == ["coder", "reviewer_code"]
        assert reloaded.mode == PipelineMode.CUSTOM

    def test_issue_mode_with_active_roles_also_works(self, state_store):
        """The kwarg is not coupled to PipelineMode.CUSTOM — future
        callers can persist active_roles on non-CUSTOM pipelines too."""
        pipeline = state_store.create_pipeline(
            issue_number=998,
            repo="owner/repo",
            branch="egg/issue-998",
            active_roles=[
                AgentRole.CODER.value,
                AgentRole.TESTER.value,
            ],
        )
        assert pipeline.active_roles == ["coder", "tester"]


# ---------------------------------------------------------------------------
# Validation errors surface from the Pipeline model
# ---------------------------------------------------------------------------


class TestActiveRolesValidation:
    def test_empty_list_raises_validation_error(self, state_store):
        with pytest.raises(ValidationError):
            state_store.create_pipeline(
                pipeline_id="pipeline-00000001",
                repo="owner/repo",
                branch="egg/custom-00000001",
                mode=PipelineMode.CUSTOM,
                active_roles=[],
            )

    def test_reviewer_only_raises_validation_error(self, state_store):
        with pytest.raises(ValidationError):
            state_store.create_pipeline(
                pipeline_id="pipeline-00000002",
                repo="owner/repo",
                branch="egg/custom-00000002",
                mode=PipelineMode.CUSTOM,
                active_roles=[AgentRole.REVIEWER_CODE.value],
            )

    def test_unknown_role_raises_validation_error(self, state_store):
        with pytest.raises(ValidationError):
            state_store.create_pipeline(
                pipeline_id="pipeline-00000003",
                repo="owner/repo",
                branch="egg/custom-00000003",
                mode=PipelineMode.CUSTOM,
                active_roles=["nope"],
            )
