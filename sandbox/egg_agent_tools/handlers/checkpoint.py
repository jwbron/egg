"""Checkpoint-namespace handlers (list, show, search).

All three verbs operate on local git-ref state (the ``egg/checkpoints/v2``
branch of the current repo or a configured external checkpoint repo) so
there is no gateway endpoint to forward to — handlers call the helpers
exported by :mod:`egg_contracts.checkpoint_cli`, which are the same
helpers the shell CLI uses. Keeping the two code paths on one helper
set is how the drift gate stays honest for the checkpoint namespace.

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
    path = req.get("repo_path") or os.environ.get("EGG_REPO_PATH") or os.getcwd()
    return str(path)


def collect_checkpoints(filters: dict[str, Any]) -> dict[str, Any]:
    """Return every checkpoint summary matching the filter set.

    Public (non-underscore) name so the CLI shim and the MCP handler
    can both import it; decision-18. The CLI shim keeps argparse +
    stdout shaping; this helper returns JSON-serialisable dicts.

    Args:
        filters: dict with any of ``repo_path``, ``checkpoint_repo``,
            ``branch``, ``issue``, ``pr``, ``session``, ``trigger``,
            ``status``, ``agent_type``, ``phase``, ``pipeline``,
            ``repo``, ``limit`` (upstream cap applied before the
            MCP-level page).

    Returns:
        ``{"checkpoints": [dict, ...], "composite_role": str|None,
           "ref": str|None, "checkpoint_repo": str|None}`` — ``ref``
        and ``checkpoints`` may be empty when no checkpoint branch
        exists. ``composite_role`` is non-None when the caller asked
        for a BRC composite reviewer role (``reviewer_code``,
        ``reviewer_contract``, etc.).
    """
    from egg_contracts.checkpoint_cli import (
        _decompose_composite_role,
        ensure_checkpoint_ref,
        load_checkpoint_from_ref,
        load_index_from_ref,
    )
    from egg_contracts.checkpoint_loader import filter_checkpoints_v2

    repo_path = filters.get("repo_path")
    if not repo_path:
        raise HandlerError("'repo_path' is required on collect_checkpoints")
    checkpoint_repo = filters.get("checkpoint_repo")

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    composite_role: str | None = None
    if not ref:
        return {
            "checkpoints": [],
            "composite_role": composite_role,
            "ref": None,
            "checkpoint_repo": checkpoint_repo,
        }

    index = load_index_from_ref(ref, repo_path)
    if not index:
        return {
            "checkpoints": [],
            "composite_role": composite_role,
            "ref": ref,
            "checkpoint_repo": checkpoint_repo,
        }

    agent_type_filter, composite_role = _decompose_composite_role(filters.get("agent_type"))

    summaries = filter_checkpoints_v2(
        index,
        issue_number=filters.get("issue"),
        pr_number=filters.get("pr"),
        branch=filters.get("branch"),
        session_id=filters.get("session"),
        trigger_type=filters.get("trigger"),
        session_status=filters.get("status"),
        agent_type=agent_type_filter,
        pipeline_phase=filters.get("phase"),
        pipeline_id=filters.get("pipeline"),
        repo=filters.get("repo"),
        limit=filters.get("limit"),
    )

    if composite_role and summaries:
        filtered = []
        for s in summaries:
            cp = load_checkpoint_from_ref(s.id, ref, repo_path)
            if cp and cp.session and cp.session.agent_role == composite_role:
                filtered.append(s)
        summaries = filtered

    return {
        "checkpoints": [s.model_dump(mode="json") for s in summaries],
        "composite_role": composite_role,
        "ref": ref,
        "checkpoint_repo": checkpoint_repo,
    }


def load_checkpoint(identifier: str, repo_path: str, checkpoint_repo: str | None) -> dict[str, Any] | None:
    """Load a single checkpoint by ID or commit SHA.

    Returns the ``model_dump``'d CheckpointV2 or ``None`` if not found.
    """
    from egg_contracts.checkpoint_cli import (
        ensure_checkpoint_ref,
        load_checkpoint_from_ref,
        load_index_from_ref,
    )

    ref = ensure_checkpoint_ref(repo_path, checkpoint_repo=checkpoint_repo)
    if not ref:
        return None

    cp = None
    if identifier.startswith("ckpt-"):
        cp = load_checkpoint_from_ref(identifier, ref, repo_path)
    else:
        index = load_index_from_ref(ref, repo_path)
        if index:
            checkpoint_id = index.get_by_commit(identifier)
            if checkpoint_id:
                cp = load_checkpoint_from_ref(checkpoint_id, ref, repo_path)

    if cp is None:
        return None
    return cp.model_dump(mode="json")


def search_checkpoints(query: str, filters: dict[str, Any]) -> dict[str, Any]:
    """Search checkpoint transcripts for *query* across summaries matching *filters*.

    Returns ``{"matches": [{"summary": {...}, "snippets": [...]}], ...}``.
    """
    from egg_contracts.checkpoint_cli import (
        _search_checkpoint_transcript,
        ensure_checkpoint_ref,
        load_checkpoint_from_ref,
    )

    if not isinstance(query, str) or not query:
        raise HandlerError("'query' is required")

    collected = collect_checkpoints(filters)
    summaries_dicts = collected["checkpoints"]
    ref = collected["ref"]
    checkpoint_repo = collected["checkpoint_repo"]
    composite_role = collected["composite_role"]

    if not ref or not summaries_dicts:
        return {
            "matches": [],
            "composite_role": composite_role,
            "ref": ref,
            "checkpoint_repo": checkpoint_repo,
            "query": query,
        }

    repo_path = filters.get("repo_path")
    matches: list[dict[str, Any]] = []
    for summary_dict in summaries_dicts:
        cp = load_checkpoint_from_ref(summary_dict["id"], ref, repo_path)
        if cp is None:
            continue
        if composite_role and not (
            cp.session and cp.session.agent_role == composite_role
        ):
            continue
        snippets = _search_checkpoint_transcript(cp, query)
        if snippets:
            matches.append({"summary": summary_dict, "snippets": snippets})

    return {
        "matches": matches,
        "composite_role": composite_role,
        "ref": ref,
        "checkpoint_repo": checkpoint_repo,
        "query": query,
    }


# --------------------------------------------------------------------
# Handler entry points (MCP verbs)
# --------------------------------------------------------------------


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
