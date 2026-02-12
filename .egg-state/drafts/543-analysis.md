# Analysis: Local-mode pipelines missing contracts, checkpoints, and tracking

> Issue: #543 | Phase: refine

## Problem Statement

Local-mode and issue-mode pipelines have diverged in behavior beyond what's necessary. The two modes should be unified so both follow the same contract/checkpoint/push discipline, with the only real difference being where initial context comes from (GitHub issue vs user prompt).

**Current state**: Local-mode pipelines:
- Block all git push operations except in PR phase
- Do not commit pipeline state changes to git
- Use simple file paths without prefixes (e.g., `.egg-state/drafts/analysis.md`)
- Omit `egg-contract` CLI instructions from agent prompts
- Do not produce checkpoints
- Do not populate `PhaseState.containers` and `PhaseState.agents` fields

**Desired outcome**: Both modes operate identically except for initial context source, with:
- Phase-based (not mode-based) file push restrictions
- Pipeline state committed to git in both modes
- Prefixed file paths for concurrent pipeline support
- `egg-contract` CLI usage in both modes
- Checkpoint production in both modes
- Container/agent tracking populated during execution

## Current Behavior

### 1. Git Push Restrictions (`gateway/gateway.py:521-533`)

Push is blocked globally for local-mode sessions:

```python
if session_mode == "local":
    return make_error(
        "Operation blocked in local SDLC mode. Push manually when the pipeline completes.",
        status_code=403,
        details={"session_mode": "local"},
    )
```

The only exception is PR phase, where `_run_pipeline()` overrides the gateway mode:
```python
if current_phase.value == "pr" and pipeline_mode == "local":
    phase_gateway_mode = "public"
```

### 2. State Persistence (`orchestrator/state_store.py:222-226`)

Local pipeline state is not committed to git:

```python
is_local = getattr(pipeline, "mode", "issue") == "local"
if commit and not is_local:
    self._commit_state(pipeline, message)
```

### 3. File Path Prefixing (`orchestrator/routes/pipelines.py:757-773`)

Local mode uses unprefixed paths, issue mode uses issue number prefixes:

```python
def _get_draft_path(phase: str, pipeline_mode: str, issue_number: int | None = None) -> str | None:
    is_local = pipeline_mode == "local"
    if is_local:
        if phase == "refine":
            return ".egg-state/drafts/analysis.md"  # No prefix
        # ...
    else:
        if phase == "refine":
            return f".egg-state/drafts/{issue_number}-analysis.md"  # Issue number prefix
```

### 4. Contract CLI Instructions (`orchestrator/routes/pipelines.py:1066-1076`)

Contract CLI instructions are only included for issue-mode:

```python
if not is_local:
    lines.extend([
        "Use the contract CLI to track progress:",
        "- `egg-contract show` — View current contract state",
        "- `egg-contract add-commit --task <id> --commit <sha>` — Link commit to task",
    ])
```

### 5. Checkpoints (`gateway/checkpoint_handler.py`)

Checkpoints are captured after successful git push operations via `capture_and_store_checkpoints_for_push()`. Since local-mode blocks all pushes (except PR phase), no checkpoints are produced during refine, plan, or implement phases.

### 6. Container/Agent Tracking (`orchestrator/routes/pipelines.py:1206-1261`)

Container tracking is partially implemented in `_spawn_and_wait()`:

```python
if store is not None:
    try:
        pipeline = store.load_pipeline(pipeline_id)
        phase_execution = pipeline.get_phase_execution(PipelinePhase(phase))
        container_info = ContainerInfo(...)
        phase_execution.containers.append(container_info)
        store.save_pipeline(pipeline)
```

However, the `store` parameter is passed explicitly and tracking depends on the caller. Agent tracking (`PhaseExecution.agents`) is not populated at all during pipeline execution.

## Constraints

### Technical Constraints
- **Gateway architecture**: Push restrictions are enforced at the session level in the gateway. Changing to phase-based restrictions requires passing phase information to the gateway session.
- **Checkpoint dependency on push**: Checkpoints are triggered by successful push operations. Enabling checkpoints for local mode requires allowing pushes (to specific paths) during all phases.
- **Concurrent pipelines**: Multiple pipelines may run in the same repo simultaneously. File paths must include unique identifiers (issue number or pipeline ID) to prevent conflicts.
- **Git worktree isolation**: The orchestrator already runs in an isolated worktree. State commits in local mode would affect the worktree's history.

### Compatibility Constraints
- **Existing local pipelines**: Any pipelines already created in local mode will have unprefixed paths. Migration or graceful fallback may be needed.
- **Issue-mode behavior**: Changes must not regress issue-mode functionality, especially the checkpoint flow that's critical for traceability.

### Security Constraints
- **File-level restrictions by phase**: Each phase should only be able to modify specific file paths. The implement phase should not modify `.egg-state/` (except checkpoints) to prevent contract tampering.

## Options Considered

### Option A: Phase-based Push Restrictions via Gateway Session Enhancement

**Approach**: Extend the gateway session registration to include the current phase. The gateway enforces file-level restrictions based on phase rather than blanket blocking for local mode.

1. Modify `gateway_client.register_session()` to accept `phase` parameter (already exists)
2. Add a phase-permissions configuration file defining allowed paths per phase
3. Update `gateway.py` push handler to check file paths against phase permissions
4. Remove the blanket `session_mode == "local"` block

**Phase permissions example**:
```json
{
  "refine": {
    "allowed": [".egg-state/contracts/*", ".egg-state/drafts/*analysis*", ".egg-state/checkpoints/*"]
  },
  "plan": {
    "allowed": [".egg-state/contracts/*", ".egg-state/drafts/*plan*", ".egg-state/checkpoints/*"]
  },
  "implement": {
    "allowed": ["*"],
    "blocked": [".egg-state/contracts/*", ".egg-state/drafts/*"]
  },
  "pr": {
    "allowed": ["*"]
  }
}
```

**Pros**:
- Clean separation of concerns (gateway enforces file restrictions)
- Phase information already partially exists in gateway sessions
- Enables checkpoints by allowing pushes of checkpoint files

**Cons**:
- Requires gateway changes in addition to orchestrator changes
- Need to handle edge cases (force push, merge commits)
- Phase transitions during a push could create race conditions

### Option B: Orchestrator-managed Push with Post-Commit Checkpoints

**Approach**: Keep push blocked for agents, but have the orchestrator push on behalf of agents after each phase completes. Checkpoints are captured by the orchestrator after it pushes.

1. Agent commits changes locally but cannot push
2. After agent container exits, orchestrator reads the local commits
3. Orchestrator pushes commits and captures checkpoints
4. All push operations go through the orchestrator, not the agent

**Pros**:
- Simpler security model (agents never push directly)
- Checkpoints can be captured with full orchestrator context
- No gateway changes needed for phase-based restrictions

**Cons**:
- Breaks the "push before handoff" principle
- Increases orchestrator complexity
- Agents cannot push mid-phase (e.g., push analysis draft before starting review)
- Different from issue-mode where agents push directly

### Option C: Hybrid Approach with Gateway Filter Enhancement

**Approach**: Use the existing `PhaseFilter` module in the gateway (already exists at `gateway/phase_filter.py`) to enforce file-level restrictions. Extend it to support both role-based and phase-based filtering.

1. Extend `phase-permissions.json` to include phase-based file restrictions
2. Modify the gateway push handler to check both role and phase restrictions
3. Update orchestrator to pass phase in gateway session (already done)
4. Remove the blanket local-mode push block
5. Use the existing `PhaseFilter.check_file_modification()` pattern

**Pros**:
- Builds on existing infrastructure (PhaseFilter already exists)
- Minimal new code needed
- Consistent with existing role-based file restriction pattern
- Phase is already passed in session registration

**Cons**:
- PhaseFilter currently focuses on role-based restrictions, not phase-based
- Need to ensure phase is reliably available in session context

## Recommended Approach

**Option C: Hybrid Approach with Gateway Filter Enhancement**

This approach is recommended because:

1. **Builds on existing infrastructure**: The `PhaseFilter` module and `phase-permissions.json` already exist and are used for role-based file restrictions. Extending them for phase-based restrictions is a natural evolution.

2. **Minimal gateway changes**: The phase is already passed to the gateway during session registration (`container_spawner.py:212`). The main change is adding phase-based rules to the existing permission check.

3. **Consistent security model**: Using the same permission framework for both role and phase restrictions provides a single source of truth for file access control.

4. **Enables checkpoints naturally**: Once pushes are allowed (with file restrictions), checkpoints will be captured automatically via the existing `capture_and_store_checkpoints_for_push()` flow.

### Implementation Plan

#### Phase 1: Unify Git Push Restrictions

1. Add phase-based rules to `phase-permissions.json`:
   - `refine`: Allow `.egg-state/contracts/*`, `.egg-state/drafts/*analysis*`, `.egg-state/checkpoints/*`
   - `plan`: Allow `.egg-state/contracts/*`, `.egg-state/drafts/*plan*`, `.egg-state/checkpoints/*`
   - `implement`: Allow everything except `.egg-state/` (but include `.egg-state/checkpoints/*`)
   - `pr`: Allow everything

2. Extend `PhaseFilter.check_file_modification()` to accept phase context and apply phase-based restrictions.

3. Remove the blanket local-mode push block in `gateway.py:521-533`.

4. Update the gateway push handler to check phase restrictions for all files in the push.

#### Phase 2: Unify State Persistence

1. Modify `StateStore.save_pipeline()` to commit state changes for local pipelines:
   - Remove the `is_local` check that skips commits
   - Use pipeline ID in commit messages for both modes

2. Ensure the orchestrator pushes pipeline state after each phase transition.

#### Phase 3: Unify File Paths

1. Update `_get_draft_path()` and `_verdict_path_for_type()` to use pipeline ID as prefix for local mode:
   - Local mode: `.egg-state/drafts/{pipeline_id}-analysis.md`
   - Issue mode: `.egg-state/drafts/{issue_number}-analysis.md`

2. Add backward compatibility: Check for unprefixed paths if prefixed path doesn't exist.

#### Phase 4: Unify Contract Usage

1. Update `_build_phase_prompt()` to include `egg-contract` CLI instructions for both modes.

2. Ensure `create_local_contract()` sets `contract_synced = False` initially.

3. Verify plan-to-contract population works for both modes.

#### Phase 5: Enable Checkpoints for Local Mode

1. With phase-based push restrictions in place, checkpoints will be captured automatically.

2. Ensure checkpoint branch pushes are allowed regardless of mode/phase.

#### Phase 6: Container and Agent Tracking

1. Ensure `_spawn_and_wait()` always receives the `store` parameter.

2. Add agent tracking: When spawning multi-agent containers, populate `PhaseExecution.agents` with `AgentExecution` records.

3. Update container status on exit (already partially implemented).

#### Phase 7: Integration Tests

1. Test local pipeline with push restrictions per phase.
2. Test checkpoint creation for local pipelines.
3. Test contract CLI usage in local mode.
4. Test concurrent pipelines with prefixed paths.
5. Test container/agent tracking visibility via API.

## Open Questions

### HITL Decision Required

```
egg-contract add-decision --question "How should we handle the repo path resolution difference between local and issue modes?" \
  --options "Unify to always use base EGG_REPO_PATH" "Keep separate resolution for multi-repo support" "Investigate further before deciding" --format markdown
```

**Context**: Issue mode uses `_resolve_pipeline()` which scans repo subdirectories to find pipelines. Local mode always uses the base `EGG_REPO_PATH`. The issue description flags this as needing investigation.

### Open-Ended Questions

1. **Migration strategy**: Should we migrate existing local pipelines to use prefixed paths, or maintain backward compatibility by checking both paths?

2. **Checkpoint branch permissions**: Should checkpoint pushes to `egg/checkpoints/v1` bypass all phase restrictions, or should they be explicitly allowed in each phase?

3. **State persistence frequency**: Should local pipelines commit state on every save (matching issue mode), or only at phase boundaries to reduce commit noise?

---

*Authored-by: egg*
