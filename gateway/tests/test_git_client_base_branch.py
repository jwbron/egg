"""Regression tests for #3024 — the restricted-path push guard must diff a
new-branch push against the pipeline's configured ``base_branch`` instead of
trunk (``main``/``master``).

When ``base_branch`` carries content not yet on trunk (e.g. an in-flight skills
library), the old merge-base-with-main fallback enumerated those inherited
commits and mis-attributed their files to the pushing role, blocking
non-documenter roles. These tests pin the corrected diff base.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from git_client import (  # noqa: E402
    _enumerate_push_commits,
    _fallback_base_candidates,
    get_changed_files_in_push,
    git_cmd,
)

# ---------------------------------------------------------------------------
# _fallback_base_candidates — ordering / dedup
# ---------------------------------------------------------------------------


class TestFallbackBaseCandidates:
    def test_base_branch_tried_first(self):
        assert _fallback_base_candidates("release-x") == ["release-x", "main", "master"]

    def test_none_keeps_trunk_only(self):
        assert _fallback_base_candidates(None) == ["main", "master"]

    def test_head_sentinel_ignored(self):
        # "HEAD" is the worktree-create default, not a real base — skip it.
        assert _fallback_base_candidates("HEAD") == ["main", "master"]

    def test_base_branch_equal_to_trunk_is_deduped(self):
        assert _fallback_base_candidates("main") == ["main", "master"]
        assert _fallback_base_candidates("master") == ["master", "main"]


# ---------------------------------------------------------------------------
# Real-git end-to-end regression (the #3024 scenario)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        git_cmd(*args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _build_repo(tmp_path: Path) -> Path:
    """Create a bare origin + a working clone modelling the #3024 setup.

    Layout produced in the working clone (``work``):
      - ``main``            : initial commit only
      - ``base``            : ``main`` + a documenter-owned skills file and
                              ``lint_ignorelist.txt`` (carried, not yet on main)
      - ``egg/p/work``      : ``base`` + the refiner's analysis draft only;
                              NOT pushed to origin (new-branch push scenario)
    Only ``main`` and ``base`` are pushed to origin.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main", str(origin))

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "--initial-branch=main", str(work))
    _git(work, "config", "user.email", "test@egg.local")
    _git(work, "config", "user.name", "egg test")
    _git(work, "remote", "add", "origin", str(origin))

    # main: initial commit
    _write(work, "README.md", "hello\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "main")

    # base: carries documenter-owned content not yet on main
    _git(work, "checkout", "-b", "base")
    _write(work, ".agents/skills/add-graphql-field/SKILL.md", "# skill\n")
    _write(work, "lint_ignorelist.txt", "ignore-me\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "carry skills library on base")
    _git(work, "push", "origin", "base")

    # pipeline branch off base: refiner writes only its analysis draft
    _git(work, "checkout", "-b", "egg/p/work")
    _write(work, ".egg-state/drafts/123-analysis.md", "# analysis\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "refiner analysis")
    return work


def test_base_branch_excludes_inherited_files(tmp_path):
    """With base_branch set, only the refiner's own commit is attributed —
    files inherited unchanged from base are not blamed on the push (#3024)."""
    work = _build_repo(tmp_path)

    files, error = get_changed_files_in_push(str(work), "origin", "egg/p/work", base_branch="base")

    assert error is None
    assert files == [".egg-state/drafts/123-analysis.md"]
    # The documenter-owned files inherited from base must NOT appear.
    assert ".agents/skills/add-graphql-field/SKILL.md" not in files
    assert "lint_ignorelist.txt" not in files


def test_without_base_branch_inherited_files_leak_in(tmp_path):
    """Pins the pre-fix behavior: diffing against trunk re-introduces the
    inherited base files (the false positive #3024 fixes). base_branch=None
    falls back to main, so the carried skills files appear in the diff."""
    work = _build_repo(tmp_path)

    files, error = get_changed_files_in_push(str(work), "origin", "egg/p/work", base_branch=None)

    assert error is None
    assert ".agents/skills/add-graphql-field/SKILL.md" in files
    assert "lint_ignorelist.txt" in files


def test_enumerate_push_commits_excludes_base_commits(tmp_path):
    """_enumerate_push_commits (attribution path) honours base_branch too —
    only the refiner's commit is enumerated, not the inherited base commit."""
    work = _build_repo(tmp_path)

    with_base, err_base = _enumerate_push_commits(
        str(work), "origin", "egg/p/work", base_branch="base"
    )
    without_base, err_none = _enumerate_push_commits(
        str(work), "origin", "egg/p/work", base_branch=None
    )

    assert err_base is None and err_none is None
    # base_branch scoping yields exactly one commit (the refiner's); the
    # trunk-based fallback also pulls in the base-carry commit.
    assert len(with_base) == 1
    assert len(without_base) > len(with_base)


# ---------------------------------------------------------------------------
# Fail-safe: an unfetchable base_branch falls through to trunk, not an error
# ---------------------------------------------------------------------------


def test_base_branch_fetch_failure_falls_through_to_trunk():
    """If origin/<base_branch> can't be resolved, the merge-base loop falls
    through to main/master rather than failing closed prematurely."""
    with patch("subprocess.run") as mock_run:

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            cmd_str = " ".join(cmd)

            if "fetch" in cmd:
                # Both the pushed-branch and base_branch fetches fail.
                result.returncode = 128
                result.stderr = "fatal: couldn't find remote ref"
                return result

            # Primary rev-list fails (new branch).
            if "rev-list" in cmd and "origin/branch..HEAD" in cmd_str:
                result.returncode = 128
                result.stderr = "fatal: bad revision"
                return result

            # base_branch merge-base can't resolve (ref never fetched).
            if "merge-base" in cmd and "origin/missing-base" in cmd_str:
                result.returncode = 128
                result.stderr = "fatal: not a valid object name"
                return result

            # main merge-base succeeds — the trailing fallback.
            if "merge-base" in cmd and "origin/main" in cmd_str:
                result.returncode = 0
                result.stdout = "abc123\n"
                return result

            if "rev-list" in cmd:
                result.returncode = 0
                result.stdout = "sha1\n"
                return result

            if "diff-tree" in cmd:
                result.returncode = 0
                result.stdout = "src/app.py\n"
                return result

            result.returncode = 128
            return result

        mock_run.side_effect = side_effect

        files, error = get_changed_files_in_push(
            "/fake/repo", "origin", "branch", base_branch="missing-base"
        )

        assert error is None
        assert files == ["src/app.py"]
        # base_branch was attempted before main.
        merge_base_refs = [
            " ".join(c[0][0]) for c in mock_run.call_args_list if "merge-base" in c[0][0]
        ]
        assert any("origin/missing-base" in r for r in merge_base_refs)
        assert any("origin/main" in r for r in merge_base_refs)
