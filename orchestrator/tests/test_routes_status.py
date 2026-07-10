"""PR D (issue #3364, task-3-5): status-payload config subset (AC-D2).

``GET /api/v1/pipelines/<id>/status`` embeds an overseer-relevant subset
of ``PipelineConfig`` (``data["config"]``) so the sandbox-side overseer
monitor can read threshold knobs without a second round-trip. When the
``overseer_owns_host_detection`` calibration flag was removed, its
``getattr(cfg, "overseer_owns_host_detection", False)`` line in
``_routes_status.py`` had to go too — otherwise the payload would keep
advertising a config key with no backing field.

This pins the route-level contract end-to-end:

* the ``config`` subset is present and does NOT carry the removed key,
* the retained overseer threshold knobs are still surfaced with values.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from models import (
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import pipelines_bp

_REMOVED_KEY = "overseer_owns_host_detection"
_RETAINED_KEYS = (
    "overseer_stuck_phase_transition_seconds",
    "overseer_agent_stall_seconds",
    "overseer_silent_agent_threshold_seconds",
    "overseer_long_running_phase_seconds",
    "overseer_nack_unresolved_seconds",
)


@pytest.fixture
def app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app: Flask):
    """Lifecycle auth is injected by the orchestrator-level autouse
    ``_inject_lifecycle_auth`` fixture (see conftest)."""
    return app.test_client()


def _make_pipeline() -> Pipeline:
    return Pipeline(
        id="issue-3364-status",
        issue_number=3364,
        repo="owner/repo",
        branch="egg/issue-3364-status/work",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=PipelineConfig(),
    )


def _get_status_config(client) -> dict:
    """Drive the real status route and return ``data["config"]``.

    The pipeline resolution + PR/concurrent enrichers are patched to
    keep the config-subset block deterministic; nothing here mocks the
    subset itself, so the assertion exercises the production code path.
    """
    pipeline = _make_pipeline()
    with (
        patch("routes.pipelines.get_repo_path", return_value="/tmp/repo"),
        patch(
            "routes.pipelines._resolve_pipeline",
            return_value=(MagicMock(), pipeline),
        ),
        patch("routes.pipelines._get_pr_info", return_value=(None, None)),
        patch("routes.pipelines._get_concurrent_status", return_value=None),
    ):
        resp = client.get("/api/v1/pipelines/issue-3364-status/status")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is True
    return body["data"]["config"]


def test_status_config_subset_omits_removed_flag(client) -> None:
    cfg = _get_status_config(client)
    assert _REMOVED_KEY not in cfg


def test_status_config_subset_keeps_retained_knobs(client) -> None:
    cfg = _get_status_config(client)
    for key in _RETAINED_KEYS:
        assert key in cfg, key
        assert isinstance(cfg[key], int), key
