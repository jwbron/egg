#!/usr/bin/env bash
# 2908_event_pump_prototype.sh — WS0 de-risking spike (issue #2908)
#
# THROWAWAY PROTOTYPE.  This is *not* a production primitive.  Its only job
# is to demonstrate the new control flow that slices 5-6 will land in
# `orchestrator/consensus_wrapper.py`:
#
#   while not (consensus-confirmed AND is_complete):
#       call `egg-orch message wait-loop --for <BRC events> --timeout 60`
#       on actionable event -> spawn `python3 -m egg_agent` (the raw `claude`
#                              CLI's print mode is an EGG100 anti-pattern; see
#                              docs/guides/agent-mode-design.md:90-104)
#       agent reads `--memory-file <path>` + the one event, acts, exits naturally
#       loop continues with cursor state preserved by the CLI (#2323)
#
# Because the production `egg-orch consensus next-action` verb does not
# exist yet (it lands in slice-3), this prototype derives `is_complete`
# from `egg-orch consensus status --json` (existing verb) and treats every
# delivered BRC message as an "actionable event" without further per-event
# discrimination — slice-3 will move that discrimination server-side.
#
# Two execution modes:
#
#   - `--real` (default): polls the live orchestrator using the env vars
#     EGG_PIPELINE_ID, EGG_AGENT_ROLE, EGG_SLICE_ID, and
#     ORCHESTRATOR_URL (passed through to `egg-orch`).
#
#   - `--mock <event-script>`: drives the loop from a local JSON-lines
#     file of stub BRC events so the script's acceptance criteria can be
#     verified without a cluster.  This is what TASK-1-1's AC asks for:
#     "running it against a mock orchestrator that emits one
#      CONSENSUS_PROPOSE → ACK → CONSENSUS_CONFIRMED sequence completes
#      without a Python traceback and exits 0".
#
# Memory carry-across (TASK-1-1 (c)): the prototype keeps a tmp-file
# memory of prior actions; the durable format lives in slice-7 and is
# explicitly out of scope here.
#
# Slice-5/6 carry-forward notes for the production wrapper rewrite
# (intentional simplifications in this throwaway prototype that the
# production wrapper MUST NOT inherit):
#
#   * `is_consensus_complete` swallows `egg-orch consensus status`
#     failures (stderr -> /dev/null) and treats them as "not complete",
#     so an unreachable orchestrator silently spins.  The slice-5/6
#     wrapper must classify exit codes and surface a distinct signal
#     (OVERSEER_ALERT or controlled exit) on orchestrator-unreachable
#     -- cq-3's durable safety-budget assumes BRC events arrive.
#   * `wait_for_event` collapses (a) 60s timeout, (b) transient error,
#     (c) permanent error into `rc != 0 -> continue`.  Production must
#     distinguish: permanent -> exit non-zero (CLAUDE.md wait-pattern
#     guidance).
#   * No periodic heartbeat from the wrapper itself (#2036 / #2451).
#     The mock-mode run finishes in milliseconds so it never trips the
#     stall window; production runs that block on a long agent spawn
#     would.  Slice-5/6 wrapper must emit `egg-orch message heartbeat`
#     on each loop iteration (or a background timer).
#
# This script will be DELETED after the production wrapper rewrite
# (slices 5-6) lands — do not depend on it from any test or doc.

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
MODE="real"
MOCK_EVENT_FILE=""
MEMORY_FILE_OVERRIDE=""
MAX_EVENTS="0"  # 0 = unlimited; TASK-1-2 uses a small cap during the spike

while [[ $# -gt 0 ]]; do
    case "$1" in
        --real)
            MODE="real"
            shift
            ;;
        --mock)
            MODE="mock"
            MOCK_EVENT_FILE="${2:-}"
            if [[ -z "$MOCK_EVENT_FILE" ]]; then
                echo "ERROR: --mock requires a path to a JSON-lines event file" >&2
                exit 2
            fi
            shift 2
            ;;
        --memory-file)
            MEMORY_FILE_OVERRIDE="${2:-}"
            shift 2
            ;;
        --max-events)
            MAX_EVENTS="${2:-0}"
            shift 2
            ;;
        -h | --help)
            sed -n '1,55p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
: "${EGG_PIPELINE_ID:?EGG_PIPELINE_ID must be set}"
: "${EGG_AGENT_ROLE:?EGG_AGENT_ROLE must be set}"
EGG_SLICE_ID="${EGG_SLICE_ID:-}"

MEMORY_FILE="${MEMORY_FILE_OVERRIDE:-$(mktemp -t "2908-memory-${EGG_AGENT_ROLE}.XXXXXX")}"
echo "spike: memory-file=${MEMORY_FILE}"

# Mock-mode sentinel: subshell `$()` captures prevent `export` from
# escaping `wait_for_event`, so we use a tmp file the main loop can
# stat to decide consensus-complete.
MOCK_CONFIRMED_FLAG="$(mktemp -t "2908-confirmed.XXXXXX")"
rm -f "$MOCK_CONFIRMED_FLAG"  # ensure absent until CONFIRMED arrives

cleanup() {
    if [[ -z "$MEMORY_FILE_OVERRIDE" && -e "$MEMORY_FILE" ]]; then
        rm -f "$MEMORY_FILE" || true
    fi
    rm -f "$MOCK_CONFIRMED_FLAG" || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
    # ISO-8601 prefix so the run log produced by TASK-1-2 is grep-friendly.
    printf '%s [event-pump:%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$EGG_AGENT_ROLE" "$*"
}

# Spawn the per-event agent.  Uses `python3 -m egg_agent` (the raw CLI
# print-mode entrypoint is forbidden by EGG100).  The `--memory-file`
# and `--event-json` flags are
# the slice-5 TASK-5-1 surface the production wrapper will rely on; for
# the spike we approximate by passing them via the prompt body when the
# binary doesn't yet accept the flags.
spawn_agent() {
    local event_payload="$1"
    local prompt
    prompt="$(printf 'You are the %s role on pipeline %s.\nMemory file: %s\nOne BRC event follows; handle it and exit cleanly.\n--- event ---\n%s\n' \
        "$EGG_AGENT_ROLE" "$EGG_PIPELINE_ID" "$MEMORY_FILE" "$event_payload")"

    if [[ "$MODE" == "mock" ]]; then
        # In mock mode we don't spawn a real agent -- we just record the
        # invocation in the memory file so the AC ("script invokes
        # python3 -m egg_agent, verifiable via grep -n") is satisfied by
        # the source, and the run log captures the would-be argv.
        printf 'spike-mock-invoke: python3 -m egg_agent --memory-file %q --event-json %q -- <prompt %d bytes>\n' \
            "$MEMORY_FILE" "$event_payload" "${#prompt}"
        printf 'event-handled-at=%s payload-len=%d\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${#event_payload}" >>"$MEMORY_FILE"
        return 0
    fi

    # Real mode: shlex.quote is not available in bash but printf %q is
    # equivalent for argv quoting (BC-2 corollary -- never interpolate
    # raw prose into a shell-evaluated command line).
    log "spawning: python3 -m egg_agent (memory=${MEMORY_FILE}, prompt=${#prompt} bytes)"
    if command -v python3 >/dev/null 2>&1; then
        # The production --memory-file / --event-json flags land in
        # slice-5 TASK-5-1; until then, pass the joined prompt
        # positionally and let the agent read the memory file from a
        # well-known path.  No raw-CLI print-mode entrypoint.
        python3 -m egg_agent --max-turns 20 "$prompt" || {
            log "agent exited non-zero (rc=$?); spike continues"
        }
    else
        log "python3 missing; would have spawned python3 -m egg_agent"
    fi
}

is_consensus_complete() {
    # Returns 0 (success / true) when consensus.is_complete == true for
    # this role in the current pipeline scope.  In mock mode the
    # CONSENSUS_CONFIRMED branch of `wait_for_event` touches a sentinel
    # file because subshell exports don't escape `$(...)` capture.
    if [[ "$MODE" == "mock" ]]; then
        [[ -e "$MOCK_CONFIRMED_FLAG" ]]
        return $?
    fi
    local args=(consensus status "$EGG_PIPELINE_ID" --json)
    if [[ -n "$EGG_SLICE_ID" ]]; then
        args+=(--slice-id "$EGG_SLICE_ID")
    fi
    # We pipe through python3 (already a hard dep) rather than jq so the
    # spike can run inside a stripped-down sandbox.
    local json
    if ! json="$(egg-orch "${args[@]}" 2>/dev/null)"; then
        log "consensus-status query failed; assuming not-complete"
        return 1
    fi
    python3 - <<'PY' "$json" "$EGG_AGENT_ROLE"
import json
import sys

payload = sys.argv[1]
role = sys.argv[2]
try:
    data = json.loads(payload)
except Exception:
    sys.exit(1)
if data.get("is_complete") is True:
    sys.exit(0)
agents = data.get("agents") or {}
agent = agents.get(role) or {}
# Treat per-role completeness as "this role's consensus is confirmed"
# -- the precise field name varies by orchestrator version, so probe
# the common shapes.
for key in ("is_complete", "confirmed", "consensus_confirmed"):
    if agent.get(key) is True:
        sys.exit(0)
sys.exit(1)
PY
}

# Block on the next BRC event.  In real mode this is the production CLI
# call.  In mock mode we read one line from the event script file.
wait_for_event() {
    if [[ "$MODE" == "mock" ]]; then
        # Each non-blank line is one event; the special token
        # CONSENSUS_CONFIRMED touches a file the parent loop watches.
        local line
        if ! IFS= read -r line <&"$MOCK_FD"; then
            return 2  # EOF -- caller should bail rather than spin
        fi
        if [[ -z "$line" ]]; then
            return 1
        fi
        if [[ "$line" == "CONSENSUS_CONFIRMED" ]]; then
            : >"$MOCK_CONFIRMED_FLAG"
            printf 'CONSENSUS_CONFIRMED\n'
            return 0
        fi
        printf '%s\n' "$line"
        return 0
    fi

    # Real mode: invoke the existing CLI wait-loop verb.  We list every
    # BRC event type the slice-5 wrapper will care about; the CLI
    # threads the cursor for us across re-entries (#2323).
    local out
    if ! out="$(
        egg-orch message wait-loop \
            --for CONSENSUS_PROPOSE \
            --for CONSENSUS_ACK \
            --for CONSENSUS_NACK \
            --for CONSENSUS_RE_REVIEW \
            --for CONSENSUS_CONFIRMED \
            --for STATUS \
            --for HANDOFF \
            --for OVERSEER_ALERT \
            --max-iterations 1 \
            --timeout 60 \
            --pipeline-id "$EGG_PIPELINE_ID" \
            --role "$EGG_AGENT_ROLE" \
            ${EGG_SLICE_ID:+--slice-id "$EGG_SLICE_ID"} 2>/dev/null
    )"; then
        # Timeout / transient error -- let the outer loop re-poll.
        return 1
    fi
    printf '%s\n' "$out"
    return 0
}

# ---------------------------------------------------------------------------
# Mock-mode setup
# ---------------------------------------------------------------------------
if [[ "$MODE" == "mock" ]]; then
    if [[ ! -e "$MOCK_EVENT_FILE" ]]; then
        echo "ERROR: mock event file not found: $MOCK_EVENT_FILE" >&2
        exit 2
    fi
    exec {MOCK_FD}<"$MOCK_EVENT_FILE"
fi

# ---------------------------------------------------------------------------
# Event pump
# ---------------------------------------------------------------------------
log "starting (mode=${MODE}, role=${EGG_AGENT_ROLE}, pipeline=${EGG_PIPELINE_ID}, slice=${EGG_SLICE_ID:-<none>})"

event_count=0
while true; do
    if is_consensus_complete; then
        log "consensus complete; exiting 0"
        exit 0
    fi

    if [[ "$MAX_EVENTS" != "0" && "$event_count" -ge "$MAX_EVENTS" ]]; then
        log "hit --max-events cap (${MAX_EVENTS}); exiting 0"
        exit 0
    fi

    event_payload=""
    wait_rc=0
    event_payload="$(wait_for_event)" || wait_rc=$?
    if [[ "$wait_rc" -eq 2 ]]; then
        log "event source exhausted (EOF); exiting 0"
        exit 0
    fi
    if [[ "$wait_rc" -ne 0 ]]; then
        log "no event this iteration; re-polling"
        continue
    fi

    if [[ -z "$event_payload" ]]; then
        log "empty event payload; re-polling"
        continue
    fi

    event_count=$((event_count + 1))
    log "event #${event_count} received (${#event_payload} bytes)"

    # On every actionable event we'd spawn an agent.  CONSENSUS_CONFIRMED
    # is special: it's the signal to re-check is_complete and exit, not
    # a prompt to spawn anything.
    if [[ "$event_payload" == *CONSENSUS_CONFIRMED* ]]; then
        log "saw CONSENSUS_CONFIRMED; re-checking is_complete"
        continue
    fi

    spawn_agent "$event_payload"
done
