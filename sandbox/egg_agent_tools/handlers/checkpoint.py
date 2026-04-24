"""Checkpoint-namespace handlers (list, show, search).

Thin MCP shims over the public helpers exported from
:mod:`egg_contracts.checkpoint_cli`
(``collect_checkpoints`` / ``load_checkpoint`` / ``search_checkpoints``).
Keeping the helpers in ``shared/`` and importing them here — not the
other way around — preserves the shared→sandbox-only dependency
direction (reviewer_code NACK #3 + decision-20).

Pagination:
- ``list`` and ``search`` accept an opaque ``cursor`` token plus a
  positive-integer ``limit`` (defaults tuned to stay well under the
  MCP 60 s timeout on worst-case live data).
- ``show`` is a single-item read — no pagination.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from egg_agent_tools.handlers.errors import HandlerError

# Defaults chosen to complete within the 60 s MCP timeout on the
# largest pipelines we see in production (~5k checkpoints). Bump via
# the request ``limit`` parameter when needed.
_DEFAULT_LIST_LIMIT = 100
_DEFAULT_SEARCH_LIMIT = 100
_MAX_LIMIT = 500


def _encode_cursor(offset: int) -> str:
    payload = json.dumps({"offset": int(offset)}).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: Any) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str):
        raise HandlerError("'cursor' must be a string if provided")
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        data = json.loads(raw.decode())
        offset = int(data.get("offset", 0))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HandlerError(f"Invalid cursor: {cursor!r}") from exc
    if offset < 0:
        raise HandlerError(f"Invalid cursor offset {offset}; must be >= 0")
    return offset


def _coerce_limit(raw: Any, *, default: int) -> int:
    if raw is None:
        return default
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise HandlerError("'limit' must be an integer") from exc
    if limit <= 0:
        raise HandlerError("'limit' must be > 0")
    if limit > _MAX_LIMIT:
        raise HandlerError(f"'limit' must be <= {_MAX_LIMIT}")
    return limit


def _resolve_repo_path(req: dict[str, Any]) -> str:
    """Resolve the repo path from env vars, with caller-override containment.

    Security: if the caller supplies ``repo_path``, validate it is
    under ``~/repos/`` or matches ``EGG_REPO_PATH`` exactly.  This
    prevents an agent from passing an arbitrary path (e.g., ``/etc``,
    ``../../``) that would be used for git operations.  Matches the
    containment approach used in ``brc.read_peer_artifact``.
    """
    env_path = os.environ.get("EGG_REPO_PATH")
    caller_path = req.get("repo_path")
    if caller_path:
        caller_resolved = os.path.realpath(caller_path)
        repos_root = os.path.realpath(os.path.expanduser("~/repos"))
        if env_path and caller_resolved == os.path.realpath(env_path):
            pass  # exact match with env — allowed
        elif caller_resolved.startswith(repos_root + os.sep) or caller_resolved == repos_root:
            pass  # under ~/repos/ — allowed
        else:
            raise HandlerError(
                f"repo_path must be under ~/repos/ or match EGG_REPO_PATH; got {caller_path!r}"
            )
        return str(caller_resolved)
    return str(env_path or os.getcwd())


def _build_filters(req: dict[str, Any]) -> dict[str, Any]:
    """Project the request dict to the subset of keys ``collect_checkpoints`` expects."""
    return {
        "repo_path": _resolve_repo_path(req),
        "checkpoint_repo": req.get("checkpoint_repo"),
        "branch": req.get("branch"),
        "issue": req.get("issue"),
        "pr": req.get("pr"),
        "session": req.get("session"),
        "trigger": req.get("trigger"),
        "status": req.get("status"),
        "agent_type": req.get("agent_type"),
        "phase": req.get("phase"),
        "pipeline": req.get("pipeline"),
        "repo": req.get("repo"),
        "limit": req.get("upstream_limit"),
    }


def checkpoint_list(req: dict[str, Any]) -> dict[str, Any]:
    """List checkpoints matching the filter set.

    CLI counterpart: ``egg-checkpoint list``.

    Request:
        issue (int), pr (int), branch (str), session (str),
        trigger (str), status (str), agent_type (str), phase (str),
        pipeline (str), repo (str): optional filters.
        limit (int): page size for MCP pagination (default 100,
            max 500). ``upstream_limit`` is passed to the index-level
            filter as the equivalent of ``egg-checkpoint list --limit N``
            and is only relevant when you want to cap the raw index
            scan (rare — default is unbounded for post-filter accuracy).
        cursor (str): opaque pagination token.
        repo_path, checkpoint_repo: optional overrides.

    Response:
        { ok: True, items: [...], next_cursor, total_available,
          ref: str|None }
    """
    from egg_contracts.checkpoint_cli import collect_checkpoints

    limit = _coerce_limit(req.get("limit"), default=_DEFAULT_LIST_LIMIT)
    offset = _decode_cursor(req.get("cursor"))
    filters = _build_filters(req)
    collected = collect_checkpoints(filters)
    all_items = collected["checkpoints"]
    total = len(all_items)
    page = all_items[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = _encode_cursor(next_offset) if next_offset < total else None

    return {
        "ok": True,
        "items": page,
        "next_cursor": next_cursor,
        "total_available": total,
        "ref": collected["ref"],
        "checkpoint_repo": collected["checkpoint_repo"],
    }


def checkpoint_show(req: dict[str, Any]) -> dict[str, Any]:
    """Load a single checkpoint by ID (ckpt-...) or commit SHA.

    CLI counterpart: ``egg-checkpoint show``.

    Request:
        identifier (str): required.
        repo_path, checkpoint_repo: optional overrides.

    Response:
        { ok: True, checkpoint: {...} } — the fully-expanded
        CheckpointV2.
    """
    from egg_contracts.checkpoint_cli import load_checkpoint

    identifier = req.get("identifier")
    if not identifier or not isinstance(identifier, str):
        raise HandlerError("'identifier' is required")
    repo_path = _resolve_repo_path(req)
    checkpoint_repo = req.get("checkpoint_repo")
    cp = load_checkpoint(identifier, repo_path, checkpoint_repo)
    if cp is None:
        raise HandlerError(f"No checkpoint found for {identifier!r}")
    return {"ok": True, "checkpoint": cp}


def checkpoint_search(req: dict[str, Any]) -> dict[str, Any]:
    """Search checkpoint transcripts for matching text.

    CLI counterpart: ``egg-checkpoint search``.

    Request:
        text (str): required search substring (case-insensitive).
        (same filter keys as ``checkpoint_list``)
        limit (int): page size (default 100, max 500).
        cursor (str): opaque pagination token.

    Response:
        { ok: True, items: [{"summary": {...}, "snippets": [...]}, ...],
          next_cursor, total_available, query }
    """
    from egg_contracts.checkpoint_cli import search_checkpoints

    text = req.get("text") or req.get("query")
    if not text or not isinstance(text, str):
        raise HandlerError("'text' is required")

    limit = _coerce_limit(req.get("limit"), default=_DEFAULT_SEARCH_LIMIT)
    offset = _decode_cursor(req.get("cursor"))
    filters = _build_filters(req)
    result = search_checkpoints(text, filters)
    all_matches = result["matches"]
    total = len(all_matches)
    page = all_matches[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = _encode_cursor(next_offset) if next_offset < total else None

    return {
        "ok": True,
        "items": page,
        "next_cursor": next_cursor,
        "total_available": total,
        "query": text,
        "ref": result["ref"],
        "checkpoint_repo": result["checkpoint_repo"],
    }
