"""Commit-authorship registry HTTP endpoints.

The gateway's commit observer posts to ``/api/v1/commit-authorship/register``
every time a commit is created through ``/api/v1/git/execute``; the gateway's
push handler later calls ``/api/v1/commit-authorship/lookup`` to partition
a push range by the role that authored each commit.

Both routes are protected by the same ``require_lifecycle_secret`` decorator
we use for other authorization-affecting endpoints.  The gateway pod carries
``EGG_LIFECYCLE_SECRET`` as a trusted infrastructure service; agent pods
must never see it (see ``kubernetes_spawner.py`` for the env-scrub list).

See issue #1882.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

# Path setup mirrors the sibling route modules.
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs: Any):  # type: ignore[misc]
        return logging.getLogger(name)


from commit_authorship_store import (  # type: ignore[import-not-found]
    AuthorshipCollisionError,
    CommitAuthorshipStoreError,
    get_store,
)
from lifecycle_auth import require_lifecycle_secret

logger = get_logger("orchestrator.commit_authorship")


def _publish_container_activity(pipeline_id: str | None, role: str, kind: str) -> None:
    """Best-effort publish of a CONTAINER_ACTIVITY event.

    The event lets HealthMonitor suppress heartbeat/progress stall alerts
    against agents that are demonstrably alive (mid-commit) but not
    emitting bus-level HEARTBEATs. See issue #2190.

    Failure to publish must not affect the registration response.
    """
    if not isinstance(pipeline_id, str) or not pipeline_id.strip():
        return
    try:
        try:
            from events import Event, EventType, get_event_bus  # type: ignore[import-not-found]
        except ImportError:
            from ..events import Event, EventType, get_event_bus  # type: ignore[no-redef]

        get_event_bus().publish(
            Event(
                event_type=EventType.CONTAINER_ACTIVITY,
                pipeline_id=pipeline_id,
                data={"agent_role": role, "kind": kind},
                source="commit_authorship",
            )
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("container_activity_publish_failed", error=str(exc))


commit_authorship_bp = Blueprint(
    "commit_authorship", __name__, url_prefix="/api/v1/commit-authorship"
)


# The lookup path accepts batches; this is defence-in-depth to prevent a
# misbehaving client from exploding memory with a single call.  A push's
# commit range is typically ≤ tens of commits in practice.
_MAX_LOOKUP_BATCH = 500
_MAX_REGISTER_BATCH = 100


def _json_error(message: str, status: int, **extra: Any) -> tuple[Response, int]:
    payload: dict[str, Any] = {"success": False, "message": message}
    if extra:
        payload.update(extra)
    return jsonify(payload), status


def _json_body() -> dict[str, Any] | None:
    """Return the parsed JSON body, or ``None`` when missing/malformed."""
    if not request.is_json:
        return None
    try:
        data = request.get_json(silent=True)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


@commit_authorship_bp.route("/register", methods=["POST"])
@require_lifecycle_secret
def register_commit() -> tuple[Response, int] | Response:
    """Register one commit's authorship.

    Request body::

        {
          "sha": "abc123...",
          "role": "coder",
          "pipeline_id": "issue-1882",   # optional -> orphan shard
          "repo": "owner/repo",           # optional; advisory
          "branch": "egg/issue-1882"      # optional; advisory
        }

    Response:

    - ``200`` on a fresh registration or on an idempotent re-register
      with the same role.
    - ``409`` when the SHA is already bound to a *different* role
      (first-wins — the original binding is preserved).
    - ``400`` on malformed input.
    - ``500`` when the store itself is unreachable.
    """
    data = _json_body()
    if data is None:
        return _json_error("Missing or malformed JSON body", 400)

    sha = data.get("sha")
    role = data.get("role")
    if not isinstance(sha, str) or not sha.strip():
        return _json_error("Missing or invalid 'sha'", 400)
    if not isinstance(role, str) or not role.strip():
        return _json_error("Missing or invalid 'role'", 400)

    pipeline_id = data.get("pipeline_id")
    if pipeline_id is not None and not isinstance(pipeline_id, str):
        return _json_error("'pipeline_id' must be a string", 400)

    repo = data.get("repo")
    branch = data.get("branch")
    if repo is not None and not isinstance(repo, str):
        return _json_error("'repo' must be a string", 400)
    if branch is not None and not isinstance(branch, str):
        return _json_error("'branch' must be a string", 400)

    try:
        store = get_store()
    except Exception as exc:
        logger.error("commit_authorship_store_unavailable", error=str(exc))
        return _json_error("Commit-authorship store unavailable", 500)

    try:
        normalized_sha, inserted, _existing = store.register(
            sha=sha,
            role=role,
            pipeline_id=pipeline_id,
            repo=repo,
            branch=branch,
        )
    except AuthorshipCollisionError as exc:
        logger.warning(
            "commit_authorship_register_collision",
            sha=exc.sha,
            existing_role=exc.existing_role,
            attempted_role=exc.attempted_role,
            source=getattr(request, "egg_source", "unknown"),
        )
        return _json_error(
            "Authorship collision: SHA already bound to a different role",
            409,
            sha=exc.sha,
            existing_role=exc.existing_role,
            attempted_role=exc.attempted_role,
        )
    except CommitAuthorshipStoreError as exc:
        return _json_error(f"Invalid registration: {exc}", 400)
    except Exception as exc:
        logger.error(
            "commit_authorship_register_failed",
            error=str(exc),
            exc_info=True,
        )
        return _json_error("Failed to register commit authorship", 500)

    _publish_container_activity(pipeline_id, role.strip().lower(), "git_commit")

    return (
        jsonify(
            {
                "success": True,
                "sha": normalized_sha,
                "role": role.strip().lower(),
                "inserted": inserted,
            }
        ),
        200,
    )


@commit_authorship_bp.route("/register-bulk", methods=["POST"])
@require_lifecycle_secret
def register_bulk() -> tuple[Response, int] | Response:
    """Register many commits in one call (best-effort per item).

    Returns a per-sha status map; individual failures do not abort the
    batch.  Used by the gateway's observer to register a cherry-picked
    range in one round-trip.
    """
    data = _json_body()
    if data is None:
        return _json_error("Missing or malformed JSON body", 400)

    items = data.get("items")
    if not isinstance(items, list):
        return _json_error("'items' must be a list", 400)
    if len(items) > _MAX_REGISTER_BATCH:
        return _json_error(
            f"'items' exceeds max batch size ({_MAX_REGISTER_BATCH})",
            400,
        )

    try:
        store = get_store()
    except Exception:
        return _json_error("Commit-authorship store unavailable", 500)

    results: list[dict[str, Any]] = []
    activity_seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            results.append({"success": False, "message": "Item must be an object"})
            continue
        try:
            normalized_sha, inserted, _existing = store.register(
                sha=item.get("sha", ""),
                role=item.get("role", ""),
                pipeline_id=item.get("pipeline_id"),
                repo=item.get("repo"),
                branch=item.get("branch"),
            )
        except AuthorshipCollisionError as exc:
            results.append(
                {
                    "success": False,
                    "status": 409,
                    "message": "Authorship collision",
                    "sha": exc.sha,
                    "existing_role": exc.existing_role,
                    "attempted_role": exc.attempted_role,
                }
            )
            continue
        except CommitAuthorshipStoreError as exc:
            results.append({"success": False, "status": 400, "message": str(exc)})
            continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "commit_authorship_bulk_register_failed",
                error=str(exc),
                exc_info=True,
            )
            results.append({"success": False, "status": 500, "message": "Internal error"})
            continue

        results.append(
            {
                "success": True,
                "status": 200,
                "sha": normalized_sha,
                "inserted": inserted,
            }
        )

        item_pipeline = item.get("pipeline_id")
        item_role = item.get("role", "")
        if (
            isinstance(item_pipeline, str)
            and item_pipeline
            and isinstance(item_role, str)
            and item_role
        ):
            key = (item_pipeline, item_role.strip().lower())
            if key not in activity_seen:
                activity_seen.add(key)
                _publish_container_activity(item_pipeline, key[1], "git_commit")

    return jsonify({"success": True, "results": results}), 200


@commit_authorship_bp.route("/lookup", methods=["POST"])
@require_lifecycle_secret
def lookup_commits() -> tuple[Response, int] | Response:
    """Bulk attribution lookup.

    Request body::

        { "shas": ["abc...", "def..."] }

    Response::

        {
          "success": true,
          "attribution": { "abc...": "coder", "def...": null }
        }
    """
    data = _json_body()
    if data is None:
        return _json_error("Missing or malformed JSON body", 400)

    shas = data.get("shas")
    if not isinstance(shas, list):
        return _json_error("'shas' must be a list", 400)
    if len(shas) > _MAX_LOOKUP_BATCH:
        return _json_error(
            f"'shas' exceeds max batch size ({_MAX_LOOKUP_BATCH})",
            400,
        )

    try:
        store = get_store()
    except Exception:
        return _json_error("Commit-authorship store unavailable", 500)

    try:
        attribution = store.lookup_bulk(shas)
    except Exception as exc:
        logger.error(
            "commit_authorship_lookup_failed",
            error=str(exc),
            exc_info=True,
        )
        return _json_error("Failed to look up commit authorship", 500)

    return jsonify({"success": True, "attribution": attribution}), 200
