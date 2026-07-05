"""Worktree validation / reuse / cleanup (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

import os
from pathlib import Path

import kubernetes_spawner as _pkg
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
) -> tuple[bool, dict[str, str]] | None:
    """Validate an existing worktree and, on success, clean dirty state.

    Composes :func:`_validate_worktree_for_reuse` (filesystem health
    checks) followed by :meth:`_clean_reused_worktree` (R6 dirty-state
    discard + fast-forward-aware sync, #3506). Returns ``(success, repo_volumes)`` on
    success, or ``None`` on any validation or cleanup mismatch (the
    caller falls back to create-with-retry).

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
    if not self._clean_reused_worktree(agent_worktree_id, branch, repos):
        return None
    return True, _local_to_host_volumes(vols)


def _clean_reused_worktree(
    self,
    agent_worktree_id: str,
    branch: str | None,
    repos: list[str] | None,
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
    the reset discards are first logged with their SHAs so the orphaning
    is visible and salvageable (``salvage_agent_commits``, #3368).

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
        try:
            was_dirty = bool(_git(d, "status", "--porcelain").stdout.strip())
        except Exception:
            was_dirty = True

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
                try:
                    if local_head and remote_tip:
                        orphans = _git(
                            d, "rev-list", f"{remote_tip}..{local_head}", check=False
                        ).stdout.split()
                        if orphans:
                            logger.warning(
                                "Worktree re-attach: hard-reset is discarding "
                                "unpushed local commits (dirty or diverged); "
                                "salvageable via salvage_agent_commits (#3368)",
                                agent_worktree_id=agent_worktree_id,
                                repo=n,
                                branch=branch,
                                was_dirty=was_dirty,
                                discarded_commit_count=len(orphans),
                                discarded_commits=orphans[:20],
                            )
                except Exception:
                    pass
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
