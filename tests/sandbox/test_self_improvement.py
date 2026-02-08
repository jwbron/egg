"""Tests for self_improvement module.

The self_improvement module provides log collection utilities for egg's
self-improvement cycle. The actual analysis and issue creation is handled
by egg itself, following agent-mode design principles.
"""

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.self_improvement import (
    LogCollector,
    RunLog,
    collect_run_summary,
    format_markdown_summary,
    format_partition_markdown,
    partition_runs,
)
from egg_lib.self_improvement.collect import MAX_RUNS_PER_PARTITION, truncate_logs
from egg_lib.self_improvement.collectors.gha import GHALogCollector
from egg_lib.self_improvement.collectors.local import LocalLogCollector
from egg_lib.self_improvement.config import (
    BOT_USERNAME,
    DEFAULT_SINCE_HOURS,
    EGG_WORKFLOWS,
)


class TestRunLog:
    """Tests for RunLog dataclass."""

    def test_create_runlog_minimal(self):
        """RunLog can be created with required fields."""
        now = datetime.now(UTC)
        log = RunLog(
            run_id="test-123",
            source="gha",
            started_at=now,
            completed_at=now,
            status="success",
            trigger="issue_comment",
            logs="test logs",
        )
        assert log.run_id == "test-123"
        assert log.source == "gha"
        assert log.status == "success"
        assert log.metadata == {}

    def test_create_runlog_with_metadata(self):
        """RunLog can include optional metadata."""
        now = datetime.now(UTC)
        log = RunLog(
            run_id="test-456",
            source="local",
            started_at=now,
            completed_at=None,
            status="running",
            trigger="exec",
            logs="",
            metadata={"task_id": "task-123", "workflow": "on-mention.yml"},
        )
        assert log.metadata["task_id"] == "task-123"
        assert log.completed_at is None


class TestLogCollectorABC:
    """Tests for LogCollector abstract base class."""

    def test_cannot_instantiate_abstract(self):
        """LogCollector cannot be instantiated directly."""
        with pytest.raises(TypeError):
            LogCollector()  # type: ignore[abstract]

    def test_subclass_must_implement_collect(self):
        """Subclasses must implement collect()."""

        class IncompleteCollector(LogCollector):
            pass

        with pytest.raises(TypeError):
            IncompleteCollector()  # type: ignore[abstract]


class TestConfig:
    """Tests for self_improvement config."""

    def test_default_values(self):
        """Config has expected default values."""
        assert DEFAULT_SINCE_HOURS == 24
        assert "on-mention.yml" in EGG_WORKFLOWS
        assert "on-pull-request.yml" in EGG_WORKFLOWS

    def test_bot_username_default(self):
        """BOT_USERNAME has a default value."""
        assert BOT_USERNAME is not None
        assert len(BOT_USERNAME) > 0


class TestLocalLogCollector:
    """Tests for LocalLogCollector."""

    def test_collect_returns_empty_when_no_index(self):
        """Returns empty list when log index doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            collector = LocalLogCollector(logs_dir=Path(tmpdir))
            since = datetime.now(UTC) - timedelta(hours=1)
            runs = collector.collect(since)
            assert runs == []

    def test_collect_filters_by_since(self):
        """Only returns entries after the since timestamp."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)

            # Create index with entries at different times
            now = datetime.now(UTC)
            old_time = (now - timedelta(hours=2)).isoformat()
            recent_time = now.isoformat()

            index = {
                "task_to_container": {},
                "thread_to_task": {},
                "entries": [
                    {
                        "container_id": "old-container",
                        "task_id": None,
                        "timestamp": old_time,
                        "log_file": None,
                    },
                    {
                        "container_id": "recent-container",
                        "task_id": None,
                        "timestamp": recent_time,
                        "log_file": None,
                    },
                ],
            }

            index_file = logs_dir / "log-index.json"
            index_file.write_text(json.dumps(index))

            collector = LocalLogCollector(logs_dir=logs_dir)
            since = now - timedelta(hours=1)
            runs = collector.collect(since)

            # Should only get the recent entry
            assert len(runs) == 1
            assert runs[0].run_id == "recent-container"

    def test_infer_status_from_logs(self):
        """Status is inferred from log content."""
        collector = LocalLogCollector()

        # Test success inference
        assert collector._infer_status("Task completed successfully") == "success"
        assert collector._infer_status("egg finished successfully") == "success"

        # Test failure inference - patterns at line start
        assert collector._infer_status("Error: something went wrong") == "failure"
        assert collector._infer_status("FAILED: test_example") == "failure"
        assert collector._infer_status("Traceback (most recent call last):\n  File...") == "failure"
        assert collector._infer_status("FATAL: could not connect") == "failure"

        # These should NOT trigger failure (patterns not at line start)
        assert collector._infer_status("fixed the error from yesterday") == "success"
        assert collector._infer_status("tests that previously failed now pass") == "success"

        # Test default (no clear indicators)
        assert collector._infer_status("Just some normal output") == "success"


class TestGHALogCollector:
    """Tests for GHALogCollector."""

    def test_init_with_explicit_repo(self):
        """Can initialize with explicit repo name."""
        collector = GHALogCollector(repo="owner/repo")
        assert collector.repo == "owner/repo"

    @patch("subprocess.run")
    def test_get_repo_from_gh_cli(self, mock_run: MagicMock):
        """Falls back to gh CLI for repo detection."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="detected/repo\n",
        )

        collector = GHALogCollector()
        assert collector.repo == "detected/repo"

    @patch("subprocess.run")
    def test_collect_returns_empty_on_api_error(self, mock_run: MagicMock):
        """Returns empty list when API call fails."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="API error",
        )

        collector = GHALogCollector(repo="test/repo")
        since = datetime.now(UTC) - timedelta(hours=1)
        runs = collector.collect(since)

        assert runs == []

    @patch("subprocess.run")
    def test_filters_to_egg_workflows(self, mock_run: MagicMock):
        """Only includes runs from egg workflows."""
        now = datetime.now(UTC)

        # First call: repo detection
        # Second call: fetch runs
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="test/repo\n"),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "id": 1,
                            "path": ".github/workflows/on-mention.yml",
                            "created_at": now.isoformat(),
                            "updated_at": now.isoformat(),
                            "status": "completed",
                            "conclusion": "success",
                            "event": "issue_comment",
                            "name": "egg: On Mention",
                        },
                        {
                            "id": 2,
                            "path": ".github/workflows/test.yml",
                            "created_at": now.isoformat(),
                            "updated_at": now.isoformat(),
                            "status": "completed",
                            "conclusion": "success",
                            "event": "push",
                            "name": "Test",
                        },
                    ]
                ),
            ),
        ]

        collector = GHALogCollector()
        since = now - timedelta(hours=1)
        runs = collector._fetch_workflow_runs(since)

        # Should only include the on-mention workflow
        assert len(runs) == 1
        assert runs[0]["id"] == 1

    @patch("subprocess.run")
    def test_process_run_invokes_gh_run_view(self, mock_run: MagicMock):
        """_process_run invokes gh run view with correct arguments."""
        now = datetime.now(UTC)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Job log content here",
        )

        collector = GHALogCollector(repo="owner/repo")
        run = {
            "id": 12345,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "status": "completed",
            "conclusion": "success",
            "event": "push",
            "name": "Test Workflow",
            "head_branch": "main",
            "run_number": 42,
            "html_url": "https://github.com/owner/repo/actions/runs/12345",
            "path": ".github/workflows/test.yml",
        }

        result = collector._process_run(run)

        # Verify gh run view was called with correct arguments
        mock_run.assert_called_once_with(
            ["gh", "run", "view", "12345", "--repo", "owner/repo", "--log"],
            capture_output=True,
            text=True,
            check=False,
        )

        # Verify the result
        assert result is not None
        assert result.run_id == "12345"
        assert result.logs == "Job log content here"
        assert result.status == "success"
        assert result.metadata["workflow"] == "Test Workflow"

    @patch("subprocess.run")
    def test_process_run_returns_none_on_failure(self, mock_run: MagicMock):
        """_process_run returns None when gh run view fails."""
        now = datetime.now(UTC)
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="failed to fetch logs",
        )

        collector = GHALogCollector(repo="owner/repo")
        run = {
            "id": 99999,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "status": "in_progress",
            "conclusion": None,
            "event": "push",
        }

        result = collector._process_run(run)

        # Should return None when command fails
        assert result is None


class TestCollect:
    """Tests for the collect module."""

    def test_truncate_logs_short_content(self):
        """Short logs are not truncated."""
        short_log = "This is a short log"
        result = truncate_logs(short_log, max_chars=100)
        assert result == short_log

    def test_truncate_logs_long_content(self):
        """Long logs are truncated with indicator."""
        long_log = "x" * 1000
        result = truncate_logs(long_log, max_chars=100)

        # Should have truncation indicator
        assert "truncated" in result
        # Should be shorter than original
        assert len(result) < len(long_log)
        # Should preserve some content from start and end
        assert result.startswith("x")
        assert result.endswith("x")

    def test_truncate_logs_preserves_head_and_tail(self):
        """Truncation preserves content from both ends."""
        # Create log with distinct start and end
        log = "START" + ("x" * 1000) + "END"
        result = truncate_logs(log, max_chars=100)

        assert "START" in result
        assert "END" in result
        assert "truncated" in result

    @patch.object(GHALogCollector, "collect")
    def test_collect_run_summary_structure(self, mock_collect: MagicMock):
        """collect_run_summary returns expected structure."""
        now = datetime.now(UTC)
        mock_collect.return_value = [
            RunLog(
                run_id="123",
                source="gha",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="issue_comment",
                logs="Error: test failure",
                metadata={
                    "workflow": "on-mention.yml",
                    "head_branch": "main",
                    "html_url": "https://github.com/test/repo/actions/runs/123",
                },
            ),
            RunLog(
                run_id="456",
                source="gha",
                started_at=now,
                completed_at=now,
                status="success",
                trigger="push",
                logs="All tests passed",
                metadata={"workflow": "on-pull-request.yml"},
            ),
        ]

        collector = GHALogCollector(repo="test/repo")
        since = now - timedelta(hours=1)
        result = collect_run_summary(collector, since)

        # Check structure
        assert "collected_at" in result
        assert "since" in result
        assert "repository" in result
        assert "statistics" in result
        assert "failed_runs" in result
        assert "successful_runs" in result
        assert "runs_to_analyze" in result

        # Check statistics
        assert result["statistics"]["total_runs"] == 2
        assert result["statistics"]["failed_runs"] == 1
        assert result["statistics"]["successful_runs"] == 1

        # Check runs_to_analyze includes all runs with log excerpts
        assert len(result["runs_to_analyze"]) == 2
        # Failed runs come first
        assert result["runs_to_analyze"][0]["run_id"] == "123"
        assert result["runs_to_analyze"][0]["status"] == "failure"
        assert "log_excerpt" in result["runs_to_analyze"][0]
        # Successful runs have log excerpts too (for analyzing tool errors)
        assert result["runs_to_analyze"][1]["run_id"] == "456"
        assert result["runs_to_analyze"][1]["status"] == "success"
        assert "log_excerpt" in result["runs_to_analyze"][1]

        # Check failed_runs and successful_runs are still present (backwards compat)
        assert len(result["failed_runs"]) == 1
        assert result["failed_runs"][0]["run_id"] == "123"
        assert len(result["successful_runs"]) == 1
        assert result["successful_runs"][0]["run_id"] == "456"

    @patch.object(GHALogCollector, "collect")
    def test_collect_run_summary_empty(self, mock_collect: MagicMock):
        """collect_run_summary handles empty results."""
        mock_collect.return_value = []

        collector = GHALogCollector(repo="test/repo")
        since = datetime.now(UTC) - timedelta(hours=1)
        result = collect_run_summary(collector, since)

        assert result["statistics"]["total_runs"] == 0
        assert result["statistics"]["failed_runs"] == 0
        assert result["failed_runs"] == []
        assert result["successful_runs"] == []
        assert result["runs_to_analyze"] == []

    @patch.object(GHALogCollector, "collect")
    def test_format_markdown_summary_with_failures(self, mock_collect: MagicMock):
        """format_markdown_summary includes failed run details."""
        now = datetime.now(UTC)
        mock_collect.return_value = [
            RunLog(
                run_id="123",
                source="gha",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="issue_comment",
                logs="Error: gateway connection failed",
                metadata={
                    "workflow": "on-mention.yml",
                    "workflow_path": ".github/workflows/on-mention.yml",
                    "head_branch": "main",
                    "html_url": "https://github.com/test/repo/actions/runs/123",
                    "run_number": 42,
                },
            ),
        ]

        collector = GHALogCollector(repo="test/repo")
        since = now - timedelta(hours=1)
        data = collect_run_summary(collector, since)
        markdown = format_markdown_summary(data)

        # Check key sections are present
        assert "## Pre-Collected Run Data" in markdown
        assert "### Statistics" in markdown
        assert "### Runs to Analyze" in markdown
        assert "Run 123" in markdown
        assert "on-mention.yml" in markdown
        assert "gateway connection failed" in markdown
        # Failed runs should have failure emoji
        assert "❌" in markdown

    @patch.object(GHALogCollector, "collect")
    def test_format_markdown_summary_no_runs(self, mock_collect: MagicMock):
        """format_markdown_summary indicates when no runs to analyze."""
        mock_collect.return_value = []

        collector = GHALogCollector(repo="test/repo")
        since = datetime.now(UTC) - timedelta(hours=1)
        data = collect_run_summary(collector, since)
        markdown = format_markdown_summary(data)

        assert "### No Runs to Analyze" in markdown

    @patch.object(GHALogCollector, "collect")
    def test_format_markdown_summary_includes_successful_runs(self, mock_collect: MagicMock):
        """format_markdown_summary includes successful runs with log excerpts."""
        now = datetime.now(UTC)
        mock_collect.return_value = [
            RunLog(
                run_id="456",
                source="gha",
                started_at=now,
                completed_at=now,
                status="success",
                trigger="push",
                logs="Tool error: retry succeeded",
                metadata={
                    "workflow": "on-pull-request.yml",
                    "head_branch": "main",
                    "html_url": "https://github.com/test/repo/actions/runs/456",
                },
            ),
        ]

        collector = GHALogCollector(repo="test/repo")
        since = now - timedelta(hours=1)
        data = collect_run_summary(collector, since)
        markdown = format_markdown_summary(data)

        # Successful runs should be included with their logs
        assert "Run 456" in markdown
        assert "retry succeeded" in markdown
        # Success runs should have success emoji
        assert "✅" in markdown

    def test_self_improvement_workflow_in_egg_workflows(self):
        """self-improvement.yml is included in EGG_WORKFLOWS for self-reflection."""
        assert "self-improvement.yml" in EGG_WORKFLOWS

    @patch.object(GHALogCollector, "collect")
    def test_collect_run_summary_logs_omitted_flag(self, mock_collect: MagicMock):
        """collect_run_summary sets logs_omitted flag when context limit reached."""
        now = datetime.now(UTC)
        # Create many runs with large logs to trigger context limit
        # Each run has 20k chars, truncated to ~3.4k per run after truncation message
        # After ~15 runs we exceed MAX_TOTAL_LOG_CHARS (50k), later runs get omitted
        mock_collect.return_value = [
            RunLog(
                run_id=str(i),
                source="gha",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="issue_comment",
                logs="x" * 20000,  # Large logs
                metadata={
                    "workflow": "on-mention.yml",
                    "head_branch": "main",
                    "html_url": f"https://github.com/test/repo/actions/runs/{i}",
                },
            )
            for i in range(20)  # 20 runs ensures we hit the limit
        ]

        collector = GHALogCollector(repo="test/repo")
        since = now - timedelta(hours=1)
        result = collect_run_summary(collector, since)

        # Early runs should have logs_omitted=False
        assert result["runs_to_analyze"][0]["logs_omitted"] is False

        # At least one later run should have logs_omitted=True
        omitted_runs = [r for r in result["runs_to_analyze"] if r["logs_omitted"]]
        assert len(omitted_runs) > 0

    @patch.object(GHALogCollector, "collect")
    def test_format_markdown_summary_shows_logs_omitted(self, mock_collect: MagicMock):
        """format_markdown_summary shows warning when logs are omitted."""
        now = datetime.now(UTC)
        # Create many failed runs with large logs to trigger context limit
        mock_collect.return_value = [
            RunLog(
                run_id=str(i),
                source="gha",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="issue_comment",
                logs="x" * 20000,
                metadata={
                    "workflow": "on-mention.yml",
                    "head_branch": "main",
                    "html_url": f"https://github.com/test/repo/actions/runs/{i}",
                },
            )
            for i in range(20)  # 20 runs ensures we hit the limit
        ]

        collector = GHALogCollector(repo="test/repo")
        since = now - timedelta(hours=1)
        data = collect_run_summary(collector, since)
        markdown = format_markdown_summary(data)

        # Should contain warning about omitted logs with fetch instructions
        assert "Logs omitted" in markdown
        assert "gh run view" in markdown


class TestPartitioning:
    """Tests for run partitioning functionality."""

    def test_partition_runs_empty(self):
        """partition_runs returns empty list for empty input."""
        result = partition_runs([])
        assert result == []

    def test_partition_runs_single_partition(self):
        """partition_runs returns single partition when runs fit."""
        runs = [{"run_id": str(i)} for i in range(3)]
        result = partition_runs(runs, max_runs=5)

        assert len(result) == 1
        assert len(result[0]) == 3

    def test_partition_runs_multiple_partitions(self):
        """partition_runs splits runs across multiple partitions."""
        runs = [{"run_id": str(i)} for i in range(12)]
        result = partition_runs(runs, max_runs=5)

        assert len(result) == 3
        assert len(result[0]) == 5
        assert len(result[1]) == 5
        assert len(result[2]) == 2

    def test_partition_runs_exact_fit(self):
        """partition_runs handles exact multiple of max_runs."""
        runs = [{"run_id": str(i)} for i in range(10)]
        result = partition_runs(runs, max_runs=5)

        assert len(result) == 2
        assert len(result[0]) == 5
        assert len(result[1]) == 5

    def test_partition_runs_default_max(self):
        """partition_runs uses default MAX_RUNS_PER_PARTITION."""
        runs = [{"run_id": str(i)} for i in range(MAX_RUNS_PER_PARTITION + 1)]
        result = partition_runs(runs)

        assert len(result) == 2
        assert len(result[0]) == MAX_RUNS_PER_PARTITION
        assert len(result[1]) == 1

    def test_format_partition_markdown_includes_partition_info(self):
        """format_partition_markdown includes partition metadata."""
        partition = [
            {
                "run_id": "123",
                "workflow": "test.yml",
                "status": "failure",
                "trigger": "push",
                "branch": "main",
                "started_at": "2024-01-01T00:00:00Z",
                "url": "https://example.com/run/123",
                "log_excerpt": "Error: test failed",
                "logs_omitted": False,
            }
        ]
        base_data = {
            "repository": "test/repo",
            "since": "2024-01-01T00:00:00Z",
            "collected_at": "2024-01-01T01:00:00Z",
            "statistics": {
                "total_runs": 10,
                "failed_runs": 3,
                "successful_runs": 7,
                "other_runs": 0,
            },
        }

        result = format_partition_markdown(partition, 0, 3, base_data)

        assert "Partition:** 1 of 3" in result
        assert "1 runs in this batch" in result
        assert "Run 123" in result
        assert "Total failed runs: 3" in result
        # Should include status emoji for failed run
        assert "❌" in result

    def test_format_partition_markdown_shows_logs_omitted(self):
        """format_partition_markdown shows warning for omitted logs."""
        partition = [
            {
                "run_id": "456",
                "workflow": "test.yml",
                "status": "failure",
                "trigger": "push",
                "branch": "main",
                "started_at": "2024-01-01T00:00:00Z",
                "url": "https://example.com/run/456",
                "log_excerpt": "[logs omitted]",
                "logs_omitted": True,
            }
        ]
        base_data = {
            "repository": "test/repo",
            "since": "2024-01-01T00:00:00Z",
            "collected_at": "2024-01-01T01:00:00Z",
            "statistics": {
                "total_runs": 5,
                "failed_runs": 1,
                "successful_runs": 4,
                "other_runs": 0,
            },
        }

        result = format_partition_markdown(partition, 0, 1, base_data)

        assert "Logs omitted" in result
        assert "gh run view 456 --log" in result

    @patch.object(GHALogCollector, "collect")
    def test_collect_run_summary_with_partitioning(self, mock_collect: MagicMock):
        """Collected runs can be partitioned correctly."""
        now = datetime.now(UTC)
        # Create 7 runs (mix of failed and successful)
        mock_collect.return_value = [
            RunLog(
                run_id=str(i),
                source="gha",
                started_at=now,
                completed_at=now,
                status="failure" if i % 2 == 0 else "success",
                trigger="push",
                logs=f"Log content for run {i}",
                metadata={
                    "workflow": "test.yml",
                    "head_branch": "main",
                    "html_url": f"https://example.com/runs/{i}",
                },
            )
            for i in range(7)
        ]

        collector = GHALogCollector(repo="test/repo")
        since = now - timedelta(hours=1)
        data = collect_run_summary(collector, since)

        # Partition all runs (failed come first, then successful)
        partitions = partition_runs(data["runs_to_analyze"], max_runs=3)

        assert len(partitions) == 3
        assert len(partitions[0]) == 3
        assert len(partitions[1]) == 3
        assert len(partitions[2]) == 1

    @patch.object(GHALogCollector, "collect")
    def test_runs_to_analyze_order_failed_first(self, mock_collect: MagicMock):
        """runs_to_analyze has failed runs before successful runs."""
        now = datetime.now(UTC)
        mock_collect.return_value = [
            RunLog(
                run_id="success1",
                source="gha",
                started_at=now,
                completed_at=now,
                status="success",
                trigger="push",
                logs="Success log",
                metadata={"workflow": "test.yml"},
            ),
            RunLog(
                run_id="failure1",
                source="gha",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="push",
                logs="Failure log",
                metadata={"workflow": "test.yml"},
            ),
            RunLog(
                run_id="success2",
                source="gha",
                started_at=now,
                completed_at=now,
                status="success",
                trigger="push",
                logs="Success log 2",
                metadata={"workflow": "test.yml"},
            ),
        ]

        collector = GHALogCollector(repo="test/repo")
        since = now - timedelta(hours=1)
        data = collect_run_summary(collector, since)

        # Failed runs should come first
        assert data["runs_to_analyze"][0]["run_id"] == "failure1"
        assert data["runs_to_analyze"][0]["status"] == "failure"
        # Then successful runs
        assert data["runs_to_analyze"][1]["run_id"] == "success1"
        assert data["runs_to_analyze"][1]["status"] == "success"
        assert data["runs_to_analyze"][2]["run_id"] == "success2"
        assert data["runs_to_analyze"][2]["status"] == "success"
