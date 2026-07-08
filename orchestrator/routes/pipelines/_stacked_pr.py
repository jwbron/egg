"""stacked-PR reconciler launcher helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _start_stacked_pr_reconciler(
    pipeline_id: str,
    contract_loader: _pkg.Callable[[], _pkg.Any],
    gateway,
    pipeline,
    *,
    interval_seconds: float | None = None,
    worktree_repo_path: _pkg.Path | None = None,
    repo: str | None = None,
    store=None,  # noqa: ANN001 — StateStore (avoid import cycle)
) -> tuple[_pkg.threading.Thread, _pkg.threading.Event]:
    """Start the periodic stacked-PR reconciler as a daemon thread (#2137 TASK-5-3).

    Returns ``(thread, stop_event)``: caller calls ``stop_event.set()``
    when the implement phase is shutting down so the daemon exits
    cleanly. The daemon loops on the configured interval and invokes
    :func:`stacked_pr_reconciler.reconcile_once` with callables that
    decouple it from the gateway client.

    ``store`` (optional) bounds the daemon's lifetime independently of the
    launching driver: each tick re-reads the pipeline status and the loop
    exits once the pipeline is terminal or deleted (#3540). Without it the
    daemon's only exit is ``stop_event``, which is set in the launcher's
    ``finally`` block; a driver wedged inside its work loop never reaches
    that block, and in #3540 the orphaned reconciler's 30s gateway session
    registrations were the only log signal for 11 hours.

    The list-callables (``list_open_prs`` and ``list_remote_branches``)
    forward to ``GatewayClient.list_open_prs`` /
    ``GatewayClient.list_remote_branches``. ``list_open_prs`` routes
    through the launcher-authed control-plane route
    ``/api/v1/gh/list_open_prs`` (#2925); ``list_remote_branches`` routes
    through the existing per-agent ``git ls-remote`` allowlist. The rebase
    callable forwards to
    ``GatewayClient.rebase_onto``, which performs the full local
    rebase + ``--force-with-lease`` push + ``gh api PATCH base=…``
    retarget so an orphaned child PR is fully healed on origin
    rather than just locally rewritten.
    """
    try:
        from orchestrator.env_config import get_stacked_pr_reconciler_interval_seconds
    except ImportError:
        from env_config import (  # type: ignore[no-redef]
            get_stacked_pr_reconciler_interval_seconds,
        )
    try:
        from orchestrator.stacked_pr_reconciler import reconcile_once
    except ImportError:
        from stacked_pr_reconciler import reconcile_once  # type: ignore[no-redef]
    # #3393 slice-5: the cross-repo merge-sequencing gate rides the SAME
    # reconciler cadence (no new scheduler subsystem). Imported here (not
    # top-level) to keep this helper's import surface minimal, mirroring
    # the ``reconcile_once`` import above.
    try:
        import orchestrator.cross_repo_merge_gate as cross_repo_merge_gate
    except ImportError:
        import cross_repo_merge_gate  # type: ignore[no-redef]
    try:
        from orchestrator.env_config import get_cross_repo_merge_gate_max_attempts
    except ImportError:
        from env_config import (  # type: ignore[no-redef]
            get_cross_repo_merge_gate_max_attempts,
        )
    try:
        from orchestrator.models import resolve_slice_repo
    except ImportError:
        from models import resolve_slice_repo  # type: ignore[no-redef]

    if interval_seconds is None:
        try:
            interval_seconds = float(get_stacked_pr_reconciler_interval_seconds())
        except Exception:  # noqa: BLE001
            interval_seconds = 30.0

    stop_event = _pkg.threading.Event()

    # ``repo_path`` must be a filesystem path the gateway's
    # ``validate_repo_path`` accepts (``/home/egg/repos/``,
    # ``/home/egg/.egg-worktrees/``, etc.) — NOT the git branch
    # name. Use the orchestrator-side worktree path that the
    # implement loop already owns.
    repo_path_str = str(worktree_repo_path) if worktree_repo_path is not None else ""
    pr_repo = repo or str(getattr(pipeline, "repo", "") or "")

    # #3393 slice-5: only multi-repo pipelines can have cross-repo
    # dependency edges, so the merge gate is a strict no-op for N=1 —
    # skip it entirely rather than burning a contract scan per tick.
    _gate_enabled = len(getattr(pipeline, "repos", None) or []) > 1
    # Per-run gate bookkeeping (attempts / hold-registered / resolved),
    # keyed by dependent slice id; persists across reconciler ticks.
    _gate_state: dict[str, _pkg.Any] = {}
    try:
        _gate_max_attempts = int(get_cross_repo_merge_gate_max_attempts())
    except Exception:  # noqa: BLE001
        _gate_max_attempts = cross_repo_merge_gate.DEFAULT_MAX_POLL_ATTEMPTS
    _gate_current_phase = getattr(pipeline, "current_phase", None)

    def _poll_cross_repo_merge_gate(contract: _pkg.Any) -> None:
        # Drive one cross-repo merge-sequencing pass on the reconciler
        # cadence (#3393 slice-5, task-5-1 / task-5-2). Reads upstream PR
        # merge-state and auto-readies a dependent draft PR on merge
        # (Tier A); registers a HITL hold on the closed-unmerged / timeout
        # terminals and for plan-declared beyond-merge-state edges (Tier
        # B). All gateway/contract effects are funnelled through the
        # injected callables so the gate logic stays pure + unit-tested.
        if not _gate_enabled:
            return
        cross_repo_merge_gate.poll_once(
            contract,
            resolve_repo=lambda s: resolve_slice_repo(s, pipeline),
            get_merge_state=lambda repo_slug, pr_num: gateway.get_pr_merge_state(
                pipeline_id, repo_slug, pr_number=pr_num
            ),
            mark_ready=lambda repo_slug, pr_num: bool(
                gateway.mark_pr_ready(pipeline_id, repo_slug, pr_number=pr_num)
            ),
            register_hold=lambda gate, reason: _pkg._register_cross_repo_hold(
                pipeline_id=pipeline_id,
                slice_id=gate.slice_id,
                repo=gate.repo,
                pr_number=gate.pr_number,
                reason=reason,
                worktree_repo_path=worktree_repo_path,
                current_phase=_gate_current_phase,
            ),
            hold_resolution=lambda gate: _pkg._cross_repo_hold_resolution(contract, gate.slice_id),
            state=_gate_state,
            max_attempts=_gate_max_attempts,
        )

    def _list_open_prs() -> list[dict[str, _pkg.Any]]:
        # Lists open PRs in ``pr_repo`` so ``find_orphaned_child_prs``
        # can detect children whose base branch was deleted (parent
        # merged through the GitHub UI). Routes through the launcher-authed
        # control-plane endpoint ``/api/v1/gh/list_open_prs`` — the
        # orchestrator is the server that manages pipelines, not an agent,
        # so it does not register a synthetic agent session or impersonate
        # a role (#2922 / #2925).
        if not pr_repo:
            return []
        try:
            return list(gateway.list_open_prs(pipeline_id, pr_repo))
        except Exception as exc:  # noqa: BLE001
            _pkg.logger.debug(
                "stacked_pr_reconciler: list_open_prs raised — treating as empty",
                pipeline_id=pipeline_id,
                error=str(exc),
            )
            return []

    def _list_extant_branches() -> set[str]:
        # Lists remote branches via ``git ls-remote --heads origin``
        # so the reconciler can detect deleted parents. Routes through
        # the existing per-agent ``git ls-remote`` allowlist. The
        # synthetic session uses ``agent_role="orchestrator"`` so this
        # orchestrator-driven ls-remote is attributed to the orchestrator
        # in the audit log instead of a phantom coder (#2919).
        if not repo_path_str:
            return set()
        try:
            return set(
                gateway.list_remote_branches(
                    pipeline_id,
                    repo_path_str,
                    agent_role="orchestrator",
                )
            )
        except Exception as exc:  # noqa: BLE001
            _pkg.logger.debug(
                "stacked_pr_reconciler: list_remote_branches raised — treating as empty",
                pipeline_id=pipeline_id,
                error=str(exc),
            )
            return set()

    def _rebase_onto(orphan: _pkg.Any) -> bool:
        # ``orphan`` is a ``stacked_pr_reconciler.OrphanedChildPR``;
        # avoid the import here so this module stays a pure consumer
        # of the reconciler's typed interface (the type checker at
        # the reconciler boundary already validates the shape).
        try:
            return bool(
                gateway.rebase_onto(
                    pipeline_id,
                    repo_path_str,
                    branch=orphan.branch,
                    new_base=orphan.intended_new_base,
                    old_base=orphan.deleted_base,
                    pr_number=orphan.pr_number,
                    repo=pr_repo or None,
                    # Orchestrator-driven heal (rebase + force-push +
                    # pr-edit); attribute to the orchestrator, not a
                    # phantom coder (#2919). The force-push targets the
                    # slice integration branch on a synthetic session, so
                    # the slice-integration exemption admits it regardless
                    # of role.
                    agent_role="orchestrator",
                )
            )
        except Exception:  # noqa: BLE001
            _pkg.logger.debug(
                "stacked_pr_reconciler: rebase_onto raised — counted as failure",
                pipeline_id=pipeline_id,
                branch=getattr(orphan, "branch", "?"),
            )
            return False

    def _loop() -> None:
        # Defensive: a slow tick must not pin this thread on a stale
        # sleep — Event.wait returns True the moment ``stop_event`` is
        # set, so shutdown is bounded by the configured interval.
        while not stop_event.wait(interval_seconds):
            # #3540: self-terminate when the pipeline is terminal or gone,
            # so a driver that never reaches its finally block (wedged or
            # hung) cannot leave this daemon registering gateway sessions
            # forever. Transient read failures keep the loop running; only
            # a positive terminal/deleted signal stops it.
            if store is not None:
                try:
                    status = store.load_pipeline(pipeline_id).status
                except _pkg.PipelineNotFoundError:
                    _pkg.logger.info(
                        "stacked_pr_reconciler: pipeline deleted; stopping (#3540)",
                        pipeline_id=pipeline_id,
                    )
                    return
                except Exception:  # noqa: BLE001
                    status = None
                if status is not None and status in _pkg.PipelineStatus.terminal():
                    _pkg.logger.info(
                        "stacked_pr_reconciler: pipeline is terminal; stopping (#3540)",
                        pipeline_id=pipeline_id,
                        status=status.value,
                    )
                    return
            try:
                contract = contract_loader()
                if contract is None:
                    continue
                reconcile_once(
                    contract,
                    list_open_prs=_list_open_prs,
                    list_extant_branches=_list_extant_branches,
                    rebase_onto=_rebase_onto,
                )
                # #3393 slice-5: drive the cross-repo merge-sequencing
                # gate on the same tick + same freshly-loaded contract.
                # No-op for N=1 pipelines. Wrapped in its own try so a
                # gate failure never disrupts stacked-PR reconciliation.
                try:
                    _poll_cross_repo_merge_gate(contract)
                except Exception as gate_exc:  # noqa: BLE001
                    _pkg.logger.debug(
                        "cross_repo_merge_gate tick raised — continuing",
                        pipeline_id=pipeline_id,
                        error=str(gate_exc),
                    )
            except Exception as exc:  # noqa: BLE001
                _pkg.logger.debug(
                    "stacked_pr_reconciler tick raised — continuing",
                    pipeline_id=pipeline_id,
                    error=str(exc),
                )

    thread = _pkg.threading.Thread(
        target=_loop,
        name=f"stacked-pr-reconciler-{pipeline_id}",
        daemon=True,
    )
    thread.start()
    return thread, stop_event
