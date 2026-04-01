### Task Analysis

**Problem statement**: Pipelines that achieve full BRC consensus still report as `failed` and never create PRs. Users must manually create PRs even when all agents completed their work successfully.

**System context**: Two components interact:
1. The **consensus wrapper** (`orchestrator/consensus_wrapper.py`) wraps agent execution in concurrent mode. After the agent exits cleanly (code 0), it checks if the agent reached CONFIRMED in the BRC protocol. If not, it restarts the agent up to MAX_RESTARTS times. If restarts are exhausted without detecting CONFIRMED state, it exits with code 1 (line 333).
2. The **orchestrator's consensus polling loop** (`orchestrator/routes/pipelines.py:4540+`) monitors containers and consensus state. When a container exits with code != 0, it sets `has_failures[0] = True` (line 4564) and marks the agent as FAILED (line 4588). When consensus IS detected as complete, `_update_agents_complete()` correctly re-marks agents as COMPLETE (line 4619-4620), but the function still returns 1 if `has_failures[0]` was set (line 4717-4718), causing the phase to be marked as failed.

**Technical root cause**: Two bugs compound:
1. **Consensus wrapper** (line 332-333): After exhausting restarts, exits with code 1 without a final consensus check. Due to timing, the agent may have reached CONFIRMED but the wrapper's poll didn't catch it.
2. **Orchestrator** (line 4717-4718): When `is_complete` is True (ALL agents confirmed consensus), still returns failure if any container had a non-zero exit. But `is_complete=True` means every agent successfully participated — the `has_failures` flag is stale. The comment references OOM kills, but an OOM-killed agent wouldn't confirm, so `is_complete` would be False.

**Files affected**:
- `orchestrator/consensus_wrapper.py` — Add final consensus check before exit 1 at line 331-333
- `orchestrator/routes/pipelines.py` — Remove `has_failures` check at lines 4717-4718 when consensus is complete