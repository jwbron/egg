### Task Analysis

**Problem statement**: A BRC consensus deadlock occurs when a reviewer NACKs a producer, then confirms (enters terminal CONFIRMED state) before the producer re-proposes. The producer is permanently blocked because the reviewer never re-reviews the new proposal.

**Source context**: Issue #1576, observed in pipeline `issue-1551` (2026-04-08). Timeline: reviewer_code NACKs tester, confirms 9 seconds later, tester re-proposes ~6 minutes later, tester permanently blocked with "not fully ACKed. Pending reviewers: ['reviewer_code']".

**System context**: The BRC consensus protocol lives in `PeerConsensusTracker` (`orchestrator/peer_consensus.py`). Each reviewer-producer relationship is tracked in `ApprovalMatrix` with states PENDING/ACKED/NACKED. When a producer re-proposes, `_un_confirm_stale_reviewers()` (line 814) is called to pull confirmed reviewers back to REVIEWING. The signal handler in `routes/signals.py:935` sends `CONSENSUS_RE_REVIEW` messages to stale reviewers.

**Technical root cause**: `handle_confirmed()` (line 382) allows a reviewer to confirm with outstanding NACKs. The version-match guard (line 412) only checks for stale ACKs (`entry.state == ApprovalState.ACKED`). A NACKED entry is invisible to this guard. So reviewer_code NACKs tester, then confirms — the tracker sees the NACK as "has_reviewed = True" (line 255: `state != PENDING`) and the version guard skips it entirely. The reviewer enters terminal CONFIRMED state, stops processing new proposals, and the NACKed producer can never get ACKed.

**Files affected**:
- `orchestrator/peer_consensus.py` — Add unresolved-NACK guard in `handle_confirmed()`
- `orchestrator/tests/test_peer_consensus_integration.py` — Add tests for the #1576 scenario