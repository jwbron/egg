"""Git, filesystem, and LKG-sidecar I/O helpers.

This submodule bundles the side-effecting layer the rest of the
selector talks to: shelling out to ``git``, atomic file writes, the
read-only-role gate, the per-branch LKG sidecar, baseline resolution,
and the ``--record-good`` validation/write path.

Tests pin these helpers via ``monkeypatch.setattr(selector._io,
"_run_git", ...)`` (e.g. the ``real_git`` fixture in
``tests/tools/conftest.py``).  Internal callers of ``_run_git`` inside
this module reference the function by bare name, which Python resolves
through the module's own namespace at call time — so the patch reaches
every callsite without per-callsite indirection.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from ._constants import (
    _SHA_HEX_RE,
    SIDECAR_DIR,
    STDERR_DETACHED_HEAD_NOTICE,
    STDERR_DETACHED_HEAD_RECORD_NOTICE,
    STDERR_READONLY_RECORD_NOTICE,
)

# ----------------------------------------------------------------------
# Tiny helpers
# ----------------------------------------------------------------------


def _log(msg: str) -> None:
    """Write a stderr line — kept centralised so tests can monkeypatch."""
    import sys

    print(msg, file=sys.stderr)


def _run_git(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run `git <args>`; return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _is_valid_sha(s: str) -> bool:
    return bool(_SHA_HEX_RE.match(s.strip()))


def _git_object_exists(sha: str, cwd: Path | None = None) -> bool:
    rc, _, _ = _run_git(["cat-file", "-e", sha], cwd=cwd)
    return rc == 0


def _git_is_ancestor(sha: str, descendant: str = "HEAD", cwd: Path | None = None) -> bool:
    rc, _, _ = _run_git(["merge-base", "--is-ancestor", sha, descendant], cwd=cwd)
    return rc == 0


def _git_current_branch(cwd: Path | None = None) -> str | None:
    """Return the current branch name, or None on detached HEAD.

    Uses `git rev-parse --abbrev-ref HEAD` (not `git symbolic-ref`)
    because the egg gateway sidecar blocks `symbolic-ref` on agent
    sandboxes (allowlist enforcement).  `rev-parse --abbrev-ref HEAD`
    returns the literal string `HEAD` on detached HEAD, which we
    canonicalise to None.
    """
    rc, stdout, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if rc != 0:
        return None
    name = stdout.strip()
    if not name or name == "HEAD":
        return None
    return name


def _git_repo_root(cwd: Path | None = None) -> Path:
    rc, stdout, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if rc != 0:
        return Path.cwd()
    return Path(stdout.strip())


# ----------------------------------------------------------------------
# Read-only role detection (Q13 / R14)
# ----------------------------------------------------------------------


def is_role_readonly(repo_root: Path | None = None) -> bool:
    """Return True iff the current sandbox is in a read-only role.

    Two signals (either fires the read-only path):
      - EGG_AGENT_ROLE env var starts with `reviewer_` or equals `refiner`.
      - `.egg-readonly` marker file present in the repo root (sandbox
        primitive — catches read-only sandboxes launched without the
        env var set, per risk_analyst R14).

    When EGG_AGENT_ROLE is unset or names a writer role (coder, tester,
    documenter, planner, anything else), and no marker is present,
    returns False — the LKG-preferred path applies.
    """
    role = os.environ.get("EGG_AGENT_ROLE", "")
    if role.startswith("reviewer_") or role == "refiner":
        return True
    root = repo_root if repo_root is not None else _git_repo_root()
    if (root / ".egg-readonly").exists():
        return True
    return False


# ----------------------------------------------------------------------
# Atomic file I/O
# ----------------------------------------------------------------------


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically via tempfile + os.replace.

    A concurrent reader will either see the previous content or the new
    content, never a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        # On any error, clean up the tempfile so we don't litter
        # `.tmp` artifacts in the sidecar dir.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# ----------------------------------------------------------------------
# Sidecar LKG I/O (TASK-2-4a)
# ----------------------------------------------------------------------


def _resolve_root(repo_root: Path | None) -> Path:
    """Default repo_root to the resolved git toplevel when callers
    don't pass one explicitly.  Centralised so every sidecar I/O
    call site goes through the same fallback (tester blocking #2)."""
    return repo_root if repo_root is not None else _git_repo_root()


def _sidecar_path(branch: str, repo_root: Path | None = None) -> Path:
    return _resolve_root(repo_root) / SIDECAR_DIR / f"{branch}.sha"


def read_sidecar_lkg(branch: str | None, repo_root: Path | None = None) -> str | None:
    """Read the LKG sidecar for `branch`; return the sha or None.

    Returns None if:
      - branch is None (detached HEAD)
      - sidecar file is missing
      - sidecar contents fail the 40-hex regex (treated as absent;
        matches "no LKG" semantics).

    `repo_root` defaults to the git toplevel — passing None when the
    caller is running from a non-repo-root CWD would previously
    write/read under the wrong directory (tester blocking #2).
    """
    if branch is None:
        return None
    path = _sidecar_path(branch, repo_root)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not _is_valid_sha(content):
        return None
    return content


def write_sidecar_lkg(branch: str, sha: str, repo_root: Path | None = None) -> None:
    """Atomically write `sha` to the LKG sidecar for `branch`."""
    _atomic_write_text(_sidecar_path(branch, repo_root), sha + "\n")


# ----------------------------------------------------------------------
# --record-good implementation (TASK-2-4a)
# ----------------------------------------------------------------------


class RecordGoodValidationError(Exception):
    """Raised when --record-good cannot proceed.  The CLI converts to exit !=0."""


def record_good(sha_arg: str | None, repo_root: Path | None = None) -> int:
    """Implement `--record-good [--sha <sha>]`.

    Returns 0 on success or skip-with-notice (detached HEAD, read-only
    role, missing branch).  Raises RecordGoodValidationError on a typo'd
    sha (regex/cat-file/ancestor failure); the caller converts that to
    exit 1.
    """
    if is_role_readonly(repo_root):
        _log(STDERR_READONLY_RECORD_NOTICE)
        return 0

    branch = _git_current_branch(cwd=repo_root)
    if branch is None:
        _log(STDERR_DETACHED_HEAD_RECORD_NOTICE)
        return 0

    # Determine the sha being recorded.  When --sha is omitted, default
    # to HEAD — but resolve HEAD to its 40-char form so the sidecar
    # contents are always normalised.
    if sha_arg is None:
        rc, stdout, _ = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
        if rc != 0:
            raise RecordGoodValidationError("could not resolve HEAD to a sha")
        sha = stdout.strip()
    else:
        sha = sha_arg.strip()

    # (a) regex
    if not _is_valid_sha(sha):
        raise RecordGoodValidationError(f"sha is not 40 lowercase hex chars: {sha!r}")
    # (b) object exists
    if not _git_object_exists(sha, cwd=repo_root):
        raise RecordGoodValidationError(f"sha {sha} not found in object database")
    # (c) ancestor of HEAD
    if not _git_is_ancestor(sha, "HEAD", cwd=repo_root):
        raise RecordGoodValidationError(f"sha {sha} is not an ancestor of HEAD")

    write_sidecar_lkg(branch, sha, repo_root=repo_root)
    return 0


# ----------------------------------------------------------------------
# Baseline resolution + diff (TASK-2-2)
# ----------------------------------------------------------------------


def resolve_baseline(
    repo_root: Path | None = None,
    base_branch: str | None = None,
) -> tuple[str | None, str, str | None]:
    """Resolve the diff baseline.

    Returns (baseline_sha, source, branch):
      - baseline_sha: 40-char sha or None when unresolvable.
      - source: one of "LKG", "BASE_BRANCH", "UNRESOLVABLE".
      - branch: current branch name, or None on detached HEAD.

    Resolution order (Q13 / R14):
      1. If read-only role (EGG_AGENT_ROLE starts with reviewer_, equals
         refiner, or `.egg-readonly` marker present) → SKIP sidecar
         entirely; proceed to base-branch.
      2. Else: try `.egg-state/last-known-good/<branch>.sha`; accept
         only if 40-hex AND ancestor-of-HEAD.
      3. Else: `git merge-base HEAD origin/<base_branch>` (default
         BASE_BRANCH env var → fallback to "main").
      4. Else: UNRESOLVABLE → caller widens to full suite.
    """
    branch = _git_current_branch(cwd=repo_root)
    if branch is None:
        _log(STDERR_DETACHED_HEAD_NOTICE)

    readonly = is_role_readonly(repo_root)

    # (1) LKG sidecar — skipped on read-only role.
    if not readonly and branch is not None:
        sidecar_sha = read_sidecar_lkg(branch, repo_root=repo_root)
        if sidecar_sha is not None and _git_is_ancestor(sidecar_sha, "HEAD", cwd=repo_root):
            return sidecar_sha, "LKG", branch
        # Sidecar exists but fails ancestry — caller will surface this
        # as the "LKG not ancestor of HEAD" trigger via diff-side logic;
        # we fall through here so the trigger comes from the same place
        # as any other "use base branch" path.

    # (2) Base branch.
    base = base_branch or os.environ.get("BASE_BRANCH", "main")
    rc, stdout, _ = _run_git(["merge-base", "HEAD", f"origin/{base}"], cwd=repo_root)
    if rc != 0:
        return None, "UNRESOLVABLE", branch
    base_sha = stdout.strip()
    if not _is_valid_sha(base_sha):
        return None, "UNRESOLVABLE", branch
    return base_sha, "BASE_BRANCH", branch


def lkg_is_stale(repo_root: Path | None = None) -> bool:
    """Return True iff the sidecar exists for the current branch but
    its sha is NOT an ancestor of HEAD (force-push / reset case)."""
    if is_role_readonly(repo_root):
        return False
    branch = _git_current_branch(cwd=repo_root)
    if branch is None:
        return False
    sidecar_sha = read_sidecar_lkg(branch, repo_root=repo_root)
    if sidecar_sha is None:
        return False
    return not _git_is_ancestor(sidecar_sha, "HEAD", cwd=repo_root)


def changed_files(baseline_sha: str, repo_root: Path | None = None) -> list[str]:
    """Return the union of committed-since-baseline + uncommitted files.

    Uncommitted changes ALWAYS participate; a dirty tree cannot have a
    clean LKG effect.  Paths are repo-relative POSIX strings.
    """
    diff_paths: set[str] = set()

    rc, stdout, _ = _run_git(["diff", "--name-only", f"{baseline_sha}...HEAD"], cwd=repo_root)
    if rc == 0:
        for line in stdout.splitlines():
            line = line.strip()
            if line:
                diff_paths.add(line)

    rc, stdout, _ = _run_git(["status", "--porcelain"], cwd=repo_root)
    if rc == 0:
        for line in stdout.splitlines():
            # `git status --porcelain` lines are `XY <path>` where XY
            # is a 2-char status code followed by a space and the path.
            # Renames look like `R  old -> new`; we want both.
            if len(line) < 4:
                continue
            payload = line[3:]
            if " -> " in payload:
                old, new = payload.split(" -> ", 1)
                diff_paths.add(old.strip())
                diff_paths.add(new.strip())
            else:
                diff_paths.add(payload.strip())

    return sorted(diff_paths)


# ----------------------------------------------------------------------
# Path → grimp module resolution (TASK-2-3 helper)
# ----------------------------------------------------------------------


def path_to_module(path: str) -> str | None:
    """Resolve a repo-relative path to a grimp-compatible module id.

    Returns None when the path cannot be mapped (caller treats as a
    fallback trigger).
    """
    if not path.endswith(".py"):
        return None
    p = Path(path)
    # Drop leading "./" if present.
    parts = p.with_suffix("").parts
    if not parts:
        return None
    # Special case: `__init__.py` — module is the parent package.
    if parts[-1] == "__init__":
        parts = parts[:-1]
        if not parts:
            return None
    # gateway/foo.py → "gateway.foo"
    # shared/egg_config/bar.py → "shared.egg_config.bar"
    # tests/test_x.py → "tests.test_x"
    # orchestrator/tests/conftest.py → "orchestrator.tests.conftest"
    return ".".join(parts)


__all__ = (
    "RecordGoodValidationError",
    "_atomic_write_text",
    "_git_current_branch",
    "_git_is_ancestor",
    "_git_object_exists",
    "_git_repo_root",
    "_is_valid_sha",
    "_log",
    "_resolve_root",
    "_run_git",
    "_sidecar_path",
    "changed_files",
    "is_role_readonly",
    "lkg_is_stale",
    "path_to_module",
    "read_sidecar_lkg",
    "record_good",
    "resolve_baseline",
    "write_sidecar_lkg",
)
