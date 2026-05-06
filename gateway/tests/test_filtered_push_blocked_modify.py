"""Tests for #2039: gateway rejects pushes that modify restricted paths.

Before #2039 the gateway's auto-filter silently stripped blocked paths
from agent commits — producing destructive deletions (and, even worse,
silently dropping any follow-up commit that tried to restore the file
from origin/main).  The fix replaces both the silent-strip and the
silent ``nothing_to_push=true`` arms with a structured 403 rejection
that points the agent at the conditional-ACK / ``--pre-merge-condition``
recovery pattern (#1998).

These tests pin the new behavior end-to-end through the Flask test
client.
"""

import contextlib
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

_FAKE_SHA = "a" * 40
_FAKE_SHA_2 = "b" * 40


def _make_session(role: str):
    s = MagicMock()
    s.mode = "public"
    s.container_id = "test-container"
    s.expires_at = None
    s.agent_role = role
    s.phase = None
    return s


def _attributed(files_per_sha: list[tuple[str, list[str], str]]) -> AttributedPushRange:
    """Build an AttributedPushRange.

    ``files_per_sha`` is a list of ``(sha, [paths], author_role)`` tuples.
    """
    files: list[AttributedFile] = []
    commits: list[str] = []
    attribution: dict[str, str] = {}
    for sha, paths, role in files_per_sha:
        if sha not in commits:
            commits.append(sha)
            attribution[sha] = role
        for p in paths:
            files.append(AttributedFile(path=p, commit_sha=sha, authored_by=role))
    return AttributedPushRange(files=files, commits=commits, attribution=attribution)


def _push_patches(session, attributed: AttributedPushRange, changed_files: list[str]):
    """Patch the surrounding gateway machinery so the push reaches the
    auto-filter branch with the supplied attribution."""
    import auth

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
        patch.object(
            current_sm,
            "validate_session_for_request",
            return_value=SessionValidationResult(valid=True, session=session),
        ),
        patch.object(
            gateway,
            "check_private_repo_access",
            return_value=PrivateRepoPolicyResult(
                allowed=True, reason="Test mode", visibility="public"
            ),
        ),
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
        patch.object(
            gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
        ),
        patch.object(
            git_client,
            "get_attributed_changed_files_in_push",
            return_value=attributed,
        ),
    )


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as c:
        yield c


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


def _assert_restricted_path_modified(
    response,
    expected_role: str,
    expected_blocked_paths: list[str],
):
    """Assert the #2039 structured 403 rejection shape."""
    assert response.status_code == 403, response.data
    body = json.loads(response.data)
    assert body["success"] is False, body
    data = body.get("data") or {}
    assert data.get("error") == "restricted_path_modified", body
    assert data.get("role") == expected_role, body
    blocked = data.get("blocked_paths") or []
    for p in expected_blocked_paths:
        assert p in blocked, body
    assert data.get("recommended_action"), body


class TestRejectsModifyToRestrictedPath:
    """Replaces the prior silent-strip behavior — push is rejected with 403."""

    def test_coder_modify_to_workflow_yaml_rejected(self, client):
        """Coder editing .github/workflows/test.yml — the original #2039 case."""
        session = _make_session("coder")
        attributed = _attributed([(_FAKE_SHA, [".github/workflows/test.yml"], "coder")])
        patches = _push_patches(session, attributed, [".github/workflows/test.yml"])
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)
        _assert_restricted_path_modified(response, "coder", [".github/workflows/test.yml"])

    def test_recommended_action_points_at_pre_merge_condition(self, client):
        """The 403 body must mention the supported recovery pattern (#1998)."""
        session = _make_session("coder")
        attributed = _attributed([(_FAKE_SHA, ["docs/x.md"], "coder")])
        patches = _push_patches(session, attributed, ["docs/x.md"])
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)
        body = json.loads(response.data)
        action = (body.get("data") or {}).get("recommended_action", "")
        # Must point the agent at the supported pattern, not just say "denied".
        assert "pre-merge-condition" in action.lower() or "pre_merge_condition" in action.lower(), (
            body
        )


class TestRejectsRevertOfStrippedPath:
    """The #2039 update finding: a follow-up commit that touches the same
    restricted path (e.g. ``git checkout origin/main -- <path>``) must
    also be rejected — not silently dropped.  Under Option A the
    behavior is identical to the first push: 403 with the same body."""

    def test_revert_commit_to_restricted_path_rejected(self, client):
        session = _make_session("coder")
        # Single commit attempting to "restore" .github/workflows/test.yml.
        attributed = _attributed([(_FAKE_SHA_2, [".github/workflows/test.yml"], "coder")])
        patches = _push_patches(session, attributed, [".github/workflows/test.yml"])
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)
        _assert_restricted_path_modified(response, "coder", [".github/workflows/test.yml"])


class TestMixedCommitAllOrNothing:
    """A commit that touches BOTH allowed and restricted paths must be
    rejected entirely — no partial apply that lands the allowed half
    and silently drops the restricted half."""

    def test_mixed_paths_in_single_commit_rejected(self, client):
        session = _make_session("coder")
        attributed = _attributed(
            [(_FAKE_SHA, ["src/main.py", ".github/workflows/test.yml"], "coder")]
        )
        patches = _push_patches(session, attributed, ["src/main.py", ".github/workflows/test.yml"])
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)
        _assert_restricted_path_modified(response, "coder", [".github/workflows/test.yml"])
        # The allowed path must NOT appear in pushed_files or any
        # success-flavored field — the entire push is rejected.
        body = json.loads(response.data)
        data = body.get("data") or {}
        assert "pushed_files" not in data or not data.get("pushed_files"), body
        assert "pushed_commits" not in data or not data.get("pushed_commits"), body

    def test_mixed_across_separate_commits_rejected(self, client):
        """Two commits in the push range — one all-allowed, one
        all-blocked — still rejected as a whole."""
        session = _make_session("coder")
        attributed = _attributed(
            [
                (_FAKE_SHA, ["src/main.py"], "coder"),
                (_FAKE_SHA_2, [".github/workflows/test.yml"], "coder"),
            ]
        )
        patches = _push_patches(session, attributed, ["src/main.py", ".github/workflows/test.yml"])
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)
        _assert_restricted_path_modified(response, "coder", [".github/workflows/test.yml"])


class TestAllBlockedPushRejected:
    """The previous ``nothing_to_push=true success=true`` arm is gone — a
    push whose entire diff is restricted is now rejected, identical to
    the mixed case from the agent's perspective."""

    def test_all_paths_blocked_push_rejected(self, client):
        session = _make_session("coder")
        attributed = _attributed([(_FAKE_SHA, ["docs/x.md"], "coder")])
        patches = _push_patches(session, attributed, ["docs/x.md"])
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)
        _assert_restricted_path_modified(response, "coder", ["docs/x.md"])
        # No nothing_to_push=true success.
        body = json.loads(response.data)
        assert body["success"] is False, body
        assert (body.get("data") or {}).get("nothing_to_push") is not True, body


class TestWarnOnlyModeStillBypasses:
    """``EGG_AGENT_RESTRICTIONS_ENFORCE=false`` keeps the kill-switch
    semantics: violations log but the push proceeds."""

    def test_warn_only_does_not_reject(self, client):
        session = _make_session("coder")
        attributed = _attributed([(_FAKE_SHA, ["docs/x.md"], "coder")])
        patches = _push_patches(session, attributed, ["docs/x.md"])
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "false"}))
            response = _do_push(client)
        assert response.status_code == 200, response.data


class TestNoRoleSkipsCheck:
    """Sessions without ``agent_role`` (e.g. infrastructure pushes) are
    unaffected — same as before #2039."""

    def test_no_role_passes_through(self, client):
        session = _make_session("coder")
        session.agent_role = None
        attributed = _attributed([(_FAKE_SHA, ["docs/x.md"], "coder")])
        patches = _push_patches(session, attributed, ["docs/x.md"])
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)
        assert response.status_code == 200, response.data


class TestPulledCommitsDoNotTrigger403:
    """Regression for #2489: when the diff range contains commits authored
    by *other* roles (e.g. an agent's per-role branch picked up upstream
    work from the shared work branch), those commits' file paths must NOT
    count against the pushing role's allowlist.  Only the role's own
    commits' paths can trigger the rejection.

    Before #2489 a duplicate naive ``check_file_restrictions`` call ran
    before the attribution-aware path and rejected any restricted path
    in the whole-push diff, which trapped role-restricted producers
    (e.g. risk_analyst) whose branches inherited unrelated upstream
    commits — the role had no sanctioned recovery path.
    """

    def test_pulled_blocked_paths_do_not_block_push(self, client):
        """risk_analyst pushes its own clean commit; the diff range also
        contains an upstream commit (authored by ``architect``) that
        touches ``docs/`` and ``gateway/``.  The push must succeed —
        pulled commits are exempt from the pushing role's allowlist."""
        session = _make_session("risk_analyst")
        attributed = _attributed(
            [
                # Inherited upstream commit authored by another role —
                # touches paths that risk_analyst cannot write.
                (
                    _FAKE_SHA,
                    [
                        "docs/architecture/orchestrator.md",
                        "gateway/checkpoint_handler.py",
                    ],
                    "architect",
                ),
                # The risk_analyst's own commit — only touches its
                # allowlist (``.egg-state/agent-outputs/``).
                (
                    _FAKE_SHA_2,
                    [".egg-state/agent-outputs/2474-risk_analyst-output.json"],
                    "risk_analyst",
                ),
            ]
        )
        patches = _push_patches(
            session,
            attributed,
            [
                "docs/architecture/orchestrator.md",
                "gateway/checkpoint_handler.py",
                ".egg-state/agent-outputs/2474-risk_analyst-output.json",
            ],
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)
        assert response.status_code == 200, response.data
        body = json.loads(response.data)
        assert body["success"] is True, body
        # Pulled cross-role commits are surfaced for observability.
        data = body.get("data") or {}
        pulled = data.get("pulled_commits") or []
        assert any(p.get("author_role") == "architect" for p in pulled), body
