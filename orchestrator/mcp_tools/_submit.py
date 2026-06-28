"""PipelineToolHandler submit-task / config-validation / contract-population handlers (#3312 slice-13).

Method bodies extracted verbatim from the pre-split
``orchestrator/mcp_tools.py`` and bound onto ``PipelineToolHandler``
in the package barrel (``orchestrator/mcp_tools/__init__.py``). They
take ``self`` explicitly and are AST-identical to the originals.
Barrel globals (``logger`` etc.) are imported from the package so
they stay a single binding.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from mcp_tools import logger


def _handle_submit_task(self, args: dict[str, Any]) -> dict[str, Any]:
    """Create an SDLC pipeline."""
    import json
    from urllib.error import HTTPError

    data: dict[str, Any] = {}
    qualifier = args.get("qualifier")

    # Validate qualifier: lowercase alphanumeric with hyphens only
    if qualifier and not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", qualifier):
        return {
            "error": f"Invalid qualifier '{qualifier}': must be lowercase alphanumeric segments separated by hyphens (e.g., 'backend', 'v2-hotfix')"
        }

    # Validate JIRA ticket format if provided
    if args.get("jira_ticket"):
        ticket_raw = args["jira_ticket"].strip()
        if not re.match(r"^[A-Za-z][A-Za-z0-9]+-[0-9]+$", ticket_raw):
            return {"error": f"Invalid JIRA ticket format '{ticket_raw}': expected e.g. PROJ-1234"}

    # Issue #1557: validate the new ``mode`` arg up front. Only
    # 'auto' / 'fresh' / 'reassess' are accepted; missing falls
    # back to 'auto'. Forwarded to the orchestrator API which
    # resolves the actual is_epic / pipeline_mode pair against the
    # ticket fetch.
    mode_arg = args.get("mode")
    if mode_arg is not None:
        if mode_arg not in ("auto", "fresh", "reassess"):
            return {
                "error": (
                    f"Invalid mode '{mode_arg}': must be one of "
                    "'auto', 'fresh', 'reassess' (issue #1557)"
                )
            }
        if not args.get("jira_ticket"):
            return {"error": ("mode is only meaningful with jira_ticket (issue #1557)")}

    # The required ``description`` always flows to the orchestrator as
    # the pipeline prompt — including for issue-backed submissions
    # (#3163). It feeds ``contract.task_description``, the channel the
    # per-event prompt's binding task section reads; dropping it for
    # issue pipelines left that section empty and agents anchored only
    # by whatever stale artifacts the worktree happened to carry.
    data["prompt"] = args["description"]
    if args.get("issue_number"):
        base_id = f"issue-{args['issue_number']}"
        if qualifier:
            base_id = f"{base_id}-{qualifier}"
        data["issue_number"] = args["issue_number"]
        data["pipeline_id"] = base_id
        data["branch"] = args.get("branch") or f"egg/{base_id}"
    elif args.get("jira_ticket"):
        ticket = args["jira_ticket"].upper()
        base_id = ticket
        if qualifier:
            base_id = f"{base_id}-{qualifier}"
        data["pipeline_id"] = base_id
        data["branch"] = args.get("branch") or f"egg/{base_id}"
    if args.get("repo"):
        data["repo"] = args["repo"]
    if args.get("config"):
        config = args["config"]
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError as e:
                return {"error": f"Invalid config JSON: {e}"}
        data["config"] = config
    if args.get("base_branch"):
        data["base_branch"] = args["base_branch"]
    if args.get("analysis"):
        data["analysis"] = args["analysis"]
    if args.get("plan"):
        data["plan"] = args["plan"]
    if args.get("source_branch"):
        data["source_branch"] = args["source_branch"]
    if args.get("source_artifact_prefix"):
        data["source_artifact_prefix"] = args["source_artifact_prefix"]
    # Issue #1557: forward jira_ticket + epic-mode override so the
    # orchestrator side can run epic detection and persist
    # ``is_epic`` / ``pipeline_mode`` on the Pipeline. The wire
    # field is named ``epic_mode`` to avoid colliding with the
    # existing ``mode`` field (PipelineMode: 'issue').
    if args.get("jira_ticket"):
        data["jira_ticket"] = args["jira_ticket"].upper()
    if mode_arg is not None:
        data["epic_mode"] = mode_arg

    try:
        # The create_pipeline route calls ls_remote_branch via the gateway,
        # which itself bounds at 30s.  We cap our request at 25s so the
        # MCP client (~30s streamable-HTTP deadline, see GET_STATUS_MAX_WAIT
        # in mcp_server.py) always sees a definite response or our own
        # timeout error within its budget, instead of the client giving
        # up first and the caller having to retry into a 409.
        result = self._make_request("/api/v1/pipelines", method="POST", data=data, timeout=25)
    except HTTPError as e:
        # Read the response body once upfront to avoid stream-exhaustion
        # issues if multiple branches need to inspect it.
        try:
            raw_body = e.read()
            resp_body = json.loads(raw_body.decode())
        except Exception:
            resp_body = {}

        if e.code == 409:
            # Parse enriched 409 body with existing pipeline details
            error_info: dict[str, Any] = {"error": "Pipeline already exists"}
            error_info["error"] = resp_body.get("message", error_info["error"])
            details = resp_body.get("details", {})
            if details:
                reason = details.get("reason")
                if reason:
                    error_info["reason"] = reason
                error_info["existing_pipeline_id"] = details.get("existing_pipeline_id", "")
                error_info["existing_status"] = details.get("existing_status", "")
                error_info["existing_phase"] = details.get("existing_phase", "")
            return error_info
        # For all other HTTP errors, include the API response body
        # so the actual error message is visible to callers (#1396).
        error_info = {"error": f"Pipeline creation failed (HTTP {e.code})"}
        error_info["error"] = resp_body.get("message", error_info["error"])
        return error_info

    pipeline_id = result.get("data", {}).get("pipeline", {}).get("id", "")

    if pipeline_id:
        try:
            self._make_request(
                f"/api/v1/pipelines/{quote(pipeline_id, safe='')}/start", method="POST"
            )
        except Exception:
            logger.error("Failed to start pipeline", pipeline_id=pipeline_id)
            return {
                "task_id": pipeline_id,
                "status": "created_not_started",
                "message": "Pipeline created but failed to start. Use task_id to retry.",
            }

    return {
        "task_id": pipeline_id,
        "status": "started",
        "message": f"Task submitted: {args['description'][:100]}",
    }


def _handle_validate_config(self, args: dict[str, Any]) -> dict[str, Any]:
    """Validate a pipeline configuration without creating a pipeline."""
    import json

    from models import PipelineConfig
    from pydantic import ValidationError

    config = args.get("config", {})
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError as e:
            return {
                "valid": False,
                "errors": [{"field": "config", "message": f"Invalid JSON: {e}"}],
            }

    try:
        validated = PipelineConfig.model_validate(config)
        return {
            "valid": True,
            "config": validated.model_dump(mode="json"),
        }
    except ValidationError as e:
        return {
            "valid": False,
            "errors": [
                {"field": ".".join(str(loc) for loc in err["loc"]), "message": err["msg"]}
                for err in e.errors()
            ],
        }


def _handle_update_pipeline_config(self, args: dict[str, Any]) -> dict[str, Any]:
    """Update a live pipeline's agent_models override (#3174)."""
    import json
    from urllib.error import HTTPError

    task_id = quote(args["task_id"], safe="")
    agent_models = args.get("agent_models")
    # Mirror validate_config / submit_task: tolerate a JSON-encoded
    # string from MCP clients that double-serialize object args.
    if isinstance(agent_models, str):
        try:
            agent_models = json.loads(agent_models)
        except json.JSONDecodeError as e:
            return {"error": f"agent_models is not valid JSON: {e}"}
    if not isinstance(agent_models, dict) or not agent_models:
        return {
            "error": (
                "agent_models must be a non-empty object mapping role -> "
                'model, e.g. {"coder": "deepseek-v4-pro"} (null clears a '
                "role's override)"
            )
        }

    try:
        result = self._make_request(
            f"/api/v1/pipelines/{task_id}/config",
            method="PATCH",
            data={"agent_models": agent_models},
        )
    except HTTPError as exc:
        # Surface the orchestrator's structured error body (role-key
        # validation, unknown pipeline) instead of urllib's bare
        # "HTTP Error N: <reason>".
        detail = ""
        try:
            raw = exc.read()
            body = json.loads(raw.decode()) if raw else {}
            detail = body.get("message") or ""
        except Exception:
            detail = ""
        if detail:
            return {"error": f"update_pipeline_config failed (HTTP {exc.code}): {detail}"}
        return {"error": f"update_pipeline_config failed: {exc}"}
    except Exception as exc:
        return {"error": f"update_pipeline_config failed: {exc}"}

    data = result.get("data", {})
    return {
        "updated": True,
        "pipeline_id": data.get("pipeline_id", args["task_id"]),
        "agent_models": data.get("agent_models", {}),
        "updated_roles": data.get("updated_roles", {}),
        "cleared_roles": data.get("cleared_roles", []),
        "note": (
            "Applies at the next agent spawn. Use restart_phase or "
            "restart_agent to apply to currently running agents; confirm "
            "via resolved_model in get_status / list_containers."
        ),
    }


def _handle_populate_contract(self, args: dict[str, Any]) -> dict[str, Any]:
    """Populate pipeline contract from plan draft."""
    task_id = quote(args["task_id"], safe="")
    return self._make_request(
        f"/api/v1/pipelines/{task_id}/phase/populate-contract",
        method="POST",
    )
