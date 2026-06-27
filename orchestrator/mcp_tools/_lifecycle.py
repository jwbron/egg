"""PipelineToolHandler agent/phase restart, salvage, advance/start/complete lifecycle handlers (#3312 slice-13).

Method bodies extracted verbatim from the pre-split
``orchestrator/mcp_tools.py`` and bound onto ``PipelineToolHandler``
in the package barrel (``orchestrator/mcp_tools/__init__.py``). They
take ``self`` explicitly and are AST-identical to the originals.
Barrel globals (``logger`` etc.) are imported from the package so
they stay a single binding.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from mcp_tools import (
    _is_timeout_error,
    logger,
)


def _handle_restart_agent(self, args: dict[str, Any]) -> dict[str, Any]:
    """Restart a single agent in a pipeline."""
    task_id = quote(args["task_id"], safe="")
    agent_role = quote(args["agent_role"], safe="")
    data: dict[str, Any] = {}
    if args.get("reason"):
        data["reason"] = args["reason"]
    # Forward ``slice_id`` so a per-slice agent restart targets the
    # slice's Job, worktree, and BRC tracker (#2759). When omitted the
    # route auto-derives it from the phase's agent records, but on an
    # ambiguous restart the route rejects and asks for an explicit
    # value — which is unreachable unless the MCP tool can carry it.
    if args.get("slice_id"):
        data["slice_id"] = args["slice_id"]
    try:
        result = self._make_request(
            f"/api/v1/pipelines/{task_id}/agents/{agent_role}/restart",
            method="POST",
            data=data,
            timeout=60,
        )
        return {
            "restarted": True,
            "agent_role": args["agent_role"],
            "container_id": result.get("data", {}).get("container_id", ""),
            "message": f"Agent {args['agent_role']} restarted successfully",
        }
    except (TimeoutError, OSError) as e:
        if isinstance(e, OSError) and not _is_timeout_error(e):
            return {"error": f"Failed to restart agent: {e}"}
        # Server-side restart is likely still in progress (#1594).
        return {
            "restarted": "pending",
            "agent_role": args["agent_role"],
            "message": (
                f"Restart of agent {args['agent_role']} accepted but timed out "
                "waiting for confirmation. The restart is likely still in "
                "progress. Use get_status to check."
            ),
        }
    except Exception as e:
        return {"error": f"Failed to restart agent: {e}"}


def _handle_restart_phase(self, args: dict[str, Any]) -> dict[str, Any]:
    """Restart all agents in a pipeline phase."""
    task_id = quote(args["task_id"], safe="")
    phase = quote(args["phase"], safe="")
    data: dict[str, Any] = {}
    if args.get("reason"):
        data["reason"] = args["reason"]
    try:
        result = self._make_request(
            f"/api/v1/pipelines/{task_id}/phases/{phase}/restart",
            method="POST",
            data=data,
            timeout=120,
        )
        return {
            "restarted": True,
            "phase": args["phase"],
            # API response uses future-tense "agents_to_restart" (not yet spawned);
            # MCP interface uses past-tense "agents_restarted" for caller convenience.
            "agents_restarted": result.get("data", {}).get("agents_to_restart", []),
            "message": f"Phase {args['phase']} restarted successfully",
        }
    except (TimeoutError, OSError) as e:
        if isinstance(e, OSError) and not _is_timeout_error(e):
            return {"error": f"Failed to restart phase: {e}"}
        # Server-side restart is likely still in progress (#1594).
        return {
            "restarted": "pending",
            "phase": args["phase"],
            "message": (
                f"Restart of phase {args['phase']} accepted but timed out "
                "waiting for confirmation. The restart is likely still in "
                "progress. Use get_status to check."
            ),
        }
    except Exception as e:
        return {"error": f"Failed to restart phase: {e}"}


def _handle_list_agent_local_commits(self, args: dict[str, Any]) -> dict[str, Any]:
    """List unpushed commits in this pipeline's per-agent worktrees (#2429)."""
    task_id = quote(args["task_id"], safe="")
    params: list[str] = []
    if args.get("agent_role"):
        params.append(f"agent_role={quote(args['agent_role'], safe='')}")
    if args.get("slice_id"):
        params.append(f"slice_id={quote(args['slice_id'], safe='')}")
    suffix = f"?{'&'.join(params)}" if params else ""
    try:
        result = self._make_request(
            f"/api/v1/pipelines/{task_id}/local-commits{suffix}",
        )
    except Exception as e:
        return {"error": f"Failed to list local commits: {e}"}

    data = result.get("data", {}) if isinstance(result, dict) else {}
    worktrees = data.get("worktrees", [])
    n_commits = sum(len(wt.get("commits") or []) for wt in worktrees)
    return {
        "pipeline_id": data.get("pipeline_id", args["task_id"]),
        "n_worktrees": len(worktrees),
        "n_commits": n_commits,
        "worktrees": worktrees,
    }


def _handle_salvage_agent_commits(self, args: dict[str, Any]) -> dict[str, Any]:
    """Push unpushed agent commits to recovery refs (#2429)."""
    task_id = quote(args["task_id"], safe="")
    params: list[str] = []
    if args.get("agent_role"):
        params.append(f"agent_role={quote(args['agent_role'], safe='')}")
    if args.get("slice_id"):
        params.append(f"slice_id={quote(args['slice_id'], safe='')}")
    suffix = f"?{'&'.join(params)}" if params else ""
    try:
        result = self._make_request(
            f"/api/v1/pipelines/{task_id}/salvage{suffix}",
            method="POST",
            data={},
            # Push goes through the gateway with launcher auth and may
            # block on git for a few seconds per worktree.
            timeout=120,
        )
    except Exception as e:
        return {"error": f"Failed to salvage commits: {e}"}

    data = result.get("data", {}) if isinstance(result, dict) else {}
    results = data.get("results", [])
    salvaged = [r for r in results if r.get("ok") and r.get("recovery_ref")]
    failed = [r for r in results if not r.get("ok")]
    return {
        "pipeline_id": data.get("pipeline_id", args["task_id"]),
        "n_worktrees": len(results),
        "n_salvaged": len(salvaged),
        "n_failed": len(failed),
        "recovery_refs": [r["recovery_ref"] for r in salvaged],
        "results": results,
    }


def _handle_advance_phase(self, args: dict[str, Any]) -> dict[str, Any]:
    """Advance pipeline to a target phase.

    When force=true, stops all running containers before advancing
    to prevent SIGTERM cascading into the new phase (#1570).
    """
    from models import PipelinePhase

    task_id = quote(args["task_id"], safe="")
    target_phase = args["target_phase"]
    force = args.get("force", False)

    # Validate target_phase up front so an invalid value fails fast
    # without first tearing down containers. See #1755.
    try:
        PipelinePhase(target_phase)
    except ValueError:
        valid = [p.value for p in PipelinePhase]
        return {"error": (f"Invalid target_phase: {target_phase!r}. Valid phases: {valid}")}

    # When force=true, stop running containers before the transition
    # to avoid SIGTERM cascading into the new phase.
    stopped_containers: list[str] = []
    failed_containers: list[str] = []
    if force:
        try:
            containers_result = self._make_request(
                f"/api/v1/pipelines/{task_id}/containers?all=false"
            )
            containers = containers_result.get("data", {}).get("containers", [])
            for container in containers:
                cid = container.get("container_id", "")
                if cid and container.get("status") == "running":
                    try:
                        self._make_request(
                            f"/api/v1/pipelines/{task_id}/containers/{quote(cid, safe='')}/stop",
                            method="POST",
                            timeout=30,
                        )
                        stopped_containers.append(cid)
                    except Exception:
                        logger.warning(
                            "Failed to stop container before force-advance",
                            pipeline_id=args["task_id"],
                            container_id=cid,
                        )
                        failed_containers.append(cid)
        except Exception:
            logger.warning(
                "Failed to list containers before force-advance",
                pipeline_id=args["task_id"],
            )

    data: dict[str, Any] = {"target_phase": target_phase, "force": force}
    try:
        result = self._make_request(
            f"/api/v1/pipelines/{task_id}/phase",
            method="POST",
            data=data,
        )
    except Exception as e:
        error_result: dict[str, Any] = {"error": f"Phase advance failed: {e}"}
        if stopped_containers:
            error_result["stopped_containers"] = stopped_containers
        if failed_containers:
            error_result["failed_containers"] = failed_containers
        return error_result
    if stopped_containers:
        result["stopped_containers"] = stopped_containers
    if failed_containers:
        result["failed_containers"] = failed_containers
    return result


def _handle_start_pipeline(self, args: dict[str, Any]) -> dict[str, Any]:
    """Recover a non-RUNNING pipeline (#2411).

    Targets the pipeline-level recovery route ``POST
    /api/v1/pipelines/{id}/start``.  See the ``start_pipeline`` tool
    definition in :data:`PIPELINE_TOOLS` for the full contract,
    including the FAILED + RUNNING-phase combo from startup
    reconciliation that this verb exists to recover from, and the
    live-pod safety guard added in #2420 (pass ``force=true`` to
    override).
    """
    task_id = quote(args["task_id"], safe="")
    data: dict[str, Any] = {}
    if args.get("force"):
        data["force"] = True
    if args.get("force_reason"):
        data["force_reason"] = args["force_reason"]
    return self._make_request(
        f"/api/v1/pipelines/{task_id}/start",
        method="POST",
        data=data if data else None,
    )


def _handle_start_phase(self, args: dict[str, Any]) -> dict[str, Any]:
    """Start execution of the current phase."""
    task_id = quote(args["task_id"], safe="")
    return self._make_request(
        f"/api/v1/pipelines/{task_id}/phase/start",
        method="POST",
    )


def _handle_complete_phase(self, args: dict[str, Any]) -> dict[str, Any]:
    """Mark the current phase as complete."""
    task_id = quote(args["task_id"], safe="")
    data: dict[str, Any] = {}
    if args.get("artifacts"):
        data["artifacts"] = args["artifacts"]
    if args.get("force"):
        data["force"] = True
    if args.get("force_reason"):
        data["force_reason"] = args["force_reason"]
    return self._make_request(
        f"/api/v1/pipelines/{task_id}/phase/complete",
        method="POST",
        data=data if data else None,
    )
