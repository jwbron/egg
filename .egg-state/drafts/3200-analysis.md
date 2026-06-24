# Refine analysis — issue #3200

**BRC context discipline: protected directive root + queryable environment, bounded by a deterministic threshold reseed**

Pipeline: `issue-3200` · Phase: refine · Author of issue: jwbron (body re-verified live 2026-06-24)

---

## 1. What this is

Event-pump BRC agents (producers/reviewers) are invoked once per actionable event and accumulate context across a phase. The issue proposes a **context discipline** so a long-running role stays oriented and cheap instead of degrading or compacting lossily:

1. a small **protected root** that stays permanently resident (role contract + task anchor + deterministic #3189 anchors + non-negotiable directives);
2. the bulk history moved to a **queryable environment** pulled just-in-time via existing tools;
3. the session **bounded by a proactive deterministic reseed** at a token threshold that pre-empts Claude Code's (CC) lossy ~95% auto-compaction.

This is the **"build + measure with a preserved fallback"** mandate: prototype on one reviewer role, measure against the status quo, and fall back to the original reseed-backstop framing (preserved verbatim in the issue) if it does not beat the status quo. The earlier "RLM-style" branding is explicitly dropped; true recursion is retained only as a **gated escalation**.

## 2. Corrected premise — grounded against the tree (verified 2026-06-24)

The original framing's "the Agent SDK does not auto-compact … hard failure" is false. Confirmed:

- **CC compaction-profile system — `orchestrator/agent_model_resolution.py`** (verified ~L96–124). `DISABLE_COMPACT` is *"(which we never set)"*; CC offers only two profiles (1M via the `[1m]` suffix, or the 200K default); sub-1M models **withhold `[1m]`** and take the 200K default so CC "auto-compacts safely below their real limit." Registry today: `_SUB_1M_CONTEXT_MODELS = {"kimi-k2.7-code": 262_144}` — i.e. **the only registered non-1M model is *above* 200K**, so there are **no sub-200K backends in the registry today** (corroborates the issue's "latent, not biting" claim). *Note: the issue's prose cites "GLM 202K" as also covered; GLM is not currently in `_SUB_1M_CONTEXT_MODELS` — non-blocking, the conclusion (no sub-200K backend today) holds.*
- **Post-compaction recovery subsystem exists — `shared/egg_anchor/models.py:1–8`**: anchors "capture working state at natural milestones for post-compaction state recovery during long-running agent sessions." It only exists because compaction happens.

So the wall is **not** a hard failure — it is a silent, lossy CC self-summary that drops exactly the anchors BRC needs (reviewed SHAs, NACK obligations), and below the wall context rot degrades judgment. The premise that motivates the work is sound and grounded.

## 3. Prerequisite (step 0) — capture token occupancy. Grounded.

- **`shared/egg_agent/result.py`** — `AgentResult` exposes `cost_usd / num_turns / duration_ms / session_id` and **no token counts** (verified — dataclass fields confirmed).
- **`shared/egg_agent/client.py:717–751`** — on `ResultMessage` the code builds `result_meta` from `total_cost_usd / num_turns / duration_ms / session_id` and **drops `message.usage`** (verified: no `usage` reference in the result path).
- **Requirement:** capture **window *occupancy*** = `cache_read + cache_creation + input` from `ResultMessage.usage` into `AgentResult` — **not** billed/effective input (capturing only uncached input makes the reseed trigger fire too late). This number is **both** the reseed trigger signal **and** the prototype's primary metric (peak utilization under resume). This is a hard, unambiguous, blocking prerequisite — step 1 of the build.

## 4. The build — components mapped to existing code

| Component | What changes | Grounded anchor |
|---|---|---|
| **Token-occupancy capture** | Add occupancy field(s) to `AgentResult`; stop dropping `ResultMessage.usage` | `result.py`, `client.py:717–751` |
| **Protected root** | Small, deterministic, cacheable, permanently resident: role contract + task anchor (`compose_task_description`, #3163, CLOSED), #3189 deterministic anchors (last-reviewed SHA/producer, latest verdicts, open NACKs, conditional-ACK obligations), non-negotiable directives | #3189 (OPEN) is the authoritative layer; #3163 anchor lands |
| **Queryable environment** | Stop inlining bulk; pull BRC history / peer artifacts / diffs JIT via tools that already exist | `read_peer_artifact`; `/brc-transcript` GET route `orchestrator/routes/messages.py:415`; #3188 enrichment (OPEN) moves *into* this layer |
| **Threshold reseed** | At re-invocation compare resumed-session occupancy to threshold: under → resume cached session (#3186, OPEN); at/over → reseed fresh from protected root, pre-empting CC's ~95% compaction | #3186 warm substrate + reset policy |
| **Within-event growth** | Handled by existing `shared/egg_agent/tool_output_cap.py` (verified present) + the gated recursion escalation — **not** the re-invocation threshold | `tool_output_cap.py` |

**Threshold = `min(400_000, 0.80 × real_backend_window)`.** The 400k floor is an initial context-rot/cost knob (to tune, not derived); the 80% margin is computed against the **REAL backend window, not the `[1m]` alias** — computing 80% of `opus[1m]`=1M when the backend is Qwen-128K is the mis-trigger bug to avoid. Worked: `opus[1m]`→400k; 200K profile→160k; Qwen-128K→102k.

**Honest limit (the central tension to falsify):** JIT pull reduces what is inlined *up front* but does **not** bound the window — a pulled slice stays resident until compaction, and a *resumed* session accumulates pulled slices. **What bounds the window is the reseed, not the pull.** The prototype must measure whether #3189 anchors + re-pull substitute for discarded recency.

## 5. Central hypothesis & measurement (step 4)

**Hypothesis to falsify:** *"resident-root + JIT-pull keeps peak context utilization low under resume."* Measure the prototype reviewer against a status-quo reviewer **on the same phase**:

- **peak context utilization under resume** (primary — the property in doubt);
- **single-event working set vs. real window** (the recursion-escalation signal);
- **reseed frequency per phase** (the cost case rests on this being low — each reseed forfeits the 90%+ root cache and re-pays JIT pull);
- **review quality** — does JIT pull match/beat full-inline?
- **cost** — root-cache hit rate + tokens/event.

**Go/no-go (step 5, gated on measurement — out of scope for this pipeline, see cq-1):** if utilization stays low and quality holds → generalize to producers + all roles and retire the fallback framing. If utilization climbs → lower the threshold, adopt the recursion escalation, or fall back.

## 6. Escalation: sub-agent recursion (gated — NOT default, NOT this pipeline)

True "window never fills" only comes from recursion (reviewer spawns sub-agents over diff/transcript slices; bulk lands in throwaway sub-contexts, only distilled findings return). Deliberately deferred: per-event working set fits the window today (~50–130k/call per #3183); the accumulation problem is cross-event drift, which the threshold reseed solves; recursion forfeits the root cache, adds latency, imports decomposition-error risk. **Adopt only when a single event's working set routinely approaches the real backend window** (e.g. sub-200K models become default route, or per-event scope grows). **A (this issue) is a strict prerequisite of B (recursion)** — no lost work building A first. The prototype measures the signal (single-event working set vs real window) that would justify B.

## 7. Non-goals

- No recursion build in this pipeline (gated escalation, separate trigger).
- No generalization to all roles/producers now — step 5 is explicitly gated on measurement.
- No new git/prompt choreography for state exchange (continues #3077's served-state direction).
- The reseed does **not** claim domination over CC compaction — it wins on **anchor-fidelity**, is **lossier on recency**; a favorable trade, not strict betterment.

## 8. Constraints carried from the children

- **Provider stickiness (LiteLLM route):** single-pin `deepseek-v4-pro`; a provider bounce is amplified under resume (full price on the whole accumulated history until routing returns to the caching provider).
- **Deterministic rendering:** the root must render to **stable bytes** (sorted, bounded, hard per-section caps) for a stable cacheable prefix.
- **Agent-authored = claims, not ground truth:** SHA-stamp enrichment so the git-log delta can invalidate stale claims; the deterministic #3189 layer + git-log delta stay authoritative. A wrong "verified" claim that suppresses re-checking is the failure mode to design against.
- **Persistence timing:** mid-phase restarts need the message record to survive — `_write_brc_history` persists at **phase transitions only** today; need the live Redis stream across the restart, or a history-persist step added to the restart route.

## 9. Open decisions (HITL)

- **cq-1 — Pipeline scope.** What does *this* pipeline deliver? (A) token-occupancy capture only [step 1]; **(B, recommended)** the full build+measure prototype on ONE reviewer role [steps 1–4: capture → protected-root/queryable-env split → threshold reseed → measurement harness], with generalization (step 5) and recursion (escalation) explicitly deferred; (C) B + generalize to all roles now (contradicts the measure-first mandate).
- **cq-2 — Prototype reviewer role.** Which reviewer to prototype on, for measurement validity. Recommend a reviewer that accumulates the largest working set (best stress test of the resume hypothesis + recursion signal); final pick may be left to the plan/architect phase.

## 10. Fallback (preserved)

If the prototype does not beat the status quo, fall back to the **original full-context reseed-backstop framing**, preserved verbatim in the issue body's `<details>` block (restart-fresh + orchestrator-seeded curated BRC memory as system prompt). No work is lost: #3189 + token capture + #3186 resume are the keepers in every branch.

---
*Refiner grounding pass: all code references in the issue verified against the working tree on 2026-06-24. One cosmetic discrepancy noted (§2, GLM not in the sub-1M registry); does not affect the conclusion. Child issues confirmed: #3189/#3188/#3186/#3183 OPEN, #3163/#3077 CLOSED.*
