"""Status API for egg-launcher.

This module provides a simple HTTP API for monitoring the health and status
of the egg stack. It exposes endpoints for:
- Health checks
- Container status
- Configuration info

The API runs in a background thread and is accessible at http://localhost:8080
by default.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from lifecycle import EggLifecycleManager


class StatusHandler(BaseHTTPRequestHandler):
    """HTTP request handler for status API.

    This handler provides endpoints for health checks and status queries.
    """

    # Class-level reference to lifecycle manager
    manager: Optional["EggLifecycleManager"] = None

    def log_message(self, format: str, *args) -> None:
        """Suppress default logging."""
        pass

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/status" or self.path == "/":
            self._handle_status()
        elif self.path == "/health":
            self._handle_health()
        else:
            self._send_not_found()

    def _handle_status(self) -> None:
        """Handle /status endpoint."""
        if self.manager is None:
            self._send_error(500, "Lifecycle manager not initialized")
            return

        try:
            status = self.manager.get_status()
            self._send_json(status)
        except Exception as e:
            self._send_error(500, str(e))

    def _handle_health(self) -> None:
        """Handle /health endpoint."""
        if self.manager is None:
            self._send_json({"status": "unhealthy", "reason": "not initialized"}, 503)
            return

        try:
            status = self.manager.get_status()
            gateway_healthy = status.get("gateway", {}).get("healthy", False)

            if gateway_healthy:
                self._send_json({"status": "healthy"})
            else:
                self._send_json({"status": "unhealthy", "reason": "gateway not healthy"}, 503)
        except Exception as e:
            self._send_json({"status": "unhealthy", "reason": str(e)}, 503)

    def _send_json(self, data: dict, status_code: int = 200) -> None:
        """Send a JSON response.

        Args:
            data: Dictionary to send as JSON
            status_code: HTTP status code
        """
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_error(self, status_code: int, message: str) -> None:
        """Send an error response.

        Args:
            status_code: HTTP status code
            message: Error message
        """
        self._send_json({"error": message}, status_code)

    def _send_not_found(self) -> None:
        """Send a 404 response."""
        self._send_json(
            {
                "error": "Not found",
                "endpoints": ["/status", "/health"],
            },
            404,
        )


class StatusServer:
    """Background HTTP server for status API.

    This server runs in a daemon thread and provides health/status endpoints.
    """

    def __init__(self, manager: "EggLifecycleManager", port: int = 8080):
        """Initialize the status server.

        Args:
            manager: Lifecycle manager for status queries
            port: Port to listen on
        """
        self.manager = manager
        self.port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the server in a background thread."""
        # Set manager on handler class
        StatusHandler.manager = self.manager

        # Create server
        self._server = HTTPServer(("0.0.0.0", self.port), StatusHandler)

        # Start in daemon thread
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        print(f"Status API listening on http://localhost:{self.port}")

    def stop(self) -> None:
        """Stop the server."""
        if self._server:
            self._server.shutdown()
            self._server = None

        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


def run_status_api(
    manager: "EggLifecycleManager",
    port: int = 8080,
    background: bool = True,
) -> StatusServer | None:
    """Run the status API server.

    Args:
        manager: Lifecycle manager for status queries
        port: Port to listen on (0 to disable)
        background: If True, run in background thread

    Returns:
        StatusServer instance if running in background, None otherwise
    """
    if port <= 0:
        return None

    server = StatusServer(manager, port)

    if background:
        server.start()
        return server
    else:
        # Run in foreground (blocks)
        StatusHandler.manager = manager
        http_server = HTTPServer(("0.0.0.0", port), StatusHandler)
        print(f"Status API listening on http://localhost:{port}")
        try:
            http_server.serve_forever()
        except KeyboardInterrupt:
            pass
        return None
