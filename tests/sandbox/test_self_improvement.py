"""Tests for self_improvement module."""

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

from egg_lib.self_improvement import (
    Detection,
    DetectionEngine,
    IssueCreator,
    LogCollector,
    RunLog,
    Severity,
)
from egg_lib.self_improvement.collectors.gha import GHALogCollector
from egg_lib.self_improvement.collectors.local import LocalLogCollector
from egg_lib.self_improvement.config import (
    BOT_USERNAME,
    DEFAULT_SINCE_HOURS,
    EGG_WORKFLOWS,
    ISSUE_LABEL_PREFIX,
)
from egg_lib.self_improvement.output.issue_creator import generate_fingerprint


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
        assert ISSUE_LABEL_PREFIX == "self-improvement"
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
        # Logs should not be included in JSON output
        assert "logs" not in data["runs"][0]

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


class TestSeverity:
    """Tests for Severity enum."""

    def test_severity_values(self):
        """Severity has expected values."""
        assert Severity.LOW.value == "low"
        assert Severity.MEDIUM.value == "medium"
        assert Severity.HIGH.value == "high"


class TestDetection:
    """Tests for Detection dataclass."""

    def test_create_detection(self):
        """Detection can be created with all fields."""
        detection = Detection(
            rule_id="test_rule",
            category="test_category",
            title="Test Detection",
            description="A test detection",
            severity=Severity.HIGH,
            pattern="test.*pattern",
            evidence=["line 1", "line 2"],
            run_ids=["run-1", "run-2"],
            occurrence_count=5,
            recommendation="Fix this issue",
        )
        assert detection.rule_id == "test_rule"
        assert detection.severity == Severity.HIGH
        assert len(detection.evidence) == 2
        assert detection.occurrence_count == 5

    def test_detection_defaults(self):
        """Detection has reasonable defaults."""
        detection = Detection(
            rule_id="test",
            category="test",
            title="Test",
            description="Test",
            severity=Severity.LOW,
            pattern="test",
        )
        assert detection.evidence == []
        assert detection.run_ids == []
        assert detection.occurrence_count == 0
        assert detection.recommendation == ""


class TestDetectionEngine:
    """Tests for DetectionEngine."""

    def test_engine_loads_rules_from_directory(self):
        """Engine loads YAML rules from rules directory."""
        engine = DetectionEngine()
        # Should have loaded rules from the errors.yaml file
        assert len(engine.rules) > 0

    def test_engine_handles_missing_rules_dir(self):
        """Engine handles non-existent rules directory gracefully."""
        engine = DetectionEngine(rules_dir=Path("/nonexistent/path"))
        assert engine.rules == []

    def test_get_high_severity_rules(self):
        """Can filter rules by HIGH severity."""
        engine = DetectionEngine()
        high_rules = engine.get_high_severity_rules()
        for rule in high_rules:
            assert rule.severity == Severity.HIGH

    def test_analyze_detects_gateway_errors(self):
        """Engine detects gateway connection failures."""
        engine = DetectionEngine()

        now = datetime.now(UTC)
        runs = [
            RunLog(
                run_id="test-1",
                source="local",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="exec",
                logs="Starting container...\nError: gateway connection refused\nContainer exited",
            ),
        ]

        detections = engine.analyze(runs)

        # Should detect the gateway connection failure
        gateway_detections = [d for d in detections if "gateway" in d.rule_id.lower()]
        assert len(gateway_detections) >= 1

    def test_analyze_aggregates_across_runs(self):
        """Engine aggregates detections across multiple runs."""
        engine = DetectionEngine()

        now = datetime.now(UTC)
        runs = [
            RunLog(
                run_id="run-1",
                source="local",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="exec",
                logs="gateway connection refused",
            ),
            RunLog(
                run_id="run-2",
                source="local",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="exec",
                logs="gateway connection refused again",
            ),
        ]

        detections = engine.analyze(runs)

        # Find the gateway detection
        gateway_detections = [d for d in detections if "gateway" in d.rule_id.lower()]
        if gateway_detections:
            d = gateway_detections[0]
            # Should have both run IDs
            assert len(d.run_ids) == 2
            # Occurrence count should be aggregated
            assert d.occurrence_count >= 2

    def test_severity_escalation(self):
        """Engine escalates severity based on occurrence count."""
        engine = DetectionEngine()

        # Create a mock detection with low severity but many occurrences
        detection = Detection(
            rule_id="test",
            category="test",
            title="Test",
            description="Test",
            severity=Severity.LOW,
            pattern="test",
            occurrence_count=6,
        )

        engine._escalate_severity(detection)

        # Should be escalated to HIGH
        assert detection.severity == Severity.HIGH

    def test_severity_escalation_medium(self):
        """Engine escalates LOW to MEDIUM at 3 occurrences."""
        engine = DetectionEngine()

        detection = Detection(
            rule_id="test",
            category="test",
            title="Test",
            description="Test",
            severity=Severity.LOW,
            pattern="test",
            occurrence_count=3,
        )

        engine._escalate_severity(detection)

        assert detection.severity == Severity.MEDIUM


class TestFingerprint:
    """Tests for fingerprint generation."""

    def test_fingerprint_is_deterministic(self):
        """Same detection produces same fingerprint."""
        detection = Detection(
            rule_id="test_rule",
            category="test_category",
            title="Test",
            description="Test",
            severity=Severity.HIGH,
            pattern="test",
        )

        fp1 = generate_fingerprint(detection)
        fp2 = generate_fingerprint(detection)

        assert fp1 == fp2

    def test_fingerprint_is_12_chars(self):
        """Fingerprint is 12 hex characters."""
        detection = Detection(
            rule_id="test",
            category="test",
            title="Test",
            description="Test",
            severity=Severity.HIGH,
            pattern="test",
        )

        fp = generate_fingerprint(detection)

        assert len(fp) == 12
        assert all(c in "0123456789abcdef" for c in fp)

    def test_fingerprint_differs_by_rule(self):
        """Different rules produce different fingerprints."""
        d1 = Detection(
            rule_id="rule_a",
            category="test",
            title="Test",
            description="Test",
            severity=Severity.HIGH,
            pattern="test",
        )
        d2 = Detection(
            rule_id="rule_b",
            category="test",
            title="Test",
            description="Test",
            severity=Severity.HIGH,
            pattern="test",
        )

        assert generate_fingerprint(d1) != generate_fingerprint(d2)


class TestIssueCreator:
    """Tests for IssueCreator."""

    @patch("subprocess.run")
    def test_init_with_explicit_repo(self, mock_run: MagicMock):
        """Can initialize with explicit repo."""
        creator = IssueCreator(repo="owner/repo", dry_run=True)
        assert creator.repo == "owner/repo"
        assert creator.dry_run is True

    @patch("subprocess.run")
    def test_find_existing_issue_by_fingerprint(self, mock_run: MagicMock):
        """Finds existing issue by fingerprint in body."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {
                    "number": 42,
                    "body": "<!-- fingerprint:abc123def456 -->\n\nIssue content",
                    "url": "https://github.com/owner/repo/issues/42",
                },
            ]),
        )

        creator = IssueCreator(repo="owner/repo")
        existing = creator._find_existing_issue("abc123def456")

        assert existing is not None
        assert existing["number"] == 42

    @patch("subprocess.run")
    def test_find_existing_issue_returns_none_when_not_found(self, mock_run: MagicMock):
        """Returns None when no matching issue exists."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {
                    "number": 42,
                    "body": "<!-- fingerprint:different123 -->\n\nIssue content",
                    "url": "https://github.com/owner/repo/issues/42",
                },
            ]),
        )

        creator = IssueCreator(repo="owner/repo")
        existing = creator._find_existing_issue("abc123def456")

        assert existing is None

    @patch("subprocess.run")
    def test_create_issue_dry_run(self, mock_run: MagicMock):
        """Dry run mode doesn't actually create issues."""
        creator = IssueCreator(repo="owner/repo", dry_run=True)

        detection = Detection(
            rule_id="test_rule",
            category="test",
            title="Test Issue",
            description="Test description",
            severity=Severity.HIGH,
            pattern="test",
            occurrence_count=1,
        )

        result = creator._create_issue(detection, "abc123def456")

        assert result.success is True
        assert result.action == "skipped"
        # subprocess.run should not be called for gh issue create
        # (only for finding existing issues if called)

    @patch("subprocess.run")
    def test_format_issue_body_includes_fingerprint(self, mock_run: MagicMock):
        """Issue body includes fingerprint for deduplication."""
        creator = IssueCreator(repo="owner/repo")

        detection = Detection(
            rule_id="test_rule",
            category="errors",
            title="Test Issue",
            description="Test description",
            severity=Severity.HIGH,
            pattern="test.*pattern",
            evidence=["Error line 1", "Error line 2"],
            run_ids=["run-1", "run-2"],
            occurrence_count=5,
            recommendation="Fix it",
        )

        body = creator._format_issue_body(detection, "abc123def456")

        assert "<!-- fingerprint:abc123def456 -->" in body
        assert "Test description" in body
        assert "errors" in body
        assert "HIGH" in body
        assert "Error line 1" in body
        assert "run-1" in body
        assert "Fix it" in body

    @patch("subprocess.run")
    def test_get_labels_includes_severity(self, mock_run: MagicMock):
        """Labels include severity level."""
        creator = IssueCreator(repo="owner/repo")

        detection = Detection(
            rule_id="test",
            category="test",
            title="Test",
            description="Test",
            severity=Severity.HIGH,
            pattern="test",
        )

        labels = creator._get_labels(detection)

        assert "self-improvement" in labels
        assert "self-improvement:high" in labels

    @patch("subprocess.run")
    def test_create_issues_filters_by_min_severity(self, mock_run: MagicMock):
        """Only creates issues for detections meeting minimum severity."""
        # Mock the issue list (no existing issues)
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")

        creator = IssueCreator(repo="owner/repo", dry_run=True)

        detections = [
            Detection(
                rule_id="high_rule",
                category="test",
                title="High Severity",
                description="Test",
                severity=Severity.HIGH,
                pattern="test",
            ),
            Detection(
                rule_id="low_rule",
                category="test",
                title="Low Severity",
                description="Test",
                severity=Severity.LOW,
                pattern="test",
            ),
        ]

        results = creator.create_issues_for_detections(
            detections, min_severity=Severity.HIGH
        )

        # Should only process the HIGH severity detection
        assert len(results) == 1


class TestAnalyzeWithDetection:
    """Tests for analyze.py with detection integration."""

    def test_generate_json_with_detections(self):
        """JSON output includes detection data when provided."""
        from egg_lib.self_improvement.analyze import generate_json

        now = datetime.now(UTC)
        runs = [
            RunLog(
                run_id="test-1",
                source="gha",
                started_at=now,
                completed_at=now,
                status="failure",
                trigger="issue_comment",
                logs="gateway connection refused",
            ),
        ]

        detections = [
            Detection(
                rule_id="gateway_error",
                category="environment_errors",
                title="Gateway Connection Failure",
                description="Gateway connection failed",
                severity=Severity.HIGH,
                pattern="gateway.*refused",
                occurrence_count=1,
                run_ids=["test-1"],
            ),
        ]

        since = now - timedelta(hours=1)
        output = generate_json(runs, since, detections)

        data = json.loads(output)
        assert "detections" in data
        assert len(data["detections"]) == 1
        assert data["detections"][0]["rule_id"] == "gateway_error"
        assert data["detections"][0]["severity"] == "high"
        assert data["summary"]["detection_count"] == 1
        assert data["summary"]["high_severity_count"] == 1

    def test_generate_detection_summary(self):
        """Detection summary is formatted correctly."""
        from egg_lib.self_improvement.analyze import generate_detection_summary

        detections = [
            Detection(
                rule_id="high_rule",
                category="errors",
                title="High Severity Issue",
                description="Test",
                severity=Severity.HIGH,
                pattern="test",
                occurrence_count=3,
                run_ids=["run-1"],
                evidence=["Error: something went wrong"],
            ),
            Detection(
                rule_id="low_rule",
                category="perf",
                title="Low Severity Issue",
                description="Test",
                severity=Severity.LOW,
                pattern="test",
                occurrence_count=1,
                run_ids=["run-2"],
            ),
        ]

        summary = generate_detection_summary(detections)

        assert "Detected Issues" in summary
        assert "HIGH Severity" in summary
        assert "High Severity Issue" in summary
        assert "LOW Severity" in summary
        assert "Low Severity Issue" in summary

    def test_generate_detection_summary_empty(self):
        """Empty detection list produces simple message."""
        from egg_lib.self_improvement.analyze import generate_detection_summary

        summary = generate_detection_summary([])
        assert "No issues detected" in summary
