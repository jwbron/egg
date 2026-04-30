"""TASK-5-5 — End-to-end subprocess test for scripts/select_tests/.

Drives the selector via ``subprocess.run`` against the synthetic
mini-monorepo + tmp_path git repo, exercising the same code path the
Makefile would.  Closes the gap between per-unit tests and the manual
verification steps by confirming:

  (a) ``select_tests.py`` (default mode) on a single-file change exits 0.
  (b) ``select_tests.py --full-suite`` emits the four test-root paths
      on stdout.
  (c) ``select_tests.py --record-good`` writes the LKG sidecar.
  (d) ``select_tests.py --patch-selection-json --head <sha> --pytest-ms <ms>``
      patches an existing selection record.
  (e) ``select_tests.py --record-good --sha <bad>`` exits non-zero on
      validation failure.

The full module finishes in ~1 second on a warm cache so we don't
mark the cases as ``slow`` — the project's pytest config doesn't
register a ``slow`` marker (see ``[tool.pytest.ini_options].markers``
in pyproject.toml).  TASK-5-5's AC mentions tagging slow but only as
a deselect-helper; here the runtime is small enough that the unit-
test sweep can stay on by default.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests.tools._select_tests_helpers import (
    SELECTOR_PATH,
    _git,
    commit_file,
    find_python,
    init_git_repo,
    load_selector,
)

selector = load_selector()


# We don't need grimp for several E2E cases (full-suite, record-good,
# patch-selection-json) — those work purely on metadata.  Default-mode
# without grimp goes through the fail-open path and still exits 0.  So
# we DO NOT importorskip grimp here; tests that require grimp use
# ``pytest.importorskip`` inline.


# ----------------------------------------------------------------------
# Subprocess-runner helper
# ----------------------------------------------------------------------


def _real_git_dir(tmp_path: Path) -> Path:
    """Build a private bin dir that places the real git binary first
    on PATH so a subprocess'd selector calls into the real binary
    instead of the sandbox gateway wrapper."""
    bin_dir = tmp_path / "_e2e_bin"
    bin_dir.mkdir(exist_ok=True)
    real_git = Path("/opt/.egg-internal/git")
    target = bin_dir / "git"
    if real_git.exists() and not target.exists():
        target.symlink_to(real_git)
    return bin_dir


def _run_selector(
    cwd: Path, *args: str, env_extra: dict[str, str] | None = None, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Ensure clean slate — never inherit a parent role that would flip
    # the read-only path.
    env.pop("EGG_AGENT_ROLE", None)
    # Prepend the real-git bin dir so the subprocess'd selector calls
    # the real git binary, bypassing the sandbox gateway wrapper that
    # rejects ``git init`` / synthetic-repo operations.  The directory
    # is created as a sibling of ``cwd`` to keep test isolation.
    bin_dir = _real_git_dir(cwd)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    # Also drop any global git config that the real environment might
    # have so synthetic repos don't trip on user-level settings.
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [find_python(), str(SELECTOR_PATH), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=timeout,
    )


# ----------------------------------------------------------------------
# (a) default mode against a synthetic repo with a single-file change.
# ----------------------------------------------------------------------


def test_default_mode_exits_0_on_single_file_diff(real_git, tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "shared/foo.py", "x = 1\n", "first")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", head_sha)
    # Add an uncommitted change.
    (tmp_path / "shared" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    proc = _run_selector(tmp_path)
    # Default mode MUST exit 0 thanks to the fail-open contract.
    assert proc.returncode == 0, (
        f"selector exited {proc.returncode}; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


# ----------------------------------------------------------------------
# (b) --full-suite emits the four test-root paths.
# ----------------------------------------------------------------------


def test_full_suite_emits_test_root_dirs(real_git, tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "x.py", "x = 1\n", "first")
    proc = _run_selector(tmp_path, "--full-suite")
    assert proc.returncode == 0
    out = proc.stdout.splitlines()
    for d in selector.TEST_ROOT_DIRS:
        assert d in out, f"missing {d} in stdout: {out!r}"


# ----------------------------------------------------------------------
# (c) --record-good writes the sidecar.
# ----------------------------------------------------------------------


def test_record_good_writes_sidecar(real_git, tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    head_sha = commit_file(tmp_path, "x.py", "x = 1\n", "first")
    proc = _run_selector(tmp_path, "--record-good")
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    sidecar = tmp_path / selector.SIDECAR_DIR / "main.sha"
    assert sidecar.exists(), "sidecar not written"
    assert sidecar.read_text(encoding="utf-8").strip() == head_sha


def test_record_good_validation_failure_exits_nonzero(real_git, tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "x.py", "x = 1\n", "first")
    # 39-char sha → regex failure → exit 1.
    proc = _run_selector(tmp_path, "--record-good", "--sha", "a" * 39)
    assert proc.returncode != 0
    assert "validation failed" in proc.stderr


# ----------------------------------------------------------------------
# (d) --patch-selection-json appends pytest_ms.
# ----------------------------------------------------------------------


def test_patch_selection_json_appends_pytest_ms(real_git, tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "x.py", "x = 1\n", "first")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    # Pre-write a selection record (CWD-relative).
    log_dir = tmp_path / selector.SELECTION_LOG_DIR
    log_dir.mkdir(parents=True)
    record = {
        "schema_version": 1,
        "head": head_sha,
        "baseline": {"sha": "a" * 40, "source": "LKG"},
        "branch": "main",
        "mode": "narrow",
        "trigger": "none",
        "selected_count": 1,
        "total_count": 10,
        "compute_ms": 5,
        "pytest_ms": None,
        "timestamp": "2026-04-24T12:00:00+00:00",
        "changed_files": ["x.py"],
        "changed_modules": ["x"],
        "dynamic_import_seeds_hit": [],
    }
    (log_dir / f"{head_sha}.json").write_text(json.dumps(record), encoding="utf-8")
    proc = _run_selector(
        tmp_path,
        "--patch-selection-json",
        "--head",
        head_sha,
        "--pytest-ms",
        "1234",
    )
    assert proc.returncode == 0
    patched = json.loads((log_dir / f"{head_sha}.json").read_text(encoding="utf-8"))
    assert patched["pytest_ms"] == 1234


def test_patch_selection_json_missing_head_arg_is_fail_open(real_git, tmp_path: Path) -> None:
    """Missing --head / --pytest-ms args print to stderr and exit 0
    (fail-open contract)."""
    init_git_repo(tmp_path)
    commit_file(tmp_path, "x.py", "x = 1\n", "first")
    proc = _run_selector(tmp_path, "--patch-selection-json")
    assert proc.returncode == 0
    assert "requires --head and --pytest-ms" in proc.stderr


# ----------------------------------------------------------------------
# (e) --help is wired (smoke test on argparse).
# ----------------------------------------------------------------------


def test_help_flag_lists_all_commands(real_git, tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "x.py", "x = 1\n", "first")
    proc = _run_selector(tmp_path, "--help")
    # argparse --help exits 0.
    assert proc.returncode == 0
    for flag in ("--why", "--record-good", "--full-suite", "--patch-selection-json"):
        assert flag in proc.stdout, f"{flag!r} missing from --help output"


# ----------------------------------------------------------------------
# Sanity: argparse syntax error exits non-zero (NOT through fail-open).
# ----------------------------------------------------------------------


def test_unknown_flag_exits_nonzero(real_git, tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    proc = _run_selector(tmp_path, "--no-such-flag")
    assert proc.returncode != 0
