"""Remote-branch list / fetch / ls-remote / sha lookups (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

from typing import Any, Literal

import gateway_client as _pkg
from gateway_client import GatewayError


def list_remote_branches(
    self,
    pipeline_id: str,
    repo_path: str,
    *,
    agent_role: str = "coder",
    mode: Literal["public", "private"] = "public",
) -> set[str]:
    """List remote branches via ``git ls-remote --heads origin``.

    Returns a set of branch names (the trailing-segment of each
    ``refs/heads/<name>`` line in ``ls-remote`` output). The
    transport is the existing ``/api/v1/git/fetch`` route with
    ``operation="ls-remote"`` — no new privileged surface.

    On error returns an empty set. The reconciler treats an empty
    set as "nothing confirmable on origin" and skips every
    candidate (an open PR whose head branch is not in the set is
    not actionable; see ``find_orphaned_child_prs``), so a failed
    listing degrades to a no-op pass rather than a storm of doomed
    rebases (#3479).
    """
    return set(
        self.list_remote_branches_with_shas(
            pipeline_id,
            repo_path,
            agent_role=agent_role,
            mode=mode,
            # Preserve the audit-log identifier the stacked-PR
            # reconciler used before this method was unified with
            # the SHA-returning variant — runbooks and dashboards
            # filter by this string.
            operation_tag="stacked-pr-ls-remote",
        ).keys()
    )


def list_remote_branches_with_shas(
    self,
    pipeline_id: str,
    repo_path: str,
    *,
    agent_role: str = "coder",
    mode: Literal["public", "private"] = "public",
    operation_tag: str = "ls-remote",
) -> dict[str, str]:
    """Like :meth:`list_remote_branches` but returns ``{branch: sha}``.

    Used by the recovery-ref cleanup sweep (#2446), which needs the
    SHA at each ref tip to read its committer date for staleness
    detection without an extra fetch round-trip.

    ``operation_tag`` is appended to the synthetic gateway session's
    container id (``f"{pipeline_id}-{operation_tag}"``) and shows up
    in audit/session logs. Callers should pick a tag that matches
    the operation they are performing so existing log-filter rules
    keep working — e.g. the stacked-PR reconciler passes
    ``"stacked-pr-ls-remote"``. Empty / non-alphanumeric values are
    rejected to keep the audit-log identifier well-formed: an empty
    tag would produce a trailing-dash id, and a tag containing
    whitespace or ``/`` would silently break the log-filter rules
    the kwarg was added to preserve.

    Same error-handling contract as :meth:`list_remote_branches`:
    on any gateway failure returns an empty mapping.
    """
    if not repo_path:
        return {}
    # Validation is intentionally strict: callers are all internal,
    # so a bad tag is a programming error, not user input. Hyphens
    # are allowed because the canonical tag format is hyphen-
    # separated (e.g. "stacked-pr-ls-remote"). The isascii() check
    # rejects unicode alphanumerics (e.g. "café") that would
    # otherwise pass isalnum() and produce mixed-encoding audit-log
    # identifiers.
    if (
        not operation_tag
        or not operation_tag.isascii()
        or not operation_tag.replace("-", "").isalnum()
    ):
        raise ValueError(
            f"operation_tag must be non-empty ASCII alphanumeric (hyphens allowed); "
            f"got {operation_tag!r}"
        )
    temp_container_id = f"{pipeline_id}-{operation_tag}"
    session_token: str | None = None
    try:
        session = self.register_session(
            container_id=temp_container_id,
            container_ip=self.self_ip,
            mode=mode,
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            synthetic=True,
        )
        session_token = session.session_token

        result = self._make_request(
            "/api/v1/git/fetch",
            method="POST",
            data={
                "repo_path": repo_path,
                "remote": "origin",
                "operation": "ls-remote",
                "args": ["--heads"],
            },
            bearer_token=session_token,
        )
        stdout = (result.get("data", {}) or {}).get("stdout", "") or ""
        branches: dict[str, str] = {}
        for line in stdout.splitlines():
            # Lines look like "<sha>\trefs/heads/<name>".
            parts = line.strip().split("\t")
            if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                branches[parts[1][len("refs/heads/") :]] = parts[0]
        return branches
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "list_remote_branches_with_shas: gateway request failed",
            pipeline_id=pipeline_id,
            repo_path=repo_path,
            error=str(exc),
        )
        return {}
    finally:
        if session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass


def fetch_worktree_branch(
    self,
    pipeline_id: str,
    repo_path: str,
    mode: Literal["public", "private"] = "public",
) -> bool:
    """Fetch latest remote state into a worktree using a temporary session.

    Best-effort operation to sync remote changes into a worktree —
    called before phase execution to ensure the worktree has all state
    from previous phases (e.g., after orchestrator restart where the
    local branch diverged from remote).

    Args:
        pipeline_id: Pipeline ID (used as container_id for the temp session)
        repo_path: Path to the worktree repo directory

    Returns:
        True if fetch succeeded, False otherwise
    """
    temp_container_id = f"{pipeline_id}-failsafe-fetch"
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

        # Do NOT include container_id — repo_path is already the
        # resolved worktree path; the synthetic container_id has no
        # real worktree and would trigger a "worktree not found" error.
        self._make_request(
            "/api/v1/git/fetch",
            method="POST",
            data={
                "repo_path": repo_path,
                "remote": "origin",
            },
            bearer_token=session_token,
        )

        _pkg.logger.info(
            "Fetched remote state into worktree",
            pipeline_id=pipeline_id,
        )
        return True
    except Exception as e:
        _pkg.logger.warning(
            "Best-effort fetch failed (continuing with local state)",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return False
    finally:
        if session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass


def fetch_branch(
    self,
    pipeline_id: str,
    repo_path: str,
    args: list[str] | None = None,
    mode: Literal["public", "private"] = "public",
    *,
    bearer_token: str | None = None,
    retry_transient: bool = False,
) -> bool:
    """Fetch with custom args using a temporary session.

    Best-effort operation used to fetch specific refs from remote.

    Args:
        pipeline_id: Pipeline ID; used as ``container_id`` for the
            temp session and for log fields.  When ``bearer_token``
            is supplied no session is registered, so it's only used
            for log fields in that case.
        repo_path: Path to the repo directory
        args: Additional args for git fetch (e.g., ["+remote:local"])
        mode: Network mode for the temp session.  Ignored when
            ``bearer_token`` is supplied — the supplied session's
            mode was fixed at its register time.
        bearer_token: Pre-registered synthetic session token to reuse
            (#2398).  When provided, skip the internal
            ``register_session``/``delete_session`` and authenticate
            the fetch with the supplied token — lets a caller share
            one session across several gateway calls.

    Returns:
        True if fetch succeeded, False otherwise
    """
    owns_session = bearer_token is None
    session_token: str | None = bearer_token
    try:
        if owns_session:
            temp_container_id = f"{pipeline_id}-state-fetch"
            session = self.register_session(
                container_id=temp_container_id,
                container_ip=self.self_ip,
                mode=mode,
                pipeline_id=pipeline_id,
                synthetic=True,
                retry_transient=retry_transient,
            )
            session_token = session.session_token

        # Do NOT include container_id — repo_path is already the
        # resolved path; the synthetic container_id has no real
        # worktree and would trigger a "worktree not found" error.
        def _do_fetch() -> dict[str, Any]:
            return self._make_request(
                "/api/v1/git/fetch",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
                    "args": args or [],
                },
                bearer_token=session_token,
            )

        if retry_transient:
            self._retry_transient(_do_fetch, operation="fetch branch")
        else:
            _do_fetch()

        _pkg.logger.info(
            "Fetched branch from remote",
            pipeline_id=pipeline_id,
            fetch_args=args,
        )
        return True
    except Exception as e:
        _pkg.logger.warning(
            "Best-effort fetch failed",
            pipeline_id=pipeline_id,
            fetch_args=args,
            error=str(e),
        )
        return False
    finally:
        if owns_session and session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass


def _ls_remote_branch_impl(
    self,
    pipeline_id: str,
    repo_path: str,
    ref: str,
    mode: Literal["public", "private"],
    container_id_suffix: str,
) -> bool:
    """Shared implementation of the ls-remote branch-existence probe.

    Raises on any gateway / network / policy failure — including a
    ``{"success": false, ...}`` envelope returned at HTTP 200. Public
    wrappers apply their respective error policies at the outer
    layer: :meth:`ls_remote_branch` swallows and returns ``False``;
    :meth:`ls_remote_branch_strict` propagates.
    """
    temp_container_id = f"{pipeline_id}-{container_id_suffix}"
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

        # Do NOT include container_id — repo_path is already the
        # resolved path; the synthetic container_id has no real
        # worktree and would trigger a "worktree not found" error.
        result = self._make_request(
            "/api/v1/git/fetch",
            method="POST",
            data={
                "repo_path": repo_path,
                "remote": "origin",
                "operation": "ls-remote",
                "args": ["--heads", ref],
            },
            bearer_token=session_token,
        )

        # A {"success": false, ...} envelope returned at HTTP 200 is
        # a gateway-side failure surfaced via the envelope rather
        # than the status code. Without this guard the strict
        # variant would silently collapse such a response to "branch
        # absent", contradicting its propagate-any-failure contract.
        if not result.get("success", True):
            raise GatewayError(result.get("message", "ls-remote envelope reported success=false"))

        # ls-remote returns output in data.stdout; non-empty means branch exists
        stdout = result.get("data", {}).get("stdout", "")
        return bool(stdout.strip())
    finally:
        if session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass


def ls_remote_branch(
    self,
    pipeline_id: str,
    repo_path: str,
    ref: str,
    mode: Literal["public", "private"] = "public",
) -> bool:
    """Check if a remote branch exists using ls-remote.

    Lenient variant: collapses gateway / network / policy failures
    to ``False``. Callers that need to distinguish "branch absent
    on origin" from "probe could not be performed" — notably the
    ``_resolve_slice_base_branch`` parent-existence gate (#2928) —
    must use :meth:`ls_remote_branch_strict` instead.

    Args:
        pipeline_id: Pipeline ID (used as container_id for the temp session)
        repo_path: Path to the repo directory
        ref: Branch ref to check (e.g., "refs/heads/egg/pipeline-state")

    Returns:
        True if the remote branch exists, False otherwise (or on error).
    """
    try:
        return self._ls_remote_branch_impl(
            pipeline_id=pipeline_id,
            repo_path=repo_path,
            ref=ref,
            mode=mode,
            container_id_suffix="state-ls-remote",
        )
    except Exception as e:
        _pkg.logger.warning(
            "ls-remote check failed",
            pipeline_id=pipeline_id,
            ref=ref,
            error=str(e),
        )
        return False


def ls_remote_branch_strict(
    self,
    pipeline_id: str,
    repo_path: str,
    ref: str,
    mode: Literal["public", "private"] = "public",
) -> bool:
    """Check if a remote branch exists using ls-remote.

    Strict variant of :meth:`ls_remote_branch`: a gateway / network
    / policy failure RAISES rather than collapsing to ``False``.
    Use this when the caller needs to distinguish "branch absent
    on origin" from "probe could not be performed" — for example,
    ``_resolve_slice_base_branch`` (#2928) routes a confirmed
    absent parent onto ``pipeline_branch`` but treats a raised
    probe as "assume parent exists" so a flaky gateway never
    silently swaps a real slice onto ``work``. The lenient
    :meth:`ls_remote_branch` collapses those two outcomes and is
    unsafe for that gate.
    """
    return self._ls_remote_branch_impl(
        pipeline_id=pipeline_id,
        repo_path=repo_path,
        ref=ref,
        mode=mode,
        container_id_suffix="state-ls-remote-strict",
    )


def get_remote_branch_sha(
    self,
    pipeline_id: str,
    repo_path: str,
    ref: str,
    mode: Literal["public", "private"] = "public",
    *,
    bearer_token: str | None = None,
    retry_transient: bool = False,
) -> str | None:
    """Resolve a remote ref to its commit SHA via ``git ls-remote``.

    Returns the SHA string when the ref exists on origin, or ``None``
    when it doesn't (or when the gateway request fails).  Used by
    ``create_pipeline`` to detect stale-pipeline-branch state on
    re-submit (#2222): if ``origin/egg/issue-N`` resolves to a
    different SHA than ``origin/<base_branch>``, the branch carries
    prior-pipeline commits and starting on top of it would inherit
    them — so refuse with a hint to ``cancel_task(cleanup=true)``.

    ``bearer_token`` lets a caller pass in a pre-registered synthetic
    session to share across several gateway calls (#2398).  When
    provided, the per-call ``register_session``/``delete_session``
    round-trip is skipped, and the ``mode`` argument is ignored —
    the supplied session's mode was fixed at its register time.
    """
    owns_session = bearer_token is None
    session_token: str | None = bearer_token
    try:
        if owns_session:
            temp_container_id = f"{pipeline_id}-state-ls-remote-sha"
            session = self.register_session(
                container_id=temp_container_id,
                container_ip=self.self_ip,
                mode=mode,
                pipeline_id=pipeline_id,
                synthetic=True,
                retry_transient=retry_transient,
            )
            session_token = session.session_token

        def _do_ls_remote() -> dict[str, Any]:
            return self._make_request(
                "/api/v1/git/fetch",
                method="POST",
                data={
                    "repo_path": repo_path,
                    "remote": "origin",
                    "operation": "ls-remote",
                    "args": ["--heads", ref],
                },
                bearer_token=session_token,
            )

        if retry_transient:
            result = self._retry_transient(_do_ls_remote, operation="resolve remote SHA")
        else:
            result = _do_ls_remote()

        stdout = result.get("data", {}).get("stdout", "")
        if not stdout.strip():
            return None
        # ``git ls-remote`` output: ``<sha>\trefs/heads/<branch>``
        sha = stdout.split()[0].strip()
        return sha or None
    except Exception as e:
        _pkg.logger.warning(
            "ls-remote sha lookup failed",
            pipeline_id=pipeline_id,
            ref=ref,
            error=str(e),
        )
        return None
    finally:
        if owns_session and session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass
