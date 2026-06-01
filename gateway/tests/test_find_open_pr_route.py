"""Tests for the ``/api/v1/gh/find_open_pr`` control-plane route.

This route is the orchestrator's slice-PR idempotency lookup. It is
**launcher-authed** (control plane), not session-authed (agent): the
orchestrator is the server that manages pipelines, not an ``AgentRole``,
so it does not register a synthetic agent session or impersonate a role
on the per-agent ``/api/v1/gh/execute`` surface. (This supersedes the
#2893 approach of wedging ``AgentRole.ORCHESTRATOR`` into the agent
allowlist.)

Covers:
- Launcher-bearer-token enforcement (401 on missing / wrong credentials).
- Hit returns the matching PR number; miss returns ``null``.
- Input validation (400 on missing/invalid repo/head/base).
- The route constructs a *fixed* read-only argv server-side — there is
  no arbitrary gh-command surface on the launcher-auth path.
- Upstream gh failure surfaces as 500.
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


class TestFindOpenPrAuth:
    """Launcher auth is the only gate — agents must not reach this route."""

    def test_missing_auth_returns_401(self, client):
        response = client.post(
            "/api/v1/gh/find_open_pr",
            json={"repo": "owner/repo", "head": "h", "base": "b"},
        )
        assert response.status_code == 401

    def test_wrong_bearer_token_returns_401(self, client):
        response = client.post(
            "/api/v1/gh/find_open_pr",
            json={"repo": "owner/repo", "head": "h", "base": "b"},
            headers={"Authorization": "Bearer nope"},
        )
        assert response.status_code == 401


class TestFindOpenPrLookup:
    def test_hit_returns_pr_number(self, client, launcher_auth_headers):
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(
                gateway,
                "get_github_client",
                return_value=_fake_github('[{"number": 4242}]'),
            ),
        ):
            response = client.post(
                "/api/v1/gh/find_open_pr",
                json={
                    "repo": "owner/repo",
                    "head": "egg/issue-42/slice-1",
                    "base": "egg/issue-42/work",
                },
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        assert response.get_json()["data"]["number"] == 4242

    def test_miss_returns_null(self, client, launcher_auth_headers):
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "get_github_client", return_value=_fake_github("[]")),
        ):
            response = client.post(
                "/api/v1/gh/find_open_pr",
                json={"repo": "owner/repo", "head": "h", "base": "b"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        assert response.get_json()["data"]["number"] is None

    def test_constructs_fixed_readonly_argv(self, client, launcher_auth_headers):
        """The route must NOT accept arbitrary argv — it builds the fixed
        read-only ``gh pr list`` command server-side from repo/head/base."""
        github = _fake_github("[]")
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "get_github_client", return_value=github),
        ):
            response = client.post(
                "/api/v1/gh/find_open_pr",
                json={"repo": "owner/repo", "head": "myhead", "base": "mybase"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        called_args = github.execute.call_args[0][0]
        assert called_args == [
            "pr",
            "list",
            "--repo",
            "owner/repo",
            "--head",
            "myhead",
            "--base",
            "mybase",
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            "number",
        ]


class TestFindOpenPrValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {"head": "h", "base": "b"},  # missing repo
            {"repo": "owner/repo", "base": "b"},  # missing head
            {"repo": "owner/repo", "head": "h"},  # missing base
            {"repo": "owner/repo", "head": "", "base": "b"},  # empty head
            {"repo": "owner/repo", "head": "h", "base": ""},  # empty base
            {"repo": 123, "head": "h", "base": "b"},  # non-string repo
        ],
    )
    def test_invalid_inputs_return_400(self, client, launcher_auth_headers, body):
        response = client.post(
            "/api/v1/gh/find_open_pr",
            json=body,
            headers=launcher_auth_headers,
        )
        assert response.status_code == 400

    def test_malformed_repo_returns_400(self, client, launcher_auth_headers):
        response = client.post(
            "/api/v1/gh/find_open_pr",
            json={"repo": "not-owner-slash-repo", "head": "h", "base": "b"},
            headers=launcher_auth_headers,
        )
        assert response.status_code == 400


class TestFindOpenPrUpstreamFailure:
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
                "/api/v1/gh/find_open_pr",
                json={"repo": "owner/repo", "head": "h", "base": "b"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 500

    def test_malformed_json_treated_as_miss(self, client, launcher_auth_headers):
        """If gh returns non-JSON stdout (should not happen with --json,
        but defensive), the route returns a miss rather than 500."""
        with (
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "get_github_client", return_value=_fake_github("not-json")),
        ):
            response = client.post(
                "/api/v1/gh/find_open_pr",
                json={"repo": "owner/repo", "head": "h", "base": "b"},
                headers=launcher_auth_headers,
            )
        assert response.status_code == 200
        assert response.get_json()["data"]["number"] is None
