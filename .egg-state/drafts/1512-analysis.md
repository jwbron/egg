### Task Analysis

**Problem statement**: When an agent crashes due to a transient infrastructure failure (Bun segfault, OOM kill), the consensus wrapper permanently kills the agent because all non-zero exits are treated as unrecoverable failures — even though the wrapper already has restart logic for clean (exit 0) exits.

**Source context**: Issue #1512 reports pipeline `pipeline-7a244c58` where `reviewer_contract` crashed with a Bun 1.3.11 segfault (exit code 255). The wrapper detected empty tracker state and checked the message bus, but still refused to restart because the non-zero exit handler at line 232 unconditionally exits: `"Agent failed (code $AGENT_EXIT). NOT restarting."` This caused the entire pipeline to be marked `failed` even though 4/5 agents were healthy.

**System context**: The consensus wrapper (`orchestrator/consensus_wrapper.py`) is a bash script template that wraps agent invocations in BRC (Broadcast-Review-Converge) mode. It has two exit paths:

1. **Clean exit (code 0)** — lines 236-399: checks if agent is already CONFIRMED, if not enters a restart loop up to `MAX_RESTARTS` (default 2) with full BRC recovery prompts
2. **Non-zero exit** — lines 211-234: checks consensus complete or agent confirmed, otherwise immediately fails with `NOT restarting`

The restart loop (path 1) already has all the recovery machinery: it fetches BRC state, builds recovery system prompts, handles NACK feedback, loads anchor state, and checks confirmed status after each restart. Path 2 has none of this.

**Technical root cause**: The non-zero exit handler (lines 211-234) treats all non-zero exit codes identically. After the consensus/confirmed checks fail, it exits immediately at line 232-233:

```bash
cw_log "Agent failed (code $AGENT_EXIT). NOT restarting."
exit $AGENT_EXIT
```

There is no classification of exit codes. Transient crashes (segfault=139/255, OOM=137, SIGABRT=134) are handled the same as application-level errors (exit 1). The restart loop at lines 292-381 is only reachable from the clean exit path.

**Files affected**:
- `orchestrator/consensus_wrapper.py` — Add transient exit code classification; modify non-zero exit handler to restart on transient crashes with backoff; add a `TRANSIENT_CRASH_MAX_RESTARTS` constant
- `orchestrator/tests/test_consensus_wrapper.py` — Tests for transient crash restart behavior, backoff, and the classification function

**Risks / edge cases**:
- An agent in a crash loop (repeated segfaults) must not restart forever — need a separate or shared restart cap
- Existing test `test_nonzero_exit_does_not_restart` asserts current behavior; needs updating for non-transient codes specifically
- Exit code 255 is used by Bun for segfaults but could also be a generic error in other runtimes — acceptable to treat as transient since the worst case is one extra restart attempt
- The restart loop already handles the case where a restarted agent crashes again with non-zero (lines 343-362) — the transient crash logic should feed into the same loop