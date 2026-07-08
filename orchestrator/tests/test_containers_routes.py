"""Tests for container API routes (orchestrator/routes/containers.py).

The container-spawner / monitor / backend integrations are covered by
``test_container_spawner*.py`` and ``test_container_backend.py``. This
file covers route-level input validation that runs before the backend
is touched — specifically the #2656 sweep landed in PR #2645.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


@pytest.fixture
def app():
    """Create a test Flask app with the containers blueprint."""
    from flask import Flask
    from routes.containers import containers_bp

    app = Flask(__name__)
    app.register_blueprint(containers_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client. Lifecycle auth is injected by the
    orchestrator-level ``_inject_lifecycle_auth`` autouse fixture."""
    return app.test_client()


class TestNonObjectJsonBodyReturns400:
    """Sweep of the #2656 fix into the containers routes (PR #2645).

    ``spawn_container`` and ``stop_container`` previously did
    ``data = request.get_json() or {}`` then ``data.get(...)``. When the
    body was syntactically-valid JSON but not an object (list / scalar),
    ``.get`` raised ``AttributeError`` and the handler's generic
    exception mapper returned 500. Both handlers now reject non-dict
    bodies with ``400 Request body must be a JSON object`` before any
    ``.get`` call, mirroring the original decisions-route fix.

    Both routes sit behind ``@require_lifecycle_secret``; the
    autouse ``_inject_lifecycle_auth`` fixture in
    ``orchestrator/tests/conftest.py`` injects the bearer token. The
    backend (``_get_backend``) is patched per-test so the body-validation
    rejection is never racing a real Docker / Kubernetes call.
    """

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true", "[]", "0", "false", '""'],
        ids=[
            "array",
            "string",
            "number",
            "bool",
            "empty-array",
            "zero",
            "false",
            "empty-string",
        ],
    )
    def test_spawn_non_object_json_body_returns_400(self, client, raw_body):
        """POST /pipelines/<id>/spawn with non-object JSON body → 400."""
        with patch("routes.containers._get_backend") as mock_get_backend:
            response = client.post(
                "/api/v1/pipelines/test-pipeline/spawn",
                content_type="application/json",
                data=raw_body,
            )
        assert response.status_code == 400, response.data
        body = response.get_json()
        assert body["success"] is False
        assert "json object" in body["message"].lower(), body
        # Body validation must run before backend dispatch — the
        # backend should never have been asked for a handle.
        mock_get_backend.assert_not_called()

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true", "[]", "0", "false", '""'],
        ids=[
            "array",
            "string",
            "number",
            "bool",
            "empty-array",
            "zero",
            "false",
            "empty-string",
        ],
    )
    def test_stop_non_object_json_body_returns_400(self, client, raw_body):
        """POST /pipelines/<id>/containers/<cid>/stop with non-object JSON body → 400."""
        with patch("routes.containers._get_backend") as mock_get_backend:
            response = client.post(
                "/api/v1/pipelines/test-pipeline/containers/abc123/stop",
                content_type="application/json",
                data=raw_body,
            )
        assert response.status_code == 400, response.data
        body = response.get_json()
        assert body["success"] is False
        assert "json object" in body["message"].lower(), body
        mock_get_backend.assert_not_called()


class TestListContainersResolvedModel:
    """#3174: ``list_containers`` joins the per-spawn ``resolved_model``
    from persisted pipeline state onto the live container rows."""

    def _container(self, container_id: str):
        from models import AgentRole, ContainerInfo, ContainerStatus

        return ContainerInfo(
            container_id=container_id,
            container_name=f"egg-test-{container_id}",
            status=ContainerStatus.RUNNING,
            agent_role=AgentRole.CODER,
        )

    def test_resolved_model_joined_onto_rows(self, client):
        from unittest.mock import MagicMock

        backend = MagicMock()
        backend.list_containers.return_value = [
            self._container("container-aaa"),
            self._container("container-bbb"),
        ]
        with (
            patch("routes.containers._get_backend", return_value=backend),
            patch(
                "routes.containers._resolved_models_by_container",
                return_value={"container-aaa": "deepseek-v4-pro[1m]"},
            ),
        ):
            response = client.get("/api/v1/pipelines/issue-77/containers")

        assert response.status_code == 200, response.data
        rows = {c["container_id"]: c for c in response.get_json()["data"]["containers"]}
        assert rows["container-aaa"]["resolved_model"] == "deepseek-v4-pro[1m]"
        # Pre-#3174 records (no persisted decision) degrade to null.
        assert rows["container-bbb"]["resolved_model"] is None

    def test_join_helper_reads_pipeline_state(self):
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from models import (
            AgentExecution,
            AgentRole,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
        )
        from routes.containers import _resolved_models_by_container

        pipeline = Pipeline(
            id="issue-77",
            issue_number=77,
            repo="test/repo",
            branch="egg/issue-77",
        )
        pipeline.phases = {
            "implement": PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                agents=[
                    AgentExecution(
                        role=AgentRole.CODER,
                        container_id="container-aaa",
                        resolved_model="qwen3-max[1m]",
                    ),
                    # No recorded decision — must be absent from the map,
                    # not present as None.
                    AgentExecution(role=AgentRole.TESTER, container_id="container-bbb"),
                ],
            ),
        }
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.get_repo_path", return_value=Path("/repos")),
            patch("state_store.discover_repo_paths", return_value=[Path("/repos/test")]),
            patch("state_store.get_state_store", return_value=store),
        ):
            mapping = _resolved_models_by_container("issue-77")

        assert mapping == {"container-aaa": "qwen3-max[1m]"}

    def test_join_helper_degrades_to_empty_on_failure(self):
        """Listing containers must not depend on state-store health."""
        from routes.containers import _resolved_models_by_container

        with patch("routes.get_repo_path", side_effect=RuntimeError("no repo path")):
            assert _resolved_models_by_container("issue-77") == {}


class TestPersistedAgentLogFallback:
    """Post-reap log capture read paths (#3547).

    One-shot agent pods are reaped minutes after exit; ``remove_agent_job``
    snapshots their logs into the agent-log store, and the logs route falls
    back to that capture instead of returning 404.
    """

    @pytest.fixture(autouse=True)
    def _fakeredis_store(self):
        import agent_log_store
        import fakeredis
        from agent_log_store import AgentLogStore

        agent_log_store.set_agent_log_store(AgentLogStore(fakeredis.FakeRedis()))
        yield
        agent_log_store.reset_agent_log_store()

    def _persist(self, pipeline_id="p-1", job_name="egg-agent-p-1-coder-abc", **kwargs):
        from agent_log_store import get_agent_log_store

        defaults = {"logs": "agent stdout tail", "agent_role": "coder", "exit_code": 1}
        defaults.update(kwargs)
        get_agent_log_store().put(pipeline_id, job_name, **defaults)

    def test_logs_route_falls_back_to_capture(self, client):
        from kubernetes_client import PodNotFoundError

        self._persist()
        with patch("routes.containers._get_backend") as mock_get_backend:
            mock_get_backend.return_value.get_container_logs.side_effect = PodNotFoundError(
                "pod gone"
            )
            response = client.get("/api/v1/pipelines/p-1/containers/egg-agent-p-1-coder-abc/logs")
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["logs"] == "agent stdout tail"
        assert data["source"] == "persisted"
        assert data["exit_code"] == 1
        assert data["agent_role"] == "coder"

    def test_logs_route_404_when_no_capture(self, client):
        from kubernetes_client import PodNotFoundError

        with patch("routes.containers._get_backend") as mock_get_backend:
            mock_get_backend.return_value.get_container_logs.side_effect = PodNotFoundError(
                "pod gone"
            )
            response = client.get("/api/v1/pipelines/p-1/containers/unknown/logs")
        assert response.status_code == 404

    def test_live_logs_do_not_touch_store(self, client):
        self._persist(logs="stale capture")
        with patch("routes.containers._get_backend") as mock_get_backend:
            mock_get_backend.return_value.get_container_logs.return_value = "live logs"
            response = client.get("/api/v1/pipelines/p-1/containers/egg-agent-p-1-coder-abc/logs")
        data = response.get_json()["data"]
        assert data["logs"] == "live logs"
        assert "source" not in data

    def test_agent_logs_index(self, client):
        self._persist(job_name="job-a", captured_at="2026-07-07T01:00:00+00:00")
        self._persist(job_name="job-b", captured_at="2026-07-07T02:00:00+00:00")
        response = client.get("/api/v1/pipelines/p-1/agent-logs")
        assert response.status_code == 200
        records = response.get_json()["data"]["records"]
        assert [r["job_name"] for r in records] == ["job-b", "job-a"]
        assert all("logs" not in r for r in records)

    def test_agent_logs_get_by_job_name(self, client):
        self._persist(job_name="job-a")
        response = client.get("/api/v1/pipelines/p-1/agent-logs/job-a")
        assert response.status_code == 200
        assert response.get_json()["data"]["logs"] == "agent stdout tail"

    def test_agent_logs_get_miss_404(self, client):
        response = client.get("/api/v1/pipelines/p-1/agent-logs/unknown")
        assert response.status_code == 404
