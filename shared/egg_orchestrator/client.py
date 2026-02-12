"""Typed orchestrator API client for sandbox-to-orchestrator communication.

Provides a typed Python client for sandboxes to communicate with the
orchestrator during managed execution.
"""

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .constants import (
    ENV_ORCHESTRATOR_URL,
    ORCHESTRATOR_HEALTH_ENDPOINT,
    ORCHESTRATOR_ISOLATED_IP,
    ORCHESTRATOR_PORT,
    ORCHESTRATOR_SIGNAL_ENDPOINT,
)
from .types import (
    CompletionData,
    ErrorData,
    HeartbeatData,
    ProgressData,
    SignalResponse,
    SignalType,
)


class OrchestratorError(Exception):
    """Error from orchestrator operations."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details

    def __str__(self) -> str:
        if self.status_code:
            return f"OrchestratorError({self.status_code}): {self.message}"
        return f"OrchestratorError: {self.message}"


@dataclass
class OrchestratorHealth:
    """Orchestrator health status."""

    healthy: bool
    status: str
    service: str = "egg-orchestrator"
    timestamp: str | None = None
    components: dict[str, str] | None = None
    error: str | None = None


class OrchestratorClient:
    """Typed client for sandbox-to-orchestrator communication.

    Provides methods for:
    - Signaling completion/progress/errors back to orchestrator
    - Health checking the orchestrator
    - Sending heartbeats for monitoring

    Usage:
        from egg_orchestrator import OrchestratorClient

        client = OrchestratorClient()

        # Signal completion
        response = client.signal_complete(
            pipeline_id="issue-123",
            agent_role="coder",
            commit="abc1234",
        )

        # Signal error
        response = client.signal_error(
            pipeline_id="issue-123",
            agent_role="coder",
            error="Test failure",
            recoverable=True,
        )
    """

    # Default timeout for health checks (kept short to avoid blocking)
    HEALTH_CHECK_TIMEOUT = 5

    def __init__(
        self,
        orchestrator_url: str | None = None,
        timeout: int = 10,
    ):
        """Initialize the orchestrator client.

        Args:
            orchestrator_url: Orchestrator URL (default: from env or network)
            timeout: Request timeout in seconds (default: 10s for signal operations)
        """
        self.orchestrator_url = orchestrator_url or self._get_default_url()
        self.timeout = timeout
        # Bypass proxy env vars — orchestrator is always on the internal network
        self._opener = build_opener(ProxyHandler({}))

    def _get_default_url(self) -> str:
        """Get default orchestrator URL from environment or network."""
        # Check environment first
        env_url = os.environ.get(ENV_ORCHESTRATOR_URL)
        if env_url:
            return env_url

        # Default to isolated network IP
        return f"http://{ORCHESTRATOR_ISOLATED_IP}:{ORCHESTRATOR_PORT}"

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        timeout_override: int | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to the orchestrator.

        Args:
            endpoint: API endpoint path
            method: HTTP method
            data: Request body data
            timeout_override: Optional timeout override (uses self.timeout if not provided)

        Returns:
            Response JSON data

        Raises:
            OrchestratorError: On request failure
        """
        url = f"{self.orchestrator_url}{endpoint}"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        timeout = timeout_override if timeout_override is not None else self.timeout

        body = json.dumps(data).encode() if data else None

        try:
            request = Request(url, data=body, headers=headers, method=method)
            with self._opener.open(request, timeout=timeout) as response:
                result: dict[str, Any] = json.loads(response.read().decode())
                return result
        except HTTPError as e:
            # Preserve response body before parsing to avoid losing details on JSONDecodeError
            error_body = e.read().decode()
            try:
                error_data = json.loads(error_body)
                raise OrchestratorError(
                    error_data.get("message", str(e)),
                    status_code=e.code,
                    details=error_data.get("details"),
                ) from e
            except json.JSONDecodeError:
                raise OrchestratorError(f"{e}: {error_body}", status_code=e.code) from e
        except URLError as e:
            raise OrchestratorError(f"Failed to connect to orchestrator: {e.reason}") from e
        except TimeoutError as e:
            raise OrchestratorError("Orchestrator request timed out") from e

    def check_health(self) -> OrchestratorHealth:
        """Check orchestrator health status.

        Uses a shorter timeout (5s) than regular operations to avoid blocking.

        Returns:
            OrchestratorHealth with status information
        """
        try:
            result = self._make_request(
                ORCHESTRATOR_HEALTH_ENDPOINT,
                timeout_override=self.HEALTH_CHECK_TIMEOUT,
            )

            return OrchestratorHealth(
                healthy=result.get("status") == "healthy",
                status=result.get("status", "unknown"),
                service=result.get("service", "egg-orchestrator"),
                timestamp=result.get("timestamp"),
                components=result.get("components"),
            )
        except OrchestratorError as e:
            return OrchestratorHealth(
                healthy=False,
                status="unhealthy",
                error=str(e),
            )
        except Exception as e:
            return OrchestratorHealth(
                healthy=False,
                status="unreachable",
                error=str(e),
            )

    def is_healthy(self) -> bool:
        """Quick health check.

        Returns:
            True if orchestrator is healthy
        """
        return self.check_health().healthy

    def _send_signal(
        self,
        pipeline_id: str,
        data: dict[str, Any],
    ) -> SignalResponse:
        """Send a signal to the orchestrator.

        Args:
            pipeline_id: Pipeline ID
            data: Signal data (must include signal_type)

        Returns:
            SignalResponse from orchestrator

        Raises:
            OrchestratorError: On request failure
        """
        endpoint = ORCHESTRATOR_SIGNAL_ENDPOINT.format(pipeline_id=pipeline_id)
        result = self._make_request(endpoint, method="POST", data=data)
        return SignalResponse.from_dict(result)

    def signal_complete(
        self,
        pipeline_id: str,
        agent_role: str,
        commit: str | None = None,
        files_changed: list[str] | None = None,
        handoff_data: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> SignalResponse:
        """Signal agent completion to orchestrator.

        Args:
            pipeline_id: Pipeline ID
            agent_role: Role of the completing agent
            commit: Optional commit SHA
            files_changed: List of changed files
            handoff_data: Data to pass to dependent agents
            metrics: Execution metrics

        Returns:
            SignalResponse from orchestrator

        Raises:
            OrchestratorError: On request failure
        """
        data = CompletionData(
            agent_role=agent_role,
            commit=commit,
            files_changed=files_changed or [],
            handoff_data=handoff_data or {},
            metrics=metrics or {},
        )
        return self._send_signal(pipeline_id, data.to_dict())

    def signal_progress(
        self,
        pipeline_id: str,
        agent_role: str,
        progress_percent: int,
        current_task: str = "",
        message: str = "",
    ) -> SignalResponse:
        """Signal progress update to orchestrator.

        Args:
            pipeline_id: Pipeline ID
            agent_role: Role of the agent
            progress_percent: Completion percentage (0-100)
            current_task: Description of current task
            message: Optional status message

        Returns:
            SignalResponse from orchestrator

        Raises:
            OrchestratorError: On request failure
        """
        data = ProgressData(
            agent_role=agent_role,
            progress_percent=progress_percent,
            current_task=current_task,
            message=message,
        )
        return self._send_signal(pipeline_id, data.to_dict())

    def signal_error(
        self,
        pipeline_id: str,
        agent_role: str,
        error: str,
        recoverable: bool = False,
        traceback: str | None = None,
    ) -> SignalResponse:
        """Signal error to orchestrator.

        Args:
            pipeline_id: Pipeline ID
            agent_role: Role of the agent
            error: Error message
            recoverable: Whether the error is recoverable
            traceback: Optional traceback string

        Returns:
            SignalResponse from orchestrator

        Raises:
            OrchestratorError: On request failure
        """
        data = ErrorData(
            agent_role=agent_role,
            error=error,
            recoverable=recoverable,
            traceback=traceback,
        )
        return self._send_signal(pipeline_id, data.to_dict())

    def signal_heartbeat(
        self,
        pipeline_id: str,
        agent_role: str,
        container_id: str | None = None,
    ) -> SignalResponse:
        """Send heartbeat to orchestrator.

        Args:
            pipeline_id: Pipeline ID
            agent_role: Role of the agent
            container_id: Optional container ID

        Returns:
            SignalResponse from orchestrator

        Raises:
            OrchestratorError: On request failure
        """
        data = HeartbeatData(
            agent_role=agent_role,
            container_id=container_id,
        )
        return self._send_signal(pipeline_id, data.to_dict())

    def send_signal(
        self,
        pipeline_id: str,
        signal_type: SignalType,
        agent_role: str,
        **kwargs: Any,
    ) -> SignalResponse:
        """Generic signal sender.

        Args:
            pipeline_id: Pipeline ID
            signal_type: Type of signal
            agent_role: Role of the agent
            **kwargs: Additional signal data

        Returns:
            SignalResponse from orchestrator

        Raises:
            OrchestratorError: On request failure
        """
        data = {
            "signal_type": signal_type.value,
            "agent_role": agent_role,
            **kwargs,
        }
        return self._send_signal(pipeline_id, data)


# Singleton client instance with thread-safe initialization
import threading

_orchestrator_client: OrchestratorClient | None = None
_orchestrator_client_lock = threading.Lock()


def get_orchestrator_client() -> OrchestratorClient:
    """Get the singleton orchestrator client.

    Thread-safe lazy initialization using double-checked locking pattern.

    Returns:
        OrchestratorClient instance
    """
    global _orchestrator_client
    if _orchestrator_client is None:
        with _orchestrator_client_lock:
            # Double-check after acquiring lock
            if _orchestrator_client is None:
                _orchestrator_client = OrchestratorClient()
    return _orchestrator_client


__all__ = [
    "OrchestratorClient",
    "OrchestratorError",
    "OrchestratorHealth",
    "get_orchestrator_client",
]
