"""TASK-5-3 — Baseline-resolution tests for scripts/select_tests/.

Covers:
  * sidecar present-and-ancestor → returns the LKG sha.
  * sidecar present-but-not-ancestor → falls through to base-branch.
  * sidecar absent → falls through to base-branch.
  * origin/main missing → UNRESOLVABLE.
  * EGG_AGENT_ROLE=reviewer_*/refiner ignores the sidecar even when
    valid; coder/tester/unset use the LKG-preferred path.
  * .egg-readonly marker triggers the read-only path even when
    EGG_AGENT_ROLE is unset.
  * Detached HEAD prints the documented stderr notice and falls back
    to base-branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import (
    _git,
    commit_file,
    init_git_repo,
    load_selector,
)

selector = load_selector()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _repo_with_two_commits(repo: Path) -> tuple[str, str]:
    """Return (initial_sha, head_sha) — repo has two commits on `main`."""
    init_git_repo(repo)
    rc = _git(repo, "rev-parse", "HEAD")
    initial_sha = rc.stdout.strip()
    head_sha = commit_file(repo, "a.py", "x = 1\n", "second commit")
    # Refresh origin/main to point at the LATEST commit so the merge-base
    # against origin/main resolves correctly.  ``init_git_repo`` only
    # synced the empty initial commit.
    _git(repo, "update-ref", "refs/remotes/origin/main", head_sha)
    return initial_sha, head_sha


# ----------------------------------------------------------------------
# Sidecar present-and-ancestor → LKG path
# ----------------------------------------------------------------------


def test_baseline_sidecar_ancestor_returns_lkg_sha(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha, head_sha = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Write the sidecar to the initial commit (ancestor of HEAD).
    selector.write_sidecar_lkg("main", initial_sha)
    sha, source, branch = selector.resolve_baseline(repo_root=tmp_path)
    assert sha == initial_sha
    assert source == "LKG"
    assert branch == "main"


def test_baseline_sidecar_not_ancestor_falls_to_base_branch(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar pointing at a sha that is NOT an ancestor of HEAD must
    NOT be used; the resolver falls through to origin/main and returns
    BASE_BRANCH source."""
    initial_sha, head_sha = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Forge a sidecar whose sha is a real-but-stale value: pick any
    # sha that's not an ancestor of HEAD.  Easiest: branch off, commit,
    # capture sha, then go back to main.
    _git(tmp_path, "checkout", "-q", "-b", "side", "HEAD~1")
    side_sha = commit_file(tmp_path, "side.py", "z = 1\n", "side commit")
    _git(tmp_path, "checkout", "-q", "main")
    selector.write_sidecar_lkg("main", side_sha)
    sha, source, branch = selector.resolve_baseline(repo_root=tmp_path)
    # side_sha is on `side`, not an ancestor of HEAD on main, so falls
    # through.
    assert source == "BASE_BRANCH"
    assert branch == "main"
    # Returned sha should be the merge-base with origin/main, which
    # equals HEAD because origin/main was updated to HEAD.
    assert sha == head_sha


def test_baseline_sidecar_absent_uses_base_branch(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha, head_sha = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(tmp_path)
    sha, source, branch = selector.resolve_baseline(repo_root=tmp_path)
    assert source == "BASE_BRANCH"
    assert sha == head_sha  # merge-base HEAD origin/main == HEAD


def test_baseline_origin_missing_returns_unresolvable(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When origin/<base> doesn't exist, source is UNRESOLVABLE."""
    init_git_repo(tmp_path)
    head_sha = commit_file(tmp_path, "a.py", "x = 1\n", "first")
    monkeypatch.chdir(tmp_path)
    # Delete the origin/main ref so merge-base fails.
    _git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")
    sha, source, branch = selector.resolve_baseline(repo_root=tmp_path)
    assert source == "UNRESOLVABLE"
    assert sha is None
    assert head_sha  # smoke


def test_baseline_respects_BASE_BRANCH_env_override(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BASE_BRANCH env var is set, the resolver consults
    origin/<that-name> instead of origin/main."""
    init_git_repo(tmp_path)
    head_sha = commit_file(tmp_path, "a.py", "x = 1\n", "first")
    # Set up origin/develop ref; remove origin/main so a wrong default
    # would surface as UNRESOLVABLE.
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    _git(tmp_path, "update-ref", "-d", "refs/remotes/origin/main")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BASE_BRANCH", "develop")
    sha, source, _branch = selector.resolve_baseline(repo_root=tmp_path)
    assert source == "BASE_BRANCH"
    assert sha == head_sha


# ----------------------------------------------------------------------
# EGG_AGENT_ROLE — read-only role detection (Q13 / R14)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    ["reviewer_code", "reviewer_contract", "reviewer_plan", "reviewer_refine", "refiner"],
)
def test_baseline_readonly_role_skips_sidecar(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A read-only role MUST skip the sidecar even when present and
    ancestor-valid; resolution falls through to base-branch."""
    initial_sha, head_sha = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(tmp_path)
    selector.write_sidecar_lkg("main", initial_sha)
    monkeypatch.setenv("EGG_AGENT_ROLE", role)
    sha, source, branch = selector.resolve_baseline(repo_root=tmp_path)
    assert source == "BASE_BRANCH", f"role={role!r} leaked sidecar use"
    assert branch == "main"


@pytest.mark.parametrize("role", ["coder", "tester", "documenter", "task_planner", "architect", ""])
def test_baseline_writer_role_uses_lkg(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Writer roles (coder, tester, etc.) and unset env take the
    LKG-preferred path when a valid sidecar is present."""
    initial_sha, _head_sha = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(tmp_path)
    selector.write_sidecar_lkg("main", initial_sha)
    if role:
        monkeypatch.setenv("EGG_AGENT_ROLE", role)
    else:
        monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    sha, source, _branch = selector.resolve_baseline(repo_root=tmp_path)
    assert source == "LKG"
    assert sha == initial_sha


def test_baseline_readonly_marker_skips_sidecar(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.egg-readonly` marker triggers read-only even with EGG_AGENT_ROLE
    unset — the second R14 detection signal."""
    initial_sha, _head = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(tmp_path)
    selector.write_sidecar_lkg("main", initial_sha)
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    (tmp_path / ".egg-readonly").write_text("", encoding="utf-8")
    sha, source, _branch = selector.resolve_baseline(repo_root=tmp_path)
    assert source == "BASE_BRANCH"


# ----------------------------------------------------------------------
# Detached-HEAD notice
# ----------------------------------------------------------------------


def test_baseline_detached_head_logs_notice_and_uses_base_branch(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    init_git_repo(tmp_path)
    head_sha = commit_file(tmp_path, "a.py", "x = 1\n", "first")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", head_sha)
    # Detach HEAD.
    _git(tmp_path, "checkout", "-q", head_sha)
    monkeypatch.chdir(tmp_path)
    sha, source, branch = selector.resolve_baseline(repo_root=tmp_path)
    captured = capsys.readouterr()
    assert selector.STDERR_DETACHED_HEAD_NOTICE in captured.err
    assert branch is None  # detached
    # Base-branch resolution still works (origin/main exists).
    assert source == "BASE_BRANCH"
    assert sha == head_sha


# ----------------------------------------------------------------------
# lkg_is_stale helper — used by the run flow to surface the
# "LKG not ancestor" trigger.
# ----------------------------------------------------------------------


def test_lkg_is_stale_returns_true_for_non_ancestor(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha, head_sha = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Branch + commit so we get a sha that's not ancestor of main HEAD.
    _git(tmp_path, "checkout", "-q", "-b", "side", "HEAD~1")
    side_sha = commit_file(tmp_path, "side.py", "z = 1\n", "side commit")
    _git(tmp_path, "checkout", "-q", "main")
    selector.write_sidecar_lkg("main", side_sha)
    assert selector.lkg_is_stale(repo_root=tmp_path) is True


def test_lkg_is_stale_false_for_ancestor(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha, head_sha = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(tmp_path)
    selector.write_sidecar_lkg("main", initial_sha)
    assert selector.lkg_is_stale(repo_root=tmp_path) is False


def test_lkg_is_stale_false_for_readonly_role(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only roles never see staleness — the sidecar isn't read at all."""
    initial_sha, head_sha = _repo_with_two_commits(tmp_path)
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "side", "HEAD~1")
    side_sha = commit_file(tmp_path, "side.py", "z = 1\n", "side commit")
    _git(tmp_path, "checkout", "-q", "main")
    selector.write_sidecar_lkg("main", side_sha)
    monkeypatch.setenv("EGG_AGENT_ROLE", "reviewer_plan")
    assert selector.lkg_is_stale(repo_root=tmp_path) is False


def test_lkg_is_stale_false_when_sidecar_missing(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "a.py", "x = 1\n", "first")
    monkeypatch.chdir(tmp_path)
    assert selector.lkg_is_stale(repo_root=tmp_path) is False


# ----------------------------------------------------------------------
# changed_files diff helper
# ----------------------------------------------------------------------


def test_changed_files_combines_committed_and_uncommitted(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_git_repo(tmp_path)
    base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    commit_file(tmp_path, "committed.py", "x = 1\n", "added committed.py")
    monkeypatch.chdir(tmp_path)
    # Add uncommitted change.
    (tmp_path / "uncommitted.py").write_text("y = 1\n", encoding="utf-8")
    paths = selector.changed_files(base_sha, repo_root=tmp_path)
    assert "committed.py" in paths
    assert "uncommitted.py" in paths


def test_changed_files_handles_renames(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git status --porcelain` reports renames as `R  old -> new`;
    both old and new paths must surface in the diff list."""
    init_git_repo(tmp_path)
    commit_file(tmp_path, "old.py", "x = 1\n", "first")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.chdir(tmp_path)
    # Rename old.py → new.py via git mv (gives the porcelain "R " status).
    _git(tmp_path, "mv", "old.py", "new.py")
    paths = selector.changed_files(base_sha, repo_root=tmp_path)
    assert "old.py" in paths
    assert "new.py" in paths


def test_changed_files_returns_empty_on_clean_tree(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_git_repo(tmp_path)
    head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.chdir(tmp_path)
    paths = selector.changed_files(head_sha, repo_root=tmp_path)
    assert paths == []
