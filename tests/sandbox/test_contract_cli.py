"""Tests for egg_lib.contract_cli module."""

import json

# Import the module under test
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

# Add shared and sandbox to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_config import GATEWAY_PORT

from egg_lib.contract_cli import (
    create_parser,
    format_decision_markdown,
    get_gateway_url,
    get_issue_number,
    get_repo_path,
    main,
    parse_phase_id,
    parse_task_id,
    validate_commit_sha,
    validate_decision_id,
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

    def test_add_decision_with_format_markdown(self):
        """Test parsing add-decision with --format markdown."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "add-decision",
                "--question",
                "Which approach?",
                "--format",
                "markdown",
            ]
        )
        assert args.format == "markdown"

    def test_add_decision_format_default_json(self):
        """Test that add-decision defaults to json format."""
        parser = create_parser()
        args = parser.parse_args(["add-decision", "--question", "Which approach?"])
        assert args.format == "json"


class TestEnvironmentHelpers:
    """Tests for environment variable helpers."""

    def test_get_gateway_url_default(self):
        """Test default gateway URL."""
        with patch.dict("os.environ", {}, clear=True):
            url = get_gateway_url()
            assert url == f"http://egg-gateway:{GATEWAY_PORT}"

    def test_get_gateway_url_from_env(self):
        """Test gateway URL from environment."""
        with patch.dict("os.environ", {"GATEWAY_URL": "http://localhost:8080"}):
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


def create_mock_gateway_handler(responses_dict: dict):
    """Create a MockGatewayHandler class with isolated responses.

    This factory function creates a new handler class with its own responses dict,
    ensuring thread safety when running tests in parallel (e.g., with pytest-xdist).

    Args:
        responses_dict: Dictionary mapping (method, path) tuples to response dicts

    Returns:
        A new BaseHTTPRequestHandler subclass with isolated responses
    """

    class MockGatewayHandler(BaseHTTPRequestHandler):
        """Mock HTTP handler for gateway responses."""

        # Instance-specific responses (not a class variable)
        responses = responses_dict

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

    return MockGatewayHandler


class TestWithMockGateway:
    """Tests that use a mock gateway server."""

    @pytest.fixture
    def mock_gateway_factory(self):
        """Factory fixture for creating mock gateway servers with isolated responses.

        Returns a function that creates a gateway with the specified responses.
        This ensures thread safety when running tests in parallel.
        """
        servers = []

        def create_gateway(responses: dict) -> str:
            handler_class = create_mock_gateway_handler(responses)
            server = HTTPServer(("127.0.0.1", 0), handler_class)
            port = server.server_address[1]
            thread = Thread(target=server.handle_request)
            thread.daemon = True
            thread.start()
            servers.append(server)
            return f"http://127.0.0.1:{port}"

        yield create_gateway

        # Cleanup all servers
        for server in servers:
            server.server_close()

    def test_show_command_success(self, mock_gateway_factory, capsys):
        """Test show command with successful response."""
        responses = {
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
        mock_gateway = mock_gateway_factory(responses)

        with patch.dict("os.environ", {"GATEWAY_URL": mock_gateway}):
            result = main(["--issue", "123", "show"])

        assert result == 0
        captured = capsys.readouterr()
        assert "123" in captured.out

    def test_add_commit_success(self, mock_gateway_factory, capsys):
        """Test add-commit command with successful response."""
        responses = {
            ("POST", "/api/v1/contract/mutate"): {
                "success": True,
                "message": "Mutation applied",
            }
        }
        mock_gateway = mock_gateway_factory(responses)

        with patch.dict(
            "os.environ",
            {
                "GATEWAY_URL": mock_gateway,
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
            result = main(["add-commit", "--task", "task-1", "--commit", "abc1234"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Issue number required" in captured.err

    def test_add_commit_invalid_commit_sha(self, capsys):
        """Test add-commit with invalid commit SHA."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "123"}):
            result = main(["add-commit", "--task", "task-1", "--commit", "not-a-sha"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Invalid commit SHA" in captured.err

    def test_add_commit_too_short_sha(self, capsys):
        """Test add-commit with commit SHA that's too short."""
        with patch.dict("os.environ", {"EGG_ISSUE_NUMBER": "123"}):
            result = main(["add-commit", "--task", "task-1", "--commit", "abc12"])
        assert result == 1
        captured = capsys.readouterr()
        assert "Invalid commit SHA" in captured.err


class TestCommitShaValidation:
    """Tests for commit SHA validation."""

    def test_valid_short_sha(self):
        """Test valid 7-character SHA."""
        assert validate_commit_sha("abc1234") == "abc1234"

    def test_valid_full_sha(self):
        """Test valid 40-character SHA."""
        sha = "a" * 40
        assert validate_commit_sha(sha) == sha

    def test_valid_mixed_case(self):
        """Test valid SHA with mixed case."""
        assert validate_commit_sha("AbC1234DeF") == "AbC1234DeF"

    def test_invalid_too_short(self):
        """Test SHA that's too short."""
        with pytest.raises(ValueError, match="Invalid commit SHA"):
            validate_commit_sha("abc12")

    def test_invalid_too_long(self):
        """Test SHA that's too long."""
        with pytest.raises(ValueError, match="Invalid commit SHA"):
            validate_commit_sha("a" * 41)

    def test_invalid_non_hex(self):
        """Test SHA with non-hexadecimal characters."""
        with pytest.raises(ValueError, match="Invalid commit SHA"):
            validate_commit_sha("ghijklm")

    def test_invalid_with_spaces(self):
        """Test SHA with spaces."""
        with pytest.raises(ValueError, match="Invalid commit SHA"):
            validate_commit_sha("abc 123")


class TestDecisionMarkdownFormat:
    """Tests for format_decision_markdown function."""

    def test_format_decision_markdown_basic(self):
        """Test basic markdown formatting with options."""
        options = [
            {"id": "opt-1", "label": "Option A"},
            {"id": "opt-2", "label": "Option B"},
        ]
        result = format_decision_markdown("decision-1", "Which approach?", options)

        assert "<!-- egg-hitl-decision id=decision-1 -->" in result
        assert "**Which approach?**" in result
        assert "- [ ] Option A" in result
        assert "- [ ] Option B" in result

    def test_format_decision_markdown_includes_other(self):
        """Test that Other option is included when present."""
        options = [
            {"id": "opt-1", "label": "Option A"},
            {"id": "opt-2", "label": "Other (explain in reply)"},
        ]
        result = format_decision_markdown("decision-2", "Pick one?", options)

        assert "- [ ] Other (explain in reply)" in result

    def test_format_decision_markdown_no_options(self):
        """Test markdown formatting with no options."""
        result = format_decision_markdown("decision-3", "Thoughts?", [])

        assert "<!-- egg-hitl-decision id=decision-3 -->" in result
        assert "**Thoughts?**" in result
        assert "- [ ]" not in result

    def test_format_decision_markdown_special_characters(self):
        """Test markdown formatting handles special characters in question."""
        options = [{"id": "opt-1", "label": "Yes"}]
        result = format_decision_markdown("decision-4", "Is this a `code` example?", options)

        assert "**Is this a `code` example?**" in result

    def test_format_decision_markdown_rejects_invalid_id(self):
        """Test that format_decision_markdown rejects invalid decision IDs."""
        options = [{"id": "opt-1", "label": "Yes"}]

        with pytest.raises(ValueError, match="Invalid decision_id"):
            format_decision_markdown("Decision-1", "Question?", options)

        with pytest.raises(ValueError, match="Invalid decision_id"):
            format_decision_markdown("decision_1", "Question?", options)

        with pytest.raises(ValueError, match="Invalid decision_id"):
            format_decision_markdown("decision 1", "Question?", options)


class TestValidateDecisionId:
    """Tests for validate_decision_id function."""

    def test_valid_decision_ids(self):
        """Test that valid decision IDs pass validation."""
        valid_ids = [
            "decision-1",
            "decision-123",
            "my-decision",
            "abc123",
            "a",
            "1",
            "a-b-c-1-2-3",
        ]
        for decision_id in valid_ids:
            validate_decision_id(decision_id)  # Should not raise

    def test_invalid_uppercase(self):
        """Test that uppercase letters are rejected."""
        with pytest.raises(ValueError, match="Invalid decision_id"):
            validate_decision_id("Decision-1")

    def test_invalid_underscore(self):
        """Test that underscores are rejected."""
        with pytest.raises(ValueError, match="Invalid decision_id"):
            validate_decision_id("decision_1")

    def test_invalid_spaces(self):
        """Test that spaces are rejected."""
        with pytest.raises(ValueError, match="Invalid decision_id"):
            validate_decision_id("decision 1")

    def test_invalid_special_chars(self):
        """Test that special characters are rejected."""
        invalid_ids = [
            "decision-->",
            "decision<1",
            "decision!",
            "decision@1",
        ]
        for decision_id in invalid_ids:
            with pytest.raises(ValueError, match="Invalid decision_id"):
                validate_decision_id(decision_id)

    def test_empty_string(self):
        """Test that empty string is rejected."""
        with pytest.raises(ValueError, match="Invalid decision_id"):
            validate_decision_id("")


class TestAddDecisionWithMockGateway:
    """Tests for add-decision command with mock gateway."""

    @pytest.fixture
    def mock_gateway_factory(self):
        """Factory fixture for creating mock gateway servers."""
        servers = []

        def create_gateway(responses: dict) -> str:
            handler_class = create_mock_gateway_handler(responses)
            server = HTTPServer(("127.0.0.1", 0), handler_class)
            port = server.server_address[1]
            # Need to handle two requests: GET contract + POST mutate
            thread1 = Thread(target=server.handle_request)
            thread1.daemon = True
            thread1.start()
            thread2 = Thread(target=server.handle_request)
            thread2.daemon = True
            thread2.start()
            servers.append(server)
            return f"http://127.0.0.1:{port}"

        yield create_gateway

        for server in servers:
            server.server_close()

    def test_add_decision_auto_appends_other_option(self, mock_gateway_factory, capsys):
        """Test that add-decision auto-appends Other option when options provided."""
        responses = {
            ("GET", "/api/v1/contract/123"): {
                "success": True,
                "data": {
                    "issue": {"number": 123, "title": "Test"},
                    "current_phase": "refine",
                    "phases": [],
                    "decisions": [],
                },
            },
            ("POST", "/api/v1/contract/mutate"): {
                "success": True,
                "message": "Mutation applied",
            },
        }
        mock_gateway = mock_gateway_factory(responses)

        with patch.dict(
            "os.environ",
            {"GATEWAY_URL": mock_gateway, "EGG_ISSUE_NUMBER": "123"},
        ):
            result = main(
                [
                    "add-decision",
                    "--question",
                    "Which approach?",
                    "--options",
                    "Option A",
                    "Option B",
                ]
            )

        assert result == 0

    def test_add_decision_markdown_format(self, mock_gateway_factory, capsys):
        """Test add-decision with --format markdown outputs correct format."""
        responses = {
            ("GET", "/api/v1/contract/123"): {
                "success": True,
                "data": {
                    "issue": {"number": 123, "title": "Test"},
                    "current_phase": "refine",
                    "phases": [],
                    "decisions": [],
                },
            },
            ("POST", "/api/v1/contract/mutate"): {
                "success": True,
                "message": "Mutation applied",
            },
        }
        mock_gateway = mock_gateway_factory(responses)

        with patch.dict(
            "os.environ",
            {"GATEWAY_URL": mock_gateway, "EGG_ISSUE_NUMBER": "123"},
        ):
            result = main(
                [
                    "add-decision",
                    "--question",
                    "Which approach?",
                    "--options",
                    "Option A",
                    "Option B",
                    "--format",
                    "markdown",
                ]
            )

        assert result == 0
        captured = capsys.readouterr()
        assert "<!-- egg-hitl-decision id=decision-1 -->" in captured.out
        assert "**Which approach?**" in captured.out
        assert "- [ ] Option A" in captured.out
        assert "- [ ] Option B" in captured.out
        assert "- [ ] Other (explain in reply)" in captured.out

    def test_add_decision_no_options_no_other(self, mock_gateway_factory, capsys):
        """Test that add-decision without options doesn't add Other."""
        responses = {
            ("GET", "/api/v1/contract/123"): {
                "success": True,
                "data": {
                    "issue": {"number": 123, "title": "Test"},
                    "current_phase": "refine",
                    "phases": [],
                    "decisions": [],
                },
            },
            ("POST", "/api/v1/contract/mutate"): {
                "success": True,
                "message": "Mutation applied",
            },
        }
        mock_gateway = mock_gateway_factory(responses)

        with patch.dict(
            "os.environ",
            {"GATEWAY_URL": mock_gateway, "EGG_ISSUE_NUMBER": "123"},
        ):
            result = main(
                [
                    "add-decision",
                    "--question",
                    "Open-ended question?",
                    "--format",
                    "markdown",
                ]
            )

        assert result == 0
        captured = capsys.readouterr()
        assert "<!-- egg-hitl-decision id=decision-1 -->" in captured.out
        assert "Other" not in captured.out
