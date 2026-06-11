"""Tests for ``_build_slice_diff_summary`` (#3115).

The helper feeds the slice PR body's ``## What's in this PR`` section:
commit subjects (``git log origin/<parent>..origin/<head>``) and a
diffstat against the merge base
(``git diff --stat origin/<parent>...origin/<head>``). It is strictly
best-effort — any fetch/git failure returns ``(None, None)`` so PR
creation never blocks on it.

The tests build a real throwaway git repo and point the remote-tracking
refs (``refs/remotes/origin/...``) at local branches, since the helper
reads ``origin/<branch>`` after its (mocked-out) gateway fetch.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mock heavy dependencies before importing routes.pipelines.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
_shared_path = _orchestrator_path.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from routes.pipelines import _build_slice_diff_summary  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Repo with origin/work (one file) and origin/slice-1 (two commits on top)."""
    _git(tmp_path, "init", "-b", "work")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "base.py").write_text("base\n")
    _git(tmp_path, "add", "base.py")
    _git(tmp_path, "commit", "-m", "base commit")

    _git(tmp_path, "checkout", "-b", "slice-1")
    (tmp_path / "feature.py").write_text("feature\n")
    _git(tmp_path, "add", "feature.py")
    _git(tmp_path, "commit", "-m", "add feature module")
    (tmp_path / "feature.py").write_text("feature v2\n")
    _git(tmp_path, "add", "feature.py")
    _git(tmp_path, "commit", "-m", "iterate on feature")

    # The helper reads origin/<branch>; point the remote-tracking refs
    # at the local branches (its gateway fetch is mocked out).
    _git(tmp_path, "update-ref", "refs/remotes/origin/work", "refs/heads/work")
    _git(tmp_path, "update-ref", "refs/remotes/origin/slice-1", "refs/heads/slice-1")
    return tmp_path


def _pipeline() -> MagicMock:
    p = MagicMock()
    p.id = "pipeline-test"
    return p


def test_returns_subjects_and_diffstat(repo):
    spawner = MagicMock()
    subjects, diffstat = _build_slice_diff_summary(_pipeline(), spawner, repo, "slice-1", "work")
    # Newest-first log order; only slice commits, not the base commit.
    assert subjects == ["iterate on feature", "add feature module"]
    assert "feature.py" in diffstat
    assert "1 file changed" in diffstat
    assert "base.py" not in diffstat
    # Both branches refreshed via the gateway before reading.
    assert spawner.gateway.fetch_branch.call_count == 2


def test_fetch_failure_is_nonfatal(repo):
    """A failing gateway fetch degrades to the existing refs (which the
    fixture already planted) instead of dropping the whole summary."""
    spawner = MagicMock()
    spawner.gateway.fetch_branch.side_effect = RuntimeError("gateway down")
    subjects, diffstat = _build_slice_diff_summary(_pipeline(), spawner, repo, "slice-1", "work")
    assert subjects == ["iterate on feature", "add feature module"]
    assert diffstat is not None


def test_missing_refs_return_none(tmp_path):
    """No git repo at all → (None, None), no raise."""
    spawner = MagicMock()
    subjects, diffstat = _build_slice_diff_summary(
        _pipeline(), spawner, tmp_path, "slice-1", "work"
    )
    assert subjects is None
    assert diffstat is None


def test_identical_branches_return_none(repo):
    """Empty slice branch (tip == parent) → nothing to report."""
    spawner = MagicMock()
    subjects, diffstat = _build_slice_diff_summary(_pipeline(), spawner, repo, "work", "work")
    assert subjects is None
    assert diffstat is None
