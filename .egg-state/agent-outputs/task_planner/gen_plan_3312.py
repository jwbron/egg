import json

d = json.load(open('.egg-state/drafts/3312-plan-task-planner.json'))
phases = d['phases']
TOT = sum(len(p['tasks']) for p in phases)

def yblock(text, indent):
    pad = ' ' * indent
    return '\n'.join((pad + ln) if ln else '' for ln in text.rstrip('\n').split('\n'))

out = []
W = out.append

W("# Plan — issue #3312: decompose 19 oversize Python files; empty the file-size allowlist")
W("")
W("> Refresh of #3111 → #2817 → #2261. The canonical pattern + worked reference")
W("> landed in **merged PR #2335**; **no decomposition slice has ever landed.**")
W("> Scope is **LOCKED** by the operator directive and the resolved refine HITL")
W("> (decision-1, 2026-06-26): all 19 files, including `pipelines.py` (~27.2k) and")
W("> `gateway.py` (~10.4k); `_run_pipeline` tackled head-on; **no descope.**")
W("> **v2:** folds in the R3 container-image COPY mitigation (reviewer_plan NACK v1).")
W("")
W("## Approach")
W("")
W("19 per-file decomposition slices (one slice = one file = one PR), ordered")
W("**easiest → hardest** by line count with the two structural outliers")
W("(`gateway.py`, `pipelines.py`) at the **tail, fully in scope**. The pattern-adoption")
W("parent that anchored the #2261 DAG is already merged (#2335), so there is no parent")
W("slice; but because every slice edits the shared `scripts/file-size-allowlist.yaml`,")
W("the slices are serialized into **one linear dependency chain** (`slice-1 → … →")
W("slice-19`) per #3046 — independent branches off the shared base would collide at")
W("integration on that file. Easiest-first ordering along the chain banks value early")
W("given two prior attempts that never landed a slice.")
W("")
W("Each slice follows the canonical recipe (`docs/guides/decomposition-pattern.md`,")
W("worked reference `scripts/select_tests/`, template PR #2335): external-importer")
W("audit → step-0 `git mv` baseline commit → cluster extraction with an explicit")
W("per-symbol re-export barrel + underscore-prefixed private submodules → allowlist")
W("drop + concrete CLAUDE.md seam coverage → **R3 container-COPY parity (where")
W("applicable)** → `make lint` + `make test-all` green.")
W("")
W("## Grounding (verified against the refine analysis + live tree, 2026-06-26)")
W("")
W("- **19 files, live counts authoritative.** 9 breach the byte cap. Caps: 1,500 lines / 100 KB.")
W("- **Back-compat surface:** `routes.pipelines.*` ~57 patch targets across ~137 test")
W("  files (dominant); `gateway.gateway.*` ~12. Coder re-runs the section-(d) audit per cluster.")
W("- **Seam grounding correction (honored):** `orchestrator/CLAUDE.md` and")
W("  `gateway/CLAUDE.md` carry **no** `#2261`/TBD placeholder rows — seam work is")
W("  **create concrete rows**, not retag. `sandbox/CLAUDE.md` exists w/o a seam table")
W("  (add one, slice-1); `shared/CLAUDE.md` is **absent** (create it, slice-7).")
W("- **`_run_pipeline`** (non-negotiable #7): split into per-phase handlers + a thin")
W("  orchestration loop inside the `pipelines.py` slice — addressed directly, not deferred.")
W("")
cp = d['container_packaging']
W("## Container packaging (R3 mitigation — addresses reviewer_plan NACK v1)")
W("")
W(cp['problem'])
W("")
W("| Slices | Files | Image-copy mechanism | Action |")
W("|---|---|---|---|")
for r in cp['dispositions']:
    W(f"| {r['slices']} | {r['files']} | {r['mechanism']} | {r['action']} |")
W("")
W("**Why source-tree gates miss this:** `make lint` / `make test-all` never build the")
W("image, so a dropped `COPY` is invisible. Every R3-affected slice therefore carries a")
W("same-slice Dockerfile `COPY` task **and** an image-build + import smoke check;")
W("transparent-shipping slices carry a confirm-by-grounding-and-smoke task.")
W("")
W(f"## Slice DAG (19 slices · {TOT} tasks) — easiest → hardest")
W("")
W("```")
for p in phases:
    tag = "  ⟵ STRUCTURAL OUTLIER" if p.get('outlier') else ""
    W(f"{p['slice']:>9}: {p['file']:<42} {p['lines']:>6} lines{tag}")
W("```")
W("")
W("**Single linear dependency chain** `slice-1 → slice-2 → … → slice-19` (every slice")
W("edits `scripts/file-size-allowlist.yaml`; #3046 forbids unordered overlap). The chain")
W("preserves easiest-first ordering so 17 simpler files bank value before the two outliers.")
W("")
W("## Acceptance criteria")
W("")
for ac in d['acceptance_criteria']:
    W(f"- {ac}")
W("")
W("## Non-goals")
W("")
for ng in d['non_goals']:
    W(f"- {ng}")
W("")
W("## Risks carried to reviewers")
W("")
for r in d['risks_for_reviewers']:
    W(f"- {r}")
W("")

W("```yaml")
W("# yaml-tasks")
W("pr:")
W('  title: "Decompose 19 oversize Python files; clear the file-size allowlist (#3312)"')
W("  description: |")
W(yblock(
"""The `make lint` cap (1,500 lines / 100 KB, added by #2250 closing #2248) caps Python
source files; 19 files are grandfathered in `scripts/file-size-allowlist.yaml`. This
program decomposes ALL 19 into sub-packages following the canonical pattern (merged PR
#2335 + `docs/guides/decomposition-pattern.md` + worked reference `scripts/select_tests/`),
driving the allowlist `files:` map to EMPTY.

Each file `F.py` becomes `F/__init__.py` (an explicit per-symbol re-export barrel, the
stable public API) plus underscore-prefixed private `_*.py` submodules. Test patch
targets keep resolving through the barrel. The `_run_pipeline` state machine is split
into per-phase handlers + a thin loop directly (non-negotiable #7). `gateway.py`'s
`@app.route` decorators stay in `__init__.py` (non-negotiable #8).

R3 container parity (v2): slices converting a top-level module copied by a NON-recursive
Dockerfile glob add the matching recursive `COPY <pkg>/` in the SAME slice + an
image-build/import smoke check, because the source-tree gates cannot catch a missing COPY.

Scope is locked (operator directive + resolved refine HITL): all 19 files, no descope.
Pure refactor — no behavior changes; bugs surfaced are separate `Part of #3312`
follow-ups. Branches prefixed `egg/`. Implements #3312.""", 4))
W("  test_plan: |")
W(yblock(
"""- Automated: `make lint` + `make test-all` green at EVERY slice boundary. A missed
  re-export fails the test that patches the moved symbol. Each slice runs the section-(d)
  `git grep` audit before extraction.
- Per slice: step-0 `git mv` baseline commit is independently green; every submodule
  verified under BOTH caps (further-split in-slice, never a fresh allowlist entry).
- R3 container gate: slices converting a top-level module under a NON-recursive Dockerfile
  glob (orchestrator 3/10/13/14/16, gateway 11/12/18) build the affected image and run an
  import smoke check IN-SLICE; slice-9 builds the sandbox image and smoke-starts the
  ENTRYPOINT; transparent-shipping slices (1/8/17, routes) confirm by grounding + smoke.
- `_run_pipeline` handlers stay private, tested THROUGH `_run_pipeline` via existing dense
  seam coverage (test_consensus_polling, test_brc_nack_iteration, test_concurrent_*,
  test_advance_phase_*); isolation tests are a follow-up.
- Run with `make test` (changeset-aware) inner-loop; full suite via `make test-all`.""", 4))
W("  manual_steps: |")
W(yblock(
"""Pre-merge (per slice): reviewer spot-checks (a) the __init__.py re-export list against
`git grep`, (b) submodule clustering is cohesive, (c) the CLAUDE.md seam row matches.
Per R3 slice: build the affected container image and run the import/ENTRYPOINT smoke check
(the source-tree gates do not build images).
Pre-merge (final slice = pipelines.py): verify the allowlist `files:` map is EMPTY and all
four CLAUDE.md seam tables are populated.
Post-merge: none (pure refactor; no migrations/config/deploy).""", 4))
W("phases:")
for i, p in enumerate(phases, 1):
    dep_note = ("depends on the previous slice (single linear chain: every slice edits "
                "scripts/file-size-allowlist.yaml, so per #3046 overlapping slices must be "
                "serialized)") if i > 1 else "head of the linear chain (no parent)"
    extra = " STRUCTURAL OUTLIER — scheduled last, fully in scope." if p.get('outlier') else ""
    goal = (f"Decompose `{p['file']}` ({p['lines']} lines"
            f"{', over byte cap' if p['over_byte_cap'] else ''}) into a sub-package; "
            f"drop its allowlist entry; seam coverage in {p['claude_md']}. {dep_note}.{extra}")
    W(f"  - id: {i}")
    W(f"    name: {json.dumps(p['name'])}")
    W(f"    goal: {json.dumps(goal)}")
    if i == 1:
        W("    dependencies: []")
    else:
        W("    dependencies:")
        W(f"      - {i-1}")
    W("    tasks:")
    for t in p['tasks']:
        W(f"      - id: {t['id']}")
        W(f"        description: {json.dumps(t['description'])}")
        W(f"        acceptance: {json.dumps(t['acceptance_criteria'])}")
        W("        files:")
        for f in t['files_affected']:
            W(f"          - {f}")
W("```")

open('.egg-state/drafts/3312-plan.md', 'w').write('\n'.join(out) + '\n')
print("wrote 3312-plan.md;", TOT, "tasks")
