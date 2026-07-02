"""Gateway worktree create / delete (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

from typing import Any

from gateway_client import GatewayError
from gateway_client._models import WorktreeResult


def create_worktrees(
    self,
    container_id: str,
    repos: list[str],
    uid: int | None = None,
    gid: int | None = None,
    base_branch: str | None = None,
    assigned_branch: str | None = None,
    timeout: int = 120,
) -> WorktreeResult:
    """Create isolated worktrees for a container.

    Calls the gateway's /api/v1/worktree/create endpoint to create
    per-container worktrees. Returns host paths suitable for Docker
    volume mount sources.

    Args:
        container_id: Container identifier (e.g., 'egg-local-abc123-coder')
        repos: List of repository names (or owner/repo format)
        uid: User ID for worktree ownership
        gid: Group ID for worktree ownership
        base_branch: Branch to base worktrees on. When None, the gateway
            resolves the remote default branch per-repo (e.g., origin/main).
        assigned_branch: Remote branch that pushes from the worktree
            should target.  When set, the gateway configures
            ``branch.<local>.merge`` so a naive ``git push`` from the
            worktree resolves to a refspec targeting this branch
            instead of the per-worktree local branch name.  See #1809.
        timeout: Request timeout in seconds. Defaults to 120s because
            concurrent pipeline starts may queue behind per-repo locks
            in the gateway.

    Returns:
        WorktreeResult with host paths for each repo. ``worktrees`` is keyed
        by the full ``owner/repo`` slug (#3393 slice-3, operator ruling #6):
        the gateway's ``/api/v1/worktree/create`` handler keys the map by the
        slug it was handed, so two repos sharing a short name under different
        owners get distinct entries. This client is a passthrough — it does
        not re-key. The on-disk worktree directory (the map VALUE's leaf) is
        still the bare repo name, so consumers that reconstruct a path from
        the KEY must strip the owner prefix.

    Raises:
        GatewayError: On request failure
    """
    request_data: dict[str, Any] = {
        "container_id": container_id,
        "repos": repos,
    }
    if base_branch is not None:
        request_data["base_branch"] = base_branch
    if assigned_branch is not None:
        request_data["assigned_branch"] = assigned_branch
    if uid is not None:
        request_data["uid"] = uid
    if gid is not None:
        request_data["gid"] = gid

    try:
        result = self._make_request(
            "/api/v1/worktree/create",
            method="POST",
            data=request_data,
            use_launcher_auth=True,
            timeout=timeout,
        )

        data = result.get("data", {})
        return WorktreeResult(
            success=result.get("success", False),
            # Handle both missing key and explicit null from API
            worktrees=data.get("worktrees") or {},
            errors=data.get("errors") or [],
        )
    except GatewayError as e:
        # The gateway returns per-repo failure reasons in details["errors"]
        # when every worktree fails. Inline them into the message so
        # downstream callers that only see str(e) (e.g. kubernetes_spawner
        # wrapping in KubernetesSpawnError) still surface the specific
        # cause instead of the generic "Failed to create any worktrees".
        # See #1838.
        specific = e.details.get("errors") if e.details else None
        if specific:
            raise GatewayError(
                f"{e.message}: {'; '.join(str(x) for x in specific)}",
                status_code=e.status_code,
                details=e.details,
            ) from e
        raise
    except Exception as e:
        raise GatewayError(f"Failed to create worktrees: {e}") from e


def delete_worktrees(
    self,
    container_id: str,
    force: bool = True,
) -> WorktreeResult:
    """Delete worktrees for a container.

    Calls the gateway's /api/v1/worktree/delete endpoint to clean up
    worktrees when a container exits.

    Args:
        container_id: Container identifier
        force: Force removal even with uncommitted changes

    Returns:
        WorktreeResult with deletion status

    Raises:
        GatewayError: On request failure
    """
    try:
        result = self._make_request(
            "/api/v1/worktree/delete",
            method="POST",
            data={
                "container_id": container_id,
                "force": force,
            },
            use_launcher_auth=True,
        )

        data = result.get("data", {})
        return WorktreeResult(
            success=result.get("success", False),
            # Handle both missing key and explicit null from API
            worktrees=dict.fromkeys(data.get("deleted") or [], "deleted"),
            errors=data.get("errors") or [],
        )
    except GatewayError:
        raise
    except Exception as e:
        raise GatewayError(f"Failed to delete worktrees: {e}") from e
