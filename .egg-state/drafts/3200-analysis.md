# Refine analysis — issue #3200

**BRC context discipline: protected directive root + queryable environment, bounded by a deterministic threshold reseed**

Pipeline: `issue-3200` · Phase: refine · Author of issue: jwbron (body re-verified live 2026-06-24)

> **Scope (operator-decided, 2026-06-24/25):** This pipeline **builds the full mechanism, wired for ALL BRC roles (producers AND reviewers)** — it is *not* a single-role prototype. It also builds the **measurement *tooling*** (token-occupancy capture + per-event occupancy/metrics surfaces), but it **runs no measurement, no A/B, and gates nothing on measured results.** The actual measurement, the go/no-go, the gated generalization, the recursion escalation, and the preserved full-context fallback are all explicit **FOLLOW-UP** work, out of scope here. The operator will test the fully-built system end-to-end once it lands.

---

## 1. What this is

Event-pump BRC agents (producers *and* reviewers) are invoked once per actionable event and accumulate context across a phase. The issue proposes a **context discipline** so a long-running role stays oriented and cheap instead of degrading or compacting lossily:

1. a small **protected root** that stays permanently resident (role contract + task anchor + deterministic #3189 anchors + non-negotiable directives);
2. the bulk history moved to a **queryable environment** pulled just-in-time via existing tools;
3. the session **bounded by a proactive deterministic reseed** at a token threshold that pre-empts Claude Code's (CC) lossy ~95% auto-compaction.

**This pipeline builds that discipline in full, across every BRC role**, plus the measurement tooling a later pass will consume. The earlier "RLM-style" branding is explicitly dropped; true recursion is retained only as a **gated escalation**, deferred to a follow-up (§6). Whether the discipline beats the status quo — and any fallback to the original reseed-backstop framing — is decided by the deferred measurement pass, not here (§5, §10).

## 2. Corrected premise — grounded against the tree (verified 2026-06-24)

The original framing's "the Agent SDK does not auto-compact … hard failure" is false. Confirmed:

- **CC compaction-profile system — `orchestrator/agent_model_resolution.py`** (verified ~L96–124). `DISABLE_COMPACT` is *"(which we never set)"*; CC offers only two profiles (1M via the `[1m]` suffix, or the 200K default); sub-1M models **withhold `[1m]`** and take the 200K default so CC "auto-compacts safely below their real limit." Registry today: `_SUB_1M_CONTEXT_MODELS = {"kimi-k2.7-code": 262_144}` — i.e. **the only registered non-1M model is *above* 200K**, so there are **no sub-200K backends in the registry today** (corroborates the issue's "latent, not biting" claim). *Note: the issue's prose cites "GLM 202K" as also covered; GLM is not currently in `_SUB_1M_CONTEXT_MODELS` — non-blocking, the conclusion (no sub-200K backend today) holds.*
- **Post-compaction recovery subsystem exists — `shared/egg_anchor/models.py:1–8`**: anchors "capture working state at natural milestones for post-compaction state recovery during long-running agent sessions." It only exists because compaction happens.

So the wall is **not** a hard failure — it is a silent, lossy CC self-summary that drops exactly the anchors BRC needs (reviewed SHAs, NACK obligations), and below the wall context rot degrades judgment. The premise that motivates the work is sound and grounded.

## 3. Prerequisite (step 0) — capture token occupancy. Grounded.

- **`shared/egg_agent/result.py`** — `AgentResult` exposes `cost_usd / num_turns / duration_ms / session_id` and **no token counts** (verified — dataclass fields confirmed).
- **`shared/egg_agent/client.py:717–751`** — on `ResultMessage` the code builds `result_meta` from `total_cost_usd / num_turns / duration_ms / session_id` and **drops `message.usage`** (verified: no `usage` reference in the result path).
- **Requirement:** capture **window *occupancy*** = `cache_read + cache_creation + input` from `ResultMessage.usage` into `AgentResult` — **not** billed/effective input (capturing only uncached input makes the reseed trigger fire too late). This number is **both** the reseed trigger signal **and** the metric surface the deferred measurement pass consumes. This is a hard, unambiguous, blocking prerequisite — step 1 of the build.

## 4. The build — components mapped to existing code (all of it lands in this pipeline)

| Component | What changes | Grounded anchor |
|---|---|---|
| **Token-occupancy capture** | Add occupancy field(s) to `AgentResult`; stop dropping `ResultMessage.usage` | `result.py`, `client.py:717–751` |
| **Protected root** | Small, deterministic, cacheable, permanently resident: role contract + task anchor (`compose_task_description`, #3163, CLOSED), #3189 deterministic anchors (last-reviewed SHA/producer, latest verdicts, open NACKs, conditional-ACK obligations), non-negotiable directives | #3189 (OPEN) is the authoritative layer; #3163 anchor lands |
| **Queryable environment** | Stop inlining bulk; pull BRC history / peer artifacts / diffs JIT via tools that already exist | `read_peer_artifact`; `/brc-transcript` GET route `orchestrator/routes/messages.py:415`; #3188 enrichment (OPEN) moves *into* this layer |
| **Threshold reseed** | At re-invocation compare resumed-session occupancy to threshold: under → resume cached session (#3186, OPEN); at/over → reseed fresh from protected root, pre-empting CC's ~95% compaction | #3186 warm substrate + reset policy |
| **Within-event growth** | Handled by existing `shared/egg_agent/tool_output_cap.py` (verified present) + the gated recursion escalation (follow-up) — **not** the re-invocation threshold | `tool_output_cap.py` |
| **Measurement surfaces** | Per-event occupancy + metrics surfaces emitted so a later measurement pass can compute peak utilization / reseed frequency / cache-hit / tokens-event — **emit only, no measurement here** | `result.py` occupancy + progress/heartbeat surfaces |

**Applies to ALL BRC roles (producers AND reviewers).** The protected-root / queryable-environment split, the token capture, and the threshold reseed are wired for every role the event pump drives — not a single reviewer. The root render is role-parameterized (each role's contract + its own #3189 anchors), but the mechanism is uniform across roles.

**Threshold = `min(400_000, 0.80 × real_backend_window)`.** The 400k floor is an initial context-rot/cost knob (to tune, not derived); the 80% margin is computed against the **REAL backend window, not the `[1m]` alias** — computing 80% of `opus[1m]`=1M when the backend is Qwen-128K is the mis-trigger bug to avoid. Worked: `opus[1m]`→400k; 200K profile→160k; Qwen-128K→102k.

**Honest limit (the central tension, carried — not gated here):** JIT pull reduces what is inlined *up front* but does **not** bound the window — a pulled slice stays resident until compaction, and a *resumed* session accumulates pulled slices. **What bounds the window is the reseed, not the pull.** Whether #3189 anchors + re-pull substitute for discarded recency is the question the **deferred** measurement pass answers; the tooling built here exists precisely to make that question measurable.

## 5. Measurement: tooling built here, measurement deferred (FOLLOW-UP)

**No measurement, no A/B, no status-quo comparison runs in this pipeline, and nothing here is gated on measured results.** What this pipeline delivers is the **tooling and surfaces** a later measurement pass will consume:

- per-event **window occupancy** captured in `AgentResult` (§3) — the primary metric signal;
- the surfaces needed to later compute, in the follow-up: **peak context utilization under resume** (the property in doubt), **single-event working set vs. real window** (the recursion-escalation signal), **reseed frequency per phase** (the cost case), **review/work quality** (does JIT pull match full-inline?), and **cost** (root-cache hit rate + tokens/event).

The hypothesis those metrics will test — *"resident-root + JIT-pull keeps peak context utilization low under resume"* — is stated here for continuity, but **falsifying it is the follow-up's job, not this pipeline's**. The follow-up issue owns: running the measurement, the **go/no-go**, the **gated generalization** decision, and the **preserved fallback** branch (§10).

## 6. Escalation: sub-agent recursion (gated — NOT this pipeline, follow-up)

True "window never fills" only comes from recursion (reviewer spawns sub-agents over diff/transcript slices; bulk lands in throwaway sub-contexts, only distilled findings return). Deliberately deferred to a gated follow-up: per-event working set fits the window today (~50–130k/call per #3183); the accumulation problem is cross-event drift, which the threshold reseed solves; recursion forfeits the root cache, adds latency, imports decomposition-error risk. **Adopt only when a single event's working set routinely approaches the real backend window** (e.g. sub-200K models become default route, or per-event scope grows). **A (this issue) is a strict prerequisite of B (recursion)** — no lost work building A first. The measurement surfaces built here emit the signal (single-event working set vs real window) that would later justify B. Unchanged from the operator direction: recursion stays out of scope.

## 7. Non-goals (this pipeline)

- **No measurement / A/B / status-quo comparison run, and nothing gated on measured outcomes** — measurement is a follow-up issue (§5).
- **No recursion build** — gated escalation, separate trigger, follow-up (§6).
- **No go/no-go decision and no generalization gate** — the build already covers all roles; whether to *retire the fallback* is decided by the deferred measurement, not here.
- No new git/prompt choreography for state exchange (continues #3077's served-state direction).
- The reseed does **not** claim domination over CC compaction — it wins on **anchor-fidelity**, is **lossier on recency**; a favorable trade, not strict betterment.

## 8. Constraints carried from the children

- **Provider stickiness (LiteLLM route):** single-pin `deepseek-v4-pro`; a provider bounce is amplified under resume (full price on the whole accumulated history until routing returns to the caching provider).
- **Deterministic rendering:** the root must render to **stable bytes** (sorted, bounded, hard per-section caps) for a stable cacheable prefix — across every role.
- **Agent-authored = claims, not ground truth:** SHA-stamp enrichment so the git-log delta can invalidate stale claims; the deterministic #3189 layer + git-log delta stay authoritative. A wrong "verified" claim that suppresses re-checking is the failure mode to design against.
- **Persistence timing:** mid-phase restarts need the message record to survive — `_write_brc_history` persists at **phase transitions only** today; need the live Redis stream across the restart, or a history-persist step added to the restart route.

## 9. Acceptance criteria (full build, all roles, measurement tooling included, measurement deferred)

- **AC-1 — Token-occupancy capture.** `AgentResult` (`shared/egg_agent/result.py`) carries cumulative **window occupancy = `cache_read + cache_creation + input`**, captured from `ResultMessage.usage` in `client.py` (the `usage` block is no longer dropped). Not billed/effective input.
- **AC-2 — Protected-root / queryable-environment split across ALL BRC roles.** Every event-pump role (producers AND reviewers) inlines only the small deterministic protected root (role contract + task anchor + #3189 anchors + non-negotiable directives); bulk history / peer artifacts / diffs are pulled JIT via existing tools (`read_peer_artifact`, `/brc-transcript`), not inlined.
- **AC-3 — Threshold reseed against the real window.** Reseed fires at `min(400_000, 0.80 × real_backend_window)` computed against the **REAL backend window, not the `[1m]` alias**, reseeding a fresh session from the protected root at re-invocation, pre-empting CC's ~95% lossy compaction. (Worked: `opus[1m]`→400k; 200K→160k; Qwen-128K→102k.)
- **AC-4 — Measurement tooling/surfaces present and emitting.** The per-event occupancy and metric surfaces a later measurement pass will consume (peak utilization under resume, reseed frequency per phase, root-cache hit rate, tokens/event, single-event working set vs real window) are present and emitting — **tooling only**.
- **AC-5 — No measurement, nothing gated on it.** This pipeline runs **no** measurement, A/B, or status-quo comparison, and gates **nothing** on measured outcomes. The measurement pass, the go/no-go, the gated generalization, the recursion escalation, and the preserved full-context fallback are explicitly deferred to a **follow-up issue**.

## 10. Deferred to a follow-up issue (no longer gating this pipeline)

The following move **out** of this pipeline into an explicit follow-up — they no longer gate the build:

- **Run the measurement / A-B / status-quo comparison** that consumes the surfaces built here.
- **Go/no-go** on the discipline, and the **gated generalization** decision (the build is already all-roles; the follow-up only decides whether to *retire* the fallback framing).
- **Preserved fallback:** the **original full-context reseed-backstop framing**, preserved verbatim in the issue body's `<details>` block (restart-fresh + orchestrator-seeded curated BRC memory as system prompt). No work is lost: #3189 + token capture + #3186 resume are the keepers in every branch.
- **Recursion escalation** (§6).

## 11. Open decisions (HITL)

**Scope is operator-decided — there is no open scope decision.** Per the operator's scope correction (2026-06-24/25): the prior `cq-1` (pipeline scope A/B/C) collapses to a single decided scope — **full build, all BRC roles, measurement tooling included, measurement deferred** — and the prior `cq-2` (which reviewer role to prototype on) is **moot/withdrawn** under the all-roles scope. No new HITL decisions are required to proceed to plan.

---
*Refiner grounding pass: all code references in the issue verified against the working tree on 2026-06-24. One cosmetic discrepancy noted (§2, GLM not in the sub-1M registry); does not affect the conclusion. Child issues confirmed: #3189/#3188/#3186/#3183 OPEN, #3163/#3077 CLOSED. Scope revised 2026-06-25 per operator directive: single-role-prototype + in-pipeline-measurement framing replaced with full-build-all-roles + measurement-tooling-only; measurement, go/no-go, generalization, recursion, and fallback deferred to a follow-up.*
