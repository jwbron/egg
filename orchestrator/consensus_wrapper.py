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
"""

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
# message-reader 1MB JSON buffer crash (issue #2804) which is
# deterministic — retrying just hits the same overflow and burns
# the restart budget for no gain.
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

# Detect the Claude Agent SDK 1 MB JSON message-reader overflow
# signature in the most recent agent run. Issue #2804. The overflow
# is deterministic: re-running the agent against the same codebase
# hits the same oversized tool result, so the wrapper must NOT
# consume retry budget on this failure class. Returns 0 (true) if
# the marker was logged, 1 otherwise.
#
# The substring matches CLI output from claude_agent_sdk emitted on
# the buffer overflow path. If a future SDK bump changes the
# wording, this grep silently falls through and the wrapper burns
# its retry budget again — the buffer-overflow tests in
# orchestrator/tests/test_consensus_wrapper.py (notably
# test_script_marker_matches_client_constant and the
# test_buffer_overflow_*_aborts_without_retry pair) exercise the
# wrapper against a synthetic log to keep this honest, but do not
# pin against the installed SDK. The real fix is tool-layer
# truncation (#2805); this is the fail-fast path until that lands.
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
