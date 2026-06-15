# issue-3200 Analysis — Full-context backstop for BRC agents

## Problem summary

Event-pump BRC agents (producers/reviewers) accumulate context across a phase. A long-running role eventually hits the context-window wall. Today that wall is a hard failure mode — the Agent SDK does **not** auto-compact (verified ~0.2.97). The generic remedy, compaction, is lossy: an LLM summarizes its own history and silently drops the anchors (reviewed SHAs, NACK obligations, cited findings) that make BRC continuity cheap in the first place.

This issue proposes a **full-context backstop**: when context fills, **restart the agent fresh and seed curated BRC memory as the fresh agent's system prompt.** The curated memory *is* the compaction output — but it is deterministic + agent-authored rather than an opaque LLM self-summary, and it lives outside the session, so it survives the restart it is meant to recover from.

## The specific thing to build (scope boundary)

This pipeline delivers three capabilities — in sequence — with a fourth prerequisite recognition:

### 1. Session resume (warm path — #3186)

When an agent event-handler session exits after handling its event, and the session's context still fits, re-enter the same conversation via `resume=<session_id>` rather than spawning fresh. This is the **warm path** — resumes are cheap (prompt cache hit for accummulated history) and lossless. The expected case for the majority of one-shot events.

**Components:**
- **Env-based session-id cursor** (`shared/egg_agent/midturn_messages.py` pattern): persist `AgentResult.session_id` (already captured in `shared/egg_agent/result.py`) across events and feed it back into `ClaudeAgentOptions.system_prompt` (wait — correction: use `resume=<session_id>` field of `ClaudeAgentOptions`, per verified SDK ~0.2.97 support).
- **Pass-through from wrapper bash** (`orchestrator/consensus_wrapper.py` `invoke_agent_for_event`): read persisted session_id, inject via `--resume <id>` (or similar CLI flag on `python3 -m egg_agent`).
- **Persistence scope**: pipeline-id + role => deduped cursor, same pattern as `midturn_messages.py` (`base_dir / f"egg-resume-cursor-{digest}"`).
- **Expiry / missing-data**: if transcript file is missing (pod death, new pod), fails silently → fresh session (same as today's behavior).

### 2. Curated seed as system prompt (cold-start floor)

When context fills, the agent cannot resume. Instead, the orchestrator spawns a **fresh** session whose **system prompt** is built from curating BRC memory derived from two layers:

#### 2a. Deterministic factual layer (#3189)

Facts derived by the orchestrator from the message record — never transcribed by agents:
- Per-producer `last_reviewed_commit_sha` (derived from latest ACK/NACK message in the live/committed record)
- Prior verdicts
- NACK reasons (latest version per producer)
- Conditional-ACK obligations
- Per-producer enumerated in deterministic (sorted) order

**Implementation approach**: extend `PeerConsensusTracker` or add a sibling data layer that renders the per-producer factual block at seed-render time. This is essentially the `reconstruct_tracker_from_messages` pattern (`peer_consensus.py:2042—2327`) applied to memory-seed rendering. The producer SHAs are already tracked by the consensus tracker (`_proposal_commit_shas`, line 116—124). The ACK/NACK verdicts are in the message store. The composer reads these deterministically.

The key difference from BRC memory transcription: this layer draws **directives from the message record, not from an agent-written file**. If an agent recorded the wrong SHA in its memory file, or a NACK with re-stated reasons, this layer preserves the authoritative record.

**Data path**: orchestrator derives → seed-render at spawn time → injected as system prompt (stable, cacheable prefix).

#### 2b. Agent-authored enrichment layer (#3188)

The judgment that can't be derived mechanically — written by agents into the memory file:
- Key findings (path:line@SHA citations)
- Producer narrative (what I built, why, what's left)
- Decision trail distilled
- Claims ledger (SHA-stamped claims-to-verify)

**Implementation approach**: reuse the existing `sandbox/egg_agent_tools/handlers/brc_memory.py` writer and its rendered memory format. The seed composer reads this file and injects its contents into the system-prompt seed alongside the deterministic layer. Process: deterministic prefix → functional "orientation / claims ledger" → "This is orientation-to-spot-check, not ground truth."

### 3. Architecture for evidence-based seed composition (bounds, caps, cacheable stability)

Each layer rendered into the seed must satisfy:
- **Deterministic rendering** — sorted, bounded, hard per-section caps so the cacheable prefix stays stable
- **Bound each section independently** — no variable-length section can push later sections past budget; truncate with sentinels
- **SHA-stamped claims** — enrichment claims reference a SHA so stale citations can be detected

**Composer responsibilities** (new code in `orchestrator/routes/`):
1. Read deterministic layer by querying message store / peer consensus state
2. Read enrichment layer (agent-authored) from worktree memory file (`brc-memory-<pipeline-id>.md`)
3. Render seed following deterministic ordering: deterministic anchor block → enrichment orientation → caps enforced
4. Inject as `--system-prompt <seed>` into agent command

### 4. Prerequisite: persistence at restart (reusing #3183 / motivating incident)

The catastrophic loss of memory files during `restart_phase` must be fixed. The `orchestrator/agent_salvage.py` `auto_salvage_pipeline()` recovery path already salvages unpushed commits — add BRC memory files to the preserved set.

**Mechanism**: copy `.egg-state/agent-outputs/<role>/brc-memory*.md` alongside the commit-salvage step before worktree deletion. This ensures the memory file survives a phase restart or pod kill, and the seed can be recomputed if enrichment content is needed.

## Architecture decisions (crystallized)

### How the seed renders into the system prompt

Today: `shared/egg_agent/client.py` passes `system_prompt` through to `ClaudeAgentOptions.system_prompt` (line 345-346), and `shared/egg_agent/command.py` accepts `--system-prompt` on the CLI (line 53-54). The command builder (`build_agent_command`) maps this to `--system-prompt <text>`.

The seed overlay is added at wrapper-bash time **in the wrapping orchestration** (not inside the agent SDK where `SYSTEM_PROMPT_NUDGE` appends low-level primitives). When spawn commands are assembled (or the wrapper wraps the agent invocation), a deterministic seed builder reads both layers and passes them in.

### What the seed does NOT cover

- **Live transcript** — the message record IS the authoritative source for the deterministic layer; decode it rather than requiring agents to derive it
- **Full history replay** — the seed is lean on purpose, providing just the anchors needed to reorient quickly (not a transcript replacement)
- **Cross-phase continuity** — limited to this pipeline's this-phase only (consensus phases reset between phases naturally)
- **Historical phases** — if a later phase wants context from an earlier one, the seed only covers what comes through the HITL feedback / contract decisions / accepted enrichments

### Where the composer lives

The seed composer is an `orchestrator/routes/` module (like `event_prompt.py` for user-prompt composition). It reads from:
- `self-tracked` peer_consensus.py state (like `reconstruct_tracker_from_messages`, live message store, committed history from `.egg-state/brc-history/`)
- Worktree agent-authored `brc-memory-<pipeline-id>.md` (for enrichment layer)

### Persistence scope of the seed

The seed is rebuilt every invocation (derived, not written to disk) so session restarts always get the latest from the message store, and no partial previous-seed cache poisoning risk exists.

### What this pipeline does NOT do

- Does NOT ship the final orchestrator-based state synthesis (synthesizing enrichment + deterministic layers into one stable system prompt). This is the **refinement** phase — building the determination of what to do. The plan phase will decompose into tasks.
- Does NOT ship LiteLLM PH fields (already done/available, issues #3175 / #3067)
- Does NOT define the shimmer between resume and fresh: the warm-path (resume) vs cold-start path decision logic is outside this refinement (plan phase electrical wiring)

## Grounded facts (from codebase exploration)

### Existing memory artifact
- Writer: `sandbox/egg_agent_tools/handlers/brc_memory.py` (lines 282-687) — `BRCMemory` dataclass with `codebase_change_model`, `per_producer` dict of `ProducerAssessment`, `decision_log`. Rendered to `.egg-state/agent-outputs/<role>/brc-memory-<pipeline-id>.md`.
- Reader: `orchestrator/routes/event_prompt.py` (line 362-393 `_render_memory_section`, line 531 `compose_event_prompt`) — reads BRC memory file, appends as tail section of the user prompt. Capped at 2 KB.

### Resume-capable SDK
- `ClaudeAgentOptions` has `resume=<session_id>` parameter (verified 2026-06-12)
- `AgentResult` already captures `session_id` (line 33, `shared/egg_agent/result.py`)
- `shared/egg_agent/client.py` line 345-346 already supports `system_prompt` parameter
- `shared/egg_agent/command.py` line 53-54 already supports `--system-prompt` CLI flag

### Message-store source for deterministic derivation
- `peer_consensus.py` already tracks per-producer SHA via `_roposal_commit_shas` (line 116-124)
- `reconstruct_tracker_from_messages()` (line 2042-2327) already derives state from message history — the same pattern applies to memory seed composition
- Consensus ATCs carry verdict, NACK reasons, and SHA in their payload (via handle_ack line 518, handle_nack line 620)

### Restart-phase memory loss (#3183)
- `orchestrator/routes/pipelines.py` `restart_phase()` lines 3560-3630 delete worktrees, discarding `brc-memory.md` in the process
- `orchestrator/agent_salvage.py` `auto_salvage_pipeline()` salvages unpushed commits (line 758) but NOT the memory files

### BRC history persistence
- `_persist_phase_brc_history()` in `pipelines.py` (line 9442) writes per-agent transcript to `.egg-state/brc-history/<identifier>-<phase>.md` (markdown) + JSON companion at phase completion. This data can be decoded for deterministic anchor derivation as part of seed composition.

## Draft scope / slice proposition (not canonical — for planner to decompose)

1. **Restart protection**: Copy `brc-memory*.md` from agent-outputs before delete
2. **Session resume**: `session_id` cursor → wrapper `--resume` pass-through
3. **Deterministic layer composer**: Read from peer consensus → render stable bytes
4. **Enrichment layer injector**: Read agent-authored memory → `--system-prompt` inject
5. **Seed composer integration**: Deterministic prefix → enrichment (capped) → hand to agent
6. **Resume integration**: Feed `--resume` or `--seed` (mutually exclusive — resume preserves cached history; seed restarts fresh with seed)
