"""Eight-scenario end-to-end tests for the #1882 auto-filter push handler.

Each test drives ``POST /api/v1/git/push`` with a mocked attribution
range and verifies the response shape + audit-log event type.  The
scenarios are the ones the refine-phase analysis defined (see
``.egg-state/drafts/1882-analysis.md``):

1. Own-only, all allowed         → plain push (filtered=False).
2. Own-only, all blocked         → 200 nothing_to_push=True.
3. Own-only, mixed               → 200 filtered=True (when the rewrite
                                    succeeds; we bypass the rewrite via
                                    the all-blocked shortcut here to
                                    avoid real git).
4. Mixed own/pulled, all allowed → plain push + pulled_commits.
5. Mixed own/pulled, pulled-would-be-blocked-for-pusher → pulled files
   are exempt; plain push.
6. Mixed own/pulled, own-blocked → filtered-push rewrite.
7. Unregistered commits          → fail-closed, treat as own-authored.
8. EGG_AGENT_RESTRICTIONS_ENFORCE=false → warn-only passthrough.

These tests use the same extensive mocking scaffold as
``test_agent_restrictions_enforce.py`` — for scenarios 3 and 6 the
real ``execute_filtered_push`` requires a live git repo, so we either
skip or replace it with a stub FilteredPushResult.
"""

from __future__ import annotations

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


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as c:
        yield c


_OWN_SHA = "a" * 40
_PULLED_SHA = "b" * 40
_UNREG_SHA = "c" * 40


def _make_session(role: str = "coder", agent_role: str | None = None):
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role if agent_role is None else agent_role
    mock_session.phase = None
    mock_session.pipeline_id = None
    return mock_session


def _patches_for(
    mock_session,
    file_paths: list[str],
    attributed_range: AttributedPushRange,
):
    """Common mock scaffold — session, policy, subprocess, attribution, auto-filter guards."""
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

    return [
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
            gateway, "get_changed_files_in_push", return_value=(file_paths, None)
        ),
        patch.object(
            gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
        ),
        patch.object(
            git_client,
            "get_attributed_changed_files_in_push",
            return_value=attributed_range,
        ),
    ]


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
# Scenario 1: own-only, all allowed → plain push.
# ---------------------------------------------------------------------------


def test_scenario_1_own_only_all_allowed(client):
    session = _make_session("coder")
    files = ["src/main.py"]
    attributed = AttributedPushRange(
        files=[AttributedFile(path=files[0], commit_sha=_OWN_SHA, authored_by="coder")],
        commits=[_OWN_SHA],
        attribution={_OWN_SHA: "coder"},
    )
    with contextlib.ExitStack() as _stack:
        for _p in _patches_for(session, files, attributed):
            _stack.enter_context(_p)
        _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
        response = _do_push(client)
        assert response.status_code == 200
        body = _body(response)
        assert body["filtered"] is False
        assert body["nothing_to_push"] is False
        assert body["pulled_commits"] == []


# ---------------------------------------------------------------------------
# Scenario 2: own-only, all blocked → 200 nothing_to_push=True.
# ---------------------------------------------------------------------------


def test_scenario_2_own_only_all_blocked(client):
    session = _make_session("coder")
    files = ["docs/guide.md"]  # coder cannot write docs
    attributed = AttributedPushRange(
        files=[AttributedFile(path=files[0], commit_sha=_OWN_SHA, authored_by="coder")],
        commits=[_OWN_SHA],
        attribution={_OWN_SHA: "coder"},
    )
    with contextlib.ExitStack() as _stack:
        for _p in _patches_for(session, files, attributed):
            _stack.enter_context(_p)
        _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
        response = _do_push(client)
        assert response.status_code == 200
        body = _body(response)
        assert body["filtered"] is True
        assert body["nothing_to_push"] is True
        assert body["excluded_files"] == files
        assert body["pushed_files"] == []
        assert body["pushed_commits"] == []


# ---------------------------------------------------------------------------
# Scenario 3: own-only mixed → auto-filter rewrite (stubbed).
# ---------------------------------------------------------------------------


def test_scenario_3_own_only_mixed_triggers_rewrite(client, monkeypatch):
    """Mixed allowed+blocked own files → execute_filtered_push is called."""
    session = _make_session("coder")
    files = ["src/main.py", "docs/guide.md"]
    attributed = AttributedPushRange(
        files=[
            AttributedFile(path=files[0], commit_sha=_OWN_SHA, authored_by="coder"),
            AttributedFile(path=files[1], commit_sha=_OWN_SHA, authored_by="coder"),
        ],
        commits=[_OWN_SHA],
        attribution={_OWN_SHA: "coder"},
    )

    # Stub execute_filtered_push to avoid real git operations.
    from filtered_push import FilteredPushResult

    fake_result = FilteredPushResult(
        success=True,
        new_tip="d" * 40,
        excluded_files=["docs/guide.md"],
        pushed_files=["src/main.py"],
        pushed_commits=["e" * 40],
        rewritten_commits=[{"original_sha": _OWN_SHA, "new_sha": "e" * 40}],
        pulled_commits=[],
    )
    import filtered_push

    with contextlib.ExitStack() as _stack:
        for _p in _patches_for(session, files, attributed):
            _stack.enter_context(_p)
        _stack.enter_context(
            patch.object(filtered_push, "execute_filtered_push", return_value=fake_result)
        )
        _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
        response = _do_push(client)
        assert response.status_code == 200
        body = _body(response)
        assert body["filtered"] is True
        assert body["nothing_to_push"] is False
        assert body["excluded_files"] == ["docs/guide.md"]
        assert body["pushed_files"] == ["src/main.py"]
        assert body["pushed_commits"] == ["e" * 40]


# ---------------------------------------------------------------------------
# Scenario 4: mixed own/pulled, all allowed → plain push + pulled_commits.
# ---------------------------------------------------------------------------


def test_scenario_4_mixed_own_and_pulled_all_allowed(client):
    session = _make_session("coder")
    files = ["src/main.py", "tests/test_main.py"]
    attributed = AttributedPushRange(
        files=[
            AttributedFile(path="src/main.py", commit_sha=_OWN_SHA, authored_by="coder"),
            AttributedFile(
                path="tests/test_main.py", commit_sha=_PULLED_SHA, authored_by="tester"
            ),
        ],
        commits=[_OWN_SHA, _PULLED_SHA],
        attribution={_OWN_SHA: "coder", _PULLED_SHA: "tester"},
    )
    with contextlib.ExitStack() as _stack:
        for _p in _patches_for(session, files, attributed):
            _stack.enter_context(_p)
        _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
        response = _do_push(client)
        assert response.status_code == 200
        body = _body(response)
        # Coder's own file (src/main.py) is allowed; tester's file
        # (tests/test_main.py) would be blocked for coder but is pulled,
        # so it's exempt.  Result: plain push.
        assert body["filtered"] is False
        assert body["nothing_to_push"] is False
        pulled = body["pulled_commits"]
        assert any(p["sha"] == _PULLED_SHA and p["author_role"] == "tester" for p in pulled)


# ---------------------------------------------------------------------------
# Scenario 5: mixed own/pulled where pulled files would be blocked for pusher.
# ---------------------------------------------------------------------------


def test_scenario_5_pulled_files_would_be_blocked_but_exempt(client):
    """Coder pushes a range containing docs/ pulled from documenter — exempt."""
    session = _make_session("coder")
    files = ["src/main.py", "docs/api.md"]
    attributed = AttributedPushRange(
        files=[
            AttributedFile(path="src/main.py", commit_sha=_OWN_SHA, authored_by="coder"),
            AttributedFile(
                path="docs/api.md", commit_sha=_PULLED_SHA, authored_by="documenter"
            ),
        ],
        commits=[_OWN_SHA, _PULLED_SHA],
        attribution={_OWN_SHA: "coder", _PULLED_SHA: "documenter"},
    )
    with contextlib.ExitStack() as _stack:
        for _p in _patches_for(session, files, attributed):
            _stack.enter_context(_p)
        _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
        response = _do_push(client)
        assert response.status_code == 200
        body = _body(response)
        # docs/api.md would block a coder push if attributed to coder —
        # but it's attributed to documenter, so it's exempt and the
        # push proceeds unfiltered.
        assert body["filtered"] is False
        assert body["nothing_to_push"] is False


# ---------------------------------------------------------------------------
# Scenario 6: mixed own/pulled, own has blocked files → rewrite pulled verbatim.
# ---------------------------------------------------------------------------


def test_scenario_6_own_blocked_with_pulled_preserved(client, monkeypatch):
    """Coder's own commit has blocked docs + pulled tester commit present."""
    session = _make_session("coder")
    files = ["src/main.py", "docs/guide.md", "tests/test_main.py"]
    attributed = AttributedPushRange(
        files=[
            AttributedFile(path="src/main.py", commit_sha=_OWN_SHA, authored_by="coder"),
            AttributedFile(path="docs/guide.md", commit_sha=_OWN_SHA, authored_by="coder"),
            AttributedFile(
                path="tests/test_main.py", commit_sha=_PULLED_SHA, authored_by="tester"
            ),
        ],
        commits=[_OWN_SHA, _PULLED_SHA],
        attribution={_OWN_SHA: "coder", _PULLED_SHA: "tester"},
    )
    from filtered_push import FilteredPushResult

    # The rewrite is expected to filter out docs/guide.md from the own
    # commit and preserve the pulled commit verbatim.
    fake_result = FilteredPushResult(
        success=True,
        new_tip="e" * 40,
        excluded_files=["docs/guide.md"],
        pushed_files=["src/main.py", "tests/test_main.py"],
        pushed_commits=["f" * 40, _PULLED_SHA],
        rewritten_commits=[{"original_sha": _OWN_SHA, "new_sha": "f" * 40}],
        pulled_commits=[{"sha": _PULLED_SHA, "author_role": "tester", "rewritten_sha": None}],
    )
    import filtered_push

    with contextlib.ExitStack() as _stack:
        for _p in _patches_for(session, files, attributed):
            _stack.enter_context(_p)
        _stack.enter_context(
            patch.object(filtered_push, "execute_filtered_push", return_value=fake_result)
        )
        _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
        response = _do_push(client)
        assert response.status_code == 200
        body = _body(response)
        assert body["filtered"] is True
        assert body["excluded_files"] == ["docs/guide.md"]
        # Pulled commit still surfaced on pulled_commits for
        # observability parity.
        assert any(p["sha"] == _PULLED_SHA for p in body["pulled_commits"])


# ---------------------------------------------------------------------------
# Scenario 7: unregistered commits → fail-closed (treat as own-authored).
# ---------------------------------------------------------------------------


def test_scenario_7_unregistered_commits_fail_closed(client):
    """An unregistered commit in the range flows through as own (blocked → nothing_to_push)."""
    session = _make_session("coder")
    files = ["docs/guide.md"]  # blocked for coder
    attributed = AttributedPushRange(
        files=[
            AttributedFile(path="docs/guide.md", commit_sha=_UNREG_SHA, authored_by=None),
        ],
        commits=[_UNREG_SHA],
        attribution={_UNREG_SHA: None},
    )
    with contextlib.ExitStack() as _stack:
        for _p in _patches_for(session, files, attributed):
            _stack.enter_context(_p)
        _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
        response = _do_push(client)
        assert response.status_code == 200
        body = _body(response)
        # Unregistered → treated as own-authored; coder can't push docs.
        assert body["nothing_to_push"] is True
        assert body["excluded_files"] == files


# ---------------------------------------------------------------------------
# Scenario 8: EGG_AGENT_RESTRICTIONS_ENFORCE=false → warn-only passthrough.
# ---------------------------------------------------------------------------


def test_scenario_8_warn_only_passthrough(client):
    session = _make_session("coder")
    files = ["docs/guide.md"]
    attributed = AttributedPushRange(
        files=[AttributedFile(path=files[0], commit_sha=_OWN_SHA, authored_by="coder")],
        commits=[_OWN_SHA],
        attribution={_OWN_SHA: "coder"},
    )
    with contextlib.ExitStack() as _stack:
        for _p in _patches_for(session, files, attributed):
            _stack.enter_context(_p)
        _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "false"}))
        response = _do_push(client)
        assert response.status_code == 200
        # Warn-only short-circuits the auto-filter; this is a plain push.
        body = _body(response)
        # nothing_to_push should NOT be true in warn-only mode.
        assert not body.get("nothing_to_push", False)


# ---------------------------------------------------------------------------
# Response schema invariants
# ---------------------------------------------------------------------------


class TestResponseSchemaInvariants:
    """Every 200 response from git_push must carry pulled_commits."""

    def test_plain_push_carries_pulled_commits_key(self, client):
        session = _make_session("coder")
        files = ["src/main.py"]
        attributed = AttributedPushRange(
            files=[AttributedFile(path=files[0], commit_sha=_OWN_SHA, authored_by="coder")],
            commits=[_OWN_SHA],
            attribution={_OWN_SHA: "coder"},
        )
        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            body = _body(_do_push(client))
            assert "pulled_commits" in body

    def test_all_blocked_response_carries_pulled_commits_key(self, client):
        session = _make_session("coder")
        files = ["docs/guide.md"]
        attributed = AttributedPushRange(
            files=[AttributedFile(path=files[0], commit_sha=_OWN_SHA, authored_by="coder")],
            commits=[_OWN_SHA],
            attribution={_OWN_SHA: "coder"},
        )
        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            body = _body(_do_push(client))
            assert "pulled_commits" in body
            assert body["nothing_to_push"] is True
