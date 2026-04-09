### Task Analysis

**Problem statement**: Pipeline PRs show a phantom "unknown" phase in the BRC consensus summary, and `.egg-state/brc-history/` files are missing from the branch — they're never written because no messages match the phase filter.

**Source context**: Issue #1580 reports two bugs surfaced by PR #1578 (the first pipeline after #1572 introduced BRC consensus context). Pipeline `issue-1526` ran only the implement phase, but the BRC summary attributes messages to "unknown" instead.

**System context**: BRC (Broadcast-Review-Confirm) messages are created in two paths:
1. **`routes/messages.py:97`** — general message REST endpoint, sets `phase=pipeline.current_phase.value` correctly
2. **`routes/signals.py`** — consensus signal handlers (`propose`, `ack`, `nack`, `withdraw`, `confirmed`), which create `Message` objects **without setting the `phase` field** — it defaults to `None`

Downstream, two consumers rely on message phase:
- `_build_brc_consensus_summary()` (line 3053) groups by `msg.phase or "unknown"` → produces the phantom phase
- `_write_brc_history()` (line 2990) filters by `m.phase == phase` → `None != "implement"` → no messages match → no file written → nothing to commit

**Technical root cause**: All 8 `Message()` creations across the 5 consensus signal handlers in `signals.py` (lines 916, 936, 1000, 1014, 1072, 1124, 1233, 1277) omit `phase=`. The `Message` model defaults `phase` to `None`. This is the single root cause of both bugs.

**Files affected**:
- `orchestrator/routes/signals.py` — Add `phase=` to all 8 BRC message creations across 5 handlers
- `orchestrator/tests/test_brc_history.py` — Add regression test for `phase=None` messages

**Risks / edge cases**: 
- The `handle_consensus_propose_signal` already loads the pipeline conditionally (for commit verification). All other handlers would need to load it. A helper function keeps this DRY.
- The fallback path in `handle_consensus_confirmed_signal` (line 1233) has `_phase` already resolved to "implement" or the actual phase — it just doesn't pass it to the `Message`.