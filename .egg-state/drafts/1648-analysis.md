### Task Analysis

**Problem statement**: In the BRC consensus protocol, `reviewer_contract` was able to confirm consensus even though `tester` (a producer) had never proposed. This violates the protocol invariant that all deliverables must exist before any agent confirms.

**Source context**: Issue #1648, observed in pipeline `issue-1646` (implement phase, 5 agents). The owner's comment identifies the root cause: the zero-proposal guard is scoped per-reviewer (only checks assigned producers), so `reviewer_contract` — which only reviews `coder` — isn't blocked by `tester` never having proposed.

**System context**: The BRC protocol uses `check_confirm_guard()` in `action_guards.py` to gate confirmation. For reviewers, Guard 2 (line 363-383) iterates `graph.producers_for(agent_role)` — which returns only producers this reviewer is assigned to review. For `reviewer_contract`, the review graph (`review_graph.py:233`) only has an edge to `coder`. So when `coder` and `documenter` have both proposed, the per-reviewer guard passes for `reviewer_contract` even though `tester` has `proposal_version == 0`. The `_check_consensus()` method (peer_consensus.py:1367) only checks whether all roles are in the `_confirmed` set — it has no proposal-version check.

**Technical root cause**: There is no **global** zero-proposal guard. The existing guard at `action_guards.py:363-383` only checks `graph.producers_for(agent_role)` — producers assigned to the specific reviewer. When a producer (like `tester`) is not in a reviewer's edge list (like `reviewer_contract`), the guard doesn't fire. No agent should be able to confirm consensus while any producer in the graph has never proposed, regardless of review assignments.

**Files affected**:
- `orchestrator/action_guards.py` — Add global zero-proposal guard in `check_confirm_guard`, before the producer/reviewer-specific guards
- `orchestrator/peer_consensus.py` — Add explicit handling for the new `global_zero_proposal` guard type in `handle_confirmed`
- `orchestrator/tests/test_action_guards.py` — Add tests for the global guard

**Risks / edge cases**: The global guard is strictly more conservative than the status quo — it prevents confirmations that would have been allowed before. This is the correct behavior per the protocol invariants. The per-reviewer guard (Guard 2) is kept for defense-in-depth and its more specific error message, though the global guard will fire first when applicable.