## Task Analysis

**Problem statement**: Pipeline `issue-1558` reached full BRC consensus (all 5 agents sent CONSENSUS_CONFIRMED) but still failed — all containers exited with code 1 within seconds of each other, and no PR was created despite `auto_create_pr: true`.

**Source context**: Triggered by overseer restart (#1562) which caused coder withdrawal at 15:31:37, invalidating near-complete consensus (4/5 confirmed). The re-proposal cascade added ~8 min of churn. Overseer also sent misleading CRITICAL alerts (#1526). Work was recovered manually via draft PR #1563.

**System context**: During concurrent (BRC) execution, `_run_concurrent_phase` in `pipelines.py` polls in a loop: (1) check consensus via `executor.check_consensus()`, (2) if complete → stop containers, return 0, (3) check for container exits, (4) if ALL containers exited with failures → return 1 immediately. The consensus wrapper in each container independently checks consensus/confirmation state before allowing non-zero exits to propagate.

**Technical root cause**: There's a race condition in `_run_concurrent_phase` at line 4896-4900. When all containers exit with non-zero codes, step 5 returns `exit_code=1` **without rechecking consensus**. The consensus check at step 2 (line 4711) runs earlier in the same poll iteration, but if the tracker state is stale (due to the withdrawal/re-proposal cascade corrupting the in-memory tracker), it may miss that consensus is actually complete. The message bus fallback in `check_consensus()` (line 406-432, added for #1471) should catch this, but there's no second chance — step 5 returns immediately.

Additionally, the consensus wrapper's `check_agent_confirmed_with_fallback` only falls back to the message bus when the tracker agents map is **empty**. After a withdrawal/re-proposal cascade, the tracker is populated but stale — the fallback never triggers, so agents exit without recognizing their own CONFIRMED status.

**Files affected**:
- `orchestrator/routes/pipelines.py:4896-4900` — Add final consensus check before returning failure when all containers exited
- `orchestrator/consensus_wrapper.py:148-186` — Improve `check_agent_confirmed_with_fallback` to also check message bus when tracker shows agent NOT confirmed (not just when tracker is empty)
- `orchestrator/tests/test_consensus_complete_with_failures.py` — Add test for the race condition
- Tests for improved wrapper fallback logic

**Risks / edge cases**:
- The extra consensus check in step 5 adds one more API call when all containers fail, but this is a terminal path so the cost is negligible
- The wrapper's expanded fallback (`--limit 1000` message poll) is expensive but only runs on agent exit, which is a one-time cost
- Must preserve the existing behavior where genuine failures (no consensus messages at all) still propagate correctly