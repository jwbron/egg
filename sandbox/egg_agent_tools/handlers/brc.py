"""BRC consensus handlers (propose, ack, nack, confirm, state, blocking, peer read)."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from egg_agent_tools.handlers._gateway import (
    get_agent_role,
    get_pipeline_id,
    orchestrator_request,
)
from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")
_PIPELINE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_commit_sha(sha: str) -> str:
    if not _COMMIT_SHA_PATTERN.match(sha):
        raise HandlerError(f"Invalid commit SHA '{sha}': expected 7-40 hexadecimal characters")
    return sha


def _require_pipeline_id(req: dict[str, Any]) -> str:
    pid = req.get("pipeline_id") or get_pipeline_id()
    if not pid:
        raise HandlerError("pipeline_id required. Set EGG_PIPELINE_ID or pass 'pipeline_id'.")
    if not _PIPELINE_ID_PATTERN.match(pid):
        raise HandlerError(f"Invalid pipeline_id {pid!r}: must match [a-zA-Z0-9_-]+")
    return pid


def _require_role(req: dict[str, Any]) -> str:
    role = req.get("role") or get_agent_role()
    if not role:
        raise HandlerError("role required. Set EGG_AGENT_ROLE or pass 'role'.")
    return role


def _require_version_int(req: dict[str, Any], key: str) -> int:
    """Require an integer version field on the request.

    The producer's current proposal version must be plumbed through ACK / NACK
    so the orchestrator's version-match guard can detect stale verdicts and
    reject with a structured 409 (#2142).  Without this, the guard's fallback
    silently passes whenever the caller omits the field.
    """
    raw = req.get(key)
    if raw is None:
        raise HandlerError(
            f"'{key}' is required (the producer's current proposal version "
            "you reviewed; read it from the CONSENSUS_PROPOSE message)"
        )
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise HandlerError(f"'{key}' must be an integer; got {raw!r}") from exc
    if version < 0:
        raise HandlerError(f"'{key}' must be non-negative; got {version}")
    return version


def _resolve_head_sha() -> str:
    cwd = os.environ.get("EGG_REPO_PATH") or None
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            cwd=cwd,
            # stdin=DEVNULL defensively avoids the subprocess inheriting
            # a non-tty parent's stdio and blocking on an interactive
            # prompt.  Covers the edge case where the handler runs
            # inside a cron / systemd-style parent.
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise HandlerError("'commit_sha' not provided and could not resolve HEAD") from exc


def brc_propose(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_PROPOSE signal.

    Request (all optional unless noted):
        summary (str): proposal summary (required; ≥50 chars recommended)
            unless ``raw_payload`` already carries it.
        artifacts (list[str]): artifact references.
        risk_considered (str): risk summary.
        commit_sha (str): commit SHA; defaults to ``git rev-parse HEAD``.
        files_changed (list[str])
        tests_run (list[str])
        tasks (list[str]): tasks_satisfied
        attestation (dict): attestation payload.
        changed_artifacts (list[str]): optional re-proposal delta.
        raw_payload (dict): pre-built payload dict — every key is
            forwarded verbatim to the orchestrator.  Structured
            ``req`` keys take precedence when both are supplied.
        pipeline_id, role: contract/role overrides.

    Response:
        { ok: True, signal: <orchestrator response>, phase: "..." }
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)

    raw_payload = req.get("raw_payload")
    if raw_payload and not isinstance(raw_payload, dict):
        raise HandlerError("'raw_payload' must be a dict if provided")

    summary = req.get("summary") or (raw_payload.get("summary") if raw_payload else None)
    if not summary or not isinstance(summary, str):
        raise HandlerError("'summary' is required")

    user_sha = req.get("commit_sha") or (raw_payload.get("commit_sha") if raw_payload else None)
    if user_sha:
        _validate_commit_sha(user_sha)
    commit_sha = user_sha or _resolve_head_sha()

    # Start from raw_payload (if any) so unknown/custom schema fields
    # are preserved verbatim; structured kwargs layer on top.
    payload: dict[str, Any] = dict(raw_payload) if raw_payload else {}
    payload.update(
        {
            "summary": summary,
            "attestation": req.get("attestation") or payload.get("attestation") or {},
            "artifacts": list(req.get("artifacts") or payload.get("artifacts") or []),
            "risk_considered": (
                req.get("risk_considered")
                or req.get("risk")
                or payload.get("risk_considered")
                or payload.get("risk")
                or ""
            ),
            "commit_sha": commit_sha,
            "files_changed": list(req.get("files_changed") or payload.get("files_changed") or []),
            "tests_run": list(req.get("tests_run") or payload.get("tests_run") or []),
            "tasks_satisfied": list(
                req.get("tasks")
                or req.get("tasks_satisfied")
                or payload.get("tasks_satisfied")
                or payload.get("tasks")
                or []
            ),
        }
    )
    data: dict[str, Any] = {
        "signal_type": "consensus_propose",
        "agent_role": role,
        "payload": payload,
    }
    if req.get("changed_artifacts"):
        data["changed_artifacts"] = list(req["changed_artifacts"])

    try:
        result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    except GatewayError as exc:
        # Open-NACK barrier (#2142): the orchestrator returns 409 with a
        # structured envelope when re-propose is blocked because NACKs
        # against the current version haven't been delivered to the
        # producer yet.  Surface it as structured return data so the
        # agent can read the inlined NACK list and aggregate fixes
        # without parsing stderr.
        if (
            getattr(exc, "status_code", None) == 409
            and isinstance(exc.details, dict)
            and exc.details.get("status") == "open_nacks_blocked"
        ):
            return {
                "ok": False,
                "role": role,
                "status": "open_nacks_blocked",
                "message": exc.message,
                "rejection": exc.details,
            }
        raise
    if not result.get("success"):
        raise GatewayError(result.get("message", "propose failed"))

    consensus = result.get("data", {}).get("consensus", {})
    phase = consensus.get("agents", {}).get(role, {}).get("phase", "")
    return {"ok": True, "role": role, "phase": phase, "signal": result}


def brc_ack(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_ACK signal for a producer.

    Request:
        producer_role (str): required.
        reason (str): required.
        files_reviewed (list[str]): optional list of artifact references.
        pre_merge_condition (str): optional. Turns this into a **conditional
            ACK** — the work is approved but a human must perform the named
            action before merging (e.g. "git mv old/path new/path"). Surfaces
            as a dedicated "Pre-merge Obligations" section on the auto-created
            PR so the merger sees it instead of skimming past a prose note in
            the ACK reason (#1998).
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    producer_role = req.get("producer_role")
    if not producer_role:
        raise HandlerError("'producer_role' is required")
    reason = req.get("reason")
    if not reason:
        raise HandlerError("'reason' is required")
    ack_version = _require_version_int(req, "ack_version")

    payload: dict[str, Any] = {
        "artifact_references": list(req.get("files_reviewed") or []),
        "reason": reason,
        "ack_version": ack_version,
    }
    pre_merge_condition = req.get("pre_merge_condition") or ""
    if pre_merge_condition:
        payload["pre_merge_condition"] = pre_merge_condition

    data = {
        "signal_type": "consensus_ack",
        "agent_role": role,
        "producer_role": producer_role,
        "payload": payload,
    }
    try:
        result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    except GatewayError as exc:
        # Stale-version rejection (#2142): the orchestrator returns 409
        # with the producer's current proposal snapshot inlined when the
        # ACK targeted a superseded version.  Surface it as structured
        # data so the reviewer can re-fetch and re-review without
        # parsing stderr.
        if (
            getattr(exc, "status_code", None) == 409
            and isinstance(exc.details, dict)
            and exc.details.get("status") == "stale_version"
        ):
            return {
                "ok": False,
                "role": role,
                "producer_role": producer_role,
                "status": "stale_version",
                "message": exc.message,
                "rejection": exc.details,
            }
        raise
    if not result.get("success"):
        raise GatewayError(result.get("message", "ack failed"))
    return {"ok": True, "role": role, "producer_role": producer_role, "signal": result}


def brc_nack(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_NACK signal for a producer.

    Request:
        producer_role (str): required.
        reason (str): required (describes why the proposal is blocked).
        files_reviewed (list[str]): optional list of artifact references.
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    producer_role = req.get("producer_role")
    if not producer_role:
        raise HandlerError("'producer_role' is required")
    reason = req.get("reason")
    if not reason:
        raise HandlerError("'reason' is required")
    nack_version = _require_version_int(req, "nack_version")

    data = {
        "signal_type": "consensus_nack",
        "agent_role": role,
        "producer_role": producer_role,
        "payload": {
            "reason": reason,
            "artifact_references": list(req.get("files_reviewed") or []),
            "nack_version": nack_version,
        },
    }
    try:
        result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    except GatewayError as exc:
        # Stale-version rejection (#2142): same envelope as ACK.  See
        # ``brc_ack`` for full rationale.
        if (
            getattr(exc, "status_code", None) == 409
            and isinstance(exc.details, dict)
            and exc.details.get("status") == "stale_version"
        ):
            return {
                "ok": False,
                "role": role,
                "producer_role": producer_role,
                "status": "stale_version",
                "message": exc.message,
                "rejection": exc.details,
            }
        raise
    if not result.get("success"):
        raise GatewayError(result.get("message", "nack failed"))
    return {"ok": True, "role": role, "producer_role": producer_role, "signal": result}


def brc_confirm(req: dict[str, Any]) -> dict[str, Any]:
    """Send CONSENSUS_CONFIRMED after all reviewers have ACKed.

    Request:
        pipeline_id, role: overrides.

    Response carries:
        ok: True only when the producer transitioned to CONFIRMED.
            False for "pending_acks" — the orchestrator received the
            request but rejected the transition (e.g.
            ``producer_not_fully_acked``, ``global_zero_proposal``,
            ``stale_acks``). Inspect ``status`` and ``message`` to pick
            corrective action.
        status: "confirmed"|"pending_acks"
        consensus_reached: bool (only for status=="confirmed")
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)

    data = {
        "signal_type": "consensus_confirmed",
        "agent_role": role,
    }
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    if not result.get("success"):
        raise GatewayError(result.get("message", "confirm failed"))
    body = result.get("data", {})
    pending = body.get("status") == "pending_acks"
    return {
        "ok": not pending,
        "role": role,
        "status": "pending_acks" if pending else "confirmed",
        "consensus_reached": bool(body.get("consensus_reached", False)),
        "message": result.get("message"),
        "signal": result,
    }


def brc_get_state(req: dict[str, Any]) -> dict[str, Any]:
    """Fetch the current BRC consensus state for the pipeline.

    Request:
        pipeline_id: override.
        verbose (bool): include the full orchestrator status payload.

    Response:
        { ok: True, consensus: {...}, verbose: bool }

    No CLI counterpart — BRC state is a derived view of the pipeline
    status endpoint; the raw JSON is available via `egg-orch pipeline
    status --json` but the scraping rules are an agent-convenience
    shape unique to this tool (decision-13).
    """
    pid = _require_pipeline_id(req)
    verbose = bool(req.get("verbose", False))
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/status")
    data = result.get("data", {})
    consensus = data.get("concurrent", {}).get("consensus", {})

    response: dict[str, Any] = {
        "ok": True,
        "consensus": consensus,
        "is_complete": bool(consensus.get("is_complete", False)),
        "blocking_agents": list(consensus.get("blocking_agents", []) or []),
    }
    if verbose:
        response["raw"] = data
    return response


def brc_list_blocking(req: dict[str, Any]) -> dict[str, Any]:
    """Return the list of agent roles currently blocking consensus.

    Request:
        pipeline_id: override.

    No CLI counterpart — the same data is reachable via `egg-orch
    pipeline status --json` but the filtered blocking-agents shape is
    an agent-convenience view (decision-13).
    """
    pid = _require_pipeline_id(req)
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/status")
    consensus = result.get("data", {}).get("concurrent", {}).get("consensus", {})
    blocking = list(consensus.get("blocking_agents", []) or [])
    return {"ok": True, "blocking_agents": blocking}


_VALID_PHASES = ("refine", "plan", "implement", "pr")

# BRC message_type values that the orchestrator writes into the
# ``.egg-state/brc-history/<pipeline>-<phase>.json`` companion file.
# Mirrors orchestrator.routes.pipelines.BRC_HISTORY_TYPES; kept as a
# local tuple so the handler can validate `message_type` filters
# without importing the orchestrator package (which pulls fastapi).
_BRC_HISTORY_TYPES: frozenset[str] = frozenset(
    {
        "CONSENSUS_PROPOSE",
        "CONSENSUS_ACK",
        "CONSENSUS_NACK",
        "CONSENSUS_CONFIRMED",
        "CONSENSUS_RE_REVIEW",
        "CONSENSUS_WITHDRAWN",
    }
)

# peer_role / role slugs must be simple identifiers so the handler can
# never be tricked into path-traversal when future refactors embed the
# role into a filename (risk_analyst R2 hardening).
_ROLE_SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")


def _encode_cursor(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> dict[str, Any]:
    if cursor is None:
        return {"offset": 0, "skipped_malformed": 0}
    if not isinstance(cursor, str):
        raise HandlerError("'cursor' must be a string if provided")
    # Add back URL-safe base64 padding that was stripped on encode.
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        data = json.loads(raw.decode())
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HandlerError(f"Invalid cursor: {cursor!r}") from exc
    if not isinstance(data, dict):
        raise HandlerError(f"Invalid cursor: {cursor!r}")
    offset = int(data.get("offset", 0))
    if offset < 0:
        raise HandlerError(f"Invalid cursor offset {offset}; must be >= 0")
    skipped = int(data.get("skipped_malformed", 0))
    if skipped < 0:
        skipped = 0
    return {"offset": offset, "skipped_malformed": skipped}


def _resolve_env_identifier_for_brc_history() -> str:
    """Resolve the filename-identifier from the env; NEVER accept a caller override.

    The orchestrator always writes the file as
    ``{identifier}-{phase}.json`` where ``identifier`` is the bare
    issue number when one exists, else the pipeline-id string. We
    mirror that resolution so the handler finds the same file on
    disk — but we deliberately ignore any caller-supplied
    ``pipeline_id`` / ``issue`` so an agent cannot read another
    pipeline's brc-history (path-traversal / cross-pipeline-read
    hardening; risk_analyst R2; reviewer_code NACK #1a).
    """
    env_issue = os.environ.get("EGG_ISSUE_NUMBER")
    if env_issue:
        try:
            return str(int(env_issue))
        except ValueError:
            raise HandlerError(
                f"EGG_ISSUE_NUMBER is set but not an integer: {env_issue!r}"
            ) from None
    pid = get_pipeline_id()
    if pid:
        return str(pid)
    raise HandlerError(
        "pipeline identifier required. "
        "Set EGG_PIPELINE_ID or EGG_ISSUE_NUMBER; caller-supplied values "
        "are rejected for cross-pipeline-read hardening."
    )


def brc_read_peer_artifact(req: dict[str, Any]) -> dict[str, Any]:
    """Read consensus history for a peer from the local brc-history log.

    No CLI counterpart (decision-8): reads from the local
    ``.egg-state/brc-history/<identifier>-<phase>.json`` file
    written by ``orchestrator.routes.pipelines._write_brc_history``
    so reviewers never have to hand-grep JSON off disk.

    Security: caller-supplied ``pipeline_id``/``issue``/``repo_path``
    are ignored; the identifier and repo root are resolved server-side
    from ``EGG_PIPELINE_ID`` / ``EGG_ISSUE_NUMBER`` / ``EGG_REPO_PATH``
    (risk_analyst R2 + reviewer_code NACK #1). The resolved file path
    is canonicalised and asserted to sit under
    ``<repo_root>/.egg-state/brc-history/``; anything else raises
    ``HandlerError``. ``peer_role`` must match ``[a-z0-9_-]``.

    Request:
        phase (str): required — one of refine/plan/implement/pr.
        peer_role (str): optional — filter by ``from_role`` on each
            record. Must match ``[a-z0-9_-]``.
        producer_role (str): alias of ``peer_role`` (accepted for
            consistency with existing BRC verbs).
        message_type (str | list[str]): optional filter on
            ``message_type``; accepts a single value or a list.
        limit (int): optional page size (default 50, max 500).
        cursor (str): opaque pagination token.

    Response:
        { ok: True, phase: str, items: [...], next_cursor: str|None,
          total_available: int, skipped_malformed: int }

    ``skipped_malformed`` counts brc-history records that were
    silently skipped because they failed isinstance-dict parsing; the
    counter is also embedded in ``next_cursor`` so paginated reads
    remain deterministic.
    """
    phase = req.get("phase")
    if not phase or not isinstance(phase, str):
        raise HandlerError("'phase' is required")
    if phase not in _VALID_PHASES:
        raise HandlerError(f"'phase' must be one of {list(_VALID_PHASES)}; got {phase!r}")

    peer_role = req.get("peer_role") or req.get("producer_role")
    if peer_role is not None:
        if not isinstance(peer_role, str):
            raise HandlerError("'peer_role' must be a string if provided")
        if not _ROLE_SLUG_PATTERN.match(peer_role):
            raise HandlerError(f"'peer_role' must match [a-z0-9_-]; got {peer_role!r}")

    raw_mt = req.get("message_type")
    message_types: frozenset[str] | None
    if raw_mt is None:
        message_types = None
    elif isinstance(raw_mt, str):
        message_types = frozenset({raw_mt})
    elif isinstance(raw_mt, (list, tuple)):
        message_types = frozenset(str(v) for v in raw_mt)
    else:
        raise HandlerError("'message_type' must be a string or list of strings")
    if message_types is not None:
        unknown = message_types - _BRC_HISTORY_TYPES
        if unknown:
            raise HandlerError(
                f"Unknown message_type(s): {sorted(unknown)}; "
                f"expected one of {sorted(_BRC_HISTORY_TYPES)}"
            )

    raw_limit = req.get("limit")
    if raw_limit is None:
        limit = 50
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise HandlerError("'limit' must be an integer") from exc
        if limit <= 0:
            raise HandlerError("'limit' must be > 0")
        if limit > 500:
            raise HandlerError("'limit' must be <= 500")

    cursor_state = _decode_cursor(req.get("cursor"))
    offset = cursor_state["offset"]
    prior_skipped = cursor_state["skipped_malformed"]

    identifier = _resolve_env_identifier_for_brc_history()
    repo_root = Path(os.environ.get("EGG_REPO_PATH") or os.getcwd()).resolve()
    history_dir = (repo_root / ".egg-state" / "brc-history").resolve()
    history_file = (history_dir / f"{identifier}-{phase}.json").resolve()
    # Containment check: catches symlinks / .. in identifier/phase that
    # escape the allowed directory even after the env-only resolution.
    if not history_file.is_relative_to(history_dir):
        raise HandlerError("Resolved brc-history path escapes .egg-state/brc-history/")

    if not history_file.exists():
        return {
            "ok": True,
            "phase": phase,
            "items": [],
            "next_cursor": None,
            "total_available": 0,
            "skipped_malformed": prior_skipped,
        }

    try:
        records = json.loads(history_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HandlerError(f"Failed to read brc-history file for phase {phase!r}: {exc}") from exc
    if not isinstance(records, list):
        raise HandlerError(f"Malformed brc-history file for phase {phase!r}: expected a JSON array")

    filtered: list[dict[str, Any]] = []
    skipped_malformed = 0
    for rec in records:
        if not isinstance(rec, dict):
            skipped_malformed += 1
            continue
        if peer_role is not None and rec.get("from_role") != peer_role:
            continue
        if message_types is not None and rec.get("message_type") not in message_types:
            continue
        filtered.append(rec)

    total = len(filtered)
    total_skipped = prior_skipped + skipped_malformed
    if offset >= total:
        page: list[dict[str, Any]] = []
        next_cursor: str | None = None
    else:
        page = filtered[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = (
            _encode_cursor({"offset": next_offset, "skipped_malformed": total_skipped})
            if next_offset < total
            else None
        )

    return {
        "ok": True,
        "phase": phase,
        "items": page,
        "next_cursor": next_cursor,
        "total_available": total,
        "skipped_malformed": total_skipped,
    }
