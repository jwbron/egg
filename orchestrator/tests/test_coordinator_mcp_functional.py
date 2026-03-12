"""
Functional tests for MCP server and coordinator tool handler.

Tests the MCPServer Flask app, RateLimiter, CoordinatorToolHandler,
and tool definitions with mocked orchestrator backend.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mcp_server import MCPServer, RateLimiter
from mcp_tools import COORDINATOR_TOOLS, CoordinatorToolHandler

# ── RateLimiter tests ────────────────────────────────────────────────


class TestRateLimiter:
    """Tests for the token bucket rate limiter."""

    def test_allows_within_limit(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            assert limiter.allow() is True

    def test_blocks_over_limit(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            assert limiter.allow() is True
        assert limiter.allow() is False

    def test_allows_after_window_expires(self):
        limiter = RateLimiter(max_requests=1, window_seconds=1)
        assert limiter.allow() is True
        assert limiter.allow() is False
        # Wait for window to expire
        time.sleep(1.1)
        assert limiter.allow() is True

    def test_zero_max_requests_blocks_all(self):
        limiter = RateLimiter(max_requests=0, window_seconds=60)
        assert limiter.allow() is False

    def test_thread_safe(self):
        """Rate limiter should be thread-safe."""
        import threading

        limiter = RateLimiter(max_requests=100, window_seconds=60)
        results = []

        def try_request():
            results.append(limiter.allow())

        threads = [threading.Thread(target=try_request) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 100  # All should succeed


# ── MCPServer app tests ─────────────────────────────────────────────


class TestMCPServerApp:
    """Tests for MCPServer Flask endpoints."""

    @pytest.fixture
    def mcp_client(self):
        """Create an MCP server test client."""
        server = MCPServer(
            orchestrator_url="http://localhost:9849",
            port=9850,
            rate_limit=100,
        )
        app = server.create_app()
        app.config["TESTING"] = True
        return app.test_client()

    def test_health_endpoint(self, mcp_client):
        response = mcp_client.get("/health")
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "healthy"
        assert data["service"] == "egg-mcp-server"

    def test_list_tools(self, mcp_client):
        response = mcp_client.get("/mcp/v1/tools")
        assert response.status_code == 200
        data = response.get_json()
        tools = data["tools"]
        assert len(tools) == 5
        tool_names = {t["name"] for t in tools}
        assert tool_names == {
            "submit_task",
            "get_status",
            "provide_input",
            "list_tasks",
            "cancel_task",
        }

    def test_call_tool_missing_body(self, mcp_client):
        response = mcp_client.post(
            "/mcp/v1/tools/call",
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_call_tool_missing_name(self, mcp_client):
        response = mcp_client.post(
            "/mcp/v1/tools/call",
            json={"arguments": {}},
        )
        assert response.status_code == 400

    def test_call_tool_unknown_tool(self, mcp_client):
        response = mcp_client.post(
            "/mcp/v1/tools/call",
            json={"name": "nonexistent_tool", "arguments": {}},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["isError"] is True
        result = json.loads(data["content"][0]["text"])
        assert "unknown tool" in result.get("error", "").lower()

    @patch.object(CoordinatorToolHandler, "handle_tool_call")
    def test_call_tool_success(self, mock_handler, mcp_client):
        mock_handler.return_value = {"task_id": "issue-42", "status": "created"}

        response = mcp_client.post(
            "/mcp/v1/tools/call",
            json={
                "name": "submit_task",
                "arguments": {"description": "Fix the bug"},
            },
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert result["task_id"] == "issue-42"

    @patch.object(CoordinatorToolHandler, "handle_tool_call")
    def test_call_tool_error_result(self, mock_handler, mcp_client):
        mock_handler.return_value = {"error": "Something went wrong"}

        response = mcp_client.post(
            "/mcp/v1/tools/call",
            json={"name": "get_status", "arguments": {"task_id": "bad"}},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["isError"] is True

    def test_rate_limiting(self):
        """Server should return 429 when rate limit exceeded."""
        server = MCPServer(
            orchestrator_url="http://localhost:9849",
            port=9850,
            rate_limit=2,
        )
        app = server.create_app()
        app.config["TESTING"] = True
        client = app.test_client()

        with patch.object(CoordinatorToolHandler, "handle_tool_call", return_value={"ok": True}):
            for _ in range(2):
                response = client.post(
                    "/mcp/v1/tools/call",
                    json={"name": "get_status", "arguments": {"task_id": "x"}},
                )
                assert response.status_code == 200

            response = client.post(
                "/mcp/v1/tools/call",
                json={"name": "get_status", "arguments": {"task_id": "x"}},
            )
            assert response.status_code == 429

    def test_sse_endpoint_returns_event_stream(self, mcp_client):
        """SSE endpoint should return event-stream content type."""
        response = mcp_client.get("/mcp/v1/sse")
        assert response.content_type.startswith("text/event-stream")


# ── Tool definitions tests ───────────────────────────────────────────


class TestToolDefinitions:
    """Tests for COORDINATOR_TOOLS schema definitions."""

    def test_all_tools_have_required_fields(self):
        for tool in COORDINATOR_TOOLS:
            assert "name" in tool, f"Tool missing name: {tool}"
            assert "description" in tool, f"Tool {tool['name']} missing description"
            assert "inputSchema" in tool, f"Tool {tool['name']} missing inputSchema"

    def test_submit_task_schema(self):
        tool = next(t for t in COORDINATOR_TOOLS if t["name"] == "submit_task")
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "description" in schema["required"]
        assert "description" in schema["properties"]
        assert "issue_number" in schema["properties"]
        assert "repo" in schema["properties"]
        assert "urgency" in schema["properties"]
        assert schema["properties"]["urgency"]["enum"] == ["low", "normal", "high"]

    def test_get_status_requires_task_id(self):
        tool = next(t for t in COORDINATOR_TOOLS if t["name"] == "get_status")
        assert "task_id" in tool["inputSchema"]["required"]

    def test_provide_input_requires_fields(self):
        tool = next(t for t in COORDINATOR_TOOLS if t["name"] == "provide_input")
        required = tool["inputSchema"]["required"]
        assert "task_id" in required
        assert "decision_id" in required
        assert "response" in required

    def test_list_tasks_has_filter_and_limit(self):
        tool = next(t for t in COORDINATOR_TOOLS if t["name"] == "list_tasks")
        props = tool["inputSchema"]["properties"]
        assert "status_filter" in props
        assert "limit" in props
        assert props["status_filter"]["enum"] == ["active", "completed", "failed", "all"]

    def test_cancel_task_requires_task_id(self):
        tool = next(t for t in COORDINATOR_TOOLS if t["name"] == "cancel_task")
        assert "task_id" in tool["inputSchema"]["required"]


# ── CoordinatorToolHandler tests ─────────────────────────────────────


class TestCoordinatorToolHandler:
    """Tests for tool call routing and handling."""

    def test_unknown_tool_returns_error(self):
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("bogus_tool", {})
        assert "error" in result
        assert "unknown tool" in result["error"].lower()

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_submit_task_with_issue(self, mock_req):
        mock_req.return_value = {"data": {"pipeline_id": "issue-42"}}
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call(
            "submit_task",
            {"description": "Fix auth bug", "issue_number": 42},
        )
        assert result["task_id"] == "issue-42"
        assert result["status"] == "created"
        # Verify the request included issue_number and mode=issue
        call_data = mock_req.call_args[1]["data"]
        assert call_data["issue_number"] == 42
        assert call_data["mode"] == "issue"

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_submit_task_without_issue(self, mock_req):
        mock_req.return_value = {"data": {"pipeline_id": "local-abcd1234"}}
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call(
            "submit_task",
            {"description": "Improve performance"},
        )
        assert result["task_id"] == "local-abcd1234"
        call_data = mock_req.call_args[1]["data"]
        assert call_data["mode"] == "local"
        assert call_data["prompt"] == "Improve performance"

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_get_status(self, mock_req):
        mock_req.return_value = {
            "data": {
                "current_phase": "implement",
                "running_agents": [],
            }
        }
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})
        assert result["current_phase"] == "implement"

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_provide_input(self, mock_req):
        mock_req.return_value = {"success": True}
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call(
            "provide_input",
            {"task_id": "issue-42", "decision_id": "d-1", "response": "REST"},
        )
        assert result["success"] is True
        # Verify correct endpoint
        mock_req.assert_called_once_with(
            "/api/v1/pipelines/issue-42/decisions/d-1",
            method="POST",
            data={"resolution": "REST"},
        )

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_list_tasks_filters_coordinator_pipelines(self, mock_req):
        mock_req.return_value = {
            "data": {
                "pipelines": [
                    {"id": "p1", "config": {"coordinator_enabled": True}, "status": "running"},
                    {"id": "p2", "config": {"coordinator_enabled": False}, "status": "running"},
                    {"id": "p3", "config": {"coordinator_enabled": True}, "status": "complete"},
                ]
            }
        }
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("list_tasks", {"status_filter": "active"})
        assert result["total"] == 1
        assert result["tasks"][0]["id"] == "p1"

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_list_tasks_all_filter(self, mock_req):
        mock_req.return_value = {
            "data": {
                "pipelines": [
                    {"id": "p1", "config": {"coordinator_enabled": True}, "status": "running"},
                    {"id": "p2", "config": {"coordinator_enabled": True}, "status": "complete"},
                    {"id": "p3", "config": {"coordinator_enabled": True}, "status": "failed"},
                ]
            }
        }
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("list_tasks", {"status_filter": "all"})
        assert result["total"] == 3

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_list_tasks_respects_limit(self, mock_req):
        mock_req.return_value = {
            "data": {
                "pipelines": [
                    {"id": f"p{i}", "config": {"coordinator_enabled": True}, "status": "running"}
                    for i in range(5)
                ]
            }
        }
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("list_tasks", {"status_filter": "all", "limit": 2})
        assert len(result["tasks"]) == 2
        assert result["total"] == 5

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_cancel_task(self, mock_req):
        mock_req.return_value = {"success": True}
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call(
            "cancel_task",
            {"task_id": "issue-42", "reason": "No longer needed"},
        )
        assert result["success"] is True

    def test_handler_catches_exceptions(self):
        """Tool handler should catch and return errors gracefully."""
        handler = CoordinatorToolHandler(orchestrator_url="http://unreachable:9849")
        result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})
        assert "error" in result

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_submit_task_empty_response_data(self, mock_req):
        """Handler should handle missing data field gracefully."""
        mock_req.return_value = {}
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call(
            "submit_task",
            {"description": "Fix bug"},
        )
        # Should not crash — returns empty string for pipeline_id
        assert result["task_id"] == ""

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_list_tasks_default_filter_is_active(self, mock_req):
        """Default status_filter should be 'active'."""
        mock_req.return_value = {
            "data": {
                "pipelines": [
                    {"id": "p1", "config": {"coordinator_enabled": True}, "status": "complete"},
                ]
            }
        }
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("list_tasks", {})
        # Complete tasks should be filtered out by "active" default
        assert result["total"] == 0

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_list_tasks_completed_filter(self, mock_req):
        mock_req.return_value = {
            "data": {
                "pipelines": [
                    {"id": "p1", "config": {"coordinator_enabled": True}, "status": "complete"},
                    {"id": "p2", "config": {"coordinator_enabled": True}, "status": "running"},
                ]
            }
        }
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("list_tasks", {"status_filter": "completed"})
        assert result["total"] == 1
        assert result["tasks"][0]["id"] == "p1"

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_list_tasks_failed_filter(self, mock_req):
        mock_req.return_value = {
            "data": {
                "pipelines": [
                    {"id": "p1", "config": {"coordinator_enabled": True}, "status": "failed"},
                    {"id": "p2", "config": {"coordinator_enabled": True}, "status": "running"},
                ]
            }
        }
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("list_tasks", {"status_filter": "failed"})
        assert result["total"] == 1
        assert result["tasks"][0]["id"] == "p1"
