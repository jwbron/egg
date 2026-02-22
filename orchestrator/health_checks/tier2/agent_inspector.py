"""
AgentInspectorCheck — Tier 2 semantic health check using Claude API.

Sends a structured prompt with pipeline context (git log, diff stats,
agent outputs, contract state) to the Claude API and parses a JSON
verdict.  Gracefully degrades to HEALTHY on API errors.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


import httpx
from health_checks.context import PipelineHealthContext
from health_checks.types import (
    HealthAction,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)

logger = get_logger("orchestrator.health_checks.tier2.agent_inspector")

# Default model for agent inspection
_DEFAULT_MODEL = "claude-sonnet-4-20250514"
# API timeout in seconds
_API_TIMEOUT = 30
# Max retries on transient failures
_MAX_RETRIES = 1

# System prompt for the inspector
_SYSTEM_PROMPT = """\
You are a pipeline health inspector. Analyze the provided context about a \
software engineering agent's work and produce a health verdict.

Respond ONLY with a JSON object (no markdown fencing, no extra text):
{
  "status": "HEALTHY" | "DEGRADED" | "FAILED",
  "reasoning": "<one-paragraph explanation>"
}

Rules:
- HEALTHY: Agent is making reasonable progress; commits present; no red flags.
- DEGRADED: Minor concerns — e.g. no recent commits, stale output, contract \
tasks still pending after a long time. Use this when there's risk but not \
certainty of failure.
- FAILED: Clear signs of stuck agent — e.g. repeated errors in outputs, \
no commits and no drafts, contradictory state.
- Be concise. One paragraph for reasoning.
"""


def _build_user_prompt(context: PipelineHealthContext) -> str:
    """Assemble the user prompt from pipeline context fields."""
    parts: list[str] = []

    parts.append(f"Pipeline: {context.pipeline_id}")
    parts.append(f"Phase: {context.current_phase.value}")
    parts.append(f"Branch: {context.branch or 'unknown'}")
    parts.append(f"Trigger: {context.trigger}")
    parts.append("")

    # Git log
    git_log = context.git_log
    if git_log:
        parts.append("## Recent Commits")
        parts.append(git_log)
        parts.append("")

    # Git diff stat
    diff_stat = context.git_diff_stat
    if diff_stat:
        parts.append("## Diff Stats (vs main)")
        parts.append(diff_stat)
        parts.append("")

    # Agent outputs (summarize keys + truncated content)
    outputs = context.agent_outputs
    if outputs:
        parts.append("## Agent Output Files")
        for name, content in outputs.items():
            parts.append(f"### {name}")
            # Cap individual output in the prompt to keep total manageable
            parts.append(content[:2000])
            parts.append("")
    else:
        parts.append("## Agent Output Files")
        parts.append("(none found)")
        parts.append("")

    # Contract state
    contract = context.contract
    if contract:
        parts.append("## Contract State")
        # Serialize key fields, not the entire blob
        summary: dict[str, Any] = {}
        for key in ("current_phase", "acceptance_criteria", "decisions", "agent_executions"):
            if key in contract:
                summary[key] = contract[key]
        parts.append(json.dumps(summary, indent=2, default=str)[:3000])
        parts.append("")

    return "\n".join(parts)


def _parse_verdict(text: str) -> tuple[HealthStatus, str]:
    """Parse Claude's JSON response into (status, reasoning).

    Returns (HEALTHY, warning) if parsing fails.
    """
    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (with optional language tag)
        nl = cleaned.find("\n")
        if nl != -1:
            cleaned = cleaned[nl + 1 :]
        else:
            cleaned = cleaned[3:]  # Strip just the backticks
    if cleaned.endswith("```"):
        cleaned = cleaned[: -len("```")]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return HealthStatus.HEALTHY, f"Could not parse verdict JSON: {text[:200]}"

    raw_status = str(data.get("status", "")).upper()
    reasoning = str(data.get("reasoning", "No reasoning provided"))

    status_map = {
        "HEALTHY": HealthStatus.HEALTHY,
        "DEGRADED": HealthStatus.DEGRADED,
        "FAILED": HealthStatus.FAILED,
    }
    status = status_map.get(raw_status, HealthStatus.HEALTHY)
    if raw_status not in status_map:
        reasoning = f"Unknown status '{raw_status}', defaulting to HEALTHY. {reasoning}"

    return status, reasoning


def _call_claude_api(
    user_prompt: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> str:
    """Call the Anthropic Messages API via httpx.

    Returns the text content of the first response block.
    Raises on HTTP or timeout errors (caller handles graceful degradation).
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    url = (base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")).rstrip(
        "/"
    )
    mdl = model or os.environ.get("HEALTH_CHECK_MODEL", _DEFAULT_MODEL)

    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "model": mdl,
        "max_tokens": 512,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = httpx.post(
                f"{url}/v1/messages",
                headers=headers,
                json=payload,
                timeout=_API_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            # Extract text from first content block
            content_blocks = body.get("content", [])
            if content_blocks and isinstance(content_blocks, list):
                return str(content_blocks[0].get("text", ""))
            return ""
        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "Claude API attempt failed, retrying",
                    attempt=attempt + 1,
                    error=str(exc),
                )
                continue
            raise
        except Exception:
            raise

    # Should not reach here, but satisfy type checker
    if last_exc:
        raise last_exc
    return ""


class AgentInspectorCheck:
    """Tier 2 health check that uses Claude to semantically inspect agent state.

    Sends pipeline context (git log, diff stats, agent outputs, contract)
    to the Claude API and interprets the structured verdict.

    On API failure, gracefully degrades to HEALTHY with a warning —
    Tier 2 failures should never block the pipeline.
    """

    name: str = "agent_inspector"
    tier: HealthTier = HealthTier.AGENT
    triggers: frozenset[HealthTrigger] = frozenset(
        {
            HealthTrigger.WAVE_COMPLETE,
            HealthTrigger.PHASE_COMPLETE,
            HealthTrigger.ON_DEMAND,
        }
    )

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Execute the agent inspection check."""
        try:
            user_prompt = _build_user_prompt(context)
            response_text = _call_claude_api(user_prompt)
            status, reasoning = _parse_verdict(response_text)

            logger.info(
                "Agent inspector verdict",
                status=status.value,
                pipeline=context.pipeline_id,
            )

            return HealthResult(
                status=status,
                check_name=self.name,
                tier=self.tier,
                reasoning=reasoning,
                action=HealthAction.ALERT
                if status != HealthStatus.HEALTHY
                else HealthAction.CONTINUE,
                details={"raw_response": response_text[:500]},
            )

        except Exception as exc:
            # Graceful degradation: API failure should not block pipeline
            logger.warning(
                "Agent inspector API call failed, degrading gracefully",
                error=str(exc),
                pipeline=context.pipeline_id,
            )
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning=f"Agent inspector unavailable ({type(exc).__name__}): {exc}",
                action=HealthAction.CONTINUE,
                details={"error": str(exc), "graceful_degradation": True},
            )
