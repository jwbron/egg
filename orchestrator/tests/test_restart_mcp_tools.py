"""Tests for restart MCP tools.

Covers:
- restart_agent MCP tool definition and handler (task-1-4)
- restart_phase MCP tool definition and handler (task-1-4)
"""

import threading
from unittest.mock import patch

import pytest
from egg_config.constants import TEST_GATEWAY_PORT
from mcp_tools import PipelineToolHandler


@pytest.fixture
def handler():
    """Create a PipelineToolHandler with test URLs."""
    return PipelineToolHandler(
        orchestrator_url="http://localhost:9849",
        gateway_url=f"http://test-gateway:{TEST_GATEWAY_PORT}",
    )


# ---------------------------------------------------------------------------
# Tool definition tests
# ---------------------------------------------------------------------------


class TestRestartToolDefinitions:
    """Tests that restart tools are properly defined in PIPELINE_TOOLS."""

    def test_restart_agent_tool_exists(self):
        """restart_agent tool should be in PIPELINE_TOOLS."""
        from mcp_tools import PIPELINE_TOOLS

        tool_names = [t["name"] for t in PIPELINE_TOOLS]
        assert "restart_agent" in tool_names

    def test_restart_phase_tool_exists(self):
        """restart_phase tool should be in PIPELINE_TOOLS."""
        from mcp_tools import PIPELINE_TOOLS

        tool_names = [t["name"] for t in PIPELINE_TOOLS]
        assert "restart_phase" in tool_names

    def test_restart_agent_requires_task_id_and_role(self):
        """restart_agent should require task_id and agent_role."""
        from mcp_tools import PIPELINE_TOOLS

        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "restart_agent")
        schema = tool["inputSchema"]

        assert "task_id" in schema.get("required", [])
        assert "agent_role" in schema.get("required", [])

    def test_restart_phase_requires_task_id_and_phase(self):
        """restart_phase should require task_id and phase."""
        from mcp_tools import PIPELINE_TOOLS

        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "restart_phase")
        schema = tool["inputSchema"]

        assert "task_id" in schema.get("required", [])
        assert "phase" in schema.get("required", [])

    def test_restart_agent_has_optional_reason(self):
        """restart_agent should have an optional reason field."""
        from mcp_tools import PIPELINE_TOOLS

        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "restart_agent")
        props = tool["inputSchema"]["properties"]

        assert "reason" in props
        assert "reason" not in tool["inputSchema"].get("required", [])

    def test_restart_agent_has_optional_slice_id(self):
        """restart_agent should expose an optional slice_id field (#2759)."""
        from mcp_tools import PIPELINE_TOOLS

        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "restart_agent")
        props = tool["inputSchema"]["properties"]

        assert "slice_id" in props
        assert "slice_id" not in tool["inputSchema"].get("required", [])

    def test_restart_phase_has_no_context_field(self):
        """restart_phase should not have a context field (not implemented in endpoint)."""
        from mcp_tools import PIPELINE_TOOLS

        tool = next(t for t in PIPELINE_TOOLS if t["name"] == "restart_phase")
        props = tool["inputSchema"]["properties"]

        assert "context" not in props


# ---------------------------------------------------------------------------
# Handler tests for restart_agent
# ---------------------------------------------------------------------------


class TestHandleRestartAgent:
    """Tests for _handle_restart_agent handler."""

    def test_calls_correct_endpoint(self, handler):
        """restart_agent should POST to /agents/<role>/restart."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"container_id": "xyz", "restart_count": 1},
            }
            handler.handle_tool_call(
                "restart_agent",
                {"task_id": "issue-42", "agent_role": "coder", "reason": "stall"},
            )

        mock_req.assert_called_once()
        call_args = mock_req.call_args
        endpoint = call_args[0][0] if call_args[0] else ""
        assert "/agents/coder/restart" in endpoint
        assert "issue-42" in endpoint

    def test_returns_structured_success(self, handler):
        """Successful restart returns structured response with restarted=True."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"container_id": "new-abc", "restart_count": 1},
            }
            result = handler.handle_tool_call(
                "restart_agent",
                {"task_id": "issue-42", "agent_role": "coder"},
            )

        assert result["restarted"] is True
        assert result["agent_role"] == "coder"

    def test_returns_error_on_failure(self, handler):
        """Failed restart returns error dict."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = Exception("connection refused")
            result = handler.handle_tool_call(
                "restart_agent",
                {"task_id": "issue-42", "agent_role": "coder"},
            )

        assert "error" in result

    def test_passes_reason_in_request(self, handler):
        """Reason should be passed in the POST body."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True, "data": {}}
            handler.handle_tool_call(
                "restart_agent",
                {"task_id": "issue-42", "agent_role": "coder", "reason": "heartbeat timeout"},
            )

        call_args = mock_req.call_args
        data = call_args.kwargs.get("data", {})
        assert data.get("reason") == "heartbeat timeout"

    def test_passes_slice_id_in_request(self, handler):
        """slice_id should be passed in the POST body so the route can
        target the slice's Job, worktree, and BRC tracker (#2759)."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True, "data": {}}
            handler.handle_tool_call(
                "restart_agent",
                {"task_id": "issue-42", "agent_role": "coder", "slice_id": "slice-3"},
            )

        call_args = mock_req.call_args
        data = call_args.kwargs.get("data", {})
        assert data.get("slice_id") == "slice-3"

    def test_omitted_slice_id_not_in_request(self, handler):
        """Omitting slice_id leaves it off the body so the route's
        auto-derivation path runs (#2759)."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True, "data": {}}
            handler.handle_tool_call(
                "restart_agent",
                {"task_id": "issue-42", "agent_role": "coder"},
            )

        call_args = mock_req.call_args
        data = call_args.kwargs.get("data", {})
        assert "slice_id" not in data


# ---------------------------------------------------------------------------
# Handler tests for restart_phase
# ---------------------------------------------------------------------------


class TestHandleRestartPhase:
    """Tests for _handle_restart_phase handler."""

    def test_calls_correct_endpoint(self, handler):
        """restart_phase should POST to /phases/<phase>/restart."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"agents_to_restart": ["coder", "tester"]},
            }
            handler.handle_tool_call(
                "restart_phase",
                {"task_id": "issue-42", "phase": "implement"},
            )

        mock_req.assert_called_once()
        call_args = mock_req.call_args
        endpoint = call_args[0][0] if call_args[0] else ""
        assert "/phases/implement/restart" in endpoint
        assert "issue-42" in endpoint

    def test_returns_structured_success(self, handler):
        """Successful phase restart returns agents_restarted list."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {
                "success": True,
                "data": {"agents_to_restart": ["coder", "tester", "documenter"]},
            }
            result = handler.handle_tool_call(
                "restart_phase",
                {"task_id": "issue-42", "phase": "implement"},
            )

        assert result["restarted"] is True
        assert result["phase"] == "implement"
        assert "coder" in result["agents_restarted"]

    def test_returns_error_on_failure(self, handler):
        """Failed phase restart returns error dict."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = Exception("pipeline not found")
            result = handler.handle_tool_call(
                "restart_phase",
                {"task_id": "issue-42", "phase": "implement"},
            )

        assert "error" in result

    def test_passes_reason(self, handler):
        """Reason should be passed in the POST body."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.return_value = {"success": True, "data": {}}
            handler.handle_tool_call(
                "restart_phase",
                {
                    "task_id": "issue-42",
                    "phase": "implement",
                    "reason": "multiple stalls",
                },
            )

        call_args = mock_req.call_args
        data = call_args.kwargs.get("data", {})
        assert data.get("reason") == "multiple stalls"
        assert "context" not in data


# ---------------------------------------------------------------------------
# Timeout handling tests (#1594)
# ---------------------------------------------------------------------------


class TestRestartAgentTimeout:
    """Tests for graceful timeout handling in restart_agent."""

    def test_timeout_returns_pending_not_error(self, handler):
        """Timeout should return restarted='pending', not an error."""
        from urllib.error import URLError

        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = URLError(TimeoutError("timed out"))
            result = handler.handle_tool_call(
                "restart_agent",
                {"task_id": "issue-42", "agent_role": "coder"},
            )

        assert "error" not in result
        assert result["restarted"] == "pending"
        assert result["agent_role"] == "coder"
        assert "get_status" in result["message"].lower()

    def test_non_timeout_oserror_returns_error(self, handler):
        """Non-timeout OSError should still return an error."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = ConnectionRefusedError("connection refused")
            result = handler.handle_tool_call(
                "restart_agent",
                {"task_id": "issue-42", "agent_role": "coder"},
            )

        assert "error" in result


class TestRestartPhaseTimeout:
    """Tests for graceful timeout handling in restart_phase."""

    def test_timeout_returns_pending_not_error(self, handler):
        """Timeout should return restarted='pending', not an error."""
        from urllib.error import URLError

        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = URLError(TimeoutError("timed out"))
            result = handler.handle_tool_call(
                "restart_phase",
                {"task_id": "issue-42", "phase": "implement"},
            )

        assert "error" not in result
        assert result["restarted"] == "pending"
        assert result["phase"] == "implement"
        assert "get_status" in result["message"].lower()

    def test_non_timeout_oserror_returns_error(self, handler):
        """Non-timeout OSError should still return an error."""
        with patch.object(handler, "_make_request") as mock_req:
            mock_req.side_effect = ConnectionRefusedError("connection refused")
            result = handler.handle_tool_call(
                "restart_phase",
                {"task_id": "issue-42", "phase": "implement"},
            )

        assert "error" in result


class TestCancelTaskBackgroundCleanup:
    """Tests for fire-and-forget DELETE in cancel_task (#1594)."""

    def test_cleanup_returns_immediately_with_started_flag(self, handler):
        """cancel_task with cleanup=True should return without waiting for DELETE."""
        import time

        call_log: list[str] = []

        def mock_request(endpoint, method="GET", **kwargs):
            call_log.append(method)
            if method == "PATCH":
                return {"success": True}
            elif method == "DELETE":
                # Simulate slow DELETE — should not block the response
                time.sleep(5)
                return {"success": True}
            return {}

        with patch.object(handler, "_make_request", side_effect=mock_request):
            start = time.monotonic()
            result = handler.handle_tool_call(
                "cancel_task",
                {"task_id": "issue-42", "cleanup": True},
            )
            elapsed = time.monotonic() - start

        assert result["cancelled"] is True
        assert result.get("cleanup_started") is True
        assert "PATCH" in call_log
        assert elapsed < 3, f"cancel_task took {elapsed:.1f}s — DELETE should be async"

        # Wait for the background thread to complete and verify DELETE executed
        for t in threading.enumerate():
            if t.name.startswith("mcp-cleanup-"):
                t.join(timeout=10)
        assert "DELETE" in call_log, "Background DELETE should have been called"
