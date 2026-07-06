"""Tests for the live pipeline config-update endpoint (#3174, #3490).

``PATCH /api/v1/pipelines/<id>/config`` is the scoped operator surface
for changing a running pipeline's ``agent_models`` override and its
``consensus_timeout_minutes*`` overrides (wrapped by the
``update_pipeline_config`` MCP tool). Covers:

- the key allowlist (``agent_models`` + ``consensus_timeout_minutes*``),
- per-role merge semantics (set / null-clears / absent-keeps),
- role-key validation against ``MODEL_OVERRIDE_ROLES`` and value
  validation (non-empty strings or null),
- consensus-timeout value validation (int >= 1 or null) and
  set / clear / combined-update semantics (#3490),
- error mapping (404 unknown pipeline, 400 validation, 401 unauth,
  409 terminal-state mutation).
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


def _make_pipeline(
    agent_models: dict[str, str] | None = None,
    status: PipelineStatus = PipelineStatus.RUNNING,
    **config_kwargs,
) -> Pipeline:
    return Pipeline(
        id="issue-77",
        repo="test/repo",
        issue_number=77,
        branch="egg/issue-77",
        status=status,
        config=PipelineConfig(agent_models=agent_models or {}, **config_kwargs),
    )


def _mock_store(pipeline: Pipeline) -> MagicMock:
    """Store whose ``update_pipeline`` applies the dotted-key updates,
    mirroring the real load-modify-save behavior."""
    store = MagicMock()
    store.load_pipeline.return_value = pipeline

    def _update(pipeline_id, updates):
        data = pipeline.model_dump()
        for key, value in updates.items():
            target = data
            parts = key.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
        return Pipeline.model_validate(data)

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
class TestUpdatePipelineConfigConsensusTimeouts:
    """Live consensus-timeout mutation (#3490)."""

    def test_set_implement_timeout(self, mock_resolve, mock_repo, client):
        pipeline = _make_pipeline()
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"consensus_timeout_minutes_implement": 480},
        )

        assert response.status_code == 200, response.data
        data = response.get_json()["data"]
        assert data["consensus_timeouts"]["consensus_timeout_minutes_implement"] == 480
        assert data["updated_timeouts"] == {"consensus_timeout_minutes_implement": 480}
        store.update_pipeline.assert_called_once_with(
            "issue-77",
            {"config.consensus_timeout_minutes_implement": 480},
        )

    def test_set_legacy_global_timeout(self, mock_resolve, mock_repo, client):
        pipeline = _make_pipeline()
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"consensus_timeout_minutes": 240},
        )

        assert response.status_code == 200, response.data
        data = response.get_json()["data"]
        assert data["consensus_timeouts"]["consensus_timeout_minutes"] == 240

    def test_null_clears_timeout_override(self, mock_resolve, mock_repo, client):
        pipeline = _make_pipeline(consensus_timeout_minutes_implement=120)
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"consensus_timeout_minutes_implement": None},
        )

        assert response.status_code == 200, response.data
        data = response.get_json()["data"]
        assert data["consensus_timeouts"]["consensus_timeout_minutes_implement"] is None
        assert data["updated_timeouts"] == {"consensus_timeout_minutes_implement": None}

    def test_combined_agent_models_and_timeout(self, mock_resolve, mock_repo, client):
        pipeline = _make_pipeline({"coder": "opus"})
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={
                "agent_models": {"tester": "deepseek-v4-pro"},
                "consensus_timeout_minutes_implement": 480,
            },
        )

        assert response.status_code == 200, response.data
        data = response.get_json()["data"]
        assert data["agent_models"] == {"coder": "opus", "tester": "deepseek-v4-pro"}
        assert data["consensus_timeouts"]["consensus_timeout_minutes_implement"] == 480
        store.update_pipeline.assert_called_once_with(
            "issue-77",
            {
                "config.agent_models": {"coder": "opus", "tester": "deepseek-v4-pro"},
                "config.consensus_timeout_minutes_implement": 480,
            },
        )

    @pytest.mark.parametrize("bad_value", [0, -5, "120", 12.5, True, False, [120]])
    def test_invalid_timeout_value_400(self, mock_resolve, mock_repo, client, bad_value):
        pipeline = _make_pipeline()
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"consensus_timeout_minutes_implement": bad_value},
        )

        assert response.status_code == 400, response.data
        assert "consensus_timeout_minutes_implement" in response.get_json()["message"]
        store.update_pipeline.assert_not_called()


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


@patch("routes.pipelines.get_repo_path")
@patch("routes.pipelines._resolve_pipeline")
class TestUpdatePipelineConfigTerminalState:
    """Terminal pipelines reject config mutations with 409 (#3174 review).

    Once a pipeline is COMPLETE / FAILED / CANCELLED no future spawn consumes
    ``agent_models``, so the merge would be a silent no-op. A 409 gives the
    operator a clear signal instead.
    """

    @pytest.mark.parametrize(
        "status",
        [PipelineStatus.COMPLETE, PipelineStatus.FAILED, PipelineStatus.CANCELLED],
    )
    def test_terminal_state_409_and_no_write(self, mock_resolve, mock_repo, client, status):
        pipeline = _make_pipeline({"coder": "opus"}, status=status)
        store = _mock_store(pipeline)
        mock_resolve.return_value = (store, pipeline)

        response = client.patch(
            "/api/v1/pipelines/issue-77/config",
            json={"agent_models": {"tester": "deepseek-v4-pro"}},
        )

        assert response.status_code == 409, response.data
        assert status.value in response.get_json()["message"]
        store.update_pipeline.assert_not_called()
