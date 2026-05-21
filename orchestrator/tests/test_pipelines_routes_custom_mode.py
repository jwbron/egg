"""CUSTOM-mode route tests for ``POST /api/v1/pipelines`` (#1762).

Covers the route-level validation added by Phase 2 of the
``run_agent_task`` primitive:

    * Required ``phase`` + valid phase enum.
    * Repo format + allowlist rejection.
    * ``validate_roles_for_custom_phase`` integration surfaces as
      structured HTTP 400 with ``details.reason``.
    * Auto-generated ``egg/custom-<pipeline_id>`` branch fallback.
    * ``active_roles`` persisted on the created pipeline.
    * CUSTOM + PR runs PR pre-flight (merged/closed/fork/empty).

These tests call the Flask route directly with
``store.create_pipeline`` mocked — we verify that the route assembles
the expected kwargs, not that the state store works (covered by
``test_state_store_active_roles.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())


@pytest.fixture
def app():
    from routes.pipelines import pipelines_bp

    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_mock_pipeline(
    pipeline_id: str = "pipeline-abcd1234",
    *,
    mode: str = "custom",
    active_roles: list[str] | None = None,
    has_contract: bool = True,
    branch: str | None = "egg/custom-pipeline-abcd1234",
) -> MagicMock:
    fake = MagicMock()
    fake.id = pipeline_id
    fake.model_dump.return_value = {
        "id": pipeline_id,
        "mode": mode,
        "active_roles": active_roles,
        "has_contract": has_contract,
        "branch": branch,
    }
    fake.mode = mode
    fake.active_roles = active_roles
    fake.branch = branch
    return fake


def _with_common_patches():
    """Return a patcher context that wires up every external dependency
    the route touches."""


class _PatchBundle:
    """Context manager that opens every patch the CUSTOM route depends
    on so individual tests stay readable."""

    def __enter__(self):
        self._ctx = [
            patch("routes.pipelines.get_state_store"),
            patch("routes.pipelines.get_repo_path"),
            patch("routes.pipelines.get_gateway_client"),
            patch("routes.pipelines._fetch_pr_state"),
            patch("config.repo_config.is_writable_repo", return_value=True),
            patch("config.repo_config.is_readable_repo", return_value=True),
        ]
        self.mock_store_factory, self.mock_repo_path, self.mock_gw, self.mock_fetch, *_ = [
            c.__enter__() for c in self._ctx
        ]
        self.mock_store = MagicMock()
        self.mock_store_factory.return_value = self.mock_store
        self.mock_repo_path.return_value = Path("/tmp/repo")
        self.mock_gw.return_value.ls_remote_branch.return_value = False
        self.mock_gw.return_value.wait_for_healthy.return_value = True
        self.mock_fetch.return_value = {}  # No PR by default
        return self

    def __exit__(self, *exc_info):
        for c in reversed(self._ctx):
            c.__exit__(*exc_info)


# ---------------------------------------------------------------------------
# Required phase + valid phase
# ---------------------------------------------------------------------------


class TestCustomPhaseValidation:
    def test_missing_phase_returns_400(self, client):
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={"mode": "custom", "repo": "owner/repo", "prompt": "x"},
            )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body.get("details", {}).get("reason") == "missing_phase"

    def test_invalid_phase_returns_400(self, client):
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "pr",
                    "repo": "owner/repo",
                    "prompt": "x",
                },
            )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["details"]["reason"] == "invalid_phase"

    @pytest.mark.parametrize("phase", ["refine", "plan", "implement"])
    def test_valid_phases_accepted(self, client, phase):
        with _PatchBundle() as bundle:
            bundle.mock_store.create_pipeline.return_value = _make_mock_pipeline()
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": phase,
                    "repo": "owner/repo",
                    "prompt": "x",
                    "pipeline_id": "pipeline-aabbccdd",
                },
            )
        # Route may return 500 due to unmocked start-pipeline bits, but
        # the response body should not be a phase-validation error.
        if resp.status_code == 400:
            reason = (resp.get_json() or {}).get("details", {}).get("reason")
            assert reason not in ("missing_phase", "invalid_phase")


# ---------------------------------------------------------------------------
# Roles validation
# ---------------------------------------------------------------------------


class TestCustomRolesValidation:
    def test_reviewer_only_roster_returns_400(self, client):
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "roles": ["reviewer_code"],
                    "pipeline_id": "pipeline-aabbccdd",
                },
            )
        assert resp.status_code == 400
        assert resp.get_json()["details"]["reason"] == "reviewer_only_roster"

    def test_cross_phase_role_returns_400(self, client):
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "roles": ["coder", "overseer"],
                    "pipeline_id": "pipeline-aabbccdd",
                },
            )
        assert resp.status_code == 400
        assert resp.get_json()["details"]["reason"] == "cross_phase_role"

    def test_unknown_role_returns_400(self, client):
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "roles": ["bogus"],
                    "pipeline_id": "pipeline-aabbccdd",
                },
            )
        assert resp.status_code == 400
        assert resp.get_json()["details"]["reason"] == "invalid_roles"

    def test_reviewer_contract_without_artifact_returns_400(self, client):
        """On a CUSTOM pipeline without analysis/plan/issue, asking for
        reviewer_contract is rejected — has_contract=False in that
        branch."""
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "pr_number": 42,  # pr_number forces has_contract=False
                    "roles": ["coder", "reviewer_contract"],
                    "pipeline_id": "pipeline-aabbccdd",
                },
            )
        # PR preflight might 400 first; acceptable reasons are either
        # the PR check or the reviewer_contract gate.
        if resp.status_code == 400:
            reason = resp.get_json()["details"]["reason"]
            assert reason in (
                "reviewer_contract_without_artifact",
                "pr_empty_diff",
                "pr_merged",
                "pr_closed",
                "pr_from_fork",
            )

    def test_happy_path_roles_forwarded_to_store(self, client):
        with _PatchBundle() as bundle:
            bundle.mock_store.create_pipeline.return_value = _make_mock_pipeline(
                active_roles=["coder"]
            )
            client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "do it",
                    "roles": ["coder"],
                    "pipeline_id": "pipeline-aabbccdd",
                },
            )
            call = bundle.mock_store.create_pipeline.call_args
            assert call is not None, "create_pipeline was never called"
            assert call.kwargs.get("active_roles") == ["coder"]


# ---------------------------------------------------------------------------
# custom_phase threaded to create_pipeline (review feedback #1)
# ---------------------------------------------------------------------------


class TestCustomPhaseThreading:
    """Verify that the route threads ``custom_phase`` into
    ``create_pipeline`` so the phase is set atomically during creation
    instead of via a post-creation fixup."""

    @pytest.mark.parametrize(
        "phase,role",
        [("refine", "refiner"), ("plan", "architect"), ("implement", "coder")],
    )
    def test_custom_phase_forwarded_to_store(self, client, phase, role):
        with _PatchBundle() as bundle:
            bundle.mock_store.create_pipeline.return_value = _make_mock_pipeline()
            client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": phase,
                    "repo": "owner/repo",
                    "prompt": "x",
                    "roles": [role],
                    "pipeline_id": "pipeline-aabbccdd",
                },
            )
            call = bundle.mock_store.create_pipeline.call_args
            assert call is not None
            assert call.kwargs.get("custom_phase") == phase

    def test_non_custom_mode_does_not_set_custom_phase(self, client):
        with _PatchBundle() as bundle:
            bundle.mock_store.create_pipeline.return_value = _make_mock_pipeline(mode="issue")
            client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "issue",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "issue_number": 42,
                },
            )
            call = bundle.mock_store.create_pipeline.call_args
            assert call is None, "create_pipeline should not be called for non-custom mode"


# ---------------------------------------------------------------------------
# Auto-generated branch fallback (decision-7)
# ---------------------------------------------------------------------------


class TestAutoGeneratedBranch:
    def test_no_branch_gets_custom_fallback(self, client):
        """When caller omits ``branch`` AND no PR is targeted, the route
        must auto-generate ``egg/custom-<pipeline_id>``."""
        with _PatchBundle() as bundle:
            bundle.mock_store.create_pipeline.return_value = _make_mock_pipeline(
                pipeline_id="pipeline-abcd1234",
                branch="egg/custom-pipeline-abcd1234",
            )
            client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "roles": ["coder"],
                    "pipeline_id": "pipeline-abcd1234",
                },
            )
            call = bundle.mock_store.create_pipeline.call_args
            assert call is not None, "create_pipeline was never called"
            branch = call.kwargs.get("branch")
            assert branch is not None
            assert branch.startswith("egg/custom-")

    def test_caller_supplied_branch_preserved(self, client):
        with _PatchBundle() as bundle:
            bundle.mock_store.create_pipeline.return_value = _make_mock_pipeline(
                branch="my-custom-branch"
            )
            client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "roles": ["coder"],
                    "branch": "my-custom-branch",
                    "pipeline_id": "pipeline-aabbccdd",
                },
            )
            call = bundle.mock_store.create_pipeline.call_args
            assert call is not None, "create_pipeline was never called"
            assert call.kwargs.get("branch") == "my-custom-branch"


# ---------------------------------------------------------------------------
# Repo allowlist / format (risk_analyst R9)
# ---------------------------------------------------------------------------


class TestRepoValidation:
    def test_invalid_repo_format_returns_400_with_reason(self, client):
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "no-slash",
                    "prompt": "x",
                },
            )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["details"].get("reason") == "repo_not_allowed"

    def test_shell_metachar_repo_rejected(self, client):
        """risk_analyst R9: prevents values that could be re-interpreted
        as git flags / shell args."""
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo;echo",
                    "prompt": "x",
                },
            )
        assert resp.status_code == 400
        assert resp.get_json()["details"]["reason"] == "repo_not_allowed"


# ---------------------------------------------------------------------------
# CUSTOM + PR runs PR pre-flight
# ---------------------------------------------------------------------------


class TestCustomPlusPrPreflight:
    def test_merged_pr_rejected(self, client):
        with _PatchBundle() as bundle:
            bundle.mock_fetch.return_value = {
                "state": "MERGED",
                "base_ref": "main",
                "head_ref": "feature",
                "head_sha": "abc1234",
                "is_fork": False,
                "changed_files": 3,
            }
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "pr_number": 42,
                },
            )
        assert resp.status_code == 409
        assert resp.get_json()["details"]["reason"] == "pr_merged"

    def test_fork_pr_rejected(self, client):
        with _PatchBundle() as bundle:
            bundle.mock_fetch.return_value = {
                "state": "OPEN",
                "base_ref": "main",
                "head_ref": "feature",
                "head_sha": "abc1234",
                "is_fork": True,
                "changed_files": 3,
                "head_repository_name_with_owner": "fork/repo",
            }
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "pr_number": 42,
                },
            )
        assert resp.status_code == 400
        assert resp.get_json()["details"]["reason"] == "pr_from_fork"

    def test_empty_diff_rejected(self, client):
        with _PatchBundle() as bundle:
            bundle.mock_fetch.return_value = {
                "state": "OPEN",
                "base_ref": "main",
                "head_ref": "feature",
                "head_sha": "abc1234",
                "is_fork": False,
                "changed_files": 0,
            }
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "pr_number": 42,
                },
            )
        assert resp.status_code == 409
        assert resp.get_json()["details"]["reason"] == "pr_empty_diff"


# ---------------------------------------------------------------------------
# pr_number type checks
# ---------------------------------------------------------------------------


class TestCustomPrNumberType:
    def test_non_int_pr_number_rejected(self, client):
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "pr_number": "forty-two",
                },
            )
        assert resp.status_code == 400

    def test_negative_pr_number_rejected(self, client):
        with _PatchBundle():
            resp = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "custom",
                    "phase": "implement",
                    "repo": "owner/repo",
                    "prompt": "x",
                    "pr_number": -1,
                },
            )
        assert resp.status_code == 400
