"""Operator-facing salvage of unpushed agent commits (#2429).

When an agent's pushes to its assigned branch are wedged (gateway
rejection from a wrong-branch spawn-time env var, transient infra
failure, restart-reconciliation marking a still-running pipeline
``failed``, etc.), the orchestrator's per-agent worktree at

    /home/egg/.egg-worktrees/{worktree_id}/{repo_short}

still holds the work on its local ``egg/{worktree_id}/work`` branch.
Without a recovery path, ``cleanup_pipeline`` later deletes that
worktree and the work is silently lost.

This module exposes:

* :func:`enumerate_agent_worktrees` — list per-agent worktrees that
  exist on disk for a pipeline (pipeline-level + per-role +
  slice-scoped). Pure filesystem inspection — does not consult the
  pipeline state file, so it works even when state has been
  truncated mid-cleanup.
* :func:`list_unpushed_commits` — for a single worktree, return the
  commits on its local work branch that are not reachable from
  ``origin/<assigned_branch>`` (or ``origin/<base_branch>`` as a
  fallback when the assigned-branch tracking ref is absent).
* :func:`salvage_worktree` — push the worktree's HEAD to a recovery
  ref under ``egg/recovered/<pipeline>/<scope>/<short_sha>`` via
  the gateway's launcher-auth path. The recovery ref name embeds
  the HEAD SHA so re-salvages don't force-overwrite earlier ones.
* :func:`auto_salvage_pipeline` — best-effort salvage of every
  per-agent worktree for a pipeline. Called from
  ``kubernetes_spawner.cleanup_pipeline`` before worktree deletion
  so the default policy on cleanup-with-unpushed-work is no longer
  silent loss.

Recovery refs are the durable record. Operators can locate every
salvaged commit for a pipeline with::

    git ls-remote origin 'refs/heads/egg/recovered/<pipeline>/*'

and replay them onto a recovery branch with ``git fetch`` plus
``git cherry-pick``. A separate periodic sweep
(:mod:`agent_salvage_cleanup`, #2446) deletes recovery refs older
than the configured TTL (default 90 days) so the namespace stays
bounded; see ``docs/reference/agent-recovery.md`` for the operator-
visible knobs.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from egg_logging import get_logger

if TYPE_CHECKING:
    from gateway_client import GatewayClient


logger = get_logger("orchestrator.agent_salvage")


# Mirrors gateway / kubernetes_spawner.WORKTREE_BASE_DIR. Kept local so this
# module has no import-time dependency on the spawner (which pulls in the
# Kubernetes client at import time).
WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")

# Recovery refs land under egg/recovered/. The gateway's egg/-prefix
# branch-ownership check accepts them without per-pipeline allowlisting,
# and operators can locate every salvaged commit with
# ``git ls-remote origin 'refs/heads/egg/recovered/*'``.
RECOVERY_BRANCH_PREFIX = "egg/recovered"

# Regex for slice-scoped per-agent worktree directory names:
# ``{pipeline_id}-slice-{N}-{role}``. Matches the role suffix as a separate
# group so the caller can validate it against the AgentRole allowlist.
_SLICE_WORKTREE_RE = re.compile(r"^(slice-[0-9]+)-(.+)$")

# Message + identity for the synthetic commit that captures a crashed
# agent's uncommitted working-tree state (#2807). Operators grep the
# message prefix to tell a salvaged dirty-tree snapshot apart from the
# agent's own commits. The orchestrator runs git on the agent's worktree
# without a configured user, so an explicit identity is required for the
# commit to succeed.
_UNCOMMITTED_SALVAGE_MESSAGE = "[salvage] pre-crash working-tree state (#2807)"
_SALVAGE_COMMIT_NAME = "egg-salvage"
_SALVAGE_COMMIT_EMAIL = "egg-salvage@localhost"

# Durable base directory for salvaged BRC memory files.
#
# Deliberately NOT under /tmp and NOT inside WORKTREE_BASE_DIR: the whole point
# of the salvage is that a curated memory copy survives BOTH the worktree
# deletion that triggers salvage AND the container restart it is meant to
# recover from. A /tmp path does not persist across a fresh container, and a
# path inside the worktree tree would be deleted alongside the worktrees.
# This sibling of WORKTREE_BASE_DIR lives on the same orchestrator-persistent
# volume the worktrees do, so the copy outlives the restart.
SALVAGE_MEMORY_BASE_DIR = Path("/home/egg/.egg-salvage/brc-memory")

# Per-role agent-output subdirectory, relative to a worktree's repo checkout.
# Mirrors ``sandbox/egg_agent_tools/handlers/brc_memory.py::memory_path_for_role``
# and ``orchestrator/routes/event_prompt.py::_memory_path``: a role's BRC
# memory lives at ``<repo>/.egg-state/agent-outputs/<role>/brc-memory-<pid>.md``.
_AGENT_OUTPUTS_SUBPATH = Path(".egg-state") / "agent-outputs"

# Salvaged memory older than this many seconds is rejected by
# ``validate_salvaged_memory`` as stale — longer than any realistic pipeline
# gap between restart spawns (7 days). Used as the default max age so the
# staleness check is on by default rather than disabled.
_MAX_RESTORE_AGE_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class AgentWorktree:
    """A per-agent (or pipeline-level) worktree visible on disk."""

    worktree_id: str
    """Filesystem name under ``WORKTREE_BASE_DIR`` (e.g.
    ``issue-2261-v9-slice-2-coder``).
    """

    pipeline_id: str
    agent_role: str | None
    """``None`` for the pipeline-level worktree, otherwise an
    ``AgentRole`` value.
    """

    slice_id: str | None
    """``slice-<N>`` for slice-scoped worktrees, ``None`` otherwise."""

    repo_path: Path
    """Path to the worktree's repo checkout
    (``WORKTREE_BASE_DIR / worktree_id / repo_short``)."""

    local_branch: str
    """Local branch name the worktree is checked out to:
    ``egg/{worktree_id}/work``."""

    @property
    def scope_label(self) -> str:
        """Stable label used in recovery-ref paths."""
        if self.agent_role is None:
            return "pipeline"
        if self.slice_id is None:
            return self.agent_role
        return f"{self.slice_id}-{self.agent_role}"


@dataclass(frozen=True)
class UnpushedCommit:
    sha: str
    summary: str
    author: str
    authored_at: str  # ISO-8601 with timezone (``%aI``)
    files_changed: int


@dataclass(frozen=True)
class WorktreeCommitReport:
    worktree: AgentWorktree
    assigned_branch: str | None
    """Remote branch the worktree was configured to push to (from
    ``branch.<local>.merge``), or ``None`` when no upstream was
    configured.
    """

    anchor_ref: str | None
    """Remote-tracking ref used as the ``^anchor`` cut for ``git log`` —
    typically ``origin/<assigned_branch>``, falling back to
    ``origin/<base_branch>``. ``None`` when neither tracking ref
    exists; in that case ``commits`` is the full HEAD history (capped
    at 200) and the report is best-effort.
    """

    commits: list[UnpushedCommit]
    error: str | None = None
    """Set when worktree git inspection failed (corrupt worktree,
    permission error, etc.). ``commits`` is empty in that case.
    """


@dataclass(frozen=True)
class SalvageResult:
    worktree_id: str
    agent_role: str | None
    slice_id: str | None
    recovery_ref: str | None  # set on success
    head_sha: str | None
    n_commits: int
    ok: bool
    error: str | None = None


# Lazy-resolved AgentRole values. Imported at call time to avoid hard-wiring
# the orchestrator package layout into a module that may run standalone in
# unit tests.
_VALID_ROLE_VALUES: frozenset[str] | None = None


def _role_values() -> frozenset[str]:
    global _VALID_ROLE_VALUES
    if _VALID_ROLE_VALUES is None:
        try:
            from egg_contracts.agent_roles import AgentRole  # type: ignore
        except ImportError:
            from shared.egg_contracts.agent_roles import AgentRole  # type: ignore
        _VALID_ROLE_VALUES = frozenset(role.value for role in AgentRole)
    return _VALID_ROLE_VALUES


# Maximum number of commits to enumerate per worktree. Operators care about
# "is there real work here?", not the full commit graph; capping keeps the
# response bounded for huge histories.
_MAX_COMMITS_PER_WORKTREE = 200


def _run_git(
    *args: str,
    cwd: Path,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``cwd`` with hooks disabled.

    ``core.hooksPath=/dev/null`` mirrors ``StateStore._run_git`` — the
    orchestrator runs git on agent-controlled worktrees and must never
    execute their hooks.
    """
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _is_git_worktree(repo_path: Path) -> bool:
    """Return True when ``repo_path`` looks like a git worktree.

    Checked-out worktrees carry a ``.git`` *file* (not a directory) that
    points at ``.git/worktrees/<name>`` in the parent repo. Bare clones
    have a ``.git`` directory. Either form is acceptable for a worktree
    that holds real commits — accept both.
    """
    git_marker = repo_path / ".git"
    return git_marker.exists()


def enumerate_agent_worktrees(
    pipeline_id: str,
    *,
    validate_git: bool = True,
) -> list[AgentWorktree]:
    """List per-agent worktrees on disk for ``pipeline_id``.

    Scans ``WORKTREE_BASE_DIR`` for directories that match the
    pipeline-level (``{pipeline_id}``), per-role
    (``{pipeline_id}-{role}``), or slice-scoped
    (``{pipeline_id}-slice-{N}-{role}``) shapes. Same parsing rules as
    :meth:`KubernetesSpawner.cleanup_pipeline` so the salvage hook
    enumerates exactly the worktrees the cleanup loop is about to
    delete.

    With ``validate_git=True`` (the default, used by salvage callers),
    each returned ``AgentWorktree.repo_path`` points at the first
    sub-directory inside the worktree that has a ``.git`` marker.
    Worktrees with no usable repo checkout are skipped — they have
    nothing to salvage.

    With ``validate_git=False`` (cleanup callers), worktrees with
    missing or unreadable ``.git`` markers are still returned, with
    ``repo_path`` falling back to the worktree directory itself. This
    is required so that broken/corrupted worktrees (e.g. wedged btrfs
    mounts) — exactly the failure class #1723 set out to clean up —
    still reach ``gateway.delete_worktrees`` rather than being silently
    skipped by a salvage-style ``.git`` gate. Such entries are not
    salvageable, but they must still be deletable.
    """
    if not WORKTREE_BASE_DIR.exists():
        return []

    valid_roles = _role_values()
    valid_role_suffixes = {f"-{role}" for role in valid_roles}
    found: list[AgentWorktree] = []

    try:
        entries = sorted(WORKTREE_BASE_DIR.iterdir())
    except OSError as e:
        logger.warning(
            "Salvage enumeration failed: cannot iterate worktree base",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return []

    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name

        agent_role: str | None
        slice_id: str | None

        if name == pipeline_id:
            agent_role = None
            slice_id = None
        elif name.startswith(pipeline_id):
            suffix = name[len(pipeline_id) :]
            if suffix in valid_role_suffixes:
                agent_role = suffix.lstrip("-")
                slice_id = None
            else:
                # Slice-scoped: suffix is "-slice-N-{role}". Strip the
                # leading hyphen, then match.
                if not suffix.startswith("-"):
                    continue
                m = _SLICE_WORKTREE_RE.match(suffix[1:])
                if m is None:
                    continue
                role_value = m.group(2)
                if role_value not in valid_roles:
                    continue
                slice_id = m.group(1)
                agent_role = role_value
        else:
            continue

        repo_path = _resolve_repo_path(entry)
        if repo_path is None:
            if validate_git:
                continue
            repo_path = entry

        worktree = AgentWorktree(
            worktree_id=name,
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            repo_path=repo_path,
            local_branch=f"egg/{name}/work",
        )
        found.append(worktree)

    return found


def _resolve_repo_path(worktree_dir: Path) -> Path | None:
    """Pick the repo subdirectory inside a worktree.

    A worktree dir contains a per-repo subdirectory:
    ``{worktree_dir}/{repo_short}/.git``. Return the first match. If the
    worktree itself is the checkout (rare but seen in tests) accept it
    too.
    """
    if _is_git_worktree(worktree_dir):
        return worktree_dir
    try:
        for sub in sorted(worktree_dir.iterdir()):
            if sub.is_dir() and _is_git_worktree(sub):
                return sub
    except OSError:
        return None
    return None


def _read_assigned_branch(repo_path: Path, local_branch: str) -> str | None:
    """Read the remote branch the worktree was configured to push to.

    The gateway sets ``branch.<local>.merge`` at worktree-create time
    (``GatewayClient.create_worktrees(... assigned_branch=...)``) so a
    naive ``git push`` from the agent resolves to a refspec targeting
    the assigned remote branch. Reading it back gives us the right
    ``^origin/...`` cut without consulting the pipeline state file.
    """
    try:
        result = _run_git(
            "config",
            "--get",
            f"branch.{local_branch}.merge",
            cwd=repo_path,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    raw = (result.stdout or "").strip()
    if result.returncode != 0 or not raw:
        return None
    if raw.startswith("refs/heads/"):
        return raw[len("refs/heads/") :]
    return raw


def _ref_exists(repo_path: Path, ref: str) -> bool:
    try:
        result = _run_git(
            "rev-parse",
            "--verify",
            "--quiet",
            ref,
            cwd=repo_path,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0


def _resolve_anchor(
    repo_path: Path,
    assigned_branch: str | None,
    base_branch: str | None,
) -> str | None:
    """Pick the ``^anchor`` ref for the unpushed-commits diff.

    Order: ``origin/<assigned_branch>`` → ``origin/<base_branch>`` →
    ``None`` (HEAD-only fallback, capped). Skipping fetch is intentional:
    accuracy in salvage means "don't trim too much" — over-including
    already-pushed commits is harmless (the receiver dedupes when
    cherry-picking) but trimming a real commit is silent loss.
    """
    if assigned_branch:
        ref = f"refs/remotes/origin/{assigned_branch}"
        if _ref_exists(repo_path, ref):
            return ref
    if base_branch:
        ref = f"refs/remotes/origin/{base_branch}"
        if _ref_exists(repo_path, ref):
            return ref
    return None


def list_unpushed_commits(
    worktree: AgentWorktree,
    base_branch: str | None = None,
) -> WorktreeCommitReport:
    """Enumerate commits on the worktree's local branch not in the anchor.

    Returns a ``WorktreeCommitReport`` whose ``commits`` is the
    chronological-newest-first list of up to
    ``_MAX_COMMITS_PER_WORKTREE`` commits. ``error`` is set when the
    worktree is unreadable; in that case ``commits`` is empty and the
    caller should skip salvage.
    """
    if not _is_git_worktree(worktree.repo_path):
        return WorktreeCommitReport(
            worktree=worktree,
            assigned_branch=None,
            anchor_ref=None,
            commits=[],
            error="worktree has no .git marker",
        )

    if not _ref_exists(worktree.repo_path, worktree.local_branch):
        # The work branch was never created (agent never committed) — not
        # an error, just nothing to salvage.
        return WorktreeCommitReport(
            worktree=worktree,
            assigned_branch=None,
            anchor_ref=None,
            commits=[],
        )

    assigned_branch = _read_assigned_branch(worktree.repo_path, worktree.local_branch)
    anchor_ref = _resolve_anchor(worktree.repo_path, assigned_branch, base_branch)

    log_args: list[str] = [
        "log",
        f"--max-count={_MAX_COMMITS_PER_WORKTREE}",
        # %x1f is the ASCII unit separator (U+001F). Using it instead of
        # a tab keeps the parser correct when the commit subject itself
        # contains a tab — git would otherwise pass the literal tab
        # through and shift the trailing fields on str.split.
        "--format=%H%x1f%s%x1f%an%x1f%aI",
        "--shortstat",
        worktree.local_branch,
    ]
    if anchor_ref is not None:
        log_args.append(f"^{anchor_ref}")

    try:
        result = _run_git(*log_args, cwd=worktree.repo_path, check=True)
    except subprocess.CalledProcessError as e:
        return WorktreeCommitReport(
            worktree=worktree,
            assigned_branch=assigned_branch,
            anchor_ref=anchor_ref,
            commits=[],
            error=(e.stderr or str(e)).strip(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return WorktreeCommitReport(
            worktree=worktree,
            assigned_branch=assigned_branch,
            anchor_ref=anchor_ref,
            commits=[],
            error=str(e),
        )

    commits = _parse_git_log(result.stdout)
    return WorktreeCommitReport(
        worktree=worktree,
        assigned_branch=assigned_branch,
        anchor_ref=anchor_ref,
        commits=commits,
    )


_SHORTSTAT_FILES_RE = re.compile(r"(\d+)\s+files?\s+changed")


_UNIT_SEP = "\x1f"


def _parse_git_log(output: str) -> list[UnpushedCommit]:
    """Parse ``git log --format=... --shortstat`` output.

    Each commit looks like::

        <sha>\x1f<summary>\x1f<author>\x1f<authored_at>
        <blank>
         3 files changed, 12 insertions(+), 4 deletions(-)
        <blank>

    Empty lines separate commits. Shortstat may be absent (merge with no
    diff, empty commit). We skip blank lines, parse the unit-separated
    line as the commit header, and accumulate any following line
    without a unit separator as the shortstat for that commit.
    """
    commits: list[UnpushedCommit] = []
    pending: list[str] | None = None
    files_changed = 0

    def _flush() -> None:
        nonlocal pending, files_changed
        if pending is None:
            return
        sha, summary, author, authored_at = pending[0], pending[1], pending[2], pending[3]
        commits.append(
            UnpushedCommit(
                sha=sha,
                summary=summary,
                author=author,
                authored_at=authored_at,
                files_changed=files_changed,
            )
        )
        pending = None
        files_changed = 0

    for raw in output.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if _UNIT_SEP in line:
            _flush()
            parts = line.split(_UNIT_SEP, 3)
            if len(parts) == 4:
                pending = parts
                files_changed = 0
            continue
        m = _SHORTSTAT_FILES_RE.search(line)
        if m and pending is not None:
            try:
                files_changed = int(m.group(1))
            except ValueError:
                files_changed = 0

    _flush()
    return commits


def _recovery_ref(
    pipeline_id: str,
    scope_label: str,
    head_sha: str,
) -> str:
    """Compose the recovery ref name.

    ``egg/recovered/<pipeline_id>/<scope>/<short_sha>``. The short SHA
    makes each salvage immutable: re-salvaging the same HEAD is a no-op
    fast-forward, and a re-run after new commits gets a fresh ref
    instead of force-overwriting the prior one.
    """
    short = head_sha[:12]
    return f"{RECOVERY_BRANCH_PREFIX}/{pipeline_id}/{scope_label}/{short}"


def _has_uncommitted_changes(repo_path: Path) -> bool:
    """Return True when the worktree has staged, unstaged, or untracked changes."""
    try:
        result = _run_git("status", "--porcelain", cwd=repo_path, check=False)
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def commit_working_tree(worktree: AgentWorktree) -> str | None:
    """Commit a crashed agent's uncommitted working-tree state (#2807).

    The coder commits at consensus-propose time, not per task, so a crash
    in the ``Edit``→``git commit`` window leaves valuable work only in the
    dirty working tree. The respawn path reuses the on-disk worktree and
    hard-resets it to a remote ref (gateway
    ``_reset_reused_worktree_to_safe_ref``), which would discard that
    state. Staging and committing it onto the local work branch *before*
    salvage lets the recovery-ref push capture it.

    Best-effort: returns the new commit SHA on success, ``None`` when
    there is nothing to commit or the commit fails. Never raises — a
    failure here must not stop the committed-but-unpushed salvage that
    follows.
    """
    if not _is_git_worktree(worktree.repo_path):
        return None
    if not _has_uncommitted_changes(worktree.repo_path):
        return None
    try:
        add = _run_git("add", "-A", cwd=worktree.repo_path, check=False)
        if add.returncode != 0:
            logger.warning(
                "Salvage: git add -A failed; skipping uncommitted capture",
                worktree_id=worktree.worktree_id,
                stderr=(add.stderr or "").strip(),
            )
            return None
        commit = _run_git(
            "-c",
            f"user.name={_SALVAGE_COMMIT_NAME}",
            "-c",
            f"user.email={_SALVAGE_COMMIT_EMAIL}",
            "commit",
            "-m",
            _UNCOMMITTED_SALVAGE_MESSAGE,
            cwd=worktree.repo_path,
            check=False,
        )
        if commit.returncode != 0:
            logger.warning(
                "Salvage: commit of uncommitted working tree failed",
                worktree_id=worktree.worktree_id,
                stderr=(commit.stderr or "").strip(),
            )
            return None
        head = _run_git("rev-parse", "HEAD", cwd=worktree.repo_path, check=False)
        head_sha = (head.stdout or "").strip() if head.returncode == 0 else None
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(
            "Salvage: capturing uncommitted working tree raised; continuing",
            worktree_id=worktree.worktree_id,
            error=str(e),
        )
        return None

    logger.info(
        "Salvage: committed uncommitted working-tree state before recovery push",
        pipeline_id=worktree.pipeline_id,
        worktree_id=worktree.worktree_id,
        agent_role=worktree.agent_role,
        head_sha=head_sha,
    )
    return head_sha


def salvage_worktree(
    gateway: GatewayClient,
    worktree: AgentWorktree,
    *,
    base_branch: str | None = None,
    mode: str = "public",
    salvage_uncommitted: bool = False,
) -> SalvageResult:
    """Push ``worktree``'s HEAD to a recovery ref, if it has unpushed work.

    Returns a ``SalvageResult`` with ``ok=True`` when a push succeeded or
    when there was nothing to salvage (``n_commits=0``). ``ok=False``
    only on push failure or worktree corruption.

    When ``salvage_uncommitted`` is set, the worktree's dirty state is
    committed onto the local work branch (via :func:`commit_working_tree`)
    *before* enumeration, so the recovery push also captures uncommitted
    edits — the #2807 crash window where a respawn's ``git reset --hard``
    would otherwise destroy them. Callers on the cleanup path leave it
    ``False`` to avoid turning routine untracked build artifacts into
    recovery refs.
    """
    if salvage_uncommitted:
        commit_working_tree(worktree)
    report = list_unpushed_commits(worktree, base_branch=base_branch)
    if report.error:
        return SalvageResult(
            worktree_id=worktree.worktree_id,
            agent_role=worktree.agent_role,
            slice_id=worktree.slice_id,
            recovery_ref=None,
            head_sha=None,
            n_commits=0,
            ok=False,
            error=report.error,
        )

    if not report.commits:
        # Nothing to salvage. ok=True so callers can short-circuit
        # without surfacing a spurious failure.
        return SalvageResult(
            worktree_id=worktree.worktree_id,
            agent_role=worktree.agent_role,
            slice_id=worktree.slice_id,
            recovery_ref=None,
            head_sha=None,
            n_commits=0,
            ok=True,
        )

    head_sha = report.commits[0].sha
    target_ref = _recovery_ref(worktree.pipeline_id, worktree.scope_label, head_sha)

    # ``ref=local_branch`` would push that named ref; we want the
    # worktree's HEAD (which equals the local branch tip), so
    # ``ref=None`` is the right call. ``branch=target_ref`` becomes the
    # remote ref name. Launcher auth bypasses the agent-targeted
    # pipeline-push enforcement.
    try:
        push_result = gateway.push_worktree_branch(
            pipeline_id=worktree.pipeline_id,
            repo_path=str(worktree.repo_path),
            branch=target_ref,
            mode=mode,  # type: ignore[arg-type]
            ref=None,
            force=False,
        )
    except Exception as e:
        # GatewayError or transport failure
        return SalvageResult(
            worktree_id=worktree.worktree_id,
            agent_role=worktree.agent_role,
            slice_id=worktree.slice_id,
            recovery_ref=None,
            head_sha=head_sha,
            n_commits=len(report.commits),
            ok=False,
            error=str(e),
        )

    if not push_result.ok:
        return SalvageResult(
            worktree_id=worktree.worktree_id,
            agent_role=worktree.agent_role,
            slice_id=worktree.slice_id,
            recovery_ref=None,
            head_sha=head_sha,
            n_commits=len(report.commits),
            ok=False,
            error=push_result.describe(),
        )

    logger.info(
        "Salvaged unpushed commits to recovery ref",
        pipeline_id=worktree.pipeline_id,
        worktree_id=worktree.worktree_id,
        agent_role=worktree.agent_role,
        slice_id=worktree.slice_id,
        recovery_ref=target_ref,
        head_sha=head_sha,
        n_commits=len(report.commits),
    )
    return SalvageResult(
        worktree_id=worktree.worktree_id,
        agent_role=worktree.agent_role,
        slice_id=worktree.slice_id,
        recovery_ref=target_ref,
        head_sha=head_sha,
        n_commits=len(report.commits),
        ok=True,
    )


# ── BRC Memory Salvage / Restore (pt. of #3200 slice-1) ────────────────────
#
# The production API:
#
#   salvage_brc_memory(pipeline_id, agent_outputs, salvage_base)
#       Copies per-role ``brc-memory-<pipeline_id>.md`` files from
#       agent-output dirs into a pipeline-scoped salvage directory.
#       Returns ``list[SalvageMemoryResult]`` — one per role.
#
#   validate_salvaged_memory(pipeline_id, mem_file, *, max_age_seconds)
#       Self-contained validation. Returns ``(ok: bool, reason: str)``.
#       Run at the restore boundary — the salvage file is validated before
#       its contents are placed into the freshly created worktree.
#
#   restore_salvaged_memory_to_worktree(pipeline_id, role, repo_path, ...)
#       The production restore path: validates a salvaged file and writes it
#       into a freshly created worktree (called from
#       ``KubernetesSpawner.spawn_agent_job``), consuming the salvage after.
#
# Salvage destination defaults to SALVAGE_MEMORY_BASE_DIR (a durable,
# restart-surviving volume); ``auto_salvage_pipeline`` resolves it at call
# time so tests can monkeypatch the module global.


@dataclass
class SalvageMemoryResult:
    """Per-role result of copying one role's BRC memory file to salvage."""

    role: str
    ok: bool
    error: str | None = None
    content: str | None = None


def salvage_brc_memory(
    pipeline_id: str,
    agent_outputs: Path,
    salvage_base: Path,
) -> list[SalvageMemoryResult]:
    """Copy per-role BRC memory files to a pipeline-scoped salvage directory.

    Reads ``brc-memory-{pipeline_id}.md`` from every role subdirectory under
    *agent_outputs* and copies each into
    ``<salvage_base>/<pipeline_id>/<role>/brc-memory-<pipeline_id>.md``.

    *agent_outputs* is the ``.egg-state/agent-outputs`` directory inside a
    real worktree checkout (resolved per-worktree by
    :func:`auto_salvage_pipeline`), not a process-wide constant — the memory
    files only ever exist inside the per-role worktrees that are about to be
    deleted.

    The source filename carries the pipeline-id suffix
    (``brc-memory-<pipeline_id>.md``); the bare ``brc-memory.md`` is a
    documented previous-pipeline leftover that must be ignored (see
    ``docs/architecture/brc-memory.md`` and
    ``memory_path_for_role``), so it is intentionally not matched here.

    Must be called BEFORE worktree deletion so the files survive the cleanup.

    Best-effort per role: a failure for one role does not block copy of the
    others.  Missing agent-output dir is not an error (no roles emitted
    memory yet).
    """
    results: list[SalvageMemoryResult] = []

    if not agent_outputs.is_dir():
        return results

    pattern = f"brc-memory-{pipeline_id}.md"

    try:
        role_dirs = sorted(agent_outputs.iterdir())
    except OSError:
        return results

    for role_dir in role_dirs:
        if not role_dir.is_dir():
            continue
        role = role_dir.name
        src = role_dir / pattern

        # No memory file for this role means it emitted nothing — skip it
        # silently. A file that exists but is unreadable falls through to the
        # read below and is recorded as ok=False.
        if not src.exists():
            continue

        dest_dir = salvage_base / pipeline_id / role
        dest = dest_dir / f"brc-memory-{pipeline_id}.md"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            content = src.read_text()
            dest.write_text(content)
            results.append(
                SalvageMemoryResult(
                    role=role,
                    ok=True,
                    content=content,
                )
            )
        except OSError as e:
            results.append(
                SalvageMemoryResult(
                    role=role,
                    ok=False,
                    error=str(e),
                )
            )

    return results


def validate_salvaged_memory(
    pipeline_id: str,
    mem_file: Path,
    *,
    max_age_seconds: int = _MAX_RESTORE_AGE_SECONDS,
) -> tuple[bool, str]:
    """Validate that a salvaged memory file is safe to restore.

    Checks:
    1. File exists and is a regular file.
    2. Non-empty (zero-byte treated as empty).
    3. Belongs to *pipeline_id* — satisfied when the id appears in the file
       *path* (the salvage layout is
       ``<salvage>/<pipeline_id>/<role>/brc-memory-<pipeline_id>.md``, so the
       path is the authoritative binding) or, failing that, in the content.
       Path-first avoids rejecting a valid memory body that happens not to
       embed the literal id token — the canonical ``BRCMemory`` schema carries
       no guaranteed id in its rendered body (reviewer R3).
    4. File mtime is within *max_age_seconds* of now — rejects stale copies.
       Defaults to :data:`_MAX_RESTORE_AGE_SECONDS` (7 days) so the staleness
       check is on by default; pass ``0`` to disable it.

    Returns ``(True, "")`` on success, ``(False, reason)`` on failure.
    Consumers call :func:`restore_salvaged_memory` to get the validated
    content.
    """
    if not mem_file.is_file():
        return False, f"Memory file not found: {mem_file}"

    try:
        content = mem_file.read_text()
    except OSError as e:
        return False, f"Cannot read memory file: {e}"

    if len(content.strip()) == 0:
        return False, "Memory file is empty"

    # Path-first binding (see docstring). On both production callers the path
    # is ``<salvage>/<pid>/<role>/brc-memory-<pid>.md``, so the path check is
    # always true and the content fallback never runs — in practice this is a
    # path-shape assertion, not cross-pipeline content protection. The content
    # fallback only matters for callers that pass a non-canonical path.
    if pipeline_id not in str(mem_file) and pipeline_id not in content:
        return False, (f"Memory file does not reference pipeline {pipeline_id}")

    if max_age_seconds > 0:
        try:
            st = mem_file.stat()
        except OSError as e:
            return False, f"Cannot stat memory file: {e}"
        age = datetime.now(UTC).timestamp() - st.st_mtime
        if age > max_age_seconds:
            return False, f"Memory file is stale (age {age:.0f}s > {max_age_seconds}s)"

    return True, ""


def restore_salvaged_memory_to_worktree(
    pipeline_id: str,
    role: str,
    repo_path: Path,
    *,
    salvage_base: Path = SALVAGE_MEMORY_BASE_DIR,
) -> Path | None:
    """Restore a salvaged BRC memory file into a freshly (re)created worktree.

    This is the production restore path (task-1-1 / task-1-2). It runs
    orchestrator-side right after a worktree is (re)created on a restart —
    see ``KubernetesSpawner.spawn_agent_job``, the single chokepoint all
    spawn paths (initial spawn, agent restart, phase restart) flow through.
    The orchestrator writes the file into the worktree on disk; the agent's
    in-sandbox composer (``orchestrator/routes/event_prompt.py``) then reads
    it from the mounted worktree and seeds the fresh session from it. The
    composer cannot reach the orchestrator-side salvage dir itself, so
    validation has to happen here, at the restore boundary.

    Validity is enforced *before* the file is placed: a corrupt / zero-byte /
    wrong-pipeline / stale salvage is rejected with a logged ``error`` and the
    file is NOT written. The fresh agent then starts with no memory rather
    than degraded memory — refusing to seed from an invalid restore (a loud,
    logged hard error) instead of silently composing enrichment from garbage,
    per task-1-2. A genuinely-absent salvage (nothing was ever salvaged for
    this role) is the common cold-start case and returns ``None`` quietly.

    The restore never overwrites a memory file already present in the worktree:
    a destination that exists is the agent's current committed memory (a
    worktree checked out from ``origin/<branch>`` already carries it), which is
    authoritative and newer than any salvage snapshot. After a successful copy
    the salvage source is consumed (deleted) so a stale snapshot cannot be
    re-applied to a later worktree within the staleness window.

    Returns the restored destination path on success, ``None`` otherwise.
    Best-effort: never raises, so a restore failure cannot block the spawn.
    """
    src = salvage_base / pipeline_id / role / f"brc-memory-{pipeline_id}.md"
    if not src.is_file():
        # Nothing salvaged for this role — the common cold-start case.
        return None

    dest_dir = repo_path / _AGENT_OUTPUTS_SUBPATH / role
    dest = dest_dir / f"brc-memory-{pipeline_id}.md"

    if dest.exists():
        # The worktree already carries this role's memory — committed to the
        # branch and checked out from origin, so it is authoritative and at
        # least as new as the salvage. Never clobber it with an older snapshot.
        logger.info(
            "Worktree already has BRC memory; skipping salvage restore",
            pipeline_id=pipeline_id,
            role=role,
            dest_path=str(dest),
        )
        _consume_salvage(src, pipeline_id, role)
        return None

    ok, reason = validate_salvaged_memory(pipeline_id, src)
    if not ok:
        # Loud, logged refusal — NOT a silent skip. The fresh session starts
        # un-seeded rather than seeded from invalid memory (task-1-2).
        logger.error(
            "Refusing to restore invalid salvaged BRC memory; fresh agent "
            "will start without seed memory",
            pipeline_id=pipeline_id,
            role=role,
            source_path=str(src),
            reason=reason,
        )
        return None

    try:
        content = src.read_text(encoding="utf-8")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    except OSError as e:
        logger.error(
            "Failed to write restored BRC memory into worktree; fresh agent "
            "will start without seed memory",
            pipeline_id=pipeline_id,
            role=role,
            source_path=str(src),
            dest_path=str(dest),
            error=str(e),
        )
        return None

    logger.info(
        "Restored salvaged BRC memory into worktree for fresh agent",
        pipeline_id=pipeline_id,
        role=role,
        source_path=str(src),
        dest_path=str(dest),
    )
    # Consume the salvage so it cannot be re-applied to a later worktree.
    _consume_salvage(src, pipeline_id, role)
    return dest


def _consume_salvage(src: Path, pipeline_id: str, role: str) -> None:
    """Delete a salvage file after it has been restored (or superseded).

    Best-effort: a failure to clean up is logged but never propagated, since
    the restore itself has already succeeded.
    """
    try:
        src.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(
            "Failed to consume salvaged BRC memory after restore",
            pipeline_id=pipeline_id,
            role=role,
            source_path=str(src),
            error=str(e),
        )


def _salvage_memory_for_worktrees(
    pipeline_id: str,
    worktrees: list[AgentWorktree],
    worktree_filter: set[str] | None,
    *,
    salvage_base: Path = SALVAGE_MEMORY_BASE_DIR,
) -> list[SalvageMemoryResult]:
    """Salvage each per-role worktree's BRC memory file before deletion.

    Resolves the real on-disk source — ``<repo>/.egg-state/agent-outputs``
    inside each worktree — and delegates the copy to
    :func:`salvage_brc_memory`. Pipeline-level worktrees (``agent_role is
    None``) hold no role memory and are skipped. Honours ``worktree_filter``
    so only the worktrees about to be deleted are read.

    Returns the flattened per-role results (for logging); best-effort.
    """
    all_results: list[SalvageMemoryResult] = []
    for wt in worktrees:
        if worktree_filter is not None and wt.worktree_id not in worktree_filter:
            continue
        if wt.agent_role is None:
            continue
        agent_outputs = wt.repo_path / _AGENT_OUTPUTS_SUBPATH
        results = salvage_brc_memory(pipeline_id, agent_outputs, salvage_base)
        all_results.extend(results)

    salvaged = [r for r in all_results if r.ok]
    if salvaged:
        logger.info(
            "Salvaged BRC memory before worktree deletion",
            pipeline_id=pipeline_id,
            salvage_base=str(salvage_base),
            roles=[r.role for r in salvaged],
            n_salvaged=len(salvaged),
        )
    failed = [r for r in all_results if not r.ok]
    if failed:
        logger.warning(
            "Some BRC memory files could not be salvaged",
            pipeline_id=pipeline_id,
            roles=[r.role for r in failed],
            errors=[r.error for r in failed],
        )
    return all_results


def auto_salvage_pipeline(
    gateway: GatewayClient,
    pipeline_id: str,
    *,
    base_branch: str | None = None,
    mode: str = "public",
    worktree_filter: set[str] | None = None,
    salvage_uncommitted: bool = False,
) -> list[SalvageResult]:
    """Best-effort salvage of every per-agent worktree for a pipeline.

    Called from ``cleanup_pipeline`` before worktree deletion so any
    unpushed work lands on ``egg/recovered/...`` before the filesystem
    state is gone. Always returns — never raises — so a salvage failure
    cannot block the cleanup loop.

    ``worktree_filter`` (when set) restricts salvage to worktree ids in
    the given set, so the caller can pass exactly the ids it is about
    to delete and skip stale on-disk worktrees from a prior pipeline
    re-use.

    ``salvage_uncommitted`` (set by the restart-recovery caller, #2807)
    commits each worktree's dirty state onto its work branch before the
    recovery push so uncommitted edits survive the respawn's
    ``git reset --hard``. Left ``False`` on the cleanup path.
    """
    results: list[SalvageResult] = []
    try:
        worktrees = enumerate_agent_worktrees(pipeline_id)
    except Exception as e:
        logger.warning(
            "Salvage enumeration failed; cleanup proceeding without salvage",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return results

    # Salvage BRC memory files before worktree deletion (best-effort). The
    # memory files live INSIDE each per-role worktree at
    # ``<repo>/.egg-state/agent-outputs/<role>/brc-memory-<pid>.md`` — the very
    # worktrees this function is about to delete — so we read each worktree's
    # own checkout, not a process-wide path. Failure is logged, never
    # propagated, so it cannot block the unpushed-commit salvage that follows.
    try:
        # Resolve SALVAGE_MEMORY_BASE_DIR from the module global at call time
        # rather than relying on the callee's default-argument binding (which is
        # captured at import time and so is invisible to a test/runtime patch of
        # the module constant).
        _salvage_memory_for_worktrees(
            pipeline_id,
            worktrees,
            worktree_filter,
            salvage_base=SALVAGE_MEMORY_BASE_DIR,
        )
    except Exception as e:
        logger.warning(
            "BRC memory salvage failed; continuing with worktree salvage",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    for wt in worktrees:
        if worktree_filter is not None and wt.worktree_id not in worktree_filter:
            continue
        try:
            result = salvage_worktree(
                gateway,
                wt,
                base_branch=base_branch,
                mode=mode,
                salvage_uncommitted=salvage_uncommitted,
            )
        except Exception as e:
            # salvage_worktree is supposed to catch its own failures, but
            # an unexpected exception here must not block cleanup.
            logger.warning(
                "Salvage raised unexpectedly; continuing cleanup",
                pipeline_id=pipeline_id,
                worktree_id=wt.worktree_id,
                error=str(e),
            )
            results.append(
                SalvageResult(
                    worktree_id=wt.worktree_id,
                    agent_role=wt.agent_role,
                    slice_id=wt.slice_id,
                    recovery_ref=None,
                    head_sha=None,
                    n_commits=0,
                    ok=False,
                    error=str(e),
                )
            )
            continue
        results.append(result)
        if not result.ok:
            logger.warning(
                "Salvage failed for worktree",
                pipeline_id=pipeline_id,
                worktree_id=wt.worktree_id,
                error=result.error,
            )

    salvaged = [r for r in results if r.ok and r.recovery_ref]
    if salvaged:
        logger.info(
            "Auto-salvage complete",
            pipeline_id=pipeline_id,
            n_worktrees_inspected=len(results),
            n_salvaged=len(salvaged),
            recovery_refs=[r.recovery_ref for r in salvaged],
        )
    return results
