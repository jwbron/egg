# Agent team communication and peer consensus protocol

> Issue: #1110 | Phase: refine

## Problem

The concurrent agent consensus protocol needs a fundamental redesign. The current architecture has two core problems:

1. **Polling-only communication** — Agents poll for messages every 30s. There is no push/alert mechanism. A message sent at t=0 may not be seen until t=29s later.

2. **Orchestrator-centric consensus** — Each agent independently tells the orchestrator "I'm READY". The orchestrator tallies votes and declares consensus. Agents never actually agree *with each other* — they just announce their own state to a central authority. An agent saying "READY" doesn't mean its work is good — it means the agent *thinks* its work is good. These are unreliable reasoners, and peer verification is the only mechanism available to catch errors the agent itself can't see.

This needs to be replaced with a **many-to-many peer consensus protocol** where agents communicate directly with each other, review each other's work, and individually decide they agree with the group.

---

## Three Layers of the Problem

This is not just a distributed systems problem. There are three distinct layers:

| Layer | Concern | Domain |
|-------|---------|--------|
| **Physical transport** | How messages are delivered between agents | Distributed systems (Redis Streams, long-polling, message bus) |
| **Protocol structure** | The rules governing proposals, acks, state transitions | Distributed consensus (2PC, barriers, ack-all) |
| **Reasoning-level agreement** | Agents genuinely evaluating each other's work and forming independent judgments | Sociology, game theory, social choice |

The current system only addresses layer 1 (poorly — polling) and layer 2 (minimally — READY tallying). Layer 3 is entirely absent: agents don't reason about each other's outputs, they just declare their own state.

---

## Research Summary

### Distributed Systems Protocols

| Protocol | Fit | What to borrow |
|----------|-----|----------------|
| **Two-Phase Commit (2PC)** | Moderate | Vote-then-commit structure; coordinator as fallback |
| **Three-Phase Commit (3PC)** | Low | Timeout-based autonomous decisions |
| **Interactive Consistency** | **High** | The intuition — every participant agrees on every other's output. We adapt for probabilistic rather than crash-stop failures, since LLM agents produce *partially correct* output rather than simply crashing or succeeding. |
| **Reliable Broadcast** | Already have | The message bus provides this; layer consensus on top |
| **Barrier Synchronization** | Partial | Use barriers to separate phases (all outputs submitted → review → resolve) |
| **Ack-All** | **High** | Core primitive: broadcast output → collect ACK/NACK from all peers → unanimous ACK required |
| **Gossip** | Low | Overkill for 3-8 agents; probabilistic where we need deterministic |

**Best fit**: Interactive Consistency + Ack-All. We borrow the intuition that every participant must agree on every other's output, adapted for agents that produce varying-quality output rather than binary crash/success.

### Sociology & Group Decision-Making

| Model | Key Insight | Protocol Implication |
|-------|-------------|---------------------|
| **Habermasian discourse** | Consensus requires *validity claims* that survive challenge — not just assertions. DeepMind's "Habermas Machine" (Science, 2024) demonstrated LLM-mediated consensus among *human participants* via structured argumentation; we adapt its structured-claims approach to agent-to-agent interaction. | READY signals must include structured claims (truth: "tests pass", rightness: "follows ADR-007", sincerity: "here's what I changed"). Peers challenge claims, not just vote. |
| **Delphi method** | Iterated anonymous feedback converges better than single-shot voting. DelphiAgent (2025) applies this to multi-LLM verification. | Agents submit independent assessments before seeing others' states. Synthesis + revision rounds (bounded to 2-3) before final consensus. |
| **Nominal Group Technique** | Separate *generation* from *evaluation* to prevent anchoring. | Agents assess independently → share in fixed order → discuss → vote. Coder's READY should NOT be visible to reviewers until they've submitted their own assessment. |
| **Social Choice Theory** | Arrow's theorem: no perfect aggregation. But unanimity + structured deliberation avoids the worst pathologies. ACL 2025 paper compared 7 decision protocols for multi-agent LLMs. | Unanimity gives every agent veto power → require structured justification to veto (prevents frivolous blocking). |
| **Groupthink / Sycophancy** | CONSENSAGENT (ACL 2025): LLM agents have strong sycophancy in group settings. Must be *actively mitigated* through process design. | Reviewers must identify at least one concern before ACKing. Embed critical evaluation structurally, not just via role names. |
| **Devil's Advocate** | Rigid adversarial roles backfire (ICLR 2025 MAD blog confirms this). The alternative — embedded critical evaluation in every role — is a design inference, not a specific research finding. | Don't create a "devil's advocate agent." Instead, require every agent to document one risk it considered and why it's acceptable, as part of its proposal. |
| **Stigmergy** | Agents already coordinate through artifacts (commits, test results). Research shows traces require cognitive infrastructure for interpretation (68.7% improvement only with memory + traces). | Make artifacts (handoffs) richer and structured. Ensure agents have context to interpret them, not just raw data dumps. |
| **Shared Mental Models** | Teams need shared understanding of "done." Disagreements often trace to divergent models, not genuine conflicts. | Make acceptance criteria explicit and verifiable per role before the phase starts. Detect model divergence early. |

### Game Theory

| Concept | Key Insight | Protocol Implication |
|---------|-------------|---------------------|
| **Cooperative game theory (Shapley values)** | Not all agents contribute equally to consensus quality. A reviewer catching a bug contributes more than a documenter confirming formatting. | Weight ACKs/NACKs by role criticality per phase. A reviewer's NACK blocks; a documenter's flags. |
| **Mechanism design** | Design rules so thorough work is the dominant strategy. The protocol *is* the mechanism. | Don't reward speed. Require evidence-backed signals. Make rubber-stamping detectable and non-advantageous. |
| **Incentive compatibility** | Agents shouldn't benefit from misrepresenting their state. Premature READY should be detectable. | READY signals must include verifiable attestations (test results, files reviewed, commit SHAs). Transforms cheap talk into costly signals. |
| **Coordination games (Stag Hunt)** | The real risk isn't adversarial behavior — it's everyone settling on low effort. If coder doubts reviewer will catch bugs, coder invests less in quality. | Make effort visible via PROGRESS messages. Sequential revelation of intermediate work reduces uncertainty about others' effort. |
| **Cheap talk** | All messages on the bus are costless and non-binding. With fully aligned interests, cheap talk allows full disclosure (Crawford & Sobel 1982). But LLM agents are *unreliable communicators* — they may genuinely believe bad work is good. This is actually a stronger argument for costly signals than strategic withholding would be. | Keep cheap talk for STATUS/PROGRESS (low overhead). Upgrade to costly signals for READY/OBJECTING (require evidence). |
| **Signaling theory** | Credible signals are harder to produce if the agent hasn't done the work. | Per-role attestation requirements: coder → commit SHAs + test summary; tester → test count + coverage; reviewer → files reviewed + issues found; documenter → sections updated. |
| **Prompt effectiveness tracking** | Tracking "reputation" for stateless LLM agents is a category error — each invocation is `{model + prompt + context + task}`, and all four change between runs. The improvement loop should target prompt quality, not agent identity. | Track *prompt effectiveness* per role: A/B test prompt versions, measure NACK rates and post-merge defect rates. Build a failure pattern catalog (what errors each role systematically misses) and feed back into prompts. Score attestation quality (do cited artifacts match reality?) to measure prompt quality. |
| **Principal-agent problem** | The orchestrator can't observe effort directly — only outputs and signals. | The integrator role acts as the principal's auditor. Cross-reference attestations against actual artifacts. |
| **Commitment devices** | READY must be meaningful. Free flip-flopping destroys signal value. | Cooldown period after READY. Retraction requires citing specific new information. Lockout after K flip-flops → orchestrator review. |

---

## Design: Three-Layer Consensus Architecture

### Layer 1: Transport — Redis Streams + Long-Polling

All agents in a concurrent phase share a **single message stream** per pipeline. Every message, state change, and consensus protocol event flows through this one stream.

**Why Redis Streams over SSE:**
- SSE is server→client only; agents still need a separate channel to send. Redis Streams is natively bidirectional (agents publish and consume).
- Redis Streams provides message persistence — agents that restart can replay missed messages from their last-seen ID. SSE connections drop messages on disconnect.
- Ordered, append-only log creates a natural audit trail.
- Redis is already running in the environment — zero new infrastructure.

**Stream architecture:**
- Single Redis Stream per pipeline: `pipeline:{id}:messages`
- Agents interact via the orchestrator API, not directly with Redis. The API layer handles role-based filtering.
- **Read pattern**: `XREAD` (each agent maintains its own cursor via `since_id`). The orchestrator API reads from the stream and filters by `to_role` matching the requesting agent's role or `"all"`. Consumer groups are unnecessary — the orchestrator API is the filtered view, the Redis Stream is the raw ordered log.
- **Write pattern**: Agents POST messages via the orchestrator API, which writes to the stream.
- **Long-polling**: `GET /api/v1/pipelines/{id}/messages?role=coder&since_id=abc&wait=30` — orchestrator reads from Redis Stream with `XREAD BLOCK 30000`, applies role filter, returns immediately when matching messages arrive or on timeout.

**Agent experience**: `egg-orch message poll --wait 30` blocks until messages arrive (~1s delivery) instead of sleeping 30s between polls.

### Layer 2: Protocol — Broadcast-Review-Converge (BRC)

A structured protocol inspired by Interactive Consistency + Ack-All + Delphi, with three phases.

**Asymmetric review topology: Producers propose, reviewers judge.**

The review graph is **asymmetric by role type**. There are two categories of agent:

- **Producers** (coder, tester, documenter): Create artifacts (code, tests, docs) and propose them for review. Their proposals contain work products.
- **Reviewers** (rev_code, rev_contract, checker): Evaluate producers' proposals and issue ACK/NACK judgments. They don't propose artifacts that need peer ACK — they produce *judgments*.

This eliminates the circular ACK problem where a coder would be incentivized to NACK a negative review of their own code. The coder doesn't ACK the reviewer's review; the coder *responds to NACKs* by revising and re-proposing.

**Review adjacency (who reviews whom):**

| Reviewer role | Reviews proposals from |
|---|---|
| rev_code | coder, tester |
| rev_contract | coder |
| checker | coder |
| tester | coder (implicitly — writes tests against the code, ACKs if tests pass) |

**Note**: The tester has a **dual role** — it is both a producer (proposes test artifacts) and a reviewer (evaluates coder's work by writing and running tests against it). The tester has a hybrid state machine: it proposes its own test artifacts AND evaluates the coder's proposal. The producer/reviewer categorization is not strictly exclusive; the tester is the exception.

Producers don't review each other — their proposals are evaluated by designated reviewer roles. ~7-10 directed edges instead of 30 for N=6. Configurable per phase.

**BRC Protocol:**

**Phase 1: Broadcast** (barrier sync)
- Each **producer** completes its work and broadcasts a `CONSENSUS_PROPOSE` to all peers
- The proposal includes: summary of work done, artifacts produced, attestations per role (see Layer 3)
- Reviewers observe proposals arriving on the stream, but the producer's self-assessment metadata is held back from reviewers until they've submitted an independent evaluation (see Delphi ordering below)
- Wait until all producers have proposed (or timeout → escalate missing agents)

**Delphi-style ordering** (simpler than it sounds):

The server knows which roles are reviewers vs. producers (from the phase-role mapping). Work artifacts live in git — reviewers can read actual commits and form independent judgments immediately. The ordering constraint is narrow:

- Reviewers see git artifacts (commits, files) immediately — they're in the repo
- The server holds back the producer's `CONSENSUS_PROPOSE` message (self-assessment, attestations, "one risk I considered") from reviewers until the reviewer has submitted their own initial ACK/NACK
- After the reviewer posts their independent evaluation, the server releases the producer's self-assessment metadata

This is a **conditional release on one message type to one role category**, not a general-purpose visibility filter. Implementation: the long-polling endpoint checks whether the requesting agent is a reviewer for the proposer and whether the reviewer has already submitted an evaluation. If not, the PROPOSE message is withheld. No separate streams or complex fan-out needed.

**Phase 2: Review** (asymmetric ack)
- Each **reviewer** reviews proposals from its assigned producers
- For each: sends `CONSENSUS_ACK` (agree, with artifact references) or `CONSENSUS_NACK` (disagree, with structured reason + artifact references)
- A NACK drops the producer back to WORKING; producer addresses the concern and re-proposes

**Scoped re-evaluation on re-proposal:**
- A NACK targets a specific proposal version. Re-proposal only triggers re-review from the NACKing reviewer. Other reviewers' prior ACKs stand unless the revision affected artifacts they referenced.
- Example: rev_code NACKs coder citing a bug in `auth.py`. Coder fixes and re-proposes. rev_code must re-review. Tester's prior ACK (which referenced test results, not `auth.py`) stands unless the fix changed test behavior.
- Circular NACKs cannot occur because the review graph is asymmetric — reviewers judge producers, not the reverse.

**Bounded revision rounds**: Max K rounds (e.g., K=2) per producer-reviewer pair before HITL escalation. Tracked per-edge, not globally.

**Phase 3: Converge** (confirmation)
- When a **producer** has: (a) proposed, (b) received ACKs from all assigned reviewers → it broadcasts `CONSENSUS_CONFIRMED`
- When a **reviewer** has: (a) ACKed all assigned producers → it broadcasts `CONSENSUS_CONFIRMED`
- When ALL agents have confirmed → the orchestrator (observing the stream) acts
- The orchestrator does not *decide* consensus — it *observes* that all agents independently confirmed

**State machine per producer:**
```
WORKING → PROPOSED → CONFIRMED
    ↑         |
    └─────────┘  (NACK received → address concern → re-propose)
```

**State machine per reviewer:**
```
WORKING → REVIEWING → CONFIRMED
             |   ↑
             └───┘  (producer re-proposes → re-review if affected)
```

**Reviewer trigger**: A reviewer transitions from WORKING to REVIEWING when it detects new commits from a producer it's assigned to review (via `git log` or branch inspection), not when it receives a PROPOSE message on the stream. The Delphi ordering means the producer's self-assessment is held back — but the work artifacts (commits, files) are immediately visible in git. The reviewer forms its independent judgment from git, then posts ACK/NACK, and only then sees the producer's self-assessment metadata.

### Layer 3: Reasoning — Evidence-Backed Deliberation

Agents don't just signal states — they make **structured claims** that peers evaluate. Borrowed from Habermasian discourse, signaling theory, and sycophancy mitigation.

**Proposal structure** (per-role attestations):

| Role | Required attestation in CONSENSUS_PROPOSE |
|------|------------------------------------------|
| Coder | Commit SHAs, files changed, test pass/fail summary, one risk considered |
| Tester | Tests written/run count, coverage delta, edge cases covered, one concern considered |
| Documenter | Sections updated, links verified, one concern considered |

**Review structure** (per-role attestations in ACK/NACK):

| Role | Required attestation in CONSENSUS_ACK/NACK |
|------|------------------------------------------|
| Reviewer (code) | Files reviewed (specific paths), issues found + resolved count, one risk considered |
| Reviewer (contract) | Tasks verified (specific IDs), acceptance criteria checked, gaps identified |
| Checker | Lint/type/test results (pass counts), auto-fixes applied, remaining warnings |

**Anti-sycophancy measures** (from CONSENSAGENT research):
- ACKs and NACKs must reference **specific artifacts** (file paths, line numbers, commit SHAs, test names) — not just "looks good" or "looks wrong." Generic acknowledgments are rejected by schema validation.
- Reviewers form independent judgments from git artifacts *before* seeing the producer's self-assessment (Delphi ordering)
- NACKs must include specific, actionable feedback
- Every proposal/review must include "one risk I considered" as a lightweight critical-thinking prompt. **Note**: This is a secondary measure. The primary anti-sycophancy mechanism is the artifact reference requirement — citing specific file paths, commit SHAs, and line numbers is mechanically hard to satisfy without doing the work. The "one risk" prompt is easy to satisfy with generic output and should not be weighted equally when evaluating protocol effectiveness.
- The integrator **cross-references attestations against actual changes** — verifies cited files were actually modified, cited tests actually exist, cited commit SHAs are real
- Per CONSENSAGENT: use **dynamic prompt refinement** — evolve prompts based on observed rubber-stamping patterns, don't rely solely on procedural rules

**Cheap talk vs costly signals:**
- STATUS and PROGRESS messages remain cheap talk (low overhead, informative with aligned interests)
- CONSENSUS_PROPOSE requires attestations (costly signal — harder to produce without doing the work)
- CONSENSUS_ACK requires referencing specific artifacts reviewed (prevents rubber-stamping)
- CONSENSUS_NACK requires specific, actionable objection with artifact references

---

## Consensus Failure Modes

The orchestrator observes the peer protocol but must act when the protocol fails. Three failure modes with specified recovery:

### 1. Attestation Verification Failure

The integrator cross-references attestations against actual artifacts and finds discrepancies (e.g., cited commit SHA doesn't exist, cited tests didn't actually run, cited files weren't modified).

**Orchestrator behavior:**
- Integrator sends `CONSENSUS_NACK` to the offending agent on behalf of its verification role, with specific discrepancies cited
- The offending agent's CONFIRMED status is revoked; it must re-propose with accurate attestations
- If the agent re-proposes with the same false attestations, the integrator escalates to HITL after K attempts
- This is the mechanism that makes costly signals actually costly — fabrication is detected and punished

### 2. Partial Consensus at Timeout

The phase times out with some agents CONFIRMED and others still stuck (e.g., 4/6 confirmed, 2 in NACK loops).

**Orchestrator behavior:**
- Evaluate which agents are still blocking, using the review adjacency graph and role criticality
- **Critical roles unconfirmed** (rev_code, checker, tester): Block the phase. Create HITL escalation with the full approval matrix showing who NACKed whom and why. Human decides whether to override, restart the stuck agents, or intervene.
- **Non-critical roles unconfirmed** (documenter): Proceed with HITL notification. Log the gap. The integrator notes the incomplete consensus in the PR description.
- Role criticality is configurable per phase in the review adjacency definition

### 3. Agent Crash Mid-Protocol

An agent crashes (container exits) after proposing but before the review phase completes. Remaining agents can't get the crashed agent's ACK/review.

**Orchestrator behavior:**
1. Detect crash via container exit event on the stream
2. Evaluate the crashed agent's role:
   - **Producer crashed**: Its proposal stands (already on the stream). Reviewers can still ACK/NACK it. If the reviewers NACK and the producer can't respond, escalate to HITL or restart the agent.
   - **Reviewer crashed**: Remove the crashed reviewer from the review graph. Re-evaluate whether remaining ACKs satisfy consensus for the producers it was reviewing. If yes, proceed. If no (the crashed reviewer was the only reviewer for a producer), restart the agent or escalate.
3. Restarted agents can replay missed messages from Redis Stream (messages are persistent) and rejoin the protocol where they left off.

---

## Cost and Latency Analysis

Consensus overhead for a sparse review graph (N=6 agents, ~8 review edges):

| Item | Estimate |
|------|----------|
| Messages per consensus round (proposals + reviews + confirmations) | ~20-25 |
| Added token cost per run (Sonnet) | ~$0.25-0.50 |
| Added token cost per run (Opus) | ~$1.25-2.50 |
| Added latency per consensus round | ~1-3 minutes |
| Monthly cost at 500 runs (Sonnet) | ~$125-250 |
| Monthly cost at 500 runs (Opus) | ~$625-1250 |

This is reasonable relative to total pipeline cost ($5-50+ per run). The sparse review graph keeps costs ~3-4× lower than full N×N would be.

---

## Decision

**Option C (Full)** — Implement all three layers from the start: Redis Streams transport, BRC protocol with asymmetric review graph, and evidence-backed deliberation. Adjust the *strictness* of reasoning-layer constraints based on observed behavior.

Rationale:
- The reasoning layer dictates protocol structure — designing BRC without considering attestations and ordering would require restructuring later when those constraints are added
- LLM agents *can* reason about each other's work. Not leveraging that capability means building a consensus protocol that could be used by dumb processes — we should design for the agents we have
- The central risk is **premature convergence** (Stag Hunt model). Evidence requirements and anti-sycophancy measures address this directly — deferring them means shipping a protocol that's vulnerable to the exact problem we're solving
- The tuning knob is the **strictness** of attestation requirements per role, not whether attestations exist. Start with all constraints active, relax where they prove unnecessary

**Rollout approach**: One-shot replacement. The current polling + READY-tallying system is replaced wholesale. No backwards compatibility layer or phased rollout — the new protocol ships complete. Implementation is organized into phases for development purposes, but the feature lands as a single unit.

---

## Implementation Scope

**Server-side:**
- Replace in-memory `MessageStore` with Redis Streams backend (`pipeline:{id}:messages`), using `XREAD` with server-side role filtering in the API layer
- Add long-polling support to message poll endpoint (`wait` parameter — orchestrator uses `XREAD BLOCK`, applies role filter, returns on match or timeout)
- New consensus message types: `CONSENSUS_PROPOSE`, `CONSENSUS_ACK`, `CONSENSUS_NACK`, `CONSENSUS_WITHDRAW`, `CONSENSUS_CONFIRMED`
- New `PeerConsensusTracker` — observes consensus messages on the stream, tracks sparse approval matrix per asymmetric review graph
- Asymmetric review graph definition per phase (producers vs. reviewers, configurable adjacency + role criticality)
- Update `ConsensusEvaluator` to use peer protocol observations instead of READY tallying
- Per-role attestation schema validation on `CONSENSUS_PROPOSE` and `CONSENSUS_ACK/NACK` messages (configurable strictness)
- Scoped re-evaluation logic: track which ACKs reference which artifacts, invalidate only affected ACKs on re-proposal
- Delphi-style proposal visibility: conditional release of producer PROPOSE messages to reviewer roles — hold until reviewer has submitted independent evaluation. Implementation is a check in the long-polling endpoint (is requester a reviewer for the proposer? has reviewer already submitted evaluation?), not a general visibility filter.
- Consensus failure mode handlers: attestation verification rejection, partial consensus at timeout with role-criticality evaluation, agent crash with review graph removal and re-evaluation

**Agent CLI:**
- `egg-orch message poll --wait <seconds>` — long-polling mode (blocks until messages arrive)
- `egg-orch consensus propose` — broadcast proposal with summary + per-role attestations
- `egg-orch consensus ack <role>` — agree with a peer's proposal (must reference specific artifacts reviewed)
- `egg-orch consensus nack <role>` — disagree with specific, actionable reason + artifact references
- `egg-orch consensus withdraw` — retract own proposal (must cite new information)
- `egg-orch consensus status` — show current approval matrix and agent states

**Agent prompts (CLAUDE.md):**
- Replace "signal READY and poll in a loop" with full BRC protocol instructions
- Separate instructions for producer vs. reviewer roles
- Per-role attestation requirements in proposals and reviews (see Layer 3 tables)
- Anti-sycophancy: ACKs must cite specific artifacts; reviewers must identify at least one concern (or explicitly reason about absence)
- Reviewers form independent judgments from git before seeing producer self-assessments

**Consensus wrapper** (significant rework):
- The wrapper must understand BRC state to generate appropriate recovery prompts. This is a non-trivial change from the current READY-checking logic.
- Detect which BRC phase the agent was in when it exited: WORKING (hadn't proposed yet), PROPOSED (proposed but not all reviews in), REVIEWING (reviewer mid-evaluation), CONFIRMED (done — no recovery needed)
- Read the agent's last-seen message ID from Redis Stream to determine what happened while the agent was down
- Generate phase-appropriate recovery prompts:
  - Exited in WORKING → "Complete your work and propose"
  - Exited in PROPOSED → "Your proposal is on the stream. Check for ACKs/NACKs and respond. Here are messages you missed: [replay from Redis]"
  - Exited in REVIEWING → "You were reviewing proposals. Here's what you've ACKed/NACKed so far, and here are new proposals since you left: [replay from Redis]"
- Max K restarts per agent (existing behavior, preserved)

**Types:**
- New `ConsensusPhase` enum: WORKING, PROPOSED, REVIEWING, CONFIRMED
- New consensus message types in `MessageType`
- Per-role attestation schemas with configurable strictness (separate schemas for producer proposals and reviewer evaluations)
- Sparse approval matrix model (asymmetric adjacency graph + per-edge ACK/NACK state + role criticality)
- Review graph definition per phase (producers, reviewers, edges, criticality)

**Commitment devices:**
- Cooldown period after PROPOSED (prevent flip-flopping)
- Retraction requires citing specific new information
- Lockout after K flip-flops per producer → orchestrator HITL escalation

**Cross-verification:**
- Integrator checks attestations against actual artifacts (commits exist, tests ran, cited files were modified, review comments posted)
- Attestation verification failure triggers NACK from integrator (see Consensus Failure Modes)
- Prompt effectiveness tracking: measure NACK rates, post-merge defect rates, attestation quality scores per role prompt version

---

## Success Criteria

1. **Transport**: Agents receive messages within ~1s of being sent (not 30s)
2. **Protocol**: Consensus is reached through asymmetric peer review, not orchestrator decree
3. **Reasoning**: Each agent can articulate *why* it agreed (referencing specific artifacts it reviewed)
4. **Robustness**: Failures, disagreements, NACK cascades, and timeouts handled gracefully per specified failure modes
5. **Anti-sycophancy**: Reviewers produce substantive evaluations citing specific artifacts, not rubber stamps
6. **Observable**: The orchestrator can see the full approval matrix and act on it
7. **Cost**: Consensus overhead < 10% of total pipeline cost

---

## References

### Distributed Systems
- Two-Phase Commit Protocol — Wikipedia, Martin Fowler
- Three-Phase Commit — Paper Trail
- Barrier Synchronization — Wikipedia
- Gossip Protocols — Montresor (2017)
- Redis Streams — Redis Documentation

### Sociology & Group Decision-Making
- Habermas Machine — Science (2024) — demonstrated LLM-mediated consensus among human participants; we adapt its structured-claims approach to agent-to-agent interaction
- DelphiAgent — ScienceDirect (2025)
- CONSENSAGENT (sycophancy mitigation) — ACL 2025
- Voting vs Consensus in Multi-Agent Debate — ACL 2025
- Emergent Social Conventions in LLM Populations — Science Advances (2025)
- Social Choice for AI Alignment — ICML 2024
- LLM-Powered Devil's Advocate — IUI 2024
- Multi-Agent LLM Debate Scaling — ICLR 2025 Blog — confirms rigid adversarial roles backfire
- Memory in Multi-Agent Systems — TechRxiv (2025)
- Shared Mental Models in Human-AI Teams — SAGE (2025)

### Game Theory
- Crawford & Sobel (1982) — Cheap talk
- Spence Signaling Model — Costly signals for credible communication
- Stag Hunt / Coordination Games — Cooperation equilibria with assurance
- Mechanism Design — Vickrey-Clarke-Groves
- Multi-Agent AI Coordination Survey — arXiv (2025)
- Consensus Algorithms for Distributed Agent Systems — Muthu.co (2025)
