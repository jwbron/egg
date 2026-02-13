"""
Tests for egg-orchestrator CLI.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest
from cli import create_parser, main


@pytest.fixture
def parser():
    """Create CLI parser."""
    return create_parser()


class TestParserBasics:
    """Tests for CLI parser."""

    def test_version_flag(self, parser):
        """Test --version flag."""
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0

    def test_serve_command(self, parser):
        """Test serve command parsing."""
        args = parser.parse_args(["serve"])
        assert args.command == "serve"
        assert args.host == "0.0.0.0"
        assert args.port == 9849
        assert args.debug is False

    def test_serve_with_options(self, parser):
        """Test serve with options."""
        args = parser.parse_args(
            [
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8080",
                "--debug",
            ]
        )
        assert args.host == "127.0.0.1"
        assert args.port == 8080
        assert args.debug is True

    def test_health_command(self, parser):
        """Test health command parsing."""
        args = parser.parse_args(["health"])
        assert args.command == "health"

    def test_pipelines_list_command(self, parser):
        """Test pipelines list command."""
        args = parser.parse_args(["pipelines", "list"])
        assert args.command == "pipelines"
        assert args.pipelines_command == "list"

    def test_pipelines_create_command(self, parser):
        """Test pipelines create command."""
        args = parser.parse_args(
            [
                "pipelines",
                "create",
                "--issue",
                "123",
                "--repo",
                "owner/repo",
            ]
        )
        assert args.command == "pipelines"
        assert args.pipelines_command == "create"
        assert args.issue == 123
        assert args.repo == "owner/repo"

    def test_pipelines_status_command(self, parser):
        """Test pipelines status command."""
        args = parser.parse_args(["pipelines", "status", "issue-123"])
        assert args.command == "pipelines"
        assert args.pipelines_command == "status"
        assert args.pipeline_id == "issue-123"


class TestMainNoArgs:
    """Tests for main with no arguments."""

    def test_main_no_command(self, capsys):
        """Test main with no command shows help."""
        result = main([])
        assert result == 1

    def test_pipelines_no_subcommand(self, capsys):
        """Test pipelines with no subcommand shows help."""
        with pytest.raises(SystemExit):
            main(["pipelines"])


class MockHealthHandler(BaseHTTPRequestHandler):
    """Mock handler for health endpoint."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/api/v1/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "status": "healthy",
                        "version": "0.1.0",
                    }
                ).encode()
            )
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_health_server():
    """Start a mock health server."""
    server = HTTPServer(("localhost", 19849), MockHealthHandler)
    thread = Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    yield server
    server.shutdown()


class TestHealthCommand:
    """Tests for health command."""

    def test_health_success(self, mock_health_server, capsys):
        """Test successful health check."""
        result = main(["health", "--host", "localhost", "--port", "19849"])

        assert result == 0
        captured = capsys.readouterr()
        assert "healthy" in captured.out.lower()

    def test_health_failure(self, capsys):
        """Test health check failure."""
        result = main(["health", "--host", "localhost", "--port", "19999"])

        assert result == 1
        captured = capsys.readouterr()
        assert "failed" in captured.err.lower() or "connect" in captured.err.lower()


class TestPipelinesCommands:
    """Tests for pipelines commands."""

    @pytest.fixture
    def mock_state_store(self, tmp_path):
        """Create mock state store."""
        with patch("state_store.get_state_store") as mock:
            store = MagicMock()
            mock.return_value = store
            yield store

    def test_pipelines_list_empty(self, mock_state_store, capsys):
        """Test listing when no pipelines exist."""
        mock_state_store.list_pipelines.return_value = []

        result = main(["pipelines", "list", "--repo-path", "/tmp"])

        assert result == 0
        captured = capsys.readouterr()
        assert "no pipelines" in captured.out.lower()

    def test_pipelines_list_with_pipelines(self, mock_state_store, capsys):
        """Test listing pipelines."""
        from models import Pipeline, PipelineStatus

        mock_state_store.list_pipelines.return_value = ["issue-123", "issue-456"]

        mock_pipeline = Pipeline(
            id="issue-123",
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
            status=PipelineStatus.RUNNING,
        )
        mock_state_store.load_pipeline.return_value = mock_pipeline

        result = main(["pipelines", "list", "--repo-path", "/tmp"])

        assert result == 0
        captured = capsys.readouterr()
        assert "issue-123" in captured.out

    def test_pipelines_create(self, mock_state_store, capsys):
        """Test creating a pipeline."""
        from models import Pipeline, PipelineStatus

        mock_pipeline = Pipeline(
            id="issue-789",
            issue_number=789,
            repo="owner/repo",
            branch="egg/issue-789",
            status=PipelineStatus.PENDING,
        )
        mock_state_store.create_pipeline.return_value = mock_pipeline

        result = main(
            [
                "pipelines",
                "create",
                "--issue",
                "789",
                "--repo",
                "owner/repo",
                "--repo-path",
                "/tmp",
            ]
        )

        assert result == 0
        captured = capsys.readouterr()
        assert "issue-789" in captured.out

    def test_pipelines_status(self, mock_state_store, capsys):
        """Test getting pipeline status."""
        from models import Pipeline, PipelineStatus

        mock_pipeline = Pipeline(
            id="issue-123",
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
            status=PipelineStatus.RUNNING,
        )
        mock_state_store.load_pipeline.return_value = mock_pipeline

        result = main(
            [
                "pipelines",
                "status",
                "issue-123",
                "--repo-path",
                "/tmp",
            ]
        )

        assert result == 0
        captured = capsys.readouterr()
        assert "issue-123" in captured.out
        assert "running" in captured.out.lower()

    def test_pipelines_status_not_found(self, mock_state_store, capsys):
        """Test status of non-existent pipeline."""
        from state_store import PipelineNotFoundError

        mock_state_store.load_pipeline.side_effect = PipelineNotFoundError("Not found")

        result = main(
            [
                "pipelines",
                "status",
                "issue-999",
                "--repo-path",
                "/tmp",
            ]
        )

        assert result == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_pipelines_delete(self, mock_state_store, capsys):
        """Test deleting a pipeline."""
        result = main(
            [
                "pipelines",
                "delete",
                "issue-123",
                "--repo-path",
                "/tmp",
            ]
        )

        assert result == 0
        mock_state_store.delete_pipeline.assert_called_with("issue-123")

    def test_pipelines_list_json(self, mock_state_store, capsys):
        """Test listing pipelines as JSON."""
        from models import Pipeline, PipelineStatus

        mock_state_store.list_pipelines.return_value = ["issue-123"]

        mock_pipeline = Pipeline(
            id="issue-123",
            issue_number=123,
            repo="owner/repo",
            branch="egg/issue-123",
            status=PipelineStatus.RUNNING,
        )
        mock_state_store.load_pipeline.return_value = mock_pipeline

        result = main(["pipelines", "list", "--repo-path", "/tmp", "--json"])

        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["id"] == "issue-123"


class TestGatewayCommand:
    """Tests for gateway commands."""

    def test_gateway_status(self, capsys):
        """Test gateway status command."""
        with patch("gateway_client.get_gateway_client") as mock_get:
            from gateway_client import GatewayHealth

            mock_client = MagicMock()
            mock_client.check_health.return_value = GatewayHealth(
                healthy=True,
                status="healthy",
                version="0.1.0",
                uptime_seconds=100.0,
            )
            mock_get.return_value = mock_client

            result = main(["gateway", "status"])

            assert result == 0
            captured = capsys.readouterr()
            assert "healthy" in captured.out.lower()

    def test_gateway_status_unhealthy(self, capsys):
        """Test gateway status when unhealthy."""
        with patch("gateway_client.get_gateway_client") as mock_get:
            from gateway_client import GatewayHealth

            mock_client = MagicMock()
            mock_client.check_health.return_value = GatewayHealth(
                healthy=False,
                status="unreachable",
                error="Connection refused",
            )
            mock_get.return_value = mock_client

            result = main(["gateway", "status"])

            assert result == 1
            captured = capsys.readouterr()
            assert "unreachable" in captured.out.lower()

    def test_gateway_status_json(self, capsys):
        """Test gateway status as JSON."""
        with patch("gateway_client.get_gateway_client") as mock_get:
            from gateway_client import GatewayHealth

            mock_client = MagicMock()
            mock_client.check_health.return_value = GatewayHealth(
                healthy=True,
                status="healthy",
                version="0.1.0",
            )
            mock_get.return_value = mock_client

            result = main(["gateway", "status", "--json"])

            assert result == 0
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["healthy"] is True
            assert data["status"] == "healthy"
