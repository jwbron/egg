"""
MCP server for coordinator integration with Claude Code.

Provides an MCP-compatible server that exposes coordinator tools
via Streamable HTTP transport using the official mcp Python SDK.
Runs as a sidecar alongside the orchestrator.
"""

import functools
import json
import sys
import threading
import time
from pathlib import Path

import anyio

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:
        return logging.getLogger(name)


logger = get_logger("orchestrator.mcp_server")

# Default configuration
DEFAULT_MCP_PORT = 9850
DEFAULT_RATE_LIMIT = 30  # requests per minute


class RateLimiter:
    """Simple sliding-window rate limiter (async-safe)."""

    def __init__(self, max_requests: int = DEFAULT_RATE_LIMIT, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: list[float] = []

    def allow(self) -> bool:
        """Check if a request is allowed.

        Safe to call from the event loop — single-event-loop usage means
        no concurrent calls to this method, so no locks are needed.
        """
        now = time.time()
        cutoff = now - self.window_seconds
        self._requests = [t for t in self._requests if t > cutoff]
        if len(self._requests) >= self.max_requests:
            return False
        self._requests.append(now)
        return True


class MCPServer:
    """MCP server with Streamable HTTP transport for coordinator tools.

    Uses the official mcp Python SDK (FastMCP) to expose coordinator tools
    over the Streamable HTTP transport protocol.

    Provides:
    - MCP tools via Streamable HTTP at /mcp
    - Health check endpoint at /health
    - Rate limiting on tool calls

    No authentication required — localhost-only access is enforced via
    Docker port mapping (127.0.0.1 binding in docker-compose.yml).
    """

    def __init__(
        self,
        orchestrator_url: str = "http://localhost:9849",
        port: int = DEFAULT_MCP_PORT,
        rate_limit: int = DEFAULT_RATE_LIMIT,
    ):
        self.orchestrator_url = orchestrator_url
        self.port = port
        self.rate_limiter = RateLimiter(max_requests=rate_limit)

        from mcp_tools import COORDINATOR_TOOLS, CoordinatorToolHandler

        self.tool_handler = CoordinatorToolHandler(orchestrator_url=orchestrator_url)
        self.tools_config = COORDINATOR_TOOLS
        self._mcp = None

    def create_app(self):
        """Create the FastMCP application with coordinator tools."""
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP(
            "egg-mcp-server",
            host="0.0.0.0",
            port=self.port,
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
        )

        rate_limiter = self.rate_limiter
        tool_handler = self.tool_handler

        # Register /health as a custom route
        @mcp.custom_route("/health", methods=["GET"])
        async def health(request):
            from starlette.responses import JSONResponse

            return JSONResponse({"status": "healthy", "service": "egg-mcp-server"})

        # Register each coordinator tool with FastMCP.
        # We create wrapper functions that delegate to CoordinatorToolHandler.
        def _make_tool_fn(tool_name: str, tool_schema: dict):
            """Build an async tool function for FastMCP from a tool schema."""
            required = set(tool_schema.get("required", []))
            properties = tool_schema.get("properties", {})

            async def tool_fn(**kwargs) -> str:
                if not rate_limiter.allow():
                    return json.dumps({"error": "Rate limit exceeded"})
                result = await anyio.to_thread.run_sync(
                    functools.partial(tool_handler.handle_tool_call, tool_name, kwargs)
                )
                return json.dumps(result, indent=2)

            # Build a useful signature so FastMCP can inspect parameters
            import inspect

            params = []
            for prop_name, prop_def in properties.items():
                default = prop_def.get("default", inspect.Parameter.empty)
                if prop_name not in required and default is inspect.Parameter.empty:
                    default = None
                params.append(
                    inspect.Parameter(
                        prop_name,
                        inspect.Parameter.KEYWORD_ONLY,
                        default=default,
                        annotation=_json_type_to_python(prop_def),
                    )
                )
            tool_fn.__signature__ = inspect.Signature(params, return_annotation=str)
            tool_fn.__name__ = tool_name
            tool_fn.__qualname__ = tool_name
            return tool_fn

        for tool_def in self.tools_config:
            fn = _make_tool_fn(tool_def["name"], tool_def["inputSchema"])
            mcp.tool(
                name=tool_def["name"],
                description=tool_def["description"],
            )(fn)

        self._mcp = mcp
        return mcp

    def run(self):
        """Start the MCP server.

        Binds to 0.0.0.0 inside the container so Docker port forwarding works.
        Host is set in the FastMCP constructor; localhost-only access is enforced
        by the docker-compose port mapping.
        """
        mcp = self.create_app()
        logger.info("Starting MCP server", port=self.port)
        mcp.run(transport="streamable-http")


def _json_type_to_python(prop_def: dict) -> type:
    """Map JSON Schema type to Python type annotation for FastMCP."""
    json_type = prop_def.get("type", "string")
    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }
    return mapping.get(json_type, str)


def start_mcp_server(
    orchestrator_url: str = "http://localhost:9849",
    port: int = DEFAULT_MCP_PORT,
    rate_limit: int = DEFAULT_RATE_LIMIT,
) -> MCPServer:
    """Start the MCP server in a background thread."""
    server = MCPServer(
        orchestrator_url=orchestrator_url,
        port=port,
        rate_limit=rate_limit,
    )

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    logger.info("MCP server started in background", port=port)
    return server
