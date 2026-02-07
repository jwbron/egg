"""Tests for egg_lib.contract_cli module."""

import json

# Import the module under test
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

# Add sandbox to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.contract_cli import (
    create_parser,
    get_gateway_url,
    get_issue_number,
    get_repo_path,
    main,
    parse_phase_id,
    parse_task_id,
)


class TestArgumentParsing:
    """Tests for CLI argument parsing."""

    def test_parser_creation(self):
        """Test that parser is created correctly."""
        parser = create_parser()
        assert parser.prog == "egg-contract"

    def test_show_command(self):
        """Test parsing show command."""
        parser = create_parser()
        args = parser.parse_args(["--issue", "123", "show"])
        assert args.command == "show"
        assert args.issue == 123

    def test_show_with_json(self):
        """Test parsing show with --json flag."""
        parser = create_parser()
        args = parser.parse_args(["show", "--json"])
        assert args.json is True

    def test_show_with_audit(self):
        """Test parsing show with --audit flag."""
        parser = create_parser()
        args = parser.parse_args(["show", "--audit"])
        assert args.audit is True

    def test_add_commit_command(self):
        """Test parsing add-commit command."""
        parser = create_parser()
        args = parser.parse_args(["add-commit", "--task", "task-1", "--commit", "abc1234"])
        assert args.command == "add-commit"
        assert args.task == "task-1"
        assert args.commit == "abc1234"

    def test_update_notes_command(self):
        """Test parsing update-notes command."""
        parser = create_parser()
        args = parser.parse_args(["update-notes", "--task", "task-1", "--notes", "Some notes"])
        assert args.command == "update-notes"
        assert args.task == "task-1"
        assert args.notes == "Some notes"

    def test_mark_task_command(self):
        """Test parsing mark-task command."""
        parser = create_parser()
        args = parser.parse_args(["mark-task", "--task", "task-1", "--status", "complete"])
        assert args.command == "mark-task"
        assert args.task == "task-1"
        assert args.status == "complete"

    def test_mark_task_invalid_status(self):
        """Test that invalid status is rejected."""
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["mark-task", "--task", "task-1", "--status", "invalid"])

    def test_mark_phase_command(self):
        """Test parsing mark-phase command."""
        parser = create_parser()
        args = parser.parse_args(["mark-phase", "--phase", "phase-1", "--passed", "true"])
        assert args.command == "mark-phase"
        assert args.phase == "phase-1"
        assert args.passed is True

    def test_mark_phase_false(self):
        """Test parsing mark-phase with false."""
        parser = create_parser()
        args = parser.parse_args(["mark-phase", "--phase", "phase-1", "--passed", "false"])
        assert args.passed is False

    def test_add_decision_command(self):
        """Test parsing add-decision command."""
        parser = create_parser()
        args = parser.parse_args(["add-decision", "--question", "Should we proceed?"])
        assert args.command == "add-decision"
        assert args.question == "Should we proceed?"

    def test_add_decision_with_options(self):
        """Test parsing add-decision with options."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "add-decision",
                "--question",
                "Which approach?",
                "--options",
                "Option A",
                "Option B",
                "Option C",
            ]
        )
        assert args.options == ["Option A", "Option B", "Option C"]


class TestEnvironmentHelpers:
    """Tests for environment variable helpers."""

    def test_get_gateway_url_default(self):
        """Test default gateway URL."""
        with patch.dict("os.environ", {}, clear=True):
            url = get_gateway_url()
            assert url == "http://egg-gateway:9847"

    def test_get_gateway_url_from_env(self):
        """Test gateway URL from environment."""
        with patch.dict("os.environ", {"EGG_GATEWAY_URL": "http://localhost:8080"}):
            url = get_gateway_url()
            assert url == "http://localhost:8080"

    def test_get_issue_number_none(self):
        """Test issue number when not set."""
        with patch.dict("os.environ", {}, clear=True):
            issue = get_issue_number()
            assert issue is None

    def test_get_issue_number_from_env(self):
        """Test issue number from environment."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "123"}):
            issue = get_issue_number()
            assert issue == 123

    def test_get_issue_number_invalid(self):
        """Test issue number with invalid value."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "not-a-number"}):
            issue = get_issue_number()
            assert issue is None

    def test_get_repo_path_default(self):
        """Test default repo path."""
        with patch.dict("os.environ", {}, clear=True):
            path = get_repo_path()
            # Should return current working directory
            assert path == str(Path.cwd())

    def test_get_repo_path_from_env(self):
        """Test repo path from environment."""
        with patch.dict("os.environ", {"EGG_REPO_PATH": "/home/test/repo"}):
            path = get_repo_path()
            assert path == "/home/test/repo"


class TestTaskIdParsing:
    """Tests for task ID parsing."""

    def test_simple_task_id(self):
        """Test parsing simple task ID (task-N)."""
        phase_idx, task_idx = parse_task_id("task-1")
        assert phase_idx == 0
        assert task_idx == 0

    def test_full_task_id(self):
        """Test parsing full task ID (task-P-T)."""
        phase_idx, task_idx = parse_task_id("task-2-3")
        assert phase_idx == 1
        assert task_idx == 2

    def test_task_id_case_insensitive(self):
        """Test that task ID parsing is case insensitive."""
        phase_idx, task_idx = parse_task_id("TASK-1-2")
        assert phase_idx == 0
        assert task_idx == 1

    def test_task_id_invalid_format_too_many_parts(self):
        """Test that task ID with too many parts raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            parse_task_id("task-1-2-3")
        assert "Invalid task ID format" in str(exc_info.value)

    def test_task_id_non_numeric(self):
        """Test that non-numeric task ID raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            parse_task_id("task-abc")
        assert "Invalid task ID" in str(exc_info.value)

    def test_task_id_zero_task_number(self):
        """Test that task number 0 raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            parse_task_id("task-0")
        assert "must be >= 1" in str(exc_info.value)

    def test_task_id_zero_phase_number(self):
        """Test that phase number 0 raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            parse_task_id("task-0-1")
        assert "must be >= 1" in str(exc_info.value)

    def test_task_id_negative_numbers(self):
        """Test that negative numbers in task ID raise ValueError."""
        # Negative numbers in the ID format won't parse correctly
        # because the split creates multiple parts
        with pytest.raises(ValueError):
            parse_task_id("task--1")


class TestPhaseIdParsing:
    """Tests for phase ID parsing."""

    def test_valid_phase_id(self):
        """Test parsing valid phase ID."""
        phase_idx = parse_phase_id("phase-1")
        assert phase_idx == 0

    def test_phase_id_case_insensitive(self):
        """Test that phase ID parsing is case insensitive."""
        phase_idx = parse_phase_id("PHASE-2")
        assert phase_idx == 1

    def test_phase_id_non_numeric(self):
        """Test that non-numeric phase ID raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            parse_phase_id("phase-abc")
        assert "Invalid phase ID" in str(exc_info.value)

    def test_phase_id_zero(self):
        """Test that phase number 0 raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            parse_phase_id("phase-0")
        assert "must be >= 1" in str(exc_info.value)


class TestMainNoCommand:
    """Tests for main function without command."""

    def test_no_command_prints_help(self, capsys):
        """Test that running without command prints help."""
        result = main([])
        assert result == 1
        captured = capsys.readouterr()
        assert "usage" in captured.out.lower() or "egg-contract" in captured.out


class MockGatewayHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler for gateway responses."""

    responses = {}

    def log_message(self, format, *args):
        """Suppress logging."""
        pass

    def do_GET(self):
        """Handle GET requests."""
        response = self.responses.get(("GET", self.path), {"success": True})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def do_POST(self):
        """Handle POST requests."""
        content_length = int(self.headers.get("Content-Length", 0))
        # Read and discard the body to complete the request
        if content_length:
            self.rfile.read(content_length)

        response = self.responses.get(("POST", self.path), {"success": True})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


class TestWithMockGateway:
    """Tests that use a mock gateway server."""

    @pytest.fixture
    def mock_gateway(self):
        """Create a mock gateway server."""
        server = HTTPServer(("127.0.0.1", 0), MockGatewayHandler)
        port = server.server_address[1]
        thread = Thread(target=server.handle_request)
        thread.daemon = True
        thread.start()
        yield f"http://127.0.0.1:{port}"
        server.server_close()

    def test_show_command_success(self, mock_gateway, capsys):
        """Test show command with successful response."""
        MockGatewayHandler.responses = {
            ("GET", "/api/v1/contract/123"): {
                "success": True,
                "data": {
                    "issue": {"number": 123, "title": "Test"},
                    "current_phase": "implement",
                    "phases": [],
                    "decisions": [],
                    "circuit_breaker": {"status": "closed"},
                },
            }
        }

        with patch.dict("os.environ", {"EGG_GATEWAY_URL": mock_gateway}):
            result = main(["--issue", "123", "show"])

        assert result == 0
        captured = capsys.readouterr()
        assert "123" in captured.out

    def test_add_commit_success(self, mock_gateway, capsys):
        """Test add-commit command with successful response."""
        MockGatewayHandler.responses = {
            ("POST", "/api/v1/contract/mutate"): {
                "success": True,
                "message": "Mutation applied",
            }
        }

        with patch.dict(
            "os.environ",
            {
                "EGG_GATEWAY_URL": mock_gateway,
                "EGG_ISSUE_NUMBER": "123",
            },
        ):
            result = main(["add-commit", "--task", "task-1", "--commit", "abc1234def"])

        assert result == 0
        captured = capsys.readouterr()
        assert "abc1234" in captured.out or "task-1" in captured.out


class TestErrorPaths:
    """Tests for error handling paths."""

    def test_add_commit_invalid_task_id(self, capsys):
        """Test add-commit with invalid task ID."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "123"}):
            result = main(["add-commit", "--task", "task-abc", "--commit", "abc1234"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Invalid task ID" in captured.err

    def test_add_commit_zero_task_number(self, capsys):
        """Test add-commit with zero task number."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "123"}):
            result = main(["add-commit", "--task", "task-0", "--commit", "abc1234"])
        assert result == 1
        captured = capsys.readouterr()
        assert "must be >= 1" in captured.err

    def test_mark_task_invalid_task_id(self, capsys):
        """Test mark-task with invalid task ID."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "123"}):
            result = main(["mark-task", "--task", "task-not-valid", "--status", "complete"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Invalid task ID" in captured.err

    def test_mark_phase_invalid_phase_id(self, capsys):
        """Test mark-phase with invalid phase ID."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "123"}):
            result = main(["mark-phase", "--phase", "phase-abc", "--passed", "true"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Invalid phase ID" in captured.err

    def test_mark_phase_zero_phase(self, capsys):
        """Test mark-phase with zero phase number."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "123"}):
            result = main(["mark-phase", "--phase", "phase-0", "--passed", "true"])
        assert result == 1
        captured = capsys.readouterr()
        assert "must be >= 1" in captured.err

    def test_update_notes_invalid_task_id(self, capsys):
        """Test update-notes with invalid task ID."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "123"}):
            result = main(["update-notes", "--task", "task-xyz", "--notes", "Some notes"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Invalid task ID" in captured.err

    def test_show_no_issue_number(self, capsys):
        """Test show command without issue number."""
        with patch.dict("os.environ", {}, clear=True):
            result = main(["show"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Issue number required" in captured.err

    def test_add_commit_no_issue_number(self, capsys):
        """Test add-commit without issue number."""
        with patch.dict("os.environ", {}, clear=True):
            result = main(["add-commit", "--task", "task-1", "--commit", "abc123"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Issue number required" in captured.err
