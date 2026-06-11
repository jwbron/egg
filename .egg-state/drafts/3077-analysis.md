# Analysis: Coordination state is served, not replicated — retire git/prompt choreography for agent state exchange

> Issue: #3077 | Phase: refine | Pipeline: issue-3077

## Problem Statement

Issue #3077 names a recurring failure class: **pipeline coordination state has one logical home but N physical replicas** (orchestrator worktree, gateway worktree, per-agent worktrees, origin, per-role/slice branches), synchronized by ad-hoc git choreography and prompt prose. Eleven-plus incidents (#3068/#3073, #3072, #3076, #3033, #3016, #2629, #2626/#2625, #2972/#2488, #3005, #2548/#2755, #3117) are the same bug in different channels. The control group — served channels (contract API, consensus signals over HTTP, message wait-loops) — has produced zero such incidents.

The issue proposes a three-clause invariant:

1. **Served reads** for everything an agent consumes but doesn't own.
2. **Harness-performed sync** (deterministic bash in the wrapper, not prompt prose) where a real checkout is genuinely needed.
3. **One artifact spec** from which propose-time validation, the served read API, and prompt text all derive.

**Phase 1 has already landed** (PR #3078 + PR #3083): live-backed `read_peer_artifact`, SHA-scoped review deltas with rendered `git show` commands, wrapper `sync_to_proposals()` on ack/nack, honest empty results. This pipeline's job is the **remaining scope**: one phase-1 residual, phase 2 (the artifact spec + the gateway artifact-read endpoint that blocks #3002), phase 3 (prompt-prose cleanup + the ratchet mechanism), and a bounded handling of the durability prerequisite the issue names in Trade-offs.

## Current Behavior (verified against the working tree)

### R1 — sync_to_proposals silent-skip (phase-1 residual)

`orchestrator/consensus_wrapper.py:487-539` (`sync_to_proposals()`) is fail-soft at every step: unresolvable SHA (≈524-526) and failed/conflicting merge (≈534-535) log a message — `"sync-to-proposal: merge of $sha failed (conflict or dirty tree); aborted — reviewer reads via git show instead."` — and continue. Nothing about the failure reaches the agent.

The per-event review prompt is rendered in `orchestrator/routes/event_prompt.py:207-298` (`_render_producer_delta_section`): it emits the `git show <sha>:<path>` delta commands (≈260) and caution text for an empty delta (≈272-278), but prompt construction is **orthogonal to sync success/failure** — a reviewer whose worktree silently failed to sync sees no warning and may trust a stale local diff. The issue calls this "the retired failure mode wearing the new mechanism" and explicitly extends the non-silence rule to clause-2 sync.

### Propose-time validation today (the #3016 lineage)

`orchestrator/routes/signals.py:1076-1139` validates **plan** proposals only (role `task_planner`): draft presence at the proposed commit via `git show` (≈1195), parseability via `parse_plan` (#3026), and role↔files alignment (#2527/#2528). Refine uses a separate `_validate_producer_draft_present` (≈1115-1117). Canonical paths are built by a bespoke `_get_draft_path("plan", issue_number, pipeline_id)` helper (≈1162-1166). This is the bespoke validator phase 2 is meant to subsume with spec-derived validation covering **all** producers.

### Where canonical artifact paths live today (the drift surface)

There is no single source of truth; each consumer hardcodes its own pattern:

| Location | Knowledge encoded |
|---|---|
| `gateway/phase_filter.py:605-627` | phase gates: `.egg-state/drafts/*analysis*` (refine), `*plan*` (plan), `drafts/*` (generic) — hardcoded dicts, not config |
| `orchestrator/routes/signals.py:1162-1166` | `_get_draft_path()` path builder for propose validation |
| `orchestrator/routes/event_prompt.py:447, 1186` | `.egg-state/contracts/<key>.json`; `.egg-state/agent-outputs/<role>/brc-memory.md` |
| `shared/egg_restrictions/phase_patterns.py` | mirror of the gateway phase gate for local `check_file_restriction` |
| Agent prompt prose | per-role "write your analysis to …" instructions (the #3016 trigger; repeated by the architect in #3076) |

Each hardcoding is an independent opportunity for write-path drift — exactly the failure #3016 and the architect's `architect-plan.md` incident demonstrated.

### Gateway artifact-read precedent

`gateway/contract_api.py:1-100` is the template the issue cites: the gateway forwards contract reads to the orchestrator's authoritative copy; agents never read replica state. The `git show {sha}:{path}` read pattern already exists server-side at `orchestrator/routes/signals.py:1195`. **No gateway route serves arbitrary committed artifact content today.** The rendered `git show` commands in review prompts work only because every per-role worktree currently shares the host repo's object store; on a split-object-store runtime (GKE, #3002) they break on day one — which is why the issue marks this endpoint a **blocking prerequisite of #3002**, deliberately deferred out of phase 1.

### Message-store durability (clause-1 prerequisite)

`orchestrator/message_store.py:589-633`: backend selection via `EGG_MESSAGE_STORE_BACKEND` (`redis` / `memory` / `auto`); Redis Streams when available (≈621), in-memory `MessageStore` fallback (≈613, 633). The issue's named concern — disk brc-history is *structurally* empty mid-phase, so a mid-phase orchestrator restart on the memory backend reproduces the #3076 condition exactly when the fallback is needed. The `_clear_concurrent_state()` symbol named in the issue body no longer exists; the nearest analogue is `reset_message_store()` (≈636-639, singleton reset). The Redis path covers most of the risk; the gap is the memory backend running where durability matters, **silently**.

### Phase-3 deletion/demotion list (prompt sync mechanics found)

- `shared/prompts/REVIEWER-SYNC.md` (≈109-111): `git fetch origin ${BASE_REF}` + `git log` instructions to reviewers — prose sync machinery.
- `orchestrator/routes/event_prompt.py:207-298, 676-735`: "fetch and read the diffs yourself" fallback prose around `_render_producer_delta_section()` / `_run_git_log()`.
- Residual per-incident prose about where drafts/brc-history live in agent-facing templates.

### #3017 status (phase-2 home, decoupled)

Nothing declarative exists yet: phases are a code enum (`shared/egg_contracts/models.py:62`), phase file-restrictions are hardcoded dicts (`gateway/phase_filter.py:316-655`), no `.egg/phase-definitions.json` or artifact schema. The issue (as revised) **decouples** the artifact spec from #3017: the table is small, lands standalone, and #3017 later consumes it.

## Scope Options

**Option A — Residual + ratchet only (minimal).** Surface sync failure in the event prompt (R1); add the docs invariant + no-sync-mechanics-in-prompts test. Defers the artifact spec and the gateway artifact-read endpoint. *Cheap, but leaves #3002 blocked and the drift surface untouched; the ratchet test is also awkward to write while REVIEWER-SYNC.md legitimately still carries fetch prose.*

**Option B — Residual + Phase 2 (spec + endpoint + spec-derived validation).** R1, plus: a standalone artifact-spec module (name, path template, producer role, consumer roles, per phase); propose-time validation derived from the spec for **all** producers (subsuming `signals.py:1076-1139` and `_validate_producer_draft_present`); a gateway artifact-read endpoint (`git show <ref>:<path>` against the gateway/orchestrator's authoritative repo, resolved by artifact **name**) that unblocks #3002. Phase-3 cleanup deferred. *Delivers the load-bearing machinery but leaves the prose channel free to drift back — the issue explicitly warns a rule without a mechanism has prompt-grade reliability.*

**Option C — Full remaining scope (recommended).** Option B **plus** phase 3: delete/demote the REVIEWER-SYNC fetch prose and event-prompt fallback text now that served reads + wrapper sync cover them; add the `docs/architecture` invariant entry; add the ratchet test asserting agent-facing prompt templates carry no sync mechanics (`git fetch`/`git merge`/`git pull` instructions, brc-history paths). Durability handled as a **bounded** task: fail-loud, not re-engineering — when the memory backend is selected in a context where BRC consensus runs (i.e. `auto` resolved to memory in a deployed pipeline), emit a prominent startup warning/health signal so a mid-phase-restart data loss is attributable, and verify restart semantics of the Redis path with a test. Deeper durability work stays in the #3070 lineage.

**Recommended: Option C.** The pieces are mutually reinforcing — the ratchet test is only writable once phase-3 prose is deleted, the endpoint is only name-resolving once the spec exists, and the spec is only honest once all producers validate against it. The bounded-durability framing keeps the one genuinely open-ended item from inflating the pipeline.

## Proposed Work Breakdown (sketch for plan phase)

1. **R1 — non-silent sync** (small): thread `sync_to_proposals()` per-SHA outcomes into wrapper state consumed by event-prompt rendering; on failure render: *"worktree NOT synced to `<sha>`; treat your local diff as unreliable — use the `git show` commands below."* Test: failed merge ⇒ warning text present in rendered prompt.
2. **Artifact spec module** (`shared/egg_contracts/` or sibling): declarative table — artifact name, path template, phase, producer role, consumer roles. Single registry; unit-tested path resolution; existing hardcodings in `phase_filter.py` / `signals.py` / `event_prompt.py` re-derived from or asserted-consistent with it.
3. **Spec-derived propose validation**: generalize `signals.py:1076-1139` to all producer roles via the spec; keep the plan-specific parseability/role-alignment checks as plan-artifact extensions.
4. **Gateway artifact-read endpoint**: `POST /api/v1/artifact/get` (name + ref → content via `git show` on the authoritative repo), modeled on `contract_api.py`; output caps consistent with existing handlers; sandbox-side read helper. Cross-link as unblocking #3002.
5. **Phase-3 cleanup + ratchet**: delete REVIEWER-SYNC fetch prose + event-prompt fallback sync instructions; `docs/architecture` invariant entry; test scanning agent-facing prompt templates for sync mechanics.
6. **Bounded durability**: fail-loud signal when BRC runs on the memory backend; restart-semantics test for the Redis Streams path.

## Open Questions (HITL)

1. **Scope**: Option A / B / C above (recommendation: C).
2. **Endpoint resolution surface**: should the artifact-read endpoint accept *only* spec-registered artifact names (strict, recommended — path-guessing is the disease) or also raw repo paths as an escape hatch?
3. **Durability bar**: is fail-loud-on-memory-backend sufficient for this pipeline, or should `auto` be changed to refuse to run BRC phases without Redis (a behavioral change to dev setups)?

## Out of Scope / Non-Goals (restating the issue's own fences)

- No new git distribution machinery for coordination state (e.g. mid-phase brc-history push/fetch).
- No shared worktree per phase — per-role isolation stays.
- No attempt to make the prompt channel reliable; it remains lossy by design.
- Full #3017 phase genericization — it consumes the spec later; not gated here.
- Deep message-store re-architecture (#3070 lineage) beyond the fail-loud + verification bound.

## Risks

- **Spec adoption risk**: if existing hardcodings are left in place alongside the spec (rather than derived/asserted), the spec becomes a fourth replica of path knowledge. Mitigation: consistency test in task 2 is mandatory, not optional.
- **Endpoint scope creep**: a generic file-serving route on the gateway is a policy surface; restrict to spec-registered artifacts + committed refs, reuse existing output caps.
- **Prose deletion regressions**: reviewers on the current deploy still occasionally rely on local fetch fallback; sequence phase-3 deletion after tasks 1-4 land in the same pipeline.
