"""
Tests for the egg_orchestrator shared package.

Tests the types, client, and detection functionality used for
sandbox-to-orchestrator communication.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add shared module to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))

from egg_orchestrator import (
    CompletionData,
    DeploymentMode,
    ErrorData,
    OrchestratorClient,
    ProgressData,
    SignalResponse,
    SignalType,
    get_orchestrator_client,
    get_orchestrator_url,
    is_orchestrator_mode,
)
from egg_orchestrator.types import AgentRole


class TestDeploymentMode:
    """Tests for DeploymentMode enum."""

    def test_enum_values(self):
        """Test that all expected values exist."""
        assert DeploymentMode.LOCAL == "local"
        assert DeploymentMode.REMOTE_SINGLE == "remote-single"
        assert DeploymentMode.DISTRIBUTED == "distributed"

    def test_from_env_default(self, monkeypatch):
        """Test default mode when env var not set."""
        monkeypatch.delenv("EGG_ORCHESTRATOR_MODE", raising=False)
        assert DeploymentMode.from_env() == DeploymentMode.LOCAL

    def test_from_env_distributed(self, monkeypatch):
        """Test distributed mode detection."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "distributed")
        assert DeploymentMode.from_env() == DeploymentMode.DISTRIBUTED

    def test_from_env_invalid(self, monkeypatch):
        """Test fallback for invalid mode."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "invalid-mode")
        assert DeploymentMode.from_env() == DeploymentMode.LOCAL


class TestSignalType:
    """Tests for SignalType enum."""

    def test_enum_values(self):
        """Test all signal types exist."""
        assert SignalType.COMPLETE == "complete"
        assert SignalType.PROGRESS == "progress"
        assert SignalType.ERROR == "error"
        assert SignalType.HEARTBEAT == "heartbeat"


class TestAgentRole:
    """Tests for AgentRole enum."""

    def test_enum_values(self):
        """Test all agent roles exist."""
        assert AgentRole.CODER == "coder"
        assert AgentRole.REVIEWER == "reviewer"
        assert AgentRole.CHECKER == "checker"
        assert AgentRole.TESTER == "tester"
        assert AgentRole.DOCUMENTER == "documenter"
        assert AgentRole.INTEGRATOR == "integrator"


class TestProgressData:
    """Tests for ProgressData validation."""

    def test_valid_progress(self):
        """Test creating valid progress data."""
        data = ProgressData(agent_role="coder", progress_percent=50)
        assert data.progress_percent == 50

    def test_progress_at_bounds(self):
        """Test progress at boundary values."""
        data_min = ProgressData(agent_role="coder", progress_percent=0)
        assert data_min.progress_percent == 0

        data_max = ProgressData(agent_role="coder", progress_percent=100)
        assert data_max.progress_percent == 100

    def test_progress_below_minimum(self):
        """Test that negative progress raises error."""
        with pytest.raises(ValueError, match="progress_percent must be 0-100"):
            ProgressData(agent_role="coder", progress_percent=-1)

    def test_progress_above_maximum(self):
        """Test that progress over 100 raises error."""
        with pytest.raises(ValueError, match="progress_percent must be 0-100"):
            ProgressData(agent_role="coder", progress_percent=101)


class TestCompletionData:
    """Tests for CompletionData serialization."""

    def test_to_dict_basic(self):
        """Test basic completion data serialization."""
        data = CompletionData(agent_role="coder")
        result = data.to_dict()

        assert result["signal_type"] == "complete"
        assert result["agent_role"] == "coder"
        assert result["commit"] is None
        assert result["files_changed"] == []

    def test_to_dict_with_commit(self):
        """Test completion data with commit."""
        data = CompletionData(
            agent_role="coder",
            commit="abc1234",
            files_changed=["src/main.py"],
        )
        result = data.to_dict()

        assert result["commit"] == "abc1234"
        assert result["files_changed"] == ["src/main.py"]


class TestErrorData:
    """Tests for ErrorData serialization."""

    def test_to_dict_basic(self):
        """Test basic error data serialization."""
        data = ErrorData(agent_role="coder", error="Test failed")
        result = data.to_dict()

        assert result["signal_type"] == "error"
        assert result["agent_role"] == "coder"
        assert result["error"] == "Test failed"
        assert result["recoverable"] is False
        assert "traceback" not in result

    def test_to_dict_with_traceback(self):
        """Test error data with traceback."""
        data = ErrorData(
            agent_role="coder",
            error="Test failed",
            recoverable=True,
            traceback="Traceback...",
        )
        result = data.to_dict()

        assert result["recoverable"] is True
        assert result["traceback"] == "Traceback..."


class TestSignalResponse:
    """Tests for SignalResponse parsing."""

    def test_from_dict_success(self):
        """Test parsing successful response."""
        response = SignalResponse.from_dict({
            "success": True,
            "message": "Signal received",
            "data": {"id": 123},
        })

        assert response.success is True
        assert response.message == "Signal received"
        assert response.data == {"id": 123}

    def test_from_dict_minimal(self):
        """Test parsing minimal response."""
        response = SignalResponse.from_dict({})

        assert response.success is False
        assert response.message == ""
        assert response.data == {}


class TestOrchestratorDetection:
    """Tests for orchestrator mode detection."""

    def test_is_orchestrator_mode_false(self, monkeypatch):
        """Test detection when not in orchestrator mode."""
        monkeypatch.delenv("EGG_ORCHESTRATOR_MODE", raising=False)
        monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)
        assert is_orchestrator_mode() is False

    def test_is_orchestrator_mode_true(self, monkeypatch):
        """Test detection when in orchestrator mode."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_MODE", "distributed")
        monkeypatch.setenv("EGG_PIPELINE_ID", "issue-123")
        assert is_orchestrator_mode() is True

    def test_get_orchestrator_url_not_set(self, monkeypatch):
        """Test URL detection when not in orchestrator mode."""
        monkeypatch.delenv("EGG_ORCHESTRATOR_URL", raising=False)
        monkeypatch.delenv("EGG_ORCHESTRATOR_MODE", raising=False)
        assert get_orchestrator_url() is None

    def test_get_orchestrator_url_explicit(self, monkeypatch):
        """Test explicit orchestrator URL."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://custom:8080")
        assert get_orchestrator_url() == "http://custom:8080"


class TestOrchestratorClient:
    """Tests for OrchestratorClient."""

    def test_init_with_url(self):
        """Test client initialization with explicit URL."""
        client = OrchestratorClient(orchestrator_url="http://test:8080")
        assert client.orchestrator_url == "http://test:8080"

    def test_init_from_env(self, monkeypatch):
        """Test client initialization from environment."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://env:9090")
        client = OrchestratorClient()
        assert client.orchestrator_url == "http://env:9090"


class TestGetOrchestratorClientSingleton:
    """Tests for thread-safe singleton pattern."""

    def test_singleton_returns_same_instance(self, monkeypatch):
        """Test that singleton returns the same instance."""
        # Reset the global client first
        import egg_orchestrator.client as client_module
        client_module._orchestrator_client = None

        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://test:8080")

        client1 = get_orchestrator_client()
        client2 = get_orchestrator_client()

        assert client1 is client2

        # Cleanup
        client_module._orchestrator_client = None


class TestOrchestratorClientSignalMethods:
    """Tests for OrchestratorClient signal methods with mocked HTTP responses."""

    @pytest.fixture
    def client(self):
        """Create a client with test URL."""
        return OrchestratorClient(orchestrator_url="http://test-orchestrator:8080")

    @pytest.fixture
    def mock_urlopen(self, client):
        """Mock the client's opener to bypass proxy-free urllib opener."""
        with patch.object(client, "_opener") as mock_opener:
            yield mock_opener.open

    def _create_mock_response(self, data: dict) -> MagicMock:
        """Create a mock HTTP response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response

    def test_signal_complete_success(self, client, mock_urlopen):
        """Test signal_complete sends correct HTTP request."""
        mock_urlopen.return_value = self._create_mock_response({
            "success": True,
            "message": "Signal received",
            "data": {"signal_id": 123},
        })

        response = client.signal_complete(
            pipeline_id="issue-456",
            agent_role="coder",
            commit="abc1234",
            files_changed=["src/main.py"],
        )

        assert response.success is True
        assert response.message == "Signal received"

        # Verify the HTTP request
        mock_urlopen.assert_called_once()
        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "http://test-orchestrator:8080/api/v1/pipelines/issue-456/signal"
        assert request.method == "POST"

        body = json.loads(request.data.decode())
        assert body["signal_type"] == "complete"
        assert body["agent_role"] == "coder"
        assert body["commit"] == "abc1234"
        assert body["files_changed"] == ["src/main.py"]

    def test_signal_error_success(self, client, mock_urlopen):
        """Test signal_error sends correct HTTP request."""
        mock_urlopen.return_value = self._create_mock_response({
            "success": True,
            "message": "Error signal received",
        })

        response = client.signal_error(
            pipeline_id="issue-789",
            agent_role="tester",
            error="Test suite failed",
            recoverable=True,
            traceback="Traceback: ...",
        )

        assert response.success is True

        # Verify the HTTP request body
        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode())
        assert body["signal_type"] == "error"
        assert body["agent_role"] == "tester"
        assert body["error"] == "Test suite failed"
        assert body["recoverable"] is True
        assert body["traceback"] == "Traceback: ..."

    def test_signal_progress_success(self, client, mock_urlopen):
        """Test signal_progress sends correct HTTP request."""
        mock_urlopen.return_value = self._create_mock_response({
            "success": True,
            "message": "Progress updated",
        })

        response = client.signal_progress(
            pipeline_id="issue-100",
            agent_role="coder",
            progress_percent=50,
            current_task="Running tests",
            message="Halfway done",
        )

        assert response.success is True

        # Verify the HTTP request body
        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode())
        assert body["signal_type"] == "progress"
        assert body["agent_role"] == "coder"
        assert body["progress_percent"] == 50
        assert body["current_task"] == "Running tests"
        assert body["message"] == "Halfway done"

    def test_signal_heartbeat_success(self, client, mock_urlopen):
        """Test signal_heartbeat sends correct HTTP request."""
        mock_urlopen.return_value = self._create_mock_response({
            "success": True,
            "message": "Heartbeat received",
        })

        response = client.signal_heartbeat(
            pipeline_id="issue-200",
            agent_role="reviewer",
            container_id="container-abc123",
        )

        assert response.success is True

        # Verify the HTTP request body
        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode())
        assert body["signal_type"] == "heartbeat"
        assert body["agent_role"] == "reviewer"
        assert body["container_id"] == "container-abc123"

    def test_signal_http_error_with_json_response(self, client, mock_urlopen):
        """Test signal method handles HTTP errors with JSON response."""
        from urllib.error import HTTPError

        from egg_orchestrator.client import OrchestratorError

        error_response = MagicMock()
        error_response.read.return_value = json.dumps({
            "message": "Pipeline not found",
            "details": {"pipeline_id": "invalid"},
        }).encode()

        mock_urlopen.side_effect = HTTPError(
            url="http://test",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=error_response,
        )

        with pytest.raises(OrchestratorError) as exc_info:
            client.signal_complete(pipeline_id="invalid", agent_role="coder")

        assert exc_info.value.status_code == 404
        assert "Pipeline not found" in str(exc_info.value)

    def test_signal_http_error_with_non_json_response(self, client, mock_urlopen):
        """Test signal method handles HTTP errors with non-JSON response."""
        from urllib.error import HTTPError

        from egg_orchestrator.client import OrchestratorError

        error_response = MagicMock()
        error_response.read.return_value = b"Internal Server Error"

        mock_urlopen.side_effect = HTTPError(
            url="http://test",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=error_response,
        )

        with pytest.raises(OrchestratorError) as exc_info:
            client.signal_complete(pipeline_id="issue-123", agent_role="coder")

        assert exc_info.value.status_code == 500
        assert "Internal Server Error" in str(exc_info.value)
