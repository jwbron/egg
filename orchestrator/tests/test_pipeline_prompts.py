"""
Tests for pipeline prompt builder functions.
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import (
    _build_agent_prompt,
    _build_autofix_prompt,
    _build_check_and_fix_prompt,
    _build_checker_prompt,
    _build_phase_prompt,
    _build_phase_scoped_prompt,
    _build_role_context,
    _extract_plan_overview,
    _get_agent_design_criteria,
    _get_code_review_criteria,
    _get_contract_review_criteria,
    _read_shared_criteria,
    _render_contract_tasks,
    _summarize_issue,
)


class TestBuildCheckerPrompt:
    """Tests for _build_checker_prompt with repo_checks parameter."""

    def test_discovery_mode_without_repo_checks(self):
        """Without repo_checks, prompt uses discovery instructions."""
        result = _build_checker_prompt("pid-1", "local")
        assert "Discover and run all project test and lint commands" in result
        assert "Makefile" in result

    def test_discovery_mode_with_repo(self):
        """Discovery mode includes repo working directory."""
        result = _build_checker_prompt("pid-1", "local", repo="acme/web-app")
        assert "Repository: acme/web-app" in result
        assert "~/repos/web-app" in result
        assert "Discover and run all" in result

    def test_explicit_checks_mode(self):
        """With repo_checks, prompt lists explicit commands instead of discovery."""
        checks = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
        ]
        result = _build_checker_prompt("pid-1", "local", repo="acme/web-app", repo_checks=checks)
        assert "make lint" in result
        assert "make test" in result
        assert "**lint**" in result
        assert "**test**" in result
        # Should NOT contain discovery instructions
        assert "Discover and run all" not in result
        assert "Makefile" not in result

    def test_explicit_checks_without_repo(self):
        """Explicit checks work even without a repo specified."""
        checks = [{"name": "build", "command": "npm run build"}]
        result = _build_checker_prompt("pid-1", "issue", repo_checks=checks)
        assert "npm run build" in result
        assert "Repository:" not in result

    def test_always_includes_results_format(self):
        """Both modes include the results JSON format."""
        checks = [{"name": "test", "command": "pytest"}]
        for prompt in [
            _build_checker_prompt("pid-1", "local"),
            _build_checker_prompt("pid-1", "local", repo_checks=checks),
        ]:
            assert "implement-results.json" in prompt
            assert "all_passed" in prompt

    def test_includes_pipeline_metadata(self):
        """Prompt always includes pipeline ID and mode."""
        result = _build_checker_prompt("pid-42", "issue")
        assert "pid-42" in result
        assert "issue" in result


class TestBuildAutofixPrompt:
    """Tests for _build_autofix_prompt with repo parameter."""

    def test_without_repo(self):
        """Basic autofix prompt without repo context."""
        results = {"checks": [{"name": "lint", "passed": False, "output": "3 errors"}]}
        result = _build_autofix_prompt("pid-1", "local", results)
        assert "lint" in result
        assert "3 errors" in result
        assert "Repository:" not in result

    def test_with_repo(self):
        """Autofix prompt includes repo working directory."""
        results = {"checks": [{"name": "test", "passed": False, "output": "1 failure"}]}
        result = _build_autofix_prompt("pid-1", "local", results, repo="acme/web-app")
        assert "Repository: acme/web-app" in result
        assert "~/repos/web-app" in result

    def test_no_failures(self):
        """Prompt handles case with no failing checks."""
        results = {"checks": [{"name": "lint", "passed": True, "output": "ok"}]}
        result = _build_autofix_prompt("pid-1", "local", results)
        assert "No specific failures recorded" in result

    def test_includes_pipeline_metadata(self):
        """Prompt includes pipeline ID and mode."""
        results = {"checks": []}
        result = _build_autofix_prompt("pid-99", "issue", results)
        assert "pid-99" in result
        assert "issue" in result

    def test_autofix_includes_shared_rules(self):
        """Autofix prompt loads rules from shared/prompts/autofixer-rules.md."""
        results = {"checks": [{"name": "lint", "passed": False, "output": "3 errors"}]}
        result = _build_autofix_prompt("pid-1", "local", results)
        # The shared file content should appear in the prompt
        assert "Auto-fixable" in result or "auto-fixable" in result.lower()


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

    def test_autofix_prompt_falls_back_when_shared_missing(self):
        """_build_autofix_prompt uses inline fallback when shared file is missing."""
        results = {"checks": [{"name": "lint", "passed": False, "output": "err"}]}
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            result = _build_autofix_prompt("pid-1", "local", results)
            assert "Auto-fixable" in result
            assert "Report only" in result


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
                pipeline_mode="local",
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
            pipeline_mode="local",
            prompt="Build a widget",
            repo_path=None,
        )
        assert "Review the plan (check `.egg-state/drafts/`)" in result
        assert "## Plan\n" not in result

    def test_short_circuit_embeds_analysis(self):
        """Short-circuit mode embeds the analysis draft instead of the plan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            drafts_dir = Path(tmpdir) / ".egg-state" / "drafts"
            drafts_dir.mkdir(parents=True)
            (drafts_dir / "42-analysis.md").write_text("## Analysis\nThe root cause is X.")

            result = _build_phase_prompt(
                phase="implement",
                pipeline_id="test-pid",
                pipeline_mode="issue",
                prompt="Fix the bug",
                issue_number=42,
                repo_path=tmpdir,
                short_circuit=True,
            )
            assert "## Analysis" in result
            assert "root cause is X" in result

    def test_fallback_when_draft_file_missing(self):
        """Falls back to file-I/O instruction when draft file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _build_phase_prompt(
                phase="implement",
                pipeline_id="test-pid",
                pipeline_mode="local",
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
                pipeline_mode="local",
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
            pipeline_mode="local",
            prompt="Build a widget with many features",
            review_cycle=1,
            review_feedback="Fix naming",
        )
        assert "## Task Description" not in result
        assert "Build a widget with many features" not in result

    def test_revision_cycle_contains_revision_instructions(self):
        """Cycle 2+ contains revision-focused instructions."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="local",
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
            pipeline_mode="local",
            prompt="Build a widget",
            review_cycle=1,
            review_feedback="Variable naming is inconsistent",
        )
        assert "Variable naming is inconsistent" in result
        assert "Prior Review Feedback" in result

    def test_cycle_0_includes_task_description(self):
        """Cycle 0 still includes the full task description."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="local",
            prompt="Build a widget with many features",
            review_cycle=0,
        )
        assert "## Task Description" in result
        assert "Build a widget with many features" in result

    def test_revision_cycle_without_feedback_includes_task_description(self):
        """Cycle 2+ with no feedback falls back to including the task description."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-pid",
            pipeline_mode="local",
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


class TestBuildCheckAndFixPrompt:
    """Tests for the combined check-and-fix prompt."""

    def test_includes_check_commands(self):
        """Explicit check commands appear in the prompt."""
        checks = [
            {"name": "lint", "command": "make lint"},
            {"name": "test", "command": "make test"},
        ]
        result = _build_check_and_fix_prompt("pid-1", "local", repo="acme/app", repo_checks=checks)
        assert "make lint" in result
        assert "make test" in result
        assert "**lint**" in result
        assert "**test**" in result

    def test_discovery_mode_without_checks(self):
        """Without repo_checks, uses discovery mode."""
        result = _build_check_and_fix_prompt("pid-1", "local")
        assert "Discover" in result
        assert "Makefile" in result

    def test_includes_fix_rules(self):
        """Includes autofixer rules."""
        result = _build_check_and_fix_prompt("pid-1", "local")
        assert "Auto-fixable" in result or "auto-fixable" in result.lower()

    def test_includes_results_file_format(self):
        """Includes the results JSON format."""
        result = _build_check_and_fix_prompt("pid-1", "local")
        assert "implement-results.json" in result
        assert "all_passed" in result

    def test_includes_repeat_workflow(self):
        """Includes repeat-up-to-3-times workflow."""
        result = _build_check_and_fix_prompt("pid-1", "local")
        assert "3 times" in result

    def test_includes_pipeline_metadata(self):
        """Prompt includes pipeline ID and mode."""
        result = _build_check_and_fix_prompt("pid-42", "issue")
        assert "pid-42" in result
        assert "issue" in result

    def test_with_repo_sets_working_directory(self):
        """With repo, sets working directory."""
        result = _build_check_and_fix_prompt("pid-1", "local", repo="acme/web-app")
        assert "Repository: acme/web-app" in result
        assert "~/repos/web-app" in result

    def test_inline_fallback_when_shared_missing(self):
        """Falls back to inline rules when shared file is missing."""
        with patch("routes.pipelines._read_shared_criteria", return_value=None):
            result = _build_check_and_fix_prompt("pid-1", "local")
            assert "Auto-fixable" in result
            assert "Report only" in result


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
        """In issue mode, uses issue_number as contract identifier."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_contract(
                tmpdir,
                42,
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
        result = _build_role_context("documenter", "# Big feature\n\nLots of detail.", issue_number=5)
        assert "## Background" in result
        assert "## Task Description" not in result
        assert "## For More Context" in result

    def test_integrator_gets_summary_not_full_body(self):
        """Integrator receives a summary, not the full issue body."""
        result = _build_role_context("integrator", "# Feature\n\nDetail.", issue_number=10)
        assert "## Background" in result
        assert "## Task Description" not in result

    def test_tester_with_phase_obj_includes_tasks(self):
        """Tester with phase_obj includes phase-scoped task details."""
        task = self._make_task("TASK-1-1", "Add validation", ["models.py"], "Tests pass")
        phase = self._make_phase("phase-1", "Schema", [task])
        result = _build_role_context(
            "tester", "# Issue\n\nBody.", issue_number=1, phase_obj=phase
        )
        assert "Phase Scope" in result
        assert "TASK-1-1" in result
        assert "Add validation" in result
        assert "models.py" in result
        assert "Focus your testing" in result

    def test_documenter_with_phase_obj_includes_tasks(self):
        """Documenter with phase_obj includes phase-scoped task details."""
        task = self._make_task("TASK-2-1", "Update docs", ["README.md"])
        phase = self._make_phase("phase-2", "Docs", [task])
        result = _build_role_context(
            "documenter", "# Issue\n\nBody.", issue_number=1, phase_obj=phase
        )
        assert "Phase Scope" in result
        assert "TASK-2-1" in result
        assert "Focus your documentation" in result

    def test_integrator_with_all_phases_includes_summary(self):
        """Integrator with all_phases includes implementation summary."""
        phases = [
            self._make_phase("phase-1", "Core", [self._make_task()]),
            self._make_phase("phase-2", "Tests", [self._make_task("t2", "Test")]),
        ]
        result = _build_role_context(
            "integrator", "# Issue\n\nBody.", issue_number=1, all_phases=phases
        )
        assert "## Implementation Summary" in result
        assert "phase-1" in result
        assert "phase-2" in result

    def test_tester_with_all_phases_shows_other_phases(self):
        """Tester sees other phases listed for orientation."""
        task = self._make_task()
        phase1 = self._make_phase("phase-1", "Core", [task])
        phase2 = self._make_phase("phase-2", "Tests", [])
        result = _build_role_context(
            "tester", "# Issue", issue_number=1,
            phase_obj=phase1, all_phases=[phase1, phase2],
        )
        assert "Other Phases" in result
        assert "phase-2" in result

    def test_context_pointers_always_present_for_execution_roles(self):
        """All execution roles get 'For More Context' pointers."""
        for role in ("tester", "documenter", "integrator"):
            result = _build_role_context(role, "# Issue\n\nBody.", issue_number=1)
            assert "## For More Context" in result
            assert "gh issue view 1" in result

    def test_no_prompt_for_execution_role(self):
        """Execution role with no prompt still gets context pointers."""
        result = _build_role_context("tester", None, issue_number=5)
        assert "## For More Context" in result
        assert "5" in result


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

    def test_integrator_prompt_has_background_not_task_description(self):
        """Integrator prompt uses Background section, not Task Description."""
        result = _build_agent_prompt(
            role_value="integrator",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=10,
        )
        assert "## Background" in result
        assert "## Task Description" not in result

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
        for role in ("tester", "documenter", "integrator", "architect"):
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


class TestBuildPhaseScopedPromptOverview:
    """Tests for plan overview in _build_phase_scoped_prompt()."""

    def _make_phase(self, phase_id="phase-1", name="Core", tasks=None, status="pending"):
        """Create a mock phase object."""
        phase = MagicMock()
        phase.id = phase_id
        phase.name = name
        phase.tasks = tasks or []
        phase.status = status
        return phase

    def _make_task(self, task_id="task-1", desc="Fix bug", files=None):
        """Create a mock task."""
        task = MagicMock()
        task.id = task_id
        task.description = desc
        task.status = "pending"
        task.acceptance_criteria = "Tests pass"
        task.files_affected = files or []
        return task

    def test_plan_overview_not_full_plan(self, tmp_path):
        """Phase-scoped prompt embeds plan overview, not the full plan."""
        # Lazy import to avoid circular — Pipeline model is in orchestrator/models.py
        from models import Pipeline

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-plan.md").write_text(
            "# Plan: Auth\n\n## Summary\n\nAdd auth to API.\n\n"
            "## Implementation Phases\n\n"
            "### Phase 1: JWT support\n\n**Tasks**:\n- Add JWT\n\n"
            "### Phase 2: Middleware\n\n**Tasks**:\n- Add middleware\n",
            encoding="utf-8",
        )

        phase = self._make_phase("phase-1", "JWT support", [self._make_task()])
        pipeline = Pipeline(id="test-1", issue_number=42, repo="owner/repo", branch="egg/test")

        result = _build_phase_scoped_prompt(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
        )

        assert "## Plan Overview" in result
        assert "Add auth to API" in result
        # Should NOT contain individual phase task details from the plan text
        assert "Add middleware" not in result

    def test_other_phases_listed_for_orientation(self, tmp_path):
        """Other phases appear as one-line summaries."""
        from models import Pipeline

        phase1 = self._make_phase("phase-1", "Core", [self._make_task()])
        phase2 = self._make_phase("phase-2", "Tests", status="complete")
        pipeline = Pipeline(id="test-1", issue_number=42, repo="owner/repo", branch="egg/test")

        result = _build_phase_scoped_prompt(
            phase_obj=phase1,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
            all_phases=[phase1, phase2],
        )

        assert "Other Phases" in result
        assert "phase-2" in result
        assert "Tests" in result

    def test_full_plan_pointer_present(self, tmp_path):
        """Prompt includes pointer to full plan file."""
        from models import Pipeline

        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-plan.md").write_text("# Plan\n\n## Summary\n\nOverview.\n")

        phase = self._make_phase()
        pipeline = Pipeline(id="test-1", issue_number=42, repo="owner/repo", branch="egg/test")

        result = _build_phase_scoped_prompt(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
        )

        assert "For full plan details" in result
        assert "42-plan.md" in result
