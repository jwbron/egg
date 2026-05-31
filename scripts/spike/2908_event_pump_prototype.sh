#!/bin/bash
# 2908_event_pump_prototype.sh — THROWAWAY spike (issue #2908, slice-1 / WS0)
#
# Deterministic BRC event-pump prototype that mimics the production
# ``orchestrator/consensus_wrapper.py`` rewrite landed in slice-6.  Used
# to de-risk the rewrite by running against the #2906 reproducer before
# the production wrapper changes.
#
# The prototype is intentionally minimal:
#
#   1. Read env vars (EGG_PIPELINE_ID, EGG_AGENT_ROLE, EGG_SLICE_ID).
#   2. Loop:
#      a. Call ``egg-orch consensus status --json`` and check
#         ``.consensus.is_complete``.  If true → exit 0.
#      b. Block on ``egg-orch message wait-loop --timeout 60`` for the
#         full BRC event-type set the production wrapper consumes.
#      c. On each actionable event, spawn the per-event agent via
#         ``python3 -m egg_agent`` (built by
#         ``shared/egg_agent/command.py:build_agent_command`` — NOT the
#         CLI single-shot mode, which is an EGG100 anti-pattern per
#         ``docs/guides/agent-mode-design.md:90-104``).
#      d. Record the iteration in a tmp memory file so the next per-event
#         agent invocation can see prior actions.
#
# The script is throwaway and intentionally avoids the slice-2/3/4/5/6
# durable primitives (no contract field writes, no sync-flush, no
# CONSENSUS_PROPOSE/ACK plumbing).  The memory file format is a flat
# JSONL append-log under ``/tmp`` — the durable format is decided in
# slice-5 (``brc-memory.md`` distilled).
#
# Acceptance gates (per task-1-1):
#   * Script exists at this path.
#   * The EGG100 anti-pattern grep (CLI single-shot subprocess) returns
#     zero hits in this script.
#   * Running against a mock orchestrator that emits one
#     CONSENSUS_PROPOSE → ACK → CONSENSUS_CONFIRMED sequence completes
#     without a Python traceback and exits 0.
#   * ``grep -n`` confirms the script invokes ``python3 -m egg_agent``.
#
# This script is NOT wired into any production code path.  It is run
# manually inside a k3s test pod for slice-1's measurement.

set -uo pipefail

# ---------------------------------------------------------------------------
# Env / args
# ---------------------------------------------------------------------------
EGG_PIPELINE_ID="${EGG_PIPELINE_ID:-}"
EGG_AGENT_ROLE="${EGG_AGENT_ROLE:-}"
EGG_SLICE_ID="${EGG_SLICE_ID:-}"

# Per-event agent prompt template.  Pointed at the spike memory file so
# the agent sees what prior iterations did.  Real production wrapper
# (slice-6) injects the slice-5 ``brc-memory.md`` path here.
PROMPT_PROMPT_TEMPLATE='You are the %s for pipeline %s. A BRC event just arrived: %s. Spike memory file: %s. Inspect the event, take one BRC action (propose/ack/nack/confirm), then exit cleanly. Do NOT re-enter a wait loop — the wrapper will re-invoke you on the next event.'

# Spike-local memory file.  Format: JSONL of "{iter, ts, event_type, action}"
# rows appended after every per-event spawn.  Throwaway — durable format
# is slice-5's job.
SPIKE_MEMORY="${SPIKE_MEMORY:-/tmp/2908-event-pump-spike-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}.jsonl}"
touch "$SPIKE_MEMORY"

# Per-iteration event payload tmp file.  ``egg-orch message wait-loop``
# prints the matched message to stdout; we capture it here so the
# per-event agent invocation receives the full payload as a prompt
# argument.
EVENT_LOG="${EVENT_LOG:-/tmp/2908-event-pump-spike-event-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}.log}"

# Per-event timing log (input to task-1-2's run log).
TIMING_LOG="${TIMING_LOG:-/tmp/2908-event-pump-spike-timing-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}.tsv}"
if [ ! -s "$TIMING_LOG" ]; then
    echo -e "iter\tstart_epoch\tevent_type\tagent_exit\twall_secs" > "$TIMING_LOG"
fi

# Agent model.  Spike defaults to the orchestrator alias resolution
# already used at ``orchestrator/concurrent_executor.py:489`` — k3s test
# pods pin via $EGG_SPIKE_MODEL.
EGG_SPIKE_MODEL="${EGG_SPIKE_MODEL:-sonnet}"

# BRC event-type set the wrapper consumes (matches the production
# producer wait set documented at ``docs/reference/agent-wait-patterns.md``
# minus CONSENSUS_CONFIRMED — the wrapper polls consensus status for
# that directly).
WAIT_FOR_FLAGS=(
    "--for" "CONSENSUS_PROPOSE"
    "--for" "CONSENSUS_NACK"
    "--for" "CONSENSUS_ACK"
    "--for" "CONSENSUS_RE_REVIEW"
    "--for" "STATUS"
    "--for" "HANDOFF"
    "--for" "OVERSEER_ALERT"
)

# Hard cap on loop iterations as a spike safety valve.  Production
# wrapper has no such cap (the orchestrator drives termination); the
# spike caps to keep a runaway test pod from chewing tokens.
MAX_SPIKE_ITERATIONS="${MAX_SPIKE_ITERATIONS:-200}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
    echo "[spike $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2
}

require_env() {
    if [ -z "$EGG_PIPELINE_ID" ]; then
        log "ERROR: EGG_PIPELINE_ID is unset; cannot run event pump"
        exit 2
    fi
    if [ -z "$EGG_AGENT_ROLE" ]; then
        log "ERROR: EGG_AGENT_ROLE is unset; cannot run event pump"
        exit 2
    fi
}

is_consensus_complete() {
    # Returns 0 if the orchestrator reports the role's consensus is
    # complete AND the broader consensus payload is_complete=true.
    # Uses ``egg-orch consensus status --json`` per the slice-3
    # extension point — at spike time the endpoint already returns
    # ``is_complete`` in the consensus payload (slice-3 will add
    # ``next_action`` per role; the spike does not depend on that yet).
    local status_json
    if ! status_json=$(egg-orch consensus status --json 2>/dev/null); then
        return 1
    fi
    # ``is_complete`` is the top-level boolean in the --json payload
    # (matches consensus.is_complete).  Use python over jq because the
    # sandbox image guarantees python but not jq.
    python3 - "$status_json" <<'PYEOF'
import json
import sys

raw = sys.argv[1] or "{}"
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(1)
# ``consensus status --json`` prints the consensus block directly
# (see cmd_consensus_status at sandbox/egg_lib/orch_cli.py:2810).
if data.get("is_complete") is True:
    sys.exit(0)
sys.exit(1)
PYEOF
}

build_per_event_prompt() {
    # Render the minimal per-event prompt the spike feeds to
    # ``python3 -m egg_agent``.  Production wrapper (slice-6) renders a
    # similar lean prompt via the slice-7 delta adapter; the spike is
    # deliberately minimal so we exercise the cache-cost dimension
    # without the full slice-5 memory file.
    local event_blob="$1"
    # Trim event blob to a sane size for the spike prompt.  Production
    # wrapper would render via the slice-7 BrcDelta adapter.
    local trimmed
    trimmed=$(printf '%s' "$event_blob" | head -c 4000)
    # shellcheck disable=SC2059  # PROMPT_PROMPT_TEMPLATE is a fixed
    # constant defined at the top of this file; the %s placeholders
    # are part of the format-string contract.  Switching to '%s' would
    # defeat the per-event template parametrization.
    printf "$PROMPT_PROMPT_TEMPLATE" \
        "$EGG_AGENT_ROLE" "$EGG_PIPELINE_ID" "$trimmed" "$SPIKE_MEMORY"
}

spawn_agent_for_event() {
    # Spawn the per-event agent via ``python3 -m egg_agent`` — the
    # canonical entry point built by
    # ``shared/egg_agent/command.py:build_agent_command``.  We do NOT
    # invoke the CLI in single-shot mode here (EGG100 anti-pattern per
    # docs/guides/agent-mode-design.md:90-104).
    local iter_n="$1"
    local event_blob="$2"
    local event_type="$3"
    local start_epoch end_epoch wall_secs prompt exit_code

    start_epoch=$(date -u +%s)
    prompt="$(build_per_event_prompt "$event_blob")"

    # Per-event invocation.  ``--model``, ``--max-turns``, and the
    # positional prompt match the build_agent_command signature so the
    # production rewrite can use this exact pattern.
    set +e
    python3 -m egg_agent \
        --model "$EGG_SPIKE_MODEL" \
        --max-turns 50 \
        "$prompt"
    exit_code=$?
    set -e
    end_epoch=$(date -u +%s)
    wall_secs=$((end_epoch - start_epoch))

    # Append to spike memory + timing log.
    printf '{"iter": %d, "ts": "%s", "event_type": "%s", "agent_exit": %d, "wall_secs": %d}\n' \
        "$iter_n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$event_type" "$exit_code" "$wall_secs" \
        >> "$SPIKE_MEMORY"
    echo -e "${iter_n}\t${start_epoch}\t${event_type}\t${exit_code}\t${wall_secs}" >> "$TIMING_LOG"

    return $exit_code
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
main() {
    require_env

    log "Starting event-pump spike for ${EGG_AGENT_ROLE} on pipeline ${EGG_PIPELINE_ID}"
    log "Memory file: $SPIKE_MEMORY"
    log "Timing log:  $TIMING_LOG"

    local iter_n=0
    while [ "$iter_n" -lt "$MAX_SPIKE_ITERATIONS" ]; do
        iter_n=$((iter_n + 1))

        # 1. Check consensus.is_complete on every iteration — exits 0
        #    as soon as the orchestrator reports the role is done.
        if is_consensus_complete; then
            log "Consensus complete on iteration ${iter_n}; exiting 0"
            exit 0
        fi

        # 2. Block on the next BRC event.  Cursor threading is owned by
        #    the CLI (issue #2323) — the wrapper does NOT roll its own
        #    wait/cursor logic.
        log "Iteration ${iter_n}: waiting for next BRC event (timeout 60s)"
        : > "$EVENT_LOG"
        set +e
        egg-orch message wait-loop "${WAIT_FOR_FLAGS[@]}" --timeout 60 \
            > "$EVENT_LOG" 2>&1
        wait_rc=$?
        set -e

        # rc=0 → matched event in $EVENT_LOG; rc=1 → permanent error or
        # timeout (re-loop and recheck consensus status).
        if [ "$wait_rc" -ne 0 ]; then
            log "wait-loop returned rc=${wait_rc}; re-checking consensus"
            continue
        fi

        # 3. Extract a quick event-type tag from the wait-loop output.
        #    The CLI prints ``[ts] from -> to (TYPE): subject`` per
        #    matched message (cmd_message_wait_loop:1814-1819) — grep
        #    out the first TYPE in parens.
        event_type=$(grep -oE '\(([A-Z_]+)\)' "$EVENT_LOG" | head -n 1 | tr -d '()')
        if [ -z "$event_type" ]; then
            event_type="UNKNOWN"
        fi
        log "Got event type=${event_type}; spawning per-event agent"

        # 4. Spawn the per-event agent.  Exit code is recorded; the
        #    spike never restarts on non-zero (the orchestrator
        #    determines BRC state, not the agent's exit code).
        set +e
        spawn_agent_for_event "$iter_n" "$(cat "$EVENT_LOG")" "$event_type"
        agent_rc=$?
        set -e
        log "Agent exited rc=${agent_rc} on iteration ${iter_n}"
    done

    log "Hit MAX_SPIKE_ITERATIONS=${MAX_SPIKE_ITERATIONS}; exiting 1"
    exit 1
}

main "$@"
