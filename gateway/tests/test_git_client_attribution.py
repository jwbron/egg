"""Tests for get_attributed_changed_files_in_push() and related helpers.

These tests validate the attribution layer that sits on top of
get_changed_files_in_push: for each file in a push range, record the
commit SHA that introduced the file and the role that authored the
commit (via the commit-authorship registry).

The function is security-critical: on any detection or registry
failure we must fail closed (empty files + error, or None
authored_by per file so the caller treats it as own-authored).
"""

import subprocess
import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports (matches test_git_client.py pattern).
sys.path.insert(0, str(Path(__file__).parent.parent))

from git_client import (  # noqa: E402
    AttributedFile,
    AttributedPushRange,
    get_attributed_changed_files_in_push,
)

# ---------------------------------------------------------------------------
# subprocess.run fake factory
# ---------------------------------------------------------------------------


def make_run(responses):
    """Create a subprocess.run fake driven by matcher/response tuples.

    Args:
        responses: list of ``(matcher_fn, CompletedProcess)`` tuples. The
            first matcher whose ``matcher_fn(cmd)`` is truthy wins. Falls
            back to ``returncode=0`` with empty stdout/stderr.

    Returns:
        ``(fake_run, calls)`` — ``calls`` is a list that records every
        command issued (useful for asserting call order).
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        for matcher, result in responses:
            if matcher(cmd):
                return result
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    return fake_run, calls


def _cp(returncode=0, stdout="", stderr=""):
    """Shorthand for subprocess.CompletedProcess()."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _cmd_contains(cmd, *needles):
    """True when each needle appears as an exact element of cmd."""
    return all(n in cmd for n in needles)


# ---------------------------------------------------------------------------
# Fake registry client
# ---------------------------------------------------------------------------


class FakeRegistryClient:
    """Minimal registry client stub with a recordable lookup_bulk()."""

    def __init__(self, result=None, raise_exc=None):
        self._result = result if result is not None else {}
        self._raise_exc = raise_exc
        self.calls: list[list[str]] = []

    def lookup_bulk(self, shas):
        self.calls.append(list(shas))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAttributedDataclasses:
    """Sanity checks for the two public dataclasses."""

    def test_attributed_file_defaults_authored_by_to_none(self):
        af = AttributedFile(path="a.py", commit_sha="abc")
        assert af.path == "a.py"
        assert af.commit_sha == "abc"
        assert af.authored_by is None

    def test_attributed_file_accepts_role(self):
        af = AttributedFile(path="a.py", commit_sha="abc", authored_by="coder")
        assert af.authored_by == "coder"

    def test_attributed_push_range_defaults(self):
        r = AttributedPushRange()
        assert r.files == []
        assert r.commits == []
        assert r.attribution == {}
        assert r.error is None


class TestHappyPath:
    """The simple case: rev-list succeeds, diff-tree succeeds, registry returns roles."""

    def test_happy_path_single_commit(self, monkeypatch):
        registry = FakeRegistryClient(result={"abc1111": "coder"})
        responses = [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            (
                lambda cmd: "rev-list" in cmd
                and "origin/branch..HEAD" in " ".join(cmd),
                _cp(returncode=0, stdout="abc1111\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd and "abc1111" in cmd,
                _cp(returncode=0, stdout="file1.py\nfile2.py\n"),
            ),
        ]
        fake_run, _ = make_run(responses)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=registry
        )

        assert result.error is None
        assert result.commits == ["abc1111"]
        assert len(result.files) == 2
        for f in result.files:
            assert f.commit_sha == "abc1111"
            assert f.authored_by == "coder"
        paths = sorted(f.path for f in result.files)
        assert paths == ["file1.py", "file2.py"]
        assert result.attribution == {"abc1111": "coder"}
        assert registry.calls == [["abc1111"]]

    def test_happy_path_multi_commit(self, monkeypatch):
        registry = FakeRegistryClient(
            result={"abc1111": "coder", "def2222": "tester", "1234567": "coder"}
        )
        responses = [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            (
                lambda cmd: "rev-list" in cmd
                and "origin/feature..HEAD" in " ".join(cmd),
                _cp(returncode=0, stdout="abc1111\ndef2222\n1234567\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd and "abc1111" in cmd,
                _cp(returncode=0, stdout="src/a.py\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd and "def2222" in cmd,
                _cp(returncode=0, stdout="tests/test_a.py\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd and "1234567" in cmd,
                _cp(returncode=0, stdout="src/b.py\nsrc/a.py\n"),
            ),
        ]
        fake_run, _ = make_run(responses)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "feature", registry_client=registry
        )

        assert result.error is None
        assert result.commits == ["abc1111", "def2222", "1234567"]
        # Each AttributedFile must carry the exact SHA whose diff-tree
        # output emitted it, tagged with that SHA's role.
        by_path = {(f.path, f.commit_sha): f.authored_by for f in result.files}
        assert by_path[("src/a.py", "abc1111")] == "coder"
        assert by_path[("tests/test_a.py", "def2222")] == "tester"
        assert by_path[("src/b.py", "1234567")] == "coder"
        assert by_path[("src/a.py", "1234567")] == "coder"
        assert registry.calls == [["abc1111", "def2222", "1234567"]]


class TestRegistryUnavailable:
    """When the registry fails (exception or empty map), every file is None."""

    def test_registry_lookup_bulk_raises_fails_closed(self, monkeypatch):
        registry = FakeRegistryClient(raise_exc=RuntimeError("registry down"))
        responses = [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            (
                lambda cmd: "rev-list" in cmd
                and "origin/branch..HEAD" in " ".join(cmd),
                _cp(returncode=0, stdout="abc1111\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd,
                _cp(returncode=0, stdout="a.py\nb.py\n"),
            ),
        ]
        fake_run, _ = make_run(responses)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=registry
        )

        # The function swallows the exception internally and records the
        # empty attribution; the push handler (the caller) is the one
        # that actually fails closed. Here we just assert the signal.
        assert result.error is None
        assert result.attribution == {}
        assert len(result.files) == 2
        for f in result.files:
            assert f.authored_by is None

    def test_registry_returns_empty_dict_marks_all_none(self, monkeypatch):
        registry = FakeRegistryClient(result={})
        responses = [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            (
                lambda cmd: "rev-list" in cmd
                and "origin/branch..HEAD" in " ".join(cmd),
                _cp(returncode=0, stdout="feedcab\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd,
                _cp(returncode=0, stdout="x.py\n"),
            ),
        ]
        fake_run, _ = make_run(responses)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=registry
        )

        assert result.error is None
        assert result.attribution == {}
        assert len(result.files) == 1
        assert result.files[0].authored_by is None

    def test_partial_attribution_tags_each_file_independently(self, monkeypatch):
        # Only sha1 is in the registry; sha2 is missing.
        registry = FakeRegistryClient(result={"abc1111": "coder"})
        responses = [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            (
                lambda cmd: "rev-list" in cmd
                and "origin/branch..HEAD" in " ".join(cmd),
                _cp(returncode=0, stdout="abc1111\ndef2222\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd and "abc1111" in cmd,
                _cp(returncode=0, stdout="a.py\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd and "def2222" in cmd,
                _cp(returncode=0, stdout="b.py\n"),
            ),
        ]
        fake_run, _ = make_run(responses)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=registry
        )

        by_path = {f.path: f.authored_by for f in result.files}
        assert by_path["a.py"] == "coder"
        assert by_path["b.py"] is None
        assert result.attribution == {"abc1111": "coder"}


class TestDetectionFailures:
    """diff-tree / rev-list errors fail closed with error set on the range."""

    def test_diff_tree_error_fails_closed_with_commits_retained(self, monkeypatch):
        registry = FakeRegistryClient(result={"abc1111": "coder"})
        responses = [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            (
                lambda cmd: "rev-list" in cmd
                and "origin/branch..HEAD" in " ".join(cmd),
                _cp(returncode=0, stdout="abc1111\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd,
                _cp(returncode=128, stderr="fatal: bad object"),
            ),
        ]
        fake_run, _ = make_run(responses)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=registry
        )

        assert result.error is not None
        assert "diff-tree failed" in result.error
        assert result.files == []
        # commits list is retained for audit logging
        assert result.commits == ["abc1111"]
        # Registry was never consulted when diff-tree failed
        assert registry.calls == []

    def test_rev_list_failure_all_paths_fails_closed(self, monkeypatch):
        responses = [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            # Every rev-list and merge-base call fails.
            (
                lambda cmd: "rev-list" in cmd,
                _cp(returncode=128, stderr="fatal: bad revision"),
            ),
            (
                lambda cmd: "merge-base" in cmd,
                _cp(returncode=128, stderr="fatal: not a valid object"),
            ),
        ]
        fake_run, _ = make_run(responses)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=FakeRegistryClient()
        )

        assert result.files == []
        assert result.commits == []
        assert result.error is not None
        assert "Could not determine" in result.error


class TestNewBranchFallback:
    """When origin/<branch> doesn't exist, the function falls back to merge-base."""

    def test_new_branch_falls_back_to_merge_base_main(self, monkeypatch):
        registry = FakeRegistryClient(result={"abc1111": "coder"})

        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "fetch" in cmd:
                return _cp(returncode=0)
            # Primary rev-list against origin/branch fails (new branch).
            if "rev-list" in cmd and "origin/branch..HEAD" in cmd_str:
                return _cp(returncode=128, stderr="fatal: bad revision")
            # merge-base with main succeeds.
            if "merge-base" in cmd and "origin/main" in cmd:
                return _cp(returncode=0, stdout="f0e0d0c\n")
            # Fallback rev-list against the fork point.
            if "rev-list" in cmd and "f0e0d0c..HEAD" in cmd_str:
                return _cp(returncode=0, stdout="abc1111\n")
            if "diff-tree" in cmd:
                return _cp(returncode=0, stdout="new_file.py\n")
            return _cp(returncode=128)

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=registry
        )

        assert result.error is None
        assert result.commits == ["abc1111"]
        assert len(result.files) == 1
        assert result.files[0].path == "new_file.py"
        assert result.files[0].commit_sha == "abc1111"
        assert result.files[0].authored_by == "coder"

    def test_new_branch_falls_back_to_merge_base_master(self, monkeypatch):
        registry = FakeRegistryClient(result={"def2222": "tester"})

        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "fetch" in cmd:
                return _cp(returncode=0)
            # Primary rev-list against origin/branch fails.
            if "rev-list" in cmd and "origin/branch..HEAD" in cmd_str:
                return _cp(returncode=128)
            # merge-base with main fails...
            if "merge-base" in cmd and "origin/main" in cmd:
                return _cp(returncode=128, stderr="fatal: no such ref")
            # ...merge-base with master succeeds.
            if "merge-base" in cmd and "origin/master" in cmd:
                return _cp(returncode=0, stdout="1a2b3c4\n")
            # Fallback rev-list
            if "rev-list" in cmd and "1a2b3c4..HEAD" in cmd_str:
                return _cp(returncode=0, stdout="def2222\n")
            if "diff-tree" in cmd:
                return _cp(returncode=0, stdout="README.md\n")
            return _cp(returncode=128)

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=registry
        )

        assert result.error is None
        assert result.commits == ["def2222"]
        assert result.files[0].path == "README.md"
        assert result.files[0].commit_sha == "def2222"
        assert result.files[0].authored_by == "tester"


class TestEmptyRange:
    """rev-list returning nothing is legal: no commits → empty range, no error."""

    def test_empty_commit_range_returns_empty_range(self, monkeypatch):
        registry = FakeRegistryClient(result={})
        responses = [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            (
                lambda cmd: "rev-list" in cmd
                and "origin/branch..HEAD" in " ".join(cmd),
                _cp(returncode=0, stdout=""),
            ),
        ]
        fake_run, _ = make_run(responses)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=registry
        )

        assert result.error is None
        assert result.commits == []
        assert result.files == []
        assert result.attribution == {}
        # Registry is NOT consulted on an empty range.
        assert registry.calls == []


class TestSessionRoleIsAdvisory:
    """session_role is for audit logging only; it does not filter output."""

    def _minimal_responses(self, commits_stdout="abc1111\n", diff_stdout="f.py\n"):
        return [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            (
                lambda cmd: "rev-list" in cmd
                and "origin/branch..HEAD" in " ".join(cmd),
                _cp(returncode=0, stdout=commits_stdout),
            ),
            (
                lambda cmd: "diff-tree" in cmd,
                _cp(returncode=0, stdout=diff_stdout),
            ),
        ]

    def test_session_role_does_not_change_output(self, monkeypatch):
        # Same fixture, different session_role — identical output.
        fake_run_a, _ = make_run(self._minimal_responses())
        monkeypatch.setattr(subprocess, "run", fake_run_a)
        result_coder = get_attributed_changed_files_in_push(
            "/fake/repo",
            "origin",
            "branch",
            session_role="coder",
            registry_client=FakeRegistryClient(result={"abc1111": "coder"}),
        )

        fake_run_b, _ = make_run(self._minimal_responses())
        monkeypatch.setattr(subprocess, "run", fake_run_b)
        result_tester = get_attributed_changed_files_in_push(
            "/fake/repo",
            "origin",
            "branch",
            session_role="tester",
            registry_client=FakeRegistryClient(result={"abc1111": "coder"}),
        )

        assert [(f.path, f.commit_sha, f.authored_by) for f in result_coder.files] == [
            (f.path, f.commit_sha, f.authored_by) for f in result_tester.files
        ]
        assert result_coder.commits == result_tester.commits
        assert result_coder.attribution == result_tester.attribution


class TestRegistryClientResolution:
    """When registry_client=None the function lazy-imports get_client()."""

    def test_registry_client_none_uses_singleton(self, monkeypatch):
        responses = [
            (lambda cmd: "fetch" in cmd, _cp(returncode=0)),
            (
                lambda cmd: "rev-list" in cmd
                and "origin/branch..HEAD" in " ".join(cmd),
                _cp(returncode=0, stdout="abc1111\n"),
            ),
            (
                lambda cmd: "diff-tree" in cmd,
                _cp(returncode=0, stdout="q.py\n"),
            ),
        ]
        fake_run, _ = make_run(responses)
        monkeypatch.setattr(subprocess, "run", fake_run)

        # Install a fake commit_registry_client module so the lazy import
        # inside the function picks it up.
        import types

        singleton = FakeRegistryClient(result={"abc1111": "coder"})
        fake_module = types.ModuleType("commit_registry_client")

        get_client_calls = {"count": 0}

        def get_client():
            get_client_calls["count"] += 1
            return singleton

        fake_module.get_client = get_client
        monkeypatch.setitem(sys.modules, "commit_registry_client", fake_module)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch"
        )

        assert get_client_calls["count"] == 1
        assert result.error is None
        assert result.files[0].authored_by == "coder"
        assert singleton.calls == [["abc1111"]]


class TestFetchFailureTolerated:
    """The best-effort fetch may raise; rev-list must still run."""

    def test_fetch_oserror_is_swallowed(self, monkeypatch):
        registry = FakeRegistryClient(result={"abc1111": "coder"})

        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "fetch" in cmd:
                raise OSError("fetch blew up")
            if "rev-list" in cmd and "origin/branch..HEAD" in cmd_str:
                return _cp(returncode=0, stdout="abc1111\n")
            if "diff-tree" in cmd:
                return _cp(returncode=0, stdout="z.py\n")
            return _cp(returncode=128)

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_attributed_changed_files_in_push(
            "/fake/repo", "origin", "branch", registry_client=registry
        )

        assert result.error is None
        assert result.commits == ["abc1111"]
        assert len(result.files) == 1
        assert result.files[0].authored_by == "coder"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
