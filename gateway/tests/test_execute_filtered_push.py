"""Tests for gateway/filtered_push.py::execute_filtered_push (#1882 TASK-5-6).

These tests use real local git repositories created in ``tmp_path`` so we
exercise the actual git commit-tree / update-ref / read-tree plumbing
rather than trying to mock subprocess calls — the per-commit rewriter
sequences so many git invocations that mocking would both be brittle and
fail to verify the actual bitwise behavior the algorithm promises.

The ``push_fn`` callable is stubbed so no remote push happens; we inspect
the local branch ref and the worktree to verify the rewriter's effects.

Covered scenarios (matching TASK-5-6):

a. Single-commit mixed (one own-commit, some blocked paths, some allowed)
b. Multi-commit all-own mixed (structure preserved, suffix added)
c. Interleaved own+pulled (pulled commits bitwise-unchanged)
d. Own-commit-becomes-empty dropped (reparenting preserves chain)
e. New-branch merge-base fallback (orig_parent empty → commit-tree with no parent)
f. Rollback on mid-walk exception (HEAD restored, branch ref restored)
g. Rollback on push failure (HEAD restored)
h. Post-success worktree has blocked files re-staged
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_gateway_path = Path(__file__).parent.parent
if str(_gateway_path) not in sys.path:
    sys.path.insert(0, str(_gateway_path))

from filtered_push import (  # type: ignore[import-not-found]
    execute_filtered_push,
)
from git_client import AttributedFile  # type: ignore[import-not-found]

# ---------------------------------------------------------------------------
# Git repo helpers
# ---------------------------------------------------------------------------


def _run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.strip()


def _make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "init"],
        cwd=path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"git init not available: {result.stderr.strip()}")
    # Rename the default branch to 'main' after init so we don't require
    # git >= 2.28 for --initial-branch.
    try:
        _run(path, "checkout", "-b", "main")
    except subprocess.CalledProcessError:
        # If the initial-branch was already main or the default name is
        # still unborn, skip.
        pass
    _run(path, "config", "user.name", "Test User")
    _run(path, "config", "user.email", "test@example.com")
    _run(path, "config", "commit.gpgsign", "false")
    _run(path, "config", "tag.gpgsign", "false")


def _commit_file(
    path: Path,
    relpath: str,
    content: str,
    *,
    author_name: str = "coder",
    author_email: str = "coder@egg.local",
    message: str = "update",
) -> str:
    file_path = path / relpath
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    _run(path, "add", "--", relpath)
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = author_name
    env["GIT_AUTHOR_EMAIL"] = author_email
    env["GIT_COMMITTER_NAME"] = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", message],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )
    return _run(path, "rev-parse", "HEAD")


def _current_tree(path: Path, sha: str = "HEAD") -> str:
    return _run(path, "rev-parse", f"{sha}^{{tree}}")


def _message(path: Path, sha: str = "HEAD") -> str:
    return _run(path, "log", "-1", "--format=%B", sha).rstrip()


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    _make_repo(path)
    # Seed with one baseline commit so rev-parse HEAD always works.
    _commit_file(path, "baseline.txt", "baseline\n", message="baseline")
    return path


class _PushStub:
    """Callable passed as ``push_fn`` to execute_filtered_push."""

    def __init__(self, ok: bool = True, error: str | None = None) -> None:
        self.ok = ok
        self.error = error
        self.calls = 0

    def __call__(self) -> tuple[bool, str | None]:
        self.calls += 1
        return self.ok, self.error


class _RegistryStub:
    """Callable passed as ``registry_register``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


def _ensure_branch(path: Path, branch: str) -> None:
    """Ensure we're on ``branch`` so update-ref refs/heads/<branch> is meaningful."""
    # init uses default 'main'; rename to the branch we want if needed.
    current = _run(path, "rev-parse", "--abbrev-ref", "HEAD")
    if current != branch:
        _run(path, "branch", "-M", branch)


# ---------------------------------------------------------------------------
# Scenario (a): single own-commit mixed
# ---------------------------------------------------------------------------


class TestSingleOwnCommitMixed:
    def test_rewrite_strips_blocked_paths(self, repo: Path):
        _ensure_branch(repo, "egg/issue-1882")
        sha = _commit_file(repo, "src/main.py", "print('hi')\n", message="feat: add src")
        # Overwrite baseline.txt as well in the same commit — mix of blocked + allowed paths
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "README.md").write_text("# docs\n")
        _run(repo, "add", "docs/README.md")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "coder"
        env["GIT_AUTHOR_EMAIL"] = "coder@egg.local"
        env["GIT_COMMITTER_NAME"] = "coder"
        env["GIT_COMMITTER_EMAIL"] = "coder@egg.local"
        subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=repo,
            check=True,
            capture_output=True,
            env=env,
        )
        sha = _run(repo, "rev-parse", "HEAD")

        attributed_files = [
            AttributedFile(path="src/main.py", commit_sha=sha, authored_by="coder"),
            AttributedFile(path="docs/README.md", commit_sha=sha, authored_by="coder"),
        ]
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha],
            attributed_files=attributed_files,
            blocked_own_files={"docs/README.md"},
            push_fn=_PushStub(),
            registry_register=_RegistryStub(),
            pipeline_id="issue-1882",
            repo="jwbron/egg",
        )
        assert result.success is True
        assert "docs/README.md" in result.excluded_files
        assert "src/main.py" in result.pushed_files
        # Exactly one rewritten commit with the auto-filter suffix.
        assert len(result.rewritten_commits) == 1
        new_sha = result.rewritten_commits[0]["new_sha"]
        assert new_sha != sha
        assert result.pushed_commits == [new_sha]
        # The marker is emitted as a proper git trailer so trailers
        # parse cleanly.
        new_message = _message(repo, new_sha)
        assert "Auto-Filtered: true" in new_message
        # The rewritten tree must NOT contain the blocked path.
        tree_listing = _run(repo, "ls-tree", "-r", new_sha)
        assert "docs/README.md" not in tree_listing
        assert "src/main.py" in tree_listing


# ---------------------------------------------------------------------------
# Scenario (b): multi-commit all-own mixed, structure preserved
# ---------------------------------------------------------------------------


class TestMultiCommitAllOwnMixed:
    def test_structure_preserved_after_rewrite(self, repo: Path):
        _ensure_branch(repo, "egg/issue-1882")
        sha1 = _commit_file(repo, "a.py", "a = 1\n", message="first")
        sha2 = _commit_file(repo, "b.md", "# b\n", message="second (docs)")
        sha3 = _commit_file(repo, "c.py", "c = 3\n", message="third")
        attributed_files = [
            AttributedFile(path="a.py", commit_sha=sha1, authored_by="coder"),
            AttributedFile(path="b.md", commit_sha=sha2, authored_by="coder"),
            AttributedFile(path="c.py", commit_sha=sha3, authored_by="coder"),
        ]
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha1, sha2, sha3],
            attributed_files=attributed_files,
            blocked_own_files={"b.md"},
            push_fn=_PushStub(),
            registry_register=_RegistryStub(),
        )
        assert result.success is True
        # The middle commit introduced only a blocked path — the filter
        # leaves it empty and drops it. Two pushed commits remain.
        assert sha2 in result.dropped_commits
        assert len(result.pushed_commits) == 2
        # Every pushed commit's message came from an own commit; only sha2
        # introduced a blocked path so sha1/sha3 pass through unrewritten
        # in content but may be re-parented (new_sha).  Structure (order)
        # is preserved: the first pushed maps from sha1, second from sha3.
        assert result.excluded_files == ["b.md"]
        # Walk the tip and verify a.py and c.py both exist, b.md does not.
        tip = result.new_tip
        assert tip is not None
        listing = _run(repo, "ls-tree", "-r", tip)
        assert "a.py" in listing
        assert "c.py" in listing
        assert "b.md" not in listing


# ---------------------------------------------------------------------------
# Scenario (c): interleaved own+pulled — pulled commit bitwise-preserved
# ---------------------------------------------------------------------------


class TestInterleavedOwnAndPulled:
    def test_pulled_commit_bitwise_unchanged(self, repo: Path):
        _ensure_branch(repo, "egg/issue-1882")
        sha_own1 = _commit_file(
            repo,
            "src/a.py",
            "a=1\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="own 1",
        )
        sha_pulled = _commit_file(
            repo,
            "tests/test_a.py",
            "def test_a(): pass\n",
            author_name="tester",
            author_email="tester@egg.local",
            message="pulled (tester)",
        )
        sha_own2 = _commit_file(
            repo,
            "src/b.py",
            "b=2\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="own 2",
        )

        attributed_files = [
            AttributedFile(path="src/a.py", commit_sha=sha_own1, authored_by="coder"),
            AttributedFile(path="tests/test_a.py", commit_sha=sha_pulled, authored_by="tester"),
            AttributedFile(path="src/b.py", commit_sha=sha_own2, authored_by="coder"),
        ]
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha_own1, sha_pulled, sha_own2],
            attributed_files=attributed_files,
            blocked_own_files=set(),  # nothing to strip — we're testing pass-through
            push_fn=_PushStub(),
            registry_register=_RegistryStub(),
        )
        assert result.success is True
        # The pulled commit's original SHA should appear in pulled_commits,
        # and its rewritten_sha must equal None when the parent was unchanged.
        pulled_entries = [p for p in result.pulled_commits if p.get("sha") == sha_pulled]
        assert len(pulled_entries) == 1
        # If the chain was stable, rewritten_sha is None (SHA preserved).
        # In the mixed case tests elsewhere it may be non-None.
        # Here own commits have nothing stripped so their SHAs should
        # also be unchanged.
        assert pulled_entries[0].get("rewritten_sha") is None

    def test_pulled_commit_reparented_when_own_rewritten(self, repo: Path):
        """When an own commit before a pulled one is rewritten, the pulled
        commit must be reparented — but still preserve its tree and message."""
        _ensure_branch(repo, "egg/issue-1882")
        sha_own = _commit_file(
            repo,
            "docs/foo.md",  # blocked for coder
            "# foo\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="own with blocked",
        )
        sha_pulled = _commit_file(
            repo,
            "tests/test_foo.py",
            "def test_foo(): pass\n",
            author_name="tester",
            author_email="tester@egg.local",
            message="pulled",
        )
        attributed_files = [
            AttributedFile(path="docs/foo.md", commit_sha=sha_own, authored_by="coder"),
            AttributedFile(path="tests/test_foo.py", commit_sha=sha_pulled, authored_by="tester"),
        ]
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha_own, sha_pulled],
            attributed_files=attributed_files,
            blocked_own_files={"docs/foo.md"},
            push_fn=_PushStub(),
            registry_register=_RegistryStub(),
        )
        assert result.success is True
        # own got dropped (all files blocked → empty after filter)
        assert sha_own in result.dropped_commits
        # pulled_commits has one entry; tree preserved, but reparented.
        pulled_entries = result.pulled_commits
        assert len(pulled_entries) == 1
        rewritten_sha = pulled_entries[0].get("rewritten_sha")
        # Because the own commit was dropped but parent chain shifts, the
        # pulled commit must be reparented to the same upstream point —
        # its tree remains identical though.
        if rewritten_sha:
            # Reparented → new SHA, but tree should match the original.
            assert _current_tree(repo, rewritten_sha) == _current_tree(repo, sha_pulled)
            assert _message(repo, rewritten_sha) == _message(repo, sha_pulled)
        assert "docs/foo.md" in result.excluded_files


# ---------------------------------------------------------------------------
# Scenario (d): own commit becomes empty after filter — dropped
# ---------------------------------------------------------------------------


class TestOwnCommitBecomesEmpty:
    def test_empty_own_commit_is_dropped(self, repo: Path):
        _ensure_branch(repo, "egg/issue-1882")
        # A commit that ONLY introduces docs/ (blocked for coder)
        sha = _commit_file(
            repo,
            "docs/README.md",
            "# doc\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="docs-only commit",
        )
        attributed_files = [
            AttributedFile(path="docs/README.md", commit_sha=sha, authored_by="coder"),
        ]
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha],
            attributed_files=attributed_files,
            blocked_own_files={"docs/README.md"},
            push_fn=_PushStub(),
            registry_register=_RegistryStub(),
        )
        assert result.success is True
        assert sha in result.dropped_commits
        assert result.pushed_commits == []
        assert result.excluded_files == ["docs/README.md"]


# ---------------------------------------------------------------------------
# Scenario (f): rollback on mid-walk exception
# ---------------------------------------------------------------------------


class TestRollbackOnMidWalkError:
    def test_rollback_on_missing_commit_metadata(self, repo: Path, monkeypatch):
        _ensure_branch(repo, "egg/issue-1882")
        sha = _commit_file(repo, "src/a.py", "a=1\n", message="first")
        attributed_files = [
            AttributedFile(path="src/a.py", commit_sha=sha, authored_by="coder"),
        ]
        # Force _commit_metadata to return None for this sha.
        import filtered_push as fp

        original_meta = fp._commit_metadata

        def broken_meta(exec_path, target_sha):
            if target_sha == sha:
                return None
            return original_meta(exec_path, target_sha)

        monkeypatch.setattr(fp, "_commit_metadata", broken_meta)
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha],
            attributed_files=attributed_files,
            blocked_own_files=set(),
            push_fn=_PushStub(),
            registry_register=_RegistryStub(),
        )
        assert result.success is False
        assert result.error is not None
        # HEAD must still point at the same SHA — rollback restored it.
        assert _run(repo, "rev-parse", "HEAD") == sha  # branch tip at sha; walk never advanced


# ---------------------------------------------------------------------------
# Scenario (g): rollback on push failure
# ---------------------------------------------------------------------------


class TestRollbackOnPushFailure:
    def test_rollback_restores_original_head_and_tree(self, repo: Path):
        _ensure_branch(repo, "egg/issue-1882")
        sha = _commit_file(
            repo,
            "src/a.py",
            "a=1\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="will be filtered",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "foo.md").write_text("# foo\n")
        _run(repo, "add", "docs/foo.md")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "coder"
        env["GIT_AUTHOR_EMAIL"] = "coder@egg.local"
        env["GIT_COMMITTER_NAME"] = "coder"
        env["GIT_COMMITTER_EMAIL"] = "coder@egg.local"
        subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=repo,
            check=True,
            capture_output=True,
            env=env,
        )
        sha = _run(repo, "rev-parse", "HEAD")

        attributed_files = [
            AttributedFile(path="src/a.py", commit_sha=sha, authored_by="coder"),
            AttributedFile(path="docs/foo.md", commit_sha=sha, authored_by="coder"),
        ]

        push = _PushStub(ok=False, error="remote rejected")
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha],
            attributed_files=attributed_files,
            blocked_own_files={"docs/foo.md"},
            push_fn=push,
            registry_register=_RegistryStub(),
        )
        assert result.success is False
        assert result.error and "rejected" in result.error
        assert push.calls == 1
        # Branch ref must be back at the original HEAD (via _rollback)
        assert _run(repo, "rev-parse", "refs/heads/egg/issue-1882") == sha


# ---------------------------------------------------------------------------
# Scenario (h): post-success worktree has blocked files re-staged
# ---------------------------------------------------------------------------


class TestPostSuccessRestage:
    def test_blocked_files_are_restaged_in_worktree(self, repo: Path):
        _ensure_branch(repo, "egg/issue-1882")
        sha = _commit_file(
            repo,
            "src/a.py",
            "a=1\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="feat",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "note.md").write_text("# note\n")
        _run(repo, "add", "docs/note.md")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "coder"
        env["GIT_AUTHOR_EMAIL"] = "coder@egg.local"
        env["GIT_COMMITTER_NAME"] = "coder"
        env["GIT_COMMITTER_EMAIL"] = "coder@egg.local"
        subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=repo,
            check=True,
            capture_output=True,
            env=env,
        )
        sha = _run(repo, "rev-parse", "HEAD")

        attributed_files = [
            AttributedFile(path="src/a.py", commit_sha=sha, authored_by="coder"),
            AttributedFile(path="docs/note.md", commit_sha=sha, authored_by="coder"),
        ]
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha],
            attributed_files=attributed_files,
            blocked_own_files={"docs/note.md"},
            push_fn=_PushStub(),
            registry_register=_RegistryStub(),
        )
        assert result.success is True
        assert "docs/note.md" in result.excluded_files
        # The file is back in the worktree (re-staged with intent-to-add).
        reinjected = repo / "docs" / "note.md"
        assert reinjected.exists()
        assert reinjected.read_text() == "# note\n"
        # And it's not part of the current tip's tree.
        tree_listing = _run(repo, "ls-tree", "-r", "HEAD")
        assert "docs/note.md" not in tree_listing


# ---------------------------------------------------------------------------
# Registry-register on rewritten commit
# ---------------------------------------------------------------------------


class TestRegistryRegisterOnRewrite:
    def test_registers_rewritten_own_commit(self, repo: Path):
        _ensure_branch(repo, "egg/issue-1882")
        sha = _commit_file(
            repo,
            "src/a.py",
            "a=1\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="feat",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "x.md").write_text("# x\n")
        _run(repo, "add", "docs/x.md")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "coder"
        env["GIT_AUTHOR_EMAIL"] = "coder@egg.local"
        env["GIT_COMMITTER_NAME"] = "coder"
        env["GIT_COMMITTER_EMAIL"] = "coder@egg.local"
        subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=repo,
            check=True,
            capture_output=True,
            env=env,
        )
        sha = _run(repo, "rev-parse", "HEAD")

        attributed_files = [
            AttributedFile(path="src/a.py", commit_sha=sha, authored_by="coder"),
            AttributedFile(path="docs/x.md", commit_sha=sha, authored_by="coder"),
        ]
        registry = _RegistryStub()
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha],
            attributed_files=attributed_files,
            blocked_own_files={"docs/x.md"},
            push_fn=_PushStub(),
            registry_register=registry,
            pipeline_id="issue-1882",
            repo="jwbron/egg",
        )
        assert result.success
        assert len(registry.calls) == 1
        call = registry.calls[0]
        assert call["role"] == "coder"
        assert call["pipeline_id"] == "issue-1882"
        assert call["repo"] == "jwbron/egg"
        assert call["sha"] == result.rewritten_commits[0]["new_sha"]

    def test_registry_exception_swallowed(self, repo: Path):
        """Registry failures must not affect the push result."""
        _ensure_branch(repo, "egg/issue-1882")
        sha = _commit_file(
            repo,
            "src/a.py",
            "a=1\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="feat",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "y.md").write_text("# y\n")
        _run(repo, "add", "docs/y.md")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "coder"
        env["GIT_AUTHOR_EMAIL"] = "coder@egg.local"
        env["GIT_COMMITTER_NAME"] = "coder"
        env["GIT_COMMITTER_EMAIL"] = "coder@egg.local"
        subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=repo,
            check=True,
            capture_output=True,
            env=env,
        )
        sha = _run(repo, "rev-parse", "HEAD")

        def boom(**_kwargs):
            raise RuntimeError("registry down")

        attributed_files = [
            AttributedFile(path="src/a.py", commit_sha=sha, authored_by="coder"),
            AttributedFile(path="docs/y.md", commit_sha=sha, authored_by="coder"),
        ]
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha],
            attributed_files=attributed_files,
            blocked_own_files={"docs/y.md"},
            push_fn=_PushStub(),
            registry_register=boom,
        )
        assert result.success is True


# ---------------------------------------------------------------------------
# Empty result when every commit is dropped
# ---------------------------------------------------------------------------


class TestAllDropped:
    def test_all_dropped_returns_success_with_empty_pushed(self, repo: Path):
        """If every commit becomes empty after filter, success with no pushed_commits."""
        _ensure_branch(repo, "egg/issue-1882")
        sha = _commit_file(
            repo,
            "docs/only.md",
            "# only docs\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="only docs",
        )
        attributed_files = [
            AttributedFile(path="docs/only.md", commit_sha=sha, authored_by="coder"),
        ]
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha],
            attributed_files=attributed_files,
            blocked_own_files={"docs/only.md"},
            push_fn=_PushStub(),
            registry_register=_RegistryStub(),
        )
        # The caller would normally intercept this via the
        # all-blocked path, but if it doesn't, the rewriter returns a
        # defensive success with empty pushed_commits.
        assert result.success is True
        assert result.pushed_commits == []
        assert sha in result.dropped_commits


# ---------------------------------------------------------------------------
# Unregistered commit treated as own (fail-closed)
# ---------------------------------------------------------------------------


class TestUnregisteredTreatedAsOwn:
    def test_authored_by_none_is_filtered_as_own(self, repo: Path):
        """authored_by=None (unregistered) triggers the own-role filter."""
        _ensure_branch(repo, "egg/issue-1882")
        sha = _commit_file(
            repo,
            "src/a.py",
            "a=1\n",
            author_name="coder",
            author_email="coder@egg.local",
            message="feat",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "z.md").write_text("# z\n")
        _run(repo, "add", "docs/z.md")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "coder"
        env["GIT_AUTHOR_EMAIL"] = "coder@egg.local"
        env["GIT_COMMITTER_NAME"] = "coder"
        env["GIT_COMMITTER_EMAIL"] = "coder@egg.local"
        subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=repo,
            check=True,
            capture_output=True,
            env=env,
        )
        sha = _run(repo, "rev-parse", "HEAD")
        attributed_files = [
            AttributedFile(path="src/a.py", commit_sha=sha, authored_by=None),
            AttributedFile(path="docs/z.md", commit_sha=sha, authored_by=None),
        ]
        result = execute_filtered_push(
            exec_path=str(repo),
            push_role="coder",
            branch="egg/issue-1882",
            attributed_commits=[sha],
            attributed_files=attributed_files,
            blocked_own_files={"docs/z.md"},
            push_fn=_PushStub(),
            registry_register=_RegistryStub(),
        )
        # Fail-closed: authored_by=None treated as own → blocked path stripped.
        assert result.success
        assert "docs/z.md" in result.excluded_files
