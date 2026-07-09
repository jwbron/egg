"""Build consensus-wrapped commands for concurrent agent containers.

ONE-SHOT EVENT MODEL (#3164)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The orchestrator owns the BRC event loop (``orchestrator/event_loop.py``)
and spawns this wrapper ONCE per actionable BRC event, injecting the
event identity via env (``EGG_EVENT_ACTION`` ∈ ``propose|ack|nack``,
``EGG_EVENT_DEDUPE_KEY``, payload refs). The wrapper does no waiting of
its own — it re-derives ``egg-orch brc next-action`` once as a
stale-event backstop, invokes the agent exactly once via
``python3 -m egg_agent`` with the per-event prompt composed by
``orchestrator/routes/event_prompt.py:compose_event_prompt``, and exits
with the agent's rc. The orchestrator-side supervisor / convergence-stall
notifier (``event_loop.py``) owns respawn, backoff, idle-budget alerting,
and the failure-streak escalation that the retired in-pod loop carried.

History
~~~~~~~
* #2908 introduced the in-pod event-pump: a deterministic bash loop that
  blocked on ``egg-orch message wait-loop`` between events while a 30 s
  background heartbeat refreshed the slice-scoped gateway session.
* #3064/#3229 added the orchestrator-owned event loop behind an
  ``EGG_EVENT_LOOP_OWNER`` flag (dormant one-shot arm spliced into the
  wrapper) and proved it on a live BRC cycle.
* #3164 (this change) retired the in-pod wait arm, the 30 s background
  heartbeat, the wrapper-side idle-budget alert, the failure-streak
  escalation, and the ownership flag itself. The one-shot event handler
  is now the only path — there is no in-pod loop and no rollback flag.
"""

import os
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from egg_contracts.agent_roles import REVIEWER_CHECKOUT_ROLE_VALUES

# ``evidence_prefix_mode`` is the S7 staged-flag resolver
# (``EGG_REVIEW_EVIDENCE_PREFIX`` off/log/on, unknown => off) owned by the
# evidence-gatherer feature module. The wrapper reads it to decide whether the
# shared-evidence prefix is active and, in ``log`` mode, to record the measured
# per-wave cache-hit rate + cost into the BRC artifacts. Plain top-level import
# (no cycle: evidence_gatherer depends only on egg_logging + stdlib).
from evidence_gatherer import evidence_prefix_mode

# ``review_findings_mode`` is the S3 staged-flag resolver
# (``EGG_REVIEW_FINDINGS_MODE`` off/log/on, unknown => off). S4's
# per-finding tool-call cap RIDES that same flag rather than adding a
# second switch, so the whole #3523 item-2 machinery flips together. The
# import is a plain top-level module import (no cycle: review_findings_verdict
# depends only on egg_contracts, never on consensus_wrapper).
from review_findings_verdict import review_findings_mode

if TYPE_CHECKING:
    from egg_contracts.review_findings import FindingAnchor
    from review_findings_verdict import ComputedVerdict

# Space-separated role values for which ``sync_to_proposals`` performs a
# working-tree merge (#3216, WS1 of #3209). Rendered into the wrapper
# template as the ``checkout_roles`` field; the bash gate skips the merge
# for any role not in this list. Sorted for a deterministic, golden-stable
# render. The policy itself lives in ``egg_contracts.agent_roles`` so role
# semantics stay in one place.
EVENT_PUMP_CHECKOUT_ROLES = " ".join(sorted(str(r) for r in REVIEWER_CHECKOUT_ROLE_VALUES))


# Default idle budget for the BRC convergence-stall alert (#2908 od-4).
# The in-pod wrapper that originally owned this was retired in #3164; the
# orchestrator-side convergence-stall check (``event_loop.py``) now owns
# the alert, but the default lives here as the single source the health
# monitor's ``_IDLE_BUDGET_MIN_DEFAULT`` and the event loop both read.
# 30 min is well above the WS7-observed 10-13 min legitimate-idle ceiling.
EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT = 30

# OVERSEER_ALERT anomaly name raised when the idle budget is exceeded.
# Single-sourced here and imported by ``event_loop`` for its
# orchestrator-side convergence-stall OVERSEER_ALERT emission.
EVENT_PUMP_IDLE_BUDGET_ANOMALY = "stuck-phase-transition"


# ---------------------------------------------------------------------------
# Per-finding tool-call cap (#3523 S4 — item 2's non-prompt half)
# ---------------------------------------------------------------------------
#
# The #3523 verification ladder (S1) invites reviewers to run cheap,
# read-only *scratch-check* experiments in a scratch dir — actually run the
# disputed command, read a pinned dependency's real source instead of
# trusting memory — to CONFIRM or REFUTE a finding before it blocks. Those
# experiments sharpen findings, but left unbounded they multiply per-wave
# cost across 5+ reviewers x N findings. This module owns a configurable
# **per-finding tool-call cap** that bounds them, in the wrapper rather than
# in the prompt (issue item 2: "enforce in the wrapper, not the prompt,
# where feasible") — so the cap NUMBER and its enforce/record mode are owned
# by orchestrator code, not baked into prose a reviewer could reinterpret.
#
# The cap RIDES the S3 ``EGG_REVIEW_FINDINGS_MODE`` staged flag
# (``review_findings_mode()``): ``off`` => inert (spawn command byte-
# identical to the legacy path), ``log`` => record would-be cap hits without
# enforcing, ``on`` => enforce. A flag typo resolves to ``off`` via the
# shared resolver, so a misconfiguration can never silently strangle review.

# Env the operator sets (in the orchestrator process, where the spawn
# command is built — same place ``green_gate_mode()`` /
# ``review_findings_mode()`` read theirs) to tune the cap.
REVIEW_FINDING_TOOL_CALL_CAP_ENV_VAR = "EGG_REVIEW_FINDING_TOOL_CALL_CAP"

# Marker env the wrapper exports ALONGSIDE the cap so the reviewer's
# scratch-check runtime knows whether the cap is advisory (``log``) or
# enforced (``on``). Never exported in ``off`` mode — the export block is
# omitted wholesale, keeping the spawn command byte-identical to legacy.
REVIEW_FINDING_TOOL_CALL_CAP_MODE_ENV_VAR = "EGG_REVIEW_FINDING_TOOL_CALL_CAP_MODE"

# Safe default: 8 scratch-check tool calls per finding. Mirrors the /review
# skill's medium-effort finding cap (#3523 reference design) — enough to run
# a disputed command and read a source file or two, not enough to fund an
# open-ended investigation. An unset / non-integer / non-positive value
# resolves to this default: a typo must never degrade to "0 tool calls
# allowed" (which would forbid every scratch check) nor to a negative
# sentinel.
_DEFAULT_FINDING_TOOL_CALL_CAP = 8

# Reviewer roles the wrapper structurally CANNOT cap per-finding, with the
# reason each is exempt (task-4-1 requires documenting these in-code):
#   * ``tester`` — its verdict comes from EXECUTING the proposal end to end
#     (``make test`` / ``make lint`` against the merged worktree, #3216),
#     an unbounded, legitimate tool budget a per-finding scratch-check cap
#     would wrongly strangle mid-suite. It also deliberately stays
#     cold-start (#3523 S7 independence guardrail), so it never shares the
#     specialist lenses' scratch-check budget in the first place.
# Producers on the ``propose`` arm are outside the cap by construction: the
# cap governs the reviewer ack/nack arms only (a producer implements, it
# does not run per-finding scratch checks), so the exported bash block is
# gated on the reviewer arms rather than enumerating every producer role
# here.
_TOOL_CALL_CAP_EXEMPT_ROLES = frozenset({"tester"})


def review_finding_tool_call_cap() -> int:
    """Resolve the per-finding scratch-check tool-call cap from env.

    Fail-safe: an unset, non-integer, or non-positive value resolves to
    :data:`_DEFAULT_FINDING_TOOL_CALL_CAP`. A typo must never degrade to
    "0 tool calls allowed" (which would forbid every scratch check) nor to
    a negative sentinel — the cap only ever tightens a *positive* budget.
    """
    raw = os.environ.get(REVIEW_FINDING_TOOL_CALL_CAP_ENV_VAR, "")
    try:
        value = int(raw)
    except ValueError:
        # ``raw`` is always a ``str`` (``os.environ.get(..., "")``), so
        # ``int()`` can only raise ``ValueError`` here.
        return _DEFAULT_FINDING_TOOL_CALL_CAP
    return value if value > 0 else _DEFAULT_FINDING_TOOL_CALL_CAP


@dataclass(frozen=True)
class ToolCallCapDecision:
    """The per-finding tool-call cap outcome for one finding's scratch checks.

    Pure data computed by :func:`evaluate_finding_tool_call_cap`. The three
    booleans are mutually consistent with the staged flag and never all set:

    * ``off``  => ``cap_hit`` is always ``False`` (the cap is inert).
    * ``log``  => a hit sets ``recorded`` (record the would-be cap hit; do
      NOT enforce — the reviewer keeps going).
    * ``on``   => a hit sets ``enforced`` (the cap bites: no further
      scratch checks for this finding).

    ``exempt`` roles (see :data:`_TOOL_CALL_CAP_EXEMPT_ROLES`) never hit the
    cap regardless of mode.
    """

    cap: int
    tool_calls: int
    mode: str
    exempt: bool
    cap_hit: bool
    enforced: bool
    recorded: bool


def evaluate_finding_tool_call_cap(
    tool_calls: int,
    *,
    role: str | None = None,
    cap: int | None = None,
    mode: str | None = None,
) -> ToolCallCapDecision:
    """Decide whether a finding's scratch-check budget is spent (pure).

    ``tool_calls`` is the count of read-only scratch-check tool calls spent
    on ONE finding so far. The cap *triggers at the configured limit*: the
    finding may spend up to ``cap`` calls, and the decision reports
    ``cap_hit`` once ``tool_calls`` reaches ``cap`` (``>=``) — the budget is
    exhausted and the next scratch check is the one over the line.

    Everything derives from the staged flag (``mode``) and the resolved
    ``cap``, so this is the single source of truth for the cap semantics the
    wrapper exports and the tester pins at its boundary. ``off`` mode (and
    the exempt roles) make the cap inert; ``log`` records a would-be hit
    without enforcing; ``on`` enforces. ``role``/``cap``/``mode`` default to
    the live env resolution but are injectable for unit tests.
    """
    resolved_mode = mode if mode is not None else review_findings_mode()
    resolved_cap = cap if cap is not None else review_finding_tool_call_cap()
    exempt = role in _TOOL_CALL_CAP_EXEMPT_ROLES
    cap_hit = (not exempt) and resolved_mode != "off" and tool_calls >= resolved_cap
    return ToolCallCapDecision(
        cap=resolved_cap,
        tool_calls=tool_calls,
        mode=resolved_mode,
        exempt=exempt,
        cap_hit=cap_hit,
        enforced=cap_hit and resolved_mode == "on",
        recorded=cap_hit and resolved_mode == "log",
    )


def tool_call_cap_log_record(
    decision: ToolCallCapDecision,
    *,
    role: str | None = None,
    finding_id: str | None = None,
) -> dict[str, object]:
    """A JSON-serializable record of a cap outcome for ``log`` mode (pure).

    Mirrors ``review_findings_verdict.verdict_log_record``: in ``log`` mode
    the caller writes this into the BRC artifacts so an operator can see how
    often the cap WOULD have bitten (``cap_hit``/``recorded``) before
    flipping the flag to ``on``.
    """
    return {
        "kind": "tool_call_cap",
        "mode": decision.mode,
        "role": role,
        "finding_id": finding_id,
        "cap": decision.cap,
        "tool_calls": decision.tool_calls,
        "cap_hit": decision.cap_hit,
        "enforced": decision.enforced,
        "recorded": decision.recorded,
        "exempt": decision.exempt,
    }


# ---------------------------------------------------------------------------
# Shared-evidence prompt prefix — log-mode measurement (#3523 S7 / task-7-2)
# ---------------------------------------------------------------------------
#
# Item 5's whole bet is cost: a shared, byte-identical prefix across a wave of
# same-model reviewers turns most of each agent's ramp-up into cache reads
# (~1/10 the price). "Measure actual cache-hit rate and per-wave cost in log
# mode before enabling" is an EXPLICIT acceptance criterion, not an afterthought
# — so the wrapper (the shared serial spine that builds every reviewer's spawn
# command) owns a pure aggregator over the gateway/LiteLLM per-session cache
# stats and a JSON record the caller writes into the BRC artifacts. The prompt
# assembly itself is untouched in ``log`` mode (that is ``_criteria.py``'s
# contract); this half only measures.
#
# The per-session stat dicts are exactly what ``config/litellm/cost_callback.py``
# emits: each carries a ``session`` sub-dict (cumulative ``prompt_tokens`` /
# ``cached_tokens`` / ``cache_write_tokens`` / ``cost``) and a top-level
# ``cache_hit_rate_pct``. We accept either the whole line or a pre-reduced
# per-session summary and read defensively.


def _stat_get(record: dict[str, object], key: str) -> float:
    """Read a numeric field from a cost-callback record's ``session`` or top level."""
    session = record.get("session")
    if isinstance(session, dict) and key in session:
        raw = session.get(key)
    else:
        raw = record.get(key)
    try:
        return float(raw)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return 0.0


def aggregate_wave_cache_stats(session_records: list[dict[str, object]]) -> dict[str, object]:
    """Roll up per-session LiteLLM cache stats into wave-level totals (pure).

    ``session_records`` is one cost-callback summary per reviewer session in the
    wave (the final cumulative line for each session). Returns the wave's total
    prompt / cached / cache-write tokens, total cost, session count, and the
    wave cache-hit rate (cached / prompt * 100, clamped to [0, 100]). Cost is
    ``None`` when no session reported a known cost — never silently 0, so an
    operator can tell "no cache benefit" from "cost not captured".
    """
    total_prompt = 0.0
    total_cached = 0.0
    total_cache_write = 0.0
    total_cost = 0.0
    cost_known = 0
    for rec in session_records:
        total_prompt += _stat_get(rec, "prompt_tokens")
        total_cached += _stat_get(rec, "cached_tokens")
        total_cache_write += _stat_get(rec, "cache_write_tokens")
        cost = _stat_get(rec, "cost")
        # Distinguish "cost 0.0 reported" from "cost absent": only count it as
        # known when the record actually carried a cost field.
        session = rec.get("session")
        has_cost = (isinstance(session, dict) and session.get("cost") is not None) or (
            rec.get("cost") is not None
        )
        if has_cost:
            total_cost += cost
            cost_known += 1

    hit_rate: float | None = None
    if total_prompt > 0:
        hit_rate = round(min(total_cached * 100.0 / total_prompt, 100.0), 2)

    return {
        "sessions": len(session_records),
        "prompt_tokens": int(total_prompt),
        "cached_tokens": int(total_cached),
        "cache_write_tokens": int(total_cache_write),
        "cache_hit_rate_pct": hit_rate,
        "per_wave_cost": total_cost if cost_known else None,
        "cost_known_sessions": cost_known,
    }


def evidence_prefix_log_record(
    *,
    wave_roles: list[str] | None = None,
    shared_prefix_bytes: int | None = None,
    session_records: list[dict[str, object]] | None = None,
    mode: str | None = None,
) -> dict[str, object]:
    """A JSON-serializable shared-evidence-prefix record for ``log`` mode (pure).

    Mirrors ``tool_call_cap_log_record`` / ``verdict_log_record``: in ``log``
    mode the caller writes this into the BRC artifacts so an operator can read
    the measured wave cache-hit rate and per-wave cost — the go/no-go signal for
    flipping ``EGG_REVIEW_EVIDENCE_PREFIX`` to ``on`` — before any prompt
    assembly changes. ``mode`` defaults to the live flag resolution but is
    injectable for tests. ``shared_prefix_bytes`` is the byte length of the
    byte-identical prefix every sharing lens would carry (the cacheable span).
    """
    resolved_mode = mode if mode is not None else evidence_prefix_mode()
    stats = aggregate_wave_cache_stats(session_records or [])
    return {
        "kind": "evidence_prefix",
        "mode": resolved_mode,
        "wave_roles": sorted(wave_roles) if wave_roles else [],
        "shared_prefix_bytes": shared_prefix_bytes,
        "cache_stats": stats,
    }


def _render_tool_call_cap_env_block(mode: str, cap: int) -> str:
    """Render the wrapper bash that exports the per-finding cap for reviewers.

    Returns ``""`` in ``off`` mode so the spawn command is byte-identical to
    the legacy path (the staged-flag "off => no behavior change" contract).
    In ``log`` / ``on`` mode returns a bash block that — for the reviewer
    ``ack``/``nack`` arms only, skipping the exempt roles — exports the
    wrapper-owned cap value + mode into the agent environment so the cap
    NUMBER and its enforce/record mode are owned here, not in prompt prose.
    The producer ``propose`` arm is outside the cap (it does not run
    per-finding scratch checks), so the block is gated on ``ack``/``nack``.

    The block is inserted into ``invoke_agent_for_event`` (which has
    ``$action`` and ``$EGG_AGENT_ROLE`` in scope) immediately before the
    agent invocation, so the exports are live for the agent process.
    """
    if mode == "off":
        return ""
    exempt = " ".join(sorted(_TOOL_CALL_CAP_EXEMPT_ROLES))
    cap_var = REVIEW_FINDING_TOOL_CALL_CAP_ENV_VAR
    mode_var = REVIEW_FINDING_TOOL_CALL_CAP_MODE_ENV_VAR
    # NB: this is a build-time-generated bash snippet returned as a plain
    # string and substituted into the template as a ``.format()`` VALUE, so
    # its braces are LITERAL bash (str.format does not re-scan substituted
    # values). Doubled ``{{``/``}}`` below are the f-string escape for a
    # single literal brace in the emitted bash.
    return f"""    # Per-finding tool-call cap (#3523 S4, item 2 wrapper half): export the
    # wrapper-owned scratch-check budget for the reviewer ack/nack arms so the
    # cap NUMBER + enforce/record mode live here, not in prompt prose. Skipped
    # for the propose arm (producers do not scratch-check per finding) and for
    # the exempt roles (tester runs the whole suite; see consensus_wrapper.py).
    if [ "$action" = "ack" ] || [ "$action" = "nack" ]; then
        case " {exempt} " in
            *" ${{EGG_AGENT_ROLE:-}} "*)
                cw_log "tool-call cap: role=${{EGG_AGENT_ROLE:-?}} exempt; per-finding scratch cap N/A." ;;
            *)
                export {cap_var}="{cap}"
                export {mode_var}="{mode}"
                cw_log "tool-call cap: per-finding scratch-check cap={cap} mode={mode} role=${{EGG_AGENT_ROLE:-?}}." ;;
        esac
    fi
"""


# One-shot event wrapper bash template (#3164). Composed by
# ``build_consensus_wrapped_command``. The orchestrator owns the BRC
# event loop and spawns this wrapper once per actionable event with
# ``EGG_EVENT_ACTION`` injected; the wrapper re-derives next-action once
# as a stale-event backstop, invokes the agent one-shot, and exits with
# the agent's rc. There is no blocking wait-loop, no background
# heartbeat, and no idle budget — the orchestrator owns all waiting and
# the convergence-stall / failure-streak escalation (see
# ``event_loop.py``).
#
# Placeholders interpolated by ``str.format``:
#   {agent_command_prefix}   -- ``python3 -m egg_agent --model X --max-turns N``
#   {checkout_roles}         -- space-separated roles whose ack/nack arm
#                               performs a working-tree merge (#3216)
_EVENT_PUMP_WRAPPER_TEMPLATE = r"""#!/bin/bash
set -uo pipefail

# One-shot event wrapper (#3164). The orchestrator spawns this wrapper
# once per actionable BRC event (``EGG_EVENT_ACTION`` injected); the
# helper functions below are shared by the one-shot event handler
# appended at the end. No blocking wait-loop and no background heartbeat:
# the orchestrator owns all waiting.

cw_log() {{
    echo "[event-pump] $*" >&2
}}

# Emit one heartbeat. The CLI's ``message heartbeat`` handler auto-
# attaches ``slice_id`` from ``$EGG_SLICE_ID`` via
# ``_maybe_attach_slice_id`` in
# ``sandbox/egg_agent_tools/handlers/_gateway.py``. The one-shot event
# handler emits a single foreground ping (no background emitter) so the
# slice-5 silent-mid-event tripwire keys on heartbeats from an active
# one-shot pod. We also echo the slice tag in the ``--body`` so a
# snapshot test can grep for the slice_id propagation without
# intercepting the HTTP POST.
emit_heartbeat() {{
    local state="$1"
    local body_text="$2"
    local slice_tag="${{EGG_SLICE_ID:-none}}"
    timeout 5 egg-orch message heartbeat \
        --state "$state" \
        --body "$body_text (slice=$slice_tag)" \
        >/dev/null 2>&1 || true
}}

# Ask the orchestrator route what to do next. Returns a JSON document
# with ``action`` ('wait'|'propose'|'ack'|'nack'|'confirm'|'complete')
# and an optional ``event_payload``. The one-shot handler uses this once
# as a stale-event freshness re-check before invoking the agent.
#
# HTTP 409 from the route (409 stale_version, 409 aggregated-NACK
# barrier) is a signal, not a crash: the CLI surfaces it as a non-zero
# exit code with an empty/invalid JSON. We fall back to
# ``{{"action":"wait"}}`` and propagate the original ``rc`` so the
# one-shot handler can tell an inconclusive re-check (exit 75) from a
# positively-derived action.
fetch_next_action() {{
    local out rc
    out=$(egg-orch brc next-action --role "${{EGG_AGENT_ROLE:-unknown}}" --json 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
        echo "$out"
        return 0
    fi
    # The CLI returns non-zero for any of: 409 stale_version, 409
    # aggregated-NACK barrier, 5xx, transport failure. Log it so
    # operators reading wrapper logs can distinguish "orchestrator
    # returned 409" (benign, expected) from "transport unreachable"
    # (worth checking). We always echo the fallback JSON so the parser
    # doesn't crash, and propagate the original ``rc`` as the function's
    # exit code (the caller reads ``$?`` after the ``$(...)`` capture).
    cw_log "brc next-action returned rc=$rc / empty body; falling back to {{\"action\":\"wait\"}}."
    echo '{{"action":"wait"}}'
    return "$rc"
}}

# Extract a top-level scalar field from a next-action JSON document.
# Nested objects (event_payload) are re-serialised as JSON so the
# bash caller can pass them downstream verbatim.
next_action_field() {{
    local action_json="$1"
    local field="$2"
    echo "$action_json" | python3 -c "
import sys, json
field = sys.argv[1]
try:
    d = json.load(sys.stdin)
except Exception:
    print(''); sys.exit(0)
v = d.get(field)
if isinstance(v, (dict, list)):
    print(json.dumps(v))
elif v is None:
    print('')
else:
    print(v)
" "$field" 2>/dev/null || echo ""
}}

# Invoke the agent one-shot with the per-event prompt. Slice-3
# (TASK-3-1 / TASK-3-2) replaces the slice-2 stub with the full
# ``compose_event_prompt`` payload: memory excerpt (when
# ``EGG_BRC_MEMORY=full``) + per-producer
# ``git log {{sha}}..HEAD --not origin/{{base}} -p`` delta + NACK
# payload + the contract's ``task_description`` (#3123, read from the
# worktree contract file via the pod-inherited ``EGG_PIPELINE_ID`` /
# ``EGG_ISSUE_NUMBER``). The composer is the ``routes/event_prompt``
# sub-package (#3312 slice-6); the wrapper invokes its
# ``__main__.py`` entry directly so the package's standalone CLI runs
# (``__main__`` bootstraps ``sys.path`` and calls ``_cli``) WITHOUT
# importing the heavy ``orchestrator.routes`` package ``__init__.py``
# (Flask import). ``EGG_EVENT_PROMPT_SCRIPT`` overrides the path for tests.
#
# When the composer fails (script missing, malformed memory file, git
# log subprocess crash) we fall back to the slice-2 minimal stub so the
# event-pump keeps running rather than failing the agent invocation.
# This is symmetric with the rest of the wrapper's "block, alert,
# continue" stance under the idle-budget safety net.
invoke_agent_for_event() {{
    local action="$1"
    local event_payload="$2"
    local role="${{EGG_AGENT_ROLE:-unknown}}"
    local slice="${{EGG_SLICE_ID:-none}}"
    local base_branch="${{EGG_BASE_BRANCH:-main}}"
    local script_path="${{EGG_EVENT_PROMPT_SCRIPT:-/opt/egg-runtime/orchestrator/routes/event_prompt/__main__.py}}"
    local prompt prompt_rc=1

    if [ -r "$script_path" ]; then
        # Pass the event_payload JSON via stdin so shell metacharacters
        # ($VAR, backticks, ;, &&) don't fall through to argv (the
        # #2741 / slice-5 motivating concern; even though this argv is
        # composed entirely by the wrapper here, the stdin path keeps
        # the surface honest and matches the slice-5 prose-arg rule).
        # All four env vars (``EGG_AGENT_ROLE`` / ``EGG_BASE_BRANCH`` /
        # ``EGG_REPO_PATH`` / ``EGG_BRC_MEMORY``) are read by the script
        # from env directly. The prefix MUST attach to ``python3`` (RHS),
        # not ``printf`` (LHS) -- the earlier form attached only to
        # ``printf`` and ``python3`` inherited from the parent shell.
        # ``EGG_REPO_PATH`` is re-exported explicitly here
        # (reviewer_holistic v2 #2): the script falls back to
        # ``os.getcwd()`` when unset, but propagating the
        # orchestrator-set value keeps the wrapper symmetric and immune
        # to an unset-in-parent edge case. Capture stderr to a temp
        # file so the cw_log fallback surfaces the first line of the
        # failure (script-not-found vs schema-drift vs crash otherwise
        # indistinguishable).
        local err_tmp
        err_tmp=$(mktemp -t event-prompt-stderr.XXXXXX 2>/dev/null || echo "/tmp/event-prompt-stderr-$$.log")
        prompt=$(printf '%s' "$event_payload" \
            | EGG_AGENT_ROLE="$role" \
                EGG_BASE_BRANCH="$base_branch" \
                EGG_REPO_PATH="${{EGG_REPO_PATH:-$PWD}}" \
                EGG_BRC_MEMORY="${{EGG_BRC_MEMORY:-full}}" \
                python3 "$script_path" "$action" 2>"$err_tmp")
        prompt_rc=$?
    fi

    if [ "$prompt_rc" -ne 0 ] || [ -z "$prompt" ]; then
        # Fallback prompt -- keep the event-pump moving rather than
        # failing the agent invocation when the composer is unavailable
        # (script missing, schema drift, transient git log failure).
        # The idle-budget safety net catches a wedged event-pump even
        # under a degraded composer; failing here would defeat that.
        local err_head=""
        if [ -n "${{err_tmp:-}}" ] && [ -r "$err_tmp" ]; then
            err_head=$(head -1 "$err_tmp" 2>/dev/null)
        fi
        if [ -n "$err_head" ]; then
            cw_log "compose_event_prompt unavailable (rc=$prompt_rc, stderr: $err_head); using slice-2 stub prompt."
        else
            cw_log "compose_event_prompt unavailable (rc=$prompt_rc); using slice-2 stub prompt."
        fi
        prompt=$(printf 'BRC event-pump handler\nRole: %s\nSlice: %s\nAction: %s\nEvent payload (JSON): %s\n\nHandle this single event according to the role contract, update durable BRC memory, then exit naturally. The wrapper will invoke you again with the next event.\n' \
            "$role" "$slice" "$action" "$event_payload")
    fi
    # Best-effort cleanup of the stderr capture file. The trap on the
    # outer wrapper handles SIGTERM cleanup; this cleanup keeps a busy
    # event-pump from accumulating stale per-invocation temp files.
    if [ -n "${{err_tmp:-}}" ] && [ -e "$err_tmp" ]; then
        rm -f "$err_tmp" 2>/dev/null || true
    fi
    # #3077 slice-1 task-1-1: prepend any sync-to-proposal failure
    # banners the prior ``sync_to_proposals`` accumulated so the agent
    # learns that its worktree may not reflect the producer's commit
    # BEFORE it reads any local diff. The common (no-failure) path
    # leaves ``SYNC_FAILURE_BANNERS`` empty so the prompt handed to the
    # agent is byte-identical to the slice-0 prompt. Re-clear after
    # consumption so a subsequent ``propose`` arm (which intentionally
    # skips the sync per R11a) can never inherit stale banners.
    if [ -n "${{SYNC_FAILURE_BANNERS:-}}" ]; then
        prompt="${{SYNC_FAILURE_BANNERS}}${{prompt}}"
        SYNC_FAILURE_BANNERS=""
    fi
    # Warm-resume session-store sync (#3278). The orchestrator sets
    # ``EGG_SESSION_STATE_FILE`` only when warm resume is enabled, so its
    # presence gates the round-trip. ``pull`` re-materialises the prior
    # event's transcript + pointer into this pod's ephemeral Claude store so
    # ``--resume`` (the slice-8 gate's resume branch) finds a real session;
    # ``push`` ships the updated session back after the agent exits. Both are
    # best-effort (``|| true``, bounded by ``timeout``) — a failed sync
    # degrades to a safe cold reseed and never wedges the event. The agent's
    # own return code is preserved as this function's rc (the one-shot
    # handler exits with it), so the post-agent push can't mask it.
    if [ -n "${{EGG_SESSION_STATE_FILE:-}}" ]; then
        timeout 60 egg-orch session-state pull 2>&1 | sed 's/^/[session-state] /' >&2 || true
    fi
{tool_call_cap_block}    {agent_command_prefix} "$prompt"
    local _agent_rc=$?
    if [ -n "${{EGG_SESSION_STATE_FILE:-}}" ]; then
        timeout 60 egg-orch session-state push 2>&1 | sed 's/^/[session-state] /' >&2 || true
    fi
    return "$_agent_rc"
}}

# Sync-to-proposal (#3076 / #3077 clause 2): before a review invocation
# (ack/nack), merge each pending producer's proposed commit into this
# reviewer's worktree so reviewers that must RUN the proposal (tester)
# have a real checkout. This is deterministic wrapper bash replacing
# the fetch/merge prose that previously lived in spawn prompts the
# event pump provably discards (#3033). Fail-soft at every step: an
# unresolvable SHA, a conflicting merge, or a dirty tree logs and
# continues — the per-event prompt's `git show <sha>:<path>` reads
# (#3078) work from the shared object store either way, so the agent
# is never blocked on this sync succeeding.
#
# NB: ``git merge --no-edit`` produces a real merge commit (or fast-
# forwards) — HEAD advances and is NOT reset between events. Two
# durable side effects follow:
#   - Dual-role agents (e.g. tester acting as producer-then-reviewer-
#     then-producer): a subsequent producer turn commits on top of an
#     ancestry that contains peer proposal commits. The producer arm
#     (R11a, see the dispatcher below) intentionally skips the sync,
#     but it does NOT un-merge syncs from prior reviewer arms.
#   - Multiple producers per event: SHAs are merged sequentially. A
#     conflict on the second SHA aborts that merge but leaves the
#     first merge intact on HEAD.
# Neither is wrong — the per-event-prompt ``git show <sha>:<path>``
# fallback covers the failure path either way — but this is durable
# HEAD mutation, not transient enrichment.
# Per-event accumulator for sync-to-proposal failure banners
# (#3077 slice-1 task-1-1). Reset at the top of every
# ``sync_to_proposals`` call; appended to per failed SHA; consumed
# (and re-cleared) by ``invoke_agent_for_event`` which prepends the
# accumulated banner text to the composed event prompt BEFORE handing
# it to the agent. The default empty value keeps the
# ``merged``/``already-ancestor`` (no-failure) path byte-identical to
# the slice-0 prompt — the prepend is a no-op when this variable is
# empty.
SYNC_FAILURE_BANNERS=""

sync_to_proposals() {{
    # #3216 (WS1 of #3209): only reviewers that EXECUTE the proposal (run
    # the test suite / build against the merged tree) need a working-tree
    # merge. Every other reviewer reads peer artifacts via this prompt's
    # ``git show`` / ``egg-artifact`` served reads, so merging the peer's
    # *whole* tree into their worktree buys nothing and risks the dual-role
    # criss-cross propagation that corrupts shared drafts (#3208: a reviewer
    # arm merges a peer's plan.md, a later producer turn commits on top, and
    # the cross-merged lineages spawn spurious conflicts / a rebase-mangled
    # yaml fence / a "File modified since read" livelock). Allowed roles are
    # rendered from ``REVIEWER_CHECKOUT_ROLE_VALUES``; default-deny so an
    # unset/unknown role reads via git-show (the safe, non-replicating side).
    local _role="${{EGG_AGENT_ROLE:-}}"
    case " {checkout_roles} " in
        *" $_role "*) : ;;  # role runs the proposal; a real merged checkout is required
        *)
            SYNC_FAILURE_BANNERS=""
            cw_log "sync-to-proposal: role=$_role reads peer artifacts via git-show/egg-artifact; skipping working-tree merge (#3216)."
            return 0
            ;;
    esac
    local event_payload="$1"
    local repo="${{EGG_REPO_PATH:-$PWD}}"
    local shas sha
    # Reset the per-event failure accumulator at the top of each call
    # so banners from a prior event never leak into the next prompt.
    SYNC_FAILURE_BANNERS=""
    # Extract pending_reviews[].proposal_commit_sha, strictly hex-
    # validated (7-64 chars) before any git interpolation. The payload
    # is orchestrator-composed, but the producer-supplied SHA rides
    # through it, so revalidate at the consumer — same stance as
    # event_prompt.py's _extract_proposal_sha_for_producer. The
    # ProposalPayload writer-side check also admits non-hex sentinels
    # like RECONSTRUCTED_NO_SHA; the hex requirement here filters those
    # out (there is nothing to merge for a reconstructed proposal).
    shas=$(printf '%s' "$event_payload" | python3 -c "
import sys, json, re
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
pat = re.compile(r'[0-9a-fA-F]{{7,64}}')
out = []
for pr in (d.get('pending_reviews') or []):
    if not isinstance(pr, dict):
        continue
    sha = str(pr.get('proposal_commit_sha') or '')
    if pat.fullmatch(sha) and sha not in out:
        out.append(sha)
print('\n'.join(out))
" 2>/dev/null)
    [ -z "$shas" ] && return 0
    while IFS= read -r sha; do
        [ -z "$sha" ] && continue
        if ! git -C "$repo" cat-file -e "$sha^{{commit}}" 2>/dev/null; then
            # Per-role worktrees share the host repo's object store, so
            # the SHA normally resolves without network; a best-effort
            # fetch covers split-object-store runtimes.
            git -C "$repo" fetch --quiet origin >/dev/null 2>&1 || true
        fi
        if ! git -C "$repo" cat-file -e "$sha^{{commit}}" 2>/dev/null; then
            cw_log "sync-to-proposal: $sha unresolvable in $repo; reviewer falls back to the prompt's git-show reads."
            cw_log "sync-to-proposal: outcome=unresolvable sha=$sha"
            # ``unresolvable`` failure: append a NOT-synced banner so
            # the agent treats its local diff as unreliable for this
            # SHA and falls back to the rendered ``git show`` commands.
            SYNC_FAILURE_BANNERS+="> **WARNING:** worktree NOT synced to \`$sha\` (\`unresolvable\`); treat your local diff as unreliable — use the rendered \`git log\` / \`git show\` fallback commands in this prompt instead."$'\n\n'
            continue
        fi
        if git -C "$repo" merge-base --is-ancestor "$sha" HEAD 2>/dev/null; then
            cw_log "sync-to-proposal: outcome=already-ancestor sha=$sha"
            continue
        fi
        if git -C "$repo" merge --no-edit "$sha" >/dev/null 2>&1; then
            cw_log "sync-to-proposal: merged proposal commit $sha into the worktree."
            cw_log "sync-to-proposal: outcome=merged sha=$sha"
        else
            git -C "$repo" merge --abort >/dev/null 2>&1 || true
            cw_log "sync-to-proposal: merge of $sha failed (conflict or dirty tree); aborted — reviewer reads via git show instead."
            cw_log "sync-to-proposal: outcome=merge-failed sha=$sha"
            # ``merge-failed`` failure: same banner shape as
            # ``unresolvable`` so the agent learns NOT to trust the
            # local diff and falls back to ``git show``. The merge
            # was already aborted above so wrapper state is clean.
            SYNC_FAILURE_BANNERS+="> **WARNING:** worktree NOT synced to \`$sha\` (\`merge-failed\`); treat your local diff as unreliable — use the rendered \`git log\` / \`git show\` fallback commands in this prompt instead."$'\n\n'
        fi
    done <<< "$shas"
    return 0
}}

# Restore the repo's prebuilt build_commands toolchain (#3413): copy
# /opt/prebuilt-deps/<owner--repo>/* (the persist_dirs snapshot baked at
# image build, e.g. the repo's pinned .venv) into the mounted worktree,
# skipping paths that already exist. This is the agent-path twin of
# ``sandbox.entrypoint._worktrees.restore_prebuilt_deps`` and the #3412
# green-gate runner's ``restore_prebuilt``: k8s agent pods override the
# image ENTRYPOINT (the orchestrator injects this wrapper as the pod
# command), so the entrypoint's restore never runs on this path and repo
# checks would otherwise resolve to whatever tools the image happens to
# install globally instead of the versions the repo pins. Copy-if-missing
# keeps it idempotent across the many one-shot pods that share a role
# worktree — only the first pod per worktree pays the copy. No chown
# needed: this pod runs as the worktree's owning uid, unlike the root
# entrypoint. Fail-soft: a failed restore logs and continues — the agent
# invocation is never blocked on it, and a missing toolchain surfaces at
# check time instead. ``EGG_PREBUILT_DEPS_BASE`` overrides the snapshot
# source for tests.
restore_prebuilt_deps() {{
    local repo="${{EGG_REPO_PATH:-$PWD}}"
    local out rc
    out=$(python3 - "$repo" 2>&1 <<'RESTORE_PREBUILT_PY'
import os
import shutil
import sys

repo_dir = sys.argv[1]
base = os.environ.get("EGG_PREBUILT_DEPS_BASE", "/opt/prebuilt-deps")
name = os.path.basename(os.path.normpath(repo_dir))
if not os.path.isdir(base) or not os.path.isdir(repo_dir):
    print("skipped (no prebuilt base or repo dir)")
    sys.exit(0)


def copy_if_missing(src, dst, **kwargs):
    if os.path.exists(dst) or os.path.islink(dst):
        return
    # Mirror the entrypoint's _copy_if_missing: log-and-continue per file
    # so a single unreadable/mode-600 source degrades to a per-file warning
    # instead of flipping the whole restore to a "restore failed" line via
    # shutil.copytree's collected shutil.Error.
    try:
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst, **kwargs)
    except OSError as e:
        print("  warn: failed to restore " + dst + ": " + str(e))


restored = None
for entry in sorted(os.listdir(base)):
    if entry == "__egg_system_dirs__" or not entry.endswith("--" + name):
        continue
    shutil.copytree(
        os.path.join(base, entry),
        repo_dir,
        copy_function=copy_if_missing,
        dirs_exist_ok=True,
        symlinks=False,
    )
    restored = entry
    break
if restored is None:
    print("no prebuilt snapshot for " + name)
else:
    print("restored " + restored)
RESTORE_PREBUILT_PY
)
    rc=$?
    if [ "$rc" -ne 0 ]; then
        cw_log "prebuilt-deps restore failed (rc=$rc, fail-soft): ${{out:-<no output>}}"
    else
        cw_log "prebuilt-deps restore: $out"
    fi
    return 0
}}

# Trust the gateway proxy CA for TLS-bumped hosts (#3459): k8s agent
# pods override the image ENTRYPOINT (this wrapper IS the pod command),
# so the sandbox entrypoint's ``setup_gateway_ca()`` never runs and no
# shared-certs volume is mounted. Fetch the current CA from the
# gateway's public ca-cert endpoint (#3458) and export
# NODE_EXTRA_CA_CERTS so node/npm/pnpm validate TLS-bumped hosts — the
# GitHub Packages npm read-through (#3456) — without per-run
# hand-wiring. Compose pods inherit NODE_EXTRA_CA_CERTS from the
# entrypoint (run_exec copies os.environ), so the already-set guard
# keeps that path byte-identical. Fetching per-spawn also stays correct
# across gateway restarts, which regenerate the CA. Fail-soft: a failed
# fetch logs and continues — the agent invocation is never blocked on
# it, and only installs from TLS-bumped registries would later fail,
# with a certificate error at install time.
#
# Scope: NODE_EXTRA_CA_CERTS only — deliberately narrower than the
# Compose entrypoint, which also sets REQUESTS_CA_BUNDLE/SSL_CERT_FILE
# and installs the CA into the system trust store. The only TLS-bumped
# host today is npm.pkg.github.com (Node-only), so the Node var is
# sufficient here. A future Python/curl-side TLS-bumped read-through
# would need the extra vars exported on this k8s path too.
setup_gateway_ca() {{
    if [ -n "${{NODE_EXTRA_CA_CERTS:-}}" ]; then
        cw_log "gateway-ca: NODE_EXTRA_CA_CERTS already set (entrypoint path); skipping fetch."
        return 0
    fi
    if [ -z "${{GATEWAY_URL:-}}" ]; then
        cw_log "gateway-ca: GATEWAY_URL unset; skipping CA fetch (fail-soft)."
        return 0
    fi
    local ca_file="${{TMPDIR:-/tmp}}/gateway-ca.crt"
    if curl -sf --connect-timeout 5 --max-time 15 \
            "$GATEWAY_URL/api/v1/proxy/ca-cert" -o "$ca_file" \
            && [ -s "$ca_file" ]; then
        export NODE_EXTRA_CA_CERTS="$ca_file"
        cw_log "gateway-ca: fetched proxy CA; exported NODE_EXTRA_CA_CERTS=$ca_file"
    else
        rm -f "$ca_file" 2>/dev/null || true
        cw_log "gateway-ca: CA fetch failed (fail-soft); TLS-bumped registry installs will not trust the gateway CA."
    fi
    return 0
}}

"""


# One-shot event handler (#3164). Appended verbatim after
# ``_EVENT_PUMP_WRAPPER_TEMPLATE.format(...)`` runs, so it uses LITERAL
# braces (no ``{{``/``}}`` doubling) and is NOT passed through
# ``str.format``. It relies on the helper functions defined above
# (``fetch_next_action``, ``next_action_field``, ``sync_to_proposals``,
# ``invoke_agent_for_event``, ``emit_heartbeat``, ``cw_log``).
#
# The orchestrator spawns this wrapper ONCE per actionable BRC event with
# ``EGG_EVENT_ACTION`` (in propose|ack|nack) injected. The handler:
#   * Refuses loudly (exit 64) if no action was injected -- under the
#     orchestrator-owned loop every pod is a one-shot event spawn, so a
#     missing action is a caller bug, not a fall-through to an in-pod loop
#     (which no longer exists).
#   * Refuses ``confirm``/``complete`` (exit 64) -- those run
#     orchestrator-side with no pod and must never reach a wrapper.
#   * Re-checks next-action ONCE as a stale-event backstop, with three
#     distinct exit codes the orchestrator supervisor reads:
#       - inconclusive re-check (fetch rc != 0) -> exit 75 (EX_TEMPFAIL)
#         WITHOUT invoking; the supervisor re-derives.
#       - derived action moved on -> exit 0 WITHOUT invoking (stale dedupe).
#       - re-check confirms the event -> invoke once, exit the agent rc.
_ONE_SHOT_EVENT_HANDLER = r"""# --- one-shot event handler (#3164) ------------------------------------
ONE_SHOT_ACTION="${EGG_EVENT_ACTION:-}"
if [ -z "$ONE_SHOT_ACTION" ]; then
    cw_log "FATAL: no EGG_EVENT_ACTION injected. Under the orchestrator-owned event loop (#3164) every agent pod is spawned one-shot per BRC event; a wrapper with no injected action is a caller bug. Refusing (exit 64)."
    exit 64
fi
cw_log "One-shot event handler engaged (action=$ONE_SHOT_ACTION, dedupe=${EGG_EVENT_DEDUPE_KEY:-none}, role=${EGG_AGENT_ROLE:-?}, slice=${EGG_SLICE_ID:-none})."

case "$ONE_SHOT_ACTION" in
    confirm|complete)
        # confirm/complete are agent-free and run orchestrator-side (no
        # pod is ever spawned for them). Reaching a wrapper with one
        # injected is a caller bug -- reject loudly.
        cw_log "FATAL: action=$ONE_SHOT_ACTION must run orchestrator-side with no pod and must never be injected into a wrapper. Refusing (exit 64)."
        exit 64
        ;;
    propose|ack|nack)
        : ;;
    *)
        cw_log "FATAL: unknown injected EGG_EVENT_ACTION='$ONE_SHOT_ACTION' (expected propose|ack|nack). Refusing (exit 64)."
        exit 64
        ;;
esac

# Single foreground liveness ping (no background emitter): the
# silent-mid-event tripwire keys on heartbeats from an active one-shot pod.
emit_heartbeat "WORKING" "one-shot event handler action=$ONE_SHOT_ACTION"

# Stale-event backstop: re-derive next-action ONCE. If consensus has moved
# on (derived action != injected event) this spawn is a duplicate/stale
# delivery -- exit 0 WITHOUT invoking the agent. The spawner dedupes on the
# derived event, but a race can still deliver a stale one; this is the
# in-pod backstop for that race.
ONE_SHOT_NEXT_JSON=$(fetch_next_action)
ONE_SHOT_FETCH_RC=$?
ONE_SHOT_DERIVED=$(next_action_field "$ONE_SHOT_NEXT_JSON" "action")
if [ "$ONE_SHOT_FETCH_RC" -ne 0 ]; then
    # The re-check itself did NOT cleanly succeed. ``fetch_next_action``
    # returns its ``{"action":"wait"}`` fallback on ANY non-zero rc (409
    # stale_version / aggregated-NACK barrier, 5xx, transport failure
    # alike), so we cannot distinguish a genuinely stale event from a
    # transient blip. Exiting 0 here would report a "clean handoff" and let
    # a transient blip silently DROP a live event. Instead exit 75
    # (EX_TEMPFAIL): a distinct code telling the orchestrator supervisor the
    # freshness re-check was inconclusive, so it must re-derive next-action
    # rather than treat the event as handled.
    cw_log "One-shot re-check did not cleanly succeed (fetch rc=$ONE_SHOT_FETCH_RC); cannot confirm event freshness. Exiting 75 (EX_TEMPFAIL) so the supervisor re-derives rather than assuming a clean handoff."
    exit 75
fi
if [ "$ONE_SHOT_DERIVED" != "$ONE_SHOT_ACTION" ]; then
    # Re-check succeeded and the live action has positively moved on: a
    # genuinely stale/duplicate delivery. exit 0 means "confirmed stale, no
    # agent needed" -- NOT "could not tell".
    cw_log "Injected event is stale (injected=$ONE_SHOT_ACTION, derived=${ONE_SHOT_DERIVED:-<none>}); exiting 0 without invoking the agent."
    exit 0
fi

# Restore the repo-pinned prebuilt toolchain (#3413) after the freshness
# re-check (a stale event exits above without paying the copy) and before
# any arm runs — producer arms run repo checks too. Fail-soft inside the
# function; the agent invocation is never blocked on it.
restore_prebuilt_deps

# Trust the gateway proxy CA (#3459), same placement rationale: after the
# freshness re-check (a stale event exits without paying the fetch),
# before any arm runs, so the exported NODE_EXTRA_CA_CERTS is inherited
# by the agent and every subprocess it spawns. Fail-soft inside the
# function; the agent invocation is never blocked on it.
setup_gateway_ca

# Use the freshly-derived event_payload (current truth) for the invocation.
# Reviewer arms (ack/nack) sync the pending proposal commits into the
# worktree first (#3076/#3077); the producer ``propose`` arm intentionally
# does not (R11a -- a producer's own commits are already on HEAD).
ONE_SHOT_PAYLOAD=$(next_action_field "$ONE_SHOT_NEXT_JSON" "event_payload")
if [ "$ONE_SHOT_ACTION" = "ack" ] || [ "$ONE_SHOT_ACTION" = "nack" ]; then
    sync_to_proposals "$ONE_SHOT_PAYLOAD"
fi

one_shot_start=$SECONDS
invoke_agent_for_event "$ONE_SHOT_ACTION" "$ONE_SHOT_PAYLOAD"
one_shot_rc=$?
one_shot_secs=$(( SECONDS - one_shot_start ))
cw_log "One-shot invocation done (action=$ONE_SHOT_ACTION, rc=$one_shot_rc, duration=${one_shot_secs}s); exiting with the agent rc."
# Exit-code contract for the orchestrator supervisor: 75/EX_TEMPFAIL is
# RESERVED for the inconclusive-re-check outcome above ("freshness could
# not be confirmed, re-derive next-action"). Agent passthrough rcs do not
# include 75, so the two meanings never collide. The agent CLI's auth-fatal
# code (77/EX_AUTH_FATAL, egg_agent.auth_errors.EX_AUTH_FATAL — a
# non-retryable credential/quota failure, #3373) is a genuine agent
# passthrough rc and rides through here unchanged; the orchestrator event
# loop reads it off the failed pod and fast-fails the dedupe key.
exit "$one_shot_rc"
"""


def build_event_pump_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 1000,
    effort: str | None = None,
) -> list[str]:
    """Compose the one-shot event wrapper bash command (#3164).

    Public entry-point shared by ``build_consensus_wrapped_command``.

    The ``prompt_text`` argument is accepted for signature parity with the
    historical entry-point; the wrapper emits its own per-event prompt
    inside ``invoke_agent_for_event`` from the rendered
    ``compose_event_prompt`` output, so the initial prompt is not
    interpolated into the bash directly.
    """
    del prompt_text  # interface parity; per-event prompts are composed in-wrapper

    agent_prefix_parts = [
        "python3",
        "-m",
        "egg_agent",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
    ]
    if effort is not None:
        # Pin the reasoning effort instead of inheriting the installed
        # Claude Code build's per-model default (AgentModelDecision.effort).
        agent_prefix_parts.extend(["--effort", effort])
    agent_command_prefix = " ".join(shlex.quote(p) for p in agent_prefix_parts)

    # Per-finding tool-call cap (#3523 S4): resolve the staged flag + cap
    # here (build time, in the orchestrator process) and render the export
    # block. ``off`` mode yields an empty block, so the spawn command stays
    # byte-identical to the pre-S4 legacy path.
    tool_call_cap_block = _render_tool_call_cap_env_block(
        review_findings_mode(), review_finding_tool_call_cap()
    )

    script = _EVENT_PUMP_WRAPPER_TEMPLATE.format(
        agent_command_prefix=agent_command_prefix,
        checkout_roles=EVENT_PUMP_CHECKOUT_ROLES,
        tool_call_cap_block=tool_call_cap_block,
    )
    # The triple-quoted template opens with a newline (so the source reads
    # cleanly); strip it so ``#!/bin/bash`` lands on line 1.
    script = script.lstrip("\n")
    # The orchestrator owns the event loop (#3164): every spawn is one-shot,
    # so the one-shot event handler is always appended. There is no in-pod
    # wait loop and no ownership flag to branch on.
    script = script.rstrip("\n") + "\n\n" + _ONE_SHOT_EVENT_HANDLER
    return ["bash", "-c", script]


def _render_finding_anchor(anchor: FindingAnchor) -> str:
    """Render a finding anchor as a compact ``path:line`` locator.

    Empty string for a slice-level / unanchored finding — the caller omits the
    ``where:`` line entirely rather than printing a bare colon.
    """
    if anchor.slice_level or not anchor.path:
        return ""
    if anchor.line_start and anchor.line_end and anchor.line_end != anchor.line_start:
        return f"{anchor.path}:{anchor.line_start}-{anchor.line_end}"
    if anchor.line_start:
        return f"{anchor.path}:{anchor.line_start}"
    return anchor.path


def render_findings_nack_reason(computed: ComputedVerdict) -> str:
    """Render the producer-facing NACK reason from a computed verdict (#3523 S3).

    This is the "rendering" half of the S3 determinism-boundary split
    (``orchestrator/review_findings_verdict.py`` owns dedup + the ACK/NACK
    outcome; this owns turning the resulting findings into the prose the
    producer sees in ``ApprovalEntry.reason``). It lives here — the shared
    consensus-wrapper serial spine — so the S3/S4 edits to this file serialise
    cleanly.

    Only the blocking findings drive the reason (they are what the producer
    must fix); the merged convergence signal (``converged_roles``) is surfaced
    inline so a finding corroborated by multiple lenses reads as higher-signal,
    and any advisory obligations are appended as a non-blocking footer. Returns
    a stable, deterministic string suitable for a unit-test golden.
    """
    blocking = computed.blocking_findings
    count = len(blocking)
    plural = "s" if count != 1 else ""
    lines: list[str] = [
        f"{count} blocking finding{plural} must be addressed before this proposal can be ACKed:"
    ]

    for index, finding in enumerate(blocking, start=1):
        header = f"{index}. [{finding.role}]"
        if len(finding.converged_roles) >= 2:
            header += (
                f" (converged across {len(finding.converged_roles)} lenses: "
                + ", ".join(finding.converged_roles)
                + ")"
            )
        header += f" {finding.summary}"
        lines.append(header)

        location = _render_finding_anchor(finding.anchor)
        if location:
            lines.append(f"   where: {location}")
        if finding.failure_scenario.strip():
            lines.append(f"   failure scenario: {finding.failure_scenario.strip()}")
        if finding.evidence.strip():
            lines.append(f"   evidence: {finding.evidence.strip()}")
        if finding.suggested_patch and finding.suggested_patch.strip():
            lines.append(f"   suggested fix: {finding.suggested_patch.strip()}")

    if computed.obligations:
        lines.append("")
        lines.append("Advisory (non-blocking) pre-merge obligations noted:")
        lines.extend(f"- {obligation}" for obligation in computed.obligations)

    return "\n".join(lines)


def build_consensus_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 1000,
    effort: str | None = None,
) -> list[str]:
    """Build a shell command that runs the agent under the one-shot BRC wrapper.

    A thin alias for :func:`build_event_pump_wrapped_command`, retained so
    call sites in ``concurrent_executor.py`` and ``kubernetes_spawner.py``
    do not have to be renamed. The orchestrator owns the event loop
    (#3164): the wrapper is spawned one-shot per BRC event and there is no
    in-pod wait loop.

    Args:
        prompt_text: Initial prompt (reserved — the wrapper emits its own
            per-event prompts inside ``invoke_agent_for_event`` from the
            rendered ``compose_event_prompt`` output, so the initial prompt
            is not interpolated into the bash directly. Accepted for
            interface parity with the historical signature).
        model: Agent model to use.
        max_turns: Maximum number of tool-call turns per agent
            invocation.
        effort: Reasoning effort to pin via ``--effort`` (e.g.
            ``"high"``), or ``None`` to inherit Claude Code's
            per-model default.

    Returns:
        Command list suitable for container spawning (``bash -c "..."``).
    """
    return build_event_pump_wrapped_command(
        prompt_text,
        model=model,
        max_turns=max_turns,
        effort=effort,
    )
