"""PipelineToolHandler deployment-context / manifest / network / logs / rebuild handlers (#3312 slice-13).

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


def _handle_get_deployment_context(self, args: dict[str, Any]) -> dict[str, Any]:
    """Return runtime/cluster introspection.

    Always returns a data dict.  On Docker, the response carries a
    degraded placeholder payload with ``runtime: "docker"`` and
    ``detection_source`` indicating provenance.  The k8s-gated routes
    (not this one) use the ``not_available_on_runtime`` / ``runtime_detection_failed``
    error pattern.
    """
    try:
        result = self._make_request("/api/v1/deployment/context", method="GET")
    except Exception as exc:
        return {"error": f"get_deployment_context failed: {exc}"}
    return result.get("data", result)


def _handle_validate_deployment_manifests(self, args: dict[str, Any]) -> dict[str, Any]:
    """Static validation of the committed kustomize overlay."""
    data: dict[str, Any] = {}
    if args.get("overlay_path"):
        data["overlay_path"] = args["overlay_path"]
    try:
        result = self._make_request(
            "/api/v1/deployment/validate-manifests",
            method="POST",
            data=data,
            timeout=90,
        )
    except Exception as exc:
        return {"error": f"validate_deployment_manifests failed: {exc}"}
    return result.get("data", result)


def _handle_prune_stale_worktrees(self, args: dict[str, Any]) -> dict[str, Any]:
    """Proxy to /api/v1/deployment/prune-worktrees (gateway-backed).

    The schema accepts only ``dry_run`` — a ``repo`` scope argument
    was removed after the review in #1759 because the gateway helper
    (:py:func:`gateway.worktrees_prune`) always sweeps every repo
    under ``REPOS_BASE_DIR``; a silent-drop would mislead callers.
    """
    body: dict[str, Any] = {"dry_run": bool(args.get("dry_run", True))}
    try:
        result = self._make_request(
            "/api/v1/deployment/prune-worktrees",
            method="POST",
            data=body,
            timeout=120,
        )
    except Exception as exc:
        return {"error": f"prune_stale_worktrees failed: {exc}"}
    return result.get("data", result)


def _handle_validate_network_isolation(self, args: dict[str, Any]) -> dict[str, Any]:
    """Spawn the throwaway probe Job and return its JSON payload."""
    pipeline_id = args.get("pipeline_id")
    if not pipeline_id:
        return {"error": "pipeline_id is required"}
    body: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "role": args.get("role") or "coder",
    }
    try:
        result = self._make_request(
            "/api/v1/deployment/validate-network-isolation",
            method="POST",
            data=body,
            timeout=90,
        )
    except Exception as exc:
        return {"error": f"validate_network_isolation failed: {exc}"}
    return result.get("data", result)


def _handle_get_service_logs(self, args: dict[str, Any]) -> dict[str, Any]:
    """Fetch logs for the gateway or orchestrator Deployment."""
    import json
    from urllib.error import HTTPError

    service = args.get("service")
    if not service:
        return {"error": "service is required"}

    params: list[str] = [f"service={quote(str(service), safe='')}"]
    lines = args.get("lines")
    if lines is not None:
        params.append(f"lines={int(lines)}")
    since_seconds = args.get("since_seconds")
    if since_seconds is not None:
        params.append(f"since_seconds={int(since_seconds)}")
    # Server-side filters (#3032) — applied before truncation.
    pipeline_id = args.get("pipeline_id")
    if pipeline_id:
        params.append(f"pipeline_id={quote(str(pipeline_id), safe='')}")
    level = args.get("level")
    if level:
        params.append(f"level={quote(str(level), safe='')}")
    pattern = args.get("pattern")
    if pattern:
        params.append(f"pattern={quote(str(pattern), safe='')}")

    endpoint = "/api/v1/deployment/logs?" + "&".join(params)
    try:
        result = self._make_request(endpoint, method="GET", timeout=30)
    except HTTPError as exc:
        # Surface the orchestrator's structured error body — urllib's
        # default HTTPError.__str__ is just "HTTP Error N: <reason>",
        # which hides the message our route actually set. Before this
        # #1870 fix the caller saw only "HTTP Error 500: INTERNAL
        # SERVER ERROR" with no hint that the real cause was an RBAC
        # denial reading Deployments in egg-system.
        detail = ""
        try:
            raw = exc.read()
            resp_body = json.loads(raw.decode()) if raw else {}
            detail = resp_body.get("message") or ""
        except Exception:
            detail = ""
        if detail:
            return {"error": f"get_service_logs failed (HTTP {exc.code}): {detail}"}
        return {"error": f"get_service_logs failed: {exc}"}
    except Exception as exc:
        return {"error": f"get_service_logs failed: {exc}"}
    return result.get("data", result)


def _handle_rebuild_and_rollout(self, args: dict[str, Any]) -> dict[str, Any]:
    """Start a ``make redeploy`` and optionally wait for the terminal record."""
    import time
    from urllib.error import HTTPError

    wait = bool(args.get("wait", False))
    try:
        result = self._make_request(
            "/api/v1/deployment/rebuild-and-rollout",
            method="POST",
            data={},
            timeout=30,
        )
    except HTTPError as exc:
        try:
            import json as _json

            body = _json.loads(exc.read().decode())
        except Exception:
            body = {}
        # 409 ← rollout_already_in_progress. Surface as a structured
        # payload rather than an error so callers can branch on it.
        if exc.code == 409:
            data = body.get("data") or {}
            return {
                "error": "rollout_already_in_progress",
                "progress_stream_id": data.get("progress_stream_id"),
                "message": body.get("message", "rollout_already_in_progress"),
            }
        return {"error": f"rebuild_and_rollout failed (HTTP {exc.code})"}
    except Exception as exc:
        return {"error": f"rebuild_and_rollout failed: {exc}"}

    data = result.get("data") or {}
    # not_available_on_runtime / runtime_detection_failed short-circuit
    if data.get("error") in ("not_available_on_runtime", "runtime_detection_failed"):
        return data
    stream_id = data.get("progress_stream_id")
    if not stream_id:
        return data
    if not wait:
        return data

    # wait=true: long-poll until the stream reports done.
    deadline = time.time() + 15 * 60  # 15-minute hard cap
    since = 0
    terminal: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    while time.time() < deadline:
        try:
            poll = self._make_request(
                f"/api/v1/deployment/rebuild-and-rollout/streams/{quote(stream_id, safe='')}?since={since}",
                method="GET",
                timeout=30,
            )
        except Exception as exc:
            return {
                "error": f"stream poll failed: {exc}",
                "progress_stream_id": stream_id,
            }
        batch = (poll.get("data") or {}).get("events") or []
        since = (poll.get("data") or {}).get("next_since", since + len(batch))
        events.extend(batch)
        for event in batch:
            if event.get("phase") == "done":
                terminal = event
                break
        if terminal or (poll.get("data") or {}).get("done"):
            break
        time.sleep(2.0)

    payload = {
        "progress_stream_id": stream_id,
        "events": events,
    }
    if terminal:
        payload["terminal"] = terminal
        payload["exit_code"] = terminal.get("exit_code")
        payload["rolled_out_images"] = terminal.get("rolled_out_images") or {}
    else:
        payload["error"] = "wait_timeout"
    return payload
