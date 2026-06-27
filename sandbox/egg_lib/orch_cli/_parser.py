"""argparse wiring: builds the egg-orch parser tree. Dispatch targets resolve through the barrel (``_pkg.cmd_*``).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse

from egg_lib import orch_cli as _pkg

from ._brc import _VALID_BRC_HISTORY_PHASES
from ._http import _proposal_version_type
from ._overseer import _OVERSEER_VALID_LABEL_PRIORITIES


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """Add --json flag to a subparser."""
    parser.add_argument("--json", action="store_true", help="Output raw JSON")


def _non_negative_int(value: str) -> int:
    """argparse type validator: reject negative ints, mirror PipelineConfig ge=0."""
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from exc
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"{ivalue} must be >= 0 (use 0 to disable)")
    return ivalue


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="egg-orch",
        description="CLI for the egg orchestrator and gateway APIs",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command group")

    # -- health --
    health_parser = subparsers.add_parser("health", help="Health check and alerts")
    health_sub = health_parser.add_subparsers(dest="health_command")

    # health check (default when no subcommand given)
    health_check_parser = health_sub.add_parser("check", help="Check orchestrator + gateway health")
    _add_json_flag(health_check_parser)
    health_check_parser.set_defaults(func=_pkg.cmd_health)

    # health alerts
    health_alerts_parser = health_sub.add_parser("alerts", help="Get active health alerts")
    health_alerts_parser.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(health_alerts_parser)
    health_alerts_parser.set_defaults(func=_pkg.cmd_health_alerts)

    # health resolve
    health_resolve_parser = health_sub.add_parser("resolve", help="Resolve health alerts")
    health_resolve_parser.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    health_resolve_parser.add_argument(
        "--agent-id", required=True, dest="agent_id", help="Agent ID"
    )
    health_resolve_parser.add_argument(
        "--alert-type", required=True, dest="alert_type", help="Alert type"
    )
    _add_json_flag(health_resolve_parser)
    health_resolve_parser.set_defaults(func=_pkg.cmd_health_resolve)

    # Default: if no subcommand, run health check
    _add_json_flag(health_parser)
    health_parser.set_defaults(func=_pkg.cmd_health)

    # -- env --
    env_parser = subparsers.add_parser("env", help="Show orchestrator environment variables")
    _add_json_flag(env_parser)
    env_parser.set_defaults(func=_pkg.cmd_env)

    # -- pipeline --
    pipeline_parser = subparsers.add_parser("pipeline", help="Pipeline operations")
    pipeline_sub = pipeline_parser.add_subparsers(dest="pipeline_command")

    # pipeline list
    pl_list = pipeline_sub.add_parser("list", help="List pipelines")
    pl_list.add_argument("--status", help="Filter by status")
    pl_list.add_argument("--limit", type=int, help="Max results")
    _add_json_flag(pl_list)
    pl_list.set_defaults(func=_pkg.cmd_pipeline_list)

    # pipeline get
    pl_get = pipeline_sub.add_parser("get", help="Get pipeline details")
    pl_get.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(pl_get)
    pl_get.set_defaults(func=_pkg.cmd_pipeline_get)

    # pipeline create
    pl_create = pipeline_sub.add_parser("create", help="Create a pipeline")
    pl_create.add_argument("--repo", required=True, help="Repository (owner/name)")
    pl_create.add_argument("--issue", type=int, help="Issue number")
    pl_create.add_argument("--branch", help="Branch name")
    pl_create.add_argument("--prompt", help="Prompt (for prompt-driven pipelines)")
    pl_create.add_argument(
        "--network-mode",
        choices=["public", "private"],
        help="Network mode for spawned containers",
    )
    pl_create.add_argument(
        "--concurrent",
        action="store_true",
        default=False,
        help="Enable concurrent agent execution within phases",
    )
    _add_json_flag(pl_create)
    pl_create.set_defaults(func=_pkg.cmd_pipeline_create)

    # pipeline status
    pl_status = pipeline_sub.add_parser("status", help="Get pipeline status")
    pl_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(pl_status)
    pl_status.set_defaults(func=_pkg.cmd_pipeline_status)

    # pipeline delete
    pl_delete = pipeline_sub.add_parser("delete", help="Delete a pipeline")
    pl_delete.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(pl_delete)
    pl_delete.set_defaults(func=_pkg.cmd_pipeline_delete)

    # pipeline wait-status — host-side blocking-wait CLI (issue #2211).
    # Counterpart to `egg-orch message wait-loop`. Loops the orchestrator's
    # /status/wait route, threads the cursor, emits JSON-lines on each
    # Path-A event; silent on Path-B. Exit codes per
    # docs/reference/agent-wait-patterns.md §3.
    pl_wait_status = pipeline_sub.add_parser(
        "wait-status",
        help="Long-poll for pipeline events; JSON-lines on stdout",
        description=(
            "Loops the orchestrator's /status/wait route server-side, "
            "threading the response cursor between calls. Emits one JSON "
            "line per pipeline-relevant event (phase transition, terminal "
            "state, HITL DECISION_CREATED, OVERSEER_ALERT, consensus "
            "message). Silent on no_change. Exits 0 on terminal pipeline "
            "state, 1 on --max-iterations cap (test only), 2 on transient "
            "errors after backoff budget, 3 on permanent errors (4xx). "
            "Use --since <cursor> to resume after a Bash-cap timeout."
        ),
    )
    pl_wait_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    pl_wait_status.add_argument(
        "--since",
        default="",
        help=(
            "Opaque cursor from a prior wait-status JSON-line. Empty / "
            "absent snaps to the tip of both event sources."
        ),
    )
    pl_wait_status.add_argument(
        "--inner-timeout",
        type=int,
        default=25,
        help=(
            "Per-call server-side block timeout in seconds (default 25, "
            "clamped server-side by GET_STATUS_MAX_WAIT)."
        ),
    )
    pl_wait_status.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Safety cap on outer-loop iterations (test harnesses only). "
            "Loops until terminal pipeline state by default."
        ),
    )
    # --json is intentionally NOT supported on wait-status: the loop emits
    # one JSON object per event already (JSON-lines on stdout); a --json
    # toggle would just re-print the last envelope and confuse the
    # streaming contract.
    pl_wait_status.set_defaults(func=_pkg.cmd_pipeline_wait_status)

    # -- signal --
    signal_parser = subparsers.add_parser("signal", help="Send signals to orchestrator")
    signal_sub = signal_parser.add_subparsers(dest="signal_command")

    # Common signal args helper
    def add_signal_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
        p.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
        _add_json_flag(p)

    # signal complete
    sig_complete = signal_sub.add_parser("complete", help="Signal completion")
    add_signal_args(sig_complete)
    sig_complete.add_argument("--commit", help="Commit SHA")
    sig_complete.add_argument("--files", nargs="*", help="Changed files")
    sig_complete.set_defaults(func=_pkg.cmd_signal_complete)

    # signal progress
    sig_progress = signal_sub.add_parser("progress", help="Signal progress")
    add_signal_args(sig_progress)
    sig_progress.add_argument(
        "--percent", type=int, required=True, help="Progress percentage (0-100)"
    )
    sig_progress.add_argument("--task", help="Current task description")
    sig_progress.add_argument("--message", help="Status message")
    sig_progress.set_defaults(func=_pkg.cmd_signal_progress)

    # signal error
    sig_error = signal_sub.add_parser("error", help="Signal error")
    add_signal_args(sig_error)
    sig_error.add_argument("--error", required=True, help="Error message")
    sig_error.add_argument("--recoverable", action="store_true", help="Error is recoverable")
    sig_error.set_defaults(func=_pkg.cmd_signal_error)

    # signal heartbeat
    sig_hb = signal_sub.add_parser("heartbeat", help="Send heartbeat")
    add_signal_args(sig_hb)
    sig_hb.set_defaults(func=_pkg.cmd_signal_heartbeat)

    # signal readiness (concurrent mode)
    sig_ready = signal_sub.add_parser("readiness", help="Signal readiness state (concurrent mode)")
    add_signal_args(sig_ready)
    sig_ready.add_argument(
        "--state",
        required=True,
        choices=["WORKING", "READY", "BLOCKED", "OBJECTING"],
        help="Readiness state",
    )
    sig_ready.add_argument("--reason", help="Reason for state")
    sig_ready.set_defaults(func=_pkg.cmd_signal_readiness)

    # -- message (concurrent mode) --
    msg_parser = subparsers.add_parser("message", help="Inter-agent messaging (concurrent mode)")
    msg_sub = msg_parser.add_subparsers(dest="message_command")

    # message send
    msg_send = msg_sub.add_parser("send", help="Send a message")
    msg_send.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_send.add_argument("--role", help="Sender role (default: EGG_AGENT_ROLE)")
    msg_send.add_argument("--to", required=True, help="Target role or 'all'")
    msg_send.add_argument(
        "--type",
        required=True,
        choices=["PROGRESS", "STATUS", "HANDOFF", "HEARTBEAT"],
        help=(
            "Message type (PROGRESS, STATUS, HANDOFF, HEARTBEAT). "
            "QUESTION was removed in issue #1897 — put clarifying "
            "questions in a NACK --reason block marked "
            '"### Non-blocking" so the producer sees them with the '
            "verdict.  For HEARTBEAT prefer the dedicated "
            "`message heartbeat` subcommand (schema validation + "
            "rate limit + dedup)."
        ),
    )
    msg_send.add_argument("--subject", help="Message subject")
    msg_send.add_argument("--body", help="Message body")
    _add_json_flag(msg_send)
    msg_send.set_defaults(func=_pkg.cmd_message_send)

    # message poll
    msg_poll = msg_sub.add_parser("poll", help="Poll for messages")
    msg_poll.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_poll.add_argument("--role", help="Filter for role (default: EGG_AGENT_ROLE)")
    msg_poll.add_argument("--since", help="Return messages after this ID")
    msg_poll.add_argument("--limit", type=int, help="Max messages")
    msg_poll.add_argument(
        "--wait", type=int, help="Long-poll timeout in seconds (server holds connection)"
    )
    _add_json_flag(msg_poll)
    msg_poll.set_defaults(func=_pkg.cmd_message_poll)

    # message wait — typed event-driven blocking primitive (issue #1897)
    msg_wait = msg_sub.add_parser(
        "wait",
        help="Block until a message of one or more types arrives",
        description=(
            "Block on a typed BRC event.  Exit 0 = matched, "
            "1 = timeout, 2 = transient (retry ok), "
            "3 = permanent.  Prefer this over shell retry loops."
        ),
    )
    msg_wait.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_wait.add_argument(
        "--for",
        dest="for_",
        action="append",
        required=True,
        help="Message type to wait for (repeatable, required)",
    )
    msg_wait.add_argument("--role", help="Filter for role (default: EGG_AGENT_ROLE)")
    msg_wait.add_argument("--from", dest="from_", help="Filter by sender role")
    msg_wait.add_argument(
        "--from-producer",
        dest="from_producer",
        action="append",
        help=(
            "Sender allowlist (repeatable, #2725). Only messages whose "
            "from_role is in this set wake the wait. Defaults to the "
            "comma-separated list in $EGG_WAIT_PRODUCER_ALLOWLIST when set "
            "by the spawner. Explicit --from-producer args replace the "
            "env-derived list."
        ),
    )
    msg_wait.add_argument(
        "--slice",
        dest="slice_id",
        help=(
            "Slice scope (#2725). Only match messages whose "
            "metadata.slice_id equals this value OR is null "
            "(pipeline-level passthrough — OVERSEER_ALERT and global "
            "phase signals continue to wake every waiter). Defaults to "
            "$EGG_SLICE_ID when set."
        ),
    )
    msg_wait.add_argument("--since", help="Return messages after this ID")
    msg_wait.add_argument("--limit", type=int, help="Max messages")
    msg_wait.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Server-side block timeout in seconds (clamped by "
        "EGG_MESSAGE_POLL_MAX_WAIT, default 60)",
    )
    _add_json_flag(msg_wait)
    msg_wait.set_defaults(func=_pkg.cmd_message_wait)

    # message wait-loop — the consensus wrapper's blocking wait (#2908)
    msg_wait_loop = msg_sub.add_parser(
        "wait-loop",
        help="[wrapper-internal] Loop message wait until matched or max iterations reached",
        description=(
            "Convenience wrapper: call `message wait` in a loop until a "
            "match arrives (exit 0) or max-iterations / permanent error "
            "occurs (exit 1).  Wrapper-internal (#2908/#3157): the "
            "consensus wrapper issues this between BRC events. Agents "
            "never wait on the bus — handle the event in your prompt "
            "and exit; the wrapper re-invokes you on the next event."
        ),
    )
    msg_wait_loop.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_wait_loop.add_argument(
        "--for",
        dest="for_",
        action="append",
        required=True,
        help="Message type to wait for (repeatable, required)",
    )
    msg_wait_loop.add_argument("--role", help="Filter for role (default: EGG_AGENT_ROLE)")
    msg_wait_loop.add_argument("--from", dest="from_", help="Filter by sender role")
    msg_wait_loop.add_argument(
        "--from-producer",
        dest="from_producer",
        action="append",
        help=(
            "Sender allowlist (repeatable, #2725). Only messages whose "
            "from_role is in this set wake the wait. Defaults to the "
            "comma-separated list in $EGG_WAIT_PRODUCER_ALLOWLIST when set "
            "by the spawner. Explicit --from-producer args replace the "
            "env-derived list."
        ),
    )
    msg_wait_loop.add_argument(
        "--slice",
        dest="slice_id",
        help=(
            "Slice scope (#2725). Only match messages whose "
            "metadata.slice_id equals this value OR is null "
            "(pipeline-level passthrough — OVERSEER_ALERT and global "
            "phase signals continue to wake every waiter). Defaults to "
            "$EGG_SLICE_ID when set."
        ),
    )
    msg_wait_loop.add_argument("--since", help="Return messages after this ID")
    msg_wait_loop.add_argument("--limit", type=int, help="Max messages")
    msg_wait_loop.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Per-call block timeout in seconds",
    )
    msg_wait_loop.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Safety cap on outer-loop iterations.  **Loops forever by "
            "default** (value is effectively unbounded unless set) so "
            "normal BRC consensus never trips it.  Set to a positive "
            "integer only for test harnesses or deterministic "
            "reproductions."
        ),
    )
    # --json is intentionally NOT supported on wait-loop: the loop calls
    # cmd_message_wait repeatedly, and each timeout iteration would print
    # a JSON object to stdout, producing concatenated invalid JSON.
    # Use ``egg-orch message wait --json`` directly for single-shot JSON.
    msg_wait_loop.set_defaults(func=_pkg.cmd_message_wait_loop)

    # message heartbeat — emit a structured HEARTBEAT (issue #1897)
    msg_hb = msg_sub.add_parser(
        "heartbeat",
        help="Emit a structured HEARTBEAT state message",
        description=(
            "Emit a HEARTBEAT with a required --state "
            "(WORKING|WAITING_ON_ROLE|WAITING_FOR_EVENT|PROPOSED|IDLE). "
            "--state WAITING_ON_ROLE requires --waiting-on."
        ),
    )
    msg_hb.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    msg_hb.add_argument("--role", help="Sender role (default: EGG_AGENT_ROLE)")
    msg_hb.add_argument(
        "--state",
        required=True,
        choices=[
            "WORKING",
            "WAITING_ON_ROLE",
            "WAITING_FOR_EVENT",
            "PROPOSED",
            "IDLE",
        ],
        help="Agent state",
    )
    msg_hb.add_argument(
        "--waiting-on",
        dest="waiting_on",
        help="Peer role the agent is waiting on (required for WAITING_ON_ROLE)",
    )
    msg_hb.add_argument(
        "--since",
        help="Optional ISO-8601 / epoch timestamp naming when the current state began",
    )
    msg_hb.add_argument("--body", help="Free-form body text")
    _add_json_flag(msg_hb)
    msg_hb.set_defaults(func=_pkg.cmd_message_heartbeat)

    # message status
    msg_status = msg_sub.add_parser("status", help="Message bus status")
    msg_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(msg_status)
    msg_status.set_defaults(func=_pkg.cmd_message_status)

    # -- consensus (BRC protocol) --
    consensus_parser = subparsers.add_parser("consensus", help="BRC consensus protocol commands")
    consensus_sub = consensus_parser.add_subparsers(dest="consensus_command")

    # consensus propose
    cons_propose = consensus_sub.add_parser("propose", help="Send consensus proposal")
    cons_propose.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_propose.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
    cons_propose.add_argument("--file", help="JSON file with proposal payload")
    cons_propose.add_argument(
        "--summary",
        help=(
            "Proposal summary. Pass ``--summary -`` to read from stdin, or "
            "use ``--summary-file PATH`` for a file source (recommended when "
            "the prose contains shell metacharacters — argv prose flows "
            "through ``bash -c`` and is corrupted by ``$VAR`` / backticks / "
            "``;`` / ``&&`` / embedded newlines; #2741, #2908 slice-5)."
        ),
    )
    cons_propose.add_argument(
        "--summary-file",
        dest="summary_file",
        help=(
            "Path to a file containing the proposal summary (#2741 "
            "shell-metachar-safe alternative to ``--summary``). "
            "Mutually exclusive with non-sentinel ``--summary``."
        ),
    )
    cons_propose.add_argument("--artifacts", nargs="*", help="Artifact paths")
    cons_propose.add_argument(
        "--risk",
        help=(
            "Risk considerations. Pass ``--risk -`` to read from stdin, or "
            "use ``--risk-file PATH`` for a file source (recommended when the "
            "prose contains shell metacharacters — argv prose flows through "
            "``bash -c`` and is corrupted by ``$VAR`` / backticks / ``;`` / "
            "``&&`` / embedded newlines; #2741, #2908 slice-5)."
        ),
    )
    cons_propose.add_argument(
        "--risk-file",
        dest="risk_file",
        help=(
            "Path to a file containing risk considerations (#2741 "
            "shell-metachar-safe alternative to ``--risk``). Mutually "
            "exclusive with non-sentinel ``--risk``."
        ),
    )
    cons_propose.add_argument(
        "--commit-sha",
        default=None,
        help="Commit SHA pushed to the remote branch (defaults to HEAD)",
    )
    cons_propose.add_argument(
        "--changed-artifacts",
        nargs="*",
        help="Changed artifacts (for re-proposals after NACK)",
    )
    cons_propose.add_argument("--files-changed", nargs="*", help="Files changed in this proposal")
    cons_propose.add_argument("--tests-run", nargs="*", help="Tests executed for this proposal")
    cons_propose.add_argument(
        "--tasks", nargs="*", help="Contract tasks satisfied by this proposal"
    )
    cons_propose.add_argument(
        "--push",
        action="store_true",
        help="Run git push before sending the proposal (bundles push+propose "
        "so auto-repropose is suppressed)",
    )
    cons_propose.add_argument(
        "--no-changes-needed",
        dest="no_changes_needed",
        action="store_true",
        help=(
            "Generic no-op propose (#3027): this producer has no work in this "
            "slice (no assigned task / its domain is not impacted). Submittable "
            "without --artifacts or --commit-sha; counts as proposing so "
            "consensus is not blocked, and reviewers accept it as a non-blocking "
            "no-op. Requires --no-changes-reason."
        ),
    )
    cons_propose.add_argument(
        "--no-changes-reason",
        dest="no_changes_reason",
        help="Why this producer has no work in this slice (required with --no-changes-needed).",
    )
    _add_json_flag(cons_propose)
    cons_propose.set_defaults(func=_pkg.cmd_consensus_propose)

    # consensus ack
    cons_ack = consensus_sub.add_parser("ack", help="ACK a producer's proposal")
    cons_ack.add_argument("producer_role", help="Producer role to ACK")
    cons_ack.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_ack.add_argument("--role", help="Reviewer role (default: EGG_AGENT_ROLE)")
    cons_ack.add_argument(
        "--files-reviewed",
        nargs="+",
        help=(
            "Artifact references (files, commits) reviewed. Required unless "
            "``--files-reviewed-file PATH`` is given (one path per line)."
        ),
    )
    cons_ack.add_argument(
        "--files-reviewed-file",
        dest="files_reviewed_file",
        help=(
            "Path to a file listing artifact references reviewed (one path "
            "per line; blank lines and ``#`` comments stripped). Mutually "
            "exclusive with ``--files-reviewed`` (#2741, #2908 slice-5)."
        ),
    )
    cons_ack.add_argument(
        "--reason",
        help=(
            "Substantive rationale: what was read, what was checked, why "
            "the verdict follows. Pass ``--reason -`` to read from stdin, "
            "or use ``--reason-file PATH`` for a file source (recommended "
            "when the prose contains shell metacharacters — argv prose flows "
            "through ``bash -c`` and is corrupted by ``$VAR`` / backticks / "
            "``;`` / ``&&`` / embedded newlines; #2741, #2908 slice-5). "
            "Required unless ``--reason-file PATH`` is given."
        ),
    )
    cons_ack.add_argument(
        "--reason-file",
        dest="reason_file",
        help=(
            "Path to a file containing the ACK reason (#2741 shell-metachar-"
            "safe alternative to ``--reason``). Mutually exclusive with "
            "non-sentinel ``--reason``."
        ),
    )
    cons_ack.add_argument(
        "--ack-version",
        dest="ack_version",
        type=_proposal_version_type,
        required=True,
        help=(
            "The producer's proposal version you reviewed (must be >= 1). "
            "The orchestrator rejects the ACK with HTTP 409 (stale_version) "
            "if the producer has since re-proposed (#2142). Read it from the "
            "CONSENSUS_PROPOSE message you waited on, or from "
            "`egg-orch consensus status --json`."
        ),
    )
    cons_ack.add_argument(
        "--pre-merge-condition",
        dest="pre_merge_condition",
        default="",
        help=(
            "Optional: mark this as a conditional ACK (#1998). The work is "
            "approved but the named action must be performed by a human "
            "before merging (e.g. 'git mv old/path new/path'). Surfaces as "
            "a Pre-merge Obligations section on the auto-created PR. "
            "Pass ``--pre-merge-condition -`` to read from stdin, or use "
            "``--pre-merge-condition-file PATH`` for a file source "
            "(recommended when the prose contains shell metacharacters — "
            "obligation strings frequently quote shell commands; #2741, "
            "#2908 slice-5)."
        ),
    )
    cons_ack.add_argument(
        "--pre-merge-condition-file",
        dest="pre_merge_condition_file",
        help=(
            "Path to a file containing the pre-merge obligation prose "
            "(#2741 shell-metachar-safe alternative to "
            "``--pre-merge-condition``). Mutually exclusive with non-sentinel "
            "``--pre-merge-condition``."
        ),
    )
    cons_ack.add_argument(
        "--pre-merge-condition-resolved-in-diff",
        dest="pre_merge_condition_resolved_in_diff",
        default="",
        help=(
            "Optional: commit SHA that satisfied --pre-merge-condition within "
            "the same PR's diff (#2336). Use on a re-ACK when the obligation "
            "has been met in-pipeline since the initial conditional ACK — the "
            "PR-body renderer moves the obligation out of the merge-blocking "
            "section and into a 'Resolved within this PR' subsection. Requires "
            "--pre-merge-condition."
        ),
    )
    _add_json_flag(cons_ack)
    cons_ack.set_defaults(func=_pkg.cmd_consensus_ack)

    # consensus nack
    cons_nack = consensus_sub.add_parser("nack", help="NACK a producer's proposal")
    cons_nack.add_argument("producer_role", help="Producer role to NACK")
    cons_nack.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_nack.add_argument("--role", help="Reviewer role (default: EGG_AGENT_ROLE)")
    cons_nack.add_argument(
        "--reason",
        help=(
            "Reason for NACK. Pass ``--reason -`` to read from stdin, or "
            "use ``--reason-file PATH`` for a file source (recommended when "
            "the prose contains shell metacharacters — argv prose flows "
            "through ``bash -c`` and is corrupted by ``$VAR`` / backticks / "
            "``;`` / ``&&`` / embedded newlines; #2741, #2908 slice-5). "
            "Required unless ``--reason-file PATH`` is given."
        ),
    )
    cons_nack.add_argument(
        "--reason-file",
        dest="reason_file",
        help=(
            "Path to a file containing the NACK reason (#2741 shell-metachar-"
            "safe alternative to ``--reason``). Mutually exclusive with "
            "non-sentinel ``--reason``."
        ),
    )
    cons_nack.add_argument(
        "--files-reviewed",
        nargs="+",
        help=(
            "Artifact references (files, commits) reviewed. Required unless "
            "``--files-reviewed-file PATH`` is given (one path per line)."
        ),
    )
    cons_nack.add_argument(
        "--files-reviewed-file",
        dest="files_reviewed_file",
        help=(
            "Path to a file listing artifact references reviewed (one path "
            "per line; blank lines and ``#`` comments stripped). Mutually "
            "exclusive with ``--files-reviewed`` (#2741, #2908 slice-5)."
        ),
    )
    cons_nack.add_argument(
        "--nack-version",
        dest="nack_version",
        type=_proposal_version_type,
        required=True,
        help=(
            "The producer's proposal version you reviewed (must be >= 1). "
            "The orchestrator rejects the NACK with HTTP 409 (stale_version) "
            "if the producer has since re-proposed (#2142). Read it from the "
            "CONSENSUS_PROPOSE message you waited on, or from "
            "`egg-orch consensus status --json`."
        ),
    )
    _add_json_flag(cons_nack)
    cons_nack.set_defaults(func=_pkg.cmd_consensus_nack)

    # consensus withdraw
    cons_withdraw = consensus_sub.add_parser("withdraw", help="Withdraw proposal")
    cons_withdraw.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_withdraw.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
    cons_withdraw.add_argument(
        "--reason",
        help=(
            "Reason for withdrawal. Pass ``--reason -`` to read from stdin, "
            "or use ``--reason-file PATH`` for a file source (recommended "
            "when the prose contains shell metacharacters — argv prose flows "
            "through ``bash -c`` and is corrupted by ``$VAR`` / backticks / "
            "``;`` / ``&&`` / embedded newlines; #2741, #2908 slice-5). "
            "Required unless ``--reason-file PATH`` is given."
        ),
    )
    cons_withdraw.add_argument(
        "--reason-file",
        dest="reason_file",
        help=(
            "Path to a file containing the withdrawal reason (#2741 shell-"
            "metachar-safe alternative to ``--reason``). Mutually exclusive "
            "with non-sentinel ``--reason``."
        ),
    )
    _add_json_flag(cons_withdraw)
    cons_withdraw.set_defaults(func=_pkg.cmd_consensus_withdraw)

    # consensus confirmed
    cons_confirmed = consensus_sub.add_parser("confirmed", help="Confirm after all reviewers ACK")
    cons_confirmed.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_confirmed.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
    _add_json_flag(cons_confirmed)
    cons_confirmed.set_defaults(func=_pkg.cmd_consensus_confirmed)

    # consensus status
    cons_status = consensus_sub.add_parser("status", help="Show consensus status")
    cons_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    cons_status.add_argument(
        "--slice-id",
        dest="slice_id",
        default=None,
        help=(
            "Slice to scope the consensus status to (e.g. 'slice-7'). In a "
            "slice-DAG implement phase each slice runs its own BRC "
            "consensus; without a slice scope only pipeline-level "
            "consensus is shown. Defaults to $EGG_SLICE_ID."
        ),
    )
    _add_json_flag(cons_status)
    cons_status.set_defaults(func=_pkg.cmd_consensus_status)

    # -- brc (verb-level BRC operations) --
    #
    # ``brc`` is the verb-level surface used by the event-pump wrapper
    # (#2908 slice-2). It collects the read/derive verbs the wrapper
    # invokes from bash — ``next-action``, ``get-state``,
    # ``list-blocking`` (and in slice-5, ``resolve-obligation`` /
    # ``read-peer-artifact``). The write-side BRC verbs (propose, ack,
    # nack, withdraw, confirmed) remain under ``consensus`` to
    # preserve the existing CLI surface; documenters note the
    # cross-reference in ``docs/reference/agent-tools.md``.
    brc_parser = subparsers.add_parser("brc", help="BRC verb-level operations")
    brc_sub = brc_parser.add_subparsers(dest="brc_command")

    # brc next-action
    brc_next = brc_sub.add_parser(
        "next-action",
        help="Derive the next BRC action for a role from the orchestrator",
    )
    brc_next.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    brc_next.add_argument(
        "--role",
        default=None,
        help=(
            "Role to derive the next action for (defaults to "
            "$EGG_AGENT_ROLE). The wrapper passes the role of the "
            "agent it is about to invoke."
        ),
    )
    brc_next.add_argument(
        "--slice-id",
        dest="slice_id",
        default=None,
        help=("Slice to scope the derivation to (e.g. 'slice-7'). Defaults to $EGG_SLICE_ID."),
    )
    _add_json_flag(brc_next)
    brc_next.set_defaults(func=_pkg.cmd_brc_next_action)

    # brc get-state
    brc_state = brc_sub.add_parser(
        "get-state",
        help=("Return the BRC consensus state (verb-level alias for mcp__brc__get_state)"),
    )
    brc_state.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    brc_state.add_argument(
        "--slice-id",
        dest="slice_id",
        default=None,
        help="Slice to scope the state to. Defaults to $EGG_SLICE_ID.",
    )
    brc_state.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Include the full orchestrator status payload under "
            "the 'raw' key — matches the MCP-tool 'verbose' flag."
        ),
    )
    brc_state.set_defaults(func=_pkg.cmd_brc_get_state)

    # brc list-blocking
    brc_blocking = brc_sub.add_parser(
        "list-blocking",
        help=(
            "List roles currently blocking consensus (verb-level "
            "alias for mcp__brc__list_blocking). Newline-delimited "
            "by default; --json returns the array."
        ),
    )
    brc_blocking.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(brc_blocking)
    brc_blocking.set_defaults(func=_pkg.cmd_brc_list_blocking)

    # brc resolve-obligation (#2908 slice-5, TASK-5-2)
    brc_resolve = brc_sub.add_parser(
        "resolve-obligation",
        help=(
            "Mark a reviewer's conditional-ACK obligation satisfied in-cycle "
            "(verb-level alias for mcp__brc__resolve_obligation, #2338). "
            "Use after committing the conditioning work to drop the "
            "obligation from the PR body and HITL gate."
        ),
    )
    brc_resolve.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    brc_resolve.add_argument(
        "--role",
        help=(
            "Resolver role (defaults to $EGG_AGENT_ROLE). Producers cannot "
            "self-resolve their own obligations — the orchestrator rejects "
            "resolver_role == producer_role."
        ),
    )
    brc_resolve.add_argument(
        "--reviewer-role",
        dest="reviewer_role",
        required=True,
        help=(
            "Reviewer whose conditional-ACK obligation you are marking "
            "resolved (e.g. ``reviewer_contract``)."
        ),
    )
    brc_resolve.add_argument(
        "--producer-role",
        dest="producer_role",
        required=True,
        help=(
            "Producer the conditional-ACK was attached to (the role on the "
            "other side of the review edge — e.g. ``coder``)."
        ),
    )
    brc_resolve.add_argument(
        "--commit-sha",
        dest="commit_sha",
        default=None,
        help=(
            "Optional commit SHA that satisfies the obligation. Recorded "
            "for audit; the orchestrator does not re-verify the commit's "
            "contents against the obligation text."
        ),
    )
    brc_resolve.add_argument(
        "--note",
        help=(
            "Optional free-form note explaining how the obligation was "
            "satisfied. Surfaces in the audit log alongside the resolver "
            "role and commit SHA. Pass ``--note -`` to read from stdin, "
            "or use ``--note-file PATH`` for a file source (recommended "
            "when the prose contains shell metacharacters; #2741)."
        ),
    )
    brc_resolve.add_argument(
        "--note-file",
        dest="note_file",
        help=(
            "Path to a file containing the resolution note (#2741 shell-"
            "metachar-safe alternative to ``--note``). Mutually exclusive "
            "with non-sentinel ``--note``."
        ),
    )
    _add_json_flag(brc_resolve)
    brc_resolve.set_defaults(func=_pkg.cmd_brc_resolve_obligation)

    # brc read-peer-artifact (#2908 slice-5, TASK-5-3)
    brc_read = brc_sub.add_parser(
        "read-peer-artifact",
        help=(
            "Read BRC consensus history for a peer (verb-level alias for "
            "mcp__brc__read_peer_artifact). Stdout JSON; pagination via "
            "--limit + opaque --cursor token."
        ),
    )
    brc_read.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    brc_read.add_argument(
        "--phase",
        required=True,
        choices=list(_VALID_BRC_HISTORY_PHASES),
        help="Phase whose BRC history to read.",
    )
    brc_read.add_argument(
        "--peer-role",
        dest="peer_role",
        help=(
            "Optional: filter records to those whose ``from_role`` matches. "
            "Must match ``[a-z0-9_-]``."
        ),
    )
    brc_read.add_argument(
        "--message-type",
        dest="message_type",
        action="append",
        help=(
            "Optional message_type filter (repeatable). Accepts one of: "
            "CONSENSUS_PROPOSE, CONSENSUS_ACK, CONSENSUS_NACK, "
            "CONSENSUS_WITHDRAW, CONSENSUS_CONFIRMED, CONSENSUS_RE_REVIEW, "
            "CONSENSUS_OBLIGATION_RESOLVED, STATUS, HANDOFF, AGENT_FAILED, "
            "NUDGE, OVERSEER_ALERT, HEARTBEAT."
        ),
    )
    brc_read.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum items per page (default 50, max 500).",
    )
    brc_read.add_argument(
        "--cursor",
        help="Opaque pagination token returned by a prior call.",
    )
    brc_read.add_argument(
        "--no-include-unattributed",
        dest="include_unattributed",
        action="store_false",
        default=True,
        help=(
            "When reading a slice-scoped implement transcript "
            "(EGG_SLICE_ID set + phase=implement), do NOT merge records "
            "from the sibling ``<identifier>-implement-unattributed.json``. "
            "By default cross-cutting messages without slice scope are "
            "included."
        ),
    )
    _add_json_flag(brc_read)
    brc_read.set_defaults(func=_pkg.cmd_brc_read_peer_artifact)

    # -- phase --
    phase_parser = subparsers.add_parser("phase", help="Phase operations")
    phase_sub = phase_parser.add_subparsers(dest="phase_command")

    # phase get
    ph_get = phase_sub.add_parser("get", help="Get current phase")
    ph_get.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(ph_get)
    ph_get.set_defaults(func=_pkg.cmd_phase_get)

    # phase advance
    ph_advance = phase_sub.add_parser("advance", help="Advance to next phase")
    ph_advance.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ph_advance.add_argument(
        "--target-phase",
        required=True,
        choices=["refine", "plan", "implement", "pr"],
        help="Target phase to advance to",
    )
    ph_advance.add_argument("--reason", help="Reason for advancement")
    _add_json_flag(ph_advance)
    ph_advance.set_defaults(func=_pkg.cmd_phase_advance)

    # phase start
    ph_start = phase_sub.add_parser("start", help="Start current phase")
    ph_start.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(ph_start)
    ph_start.set_defaults(func=_pkg.cmd_phase_start)

    # phase complete
    ph_complete = phase_sub.add_parser("complete", help="Complete current phase")
    ph_complete.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ph_complete.add_argument("--reason", help="Completion reason")
    _add_json_flag(ph_complete)
    ph_complete.set_defaults(func=_pkg.cmd_phase_complete)

    # phase get-context (verb-level alias for mcp__phase__get_context)
    #
    # The event-pump wrapper (#2908 slice-2) calls this when it needs
    # the bundled phase context (pipeline_id, phase, role, assigned
    # tasks, prior-phase artifact paths) before invoking the agent.
    ph_ctx = phase_sub.add_parser(
        "get-context",
        help=("Bundle the caller's phase context (verb-level alias for mcp__phase__get_context)"),
    )
    ph_ctx.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ph_ctx.add_argument(
        "--phase",
        default=None,
        help="Phase override. Defaults to $EGG_PHASE.",
    )
    ph_ctx.add_argument(
        "--role",
        default=None,
        help="Role override. Defaults to $EGG_AGENT_ROLE.",
    )
    ph_ctx.add_argument(
        "--no-artifacts",
        dest="no_artifacts",
        action="store_true",
        help=(
            "Skip the best-effort prior-phase artifact scan "
            "(matches the MCP-tool ``include_artifacts=false`` flag)"
        ),
    )
    ph_ctx.set_defaults(func=_pkg.cmd_phase_get_context)

    # -- decision --
    decision_parser = subparsers.add_parser("decision", help="Decision queue operations")
    decision_sub = decision_parser.add_subparsers(dest="decision_command")

    # decision list
    dec_list = decision_sub.add_parser("list", help="List decisions")
    dec_list.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(dec_list)
    dec_list.set_defaults(func=_pkg.cmd_decision_list)

    # decision create
    dec_create = decision_sub.add_parser("create", help="Queue a decision")
    dec_create.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    dec_create.add_argument("--question", required=True, help="Decision question")
    dec_create.add_argument("--context", help="Additional context")
    dec_create.add_argument("--options", nargs="*", help="Decision options")
    dec_create.add_argument(
        "--phase",
        choices=["refine", "plan", "implement", "pr"],
        help="Pipeline phase (auto-inferred from pipeline state if omitted)",
    )
    dec_create.add_argument(
        "--decision-type",
        dest="decision_type",
        choices=["phase_gate", "choice", "feedback"],
        default=None,
        help="Decision type (default: choice). phase_gate is typically created by the orchestrator but can be used for manual debugging/recovery.",
    )
    _add_json_flag(dec_create)
    dec_create.set_defaults(func=_pkg.cmd_decision_create)

    # decision resolve
    dec_resolve = decision_sub.add_parser("resolve", help="Resolve a decision")
    dec_resolve.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    dec_resolve.add_argument("decision_id", help="Decision ID")
    dec_resolve.add_argument("--resolution", required=True, help="Resolution value")
    dec_resolve.add_argument("--resolved-by", help="Who resolved it")
    _add_json_flag(dec_resolve)
    dec_resolve.set_defaults(func=_pkg.cmd_decision_resolve)

    # decision status
    dec_status = decision_sub.add_parser("status", help="Decision queue status")
    dec_status.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(dec_status)
    dec_status.set_defaults(func=_pkg.cmd_decision_status)

    # -- container --
    container_parser = subparsers.add_parser("container", help="Container operations")
    container_sub = container_parser.add_subparsers(dest="container_command")

    # container list
    ct_list = container_sub.add_parser("list", help="List containers")
    ct_list.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    _add_json_flag(ct_list)
    ct_list.set_defaults(func=_pkg.cmd_container_list)

    # container spawn
    ct_spawn = container_sub.add_parser("spawn", help="Spawn a container")
    ct_spawn.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ct_spawn.add_argument("--role", required=True, help="Agent role")
    ct_spawn.add_argument("--issue", type=int, help="Issue number")
    ct_spawn.add_argument("--private", action="store_true", help="Private mode")
    _add_json_flag(ct_spawn)
    ct_spawn.set_defaults(func=_pkg.cmd_container_spawn)

    # container get
    ct_get = container_sub.add_parser("get", help="Get container info")
    ct_get.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ct_get.add_argument("container_id", help="Container ID")
    _add_json_flag(ct_get)
    ct_get.set_defaults(func=_pkg.cmd_container_get)

    # container stop
    ct_stop = container_sub.add_parser("stop", help="Stop a container")
    ct_stop.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ct_stop.add_argument("container_id", help="Container ID")
    _add_json_flag(ct_stop)
    ct_stop.set_defaults(func=_pkg.cmd_container_stop)

    # container logs
    ct_logs = container_sub.add_parser("logs", help="Get container logs")
    ct_logs.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ct_logs.add_argument("container_id", help="Container ID")
    ct_logs.add_argument("--lines", type=int, help="Number of log lines")
    _add_json_flag(ct_logs)
    ct_logs.set_defaults(func=_pkg.cmd_container_logs)

    # -- gateway --
    gw_parser = subparsers.add_parser("gateway", help="Gateway operations")
    gw_sub = gw_parser.add_subparsers(dest="gateway_command")

    # gateway health
    gw_health = gw_sub.add_parser("health", help="Check gateway health")
    _add_json_flag(gw_health)
    gw_health.set_defaults(func=_pkg.cmd_gateway_health)

    # gateway phase
    gw_phase = gw_sub.add_parser("phase", help="Get current phase from gateway")
    gw_phase.add_argument("--issue", type=int, help="Issue number")
    _add_json_flag(gw_phase)
    gw_phase.set_defaults(func=_pkg.cmd_gateway_phase)

    # gateway permissions
    gw_perms = gw_sub.add_parser("permissions", help="Get allowed operations for a phase")
    gw_perms.add_argument(
        "phase",
        choices=["refine", "plan", "implement", "pr"],
        help="SDLC phase",
    )
    _add_json_flag(gw_perms)
    gw_perms.set_defaults(func=_pkg.cmd_gateway_permissions)

    # -- progress (structured progress tracking) --
    progress_parser = subparsers.add_parser("progress", help="Structured progress event commands")
    progress_sub = progress_parser.add_subparsers(dest="progress_command")

    # progress emit
    prog_emit = progress_sub.add_parser("emit", help="Emit a structured progress event")
    prog_emit.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    prog_emit.add_argument("--role", help="Agent role (default: EGG_AGENT_ROLE)")
    prog_emit.add_argument("--step", required=True, help="Description of current step")
    prog_emit.add_argument(
        "--state",
        required=True,
        choices=["working", "blocked", "complete"],
        help="Progress state",
    )
    prog_emit.add_argument("--detail", help="Additional detail about the step")
    prog_emit.add_argument("--blocker", help="Description of blocker (when state=blocked)")
    _add_json_flag(prog_emit)
    prog_emit.set_defaults(func=_pkg.cmd_progress_emit)

    # progress query
    prog_query = progress_sub.add_parser("query", help="Query progress events")
    prog_query.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    prog_query.add_argument("--agent", help="Filter by agent role")
    prog_query.add_argument("--since", help="Filter events after this ISO timestamp")
    prog_query.add_argument("--limit", type=int, help="Max events to return")
    _add_json_flag(prog_query)
    prog_query.set_defaults(func=_pkg.cmd_progress_query)

    # -- overseer --
    overseer_parser = subparsers.add_parser(
        "overseer",
        help="Overseer-only operations (anomaly escalation)",
    )
    overseer_sub = overseer_parser.add_subparsers(dest="overseer_command")

    # overseer alert
    ov_alert = overseer_sub.add_parser(
        "alert",
        help="Broadcast an OVERSEER_ALERT to the human operator",
        description=(
            "Emit an OVERSEER_ALERT message that the human-facing alert "
            "surfaces watch for. Always sends with message_type=OVERSEER_ALERT "
            "and to_role=all. Use this whenever you observe an anomaly that "
            "requires human attention -- never use 'message send --type "
            "HANDOFF/STATUS' for anomaly escalation, those types blend into "
            "normal inter-agent traffic."
        ),
    )
    ov_alert.add_argument("pipeline_id", nargs="?", help="Pipeline ID")
    ov_alert.add_argument("--role", help="Sender role (default: EGG_AGENT_ROLE or 'overseer')")
    ov_alert.add_argument(
        "--anomaly",
        required=True,
        help=(
            "Anomaly type -- intentionally free-text so new types can emerge "
            "without CLI changes. Known types: stuck-phase-transition, "
            "agent-heartbeat-stall, agent-loop, orchestrator-consensus-silent, "
            "unauthorized-overseer-action, unmediated-disagreement. NOTE: "
            "'unmediated-disagreement' is for observers (overseer/mediator) "
            "flagging that no one is adjudicating; producers blocked by "
            "reviewer NACKs naming an operator-decidable scope question "
            "should use 'egg-contract add-decision' / "
            "'mcp__sdlc__register_open_question' instead -- alerts are "
            "informational, decisions are HITL gates."
        ),
    )
    ov_alert.add_argument(
        "--priority",
        required=True,
        choices=["low", "medium", "high"],
        help="Alert priority",
    )
    ov_alert.add_argument(
        "--summary",
        required=True,
        help="One-line summary of what was observed",
    )
    ov_alert.add_argument("--detail", help="Longer description / observed evidence")
    ov_alert.add_argument(
        "--recommend",
        help="What you'd recommend the human do (optional, for context)",
    )
    # Issue #1962: structured advisor recommendation. Surfaces in /sdlc
    # as a HITL decision; the human gates the actual action (file_issue).
    ov_alert.add_argument(
        "--recommendation",
        choices=["file_issue"],
        help=(
            "Structured advisor recommendation (issue #1962). Currently "
            "the only legal value is 'file_issue'. The human gates the "
            "actual filing via the existing pending_decisions HITL flow."
        ),
    )
    ov_alert.add_argument(
        "--recommendation-payload-file",
        help=(
            "Path to a JSON file containing the recommendation payload "
            "(e.g. composed issue_title + issue_body + priority + "
            "anomaly_signature). Required when --recommendation is set. "
            "Bounded at 50 KB."
        ),
    )
    _add_json_flag(ov_alert)
    ov_alert.set_defaults(func=_pkg.cmd_overseer_alert)

    # overseer file-issue (issue #1962, decision-9 opt-1)
    ov_file = overseer_sub.add_parser(
        "file-issue",
        help="File a GitHub issue from the overseer role (advisor-gated)",
        description=(
            "Run `gh issue create` itself, inside the sandbox, mediated "
            "by the gateway. Looks up an existing open issue with the "
            "same anomaly signature first; if found, prints "
            "{filed: false, dedup_match: <number>} and exits 0 without "
            "calling gh. On a fresh filing, appends a FiledIssueRecord "
            "to .egg-state/oversight/filed-issues.jsonl and prints "
            "{filed: true, issue_number: <number>}."
        ),
    )
    ov_file.add_argument(
        "--anomaly-type",
        required=True,
        help="Stable kebab-case anomaly identifier (e.g. agent-loop)",
    )
    ov_file.add_argument(
        "--priority",
        required=True,
        choices=list(_OVERSEER_VALID_LABEL_PRIORITIES),
        help="GitHub label priority (p0|p1|p2|p3)",
    )
    ov_file.add_argument(
        "--agent-role",
        required=True,
        help="Affected agent role (e.g. coder, refiner)",
    )
    ov_file.add_argument(
        "--anomaly-signature",
        required=True,
        help=(
            "16-hex anomaly signature (egg_overseer.state."
            "compute_anomaly_signature output). The first 8 chars "
            "embed in the issue title for cross-phase dedup."
        ),
    )
    ov_file.add_argument(
        "--issue-title-file",
        required=True,
        help="Path to a sandbox-local file containing the issue title",
    )
    ov_file.add_argument(
        "--issue-body-file",
        required=True,
        help="Path to a sandbox-local file containing the issue body",
    )
    ov_file.add_argument(
        "--parent-alert-message-id",
        help="ID of the parent OVERSEER_ALERT message",
    )
    ov_file.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the composed gh argv + JSON without calling gh",
    )
    _add_json_flag(ov_file)
    ov_file.set_defaults(func=_pkg.cmd_overseer_file_issue)

    # overseer consult-advisor (issue #1962, EGG200 boundary fix)
    ov_advisor = overseer_sub.add_parser(
        "consult-advisor",
        help="Consult the Opus advisor for a structured verdict (sandbox-side LLM call)",
        description=(
            "Run egg_overseer.advisor.consult_advisor inside the "
            "sandbox so the Opus run_agent_async invocation lives on "
            "the LLM-execution side of the EGG200 boundary. The "
            "orchestrator pod never touches Anthropic credentials. "
            "Reads the inputs (Haiku classification + Tier-1 health "
            "alerts + optional progress events / log lines) from a "
            "JSON file and writes the validated AdvisorVerdict JSON "
            "to --output-file (or stdout when omitted)."
        ),
    )
    ov_advisor.add_argument(
        "pipeline_id",
        nargs="?",
        help=(
            "Optional pipeline ID. When provided (or EGG_PIPELINE_ID is "
            "set), the verb reads PipelineConfig.overseer_advisor_model "
            "from the orchestrator status endpoint and passes the "
            "configured alias to consult_advisor. Omitted: falls back "
            "to the 'opus' default."
        ),
    )
    ov_advisor.add_argument(
        "--inputs-file",
        required=True,
        help=(
            "Path to a JSON file with keys: classification (object), "
            "health_alerts (array), progress_events (array, optional), "
            "recent_log_lines (array, optional)."
        ),
    )
    ov_advisor.add_argument(
        "--output-file",
        help=(
            "Path to write the AdvisorVerdict JSON. When omitted the "
            "verdict is written to stdout (pretty-printed). With "
            "--output-file, --json additionally tees the verdict JSON "
            "to stdout; without --output-file, --json is a no-op "
            "since stdout is already JSON."
        ),
    )
    ov_advisor.add_argument(
        "--recent-log-bytes-cap",
        type=_non_negative_int,
        default=None,
        help=(
            "Byte cap for the recent_log_lines block in the advisor "
            "prompt (issue #2120). When omitted, consult_advisor uses "
            "the PipelineConfig value or its 256 KiB default. 0 "
            "disables the cap (not recommended). Negative values are "
            "rejected (matches PipelineConfig ge=0)."
        ),
    )
    _add_json_flag(ov_advisor)
    ov_advisor.set_defaults(func=_pkg.cmd_overseer_consult_advisor)

    # --- push ---
    from egg_lib.cli_push import register_push_subcommand

    register_push_subcommand(subparsers)

    # --- session-state (cross-pod warm-resume sync, #3278) ---
    from egg_lib.cli_session_state import register_session_state_subcommand

    register_session_state_subcommand(subparsers)

    return parser
