"""Tests for egg_lib.contract_cli module."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch

import pytest

# Import the module under test
import sys
from pathlib import Path

# Add sandbox to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.contract_cli import (
    create_parser,
    main,
    get_gateway_url,
    get_issue_number,
    get_repo_path,
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
        args = parser.parse_args([
            "add-decision",
            "--question", "Which approach?",
            "--options", "Option A", "Option B", "Option C",
        ])
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
        body = self.rfile.read(content_length) if content_length else b""

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

        with patch.dict("os.environ", {
            "EGG_GATEWAY_URL": mock_gateway,
            "EGG_ISSUE_NUMBER": "123",
        }):
            result = main(["add-commit", "--task", "task-1", "--commit", "abc1234def"])

        assert result == 0
        captured = capsys.readouterr()
        assert "abc1234" in captured.out or "task-1" in captured.out
