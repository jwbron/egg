"""
Tests for checkpoint discovery hints in pipeline prompts.

Validates that egg-checkpoint CLI references are correctly injected into
agent prompts for tester, documenter, integrator, and coder (revision) roles.
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

    def test_integrator_has_checkpoint_pointer(self):
        """Integrator context includes egg-checkpoint pointer."""
        result = _build_role_context("integrator", "# Issue\n\nBody.", issue_number=1)
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

    def test_integrator_prompt_has_context_and_cost_commands(self):
        """Integrator prompt includes both context and cost checkpoint commands."""
        result = _build_agent_prompt(
            role_value="integrator",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files" in result
        assert "egg-checkpoint cost --pipeline $EGG_PIPELINE_ID" in result

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

    def test_integrator_with_all_phases_still_gets_checkpoint_hint(self):
        """Integrator with all_phases still gets checkpoint hints."""
        p1 = MagicMock()
        p1.id = "phase-1"
        p1.name = "Core"
        p1.tasks = []
        p1.status = "complete"

        result = _build_agent_prompt(
            role_value="integrator",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature",
            issue_number=1,
            all_phases=[p1],
        )
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files" in result
        assert "egg-checkpoint cost --pipeline $EGG_PIPELINE_ID" in result


# ---------------------------------------------------------------------------
# Cross-cutting: checkpoint hints flow through _build_agent_prompt end-to-end
# ---------------------------------------------------------------------------


class TestCheckpointHintsEndToEnd:
    """End-to-end tests ensuring checkpoint hints appear in final prompts."""

    def test_all_execution_roles_get_general_checkpoint_pointer(self):
        """All execution roles get the general checkpoint pointer in context."""
        for role in ("tester", "documenter", "integrator"):
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

    def test_integrator_gets_both_general_and_specific_checkpoint_hints(self):
        """Integrator gets the general pointer plus context+cost commands."""
        result = _build_agent_prompt(
            role_value="integrator",
            phase="implement",
            pipeline_id="pid-1",
            pipeline_mode="issue",
            prompt="# Feature\n\nDetail.",
            issue_number=1,
        )
        # General pointer
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID" in result
        # Specific commands
        assert "egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files" in result
        assert "egg-checkpoint cost --pipeline $EGG_PIPELINE_ID" in result

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
