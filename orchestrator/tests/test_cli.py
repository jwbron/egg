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


class TestRootGuard:
    """Tests for root user safety check."""

    def test_serve_refuses_to_run_as_root(self, capsys):
        """Orchestrator must refuse to start as root to prevent root-owned git refs."""
        with patch("os.getuid", return_value=0):
            with pytest.raises(SystemExit) as exc_info:
                main(["serve"])
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "must not run as root" in captured.err

    def test_serve_runs_as_non_root(self):
        """Orchestrator starts normally when not root."""
        with patch("os.getuid", return_value=1000):
            with patch.dict("sys.modules", {"api": MagicMock()}):
                with patch("waitress.serve"):
                    with patch("cli.logger"):
                        result = main(["serve"])
                        assert result == 0


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


class TestWaitressSizing:
    """Issue #1897 Phase 4 (plan revision 4, TASK-4-1): EGG_ORCH_WAITRESS_THREADS
    sizes the waitress thread pool (default 16), refuses to boot below 4
    (sys.exit(78) per sysexits EX_CONFIG), and channel_timeout is
    derived from EGG_MESSAGE_POLL_MAX_WAIT so long-polls do not hit
    the socket idle-timeout before the request's own timeout.
    """

    def test_default_threads_is_24(self, monkeypatch):
        """Default raised 16 → 24 in issue #1932 TASK-1-4 to absorb
        host-side ``/status/wait`` load on top of existing sandbox-side
        long polls. Each host wait (now driven by the
        ``egg-orch pipeline wait-status`` Bash CLI per #2211) costs two
        threads for up to the wait duration. Raising this further
        requires an explicit EGG_ORCH_WAITRESS_THREADS value. See
        docs/reference/agent-wait-patterns.md §7.
        """
        monkeypatch.delenv("EGG_ORCH_WAITRESS_THREADS", raising=False)
        monkeypatch.delenv("EGG_MESSAGE_POLL_MAX_WAIT", raising=False)
        with patch("os.getuid", return_value=1000):
            with patch.dict("sys.modules", {"api": MagicMock()}):
                with patch("waitress.serve") as mock_serve:
                    with patch("cli.logger"):
                        main(["serve"])
                        kwargs = mock_serve.call_args.kwargs
                        assert kwargs["threads"] == 24

    def test_thread_count_honors_env_var(self, monkeypatch):
        """Operator can raise above the default for high long-poll loads."""
        monkeypatch.setenv("EGG_ORCH_WAITRESS_THREADS", "128")
        monkeypatch.delenv("EGG_MESSAGE_POLL_MAX_WAIT", raising=False)
        with patch("os.getuid", return_value=1000):
            with patch.dict("sys.modules", {"api": MagicMock()}):
                with patch("waitress.serve") as mock_serve:
                    with patch("cli.logger"):
                        main(["serve"])
                        kwargs = mock_serve.call_args.kwargs
                        assert kwargs["threads"] == 128

    def test_refuse_to_boot_when_threads_lt_4(self, monkeypatch, caplog):
        """Plan TASK-4-1: values < 4 MUST refuse to boot with sys.exit(78)
        (sysexits.h EX_CONFIG) so a silently-saturated pool doesn't
        mask operator misconfiguration. The operator should see an
        ERROR log line naming the env var and the minimum.
        """
        import logging

        monkeypatch.setenv("EGG_ORCH_WAITRESS_THREADS", "2")
        monkeypatch.delenv("EGG_MESSAGE_POLL_MAX_WAIT", raising=False)
        caplog.set_level(logging.ERROR, logger="orchestrator.env_config")
        with patch("os.getuid", return_value=1000):
            with patch.dict("sys.modules", {"api": MagicMock()}):
                with patch("waitress.serve"):
                    with patch("cli.logger"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["serve"])
                        # EX_CONFIG per sysexits.h
                        assert exc_info.value.code == 78
        # ERROR line should mention both the env var and the minimum.
        combined = " ".join(r.message for r in caplog.records)
        assert "EGG_ORCH_WAITRESS_THREADS" in combined
        assert "4" in combined  # minimum value

    def test_refuse_to_boot_at_boundary_three(self, monkeypatch):
        """Boundary: 3 should still trip refuse-to-boot (strict <4)."""
        monkeypatch.setenv("EGG_ORCH_WAITRESS_THREADS", "3")
        monkeypatch.delenv("EGG_MESSAGE_POLL_MAX_WAIT", raising=False)
        with patch("os.getuid", return_value=1000):
            with patch.dict("sys.modules", {"api": MagicMock()}):
                with patch("waitress.serve"):
                    with patch("cli.logger"):
                        with pytest.raises(SystemExit) as exc_info:
                            main(["serve"])
                        assert exc_info.value.code == 78

    def test_accepts_minimum_four_threads(self, monkeypatch):
        """Boundary: 4 is the minimum acceptable value."""
        monkeypatch.setenv("EGG_ORCH_WAITRESS_THREADS", "4")
        monkeypatch.delenv("EGG_MESSAGE_POLL_MAX_WAIT", raising=False)
        with patch("os.getuid", return_value=1000):
            with patch.dict("sys.modules", {"api": MagicMock()}):
                with patch("waitress.serve") as mock_serve:
                    with patch("cli.logger"):
                        main(["serve"])
                        kwargs = mock_serve.call_args.kwargs
                        assert kwargs["threads"] == 4

    def test_malformed_threads_falls_back_to_default(self, monkeypatch):
        """Non-integer values fall back to default rather than crashing.

        Malformed values are a different failure mode than "too small" —
        they indicate an operator typo, not an intentional misconfiguration
        of the pool size. Falling back keeps the server booting with a
        safe default and logs a warning.
        """
        monkeypatch.setenv("EGG_ORCH_WAITRESS_THREADS", "not-a-number")
        monkeypatch.delenv("EGG_MESSAGE_POLL_MAX_WAIT", raising=False)
        with patch("os.getuid", return_value=1000):
            with patch.dict("sys.modules", {"api": MagicMock()}):
                with patch("waitress.serve") as mock_serve:
                    with patch("cli.logger"):
                        main(["serve"])
                        kwargs = mock_serve.call_args.kwargs
                        # Default raised 16 → 24 in issue #1932 TASK-1-4.
                        assert kwargs["threads"] == 24

    def test_channel_timeout_derived_from_poll_max_wait(self, monkeypatch):
        """channel_timeout must be >= 2 × poll_cap + 30 so waitress does
        not close the socket before the request finishes."""
        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "90")
        with patch("os.getuid", return_value=1000):
            with patch.dict("sys.modules", {"api": MagicMock()}):
                with patch("waitress.serve") as mock_serve:
                    with patch("cli.logger"):
                        main(["serve"])
                        kwargs = mock_serve.call_args.kwargs
                        # 2*90+30 = 210
                        assert kwargs["channel_timeout"] == 210

    def test_channel_timeout_min_120(self, monkeypatch):
        """With the default 60s cap, channel_timeout should be at least 120s."""
        monkeypatch.delenv("EGG_MESSAGE_POLL_MAX_WAIT", raising=False)
        with patch("os.getuid", return_value=1000):
            with patch.dict("sys.modules", {"api": MagicMock()}):
                with patch("waitress.serve") as mock_serve:
                    with patch("cli.logger"):
                        main(["serve"])
                        kwargs = mock_serve.call_args.kwargs
                        # 2*60+30 = 150
                        assert kwargs["channel_timeout"] >= 120
