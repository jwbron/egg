"""OverseerMonitor orchestrator/CLI query tier (#3312, slice-8)."""

from __future__ import annotations

import asyncio
import json

from . import logger


async def _run_cli(self, *args: str, timeout: float = 15) -> tuple[int, str, str]:
    """Run a CLI command asynchronously.

    Returns:
        Tuple of (returncode, stdout, stderr).
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "timeout"
    return proc.returncode or 0, (stdout_bytes or b"").decode(), (stderr_bytes or b"").decode()


async def _query_progress(self) -> list[dict]:
    """Query progress events from the orchestrator."""
    try:
        rc, stdout, _ = await self._run_cli(
            "egg-orch",
            "progress",
            "query",
            "--pipeline",
            self.pipeline_id,
            "--json",
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            return data if isinstance(data, list) else [data]
    except Exception:
        logger.debug("Failed to query progress events", exc_info=True)
    return []


async def _query_health_alerts(self) -> list[dict]:
    """Query active health alerts from the orchestrator."""
    try:
        rc, stdout, _ = await self._run_cli(
            "egg-orch",
            "health",
            "alerts",
            "--pipeline",
            self.pipeline_id,
            "--json",
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            return data if isinstance(data, list) else [data]
    except Exception:
        logger.debug("Failed to query health alerts", exc_info=True)
    return []


async def _poll_escalation_messages(self) -> list[dict]:
    """Poll for escalation messages directed to the overseer."""
    try:
        rc, stdout, _ = await self._run_cli(
            "egg-orch",
            "message",
            "poll",
            "--role",
            "overseer",
            "--wait",
            "5",
            "--json",
            timeout=20,
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and data.get("messages"):
                return data["messages"]
    except Exception:
        logger.debug("Failed to poll escalation messages", exc_info=True)
    return []


async def _query_pipeline_data(self) -> dict:
    """Query the full pipeline status data.

    Returns:
        Pipeline status dict, or empty dict on failure.
    """
    try:
        rc, stdout, _ = await self._run_cli(
            "egg-orch",
            "pipeline",
            "status",
            self.pipeline_id,
            "--json",
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("Failed to query pipeline status", exc_info=True)
    return {}


async def _query_consensus_status(self) -> dict:
    """Query current BRC consensus state from the orchestrator."""
    try:
        rc, stdout, _ = await self._run_cli(
            "egg-orch",
            "consensus",
            "status",
            "--pipeline",
            self.pipeline_id,
            "--json",
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("Failed to query consensus status", exc_info=True)
    return {}


async def _query_current_phase(self) -> dict:
    """Query current phase name and status from the orchestrator."""
    try:
        rc, stdout, _ = await self._run_cli(
            "egg-orch",
            "phase",
            "get",
            "--pipeline",
            self.pipeline_id,
            "--json",
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("Failed to query current phase", exc_info=True)
    return {}


async def _query_decisions(self) -> list[dict]:
    """Query all decisions (including resolved) for the pipeline."""
    try:
        rc, stdout, _ = await self._run_cli(
            "egg-orch",
            "decision",
            "list",
            "--pipeline",
            self.pipeline_id,
            "--json",
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and data.get("decisions"):
                return data["decisions"]
    except Exception:
        logger.debug("Failed to query decisions", exc_info=True)
    return []


async def _query_contract_data(self) -> dict:
    """Query SDLC contract state via egg-contract show."""
    try:
        rc, stdout, _ = await self._run_cli(
            "egg-contract",
            "show",
            "--json",
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("Failed to query contract data", exc_info=True)
    return {}


async def _query_container_list(self) -> list[dict]:
    """List containers for the pipeline via ``egg-orch container list``."""
    try:
        rc, stdout, _ = await self._run_cli(
            "egg-orch",
            "container",
            "list",
            self.pipeline_id,
            "--json",
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            if isinstance(data, dict):
                return data.get("data", {}).get("containers", [])
            if isinstance(data, list):
                return data
    except Exception:
        logger.debug("Failed to list containers", exc_info=True)
    return []


async def _query_container_logs(self, agent_role: str, tail: int = 200) -> str:
    """Fetch recent container logs for an agent role.

    Auto-selects the best container for the role: prefers running
    containers, then falls back to the most recently started one.

    Args:
        agent_role: The agent role whose container logs to fetch.
        tail: Number of log lines from the end (default 200).

    Returns:
        Log output as a string, or empty string on failure.
    """
    try:
        containers = await self._query_container_list()
        if not containers:
            return ""

        # Filter to containers matching the target agent role
        role_containers = [c for c in containers if c.get("agent_role") == agent_role]
        if not role_containers:
            return ""

        # Prefer running containers (newest first), then most recently started
        running = [c for c in role_containers if c.get("status") == "running"]
        if running:
            running.sort(key=lambda c: c.get("started_at", ""), reverse=True)
            selected = running[0]
        else:
            role_containers.sort(key=lambda c: c.get("started_at", ""), reverse=True)
            selected = role_containers[0]

        container_id = selected.get("container_id", "")
        if not container_id:
            return ""

        rc, stdout, _ = await self._run_cli(
            "egg-orch",
            "container",
            "logs",
            self.pipeline_id,
            container_id,
            "--lines",
            str(tail),
            "--json",
            timeout=30,
        )
        if rc == 0 and stdout.strip():
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                return stdout.strip()
            if isinstance(data, dict):
                return data.get("data", {}).get("logs", data.get("logs", ""))
            return stdout.strip()
    except Exception:
        logger.debug(
            "Failed to fetch container logs for %s",
            agent_role,
            exc_info=True,
        )
    return ""
