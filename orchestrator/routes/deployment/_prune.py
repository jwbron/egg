"""``prune_stale_worktrees`` orchestrator->gateway proxy (#3312).

Thin proxy to the gateway's worktree-prune endpoint. The gateway owns the
filesystem mutation and its in-process mutex; the orchestrator layer exists
only to enforce ``@require_lifecycle_secret`` (parity with #1769) and to
shield agents from the launcher secret needed to call the gateway directly.
"""

from __future__ import annotations

from flask import Response, jsonify, request


def prune_worktrees_proxy() -> tuple[Response, int]:
    """Proxy to the gateway's worktree-prune endpoint.

    The gateway owns the filesystem mutation and its in-process mutex.
    The orchestrator layer is kept to enforce
    ``@require_lifecycle_secret`` (parity with #1769) and to shield
    agents from the launcher-secret needed to call the gateway
    directly.
    """
    try:
        from gateway_client import GatewayError, get_gateway_client
    except Exception as exc:  # pragma: no cover - wiring guard
        return jsonify({"success": False, "message": f"gateway unavailable: {exc}"}), 503

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", True))

    client = get_gateway_client()
    try:
        # _make_request is private but the simplest integration point;
        # all other gateway methods go through it.
        result = client._make_request(  # noqa: SLF001
            "/api/v1/worktrees/prune",
            method="POST",
            data={"dry_run": dry_run},
            use_launcher_auth=True,
            timeout=120,
        )
    except GatewayError as exc:
        status = getattr(exc, "status_code", 502) or 502
        return jsonify({"success": False, "message": str(exc)}), status
    except Exception as exc:
        return jsonify({"success": False, "message": f"gateway error: {exc}"}), 502

    return jsonify({"success": True, "data": result.get("data", result)}), 200
