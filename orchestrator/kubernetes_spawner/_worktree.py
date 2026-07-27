"""Worktree validation / reuse / cleanup (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

import os
from collections.abc import Callable
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, NamedTuple

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

    def _git(
        repo_dir: Path,
        *args: str,
        timeout: int = 30,
        check: bool = True,
        errors: str | None = None,
    ):
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
                # Pinned, not inherited (#3639 re-review NB-3). Every command
                # here runs under ``text=True`` with a strict UTF-8 decode, so
                # any git output that echoes a filename verbatim raises
                # ``UnicodeDecodeError`` from inside ``run`` the moment a path
                # in this tree is not valid UTF-8. The default
                # ``core.quotePath=true`` C-quote-encodes those bytes to ASCII
                # and is what keeps ``status --porcelain`` and ``clean -fd``
                # decodable; a worktree that inherited ``quotePath=false``
                # would break them — ``clean -fd`` failing *after* the snapshot
                # commit but *before* the salvage push loses the commit
                # entirely. ``-z`` reads are unaffected either way (git never
                # quotes NUL-terminated output), so this costs nothing and
                # turns an inherited default into an invariant.
                "-c",
                "core.quotePath=true",
                *args,
            ],
            capture_output=True,
            text=True,
            errors=errors,
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
        wip_files: int | None = None
        wip_paths: tuple[str, ...] | None = None
        wip_partial = False
        if was_dirty:
            if branch:
                snapshot = _preserve_dirty_tree(
                    _git,
                    d,
                    agent_worktree_id=agent_worktree_id,
                    repo=n,
                    n_entries=len(dirty_entries),
                    state_unknown=state_unknown,
                )
                if snapshot is not None:
                    wip_commit, wip_files = snapshot.sha, snapshot.n_files
                    wip_paths, wip_partial = snapshot.paths, snapshot.partial
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
                    dirty_entries=len(dirty_entries),
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
                        # Completeness rides with the sha so "which recovery
                        # refs are truncated" is one query over this WARNING,
                        # not a join back to the earlier _preserve_dirty_tree
                        # line by worktree id. Named ``wip_*`` to match the bus
                        # record's metadata keys exactly (#3639 re-review
                        # NB-1): one query shape has to work over both, and a
                        # log field called ``preserved_files`` next to a
                        # metadata field called ``wip_files`` guarantees it
                        # does not.
                        wip_partial=wip_partial,
                        wip_files=wip_files,
                        # ``wip_files`` is ``None`` when the staged-path read
                        # failed — the exact path this WARNING exists to make
                        # queryable. A bare ``None`` renders as ``wip_files=``,
                        # which reads as *zero files preserved* rather than
                        # *unknown*, so the distinction gets its own boolean
                        # (the same reason ``dirty_state_unknown`` exists).
                        wip_files_unknown=(wip_commit is not None and wip_files is None),
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
                            wip_files=wip_files,
                            wip_paths=wip_paths,
                            wip_partial=wip_partial,
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

# Paths whose loss in a discard costs nothing durable: each is rebuilt by the
# next event and has a durable record elsewhere, so a respawn that trips this
# path over nothing else is routine and :func:`_record_discarded_tip` may
# soften its ask. Matching the noise source *by name* rather than by a file
# count is deliberate: a count threshold scores one rewritten 400-line module
# identically to one stray memory file, and telling the agent that ref "is as
# likely to be a leftover state file as work" is the one framing that could
# talk it out of fetching a real #3639 loss. Anything not on this list —
# including an unknown file set — takes the imperative.
#
# Membership test is "mechanically regenerated on the next event, with a
# durable backstop elsewhere", not "written by orchestrator code" and not
# "looks mechanical". The narrower "orchestrator-written" rule would exclude
# the dominant member below, whose *writer* is in the sandbox and whose
# content is agent-authored prose:
#   * ``<role>/brc-memory-<pipeline-id>.md`` — the dominant case. Written by
#     ``sandbox/egg_agent_tools/handlers/brc_memory.py::write_memory_atomic``,
#     reached from the agent's own ``brc_ack``/``brc_nack`` tool call, so the
#     prose in it is the reviewer's. It qualifies because the next ack/nack
#     rewrites it and ``docs/architecture/brc-memory.md`` names the durable
#     backstop: the orchestrator message history, rehydrated by
#     ``reconstruct_tracker_from_messages``.
#   * ``consensus-confirmed`` — ``routes/signals/_consensus_confirm`` writes the
#     marker the gateway reads back (``gateway/session_manager``); the
#     consensus state it mirrors lives in the tracker.
#   * ``<pipeline-id>-apply-handoff.json`` — ``routes/pipelines/_ledger``
#     writes the applier's *input* handoff just before APPLY spawns, and
#     rewrites it on the next spawn from the ledger it was derived from.
# Deliberately NOT listed, though they sit in the same directory and look
# alike: ``<pipeline>-wontdo.json`` and ``<identifier>-tester-output.json`` are
# agent *output* with no regeneration path — nothing rewrites them on the next
# event and no other store holds them — so a discard that loses them is a real
# loss, worth the imperative. That is the line to test a new entry against.
_MACHINE_STATE_FILE_GLOBS = (
    ".egg-state/agent-outputs/*/brc-memory*.md",
    ".egg-state/agent-outputs/consensus-confirmed",
    ".egg-state/agent-outputs/*-apply-handoff.json",
)

# Above this many paths the soft branch states a count instead of naming each
# one: a bus body is read by an agent with a context budget, and a per-role
# memory file for a wide roster would otherwise inline a dozen paths to say
# "nothing here". ``wip_paths`` rides in the metadata either way.
_SOFT_BRANCH_MAX_NAMED_PATHS = 4

# Cap on the ``wip_paths`` list carried in the bus record's metadata. The
# #3639 incident was 33 files; a cap in that neighbourhood keeps the whole
# path set for realistic discards while bounding a pathological one.
_METADATA_MAX_PATHS = 50

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
# Appended when ``git add -A`` did not complete cleanly. A truncated snapshot
# is otherwise indistinguishable downstream from a complete one — same subject,
# same ``egg/recovered/...`` ref — and the only other record is an
# orchestrator WARNING nobody who cherry-picks this commit will read.
#
# "did not complete cleanly" rather than "reported errors" (#3639 re-review
# NB-6): the caller's ``except Exception`` sets ``partial`` for a
# ``TimeoutExpired`` at 120s as well as for a non-zero exit, and a commit
# message that asserts git's exit status when the add never returned one is a
# claim a triager cannot check. The neutral phrasing covers both without
# losing the fact that staging is where the truncation happened.
#
# Deliberately a near-duplicate of the #2807 restart path's
# ``agent_salvage._UNCOMMITTED_SALVAGE_PARTIAL_SUFFIX`` rather than a shared
# constant: the two differ only in naming whose working tree was truncated
# ("previous session's" here, "crashed agent's" there), and that provenance is
# the one thing a triager reading a lone commit message cannot infer. The
# grep token — the leading ``INCOMPLETE:`` and the ``git add -A`` phrase — is
# identical in both, so one search still finds every truncated snapshot
# regardless of which path took it, and
# ``docs/reference/agent-recovery.md`` quotes it verbatim for triagers.
# Change one, change the other — and the runbook.
_WIP_COMMIT_PARTIAL_SUFFIX = (
    "\n"
    "\n"
    "INCOMPLETE: `git add -A` did not complete cleanly while staging, so\n"
    "files present in the previous session's working tree may be missing\n"
    "from this commit."
)


class _DirtySnapshot(NamedTuple):
    """The commit :func:`_preserve_dirty_tree` made, and what is in it.

    ``paths`` and ``n_files`` are carried alongside the sha because they
    are the only signal that separates the #3639 incident (110 minutes, 33
    files) from a respawn whose worktree held one stray
    ``brc-memory-*.md``. The message a resuming agent reads is worded off
    them — see :func:`_record_discarded_tip`.

    ``partial`` is set when ``git add -A`` did not complete cleanly, so
    the commit may be missing files the previous session's tree held. It
    rides all the way to the bus message: a snapshot that is silently
    truncated is a worse failure than one the agent is told is truncated,
    and the orchestrator WARNING that records it is not a surface the
    resuming agent reads.

    ``paths``/``n_files`` are ``None`` when the staged-path list could not
    be read at all — the ``diff --cached`` timed out, exited non-zero, or
    raised (see :func:`_preserve_dirty_tree`).
    That degrades the *wording* to the imperative, never the snapshot: the
    commit is taken either way, because a path list is a nicety and the
    working tree is the thing #3639 exists to save.
    """

    sha: str
    n_files: int | None
    paths: tuple[str, ...] | None
    partial: bool = False


def _preserve_dirty_tree(
    git: Callable[..., Any],
    repo_dir: Path,
    *,
    agent_worktree_id: str,
    repo: str,
    n_entries: int,
    state_unknown: bool = False,
) -> _DirtySnapshot | None:
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

    Returns a :class:`_DirtySnapshot` (the commit's SHA, the files it
    captured, and whether the capture was partial), or ``None`` when
    nothing was preserved (a tree with no committable change, or a failed
    commit). Nothing short of "no commit exists" returns ``None``: a
    staged-path list that cannot be read — *for any reason*, decode
    failure or timeout or a non-zero ``diff`` — degrades the snapshot's
    *metadata* (``paths``/``n_files`` become ``None``) and never its
    existence. Undecodable bytes cost less than that: they are replaced
    per-path, so a single latin-1 filename leaves the other N-1 names
    intact.
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
    "never lose the working tree". Such a snapshot is marked ``partial``
    and says so in both its commit message and the bus record, so a
    resuming agent that cherry-picks it knows files may be missing.

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
    partial = False
    try:
        try:
            git(repo_dir, "add", "-A", "--ignore-errors", timeout=120)
        except Exception as add_error:  # partial index beats no index
            partial = True
            logger.warning(
                "Worktree re-attach: `git add -A` did not complete cleanly; "
                "committing whatever reached the index",
                agent_worktree_id=agent_worktree_id,
                repo=repo,
                dirty_entries=n_entries,
                dirty_state_unknown=state_unknown,
                # A non-zero exit and a 120s ``TimeoutExpired`` both land here
                # and mean different things to a triager; the commit message's
                # ``INCOMPLETE:`` paragraph is deliberately neutral between
                # them, so the distinction has to live in the log.
                error_type=type(add_error).__name__,
                error=str(add_error),
            )
        # ``-z`` (NUL-terminated, unmunged bytes) rather than the newline
        # form: with the default ``core.quotePath=true`` git C-quote-encodes
        # any path holding non-ASCII or control characters, so
        # ``.egg-state/agent-outputs/coder/brc-memory-café.md`` comes back as
        # the literal token ``".egg-state/.../brc-memory-caf\303\251.md"``,
        # double quotes included — and ``splitlines()`` additionally misparses
        # a path containing a newline. Those encoded names would flow verbatim
        # into :func:`_path_matches_glob` (harmless: the leading quote fails
        # every glob, so the record takes the imperative — the safe default)
        # and into the ``wip_paths`` bus metadata, which is a machine-readable
        # field a consumer matches its own paths against. NUL separation makes
        # the field mean what its name says.
        #
        # ``-z`` moves the failure mode down a layer. Unmunged bytes reach the
        # caller's ``subprocess.run(..., text=True)``, which decodes as strict
        # UTF-8 by default: a filename that is not valid UTF-8 (a latin-1 name
        # from an extracted archive, a fixture written with raw bytes) would
        # raise ``UnicodeDecodeError`` *inside* ``run``, before the split.
        #
        # ``errors="replace"`` decodes the bad bytes to U+FFFD instead of
        # raising, so one bad name costs one name rather than the whole path
        # set (#3639 re-review NB-2) — the shape
        # ``routes/pipelines/_worktree_sync`` already uses for its own ``-z``
        # read. Replacement is byte-local and never synthesises a NUL, so
        # replacing across the whole stream is equivalent to decoding each
        # path separately. ``surrogateescape`` was rejected: lone surrogates
        # are unencodable by ``json.dumps`` and by a UTF-8 DB driver, which
        # would move the failure downstream into the message bus. A replaced
        # name still fails every glob in :func:`_path_matches_glob`, so it can
        # only cost the softened wording, never grant it.
        #
        # The handler is deliberately ``Exception`` and not
        # ``UnicodeDecodeError`` (#3639 re-review B1). This read is *metadata
        # only*: whatever makes it fail — a decode this replace pass does not
        # cover, the 60s timeout expiring on a large staged set, a non-zero
        # exit from an index another git process is still holding — must not
        # reach the outer handler, which abandons the commit and hands the
        # whole working tree to the reset. That is #3639 itself, with the
        # trigger moved from "one bad filename" to "the metadata read was slow
        # or exited non-zero". The cost is strictly one-directional: ``staged
        # is None`` can only lose the softening and the size claim, never make
        # the record quieter or wronger. A genuinely empty index is unaffected
        # — the ``git commit`` below then exits non-zero and falls through to
        # the outer handler exactly as it does today.
        try:
            staged_out = git(
                repo_dir,
                "diff",
                "--cached",
                "--name-only",
                "-z",
                timeout=60,
                errors="replace",
            ).stdout
            staged: list[str] | None = [p for p in staged_out.split("\0") if p]
        except Exception as read_error:
            logger.warning(
                "Worktree re-attach: staged-path list could not be read "
                "(undecodable filename, timeout, or a failed diff); "
                "committing the snapshot without a path set",
                agent_worktree_id=agent_worktree_id,
                repo=repo,
                dirty_entries=n_entries,
                dirty_state_unknown=state_unknown,
                error_type=type(read_error).__name__,
                error=str(read_error),
            )
            staged = None
        # Only a *known*-empty index skips the commit. ``staged is None`` means
        # "could not tell", and this helper never discards a tree on a maybe.
        if staged is not None and not staged:
            logger.warning(
                "Worktree re-attach: dirty tree held no committable change "
                "(ignored files, submodule-only dirt, or a failed add); "
                "discarding it",
                agent_worktree_id=agent_worktree_id,
                repo=repo,
                dirty_entries=n_entries,
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
            _WIP_COMMIT_MESSAGE + (_WIP_COMMIT_PARTIAL_SUFFIX if partial else ""),
            timeout=120,
        )
        sha = git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    except Exception as e:  # preservation must never block the reset
        logger.warning(
            "Worktree re-attach: could not preserve uncommitted work; "
            "the hard reset below WILL discard it",
            agent_worktree_id=agent_worktree_id,
            repo=repo,
            dirty_entries=n_entries,
            dirty_state_unknown=state_unknown,
            error=str(e),
        )
        return None

    paths = tuple(staged) if staged is not None else None
    logger.warning(
        "Worktree re-attach: auto-committed uncommitted work before hard reset (#3639)",
        agent_worktree_id=agent_worktree_id,
        repo=repo,
        dirty_entries=n_entries,
        dirty_state_unknown=state_unknown,
        # ``wip_*`` throughout, matching the discard WARNING and the bus
        # record's metadata keys so one query shape spans all three
        # (#3639 re-review NB-1).
        wip_files=len(paths) if paths is not None else None,
        wip_files_unknown=paths is None,
        wip_partial=partial,
        wip_commit=sha,
    )
    return _DirtySnapshot(
        sha=sha,
        n_files=len(paths) if paths is not None else None,
        paths=paths,
        partial=partial,
    )


def _path_matches_glob(path: str, glob: str) -> bool:
    """Segment-wise glob match where ``*`` does not cross ``/``.

    ``fnmatch`` is the wrong primitive for a discriminator whose whole job
    is to match the noise source *precisely*: its ``*`` crosses separators,
    so ``.egg-state/agent-outputs/a/b/c/brc-memory-x.md`` matches
    ``.../*/brc-memory*.md``. Matching segment-by-segment with equal depth
    keeps a deeper path off the soft branch.

    :func:`fnmatch.fnmatchcase` rather than :func:`fnmatch.fnmatch` is a
    smaller point and not a behaviour change on the deployment platform:
    ``fnmatch`` normalises case through ``os.path.normcase``, which is the
    identity on POSIX, so both are case-sensitive on Linux. The explicit
    form states the intended semantics on every platform rather than
    inheriting them from the host.
    """
    segments = path.split("/")
    patterns = glob.split("/")
    if len(segments) != len(patterns):
        return False
    return all(fnmatchcase(s, p) for s, p in zip(segments, patterns, strict=True))


def _is_machine_state_only(paths: tuple[str, ...] | None) -> bool:
    """True when every captured path is regenerated state with a backstop.

    "Machine state" here means the file is rewritten by the next event and
    the thing it carries survives elsewhere (``_MACHINE_STATE_FILE_GLOBS``
    documents the test per member) — *not* that the orchestrator wrote it.
    The dominant member, ``brc-memory-<pipeline-id>.md``, is written by the
    sandbox on the agent's own tool call and holds agent-authored prose; it
    qualifies on regeneration, not provenance.

    The discriminator behind :func:`_record_discarded_tip`'s soft wording.
    An empty or unknown path set is False: softening must be earned by
    evidence, never fall out of missing evidence.
    """
    if not paths:
        return False
    return all(
        any(_path_matches_glob(p, glob) for glob in _MACHINE_STATE_FILE_GLOBS) for p in paths
    )


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
    wip_files: int | None = None,
    wip_paths: tuple[str, ...] | None = None,
    wip_partial: bool = False,
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

    ``wip_paths`` is what that snapshot contains, and it is what decides
    how hard the message pushes. "Snapshot-only" on its own is a bad proxy
    for "trivial": the #3639 incident (110 minutes, 33 modified files,
    zero commits) is snapshot-only too, so keying the soft wording off the
    commit count alone would soften precisely the case this record exists
    for. The ask is softened only when every captured path is a known
    machine-maintained state file — one the next event rewrites and whose
    contents survive elsewhere (``_MACHINE_STATE_FILE_GLOBS``) — since the
    noise source is known by name, and matching it by name is strictly
    sharper than any size threshold. On the imperative branches ``wip_files`` is
    stated outright rather than asking the reader whether anything is
    "missing": a memory-less agent has no baseline against which that
    question means anything, which is this function's own premise. (The
    soft branch states the paths themselves, so the count is redundant
    there and is not rendered.)

    ``wip_partial`` marks a snapshot whose ``git add -A`` reported errors.
    It is surfaced because an agent that cherry-picks a silently truncated
    snapshot fails worse than one told the snapshot is truncated, and it
    also *disqualifies* the soft branch: a truncated capture's path list
    omits whatever failed to stage, so it cannot establish that the
    snapshot holds nothing but state files.

    Best-effort: a record failure is logged and swallowed; it must not
    block the re-attach path.
    """
    from message_store import Message, MessageType, get_message_store

    # A discard whose only casualty is the machine-made snapshot is not the
    # same event as losing a stack of the agent's own commits: the imperative
    # "inspect it before starting work" is what turns #3509's message into
    # background noise when every respawn with a stray memory file triggers
    # it. But the *contents* of the snapshot, not its snapshot-ness, are what
    # make it ignorable — #3639 itself was 33 files with no commits, and a
    # single rewritten source module is the same loss one file wide. Only a
    # capture consisting entirely of regenerated-with-a-backstop state files softens;
    # an unknown or unrecognised file set counts as substantial, so the soft
    # wording is an opt-in for the demonstrably trivial case, never a default.
    #
    # ``wip_partial`` disqualifies the soft branch for the same reason
    # ``wip_paths=None`` does. When ``git add -A`` reported errors the path
    # list is *by construction* only the subset that reached the index — the
    # files that failed to stage are absent from it — so "every captured path
    # is a state file" says nothing about what was in the tree. A partial
    # capture is missing evidence by another name, and softening must never
    # fall out of missing evidence.
    snapshot_only = bool(wip_commit) and n_commits == 1
    machine_state_only = _is_machine_state_only(wip_paths)
    trivial_snapshot = snapshot_only and not wip_partial and machine_state_only
    # ``wip_files``/``wip_paths`` are assigned together off ``_DirtySnapshot``
    # and are both ``None`` on the one production path where the snapshot
    # exists but its contents could not be read: a filename whose bytes are
    # not valid UTF-8 makes the staged-path list undecodable, and
    # ``_preserve_dirty_tree`` commits anyway rather than lose the tree over a
    # name. So the ``None`` arms — ``_is_machine_state_only(None) is False``
    # above, and the ``snapshot_size`` fallback just below — are live
    # degradation paths, not merely defensive: knowing the sha but not the
    # contents must take the imperative rather than crash or soften.
    snapshot_size = (
        f"{wip_files} file(s) of uncommitted work" if wip_files is not None else "uncommitted work"
    )

    if recovery_ref and trivial_snapshot:
        # ``trivial_snapshot`` implies a non-empty ``wip_paths``.
        paths = wip_paths or ()
        # The descriptor stays out of the sentence's grammar: an apposition
        # ("— a state file the orchestrator rewrites on every BRC ack/nack")
        # is singular on a plural subject, and it hardcodes one member's
        # provenance into a message keyed off a tuple designed to grow, so
        # the second entry makes the clause false.
        if len(paths) == 1:
            named = f"only `{paths[0]}`"
        elif len(paths) <= _SOFT_BRANCH_MAX_NAMED_PATHS:
            named = f"only {len(paths)} files (" + ", ".join(f"`{p}`" for p in paths) + ")"
        else:
            named = f"only {len(paths)} files"
        # "rewritten by the step that produces it rather than restored before
        # you start" and not "rebuilt on your next event" (#3639 re-review
        # NB-7): nothing restores these files ahead of the agent. brc-memory
        # is rewritten by the agent's *own* ``brc_ack``/``brc_nack`` — i.e.
        # after it has already redone the review — and the other two entries
        # are rewritten by whichever orchestrator step next produces them. The
        # softening still holds (none of it is lost work), but the sentence
        # must not promise the agent it will find the file waiting.
        recovery_text = (
            f"The snapshot holds {named} — machine-maintained coordination state, "
            "rewritten by the step that produces it rather than restored "
            "before you start, and durably recorded elsewhere. It is "
            f"preserved on remote ref {recovery_ref}; run `git fetch origin "
            f"{recovery_ref}` to read it if you need it."
        )
    elif recovery_ref and snapshot_only:
        recovery_text = (
            f"The snapshot holds {snapshot_size} and is preserved on remote ref "
            f"{recovery_ref}; run `git fetch origin {recovery_ref}` and inspect it "
            "before starting work. If it contains completed work, build on it "
            "(cherry-pick or reset) instead of re-deriving it."
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
    if recovery_ref and trivial_snapshot:
        # ``trivial_snapshot`` already implies a truthy ``wip_commit`` (via
        # ``snapshot_only``), so this arm needs no separate conjunct for it.
        # The softening has to survive the whole body: restating "AUTOMATIC
        # snapshot ... treat it as a WIP checkpoint to review" here would put
        # an imperative in the last sentence the reader sees, undoing the
        # branch above — which is exactly the noise this case exists to
        # suppress.
        wip_text = f" Commit {wip_commit} is that snapshot; it is on the recovery ref above."
    elif wip_commit and recovery_ref:
        wip_text = (
            f" Commit {wip_commit} is an AUTOMATIC snapshot of the uncommitted "
            "changes your previous session left behind (#3639); it is on the "
            "recovery ref above, so nothing was lost. Treat it as a WIP "
            "checkpoint to review, not as work you already proposed."
        )
    elif wip_commit:
        wip_text = (
            f" Commit {wip_commit} is an AUTOMATIC snapshot of the uncommitted "
            f"changes your previous session left behind ({snapshot_size}, #3639) "
            "and it was NOT pushed — it exists only in the local object store. "
            "Escalate to an operator before re-deriving any work."
        )
    else:
        wip_text = ""
    partial_text = (
        " WARNING: `git add -A` reported errors while taking this snapshot, so "
        "it may be INCOMPLETE — files the previous session's working tree held "
        "may be missing from it."
        if wip_commit and wip_partial
        else ""
    )
    count_text = f"{n_commits} unpushed commit(s)"
    # ``recovery_text`` already names the snapshot and its contents on the
    # trivial branch; repeating it in the opening clause is the third
    # restatement of the same fact in one message. That is only true under
    # ``recovery_ref`` — with the salvage push failed, ``recovery_text`` is
    # the escalation prose, which never names the snapshot, so dropping the
    # clarifier there would leave the opening clause silent about what the
    # discarded commit actually was.
    if wip_commit and not (trivial_snapshot and recovery_ref):
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
        + partial_text
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
                    # The body makes a size claim and a completeness claim;
                    # both belong in the metadata so a consumer (or a triage
                    # query like "discards over N files") reads them
                    # structurally instead of regexing the prose. ``wip_paths``
                    # and the derived verdict ride along because they are what
                    # the wording decision is *made from* — without them a
                    # consumer can see a softened body and cannot reconstruct
                    # why it was softened. The path list is capped: a bus
                    # record is not the place to inline an arbitrarily wide
                    # working tree, and ``wip_files`` already carries the
                    # untruncated count.
                    #
                    # The two derived fields are kept distinct because they
                    # answer different questions and diverge on real inputs.
                    # ``wip_machine_state_only`` is the *path predicate* alone,
                    # so it stays true for a multi-commit discard, a truncated
                    # capture, or a failed salvage push — cases where the
                    # wording is not softened. ``wip_softened`` is the actual
                    # verdict the body took, gated on ``recovery_ref`` exactly
                    # as the soft branch is. Reporting the verdict under the
                    # predicate's name would contradict ``wip_paths`` in one
                    # direction and the body in the other.
                    "wip_files": wip_files,
                    "wip_partial": wip_partial,
                    "wip_paths": list(wip_paths[:_METADATA_MAX_PATHS]) if wip_paths else None,
                    "wip_paths_truncated": bool(wip_paths and len(wip_paths) > _METADATA_MAX_PATHS),
                    "wip_machine_state_only": machine_state_only,
                    "wip_softened": bool(recovery_ref and trivial_snapshot),
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
