"""
MCP server for pipeline management integration with Claude Code.

Provides an MCP-compatible server that exposes pipeline management tools
via Streamable HTTP transport using the official mcp Python SDK.
Runs as a sidecar alongside the orchestrator.
"""

import asyncio
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

# Upper bound for the ``get_status`` wait parameter.  Must stay safely under
# Claude Code's streamable-HTTP MCP tool-call timeout (~30s, and the documented
# MCP_TOOL_TIMEOUT env var is ignored on that transport, see
# anthropics/claude-code#20335), otherwise the client gives up before we reply.
GET_STATUS_MAX_WAIT = 25

# Module-level reference so tests can patch ``mcp_server._async_sleep``
# without replacing the global ``asyncio.sleep`` (which would capture
# unrelated calls from anyio internals during the test suite).
_async_sleep = asyncio.sleep


async def _apply_get_status_wait(tool_name: str, kwargs: dict) -> None:
    """Handle the ``get_status`` ``wait`` parameter on the event loop.

    Consumes the ``wait`` key from ``kwargs`` (if present) and awaits
    ``asyncio.sleep`` for up to :data:`GET_STATUS_MAX_WAIT` seconds.  Running
    this in the async wrapper — rather than ``time.sleep`` inside the sync
    tool handler — keeps the anyio worker thread pool free during polling
    delays and makes the wait cancellable when the client disconnects.

    Non-numeric, zero, or negative ``wait`` values are silently ignored.
    """
    if tool_name != "get_status":
        return
    wait = kwargs.pop("wait", 0)
    if isinstance(wait, bool):
        return  # bool is a subclass of int; reject to avoid True -> 1 sleep
    if isinstance(wait, (int, float)) and wait > 0:
        await _async_sleep(min(wait, GET_STATUS_MAX_WAIT))


class RateLimiter:
    """Thread-safe sliding-window rate limiter.

    FastMCP runs with ``stateless_http=True`` and dispatches each tool
    call through ``anyio.to_thread.run_sync`` (see :func:`MCPServer.create_app`),
    so :meth:`allow` can be hit from multiple OS worker threads under
    contention.  Mutations to ``_requests`` are guarded by ``_lock`` so
    the limiter stays exact across that boundary.
    """

    def __init__(self, max_requests: int = DEFAULT_RATE_LIMIT, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: list[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """Check if a request is allowed.

        Prunes expired entries and records the new one atomically — see
        the class docstring for why the lock is required despite the
        async wrapper.
        """
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            self._requests = [t for t in self._requests if t > cutoff]
            if len(self._requests) >= self.max_requests:
                return False
            self._requests.append(now)
            return True


class MCPServer:
    """MCP server with Streamable HTTP transport for pipeline management tools.

    Uses the official mcp Python SDK (FastMCP) to expose pipeline tools
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
        gateway_url: str | None = None,
        port: int = DEFAULT_MCP_PORT,
        rate_limit: int = DEFAULT_RATE_LIMIT,
    ):
        self.orchestrator_url = orchestrator_url
        self.gateway_url = gateway_url
        self.port = port
        self.rate_limiter = RateLimiter(max_requests=rate_limit)

        from mcp_tools import PIPELINE_TOOLS, PipelineToolHandler

        self.tool_handler = PipelineToolHandler(
            orchestrator_url=orchestrator_url,
            gateway_url=gateway_url,
        )
        self.tools_config = PIPELINE_TOOLS
        self._mcp = None

    def create_app(self):
        """Create the FastMCP application with pipeline management tools."""
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

        # Register each pipeline tool with FastMCP.
        # We create wrapper functions that delegate to PipelineToolHandler.
        def _make_tool_fn(tool_name: str, tool_schema: dict):
            """Build an async tool function for FastMCP from a tool schema."""
            required = set(tool_schema.get("required", []))
            properties = tool_schema.get("properties", {})

            async def tool_fn(**kwargs) -> str:
                if not rate_limiter.allow():
                    return json.dumps({"error": "Rate limit exceeded"})

                # ``get_status`` supports an optional server-side polling
                # delay via ``wait``.  We sleep on the event loop here rather
                # than inside the sync handler so no worker thread is held
                # during the delay — the anyio thread pool is shared with
                # every other MCP tool call, and a blocking time.sleep would
                # pin a worker for the full wait even after a client timeout
                # (time.sleep cannot be cancelled), leading to thread-pool
                # exhaustion under polling load.
                await _apply_get_status_wait(tool_name, kwargs)

                result = await anyio.to_thread.run_sync(
                    functools.partial(tool_handler.handle_tool_call, tool_name, kwargs)
                )
                return json.dumps(result, indent=2)

            # Build a useful signature so FastMCP can inspect parameters
            import inspect
            from typing import Optional

            params = []
            for prop_name, prop_def in properties.items():
                default = prop_def.get("default", inspect.Parameter.empty)
                is_optional = prop_name not in required
                if is_optional and default is inspect.Parameter.empty:
                    default = None
                annotation = _json_type_to_python(prop_def)
                if is_optional:
                    # Pydantic v2 requires Optional[T] (not bare T) for fields
                    # with a None default — bare T with default=None causes
                    # "Field required" errors when the argument is omitted.
                    annotation = Optional[annotation]
                params.append(
                    inspect.Parameter(
                        prop_name,
                        inspect.Parameter.KEYWORD_ONLY,
                        default=default,
                        annotation=annotation,
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
    """Map JSON Schema type to Python type annotation for FastMCP.

    FastMCP builds a Pydantic model from the tool's signature
    annotations and rejects any call whose argument shape doesn't
    match.  Missing ``"array"`` / ``"object"`` rows in the mapping
    silently fell through to ``str`` here, which made any dict-valued
    (``config``) or list-valued (``roles``) parameter unreachable
    over the MCP transport — the client got
    ``"Input should be a valid string"`` from Pydantic *before* the
    tool handler ever ran, even though the JSON-Schema input the
    tool advertised said ``object`` / ``array``.
    """
    json_type = prop_def.get("type", "string")
    mapping: dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    return mapping.get(json_type, str)


def start_mcp_server(
    orchestrator_url: str = "http://localhost:9849",
    gateway_url: str | None = None,
    port: int = DEFAULT_MCP_PORT,
    rate_limit: int = DEFAULT_RATE_LIMIT,
) -> MCPServer:
    """Start the MCP server in a background thread."""
    server = MCPServer(
        orchestrator_url=orchestrator_url,
        gateway_url=gateway_url,
        port=port,
        rate_limit=rate_limit,
    )

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    logger.info("MCP server started in background", port=port)
    return server
