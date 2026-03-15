# Analysis: SDLC Pipeline Opening PRs Early

> Issue: #518 | Phase: refine

## Problem Statement

The SDLC pipeline is creating PRs before the check/fix/review cycle completes. In the referenced run ([#21890469219](https://github.com/jwbron/egg/actions/runs/21890469219)), PR #517 was created during the implement phase while checks were still running and before the review cycle completed. The run subsequently failed due to lint and test failures, but the PR had already been opened.

**Current state**: PRs are created during the work phase by the agent, bypassing phase restrictions.

**Desired outcome**: PRs should only be created after all checks pass and the review cycle completes successfully.

## Current Behavior

### Timeline from the Failing Run

1. **02:30:59Z** - Work phase started (agent execution)
2. **02:52:38Z** - PR #517 created by `james-in-a-box[bot]` (during work phase)
3. **02:53:12Z** - Work phase completed
4. **02:53:24Z - 02:56:07Z** - Check jobs ran (lint, test - several failed)
5. **02:56:25Z** - Review jobs **skipped** due to check failures
6. **02:56:26Z** - Human gate jobs **skipped**

The PR was created ~21 minutes into the work phase, before checks ran. When checks failed, the review and human-gate stages were skipped, leaving an open PR for incomplete work.

### Root Cause: Phase Restrictions Not Enforced for PR Creation

The phase-permissions.json (`.egg/phase-permissions.json:101-107`) explicitly blocks PR creation during implement phase:

```json
"implement": {
  "blocked_operations": [
    {
      "type": "gh",
      "pattern": "pr create*",
      "description": "Cannot create PRs until implementation is complete"
    }
  ]
}
```

However, the gateway's `gh_pr_create` endpoint (`gateway/gateway.py:1181-1327`) only checks:
1. Private repo mode policy (line 1226)
2. User vs bot auth mode (line 1253)

**It does not call `phase_filter.filter_operation()` to enforce phase restrictions.**

The agent prompt at `action/build-sdlc-prompt.sh:682` correctly tells the agent:
```
- You CANNOT create PRs (the pipeline manages the PR)
```

But this is just a prompt instruction - the agent can still call `gh pr create` and the gateway allows it because phase filtering is not implemented in that endpoint.

## Constraints

- **Backward compatibility**: The `human-gate-pr` job (lines 1966-2102 of sdlc-work-loop.yml) legitimately creates PRs when the pipeline advances to that stage. Any fix must not break this flow.
- **Session context**: The gateway needs access to the current pipeline phase from session metadata.
- **Prompt-only enforcement is insufficient**: Agents may not always follow prompt instructions, especially under complex scenarios.

## Options Considered

### Option A: Add Phase Filtering to Gateway PR Create Endpoint

**Approach**: Modify `gateway/gateway.py:gh_pr_create()` to call `phase_filter.filter_operation()` before allowing PR creation. Block PR creation when phase is `refine`, `plan`, or `implement`.

**Implementation**:
```python
# In gh_pr_create(), after auth mode check:
from .phase_filter import filter_operation, OperationType

# Get current phase from session
phase = getattr(g, "session_phase", None)
if phase:
    result = filter_operation(phase, OperationType.GH, "pr create")
    if not result.allowed:
        return make_error(result.message, status_code=403)
```

**Pros**:
- Enforces phase restrictions at the gateway level (defense in depth)
- Uses existing phase_filter infrastructure
- Clear audit trail in gateway logs
- Agent cannot bypass via prompt manipulation

**Cons**:
- Requires session to carry phase metadata (may need session_manager changes)
- Need to ensure pipeline's own PR creation (human-gate-pr) bypasses this check

### Option B: Remove Agent's Ability to Call gh pr create

**Approach**: Remove the gateway endpoint entirely for agent sessions. Only allow PR creation from workflow context (GitHub Actions).

**Pros**:
- Simpler - no phase checking needed
- Eliminates the problem at the source

**Cons**:
- May break legitimate use cases where agent needs to create PRs in pr phase
- Reduces flexibility for future workflows
- Requires different auth handling for workflows vs agents

### Option C: Workflow-Level Gate - Create PR Only After All Checks Pass

**Approach**: Modify the sdlc-work-loop.yml to ensure PR creation only happens in the `human-gate-pr` job (after checks and review pass). Rely solely on prompt to prevent agent from creating PRs during work phase.

**Pros**:
- No gateway changes required
- Follows current architecture intent

**Cons**:
- Agents can still create PRs early if they ignore prompt instructions
- No enforcement mechanism - relies on agent compliance
- This is effectively the current approach, which failed

### Option D: Hybrid - Phase Filtering + Workflow Context Flag

**Approach**:
1. Add phase filtering to gateway (Option A)
2. Add a `workflow_context` flag that the pipeline can set to bypass phase restrictions when legitimately creating PRs

**Pros**:
- Strong enforcement for agent sessions
- Pipeline can still create PRs when appropriate
- Defense in depth

**Cons**:
- More complex implementation
- Need to securely pass workflow context flag

## Recommended Approach

**Option A: Add Phase Filtering to Gateway PR Create Endpoint**

This is the recommended approach because:

1. **Defense in depth**: The gateway already enforces other policies (merge blocking, branch ownership). Phase restrictions should be enforced at the same layer.

2. **Uses existing infrastructure**: The `phase_filter.py` module already has the logic; it just needs to be called.

3. **Minimal changes**: Only requires modifying `gh_pr_create()` to check phase restrictions.

4. **The pipeline's PR creation is not affected**: The `human-gate-pr` job creates PRs via `gh pr create` directly, not through the gateway's API endpoint. It runs on GitHub Actions with its own token, bypassing the gateway entirely.

### Implementation Notes

The gateway session already has `session_mode` (bot/user). We need to add `session_phase` to the session metadata:

1. **Session manager** (`gateway/session_manager.py`): Add `phase` field to session state
2. **Phase API** (`gateway/phase_api.py`): Provide endpoint to update session phase
3. **Gateway PR create** (`gateway/gateway.py:gh_pr_create`): Check phase restrictions before allowing

The agent container startup should initialize the session with the current phase from environment variables (already passed as `EGG_PIPELINE_PHASE`).

## Open Questions

1. **Should the pipeline have a way to update the session phase as it progresses?** Currently the phase is set at container startup. If a session spans multiple phases, we may need a mechanism to update it.

2. **What should happen to existing sessions that don't have phase metadata?** Recommend: allow by default for backward compatibility, but log a warning.

---

*Authored-by: egg*
