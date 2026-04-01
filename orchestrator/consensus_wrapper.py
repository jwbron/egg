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
are capped at ``MAX_CONSENSUS_RESTARTS`` (default 2). After exhausting
restarts the wrapper exits with code 1 so the orchestrator's failure path
handles escalation.
"""

import shlex

# Default maximum number of times the wrapper will restart the agent after a
# clean exit without consensus being reached.
MAX_CONSENSUS_RESTARTS = 2

# Default maximum number of poll cycles to wait for consensus when the agent
# already signaled READY. With a default poll interval of 30s, this gives
# 10 * 30 = 300 seconds (5 minutes) for other agents to finish.
MAX_READY_POLL_CYCLES = 10

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
#   exit cleanly (issue #1495). Otherwise treat as failure, no restart.
# - Clean exit (code 0): restart the agent with a recovery prompt (up to
#   MAX_RESTARTS times). After max restarts, exit 1 to trigger the
#   orchestrator's agent failure path (HITL decision).
_CONSENSUS_WRAPPER_TEMPLATE = r"""
#!/bin/bash
set -uo pipefail

MAX_RESTARTS={max_restarts}
RESTART_COUNT=0

# Log wrapper messages to stderr so they never leak into agent SDK context.
cw_log() {{
    echo "[consensus-wrapper] $*" >&2
}}

run_agent() {{
    local prompt="$1"
    local system_prompt="${{2:-}}"
    if [ -n "$system_prompt" ]; then
        {agent_command_prefix} --system-prompt "$system_prompt" "$prompt"
    else
        {agent_command_prefix} "$prompt"
    fi
    return $?
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

# --- Initial run ---
run_agent {initial_prompt}
AGENT_EXIT=$?

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
        CW_AGENT_CONFIRMED=$(get_agent_confirmed "$CW_RESPONSE" "$CW_AGENT_ROLE")
        # Message bus fallback (tracker may be lost after orchestrator restart)
        if [ "$CW_AGENT_CONFIRMED" != "True" ]; then
            CW_AGENTS_EMPTY=$(echo "$CW_RESPONSE" | python3 -c \
                "import sys,json; d=json.load(sys.stdin); agents=d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('agents',{{}}); print('True' if not agents else 'False')" \
                2>/dev/null || echo "False")
            if [ "$CW_AGENTS_EMPTY" = "True" ]; then
                CW_MSG_RESPONSE=$(egg-orch message poll --json --limit 1000 2>/dev/null || echo "[]")
                CW_AGENT_CONFIRMED=$(echo "$CW_MSG_RESPONSE" | python3 -c "
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
" "$CW_AGENT_ROLE" 2>/dev/null || echo "False")
            fi
        fi
        if [ "$CW_AGENT_CONFIRMED" = "True" ]; then
            cw_log "Agent exited with code $AGENT_EXIT but already CONFIRMED in BRC. Exiting cleanly."
            exit 0
        fi
    fi

    cw_log "Agent failed (code $AGENT_EXIT). NOT restarting."
    exit $AGENT_EXIT
fi

# --- Check if consensus is already complete or agent already CONFIRMED ---
# If the agent reached CONFIRMED in the BRC protocol but then exited
# (e.g., context exhaustion), restarting is unnecessary.
MAX_READY_POLLS={max_ready_polls}
RESPONSE=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
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
    agent_confirmed=$(get_agent_confirmed "$response" "$agent_role")

    # Message bus fallback: if pipeline status returned empty consensus state
    # (e.g. after orchestrator restart lost in-memory tracker), check the
    # message store directly for our own CONSENSUS_CONFIRMED message.
    if [ "$agent_confirmed" != "True" ]; then
        local agents_empty
        agents_empty=$(echo "$response" | python3 -c \
            "import sys,json; d=json.load(sys.stdin); agents=d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('agents',{{}}); print('True' if not agents else 'False')" \
            2>/dev/null || echo "False")
        if [ "$agents_empty" = "True" ]; then
            cw_log "Consensus state empty (tracker lost?). Checking message bus..."
            local msg_response confirmed_via_msg
            msg_response=$(egg-orch message poll --json --limit 1000 2>/dev/null || echo "[]")
            confirmed_via_msg=$(echo "$msg_response" | python3 -c "
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
" "$agent_role" 2>/dev/null || echo "False")
            if [ "$confirmed_via_msg" = "True" ]; then
                cw_log "Found own CONSENSUS_CONFIRMED in message bus. Already confirmed."
                agent_confirmed="True"
            fi
        fi
    fi

    if [ "$agent_confirmed" = "True" ]; then
        cw_log "Agent already CONFIRMED in BRC protocol. Waiting for consensus..."
        local poll_interval wait_count
        poll_interval="${{EGG_MESSAGE_POLL_INTERVAL:-30}}"
        wait_count=0
        while [ "$wait_count" -lt "$MAX_READY_POLLS" ]; do
            wait_count=$((wait_count + 1))
            sleep "$poll_interval"
            local resp is_complete
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

# --- Restart loop for clean exits without BRC consensus ---
while [ "$RESTART_COUNT" -lt "$MAX_RESTARTS" ]; do
    RESTART_COUNT=$((RESTART_COUNT + 1))
    cw_log "Agent exited without BRC consensus. Restarting ($RESTART_COUNT/$MAX_RESTARTS)..."

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

    run_agent {recovery_user_prompt} "$RECOVERY_SYS"
    AGENT_EXIT=$?

    if [ "$AGENT_EXIT" -ne 0 ]; then
        cw_log "Agent failed on restart $RESTART_COUNT (code $AGENT_EXIT). Stopping."
        exit $AGENT_EXIT
    fi

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

# --- Max restarts exhausted: shut down with failure ---
cw_log "Max restarts ($MAX_RESTARTS) exhausted. Agent never reached CONFIRMED. Exiting with failure."
exit 1
"""


def build_consensus_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 1000,
    max_restarts: int = MAX_CONSENSUS_RESTARTS,
    max_ready_polls: int = MAX_READY_POLL_CYCLES,
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
    )

    return ["bash", "-c", script]
