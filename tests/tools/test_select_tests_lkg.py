"""TASK-5-3 — LKG sidecar I/O tests for scripts/select_tests/.

Covers:
  * Atomic write semantics (tempfile + os.replace; concurrent reader
    never sees a half-written file).
  * read_sidecar_lkg validation (40-hex; malformed contents = absent).
  * --record-good validation failures (regex / cat-file / ancestor)
    each surface a distinct stderr reason and exit non-zero.
  * --record-good no-op exits (detached HEAD, read-only role,
    missing branch) skip the write and exit 0.
  * Per-branch isolation — writing one branch's sidecar does not
    leak into another branch's sidecar file.

Tests do NOT depend on grimp; they exercise only sidecar I/O and the
record-good validation pipeline, which are pure-Python + git-shell.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import (
    _git,
    commit_file,
    in_chdir,
    init_git_repo,
    load_selector,
)

selector = load_selector()


# ----------------------------------------------------------------------
# Atomic write semantics
# ----------------------------------------------------------------------


def test_atomic_write_creates_directory_if_missing(tmp_path: Path) -> None:
    target = tmp_path / "deeply" / "nested" / "out.sha"
    selector._atomic_write_text(target, "abc" * 13 + "\n")
    assert target.read_text(encoding="utf-8").strip() == "abc" * 13


def test_atomic_write_replaces_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.sha"
    target.write_text("0" * 40 + "\n", encoding="utf-8")
    selector._atomic_write_text(target, "f" * 40 + "\n")
    assert target.read_text(encoding="utf-8").strip() == "f" * 40


def test_atomic_write_concurrent_reader_never_half_written(tmp_path: Path) -> None:
    """A reader looping while writers replace the sidecar must always
    see one of the previously-flushed contents, never a partial value.

    We loop 50 times to give the race a real chance to surface; on any
    one iteration where the reader sees a half-written file the test
    fails immediately (assert).  Atomic rename + ext4 / xfs sequencing
    means the write should be observed all-or-nothing.
    """
    target = tmp_path / "branch.sha"
    accepted = {"a" * 40, "b" * 40}
    target.write_text("a" * 40 + "\n", encoding="utf-8")

    stop = threading.Event()
    errors: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                content = target.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                continue
            if content not in accepted:
                errors.append(content)
                stop.set()
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for i in range(50):
            content = ("a" if i % 2 == 0 else "b") * 40 + "\n"
            selector._atomic_write_text(target, content)
    finally:
        stop.set()
    for t in threads:
        t.join(timeout=2)
    assert not errors, f"reader saw half-written content: {errors!r}"


def test_atomic_write_cleans_tempfile_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forcing os.replace to raise must leave no `.tmp` artifacts behind."""
    target = tmp_path / "branch.sha"
    real_replace = os.replace

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="synthetic failure"):
        selector._atomic_write_text(target, "x" * 40 + "\n")
    monkeypatch.setattr(os, "replace", real_replace)
    leftovers = list(tmp_path.glob(".branch.sha*.tmp"))
    assert leftovers == []


# ----------------------------------------------------------------------
# read_sidecar_lkg validation
# ----------------------------------------------------------------------


def test_read_sidecar_returns_none_for_missing_file(tmp_path: Path) -> None:
    with in_chdir(tmp_path):
        assert selector.read_sidecar_lkg("missing-branch") is None


def test_read_sidecar_returns_none_for_detached_head(tmp_path: Path) -> None:
    with in_chdir(tmp_path):
        assert selector.read_sidecar_lkg(None) is None


def test_read_sidecar_returns_sha_when_valid(tmp_path: Path) -> None:
    with in_chdir(tmp_path):
        sidecar_dir = tmp_path / selector.SIDECAR_DIR
        sidecar_dir.mkdir(parents=True)
        sha = "a" * 40
        (sidecar_dir / "main.sha").write_text(sha + "\n", encoding="utf-8")
        assert selector.read_sidecar_lkg("main") == sha


@pytest.mark.parametrize(
    "content",
    [
        "",  # empty
        "not-a-sha",  # bare text
        "a" * 39,  # 39 hex chars (regex fail, length)
        "a" * 41,  # 41 chars
        "A" * 40,  # uppercase hex (regex requires lowercase)
        "g" * 40,  # non-hex chars
        # binary-shaped content
        "\x00\x01" * 20,
    ],
)
def test_read_sidecar_treats_malformed_content_as_absent(tmp_path: Path, content: str) -> None:
    with in_chdir(tmp_path):
        sidecar_dir = tmp_path / selector.SIDECAR_DIR
        sidecar_dir.mkdir(parents=True)
        (sidecar_dir / "main.sha").write_text(content, encoding="utf-8")
        assert selector.read_sidecar_lkg("main") is None


def test_per_branch_sidecar_isolation(tmp_path: Path) -> None:
    """Writing one branch's sidecar must never bleed into another
    branch's sidecar.  Two simulated branches advance independently."""
    with in_chdir(tmp_path):
        sha_a = "a" * 40
        sha_b = "b" * 40
        selector.write_sidecar_lkg("feature-a", sha_a)
        selector.write_sidecar_lkg("feature-b", sha_b)
        assert selector.read_sidecar_lkg("feature-a") == sha_a
        assert selector.read_sidecar_lkg("feature-b") == sha_b
        # Updating one must not touch the other.
        sha_a2 = "c" * 40
        selector.write_sidecar_lkg("feature-a", sha_a2)
        assert selector.read_sidecar_lkg("feature-a") == sha_a2
        assert selector.read_sidecar_lkg("feature-b") == sha_b


# ----------------------------------------------------------------------
# --record-good validation failures
# ----------------------------------------------------------------------


def test_record_good_rejects_bad_regex(
    real_git, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A 39-char sha (regex failure) raises RecordGoodValidationError
    and the CLI converts to exit 1."""
    init_git_repo(tmp_path)
    bad_sha = "a" * 39
    with pytest.raises(selector.RecordGoodValidationError) as exc_info:
        selector.record_good(bad_sha, repo_root=tmp_path)
    assert "not 40 lowercase hex" in str(exc_info.value)


def test_record_good_rejects_unknown_sha(real_git, tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    # Valid regex but no such object in the repo.
    bogus_sha = "a" * 40
    with pytest.raises(selector.RecordGoodValidationError) as exc_info:
        selector.record_good(bogus_sha, repo_root=tmp_path)
    assert "not found in object database" in str(exc_info.value)


def test_record_good_rejects_non_ancestor(real_git, tmp_path: Path) -> None:
    """A sha that exists but is on a different branch (not an ancestor
    of HEAD) must be rejected."""
    init_git_repo(tmp_path)
    # Make a real commit on main, then branch off and make another.
    commit_file(tmp_path, "a.py", "x = 1\n", "main commit")
    main_sha_rc = _git(tmp_path, "rev-parse", "HEAD")
    main_sha = main_sha_rc.stdout.strip()
    # Switch to a side branch from initial commit.
    _git(tmp_path, "checkout", "-q", "-b", "side", "HEAD~1")
    side_sha = commit_file(tmp_path, "b.py", "y = 1\n", "side commit")
    # Currently on `side`; main_sha is NOT an ancestor of side_sha.
    with pytest.raises(selector.RecordGoodValidationError) as exc_info:
        selector.record_good(main_sha, repo_root=tmp_path)
    assert "not an ancestor of HEAD" in str(exc_info.value)
    assert side_sha  # smoke


def test_record_good_writes_sidecar_on_valid_sha(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_git_repo(tmp_path)
    head_sha = commit_file(tmp_path, "a.py", "x = 1\n", "first commit")
    monkeypatch.chdir(tmp_path)
    # record_good defaults to HEAD when --sha is omitted.
    rc = selector.record_good(None, repo_root=tmp_path)
    assert rc == 0
    sidecar = tmp_path / selector.SIDECAR_DIR / "main.sha"
    assert sidecar.exists(), f"sidecar not written at {sidecar}"
    assert sidecar.read_text(encoding="utf-8").strip() == head_sha


def test_record_good_writes_explicit_sha(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_git_repo(tmp_path)
    sha_a = commit_file(tmp_path, "a.py", "x = 1\n", "first")
    sha_b = commit_file(tmp_path, "b.py", "y = 1\n", "second")
    monkeypatch.chdir(tmp_path)
    # Pass the older sha explicitly — it IS an ancestor of HEAD.
    rc = selector.record_good(sha_a, repo_root=tmp_path)
    assert rc == 0
    sidecar = tmp_path / selector.SIDECAR_DIR / "main.sha"
    assert sidecar.read_text(encoding="utf-8").strip() == sha_a
    # Sanity: the head is sha_b, but we recorded sha_a.
    assert sha_b != sha_a


# ----------------------------------------------------------------------
# --record-good skip paths (exit 0 with stderr notice)
# ----------------------------------------------------------------------


def test_record_good_skips_on_detached_head(
    real_git, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    init_git_repo(tmp_path)
    head_sha = commit_file(tmp_path, "a.py", "x = 1\n", "first commit")
    # Detach HEAD — `git checkout <sha>` puts us on detached HEAD.
    _git(tmp_path, "checkout", "-q", head_sha)
    rc = selector.record_good(None, repo_root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert selector.STDERR_DETACHED_HEAD_RECORD_NOTICE in captured.err
    # Sidecar must NOT have been written.
    assert (
        not (tmp_path / selector.SIDECAR_DIR).exists()
        or list((tmp_path / selector.SIDECAR_DIR).iterdir()) == []
    )


def test_record_good_skips_on_readonly_role_env(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "a.py", "x = 1\n", "first commit")
    monkeypatch.setenv("EGG_AGENT_ROLE", "reviewer_plan")
    rc = selector.record_good(None, repo_root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert selector.STDERR_READONLY_RECORD_NOTICE in captured.err


def test_record_good_skips_on_readonly_marker(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "a.py", "x = 1\n", "first commit")
    (tmp_path / ".egg-readonly").write_text("", encoding="utf-8")
    # Ensure the env-var path is not what fires.
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    rc = selector.record_good(None, repo_root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert selector.STDERR_READONLY_RECORD_NOTICE in captured.err


@pytest.mark.parametrize(
    "writer_role", ["coder", "tester", "documenter", "task_planner", "architect"]
)
def test_record_good_writes_for_writer_roles(
    real_git,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_role: str,
) -> None:
    init_git_repo(tmp_path)
    head_sha = commit_file(tmp_path, "a.py", "x = 1\n", "first")
    monkeypatch.setenv("EGG_AGENT_ROLE", writer_role)
    monkeypatch.chdir(tmp_path)
    rc = selector.record_good(None, repo_root=tmp_path)
    assert rc == 0
    assert (tmp_path / selector.SIDECAR_DIR / "main.sha").read_text(
        encoding="utf-8"
    ).strip() == head_sha


def test_record_good_idempotent_on_repeat_call(
    real_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running --record-good twice in a row writes the same sha
    (HEAD doesn't move) and does not error out."""
    init_git_repo(tmp_path)
    head_sha = commit_file(tmp_path, "a.py", "x = 1\n", "first")
    monkeypatch.chdir(tmp_path)
    assert selector.record_good(None, repo_root=tmp_path) == 0
    assert selector.record_good(None, repo_root=tmp_path) == 0
    sidecar = tmp_path / selector.SIDECAR_DIR / "main.sha"
    assert sidecar.read_text(encoding="utf-8").strip() == head_sha
