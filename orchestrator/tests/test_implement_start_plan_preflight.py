"""Implement-start plan pre-flight gate (#3100).

``start_phase=implement`` submits never traverse ``advance_phase``, so
the #2777 plan pre-flight (which 422s when the plan draft lacks the
``pr:`` block the context-PR opener needs) never ran on that path: the
pipeline executed the whole implement phase with no context PR and
nothing surfaced beyond WARNING log lines (observed on
pipeline-da68d70c and pipeline-2d9cc50d, Khan/webapp).

These tests cover :func:`_enforce_implement_start_plan_preflight`
directly: remote pipelines with a metadata-less draft fail loud (FAILED
+ dedicated HITL), local-mode pipelines and infra failures pass
through, and a well-formed draft validates silently.  Persistence goes
through :func:`_persist_hitl_decision`, mocked here so we don't need a
live state store.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

# Well-formed draft: parseable yaml-tasks fence + complete pr: block
# (mirrors TestValidatePlanPreflight.HAPPY_PLAN in the parser tests).
HAPPY_PLAN = """# Plan

```yaml
# yaml-tasks
pr:
  title: Add feature X
  description: |
    Adds X to Y.
  test_plan: |
    - Automated: pytest
  manual_steps: ""
phases:
  - id: 1
    name: Setup
    goal: Initialize
    tasks:
      - id: TASK-1-1
        description: Create schema
        acceptance: Schema validates
```
"""

# Same fence with NO pr: block — the pipeline-2d9cc50d shape (#3100).
PLAN_WITHOUT_PR_BLOCK = """# Plan

```yaml
# yaml-tasks
phases:
  - id: 1
    name: Setup
    goal: Initialize
    tasks:
      - id: TASK-1-1
        description: Create schema
        acceptance: Schema validates
```
"""

DRAFT_REL = ".egg-state/drafts/plan.md"


def _write_draft(tmp_path, content: str) -> None:
    draft = tmp_path / DRAFT_REL
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(content)


def _remote_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline.repo = "Khan/webapp"
    pipeline.base_branch = None
    return pipeline


class TestEnforceImplementStartPlanPreflight:
    def test_local_mode_skips_without_reading_draft(self, tmp_path):
        """No repo + no base_branch ⇒ local mode: gate is a no-op.

        The draft file deliberately does not exist — a read attempt
        would surface as the OSError warn-path, so a clean False here
        proves the local-mode short-circuit fires first.
        """
        from routes.pipelines import _enforce_implement_start_plan_preflight

        pipeline = MagicMock()
        pipeline.repo = None
        pipeline.base_branch = None
        assert (
            _enforce_implement_start_plan_preflight(
                "pipeline-1", pipeline, MagicMock(), tmp_path, DRAFT_REL
            )
            is False
        )

    def test_happy_draft_passes(self, tmp_path):
        from routes.pipelines import _enforce_implement_start_plan_preflight

        _write_draft(tmp_path, HAPPY_PLAN)
        store = MagicMock()
        assert (
            _enforce_implement_start_plan_preflight(
                "pipeline-1", _remote_pipeline(), store, tmp_path, DRAFT_REL
            )
            is False
        )
        store.save_pipeline.assert_not_called()

    def test_missing_draft_warns_and_continues(self, tmp_path):
        """Infra failure (unreadable/absent draft) must not hard-fail —
        the populate path's own outcomes cover a missing draft."""
        from routes.pipelines import _enforce_implement_start_plan_preflight

        store = MagicMock()
        assert (
            _enforce_implement_start_plan_preflight(
                "pipeline-1", _remote_pipeline(), store, tmp_path, DRAFT_REL
            )
            is False
        )
        store.save_pipeline.assert_not_called()

    def test_metadata_less_draft_fails_loud(self, tmp_path):
        """The #3100 shape: slices parse fine, pr: block absent ⇒ the
        pipeline is FAILED and the dedicated HITL is emitted."""
        from models import PipelineStatus
        from routes.pipelines import (
            _PLAN_PREFLIGHT_HITL_OPTIONS,
            _enforce_implement_start_plan_preflight,
        )

        _write_draft(tmp_path, PLAN_WITHOUT_PR_BLOCK)
        store = MagicMock()
        disk_pipeline = MagicMock()
        store.load_pipeline.return_value = disk_pipeline

        with (
            patch(
                "routes.pipelines._persist_hitl_decision",
                return_value=MagicMock(),
            ) as mock_persist,
            patch("routes.pipelines.report_pipeline_status") as mock_report,
            patch("routes.pipelines._emit_pipeline_event") as mock_emit,
        ):
            failed = _enforce_implement_start_plan_preflight(
                "pipeline-1", _remote_pipeline(), store, tmp_path, DRAFT_REL
            )

        assert failed is True
        # FAILED persisted on the disk-loaded copy under the state lock.
        assert disk_pipeline.status == PipelineStatus.FAILED
        assert "pr.title" in disk_pipeline.error
        store.save_pipeline.assert_called_once_with(disk_pipeline)
        # Dedicated HITL with the recovery option set, not generic Retry.
        assert mock_persist.call_count == 1
        call = mock_persist.call_args
        assert call.args == ("pipeline-1", disk_pipeline, store)
        assert call.kwargs["options"] == list(_PLAN_PREFLIGHT_HITL_OPTIONS)
        question = call.kwargs["question"]
        assert "pr.title" in question
        assert DRAFT_REL in question
        assert "restart_phase implement" in question
        # Failure surfaced on the status/event channels.
        assert mock_report.call_count == 1
        assert mock_report.call_args.kwargs["event_type"] == "pipeline.failed"
        mock_emit.assert_called_once_with(disk_pipeline, "pipeline.failed")

    def test_base_branch_only_pipeline_is_remote(self, tmp_path):
        """A pipeline with base_branch set but repo unset is NOT local
        mode (the opener treats it as a misconfiguration, not a skip),
        so the gate still enforces."""
        from routes.pipelines import _enforce_implement_start_plan_preflight

        _write_draft(tmp_path, PLAN_WITHOUT_PR_BLOCK)
        pipeline = MagicMock()
        pipeline.repo = None
        pipeline.base_branch = "release-branch"
        store = MagicMock()
        store.load_pipeline.return_value = MagicMock()

        with (
            patch("routes.pipelines._persist_hitl_decision", return_value=MagicMock()),
            patch("routes.pipelines.report_pipeline_status"),
            patch("routes.pipelines._emit_pipeline_event"),
        ):
            assert (
                _enforce_implement_start_plan_preflight(
                    "pipeline-1", pipeline, store, tmp_path, DRAFT_REL
                )
                is True
            )


class TestPlanPreflightHitlQuestion:
    def test_names_fields_and_recovery_actions(self):
        from routes.pipelines import (
            _PLAN_PREFLIGHT_HITL_OPTIONS,
            _plan_preflight_hitl_question,
        )

        question = _plan_preflight_hitl_question(
            missing_fields=["pr.title", "pr.description"],
            plan_draft_rel=DRAFT_REL,
        )
        assert "pr.title, pr.description" in question
        assert DRAFT_REL in question
        # Every offered option is explained inline so the operator
        # doesn't have to guess what each choice does.
        for option in _PLAN_PREFLIGHT_HITL_OPTIONS:
            assert f"'{option}'" in question
