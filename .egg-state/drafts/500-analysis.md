# Analysis: Checkpoints - Capture Agent Session Context as Versioned Data in Git

> Issue: #500 | Phase: refine

## Problem Statement

Agent sessions are ephemeral. When an agent completes work and the session ends, the prompts, reasoning, and decision-making context that produced the code disappear. Git preserves *what* changed (the diffs), but nothing about *why* those changes were made.

This context loss compounds as agents generate hundreds or thousands of lines per session. Without shared context:
- Agents retrace steps and duplicate reasoning across sessions
- Token waste increases as agents rediscover constraints already established
- Code reviews lack the intent and constraints behind decisions
- Handoffs between agents or sessions require replaying prompts
- Audit trails are incomplete for AI-generated code provenance

**Current state**: egg versions SDLC contracts, audit logs, and phase decisions in `.egg-state/`, but the full reasoning trail (transcripts, tool calls, files read) is not captured.

**Desired outcome**: Every agent-generated commit links to a structured checkpoint containing the full session context that produced it, stored as versioned data in Git.

## Current Behavior

### Session Management (`gateway/session_manager.py:74-138`)

The gateway already tracks sessions with:
- `session_token_hash`: SHA-256 hash for authentication
- `container_id`: Docker container ID
- `container_ip`: Source IP for verification
- `mode`: Repository visibility (private/public)
- `agent_role`: Role set by workflow context
- `created_at`, `last_seen`, `expires_at`: Timestamps

Sessions persist to `~/.egg-state/sessions/sessions.json` with atomic writes.

### Audit System (`shared/egg_contracts/audit.py:14-47`)

Audit entries capture:
- `timestamp`: When action occurred
- `actor`: Who performed the action
- `role`: IMPLEMENTER, REVIEWER, HUMAN, SYSTEM
- `action`: CREATE, UPDATE, DELETE, TRANSITION
- `field_path`: JSON path of modified field
- `old_value`/`new_value`: State changes
- `reason`: Optional justification

Audit logs are stored within the Contract model and committed to `.egg-state/contracts/{issue}.json`.

### Git Wrapper (`sandbox/scripts/git:117-199`)

All git operations route through the gateway sidecar via REST API. The gateway:
- Authenticates requests using session tokens
- Enforces branch ownership policies (egg-prefixed branches)
- Intercepts push operations at `POST /api/v1/git/push`

This interception point provides a natural hook for checkpoint creation.

### Claude Code Session Data

Claude Code stores conversation data in `~/.claude/projects/{project-path}/{session-id}.jsonl`. Each line contains:
- Message content (user prompts, assistant responses)
- Tool use invocations and results
- Token usage per message
- Session metadata (version, branch, timestamp)

This is the richest source of transcript data available.

## Constraints

### Technical Constraints
- **Storage size**: Full transcripts can be 100KB-1MB+ per session. A busy repository could accumulate gigabytes of checkpoint data.
- **Branch isolation**: Main branch must stay clean; checkpoint data should not pollute working branches.
- **Atomicity**: Checkpoint creation must be atomic with the associated commit to ensure consistency.
- **Performance**: Checkpoint capture cannot significantly slow down git push operations.
- **Claude Code coupling**: Relying on `~/.claude/projects/` creates a dependency on Claude Code's internal storage format, which may change.

### Security Constraints
- Session transcripts may contain sensitive discussion of security vulnerabilities, credentials mentioned in error messages, or private context.
- Checkpoint storage must have appropriate access controls.

### Architectural Constraints
- Gateway sidecar holds credentials; container has no direct git access.
- Existing `.egg-state/` patterns use JSON with atomic file writes.
- Contract schema must remain stable (versioned at 1.0).

### Dependencies
- Claude Code session format (JSONL in `~/.claude/projects/`)
- Gateway git push interception
- Branch ownership policies
- Existing audit/contract infrastructure

## Options Considered

### Option A: Gateway Hook with Dedicated Branch

**Approach**: Hook checkpoint creation into the gateway's git push handler. Store checkpoints as JSON files in a dedicated orphan branch (`egg/checkpoints/v1`).

**Capture mechanism**:
1. On successful git push through gateway, capture checkpoint data
2. Read transcript from `~/.claude/projects/{session-id}.jsonl`
3. Write checkpoint JSON to `checkpoints/{repo}/{issue}/{commit-sha}.json`
4. Push to `egg/checkpoints/v1` branch (separate from main)

**Storage format**:
```json
{
  "version": "1.0",
  "checkpoint_id": "chkpt-abc123",
  "commit_sha": "abc123def456",
  "created_at": "2026-02-10T22:30:00Z",
  "session": {
    "session_id": "03fbf920-b347-4a61-bffc-61879e2be931",
    "container_id": "container-xyz",
    "agent_role": "implementer",
    "mode": "private"
  },
  "context": {
    "issue_number": 500,
    "phase": "implement",
    "branch": "egg/issue-500"
  },
  "transcript": {
    "messages": [...],
    "tool_calls": [...],
    "files_touched": ["src/foo.py", "tests/test_foo.py"]
  },
  "metrics": {
    "input_tokens": 45000,
    "output_tokens": 12000,
    "duration_seconds": 1800
  }
}
```

**Pros**:
- Natural hook point (gateway already intercepts all pushes)
- Dedicated branch keeps main branch clean
- Full data capture with commit SHA linkage
- Append-only audit log in Git (immutable history)
- Leverages existing gateway session tracking

**Cons**:
- Couples to Claude Code's `~/.claude/projects/` format
- Requires gateway to read container filesystem
- Dedicated branch grows indefinitely (needs pruning strategy)
- Push latency increases (checkpoint creation on push path)

### Option B: Post-Commit Hook with Git Notes

**Approach**: Use a Claude Code post-commit hook (`~/.claude/hooks/PostCommit`) to attach checkpoint data as Git notes to each commit.

**Capture mechanism**:
1. Configure hook at `~/.claude/hooks/PostCommit`
2. On each commit, extract transcript summary and attach as Git note
3. Notes stored in `refs/notes/checkpoints` namespace

**Storage format**: Git notes attached to commit objects, containing checkpoint JSON.

**Pros**:
- Native Git feature (notes designed for metadata attachment)
- Checkpoint travels with commit during cherry-pick/rebase
- No separate branch to manage
- Hook runs in container context (easy filesystem access)

**Cons**:
- Git notes have size limits (practical limit ~1MB)
- Notes not fetched by default (`git fetch origin refs/notes/*`)
- Hook runs per commit, not per push (more granular than needed?)
- Claude Code hook format/stability unclear
- Notes don't version history well (notes can be amended)

### Option C: Anthropic API Proxy Instrumentation

**Approach**: Instrument the gateway's Anthropic API proxy to capture all API traffic, building transcripts from the raw request/response stream.

**Capture mechanism**:
1. Proxy logs all requests/responses to Anthropic API
2. Session manager correlates requests to container sessions
3. On push, aggregate logged API traffic into checkpoint

**Pros**:
- Format-independent (doesn't rely on Claude Code internals)
- Captures exact API payloads (most accurate transcript)
- Gateway already handles Anthropic API routing
- Could support non-Claude-Code agents in future

**Cons**:
- Significant implementation complexity
- Must handle streaming responses, tool use, etc.
- Adds latency to API path (logging overhead)
- Requires correlation logic between API calls and git operations
- Privacy concerns with logging all API traffic

### Option D: Hybrid - Summary on Push, Full Transcript on Demand

**Approach**: Capture lightweight summary on push (files touched, token counts, phase); store full transcript separately with on-demand retrieval.

**Capture mechanism**:
1. On push, write lightweight summary checkpoint
2. Full transcript remains in `~/.claude/projects/` with pointer in checkpoint
3. CLI command to "hydrate" checkpoint with full transcript when needed

**Pros**:
- Minimal push latency impact
- Storage-efficient by default
- Full data available when needed for investigation
- Decouples capture from storage concerns

**Cons**:
- Full transcript may be lost if container is destroyed before hydration
- Two-tier system adds complexity
- Incomplete checkpoints unless explicitly hydrated
- `~/.claude/projects/` is ephemeral (container-local)

## Recommended Approach

**Option A: Gateway Hook with Dedicated Branch** is recommended.

**Rationale**:

1. **Natural integration point**: The gateway already intercepts all git pushes and has session context. Adding checkpoint creation here requires minimal architectural change.

2. **Atomic capture**: By hooking into the push handler, we can ensure checkpoint creation is transactional with the push itself. If checkpoint creation fails, the push can be retried.

3. **Clean separation**: A dedicated branch (`egg/checkpoints/v1`) keeps checkpoint data out of working branches while maintaining full Git history and immutability.

4. **Leverages existing patterns**: egg already uses JSON files with atomic writes for contracts and sessions. Checkpoints follow the same pattern.

5. **Extensible**: The checkpoint schema can evolve with versioning. New data sources (beyond Claude Code) can be added later.

**Mitigation for key concerns**:

- **Claude Code coupling**: Accept this for MVP. Abstract transcript extraction behind an interface for future providers.
- **Storage growth**: Implement retention policy (e.g., compress after 30 days, prune after 90 days). Consider optional summarization mode.
- **Push latency**: Make checkpoint creation async where possible. Write checkpoint after successful push confirmation.

**Implementation breakdown**:

1. **Phase 1**: Define checkpoint schema, implement capture in gateway push handler
2. **Phase 2**: Create checkpoint branch management (`egg/checkpoints/v1` initialization, push logic)
3. **Phase 3**: CLI for browsing/querying checkpoints (`egg checkpoint list`, `egg checkpoint show`)
4. **Phase 4**: Integrate with contracts (link checkpoints to issue/phase transitions)
5. **Phase 5**: Add retention policies and storage management

## Open Questions

### Storage Strategy Decision

The checkpoint storage strategy significantly impacts implementation complexity and operational overhead.

```
egg-contract add-decision --question "Which storage strategy should checkpoints use?" \
  --options "Dedicated branch (egg/checkpoints/v1)" "Git notes on commits" "External storage with Git pointers" --format markdown
```

**Decision: checkpoint-storage-strategy**

- [ ] **Dedicated branch (egg/checkpoints/v1)** - Checkpoints stored as JSON files in an orphan branch. Keeps main clean, full Git history, but branch grows over time.
- [ ] **Git notes on commits** - Native Git feature, travels with commits, but size-limited and not fetched by default.
- [ ] **External storage with Git pointers** - Store in S3/GCS with commit-sha pointers in Git. Scalable but adds external dependency.
- [ ] Other (explain in reply)

### Capture Granularity

The issue mentions options of per-commit, per-push, or per-session granularity.

```
egg-contract add-decision --question "What granularity should checkpoints capture?" \
  --options "Per-commit" "Per-push" "Per-session" --format markdown
```

**Decision: checkpoint-granularity**

- [ ] **Per-commit** - One checkpoint per commit. Finest granularity, highest storage, most traceable.
- [ ] **Per-push** - One checkpoint per push. May include multiple commits. Moderate storage.
- [ ] **Per-session** - One checkpoint per agent session. Coarsest granularity, may span multiple pushes.
- [ ] Other (explain in reply)

### Transcript Source

The transcript is the most valuable data but also the most complex to capture reliably.

```
egg-contract add-decision --question "What should be the primary transcript source?" \
  --options "Claude Code JSONL files" "Anthropic API proxy instrumentation" "Both with fallback" --format markdown
```

**Decision: transcript-source**

- [ ] **Claude Code JSONL files** - Read from `~/.claude/projects/`. Simple, full data, but couples to Claude Code format.
- [ ] **Anthropic API proxy instrumentation** - Capture from API traffic. Format-independent but complex to implement.
- [ ] **Both with fallback** - Try Claude Code first, fall back to API logs. Most robust but most complex.
- [ ] Other (explain in reply)

### Open-ended Questions

- **Retention policy**: How long should full checkpoints be retained before compression/summarization/pruning? What compliance requirements exist?
- **Multi-agent scenarios**: When multiple agents collaborate on an issue, should checkpoints be linked by issue number, branch name, or both?
- **Sensitive data**: Should checkpoints be filtered to redact potentially sensitive content (API keys in error messages, etc.)?

---

*Authored-by: egg*
