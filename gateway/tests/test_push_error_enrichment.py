"""Tests for the gateway push handler response schema (post-#2039).

Historical: #1527 returned 403 with enrichment fields on push denials;
#1882 replaced that with silent auto-filter rewrites + 200 responses
(``filtered``, ``excluded_files``, etc.); #2039 reverted the silent
behavior and now returns a structured 403 ``restricted_path_modified``
when any own-authored file is blocked.

The current contract:

- All-allowed push: 200, ``filtered=false``, ``pulled_commits`` present.
- Any blocked own file (enforce mode): 403 with
  ``error=restricted_path_modified``, ``role``, ``blocked_paths``,
  ``recommended_action``.  See test_filtered_push_blocked_modify.py
  for the 403 contract; this file covers the all-allowed plain-push
  schema.

These tests are the canonical contract for the response shape the
sandbox clients rely on.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import git_client
import pytest
import session_manager
from git_client import AttributedFile, AttributedPushRange
from phase_filter import FileRestrictionResult
from policy import PolicyResult
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import SessionValidationResult

import gateway


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


_VALID_SHA = "a" * 40


def _make_session(role: str = "coder"):
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role
    mock_session.phase = None
    # Intentionally leave pipeline_id unset — the gateway's pipeline-push
    # enforcement otherwise blocks the push before the auto-filter decision tree.
    mock_session.pipeline_id = None
    return mock_session


def _build_range(file_paths: list[str], role: str) -> AttributedPushRange:
    """Every file in the range attributed to ``role`` (own-authored)."""
    files = [AttributedFile(path=p, commit_sha=_VALID_SHA, authored_by=role) for p in file_paths]
    return AttributedPushRange(
        files=files,
        commits=[_VALID_SHA],
        attribution={_VALID_SHA: role},
    )


def _push_context(mock_session, file_paths: list[str]):
    """Mock the Flask request chain and the push handler's helpers."""
    import auth

    mock_result = SessionValidationResult(valid=True, session=mock_session)
    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True, reason="Test mode", visibility="public"
    )

    auth._session_manager = None
    auth._rate_limiter = None
    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    current_sm = sys.modules.get("session_manager", session_manager)

    def run_side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "remote" in cmd and "get-url" in cmd:
            result.stdout = "https://github.com/owner/repo.git\n"
        elif "branch" in cmd and "--show-current" in cmd:
            result.stdout = "egg-feature\n"
        elif "push" in cmd:
            result.stdout = "Everything up-to-date\n"
        else:
            result.stdout = ""
        return result

    fake_range = _build_range(file_paths, mock_session.agent_role or "coder")
    return (
        patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
        patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
        patch("subprocess.run", side_effect=run_side_effect),
        patch.object(
            gateway,
            "get_policy_engine",
            return_value=MagicMock(
                check_branch_ownership=MagicMock(
                    return_value=PolicyResult(
                        allowed=True,
                        reason="OK",
                        details={"branch": "egg-feature"},
                    )
                ),
            ),
        ),
        patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
        patch.object(gateway, "get_changed_files_in_push", return_value=(file_paths, None)),
        # Legacy ``gateway.check_file_restrictions`` patch removed in
        # #2489 — the gateway no longer calls that function from
        # ``git_push`` (the attribution-aware path is the sole agent-
        # role enforcer).  See ``test_filtered_push_blocked_modify``.
        patch.object(
            git_client,
            "get_attributed_changed_files_in_push",
            return_value=fake_range,
        ),
    )


def _do_push(client):
    return client.post(
        "/api/v1/git/push",
        headers={"Authorization": "Bearer test-session-token"},
        data=json.dumps(
            {
                "repo_path": "/home/egg/repos/test-repo",
                "remote": "origin",
                "refspec": "egg-feature",
            }
        ),
        content_type="application/json",
    )


def _body(response) -> dict:
    payload = json.loads(response.data)
    return payload.get("data", payload)


# ---------------------------------------------------------------------------
# Restricted-path-modified rejection (#2039)
# ---------------------------------------------------------------------------


class TestRestrictedPathRejection:
    """When any own-authored file is blocked, return 403 with the
    ``restricted_path_modified`` structured body.  Replaces the prior
    nothing_to_push=true success arm."""

    def test_response_status_is_403(self, client):
        session = _make_session("coder")
        files = ["docs/guide.md", "docs/another.md"]
        patches = _push_context(session, files)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                body = _body(response)
                assert body["error"] == "restricted_path_modified"

    def test_response_includes_blocked_paths(self, client):
        session = _make_session("coder")
        files = ["docs/guide.md", "docs/api.md"]
        patches = _push_context(session, files)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                body = _body(response)
                assert sorted(body["blocked_paths"]) == sorted(files)

    def test_response_includes_role_and_recommended_action(self, client):
        session = _make_session("coder")
        patches = _push_context(session, ["docs/guide.md"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                body = _body(response)
                assert body["role"] == "coder"
                assert body["recommended_action"]
                assert "pre-merge-condition" in body["recommended_action"].lower()


# ---------------------------------------------------------------------------
# Plain push (all-allowed) — response-schema parity
# ---------------------------------------------------------------------------


class TestAllAllowedResponse:
    """When nothing is blocked, return 200 filtered=False plus observability fields."""

    def test_plain_push_has_filtered_false(self, client):
        session = _make_session("coder")
        patches = _push_context(session, ["src/main.py"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 200
                body = _body(response)
                assert body["filtered"] is False

    def test_plain_push_has_pulled_commits_key(self, client):
        """Plain pushes still carry pulled_commits for observability parity."""
        session = _make_session("coder")
        patches = _push_context(session, ["src/main.py"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                body = _body(response)
                assert "pulled_commits" in body

    def test_plain_push_has_nothing_to_push_false(self, client):
        session = _make_session("coder")
        patches = _push_context(session, ["src/main.py"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                body = _body(response)
                assert body["nothing_to_push"] is False


# ---------------------------------------------------------------------------
# Warn-only passthrough (EGG_AGENT_RESTRICTIONS_ENFORCE=false)
# ---------------------------------------------------------------------------


class TestWarnOnlyPassthrough:
    """With the kill switch, the auto-filter short-circuits and plain push runs."""

    def test_warn_only_returns_200(self, client):
        session = _make_session("coder")
        patches = _push_context(session, ["docs/guide.md"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "false"}):
                response = _do_push(client)
                assert response.status_code == 200


# ---------------------------------------------------------------------------
# Cross-role (pulled_commits) surfacing
# ---------------------------------------------------------------------------


class TestPulledCommitsField:
    """When a commit in the range is attributed to another role, it appears in pulled_commits."""

    def test_pulled_commits_populated_with_cross_role_entry(self, client):
        """Commit authored by 'tester' is surfaced in pulled_commits with author_role."""
        session = _make_session("coder")

        own_sha = "a" * 40
        pulled_sha = "b" * 40
        fake_range = AttributedPushRange(
            files=[
                AttributedFile(path="src/main.py", commit_sha=own_sha, authored_by="coder"),
                AttributedFile(
                    path="tests/test_main.py", commit_sha=pulled_sha, authored_by="tester"
                ),
            ],
            commits=[own_sha, pulled_sha],
            attribution={own_sha: "coder", pulled_sha: "tester"},
        )

        import auth

        mock_result = SessionValidationResult(valid=True, session=session)
        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True, reason="Test mode", visibility="public"
        )
        auth._session_manager = None
        auth._rate_limiter = None

        current_sm = sys.modules.get("session_manager", session_manager)

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "remote" in cmd and "get-url" in cmd:
                result.stdout = "https://github.com/owner/repo.git\n"
            elif "branch" in cmd and "--show-current" in cmd:
                result.stdout = "egg-feature\n"
            elif "push" in cmd:
                result.stdout = "Everything up-to-date\n"
            else:
                result.stdout = ""
            return result

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run", side_effect=run_side_effect),
            patch.object(
                gateway,
                "get_policy_engine",
                return_value=MagicMock(
                    check_branch_ownership=MagicMock(
                        return_value=PolicyResult(
                            allowed=True,
                            reason="OK",
                            details={"branch": "egg-feature"},
                        )
                    ),
                ),
            ),
            patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
            patch.object(
                gateway,
                "get_changed_files_in_push",
                return_value=(["src/main.py", "tests/test_main.py"], None),
            ),
            # Legacy ``gateway.check_file_restrictions`` patch removed in
            # #2489 — the gateway no longer calls that function from
            # ``git_push`` (see ``test_filtered_push_blocked_modify``).
            patch.object(
                git_client,
                "get_attributed_changed_files_in_push",
                return_value=fake_range,
            ),
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 200
                body = _body(response)
                pulled = body["pulled_commits"]
                assert len(pulled) == 1
                assert pulled[0]["sha"] == pulled_sha
                assert pulled[0]["author_role"] == "tester"


# ---------------------------------------------------------------------------
# Non-agent session (no agent_role) — auto-filter does not apply
# ---------------------------------------------------------------------------


class TestNoAgentRole:
    """Non-agent sessions skip the auto-filter entirely."""

    def test_no_role_yields_plain_push(self, client):
        session = _make_session("coder")
        session.agent_role = None
        patches = _push_context(session, ["src/main.py"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 200


# ---------------------------------------------------------------------------
# File restrictions (check_file_restrictions) — separate from auto-filter
# ---------------------------------------------------------------------------


class TestFileRestrictionsThreeRoleEnrichment1901:
    """TASK-5-3 (#1901): the three new file_restrictions JSON entries
    (coder/tester/documenter) in .egg/phase-permissions.json must produce
    the correct enriched error response when violated.

    Originally these tests drove the legacy ``check_file_restrictions``
    path that ran before the attribution-aware enforcement.  That naive
    whole-push-diff check was removed in #2489 because it falsely
    rejected role-restricted producers whose branches inherited
    upstream commits authored by other roles.  The attribution-aware
    path (#2039) is now the canonical agent-role enforcer; when
    attribution lookup returns no commits the handler fails closed and
    treats every file in the diff as own-authored, so these three
    role/path combinations still produce a 403 — under the
    ``restricted_path_modified`` shape rather than the old
    ``Path allowlist violation`` shape.
    """

    def _push_context_real_file_restrictions(self, mock_session, blocked_files):
        """Push context that does NOT mock check_file_restrictions — the
        attribution-aware path's fail-closed branch (empty commit list →
        every file treated as own-authored, partition_files_by_role
        consulted directly against AGENT_PATTERNS) drives the decision.
        check_agent_restrictions is mocked to allow so we can exercise
        the agent-role branch in isolation.
        """
        import auth

        mock_result = SessionValidationResult(valid=True, session=mock_session)
        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test mode",
            visibility="public",
        )

        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None

        current_sm = sys.modules.get("session_manager", session_manager)

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "remote" in cmd and "get-url" in cmd:
                result.stdout = "https://github.com/owner/repo.git\n"
            elif "branch" in cmd and "--show-current" in cmd:
                result.stdout = "egg-feature\n"
            elif "push" in cmd:
                result.stdout = "Everything up-to-date\n"
            else:
                result.stdout = ""
            return result

        return (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run", side_effect=run_side_effect),
            patch.object(
                gateway,
                "get_policy_engine",
                return_value=MagicMock(
                    check_branch_ownership=MagicMock(
                        return_value=PolicyResult(
                            allowed=True,
                            reason="OK",
                            details={"branch": "egg-feature"},
                        )
                    ),
                ),
            ),
            patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
            patch.object(gateway, "get_changed_files_in_push", return_value=(blocked_files, None)),
            # Mock agent_restrictions to allow so the file_restrictions branch
            # is the only thing that can block.
            patch.object(
                gateway,
                "check_agent_restrictions",
                return_value=FileRestrictionResult.allow("ok"),
            ),
            # NOT mocking check_file_restrictions — this is the path under test.
        )

    def test_coder_role_blocked_from_contracts(self, client):
        """The coder file_restrictions entry blocks .egg-state/contracts/."""
        session = _make_session("coder")
        patches = self._push_context_real_file_restrictions(
            session, [".egg-state/contracts/foo.json"]
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                # Top-level message uses the canonical
                # restricted_path_modified format (#2039).
                assert "coder" in data["message"]
                assert ".egg-state/contracts/foo.json" in data["message"]
                resp_data = data.get("data", {})
                assert resp_data.get("error") == "restricted_path_modified"
                assert resp_data["role"] == "coder"
                assert ".egg-state/contracts/foo.json" in resp_data.get("blocked_paths", [])

    def test_tester_role_blocked_from_contracts(self, client):
        """The tester file_restrictions entry blocks .egg-state/contracts/."""
        session = _make_session("tester")
        patches = self._push_context_real_file_restrictions(
            session, [".egg-state/contracts/spec.json"]
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                assert "tester" in data["message"]
                assert ".egg-state/contracts/spec.json" in data["message"]
                resp_data = data.get("data", {})
                assert resp_data.get("error") == "restricted_path_modified"
                assert resp_data["role"] == "tester"
                assert ".egg-state/contracts/spec.json" in resp_data.get("blocked_paths", [])

    def test_documenter_role_blocked_from_contracts(self, client):
        """The documenter file_restrictions entry blocks .egg-state/contracts/."""
        session = _make_session("documenter")
        patches = self._push_context_real_file_restrictions(
            session, [".egg-state/contracts/x.json"]
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                assert "documenter" in data["message"]
                assert ".egg-state/contracts/x.json" in data["message"]
                resp_data = data.get("data", {})
                assert resp_data.get("error") == "restricted_path_modified"
                assert resp_data["role"] == "documenter"
                assert ".egg-state/contracts/x.json" in resp_data.get("blocked_paths", [])
