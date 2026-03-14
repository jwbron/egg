"""
Functional tests for MCP server and coordinator tool handler.

Tests the MCPServer (FastMCP/Starlette), RateLimiter, CoordinatorToolHandler,
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
    """Tests for MCPServer Starlette/FastMCP endpoints."""

    @pytest.fixture
    def mcp_client(self):
        """Create an MCP server HTTPX test client with lifespan."""
        from starlette.testclient import TestClient

        server = MCPServer(
            orchestrator_url="http://localhost:9849",
            port=9850,
            rate_limit=100,
        )
        mcp = server.create_app()
        app = mcp.streamable_http_app()
        with TestClient(app) as client:
            yield client

    def test_health_endpoint(self, mcp_client):
        response = mcp_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "egg-mcp-server"

    def test_mcp_endpoint_exists(self, mcp_client):
        """The /mcp endpoint should accept POST requests (MCP protocol)."""
        # A GET to /mcp should return 405 (only POST/DELETE are valid)
        # or handle the SSE upgrade path depending on SDK version
        response = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1.0"},
                },
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        # Should get a valid JSON-RPC response (200) with server capabilities
        assert response.status_code == 200
        data = response.json()
        assert data.get("jsonrpc") == "2.0"
        assert "result" in data
        assert "serverInfo" in data["result"]

    def test_mcp_tools_list(self, mcp_client):
        """MCP tools/list should return all 5 coordinator tools."""
        # Initialize first
        init_resp = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1.0"},
                },
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        session_id = init_resp.headers.get("mcp-session-id")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id

        # Send initialized notification
        mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
            headers=headers,
        )

        # List tools
        response = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        tools = data["result"]["tools"]
        assert len(tools) == 5
        tool_names = {t["name"] for t in tools}
        assert tool_names == {
            "submit_task",
            "get_status",
            "provide_input",
            "list_tasks",
            "cancel_task",
        }

    @patch.object(CoordinatorToolHandler, "handle_tool_call")
    def test_call_tool_success(self, mock_handler, mcp_client):
        mock_handler.return_value = {"task_id": "issue-42", "status": "started"}

        # Initialize session
        init_resp = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1.0"},
                },
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        session_id = init_resp.headers.get("mcp-session-id")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id

        mcp_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )

        # Call tool
        response = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "submit_task",
                    "arguments": {"description": "Fix the bug", "repo": "owner/repo"},
                },
            },
            headers=headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert "result" in data
        content = data["result"]["content"]
        assert len(content) > 0
        result_text = content[0]["text"]
        result = json.loads(result_text)
        assert result["task_id"] == "issue-42"

    def test_rate_limiting(self):
        """Server should return rate limit error when limit exceeded."""
        from starlette.testclient import TestClient

        server = MCPServer(
            orchestrator_url="http://localhost:9849",
            port=9850,
            rate_limit=2,
        )
        mcp = server.create_app()
        app = mcp.streamable_http_app()

        with (
            patch.object(CoordinatorToolHandler, "handle_tool_call", return_value={"ok": True}),
            TestClient(app) as client,
        ):
            # Initialize session
            init_resp = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            session_id = init_resp.headers.get("mcp-session-id")
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if session_id:
                headers["mcp-session-id"] = session_id

            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )

            # Make calls up to the rate limit
            for i in range(2):
                response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 10 + i,
                        "method": "tools/call",
                        "params": {
                            "name": "get_status",
                            "arguments": {"task_id": "x"},
                        },
                    },
                    headers=headers,
                )
                assert response.status_code == 200
                data = response.json()
                result_text = data["result"]["content"][0]["text"]
                assert "Rate limit" not in result_text

            # Third call should hit rate limit (returned in tool response text)
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {
                        "name": "get_status",
                        "arguments": {"task_id": "x"},
                    },
                },
                headers=headers,
            )
            assert response.status_code == 200
            data = response.json()
            result_text = data["result"]["content"][0]["text"]
            result = json.loads(result_text)
            assert "Rate limit exceeded" in result.get("error", "")

    def test_tools_registered_with_correct_schemas(self, mcp_client):
        """All tools should be registered in FastMCP with proper parameters."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        init_resp = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1.0"},
                },
            },
            headers=headers,
        )
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["mcp-session-id"] = session_id
        mcp_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
        resp = mcp_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert resp.status_code == 200
        tools = resp.json()["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert tool_names == {
            "submit_task",
            "get_status",
            "provide_input",
            "list_tasks",
            "cancel_task",
        }


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
        assert "repo" in schema["required"]
        assert "branch" not in schema.get("required", [])
        assert "description" in schema["properties"]
        assert "issue_number" in schema["properties"]
        assert "branch" in schema["properties"]
        assert "repo" in schema["properties"]

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
        mock_req.return_value = {"data": {"pipeline": {"id": "issue-42"}}}
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call(
            "submit_task",
            {"description": "Fix auth bug", "issue_number": 42, "repo": "owner/repo"},
        )
        assert result["task_id"] == "issue-42"
        assert result["status"] == "started"
        # First call is the pipeline create; second is /start
        assert mock_req.call_count == 2
        call_data = mock_req.call_args_list[0][1]["data"]
        assert call_data["issue_number"] == 42
        assert "mode" not in call_data
        assert call_data["branch"] == "egg/issue-42"
        start_call = mock_req.call_args_list[1]
        assert "/start" in start_call[0][0]
        assert start_call[1]["method"] == "POST"

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_submit_task_with_issue_and_branch_override(self, mock_req):
        mock_req.return_value = {"data": {"pipeline": {"id": "issue-42"}}}
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call(
            "submit_task",
            {
                "description": "Fix auth bug",
                "issue_number": 42,
                "repo": "owner/repo",
                "branch": "egg/custom-branch",
            },
        )
        assert result["task_id"] == "issue-42"
        call_data = mock_req.call_args_list[0][1]["data"]
        assert call_data["issue_number"] == 42
        assert call_data["branch"] == "egg/custom-branch"

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_submit_task_without_issue(self, mock_req):
        mock_req.return_value = {"data": {"pipeline": {"id": "local-abcd1234"}}}
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call(
            "submit_task",
            {"description": "Improve performance", "repo": "owner/repo"},
        )
        assert result["task_id"] == "local-abcd1234"
        # First call is the pipeline create; second is /start
        assert mock_req.call_count == 2
        call_data = mock_req.call_args_list[0][1]["data"]
        assert "mode" not in call_data
        assert call_data["prompt"] == "Improve performance"
        start_call = mock_req.call_args_list[1]
        assert "/start" in start_call[0][0]
        assert start_call[1]["method"] == "POST"

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_get_status(self, mock_req):
        mock_req.side_effect = [
            # 1st call: coordinator state (primary)
            {
                "data": {
                    "current_phase": "implement",
                    "running_agents": [],
                }
            },
            # 2nd call: pipeline details (enrichment)
            {
                "data": {
                    "pipeline": {
                        "id": "issue-42",
                        "repo": "owner/repo",
                        "issue_number": 42,
                        "created_at": "2026-03-13T00:00:00Z",
                    }
                }
            },
            # 3rd call: messages (enrichment)
            {
                "data": {
                    "messages": [
                        {
                            "from_role": "coder",
                            "type": "PROGRESS",
                            "subject": "Implementation started",
                            "timestamp": "2026-03-13T00:01:00Z",
                        }
                    ]
                }
            },
        ]
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("get_status", {"task_id": "issue-42"})
        assert result["current_phase"] == "implement"
        # Verify enrichment: pipeline details
        assert "pipeline" in result
        assert result["pipeline"]["id"] == "issue-42"
        assert result["pipeline"]["repo"] == "owner/repo"
        assert result["pipeline"]["issue_number"] == 42
        # Verify enrichment: recent messages
        assert "recent_messages" in result
        assert len(result["recent_messages"]) == 1
        assert result["recent_messages"][0]["from_role"] == "coder"
        assert result["recent_messages"][0]["subject"] == "Implementation started"

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_get_status_enrichment_fallback(self, mock_req):
        """get_status returns core state when enrichment calls fail."""

        def side_effect_fn(url, **kwargs):
            if "coordinator/state" in url:
                return {
                    "data": {
                        "current_phase": "plan",
                        "running_agents": ["refiner"],
                    }
                }
            raise ConnectionError("enrichment unavailable")

        mock_req.side_effect = side_effect_fn
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call("get_status", {"task_id": "issue-99"})
        assert result["current_phase"] == "plan"
        # Enrichment keys should be absent (graceful fallback)
        assert "pipeline" not in result
        assert "recent_messages" not in result

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
            "/api/v1/pipelines/issue-42/decisions/d-1/resolve",
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
        # No /start call when pipeline_id is empty
        assert mock_req.call_count == 1

    @patch.object(CoordinatorToolHandler, "_make_request")
    def test_submit_task_start_failure_returns_created_not_started(self, mock_req):
        """If /start fails, caller gets task_id with created_not_started status."""
        mock_req.side_effect = [
            {"data": {"pipeline": {"id": "test-123"}}},  # create succeeds
            Exception("connection refused"),  # start fails
        ]
        handler = CoordinatorToolHandler()
        result = handler.handle_tool_call(
            "submit_task",
            {"description": "Fix bug", "repo": "owner/repo"},
        )
        assert result["task_id"] == "test-123"
        assert result["status"] == "created_not_started"
        assert mock_req.call_count == 2

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
