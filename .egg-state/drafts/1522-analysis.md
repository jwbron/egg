### Task Analysis

**Problem statement**: During BRC consensus, `reviewer_code` received zero messages from the message bus, causing a deadlock that required human intervention to restart reviewer agents. The other reviewers (`reviewer_contract`, `tester`) received and processed proposals normally.

**Source context**: Issue #1522, observed in pipeline `issue-1512` (PR #1521). The overseer raised a HITL decision citing message bus routing failure. After restarting reviewer agents, proposals were delivered within seconds — suggesting a transient failure.

**Workarounds**: Human selected "Restart reviewer agents and replay all proposals" to unblock. This is not sustainable for unattended pipelines.

**System context**: The message bus is polling-based — agents call `egg-orch message poll --wait 30` to receive messages. The orchestrator stores messages in Redis Streams and serves them via `GET /api/v1/pipelines/{id}/messages?role={role}`. When a producer proposes, the signal handler (`routes/signals.py:916-930`) writes a `CONSENSUS_PROPOSE` message with `to_role="all"` to the message store.

There is a **Delphi visibility filter** at `routes/messages.py:173-193` that intercepts message polling for reviewers. For each `CONSENSUS_PROPOSE` message, it checks whether the polling reviewer has already ACK'd/NACK'd the producer (`tracker.matrix.has_reviewed(role, producer)`). If the reviewer has NOT yet reviewed, the entire message is **withheld** — dropped from the poll response.

The intent is anti-sycophancy: prevent reviewers from being influenced by the producer's self-assessment before forming their own opinion. However, reviewers use `egg-orch message poll` to discover that proposals exist (per agent instructions in `mission.md:140`: "Reviewers: prepare → poll for proposals → review → ACK/NACK → confirm → stay alive"). This creates a deadlock: the reviewer waits for a PROPOSE message to know when to start reviewing, but Delphi withholds it until the reviewer reviews.

**Technical root cause**: The Delphi filter at `routes/messages.py:184-191` drops `CONSENSUS_PROPOSE` messages entirely for reviewers who haven't evaluated the producer. In the implement phase review graph (`review_graph.py:226-238`), `reviewer_code` has edges to `coder`, `tester`, and `documenter`. When coder proposes, the filter withholds the message from `reviewer_code` because `has_reviewed("reviewer_code", "coder")` returns `False` (approval_matrix.py:255-258 — checks that entry exists and state != PENDING).

The intermittent nature is explained by LLM-driven agent behavior: some reviewer agents proactively check `egg-orch consensus status` or inspect git commits instead of waiting for poll messages, accidentally bypassing the filter. After restart, the agent may take a different approach. But any reviewer that follows the documented "poll for proposals" workflow will deadlock.

**Files affected**:
- `orchestrator/routes/messages.py` — Replace full message withholding with a redacted pass-through
- `orchestrator/tests/test_messages.py` — Update existing Delphi test and add redaction verification test

**Risks / edge cases**:
- The redacted message's `subject` field still contains "Proposal from <role>" — this is intentional metadata, not the producer's rationale. The `body` (which contains `payload.summary`) is what gets stripped.
- Agents that already handle zero-PROPOSE results gracefully (by checking consensus status) will continue to work — they'll just also receive the redacted notification, which is additive.
- The `test_propose_visible_after_reviewer_evaluates` test should continue passing (full message visible after ACK).