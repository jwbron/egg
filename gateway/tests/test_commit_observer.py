"""Tests for gateway/commit_observer.py (issue #1882, TASK-5-4).

The observer runs inline inside the gateway's ``/api/v1/git/execute``
handler: snapshot HEAD before the inner git call, snapshot HEAD after,
and POST each new SHA to the orchestrator's commit-authorship registry
via the shared-secret-authenticated client.

Covered:

- HEAD snapshot before / after
- multi-commit detection (cherry-pick of N fires register_bulk)
- best-effort on registry-unavailable (logs + returns; never raises)
- non-agent session skip (``session_role=None``)
- no-op when ``before_head == after_head`` (non-mutating op)
- unborn-branch (no before_head) registers just ``after_head``
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_gateway_path = Path(__file__).parent.parent
if str(_gateway_path) not in sys.path:
    sys.path.insert(0, str(_gateway_path))

from commit_observer import (  # type: ignore[import-not-found]
    capture_head,
    observe,
    observe_after_git_execute,
)


class _FakeClient:
    """Collecting stand-in for CommitRegistryClient."""

    def __init__(self, *, register_ok: bool = True, bulk_ok: bool = True):
        self.register_calls: list[dict] = []
        self.bulk_calls: list[list[dict]] = []
        self.register_ok = register_ok
        self.bulk_ok = bulk_ok

    def register(self, *, sha, role, pipeline_id, repo, branch):
        self.register_calls.append(
            {
                "sha": sha,
                "role": role,
                "pipeline_id": pipeline_id,
                "repo": repo,
                "branch": branch,
            }
        )
        return self.register_ok

    def register_bulk(self, items):
        self.bulk_calls.append(list(items))
        return self.bulk_ok


# ---------------------------------------------------------------------------
# Non-agent sessions
# ---------------------------------------------------------------------------


class TestNonAgentSession:
    def test_observe_with_no_role_returns_empty(self):
        client = _FakeClient()
        result = observe(
            "/tmp",
            before_head="a" * 40,
            after_head="b" * 40,
            branch="main",
            session_role=None,
            pipeline_id="issue-1882",
            repo="jwbron/egg",
            registry_client=client,
        )
        assert result == []
        assert client.register_calls == []
        assert client.bulk_calls == []

    def test_observe_with_empty_role_returns_empty(self):
        client = _FakeClient()
        result = observe(
            "/tmp",
            before_head="a" * 40,
            after_head="b" * 40,
            branch="main",
            session_role="",
            pipeline_id="issue-1882",
            repo="jwbron/egg",
            registry_client=client,
        )
        assert result == []
        assert client.register_calls == []

    def test_observe_after_git_execute_with_no_role_returns_empty(self):
        client = _FakeClient()
        result = observe_after_git_execute(
            "/tmp",
            before_head="a" * 40,
            branch="main",
            session_role=None,
            pipeline_id="issue-1882",
            repo=None,
            registry_client=client,
        )
        assert list(result) == []


# ---------------------------------------------------------------------------
# No-op HEAD transitions
# ---------------------------------------------------------------------------


class TestNoChange:
    def test_before_equals_after_is_noop(self):
        client = _FakeClient()
        sha = "a" * 40
        result = observe(
            "/tmp",
            before_head=sha,
            after_head=sha,
            branch="main",
            session_role="coder",
            pipeline_id="issue-1882",
            repo="jwbron/egg",
            registry_client=client,
        )
        assert result == []
        assert client.register_calls == []

    def test_missing_after_is_noop(self):
        client = _FakeClient()
        result = observe(
            "/tmp",
            before_head="a" * 40,
            after_head=None,
            branch="main",
            session_role="coder",
            pipeline_id="issue-1882",
            repo="jwbron/egg",
            registry_client=client,
        )
        assert result == []


# ---------------------------------------------------------------------------
# Single-commit path — uses ``register``
# ---------------------------------------------------------------------------


class TestSingleCommit:
    def test_single_commit_fires_register(self, monkeypatch):
        """One new commit → one register() call with the session role."""
        before = "a" * 40
        after = "b" * 40

        def fake_rev_list(*args, **kwargs):
            # rev-list before..after returns exactly one SHA
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=f"{after}\n",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", fake_rev_list)
        client = _FakeClient()
        result = observe(
            "/repo",
            before_head=before,
            after_head=after,
            branch="egg/issue-1882",
            session_role="coder",
            pipeline_id="issue-1882",
            repo="jwbron/egg",
            registry_client=client,
        )
        assert result == [after]
        assert len(client.register_calls) == 1
        assert client.register_calls[0]["sha"] == after
        assert client.register_calls[0]["role"] == "coder"
        assert client.register_calls[0]["pipeline_id"] == "issue-1882"
        assert client.register_calls[0]["branch"] == "egg/issue-1882"
        # No bulk call on the single-commit path.
        assert client.bulk_calls == []

    def test_unborn_branch_registers_after_head_only(self):
        """``before_head=None`` (no prior HEAD) registers just ``after_head``."""
        client = _FakeClient()
        after = "a" * 40
        result = observe(
            "/repo",
            before_head=None,
            after_head=after,
            branch="egg/issue-1882",
            session_role="coder",
            pipeline_id="issue-1882",
            repo="jwbron/egg",
            registry_client=client,
        )
        # No rev-list walk → no subprocess → falls back to [after_head].
        assert result == [after]
        assert len(client.register_calls) == 1
        assert client.register_calls[0]["sha"] == after


# ---------------------------------------------------------------------------
# Multi-commit path — uses ``register_bulk``
# ---------------------------------------------------------------------------


class TestMultiCommit:
    def test_multi_commit_fires_register_bulk(self, monkeypatch):
        """N new commits → one register_bulk() with all of them."""
        before = "a" * 40
        new_shas = ["b" * 40, "c" * 40, "d" * 40]

        def fake_rev_list(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="\n".join(new_shas) + "\n",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", fake_rev_list)
        client = _FakeClient()
        result = observe(
            "/repo",
            before_head=before,
            after_head=new_shas[-1],
            branch="egg/issue-1882",
            session_role="coder",
            pipeline_id="issue-1882",
            repo="jwbron/egg",
            registry_client=client,
        )
        assert result == new_shas
        assert client.register_calls == []
        assert len(client.bulk_calls) == 1
        items = client.bulk_calls[0]
        assert len(items) == 3
        assert [i["sha"] for i in items] == new_shas
        for item in items:
            assert item["role"] == "coder"
            assert item["pipeline_id"] == "issue-1882"
            assert item["repo"] == "jwbron/egg"

    def test_rev_list_nonzero_falls_back_to_after_head(self, monkeypatch):
        """If rev-list fails (e.g. rewritten history), register just the tip."""
        before = "a" * 40
        after = "b" * 40

        def fake_rev_list(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args,
                returncode=128,
                stdout="",
                stderr="fatal: ambiguous argument\n",
            )

        monkeypatch.setattr(subprocess, "run", fake_rev_list)
        client = _FakeClient()
        result = observe(
            "/repo",
            before_head=before,
            after_head=after,
            branch="egg/issue-1882",
            session_role="coder",
            pipeline_id="issue-1882",
            repo="jwbron/egg",
            registry_client=client,
        )
        assert result == [after]
        assert len(client.register_calls) == 1

    def test_rev_list_timeout_returns_empty(self, monkeypatch):
        """Subprocess timeout is logged and swallowed."""

        def fake_rev_list(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=10)

        monkeypatch.setattr(subprocess, "run", fake_rev_list)
        client = _FakeClient()
        result = observe(
            "/repo",
            before_head="a" * 40,
            after_head="b" * 40,
            branch="main",
            session_role="coder",
            pipeline_id="issue-1882",
            repo=None,
            registry_client=client,
        )
        assert result == []
        assert client.register_calls == []
        assert client.bulk_calls == []


# ---------------------------------------------------------------------------
# Best-effort: registry errors must never propagate
# ---------------------------------------------------------------------------


class TestRegistryUnavailable:
    def test_register_exception_is_swallowed(self, monkeypatch):
        """A RuntimeError from client.register() is logged and swallowed."""

        def fake_rev_list(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="b" * 40 + "\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_rev_list)

        class BoomClient:
            def register(self, **_kw):
                raise RuntimeError("simulated network failure")

            def register_bulk(self, _items):
                raise RuntimeError("also broken")

        client = BoomClient()
        # Must not raise.
        result = observe(
            "/repo",
            before_head="a" * 40,
            after_head="b" * 40,
            branch="main",
            session_role="coder",
            pipeline_id="issue-1882",
            repo=None,
            registry_client=client,
        )
        assert result == ["b" * 40]

    def test_register_bulk_exception_is_swallowed(self, monkeypatch):
        """register_bulk failure is logged; observe still returns the SHA list."""

        def fake_rev_list(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="\n".join(["b" * 40, "c" * 40]) + "\n",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", fake_rev_list)

        class BoomClient:
            def register(self, **_kw):  # pragma: no cover
                pass

            def register_bulk(self, _items):
                raise RuntimeError("kaboom")

        client = BoomClient()
        result = observe(
            "/repo",
            before_head="a" * 40,
            after_head="c" * 40,
            branch="main",
            session_role="coder",
            pipeline_id="issue-1882",
            repo=None,
            registry_client=client,
        )
        assert result == ["b" * 40, "c" * 40]


# ---------------------------------------------------------------------------
# capture_head
# ---------------------------------------------------------------------------


class TestCaptureHead:
    def test_capture_head_returns_sha(self, monkeypatch):
        sha = "a" * 40

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=sha + "\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert capture_head("/repo") == sha

    def test_capture_head_returns_none_on_unborn_branch(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=128, stdout="", stderr="fatal: no HEAD\n"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert capture_head("/repo") is None

    def test_capture_head_returns_none_on_exception(self, monkeypatch):
        def fake_run(*_a, **_kw):
            raise OSError("boom")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert capture_head("/repo") is None

    def test_capture_head_returns_none_on_empty_output(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert capture_head("/repo") is None


# ---------------------------------------------------------------------------
# observe_after_git_execute wrapper
# ---------------------------------------------------------------------------


class TestObserveAfterGitExecute:
    def test_captures_after_head_internally(self, monkeypatch):
        """The wrapper calls capture_head() itself to get after_head."""
        before = "a" * 40
        after = "b" * 40

        # Two fake subprocesses: capture_head returns ``after``, then
        # rev-list returns the single-commit range.
        call_count = {"n": 0}

        def fake_run(args, *_a, **_kw):
            call_count["n"] += 1
            if "rev-parse" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=after + "\n", stderr=""
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=after + "\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = _FakeClient()
        result = list(
            observe_after_git_execute(
                "/repo",
                before_head=before,
                branch="main",
                session_role="coder",
                pipeline_id="issue-1882",
                repo=None,
                registry_client=client,
            )
        )
        assert result == [after]
        assert len(client.register_calls) == 1

    def test_returns_empty_when_capture_head_fails(self, monkeypatch):
        """rev-parse failure → wrapper returns empty; no register."""

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=128, stdout="", stderr="no HEAD\n"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = _FakeClient()
        result = list(
            observe_after_git_execute(
                "/repo",
                before_head="a" * 40,
                branch="main",
                session_role="coder",
                pipeline_id="issue-1882",
                repo=None,
                registry_client=client,
            )
        )
        assert result == []
        assert client.register_calls == []
