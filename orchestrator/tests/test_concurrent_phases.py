"""Tests for per-phase concurrent execution (BRC activation).

Verifies that is_concurrent_execution correctly activates BRC for phases
listed in concurrent_phases, even when the global concurrent_execution
flag is False.
"""

import sys
from pathlib import Path

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus
from multi_agent import is_concurrent_execution


def _make_pipeline(
    concurrent_execution: bool = False,
    concurrent_phases: list[str] | None = None,
    current_phase: PipelinePhase = PipelinePhase.IMPLEMENT,
) -> Pipeline:
    """Create a test pipeline with configurable concurrent settings."""
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
    """Test is_concurrent_execution with phase-aware logic."""

    def test_global_flag_true_enables_all_phases(self):
        pipeline = _make_pipeline(concurrent_execution=True)
        assert is_concurrent_execution(pipeline) is True
        assert is_concurrent_execution(pipeline, phase="refine") is True
        assert is_concurrent_execution(pipeline, phase="plan") is True
        assert is_concurrent_execution(pipeline, phase="implement") is True

    def test_global_flag_false_no_phase_returns_false(self):
        pipeline = _make_pipeline(concurrent_execution=False)
        assert is_concurrent_execution(pipeline) is False

    def test_default_concurrent_phases_enables_refine_plan_implement(self):
        """Default concurrent_phases includes all three phases."""
        pipeline = _make_pipeline(concurrent_execution=False)
        assert is_concurrent_execution(pipeline, phase="refine") is True
        assert is_concurrent_execution(pipeline, phase="plan") is True
        assert is_concurrent_execution(pipeline, phase="implement") is True

    def test_custom_concurrent_phases(self):
        pipeline = _make_pipeline(
            concurrent_execution=False,
            concurrent_phases=["refine"],
        )
        assert is_concurrent_execution(pipeline, phase="refine") is True
        assert is_concurrent_execution(pipeline, phase="plan") is False
        assert is_concurrent_execution(pipeline, phase="implement") is False

    def test_empty_concurrent_phases(self):
        pipeline = _make_pipeline(
            concurrent_execution=False,
            concurrent_phases=[],
        )
        assert is_concurrent_execution(pipeline, phase="refine") is False
        assert is_concurrent_execution(pipeline, phase="plan") is False
        assert is_concurrent_execution(pipeline, phase="implement") is False

    def test_unknown_phase_returns_false(self):
        pipeline = _make_pipeline(concurrent_execution=False)
        assert is_concurrent_execution(pipeline, phase="integrate") is False

    def test_backward_compat_no_concurrent_phases_attr(self):
        """Pipelines without concurrent_phases still work via global flag."""
        pipeline = _make_pipeline(concurrent_execution=True)
        # Simulate old config without the field
        if hasattr(pipeline.config, "concurrent_phases"):
            delattr(pipeline.config, "concurrent_phases")
        assert is_concurrent_execution(pipeline, phase="refine") is True
