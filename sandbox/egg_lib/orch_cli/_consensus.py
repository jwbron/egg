"""BRC consensus subcommands (propose/ack/nack/withdraw/confirmed/status).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import json
import sys
from typing import Any

from egg_lib import orch_cli as _pkg

from ._common import (
    _ProseArgError,
    _render_handler_error,
    _resolve_files_reviewed_arg,
    _resolve_prose_arg,
)
from ._http import (
    print_json,
    resolve_slice_id,
)


def _consensus_push() -> int:
    """Back-compat alias for :func:`egg_agent_tools.push.consensus_push`.

    The implementation moved to ``egg_agent_tools.push`` in #1994 so the
    ``mcp__brc__propose`` tool can share it.  Kept here as a thin alias
    so existing CLI callers and unit tests keep working.

    Returns only the exit code (discards error message) — the CLI
    surfaces errors via stderr prints inside ``consensus_push()``.
    """
    from egg_agent_tools.push import consensus_push as _impl

    rc, _err = _impl()
    return rc


def _render_stale_version_rejection(
    args: argparse.Namespace, resp: dict[str, Any], verdict: str
) -> int:
    """Render a stale-version ACK / NACK rejection (#2142).

    The orchestrator returns the producer's current proposal snapshot
    inline so the reviewer can re-fetch and re-review without a separate
    call.  Always exits 2 to signal "retry after re-review."
    """
    rejection = resp.get("rejection", {}) or {}
    if getattr(args, "json", False):
        print_json(rejection)
        return 2
    snap = rejection.get("current_proposal", {}) or {}
    producer = snap.get("producer") or resp.get("producer_role")
    print(
        f"{verdict} rejected: producer {producer} "
        f"is at v{snap.get('version')} (you reviewed an older version).",
        file=sys.stderr,
    )
    if snap.get("commit_sha"):
        print(f"  Current commit: {snap['commit_sha']}", file=sys.stderr)
    if snap.get("artifacts"):
        print(f"  Current artifacts: {', '.join(snap['artifacts'])}", file=sys.stderr)
    print(
        "Re-fetch the branch, re-review against the current version, and re-submit your verdict.",
        file=sys.stderr,
    )
    return 2


_CONSIDERED_DISPOSITIONS = ("not_operator_grade", "deferred_to_plan")


def _parse_considered_args(values: list[str]) -> list[dict[str, str]]:
    """Parse repeated ``--considered`` flags into candidate entries (#3526).

    Each value is ``"<disposition> :: <question> :: <why>"``. Raises
    ``ValueError`` with an actionable message on a malformed entry; the
    orchestrator re-validates the structured form on propose, so this is
    a fast local check, not the authority.
    """
    candidates: list[dict[str, str]] = []
    for raw in values:
        parts = [p.strip() for p in raw.split("::", 2)]
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                f"--considered entry {raw!r} is malformed; expected "
                '"<disposition> :: <question> :: <why>" with all three '
                "parts non-empty"
            )
        disposition, question, why = parts
        if disposition not in _CONSIDERED_DISPOSITIONS:
            raise ValueError(
                f"--considered disposition {disposition!r} is not one of "
                f"{list(_CONSIDERED_DISPOSITIONS)}"
            )
        candidates.append({"question": question, "disposition": disposition, "why": why})
    return candidates


def cmd_consensus_propose(args: argparse.Namespace) -> int:
    """Send CONSENSUS_PROPOSE signal, optionally pushing code first.

    Delegates to :func:`egg_agent_tools.handlers.brc.brc_propose` so the
    MCP ``mcp__brc__propose`` tool and the CLI share one handler.  The
    ``--push`` / ``--file`` / ``--json`` / ``--commit-sha`` surface is
    preserved.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    # Mutual-presence check for the no-op surface (#3027 review feedback):
    # ``--no-changes-reason`` without ``--no-changes-needed`` was silently
    # discarded, then the orchestrator bounced the propose with the generic
    # "requires at least one artifact" error — opaque to the user. Catch it
    # at the CLI with a clearer message before the handler runs.
    if getattr(args, "no_changes_reason", None) and not getattr(args, "no_changes_needed", False):
        print(
            "--no-changes-reason requires --no-changes-needed. "
            "If you have no work in this slice, pass both flags together; "
            "otherwise drop --no-changes-reason and propose normally.",
            file=sys.stderr,
        )
        return 2

    pid = _pkg.require_pipeline_id(args)
    role = _pkg._require_role(args)

    # If --push, run git push before proposing so the code is on the remote
    # before the proposal is sent.  Because the proposal and push happen
    # together, auto-repropose is suppressed (the explicit proposal covers
    # the push within the debounce window).
    if getattr(args, "push", False):
        push_result = _pkg._consensus_push()
        if push_result != 0:
            return push_result

    req: dict[str, Any]
    if getattr(args, "file", None):
        # File-based payload: forward the parsed dict VERBATIM to the
        # handler via the ``raw_payload`` key so unknown/custom schema
        # fields are not silently dropped.  The handler still layers
        # our structured request on top for required defaults
        # (pipeline_id / role / commit_sha fallback).
        with open(args.file) as f:
            file_payload: dict[str, Any] = json.load(f)
        req = {
            "pipeline_id": pid,
            "role": role,
            "raw_payload": file_payload,
        }
        # Resolve commit SHA fallback even for --file so the handler
        # can default to HEAD when the payload omits it.
        commit_sha = file_payload.get("commit_sha") or getattr(args, "commit_sha", None)
        if commit_sha:
            req["commit_sha"] = commit_sha
    else:
        # Resolve --summary from argv / stdin sentinel / --summary-file.
        # The prose payload is not required at the CLI surface here
        # because the handler ultimately validates it (an empty summary
        # is rejected downstream with a clearer error); leaving the
        # CLI permissive keeps the orchestrator the single source of
        # truth on summary-length policy. (#2741, #2908 slice-5.)
        try:
            summary_text = _resolve_prose_arg(
                argv_value=getattr(args, "summary", None),
                file_path=getattr(args, "summary_file", None),
                arg_name="--summary",
                file_flag="--summary-file",
                required=False,
            )
            risk_text = _resolve_prose_arg(
                argv_value=getattr(args, "risk", None),
                file_path=getattr(args, "risk_file", None),
                arg_name="--risk",
                file_flag="--risk-file",
                required=False,
            )
        except _ProseArgError:
            return 2
        req = {
            "pipeline_id": pid,
            "role": role,
            "summary": summary_text,
            "artifacts": list(getattr(args, "artifacts", []) or []),
            "risk_considered": risk_text,
            "files_changed": list(getattr(args, "files_changed", []) or []),
            "tests_run": list(getattr(args, "tests_run", []) or []),
            "tasks": list(getattr(args, "tasks", []) or []),
        }
        if getattr(args, "commit_sha", None):
            req["commit_sha"] = args.commit_sha

    # Decision-ledger attestation flags (#3390): refine/plan producers attest
    # the HITL decisions they registered (or an explicit empty ledger). Layered
    # into ``req["attestation"]`` so they win over a ``--file`` payload's
    # attestation (the handler prefers structured ``req`` keys).
    ledger_attestation: dict[str, Any] = {}
    if getattr(args, "decisions_registered", None):
        ledger_attestation["decisions_registered"] = list(args.decisions_registered)
    if getattr(args, "no_decisions_rationale", None):
        ledger_attestation["no_decisions_rationale"] = args.no_decisions_rationale
    if getattr(args, "considered", None):
        try:
            ledger_attestation["candidates_considered"] = _parse_considered_args(args.considered)
        except ValueError as err:
            print(f"Error: {err}", file=sys.stderr)
            return 2
    if ledger_attestation:
        req["attestation"] = ledger_attestation

    changed_artifacts = getattr(args, "changed_artifacts", None)
    if changed_artifacts:
        req["changed_artifacts"] = list(changed_artifacts)

    # Generic no-op propose (#3027): producer has no work in this slice.
    # Threaded into the shared handler payload for both the structured and
    # ``--file`` paths; the handler skips the HEAD commit-sha fallback for it.
    if getattr(args, "no_changes_needed", False):
        req["no_changes_needed"] = True
        req["no_changes_reason"] = getattr(args, "no_changes_reason", None) or ""

    try:
        resp = _handlers.brc_propose(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    # Open-NACK barrier rejection (#2142): brc_propose returns a
    # structured ``open_nacks_blocked`` payload instead of raising so
    # the agent can introspect the inline NACK list and aggregate
    # fixes.  Render the rejection cleanly and exit non-zero so shell
    # callers can branch on it.
    if resp.get("status") == "open_nacks_blocked":
        rejection = resp.get("rejection", {}) or {}
        if args.json:
            print_json(rejection)
            return 2
        nacks = rejection.get("nacks") or []
        print(
            f"Re-propose blocked: {len(nacks)} unresolved NACK(s) "
            f"on v{rejection.get('current_version')}",
            file=sys.stderr,
        )
        for nack in nacks:
            print(
                f"  [{nack.get('reviewer')}] (v{nack.get('version')}) {nack.get('reason', '')}",
                file=sys.stderr,
            )
        print(
            "Address every finding above and re-propose. "
            "The retry will succeed once you've been notified of the full set.",
            file=sys.stderr,
        )
        return 2

    signal = resp.get("signal", {})
    if args.json:
        print_json(signal)
        return 0

    print(f"Proposal sent by {role}")
    phase = resp.get("phase")
    if phase:
        print(f"  BRC phase: {phase}")
    return 0


def cmd_consensus_ack(args: argparse.Namespace) -> int:
    """Send CONSENSUS_ACK signal for a producer.

    Delegates to :func:`egg_agent_tools.handlers.brc.brc_ack`.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = _pkg.require_pipeline_id(args)
    role = _pkg._require_role(args)
    pre_merge_condition_resolved_in_diff = (
        getattr(args, "pre_merge_condition_resolved_in_diff", "") or ""
    )
    # Resolve prose --reason from argv / stdin sentinel / --reason-file,
    # --pre-merge-condition from argv / stdin sentinel / file, and
    # --files-reviewed from argv list / --files-reviewed-file (one path
    # per line). The handler-layer still enforces non-empty reason, so
    # leaving the CLI surface permissive here keeps a single source of
    # truth (#2741, #2908 slice-5).
    try:
        reason_text = _resolve_prose_arg(
            argv_value=getattr(args, "reason", None),
            file_path=getattr(args, "reason_file", None),
            arg_name="--reason",
            file_flag="--reason-file",
            required=True,
        )
        pre_merge_condition = _resolve_prose_arg(
            argv_value=getattr(args, "pre_merge_condition", None) or None,
            file_path=getattr(args, "pre_merge_condition_file", None),
            arg_name="--pre-merge-condition",
            file_flag="--pre-merge-condition-file",
            required=False,
        )
        files_reviewed = _resolve_files_reviewed_arg(
            argv_value=getattr(args, "files_reviewed", None),
            file_path=getattr(args, "files_reviewed_file", None),
        )
    except _ProseArgError:
        return 2
    if pre_merge_condition_resolved_in_diff and not pre_merge_condition:
        print(
            "error: --pre-merge-condition-resolved-in-diff requires "
            "--pre-merge-condition; a resolution SHA has nothing to resolve "
            "on a plain ACK",
            file=sys.stderr,
        )
        return 2
    if not files_reviewed:
        print(
            "Error: --files-reviewed (or --files-reviewed-file) is required.",
            file=sys.stderr,
        )
        return 2
    req = {
        "pipeline_id": pid,
        "role": role,
        "producer_role": args.producer_role,
        "reason": reason_text,
        "files_reviewed": files_reviewed,
        "pre_merge_condition": pre_merge_condition,
        "pre_merge_condition_resolved_in_diff": pre_merge_condition_resolved_in_diff,
        "ack_version": args.ack_version,
    }
    try:
        resp = _handlers.brc_ack(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    # Stale-version rejection (#2142): re-fetch and re-review.
    if resp.get("status") == "stale_version":
        return _render_stale_version_rejection(args, resp, "ACK")

    if args.json:
        print_json(resp.get("signal", {}))
        return 0
    if req["pre_merge_condition"]:
        suffix = (
            f"; resolved in {req['pre_merge_condition_resolved_in_diff']}"
            if req["pre_merge_condition_resolved_in_diff"]
            else ""
        )
        print(
            f"Conditional ACK sent by {role} for {args.producer_role} "
            f"(obligation: {req['pre_merge_condition']}{suffix})"
        )
    else:
        print(f"ACK sent by {role} for {args.producer_role}")
    return 0


def cmd_consensus_nack(args: argparse.Namespace) -> int:
    """Send CONSENSUS_NACK signal for a producer.

    Delegates to :func:`egg_agent_tools.handlers.brc.brc_nack`.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = _pkg.require_pipeline_id(args)
    role = _pkg._require_role(args)
    try:
        reason_text = _resolve_prose_arg(
            argv_value=getattr(args, "reason", None),
            file_path=getattr(args, "reason_file", None),
            arg_name="--reason",
            file_flag="--reason-file",
            required=True,
        )
        files_reviewed = _resolve_files_reviewed_arg(
            argv_value=getattr(args, "files_reviewed", None),
            file_path=getattr(args, "files_reviewed_file", None),
        )
    except _ProseArgError:
        return 2
    if not files_reviewed:
        print(
            "Error: --files-reviewed (or --files-reviewed-file) is required.",
            file=sys.stderr,
        )
        return 2
    req = {
        "pipeline_id": pid,
        "role": role,
        "producer_role": args.producer_role,
        "reason": reason_text,
        "files_reviewed": files_reviewed,
        "nack_version": args.nack_version,
    }
    try:
        resp = _handlers.brc_nack(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    # Stale-version rejection (#2142): re-fetch and re-review.
    if resp.get("status") == "stale_version":
        return _render_stale_version_rejection(args, resp, "NACK")

    if args.json:
        print_json(resp.get("signal", {}))
        return 0
    print(f"NACK sent by {role} for {args.producer_role}: {reason_text}")
    return 0


def cmd_consensus_withdraw(args: argparse.Namespace) -> int:
    """Send CONSENSUS_WITHDRAW signal."""
    pid = _pkg.require_pipeline_id(args)
    role = _pkg._require_role(args)

    try:
        reason_text = _resolve_prose_arg(
            argv_value=getattr(args, "reason", None),
            file_path=getattr(args, "reason_file", None),
            arg_name="--reason",
            file_flag="--reason-file",
            required=True,
        )
    except _ProseArgError:
        return 2

    data: dict[str, Any] = {
        "signal_type": "consensus_withdraw",
        "agent_role": role,
        "reason": reason_text,
    }
    slice_id = resolve_slice_id()
    if slice_id:
        data["slice_id"] = slice_id

    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        print(f"Proposal withdrawn by {role}: {reason_text}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_consensus_confirmed(args: argparse.Namespace) -> int:
    """Send CONSENSUS_CONFIRMED signal after all reviewers ACK.

    Delegates to :func:`egg_agent_tools.handlers.brc.brc_confirm`.
    Exit-code parity preserved: 2 for ``pending_acks``, 0 for
    confirmed, 1 for gateway error.
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = _pkg.require_pipeline_id(args)
    role = _pkg._require_role(args)
    try:
        resp = _handlers.brc_confirm({"pipeline_id": pid, "role": role})
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    if args.json:
        print_json(resp.get("signal", {}))
        return 0

    if resp.get("status") == "pending_acks":
        print(f"Waiting for reviewer re-ACKs: {resp.get('message')}")
        return 2
    print(f"Confirmation recorded for {role}")
    if resp.get("consensus_reached"):
        print("  Consensus reached!")
    return 0


def cmd_consensus_status(args: argparse.Namespace) -> int:
    """Show BRC consensus status (approval matrix and review graph).

    Delegates the structured data-build to
    :func:`egg_agent_tools.handlers.brc.brc_get_state` so the MCP
    ``mcp__brc__get_state`` tool and this CLI share one handler.  The
    human-readable rendering stays here in the shim.

    In a slice-DAG implement phase queried without a slice scope, the
    handler resolves the live slice-scoped trackers (#3487): a single
    active slice renders directly (named as auto-resolved), several
    render one section per slice under ``slice_consensus``; they are
    never merged into one block (#2761).
    """
    from egg_agent_tools.handlers import brc as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = _pkg.require_pipeline_id(args)

    # Scope to a slice when one is given (or inherited from $EGG_SLICE_ID
    # inside a per-slice agent sandbox). In a slice-DAG implement phase
    # each slice runs its own BRC consensus; querying without a slice
    # scope reports only pipeline-level consensus (#2761).
    req: dict[str, Any] = {"pipeline_id": pid}
    if getattr(args, "slice_id", None):
        req["slice_id"] = args.slice_id

    try:
        resp = _handlers.brc_get_state(req)
    except (GatewayError, HandlerError) as err:
        return _render_handler_error(err)

    consensus = resp.get("consensus", {}) or {}
    slice_consensus = resp.get("slice_consensus", {}) or {}
    if args.json:
        # Top-level shape stays the consensus block (legacy contract);
        # the slice-scope keys ride alongside when present.
        payload = dict(consensus)
        if resp.get("resolved_slice_id"):
            payload["resolved_slice_id"] = resp["resolved_slice_id"]
        if slice_consensus:
            payload["active_slice_ids"] = list(
                resp.get("active_slice_ids") or sorted(slice_consensus)
            )
            payload["slice_consensus"] = slice_consensus
        # Carry the handler's top-level multi-slice note so `--json` stays
        # symmetric with `egg-orch brc get-state`, which dumps the whole
        # response. (The single-slice note rides inside the consensus block.)
        if resp.get("note") and "note" not in payload:
            payload["note"] = resp["note"]
        print_json(payload)
        return 0

    if slice_consensus:
        print(
            f"No pipeline-level consensus; {len(slice_consensus)} active "
            f"slice consensus rounds (pass --slice-id to scope to one):"
        )
        for sid in sorted(slice_consensus):
            print(f"\nSlice: {sid}")
            _render_consensus_block(slice_consensus[sid] or {})
        return 0

    if not consensus:
        scope = resp.get("slice_id")
        if scope:
            print(f"No consensus data available for {scope}.")
        else:
            print("No consensus data available.")
        return 0

    resolved = resp.get("resolved_slice_id")
    scope = resp.get("slice_id")
    if resolved and not scope:
        print(f"Slice: {resolved} (single active slice, auto-resolved)")
    elif scope:
        print(f"Slice: {scope}")
    _render_consensus_block(consensus)
    return 0


def _render_consensus_block(consensus: dict[str, Any]) -> None:
    """Render one consensus block (agent matrix, blockers, obligations)."""
    is_complete = consensus.get("is_complete", False)
    print(f"Consensus complete: {is_complete}")

    agents = consensus.get("agents", {})
    if agents:
        print("\nAgent states:")
        for agent_name, agent_data in agents.items():
            producer_phase = agent_data.get("producer_phase")
            reviewer_phase = agent_data.get("reviewer_phase")
            confirmed = agent_data.get("confirmed", False)
            parts = [f"  {agent_name}:"]
            if producer_phase:
                parts.append(f"producer={producer_phase}")
            if reviewer_phase:
                parts.append(f"reviewer={reviewer_phase}")
            if not producer_phase and not reviewer_phase:
                parts.append("phase=unknown")
            state_str = " ".join(parts)
            if confirmed:
                state_str += " [CONFIRMED]"
            print(state_str)

    blocking = consensus.get("blocking_agents", [])
    if blocking:
        print(f"\nBlocking agents: {', '.join(blocking)}")

    conditions = consensus.get("pre_merge_conditions") or []
    if conditions:
        open_conditions = [c for c in conditions if not (c.get("resolved_in_diff") or "")]
        resolved_conditions = [c for c in conditions if (c.get("resolved_in_diff") or "")]
        if open_conditions:
            print("\nPending pre-merge obligations:")
            for cond in open_conditions:
                reviewer = cond.get("reviewer", "?")
                producer = cond.get("producer", "?")
                text = cond.get("condition", "")
                print(f"  {reviewer} → {producer}: {text}")
        if resolved_conditions:
            print("\nResolved within this PR:")
            for cond in resolved_conditions:
                reviewer = cond.get("reviewer", "?")
                producer = cond.get("producer", "?")
                text = cond.get("condition", "")
                sha = cond.get("resolved_in_diff", "")
                print(f"  {reviewer} → {producer}: {text} [resolved in {sha}]")


# ---------------------------------------------------------------------------
# BRC verb-level subcommands (#2908 slice-1)
#
# These wrap the existing handler-layer surface
# (``sandbox/egg_agent_tools/handlers/brc.py``) and the new orchestrator
# next-action route in shell-friendly CLI form. The event-pump wrapper
# (#2908 slice-2) drives ``egg-orch brc get-state`` /
# ``egg-orch brc next-action`` / ``egg-orch brc list-blocking`` from
# bash; surfacing them under their own ``brc`` parent (rather than
# under ``consensus``) matches the MCP-tool naming so the wrapper bash
# can call the CLI form directly without remembering a different verb
# mapping.
# ---------------------------------------------------------------------------
