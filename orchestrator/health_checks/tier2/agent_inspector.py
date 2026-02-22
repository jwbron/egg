"""
AgentInspectorCheck — Tier 2 semantic health check via sandbox container.

Serializes pipeline context and delegates LLM analysis to a short-lived
sandbox container running ``egg-health-inspect``.  The orchestrator never
calls the Anthropic API directly — that happens inside the sandbox.

Gracefully degrades to HEALTHY on container errors.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
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


from health_checks.context import PipelineHealthContext
from health_checks.types import (
    HealthAction,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)

logger = get_logger("orchestrator.health_checks.tier2.agent_inspector")

# Timeout for the inspector container (seconds)
_CONTAINER_TIMEOUT = 60


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


def _serialize_context(context: PipelineHealthContext) -> dict[str, Any]:
    """Serialize PipelineHealthContext into a JSON-safe dict for the inspector."""
    # Build contract summary (filtered keys)
    contract = context.contract
    contract_summary: dict[str, Any] = {}
    if contract:
        for key in ("current_phase", "acceptance_criteria", "decisions", "agent_executions"):
            if key in contract:
                contract_summary[key] = contract[key]

    return {
        "pipeline_id": context.pipeline_id,
        "current_phase": context.current_phase.value,
        "branch": context.branch or "unknown",
        "trigger": context.trigger,
        "git_log": context.git_log,
        "git_diff_stat": context.git_diff_stat,
        "agent_outputs": context.agent_outputs,
        "contract_summary": contract_summary,
    }


def _run_inspector_container(
    context_payload: dict[str, Any],
    pipeline_id: str,
) -> str:
    """Spawn a sandbox container to run the inspector script.

    Returns the raw response text from the container's stdout.
    Raises on container spawn/wait/parse failures (caller handles graceful degradation).
    """
    from container_spawner import ContainerSpawner, ContainerSpawnError
    from docker_client import DockerClient, DockerClientError, get_docker_client
    from models import AgentRole

    # Write context to a temp file that will be mounted into the container
    context_json = json.dumps(context_payload, default=str)

    # Use the docker client and spawner
    docker: DockerClient = get_docker_client()
    spawner = ContainerSpawner(docker_client=docker)

    # Write context to temp file
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="inspector-ctx-",
        delete=False,
    ) as f:
        f.write(context_json)
        context_file = f.name

    try:
        # Spawn the inspector container
        spawned = spawner.spawn_agent_container(
            pipeline_id=pipeline_id,
            agent_role=AgentRole.INSPECTOR,
            mode=os.environ.get("EGG_NETWORK_MODE", "public"),
            extra_env={
                "EGG_INSPECTOR_CONTEXT_PATH": "/tmp/inspector-context.json",
            },
            command=["python3", "/home/egg/sandbox/bin/egg-health-inspect"],
            wait_for_gateway=False,
            repo_volumes={},
        )

        container_id = spawned.container_info.container_id

        try:
            # Wait for the container to exit
            exit_info = docker.wait_for_container(
                container_id,
                timeout=_CONTAINER_TIMEOUT,
            )

            # Get the container logs (stdout contains the JSON verdict)
            logs = docker.get_container_logs(container_id, tail=50)

            if exit_info.exit_code != 0:
                raise RuntimeError(
                    f"Inspector container exited with code {exit_info.exit_code}: "
                    f"{logs[-500:] if logs else '(no logs)'}"
                )

            return logs

        finally:
            # Clean up the container
            try:
                spawner.remove_agent_container(
                    container_id,
                    force=True,
                    cleanup_session=True,
                )
            except (DockerClientError, ContainerSpawnError):
                pass  # Best effort cleanup

    finally:
        # Clean up temp file
        try:
            os.unlink(context_file)
        except OSError:
            pass


def _parse_container_output(logs: str) -> str:
    """Extract the raw_response from container stdout JSON.

    The container outputs a JSON object like {"raw_response": "..."}.
    We extract the raw_response field for verdict parsing.
    """
    # Container logs may have timestamps prefixed — find the JSON line
    for line in reversed(logs.strip().splitlines()):
        # Strip Docker timestamp prefix if present (format: 2024-01-01T00:00:00.000000000Z ...)
        stripped = line.strip()
        if " " in stripped and stripped[0].isdigit():
            # Try stripping timestamp prefix
            _, _, after = stripped.partition(" ")
            if after.startswith("{"):
                stripped = after

        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
                return str(data.get("raw_response", ""))
            except (json.JSONDecodeError, ValueError):
                continue

    raise ValueError(f"No valid JSON found in container output: {logs[-300:]}")


class AgentInspectorCheck:
    """Tier 2 health check that delegates LLM analysis to a sandbox container.

    Serializes pipeline context and spawns a short-lived container running
    ``egg-health-inspect``, which calls the Claude API from inside the sandbox.

    On container failure, gracefully degrades to HEALTHY with a warning —
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
        """Execute the agent inspection check via sandbox container."""
        try:
            # Serialize context for the inspector container
            context_payload = _serialize_context(context)

            # Spawn container and get response
            logs = _run_inspector_container(
                context_payload=context_payload,
                pipeline_id=context.pipeline_id,
            )

            # Parse container output to get raw Claude response
            response_text = _parse_container_output(logs)

            # Parse the verdict from Claude's response
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
            # Graceful degradation: container failure should not block pipeline
            logger.warning(
                "Agent inspector container failed, degrading gracefully",
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
