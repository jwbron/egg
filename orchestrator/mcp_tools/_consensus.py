"""PipelineToolHandler consensus-status handler + message-inference helper (#3312 slice-13).

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

from mcp_tools import _SLICE_ID_PATTERN


def _structured_consensus(consensus: dict[str, Any]) -> dict[str, Any]:
    """Shape a structured (tracker-backed) consensus block for the tool result.

    Derives ``confirmed_agents`` from the per-role ``confirmed`` flags so
    the structured path and the message-inference fallback expose the
    same top-level fields (#3481).
    """
    agents = dict(consensus.get("agents", {}))
    return {
        "is_complete": consensus.get("is_complete", False),
        "confirmed_agents": [
            role
            for role, info in agents.items()
            if isinstance(info, dict) and info.get("confirmed")
        ],
        "blocking_agents": consensus.get("blocking_agents", []),
        "has_unresolved_nacks": consensus.get("has_unresolved_nacks", False),
        "unresolved_nacks": consensus.get("unresolved_nacks", []),
        "agents": agents,
    }


def _handle_get_consensus_status(self, args: dict[str, Any]) -> dict[str, Any]:
    """Get consensus status for a pipeline's current phase.

    ``slice_id`` scopes the result to one slice's BRC consensus in a
    slice-DAG implement phase — each slice runs its own consensus,
    keyed ``{pipeline_id}/{slice_id}``. Without it, pipeline-level
    consensus is reported; when only slice-scoped trackers are live
    (the slice-DAG case), the active slice trackers are resolved
    automatically from the status endpoint's ``slice_consensus`` map
    (#3481), rather than degrading to an empty message-inference view:
    a single active slice is served directly, multiple are returned
    per-slice. Slices are never merged into one block (#2761).
    """
    task_id = quote(args["task_id"], safe="")
    raw_slice_id = args.get("slice_id") or None

    # Validate ``slice_id`` client-side against the canonical
    # ``slice-<N>`` shape so a malformed value yields a clear error
    # instead of being swallowed by the bare ``except Exception``
    # around ``_make_request`` and silently degraded into a (now
    # slice-filtered) inference fallback. Matches the orchestrator's
    # ``extract_slice_id`` regex and ``resolve_slice_id`` on the
    # sandbox side; see review of #2764.
    if raw_slice_id is not None and not _SLICE_ID_PATTERN.fullmatch(raw_slice_id):
        raise ValueError(f"Invalid slice_id {raw_slice_id!r}: must match 'slice-<N>'")
    slice_id = raw_slice_id

    # Always include ``slice_id`` in the response (matches the
    # ``brc_get_state`` handler's shape — callers that read
    # ``resp["slice_id"]`` unconditionally see ``None`` rather than
    # a missing key on a pipeline-level query).
    result: dict[str, Any] = {"slice_id": slice_id}

    # Get pipeline base info
    pipeline_result = self._make_request(f"/api/v1/pipelines/{task_id}")
    pipeline_data = pipeline_result.get("data", {}).get("pipeline", {})
    result["pipeline_id"] = pipeline_data.get("id", "")
    result["current_phase"] = pipeline_data.get("current_phase", "")
    result["status"] = pipeline_data.get("status", "")

    # Try to get structured consensus from status endpoint
    status_endpoint = f"/api/v1/pipelines/{task_id}/status"
    if slice_id:
        status_endpoint += "?slice_id=" + quote(str(slice_id), safe="")
    try:
        status_result = self._make_request(status_endpoint)
        concurrent = status_result.get("data", {}).get("concurrent", {})
    except Exception:
        concurrent = {}

    consensus = concurrent.get("consensus", {})
    slice_consensus = concurrent.get("slice_consensus", {}) if slice_id is None else {}

    if consensus and consensus.get("agents"):
        result["consensus"] = _structured_consensus(consensus)
    elif slice_consensus:
        # Slice-scoped trackers are live but no explicit slice_id was
        # given (#3481). Serve the real tracker snapshots instead of
        # degrading to message inference: one active slice resolves
        # directly (with ``resolved_slice_id`` naming the scope), and
        # multiple stay keyed per-slice so they are never merged (#2761).
        if len(slice_consensus) == 1:
            resolved_slice_id, block = next(iter(slice_consensus.items()))
            result["resolved_slice_id"] = resolved_slice_id
            result["consensus"] = _structured_consensus(block)
            result["consensus"]["note"] = (
                f"Resolved the single active slice-scoped tracker "
                f"'{resolved_slice_id}'; pass slice_id to pin the scope"
            )
        else:
            result["active_slice_ids"] = sorted(slice_consensus)
            result["slice_consensus"] = {
                sid: _structured_consensus(block) for sid, block in slice_consensus.items()
            }
            result["note"] = (
                "Multiple slice-scoped consensus rounds are active; "
                "per-slice snapshots are under 'slice_consensus'. Pass "
                "slice_id to scope the result to one slice."
            )
    else:
        # Fall back to message-based inference. Filter messages by
        # the requested slice scope — symmetric with
        # ``reconstruct_tracker_from_messages``: a non-None
        # ``slice_id`` keeps only that slice's tagged messages, and
        # ``slice_id is None`` keeps only pipeline-level (untagged)
        # messages. Without the ``slice_id is None`` filter a
        # slice-DAG pipeline queried without a scope would still
        # mingle every slice's ``CONSENSUS_*`` into one inference —
        # exactly the cross-slice "soup" the orchestrator-side fix
        # is meant to eliminate (#2761).
        try:
            messages_result = self._make_request(f"/api/v1/pipelines/{task_id}/messages?limit=50")
            messages = messages_result.get("data", {}).get("messages", [])
            messages = [
                m for m in messages if (m.get("metadata") or {}).get("slice_id") == slice_id
            ]
            result["consensus"] = self._infer_consensus_from_messages(messages)
            result["consensus"]["note"] = (
                "Inferred from messages — structured consensus data not available"
            )
        except Exception:
            result["consensus"] = {"error": "Could not retrieve consensus data"}

    return result


def _infer_consensus_from_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Infer consensus state from message history.

    Note: uses last-write-wins semantics, so messages must be in
    chronological order (as returned by the orchestrator messages endpoint).
    """
    roles: dict[str, str] = {}  # role -> last consensus message type
    nacks: dict[str, dict[str, str]] = {}  # key -> {reviewer, producer, reason}

    for msg in messages:
        msg_type = msg.get("message_type", "")
        from_role = msg.get("from_role", "")

        if msg_type == "CONSENSUS_CONFIRMED":
            roles[from_role] = "confirmed"
        elif msg_type == "CONSENSUS_PROPOSE":
            roles[from_role] = "proposed"
            # Clear NACKs targeting this producer
            nacks = {k: v for k, v in nacks.items() if not k.endswith(f"->{from_role}")}
        elif msg_type == "CONSENSUS_ACK":
            if from_role not in roles or roles[from_role] != "confirmed":
                roles[from_role] = "acked"
        elif msg_type == "CONSENSUS_NACK":
            to_role = msg.get("to_role", "unknown")
            nacks[f"{from_role}->{to_role}"] = {
                "reviewer": from_role,
                "producer": to_role,
                "reason": msg.get("body", "") or msg.get("subject", ""),
            }

    confirmed = [r for r, s in roles.items() if s == "confirmed"]
    blocking = [r for r, s in roles.items() if s != "confirmed"]

    return {
        "is_complete": len(blocking) == 0 and len(confirmed) > 0,
        "confirmed_agents": confirmed,
        "blocking_agents": blocking,
        "has_unresolved_nacks": len(nacks) > 0,
        "unresolved_nacks": list(nacks.values()),
    }
