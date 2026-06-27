"""create_slice_integration_branch -- slice integration-branch lifecycle (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

from typing import Literal

import gateway_client as _pkg


def create_slice_integration_branch(
    self,
    pipeline_id: str,
    repo_path: str,
    *,
    integration_branch: str,
    parent_branch: str,
    integration_base_sha: str | None = None,
    agent_role: str = "coder",
    mode: Literal["public", "private"] = "public",
) -> str | None:
    """Create the slice integration branch on origin from ``parent_branch``.

    On success returns the fork-base SHA the integration branch was
    pushed at (the resolved ``parent_sha``) — exactly the value the
    caller records as :attr:`Slice.integration_base_sha` (#2871).
    Returning it here lets the caller persist the fork base from the
    same gateway call that created the branch, with no extra
    ``get_remote_branch_sha`` round-trip whose best-effort failure
    previously left ``integration_base_sha`` unset and armed the
    #3185 empty-branch trap (an un-started branch later misclassified
    as merged by ancestor-only detection). ``None`` on any failure
    (the caller logs and surfaces a clear error to the run loop). The
    #2512 / #2947 recovery short-circuits also return ``parent_sha``:
    they only fire when the branch already exists, which means a
    prior run already recorded its base, so the caller's
    "record only when unset" guard ignores the returned value there.

    Pushes ``<parent_sha>:refs/heads/<integration_branch>`` via a
    synthetic, launcher-authenticated session through
    ``/api/v1/git/push``.  The parent SHA is resolved by querying
    origin directly (``git ls-remote``); pushing an explicit SHA on
    the source side avoids relying on local ref-name resolution in
    the orchestrator's per-pipeline worktree, which is checked out
    on ``<branch>/work`` and does NOT carry a local ref matching
    ``<parent_branch>`` (only ``refs/remotes/origin/<parent_branch>``
    after a fetch — #2393).

    The gateway treats this push as orchestrator infrastructure: the
    synthetic flag (only settable by ``/api/v1/sessions/create``,
    which is gated on the launcher secret) combined with the slice
    integration-branch name ``egg/<base>/(slice|phase)-N``
    short-circuits the pipeline-session push block from #2028 — see
    the ``_SLICE_INTEGRATION_BRANCH_RE`` exemption in
    ``gateway/gateway.py``.  The branch itself still passes the
    normal ``egg/`` prefix branch-ownership check, so no
    orchestrator-role push surface is introduced.

    ``integration_base_sha`` is the fork base recorded at the
    slice's creation (#2871). When supplied it unlocks the #2947
    resume-in-place path below: a crash / ``restart_phase`` that
    lands while the slice already has committed work *and* the
    parent advanced additively is recognised as a resumable branch
    (rather than non-fast-forward-rejected) by checking that the
    recorded base is still an ancestor of both the existing tip and
    the advanced parent. ``None`` (slices provisioned before #2871,
    or whose base was never recorded) does not gate this fast-path.

    When that recorded-base fast-path does not fire — base absent, or
    an out-of-band actor (restart/salvage/manual edit) rewrote it so
    it no longer descends to the slice tip (#3245) — the method falls
    back to re-deriving the fork point from git (``merge-base`` of the
    existing tip and the advanced parent). A genuine shared fork point
    that is strictly behind the existing tip means the branch is a
    resumable additive fork: it is adopted in place rather than
    non-fast-forward-rejected. Only a branch with no shared history,
    or one sitting at/behind the fork with no own commits, still falls
    through to the push (surfacing the rejection / fast-forwarding).

    Returns the fork-base SHA (``parent_sha``) on success, ``None``
    on any error (the caller logs and surfaces a clear error to the
    run loop).
    """
    if not integration_branch or not parent_branch:
        return None
    if integration_branch == parent_branch:
        # No-op: integration branch already exists at parent's tip.
        # ``parent_sha`` is not resolved on this no-op path (no
        # round-trip has happened yet), so return an empty string
        # rather than a misleading None — the caller's "record only
        # when unset" guard skips recording here anyway because the
        # branch already existing means a prior run recorded its base.
        return ""

    # Register one synthetic session up front and share it across
    # the fetch, ls-remote, and push (#2398).  The session is
    # tagged for the push (branch=integration_branch + agent_role
    # — required for the gateway's slice integration-branch
    # exemption and branch-ownership check); the fetch and
    # ls-remote endpoints accept any synthetic session, so the
    # extra metadata is harmless for those calls.
    temp_container_id = f"{pipeline_id}-slice-branch-{integration_branch.replace('/', '-')}"
    parent_sha: str | None = None
    session_token: str | None = None
    try:
        session = self.register_session(
            container_id=temp_container_id,
            container_ip=self.self_ip,
            mode=mode,
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            branch=integration_branch,
            synthetic=True,
            # #2869 — ride out a transient DNS/connection blip rather
            # than hard-failing the slice (and cascading to the whole
            # phase) before any agent is spawned.
            retry_transient=True,
        )
        session_token = session.session_token

        # Refresh the local remote-tracking ref + odb so the
        # parent's commit object is available for the push below.
        # ``git push <sha>:refs/heads/...`` requires the source
        # object to be locally reachable; the fetch makes that
        # true even when the worktree was just created and has
        # never seen this ref.  Best-effort: if it fails the
        # object may already be local from a prior step, so we
        # still attempt the ls-remote / push.
        self.fetch_branch(
            pipeline_id,
            repo_path,
            args=[f"+refs/heads/{parent_branch}:refs/remotes/origin/{parent_branch}"],
            mode=mode,
            bearer_token=session_token,
            retry_transient=True,
        )

        # Resolve the parent to a SHA on origin.  Failing fast
        # here produces a clear "parent not found" error instead
        # of git's confusing ``src refspec X does not match any``.
        parent_sha = self.get_remote_branch_sha(
            pipeline_id,
            repo_path,
            f"refs/heads/{parent_branch}",
            mode=mode,
            bearer_token=session_token,
            retry_transient=True,
        )
        if not parent_sha:
            _pkg.logger.warning(
                "Parent branch not found on origin; cannot create slice integration branch",
                pipeline_id=pipeline_id,
                integration_branch=integration_branch,
                parent_branch=parent_branch,
            )
            return None

        # #2512 — restart_phase recovery: if the slice integration
        # branch already exists on origin with commits descended
        # from the current parent tip, preserve them and short-
        # circuit success.  This happens when a pipeline is
        # cancelled mid-implement-phase (cleanup=false) and then
        # restarted: the prior run's per-role commits live on
        # the slice integration branch.  Naively pushing
        # ``parent_sha:refs/heads/<integration_branch>`` against
        # that tip is non-fast-forward and gets rejected by
        # origin, which previously killed the slice (and cascaded
        # to the whole phase) before any agent was spawned.
        # decision-3's "Committed work is preserved on retry"
        # wording is honored only if we treat that case as
        # success rather than silent failure.
        existing_sha = self.get_remote_branch_sha(
            pipeline_id,
            repo_path,
            f"refs/heads/{integration_branch}",
            mode=mode,
            bearer_token=session_token,
            retry_transient=True,
        )
        if existing_sha and existing_sha != parent_sha:
            # Fetch the integration branch so its tip object is
            # in the local odb for the merge-base check below
            # (parent_sha was made local by the earlier
            # fetch_branch on parent_branch).  Best-effort: if
            # this fetch fails (gateway down, transient network,
            # expired session), the existing tip won't be in the
            # local odb and ``_sha_is_ancestor`` will return
            # False, so we'll degrade to the original push-and-
            # hope behaviour rather than mistakenly preserving
            # prior work on an unverifiable check.  See the
            # softened fall-through warning below.
            self.fetch_branch(
                pipeline_id,
                repo_path,
                args=[f"+refs/heads/{integration_branch}:refs/remotes/origin/{integration_branch}"],
                mode=mode,
                bearer_token=session_token,
                retry_transient=True,
            )
            if self._sha_is_ancestor(
                pipeline_id,
                repo_path,
                parent_sha,
                existing_sha,
                bearer_token=session_token,
            ):
                _pkg.logger.info(
                    "Slice integration branch already exists "
                    "with commits descended from parent — "
                    "preserving prior work (#2512 restart recovery)",
                    pipeline_id=pipeline_id,
                    integration_branch=integration_branch,
                    parent_branch=parent_branch,
                    parent_sha=parent_sha,
                    existing_sha=existing_sha,
                )
                # The branch already existed with commits descended from
                # ``parent_sha``, so ``parent_sha`` *is* the fork
                # base (the branch was created at it and advanced).
                # For post-#2871 contracts a prior run already
                # recorded it, so the caller's "record only when
                # unset" guard no-ops; for a pre-#2871 contract
                # this return value backfills the field correctly.
                # Either way ``parent_sha`` is the right value.
                return parent_sha
            # #2947 — crash / restart mid-slice resume-in-place. The
            # existing tip is NOT a descendant of the (advanced)
            # parent, so the #2512 fast-path above did not fire — but
            # that does not necessarily mean diverged/unknown history.
            # When a host crash or ``restart_phase`` lands while the
            # slice already has its own committed work AND the parent
            # moved forward *additively* (no history rewrite), the
            # slice is a legitimate, resumable branch that simply has
            # not picked up the parent's new commits yet. The #2914
            # "treat as fresh to force re-spawn" path then routes it
            # back through here, and a plain
            # ``parent_sha:refs/heads/<int>`` push would be
            # non-fast-forward — failing the slice and cascading the
            # whole phase (the exact 2026-06-02 issue-2908-impl2
            # slice-3 incident).
            #
            # We can recognise this case precisely using the fork
            # base recorded at creation (#2871): if the slice's own
            # recorded base is a *strict* ancestor of the existing tip
            # (the branch genuinely carries this slice's commits built
            # on its base) AND that base is also an ancestor of the
            # advanced parent (the parent moved forward without
            # rewriting the base out of its history), then the slice
            # and the parent differ only by additive commits on each
            # side of a shared base. Preserve the branch as-is and
            # short-circuit success: the cohort re-spawns onto the
            # existing tip, and the parent's new commits reconcile at
            # slice-PR / merge time exactly as they would for a slice
            # whose parent advanced while it was running normally. We
            # never reset or force-push, so no committed work is
            # destroyed.
            #
            # Gating on the slice's *own* recorded base (not a generic
            # merge-base of the two tips) is what keeps this *fast-path*
            # safe: a genuinely unrelated stale branch would not descend
            # from this slice's recorded base, so it still falls through
            # to the push and surfaces the rejection (preserving the
            # #2512/#2549 "don't silently overwrite unknown work"
            # instinct). NOTE (#3245): the recorded-base invariant
            # described here governs *this* check only. The #3245
            # fall-through immediately below deliberately relaxes it —
            # when the recorded base is untrusted/absent it re-derives a
            # *generic* merge-base of the two tips and adopts on that.
            # That widens adoption to the parent-rewrite class and any
            # branch sharing some ancestor with the parent; it is safe
            # because the slice branch name is slice-specific and
            # gateway-restricted, adoption stays non-destructive, and a
            # genuinely-unexpected branch surfaces as a CONFLICTING slice
            # PR for a human rather than cascade-failing the phase. Do
            # not read the "we gate on our own base" claim above as a
            # whole-function invariant — see the #3245 block below.
            # A true history rewrite of the parent (rebase)
            # drops the base out of the parent's ancestry, so the
            # second check fails and we likewise fall through — that
            # harder class is deliberately left to the operator. The
            # ``existing_sha != integration_base_sha`` guard keeps an
            # *un-started* branch still sitting at its base on the
            # fast-forward push path (advancing it to the new parent
            # tip) rather than pinning it to the stale base.
            # ``existing_sha`` and ``integration_base_sha`` both
            # originate from ``get_remote_branch_sha`` (full 40-char
            # SHAs), so an exact compare is correct (same invariant as
            # the #2871 un-started-branch guard in
            # ``is_slice_branch_merged_into_parent``).
            if (
                integration_base_sha
                and existing_sha != integration_base_sha
                and self._sha_is_ancestor(
                    pipeline_id,
                    repo_path,
                    integration_base_sha,
                    existing_sha,
                    bearer_token=session_token,
                )
                and self._sha_is_ancestor(
                    pipeline_id,
                    repo_path,
                    integration_base_sha,
                    parent_sha,
                    bearer_token=session_token,
                )
            ):
                _pkg.logger.info(
                    "Slice integration branch carries its own commits "
                    "and the parent advanced additively since creation "
                    "— preserving the branch and resuming in place "
                    "(#2947 crash/restart recovery)",
                    pipeline_id=pipeline_id,
                    integration_branch=integration_branch,
                    parent_branch=parent_branch,
                    parent_sha=parent_sha,
                    existing_sha=existing_sha,
                    integration_base_sha=integration_base_sha,
                )
                # This path is gated on ``integration_base_sha`` being
                # supplied, so the caller already has the recorded
                # base (``recorded_base_sha`` is not None) and skips
                # recording this return value. Return ``parent_sha``
                # so the caller treats the call as success.
                return parent_sha
            # #3245 — durable resume-in-place when the *recorded* base
            # cannot be trusted. The #2947 fast-path above gates
            # resume-in-place on ``integration_base_sha``, but that
            # stored SHA is mutable by out-of-band actors: a
            # ``restart_phase`` slice copy, a ``salvage_agent_commits``
            # run, or a manual contract edit can rewrite it — in the
            # observed incident (issue-3200 slice-7) it was overwritten
            # to the *advanced parent tip*. A base that no longer is an
            # ancestor of the slice's own tip fails the #2947 check, and
            # we would otherwise fall through to a ``parent_sha`` push
            # that is non-fast-forward → FAILS the slice → cascades the
            # whole phase, even though the branch is a perfectly
            # resumable additive fork whose committed work is intact.
            #
            # Re-derive the fork point straight from git instead of
            # trusting the stored SHA: the merge-base of the existing
            # tip and the advanced parent. We reach here only when the
            # parent is NOT an ancestor of the existing tip (the #2512
            # fast-forward path already returned above), so a real
            # shared merge-base that is *strictly behind* the existing
            # tip means the two have DIVERGED from a common fork point
            # — i.e. the slice forked from the parent's lineage and
            # carries its own commits while the parent advanced. Adopt
            # the branch as-is and resume in place; the parent's new
            # commits reconcile at slice-PR / merge time exactly as for
            # a slice whose parent advanced while it ran normally. We
            # never reset or force-push, so no committed work is
            # destroyed. The re-derivation is idempotent, so a contract
            # still carrying the corrupted base self-heals on every
            # subsequent resume.
            #
            # Return value: ``fork_base`` (the re-derived true base), not
            # ``parent_sha``. When the recorded base was *corrupted* the
            # caller's ``recorded_base_sha is None`` write-back guard is
            # already satisfied (a stored — if wrong — base exists) and
            # skips the write, so the return value is inert there. But
            # this path *also* fires when ``integration_base_sha`` was
            # absent (the #2947 ``integration_base_sha and …`` short-
            # circuit drops through to here): then ``recorded_base_sha is
            # None`` and the caller *does* persist whatever we return. We
            # return the true ``fork_base`` so it records the real base
            # rather than the advanced parent tip — which would itself be
            # exactly the corruption shape this path exists to tolerate,
            # and would pin the slice on the slower re-derivation route
            # forever. Recording ``fork_base`` instead lets the cheaper
            # #2947 fast-path fire on the next resume.
            #
            # Safety: ``fork_base is None`` (no shared history at all —
            # an unrelated/orphan branch sitting at this name) and
            # ``fork_base == existing_sha`` (the branch is *behind* the
            # parent with no unique commits — an un-started branch that
            # should fast-forward, not resume) both fall through to the
            # push below, preserving the "don't adopt unknown work /
            # do fast-forward an empty branch" instincts of #2512/#2947.
            fork_base = self.merge_base(
                pipeline_id,
                repo_path,
                existing_sha,
                parent_sha,
                bearer_token=session_token,
                mode=mode,
            )
            if fork_base and fork_base != existing_sha:
                _pkg.logger.info(
                    "Slice integration branch shares a fork point with "
                    "the advanced parent and carries its own commits — "
                    "adopting it and resuming in place via runtime "
                    "merge-base re-derivation (#3245; recorded base "
                    "untrusted/stale)",
                    pipeline_id=pipeline_id,
                    integration_branch=integration_branch,
                    parent_branch=parent_branch,
                    parent_sha=parent_sha,
                    existing_sha=existing_sha,
                    fork_base=fork_base,
                    recorded_integration_base_sha=integration_base_sha,
                )
                return fork_base

            # Could not verify ancestry: parent_sha is either not
            # reachable from the existing tip (genuinely diverged
            # history) or the merge-base call itself failed
            # (gateway down, missing object after a failed
            # integration-branch fetch, expired session).  The
            # inner warning emitted by ``_sha_is_ancestor`` for
            # the second case captures the true cause; the outer
            # message stays deliberately neutral so an operator
            # triaging "why was my slice rejected?" doesn't latch
            # onto "diverged history" when the real failure was
            # an unverifiable check.  Either way: fall through
            # to the push so the rejection surfaces rather than
            # silently overwriting unknown work.
            _pkg.logger.warning(
                "Could not verify that parent is an ancestor of "
                "the existing slice integration tip; push may be "
                "rejected as non-fast-forward",
                pipeline_id=pipeline_id,
                integration_branch=integration_branch,
                parent_sha=parent_sha,
                existing_sha=existing_sha,
            )

        refspec = f"{parent_sha}:refs/heads/{integration_branch}"
        self._retry_transient(
            lambda: self._make_request(
                "/api/v1/git/push",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
                    "refspec": refspec,
                },
                bearer_token=session_token,
            ),
            operation="push integration branch",
        )
        _pkg.logger.info(
            "Created slice integration branch",
            pipeline_id=pipeline_id,
            integration_branch=integration_branch,
            parent_branch=parent_branch,
            parent_sha=parent_sha,
        )
        # #3185 — return the fork base the branch was pushed at so
        # the caller records ``integration_base_sha`` from this call
        # with no extra round-trip. The previous best-effort re-fetch
        # could silently fail (no ``retry_transient``) and leave the
        # field unset, arming the empty-branch trap.
        return parent_sha
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "Failed to create slice integration branch",
            pipeline_id=pipeline_id,
            integration_branch=integration_branch,
            parent_branch=parent_branch,
            parent_sha=parent_sha,
            error=str(exc),
        )
        return None
    finally:
        if session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass
