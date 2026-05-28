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
