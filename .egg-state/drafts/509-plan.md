# Plan: Migrate checkpoints to per-commit granularity and API proxy transcript capture

> Issue: #509 | Phase: plan

## Summary

This plan implements per-commit checkpoint granularity and migrates transcript capture from Claude Code JSONL files to Anthropic API proxy instrumentation. Based on the approved analysis (Option B), we will:

1. Modify the gateway push handler to iterate over all commits using `git rev-list`
2. Instrument the Anthropic API proxy to capture request/response pairs to a session-keyed buffer file
3. Replace the transcript extractor to read from the proxy buffer instead of Claude Code JSONL files
4. Implement fixed-size buffer rotation with hard cutover (no fallback to JSONL)

Per human feedback: buffer location is `/tmp/egg-transcripts/{container_id}.jsonl`, fixed size limit (10MB), and hard cutover with no JSONL fallback.

## Implementation Phases

### Phase 1: Per-Commit Iteration

**Goal**: Create one checkpoint per commit instead of one per push, using the existing transcript source (JSONL) as a stepping stone.

**Tasks**:
- [TASK-1-1] Add helper function to enumerate commits in a push — Acceptance: `get_commits_in_push(repo_path, old_sha, new_sha)` returns list of commit SHAs in chronological order
- [TASK-1-2] Modify push handler to call `capture_and_store_checkpoint` for each commit — Acceptance: Multi-commit push creates N checkpoints where N = number of commits
- [TASK-1-3] Add `push_sha` field to all per-commit checkpoints for traceability — Acceptance: Each checkpoint has `push_sha` pointing to tip commit of the push
- [TASK-1-4] Add unit tests for per-commit iteration — Acceptance: Tests verify correct number of checkpoints created for 1-commit, 3-commit, and 5-commit pushes

**Dependencies**: None

**Exit criteria**: Multi-commit pushes create one checkpoint per commit; all existing tests pass; `egg-checkpoint list` shows individual commits

### Phase 2: API Proxy Instrumentation

**Goal**: Capture Anthropic API request/response pairs at the gateway proxy layer.

**Tasks**:
- [TASK-2-1] Create `TranscriptBuffer` class to manage per-session buffer files — Acceptance: Buffer writes to `/tmp/egg-transcripts/{container_id}.jsonl`, handles concurrent access safely
- [TASK-2-2] Implement fixed-size (10MB) buffer rotation with oldest entries dropped — Acceptance: Buffer enforces size limit, rotates correctly, logs when entries are dropped
- [TASK-2-3] Add instrumentation hooks to `proxy_anthropic_messages()` for non-streaming requests — Acceptance: Non-streaming API calls are captured with request body, response content, and usage
- [TASK-2-4] Add instrumentation hooks for streaming requests — Acceptance: Streaming responses are reassembled from SSE chunks and captured with full content
- [TASK-2-5] Define buffer entry schema (request/response pairs with timestamps) — Acceptance: Schema documented, includes model, messages, tool_use, content, usage, timestamps
- [TASK-2-6] Add unit tests for buffer operations — Acceptance: Tests cover write, rotation, concurrent access, and edge cases (empty buffer, malformed entries)
- [TASK-2-7] Add integration tests for proxy instrumentation — Acceptance: Tests verify both streaming and non-streaming requests are captured correctly

**Dependencies**: None (can run in parallel with Phase 1)

**Exit criteria**: All API traffic is captured to buffer files; buffer rotation works correctly; streaming and non-streaming requests both captured

### Phase 3: Transcript Source Migration

**Goal**: Replace Claude Code JSONL extraction with proxy buffer reading.

**Tasks**:
- [TASK-3-1] Create `extract_transcript_from_proxy_buffer()` function — Acceptance: Reads `/tmp/egg-transcripts/{container_id}.jsonl` and returns same tuple as `extract_transcript_from_jsonl()`
- [TASK-3-2] Implement message extraction from proxy buffer format — Acceptance: Messages extracted with correct roles, timestamps, and content (respecting truncation limits)
- [TASK-3-3] Implement tool call extraction from proxy buffer — Acceptance: Tool calls extracted from `tool_use` blocks with parameters and result matching
- [TASK-3-4] Implement token usage aggregation from proxy buffer — Acceptance: Token counts aggregated from `usage` field in API responses
- [TASK-3-5] Update `CheckpointHandler.capture_checkpoint()` to use proxy buffer — Acceptance: Uses `extract_transcript_from_proxy_buffer()` with container ID from session; removes JSONL fallback
- [TASK-3-6] Add unit tests for proxy buffer transcript extraction — Acceptance: Tests verify correct extraction of messages, tool calls, file operations, and token usage
- [TASK-3-7] Add integration test for end-to-end checkpoint capture via proxy — Acceptance: API call → proxy capture → checkpoint extraction → correct checkpoint content

**Dependencies**: Phase 2 (proxy instrumentation must be working)

**Exit criteria**: Checkpoints are created from proxy buffer data; Claude Code JSONL files are no longer read; redaction still applies correctly

### Phase 4: Cleanup and Removal

**Goal**: Remove deprecated code and add operational monitoring.

**Tasks**:
- [TASK-4-1] Remove Claude Code JSONL-specific code from `transcript_extractor.py` — Acceptance: `find_session_file()` and `extract_transcript_from_jsonl()` removed; file renamed or repurposed
- [TASK-4-2] Remove `CLAUDE_PROJECTS_DIR` constant and related code from `checkpoint_handler.py` — Acceptance: No references to `~/.claude/projects/` remain in checkpoint code
- [TASK-4-3] Add buffer cleanup on session end — Acceptance: Buffer file deleted when container stops or session ends (via gateway session cleanup hooks)
- [TASK-4-4] Add monitoring metrics for buffer operations — Acceptance: Metrics for buffer writes, rotation events, and checkpoint captures logged to structured logs
- [TASK-4-5] Update CLI help text if needed — Acceptance: `egg-checkpoint --help` reflects per-commit behavior
- [TASK-4-6] Add documentation comments explaining the proxy buffer architecture — Acceptance: Code comments explain buffer format, lifecycle, and integration points

**Dependencies**: Phase 3 (migration must be complete before removal)

**Exit criteria**: No Claude Code JSONL dependencies remain; buffer cleanup works; monitoring is in place

## Test Strategy

- **Unit tests**:
  - `test_checkpoint_handler.py`: Add tests for per-commit iteration, push_sha field, edge cases (empty push, force push)
  - `test_transcript_buffer.py` (new): Buffer write/read, rotation, concurrent access, size limits
  - `test_proxy_transcript_extractor.py` (new): Extract messages, tool calls, token usage from proxy format

- **Integration tests**:
  - `test_anthropic_proxy.py`: Add tests for capture hooks (streaming and non-streaming)
  - `test_checkpoint_e2e.py` (new or extended): End-to-end test: API call → buffer → checkpoint

- **Manual testing**:
  1. Make multiple commits locally, push, verify N checkpoints created
  2. Verify `egg-checkpoint show <sha>` works for any commit in a multi-commit push
  3. Verify transcript content matches actual API conversation
  4. Verify buffer rotation occurs when session exceeds 10MB of API traffic

## Rollback Plan

If issues are discovered after deployment:

1. **Phase 1 rollback**: Revert `checkpoint_handler.py` and `gateway.py` changes to restore per-push behavior
   ```bash
   git revert <phase-1-commit>
   ```

2. **Phase 2 rollback**: Remove instrumentation hooks from `proxy_anthropic_messages()`; buffer files are harmless and auto-deleted on reboot
   ```bash
   git revert <phase-2-commit>
   ```

3. **Phase 3 rollback**: This is the critical phase. If issues occur:
   - Re-add JSONL extraction as primary source
   - Keep proxy buffer as secondary for debugging
   ```bash
   git revert <phase-3-commit>
   ```

4. **Emergency**: If checkpoints are causing push failures, disable via environment:
   ```bash
   export CHECKPOINT_ENABLED=false
   systemctl --user restart gateway
   ```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Streaming reassembly drops content | Medium | High | Comprehensive tests for SSE parsing; log warnings on incomplete chunks |
| Buffer file grows unbounded | Low | Medium | Fixed 10MB limit with rotation; monitoring for rotation events |
| Race condition in buffer writes | Low | Medium | Use file locking or append-only writes; each API call writes atomically |
| Proxy instrumentation adds latency | Low | Low | Capture is append-only to local file; no synchronous processing |
| Per-commit checkpoints increase storage | Medium | Low | Checkpoints compress well; storage is on dedicated branch |
| Session cleanup misses buffer files | Low | Low | `/tmp/` auto-cleans on reboot; add explicit cleanup hook |

## Migration Notes

- **No schema changes**: The checkpoint schema already supports per-commit granularity (`commit_sha` is a single value)
- **No CLI changes required**: `egg-checkpoint show <sha>` already works per-commit; `list` will show more entries
- **No breaking changes for users**: Checkpoints are internal; external contracts unchanged
- **Buffer format**: JSONL with one entry per API turn (request + response paired), enables simple append and line-by-line reading

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above.

```yaml
# yaml-tasks
pr:
  title: "Migrate checkpoints to per-commit granularity and API proxy capture"
  description: |
    Implements per-commit checkpoint granularity and migrates transcript capture from
    Claude Code JSONL files to Anthropic API proxy instrumentation. Each commit now
    produces exactly one checkpoint, enabling precise traceability. Transcripts are
    captured from API traffic at the gateway, eliminating dependency on Claude Code's
    internal file format.

    Closes #509
phases:
  - id: 1
    name: Per-Commit Iteration
    goal: Create one checkpoint per commit instead of one per push
    tasks:
      - id: TASK-1-1
        description: Add helper function to enumerate commits in a push
        acceptance: get_commits_in_push(repo_path, old_sha, new_sha) returns list of commit SHAs in chronological order
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-1-2
        description: Modify push handler to call capture_and_store_checkpoint for each commit
        acceptance: Multi-commit push creates N checkpoints where N = number of commits
        files:
          - gateway/gateway.py
          - gateway/checkpoint_handler.py
      - id: TASK-1-3
        description: Add push_sha field to all per-commit checkpoints for traceability
        acceptance: Each checkpoint has push_sha pointing to tip commit of the push
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-1-4
        description: Add unit tests for per-commit iteration
        acceptance: Tests verify correct number of checkpoints created for 1-commit, 3-commit, and 5-commit pushes
        files:
          - tests/gateway/test_checkpoint_handler.py
  - id: 2
    name: API Proxy Instrumentation
    goal: Capture Anthropic API request/response pairs at the gateway proxy layer
    tasks:
      - id: TASK-2-1
        description: Create TranscriptBuffer class to manage per-session buffer files
        acceptance: Buffer writes to /tmp/egg-transcripts/{container_id}.jsonl, handles concurrent access safely
        files:
          - gateway/transcript_buffer.py
      - id: TASK-2-2
        description: Implement fixed-size (10MB) buffer rotation with oldest entries dropped
        acceptance: Buffer enforces size limit, rotates correctly, logs when entries are dropped
        files:
          - gateway/transcript_buffer.py
      - id: TASK-2-3
        description: Add instrumentation hooks to proxy_anthropic_messages() for non-streaming requests
        acceptance: Non-streaming API calls are captured with request body, response content, and usage
        files:
          - gateway/gateway.py
          - gateway/transcript_buffer.py
      - id: TASK-2-4
        description: Add instrumentation hooks for streaming requests
        acceptance: Streaming responses are reassembled from SSE chunks and captured with full content
        files:
          - gateway/gateway.py
          - gateway/transcript_buffer.py
      - id: TASK-2-5
        description: Define buffer entry schema (request/response pairs with timestamps)
        acceptance: Schema documented, includes model, messages, tool_use, content, usage, timestamps
        files:
          - gateway/transcript_buffer.py
      - id: TASK-2-6
        description: Add unit tests for buffer operations
        acceptance: Tests cover write, rotation, concurrent access, and edge cases
        files:
          - tests/gateway/test_transcript_buffer.py
      - id: TASK-2-7
        description: Add integration tests for proxy instrumentation
        acceptance: Tests verify both streaming and non-streaming requests are captured correctly
        files:
          - tests/gateway/test_anthropic_proxy.py
  - id: 3
    name: Transcript Source Migration
    goal: Replace Claude Code JSONL extraction with proxy buffer reading
    tasks:
      - id: TASK-3-1
        description: Create extract_transcript_from_proxy_buffer() function
        acceptance: Reads /tmp/egg-transcripts/{container_id}.jsonl and returns same tuple as extract_transcript_from_jsonl()
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-3-2
        description: Implement message extraction from proxy buffer format
        acceptance: Messages extracted with correct roles, timestamps, and content
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-3-3
        description: Implement tool call extraction from proxy buffer
        acceptance: Tool calls extracted from tool_use blocks with parameters and result matching
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-3-4
        description: Implement token usage aggregation from proxy buffer
        acceptance: Token counts aggregated from usage field in API responses
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-3-5
        description: Update CheckpointHandler.capture_checkpoint() to use proxy buffer
        acceptance: Uses extract_transcript_from_proxy_buffer() with container ID from session; removes JSONL fallback
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-3-6
        description: Add unit tests for proxy buffer transcript extraction
        acceptance: Tests verify correct extraction of messages, tool calls, file operations, and token usage
        files:
          - tests/shared/egg_contracts/test_transcript_extractor.py
      - id: TASK-3-7
        description: Add integration test for end-to-end checkpoint capture via proxy
        acceptance: API call to proxy capture to checkpoint extraction produces correct checkpoint content
        files:
          - tests/gateway/test_checkpoint_e2e.py
  - id: 4
    name: Cleanup and Removal
    goal: Remove deprecated code and add operational monitoring
    tasks:
      - id: TASK-4-1
        description: Remove Claude Code JSONL-specific code from transcript_extractor.py
        acceptance: find_session_file() and extract_transcript_from_jsonl() removed; file renamed or repurposed
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-4-2
        description: Remove CLAUDE_PROJECTS_DIR constant and related code from checkpoint_handler.py
        acceptance: No references to ~/.claude/projects/ remain in checkpoint code
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-4-3
        description: Add buffer cleanup on session end
        acceptance: Buffer file deleted when container stops or session ends
        files:
          - gateway/session_manager.py
          - gateway/transcript_buffer.py
      - id: TASK-4-4
        description: Add monitoring metrics for buffer operations
        acceptance: Metrics for buffer writes, rotation events, and checkpoint captures logged to structured logs
        files:
          - gateway/transcript_buffer.py
          - gateway/checkpoint_handler.py
      - id: TASK-4-5
        description: Update CLI help text if needed
        acceptance: egg-checkpoint --help reflects per-commit behavior
        files:
          - shared/egg_contracts/checkpoint_cli.py
      - id: TASK-4-6
        description: Add documentation comments explaining the proxy buffer architecture
        acceptance: Code comments explain buffer format, lifecycle, and integration points
        files:
          - gateway/transcript_buffer.py
          - gateway/checkpoint_handler.py
```

---

*Authored-by: egg*
