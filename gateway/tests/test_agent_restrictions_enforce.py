"""Tests for agent-role restriction enforcement behavior.

Validates the EGG_AGENT_RESTRICTIONS_ENFORCE flag controlling whether
agent-role file restriction violations are handled by the #2039
restricted-path-modify rejection (enforce mode) or only logged as
warnings (warn-only mode). Enforce mode is the default.

With #2039, enforce-mode violations return a structured 403:

- All own-files allowed     → plain push, 200 ``filtered=false``
- Any own-file blocked      → 403 ``error=restricted_path_modified``
  with ``role`` / ``blocked_paths`` / ``recommended_action`` in the
  response body, pointing the agent at the conditional-ACK pattern
  (#1998).  This replaces the prior #1882 silent-strip + nothing_to_push
  arms, which produced destructive deletions on the shared branch.

Phase, anchor, protected-file, and branch-ownership checks also return
403 — those are unaffected by #2039.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import git_client
import pytest
import session_manager
from git_client import AttributedFile, AttributedPushRange
from policy import PolicyResult
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import SessionValidationResult

import gateway

# A valid 40-char SHA stand-in used for every mocked attributed commit.
_FAKE_SHA = "a" * 40


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


def _make_coder_session():
    """Create a mock session with 'coder' agent role."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = "coder"
    mock_session.phase = None
    return mock_session


def _build_attributed_range(changed_files, session_role):
    """Build an AttributedPushRange where every file is own-authored.

    The gateway treats ``authored_by == session_role`` as own-authored,
    which is what the #1882 auto-filter needs to see in order to run
    ``partition_files_by_role`` against the push diff.
    """
    return AttributedPushRange(
        files=[
            AttributedFile(
                path=p,
                commit_sha=_FAKE_SHA,
                authored_by=session_role,
            )
            for p in changed_files
        ],
        commits=[_FAKE_SHA],
        attribution={_FAKE_SHA: session_role},
    )


def _push_context(mock_session, agent_blocked=True):
    """Return a context manager that sets up all mocking for a push request.

    Args:
        mock_session: Mock session object.
        agent_blocked: If True, the mocked push diff contains a file the
            ``coder`` role cannot write (driving the auto-filter into the
            all-blocked branch).  If False, the file is one the role can
            write (plain push).
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

    # Choose a file that drives the desired partition outcome.  The
    # ``coder`` role cannot write ``tests/`` (per CODER_PATTERNS), but
    # can write ``src/`` — use those to get blocked/allowed respectively
    # regardless of which role the mock session advertises.
    if agent_blocked:
        changed_files = ["tests/test_foo.py"]
    else:
        changed_files = ["src/foo.py"]

    session_role_for_attribution = mock_session.agent_role or "coder"

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
        patch.object(gateway, "get_changed_files_in_push", return_value=(changed_files, None)),
        # Legacy ``gateway.check_file_restrictions`` patch removed in
        # #2489 — the gateway no longer calls that function from
        # ``git_push`` (see ``test_filtered_push_blocked_modify``).
        patch.object(
            git_client,
            "get_attributed_changed_files_in_push",
            return_value=_build_attributed_range(changed_files, session_role_for_attribution),
        ),
    )


def _do_push(client):
    """Send a push request and return the response."""
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


def _assert_auto_filtered_all_blocked(response, expected_role, expected_blocked_file):
    """Assert the #2039 restricted-path-modified rejection shape.

    (Function name kept for callers; behavior flipped from the prior
    #1882 silent-strip + nothing_to_push assertion to the new 403.)
    """
    assert response.status_code == 403, response.data
    body = json.loads(response.data)
    assert body["success"] is False, body
    data = body.get("data") or {}
    assert data.get("error") == "restricted_path_modified", body
    assert data.get("role") == expected_role, body
    blocked = data.get("blocked_paths") or []
    assert expected_blocked_file in blocked, body
    assert data.get("recommended_action"), body


class TestAgentRestrictionsWarnOnly:
    """Explicit warn-only mode: violations logged but push proceeds."""

    def test_warn_mode_allows_push(self, client):
        """With EGG_AGENT_RESTRICTIONS_ENFORCE=false, violations allow push."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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

    def test_warn_mode_accepts_0_value(self, client):
        """EGG_AGENT_RESTRICTIONS_ENFORCE=0 works as warn-only."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "0"}):
                response = _do_push(client)
                assert response.status_code == 200

    def test_warn_mode_accepts_no_value(self, client):
        """EGG_AGENT_RESTRICTIONS_ENFORCE=no works as warn-only."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "no"}):
                response = _do_push(client)
                assert response.status_code == 200

    def test_enforce_mode_is_default(self, client):
        """Without the env var set, enforce mode is used (auto-filter engages)."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            # Remove the env var entirely — default should enforce
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EGG_AGENT_RESTRICTIONS_ENFORCE", None)
                response = _do_push(client)
                _assert_auto_filtered_all_blocked(response, "coder", "tests/test_foo.py")


class TestAgentRestrictionsEnforceMode:
    """Enforce mode: violations trigger the #2039 restricted-path-modified rejection."""

    def test_enforce_mode_auto_filters_push(self, client):
        """EGG_AGENT_RESTRICTIONS_ENFORCE=true: blocked path → 403 restricted_path_modified."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
                _assert_auto_filtered_all_blocked(response, "coder", "tests/test_foo.py")

    def test_enforce_mode_allows_clean_push(self, client):
        """Enforce mode allows push when agent restrictions pass."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=False)

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

    def test_enforce_accepts_yes_value(self, client):
        """EGG_AGENT_RESTRICTIONS_ENFORCE=yes works as enforce."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "yes"}):
                response = _do_push(client)
                _assert_auto_filtered_all_blocked(response, "coder", "tests/test_foo.py")

    def test_enforce_accepts_1_value(self, client):
        """EGG_AGENT_RESTRICTIONS_ENFORCE=1 works as enforce."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "1"}):
                response = _do_push(client)
                _assert_auto_filtered_all_blocked(response, "coder", "tests/test_foo.py")


class TestAgentRestrictionsUnknownRole:
    """Unknown agent roles get every file blocked (deny-by-default)."""

    def test_unknown_role_passes_when_allowed(self, client):
        """Unknown role + allowed-only file → still rejected as blocked.

        ``partition_files_by_role`` returns ``([], files)`` for unknown
        roles (deny-by-default), so even a "clean" file is blocked.  The
        test asserts the gateway recognises this and returns 403
        ``restricted_path_modified`` (#2039).
        """
        session = _make_coder_session()
        session.agent_role = "unknown_role"
        patches = _push_context(session, agent_blocked=False)

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
                _assert_auto_filtered_all_blocked(response, "unknown_role", "src/foo.py")


class TestAgentRestrictionsNoRole:
    """Sessions without agent_role skip agent restriction checks."""

    def test_no_role_skips_check(self, client):
        """Sessions without agent_role bypass the auto-filter entirely."""
        session = _make_coder_session()
        session.agent_role = None
        patches = _push_context(session, agent_blocked=True)

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
                # Should succeed because agent_role is None, so the check is skipped
                assert response.status_code == 200


def _push_context_real_check(mock_session, changed_files):
    """Like _push_context but drives the real ``partition_files_by_role``.

    Used for #1901 end-to-end push scenarios.  The new #1882 code path no
    longer calls ``check_agent_restrictions`` — it runs
    ``partition_files_by_role`` against the own-authored subset of
    ``get_attributed_changed_files_in_push``.  To exercise that real
    partitioning logic we must still mock the attribution lookup so
    every file is tagged with the session's own role (else the gateway
    cannot enumerate commits and fails closed on the empty range).
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

    attributed = _build_attributed_range(changed_files, mock_session.agent_role)

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
        patch.object(gateway, "get_changed_files_in_push", return_value=(changed_files, None)),
        # Legacy ``gateway.check_file_restrictions`` patch removed in
        # #2489 — the gateway no longer calls that function from
        # ``git_push`` (see ``test_filtered_push_blocked_modify``).
        patch.object(
            git_client,
            "get_attributed_changed_files_in_push",
            return_value=attributed,
        ),
        # Intentionally NOT mocking partition_files_by_role — drive the
        # real #1882 partition path against the pushing role's patterns.
    )


def _make_role_session(role):
    """Create a mock session with the given agent role."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role
    mock_session.phase = None
    return mock_session


class TestCoderEndToEndPushRejection1901:
    """#1901 end-to-end auto-filter path for session_role='coder'.

    These tests drive the real ``partition_files_by_role`` to confirm
    CODER_PATTERNS, the auto-filter no-op short-circuit, and the
    gateway's response shaping all agree — the file-attribution and
    session validation are mocked, everything else is real.
    """

    def _coder_session(self):
        mock_session = MagicMock()
        mock_session.mode = "public"
        mock_session.container_id = "test-container"
        mock_session.expires_at = None
        mock_session.agent_role = "coder"
        mock_session.phase = None
        return mock_session

    def test_coder_can_push_extensionless_bin_egg(self, client):
        """bin/egg was previously blocked under the legacy allowlist; now allowed."""
        session = self._coder_session()
        patches = _push_context_real_check(session, ["bin/egg"])
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
                assert response.status_code == 200, response.data

    def test_coder_docs_md_gets_auto_filtered(self, client):
        """docs/*.md in a coder push → 403 restricted_path_modified."""
        session = self._coder_session()
        patches = _push_context_real_check(session, ["docs/x.md"])
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
                _assert_auto_filtered_all_blocked(response, "coder", "docs/x.md")

    def test_coder_tests_get_auto_filtered(self, client):
        """tests/*.py in a coder push → 403 restricted_path_modified."""
        session = self._coder_session()
        patches = _push_context_real_check(session, ["tests/test_x.py"])
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
                _assert_auto_filtered_all_blocked(response, "coder", "tests/test_x.py")

    def test_coder_contracts_get_auto_filtered(self, client):
        """.egg-state/contracts/*.json in a coder push → 403 restricted_path_modified."""
        session = self._coder_session()
        patches = _push_context_real_check(session, [".egg-state/contracts/foo.json"])
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
                _assert_auto_filtered_all_blocked(
                    response, "coder", ".egg-state/contracts/foo.json"
                )


class TestTesterEndToEndPushRejection1901:
    """#1901 end-to-end auto-filter path for session_role='tester'."""

    def test_tester_can_push_test_files(self, client):
        """Tester is allowed to push test files."""
        session = _make_role_session("tester")
        patches = _push_context_real_check(session, ["tests/test_foo.py"])
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
                assert response.status_code == 200, response.data

    def test_tester_source_code_gets_auto_filtered(self, client):
        """Tester cannot push source code → 403 restricted_path_modified."""
        session = _make_role_session("tester")
        patches = _push_context_real_check(session, ["shared/egg_restrictions/patterns.py"])
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
                _assert_auto_filtered_all_blocked(
                    response, "tester", "shared/egg_restrictions/patterns.py"
                )

    def test_tester_docs_get_auto_filtered(self, client):
        """Tester cannot push documentation → 403 restricted_path_modified."""
        session = _make_role_session("tester")
        patches = _push_context_real_check(session, ["docs/guide.md"])
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
                _assert_auto_filtered_all_blocked(response, "tester", "docs/guide.md")


class TestDocumenterEndToEndPushRejection1901:
    """#1901 end-to-end auto-filter path for session_role='documenter'."""

    def test_documenter_can_push_docs(self, client):
        """Documenter is allowed to push documentation files."""
        session = _make_role_session("documenter")
        patches = _push_context_real_check(session, ["docs/guide.md"])
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
                assert response.status_code == 200, response.data

    def test_documenter_source_code_gets_auto_filtered(self, client):
        """Documenter cannot push source code → 403 restricted_path_modified."""
        session = _make_role_session("documenter")
        patches = _push_context_real_check(session, ["gateway/auth.py"])
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
                _assert_auto_filtered_all_blocked(response, "documenter", "gateway/auth.py")

    def test_documenter_tests_get_auto_filtered(self, client):
        """Documenter cannot push test files → 403 restricted_path_modified."""
        session = _make_role_session("documenter")
        patches = _push_context_real_check(session, ["tests/test_x.py"])
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
                _assert_auto_filtered_all_blocked(response, "documenter", "tests/test_x.py")
