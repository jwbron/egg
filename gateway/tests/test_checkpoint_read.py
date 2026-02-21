"""Tests for gateway checkpoint read endpoints (list, show, cost)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from egg_contracts.checkpoints import (
    AgentType,
    CheckpointIndexV2,
    CheckpointSummaryV2,
    CheckpointV2,
    SessionMetadata,
    SessionStatus,
    TokenUsage,
    TriggerType,
)


def _make_summary(
    checkpoint_id: str = "ckpt-abc12345def67",
    issue_number: int = 738,
) -> CheckpointSummaryV2:
    return CheckpointSummaryV2(
        id=checkpoint_id,
        trigger_type=TriggerType.COMMIT,
        session_status=SessionStatus.COMPLETED,
        commit_sha="deadbeef12345678",
        branch="egg/test",
        session_id="sess-001",
        issue_number=issue_number,
        agent_type=AgentType.CODER,
        pipeline_phase="implement",
        total_tokens=50000,
        message_count=15,
        tool_call_count=8,
        files_touched_count=3,
        created_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
    )


def _make_index(summaries: list[CheckpointSummaryV2] | None = None) -> CheckpointIndexV2:
    if summaries is None:
        summaries = [_make_summary()]
    index = CheckpointIndexV2(
        checkpoints=summaries,
        last_updated=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
    )
    # Build secondary indices
    for s in summaries:
        if s.issue_number is not None:
            key = str(s.issue_number)
            if key not in index.by_issue:
                index.by_issue[key] = []
            index.by_issue[key].append(s.id)
    return index


def _make_checkpoint(checkpoint_id: str = "ckpt-abc12345def67") -> CheckpointV2:
    return CheckpointV2(
        id=checkpoint_id,
        trigger_type=TriggerType.COMMIT,
        session_status=SessionStatus.COMPLETED,
        commit_sha="deadbeef12345678",
        branch="egg/test",
        session_id="sess-001",
        issue_number=738,
        agent_type=AgentType.CODER,
        pipeline_phase="implement",
        session=SessionMetadata(
            session_id="sess-001",
            started_at=datetime(2025, 1, 15, 11, 0, 0, tzinfo=UTC),
            model="claude-sonnet-4-20250514",
        ),
        token_usage=TokenUsage(
            input_tokens=30000,
            output_tokens=15000,
            cache_read_tokens=5000,
            total_tokens=50000,
        ),
        created_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
        session_started_at=datetime(2025, 1, 15, 11, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def app():
    """Create the gateway Flask app for testing."""
    from gateway import app as gateway_app

    gateway_app.config["TESTING"] = True
    yield gateway_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_headers():
    """Provide valid session auth headers."""
    # Register a session so auth succeeds
    from session_manager import get_session_manager

    mgr = get_session_manager()
    token, _session = mgr.register_session("test-container", "127.0.0.1", "public")
    return {"Authorization": f"Bearer {token}"}


class TestCheckpointList:
    """Tests for GET /api/v1/checkpoints."""

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_list_returns_checkpoints(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.fetch_and_read_index.return_value = _make_index()
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints?issue=738",
            headers=auth_headers,
        )
        assert response.status_code == 200

        data = response.get_json()
        assert data["success"] is True
        checkpoints = data["data"]["checkpoints"]
        assert len(checkpoints) == 1
        assert checkpoints[0]["id"] == "ckpt-abc12345def67"

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_list_empty(self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.fetch_and_read_index.return_value = None
        mock_get_handler.return_value = mock_handler

        response = client.get("/api/v1/checkpoints", headers=auth_headers)
        assert response.status_code == 200

        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["checkpoints"] == []

    def test_list_requires_auth(self, client):
        response = client.get("/api/v1/checkpoints")
        assert response.status_code == 401

    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_list_no_repo_path(self, mock_resolve, client, auth_headers):
        mock_resolve.return_value = None

        response = client.get("/api/v1/checkpoints", headers=auth_headers)
        assert response.status_code == 400


class TestCheckpointRepoOverride:
    """Tests for checkpoint_repo query param override."""

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_list_uses_explicit_checkpoint_repo(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        # Auto-detection returns None, but query param should override
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.fetch_and_read_index.return_value = _make_index()
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints?issue=738&checkpoint_repo=org/checkpoints",
            headers=auth_headers,
        )
        assert response.status_code == 200

        # The explicit checkpoint_repo should be passed to the handler
        mock_handler.fetch_and_read_index.assert_called_once_with(
            "/repo", checkpoint_repo="org/checkpoints"
        )
        # Auto-detection should not be called when explicit value is valid
        mock_ckpt_repo.assert_not_called()

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_list_rejects_invalid_checkpoint_repo(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.fetch_and_read_index.return_value = _make_index()
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints?issue=738&checkpoint_repo=invalid-format",
            headers=auth_headers,
        )
        assert response.status_code == 200

        # Invalid format → falls back to auto-detection (which returns None)
        mock_handler.fetch_and_read_index.assert_called_once_with("/repo", checkpoint_repo=None)

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_show_uses_explicit_checkpoint_repo(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.ensure_ref.return_value = "abc123sha"
        mock_handler.read_checkpoint.return_value = _make_checkpoint()
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints/ckpt-abc12345def67?checkpoint_repo=org/checkpoints",
            headers=auth_headers,
        )
        assert response.status_code == 200

        mock_handler.ensure_ref.assert_called_once_with("/repo", checkpoint_repo="org/checkpoints")

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_cost_uses_explicit_checkpoint_repo(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.fetch_and_read_index.return_value = _make_index()
        mock_handler.ensure_ref.return_value = "abc123sha"
        mock_handler.read_checkpoint.return_value = _make_checkpoint()
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints/cost?issue=738&checkpoint_repo=org/checkpoints",
            headers=auth_headers,
        )
        assert response.status_code == 200

        # The explicit checkpoint_repo should be passed to fetch_and_read_index
        mock_handler.fetch_and_read_index.assert_called_once_with(
            "/repo", checkpoint_repo="org/checkpoints"
        )
        # Auto-detection should not be called when explicit value is valid
        mock_ckpt_repo.assert_not_called()


class TestCheckpointShow:
    """Tests for GET /api/v1/checkpoints/<id>."""

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_show_by_id(self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.ensure_ref.return_value = "abc123sha"
        mock_handler.read_checkpoint.return_value = _make_checkpoint()
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints/ckpt-abc12345def67",
            headers=auth_headers,
        )
        assert response.status_code == 200

        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["checkpoint"]["id"] == "ckpt-abc12345def67"

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_show_not_found(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.ensure_ref.return_value = "abc123sha"
        mock_handler.read_checkpoint.return_value = None
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints/ckpt-nonexistent",
            headers=auth_headers,
        )
        assert response.status_code == 404

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_show_no_branch(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.ensure_ref.return_value = None
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints/ckpt-abc12345def67",
            headers=auth_headers,
        )
        assert response.status_code == 404


class TestCheckpointCost:
    """Tests for GET /api/v1/checkpoints/cost."""

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_cost_returns_breakdown(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.ensure_ref.return_value = "abc123sha"
        mock_handler.fetch_and_read_index.return_value = _make_index()
        mock_handler.read_checkpoint.return_value = _make_checkpoint()
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints/cost?issue=738",
            headers=auth_headers,
        )
        assert response.status_code == 200

        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["checkpoint_count"] >= 1
        assert data["data"]["total_cost_usd"] > 0

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_cost_no_data(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.ensure_ref.return_value = None
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints/cost",
            headers=auth_headers,
        )
        assert response.status_code == 200

        data = response.get_json()
        assert data["data"]["checkpoint_count"] == 0


class TestCostRouteBeforeShow:
    """Verify /checkpoints/cost is not captured by /checkpoints/<id>."""

    @patch("gateway.get_checkpoint_handler")
    @patch("gateway._get_checkpoint_repo_for_path")
    @patch("gateway._resolve_repo_path_for_checkpoints")
    def test_cost_not_treated_as_id(
        self, mock_resolve, mock_ckpt_repo, mock_get_handler, client, auth_headers
    ):
        mock_resolve.return_value = "/repo"
        mock_ckpt_repo.return_value = None

        mock_handler = MagicMock()
        mock_handler.ensure_ref.return_value = None
        mock_get_handler.return_value = mock_handler

        response = client.get(
            "/api/v1/checkpoints/cost",
            headers=auth_headers,
        )
        assert response.status_code == 200

        data = response.get_json()
        # Should be a cost response, not a "not found" for id="cost"
        assert "checkpoint_count" in data.get("data", {})
