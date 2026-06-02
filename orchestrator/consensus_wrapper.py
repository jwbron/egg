"""Build consensus-wrapped commands for concurrent agent containers.

DESIGN INTENT — SAFETY NET, NOT PRIMARY MECHANISM
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The consensus wrapper exists as a **safety net** for agent exits that
should not normally happen.  The intended lifecycle is:

1. All agents run concurrently with enough ``max_turns`` (default 1000)
   to complete their work AND remain alive for the full BRC consensus
   protocol — including stay-alive polling while other agents finish.
2. The orchestrator detects consensus and sends SIGTERM to stop all
   containers.  Agents should only exit because the orchestrator tells
   them to, not because they exhausted turns.

The wrapper handles the edge case where an agent exits prematurely
(e.g. context exhaustion on an unusually long phase) by restarting it
with a recovery prompt so it can re-join consensus.  This restart path
is expensive — it requires reloading context and re-evaluating BRC
state — so it should be rare.

If the agent exits without reaching CONFIRMED state in the BRC protocol,
the wrapper restarts the agent with a prompt that explains what happened and
instructs it to assess state, then continue the BRC protocol. Restarts
are capped at ``MAX_CONSENSUS_RESTARTS`` (default 3). After exhausting
restarts the wrapper exits with code 1 so the orchestrator's failure path
handles escalation (Option A — producer permanent death transitions the
pipeline to FAILED; see issue #2806). Each restart also publishes an
``OVERSEER_ALERT`` so the operator sees every recovery attempt rather than
only learning about the cohort after the wrapper gives up.

EVENT-PUMP REFRAMING (#2908 slice-2, gated by ``EGG_BRC_EVENT_PUMP``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The capped-restart model above leans on the agent re-entering a
blocking ``egg-orch message wait-loop`` between BRC events. Models
that exit naturally after one match (qwen3.7-max in #2906, lineage
back to #2323 / #2064 / #2482 / #2036) burn the restart budget and
hard-fail (#2806).

When ``EGG_BRC_EVENT_PUMP=true`` is set on the orchestrator pod at
``build_consensus_wrapped_command`` composition time, the wrapper
emits a *deterministic event-pump* bash branch instead of the
capped-restart template. The pump:

* fetches BRC state via ``egg-orch brc get-state`` (#2908 task-1-3);
* asks ``egg-orch brc next-action`` what to do (#2908 task-1-1);
* on ``wait`` blocks on ``egg-orch message wait-loop`` while emitting
  ``egg-orch message heartbeat`` (#2036 migrated from
  ``handlers/message.py:267-429``) and refreshing the gateway-session
  via the same heartbeat (#2451 migrated -- heartbeats carry
  ``slice_id`` so ``_maybe_attach_slice_id`` in the orchestrator
  fan-out refreshes the slice-scoped container session);
* on ``propose|ack|nack`` invokes the agent one-shot via
  ``python3 -m egg_agent`` with the per-event prompt (slice-3 wires
  the full ``compose_event_prompt`` payload -- slice-2 ships a minimal
  stub so the structure is in place);
* on ``confirm``/``complete`` calls ``egg-orch consensus confirmed``
  (NOT ``progress complete`` -- that command doesn't exist; the
  pseudocode-typo guard test in task-2-6 (vii.b) pins this);
* trips an ``OVERSEER_ALERT`` (anomaly ``stuck-phase-transition``)
  when the idle budget ``EGG_BRC_IDLE_BUDGET_MIN`` (default 30 min,
  od-4) expires, raising priority on the 2x boundary, and keeps
  blocking (NOT exit 1 -> FAILED, replacing the
  ``MAX_CONSENSUS_RESTARTS`` cap per #2908 task-2-3).

Slice-4 task-4-1 flipped the default to ``true``: the event-pump
template is now the production path, and the legacy capped-restart
template above remains available for a one-release rollback window
via ``EGG_BRC_EVENT_PUMP=false``. Slice-4 task-4-2 deletes the
legacy template (and the ``EGG_BRC_EVENT_PUMP`` env flag along with
it) once the new path has been validated in production traffic.
``EGG_BRC_MEMORY`` follows suit: slice-4 task-4-1 flipped its
default from ``off`` to ``full`` so the event-pump composer reads
the durable memory file by default.
"""

import os
import shlex

# Default maximum number of times the wrapper will restart the agent after a
# clean exit without consensus being reached. Bumped from 2 → 3 per issue
# #2806 to give one more recovery attempt before the orchestrator hard-fails
# the pipeline on producer permanent death.
MAX_CONSENSUS_RESTARTS = 3

# Default maximum number of poll cycles to wait for consensus when the agent
# already signaled READY. With a default poll interval of 30s, this gives
# 10 * 30 = 300 seconds (5 minutes) for other agents to finish.
MAX_READY_POLL_CYCLES = 10

# Default initial backoff (in seconds) when restarting after a transient crash
# (signal-based exit codes like SIGSEGV, SIGKILL, etc.). The backoff doubles
# after each consecutive crash restart, capped at 30 seconds.
TRANSIENT_RESTART_BACKOFF_INITIAL = 5

# Window (in seconds) during which an exit code 1 is classified as a transient
# startup failure rather than a permanent error. The Agent SDK surfaces
# API-level errors (network blips, socket closes, 5xx responses during the
# first few turns) as success=False + exit 1, which exit-code alone cannot
# distinguish from a prompt-level failure. Agents that exit 1 within this
# window have almost certainly not done meaningful work yet, so the retry
# cost is negligible compared to stalling a BRC phase on a transient network
# hiccup. Agents that exit 1 after doing real work (past the window) are
# still treated as permanent failures.
STARTUP_FAILURE_WINDOW_SECONDS = 30

# System prompt injected on restart so the agent treats recovery instructions as
# trusted operator context (not user input that might be flagged as injection).
# Placeholders: {restart_number}, {max_restarts}, {brc_state}, {nack_feedback}
_RECOVERY_SYSTEM_PROMPT = (
    "# BRC Consensus Recovery\n\n"
    "This agent was restarted by the orchestrator's consensus wrapper because it "
    "exited without completing the BRC (Broadcast-Review-Converge) consensus protocol. "
    "This is restart {restart_number} of {max_restarts}.\n\n"
    "## Current BRC state\n\n"
    "{brc_state}\n\n"
    "{nack_feedback}"
    "{anchor_state}"
    "## Empty state recovery\n\n"
    "If BRC state is empty (`{{}}`), the in-memory tracker was likely lost "
    "(e.g. orchestrator restart). In this case:\n"
    "1. Run `egg-orch consensus status` to check if state was reconstructed.\n"
    "2. If you are already fully ACKed, call `egg-orch consensus confirmed` "
    "to re-confirm.\n"
    "3. If already confirmed, stay alive and poll — do NOT re-propose.\n\n"
    "## Required actions\n\n"
    "1. Check consensus status: `egg-orch consensus status`\n"
    "2. Poll for messages: `egg-orch message poll --wait 30`\n"
    "3. Based on your role type:\n"
    "   - **Producer**: If you received NACKs, address the reviewer feedback, "
    "revise your work, and re-propose (`egg-orch consensus propose`). "
    "If WORKING, complete work and propose. "
    "If PROPOSED, check for ACKs/NACKs and respond. If all ACKed, confirm "
    "(`egg-orch consensus confirmed`). "
    "**Do NOT re-propose if already fully ACKed** — call confirmed instead.\n"
    "   - **Reviewer**: Check for proposals from assigned producers. Review "
    "artifacts in git, then ACK (`egg-orch consensus ack <role>`) or "
    'NACK (`egg-orch consensus nack <role> --reason "..."`).\n'
    "     Once all assigned producers reviewed, confirm "
    "(`egg-orch consensus confirmed`).\n"
    "4. **Stay alive** — keep polling with `egg-orch message poll --wait 30`. "
    "The orchestrator will send SIGTERM when consensus is reached.\n\n"
    "If the agent exits again without reaching CONFIRMED, it will be restarted "
    "(up to the maximum).\n"
)

# Simple user prompt for recovery — the actual instructions are in the system prompt.
_RECOVERY_USER_PROMPT = (
    "Continue the BRC consensus protocol. Check your current state and "
    "take the appropriate next steps for your role."
)

# Shell script that wraps the agent invocation. After the agent exits:
# - Non-concurrent mode: exit normally.
# - Non-zero exit: check if consensus/confirmation reached first; if so
#   exit cleanly (issue #1495). Otherwise classify the exit code:
#   transient crashes (segfault, OOM, etc.) fall through to the restart
#   loop with backoff; non-transient failures exit immediately.
# - Clean exit (code 0): restart the agent with a recovery prompt (up to
#   MAX_RESTARTS times). After max restarts, exit 1 to trigger the
#   orchestrator's agent failure path (HITL decision).
_CONSENSUS_WRAPPER_TEMPLATE = r"""
#!/bin/bash
set -uo pipefail

MAX_RESTARTS={max_restarts}
RESTART_COUNT=0
CRASH_BACKOFF=0
TRANSIENT_BACKOFF_INITIAL={transient_backoff_initial}
TRANSIENT_BACKOFF_MAX=30
STARTUP_FAILURE_WINDOW_SECONDS={startup_failure_window_seconds}

# Capture agent stdout+stderr so the wrapper can post-mortem the run.
# Used by is_buffer_overflow() to detect the Claude Agent SDK
# message-reader JSON buffer crash (issue #2804) which is deterministic —
# retrying just hits the same overflow and burns the restart budget for
# no gain. With the reader buffer raised to 32 MiB on the egg path (#2884,
# see shared/egg_agent/client.py::_DEFAULT_SDK_MAX_BUFFER_BYTES) this is
# a rare backstop rather than the common path it was at the 1 MiB SDK
# default, but the wrapper still has to fail-fast when it does fire.
#
# Use ``mktemp`` for the default path so a co-tenant on the same host
# cannot pre-create a symlink at a predictable ``/tmp/agent-output-$$``
# location (``tee -a`` follows symlinks). The container is single-tenant
# in normal operation; mktemp is defense-in-depth for multi-tenant
# sandbox setups. The fallback to ``/tmp/agent-output-$$.log`` fires on
# any ``mktemp`` failure — missing from PATH, ``/tmp`` full (``ENOSPC``),
# tmpfs read-only, fd exhaustion, etc. The predictable-path attack
# window narrows considerably in practice (mktemp is in coreutils and
# the failure modes above are themselves rare), but the fallback is
# still a known weakening of the symlink protection rather than an
# unreachable branch.
if [ -z "${{AGENT_OUTPUT_LOG:-}}" ]; then
    AGENT_OUTPUT_LOG="$(mktemp -t agent-output.XXXXXX 2>/dev/null || echo "/tmp/agent-output-$$.log")"
fi

# Log wrapper messages to stderr so they never leak into agent SDK context.
cw_log() {{
    echo "[consensus-wrapper] $*" >&2
}}

run_agent() {{
    local prompt="$1"
    local system_prompt="${{2:-}}"
    : > "$AGENT_OUTPUT_LOG"  # truncate per run so old crashes don't bleed forward
    # Pipe stdout+stderr through tee so the post-mortem grep
    # (is_buffer_overflow) sees the agent's full output. We use a
    # single pipeline rather than per-stream process substitution
    # because bash waits on pipelines synchronously; process
    # substitution (> >(tee ...)) backgrounds the tee subshell and
    # doesn't wait, which races with the immediate is_buffer_overflow
    # grep that follows on agent exit.
    #
    # Note: ``2>&1 | tee -a`` interleaves stdout and stderr in the
    # captured log. This is intentional — the SDK overflow marker
    # is emitted on stderr (``logger.error`` in
    # ``claude_agent_sdk.query``), and the grep that triggers
    # is_buffer_overflow needs to see it in the same file as
    # stdout. Side-effect: any future log analysis that depends on
    # stdout/stderr separation will need to capture them separately
    # upstream (e.g. via ``script`` or a wrapper process), not from
    # ``$AGENT_OUTPUT_LOG``.
    if [ -n "$system_prompt" ]; then
        {agent_command_prefix} --system-prompt "$system_prompt" "$prompt" 2>&1 | tee -a "$AGENT_OUTPUT_LOG"
    else
        {agent_command_prefix} "$prompt" 2>&1 | tee -a "$AGENT_OUTPUT_LOG"
    fi
    return ${{PIPESTATUS[0]}}
}}

# Detect the Claude Agent SDK JSON message-reader overflow signature in
# the most recent agent run. Issue #2804. The overflow is deterministic:
# re-running the agent against the same codebase hits the same oversized
# tool result, so the wrapper must NOT consume retry budget on this
# failure class. Returns 0 (true) if the marker was logged, 1 otherwise.
#
# The substring matches CLI output from claude_agent_sdk emitted on
# the buffer overflow path. If a future SDK bump changes the
# wording, this grep silently falls through and the wrapper burns
# its retry budget again — the buffer-overflow tests in
# orchestrator/tests/test_consensus_wrapper.py (notably
# test_script_marker_matches_client_constant and the
# test_buffer_overflow_*_aborts_without_retry pair) exercise the
# wrapper against a synthetic log to keep this honest, but do not
# pin against the installed SDK. The real fix for the overflow class
# is the raised reader buffer (#2884, see
# shared/egg_agent/client.py::_DEFAULT_SDK_MAX_BUFFER_BYTES = 32 MiB);
# this fail-fast is the clean backstop for anything beyond it. The
# per-tool MCP @tool caps (#2805) and Read/Grep predictive caps (#2876)
# are independent model-context/cost discipline — not the crash fix.
is_buffer_overflow() {{
    [ -f "$AGENT_OUTPUT_LOG" ] || return 1
    grep -q "exceeded maximum buffer size" "$AGENT_OUTPUT_LOG" 2>/dev/null
}}

# Helper: extract BRC agent state from pipeline status JSON
get_brc_state() {{
    local response="$1"
    local role="$2"
    echo "$response" | python3 -c \
        "import sys,json; role=sys.argv[1]; d=json.load(sys.stdin); agent=d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('agents',{{}}).get(role,{{}}); print(json.dumps(agent))" \
        "$role" 2>/dev/null || echo "{{}}"
}}

get_agent_confirmed() {{
    local response="$1"
    local role="$2"
    echo "$response" | python3 -c \
        "import sys,json; role=sys.argv[1]; d=json.load(sys.stdin); agent=d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('agents',{{}}).get(role,{{}}); print(agent.get('confirmed',False))" \
        "$role" 2>/dev/null || echo "False"
}}

# Check if an agent is confirmed, with message bus fallback for when the
# in-memory consensus tracker was lost or stale (e.g. orchestrator restart,
# withdrawal cascade leaving stale state).
# Prints "True" or "False".
# NOTE: The --limit 1000 fallback pulls all messages, which is expensive.
# This only runs when the tracker does not show confirmed, so it should be rare.
check_agent_confirmed_with_fallback() {{
    local response="$1"
    local role="$2"
    local confirmed
    confirmed=$(get_agent_confirmed "$response" "$role")
    if [ "$confirmed" = "True" ]; then
        echo "True"
        return
    fi
    # Fallback: tracker does not show confirmed. This can happen when:
    # 1. The agents map is empty (tracker was lost, e.g. orchestrator restart)
    # 2. The agents map is stale (e.g. withdrawal cascade left outdated state)
    # In either case, check the message bus for our own CONSENSUS_CONFIRMED message.
    local agents_empty
    agents_empty=$(echo "$response" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); agents=d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('agents',{{}}); print('True' if not agents else 'False')" \
        2>/dev/null || echo "False")
    if [ "$agents_empty" = "True" ]; then
        cw_log "Consensus state empty (tracker lost?). Checking message bus..."
    else
        cw_log "Tracker shows not confirmed (stale?). Checking message bus..."
    fi
    local msg_response
    msg_response=$(egg-orch message poll --json --limit 1000 2>/dev/null || echo "[]")
    confirmed=$(echo "$msg_response" | python3 -c "
import sys, json
role = sys.argv[1]
try:
    msgs = json.load(sys.stdin)
    if isinstance(msgs, dict):
        msgs = msgs.get('data', msgs.get('messages', []))
    found = any(
        m.get('message_type') == 'CONSENSUS_CONFIRMED' and m.get('from_role') == role
        for m in msgs
    )
    print('True' if found else 'False')
except Exception:
    print('False')
" "$role" 2>/dev/null || echo "False")
    if [ "$confirmed" = "True" ]; then
        cw_log "Found own CONSENSUS_CONFIRMED in message bus. Already confirmed."
    fi
    echo "$confirmed"
}}

# Extract unresolved NACK feedback targeting this agent (as a producer)
get_nack_feedback() {{
    local response="$1"
    local role="$2"
    echo "$response" | python3 -c "
import sys, json
role = sys.argv[1]
d = json.load(sys.stdin)
nacks = d.get('data', {{}}).get('concurrent', {{}}).get('consensus', {{}}).get('unresolved_nacks', [])
my_nacks = [n for n in nacks if n.get('producer') == role]
if my_nacks:
    print('**UNRESOLVED NACKs — You MUST address these before re-proposing:**')
    for n in my_nacks:
        reason = n.get('reason') or 'no reason given'
        print(f\"- **{{n.get('reviewer', '?')}}**: {{reason}}\")
    print()
" "$role" 2>/dev/null || echo ""
}}

# Detect transient crashes (signal-based exits) that warrant a restart with backoff.
# Returns 0 (true) for transient, 1 (false) for permanent failures.
is_transient_crash() {{
    local code="$1"
    case "$code" in
        134|136|137|139|255) return 0 ;;  # SIGABRT, SIGFPE, SIGKILL/OOM, SIGSEGV, Bun segfault
        *) return 1 ;;
    esac
}}

# Detect transient startup failures: exit code 1 within the startup window.
# The Agent SDK reports API-level errors (socket close, 5xx, network) as
# success=False + exit 1, which looks identical to a permanent prompt error
# by exit code alone. Gating on agent lifetime distinguishes "died before
# doing any real work" (retry) from "completed work and failed at the end"
# (permanent). Returns 0 (true) if retryable, 1 (false) otherwise.
is_startup_failure() {{
    local code="$1"
    local duration="$2"
    if [ "$code" -ne 1 ]; then
        return 1
    fi
    if [ "$duration" -lt "$STARTUP_FAILURE_WINDOW_SECONDS" ]; then
        return 0
    fi
    return 1
}}

# --- Initial run ---
AGENT_START=$SECONDS
run_agent {initial_prompt}
AGENT_EXIT=$?
AGENT_DURATION=$((SECONDS - AGENT_START))

# If not in concurrent mode, exit normally
if [ "${{EGG_CONCURRENT_MODE:-}}" != "true" ]; then
    exit $AGENT_EXIT
fi

# Non-zero exit — but check if consensus was already reached or agent
# already confirmed before treating it as a failure.  Agents can exit
# with non-zero codes (e.g. context exhaustion, idle timeout) after
# successfully completing their BRC work.  See issue #1495.
if [ "$AGENT_EXIT" -ne 0 ]; then
    CW_RESPONSE=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
    CW_IS_COMPLETE=$(echo "$CW_RESPONSE" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('is_complete',False))" \
        2>/dev/null || echo "False")
    if [ "$CW_IS_COMPLETE" = "True" ]; then
        cw_log "Agent exited with code $AGENT_EXIT but consensus already reached. Exiting cleanly."
        exit 0
    fi

    # Check if this agent already confirmed in BRC — consensus may still
    # be in progress but our contribution is done.
    CW_AGENT_ROLE="${{EGG_AGENT_ROLE:-}}"
    if [ -n "$CW_AGENT_ROLE" ]; then
        CW_AGENT_CONFIRMED=$(check_agent_confirmed_with_fallback "$CW_RESPONSE" "$CW_AGENT_ROLE")
        if [ "$CW_AGENT_CONFIRMED" = "True" ]; then
            cw_log "Agent exited with code $AGENT_EXIT but already CONFIRMED in BRC. Exiting cleanly."
            exit 0
        fi
    fi

    if is_buffer_overflow; then
        cw_log "Agent crashed on Claude Agent SDK buffer overflow (issue #2804). Deterministic failure; retry budget would be wasted. NOT restarting."
        exit $AGENT_EXIT
    elif is_transient_crash "$AGENT_EXIT"; then
        cw_log "Transient crash (code $AGENT_EXIT). Will restart with backoff."
        CRASH_BACKOFF=$TRANSIENT_BACKOFF_INITIAL
    elif is_startup_failure "$AGENT_EXIT" "$AGENT_DURATION"; then
        cw_log "Startup failure (code $AGENT_EXIT after ${{AGENT_DURATION}}s, likely transient API/network error). Will restart with backoff."
        CRASH_BACKOFF=$TRANSIENT_BACKOFF_INITIAL
    else
        cw_log "Agent failed (code $AGENT_EXIT after ${{AGENT_DURATION}}s). NOT restarting."
        exit $AGENT_EXIT
    fi
fi

# --- Check if consensus is already complete or agent already CONFIRMED ---
# If the agent reached CONFIRMED in the BRC protocol but then exited
# (e.g., context exhaustion), restarting is unnecessary.
MAX_READY_POLLS={max_ready_polls}
# Reuse response from the non-zero handler if available, otherwise fetch fresh.
RESPONSE="${{CW_RESPONSE:-$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")}}"
IS_COMPLETE=$(echo "$RESPONSE" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('is_complete',False))" \
    2>/dev/null || echo "False")
if [ "$IS_COMPLETE" = "True" ]; then
    cw_log "Consensus already reached. Exiting."
    exit 0
fi

# Check if this agent already reached CONFIRMED state (BRC protocol)
AGENT_ROLE="${{EGG_AGENT_ROLE:-}}"

# Shell function: check if agent is confirmed (via tracker or message bus)
# and wait for global consensus. Exits 0 if consensus reached.
# Returns 0 if agent is confirmed (caller should not restart).
# Returns 1 if agent is NOT confirmed (caller should continue to restart loop).
check_confirmed_and_wait() {{
    local response="$1"
    local agent_role="$2"
    local agent_confirmed
    agent_confirmed=$(check_agent_confirmed_with_fallback "$response" "$agent_role")

    if [ "$agent_confirmed" = "True" ]; then
        cw_log "Agent already CONFIRMED in BRC protocol. Waiting for consensus..."
        # Event-driven wait (issue #1897, TASK-5-1): instead of
        # sleep-looping over pipeline status, block on the SSE event
        # stream and parse for the ``consensus.reached`` event-name.
        # Any peer confirmation that completes consensus triggers the
        # event within milliseconds, so consensus completion is
        # noticed immediately rather than on the next 30s poll
        # boundary.
        #
        # Fallback: if curl is unavailable or the SSE endpoint
        # returns 5xx, degrade to the legacy sleep+status loop so
        # local-dev without full SSE infrastructure still works
        # (RISK-7 — keep the zero-Redis path viable).
        local poll_interval wait_count sse_url rc
        poll_interval="${{EGG_MESSAGE_POLL_INTERVAL:-30}}"
        sse_url="${{EGG_ORCHESTRATOR_URL:-http://egg-orchestrator:9849}}/api/v1/pipelines/${{EGG_PIPELINE_ID:-unknown}}/stream"
        wait_count=0

        # Try SSE path if curl is available.
        if command -v curl >/dev/null 2>&1 && [ -n "${{EGG_PIPELINE_ID:-}}" ]; then
            cw_log "Waiting on SSE event 'consensus.reached' at $sse_url"
            # Overall time cap for the SSE subscription. When the curl
            # socket closes (SIGTERM, hangup, server EOF, max-time) we
            # fall through to the final status check.
            local max_seconds sse_exit_code
            max_seconds=$(( MAX_READY_POLLS * poll_interval ))

            # Run curl in the background so we can install a SIGTERM
            # trap (issue #1897 TASK-5-1 acceptance b, reviewer_contract
            # blocker 3). The orchestrator sends SIGTERM to the wrapper
            # PID when it closes the pod; without the trap, curl would
            # keep the stream open while the default bash handler tears
            # down the process, producing a > 2s shutdown. With the trap
            # we kill curl on TERM, clean up the temp file, and exit
            # cleanly within the graceful shutdown window.
            #
            # ``--connect-timeout 5`` ensures we fail fast if the SSE
            # endpoint is unreachable (older sandbox image, DNS error,
            # proxy restriction) rather than blocking for the full
            # max_seconds budget before falling through.
            local curl_pid sse_tmp sse_exit_code
            sse_tmp=$(mktemp -t consensus_sse.XXXXXX)
            curl --no-buffer -sf \
                --connect-timeout 5 \
                -m "$max_seconds" \
                "$sse_url" > "$sse_tmp" 2>/dev/null &
            curl_pid=$!
            trap "
                cw_log 'SIGTERM received; stopping SSE curl (pid $curl_pid) and exiting cleanly.'
                kill '$curl_pid' 2>/dev/null || true
                rm -f '$sse_tmp' 2>/dev/null || true
                exit 0
            " TERM

            # Poll the curl output for the consensus.reached event
            # while curl is alive. Reading a growing temp file is more
            # robust under ``set -uo pipefail`` than ``exec 9< <(curl)``
            # (process substitution) — the fd-based approach was seen
            # to hang rather than surface curl's fast-fail exit.
            sse_exit_code=1
            local tail_deadline
            tail_deadline=$((SECONDS + max_seconds))
            while [ "$SECONDS" -lt "$tail_deadline" ]; do
                if grep -q '^event:.*consensus\.reached' "$sse_tmp" 2>/dev/null; then
                    sse_exit_code=0
                    break
                fi
                if ! kill -0 "$curl_pid" 2>/dev/null; then
                    # curl exited — check one last time for the event.
                    if grep -q '^event:.*consensus\.reached' "$sse_tmp" 2>/dev/null; then
                        sse_exit_code=0
                    fi
                    break
                fi
                sleep 0.5
            done
            # Clean up: drop the trap and kill curl before falling
            # through; we don't want the trap to fire during the rest
            # of the function (which runs its own kill semantics).
            trap - TERM
            kill "$curl_pid" 2>/dev/null || true
            wait "$curl_pid" 2>/dev/null || true
            rm -f "$sse_tmp" 2>/dev/null || true

            if [ "$sse_exit_code" -eq 0 ]; then
                cw_log "SSE delivered consensus.reached. Verifying via status..."
                local resp is_complete
                resp=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
                is_complete=$(echo "$resp" | python3 -c \
                    "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('is_complete',False))" \
                    2>/dev/null || echo "False")
                if [ "$is_complete" = "True" ]; then
                    cw_log "Consensus reached. Exiting."
                    exit 0
                fi
            else
                cw_log "SSE stream ended without consensus.reached; falling back to status loop"
            fi
        else
            cw_log "curl or EGG_PIPELINE_ID unavailable; using status-poll fallback"
        fi

        # Secondary fallback: if SSE didn't deliver but egg-orch is
        # available, block on the typed `egg-orch message wait` primitive
        # before falling through to sleep.  This keeps the wrapper
        # event-driven even when the SSE endpoint is unreachable
        # (older sandbox image, proxy restriction) so we don't burn
        # the full MAX_READY_POLLS budget on empty sleeps.
        while [ "$wait_count" -lt "$MAX_READY_POLLS" ]; do
            wait_count=$((wait_count + 1))
            if command -v egg-orch >/dev/null 2>&1; then
                # Block up to poll_interval seconds on a peer
                # CONSENSUS_CONFIRMED / CONSENSUS_RE_REVIEW event.
                egg-orch message wait \
                    --for CONSENSUS_CONFIRMED \
                    --for CONSENSUS_RE_REVIEW \
                    --timeout "$poll_interval" >/dev/null 2>&1
                rc=$?
                if [ "$rc" -eq 2 ]; then
                    # Transient error — short backoff to avoid tight-loop
                    sleep 2
                elif [ "$rc" -eq 3 ]; then
                    # Permanent egg-orch error — sleep fallback
                    sleep "$poll_interval"
                fi
            else
                # No egg-orch CLI — pure sleep fallback (issue #1897
                # RISK-7: keep zero-CLI local-dev path viable).
                sleep "$poll_interval"
            fi
            resp=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
            is_complete=$(echo "$resp" | python3 -c \
                "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('is_complete',False))" \
                2>/dev/null || echo "False")
            if [ "$is_complete" = "True" ]; then
                cw_log "Consensus reached. Exiting."
                exit 0
            fi
        done
        cw_log "Agent was CONFIRMED but consensus not reached. Exiting cleanly."
        exit 0
    fi

    return 1
}}

if [ -n "$AGENT_ROLE" ]; then
    check_confirmed_and_wait "$RESPONSE" "$AGENT_ROLE" || true
fi

# --- Restart loop for clean exits and transient crashes without BRC consensus ---
while [ "$RESTART_COUNT" -lt "$MAX_RESTARTS" ]; do
    RESTART_COUNT=$((RESTART_COUNT + 1))

    # Apply backoff delay for transient crash restarts
    if [ "$CRASH_BACKOFF" -gt 0 ]; then
        cw_log "Backoff: sleeping ${{CRASH_BACKOFF}}s before restart..."
        sleep "$CRASH_BACKOFF"
        CRASH_BACKOFF=$((CRASH_BACKOFF * 2))
        if [ "$CRASH_BACKOFF" -gt "$TRANSIENT_BACKOFF_MAX" ]; then
            CRASH_BACKOFF=$TRANSIENT_BACKOFF_MAX
        fi
    fi

    cw_log "Agent exited without BRC consensus. Restarting ($RESTART_COUNT/$MAX_RESTARTS)..."

    # Issue #2806: publish an OVERSEER_ALERT on every restart so the operator
    # sees recovery attempts in real time rather than only learning about a
    # dead agent once the wrapper has fully exhausted retries. Best-effort —
    # a failed alert must not block the restart itself.
    if command -v egg-orch >/dev/null 2>&1 && [ -n "${{EGG_PIPELINE_ID:-}}" ]; then
        ALERT_ROLE="${{AGENT_ROLE:-agent}}"
        # ``timeout 5`` bounds wall-clock time so a stalled orchestrator
        # cannot delay the restart itself (issue #2811 review).
        timeout 5 egg-orch overseer alert "${{EGG_PIPELINE_ID}}" \
            --role "$ALERT_ROLE" \
            --anomaly agent-restart \
            --priority medium \
            --summary "Agent ${{ALERT_ROLE}} restart $RESTART_COUNT/$MAX_RESTARTS" \
            --detail "Consensus-wrapper restarted agent after a clean/transient exit without reaching CONFIRMED. After $MAX_RESTARTS restarts the pipeline will be marked FAILED (issue #2806)." \
            >/dev/null 2>&1 || true
    fi

    # Get current BRC state and NACK feedback for the recovery system prompt
    RESPONSE=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
    BRC_STATE="unknown"
    NACK_FEEDBACK=""
    if [ -n "$AGENT_ROLE" ]; then
        BRC_STATE=$(get_brc_state "$RESPONSE" "$AGENT_ROLE")
        # RC1: When BRC state is empty (tracker lost), query consensus status
        # directly for better recovery context.
        if [ "$BRC_STATE" = "{{}}" ]; then
            CONSENSUS_STATUS=$(egg-orch consensus status --json 2>/dev/null || echo "{{}}")
            BRC_STATE="Empty (tracker likely lost). Consensus status: $CONSENSUS_STATUS"
        fi
        NACK_FEEDBACK=$(get_nack_feedback "$RESPONSE" "$AGENT_ROLE")
    fi

    # Load agent anchor if available
    ANCHOR_STATE=""
    if [ -n "${{AGENT_ANCHOR_ID:-}}" ]; then
        ANCHOR_JSON=$(egg-orch anchor show --json 2>/dev/null || echo "")
        if [ -n "$ANCHOR_JSON" ]; then
            ANCHOR_STATE="## Agent Anchor State\n\nYour persisted anchor state from before the context clear:\n\n\`\`\`json\n${{ANCHOR_JSON}}\n\`\`\`\n\nUse this to understand your task progress, decisions made, and BRC state.\n\n"
        fi
    fi

    # Build recovery system prompt with restart context.
    # The system prompt is a trusted channel — the Agent SDK model will not
    # flag it as prompt injection (unlike recovery text in the user prompt).
    RECOVERY_SYS=$(cat <<'RECOVERY_EOF'
{recovery_system_prompt_template}
RECOVERY_EOF
)
    # Use Python regex for single-pass template substitution. This avoids both
    # sed/awk special-character issues and the order-dependency of sequential
    # str.replace() (where an earlier substituted value could contain a later
    # placeholder, causing incorrect replacement).
    RECOVERY_SYS=$(_CW_RESTART="$RESTART_COUNT" _CW_MAX="$MAX_RESTARTS" \
        _CW_BRC="$BRC_STATE" _CW_NACK="$NACK_FEEDBACK" _CW_ANCHOR="$ANCHOR_STATE" \
        python3 -c 'import sys, os, re
t = sys.stdin.read()
m = {{"restart_number": os.environ["_CW_RESTART"], "max_restarts": os.environ["_CW_MAX"],
     "brc_state": os.environ["_CW_BRC"], "nack_feedback": os.environ["_CW_NACK"],
     "anchor_state": os.environ.get("_CW_ANCHOR", "")}}
sys.stdout.write(re.sub(r"\{{(\w+)\}}", lambda x: m.get(x.group(1), x.group(0)), t))' <<< "$RECOVERY_SYS")

    AGENT_START=$SECONDS
    run_agent {recovery_user_prompt} "$RECOVERY_SYS"
    AGENT_EXIT=$?
    AGENT_DURATION=$((SECONDS - AGENT_START))

    if [ "$AGENT_EXIT" -ne 0 ]; then
        # Same consensus/confirmed check as the initial exit handler (issue #1495).
        CW_RESPONSE=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
        CW_IS_COMPLETE=$(echo "$CW_RESPONSE" | python3 -c \
            "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('is_complete',False))" \
            2>/dev/null || echo "False")
        if [ "$CW_IS_COMPLETE" = "True" ]; then
            cw_log "Agent failed on restart $RESTART_COUNT (code $AGENT_EXIT) but consensus already reached. Exiting cleanly."
            exit 0
        fi
        if [ -n "$AGENT_ROLE" ]; then
            CW_AGENT_CONFIRMED=$(check_agent_confirmed_with_fallback "$CW_RESPONSE" "$AGENT_ROLE")
            if [ "$CW_AGENT_CONFIRMED" = "True" ]; then
                cw_log "Agent failed on restart $RESTART_COUNT (code $AGENT_EXIT) but already CONFIRMED. Exiting cleanly."
                exit 0
            fi
        fi
        if is_buffer_overflow; then
            cw_log "Agent crashed on Claude Agent SDK buffer overflow (issue #2804) on restart $RESTART_COUNT. Deterministic failure; further retries would waste budget. Stopping."
            exit $AGENT_EXIT
        fi
        if is_transient_crash "$AGENT_EXIT"; then
            cw_log "Transient crash on restart $RESTART_COUNT (code $AGENT_EXIT). Will retry."
            if [ "$CRASH_BACKOFF" -eq 0 ]; then
                CRASH_BACKOFF=$TRANSIENT_BACKOFF_INITIAL
            fi
            continue
        fi
        if is_startup_failure "$AGENT_EXIT" "$AGENT_DURATION"; then
            cw_log "Startup failure on restart $RESTART_COUNT (code $AGENT_EXIT after ${{AGENT_DURATION}}s). Will retry."
            if [ "$CRASH_BACKOFF" -eq 0 ]; then
                CRASH_BACKOFF=$TRANSIENT_BACKOFF_INITIAL
            fi
            continue
        fi
        cw_log "Agent failed on restart $RESTART_COUNT (code $AGENT_EXIT after ${{AGENT_DURATION}}s). Stopping."
        exit $AGENT_EXIT
    fi

    # Reset backoff on clean exit
    CRASH_BACKOFF=0

    # Check if consensus was reached during the restart
    RESPONSE=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
    IS_COMPLETE=$(echo "$RESPONSE" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('is_complete',False))" \
        2>/dev/null || echo "False")

    if [ "$IS_COMPLETE" = "True" ]; then
        cw_log "Consensus reached after restart $RESTART_COUNT. Exiting."
        exit 0
    fi

    # RC4: After restart, check if this agent reached CONFIRMED state.
    # If so, enter the wait-for-consensus polling loop instead of
    # burning another restart on a pointless re-run.
    if [ -n "$AGENT_ROLE" ]; then
        check_confirmed_and_wait "$RESPONSE" "$AGENT_ROLE" || true
    fi
done

# --- Max restarts exhausted: final consensus check before giving up ---
# The agent may have contributed to consensus even though it never reached
# CONFIRMED locally (e.g. network hiccup after signaling READY).  A final
# poll avoids failing a pipeline that actually succeeded.
FINAL_RESPONSE=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
FINAL_IS_COMPLETE=$(echo "$FINAL_RESPONSE" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('is_complete',False))" \
    2>/dev/null || echo "False")

if [ "$FINAL_IS_COMPLETE" = "True" ]; then
    cw_log "Consensus reached on final check (after max restarts). Exiting successfully."
    exit 0
fi

cw_log "Max restarts ($MAX_RESTARTS) exhausted. Agent never reached CONFIRMED. Exiting with failure."
exit 1
"""


# Default idle budget for the event-pump template (#2908 task-2-3). The
# overseer alert fires when ``LAST_PROGRESS`` ages past this many
# minutes without an actionable BRC event; priority climbs to ``high``
# on the 2x boundary. The runtime override is the ``EGG_BRC_IDLE_BUDGET_MIN``
# env var which the bash reads (composition-time formatting only sets the
# fallback when the env is unset/empty). 30 min is the architect od-4
# default -- well above the WS7-observed 10-13 min legitimate-idle ceiling.
EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT = 30

# Heartbeat cadence for the wrapper-owned background heartbeat emitter
# (#2908 task-2-2). Migrated from
# ``sandbox/egg_agent_tools/handlers/message.py:_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS``
# (60 s); 30 s keeps the wrapper well under the overseer's 120 s
# (default) / 600 s (implement-phase) ``heartbeat_threshold`` even on
# a one-missed-tick basis. Tests can override via
# ``EGG_BRC_HEARTBEAT_INTERVAL_SECS``.
EVENT_PUMP_HEARTBEAT_INTERVAL_SECS_DEFAULT = 30

# Inner wait-loop timeout for the event-pump's blocking call (#2908
# task-2-1). Short relative to the idle budget so the bash loop returns
# regularly and can recompute next-action / age the idle counter. The
# orchestrator long-poll caps at 60 s anyway (see
# ``sandbox/egg_lib/orch_cli.py`` cmd_message_wait_loop), so matching
# that here avoids carrying a longer client timeout than the server
# honors.
EVENT_PUMP_WAIT_TIMEOUT_SECS_DEFAULT = 60


# Event-pump bash template (#2908 task-2-1). Composed by
# ``build_consensus_wrapped_command`` when ``EGG_BRC_EVENT_PUMP=true`` is
# set on the orchestrator pod at composition time. The pump is a
# deterministic loop that calls ``egg-orch brc get-state`` +
# ``egg-orch brc next-action`` to decide what to do next, blocks on
# ``egg-orch message wait-loop`` while emitting wrapper-owned heartbeats
# (#2036 + #2451 migrated out of the agent-side ``message_wait_loop``
# handler), and invokes the agent one-shot via ``python3 -m egg_agent``
# when an event needs handling. The wait-filter set is built
# conditionally with ``CONSENSUS_CONFIRMED`` omitted pre-confirm (the
# orchestrator rejects that combination with HTTP 400 per #2064 / #2482,
# risk_analyst R12).
#
# The idle budget (env ``EGG_BRC_IDLE_BUDGET_MIN``, default 30 min)
# replaces ``MAX_CONSENSUS_RESTARTS``: no actionable event for the
# budget duration raises an ``OVERSEER_ALERT``, but the loop keeps
# blocking instead of exiting 1 -> FAILED.
#
# Placeholders interpolated by ``str.format``:
#   {agent_command_prefix}   -- ``python3 -m egg_agent --model X --max-turns N``
#   {idle_budget_min_default}, {hb_interval_default}, {wait_timeout_default}
_EVENT_PUMP_WRAPPER_TEMPLATE = r"""
#!/bin/bash
set -uo pipefail

# Event-pump wrapper (#2908 slice-2). Deterministic loop driven by
# ``egg-orch brc get-state`` + ``egg-orch brc next-action``; the agent
# is invoked one-shot per actionable event rather than holding a
# blocking wait. Migrated wrapper-owned heartbeats (#2036, #2451)
# replace the agent-side liveness path.

IDLE_BUDGET_MIN="${{EGG_BRC_IDLE_BUDGET_MIN:-{idle_budget_min_default}}}"
IDLE_BUDGET_SECS=$(( IDLE_BUDGET_MIN * 60 ))
HB_INTERVAL_SECS="${{EGG_BRC_HEARTBEAT_INTERVAL_SECS:-{hb_interval_default}}}"
WAIT_TIMEOUT_SECS="${{EGG_BRC_WAIT_TIMEOUT_SECS:-{wait_timeout_default}}}"

# Wrapper-owned background heartbeat PID. ``cleanup`` (installed below)
# kills it on EXIT so SIGTERM from the orchestrator does not leave a
# stray background process holding the gateway session open.
HB_BG_PID=""

cw_log() {{
    echo "[event-pump] $*" >&2
}}

# Emit one heartbeat. The CLI's ``message heartbeat`` handler auto-
# attaches ``slice_id`` from ``$EGG_SLICE_ID`` via
# ``_maybe_attach_slice_id`` in
# ``sandbox/egg_agent_tools/handlers/_gateway.py`` -- this is the
# #2451 migration: every wrapper heartbeat refreshes the slice-scoped
# gateway session as a side effect. We also echo the slice tag in the
# ``--body`` so a snapshot test (#2908 task-2-6 (ii)) can grep for the
# slice_id propagation without intercepting the HTTP POST.
emit_heartbeat() {{
    local state="$1"
    local body_text="$2"
    local slice_tag="${{EGG_SLICE_ID:-none}}"
    timeout 5 egg-orch message heartbeat \
        --state "$state" \
        --body "$body_text (slice=$slice_tag)" \
        >/dev/null 2>&1 || true
}}

# Start a background heartbeat emitter for the duration of a blocking
# call. Replaces ``handlers/message.py:_start_wait_loop_heartbeat`` --
# the wrapper now owns this responsibility (#2908 task-2-2).
start_background_heartbeat() {{
    local body_text="$1"
    (
        # Install a TERM trap that exits the subshell cleanly so the
        # outer ``stop_background_heartbeat``'s ``kill $HB_BG_PID``
        # (default signal SIGTERM) reaps the child rather than
        # deadlocking on ``wait``. The earlier ``trap '' TERM`` form
        # MASKED the signal and caused a wait deadlock that hung the
        # whole event-pump after the first wait-loop return
        # (reviewer_concurrency v1 finding 1 / #2908 slice-2 NACK).
        # ``set -uo pipefail`` (without ``-e``) does not propagate
        # subshell failures to the parent; ``emit_heartbeat``'s
        # ``|| true`` already swallows any CLI error -- so there is no
        # signal-defense to install here.
        trap 'exit 0' TERM
        while true; do
            sleep "$HB_INTERVAL_SECS"
            emit_heartbeat "WAITING_FOR_EVENT" "$body_text"
        done
    ) &
    HB_BG_PID=$!
}}

stop_background_heartbeat() {{
    if [ -n "$HB_BG_PID" ]; then
        # Use SIGTERM (default ``kill`` signal) so the subshell's
        # ``trap 'exit 0' TERM`` exits the heartbeat loop cleanly,
        # then ``wait`` reaps it without blocking. SIGKILL would
        # work too but loses the chance to log a final exit code.
        kill "$HB_BG_PID" 2>/dev/null || true
        wait "$HB_BG_PID" 2>/dev/null || true
        HB_BG_PID=""
    fi
}}

cleanup() {{
    stop_background_heartbeat
}}
trap cleanup EXIT TERM INT

# Fetch the BRC consensus state. Returns ``{{}}`` on any failure so
# downstream parsers can short-circuit without a Python crash.
fetch_state() {{
    egg-orch brc get-state --json 2>/dev/null || echo "{{}}"
}}

# Has the role for this pod already reached CONFIRMED in the BRC
# matrix? Used to decide whether to include CONSENSUS_CONFIRMED in
# the wait-filter set (risk_analyst R12, orchestrator HTTP-400 rule
# from #2064 / #2482).
role_is_confirmed() {{
    local state_json="$1"
    echo "$state_json" | python3 -c "
import sys, json, os
role = os.environ.get('EGG_AGENT_ROLE', '')
try:
    d = json.load(sys.stdin)
except Exception:
    print('False'); sys.exit(0)
agents = (d.get('consensus') or {{}}).get('agents') or {{}}
print('True' if agents.get(role, {{}}).get('confirmed') else 'False')
" 2>/dev/null || echo "False"
}}

# Has global consensus already completed? Lets the loop short-circuit
# (e.g. when another role's confirmation flipped is_complete after we
# CONFIRMED but before SIGTERM landed).
consensus_is_complete() {{
    local state_json="$1"
    echo "$state_json" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('False'); sys.exit(0)
print('True' if (d.get('consensus') or {{}}).get('is_complete') else 'False')
" 2>/dev/null || echo "False"
}}

# Ask the orchestrator route what to do next. Returns a JSON document
# with ``action`` ('wait'|'propose'|'ack'|'nack'|'confirm'|'complete')
# and an optional ``event_payload``.
#
# Per #2908 task-2-1 (acceptance: "Wrapper handles 409 stale_version
# and 409 aggregated-NACK from ``brc next-action`` as event-pump
# signals (re-fetch state, re-invoke), NOT as transient crashes to
# retry with backoff."), HTTP 409 from the route is a signal to
# re-fetch state, not a crash. The CLI surfaces 409 as a non-zero
# exit code with an empty/invalid JSON; falling back to ``{{"action":"wait"}}``
# lets the next loop iteration call ``brc get-state`` again, which
# observes the new state and emits the correct next action.
fetch_next_action() {{
    local out rc
    out=$(egg-orch brc next-action --role "${{EGG_AGENT_ROLE:-unknown}}" --json 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
        echo "$out"
        return 0
    fi
    # Fallback so the main loop keeps blocking on the bus and re-derives
    # state on the next iteration. The CLI returns non-zero for any of:
    # 409 stale_version, 409 aggregated-NACK barrier (both expected
    # event-pump signals -- the next loop's ``brc get-state`` observes
    # the changed state), 5xx, transport failure. Log it so operators
    # reading wrapper logs can distinguish "orchestrator returned 409"
    # (benign, expected) from "transport unreachable" (worth checking)
    # without correlating against the orchestrator's audit log
    # (tester v1 non-blocker #3). We always echo the fallback JSON so
    # the next-action parser doesn't crash; we propagate the original
    # ``rc`` as the function's exit code (caller reads ``$?`` after the
    # ``$(...)`` substitution to count the streak per reviewer §3).
    cw_log "brc next-action returned rc=$rc / empty body; falling back to {{\"action\":\"wait\"}} and re-deriving state next loop."
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

# Build the typed wait-filter set. Pre-confirm waits MUST omit
# CONSENSUS_CONFIRMED -- the orchestrator rejects that filter
# combination with HTTP 400 (#2064, #2482, risk_analyst R12). The
# six-event set is the union the architect's
# ``verification_strategy.slice_2.i`` snapshot test pins.
build_wait_args() {{
    local include_confirmed="$1"
    local args=(
        --for CONSENSUS_PROPOSE
        --for CONSENSUS_ACK
        --for CONSENSUS_NACK
        --for STATUS
        --for CONSENSUS_RE_REVIEW
        --for OVERSEER_ALERT
    )
    if [ "$include_confirmed" = "True" ]; then
        args+=( --for CONSENSUS_CONFIRMED )
    fi
    args+=( --timeout "$WAIT_TIMEOUT_SECS" )
    printf '%s\n' "${{args[@]}}"
}}

# Block on the orchestrator message bus for the next BRC event while
# the wrapper-owned background heartbeat emitter keeps the overseer's
# liveness tracker and the gateway-session idle timer happy.
wait_for_event() {{
    local include_confirmed="$1"
    local hb_body="event-pump wait role=${{EGG_AGENT_ROLE:-?}}"
    mapfile -t WAIT_ARGS < <(build_wait_args "$include_confirmed")
    start_background_heartbeat "$hb_body"
    emit_heartbeat "WAITING_FOR_EVENT" "$hb_body"
    egg-orch message wait-loop "${{WAIT_ARGS[@]}}" --max-iterations 1 >/dev/null 2>&1
    local rc=$?
    stop_background_heartbeat
    emit_heartbeat "WORKING" "event-pump woke (rc=$rc)"
    return $rc
}}

# Invoke the agent one-shot with the per-event prompt. Slice-3
# (TASK-3-1 / TASK-3-2) replaces the slice-2 stub with the full
# ``compose_event_prompt`` payload: memory excerpt (when
# ``EGG_BRC_MEMORY=full``) + per-producer
# ``git log {{sha}}..HEAD --not origin/{{base}} -p`` delta + NACK
# payload. The composer lives at
# ``/opt/egg-runtime/orchestrator/routes/event_prompt.py`` -- the
# wrapper invokes its ``if __name__ == '__main__'`` CLI directly so the
# heavy ``orchestrator.routes`` package ``__init__.py`` (Flask import)
# is bypassed. ``EGG_EVENT_PROMPT_SCRIPT`` overrides the path for tests.
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
    local script_path="${{EGG_EVENT_PROMPT_SCRIPT:-/opt/egg-runtime/orchestrator/routes/event_prompt.py}}"
    local prompt prompt_rc=1

    if [ -r "$script_path" ]; then
        # Pass the event_payload JSON via stdin so shell metacharacters
        # ($VAR, backticks, ;, &&) don't fall through to argv (the
        # #2741 / slice-5 motivating concern; even though this argv is
        # composed entirely by the wrapper here, the stdin path keeps
        # the surface honest and matches the slice-5 prose-arg rule).
        # ``EGG_AGENT_ROLE`` / ``EGG_BASE_BRANCH`` / ``EGG_REPO_PATH`` /
        # ``EGG_BRC_MEMORY`` are read by the script from env directly --
        # the env-var prefix MUST attach to ``python3`` (RHS of the
        # pipe), not ``printf`` (LHS); the earlier form attached only
        # to ``printf`` and ``python3`` inherited from the parent
        # shell, which works in production today but is misleading
        # and breaks if a parent shell hasn't exported them. Capture
        # stderr to a temp file so the cw_log fallback message can
        # surface the first line of the failure (script-not-found vs
        # schema-drift vs subprocess crash are otherwise
        # indistinguishable in the log).
        local err_tmp
        err_tmp=$(mktemp -t event-prompt-stderr.XXXXXX 2>/dev/null || echo "/tmp/event-prompt-stderr-$$.log")
        prompt=$(printf '%s' "$event_payload" \
            | EGG_AGENT_ROLE="$role" \
                EGG_BASE_BRANCH="$base_branch" \
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
    {agent_command_prefix} "$prompt"
}}

# Idle / no-progress safety budget (#2908 task-2-3). Replaces the
# ``MAX_CONSENSUS_RESTARTS`` cap from the legacy template: if no
# actionable event arrives for the configured idle budget we raise an
# OVERSEER_ALERT but the loop keeps blocking (the legacy template
# would exit 1 -> FAILED at this point).
LAST_PROGRESS=$SECONDS
ALERTED_AT_BUDGET=false
ALERTED_AT_DOUBLE=false

# Consecutive-failure counters for the action arms (reviewer §1).
# Used to apply linear backoff on the ``confirm`` arm and to surface
# a distinguishable log when an action is persistently failing.
# The counters are arm-cluster scoped: they reset to 0 at the top of the
# loop when the next-action transitions away from the arm they apply to
# (reviewer §2 follow-up). That way a long-ago confirm-failure streak
# doesn't pre-load the backoff for a fresh confirm attempt hours later
# after the role briefly transitioned through ``wait`` / ``propose``.
CONFIRM_FAIL_STREAK=0
AGENT_FAIL_STREAK=0
# Consecutive failures from ``fetch_next_action`` (reviewer §3): used
# to surface a distinguishable "many consecutive 5xx/transport failures"
# log line so an unhealthy orchestrator is differentiable from a benign
# 409 stale_version that re-derives on the next loop. Latches are
# sticky for the wrapper lifetime so the log doesn't re-fire if the
# counter happens to land back on the threshold after a brief recovery.
NEXT_ACTION_FAIL_STREAK=0
NEXT_ACTION_ALERTED_5=false
NEXT_ACTION_ALERTED_20=false

raise_idle_alert() {{
    local idle="$1"
    local priority="$2"
    local summary_extra="$3"
    # Snapshot the current BRC state for the alert detail so operators
    # see the consensus.agents.<role> matrix without having to query
    # pipeline status separately. Plan TASK-2-3 acceptance line:
    # "alert payload includes anomaly type, priority, current BRC
    # state" (tester v1 non-blocker #2).
    local brc_snapshot snapshot_input
    # The naive ``echo $VAR_OR_EMPTY_JSON | python3 ...`` form using a
    # parameter-expansion default containing literal braces is unsafe:
    # bash parses the brace inside the default greedily and leaves a
    # trailing literal close-brace appended to the expanded value,
    # corrupting JSON when STATE_JSON is set (tester v2 NACK finding).
    # Use a separate variable and an explicit empty-string check so
    # bash never sees unbalanced braces inside a parameter expansion.
    snapshot_input="${{STATE_JSON-}}"
    if [ -z "$snapshot_input" ]; then
        snapshot_input='{{}}'
    fi
    brc_snapshot=$(printf '%s' "$snapshot_input" | python3 -c "
import sys, json, os
role = os.environ.get('EGG_AGENT_ROLE', '')
try:
    d = json.load(sys.stdin)
except Exception:
    print('(unavailable)'); sys.exit(0)
agents = (d.get('consensus') or {{}}).get('agents') or {{}}
my = agents.get(role) or {{}}
blocking = (d.get('consensus') or {{}}).get('blocking_agents') or []
print(f\"role={{role}} producer_phase={{my.get('producer_phase','?')}} reviewer_phase={{my.get('reviewer_phase','?')}} confirmed={{my.get('confirmed','?')}} blocking_agents={{blocking}}\")
" 2>/dev/null || echo "(snapshot unavailable)")
    timeout 5 egg-orch overseer alert "${{EGG_PIPELINE_ID:-unknown}}" \
        --role "${{EGG_AGENT_ROLE:-agent}}" \
        --anomaly stuck-phase-transition \
        --priority "$priority" \
        --summary "BRC event-pump idle for ${{idle}}s$summary_extra" \
        --detail "Event-pump for role=${{EGG_AGENT_ROLE:-agent}} slice=${{EGG_SLICE_ID:-none}} has seen no actionable BRC event for ${{idle}}s (configured budget ${{IDLE_BUDGET_SECS}}s). The loop continues blocking; no FAILED transition is forced. BRC state: $brc_snapshot" \
        >/dev/null 2>&1 || true
}}

check_idle_budget() {{
    local idle=$(( SECONDS - LAST_PROGRESS ))
    local double=$(( 2 * IDLE_BUDGET_SECS ))
    if [ "$idle" -ge "$double" ] && [ "$ALERTED_AT_DOUBLE" != "true" ]; then
        cw_log "Idle 2x budget exceeded (${{idle}}s >= ${{double}}s); raising HIGH overseer alert."
        raise_idle_alert "$idle" "high" " (2x budget)"
        ALERTED_AT_DOUBLE=true
        # When the loop jumps straight from idle=0 to >=2x budget (e.g.
        # the wrapper paused for 60+ min between checks), set the 1x
        # latch too -- the 2x alert subsumes the 1x notification, so
        # the next ``check_idle_budget`` should not re-fire the 1x
        # branch (tester v1 non-blocker #1).
        ALERTED_AT_BUDGET=true
    elif [ "$idle" -ge "$IDLE_BUDGET_SECS" ] && [ "$ALERTED_AT_BUDGET" != "true" ]; then
        cw_log "Idle budget exceeded (${{idle}}s >= ${{IDLE_BUDGET_SECS}}s); raising overseer alert."
        raise_idle_alert "$idle" "high" ""
        ALERTED_AT_BUDGET=true
    fi
}}

note_progress() {{
    LAST_PROGRESS=$SECONDS
    ALERTED_AT_BUDGET=false
    # Reviewer §6 nit: ``ALERTED_AT_DOUBLE`` is sticky for the lifetime
    # of the loop. Once the operator has been paged at 2x budget,
    # re-arming a fresh 1x alert later (after a single spurious
    # ``note_progress``) is noise, not signal.
}}

# --- main event-pump loop ---
cw_log "Event-pump starting (role=${{EGG_AGENT_ROLE:-?}}, slice=${{EGG_SLICE_ID:-none}}, idle-budget=${{IDLE_BUDGET_MIN}}m)"
emit_heartbeat "WORKING" "event-pump start"

while true; do
    STATE_JSON=$(fetch_state)

    if [ "$(consensus_is_complete "$STATE_JSON")" = "True" ]; then
        cw_log "Global consensus complete; exiting cleanly."
        exit 0
    fi

    ROLE_CONFIRMED=$(role_is_confirmed "$STATE_JSON")

    ACTION_JSON=$(fetch_next_action)
    NEXT_ACTION_RC=$?
    if [ "$NEXT_ACTION_RC" -eq 0 ]; then
        NEXT_ACTION_FAIL_STREAK=0
    else
        # Reviewer §3 (minor): the CLI surfaces 409 stale_version, 409
        # aggregated-NACK barrier, 5xx, and transport failure all as the
        # same non-zero rc + empty body. A single non-zero rc is benign
        # (expected event-pump signal). A *streak* is an orchestrator-
        # health signal worth surfacing separately so operators reading
        # wrapper logs can tell a 409-stuck role from an unhealthy
        # orchestrator without correlating against the audit log.
        #
        # Use ``-ge`` with sticky latches (rather than ``-eq``) so the log
        # is robust to any future change in how the counter advances and
        # the warning fires the first time the threshold is crossed
        # without re-firing on every iteration past it.
        NEXT_ACTION_FAIL_STREAK=$(( NEXT_ACTION_FAIL_STREAK + 1 ))
        if [ "$NEXT_ACTION_FAIL_STREAK" -ge 5 ] && [ "$NEXT_ACTION_ALERTED_5" != "true" ]; then
            cw_log "brc next-action has returned non-zero ${{NEXT_ACTION_FAIL_STREAK}} times in a row -- orchestrator may be unhealthy (5xx loop / transport down), not just a benign 409 stale_version. Idle budget continues to accrue."
            NEXT_ACTION_ALERTED_5=true
        fi
        if [ "$NEXT_ACTION_FAIL_STREAK" -ge 20 ] && [ "$NEXT_ACTION_ALERTED_20" != "true" ]; then
            cw_log "brc next-action has returned non-zero ${{NEXT_ACTION_FAIL_STREAK}} times in a row -- orchestrator may be unhealthy (5xx loop / transport down), not just a benign 409 stale_version. Idle budget continues to accrue."
            NEXT_ACTION_ALERTED_20=true
        fi
    fi
    ACTION=$(next_action_field "$ACTION_JSON" "action")
    EVENT_PAYLOAD=$(next_action_field "$ACTION_JSON" "event_payload")

    # Reviewer §2 (non-blocking): the per-arm failure counters are
    # arm-cluster scoped, not wrapper-lifetime. When the orchestrator
    # transitions the role to a different action verb (e.g., a stuck
    # ``confirm`` recovers via a fresh ``propose`` after a re-review
    # event), reset the streak for the arm we just left so a brand-new
    # attempt isn't pre-loaded with an old streak's backoff cap.
    if [ "$ACTION" != "confirm" ]; then
        CONFIRM_FAIL_STREAK=0
    fi
    case "$ACTION" in
        propose|ack|nack) ;;
        *) AGENT_FAIL_STREAK=0 ;;
    esac

    case "$ACTION" in
        complete)
            cw_log "Role complete; finalising via egg-orch consensus confirmed."
            timeout 30 egg-orch consensus confirmed >/dev/null 2>&1 || true
            cw_log "Exiting (role complete)."
            exit 0
            ;;
        confirm)
            # Reviewer §1 (slice-4-blocking): ``note_progress`` must only
            # fire when the CLI actually succeeded. Otherwise a persistent
            # 5xx / transport / ``producer_not_fully_acked`` race against
            # ``egg-orch consensus confirmed`` becomes a tight retry loop
            # (~tens of ms per iteration, two short HTTP calls) that
            # silently drains budget because the idle latch keeps resetting.
            # The legacy template guarded this with ``MAX_CONSENSUS_RESTARTS=3``;
            # the event-pump path's equivalent is the idle-budget safety net
            # gated on rc.
            cw_log "Confirming via egg-orch consensus confirmed."
            timeout 30 egg-orch consensus confirmed >/dev/null 2>&1
            confirm_rc=$?
            if [ "$confirm_rc" -eq 0 ]; then
                note_progress
                CONFIRM_FAIL_STREAK=0
            else
                # Floor the retry cadence so a persistent failure can't
                # hot-loop the orchestrator faster than the idle counter
                # can age. The sleep grows linearly with the streak length
                # (capped at 30 s) so the operator sees the idle alert
                # within ~30 min on the default budget while consecutive
                # short failures don't escalate the load on the route.
                CONFIRM_FAIL_STREAK=$(( CONFIRM_FAIL_STREAK + 1 ))
                # NOTE: bash ``case``-branch scope is global, not function-
                # local. Name reflects scope to avoid the false suggestion
                # that this could be ``declare local`` (reviewer §4).
                confirm_backoff_secs=$(( CONFIRM_FAIL_STREAK * 2 ))
                if [ "$confirm_backoff_secs" -gt 30 ]; then
                    confirm_backoff_secs=30
                fi
                cw_log "consensus confirmed failed (rc=$confirm_rc, streak=$CONFIRM_FAIL_STREAK); backing off ${{confirm_backoff_secs}}s. Idle counter continues to accrue."
                sleep "$confirm_backoff_secs"
            fi
            ;;
        wait)
            # Block on the bus. ``wait_for_event`` returns 0 only when
            # a matching message was delivered (sandbox/egg_lib CLI
            # contract: ``message wait-loop`` exits 0 on match, 1 on
            # safety-cap / no-match). Reset the idle counter ONLY in
            # the match case -- a timeout return is the DEFINITION of
            # idle, not progress. (reviewer_concurrency v1 finding 2 /
            # #2908 slice-2 NACK: unconditional ``note_progress`` here
            # would defeat the entire idle-budget safety net because
            # the inner wait-loop returns every ~60 s with no event.)
            wait_for_event "$ROLE_CONFIRMED"
            wait_rc=$?
            if [ "$wait_rc" -eq 0 ]; then
                note_progress
            fi
            ;;
        propose|ack|nack)
            # Reviewer §1 (slice-4-blocking): symmetric with the ``confirm``
            # arm above -- ``note_progress`` must only fire when the agent
            # invocation actually succeeded. A persistent ``mcp__brc__propose``
            # / API-quota / prompt-rendering failure can fail in well under a
            # second, and without rc-gating here the idle latch resets every
            # iteration so the operator-visible idle alert never fires. The
            # PR removed ``MAX_CONSENSUS_RESTARTS=3``; this rc gate is the
            # equivalent ceiling on the action path.
            cw_log "Invoking agent (action=$ACTION)."
            invoke_agent_for_event "$ACTION" "$EVENT_PAYLOAD"
            agent_rc=$?
            if [ "$agent_rc" -eq 0 ]; then
                note_progress
                AGENT_FAIL_STREAK=0
            else
                AGENT_FAIL_STREAK=$(( AGENT_FAIL_STREAK + 1 ))
                cw_log "agent invocation failed (action=$ACTION, rc=$agent_rc, streak=$AGENT_FAIL_STREAK). Idle counter continues to accrue."
                # Agent startup typically gives a natural floor of
                # seconds-to-tens-of-seconds, so no explicit backoff is
                # required here -- but if the failure was sub-second (e.g.
                # a prompt-rendering crash before SDK init), add a small
                # floor so the orchestrator's next-action route isn't
                # hammered.
                sleep 1
            fi
            ;;
        *)
            # Defensive: unknown action surfaced from the orchestrator
            # (older orchestrator, schema drift, transient error). Short
            # sleep + recheck rather than a tight loop. The idle counter
            # still ticks against the configured budget.
            cw_log "Unknown next-action='$ACTION'; sleeping briefly and re-fetching."
            sleep 5
            ;;
    esac

    check_idle_budget
done
"""


def _event_pump_enabled() -> bool:
    """Should ``build_consensus_wrapped_command`` emit the event-pump branch?

    Read at template-composition time on the orchestrator pod (#2908
    task-2-1). Slice-4 task-4-1 flips the default to ON (the event-pump
    template is the production path post-slice-4); slice-4 task-4-2
    deletes the legacy template entirely. Until task-4-2 lands an
    operator can opt back into the legacy template for a one-release
    rollback window by setting ``EGG_BRC_EVENT_PUMP=false`` (or
    ``0`` / ``no`` / ``off``).

    Default (unset env): True.
    Falsy values: ``false``, ``0``, ``no``, ``off`` (case-insensitive).
    Anything else (including unrecognised tokens) returns True so a
    typo cannot silently downgrade the production path back to the
    legacy template.
    """
    raw = os.environ.get("EGG_BRC_EVENT_PUMP", "")
    return raw.strip().lower() not in {"false", "0", "no", "off"}


def build_event_pump_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 1000,
    idle_budget_min: int = EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT,
    heartbeat_interval_secs: int = EVENT_PUMP_HEARTBEAT_INTERVAL_SECS_DEFAULT,
    wait_timeout_secs: int = EVENT_PUMP_WAIT_TIMEOUT_SECS_DEFAULT,
) -> list[str]:
    """Compose the event-pump wrapper bash command (#2908 task-2-1).

    Public entry-point so tests can build the event-pump template
    deterministically without setting ``EGG_BRC_EVENT_PUMP`` in the
    test environment. ``build_consensus_wrapped_command`` delegates
    here when the env flag is true.

    The ``prompt_text`` argument is the *initial* prompt used today
    by the legacy template; the event-pump emits its own per-event
    prompts inside ``invoke_agent_for_event``, so the initial prompt
    is not interpolated into the bash directly. We accept it for
    interface parity with ``build_consensus_wrapped_command`` and so
    a future revision can choose to pass it through (e.g. as a
    bootstrap prompt for the first ``propose`` event in slice-3
    when ``compose_event_prompt`` is wired up).
    """
    del prompt_text  # reserved for slice-3 / interface parity (see docstring)

    agent_prefix_parts = [
        "python3",
        "-m",
        "egg_agent",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
    ]
    agent_command_prefix = " ".join(shlex.quote(p) for p in agent_prefix_parts)

    script = _EVENT_PUMP_WRAPPER_TEMPLATE.format(
        agent_command_prefix=agent_command_prefix,
        idle_budget_min_default=idle_budget_min,
        hb_interval_default=heartbeat_interval_secs,
        wait_timeout_default=wait_timeout_secs,
    )
    return ["bash", "-c", script]


def build_consensus_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 1000,
    max_restarts: int = MAX_CONSENSUS_RESTARTS,
    max_ready_polls: int = MAX_READY_POLL_CYCLES,
    transient_backoff_initial: int = TRANSIENT_RESTART_BACKOFF_INITIAL,
    startup_failure_window_seconds: int = STARTUP_FAILURE_WINDOW_SECONDS,
) -> list[str]:
    """Build a shell command that runs the agent with a BRC consensus restart wrapper.

    The wrapper detects when the agent exits without reaching CONFIRMED state
    in the BRC protocol and restarts it with a recovery prompt. This ensures
    agents explicitly participate in the Broadcast-Review-Converge consensus
    rather than having it faked.

    Args:
        prompt_text: The prompt to pass to the agent.
        model: Agent model to use.
        max_turns: Maximum number of tool-call turns.
        max_restarts: Maximum restart attempts before exiting with failure.
        max_ready_polls: Maximum poll cycles to wait when agent already
            signaled READY (avoids unnecessary restarts).
        transient_backoff_initial: Initial backoff in seconds for transient
            crash restarts. Doubles after each crash, capped at 30s.
        startup_failure_window_seconds: Agents that exit with code 1 within
            this many seconds are treated as transient API/network failures
            and restarted. Set to 0 to disable the heuristic.

    Returns:
        Command list suitable for container spawning (bash -c "...").
    """
    # #2908 task-2-1: when ``EGG_BRC_EVENT_PUMP`` is truthy (slice-4
    # task-4-1 flipped the unset-env default from OFF to ON) on the
    # orchestrator pod, emit the event-pump bash template instead.
    # Setting ``EGG_BRC_EVENT_PUMP=false`` keeps the legacy template
    # available for a one-release rollback window. Slice-4 task-4-2
    # deletes the legacy template (and the env flag along with it).
    if _event_pump_enabled():
        return build_event_pump_wrapped_command(
            prompt_text,
            model=model,
            max_turns=max_turns,
        )

    # Build the agent command prefix (everything except the prompt argument).
    # Uses the Agent SDK entry point instead of the claude CLI.
    agent_prefix_parts = [
        "python3",
        "-m",
        "egg_agent",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
    ]
    agent_command_prefix = " ".join(shlex.quote(p) for p in agent_prefix_parts)
    initial_prompt = shlex.quote(prompt_text)

    recovery_user_prompt = shlex.quote(_RECOVERY_USER_PROMPT)

    script = _CONSENSUS_WRAPPER_TEMPLATE.format(
        agent_command_prefix=agent_command_prefix,
        initial_prompt=initial_prompt,
        max_restarts=max_restarts,
        max_ready_polls=max_ready_polls,
        recovery_system_prompt_template=_RECOVERY_SYSTEM_PROMPT,
        recovery_user_prompt=recovery_user_prompt,
        transient_backoff_initial=transient_backoff_initial,
        startup_failure_window_seconds=startup_failure_window_seconds,
    )

    return ["bash", "-c", script]
