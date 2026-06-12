# Served Coordination State

> Pipeline coordination state has **one logical home and a served read path** —
> not N replicas synchronized by ad-hoc git choreography and prompt prose.
> This page records the three-clause invariant adopted in
> [#3077](https://github.com/jwbron/egg/issues/3077) and names the mechanism
> that enforces each clause. Eleven-plus incidents
> ([#3068](https://github.com/jwbron/egg/issues/3068)/[#3073](https://github.com/jwbron/egg/issues/3073),
> [#3072](https://github.com/jwbron/egg/issues/3072),
> [#3076](https://github.com/jwbron/egg/issues/3076),
> [#3033](https://github.com/jwbron/egg/issues/3033),
> [#3016](https://github.com/jwbron/egg/issues/3016),
> [#2629](https://github.com/jwbron/egg/issues/2629),
> [#2626](https://github.com/jwbron/egg/issues/2626)/[#2625](https://github.com/jwbron/egg/issues/2625),
> [#2972](https://github.com/jwbron/egg/issues/2972)/[#2488](https://github.com/jwbron/egg/issues/2488),
> [#3005](https://github.com/jwbron/egg/issues/3005),
> [#2548](https://github.com/jwbron/egg/issues/2548)/[#2755](https://github.com/jwbron/egg/issues/2755),
> [#3117](https://github.com/jwbron/egg/issues/3117)) traced to ad-hoc
> replication of coordination state across orchestrator/gateway/per-agent
> worktrees, origin, and per-role/slice branches. The control group — served
> channels (contract API, consensus signals over HTTP, message wait-loops) —
> has produced zero such incidents. The invariant below is the structural fix
> that retires the failure class.

## Slice landings (status as of slice-6)

This page describes the **final shape** of the #3077 invariant. The epic was
landed in six slices, all of which have now shipped to `main`; every enforcing
mechanism cited below exists. The table records each slice landing so a reader
can trace which mechanism arrived in which slice.

| Mechanism | Cited under | Status |
|-----------|-------------|--------|
| Wrapper `sync_to_proposals()` per-SHA outcome recording + "worktree NOT synced" banner | Clause 2 | **Shipped** (slice-1) |
| Empty-delta caution cross-reference to the wrapper banner | Clause 2 | **Shipped** (slice-1) |
| `mcp__brc__read_peer_artifact` (live store + on-disk merge, `live` flag) | Clause 1 | **Shipped** (predates #3077) |
| Contract reads via `mcp__sdlc__show_contract` / `mcp__task__*` / `mcp__phase__get_context` | Clause 1 | **Shipped** (predates #3077) |
| `mcp__progress__query_status` / `mcp__progress__emit` HTTP-backed reads | Clause 1 | **Shipped** (predates #3077) |
| `shared/egg_contracts/artifact_spec.py` declarative artifact registry | Clause 3 | **Shipped** (slice-2) |
| `shared/egg_contracts/tests/test_artifact_spec.py` spec-consistency tests | Clause 3 | **Shipped** (slice-2) |
| `handle_consensus_propose_signal` generalisation to every spec-registered artifact | Clause 3 | **Shipped** (slice-3) |
| Gateway `POST /api/v1/artifact/get` + `orchestrator/routes/artifacts.py` | Clause 1 | **Shipped** (slice-4) |
| Sandbox `egg-artifact` verb | Clauses 1, 3 | **Shipped** (slice-4) |
| `orchestrator/tests/test_prompt_sync_ratchet.py` no-sync-mechanics ratchet | Clause 2 | **Shipped** (slice-5) |
| Fail-loud memory-backend signal + Redis restart-semantics test | Wipe-semantics | **Shipped** (slice-6) |

The clause descriptions below are written in present tense. With all six
slices shipped, every "Enforcing mechanisms" bullet now describes a
current-state claim rather than a design target. The slice column above
records when each mechanism landed.

## The Three-Clause Invariant

These clauses are **normative**. A new mechanism MUST satisfy all three, or
explicitly justify the exception in this page before it ships.

### Clause 1 — Served reads

> Everything an agent **consumes but does not own** MUST be read via a served
> channel (orchestrator HTTP, gateway HTTP, in-process MCP verb backed by
> either). Agents MUST NOT read peer state from a worktree replica or by
> reconstructing a disk path.

**Enforcing mechanisms:**

- **BRC transcript reads** — `mcp__brc__read_peer_artifact` merges the live
  message store with the on-disk `.egg-state/brc-history/` log; the agent
  receives both sources through a single MCP verb and never opens the disk
  file directly. The verb returns `live: true|false` so callers can tell
  when the live source was unreachable rather than silently treating an
  empty result as "peer has not proposed." See
  [`sandbox/egg_agent_tools/tools/brc.py`](../../sandbox/egg_agent_tools/tools/brc.py)
  and the orchestrator-side history handler in
  [`orchestrator/routes/messages.py`](../../orchestrator/routes/messages.py).
- **Contract reads** — `mcp__sdlc__show_contract`,
  `mcp__task__*`, `mcp__phase__get_context` all read the live orchestrator
  copy of the contract. The contract-review criteria explicitly direct
  reviewers AWAY from the `.egg-state/contracts/` checkout snapshot
  (see [`shared/prompts/contract-review-criteria.md`](../../shared/prompts/contract-review-criteria.md)).
- **Committed artifact reads** — the gateway
  `POST /api/v1/artifact/get` endpoint serves committed artifact content
  by **spec-registered name** + hex-validated ref, executing
  `git show <ref>:<path>` against the authoritative repo. The endpoint is
  strict — it accepts no raw repo-path escape hatch
  ([#3077](https://github.com/jwbron/egg/issues/3077) HITL Q2) — and
  unblocks [#3002](https://github.com/jwbron/egg/issues/3002) by removing
  the implicit shared-object-store assumption that made `git show` work on
  a single-host deployment but break on a split-object-store runtime
  (GKE). The sandbox-side helper is the `egg-artifact` verb; the
  reviewer-facing prompt embeds rendered `git show <sha>:<path>` commands
  derived from the same spec.
- **Pipeline status / progress** — `mcp__progress__query_status` and
  `mcp__progress__emit` go through HTTP rather than scraping any
  orchestrator-side state file.

### Clause 2 — Harness-performed deterministic sync

> Where a real checkout is genuinely needed (the BRC reviewer's worktree
> must contain the producer's proposed tree so static analysis and tests
> can run against it), the sync MUST be performed by the **harness**
> (wrapper bash, gateway-side action) and MUST fail loudly. Agents MUST
> NOT be instructed to run `git fetch` / `git merge` / `git pull` against
> peer state in prompt prose.

**Enforcing mechanisms:**

- **Wrapper-performed sync** — the consensus wrapper calls
  `sync_to_proposals()` on every BRC event before invoking the agent. Each
  per-producer SHA's merge outcome is recorded; on any failure (unresolved
  SHA, conflicting merge, dirty tree) the wrapper prepends a
  **"worktree NOT synced"** banner to the per-event prompt. A reviewer
  whose worktree silently failed to sync can no longer trust a stale
  local diff — the banner says so. See
  [`orchestrator/consensus_wrapper.py`](../../orchestrator/consensus_wrapper.py)
  and [`orchestrator/routes/event_prompt.py`](../../orchestrator/routes/event_prompt.py).
- **Per-event prompt composer** — the per-SHA delta section embeds
  rendered `git show <sha>:<path>` commands so the reviewer reads each
  producer's contribution by SHA, not by reconstructing a path or
  fetching from origin themselves. The deltas are derived from the
  artifact spec, not from prompt-template literals.
- **No-sync-mechanics-in-prompts ratchet** —
  `orchestrator/tests/test_prompt_sync_ratchet.py` scans agent-facing
  prompt sources (`shared/prompts/*.md`, prompt-constructing template
  strings in `orchestrator/routes/event_prompt.py`) for instructional
  occurrences of `git fetch` / `git merge` / `git pull` and brc-history
  disk paths. Additions to its allowlist require editing the test — that
  is the ratchet. Rendered `git show` commands are intentionally
  exempt: they are served-read companions, not sync mechanics.

### Clause 3 — One artifact spec

> One declarative registry — `shared/egg_contracts/artifact_spec.py` —
> MUST be the single source of truth for every coordination artifact's
> `name → path template → phase → producer role → consumer roles`
> mapping. Propose-time validation, the served read API, and prompt text
> MUST derive from or be asserted-consistent with the spec.

**Enforcing mechanisms:**

- **Declarative registry** — `shared/egg_contracts/artifact_spec.py`
  is the single table. Each entry binds an artifact name to its phase,
  path template, producer role, and consumer roles. Adding a new
  artifact means adding a row to the spec, not editing a gate dict, a
  validator helper, and a prompt template independently.
- **Mandatory consistency tests** —
  `shared/egg_contracts/tests/test_artifact_spec.py` pins every
  pre-existing hardcoding (gateway phase gates in
  `gateway/phase_filter.py`, the local mirror in
  `shared/egg_restrictions/phase_patterns.py`, the `_get_draft_path`
  helper, and the prompt literals in `orchestrator/routes/pipelines.py`)
  to the spec. Drift between the spec and any of these surfaces fails
  the test — the spec cannot quietly become a fourth replica.
- **Spec-derived propose validation** — `handle_consensus_propose_signal`
  (`orchestrator/routes/signals.py`) generalises the previous plan-only
  presence check to every refine/plan producer that owns a
  spec-registered artifact. Plan-specific parseability and role/files
  alignment remain as plan-artifact extensions; the `branch_verified`
  graceful-degradation path ([#3081](https://github.com/jwbron/egg/issues/3081))
  is preserved. `no_changes_needed` proposes pass through unchanged.
- **Served reads keyed by spec name** — the gateway artifact-read
  endpoint accepts only spec-registered names. Path-guessing is the
  disease this issue retires; the policy surface stays small.

## Wipe semantics: designed boundary wipe vs accidental mid-phase loss

The message store and the BRC consensus tracker are wiped at **two**
points. These MUST NOT be conflated when reasoning about durability:

| Wipe | Where | When | Status |
|------|-------|------|--------|
| **Designed phase-boundary wipe** | `_clear_concurrent_state()` in `orchestrator/routes/phases.py` (called from `phases.py` at the phase transitions and from `routes/pipelines.py`) | At phase transitions, **after** the brc-history persistence has captured the transcript | **Required behaviour.** This wipe must keep happening. The persisted history is the audit trail; the live message store is per-phase scratch space. |
| **Accidental mid-phase restart loss** | A mid-phase orchestrator restart on the in-memory `MessageStore` backend (`orchestrator/message_store.py` `_create_message_store()`, the `auto` → memory fallback path) | When `EGG_MESSAGE_STORE_BACKEND` is `auto` and Redis is unavailable, the in-memory backend is silently picked — and a mid-phase restart loses the running transcript | **Defect surface.** No code intends this loss. |

The bounded-durability response in slice 6 of #3077 is a **fail-loud
signal**, not a re-architecture (per HITL Q3). When `auto` resolves to
the in-memory backend in a context where BRC consensus runs, the
orchestrator emits an error-level structured log with a stable marker
and sets a health-visible degraded flag. Explicit
`EGG_MESSAGE_STORE_BACKEND=memory` (a dev/test choice) stays at warning
level with no degraded flag. The `auto` selection semantics — Redis
when available, in-memory fallback — are unchanged; deeper durability
work stays in the [#3070](https://github.com/jwbron/egg/issues/3070)
lineage. The Redis path's restart semantics are pinned by
`orchestrator/tests/test_redis_message_store.py`: mid-phase messages
survive a store re-instantiation against the same Redis (simulated
orchestrator restart), while the designed `_clear_concurrent_state()`
phase-boundary wipe still clears state. Both wipe semantics are named
explicitly in the test ids/docstrings so a future regression cannot
quietly trade one for the other.

## Mechanism map

| Mechanism | Module / path | Clause it enforces |
|-----------|---------------|--------------------|
| `mcp__brc__read_peer_artifact` (live store + on-disk merge, `live` flag) | `sandbox/egg_agent_tools/tools/brc.py`, `orchestrator/routes/messages.py` (`get_brc_transcript`) | Clause 1 |
| Wrapper `sync_to_proposals()` with per-SHA outcome recording | `orchestrator/consensus_wrapper.py` | Clause 2 |
| "Worktree NOT synced" banner in the event prompt | `orchestrator/consensus_wrapper.py` (`SYNC_FAILURE_BANNERS`), `orchestrator/routes/event_prompt.py` (cross-reference text) | Clause 2 |
| Artifact spec registry | `shared/egg_contracts/artifact_spec.py` | Clause 3 |
| Spec consistency tests (gates, mirror, helper, literals) | `shared/egg_contracts/tests/test_artifact_spec.py` | Clause 3 |
| Spec-derived propose validation (all producers) | `orchestrator/routes/signals.py` `handle_consensus_propose_signal` | Clause 3 |
| Gateway artifact-read endpoint `POST /api/v1/artifact/get` (strict, name-only) | `gateway/artifact_api.py` (forwarding) + `orchestrator/routes/artifacts.py` (`git show` against the authoritative repo) | Clauses 1, 3 |
| Sandbox `egg-artifact` verb | `sandbox/scripts/egg-artifact` | Clauses 1, 3 |
| No-sync-mechanics ratchet | `orchestrator/tests/test_prompt_sync_ratchet.py` | Clause 2 |
| Fail-loud memory-backend signal + Redis restart-semantics test | `orchestrator/message_store.py`, `orchestrator/tests/test_message_store.py`, `orchestrator/tests/test_redis_message_store.py` | Wipe-semantics distinction (clause-1 prerequisite) |

## Cross-references

- [Issue #3077 — Coordination state is served, not replicated](https://github.com/jwbron/egg/issues/3077)
  is the canonical incident lineage and rationale.
- [#3002](https://github.com/jwbron/egg/issues/3002) (per-role
  worktree isolation on split-object-store runtimes) is unblocked by
  the gateway artifact-read endpoint: the rendered `git show` commands
  in review prompts previously worked only because every per-role
  worktree shared the host repo's object store. The served endpoint
  removes that implicit dependency.
- [BRC Memory Artifact](brc-memory.md) — per-role distilled memory
  written by reviewers; its scope-keying and atomic-write contract
  illustrate clause-1 thinking applied to ephemeral coordination
  state.
- [BRC Consensus Wrapper](orchestrator.md#brc-consensus-wrapper) —
  the wrapper that holds the wait, performs deterministic sync, and
  invokes the per-event agent.
- [Reviewer Sync Guide](../../shared/prompts/REVIEWER-SYNC.md) — the
  agent-facing reviewer contract; its delta-command row was rewritten
  in #3077 slice 5 from `git fetch` + `git log` instructions to
  served-reads wording so it no longer drifts back into prompt-prose
  sync mechanics.
