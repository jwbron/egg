# Analysis: Design formal BRC protocol state machine with action guards

> Issue: #1579 | Phase: refine

## Problem Statement

The BRC (Broadcast-Review-Converge) consensus protocol in `orchestrator/peer_consensus.py` has accumulated individual fixes for specific deadlock scenarios (#1405, #1576, #1411, #1598), but lacks a formal specification of **when each agent action is valid** given the current protocol state. This leads to subtle bugs where legal-but-problematic action sequences create deadlocks — most recently observed in production pipelines `KORE-1191-complete` and `issue-1536` where agents entered terminal polling loops after being fully ACKed but never confirming.

**Current state**: The protocol has grown organically with ad-hoc guards added reactively as deadlocks were discovered. Guards are scattered across `handle_confirmed()`, `_handle_propose_inner()`, and the signal handlers in `routes/signals.py`, making it difficult to verify completeness or reason about correctness.

**Desired outcome**: A formal state machine specification with explicit preconditions (guards) for each protocol action, a state transition diagram, invariants that must hold at all times, and an evaluation of whether existing recovery mechanisms remain necessary or become redundant.

## Current Behavior

### Protocol Architecture

The BRC protocol coordinates multi-agent consensus across four components:

1. **`PeerConsensusTracker`** (`orchestrator/peer_consensus.py`) — Central state machine managing per-agent `ConsensusPhase`, the review graph, and the approval matrix. Thread-safe via `RLock`.

2. **`ApprovalMatrix`** (`orchestrator/approval_matrix.py`) — Sparse matrix tracking `ApprovalState` (PENDING/ACKED/NACKED) per review edge (reviewer → producer), with version numbers, artifact references, and revision counts.

3. **`ReviewGraph`** (`orchestrator/review_graph.py`) — Immutable asymmetric topology defining which roles review which. Edges have criticality (CRITICAL/ADVISORY). Supports dual-role agents (e.g., tester is both producer and reviewer).

4. **Signal handlers** (`orchestrator/routes/signals.py`, lines 855–1329) — REST endpoints bridging agent containers to the tracker, including commit SHA verification, attestation validation, tester check coverage, and Delphi redaction.

### Current State Machines

**Producer states** (per `ConsensusPhase` enum in `shared/egg_orchestrator/types.py`):
```
WORKING → PROPOSED → CONFIRMED
    ↑         |
    ├─────────┘  (NACK → WORKING)
    └─────────┘  (WITHDRAW → WORKING)
```

**Reviewer states**:
```
WORKING → REVIEWING → CONFIRMED
             |   ↑
             └───┘  (producer re-proposes → re-review)
```

### Existing Guards by Action

| Action | Guard | Location | Issue |
|--------|-------|----------|-------|
| **propose** | Reject if fully ACKed and in PROPOSED state | `_handle_propose_inner()` L137–148 | #1185 |
| **propose** | Validate attestation payload (schema + role) | `_handle_propose_inner()` L151–160 | — |
| **re_propose** | Skips ACK guard (always legitimate after NACK) | `handle_re_propose()` L532 | — |
| **ack** | Validate review edge exists in graph | `handle_ack()` L214–215 | — |
| **ack** | Validate attestation payload | `handle_ack()` L221–227 | — |
| **nack** | Validate review edge exists | `handle_nack()` L271 | — |
| **nack** | Revision count escalation check | `handle_nack()` L301–305 | — |
| **confirm (producer)** | Must be `is_fully_acked()` | `handle_confirmed()` L387 | — |
| **confirm (reviewer)** | Must have reviewed all assigned producers | `handle_confirmed()` L401–406 | — |
| **confirm (reviewer)** | Version-match guard: ACK version == proposal version | `handle_confirmed()` L412–439 | #1405 |
| **confirm (reviewer)** | Unresolved-NACK guard: no open NACKs against producers | `handle_confirmed()` L441–472 | #1576 |
| **confirm (dual-role)** | Both producer and reviewer phases must be CONFIRMED | `handle_confirmed()` L477–483 | — |
| **withdraw** | Requires reason citing new information | `handle_withdraw()` L339–340 | — |
| **withdraw** | Cooldown enforcement (default 30s) | `handle_withdraw()` L343–350 | — |
| **withdraw** | Flip-flop lockout (default 3 cycles) | `handle_withdraw()` L353–368 | — |

### Existing Recovery Mechanisms

| Mechanism | Purpose | Location |
|-----------|---------|----------|
| `_un_confirm_stale_reviewers()` | Un-confirm reviewers whose ACKs are stale after producer re-proposes | L924–969 |
| `_invalidate_pre_proposal_acks()` | Invalidate version-0 ACKs that can never match post-proposal version | L971–1006 |
| `invalidate_overlapping_acks()` | Scoped re-evaluation: invalidate only ACKs for changed artifacts on re-proposal | `ApprovalMatrix` L220–245 |
| `reconstruct_tracker_from_messages()` | Rebuild tracker state by replaying Redis messages after orchestrator restart | L1055–1207 |
| Message-bus authoritative fallback | Accept confirmation if all roles have CONSENSUS_CONFIRMED messages (tracker lost) | `signals.py` L1232–1277 |

### Known Gaps (from issue and reproduction comments)

1. **"ACKed but never confirms" failure mode** (KORE-1191-complete, issue-1536): Agents lose track of their BRC state and enter infinite heartbeat/poll loops. Nothing in the protocol enforces the ACKed → CONFIRMED transition — it relies entirely on agent behavior.

2. **No auto-repropose on push/commit**: When a producer pushes new commits after proposing, existing reviews become stale. Currently, there is no mechanism to automatically invalidate reviews and trigger re-review. Reviewers can confirm based on outdated code.

3. **Confirm with zero proposals** (#1598): A reviewer can NACK a non-delivering producer, then confirm, causing consensus to complete without the primary deliverable.

4. **Stale confirm state view** (issue-1536 tester): ACK was delivered via message bus before the approval matrix was updated, causing a race condition where the confirm endpoint rejected a legitimately ACKed agent.

5. **Withdraw as escape from stale state** (issue-1536 tester): Agent withdraws and re-proposes to "fix" a stale state view, but this resets proposal state and requires re-review from already-confirmed reviewers — an anti-pattern that worsens the situation.

## Constraints

### Technical
- **Thread safety**: All state mutations in `PeerConsensusTracker` are under an `RLock`. Any new guards or transitions must maintain this invariant.
- **Backward compatibility**: The signal handler API (`/api/v1/pipelines/{id}/signal`) is the contract between agents and the orchestrator. Changes must be backward-compatible or coordinated with agent prompt updates.
- **Message reconstruction**: `reconstruct_tracker_from_messages()` replays historical messages to rebuild state. Any new state (e.g., tracked commit SHAs per ACK) must be reconstructable from messages.
- **Existing test suite**: 30+ test classes in `test_peer_consensus_integration.py` (2758 lines) covering happy paths, 6+ deadlock scenarios, and edge cases. Changes must not break existing tests.
- **Attestation validation**: Proposal/review payloads have strict schema requirements. New fields require schema updates in `attestation_schemas.py`.

### Architectural
- **Agent-side vs orchestrator-side enforcement**: The protocol can enforce invariants server-side (in `PeerConsensusTracker`) or agent-side (via prompt instructions). Server-side enforcement is stronger but requires API changes. Agent-side is brittle (as demonstrated by the "ACKed but never confirms" failures).
- **Separation of concerns**: The `PeerConsensusTracker` manages protocol state; `routes/signals.py` handles HTTP transport and ancillary validation (commit SHA verification, tester checks). Guards should live in the tracker, not scattered across signal handlers.

### Dependencies
- **Agent prompts**: The BRC lifecycle instructions in agent prompts (CLAUDE.md) describe the expected agent behavior. Formal state machine changes may require corresponding prompt updates.
- **Health monitoring**: `get_fully_acked_producers()` (L867–889) is used by the health monitor to detect agents stuck in the fully-ACKed state. New states or transitions may affect health check logic.
- **Concurrent execution**: BRC is active in `refine`, `plan`, and `implement` phases. Changes affect all concurrent pipeline phases.

## Options Considered

### Option A: Formal state machine with comprehensive action guards (server-side enforcement)

**Approach**: Define an explicit state machine enum for both producer and reviewer roles with transition tables. Each action handler checks preconditions against the current state before executing. Add missing guards:
- **Confirm guard for "all changes reviewed"**: Track commit SHA at ACK time; on confirm, verify reviewer's ACK SHA matches producer's current commit SHA.
- **Auto-repropose on push**: When a producer sends a new proposal (or when detected via commit SHA change), automatically invalidate stale reviews and notify reviewers.
- **Confirm guard for "no zero-proposal producers"**: Reject reviewer confirmation if any assigned producer has never proposed (version 0).
- **Withdraw guard for fully-ACKed state**: Reject withdrawal when producer is already fully ACKed (withdraw is irrational in this state).

**Pros**:
- Eliminates deadlocks by construction — invalid transitions are rejected at the API level
- Makes the protocol formally verifiable; invariants can be checked programmatically
- Centralizes all guards in one place (the state machine transition table)
- Agent behavior bugs (like "ACKed but never confirms") can be detected and auto-corrected server-side

**Cons**:
- Significant refactor of `PeerConsensusTracker` — all handler methods need restructuring
- Risk of regression in existing behavior that works correctly
- Auto-repropose on push requires either gateway integration or polling mechanism
- May require API changes for new state tracking (e.g., commit SHA per ACK)

### Option B: Incremental guard additions with design document

**Approach**: Keep the existing handler structure but add the missing guards identified in the issue one at a time, similar to how #1576's unresolved-NACK guard was added. Document the state machine and invariants as a design specification without restructuring the code.

**Pros**:
- Lower risk — each guard is a small, testable addition
- Proven pattern (this is how #1405, #1576, #1411 were addressed)
- Can be deployed incrementally without coordinated changes
- No structural refactor needed

**Cons**:
- Guards remain scattered across multiple methods — harder to verify completeness
- Doesn't address the root cause (lack of formal specification)
- "ACKed but never confirms" requires server-side auto-transition, which is beyond simple guard addition
- Auto-repropose on push still needs a new mechanism regardless

### Option C: Server-side auto-transitions with orchestrator-driven protocol

**Approach**: Shift the protocol from agent-driven to orchestrator-driven. Instead of agents calling `confirmed` themselves, the orchestrator automatically transitions agents when preconditions are met (e.g., auto-confirm when fully ACKed, auto-repropose when new commits detected). Agents report work status; the orchestrator drives state transitions.

**Pros**:
- Eliminates the "ACKed but never confirms" class of failures entirely
- Removes dependency on agent prompt fidelity for protocol correctness
- Cleaner separation: agents do work, orchestrator manages protocol
- Most robust against agent behavioral failures

**Cons**:
- Largest scope of change — requires rearchitecting the signal flow
- Current agents actively participate in the protocol (propose, confirm); this removes that participation
- May reduce agent autonomy in ways that affect other design goals
- Harder to test and reason about (orchestrator becomes more stateful)
- Could mask real issues where agents are stuck on work (not just on protocol)

## Recommended Approach

**Option A: Formal state machine with comprehensive action guards** is recommended.

This approach directly addresses the root cause identified in the issue: the protocol lacks a formal specification of valid action preconditions. It provides the strongest guarantees against deadlocks while keeping agents as active protocol participants (unlike Option C).

The key insight from the production failures is that **the protocol is correct on paper but incorrect in implementation** — the guards that exist are individually correct, but there are gaps between them that allow agents to reach states from which no valid transition exists. A formal state machine with a complete transition table makes these gaps visible and testable.

Option B (incremental guards) is the lower-risk path but doesn't solve the structural problem. The protocol will continue to accumulate patches for deadlocks as new edge cases emerge. The KORE-1191 and issue-1536 failures demonstrate that guard additions alone don't prevent agent-side behavioral failures.

The recommended implementation should:
1. Define a formal `ProducerState` and `ReviewerState` enum with all valid states
2. Create a transition table mapping `(current_state, action) → (new_state, guard_fn)`
3. Consolidate all existing guards into the transition table
4. Add the missing guards (all-changes-reviewed, no-zero-proposal-confirm, auto-repropose)
5. Keep existing recovery mechanisms as defense-in-depth (they serve as circuit breakers even if guards are comprehensive)
6. Add a server-side mechanism to detect "stuck in fully-ACKed" producers and send orchestrator-initiated nudges or auto-confirm

**Complexity assessment: high** — This is an architectural change to a core subsystem with cross-cutting implications for agent prompts, health monitoring, signal handlers, and test infrastructure. However, the scope is well-bounded to the four files identified in the issue.

## Open Questions

The following questions need human input to proceed with planning. Each has been registered as a HITL decision or feedback item via the orchestrator.

### Q1: Auto-repropose trigger mechanism

Should auto-repropose on push/commit be implemented at the orchestrator level (the orchestrator detects new commits on the branch and auto-triggers re-proposal) or should agents remain responsible for explicitly calling re-propose after pushing?

**Options**:
- **Orchestrator-level**: Gateway intercepts `git push` and triggers a re-propose signal, or orchestrator polls for new commits on the branch
- **Agent-level**: Agents call `consensus re-propose` explicitly after pushing (current pattern, but with stronger prompt instructions)
- **Hybrid**: Orchestrator detects new commits and sends `CONSENSUS_RE_REVIEW` to reviewers, but does not create a new proposal version — the producer must still explicitly re-propose

### Q2: Change tracking mechanism for "all changes reviewed" guard

How should the "all producer changes must be reviewed" invariant be tracked?

**Options**:
- **Commit SHA-based**: Track the commit SHA at ACK time; on reviewer confirm, compare against producer's current HEAD commit
- **Version-based**: Extend the existing version mechanism to auto-increment when new commits are detected
- **Both**: Version for protocol state transitions, SHA for verification/auditing

### Q3: Handling the "ACKed but never confirms" agent failure

Should the orchestrator auto-confirm producers who are fully ACKed but haven't confirmed within a timeout, or should it only nudge agents?

**Options**:
- **Auto-confirm**: Orchestrator confirms on behalf of the agent after a grace period (e.g., 2 minutes post full-ACK)
- **Nudge only**: Send a targeted `OVERSEER_ALERT` message to the stuck agent; escalate to HITL if no response
- **Both**: Nudge first, auto-confirm after escalated timeout

### Q4: Recovery mechanism disposition

Should existing recovery mechanisms (`_un_confirm_stale_reviewers`, `_invalidate_pre_proposal_acks`) be retained as defense-in-depth or removed if the formal state machine guards make them redundant?

**Options**:
- **Retain all**: Keep as defense-in-depth even if guards should prevent the conditions they handle
- **Remove redundant**: Remove mechanisms whose triggering conditions are prevented by the new guards
- **Retain but deprecate**: Keep with log warnings to monitor whether they still trigger, remove in a follow-up

### Q5: Scope of "no zero-proposal confirm" guard

Should a reviewer be blocked from confirming if *any* assigned producer has zero proposals, or only if *critical* producers have zero proposals? (Advisory producers might legitimately not need to produce anything.)

**Options**:
- **All producers**: Reviewer cannot confirm if any assigned producer has version 0
- **Critical producers only**: Only block if a producer on a CRITICAL edge has version 0
- **Configurable**: Add a per-edge flag indicating whether the producer is expected to produce

### Q6: State machine formalization scope

Should the formal state machine be purely a design document (documentation + invariant assertions in code), or should it be implemented as an executable state machine (e.g., with a transition table that replaces the current handler if-else logic)?

**Options**:
- **Executable state machine**: Replace handler logic with a transition table; guards are functions in the table
- **Design document + assertions**: Document the state machine formally; add `assert` checks at handler entry/exit to verify invariants
- **Hybrid**: Transition table for the core state, existing handlers for side effects (events, messages, etc.)

---

*Authored-by: egg*
