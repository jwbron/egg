# refiner BRC memory — issue-3288 (refine)

## Task
Documenter agent + docs: **snapshot of current state, not a ledger of changes.**
Two coupled work streams (planner slices): (1) revise documenter task
instructions so it writes current-state docs, never SDLC artifacts
(slice/TASK/phase/HITL ids); (2) corpus cleanup of existing docs/docstrings.

## Status
- v1 analysis written to `.egg-state/drafts/3288-analysis.md`. Grounded all
  issue code-claims against the live tree (2026-06-26). Registered cq-1
  (cleanup completeness; recommend opt-1 enumerated+bounded sweep) and cq-2
  (durability guardrail; recommend opt-1 no). Committed + proposed v1.

## Verdict / position
- **WS1 (documenter agent) — low risk, two files:**
  `orchestrator/routes/pipelines.py` ("## Your Task" docu branch ~14781 +
  per-phase summary ~6761 + plan-phase branch ~14157) and
  `shared/egg_contracts/agent_roles.py:306` (DOCUMENTER_ROLE description +
  responsibilities). Recommend planner lands WS1 first / early.
- **WS2 (corpus cleanup) — large:** ~260 files carry slice-N/TASK-N refs across
  docs/, gateway/, orchestrator/, shared/. Recommend slice by doc-area/package.
- **Hard constraints to defend:** preserve documenter gateway file boundaries
  (docs/, **/*.md, .egg-state/agent-outputs/) and the BRC no-op propose path
  (#3027 block at ~14800 / ~14157). NOT "delete all issue references" — keep
  rationale links, strip chronology.

## Grounded facts (verified 2026-06-26)
- Documenter ledger nudge text: pipelines.py ~14781 "Update documentation for
  the changes made by the CODER agent"; ~6761 "Focus your documentation on
  changes from plan phase {id}". Role def agent_roles.py:306
  description="Updates documentation for the changes".
- No-op propose path present at pipelines.py ~14800 (#3027 block) and ~14157 —
  PRESERVE. (Minor: no-op example reason itself says "slice-3 is a pure
  decomposition" — ephemeral CLI arg, not a doc; reword optional, low priority.)
- ~260 files with ledger refs (grep slice-[0-9]|TASK-[0-9]|slice [0-9] over
  docs/ gateway/ orchestrator/ shared/ md+py). No existing lint/CI guard.
- High-value corpus targets all exist: docs/architecture/{brc-memory,
  orchestrator,slice-dag,gateway-auto-filter,coordination-state}.md;
  gateway/artifact_api.py, gateway/jira_client.py, orchestrator/kubernetes_spawner.py,
  shared/egg_anchor/protected_root.py, shared/egg_agent/context_discipline.py,
  shared/egg_agent/__main__.py; gateway/CLAUDE.md, orchestrator/CLAUDE.md.

## If NACKed
- Edit `.egg-state/drafts/3288-analysis.md` in place, re-commit, re-propose
  (version bumps). Defend grounded file:line facts; the issue is heavily
  author-specified — don't invent scope. Keep cq-1/cq-2 framing unless a
  reviewer shows a factual error.

## Decision log
- 2026-06-26: grounded issue #3288 (live body) vs tree; wrote 3288-analysis.md;
  registered cq-1 (completeness) + cq-2 (guardrail); proposed v1.
