"""Orchestrator-side MCP tool surface for the advisor (issue #1962).

The agent-side overseer's Haiku-classify loop (sandbox/overseer_monitor.py)
invokes this tool when its anomaly classification intersects a Tier-1
health alert (decision-18 — Tier-1 intersection gate). The tool wraps
``shared.egg_overseer.advisor.consult_advisor`` so the actual SDK call
lives orchestrator-side and the sandbox doesn't have to ship the Opus
credentials directly.

Tool schema (JSON-Schema):

```
{
  "name": "consult_advisor",
  "description": "Consult the Opus advisor for a structured verdict on a Haiku-flagged anomaly.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "classification": { "type": "object" },
      "health_alerts": { "type": "array", "items": {"type": "object"} },
      "progress_events": { "type": "array", "items": {"type": "object"} },
      "recent_log_lines": { "type": "array", "items": {"type": "string"} }
    },
    "required": ["classification", "health_alerts"]
  }
}
```

Output: JSON-serialized ``AdvisorVerdict``.

Auth: this tool is gated to the ``overseer`` role only — the gateway
(or the MCP server's role-aware dispatcher, when wired in) MUST reject
calls from non-overseer agents.

Wiring into the FastMCP server: this module exposes
``CONSULT_ADVISOR_TOOL`` (the schema dict) and ``handle_consult_advisor``
(the async handler). Adding a registration entry to
``orchestrator/mcp_tools.py``'s ``PIPELINE_TOOLS`` and the
``PipelineToolHandler.handle_tool_call`` dispatch table will surface it
under the ``mcp__overseer__consult_advisor`` MCP tool name (the
sandbox-side overseer rule doc reflects that path).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


CONSULT_ADVISOR_TOOL: dict[str, Any] = {
    "name": "consult_advisor",
    "description": (
        "Consult the Opus advisor for a structured verdict on a "
        "Haiku-flagged anomaly. Returns AdvisorVerdict JSON with "
        "decision in {alert, file_issue, watch}, an optional priority, "
        "an alert summary/detail, and (when decision=file_issue) a "
        "fully-composed issue_title + issue_body that the human gates "
        "via the existing HITL flow. Auth-gated to the overseer role."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "classification": {
                "type": "object",
                "description": (
                    "Haiku's classification output (anomaly_type, confidence, reasoning)."
                ),
            },
            "health_alerts": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Active Tier-1 health alerts.",
            },
            "progress_events": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Last N progress events from the affected agent.",
            },
            "recent_log_lines": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Last K container log lines.",
            },
        },
        "required": ["classification", "health_alerts"],
    },
}


async def handle_consult_advisor(
    *,
    classification: dict[str, Any],
    health_alerts: list[dict[str, Any]],
    progress_events: list[dict[str, Any]] | None = None,
    recent_log_lines: list[str] | None = None,
    role: str | None = None,
    config: Any = None,
) -> dict[str, Any]:
    """Forward a ``consult_advisor`` MCP tool call to the shared advisor.

    Args:
        classification: Haiku classification dict.
        health_alerts: Tier-1 health alerts.
        progress_events: Last N progress events.
        recent_log_lines: Last K container log lines.
        role: Calling agent role (auth gate).
        config: ``PipelineConfig`` for ``overseer_advisor_model`` lookup.

    Returns:
        Dict with ``ok`` (bool) and either ``verdict`` (the
        AdvisorVerdict dict) or ``error`` (auth / parse failure).
    """
    if (role or "").lower() != "overseer":
        return {
            "ok": False,
            "error": (
                f"consult_advisor: only the 'overseer' role may call this tool (got role={role!r})"
            ),
        }

    from egg_overseer.advisor import AdvisorParseError, consult_advisor

    try:
        verdict = await consult_advisor(
            classification=classification,
            health_alerts=health_alerts,
            progress_events=progress_events or [],
            recent_log_lines=recent_log_lines or [],
            config=config,
        )
    except AdvisorParseError as exc:
        logger.warning("consult_advisor: parse failure: %s", exc)
        return {"ok": False, "error": f"parse_failure: {exc}"}

    return {"ok": True, "verdict": verdict.model_dump()}


__all__ = ["CONSULT_ADVISOR_TOOL", "handle_consult_advisor"]
