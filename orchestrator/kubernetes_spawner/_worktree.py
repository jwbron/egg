"""Worktree validation / reuse / cleanup (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import kubernetes_spawner as _pkg
from agent_salvage import _SALVAGE_COMMIT_EMAIL, _SALVAGE_COMMIT_NAME
from kubernetes_spawner import (
    logger,
)
from models import AgentRole


def _validate_worktree_for_reuse(
    agent_worktree_id: str,
    repos: list[str],
    branch: str | None,
) -> dict[str, str] | None:
    """Validate an existing worktree for re-attach.

    Checks the filesystem worktree at ``WORKTREE_BASE_DIR / agent_worktree_id / <repo>``
    for directory existence, ``.git`` integrity (``git rev-parse --git-dir``), lock
    files (``.git/*.lock`` and ``.git/refs/*/*.lock``), and expected branch (when
    ``branch`` is supplied). The gateway materializes every per-agent worktree on
    its own local work branch ``egg/{agent_worktree_id}/work`` (wired to push to
    the assigned branch), so ``HEAD`` is never on the assigned branch itself
    (#3480). The branch check therefore accepts the derived work branch, the
    assigned ``branch``, or a detached ``HEAD``.

    This function performs **validation only** — the caller must also invoke
    :meth:`KubernetesSpawner._clean_reused_worktree` to discard dirty state
    and sync to the role branch tip (R6 dirty-state policy, fast-forward-aware
    per #3506) before the agent runs. The separation lets the test-first contract
    (:meth:`_try_reuse_worktree`) compose validation + cleanup into one call
    while keeping each concern independently testable.

    Returns a ``{owner/repo: filesystem_path}`` dict on success, or ``None`` on ANY
    validation mismatch (the caller falls back to create-with-retry). Best-effort logging.

    The returned paths are ORCHESTRATOR-LOCAL (under ``WORKTREE_BASE_DIR``,
    i.e. this container's own mount of the worktree tree) — suitable for the
    orchestrator's filesystem ops but NOT for a Job spec's ``hostPath``
    mounts. :func:`_try_reuse_worktree` translates them to host paths via
    :func:`_local_to_host_volumes` before handing them to the spawn path
    (#3502).
    """
    import subprocess as _sp

    if not repos or not _pkg.WORKTREE_BASE_DIR.exists():
        return None
    wt = _pkg.WORKTREE_BASE_DIR / agent_worktree_id
    if not wt.exists() or not wt.is_dir():
        logger.info(
            "Worktree re-attach: directory missing",
            agent_worktree_id=agent_worktree_id,
        )
        return None

    vols: dict[str, str] = {}
    for ref in repos:
        n = ref.split("/")[-1] if "/" in ref else ref
        d = wt / n
        if not d.exists() or not d.is_dir():
            logger.info(
                "Worktree re-attach: repo directory missing",
                agent_worktree_id=agent_worktree_id,
                repo=n,
            )
            return None

        # .git integrity
        try:
            gd = _sp.run(
                # ``safe.directory=*`` mirrors ``_clean_reused_worktree`` so a
                # host_uid worktree owned by a different uid than the
                # orchestrator process does not trip git's "dubious ownership"
                # guard — which would fail rev-parse and silently degrade
                # re-attach to create-with-retry on every event.
                ["git", "-C", str(d), "-c", "safe.directory=*", "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            ).stdout.strip()
            gdp = Path(gd)
            if not gdp.is_absolute():
                gdp = d / gdp
        except Exception as e:
            logger.info(
                "Worktree re-attach: .git check failed",
                agent_worktree_id=agent_worktree_id,
                repo=n,
                error=str(e),
            )
            return None

        # Lock files
        for lk in (gdp / "index.lock", gdp / "HEAD.lock", gdp / "config.lock"):
            if lk.exists():
                logger.info(
                    "Worktree re-attach: lock file present",
                    agent_worktree_id=agent_worktree_id,
                    repo=n,
                    lock=str(lk),
                )
                return None
        try:
            for pat in ("refs/heads/*.lock", "refs/remotes/*.lock"):
                if list(gdp.glob(pat)):
                    logger.info(
                        "Worktree re-attach: ref lock file present",
                        agent_worktree_id=agent_worktree_id,
                        repo=n,
                    )
                    return None
        except Exception:
            pass

        # Expected branch
        if branch:
            try:
                cb = _sp.run(
                    [
                        "git",
                        "-C",
                        str(d),
                        "-c",
                        "safe.directory=*",
                        "rev-parse",
                        "--abbrev-ref",
                        "HEAD",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ).stdout.strip()
                # The gateway creates per-agent worktrees on the role's own
                # local work branch (egg/{agent_worktree_id}/work), not on the
                # assigned branch it pushes to; the assigned branch alone
                # would mismatch on every event spawn and permanently degrade
                # re-attach to create-with-retry (#3480).
                work_branch = f"egg/{agent_worktree_id}/work"
                if cb not in (branch, work_branch, "HEAD"):
                    logger.info(
                        "Worktree re-attach: branch mismatch",
                        agent_worktree_id=agent_worktree_id,
                        repo=n,
                        expected=f"{work_branch} or {branch}",
                        actual=cb,
                    )
                    return None
            except Exception as e:
                logger.info(
                    "Worktree re-attach: branch check failed",
                    agent_worktree_id=agent_worktree_id,
                    repo=n,
                    error=str(e),
                )
                return None

        vols[ref] = str(d)

    logger.info(
        "Worktree re-attach: validation succeeded (cleanup pending)",
        agent_worktree_id=agent_worktree_id,
        repos=[r.split("/")[-1] if "/" in r else r for r in repos],
    )
    return vols


def _role_needs_worktree(role: AgentRole) -> bool:
    """Return True for roles whose work cannot proceed without a worktree."""
    return role not in _pkg._ROLES_WITHOUT_WORKTREE


def _host_to_local_volumes(repo_volumes: dict[str, str]) -> dict[str, str]:
    """Translate host paths to orchestrator-local paths for filesystem ops.

    The gateway returns worktree paths relative to the Docker host
    (e.g. ``/home/user/.egg-worktrees/...``), but the orchestrator
    container only sees these via a volume mount at ``/home/egg/...``.
    Uses the ``HOST_HOME`` env var to perform the translation.
    """
    host_home = os.environ.get("HOST_HOME", "").rstrip("/")
    container_home = "/home/egg"
    if not host_home or host_home == container_home:
        return repo_volumes
    return {
        name: path.replace(host_home, container_home, 1) if path.startswith(host_home) else path
        for name, path in repo_volumes.items()
    }


# Lazily-loaded (mount_point, host_root) pairs from /proc/self/mountinfo,
# longest mount_point first. ``None`` until first use; tests may seed it.
_LOCAL_MOUNT_MAPPING: list[tuple[str, str]] | None = None


def _load_local_mount_mapping(source: str = "/proc/self/mountinfo") -> list[tuple[str, str]]:
    """Read /proc/self/mountinfo and return (mount_point, host_root) pairs.

    Mirrors the gateway's ``_load_mount_mapping`` / ``translate_to_host_path``
    (``gateway/gateway.py``): for a kubelet- or docker-managed bind mount the
    mountinfo *root* field (``fields[3]``) records the host path the mount
    was bound from — exactly the value a Job spec's ``hostPath`` needs.

    Entries that cannot name a host directory are skipped, so translation is
    a no-op outside a container (unit tests, developer hosts): the rootfs
    ``/`` entry, entries whose root is ``/`` (tmpfs and other non-bind
    filesystems), and identity mappings (root == mount_point, e.g. a
    partition or subvolume mounted at its own path). Skipped entries fall
    through to the ``HOST_HOME`` env-var fallback in
    :func:`_local_to_host_path`.
    """
    entries: list[tuple[str, str]] = []
    try:
        with open(source) as fh:
            for line in fh:
                # Format: mount_id parent_id major:minor root mount_point ...
                fields = line.split()
                if len(fields) < 5:
                    continue
                host_root, mount_point = fields[3], fields[4]
                if mount_point == "/" or host_root == "/" or host_root == mount_point:
                    continue
                entries.append((mount_point, host_root))
    except OSError:
        return []
    entries.sort(key=lambda p: len(p[0]), reverse=True)
    return entries


def _local_to_host_path(local_path: str, mapping: list[tuple[str, str]] | None = None) -> str:
    """Translate an orchestrator-local path to the host path it is bound from.

    Counterpart of :func:`_host_to_local_volumes` (not an exact inverse:
    this uses mountinfo + ``HOST_HOME`` where that does a bare ``HOST_HOME``
    string replace), for paths the orchestrator
    derived from its OWN mounts (``WORKTREE_BASE_DIR``) that must be handed
    to a Job spec as ``hostPath`` sources. Handing the local path onward
    makes kubelet ``DirectoryOrCreate`` an empty root-owned dir on the node
    and the agent boots into an empty worktree, no-ops, and exits rc=0 —
    the silent consensus stall in #3502.

    Tries the mountinfo mapping first (no configuration needed — the same
    mechanism the gateway uses to return host paths from
    ``create_worktrees``), then the ``HOST_HOME`` env var for
    ``/home/egg``-prefixed paths, and otherwise returns the path unchanged
    (already a host path, or no translation is known).

    ``mapping`` overrides the lazily-cached mountinfo entries (tests).
    """
    global _LOCAL_MOUNT_MAPPING
    if mapping is None:
        if _LOCAL_MOUNT_MAPPING is None:
            _LOCAL_MOUNT_MAPPING = _load_local_mount_mapping()
        mapping = _LOCAL_MOUNT_MAPPING
    for mount_point, host_root in mapping:
        if local_path == mount_point or local_path.startswith(mount_point + "/"):
            return host_root + local_path[len(mount_point) :]

    host_home = os.environ.get("HOST_HOME", "").rstrip("/")
    container_home = "/home/egg"
    if (
        host_home
        and host_home != container_home
        and (local_path == container_home or local_path.startswith(container_home + "/"))
    ):
        return host_home + local_path[len(container_home) :]
    return local_path


def _local_to_host_volumes(repo_volumes: dict[str, str]) -> dict[str, str]:
    """Translate orchestrator-local repo volume paths to host paths (#3502).

    Applied to the volumes :func:`_validate_worktree_for_reuse` builds from
    ``WORKTREE_BASE_DIR`` so the reuse path hands the spawn path the same
    kind of value the create path gets from the gateway: a HOST path fit
    for a ``hostPath`` mount. Paths with no known translation pass through
    unchanged.
    """
    return {name: _local_to_host_path(path) for name, path in repo_volumes.items()}


def _try_reuse_worktree(
    self,
    agent_worktree_id: str,
    branch: str | None,
    repos: list[str] | None,
    *,
    pipeline_id: str | None = None,
    agent_role: str | None = None,
    slice_id: str | None = None,
    mode: str = "public",
) -> tuple[bool, dict[str, str]] | None:
    """Validate an existing worktree and, on success, clean dirty state.

    Composes :func:`_validate_worktree_for_reuse` (filesystem health
    checks) followed by :meth:`_clean_reused_worktree` (R6 dirty-state
    discard + fast-forward-aware sync, #3506). Returns ``(success, repo_volumes)`` on
    success, or ``None`` on any validation or cleanup mismatch (the
    caller falls back to create-with-retry).

    ``pipeline_id`` / ``agent_role`` / ``slice_id`` give the cleanup step
    the context it needs to auto-salvage and durably record any commits
    its hard-reset discards (#3509); when omitted the discard is log-only.
    ``mode`` is the pipeline's gateway network mode ("public" / "private")
    and MUST match the running pipeline: a "public" salvage push on a
    private-mode pipeline over a private repo is denied by the gateway's
    private-repo policy, degrading auto-salvage to record-only.

    The returned ``repo_volumes`` carry HOST paths (translated via
    :func:`_local_to_host_volumes`), matching the create path's
    gateway-returned values, because the caller feeds them straight into
    the Job spec's ``hostPath`` mounts. Handing the validator's
    orchestrator-local paths onward instead made every post-restart
    re-attach spawn mount an empty kubelet-created dir (#3502).

    Signature matches the tester's test-first contract:
    ``(agent_worktree_id, branch, repos) -> (bool, dict) | None``.

    ``repos`` is a list of ``"owner/repo"`` strings. When ``None`` or
    empty, the method returns ``None`` immediately — there is nothing
    to validate.
    """
    if not repos:
        return None
    vols = _validate_worktree_for_reuse(agent_worktree_id, repos, branch)
    if vols is None:
        return None
    if not self._clean_reused_worktree(
        agent_worktree_id,
        branch,
        repos,
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        slice_id=slice_id,
        mode=mode,
    ):
        return None
    return True, _local_to_host_volumes(vols)


def _clean_reused_worktree(
    self,
    agent_worktree_id: str,
    branch: str | None,
    repos: list[str] | None,
    *,
    pipeline_id: str | None = None,
    agent_role: str | None = None,
    slice_id: str | None = None,
    mode: str = "public",
) -> bool:
    """Discard dirty state and sync a re-attached worktree (R6, #3506).

    Applies ``git reset --hard && git clean -fd`` to discard uncommitted
    changes and untracked staging artifacts, then syncs to the role
    branch tip via ``git fetch origin {branch}``.

    The sync is **fast-forward-aware** (#3506): when the pre-discard tree
    was clean and the local HEAD is a strict descendant of
    ``origin/{branch}``, the local commits are the agent's own durable
    multi-session work (a clean session exit that had not pushed yet) and
    HEAD is kept. Unconditionally hard-resetting here made multi-session
    slices structurally impossible: every event-pump cycle silently
    orphaned the previous session's unpushed commits. On divergence, a
    behind-tip HEAD, or a dirty pre-discard tree (the killed-mid-event
    signature the R6 residue policy exists to contain), the worktree
    hard-resets to ``origin/{branch}``; any commits ahead of the tip that
    the reset discards are first auto-salvaged and durably recorded
    (#3509): the doomed tip is pushed to an ``egg/recovered/...``
    recovery ref through the same gateway launcher-auth path as #3368's
    ``salvage_agent_commits``, and a message-bus system message to the
    role records the discarded tip + recovery ref so a resuming agent
    with zero session memory can find its prior work instead of
    re-deriving it. Both steps are best-effort: a salvage or record
    failure is logged and the reset proceeds (blocking reuse would only
    force a fresh worktree that orphans the same commits with less
    visibility). The salvage/record steps require ``pipeline_id`` (plus
    ``agent_role`` / ``slice_id`` for the recovery-ref scope); legacy
    callers that omit them get the pre-#3509 log-only behaviour.

    ``mode`` is the pipeline's gateway network mode ("public" /
    "private") and is threaded straight into the salvage push. It MUST
    match the running pipeline: a "public" push on a private-mode
    pipeline over a private repo is denied by the gateway's private-repo
    policy, silently degrading auto-salvage to record-only — the exact
    silent-loss class this hook exists to prevent.

    Only the committed tip (``HEAD``) is salvaged, so a dirty tree with
    no commits used to fall outside every preservation path: the ``git
    reset --hard`` + ``git clean -fd`` above run *before* orphan
    detection, and orphan detection then found nothing to save. That is
    the #3639 loss (110 minutes across 33 modified files, discarded
    silently on a routine respawn). The gap is closed one step earlier:
    :func:`_preserve_dirty_tree` commits the dirty tree BEFORE the reset,
    so the snapshot becomes an ordinary orphan and rides the salvage +
    record path above. Preservation is best-effort and never blocks the
    reset; when it cannot run (no ``branch``, or the commit fails) the
    discard is logged at WARNING with the file count rather than the
    pre-#3639 silence.

    Returns ``True`` on success, ``False`` on any failure (the caller
    falls back to create-with-retry — never allow a half-cleaned
    worktree into the agent's commit scope).

    ``repos`` is a list of ``"owner/repo"`` strings. When ``None`` or
    empty, returns ``True`` (nothing to clean).
    """
    import subprocess as _sp

    def _git(repo_dir: Path, *args: str, timeout: int = 30, check: bool = True):
        return _sp.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "safe.directory=*",
                # A worktree that inherits ``commit.gpgsign=true`` from the
                # clone's config would fail every #3639 snapshot commit (no
                # signing key in the orchestrator image), losing exactly the
                # work the snapshot exists to save.
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    if not repos or not _pkg.WORKTREE_BASE_DIR.exists():
        return True
    wt = _pkg.WORKTREE_BASE_DIR / agent_worktree_id
    if not wt.exists():
        return True

    for ref in repos:
        n = ref.split("/")[-1] if "/" in ref else ref
        d = wt / n
        if not d.exists():
            continue

        # Record whether the tree carried uncommitted/untracked state
        # BEFORE the discard below erases the evidence: dirt is the
        # killed-mid-event signature that disqualifies the fast-forward
        # keep in the sync step (#3506). Unknown state counts as dirty.
        # ``state_unknown`` distinguishes "status failed, assume dirty" from
        # "status reported zero entries": without it the downstream WARNINGs
        # report ``discarded_dirty_entries=0``, which reads as "nothing was
        # there" on the one path where we genuinely do not know.
        state_unknown = False
        try:
            dirty_entries = _git(d, "status", "--porcelain").stdout.strip().splitlines()
            was_dirty = bool(dirty_entries)
        except Exception:
            dirty_entries = []
            was_dirty = True
            state_unknown = True

        # #3639: snapshot the dirty tree into a commit BEFORE the reset.
        # Uncommitted work is otherwise the one class of agent output no
        # preservation path can reach: ``reset --hard`` erases it and the
        # orphan detector below then finds nothing to salvage. Committing
        # here turns it into an ordinary orphan that the existing #3509
        # salvage + record path pushes to ``egg/recovered/...``. The commit
        # does NOT change the residue policy: ``was_dirty`` is already
        # latched above, so the tree still hard-resets to the origin tip
        # and the successor never inherits a killed-mid-event working set.
        wip_commit: str | None = None
        if was_dirty:
            if branch:
                wip_commit = _preserve_dirty_tree(
                    _git,
                    d,
                    agent_worktree_id=agent_worktree_id,
                    repo=n,
                    n_entries=len(dirty_entries),
                    state_unknown=state_unknown,
                )
            else:
                # No branch ⇒ no origin tip to reset to and no salvage
                # target, so a snapshot commit would just become the
                # successor's HEAD: un-vetted residue promoted to committed
                # state, which is exactly what R6 exists to prevent. Discard
                # as before, but say so.
                logger.warning(
                    "Worktree re-attach: discarding uncommitted work "
                    "(no branch to sync or salvage against)",
                    agent_worktree_id=agent_worktree_id,
                    repo=n,
                    discarded_dirty_entries=len(dirty_entries),
                    dirty_state_unknown=state_unknown,
                )

        # reset --hard
        try:
            _git(d, "reset", "--hard")
        except Exception as e:
            logger.warning(
                "Worktree re-attach: reset --hard failed",
                agent_worktree_id=agent_worktree_id,
                repo=n,
                error=str(e),
            )
            return False
        # clean -fd
        try:
            _git(d, "clean", "-fd")
        except Exception as e:
            logger.warning(
                "Worktree re-attach: clean -fd failed",
                agent_worktree_id=agent_worktree_id,
                repo=n,
                error=str(e),
            )
            return False
        # Sync to the role branch tip. This is the ONLY step that can
        # remove a predecessor's *local, unpushed* commit — ``reset
        # --hard`` (above) only discards the uncommitted working tree, so
        # a local commit always carries through to here. If the fetch
        # fails we MUST fall back to recreate (return False): without the
        # true origin tip we cannot tell the agent's own fast-forward
        # work from divergent residue. A transient ``fetch origin`` blip
        # is precisely what this resilience path must survive, so it is
        # fatal-to-reuse, not silently swallowed.
        if branch:
            try:
                _git(d, "fetch", "origin", branch, timeout=60)
            except Exception as e:
                logger.warning(
                    "Worktree re-attach: fetch failed; falling back "
                    "to recreate (cannot prove worktree is at origin tip)",
                    agent_worktree_id=agent_worktree_id,
                    repo=n,
                    error=str(e),
                )
                return False

            # Fast-forward-aware keep (#3506): equal-to-tip needs no
            # reset; a clean-tree strict descendant is the agent's own
            # accumulated work and is kept. Anything else (divergence,
            # behind-tip, dirty pre-discard tree, or an unreadable
            # topology) hard-resets to the origin tip.
            keep_local = False
            local_head = remote_tip = ""
            try:
                local_head = _git(d, "rev-parse", "HEAD").stdout.strip()
                remote_tip = _git(d, "rev-parse", f"origin/{branch}").stdout.strip()
                if local_head == remote_tip:
                    keep_local = True
                elif not was_dirty:
                    keep_local = (
                        _git(
                            d,
                            "merge-base",
                            "--is-ancestor",
                            remote_tip,
                            local_head,
                            check=False,
                        ).returncode
                        == 0
                    )
            except Exception:
                keep_local = False

            if keep_local:
                if local_head != remote_tip:
                    logger.info(
                        "Worktree re-attach: keeping local commits "
                        "(clean fast-forward of origin tip, #3506)",
                        agent_worktree_id=agent_worktree_id,
                        repo=n,
                        branch=branch,
                        local_head=local_head,
                        remote_tip=remote_tip,
                    )
            else:
                # Orphan detector (#3506): surface exactly which commits
                # the reset is about to discard; between-cycle resets
                # produce the same orphan class as restart-time resets
                # (#3368) and must not be silent.
                orphans: list[str] = []
                try:
                    if local_head and remote_tip:
                        orphans = _git(
                            d, "rev-list", f"{remote_tip}..{local_head}", check=False
                        ).stdout.split()
                except Exception:
                    orphans = []
                if orphans:
                    # Auto-salvage + durable record (#3509). Must run
                    # BEFORE the reset below: afterwards the tip exists
                    # only in the object store, where salvage_agent_commits
                    # cannot see it and gc eventually erases it.
                    recovery_ref = None
                    salvage_error: str | None = None
                    if pipeline_id:
                        try:
                            salvage = _pkg.agent_salvage.salvage_discarded_tip(
                                self.gateway,
                                pipeline_id=pipeline_id,
                                worktree_id=agent_worktree_id,
                                repo_path=d,
                                head_sha=local_head,
                                agent_role=agent_role,
                                slice_id=slice_id,
                                n_commits=len(orphans),
                                mode=mode,
                            )
                            recovery_ref = salvage.recovery_ref if salvage.ok else None
                            salvage_error = salvage.error
                        except Exception as e:
                            salvage_error = str(e)
                    else:
                        salvage_error = "no pipeline context (legacy caller)"
                    logger.warning(
                        "Worktree re-attach: hard-reset is discarding "
                        "unpushed local commits (dirty or diverged)",
                        agent_worktree_id=agent_worktree_id,
                        repo=n,
                        branch=branch,
                        was_dirty=was_dirty,
                        discarded_commit_count=len(orphans),
                        discarded_commits=orphans[:20],
                        recovery_ref=recovery_ref,
                        salvage_error=salvage_error,
                        wip_commit=wip_commit,
                    )
                    if pipeline_id:
                        _record_discarded_tip(
                            pipeline_id=pipeline_id,
                            agent_worktree_id=agent_worktree_id,
                            repo=n,
                            branch=branch,
                            agent_role=agent_role,
                            slice_id=slice_id,
                            discarded_tip=local_head,
                            remote_tip=remote_tip,
                            n_commits=len(orphans),
                            was_dirty=was_dirty,
                            recovery_ref=recovery_ref,
                            salvage_error=salvage_error,
                            wip_commit=wip_commit,
                        )
                try:
                    _git(d, "reset", "--hard", f"origin/{branch}")
                except Exception as e:
                    logger.warning(
                        "Worktree re-attach: hard-sync failed — falling back "
                        "to recreate (cannot prove worktree is at origin tip)",
                        agent_worktree_id=agent_worktree_id,
                        repo=n,
                        error=str(e),
                    )
                    # Fatal: without a successful hard-sync we cannot
                    # guarantee the worktree carries no predecessor residue
                    # ahead of origin/{branch}. Recreate-with-retry is the
                    # safe fallback.
                    return False

    logger.info(
        "Worktree re-attach: cleaned and synced",
        agent_worktree_id=agent_worktree_id,
        repos=[r.split("/")[-1] if "/" in r else r for r in repos],
    )
    return True


# Identity for the synthetic commit that captures a re-attached worktree's
# dirty state. Bound from the #2807 restart-path constants at import time so
# one ``[salvage]`` grep finds every machine-made working-tree snapshot
# regardless of which path took it, and so the two identities cannot drift.
# The module-level ``from`` import binds the real values and is unaffected by
# the suite's ``patch("kubernetes_spawner.agent_salvage")`` seam — that
# rebinds the package *attribute*, not what is already bound here.
_WIP_COMMIT_AUTHOR_NAME = _SALVAGE_COMMIT_NAME
_WIP_COMMIT_AUTHOR_EMAIL = _SALVAGE_COMMIT_EMAIL
_WIP_COMMIT_MESSAGE = (
    "[salvage] pre-reset working-tree state (#3639)\n"
    "\n"
    "Snapshot taken by the orchestrator's worktree re-attach before the R6\n"
    "dirty-state reset, which would otherwise discard it. This is a\n"
    "mechanical checkpoint of a previous session's working tree, not\n"
    "reviewed work."
)


def _preserve_dirty_tree(
    git: Callable[..., Any],
    repo_dir: Path,
    *,
    agent_worktree_id: str,
    repo: str,
    n_entries: int,
    state_unknown: bool = False,
) -> str | None:
    """Commit a re-attached worktree's dirty state before the R6 reset (#3639).

    ``_clean_reused_worktree``'s ``git reset --hard`` + ``git clean -fd``
    are the only step in the re-attach path that no preservation hook can
    see behind: #3509's auto-salvage runs after them and operates on
    commits, so a session that worked for hours without committing had its
    entire output erased on the next respawn, silently. Committing the
    tree here (tracked edits *and* non-ignored untracked files, via ``git
    add -A``) converts that state into a commit ahead of the origin tip,
    the exact shape the orphan detector already salvages to
    ``egg/recovered/...`` and records on the message bus.

    Returns the snapshot commit's SHA, or ``None`` when nothing was
    preserved (a tree with no committable change, or a failed commit).
    Strictly best-effort: every failure logs at WARNING and returns
    ``None`` so the caller proceeds with the reset. Blocking reuse instead
    would only send the spawn down the create-with-retry path, which
    discards the same state with less visibility.

    A failing ``git add -A`` is *not* treated as fatal. Per ``git-add(1)``
    the default behaviour on an unindexable entry (unreadable file, fifo,
    a filter that is not installed in the orchestrator image) is to abort
    the whole add and exit non-zero, leaving a partially populated index —
    so returning early there would discard the other N-1 files. The add
    passes ``--ignore-errors`` to keep going past the bad entry, and on a
    non-zero exit the helper still commits whatever reached the index:
    partial preservation strictly beats none for a helper whose purpose is
    "never lose the working tree".

    Ignored files (``.gitignore``) are deliberately not captured: they are
    build output and caches, not agent work, and sweeping them in would
    make the recovery ref unpushably large.

    This does not call ``agent_salvage.commit_working_tree`` (#2807's
    equivalent for the restart path) even though the two are otherwise the
    same operation: that helper's ``_run_git`` omits ``safe.directory=*``,
    which the re-attach path needs because the orchestrator's uid differs
    from the host uid owning the worktree. Reusing it would fail on
    "dubious ownership" exactly in production and silently degrade back to
    the discard this exists to prevent. The caller's ``git`` closure
    carries the right config, so it is threaded in as a parameter.
    """
    try:
        try:
            git(repo_dir, "add", "-A", "--ignore-errors", timeout=120)
        except Exception as add_error:  # partial index beats no index
            logger.warning(
                "Worktree re-attach: `git add -A` reported errors; committing "
                "whatever reached the index",
                agent_worktree_id=agent_worktree_id,
                repo=repo,
                dirty_entries=n_entries,
                dirty_state_unknown=state_unknown,
                error=str(add_error),
            )
        staged = git(repo_dir, "diff", "--cached", "--name-only", timeout=60).stdout.strip()
        if not staged:
            logger.warning(
                "Worktree re-attach: dirty tree held no committable change "
                "(ignored files, or submodule-only dirt); discarding it",
                agent_worktree_id=agent_worktree_id,
                repo=repo,
                discarded_dirty_entries=n_entries,
                dirty_state_unknown=state_unknown,
            )
            return None
        git(
            repo_dir,
            "-c",
            f"user.name={_WIP_COMMIT_AUTHOR_NAME}",
            "-c",
            f"user.email={_WIP_COMMIT_AUTHOR_EMAIL}",
            "commit",
            "--no-verify",
            "-m",
            _WIP_COMMIT_MESSAGE,
            timeout=120,
        )
        sha = git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    except Exception as e:  # preservation must never block the reset
        logger.warning(
            "Worktree re-attach: could not preserve uncommitted work; "
            "the hard reset below WILL discard it",
            agent_worktree_id=agent_worktree_id,
            repo=repo,
            discarded_dirty_entries=n_entries,
            dirty_state_unknown=state_unknown,
            error=str(e),
        )
        return None

    logger.warning(
        "Worktree re-attach: auto-committed uncommitted work before hard reset (#3639)",
        agent_worktree_id=agent_worktree_id,
        repo=repo,
        preserved_dirty_entries=n_entries,
        preserved_files=len(staged.splitlines()),
        wip_commit=sha,
    )
    return sha


def _record_discarded_tip(
    *,
    pipeline_id: str,
    agent_worktree_id: str,
    repo: str,
    branch: str | None,
    agent_role: str | None,
    slice_id: str | None,
    discarded_tip: str,
    remote_tip: str,
    n_commits: int,
    was_dirty: bool,
    recovery_ref: str | None,
    salvage_error: str | None,
    wip_commit: str | None = None,
) -> None:
    """Durably record a dirty-discard's orphaned tip where the agent looks (#3509).

    A resuming agent whose session state expired and whose durable memory
    commits rode the discarded lineage has no way to learn its prior tip
    existed: the #3506 incident showed such an agent silently re-deriving
    days of completed work while the pipeline reported WORKING. The
    message bus is the one channel that survives both failure modes (no
    TTL, replayed into brc history), so record the discarded tip, the
    reset target, and the recovery ref there as a system message to the
    role.

    ``wip_commit`` is set when the tip being discarded is the automatic
    snapshot :func:`_preserve_dirty_tree` took of the previous session's
    uncommitted work (#3639). The message calls that out, because a
    resuming agent must read such a commit as a mechanical checkpoint to
    inspect rather than as reviewed work it already proposed.

    The reassurance that nothing was lost is conditional on
    ``recovery_ref``: when the salvage push failed, the snapshot exists
    only in the local object store and the message must say so and ask for
    escalation. Telling a memory-less agent "nothing was lost" on the one
    path where the work is a ``gc`` away from gone would suppress exactly
    the escalation this record exists to trigger.

    Best-effort: a record failure is logged and swallowed; it must not
    block the re-attach path.
    """
    from message_store import Message, MessageType, get_message_store

    # A discard whose only casualty is the machine-made snapshot is not the
    # same event as losing a stack of the agent's own commits: the imperative
    # "inspect it before starting work" is what turns #3509's message into
    # background noise when every respawn with a stray memory file triggers
    # it. Soften the ask without suppressing the record.
    snapshot_only = bool(wip_commit) and wip_commit == discarded_tip and n_commits == 1

    if recovery_ref and snapshot_only:
        recovery_text = (
            f"The snapshot is preserved on remote ref {recovery_ref}; if any of "
            f"that work is missing, run `git fetch origin {recovery_ref}` and "
            "inspect it before re-deriving it."
        )
    elif recovery_ref:
        recovery_text = (
            f"The full commit stack is preserved on remote ref {recovery_ref}; "
            f"run `git fetch origin {recovery_ref}` and inspect it before "
            "starting work. If it contains completed work, build on it "
            "(cherry-pick or reset) instead of re-deriving it."
        )
    else:
        # NOT salvage_agent_commits: it enumerates worktree branches, which
        # the reset below has already moved off the discarded tip
        # (``salvage_discarded_tip``'s own docstring says so), so it provably
        # cannot see this sha. The worktree's HEAD reflog can.
        recovery_text = (
            f"Automatic salvage FAILED ({salvage_error or 'unknown error'}); the "
            "commits survive only in this worktree's local git object store "
            "until gc, unreachable from any ref. salvage_agent_commits cannot "
            "recover them (it inspects worktree branches the reset has already "
            f"moved, #3509) — ask an operator to fetch {discarded_tip} directly "
            "out of the worktree (`git reflog`) before re-deriving any work."
        )
    if wip_commit and recovery_ref:
        wip_text = (
            f" Commit {wip_commit} is an AUTOMATIC snapshot of the uncommitted "
            "changes your previous session left behind (#3639); it is on the "
            "recovery ref above, so nothing was lost. Treat it as a WIP "
            "checkpoint to review, not as work you already proposed."
        )
    elif wip_commit:
        wip_text = (
            f" Commit {wip_commit} is an AUTOMATIC snapshot of the uncommitted "
            "changes your previous session left behind (#3639) and it was NOT "
            "pushed — it exists only in the local object store. Escalate to an "
            "operator before re-deriving any work."
        )
    else:
        wip_text = ""
    count_text = f"{n_commits} unpushed commit(s)"
    if wip_commit:
        count_text += (
            " (one of which is an automatic snapshot of uncommitted work)"
            if n_commits > 1
            else " (an automatic snapshot of uncommitted work)"
        )
    body = (
        f"Worktree re-attach discarded {count_text} from "
        f"{repo} (worktree {agent_worktree_id}). Your previous tip was "
        f"{discarded_tip}; the worktree was reset to {remote_tip}"
        + (f" (origin/{branch})." if branch else ".")
        + " "
        + recovery_text
        + wip_text
    )
    try:
        store = get_message_store()
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role=agent_role or "all",
                message_type=MessageType.STATUS,
                subject=f"Unpushed commits discarded on re-attach ({repo})",
                body=body,
                metadata={
                    "event": "dirty_discard_salvage",
                    "agent_worktree_id": agent_worktree_id,
                    "repo": repo,
                    "branch": branch,
                    "slice_id": slice_id,
                    "discarded_tip": discarded_tip,
                    "remote_tip": remote_tip,
                    "discarded_commit_count": n_commits,
                    "was_dirty": was_dirty,
                    "recovery_ref": recovery_ref,
                    "salvage_error": salvage_error,
                    "wip_commit": wip_commit,
                },
            )
        )
    except Exception as e:
        logger.warning(
            "Failed to record discarded tip on message bus",
            pipeline_id=pipeline_id,
            agent_worktree_id=agent_worktree_id,
            repo=repo,
            discarded_tip=discarded_tip,
            error=str(e),
        )


def _find_missing_worktrees(self, agent_worktree_id: str, repos: list[str]) -> list[str]:
    """Return the list of per-agent worktree paths that don't exist on disk.

    Called right before spawning the k8s Job to catch the #1869 class
    of failure: ``create_worktrees`` returned success but the directory
    is gone by the time we'd spawn the Job (concurrent cleanup race,
    or create_worktrees was never called because ``repos`` was empty).
    Returns an empty list when all worktrees are in place.  Split out
    as an instance method so tests can monkey-patch it without having
    to manage a tmp filesystem hierarchy.
    """
    missing: list[str] = []
    for repo in repos:
        repo_name = repo.split("/")[-1] if "/" in repo else repo
        expected = _pkg.WORKTREE_BASE_DIR / agent_worktree_id / repo_name
        if not expected.exists():
            missing.append(str(expected))
    return missing
