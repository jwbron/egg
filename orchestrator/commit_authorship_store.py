"""Commit-authorship registry store.

Maintains a durable mapping of ``commit_sha → role`` sharded per pipeline
on the ``egg/pipeline-state`` orphan branch.  The gateway's commit
observer POSTs to the HTTP routes in ``orchestrator/routes/commit_authorship.py``,
which write through this store; the gateway's push handler later looks
commits up here to attribute each file in a push range to the role that
authored it (see issue #1882 B3 decision).

Storage layout (relative to the state worktree):

    .egg-state/commit-authorship/<pipeline_id>.json   # one shard per pipeline
    .egg-state/commit-authorship/_orphan.json          # fallback for
                                                        # commits registered
                                                        # before a pipeline_id
                                                        # is known.

Each shard is a JSON object:

    {
      "version": 1,
      "entries": {
        "<sha>": {
          "role": "<role>",
          "pipeline_id": "<pipeline_id>",
          "repo": "<owner/repo>",
          "branch": "<branch>",
          "registered_at": "<iso8601>"
        },
        ...
      }
    }

Semantics:

- **First-wins**:  once ``(sha, role)`` is bound, subsequent registrations
  with a *different* role are rejected (the original binding is preserved
  and a collision is audit-logged).  Same-role re-registration is a no-op.
  This prevents a malicious agent from suppressing the observer on its
  own commit and then re-registering later under a different role to pick
  attribution.
- **Idempotent**:  identical re-registrations return success without
  mutating the shard.
- **Bulk lookup**:  ``lookup_bulk`` is the primary read path used by the
  gateway's push handler to partition files by author.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.state_store import StateStore

logger = logging.getLogger("orchestrator.commit_authorship_store")


SUBSTORE_DIR = ".egg-state/commit-authorship"
ORPHAN_SHARD_ID = "_orphan"

# A git commit SHA is lowercase hex, 7–64 chars.  We accept the canonical
# 40-char SHA-1 and the emerging 64-char SHA-256 format without rejecting
# test data that uses shorter values (our own unit tests do).
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
# Role identifier: lowercase alphanumeric + underscore/hyphen.  Matches
# the AgentRole values used elsewhere in the codebase.
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# Pipeline ID: same pattern as ``orchestrator.state_store.PIPELINE_ID_PATTERN``
# plus the orphan sentinel.  We duplicate the validation here so this
# module can be unit-tested without pulling in the full state_store
# module graph (which drags in Flask/kubernetes mocks in CI).
_PIPELINE_ID_RE = re.compile(
    r"^("
    r"issue-[0-9]+(-[a-z0-9]+)*"
    r"|[A-Z][A-Z0-9]+-[0-9]+(-[a-z0-9]+)*"
    r"|local-[0-9a-f]{8}"
    r"|pipeline-[0-9a-f]{8}"
    r"|pr-[0-9]+"
    r"|_orphan"
    r")$"
)

_SCHEMA_VERSION = 1


class CommitAuthorshipStoreError(Exception):
    """Base exception for authorship store errors."""


class AuthorshipCollisionError(CommitAuthorshipStoreError):
    """A later registration tried to re-bind a SHA to a different role.

    Carries the ``existing_role`` so callers can audit-log the attempt
    without re-reading the store.
    """

    def __init__(self, sha: str, existing_role: str, attempted_role: str) -> None:
        self.sha = sha
        self.existing_role = existing_role
        self.attempted_role = attempted_role
        super().__init__(
            f"Authorship collision for {sha}: "
            f"existing_role={existing_role!r} attempted_role={attempted_role!r}"
        )


@dataclass
class AuthorshipEntry:
    """One row in the authorship store."""

    sha: str
    role: str
    pipeline_id: str
    repo: str | None
    branch: str | None
    registered_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "pipeline_id": self.pipeline_id,
            "repo": self.repo,
            "branch": self.branch,
            "registered_at": self.registered_at,
        }


def _validate_sha(sha: str) -> str:
    sha_s = (sha or "").strip().lower()
    if not _SHA_RE.match(sha_s):
        raise CommitAuthorshipStoreError(f"Invalid commit SHA: {sha!r}")
    return sha_s


def _validate_role(role: str) -> str:
    role_s = (role or "").strip().lower()
    if not role_s:
        # Direct Python callers (not through HTTP) would otherwise get a
        # confusing regex-mismatch error for an empty/whitespace role.
        raise CommitAuthorshipStoreError("Role is required (got empty string)")
    if not _ROLE_RE.match(role_s):
        raise CommitAuthorshipStoreError(f"Invalid role: {role!r}")
    return role_s


def _validate_pipeline_id(pipeline_id: str) -> str:
    pid = (pipeline_id or "").strip()
    if pid == "":
        pid = ORPHAN_SHARD_ID
    if not _PIPELINE_ID_RE.match(pid):
        raise CommitAuthorshipStoreError(f"Invalid pipeline ID: {pipeline_id!r}")
    return pid


# ---------------------------------------------------------------------------
# State-branch git commit (inline + async background flusher)
# ---------------------------------------------------------------------------

# Polling interval for the background flusher's ``queue.get`` call.  The
# value only bounds shutdown latency — items are picked up immediately on
# arrival because ``queue.get`` returns as soon as a producer puts.
_FLUSHER_POLL_SECONDS = 0.5

# Time budget the atexit hook gives any in-flight flusher to drain.
_FLUSHER_ATEXIT_TIMEOUT_SECONDS = 5.0

# Upper bound on the flusher's pending queue.  Each item is a small
# tuple, so the memory ceiling is well under 10 MiB even at full
# capacity.  When the worker falls this far behind (e.g. the gateway is
# holding the bare-repo flock for an extended push), ``enqueue`` falls
# back to inline replication rather than letting the queue grow without
# bound.
_FLUSHER_QUEUE_MAX = 10_000


def _commit_one_shard_inline(
    state_store: Any,
    path: Path,
    sha: str,
    role: str,
    pipeline_id: str,
) -> None:
    """Stage ``path`` and commit the result through ``state_store``."""
    wt = state_store.worktree
    try:
        rel = str(path.relative_to(wt))
    except ValueError:
        # If the shard lives outside the state worktree (shouldn't
        # happen in production, but can in tests that point
        # ``worktree_dir`` elsewhere), skip the commit silently.
        return
    try:
        state_store._run_git("add", rel, cwd=wt)
        diff = state_store._run_git("diff", "--cached", "--quiet", cwd=wt, check=False)
        if diff.returncode == 0:
            return
        msg = f"commit-authorship: register {sha[:12]} as {role} (pipeline={pipeline_id})"
        state_store._run_git("commit", "--no-verify", "-m", msg, cwd=wt)
        try:
            state_store._sync_to_remote_async()
        except Exception:
            logger.debug("authorship_state_branch_push_deferred", exc_info=True)
    except subprocess.CalledProcessError:
        logger.warning(
            "authorship_state_branch_commit_failed sha=%s pipeline_id=%s",
            sha,
            pipeline_id,
            exc_info=True,
        )
    except Exception:
        # Store-level errors (GitOperationError, etc.) must not
        # poison the registration — the shard is already on disk.
        logger.warning(
            "authorship_state_branch_commit_failed sha=%s pipeline_id=%s",
            sha,
            pipeline_id,
            exc_info=True,
        )


def _commit_batch_inline(
    state_store: Any,
    batch: list[tuple[Path, str, str, str]],
) -> None:
    """Stage every distinct shard path in ``batch`` and create one commit.

    Coalescing N registers into one commit cuts the number of
    ``git add`` + ``git commit`` cycles per flusher tick from N to 1,
    which is the whole point of the async path (#2453).
    """
    if not batch:
        return
    wt = state_store.worktree
    rel_to_entries: dict[str, list[tuple[str, str, str]]] = {}
    rel_order: list[str] = []
    for path, sha, role, pipeline_id in batch:
        try:
            rel = str(path.relative_to(wt))
        except ValueError:
            continue
        if rel not in rel_to_entries:
            rel_to_entries[rel] = []
            rel_order.append(rel)
        rel_to_entries[rel].append((sha, role, pipeline_id))
    if not rel_order:
        return
    if len(rel_order) == 1 and len(rel_to_entries[rel_order[0]]) == 1:
        # Singleton path keeps the original per-sha commit message so log
        # consumers and audit tools see the familiar format.
        rel = rel_order[0]
        sha, role, pipeline_id = rel_to_entries[rel][0]
        path = wt / rel
        _commit_one_shard_inline(state_store, path, sha, role, pipeline_id)
        return
    try:
        for rel in rel_order:
            state_store._run_git("add", rel, cwd=wt)
        diff = state_store._run_git("diff", "--cached", "--quiet", cwd=wt, check=False)
        if diff.returncode == 0:
            return
        total_entries = sum(len(v) for v in rel_to_entries.values())
        msg = (
            f"commit-authorship: batch register "
            f"({total_entries} entries across {len(rel_order)} shard(s))"
        )
        state_store._run_git("commit", "--no-verify", "-m", msg, cwd=wt)
        try:
            state_store._sync_to_remote_async()
        except Exception:
            logger.debug("authorship_state_branch_push_deferred", exc_info=True)
    except subprocess.CalledProcessError:
        logger.warning(
            "authorship_state_branch_commit_failed batch_size=%d",
            len(batch),
            exc_info=True,
        )
    except Exception:
        logger.warning(
            "authorship_state_branch_commit_failed batch_size=%d",
            len(batch),
            exc_info=True,
        )


class _AuthorshipFlusher:
    """Background worker that absorbs state-branch git commits.

    The single worker design matches the cross-process ``flock`` the
    state-store already holds on the bare repo — concurrent commits
    serialise on that lock anyway, so an in-process pool would only add
    contention.  Each tick drains the queue completely and folds every
    pending shard into one ``git add`` + ``git commit`` (see
    ``_commit_batch_inline``).
    """

    # Sentinel pushed onto the queue to wake the worker out of its
    # blocking ``queue.get`` during shutdown.  ``None`` is unambiguous
    # because every real item is a 4-tuple.
    _SHUTDOWN_SENTINEL: Any = None

    def __init__(self, state_store: Any) -> None:
        self._state_store = state_store
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=_FLUSHER_QUEUE_MAX)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="authorship-flusher",
            daemon=True,
        )
        self._thread.start()
        # Bind the atexit hook once so ``atexit.unregister`` can match
        # the same callable in ``shutdown()`` — bound-method objects
        # compare by identity in atexit's registry, and re-binding via
        # ``self._atexit_drain`` would create a fresh object that
        # ``unregister`` can't find.
        self._atexit_hook = self._atexit_drain
        atexit.register(self._atexit_hook)

    def enqueue(self, path: Path, sha: str, role: str, pipeline_id: str) -> None:
        if self._stop.is_set():
            # The flusher has been shut down; fall back to inline so the
            # caller still gets state-branch replication for this commit.
            _commit_one_shard_inline(self._state_store, path, sha, role, pipeline_id)
            return
        try:
            self._queue.put_nowait((path, sha, role, pipeline_id))
        except queue.Full:
            # Worker has fallen far enough behind that the bounded queue
            # is full.  Degrade gracefully by running this commit inline
            # rather than blocking the caller or growing memory without
            # bound.
            logger.warning(
                "authorship_flusher_queue_full sha=%s pipeline_id=%s "
                "(falling back to inline commit)",
                sha,
                pipeline_id,
            )
            _commit_one_shard_inline(self._state_store, path, sha, role, pipeline_id)

    def flush(self, timeout: float | None = None) -> bool:
        """Block until every enqueued item has been processed.

        Implemented by polling ``Queue.unfinished_tasks`` rather than
        rolling our own idle flag — the counter is incremented inside
        ``Queue.put`` and decremented in ``Queue.task_done``, so the
        "item dequeued but not yet processed" state is observable
        race-free.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        while self._queue.unfinished_tasks > 0:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def shutdown(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self.flush(timeout=timeout)
        # Wake the worker out of its blocking ``queue.get`` so it
        # observes ``_stop`` immediately rather than waiting up to
        # ``_FLUSHER_POLL_SECONDS`` on the next poll.
        try:
            self._queue.put_nowait(self._SHUTDOWN_SENTINEL)
        except queue.Full:  # pragma: no cover - defensive
            # If the queue is somehow still full, the poll-timeout
            # fallback in ``_run`` will catch ``_stop`` shortly.
            pass
        self._thread.join(timeout=timeout)
        # Drop the atexit registration so the hook (and the bound
        # ``self`` it pins) can be garbage-collected.  Without this,
        # every ``reset_singleton`` cycle would accumulate a hook for
        # the lifetime of the process.
        try:
            atexit.unregister(self._atexit_hook)
        except Exception:  # pragma: no cover - defensive
            logger.debug("authorship_flusher_atexit_unregister_failed", exc_info=True)

    def _atexit_drain(self) -> None:
        # Best-effort: give in-flight registrations a short window to
        # land on the state branch before the process exits.
        try:
            self.flush(timeout=_FLUSHER_ATEXIT_TIMEOUT_SECONDS)
        except Exception:  # pragma: no cover - defensive
            logger.debug("authorship_flusher_atexit_failed", exc_info=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=_FLUSHER_POLL_SECONDS)
            except queue.Empty:
                continue
            if first is self._SHUTDOWN_SENTINEL:
                # Shutdown wake-up.  Ack the sentinel so the
                # unfinished-task counter stays in sync, then exit.
                self._queue.task_done()
                return
            batch: list[tuple[Path, str, str, str]] = [first]
            try:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is self._SHUTDOWN_SENTINEL:
                        # Don't fold the sentinel into the work batch;
                        # ack it separately so unfinished_tasks balances.
                        self._queue.task_done()
                        continue
                    batch.append(item)
                _commit_batch_inline(self._state_store, batch)
            except Exception:  # pragma: no cover - defensive
                logger.warning("authorship_flusher_tick_failed", exc_info=True)
            finally:
                # Mark every real item we dequeued as done so
                # ``unfinished_tasks`` returns to zero.
                for _ in batch:
                    self._queue.task_done()


class CommitAuthorshipStore:
    """Git-backed, pipeline-sharded commit-authorship store.

    Reuses the state-store worktree so both stores share the same
    ``egg/pipeline-state`` branch, cross-process ``fcntl`` lock, and
    remote-sync daemon.  The state-store reference is optional so this
    module can be exercised in unit tests without standing up a full
    ``StateStore`` (the tests pass an explicit ``worktree_dir``).

    Git replication of new shard contents to the ``egg/pipeline-state``
    branch is dispatched to a background ``_AuthorshipFlusher`` thread so
    ``register()`` returns as soon as the disk write completes.  Lookups
    only ever read disk, so they remain correct even before the git
    commit lands.  See issue #2453.
    """

    # Process-wide lock to serialize in-memory read-modify-write of the
    # same shard.  Cross-process serialization is provided by the state
    # store's existing ``fcntl.flock`` on the worktree.
    _lock: threading.RLock = threading.RLock()

    def __init__(
        self,
        *,
        state_store: StateStore | None = None,
        worktree_dir: Path | None = None,
        synchronous: bool = False,
    ) -> None:
        """Initialize the store.

        Args:
            state_store: An existing StateStore.  When set, all commits
                go through its ``_run_git`` + ``_commit_state`` plumbing
                and are synced to the remote.  When ``None``, ``worktree_dir``
                must be supplied and the store operates purely on the
                filesystem (unit-test mode).
            worktree_dir: Explicit worktree path.  Required when
                ``state_store`` is None.  Ignored otherwise (the state
                store's worktree wins).
            synchronous: When True, run state-branch git work inline in
                ``register()`` instead of dispatching to the background
                flusher.  Intended for unit tests that want to assert on
                git activity without driving a worker thread.  Defaults
                to False (the production async path).
        """
        if state_store is None and worktree_dir is None:
            raise ValueError("Provide either state_store or worktree_dir")
        self._state_store = state_store
        self._worktree_dir = worktree_dir
        self._synchronous = synchronous
        self._flusher: _AuthorshipFlusher | None = None
        self._flusher_lock = threading.Lock()

    # -- worktree resolution ----------------------------------------------

    @property
    def worktree(self) -> Path:
        if self._state_store is not None:
            return self._state_store.worktree
        assert self._worktree_dir is not None
        self._worktree_dir.mkdir(parents=True, exist_ok=True)
        return self._worktree_dir

    @property
    def _substore_dir(self) -> Path:
        return self.worktree / SUBSTORE_DIR

    def _shard_path(self, pipeline_id: str) -> Path:
        """Path to a pipeline's shard file, validating for path traversal."""
        pid = _validate_pipeline_id(pipeline_id)
        path = self._substore_dir / f"{pid}.json"
        # Guard against path traversal — the validated pipeline_id should
        # already make this impossible, but belt-and-braces.  Use
        # ``relative_to`` (path-aware) rather than ``str.startswith``
        # which would wrongly prefix-match a sister directory like
        # ``commit-authorship-evil`` against ``commit-authorship``.
        resolved = path.resolve()
        base_resolved = self._substore_dir.resolve()
        try:
            resolved.relative_to(base_resolved)
        except ValueError as exc:
            raise CommitAuthorshipStoreError(
                f"Path traversal detected in pipeline ID: {pipeline_id!r}"
            ) from exc
        return path

    def _ensure_substore_dir(self) -> None:
        self._substore_dir.mkdir(parents=True, exist_ok=True)

    # -- shard I/O --------------------------------------------------------

    def _load_shard(self, pipeline_id: str) -> dict[str, Any]:
        path = self._shard_path(pipeline_id)
        if not path.exists():
            return {"version": _SCHEMA_VERSION, "entries": {}}
        try:
            raw = path.read_text()
            if not raw.strip():
                return {"version": _SCHEMA_VERSION, "entries": {}}
            data = json.loads(raw)
            if not isinstance(data, dict) or "entries" not in data:
                raise CommitAuthorshipStoreError(
                    f"Corrupt authorship shard {path}: missing entries"
                )
            return data
        except json.JSONDecodeError as e:
            raise CommitAuthorshipStoreError(f"Corrupt authorship shard {path}: {e}") from e

    def _write_shard_atomic(self, path: Path, data: dict[str, Any]) -> None:
        self._ensure_substore_dir()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- public API -------------------------------------------------------

    def register(
        self,
        sha: str,
        role: str,
        pipeline_id: str | None,
        *,
        repo: str | None = None,
        branch: str | None = None,
        commit: bool = True,
    ) -> tuple[str, bool, str | None]:
        """Register a commit's authorship with first-wins semantics.

        Args:
            sha: The commit SHA being registered.
            role: The agent role that authored the commit.
            pipeline_id: The pipeline that produced the commit.  ``None``
                or empty string routes the entry to the orphan shard.
            repo: Optional owner/repo string for forensic logging.
            branch: Optional branch name for forensic logging.
            commit: When True and a state store is attached, commit the
                shard change to the state branch.

        Returns:
            Tuple ``(sha, inserted, existing_role)``:
              - ``inserted``: True when this call wrote a new binding;
                False when the call was a no-op (identical re-register).
              - ``existing_role``: Populated only when the call raised
                AuthorshipCollisionError; None on the happy paths.

        Raises:
            AuthorshipCollisionError: When a different role has already
                bound this SHA.  The original binding is preserved.
            CommitAuthorshipStoreError: On malformed inputs or I/O errors.
        """
        sha_s = _validate_sha(sha)
        role_s = _validate_role(role)
        pid = _validate_pipeline_id(pipeline_id or ORPHAN_SHARD_ID)

        with self._lock:
            shard = self._load_shard(pid)
            entries = shard.setdefault("entries", {})
            existing = entries.get(sha_s)
            if existing is not None:
                existing_role = (existing or {}).get("role")
                if existing_role == role_s:
                    # Idempotent re-register — no-op.
                    logger.debug(
                        "authorship_register_noop sha=%s role=%s pipeline_id=%s",
                        sha_s,
                        role_s,
                        pid,
                    )
                    return sha_s, False, None
                # First-wins: reject the conflicting role but preserve the
                # original binding. Caller audit-logs the collision.
                logger.warning(
                    "authorship_register_collision sha=%s existing_role=%s attempted_role=%s "
                    "pipeline_id=%s",
                    sha_s,
                    existing_role,
                    role_s,
                    pid,
                )
                raise AuthorshipCollisionError(sha_s, existing_role or "", role_s)

            entry = AuthorshipEntry(
                sha=sha_s,
                role=role_s,
                pipeline_id=pid,
                repo=repo,
                branch=branch,
                registered_at=datetime.now(UTC).isoformat(),
            )
            entries[sha_s] = entry.to_dict()
            shard.setdefault("version", _SCHEMA_VERSION)

            path = self._shard_path(pid)
            self._write_shard_atomic(path, shard)

            if commit and self._state_store is not None:
                self._commit_shard_to_state_branch(path, sha_s, role_s, pid)

            logger.info(
                "authorship_register sha=%s role=%s pipeline_id=%s",
                sha_s,
                role_s,
                pid,
            )
            return sha_s, True, None

    def lookup(self, sha: str) -> str | None:
        """Return the attributed role for a commit, or ``None`` if unknown."""
        try:
            sha_s = _validate_sha(sha)
        except CommitAuthorshipStoreError:
            return None
        return self._lookup_in_all_shards(sha_s)

    def lookup_bulk(self, shas: list[str]) -> dict[str, str | None]:
        """Bulk lookup for a push range.

        Returns a dict mapping every *valid* input sha to its role or
        ``None`` when unregistered.  Invalid shas are silently dropped
        from the result (the caller treats missing entries as
        unregistered anyway).
        """
        # De-duplicate while preserving insertion order so callers with
        # a stable iteration order get predictable output.
        seen: dict[str, None] = {}
        normalized: list[str] = []
        for raw in shas or []:
            try:
                sha_s = _validate_sha(raw)
            except CommitAuthorshipStoreError:
                continue
            if sha_s in seen:
                continue
            seen[sha_s] = None
            normalized.append(sha_s)

        if not normalized:
            return {}

        # Scan every shard once rather than once per SHA — the shard set
        # is bounded by pipeline count and each read is O(1) after load.
        result: dict[str, str | None] = dict.fromkeys(normalized)
        for shard in self._iter_all_shards():
            entries = shard.get("entries", {})
            for sha_s in normalized:
                if result[sha_s] is not None:
                    continue
                entry = entries.get(sha_s)
                if entry is not None:
                    result[sha_s] = entry.get("role")
        return result

    # -- helpers ----------------------------------------------------------

    def _lookup_in_all_shards(self, sha: str) -> str | None:
        for shard in self._iter_all_shards():
            entry = (shard.get("entries") or {}).get(sha)
            if entry is not None:
                return entry.get("role")
        return None

    def _iter_all_shards(self) -> list[dict[str, Any]]:
        """Load every shard on disk.  Bounded by pipeline count."""
        if not self._substore_dir.exists():
            return []
        shards: list[dict[str, Any]] = []
        for path in sorted(self._substore_dir.glob("*.json")):
            try:
                raw = path.read_text()
                if not raw.strip():
                    continue
                data = json.loads(raw)
                if isinstance(data, dict):
                    shards.append(data)
            except json.JSONDecodeError, OSError:
                logger.warning(
                    "Ignoring corrupt authorship shard: %s",
                    path,
                    exc_info=True,
                )
        return shards

    def _commit_shard_to_state_branch(
        self, path: Path, sha: str, role: str, pipeline_id: str
    ) -> None:
        """Replicate the shard change to the state branch.

        In the default (async) mode the work is enqueued onto the
        process-wide flusher so ``register()`` returns within
        microseconds.  In synchronous mode (tests) the git commit runs
        inline.
        """
        assert self._state_store is not None
        if self._synchronous:
            _commit_one_shard_inline(self._state_store, path, sha, role, pipeline_id)
            return
        flusher = self._get_or_start_flusher()
        flusher.enqueue(path, sha, role, pipeline_id)

    def _get_or_start_flusher(self) -> _AuthorshipFlusher:
        """Lazily instantiate the flusher on first registration.

        Lazy construction means tests that only exercise the
        pure-filesystem mode never spin up a worker thread, and the
        process-wide singleton starts the flusher exactly once.
        """
        with self._flusher_lock:
            if self._flusher is None:
                assert self._state_store is not None
                self._flusher = _AuthorshipFlusher(self._state_store)
            return self._flusher

    def flush(self, timeout: float | None = None) -> bool:
        """Block until any pending state-branch commits have landed.

        Returns True when the queue drained within ``timeout`` seconds
        (or unconditionally when no flusher has been started).  Tests
        that need to observe state-branch state after ``register()`` use
        this to avoid races; production callers usually do not need it.
        """
        with self._flusher_lock:
            flusher = self._flusher
        if flusher is None:
            return True
        return flusher.flush(timeout=timeout)

    def _shutdown_flusher(self, *, timeout: float = 5.0) -> None:
        """Stop the background flusher after one final drain attempt.

        Exposed for tests / ``reset_singleton``; callers should not need
        it during normal operation (the daemon thread exits with the
        process and ``atexit`` flushes pending work).
        """
        with self._flusher_lock:
            flusher = self._flusher
            self._flusher = None
        if flusher is not None:
            flusher.shutdown(timeout=timeout)


_singleton: CommitAuthorshipStore | None = None
_singleton_lock = threading.Lock()


def _resolve_authorship_repo_path() -> Path:
    """Resolve ``EGG_REPO_PATH`` to a single git repo for the authorship store.

    The authorship store keeps a single shared shard tree across all repos
    (sharded by pipeline_id, with ``repo`` recorded as advisory metadata
    on each entry — see ``register``).  It needs exactly one git repo
    whose state branch can host that tree.

    ``EGG_AUTHORSHIP_REPO`` (optional) explicitly names the repo path or
    repo directory name to use.  When set, it bypasses ``EGG_REPO_PATH``
    discovery entirely — useful for forked / renamed deployments where
    the default ``egg``-name preference would silently pick the wrong
    repo.

    ``EGG_REPO_PATH`` may otherwise point at:

    1. A single git repo (e.g. ``/home/egg/repos/egg``) — use it directly.
    2. A parent directory containing several repos (e.g. ``/home/egg/repos``
       with ``egg/``, ``actions/``, …) — pick one, preferring the ``egg``
       repo by name (the orchestrator's own repo, where pipeline state
       conventionally lives).  Falls back to the first repo alphabetically
       when ``egg`` is absent so the failure mode is deterministic in
       hand-rolled deployments.
    3. A non-existent or non-repo path — return the env value verbatim and
       let ``get_state_store`` raise its own actionable error
       (the defensive ``_ensure_worktree`` guard catches direct
       ``StateStore`` constructions that bypass this resolver).

    Empty / unset ``EGG_REPO_PATH`` keeps the historical default
    ``/home/egg/repos/egg`` for backwards compatibility with single-repo
    deployments that never set the env var.
    """
    from state_store import discover_repo_paths  # type: ignore[import-not-found]

    env_path = Path(os.environ.get("EGG_REPO_PATH", "/home/egg/repos/egg"))

    # Explicit override wins over discovery — handles forked / renamed
    # deployments where the alphabetical fallback would pick the wrong
    # repo.  Accepts either an absolute path or a repo name relative to
    # ``EGG_REPO_PATH``.
    override = os.environ.get("EGG_AUTHORSHIP_REPO", "").strip()
    if override:
        override_path = Path(override)
        if override_path.is_absolute():
            return override_path
        return env_path / override

    repos: list[Path] = discover_repo_paths(env_path)
    if len(repos) == 0:
        return env_path
    if len(repos) == 1:
        return repos[0]
    return next((r for r in repos if r.name == "egg"), repos[0])


def get_store(state_store: StateStore | None = None) -> CommitAuthorshipStore:
    """Return a process-wide singleton backed by the state store.

    Unit tests that need isolation should instantiate ``CommitAuthorshipStore``
    directly with an explicit ``worktree_dir`` rather than calling this
    helper.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            if state_store is None:
                # Late import to avoid pulling in the full state_store
                # module graph at import time.
                #
                # Route through ``get_state_store`` so the worktree path
                # matches the rest of the orchestrator (multi-repo
                # deployments derive ``pipeline-worktree-<name>`` from
                # the repo dir name — see ``state_store.get_state_store``).
                # Constructing ``StateStore`` directly would fall back to
                # the default ``pipeline-worktree`` path and conflict with
                # ``unified_sse`` / ``routes/health`` over the
                # ``egg/pipeline-state`` branch.
                from state_store import (  # type: ignore[import-not-found]
                    get_state_store as _get_state_store,
                )

                repo_path = _resolve_authorship_repo_path()
                _singleton = CommitAuthorshipStore(state_store=_get_state_store(repo_path))
            else:
                _singleton = CommitAuthorshipStore(state_store=state_store)
        return _singleton


def reset_singleton() -> None:
    """Drop the process-wide singleton.  Intended for tests only."""
    global _singleton
    with _singleton_lock:
        existing = _singleton
        _singleton = None
    if existing is not None:
        try:
            existing._shutdown_flusher(timeout=2.0)
        except Exception:  # pragma: no cover - defensive
            logger.debug("authorship_flusher_reset_shutdown_failed", exc_info=True)
