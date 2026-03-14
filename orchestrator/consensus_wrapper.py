"""Build consensus-wrapped commands for concurrent agent containers.

When agents run in concurrent mode, they must stay alive after completing
their work to participate in consensus. This module provides a shell wrapper
that catches early Claude exits and keeps the container alive polling for
consensus, as a safety net for agents that exit without following the
stay-alive protocol.
"""

import shlex

# Shell script that wraps the Claude CLI invocation. After Claude exits,
# if EGG_CONCURRENT_MODE is set and exit was clean (code 0), it auto-signals
# READY (if the agent didn't) and polls until consensus is reached or a
# timeout expires. Non-zero exits are treated as failures — no READY signal.
_CONSENSUS_WRAPPER_TEMPLATE = r"""
#!/bin/bash
set -uo pipefail

# Run the Claude agent
{claude_command}
CLAUDE_EXIT=$?

# If not in concurrent mode, exit normally
if [ "${{EGG_CONCURRENT_MODE:-}}" != "true" ]; then
    exit $CLAUDE_EXIT
fi

# Only signal READY on clean exit. A non-zero exit means the agent crashed
# or errored — its work may be incomplete, so we must NOT claim readiness.
if [ "$CLAUDE_EXIT" -ne 0 ]; then
    echo "[consensus-wrapper] Agent failed (code $CLAUDE_EXIT). NOT signaling READY."
    exit $CLAUDE_EXIT
fi

# Clean exit — auto-signal READY as a safety net.
# If the agent already signaled READY, this is a no-op update.
echo "[consensus-wrapper] Agent exited cleanly. Auto-signaling READY..."
egg-orch signal readiness --state READY \
    --reason "Agent process exited cleanly, auto-signaling READY" \
    2>/dev/null || true

# Stay alive polling for consensus until reached or timeout.
POLL_INTERVAL="${{EGG_MESSAGE_POLL_INTERVAL:-30}}"
TIMEOUT="${{EGG_CONSENSUS_WRAPPER_TIMEOUT:-300}}"
ELAPSED=0

echo "[consensus-wrapper] Entering consensus wait loop (timeout=${{TIMEOUT}}s)..."
while [ "$ELAPSED" -lt "$TIMEOUT" ]; do
    # Check consensus via readiness signal response
    RESPONSE=$(egg-orch signal readiness --state READY \
        --reason "Waiting for consensus" --json 2>/dev/null || echo "{{}}")

    IS_COMPLETE=$(echo "$RESPONSE" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d.get('data',{{}}).get('consensus',{{}}).get('is_complete',False))" \
        2>/dev/null || echo "False")

    if [ "$IS_COMPLETE" = "True" ]; then
        echo "[consensus-wrapper] Consensus reached. Exiting."
        exit $CLAUDE_EXIT
    fi

    # Poll for messages (may contain work that should have been handled)
    egg-orch message poll 2>/dev/null || true

    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

echo "[consensus-wrapper] Consensus not reached within ${{TIMEOUT}}s. Exiting."
exit $CLAUDE_EXIT
"""


def build_consensus_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 200,
) -> list[str]:
    """Build a shell command that runs Claude with a consensus wait wrapper.

    The wrapper ensures that after Claude exits, the container stays alive
    polling for consensus rather than disappearing and triggering the
    orchestrator's fallback path.

    Args:
        prompt_text: The prompt to pass to the Claude CLI.
        model: Claude model to use.
        max_turns: Maximum number of tool-call turns.

    Returns:
        Command list suitable for container spawning (bash -c "...").
    """
    claude_parts = [
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
        prompt_text,
    ]
    claude_command = " ".join(shlex.quote(p) for p in claude_parts)
    script = _CONSENSUS_WRAPPER_TEMPLATE.format(claude_command=claude_command)

    return ["bash", "-c", script]
