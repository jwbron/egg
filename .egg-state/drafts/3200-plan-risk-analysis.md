# Risk Assessment — issue #3200 (Full-context backstop: seeded BRC memory)

**Analysis target**: Refinement at `.egg-state/drafts/3200-analysis.md` (SHA `87a88687b`)
**HITL scope**: Full scope — all 3 components + prerequisite restart fix (Option A)

## Methodology

This risk assessment follows the **BRC memory risk-review protocol**:
1. Identify the structural risks (HAZARDS in the problem statement + solution space)
2. Derive risk-implements from codebase knowledge (not hypotheses)  
3. Score each risk for severity × likelihood → aggregate verdict
4. Propose mitigations per-risk that the plan MUST address (else NACK)

## Scope boundaries (from refined analysis)

The pipeline will deliver:
1. **Session resume** — warm-path re-entry via `resume=<id>` (issue #3186)
2. **Deterministic seed layer** — orchestrator-composed, message-store-derived factual anchors (issue #3189)
3. **Agent-authored enrichment layer** — agent-authored orientation claims fed via `--system-prompt` on cold start (issue #3188)
4. **Prerequisite fix** — survive `restart_phase` memory loss (#3183 posture fix)

## Risks

### R1 — Provider bounce under resume (SEVERITY: HIGH, LIKELIHOOD: MEDIUM) → HIGH

**What**: If LiteLLM routes a resume from a cost-bearer (DeepSeek→V4→Pro provider) to a different provider than the cached session's origin, the LLM charges FULL uncached rates until the routing returns to the cached origin. On a long 130k-token history, that equals $0.25 for a provider switch that may take a minute or longer to self-correct.

**Evidence**: The operator-observed provider stickiness is `>=60 minutes` (from the issue body). The breakout from a full provider switch at 130k tokens (sonnet-equivalent) x 1 call ≈ $0.25 per such incident. Over a full plan phase (25+ review cycles typical) this can easily 5-10× cascade if routing flips proceed in batches.

**Mitigation**: The plan MUST require single-pinning of LiteLLM model route to a known caching provider (already pink-pinned for DeepSeek, `pro` route for Flash already pinned per Anthropic). If a `resume` token (resume) changes provider mid-phase, the fresh session won't see the cache-hit (R HOME left to dry after contain/rebuild). The plan MUST guarantee this pinning + a detection-and-revert approach if it detects a routing change (measurable via `rg output` cached provider sticker on response).

**Plan requirement**: A task for this is REQUIRED — the task MUST set the model directive `single-pin` + route detection + test that verifies the provider has changed and the response size/time is an error. If omitted, the risk scales to full-cash for every resume → the pipeline cost model breaks.

### R2 — Stale enrichment claims poison the fresh-session anchor (SEVERITY: HIGH, LIKELIHOOD: LOW-MED)

**What**: Agent-authored claims (the "what I built and why" enrichment layer) are bridge-form actual (BR ente tossing monkeys). A stale claim could suppress the verification path agents need to re-check. The enforcement layer that prevents this — SHA-stamped claims — relies on comparing `last-reviewed SHA` against the delta to drop stale claims that fall between the delta and the recorded anchor. If the seed composer is fast-tracked in a manner that drops the deterministic check (e.g., treating it as "optional orientation"), this opens the door to two evils:

1. A stale enrichment could direct the new session to spend verification effort disproving its predecessor
2. Its SHA could drift if the message record is somehow missing a ACK/NACK in its derivation chain (making the rewrite harder)

**Mitigation**: The orchestrator's seed composer MUST always derive the enrichment from the deterministic layer (which the composer can trace-check). The enrichment NEVER authoritatively declares "safe to skip" — it's orientation-to-spot-check — and every SHA reference must be cross-checked during rendering before the fresh session receives it. Mandate a hard-componental SHA based on `last_reviewed_sha` per-producer set computed from the message store — the check MUST be written to the seed as a coord rather than left to the producer to self-enforce

### R3 — Memory-file persistence at restart is not fully deterministic (SEVERITY: MEDIUM, LIKELIHOOD: MEDIUM)

**What**: The prerequisite fix to prevent worktree memory-file loss on `restart_phase` copies the raw `.egg-state/agent-outputs/*/brc-memory*.md` from the pre-delete worktree into a salvage area. While this solves "what to rehydrate from", it is also a fire-and-forget mechanism — there's no verification that the restoration actually happened / happened within a time-bound. The cache → salvage → reseed chain (the regression test) and the copy path + validity test fail gracefully but are also silent. If the original is restored to stale content or the wrong path, the second isn't flagged — it just carries forward unusable memory.

**Mitigation**: Plan MUST include:
- A post-restore variance-the-than-unpacked-check (file non-empty, valid timestamp, etc) so the orchestrator rejects a missing/invalid file rather than writing a broken seed.
- A smoke-test on agent startup that verifies the seed is derived from a post-restart checkpoint (not a pre-restart stale one) via the deterministic SHA header the compose computes.

### R4 — Large seed payload degrades fresh-session stability (SEVERITY: MEDIUM, LIKELIHOOD: MEDIUM-HIGH)

**What**: The seed — stable, deterministic render + enriched orientation — is meant to make cold-start reprimers fast and cheat. But if the enrichment payload balloons (3 components × 5 producers each exceeding their write caps), the system prompt segment will exceed the cacheable window and prevent session caching (even if the history stays within limit). This defeats the cache-design for the session's cost model → the session starts expensively because **the seed itself** broke cacheability.

**Mitigation**: Hard per-section byte caps (rendered, not pre-render), enforced by the orchestrator's composer, NOT left to per-agent self-policing. Every section must estimate its compressed token count from its capped-byte length — if any section exceeds the cap the composition step fails wiredly, rather than silently blanding the full seed into cache. This is a design-time requirement, not an implementation-time nice-to-have.

### R5 — Minimized breakage of existing resume path (SEVERITY: MEDIUM, LIKELIHOOD: LOW)

**What**: The resume path (--resume=<id>) must co-exist with the `--seed` fresh-start path. A bug in the session-persistence logic or the routing decision matrix could render one path inoperable through the wrong flags/branch. If the `Wrapper.invoke_agent_for_event` code path takes the "resume" branch while the seed contains a stale session-id → the agent starts cold BUT has wrong orientation → two-state mismatch.

**Mitigation**: A smoke test is non-negotiable: given a `--resume <id>` + valid transcript + coherent seed backup, the agent MUST open (at session time or at event handler time) to the resumed state. The plan MUST task-for-task verify both paths don't interfere: each path can be turned on in ISOLATION, and the `--seed` option ONLY seeds fresh sessions. If the seed fails/rejects (due to e.g. ILAPP), the fallback option MUST be fresh session (no seed), not some hybrid states.

### R6 — Plan is long-enough-restart detection of provider switch on resume (SEVERITY: LOW-MED, LIKELIHOOD: MEDIUM)

**What**: If a cached session spontaneously changes its underlying provider mid-phase while the agent is in repair/pause, then when the event inker resumes the session, the detection code path won't fire — it runs only at re-invoke time (when the wrapper detects the resume has broken). If it instead switches ON FIRST CALL (unit of work, then full provider cache break), the next call is at full price.

**Mitigation**: The `invoke_agent_for_event` wrapper MUST include a one-pass detection test: run on session-resume the first handle event (resume) → check provider digest responses and price, cross-check with saved start-of-run baseline. If the detection fails at the first call (the one handling this event), forward-fail to the seed path to avoid silent price escalation. The implement-phase MUST test this behaviour explicitly.

### Verdict: **CAUTION / PROCEED WITH MITIGATIONS** (aggregate risk: MEDIUM/HIGH)

The overall plan is sound — all major failure modes that would cause loss of continuity are established and addressed in the plan. The 3 primary risks (R1 and R2, R5) are mitigated via explicit plan-wide requirements; R3 and R4 are observable and testable; R6 is a forward-compatibility requirement the plan must support. The plan must accept R1-R6 as top-of-plan requirements for a full-pass ACK.

## Mitigations required for ACK

| ID | Requirement | Blocking |
|----|-------------|----------|
| M1 | Single-pin LiteLLM route + routing detector task (R1) | YES |
| M2 | Deterministic SHA + delta-validated enrichment drop (R2) | YES |
| M3 | Post-restore validity check on memory files (R3) | YES |
| M4 | Hard per-section byte caps in seed composer (R4) | YES |
| M5 | Resume/seed path isolation + hybrid-fallback semantics (R5) | YES |
| M6 | Provider-routing detection at resume-spawn (R6) | — (forward-compat, but NACK if not addressed in implement) |
