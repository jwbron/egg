"""Tests for orchestrator/commit_authorship_store.py (issue #1882).

Covers the TASK-5-1 acceptance criteria:

- round-trip (register + lookup)
- idempotent re-register (same role = no-op)
- first-wins re-register with a different role (collision, original preserved)
- bulk lookup (hit / miss / partial)
- two-thread concurrent writes
- per-pipeline sharding
- state-branch commit on write

The store talks to a real git worktree through the StateStore plumbing
when one is attached; these unit tests exercise the pure-filesystem mode
(``worktree_dir=...``) so the test matrix runs without spinning up a
real state branch.  A separate test covers the commit-on-write hook
using a stub StateStore.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# conftest already inserts orchestrator/ on sys.path, but explicit is
# fine for test discovery tooling that imports this file directly.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from commit_authorship_store import (  # type: ignore[import-not-found]
    ORPHAN_SHARD_ID,
    SUBSTORE_DIR,
    AuthorshipCollisionError,
    CommitAuthorshipStore,
    CommitAuthorshipStoreError,
    reset_singleton,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_store_singleton():
    """Ensure the module-level singleton never leaks across tests."""
    reset_singleton()
    yield
    reset_singleton()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """Return a fresh, isolated worktree directory for each test."""
    return tmp_path / "state-worktree"


@pytest.fixture
def store(worktree: Path) -> CommitAuthorshipStore:
    """Return a store backed by a per-test filesystem worktree."""
    return CommitAuthorshipStore(worktree_dir=worktree)


_VALID_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_THIRD_SHA = "c" * 40


# ---------------------------------------------------------------------------
# Happy-path round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_register_then_lookup_returns_role(self, store: CommitAuthorshipStore):
        """A freshly registered sha is returned from lookup."""
        normalized, inserted, existing = store.register(_VALID_SHA, "coder", "issue-1882")
        assert normalized == _VALID_SHA
        assert inserted is True
        assert existing is None
        assert store.lookup(_VALID_SHA) == "coder"

    def test_register_persists_to_disk(self, store: CommitAuthorshipStore, worktree: Path):
        """The shard file is written under .egg-state/commit-authorship/."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        shard = worktree / SUBSTORE_DIR / "issue-1882.json"
        assert shard.exists()
        data = json.loads(shard.read_text())
        assert data["version"] == 1
        assert _VALID_SHA in data["entries"]
        assert data["entries"][_VALID_SHA]["role"] == "coder"
        assert data["entries"][_VALID_SHA]["pipeline_id"] == "issue-1882"

    def test_register_captures_metadata(self, store: CommitAuthorshipStore):
        """repo + branch survive a register round-trip."""
        store.register(
            _VALID_SHA,
            "coder",
            "issue-1882",
            repo="owner/repo",
            branch="egg/issue-1882",
        )
        shard = store.worktree / SUBSTORE_DIR / "issue-1882.json"
        entry = json.loads(shard.read_text())["entries"][_VALID_SHA]
        assert entry["repo"] == "owner/repo"
        assert entry["branch"] == "egg/issue-1882"
        assert entry["registered_at"]  # non-empty ISO8601

    def test_register_normalizes_sha_case(self, store: CommitAuthorshipStore):
        """SHA case is normalised before storage / lookup."""
        uppercase = _VALID_SHA.upper()
        normalized, _, _ = store.register(uppercase, "coder", "issue-1882")
        assert normalized == _VALID_SHA
        assert store.lookup(uppercase) == "coder"
        assert store.lookup(_VALID_SHA) == "coder"

    def test_register_normalizes_role_case(self, store: CommitAuthorshipStore):
        """Role case is normalised to lowercase."""
        store.register(_VALID_SHA, "CODER", "issue-1882")
        assert store.lookup(_VALID_SHA) == "coder"

    def test_lookup_returns_none_for_unknown_sha(self, store: CommitAuthorshipStore):
        """Unregistered SHAs map to ``None``."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        assert store.lookup(_OTHER_SHA) is None

    def test_lookup_returns_none_for_invalid_sha(self, store: CommitAuthorshipStore):
        """Malformed SHAs never collide with storage — silent ``None``."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        assert store.lookup("") is None
        assert store.lookup("not-a-sha") is None
        assert store.lookup("Z" * 40) is None  # non-hex


# ---------------------------------------------------------------------------
# Idempotency (same role)
# ---------------------------------------------------------------------------


class TestIdempotentReregister:
    def test_same_role_re_register_is_noop(self, store: CommitAuthorshipStore):
        """Re-registering an identical (sha, role) is a no-op success."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        normalized, inserted, existing = store.register(_VALID_SHA, "coder", "issue-1882")
        assert normalized == _VALID_SHA
        assert inserted is False
        assert existing is None
        assert store.lookup(_VALID_SHA) == "coder"

    def test_same_role_case_insensitive(self, store: CommitAuthorshipStore):
        """Re-register with a different-case role is still a no-op."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        _, inserted, _ = store.register(_VALID_SHA, "CODER", "issue-1882")
        assert inserted is False

    def test_idempotent_preserves_original_registered_at(
        self, store: CommitAuthorshipStore, worktree: Path
    ):
        """A no-op re-register does not change ``registered_at``."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        shard = worktree / SUBSTORE_DIR / "issue-1882.json"
        before = json.loads(shard.read_text())["entries"][_VALID_SHA]["registered_at"]
        store.register(_VALID_SHA, "coder", "issue-1882")
        after = json.loads(shard.read_text())["entries"][_VALID_SHA]["registered_at"]
        assert before == after


# ---------------------------------------------------------------------------
# First-wins collision
# ---------------------------------------------------------------------------


class TestFirstWinsCollision:
    def test_different_role_raises_collision(self, store: CommitAuthorshipStore):
        """A second (sha, role) binding with a different role raises."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        with pytest.raises(AuthorshipCollisionError) as exc_info:
            store.register(_VALID_SHA, "tester", "issue-1882")
        err = exc_info.value
        assert err.sha == _VALID_SHA
        assert err.existing_role == "coder"
        assert err.attempted_role == "tester"

    def test_collision_preserves_original_role(self, store: CommitAuthorshipStore):
        """Lookup still returns the first-wins role after a collision."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        with pytest.raises(AuthorshipCollisionError):
            store.register(_VALID_SHA, "tester", "issue-1882")
        assert store.lookup(_VALID_SHA) == "coder"

    def test_collision_preserves_original_metadata(
        self, store: CommitAuthorshipStore, worktree: Path
    ):
        """Collision must NOT overwrite repo/branch/registered_at either."""
        store.register(
            _VALID_SHA,
            "coder",
            "issue-1882",
            repo="owner/repo",
            branch="egg/issue-1882",
        )
        shard = worktree / SUBSTORE_DIR / "issue-1882.json"
        before = json.loads(shard.read_text())["entries"][_VALID_SHA]
        with pytest.raises(AuthorshipCollisionError):
            store.register(_VALID_SHA, "tester", "issue-1882", repo="other/repo", branch="main")
        after = json.loads(shard.read_text())["entries"][_VALID_SHA]
        assert before == after


# ---------------------------------------------------------------------------
# Bulk lookup
# ---------------------------------------------------------------------------


class TestBulkLookup:
    def test_bulk_lookup_returns_all_shas(self, store: CommitAuthorshipStore):
        """Every requested sha appears in the output (hit or miss)."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        store.register(_OTHER_SHA, "tester", "issue-1882")
        result = store.lookup_bulk([_VALID_SHA, _OTHER_SHA, _THIRD_SHA])
        assert result == {
            _VALID_SHA: "coder",
            _OTHER_SHA: "tester",
            _THIRD_SHA: None,
        }

    def test_bulk_lookup_partial_miss(self, store: CommitAuthorshipStore):
        """Unknown SHAs map to ``None``; known SHAs keep their role."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        result = store.lookup_bulk([_VALID_SHA, _OTHER_SHA])
        assert result[_VALID_SHA] == "coder"
        assert result[_OTHER_SHA] is None

    def test_bulk_lookup_all_miss(self, store: CommitAuthorshipStore):
        """An entirely-unregistered batch returns all ``None``s."""
        result = store.lookup_bulk([_VALID_SHA, _OTHER_SHA])
        assert result == {_VALID_SHA: None, _OTHER_SHA: None}

    def test_bulk_lookup_empty_input(self, store: CommitAuthorshipStore):
        """Empty input returns an empty dict, not an error."""
        assert store.lookup_bulk([]) == {}
        assert store.lookup_bulk(None) == {}  # type: ignore[arg-type]

    def test_bulk_lookup_skips_invalid_shas(self, store: CommitAuthorshipStore):
        """Invalid SHAs are silently dropped (caller treats as unregistered)."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        result = store.lookup_bulk([_VALID_SHA, "not-a-sha", ""])
        assert result == {_VALID_SHA: "coder"}

    def test_bulk_lookup_deduplicates(self, store: CommitAuthorshipStore):
        """Duplicate SHAs are collapsed into one key."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        result = store.lookup_bulk([_VALID_SHA, _VALID_SHA, _VALID_SHA.upper()])
        assert result == {_VALID_SHA: "coder"}


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrentWrites:
    def test_two_threads_different_shas(self, store: CommitAuthorshipStore):
        """Two threads writing different SHAs both succeed (no lost update)."""
        errors: list[BaseException] = []

        def worker(sha: str, role: str) -> None:
            try:
                store.register(sha, role, "issue-1882")
            except BaseException as exc:  # noqa: BLE001 - surface to parent
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(_VALID_SHA, "coder"))
        t2 = threading.Thread(target=worker, args=(_OTHER_SHA, "tester"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []
        assert store.lookup(_VALID_SHA) == "coder"
        assert store.lookup(_OTHER_SHA) == "tester"

    def test_two_threads_same_sha_same_role(self, store: CommitAuthorshipStore):
        """Concurrent same-role registers are both safe (idempotent)."""
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                store.register(_VALID_SHA, "coder", "issue-1882")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert store.lookup(_VALID_SHA) == "coder"

    def test_two_threads_same_sha_conflicting_roles(self, store: CommitAuthorshipStore):
        """Exactly one thread wins; the others see AuthorshipCollisionError."""
        collisions: list[AuthorshipCollisionError] = []
        successes: list[str] = []
        lock = threading.Lock()

        def worker(role: str) -> None:
            try:
                store.register(_VALID_SHA, role, "issue-1882")
                with lock:
                    successes.append(role)
            except AuthorshipCollisionError as exc:
                with lock:
                    collisions.append(exc)

        threads = [
            threading.Thread(target=worker, args=(r,))
            for r in ("coder", "tester", "reviewer_code", "documenter")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread was first; everyone else collided.
        assert len(successes) == 1
        assert len(collisions) == 3
        assert store.lookup(_VALID_SHA) == successes[0]


# ---------------------------------------------------------------------------
# Per-pipeline sharding
# ---------------------------------------------------------------------------


class TestPerPipelineSharding:
    def test_different_pipelines_write_different_shards(
        self, store: CommitAuthorshipStore, worktree: Path
    ):
        """A commit registered for pipeline A lives in shard A, not B."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        store.register(_OTHER_SHA, "tester", "issue-1897")
        shard_a = worktree / SUBSTORE_DIR / "issue-1882.json"
        shard_b = worktree / SUBSTORE_DIR / "issue-1897.json"
        assert shard_a.exists() and shard_b.exists()
        assert _VALID_SHA in json.loads(shard_a.read_text())["entries"]
        assert _VALID_SHA not in json.loads(shard_b.read_text())["entries"]
        assert _OTHER_SHA in json.loads(shard_b.read_text())["entries"]

    def test_lookup_spans_all_shards(self, store: CommitAuthorshipStore):
        """``lookup`` / ``lookup_bulk`` scan every shard on disk."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        store.register(_OTHER_SHA, "tester", "issue-1897")
        assert store.lookup(_VALID_SHA) == "coder"
        assert store.lookup(_OTHER_SHA) == "tester"
        assert store.lookup_bulk([_VALID_SHA, _OTHER_SHA]) == {
            _VALID_SHA: "coder",
            _OTHER_SHA: "tester",
        }

    def test_orphan_shard_for_missing_pipeline(self, store: CommitAuthorshipStore, worktree: Path):
        """An empty / None pipeline_id routes to the orphan shard."""
        store.register(_VALID_SHA, "coder", None)
        orphan = worktree / SUBSTORE_DIR / f"{ORPHAN_SHARD_ID}.json"
        assert orphan.exists()
        assert _VALID_SHA in json.loads(orphan.read_text())["entries"]

    def test_empty_pipeline_string_routes_to_orphan(
        self, store: CommitAuthorshipStore, worktree: Path
    ):
        """An empty-string pipeline also routes to the orphan shard."""
        store.register(_VALID_SHA, "coder", "")
        orphan = worktree / SUBSTORE_DIR / f"{ORPHAN_SHARD_ID}.json"
        assert orphan.exists()


# ---------------------------------------------------------------------------
# State-branch commit on write
# ---------------------------------------------------------------------------


class _StubStateStore:
    """Minimal stub around StateStore._run_git + _sync_to_remote_async."""

    def __init__(self, worktree: Path):
        self._worktree = worktree
        worktree.mkdir(parents=True, exist_ok=True)
        self.run_git_calls: list[tuple[tuple, dict]] = []
        self.sync_called = False
        # By default simulate "there are changes to commit" so the commit
        # actually runs. Tests can reset this to simulate idempotent re-registers.
        self._diff_returncode = 1

    @property
    def worktree(self) -> Path:
        return self._worktree

    def _run_git(self, *args, cwd=None, check=True, **_kwargs):
        self.run_git_calls.append((args, {"cwd": cwd, "check": check}))
        # Return a MagicMock that mimics subprocess.CompletedProcess
        mock_result = MagicMock()
        mock_result.args = args
        mock_result.returncode = self._diff_returncode if args[:2] == ("diff", "--cached") else 0
        return mock_result

    def _sync_to_remote_async(self) -> None:
        self.sync_called = True


class TestStateBranchCommitOnWrite:
    def test_register_triggers_git_add_and_commit(self, worktree: Path):
        """A successful register triggers ``git add`` + ``git commit`` through the StateStore."""
        stub = _StubStateStore(worktree)
        store = CommitAuthorshipStore(state_store=stub)  # type: ignore[arg-type]
        store.register(_VALID_SHA, "coder", "issue-1882")
        cmds = [args[0] for args, _kwargs in stub.run_git_calls]
        assert "add" in cmds
        assert "commit" in cmds
        # The diff --cached probe sits between add and commit.
        assert "diff" in cmds
        assert stub.sync_called is True

    def test_idempotent_reregister_skips_commit(self, worktree: Path):
        """When the diff probe reports no changes, no commit is made."""
        stub = _StubStateStore(worktree)
        store = CommitAuthorshipStore(state_store=stub)  # type: ignore[arg-type]
        store.register(_VALID_SHA, "coder", "issue-1882")
        stub.run_git_calls.clear()
        stub.sync_called = False
        stub._diff_returncode = 0  # simulate "no changes"
        store.register(_VALID_SHA, "coder", "issue-1882")  # idempotent
        # Same-role re-register is a no-op; no state-branch activity.
        cmds = [args[0] for args, _kwargs in stub.run_git_calls]
        assert "commit" not in cmds

    def test_commit_false_skips_git(self, worktree: Path):
        """Passing ``commit=False`` suppresses the state-branch writeback."""
        stub = _StubStateStore(worktree)
        store = CommitAuthorshipStore(state_store=stub)  # type: ignore[arg-type]
        store.register(_VALID_SHA, "coder", "issue-1882", commit=False)
        assert stub.run_git_calls == []
        assert stub.sync_called is False

    def test_commit_swallows_git_errors(self, worktree: Path):
        """A CalledProcessError on git add/commit is logged and swallowed."""
        import subprocess as _sp

        stub = _StubStateStore(worktree)

        def boom(*_args, **_kwargs):
            raise _sp.CalledProcessError(1, ["git", "commit"])

        stub._run_git = boom  # type: ignore[assignment]
        store = CommitAuthorshipStore(state_store=stub)  # type: ignore[arg-type]
        # Must not raise — the registration still succeeded on disk.
        store.register(_VALID_SHA, "coder", "issue-1882")
        assert store.lookup(_VALID_SHA) == "coder"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_invalid_sha_raises(self, store: CommitAuthorshipStore):
        with pytest.raises(CommitAuthorshipStoreError):
            store.register("not-a-sha", "coder", "issue-1882")
        with pytest.raises(CommitAuthorshipStoreError):
            store.register("", "coder", "issue-1882")
        with pytest.raises(CommitAuthorshipStoreError):
            store.register("Z" * 40, "coder", "issue-1882")  # non-hex

    def test_invalid_role_raises(self, store: CommitAuthorshipStore):
        with pytest.raises(CommitAuthorshipStoreError):
            store.register(_VALID_SHA, "", "issue-1882")
        with pytest.raises(CommitAuthorshipStoreError):
            store.register(_VALID_SHA, "Role With Spaces", "issue-1882")
        with pytest.raises(CommitAuthorshipStoreError):
            store.register(_VALID_SHA, "1starts-with-digit", "issue-1882")

    def test_invalid_pipeline_id_raises(self, store: CommitAuthorshipStore):
        """An unknown pipeline-id pattern is rejected (path-traversal guard)."""
        with pytest.raises(CommitAuthorshipStoreError):
            store.register(_VALID_SHA, "coder", "../../etc")
        with pytest.raises(CommitAuthorshipStoreError):
            store.register(_VALID_SHA, "coder", "weird pipeline name")

    def test_path_traversal_blocked(self, tmp_path: Path):
        """Explicit path-traversal attempt is caught even if validation slipped."""
        worktree = tmp_path / "wt"
        store = CommitAuthorshipStore(worktree_dir=worktree)
        with pytest.raises(CommitAuthorshipStoreError):
            # issue-... is accepted by the pipeline regex, but this
            # embeds ".."; _shard_path catches it via resolve() guard.
            store._shard_path("issue-1882/../../../etc")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Corrupt shard handling
# ---------------------------------------------------------------------------


class TestCorruptShard:
    def test_corrupt_shard_on_read_raises(self, store: CommitAuthorshipStore, worktree: Path):
        """A corrupt JSON shard surfaces as CommitAuthorshipStoreError on register."""
        shard = worktree / SUBSTORE_DIR / "issue-1882.json"
        shard.parent.mkdir(parents=True, exist_ok=True)
        shard.write_text("{not valid json")
        with pytest.raises(CommitAuthorshipStoreError):
            store.register(_VALID_SHA, "coder", "issue-1882")

    def test_corrupt_shard_ignored_on_lookup(self, store: CommitAuthorshipStore, worktree: Path):
        """Corrupt shards are logged-and-skipped by bulk lookup."""
        bad = worktree / SUBSTORE_DIR / "issue-1897.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{{{")
        store.register(_VALID_SHA, "coder", "issue-1882")  # good shard
        # Should not raise; the good shard is still visible.
        assert store.lookup(_VALID_SHA) == "coder"

    def test_missing_entries_key_raises(self, store: CommitAuthorshipStore, worktree: Path):
        """Shard JSON without ``entries`` key is treated as corrupt."""
        shard = worktree / SUBSTORE_DIR / "issue-1882.json"
        shard.parent.mkdir(parents=True, exist_ok=True)
        shard.write_text('{"version": 1}')
        with pytest.raises(CommitAuthorshipStoreError):
            store.register(_VALID_SHA, "coder", "issue-1882")


# ---------------------------------------------------------------------------
# Factory errors
# ---------------------------------------------------------------------------


class TestFactory:
    def test_neither_state_store_nor_worktree_raises(self):
        """Constructor requires at least one backing store."""
        with pytest.raises(ValueError):
            CommitAuthorshipStore()

    def test_worktree_created_on_demand(self, tmp_path: Path):
        """Pure-filesystem mode creates the worktree if missing."""
        worktree = tmp_path / "missing"
        assert not worktree.exists()
        store = CommitAuthorshipStore(worktree_dir=worktree)
        store.register(_VALID_SHA, "coder", "issue-1882")
        assert worktree.exists()


class TestResolveAuthorshipRepoPath:
    """``_resolve_authorship_repo_path`` must handle the three shapes
    ``EGG_REPO_PATH`` takes in the wild: single repo, parent dir with
    multiple child repos, and missing/non-existent path."""

    def test_single_git_repo(self, tmp_path: Path, monkeypatch):
        """``EGG_REPO_PATH`` pointing at a single repo returns that repo."""
        from commit_authorship_store import _resolve_authorship_repo_path

        repo = tmp_path / "single"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setenv("EGG_REPO_PATH", str(repo))

        assert _resolve_authorship_repo_path() == repo

    def test_parent_dir_prefers_egg_repo(self, tmp_path: Path, monkeypatch):
        """Parent dir with multiple repos picks ``egg`` by name when present."""
        from commit_authorship_store import _resolve_authorship_repo_path

        parent = tmp_path / "repos"
        parent.mkdir()
        for name in ("actions", "egg", "zzz"):
            child = parent / name
            child.mkdir()
            (child / ".git").mkdir()
        monkeypatch.setenv("EGG_REPO_PATH", str(parent))

        assert _resolve_authorship_repo_path() == parent / "egg"

    def test_parent_dir_falls_back_to_first_alpha(self, tmp_path: Path, monkeypatch):
        """When ``egg`` is absent, fall back to the first repo alphabetically
        so deployments without an ``egg`` repo get a deterministic choice."""
        from commit_authorship_store import _resolve_authorship_repo_path

        parent = tmp_path / "repos"
        parent.mkdir()
        for name in ("alpha", "beta"):
            child = parent / name
            child.mkdir()
            (child / ".git").mkdir()
        monkeypatch.setenv("EGG_REPO_PATH", str(parent))

        assert _resolve_authorship_repo_path() == parent / "alpha"

    def test_non_existent_path_returns_env_value(self, tmp_path: Path, monkeypatch):
        """Non-existent path is returned verbatim — ``StateStore`` will
        raise the actionable error when ``_ensure_worktree`` runs."""
        from commit_authorship_store import _resolve_authorship_repo_path

        missing = tmp_path / "nope"
        monkeypatch.setenv("EGG_REPO_PATH", str(missing))

        assert _resolve_authorship_repo_path() == missing

    def test_parent_dir_with_no_repos_returns_env_value(self, tmp_path: Path, monkeypatch):
        """Parent dir whose children are not git repos is returned
        verbatim — ``StateStore`` will raise on access."""
        from commit_authorship_store import _resolve_authorship_repo_path

        parent = tmp_path / "non-repos"
        parent.mkdir()
        (parent / "not-a-repo").mkdir()  # No .git inside.
        monkeypatch.setenv("EGG_REPO_PATH", str(parent))

        assert _resolve_authorship_repo_path() == parent

    def test_unset_env_uses_historical_default(self, monkeypatch):
        """Unset ``EGG_REPO_PATH`` falls back to ``/home/egg/repos/egg``
        for back-compat with single-repo deployments that never set it."""
        from commit_authorship_store import _resolve_authorship_repo_path

        monkeypatch.delenv("EGG_REPO_PATH", raising=False)
        monkeypatch.delenv("EGG_AUTHORSHIP_REPO", raising=False)

        # The default path almost certainly does not exist on the test
        # host, so the resolver returns it verbatim per the documented
        # non-existent-path branch.  The assertion locks in the default.
        assert _resolve_authorship_repo_path() == Path("/home/egg/repos/egg")

    def test_authorship_repo_override_absolute_path(self, tmp_path: Path, monkeypatch):
        """``EGG_AUTHORSHIP_REPO`` as absolute path bypasses
        ``EGG_REPO_PATH`` discovery entirely — useful for forked /
        renamed deployments where the alphabetical fallback would pick
        the wrong repo."""
        from commit_authorship_store import _resolve_authorship_repo_path

        parent = tmp_path / "repos"
        parent.mkdir()
        for name in ("alpha", "egg", "zzz"):
            child = parent / name
            child.mkdir()
            (child / ".git").mkdir()
        monkeypatch.setenv("EGG_REPO_PATH", str(parent))
        # Override picks `zzz` even though `egg` would normally win.
        monkeypatch.setenv("EGG_AUTHORSHIP_REPO", str(parent / "zzz"))

        assert _resolve_authorship_repo_path() == parent / "zzz"

    def test_authorship_repo_override_relative_name(self, tmp_path: Path, monkeypatch):
        """``EGG_AUTHORSHIP_REPO`` as a relative name resolves under
        ``EGG_REPO_PATH`` so operators can write ``EGG_AUTHORSHIP_REPO=myfork``
        without repeating the parent path."""
        from commit_authorship_store import _resolve_authorship_repo_path

        parent = tmp_path / "repos"
        parent.mkdir()
        for name in ("alpha", "egg", "myfork"):
            child = parent / name
            child.mkdir()
            (child / ".git").mkdir()
        monkeypatch.setenv("EGG_REPO_PATH", str(parent))
        monkeypatch.setenv("EGG_AUTHORSHIP_REPO", "myfork")

        assert _resolve_authorship_repo_path() == parent / "myfork"


class TestGetStoreUsesGetStateStore:
    """Regression for #2184: ``get_store()`` must route through
    ``state_store.get_state_store`` so the worktree path matches what
    ``unified_sse``/``routes/health``/``routes/signals`` use.

    Constructing ``StateStore`` directly fell back to the default
    ``pipeline-worktree`` path while the rest of the orchestrator picked
    ``pipeline-worktree-egg`` in multi-repo mode — both then raced to
    ``git worktree add`` the same ``egg/pipeline-state`` branch and one
    side wedged with ``branch already in use``.
    """

    def test_multi_repo_converges_on_per_repo_worktree(self, tmp_path: Path, monkeypatch):
        """In multi-repo mode, ``get_store()`` and ``get_state_store(egg)``
        agree on ``pipeline-worktree-<repo_name>`` so they share — not
        clash — over the state branch.
        """
        # Stub out StateStore — we don't want real git to run.  We only
        # need to observe the ``worktree_dir`` argument the factory
        # passes in.
        import state_store as ss_mod  # type: ignore[import-not-found]

        captured: dict[str, Path | None] = {}

        class _StubStateStore:
            def __init__(self, repo_path, worktree_dir=None):
                self.repo_path = repo_path
                self._worktree_dir = worktree_dir
                captured["repo_path"] = repo_path
                captured["worktree_dir"] = worktree_dir

            def commit_state_to_branch(self, *args, **kwargs):
                return None

        monkeypatch.setattr(ss_mod, "StateStore", _StubStateStore)

        # Build a parent dir with two sibling repos so multi-repo mode
        # kicks in (a single child repo intentionally falls back to the
        # default path — see ``state_store.get_state_store``).
        parent = tmp_path / "repos"
        parent.mkdir()
        for name in ("actions", "egg"):
            child = parent / name
            child.mkdir()
            (child / ".git").mkdir()

        state_dir = tmp_path / "egg-state"
        monkeypatch.setenv("EGG_REPO_PATH", str(parent))
        monkeypatch.setenv("EGG_STATE_DIR", str(state_dir))
        monkeypatch.delenv("EGG_AUTHORSHIP_REPO", raising=False)

        # First, observe what ``get_state_store`` picks for the egg repo
        # — this is what ``unified_sse`` / health probes use.
        ss_mod.get_state_store(parent / "egg")
        expected_worktree = state_dir / "pipeline-worktree-egg"
        assert captured["repo_path"] == parent / "egg"
        assert captured["worktree_dir"] == expected_worktree

        # Now drive the authorship-store factory and confirm it picks
        # the *same* worktree path — i.e., it routes through
        # ``get_state_store`` rather than constructing ``StateStore``
        # directly with the default ``pipeline-worktree``.
        captured.clear()
        from commit_authorship_store import get_store

        get_store()
        assert captured["repo_path"] == parent / "egg"
        assert captured["worktree_dir"] == expected_worktree, (
            "commit-authorship store must converge on the per-repo worktree "
            "path that the rest of the orchestrator uses; otherwise both "
            "sides race over the egg/pipeline-state branch."
        )
