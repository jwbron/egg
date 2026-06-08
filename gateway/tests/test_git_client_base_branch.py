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
    _fetch_base_branch_best_effort,
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
    through to main/master rather than failing closed prematurely.

    Mock routing keys on the **subcommand verb at a fixed argv position** and
    on **exact equality of named positional argv slots**, never substring
    ``in`` checks against ``" ".join(cmd)`` — a `"origin/main"` substring
    would otherwise misroute if a future ref name happened to contain it
    (``origin/main-backup``) or if a ``-c`` config slot mentioned the trunk.
    """
    # git_cmd argv shapes the SUT calls. Verifying these here keeps the mock
    # honest if git_cmd grows new prefix args (e.g. extra ``-c`` flags).
    fetch_branch_argv = git_cmd("fetch", "origin", "branch")
    fetch_base_argv = git_cmd("fetch", "origin", "missing-base")
    primary_rev_list_argv = git_cmd("rev-list", "origin/branch..HEAD")
    rev_parse_base_argv = git_cmd("rev-parse", "--verify", "--quiet", "origin/missing-base")
    mb_base_argv = git_cmd("merge-base", "origin/missing-base", "HEAD")
    mb_main_argv = git_cmd("merge-base", "origin/main", "HEAD")
    # 40-char hex SHA — _SHA_LINE_RE requires 7-64 lowercase hex; the
    # harmonized validation in get_changed_files_in_push now rejects shorter
    # values too, so use a realistic SHA shape rather than the toy "abc123".
    fake_fork_point = "abcdef0123456789abcdef0123456789abcdef01"
    fake_commit = "1234567890abcdef1234567890abcdef12345678"
    rev_list_from_main_argv = git_cmd("rev-list", f"{fake_fork_point}..HEAD")
    diff_tree_argv = git_cmd("diff-tree", "--no-commit-id", "--name-only", "-r", fake_commit)

    with patch("subprocess.run") as mock_run:

        def side_effect(cmd, **kwargs):
            result = MagicMock()

            # Both fetches fail (pushed-branch + base_branch refs missing).
            if cmd == fetch_branch_argv or cmd == fetch_base_argv:
                result.returncode = 128
                result.stderr = "fatal: couldn't find remote ref"
                result.stdout = ""
                return result

            # rev-parse on origin/missing-base fails (ref never fetched), so
            # the helper falls through to the network fetch (which also fails).
            if cmd == rev_parse_base_argv:
                result.returncode = 128
                result.stdout = ""
                result.stderr = ""
                return result

            # Primary rev-list fails (new branch).
            if cmd == primary_rev_list_argv:
                result.returncode = 128
                result.stderr = "fatal: bad revision"
                result.stdout = ""
                return result

            # base_branch merge-base can't resolve (ref never fetched).
            if cmd == mb_base_argv:
                result.returncode = 128
                result.stderr = "fatal: not a valid object name"
                result.stdout = ""
                return result

            # main merge-base succeeds — the trailing fallback.
            if cmd == mb_main_argv:
                result.returncode = 0
                result.stdout = f"{fake_fork_point}\n"
                result.stderr = ""
                return result

            # rev-list of the post-fork-point range yields one synthetic SHA.
            if cmd == rev_list_from_main_argv:
                result.returncode = 0
                result.stdout = f"{fake_commit}\n"
                result.stderr = ""
                return result

            # diff-tree of that synthetic SHA reports one changed file.
            if cmd == diff_tree_argv:
                result.returncode = 0
                result.stdout = "src/app.py\n"
                result.stderr = ""
                return result

            result.returncode = 128
            result.stdout = ""
            result.stderr = ""
            return result

        mock_run.side_effect = side_effect

        files, error = get_changed_files_in_push(
            "/fake/repo", "origin", "branch", base_branch="missing-base"
        )

        assert error is None
        assert files == ["src/app.py"]

        # base_branch merge-base was tried **before** main: assert ordering
        # by indexing on the exact argv, not on substring containment.
        merge_base_calls = [
            call.args[0]
            for call in mock_run.call_args_list
            if len(call.args) > 0 and "merge-base" in call.args[0]
        ]
        assert mb_base_argv in merge_base_calls
        assert mb_main_argv in merge_base_calls
        assert merge_base_calls.index(mb_base_argv) < merge_base_calls.index(mb_main_argv)


# ---------------------------------------------------------------------------
# _fetch_base_branch_best_effort — rev-parse short-circuit avoids redundant
# fetch when the ref is already local (e.g. on the second call within one push)
# ---------------------------------------------------------------------------


class TestFetchBaseBranchBestEffort:
    def test_skips_fetch_when_ref_already_local(self):
        """If rev-parse resolves origin/<base_branch>, no network fetch runs.

        This is the redundant-fetch elimination: a single push calls both
        get_changed_files_in_push and _enumerate_push_commits, each of which
        runs this helper. The first call fetches; the second finds the ref
        locally and skips, halving the worst-case fallback latency.
        """
        with patch("subprocess.run") as mock_run:

            def side_effect(cmd, **kwargs):
                result = MagicMock()
                # rev-parse succeeds — ref is already local.
                if "rev-parse" in cmd:
                    result.returncode = 0
                    result.stdout = "deadbeefdeadbeef\n"
                    result.stderr = ""
                    return result
                # Any other call (we expect none) returns a failure to make
                # an accidental network fetch loud rather than silent.
                result.returncode = 128
                result.stdout = ""
                result.stderr = ""
                return result

            mock_run.side_effect = side_effect

            _fetch_base_branch_best_effort("/fake/repo", "origin", "base")

            argvs = [call.args[0] for call in mock_run.call_args_list if len(call.args) > 0]
            # rev-parse ran; fetch did NOT.
            assert any("rev-parse" in argv for argv in argvs)
            for argv in argvs:
                assert "fetch" not in argv, f"Unexpected fetch after successful rev-parse: {argv!r}"

    def test_fetches_when_ref_not_local(self):
        """If rev-parse fails, the helper falls through to git fetch."""
        with patch("subprocess.run") as mock_run:

            def side_effect(cmd, **kwargs):
                result = MagicMock()
                if "rev-parse" in cmd:
                    result.returncode = 128  # ref unknown locally
                    result.stdout = ""
                    result.stderr = ""
                    return result
                if "fetch" in cmd:
                    result.returncode = 0
                    result.stdout = ""
                    result.stderr = ""
                    return result
                result.returncode = 128
                return result

            mock_run.side_effect = side_effect

            _fetch_base_branch_best_effort("/fake/repo", "origin", "base")

            fetched = [
                call.args[0]
                for call in mock_run.call_args_list
                if len(call.args) > 0 and "fetch" in call.args[0]
            ]
            assert fetched, "expected git fetch to run when rev-parse fails"
            assert fetched[0] == git_cmd("fetch", "origin", "base")
