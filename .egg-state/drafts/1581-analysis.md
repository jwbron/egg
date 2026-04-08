### Task Analysis

**Problem statement**: A PR was opened despite the BRC Consensus Summary showing "Consensus not reached" for all phases. BRC consensus should gate phase completion, which in turn gates PR creation.

**Source context**: Issue #1581. The triggering pipeline was `issue-1526` (#1578). Related: #1572 (BRC context in PR body), #1580 (other BRC bugs).

**System context**: `_run_concurrent_phase()` (pipelines.py:4717) is the shared function that runs any BRC-enabled phase. It polls `executor.check_consensus()` in a loop (step 2), tracks container exits (step 4), and at step 5 decides the phase outcome when all containers have exited. The return value (exit code 0 or 1) determines whether the pipeline advances to the next phase — ultimately reaching the `pr` phase where `_auto_create_pr()` runs unconditionally.

**Technical root cause**: At `pipelines.py:5212-5297` (step 5, all-containers-exited), there are two branches:
- **`has_failures` is True** (line 5215): Does a final `executor.check_consensus()` recheck. Returns 0 only if `is_complete=True`, otherwise returns 1. Correct.
- **`has_failures` is False** (line 5270): Only checks for unresolved NACKs. If none, returns 0 at line 5297 **without ever verifying `consensus.is_complete`**. This is the bug — all containers exiting cleanly (exit code 0) is treated as success regardless of consensus state.

This affects every BRC-enabled phase, not just implement. Any phase where agents exit code 0 without confirming consensus will be treated as successful, allowing the pipeline to advance.

**Files affected**:
- `orchestrator/routes/pipelines.py` — Add consensus completeness check in the no-failures exit path (around line 5270-5297)
- `orchestrator/tests/test_consensus_race_on_exit.py` — Add test for clean-exit-without-consensus scenario

**Risks / edge cases**: The consensus wrapper inside containers is supposed to ensure agents confirm before exiting, but agents can exit code 0 without confirming (e.g., wrapper bug, timeout handling). The fix must treat "all exited cleanly but consensus incomplete" as failure, matching how the failure path already works.