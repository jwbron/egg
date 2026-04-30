"""BRC consensus @tool wrappers."""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers import brc as handlers
from egg_agent_tools.handlers.errors import HandlerError
from egg_agent_tools.tools._common import invoke_handler
from egg_agent_tools.tools._registry import ToolRegistration
from egg_agent_tools.tools._tool_compat import tool

NAMESPACE = "brc"

_PROPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Substantive proposal summary (>=50 chars recommended)",
        },
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Artifact references (paths, IDs, URLs)",
        },
        "risk_considered": {"type": "string", "description": "Summary of risks considered"},
        "commit_sha": {
            "type": "string",
            "description": "Commit SHA; defaults to HEAD",
        },
        "files_changed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files touched by the proposal",
        },
        "tests_run": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Test *identifiers* executed (e.g. pytest node IDs). "
                "Distinct from `attestation.tests_run`, which is an "
                "integer count of tests run for strict-mode "
                "validation."
            ),
        },
        "tasks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Contract task IDs the proposal satisfies",
        },
        "attestation": {
            "type": "object",
            "description": (
                "Role-specific attestation payload forwarded to the "
                "orchestrator. For the `tester` role under strict mode, "
                "must include either (a) `tests_run` > 0 (integer count) "
                "and a non-empty `checks_passed` list (e.g. "
                "['lint', 'test']), or (b) `tests_execution_blocked`=true "
                "with a non-empty `tests_execution_blocked_reason`. The "
                "handler validates these pre-flight (#2338) so a "
                "misconfigured payload fails locally with an actionable "
                "error rather than as a 400 from the orchestrator."
            ),
        },
        "changed_artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Re-proposal delta (changed artifact references)",
        },
        "push": {
            "type": "boolean",
            "default": True,
            "description": (
                "Push committed changes to origin via the gateway before "
                "sending the proposal (default true). Required in BRC "
                "mode — reviewers pull from origin, so an un-pushed "
                "artifact is invisible to them. Pass false only if you "
                "have already pushed through another route."
            ),
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["summary"],
}

_ACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "producer_role": {"type": "string", "description": "Producer being ACKed"},
        "reason": {"type": "string", "description": "Reason / review summary"},
        "ack_version": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Producer's proposal version you reviewed (#2142). The "
                "orchestrator rejects the ACK with status 'stale_version' if "
                "the producer has since re-proposed; read the version from "
                "the CONSENSUS_PROPOSE message you waited on. Must be >= 1: "
                "v0 means no proposal exists yet."
            ),
        },
        "files_reviewed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Artifacts actually reviewed",
        },
        "pre_merge_condition": {
            "type": "string",
            "description": (
                "Optional conditional-ACK obligation (issue #1998). The work "
                "is approved but the named action must be performed by a "
                "human before merging (e.g. 'git mv old/path new/path'). "
                "Surfaces as a Pre-merge Obligations section on the "
                "auto-created PR. Leave empty for an unconditional ACK."
            ),
        },
        "pre_merge_condition_resolved_in_diff": {
            "type": "string",
            "description": (
                "Optional commit SHA (issue #2336). Set this on a re-ACK "
                "when the obligation in `pre_merge_condition` has been "
                "satisfied within the same PR's diff since your initial "
                "conditional ACK — the PR-body renderer demotes resolved "
                "obligations from the merge-blocking section to a "
                "'Resolved within this PR' subsection. Only meaningful "
                "alongside a non-empty `pre_merge_condition`."
            ),
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["producer_role", "reason", "ack_version"],
}

_NACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "producer_role": {"type": "string", "description": "Producer being NACKed"},
        "reason": {
            "type": "string",
            "description": "Specific blocking reason; producer must address",
        },
        "nack_version": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Producer's proposal version you reviewed (#2142). The "
                "orchestrator rejects the NACK with status 'stale_version' "
                "if the producer has since re-proposed; read the version "
                "from the CONSENSUS_PROPOSE message you waited on. Must be "
                ">= 1: v0 means no proposal exists yet."
            ),
        },
        "files_reviewed": {
            "type": "array",
            "items": {"type": "string"},
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["producer_role", "reason", "nack_version"],
}

_CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
}

_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_id": {"type": "string"},
        "verbose": {
            "type": "boolean",
            "description": "Include the full orchestrator status payload",
            "default": False,
        },
    },
}

_LIST_BLOCKING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pipeline_id": {"type": "string"},
    },
}

_RESOLVE_OBLIGATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reviewer_role": {
            "type": "string",
            "description": (
                "Reviewer whose conditional-ACK obligation you are marking "
                "resolved (e.g. 'reviewer_contract')."
            ),
        },
        "producer_role": {
            "type": "string",
            "description": (
                "Producer the conditional-ACK was attached to (the role on "
                "the other side of the review edge — e.g. 'coder')."
            ),
        },
        "commit_sha": {
            "type": "string",
            "description": (
                "Optional commit SHA that satisfies the obligation. Recorded "
                "for audit; the orchestrator does not currently re-verify "
                "the commit's contents against the obligation text."
            ),
        },
        "note": {
            "type": "string",
            "description": (
                "Optional free-form note explaining how the obligation was "
                "satisfied. Surfaces in the audit log alongside the resolver "
                "role and commit SHA."
            ),
        },
        "pipeline_id": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["reviewer_role", "producer_role"],
    "additionalProperties": False,
}

_READ_PEER_ARTIFACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["refine", "plan", "implement", "pr"],
            "description": "Phase whose BRC history to read",
        },
        "peer_role": {
            "type": "string",
            "pattern": "^[a-z0-9_-]+$",
            "description": (
                "Optional filter: only records whose from_role matches. Must match [a-z0-9_-]."
            ),
        },
        "producer_role": {
            "type": "string",
            "pattern": "^[a-z0-9_-]+$",
            "description": "Alias of peer_role for consistency with other BRC verbs",
        },
        "message_type": {
            "description": (
                "Optional message_type filter; accepts a single type or a "
                "list (CONSENSUS_PROPOSE, CONSENSUS_ACK, CONSENSUS_NACK, "
                "CONSENSUS_CONFIRMED, CONSENSUS_RE_REVIEW, CONSENSUS_WITHDRAWN)"
            ),
        },
        "limit": {
            "type": "integer",
            "default": 50,
            "description": "Maximum items per page (default 50, max 500)",
        },
        "cursor": {
            "type": "string",
            "description": "Opaque pagination token returned by a prior call",
        },
    },
    "required": ["phase"],
    "additionalProperties": False,
}


@tool(
    "propose",
    "Send a CONSENSUS_PROPOSE signal starting or re-starting the BRC cycle "
    "for this producer. Pushes your committed changes to origin via the "
    "gateway first (disable with push=false). Prefer this over "
    "'egg-orch consensus propose --push'.",
    _PROPOSE_SCHEMA,
)
async def brc_propose(args: dict[str, Any]) -> dict[str, Any]:
    from egg_agent_tools.push import consensus_push

    # Strip the MCP-only ``push`` flag before handing the dict to the
    # handler so its schema stays clean.  Default: push (BRC requires
    # the artifact on origin for reviewers to pull).
    inbound = dict(args or {})
    should_push = bool(inbound.pop("push", True))

    def _push_then_propose(handler_args: dict[str, Any]) -> dict[str, Any]:
        if should_push:
            rc, err = consensus_push()
            if rc != 0:
                raise HandlerError(
                    f"Push to origin failed: {err or 'unknown error'}; "
                    "CONSENSUS_PROPOSE not sent. Fix the push error first, "
                    "then retry mcp__brc__propose. Pass push=false only if "
                    "you have already pushed through another route."
                )
        return handlers.brc_propose(handler_args)

    return await invoke_handler(_push_then_propose, inbound)


@tool(
    "ack",
    "ACK a producer's proposal (reviewer side). Prefer this over 'egg-orch consensus ack'.",
    _ACK_SCHEMA,
)
async def brc_ack(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_ack, args)


@tool(
    "nack",
    "NACK a producer's proposal with a specific blocking reason. Prefer this "
    "over 'egg-orch consensus nack'.",
    _NACK_SCHEMA,
)
async def brc_nack(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_nack, args)


@tool(
    "confirm",
    "Send CONSENSUS_CONFIRMED after all reviewers ACK. Returns status "
    "'pending_acks' if any reviewer has not yet re-ACKed. Prefer this over "
    "'egg-orch consensus confirmed'.",
    _CONFIRM_SCHEMA,
)
async def brc_confirm(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_confirm, args)


@tool(
    "get_state",
    "Fetch the current BRC consensus state (agent matrix, blocking roles, "
    "phase). Structured — no text scrape needed.",
    _STATE_SCHEMA,
)
async def brc_get_state(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_get_state, args)


@tool(
    "list_blocking",
    "Return the agent roles currently blocking consensus. Derived view of the BRC state.",
    _LIST_BLOCKING_SCHEMA,
)
async def brc_list_blocking(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_list_blocking, args)


@tool(
    "resolve_obligation",
    "Mark a reviewer's conditional-ACK obligation as satisfied in-cycle "
    "(#2338). Call this after committing the conditioning work referenced "
    "by a `pre_merge_condition` — typically the tester picking up a rename "
    "or test-path rewrite that the coder is gateway-blocked from. The "
    "matrix keeps the obligation text for audit, but the PR body and HITL "
    "gate stop surfacing it. Resolution is per-version: any later ACK / "
    "NACK / invalidate on the same edge resets the resolved flag.",
    _RESOLVE_OBLIGATION_SCHEMA,
)
async def brc_resolve_obligation(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_resolve_obligation, args)


@tool(
    "read_peer_artifact",
    "Read BRC consensus history for a peer from the local "
    "`.egg-state/brc-history/<identifier>-<phase>.json` log. Paginated via "
    "`limit` + opaque `cursor`. No CLI counterpart — this is a net-new "
    "capability so reviewers don't have to hand-grep brc-history files "
    "(decision-8).",
    _READ_PEER_ARTIFACT_SCHEMA,
)
async def brc_read_peer_artifact(args: dict[str, Any]) -> dict[str, Any]:
    return await invoke_handler(handlers.brc_read_peer_artifact, args)


REGISTRATIONS: list[ToolRegistration] = [
    ToolRegistration(
        name="mcp__brc__propose",
        namespace=NAMESPACE,
        handler=handlers.brc_propose,
        sdk_tool=brc_propose,
        cli_command=("egg-orch", "consensus", "propose"),
    ),
    ToolRegistration(
        name="mcp__brc__ack",
        namespace=NAMESPACE,
        handler=handlers.brc_ack,
        sdk_tool=brc_ack,
        cli_command=("egg-orch", "consensus", "ack"),
    ),
    ToolRegistration(
        name="mcp__brc__nack",
        namespace=NAMESPACE,
        handler=handlers.brc_nack,
        sdk_tool=brc_nack,
        cli_command=("egg-orch", "consensus", "nack"),
    ),
    ToolRegistration(
        name="mcp__brc__confirm",
        namespace=NAMESPACE,
        handler=handlers.brc_confirm,
        sdk_tool=brc_confirm,
        cli_command=("egg-orch", "consensus", "confirmed"),
    ),
    ToolRegistration(
        name="mcp__brc__get_state",
        namespace=NAMESPACE,
        handler=handlers.brc_get_state,
        sdk_tool=brc_get_state,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__brc__list_blocking",
        namespace=NAMESPACE,
        handler=handlers.brc_list_blocking,
        sdk_tool=brc_list_blocking,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__brc__resolve_obligation",
        namespace=NAMESPACE,
        handler=handlers.brc_resolve_obligation,
        sdk_tool=brc_resolve_obligation,
        cli_command=None,
    ),
    ToolRegistration(
        name="mcp__brc__read_peer_artifact",
        namespace=NAMESPACE,
        handler=handlers.brc_read_peer_artifact,
        sdk_tool=brc_read_peer_artifact,
        cli_command=None,
    ),
]
