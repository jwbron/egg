"""
Tests for checkpoint discovery hints in pipeline prompts.

Validates that egg-checkpoint CLI references are correctly injected into
agent prompts for tester, documenter, and coder (revision) roles.
See issue #887.
"""

import sys
from unittest.mock import MagicMock

_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from routes.pipelines import (
    _build_agent_prompt,
    _build_phase_scoped_prompt,
    _build_role_context,
)

# ---------------------------------------------------------------------------
# _build_role_context: checkpoint pointer in "For More Context"
# ---------------------------------------------------------------------------


class TestRoleContextCheckpointPointer:
    """Checkpoint pointer appears in 'For More Context' for execution roles."""

    def test_tester_has_checkpoint_pointer(self):
        """Tester context includes egg-checkpoint pointer."""
        result = _build_role_context("tester", "# Issue\n\nBody.", issue_number=1)
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID" in result

    def test_documenter_has_checkpoint_pointer(self):
        """Documenter context includes egg-checkpoint pointer."""
        result = _build_role_context("documenter", "# Issue\n\nBody.", issue_number=1)
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID" in result

    def test_checkpoint_pointer_mentions_checkpoint_rule(self):
        """Checkpoint pointer references the checkpoint rule for more info."""
        result = _build_role_context("tester", "# Issue", issue_number=1)
        assert "checkpoint rule" in result

    def test_checkpoint_pointer_present_without_issue_number(self):
        """Checkpoint pointer appears even when issue_number is None."""
        result = _build_role_context("tester", "# Issue", issue_number=None)
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID" in result

    def test_checkpoint_pointer_present_with_none_prompt(self):
        """Checkpoint pointer appears even when prompt is None."""
        result = _build_role_context("tester", None, issue_number=1)
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID" in result

    def test_analysis_roles_no_checkpoint_pointer(self):
        """Analysis roles (architect, task_planner, risk_analyst) don't get checkpoint pointer."""
        for role in ("architect", "task_planner", "risk_analyst"):
            result = _build_role_context(role, "# Issue\n\nBody.", issue_number=1)
            # Analysis roles return early with task description, no 'For More Context'
            assert "egg-checkpoint" not in result

    def test_checkpoint_pointer_among_other_context_pointers(self):
        """Checkpoint pointer coexists with other context pointers (git diff, handoff)."""
        result = _build_role_context("tester", "# Issue\n\nBody.", issue_number=42)
        assert "git diff HEAD~10..HEAD" in result
        assert "EGG_HANDOFF_DATA" in result
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID" in result
        assert "gh issue view 42" in result


# ---------------------------------------------------------------------------
# _build_agent_prompt: role-specific checkpoint commands
# ---------------------------------------------------------------------------


class TestAgentPromptCheckpointHints:
    """Checkpoint-specific commands appear in role task descriptions."""

    def test_tester_prompt_has_coder_checkpoint_command(self):
        """Tester prompt includes command to list coder's checkpoints."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert (
            "egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement"
            in result
        )

    def test_tester_prompt_checkpoint_appears_after_gap_finding(self):
        """Tester checkpoint command appears after the gap-finding section."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        gap_pos = result.find("Integration gaps between components")
        ckpt_pos = result.find("egg-checkpoint list --pipeline")
        assert gap_pos < ckpt_pos, "Checkpoint hint should come after gap-finding section"

    def test_documenter_prompt_has_context_files_command(self):
        """Documenter prompt includes command to find changed files via checkpoint."""
        result = _build_agent_prompt(
            role_value="documenter",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files" in result

    def test_architect_prompt_has_no_checkpoint_commands(self):
        """Architect prompt does not include checkpoint discovery hints."""
        result = _build_agent_prompt(
            role_value="architect",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "egg-checkpoint" not in result

    def test_task_planner_prompt_has_no_checkpoint_commands(self):
        """Task planner prompt does not include checkpoint discovery hints."""
        result = _build_agent_prompt(
            role_value="task_planner",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "egg-checkpoint" not in result

    def test_risk_analyst_prompt_has_no_checkpoint_commands(self):
        """Risk analyst prompt does not include checkpoint discovery hints."""
        result = _build_agent_prompt(
            role_value="risk_analyst",
            phase="plan",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "egg-checkpoint" not in result

    def test_tester_checkpoint_hint_with_phase_obj(self):
        """Tester with phase_obj still gets checkpoint hint."""
        phase = MagicMock()
        phase.id = "phase-1"
        phase.name = "Core"
        task = MagicMock()
        task.id = "TASK-1-1"
        task.description = "Add validation"
        task.acceptance_criteria = "Tests pass"
        task.files_affected = ["models.py"]
        phase.tasks = [task]

        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
            phase_obj=phase,
        )
        assert (
            "egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement"
            in result
        )

    def test_documenter_with_none_prompt_still_gets_checkpoint_hint(self):
        """Documenter with None prompt still gets checkpoint hint."""
        result = _build_agent_prompt(
            role_value="documenter",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt=None,
            issue_number=1,
        )
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files" in result


# ---------------------------------------------------------------------------
# _build_phase_scoped_prompt: failed session checkpoint hint in revision
# ---------------------------------------------------------------------------


class TestPhaseScopedPromptCheckpointHint:
    """Failed-session checkpoint hint in revision checklist."""

    def _make_phase(self, phase_id="phase-1", name="Core", tasks=None, status="pending"):
        phase = MagicMock()
        phase.id = phase_id
        phase.name = name
        phase.tasks = tasks or []
        phase.status = status
        return phase

    def _make_task(self, task_id="task-1", desc="Fix bug", files=None):
        task = MagicMock()
        task.id = task_id
        task.description = desc
        task.status = "pending"
        task.acceptance_criteria = "Tests pass"
        task.files_affected = files or []
        return task

    def test_revision_checklist_has_failed_session_hint(self, tmp_path):
        """Revision checklist includes egg-checkpoint for failed sessions."""
        from models import Pipeline

        phase = self._make_phase(tasks=[self._make_task()])
        pipeline = Pipeline(id="test-1", issue_number=42, repo="owner/repo", branch="egg/test")

        result = _build_phase_scoped_prompt(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
            review_feedback="Fix the naming convention",
            review_cycle=1,
        )

        assert "egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed" in result

    def test_revision_checklist_failed_hint_inside_checklist(self, tmp_path):
        """Failed session hint appears within the Revision Checklist section."""
        from models import Pipeline

        phase = self._make_phase(tasks=[self._make_task()])
        pipeline = Pipeline(id="test-1", issue_number=42, repo="owner/repo", branch="egg/test")

        result = _build_phase_scoped_prompt(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
            review_feedback="Fix the bugs",
            review_cycle=1,
        )

        checklist_pos = result.find("### Revision Checklist")
        failed_hint_pos = result.find(
            "egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed"
        )
        # The hint must appear after the checklist heading
        assert checklist_pos < failed_hint_pos

    def test_cycle_0_no_failed_session_hint(self, tmp_path):
        """Cycle 0 (first run) does not include the failed session hint."""
        from models import Pipeline

        phase = self._make_phase(tasks=[self._make_task()])
        pipeline = Pipeline(id="test-1", issue_number=42, repo="owner/repo", branch="egg/test")

        result = _build_phase_scoped_prompt(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
            review_cycle=0,
        )

        assert "egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed" not in result

    def test_revision_no_feedback_no_failed_session_hint(self, tmp_path):
        """Revision without feedback does not include failed session hint."""
        from models import Pipeline

        phase = self._make_phase(tasks=[self._make_task()])
        pipeline = Pipeline(id="test-1", issue_number=42, repo="owner/repo", branch="egg/test")

        result = _build_phase_scoped_prompt(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
            review_feedback=None,
            review_cycle=1,
        )

        assert "egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed" not in result

    def test_revision_with_empty_feedback_no_failed_session_hint(self, tmp_path):
        """Revision with empty string feedback does not include failed session hint."""
        from models import Pipeline

        phase = self._make_phase(tasks=[self._make_task()])
        pipeline = Pipeline(id="test-1", issue_number=42, repo="owner/repo", branch="egg/test")

        result = _build_phase_scoped_prompt(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
            review_feedback="",
            review_cycle=1,
        )

        # Empty string is falsy, so no revision checklist
        assert "egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed" not in result

    def test_default_review_cycle_no_failed_session_hint(self, tmp_path):
        """Default review_cycle (0) does not include failed session hint."""
        from models import Pipeline

        phase = self._make_phase(tasks=[self._make_task()])
        pipeline = Pipeline(id="test-1", issue_number=42, repo="owner/repo", branch="egg/test")

        result = _build_phase_scoped_prompt(
            phase_obj=phase,
            pipeline_id="test-1",
            pipeline_mode="issue",
            pipeline=pipeline,
            worktree_repo_path=tmp_path,
        )

        assert "egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed" not in result


# ---------------------------------------------------------------------------
# Cross-cutting: checkpoint hints flow through _build_agent_prompt end-to-end
# ---------------------------------------------------------------------------


class TestCheckpointHintsEndToEnd:
    """End-to-end tests ensuring checkpoint hints appear in final prompts."""

    def test_all_execution_roles_get_general_checkpoint_pointer(self):
        """All execution roles get the general checkpoint pointer in context."""
        for role in ("tester", "documenter"):
            result = _build_agent_prompt(
                role_value=role,
                phase="implement",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Issue\n\nBody.",
                issue_number=1,
            )
            assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID" in result, (
                f"{role} should have general checkpoint pointer"
            )

    def test_tester_gets_both_general_and_specific_checkpoint_hints(self):
        """Tester gets both the general pointer and the coder-specific checkpoint command."""
        result = _build_agent_prompt(
            role_value="tester",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        # General pointer from _build_role_context
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID" in result
        # Specific command for tester
        assert (
            "egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement"
            in result
        )

    def test_documenter_gets_both_general_and_specific_checkpoint_hints(self):
        """Documenter gets both the general pointer and the files-specific command."""
        result = _build_agent_prompt(
            role_value="documenter",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        # General pointer
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID" in result
        # Specific command (note: same command in both, but appears in different sections)
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files" in result

    def test_no_analysis_roles_get_checkpoint_hints(self):
        """No analysis role gets any checkpoint hint."""
        for role in ("architect", "task_planner", "risk_analyst"):
            result = _build_agent_prompt(
                role_value=role,
                phase="plan",
                pipeline_id="pid-1",
                pipeline_mode="issue",
                prompt="# Feature\n\nDetail.",
                issue_number=1,
            )
            assert "egg-checkpoint" not in result, f"{role} should not have any checkpoint hints"
