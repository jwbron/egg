### Task Analysis

**Problem statement**: Pipelines that achieve full BRC consensus still report as `failed` and never create PRs.

**Technical root cause**: Two bugs compound:
1. **Consensus wrapper** (line 332-333): After exhausting restarts, exits with code 1 without a final consensus check.
2. **Orchestrator** (line 4717-4718): When `is_complete` is True, still returns failure if any container had a non-zero exit.

**Files affected**:
- `orchestrator/consensus_wrapper.py` — Add final consensus check before exit 1
- `orchestrator/routes/pipelines.py` — Remove `has_failures` check when consensus is complete