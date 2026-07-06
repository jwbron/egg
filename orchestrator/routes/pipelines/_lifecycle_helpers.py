"""pipeline lifecycle mutation helpers helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

from typing import Literal  # noqa: F401

import routes.pipelines as _pkg  # noqa: E402,F401


def _normalize_submission_repos(
    repos_arg: _pkg.Any,
) -> tuple[str | None, list[dict[str, str | None]], str | None, str | None]:
    """Validate + normalize a multi-repo submission list (#3393).

    Accepts the ``repos`` payload from ``POST /api/v1/pipelines`` — a list of
    ``{repo, base_branch?, primary?}`` entries (a bare ``"owner/name"`` string
    is tolerated as ``{repo: ...}``). Returns
    ``(error, entries, primary_repo, primary_base_branch)``:

    * ``error`` — a human-readable message when validation fails (the other
      fields are meaningless in that case), else ``None``.
    * ``entries`` — normalized ``{"repo", "base_branch"}`` dicts, reordered so
      the primary is ``entries[0]`` (the ``Pipeline`` validator mirrors
      ``repos[0]`` onto the legacy singleton and ``primary_repo``).

    Per-entry repo/base_branch formats are validated with the same regexes the
    single-repo path uses. Same-name repos under different owners are NOT
    rejected here — they are distinct full ``owner/name`` slugs (operator
    ruling #6; the owner/repo re-key lands in slice 3).
    """
    if not isinstance(repos_arg, list) or not repos_arg:
        return ("repos must be a non-empty list of {repo, base_branch} entries", [], None, None)
    entries: list[dict[str, str | None]] = []
    primary_index = 0
    seen_primary = False
    for idx, raw in enumerate(repos_arg):
        entry = {"repo": raw} if isinstance(raw, str) else raw
        if not isinstance(entry, dict) or not entry.get("repo"):
            return (f"repos[{idx}] must be an object with a 'repo' field", [], None, None)
        repo_val = entry["repo"]
        if not _pkg.re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$", repo_val):
            return (
                f"Invalid repo format in repos[{idx}]: {repo_val!r} (expected owner/name)",
                [],
                None,
                None,
            )
        base_val = entry.get("base_branch")
        if base_val is not None and (
            not _pkg.re.match(r"^[a-zA-Z0-9_./-]+$", base_val) or ".." in base_val
        ):
            return (f"Invalid base_branch in repos[{idx}]: {base_val!r}", [], None, None)
        entries.append({"repo": repo_val, "base_branch": base_val})
        if entry.get("primary"):
            if seen_primary:
                return ("At most one repos entry may set 'primary'", [], None, None)
            seen_primary = True
            primary_index = idx
    # Reorder so the primary is first: the Pipeline model mirrors repos[0]
    # onto the legacy repo/base_branch singleton and exposes it as
    # ``primary_repo``.
    if primary_index != 0:
        entries.insert(0, entries.pop(primary_index))
    primary = entries[0]
    return (None, entries, primary["repo"], primary["base_branch"])


def _assert_repo_set_uniform(repos: list[str]) -> str | None:
    """Reject mixed-visibility / mixed-auth repo sets at submission (#3393, task-2-2).

    A pipeline-wide private-mode posture (context filtering, egress rules)
    requires every repo in one run to be uniformly private or uniformly public,
    and — for v1 — to share a single auth mode. Returns an actionable,
    repo-naming error string when the set diverges on either dimension, or
    ``None`` when it is uniform. A single repo (after de-duplication) is
    trivially uniform and short-circuits before any lookup, so N=1 pipelines
    pay no cost and make no gateway round-trip.

    Runtime note (container boundary): the orchestrator image bundles
    ``config/repo_config.py`` but NOT ``gateway/``, so the per-repo lookups are
    reached the way the orchestrator already reaches them — auth via
    ``repo_config.assert_uniform_auth`` (imported directly, the same callable the
    gateway's ``validate_auth_mode_uniformity`` delegates to) and visibility via
    ``GatewayClient.get_repo_visibility`` over HTTP (the gateway holds the
    tokens; mirrors ``_compute_gateway_mode``). ``internal`` counts as private.
    The visibility comparison below is the HTTP-boundary twin of
    ``gateway.repo_visibility.validate_visibility_uniformity`` (which the
    orchestrator cannot import); keep the two in step.
    """
    unique = list(dict.fromkeys(repos))
    if len(unique) <= 1:
        return None

    # Auth-mode uniformity — repo_config is bundled into the orchestrator image.
    try:
        from repo_config import assert_uniform_auth

        assert_uniform_auth(unique)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:  # pragma: no cover - defensive (config read failure)
        # Fail CLOSED for consistency with the visibility boundary below
        # (reviewer_security v1): a config-read failure means we cannot prove a
        # uniform auth mode, so we must not admit the set. repo_config is a
        # local, bundled read — this path is genuinely exceptional, not a
        # transient network hiccup.
        _pkg.logger.warning("Auth-mode uniformity check errored; failing closed", error=str(exc))
        return (
            "Could not determine the auth mode for the pipeline's repos, so a "
            "uniform bot/user auth mode cannot be verified. Resubmit once repo "
            "configuration is resolvable."
        )

    # Visibility uniformity — resolved via the gateway (the orchestrator's only
    # visibility source). FAIL CLOSED on an indeterminate lookup (reviewer_security
    # v1): for a multi-repo set (we only reach here when len(unique) > 1) a repo
    # whose visibility cannot be resolved to a known bucket means the uniform
    # private/public posture cannot be PROVEN — and this is a confidentiality
    # boundary (a mixed set that slips through would let private-repo content
    # flow through shared plan/contract/PR surfaces into a public repo, with no
    # downstream re-check: _compute_gateway_mode derives the network mode from
    # the PRIMARY repo only). N=1 short-circuits above, so the common case pays
    # nothing. This mirrors gateway.repo_visibility.validate_visibility_uniformity;
    # keep the two in step. Unrecognized (non-None) labels are treated as
    # indeterminate too — only the known {public|private|internal} contract admits.
    gw = _pkg.get_gateway_client()
    posture: dict[str, list[str]] = {}
    for repo in unique:
        vis = gw.get_repo_visibility(repo)
        if vis in ("private", "internal"):
            bucket = "private"
        elif vis == "public":
            bucket = "public"
        else:
            return (
                f"Could not determine repository visibility for {repo!r}; cannot "
                "verify a uniform private/public posture across the pipeline's "
                "repos (a run must be uniformly private or uniformly public so "
                "private-repo content cannot leak through shared plan/contract/PR "
                "surfaces). Resubmit once the repo's visibility is resolvable."
            )
        posture.setdefault(bucket, []).append(repo)
    if len(posture) > 1:
        groups = "; ".join(f"{b}: {', '.join(sorted(rs))}" for b, rs in sorted(posture.items()))
        return (
            "Mixed repository visibility across the pipeline's repos is not allowed "
            "(a run must be uniformly private or uniformly public, so private-repo "
            f"content cannot leak through shared plan/PR surfaces). Diverging repos — {groups}."
        )
    return None


def _clear_pipeline_runtime_state(pipeline_id: str, *, reason: str) -> None:
    """Evict per-pipeline runtime state that is keyed by pipeline_id alone.

    The peer-consensus tracker, the legacy consensus evaluator, and the
    inter-agent message store are all keyed by pipeline_id. Without a
    matching ``run_epoch`` namespace, a fresh pipeline that reuses an id
    from a prior terminal run (same branch, e.g. ``issue-1965``) will
    inherit the prior run's CONFIRMED consensus and message history. The
    leak surfaces in the ``/status/wait`` route's Path-B envelope, which
    would report ``concurrent.consensus.is_complete: true`` for a
    pipeline that has not spawned any agents yet (#2053).

    Called when a pipeline transitions to a terminal status, when its
    state file is deleted, and immediately after a fresh pipeline is
    created (covers paths that bypass PATCH/DELETE — auto-FAILED, and
    Redis-backed message-store entries that survived an orchestrator
    restart between cancel and resubmit).
    """
    try:
        try:
            from peer_consensus import remove_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (  # type: ignore[no-redef]
                remove_peer_consensus_tracker,
            )
        remove_peer_consensus_tracker(pipeline_id)
    except ImportError:
        pass
    except Exception as e:
        _pkg.logger.warning(
            "Failed to clear peer consensus tracker",
            pipeline_id=pipeline_id,
            reason=reason,
            error=str(e),
        )

    # Reconstruct-from-messages would otherwise replay the prior run's
    # CONSENSUS_* messages and rebuild a CONFIRMED tracker, defeating the
    # tracker eviction above.
    try:
        try:
            from message_store import get_message_store
        except ImportError:
            from ..message_store import get_message_store  # type: ignore[no-redef]
        get_message_store().clear(pipeline_id)
    except ImportError:
        pass
    except Exception as e:
        _pkg.logger.warning(
            "Failed to clear message store",
            pipeline_id=pipeline_id,
            reason=reason,
            error=str(e),
        )


def _mark_pipeline_records_terminated(
    store: _pkg.StateStore,
    pipeline_id: str,
) -> _pkg.Pipeline:
    """Mark all running containers and agents as stopped after pipeline termination.

    Called when a pipeline transitions to a terminal state (cancelled or failed).
    After Docker containers are force-removed, the pipeline state still shows
    them as "running". This reloads the latest state from the store (to avoid
    overwriting updates made between the status change and container
    cleanup), marks running records as stopped, and saves.

    Returns the updated pipeline so the caller can use it in the response.
    """
    pipeline = store.load_pipeline(pipeline_id)
    now = _pkg.datetime.now(_pkg.UTC)
    changed = False

    for phase_exec in pipeline.phases.values():
        for container in phase_exec.containers:
            if container.status in (
                _pkg.ContainerStatus.PENDING,
                _pkg.ContainerStatus.CREATING,
                _pkg.ContainerStatus.RUNNING,
            ):
                container.status = _pkg.ContainerStatus.REMOVED
                container.exited_at = now
                changed = True

        for agent in phase_exec.agents:
            if agent.status in (
                _pkg.AgentExecutionStatus.PENDING,
                _pkg.AgentExecutionStatus.RUNNING,
            ):
                agent.status = _pkg.AgentExecutionStatus.FAILED
                agent.completed_at = now
                agent.error = f"Pipeline {pipeline.status.value}"
                changed = True

    if changed:
        store.save_pipeline(pipeline)
        _pkg.logger.info(
            "Synced pipeline state after termination",
            pipeline_id=pipeline_id,
        )

    return pipeline


def _compute_gateway_mode(
    pipeline: _pkg.Pipeline,
) -> tuple[Literal["public", "private"], str | None]:
    """Compute gateway session mode from pipeline config and repo visibility.

    Uses the explicit ``network_mode`` if set, otherwise auto-detects from
    repository visibility via the gateway.  Defaults to ``"public"``.

    Returns:
        A ``(mode, visibility)`` tuple.  ``visibility`` is ``None`` when
        ``network_mode`` is explicit, the pipeline has no repo, or the
        gateway query failed.
    """
    if pipeline.network_mode:
        return pipeline.network_mode, None
    if pipeline.repo:
        vis = _pkg.get_gateway_client().get_repo_visibility(pipeline.repo)
        if vis in ("private", "internal"):
            return "private", vis
        return "public", vis
    return "public", None


def _cleanup_remote_branches(
    pipeline_id: str,
    pipeline: _pkg.Pipeline,
    repo_path: _pkg.Path,
) -> None:
    """Best-effort cleanup of remote branches for a pipeline.

    Deletes the pipeline's shared branch (``pipeline.branch``, typically
    ``egg/{pipeline_id}/work`` since #2399) and every per-container
    worktree branch (``egg/{container_id}/work``).  Slice integration
    branches at ``egg/{pipeline_id}/slice-N`` are siblings of the
    pipeline tip and are NOT deleted here — see follow-up tracking on
    #2399 for full namespace cleanup.  Failures are logged as warnings
    and do not block pipeline deletion.
    """
    branches: set[str] = set()
    if pipeline.branch:
        branches.add(pipeline.branch)
    for phase_exec in pipeline.phases.values():
        for container in phase_exec.containers:
            branches.add(f"egg/{container.container_id}/work")

    if not branches:
        return

    gateway_client = _pkg.get_gateway_client()
    repo_path_str = str(repo_path)
    mode, _vis = _pkg._compute_gateway_mode(pipeline)

    deleted = 0
    for branch in sorted(branches):
        result = gateway_client.delete_remote_branch(pipeline_id, repo_path_str, branch, mode=mode)
        # ``already_deleted`` means the desired state (branch absent on
        # remote) is satisfied — count it as success rather than churning a
        # warning every time a pipeline is cleaned up before any branch was
        # ever pushed.
        if result or result.category == "already_deleted":
            deleted += 1
        else:
            _pkg.logger.warning(
                "Remote branch deletion failed during pipeline cleanup",
                pipeline_id=pipeline_id,
                branch=branch,
                category=result.category,
                detail=result.detail,
            )

    if deleted:
        _pkg.logger.info(
            "Cleaned up remote branches",
            pipeline_id=pipeline_id,
            branches_deleted=deleted,
            branches_total=len(branches),
        )
