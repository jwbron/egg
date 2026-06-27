# documenter BRC memory — issue #3312, slice-15

## Verdict: PROPOSED (orchestrator/CLAUDE.md routes/signals/ seam row)
- My commit: 89a24cc31 `docs(orchestrator): add decomposition seam table for routes/signals/ (#3312)`.
- Based on coder decomposition @ 4ee364f5e (coder commits: 42542ce98 baseline git mv →
  c0ca9b3d0 decompose → a2dd8df66 drop allowlist → 4ee364f5e audit+seam handoff).
- Branch: egg/issue-3312-slice-15-documenter/work.

## Slice target
Decompose `orchestrator/routes/signals.py` (3,398 lines / 142,839 bytes — OVER byte cap)
→ sub-package `orchestrator/routes/signals/`. Flask-blueprint shape (`signals_bp`, 2 routes).

## What I did (documenter-owned)
- Appended the `### routes/signals/` seam subsection to orchestrator/CLAUDE.md's
  "## Decomposition seams" section, AFTER the `kubernetes_spawner/` subsection (slice-14),
  BEFORE the landed-decompositions summary paragraph.
- Updated that summary paragraph to append `routes/signals/`.
- Used the coder's handoff draft verbatim (.egg-state/agent-outputs/coder/slice-15-claude-md-seam-row.md);
  VERIFIED its submodule table against on-disk layout — all match:
  __init__.py 170, _responses.py 29, _validation.py 609, _lifecycle.py 676,
  _consensus_verdicts.py 1075 (largest), _consensus_confirm.py 820, _dispatch.py 174.
  (Note: architect-recommended _validation/ sub-sub-package was NOT needed — the validation
  cluster landed at 609 lines, under the 1,500 hard cap, as a single _validation.py.)
- Module-layout table: NO change needed — it references `routes/` as a whole, not per-file
  (consistent with prior route-file slices decisions/phases/deployment/event_prompt).
- #2261 retag: none present in orchestrator/CLAUDE.md (already cleaned in prior slices).
- Packaging-neutral: orchestrator/routes/ already shipped by recursive COPY (Dockerfile:45) →
  no Dockerfile change (coder confirmed).
- file-restriction check: documenter can_write orchestrator/CLAUDE.md = true.

## If re-spawned (review/NACK handling)
- If a reviewer NACKs my seam-row, re-read their reason + re-verify against on-disk signals/
  layout and the coder's current proposal SHA (git log delta), fix, re-commit, re-propose.
- If asked to ACK a peer producer (coder/tester), that's a reviewer action — not my role here
  (EGG_BRC_REVIEWERS=reviewer_contract,reviewer_code; I'm a producer).

## History: was BLOCKED ON CODER from 16:05–16:40 (coder took ~50min on this largest WAVE-3
## target). Coder healthy throughout (no AGENT_FAILED; pipeline status=running; only a benign
## tester agent-loop [low] @16:11). Decomposition landed ~16:40; I proposed immediately after.
