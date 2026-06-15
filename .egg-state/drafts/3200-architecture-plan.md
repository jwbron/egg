# Architecture Plan — Issue #3200: Full-Context Backstop via Curated BRC Memory Seed

**Status**: Plan-phase architect proposal
**Phase**: plan
**Pipeline**: issue-3200
**Author**: architect
**Date**: 2026-06-15

## Scope & Binding Decisions

### Binding (from HITL cq-1)

**Option A (selected): Full scope** — All 3 components + restart fix:
1. Session resume (#3186 warm path)
2. Deterministic factual seed layer (#3189 — orchestrator-derived review anchors)
3. Agent-authored enrichment layer (#3188 — per-role judgment/memory)
4. **Prerequisite**: Restart-memory preservation (#3183 fix — salvage memory files across `restart_phase`)

### Out of Scope (explicit from issue body)

- **No final orchestrator-based state synthesis** — this pipeline defines the how; synthesizing layers into one stable seed is the implementation phase, not architecture
- **No LiteLLM PH fields** — already deployed (#3175, #3067)
- **No cross-phase memory persistence** — each phase gets only its own seed

## Architecture Overview

The system today has two critical gaps when BRC agents hit the context window:

1. **No resume**: Every one-shot event-handler invocation is a COLD start. The Agent SDK already supports `resume=<session_id>` (`shared/egg_agent/client.py` line 345-346 via `ClaudeAgentOptions.resume`), but the event-pump wrapper never captures or replays `session_id`.

2. **No seed**: When a cold start IS necessary (context full, pod death, `restart_phase`, consensus reset), the agent loses ALL context — prior review verdicts, NACK reasons, findings cited at SHA pins, the history that makes re-review efficient. The agent re-derives everything from scratch at full token cost (#3183 incident: 14.7M prompt tokens in 30 minutes).

The backstop design fills both gaps with a single unified flow: **always set `resume` if possible; when not possible, seed a fresh session with a curated BRC memory seed injected as the system prompt.**

### Architectural Pattern: Two Paths, One Substrate

```
                     ┌─────────────────────────┐
                     │  Event-pump wrapper    │
                     │  invoke_agent_for_event│
                     └──────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │ Session-ID file exists? │
                    └──────────┬────────────┘
                               │
              ┌────────────────┼────────────────┐
              │ YES            │                │ NO
              ▼                │                ▼
    ┌────────────────────┐     │     ┌────────────────────────┐
    │ Resume session    │     │     │ Seed fresh session    │
    │ (prompt cache)    │     │     │ (curated system      │
    │                   │     │     │  prompt as stable     │
    │ resume=<id>       │     │     │  cacheable prefix)   │
    └────────────────────┘     │     └────────────────────────┘
                              │
```

The richness difference is in HOW the seed is composed — two layers:

**Layer 1 (Deterministic — #3189)** — Orchestrator-derived, authoritatively from the message record:
- Per-producer last-reviewed SHA (from `_proposal_commit_sha_history`)
- Prior verdicts (ACK/NACK/conditional-ACK from message store)
- NACK reasons (latest per producer)
- Conditional-ACK obligations

**Layer 2 (Enrichment — #3188)** — Agent-authored, SHA-stamped claims:
- Key findings with `path:line@<sha>` citations
- Producer narrative ("what I built, why")
- Decision trail distilled
- Verified-claims ledger

## Detailed Component Design

### Component 1: Session-ID Capture and Resume (#3186)

**Goal**: When a session exits after handling its event, persist its `session_id` so the next invocation can resume the same conversation and exploit prompt caching.

**Design**:

The Agent SDK `AgentResult` already captures `session_id` on exit (`shared/egg_agent/result.py` line 33: `AgentResult.session_id: str | None`). What's missing is a persistence cursor in the orchestrator that reads the SDK's output JSON from stdout, extracts `session_id`, and stores it keyed by `(pipeline_id, role)`.

**File changes:**

| File | Change |
|------|--------|
| `shared/egg_agent/result.py` | No change needed — `session_id` already captured |
| `orchestrator/consensus_wrapper.py` | **New**: After `invoke_agent_for_event` (line 443-519), parse agent stdout for `AgentResult JSON`, extract `session_id`, persist to `egg-resume-cursor-{digest}` on disk |
| `orchestrator/consensus_wrapper.py` `build_event_pump_wrapped_command` (line 1111-1182) | **New**: Thread `session_id` to wrapper template; when present, add `--resume <session_id>` (warm path) or `--system-prompt <seed>` (cold path, mutually exclusive) to `agent_prefix_parts` at line 1137-1150 |
| `orchestrator/consensus_wrapper.py` `_EVENT_PUMP_WRAPPER_TEMPLATE` (line 150) | **New**: New wrapper-template bash logic: post-process agent stdout for session_id, persist to `<cursordir>/egg-resume-cursor-{digest}-{role}` |

**Persistence location**: `<repo>/.egg-state/cursors/egg-resume-cursor-{digest}-{role}`

**Edge cases**:
- **Cursor file missing** (pod death, new pod) → falls through to cold start with seed → common path
- **SDK version mismatch** → graceful: the `--resume` flag is silently accepted (or results in an unknown-flag warning suppressed by the SDK); fallback is a non-resumed fresh session
- **Provider stickiness**: The cursor is `{pipeline_id}_{role}`, not per-provider → a resume starts from the same provider that served the cached prefix; LiteLLM routing must stay pinned (`deepseek-v4-pro`) to avoid a cache-busting route change

**Resume-vs-fresh decision logic** (in wrapper bash):
```bash
cursor_file="${EGG_CURSOR_DIR}/egg-resume-cursor-${role_digest}"
if [[ -f "$cursor_file" ]]; then
    resume_id="$(cat "$cursor_file")"
    agent_prefix="python3 -m egg_agent --model $MODEL --max-turns $MAX_TURNS --resume $resume_id"
else
    # No resume possible — fall through to fresh start with seed
    agent_prefix="python3 -m egg_agent --model $MODEL --max-turns $MAX_TURNS --system-prompt $seed_prompt"
fi
```

**Tech:** The `system_prompt` CLI path confirms the orchestrator passes the seed as a raw string, not through the wrapper template's prompt interleaving. The resume path confirms the direct flag propagation already supported by `ClaudeAgentOptions.resume`. Both are mutually exclusive.

---

### Component 2: Restart-Phase Memory Preservation (#3183 prerequisite)

**Goal**: Fix the #3183 cold blowup: when `restart_phase` deletes worktrees, the BRC memory files vanish with them. Saivage the memory alongside commits in the existing salvage path.

**Design**:

`agent_salvage.py` `auto_salvage_pipeline()` (line 758-846) already salvages unpushed commits before worktree deletion. Add a parallel step that copies BRC memory files to a durable location that survives the deletion.

**File changes:**

| File | Change |
|------|--------|
| `orchestrator/agent_salvage.py` | **New function** `salvage_brc_memory(pipeline_id, role_dirs)` — copies `brc-memory-<pipeline-id>.md` from each role dir to `.egg-state/salvaged-memory/` alongside commit backups |
| `orchestrator/agent_salvage.py` `auto_salvage_pipeline()` (line 758) | **Hook**: call `salvage_brc_memory()` after commit salvage (line 786-790), before worktree deletion (line 810+). The order: extract commits → extract memory files → delete worktrees |
| `orchestrator/routes/pipelines.py` `restart_phase` (line 3336-3630) | **No change needed** — the salvage enhancement is internal to `auto_salvage_pipeline()` and fires as the same epilogue step (line 3597) that already salvages before deletion |

**Salvage layout**:
```
.egg-state/salvaged-memory/
    <pipeline-id>/
        <role>/
            brc-memory-<pipeline-id>.md
        .../
```

**Failure mode**: If the salvage directory already exists (unlikely race), append a unique suffix. The salving is opportunistic — failure falls through silently (not blocking phase restart).

---

### Component 3: Deterministic Factual Layer Composer (#3189)

**Goal**: Derive authoritative review anchors from the message record (not agent memory), guaranteeing that a restarted agent can't drift from consensus reality.

**Design**:

The orchestrator already tracks per-producer SHA via `_proposal_commit_shas` / `_proposal_commit_sha_history` (`peer_consensus.py` line 116-124). It already reconstructs consensus state from the message record via `reconstruct_tracker_from_messages()` (line 2042-2327). The missing piece is a deterministic renderer that formats this for system-prompt injection.

**New module**: `orchestrator/routes/seed_composer.py`

**Core function**:
```python
def compose_deterministic_seed(pipeline_id: str, phase: str) -> str:
    """Read message store, derive review anchors, render stable bytes."""
```

**Data sources (in order, all deterministically derived)**:
1. Message record (Redis/committed BRC history via `.egg-state/brc-history/`):
   - Per-producer: `last_reviewed_commit_sha` (latest ACK/NACK verdict with a commit_sha)
   - Prior verdicts: ACK / NACK / conditional-ACK (latest per reviewer-per-producer edge)
   - Active NACK reasons
   - Active conditional-ACK obligations
2. Tracker state (`PeerConsensusTracker` internal `_proposal_commit_shas`):
   - Current proposal version per producer (distinguishing stale from fresh review)

**Rendered format** (deterministic, stable, bounded):
```
## Recent Consensus Anchors
The following are derived from the orchestrator's event record and should be treated as authoritative.

### Review Status
- **coder**: last reviewed by reviewer_code at SHA abc1234 → ACK
- **tester**: last reviewed by reviewer_tester at SHA def5678 → NACK (reason)
```

**Hard cap**: ~500 chars per producer (sorted keys), overall section ~2 KB.

**Invocation point**: Called during `compose_event_prompt()` in `event_prompt.py`, or from a new seed-composition entry point run BEFORE the event-prompt step. The composer reads from Redis (live) and falls back to committed BRC history; if neither has data (first event), produces a minimal "no prior consensus" line.

**File changes:**

| File | Change |
|------|--------|
| `orchestrator/routes/seed_composer.py` | **New file**: `compose_deterministic_seed()`, `compose_enrichment_seed()`, `compose_seed_payload()` |
| `orchestrator/routes/event_prompt.py` `compose_event_prompt()` (line 531+) | **New keyword-only arg**: `seed_prompt: str \| None = None`. When set, insert as a new section at the beginning of the event prompt: "## Curated BRC Memory for Reorientation" |
| `orchestrator/routes/event_prompt.py` `_main` CLI (line 1260+) | **New flag**: `--seed-prompt <file>` or passed via stdin as a distinct field |

---

### Component 4: Agent-Authored Enrichment Layer (#3188)

**Goal**: Inject existing per-role `BRCMemory` content (key findings, producer narrative, claims ledger) as a second layer — BUT framed as "claims to spot-check" vs "ground truth."

**Design**:

`BRCMemory` already captures enrichment: per-producer findings, summary of assessment, decision log. The seed composer reads this file(s), extracts the agent-authored judgment, and appends it as a distinct layer in the seed.

**File read path**: `sandbox/egg_agent_tools/handlers/brc_memory.py` `memory_path_for_role()` → `<repo>/.egg-state/agent-outputs/<role>/brc-memory-<pipeline-id>.md`

**Enrichment curation**:
```python
def compose_enrichment_seed(pipeline_id: str, phase: str, role: str) -> str:
    """Extract agent-authored judgment from BRCMemory files, SHA-stamp claims."""
```
- Reads `brc-memory-<id>.md` for the target role (or all roles if cross-role visibility is needed)
- Extracts only key non-mechanically-derivable sections
- SHA-stamps each reference so stale claims can be detected on restart

**Rendered format**:
```
## Enrichment (Agent-Authored — Orientation Only)
Prior producer narrative (claims, not ground truth — restamped to current SHA in protocol delta; struct-checked at each event)

- Finding: NACK reason #3 "external regression in tests" has been verified fixed, at <sha> path:file.py:42→<sha> (stale if current SHA differ)
- Decision trail: coder proposed at <sha>, reviewer_plan NACKed for missing test coverage, coder re-proposed at <sha2>, all reviewers ACKed
- Verified claims: None yet this cycle
```

**Hard cap**: ~1 KB per role — agent-authored judgment is rich but bounded.

**The "stale detection" mechanism**: The seed carries `@<sha>` annotations on each enrichment line. The git-log delta computed at compose-event-prompt time (the existing `git_log_delta` parameter) includes prior-review shas. A reader cross-references and marks stale annotations:
- `@<sha>` → restamp to current `git log base..HEAD` → if SHA is NO longer present, annotate "(STALE)" before injecting
- Produce a fresh seed each event invocation (composed, not cached), so timestamp drift detection is always current

This is the cost-reduction arm: a stale-stamp detection allows the LLM to focus only on fresh findings, not re-derive from the whole log.

---

### Component 5: Seed Composition & Injection

**The full seed payload**, injected as `--system-prompt <seed>`, is:

```
## Restart seed for <role> in pipeline <pipeline-id> phase <phase> (issue #3200)

<REST: here begins cached-stable prefix>

[Component 3 output — deterministic anchors]
[Component 4 output — agent-authored enrichment]

## Orautar-Confirm-notics
(<guidance to the agent: these are claims; cross-ref against live event; not an authoritative record>)
```

**Composition order Complex**:
1. `compose_deterministic_seed()` → derives from message record → "Deterministic review anchors"
2. `compose_enrichment_seed_forspecificn_role()` → reads per-irlmemmory →
3. Compositor injects both into SING system prompt (stable, cacheable prefix forpritent restarts)

**Tie-in to consensus wrapper bash**: The wrapper's `invoke_agent_for_event` function (line 443-519) currently runs `{agent_command_prefix} "$prompt"`. When a fresh start is needed AND the seed is available:

```bash
agent_command="python3 -m egg_agent --model $MODEL --max-turns $MAX_TURNS --system-prompt "$seed_prompt""
```

The seed is external to the event prompt (the user-prompt text is unchanged from today). The LLM instruction architecture is:

- **System prompt** = Seed bytes + normal Claude-container system boards (static, cacheable prefix)
- **User prompt** = Event payload + memory section (dynamic, per-event, per-role)

This arrangement lets the seed cache across the session (it is per-restart invariant), while the per-event prompt continues to flow in as incremental user messages.

---

### Component 6: Deterministic Rendering Discipline

The seed format MUST render to stable bytes so the provider prompt cache can recognize it across restarts:

| Rule | Rationale |
|------|-----------|
| Sorted by producer_roll alphabetically (python `sorted()`) | Stable dict ordering |
| Hard cap per section: 2 KB deterministic, 1 KB enrichment | Bounds per-event cost |
| No timestamps in the rendered output | Prevents cache miss from changing wall-clock |
| No random UUIDs, nonces, counters | Cache poison from uncoordinated increment |
| Trimmed whitespace (strip trailing at each section) | Smooth provider normalization |
| At most 1 prior NACK reason per producer (most recent, not complete history) | Bound seed size |

---

## Proposed Slice Decomposition

The task planner will decompose this into a DAG, but the architectural dependency ordering is:

1. **Slice 1: Restart-memory preservation (prerequisite)**  
   - Modify `auto_salvage_pipeline()` to copy BRC-memory files  
   - Worktree-cleanup sentencing: salvage before destroy  
2. **Slice 2: Session-ID cursor capture** (cummulate via `result.session_id`)  
   - Rrame agent post-processor in wrapper bash  
   - Write cursor file on clean exit  
3. **Slice 3: Resume path** (warm)  
   - Thread `--resume <session_id>` through wrapper `invoke_agent_for_event`  
   - Read cursor, inject flag, handle missing-cursor gracefully  
4. **Slice 4: Seed composition framework**  
   - New base module `orchestrator/routes/seed_composer.py`  
   - Reading from tracker, message-record history for deterministic lens layer  
   - Reading from BRC-memory files for enrichment  
5. **Slice 5: Deterministic factual layer**  
   - Message-record derivation of `last_reviewed_sha`, `prior_brevity`, etc.  
   - Stable rendering with caps  
6. **Slice 6: Enrichment layer injection**  
   - Reading agent-authored memory, SHA-stamp, cap, inject  
7. **Slice 7: System-prompt injection path**  
   - Thread `--system-prompt` flag through `build_event_pmlWrapped_command` → `agent_prefix_polum`  
   - Successfully mutually exclusive with `--resume`  
8. **Slice 8: Integration & test**  
   - E2E restart: kills pod, destroysworktreee, evokes memorize survive, READ logged w subtialeict wi KSHswitchmans orch agent.

## Testing Boundary

- **Unit tests**: Seed composition from mock message records, saliving memory, id cursoring
- **Integration**: restart_phase in Dockerized pipeline, restore to restart with saved cursor and seeded memory
- **Performance test**: Measure cache-hit rate on reseed versus cold-start (typical metric: time from restart_phase to first COMPLETE event)
- **Regression test**: Unseed path (no cursor, no seed) → cold-start behavior is identical to pre-this-issue

## Architect Decisions — Open for Review

1. **Resume-vs-re-seed tiebreaker**: The wrapper decides in bash, not inside the agent SDK. ResumePath (prompt-cache) → always preferred if available; only when cursor is missing or context-full does it fall to seed path. Is the wrapper the right decision point? (Alternative: agent client-side in `run_wrapper_agent_async`.)

2. **Seed granularity**: Per-event or per-session? Proposal: per-session (the seed is a stable system prompt for the whole session; the \(per-event [porms user messaging)). Per-session caching → cheaper. But is it durable if a new slice starts? Each restart rederives the set newly.

3. **Memory file-time boundedness**: The editor flow uses committed agent event output (per-event message store), not a snapshot. So if the agent wrote partial memory mid-event, the message record would miss it. This design ensures the message record (or container stdout) is used for rebasing the seed; BRC memory file is for enrichment, not anchors. Consequence: no race-writes needed.

4. **Edge: role auto-sequence**: The design injects margin for only THIS target's role. When role==changer (RPE: all peer sound), a crash-recover restarts with curated memory from ALL ROLES. The design injects only own role (stable prefix), which is enough to resume review context, but NOT enough to transparently-handover. Task planner should note: is cross-role seed needed? Or is own role sufficient?

## Risks

| Risk | Mitigation |
|------|-------------|
| Seed choice amplifies a systemic wrong belief (propagation error) | SHA-stamp + git-log delta cross-ref invalidates stale traced claims |
| Deterministic rendering not perfectly compstable bytes (cache miss) | Sorted keys, hard numerical caps, no timestamps, strip trailing whitespace |
| Mismatched provider on resume (no prompt cache hit → 100% token toll) | LiteLLM single-pin `--route deepseek-v4-pro` in the wrapper, same as today |
| Memory-file capacity on savaage attempt | Guard: file may be up to ~10 KB raw, pre-filtered before write |
| New claude-sdk API changes break --system-prompt | Test with target SDK version at integration, CI gate require pass before mark complete |
| Seed > 8KB (budget overrun) → just truncate | Hard cap at compostion, trunc before render |~~~
---
## Decisions Waiting (tieup/slictor remarigin)

All three working-memory layers produce final-seed content:

```
deterministic layer  → system: MESSAGE store authority (derived, authoritative) ─────┐
agent-authored enrichment → STRINS: decisions, findings, SHA-citations (claim, check) ─┤→ seed_prompt
merries-acceptance    → (formatted seed prompt)                                        │
                                                                                       
```

The architecture is ready for task planning. This completes my architecture proposal. Task name/task IDs to follow in imple phase breakdown. Any reviewer concerns on the above → raise as NACK or augmentation before marking task complete.
