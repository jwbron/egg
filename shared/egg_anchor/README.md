# egg_anchor

Persistent anchor mechanism for agent post-compaction state recovery.

## Overview

`egg_anchor` provides structured state persistence that allows agents to recover coherent working state after their context window is cleared. Instead of relying on lossy context compaction, agents fully clear their context and reload from a structured JSON anchor file.

Each running agent maintains an anchor file at `.egg-state/agent-anchors/<agent-id>.json` containing:
- **Task progress** — completed, working, pending, and blocked sub-steps (max 10)
- **HITL decisions** — decisions encountered and their resolutions (max 8)
- **BRC consensus state** — protocol phase, ACKs/NACKs, last processed message ID
- **Key context** — critical labeled context items (max 5, label ≤50 chars, value ≤500 chars)
- **Error history** — failed approaches with optional resolutions (max 5)
- **Modified files** — files currently being edited (max 15)

## Quick Start

```python
from datetime import datetime, timezone
from egg_anchor import AgentAnchor, AnchorMeta, ProgressItem, TaskInfo
from egg_anchor import load_anchor, save_anchor, validate_anchor, check_size_budget
from egg_anchor.models import AnchorStatus, ProgressState

now = datetime.now(timezone.utc)

# Create an anchor
anchor = AgentAnchor(
    _meta=AnchorMeta(
        schema_version="1.0",
        created_at=now,
        updated_at=now,
        sequence=0,
    ),
    agent_id="coder-abc12345",
    role="coder",
    pipeline_id="issue-123",
    task=TaskInfo(id="task-1-1", description="Fix auth bypass", phase="implement"),
    status=AnchorStatus.WORKING,
    progress=[
        ProgressItem(step="Identified root cause", state=ProgressState.COMPLETE, timestamp=now),
        ProgressItem(step="Fixing token validation", state=ProgressState.WORKING, timestamp=now),
    ],
)

# Save atomically (temp-file-then-rename)
save_anchor(anchor)

# Validate against JSON Schema
errors = validate_anchor(anchor)
if errors:
    raise ValueError(f"Validation failed: {errors}")

# Check size budget
budget = check_size_budget(anchor)
if budget.warnings:
    print(f"Size warning: {budget.warnings}")  # Soft limit exceeded
if not budget.within_budget:
    raise ValueError(f"Over budget: {budget.errors}")  # Hard limit exceeded

# Load by agent ID (reads from .egg-state/agent-anchors/<agent-id>.json)
loaded = load_anchor("coder-abc12345")
```

## Size Budget

Anchors are size-constrained to minimize context window usage after recovery:

| Scope | Soft Limit | Hard Limit | Approx. Tokens |
|-------|-----------|------------|-----------------|
| Per agent | 2 KB | 3 KB | ~500-600 |
| Team (all agents) | 4 KB | 6 KB | ~1,000-1,500 |

- **Soft limit**: CLI warns but allows the write
- **Hard limit**: CLI rejects the write

Reading own anchor (2 KB) + team anchor (4 KB) ≈ 1,500 tokens — under 2% of a post-clear context window.

## CLI Usage

Anchors are managed via `egg-orch anchor` subcommands:

```bash
# Initialize anchor for current task
egg-orch anchor init --task "Fix auth bypass in gateway/auth.py"

# Update anchor (atomic, all-or-nothing)
egg-orch anchor update --status working \
  --progress '{"step":"Fixing token validation","state":"working"}' \
  --key-context "Token validation skips expiry for admin scope"

# View anchors
egg-orch anchor show                    # Own anchor
egg-orch anchor show --agent coder-abc  # Another agent's (via API)
egg-orch anchor show --team             # Team anchor (via API)

# Validate schema and size
egg-orch anchor validate

# Clean up orphaned anchors
egg-orch anchor cleanup
```

See [Orchestrator CLI Reference](../../docs/reference/orchestrator-cli.md) for full command details.

## Models

### AgentAnchor

Top-level anchor model:

| Field | Type | Description |
|-------|------|-------------|
| `_meta` | `AnchorMeta` | Schema version, timestamps, sequence counter |
| `agent_id` | `str` | Agent identifier (`{role}-{short_container_id}`) |
| `role` | `str` | Agent role (coder, tester, documenter, etc.) |
| `pipeline_id` | `str` | Pipeline this agent belongs to |
| `team` | `list[str]` | Other agent IDs in the pipeline |
| `task` | `TaskInfo` | Current task (id, description, phase) |
| `status` | `AnchorStatus` | Agent status enum |
| `progress` | `list[ProgressItem]` | Sub-step progress (max 10) |
| `decisions` | `list[Decision]` | HITL decisions encountered (max 8) |
| `brc_state` | `BRCState` | BRC consensus state with `last_message_id` |
| `key_context` | `list[KeyContext]` | Critical labeled context (max 5) |
| `errors_encountered` | `list[ErrorEncountered]` | Failed approaches (max 5) |
| `files_modified` | `list[str]` | Files being edited (max 15) |

### Enums

| Enum | Values |
|------|--------|
| `AnchorStatus` | `initializing`, `working`, `proposed`, `confirmed`, `blocked`, `failed` |
| `BRCPhase` | `orient`, `working`, `proposed`, `reviewing`, `confirmed` |
| `ProgressState` | `pending`, `working`, `complete`, `blocked` |

### Key Sub-Models

- **`AnchorMeta`**: `schema_version`, `created_at`, `updated_at`, `sequence` (monotonic counter)
- **`TaskInfo`**: `id`, `description`, `phase`
- **`ProgressItem`**: `step`, `state` (ProgressState), `detail` (optional), `timestamp`
- **`Decision`**: `id`, `question`, `answer` (optional), `decided_by` (optional), `timestamp`
- **`BRCState`**: `phase` (BRCPhase), `proposed_at`, `acks`, `nacks`, `last_message_id`
- **`KeyContext`**: `label` (max 50 chars), `value` (max 500 chars)
- **`ErrorEncountered`**: `error` (max 200 chars), `resolution` (max 200 chars), `timestamp`

## Architecture

```
Agent containers (shared worktree in BRC concurrent phases)
  ├── Local anchor write: .egg-state/agent-anchors/<agent-id>.json
  └── Sync to orchestrator: POST /api/v1/anchors/{agent_id}

Orchestrator
  ├── Stores all agent anchors (Redis: anchor:{pipeline_id}:{agent_id})
  ├── Cross-agent reads: GET /api/v1/anchors/{agent_id}
  └── Team anchor generation: GET /api/v1/anchors/team/{pipeline_id}
```

- **Agent anchors**: Agent is the sole writer via `save_anchor()` + `sync_anchor_to_api()`. Updates at natural checkpoints (sub-task completion, decisions, status changes).
- **Team anchor**: Orchestrator-generated projection from individual anchors + pipeline state. Never written by agents.
- **Gateway enforcement**: Session-scoped — agents can only write their own anchor file.

## Functions

### Loader (`egg_anchor.loader`)

| Function | Description |
|----------|-------------|
| `load_anchor(agent_id, base_dir=None)` | Load anchor from filesystem. Returns `None` if not found. |
| `save_anchor(anchor, base_dir=None)` | Save anchor atomically (temp-then-rename). Returns file path. |
| `sync_anchor_to_api(anchor, orchestrator_url=None)` | Sync anchor to orchestrator Redis. Returns `True` on success. |

### Validator (`egg_anchor.validator`)

| Function | Description |
|----------|-------------|
| `validate_anchor(anchor)` | Validate against JSON Schema. Returns list of error strings (empty = valid). |
| `check_size_budget(anchor, is_team=False)` | Check size limits. Returns `SizeBudgetResult`. |

## Files

| File | Purpose |
|------|---------|
| `__init__.py` | Public API exports (models, loader, validator) |
| `models.py` | Pydantic models with field validators for array size constraints |
| `loader.py` | Atomic file I/O (temp-then-rename), API sync via HTTP POST |
| `validator.py` | JSON Schema validation, size budget checking (SizeBudgetResult) |
| `constants.py` | Re-exports anchor constants from egg_config (with fallback defaults) |
| `tests/` | Unit tests for models, loader, validator |

## Integration Points

- **egg-contract**: Anchors complement contracts. Contracts = pipeline-level tasks. Anchors = agent-level working state.
- **Checkpoints**: Anchor files are included in checkpoint captures. `egg-checkpoint show` displays anchor data.
- **BRC consensus**: `brc_state` mirrors `PeerConsensusTracker`. Consensus wrapper loads anchor in recovery prompt.
- **Cross-agent messaging**: `brc_state.last_message_id` enables efficient post-clear message catch-up.
- **Gateway**: Phase filter allows anchor writes in all phases. Session-scoped validation prevents cross-agent tampering.

## Testing

```bash
# Run egg_anchor unit tests
python -m pytest shared/egg_anchor/tests/ -v
```

## Related Documentation

- [Anchor Recovery Guide](../../docs/guides/anchor-recovery.md) — Post-clear recovery protocol
- [Orchestrator CLI Reference](../../docs/reference/orchestrator-cli.md) — `egg-orch anchor` commands
- [Agent Recovery Reference](../../docs/reference/agent-recovery.md) — Retry, circuit breaker, conflict detection
- [Concurrent Execution Guide](../../docs/guides/concurrent-execution.md) — BRC consensus protocol
