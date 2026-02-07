"""Tests for self_improvement module.

Following agent-mode design principles, the self_improvement module focuses
on collecting run metadata. The actual log analysis and issue creation is
delegated to egg itself.
"""

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.self_improvement import LogCollector, RunLog
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


class TestAnalyzeModule:
    """Tests for analyze.py module."""

    def test_generate_summary_empty_runs(self):
        """Summary handles empty run list."""
        from egg_lib.self_improvement.analyze import generate_summary

        since = datetime.now(UTC)
        summary = generate_summary([], since)
        assert "No runs found" in summary

    def test_generate_summary_with_runs(self):
        """Summary includes run statistics."""
        from egg_lib.self_improvement.analyze import generate_summary

        now = datetime.now(UTC)
        runs = [
            RunLog(
                run_id="1",
                source="gha",
                started_at=now,
                completed_at=now,
                status="success",
                trigger="issue_comment",
                logs="",
            ),
            RunLog(
                run_id="2",
                source="gha",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="pull_request",
                logs="Error: test failed",
            ),
        ]

        since = now - timedelta(hours=1)
        summary = generate_summary(runs, since)

        assert "Total runs: 2" in summary
        assert "Successful: 1" in summary
        assert "Failed: 1" in summary

    def test_generate_json_output(self):
        """JSON output is valid and contains expected fields."""
        from egg_lib.self_improvement.analyze import generate_json

        now = datetime.now(UTC)
        runs = [
            RunLog(
                run_id="test-1",
                source="gha",
                started_at=now,
                completed_at=now,
                status="success",
                trigger="issue_comment",
                logs="test logs content",
            ),
        ]

        since = now - timedelta(hours=1)
        output = generate_json(runs, since)

        data = json.loads(output)
        assert "summary" in data
        assert data["summary"]["total_runs"] == 1
        assert data["summary"]["successful_runs"] == 1
        assert "runs" in data
        assert len(data["runs"]) == 1
        assert data["runs"][0]["run_id"] == "test-1"

    def test_select_collector_gha(self):
        """Selects GHALogCollector for gha source."""
        from egg_lib.self_improvement.analyze import select_collector

        collector = select_collector("gha", repo="test/repo")
        assert isinstance(collector, GHALogCollector)

    def test_select_collector_local(self):
        """Selects LocalLogCollector for local source."""
        from egg_lib.self_improvement.analyze import select_collector

        collector = select_collector("local")
        assert isinstance(collector, LocalLogCollector)

    @patch.dict("os.environ", {"GITHUB_ACTIONS": "true"})
    def test_select_collector_auto_in_gha(self):
        """Auto-selects GHA collector in GitHub Actions environment."""
        from egg_lib.self_improvement.analyze import select_collector

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="test/repo\n")
            collector = select_collector("auto")
            assert isinstance(collector, GHALogCollector)

    @patch.dict("os.environ", {}, clear=True)
    def test_select_collector_auto_outside_gha(self):
        """Auto-selects Local collector outside GitHub Actions."""
        from egg_lib.self_improvement.analyze import select_collector

        os.environ.pop("GITHUB_ACTIONS", None)

        collector = select_collector("auto")
        assert isinstance(collector, LocalLogCollector)

    def test_json_output_includes_metadata(self):
        """JSON output includes run metadata for egg to use."""
        from egg_lib.self_improvement.analyze import generate_json

        now = datetime.now(UTC)
        runs = [
            RunLog(
                run_id="12345",
                source="gha",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="issue_comment",
                logs="",
                metadata={
                    "html_url": "https://github.com/owner/repo/actions/runs/12345",
                    "workflow": "on-mention.yml",
                },
            ),
        ]

        since = now - timedelta(hours=1)
        output = generate_json(runs, since)

        data = json.loads(output)
        assert (
            data["runs"][0]["metadata"]["html_url"]
            == "https://github.com/owner/repo/actions/runs/12345"
        )
        assert data["runs"][0]["metadata"]["workflow"] == "on-mention.yml"
