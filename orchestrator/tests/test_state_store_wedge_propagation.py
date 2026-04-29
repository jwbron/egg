"""
Regression tests for #2167 — state-store wedges must surface as 500
with a useful error message, not as 404 "Pipeline not found".

Three layers covered:

1. ``_resolve_pipeline`` no longer swallows ``GitOperationError`` as
   ``PipelineNotFoundError`` (the original 500→404 trap).
2. The Flask app-level error handler renders ``StateStoreError``
   subclasses with the actual error string, not a generic
   "Internal server error".
3. ``GET /api/v1/health`` flips ``status`` to ``degraded`` (and
   surfaces the error in ``components.state_store``) when the
   state-store probe fails.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

# Add orchestrator + shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


# ---------------------------------------------------------------------------
# 1. _resolve_pipeline error propagation
# ---------------------------------------------------------------------------


class TestResolvePipelinePropagatesGitErrors:
    """``_resolve_pipeline`` must let ``GitOperationError`` propagate.

    The previous ``except (PipelineNotFoundError, StateStoreError)``
    clause caught every state-store wedge and re-raised it as
    ``PipelineNotFoundError``, which routes returned as 404.  The fix
    narrows the catch to ``PipelineNotFoundError`` only; infrastructure
    failures must surface so routes return 500.
    """

    def test_propagates_git_operation_error(self, tmp_path):
        from routes.pipelines import _resolve_pipeline
        from state_store import GitOperationError

        # Pretend we're in multi-repo mode with one repo discovered.
        repo = tmp_path / "egg"
        repo.mkdir()
        (repo / ".git").mkdir()

        wedged_store = MagicMock()
        wedged_store.load_pipeline.side_effect = GitOperationError(
            "Git command failed: fatal: 'egg/pipeline-state' "
            "is already used by worktree at '/tmp/old'"
        )

        with (
            patch("state_store.discover_repo_paths", return_value=[repo]),
            patch("routes.pipelines.get_state_store", return_value=wedged_store),
        ):
            with pytest.raises(GitOperationError, match="already used by worktree"):
                _resolve_pipeline("issue-1924", tmp_path)

    def test_pipeline_not_found_still_works(self, tmp_path):
        """``PipelineNotFoundError`` is still translated correctly: the
        loop exhausts and re-raises the canonical "not found" error."""
        from routes.pipelines import _resolve_pipeline
        from state_store import PipelineNotFoundError

        repo = tmp_path / "egg"
        repo.mkdir()
        (repo / ".git").mkdir()

        store = MagicMock()
        store.load_pipeline.side_effect = PipelineNotFoundError("nope")

        with (
            patch("state_store.discover_repo_paths", return_value=[repo]),
            patch("routes.pipelines.get_state_store", return_value=store),
        ):
            with pytest.raises(PipelineNotFoundError, match="issue-1924"):
                _resolve_pipeline("issue-1924", tmp_path)


# ---------------------------------------------------------------------------
# 2. Flask app error handler renders StateStoreError → 500 with detail
# ---------------------------------------------------------------------------


class TestStateStoreErrorHandlerSurfacesDetail:
    """The app-level error handler must report the actual error string
    for ``StateStoreError`` subclasses, not the generic 500 fallback.
    Operators were debugging blind because every wedge looked the same
    on the wire."""

    @pytest.fixture
    def client(self):
        # Import here so the @errorhandler registration in api.py runs.
        from api import app

        app.config["TESTING"] = True
        return app.test_client()

    def test_git_operation_error_surfaces_message_and_500(self, client):
        """Mock _resolve_pipeline to raise GitOperationError; the
        ``GET /api/v1/pipelines/<id>`` route must return 500 with the
        actual error message visible in the response body."""
        from state_store import GitOperationError

        with patch(
            "routes.pipelines._resolve_pipeline",
            side_effect=GitOperationError(
                "Git command failed: fatal: 'egg/pipeline-state' "
                "is already used by worktree at '/tmp/stale'"
            ),
        ):
            response = client.get("/api/v1/pipelines/issue-1924")

        assert response.status_code == 500
        body = response.get_json()
        assert body["success"] is False
        # Must include the original git error so operators can diagnose
        # without grepping logs (#2167 ask).
        assert "is already used by worktree" in body["message"]
        assert body.get("error_type") == "GitOperationError"


# ---------------------------------------------------------------------------
# 3. /api/v1/health flips to degraded on state-store probe failure
# ---------------------------------------------------------------------------


class TestHealthEndpointReflectsStateStoreFailure:
    """``GET /api/v1/health`` must report ``degraded`` when the state-store
    probe fails — the original report had ``check_health`` reporting
    ``healthy=true`` while every state load was throwing 500."""

    @pytest.fixture(autouse=True)
    def _reset_health_tracker(self):
        """``routes.health._health_tracker`` is a module-level singleton.
        These tests record both healthy and unhealthy observations; if
        we don't restore, downstream tests that assert tracker
        invariants (e.g. ``healthy_since == process_start_time``) flake
        based on execution order."""
        import routes.health as health_module
        from egg_health import HealthTracker

        original = health_module._health_tracker
        health_module._health_tracker = HealthTracker()
        try:
            yield
        finally:
            health_module._health_tracker = original

    @pytest.fixture(autouse=True)
    def _reset_state_store_probe(self):
        """``state_store_probe._PROBE`` is a module-level singleton with
        cached observations. Reset between tests so each test starts
        with an empty cache and isn't affected by sibling tests."""
        from state_store_probe import reset_state_store_probe_for_test

        reset_state_store_probe_for_test()
        try:
            yield
        finally:
            reset_state_store_probe_for_test()

    @pytest.fixture
    def client(self):
        from routes.health import health_bp

        app = Flask(__name__)
        app.register_blueprint(health_bp)
        app.config["TESTING"] = True
        return app.test_client()

    def test_healthy_when_probe_succeeds(self, client, monkeypatch):
        """Prime the cached state-store probe with a healthy observation
        and confirm ``/api/v1/health`` reports it. Since #2191 the
        endpoint reads the cache instead of running the probe inline,
        so we drive the cache via ``probe_now()`` after patching the
        underlying probe function. ``EGG_REPO_PATH`` must be set or
        ``probe_now()`` short-circuits to ``"probe-skipped"`` before
        the patch can intercept."""
        from state_store_probe import get_state_store_probe

        monkeypatch.setenv("EGG_REPO_PATH", "/sentinel/repo/path")
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok", {"/sentinel/repo/path": {"status": "ok"}}),
        ):
            get_state_store_probe().probe_now()
            response = client.get("/api/v1/health")

        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "healthy"
        assert body["components"]["state_store"] == {"/sentinel/repo/path": {"status": "ok"}}
        assert body["components"]["state_store_summary"] == "ok"

    def test_degraded_when_probe_fails(self, client, monkeypatch):
        """The exact wedge from #2167: the probe surfaces the
        worktree-contention error and we flip status."""
        from state_store_probe import get_state_store_probe

        monkeypatch.setenv("EGG_REPO_PATH", "/sentinel/repo/path")
        err_msg = (
            "GitOperationError: Git command failed: fatal: "
            "'egg/pipeline-state' is already used by worktree at "
            "'/home/egg/.egg-state/pipeline-worktree'"
        )
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(
                False,
                "1/1 repos wedged: /sentinel/repo/path",
                {
                    "/sentinel/repo/path": {
                        "status": "error",
                        "error": err_msg,
                    }
                },
            ),
        ):
            get_state_store_probe().probe_now()
            response = client.get("/api/v1/health")

        # HTTP status stays 200 so kubernetes probes don't flap, but the
        # body MUST report degraded so MCP check_health surfaces it.
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "degraded"
        repo_entry = body["components"]["state_store"]["/sentinel/repo/path"]
        assert repo_entry["status"] == "error"
        assert "is already used by worktree" in repo_entry["error"]

    def test_probe_skipped_when_no_repo_configured(self):
        """Health probe must not flap on configuration issues unrelated
        to the wedge we're trying to detect.  When ``get_repo_path``
        returns a path that has no git repo and no child repos, the
        probe reports skipped and health stays healthy."""
        from routes.health import _probe_state_store

        with patch("routes.get_repo_path", return_value=Path("/definitely/not/a/repo")):
            healthy, status = _probe_state_store()

        assert healthy is True
        assert "probe-skipped" in status

    def test_probe_skipped_when_request_context_missing(self):
        """Calling the probe outside a Flask request context must be
        treated as a configuration issue, not a wedge."""
        from routes.health import _probe_state_store

        # No Flask app pushed → get_repo_path raises RuntimeError when
        # it touches `request`.  Probe should swallow and report skipped.
        healthy, status = _probe_state_store()

        assert healthy is True
        assert "probe-skipped" in status
