"""
SSE-based MCP server for coordinator integration with Claude Code.

Provides an MCP-compatible server that exposes coordinator tools
via Server-Sent Events transport. Runs as a sidecar alongside
the orchestrator.
"""

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

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
    """Simple token bucket rate limiter."""

    def __init__(self, max_requests: int = DEFAULT_RATE_LIMIT, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: list[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """Check if a request is allowed."""
        now = time.time()
        with self._lock:
            # Remove expired entries
            cutoff = now - self.window_seconds
            self._requests = [t for t in self._requests if t > cutoff]
            if len(self._requests) >= self.max_requests:
                return False
            self._requests.append(now)
            return True


class MCPServer:
    """MCP server with SSE transport for coordinator tools.

    Provides:
    - Tool listing endpoint
    - Tool execution endpoint
    - Health check endpoint
    - Rate limiting
    - Gateway token authentication
    """

    def __init__(
        self,
        orchestrator_url: str = "http://localhost:9849",
        port: int = DEFAULT_MCP_PORT,
        rate_limit: int = DEFAULT_RATE_LIMIT,
        gateway_url: str | None = None,
    ):
        self.orchestrator_url = orchestrator_url
        self.port = port
        self.rate_limiter = RateLimiter(max_requests=rate_limit)
        self.gateway_url = gateway_url

        from mcp_tools import COORDINATOR_TOOLS, CoordinatorToolHandler

        self.tool_handler = CoordinatorToolHandler(orchestrator_url=orchestrator_url)
        self.tools = COORDINATOR_TOOLS
        self._app = None

    def _validate_gateway_token(self, token: str) -> bool:
        """Validate a session token against the gateway.

        Args:
            token: Bearer token from Authorization header

        Returns:
            True if the token is valid
        """
        if not self.gateway_url:
            logger.warning("No gateway_url configured, skipping token validation")
            return False

        try:
            url = f"{self.gateway_url}/api/v1/sessions/{token}"
            req = urllib.request.Request(url, method="GET")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                return data.get("valid", False)
        except Exception:
            logger.warning("Gateway token validation failed", gateway_url=self.gateway_url)
            return False

    def create_app(self):
        """Create the Flask application for the MCP server."""
        import functools

        from flask import Flask, Response, jsonify, request

        app = Flask("egg-mcp-server")
        server = self

        def require_auth(f):
            """Decorator that validates gateway session tokens on protected endpoints."""

            @functools.wraps(f)
            def decorated(*args, **kwargs):
                if not server.gateway_url:
                    # No gateway configured — reject all requests
                    return jsonify({"error": "Authentication not configured"}), 503

                auth_header = request.headers.get("Authorization", "")
                if not auth_header.startswith("Bearer "):
                    return jsonify({"error": "Missing or invalid Authorization header"}), 401

                token = auth_header[7:]  # Remove "Bearer " prefix
                if not server._validate_gateway_token(token):
                    return jsonify({"error": "Invalid or expired session token"}), 401

                return f(*args, **kwargs)

            return decorated

        @app.route("/health")
        def health():
            return jsonify({"status": "healthy", "service": "egg-mcp-server"})

        @app.route("/mcp/v1/tools", methods=["GET"])
        def list_tools():
            return jsonify({"tools": self.tools})

        @app.route("/mcp/v1/tools/call", methods=["POST"])
        @require_auth
        def call_tool():
            # Rate limiting
            if not self.rate_limiter.allow():
                return jsonify({"error": "Rate limit exceeded"}), 429

            data = request.get_json()
            if not data:
                return jsonify({"error": "Missing request body"}), 400

            tool_name = data.get("name")
            arguments = data.get("arguments", {})

            if not tool_name:
                return jsonify({"error": "Missing tool name"}), 400

            result = self.tool_handler.handle_tool_call(tool_name, arguments)

            return jsonify(
                {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "isError": "error" in result,
                }
            )

        @app.route("/mcp/v1/sse")
        @require_auth
        def sse_stream():
            """SSE endpoint for MCP protocol events."""

            def generate():
                # Send initial tools list
                tools_event = json.dumps({"type": "tools_list", "tools": self.tools})
                yield f"data: {tools_event}\n\n"

                # Keep connection alive with heartbeats
                while True:
                    time.sleep(15)
                    yield ": heartbeat\n\n"

            return Response(
                generate(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        self._app = app
        return app

    def run(self, host: str = "0.0.0.0", debug: bool = False):
        """Start the MCP server."""
        app = self.create_app()
        logger.info("Starting MCP server", port=self.port, host=host)
        app.run(host=host, port=self.port, debug=debug, threaded=True)


def start_mcp_server(
    orchestrator_url: str = "http://localhost:9849",
    port: int = DEFAULT_MCP_PORT,
    rate_limit: int = DEFAULT_RATE_LIMIT,
    gateway_url: str | None = None,
) -> MCPServer:
    """Start the MCP server in a background thread."""
    server = MCPServer(
        orchestrator_url=orchestrator_url,
        port=port,
        rate_limit=rate_limit,
        gateway_url=gateway_url,
    )

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    logger.info("MCP server started in background", port=port)
    return server
