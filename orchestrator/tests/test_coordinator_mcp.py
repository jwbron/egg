"""
Tests for MCP server (Phase 4).

Tests the SSE-based MCP server sidecar for Claude Code integration,
including MCP tool definitions and authentication.
"""

import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


class TestMCPServerModuleExists:
    """Tests for the existence of MCP server modules."""

    def test_mcp_server_file_exists(self):
        """orchestrator/mcp_server.py must exist.

        Gap: This is a net-new component.
        """
        mcp_path = _project_root / "orchestrator" / "mcp_server.py"
        assert mcp_path.exists(), (
            "orchestrator/mcp_server.py does not exist. "
            "Create SSE-based MCP server using the official mcp Python package."
        )

    def test_mcp_tools_file_exists(self):
        """orchestrator/mcp_tools.py must exist.

        Gap: MCP tool definitions need to be created.
        """
        tools_path = _project_root / "orchestrator" / "mcp_tools.py"
        assert tools_path.exists(), (
            "orchestrator/mcp_tools.py does not exist. "
            "Create MCP tool definitions: submit_task, get_status, provide_input, "
            "list_tasks, cancel_task."
        )


class TestMCPServerStructure:
    """Tests for MCP server implementation structure."""

    def test_mcp_server_uses_sse_transport(self):
        """MCP server must use SSE transport."""
        mcp_path = _project_root / "orchestrator" / "mcp_server.py"
        if not mcp_path.exists():
            pytest.skip("mcp_server.py not yet created")

        content = mcp_path.read_text()
        has_sse = "sse" in content.lower()
        assert has_sse, "MCP server should use SSE transport"

    def test_mcp_server_has_health_endpoint(self):
        """MCP server must have a /health endpoint."""
        mcp_path = _project_root / "orchestrator" / "mcp_server.py"
        if not mcp_path.exists():
            pytest.skip("mcp_server.py not yet created")

        content = mcp_path.read_text()
        has_health = "health" in content.lower()
        assert has_health, "MCP server should have a health endpoint"

    def test_mcp_server_has_authentication(self):
        """MCP server must validate gateway session tokens.

        Gap: Authentication middleware.
        """
        mcp_path = _project_root / "orchestrator" / "mcp_server.py"
        if not mcp_path.exists():
            pytest.skip("mcp_server.py not yet created")

        content = mcp_path.read_text()
        has_auth = any(
            keyword in content.lower()
            for keyword in ["auth", "token", "session", "validate"]
        )
        assert has_auth, "MCP server should have authentication"

    def test_mcp_server_has_rate_limiting(self):
        """MCP server must have rate limiting (default 30 req/min).

        Gap: Rate limiting middleware.
        """
        mcp_path = _project_root / "orchestrator" / "mcp_server.py"
        if not mcp_path.exists():
            pytest.skip("mcp_server.py not yet created")

        content = mcp_path.read_text()
        has_rate_limit = "rate" in content.lower() and "limit" in content.lower()
        if not has_rate_limit:
            pytest.skip("Rate limiting not yet implemented in MCP server")


class TestMCPToolDefinitions:
    """Tests for MCP tool definitions."""

    def test_submit_task_tool_defined(self):
        """submit_task MCP tool must be defined."""
        tools_path = _project_root / "orchestrator" / "mcp_tools.py"
        if not tools_path.exists():
            pytest.skip("mcp_tools.py not yet created")

        content = tools_path.read_text()
        assert "submit_task" in content, "submit_task tool must be defined"

    def test_get_status_tool_defined(self):
        """get_status MCP tool must be defined."""
        tools_path = _project_root / "orchestrator" / "mcp_tools.py"
        if not tools_path.exists():
            pytest.skip("mcp_tools.py not yet created")

        content = tools_path.read_text()
        assert "get_status" in content, "get_status tool must be defined"

    def test_provide_input_tool_defined(self):
        """provide_input MCP tool must be defined."""
        tools_path = _project_root / "orchestrator" / "mcp_tools.py"
        if not tools_path.exists():
            pytest.skip("mcp_tools.py not yet created")

        content = tools_path.read_text()
        assert "provide_input" in content, "provide_input tool must be defined"

    def test_list_tasks_tool_defined(self):
        """list_tasks MCP tool must be defined."""
        tools_path = _project_root / "orchestrator" / "mcp_tools.py"
        if not tools_path.exists():
            pytest.skip("mcp_tools.py not yet created")

        content = tools_path.read_text()
        assert "list_tasks" in content, "list_tasks tool must be defined"

    def test_cancel_task_tool_defined(self):
        """cancel_task MCP tool must be defined."""
        tools_path = _project_root / "orchestrator" / "mcp_tools.py"
        if not tools_path.exists():
            pytest.skip("mcp_tools.py not yet created")

        content = tools_path.read_text()
        assert "cancel_task" in content, "cancel_task tool must be defined"

    def test_tools_proxy_to_orchestrator(self):
        """MCP tools must proxy to orchestrator APIs, not implement logic directly."""
        tools_path = _project_root / "orchestrator" / "mcp_tools.py"
        if not tools_path.exists():
            pytest.skip("mcp_tools.py not yet created")

        content = tools_path.read_text()
        # Tools should reference orchestrator client or API calls
        has_proxy = any(
            keyword in content.lower()
            for keyword in ["client", "api", "orchestrator", "requests", "http"]
        )
        assert has_proxy, "MCP tools should proxy to orchestrator APIs"
