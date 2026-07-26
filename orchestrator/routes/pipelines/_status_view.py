"""pipeline status view + slice diff summary helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal  # noqa: F401

import routes.pipelines as _pkg  # noqa: E402,F401

if TYPE_CHECKING:
    try:
        from ..container_spawner import ContainerSpawner  # noqa: F401
    except ImportError:  # pragma: no cover
        from container_spawner import ContainerSpawner  # type: ignore  # noqa: F401


def _get_pr_info(pipeline: _pkg.Pipeline) -> tuple[str | None, int | None]:
    """Extract context-PR URL and number from the pipeline contract.

    Returns ``(pr_url, pr_number)`` or ``(None, None)`` when no PR has
    been opened. Under #2777 the PR phase was removed and the context
    PR opens up-front via ``_open_context_pr_at_implement_start`` which
    persists ``context_pr_number`` to ``contract.pr.context_pr_number``;
    we read that directly. ``pr_url`` is also persisted on the pipeline
    record by ``_open_context_pr_at_implement_start`` for downstream
    consumers (the JIRA reassess sweep at ``jira_reassess.py``).
    """
    # ``Pipeline.pr_url`` / ``Pipeline.pr_number`` are populated by the
    # up-front opener; they are the canonical surface for callers that
    # used to read ``phases["pr"].artifacts["pr_url"]``.
    pr_url = getattr(pipeline, "pr_url", None)
    pr_number = getattr(pipeline, "pr_number", None)
    if not pr_url:
        return None, None
    if pr_number is None:
        match = _pkg.re.search(r"/pull/(\d+)", pr_url)
        pr_number = int(match.group(1)) if match else None
    return pr_url, pr_number


def _consensus_block(consensus_state: dict) -> dict:
    """Slim a tracker ``get_state()`` snapshot down to the status payload.

    Keeps the fields operators act on (per-role phases + confirmed
    flags, the blocking set, and the unresolved-NACK details: who
    NACKed whom, on which version, and why; #3481) and drops the
    bulky ``approval_matrix`` / ``review_graph`` dumps.

    #3548: the slimming used to drop the recorded verdicts entirely, so a
    landed ACK was indistinguishable from a lost one (the reviewer shows
    ``reviewer_phase: REVIEWING`` + ``confirmed: false`` either way) and
    the zero-proposal confirm blocker was unnamed. Keep compact
    projections of both: ``proposal_versions``, a 4-field
    ``review_edges`` list, and ``zero_proposal_producers`` (producers
    with no proposal — the global confirm guard rejects every confirm
    while this set is non-empty).

    BRC trackers only emit dict-format agent entries (the legacy
    AgentReadiness object came from the now-deleted ConsensusEvaluator,
    cq-5 of #2777).
    """
    agents = dict(consensus_state.get("agents", {}))
    matrix = consensus_state.get("approval_matrix") or {}
    proposal_versions = dict(matrix.get("proposal_versions") or {})
    review_edges = [
        {
            "reviewer": entry.get("reviewer_role"),
            "producer": entry.get("producer_role"),
            "state": entry.get("state"),
            "version": entry.get("version"),
        }
        for entry in (matrix.get("entries") or {}).values()
        if isinstance(entry, dict)
    ]
    review_edges.sort(key=lambda e: (str(e["producer"]), str(e["reviewer"])))
    zero_proposal_producers = sorted(
        role
        for role, info in agents.items()
        if isinstance(info, dict) and "producer_phase" in info and not proposal_versions.get(role)
    )
    return {
        "agents": agents,
        "is_complete": consensus_state.get("is_complete", False),
        "blocking_agents": consensus_state.get("blocking_agents", []),
        "has_unresolved_nacks": consensus_state.get("has_unresolved_nacks", False),
        "unresolved_nacks": consensus_state.get("unresolved_nacks", []),
        "proposal_versions": proposal_versions,
        "review_edges": review_edges,
        "zero_proposal_producers": zero_proposal_producers,
        "protocol": consensus_state.get("protocol", "brc"),
    }


def _get_concurrent_status(pipeline: _pkg.Pipeline, slice_id: str | None = None) -> dict | None:
    """Get concurrent execution monitoring data for a pipeline.

    Returns None if concurrent execution is not enabled for this pipeline.
    Returns a dict with the following structure when concurrent mode is active::

        {
            "enabled": True,
            "max_concurrent_agents": int,
            "messages": {"total": int, "by_type": {"PROGRESS": int, ...}},
            "consensus": {
                "agents": {"coder": {"state": "READY", ...}, ...},
                "is_complete": bool,
                "blocking_agents": ["role", ...]  # agents not yet READY
            },
            "agents": [{"role": str, "status": str}, ...]  # from phase execution
        }

    Dependencies on other concurrent-mode modules (message_store, consensus) are
    imported lazily and degrade gracefully to empty structures when unavailable.

    ``slice_id``: in a slice-DAG implement phase each slice runs its own
    BRC consensus, keyed ``{pipeline_id}/{slice_id}``. The bare pipeline
    id has no tracker, so a non-slice lookup reported a misleading
    cross-slice reconstruction (#2761). Callers querying a per-slice
    agent's consensus must pass that agent's ``slice_id``; the consensus
    block then reflects exactly that slice's tracker. When omitted, only
    pipeline-level (non-slice) consensus is reported in ``consensus``;
    a slice-DAG pipeline queried without a slice yields no ``consensus``
    block rather than a fabricated one. Instead, live slice-scoped
    trackers are surfaced under ``slice_consensus`` keyed by slice_id
    (#3481), so operators still see each active round's real state.
    """
    try:
        from concurrent_executor import is_concurrent_execution
    except ImportError:
        from ..concurrent_executor import is_concurrent_execution  # type: ignore[no-redef]

    current_phase = pipeline.current_phase.value if pipeline.current_phase else None
    if not is_concurrent_execution(pipeline, phase=current_phase):
        return None

    config = pipeline.config
    result: dict = {
        "enabled": True,
        "max_concurrent_agents": getattr(config, "max_concurrent_agents", 6),
    }

    # Message store provides aggregate counts of inter-agent messages by type.
    # This module is implemented in phase-1 of the concurrent execution feature;
    # ImportError is expected until that phase lands.
    try:
        from message_store import get_message_store
    except ImportError:
        try:
            from ..message_store import get_message_store  # type: ignore[no-redef]
        except ImportError:
            _pkg.logger.debug("Message store not available for status")
            get_message_store = None  # type: ignore[assignment]

    if get_message_store is not None:
        store = get_message_store()
        _run_epoch_str = pipeline.run_epoch.isoformat() if pipeline.run_epoch else None
        msg_status = store.get_status(pipeline.id, run_epoch=_run_epoch_str)
        result["messages"] = {
            "total": msg_status.get("total", 0),
            "by_type": msg_status.get("by_type", {}),
        }
    else:
        result["messages"] = {"total": 0, "by_type": {}}

    # Consensus evaluator tracks per-agent readiness states and determines
    # whether all agents agree the phase is complete. Implemented in phase-3;
    # blocking_agents lists roles that are not yet READY (WORKING or BLOCKED).
    # BRC peer consensus (preferred) or legacy readiness-based
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

        tracker = get_peer_consensus_tracker(pipeline.id, slice_id)
        if not tracker:
            # Attempt lazy reconstruction from message store for concurrent
            # pipelines. ``slice_id`` scopes the replay to one slice's
            # tracker; without it, only pipeline-level messages replay so a
            # slice-DAG pipeline does not reconstruct cross-slice (#2761).
            try:
                from review_graph import get_review_graph_for_phase

                try:
                    from peer_consensus import reconstruct_tracker_from_messages
                except ImportError:
                    from ..peer_consensus import (
                        reconstruct_tracker_from_messages,  # type: ignore[no-redef]
                    )

                if is_concurrent_execution(pipeline, pipeline.current_phase):
                    graph = get_review_graph_for_phase(
                        pipeline.current_phase.value, repo=pipeline.repo
                    )
                    tracker = reconstruct_tracker_from_messages(
                        pipeline.id,
                        graph,
                        slice_id=slice_id,
                        phase=pipeline.current_phase.value,
                    )
            except ImportError:
                pass  # Fall through to legacy evaluator
            except Exception as e:
                _pkg.logger.warning(
                    "Tracker reconstruction failed",
                    error=str(e),
                    pipeline_id=pipeline.id,
                    slice_id=slice_id,
                )
        if tracker:
            consensus_state = tracker.get_state()
        else:
            # No BRC tracker available (slice-scoped query for a slice with
            # no tracker yet, or a non-concurrent pipeline). The legacy
            # ConsensusEvaluator was removed under cq-5 of #2777, so there
            # is no fallback evaluator to consult. Report no consensus
            # block; callers (e.g. the MCP get_consensus_status tool) fall
            # back to message-based inference per the existing #1229 path.
            consensus_state = None
    except ImportError:
        _pkg.logger.debug("Peer consensus tracker not available for status")
        consensus_state = None

    if consensus_state is not None:
        result["consensus"] = _pkg._consensus_block(consensus_state)
    else:
        # Don't populate consensus with empty placeholder — callers (e.g. the
        # MCP get_consensus_status tool) use truthiness to decide whether to
        # fall back to message-based inference.  An empty-but-truthy dict
        # prevents that fallback from triggering (see issue #1229).
        pass

    # Slice-id-less observability (#3481): in a slice-DAG implement phase
    # the live trackers are keyed ``{pipeline_id}/{slice_id}``, so the
    # pipeline-level lookup above finds nothing and an operator querying
    # without a slice scope saw no structured consensus at all; the only
    # way to see tracker state was tailing orchestrator pod logs. Surface
    # each active slice's real snapshot, explicitly keyed by slice. This
    # is NOT the #2761 cross-slice "soup" (that was mingling every
    # slice's messages into ONE inferred tracker); the pipeline-level
    # ``consensus`` block above still never reflects a slice tracker.
    if slice_id is None:
        try:
            try:
                from peer_consensus import get_slice_trackers
            except ImportError:
                from ..peer_consensus import get_slice_trackers  # type: ignore[no-redef]

            slice_trackers = get_slice_trackers(pipeline.id)
        except ImportError:
            slice_trackers = {}
        slice_consensus: dict[str, dict] = {}
        for sid in sorted(slice_trackers):
            try:
                slice_consensus[sid] = _pkg._consensus_block(slice_trackers[sid].get_state())
            except Exception as e:  # noqa: BLE001 - one bad slice must not hide the rest
                _pkg.logger.warning(
                    "Slice consensus snapshot failed",
                    pipeline_id=pipeline.id,
                    slice_id=sid,
                    error=str(e),
                )
        if slice_consensus:
            result["slice_consensus"] = slice_consensus

    # Agent lifecycle info from the phase execution record — shows which agents
    # are spawned for the current phase and their container-level status.
    # Includes ``container_id`` and server-computed ``elapsed_seconds`` so the
    # sandboxed overseer can anchor stall-duration math on the live container's
    # ``started_at`` rather than pre-restart message-bus events (issue #2084).
    current_phase_name = pipeline.current_phase.value
    phase_exec = pipeline.phases.get(current_phase_name)
    agents_info: list[dict[str, _pkg.Any]] = []
    if phase_exec and hasattr(phase_exec, "agents"):
        now = _pkg.datetime.now(_pkg.UTC)
        for agent in phase_exec.agents:
            if hasattr(agent, "role"):
                role = agent.role.value if hasattr(agent.role, "value") else str(agent.role)
            else:
                role = str(agent)
            if hasattr(agent, "status"):
                status = agent.status.value if hasattr(agent.status, "value") else "unknown"
            else:
                status = "unknown"

            entry: dict[str, _pkg.Any] = {"role": role, "status": status}

            container_id = getattr(agent, "container_id", None)
            if isinstance(container_id, str) and container_id:
                entry["container_id"] = container_id

            started_at = getattr(agent, "started_at", None)
            started_dt: _pkg.datetime | None = None
            if isinstance(started_at, _pkg.datetime):
                started_dt = started_at
            elif isinstance(started_at, str) and started_at:
                try:
                    started_dt = _pkg.datetime.fromisoformat(started_at)
                except ValueError:
                    started_dt = None
            if started_dt is not None:
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=_pkg.UTC)
                entry["started_at"] = started_dt.isoformat()
                entry["elapsed_seconds"] = max(0, int((now - started_dt).total_seconds()))

            agents_info.append(entry)

        # When the persisted phase-agent list is empty, backfill the
        # running-pod view from live Job labels (#3230). Under the
        # orchestrator-owned event loop (#3164) on-demand one-shot pods are
        # never persisted into ``phase_exec.agents``, so without this the
        # overseer's stall-duration math and the dashboard see "0 running
        # agents" while role pods are demonstrably ``Running``. Empty stays
        # empty when no pod is live, so legitimate between-spawn quiescence is
        # not misreported as a cohort.
        if not agents_info:
            agents_info = _pkg._live_event_agents(pipeline.id, slice_id)
        result["agents"] = agents_info

    return result


def _build_slice_diff_summary(
    pipeline,
    spawner: "ContainerSpawner",  # noqa: UP037
    worktree_repo_path: _pkg.Path,
    integration_branch: str,
    parent_branch: str,
    gateway_mode: Literal["public", "private"] = "public",
) -> tuple[list[str] | None, str | None]:
    """Compute commit subjects + diffstat for a slice PR body (#3115).

    The slice PR body's task list is plan-derived — it describes intent,
    not what the pushed branch actually contains. This helper reads the
    real git state so ``create_slice_pr`` can render a ``## What's in
    this PR`` section: the slice's commit subjects
    (``git log origin/<parent>..origin/<head>``) and a diffstat against
    the merge base (``git diff --stat origin/<parent>...origin/<head>``,
    three-dot to match GitHub's PR diff semantics).

    Both remote-tracking refs are refreshed first via
    ``GatewayClient.fetch_branch`` — the slice's agents push directly to
    origin, so the orchestrator worktree's tracking refs may lag (same
    pattern as :func:`_commit_slice_brc_history_to_integration_branch`,
    which runs immediately before this in the slice loop). ``gateway_mode``
    must be threaded from the pipeline-computed mode at the call site;
    defaulting to ``public`` against a private/internal repo causes the
    gateway to refuse the session and the whole diff section silently
    no-ops.

    Strictly best-effort: returns ``(None, None)`` on any failure
    (fetch, git error, timeout) and never raises — a missing diff
    summary must not block slice PR creation.
    """
    pipeline_id = pipeline.id
    try:
        for branch in (parent_branch, integration_branch):
            # ``fetch_branch`` swallows exceptions and returns False;
            # a stale parent ref degrades the diffstat, it doesn't
            # break it, so we just continue.
            spawner.gateway.fetch_branch(
                pipeline_id,
                str(worktree_repo_path),
                args=[f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                mode=gateway_mode,
            )

        git_base = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            f"safe.directory={worktree_repo_path}",
            "-C",
            str(worktree_repo_path),
        ]
        span = f"origin/{parent_branch}..origin/{integration_branch}"
        log_proc = _pkg.subprocess.run(
            [*git_base, "log", "--no-merges", "--format=%s", span],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        commit_subjects = (
            [line.strip() for line in log_proc.stdout.splitlines() if line.strip()]
            if log_proc.returncode == 0
            else None
        )
        # ``--stat=100,80,40``: 100-col output, then git truncates past
        # 40 entries with an ellipsis line — a slice touching hundreds
        # of files must not produce a body longer than the task dump
        # this section exists to displace.
        diff_proc = _pkg.subprocess.run(
            [
                *git_base,
                "diff",
                "--stat=100,80,40",
                f"origin/{parent_branch}...origin/{integration_branch}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        diffstat = diff_proc.stdout.strip() if diff_proc.returncode == 0 else None
        if not commit_subjects and not diffstat:
            return None, None
        return commit_subjects or None, diffstat or None
    except Exception as err:  # noqa: BLE001
        _pkg.logger.warning(
            "Slice diff summary failed (slice PR opens without it) (#3115)",
            pipeline_id=pipeline_id,
            integration_branch=integration_branch,
            parent_branch=parent_branch,
            error=str(err),
        )
        return None, None
