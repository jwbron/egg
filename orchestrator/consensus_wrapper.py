"""Build consensus-wrapped commands for concurrent agent containers.

When agents run in concurrent mode, they must stay alive after completing
their work to participate in consensus. This module provides a shell wrapper
that detects early Claude exits and restarts the agent with a recovery prompt
instead of blindly marking consensus as approved.

If the agent exits without signaling READY, the wrapper restarts Claude with
a prompt that explains what happened and instructs it to assess state, then
either signal READY or continue working. Restarts are capped at
``MAX_CONSENSUS_RESTARTS`` (default 2). After exhausting restarts the wrapper
exits with code 1 so the orchestrator's failure path handles escalation.
"""

import shlex

# Default maximum number of times the wrapper will restart Claude after a
# clean exit without consensus being reached.
MAX_CONSENSUS_RESTARTS = 2

# Recovery prompt given to Claude when it is restarted by the wrapper.
# Placeholders: {restart_number}, {max_restarts}, {role}
_RECOVERY_PROMPT = (
    "## CONSENSUS RECOVERY — You were restarted by the consensus wrapper\n\n"
    "You exited your previous session without the orchestrator confirming "
    "consensus. This is restart {restart_number} of {max_restarts}.\n\n"
    "**What happened**: Your agent process exited cleanly, but the consensus "
    "protocol requires you to remain alive until ALL agents signal READY and "
    "the orchestrator stops your container. Because you exited early, the "
    "wrapper restarted you so you can finish the protocol.\n\n"
    "**What you must do now**:\n"
    "1. Poll for messages: `egg-orch message poll`\n"
    "2. Check if your work is complete — review any new commits or feedback "
    "from other agents.\n"
    "3. If your work is done, signal READY: "
    '`egg-orch signal readiness --state READY --reason "Work complete"`\n'
    "4. If there is new feedback or work to address, handle it first, then "
    "signal READY.\n"
    "5. **Stay alive** — keep polling with `egg-orch message poll` in a loop. "
    "Do NOT exit. The orchestrator will send SIGTERM when consensus is reached.\n\n"
    "**If you exit again without signaling READY, you will be restarted again "
    "(up to the maximum). After that, your role will be left without consensus "
    "and the orchestrator will need to handle it.**\n"
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

# --- Initial run ---
run_claude {initial_prompt}
CLAUDE_EXIT=$?

# If not in concurrent mode, exit normally
if [ "${{EGG_CONCURRENT_MODE:-}}" != "true" ]; then
    exit $CLAUDE_EXIT
fi

# Non-zero exit means the agent crashed — do not restart or signal READY.
if [ "$CLAUDE_EXIT" -ne 0 ]; then
    echo "[consensus-wrapper] Agent failed (code $CLAUDE_EXIT). NOT restarting."
    exit $CLAUDE_EXIT
fi

# --- Restart loop for clean exits without consensus ---
while [ "$RESTART_COUNT" -lt "$MAX_RESTARTS" ]; do
    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo "[consensus-wrapper] Agent exited without consensus. Restarting ($RESTART_COUNT/$MAX_RESTARTS)..."

    # Build recovery prompt with restart context
    RECOVERY_PROMPT=$(cat <<'RECOVERY_EOF'
{recovery_prompt_template}
RECOVERY_EOF
)
    # Substitute restart number into the prompt
    RECOVERY_PROMPT=$(echo "$RECOVERY_PROMPT" | sed "s/{{restart_number}}/$RESTART_COUNT/g; s/{{max_restarts}}/$MAX_RESTARTS/g")

    run_claude "$RECOVERY_PROMPT"
    CLAUDE_EXIT=$?

    if [ "$CLAUDE_EXIT" -ne 0 ]; then
        echo "[consensus-wrapper] Agent failed on restart $RESTART_COUNT (code $CLAUDE_EXIT). Stopping."
        exit $CLAUDE_EXIT
    fi

    # Check if consensus was reached during the restart
    RESPONSE=$(egg-orch message status --json 2>/dev/null || echo "{{}}")
    IS_COMPLETE=$(echo "$RESPONSE" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('consensus',{{}}).get('is_complete',False))" \
        2>/dev/null || echo "False")

    if [ "$IS_COMPLETE" = "True" ]; then
        echo "[consensus-wrapper] Consensus reached after restart $RESTART_COUNT. Exiting."
        exit 0
    fi
done

# --- Max restarts exhausted: shut down with failure ---
echo "[consensus-wrapper] Max restarts ($MAX_RESTARTS) exhausted. Agent never signaled READY. Exiting with failure."
exit 1
"""


def build_consensus_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 200,
    max_restarts: int = MAX_CONSENSUS_RESTARTS,
) -> list[str]:
    """Build a shell command that runs Claude with a consensus restart wrapper.

    The wrapper detects when Claude exits without consensus and restarts it
    with a recovery prompt instead of auto-signaling READY. This ensures
    agents explicitly participate in consensus rather than having it faked.

    Args:
        prompt_text: The prompt to pass to the Claude CLI.
        model: Claude model to use.
        max_turns: Maximum number of tool-call turns.
        max_restarts: Maximum restart attempts before exiting with failure.

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
        recovery_prompt_template=_RECOVERY_PROMPT,
    )

    return ["bash", "-c", script]
