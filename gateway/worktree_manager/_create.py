"""WorktreeManager creation-path method bodies (#3312 slice-12).

Worktree create/reuse, push-upstream config, credentialed base/assigned-branch
fetch, fork-point resolution, safe-ref reset, and the retrying ``git worktree add``.
Bodies are extracted verbatim from the pre-split ``gateway/worktree_manager.py`` and
bound onto ``WorktreeManager`` in the barrel; they take ``self`` explicitly. The
credential seam (``get_token_for_repo`` / credential-helper pair) is read off the
barrel via ``_barrel()`` so a patch on worktree_manager.get_token_for_repo resolves.
"""

import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

from ._common import (
    WorktreeInfo,
    _tracking_refspec,
    logger,
    validate_branch_ref,
    validate_identifier,
)

try:
    from ..git_client import git_cmd
except ImportError:  # pragma: no cover - flat (container) import path
    from git_client import git_cmd  # type: ignore[no-redef, import-untyped]


def _barrel():
    """Return the package barrel so patched seams resolve at call time.

    Method bodies extracted here reference module-level symbols that tests
    rebind via ``patch("worktree_manager.<symbol>")`` (e.g. ``get_token_for_repo``,
    ``get_active_docker_containers``). Reading them off the barrel module at call
    time preserves those patch points across the split.
    """
    return sys.modules[__package__]


def resolve_default_branch(self, repo_name: str) -> str:
    """
    Resolve the remote's default branch for a repository.

    Tries in order:
    1. origin/HEAD symbolic ref (most reliable when configured)
    2. origin/main
    3. origin/master
    4. HEAD (fallback)

    Args:
        repo_name: Name of the repository

    Returns:
        The resolved branch reference (e.g., "origin/main")
    """
    main_repo = self.repos_base / repo_name
    if not main_repo.exists():
        return "HEAD"

    # Try origin/HEAD first (configured by git clone or git remote set-head)
    result = subprocess.run(
        git_cmd("symbolic-ref", "refs/remotes/origin/HEAD", "--short"),
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    # Try origin/main
    result = subprocess.run(
        git_cmd("rev-parse", "--verify", "origin/main"),
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return "origin/main"

    # Try origin/master
    result = subprocess.run(
        git_cmd("rev-parse", "--verify", "origin/master"),
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return "origin/master"

    # Fallback to HEAD — this may re-introduce the push rejection from #860
    # for pipeline sessions, so log at error level.
    logger.error(
        "Could not resolve remote default branch, falling back to HEAD",
        repo=repo_name,
    )
    return "HEAD"


def create_worktree(
    self,
    repo_name: str,
    container_id: str,
    base_branch: str = "HEAD",
    uid: int | None = None,
    gid: int | None = None,
    assigned_branch: str | None = None,
    repo_slug: str | None = None,
) -> WorktreeInfo:
    """
    Create an isolated worktree for a container.

    Args:
        repo_name: Name of the repository
        container_id: Container identifier (e.g., 'egg-xxx-yyy')
        base_branch: Branch or ref to base the worktree on (default: HEAD)
        uid: User ID to set ownership to (default: 1000)
        gid: Group ID to set ownership to (default: 1000)
        assigned_branch: Remote branch this worktree's pushes should
            target.  When set, configures ``branch.<local>.merge`` so
            the sandbox's push client builds a refspec that targets the
            assigned branch instead of the per-worktree local branch
            (which the gateway would reject as push_denied_wrong_branch).
            See #1809.  Also the preferred fork point for *fresh*
            worktrees: when ``origin/{assigned_branch}`` exists, the
            worktree forks from its tip (which carries the
            orchestrator's contract-init commit and any seeded
            drafts) rather than from ``base_branch`` — see #3068 and
            :meth:`_resolve_assigned_fork_point`.
        repo_slug: Full ``owner/repo`` slug used to resolve the GitHub
            token for the authenticated base-branch fetch (#3021).
            Defaults to ``repo_name`` when omitted, which makes token
            resolution fall through to bot mode.

    Returns:
        WorktreeInfo with paths and branch information

    Raises:
        ValueError: If inputs are invalid or repo not found
        RuntimeError: If worktree creation fails
    """
    # Default to egg user (1000:1000) if not specified
    if uid is None:
        uid = 1000
    if gid is None:
        gid = 1000

    # Validate uid/gid are positive integers
    if not isinstance(uid, int) or uid < 0:
        raise ValueError(f"Invalid uid: must be a non-negative integer, got {uid!r}")
    if not isinstance(gid, int) or gid < 0:
        raise ValueError(f"Invalid gid: must be a non-negative integer, got {gid!r}")

    # Validate inputs to prevent path traversal
    validate_identifier(container_id, "container_id")
    validate_identifier(repo_name, "repo_name")
    validate_branch_ref(base_branch, "base_branch")
    if assigned_branch is not None:
        validate_branch_ref(assigned_branch, "assigned_branch")

    # Full ``owner/repo`` slug for token resolution on the authenticated
    # base-branch fetch (#3021).  Callers that know the slug (the
    # worktree-create / session routes) pass it so the fetch uses the
    # correct bot/user token; otherwise fall back to ``repo_name``.
    if repo_slug is None:
        repo_slug = repo_name

    # Find main repo
    main_repo = self.repos_base / repo_name
    if not main_repo.exists():
        raise ValueError(f"Repository not found: {repo_name}")

    # Determine paths
    worktree_path = self.worktree_base / container_id / repo_name
    branch_name = f"egg/{container_id}/work"

    # Create container directory and set ownership immediately
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    self._chown_single(worktree_path.parent, uid, gid)

    # Check if worktree already exists AND is valid
    # A valid worktree has a .git file (not directory) containing "gitdir: ..."
    git_file = worktree_path / ".git"
    worktree_is_valid = (
        worktree_path.exists()
        and git_file.exists()
        and git_file.is_file()
        and git_file.read_text().strip().startswith("gitdir:")
    )

    if worktree_is_valid:
        logger.info(
            "Worktree already exists",
            container_id=container_id,
            repo=repo_name,
            path=str(worktree_path),
            assigned_branch=assigned_branch,
        )
        # Ensure ownership is correct (may have been created with different uid/gid)
        self._chown_recursive(worktree_path, uid, gid)
        self._chown_single(worktree_path.parent, uid, gid)
        # Re-apply push upstream config and reset to a safe ref under
        # the per-repo lock — both write to ``.git/config`` (and the
        # latter to ``.git/index``), and concurrent callers without
        # the lock race on ``.git/config.lock`` (#2311).
        with self._get_repo_lock(repo_name):
            self._configure_push_upstream(main_repo, branch_name, assigned_branch)
            # Reset HEAD to a known-good ref so we don't inherit a stale
            # left-over HEAD from a prior pipeline that collided on
            # ``container_id`` (deterministic pipeline_id can collide when
            # the same issue is resubmitted — see #2222).  Without this,
            # the new pipeline's first push hits non-fast-forward and
            # the reconcile path can absorb upstream main commits onto
            # the pipeline branch.
            self._reset_reused_worktree_to_safe_ref(
                worktree_path=worktree_path,
                main_repo=main_repo,
                container_id=container_id,
                assigned_branch=assigned_branch,
                base_branch=base_branch,
                repo_slug=repo_slug,
            )
        # Return info about existing worktree
        return WorktreeInfo(
            container_id=container_id,
            repo_name=repo_name,
            branch=branch_name,
            worktree_path=worktree_path,
            git_dir=self._find_worktree_git_dir(main_repo, worktree_path),
        )

    # If directory exists but is not a valid worktree, remove it first
    if worktree_path.exists():
        logger.warning(
            "Removing invalid/empty worktree directory",
            container_id=container_id,
            repo=repo_name,
            path=str(worktree_path),
        )
        shutil.rmtree(worktree_path, ignore_errors=True)

    # If rmtree couldn't fully remove (e.g., Docker bind mount), at minimum
    # remove the .git directory so git worktree add can create its .git FILE.
    if worktree_path.exists():
        git_path = worktree_path / ".git"
        if git_path.exists() and git_path.is_dir():
            shutil.rmtree(git_path, ignore_errors=True)

    # Serialize git operations against this repo to prevent index.lock contention
    with self._get_repo_lock(repo_name):
        # Clean up stale git admin dir (.git/worktrees/<name>) left by a
        # previous worktree that was not properly removed (e.g. broken btrfs
        # mount after restart_phase).  Without this, `git worktree add` fails
        # with "already registered" even though the worktree itself is
        # invalid.  Must be inside the repo lock to avoid TOCTOU race with
        # concurrent create_worktree / remove_worktree calls.  (#1723)
        admin_dir = self._find_worktree_git_dir(main_repo, worktree_path)
        if admin_dir is not None and admin_dir.exists():
            logger.warning(
                "Removing stale worktree admin dir before recreation",
                admin_dir=str(admin_dir),
                container_id=container_id,
                repo=repo_name,
            )
            shutil.rmtree(admin_dir, ignore_errors=True)

        # Check if branch already exists (from crashed session)
        branch_exists = (
            subprocess.run(
                git_cmd("rev-parse", "--verify", branch_name),
                cwd=main_repo,
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            == 0
        )

        if branch_exists:
            # Use existing branch instead of creating new one
            logger.info(
                "Reusing existing branch for worktree",
                branch=branch_name,
                container_id=container_id,
            )
            result = self._run_git_worktree_add(
                git_cmd("worktree", "add", str(worktree_path), branch_name),
                cwd=main_repo,
                main_repo=main_repo,
                worktree_path=worktree_path,
            )
        else:
            # Resolve the base ref.  Always fetch the base branch from
            # origin and branch the worktree from the *remote tip*
            # (``origin/<base_branch>``) rather than any local copy.
            # This fixes both #3021 failure modes:
            #   1. A branch present in the mirror at a stale SHA would
            #      otherwise be used silently — the local ref is never
            #      refreshed against origin.
            #   2. A branch absent from the mirror must be fetched on
            #      demand.
            # The fetch is authenticated with the repo's token via the
            # gateway credential helper (the same one git_push uses): the
            # mirror's origin is HTTPS — SSH URLs are rewritten to HTTPS
            # via ``insteadOf`` — so an unauthenticated fetch fails with
            # "could not read Username for https://github.com".  We fail
            # loudly on any fetch error rather than fall back to a
            # possibly-stale local ref.
            effective_base = base_branch
            if base_branch != "HEAD":
                # ``base_branch`` may arrive already ``origin/``-prefixed
                # (e.g. resolve_default_branch -> "origin/main"); fetch the
                # underlying branch name in that case.
                fetch_ref = (
                    base_branch[len("origin/") :]
                    if base_branch.startswith("origin/")
                    else base_branch
                )
                logger.info(
                    "Fetching base branch from remote before worktree create",
                    base_branch=base_branch,
                    fetch_ref=fetch_ref,
                    container_id=container_id,
                )
                # Timeout caps how long every other state-store commit /
                # worktree create on this repo can be blocked by a slow
                # remote — this fetch now runs on *every* non-HEAD spawn
                # (post-#3021) rather than just cold-cache misses, so keep
                # it tight.  Mirrors the reuse-path fetch in
                # ``_reset_reused_worktree_to_safe_ref``.
                #
                # The explicit refspec (rather than the bare branch name)
                # is what makes the ``origin/<fetch_ref>`` resolution
                # below see the fresh tip on narrow-refspec mirrors —
                # see ``_tracking_refspec`` (#3068).
                with self._git_credential_env(repo_slug) as fetch_env:
                    try:
                        fetch_result = subprocess.run(
                            git_cmd("fetch", "origin", _tracking_refspec(fetch_ref)),
                            cwd=main_repo,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=30,
                            env=fetch_env,
                        )
                    except subprocess.TimeoutExpired as e:
                        raise RuntimeError(
                            f"Timed out fetching base branch '{base_branch}' from remote"
                        ) from e
                if fetch_result.returncode != 0:
                    raise RuntimeError(
                        f"Failed to fetch base branch '{base_branch}' from remote: "
                        f"{fetch_result.stderr.strip()}"
                    )
                effective_base = f"origin/{fetch_ref}"

            # Prefer the assigned branch's remote tip over the base
            # branch when it exists (#3068).  The orchestrator's
            # contract-init commit (SDLC contract + any seeded
            # analysis/plan drafts) lands on the assigned branch and is
            # pushed before agents spawn; forking from the base left
            # every fresh agent worktree behind that commit, so seeded
            # artifacts never reached agents.  Mirrors the reuse path's
            # candidate order (``origin/{assigned}`` -> ``origin/{base}``
            # in ``_reset_reused_worktree_to_safe_ref``).  Best-effort:
            # an assigned branch with nothing pushed yet falls back to
            # the base branch (the prior behaviour); the base fetch
            # above keeps its #3021 hard-fail semantics either way.
            if assigned_branch:
                assigned_fork_point = self._resolve_assigned_fork_point(
                    main_repo=main_repo,
                    assigned_branch=assigned_branch,
                    repo_slug=repo_slug,
                    container_id=container_id,
                )
                if assigned_fork_point:
                    effective_base = assigned_fork_point

            # Create new branch from the freshly fetched remote tip
            result = self._run_git_worktree_add(
                git_cmd(
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    str(worktree_path),
                    effective_base,
                ),
                cwd=main_repo,
                main_repo=main_repo,
                worktree_path=worktree_path,
            )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worktree: {result.stderr}")

        # Lock the worktree so git worktree prune never removes its admin
        # dir while the container is alive.  Without this, git gc --auto
        # (triggered e.g. by git fetch) can run git worktree prune and
        # delete the admin dir if the worktree path is momentarily
        # inaccessible, breaking all subsequent git operations in the
        # container.  Removal uses --force --force to override the lock
        # (a single --force only handles dirty worktrees, not locked ones).
        lock_result = subprocess.run(
            git_cmd("worktree", "lock", str(worktree_path)),
            cwd=main_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if lock_result.returncode != 0:
            logger.warning(
                "Failed to lock worktree",
                container_id=container_id,
                repo=repo_name,
                stderr=lock_result.stderr.strip(),
            )

    # Set ownership so the container user can write to the worktree
    self._chown_recursive(worktree_path, uid, gid)
    # Also ensure the container directory itself is writable (non-recursive)
    self._chown_single(worktree_path.parent, uid, gid)

    # Point the per-worktree local branch at the assigned remote branch
    # so `git push` resolves to a refspec the gateway will accept
    # (#1809).  Must happen before returning so the sandbox's push
    # client sees the config on its first push.  Held under the
    # per-repo lock so the ``.git/config`` write does not race the
    # state-store's commits in the orchestrator pod (#2311).
    with self._get_repo_lock(repo_name):
        self._configure_push_upstream(main_repo, branch_name, assigned_branch)

    # Find the actual git dir (git names it based on worktree basename)
    git_dir = self._find_worktree_git_dir(main_repo, worktree_path)

    info = WorktreeInfo(
        container_id=container_id,
        repo_name=repo_name,
        branch=branch_name,
        worktree_path=worktree_path,
        git_dir=git_dir,
    )

    # Track in memory
    with self._lock:
        if container_id not in self._active_worktrees:
            self._active_worktrees[container_id] = []
        self._active_worktrees[container_id].append(info)

    logger.info(
        "Worktree created",
        container_id=container_id,
        repo=repo_name,
        path=str(worktree_path),
        branch=branch_name,
    )

    return info


def _configure_push_upstream(
    self,
    main_repo: Path,
    branch_name: str,
    assigned_branch: str | None,
) -> None:
    """Configure the per-worktree branch to push to the assigned branch.

    Without this, the sandbox's push client (``sandbox/egg_lib/orch_cli.py``)
    reads ``branch.<local>.merge`` and — finding it unset — sends the
    local branch name as the push destination.  The gateway's
    ``push_denied_wrong_branch`` policy then rejects the push because
    ``egg/{container_id}/work`` differs from the pipeline's assigned
    branch.  Agents sometimes "recover" from that rejection with
    ``git reset --hard``, destroying their own committed work
    (#1809).

    Best-effort: logs and returns on failure rather than aborting the
    worktree creation, since the old behaviour (no upstream) is still
    workable for non-pipeline sessions.
    """
    if not assigned_branch or assigned_branch == branch_name:
        return

    for key, value in (
        (f"branch.{branch_name}.remote", "origin"),
        (f"branch.{branch_name}.merge", f"refs/heads/{assigned_branch}"),
    ):
        result = subprocess.run(
            git_cmd("config", key, value),
            cwd=main_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to configure push upstream for worktree branch",
                branch=branch_name,
                assigned_branch=assigned_branch,
                key=key,
                stderr=result.stderr.strip(),
            )
            return


@contextlib.contextmanager
def _git_credential_env(
    self, repo_slug: str, *, best_effort: bool = False
) -> Generator[dict[str, str]]:
    """Yield an environment carrying GitHub credentials for a git fetch.

    The gateway's local mirror talks to GitHub over HTTPS (SSH URLs are
    rewritten via ``insteadOf``), so an unauthenticated ``git fetch``
    fails with ``could not read Username for 'https://github.com'``.
    This wires the same credential helper (``GIT_ASKPASS`` +
    ``GIT_USERNAME``/``GIT_PASSWORD``) that the push path uses
    (``gateway.git_push``), resolving the repo's bot/user token via
    :func:`get_token_for_repo`.  See #3021.

    When no token is available the plain process environment is yielded
    so best-effort callers still run; the fetch then fails loudly with
    git's own credential error, which the caller can surface.  The temp
    credential file is always cleaned up on exit.

    Args:
        repo_slug: Full ``owner/repo`` slug for token resolution.
        best_effort: When True, the caller treats the fetch as best
            effort (e.g. the reuse-path reset, which falls back to a
            local ref on fetch failure); a missing token then logs at
            ``info`` rather than ``warning`` to avoid spamming
            local-file-origin scenarios (tests, cold-start before the
            token refresher initialises) where the absence is benign.
            The create path leaves this False because a missing token
            yields a hard ``RuntimeError`` on fetch failure.

    In the no-token branch the ``GIT_USERNAME`` / ``GIT_PASSWORD`` /
    ``GIT_ASKPASS`` keys are explicitly scrubbed from the yielded env so
    a stale value inherited from a parent process (operator-set, leaked
    from a container env, etc.) doesn't accidentally authenticate the
    fetch — the documented "fails loudly with git's own credential
    error" behaviour only holds when those vars are genuinely absent.
    """
    token_str, _auth_mode, token_error = _barrel().get_token_for_repo(repo_slug)
    cred_path: str | None = None
    try:
        if token_str:
            cred_path, env = _barrel().create_credential_helper(token_str, os.environ.copy())
        else:
            log = logger.info if best_effort else logger.warning
            log(
                "No GitHub token available for authenticated fetch",
                repo_slug=repo_slug,
                token_error=token_error,
                best_effort=best_effort,
            )
            env = os.environ.copy()
            for key in ("GIT_USERNAME", "GIT_PASSWORD", "GIT_ASKPASS"):
                env.pop(key, None)
        yield env
    finally:
        _barrel().cleanup_credential_helper(cred_path)


def _resolve_assigned_fork_point(
    self,
    main_repo: Path,
    assigned_branch: str,
    repo_slug: str,
    container_id: str,
) -> str | None:
    """Resolve ``origin/{assigned_branch}`` as the fork point, if pushed.

    Fresh worktrees historically forked from ``origin/{base_branch}``
    only — but the orchestrator commits pipeline state (the SDLC
    contract plus any seeded analysis/plan drafts) to the assigned
    branch and pushes it *before* agents spawn (the contract-init push
    is mandatory; see "Worktree State Synchronization" in
    ``docs/guides/sdlc-pipeline.md``).  Forking from the base left
    every fresh agent worktree behind that commit, so seeded artifacts
    never reached agent worktrees (#3068).

    This mirrors the reuse path's candidate order in
    :meth:`_reset_reused_worktree_to_safe_ref` (``origin/{assigned}``
    first, ``origin/{base}`` as fallback) — and the same safety
    argument carries over: the orchestrator's create-pipeline
    stale-branch check (#2222 Phase 3a) refuses re-submits where
    ``origin/{assigned}`` carries prior-pipeline commits.

    Best-effort by design: a spawn can legitimately precede any push
    of the assigned branch (e.g. a slice integration branch that the
    first push creates), in which case the fetch finds nothing, this
    returns ``None``, and the caller falls back to the base branch —
    the pre-#3068 behaviour.  Contrast the base-branch fetch, which
    hard-fails per #3021 because the base must exist on origin.

    Returns:
        ``origin/{fetch_ref}`` (the input with any ``origin/`` prefix
        stripped) when the branch exists on origin (tracking ref
        freshly fetched), else ``None``.
    """
    # ``_tracking_refspec`` requires a bare branch name; mirror the
    # base-branch path's defensive strip in case a future caller drifts
    # to passing ``origin/<name>`` (today's callers pass bare names).
    fetch_ref = (
        assigned_branch[len("origin/") :]
        if assigned_branch.startswith("origin/")
        else assigned_branch
    )
    try:
        with self._git_credential_env(repo_slug, best_effort=True) as fetch_env:
            # Force C locale so the "couldn't find remote ref" stderr
            # heuristic below isn't translated by gettext under e.g.
            # ``LANG=de_DE.UTF-8``.  Without this, a translated error
            # would misclassify the "branch not pushed yet" case as a
            # transient failure (the WARNING branch).  Fallback
            # behaviour is correct either way; only log level/message
            # changes.
            fetch_env = {**fetch_env, "LC_ALL": "C"}
            fetch_result = subprocess.run(
                git_cmd("fetch", "origin", _tracking_refspec(fetch_ref)),
                cwd=main_repo,
                capture_output=True,
                text=True,
                check=False,
                # Matches the reuse path's per-fetch budget in
                # ``_reset_reused_worktree_to_safe_ref`` so the
                # worst-case create-path latency under
                # ``_get_repo_lock`` is bounded by ``base (30s) +
                # assigned (15s) = 45s`` rather than 60s.  The base
                # fetch keeps its 30s budget because it hard-fails per
                # #3021; the assigned fetch is best-effort and falls
                # back to the base branch on timeout.
                timeout=15,
                env=fetch_env,
            )
    except Exception as exc:
        # Best-effort: any failure (timeout, OSError, credential-helper
        # error from _git_credential_env, etc.) falls back to the base
        # branch.  A narrower catch would surface unexpected exception
        # types as a hard spawn failure, which is the opposite of the
        # "best-effort fallback" contract this helper advertises.
        logger.warning(
            "Assigned-branch fetch before worktree create failed (falling back to base)",
            container_id=container_id,
            assigned_branch=assigned_branch,
            error=str(exc),
        )
        return None
    if fetch_result.returncode != 0:
        # git fetch on a missing branch emits "couldn't find remote ref"
        # — that's the expected "not pushed yet" case and not an error.
        # Anything else (transient 5xx, auth, network) is worth flagging
        # at a higher level so an operator triaging "why didn't my seed
        # reach the agent?" sees the real cause.
        stderr = fetch_result.stderr.strip()
        if "couldn't find remote ref" in stderr:
            logger.info(
                "Assigned branch not on origin yet — forking worktree from base",
                container_id=container_id,
                assigned_branch=assigned_branch,
            )
        else:
            logger.warning(
                "Assigned-branch fetch failed (falling back to base)",
                container_id=container_id,
                assigned_branch=assigned_branch,
                stderr=stderr[:200],
            )
        return None

    candidate = f"origin/{fetch_ref}"
    verify = subprocess.run(
        git_cmd("rev-parse", "--verify", candidate),
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        return None
    logger.info(
        "Forking worktree from assigned branch tip",
        container_id=container_id,
        assigned_branch=assigned_branch,
    )
    return candidate


def _reset_reused_worktree_to_safe_ref(
    self,
    worktree_path: Path,
    main_repo: Path,
    container_id: str,
    assigned_branch: str | None,
    base_branch: str,
    repo_slug: str,
) -> None:
    """Hard-reset a reused worktree to a known-good remote ref.

    Picks the ref in this order:

    1. ``origin/{assigned_branch}`` if ``assigned_branch`` is set and
       resolvable.  This is the pipeline's own branch tip — by the
       time we reach this code the orchestrator's create-pipeline
       stale-branch check (#2222 Phase 3a) has already refused
       re-submits where ``origin/{assigned_branch}`` carries
       prior-pipeline commits, so a reset to it discards only
       container-local state.
    2. ``origin/{base_branch}`` if ``base_branch != "HEAD"`` and
       resolvable.  Used when the assigned branch hasn't been
       pushed yet (fresh pipeline, first agent) so there is no
       remote tip to reset to.
    3. No-op if neither resolves — preserves prior behaviour rather
       than risk leaving the worktree in an undefined state.

    Best-effort: any git failure is logged and swallowed so a
    transient hiccup doesn't break worktree reuse.  The downstream
    orchestrator-side ``_sync_worktree_with_remote`` and
    ``_rebase_pipeline_branch_onto_base`` provide a second line of
    defence.
    """
    # Best-effort fetch so the remote-tracking refs are current; if
    # this fails we still attempt the reset against whatever local
    # state we have.  This runs inside the cross-process lock (the
    # caller holds it across this whole method) so the timeout
    # caps how long every other state-store commit / worktree
    # create on this repo can be blocked by a slow remote — keep
    # it tight.  Two candidate branches max (assigned + base) at
    # 15s each preserves the prior ~30s cumulative ceiling.
    #
    # Each candidate branch is fetched with an explicit tracking
    # refspec, one fetch per branch.  The previous bare ``git fetch
    # origin`` honoured the repo's configured refspec — on
    # narrow-refspec mirrors that refreshes only the configured
    # branch, so the ``origin/{assigned}`` / ``origin/{base}``
    # candidates below resolved stale tracking refs (#3068).
    # Per-branch fetches also keep an absent assigned branch (fresh
    # pipeline, first agent — nothing pushed yet) from failing the
    # base-branch fetch, and are cheaper than a full all-refs fetch
    # on large mirrors.
    #
    # ``_tracking_refspec`` requires a bare branch name; mirror the
    # base-branch and create-path defensive strips for drift-resistance
    # against future callers passing ``origin/<name>`` (today's callers
    # pass bare names).  ``base_branch`` may also arrive
    # ``origin/``-prefixed in pipeline mode (``gateway.py`` sets
    # ``worktree_base_branch = f"origin/{...}"`` and
    # ``resolve_default_branch`` returns ``"origin/main"``).  Strip
    # the prefix on both so the fetch refspec and the candidate-ref
    # lookup below agree on the bare name — otherwise the secondary
    # candidate is built as ``origin/origin/main`` and fails to resolve.
    assigned_name = assigned_branch.removeprefix("origin/") if assigned_branch else None
    fetch_branches: list[str] = []
    if assigned_name:
        fetch_branches.append(assigned_name)
    base_name: str | None = None
    if base_branch and base_branch != "HEAD":
        base_name = base_branch.removeprefix("origin/")
        if base_name not in fetch_branches:
            fetch_branches.append(base_name)
    try:
        with self._git_credential_env(repo_slug, best_effort=True) as fetch_env:
            for fetch_branch in fetch_branches:
                subprocess.run(
                    git_cmd("fetch", "origin", _tracking_refspec(fetch_branch)),
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                    env=fetch_env,
                )
    except Exception as exc:
        # Best-effort: any failure (timeout, OSError, credential-helper
        # error from ``_git_credential_env``, etc.) is logged and we
        # still attempt the reset against whatever local state we have.
        # A narrower catch would surface unexpected exception types as
        # a hard worktree-reuse failure, contradicting this helper's
        # best-effort contract — mirrors the create-path's catch in
        # ``_resolve_assigned_fork_point``.
        logger.warning(
            "Fetch before worktree-reuse reset failed (continuing)",
            container_id=container_id,
            worktree_path=str(worktree_path),
            error=str(exc),
        )

    target_ref: str | None = None
    candidates: list[str] = []
    if assigned_name:
        candidates.append(f"origin/{assigned_name}")
    if base_name is not None:
        candidates.append(f"origin/{base_name}")
    for candidate in candidates:
        verify = subprocess.run(
            git_cmd("rev-parse", "--verify", candidate),
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if verify.returncode == 0:
            target_ref = candidate
            break

    if target_ref is None:
        logger.info(
            "Worktree reuse: no remote ref to reset to (preserving HEAD)",
            container_id=container_id,
            worktree_path=str(worktree_path),
            assigned_branch=assigned_branch,
            base_branch=base_branch,
        )
        return

    reset = subprocess.run(
        git_cmd("reset", "--hard", target_ref),
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if reset.returncode != 0:
        logger.warning(
            "Worktree reuse: reset to safe ref failed (continuing)",
            container_id=container_id,
            worktree_path=str(worktree_path),
            target_ref=target_ref,
            stderr=reset.stderr.strip(),
        )
        return
    logger.info(
        "Worktree reuse: reset HEAD to safe remote ref",
        container_id=container_id,
        worktree_path=str(worktree_path),
        target_ref=target_ref,
    )


def _run_git_worktree_add(
    self,
    args: list[str],
    cwd: Path,
    main_repo: Path,
    worktree_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Run ``git worktree add`` with retry on index.lock contention.

    The primary benefit of retrying is **waiting for a short-lived
    external lock to be released** (e.g., a concurrent ``git fetch``
    started outside the WorktreeManager).  The stale-lock cleanup
    (files older than 60 s) is a secondary safeguard for the rare
    case where a previous process crashed and left a lock behind.

    Between retries the helper also removes any partial worktree
    directory that ``git worktree add`` may have created before
    hitting the lock error, preventing the next attempt from
    failing with "already exists".

    Attempts up to 5 times with exponential backoff (0.5 s, 1.0 s,
    2.0 s, 4.0 s).
    """
    max_attempts = 5
    backoff = 0.5

    for attempt in range(1, max_attempts + 1):
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode == 0:
            return result

        if "index.lock" not in result.stderr or attempt == max_attempts:
            return result

        # index.lock contention — try to clean stale lock and retry
        logger.warning(
            "index.lock contention, retrying",
            attempt=attempt,
            max_attempts=max_attempts,
            stderr=result.stderr.strip(),
        )

        # Look for stale lock files in the main repo and worktrees dir
        lock_candidates = [main_repo / ".git" / "index.lock"]
        worktrees_dir = main_repo / ".git" / "worktrees"
        if worktrees_dir.exists():
            lock_candidates.extend(worktrees_dir.glob("*/index.lock"))

        for lock_candidate in lock_candidates:
            if lock_candidate.exists():
                try:
                    age = time.time() - lock_candidate.stat().st_mtime
                    if age > 60:
                        lock_candidate.unlink(missing_ok=True)
                        logger.info(
                            "Removed stale lock file",
                            path=str(lock_candidate),
                            age_seconds=round(age, 1),
                        )
                except OSError:
                    pass

        # Clean up partial worktree state so the next attempt doesn't
        # fail with "already exists" or "already checked out".
        if worktree_path is not None and worktree_path.exists():
            git_file = worktree_path / ".git"
            worktree_is_valid = (
                git_file.exists()
                and git_file.is_file()
                and git_file.read_text().strip().startswith("gitdir:")
            )
            if not worktree_is_valid:
                logger.info(
                    "Removing partial worktree directory before retry",
                    path=str(worktree_path),
                    attempt=attempt,
                )
                shutil.rmtree(worktree_path, ignore_errors=True)

        time.sleep(backoff)
        backoff *= 2

    return result  # unreachable, but keeps type checkers happy


def create_phase_worktree(
    self,
    repo_name: str,
    container_id: str,
    phase_id: str,
    base_branch: str = "HEAD",
    uid: int | None = None,
    gid: int | None = None,
    repo_slug: str | None = None,
) -> WorktreeInfo:
    """Create a sub-worktree for a specific plan phase (Tier 3 parallel dispatch).

    Creates a worktree branched from the pipeline worktree for isolated
    phase-level implementation. Branch naming: egg/<feature>/phase-N.

    Args:
        repo_name: Name of the repository
        container_id: Container identifier
        phase_id: Plan phase ID (e.g., 'phase-1')
        base_branch: Branch or ref to base the worktree on
        uid: User ID for ownership
        gid: Group ID for ownership

    Returns:
        WorktreeInfo for the phase worktree
    """
    # Sanitize phase_id for use in paths
    safe_phase_id = re.sub(r"[^a-zA-Z0-9-]", "-", phase_id)
    phase_container_id = f"{container_id}-{safe_phase_id}"

    # Validate
    validate_identifier(container_id, "container_id")
    validate_identifier(repo_name, "repo_name")

    # Create worktree using existing infrastructure
    return self.create_worktree(
        repo_name=repo_name,
        container_id=phase_container_id,
        base_branch=base_branch,
        uid=uid,
        gid=gid,
        repo_slug=repo_slug,
    )
