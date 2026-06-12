"""Tests for the live pipeline config-update endpoint (#3174).

``PATCH /api/v1/pipelines/<id>/config`` is the scoped operator surface
for changing a running pipeline's ``agent_models`` override (wrapped by
the ``update_pipeline_config`` MCP tool). Covers:

- the key allowlist (``agent_models`` only),
- per-role merge semantics (set / null-clears / absent-keeps),
- role-key validation against ``MODEL_OVERRIDE_ROLES`` and value
  validation (non-empty strings or null),
- error mapping (404 unknown pipeline, 400 validation, 401 unauth).
"""

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import Pipeline, PipelineConfig, PipelineStatus
from routes.pipelines import pipelines_bp
from state_store import PipelineNotFoundError


@pytest.fixture
def app():
    """Create a test Flask app with the pipelines blueprint."""
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client. Lifecycle auth is injected by the
    orchestrator-level ``_inject_lifecycle_auth`` autouse fixture."""
    return app.test_client()


def _make_pipeline(agent_models: dict[str, str] | None = None) -> Pipeline:
    return Pipeline(
        id="issue-77",
        repo="test/repo",
        issue_number=77,
        branch="egg/issue-77",
        status=PipelineStatus.RUNNING,
        config=PipelineConfig(agent_models=agent_models or {}),
    )


def _mock_store(pipeline: Pipeline) -> MagicMock:
    """Store whose ``update_pipeline`` reflects the merged map back,
    mirroring the real load-modify-save behavior."""
    store = MagicMock()
    store.load_pipeline.return_value = pipeline

    def _update(pipeline_id, updates):
        assert set(updates) == {"config.agent_models"}, updates
        return _make_pipeline(updates["config.agent_models"])

    store.update_pipeline.side_effect = _update
    return store


@patch("routes.pipelines.get_repo_path")
@patch("routes.pipelines._resolve_pipeline")
class TestUpdatePipelineConfigMerge:
    """Per-role merge semantics."""

    def test_set_preserves_unnamed_roles(self, mock_resolve, mock_repo, client):
        pipeline = _make_pipeline({"coder": "opus"})
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"agent_models": {"tester": "deepseek-v4-pro"}},
        )

        assert response.status_code == 200, response.data
        data = response.get_json()["data"]
        # Unnamed role keeps its override; named role is set.
        assert data["agent_models"] == {"coder": "opus", "tester": "deepseek-v4-pro"}
        assert data["updated_roles"] == {"tester": "deepseek-v4-pro"}
        assert data["cleared_roles"] == []
        store.update_pipeline.assert_called_once_with(
            "issue-77",
            {"config.agent_models": {"coder": "opus", "tester": "deepseek-v4-pro"}},
        )

    def test_null_clears_role(self, mock_resolve, mock_repo, client):
        pipeline = _make_pipeline({"coder": "opus", "tester": "qwen3-max"})
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"agent_models": {"tester": None}},
        )

        assert response.status_code == 200, response.data
        data = response.get_json()["data"]
        assert data["agent_models"] == {"coder": "opus"}
        assert data["updated_roles"] == {}
        assert data["cleared_roles"] == ["tester"]

    def test_clearing_absent_role_is_noop(self, mock_resolve, mock_repo, client):
        pipeline = _make_pipeline({"coder": "opus"})
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"agent_models": {"tester": None}},
        )

        assert response.status_code == 200, response.data
        data = response.get_json()["data"]
        assert data["agent_models"] == {"coder": "opus"}
        # Not reported as cleared — there was nothing to clear.
        assert data["cleared_roles"] == []

    def test_model_value_whitespace_stripped(self, mock_resolve, mock_repo, client):
        pipeline = _make_pipeline()
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"agent_models": {"coder": "  deepseek-v4-pro  "}},
        )

        assert response.status_code == 200, response.data
        data = response.get_json()["data"]
        assert data["agent_models"] == {"coder": "deepseek-v4-pro"}


@patch("routes.pipelines.get_repo_path")
@patch("routes.pipelines._resolve_pipeline")
class TestUpdatePipelineConfigValidation:
    """Request validation runs before any store write."""

    def _assert_no_write(self, mock_resolve):
        if mock_resolve.return_value and isinstance(mock_resolve.return_value, tuple):
            store = mock_resolve.return_value[0]
            store.update_pipeline.assert_not_called()

    def test_invalid_role_key_400(self, mock_resolve, mock_repo, client):
        pipeline = _make_pipeline()
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"agent_models": {"overseer": "opus"}},
        )

        assert response.status_code == 400, response.data
        message = response.get_json()["message"]
        # Actionable error: names the bad key and lists the honored roles.
        assert "overseer" in message
        assert "coder" in message
        self._assert_no_write(mock_resolve)

    @pytest.mark.parametrize("bad_value", ["", "   ", 42, ["opus"]])
    def test_invalid_model_value_400(self, mock_resolve, mock_repo, client, bad_value):
        pipeline = _make_pipeline()
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"agent_models": {"coder": bad_value}},
        )

        assert response.status_code == 400, response.data
        assert "coder" in response.get_json()["message"]
        self._assert_no_write(mock_resolve)

    def test_unsupported_config_key_400(self, mock_resolve, mock_repo, client):
        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"hitl_gates": False, "agent_models": {"coder": "opus"}},
        )

        assert response.status_code == 400, response.data
        message = response.get_json()["message"]
        assert "hitl_gates" in message
        assert "agent_models" in message

    @pytest.mark.parametrize("body", [{}, {"agent_models": {}}, {"agent_models": "coder=opus"}])
    def test_missing_or_empty_agent_models_400(self, mock_resolve, mock_repo, client, body):
        response = client.patch("/api/v1/pipelines/issue-77/config", json=body)
        assert response.status_code == 400, response.data

    def test_non_object_body_400(self, mock_resolve, mock_repo, client):
        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            data="[1, 2]",
            content_type="application/json",
        )
        assert response.status_code == 400, response.data


class TestUpdatePipelineConfigErrors:
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines._resolve_pipeline")
    def test_pipeline_not_found_404(self, mock_resolve, mock_repo, client):
        mock_resolve.side_effect = PipelineNotFoundError("issue-77")

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"agent_models": {"coder": "opus"}},
        )

        assert response.status_code == 404, response.data

    def test_requires_lifecycle_auth(self, client):
        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"agent_models": {"coder": "opus"}},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401, response.data
