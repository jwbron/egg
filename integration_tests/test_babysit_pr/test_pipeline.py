"""Integration tests for babysit pipeline model compatibility."""

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.types import BabysitExitReason, BabysitResult, BabysitStep


@pytest.mark.integration
class TestPipelineBabysitMode:
    """Test that babysit config integrates with pipeline concepts."""

    def test_pipeline_babysit_mode(self):
        """BabysitConfig can represent a babysit-mode pipeline."""
        config = BabysitConfig(
            pr_number=123,
            repo="owner/repo",
            pipeline_id="pr-123",
            orchestrator_url="http://localhost:9800",
        )
        assert config.pr_number == 123
        assert config.pipeline_id == "pr-123"
        assert config.orchestrator_url == "http://localhost:9800"

    def test_pipeline_pr_number(self):
        """PR number field works correctly."""
        config = BabysitConfig(pr_number=456, repo="org/project")
        assert config.pr_number == 456

    def test_pipeline_id_format(self):
        """pr-N format is accepted as pipeline_id."""
        config = BabysitConfig(
            pr_number=789,
            repo="org/project",
            pipeline_id="pr-789",
        )
        assert config.pipeline_id == "pr-789"
        assert config.pipeline_id.startswith("pr-")

    def test_babysit_result_is_serializable(self):
        """BabysitResult fields are all basic types suitable for JSON."""
        result = BabysitResult(
            exit_reason=BabysitExitReason.MERGED,
            iterations=3,
            duration_seconds=120.5,
            last_step=BabysitStep.DONE,
            message="PR merged successfully",
        )
        # All fields should be convertible to basic types
        assert isinstance(result.exit_reason.value, str)
        assert isinstance(result.iterations, int)
        assert isinstance(result.duration_seconds, float)
        assert isinstance(result.last_step.value, str)
        assert isinstance(result.message, str)
