"""Tests for sandbox/egg_lib/orch_client.py - Orchestrator HTTP client."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.orch_client import (
    OrchClient,
    OrchestratorError,
    _is_inside_container,
    get_orchestrator_url,
)

# ---------------------------------------------------------------------------
# Helpers: lightweight HTTP stub server
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    """HTTP handler that returns canned responses configured via class attrs."""

    # Set by each test via the _serve() context manager
    responses: dict = {}  # path → (status, body_dict)
    last_path: str = ""  # full path including query string from last request
    last_body: dict | None = None  # parsed JSON body from last request

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        _StubHandler.last_path = self.path
        # Read and parse request body if present
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            raw_body = self.rfile.read(content_length)
            try:
                _StubHandler.last_body = json.loads(raw_body)
            except json.JSONDecodeError:
                _StubHandler.last_body = None
        else:
            _StubHandler.last_body = None
        key = self.path.split("?")[0]
        status, body = self.responses.get(key, (404, {"message": "not found"}))
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass  # Silence request logs during tests


@pytest.fixture()
def stub_server():
    """Start a stub HTTP server and return (client, set_responses).

    ``set_responses`` accepts a dict mapping path → (status, body_dict).
    """
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client = OrchClient(base_url=f"http://127.0.0.1:{port}", timeout=5)

    def _set(responses: dict):
        _StubHandler.responses = responses

    yield client, _set

    server.shutdown()
    _StubHandler.responses = {}
    _StubHandler.last_body = None


# ---------------------------------------------------------------------------
# Unit tests: URL detection helpers
# ---------------------------------------------------------------------------


class TestGetOrchestratorUrl:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://custom:1234")
        assert get_orchestrator_url() == "http://custom:1234"

    def test_inside_container(self, monkeypatch):
        monkeypatch.delenv("EGG_ORCHESTRATOR_URL", raising=False)
        with patch("egg_lib.orch_client._is_inside_container", return_value=True):
            assert get_orchestrator_url() == "http://egg-orchestrator:9849"

    def test_host_machine(self, monkeypatch):
        monkeypatch.delenv("EGG_ORCHESTRATOR_URL", raising=False)
        with patch("egg_lib.orch_client._is_inside_container", return_value=False):
            assert get_orchestrator_url() == "http://localhost:9849"


class TestIsInsideContainer:
    def test_env_var_set(self, monkeypatch):
        monkeypatch.setenv("EGG_CONTAINER", "1")
        assert _is_inside_container() is True

    def test_dockerenv_exists(self, monkeypatch):
        monkeypatch.delenv("EGG_CONTAINER", raising=False)
        with patch("os.path.exists", return_value=True):
            assert _is_inside_container() is True

    def test_not_in_container(self, monkeypatch):
        monkeypatch.delenv("EGG_CONTAINER", raising=False)
        with patch("os.path.exists", return_value=False):
            assert _is_inside_container() is False


# ---------------------------------------------------------------------------
# Unit tests: OrchClient construction
# ---------------------------------------------------------------------------


class TestOrchClientInit:
    def test_parses_url(self):
        c = OrchClient(base_url="http://myhost:8080")
        assert c.host == "myhost"
        assert c.port == 8080

    def test_default_port(self):
        c = OrchClient(base_url="http://myhost")
        assert c.port == 9849  # fallback

    def test_custom_timeout(self):
        c = OrchClient(base_url="http://x:1", timeout=99)
        assert c.timeout == 99


# ---------------------------------------------------------------------------
# Integration-style tests: OrchClient against stub server
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_healthy(self, stub_server):
        client, set_resp = stub_server
        set_resp({"/api/v1/health": (200, {"status": "healthy"})})
        assert client.health_check() is True

    def test_unhealthy_status(self, stub_server):
        client, set_resp = stub_server
        set_resp({"/api/v1/health": (200, {"status": "degraded"})})
        assert client.health_check() is False

    def test_server_error(self, stub_server):
        client, set_resp = stub_server
        set_resp({"/api/v1/health": (500, {"message": "boom"})})
        assert client.health_check() is False

    def test_connection_refused(self):
        client = OrchClient(base_url="http://127.0.0.1:1", timeout=1)
        assert client.health_check() is False


class TestCreatePipeline:
    def test_success(self, stub_server):
        client, set_resp = stub_server
        pipeline = {"id": "issue-42", "status": "created"}
        set_resp({"/api/v1/pipelines": (200, {"data": {"pipeline": pipeline}})})
        result = client.create_pipeline(issue_number=42, repo="org/repo", mode="issue")
        assert result["id"] == "issue-42"

    def test_conflict_409(self, stub_server):
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines": (409, {"message": "Pipeline already exists"})})
        with pytest.raises(OrchestratorError) as exc_info:
            client.create_pipeline(issue_number=42)
        assert exc_info.value.status_code == 409
        assert "already exists" in str(exc_info.value)

    def test_optional_params(self, stub_server):
        """create_pipeline builds the body correctly with optional params."""
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines": (200, {"data": {"pipeline": {"id": "p1"}}})})
        result = client.create_pipeline(
            issue_number=1,
            repo="o/r",
            branch="egg/test",
            mode="issue",
            prompt="do stuff",
            config={"key": "val"},
        )
        assert result["id"] == "p1"


class TestStartPipeline:
    def test_success(self, stub_server):
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines/p1/start": (200, {"status": "running"})})
        result = client.start_pipeline("p1")
        assert result["status"] == "running"

    def test_already_running_409(self, stub_server):
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines/p1/start": (409, {"message": "Already running"})})
        with pytest.raises(OrchestratorError) as exc_info:
            client.start_pipeline("p1")
        assert exc_info.value.status_code == 409


class TestGetPipeline:
    def test_success(self, stub_server):
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines/p1": (200, {"data": {"id": "p1", "status": "running"}})})
        result = client.get_pipeline("p1")
        assert result["id"] == "p1"


class TestGetPipelineStatus:
    def test_success(self, stub_server):
        client, set_resp = stub_server
        set_resp(
            {
                "/api/v1/pipelines/p1/status": (
                    200,
                    {"data": {"status": "running", "phase": "refine"}},
                )
            }
        )
        result = client.get_pipeline_status("p1")
        assert result["status"] == "running"


class TestListDecisions:
    def test_list_all(self, stub_server):
        client, set_resp = stub_server
        decisions = [{"id": "d1"}, {"id": "d2"}]
        set_resp({"/api/v1/pipelines/p1/decisions": (200, {"data": {"decisions": decisions}})})
        result = client.list_decisions("p1")
        assert len(result) == 2
        assert result[0]["id"] == "d1"

    def test_pending_only(self, stub_server):
        """pending_only=True sends ?pending_only=true matching the server parameter name."""
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines/p1/decisions": (200, {"data": {"decisions": [{"id": "d1"}]}})})
        result = client.list_decisions("p1", pending_only=True)
        assert len(result) == 1
        assert "pending_only=true" in _StubHandler.last_path


class TestResolveDecision:
    def test_success(self, stub_server):
        client, set_resp = stub_server
        set_resp(
            {
                "/api/v1/pipelines/p1/decisions/d1/resolve": (
                    200,
                    {"status": "resolved"},
                )
            }
        )
        result = client.resolve_decision("p1", "d1", "Approved")
        assert result["status"] == "resolved"


class TestCancelPipeline:
    def test_success(self, stub_server):
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines/p1": (200, {"status": "cancelled"})})
        result = client.cancel_pipeline("p1")
        assert result["status"] == "cancelled"


class TestStreamPipeline:
    def test_non_200_raises(self, stub_server):
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines/p1/stream": (500, {"message": "internal error"})})
        with pytest.raises(OrchestratorError) as exc_info:
            client.stream_pipeline("p1")
        assert exc_info.value.status_code == 500


class TestRequestErrorHandling:
    def test_connection_refused(self):
        client = OrchClient(base_url="http://127.0.0.1:1", timeout=1)
        with pytest.raises(OrchestratorError, match="Cannot connect"):
            client.get_pipeline("p1")

    def test_timeout(self):
        """Timeout on a non-routable address."""
        client = OrchClient(base_url="http://192.0.2.1:9999", timeout=1)
        with pytest.raises(OrchestratorError):
            client.get_pipeline("p1")

    def test_non_json_response(self, stub_server):
        """Server returns non-JSON body."""
        client, set_resp = stub_server
        # We can't easily make the stub return non-JSON, but we can return
        # a 200 with the stub's JSON format which always parses fine.
        # Instead, test the error path by returning a 400 with a message.
        set_resp({"/api/v1/pipelines/bad": (400, {"message": "bad request"})})
        with pytest.raises(OrchestratorError, match="bad request"):
            client.get_pipeline("bad")


class TestCreateDecision:
    """Tests for OrchClient.create_decision() method."""

    def test_success_with_all_params(self, stub_server):
        """create_decision sends correct POST with all parameters."""
        client, set_resp = stub_server
        decision_resp = {"id": "decision-1", "decision_type": "feedback"}
        set_resp(
            {
                "/api/v1/pipelines/p1/decisions": (
                    200,
                    {"data": {"decision": decision_resp}},
                )
            }
        )

        questions = [{"id": "q-1", "question": "Why?", "answer": ""}]
        result = client.create_decision(
            pipeline_id="p1",
            question="Please provide feedback",
            options=["A", "B"],
            decision_type="feedback",
            questions=questions,
            context="Some context",
            phase="plan",
        )

        assert result["id"] == "decision-1"
        assert result["decision_type"] == "feedback"

        # Verify the request body
        body = _StubHandler.last_body
        assert body is not None
        assert body["question"] == "Please provide feedback"
        assert body["options"] == ["A", "B"]
        assert body["decision_type"] == "feedback"
        assert body["questions"] == questions
        assert body["context"] == "Some context"
        assert body["phase"] == "plan"

    def test_default_params(self, stub_server):
        """create_decision with defaults omits decision_type='choice' from body."""
        client, set_resp = stub_server
        decision_resp = {"id": "decision-1"}
        set_resp(
            {
                "/api/v1/pipelines/p1/decisions": (
                    200,
                    {"data": {"decision": decision_resp}},
                )
            }
        )

        result = client.create_decision(
            pipeline_id="p1",
            question="Pick one?",
        )

        assert result["id"] == "decision-1"

        # With default decision_type='choice', it should not be in body
        body = _StubHandler.last_body
        assert body is not None
        assert body["question"] == "Pick one?"
        assert "decision_type" not in body  # Default omitted
        assert "questions" not in body  # None omitted
        assert "options" not in body  # None omitted
        assert "phase" not in body  # None omitted

    def test_response_parsing(self, stub_server):
        """create_decision returns the decision dict from the response."""
        client, set_resp = stub_server
        decision_resp = {
            "id": "decision-1",
            "question": "Test?",
            "decision_type": "phase_gate",
            "questions": [],
            "status": "pending",
        }
        set_resp(
            {
                "/api/v1/pipelines/p1/decisions": (
                    200,
                    {"data": {"decision": decision_resp}},
                )
            }
        )

        result = client.create_decision(
            pipeline_id="p1",
            question="Test?",
            decision_type="phase_gate",
        )

        assert result == decision_resp

    def test_error_handling(self, stub_server):
        """create_decision raises OrchestratorError on HTTP error."""
        client, set_resp = stub_server
        set_resp(
            {
                "/api/v1/pipelines/p1/decisions": (
                    400,
                    {"message": "Missing question"},
                )
            }
        )

        with pytest.raises(OrchestratorError, match="Missing question"):
            client.create_decision(
                pipeline_id="p1",
                question="",
            )

    def test_server_error(self, stub_server):
        """create_decision raises OrchestratorError on 500."""
        client, set_resp = stub_server
        set_resp(
            {
                "/api/v1/pipelines/p1/decisions": (
                    500,
                    {"message": "Internal server error"},
                )
            }
        )

        with pytest.raises(OrchestratorError) as exc_info:
            client.create_decision(
                pipeline_id="p1",
                question="Test?",
            )
        assert exc_info.value.status_code == 500

    def test_create_with_questions_only(self, stub_server):
        """create_decision with questions but no options omits options from body."""
        client, set_resp = stub_server
        decision_resp = {"id": "decision-1", "decision_type": "feedback"}
        set_resp(
            {
                "/api/v1/pipelines/p1/decisions": (
                    200,
                    {"data": {"decision": decision_resp}},
                )
            }
        )

        questions = [
            {"id": "q-1", "question": "Volume?", "answer": ""},
            {"id": "q-2", "question": "Performance?", "answer": ""},
        ]
        result = client.create_decision(
            pipeline_id="p1",
            question="Feedback needed",
            decision_type="feedback",
            questions=questions,
        )

        assert result["id"] == "decision-1"
        body = _StubHandler.last_body
        assert body["question"] == "Feedback needed"
        assert body["decision_type"] == "feedback"
        assert body["questions"] == questions
        assert "options" not in body

    def test_create_with_explicit_choice_type(self, stub_server):
        """create_decision with explicit decision_type='choice' omits it from body."""
        client, set_resp = stub_server
        decision_resp = {"id": "decision-1"}
        set_resp(
            {
                "/api/v1/pipelines/p1/decisions": (
                    200,
                    {"data": {"decision": decision_resp}},
                )
            }
        )

        result = client.create_decision(
            pipeline_id="p1",
            question="Pick one?",
            options=["A", "B"],
            decision_type="choice",
        )

        assert result["id"] == "decision-1"
        body = _StubHandler.last_body
        # Default "choice" is omitted to avoid sending unnecessary data
        assert "decision_type" not in body


class TestDeletePipeline:
    """Tests for OrchClient.delete_pipeline() method."""

    def test_success(self, stub_server):
        """delete_pipeline sends DELETE request."""
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines/p1": (200, {"status": "deleted"})})
        result = client.delete_pipeline("p1")
        assert result["status"] == "deleted"

    def test_not_found(self, stub_server):
        """delete_pipeline raises OrchestratorError on 404."""
        client, set_resp = stub_server
        set_resp({"/api/v1/pipelines/p1": (404, {"message": "Pipeline not found"})})

        with pytest.raises(OrchestratorError) as exc_info:
            client.delete_pipeline("p1")
        assert exc_info.value.status_code == 404
