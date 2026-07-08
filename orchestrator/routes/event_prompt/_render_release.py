"""Park-release delta section (#3537).

Renders WHAT changed while a no-op-parked arm was parked — the resolved
contract decisions (with their resolution text), any freshly-gating
decisions, and BRC movement — so the released probe spawn acts on the
change instead of replaying its cached "still blocked" conclusion. The
delta arrives via the ``EGG_EVENT_RELEASE_CONTEXT`` pod env, injected by
``concurrent_executor._ExecutorEventSpawner.spawn_event`` only on the
probe spawn a fingerprint-change release granted (never on ordinary
spawns or heartbeat releases).
"""

from __future__ import annotations

from typing import Any

from ._caps import RELEASE_RESOLUTION_MAX_CHARS, _truncate


def _render_release_context_section(release_context: dict[str, Any] | None) -> str:
    """Render the #3537 park-release delta, or ``""`` when absent.

    ``release_context`` is the decoded ``EGG_EVENT_RELEASE_CONTEXT`` JSON:
    ``resolved_decision_ids`` / ``newly_gating_decision_ids`` (id lists),
    ``brc_moved`` (bool), and optionally ``resolved_decisions`` — the
    enriched ``{id, question, resolved, resolution, resolved_by}`` detail
    dicts. Ids without a matching detail entry still render (the decision
    may have been removed from the contract rather than resolved).
    """
    if not release_context:
        return ""
    resolved_ids = [str(i) for i in release_context.get("resolved_decision_ids") or []]
    newly_gating = [str(i) for i in release_context.get("newly_gating_decision_ids") or []]
    brc_moved = bool(release_context.get("brc_moved"))
    details_by_id: dict[str, dict[str, Any]] = {}
    for detail in release_context.get("resolved_decisions") or []:
        if isinstance(detail, dict) and detail.get("id"):
            details_by_id[str(detail["id"])] = detail
    if not (resolved_ids or newly_gating or brc_moved):
        return ""

    lines: list[str] = [
        "## Why you were respawned: the state you were blocked on CHANGED",
        "",
        "The orchestrator parked this arm after repeated no-op invocations "
        "and has now released it because the following changed since your "
        "last invocation:",
        "",
    ]
    for decision_id in resolved_ids:
        detail = details_by_id.get(decision_id)
        if detail is None:
            lines.append(
                f"- Contract decision ``{decision_id}`` is NO LONGER in the "
                "unresolved set (resolved by the operator, or removed from "
                "the contract). Re-read the contract for its current state."
            )
            continue
        resolved_by = str(detail.get("resolved_by") or "operator")
        resolution = str(detail.get("resolution") or "").strip()
        question = str(detail.get("question") or "").strip()
        lines.append(f"- Contract decision ``{decision_id}`` has been RESOLVED by {resolved_by}.")
        if question:
            lines.append(f"  - Question: {_truncate(question, RELEASE_RESOLUTION_MAX_CHARS)}")
        if resolution:
            lines.append(f"  - Resolution: {_truncate(resolution, RELEASE_RESOLUTION_MAX_CHARS)}")
    for decision_id in newly_gating:
        lines.append(
            f"- Contract decision ``{decision_id}`` became unresolved while "
            "you were parked — a NEW gating question, not the one you were "
            "blocked on."
        )
    if brc_moved:
        lines.append(
            "- The BRC consensus state moved while you were parked (a peer "
            "proposed, reviewed, or confirmed)."
        )
    lines.extend(
        [
            "",
            "Treat this delta as ground truth over any cached conclusion or "
            "durable-memory note that says you are still blocked — those "
            "were written BEFORE this change. Verify the current state via "
            "READS (the contract file, ``mcp__sdlc__show_contract``, "
            "``egg-orch`` status queries), NEVER by retrying a "
            "side-effectful call that previously failed: such a call can "
            "return the identical error whether or not the blocker cleared, "
            "so it cannot observe recovery. If your durable memory encodes "
            "a probe-by-retry plan, supersede it now.",
            "",
        ]
    )
    return "\n".join(lines)
