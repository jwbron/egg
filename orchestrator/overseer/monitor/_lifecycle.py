"""OverseerMonitor lifecycle, oversight logging, and health-summary methods.

Extracted from the pre-split ``overseer/monitor.py`` (#3312, slice-8) as
module-level functions taking ``self`` explicitly; bound back onto
``OverseerMonitor`` in the package barrel (method-modules-on-class,
docs/guides/decomposition-pattern.md §c).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from overseer.decision_maker import (
    AdjudicationVerdict,
    build_adjudication_prompt,
    parse_adjudication_verdict,
)

from . import logger


def _resolve_oversight_dir() -> Path | None:
    """Resolve the .egg-state/oversight/ directory path."""
    repo_path = os.environ.get("EGG_REPO_PATH")
    if repo_path:
        return Path(repo_path) / ".egg-state" / "oversight"
    return None


def _log_oversight_event(self, event: dict) -> None:
    """Append an oversight event as a JSONL line."""
    if not self._jsonl_path:
        return
    try:
        import datetime

        record = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "pipeline_id": self.pipeline_id,
            **event,
        }
        with open(self._jsonl_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logger.debug("Failed to write oversight event to JSONL", exc_info=True)


def write_health_summary(self) -> None:
    """Write the pipeline health summary to .egg-state/oversight/."""
    if not self._oversight_dir:
        return
    try:
        summary = self.generate_health_summary()
        summary_path = self._oversight_dir / f"{self.pipeline_id}-health-summary.md"
        summary_path.write_text(summary)
    except Exception:
        logger.debug("Failed to write health summary", exc_info=True)


async def start(self) -> None:
    """Start the monitoring loop.

    .. deprecated:: #2270 slice-4

        This continuous poll-sleep loop is the **standing-pod** shape the
        overhaul retires (refine HITL Option C). Overseership is now an
        orchestrator-side deterministic detection plane
        (``health_checks.detection_plane``) that runs on the event loop;
        the only agent the orchestrator spawns is a NORMAL on-demand
        adjudicator (see :meth:`adjudicate`). The respawn/standing-pod
        machinery that drives this loop is removed in slice-5. New callers
        must NOT rely on a long-lived monitor.

    Runs until :meth:`stop` is called or the pipeline reaches a
    terminal state (``complete``, ``failed``, or ``cancelled``).
    """
    self._running = True
    logger.info("Overseer monitor started for pipeline %s", self.pipeline_id)

    while self._running:
        await self._poll_cycle()
        poll_interval = getattr(self.config, "overseer_poll_interval_seconds", 30)
        await asyncio.sleep(poll_interval)


async def adjudicate(self, finding: Any) -> AdjudicationVerdict:
    """Adjudicate a single detection-plane finding on-demand (#2270 slice-4).

    This is the on-demand counterpart to the retired standing-pod loop: a
    *single-shot* evaluation of ONE finding, with no polling and no sleep.
    The detection plane only calls this for findings carrying
    ``requires_adjudication`` — the routine majority never reach an LLM.

    The verdict is ADVISORY: it names one of the bounded corrective actions
    for the slice-6 authority plane to execute. A failed adjudicator call
    degrades to a conservative *defer-to-operator* verdict rather than
    dropping the finding.
    """
    prompt = build_adjudication_prompt(finding)
    # Adjudication is the high-stakes / adversarial tier — resolves to Opus
    # (#2270 §1), never the deprecated decision-maker field.
    model = self._resolve_tier_model("adversarial")
    try:
        if self._decision_maker and hasattr(self._decision_maker, "adjudicate"):
            raw = await self._decision_maker.adjudicate(finding, model=model)
        else:
            from egg_agent.client import run_agent_async

            result = await run_agent_async(prompt, model=model, max_turns=1)
            if not result.success:
                raise RuntimeError(result.error or "adjudicator call failed")
            raw = result.stdout.strip()
    except Exception as exc:  # noqa: BLE001 — never drop a finding on a call error
        logger.warning(
            "Adjudicator call failed for pipeline %s; deferring to operator: %s",
            self.pipeline_id,
            exc,
        )
        raw = ""
    return parse_adjudication_verdict(raw, finding=finding)


async def stop(self) -> None:
    """Stop the monitoring loop and write final health summary."""
    self._running = False
    self.write_health_summary()
    logger.info("Overseer monitor stopped for pipeline %s", self.pipeline_id)


def reset_escalation_history(self) -> None:
    """Drop all accumulated escalation history so a restart starts clean.

    Called when an agent is restarted (``restart_agent`` /
    ``restart_phase``): the pre-restart redirect history would otherwise
    survive and inflate ``redirect_count``, pushing a freshly-restarted
    agent straight to HITL escalation on its first stall (#2270 §3).
    Idempotent — resetting an already-empty history is a harmless no-op.
    """
    self._escalation_history.clear()


def reset_generation(self, generation: int | None = None) -> None:
    """Reset the generation token and clear all escalation history.

    Called on orchestrator pod recycle. With ``generation`` provided the
    token is set to that explicit value; with ``generation=None`` (the
    default recycle shape) the token is advanced by one. Either way the
    escalation history is cleared, so stale escalation state can never
    cascade into the new generation's corrective decisions. The
    generation stamp on each record plus the generation-filtered
    redirect-history reads make this leak-proof even if a record somehow
    survives the clear (e.g. via persisted/replayed state).
    """
    if generation is None:
        self.generation += 1
    else:
        self.generation = generation
    self.reset_escalation_history()
    # A new generation re-files diagnostics for anomalies that persist
    # across the recycle, so clear the dedup ledger too (#2270 slice-9).
    self._issue_dedup_ledger.reset()


def generate_health_summary(self) -> str:
    """Generate a pipeline health summary at completion.

    Returns:
        A markdown-formatted health summary string.
    """
    self_health = self.self_monitor.check_health()
    metrics = self_health["metrics"]

    escalation_summary_lines: list[str] = []
    for agent_role, history in self._escalation_history.items():
        actions = [h.get("action", "?") for h in history]
        escalation_summary_lines.append(
            f"- **{agent_role}**: {len(history)} escalation(s) -- actions: {', '.join(actions)}"
        )

    escalation_text = (
        "\n".join(escalation_summary_lines)
        if escalation_summary_lines
        else "- No escalations during pipeline run"
    )

    self_health_text = ""
    if self_health["concerns"]:
        concerns = "\n".join(f"  - {c}" for c in self_health["concerns"])
        self_health_text = f"\n### Overseer Self-Health Concerns\n{concerns}\n"

    return f"""## Pipeline Health Summary

**Pipeline**: `{self.pipeline_id}`
**Monitor cycles**: {metrics["cycle_count"]}
**Total messages sent**: {metrics["total_messages"]}
**LLM calls**: {metrics["total_llm_calls"]} (${metrics["total_llm_cost_usd"]:.4f})

### Escalation History
{escalation_text}
{self_health_text}
### Metrics
- Avg poll duration: {metrics["avg_poll_duration_seconds"]:.2f}s
- Max poll duration: {metrics["max_poll_duration_seconds"]:.2f}s
- Hourly LLM cost: ${metrics["hourly_llm_cost_usd"]:.4f}
"""
