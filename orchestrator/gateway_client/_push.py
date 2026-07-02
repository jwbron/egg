"""Branch push + push-reconciliation rebase mechanics (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

import os
import subprocess
from typing import Literal

import gateway_client as _pkg
from gateway_client import GatewayError
from gateway_client._models import PushResult


def push_worktree_branch(
    self,
    pipeline_id: str,
    repo_path: str,
    branch: str,
    mode: Literal["public", "private"] = "public",
    ref: str | None = None,
    base_branch: str | None = None,
    force: bool = False,
    force_with_lease: bool = False,
) -> PushResult:
    """Push a branch to remote with launcher-auth (orchestrator-trusted).

    Called after contract initialization, phase completion, or pipeline
    failure.  Authenticates with the launcher secret rather than a
    sandbox session token: the orchestrator is on the privileged side
    of the trust boundary and its programmatic pushes bypass the
    agent-targeted pipeline-push enforcement (#2028, #2051).

    When ``ref`` is ``None`` (default), pushes the worktree's current
    ``HEAD`` via ``HEAD:refs/heads/{branch}`` — used when ``repo_path``
    is a dedicated pipeline worktree. The worktree's ``HEAD`` may be on
    ``branch`` or detached (the per-slice BRC hook adds its worktree
    with ``--detach``, #2778); the refspec names the destination
    branch explicitly, so a branch checked out at ``repo_path`` is not
    required. On non-fast-forward rejection, performs
    ``git fetch origin`` + ``git rebase origin/{branch}`` in the
    worktree (with ``.egg-state/agent-outputs/`` auto-resolve) and
    retries the push once.

    When ``ref`` is set, pushes ``refs/heads/{ref}:refs/heads/{branch}``
    — used when ``repo_path`` is a plain repository (not a worktree
    checked out to ``branch``) whose ``.git/`` holds the commits to
    push. Reconcile is skipped: there is no working tree at
    ``repo_path`` that can be rebased onto the remote tip without
    disturbing its checkout.

    Args:
        pipeline_id: Pipeline ID (used as container_id for the temp session)
        repo_path: Path to the repo directory the gateway will ``cd`` into
        branch: Remote branch name to push to
        mode: Gateway session mode (public/private)
        ref: Local ref to push (omit to push worktree HEAD)
        base_branch: Pipeline's base branch (e.g. ``"main"``).  When
            set, the reconcile rebase uses ``--onto origin/{branch}
            origin/{base_branch}`` so commits already on the base
            branch are not replayed onto the pipeline branch (#1976).
            Ignored when ``ref`` is set (reconcile is skipped there).
        force: When ``True``, send ``--force`` so the push overwrites
            a non-ancestor remote tip.  Used by the rebase-on-resume
            helper to replace a stale ``origin/<branch>`` with a
            rebased-onto-base version (#2098).  Skips reconcile on
            failure since force-push has nothing to reconcile against.
        force_with_lease: When ``True``, send ``--force-with-lease``
            — overwrite a non-ancestor remote tip only if it still
            matches the local tracking ref ``refs/remotes/origin/
            {branch}`` at ``repo_path``.  The caller must have just
            fetched that tracking ref with an explicit refspec (a
            bare-name fetch leaves it stale on narrow-refspec
            mirrors, #3072) or the lease will reject.  Used by the
            state-branch divergence reconciler (#3088).  Skips
            reconcile on failure, like ``force``.

    Returns:
        ``PushResult`` whose ``ok`` flag is ``True`` on success and
        ``False`` otherwise. On failure, ``category`` and ``detail``
        describe why so callers can surface an operator-actionable
        error (e.g. ``"non_fast_forward"``, ``"auth_failed"``,
        ``"reconcile_fetch_failed"``). ``PushResult`` is truthy on
        success so existing ``if push_ok:`` callers work unchanged.
    """
    refspec = f"refs/heads/{ref}:refs/heads/{branch}" if ref else f"HEAD:refs/heads/{branch}"

    first = self._do_push(
        pipeline_id=pipeline_id,
        repo_path=repo_path,
        branch=branch,
        mode=mode,
        refspec=refspec,
        force=force,
        force_with_lease=force_with_lease,
    )
    if first.ok:
        return first

    # Reconcile is only meaningful for worktree-HEAD pushes: the rebase
    # mutates the checkout at repo_path, which we only want to do when
    # that checkout is a dedicated pipeline worktree.  Force pushes
    # also skip reconcile — the caller has already decided to overwrite.
    if ref is not None or force or force_with_lease:
        _pkg.logger.warning(
            "Push failed (no reconcile available)",
            pipeline_id=pipeline_id,
            branch=branch,
            ref=ref,
            force=force,
            category=first.category,
            detail=first.detail,
        )
        return first

    return self._reconcile_and_retry_push(
        pipeline_id=pipeline_id,
        worktree_path=repo_path,
        branch=branch,
        mode=mode,
        refspec=refspec,
        initial_failure=first,
        base_branch=base_branch,
    )


def _do_push(
    self,
    pipeline_id: str,
    repo_path: str,
    branch: str,
    mode: Literal["public", "private"],
    refspec: str,
    force: bool = False,
    force_with_lease: bool = False,
) -> PushResult:
    """Send a single push request to the gateway with launcher auth.

    The orchestrator authenticates directly with the launcher secret —
    no register-session/push/delete ceremony.  The push endpoint
    recognises launcher auth as orchestrator-trusted and skips the
    agent-targeted enforcement (pipeline-push block, push-target,
    role/phase file restrictions).  ``mode`` is forwarded in the
    request body so the private-repo policy still applies.

    Returns ``PushResult(ok=True)`` on success.  On failure the gateway
    HTTP 500 body carries git stderr in ``details["stderr"]``; we
    classify it into a category and propagate both category and raw
    stderr so callers can build an operator-actionable error.
    """
    try:
        # Do NOT include container_id — the repo_path is already resolved
        # (orchestrator-side worktree on the shared hostPath).  Including
        # one would route through map_container_path_to_worktree() and
        # fail "worktree not found" (#1500).
        self._make_request(
            "/api/v1/git/push",
            method="POST",
            data={
                "repo_path": repo_path,
                "remote": "origin",
                "refspec": refspec,
                "mode": mode,
                "force": force,
                "force_with_lease": force_with_lease,
            },
            use_launcher_auth=True,
        )

        _pkg.logger.info(
            "Pushed branch to remote",
            pipeline_id=pipeline_id,
            branch=branch,
            refspec=refspec,
        )
        return PushResult(ok=True)
    except GatewayError as e:
        # Gateway returns 500 + details={"stderr": ...} on push failure
        # (see gateway/gateway.py push handler). Connection/transport
        # errors surface as GatewayError without details.
        stderr = ""
        if isinstance(e.details, dict):
            stderr = (e.details.get("stderr") or "").strip()
        if stderr:
            category = _classify_push_stderr(stderr)
            detail = stderr
        else:
            category = "gateway_unreachable" if e.status_code is None else "gateway_error"
            detail = e.message or str(e)
        _pkg.logger.info(
            "Push attempt failed — caller may retry via reconcile",
            pipeline_id=pipeline_id,
            branch=branch,
            refspec=refspec,
            category=category,
            error=detail,
        )
        return PushResult(ok=False, category=category, detail=detail)
    except Exception as e:
        _pkg.logger.info(
            "Push attempt failed — caller may retry via reconcile",
            pipeline_id=pipeline_id,
            branch=branch,
            refspec=refspec,
            error=str(e),
        )
        return PushResult(ok=False, category="unknown", detail=str(e))


def _reconcile_and_retry_push(
    self,
    pipeline_id: str,
    worktree_path: str,
    branch: str,
    mode: Literal["public", "private"],
    refspec: str,
    initial_failure: PushResult,
    base_branch: str | None = None,
) -> PushResult:
    """Fetch, rebase the worktree onto ``origin/{branch}``, and retry push.

    Runs directly against the worktree filesystem (shared hostPath) so
    the orchestrator can mutate the checkout without round-tripping
    through the gateway. Conflicts confined to
    ``.egg-state/agent-outputs/`` are resolved in favour of the remote;
    conflicts elsewhere abort the rebase and return a failure result.

    When ``base_branch`` is provided, ``origin/{base_branch}`` is also
    fetched before the rebase and used as the ``--onto`` upstream so
    commits already on main are not replayed onto the pipeline branch
    (#1976).

    Returns ``PushResult(ok=True)`` when the retry push succeeds. On
    failure, the returned ``PushResult`` carries a category that
    identifies which stage of reconcile failed
    (``reconcile_fetch_failed``, ``reconcile_rebase_failed``,
    ``reconcile_retry_failed/<inner>``) so callers can distinguish
    "original push was rejected and reconcile never ran" from
    "reconcile ran but retry push still failed" without reading the
    gateway source.
    """
    _pkg.logger.warning(
        "Push rejected — attempting fetch+rebase+retry to reconcile divergence",
        pipeline_id=pipeline_id,
        branch=branch,
        initial_category=initial_failure.category,
        initial_detail=initial_failure.detail,
    )

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_path}",
        "-C",
        str(worktree_path),
    ]

    try:
        subprocess.run(
            [*git_base, "fetch", "origin", branch],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as fetch_err:
        stderr = (fetch_err.stderr or "").strip()
        _pkg.logger.error(
            "Push reconcile: fetch failed — work remains on local worktree only",
            pipeline_id=pipeline_id,
            branch=branch,
            stderr=stderr,
        )
        return PushResult(
            ok=False,
            category="reconcile_fetch_failed",
            detail=stderr or f"git fetch exited {fetch_err.returncode}",
        )
    except subprocess.TimeoutExpired:
        _pkg.logger.error(
            "Push reconcile: fetch timed out — work remains on local worktree only",
            pipeline_id=pipeline_id,
            branch=branch,
        )
        return PushResult(
            ok=False,
            category="reconcile_fetch_timeout",
            detail=f"git fetch origin {branch} timed out after 60s",
        )

    # Refresh origin/{base_branch} so the --onto upstream in the rebase
    # reflects the current main tip (#1976).  A stale origin/{base_branch}
    # would cause commits that landed on main since the worktree was
    # created to be replayed as duplicate-by-content commits.  Best-effort:
    # if fetching the base fails (network, permissions, branch absent),
    # fall through to the plain ``git rebase origin/{branch}`` form.
    if base_branch:
        try:
            base_fetch = subprocess.run(
                [*git_base, "fetch", "origin", base_branch],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            _pkg.logger.warning(
                "Push reconcile: base-branch fetch timed out — proceeding with stale origin/{base}",
                pipeline_id=pipeline_id,
                branch=branch,
                base_branch=base_branch,
            )
        else:
            if base_fetch.returncode != 0:
                _pkg.logger.warning(
                    "Push reconcile: base-branch fetch failed — proceeding with stale origin/{base}",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    base_branch=base_branch,
                    returncode=base_fetch.returncode,
                    stderr=base_fetch.stderr.strip(),
                )

    rebase_result = _rebase_with_agent_output_autoresolve(
        git_base=git_base,
        pipeline_id=pipeline_id,
        branch=branch,
        base_branch=base_branch,
    )
    if not rebase_result.ok:
        return rebase_result

    _pkg.logger.info(
        "Push reconcile: rebase succeeded — retrying push",
        pipeline_id=pipeline_id,
        branch=branch,
    )
    retry = self._do_push(
        pipeline_id=pipeline_id,
        repo_path=worktree_path,
        branch=branch,
        mode=mode,
        refspec=refspec,
    )
    if retry.ok:
        return retry

    _pkg.logger.error(
        "Push reconcile: retry push still failed — work remains on local worktree only",
        pipeline_id=pipeline_id,
        branch=branch,
        retry_category=retry.category,
        retry_detail=retry.detail,
    )
    inner = retry.category or "unknown"
    return PushResult(
        ok=False,
        category=f"reconcile_retry_failed/{inner}",
        detail=retry.detail,
    )


def delete_remote_branch(
    self,
    pipeline_id: str,
    repo_path: str,
    branch: str,
    mode: Literal["public", "private"] = "public",
) -> PushResult:
    """Delete a remote branch with launcher auth (orchestrator-trusted).

    Sends a deletion refspec (``:branch``) through the same
    ``_do_push`` path used by ``push_worktree_branch``.  Authenticates
    with the launcher secret rather than a sandbox session token: the
    orchestrator is on the privileged side of the trust boundary, and
    the gateway's pipeline-push enforcement (#2028) was 403'ing the
    old temp-session shape, so cleanup silently no-op'd and shared
    ``egg/<pipeline-id>`` branches accumulated on origin (#2055).

    Returns ``PushResult`` so callers can distinguish ``already_deleted``
    (the desired state — branch absent on remote) from real failures
    (``permission_denied``, ``network``, etc.).  ``PushResult`` is
    truthy on success so existing ``if delete_remote_branch(...):``
    callers keep working.
    """
    return self._do_push(
        pipeline_id=pipeline_id,
        repo_path=repo_path,
        branch=branch,
        mode=mode,
        refspec=f":{branch}",
    )


def _classify_push_stderr(stderr: str) -> str:
    """Classify a git push stderr into a coarse failure category.

    Matches are substring-based on the lowercased stderr so the same
    classifier handles pack-protocol errors, HTTP transport errors, and
    plain push-rejected output. Unknown shapes fall back to
    ``"push_rejected"``.
    """
    s = stderr.lower()
    if "non-fast-forward" in s or "(fetch first)" in s:
        return "non_fast_forward"
    if "authentication failed" in s or "invalid credentials" in s or " 403" in s:
        return "auth_failed"
    if "permission denied" in s or ("permission to" in s and "denied" in s):
        return "permission_denied"
    if "does not exist" in s and "repository" in s:
        return "repo_missing"
    if (
        "could not resolve host" in s
        or "could not read from remote" in s
        or "connection timed out" in s
        or "connection refused" in s
        or "network is unreachable" in s
    ):
        return "network"
    if "shallow" in s:
        return "shallow_clone"
    if "already exists" in s:
        return "branch_exists"
    if "remote ref does not exist" in s:
        return "already_deleted"
    return "push_rejected"


def _rebase_with_agent_output_autoresolve(
    git_base: list[str],
    pipeline_id: str,
    branch: str,
    base_branch: str | None = None,
    max_autoresolve_iterations: int = 3,
) -> PushResult:
    """Rebase the worktree onto ``origin/{branch}`` with agent-outputs auto-resolve.

    When ``base_branch`` is provided and ``origin/{base_branch}`` exists
    locally, the rebase uses the ``--onto origin/{branch}
    origin/{base_branch}`` form so only commits that are unique to the
    local worktree (i.e. ``origin/{base_branch}..HEAD``) are replayed.
    Without the ``--onto`` form, a plain ``git rebase origin/{branch}``
    replays the full ``merge-base(HEAD, origin/{branch})..HEAD`` range;
    when ``origin/{branch}`` is based on an older snapshot of main and
    HEAD is based on a newer snapshot, that range includes the upstream
    main commits that landed in between, producing duplicate-by-content
    commits with different SHAs on the pipeline branch (#1976).

    Conflicts confined to ``.egg-state/agent-outputs/`` are resolved in
    favour of the remote (``git checkout --theirs``) and the rebase is
    continued; conflicts anywhere else cause the rebase to be aborted
    and a failure ``PushResult`` returned.

    The rebase runs with ``--autostash`` so it can proceed against a
    dirty worktree (#2714).  The orchestrator continuously writes
    statefile / agent-output deltas without committing them eagerly, so
    every divergence-reconcile attempt hits a working tree with unstaged
    changes; without autostash, ``git rebase`` aborts immediately and the
    sync helper returns without bringing origin's commits into local.
    ``git rebase --autostash`` can also exit 0 with a half-applied
    autostash pop (rebase succeeded, but the pop hit a conflict because
    the rebased HEAD touches the same paths as the stashed delta) — that
    leaves ``UU`` entries in the worktree and the autostash sitting in
    ``git stash list``.  We detect this post-rebase via
    ``_list_unmerged_paths`` and surface ``reconcile_autostash_pop_conflict``
    so callers do not treat a half-merged worktree as a successful sync.

    The auto-resolve loop is bounded by ``max_autoresolve_iterations``
    to defend against pathological cases where every replayed commit
    re-introduces an agent-outputs conflict — three iterations is
    plenty for a handful of housekeeping commits.

    Returns ``PushResult(ok=True)`` when the rebase finished cleanly
    (possibly after auto-resolve). On failure, the ``category`` names
    which part of the rebase went wrong (``reconcile_rebase_timeout``,
    ``reconcile_rebase_conflict``, ``reconcile_rebase_failed``).
    """
    rebase_cmd = _build_rebase_cmd(git_base, branch, base_branch)
    if rebase_cmd is None:
        # ``base_branch`` was supplied but ``origin/{base_branch}`` is not
        # resolvable in the worktree — most likely because the upstream
        # best-effort fetch silently failed.  Surface the failure rather
        # than fall back to the plain ``git rebase origin/{branch}`` form,
        # which would replay every commit between the stale ``origin/
        # {branch}`` tip and HEAD onto the stale tip — including upstream
        # main commits that landed since.  See #2222.
        _pkg.logger.error(
            "Push reconcile: origin/{base_branch} not resolvable — refusing unsafe rebase fallback",
            pipeline_id=pipeline_id,
            branch=branch,
            base_branch=base_branch,
        )
        return PushResult(
            ok=False,
            category="reconcile_base_unavailable",
            detail=(
                f"origin/{base_branch} could not be resolved in the worktree; "
                f"refusing the plain `git rebase origin/{branch}` fallback "
                "(would absorb upstream main commits — see #2222)"
            ),
        )
    try:
        rebase_result = subprocess.run(
            rebase_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        _pkg.logger.error(
            "Push reconcile: rebase timed out — aborting",
            pipeline_id=pipeline_id,
            branch=branch,
        )
        _abort_rebase_best_effort(git_base, pipeline_id, branch)
        return PushResult(
            ok=False,
            category="reconcile_rebase_timeout",
            detail=f"git rebase {' '.join(rebase_cmd[len(git_base) + 1 :])} timed out after 120s",
        )

    if rebase_result.returncode == 0:
        pop_conflict = _autostash_pop_conflict_result(git_base, pipeline_id, branch)
        if pop_conflict is not None:
            return pop_conflict
        return PushResult(ok=True)

    for iteration in range(max_autoresolve_iterations):
        unmerged_paths = _list_unmerged_paths(git_base)
        if not unmerged_paths:
            _pkg.logger.error(
                "Push reconcile: rebase stopped with no unmerged paths — aborting",
                pipeline_id=pipeline_id,
                branch=branch,
                stdout=rebase_result.stdout,
                stderr=rebase_result.stderr,
            )
            _abort_rebase_best_effort(git_base, pipeline_id, branch)
            return PushResult(
                ok=False,
                category="reconcile_rebase_failed",
                detail=(rebase_result.stderr or rebase_result.stdout or "").strip()
                or "rebase stopped with no unmerged paths",
            )

        non_ephemeral = [p for p in unmerged_paths if not p.startswith(".egg-state/agent-outputs/")]
        if non_ephemeral:
            _pkg.logger.error(
                "Push reconcile: rebase failed — conflicts outside agent-outputs, aborting",
                pipeline_id=pipeline_id,
                branch=branch,
                rebase_cmd=rebase_cmd[len(git_base) :],
                conflicting_paths=unmerged_paths,
                stdout=rebase_result.stdout,
                stderr=rebase_result.stderr,
            )
            _abort_rebase_best_effort(git_base, pipeline_id, branch)
            # Name the conflicting paths, the exact rebase form, and the
            # git output in the detail: this string is what reaches the
            # divergence-reconcile HITL decision, and #3416 showed that
            # an operator cannot judge the pause without it (the logs
            # roll; the decision persists).
            excerpt = _compact_git_output(rebase_result.stderr, rebase_result.stdout)
            detail = (
                f"conflicts outside .egg-state/agent-outputs/: {', '.join(non_ephemeral)} "
                f"[git {' '.join(rebase_cmd[len(git_base) :])}]"
            )
            if excerpt:
                detail = f"{detail}: {excerpt}"
            return PushResult(
                ok=False,
                category="reconcile_rebase_conflict",
                detail=detail,
            )

        _pkg.logger.warning(
            "Push reconcile: auto-resolving agent-outputs conflicts (taking remote)",
            pipeline_id=pipeline_id,
            branch=branch,
            resolved_paths=unmerged_paths,
            iteration=iteration + 1,
        )
        try:
            subprocess.run(
                [
                    *git_base,
                    "checkout",
                    "--theirs",
                    "--",
                    ".egg-state/agent-outputs",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            # Some paths may have been deleted on ``--theirs``; --all handles that.
            subprocess.run(
                [*git_base, "add", "--all", "--", ".egg-state/agent-outputs"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as resolve_err:
            _pkg.logger.error(
                "Push reconcile: auto-resolve failed — aborting rebase",
                pipeline_id=pipeline_id,
                branch=branch,
                error=str(resolve_err),
            )
            _abort_rebase_best_effort(git_base, pipeline_id, branch)
            return PushResult(
                ok=False,
                category="reconcile_rebase_failed",
                detail=f"agent-outputs auto-resolve failed: {resolve_err}",
            )

        # If resolution cleared the index, ``--continue`` errors with
        # "No changes - did you forget to use 'git add'?". Use --skip.
        diff_result = subprocess.run(
            [*git_base, "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        continue_cmd = "--skip" if diff_result.returncode == 0 else "--continue"

        # GIT_EDITOR=true suppresses editor prompt on --continue only.
        env = {**os.environ, "GIT_EDITOR": "true"} if continue_cmd == "--continue" else None
        try:
            rebase_result = subprocess.run(
                [*git_base, "rebase", continue_cmd],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
                env=env,
            )
        except subprocess.TimeoutExpired:
            _pkg.logger.error(
                "Push reconcile: rebase --continue timed out — aborting",
                pipeline_id=pipeline_id,
                branch=branch,
            )
            _abort_rebase_best_effort(git_base, pipeline_id, branch)
            return PushResult(
                ok=False,
                category="reconcile_rebase_timeout",
                detail=f"git rebase {continue_cmd} timed out after 120s",
            )

        if rebase_result.returncode == 0:
            pop_conflict = _autostash_pop_conflict_result(git_base, pipeline_id, branch)
            if pop_conflict is not None:
                return pop_conflict
            return PushResult(ok=True)

    _pkg.logger.error(
        "Push reconcile: rebase auto-resolve exceeded iteration limit — aborting",
        pipeline_id=pipeline_id,
        branch=branch,
        max_iterations=max_autoresolve_iterations,
    )
    _abort_rebase_best_effort(git_base, pipeline_id, branch)
    return PushResult(
        ok=False,
        category="reconcile_rebase_failed",
        detail=(f"agent-outputs auto-resolve exceeded {max_autoresolve_iterations} iterations"),
    )


def _autostash_pop_conflict_result(
    git_base: list[str],
    pipeline_id: str,
    branch: str,
) -> PushResult | None:
    """Detect a successful-rebase-with-conflicted-autostash-pop and
    return a failure ``PushResult`` for it; return ``None`` if the
    worktree is clean.

    ``git rebase --autostash`` exits 0 even when its final ``git stash
    pop`` of the autostash hits a conflict: the rebase itself succeeded,
    but the pop leaves ``UU`` entries in the worktree and the original
    autostash entry stays in ``git stash list``.  Without this check a
    half-merged worktree would be consumed as a successful sync by
    downstream code (#2714 review fallout).
    """
    unmerged = _list_unmerged_paths(git_base)
    if not unmerged:
        return None
    _pkg.logger.error(
        "Push reconcile: rebase succeeded but autostash pop produced conflicts",
        pipeline_id=pipeline_id,
        branch=branch,
        conflicting_paths=unmerged,
    )
    return PushResult(
        ok=False,
        category="reconcile_autostash_pop_conflict",
        detail=(
            "git rebase --autostash succeeded but the autostash pop "
            f"left unmerged paths in the worktree: {', '.join(unmerged)}; "
            "the autostash entry is preserved in `git stash list` for "
            "manual recovery"
        ),
    )


def _list_unmerged_paths(git_base: list[str]) -> list[str]:
    """Return the set of paths currently in a conflicted state in the worktree.

    Returns an empty list when the query itself fails — callers should be
    aware that ``[]`` can mean either "no conflicts" or "query failed".
    """
    result = subprocess.run(
        [*git_base, "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        _pkg.logger.warning(
            "_list_unmerged_paths: git diff --diff-filter=U failed",
            returncode=result.returncode,
            stderr=result.stderr,
        )
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _compact_git_output(*streams: str | None, limit: int = 400) -> str:
    """Collapse git stdout/stderr into a single-line excerpt for a detail string.

    Joins the non-empty lines of each stream with ``" | "`` and truncates
    to ``limit`` characters, so the failing rebase's own explanation
    (e.g. ``error: could not apply <sha>... <subject>`` and the
    ``CONFLICT (add/add)`` line) can travel inside a ``PushResult.detail``
    without flooding it.  ``hint:`` lines (git's resolve-it-yourself
    boilerplate) are dropped — they would crowd the informative lines out
    of the limit.
    """
    lines: list[str] = []
    for stream in streams:
        if not stream:
            continue
        lines.extend(
            stripped
            for line in stream.splitlines()
            if (stripped := line.strip()) and not stripped.startswith("hint:")
        )
    joined = " | ".join(lines)
    if len(joined) > limit:
        joined = joined[: limit - 1] + "…"
    return joined


def _build_rebase_cmd(
    git_base: list[str],
    branch: str,
    base_branch: str | None,
) -> list[str] | None:
    """Construct the ``git rebase`` argv for the push-reconcile path.

    Three cases:

    * ``base_branch`` is ``None`` (legacy callers that don't thread the
      pipeline's base): return the plain ``git rebase origin/{branch}``
      form.  This preserves pre-#1976 behaviour for any call site that
      still doesn't know the base branch.
    * ``base_branch`` is set and ``origin/{base_branch}`` resolves: return
      an ``--onto origin/{branch} <upstream>`` form.  The upstream is
      chosen by topology (see below) so the replay range contains only
      the commits that are genuinely local — never commits already on
      main (#1976) and never commits already shared with
      ``origin/{branch}`` (#3416).
    * ``base_branch`` is set but ``origin/{base_branch}`` does NOT resolve
      (the upstream best-effort fetch silently failed earlier, or rev-parse
      timed out): return ``None``.  The caller must surface this as a
      ``reconcile_base_unavailable`` failure rather than fall back to the
      plain form — that fallback is the contamination vector behind #2222.
      With HEAD at current main and ``origin/{branch}`` stuck on a stale
      snapshot, the plain form replays merge-base..HEAD (i.e. all the
      upstream main commits that landed since the stale snapshot) on top
      of the stale tip, producing a PR full of duplicate-by-content
      commits with rewritten SHAs.

    Upstream selection (#3416): ``--onto origin/{branch}
    origin/{base_branch}`` replays ``origin/{base_branch}..HEAD`` — on a
    long-lived pipeline worktree that range is the ENTIRE pipeline
    history since main, not just the local-only commits.  Git tolerates
    re-applying the already-shared commits only while each one merges
    empty against the new tip; a shared commit whose paths were later
    rewritten on the shared lineage (the ``.egg-state/contracts/*.json``
    statefile is rewritten on every contract mutation) instead produces
    a guaranteed add/add or content conflict — a false-positive
    "unreconcilable divergence" for commits both sides already have.
    So when ``merge-base(HEAD, origin/{branch})`` resolves and is NOT an
    ancestor of ``origin/{base_branch}`` — i.e. the shared history
    extends past the branch point onto the pipeline lineage, the
    long-lived-worktree topology — use ``--onto origin/{branch}
    <merge-base>``, which replays exactly the local-only commits.  When
    the merge-base IS on the main lineage (fresh worktree cut straight
    from main — the #1976/#2222 topology, where ``merge-base..HEAD``
    would include upstream main commits), keep the
    ``origin/{base_branch}`` upstream.  If the merge-base cannot be
    determined, fall back to ``origin/{base_branch}`` (pre-#3416
    behaviour).

    ``--autostash`` is set on every returned form (#2714): the orchestrator
    writes statefile / agent-output deltas continuously and does not commit
    them eagerly, so the worktree is routinely dirty at sync time.  Without
    autostash, ``git rebase`` refuses with ``cannot rebase: You have
    unstaged changes`` and the divergence-reconcile path that #2352 added
    fails 100% of the time on the plan-complete sync.  With autostash, git
    stashes the unstaged delta before the rebase and pops it on success
    (or on abort: ``git rebase --abort`` automatically reapplies the
    autostash to the working tree, and only preserves the stash entry in
    ``git stash list`` as a fallback if that reapply itself conflicts).
    The successful-pop path is itself not conflict-free — when the pop
    collides with the rebased state, git rebase still exits 0 but leaves
    ``UU`` entries in the worktree.  The caller
    (``_rebase_with_agent_output_autoresolve``) detects that case and
    surfaces ``reconcile_autostash_pop_conflict`` so a half-merged
    worktree is never consumed as a successful sync.
    """
    if base_branch is None:
        return [*git_base, "rebase", "--autostash", f"origin/{branch}"]

    base_ref = f"origin/{base_branch}"
    try:
        verify = subprocess.run(
            [*git_base, "rev-parse", "--verify", base_ref],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        verify = None
    if verify and verify.returncode == 0:
        upstream = _divergence_replay_upstream(git_base, branch, base_ref) or base_ref
        return [*git_base, "rebase", "--autostash", "--onto", f"origin/{branch}", upstream]
    return None


def _divergence_replay_upstream(
    git_base: list[str],
    branch: str,
    base_ref: str,
) -> str | None:
    """Pick the rebase upstream that replays only local-only commits (#3416).

    Returns ``merge-base(HEAD, origin/{branch})`` when it resolves and is
    NOT an ancestor of ``base_ref`` — the long-lived-worktree topology,
    where that merge-base is a shared pipeline commit and
    ``<merge-base>..HEAD`` is exactly the local-only commit set.  Returns
    ``None`` (caller keeps the ``base_ref`` upstream) when the merge-base
    is on the main lineage (#1976/#2222 fresh-worktree topology) or
    cannot be determined.
    """
    try:
        merge_base = subprocess.run(
            [*git_base, "merge-base", "HEAD", f"origin/{branch}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None
    merge_base_sha = merge_base.stdout.strip()
    if merge_base.returncode != 0 or not merge_base_sha:
        return None
    try:
        is_ancestor = subprocess.run(
            [*git_base, "merge-base", "--is-ancestor", merge_base_sha, base_ref],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None
    # rc 0 → the merge-base is on the main lineage: HEAD sits directly on
    # (a snapshot of) main, so the caller must keep the ``base_ref``
    # upstream to exclude upstream main commits from the replay (#1976).
    # rc 1 → the merge-base is a shared pipeline commit: replaying from it
    # yields exactly the local-only commits.  Other rcs are git errors —
    # be conservative and keep the pre-#3416 upstream.
    if is_ancestor.returncode == 1:
        return merge_base_sha
    return None


def _abort_rebase_best_effort(
    git_base: list[str],
    pipeline_id: str,
    branch: str,
) -> None:
    """Run ``git rebase --abort`` and swallow any failure (worktree is junk anyway)."""
    try:
        subprocess.run(
            [*git_base, "rebase", "--abort"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except Exception:
        _pkg.logger.warning(
            "Push reconcile: rebase --abort also failed",
            pipeline_id=pipeline_id,
            branch=branch,
        )
