"""Build consensus-wrapped commands for concurrent agent containers.

When agents run in concurrent mode, they must stay alive after completing
their work to participate in BRC (Broadcast-Review-Converge) consensus.
This module provides a shell wrapper that detects early agent exits and
restarts the agent with a recovery prompt instead of blindly marking
consensus as approved.

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
    "## Required actions\n\n"
    "1. Check consensus status: `egg-orch consensus status`\n"
    "2. Poll for messages: `egg-orch message poll --wait 30`\n"
    "3. Based on your role type:\n"
    "   - **Producer**: If you received NACKs, address the reviewer feedback, "
    "revise your work, and re-propose (`egg-orch consensus propose`). "
    "If WORKING, complete work and propose. "
    "If PROPOSED, check for ACKs/NACKs and respond. If all ACKed, confirm.\n"
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
# - Non-zero exit: treat as failure, no restart.
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

# Non-zero exit means the agent crashed — do not restart.
if [ "$AGENT_EXIT" -ne 0 ]; then
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
if [ -n "$AGENT_ROLE" ]; then
    AGENT_CONFIRMED=$(get_agent_confirmed "$RESPONSE" "$AGENT_ROLE")
    if [ "$AGENT_CONFIRMED" = "True" ]; then
        cw_log "Agent already CONFIRMED in BRC protocol. Waiting for consensus..."
        POLL_INTERVAL="${{EGG_MESSAGE_POLL_INTERVAL:-30}}"
        WAIT_COUNT=0
        while [ "$WAIT_COUNT" -lt "$MAX_READY_POLLS" ]; do
            WAIT_COUNT=$((WAIT_COUNT + 1))
            sleep "$POLL_INTERVAL"
            RESPONSE=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
            IS_COMPLETE=$(echo "$RESPONSE" | python3 -c \
                "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('is_complete',False))" \
                2>/dev/null || echo "False")
            if [ "$IS_COMPLETE" = "True" ]; then
                cw_log "Consensus reached. Exiting."
                exit 0
            fi
        done
        cw_log "Agent was CONFIRMED but consensus not reached. Exiting cleanly."
        exit 0
    fi
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
        NACK_FEEDBACK=$(get_nack_feedback "$RESPONSE" "$AGENT_ROLE")
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
        _CW_BRC="$BRC_STATE" _CW_NACK="$NACK_FEEDBACK" \
        python3 -c 'import sys, os, re
t = sys.stdin.read()
m = {{"restart_number": os.environ["_CW_RESTART"], "max_restarts": os.environ["_CW_MAX"],
     "brc_state": os.environ["_CW_BRC"], "nack_feedback": os.environ["_CW_NACK"]}}
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
done

# --- Max restarts exhausted: shut down with failure ---
cw_log "Max restarts ($MAX_RESTARTS) exhausted. Agent never reached CONFIRMED. Exiting with failure."
exit 1
"""


def build_consensus_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 200,
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
