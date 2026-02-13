"""
Tests for PipelineDispatcher.
"""

from pathlib import Path

from dispatch import PipelineDispatcher
from models import Pipeline


class TestContractKey:
    """Tests for PipelineDispatcher.contract_key."""

    def test_issue_mode_returns_issue_number(self):
        """Issue-mode pipelines use the issue number as contract key."""
        pipeline = Pipeline(
            id="issue-496",
            issue_number=496,
            repo="owner/repo",
            branch="egg/issue-496",
        )
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))
        assert dispatcher.contract_key == 496

    def test_local_mode_returns_pipeline_id(self):
        """Local-mode pipelines use the pipeline ID as contract key."""
        pipeline = Pipeline(
            id="local-47601d1d",
            repo="owner/repo",
            branch="egg/local-47601d1d",
        )
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))
        assert dispatcher.contract_key == "local-47601d1d"

    def test_issue_mode_returns_int(self):
        """Issue-mode contract key is an int."""
        pipeline = Pipeline(
            id="issue-42",
            issue_number=42,
        )
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))
        assert isinstance(dispatcher.contract_key, int)

    def test_local_mode_returns_str(self):
        """Local-mode contract key is a str."""
        pipeline = Pipeline(id="local-abc123")
        dispatcher = PipelineDispatcher(pipeline, Path("/tmp/repo"))
        assert isinstance(dispatcher.contract_key, str)
