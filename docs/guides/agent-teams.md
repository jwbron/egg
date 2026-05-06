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

- **Producers** (coder, tester, documenter, autofixer, conflict_resolver): Create artifacts and propose them for review. Includes execution-category and utility-category agents.
- **Reviewers** (reviewer_code, reviewer_code_holistic, reviewer_contract, reviewer_security, reviewer_concurrency): Evaluate producers' proposals and issue ACK/NACK judgments. All review-category agents.

This eliminates circular ACK problems. A coder doesn't ACK a reviewer's review of its own code — it *responds to NACKs* by revising and re-proposing.

**Review adjacency (who reviews whom):**

| Reviewer | Reviews proposals from |
|----------|----------------------|
| reviewer_code | coder, tester |
| reviewer_code_holistic | coder, tester |
| reviewer_contract | coder |
| reviewer_security | coder, tester |
| reviewer_concurrency | coder, tester |
| tester | coder (implicitly — writes tests against the code, runs lint/type-checks, ACKs if tests and checks pass) |

The tester has a **dual role**: it is both a producer (proposes test artifacts) and a reviewer (evaluates coder's work by running tests and lint/type-checks against it).

This gives 11 directed review edges (10 critical + 1 advisory to documenter) for the default implement phase instead of ~56 for full N=8 pairwise review (3 producers + 6 reviewers, with tester counted once for its dual role). The edge count varies by phase configuration.

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

**Persistence:** All BRC messages — including proposals, ACK/NACK rationales, artifact references, commit SHAs, and version metadata — are losslessly persisted to the PR branch at each phase boundary. The committed `.egg-state/brc-history/{identifier}-{phase}.md` (human-readable with YAML metadata blocks) and `.json` (machine-readable) files provide the authoritative record. The PR body itself inlines the final round's content so human reviewers see what was said during consensus directly on GitHub. See [Concurrent Execution — BRC History Persistence](concurrent-execution.md#brc-history-persistence) for format details.

#### State Machines

**Producer:**

```
WORKING → PROPOSED → CONFIRMED
    ↑         │  ▲
    ├─────────┘  │  (NACK received → address concern → re-propose)
    ├─────────┘  │  (WITHDRAW sent → cite new information → revise)
    │            │
    │            └── (push/commit triggers auto re-propose → back to PROPOSED,
    │                 incrementing version and invalidating stale reviews)
    └────────────────
```

**Reviewer:**

```
WORKING → REVIEWING → CONFIRMED
             │   ▲          │
             └───┘          │  (producer re-proposes → CONSENSUS_RE_REVIEW
                     ▲      │   → un-confirms reviewer → back to REVIEWING)
                     └──────┘
```

> For the formal state machine with complete transition diagrams and action guard specifications, see [Concurrent Execution — Formal BRC State Machine](concurrent-execution.md#formal-brc-state-machine).

Reviewers transition from WORKING to REVIEWING when they receive a `CONSENSUS_PROPOSE` message from a producer they're assigned to review. While waiting, reviewers may read the contract/plan to prepare, but MUST NOT inspect the filesystem for producer artifacts — the producer may not have started yet. Once the proposal arrives (initially as a redacted message with `delphi_redacted=True`), reviewers examine the referenced git artifacts (commits, files) to form their independent judgment. The producer's self-assessment metadata is withheld via Delphi redaction until the reviewer submits their evaluation (see Delphi-Style Ordering below).

#### Delphi-Style Ordering

To prevent anchoring and sycophancy, the protocol enforces independent evaluation:

- Reviewers see git artifacts (commits, files) immediately — they're in the repo
- When a reviewer polls for messages before submitting their own ACK/NACK, `CONSENSUS_PROPOSE` messages are **redacted**: the `body` is cleared, `metadata.payload` keys are stripped (except `version` and `commit_sha`), and `metadata.delphi_redacted` is set to `True`. This tells the reviewer a proposal exists — unblocking their "poll for proposals" workflow — without exposing the producer's self-assessment
- After the reviewer posts their independent evaluation, subsequent polls return the full unredacted `CONSENSUS_PROPOSE` message with the producer's self-assessment metadata

This is a conditional redaction on one message type to one role category — a check in the long-polling endpoint, not a general visibility filter.

### Reasoning: Evidence-Backed Deliberation

The reasoning layer ensures agents don't just signal states — they make **structured claims** that peers evaluate. This draws on Habermasian discourse theory, signaling theory, and sycophancy mitigation research.

#### Per-Role Attestations

**Producer proposals (`CONSENSUS_PROPOSE`):**

| Role | Required attestation |
|------|---------------------|
| Coder | Commit SHAs, files changed, test pass/fail summary, one risk considered |
| Tester | Tests written/run count, coverage delta, edge cases covered, one concern considered, `checks_passed` list of all configured checks that passed (see notes below) |
| Documenter | Sections updated, links verified, one concern considered |

> **Tester blocked-execution attestation:** If tests could not execute (e.g., private network mode blocks dependency downloads), the Tester must set `tests_execution_blocked: true` with a `tests_execution_blocked_reason` explaining why. The orchestrator accepts this in place of a passing test count.

> **Tester no-op propose (refactor / doc-only slices, #2431):** When a slice warrants no new tests — pure refactor (symbol moves, no behavior change), doc-only changes, or similar — the Tester must still propose to satisfy BRC consensus (every producer must propose at least once, otherwise reviewers cannot confirm). For that case, run all configured checks against the coder's diff and propose with `no_test_changes_needed: true` plus a non-empty `no_test_changes_reason` (e.g. `"slice-3 is a pure decomposition: symbol moves between submodules, no behavior change; existing test coverage applies"`) and the usual `checks_passed` list. `tests_run = 0` is acceptable on this path. Mutually exclusive with `tests_execution_blocked` — the no-op flag means checks ran and passed; the blocked flag means they could not run.

> **Tester `checks_passed` requirement:** The Tester's attestation must include a `checks_passed` list naming every configured check that **passed** (e.g. `["lint", "test"]`). Only include checks with a clean exit — do not include checks that failed. The server validates that all checks listed in `repositories.yaml` appear in this list and rejects the proposal if any are missing. Running tests alone is not sufficient — all configured checks must pass and be reported.

> **Tester `attestation.tests_run` vs propose `tests_run`:** The `attestation.tests_run` field is an **integer count** of tests executed (e.g. `42`). This is distinct from the propose call's top-level `tests_run` argument, which is a **list of test identifiers** (e.g. `["tests/test_foo.py::test_bar"]`). Passing a list for `attestation.tests_run` (or leaving it at the default `0`) causes a validation error. The `mcp__brc__propose` handler validates tester attestation locally before sending to the orchestrator, so misconfigured payloads fail with an actionable error rather than a 400 from the server (#2338).

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
| HANDOFF | Cheap talk | Directed coordination — low overhead, enables role-boundary artifact transfers |
| HEARTBEAT | Cheap talk | Typed agent-state transition (`WORKING`/`WAITING_ON_ROLE`/`WAITING_FOR_EVENT`/`PROPOSED`/`IDLE`) — schema-validated and rate-limited, consumed by the overseer for stall detection (see [Agent Wait Patterns §4](../reference/agent-wait-patterns.md#4-heartbeat-message-type)) |
| CONSENSUS_PROPOSE | Costly signal | Attestations are harder to produce without doing the work |
| CONSENSUS_ACK | Costly signal | Must reference specific artifacts reviewed (prevents rubber-stamping) |
| CONSENSUS_NACK | Costly signal | Must include specific, actionable objection with artifact references |

This distinction comes from game theory: cheap talk (Crawford & Sobel, 1982) works when interests are fully aligned, but LLM agents are *unreliable communicators* — they may genuinely believe bad work is good. Costly signals (requiring verifiable evidence) address this.

> **Directed coordination messages** (`HANDOFF`, `STATUS`, `PROGRESS`, `HEARTBEAT`) are cheap talk by design — they carry no attestation burden and serve to keep agents unblocked. The critical distinction is that they flow *outside* the BRC consensus protocol: a `HANDOFF` message does not replace a `CONSENSUS_PROPOSE`, and clarification questions do not flow as free-form messages. `QUESTION` was removed in [#1897](https://github.com/jwbron/egg/issues/1897) because it had no reliable respondent; reviewer questions now live inside `CONSENSUS_NACK` rationales (where the producer is obligated to address them on re-propose), and "I'm blocked on peer X" is advertised via `HEARTBEAT --state WAITING_ON_ROLE`. See [Concurrent Execution — Directed Coordination](concurrent-execution.md#directed-coordination) for the CLI syntax, message type guidance, and worked examples.

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

### Pre-Proposal ACK Race Condition (Resolved)

When agents work at different speeds, a faster reviewer (e.g., tester) may send `CONSENSUS_ACK` before the slower producer (e.g., coder) has submitted its `CONSENSUS_PROPOSE`. The ACK is recorded at proposal version 0. When the producer later proposes (version 1), the version-0 ACK cannot satisfy `is_fully_acked()`, creating a permanent deadlock — the producer cannot confirm because it has "pending reviewers," but the reviewer has already confirmed and is idle.

**Resolution:** The protocol now handles this at two points:

1. **Propose-time invalidation**: When a producer proposes, `_invalidate_pre_proposal_acks()` detects any version-0 ACKs from non-confirmed reviewers and resets them to `PENDING`. Affected reviewers receive a `CONSENSUS_RE_REVIEW` notification to re-review.

2. **Confirm-time guards**: The formal action guard system (`check_confirm_guard()` in `orchestrator/action_guards.py`) enforces multiple preconditions before allowing confirmation. See [Concurrent Execution — Action Guards](concurrent-execution.md#action-guards) for the complete guard table. Key guards include:
   - **Version-match guard**: All of the reviewer's ACKs must match the current proposal versions. Stale ACKs return `pending_acks` (exit code 2) with instructions to re-ACK the listed producers.
   - **Unresolved-NACK guard**: If the reviewer has NACKed a producer that hasn't re-proposed since, confirmation returns `pending_acks` (exit code 2) — the reviewer must wait for the producer to re-propose and be re-reviewed before confirming.
   - **Global zero-proposal guard** ([#1648](https://github.com/jwbron/egg/issues/1648)): If any producer in the review graph has never proposed (version 0), no agent — producer or reviewer — can confirm. This prevents consensus from completing without all deliverables, even when the confirming agent has no direct review relationship with the non-delivering producer.
   - **Per-reviewer zero-proposal guard** ([#1598](https://github.com/jwbron/egg/issues/1598)): Additionally, if any *assigned* producer has never proposed, the reviewer cannot confirm. Retained as defense-in-depth with a more specific error message.

These two layers — proactive invalidation at propose time and defensive validation at confirm time — ensure that out-of-order ACKs, unresolved NACKs, and non-delivering producers never create unrecoverable deadlocks, regardless of agent timing.

### Auto Re-Propose on Push/Commit

When a producer pushes new commits after proposing, existing reviews become stale. The protocol detects this via the `consensus_producer_push` signal and triggers an automatic re-proposal — incrementing the proposal version, invalidating stale ACKs, and sending `CONSENSUS_RE_REVIEW` to affected reviewers. If the producer hasn't proposed yet (still in WORKING state), the push is a no-op.

This mechanism enforces the principle that **all changes must be reviewed**: post-proposal pushes cannot bypass the review process. The `check_confirm_guard()` provides a server-side blocking mechanism even if a reviewer misses the `CONSENSUS_RE_REVIEW` notification. See [Concurrent Execution — Auto Re-Propose on Push/Commit](concurrent-execution.md#auto-re-propose-on-pushcommit) for the full details.

Additionally, the gateway enforces that **direct `git push` is blocked** for pipeline sessions — agents must use `mcp__brc__propose` (or the fallback CLI `egg-orch consensus propose --push`) to bundle the push with a BRC proposal. This makes the review invariant structural rather than relying on auto-repropose detection. See [Concurrent Execution — Gateway-Level Push Enforcement](concurrent-execution.md#gateway-level-push-enforcement-pipeline-sessions) for details.

### Agent Crash Mid-Protocol

An agent crashes after proposing but before the review phase completes.

**Recovery:**

1. Detect crash via container exit event on the stream
2. **Producer crashed:** Its proposal stands (already on the stream). Reviewers can still ACK/NACK. If reviewers NACK and the producer can't respond, escalate to HITL or restart.
3. **Reviewer crashed:** Remove from the review graph via `excuse_reviewer()`. Re-evaluate whether remaining ACKs satisfy consensus. If the crashed reviewer was the only reviewer for a producer, restart or escalate.
4. **Non-delivering producer:** If a producer never proposes, the global zero-proposal confirm guard ([#1648](https://github.com/jwbron/egg/issues/1648)) prevents *any* agent from confirming — not just reviewers assigned to the non-delivering producer. The orchestrator escalates via HITL decision. See [Concurrent Execution — Excusing Non-Delivering Agents](concurrent-execution.md#excusing-non-delivering-agents).
5. Restarted agents replay missed messages from Redis Stream and rejoin the protocol.

## Commitment Devices

To prevent flip-flopping that destroys signal value:

- **Cooldown period** after PROPOSED (prevent rapid state oscillation)
- **Retraction** requires citing specific new information
- **Lockout** after 3 flip-flops (`max_flip_flops`) per producer triggers orchestrator HITL escalation

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
