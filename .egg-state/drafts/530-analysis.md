# Analysis: Checkpoint v2 — Capture All Sessions with Rich Querying (#530)

## Problem Statement

The v1 checkpoint system only captures session context when agents push commits
to GitHub. This creates two gaps in session traceability:

1. **Post-push context loss.** Agent reasoning, tool calls, and decisions made
   after the final push are never captured. The checkpoint reflects the session
   state at push time, not at session end.

2. **No-push sessions have zero checkpoints.** Sessions that don't produce code
   changes — review bots, research tasks, documentation-only work, sessions
   that error before completion — leave no trace in the checkpoint system.

The result: there is no way to query what an agent did during sessions that
didn't push, and even for sessions that did push, the final reasoning context
is lost.

## Current Architecture

### Checkpoint Models (`shared/egg_contracts/checkpoints.py`)

The v1 schema defines:

- **`Checkpoint`** (line 131): Full session context tied to a commit. `commit_sha`
  is *required* — every checkpoint must reference a commit. Key fields: `id`,
  `commit_sha`, `session` (SessionMetadata), `transcript`, `files_touched`,
  `tool_calls`, `token_usage`, `issue_number`, `pr_number`, `pipeline_phase`,
  `branch`, `push_sha`, `created_at`.

- **`SessionMetadata`** (line 39): Session info including `session_id`,
  `container_id`, `agent_role`, `started_at`. Also defines `ended_at` and
  `duration_seconds` fields — but these are **never populated** in v1.

- **`CheckpointSummary`** (line 215): Lightweight index entry. Also requires
  `commit_sha`.

- **`CheckpointIndex`** (line 252): Root-level index with flat list of
  summaries and three lookup methods: `get_by_commit()`, `get_by_issue()`,
  `get_by_branch()`. All lookups are O(n) linear scans.

### Checkpoint Capture (`gateway/checkpoint_handler.py`)

- **Single trigger point**: Checkpoints are only created on successful git push
  (`capture_and_store_checkpoints_for_push`, line 747).

- **Per-commit granularity**: One checkpoint per commit in multi-commit pushes,
  all sharing the same transcript buffer content.

- **Transcript source**: API proxy buffer at `/tmp/egg-transcripts/{container_id}.jsonl`,
  written by `transcript_buffer.py` during Anthropic API proxying.

- **Metadata resolution**: `issue_number`, `pr_number`, and `pipeline_phase`
  are resolved from environment variables (`EGG_ISSUE_NUMBER`, `EGG_PR_NUMBER`,
  `EGG_PIPELINE_PHASE`) at capture time (lines 330-349).

- **Storage**: Checkpoints are stored on orphan branch `egg/checkpoints/v1` via
  a temporary git worktree. Each checkpoint is JSON in a sharded directory
  (`checkpoints/{2-char prefix}/ckpt-{id}.json`), plus an `index.json` at root.
  Storage is async (background thread) to avoid blocking the push response.

- **ID generation**: `generate_checkpoint_id_from_commit()` uses SHA-256 of
  `{commit_sha}:{session_id}:{timestamp}` — deterministic but requires a
  commit SHA.

### Session Lifecycle (`gateway/session_manager.py`)

The `Session` dataclass (line 94) tracks: `session_token`, `session_token_hash`,
`container_id`, `container_ip`, `mode`, `created_at`, `last_seen`, `expires_at`,
`agent_role`, `phase`. Notably, it does **not** store `issue_number` or
`pr_number`.

Four deletion paths exist where session-end checkpoints should be captured:

1. **`delete_session(token)`** (line 597): Deletes by token. Does *not* call
   `_cleanup_transcript_buffer()` or capture any checkpoint.

2. **`delete_session_by_container(container_id)`** (line 629): Deletes by
   container ID. Calls `_cleanup_transcript_buffer()` which destroys the
   transcript data. No checkpoint captured.

3. **`prune_expired_sessions()`** (line 665): Removes all expired sessions
   (24-hour TTL). Calls `_cleanup_transcript_buffer()` per session. No
   checkpoint captured.

4. **`cleanup_orphaned_worktrees(active_containers)`** in `worktree_manager.py`
   (line 658): Removes worktrees for crashed containers. Receives only a set
   of active container IDs — has no access to Session objects or transcript
   buffers.

### Transcript Size Limits (`shared/egg_contracts/transcript_extractor.py`)

Size limits are defined as function parameter defaults:
- `max_content_length = 10,000` chars (message content)
- `max_param_length = 1,000` chars (tool parameters)
- `max_result_length = 500` chars (tool result summaries)
- `MAX_TRANSCRIPT_SIZE = 1,000,000` bytes (1MB, in `checkpoint_handler.py:104`)

## Constraints

**Schema constraints:**
- v2 schema must live on a separate branch (`egg/checkpoints/v2`) — no
  migration needed, v1 remains untouched
- `commit_sha` must become optional for session-end checkpoints
- v2 models must coexist with v1 models in the same Python module

**Session metadata gap:**
- `Session` does not store `issue_number` or `pr_number`
- Environment variables (`EGG_ISSUE_NUMBER`, etc.) are set in the agent
  container, not the gateway process — they may not be accessible when
  capturing session-end checkpoints from the gateway side
- Orphan cleanup has even less context: only container IDs, no Session objects

**Transcript buffer lifecycle:**
- Buffer is destroyed by `_cleanup_transcript_buffer()` during session deletion
- Session-end checkpoint capture must read the buffer *before* cleanup
- For crashed containers, the buffer may be incomplete or already cleaned up

**Concurrency:**
- Index file is updated per checkpoint write (atomic temp+rename pattern)
- With session-end checkpoints increasing volume, concurrent writes become
  more likely but remain safe due to single-threaded index updates within
  each `store_checkpoint` call

**Performance:**
- Checkpoint storage involves git operations (worktree create, commit, push,
  worktree remove) — adds latency to session deletion if synchronous
- Current push checkpoints use async storage (background thread) to avoid
  blocking

## Implementation Approaches

### Session Metadata Propagation

#### Option A: Store Metadata at Registration (Recommended)

Add optional `issue_number` and `pr_number` fields to the `Session` dataclass.
Populate them during `register_session()`. At checkpoint capture time, read
from the Session object. Fall back to environment variables if Session lacks
the fields (backward compatibility with existing sessions).

**Pros:** Reliable metadata at all capture points. Simple lookup. Works for
all deletion paths including orphan cleanup (if Session is retrieved before
deletion).

**Cons:** Requires `register_session()` API change. Existing sessions lack
these fields until re-registered.

#### Option B: Parse from Transcript Buffer

Extract issue/PR numbers from transcript content at capture time.

**Pros:** No Session model changes.

**Cons:** Unreliable — depends on transcript format. Fails for crashed/empty
sessions. Performance overhead for large transcripts.

#### Option C: Accept Partial Linkage

Session-end checkpoints may lack issue/PR linkage. Rely on commit-linked
checkpoints for workflow correlation.

**Pros:** Simplest implementation.

**Cons:** Defeats the purpose of rich querying for no-push sessions. Cannot
query failed/research sessions by issue.

### Checkpoint Timing

#### Option A: Synchronous Capture

Block session deletion until checkpoint is captured and stored.

**Pros:** Guarantees capture. Simple control flow.

**Cons:** Adds git operation latency (~5-15s) to every session deletion.
Storage failure blocks deletion.

#### Option B: Async with Buffer Preservation (Recommended)

Start async checkpoint capture. Defer transcript buffer cleanup until capture
signals completion or times out (30 seconds).

**Pros:** Non-blocking for the API caller. Buffer guaranteed available during
capture. Graceful degradation on timeout.

**Cons:** More complex lifecycle coordination. Need event/semaphore mechanism.

#### Option C: Fire-and-Forget

Start async capture, immediately proceed with cleanup.

**Pros:** Simplest. No latency impact.

**Cons:** Race condition — buffer may be deleted before capture reads it.
No guarantee of success.

### Crash Checkpoint Completeness

#### Option A: Status Field Only

Mark crashed sessions with `session_status=FAILED`.

**Pros:** Simple. Status conveys the key information.

**Cons:** No indication of transcript completeness.

#### Option B: Both Status and Truncation Reason (Recommended)

Use `session_status=FAILED` and set `truncated_reason="container_crash"` on the
Transcript when applicable.

**Pros:** Maximum clarity. Status indicates terminal state, truncation
indicates data quality. Supports analytics on crash rates vs data completeness.
Leverages existing `Transcript.truncated` and `truncation_reason` fields.

**Cons:** Slightly redundant — but serves different query patterns.

### Index Strategy

#### Option A: Per-Checkpoint Index Update (Recommended)

Keep the current approach: update the index atomically on every checkpoint write.
Add secondary index dictionaries for O(1) lookups.

**Pros:** Strong consistency. Simple mental model. Current volume is low enough
that contention is not a concern.

**Cons:** May need revisiting at high scale.

#### Option B: Batched/Eventual Consistency

Buffer index updates and flush periodically.

**Pros:** Reduced write contention.

**Cons:** Stale index between flushes. More complex. Premature optimization
for current scale.

## Recommendation

**Session metadata**: Option A — store at registration with environment
variable fallback. This provides reliable linkage for all capture paths.

**Checkpoint timing**: Option B — async with 30-second buffer preservation
timeout. Balances reliability with performance.

**Crash handling**: Option B — both status and truncation reason. Maximizes
clarity for downstream consumers.

**Index strategy**: Option A — per-checkpoint updates with secondary indices.
Current scale does not warrant batching complexity.

The overall approach is a breaking change (v2 branch, no dual-write). The v1
branch remains readable but no new checkpoints are written to it. This matches
the owner's stated preference for switching straight to v2.

## Files to Modify

| File | Changes |
|------|---------|
| `shared/egg_contracts/checkpoints.py` | Add `TriggerType`, `SessionStatus`, `AgentType` enums. Add `CheckpointV2`, `CheckpointSummaryV2`, `CheckpointIndexV2` models with optional `commit_sha`, required `trigger_type`/`session_id`, secondary index dicts |
| `shared/egg_contracts/checkpoint_loader.py` | Add `generate_checkpoint_id_v2()` (no commit required), `save_checkpoint_v2()`, `load_checkpoint_v2()`, `load_checkpoint_index_v2()`, `add_checkpoint_to_index_v2()` with multi-index updates |
| `gateway/session_manager.py` | Add `issue_number`/`pr_number` to `Session` dataclass and persistence. Update `register_session()`. Refactor `_cleanup_transcript_buffer()` to await async capture. Hook `delete_session()`, `delete_session_by_container()`, `prune_expired_sessions()` to capture session-end checkpoints |
| `gateway/checkpoint_handler.py` | Add `capture_session_end_checkpoint()`. Add `store_checkpoint_v2()` for `egg/checkpoints/v2` branch. Update `capture_checkpoint()` to produce v2 format. Update `CHECKPOINT_BRANCH` constant. Increase `MAX_TRANSCRIPT_SIZE` to 3MB |
| `gateway/worktree_manager.py` | Add session lookup before `cleanup_orphaned_worktrees()`. Capture `FAILED` checkpoints for orphaned containers |
| `shared/egg_contracts/transcript_extractor.py` | Increase `max_content_length` to 25,000, `max_param_length` to 2,500, `max_result_length` to 1,500 |

## Testing Strategy

**Unit tests:**
- v2 model validation (CheckpointV2 with/without commit_sha, TriggerType/SessionStatus enums)
- v2 loader functions (save/load, index updates with secondary indices)
- Session metadata persistence with new fields, backward compatibility

**Integration tests:**
- Session deletion triggers checkpoint capture with `COMPLETED` status
- TTL expiration triggers checkpoint with `EXPIRED` status
- Orphan cleanup triggers checkpoint with `FAILED` status and truncation reason
- Push triggers v2 checkpoint with `COMMIT` trigger type
- Multi-index queries return correct results across all dimensions
- Async capture with 30s timeout: buffer preserved until capture completes
- Buffer cleanup blocked until async capture finishes or times out

**Existing test commands:**
- `PYTHONPATH=shared pytest shared/egg_contracts/tests/`
- `PYTHONPATH=gateway:shared pytest gateway/tests/`

## Open Questions

### 1. Orphan Session Lookup

`cleanup_orphaned_worktrees()` receives only container IDs, not Session objects.
To capture checkpoints for crashed sessions, we need to look up Session metadata
by container_id. The `get_session_by_container()` method exists but returns
`None` for already-deleted sessions. The recommended approach is to capture
checkpoint *before* session deletion in each path, ensuring the Session object
is still available. For orphan cleanup specifically, the worktree manager should
be given a reference to the session manager to look up sessions before cleanup.

### 2. Retention Policy

No TTL for v2 checkpoints initially. Pruning can be added later if storage
grows. The git branch format makes it easy to rewrite history or drop old
checkpoints.

### 3. Transcript Size for Session-End Checkpoints

Session-end checkpoints may represent longer sessions than commit checkpoints.
The proposed increase from 1MB to 3MB for `MAX_TRANSCRIPT_SIZE` and 2.5x
increases to content/parameter/result limits should accommodate this.
