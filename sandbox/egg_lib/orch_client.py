"""Thin HTTP client wrapping the orchestrator REST API.

Uses http.client (stdlib) to avoid external dependencies, matching the
pattern established by egg-pipeline-watch.
"""

import json
import os
from http.client import HTTPConnection, HTTPResponse
from typing import Any
from urllib.parse import urlparse


class OrchestratorError(Exception):
    """Raised when an orchestrator API call fails."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


def _is_inside_container() -> bool:
    """Detect whether we're running inside a Docker container."""
    if os.environ.get("EGG_CONTAINER"):
        return True
    try:
        return os.path.exists("/.dockerenv")
    except OSError:
        return False


def get_orchestrator_url() -> str:
    """Get the orchestrator base URL from environment or auto-detect.

    Priority:
    1. EGG_ORCHESTRATOR_URL env var (explicit override)
    2. http://egg-orchestrator:9849 (inside container)
    3. http://localhost:9849 (host machine)
    """
    env_url = os.environ.get("EGG_ORCHESTRATOR_URL")
    if env_url:
        return env_url
    if _is_inside_container():
        return "http://egg-orchestrator:9849"
    return "http://localhost:9849"


class OrchClient:
    """HTTP client for the orchestrator REST API."""

    def __init__(self, base_url: str | None = None, timeout: int = 30) -> None:
        self.base_url = base_url or get_orchestrator_url()
        parsed = urlparse(self.base_url)
        self.host = parsed.hostname or "egg-orchestrator"
        self.port = parsed.port or 9849
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request and return parsed JSON response."""
        conn: HTTPConnection | None = None
        try:
            conn = HTTPConnection(self.host, self.port, timeout=self.timeout)
            headers: dict[str, str] = {"Accept": "application/json"}
            encoded_body: str | None = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                encoded_body = json.dumps(body)
            conn.request(method, path, body=encoded_body, headers=headers)
            response: HTTPResponse = conn.getresponse()
            raw = response.read().decode("utf-8", errors="replace")

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"raw": raw}

            if response.status >= 400:
                msg = data.get("message", raw) if isinstance(data, dict) else raw
                raise OrchestratorError(msg, status_code=response.status)

            return data
        except OrchestratorError:
            raise
        except ConnectionRefusedError:
            raise OrchestratorError(
                f"Cannot connect to orchestrator at {self.base_url}. "
                "Is the orchestrator running?"
            ) from None
        except TimeoutError:
            raise OrchestratorError(
                f"Connection to orchestrator timed out ({self.timeout}s)"
            ) from None
        except Exception as e:
            raise OrchestratorError(f"Orchestrator request failed: {e}") from e
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _stream_request(self, path: str) -> tuple[HTTPConnection, HTTPResponse]:
        """Make an SSE streaming request. Caller owns the connection lifecycle."""
        conn = HTTPConnection(self.host, self.port, timeout=120)
        conn.request(
            "GET",
            path,
            headers={
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
            },
        )
        response = conn.getresponse()
        if response.status != 200:
            raw = response.read().decode("utf-8", errors="replace")
            conn.close()
            try:
                err = json.loads(raw)
                msg = err.get("message", raw)
            except json.JSONDecodeError:
                msg = raw
            raise OrchestratorError(msg, status_code=response.status)
        return conn, response

    # --- API Methods ---

    def health_check(self) -> bool:
        """Check if the orchestrator is healthy."""
        try:
            data = self._request("GET", "/api/v1/health")
            return data.get("status") == "healthy"
        except OrchestratorError:
            return False

    def create_pipeline(
        self,
        issue_number: int | None = None,
        repo: str | None = None,
        branch: str | None = None,
        mode: str = "issue",
        prompt: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new pipeline."""
        body: dict[str, Any] = {"mode": mode}
        if issue_number is not None:
            body["issue_number"] = issue_number
        if repo:
            body["repo"] = repo
        if branch:
            body["branch"] = branch
        if prompt:
            body["prompt"] = prompt
        if config:
            body["config"] = config
        data = self._request("POST", "/api/v1/pipelines", body=body)
        return data.get("data", {}).get("pipeline", data)

    def start_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        """Start a pipeline."""
        return self._request("POST", f"/api/v1/pipelines/{pipeline_id}/start", body={})

    def get_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        """Get pipeline details."""
        data = self._request("GET", f"/api/v1/pipelines/{pipeline_id}")
        return data.get("data", data)

    def get_pipeline_status(self, pipeline_id: str) -> dict[str, Any]:
        """Get pipeline status for polling."""
        data = self._request("GET", f"/api/v1/pipelines/{pipeline_id}/status")
        return data.get("data", data)

    def list_decisions(
        self, pipeline_id: str, pending_only: bool = False
    ) -> list[dict[str, Any]]:
        """List HITL decisions for a pipeline."""
        path = f"/api/v1/pipelines/{pipeline_id}/decisions"
        if pending_only:
            path += "?pending=true"
        data = self._request("GET", path)
        return data.get("data", {}).get("decisions", [])

    def resolve_decision(
        self, pipeline_id: str, decision_id: str, resolution: str
    ) -> dict[str, Any]:
        """Resolve a HITL decision."""
        return self._request(
            "POST",
            f"/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/resolve",
            body={"resolution": resolution},
        )

    def cancel_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        """Cancel a running pipeline."""
        return self._request(
            "PATCH",
            f"/api/v1/pipelines/{pipeline_id}",
            body={"status": "cancelled"},
        )

    def stream_pipeline(self, pipeline_id: str) -> tuple[HTTPConnection, HTTPResponse]:
        """Open an SSE stream for pipeline events.

        Returns (connection, response) — caller must close the connection.
        """
        return self._stream_request(f"/api/v1/pipelines/{pipeline_id}/stream")
