"""Build consensus-wrapped commands for concurrent agent containers.

EVENT-PUMP MODEL (#2908)
~~~~~~~~~~~~~~~~~~~~~~~~
The consensus wrapper invokes the agent one-shot per actionable BRC
event. A deterministic bash loop drives the lifecycle:

* fetch BRC state via ``egg-orch brc get-state`` (slice-1 task-1-3);
* ask ``egg-orch brc next-action`` what to do (slice-1 task-1-1);
* on ``wait`` block on ``egg-orch message wait-loop`` while emitting
  ``egg-orch message heartbeat`` (heartbeat lineage #2036 + #2451 —
  the heartbeat carries ``slice_id``, so ``_maybe_attach_slice_id`` in
  the orchestrator fan-out refreshes the slice-scoped container session
  as a side effect of every wrapper heartbeat);
* on ``propose|ack|nack`` invoke the agent one-shot via
  ``python3 -m egg_agent`` with the per-event prompt composed by
  ``orchestrator/routes/event_prompt.py:compose_event_prompt``
  (slice-3 task-3-1);
* on ``confirm`` / ``complete`` call ``egg-orch consensus confirmed``;
* trip an ``OVERSEER_ALERT`` (anomaly ``stuck-phase-transition``) on
  the configured idle budget (env ``EGG_BRC_IDLE_BUDGET_MIN``,
  default 30 min, architect od-4); priority climbs to ``high`` on the
  2× boundary; the loop keeps blocking rather than exiting 1 →
  FAILED.
* escalate a consecutive agent-invocation failure streak (#3138):
  linear backoff on the ``propose|ack|nack`` arm (streak × 2 s,
  capped 30 s, parity with the ``confirm`` arm), a sticky log warning
  at streak 5, and a sticky ``OVERSEER_ALERT`` (anomaly
  ``agent-invocation-fail-streak``) at streak 10 whose detail
  classifies sub-second failures as configuration-class (unknown
  model alias, auth, prompt-rendering crash). The loop still never
  self-FAILs.

Slice-4 history
~~~~~~~~~~~~~~~
* slice-4 task-4-1 flipped the unset-env defaults: ``EGG_BRC_EVENT_PUMP``
  from off→on, ``EGG_BRC_MEMORY`` from ``off``→``full``.
* slice-4 task-4-2 (this PR) deleted the legacy capped-restart
  template, the recovery system / user prompts, the SSE consensus-
  reached curl path, the legacy restart cap constant (issue #2806)
  and its companion tunables (ready-poll cycles, transient-restart
  backoff initial, startup-failure window seconds), the
  ``EGG_BRC_EVENT_PUMP`` env flag itself, the legacy-template branch
  in ``build_consensus_wrapped_command``, and the agent-side
  heartbeat / gateway-session keep-alive in
  ``sandbox/egg_agent_tools/handlers/message.py``. The event-pump
  template is the only production path post-slice-4. Rollback under
  a regression is a ``git revert`` of slices 1–3 per the PR body;
  there is no env-flag rollback path.
* The buffer-overflow / transient-crash / startup-failure shell
  classifiers survived the deletion and now live inside the
  event-pump template (kept as named helpers for future use even
  though the current ``propose|ack|nack`` arm relies on the
  consecutive-failure counter + idle-budget escalation rather than
  branching on them directly).
"""

import shlex

try:
    from orchestrator import supervision_policy as _supervision_policy
except ImportError:
    import supervision_policy as _supervision_policy  # type: ignore[no-redef]

# Export the #3138 constants from supervision_policy so the wrapper
# template can interpolate them via ``str.format`` — one source of truth
# for backoff / streak thresholds.
SUPERVISION_BACKOFF_FACTOR = _supervision_policy.SUPERVISION_BACKOFF_FACTOR
SUPERVISION_BACKOFF_CAP_SECONDS = _supervision_policy.SUPERVISION_BACKOFF_CAP_SECONDS
SUPERVISION_FAILURE_STREAK_WARN = _supervision_policy.SUPERVISION_FAILURE_STREAK_WARN
SUPERVISION_FAILURE_STREAK_ALERT = _supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT


def _event_loop_owner() -> str:
    """Return the BRC event-loop ownership mode (``pod`` | ``orchestrator``).

    Thin lazy wrapper over ``env_config.get_event_loop_owner`` (#3064).
    The dual-path import mirrors ``concurrent_executor`` /
    ``global_slice_admit``: the orchestrator process may have the repo
    root on ``sys.path`` (``orchestrator.env_config``) or only
    ``orchestrator/`` itself (bare ``env_config``). Reading at
    composition time keeps the one-shot-arm splice driven entirely by
    the flag, so slice-1 needs no caller changes (dormant by design).
    """
    try:
        from orchestrator.env_config import get_event_loop_owner
    except ImportError:
        from env_config import get_event_loop_owner  # type: ignore[no-redef,import-not-found]
    return get_event_loop_owner()


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
# ``build_consensus_wrapped_command`` — the only template path
# post-slice-4 task-4-2 (the legacy capped-restart template and the
# ``EGG_BRC_EVENT_PUMP`` env-flag read were deleted in that task).
# The pump is a deterministic loop that calls ``egg-orch brc get-state``
# + ``egg-orch brc next-action`` to decide what to do next, blocks on
# ``egg-orch message wait-loop`` while emitting wrapper-owned heartbeats
# (#2036 + #2451 migrated out of the agent-side ``message_wait_loop``
# handler), and invokes the agent one-shot via ``python3 -m egg_agent``
# when an event needs handling. The wait-filter set is built
# conditionally with ``CONSENSUS_CONFIRMED`` omitted pre-confirm (the
# orchestrator rejects that combination with HTTP 400 per #2064 / #2482,
# risk_analyst R12).
#
# The idle budget (env ``EGG_BRC_IDLE_BUDGET_MIN``, default 30 min)
# replaces the legacy capped-restart cap: no actionable event for
# the budget duration raises an ``OVERSEER_ALERT``, but the loop
# keeps blocking instead of exiting 1 -> FAILED.
#
# Placeholders interpolated by ``str.format``:
#   {agent_command_prefix}   -- ``python3 -m egg_agent --model X --max-turns N``
#   {idle_budget_min_default}, {hb_interval_default}, {wait_timeout_default}
_EVENT_PUMP_WRAPPER_TEMPLATE = r"""#!/bin/bash
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

# --- Agent-invocation exit-code classifiers (#2908 task-4-2) -----------
#
# Migrated from the legacy capped-restart template. The event-pump
# invokes the agent one-shot per actionable event (``propose|ack|nack``
# arm in the loop below); a non-zero exit there is still meaningful in
# the same three ways the legacy template distinguished:
#
#   * ``is_buffer_overflow`` — the Claude Agent SDK 1 MiB JSON
#     message-reader overflow signature (issue #2804). Deterministic
#     under one-shot too: the next invocation hits the same oversized
#     tool result. The event-pump's idle-budget safety net catches this
#     eventually, but the classifier lets the operator alert sooner with
#     a more specific anomaly tag.
#   * ``is_transient_crash`` — signal-based exits (SIGABRT, SIGFPE,
#     SIGKILL/OOM, SIGSEGV, Bun segfault) where a retry is appropriate.
#   * ``is_startup_failure`` — exit 1 within the
#     ``$STARTUP_FAILURE_WINDOW_SECONDS`` window (SDK API/network blip
#     manifesting as exit 1 before the agent did meaningful work).
#
# The event-pump's ``propose|ack|nack`` arm does NOT yet branch on
# these signals (the consecutive-failure counter + idle-budget alert
# combo handles the operator-visible escalation today). Keeping them as
# named helpers preserves a clean hook for future revisions (per task-4-2
# acceptance: classifiers "are still valid signals under the new idle/
# no-progress safety budget").
STARTUP_FAILURE_WINDOW_SECONDS=30

is_buffer_overflow() {{
    [ -f "${{AGENT_OUTPUT_LOG:-}}" ] || return 1
    grep -q "exceeded maximum buffer size" "$AGENT_OUTPUT_LOG" 2>/dev/null
}}

is_transient_crash() {{
    local code="$1"
    case "$code" in
        134|136|137|139|255) return 0 ;;  # SIGABRT, SIGFPE, SIGKILL/OOM, SIGSEGV, Bun segfault
        *) return 1 ;;
    esac
}}

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

# -----------------------------------------------------------------------

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
# payload + the contract's ``task_description`` (#3123, read from the
# worktree contract file via the pod-inherited ``EGG_PIPELINE_ID`` /
# ``EGG_ISSUE_NUMBER``). The composer lives at
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
    {agent_command_prefix} "$prompt"
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

# Idle / no-progress safety budget (#2908 task-2-3). Replaces the
# legacy capped-restart cap (deleted by task-4-2): if no actionable
# event arrives for the configured idle budget we raise an
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
# Sticky latches for the agent-invocation fail streak (#3138). Like the
# ``NEXT_ACTION_ALERTED_*`` latches below they are wrapper-lifetime
# sticky (the streak counter itself resets via the arm-cluster dispatch
# at the top of the loop, but a re-fired warning after a brief recovery
# is noise, not signal). The 10-streak latch escalates to a real
# overseer alert -- a streak of fast-failing invocations is a strong
# permanent/configuration-class signal (unknown model alias, auth
# misconfiguration, prompt-rendering crash) that the operator should
# hear about from the wrapper itself, not minutes later via the
# overseer's generic heartbeat-silence anomaly.
AGENT_FAIL_ALERTED_5=false
AGENT_FAIL_ALERTED_10=false
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

raise_agent_fail_alert() {{
    local streak="$1"
    local last_rc="$2"
    local last_secs="$3"
    local action="$4"
    # Duration-aware classification (#3138): a fast failure means the
    # invocation died before/at SDK init -- unknown model alias, auth
    # misconfiguration, prompt-rendering crash -- i.e. a permanent
    # configuration-class failure, not a transient. Name that in the
    # alert so the operator doesn't have to infer it from cadence. The
    # wrapper still never self-FAILs (post-#2908 design): the kill
    # decision stays with the operator.
    #
    # The ``-le 2`` threshold (not ``< 1``) is deliberate: ``$SECONDS``
    # has whole-second granularity and ``invoke_agent_for_event`` includes
    # prompt-composer overhead, so a genuine pre-SDK-init crash routinely
    # measures 1-2s. Do not tighten this to ``< 1`` -- it would misclassify
    # those crashes as transient.
    local classification="repeated agent-invocation failure; attempts are taking ${{last_secs}}s so this may be transient (API/quota/transport)"
    if [ "$last_secs" -le 2 ]; then
        classification="attempts are fast-failing (${{last_secs}}s, before/at SDK init) -- likely a permanent configuration-class failure (unknown model alias, auth misconfiguration, prompt-rendering crash)"
    fi
    timeout 5 egg-orch overseer alert "${{EGG_PIPELINE_ID:-unknown}}" \
        --role "${{EGG_AGENT_ROLE:-agent}}" \
        --anomaly agent-invocation-fail-streak \
        --priority high \
        --summary "agent invocation failing repeatedly (action=${{action}}, streak=${{streak}})" \
        --detail "Event-pump for role=${{EGG_AGENT_ROLE:-agent}} slice=${{EGG_SLICE_ID:-none}} has had ${{streak}} consecutive agent-invocation failures on action=${{action}} (last rc=${{last_rc}}): ${{classification}}. The loop keeps retrying with linear backoff (capped 30s); no FAILED transition is forced. Idle budget continues to accrue." \
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
        if [ "$NEXT_ACTION_FAIL_STREAK" -ge {spvr_failure_streak_warn} ] && [ "$NEXT_ACTION_ALERTED_5" != "true" ]; then
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
            # The legacy template guarded this with a 3-restart cap; the
            # event-pump path's equivalent is the idle-budget safety net
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
                confirm_backoff_secs=$(( CONFIRM_FAIL_STREAK * {spvr_backoff_factor} ))
                if [ "$confirm_backoff_secs" -gt {spvr_backoff_cap} ]; then
                    confirm_backoff_secs={spvr_backoff_cap}
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
            # PR removed the legacy 3-restart cap; this rc gate is the
            # equivalent ceiling on the action path.
            cw_log "Invoking agent (action=$ACTION)."
            # R11a (sync_to_proposals docstring anchor): the ``propose`` arm
            # intentionally falls through this gate without syncing -- a
            # producer's own commits are already on HEAD in its worktree,
            # and merging peer proposal commits onto a producer turn is
            # exactly the dual-role bleed-through the sync is designed to
            # avoid. Only ``ack`` / ``nack`` (reviewer arms) sync.
            if [ "$ACTION" = "ack" ] || [ "$ACTION" = "nack" ]; then
                sync_to_proposals "$EVENT_PAYLOAD"
            fi
            agent_invoke_start=$SECONDS
            invoke_agent_for_event "$ACTION" "$EVENT_PAYLOAD"
            agent_rc=$?
            agent_invoke_secs=$(( SECONDS - agent_invoke_start ))
            if [ "$agent_rc" -eq 0 ]; then
                note_progress
                AGENT_FAIL_STREAK=0
            else
                AGENT_FAIL_STREAK=$(( AGENT_FAIL_STREAK + 1 ))
                cw_log "agent invocation failed (action=$ACTION, rc=$agent_rc, streak=$AGENT_FAIL_STREAK, duration=${{agent_invoke_secs}}s). Idle counter continues to accrue."
                if [ "$AGENT_FAIL_STREAK" -ge {spvr_failure_streak_warn} ] && [ "$AGENT_FAIL_ALERTED_5" != "true" ]; then
                    # The per-failure line above already printed the streak
                    # count this iteration; this escalation line adds the
                    # diagnosis (likely permanent), not the count.
                    cw_log "agent invocation streak crossed {spvr_failure_streak_warn} (action=$ACTION) -- this is likely a permanent failure (unknown model alias, auth misconfiguration, prompt-rendering crash), not a transient. Idle budget continues to accrue."
                    AGENT_FAIL_ALERTED_5=true
                fi
                if [ "$AGENT_FAIL_STREAK" -ge {spvr_failure_streak_alert} ] && [ "$AGENT_FAIL_ALERTED_10" != "true" ]; then
                    raise_agent_fail_alert "$AGENT_FAIL_STREAK" "$agent_rc" "$agent_invoke_secs" "$ACTION"
                    AGENT_FAIL_ALERTED_10=true
                fi
                # #3138: the old flat ``sleep 1`` here assumed agent
                # startup gives a natural seconds-to-tens-of-seconds
                # floor, but a pre-SDK-init failure (unknown model
                # alias, prompt-rendering crash) fails in <1 s and was
                # retried at the floor indefinitely (160+ consecutive
                # retries on the first issue-3077 run). Backoff parity
                # with the ``confirm`` arm above: linear in the streak,
                # capped at 30 s, so a deterministic fast-fail loop
                # stops hammering the orchestrator while a one-off
                # transient still retries promptly.
                agent_backoff_secs=$(( AGENT_FAIL_STREAK * {spvr_backoff_factor} ))
                if [ "$agent_backoff_secs" -gt {spvr_backoff_cap} ]; then
                    agent_backoff_secs={spvr_backoff_cap}
                fi
                sleep "$agent_backoff_secs"
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


# Splice anchor: the one-shot arm (#3064 slice-1) is inserted immediately
# before this marker in the ORCHESTRATOR-ownership build of the wrapper.
# Splicing (rather than a ``str.format`` placeholder) is deliberate — a
# placeholder that expands to empty in pod mode would leave a residual
# newline and break the "pod build byte-identical to main" golden-file
# test. In pod mode the template is returned untouched.
_MAIN_LOOP_MARKER = "# --- main event-pump loop ---"


# One-shot event arm (#3064 slice-1). Spliced into the wrapper ONLY when
# the orchestrator owns the event loop (``EGG_EVENT_LOOP_OWNER=
# orchestrator``); the pod-ownership build never contains it and is
# byte-identical to the pre-#3064 template. Dormant by design: nothing
# sets ``EGG_EVENT_ACTION`` until the slice-2 spawner exists.
#
# This block is spliced AFTER ``_EVENT_PUMP_WRAPPER_TEMPLATE.format(...)``
# runs, so it must use LITERAL braces (no ``{{``/``}}`` doubling) and must
# NOT be passed through ``str.format``. It relies only on helper functions
# defined above the marker (``fetch_next_action``, ``next_action_field``,
# ``sync_to_proposals``, ``invoke_agent_for_event``, ``emit_heartbeat``,
# ``cw_log``) and on the ``trap cleanup EXIT TERM INT`` already installed.
#
# Behavior (task-1-1 acceptance):
#   * Engages only when owner==orchestrator AND an event is injected
#     (``EGG_EVENT_ACTION`` non-empty). With no injected event it falls
#     through to the in-pod loop unchanged (belt-and-suspenders).
#   * ``confirm``/``complete`` are executed orchestrator-side with no pod
#     and must NEVER be injected here — loud non-zero rejection if they
#     are.
#   * Skips the blocking wait-loop and the background heartbeat entirely.
#   * Re-checks next-action ONCE as a stale-event backstop: if the derived
#     action no longer matches the injected event, exit 0 WITHOUT invoking
#     the agent (the dedupe race backstop).
#   * Otherwise invokes ``invoke_agent_for_event`` exactly once and exits
#     with the agent's (#2908-classified) rc so the slice-3 supervisor can
#     tell a clean handoff (0) from an abnormal termination.
_ONE_SHOT_ARM_TEMPLATE = r"""# --- one-shot event arm (#3064 slice-1, orchestrator ownership) ---------
#
# The orchestrator spawns this wrapper ONCE per actionable BRC event,
# injecting the event identity via env (EGG_EVENT_ACTION in
# propose|ack|nack, EGG_EVENT_DEDUPE_KEY, payload refs). In that mode we
# skip the blocking wait-loop and the background heartbeat: re-check
# next-action once as a stale-event backstop, invoke the agent exactly
# once, and exit with the agent's (#2908-classified) rc. The
# orchestrator-side supervisor (slice-3) owns respawn/backoff/alerting.
ONE_SHOT_OWNER="${EGG_EVENT_LOOP_OWNER:-pod}"
ONE_SHOT_ACTION="${EGG_EVENT_ACTION:-}"
if [ "$ONE_SHOT_OWNER" = "orchestrator" ] && [ -n "$ONE_SHOT_ACTION" ]; then
    cw_log "One-shot arm engaged (action=$ONE_SHOT_ACTION, dedupe=${EGG_EVENT_DEDUPE_KEY:-none}, role=${EGG_AGENT_ROLE:-?}, slice=${EGG_SLICE_ID:-none})."

    case "$ONE_SHOT_ACTION" in
        confirm|complete)
            # confirm/complete are agent-free and run orchestrator-side
            # (no pod is ever spawned for them). Reaching the one-shot arm
            # with one injected is a caller bug -- reject loudly so the
            # orchestrator surfaces it rather than silently invoking an
            # agent for a verb that must never invoke one.
            cw_log "FATAL: action=$ONE_SHOT_ACTION must run orchestrator-side with no pod and must never be injected into the one-shot arm. Refusing (exit 64)."
            exit 64
            ;;
        propose|ack|nack)
            : ;;
        *)
            cw_log "FATAL: unknown injected EGG_EVENT_ACTION='$ONE_SHOT_ACTION' (expected propose|ack|nack). Refusing (exit 64)."
            exit 64
            ;;
    esac

    # Single foreground liveness ping (NOT the background emitter): the
    # slice-5 silent-mid-event tripwire keys on heartbeats from an active
    # one-shot pod. This spawns no background process.
    emit_heartbeat "WORKING" "one-shot event arm action=$ONE_SHOT_ACTION"

    # Stale-event backstop: re-derive next-action ONCE. If consensus has
    # moved on (derived action != injected event) this spawn is a
    # duplicate/stale delivery -- exit 0 WITHOUT invoking the agent. The
    # slice-2 spawner dedupes on the derived event, but a race can still
    # deliver a stale one; this is the in-pod backstop for that race.
    ONE_SHOT_NEXT_JSON=$(fetch_next_action)
    ONE_SHOT_DERIVED=$(next_action_field "$ONE_SHOT_NEXT_JSON" "action")
    if [ "$ONE_SHOT_DERIVED" != "$ONE_SHOT_ACTION" ]; then
        cw_log "Injected event is stale (injected=$ONE_SHOT_ACTION, derived=${ONE_SHOT_DERIVED:-<none>}); exiting 0 without invoking the agent."
        exit 0
    fi

    # Use the freshly-derived event_payload (current truth) for the
    # invocation. Reviewer arms (ack/nack) sync the pending proposal
    # commits into the worktree first, exactly as the in-pod loop does
    # (#3076/#3077); the producer ``propose`` arm intentionally does not
    # (R11a -- a producer's own commits are already on HEAD).
    ONE_SHOT_PAYLOAD=$(next_action_field "$ONE_SHOT_NEXT_JSON" "event_payload")
    if [ "$ONE_SHOT_ACTION" = "ack" ] || [ "$ONE_SHOT_ACTION" = "nack" ]; then
        sync_to_proposals "$ONE_SHOT_PAYLOAD"
    fi

    one_shot_start=$SECONDS
    invoke_agent_for_event "$ONE_SHOT_ACTION" "$ONE_SHOT_PAYLOAD"
    one_shot_rc=$?
    one_shot_secs=$(( SECONDS - one_shot_start ))
    cw_log "One-shot invocation done (action=$ONE_SHOT_ACTION, rc=$one_shot_rc, duration=${one_shot_secs}s); exiting with the agent rc."
    # Exit with the agent's (#2908-classified) rc. The classifiers
    # (is_buffer_overflow / is_transient_crash / is_startup_failure)
    # remain defined above for a future revision that wants to remap; the
    # slice-3 orchestrator-side supervisor reads the Job exit code here.
    exit "$one_shot_rc"
fi

"""


# ``_event_pump_enabled`` (the read of ``EGG_BRC_EVENT_PUMP``) was
# deleted in slice-4 task-4-2 along with the legacy template branch in
# ``build_consensus_wrapped_command``. The env flag is now silently
# inert; operators with it lingering in k8s manifests can remove it
# without any production impact.


def build_event_pump_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 1000,
    idle_budget_min: int = EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT,
    heartbeat_interval_secs: int = EVENT_PUMP_HEARTBEAT_INTERVAL_SECS_DEFAULT,
    wait_timeout_secs: int = EVENT_PUMP_WAIT_TIMEOUT_SECS_DEFAULT,
    effort: str | None = None,
) -> list[str]:
    """Compose the event-pump wrapper bash command (#2908 task-2-1).

    Public entry-point retained so tests and
    ``build_consensus_wrapped_command`` (which now unconditionally
    delegates here post slice-4 task-4-2) share one composer.

    The ``prompt_text`` argument is kept for signature parity with
    the legacy capped-restart entry-point that task-4-2 deleted; the
    event-pump emits its own per-event prompts inside
    ``invoke_agent_for_event`` from the rendered ``compose_event_prompt``
    output (slice-3 task-3-1), so the initial prompt is not
    interpolated into the bash directly. A future revision could
    choose to pass it through as a bootstrap prompt for the first
    ``propose`` event without breaking the public signature.
    """
    del prompt_text  # interface parity with the deleted legacy entry-point

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

    script = _EVENT_PUMP_WRAPPER_TEMPLATE.format(
        agent_command_prefix=agent_command_prefix,
        idle_budget_min_default=idle_budget_min,
        hb_interval_default=heartbeat_interval_secs,
        wait_timeout_default=wait_timeout_secs,
        spvr_backoff_factor=SUPERVISION_BACKOFF_FACTOR,
        spvr_backoff_cap=SUPERVISION_BACKOFF_CAP_SECONDS,
        spvr_failure_streak_warn=SUPERVISION_FAILURE_STREAK_WARN,
        spvr_failure_streak_alert=SUPERVISION_FAILURE_STREAK_ALERT,
    )
    # The triple-quoted template opens with a newline (so the source reads
    # cleanly); strip it so ``#!/bin/bash`` lands on line 1. The script is run
    # via ``bash -c`` where the shebang is cosmetic, so pod runtime behaviour
    # is unchanged — but a leading blank line trips shellcheck SC1128 on the
    # committed golden snapshot, so we drop it at the single rendering source.
    script = script.lstrip("\n")
    # #3064 slice-1: in orchestrator-ownership mode, splice the dormant
    # one-shot arm in ahead of the main loop. In pod mode (default) the
    # template is returned untouched so the generated wrapper is
    # byte-identical to pre-#3064 (golden-file test). The arm itself only
    # engages when an event is injected (EGG_EVENT_ACTION) -- nothing sets
    # that until slice-2 -- so this is dormant even when orchestrator mode
    # is selected without the spawner.
    if _event_loop_owner() == "orchestrator":
        script = script.replace(
            _MAIN_LOOP_MARKER,
            _ONE_SHOT_ARM_TEMPLATE + _MAIN_LOOP_MARKER,
            1,
        )
    return ["bash", "-c", script]


def build_consensus_wrapped_command(
    prompt_text: str,
    model: str = "opus",
    max_turns: int = 1000,
    effort: str | None = None,
) -> list[str]:
    """Build a shell command that runs the agent under the BRC event-pump wrapper.

    Slice-4 task-4-2 collapsed this function to a thin alias for
    :func:`build_event_pump_wrapped_command`. The legacy capped-restart
    template, recovery system prompt, and ``EGG_BRC_EVENT_PUMP`` env
    flag were all deleted; the event-pump template is the only
    production path post-slice-4. The function signature is retained
    so call sites in ``concurrent_executor.py`` and ``kubernetes_spawner.py``
    do not have to be renamed in lock-step with this slice.

    Args:
        prompt_text: Initial prompt (reserved — the event-pump emits
            its own per-event prompts inside ``invoke_agent_for_event``
            from the rendered ``compose_event_prompt`` output, so the
            initial prompt is not interpolated into the bash directly.
            Accepted for interface parity with the legacy signature).
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
