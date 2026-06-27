"""merge-base / ancestry / slice-merge + evidence-reachability checks (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

import re
from collections.abc import Sequence
from typing import Literal

import gateway_client as _pkg
from gateway_client import GatewayError

# Full 40-char hex SHA -- ``git merge-base`` always returns the full SHA.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def merge_base(
    self,
    pipeline_id: str,
    repo_path: str,
    ref_a: str,
    ref_b: str,
    *,
    bearer_token: str | None = None,
    mode: Literal["public", "private"] = "public",
) -> str | None:
    """Return the merge-base SHA of ``ref_a`` and ``ref_b`` (or None).

    Runs ``git merge-base ref_a ref_b`` through
    ``/api/v1/git/execute`` and parses the stdout SHA from the
    gateway response. Both refs must already be locally reachable
    in the worktree's odb — the caller is responsible for any
    prior fetches.

    Returns ``None`` when:

    * Either ref does not exist locally (``git merge-base``
      exits non-zero with returncode 1).
    * The two refs share no common ancestor (also returncode 1).
    * The gateway request itself fails (network, missing object,
      policy denial).

    Auth: ``/api/v1/git/execute`` is ``@require_session_auth``;
    when ``bearer_token`` is ``None`` we self-bootstrap a
    short-lived synthetic launcher-authenticated session (same
    pattern as :meth:`fetch_branch`, :meth:`ls_remote_branch`,
    :meth:`get_remote_branch_sha`) and tear it down in a
    ``finally``. Callers that already hold a session for the
    ambient slice/pipeline pass their token through ``bearer_token``
    to avoid a redundant register/delete round-trip.

    General ancestry/fork-point primitive. (Slice-4 TASK-4-3
    once wired this into ``_resolve_slice_base_branch`` to
    validate a slice's fork point, but #2928 replaced that with a
    parent-branch-existence probe — probing the slice's own
    not-yet-created integration branch mis-based fresh slices. The
    method is retained as a general gateway utility.)
    """
    if not ref_a or not ref_b:
        return None
    owns_session = bearer_token is None
    session_token: str | None = bearer_token
    try:
        if owns_session:
            # Mirror fetch_branch / ls_remote_branch — register a
            # synthetic launcher-authenticated session so the
            # ``@require_session_auth`` endpoint accepts the call.
            # Without this self-bootstrap the merge-base probe is
            # a silent no-op (401 → GatewayError with returncode
            # None → return None → resolver mis-routes to
            # pipeline_branch).
            temp_container_id = f"{pipeline_id}-merge-base-probe"
            session = self.register_session(
                container_id=temp_container_id,
                container_ip=self.self_ip,
                mode=mode,
                pipeline_id=pipeline_id,
                synthetic=True,
            )
            session_token = session.session_token

        try:
            result = self._make_request(
                "/api/v1/git/execute",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "operation": "merge-base",
                    "args": [ref_a, ref_b],
                },
                bearer_token=session_token,
            )
        except GatewayError as exc:
            details = exc.details or {}
            returncode = details.get("returncode")
            if returncode != 1:
                _pkg.logger.warning(
                    "merge-base failed unexpectedly",
                    pipeline_id=pipeline_id,
                    ref_a=ref_a,
                    ref_b=ref_b,
                    returncode=returncode,
                    error=str(exc),
                )
            return None
        stdout = (result or {}).get("data", {}).get("stdout", "")
        if not stdout:
            return None
        sha = stdout.strip().split("\n", 1)[0].strip()
        # Strict 40-char hex SHA shape check — the gateway returns
        # the raw ``git merge-base`` output, which is always a full
        # 40-char SHA on success. Anything shorter / longer / non-hex
        # is treated as "no fork point" rather than risking a
        # malformed value being passed downstream.
        if not _FULL_SHA_RE.fullmatch(sha):
            return None
        return sha
    finally:
        if owns_session and session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass


def _sha_is_ancestor(
    self,
    pipeline_id: str,
    repo_path: str,
    ancestor_sha: str,
    descendant_sha: str,
    *,
    bearer_token: str | None = None,
) -> bool:
    """Return True iff ``ancestor_sha`` is an ancestor of ``descendant_sha``.

    Runs ``git merge-base --is-ancestor <ancestor> <descendant>``
    through ``/api/v1/git/execute``.  Both SHAs must already be
    reachable in the local odb — the caller is responsible for
    any prior fetches.

    ``git merge-base --is-ancestor`` exits 0 when the relation
    holds and 1 when it does not.  The gateway surfaces a non-zero
    exit as a 500 with ``returncode`` in the error details, which
    we map back to ``False``; any other failure (network, missing
    object) is also treated as ``False`` so callers can fall
    through to a conservative path.
    """
    try:
        self._make_request(
            "/api/v1/git/execute",
            method="POST",
            data={
                "repo_path": repo_path,
                "operation": "merge-base",
                "args": ["--is-ancestor", ancestor_sha, descendant_sha],
            },
            bearer_token=bearer_token,
        )
        return True
    except GatewayError as exc:
        details = exc.details or {}
        returncode = details.get("returncode")
        if returncode != 1:
            _pkg.logger.warning(
                "merge-base --is-ancestor failed unexpectedly",
                pipeline_id=pipeline_id,
                ancestor=ancestor_sha,
                descendant=descendant_sha,
                returncode=returncode,
                error=str(exc),
            )
        return False


def is_slice_branch_merged_into_parent(
    self,
    pipeline_id: str,
    repo_path: str,
    *,
    integration_branch: str,
    parent_branch: str,
    integration_base_sha: str | None = None,
    agent_role: str = "coder",
    mode: Literal["public", "private"] = "public",
) -> bool:
    """Return True iff the slice integration branch's tip on origin is
    already reachable from ``parent_branch``'s tip on origin.

    This is the #2549 "slice already merged" signal: after the slice's
    PR is merged into the parent, the integration branch's old tip is
    an ancestor of the parent's new tip. The inverse direction of the
    #2512 restart-recovery check — and the case that previously caused
    ``create_slice_integration_branch`` to fall through to a non-fast-
    forward push and fail the slice (and cascade-fail the phase).

    Returns False on any of:

    * Either branch is missing on origin (nothing to compare against).
    * The integration branch tip equals the parent tip (``==`` is
      neither "merged" nor "diverged"; just a no-op state — let the
      regular create path handle it as a fast-forward no-op).
    * ``integration_base_sha`` is supplied and the integration branch
      tip still equals it (#2871): the branch never received a slice
      commit, so it is *un-started* work, not merged work. Such a
      branch is trivially an ancestor of any advanced parent (its tip
      *is* the parent's old fork point), and treating that ancestry as
      "merged → COMPLETE" silently skips a slice that never ran. We
      can only make this call when the caller recorded the fork base;
      ``None`` (slices provisioned before #2871) falls through to the
      ancestor-only check, preserving the prior behaviour.
    * The ancestry check itself fails (gateway down, missing object
      after a flaky fetch). In that case we return False so the
      caller falls through to the existing create path rather than
      silently skipping the slice.

    The transport mirrors :meth:`create_slice_integration_branch`:
    a single synthetic launcher-authenticated session shared across
    ls-remote, fetch, and the merge-base call.
    """
    if not integration_branch or not parent_branch:
        return False
    if integration_branch == parent_branch:
        return False

    temp_container_id = f"{pipeline_id}-slice-merged-check-{integration_branch.replace('/', '-')}"
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
        )
        session_token = session.session_token

        parent_sha = self.get_remote_branch_sha(
            pipeline_id,
            repo_path,
            f"refs/heads/{parent_branch}",
            mode=mode,
            bearer_token=session_token,
        )
        existing_sha = self.get_remote_branch_sha(
            pipeline_id,
            repo_path,
            f"refs/heads/{integration_branch}",
            mode=mode,
            bearer_token=session_token,
        )
        if not parent_sha or not existing_sha:
            return False
        if parent_sha == existing_sha:
            return False

        # #2871 — empty / un-started slice branch guard. When the
        # caller recorded the fork base (the SHA the integration
        # branch was created at) and the branch tip still equals it,
        # the slice never received a commit. Its tip is the parent's
        # old fork point, so it is *trivially* an ancestor of any
        # advanced parent — but that is un-started work, not merged
        # work. Returning True here would mark the slice COMPLETE and
        # skip it, running dependents without their prerequisite.
        # ``existing_sha`` and ``integration_base_sha`` both originate
        # from ``get_remote_branch_sha`` (full 40-char SHAs), so an
        # exact compare is correct.
        if integration_base_sha and existing_sha == integration_base_sha:
            _pkg.logger.info(
                "Slice integration branch is still at its creation base "
                "(no slice commits) — treating as un-started, not merged (#2871)",
                pipeline_id=pipeline_id,
                integration_branch=integration_branch,
                parent_branch=parent_branch,
                integration_base_sha=integration_base_sha,
            )
            return False

        # Both refs must be locally reachable for ``merge-base
        # --is-ancestor`` to evaluate without errors. Best-effort:
        # if either fetch fails the merge-base call will return
        # False (missing object → returncode != 0) and we degrade
        # to "not merged", which matches the safe default.
        self.fetch_branch(
            pipeline_id,
            repo_path,
            args=[f"+refs/heads/{parent_branch}:refs/remotes/origin/{parent_branch}"],
            mode=mode,
            bearer_token=session_token,
        )
        self.fetch_branch(
            pipeline_id,
            repo_path,
            args=[f"+refs/heads/{integration_branch}:refs/remotes/origin/{integration_branch}"],
            mode=mode,
            bearer_token=session_token,
        )

        return self._sha_is_ancestor(
            pipeline_id,
            repo_path,
            existing_sha,
            parent_sha,
            bearer_token=session_token,
        )
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "is_slice_branch_merged_into_parent: gateway request failed",
            pipeline_id=pipeline_id,
            integration_branch=integration_branch,
            parent_branch=parent_branch,
            error=str(exc),
        )
        return False
    finally:
        if session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass


def find_unreachable_evidence_commits(
    self,
    pipeline_id: str,
    repo_path: str,
    *,
    commit_shas: Sequence[str],
    integration_branch: str,
    mode: Literal["public", "private"] = "public",
) -> list[str] | None:
    """Return the subset of ``commit_shas`` NOT reachable from the
    integration branch's tip on origin (#3125).

    The slice close path uses this to verify that every commit SHA
    cited by a contract task record actually landed on the
    integration branch before the slice PR is opened. A producer's
    post-confirmation commit (the prescribed ``complete-task
    --commit`` unblock flow) lives only on that agent's local
    worktree branch unless something pushed it — this check is what
    turns that silent loss into a hard stop.

    Tri-state per SHA, derived from ``git merge-base --is-ancestor
    <sha> <tip>`` through ``/api/v1/git/execute``:

    * exit 0 — reachable;
    * exit 1 — the commit object exists locally but is not an
      ancestor of the tip → unreachable;
    * exit 128 — the SHA does not resolve to an object at all
      (never pushed and the worktree odb was pruned, or an
      abbreviated SHA that no longer resolves) → unreachable. This
      is the fully-lost variant of the same gap, so it must fail
      the gate, not skip it.

    Returns ``None`` (caller skips the gate with a warning) when
    the check cannot be evaluated at all — branch tip unresolvable
    on origin, fetch failure, or a gateway/network error on the
    merge-base call itself. A transient infrastructure failure must
    not fail the slice; the conservative posture matches the other
    completeness checks (#3081 / #3114).

    Transport mirrors :meth:`is_slice_branch_merged_into_parent`:
    one synthetic launcher-authenticated session shared across the
    ls-remote, the fetch, and every merge-base call.
    """
    if not commit_shas:
        return []
    if not integration_branch:
        # No branch to probe means we cannot evaluate reachability.
        # Skip the gate rather than silently approve — matches the
        # other "cannot evaluate" paths in this method (#3125 review).
        return None

    temp_container_id = (
        f"{pipeline_id}-evidence-reachability-{integration_branch.replace('/', '-')}"
    )
    session_token: str | None = None
    try:
        session = self.register_session(
            container_id=temp_container_id,
            container_ip=self.self_ip,
            mode=mode,
            pipeline_id=pipeline_id,
            synthetic=True,
        )
        session_token = session.session_token

        tip_sha = self.get_remote_branch_sha(
            pipeline_id,
            repo_path,
            f"refs/heads/{integration_branch}",
            mode=mode,
            bearer_token=session_token,
        )
        if not tip_sha:
            _pkg.logger.warning(
                "Evidence-reachability check skipped: integration branch "
                "tip unresolvable on origin (#3125)",
                pipeline_id=pipeline_id,
                integration_branch=integration_branch,
            )
            return None

        # The tip's objects must be locally reachable for merge-base
        # to evaluate. Unlike the ancestor probes elsewhere, a failed
        # fetch here must SKIP the gate rather than degrade — with no
        # tip objects every merge-base would exit 128 and every cited
        # commit would be falsely flagged unreachable, failing the
        # slice on a network blip.
        fetched = self.fetch_branch(
            pipeline_id,
            repo_path,
            args=[f"+refs/heads/{integration_branch}:refs/remotes/origin/{integration_branch}"],
            mode=mode,
            bearer_token=session_token,
        )
        if not fetched:
            _pkg.logger.warning(
                "Evidence-reachability check skipped: integration branch fetch failed (#3125)",
                pipeline_id=pipeline_id,
                integration_branch=integration_branch,
            )
            return None

        unreachable: list[str] = []
        for sha in commit_shas:
            try:
                self._make_request(
                    "/api/v1/git/execute",
                    method="POST",
                    data={
                        "repo_path": repo_path,
                        "operation": "merge-base",
                        "args": ["--is-ancestor", sha, tip_sha],
                    },
                    bearer_token=session_token,
                )
            except GatewayError as exc:
                details = exc.details or {}
                returncode = details.get("returncode")
                if returncode in (1, 128):
                    # 1 — object present, not an ancestor.
                    # 128 — SHA unresolvable in the odb (fully lost).
                    unreachable.append(sha)
                    continue
                _pkg.logger.warning(
                    "Evidence-reachability check skipped: merge-base failed unexpectedly (#3125)",
                    pipeline_id=pipeline_id,
                    integration_branch=integration_branch,
                    commit_sha=sha,
                    returncode=returncode,
                    error=str(exc),
                )
                return None
        return unreachable
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "Evidence-reachability check skipped: gateway request failed (#3125)",
            pipeline_id=pipeline_id,
            integration_branch=integration_branch,
            error=str(exc),
        )
        return None
    finally:
        if session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass
