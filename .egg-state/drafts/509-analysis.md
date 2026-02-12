# Analysis: Migrate checkpoints to per-commit granularity and API proxy transcript capture

> Issue: #509 | Phase: refine

## Problem Statement

The current checkpoint implementation (PR #504) makes two design decisions that limit traceability and maintainability:

1. **Per-push granularity**: One checkpoint per push operation, even when a push contains multiple commits. This conflates the reasoning behind individual commits, making it impossible to trace exactly what led to a specific change.

2. **Claude Code JSONL dependency**: Transcripts are extracted from `~/.claude/projects/{project}/{session}.jsonl` files, which are internal to Claude Code and can change without notice. This couples the system to an unstable API surface.

The desired outcome is:
- **Per-commit checkpoints**: Each commit SHA maps to exactly one checkpoint, enabling precise traceability.
- **API proxy instrumentation**: Capture transcripts directly from Anthropic API traffic at the gateway, eliminating dependency on Claude Code file formats.

## Current Behavior

### Checkpoint Capture Flow (`gateway/gateway.py:683-708`)

After a successful git push, the gateway:
1. Runs `git rev-parse HEAD` to get the tip commit SHA
2. Calls `capture_and_store_checkpoint()` with only that single SHA
3. The checkpoint is stored asynchronously to avoid blocking the push response

```python
head_result = subprocess.run(git_cmd("rev-parse", "HEAD"), ...)
commit_sha = head_result.stdout.strip()
capture_and_store_checkpoint(
    repo_path=exec_path,
    commit_sha=commit_sha,  # Only HEAD, not individual commits
    branch=branch,
    ...
)
```

**Key limitation**: Multi-commit pushes only checkpoint HEAD. Commits `HEAD~1`, `HEAD~2`, etc. are not checkpointed.

### Transcript Extraction (`shared/egg_contracts/transcript_extractor.py`)

The current extractor:
1. Searches `~/.claude/projects/` for JSONL session files
2. Parses Claude Code's internal message format (user/assistant/tool_result entries)
3. Extracts messages, tool calls, token usage, and file operations
4. Aggregates entire session transcript regardless of commit boundaries

**Key limitations**:
- Claude Code JSONL format is undocumented and may change
- No commit-level transcript segmentation (entire session is captured)
- File discovery depends on Claude Code's project naming convention (`/` → `-`)

### Existing Proxy Architecture (`gateway/gateway.py:2689-2840`)

The gateway already proxies all Anthropic API traffic:
- Sandbox → `http://egg-gateway:9848/v1/messages` (HTTP, no credentials)
- Gateway injects credentials and forwards → `https://api.anthropic.com/v1/messages`
- Streaming SSE responses are forwarded transparently

This provides a single instrumentation point for all API traffic.

## Constraints

### Technical Constraints
- **Schema compatibility**: The checkpoint schema (`commit_sha` as single string) already supports per-commit granularity—no schema changes needed
- **Storage cost**: Per-commit checkpoints increase storage linearly with commit count, but JSON compresses well
- **Session boundaries**: A Claude Code session may span multiple commits; transcript segmentation is non-trivial
- **Non-blocking requirement**: Checkpoint capture must not block push responses (async storage)
- **Graceful degradation**: Push must succeed even if checkpoint capture fails

### Architectural Constraints
- **Gateway is the choke point**: All API traffic already flows through the gateway—ideal for instrumentation
- **No MITM for Squid**: The Squid proxy uses peek/splice (SNI inspection), not MITM—instrumentation must happen at the gateway application layer
- **Credential isolation**: API credentials are gateway-side only; container never sees them

### Dependencies
- **Existing tests**: `tests/gateway/test_anthropic_proxy.py` has 700+ lines of coverage
- **Redactor**: Same redaction patterns apply regardless of transcript source
- **CLI commands**: `egg-checkpoint show <sha>` already works per-commit; minimal CLI changes needed

## Options Considered

### Option A: Iterate Commits at Push Time + Session Transcript Reuse

**Approach**: When a push occurs, iterate over all commits being pushed (using `git rev-list`) and create one checkpoint per commit. Reuse the same session transcript for all checkpoints.

**Implementation**:
1. After push, run `git rev-list {old-sha}..{new-sha}` to get all pushed commits
2. For each commit, create a checkpoint with the same session transcript
3. Each checkpoint has unique `commit_sha` but shared transcript content

**Pros**:
- Minimal code changes—only checkpoint_handler.py push loop
- Each commit is traceable to its checkpoint
- Transcript extractor unchanged (still uses Claude Code JSONL)

**Cons**:
- Does not address Claude Code JSONL dependency (unchanged fragility)
- Transcript is duplicated across all checkpoints (storage overhead)
- No commit-level transcript segmentation (all commits share full session)
- Session transcript may not accurately reflect individual commit context

### Option B: API Proxy Instrumentation + Ring Buffer

**Approach**: Instrument the gateway's Anthropic proxy to capture request/response pairs into a session-scoped ring buffer. On push, create per-commit checkpoints using buffered API traffic.

**Implementation**:
1. Add instrumentation to `proxy_anthropic_messages()` to capture:
   - Request: model, messages, tools, system prompt (redacted)
   - Response: content, tool_use, usage (token counts)
2. Store in session-keyed ring buffer (e.g., `/tmp/egg-transcripts/{container_id}.jsonl`)
3. On push, read buffer and create per-commit checkpoints
4. Buffer is rotated/cleared on session end or size limit

**Pros**:
- Eliminates Claude Code JSONL dependency (format-stable API)
- Single source of truth for all API traffic (any client works)
- Token usage is exact (from `usage` field in response)
- Natural integration with existing gateway proxy code

**Cons**:
- Buffering adds complexity (rotation, cleanup, disk usage)
- No inherent commit-level segmentation (still need heuristics)
- More significant code changes across multiple files

### Option C: Hybrid - Proxy Instrumentation + Commit Markers

**Approach**: Combine API proxy instrumentation with commit-triggered markers to enable precise transcript segmentation.

**Implementation**:
1. Gateway captures API traffic to session buffer (per Option B)
2. On `git commit`, gateway inserts a marker into the buffer with commit SHA
3. On `git push`, extract transcript segments between markers for each commit
4. Each checkpoint contains only the API traffic relevant to that commit

**Pros**:
- Precise per-commit transcript segmentation
- Eliminates Claude Code JSONL dependency
- Enables accurate attribution of reasoning to specific commits
- Clean separation of concerns (gateway handles all instrumentation)

**Cons**:
- Requires modifying `git commit` handling in gateway (new hook point)
- Marker mechanism adds complexity
- If agent makes commits without going through gateway, markers are lost
- Transcript between commits may not align perfectly with commit content

### Option D: API Proxy Instrumentation + Shared Transcript Reference

**Approach**: Capture full session transcript via API proxy, store once, and have per-commit checkpoints reference the shared transcript.

**Implementation**:
1. Gateway captures API traffic to session buffer
2. On push, store full transcript as a single "session transcript" object
3. Create per-commit checkpoints with `transcript_ref` pointing to session transcript
4. Add `commit_offset` fields to indicate rough position in transcript

**Pros**:
- Eliminates storage duplication (transcript stored once per session)
- Eliminates Claude Code JSONL dependency
- Checkpoints remain small and fast to create
- Session-level analysis remains possible

**Cons**:
- Adds indirection (must follow reference to get full transcript)
- Schema change required (`transcript_ref` field)
- Position offsets are approximate (may not align with commit boundaries)

## Recommended Approach

**Option B: API Proxy Instrumentation + Ring Buffer** is recommended, with elements from Option A for the commit iteration.

### Rationale

1. **Addresses both issues**: Eliminates Claude Code JSONL dependency AND enables per-commit checkpoints
2. **Minimal schema changes**: Current schema supports per-commit granularity; no breaking changes
3. **Gateway is the right layer**: All API traffic already flows through the gateway—this is the natural instrumentation point
4. **Proven architecture**: The gateway proxy is well-tested (700+ lines of tests) and handles streaming properly
5. **Forward-compatible**: Works with any client (Claude Code, custom scripts, future tools)

### Implementation Phases

**Phase 1: Per-Commit Iteration (Low Risk)**
- Modify `gateway/checkpoint_handler.py` to iterate commits via `git rev-list`
- Create one checkpoint per commit (same session transcript, different SHA)
- No transcript source changes yet

**Phase 2: API Proxy Instrumentation (Medium Risk)**
- Add capture hooks to `proxy_anthropic_messages()` in `gateway/gateway.py`
- Write to session-keyed buffer file (`/tmp/egg-transcripts/{container_id}.jsonl`)
- Buffer format: one JSON object per API turn (request + response)

**Phase 3: Transcript Source Migration (Medium Risk)**
- Replace `transcript_extractor.py` content with proxy buffer reader
- Keep same function signature for backward compatibility
- Update `checkpoint_handler.py` to use new source

**Phase 4: Cleanup**
- Add buffer rotation and cleanup on session end
- Add monitoring for buffer disk usage
- Remove Claude Code JSONL fallback (or keep as deprecated path)

### Why Not Option C (Commit Markers)?

While Option C provides the most precise segmentation, it requires:
- Hook into `git commit` at the gateway (currently only push is hooked)
- Assumptions about commit workflow (agent may commit locally without gateway)
- Additional complexity for marginal benefit

Most sessions have a small number of commits (1-3), and the full session transcript provides sufficient context even without perfect segmentation.

### Why Not Option D (Shared Reference)?

While storage-efficient, it:
- Requires schema changes (`transcript_ref` field)
- Adds indirection that complicates CLI and analysis tools
- The storage savings are modest (transcripts compress well)

## Open Questions

### 1. Buffer Location and Lifecycle

Where should the proxy transcript buffer be stored, and when should it be cleaned up?

**Options**:
- **`/tmp/egg-transcripts/{container_id}.jsonl`**: Simple, auto-cleaned on reboot
- **`/var/lib/egg/transcripts/`**: Persistent, survives restarts
- **In-memory with overflow to disk**: Fast, but complex

**Recommendation**: Use `/tmp/` with explicit cleanup on session end (container stop event).

### 2. Buffer Size Limits

How large should the buffer be allowed to grow before rotation?

Long sessions can generate megabytes of API traffic. Options:
- **Fixed size (e.g., 10MB)**: Simple, predictable
- **Message count (e.g., 1000 turns)**: Semantic, but variable size
- **Time-based window (e.g., last 2 hours)**: Aligns with typical session length

**Recommendation**: 10MB ring buffer with oldest entries dropped when full.

### 3. Fallback Behavior

If proxy instrumentation fails or buffer is empty, should we fall back to Claude Code JSONL?

**Options**:
- **Hard cutover**: No fallback, rely on proxy only
- **Graceful fallback**: Try proxy buffer first, fall back to JSONL
- **Dual capture**: Capture from both, prefer proxy

**Recommendation**: Graceful fallback during migration period, with metrics to track fallback rate. Remove fallback after 30 days of production stability.

---

*Authored-by: egg*
