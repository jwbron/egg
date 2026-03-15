"""Tests for concurrent execution default behavior.

Verifies that is_concurrent_execution returns True by default so BRC
consensus activates for all multi-agent phases.
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
    concurrent_execution: bool = True,
    current_phase: PipelinePhase = PipelinePhase.IMPLEMENT,
) -> Pipeline:
    """Create a test pipeline."""
    config = PipelineConfig(concurrent_execution=concurrent_execution)
    return Pipeline(
        id="issue-1140",
        repo="test/repo",
        issue_number=1140,
        status=PipelineStatus.RUNNING,
        current_phase=current_phase,
        config=config,
    )


class TestIsConcurrentExecution:
    """Test is_concurrent_execution with default-True behavior."""

    def test_default_config_is_concurrent(self):
        pipeline = _make_pipeline()
        assert is_concurrent_execution(pipeline) is True

    def test_default_config_concurrent_for_all_phases(self):
        for phase in ("refine", "plan", "implement"):
            pipeline = _make_pipeline()
            assert is_concurrent_execution(pipeline, phase=phase) is True

    def test_explicit_false_disables(self):
        pipeline = _make_pipeline(concurrent_execution=False)
        assert is_concurrent_execution(pipeline) is False

    def test_phase_param_accepted(self):
        """phase parameter is accepted for call-site compatibility."""
        pipeline = _make_pipeline()
        assert is_concurrent_execution(pipeline, phase="refine") is True
