"""Build consensus-wrapped commands for concurrent agent containers.

When agents run in concurrent mode, they must stay alive after completing
their work to participate in BRC (Broadcast-Review-Converge) consensus.
This module provides a shell wrapper that detects early Claude exits and
restarts the agent with a recovery prompt instead of blindly marking
consensus as approved.

If the agent exits without reaching CONFIRMED state in the BRC protocol,
the wrapper restarts Claude with a prompt that explains what happened and
instructs it to assess state, then continue the BRC protocol. Restarts
are capped at ``MAX_CONSENSUS_RESTARTS`` (default 2). After exhausting
restarts the wrapper exits with code 1 so the orchestrator's failure path
handles escalation.
"""

import shlex

# Default maximum number of times the wrapper will restart Claude after a
# clean exit without consensus being reached.
MAX_CONSENSUS_RESTARTS = 2

# Default maximum number of poll cycles to wait for consensus when the agent
# already signaled READY. With a default poll interval of 30s, this gives
# 10 * 30 = 300 seconds (5 minutes) for other agents to finish.
MAX_READY_POLL_CYCLES = 10

# Recovery prompt given to Claude when it is restarted by the wrapper.
# Placeholders: {restart_number}, {max_restarts}, {brc_state}, {nack_feedback}
_RECOVERY_PROMPT = (
    "## BRC CONSENSUS RECOVERY — You were restarted by the consensus wrapper\n\n"
    "You exited your previous session without completing the BRC consensus protocol. "
    "This is restart {restart_number} of {max_restarts}.\n\n"
    "**What happened**: Your agent process exited cleanly, but the Broadcast-Review-"
    "Converge (BRC) protocol requires all agents to reach CONFIRMED state before "
    "the orchestrator stops your container.\n\n"
    "**Your BRC state**: {brc_state}\n\n"
    "{nack_feedback}"
    "**What you must do now**:\n"
    "1. Check consensus status: `egg-orch consensus status`\n"
    "2. Poll for messages: `egg-orch message poll --wait 30`\n"
    "3. Based on your role type:\n"
    "   - **Producer**: If you received NACKs, you MUST address the reviewer feedback "
    "above, revise your work, and re-propose (`egg-orch consensus propose`). "
    "If WORKING, complete work and propose. "
    "If PROPOSED, check for ACKs/NACKs and respond. If all ACKed, confirm.\n"
    "   - **Reviewer**: Check for proposals from assigned producers. Review artifacts in git, "
    "then ACK (`egg-orch consensus ack <role>`) or NACK (`egg-orch consensus nack <role>`). "
    "Once all reviewed, confirm.\n"
    "4. **Stay alive** — keep polling with `egg-orch message poll --wait 30`. "
    "The orchestrator will send SIGTERM when consensus is reached.\n\n"
    "**If you exit again without reaching CONFIRMED, you will be restarted again "
    "(up to the maximum).**\n"
)

# Shell script that wraps the Claude CLI invocation. After Claude exits:
# - Non-concurrent mode: exit normally.
# - Non-zero exit: treat as failure, no restart.
# - Clean exit (code 0): restart Claude with a recovery prompt (up to
#   MAX_RESTARTS times). After max restarts, exit 1 to trigger the
#   orchestrator's agent failure path (HITL decision).
_CONSENSUS_WRAPPER_TEMPLATE = r"""
#!/bin/bash
set -uo pipefail

MAX_RESTARTS={max_restarts}
RESTART_COUNT=0

run_claude() {{
    local prompt="$1"
    {claude_command_prefix} "$prompt"
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
run_claude {initial_prompt}
CLAUDE_EXIT=$?

# If not in concurrent mode, exit normally
if [ "${{EGG_CONCURRENT_MODE:-}}" != "true" ]; then
    exit $CLAUDE_EXIT
fi

# Non-zero exit means the agent crashed — do not restart.
if [ "$CLAUDE_EXIT" -ne 0 ]; then
    echo "[consensus-wrapper] Agent failed (code $CLAUDE_EXIT). NOT restarting."
    exit $CLAUDE_EXIT
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
    echo "[consensus-wrapper] Consensus already reached. Exiting."
    exit 0
fi

# Check if this agent already reached CONFIRMED state (BRC protocol)
AGENT_ROLE="${{EGG_AGENT_ROLE:-}}"
if [ -n "$AGENT_ROLE" ]; then
    AGENT_CONFIRMED=$(get_agent_confirmed "$RESPONSE" "$AGENT_ROLE")
    if [ "$AGENT_CONFIRMED" = "True" ]; then
        echo "[consensus-wrapper] Agent already CONFIRMED in BRC protocol. Waiting for consensus..."
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
                echo "[consensus-wrapper] Consensus reached. Exiting."
                exit 0
            fi
        done
        echo "[consensus-wrapper] Agent was CONFIRMED but consensus not reached. Exiting cleanly."
        exit 0
    fi
fi

# --- Restart loop for clean exits without BRC consensus ---
while [ "$RESTART_COUNT" -lt "$MAX_RESTARTS" ]; do
    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo "[consensus-wrapper] Agent exited without BRC consensus. Restarting ($RESTART_COUNT/$MAX_RESTARTS)..."

    # Get current BRC state and NACK feedback for the recovery prompt
    RESPONSE=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
    BRC_STATE="unknown"
    NACK_FEEDBACK=""
    if [ -n "$AGENT_ROLE" ]; then
        BRC_STATE=$(get_brc_state "$RESPONSE" "$AGENT_ROLE")
        NACK_FEEDBACK=$(get_nack_feedback "$RESPONSE" "$AGENT_ROLE")
    fi

    # Build recovery prompt with restart context
    RECOVERY_PROMPT=$(cat <<'RECOVERY_EOF'
{recovery_prompt_template}
RECOVERY_EOF
)
    # Use Python for safe template substitution (avoids sed/awk special character
    # issues with backslashes, ampersands, and other chars in NACK feedback text)
    RECOVERY_PROMPT=$(_CW_RESTART="$RESTART_COUNT" _CW_MAX="$MAX_RESTARTS" \
        _CW_BRC="$BRC_STATE" _CW_NACK="$NACK_FEEDBACK" \
        python3 -c 'import sys, os
template = sys.stdin.read()
for old, key in [("{{restart_number}}", "_CW_RESTART"), ("{{max_restarts}}", "_CW_MAX"),
                 ("{{brc_state}}", "_CW_BRC"), ("{{nack_feedback}}", "_CW_NACK")]:
    template = template.replace(old, os.environ[key])
sys.stdout.write(template)' <<< "$RECOVERY_PROMPT")

    run_claude "$RECOVERY_PROMPT"
    CLAUDE_EXIT=$?

    if [ "$CLAUDE_EXIT" -ne 0 ]; then
        echo "[consensus-wrapper] Agent failed on restart $RESTART_COUNT (code $CLAUDE_EXIT). Stopping."
        exit $CLAUDE_EXIT
    fi

    # Check if consensus was reached during the restart
    RESPONSE=$(egg-orch pipeline status --json 2>/dev/null || echo "{{}}")
    IS_COMPLETE=$(echo "$RESPONSE" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('concurrent',{{}}).get('consensus',{{}}).get('is_complete',False))" \
        2>/dev/null || echo "False")

    if [ "$IS_COMPLETE" = "True" ]; then
        echo "[consensus-wrapper] Consensus reached after restart $RESTART_COUNT. Exiting."
        exit 0
    fi
done

# --- Max restarts exhausted: shut down with failure ---
echo "[consensus-wrapper] Max restarts ($MAX_RESTARTS) exhausted. Agent never reached CONFIRMED. Exiting with failure."
exit 1
"""


def build_consensus_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 200,
    max_restarts: int = MAX_CONSENSUS_RESTARTS,
    max_ready_polls: int = MAX_READY_POLL_CYCLES,
) -> list[str]:
    """Build a shell command that runs Claude with a BRC consensus restart wrapper.

    The wrapper detects when Claude exits without reaching CONFIRMED state
    in the BRC protocol and restarts it with a recovery prompt. This ensures
    agents explicitly participate in the Broadcast-Review-Converge consensus
    rather than having it faked.

    Args:
        prompt_text: The prompt to pass to the Claude CLI.
        model: Claude model to use.
        max_turns: Maximum number of tool-call turns.
        max_restarts: Maximum restart attempts before exiting with failure.
        max_ready_polls: Maximum poll cycles to wait when agent already
            signaled READY (avoids unnecessary restarts).

    Returns:
        Command list suitable for container spawning (bash -c "...").
    """
    # Build the claude command prefix (everything except the prompt argument)
    claude_prefix_parts = [
        "claude",
        "--dangerously-skip-permissions",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
    ]
    claude_command_prefix = " ".join(shlex.quote(p) for p in claude_prefix_parts)
    initial_prompt = shlex.quote(prompt_text)

    script = _CONSENSUS_WRAPPER_TEMPLATE.format(
        claude_command_prefix=claude_command_prefix,
        initial_prompt=initial_prompt,
        max_restarts=max_restarts,
        max_ready_polls=max_ready_polls,
        recovery_prompt_template=_RECOVERY_PROMPT,
    )

    return ["bash", "-c", script]
