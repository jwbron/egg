# Agent Teams and Deliberative Consensus

Agent teams are groups of LLM agents that work concurrently on a shared task and must reach agreement before advancing. This guide covers the motivation, architecture, and protocol design for agent team communication and Deliberative Consensus in egg.

For operational details on the current concurrent execution mode, see [Concurrent Execution](concurrent-execution.md). This guide focuses on the design principles and **Deliberative Consensus** — the combination of a structured consensus protocol (BRC) and an evidence-backed reasoning layer — that replaces the original orchestrator-centric readiness tallying.

## Why Agent Teams

Single-agent pipelines are sequential: one agent does work, a reviewer checks it, the pipeline advances. This is simple but slow. Concurrent execution (multiple agents working in parallel) is faster but introduces a coordination problem — when are all agents actually done, and is their combined output coherent?

The naive solution — each agent independently tells the orchestrator "I'm READY" and the orchestrator tallies votes — has two problems:

1. **No peer verification.** An agent saying "READY" means it *thinks* its work is good. LLM agents are unreliable reasoners. The only mechanism to catch errors an agent can't see is peer review.

2. **No mutual agreement.** Agents never agree *with each other* — they announce their state to a central authority. The orchestrator declares consensus, but no agent has actually evaluated any other agent's output.

Agent teams solve this by replacing orchestrator-decreed consensus with **Deliberative Consensus** — agents communicate directly, review each other's work with verifiable evidence, and individually confirm they agree with the group.

## Three Layers of the Problem

Agent team coordination spans three distinct layers. Solutions that address only one or two layers leave gaps that surface as quality problems in production.

| Layer | Concern | Domain |
|-------|---------|--------|
| **Transport** | How messages are delivered between agents | Distributed systems |
| **Protocol** | Rules governing proposals, acknowledgments, and state transitions | Distributed consensus |
| **Reasoning** | Agents genuinely evaluating each other's work and forming independent judgments | Sociology, game theory |

The protocol and reasoning layers are independently variable but jointly necessary — BRC without the reasoning layer is just vote-counting with extra steps, and evidence-backed reasoning without protocol structure has no enforcement mechanism. Together they form **Deliberative Consensus**: agents reach agreement through a structured process (BRC) grounded in verifiable evidence (Evidence-Backed Deliberation).

### Transport: Redis Streams + Long-Polling

The original implementation used 30-second polling intervals — a message sent at t=0 might not be seen until t=29s later. The replacement uses Redis Streams with long-polling for near-instant delivery (~1s).

**Architecture:**

- Single Redis Stream per pipeline: `pipeline:{id}:messages`
- Agents interact via the orchestrator API, not directly with Redis
- **Read pattern:** `XREAD` with server-side role filtering. Each agent maintains its own cursor via `since_id`
- **Write pattern:** Agents POST messages via the orchestrator API, which writes to the stream
- **Long-polling:** The orchestrator uses `XREAD BLOCK` and applies role filters, returning immediately when matching messages arrive or on timeout

**Agent CLI:**

```bash
# Long-poll for messages (blocks until messages arrive or timeout)
egg-orch message poll --wait 30
```

### Protocol: Broadcast-Review-Converge (BRC)

BRC is a structured consensus protocol inspired by Interactive Consistency, Ack-All, and the Delphi method. It has three phases.

#### Asymmetric Review Topology

Not all agents review all other agents. The review graph is **asymmetric by role type**:

- **Producers** (coder, tester, documenter): Create artifacts and propose them for review
- **Reviewers** (reviewer_code, reviewer_contract): Evaluate producers' proposals and issue ACK/NACK judgments

This eliminates circular ACK problems. A coder doesn't ACK a reviewer's review of its own code — it *responds to NACKs* by revising and re-proposing.

**Review adjacency (who reviews whom):**

| Reviewer | Reviews proposals from |
|----------|----------------------|
| reviewer_code | coder, tester |
| reviewer_contract | coder |
| tester | coder (implicitly — writes tests against the code, runs lint/type-checks, ACKs if tests and checks pass) |

The tester has a **dual role**: it is both a producer (proposes test artifacts) and a reviewer (evaluates coder's work by running tests and lint/type-checks against it).

This gives 5 directed review edges (4 critical + 1 advisory to documenter) for the default implement phase instead of ~20 for full N=5 pairwise review. The edge count varies by phase configuration.

#### BRC Phases

**Phase 1 — Broadcast** (barrier sync)

Each producer completes its work and broadcasts a `CONSENSUS_PROPOSE` to all peers. The proposal includes a summary of work done, artifacts produced, and per-role attestations (see [Reasoning Layer](#reasoning-evidence-backed-deliberation) below). The protocol waits until all producers have proposed (or times out and escalates missing agents).

**Phase 2 — Review** (asymmetric ack)

Each reviewer evaluates proposals from its assigned producers and sends `CONSENSUS_ACK` (agree, with artifact references) or `CONSENSUS_NACK` (disagree, with structured reason and artifact references). A NACK drops the producer back to WORKING — the producer addresses the concern and re-proposes.

Producers can also withdraw their own proposal by sending `CONSENSUS_WITHDRAW`, which returns them to WORKING. Withdrawal must cite specific new information justifying the retraction (e.g., discovering a failing test or a design flaw after proposing). This is a commitment device — withdrawal without justification is rejected.

Scoped re-evaluation: a NACK targets a specific proposal version. Re-proposal only triggers re-review from the NACKing reviewer. Other reviewers' prior ACKs whose referenced artifacts were unaffected are not invalidated (their approval state is preserved), but reviewers must still re-ACK at the new proposal version for `is_fully_acked` to pass. However, any reviewer that had already *confirmed* on a prior proposal version is automatically un-confirmed and receives a `CONSENSUS_RE_REVIEW` message, requiring them to re-review the new proposal — this prevents deadlocks where a stale-confirmed reviewer never sees updated artifacts.

For example: `reviewer_code` NACKs `coder` citing a bug in `auth.py`. The coder fixes the bug and re-proposes. `reviewer_code` must re-review. The tester's prior ACK (which referenced test results, not `auth.py`) is preserved — it is not invalidated, but the tester must still re-ACK at the new proposal version for consensus to advance. If the tester had already *confirmed*, they are additionally un-confirmed and receive `CONSENSUS_RE_REVIEW`.

Revision rounds are bounded: max 2 rounds (`max_revision_rounds`) per producer-reviewer pair before HITL escalation.

**Phase 3 — Converge** (confirmation)

When a producer has proposed and received ACKs from all assigned reviewers, it broadcasts `CONSENSUS_CONFIRMED`. When a reviewer has ACKed all assigned producers, it broadcasts `CONSENSUS_CONFIRMED`. When all agents have confirmed, the orchestrator observes consensus and acts. The orchestrator does not *decide* consensus — it *observes* that all agents independently confirmed.

#### State Machines

**Producer:**

```
WORKING → PROPOSED → CONFIRMED
    ↑         |
    ├─────────┘  (NACK received → address concern → re-propose)
    └─────────┘  (WITHDRAW sent → cite new information → revise)
```

**Reviewer:**

```
WORKING → REVIEWING → CONFIRMED
             |   ↑
             └───┘  (producer re-proposes → re-review if affected)
```

Reviewers transition from WORKING to REVIEWING when they detect new commits from a producer they're assigned to review (via `git log` or branch inspection), not when they receive a PROPOSE message on the stream.

#### Delphi-Style Ordering

To prevent anchoring and sycophancy, the protocol enforces independent evaluation:

- Reviewers see git artifacts (commits, files) immediately — they're in the repo
- The server holds back the producer's `CONSENSUS_PROPOSE` message (self-assessment, attestations) from reviewers until the reviewer has submitted their own initial ACK/NACK
- After the reviewer posts their independent evaluation, the server releases the producer's self-assessment metadata

This is a conditional release on one message type to one role category — a check in the long-polling endpoint, not a general visibility filter.

### Reasoning: Evidence-Backed Deliberation

The reasoning layer ensures agents don't just signal states — they make **structured claims** that peers evaluate. This draws on Habermasian discourse theory, signaling theory, and sycophancy mitigation research.

#### Per-Role Attestations

**Producer proposals (`CONSENSUS_PROPOSE`):**

| Role | Required attestation |
|------|---------------------|
| Coder | Commit SHAs, files changed, test pass/fail summary, one risk considered |
| Tester | Tests written/run count, coverage delta, edge cases covered, one concern considered |
| Documenter | Sections updated, links verified, one concern considered |

**Reviewer evaluations (`CONSENSUS_ACK/NACK`):**

| Role | Required attestation |
|------|---------------------|
| Reviewer (code) | Files reviewed (specific paths), issues found + resolved count, one risk considered |
| Reviewer (contract) | Tasks verified (specific IDs), acceptance criteria checked, gaps identified |

#### Cheap Talk vs Costly Signals

Not all messages need the same rigor:

| Message type | Signal cost | Rationale |
|-------------|-------------|-----------|
| STATUS, PROGRESS | Cheap talk | Low overhead, informative when interests are aligned |
| CONSENSUS_PROPOSE | Costly signal | Attestations are harder to produce without doing the work |
| CONSENSUS_ACK | Costly signal | Must reference specific artifacts reviewed (prevents rubber-stamping) |
| CONSENSUS_NACK | Costly signal | Must include specific, actionable objection with artifact references |

This distinction comes from game theory: cheap talk (Crawford & Sobel, 1982) works when interests are fully aligned, but LLM agents are *unreliable communicators* — they may genuinely believe bad work is good. Costly signals (requiring verifiable evidence) address this.

#### Anti-Sycophancy Measures

Research (CONSENSAGENT, ACL 2025) shows LLM agents exhibit strong sycophancy in group settings. The protocol mitigates this structurally:

1. **Artifact reference requirement.** ACKs and NACKs must cite specific file paths, line numbers, commit SHAs, or test names. Generic "looks good" is rejected by schema validation. This is the primary anti-sycophancy mechanism — citing specific artifacts is mechanically hard to satisfy without doing the work.

2. **Delphi ordering.** Reviewers form independent judgments from git artifacts before seeing the producer's self-assessment.

3. **Critical thinking prompt.** Every proposal/review must include "one risk I considered." This is a secondary measure — easy to satisfy with generic output and should not be weighted equally.

4. **Dynamic prompt refinement.** Evolve prompts based on observed rubber-stamping patterns rather than relying solely on procedural rules.

## Consensus Failure Modes

### Attestation Verification Failure

### Partial Consensus at Timeout

The phase times out with some agents confirmed and others stuck (e.g., 4/6 confirmed, 2 in NACK loops).

**Recovery:** The orchestrator evaluates which agents are blocking using the review graph and role criticality:

- **Critical roles unconfirmed** (reviewer_code, tester): Block the phase. Create HITL escalation with the full approval matrix. Human decides whether to override, restart, or intervene.
- **Non-critical roles unconfirmed** (documenter): Proceed with HITL notification. Incomplete consensus is noted in the PR description.

Role criticality is configurable per phase in the review adjacency definition.

### Agent Crash Mid-Protocol

An agent crashes after proposing but before the review phase completes.

**Recovery:**

1. Detect crash via container exit event on the stream
2. **Producer crashed:** Its proposal stands (already on the stream). Reviewers can still ACK/NACK. If reviewers NACK and the producer can't respond, escalate to HITL or restart.
3. **Reviewer crashed:** Remove from the review graph. Re-evaluate whether remaining ACKs satisfy consensus. If the crashed reviewer was the only reviewer for a producer, restart or escalate.
4. Restarted agents replay missed messages from Redis Stream and rejoin the protocol.

## Commitment Devices

To prevent flip-flopping that destroys signal value:

- **Cooldown period** after PROPOSED (prevent rapid state oscillation)
- **Retraction** requires citing specific new information
- **Lockout** after 3 flip-flops (`max_flip_flops`) per producer triggers orchestrator HITL escalation

## Cost and Latency

Consensus overhead for a sparse review graph (N=5 agents, 5 review edges in default configuration):

| Item | Estimate |
|------|----------|
| Messages per consensus round | ~20-25 |
| Added token cost per run (Sonnet) | ~$0.25-0.50 |
| Added token cost per run (Opus) | ~$1.25-2.50 |
| Added latency per consensus round | ~1-3 minutes |

This is reasonable relative to total pipeline cost ($5-50+ per run). The sparse review graph keeps costs ~3-4x lower than full NxN.

## Implementation Scope

### Server-Side

- Replace in-memory `MessageStore` with Redis Streams backend
- Add long-polling support to message poll endpoint (`wait` parameter)
- New consensus message types: `CONSENSUS_PROPOSE`, `CONSENSUS_ACK`, `CONSENSUS_NACK`, `CONSENSUS_WITHDRAW`, `CONSENSUS_CONFIRMED`
- `PeerConsensusTracker` — observes consensus messages on the stream, tracks sparse approval matrix
- Asymmetric review graph definition per phase (producers vs reviewers, adjacency, criticality)
- Per-role attestation schema validation with configurable strictness
- Scoped re-evaluation logic and Delphi-style proposal visibility
- Consensus failure mode handlers

### Agent CLI

- `egg-orch message poll --wait <seconds>` — long-polling mode
- `egg-orch consensus propose` — broadcast proposal with attestations
- `egg-orch consensus ack <role>` — agree with a peer's proposal (must reference artifacts)
- `egg-orch consensus nack <role>` — disagree with structured reason
- `egg-orch consensus withdraw` — retract own proposal (must cite new information)
- `egg-orch consensus status` — show approval matrix and agent states

### Agent Prompts

- Replace "signal READY and poll in a loop" with full BRC protocol instructions
- Separate instructions for producer vs reviewer roles
- Per-role attestation requirements
- Anti-sycophancy: ACKs must cite specific artifacts; reviewers form independent judgments before seeing producer self-assessments

## Research Foundations

The protocol design draws on research across three domains.

### Distributed Systems

| Protocol | Fit | What we borrow |
|----------|-----|---------------|
| Interactive Consistency | High | Every participant agrees on every other's output — adapted for probabilistic rather than crash-stop failures |
| Ack-All | High | Core primitive: broadcast → collect ACK/NACK from all peers → unanimous ACK required |
| Two-Phase Commit (2PC) | Moderate | Vote-then-commit structure; centralized fallback |
| Barrier Synchronization | Partial | Barriers separate phases (all outputs submitted → review → resolve) |
| Reliable Broadcast | Already have | The message bus provides this; consensus is layered on top |

### Sociology and Group Decision-Making

| Model | Key insight | Protocol implication |
|-------|------------|---------------------|
| Habermasian discourse | Consensus requires validity claims that survive challenge — not just assertions. Note: original Habermas Machine research involved *human* participants; this protocol adapts the framework to agent-to-agent interaction where sycophancy replaces strategic misrepresentation. | READY signals must include structured claims. Peers challenge claims, not just vote. |
| Delphi method | Iterated anonymous feedback converges better than single-shot voting | Agents assess independently before seeing others' states. Bounded revision rounds before final consensus. |
| Nominal Group Technique | Separate generation from evaluation to prevent anchoring | Agents assess independently → share in fixed order → discuss → vote. |
| Social Choice Theory (Arrow's theorem) | No perfect aggregation, but unanimity + structured deliberation avoids the worst pathologies. ACL 2025 comparison of 7 decision protocols found unanimity with structured deliberation outperforms majority voting for LLM agents. | Unanimity gives every agent veto power → require structured justification to veto. |
| Groupthink / Sycophancy (CONSENSAGENT) | LLM agents have strong sycophancy in group settings. ICLR 2025 scaling study found rigid adversarial "Devil's Advocate" roles backfire — this is why the protocol has no dedicated adversarial agent role and instead embeds critical evaluation structurally. | Embed critical evaluation structurally, not just via role names. |
| Stigmergy | Agents coordinate through artifacts; traces require cognitive infrastructure for interpretation | Make handoffs richer and structured. Ensure agents have context to interpret them. |
| Shared Mental Models | Teams need shared understanding of "done" | Make acceptance criteria explicit and verifiable per role before the phase starts. |

### Game Theory

| Concept | Key insight | Protocol implication |
|---------|------------|---------------------|
| Mechanism design | Design rules so thorough work is the dominant strategy | Require evidence-backed signals. Make rubber-stamping detectable. |
| Incentive compatibility | Premature READY should be detectable | READY signals include verifiable attestations — transforms cheap talk into costly signals. |
| Coordination games (Stag Hunt) | The real risk is everyone settling on low effort | Make effort visible via PROGRESS messages. Sequential revelation reduces uncertainty about others' effort. |
| Signaling theory | Credible signals are harder to produce without doing the work | Per-role attestation requirements tied to actual artifacts. |
| Principal-agent problem | The orchestrator can't observe effort directly | Reviewers cross-reference attestations against actual artifacts as part of the BRC protocol. |
| Commitment devices | READY must be meaningful; free flip-flopping destroys signal value | Cooldown after PROPOSED. Retraction requires citing new information. Lockout after 3 flip-flops (`max_flip_flops`). |

## References

### Distributed Systems

- [Two-Phase Commit Protocol](https://en.wikipedia.org/wiki/Two-phase_commit_protocol)
- [Three-Phase Commit](https://www.the-paper-trail.org/post/2008-11-29-consensus-protocols-three-phase-commit/)
- [Barrier Synchronization](https://en.wikipedia.org/wiki/Barrier_(computer_science))
- [Redis Streams](https://redis.io/docs/data-types/streams/)

### Sociology and Group Decision-Making

- Habermas Machine — [Science (2024)](https://www.science.org/doi/10.1126/science.adq2852)
- DelphiAgent — [ScienceDirect (2025)](https://www.sciencedirect.com/science/article/abs/pii/S0306457325001827)
- CONSENSAGENT (sycophancy mitigation) — [ACL 2025](https://aclanthology.org/2025.findings-acl.1141/)
- Voting vs Consensus in Multi-Agent Debate — [ACL 2025](https://aclanthology.org/2025.findings-acl.606.pdf)
- Emergent Social Conventions in LLM Populations — [Science Advances (2025)](https://www.science.org/doi/10.1126/sciadv.adu9368)
- Multi-Agent LLM Debate Scaling — [ICLR 2025 Blog](https://d2jud02ci9yv69.cloudfront.net/2025-04-28-mad-159/blog/mad/)

### Game Theory

- Crawford & Sobel (1982) — Cheap talk equilibria; adapted for unreliable rather than strategic communicators
- Spence Signaling Model — Costly signals for credible communication
- [Multi-Agent AI Coordination Survey (2025)](https://arxiv.org/html/2502.14743v2)
- [Consensus Algorithms for Distributed Agent Systems (2025)](https://notes.muthu.co/2025/11/consensus-algorithms-for-coordinating-agreement-in-distributed-agent-systems/)

## Related Documentation

- [Concurrent Execution](concurrent-execution.md) — Current concurrent execution mode (operational reference)
- [SDLC Pipeline](sdlc-pipeline.md) — Standard wave-based execution
- [Agent Roles Reference](../reference/agent-roles.md) — All agent roles and permissions
- [Orchestrator Architecture](../architecture/orchestrator.md) — Deployment modes and API details
