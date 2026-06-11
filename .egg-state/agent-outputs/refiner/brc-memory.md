# refiner BRC memory — issue-3077 (refine)

## Status
- v1 proposal: analysis written to `.egg-state/drafts/3077-analysis.md`, committed + pushed, CONSENSUS_PROPOSE sent.
- HITL decisions registered: cq-1 (scope A/B/C; recommended C = full remaining scope), cq-2 (durability bar; recommended fail-loud on memory backend).

## Verdict / position
- Phase 1 of #3077 already landed (PR #3078 + #3083); this pipeline covers the remainder.
- Recommended scope (Option C): R1 non-silent sync_to_proposals (consensus_wrapper.py:487-539 → surface failure in event_prompt.py rendering), artifact spec module, spec-derived propose validation (generalize signals.py:1076-1139), gateway artifact-read endpoint by artifact name (unblocks #3002), phase-3 prose cleanup (REVIEWER-SYNC.md, event_prompt fallback text) + docs invariant + ratchet test, bounded durability (fail-loud memory backend + Redis restart-semantics test).
- Key grounded facts: no _clear_concurrent_state() exists anymore (nearest: reset_message_store(), message_store.py:636-639); path knowledge hardcoded in phase_filter.py:605-627, signals.py:1162-1166, event_prompt.py:447/1186, shared/egg_restrictions/phase_patterns.py.

## If NACKed
- Address reviewer points by editing the analysis in place, re-commit, re-propose (version bumps). Keep scope options A/B/C structure unless a reviewer shows a factual error.
