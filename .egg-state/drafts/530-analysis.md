# Analysis: Checkpoint v2 with Rich Querying (#530)

## Problem Statement

Checkpoints currently capture agent session context only when agents push
commits to GitHub. This creates two gaps:

1. **Post-push context loss.** Agent reasoning after the final push is never
   captured. The agent may do significant work (review analysis, documentation
   decisions, error diagnosis) between its last push and session termination.

2. **No-push sessions produce zero checkpoints.** Sessions without code changes
   — review bots, research tasks, refine-phase analysis, sessions that error
   before completion — generate no checkpoints at all. This is a total blind
   spot.

Both gaps mean that valuable agent reasoning is silently discarded. For a system
designed to learn from and audit agent behavior, this is a fundamental
limitation.

## Current Architecture

### Checkpoint Data Flow

```
API Call → TranscriptBuffer (/tmp/egg-transcripts/{container_id}.jsonl)
    ↓
On git push → CheckpointHandler.capture_checkpoint()
    ↓
TranscriptExtractor (reads proxy buffer, extracts messages/tools/tokens)
    ↓
Redactor (sanitizes sensitive data)
    ↓
Checkpoint object (v1 schema)
    ↓
Store in egg/checkpoints/v1 orphan branch
    ↓
Update index.json for lookups
```

### Key Components

| Component | Location | Role |
|-----------|----------|------|
| Checkpoint models | `shared/egg_contracts/checkpoints.py` | Schema: `Checkpoint`, `CheckpointSummary`, `CheckpointIndex` |
| Checkpoint loader | `shared/egg_contracts/checkpoint_loader.py` | Load/save with atomic writes, index management |
| Checkpoint handler | `gateway/checkpoint_handler.py` | Capture from buffer, redact, store in git |
| Transcript buffer | `gateway/transcript_buffer.py` | JSONL proxy buffer at `/tmp/egg-transcripts/` |
| Transcript extractor | `shared/egg_contracts/transcript_extractor.py` | Parse buffer into structured data |
| Session manager | `gateway/session_manager.py` | Session lifecycle, cleanup paths |
| Worktree manager | `gateway/worktree_manager.py` | Orphaned worktree cleanup |

### v1 Schema Limitations

The current `Checkpoint` model requires `commit_sha` (non-optional string with
pattern validation `^[a-f0-9]{7,40}$`). This makes it structurally impossible
to create a checkpoint for a session that never pushed a commit. Additionally:

- No `trigger_type` field — cannot distinguish commit-triggered from
  session-end checkpoints.
- No `session_status` field — cannot record whether a session completed
  normally, expired, or crashed.
- `agent_role` is a free-form string on `SessionMetadata`, not a validated
  enum — inconsistent classification.
- The index (`CheckpointIndex`) supports lookup by commit, issue, and branch,
  but not by session, PR, agent type, pipeline phase, or session status.

### Session Deletion Paths (Where Checkpoints Must Be Captured)

Four code paths terminate sessions. Today, none produce checkpoints:

1. **`delete_session(token)`** — Graceful deletion by token. Removes session
   from memory, saves to disk. Does NOT call `_cleanup_transcript_buffer`.

2. **`delete_session_by_container(container_id)`** — Graceful deletion by
   container. Calls `_cleanup_transcript_buffer(container_id)` which deletes
   the transcript buffer. This is the primary risk: the buffer is destroyed
   before any checkpoint can be captured.

3. **`prune_expired_sessions()`** — TTL expiration. Iterates expired sessions,
   calls `_cleanup_transcript_buffer` for each. Same buffer destruction risk.

4. **`cleanup_orphaned_worktrees(active_containers)`** — Crash detection.
   Called on gateway startup and periodically. Removes worktrees for containers
   no longer running. Does NOT currently interact with session manager or
   checkpoints at all.

### Session Metadata Gap

The `Session` dataclass (`session_manager.py:95`) stores `agent_role` and
`phase` but does NOT store `issue_number` or `pr_number`. These values come
from container environment variables (`EGG_ISSUE_NUMBER`, `EGG_PR_NUMBER`)
which are only accessible at push time via the checkpoint handler reading
process environment. For session-end checkpoints triggered by the gateway
(which runs in a different process than the container), these environment
variables are not available.

## Constraints and Dependencies

### Hard Constraints

- **Transcript buffer lifetime.** The buffer at `/tmp/egg-transcripts/{id}.jsonl`
  is deleted by `_cleanup_transcript_buffer()` during session deletion. Any
  checkpoint capture must happen BEFORE this cleanup, or the buffer must be
  preserved until capture completes.

- **Async storage model.** Checkpoint storage (git operations) runs in
  background threads. Buffer cleanup must wait for capture to finish, but
  storage can remain async.

- **Gateway-container process boundary.** Container environment variables
  (`EGG_ISSUE_NUMBER`, etc.) are not directly accessible from the gateway
  process. Metadata must be propagated through session registration or
  extracted from the transcript.

- **Git worktree operations are blocked.** The gateway cannot use
  `git worktree add/remove` — these are blocked by the gateway sidecar. The
  existing `store_checkpoint()` method works around this with temporary
  worktrees created via raw git commands with hooks disabled.

- **Orphan branch isolation.** Checkpoints live in a dedicated orphan branch
  (`egg/checkpoints/v1`), not in the main source tree. v2 will use
  `egg/checkpoints/v2`.

### Soft Constraints

- **Index size.** The index is a single JSON file. With multi-dimensional
  secondary indices, it will grow faster. For the expected volume (~hundreds of
  checkpoints), this is manageable. At thousands+, sharding may be needed.

- **Backward compatibility.** The issue explicitly calls for a breaking change
  (v2). No migration of v1 data is required — v1 remains in place and v2
  starts fresh on a separate branch.

- **Transcript size limits.** Current limits are conservative
  (`max_content_length=10000`, `MAX_TRANSCRIPT_SIZE=1MB`). The issue proposes
  increasing these for session-end checkpoints which may contain full session
  transcripts.

### Existing Enums to Reuse

The codebase already has well-defined enums that the v2 schema should align
with rather than duplicate:

- `AgentRole` (`shared/egg_contracts/agent_roles.py`): `coder`, `tester`,
  `documenter`, `integrator`
- `AgentStatus` (same file): `pending`, `running`, `complete`, `failed`,
  `skipped`, `blocked`
- `PipelinePhase` (`shared/egg_contracts/models.py`): `refine`, `plan`,
  `implement`, `pr`

The issue proposes a new `AgentType` enum with a `reviewer` and `unknown`
value. The existing `AgentRole` does not include `reviewer`. The v2 schema
should extend or align with `AgentRole` rather than creating a parallel enum.

## Implementation Approaches

### Approach A: Full v2 Schema + Session-End Capture (Recommended)

Implement the complete v2 system as described in the issue: new schema with
`trigger_type`, `session_status`, optional `commit_sha`, multi-dimensional
index, and session-end checkpoint capture integrated into all deletion paths.

**Schema changes:**
- Add `TriggerType` (`commit`, `session_end`) and `SessionStatus` (`completed`,
  `expired`, `failed`) enums to `checkpoints.py`.
- Add `AgentType` enum that extends `AgentRole` with `reviewer` and `unknown`.
- Create `CheckpointV2`, `CheckpointSummaryV2`, `CheckpointIndexV2` models
  with optional `commit_sha`, required `trigger_type`, required `session_id`,
  and multi-dimensional secondary indices.

**Capture changes:**
- Add `capture_session_end_checkpoint()` to `checkpoint_handler.py`.
- Add `store_checkpoint_v2()` for the `egg/checkpoints/v2` branch.
- Update `capture_checkpoint()` to produce v2 format for push-triggered
  checkpoints.

**Integration changes:**
- Store `issue_number` and `pr_number` on the `Session` dataclass during
  registration so they are available at session-end.
- In `session_manager.py`, call checkpoint capture before
  `_cleanup_transcript_buffer()` in `delete_session_by_container()` and
  `prune_expired_sessions()`.
- In `worktree_manager.py`, call checkpoint capture with `status=FAILED` in
  `cleanup_orphaned_worktrees()`.
- Implement async capture with buffer preservation: delay buffer deletion
  until capture completes or a 30-second timeout.

**Transcript limit increases:**
- Increase `max_content_length` from 10,000 to 25,000.
- Increase `max_param_length` from 1,000 to 2,500.
- Increase `max_result_length` from 500 to 1,500.
- Increase `MAX_TRANSCRIPT_SIZE` from 1MB to 3MB.

**Pros:**
- Addresses every requirement in the issue.
- Multi-dimensional index enables the query patterns described.
- Clean v2 branch avoids migration complexity.
- Aligns with the approved plan from the previous iteration of this issue.

**Cons:**
- Large surface area — touches 5+ files with schema, handler, and integration
  changes.
- Multi-dimensional index adds storage overhead (denormalized secondary
  indices).
- 30-second buffer preservation timeout adds complexity to session cleanup.

### Approach B: Minimal Session-End Capture with v1 Schema Extension

Keep the v1 schema but make `commit_sha` optional, add a `trigger_type` field,
and hook session-end capture into deletion paths. Skip the multi-dimensional
index.

**Pros:**
- Smaller change set.
- No new branch or index format.
- Addresses the core problem (no-push sessions missing checkpoints).

**Cons:**
- Does not deliver the rich querying capability requested.
- Extends v1 in a backward-incompatible way without the clean break that a v2
  branch provides.
- The issue explicitly requests a breaking v2 change — this approach contradicts
  that direction.
- Misses the opportunity to add session/PR/status-based indexing.

### Approach C: Event-Sourced Checkpoint Store

Replace the git-branch-based checkpoint store with an event log (e.g.,
PostgreSQL or a local SQLite database) for richer querying. Keep git storage
as a secondary archive.

**Pros:**
- SQL-based querying is far more flexible than JSON index files.
- No index bloat concerns.
- Better suited for the query patterns described.

**Cons:**
- Major architectural change beyond the scope of the issue.
- Introduces a new persistence dependency.
- Git-based storage has operational advantages (versioned, portable, no
  database dependency).
- Significantly more work than the issue envisions.

## Recommendation

**Approach A** is the clear choice. It matches the refined proposal in the
issue, aligns with the previously approved plan, and delivers both the
session-end capture and the rich querying capabilities. The issue was
explicitly scoped as a breaking v2 change, and the implementation has already
been planned and approved once.

### Key Design Decisions

1. **Session metadata propagation (Open Question 1):** Store `issue_number`
   and `pr_number` on the `Session` dataclass during registration
   (Option A from the issue). This is the cleanest approach — the data is
   available when the container is created and can be passed through session
   registration. Parsing from transcript content is fragile and incomplete.

2. **Checkpoint timing for session end (Open Question 2):** Async capture
   with buffer preservation and 30-second timeout (Option B from the issue).
   Synchronous capture risks blocking session cleanup. Fire-and-forget risks
   losing checkpoints. The middle ground — start async capture, delay buffer
   deletion until complete or timeout — balances reliability with
   responsiveness.

3. **Crash checkpoint completeness (Open Question 3):** Both — add
   `truncated_reason` to the transcript AND mark `session_status=FAILED`
   (Option C from the issue). The status enum tells you the session crashed;
   the truncation reason tells you the transcript may be incomplete. These are
   complementary signals.

4. **AgentType vs AgentRole alignment:** The v2 `AgentType` enum should
   include `reviewer` and `unknown` beyond the existing `AgentRole` values.
   Consider whether to extend `AgentRole` itself or keep `AgentType` as a
   superset enum in the checkpoint schema. Since `AgentRole` is used for
   file access control and agent execution, adding `reviewer` and `unknown`
   there may have unintended side effects. A separate `AgentType` enum in
   `checkpoints.py` is safer.

## Files to Modify

| File | Changes |
|------|---------|
| `shared/egg_contracts/checkpoints.py` | Add `TriggerType`, `SessionStatus`, `AgentType` enums; add `CheckpointV2`, `CheckpointSummaryV2`, `CheckpointIndexV2` models |
| `shared/egg_contracts/checkpoint_loader.py` | Add v2 save/load functions; v2 index update with secondary indices |
| `gateway/checkpoint_handler.py` | Add `capture_session_end_checkpoint()`; add `store_checkpoint_v2()`; update `capture_checkpoint()` to v2; change branch to `egg/checkpoints/v2` |
| `gateway/session_manager.py` | Add `issue_number`/`pr_number` to `Session`; call checkpoint capture in deletion paths; refactor `_cleanup_transcript_buffer()` to await capture |
| `gateway/worktree_manager.py` | Call `capture_session_end_checkpoint()` in `cleanup_orphaned_worktrees()` with `status=FAILED` |
| `shared/egg_contracts/transcript_extractor.py` | Increase `max_content_length`, `max_param_length`, `max_result_length` |

## Testing Strategy

1. Unit tests for v2 models (serialization, validation, optional `commit_sha`).
2. Unit tests for v2 index secondary indices (by_session, by_issue, by_pr,
   by_status, by_agent_type, by_phase, by_trigger).
3. Unit tests for `capture_session_end_checkpoint()` — graceful, expired, and
   crash scenarios.
4. Integration tests for session deletion paths producing checkpoints.
5. Integration tests for async buffer preservation with timeout.
6. Verify push-triggered checkpoints produce v2 format.
7. Verify existing test suite passes (no regressions in gateway functionality).

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Buffer deleted before capture completes | Medium | High (lost checkpoint) | 30s timeout with buffer preservation |
| Index file grows too large | Low | Medium (slow reads) | Monitor size; shard later if needed |
| Session metadata not available for orphan cleanup | Medium | Low (partial checkpoint) | Accept partial data for crash checkpoints; `truncated_reason` field communicates this |
| Concurrent checkpoint writes corrupt index | Low | Medium (lost index entry) | Existing atomic write pattern (temp → fsync → rename) prevents corruption |
| Transcript size increase causes storage pressure | Low | Low | 3MB cap is still bounded; git compression helps |
