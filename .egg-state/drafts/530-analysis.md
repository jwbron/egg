# Analysis: Checkpoint v2 with Rich Querying

> Issue: #530 | Phase: refine

## Problem Statement

The current v1 checkpoint system only captures session context when agents push commits to GitHub. This creates significant gaps in session traceability:

1. **Post-push context loss**: Any agent reasoning, tool calls, or decisions made after the final push are never captured
2. **No-push sessions generate zero checkpoints**: Sessions that don't produce code changes (review bots, research tasks, sessions that error before completion) leave no trace

The goal is to build a v2 checkpoint system that captures every session regardless of push activity, enables rich querying across multiple dimensions, and provides clear semantics distinguishing commit-linked vs session-end checkpoints.

## Current Behavior

The v1 checkpoint system (`gateway/checkpoint_handler.py:capture_and_store_checkpoints_for_push`) creates checkpoints only on successful git push operations:

1. **Trigger point**: `gateway/gateway.py:857-876` - checkpoint creation fires after successful push
2. **Per-commit granularity**: One checkpoint per commit in a multi-commit push, all sharing the same transcript
3. **Storage**: Orphan branch `egg/checkpoints/v1` with index at root and checkpoints sharded by ID prefix
4. **Async storage**: Push doesn't block on checkpoint storage (background thread)

Key components:
- `shared/egg_contracts/checkpoints.py`: Defines `Checkpoint`, `CheckpointSummary`, `CheckpointIndex` models
- `shared/egg_contracts/checkpoint_loader.py`: Atomic save/load with deterministic ID generation
- `gateway/checkpoint_handler.py`: `CheckpointHandler` class with `capture_checkpoint()` and `store_checkpoint()`
- `gateway/session_manager.py`: Session lifecycle with 24-hour TTL, `delete_session()` calls `_cleanup_transcript_buffer()`
- `gateway/worktree_manager.py`: `cleanup_orphaned_worktrees()` removes worktrees for inactive containers

Current deletion paths where checkpoints could be captured but aren't:
1. `SessionManager.delete_session()` - graceful deletion
2. `SessionManager.delete_session_by_container()` - container-based deletion
3. `SessionManager.prune_expired_sessions()` - TTL expiration
4. `WorktreeManager.cleanup_orphaned_worktrees()` - orphan detection on gateway startup

## Constraints

**Technical constraints**:
- Checkpoint storage must remain atomic (temp file + rename pattern)
- Transcript buffer cleanup happens immediately after session deletion - checkpoint capture must occur first
- `cleanup_orphaned_worktrees()` receives only a set of active container IDs, not full session objects
- Gateway may crash between session deletion and checkpoint storage (async risk)
- Index file is updated on every checkpoint write (concurrent write concern for high-volume scenarios)

**Schema constraints**:
- v2 schema must be distinct from v1 (`egg/checkpoints/v2` branch)
- No migration required - v1 and v2 can coexist during transition
- `commit_sha` must become optional for session-end checkpoints

**Operational constraints**:
- Checkpoint capture should not significantly delay session cleanup
- Failed/crashed sessions may have incomplete transcripts
- Session metadata (issue_number, pr_number) must be available at capture time

## Options Considered

### Option A: Store Session Metadata at Registration

**Approach**: Capture issue/PR numbers during session registration (`register_session()`) and store them in the Session object. This ensures metadata is always available for session-end checkpoints.

**Pros**:
- Guarantees metadata availability at any capture point
- Simple lookup - metadata already in Session object
- Works for all deletion paths (graceful, TTL, crash)
- No need to parse transcripts or environment variables at capture time

**Cons**:
- Requires API change to `register_session()` to accept issue/PR numbers
- Existing sessions without metadata will have null linkage
- Slight increase in Session object size

### Option B: Parse Metadata from Transcript Buffer

**Approach**: Extract issue/PR numbers from transcript content at checkpoint capture time by analyzing tool calls or message content.

**Pros**:
- No Session model changes required
- Works with existing session registration flow
- Can potentially extract richer context

**Cons**:
- Unreliable - depends on transcript format and content
- Performance overhead parsing potentially large transcripts
- May fail for sessions with truncated or missing transcripts
- Crashed sessions likely have incomplete data

### Option C: Accept Partial Linkage

**Approach**: Session-end checkpoints may lack issue/PR linkage. Use commit-linked checkpoints for workflow correlation and accept that session-end checkpoints provide agent activity without full context.

**Pros**:
- No changes to session registration
- Simplest implementation
- Session-end checkpoints still capture agent activity

**Cons**:
- Cannot query session-end checkpoints by issue/PR
- Workflow correlation incomplete for no-push sessions
- Defeats purpose of rich querying for failed/research sessions

### Option D: Hybrid - Registration with Environment Fallback

**Approach**: Store metadata at registration when available, fall back to environment variables (`EGG_ISSUE_NUMBER`, `EGG_PR_NUMBER`) at capture time if Session lacks it.

**Pros**:
- Best of both worlds - uses available data
- Backward compatible with existing sessions
- Graceful degradation for edge cases

**Cons**:
- More complex implementation
- Environment may not be accessible for orphan cleanup path
- Two code paths to maintain

---

### Timing Option 1: Synchronous Capture

**Approach**: Block session deletion until checkpoint is fully captured and stored.

**Pros**:
- Guarantees checkpoint capture before cleanup
- Simple control flow
- No race conditions

**Cons**:
- Increases deletion latency (checkpoint storage involves git operations)
- If storage fails, deletion may be blocked or need retry logic
- Gateway restart during storage leaves session in inconsistent state

### Timing Option 2: Async with Buffer Preservation

**Approach**: Start async checkpoint capture, delay transcript buffer deletion until capture completes.

**Pros**:
- Non-blocking for API response
- Transcript data guaranteed available during capture
- Can retry on failure

**Cons**:
- More complex lifecycle management
- Need coordination mechanism (semaphore/event)
- Buffer cleanup timing becomes dependent on async task

### Timing Option 3: Fire-and-Forget

**Approach**: Start async capture and immediately proceed with cleanup.

**Pros**:
- Simplest implementation
- No latency impact
- Matches current async pattern for push checkpoints

**Cons**:
- Race condition: buffer may be deleted before capture reads it
- Gateway crash loses checkpoint
- No guarantee of capture success

---

### Crash Handling Option 1: Status Only

**Approach**: Mark crashed sessions with `session_status=FAILED` only.

**Pros**:
- Simple - status field conveys the information
- No additional fields needed

**Cons**:
- No indication of transcript completeness

### Crash Handling Option 2: Truncation Reason Field

**Approach**: Add `truncated_reason` field indicating why transcript may be incomplete.

**Pros**:
- Explicit about data quality
- Can differentiate crash truncation from size truncation

**Cons**:
- Overlaps with existing `Transcript.truncated` and `truncation_reason`
- May be redundant with status

### Crash Handling Option 3: Both Status and Truncation

**Approach**: Use `session_status=FAILED` and add `truncated_reason="container_crash"` when applicable.

**Pros**:
- Maximum clarity for consumers
- Status indicates terminal state, truncation indicates data quality
- Supports analytics on crash rates and data completeness

**Cons**:
- Slightly redundant information

## Recommended Approach

**Session Metadata**: **Option D (Hybrid - Registration with Environment Fallback)**

Rationale: This provides the best data quality while maintaining backward compatibility. The registration path should be updated to accept optional `issue_number` and `pr_number` parameters, storing them in the Session object. At checkpoint capture time, use Session metadata if available, otherwise fall back to environment variables. This handles:
- New sessions: metadata stored at registration
- Existing sessions: environment fallback works
- Orphan cleanup: may lack metadata, but checkpoint still captured

**Checkpoint Timing**: **Option 2 (Async with Buffer Preservation)**

Rationale: This balances performance with reliability. The transcript buffer is the critical data source - we must not delete it before checkpoint capture reads it. Implementation:
1. Session deletion initiates async checkpoint capture
2. Buffer deletion is deferred until capture signals completion
3. Capture timeout (30s) ensures cleanup isn't blocked indefinitely
4. On timeout, proceed with cleanup and log warning

This matches the issue proposal's recommendation and provides strong guarantees without blocking the deletion API.

**Crash Handling**: **Option 3 (Both Status and Truncation)**

Rationale: The `session_status=FAILED` clearly indicates the session didn't complete normally, while `truncated_reason="container_crash"` (or similar) on the Transcript provides explicit data quality information. Consumers can:
- Query failed sessions via `by_status` index
- Understand data completeness from transcript metadata
- Distinguish crash truncation from size-based truncation

## Implementation Summary

Based on the analysis, the implementation should:

1. **Add v2 models** to `shared/egg_contracts/checkpoints.py`:
   - `TriggerType`, `SessionStatus`, `AgentType` enums
   - `CheckpointV2` with optional `commit_sha`, required `trigger_type` and `session_id`
   - `CheckpointSummaryV2` and `CheckpointIndexV2` with secondary indices

2. **Add v2 loader functions** to `shared/egg_contracts/checkpoint_loader.py`:
   - `save_checkpoint_v2()`, `load_checkpoint_v2()`
   - Multi-index update logic for `by_session`, `by_issue`, `by_pr`, etc.

3. **Extend Session model** in `gateway/session_manager.py`:
   - Add optional `issue_number` and `pr_number` fields
   - Update `register_session()` to accept these parameters

4. **Add session-end capture** to `gateway/checkpoint_handler.py`:
   - `capture_session_end_checkpoint()` function
   - Use v2 branch `egg/checkpoints/v2`
   - Async capture with buffer preservation

5. **Hook deletion paths** in `gateway/session_manager.py`:
   - `delete_session()`: capture with `status=COMPLETED`
   - `prune_expired_sessions()`: capture with `status=EXPIRED`

6. **Hook orphan cleanup** in `gateway/worktree_manager.py`:
   - `cleanup_orphaned_worktrees()`: capture with `status=FAILED`
   - Challenge: need session lookup by container_id

## Open Questions

### HITL Decision Required: Checkpoint Capture Timeout

When using async capture with buffer preservation, how long should we wait before timing out and proceeding with cleanup?

- [ ] **15 seconds** - Aggressive timeout, minimizes deletion latency
- [ ] **30 seconds** - Balanced approach (recommended)
- [ ] **60 seconds** - Conservative, maximizes capture success
- [ ] **No timeout** - Always wait for completion (may block indefinitely)
- [ ] Other (explain in reply)

### HITL Decision Required: v1 Deprecation Strategy

How should we handle the transition from v1 to v2 checkpoints?

- [ ] **Write to both v1 and v2** during transition, deprecate v1 after 30 days
- [ ] **Write only to v2 immediately** (breaking change, but cleaner)
- [ ] **Feature flag** to switch between v1 and v2 per-deployment
- [ ] Other (explain in reply)

### HITL Decision Required: Index Update Strategy for High Volume

The current index is updated on every checkpoint write. With session-end checkpoints, volume increases. How should we handle this?

- [ ] **Keep current approach** - index updated per checkpoint (simple, may have contention)
- [ ] **Batch index updates** - update index every N checkpoints or every M seconds
- [ ] **Eventual consistency** - separate index rebuild job, tolerate stale index briefly
- [ ] Other (explain in reply)

### Open-Ended Questions

1. **Retention policy**: Should v2 checkpoints have a TTL or retention policy, or should they accumulate indefinitely like v1?

2. **Orphan session lookup**: `cleanup_orphaned_worktrees()` receives only container IDs. To capture checkpoints for crashed sessions, we need to look up Session by container_id. The current `get_session_by_container()` method exists but returns None for already-deleted sessions. Should we add a separate lookup before worktree cleanup, or restructure the cleanup flow?

3. **Transcript size limits**: Should session-end checkpoints have different size limits than commit checkpoints, given they represent potentially longer sessions?

---

*Authored-by: egg*
