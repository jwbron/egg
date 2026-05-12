"""
Tests for pipeline prompt builder functions.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import (
    _build_agent_prompt,
    _build_agent_roster,
    _build_brc_preamble,
    _build_file_boundary_section,
    _build_phase_prompt,
    _build_producer_orientation,
    _build_review_prompt,
    _build_reviewer_preparation,
    _build_role_context,
    _build_role_restrictions_section,
    _extract_plan_overview,
    _get_agent_design_criteria,
    _get_code_review_criteria,
    _get_contract_review_criteria,
    _get_plan_review_criteria,
    _get_reviewer_scope_preamble,
    _read_shared_criteria,
    _read_tester_gaps,
    _render_contract_tasks,
    _summarize_issue,
    _synthesize_plan_draft,
)


class TestReadSharedCriteria:
    """Tests for _read_shared_criteria helper function."""

    def test_reads_from_source_tree(self):
        """Reads criteria from shared/prompts/ in the source tree."""
        content = _read_shared_criteria("code-review-criteria.md")
        assert content is not None
        assert "Security" in content

    def test_returns_none_for_missing_file(self):
        """Returns None when no file is found."""
        content = _read_shared_criteria("nonexistent-file.md")
        assert content is None

    def test_user_override_takes_priority(self):
        """User override file takes priority over shared file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            egg_dir = Path(tmpdir) / ".egg"
            egg_dir.mkdir()
            (egg_dir / "review-rules.md").write_text("## Custom Override\nCustom content.")

            content = _read_shared_criteria(
                "code-review-criteria.md",
                user_override="review-rules.md",
                repo_path=tmpdir,
            )
            assert content is not None
            assert "Custom Override" in content
            assert "Custom content" in content

    def test_shared_file_when_no_override(self):
        """Falls through to shared file when no user override exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _read_shared_criteria(
                "code-review-criteria.md",
                user_override="review-rules.md",
                repo_path=tmpdir,
            )
            assert content is not None
            # Should contain shared file content, not None
            assert "Security" in content


class TestGetCodeReviewCriteria:
    """Tests for _get_code_review_criteria with shared file loading."""

    def test_returns_criteria_content(self):
        """Returns review criteria content."""
        result = _get_code_review_criteria()
        assert "Security" in result

    def test_loads_from_shared_file(self):
        """Criteria loads from shared/prompts/code-review-criteria.md."""
        result = _get_code_review_criteria()
        # Shared file has enriched content compared to inline fallback
        assert "How to Review" in result

    def test_user_override(self):
        """Supports .egg/review-rules.md user override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            egg_dir = Path(tmpdir) / ".egg"
            egg_dir.mkdir()
            (egg_dir / "review-rules.md").write_text("## My Custom Rules")

            result = _get_code_review_criteria(repo_path=tmpdir)
            assert "My Custom Rules" in result

    def test_inline_fallback(self):
        """Falls back to inline criteria when shared file is missing."""
        # Patch _read_shared_criteria to return None (simulating missing files)
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            result = _get_code_review_criteria()
            assert "Security" in result
            assert "Correctness" in result
            assert "### Skip" in result


class TestGetAgentDesignCriteria:
    """Tests for _get_agent_design_criteria with shared file loading."""

    def test_returns_criteria_content(self):
        """Returns agent-design criteria content."""
        result = _get_agent_design_criteria()
        assert "pre-fetching" in result.lower()

    def test_loads_from_shared_file(self):
        """Criteria loads from shared/prompts/agent-design-criteria.md."""
        result = _get_agent_design_criteria()
        # Shared file has enriched content including review philosophy
        assert "guidelines, not absolute rules" in result

    def test_inline_fallback(self):
        """Falls back to inline criteria when shared file is missing."""
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            result = _get_agent_design_criteria()
            assert "Excessive pre-fetching" in result
            assert "Rigid procedures" in result


class TestGetContractReviewCriteria:
    """Tests for _get_contract_review_criteria with shared file loading."""

    def test_returns_criteria_content(self):
        """Returns contract review criteria content."""
        result = _get_contract_review_criteria()
        assert "Task Verification" in result

    def test_user_override(self):
        """Supports .egg/contract-rules.md user override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            egg_dir = Path(tmpdir) / ".egg"
            egg_dir.mkdir()
            (egg_dir / "contract-rules.md").write_text("## Custom Contract Rules")

            result = _get_contract_review_criteria(repo_path=tmpdir)
            assert "Custom Contract Rules" in result

    def test_inline_fallback(self):
        """Falls back to inline criteria when shared file is missing."""
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            result = _get_contract_review_criteria()
            assert "Task Verification" in result
            assert "Contract Integrity" in result


class TestReadSharedCriteriaEdgeCases:
    """Additional edge case tests for _read_shared_criteria."""

    def test_user_override_without_repo_path_ignored(self):
        """User override is ignored when repo_path is None."""
        # Even with user_override name, if no repo_path, skip override
        content = _read_shared_criteria(
            "code-review-criteria.md",
            user_override="review-rules.md",
            repo_path=None,
        )
        # Should still return content from shared file
        assert content is not None
        assert "Security" in content

    def test_user_override_without_egg_dir(self):
        """Falls through when .egg dir doesn't exist in repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # No .egg directory created
            content = _read_shared_criteria(
                "code-review-criteria.md",
                user_override="review-rules.md",
                repo_path=tmpdir,
            )
            # Should fall through to shared file
            assert content is not None
            assert "Security" in content

    def test_reads_all_shared_prompt_files(self):
        """All four shared prompt files are readable."""
        for filename in [
            "agent-design-criteria.md",
            "autofixer-rules.md",
            "code-review-criteria.md",
            "contract-review-criteria.md",
        ]:
            content = _read_shared_criteria(filename)
            assert content is not None, f"Failed to read {filename}"
            assert len(content) > 0, f"Empty content for {filename}"

    def test_docker_path_fallback(self):
        """Falls through to Docker path when source tree path doesn't exist."""
        docker_content = "## Docker Path Content"
        original_is_file = Path.is_file
        original_read_text = Path.read_text

        def mock_is_file(self):
            s = str(self)
            # Block source tree match for our test file
            if s.endswith("shared/prompts/docker-only-test.md"):
                return False
            # Simulate Docker path existing
            if s == "/app/prompts/docker-only-test.md":
                return True
            return original_is_file(self)

        def mock_read_text(self, *args, **kwargs):
            if str(self) == "/app/prompts/docker-only-test.md":
                return docker_content
            return original_read_text(self, *args, **kwargs)

        with (
            patch.object(Path, "is_file", mock_is_file),
            patch.object(Path, "read_text", mock_read_text),
        ):
            content = _read_shared_criteria("docker-only-test.md")
            assert content == docker_content

    def test_empty_override_file_is_ignored(self):
        """An existing but empty override file is skipped (falls through to shared)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            egg_dir = Path(tmpdir) / ".egg"
            egg_dir.mkdir()
            (egg_dir / "review-rules.md").write_text("")

            content = _read_shared_criteria(
                "code-review-criteria.md",
                user_override="review-rules.md",
                repo_path=tmpdir,
            )
            # Empty override is treated as absent; falls through to shared file
            assert content is not None
            assert "Security" in content


class TestSharedPromptFileContent:
    """Tests that shared prompt files contain expected content and structure."""

    def test_agent_design_criteria_has_key_sections(self):
        """agent-design-criteria.md has expected sections."""
        content = _read_shared_criteria("agent-design-criteria.md")
        assert content is not None
        assert "## Review Philosophy" in content
        assert "## What to Look For" in content
        assert "## What to Skip" in content

    def test_code_review_criteria_has_key_sections(self):
        """code-review-criteria.md has expected sections."""
        content = _read_shared_criteria("code-review-criteria.md")
        assert content is not None
        assert "Security" in content
        assert "Correctness" in content
        assert "Robustness" in content
        assert "How to Review" in content

    def test_contract_review_criteria_has_key_sections(self):
        """contract-review-criteria.md has expected sections."""
        content = _read_shared_criteria("contract-review-criteria.md")
        assert content is not None
        assert "Task Verification" in content
        assert "Phase Consistency" in content
        assert "Contract Integrity" in content

    def test_autofixer_rules_has_key_sections(self):
        """autofixer-rules.md has expected sections."""
        content = _read_shared_criteria("autofixer-rules.md")
        assert content is not None
        assert "Auto-fixable" in content
        assert "Report only" in content

    def test_shared_files_are_format_agnostic(self):
        """Shared prompt files don't contain output-format-specific content."""
        for filename in [
            "agent-design-criteria.md",
            "autofixer-rules.md",
            "code-review-criteria.md",
            "contract-review-criteria.md",
        ]:
            content = _read_shared_criteria(filename)
            assert content is not None
            # Should not contain gh commands (format-agnostic per the HTML comments)
            assert "gh pr review" not in content, f"{filename} contains gh pr review"
            assert "GITHUB_OUTPUT" not in content, f"{filename} contains GITHUB_OUTPUT"


class TestBuildPhasePromptPlanEmbedding:
    """Tests for plan/analysis text embedding in implement phase prompts."""

    def test_plan_embedded_when_repo_path_and_draft_exist(self):
        """Plan text appears in prompt when repo_path is set and draft exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a plan draft
            drafts_dir = Path(tmpdir) / ".egg-state" / "drafts"
            drafts_dir.mkdir(parents=True)
            (drafts_dir / "test-pid-plan.md").write_text("## Implementation Plan\nDo the thing.")

            result = _build_phase_prompt(
                phase="implement",
                pipeline_id="test-pid",
                pipeline_mode="issue",
                prompt="Build a widget",
                repo_path=tmpdir,
            )
            assert "## Plan" in result
            assert "Implementation Plan" in result
            assert "Do the thing." in result
            # Should NOT contain the file-I/O instruction
            assert "Review the plan (check `.egg-state/drafts/`)" not in result

    def test_fallback_when_repo_path_is_none(self):
        """Falls back to file-I/O instruction when repo_path is None."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget",
            repo_path=None,
        )
        assert "Review the plan (check `.egg-state/drafts/`)" in result
        assert "## Plan\n" not in result

    def test_fallback_when_draft_file_missing(self):
        """Falls back to file-I/O instruction when draft file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _build_phase_prompt(
                phase="implement",
                pipeline_id="test-pid",
                pipeline_mode="issue",
                prompt="Build a widget",
                repo_path=tmpdir,
            )
            # Draft doesn't exist, should fall back
            assert "Review the plan (check `.egg-state/drafts/`)" in result


class TestBuildPhasePromptRevisionMode:
    """Tests for delta-focused revision prompts on cycle 2+."""

    def test_revision_cycle_omits_plan(self):
        """Cycle 2+ omits plan embedding."""
        with tempfile.TemporaryDirectory() as tmpdir:
            drafts_dir = Path(tmpdir) / ".egg-state" / "drafts"
            drafts_dir.mkdir(parents=True)
            (drafts_dir / "test-pid-plan.md").write_text("## Plan\nBig plan.")

            result = _build_phase_prompt(
                phase="implement",
                pipeline_id="test-pid",
                pipeline_mode="issue",
                prompt="Build a widget",
                repo_path=tmpdir,
                review_cycle=1,
                review_feedback="Fix the naming convention",
            )
            # Plan should NOT be embedded on revision cycles
            assert "Big plan." not in result
            assert "## Plan\n" not in result

    def test_revision_cycle_omits_task_description(self):
        """Cycle 2+ omits the full task description."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget with many features",
            review_cycle=1,
            review_feedback="Fix naming",
        )
        assert "## Task Description" not in result
        assert "Build a widget with many features" not in result
        assert "## Parallel Execution with Subagents" not in result

    def test_revision_cycle_contains_revision_instructions(self):
        """Cycle 2+ contains revision-focused instructions."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget",
            review_cycle=1,
            review_feedback="Fix the naming convention",
        )
        assert "Revision Instructions" in result
        assert "git diff" in result
        assert "Fix the specific issues" in result

    def test_revision_cycle_includes_review_feedback(self):
        """Cycle 2+ still includes the review feedback."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget",
            review_cycle=1,
            review_feedback="Variable naming is inconsistent",
        )
        assert "Variable naming is inconsistent" in result
        assert "Prior Review Feedback" in result
        # consensus_override is gated to review_cycle == 0 only (HITL path);
        # regular revision cycles should NOT include it.
        assert "consensus is superseded" not in result.lower()

    def test_cycle_0_includes_task_description(self):
        """Cycle 0 still includes the full task description."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget with many features",
            review_cycle=0,
        )
        assert "## Task Description" in result
        assert "Build a widget with many features" in result
        assert "## Parallel Execution with Subagents" in result

    def test_revision_cycle_without_feedback_includes_task_description(self):
        """Cycle 2+ with no feedback falls back to including the task description."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget with many features",
            review_cycle=1,
            review_feedback=None,
        )
        # Without feedback the revision prompt should include the task
        # description as a fallback so the coder isn't left with an
        # empty prompt.
        assert "## Task Description" in result
        assert "Build a widget with many features" in result
        # Revision instructions should still be present
        assert "Revision Instructions" in result
        # Should NOT reference the "Prior Review Feedback" section
        # since that section is only emitted when feedback is provided.
        assert "Prior Review Feedback" not in result
        # Should use the no-feedback alternative instructions
        assert "no specific feedback was provided" in result

    def test_cycle_0_with_feedback_includes_review_feedback(self):
        """Cycle 0 with HITL-reset feedback must surface it to the coder/refiner.

        Regression for #1915: when a human rejects a phase_gate with
        change_approach/request_changes, the inline handler resets
        review_cycles to 0 and stores feedback in hitl_feedback, which
        flows back as review_feedback. The producer prompt must include
        that feedback — otherwise the refiner re-proposes the same draft.
        """
        result = _build_phase_prompt(
            phase="refine",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Analyze the issue",
            review_cycle=0,
            review_feedback="Reframe as a three-phase migration plan, not a single cutover.",
        )
        assert "Prior Review Feedback" in result
        assert "Reframe as a three-phase migration plan" in result
        # Heading should omit the cycle number on HITL cycle 0
        assert "Cycle 0" not in result
        # Refine phase uses "in-place" draft language
        assert "in-place" in result
        # Preamble should say "draft" for refine phase
        assert "previous draft" in result
        # Must explicitly override prior-consensus inference so the refiner
        # doesn't see an existing draft and short-circuit to re-confirming it.
        assert "consensus is superseded" in result.lower()

    def test_cycle_0_implement_with_feedback_includes_review_feedback(self):
        """Implement phase cycle 0 with HITL feedback must surface feedback."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget",
            review_cycle=0,
            review_feedback="Split the PR into two steps — schema change first.",
        )
        assert "Prior Review Feedback" in result
        assert "Split the PR into two steps" in result
        # Implement phase should use implementation-specific language, not
        # "draft in-place" which only applies to the refine phase.
        assert "revise your implementation" in result
        assert "in-place" not in result
        # Preamble should say "implementation" not "draft" for implement phase
        assert "previous implementation" in result
        # Must explicitly override prior-consensus inference on HITL cycle 0.
        assert "consensus is superseded" in result.lower()
        # Cycle 0 still embeds the full task + instructions (no delta cycle).
        assert "## Task Description" in result


class TestRenderContractTasks:
    """Tests for _render_contract_tasks helper."""

    def test_returns_none_for_missing_contract(self):
        """Returns None when contract cannot be loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _render_contract_tasks(tmpdir, "pid-1", "local")
            assert result is None

    def _make_contract(self, tmpdir, identifier, phases=None):
        """Helper to create a valid contract file."""
        contract_dir = Path(tmpdir) / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True, exist_ok=True)
        contract_data = {
            "schemaVersion": "1.0",
            "phases": phases or [],
        }
        if isinstance(identifier, int):
            contract_data["issue"] = {
                "number": identifier,
                "title": "Test issue",
                "url": f"https://github.com/test/repo/issues/{identifier}",
            }
        else:
            contract_data["pipeline_id"] = identifier
        (contract_dir / f"{identifier}.json").write_text(json.dumps(contract_data))

    def test_renders_checklist_with_acceptance_criteria(self):
        """Renders tasks as a markdown checklist with acceptance criteria."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_contract(
                tmpdir,
                "pid-1",
                phases=[
                    {
                        "id": "phase-1",
                        "name": "Phase 1",
                        "tasks": [
                            {
                                "id": "task-1",
                                "description": "Add the widget",
                                "status": "pending",
                                "acceptance_criteria": "Widget renders correctly",
                                "files_affected": ["src/widget.py"],
                            },
                            {
                                "id": "task-2",
                                "description": "Write tests",
                                "status": "complete",
                                "acceptance_criteria": "Tests pass",
                                "files_affected": [],
                            },
                        ],
                    }
                ],
            )

            result = _render_contract_tasks(tmpdir, "pid-1", "local")
            assert result is not None
            assert "## Contract Tasks" in result
            assert "[ ] **task-1**: Add the widget" in result
            assert "Acceptance: Widget renders correctly" in result
            assert "Files: src/widget.py" in result
            assert "[x] **task-2**: Write tests" in result

    def test_marks_complete_tasks(self):
        """Marks complete tasks with [x]."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_contract(
                tmpdir,
                "pid-1",
                phases=[
                    {
                        "id": "phase-1",
                        "name": "Phase 1",
                        "tasks": [
                            {
                                "id": "task-1",
                                "description": "Done task",
                                "status": "complete",
                            },
                        ],
                    }
                ],
            )

            result = _render_contract_tasks(tmpdir, "pid-1", "local")
            assert result is not None
            assert "[x] **task-1**" in result

    def test_issue_mode_uses_issue_number(self):
        """In issue mode, contract is loaded by pipeline_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_contract(
                tmpdir,
                "pid-1",
                phases=[
                    {
                        "id": "phase-1",
                        "name": "Phase 1",
                        "tasks": [
                            {
                                "id": "task-1",
                                "description": "Fix the bug",
                                "status": "pending",
                            },
                        ],
                    }
                ],
            )

            result = _render_contract_tasks(tmpdir, "pid-1", "issue", issue_number=42)
            assert result is not None
            assert "task-1" in result

    def test_returns_none_for_empty_phases(self):
        """Returns None when contract has no phases."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_contract(tmpdir, "pid-1", phases=[])

            result = _render_contract_tasks(tmpdir, "pid-1", "local")
            assert result is None


class TestSummarizeIssue:
    """Tests for _summarize_issue() helper."""

    def test_none_prompt_with_issue_number(self):
        """Returns generic message when prompt is None but issue number given."""
        result = _summarize_issue(None, issue_number=42)
        assert "42" in result

    def test_none_prompt_no_issue_number(self):
        """Returns empty string when both prompt and issue_number are None."""
        result = _summarize_issue(None)
        assert result == ""

    def test_extracts_heading_title(self):
        """Extracts title from first markdown heading."""
        prompt = "## Add dark mode\n\nUsers want a dark theme."
        result = _summarize_issue(prompt, issue_number=10)
        assert "Add dark mode" in result
        assert "10" in result

    def test_extracts_first_paragraph(self):
        """Includes first paragraph as context."""
        prompt = "# Feature\n\nThis is the first paragraph.\n\nSecond paragraph."
        result = _summarize_issue(prompt)
        assert "first paragraph" in result
        assert "Second paragraph" not in result

    def test_truncates_long_paragraph(self):
        """Truncates very long first paragraphs."""
        prompt = "# Title\n\n" + "x" * 500
        result = _summarize_issue(prompt)
        assert len(result) < 500
        assert "..." in result

    def test_non_heading_first_line(self):
        """Uses first non-empty line as title if no heading."""
        prompt = "Fix the authentication bug\n\nDetails here."
        result = _summarize_issue(prompt)
        assert "Fix the authentication bug" in result

    def test_empty_prompt(self):
        """Handles empty string prompt."""
        result = _summarize_issue("")
        assert result == ""


class TestExtractPlanOverview:
    """Tests for _extract_plan_overview() helper."""

    def test_extracts_overview_before_phases(self):
        """Returns content before ### Phase headings."""
        plan = (
            "# Plan: Add auth\n\n"
            "## Summary\n\n"
            "Add authentication to the API.\n\n"
            "## Implementation Phases\n\n"
            "### Phase 1: Core auth\n\n"
            "**Tasks**:\n- Add JWT support\n"
        )
        result = _extract_plan_overview(plan)
        assert "Add authentication" in result
        assert "Implementation Phases" in result
        assert "Add JWT support" not in result

    def test_stops_at_yaml_tasks(self):
        """Stops before yaml-tasks appendix."""
        plan = "# Plan\n\n## Summary\n\nOverview.\n\n```yaml\n# yaml-tasks\nphases:\n```"
        result = _extract_plan_overview(plan)
        assert "Overview" in result
        assert "yaml-tasks" not in result

    def test_stops_at_structured_task_appendix(self):
        """Stops before Structured Task Appendix heading."""
        plan = "# Plan\n\n## Summary\n\nOverview.\n\n## Structured Task Appendix\n\nYAML here."
        result = _extract_plan_overview(plan)
        assert "Overview" in result
        assert "YAML here" not in result

    def test_returns_full_text_when_no_phases(self):
        """Returns full text if no phase headings found."""
        plan = "# Plan\n\nSimple overview with no phases."
        result = _extract_plan_overview(plan)
        assert "Simple overview" in result

    def test_trims_trailing_blank_lines(self):
        """Trailing blank lines are removed."""
        plan = "# Plan\n\nOverview.\n\n\n\n### Phase 1: Core\n"
        result = _extract_plan_overview(plan)
        assert not result.endswith("\n\n")


class TestBuildRoleContext:
    """Tests for _build_role_context() helper."""

    def _make_phase(self, phase_id="phase-1", name="Core changes", tasks=None):
        """Create a mock phase object."""
        phase = MagicMock()
        phase.id = phase_id
        phase.name = name
        phase.tasks = tasks or []
        phase.status = "in_progress"
        return phase

    def _make_task(self, task_id="task-1", desc="Fix bug", files=None, acceptance=None):
        """Create a mock task object."""
        task = MagicMock()
        task.id = task_id
        task.description = desc
        task.files_affected = files or []
        task.acceptance_criteria = acceptance or "Tests pass"
        task.role = None
        return task

    def test_architect_gets_full_prompt(self):
        """Architect receives the full issue body."""
        result = _build_role_context("architect", "Full issue body here", issue_number=1)
        assert "## Task Description" in result
        assert "Full issue body here" in result

    def test_task_planner_gets_full_prompt(self):
        """Task planner receives the full issue body."""
        result = _build_role_context("task_planner", "Full body", issue_number=1)
        assert "## Task Description" in result
        assert "Full body" in result

    def test_risk_analyst_gets_full_prompt(self):
        """Risk analyst receives the full issue body."""
        result = _build_role_context("risk_analyst", "Full body", issue_number=1)
        assert "## Task Description" in result

    def test_tester_gets_summary_not_full_body(self):
        """Tester receives a summary, not the full issue body."""
        long_prompt = "# Feature\n\n" + "Detail " * 200
        result = _build_role_context("tester", long_prompt, issue_number=42)
        assert "## Background" in result
        assert "## Task Description" not in result
        assert "## For More Context" in result
        assert "gh issue view 42" in result

    def test_documenter_gets_summary_not_full_body(self):
        """Documenter receives a summary, not the full issue body."""
        result = _build_role_context(
            "documenter", "# Big feature\n\nLots of detail.", issue_number=5
        )
        assert "## Background" in result
        assert "## Task Description" not in result
        assert "## For More Context" in result

    def test_tester_with_phase_obj_includes_tasks(self):
        """Tester with phase_obj includes phase-scoped task details."""
        task = self._make_task("TASK-1-1", "Add validation", ["models.py"], "Tests pass")
        task.role = "tester"
        phase = self._make_phase("phase-1", "Schema", [task])
        result = _build_role_context("tester", "# Issue\n\nBody.", issue_number=1, phase_obj=phase)
        assert "Phase Scope" in result
        assert "TASK-1-1" in result
        assert "Add validation" in result
        assert "models.py" in result
        assert "Focus your testing" in result

    def test_documenter_with_phase_obj_includes_tasks(self):
        """Documenter with phase_obj includes phase-scoped task details."""
        task = self._make_task("TASK-2-1", "Update docs", ["README.md"])
        task.role = "documenter"
        phase = self._make_phase("phase-2", "Docs", [task])
        result = _build_role_context(
            "documenter", "# Issue\n\nBody.", issue_number=1, phase_obj=phase
        )
        assert "Phase Scope" in result
        assert "TASK-2-1" in result
        assert "Focus your documentation" in result

    def test_tester_with_all_phases_shows_other_phases(self):
        """Tester sees other phases listed for orientation."""
        task = self._make_task()
        phase1 = self._make_phase("phase-1", "Core", [task])
        phase2 = self._make_phase("phase-2", "Tests", [])
        result = _build_role_context(
            "tester",
            "# Issue",
            issue_number=1,
            phase_obj=phase1,
            all_phases=[phase1, phase2],
        )
        assert "Other Phases" in result
        assert "phase-2" in result

    def test_context_pointers_always_present_for_execution_roles(self):
        """All execution roles get 'For More Context' pointers."""
        for role in ("tester", "documenter"):
            result = _build_role_context(role, "# Issue\n\nBody.", issue_number=1)
            assert "## For More Context" in result
            assert "gh issue view 1" in result

    def test_no_prompt_for_execution_role(self):
        """Execution role with no prompt still gets context pointers."""
        result = _build_role_context("tester", None, issue_number=5)
        assert "## For More Context" in result
        assert "5" in result

    def test_coder_sees_coder_tasks_and_unassigned(self):
        """Coder sees tasks with role='coder' and role=None, but NOT role='tester'."""
        task_coder = self._make_task("task-1-1", "Code feature")
        task_coder.role = "coder"

        task_tester = self._make_task("task-1-2", "Write tests")
        task_tester.role = "tester"

        task_unassigned = self._make_task("task-1-3", "Misc task")
        task_unassigned.role = None

        phase = self._make_phase("phase-1", "Implement", [task_coder, task_tester, task_unassigned])

        result = _build_role_context("coder", "# Issue", issue_number=1, phase_obj=phase)
        assert "task-1-1" in result
        assert "Code feature" in result
        assert "task-1-3" in result
        assert "Misc task" in result
        assert "task-1-2" not in result
        assert "Write tests" not in result

    def test_tester_sees_only_tester_tasks(self):
        """Tester only sees tasks with role='tester', not role=None or role='coder'."""
        task_coder = self._make_task("task-1-1", "Code feature")
        task_coder.role = "coder"

        task_tester = self._make_task("task-1-2", "Write tests")
        task_tester.role = "tester"

        task_unassigned = self._make_task("task-1-3", "Misc task")
        task_unassigned.role = None

        phase = self._make_phase("phase-1", "Implement", [task_coder, task_tester, task_unassigned])

        result = _build_role_context("tester", "# Issue", issue_number=1, phase_obj=phase)
        assert "task-1-2" in result
        assert "Write tests" in result
        assert "task-1-1" not in result
        assert "Code feature" not in result
        assert "task-1-3" not in result
        assert "Misc task" not in result

    def test_documenter_sees_only_documenter_tasks(self):
        """Documenter only sees tasks with role='documenter'."""
        task_coder = self._make_task("task-1-1", "Code feature")
        task_coder.role = "coder"

        task_doc = self._make_task("task-1-2", "Update README")
        task_doc.role = "documenter"

        task_unassigned = self._make_task("task-1-3", "Misc task")
        task_unassigned.role = None

        phase = self._make_phase("phase-1", "Implement", [task_coder, task_doc, task_unassigned])

        result = _build_role_context("documenter", "# Issue", issue_number=1, phase_obj=phase)
        assert "task-1-2" in result
        assert "Update README" in result
        assert "task-1-1" not in result
        assert "Code feature" not in result
        assert "task-1-3" not in result
        assert "Misc task" not in result

    def test_non_execution_role_sees_all_tasks(self):
        """Non-execution roles (reviewer_code, architect) see all tasks regardless of role."""
        task_coder = self._make_task("task-1-1", "Code feature")
        task_coder.role = "coder"

        task_tester = self._make_task("task-1-2", "Write tests")
        task_tester.role = "tester"

        task_unassigned = self._make_task("task-1-3", "Misc task")
        task_unassigned.role = None

        phase = self._make_phase("phase-1", "Implement", [task_coder, task_tester, task_unassigned])

        # reviewer_code is not an execution role, should see all tasks
        result = _build_role_context("reviewer_code", "# Issue", issue_number=1, phase_obj=phase)
        assert "task-1-1" in result
        assert "Code feature" in result
        assert "task-1-2" in result
        assert "Write tests" in result
        assert "task-1-3" in result
        assert "Misc task" in result

    def test_legacy_plan_all_roles_none_shows_all_tasks(self):
        """Legacy plans (all role=None) show all tasks to all execution roles."""
        task1 = self._make_task("task-1-1", "First task")
        task1.role = None
        task2 = self._make_task("task-1-2", "Second task")
        task2.role = None
        task3 = self._make_task("task-1-3", "Third task")
        task3.role = None
        phase = self._make_phase("phase-1", "Implement", [task1, task2, task3])

        # Even though tester would normally be filtered, legacy plans show all
        for role in ("coder", "tester", "documenter"):
            result = _build_role_context(role, "# Issue", issue_number=1, phase_obj=phase)
            assert "task-1-1" in result, f"{role} should see task-1-1 in legacy plan"
            assert "task-1-2" in result, f"{role} should see task-1-2 in legacy plan"
            assert "task-1-3" in result, f"{role} should see task-1-3 in legacy plan"


class TestBuildAgentPromptRoleContext:
    """Tests for role-specific context in _build_agent_prompt()."""

    def test_tester_prompt_has_background_not_task_description(self):
        """Tester prompt uses Background section, not Task Description."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Big Feature\n\nLots of detail about the feature.",
            issue_number=42,
        )
        assert "## Background" in result
        assert "## Task Description" not in result
        assert "## For More Context" in result
        assert "TESTER" in result
        assert "## Parallel Execution with Subagents" in result

    def test_documenter_prompt_has_background_not_task_description(self):
        """Documenter prompt uses Background section, not Task Description."""
        result = _build_agent_prompt(
            role_value="documenter",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=10,
        )
        assert "## Background" in result
        assert "## Task Description" not in result
        assert "DOCUMENTER" in result

    def test_architect_prompt_retains_full_task_description(self):
        """Architect still gets full Task Description."""
        result = _build_agent_prompt(
            role_value="architect",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nFull detail needed for analysis.",
            issue_number=10,
        )
        assert "## Task Description" in result
        assert "Full detail needed for analysis" in result

    def test_task_planner_prompt_retains_full_task_description(self):
        """Task planner still gets full Task Description."""
        result = _build_agent_prompt(
            role_value="task_planner",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nFull detail.",
            issue_number=10,
        )
        assert "## Task Description" in result

    def test_tester_with_phase_obj_includes_phase_scope(self):
        """Tester with phase_obj includes phase-scoped task context."""
        phase = MagicMock()
        phase.id = "phase-1"
        phase.name = "Auth changes"
        task = MagicMock()
        task.id = "TASK-1-1"
        task.description = "Add JWT validation"
        task.acceptance_criteria = "Token verified"
        task.files_affected = ["auth.py"]
        task.role = "tester"
        phase.tasks = [task]

        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Auth feature\n\nAdd authentication.",
            issue_number=42,
            phase_obj=phase,
        )
        assert "Phase Scope" in result
        assert "TASK-1-1" in result
        assert "Add JWT validation" in result
        assert "auth.py" in result

    def test_every_prompt_has_context_section(self):
        """All role prompts include the Context metadata section."""
        for role in ("tester", "documenter", "architect"):
            result = _build_agent_prompt(
                role_value=role,
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Issue",
                issue_number=1,
                repo="owner/repo",
                branch="egg/test",
            )
            assert "## Context" in result
            assert "Pipeline ID: pid-1" in result
            assert f"Agent Role: {role}" in result


class TestProducerEscapeHatchInPrompts:
    """Producer prompts must surface the runtime escape hatch (#2529).

    The check_file_restriction / report_impasse guidance has to land in
    the agent prompt for every role that *emits* an impasse — coder,
    tester, and documenter. Without this section the producer cannot
    discover the escape hatch and falls back to inventing workarounds
    (the .github-staging/ deletion-marker anti-pattern from pipeline
    issue-2474-v2 that this PR exists to prevent).

    Originally the guidance was injected only into the planner prompt
    via _build_role_restrictions_section, which meant producers never
    saw it. The fix moves the actionable guidance into a producer-only
    helper.
    """

    @pytest.mark.parametrize("role", ["coder", "tester", "documenter"])
    def test_producer_prompt_contains_report_impasse(self, role):
        result = _build_agent_prompt(
            role_value=role,
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
        )
        assert "mcp__sdlc__report_impasse" in result, (
            f"{role} prompt missing the report_impasse escape-hatch tool name; "
            "producers must see the actionable guidance to avoid inventing workarounds."
        )
        assert "mcp__sdlc__check_file_restriction" in result, (
            f"{role} prompt missing the check_file_restriction tool name."
        )
        assert "DO NOT invent workarounds" in result, (
            f"{role} prompt missing the anti-workaround directive."
        )

    def test_planner_prompt_has_summary_not_actionable_guidance(self):
        """Planner gets only the post-failure delegation summary, not the
        producer-facing actionable text. The planner doesn't emit
        impasses; it doesn't need the call-these-tools instructions."""
        result = _build_agent_prompt(
            role_value="task_planner",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
        )
        assert "Runtime delegation" in result
        assert "auto-delegate" in result
        # The actionable producer-only directives should NOT appear in
        # the planner prompt — the summary mentions ``report_impasse``
        # by name for context, but doesn't tell the planner to call it.
        assert "DO NOT invent workarounds" not in result
        assert "mcp__sdlc__check_file_restriction" not in result

    def test_architect_prompt_does_not_contain_escape_hatch(self):
        """Architect is a non-impassing analysis role — it shouldn't
        carry the producer-only guidance."""
        result = _build_agent_prompt(
            role_value="architect",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
        )
        assert "mcp__sdlc__report_impasse" not in result


# ── Additional tester-authored coverage ──────────────────────────────────────


class TestSummarizeIssueEdgeCases:
    """Edge cases for _summarize_issue() not covered by the coder's tests."""

    def test_whitespace_only_prompt(self):
        """Whitespace-only prompt gets the same fallback as None."""
        result = _summarize_issue("   \n\n  \t  ")
        assert result == ""

    def test_whitespace_only_prompt_with_issue(self):
        """Whitespace-only prompt with issue_number gets the same fallback as None."""
        result = _summarize_issue("   \n\n  \t  ", issue_number=42)
        assert result == "Working on issue #42."

    def test_title_only_no_paragraph(self):
        """Prompt with only a title and no body paragraph."""
        result = _summarize_issue("# Just a title")
        assert "Just a title" in result
        # Should not crash or produce trailing newline artifacts
        assert "\n\n\n" not in result

    def test_multiple_headings_takes_first(self):
        """When prompt has multiple headings, first one becomes the title."""
        prompt = "## First heading\n\n## Second heading\n\nParagraph."
        result = _summarize_issue(prompt)
        assert "First heading" in result
        # The second heading falls into the first-paragraph extraction
        # (it's the first non-empty line after the title)
        assert "Second heading" in result

    def test_blank_lines_before_title(self):
        """Leading blank lines before the title are skipped."""
        prompt = "\n\n\n## Title here\n\nBody text."
        result = _summarize_issue(prompt)
        assert "Title here" in result
        assert "Body text" in result

    def test_multi_line_first_paragraph(self):
        """Multi-line first paragraphs are joined with spaces."""
        prompt = "# Title\n\nFirst line.\nSecond line.\nThird line."
        result = _summarize_issue(prompt)
        assert "First line." in result
        assert "Second line." in result
        assert "Third line." in result
        # Should be space-joined, not newline-joined
        assert "First line. Second line. Third line." in result

    def test_issue_ref_format(self):
        """Issue reference is in parenthetical format."""
        result = _summarize_issue("# Title\n\nBody.", issue_number=99)
        assert "(issue #99)" in result

    def test_no_issue_number_omits_ref(self):
        """When issue_number is None, no issue reference appears."""
        result = _summarize_issue("# Title\n\nBody.")
        assert "issue #" not in result


class TestExtractPlanOverviewEdgeCases:
    """Edge cases for _extract_plan_overview()."""

    def test_stops_at_lowercase_phase_heading(self):
        """Stops at ### phase- (lowercase) headings."""
        plan = "# Plan\n\nOverview.\n\n### phase-1: Core changes\n\nPhase detail."
        result = _extract_plan_overview(plan)
        assert "Overview" in result
        assert "Core changes" not in result
        assert "Phase detail" not in result

    def test_stops_at_issue_to_task_mapping(self):
        """Stops at ## Issue-to-Task Mapping heading."""
        plan = "# Plan\n\nOverview.\n\n## Issue-to-Task Mapping\n\nMapping detail."
        result = _extract_plan_overview(plan)
        assert "Overview" in result
        assert "Mapping detail" not in result

    def test_empty_plan_text(self):
        """Empty plan text returns empty string."""
        assert _extract_plan_overview("") == ""

    def test_plan_starting_with_phase_heading(self):
        """Plan that starts immediately with a phase heading returns empty."""
        plan = "### Phase 1: Core\n\nTasks here."
        result = _extract_plan_overview(plan)
        assert result == ""

    def test_yaml_tasks_in_code_block(self):
        """Stops at yaml-tasks even inside a code block marker."""
        plan = "# Plan\n\nOverview.\n\n```yaml\n# yaml-tasks\nphases:\n- id: p1\n```"
        result = _extract_plan_overview(plan)
        assert "Overview" in result
        assert "phases:" not in result

    def test_preserves_internal_structure(self):
        """Preserves headings and formatting within the overview section."""
        plan = (
            "# Plan: Auth\n\n"
            "## Goals\n\n- Goal 1\n- Goal 2\n\n"
            "## Approach\n\nUse JWT.\n\n"
            "### Phase 1: Core\n\nTask details.\n"
        )
        result = _extract_plan_overview(plan)
        assert "## Goals" in result
        assert "## Approach" in result
        assert "Goal 1" in result
        assert "Use JWT" in result


class TestBuildRoleContextEdgeCases:
    """Edge cases for _build_role_context()."""

    def _make_phase(self, phase_id="phase-1", name="Core", tasks=None, status="in_progress"):
        phase = MagicMock()
        phase.id = phase_id
        phase.name = name
        phase.tasks = tasks or []
        phase.status = status
        return phase

    def _make_task(self, task_id="task-1", desc="Fix bug", files=None, acceptance=None):
        task = MagicMock()
        task.id = task_id
        task.description = desc
        task.files_affected = files
        task.acceptance_criteria = acceptance
        task.role = None
        return task

    def test_analysis_role_none_prompt_returns_empty(self):
        """Analysis role with None prompt returns empty string."""
        result = _build_role_context("architect", None, issue_number=1)
        assert result == ""

    def test_analysis_role_empty_prompt_returns_empty(self):
        """Analysis role with empty string prompt returns empty string."""
        result = _build_role_context("task_planner", "", issue_number=1)
        assert result == ""

    def test_task_without_acceptance_criteria(self):
        """Tasks with no acceptance_criteria are rendered without that line."""
        task = self._make_task("t-1", "Fix it", files=["a.py"], acceptance=None)
        task.role = "tester"
        phase = self._make_phase(tasks=[task])
        result = _build_role_context("tester", "# Issue", issue_number=1, phase_obj=phase)
        assert "t-1" in result
        assert "Fix it" in result
        assert "a.py" in result
        assert "Acceptance" not in result

    def test_task_without_files_affected(self):
        """Tasks with no files_affected are rendered without that line."""
        task = self._make_task("t-2", "Update logic", files=None, acceptance="Tests pass")
        task.role = "tester"
        phase = self._make_phase(tasks=[task])
        result = _build_role_context("tester", "# Issue", issue_number=1, phase_obj=phase)
        assert "t-2" in result
        assert "Acceptance: Tests pass" in result
        assert "Files:" not in result

    def test_task_with_empty_files_list(self):
        """Tasks with empty files_affected list don't show Files line."""
        task = self._make_task("t-3", "Refactor", files=[], acceptance="Lint passes")
        task.role = "documenter"
        phase = self._make_phase(tasks=[task])
        result = _build_role_context("documenter", "# Issue", issue_number=1, phase_obj=phase)
        assert "Files:" not in result

    def test_multiple_tasks_in_phase(self):
        """All tasks in a phase are listed."""
        tasks = [
            self._make_task("t-1", "Task one", ["a.py"]),
            self._make_task("t-2", "Task two", ["b.py"]),
            self._make_task("t-3", "Task three", ["c.py"]),
        ]
        for t in tasks:
            t.role = "tester"
        phase = self._make_phase(tasks=tasks)
        result = _build_role_context("tester", "# Issue", issue_number=1, phase_obj=phase)
        assert "t-1" in result
        assert "t-2" in result
        assert "t-3" in result
        assert "Task one" in result
        assert "Task two" in result
        assert "Task three" in result

    def test_no_issue_number_omits_gh_command(self):
        """When issue_number is None, no gh issue view command appears."""
        result = _build_role_context("tester", "# Issue", issue_number=None)
        assert "gh issue view" not in result
        # But other context pointers still present
        assert "## For More Context" in result
        assert "Changed files" in result

    def test_unknown_role_treated_as_execution(self):
        """Unknown roles get execution-style context (not analysis)."""
        result = _build_role_context("some_new_role", "# Feature\n\nDetail.", issue_number=1)
        assert "## Background" in result
        assert "## Task Description" not in result
        assert "## For More Context" in result

    def test_tester_only_current_phase_in_all_phases(self):
        """When all_phases contains only the current phase, no Other Phases section."""
        task = self._make_task()
        phase = self._make_phase("phase-1", "Core", [task])
        result = _build_role_context(
            "tester",
            "# Issue",
            issue_number=1,
            phase_obj=phase,
            all_phases=[phase],
        )
        assert "Other Phases" not in result

    def test_documenter_phase_intro_text(self):
        """Documenter gets documentation-focused intro text."""
        task = self._make_task("t-1", "Add feature")
        phase = self._make_phase(tasks=[task])
        result = _build_role_context("documenter", "# Issue", phase_obj=phase)
        assert "Focus your documentation" in result

    def test_non_tester_non_documenter_phase_intro(self):
        """Non-tester/non-documenter execution roles get generic phase intro."""
        task = self._make_task("t-1", "Fix thing")
        phase = self._make_phase(tasks=[task])
        # Use a generic execution role with phase_obj to exercise the else branch
        result = _build_role_context("some_new_role", "# Issue", phase_obj=phase)
        assert "The following tasks were implemented in this phase" in result
        assert "Focus your testing" not in result
        assert "Focus your documentation" not in result


class TestBuildAgentPromptEdgeCases:
    """Edge cases for _build_agent_prompt() with new role-context params."""

    def test_risk_analyst_gets_full_task_description(self):
        """Risk analyst (third analysis role) retains full Task Description."""
        result = _build_agent_prompt(
            role_value="risk_analyst",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Risky change\n\nFull analysis needed.",
            issue_number=7,
        )
        assert "## Task Description" in result
        assert "Full analysis needed" in result
        assert "## Background" not in result

    def test_review_feedback_included_with_role_context(self):
        """Review feedback appears alongside role context for execution roles."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
            review_feedback="Please add more edge case tests.",
        )
        assert "## Background" in result
        assert "## Review Feedback" in result
        assert "edge case tests" in result

    def test_none_prompt_for_execution_role(self):
        """Execution role with None prompt still generates valid prompt."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt=None,
            issue_number=50,
        )
        # Should still be a valid prompt with TESTER role
        assert "TESTER" in result
        assert "## For More Context" in result
        assert "gh issue view 50" in result

    def test_none_prompt_for_analysis_role(self):
        """Analysis role with None prompt still generates valid prompt."""
        result = _build_agent_prompt(
            role_value="architect",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt=None,
            issue_number=1,
        )
        # Should still be a valid prompt even without task description
        assert "ARCHITECT" in result
        assert "## Task Description" not in result

    def test_coder_role_delegates_to_phase_prompt(self):
        """Coder role delegates to _build_phase_prompt, not _build_role_context."""
        result = _build_agent_prompt(
            role_value="coder",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nImplement this.",
            issue_number=1,
        )
        # Should NOT have the role header from _build_agent_prompt
        assert "**CODER**" not in result
        # Should contain phase-prompt style content
        assert "implement" in result.lower()

    def test_no_repo_or_branch_omits_those_lines(self):
        """When repo and branch are None, those lines are omitted from context."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=1,
            repo=None,
            branch=None,
        )
        assert "Repository:" not in result
        assert "Branch:" not in result
        # But other context is present
        assert "Pipeline ID: pid-1" in result

    def test_no_issue_number_omits_issue_line(self):
        """When issue_number is None, Issue line is omitted."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=None,
        )
        assert "Issue: #" not in result


class TestNamespacedOutputFilenames:
    """Tests for namespaced (identifier-prefixed) output filenames in prompts."""

    def test_architect_prompt_uses_issue_number(self):
        """Architect prompt references {issue_number}-architect-output.json."""
        result = _build_agent_prompt(
            role_value="architect",
            phase="plan",
            pipeline_id="issue-871",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=871,
        )
        assert "871-architect-output.json" in result

    def test_risk_analyst_prompt_uses_issue_number(self):
        """Risk analyst prompt references {issue_number}-risk_analyst-output.json."""
        result = _build_agent_prompt(
            role_value="risk_analyst",
            phase="plan",
            pipeline_id="issue-871",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=871,
        )
        assert "871-risk_analyst-output.json" in result

    def test_prompt_falls_back_to_pipeline_id(self):
        """Without issue_number, prompt uses pipeline_id as identifier."""
        result = _build_agent_prompt(
            role_value="architect",
            phase="plan",
            pipeline_id="local-abc123",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=None,
        )
        assert "local-abc123-architect-output.json" in result


class TestReadTesterGaps:
    """Tests for _read_tester_gaps() helper."""

    def test_with_gaps_and_failures(self, tmp_path):
        """Returns formatted string with test failures and gaps."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text(
            json.dumps(
                {
                    "tests_failed": 2,
                    "gaps_found": [
                        "No error handling for invalid input",
                        "Missing boundary check in parse()",
                    ],
                }
            )
        )

        result = _read_tester_gaps(tmp_path)
        assert result is not None
        assert "### tester findings" in result
        assert "**2** test(s) failed" in result
        assert "No error handling for invalid input" in result
        assert "Missing boundary check in parse()" in result

    def test_prefixed_file_preferred(self, tmp_path):
        """Prefixed file is preferred over global file when identifier given."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text(
            json.dumps({"tests_failed": 1, "gaps_found": ["old-gap"]})
        )
        (outputs_dir / "871-tester-output.json").write_text(
            json.dumps({"tests_failed": 3, "gaps_found": ["prefixed-gap"]})
        )

        result = _read_tester_gaps(tmp_path, identifier=871)
        assert result is not None
        assert "prefixed-gap" in result
        assert "old-gap" not in result

    def test_fallback_to_global_with_identifier(self, tmp_path):
        """Falls back to global path when prefixed file does not exist."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text(
            json.dumps({"tests_failed": 1, "gaps_found": ["global-gap"]})
        )

        result = _read_tester_gaps(tmp_path, identifier=871)
        assert result is not None
        assert "global-gap" in result

    def test_no_gaps_returns_none(self, tmp_path):
        """Returns None when no gaps or failures found."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text(
            json.dumps(
                {
                    "tests_failed": 0,
                    "gaps_found": [],
                    "summary": "All tests pass",
                }
            )
        )

        result = _read_tester_gaps(tmp_path)
        assert result is None

    def test_missing_file_returns_none(self, tmp_path):
        """Returns None when tester output file doesn't exist."""
        result = _read_tester_gaps(tmp_path)
        assert result is None

    def test_malformed_json_returns_none(self, tmp_path):
        """Returns None when JSON is malformed."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text("not valid json{{{")

        result = _read_tester_gaps(tmp_path)
        assert result is None

    def test_summary_fallback_when_no_gaps_field(self, tmp_path):
        """Falls back to scanning summary for failure keywords."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text(
            json.dumps(
                {
                    "tests_added": 10,
                    "tests_passed": 10,
                    "summary": "Tests pass but found gaps in error handling",
                }
            )
        )

        result = _read_tester_gaps(tmp_path)
        assert result is not None
        assert "tester findings" in result
        assert "gaps in error handling" in result

    def test_summary_no_keywords_returns_none(self, tmp_path):
        """Returns None when summary has no failure keywords."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text(
            json.dumps(
                {
                    "tests_added": 10,
                    "tests_passed": 10,
                    "summary": "All tests pass with good coverage",
                }
            )
        )

        result = _read_tester_gaps(tmp_path)
        assert result is None

    def test_caps_at_10_gaps(self, tmp_path):
        """Caps gaps at 10 to avoid prompt bloat."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        gaps = [f"Gap number {i}" for i in range(15)]
        (outputs_dir / "tester-output.json").write_text(json.dumps({"gaps_found": gaps}))

        result = _read_tester_gaps(tmp_path)
        assert result is not None
        assert "Gap number 0" in result
        assert "Gap number 9" in result
        assert "Gap number 10" not in result
        assert "5 more gaps" in result

    def test_non_dict_json_returns_none(self, tmp_path):
        """Returns None when JSON is valid but not a dict."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text(json.dumps([1, 2, 3]))

        result = _read_tester_gaps(tmp_path)
        assert result is None

    def test_gaps_found_non_list_ignored(self, tmp_path):
        """Non-list gaps_found is ignored."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text(
            json.dumps({"gaps_found": "not a list", "tests_failed": 0})
        )

        result = _read_tester_gaps(tmp_path)
        assert result is None

    def test_only_failures_no_gaps(self, tmp_path):
        """Returns finding when only tests_failed is set."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "tester-output.json").write_text(
            json.dumps({"tests_failed": 3, "summary": "3 tests failed"})
        )

        result = _read_tester_gaps(tmp_path)
        assert result is not None
        assert "**3** test(s) failed" in result

    def test_long_gap_strings_truncated(self, tmp_path):
        """Individual gap strings are truncated to 200 characters."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        long_gap = "A" * 300
        (outputs_dir / "tester-output.json").write_text(json.dumps({"gaps_found": [long_gap]}))

        result = _read_tester_gaps(tmp_path)
        assert result is not None
        # The gap should be truncated to 200 chars
        assert "A" * 200 in result
        assert "A" * 201 not in result


class TestTesterGapFindingPrompts:
    """Tests for tester gap-finding language in prompts."""

    def test_tester_prompt_contains_gap_finding_language(self):
        """Tester role-task block includes the dual-mandate framing and
        adversarial probing focus items.

        The mandate has two parts: comprehensive regression coverage AND
        adversarial probing that produces failing tests as bug reports
        back to the coder. Both must surface in the role-task block —
        this test guards the implement-phase tester block in
        `_build_agent_prompt`. The companion guard
        `test_tester_orientation_contains_dual_mandate_pointer` covers
        the orientation-side text; together they detect partial removal
        across the two locations (review feedback on PR #2450).
        """
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        # Dual mandate framing
        assert "mandate is two-fold" in result
        assert "Comprehensive coverage" in result
        assert "Adversarial probing" in result
        # Failing test must be paired with an explicit NACK that names it —
        # the committed failing test alone is not the bug report.
        assert "the NACK is the bug report" in result
        assert "naming the failing test" in result.lower()
        # The role-task probing list AND the testing-step header both use
        # the "Adversarial probing" wording — count >= 2 protects against
        # silent drift back to "Adversarial bug-hunting".
        assert result.count("Adversarial probing") >= 2
        assert "Missing error handling" in result
        assert "Boundary conditions" in result
        assert "Uncovered code paths" in result
        # Untested-but-still-reported gap categories
        assert "Gap-finding focus" in result
        # Bridge to Configured Checks: the failing-test workflow must
        # connect to the propose-gate so testers don't propose with a
        # red `test` check.
        assert "Configured Checks" in result

    def test_phase_prompt_revision_reviewer_only_no_tester_language(self):
        """Phase prompt with reviewer-only feedback uses reviewer-only language."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget",
            review_cycle=1,
            review_feedback="Fix the naming convention",
        )

        assert "The reviewer found issues" in result
        assert "reviewer and tester" not in result
        assert "tester-output.json" not in result

    def test_phase_prompt_revision_with_tester_findings(self):
        """Phase prompt with tester findings in feedback uses reviewer+tester language."""
        feedback = (
            "Fix the naming convention\n\n"
            "### tester findings\n"
            "- **2** test(s) failed\n"
            "- Missing error handling for null input"
        )
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget",
            review_cycle=1,
            review_feedback=feedback,
        )

        assert "reviewer and tester" in result
        assert "tester-output.json" in result
        assert "### tester findings" in result

    def test_phase_prompt_revision_no_feedback_does_not_mention_tester(self):
        """Phase prompt without feedback has no tester references."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget",
            review_cycle=1,
            review_feedback=None,
        )

        assert "tester-output.json" not in result
        assert "reviewer and tester" not in result

    def test_phase_prompt_prior_feedback_header_with_tester_findings(self):
        """Prior Review Feedback header section uses tester-aware language when findings present."""
        feedback = "Reviewer says fix naming\n\n### tester findings\n- Missing boundary check"
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="issue",
            prompt="Build a widget",
            review_cycle=1,
            review_feedback=feedback,
        )

        assert "The reviewer and tester found issues with your previous work" in result
        # Should NOT contain the reviewer-only language
        assert "found issues with your previous draft" not in result


class TestTesterRepoChecksInjection:
    """Tests for injecting per-repo check commands into the tester prompt."""

    def test_tester_prompt_with_explicit_checks(self):
        """When repo has configured checks, they appear in the tester prompt."""
        checks = [
            {"name": "lint", "command": "npm run lint"},
            {"name": "test", "command": "npm test"},
        ]
        with patch("routes.pipelines.get_repo_checks", return_value=checks):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature\n\nDetail.",
                issue_number=1,
                repo="testuser/web-app",
            )
        assert "configured for this repository" in result
        assert "**lint**" in result
        assert "`npm run lint`" in result
        assert "**test**" in result
        assert "`npm test`" in result
        # Auto-discovery instructions should NOT be present
        assert "Discover commands" not in result
        # Mandatory language must be present
        assert "MANDATORY" in result
        assert "Run **every one**" in result

    def test_tester_prompt_without_checks_uses_autodiscovery(self):
        """When repo has no configured checks, auto-discovery instructions are used."""
        with patch("routes.pipelines.get_repo_checks", return_value=[]):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature\n\nDetail.",
                issue_number=1,
                repo="testuser/web-app",
            )
        assert "Discover commands" in result
        assert "configured for this repository" not in result

    def test_tester_prompt_without_repo_uses_autodiscovery(self):
        """When repo is None, auto-discovery instructions are used."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
            repo=None,
        )
        assert "Discover commands" in result
        assert "configured for this repository" not in result

    def test_tester_prompt_checks_preserve_order(self):
        """Configured checks appear in the order defined in repositories.yaml."""
        checks = [
            {"name": "install", "command": "npm install"},
            {"name": "lint", "command": "npm run lint"},
            {"name": "test", "command": "npm test"},
        ]
        with patch("routes.pipelines.get_repo_checks", return_value=checks):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature",
                issue_number=1,
                repo="testuser/web-app",
            )
        # Verify ordering via numbered list
        assert "1. **install**" in result
        assert "2. **lint**" in result
        assert "3. **test**" in result

    def test_tester_prompt_checks_still_has_autofix_instructions(self):
        """Even with explicit checks, auto-fix and commit instructions are present."""
        checks = [{"name": "lint", "command": "make lint"}]
        with patch("routes.pipelines.get_repo_checks", return_value=checks):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature",
                issue_number=1,
                repo="org/repo",
            )
        assert "Auto-fix" in result
        assert "Commit test fixes" in result

    def test_tester_prompt_handles_missing_config(self):
        """When get_repo_checks raises FileNotFoundError, falls back to auto-discovery."""
        with patch("routes.pipelines.get_repo_checks", side_effect=FileNotFoundError):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature\n\nDetail.",
                issue_number=1,
                repo="testuser/web-app",
            )
        assert "Discover commands" in result
        assert "configured for this repository" not in result

    def test_tester_prompt_has_check_execution_verification(self):
        """Tester prompt includes check execution verification section."""
        checks = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
        ]
        with patch("routes.pipelines.get_repo_checks", return_value=checks):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature",
                issue_number=1,
                repo="org/repo",
            )
        assert "Check Execution Verification (CRITICAL)" in result
        assert "run **every** configured check command" in result
        assert "Running tests alone is NOT sufficient" in result

    def test_tester_prompt_check_verification_present_without_checks(self):
        """Check execution verification appears even with auto-discovery."""
        with patch("routes.pipelines.get_repo_checks", return_value=[]):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature",
                issue_number=1,
                repo="org/repo",
            )
        assert "Check Execution Verification (CRITICAL)" in result

    def test_tester_prompt_includes_checks_passed_attestation_instruction(self):
        """Tester prompt tells agent to populate checks_passed in attestation (#1459, #1467)."""
        checks = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
        ]
        with patch("routes.pipelines.get_repo_checks", return_value=checks):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature",
                issue_number=1,
                repo="org/repo",
            )
        assert "checks_passed" in result
        assert "attestation" in result.lower()

    def test_tester_prompt_has_source_failure_procedure(self):
        """Tester prompt includes the explicit source-failure procedure (#1966).

        The prompt must give an unambiguous procedure for the case where the
        coder's source code breaks a configured check, so the tester does not
        rationalise either fixing source itself or proposing consensus with an
        invented `checks_passed` name (the #1964 antipattern).
        """
        checks = [{"name": "lint", "command": "make lint"}]
        with patch("routes.pipelines.get_repo_checks", return_value=checks):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature",
                issue_number=1,
                repo="org/repo",
            )
        assert "When Source-Code Checks Fail (CRITICAL)" in result
        # The three load-bearing instructions:
        assert "do NOT propose consensus" in result
        assert "Do NOT invent" in result
        assert "egg-orch message send --to coder --type HANDOFF" in result
        # And the wait-loop pointer rather than a sleep loop:
        assert "egg-orch message wait-loop" in result

    def test_tester_attestation_forbids_adhoc_check_names(self):
        """Attestation block explicitly forbids inventing ad-hoc check names (#1966)."""
        checks = [{"name": "lint", "command": "make lint"}]
        with patch("routes.pipelines.get_repo_checks", return_value=checks):
            result = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature",
                issue_number=1,
                repo="org/repo",
            )
        # Calls out the actual ad-hoc patterns seen on #1964:
        assert "ruff-check-tester-files" in result
        assert "do NOT invent ad-hoc names" in result


class TestTesterCheckCoverageValidation:
    """Tests for _validate_tester_check_coverage in signals.py (#1459, #1966)."""

    @staticmethod
    def _patched_store(repo: str | None = "org/repo"):
        """Return a patch context that stubs state_store.get_state_store."""
        mock_pipeline = MagicMock()
        mock_pipeline.repo = repo
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        return patch("routes.signals.get_state_store", return_value=mock_store)

    def test_rejects_missing_checks(self):
        """Proposal missing a configured check is rejected."""
        from routes.signals import _validate_tester_check_coverage

        configured = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
        ]

        with (
            self._patched_store(),
            patch("config.repo_config.get_repo_checks", return_value=configured),
        ):
            payload = {
                "attestation": {"checks_passed": ["test"]},  # missing "lint"
            }
            with pytest.raises(ValueError, match="lint"):
                _validate_tester_check_coverage("pid-1", payload, Path("/tmp"))

    def test_accepts_all_checks_present(self):
        """Proposal with all configured checks passes."""
        from routes.signals import _validate_tester_check_coverage

        configured = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
        ]

        with (
            self._patched_store(),
            patch("config.repo_config.get_repo_checks", return_value=configured),
        ):
            payload = {
                "attestation": {"checks_passed": ["lint", "test"]},
            }
            # Should not raise
            _validate_tester_check_coverage("pid-1", payload, Path("/tmp"))

    def test_case_insensitive_matching(self):
        """Check name matching is case-insensitive."""
        from routes.signals import _validate_tester_check_coverage

        configured = [{"name": "Lint", "command": "make lint"}]

        with (
            self._patched_store(),
            patch("config.repo_config.get_repo_checks", return_value=configured),
        ):
            payload = {
                "attestation": {"checks_passed": ["lint"]},
            }
            # Should not raise — "Lint" matches "lint"
            _validate_tester_check_coverage("pid-1", payload, Path("/tmp"))

    def test_no_configured_checks_skips_validation(self):
        """When no checks configured, validation is skipped."""
        from routes.signals import _validate_tester_check_coverage

        with (
            self._patched_store(),
            patch("config.repo_config.get_repo_checks", return_value=[]),
        ):
            payload = {
                "attestation": {"checks_passed": ["test"]},
            }
            # Should not raise
            _validate_tester_check_coverage("pid-1", payload, Path("/tmp"))

    def test_pipeline_lookup_failure_skips_validation(self):
        """When pipeline state cannot be loaded, validation is skipped gracefully."""
        from routes.signals import _validate_tester_check_coverage
        from state_store import PipelineNotFoundError

        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = PipelineNotFoundError("pid-1")

        with patch("routes.signals.get_state_store", return_value=mock_store):
            payload = {
                "attestation": {"checks_passed": ["test"]},
            }
            # Should not raise — graceful degradation
            _validate_tester_check_coverage("pid-1", payload, Path("/tmp"))

    def test_state_validation_error_skips_validation(self):
        """When pipeline state is corrupt, validation is skipped gracefully."""
        from routes.signals import _validate_tester_check_coverage
        from state_store import StateValidationError

        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = StateValidationError("corrupt state")

        with patch("routes.signals.get_state_store", return_value=mock_store):
            payload = {
                "attestation": {"checks_passed": ["test"]},
            }
            # Should not raise — graceful degradation for corrupt state
            _validate_tester_check_coverage("pid-1", payload, Path("/tmp"))

    def test_repo_checks_failure_skips_validation(self):
        """When get_repo_checks raises, validation is skipped gracefully."""
        from routes.signals import _validate_tester_check_coverage

        with (
            self._patched_store(),
            patch(
                "config.repo_config.get_repo_checks",
                side_effect=FileNotFoundError("repositories.yaml not found"),
            ),
        ):
            payload = {
                "attestation": {"checks_passed": ["test"]},
            }
            # Should not raise — missing config degrades gracefully
            _validate_tester_check_coverage("pid-1", payload, Path("/tmp"))

    def test_rejects_adhoc_check_names(self):
        """Ad-hoc check names that don't match configured names are rejected (#1966).

        Prevents the tester from renaming a failing ``lint`` into a passing
        ``lint-tester-files`` subscope — which is what produced red initial pushes.
        """
        from routes.signals import _validate_tester_check_coverage

        configured = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
            {"name": "security", "command": "make security"},
        ]

        with (
            self._patched_store(),
            patch("config.repo_config.get_repo_checks", return_value=configured),
        ):
            payload = {
                "attestation": {
                    "checks_passed": [
                        "pytest-tester-suite",
                        "ruff-check-tester-files",
                    ],
                },
            }
            with pytest.raises(ValueError, match="lint.*security.*test"):
                _validate_tester_check_coverage("pid-1", payload, Path("/tmp"))

    def test_tests_execution_blocked_skips_validation(self):
        """When tests_execution_blocked is set, signal validation is skipped (#1459)."""
        from routes.signals import _validate_tester_check_coverage

        configured = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
        ]

        with (
            self._patched_store(),
            patch("config.repo_config.get_repo_checks", return_value=configured),
        ):
            payload = {
                "attestation": {
                    "tests_execution_blocked": True,
                    "checks_passed": ["test"],  # partial — would normally fail
                },
            }
            # Should not raise — blocked tester is exempt
            _validate_tester_check_coverage("pid-1", payload, Path("/tmp"))

    def test_rejected_proposal_does_not_mutate_tracker(self):
        """Integration: rejected tester proposal must not leave tracker in mutated state (#1459)."""
        from flask import Flask
        from routes.signals import handle_consensus_propose_signal

        configured = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
        ]

        mock_tracker = MagicMock()
        mock_tracker.handle_propose = MagicMock(return_value={"version": 1})

        app = Flask(__name__)
        with (
            app.app_context(),
            self._patched_store(),
            patch("config.repo_config.get_repo_checks", return_value=configured),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
        ):
            data = {
                "agent_role": "tester",
                "payload": {
                    "summary": "tests v1",
                    "artifacts": ["tests/test_a.py"],
                    "attestation": {"checks_passed": ["test"]},  # missing "lint"
                },
            }
            response, status_code = handle_consensus_propose_signal("pid-1", data, Path("/tmp"))
            # Should be rejected
            assert status_code == 400
            # Tracker.handle_propose must NOT have been called
            mock_tracker.handle_propose.assert_not_called()


class TestPlannerRoleAlignmentValidation:
    """Tests for ``_validate_planner_role_alignment`` in ``signals.py`` (#2527).

    Exercises the production code path the original PR-1 implementation
    couldn't reach: in concurrent BRC mode, ``_run_concurrent_phase``
    builds every reviewer prompt up-front before the planner has produced
    the plan, so a prompt-time validator can never fire on the first
    cycle. This validator runs at ``CONSENSUS_PROPOSE`` instead — by
    that point the planner has pushed the plan to origin, and the
    orchestrator reads it via ``git show <commit>:<plan_path>``.
    """

    _PLAN_WITH_MISASSIGNED_TASK = (
        "# Plan\n"
        "\n"
        "```yaml\n"
        "# yaml-tasks\n"
        "slices:\n"
        "  - id: 1\n"
        "    name: Setup\n"
        "    goal: scaffolding\n"
        "    tasks:\n"
        "      - id: TASK-1-1\n"
        "        description: Add pytest fixtures\n"
        "        acceptance: fixtures load\n"
        "        role: coder\n"
        "        files:\n"
        "          - integration_tests/conftest.py\n"
        "```\n"
    )

    _PLAN_WITH_CLEAN_ASSIGNMENTS = (
        "# Plan\n"
        "\n"
        "```yaml\n"
        "# yaml-tasks\n"
        "slices:\n"
        "  - id: 1\n"
        "    name: Setup\n"
        "    goal: scaffolding\n"
        "    tasks:\n"
        "      - id: TASK-1-1\n"
        "        description: Add pytest fixtures\n"
        "        acceptance: fixtures load\n"
        "        role: tester\n"
        "        files:\n"
        "          - integration_tests/conftest.py\n"
        "```\n"
    )

    @staticmethod
    def _patched_store(issue_number: int | None = 2527, branch: str = "egg/issue-2527"):
        mock_pipeline = MagicMock()
        mock_pipeline.issue_number = issue_number
        mock_pipeline.branch = branch
        mock_pipeline.mode = None
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        return patch("routes.signals.get_state_store", return_value=mock_store)

    @staticmethod
    def _patched_subprocess(plan_text: str, returncode: int = 0):
        result = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=plan_text, stderr=""
        )
        return patch("routes.signals.subprocess.run", return_value=result)

    @staticmethod
    def _patched_worktree():
        return patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))

    def test_skips_when_commit_sha_missing(self):
        """No commit SHA on payload → nothing to validate against."""
        from routes.signals import _validate_planner_role_alignment

        # Should not raise even with no other patches in place — the
        # bail-out happens before any state-store / git access.
        _validate_planner_role_alignment("issue-2527", {"payload": {}}, Path("/tmp"))
        _validate_planner_role_alignment("issue-2527", {"commit_sha": ""}, Path("/tmp"))

    def test_rejects_misassigned_plan_at_propose_time(self):
        """Planner pushed a plan with coder→test-files: validator raises."""
        from routes.signals import _validate_planner_role_alignment

        with (
            self._patched_store(),
            self._patched_worktree(),
            self._patched_subprocess(self._PLAN_WITH_MISASSIGNED_TASK),
        ):
            payload = {"commit_sha": "abc1234"}
            with pytest.raises(ValueError, match="role↔files alignment violations"):
                _validate_planner_role_alignment("issue-2527", payload, Path("/tmp/repo"))

    def test_accepts_clean_plan(self):
        """Planner pushed a plan with correctly-assigned roles: no raise."""
        from routes.signals import _validate_planner_role_alignment

        with (
            self._patched_store(),
            self._patched_worktree(),
            self._patched_subprocess(self._PLAN_WITH_CLEAN_ASSIGNMENTS),
        ):
            payload = {"commit_sha": "abc1234"}
            # Should not raise.
            _validate_planner_role_alignment("issue-2527", payload, Path("/tmp/repo"))

    def test_skips_when_git_show_fails(self):
        """``git show`` non-zero exit (plan absent at commit) → graceful skip."""
        from routes.signals import _validate_planner_role_alignment

        with (
            self._patched_store(),
            self._patched_worktree(),
            self._patched_subprocess("", returncode=128),
        ):
            payload = {"commit_sha": "abc1234"}
            # Should not raise.
            _validate_planner_role_alignment("issue-2527", payload, Path("/tmp/repo"))

    def test_skips_when_pipeline_lookup_fails(self):
        """State store load failure → graceful skip."""
        from routes.signals import _validate_planner_role_alignment
        from state_store import StateValidationError

        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = StateValidationError("corrupt state")

        with (
            patch("routes.signals.get_state_store", return_value=mock_store),
            self._patched_worktree(),
        ):
            payload = {"commit_sha": "abc1234"}
            # Should not raise — graceful degradation
            _validate_planner_role_alignment("issue-2527", payload, Path("/tmp/repo"))

    def test_skips_when_pipeline_has_no_branch(self):
        """A pipeline with ``branch=None`` → graceful skip (no git context)."""
        from routes.signals import _validate_planner_role_alignment

        with (
            self._patched_store(branch=None),
            self._patched_worktree(),
        ):
            payload = {"commit_sha": "abc1234"}
            # Should not raise — branch is required to resolve the worktree commit.
            _validate_planner_role_alignment("issue-2527", payload, Path("/tmp/repo"))

    def test_rejected_proposal_does_not_mutate_tracker(self):
        """Integration: a planner proposal carrying a misassigned plan
        is rejected at ``handle_consensus_propose_signal`` BEFORE the
        tracker is mutated — same guarantee as
        ``test_rejected_proposal_does_not_mutate_tracker`` for
        testers (#1459).

        This is the production-sequence end-to-end test the PR-1 review
        flagged as missing: it builds the propose signal exactly the
        way the planner agent does in concurrent BRC mode, mocks
        ``git show`` to return the misassigned plan (the file the
        orchestrator's worktree would read at the proposed commit),
        and asserts the tracker is left untouched.
        """
        from flask import Flask
        from routes.signals import handle_consensus_propose_signal

        mock_tracker = MagicMock()
        mock_tracker.handle_propose = MagicMock(return_value={"version": 1})

        # Two subprocess calls happen in this path:
        #   1. _verify_commit_on_branch's git fetch
        #   2. _verify_commit_on_branch's git branch --contains
        #   3. _validate_planner_role_alignment's git show
        # The first two return success; the third returns the misassigned plan.
        side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="  origin/egg/issue-2527\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=self._PLAN_WITH_MISASSIGNED_TASK,
                stderr="",
            ),
        ]

        app = Flask(__name__)
        with (
            app.app_context(),
            self._patched_store(),
            self._patched_worktree(),
            patch("routes.signals.subprocess.run", side_effect=side_effect),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
        ):
            data = {
                "agent_role": "task_planner",
                "payload": {
                    "summary": (
                        "Plan v1: 1 slice / 1 task with task-1-1 assigned "
                        "(coder) for the integration_tests fixture work"
                    ),
                    "artifacts": [".egg-state/drafts/2527-plan.md"],
                    "commit_sha": "abc1234",
                },
            }
            response, status_code = handle_consensus_propose_signal(
                "issue-2527", data, Path("/tmp/repo")
            )
            # Rejected with 400 (ValueError → make_error_response 400).
            assert status_code == 400
            data_out = response.get_json()
            assert "role↔files alignment violations" in data_out.get("message", "")
            # Tracker.handle_propose must NOT have been called — the
            # validator runs BEFORE the tracker, so a rejected proposal
            # never mutates tracker state. This is the regression
            # guarantee the PR-1 review flagged as the missing
            # production-sequence test.
            mock_tracker.handle_propose.assert_not_called()


class TestReadTesterGapsNamespacedEdgeCases:
    """Additional edge-case tests for _read_tester_gaps with identifier."""

    def test_no_identifier_uses_global_ignores_prefixed(self, tmp_path):
        """When identifier=None, only global file is used even if prefixed exists."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-tester-output.json").write_text(
            json.dumps({"tests_failed": 5, "gaps_found": ["prefixed-gap"]})
        )
        (outputs_dir / "tester-output.json").write_text(
            json.dumps({"tests_failed": 1, "gaps_found": ["global-gap"]})
        )

        result = _read_tester_gaps(tmp_path, identifier=None)
        assert result is not None
        assert "global-gap" in result
        assert "prefixed-gap" not in result

    def test_neither_file_with_identifier_returns_none(self, tmp_path):
        """Returns None when identifier given but neither file exists."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        # No files at all

        result = _read_tester_gaps(tmp_path, identifier=871)
        assert result is None

    def test_corrupted_prefixed_falls_back_to_none(self, tmp_path):
        """Corrupted prefixed file returns None (no fallback to global in this function)."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        # Prefixed file is corrupt
        (outputs_dir / "871-tester-output.json").write_text("NOT JSON{{{")

        # _read_tester_gaps selects the prefixed file because it exists,
        # then fails to parse → returns None.
        result = _read_tester_gaps(tmp_path, identifier=871)
        assert result is None

    def test_string_identifier_prefixed_file(self, tmp_path):
        """String identifiers work for _read_tester_gaps."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "local-xyz-tester-output.json").write_text(
            json.dumps({"tests_failed": 2, "gaps_found": ["gap-a"]})
        )

        result = _read_tester_gaps(tmp_path, identifier="local-xyz")
        assert result is not None
        assert "gap-a" in result


class TestSynthesizePlanDraftNamespaced:
    """Tests for _synthesize_plan_draft with namespaced agent output filenames."""

    def test_reads_prefixed_agent_outputs(self, tmp_path):
        """Reads {identifier}-architect-output.json when present."""
        # Set up draft path structure
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)

        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-architect-output.json").write_text(
            json.dumps({"content": "Architecture analysis for issue 871"})
        )
        (outputs_dir / "871-risk_analyst-output.json").write_text(
            json.dumps({"content": "Risk assessment for issue 871"})
        )

        _synthesize_plan_draft(
            repo_path=tmp_path,
            pipeline_id="issue-871",
            pipeline_mode="issue",
            issue_number=871,
        )

        draft_path = tmp_path / ".egg-state" / "drafts" / "871-plan.md"
        assert draft_path.exists()
        content = draft_path.read_text()
        assert "Architecture analysis for issue 871" in content
        assert "Risk assessment for issue 871" in content

    def test_falls_back_to_global_agent_outputs(self, tmp_path):
        """Falls back to global filenames when prefixed files missing."""
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)

        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        # Only global files exist — content must exceed _MIN_PLAN_DRAFT_CONTENT_LENGTH (50)
        long_content = "Global architecture analysis with detailed design decisions and component interactions for the feature"
        (outputs_dir / "architect-output.json").write_text(json.dumps({"content": long_content}))

        _synthesize_plan_draft(
            repo_path=tmp_path,
            pipeline_id="issue-871",
            pipeline_mode="issue",
            issue_number=871,
        )

        draft_path = tmp_path / ".egg-state" / "drafts" / "871-plan.md"
        assert draft_path.exists()
        content = draft_path.read_text()
        assert long_content in content

    def test_prefixed_preferred_over_global(self, tmp_path):
        """When both prefixed and global exist, prefixed wins."""
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)

        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "architect-output.json").write_text(
            json.dumps(
                {
                    "content": "Old global architecture output that should be ignored when prefixed version is available"
                }
            )
        )
        (outputs_dir / "871-architect-output.json").write_text(
            json.dumps(
                {
                    "content": "New prefixed architecture output with detailed design decisions and component interactions"
                }
            )
        )

        _synthesize_plan_draft(
            repo_path=tmp_path,
            pipeline_id="issue-871",
            pipeline_mode="issue",
            issue_number=871,
        )

        draft_path = tmp_path / ".egg-state" / "drafts" / "871-plan.md"
        assert draft_path.exists()
        content = draft_path.read_text()
        assert "New prefixed architecture output" in content
        assert "Old global architecture output" not in content

    def test_does_not_overwrite_existing_draft(self, tmp_path):
        """Existing draft is not overwritten by synthesis."""
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        draft_path = drafts_dir / "871-plan.md"
        draft_path.write_text("Existing plan from task_planner")

        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-architect-output.json").write_text(
            json.dumps({"content": "Architecture from architect"})
        )

        _synthesize_plan_draft(
            repo_path=tmp_path,
            pipeline_id="issue-871",
            pipeline_mode="issue",
            issue_number=871,
        )

        assert draft_path.read_text() == "Existing plan from task_planner"

    def test_uses_pipeline_id_without_issue_number(self, tmp_path):
        """Without issue_number, uses pipeline_id for both draft path and output lookup."""
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)

        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        long_content = "Architecture analysis with detailed design decisions, component interactions, and implementation strategy"
        (outputs_dir / "local-abc-architect-output.json").write_text(
            json.dumps({"content": long_content})
        )

        _synthesize_plan_draft(
            repo_path=tmp_path,
            pipeline_id="local-abc",
            pipeline_mode="issue",
            issue_number=None,
        )

        draft_path = tmp_path / ".egg-state" / "drafts" / "local-abc-plan.md"
        assert draft_path.exists()
        content = draft_path.read_text()
        assert long_content in content

    def test_no_outputs_dir_no_crash(self, tmp_path):
        """No agent-outputs directory does not crash."""
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)

        # No agent-outputs directory at all
        _synthesize_plan_draft(
            repo_path=tmp_path,
            pipeline_id="issue-871",
            pipeline_mode="issue",
            issue_number=871,
        )

        draft_path = tmp_path / ".egg-state" / "drafts" / "871-plan.md"
        assert not draft_path.exists()

    def test_empty_outputs_produce_no_draft(self, tmp_path):
        """Agent outputs with empty content do not produce a draft."""
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)

        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "871-architect-output.json").write_text(json.dumps({"content": ""}))
        (outputs_dir / "871-risk_analyst-output.json").write_text(json.dumps({"content": "   "}))

        _synthesize_plan_draft(
            repo_path=tmp_path,
            pipeline_id="issue-871",
            pipeline_mode="issue",
            issue_number=871,
        )

        draft_path = tmp_path / ".egg-state" / "drafts" / "871-plan.md"
        # No meaningful content → draft not written
        assert not draft_path.exists()


class TestBuildReviewPrompt:
    """Tests for _build_review_prompt verdict format, conventions, and preambles."""

    def test_verdict_format_includes_analysis_and_suggestions(self):
        """Verdict JSON template includes analysis and suggestions fields."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert '"analysis"' in prompt
        assert '"suggestions"' in prompt

    def test_verdict_format_no_empty_if_approved(self):
        """The old 'empty if approved' language is not present."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "empty if approved" not in prompt

    def test_review_conventions_present(self):
        """Generated prompt contains a Review Conventions section with quality standards."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "## Review Conventions" in prompt
        assert "Be comprehensive" in prompt
        assert "Be specific" in prompt
        assert "Be direct" in prompt
        assert "Suggest fixes" in prompt
        assert "Provide context" in prompt

    def test_code_reviewer_conventions_include_infrastructure_framing(self):
        """Code reviewer prompt includes 'critical infrastructure' framing."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "critical" in prompt.lower()
        assert "last line of defense" in prompt

    def test_agent_design_preamble_allows_brief_approval(self):
        """Agent-design reviewer preamble does not require detailed analysis."""
        preamble = _get_reviewer_scope_preamble("agent-design", "implement")
        assert "brief approval is acceptable" in preamble

    def test_code_preamble_includes_file_by_file(self):
        """Code reviewer preamble includes file-by-file analysis expectation."""
        preamble = _get_reviewer_scope_preamble("code", "implement")
        assert "file-by-file" in preamble

    def test_contract_preamble_includes_criterion_verification(self):
        """Contract reviewer preamble includes criterion-by-criterion verification."""
        preamble = _get_reviewer_scope_preamble("contract", "implement")
        assert "criterion-by-criterion" in preamble

    def test_refine_preamble_includes_section_evaluation(self):
        """Refine reviewer preamble includes section-by-section evaluation."""
        preamble = _get_reviewer_scope_preamble("refine", "refine")
        assert "section-by-section" in preamble

    def test_plan_preamble_includes_section_evaluation(self):
        """Plan reviewer preamble includes section-by-section evaluation."""
        preamble = _get_reviewer_scope_preamble("plan", "plan")
        assert "section-by-section" in preamble

    def test_analysis_field_guidelines(self):
        """Prompt includes guidance to always provide analysis regardless of verdict."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "Always provide detailed analysis regardless of verdict" in prompt

    def test_draft_reviewer_has_expanded_steps(self):
        """Draft-based reviewers get expanded procedural steps (6+)."""
        prompt = _build_review_prompt(
            phase="refine",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="refine",
            issue_number=100,
        )
        # Draft-based reviewer should have cross-reference and cite steps
        assert "Cross-reference" in prompt
        assert "Cite specific" in prompt
        assert "completeness" in prompt.lower()

    def test_non_code_reviewer_generic_conventions_framing(self):
        """Non-code reviewers get generic quality standards framing, not infrastructure framing."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="contract",
            issue_number=100,
        )
        assert "## Review Conventions" in prompt
        assert "Your review must meet these quality standards" in prompt
        # Should NOT have the code-specific infrastructure framing
        assert "last line of defense" not in prompt

    def test_contract_reviewer_phase_restrictions_include_contract_write(self):
        """Contract reviewer gets extra permission to update contract files."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="contract",
            issue_number=100,
        )
        assert "CAN update the contract" in prompt

    def test_code_reviewer_phase_restrictions_no_contract_write(self):
        """Code reviewer does NOT get contract write permission."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "CAN update the contract" not in prompt

    def test_delta_review_directive(self):
        """Re-review with last_reviewed_commit includes delta review section.

        See issue #1758: the delta directive now uses
        `git log <sha>..HEAD --not origin/<base> -p` (which excludes merged-in
        base-branch commits) instead of the old two-dot `git diff <sha>..HEAD`
        form, and adds a `git fetch origin <base>` nudge.
        """
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            review_cycle=2,
            last_reviewed_commit="abc123",
        )
        assert "## Delta Review" in prompt
        # New base-excluding command form appears in the directive
        assert "git log abc123..HEAD --not origin/main -p" in prompt
        # Shallow-checkout nudge
        assert "git fetch origin main" in prompt
        assert "cycle 2" in prompt
        # Old two-dot diff form must not appear on the delta path
        assert "git diff abc123..HEAD" not in prompt

    def test_first_review_no_delta_section(self):
        """First review cycle does not include delta review section."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            review_cycle=1,
        )
        assert "## Delta Review" not in prompt

    def test_prior_feedback_section(self):
        """Re-review with prior feedback includes prior feedback section."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            review_cycle=2,
            prior_feedback="Fix the SQL injection vulnerability in query.py",
        )
        assert "## Prior Review Feedback" in prompt
        assert "SQL injection vulnerability in query.py" in prompt

    def test_prior_feedback_not_shown_on_first_cycle(self):
        """Prior feedback is not shown on first review cycle even if provided."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            review_cycle=1,
            prior_feedback="Fix the bug",
        )
        assert "## Prior Review Feedback" not in prompt

    def test_pipeline_id_in_verdict_path_without_issue(self):
        """Uses pipeline_id in verdict path when no issue number is provided."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="local-abc12345",
            pipeline_mode="issue",
            reviewer_type="code",
        )
        assert "local-abc12345" in prompt
        # Should not contain an issue number reference in the verdict path
        assert "None-implement" not in prompt

    def test_non_code_non_draft_reviewer_short_steps(self):
        """Non-code reviewer in implement phase (no draft) gets short procedural steps."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="contract",
            issue_number=100,
        )
        # Contract reviewer in implement phase has no draft_path, so gets the short steps
        # It should NOT have code reviewer's extended steps (like "Trace data flow")
        assert "Trace data flow" not in prompt
        # And not the draft-based expanded steps
        assert "Cross-reference" not in prompt
        # But should still have basic evaluation steps
        assert "Evaluate it against the criteria below" in prompt

    def test_unknown_reviewer_type_raises_error(self):
        """Unknown reviewer type raises ValueError in preamble."""
        with pytest.raises(ValueError, match="Unknown reviewer type"):
            _get_reviewer_scope_preamble("unknown-type", "implement")

    def test_feedback_field_guideline_blocking_only(self):
        """Feedback field guideline makes clear it's for blocking issues only."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "blocking issues only" in prompt.lower()
        assert "Leave empty when approving" in prompt

    def test_suggestions_field_guideline_even_when_approving(self):
        """Suggestions field guideline encourages providing them even when approving."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "even when approving" in prompt.lower()

    def test_code_reviewer_has_systematic_steps(self):
        """Code reviewer in implement phase gets detailed systematic review steps."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "review every changed file systematically" in prompt
        assert "Trace data flow" in prompt
        assert "edge cases" in prompt.lower()
        assert "Research when uncertain" in prompt

    def test_code_reviewer_includes_external_research(self):
        """Code reviewer prompt includes WebSearch/WebFetch instructions."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "WebSearch" in prompt
        assert "WebFetch" in prompt

    # --- Concurrent mode tests ---

    def test_concurrent_reviewer_omits_verdict_format(self):
        """Concurrent reviewer prompt does not include verdict JSON template."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            concurrent=True,
        )
        assert "## Verdict Format" not in prompt
        assert '"verdict"' not in prompt
        assert ".egg-state/reviews/" not in prompt

    def test_concurrent_reviewer_omits_verdict_phase_restriction(self):
        """Concurrent reviewer does not get 'CAN write verdict files' restriction."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            concurrent=True,
        )
        assert "CAN write verdict files" not in prompt

    def test_concurrent_reviewer_uses_ack_nack_in_steps(self):
        """Concurrent code reviewer procedural steps reference ACK/NACK, not verdict file."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            concurrent=True,
        )
        assert "ACK/NACK" in prompt
        assert "Commit the verdict file" not in prompt

    def test_concurrent_reviewer_uses_nack_ack_labels(self):
        """Concurrent code reviewer verdict classification uses NACK/ACK labels."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            concurrent=True,
        )
        assert "NACK for" in prompt
        assert "ACK for" in prompt

    def test_sequential_reviewer_still_has_verdict_format(self):
        """Sequential (non-concurrent) reviewer still gets verdict JSON template."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            concurrent=False,
        )
        assert "## Verdict Format" in prompt
        assert '"analysis"' in prompt
        assert ".egg-state/reviews/" in prompt

    def test_concurrent_non_code_reviewer_omits_verdict(self):
        """Concurrent contract reviewer also omits verdict file."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="contract",
            issue_number=100,
            concurrent=True,
        )
        assert "## Verdict Format" not in prompt
        assert "ACK/NACK" in prompt

    def test_word_cap_removed_from_analysis_guidelines(self):
        """Sequential reviewer analysis guidelines no longer include 200-500 word cap."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            concurrent=False,
        )
        assert "200-500 words" not in prompt


class TestExternalResearchInstructions:
    """Verify all roles include WebSearch/WebFetch external research instructions."""

    def test_refine_prompt_includes_external_research(self):
        """Refine phase prompt includes external research instructions."""
        prompt = _build_phase_prompt(
            phase="refine",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Analyze this issue.",
            issue_number=100,
        )
        assert "WebSearch" in prompt
        assert "WebFetch" in prompt

    def test_coder_prompt_includes_external_research(self):
        """Coder (implement phase) prompt includes external research instructions."""
        prompt = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Implement this feature.",
            issue_number=100,
        )
        assert "WebSearch" in prompt
        assert "WebFetch" in prompt

    def test_architect_prompt_includes_external_research(self):
        """Architect agent prompt includes external research instructions."""
        prompt = _build_agent_prompt(
            role_value="architect",
            phase="plan",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Design the architecture.",
            issue_number=100,
        )
        assert "WebSearch" in prompt
        assert "WebFetch" in prompt

    def test_risk_analyst_prompt_includes_external_research(self):
        """Risk analyst agent prompt includes external research instructions."""
        prompt = _build_agent_prompt(
            role_value="risk_analyst",
            phase="plan",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Assess the risks.",
            issue_number=100,
        )
        assert "WebSearch" in prompt
        assert "WebFetch" in prompt

    def test_tester_prompt_includes_external_research(self):
        """Tester agent prompt includes external research instructions."""
        prompt = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Test the implementation.",
            issue_number=100,
        )
        assert "WebSearch" in prompt
        assert "WebFetch" in prompt

    def test_documenter_prompt_includes_external_research(self):
        """Documenter agent prompt includes external research instructions."""
        prompt = _build_agent_prompt(
            role_value="documenter",
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Document the changes.",
            issue_number=100,
        )
        assert "WebSearch" in prompt
        assert "WebFetch" in prompt


class TestRefinePromptHonorsAdditionalContext:
    """Refine prompt must instruct refiner to skip already-resolved questions.

    Regression for #2481: when the SDLC skill's pre-refine HITL captures
    answers and embeds them under `## Additional Context` in the task
    description, the refiner used to re-register those answered questions
    as `register_open_question` decisions, wasting turns and producing
    no-op decisions that the skill auto-resolves.
    """

    def test_refine_prompt_warns_against_re_registering_resolved_questions(self):
        prompt = _build_phase_prompt(
            phase="refine",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Analyze this issue.\n\n## Additional Context\n\nAlready answered.",
            issue_number=100,
        )
        assert "Additional Context" in prompt
        assert "already decided" in prompt or "already-resolved" in prompt.lower()
        assert "pre-refine" in prompt
        assert "Skip already-resolved questions" in prompt
        assert "### Resolved in Pre-Refine" in prompt


class TestRefinePromptTemplateFenceSeparation:
    """Refine prompt must keep template literal and meta-instructions separate.

    Regression for #2500: the refiner used to see a single ```markdown fence
    that mixed the analysis-document skeleton with meta-guidance about how to
    register questions via `egg-contract`. The risk is the agent transcribes
    the meta-paragraphs (e.g. the `egg-contract add-decision` bash example,
    the **DO NOT:** list, the "Skip already-resolved questions" guidance)
    verbatim into its analysis document. The fix splits the prompt: only the
    template skeleton lives inside the fence; the registration protocol
    lives in a clearly-labelled `## How to Populate Open Questions` section
    after the fence closes.
    """

    @staticmethod
    def _refine_prompt() -> str:
        return _build_phase_prompt(
            phase="refine",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Analyze this issue.",
            issue_number=100,
        )

    @staticmethod
    def _template_fence_body(prompt: str) -> str:
        # The template uses a four-backtick fence so its inner code blocks
        # (if any) don't terminate it. Find the body between the opening
        # ````markdown line and the next standalone ```` line.
        open_marker = "````markdown"
        open_idx = prompt.find(open_marker)
        assert open_idx != -1, "expected ````markdown opening fence in refine prompt"
        body_start = open_idx + len(open_marker)
        close_idx = prompt.find("\n````", body_start)
        assert close_idx != -1, "expected ```` closing fence after template body"
        return prompt[body_start:close_idx]

    def test_template_fence_holds_only_skeleton(self):
        body = self._template_fence_body(self._refine_prompt())
        # Skeleton headings stay inside the fence.
        assert "# Analysis: [Issue Title]" in body
        assert "## Problem Statement" in body
        assert "## Open Questions" in body
        assert "*Authored-by: egg*" in body

    def test_meta_instructions_live_outside_template_fence(self):
        prompt = self._refine_prompt()
        body = self._template_fence_body(prompt)
        # Meta-paragraphs (and the bash examples) must NOT live inside the
        # template fence — that is what tempts the refiner to transcribe
        # them. They must still appear elsewhere in the prompt.
        for needle in (
            "egg-contract add-decision",
            "egg-contract add-feedback",
            "**DO NOT:**",
            "Skip already-resolved questions",
            "Surface **all** uncertainties",
            "**Multiple-choice questions**",
            "**Open-ended questions**",
            "Transcribe this `## How to Populate Open Questions` section",
        ):
            assert needle not in body, (
                f"meta-instruction {needle!r} leaked into template fence — "
                "the refiner may transcribe it into its analysis document"
            )
            assert needle in prompt, f"meta-instruction {needle!r} missing from prompt entirely"

    def test_prompt_labels_meta_section_distinctly(self):
        prompt = self._refine_prompt()
        # A clearly-named section header for the meta-guidance lets the
        # refiner tell template content from registration protocol.
        assert "## How to Populate Open Questions" in prompt
        # The Open Questions placeholder inside the template should point
        # the refiner at that section instead of restating the protocol.
        body = self._template_fence_body(prompt)
        assert "How to Populate Open Questions" in body


class TestRefinePromptSliceDagFraming:
    """Refine prompt must frame work-decomposition decisions in slice-DAG terms.

    Regression for #2584: refiner used to register multi-part work-decomposition
    decisions with options framed as PR count ("Two PRs: E first, then A+F",
    "Three sequential PRs: E -> A -> F"). In egg, slices are the
    work-decomposition primitive — each slice has its own branch + BRC consensus
    + PR, and sibling slices in a wave run in parallel under the slice scheduler.
    Slice count = PR count by construction, so the decision should name the
    slice-DAG shape and annotate the PR consequence in parentheses; "N sequential
    PRs" is doubly wrong because it forces serialization the scheduler does not
    require.
    """

    @staticmethod
    def _refine_prompt() -> str:
        return _build_phase_prompt(
            phase="refine",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Analyze this issue.",
            issue_number=100,
        )

    def test_prompt_introduces_work_decomposition_section(self):
        prompt = self._refine_prompt()
        assert "Work-decomposition decisions" in prompt

    def test_prompt_frames_decomposition_on_slices_not_pr_count(self):
        prompt = self._refine_prompt()
        # Slice-DAG vocabulary must appear in the work-decomposition guidance.
        for needle in (
            "decomposition primitive",
            "decomposed into slices",
            "slice-dag.md",
            "in parallel",
        ):
            assert needle in prompt, f"slice-DAG framing token {needle!r} missing"

    def test_prompt_provides_slice_shaped_example_options(self):
        prompt = self._refine_prompt()
        # The worked egg-contract add-decision example should show options
        # framed on slice-DAG shape with the PR count as an annotation.
        assert "Single slice: all parts ship together (1 PR)" in prompt
        assert "Two slices in parallel: [A] || [B+C] (2 PRs)" in prompt
        assert "Two slices with dependency: [A] -> [B] (2 PRs)" in prompt
        assert "Three slices fully parallel: [A], [B], [C] (3 PRs)" in prompt

    def test_prompt_warns_against_sequential_pr_framing(self):
        prompt = self._refine_prompt()
        # "N sequential PRs" framing must be explicitly called out as wrong.
        assert '"N sequential PRs"' in prompt
        assert "the slice scheduler does not require" in prompt

    def test_decomposition_guidance_lives_outside_template_fence(self):
        prompt = self._refine_prompt()
        body = TestRefinePromptTemplateFenceSeparation._template_fence_body(prompt)
        # The decomposition guidance is meta-protocol — it must not leak
        # into the analysis-document template body that the refiner copies.
        for needle in (
            "Work-decomposition decisions",
            "decomposition primitive",
            "Single slice: all parts ship together (1 PR)",
            "the slice scheduler does not require",
        ):
            assert needle not in body, (
                f"decomposition guidance {needle!r} leaked into template "
                "fence — refiner may transcribe it into the analysis document"
            )


class TestPlannerPromptSliceDagFraming:
    """Planner prompts must not contradict slice-DAG decomposition (#2601).

    Both planner paths — ``_build_phase_prompt(phase="plan")`` (sequential)
    and ``_build_agent_prompt(role_value="task_planner")`` (concurrent) —
    used to open with ``CRITICAL CONSTRAINT — One Issue = One Workflow =
    One PR`` and a follow-on ``do NOT propose multiple PRs`` line. That
    directly contradicts the slice-DAG guidance the concurrent path already
    carried and silently turned multi-slice refine-phase HITL decisions
    into dead letters. This class is a negative-regression suite: the
    opener must not return, and the slice-DAG framing must remain.
    """

    @staticmethod
    def _sequential_plan_prompt() -> str:
        return _build_phase_prompt(
            phase="plan",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Implement the change.",
            issue_number=100,
        )

    @staticmethod
    def _concurrent_planner_prompt() -> str:
        return _build_agent_prompt(
            role_value="task_planner",
            phase="plan",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="Implement the change.",
            issue_number=100,
            concurrent=True,
        )

    def test_sequential_plan_drops_one_pr_opener(self):
        prompt = self._sequential_plan_prompt()
        for needle in (
            "CRITICAL CONSTRAINT",
            "One Issue = One Workflow = One PR",
            "do NOT propose multiple PRs",
        ):
            assert needle not in prompt, (
                f"sequential planner still carries removed opener {needle!r} — "
                "this contradicts slice-DAG decomposition (#2601)"
            )

    def test_concurrent_planner_drops_one_pr_opener(self):
        prompt = self._concurrent_planner_prompt()
        for needle in (
            "CRITICAL CONSTRAINT",
            "One Issue = One Workflow = One PR",
            "do NOT propose multiple PRs",
        ):
            assert needle not in prompt, (
                f"concurrent planner still carries removed opener {needle!r} — "
                "this contradicts slice-DAG decomposition (#2601)"
            )

    def test_sequential_plan_includes_slice_dag_guidance(self):
        prompt = self._sequential_plan_prompt()
        for needle in (
            "Slice-DAG guidance (#2137)",
            "stacked PR",
            "Forest constraint",
            "serialized_chain_order",
        ):
            assert needle in prompt, (
                f"sequential planner missing slice-DAG token {needle!r} — "
                "the two planner paths must stay aligned (#2601)"
            )

    def test_concurrent_planner_includes_slice_dag_guidance(self):
        prompt = self._concurrent_planner_prompt()
        for needle in (
            "Slice-DAG guidance (#2137)",
            "stacked PR",
            "Forest constraint",
            "serialized_chain_order",
        ):
            assert needle in prompt, (
                f"concurrent planner missing slice-DAG token {needle!r} — "
                "removing the One-PR opener must not have dropped the "
                "slice-DAG block (#2601)"
            )

    def test_sequential_plan_yaml_example_uses_slices_key(self):
        """The canonical YAML example must use ``slices:`` — agents copy the
        example verbatim, so leaving ``phases:`` in the example silently
        teaches the legacy key while the slice-DAG section says to prefer
        ``slices:`` (review feedback on #2607)."""
        prompt = self._sequential_plan_prompt()
        assert "\nslices:\n" in prompt, (
            "sequential planner YAML example must use 'slices:' as the "
            "canonical key (parser still accepts 'phases:' as a backward-"
            "compat alias, but new prompts should teach 'slices:')"
        )
        assert "\nphases:\n" not in prompt, (
            "sequential planner YAML example still uses 'phases:' — switch "
            "to 'slices:' to match the slice-DAG directive in the same prompt"
        )

    def test_concurrent_planner_yaml_example_uses_slices_key(self):
        """Concurrent planner's canonical YAML example must use ``slices:``
        (review feedback on #2607 — parallel to the sequential path)."""
        prompt = self._concurrent_planner_prompt()
        assert "\nslices:\n" in prompt, (
            "concurrent planner YAML example must use 'slices:' as the "
            "canonical key (parser still accepts 'phases:' as a backward-"
            "compat alias, but new prompts should teach 'slices:')"
        )
        assert "\nphases:\n" not in prompt, (
            "concurrent planner YAML example still uses 'phases:' — switch "
            "to 'slices:' to match the slice-DAG directive in the same prompt"
        )

    def test_sequential_plan_carries_worked_example_and_jaccard(self):
        """The sequential planner's slice-DAG block must now include the
        worked ``serialized_chain_order`` example and the Jaccard fallback
        heuristic, mirroring the concurrent path (review feedback on
        #2607 flagged the asymmetry as a likely copy-paste oversight)."""
        prompt = self._sequential_plan_prompt()
        for needle in (
            "Worked example",
            "Jaccard",
            "files_affected",
        ):
            assert needle in prompt, (
                f"sequential planner missing concurrent-mirror token "
                f"{needle!r} — the worked example + Jaccard fallback must "
                "be present in both planner paths (#2607)"
            )


class TestReviewerBrcPreamble:
    """Tests that reviewer agents receive BRC preamble in concurrent mode."""

    def test_reviewer_gets_brc_preamble_when_concurrent(self):
        """Reviewer prompt includes BRC consensus instructions in concurrent mode."""
        result = _build_agent_prompt(
            role_value="reviewer_code",
            phase="implement",
            pipeline_id="pid-brc",
            pipeline_mode="issue",
            issue_number=42,
            concurrent=True,
        )
        assert "consensus" in result.lower()
        assert "CONSENSUS_PROPOSE" in result or "consensus propose" in result.lower()
        assert "confirmed" in result.lower()

    def test_reviewer_no_brc_preamble_when_not_concurrent(self):
        """Reviewer prompt omits BRC consensus instructions in sequential mode."""
        result = _build_agent_prompt(
            role_value="reviewer_code",
            phase="implement",
            pipeline_id="pid-seq",
            pipeline_mode="issue",
            issue_number=42,
            concurrent=False,
        )
        # BRC preamble should not be present in sequential mode
        brc_preamble = _build_brc_preamble("reviewer_code", "implement")
        assert brc_preamble not in result

    def test_reviewer_agent_design_gets_brc_preamble(self):
        """reviewer_agent_design also receives BRC preamble in concurrent mode."""
        result = _build_agent_prompt(
            role_value="reviewer_agent_design",
            phase="implement",
            pipeline_id="pid-brc",
            pipeline_mode="issue",
            issue_number=42,
            concurrent=True,
        )
        assert "consensus" in result.lower()

    def test_reviewer_contract_gets_brc_preamble(self):
        """reviewer_contract also receives BRC preamble in concurrent mode."""
        result = _build_agent_prompt(
            role_value="reviewer_contract",
            phase="implement",
            pipeline_id="pid-brc",
            pipeline_mode="issue",
            issue_number=42,
            concurrent=True,
        )
        assert "consensus" in result.lower()

    def test_brc_preamble_no_do_not_inspect(self):
        """BRC preamble does not tell reviewers to avoid inspecting artifacts."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "Do NOT inspect" not in preamble

    def test_brc_preamble_has_structured_nack_format(self):
        """BRC preamble includes structured NACK format with Blocking section."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "### Blocking" in preamble
        assert "### Non-blocking" in preamble

    def test_brc_preamble_has_structured_ack_format(self):
        """BRC preamble includes structured ACK format guidance."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "ACK format" in preamble
        assert "Reviewed" in preamble

    def test_brc_preamble_reason_is_review(self):
        """BRC preamble tells reviewers their --reason IS the review."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "--reason" in preamble
        assert "full analysis" in preamble.lower()

    def test_reviewer_preparation_proactive_diff_review(self):
        """Code reviewer preparation encourages proactive diff review."""
        prep = _build_reviewer_preparation("reviewer_code", "implement")
        assert "Start reviewing immediately" in prep
        assert "git diff" in prep

    def test_brc_preamble_threads_branch_to_reviewer_preparation(self):
        """BRC preamble passes branch to reviewer preparation for reliable git commands."""
        preamble = _build_brc_preamble("reviewer_code", "implement", branch="egg/my-feature")
        assert "origin/egg/my-feature" in preamble


class TestAgentRoster:
    """Tests for _build_agent_roster — active agent listing in BRC preamble."""

    def test_roster_lists_all_roles(self):
        """Roster includes all provided roles."""
        roster = _build_agent_roster(["coder", "reviewer_code", "tester"], "coder", "implement")
        assert "coder" in roster
        assert "reviewer_code" in roster
        assert "tester" in roster

    def test_roster_marks_current_role(self):
        """Current agent's role is marked with (you)."""
        roster = _build_agent_roster(["coder", "reviewer_code", "tester"], "tester", "implement")
        assert "**tester** **(you)**" in roster
        # Other roles should not be marked
        assert "**coder** **(you)**" not in roster

    def test_roster_includes_role_descriptions(self):
        """Roster includes descriptions of what each role produces."""
        roster = _build_agent_roster(["coder", "reviewer_code"], "coder", "implement")
        assert "Implements code changes" in roster
        assert "Reviews code quality" in roster

    def test_roster_in_brc_preamble_for_implement(self):
        """BRC preamble for implement phase includes agent roster."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "Active Agents" in preamble
        assert "coder" in preamble
        assert "reviewer_code" in preamble

    def test_roster_in_brc_preamble_for_plan(self):
        """BRC preamble for plan phase includes agent roster."""
        preamble = _build_brc_preamble("architect", "plan")
        assert "Active Agents" in preamble
        assert "architect" in preamble

    def test_unknown_role_gets_generic_description(self):
        """Unknown roles get a generic fallback description."""
        roster = _build_agent_roster(["unknown_agent"], "unknown_agent", "implement")
        assert "unknown_agent" in roster
        assert "Executes assigned role" in roster

    def test_tester_roster_description_signals_adversarial_mandate(self):
        """The tester's roster description (visible to *every other* agent
        in the BRC preamble) names the adversarial-probing half of the
        mandate, not just the dual-role-also-reviews-coder framing.

        Review feedback on PR #2450 noted that the roster description
        changed from `"Writes and runs tests (dual role: also reviews
        coder)"` to a longer adversarial-probing description, but no test
        guarded the new text. Without this assertion, a future
        well-meaning edit could shorten the description back to the old
        defensive framing and the cross-agent visibility of the new
        mandate would silently regress — the coder/reviewer/etc. would
        stop seeing the adversarial signal in their preamble roster.
        """
        roster = _build_agent_roster(["coder", "reviewer_code", "tester"], "coder", "implement")
        assert "tester" in roster
        # The roster description names adversarial probing — the key
        # cross-agent signal that the tester is not just a coverage role.
        assert "adversarially probes" in roster
        # And keeps the dual-role disclosure so reviewers/coder still see
        # that the tester is wearing both hats.
        assert "dual role" in roster.lower()


class TestReviewerPreparation:
    """Tests for _build_reviewer_preparation — proactive prep instructions."""

    def test_code_reviewer_gets_proactive_review(self):
        """Code reviewer prep includes proactive diff review instructions."""
        prep = _build_reviewer_preparation("reviewer_code", "implement")
        assert "egg-contract show" in prep
        assert "git diff" in prep
        assert "Start reviewing immediately" in prep

    def test_code_reviewer_uses_branch_when_provided(self):
        """Code reviewer prep uses explicit branch name instead of shell subcommand."""
        prep = _build_reviewer_preparation("reviewer_code", "implement", branch="egg/my-feature")
        assert "origin/egg/my-feature" in prep
        assert "$(git branch --show-current)" not in prep

    def test_contract_reviewer_gets_acceptance_criteria(self):
        """Contract reviewer prep focuses on acceptance criteria."""
        prep = _build_reviewer_preparation("reviewer_contract", "implement")
        assert "acceptance criteria" in prep.lower()
        assert "egg-contract show" in prep

    def test_tester_gets_test_scaffolding(self):
        """Tester prep includes test scaffolding and edge case identification."""
        prep = _build_reviewer_preparation("tester", "implement")
        assert "edge case" in prep.lower()
        assert "test" in prep.lower()

    def test_plan_reviewer_gets_architecture_exploration(self):
        """Plan reviewer prep includes codebase architecture exploration."""
        prep = _build_reviewer_preparation("reviewer_plan", "plan")
        assert "codebase" in prep.lower() or "architecture" in prep.lower()

    def test_refine_reviewer_gets_feedback_focus(self):
        """Refine reviewer prep focuses on prior review feedback."""
        prep = _build_reviewer_preparation("reviewer_refine", "refine")
        assert "feedback" in prep.lower()

    def test_unknown_role_gets_generic_prep(self):
        """Unknown reviewer role gets generic preparation instructions."""
        prep = _build_reviewer_preparation("reviewer_unknown", "implement")
        assert "egg-contract show" in prep
        assert "Do NOT inspect producer artifacts" in prep

    def test_preparation_in_reviewer_lifecycle(self):
        """BRC preamble reviewer lifecycle includes PREPARE step."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "**PREPARE**" in preamble
        assert "egg-contract show" in preamble

    def test_code_reviewer_prep_callouts_documenter_no_op_implement(self):
        """Implement-phase reviewer_code prep names the documenter no-op
        path so a future refactor cannot silently drop the callout (#2444,
        review feedback on #2458).
        """
        prep = _build_reviewer_preparation("reviewer_code", "implement")
        assert "no_doc_changes_needed" in prep
        assert "#2444" in prep

    def test_code_reviewer_prep_callouts_documenter_no_op_babysit(self):
        """Babysit / PR-diff-aware reviewer_code prep also names the
        documenter no-op path. Both prompt branches must be covered —
        the documenter ships in babysit-mode rosters via
        ``get_roles_for_phase("implement", include_reviewers=True)`` so
        a missing callout here is a real reviewer-instruction gap (#2444,
        review feedback on #2458).
        """
        from models import PipelineMode

        prep = _build_reviewer_preparation(
            "reviewer_code", "implement", mode=PipelineMode.BABYSIT, pr_number=42
        )
        assert "no_doc_changes_needed" in prep
        assert "#2444" in prep


class TestProducerOrientation:
    """Tests for _build_producer_orientation — pre-work context gathering."""

    def test_coder_reads_contract_and_codebase(self):
        """Coder orientation includes contract reading and codebase exploration."""
        orient = _build_producer_orientation("coder", "implement", ["reviewer_code"])
        assert "egg-contract show" in orient
        assert "codebase" in orient.lower()
        assert "patterns" in orient.lower()

    def test_coder_knows_reviewers(self):
        """Coder orientation mentions who will review their work."""
        orient = _build_producer_orientation(
            "coder", "implement", ["reviewer_code", "reviewer_contract"]
        )
        assert "reviewer_code" in orient
        assert "reviewer_contract" in orient

    def test_tester_checks_test_infrastructure(self):
        """Tester orientation includes checking test infrastructure."""
        orient = _build_producer_orientation("tester", "implement", [])
        assert "test" in orient.lower()
        assert "edge case" in orient.lower()

    def test_tester_orientation_directs_scaffold_first(self):
        """Tester producer orientation tells tester to draft scaffolds before
        wait-loop on coder.

        Issue #2249: the scaffold-first instruction previously lived only in
        the reviewer-preparation block; the producer-orientation block (which
        is what tester reads while deciding whether to call wait-loop) had no
        such directive. Mirror it on the producer side so the comfort path
        (`wait-loop`) does not pull tester away from work it could do without
        coder output.
        """
        orient = _build_producer_orientation("tester", "implement", [])
        assert "scaffold" in orient.lower()
        assert "wait-loop" in orient.lower()
        # The directive must point at plan-derived scaffolding inputs so the
        # agent has a concrete starting point, not just a mandate.
        assert "tasks[].files" in orient
        assert "acceptance criteria" in orient.lower()

    def test_tester_orientation_contains_dual_mandate_pointer(self):
        """Tester orientation carries a brief dual-mandate pointer, NOT the
        full failing-test → NACK → HANDOFF instruction.

        Review feedback on PR #2450 flagged that the dual-mandate paragraph
        was duplicated nearly verbatim in `_build_producer_orientation` and
        the implement-phase tester role-task block, so a single-callsite
        edit could silently drift one copy. The fix keeps the full mandate
        in the role-task block and trims the orientation copy to a one-line
        pointer.

        This test guards the trim from creeping back: orientation must
        carry the brief two-fold signal but must NOT inline the full
        failing-test → NACK paragraph (which lives in the role-task block,
        guarded by `test_tester_prompt_contains_gap_finding_language`).
        """
        orient = _build_producer_orientation("tester", "implement", [])
        # Brief two-fold pointer survives in the orientation block.
        assert "mandate is two-fold" in orient
        assert "adversarial probing" in orient.lower()
        # Pointer must direct readers to the role-task block for the full
        # mandate — preserves the single-source-of-truth for the workflow
        # detail.
        assert "Your Task" in orient
        # The full failing-test → NACK paragraph must NOT be re-inlined in
        # orientation. If a future edit copies it back, this assertion
        # catches the drift before the prompt grows duplicated prose.
        assert "the NACK is the bug report" not in orient
        assert "naming the failing test" not in orient.lower()
        assert "HANDOFF to coder" not in orient

    def test_tester_orientation_directs_no_op_propose_for_refactor(self):
        """Tester orientation tells tester to use the no-op propose path on
        slices that warrant no new tests, instead of heartbeating forever
        and deadlocking BRC consensus (#2431).
        """
        orient = _build_producer_orientation("tester", "implement", [])
        assert "no_test_changes_needed" in orient
        # Must explicitly tell the tester they MUST propose even on no-op
        # slices — silent waiting is the bug.
        assert "MUST propose" in orient
        assert "deadlock" in orient.lower()

    def test_documenter_checks_doc_structure(self):
        """Documenter orientation includes checking documentation structure."""
        orient = _build_producer_orientation("documenter", "implement", [])
        assert "documentation" in orient.lower() or "doc" in orient.lower()

    def test_documenter_orientation_directs_no_op_propose_for_no_doc_surface(self):
        """Documenter orientation tells the documenter to use the no-op
        propose path on slices that warrant no doc updates, instead of
        heartbeating forever and deadlocking BRC consensus (#2444, mirror
        of #2431).
        """
        orient = _build_producer_orientation("documenter", "implement", [])
        assert "no_doc_changes_needed" in orient
        # Must explicitly tell the documenter they MUST propose even on
        # no-op slices — silent waiting is the bug.
        assert "MUST propose" in orient
        assert "deadlock" in orient.lower()

    def test_architect_explores_architecture(self):
        """Architect orientation includes architecture exploration."""
        orient = _build_producer_orientation("architect", "plan", ["reviewer_plan"])
        assert "architecture" in orient.lower()
        assert "reviewer_plan" in orient

    def test_refiner_reads_feedback(self):
        """Refiner orientation focuses on prior review feedback."""
        orient = _build_producer_orientation("refiner", "refine", ["reviewer_refine"])
        assert "feedback" in orient.lower()

    def test_unknown_role_gets_generic_orientation(self):
        """Unknown producer role gets generic orientation."""
        orient = _build_producer_orientation("custom_role", "implement", [])
        assert "egg-contract show" in orient

    def test_orientation_in_producer_lifecycle(self):
        """BRC preamble producer lifecycle includes ORIENT step."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "**ORIENT**" in preamble
        assert "egg-contract show" in preamble


class TestTesterTestVerificationPrompt:
    """Tests for tester test execution verification instructions (issue #1359)."""

    def test_tester_prompt_includes_test_verification_section(self):
        """Tester prompt includes test execution verification instructions."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "Test Execution Verification (CRITICAL)" in result
        assert "tests_execution_blocked" in result
        assert "TESTS UNVERIFIED" in result

    def test_tester_prompt_private_mode_warning(self):
        """Tester prompt in private mode includes network warning."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
            network_mode="private",
        )
        assert "Private network mode is active" in result
        assert "go mod download" in result

    def test_tester_prompt_public_mode_no_private_warning(self):
        """Tester prompt in public mode does not include private mode warning."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
            network_mode="public",
        )
        assert "Test Execution Verification (CRITICAL)" in result
        assert "Private network mode is active" not in result

    def test_reviewer_code_preparation_mentions_unverified_tests(self):
        """Code reviewer preparation includes tester attestation check."""
        result = _build_reviewer_preparation("reviewer_code", "implement")
        assert "tests_execution_blocked" in result
        assert "NACK" in result

    def test_non_tester_roles_exclude_test_verification_section(self):
        """Coder, documenter, and reviewer prompts do not include test verification."""
        for role in ("coder", "documenter", "reviewer_code"):
            result = _build_agent_prompt(
                role_value=role,
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature\n\nDetail.",
                issue_number=1,
            )
            assert "Test Execution Verification (CRITICAL)" not in result, (
                f"Test verification section leaked into {role} prompt"
            )


class TestFileBoundarySection:
    """Tests for _build_file_boundary_section helper (#1431)."""

    def test_coder_has_allowed_and_blocked(self):
        section = _build_file_boundary_section("coder")
        assert "File Boundaries" in section
        assert "CODER" in section
        assert "Allowed" in section
        assert "Blocked" in section

    def test_tester_has_test_patterns(self):
        section = _build_file_boundary_section("tester")
        assert "TESTER" in section
        assert "tests/" in section or "test/" in section

    def test_documenter_has_doc_patterns(self):
        section = _build_file_boundary_section("documenter")
        assert "DOCUMENTER" in section
        assert "*.md" in section

    def test_unknown_role_returns_empty(self):
        section = _build_file_boundary_section("nonexistent_role")
        assert section == ""

    def test_coder_prompt_includes_file_boundaries(self):
        """The coder agent prompt includes file boundary info."""
        result = _build_agent_prompt(
            role_value="coder",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "File Boundaries" in result

    def test_tester_prompt_includes_file_boundaries(self):
        """The tester agent prompt includes file boundary info."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "File Boundaries" in result

    def test_documenter_prompt_includes_file_boundaries(self):
        """The documenter agent prompt includes file boundary info."""
        result = _build_agent_prompt(
            role_value="documenter",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "File Boundaries" in result


class TestBuildRoleRestrictionsSection:
    """Tests for _build_role_restrictions_section() helper."""

    def test_includes_all_three_execution_roles(self):
        """Section includes coder, tester, documenter subsections."""
        section = _build_role_restrictions_section()
        assert "### coder" in section
        assert "### tester" in section
        assert "### documenter" in section

    def test_includes_allowed_and_blocked_patterns(self):
        """Section includes Allowed and Blocked labels."""
        section = _build_role_restrictions_section()
        assert "**Allowed**" in section
        assert "**Blocked**" in section

    def test_includes_role_assignment_guidance(self):
        """Section includes guidance about when to assign each role."""
        section = _build_role_restrictions_section()
        assert "role: tester" in section
        assert "role: documenter" in section
        assert "role: coder" in section
        assert "split it into separate tasks per role" in section

    def test_includes_header(self):
        """Section has 'Execution Role File Restrictions' header."""
        section = _build_role_restrictions_section()
        assert "## Execution Role File Restrictions" in section

    def test_includes_github_staging_convention(self):
        """Section documents the `.github-staging/` staging convention (issue #2508).

        Without this guidance, the planner schedules `.github/`-touching tasks
        that no producer role can actually push, leading to silent
        slice-failure as observed on the issue-2474 pipeline.

        Asserts the actionable guidance in addition to the bare strings, so
        a future refactor that drops the role-assignment instruction or the
        concrete staging-path example is caught here.
        """
        section = _build_role_restrictions_section()
        # Bare-string assertions — these tokens also occur in the
        # surrounding role-restrictions section (`.github/` is rendered
        # in the plan-phase blocked-write list, `.github-staging/` is
        # mentioned in the role-assignment paragraph, and `role: coder`
        # appears in the role-assignment instruction). They confirm the
        # tokens haven't been deleted from the section entirely but
        # don't pin the staging-dir prose specifically.
        assert ".github-staging/" in section
        assert ".github/" in section
        assert "role: coder" in section
        # Prose-pinning assertions: the concrete staging-path example
        # and the `.gitignore` warning only appear in the staging-dir
        # subsection. A refactor that drops the staging-dir prose will
        # break here even if the bare-string tokens above survive.
        assert ".github-staging/workflows/ci.yml" in section
        assert ".gitignore" in section


class TestTaskPlannerRoleRestrictions:
    """Tests for task_planner prompt including role restrictions."""

    def test_task_planner_prompt_includes_role_restrictions(self):
        """Task planner prompt contains Execution Role File Restrictions section."""
        result = _build_agent_prompt(
            role_value="task_planner",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
        )
        assert "Execution Role File Restrictions" in result

    def test_task_planner_yaml_example_includes_role_field(self):
        """Task planner prompt yaml-tasks example includes the role field."""
        result = _build_agent_prompt(
            role_value="task_planner",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
        )
        assert "role: coder" in result


class TestYamlTasksBlockScalars:
    """Regression tests for #1974 — planner prompts must demonstrate block
    scalars for prose fields so agents don't emit plain scalars that break
    on ``: `` sequences (e.g. `` `code: type` `` snippets)."""

    def test_task_planner_prompt_uses_block_scalars_for_prose_fields(self):
        result = _build_agent_prompt(
            role_value="task_planner",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
        )
        assert "description: |-" in result
        assert "acceptance: |-" in result
        assert "name: |-" in result
        assert "goal: |-" in result
        assert "YAML safety" in result

    def test_plan_phase_prompt_uses_block_scalars_for_prose_fields(self):
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
        )
        assert "description: |-" in result
        assert "acceptance: |-" in result
        assert "name: |-" in result
        assert "goal: |-" in result
        assert "YAML safety" in result


class TestReviewPromptBaseBranch:
    """Tests for base_branch parameter in _build_review_prompt (issue #1565)."""

    def test_base_branch_produces_correct_diff_command(self):
        """With base_branch='main', diff command uses 'origin/main...HEAD'."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            base_branch="main",
        )
        assert "origin/main...HEAD" in prompt
        assert "HEAD~10" not in prompt

    def test_base_branch_custom_branch(self):
        """With a custom base_branch, diff command uses that branch name."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            base_branch="develop",
        )
        assert "origin/develop...HEAD" in prompt
        assert "HEAD~10" not in prompt

    def test_no_base_branch_falls_back_to_origin_main(self):
        """Without base_branch, diff command falls back to 'origin/main...HEAD'."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "origin/main...HEAD" in prompt
        assert "HEAD~10" not in prompt

    def test_none_base_branch_falls_back_to_origin_main(self):
        """Explicit None base_branch falls back to 'origin/main...HEAD'."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            base_branch=None,
        )
        assert "origin/main...HEAD" in prompt
        assert "HEAD~10" not in prompt

    def test_delta_review_still_uses_commit_sha(self):
        """Delta review (cycle > 1 with last_reviewed_commit) uses commit SHA in the
        new base-excluding `git log` form (issue #1758).
        """
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            review_cycle=2,
            last_reviewed_commit="abc123def",
            base_branch="main",
        )
        # The commit SHA is still the left endpoint, but the command now excludes
        # commits reachable from origin/<base>
        assert "git log abc123def..HEAD --not origin/main -p" in prompt
        # Three-dot base_branch diff should NOT be the primary diff command for delta reviews
        assert "origin/main...HEAD" not in prompt
        # The old two-dot diff form is gone
        assert "git diff abc123def..HEAD" not in prompt

    def test_delta_review_with_non_default_base_branch(self):
        """Delta review threads a non-main base_branch (e.g. `develop`) through
        to both the diff command and the fetch nudge. Regression test for
        issue #1758: without correct threading, the command would default to
        `origin/main` and would still wrongly include develop-branch merges.
        """
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
            review_cycle=2,
            last_reviewed_commit="deadbeef",
            base_branch="develop",
        )
        # The base ref in the command must match the PR's actual base branch
        assert "git log deadbeef..HEAD --not origin/develop -p" in prompt
        # The fetch nudge in the Delta Review directive must also use develop
        assert "git fetch origin develop" in prompt
        # origin/main must NOT appear on the delta path when the base is develop
        assert "origin/main" not in prompt
        # Old two-dot diff form must be gone
        assert "git diff deadbeef..HEAD" not in prompt

    def test_delta_review_contract_reviewer_uses_base_branch(self):
        """Contract reviewer also threads base_branch through the delta command."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="contract",
            issue_number=100,
            review_cycle=2,
            last_reviewed_commit="cafef00d",
            base_branch="develop",
        )
        assert "git log cafef00d..HEAD --not origin/develop -p" in prompt
        assert "git fetch origin develop" in prompt
        assert "git diff cafef00d..HEAD" not in prompt

    def test_contract_reviewer_uses_base_branch(self):
        """Contract reviewer also uses base_branch for diff command."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="contract",
            issue_number=100,
            base_branch="main",
        )
        assert "origin/main...HEAD" in prompt
        assert "HEAD~10" not in prompt

    def test_code_reviewer_find_all_issues_emphasis(self):
        """Code reviewer prompt includes 'Find ALL issues' emphasis."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="code",
            issue_number=100,
        )
        assert "Find ALL issues on the first pass" in prompt

    def test_non_code_reviewer_no_find_all_issues(self):
        """Non-code reviewers do NOT get 'Find ALL issues' emphasis."""
        prompt = _build_review_prompt(
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            reviewer_type="contract",
            issue_number=100,
        )
        assert "Find ALL issues on the first pass" not in prompt

    def test_no_head_tilde_10_remains(self):
        """HEAD~10 should not appear in any review prompt path."""
        for reviewer_type in ("code", "contract"):
            for base_branch in (None, "main", "develop"):
                prompt = _build_review_prompt(
                    phase="implement",
                    pipeline_id="test-pipe",
                    pipeline_mode="issue",
                    reviewer_type=reviewer_type,
                    issue_number=100,
                    base_branch=base_branch,
                )
                assert "HEAD~10" not in prompt, (
                    f"HEAD~10 found with reviewer_type={reviewer_type}, base_branch={base_branch}"
                )


class TestRoleContextBaseBranch:
    """Tests for base_branch parameter in _build_role_context (issue #1565)."""

    def test_base_branch_produces_correct_diff_pointer(self):
        """With base_branch='main', context pointer uses 'origin/main...HEAD'."""
        result = _build_role_context(
            "tester", "# Issue\n\nBody.", issue_number=1, base_branch="main"
        )
        assert "origin/main...HEAD" in result
        assert "HEAD~10" not in result

    def test_custom_base_branch(self):
        """With custom base_branch, context pointer uses that branch name."""
        result = _build_role_context(
            "documenter", "# Issue\n\nBody.", issue_number=1, base_branch="release/v2"
        )
        assert "origin/release/v2...HEAD" in result
        assert "HEAD~10" not in result

    def test_no_base_branch_falls_back_to_origin_main(self):
        """Without base_branch, falls back to 'origin/main...HEAD'."""
        result = _build_role_context("tester", "# Issue\n\nBody.", issue_number=1)
        assert "origin/main...HEAD" in result
        assert "HEAD~10" not in result

    def test_none_base_branch_falls_back(self):
        """Explicit None base_branch falls back to origin/main."""
        result = _build_role_context("tester", "# Issue\n\nBody.", issue_number=1, base_branch=None)
        assert "origin/main...HEAD" in result
        assert "HEAD~10" not in result

    def test_analysis_roles_unaffected(self):
        """Analysis roles (architect, task_planner) don't have context pointers at all."""
        result = _build_role_context("architect", "Full body", issue_number=1, base_branch="main")
        # Analysis roles return just the task description, no context pointers
        assert "origin/main...HEAD" not in result
        assert "HEAD~10" not in result

    def test_no_head_tilde_10_remains_in_any_role(self):
        """HEAD~10 should not appear for any execution role."""
        for role in ("tester", "documenter", "some_new_role"):
            for base_branch in (None, "main", "develop"):
                result = _build_role_context(
                    role, "# Issue\n\nBody.", issue_number=1, base_branch=base_branch
                )
                assert "HEAD~10" not in result, (
                    f"HEAD~10 found for role={role}, base_branch={base_branch}"
                )


class TestBrcPreambleSyncStep:
    """Tests for SYNC step in BRC reviewer lifecycle (issue #1565)."""

    def test_reviewer_lifecycle_includes_sync_step(self):
        """Reviewer lifecycle includes SYNC step with fetch+merge."""
        preamble = _build_brc_preamble("reviewer_code", "implement", branch="egg/issue-123")
        assert "**SYNC**" in preamble
        assert "git fetch origin" in preamble
        assert "git merge origin/egg/issue-123" in preamble

    def test_reviewer_sync_step_before_review(self):
        """SYNC step comes before REVIEW step in reviewer lifecycle."""
        preamble = _build_brc_preamble("reviewer_code", "implement", branch="egg/issue-123")
        sync_pos = preamble.index("**SYNC**")
        review_pos = preamble.index("**REVIEW**")
        assert sync_pos < review_pos, "SYNC must come before REVIEW"

    def test_reviewer_sync_step_after_poll(self):
        """SYNC step comes after POLL step in reviewer lifecycle."""
        preamble = _build_brc_preamble("reviewer_code", "implement", branch="egg/issue-123")
        poll_pos = preamble.index("**POLL**")
        sync_pos = preamble.index("**SYNC**")
        assert poll_pos < sync_pos, "POLL must come before SYNC"

    def test_reviewer_lifecycle_renumbered(self):
        """Reviewer lifecycle steps are renumbered after SYNC insertion."""
        preamble = _build_brc_preamble("reviewer_code", "implement", branch="egg/issue-123")
        # After SYNC insertion, REVIEW should be step 4, ACK/NACK step 5
        assert "4. **REVIEW**" in preamble
        assert "5. **ACK/NACK**" in preamble
        assert "6. **CONFIRM**" in preamble
        assert "7. **STAY ALIVE**" in preamble
        assert "8. **HANDLE RE-REVIEW**" in preamble

    def test_sync_without_branch_uses_base_branch_fallback(self):
        """Without branch parameter, SYNC falls back to base_branch."""
        preamble = _build_brc_preamble("reviewer_code", "implement", base_branch="develop")
        assert "**SYNC**" in preamble
        assert "origin/develop" in preamble

    def test_sync_without_branch_or_base_branch_uses_main(self):
        """Without branch or base_branch, SYNC falls back to main."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "**SYNC**" in preamble
        assert "origin/main" in preamble

    def test_dual_role_producer_gets_sync_note(self):
        """Dual-role agents (tester) get sync note in producer orientation."""
        preamble = _build_brc_preamble("tester", "implement", branch="egg/feature-branch")
        assert "git fetch origin" in preamble
        assert "origin/egg/feature-branch" in preamble

    def test_producer_only_no_sync_step(self):
        """Pure producer (coder) does not get SYNC in reviewer lifecycle."""
        preamble = _build_brc_preamble("coder", "implement", branch="egg/issue-123")
        # Coder is producer only, so should not have reviewer lifecycle at all
        assert "### Reviewer Lifecycle" not in preamble

    def test_contract_reviewer_gets_sync(self):
        """Contract reviewer also gets SYNC step."""
        preamble = _build_brc_preamble("reviewer_contract", "implement", branch="egg/issue-456")
        assert "**SYNC**" in preamble
        assert "origin/egg/issue-456" in preamble


class TestReviewerPollUsesWaitLoop:
    """Reviewer POLL step must use ``wait-loop`` (issue #1943).

    Background: a bare ``egg-orch message wait --timeout 60`` exits
    rc=1 on every timeout, which the agent-facing Bash tool renders as
    ``is_error=True``.  On a legitimately-long proposal wait the agent
    reads that as a failure and tight-retries the exact command.
    ``wait-loop`` blocks server-side forever and re-issues the inner
    long-poll itself, so timeouts never surface to the caller.
    """

    def test_reviewer_poll_uses_wait_loop_not_bare_wait(self):
        preamble = _build_brc_preamble("reviewer_code", "implement", branch="egg/issue-123")
        # Locate the POLL block by slicing from **POLL** to the next step header.
        poll_start = preamble.index("**POLL**")
        poll_end = preamble.index("**SYNC**")
        poll_block = preamble[poll_start:poll_end]
        assert "egg-orch message wait-loop --for CONSENSUS_PROPOSE" in poll_block, (
            "POLL step must tell reviewers to use wait-loop (blocks "
            "forever server-side), not bare `message wait`."
        )
        assert "egg-orch message wait --for CONSENSUS_PROPOSE --timeout" not in poll_block, (
            "POLL step must not reintroduce bare `message wait --timeout` "
            "— issue #1943 documents why it causes tight retry loops."
        )


_PRODUCER_ROLES_BY_PHASE = [
    ("refiner", "refine"),
    ("architect", "plan"),
    ("task_planner", "plan"),
    ("risk_analyst", "plan"),
    ("coder", "implement"),
    ("tester", "implement"),
    ("documenter", "implement"),
]


class TestProducerRespondToReviewsWaitLoop:
    """Producer RESPOND TO REVIEWS step must spell out the pre-confirm
    ``--for`` allowlist and explicitly exclude ``CONSENSUS_CONFIRMED``
    (issue #2482).

    Background: step 4 originally said only "Poll for ACK/NACK from
    reviewers via ``egg-orch message wait-loop``" without the explicit
    flag list. Producer agents improvised and copied the step 6
    STAY ALIVE allowlist, which includes ``CONSENSUS_CONFIRMED``. The
    orchestrator's pre-confirm guard rejects that with HTTP 400 (#2064)
    because the producer's own confirm is part of what generates the
    global ``CONSENSUS_CONFIRMED`` signal, so the wait would deadlock
    on itself. The reject-and-retry burned a tool turn at every
    propose→confirm boundary.

    The bug originally surfaced on the refiner role; parametrizing
    across every producer role pins the property in case
    ``_build_brc_preamble`` ever becomes role-specific.
    """

    @pytest.mark.parametrize(("role", "phase"), _PRODUCER_ROLES_BY_PHASE)
    def test_step4_lists_pre_confirm_allowlist(self, role, phase):
        preamble = _build_brc_preamble(role, phase, branch="egg/issue-123")
        respond_start = preamble.index("**RESPOND TO REVIEWS**")
        respond_end = preamble.index("**CONFIRM**", respond_start)
        respond_block = preamble[respond_start:respond_end]
        for required in (
            "--for CONSENSUS_ACK",
            "--for CONSENSUS_NACK",
            "--for CONSENSUS_RE_REVIEW",
            "--for STATUS",
            "--for OVERSEER_ALERT",
        ):
            assert required in respond_block, (
                f"RESPOND TO REVIEWS step must include `{required}` in the "
                f"pre-confirm wait-loop incantation for role={role} "
                f"phase={phase} (issues #2482, #2531)."
            )

    @pytest.mark.parametrize(("role", "phase"), _PRODUCER_ROLES_BY_PHASE)
    def test_step4_explains_status_ready_to_confirm_nudge(self, role, phase):
        """`--for STATUS` must come with guidance on the directed
        ``Ready to confirm`` nudge (issue #2531).

        Background: when every reviewer has already ACKed the current
        version, no further ``CONSENSUS_ACK`` / ``CONSENSUS_NACK`` arrive
        and the producer would deadlock until its wait timed out. The
        orchestrator emits a directed ``STATUS`` (subject ``Ready to
        confirm — all confirm preconditions satisfied``,
        ``metadata.ready_to_confirm == True``) once the global
        preconditions clear. The prompt has to tell the producer to act
        on that wake (go to step 5 CONFIRM), or the agent will read the
        message, treat it as informational, and re-enter the wait.
        """
        preamble = _build_brc_preamble(role, phase, branch="egg/issue-123")
        respond_start = preamble.index("**RESPOND TO REVIEWS**")
        respond_end = preamble.index("**CONFIRM**", respond_start)
        respond_block = preamble[respond_start:respond_end]
        assert "Ready to confirm" in respond_block, (
            f"RESPOND TO REVIEWS step must mention the orchestrator's "
            f"`Ready to confirm` STATUS nudge so role={role} phase={phase} "
            "knows what to do on a STATUS wake (issue #2531)."
        )
        assert "#2531" in respond_block, (
            "RESPOND TO REVIEWS step must cite #2531 next to the STATUS "
            "guidance so future readers have the context for why STATUS "
            "is in the allowlist."
        )

    @pytest.mark.parametrize(("role", "phase"), _PRODUCER_ROLES_BY_PHASE)
    def test_step4_excludes_consensus_confirmed(self, role, phase):
        preamble = _build_brc_preamble(role, phase, branch="egg/issue-123")
        respond_start = preamble.index("**RESPOND TO REVIEWS**")
        respond_end = preamble.index("**CONFIRM**", respond_start)
        respond_block = preamble[respond_start:respond_end]
        assert "--for CONSENSUS_CONFIRMED" not in respond_block, (
            "RESPOND TO REVIEWS step must NOT include "
            "`--for CONSENSUS_CONFIRMED` — the orchestrator's "
            "pre-confirm guard rejects that pattern with HTTP 400 "
            f"(#2064, #2482) for role={role} phase={phase} because the "
            "producer's own confirm is part of what generates that signal."
        )


class TestReviewerWaitLoopMentionsAutoCursor:
    """Reviewer + producer wait-loop steps must explain auto cursor
    threading (issue #2323).

    Background: each ``wait-loop`` CLI invocation is a separate process,
    and without cursor threading each new call starts at the stream
    tip — skipping any event that arrived in the gap between the
    previous wait-loop returning and the next one entering. On
    multi-producer phases (plan: 3 producers) this stalled the phase
    by 20-30 minutes per missed event. The fix is in the CLI itself:
    ``wait`` and ``wait-loop`` auto-derive a per-(role, for_types)
    cursor file under
    ``/tmp/egg-wait-cursor-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}-*``
    with no flag needed. The prompts point at that path so operators
    debugging a stuck reviewer know where to look.
    """

    def test_reviewer_poll_mentions_auto_cursor(self):
        preamble = _build_brc_preamble("reviewer_code", "implement", branch="egg/issue-123")
        poll_start = preamble.index("**POLL**")
        poll_end = preamble.index("**SYNC**")
        poll_block = preamble[poll_start:poll_end]
        assert "automatic" in poll_block.lower(), (
            "POLL must tell the reviewer that cursor threading "
            "across re-entries is automatic (issue #2323)."
        )
        assert "/tmp/egg-wait-cursor-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}-" in poll_block, (
            "POLL must surface the cursor file path so operators "
            "debugging a stuck reviewer can `cat` it."
        )
        assert "#2323" in poll_block

    def test_reviewer_stay_alive_mentions_auto_cursor(self):
        preamble = _build_brc_preamble("reviewer_code", "implement", branch="egg/issue-123")
        sa_start = preamble.index("**STAY ALIVE**")
        sa_end = preamble.index("**HANDLE RE-REVIEW**", sa_start)
        sa_block = preamble[sa_start:sa_end]
        assert "automatic" in sa_block.lower()
        assert "/tmp/egg-wait-cursor-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}-" in sa_block
        assert "#2323" in sa_block

    def test_producer_stay_alive_mentions_auto_cursor(self):
        preamble = _build_brc_preamble("coder", "implement", branch="egg/issue-123")
        sa_start = preamble.index("**STAY ALIVE**")
        sa_end = preamble.index("**HANDLE RE-REVIEW**", sa_start)
        sa_block = preamble[sa_start:sa_end]
        assert "automatic" in sa_block.lower()
        assert "/tmp/egg-wait-cursor-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}-" in sa_block
        assert "#2323" in sa_block


class TestProducerOrientationSyncNote:
    """Tests for sync note in _build_producer_orientation (issue #1565)."""

    def test_tester_gets_sync_note_with_branch(self):
        """Tester orientation includes sync note when branch is provided."""
        orient = _build_producer_orientation("tester", "implement", [], branch="egg/issue-123")
        assert "git fetch origin" in orient
        assert "origin/egg/issue-123" in orient

    def test_tester_no_sync_note_without_branch(self):
        """Tester orientation omits sync note when branch is None."""
        orient = _build_producer_orientation("tester", "implement", [])
        assert "git fetch origin" not in orient

    def test_documenter_gets_sync_note_with_branch(self):
        """Documenter orientation includes sync note when branch is provided."""
        orient = _build_producer_orientation("documenter", "implement", [], branch="egg/issue-123")
        assert "git fetch origin" in orient
        assert "origin/egg/issue-123" in orient

    def test_documenter_no_sync_note_without_branch(self):
        """Documenter orientation omits sync note when branch is None."""
        orient = _build_producer_orientation("documenter", "implement", [])
        assert "git fetch origin" not in orient

    def test_coder_no_sync_note(self):
        """Coder orientation does not get sync note regardless of branch."""
        orient = _build_producer_orientation(
            "coder", "implement", ["reviewer_code"], branch="egg/issue-123"
        )
        assert "git fetch origin && git merge" not in orient


class TestAgentPromptBaseBranchPassthrough:
    """Tests for base_branch passthrough in _build_agent_prompt (issue #1565)."""

    def test_tester_prompt_uses_base_branch_in_context(self):
        """Tester agent prompt passes base_branch to role context."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
            base_branch="main",
        )
        assert "origin/main...HEAD" in result
        assert "HEAD~10" not in result

    def test_tester_prompt_uses_branch_in_brc_preamble(self):
        """Tester agent prompt passes branch to BRC preamble."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
            branch="egg/issue-42",
            concurrent=True,
        )
        assert "origin/egg/issue-42" in result

    def test_reviewer_prompt_passes_base_branch(self):
        """Reviewer agent prompt passes base_branch to review prompt builder."""
        result = _build_agent_prompt(
            role_value="reviewer_code",
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            issue_number=42,
            base_branch="develop",
        )
        assert "origin/develop...HEAD" in result
        assert "HEAD~10" not in result

    def test_reviewer_concurrent_gets_sync_step(self):
        """Reviewer in concurrent mode gets SYNC step with branch."""
        result = _build_agent_prompt(
            role_value="reviewer_code",
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            issue_number=42,
            branch="egg/issue-42",
            concurrent=True,
        )
        assert "**SYNC**" in result
        assert "origin/egg/issue-42" in result

    def test_coder_prompt_unaffected_by_base_branch(self):
        """Coder prompt (which delegates to _build_phase_prompt) accepts base_branch."""
        # Should not raise even though coder uses _build_phase_prompt internally
        result = _build_agent_prompt(
            role_value="coder",
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
            base_branch="main",
        )
        # Coder prompt uses _build_phase_prompt, not _build_role_context
        assert result  # Just verify it doesn't crash


class TestDirectedCoordinationGuidance:
    """Tests for directed coordination guidance in BRC preamble (issue #1718).

    Validates that ``_build_brc_preamble`` includes the new 'Directed Coordination'
    section with ``egg-orch message send`` CLI form and role-appropriate guidance
    for HANDOFF, STATUS, and QUESTION message types.
    """

    # --- Section presence ---

    def test_preamble_includes_directed_coordination_section(self):
        """BRC preamble includes the 'Directed Coordination' subsection header."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "### Directed Coordination" in preamble

    def test_preamble_includes_message_send_cli(self):
        """BRC preamble includes the ``egg-orch message send`` CLI example."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "egg-orch message send" in preamble

    def test_preamble_includes_cli_full_form(self):
        """BRC preamble shows the full CLI form with --to, --type, --subject, --body."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "--to <role>" in preamble
        assert "--type <TYPE>" in preamble

    # --- Producer-specific guidance ---

    def test_producer_gets_handoff_guidance(self):
        """Producer preamble includes HANDOFF guidance."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "HANDOFF" in preamble

    def test_producer_gets_status_guidance(self):
        """Producer preamble includes STATUS guidance for directed messages."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "STATUS" in preamble

    def test_producer_handoff_has_example(self):
        """Producer HANDOFF guidance includes a concrete example."""
        preamble = _build_brc_preamble("coder", "implement")
        # Should mention coder → tester as the canonical handoff example
        assert "coder" in preamble and "tester" in preamble

    def test_producer_guidance_labeled(self):
        """Producer guidance is labeled 'As a producer'."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "As a producer" in preamble

    # --- Reviewer-specific guidance ---

    def test_reviewer_gets_question_guidance(self):
        """Reviewer preamble includes QUESTION guidance."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "QUESTION" in preamble

    def test_reviewer_guidance_labeled(self):
        """Reviewer guidance is labeled 'As a reviewer'."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "As a reviewer" in preamble

    def test_reviewer_question_avoids_unnecessary_nacks(self):
        """Reviewer guidance recommends QUESTION to avoid unnecessary NACKs."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "unnecessary NACKs" in preamble or "NACK" in preamble

    def test_reviewer_does_not_get_producer_guidance(self):
        """Pure reviewer does not get producer-specific guidance (HANDOFF/STATUS)."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "As a producer" not in preamble

    def test_producer_does_not_get_reviewer_question_guidance(self):
        """Pure producer does not get reviewer-specific QUESTION guidance."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "As a reviewer" not in preamble

    # --- Dual-role agents (tester) ---

    def test_dual_role_gets_both_producer_and_reviewer_guidance(self):
        """Dual-role agent (tester) gets both producer and reviewer guidance."""
        preamble = _build_brc_preamble("tester", "implement")
        assert "As a producer" in preamble
        assert "As a reviewer" in preamble

    def test_dual_role_gets_handoff_and_question(self):
        """Dual-role agent gets both HANDOFF and QUESTION guidance."""
        preamble = _build_brc_preamble("tester", "implement")
        assert "HANDOFF" in preamble
        assert "QUESTION" in preamble

    # --- Guard-rail guidance ---

    def test_directed_messages_supplementary_to_consensus(self):
        """Preamble clarifies directed messages are supplementary to BRC consensus."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "supplementary" in preamble or "do NOT replace" in preamble.lower()

    # --- Section ordering ---

    def test_directed_coordination_after_reviewer_lifecycle(self):
        """Directed Coordination appears after Reviewer Lifecycle for dual-role agents."""
        preamble = _build_brc_preamble("tester", "implement")
        reviewer_pos = preamble.index("### Reviewer Lifecycle")
        coord_pos = preamble.index("### Directed Coordination")
        assert reviewer_pos < coord_pos, (
            "Directed Coordination should come after Reviewer Lifecycle"
        )

    def test_directed_coordination_after_producer_lifecycle(self):
        """Directed Coordination appears after Producer Lifecycle."""
        preamble = _build_brc_preamble("coder", "implement")
        producer_pos = preamble.index("### Producer Lifecycle")
        coord_pos = preamble.index("### Directed Coordination")
        assert producer_pos < coord_pos, (
            "Directed Coordination should come after Producer Lifecycle"
        )

    def test_directed_coordination_before_exit_warning(self):
        """Directed Coordination appears before the 'exit = FAILED' warning."""
        preamble = _build_brc_preamble("coder", "implement")
        coord_pos = preamble.index("### Directed Coordination")
        exit_pos = preamble.index("you have FAILED your role")
        assert coord_pos < exit_pos, "Directed Coordination should come before the exit warning"

    # --- Phase variations ---

    def test_directed_coordination_in_plan_phase(self):
        """Directed Coordination section is present in plan phase preamble."""
        preamble = _build_brc_preamble("architect", "plan")
        assert "### Directed Coordination" in preamble
        assert "egg-orch message send" in preamble

    def test_directed_coordination_in_refine_phase(self):
        """Directed Coordination section is present in refine phase preamble."""
        preamble = _build_brc_preamble("refiner", "refine")
        assert "### Directed Coordination" in preamble
        assert "HANDOFF" in preamble

    # --- Contract reviewer (pure reviewer) ---

    def test_contract_reviewer_gets_question_not_handoff_producer_block(self):
        """Contract reviewer gets QUESTION guidance but not producer HANDOFF block."""
        preamble = _build_brc_preamble("reviewer_contract", "implement")
        assert "QUESTION" in preamble
        assert "As a reviewer" in preamble
        assert "As a producer" not in preamble

    # --- Full agent prompt integration ---

    def test_directed_coordination_in_concurrent_agent_prompt(self):
        """Directed Coordination appears in the full agent prompt when concurrent=True."""
        result = _build_agent_prompt(
            role_value="coder",
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
            concurrent=True,
        )
        assert "### Directed Coordination" in result
        assert "egg-orch message send" in result

    def test_no_directed_coordination_in_sequential_agent_prompt(self):
        """Directed Coordination does NOT appear in sequential (non-concurrent) agent prompt."""
        result = _build_agent_prompt(
            role_value="coder",
            phase="implement",
            pipeline_id="test-pipe",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=42,
            concurrent=False,
        )
        assert "### Directed Coordination" not in result

    # --- CLI examples in guidance ---

    def test_producer_handoff_has_cli_example(self):
        """Producer HANDOFF guidance includes a concrete egg-orch CLI example."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "egg-orch message send --to tester --type HANDOFF" in preamble

    def test_producer_status_has_cli_example(self):
        """Producer STATUS guidance includes a concrete egg-orch CLI example."""
        preamble = _build_brc_preamble("coder", "implement")
        assert "egg-orch message send --to all --type STATUS" in preamble

    def test_reviewer_question_removed_in_favor_of_nack_reason(self):
        """Reviewer guidance directs questions into NACK --reason (issue #1897)."""
        preamble = _build_brc_preamble("reviewer_code", "implement")
        assert "NACK" in preamble and "--reason" in preamble
        assert "legacy QUESTION" in preamble or "QUESTION message type was removed" in preamble

    # --- Regression guard ---

    def test_revert_of_coordination_detected(self):
        """If directed coordination is removed, this test fails — regression guard."""
        preamble = _build_brc_preamble("coder", "implement")
        # Must contain the CLI example AND at least one message type
        assert "egg-orch message send" in preamble
        assert "HANDOFF" in preamble
        # Must clarify relationship to consensus
        assert "supplementary" in preamble or "consensus" in preamble.lower()


# ---------------------------------------------------------------------------
# #2527 — plan reviewer's task role↔files alignment check
# ---------------------------------------------------------------------------
#
# Original PR-1 design built a "Structural Role-Alignment Check" section
# into the reviewer prompt at _build_review_prompt time. PR-1 review
# flagged that as a cross-module silent no-op in concurrent BRC mode:
# in production, all agent prompts are built up-front by
# _run_concurrent_phase BEFORE the planner has produced the plan
# (concurrent_executor.spawn_all). The plan draft does not exist on
# the orchestrator's worktree at that moment, so the section was always
# empty and the reviewer was told "absence = no violations" — the
# opposite of the truth.
#
# Resolution (this PR-2): orchestrator-side validation runs at
# CONSENSUS_PROPOSE in routes/signals.py:_validate_planner_role_alignment,
# rejecting the planner's proposal with HTTP 400 before the tracker
# state is mutated. The reviewer prompt no longer carries a per-prompt
# section; the validator-runs-here tests live in this same file under
# class TestPlannerRoleAlignmentValidation (above).


class TestPlanReviewCriteriaReflectsOrchestratorSideValidation:
    """The plan-review criteria string documents that role↔files
    alignment is enforced orchestrator-side at CONSENSUS_PROPOSE so a
    reviewer reading the criteria does not expect a per-prompt section
    (which the broken PR-1 wiring promised but never delivered in
    concurrent mode)."""

    def test_criteria_references_orchestrator_side_enforcement(self):
        criteria = _get_plan_review_criteria()
        # Must still call out the dimension by name.
        assert "Role" in criteria and "Alignment" in criteria
        assert "#2527" in criteria
        # Must explicitly tell the reviewer the check runs at
        # CONSENSUS_PROPOSE rather than at prompt-build time.
        assert "CONSENSUS_PROPOSE" in criteria
        assert "orchestrator-side" in criteria
        # Must describe rejection of the proposal (not "absence = no
        # violations" — that was the PR-1 false-clean wording).
        assert "rejected" in criteria
        # The push-time backstop (`403 restricted_path_modified`) is
        # the link to the gateway's existing enforcement.
        assert "403" in criteria

    def test_criteria_does_not_reuse_pr1_false_clean_wording(self):
        # Specific regression guard: PR-1 included the line
        # "The absence of that section means the automated check found
        #  no violations". That sentence was structurally a false-clean
        # in concurrent BRC mode (the section was always absent because
        # the plan didn't exist yet). Make sure it doesn't reappear.
        criteria = _get_plan_review_criteria()
        assert "absence of that section" not in criteria.lower()


class TestPlanReviewCriteriaAuditSections:
    """Issue #2594 — plan-phase reviewers must perform a
    Primitive-Existence Audit (§9) and a Trust-Boundary Audit (§10) so
    plans whose tasks depend on nonexistent or wrong-tier primitives
    are NACKed cheaply at plan-phase instead of expensively at
    implement-phase."""

    def test_criteria_has_primitive_existence_audit_section(self):
        criteria = _get_plan_review_criteria()
        assert "Primitive-Existence Audit" in criteria
        # Hard-NACK framing per the issue.
        assert "hard NACK" in criteria
        # Issue reference threads back to #2594.
        assert "#2594" in criteria
        # Prescribes grep evidence and a verbatim-command rule.
        assert "grep -rn" in criteria
        # Anchors with the #2474 evidence so reviewers see the cost
        # of skipping the audit.
        assert "ScriptedProvider" in criteria

    def test_criteria_has_trust_boundary_audit_section(self):
        criteria = _get_plan_review_criteria()
        assert "Trust-Boundary Audit" in criteria
        # Names all three execution contexts.
        assert "in-sandbox-agent" in criteria
        assert "trusted-CI-runner" in criteria
        assert "human-operator" in criteria
        # References the authoritative doc.
        assert "integration-test-trust-boundary.md" in criteria

    def test_criteria_audit_sections_appear_after_role_alignment(self):
        # §9 / §10 are appended after the existing §8 role↔files
        # alignment section, not interleaved (preserves the existing
        # numbering the orchestrator-side validation tests rely on).
        # Pin on the explicit §8 heading so a future addition of a
        # criteria string containing the substring "Role" higher up
        # cannot silently make this match the wrong section.
        criteria = _get_plan_review_criteria()
        role_alignment_idx = criteria.index("### 8.")
        primitive_idx = criteria.index("Primitive-Existence Audit")
        trust_idx = criteria.index("Trust-Boundary Audit")
        assert role_alignment_idx < primitive_idx < trust_idx

    def test_primitive_existence_section_has_new_primitive_exception(self):
        # The task_planner producer prompt tells the planner to mark
        # plan-created primitives ``(NEW — task TASK-X-Y)``; §9 must
        # recognize that annotation so the asymmetry does not cause
        # false NACK loops on every plan that introduces a primitive.
        criteria = _get_plan_review_criteria()
        # Locate the §9 block specifically — assertions must hold
        # within the section, not just somewhere in the criteria.
        section_start = criteria.index("Primitive-Existence Audit")
        section_end = criteria.index("Trust-Boundary Audit")
        section = criteria[section_start:section_end]
        assert "(NEW" in section
        # The exception must be explicit that NEW-annotated primitives
        # are not NACKed on missing-grep evidence.
        assert "do not NACK" in section or "don't NACK" in section
        # And it must direct the reviewer to verify the creating task's
        # *acceptance criteria* — generic substrings like ``creates``
        # are too permissive (could appear in an unrelated future
        # example). Pin on the actual phrase the criteria uses.
        assert "acceptance" in section.lower()

    def test_trust_boundary_section_describes_gateway_url_correctly(self):
        # The §10 description used to claim parent ``conftest.py``
        # exposes ``gateway_url`` as a fixture — it does not. The
        # parent ``EggStack`` dataclass has it as an attribute; the
        # standalone ``gateway_url`` pytest fixture lives only in
        # ``local_pipeline/conftest.py``. Encode the correction so a
        # future edit cannot silently reintroduce the bug.
        criteria = _get_plan_review_criteria()
        section_start = criteria.index("Trust-Boundary Audit")
        section = criteria[section_start:]
        # Must not assert the parent conftest "exposes" gateway_url.
        assert "exposes only `gateway_url`" not in section
        assert "exposes `gateway_url` only" not in section
        # Must mention the env-vs-fixture distinction so reviewers
        # don't conflate the agent's GATEWAY_URL runtime with pytest
        # fixtures.
        assert "GATEWAY_URL" in section


class TestPlanProducerPromptsCitePrimitives:
    """Issue #2594 — producer prompts (architect / task_planner /
    risk_analyst) must direct the producer to cite runtime primitives
    in a form the plan-phase audit can verify."""

    def test_architect_prompt_directs_primitive_citation(self):
        prompt = _build_agent_prompt(
            role_value="architect",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "#2594" in prompt
        # Calls out the categories of primitive worth citing.
        assert "ConfigMap" in prompt
        # Calls out BOTH scope axes — purpose (unit-test-only vs
        # deployed-pod) AND execution context (in-sandbox-agent vs
        # trusted-CI-runner). Earlier wording conflated the two into
        # a single axis, which obscured the actual decision the
        # architect has to make.
        assert "unit-test-only" in prompt
        assert "deployed-pod" in prompt
        assert "in-sandbox-agent" in prompt
        assert "trusted-CI-runner" in prompt
        # Names the orthogonality so the architect knows the axes
        # are independent, not collapsed.
        assert "orthogonal" in prompt or "independent" in prompt

    def test_task_planner_prompt_has_primitives_audit_block(self):
        prompt = _build_agent_prompt(
            role_value="task_planner",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "Primitives audit (#2594)" in prompt
        # References the trust-boundary doc.
        assert "integration-test-trust-boundary.md" in prompt
        # Names the local_pipeline/ vs parent-conftest distinction so
        # planners know where trusted-tier tests must live.
        assert "local_pipeline" in prompt
        # NEW-primitive escape hatch (so the planner can name
        # primitives the plan itself will create).
        assert "(NEW" in prompt
        # The producer must mirror §9's reviewer-side requirements for
        # NEW-annotated primitives — acceptance-criteria coverage and
        # dependency ordering. Without this the producer/reviewer pair
        # is asymmetric and the planner cannot pre-empt the NACK.
        # Scope these assertions to bullet 1 of the audit block (the
        # (NEW — …) symmetry text) — both "acceptance criteria" and
        # "order"/"ordering" appear independently elsewhere in the
        # task_planner prompt, so a flat ``in prompt`` check would
        # pass from unrelated sources even if the symmetry text were
        # deleted.
        new_block_start = prompt.index("(NEW — task TASK-X-Y)")
        new_block_end = prompt.index(
            "2. **Cite trust-boundary scope.**",
            new_block_start,
        )
        new_block = prompt[new_block_start:new_block_end]
        assert "acceptance criteria" in new_block
        assert "order" in new_block.lower()

    def test_task_planner_prompt_describes_gateway_url_correctly(self):
        # The producer prompt previously asserted that the parent
        # `integration_tests/conftest.py` "exposes `gateway_url` only" —
        # a falsehood that produces planner/reviewer asymmetry and
        # false NACK loops. Pin the correction.
        prompt = _build_agent_prompt(
            role_value="task_planner",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        # Banned substrings: any wording that implies the parent
        # conftest exposes a `gateway_url` pytest fixture.
        assert "exposes `gateway_url` only" not in prompt
        assert "exposes only `gateway_url`" not in prompt
        # Required signals: the producer must distinguish the
        # agent-runtime `GATEWAY_URL` env surface from pytest fixtures,
        # and must name `local_pipeline/` as the only place the
        # `gateway_url` pytest fixture is defined.
        assert "GATEWAY_URL" in prompt
        assert "local_pipeline/conftest.py" in prompt
        # And must surface the kubectl-gated, no-fixture-in-sandbox
        # reality so the planner does not place a fixture-using test
        # in a sandbox-tier directory.
        assert "in-sandbox-agent" in prompt

    def test_no_plan_producer_prompt_mis_describes_gateway_url(self):
        # Scan all three plan-producer prompts for the banned
        # ``exposes ... gateway_url ... only`` falsehood. The previous
        # iteration of this PR fixed `_get_plan_review_criteria` §10
        # but left the same false claim in the task_planner producer
        # prompt — a parallel-location regression. Lock the invariant
        # across every producer prompt so this exact failure mode
        # cannot recur on either side of the producer/reviewer split.
        banned_substrings = (
            "exposes `gateway_url` only",
            "exposes only `gateway_url`",
        )
        for role in ("architect", "task_planner", "risk_analyst"):
            prompt = _build_agent_prompt(
                role_value=role,
                phase="plan",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature\n\nDetail.",
                issue_number=1,
            )
            for banned in banned_substrings:
                assert banned not in prompt, (
                    f"{role} prompt contains banned substring {banned!r} — "
                    f"reintroduces the false parent-conftest gateway_url "
                    f"claim from issue #2594 review"
                )

    def test_risk_analyst_prompt_flags_runtime_primitive_risks(self):
        prompt = _build_agent_prompt(
            role_value="risk_analyst",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "#2594" in prompt
        # References the canonical #2474 failure mode.
        assert "#2474" in prompt


class TestRefinerOrientationSurfacesPrimitives:
    """Issue #2594 — refiner phase-orientation should ask the refiner to
    surface runtime-primitive assumptions at the phase_gate so the
    plan-phase audit's candidate list is pre-named."""

    def test_refiner_orientation_mentions_primitive_surfacing(self):
        # Refine-phase, refiner-role orientation text comes from
        # _build_producer_orientation, the same helper used for other
        # producer roles.
        orientation = _build_producer_orientation(
            role_value="refiner",
            phase="refine",
            reviewers=[],
        )
        assert "#2594" in orientation
        # The three execution contexts named in the criteria.
        assert "in-sandbox-agent" in orientation
        assert "trusted-CI-runner" in orientation


# ---------------------------------------------------------------------------
# #1557 TASK-1-8 / TASK-1-11 — Jira-epic pipeline prompt branches
# ---------------------------------------------------------------------------


class TestBuildPhasePromptEpicModeRefine:
    """Refine-phase epic-mode preamble (#1557 TASK-1-8).

    When ``jira_epic_key`` is set, the refine prompt must:
    - Reframe the destination as the epic's Description (wholesale rewrite).
    - Tell the agent NOT to decompose into tickets (that's plan-phase work).
    - Include the epic key verbatim.
    - Add reassess vs fresh language depending on ``jira_effective_mode``.

    The non-epic (today's) path stays byte-identical when
    ``jira_epic_key=None``.
    """

    def test_refine_epic_mode_includes_epic_key(self):
        """The epic key appears verbatim in the rendered prompt."""
        result = _build_phase_prompt(
            phase="refine",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Analyze the issue",
            jira_epic_key="ENG-1234",
            jira_effective_mode="fresh",
        )
        assert "ENG-1234" in result

    def test_refine_epic_mode_has_epic_description_framing(self):
        """Epic-mode refine prompt frames output as 'epic Description'.

        Asserts the destination-line language so the refiner knows it's
        not writing a ticket-scoped refinement.
        """
        result = _build_phase_prompt(
            phase="refine",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Analyze the issue",
            jira_epic_key="ENG-1234",
            jira_effective_mode="fresh",
        )
        assert "## Epic-mode (Jira)" in result
        assert "epic" in result.lower()
        assert "Description" in result
        # The destination framing from #1557 decision-9
        assert "wholesale rewrite" in result

    def test_refine_epic_mode_warns_against_decomposition(self):
        """Refiner is told NOT to decompose into individual tickets."""
        result = _build_phase_prompt(
            phase="refine",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Analyze the issue",
            jira_epic_key="ENG-1234",
            jira_effective_mode="fresh",
        )
        # The refiner must defer ticket decomposition to plan phase.
        assert "plan phase" in result.lower()
        assert (
            "decompose the work into tickets yourself" in result or "not try to" in result.lower()
        )

    def test_refine_epic_mode_fresh_branch(self):
        """Fresh-mode renders the 'clean slate' language only."""
        result = _build_phase_prompt(
            phase="refine",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Analyze the issue",
            jira_epic_key="ENG-1234",
            jira_effective_mode="fresh",
        )
        assert "**fresh**" in result
        # Reassess-only sentences MUST NOT appear in fresh mode.
        assert "**reassess**" not in result
        assert "what work is already done" not in result.lower()

    def test_refine_epic_mode_reassess_branch(self):
        """Reassess mode pulls in the existing-children context + 3-axis ask."""
        result = _build_phase_prompt(
            phase="refine",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Analyze the issue",
            issue_number=1557,
            jira_epic_key="ENG-1234",
            jira_effective_mode="reassess",
        )
        assert "**reassess**" in result
        # The three axes the refiner should surface explicitly.
        assert "(a) what" in result
        assert "(b) what" in result
        assert "(c) what" in result
        # References the refine-input artifact path.
        assert "1557-refine-input.json" in result
        # The 'fresh' label must NOT appear when reassess is active.
        assert "**fresh**" not in result

    def test_refine_epic_mode_reassess_uses_pipeline_id_when_no_issue(self):
        """When there is no issue number, the path falls back to pipeline id."""
        result = _build_phase_prompt(
            phase="refine",
            pipeline_id="pid-1557",
            pipeline_mode="local",
            prompt="Analyze the issue",
            jira_epic_key="ENG-1234",
            jira_effective_mode="reassess",
        )
        # No issue number → use pipeline_id in the path
        assert "pid-1557-refine-input.json" in result

    def test_refine_non_epic_omits_epic_section(self):
        """When ``jira_epic_key`` is None, the epic block is absent."""
        result = _build_phase_prompt(
            phase="refine",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Analyze the issue",
            jira_epic_key=None,
            jira_effective_mode=None,
        )
        assert "## Epic-mode (Jira)" not in result
        assert "wholesale rewrite" not in result
        assert "ENG-1234" not in result


class TestBuildPhasePromptEpicModePlan:
    """Plan-phase epic-mode preamble (#1557 TASK-1-11).

    When ``jira_epic_key`` is set, the plan prompt must:
    - Frame output as Jira child tickets under the epic.
    - Enforce per-node ticket-shaped description structure
      (Problem / Scope / AC / OOS / Cross-links).
    - Constrain cross-task link types to the allowlist.
    - Add reassess-specific classification + Won't-Do permanence warning
      when ``jira_effective_mode == "reassess"``.
    """

    def test_plan_epic_mode_includes_epic_key(self):
        """The epic key appears in the rendered plan prompt."""
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            jira_epic_key="ENG-1234",
            jira_effective_mode="fresh",
        )
        assert "ENG-1234" in result
        assert "## Epic-mode plan output (Jira)" in result

    def test_plan_epic_mode_renders_ticket_shaped_description_structure(self):
        """Per-node ticket-body structure is named explicitly.

        The plan prompt must list the five required sections so the
        planner emits them for every node.
        """
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            jira_epic_key="ENG-1234",
            jira_effective_mode="fresh",
        )
        # The five required ticket-body sections (feedback Q2)
        assert "Problem" in result
        assert "Scope" in result
        assert "Acceptance criteria" in result
        assert "Out of scope" in result
        assert "Cross-links" in result
        # Surfaced as a mandatory ticket-body structure
        assert "ticket-body structure" in result

    def test_plan_epic_mode_link_type_allowlist(self):
        """Cross-task link types are constrained to the allowlist."""
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            jira_epic_key="ENG-1234",
            jira_effective_mode="fresh",
        )
        assert "Blocks" in result
        assert "Is blocked by" in result
        assert "Relates to" in result

    def test_plan_epic_mode_emits_yaml_block_names(self):
        """Plan-draft `# yaml-tasks` extras are explicitly named."""
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            jira_epic_key="ENG-1234",
            jira_effective_mode="fresh",
        )
        assert "epic_apply:" in result
        assert "consolidations:" in result
        assert "splits:" in result

    def test_plan_epic_mode_reassess_branch_classification(self):
        """Reassess mode lists the per-child classifications."""
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            issue_number=1557,
            jira_epic_key="ENG-1234",
            jira_effective_mode="reassess",
        )
        assert "**Reassess mode**" in result
        # The classification labels named in the prompt
        for label in ("updated", "consolidated", "split", "net-new", "wont_do"):
            assert label in result
        # References the existing-children sweep
        assert "1557-existing-children.json" in result

    def test_plan_epic_mode_reassess_includes_wont_do_warning(self):
        """Reassess plan-output rendering surfaces the Won't-Do permanence warning."""
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            issue_number=1557,
            jira_epic_key="ENG-1234",
            jira_effective_mode="reassess",
        )
        # The exact wording surfaces the permanence concern (#1557 R6)
        assert "permanent" in result
        assert "not auto-reversed" in result
        assert "wont_do_reason" in result

    def test_plan_epic_mode_reassess_includes_consolidation_survivor_rule(self):
        """Reassess mode names the consolidation survivor field."""
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            issue_number=1557,
            jira_epic_key="ENG-1234",
            jira_effective_mode="reassess",
        )
        # Consolidation survivor must be recorded per-consolidation
        assert "survivor" in result.lower()
        assert "decision-5" in result

    def test_plan_epic_mode_fresh_omits_reassess_language(self):
        """Fresh-mode plan prompt MUST NOT include reassess-specific language."""
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            jira_epic_key="ENG-1234",
            jira_effective_mode="fresh",
        )
        # Reassess-mode-only labels MUST NOT appear when fresh
        assert "**Reassess mode**" not in result
        assert "existing-children.json" not in result
        # The Won't-Do permanence warning is reassess-only too
        assert "not auto-reversed" not in result

    def test_plan_non_epic_omits_epic_section(self):
        """Non-epic plan prompt is byte-clean of the epic-mode block."""
        result = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            jira_epic_key=None,
            jira_effective_mode=None,
        )
        assert "## Epic-mode plan output (Jira)" not in result
        assert "epic_apply:" not in result
        assert "ENG-1234" not in result
        # The slice-DAG language stays present (not gated on epic mode)
        assert "slice-DAG" in result.lower() or "Slice-DAG" in result


class TestBuildPhasePromptEpicModeByteIdenticalNonEpic:
    """Guards the 'byte-identical to today's prompt when None' contract.

    Asserts that the rendered prompt does not include any epic-mode
    markers when the kwargs are absent. Counterpart to the explicit
    'omits epic section' tests, but framed as a regression guard.
    """

    def test_refine_byte_identical_when_kwargs_absent(self):
        """The refine prompt is identical with kwargs absent vs ``None``."""
        without = _build_phase_prompt(
            phase="refine",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Analyze the issue",
        )
        with_none = _build_phase_prompt(
            phase="refine",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Analyze the issue",
            jira_epic_key=None,
            jira_effective_mode=None,
        )
        assert without == with_none

    def test_plan_byte_identical_when_kwargs_absent(self):
        """The plan prompt is identical with kwargs absent vs ``None``."""
        without = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
        )
        with_none = _build_phase_prompt(
            phase="plan",
            pipeline_id="pid-1557",
            pipeline_mode="issue",
            prompt="Plan the work",
            jira_epic_key=None,
            jira_effective_mode=None,
        )
        assert without == with_none
