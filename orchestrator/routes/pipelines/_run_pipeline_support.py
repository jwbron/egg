"""run_pipeline health-monitor closure helpers helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _on_health_escalation_impl(escalation, *, health_monitor_instance, pipeline_id):
    phase = health_monitor_instance.get_current_phase()
    _pkg._send_brc_confirmation_nudge(escalation, pipeline_id, phase)


def _health_monitor_poll_impl(
    monitor,
    stop_event,
    interval=30.0,
    *,
    pipeline_id,
    worktree_repo_path,
    store,
    divergence_alerted_shas,
):
    while not stop_event.is_set():
        try:
            # Tier 1 no longer sends nudges directly — it raises
            # alerts and fires escalation callbacks internally.
            # The overseer (Tier 2) decides whether to nudge.
            monitor.check_tripwires()
        except Exception as poll_err:
            _pkg.logger.debug(
                "Health monitor poll error",
                pipeline_id=pipeline_id,
                error=str(poll_err),
            )

        # Branch-divergence detector (#2224 PR 3).  Helper
        # re-loads pipeline state each tick so a
        # base_branch / branch update mid-pipeline is
        # picked up.  Dedupe set is mutated in place.
        _pkg._branch_divergence_tick(
            pipeline_id=pipeline_id,
            worktree_repo_path=worktree_repo_path,
            store=store,
            alerted_shas=divergence_alerted_shas,
        )

        # NOTE (#2270 slice-5): the standing-pod overseer respawn loop
        # was removed here. The overseer is no longer a respawned
        # standing pod — orchestrator-side detection (slice-4
        # ``health_checks.detection_plane``) runs in-process and the
        # only agent spawned is the on-demand adjudicator. Any
        # surviving restart need is served by the general
        # agent-restart machinery (``restart_agent``), not a bespoke
        # overseer respawn. This also means a multi-hour zero-agent
        # HITL park spawns nothing from this loop (§3).

        stop_event.wait(interval)
