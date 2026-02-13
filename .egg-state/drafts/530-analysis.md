# Analysis: Checkpoint V2 with Rich Querying (#530)

> Issue: #530 | Phase: refine

## Problem Statement

Checkpoints currently capture session context only when agents push commits to
GitHub. This creates two gaps:

1. **Post-push context loss**: Agent reasoning that occurs after the final push
   is never captured. The session may continue for significant work (writing
   analysis, responding to reviews, debugging) that produces no checkpoint.

2. **No-push sessions have zero checkpoints**: Sessions without code changes
   generate nothing. This includes review bots, research tasks (refine phase
   analysis), sessions that error before producing commits, and documentation
   agents that don't push. These sessions are invisible to the checkpoint system
   despite containing valuable reasoning context.

Additionally, the v1 index structure supports only basic lookups (by commit,
issue, branch) via linear scans of the checkpoint list. As checkpoint volume
grows, queries like "all checkpoints for PR #42" or "all failed sessions"
require scanning every entry.

## Current Behavior

### Checkpoint Triggering

Checkpoints are triggered exclusively by `gateway.py` after a successful
`git push`. The flow is:

1. `gateway.py` forwards push to GitHub
2. On success, calls `capture_and_store_checkpoints_for_push()`
   (`checkpoint_handler.py:747`)
3. Enumerates commits via `get_commits_in_push()` — one checkpoint per commit
4. Each checkpoint captures the full transcript from the API proxy buffer at
   `/tmp/egg-transcripts/{container_id}.jsonl`
5. Checkpoints are stored asynchronously in `egg/checkpoints/v1` branch

### Data Model (v1)

The `Checkpoint` model (`shared/egg_contracts/checkpoints.py:131`) requires
`commit_sha` as a mandatory field with pattern validation
(`^[a-f0-9]{7,40}$`). This means it is structurally impossible to create a
checkpoint without an associated commit.

The `CheckpointIndex` (`checkpoints.py:252`) stores a flat list of
`CheckpointSummary` objects with three lookup methods:
- `get_by_commit()` — linear scan, O(n)
- `get_by_issue()` — linear scan, O(n)
- `get_by_branch()` — linear scan, O(n)

No lookup by session, PR, agent type, or status exists.

### Session Lifecycle & Cleanup

Sessions end through three paths, all in `session_manager.py`:

| Path | Method | When |
|------|--------|------|
| Graceful deletion | `delete_session_by_container()` (:629) | Orchestrator calls DELETE when agent completes |
| TTL expiration | `prune_expired_sessions()` (:665) | Periodic cleanup (every 15 min) and startup |
| Container crash | `cleanup_orphaned_worktrees()` in `worktree_manager.py` (:658) | Gateway startup — orphan detection |

All three paths clean up the transcript buffer via
`_cleanup_transcript_buffer()`. Critically, none of them capture a checkpoint
before doing so — the transcript data is destroyed without being preserved.

### Transcript Buffer Availability

The transcript buffer (`/tmp/egg-transcripts/{container_id}.jsonl`) remains
available until `_cleanup_transcript_buffer()` is called. For graceful
deletions, the buffer is intact at cleanup time. For TTL expirations, the buffer
may still exist if the container process ended but the file was not yet removed.
For crashes, the buffer file persists on the gateway filesystem (it's written by
the gateway's proxy, not the container).

## Constraints

- **Backward compatibility**: The issue explicitly requests a breaking change
  (v2), so v1 compatibility is not required. However, v1 checkpoints should
  remain accessible in their existing branch.
- **No migration needed**: v2 checkpoints go to `egg/checkpoints/v2` branch;
  v1 data stays in `egg/checkpoints/v1`.
- **Checkpoint storage must not block operations**: Checkpoint failures must
  never block pushes, session deletions, or cleanup. The existing graceful
  degradation pattern must be preserved.
- **Transcript buffer lifecycle**: Session-end checkpoints must capture the
  transcript buffer *before* `_cleanup_transcript_buffer()` destroys it.
- **Thread safety**: The session manager uses `RLock` for all operations.
  Checkpoint capture called within deletion paths must not deadlock.
- **Git worktree usage**: `store_checkpoint()` creates temporary git worktrees
  for writing to the checkpoint branch. This works for async storage but may
  have latency implications if called synchronously in deletion paths.
- **Metadata propagation**: Session-end checkpoints need issue/PR numbers.
  These currently come from environment variables (`EGG_ISSUE_NUMBER`,
  `EGG_PR_NUMBER`, `EGG_PIPELINE_PHASE`) which are set in the agent container,
  not the gateway. The gateway's `Session` dataclass has `agent_role` and
  `phase` but not issue/PR numbers.
- **Index size**: With session-end checkpoints, checkpoint volume will roughly
  double. The multi-dimensional index adds secondary lookup dicts but keeps the
  primary list for ordered iteration.

## Options Considered

### Option A: Additive V2 — New Models, New Branch, Capture at All Session-End Points

**Approach**: Implement the full v2 schema from the issue proposal. Add new
enums (`TriggerType`, `SessionStatus`, `AgentType`), a new `CheckpointV2` model
with optional `commit_sha`, and a `CheckpointIndexV2` with multi-dimensional
secondary indices. Hook into all three session-end paths to capture
`SESSION_END` checkpoints. Store in `egg/checkpoints/v2` branch.

**Pros**:
- Directly implements the refined proposal from the issue
- Clean break from v1 — no hybrid models
- Multi-dimensional index enables all query patterns from the issue
- `TriggerType` enum makes the checkpoint's origin explicit
- `SessionStatus` enum distinguishes completed/expired/failed sessions

**Cons**:
- Larger surface area — new models, new enums, new index structure, new
  capture points in three different code paths
- Session metadata propagation requires storing issue/PR numbers during
  session registration (currently not done)
- Index file grows with secondary dicts; concurrent writes need careful
  handling

### Option B: Minimal V2 — Session-End Capture Only, Reuse V1 Models

**Approach**: Make `commit_sha` optional in the existing `Checkpoint` model
(bump schema to 2.0). Add session-end capture hooks but keep the existing
index structure. Store in the same `egg/checkpoints/v1` branch.

**Pros**:
- Smaller change — fewer new files and models
- Reuses existing storage and index code
- Faster to implement

**Cons**:
- Doesn't deliver on the "rich querying" goal
- Mixing v1 and v2 semantics in one model creates confusion
- Index lookups remain O(n) linear scans
- No `TriggerType` or `SessionStatus` — consumers must infer checkpoint type
  from the presence/absence of `commit_sha`
- Doesn't match the approved issue proposal

### Option C: V2 Models with Deferred Index — Build Models Now, Add Indices Later

**Approach**: Implement `CheckpointV2` and new enums, add session-end capture,
but keep a simple flat index (like v1) initially. Add multi-dimensional
secondary indices as a follow-up.

**Pros**:
- Smaller initial change than Option A
- Gets session-end capture working quickly
- Models are forward-compatible with future index work

**Cons**:
- Splits the work into two PRs, increasing review overhead
- Consumers who want rich querying must wait for the follow-up
- The issue proposal was approved as a single unit of work

## Recommended Approach

**Option A** is recommended. It directly implements the approved proposal from
the issue, which was explicitly refined to be a breaking v2 change with rich
querying. The scope is well-defined in the issue and a plan was already approved
by the human reviewer.

Key implementation decisions for Option A:

### 1. Session Metadata Propagation

Store `issue_number`, `pr_number`, and `pipeline_phase` on the `Session`
dataclass during session registration. The orchestrator already passes `phase`
via `update_phase()`. Extending `register_session()` or adding an
`update_metadata()` method to accept issue/PR numbers is straightforward. This
is **Option A** from the issue's open question #1.

### 2. Checkpoint Timing for Graceful Session End

Use **synchronous capture with async storage** (a hybrid of the issue's options
A and B). Capture the checkpoint data (transcript extraction, model creation)
synchronously before `_cleanup_transcript_buffer()` runs, ensuring the
transcript buffer is still available. Then hand off storage (git worktree
creation, commit, push) to a background thread, matching the existing async
pattern used for push-triggered checkpoints.

This avoids the risk of losing data (fire-and-forget) without blocking the
deletion response on git push latency.

### 3. Crash Checkpoint Completeness

Use **both** `session_status=FAILED` and a `truncated_reason` field on the
transcript (Option C from the issue). The `SessionStatus.FAILED` enables
querying, while `truncation_reason="container_crash"` on the transcript signals
to consumers that the data may be incomplete.

### 4. Worktree Manager Integration

The `cleanup_orphaned_worktrees()` path in `worktree_manager.py` does not
currently have access to session metadata (it only knows `container_id`). For
crash checkpoints, we need to:
- Look up the session by `container_id` before it's deleted
- Extract whatever metadata is available (agent_role, phase, issue_number)
- Create a `SESSION_END` checkpoint with `status=FAILED`
- Then proceed with worktree cleanup

This requires the worktree manager to call into the checkpoint handler, which
is a new dependency. Alternatively, the gateway startup path could capture
crash checkpoints before calling `cleanup_orphaned_worktrees()`, keeping the
dependency direction cleaner.

## Files to Modify

| File | Changes |
|------|---------|
| `shared/egg_contracts/checkpoints.py` | Add `TriggerType`, `SessionStatus`, `AgentType` enums. Add `CheckpointV2`, `CheckpointSummaryV2`, `CheckpointIndexV2` models. Keep v1 models intact. |
| `shared/egg_contracts/checkpoint_loader.py` | Add v2 save/load functions, v2 index management with multi-dimensional secondary indices, v2 path helpers. Keep v1 functions intact. |
| `gateway/checkpoint_handler.py` | Add `capture_session_end_checkpoint()`. Update `capture_checkpoint()` to produce v2 models. Change `CHECKPOINT_BRANCH` to `egg/checkpoints/v2`. Update `store_checkpoint()` for v2 index. |
| `gateway/session_manager.py` | Add `issue_number` and `pr_number` fields to `Session`. Call `capture_session_end_checkpoint()` before `_cleanup_transcript_buffer()` in `delete_session_by_container()` and `prune_expired_sessions()`. Add `update_metadata()` method. |
| `gateway/worktree_manager.py` | Call `capture_session_end_checkpoint()` with `status=FAILED` in `cleanup_orphaned_worktrees()` before removing worktrees. |
| `tests/shared/egg_contracts/test_checkpoints.py` | Add tests for v2 models, enums, and index queries. |
| `gateway/tests/test_checkpoint_handler.py` | Add tests for session-end checkpoint flow (graceful, expired, crashed). Update existing push checkpoint tests for v2 models. |

## Testing Strategy

1. **Unit tests for v2 models**: Validate all new enums, `CheckpointV2` with
   and without `commit_sha`, `CheckpointIndexV2` secondary index lookups.
2. **Unit tests for session-end capture**: Mock transcript extraction and verify
   checkpoints are created with correct `trigger_type` and `session_status` for
   each deletion path.
3. **Unit tests for metadata propagation**: Verify `issue_number`/`pr_number`
   flow from session registration through to checkpoint creation.
4. **Index query tests**: Verify `by_session`, `by_issue`, `by_pr`, `by_commit`,
   `by_agent_type`, `by_phase`, `by_trigger`, `by_status` all return correct
   results.
5. **Regression tests**: Ensure existing push-triggered checkpoint flow works
   with v2 models (same behavior, new model structure).

## Open Questions

### 1. Session metadata source for issue/PR numbers

The orchestrator currently sets `EGG_ISSUE_NUMBER` and `EGG_PR_NUMBER` as
environment variables in the agent container, but doesn't pass them to the
gateway's session manager. Should the orchestrator:

- **A)** Pass issue/PR numbers during `register_session()` (recommended —
  single source of truth at registration time)
- **B)** Pass them via a new `update_metadata()` call after registration
- **C)** Have the checkpoint handler read from gateway environment variables
  (current approach for push checkpoints, but gateway env may not have these)

### 2. V1 deprecation timeline

The proposal says "stop writing to v1 after validation." Should we:

- **A)** Write to both v1 and v2 during a transition period for safety
- **B)** Write only to v2 immediately (recommended — the issue requests a
  clean break and v1 data is preserved in its branch)

---

*Authored-by: egg*
