"""Integration regression test for the recovery-ref TTL sweep (#2659 gap-4).

``orchestrator/tests/test_agent_salvage_cleanup.py`` covers each branch
of :func:`sweep_recovery_refs` in isolation: delete-old, skip-recent,
skip-reachable, skip-unknown-age, dry-run, already-deleted, error
fold-in. Each unit test exercises exactly one classification path.

A regression in the *aggregate* accounting — counters drifting across
actions, the ``_classify`` dispatch routing the wrong way, the
``oldest_remaining_age_days`` calculation folding in the wrong rows —
would slip past every per-branch unit test because none of them
straddle the TTL boundary in a single sweep.

This module builds a synthetic remote-tracking set with refs straddling
the boundary (old + delete, old + reachable, old + unknown-age,
recent + keep) and drives :func:`sweep_recovery_refs` once. The
assertions cover the entire :class:`CleanupReport` so a regression in
any single counter or in the deleted-vs-kept partition is caught here
even when every unit test still passes.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agent_salvage_cleanup import (
    CLEANUP_PIPELINE_ID,
    sweep_recovery_refs,
)
from gateway_client import PushResult

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Real-git helpers — mirror the unit-tier helpers in
# orchestrator/tests/test_agent_salvage_cleanup.py so a regression in
# committer-date parsing or for-each-ref reachability shows up here too.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.email=cleanup@test.example",
            "-c",
            "user.name=Cleanup Tester",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--initial-branch", "main", cwd=path)
    (path / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "seed", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _commit_orphan_at(
    path: Path,
    filename: str,
    content: str,
    message: str,
    *,
    when: datetime,
) -> str:
    """Build a single-commit orphan branch with a fixed committer date.

    Each ref in the straddle-TTL fixture must be reachability-independent
    of the others — otherwise the linear commit chain on ``main`` makes
    every older commit reachable from every younger one's ref. The
    sweep's reachability check (``git for-each-ref --contains=<sha>``)
    walks the full ancestry, so a shared parent commit silently
    re-classifies the older ref as "reachable" and the test would no
    longer exercise the delete branch.

    Each call here creates a *new orphan* (no parent) so the ref's tip
    is its only commit, making reachability strictly local to that ref.
    """
    branch = f"_orphan_{filename}"
    _git("checkout", "-q", "--orphan", branch, cwd=path)
    # ``checkout --orphan`` stages whatever was in the index; clear it
    # so the new branch genuinely starts empty.
    _git("rm", "-rf", "--cached", "-q", ".", cwd=path)
    # And wipe the working tree so the unrelated seed README isn't
    # picked up by the next ``add``.
    for entry in path.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            import shutil

            shutil.rmtree(entry)
        else:
            entry.unlink()
    (path / filename).write_text(content)
    _git("add", filename, cwd=path)
    env = os.environ.copy()
    env["GIT_COMMITTER_DATE"] = when.isoformat()
    env["GIT_AUTHOR_DATE"] = when.isoformat()
    cmd = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "user.email=cleanup@test.example",
        "-c",
        "user.name=Cleanup Tester",
        "-C",
        str(path),
        "commit",
        "-q",
        "-m",
        message,
    ]
    subprocess.run(cmd, cwd=path, capture_output=True, text=True, check=True, env=env)
    sha = _git("rev-parse", "HEAD", cwd=path).stdout.strip()
    # Return to ``main`` so the next orphan checkout doesn't error on
    # uncommitted state.
    _git("checkout", "-q", "main", cwd=path)
    return sha


def _set_remote_tracking(repo: Path, ref: str, sha: str) -> None:
    """Stand in for a fetched ``refs/remotes/origin/<ref>``."""
    _git("update-ref", f"refs/remotes/origin/{ref}", sha, cwd=repo)


class TestStraddleTtlBoundary:
    """One sweep across a mixed-state remote set: counters and the
    deleted-vs-kept partition must come out right.

    Fixture state per ref:

    | ref name suffix                       | committer date | reachability         | expected action |
    |---------------------------------------|----------------|----------------------|-----------------|
    | ``issue-1/coder/<old-1>``             | 200 days ago   | recovery-only        | delete          |
    | ``issue-1/tester/<old-2>``            | 200 days ago   | recovery-only        | delete          |
    | ``issue-2/coder/<old-reachable>``     | 200 days ago   | also on ``main``     | skip (reachable)|
    | ``issue-3/coder/<recent>``            | 5 days ago     | recovery-only        | skip (recent)   |
    | ``issue-4/coder/<unknown>``           | absent locally | absent locally       | skip (unknown)  |

    Aggregate assertions:
    - ``refs_inspected == 5``
    - ``refs_deleted == 2`` (only the two old + unreachable refs)
    - ``deleted_refs`` contains exactly those two names
    - ``refs_skipped_recent == 1``
    - ``refs_skipped_reachable == 1``
    - ``refs_skipped_unknown_age == 1``
    - ``oldest_remaining_age_days`` reflects the oldest *kept* ref
      (the reachable one at 200 days, not the unknown one)
    """

    def test_partitions_correctly_and_counters_add_up(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_repo(repo)

        old = datetime.now(UTC) - timedelta(days=200)
        recent = datetime.now(UTC) - timedelta(days=5)

        sha_old_1 = _commit_orphan_at(repo, "old1.txt", "x", "old work 1", when=old)
        ref_old_1 = "egg/recovered/issue-1/coder/aaa111aaa111"
        _set_remote_tracking(repo, ref_old_1, sha_old_1)

        sha_old_2 = _commit_orphan_at(repo, "old2.txt", "y", "old work 2", when=old)
        ref_old_2 = "egg/recovered/issue-1/tester/bbb222bbb222"
        _set_remote_tracking(repo, ref_old_2, sha_old_2)

        sha_reachable = _commit_orphan_at(repo, "reachable.txt", "z", "old reachable", when=old)
        ref_reachable = "egg/recovered/issue-2/coder/ccc333ccc333"
        _set_remote_tracking(repo, ref_reachable, sha_reachable)
        # ``main`` already carries this commit (operator replayed it).
        _set_remote_tracking(repo, "main", sha_reachable)

        sha_recent = _commit_orphan_at(repo, "recent.txt", "r", "fresh work", when=recent)
        ref_recent = "egg/recovered/issue-3/coder/ddd444ddd444"
        _set_remote_tracking(repo, ref_recent, sha_recent)

        # SHA deliberately never committed locally — unknown age path.
        ref_unknown = "egg/recovered/issue-4/coder/eee555eee555"
        sha_unknown = "0" * 40

        candidates = {
            ref_old_1: sha_old_1,
            ref_old_2: sha_old_2,
            ref_reachable: sha_reachable,
            ref_recent: sha_recent,
            ref_unknown: sha_unknown,
        }

        gateway = MagicMock()
        gateway.list_remote_branches_with_shas.return_value = candidates
        gateway.fetch_branch.return_value = True
        gateway.delete_remote_branch.return_value = PushResult(ok=True)

        report = sweep_recovery_refs(gateway, repo, ttl_days=90)

        assert report.refs_inspected == 5
        assert report.refs_deleted == 2
        assert set(report.deleted_refs) == {ref_old_1, ref_old_2}
        assert report.refs_skipped_recent == 1
        assert report.refs_skipped_reachable == 1
        assert report.refs_skipped_unknown_age == 1
        assert report.refs_skipped_error == 0

        # Only the two old + unreachable refs were actually deleted via
        # the gateway. The reachable + recent + unknown-age refs left it
        # alone.
        delete_calls = gateway.delete_remote_branch.call_args_list
        assert len(delete_calls) == 2
        deleted_branches = {call.kwargs["branch"] for call in delete_calls}
        assert deleted_branches == {ref_old_1, ref_old_2}
        for call in delete_calls:
            assert call.kwargs["pipeline_id"] == CLEANUP_PIPELINE_ID

        # ``oldest_remaining_age_days`` folds in only the *kept* refs
        # whose committer date was readable. The reachable ref at 200
        # days dominates the recent ref at 5 days; the unknown-age ref
        # contributes nothing (its committed_at is None).
        assert report.oldest_remaining_age_days is not None
        assert 195 <= report.oldest_remaining_age_days <= 205

    def test_dry_run_keeps_origin_intact_but_reports_what_would_delete(
        self, tmp_path: Path
    ) -> None:
        """Same fixture in ``dry_run=True``: no gateway delete, but
        ``deleted_refs`` still lists the same two ref names so operator
        previews are accurate.
        """
        repo = tmp_path / "repo"
        _make_repo(repo)

        old = datetime.now(UTC) - timedelta(days=200)
        recent = datetime.now(UTC) - timedelta(days=5)

        sha_old_1 = _commit_orphan_at(repo, "old1.txt", "x", "old work 1", when=old)
        ref_old_1 = "egg/recovered/issue-1/coder/aaa111aaa111"
        _set_remote_tracking(repo, ref_old_1, sha_old_1)

        sha_old_2 = _commit_orphan_at(repo, "old2.txt", "y", "old work 2", when=old)
        ref_old_2 = "egg/recovered/issue-1/tester/bbb222bbb222"
        _set_remote_tracking(repo, ref_old_2, sha_old_2)

        sha_recent = _commit_orphan_at(repo, "recent.txt", "r", "fresh", when=recent)
        ref_recent = "egg/recovered/issue-3/coder/ddd444ddd444"
        _set_remote_tracking(repo, ref_recent, sha_recent)

        gateway = MagicMock()
        gateway.list_remote_branches_with_shas.return_value = {
            ref_old_1: sha_old_1,
            ref_old_2: sha_old_2,
            ref_recent: sha_recent,
        }
        gateway.fetch_branch.return_value = True

        report = sweep_recovery_refs(gateway, repo, ttl_days=90, dry_run=True)

        # No actual deletes — counter stays at zero.
        assert report.refs_deleted == 0
        gateway.delete_remote_branch.assert_not_called()
        # But ``deleted_refs`` still reports what would have gone.
        assert set(report.deleted_refs) == {ref_old_1, ref_old_2}
        assert report.refs_skipped_recent == 1
