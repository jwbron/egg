"""Tests for per-phase concurrent execution via ``concurrent_phases``.

Verifies that ``is_concurrent_execution`` activates BRC for phases listed in
``concurrent_phases`` even when the global ``concurrent_execution`` flag is
``False``.  Also tests the ``concurrent_phases`` field validator and backward
compatibility with configs that lack the field.
"""

import sys
from pathlib import Path

import pytest

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus
from concurrent_executor import is_concurrent_execution


def _make_pipeline(
    concurrent_execution: bool = False,
    concurrent_phases: list[str] | None = None,
    current_phase: PipelinePhase = PipelinePhase.IMPLEMENT,
) -> Pipeline:
    """Create a test pipeline with the given config."""
    kwargs: dict = {"concurrent_execution": concurrent_execution}
    if concurrent_phases is not None:
        kwargs["concurrent_phases"] = concurrent_phases
    config = PipelineConfig(**kwargs)
    return Pipeline(
        id="issue-1140",
        repo="test/repo",
        issue_number=1140,
        status=PipelineStatus.RUNNING,
        current_phase=current_phase,
        config=config,
    )


class TestIsConcurrentExecution:
    """Test is_concurrent_execution with concurrent_phases."""

    def test_global_flag_overrides_phases(self):
        """concurrent_execution=True enables BRC for any phase."""
        pipeline = _make_pipeline(concurrent_execution=True)
        assert is_concurrent_execution(pipeline, phase="pr") is True

    def test_global_flag_off_no_phase_returns_false(self):
        """With global flag off and no phase, returns False."""
        pipeline = _make_pipeline()
        assert is_concurrent_execution(pipeline) is False

    def test_default_phases_enable_refine(self):
        pipeline = _make_pipeline()
        assert is_concurrent_execution(pipeline, phase="refine") is True

    def test_default_phases_enable_plan(self):
        pipeline = _make_pipeline()
        assert is_concurrent_execution(pipeline, phase="plan") is True

    def test_default_phases_enable_implement(self):
        pipeline = _make_pipeline()
        assert is_concurrent_execution(pipeline, phase="implement") is True

    def test_default_phases_exclude_pr(self):
        pipeline = _make_pipeline()
        assert is_concurrent_execution(pipeline, phase="pr") is False

    def test_custom_phases(self):
        pipeline = _make_pipeline(concurrent_phases=["refine"])
        assert is_concurrent_execution(pipeline, phase="refine") is True
        assert is_concurrent_execution(pipeline, phase="plan") is False
        assert is_concurrent_execution(pipeline, phase="implement") is False

    def test_empty_phases_disables_all(self):
        pipeline = _make_pipeline(concurrent_phases=[])
        assert is_concurrent_execution(pipeline, phase="refine") is False
        assert is_concurrent_execution(pipeline, phase="plan") is False
        assert is_concurrent_execution(pipeline, phase="implement") is False

    def test_unknown_phase_returns_false(self):
        pipeline = _make_pipeline()
        assert is_concurrent_execution(pipeline, phase="nonexistent") is False


class TestConcurrentPhasesValidator:
    """Test the field_validator on concurrent_phases."""

    def test_valid_phases_accepted(self):
        config = PipelineConfig(concurrent_phases=["refine", "plan"])
        assert config.concurrent_phases == ["refine", "plan"]

    def test_invalid_phase_rejected(self):
        with pytest.raises(ValueError, match="Invalid phase names"):
            PipelineConfig(concurrent_phases=["refine", "implment"])

    def test_empty_list_accepted(self):
        config = PipelineConfig(concurrent_phases=[])
        assert config.concurrent_phases == []


class TestBackwardCompatibility:
    """Test backward compat with configs lacking concurrent_phases."""

    def test_old_config_without_concurrent_phases(self):
        """Configs without concurrent_phases attribute fall back to empty list."""

        class OldConfig:
            concurrent_execution = False

        pipeline = Pipeline(
            id="issue-1140",
            repo="test/repo",
            issue_number=1140,
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        # Simulate an old config object without concurrent_phases
        object.__setattr__(pipeline, "config", OldConfig())
        assert is_concurrent_execution(pipeline, phase="implement") is False

    def test_old_config_with_global_flag(self):
        """Configs with concurrent_execution=True still work."""

        class OldConfig:
            concurrent_execution = True

        pipeline = Pipeline(
            id="issue-1140",
            repo="test/repo",
            issue_number=1140,
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        object.__setattr__(pipeline, "config", OldConfig())
        assert is_concurrent_execution(pipeline, phase="implement") is True
