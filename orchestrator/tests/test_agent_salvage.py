"""Unit tests for agent_salvage (#2429)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from agent_salvage import (
    RECOVERY_BRANCH_PREFIX,
    AgentWorktree,
    SalvageResult,
    UnpushedCommit,
    WorktreeCommitReport,
    auto_salvage_pipeline,
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
    def test_parses_single_commit_with_shortstat(self) -> None:
        from agent_salvage import _parse_git_log

        out = (
            "abc1234567890\tfix bug\tAlice\t2026-05-06T10:00:00+00:00\n"
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
            "sha1\tone\tA\t2026-05-06T10:00:00+00:00\n"
            "\n"
            " 1 file changed, 1 insertion(+)\n"
            "\n"
            "sha2\ttwo\tB\t2026-05-06T11:00:00+00:00\n"
            "\n"
            " 2 files changed, 5 insertions(+)\n"
        )
        commits = _parse_git_log(out)
        assert [c.sha for c in commits] == ["sha1", "sha2"]
        assert [c.files_changed for c in commits] == [1, 2]

    def test_parses_commit_without_shortstat(self) -> None:
        from agent_salvage import _parse_git_log

        # Empty/merge commits may have no shortstat line.
        out = "abc\tempty merge\tBot\t2026-05-06T10:00:00+00:00\n"
        commits = _parse_git_log(out)
        assert len(commits) == 1
        assert commits[0].files_changed == 0


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
