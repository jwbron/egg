"""Tests for _enrich_pending_decisions in PipelineToolHandler."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies before importing
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from mcp_tools import PipelineToolHandler


@pytest.fixture
def handler():
    return PipelineToolHandler(orchestrator_url="http://localhost:9849")


@pytest.fixture
def worktree(tmp_path: Path):
    """Create a worktree with analysis and plan drafts."""
    drafts = tmp_path / ".egg-state" / "drafts"
    drafts.mkdir(parents=True)
    (drafts / "42-analysis.md").write_text("# Analysis\nOptions A-E", encoding="utf-8")
    (drafts / "42-plan.md").write_text("# Plan\nTask breakdown", encoding="utf-8")
    return tmp_path


def _make_status(decisions: list[dict], current_phase: str = "refine") -> dict:
    return {
        "current_phase": current_phase,
        "status": "awaiting_human",
        "pending_decisions": decisions,
        "completed_agents": [
            {"role": "refiner", "status": "complete"},
        ],
    }


def _make_pipeline_data(repo: str = "jwbron/egg", issue_number: int = 42) -> dict:
    return {"repo": repo, "issue_number": issue_number}


class TestEnrichPendingDecisions:
    """Tests for broadened decision enrichment."""

    def test_choice_from_refine_gets_draft_content(self, handler, worktree):
        decisions = [{"decision_type": "choice", "phase": "refine", "question": "Pick option"}]
        status = _make_status(decisions)

        with patch("orchestrator.routes.resolve_worktree_path", return_value=worktree):
            handler._enrich_pending_decisions(status, "pipeline-1", _make_pipeline_data())

        assert status["pending_decisions"][0]["draft_content"] == "# Analysis\nOptions A-E"

    def test_feedback_from_plan_gets_draft_content(self, handler, worktree):
        decisions = [{"decision_type": "feedback", "phase": "plan", "question": "Review plan"}]
        status = _make_status(decisions, current_phase="plan")

        with patch("orchestrator.routes.resolve_worktree_path", return_value=worktree):
            handler._enrich_pending_decisions(status, "pipeline-1", _make_pipeline_data())

        assert status["pending_decisions"][0]["draft_content"] == "# Plan\nTask breakdown"

    def test_choice_from_implement_no_draft(self, handler, worktree):
        decisions = [{"decision_type": "choice", "phase": "implement", "question": "Pick"}]
        status = _make_status(decisions, current_phase="implement")

        with patch("orchestrator.routes.resolve_worktree_path", return_value=worktree):
            handler._enrich_pending_decisions(status, "pipeline-1", _make_pipeline_data())

        assert "draft_content" not in status["pending_decisions"][0]

    def test_phase_gate_gets_full_enrichment(self, handler, worktree):
        # Set up reviewer feedback
        reviews = worktree / ".egg-state" / "reviews"
        reviews.mkdir(parents=True)
        (reviews / "42-refine-refiner-review.json").write_text(
            json.dumps({"verdict": "approved", "summary": "LGTM"}),
            encoding="utf-8",
        )

        decisions = [{"decision_type": "phase_gate", "phase": "refine", "question": "Approve?"}]
        status = _make_status(decisions)

        with patch("orchestrator.routes.resolve_worktree_path", return_value=worktree):
            handler._enrich_pending_decisions(status, "pipeline-1", _make_pipeline_data())

        d = status["pending_decisions"][0]
        assert d["draft_content"] == "# Analysis\nOptions A-E"
        assert d["completed_agents_summary"] == [{"role": "refiner", "status": "complete"}]
        assert len(d["reviewer_feedback"]) == 1

    def test_choice_does_not_get_agents_or_reviewer_feedback(self, handler, worktree):
        decisions = [{"decision_type": "choice", "phase": "refine", "question": "Pick"}]
        status = _make_status(decisions)

        with patch("orchestrator.routes.resolve_worktree_path", return_value=worktree):
            handler._enrich_pending_decisions(status, "pipeline-1", _make_pipeline_data())

        d = status["pending_decisions"][0]
        assert "draft_content" in d
        assert "completed_agents_summary" not in d
        assert "reviewer_feedback" not in d

    def test_no_pending_decisions_is_noop(self, handler):
        status = _make_status([])

        handler._enrich_pending_decisions(status, "pipeline-1", _make_pipeline_data())

        assert status["pending_decisions"] == []

    def test_mixed_decision_types(self, handler, worktree):
        """Both phase_gate and choice get draft_content; only phase_gate gets extras."""
        decisions = [
            {"decision_type": "phase_gate", "phase": "refine", "question": "Approve?"},
            {"decision_type": "choice", "phase": "refine", "question": "Pick option"},
        ]
        status = _make_status(decisions)

        with patch("orchestrator.routes.resolve_worktree_path", return_value=worktree):
            handler._enrich_pending_decisions(status, "pipeline-1", _make_pipeline_data())

        gate = status["pending_decisions"][0]
        choice = status["pending_decisions"][1]

        assert gate["draft_content"] == "# Analysis\nOptions A-E"
        assert "completed_agents_summary" in gate
        assert choice["draft_content"] == "# Analysis\nOptions A-E"
        assert "completed_agents_summary" not in choice
