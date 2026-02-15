"""Tests for short-circuit pipeline mode (skip plan phase for low-complexity tasks)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus
from routes.phases import (
    LOCAL_PHASE_TRANSITIONS,
    PHASE_TRANSITIONS,
    validate_phase_transition,
)
from routes.pipelines import _build_phase_prompt, _check_short_circuit_signal


class TestShortCircuitSignalParsing:
    """Tests for _check_short_circuit_signal."""

    def test_detects_signal_in_yaml_block(self, tmp_path: Path):
        """Detects short_circuit: true in a fenced YAML block."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\nSome content\n\n```yaml\n# metadata\nshort_circuit: true\ncomplexity: low\n```\n",
            encoding="utf-8",
        )

        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is True

    def test_no_signal_when_absent(self, tmp_path: Path):
        """Returns False when no short-circuit signal is present."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\nSome content\n\n## Recommended Approach\n\nDo X.\n",
            encoding="utf-8",
        )

        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is False

    def test_no_signal_when_false(self, tmp_path: Path):
        """Returns False when short_circuit is explicitly false."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\nshort_circuit: false\ncomplexity: high\n```\n",
            encoding="utf-8",
        )

        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is False

    def test_no_signal_when_draft_missing(self, tmp_path: Path):
        """Returns False when the draft file doesn't exist."""
        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is False

    def test_local_mode_signal(self, tmp_path: Path):
        """Detects signal in local-mode draft."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "pid-123-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\nshort_circuit: true\ncomplexity: low\n```\n",
            encoding="utf-8",
        )

        assert _check_short_circuit_signal(tmp_path, "local", pipeline_id="pid-123") is True

    def test_ignores_yaml_without_short_circuit_key(self, tmp_path: Path):
        """Returns False when YAML block exists but has no short_circuit key."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# yaml-tasks\nphases:\n  - id: 1\n```\n",
            encoding="utf-8",
        )

        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is False

    def test_detects_signal_with_yml_fence(self, tmp_path: Path):
        """Also matches ```yml fence variant."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yml\nshort_circuit: true\n```\n",
            encoding="utf-8",
        )

        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is True

    def test_only_checks_last_yaml_block(self, tmp_path: Path):
        """Only the last YAML block is checked, not earlier ones."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        # First YAML block has signal, last one does not → should return False
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n"
            "Here is an example metadata block:\n"
            "```yaml\nshort_circuit: true\ncomplexity: low\n```\n\n"
            "## Conclusion\n\n"
            "```yaml\n# metadata\ncomplexity: high\n```\n",
            encoding="utf-8",
        )

        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is False

    def test_signal_in_last_of_multiple_yaml_blocks(self, tmp_path: Path):
        """Signal in the last YAML block is detected even with earlier blocks."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n"
            "```yaml\nphases:\n  - id: 1\n```\n\n"
            "## Metadata\n\n"
            "```yaml\n# metadata\nshort_circuit: true\ncomplexity: low\n```\n",
            encoding="utf-8",
        )

        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is True


class TestShortCircuitModel:
    """Tests for Pipeline.short_circuit field and PipelineConfig.allow_short_circuit."""

    def test_pipeline_short_circuit_defaults_false(self):
        """Pipeline.short_circuit defaults to False."""
        p = Pipeline(id="test-1")
        assert p.short_circuit is False

    def test_pipeline_short_circuit_set_true(self):
        """Pipeline.short_circuit can be set to True."""
        p = Pipeline(id="test-1")
        p.short_circuit = True
        assert p.short_circuit is True

    def test_pipeline_short_circuit_serialization(self):
        """short_circuit field round-trips through model_dump."""
        p = Pipeline(id="test-1", short_circuit=True)
        data = p.model_dump()
        assert data["short_circuit"] is True
        p2 = Pipeline(**data)
        assert p2.short_circuit is True

    def test_config_allow_short_circuit_defaults_true(self):
        """PipelineConfig.allow_short_circuit defaults to True."""
        config = PipelineConfig()
        assert config.allow_short_circuit is True

    def test_config_allow_short_circuit_disabled(self):
        """PipelineConfig.allow_short_circuit can be disabled."""
        config = PipelineConfig(allow_short_circuit=False)
        assert config.allow_short_circuit is False


class TestShortCircuitPhaseSkip:
    """Tests for plan phase being skipped when short_circuit=True."""

    def test_plan_phase_marked_complete_on_short_circuit(self):
        """When short_circuit is set, plan phase should be markable as complete."""
        p = Pipeline(id="test-1", short_circuit=True)
        plan_exec = p.get_phase_execution(PipelinePhase.PLAN)
        plan_exec.status = PipelineStatus.COMPLETE
        assert plan_exec.status == PipelineStatus.COMPLETE

    def test_phase_can_advance_refine_to_implement(self):
        """Pipeline current_phase can be set from REFINE to IMPLEMENT directly."""
        p = Pipeline(id="test-1", short_circuit=True, current_phase=PipelinePhase.REFINE)
        p.current_phase = PipelinePhase.IMPLEMENT
        assert p.current_phase == PipelinePhase.IMPLEMENT


class TestPhaseTransitions:
    """Tests for REFINE → IMPLEMENT being a valid transition."""

    def test_issue_mode_refine_to_implement_valid(self):
        """REFINE → IMPLEMENT is valid in issue mode transitions."""
        assert PipelinePhase.IMPLEMENT in PHASE_TRANSITIONS[PipelinePhase.REFINE]

    def test_local_mode_refine_to_implement_valid(self):
        """REFINE → IMPLEMENT is valid in local mode transitions."""
        assert PipelinePhase.IMPLEMENT in LOCAL_PHASE_TRANSITIONS[PipelinePhase.REFINE]

    def test_refine_to_plan_still_valid(self):
        """REFINE → PLAN remains valid (default path)."""
        assert PipelinePhase.PLAN in PHASE_TRANSITIONS[PipelinePhase.REFINE]

    def test_validate_refine_to_implement(self):
        """validate_phase_transition accepts REFINE → IMPLEMENT."""
        is_valid, error = validate_phase_transition(
            PipelinePhase.REFINE, PipelinePhase.IMPLEMENT, pipeline_mode="issue"
        )
        assert is_valid is True
        assert error == ""

    def test_validate_refine_to_implement_local(self):
        """validate_phase_transition accepts REFINE → IMPLEMENT in local mode."""
        is_valid, error = validate_phase_transition(
            PipelinePhase.REFINE, PipelinePhase.IMPLEMENT, pipeline_mode="local"
        )
        assert is_valid is True
        assert error == ""


class TestShortCircuitPrompt:
    """Tests for implement phase prompt in short-circuit mode."""

    def test_implement_prompt_references_analysis_when_short_circuit(self):
        """In short-circuit mode, implement prompt references analysis, not plan."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-1",
            pipeline_mode="issue",
            issue_number=42,
            short_circuit=True,
        )
        assert "analysis" in result.lower()
        assert "plan phase was skipped" in result.lower()

    def test_implement_prompt_references_plan_normally(self):
        """Without short-circuit, implement prompt references plan as usual."""
        result = _build_phase_prompt(
            phase="implement",
            pipeline_id="test-1",
            pipeline_mode="issue",
            issue_number=42,
            short_circuit=False,
        )
        assert "Review the plan" in result

    def test_refine_prompt_includes_complexity_assessment(self):
        """Refine prompt includes complexity assessment instructions."""
        result = _build_phase_prompt(
            phase="refine",
            pipeline_id="test-1",
            pipeline_mode="issue",
            issue_number=42,
        )
        assert "Complexity Assessment" in result
        assert "short_circuit: true" in result
        assert "complexity: low" in result


class TestShortCircuitHITLRevision:
    """Tests for short-circuit flag being correctly reset after HITL revision."""

    def test_revised_analysis_without_signal_clears_short_circuit(self, tmp_path: Path):
        """After HITL revision removes signal, re-check should return False."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)

        # First pass: analysis has short-circuit signal
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\nshort_circuit: true\ncomplexity: low\n```\n",
            encoding="utf-8",
        )
        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is True

        # Simulate HITL revision: human elevates complexity, agent rewrites analysis
        (drafts / "42-analysis.md").write_text(
            "# Analysis (revised)\n\nThis is actually complex.\n\n"
            "```yaml\n# metadata\ncomplexity: high\n```\n",
            encoding="utf-8",
        )
        # The pipeline runner resets pipeline.short_circuit = False before re-check.
        # Verify that re-checking the revised analysis returns False.
        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is False

    def test_revised_analysis_with_signal_keeps_short_circuit(self, tmp_path: Path):
        """After HITL revision that keeps signal, re-check should still return True."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)

        (drafts / "42-analysis.md").write_text(
            "# Analysis (revised)\n\nStill simple.\n\n"
            "```yaml\n# metadata\nshort_circuit: true\ncomplexity: low\n```\n",
            encoding="utf-8",
        )
        assert _check_short_circuit_signal(tmp_path, "issue", issue_number=42) is True

    def test_pipeline_short_circuit_reset_before_recheck(self, tmp_path: Path):
        """Simulates the reset-before-recheck pattern used in the pipeline runner."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)

        pipeline = Pipeline(id="test-1", short_circuit=True)

        # HITL revision produces analysis without signal
        (drafts / "42-analysis.md").write_text(
            "# Revised Analysis\n\nComplex task requiring plan.\n",
            encoding="utf-8",
        )

        # Simulate the runner's reset-before-recheck pattern
        pipeline.short_circuit = False
        if _check_short_circuit_signal(tmp_path, "issue", issue_number=42):
            pipeline.short_circuit = True

        assert pipeline.short_circuit is False


class TestShortCircuitConfigSuppression:
    """Tests for allow_short_circuit=False suppressing signal detection."""

    def test_signal_ignored_when_config_disallows(self, tmp_path: Path):
        """When allow_short_circuit=False, signal should not activate short-circuit."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\nshort_circuit: true\ncomplexity: low\n```\n",
            encoding="utf-8",
        )

        pipeline = Pipeline(
            id="test-1",
            config=PipelineConfig(allow_short_circuit=False),
        )

        # Simulate the runner's guard: only check signal if config allows
        if pipeline.config.allow_short_circuit:
            pipeline.short_circuit = False
            if _check_short_circuit_signal(tmp_path, "issue", issue_number=42):
                pipeline.short_circuit = True

        assert pipeline.short_circuit is False

    def test_signal_detected_when_config_allows(self, tmp_path: Path):
        """When allow_short_circuit=True (default), signal activates short-circuit."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "42-analysis.md").write_text(
            "# Analysis\n\n```yaml\n# metadata\nshort_circuit: true\ncomplexity: low\n```\n",
            encoding="utf-8",
        )

        pipeline = Pipeline(
            id="test-1",
            config=PipelineConfig(allow_short_circuit=True),
        )

        # Simulate the runner's guard
        if pipeline.config.allow_short_circuit:
            pipeline.short_circuit = False
            if _check_short_circuit_signal(tmp_path, "issue", issue_number=42):
                pipeline.short_circuit = True

        assert pipeline.short_circuit is True
