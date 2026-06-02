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
    patch_id_for_commit,
    patch_ids_for_commits,
)


class _FakeClient:
    """Collecting stand-in for CommitRegistryClient."""

    def __init__(self, *, register_ok: bool = True, bulk_ok: bool = True):
        self.register_calls: list[dict] = []
        self.bulk_calls: list[list[dict]] = []
        self.register_ok = register_ok
        self.bulk_ok = bulk_ok

    def register(self, *, sha, role, pipeline_id, repo, branch, patch_id=None):
        self.register_calls.append(
            {
                "sha": sha,
                "role": role,
                "pipeline_id": pipeline_id,
                "repo": repo,
                "branch": branch,
                "patch_id": patch_id,
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
            repo="owner/repo",
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
            repo="owner/repo",
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
            repo="owner/repo",
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
            repo="owner/repo",
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
            repo="owner/repo",
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
            repo="owner/repo",
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
            repo="owner/repo",
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
            assert item["repo"] == "owner/repo"

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
            repo="owner/repo",
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
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="\n", stderr="")

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


# ---------------------------------------------------------------------------
# patch-id capture (#2932)
# ---------------------------------------------------------------------------


class TestPatchIdRegistration:
    """The observer records ``git patch-id`` so attribution survives a rebase."""

    def _fake_run(self, *, commits, patch_id_out):
        def run(cmd, **kwargs):
            if "rev-list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=commits, stderr="")
            if "show" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="diff --git a/x b/x\n+y\n", stderr=""
                )
            if "patch-id" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=patch_id_out, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return run

    def test_single_commit_registers_patch_id(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            self._fake_run(commits="newsha\n", patch_id_out="abcd1234 newsha\n"),
        )
        client = _FakeClient()
        observe(
            "/repo",
            before_head="oldsha",
            after_head="newsha",
            branch="egg/b",
            session_role="coder",
            pipeline_id="issue-1",
            repo="o/r",
            registry_client=client,
        )
        assert client.register_calls[0]["patch_id"] == "abcd1234"

    def test_bulk_commits_register_patch_id(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            self._fake_run(commits="sha1\nsha2\n", patch_id_out="ffff0000 x\n"),
        )
        client = _FakeClient()
        observe(
            "/repo",
            before_head="old",
            after_head="sha2",
            branch="b",
            session_role="coder",
            pipeline_id="issue-1",
            repo="o/r",
            registry_client=client,
        )
        items = client.bulk_calls[0]
        assert len(items) == 2
        assert all(it["patch_id"] == "ffff0000" for it in items)

    def test_patch_id_for_commit_parses_first_token(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            self._fake_run(commits="", patch_id_out="deadbeef0001 abc\n"),
        )
        assert patch_id_for_commit("/repo", "abc") == "deadbeef0001"

    def test_patch_id_for_commit_empty_output_is_none(self, monkeypatch):
        # Merge / empty / rename-only commit -> no diff -> no patch-id.
        monkeypatch.setattr(subprocess, "run", self._fake_run(commits="", patch_id_out=""))
        assert patch_id_for_commit("/repo", "abc") is None

    def test_patch_id_for_commit_show_failure_is_none(self, monkeypatch):
        def run(cmd, **kwargs):
            if "show" in cmd:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="bad object")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        assert patch_id_for_commit("/repo", "abc") is None


class TestPatchIdsForCommitsBulk:
    """Direct coverage for ``patch_ids_for_commits`` — the bulk helper shared
    between observer (registration) and gateway/git_client (push-time recovery).

    Fixture-width note: the placeholders here (``"aaaaaaaa"`` for patch-ids,
    ``"sha1"`` / ``"sha2"`` for commit SHAs) are short for readability — the
    helper does not validate widths internally. In production, ``git patch-id
    --stable`` emits a 40-hex (SHA-1) or 64-hex (SHA-256) digest, and commit
    SHAs are 40-hex / 64-hex; the route-level ``_PATCH_ID_RE`` enforces those
    widths at the registration seam (see ``test_commit_authorship_routes``)."""

    def test_parses_pairs_and_correlates_by_sha(self, monkeypatch):
        # git log -p emits diffs for the requested SHAs; git patch-id --stable
        # emits one "<patch-id> <commit-sha>" pair per input patch.  Caller must
        # correlate by SHA to fill the result dict.
        def run(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="diffstub\n", stderr="")
            if "patch-id" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="aaaaaaaa sha1\nbbbbbbbb sha2\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        result = patch_ids_for_commits("/repo", ["sha1", "sha2"])
        assert result == {"sha1": "aaaaaaaa", "sha2": "bbbbbbbb"}

    def test_unknown_sha_in_patch_id_output_is_dropped(self, monkeypatch):
        # git output that references a SHA we did not ask about (or a stray
        # token line) must not poison the result dict — the input SHAs are
        # the only valid keys.
        def run(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="diffstub\n", stderr="")
            if "patch-id" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="aaaaaaaa sha1\ncccccccc shaUNKNOWN\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        result = patch_ids_for_commits("/repo", ["sha1", "sha2"])
        # sha1 matched; sha2 absent from output -> None; shaUNKNOWN ignored.
        assert result == {"sha1": "aaaaaaaa", "sha2": None}

    def test_malformed_patch_id_line_is_skipped(self, monkeypatch):
        # Lines with fewer than two tokens (e.g. a stray header) must be
        # skipped rather than crashing the parse.
        def run(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="diffstub\n", stderr="")
            if "patch-id" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="lonely\naaaaaaaa sha1\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        result = patch_ids_for_commits("/repo", ["sha1"])
        assert result == {"sha1": "aaaaaaaa"}

    def test_deduplicates_input_shas(self, monkeypatch):
        # Caller may pass duplicates; the result dict has one entry per
        # distinct SHA and we don't double-list it on the git log argv.
        captured: list[list[str]] = []

        def run(cmd, **kwargs):
            captured.append(list(cmd))
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="diffstub\n", stderr="")
            if "patch-id" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="aaaaaaaa sha1\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        result = patch_ids_for_commits("/repo", ["sha1", "sha1", " sha1 ", ""])
        assert result == {"sha1": "aaaaaaaa"}
        log_cmd = next(c for c in captured if "log" in c)
        assert log_cmd.count("sha1") == 1

    def test_empty_input_short_circuits(self, monkeypatch):
        calls: list[list[str]] = []

        def run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        assert patch_ids_for_commits("/repo", []) == {}
        # No git invocation should happen on an empty/whitespace-only input.
        assert calls == []
        assert patch_ids_for_commits("/repo", ["", "   "]) == {}
        assert calls == []

    def test_log_failure_returns_all_none(self, monkeypatch):
        # git log nonzero -> every input SHA maps to None so the caller
        # (push handler) leaves the commits fail-closed.
        def run(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="bad rev")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        result = patch_ids_for_commits("/repo", ["sha1", "sha2"])
        assert result == {"sha1": None, "sha2": None}

    def test_log_empty_stdout_returns_all_none(self, monkeypatch):
        # Empty diff stream -> nothing to feed patch-id -> keep all None.
        def run(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        result = patch_ids_for_commits("/repo", ["sha1", "sha2"])
        assert result == {"sha1": None, "sha2": None}

    def test_patch_id_nonzero_returns_all_none(self, monkeypatch):
        # git log succeeded but patch-id rejected -> still all None.
        def run(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="diff\n", stderr="")
            if "patch-id" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        result = patch_ids_for_commits("/repo", ["sha1"])
        assert result == {"sha1": None}

    def test_subprocess_exception_returns_all_none(self, monkeypatch):
        # Any subprocess raise (TimeoutExpired, OSError, …) -> all None,
        # never propagated; the caller stays fail-closed.
        def run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        monkeypatch.setattr(subprocess, "run", run)
        result = patch_ids_for_commits("/repo", ["sha1", "sha2"])
        assert result == {"sha1": None, "sha2": None}

    def test_git_cmd_injection_applies_hardened_argv(self, monkeypatch):
        # The gateway's push-time recovery path passes its hardened argv
        # builder (safe.directory / hooks-path / gc) — verify the prefix
        # actually lands on the git invocations.
        captured: list[list[str]] = []

        def fake_git_cmd(*args: str) -> list[str]:
            return [
                "git",
                "-c",
                "safe.directory=*",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "gc.auto=0",
                *args,
            ]

        def run(cmd, **kwargs):
            captured.append(list(cmd))
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="diff\n", stderr="")
            if "patch-id" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="aaaaaaaa sha1\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        result = patch_ids_for_commits("/repo", ["sha1"], git_cmd=fake_git_cmd)
        assert result == {"sha1": "aaaaaaaa"}
        # Both subprocess.run calls used the hardened prefix.
        assert len(captured) == 2
        for cmd in captured:
            assert cmd[:7] == [
                "git",
                "-c",
                "safe.directory=*",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "gc.auto=0",
            ]

    def test_default_git_cmd_uses_plain_git(self, monkeypatch):
        # No git_cmd -> plain "git ..." (the observer's own registration path).
        captured: list[list[str]] = []

        def run(cmd, **kwargs):
            captured.append(list(cmd))
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="diff\n", stderr="")
            if "patch-id" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="aaaaaaaa sha1\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", run)
        patch_ids_for_commits("/repo", ["sha1"])
        assert all(cmd[0] == "git" for cmd in captured)
        for cmd in captured:
            assert "safe.directory=*" not in cmd
