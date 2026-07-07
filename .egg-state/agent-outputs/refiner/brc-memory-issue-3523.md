# Refiner BRC memory — issue-3523

## Task
Review-quality overhaul: (1) structured findings + code-computed verdict, (2) 3-state
verification ladder, (3) method-angle procedures in code-review criteria, (4) deterministic
risk router gating lenses + effort tiers, (5) shared-evidence prompt prefix (cost bet).
All behavior-shifting pieces ride `off→log→on` (slice_green_gate precedent). Out of scope:
eval harness, human-feedback loops.

## My proposal (refine analysis)
- Artifact: `.egg-state/drafts/3523-analysis.md`
- Grounded all 5 items in verified live seams (review_graph.py, approval_matrix.py,
  consensus_wrapper.py `--effort`, agent_model_resolution.py, attestation_schemas.py,
  slice_green_gate.py `green_gate_mode()`, event_prompt/ cacheable prefix, conditional-ack.md).
- Incorporated the two overseer-relayed operator directives:
  1. Mirror Claude Code `/review` skill vocabulary (tier ladder low/med/high/xhigh, finder
     angles A–E, 3-state verify with tier-scaled stance, finding schema, xhigh gap sweep) for
     items 2/3/4.
  2. Cost is first-class: router defaults low-risk slices to lower tiers; item 5 cache-hit
     rate + per-wave cost measured in log mode = explicit acceptance criterion gating `on`.
- Registered NO HITL decisions — rationale: issue is fully prescriptive (fields, sequencing,
  flags, floors, out-of-scope all fixed); remaining choices are implementation-level (architect
  in plan phase). This rationale is my HITL-ledger attestation.

## Verdict stance
Proposed. Sequencing 1→2→3→4 per issue: item3+item2-prompt first (zero-regret), item1 next
(structural), item4 (log-first), item5 (flagged, last). Hold this line on re-review unless a
reviewer names a concrete missing/invented requirement.
