"""Unit tests for agent_salvage (#2429)."""

from __future__ import annotations

import subprocess
from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from agent_salvage import (
    _UNCOMMITTED_SALVAGE_MESSAGE,
    RECOVERY_BRANCH_PREFIX,
    AgentWorktree,
    SalvageResult,
    UnpushedCommit,
    WorktreeCommitReport,
    auto_salvage_pipeline,
    commit_working_tree,
    enumerate_agent_worktrees,
    list_unpushed_commits,
    salvage_worktree,
)
from gateway_client import PushResult

# ---------------------------------------------------------------------------
# Test helpers — build real git repos under tmp_path so the salvage helper
# exercises real git plumbing rather than mocked subprocess output.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.email=t@t.com",
            "-c",
            "user.name=Tester",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(path: Path, branch_name: str) -> str:
    """Initialise a repo at *path* on *branch_name* with one commit. Returns HEAD SHA."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--initial-branch", branch_name, cwd=path)
    (path / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "seed", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content)
    _git("add", filename, cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _set_assigned_branch(repo_path: Path, local_branch: str, assigned_branch: str) -> None:
    """Mirror what gateway worktree_manager configures at create time."""
    _git(
        "config",
        f"branch.{local_branch}.merge",
        f"refs/heads/{assigned_branch}",
        cwd=repo_path,
    )


def _create_remote_tracking(repo_path: Path, remote_branch: str, sha: str) -> None:
    """Stand in for ``origin/<branch>`` after a fetch."""
    _git("update-ref", f"refs/remotes/origin/{remote_branch}", sha, cwd=repo_path)


def _make_worktree_layout(
    base: Path,
    pipeline_id: str,
    *,
    agent_role: str | None,
    slice_id: str | None,
) -> tuple[Path, str]:
    """Create ``{base}/{worktree_id}/repo/`` with a real git repo on
    ``egg/{worktree_id}/work``. Returns (repo_path, local_branch).
    """
    if agent_role is None:
        worktree_id = pipeline_id
    elif slice_id is None:
        worktree_id = f"{pipeline_id}-{agent_role}"
    else:
        worktree_id = f"{pipeline_id}-{slice_id}-{agent_role}"
    local_branch = f"egg/{worktree_id}/work"
    repo_path = base / worktree_id / "repo"
    _make_repo(repo_path, local_branch)
    return repo_path, local_branch


# ---------------------------------------------------------------------------
# enumerate_agent_worktrees
# ---------------------------------------------------------------------------


class TestEnumerate:
    def test_empty_base_dir(self, tmp_path: Path) -> None:
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            assert enumerate_agent_worktrees("issue-99") == []

    def test_pipeline_level_worktree(self, tmp_path: Path) -> None:
        _make_worktree_layout(tmp_path, "issue-99", agent_role=None, slice_id=None)
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            wts = enumerate_agent_worktrees("issue-99")
        assert len(wts) == 1
        wt = wts[0]
        assert wt.worktree_id == "issue-99"
        assert wt.agent_role is None
        assert wt.slice_id is None
        assert wt.local_branch == "egg/issue-99/work"
        assert wt.repo_path == tmp_path / "issue-99" / "repo"

    def test_per_role_worktree(self, tmp_path: Path) -> None:
        _make_worktree_layout(tmp_path, "issue-99", agent_role="coder", slice_id=None)
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            wts = enumerate_agent_worktrees("issue-99")
        assert len(wts) == 1
        wt = wts[0]
        assert wt.agent_role == "coder"
        assert wt.slice_id is None
        assert wt.scope_label == "coder"

    def test_slice_scoped_worktree(self, tmp_path: Path) -> None:
        _make_worktree_layout(tmp_path, "issue-99", agent_role="coder", slice_id="slice-2")
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            wts = enumerate_agent_worktrees("issue-99")
        assert len(wts) == 1
        wt = wts[0]
        assert wt.agent_role == "coder"
        assert wt.slice_id == "slice-2"
        assert wt.worktree_id == "issue-99-slice-2-coder"
        assert wt.scope_label == "slice-2-coder"

    def test_mixed_worktrees(self, tmp_path: Path) -> None:
        _make_worktree_layout(tmp_path, "issue-99", agent_role=None, slice_id=None)
        _make_worktree_layout(tmp_path, "issue-99", agent_role="coder", slice_id=None)
        _make_worktree_layout(tmp_path, "issue-99", agent_role="tester", slice_id="slice-1")
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            wts = enumerate_agent_worktrees("issue-99")
        scope_labels = sorted(wt.scope_label for wt in wts)
        assert scope_labels == ["coder", "pipeline", "slice-1-tester"]

    def test_skips_unrelated_pipeline_with_shared_prefix(self, tmp_path: Path) -> None:
        """``issue-99-worktree-fix-tester`` must not be matched by ``issue-99``.

        Mirrors the #1865 protection in ``cleanup_pipeline`` — a shared
        prefix between two pipeline ids must not pull a sibling pipeline's
        worktree into the salvage scope.
        """
        # Active worktree of the *other* pipeline. Suffix "-worktree-fix-tester"
        # is not a valid AgentRole and not a slice shape, so the
        # enumerator should skip it.
        unrelated_id = "issue-99-worktree-fix-tester"
        _make_repo(tmp_path / unrelated_id / "repo", f"egg/{unrelated_id}/work")
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            wts = enumerate_agent_worktrees("issue-99")
        assert wts == []

    def test_skips_dir_without_git_marker(self, tmp_path: Path) -> None:
        (tmp_path / "issue-99-coder" / "repo").mkdir(parents=True)
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            wts = enumerate_agent_worktrees("issue-99")
        assert wts == []

    def test_validate_git_false_returns_broken_worktrees(self, tmp_path: Path) -> None:
        """``validate_git=False`` is the cleanup-style listing.

        Cleanup callers (e.g. phase restart) must still see worktrees
        with missing or unreadable ``.git`` markers — that's exactly the
        #1723 broken-btrfs-mount failure class the cleanup loop exists to
        delete. With ``validate_git=True`` (the salvage default) a broken
        worktree would be silently skipped and the wedged directory
        would survive restart.
        """
        # No .git anywhere — this would be skipped by the salvage default.
        (tmp_path / "issue-99-coder" / "repo").mkdir(parents=True)
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            wts = enumerate_agent_worktrees("issue-99", validate_git=False)
        assert len(wts) == 1
        wt = wts[0]
        assert wt.worktree_id == "issue-99-coder"
        assert wt.agent_role == "coder"
        assert wt.slice_id is None
        # Without a usable .git, repo_path falls back to the worktree dir
        # itself — cleanup callers don't need a real repo, just the id.
        assert wt.repo_path == tmp_path / "issue-99-coder"

    def test_validate_git_false_preserves_validated_repo_path(self, tmp_path: Path) -> None:
        """When .git IS present, ``validate_git=False`` still returns the validated path.

        The flag widens the filter (broken worktrees included) without
        changing the resolution for healthy worktrees — they keep their
        ``{worktree_dir}/{repo_short}/.git`` repo subdirectory so any
        downstream code that does pick up these entries can still operate
        on the real checkout.
        """
        _make_worktree_layout(tmp_path, "issue-99", agent_role="coder", slice_id=None)
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            wts = enumerate_agent_worktrees("issue-99", validate_git=False)
        assert len(wts) == 1
        assert wts[0].repo_path == tmp_path / "issue-99-coder" / "repo"

    def test_returns_empty_when_base_dir_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        with patch("agent_salvage.WORKTREE_BASE_DIR", missing):
            assert enumerate_agent_worktrees("issue-99") == []


# ---------------------------------------------------------------------------
# list_unpushed_commits
# ---------------------------------------------------------------------------


class TestListUnpushedCommits:
    def test_no_local_branch(self, tmp_path: Path) -> None:
        # repo on 'main', no egg/.../work branch — nothing to salvage.
        repo = tmp_path / "issue-99-coder" / "repo"
        _make_repo(repo, "main")
        wt = AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=repo,
            local_branch="egg/issue-99-coder/work",
        )
        report = list_unpushed_commits(wt)
        assert report.error is None
        assert report.commits == []
        assert report.assigned_branch is None

    def test_corrupt_worktree(self, tmp_path: Path) -> None:
        # No .git anywhere
        repo = tmp_path / "issue-99-coder" / "repo"
        repo.mkdir(parents=True)
        wt = AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=repo,
            local_branch="egg/issue-99-coder/work",
        )
        report = list_unpushed_commits(wt)
        assert report.error is not None
        assert report.commits == []

    def test_with_anchor_returns_only_unpushed(self, tmp_path: Path) -> None:
        repo, local_branch = _make_worktree_layout(
            tmp_path, "issue-99", agent_role="coder", slice_id=None
        )
        # Seed commit becomes the anchor (origin/<assigned>).
        anchor_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        _set_assigned_branch(repo, local_branch, "egg/issue-99/work")
        _create_remote_tracking(repo, "egg/issue-99/work", anchor_sha)

        # Two new local-only commits.
        sha1 = _commit(repo, "a.txt", "a\n", "first salvage commit")
        sha2 = _commit(repo, "b.txt", "b\n", "second salvage commit")

        wt = AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=repo,
            local_branch=local_branch,
        )
        report = list_unpushed_commits(wt)
        assert report.error is None
        assert report.assigned_branch == "egg/issue-99/work"
        assert report.anchor_ref == "refs/remotes/origin/egg/issue-99/work"
        # git log is newest-first.
        shas = [c.sha for c in report.commits]
        assert shas == [sha2, sha1]
        for c in report.commits:
            assert c.author == "Tester"
            assert c.files_changed == 1

    def test_falls_back_to_base_branch_anchor(self, tmp_path: Path) -> None:
        repo, local_branch = _make_worktree_layout(
            tmp_path, "issue-99", agent_role="coder", slice_id=None
        )
        anchor_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        # No assigned-branch tracking ref. Base-branch tracking only.
        _create_remote_tracking(repo, "main", anchor_sha)
        _commit(repo, "a.txt", "a\n", "post-anchor")
        wt = AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=repo,
            local_branch=local_branch,
        )
        report = list_unpushed_commits(wt, base_branch="main")
        assert report.anchor_ref == "refs/remotes/origin/main"
        assert len(report.commits) == 1
        assert report.commits[0].summary == "post-anchor"

    def test_no_anchor_falls_back_to_full_history(self, tmp_path: Path) -> None:
        repo, local_branch = _make_worktree_layout(
            tmp_path, "issue-99", agent_role="coder", slice_id=None
        )
        _commit(repo, "a.txt", "a\n", "extra")
        wt = AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=repo,
            local_branch=local_branch,
        )
        report = list_unpushed_commits(wt)  # no base_branch given
        assert report.anchor_ref is None
        assert len(report.commits) == 2  # seed + extra


# ---------------------------------------------------------------------------
# salvage_worktree
# ---------------------------------------------------------------------------


class TestSalvageWorktree:
    def _setup_worktree_with_unpushed(self, tmp_path: Path) -> tuple[AgentWorktree, str, str]:
        """Build a worktree with one unpushed commit on top of an anchor."""
        repo, local_branch = _make_worktree_layout(
            tmp_path, "issue-99", agent_role="coder", slice_id="slice-2"
        )
        anchor_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        _set_assigned_branch(repo, local_branch, "egg/issue-99/slice-2")
        _create_remote_tracking(repo, "egg/issue-99/slice-2", anchor_sha)
        head_sha = _commit(repo, "a.txt", "a\n", "salvage me")
        wt = AgentWorktree(
            worktree_id="issue-99-slice-2-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id="slice-2",
            repo_path=repo,
            local_branch=local_branch,
        )
        return wt, anchor_sha, head_sha

    def test_no_unpushed_commits_short_circuits(self, tmp_path: Path) -> None:
        repo, local_branch = _make_worktree_layout(
            tmp_path, "issue-99", agent_role="coder", slice_id=None
        )
        anchor_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        _set_assigned_branch(repo, local_branch, "egg/issue-99/work")
        _create_remote_tracking(repo, "egg/issue-99/work", anchor_sha)
        wt = AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=repo,
            local_branch=local_branch,
        )
        gateway = MagicMock()
        result = salvage_worktree(gateway, wt)
        assert result.ok is True
        assert result.n_commits == 0
        assert result.recovery_ref is None
        gateway.push_worktree_branch.assert_not_called()

    def test_pushes_to_recovery_ref(self, tmp_path: Path) -> None:
        wt, _, head_sha = self._setup_worktree_with_unpushed(tmp_path)
        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)

        result = salvage_worktree(gateway, wt, mode="public")

        assert result.ok is True
        assert result.head_sha == head_sha
        assert result.n_commits == 1
        expected_ref = f"{RECOVERY_BRANCH_PREFIX}/issue-99/slice-2-coder/{head_sha[:12]}"
        assert result.recovery_ref == expected_ref

        gateway.push_worktree_branch.assert_called_once()
        kwargs = gateway.push_worktree_branch.call_args.kwargs
        assert kwargs["pipeline_id"] == "issue-99"
        assert kwargs["repo_path"] == str(wt.repo_path)
        assert kwargs["branch"] == expected_ref
        assert kwargs["mode"] == "public"
        assert kwargs["ref"] is None
        assert kwargs["force"] is False

    def test_push_failure_returns_not_ok(self, tmp_path: Path) -> None:
        wt, _, _ = self._setup_worktree_with_unpushed(tmp_path)
        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(
            ok=False, category="non_fast_forward", detail="rejected"
        )
        result = salvage_worktree(gateway, wt)
        assert result.ok is False
        assert result.recovery_ref is None
        assert result.error is not None
        assert "non_fast_forward" in result.error

    def test_gateway_exception_returns_not_ok(self, tmp_path: Path) -> None:
        wt, _, _ = self._setup_worktree_with_unpushed(tmp_path)
        gateway = MagicMock()
        gateway.push_worktree_branch.side_effect = RuntimeError("boom")
        result = salvage_worktree(gateway, wt)
        assert result.ok is False
        assert "boom" in (result.error or "")

    def test_corrupt_worktree_returns_not_ok(self, tmp_path: Path) -> None:
        repo = tmp_path / "issue-99-coder" / "repo"
        repo.mkdir(parents=True)  # No .git
        wt = AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=repo,
            local_branch="egg/issue-99-coder/work",
        )
        gateway = MagicMock()
        result = salvage_worktree(gateway, wt)
        assert result.ok is False
        gateway.push_worktree_branch.assert_not_called()


# ---------------------------------------------------------------------------
# commit_working_tree / salvage_uncommitted (#2807)
# ---------------------------------------------------------------------------


class TestSalvageUncommitted:
    """The restart path commits the dirty working tree before pushing so a
    crash in the Edit→commit window survives the respawn's reset --hard.
    """

    def _clean_worktree(self, tmp_path: Path) -> AgentWorktree:
        """A worktree whose HEAD is fully pushed (no unpushed commits)."""
        repo, local_branch = _make_worktree_layout(
            tmp_path, "issue-99", agent_role="coder", slice_id=None
        )
        anchor = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        _set_assigned_branch(repo, local_branch, "egg/issue-99/work")
        _create_remote_tracking(repo, "egg/issue-99/work", anchor)
        return AgentWorktree(
            worktree_id="issue-99-coder",
            pipeline_id="issue-99",
            agent_role="coder",
            slice_id=None,
            repo_path=repo,
            local_branch=local_branch,
        )

    @staticmethod
    def _dirty(repo: Path) -> None:
        """Apply a tracked edit + an untracked add — the #2807 crash state."""
        (repo / "README.md").write_text("seed\nmid-Edit change\n")
        (repo / "new_feature.py").write_text("def added():\n    return 42\n")

    def test_commit_working_tree_returns_none_when_clean(self, tmp_path: Path) -> None:
        wt = self._clean_worktree(tmp_path)
        assert commit_working_tree(wt) is None

    def test_commit_working_tree_captures_dirty_state(self, tmp_path: Path) -> None:
        wt = self._clean_worktree(tmp_path)
        self._dirty(wt.repo_path)

        sha = commit_working_tree(wt)

        assert sha is not None
        # New commit is HEAD, carries the salvage message, and includes both
        # the tracked edit and the previously-untracked file.
        assert _git("rev-parse", "HEAD", cwd=wt.repo_path).stdout.strip() == sha
        assert (
            _git("log", "-1", "--format=%s", cwd=wt.repo_path).stdout.strip()
            == _UNCOMMITTED_SALVAGE_MESSAGE
        )
        assert (
            _git("show", "HEAD:new_feature.py", cwd=wt.repo_path).stdout
            == "def added():\n    return 42\n"
        )
        # Working tree is clean again — everything was captured.
        assert _git("status", "--porcelain", cwd=wt.repo_path).stdout.strip() == ""

    def test_salvage_pushes_uncommitted_edits_when_flag_set(self, tmp_path: Path) -> None:
        """salvage_uncommitted=True: dirty edits land in the pushed HEAD."""
        wt = self._clean_worktree(tmp_path)
        self._dirty(wt.repo_path)
        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)

        result = salvage_worktree(gateway, wt, mode="public", salvage_uncommitted=True)

        assert result.ok is True
        assert result.n_commits == 1
        head_sha = _git("rev-parse", "HEAD", cwd=wt.repo_path).stdout.strip()
        assert result.head_sha == head_sha
        expected_ref = f"{RECOVERY_BRANCH_PREFIX}/issue-99/coder/{head_sha[:12]}"
        assert result.recovery_ref == expected_ref

        # The push targets the recovery ref and pushes HEAD (ref=None), so the
        # captured edits are reachable from the recovery ref.
        kwargs = gateway.push_worktree_branch.call_args.kwargs
        assert kwargs["branch"] == expected_ref
        assert kwargs["ref"] is None
        assert (
            _git("show", "HEAD:new_feature.py", cwd=wt.repo_path).stdout
            == "def added():\n    return 42\n"
        )

    def test_default_does_not_capture_uncommitted(self, tmp_path: Path) -> None:
        """salvage_uncommitted defaults False: dirty tree is left untouched."""
        wt = self._clean_worktree(tmp_path)
        self._dirty(wt.repo_path)
        gateway = MagicMock()

        result = salvage_worktree(gateway, wt)

        # No commit made, nothing to push, working tree still dirty.
        assert result.n_commits == 0
        gateway.push_worktree_branch.assert_not_called()
        assert _git("status", "--porcelain", cwd=wt.repo_path).stdout.strip() != ""

    def test_auto_salvage_forwards_flag(self, tmp_path: Path) -> None:
        """auto_salvage_pipeline threads salvage_uncommitted to the push."""
        wt = self._clean_worktree(tmp_path)
        self._dirty(wt.repo_path)
        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)

        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(
                gateway,
                "issue-99",
                worktree_filter={"issue-99-coder"},
                salvage_uncommitted=True,
            )

        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].n_commits == 1
        gateway.push_worktree_branch.assert_called_once()


# ---------------------------------------------------------------------------
# auto_salvage_pipeline
# ---------------------------------------------------------------------------


class TestAutoSalvagePipeline:
    def test_filters_by_worktree_filter(self, tmp_path: Path) -> None:
        # Two worktrees on disk; salvage only one via filter.
        for role in ("coder", "tester"):
            repo, local_branch = _make_worktree_layout(
                tmp_path, "issue-99", agent_role=role, slice_id=None
            )
            anchor = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
            _set_assigned_branch(repo, local_branch, "egg/issue-99/work")
            _create_remote_tracking(repo, "egg/issue-99/work", anchor)
            _commit(repo, "x.txt", role, f"unpushed by {role}")

        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(
                gateway,
                "issue-99",
                worktree_filter={"issue-99-coder"},
            )
        assert [r.worktree_id for r in results] == ["issue-99-coder"]
        assert all(r.ok for r in results)

    def test_continues_on_per_worktree_failure(self, tmp_path: Path) -> None:
        for role in ("coder", "tester"):
            repo, local_branch = _make_worktree_layout(
                tmp_path, "issue-99", agent_role=role, slice_id=None
            )
            anchor = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
            _set_assigned_branch(repo, local_branch, "egg/issue-99/work")
            _create_remote_tracking(repo, "egg/issue-99/work", anchor)
            _commit(repo, "x.txt", role, f"unpushed by {role}")

        # First push raises, second succeeds — caller must see both rows.
        gateway = MagicMock()
        gateway.push_worktree_branch.side_effect = [
            RuntimeError("first one explodes"),
            PushResult(ok=True),
        ]
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(gateway, "issue-99")
        assert len(results) == 2
        ok_count = sum(1 for r in results if r.ok)
        # One success, one failure.
        assert ok_count == 1

    def test_never_raises_on_enumeration_failure(self, tmp_path: Path) -> None:
        gateway = MagicMock()
        with patch(
            "agent_salvage.enumerate_agent_worktrees",
            side_effect=RuntimeError("disk failure"),
        ):
            results = auto_salvage_pipeline(gateway, "issue-99")
        assert results == []

    def test_empty_pipeline_no_worktrees(self, tmp_path: Path) -> None:
        gateway = MagicMock()
        with patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path):
            results = auto_salvage_pipeline(gateway, "issue-99")
        assert results == []
        gateway.push_worktree_branch.assert_not_called()


# ---------------------------------------------------------------------------
# git-log shortstat parser
# ---------------------------------------------------------------------------


class TestParseGitLog:
    # ASCII unit separator (U+001F) — the field separator the production
    # ``--format`` string uses (`%x1f`). Picked because it cannot appear
    # in a commit subject, so the parser does not shift on a tab-bearing
    # subject the way the original ``%x09`` separator did.
    _US = "\x1f"

    def test_parses_single_commit_with_shortstat(self) -> None:
        from agent_salvage import _parse_git_log

        out = (
            f"abc1234567890{self._US}fix bug{self._US}Alice{self._US}2026-05-06T10:00:00+00:00\n"
            "\n"
            " 3 files changed, 12 insertions(+), 4 deletions(-)\n"
        )
        commits = _parse_git_log(out)
        assert len(commits) == 1
        c = commits[0]
        assert c.sha == "abc1234567890"
        assert c.summary == "fix bug"
        assert c.author == "Alice"
        assert c.files_changed == 3

    def test_parses_multiple_commits(self) -> None:
        from agent_salvage import _parse_git_log

        out = (
            f"sha1{self._US}one{self._US}A{self._US}2026-05-06T10:00:00+00:00\n"
            "\n"
            " 1 file changed, 1 insertion(+)\n"
            "\n"
            f"sha2{self._US}two{self._US}B{self._US}2026-05-06T11:00:00+00:00\n"
            "\n"
            " 2 files changed, 5 insertions(+)\n"
        )
        commits = _parse_git_log(out)
        assert [c.sha for c in commits] == ["sha1", "sha2"]
        assert [c.files_changed for c in commits] == [1, 2]

    def test_parses_commit_without_shortstat(self) -> None:
        from agent_salvage import _parse_git_log

        # Empty/merge commits may have no shortstat line.
        out = f"abc{self._US}empty merge{self._US}Bot{self._US}2026-05-06T10:00:00+00:00\n"
        commits = _parse_git_log(out)
        assert len(commits) == 1
        assert commits[0].files_changed == 0

    def test_tab_in_commit_subject_does_not_shift_fields(self) -> None:
        """Subjects with literal tabs must not bleed into trailing fields.

        Regression test for the original ``%x09`` (tab) separator: a commit
        whose subject contained a tab caused ``str.split('\\t', 3)`` to
        slice the subject in half and shift author / authored_at. The
        unit separator (`%x1f`) cannot appear in a subject, so the parser
        keeps every field intact.
        """
        from agent_salvage import _parse_git_log

        subject_with_tab = "fix:\tindentation in helper"
        out = (
            f"deadbeef{self._US}{subject_with_tab}{self._US}Alice"
            f"{self._US}2026-05-06T10:00:00+00:00\n"
        )
        commits = _parse_git_log(out)
        assert len(commits) == 1
        c = commits[0]
        assert c.sha == "deadbeef"
        assert c.summary == subject_with_tab
        assert c.author == "Alice"
        assert c.authored_at == "2026-05-06T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Static dataclass smoke tests — ensure public surface didn't drift
# ---------------------------------------------------------------------------


def test_recovery_ref_namespace_constant() -> None:
    assert RECOVERY_BRANCH_PREFIX == "egg/recovered"


def test_salvage_result_ok_with_no_commits_is_short_circuit() -> None:
    """``ok=True`` + ``recovery_ref=None`` is the canonical "nothing to do" shape."""
    r = SalvageResult(
        worktree_id="x",
        agent_role="coder",
        slice_id=None,
        recovery_ref=None,
        head_sha=None,
        n_commits=0,
        ok=True,
    )
    assert r.ok and r.recovery_ref is None and r.n_commits == 0


def test_unpushed_commit_is_frozen() -> None:
    c = UnpushedCommit(
        sha="x",
        summary="y",
        author="z",
        authored_at="2026-05-06",
        files_changed=1,
    )
    with pytest.raises((AttributeError, TypeError)):
        c.sha = "mutated"  # type: ignore[misc]


def test_worktree_commit_report_is_dataclass() -> None:
    wt = AgentWorktree(
        worktree_id="w",
        pipeline_id="p",
        agent_role="coder",
        slice_id=None,
        repo_path=Path("/tmp"),
        local_branch="egg/w/work",
    )
    report = WorktreeCommitReport(
        worktree=wt,
        assigned_branch="egg/p/work",
        anchor_ref=None,
        commits=[],
    )
    assert report.worktree is wt
    assert report.error is None


# ---------------------------------------------------------------------------
# BRC memory salvage / restore / validation (#3200 slice-1)
# ---------------------------------------------------------------------------

_SALVAGE_DIR = Path("/tmp/.egg-test-salvage")


class _MemoryFixture:
    """Helper to lay down brc-memory.md files under tmp_path for tests."""

    @staticmethod
    def create(
        base: Path,
        pipeline_id: str,
        role: str,
        content: str | None = None,
    ) -> Path:
        """Write a brc-memory.md for *role* under *base*/*role*/."""
        role_dir = base / role
        role_dir.mkdir(parents=True, exist_ok=True)
        mem_file = role_dir / "brc-memory.md"
        if content is None:
            content = (
                f"# BRC memory — {role} ({pipeline_id})\n\n"
                f"## My proposal\n- Artifacts: some/path.md\n"
                f"## Peer state\n- No peer proposals reviewed yet.\n"
            )
        mem_file.write_text(content)
        return mem_file


class TestSalvageBrcMemory:
    """salvage_brc_memory() copies brc-memory.md files to a salvage directory
    before worktree deletion, so they survive the restart."""

    def test_copies_single_role_memory(self, tmp_path: Path) -> None:
        from agent_salvage import salvage_brc_memory

        agent_outputs = tmp_path / "agent-outputs"
        salvage_base = tmp_path / "salvaged-memory"
        _MemoryFixture.create(agent_outputs, "issue-99", "coder")

        results = salvage_brc_memory("issue-99", agent_outputs, salvage_base)

        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.role == "coder"
        assert r.error is None
        # Verify destination file exists and has content.
        dest = salvage_base / "issue-99" / "coder" / "brc-memory-issue-99.md"
        assert dest.exists()
        assert dest.read_text() == r.content

    def test_copies_multiple_roles(self, tmp_path: Path) -> None:
        from agent_salvage import salvage_brc_memory

        agent_outputs = tmp_path / "agent-outputs"
        salvage_base = tmp_path / "salvaged-memory"
        for role in ("coder", "tester", "documenter"):
            _MemoryFixture.create(agent_outputs, "issue-99", role)

        results = salvage_brc_memory("issue-99", agent_outputs, salvage_base)
        roles = sorted(r.role for r in results)
        assert roles == ["coder", "documenter", "tester"]
        assert all(r.ok for r in results)

    def test_skips_dir_without_brc_memory(self, tmp_path: Path) -> None:
        from agent_salvage import salvage_brc_memory

        agent_outputs = tmp_path / "agent-outputs"
        salvage_base = tmp_path / "salvaged-memory"
        # Role dir exists but has no brc-memory.md.
        (agent_outputs / "coder").mkdir(parents=True)
        (agent_outputs / "coder" / "unrelated.txt").write_text("nothing\n")

        results = salvage_brc_memory("issue-99", agent_outputs, salvage_base)
        assert results == []

    def test_skips_non_role_directories(self, tmp_path: Path) -> None:
        from agent_salvage import salvage_brc_memory

        agent_outputs = tmp_path / "agent-outputs"
        salvage_base = tmp_path / "salvaged-memory"
        # A non-role file/directory at top level must not be scanned.
        (agent_outputs / "architect-output.json").write_text("{}\n")
        # Role dirs are still found.
        _MemoryFixture.create(agent_outputs, "issue-99", "coder")

        results = salvage_brc_memory("issue-99", agent_outputs, salvage_base)
        assert len(results) == 1
        assert results[0].role == "coder"

    def test_overwrites_existing_salvage(self, tmp_path: Path) -> None:
        """Re-salvage on a pipeline that already has salvaged memory replaces it."""
        from agent_salvage import salvage_brc_memory

        agent_outputs = tmp_path / "agent-outputs"
        salvage_base = tmp_path / "salvaged-memory"
        _MemoryFixture.create(agent_outputs, "issue-99", "coder", content="old content")

        # First salvage.
        salvage_brc_memory("issue-99", agent_outputs, salvage_base)

        # Update source and salvage again.
        _MemoryFixture.create(agent_outputs, "issue-99", "coder", content="new content")
        results = salvage_brc_memory("issue-99", agent_outputs, salvage_base)

        assert len(results) == 1
        dest = salvage_base / "issue-99" / "coder" / "brc-memory-issue-99.md"
        assert "new content" in dest.read_text()

    def test_failure_on_missing_source_dir_does_not_raise(self, tmp_path: Path) -> None:
        """If agent-outputs base dir doesn't exist, return empty list — never raise."""
        from agent_salvage import salvage_brc_memory

        missing = tmp_path / "does-not-exist"
        salvage_base = tmp_path / "salvaged-memory"
        results = salvage_brc_memory("issue-99", missing, salvage_base)
        assert results == []

    def test_failure_on_per_role_read_error_returns_not_ok(self, tmp_path: Path) -> None:
        """A role dir that exists but whose brc-memory.md is unreadable returns
        ok=False without blocking other roles."""
        from agent_salvage import salvage_brc_memory

        agent_outputs = tmp_path / "agent-outputs"
        salvage_base = tmp_path / "salvaged-memory"
        _MemoryFixture.create(agent_outputs, "issue-99", "coder")
        # Create a role dir with a brc-memory.md that is a directory (unreadable).
        bad_role_dir = agent_outputs / "reviewer_code"
        bad_role_dir.mkdir()
        (bad_role_dir / "brc-memory.md").mkdir()  # directory, not a file

        results = salvage_brc_memory("issue-99", agent_outputs, salvage_base)
        roles = sorted(r.role for r in results)
        assert roles == ["coder", "reviewer_code"]
        assert next(r for r in results if r.role == "coder").ok is True
        assert next(r for r in results if r.role == "reviewer_code").ok is False
        assert next(r for r in results if r.role == "reviewer_code").error is not None

    def test_destination_path_encodes_pipeline_id(self, tmp_path: Path) -> None:
        """Salvage destination path is
        ``<salvage>/<pipeline-id>/<role>/brc-memory-<pipeline-id>.md``
        so different pipelines do not collide even with the same role name."""
        from agent_salvage import salvage_brc_memory

        agent_outputs = tmp_path / "agent-outputs"
        salvage_base = tmp_path / "salvaged-memory"
        _MemoryFixture.create(agent_outputs, "issue-3200", "coder")

        results = salvage_brc_memory("issue-3200", agent_outputs, salvage_base)

        assert len(results) == 1
        expected = salvage_base / "issue-3200" / "coder" / "brc-memory-issue-3200.md"
        assert expected.exists()


class TestRestoreSalvagedMemory:
    """restore_salvaged_memory() reads a salvaged brc-memory.md for a
    specific pipeline + role and returns structured content."""

    def test_restores_existing_memory(self, tmp_path: Path) -> None:
        from agent_salvage import restore_salvaged_memory

        salvage_base = tmp_path / "salvaged-memory"
        content = "# BRC memory — tester (issue-99)\n\nKey findings: ...\n"
        dest = salvage_base / "issue-99" / "tester" / "brc-memory-issue-99.md"
        dest.parent.mkdir(parents=True)
        dest.write_text(content)

        restored = restore_salvaged_memory("issue-99", "tester", salvage_base)

        assert restored is not None
        assert restored.role == "tester"
        assert restored.pipeline_id == "issue-99"
        assert restored.content == content
        assert restored.source_path == dest

    def test_returns_none_when_no_salvage(self, tmp_path: Path) -> None:
        from agent_salvage import restore_salvaged_memory

        salvage_base = tmp_path / "salvaged-memory"
        restored = restore_salvaged_memory("issue-99", "missing_role", salvage_base)
        assert restored is None

    def test_returns_none_when_role_not_salvaged(self, tmp_path: Path) -> None:
        """Coder was salvaged, tester was not — tester restore returns None."""
        from agent_salvage import restore_salvaged_memory

        salvage_base = tmp_path / "salvaged-memory"
        dest = salvage_base / "issue-99" / "coder" / "brc-memory-issue-99.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("content")

        restored = restore_salvaged_memory("issue-99", "tester", salvage_base)
        assert restored is None

    def test_restored_memory_includes_timestamp(self, tmp_path: Path) -> None:
        """The RestoredMemory record includes when the restoration was attempted."""
        from datetime import datetime

        from agent_salvage import restore_salvaged_memory

        salvage_base = tmp_path / "salvaged-memory"
        dest = salvage_base / "issue-99" / "coder" / "brc-memory-issue-99.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("# memory")

        restored = restore_salvaged_memory("issue-99", "coder", salvage_base)

        assert restored is not None
        # restored_at should be a parseable ISO-8601 timestamp close to "now".
        ts = datetime.fromisoformat(restored.restored_at)
        assert ts.tzinfo is not None  # timezone-aware
        delta = datetime.now(UTC) - ts
        assert abs(delta.total_seconds()) < 30  # within 30s


class TestValidateSalvagedMemory:
    """validate_salvaged_memory() checks that a salvaged memory file is
    non-empty, has a parseable timestamp in the expected range, and
    belongs to the correct pipeline restart."""

    def test_valid_memory_passes(self, tmp_path: Path) -> None:
        from agent_salvage import validate_salvaged_memory

        content = "# BRC memory — coder (issue-99)\n\n## My proposal\nstuff\n"
        mem_file = tmp_path / "brc-memory.md"
        mem_file.write_text(content)

        ok, reason = validate_salvaged_memory("issue-99", mem_file)
        assert ok is True
        assert reason == ""

    def test_non_existent_file_fails(self, tmp_path: Path) -> None:
        from agent_salvage import validate_salvaged_memory

        missing = tmp_path / "does-not-exist.md"
        ok, reason = validate_salvaged_memory("issue-99", missing)
        assert ok is False
        assert "not found" in reason.lower() or "missing" in reason.lower()

    def test_empty_file_fails(self, tmp_path: Path) -> None:
        from agent_salvage import validate_salvaged_memory

        mem_file = tmp_path / "brc-memory.md"
        mem_file.write_text("")

        ok, reason = validate_salvaged_memory("issue-99", mem_file)
        assert ok is False
        assert "empty" in reason.lower() or "zero" in reason.lower()

    def test_zero_byte_file_fails(self, tmp_path: Path) -> None:
        from agent_salvage import validate_salvaged_memory

        mem_file = tmp_path / "brc-memory.md"
        mem_file.touch()  # zero-byte

        ok, reason = validate_salvaged_memory("issue-99", mem_file)
        assert ok is False
        assert "empty" in reason.lower() or "zero" in reason.lower()

    def test_memory_does_not_contain_pipeline_id_fails(self, tmp_path: Path) -> None:
        """The file must reference the pipeline it claims to belong to."""
        from agent_salvage import validate_salvaged_memory

        content = "# BRC memory — coder (wrong-pipeline)\n"
        mem_file = tmp_path / "brc-memory.md"
        mem_file.write_text(content)

        ok, reason = validate_salvaged_memory("issue-99", mem_file)
        assert ok is False
        # Reason should mention the pipeline mismatch.
        assert "issue-99" in reason.lower() or "pipeline" in reason.lower()

    def test_timestamp_in_valid_range_passes(self, tmp_path: Path) -> None:
        """A file whose mtime is within an expected time window passes."""
        from datetime import datetime, timedelta

        from agent_salvage import validate_salvaged_memory

        content = "# BRC memory — coder (issue-99)\n\n## My proposal\nstuff\n"
        mem_file = tmp_path / "brc-memory.md"
        mem_file.write_text(content)
        # Set mtime to 30 seconds ago — well within expected window.
        recent = (datetime.now(UTC) - timedelta(seconds=30)).timestamp()
        os_utime = __import__("os").utime
        os_utime(str(mem_file), (recent, recent))

        ok, reason = validate_salvaged_memory("issue-99", mem_file, max_age_seconds=3600)
        assert ok is True
        assert reason == ""

    def test_stale_timestamp_fails(self, tmp_path: Path) -> None:
        """A file whose mtime is older than the max age fails validation."""
        from datetime import datetime, timedelta

        from agent_salvage import validate_salvaged_memory

        content = "# BRC memory — coder (issue-99)\n\n## My proposal\nstuff\n"
        mem_file = tmp_path / "brc-memory.md"
        mem_file.write_text(content)
        # Set mtime to 2 hours ago — stale relative to 1-hour max age.
        stale = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
        os_utime = __import__("os").utime
        os_utime(str(mem_file), (stale, stale))

        ok, reason = validate_salvaged_memory("issue-99", mem_file, max_age_seconds=3600)
        assert ok is False
        assert "stale" in reason.lower() or "age" in reason.lower() or "expired" in reason.lower()


class TestAutoSalvagePipelineBrcMemory:
    """auto_salvage_pipeline() integration with BRC memory salvage:
    salvage_brc_memory is called alongside worktree salvage; a failure
    in memory salvage does not block worktree salvage."""

    def test_memory_salvage_is_best_effort(self, tmp_path: Path) -> None:
        """When salvage_brc_memory raises, auto_salvage_pipeline still
        completes worktree salvage — memory failure is logged, not propagated."""
        from agent_salvage import auto_salvage_pipeline

        repo, local_branch = _make_worktree_layout(
            tmp_path, "issue-99", agent_role="coder", slice_id=None
        )
        anchor = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        _set_assigned_branch(repo, local_branch, "egg/issue-99/work")
        _create_remote_tracking(repo, "egg/issue-99/work", anchor)
        _commit(repo, "x.txt", "a", "unpushed")

        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)

        # The salvage_brc_memory is called from within auto_salvage_pipeline;
        # we simulate a failure. The worktree salvage results must still come through.
        with (
            patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path),
            patch(
                "agent_salvage.salvage_brc_memory",
                side_effect=RuntimeError("memory salvage failed"),
            ),
        ):
            results = auto_salvage_pipeline(gateway, "issue-99")

        # Worktree salvage results are still returned.
        assert len(results) >= 1
        # At least the coder's worktree was salvaged.
        coder_results = [r for r in results if r.agent_role == "coder"]
        assert len(coder_results) == 1
        assert coder_results[0].ok is True
        gateway.push_worktree_branch.assert_called()

    def test_memory_salvage_happy_path(self, tmp_path: Path) -> None:
        """When both memory and worktree salvage succeed, results include
        SalvageMemoryResult metadata and worktree salvage results."""
        from agent_salvage import auto_salvage_pipeline

        repo, local_branch = _make_worktree_layout(
            tmp_path, "issue-99", agent_role="coder", slice_id=None
        )
        anchor = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        _set_assigned_branch(repo, local_branch, "egg/issue-99/work")
        _create_remote_tracking(repo, "egg/issue-99/work", anchor)
        _commit(repo, "x.txt", "a", "unpushed")

        agent_outputs = tmp_path / "agent-outputs"
        salvage_base = tmp_path / "salvaged-memory"
        _MemoryFixture.create(agent_outputs, "issue-99", "coder")

        gateway = MagicMock()
        gateway.push_worktree_branch.return_value = PushResult(ok=True)

        with (
            patch("agent_salvage.WORKTREE_BASE_DIR", tmp_path),
            patch("agent_salvage.AGENT_OUTPUT_BASE_DIR", agent_outputs),
            patch("agent_salvage.SALVAGE_BASE_DIR", salvage_base),
        ):
            results = auto_salvage_pipeline(gateway, "issue-99")

        assert len(results) >= 1
        dest = salvage_base / "issue-99" / "coder" / "brc-memory-issue-99.md"
        assert dest.exists()
