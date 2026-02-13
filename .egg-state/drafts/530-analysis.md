# Analysis: Checkpoint v2 with Rich Querying

> Issue: #530 | Phase: refine

## Problem Statement

The v1 checkpoint system captures agent session context only when agents push commits to GitHub. This creates two gaps:

1. **Post-push context loss**: Agent reasoning, tool calls, and decisions made after the final `git push` are never captured. The transcript buffer is cleaned up when the session ends (`session_manager.py:653`), discarding all post-push context.

2. **No-push sessions produce zero checkpoints**: Sessions that never push code — review bots, research tasks, documentation agents, errored sessions, or sessions that fail before producing any commits — generate no checkpoints at all. These sessions are invisible to the checkpoint system.

Together, these gaps mean the checkpoint system provides incomplete coverage of agent activity. Any workflow correlation, debugging, or audit trail is limited to sessions that happen to push code.

## Current Behavior

### Checkpoint Capture Flow

Checkpoints are triggered exclusively from the gateway push handler (`gateway.py` → `checkpoint_handler.py:747`):

1. Agent pushes code via `git push origin <branch>`
2. Gateway forwards push to GitHub
3. On success, `capture_and_store_checkpoints_for_push()` is called
4. `get_commits_in_push()` enumerates commits between old and new SHA
5. For each commit, a `Checkpoint` is created with transcript from the API proxy buffer
6. Checkpoints are stored asynchronously to the `egg/checkpoints/v1` orphan branch

### Current Data Model (`checkpoints.py`)

The v1 `Checkpoint` model has `commit_sha` as a **required** field (`checkpoints.py:146-149`), making it structurally impossible to create a checkpoint without a commit. The `CheckpointIndex` provides lookup by commit SHA, issue number, and branch — but no lookup by session ID, agent type, or session status.

Key v1 models:
- `Checkpoint` — schema version 1.0, requires `commit_sha`
- `CheckpointSummary` — index entry with `commit_sha`, `session_id`, `agent_role`
- `CheckpointIndex` — flat list of summaries with `get_by_commit()`, `get_by_issue()`, `get_by_branch()`

### Session Deletion Paths

There are four paths where sessions end and transcript buffers are cleaned up, none of which capture checkpoints:

| Path | Location | Trigger |
|------|----------|---------|
| `delete_session()` | `session_manager.py:597` | Explicit session deletion by token |
| `delete_session_by_container()` | `session_manager.py:629` | Container-based deletion (calls `_cleanup_transcript_buffer`) |
| `prune_expired_sessions()` | `session_manager.py:665` | 24-hour TTL expiration (calls `_cleanup_transcript_buffer`) |
| `cleanup_orphaned_worktrees()` | `worktree_manager.py:658` | Orphan detection on startup (no transcript cleanup, but worktree is removed) |

Note: `delete_session()` does **not** call `_cleanup_transcript_buffer()` — only `delete_session_by_container()` and `prune_expired_sessions()` do. This is a potential existing bug where transcript buffers leak when sessions are deleted by token.

### Session Metadata

The `Session` dataclass (`session_manager.py:94`) stores:
- `container_id`, `container_ip`, `mode` (private/public/local)
- `agent_role` (optional, set by workflow context)
- `phase` (optional, SDLC pipeline phase)
- **No `issue_number` or `pr_number`** — these are only available as environment variables (`EGG_ISSUE_NUMBER`, `EGG_PR_NUMBER`) at checkpoint capture time

This means session-end checkpoints would need to obtain issue/PR metadata from either the environment variables (if still set) or from the session object (if extended).

## Constraints

### Technical Constraints

1. **Git-based storage**: Checkpoints are stored in an orphan branch (`egg/checkpoints/v1`). A v2 branch (`egg/checkpoints/v2`) adds a second branch that must be created, pushed, and maintained.

2. **Async checkpoint capture**: The current system stores checkpoints in background threads (`checkpoint_handler.py:723-740`). Session-end checkpoints face a timing race: the transcript buffer must survive long enough for the checkpoint to be captured before `_cleanup_transcript_buffer()` deletes it.

3. **Gateway sidecar context**: The checkpoint handler runs in the gateway process, which has access to the git repo and GitHub token. Session-end checkpoints must also run in this context.

4. **Transcript buffer location**: Buffers live at `/tmp/egg-transcripts/{container_id}.jsonl` and are written by `transcript_buffer.py` during API proxying. They are the sole source of transcript data.

5. **Non-blocking requirement**: Checkpoint failures must never block session cleanup or API responses. The current system enforces this via try/except and background threads.

### Schema Constraints

1. **Breaking change accepted**: The issue explicitly calls for a v2 schema with no backward compatibility requirement. v1 checkpoints remain untouched in their branch.

2. **Pydantic model validation**: All models use Pydantic v2 with strict field validation (`pattern`, `ge`, `Field`). New enums and models must follow the same patterns.

3. **Deterministic ID generation**: `generate_checkpoint_id_from_commit()` uses `commit_sha:session_id:timestamp`. Session-end checkpoints without commits need a different ID generation strategy.

### Operational Constraints

1. **Index size**: The single `index.json` file grows with every checkpoint. Adding multi-dimensional secondary indices (by_session, by_issue, by_pr, etc.) will increase index file size. For a repository with many agent sessions, this could become a concern.

2. **Concurrent writes**: Multiple sessions ending simultaneously could race on index updates. The current worktree-based checkout-commit-push pattern serializes writes through git, but concurrent pushes to the checkpoint branch could fail.

3. **Gateway restart**: If the gateway crashes, in-flight async checkpoint storage is lost. Session-end checkpoints for crashed containers need to be captured during orphan cleanup.

## Dependencies

| Component | Role | Impact |
|-----------|------|--------|
| `shared/egg_contracts/checkpoints.py` | Data models | New v2 models, enums |
| `shared/egg_contracts/checkpoint_loader.py` | Storage/retrieval | New v2 save/load with multi-index |
| `gateway/checkpoint_handler.py` | Capture logic | New `capture_session_end_checkpoint()`, v2 branch |
| `gateway/session_manager.py` | Session lifecycle | Hook checkpoint capture into deletion paths |
| `gateway/worktree_manager.py` | Orphan cleanup | Hook checkpoint capture for crashed sessions |
| `shared/egg_contracts/transcript_extractor.py` | Transcript parsing | Increased size limits |
| `gateway/transcript_buffer.py` | Buffer I/O | Buffer preservation during capture |
| `gateway/gateway.py` | Push handler | Migrate push-triggered checkpoints to v2 |
| `.egg/schemas/checkpoint.schema.json` | JSON Schema | v2 schema definition |

## Options Considered

### Option A: Minimal — Session-End Capture Only (No Schema Change)

**Approach**: Add session-end checkpoint capture to the existing v1 system. Make `commit_sha` optional in the v1 model. Keep the flat index.

**Pros**:
- Smallest change surface
- No migration, no new branch
- Addresses the core problem (missing checkpoints for no-push sessions)

**Cons**:
- Making `commit_sha` optional in v1 breaks the existing contract (v1 consumers expect it)
- Flat index provides no efficient querying by session, agent type, or status
- No `trigger_type` or `session_status` fields — consumers can't distinguish commit checkpoints from session-end checkpoints
- Loses the opportunity to build a queryable checkpoint system

### Option B: Full v2 as Proposed in Issue (Recommended)

**Approach**: Implement the full v2 schema as described in the issue: new enums (`TriggerType`, `SessionStatus`, `AgentType`), new `CheckpointV2` model with optional `commit_sha`, `CheckpointIndexV2` with multi-dimensional indices. Store in new `egg/checkpoints/v2` branch.

**Pros**:
- Clean break from v1 — no backward compatibility concerns
- Rich querying via secondary indices (by_session, by_issue, by_pr, by_commit, by_agent_type, by_phase, by_trigger, by_status)
- Clear semantics with `trigger_type` and `session_status`
- `session_id` promoted to top-level required field enables session-based workflows
- Future-proof for additional checkpoint triggers (e.g., periodic, on-demand)

**Cons**:
- Larger implementation scope (8 phases, 39 tasks per the existing plan)
- Index file grows larger with secondary indices
- Dual branch maintenance during transition (v1 and v2 coexist)

### Option C: v2 Schema with Deferred Indexing

**Approach**: Implement v2 checkpoint model and session-end capture, but keep a simple flat index (like v1). Add secondary indices later as a separate issue.

**Pros**:
- Moderate scope — gets the core value (session-end capture) with v2 semantics
- Index complexity deferred
- Still a clean v2 schema with proper enums and optional `commit_sha`

**Cons**:
- Querying capabilities limited until indices are added
- May require another schema migration for index changes
- The flat index will perform poorly for session/issue/PR lookups as checkpoints accumulate

## Recommended Approach

**Option B: Full v2 as proposed in the issue.** The issue has already been through analysis and planning phases with owner approval. The v2 schema with multi-dimensional indexing addresses both the immediate problem (missing checkpoints) and the broader need (queryable agent activity history).

### Justification

1. **Approved design**: The owner (@jwbron) has already approved the v2 approach with specific decisions on metadata propagation (hybrid registration + env fallback), checkpoint timing (async with buffer preservation), and crash handling (both status and truncation reason).

2. **Clean semantics**: `TriggerType` and `SessionStatus` enums provide unambiguous classification of checkpoints. Consumers never need to guess whether a checkpoint came from a push or a session end.

3. **Query efficiency**: Secondary indices enable O(1) lookups by session, issue, PR, commit, agent type, phase, trigger type, and status. Without these, every query requires a full scan of the checkpoint list.

4. **Session-end checkpoints solve the core problem**: Every session gets at least one checkpoint — whether it pushed code or not. The `SESSION_END` trigger type with `COMPLETED`/`EXPIRED`/`FAILED` status provides visibility into all agent activity.

5. **Increased transcript limits**: Bumping content limits (10KB→25KB, 1KB→2.5KB params, 500B→1.5KB results, 1MB→3MB total) captures more context per checkpoint, particularly valuable for long-running sessions.

### Key Implementation Considerations

**Buffer preservation timing**: The most critical design challenge is ensuring the transcript buffer survives long enough for session-end checkpoint capture. The recommended approach (async with 30-second timeout) requires:
- Starting checkpoint capture **before** calling `_cleanup_transcript_buffer()`
- Adding a completion signal/callback so cleanup waits for capture
- Falling back to timeout-based cleanup if capture hangs

**Session metadata propagation**: The hybrid approach (store issue/PR at registration + env fallback) requires adding `issue_number` and `pr_number` fields to the `Session` dataclass and persisting them in `sessions.json`. The `register_session()` API must accept these optional parameters.

**Checkpoint ID generation for session-end**: Without a `commit_sha`, the deterministic ID generator needs a new strategy. Using `session_id:trigger_type:timestamp` would maintain uniqueness while accommodating the new trigger types.

**Concurrent index updates**: With more frequent checkpoint creation (session-end in addition to push), the risk of concurrent pushes to the checkpoint branch increases. The current git worktree checkout-commit-push pattern provides serialization, but failed pushes due to conflicts need retry logic.

## Open Questions

1. **Should `delete_session()` also capture a session-end checkpoint?** Currently only `delete_session_by_container()` and `prune_expired_sessions()` clean up transcript buffers. If `delete_session()` (by token) is used as a graceful deletion path, it should also trigger checkpoint capture.

2. **What is the expected index size at scale?** With multi-dimensional indices and more frequent checkpoints, the `index.json` file could grow substantially. Should we set a maximum index size or implement index sharding (e.g., per-month indices)?

3. **Should v1 checkpoint writes be disabled immediately?** The issue says "write only to v2 immediately" (no dual-write). This means existing tooling that reads from `egg/checkpoints/v1` will stop receiving new data. Is there any downstream consumer that needs migration time?

4. **Retry strategy for failed checkpoint branch pushes**: With more concurrent checkpoint writes, push conflicts become more likely. Should we implement exponential backoff retry, or is a single retry sufficient?

---

*Authored-by: egg*
