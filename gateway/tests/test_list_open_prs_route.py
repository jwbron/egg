"""Tests for the ``/api/v1/gh/list_open_prs`` control-plane route.

This route is the orchestrator's open-PR listing, used by the context-PR
idempotency pre-flight (``_open_context_pr_at_implement_start``) and the
stacked-PR reconciler. Like ``/api/v1/gh/find_open_pr``, it is
**launcher-authed** (control plane), not session-authed (agent): the
orchestrator is the server that manages pipelines, not an ``AgentRole``,
so it does not register a synthetic agent session or impersonate a role
on the per-agent ``/api/v1/gh/execute`` surface. (This completes the
#2925 migration that lets ``AgentRole.ORCHESTRATOR`` be deleted; #2910
had wedged that bogus role into the agent allowlist.)

Covers:
- Launcher-bearer-token enforcement (401 on missing / wrong credentials).
- Hit returns the matching PR records; empty returns ``[]``.
- Input validation (400 on missing/invalid repo and out-of-range limit).
- The route constructs a *fixed* read-only argv server-side — there is
  no arbitrary gh-command surface on the launcher-auth path.
- Upstream gh failure surfaces as 500.
- Malformed gh stdout is treated as an empty list rather than 500.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# conftest.py loads the gateway modules under bare names.
import gateway

TEST_LAUNCHER_SECRET = os.environ.get("EGG_LAUNCHER_SECRET", "test-launcher-secret-12345")


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


@pytest.fixture
def launcher_auth_headers():
    return {"Authorization": f"Bearer {TEST_LAUNCHER_SECRET}"}


def _fake_github(stdout: str = "[]", *, success: bool = True, stderr: str = ""):
    """A ``get_github_client`` substitute whose ``execute`` returns a
    GitHubResult-shaped object."""
    result = MagicMock()
    result.success = success
    result.stdout = stdout
    result.stderr = stderr
    result.to_dict.return_value = {"success": success, "stdout": stdout, "stderr": stderr}

    github = MagicMock()
    github.execute.return_value = result
    return github


class TestListOpenPrsAuth:
    """Launcher auth is the only gate — agents must not reach this route."""

    def test_missing_auth_returns_401(self, client):
        response = client.post(
            "/api/v1/gh/list_open_prs",
            json={"repo": "owner/repo"},
        )
        assert response.status_code == 401

    def test_wrong_bearer_token_returns_401(self, client):
        response = client.post(
            "/api/v1/gh/list_open_prs",
            json={"repo": "owner/repo"},
            headers={"Authorization": "Bearer nope"},
        )
        assert response.status_code == 401


class TestListOpenPrsListing:
    def test_returns_pr_records(self, client, launcher_auth_headers):
        stdout = (
            '[{"number": 11, "headRefName": "egg/i/slice-1", "baseRefName": "egg/i/work"},'
            ' {"number": 12, "headRefName": "egg/i/slice-2", "baseRefName": "egg/i/slice-1"}]'
        )
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "get_github_client", return_value=_fake_github(stdout)),
        ):
            response = client.post(
                "/api/v1/gh/list_open_prs",
                json={"repo": "owner/repo"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        prs = response.get_json()["data"]["prs"]
        assert [p["number"] for p in prs] == [11, 12]
        assert prs[0]["headRefName"] == "egg/i/slice-1"
        assert prs[1]["baseRefName"] == "egg/i/slice-1"

    def test_empty_returns_empty_list(self, client, launcher_auth_headers):
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "get_github_client", return_value=_fake_github("[]")),
        ):
            response = client.post(
                "/api/v1/gh/list_open_prs",
                json={"repo": "owner/repo"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        assert response.get_json()["data"]["prs"] == []

    def test_constructs_fixed_readonly_argv(self, client, launcher_auth_headers):
        """The route must NOT accept arbitrary argv — it builds the fixed
        read-only ``gh pr list`` command server-side from repo + limit.

        Also pins the ``mode=auth_mode`` flow-through at both seams: the
        route resolves ``auth_mode = get_auth_mode(repo)`` and forwards it
        as a kwarg to both ``get_github_client`` (so the upstream auth-mode
        lookup picks the right client) AND ``github.execute`` (so the
        per-call execution uses the matching credentials). A regression
        that drops the ``mode`` keyword at either seam would silently fall
        back to the default mode.
        """
        github = _fake_github("[]")
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "get_github_client", return_value=github) as get_client,
        ):
            response = client.post(
                "/api/v1/gh/list_open_prs",
                json={"repo": "owner/repo", "limit": 50},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        called_args = github.execute.call_args[0][0]
        assert called_args == [
            "pr",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,headRefName,baseRefName",
        ]
        called_kwargs = github.execute.call_args[1]
        assert called_kwargs.get("mode") == "bot"
        get_client.assert_called_with(mode="bot")

    def test_default_limit_is_200(self, client, launcher_auth_headers):
        """Omitting ``limit`` defaults to 200 in the server-side argv."""
        github = _fake_github("[]")
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "get_github_client", return_value=github),
        ):
            response = client.post(
                "/api/v1/gh/list_open_prs",
                json={"repo": "owner/repo"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        called_args = github.execute.call_args[0][0]
        limit_idx = called_args.index("--limit")
        assert called_args[limit_idx + 1] == "200"


class TestListOpenPrsValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {},  # missing repo
            {"repo": ""},  # empty repo
            {"repo": "   "},  # whitespace-only repo
            {"repo": 123},  # non-string repo
            {"repo": "owner/repo", "limit": 0},  # limit below range
            {"repo": "owner/repo", "limit": 1001},  # limit above range
            {"repo": "owner/repo", "limit": "50"},  # non-int limit
            {"repo": "owner/repo", "limit": True},  # bool is not a valid int limit
            {"repo": "owner/repo", "limit": 1.5},  # float limit
        ],
    )
    def test_invalid_inputs_return_400(self, client, launcher_auth_headers, body):
        response = client.post(
            "/api/v1/gh/list_open_prs",
            json=body,
            headers=launcher_auth_headers,
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "malformed_repo",
        [
            "not-owner-slash-repo",  # no slash
            # Full GitHub URL form: ``parse_owner_repo`` would have accepted
            # this via its ``parse_github_url`` fallback, but the route
            # promises the literal ``owner/name`` shape and uses
            # ``OWNER_REPO_PATTERN.match`` directly.
            "https://github.com/owner/repo",
            "owner/repo/extra",  # too many segments
        ],
    )
    def test_malformed_repo_returns_400(self, client, launcher_auth_headers, malformed_repo):
        response = client.post(
            "/api/v1/gh/list_open_prs",
            json={"repo": malformed_repo},
            headers=launcher_auth_headers,
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "non_object_body",
        [
            [],  # JSON array — ``data.get(...)`` would raise AttributeError
            ["owner/repo"],
            "owner/repo",  # JSON string
        ],
    )
    def test_non_object_body_returns_400(self, client, launcher_auth_headers, non_object_body):
        """A launcher caller that posts a non-object JSON body (array,
        string, etc.) gets a clean 400, not a 500 from
        ``AttributeError: 'list' object has no attribute 'get'``.
        """
        response = client.post(
            "/api/v1/gh/list_open_prs",
            json=non_object_body,
            headers=launcher_auth_headers,
        )
        assert response.status_code == 400


class TestListOpenPrsUpstreamFailure:
    def test_gh_failure_returns_500(self, client, launcher_auth_headers):
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(
                gateway,
                "get_github_client",
                return_value=_fake_github("", success=False, stderr="boom"),
            ),
        ):
            response = client.post(
                "/api/v1/gh/list_open_prs",
                json={"repo": "owner/repo"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 500

    def test_malformed_json_treated_as_empty(self, client, launcher_auth_headers):
        """If gh returns non-JSON stdout (should not happen with --json,
        but defensive), the route returns an empty list rather than 500."""
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "get_github_client", return_value=_fake_github("not-json")),
        ):
            response = client.post(
                "/api/v1/gh/list_open_prs",
                json={"repo": "owner/repo"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        assert response.get_json()["data"]["prs"] == []

    def test_non_dict_items_are_filtered(self, client, launcher_auth_headers):
        """A defensive guard: non-dict entries in the gh JSON array are
        dropped rather than surfaced (or crashing the route)."""
        stdout = '[{"number": 7, "headRefName": "h", "baseRefName": "b"}, "garbage", 42]'
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "get_github_client", return_value=_fake_github(stdout)),
        ):
            response = client.post(
                "/api/v1/gh/list_open_prs",
                json={"repo": "owner/repo"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        prs = response.get_json()["data"]["prs"]
        assert prs == [{"number": 7, "headRefName": "h", "baseRefName": "b"}]
