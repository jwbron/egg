# Analysis: Add Token Usage Tracking

> Issue: #519 | Phase: refine

## Problem Statement

The system currently captures token usage data per checkpoint (individual git push/commit) but lacks aggregate tracking and querying across multiple dimensions. The issue requests the ability to track token usage per:

- **Session**: A single Claude Code session (container run)
- **Job**: A task within a phase (e.g., `task-1`)
- **Workflow**: The entire SDLC pipeline for an issue (refine → plan → implement → pr)
- **Issue**: All work related to a GitHub issue number
- **PR**: All work related to a pull request

The goal is to enable cost analysis, usage monitoring, and budget tracking at various granularity levels.

## Current Behavior

Token usage is already captured at the **checkpoint level** with detailed breakdown:

**Data Model** (`shared/egg_contracts/checkpoints.py:114-129`):
```python
class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None
```

**Extraction** (`shared/egg_contracts/transcript_extractor.py:315-372`):
- Parses Claude Code JSONL session files
- Aggregates tokens from all assistant message `usage` blocks
- Calculates cost using current Opus 4.5 pricing ($15/MTok input, $75/MTok output, $1.5/MTok cache read)

**Storage** (`gateway/checkpoint_handler.py`):
- Checkpoints are stored in orphaned branch `egg/checkpoints/v1`
- Each checkpoint links to: `commit_sha`, `issue_number`, `pipeline_phase`, `branch`, `session_id`
- Index file (`index.json`) provides fast lookups by commit SHA, issue, or branch

**Checkpoint Summary** includes `total_tokens` for quick access without loading full checkpoint.

**Current Limitations**:
1. No aggregate views across checkpoints
2. No association with PR numbers (only issue numbers)
3. No workflow-level totals
4. No time-series analysis capability
5. Checkpoints are 1:1 with commits, but a session may span multiple commits

## Constraints

**Technical**:
- Must use existing orphaned branch mechanism for storage (as specified in issue)
- Must not impact gateway push latency (checkpoint storage is already async)
- Storage format should be queryable without loading all checkpoints
- Must maintain backward compatibility with existing checkpoint data

**Data Availability**:
- PR numbers are not available until PR is created (late in workflow)
- Session boundaries may span multiple commits/checkpoints
- Issue numbers may not be set for all branches/commits

**Performance**:
- Active repositories may generate hundreds of checkpoints per issue
- Aggregation queries should be O(1) or O(log n), not O(n) over all checkpoints

## Options Considered

### Option A: Aggregate Summary Files (Recommended)

**Approach**: Store pre-computed aggregate JSON files alongside checkpoints in the orphaned branch, updated atomically when checkpoints are added.

**Storage Structure**:
```
egg/checkpoints/v1/
├── index.json              # Existing checkpoint index
├── checkpoints/            # Existing per-checkpoint storage
│   └── ab/ckpt-abc123.json
├── usage/                  # NEW: Token usage aggregates
│   ├── by-issue/
│   │   └── 519.json        # { issue_number, total_tokens, cost, checkpoints[], ... }
│   ├── by-session/
│   │   └── sess-abc.json   # { session_id, total_tokens, checkpoints[], ... }
│   ├── by-workflow/
│   │   └── 519.json        # { issue_number, phases: { refine: {...}, plan: {...} }, ... }
│   └── by-pr/
│       └── 42.json         # { pr_number, issue_number, total_tokens, ... }
```

**Data Model** (new `UsageSummary` classes):
```python
class SessionUsage(BaseModel):
    session_id: str
    container_id: str | None
    agent_role: str | None
    checkpoints: list[str]  # checkpoint IDs
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    started_at: datetime
    ended_at: datetime | None

class IssueUsage(BaseModel):
    issue_number: int
    sessions: list[str]  # session IDs
    checkpoints: list[str]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    by_phase: dict[str, TokenUsage]  # refine, plan, implement, pr
    by_role: dict[str, TokenUsage]   # coder, tester, documenter, integrator

class WorkflowUsage(BaseModel):
    issue_number: int
    phases: dict[str, PhaseUsage]  # Detailed per-phase breakdown
    total_tokens: int
    estimated_cost_usd: float

class PRUsage(BaseModel):
    pr_number: int
    issue_number: int | None
    checkpoints: list[str]
    total_tokens: int
    estimated_cost_usd: float
```

**Implementation**:
1. Extend `CheckpointHandler.store_checkpoint()` to update aggregate files
2. Use atomic file operations (write temp, rename) for consistency
3. Add new loader methods: `load_issue_usage()`, `load_session_usage()`, etc.
4. Update summaries incrementally (read-modify-write pattern)

**Pros**:
- O(1) lookups for aggregates
- No schema migration required for existing checkpoints
- Aggregates stored alongside checkpoints (single orphaned branch)
- Incremental updates, no full recomputation needed
- Easy to add new aggregate dimensions later

**Cons**:
- Additional storage (duplicated summary data)
- Read-modify-write pattern requires locking or retry logic
- PR association requires backfill when PR is created

---

### Option B: Separate Orphaned Branch for Usage

**Approach**: Create a second orphaned branch (`egg/usage/v1`) specifically for usage tracking, separate from checkpoints.

**Storage Structure**:
```
egg/usage/v1/
├── index.json              # Master index of all usage records
├── sessions/
│   └── sess-abc.json
├── issues/
│   └── 519.json
└── prs/
    └── 42.json
```

**Pros**:
- Clean separation of concerns
- Independent versioning/schema evolution
- Smaller branch to fetch when only querying usage

**Cons**:
- Two branches to manage and sync
- More complex checkpoint → usage update flow
- Risk of inconsistency between branches
- Duplicate infrastructure (branch creation, push logic)

---

### Option C: Database/External Storage

**Approach**: Store usage data in PostgreSQL or Redis (already available in container).

**Schema** (PostgreSQL example):
```sql
CREATE TABLE token_usage (
    id SERIAL PRIMARY KEY,
    checkpoint_id VARCHAR(20),
    session_id VARCHAR(100),
    issue_number INT,
    pr_number INT,
    pipeline_phase VARCHAR(20),
    agent_role VARCHAR(20),
    input_tokens INT,
    output_tokens INT,
    cache_read_tokens INT,
    total_tokens INT,
    estimated_cost_usd DECIMAL(10,4),
    created_at TIMESTAMP
);

CREATE INDEX idx_usage_issue ON token_usage(issue_number);
CREATE INDEX idx_usage_session ON token_usage(session_id);
CREATE INDEX idx_usage_pr ON token_usage(pr_number);
```

**Pros**:
- Full SQL query flexibility (complex aggregations, time-series)
- Native indexing for all dimensions
- No file-level locking concerns
- Built-in aggregation functions (SUM, AVG, GROUP BY)

**Cons**:
- Adds database dependency for usage tracking
- Database state not versioned in Git
- Requires database availability (less resilient)
- Migration/backup complexity
- Diverges from "orphaned branch" approach mentioned in issue

---

### Option D: Lazy Aggregation (Query-Time Computation)

**Approach**: Keep only checkpoints, compute aggregates on-demand by scanning checkpoints.

**Implementation**:
```python
def get_issue_usage(issue_number: int) -> IssueUsage:
    checkpoints = index.get_by_issue(issue_number)
    return aggregate_checkpoints(checkpoints)
```

**Pros**:
- No additional storage
- Always consistent with checkpoint data
- Simple implementation

**Cons**:
- O(n) query time where n = checkpoints for issue
- Poor performance for large workflows (100+ checkpoints)
- Repeated computation for same queries
- Network overhead (fetch all checkpoints)

## Recommended Approach

**Option A: Aggregate Summary Files** is recommended because:

1. **Aligns with issue requirements**: Uses the existing orphaned branch mechanism
2. **Performance**: O(1) aggregate lookups, no full checkpoint scans
3. **Simplicity**: Single branch, incremental updates, no new infrastructure
4. **Extensibility**: Easy to add new dimensions (by-team, by-model, etc.)
5. **Resilience**: File-based, no database dependency

**Implementation Plan**:

1. **Add usage models** (`shared/egg_contracts/usage.py`):
   - `SessionUsage`, `IssueUsage`, `WorkflowUsage`, `PRUsage`
   - Aggregate update helpers

2. **Extend checkpoint handler** (`gateway/checkpoint_handler.py`):
   - After storing checkpoint, update relevant usage files
   - Handle race conditions with optimistic locking (read version, write if unchanged, retry)

3. **Add usage loader** (`shared/egg_contracts/usage_loader.py`):
   - Functions to load/query usage summaries
   - Backfill utility for existing checkpoints

4. **PR association**:
   - Add `pr_number` field to `Checkpoint` model
   - Update usage when PR is created (link existing checkpoints via issue)

5. **CLI/API for querying** (optional, phase 2):
   - `egg-usage --issue 519`
   - `egg-usage --session sess-abc`

## Open Questions

The following decisions would benefit from human input:

### Storage Format for Aggregates

<!-- HITL-DECISION:decision-1 -->
**Question**: What storage format should be used for aggregate files?

- [ ] **JSON** - Human-readable, consistent with checkpoints, easy to debug
- [ ] **MessagePack** - More compact (~40% smaller), faster parsing, less readable
- [ ] **SQLite** - Single file database, SQL queries, more complex but powerful
- [ ] Other (explain in reply)

<!-- /HITL-DECISION -->

### PR Number Association

<!-- HITL-DECISION:decision-2 -->
**Question**: How should PR numbers be associated with checkpoints that were created before the PR?

- [ ] **Backfill on PR creation** - When PR is created, update all checkpoints for the issue's branch to include `pr_number`
- [ ] **Lazy association** - Don't modify checkpoints; link via issue → PR mapping at query time
- [ ] **Both** - Backfill for query performance but also maintain mapping for queries
- [ ] Other (explain in reply)

<!-- /HITL-DECISION -->

### Historical Data Migration

Open-ended question: Are there existing checkpoints that need to be migrated to the new aggregate format, and if so, what is the approximate volume? Should migration be automated or can it be a one-time manual process?

---

*Authored-by: egg*
