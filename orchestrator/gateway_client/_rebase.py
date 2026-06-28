"""rebase_onto + canonical rebase --onto argv builder (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

import re
from typing import Literal

import gateway_client as _pkg

_REBASE_REF_RE = re.compile(r"^[A-Za-z0-9._/+-][A-Za-z0-9._/+-]*$")


def rebase_onto(
    self,
    pipeline_id: str,
    repo_path: str,
    *,
    branch: str,
    new_base: str,
    old_base: str,
    pr_number: int | None = None,
    repo: str | None = None,
    agent_role: str = "coder",
    mode: Literal["public", "private"] = "public",
) -> bool:
    """Heal an orphaned stacked PR end-to-end.

    Three steps, in order — any failure short-circuits and
    returns ``False`` so the reconciler counts it as
    ``rebases_failed`` and retries on the next tick:

    1. ``git rebase --onto <new_base> <old_base> <branch>``
       (via the existing per-agent ``/api/v1/git/execute``
       endpoint and the canonical argv from the local
       :func:`_build_rebase_onto_args` helper — which mirrors
       ``gateway.git_client.build_rebase_onto_args`` so the
       orchestrator image does not need ``gateway/`` on its
       Python path).
    2. ``git push --force-with-lease origin <branch>``
       (via the existing per-agent ``/api/v1/git/push``
       endpoint) — propagates the rewritten history to origin
       so the open PR's head ref reflects the rebase. Without
       this step the local rebase is invisible to GitHub and
       the orphan remains. The ``consensus_push=true`` marker
       is set so the gateway's pipeline-push enforcement
       accepts the request — defense-in-depth lives in the
       push-target enforcement, which still requires
       ``branch == session.assigned_branch``.
    3. ``gh api repos/<repo>/pulls/<pr_number> -X PATCH -f
       base=<new_base>`` (via the existing per-agent
       ``/api/v1/gh/pr/edit`` endpoint) — retargets the PR's
       base on GitHub so the diff renders against the new
       parent. Skipped when ``pr_number`` / ``repo`` are not
       supplied (callers without PR context just want the
       local rebase + push).

    No new privileged orchestrator-role endpoint is introduced
    (refine-phase decision-15) — every step routes through the
    same per-agent allowlists already in production.

    Returns ``True`` only when every applicable step succeeded.
    Returns ``False`` on argument validation failure, push
    failure, retarget failure, or any HTTP error. The
    reconciler counts both ``False`` and exceptions as
    ``rebases_failed``.
    """
    args, ok, err = _build_rebase_onto_args(branch, new_base, old_base)
    if not ok:
        _pkg.logger.warning(
            "rebase_onto: argv rejected by allowlist validator",
            pipeline_id=pipeline_id,
            branch=branch,
            new_base=new_base,
            old_base=old_base,
            error=err,
        )
        return False

    # Validate retarget inputs early — if the caller asked for
    # PR retargeting, we want to fail fast rather than rebase +
    # push and then discover the PR number was bogus.
    retarget_requested = pr_number is not None or bool(repo)
    if retarget_requested:
        if (
            pr_number is None
            or isinstance(pr_number, bool)
            or not isinstance(pr_number, int)
            or pr_number <= 0
        ):
            _pkg.logger.warning(
                "rebase_onto: pr_number must be a positive int when retargeting",
                pipeline_id=pipeline_id,
                branch=branch,
                pr_number=pr_number,
            )
            return False
        if not repo or not isinstance(repo, str):
            _pkg.logger.warning(
                "rebase_onto: repo must be 'owner/name' when retargeting",
                pipeline_id=pipeline_id,
                branch=branch,
                repo=repo,
            )
            return False

    temp_container_id = f"{pipeline_id}-stacked-pr-rebase"
    session_token: str | None = None
    try:
        # The session's ``assigned_branch`` is set to the slice's
        # integration branch when retargeting so the gateway's
        # push-target enforcement (``branch ==
        # session.assigned_branch``) accepts the push step. The
        # legacy local-only path uses ``branch=None`` because no
        # push is issued.
        session = self.register_session(
            container_id=temp_container_id,
            container_ip=self.self_ip,
            mode=mode,
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            branch=branch if retarget_requested else None,
            synthetic=True,
        )
        session_token = session.session_token

        # Step 1: local rebase via /api/v1/git/execute (the
        # gateway's git-command surface; ``/api/v1/git`` is not a
        # registered route).
        self._make_request(
            "/api/v1/git/execute",
            method="POST",
            data={
                "operation": "rebase",
                "args": args,
                "repo_path": repo_path,
            },
            bearer_token=session_token,
        )

        # If the caller didn't ask for the full heal (push +
        # retarget), preserve the legacy local-only behaviour.
        if not retarget_requested:
            return True

        # Step 2: push --force-with-lease so origin sees the
        # rebased history. The reconciler is the only writer of
        # this branch; force-with-lease catches the rare case of
        # a concurrent push from elsewhere and refuses rather
        # than clobbering it.
        #
        # ``consensus_push=true`` short-circuits the gateway's
        # pipeline-push enforcement (the session has a
        # ``pipeline_id`` so a bare push would be rejected with
        # 403). The defense-in-depth surface still applies — the
        # push-target check requires ``branch ==
        # session.assigned_branch`` (set above) and branch
        # ownership, fork-policy, and force-with-lease together
        # bound the blast radius.
        self._make_request(
            "/api/v1/git/push",
            method="POST",
            data={
                "repo_path": repo_path,
                "remote": "origin",
                "refspec": f"{branch}:refs/heads/{branch}",
                "mode": mode,
                "force_with_lease": True,
                "consensus_push": True,
            },
            bearer_token=session_token,
        )

        # Step 3: retarget the PR's base on GitHub.
        self._make_request(
            "/api/v1/gh/pr/edit",
            method="POST",
            data={
                "repo": repo,
                "pr_number": int(pr_number),  # type: ignore[arg-type]
                "base": new_base,
            },
            bearer_token=session_token,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        # ``str(exc)`` is the gateway's flattened message (e.g. the
        # opaque ``"git rebase failed"`` for a non-zero git exit).
        # The real git stderr — which distinguishes a dirty-worktree
        # refusal from a genuine content conflict from a transport
        # error — rides in ``GatewayError.details`` (the gateway puts
        # ``{stdout, stderr, returncode}`` there). Surface it so an
        # operator can tell a recoverable mechanism failure apart from
        # a real conflict without spelunking the gateway pod (#3245).
        raw_details = getattr(exc, "details", None)
        details = raw_details if isinstance(raw_details, dict) else {}
        _pkg.logger.warning(
            "rebase_onto: gateway request failed",
            pipeline_id=pipeline_id,
            branch=branch,
            error=str(exc),
            git_stderr=(details.get("stderr") or "").strip() or None,
            git_returncode=details.get("returncode"),
        )
        return False
    finally:
        if session_token:
            try:
                self.delete_session(session_token)
            except Exception as exc:  # noqa: BLE001
                _pkg.logger.debug(
                    "rebase_onto: session cleanup failed",
                    pipeline_id=pipeline_id,
                    error=str(exc),
                )


def _build_rebase_onto_args(
    branch: str, new_base: str, old_base: str
) -> tuple[list[str], bool, str]:
    """Construct the canonical ``rebase --onto`` argv for the reconciler.

    Performs the same ref-shape sanity checks as
    :func:`gateway.git_client.build_rebase_onto_args` (reject empty,
    flag-shaped, whitespace-bearing, or non-git-ref-shaped inputs) but
    lives in the orchestrator package so the deployed orchestrator image
    (which does not ship ``gateway/``) can build the argv without an
    import-time dependency on the gateway code. Two intentional
    differences from the gateway helper:

    - The argv is emitted with each ref *stripped*, so leading/trailing
      whitespace that the regex would otherwise reject as the input is
      normalised before the gateway round-trip.
    - This helper does NOT call ``gateway.git_client.validate_git_args``
      — pulling that import in would defeat the point of inlining. The
      gateway server's ``/git`` endpoint runs the same allowlist
      validator on every submission, so the security floor is unchanged
      (audit boundary is the server, not the client-side helper).
    """
    if not isinstance(branch, str) or not branch.strip():
        return [], False, "branch must be a non-empty string"
    if not isinstance(new_base, str) or not new_base.strip():
        return [], False, "new_base must be a non-empty string"
    if not isinstance(old_base, str) or not old_base.strip():
        return [], False, "old_base must be a non-empty string"

    for label, value in (("branch", branch), ("new_base", new_base), ("old_base", old_base)):
        v = value.strip()
        if v.startswith("-"):
            return [], False, f"{label} must not start with '-' (rejected flag-shaped ref: {v!r})"
        if any(ch.isspace() or ch == "\x00" for ch in v):
            return (
                [],
                False,
                f"{label} must not contain whitespace or NUL (rejected: {v!r})",
            )
        if not _REBASE_REF_RE.fullmatch(v):
            return (
                [],
                False,
                f"{label} must look like a git ref (alnum + . _ / + -); got {v!r}",
            )

    # ``--autostash`` is prepended so the rebase proceeds against a
    # worktree carrying uncommitted ``.egg-state/agent-outputs/`` residue
    # (BRC memory writes left uncommitted because post-agent auto-commit
    # is disabled). Without it ``git rebase`` refuses with ``cannot
    # rebase: You have unstaged changes`` even on conflict-free content,
    # surfacing as the opaque ``"git rebase failed"`` (#3245) — the same
    # refusal #2714 fixed on the push-reconcile rebase path. The gateway
    # ``rebase`` allowlist permits ``--autostash`` (gateway/git_client.py),
    # and the base-branch rebase guard (gateway.py) ignores it (it is
    # neither a positional nor an ``--onto`` value), so the protected-ref
    # check on ``new_base`` is unaffected.
    return (
        ["--autostash", "--onto", new_base.strip(), old_base.strip(), branch.strip()],
        True,
        "",
    )
