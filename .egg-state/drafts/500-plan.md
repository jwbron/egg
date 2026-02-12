# Plan: Checkpoints — Capture Agent Session Context as Versioned Data in Git

> Issue: #500 | Phase: plan

## Summary

This plan implements Checkpoints, a system for capturing agent session context as first-class versioned data in Git. Following the analysis recommendations, we will use a **Gateway Hook with Dedicated Branch** approach: intercept git push operations in the gateway sidecar to atomically capture checkpoint data, store checkpoints as structured JSON in the `egg/checkpoints/v1` branch, and link each checkpoint to its corresponding commit SHA. The implementation leverages existing patterns from `egg_contracts` (Pydantic models, JSON schema, atomic writes) and `session_manager.py` (session tracking, role metadata).

## Implementation Phases

### Phase 1: Schema and Models

**Goal**: Define the checkpoint data structure with JSON schema and Pydantic models, following established patterns in `.egg/schemas/` and `shared/egg_contracts/`.

**Tasks**:
- [TASK-1-1] Create checkpoint JSON schema — Acceptance: Schema validates sample checkpoints with all required fields (commit_sha, transcript, files_touched, tool_calls, token_usage, session_metadata)
- [TASK-1-2] Create Pydantic models for checkpoint data — Acceptance: Models match JSON schema, unit tests pass for serialization/deserialization
- [TASK-1-3] Add checkpoint loader with atomic write support — Acceptance: Loader can read/write checkpoints with temp file + rename pattern, handles concurrent access

**Dependencies**: None (foundational phase)

**Exit criteria**: Schema validates test fixtures, models can be instantiated and serialized, loader passes unit tests.

### Phase 2: Transcript Capture

**Goal**: Implement transcript extraction from Claude Code's session data, with sensitive data redaction per human decision.

**Tasks**:
- [TASK-2-1] Create transcript extractor for Claude Code JSONL format — Acceptance: Extractor parses `~/.claude/projects/` JSONL files and returns structured transcript data
- [TASK-2-2] Implement tool call extraction with parameters and results — Acceptance: Tool calls captured with name, parameters, and result summaries
- [TASK-2-3] Add sensitive data redaction (file paths, potential secrets) — Acceptance: Redactor removes/masks env vars, tokens, and other sensitive patterns; configurable patterns
- [TASK-2-4] Implement token usage extraction from session data — Acceptance: Token counts (input/output) and cost estimates captured

**Dependencies**: Phase 1 (models for transcript data)

**Exit criteria**: Transcript extractor produces valid checkpoint transcript data from real session files, sensitive data is redacted.

### Phase 3: Gateway Integration

**Goal**: Hook checkpoint capture into the gateway's git push handler for atomic capture alongside commits.

**Tasks**:
- [TASK-3-1] Add checkpoint capture hook to gateway push endpoint — Acceptance: Checkpoint creation triggered on successful git push, checkpoint contains commit SHA
- [TASK-3-2] Integrate session context (container_id, role, issue_number) into checkpoint — Acceptance: Session metadata from SessionManager included in checkpoint
- [TASK-3-3] Add checkpoint branch management (create/push to `egg/checkpoints/v1`) — Acceptance: Checkpoints pushed to dedicated branch, branch created if not exists
- [TASK-3-4] Implement graceful degradation if checkpoint fails — Acceptance: Push succeeds even if checkpoint capture fails; failures logged but not blocking

**Dependencies**: Phase 1 (schema/models), Phase 2 (transcript capture)

**Exit criteria**: Git push operations create checkpoints atomically, checkpoints are stored in dedicated branch.

### Phase 4: CLI and Retrieval

**Goal**: Provide CLI commands for browsing and querying checkpoints.

**Tasks**:
- [TASK-4-1] Add `egg-checkpoint list` command — Acceptance: Lists checkpoints by branch/issue, shows commit SHA, timestamp, agent role
- [TASK-4-2] Add `egg-checkpoint show <commit-sha>` command — Acceptance: Displays full checkpoint details for a commit
- [TASK-4-3] Add `egg-checkpoint browse --issue <number>` command — Acceptance: Filters checkpoints by issue number across sessions

**Dependencies**: Phase 3 (checkpoints are being created)

**Exit criteria**: CLI commands work for browsing, filtering, and displaying checkpoints.

### Phase 5: Contract Integration

**Goal**: Link checkpoints to SDLC contracts for traceability.

**Tasks**:
- [TASK-5-1] Add checkpoint reference to task commit field in contract schema — Acceptance: Task can optionally include checkpoint_id alongside commit SHA
- [TASK-5-2] Update agent execution model to include checkpoint references — Acceptance: AgentExecutionModel includes checkpoint_ids for each commit
- [TASK-5-3] Add checkpoint link to audit log entries — Acceptance: Audit entries for commit-related actions include checkpoint reference

**Dependencies**: Phase 3 (checkpoints exist to reference)

**Exit criteria**: Contracts link to checkpoints, enabling full traceability from task to reasoning.

## Test Strategy

- **Unit tests**:
  - Schema validation with valid/invalid fixtures
  - Pydantic model serialization/deserialization
  - Transcript extraction from sample JSONL files
  - Sensitive data redaction patterns
  - Checkpoint loader atomic writes
- **Integration tests**:
  - Gateway push with checkpoint capture (mock session data)
  - Checkpoint branch creation and push
  - CLI commands with sample checkpoint data
- **Manual testing**:
  - End-to-end: Make a commit as agent, verify checkpoint created
  - Browse checkpoints via CLI
  - Verify sensitive data is redacted in captured transcripts

## Rollback Plan

If something goes wrong during deployment:

1. **Feature flag**: Add `CHECKPOINT_ENABLED=false` environment variable to disable checkpoint capture without code changes
2. **Gateway rollback**: The checkpoint hook is additive to the push endpoint; if it causes issues, revert the gateway changes
3. **Branch cleanup**: If the `egg/checkpoints/v1` branch has corrupt data, delete and recreate it (checkpoints are additive, not critical path)
4. **Schema versioning**: Schema includes version field for future migrations; old checkpoints remain readable

```bash
# Disable checkpoints temporarily
export CHECKPOINT_ENABLED=false

# Delete checkpoint branch if needed
git push origin --delete egg/checkpoints/v1

# Rollback gateway to previous commit
cd gateway && git checkout HEAD~1 -- gateway.py
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Checkpoint capture slows down git push | Low | Medium | Async capture after push returns; graceful degradation |
| Claude Code JSONL format changes | Low | High | Version detection in extractor; fallback to empty transcript |
| Sensitive data leaks in transcripts | Medium | High | Comprehensive redaction with pattern matching; human review of initial checkpoints |
| Checkpoint branch grows unbounded | Medium | Low | No retention policy per human decision; monitor and add later if needed |
| Concurrent push race condition | Low | Low | Atomic file operations with temp + rename pattern |

## Migration Notes

No database migrations required. Changes are additive:

- **New schema file**: `.egg/schemas/checkpoint.schema.json`
- **New models**: `shared/egg_contracts/checkpoints.py`
- **New CLI**: `bin/egg-checkpoint`
- **Gateway modification**: Additive hook in push endpoint
- **Contract schema update**: Optional `checkpoint_id` fields (backwards compatible)

The `egg/checkpoints/v1` branch is created on first checkpoint. Existing repositories continue to work without checkpoints until the gateway is updated.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add Checkpoints: capture agent session context in Git"
  description: |
    Implements the Checkpoints feature for capturing agent session context as
    versioned data in Git. When agents commit code, a checkpoint captures the
    full session (transcript, tool calls, token usage) alongside the commit.

    This enables traceability, faster reviews, and better handoffs by preserving
    the reasoning behind each change. Checkpoints are stored in a dedicated
    branch (egg/checkpoints/v1) and linked to commits and SDLC contracts.

    See issue #500 for background and design decisions.
phases:
  - id: 1
    name: Schema and Models
    goal: Define checkpoint data structure with JSON schema and Pydantic models
    tasks:
      - id: TASK-1-1
        description: Create checkpoint JSON schema
        acceptance: Schema validates sample checkpoints with all required fields
        files:
          - .egg/schemas/checkpoint.schema.json
      - id: TASK-1-2
        description: Create Pydantic models for checkpoint data
        acceptance: Models match JSON schema, unit tests pass
        files:
          - shared/egg_contracts/checkpoints.py
      - id: TASK-1-3
        description: Add checkpoint loader with atomic write support
        acceptance: Loader can read/write checkpoints with temp file + rename pattern
        files:
          - shared/egg_contracts/checkpoint_loader.py
  - id: 2
    name: Transcript Capture
    goal: Implement transcript extraction from Claude Code with redaction
    tasks:
      - id: TASK-2-1
        description: Create transcript extractor for Claude Code JSONL format
        acceptance: Extractor parses Claude Code session files and returns structured data
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-2-2
        description: Implement tool call extraction with parameters and results
        acceptance: Tool calls captured with name, parameters, and result summaries
        files:
          - shared/egg_contracts/transcript_extractor.py
      - id: TASK-2-3
        description: Add sensitive data redaction
        acceptance: Redactor removes/masks env vars, tokens, and sensitive patterns
        files:
          - shared/egg_contracts/redactor.py
      - id: TASK-2-4
        description: Implement token usage extraction from session data
        acceptance: Token counts and cost estimates captured
        files:
          - shared/egg_contracts/transcript_extractor.py
  - id: 3
    name: Gateway Integration
    goal: Hook checkpoint capture into gateway git push for atomic capture
    tasks:
      - id: TASK-3-1
        description: Add checkpoint capture hook to gateway push endpoint
        acceptance: Checkpoint creation triggered on successful git push
        files:
          - gateway/gateway.py
          - gateway/checkpoint_handler.py
      - id: TASK-3-2
        description: Integrate session context into checkpoint
        acceptance: Session metadata from SessionManager included in checkpoint
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-3-3
        description: Add checkpoint branch management
        acceptance: Checkpoints pushed to dedicated branch, branch created if not exists
        files:
          - gateway/checkpoint_handler.py
      - id: TASK-3-4
        description: Implement graceful degradation if checkpoint fails
        acceptance: Push succeeds even if checkpoint capture fails
        files:
          - gateway/gateway.py
  - id: 4
    name: CLI and Retrieval
    goal: Provide CLI commands for browsing and querying checkpoints
    tasks:
      - id: TASK-4-1
        description: Add egg-checkpoint list command
        acceptance: Lists checkpoints by branch/issue with metadata
        files:
          - bin/egg-checkpoint
          - shared/egg_contracts/checkpoint_cli.py
      - id: TASK-4-2
        description: Add egg-checkpoint show command
        acceptance: Displays full checkpoint details for a commit
        files:
          - shared/egg_contracts/checkpoint_cli.py
      - id: TASK-4-3
        description: Add egg-checkpoint browse --issue command
        acceptance: Filters checkpoints by issue number
        files:
          - shared/egg_contracts/checkpoint_cli.py
  - id: 5
    name: Contract Integration
    goal: Link checkpoints to SDLC contracts for traceability
    tasks:
      - id: TASK-5-1
        description: Add checkpoint reference to task commit field
        acceptance: Task can include checkpoint_id alongside commit SHA
        files:
          - .egg/schemas/contract.schema.json
          - shared/egg_contracts/models.py
      - id: TASK-5-2
        description: Update agent execution model to include checkpoint references
        acceptance: AgentExecutionModel includes checkpoint_ids for each commit
        files:
          - shared/egg_contracts/models.py
      - id: TASK-5-3
        description: Add checkpoint link to audit log entries
        acceptance: Audit entries for commit actions include checkpoint reference
        files:
          - shared/egg_contracts/audit.py
```

---

*Authored-by: egg*
