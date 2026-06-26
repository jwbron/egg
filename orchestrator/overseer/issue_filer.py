"""GitHub diagnostic issue filing for the overseer agent.

# The canonical issue-body template literal is the single source of truth at
# shared/egg_overseer/issue_template.py; sandbox-side production filing goes
# through the ``egg-orch overseer file-issue`` CLI verb
# (``sandbox/egg_lib/orch_cli.py``, #1962 decision-9 opt-1).
#
# ``file_diagnostic_issue`` below is still exercised orchestrator-side: the
# overseer monitor's ``issue`` corrective action calls it (guarded by the
# ``overseer_auto_file_issues_mode`` shadow->enforce gate and the two-tier
# ``IssueDedupLedger``). ``FINDING_CLASS_REMEDIATIONS`` /
# ``remediation_for_finding_class`` feed the per-finding-class remediation
# lines. Keep this module — it is imported and called, not dead.
#
# The literal at lines 86-107 below is preserved byte-for-byte as the
# canonical-literal source: ``orchestrator/tests/test_overseer_issue_filer.py``
# asserts the literal here matches
# ``egg_overseer.issue_template.TEMPLATE_LITERAL`` substring-for-substring
# so any drift fails CI loudly.

Builds structured issue bodies following the diagnostic format and
files them via the ``gh`` CLI.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from egg_overseer.issue_template import TEMPLATE_LITERAL

logger = logging.getLogger(__name__)

# Paste-ready remediation entries per detection-plane finding class (#2270 §5).
# Each new coverage-gap detector class gets one line here so a filed diagnostic
# issue ships with an actionable next step instead of the generic fallback.
# Keyed on the detector's ``finding_class`` string — these keys MUST stay in
# lock-step with the ``FINDING_*`` constants the tier1/detection-plane detectors
# emit (orchestrator/health_checks/...). A key that drifts from the emitted
# ``finding_class`` string silently falls through to ``_DEFAULT_REMEDIATION``.
FINDING_CLASS_REMEDIATIONS: dict[str, str] = {
    "container_death": (
        "Producer container is genuinely dead (crash/fatal exit, no reschedule). "
        "Inspect the crash cause, then respawn the cohort or open an operator HITL. "
        "Distinct from a transient eviction that reschedules (#2948)."
    ),
    "container_oom_evicted": (
        "A container was OOM-killed / evicted under memory or node pressure and "
        "rescheduled. Confirm the reschedule landed; if the workload is genuinely "
        "over its memory request, raise the limit or shrink the working set before "
        "the next spawn. Distinct from a fatal crash (container_death)."
    ),
    "overseer_self_injection": (
        "Overseer is refusing its own bootstrap as prompt-injection and looping "
        "refuse->exit->respawn (#2270 §1). Run the overseer decision tier on Opus "
        "and deliver instructions via tools/prompt, not a baked-in script."
    ),
    "overseer_self_health": (
        "The overseer's own self-monitor is unhealthy (respawn churn / bootstrap "
        "fail-loop / lifetime-cost breach — #2270 §1, §3). Inspect the overseer "
        "pod's own lifecycle before trusting its alerts; stop respawning and open "
        "an operator HITL if it cannot bootstrap cleanly."
    ),
    "container_restart_loop": (
        "A role's container is crash-looping past the restart threshold. Inspect "
        "the crash logs before respawning again; escalate to HITL if it persists."
    ),
    "runtime_thread_dead": (
        "The orchestrator _run_pipeline driver thread appears dead/hung while the "
        "phase is RUNNING (#2234/#3233). Restart the run loop / recycle the "
        "orchestrator pod and reset the generation token."
    ),
    "duration_drift": (
        "Phase is running far past its expected budget. Check for a silent wedge "
        "or a misconfigured duration estimate."
    ),
    "phase_stall": (
        "A phase is genuinely wedged — no lifecycle owner is driving it and it is "
        "neither awaiting a spawn nor parked on a HITL (#3230). Re-drive the phase "
        "transition or open an operator HITL to break the deadlock."
    ),
    "heartbeat_stall": (
        "An agent has stopped heart-beating while still expected to be working. "
        "Confirm the container is alive (distinguish a dead pod from a stale "
        "health-DB record), then nudge or respawn the affected role."
    ),
    "agent_restart_propagation": (
        "An agent restart was requested but never propagated to a running "
        "container. Verify the restart endpoint and Job teardown."
    ),
    "auto_advance_wedge": (
        "An auto-advanceable approved decision did not advance the phase (#2219). "
        "Re-drive the phase transition or open an operator HITL."
    ),
    "approved_decision_orphaned": (
        "A resolved/approved decision has no consumer acting on it. Re-deliver the "
        "decision to the owning phase handler."
    ),
    "restarted_decision_replay": (
        "A decision is being replayed after restart (stale escalation cascade). "
        "Clear per-agent escalation history and reset the generation token."
    ),
    "hitl_queue_backlog": (
        "Operator-facing HITL decisions are piling up unanswered past the backlog "
        "threshold. The pipeline is blocked on human input — surface the oldest "
        "pending decisions to the operator and stop generating new escalations "
        "until the queue drains."
    ),
    "worktree_corruption": (
        "Worktree git index is locked/corrupt. Clear the stale lock or re-clone "
        "the worktree before resuming git operations."
    ),
    "disk_inode_pressure": (
        "Disk or inode usage is critically high. Prune worktrees / build caches "
        "or grow the volume before the next agent spawn."
    ),
    "pr_external_mutation": (
        "The PR was mutated outside the pipeline. Reconcile the PR head against "
        "the last pushed SHA before continuing."
    ),
    "pushed_pr_not_updated": (
        "A pushed commit is not reflected in the PR. Re-sync the PR head or "
        "re-push; check for a stuck PR-update step."
    ),
    "gateway_error_spike": (
        "Gateway 5xx error rate spiked. Inspect gateway logs and upstream "
        "(LiteLLM/Anthropic) health."
    ),
    "gateway_repeated_denial": (
        "The gateway is repeatedly denying the same operation. The agent is "
        "likely retrying a forbidden action — re-scope its task or fix the call."
    ),
    "gateway_token_expiry": (
        "A gateway credential/token has expired. Refresh the injected credential "
        "and restart the affected agent."
    ),
    "brc_thrash": (
        "BRC consensus is thrashing (repeated NACK/propose cycles, including a "
        "CONFIRMED edge followed by a late re-NACK). Adjudicate the disagreement "
        "or open an operator HITL to break the loop."
    ),
    "incomplete_consensus_deferral": (
        "Incomplete-consensus deferral exceeded its cap. Stop deferring and "
        "escalate the blocked consensus to an operator HITL."
    ),
    "cost_anomaly": (
        "LLM cost breached its envelope — either the hourly budget "
        "(max_llm_cost_per_hour) or an anomalous cost-per-token spike. Throttle or "
        "pause the offending agent tier and check for a model-tier misroute or a "
        "runaway prompt."
    ),
    "llm_substrate_unreachable": (
        "The LiteLLM proxy is unreachable (#2769). Verify the proxy pod and "
        "upstream routing before respawning agents."
    ),
    "effective_model_drift": (
        "The effective served model differs from the requested model. Inspect "
        "LiteLLM routing / fallbacks."
    ),
    "anthropic_5xx": (
        "Sustained Anthropic 5xx errors. Back off and retry; surface to the "
        "operator if the upstream outage persists."
    ),
}

_DEFAULT_REMEDIATION = "Investigate the agent logs and pipeline state"


def remediation_for_finding_class(
    finding_class: str,
    default: str = _DEFAULT_REMEDIATION,
) -> str:
    """Return the paste-ready remediation for a finding class, or ``default``."""
    return FINDING_CLASS_REMEDIATIONS.get(finding_class, default)


# Labels applied to diagnostic issues filed by the overseer.
# DEAD: production filing uses agent:overseer + matching priority label
# only (per decision-7 / decision-17 / feedback-1.Q4); this list is
# preserved only to keep the historical import graph intact.
DIAGNOSTIC_LABELS = ["egg:diagnostic", "pipeline-health"]


# Pre-#1962 canonical body literal — preserved verbatim for the
# byte-equality regression test (TASK-7-1) that the planner specified
# at "the literal at orchestrator/overseer/issue_filer.py:86-107".
# The runtime code path now uses TEMPLATE_LITERAL.format(...) below;
# this constant is the historical anchor against which the regression
# test asserts the canonical bytes have not drifted. The canonical
# template extends this with a Pipeline Links sub-block per
# decision-8 opt-2 — see egg_overseer.issue_template.TEMPLATE_LITERAL
# for the live template, and tests for byte-equality of this prefix
# against the live template's leading region.
LEGACY_BODY_LITERAL: str = """## Pipeline Diagnostic: {anomaly_type}

**Pipeline**: `{pipeline_id}`
**Phase**: `{phase}`
**Agent**: `{agent_role}`
**Detected**: `{timestamp}`

### Anomaly
{anomaly_description}

### Timeline
{timeline_lines}

### Classification
{classification_lines}

### Actions Taken
{actions_lines}
{container_logs_section}
### Suggested Remediation
- {remediation}
"""


class IssueDedupLedger:
    """Two-tier dedup gate for diagnostic issue filing (#2270 §5).

    Hardens against duplicate diagnostic issues with two independent tiers:

    * **Tier 1 (coarse, time-windowed):** suppress a repeat for the same
      ``(anomaly_type, agent_role)`` within ``window_seconds`` — the common case
      of a persistent anomaly re-detected every poll cycle.
    * **Tier 2 (fine, content-addressed):** suppress an *exact-duplicate* issue
      body via a sha256 of the rendered body, regardless of the window — so a
      byte-identical issue is never filed twice even after the Tier-1 window
      lapses.

    Both tiers must pass for :meth:`should_file` to return ``True``; a True
    result records the key in both tiers. Deterministic given an injected
    ``clock`` so it is unit-testable.
    """

    def __init__(
        self,
        window_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.window_seconds = window_seconds
        self._clock = clock
        self._tier1_seen: dict[tuple[str, str], float] = {}
        self._tier2_hashes: set[str] = set()

    @staticmethod
    def _body_hash(body: str) -> str:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def should_file(self, *, anomaly_type: str, agent_role: str, body: str) -> bool:
        """Return True if this issue is novel under BOTH dedup tiers.

        Records the key in both tiers when it returns True; a suppressed
        (False) call records nothing, so the gate is idempotent under repeats.
        """
        now = self._clock()
        body_hash = self._body_hash(body)

        # Tier 2: exact-body duplicate, ever. (Intentionally unbounded — the
        # content-addressed guarantee is "a byte-identical issue is never filed
        # twice, even after the Tier-1 window lapses", so these are not pruned.)
        if body_hash in self._tier2_hashes:
            return False

        # Tier 1: same (type, role) within the window. Opportunistically prune
        # expired Tier-1 keys first so a long-lived ledger's memory stays
        # bounded by the number of *currently-windowed* anomalies, not the
        # all-time count. Pruning an expired key is outcome-neutral: it would
        # pass the window check anyway.
        expired = [k for k, ts in self._tier1_seen.items() if (now - ts) >= self.window_seconds]
        for k in expired:
            del self._tier1_seen[k]

        key = (str(anomaly_type), str(agent_role))
        last = self._tier1_seen.get(key)
        if last is not None and (now - last) < self.window_seconds:
            return False

        self._tier1_seen[key] = now
        self._tier2_hashes.add(body_hash)
        return True

    def forget(self, *, anomaly_type: str, agent_role: str, body: str) -> None:
        """Roll back a recording made by an admitting :meth:`should_file`.

        Called when a filing the gate *admitted* (``should_file`` returned
        True) subsequently fails — e.g. ``gh issue create`` returns non-zero,
        times out, or ``gh`` is missing. Without this, the failed attempt would
        leave the anomaly recorded in both tiers and suppress every retry for
        the rest of the Tier-1 window, silently dropping the diagnostic. By
        forgetting exactly what the admitting call recorded, the next poll
        cycle re-evaluates the anomaly so it can be retried until actually
        filed.

        Precisely reverses the admitting call: that call only returns True when
        the Tier-1 key was absent/expired and the body hash was novel, so both
        entries removed here were added by it (never a still-live prior record).
        """
        key = (str(anomaly_type), str(agent_role))
        self._tier1_seen.pop(key, None)
        self._tier2_hashes.discard(self._body_hash(body))

    def reset(self) -> None:
        """Clear both dedup tiers (e.g. on orchestrator generation reset)."""
        self._tier1_seen.clear()
        self._tier2_hashes.clear()


def _build_issue_body(
    pipeline_id: str,
    agent_role: str,
    anomaly: dict,
    context: dict,
) -> str:
    """Build a markdown issue body following the diagnostic format.

    Args:
        pipeline_id: The pipeline identifier.
        agent_role: The agent role experiencing the issue.
        anomaly: Dict with anomaly details (type, description, classification, etc.).
        context: Dict with additional context (phase, timeline, actions taken, etc.).

    Returns:
        A markdown-formatted issue body string.
    """
    phase = context.get("phase", "unknown")
    timestamp = context.get("detected_at", datetime.now(UTC).isoformat())

    anomaly_type = anomaly.get("type", "unknown")
    anomaly_description = anomaly.get("description", "No description provided")

    # Build timeline section
    timeline_entries = context.get("timeline", [])
    if timeline_entries:
        timeline_lines = "\n".join(
            f"- `{entry.get('timestamp', '?')}`: {entry.get('event', '?')}"
            for entry in timeline_entries
        )
    else:
        timeline_lines = "- No timeline events recorded"

    # Build classification section
    classification = anomaly.get("classification", {})
    if classification:
        classification_lines = (
            f"- **Type**: {classification.get('type', anomaly_type)}\n"
            f"- **Confidence**: {classification.get('confidence', 'N/A')}\n"
            f"- **Reasoning**: {classification.get('reasoning', 'N/A')}"
        )
    else:
        classification_lines = "- No classification data available"

    # Build actions taken section
    actions_taken = context.get("actions_taken", [])
    if actions_taken:
        actions_lines = "\n".join(f"- {action}" for action in actions_taken)
    else:
        actions_lines = "- No corrective actions taken yet"

    # Build suggested remediation
    remediation = context.get(
        "suggested_remediation",
        anomaly.get("recommended_action", "Investigate the agent logs and pipeline state"),
    )

    # Build container logs section (if available)
    container_logs_section = ""
    raw_container_logs = context.get("container_logs", "")
    if raw_container_logs:
        # Truncate to last 2000 chars to keep issue body manageable
        truncated = raw_container_logs[-2000:]
        if len(raw_container_logs) > 2000:
            truncated = f"... (truncated, showing last 2000 chars)\n{truncated}"
        container_logs_section = f"\n\n### Container Logs\n````\n{truncated}\n````\n"

    # DEAD CODE — preserved byte-for-byte as the canonical literal
    # source for the regression test. Production rendering uses
    # egg_overseer.issue_template.render(**fields). The fields below are
    # the historical no-link dialect; the new "Pipeline Links" sub-block
    # the canonical TEMPLATE_LITERAL adds is rendered with placeholder
    # values so the literal substring at lines below matches the body
    # the canonical render produces when the same fields are passed.
    branch = context.get("branch", "unknown")
    commit_sha = context.get("commit_sha", "unknown")
    parent_alert_message_id = context.get("parent_alert_message_id", "unknown")
    branch_url = context.get("branch_url", f"https://github.com/unknown/tree/{branch}")

    body = TEMPLATE_LITERAL.format(
        anomaly_type=anomaly_type,
        pipeline_id=pipeline_id,
        phase=phase,
        agent_role=agent_role,
        timestamp=timestamp,
        anomaly_description=anomaly_description,
        timeline_lines=timeline_lines,
        classification_lines=classification_lines,
        actions_lines=actions_lines,
        container_logs_section=container_logs_section,
        remediation=remediation,
        branch_url=branch_url,
        branch=branch,
        commit_sha=commit_sha,
        parent_alert_message_id=parent_alert_message_id,
    )
    return body


async def file_diagnostic_issue(
    pipeline_id: str,
    agent_role: str,
    anomaly: dict,
    context: dict,
    *,
    dedup_ledger: IssueDedupLedger | None = None,
) -> dict:
    """File a diagnostic GitHub issue for a persistent problem.

    DEAD CODE — production filing happens sandbox-side via
    ``egg-orch overseer file-issue`` (issue #1962). This function is
    retained only to keep the historical import graph intact.

    Builds a structured issue body and files it via ``gh issue create``.
    If the ``gh`` CLI is not available or the command fails, the issue
    template is still returned so it can be logged or sent via other
    channels.

    Args:
        pipeline_id: The pipeline identifier.
        agent_role: The agent role experiencing the issue.
        anomaly: Dict with anomaly details.
        context: Dict with additional context.
        dedup_ledger: Optional :class:`IssueDedupLedger`. When supplied and the
            issue is a duplicate under either dedup tier, filing is skipped and
            a ``deduplicated`` result is returned. ``None`` (default) preserves
            the legacy always-file behaviour.

    Returns:
        A dict with keys:
            issue_number: int | None (None if filing failed / deduplicated)
            filed: bool
            template: str (the issue body markdown)
            deduplicated: bool (present and True when suppressed by the ledger)
    """
    anomaly_type = anomaly.get("type", "unknown")
    title = f"[Pipeline Diagnostic] {anomaly_type} - {agent_role} ({pipeline_id})"
    body = _build_issue_body(pipeline_id, agent_role, anomaly, context)

    if dedup_ledger is not None and not dedup_ledger.should_file(
        anomaly_type=anomaly_type, agent_role=agent_role, body=body
    ):
        logger.info(
            "Diagnostic issue deduplicated (type=%s, agent=%s, pipeline=%s)",
            anomaly_type,
            agent_role,
            pipeline_id,
        )
        return {
            "issue_number": None,
            "filed": False,
            "template": body,
            "deduplicated": True,
        }

    # Build label arguments
    label_args: list[str] = []
    for label in DIAGNOSTIC_LABELS:
        label_args.extend(["--label", label])

    def _rollback_dedup() -> None:
        # The gate admitted this filing above (should_file recorded it); if the
        # filing now fails, forget that record so the next poll cycle retries
        # rather than treating the anomaly as already-filed for the window.
        if dedup_ledger is not None:
            dedup_ledger.forget(anomaly_type=anomaly_type, agent_role=agent_role, body=body)

    try:
        cmd = [
            "gh",
            "issue",
            "create",
            "--title",
            title,
            "--body",
            body,
            *label_args,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
        stdout_text = (stdout_bytes or b"").decode()
        stderr_text = (stderr_bytes or b"").decode()

        if proc.returncode == 0:
            # Parse issue number from URL output (e.g. https://github.com/org/repo/issues/123)
            url = stdout_text.strip()
            issue_number: int | None = None
            if url and "/" in url:
                try:
                    issue_number = int(url.rstrip("/").rsplit("/", 1)[-1])
                except ValueError, IndexError:
                    pass
            logger.info(
                "Filed diagnostic issue #%s for pipeline %s agent %s",
                issue_number,
                pipeline_id,
                agent_role,
            )
            return {"issue_number": issue_number, "filed": True, "template": body}

        logger.warning(
            "gh issue create failed (rc=%d): %s",
            proc.returncode,
            stderr_text,
        )
        _rollback_dedup()
        return {"issue_number": None, "filed": False, "template": body}

    except FileNotFoundError:
        logger.warning("gh CLI not found; cannot file diagnostic issue")
        _rollback_dedup()
        return {"issue_number": None, "filed": False, "template": body}

    except TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        logger.warning("gh issue create timed out")
        _rollback_dedup()
        return {"issue_number": None, "filed": False, "template": body}

    except Exception as exc:
        logger.warning("Failed to file diagnostic issue: %s", exc)
        _rollback_dedup()
        return {"issue_number": None, "filed": False, "template": body}
