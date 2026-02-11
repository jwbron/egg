# Analysis: Ensure checkpoints are captured if an agent doesn't push to github

> Issue: #530 | Phase: refine

## Problem Statement

Currently, checkpoints are **only created when a git push occurs** (see `gateway.py:747` calling `capture_and_store_checkpoints_for_push`). This means:

1. **Context after push is lost**: If an agent continues working after pushing code, that additional context is never captured
2. **Non-pushing sessions have no checkpoints**: Sessions that don't push code (e.g., review bots, planning phases, exploration) lose all their context
3. **Workflow correlation is incomplete**: Review bots are clearly associated with a PR, but their reasoning context cannot be retrieved alongside code changes

The desired outcome is to capture session context for **all** agent sessions, regardless of whether they push code, and enable correlation via issue numbers and PR numbers.

## Current Behavior

### Checkpoint Creation Flow

Checkpoints are triggered **only** in `gateway.py:747` after a successful git push:

```python
# gateway.py:745-766
if old_ref_sha:
    capture_and_store_checkpoints_for_push(
        repo_path=exec_path,
        old_sha=old_ref_sha,
        new_sha=new_sha,
        branch=branch,
        session=session,
        github_token=token_str,
        async_store=True,
    )
```

### Session Lifecycle

Sessions go through the following states (see `session_manager.py`):

| Event | Proxy Buffer | Checkpoint |
|-------|--------------|------------|
| `register_session()` | Created at `/tmp/egg-transcripts/{cid}.jsonl` | None |
| Agent works (API calls) | Growing | None |
| `git push` succeeds | Still available | **Created** |
| Agent continues working | Still growing | None |
| `delete_session()` | Cleaned up via `_cleanup_transcript_buffer()` | **MISSING** |
| `prune_expired_sessions()` | Cleaned up | **MISSING** |

### Key Code References

- Checkpoint models: `shared/egg_contracts/checkpoints.py`
- Checkpoint creation: `gateway/checkpoint_handler.py:218-391` (`capture_checkpoint()`)
- Checkpoint storage: `gateway/checkpoint_handler.py:465-601` (`store_checkpoint()`)
- Session cleanup: `session_manager.py:561-595` (`delete_session_by_container()`)
- Transcript buffer: `/tmp/egg-transcripts/{container_id}.jsonl`

### Data Model Constraint

The `Checkpoint` model requires `commit_sha` (line 146-149 in `checkpoints.py`):
```python
commit_sha: str = Field(
    ...,
    pattern=r"^[a-f0-9]{7,40}$",
    description="Git commit SHA this checkpoint is associated with",
)
```

For sessions without pushes, there is no commit SHA to associate with.

## Constraints

- **Technical**:
  - Checkpoint ID generation currently uses commit SHA (`generate_checkpoint_id_from_commit`)
  - Index lookup methods assume commit SHA exists (`get_by_commit`)
  - Proxy buffer is cleaned up when session ends - must capture before cleanup
  - Storage uses worktrees to push to `egg/checkpoints/v1` branch

- **Business**:
  - Must maintain backward compatibility with existing checkpoints
  - Should not break existing checkpoint queries
  - Review bots and non-pushing sessions need correlation to PRs/issues

- **Dependencies**:
  - Environment variables: `EGG_PR_NUMBER`, `EGG_ISSUE_NUMBER`, `EGG_PIPELINE_PHASE`
  - Session object contains: `container_id`, `agent_role`, `phase`

## Options Considered

### Option A: Session-End Checkpoint with Virtual SHA

**Approach**: Create checkpoints when sessions end (delete/expire) using a synthetic "session SHA" instead of a commit SHA.

**Implementation**:
1. Modify `Checkpoint.commit_sha` to be optional OR use a reserved prefix like `sess-{container_id[:8]}`
2. Hook into `delete_session()` and `prune_expired_sessions()` to capture checkpoint before cleanup
3. Generate session-based checkpoint ID using container_id instead of commit SHA
4. Add `get_by_pr(pr_number)` method to `CheckpointIndex`
5. Update index to support PR-based lookups

**Pros**:
- Single checkpoint per session (no duplicates)
- Captures full session context at end
- Clean integration point (session lifecycle)
- Minimal changes to existing push-based flow

**Cons**:
- Requires making `commit_sha` optional or using synthetic values
- Changes the data model semantics
- Must ensure checkpoint capture happens before buffer cleanup

### Option B: Periodic Session Checkpoints

**Approach**: Create checkpoints periodically during the session (e.g., every N minutes or every N API calls) in addition to push-based checkpoints.

**Implementation**:
1. Add background thread that periodically snapshots active sessions
2. Use timestamp-based checkpoint IDs
3. Store intermediate checkpoints alongside push checkpoints

**Pros**:
- Captures context even if session crashes unexpectedly
- Progressive context capture (don't lose everything on crash)

**Cons**:
- Many more checkpoints to store and manage
- Potential storage growth issues
- Complex cleanup logic
- More network overhead pushing to checkpoint branch
- Doesn't address the commit_sha requirement cleanly

### Option C: Dual-Path Checkpoints (Push + Session-End)

**Approach**: Keep push-based checkpoints as-is, add session-end checkpoints as a separate category.

**Implementation**:
1. Create new checkpoint type: `SessionCheckpoint` (without commit_sha requirement)
2. Store in separate index path: `checkpoints/sessions/{session_id}.json`
3. Link via `issue_number` and `pr_number` for correlation
4. Keep `Checkpoint` model unchanged for backward compatibility

**Pros**:
- No breaking changes to existing checkpoint model
- Clear separation between commit-linked and session-linked checkpoints
- Backward compatible

**Cons**:
- Two parallel systems to maintain
- Query complexity increases
- May lead to duplicate transcript data (push checkpoint + session checkpoint)

### Option D: Finalize Checkpoint on Session End

**Approach**: When a session ends, if there was a push, update the last checkpoint with final session state. If no push occurred, create a session-level checkpoint.

**Implementation**:
1. Track whether session has pushed (`session.has_pushed` flag)
2. On session end:
   - If pushed: Update last checkpoint with "finalized" timestamp and any post-push context
   - If not pushed: Create session checkpoint with `commit_sha=None` or synthetic value
3. Make `commit_sha` optional in the model
4. Add `session_id` as primary key for non-push checkpoints

**Pros**:
- Minimal duplication
- Complete context capture
- Clear semantics (finalized vs. in-progress)

**Cons**:
- Requires updating existing checkpoints
- Model change for optional commit_sha
- Tracking push state adds complexity

## Recommended Approach

**Option A: Session-End Checkpoint with Virtual SHA** is recommended for the following reasons:

1. **Simplest integration**: Single hook point at session end
2. **Complete coverage**: Captures all session types (push, no-push, review, planning)
3. **Clean correlation**: PR/issue numbers already available in environment
4. **Minimal storage overhead**: One checkpoint per session vs. many periodic ones
5. **Backward compatible queries**: Existing commit-based lookups still work; new PR/session lookups added

**Key Implementation Details**:

1. **Virtual SHA format**: Use `sess-{container_id[:32]}` for sessions without commits
2. **Checkpoint ID**: `ckpt-{hash(container_id:session_id:end_time)}`
3. **Hook location**: `session_manager.py:561` (`delete_session_by_container()`) and `session_manager.py:597` (`prune_expired_sessions()`)
4. **Capture order**: Capture checkpoint BEFORE `_cleanup_transcript_buffer()`
5. **Index additions**: Add `get_by_pr()` and `get_by_session()` methods

**Execution sequence**:
```
Session ends → capture_session_checkpoint() → _cleanup_transcript_buffer()
```

## Open Questions

The following questions require human input:

### Multiple-Choice Decision

```
egg-contract add-decision --question "How should we handle the commit_sha requirement for session checkpoints?" \
  --options "Make commit_sha optional (allow null)" "Use synthetic SHA format (sess-{id})" "Keep required, use session container_id" --format markdown
```

- [ ] Make commit_sha optional (allow null) — Cleanest semantically but requires model change and migration
- [ ] Use synthetic SHA format (sess-{id}) — No model change, clear distinction, but pollutes SHA namespace
- [ ] Keep required, use session container_id — Minimal change but loses semantic meaning
- [ ] Other (explain in reply)

### Open-Ended Questions

1. **Should we capture checkpoints for ALL sessions or only specific agent roles?** Some sessions (e.g., health checks, brief tool calls) may not warrant checkpoint storage.

2. **What is the expected checkpoint storage growth with this change?** If every session gets a checkpoint regardless of duration, storage may grow significantly.

3. **Should session checkpoints include the "post-push" context only, or the entire session transcript?** For sessions that did push, we already have commit-linked checkpoints - should the session-end checkpoint be incremental or complete?

---

*Authored-by: egg*
