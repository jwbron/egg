"""Babysit-pr pipeline creation route tests.

Replaces the deleted ``test_babysit_pipeline_creation.py`` after #1748,
where the legacy ``shared/egg_babysit/`` package was removed and the
babysit-pr workflow now lives behind ``POST /api/v1/pipelines`` with
``mode=babysit``.

Covers:
    * Happy-path creation (pipeline_id derivation, auto-populated
      branch/base_branch from PR head/base refs, caller overrides).
    * Early-exit refusals (MERGED/CLOSED/fork/empty-diff/missing pr_number
      or repo/non-positive pr_number/non-int pr_number).
    * ``has_contract=False`` invariant for babysit vs. issue-mode.
    * ``pr_head_sha`` captured from the gh PR state helper.
    * Duplicate ``pr-{N}`` returns 409 with existing pipeline details.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

# sys.path bootstrap mirroring other orchestrator/tests/ files
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
    """Flask app with the pipelines blueprint registered."""
    from routes.pipelines import pipelines_bp

    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _pr_state(**overrides):
    """Build a canned ``_fetch_pr_state()`` return dict for tests."""
    base = {
        "state": "OPEN",
        "base_ref": "main",
        "head_ref": "feature-branch",
        "head_sha": "abc1234deadbeef",
        "is_fork": False,
        "changed_files": 3,
        "head_repository_name_with_owner": "owner/repo",
    }
    base.update(overrides)
    return base


def _make_mock_pipeline(
    pipeline_id: str = "pr-42",
    *,
    has_contract: bool = False,
    pr_head_sha: str | None = "abc1234deadbeef",
    branch: str | None = "feature-branch",
    base_branch: str | None = "main",
    pr_number: int | None = 42,
) -> MagicMock:
    """Create a MagicMock shaped like a Pipeline for route responses."""
    fake = MagicMock()
    fake.id = pipeline_id
    fake.model_dump.return_value = {
        "id": pipeline_id,
        "has_contract": has_contract,
        "pr_head_sha": pr_head_sha,
        "branch": branch,
        "base_branch": base_branch,
        "pr_number": pr_number,
        "mode": "babysit",
    }
    return fake


def _babysit_patches():
    """Return a context-managed bundle of patches for the babysit route.

    Use like:
        with _babysit_patches() as (mock_fetch, mock_store_factory,
                                    mock_repo, mock_gw):
            ...
    """
    # We cannot easily return multiple contextmanagers from a single helper
    # without ExitStack; keep as a docstring marker so the individual tests
    # can inline their ``with patch(...) as ...`` blocks.
    raise NotImplementedError


class TestBabysitCreationHappyPath:
    """201/200 happy path for ``mode=babysit``."""

    def test_open_pr_creates_pipeline_successfully(self, client):
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state()
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            mock_store.create_pipeline.return_value = _make_mock_pipeline("pr-42")
            mock_store_factory.return_value = mock_store
            # No branch-existence conflict — ls_remote_branch returns False.
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code in (200, 201), response.get_json()
        body = response.get_json()
        assert body["success"] is True
        pipeline = body["data"]["pipeline"]
        assert pipeline["id"] == "pr-42"
        assert pipeline["has_contract"] is False
        assert pipeline["pr_head_sha"] == "abc1234deadbeef"

    def test_branch_auto_populated_from_pr_head_ref(self, client):
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state(head_ref="special-head-branch")
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            mock_store.create_pipeline.return_value = _make_mock_pipeline(
                "pr-42", branch="special-head-branch"
            )
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code in (200, 201), response.get_json()
        call_kwargs = mock_store.create_pipeline.call_args.kwargs
        assert call_kwargs["branch"] == "special-head-branch"

    def test_base_branch_auto_populated_from_pr_base_ref(self, client):
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state(base_ref="release-2.0")
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            mock_store.create_pipeline.return_value = _make_mock_pipeline(
                "pr-42", base_branch="release-2.0"
            )
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code in (200, 201), response.get_json()
        call_kwargs = mock_store.create_pipeline.call_args.kwargs
        assert call_kwargs["base_branch"] == "release-2.0"

    def test_caller_overrides_beat_gh_derived_values(self, client):
        """Explicit branch / base_branch in the request body win over gh-derived ones."""
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state(
                head_ref="gh-derived-head", base_ref="gh-derived-base"
            )
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            mock_store.create_pipeline.return_value = _make_mock_pipeline(
                "pr-42", branch="explicit-branch", base_branch="explicit-base"
            )
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "babysit",
                    "pr_number": 42,
                    "repo": "owner/repo",
                    "branch": "explicit-branch",
                    "base_branch": "explicit-base",
                },
            )

        assert response.status_code in (200, 201), response.get_json()
        call_kwargs = mock_store.create_pipeline.call_args.kwargs
        assert call_kwargs["branch"] == "explicit-branch"
        assert call_kwargs["base_branch"] == "explicit-base"


class TestBabysitCreationEarlyExits:
    """Refusals that must short-circuit before ``store.create_pipeline()``."""

    @pytest.mark.parametrize(
        "pr_state_overrides,expected_status,expected_reason",
        [
            ({"state": "MERGED"}, 409, "pr_merged"),
            ({"state": "CLOSED"}, 409, "pr_closed"),
            ({"is_fork": True}, 400, "pr_from_fork"),
            ({"changed_files": 0}, 409, "pr_empty_diff"),
        ],
    )
    def test_pr_state_early_exits(
        self, client, pr_state_overrides, expected_status, expected_reason
    ):
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
        ):
            mock_fetch.return_value = _pr_state(**pr_state_overrides)
            mock_repo_path.return_value = "/tmp/repo"

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code == expected_status
        body = response.get_json()
        assert body["success"] is False
        assert body["details"]["reason"] == expected_reason
        # Pipeline must not be created on refusal.
        mock_store_factory.assert_not_called()

    def test_fork_error_message_mentions_head_repo(self, client):
        """Fork refusal surfaces the offending head repo name in the message."""
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
        ):
            mock_fetch.return_value = _pr_state(
                is_fork=True, head_repository_name_with_owner="evil-fork/repo"
            )
            mock_repo_path.return_value = "/tmp/repo"

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code == 400
        body = response.get_json()
        assert "evil-fork/repo" in body["message"]
        mock_store_factory.assert_not_called()

    def test_missing_pr_number(self, client):
        with patch("routes.pipelines.get_repo_path") as mock_repo_path:
            mock_repo_path.return_value = "/tmp/repo"
            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "repo": "owner/repo"},
            )
        assert response.status_code == 400
        body = response.get_json()
        assert "pr_number" in body["message"].lower()

    def test_zero_pr_number(self, client):
        with patch("routes.pipelines.get_repo_path") as mock_repo_path:
            mock_repo_path.return_value = "/tmp/repo"
            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 0, "repo": "owner/repo"},
            )
        # pr_number=0 is falsy, so it triggers the "missing pr_number" branch.
        assert response.status_code == 400
        body = response.get_json()
        assert "pr_number" in body["message"].lower()

    def test_negative_pr_number(self, client):
        with patch("routes.pipelines.get_repo_path") as mock_repo_path:
            mock_repo_path.return_value = "/tmp/repo"
            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": -5, "repo": "owner/repo"},
            )
        assert response.status_code == 400
        body = response.get_json()
        assert "positive integer" in body["message"].lower()

    def test_string_pr_number(self, client):
        with patch("routes.pipelines.get_repo_path") as mock_repo_path:
            mock_repo_path.return_value = "/tmp/repo"
            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": "42", "repo": "owner/repo"},
            )
        assert response.status_code == 400
        body = response.get_json()
        # Must be rejected either as "not an int" (positive integer) branch.
        assert "positive integer" in body["message"].lower()

    def test_missing_repo(self, client):
        with patch("routes.pipelines.get_repo_path") as mock_repo_path:
            mock_repo_path.return_value = "/tmp/repo"
            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42},
            )
        assert response.status_code == 400
        body = response.get_json()
        assert "repo" in body["message"].lower()


class TestBabysitCreationPipelineIdFormat:
    """Validate ``pipeline_id`` auto-derivation and caller overrides."""

    def test_pipeline_id_defaults_to_pr_prefix(self, client):
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state()
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            mock_store.create_pipeline.return_value = _make_mock_pipeline("pr-314")
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 314, "repo": "owner/repo"},
            )

        assert response.status_code in (200, 201), response.get_json()
        call_kwargs = mock_store.create_pipeline.call_args.kwargs
        assert call_kwargs["pipeline_id"] == "pr-314"

    def test_caller_supplied_pipeline_id_is_honored(self, client):
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state()
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            mock_store.create_pipeline.return_value = _make_mock_pipeline("custom-id-xyz")
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={
                    "mode": "babysit",
                    "pr_number": 42,
                    "repo": "owner/repo",
                    "pipeline_id": "custom-id-xyz",
                },
            )

        assert response.status_code in (200, 201), response.get_json()
        call_kwargs = mock_store.create_pipeline.call_args.kwargs
        assert call_kwargs["pipeline_id"] == "custom-id-xyz"


class TestBabysitCreationHasContractFalse:
    """Babysit pipelines set ``has_contract=False`` (vs. issue-mode True)."""

    def test_babysit_sets_has_contract_false(self, client):
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state()
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            mock_store.create_pipeline.return_value = _make_mock_pipeline("pr-42")
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code in (200, 201), response.get_json()
        call_kwargs = mock_store.create_pipeline.call_args.kwargs
        assert call_kwargs["has_contract"] is False

    def test_issue_mode_sets_has_contract_true(self, client):
        """Sanity: issue-mode flows keep the default ``has_contract=True``."""
        with (
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            fake = MagicMock()
            fake.id = "issue-123"
            fake.model_dump.return_value = {"id": "issue-123", "has_contract": True}
            mock_store.create_pipeline.return_value = fake
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={
                    "issue_number": 123,
                    "repo": "owner/repo",
                    "branch": "egg/issue-123",
                },
            )

        assert response.status_code in (200, 201), response.get_json()
        call_kwargs = mock_store.create_pipeline.call_args.kwargs
        assert call_kwargs["has_contract"] is True


class TestBabysitCreationPrHeadShaCaptured:
    """``pr_head_sha`` is plucked from ``_fetch_pr_state`` at creation time."""

    def test_pr_head_sha_forwarded_from_gh(self, client):
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state(head_sha="abc1234deadbeef")
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            mock_store.create_pipeline.return_value = _make_mock_pipeline(
                "pr-42", pr_head_sha="abc1234deadbeef"
            )
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code in (200, 201), response.get_json()
        call_kwargs = mock_store.create_pipeline.call_args.kwargs
        assert call_kwargs["pr_head_sha"] == "abc1234deadbeef"

    @pytest.mark.parametrize("missing_sha", [None, ""])
    def test_empty_head_sha_forwarded_as_none(self, client, missing_sha):
        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state(head_sha=missing_sha)
            mock_repo_path.return_value = "/tmp/repo"
            mock_store = MagicMock()
            mock_store.create_pipeline.return_value = _make_mock_pipeline("pr-42", pr_head_sha=None)
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code in (200, 201), response.get_json()
        call_kwargs = mock_store.create_pipeline.call_args.kwargs
        assert call_kwargs["pr_head_sha"] is None


class TestBabysitCreationDuplicate:
    """Duplicate ``pr-{N}`` pipeline returns 409 with existing details."""

    def test_duplicate_returns_409_with_existing_pipeline_details(self, client):
        from state_store import StateStoreError

        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state()
            mock_repo_path.return_value = "/tmp/repo"

            mock_store = MagicMock()
            mock_store.create_pipeline.side_effect = StateStoreError(
                "Pipeline pr-42 already exists"
            )
            existing = MagicMock()
            existing.id = "pr-42"
            existing.status.value = "running"
            existing.current_phase.value = "implement"
            mock_store.load_pipeline.return_value = existing
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code == 409
        body = response.get_json()
        assert body["success"] is False
        assert "already exists" in body["message"].lower()
        details = body.get("details", {})
        assert details.get("existing_pipeline_id") == "pr-42"
        assert details.get("existing_status") == "running"
        assert details.get("existing_phase") == "implement"

    def test_duplicate_without_loadable_existing_still_returns_409(self, client):
        """If load_pipeline fails (e.g. race), we still surface the 409."""
        from state_store import StateStoreError

        with (
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_fetch.return_value = _pr_state()
            mock_repo_path.return_value = "/tmp/repo"

            mock_store = MagicMock()
            mock_store.create_pipeline.side_effect = StateStoreError(
                "Pipeline pr-42 already exists"
            )
            mock_store.load_pipeline.side_effect = RuntimeError("corrupt state file")
            mock_store_factory.return_value = mock_store
            mock_gw.return_value.ls_remote_branch.return_value = False

            response = client.post(
                "/api/v1/pipelines",
                json={"mode": "babysit", "pr_number": 42, "repo": "owner/repo"},
            )

        assert response.status_code == 409
        body = response.get_json()
        assert body["success"] is False
        assert "already exists" in body["message"].lower()


class TestGatewayReadinessGate:
    """``create_pipeline`` waits for gateway readiness before any gateway-

    dependent work.  See #1851 — without this gate, fresh deploys hit a
    window where the orchestrator is up but the gateway HTTP listener
    isn't, and the first submission cascades into per-agent ConnectionRefused
    errors that operators have to reverse-engineer.
    """

    def test_unready_gateway_returns_503_with_named_reason(self, client, monkeypatch):
        """If ``wait_for_healthy`` times out, return a clear 503 — not a 200
        followed by a downstream cascade of per-agent spawn failures.
        """
        # Cap the wait so the test stays fast even if patching regresses.
        monkeypatch.setenv("EGG_GATEWAY_READY_TIMEOUT_SECONDS", "1")

        with (
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_repo_path.return_value = "/tmp/repo"
            mock_gw.return_value.wait_for_healthy.return_value = False
            mock_gw.return_value.check_health.return_value = MagicMock(
                status="unreachable", error="Connection refused"
            )

            response = client.post(
                "/api/v1/pipelines",
                json={
                    "issue_number": 123,
                    "repo": "owner/repo",
                    "branch": "egg/issue-123",
                },
            )

        assert response.status_code == 503, response.get_json()
        body = response.get_json()
        assert body["success"] is False
        assert "gateway not ready" in body["message"].lower()
        details = body.get("details", {})
        assert details.get("reason") == "gateway_not_ready"
        assert details.get("gateway_status") == "unreachable"
        assert details.get("gateway_error") == "Connection refused"
        assert details.get("timeout_seconds") == 1
        # RFC 7231 §6.6.4: Retry-After guides client retry behaviour.
        assert response.headers.get("Retry-After") == "1"
        # Pipeline must not be created when the gateway is unreachable.
        mock_store_factory.assert_not_called()

    def test_ready_gateway_proceeds_to_pipeline_creation(self, client, monkeypatch):
        """When ``wait_for_healthy`` returns True the route falls through
        to the existing branch-existence check + creation flow.
        """
        monkeypatch.setenv("EGG_GATEWAY_READY_TIMEOUT_SECONDS", "5")

        with (
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_repo_path.return_value = "/tmp/repo"
            mock_gw.return_value.wait_for_healthy.return_value = True
            mock_gw.return_value.ls_remote_branch.return_value = False
            mock_store = MagicMock()
            fake = MagicMock()
            fake.id = "issue-123"
            fake.model_dump.return_value = {"id": "issue-123", "has_contract": True}
            mock_store.create_pipeline.return_value = fake
            mock_store_factory.return_value = mock_store

            response = client.post(
                "/api/v1/pipelines",
                json={
                    "issue_number": 123,
                    "repo": "owner/repo",
                    "branch": "egg/issue-123",
                },
            )

        assert response.status_code in (200, 201), response.get_json()
        # check_health should NOT be called when wait_for_healthy succeeds —
        # we only probe again to surface the last-known status on failure.
        mock_gw.return_value.check_health.assert_not_called()

    def test_zero_timeout_disables_gate(self, client, monkeypatch):
        """``EGG_GATEWAY_READY_TIMEOUT_SECONDS=0`` skips the gate entirely
        (escape hatch for environments where the gateway is started out of
        band, e.g. integration test harnesses that wire it up themselves).
        """
        monkeypatch.setenv("EGG_GATEWAY_READY_TIMEOUT_SECONDS", "0")

        with (
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_repo_path.return_value = "/tmp/repo"
            mock_gw.return_value.ls_remote_branch.return_value = False
            mock_store = MagicMock()
            fake = MagicMock()
            fake.id = "issue-123"
            fake.model_dump.return_value = {"id": "issue-123", "has_contract": True}
            mock_store.create_pipeline.return_value = fake
            mock_store_factory.return_value = mock_store

            response = client.post(
                "/api/v1/pipelines",
                json={
                    "issue_number": 123,
                    "repo": "owner/repo",
                    "branch": "egg/issue-123",
                },
            )

        assert response.status_code in (200, 201), response.get_json()
        mock_gw.return_value.wait_for_healthy.assert_not_called()

    def test_negative_timeout_clamped_to_zero(self, client, monkeypatch):
        """Negative ``EGG_GATEWAY_READY_TIMEOUT_SECONDS`` is clamped to 0
        (i.e. gate disabled), not silently treated as a truthy value.
        """
        monkeypatch.setenv("EGG_GATEWAY_READY_TIMEOUT_SECONDS", "-5")

        with (
            patch("routes.pipelines.get_state_store") as mock_store_factory,
            patch("routes.pipelines.get_repo_path") as mock_repo_path,
            patch("routes.pipelines.get_gateway_client") as mock_gw,
        ):
            mock_repo_path.return_value = "/tmp/repo"
            mock_gw.return_value.ls_remote_branch.return_value = False
            mock_store = MagicMock()
            fake = MagicMock()
            fake.id = "issue-123"
            fake.model_dump.return_value = {"id": "issue-123", "has_contract": True}
            mock_store.create_pipeline.return_value = fake
            mock_store_factory.return_value = mock_store

            response = client.post(
                "/api/v1/pipelines",
                json={
                    "issue_number": 123,
                    "repo": "owner/repo",
                    "branch": "egg/issue-123",
                },
            )

        assert response.status_code in (200, 201), response.get_json()
        mock_gw.return_value.wait_for_healthy.assert_not_called()
