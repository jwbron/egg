# Analysis: Sync pipeline state branch to remote for durability

> Issue: #857 | Phase: refine

## Problem Statement

The orchestrator persists pipeline state on a local-only git branch (`egg/pipeline-state`) backed by a Docker named volume. This state is never pushed to a remote, creating three concrete problems:

1. **Volume loss = total state loss.** If the Docker volume is deleted or corrupted, all pipeline history is gone. There is no recovery path.
2. **No cross-host recovery.** Spinning up the orchestrator on a different host starts with a blank slate — no way to resume or inspect prior pipelines.
3. **Old pipelines are invisible externally.** Unlike checkpoints (pushed to `egg/checkpoints/v2`), pipeline state cannot be inspected outside the container.

Additionally, old pipeline state files with outdated schemas (e.g., the removed `reviewer_unified` agent role) cause Pydantic `ValidationError` on every load attempt. Because startup reconciliation iterates all pipelines and attempts to load each one, these warnings recur on every restart indefinitely — there is no migration path or cleanup mechanism.

## Current Behavior

### Pipeline state persistence

Pipeline state lives on an orphan branch `egg/pipeline-state` managed via a persistent git worktree at `/home/egg/.egg-state/pipeline-worktree`. Key code:

- **Branch constant**: `STATE_BRANCH = "egg/pipeline-state"` — `orchestrator/state_store.py:40`
- **Worktree creation**: `_ensure_worktree()` — `orchestrator/state_store.py:188-235`. On first run, creates an orphan branch; on subsequent runs, reattaches the worktree.
- **State files**: Pipeline JSON stored at `.egg-state/pipelines/{id}.json` (e.g., `issue-496.json`).
- **Load/save**: `load_pipeline()` at `state_store.py:401-425` uses `Pipeline.model_validate(data)`. `save_pipeline()` at `state_store.py:427-484` serializes via `model_dump_json()`, writes to disk, and commits.
- **Commits are local-only**: No `git push` ever happens for this branch (explicitly documented at `state_store.py:10`).
- **Locking**: Cross-process `fcntl.flock` + in-process `threading.RLock` — `state_store.py:107-174`.

### Checkpoint system (the pattern to replicate)

Checkpoints push to `egg/checkpoints/v2` on every git push event. Key code:

- **Push logic**: `store_checkpoint_v2()` — `gateway/checkpoint_handler.py:728-900`
  1. Check branch existence on remote via `git ls-remote --heads`
  2. Force-fetch with `+` prefix to handle concurrent pushes
  3. Create orphan branch if new
  4. Write checkpoint JSON + update multi-dimensional index
  5. Commit and push with 120s timeout
- **Pull logic**: `fetch_and_read_index()` — `gateway/checkpoint_handler.py:905-962`. Fetches branch, reads `index.json` via `git show`.
- **Conflict resolution**: Force-fetch (`+refspec`) ensures local always matches remote before push. Git's non-fast-forward rejection provides natural conflict detection.
- **Error handling**: Non-blocking — failures are logged but never interrupt normal operations.

### Schema validation failures on startup

Startup reconciliation (`orchestrator/startup_reconciliation.py:30-142`) iterates all pipelines:

```python
for pipeline_id in pipeline_ids:
    try:
        pipeline = store.load_pipeline(pipeline_id)  # Pydantic model_validate
    except Exception as e:
        logger.warning("could not load pipeline", pipeline_id=pipeline_id, error=str(e))
        continue
```

When `load_pipeline()` encounters a JSON file containing `"role": "reviewer_unified"` (removed from `AgentRole` enum in commit e6706a916), `Pipeline.model_validate()` raises `ValidationError`, which is caught and logged as a warning. This happens on **every** orchestrator restart for every old pipeline with an outdated schema. Similarly, `get_active_pipelines()` at `state_store.py:652-674` silently skips these.

Current backwards-compatibility approach is ad-hoc: `DecisionStatus.TIMEOUT` was kept as a vestigial enum value (`models.py:60`) when the timeout mechanism was removed, but `REVIEWER_UNIFIED` was simply deleted with no equivalent accommodation.

## Constraints

- **Concurrency**: Multiple orchestrator instances could theoretically push to the same state branch simultaneously (though currently single-instance). The solution must handle concurrent pushes or explicitly document single-writer semantics.
- **Checkpoint pattern dependency**: The push logic lives in the **gateway** (`checkpoint_handler.py`), not the orchestrator. The orchestrator communicates with the gateway via `gateway_client.py`. Pushing pipeline state to remote needs to decide where this push logic lives — gateway vs. orchestrator.
- **Token access**: The orchestrator does not have direct GitHub token access. All authenticated git operations go through the gateway sidecar. This constrains where push logic can be implemented.
- **Write frequency**: Pipeline state is updated frequently (on every phase transition, agent status change, decision resolution). Checkpoints are written much less frequently (one per git push). The sync strategy must account for this volume difference.
- **State volume**: The number of pipeline state files grows unboundedly over time. All are loaded during `list_pipelines()` enumeration. Remote sync amplifies this if old pipelines are never cleaned up.
- **Backwards compatibility**: Any migration mechanism must handle arbitrary old schemas — not just the current known gap (`reviewer_unified`), but any future schema changes.
- **Branch ownership**: The gateway enforces branch ownership (`egg/` or `egg-` prefix). The `egg/pipeline-state` branch already follows this convention.

## Options Considered

### Option A: Push-on-commit (mirror checkpoint pattern)

**Approach**: After every `_commit_state()` call in `StateStore`, push `egg/pipeline-state` to remote via the gateway. On startup, fetch from remote before creating/attaching the worktree. Mirror the checkpoint system's force-fetch + push pattern.

**Pros**:
- Proven pattern — checkpoints already do exactly this
- Full durability — every state change is persisted remotely
- Simple mental model — remote always has latest state

**Cons**:
- High push frequency — pipeline state changes much more often than checkpoints (every phase transition, agent status change, decision resolution). Could add significant latency to state operations.
- Gateway dependency — every state save becomes a network round-trip through the gateway
- Conflict handling complexity — if push fails (network blip, concurrent writer), the state save must decide whether to retry, queue, or ignore. Current state saves are synchronous and local-only.
- Lock contention — the existing `fcntl.flock` + `RLock` would need to span the push operation, increasing lock hold times

### Option B: Periodic background sync

**Approach**: A background thread in the orchestrator periodically pushes the `egg/pipeline-state` branch to remote (e.g., every 30-60 seconds). On startup, fetch from remote. State saves remain local and fast.

**Pros**:
- Decouples write latency from sync — state saves stay fast and local
- Batches multiple changes into single pushes — reduces network traffic
- Failure-tolerant — a failed push is retried on the next cycle with no impact on state operations
- Simpler locking — push happens outside the critical path

**Cons**:
- Bounded data loss window — up to one sync interval of state changes could be lost if the volume dies between syncs
- Still requires gateway for push — the background thread needs gateway API access
- New component to maintain — periodic sync thread with health monitoring
- Startup fetch adds latency — orchestrator must wait for remote state before serving requests

### Option C: Push at phase boundaries only

**Approach**: Push to remote only at significant state transitions (phase start/complete, pipeline creation, pipeline terminal states). Intermediate state changes (agent heartbeats, container status) remain local-only.

**Pros**:
- Low push frequency — roughly 8-12 pushes per pipeline lifecycle (create + 4 phases × start/complete + terminal)
- Captures the most important state transitions
- Acceptable data loss — intermediate agent status is recoverable (startup reconciliation handles stale containers)
- Can be implemented as a simple hook in existing phase transition code

**Cons**:
- Partial durability — in-progress agent executions within a phase are not synced
- Must define exactly which transitions trigger a push — adds a new concept to reason about
- Intermediate state loss could confuse external inspection tools that expect up-to-date state

### Schema migration: Option I — Pre-validation data sanitization

**Approach**: Before calling `Pipeline.model_validate(data)`, run a sanitization pass that normalizes unknown enum values, adds missing fields with defaults, and removes unrecognized fields. Implemented as a `sanitize_pipeline_data(data: dict) -> dict` function in `state_store.py`.

**Pros**:
- Handles arbitrary schema drift — not tied to specific known changes
- No changes to the Pydantic models themselves
- Can log what was sanitized for audit trail
- Old state files become loadable without manual intervention

**Cons**:
- Must track which fields are enums and what their valid values are — fragile if models change
- Sanitized data may lose information (unknown enum values are replaced, not preserved)
- Needs comprehensive test coverage for edge cases

### Schema migration: Option II — Pydantic `model_validator` with lenient mode

**Approach**: Add a `@model_validator(mode="before")` to the `Pipeline` model that catches and normalizes known schema issues (e.g., mapping `reviewer_unified` → `reviewer_code`, stripping unknown fields). This keeps migration logic close to the model definition.

**Pros**:
- Co-located with the model — easy to find and maintain
- Pydantic-native — uses the framework's own extension point
- Can provide targeted, semantic migrations (e.g., `reviewer_unified` → specific reviewer role)

**Cons**:
- Grows over time as more migrations accumulate — risk of becoming a dumping ground
- Tightly couples the model to its history — models should ideally validate the present, not the past
- Harder to test in isolation

### Schema migration: Option III — Versioned migration chain

**Approach**: Add a `schema_version` field to pipeline state. On load, run a chain of migration functions (v1→v2, v2→v3, etc.) to bring old data up to the current schema before validation.

**Pros**:
- Clean separation — each migration is a discrete, testable function
- Explicit versioning — easy to reason about what schema an old file uses
- Standard pattern in database migrations (Django, Alembic)

**Cons**:
- Overhead for what may be infrequent schema changes
- Existing state files have no `schema_version` — need a bootstrap migration for the initial version
- More infrastructure to maintain (migration registry, version tracking)

## Recommended Approach

**Remote sync: Option C (push at phase boundaries)** — This provides meaningful durability for the most important state transitions while keeping the implementation simple and avoiding the latency/complexity costs of per-commit pushing. Pipeline state changes dozens of times per phase, but only phase boundaries matter for recovery — intermediate agent status is already handled by startup reconciliation. This approach adds roughly 8-12 remote pushes per pipeline lifecycle, well within acceptable limits.

**Schema compat: Option I (pre-validation sanitization)** — A `sanitize_pipeline_data()` function that normalizes unknown enum values and handles missing/extra fields provides the broadest coverage with the least model pollution. It's straightforward to implement and test. The versioned migration chain (Option III) is more principled but overkill for the current frequency of schema changes — it can be introduced later if migrations become more common.

## Open Questions

1. **Push frequency threshold**: Option C pushes at phase boundaries. Should it also push on pipeline creation and terminal state transitions (COMPLETE, FAILED, CANCELLED)? The recommendation assumes yes, but this should be confirmed.

2. **Startup fetch behavior**: When fetching from remote on startup, what should happen if the remote state is newer than local state? Options: (a) remote wins (overwrite local), (b) local wins (push local to override remote), (c) merge somehow. The recommendation is (a) remote wins, since the whole point is durability — if local state is stale, remote is the source of truth.

3. **Startup fetch failure**: If the remote fetch fails on startup (network error, branch doesn't exist yet), should the orchestrator (a) fail to start, (b) proceed with empty state, or (c) proceed with whatever local state exists? Option (c) seems most resilient and backwards-compatible.

4. **Old pipeline cleanup**: Old pipelines accumulate indefinitely. Should this issue also add a cleanup/archival mechanism (e.g., delete pipelines older than N days, or archive terminal pipelines)? Or is that a separate concern?

5. **Unknown enum handling**: When sanitizing old pipeline state, what should unknown enum values be mapped to? Options: (a) drop the containing object (e.g., remove the agent execution with unknown role), (b) map to a generic sentinel value (e.g., `"unknown"`), (c) map to specific replacements case-by-case. Recommendation is (a) drop with a log warning.

6. **Gateway push API**: The orchestrator doesn't have direct GitHub token access. The gateway currently has checkpoint push endpoints. Should a new gateway endpoint be added for pipeline state push, or should the orchestrator reuse the existing `gateway_client` infrastructure (e.g., `push_worktree_branch()`)?

7. **Concurrent orchestrator instances**: Is there any scenario where multiple orchestrator instances would run simultaneously against the same repo? If so, the push-at-phase-boundary approach needs conflict handling (force-push? merge? last-writer-wins?). If single-instance is guaranteed, this simplifies things significantly.

---

*Authored-by: egg*

<!-- metadata -->
```yaml
# metadata
complexity_tier: mid
```
