# Analysis: Make the plan phase conformant with the Actionable Plan Framework

> Issue: #2766 | Phase: refine

## Problem Statement

The [Actionable Plan Framework](https://github.com/Khan/actionable-plans)
(private upstream repo) is a shared grammar for plan documents that are
simultaneously human-readable structured markdown and machine-readable typed
data. The framework explicitly names egg's plan contract as one of two efforts
it builds on, and calls out egg's current parser as having a "fragile" markdown
fallback whose stance the framework explicitly inverts: a strict, specified
structured-markdown document is the primary and only representation.

egg's plan phase today produces a hybrid: prose markdown (Summary,
Implementation Phases, Test Strategy, Rollback Plan, a freeform Risk Assessment
table, Migration Notes) **followed by** a `# yaml-tasks` YAML code-fence
appendix. The prose and the YAML are two artifacts: only the YAML appendix
populates the contract; the prose can drift freely. The parser
(`shared/egg_contracts/plan_parser.py`) has three extraction modes — YAML code
fence (preferred), `---` YAML frontmatter (legacy), and a markdown-regex
`[TASK-N-M]` scrape (the "fragile" fallback the spec names) — with multiple
layers of "warn-and-emit-best-effort" placeholder synthesis when a slice has
no parseable tasks.

The desired outcome is that egg's plan document IS the typed contract: one
strict structured-markdown grammar parsed directly into the contract; no YAML
appendix, no frontmatter mode, no regex fallback. New typed record types
(`AC-<n>`, `BL-<n>`, `OQ-<n>`, `R-<n>`) capture acceptance criteria, blockers,
open questions, and risks that egg's contract has no typed home for today, plus
a per-slice `### Validation` block and a top-level `## Impact & Risk` section
aligned with the Impact Analysis Framework's `ExpectedImpact`. The framework's
JSON Schema (`plan-contract-1.0.json`) replaces / extends
`.egg/schemas/yaml-tasks.schema.json` and the plan-shaped subtree of
`.egg/schemas/contract.schema.json`.

## Current Behavior

### Plan document and parser

- **Template** — `docs/templates/plan.md`. Prose sections + a `# yaml-tasks`
  YAML code-fence appendix. The YAML carries the load-bearing structure: a
  `pr:` block (`title`, `description`, `test_plan`, `manual_steps`, plus the
  optional context-PR keys `context_title` / `context_description` from #2548)
  and a `slices:` list (canonical, post-#2137; `phases:` is still accepted as
  a legacy alias).
- **Parser** — `shared/egg_contracts/plan_parser.py`. Three priority levels:
  1. `parse_yaml_code_fence` → `parse_phases_from_yaml` (preferred). Yields
     `ParsedPhase` (keeps the legacy class name; `to_contract_slice`
     emits a `slice-N` id).
  2. `parse_yaml_frontmatter` → `parse_tasks_from_yaml` (legacy `---` mode).
  3. `parse_tasks_from_markdown` → `parse_phases_from_markdown` (markdown
     regex fallback over `[TASK-N-M]` patterns).
- **Validators** — `validate_forest(slices)` (the slice DAG must be an acyclic
  forest; ≤1 parent per slice) and `validate_task_role_alignment(slices, repo)`
  (per-task `role` must be able to push every `files_affected` entry per the
  gateway's pattern blocklist; #2527).
- **Contract** — `shared/egg_contracts/models.py::Contract` (`schemaVersion 1.1`)
  with `slices: list[Slice]`, each `Slice` having
  `id: 'slice-<N>'`, `dependencies`, `serialized_chain_order`, `tasks: list[Task]`,
  each `Task` having `id: 'task-<N>-<M>'`, `description`, `acceptance_criteria`
  (string), `files_affected`, `role` (`coder`/`tester`/`documenter`), plus the
  Jira-mode optional fields `jira_key` / `jira_action` / `jira_action_status`
  (#1557).
- **Schemas** — `.egg/schemas/yaml-tasks.schema.json` (the YAML appendix
  schema) and `.egg/schemas/contract.schema.json` (the full contract).

### Downstream consumers of the plan contract

| Consumer | Location | What it reads / writes |
|---|---|---|
| Orchestrator contract ingestion | `orchestrator/routes/pipelines.py::_populate_contract_from_plan` (~line 18473) | Calls `parse_plan`, runs `validate_forest`, populates `contract.slices` and `contract.pr.*` |
| PR-body composition | `orchestrator/routes/pipelines.py::_build_pr_body` (~line 9292) and `_pr_metadata_from_plan_draft` (~line 9150) | Renders `pr.title` / `description` / `test_plan` / `manual_steps` / `deferred_actions` into the PR body |
| Plan-time CONSENSUS_PROPOSE validation | `orchestrator/routes/signals.py` (~lines 1087–1102) | Calls `parse_plan` + `validate_task_role_alignment` and converts findings into NACK reasons |
| Slice DAG scheduler | `orchestrator/slice_scheduler.py` | Reads `Slice.dependencies`; computes execution waves; routes spawning per DAG parents |
| Slice role spawning | `orchestrator/routes/pipelines.py` (~line 10577) | Reads `(task.role or 'coder') for task in slice.tasks` to spawn the agent roster |
| Impasse role reassignment | `orchestrator/impasse_routing.py` (lines 85, 185, 437) | Reads / mutates `Task.role` on producer failure |
| Planner agent | `plugins/refine-plan/skills/refine-plan/agents/task-planner.md` | Generates the plan markdown and the `# yaml-tasks` YAML appendix |
| Plan reviewer | `plugins/refine-plan/skills/refine-plan/agents/reviewer-plan.md` | Checks the eight-key rubric (`alignment_with_analysis`, `task_breakdown`, `role_assignments`, `slice_dag_shape`, `test_strategy`, `rollback_plan`, `risk_coverage`, `pr_block`) |
| `refine-plan` plugin (portable) | `plugins/refine-plan/skills/refine-plan/bin/{validate-yaml-tasks,emit-contract}` | Pre-spawn validator + portable contract emitter |
| Public API re-export | `shared/egg_contracts/__init__.py` (lines 200–209) | Exports `parse_plan`, `parse_plan_file`, `validate_forest`, `validate_task_role_alignment`, `ParseResult`, etc. |
| Plan-yaml CI check | `.github/scripts/checks/plan_yaml_check.py` | Lints `# yaml-tasks` blocks in plan drafts |

### Record types egg has no typed home for today

- **Acceptance Criteria** — only a per-task `acceptance_criteria: str`. There
  is no `AC-<n>` registry, no slice-level `Satisfies: AC-1, AC-3` reference,
  and the top-level `Contract.acceptance_criteria` list is almost never
  populated in practice.
- **Blockers** — egg's `pre_merge_condition` mechanism (#1998 conditional
  ACKs, surfaced as `deferred_actions` on the PR contract) is the closest
  analogue. There is no `BL-<n>` registry, no category enum
  (`dependency` / `decision` / `external` / `infra` / `data`), and blockers
  don't gate slice scheduling.
- **Open Questions** — the refine phase has `egg-contract add-decision` /
  `add-feedback`, but plan-phase open questions are written as prose into
  the plan-draft narrative; no `OQ-<n>` registry, no `If resolved high` /
  `Suggested action` blocks.
- **Impact & Risk** — egg's template has a freeform Risk Assessment table
  that the parser doesn't extract. The `risk_analyst` plan-phase subagent
  emits a sibling JSON (`risk_analyst-output.json`) with shape
  `{risks: [{name, category, likelihood, impact, evidence, mitigation,
  owns_task}], top_3_risks, blocking_concerns}`, but the contract
  never sees this JSON — only what the `task_planner` chose to copy into
  the prose Risk Assessment table. The issue says egg "already produces
  `ExpectedImpact` JSON via the `/impact-analysis` skill" but no such skill,
  type, or producer exists in this repo (see Open Question Q1).

### "Single document grammar" gaps egg has today

- **YAML appendix is a separate artifact.** The prose and the YAML are two
  documents bound together. The reviewer rubric calls this out: criterion 8
  reviews the `pr:` YAML block; criteria 5–7 review the prose. Drift between
  prose and YAML is a recurring failure mode (#1974, #2743 logged
  YAML-appendix bugs that broke this seam).
- **Markdown regex fallback.** `parse_tasks_from_markdown` ships partial
  contracts when the YAML fence is missing or malformed. The framework
  inverts this stance: a plan that doesn't parse is "not executable",
  reported as a structured error.
- **Placeholder synthesis.** When a slice contains no parseable tasks,
  the parser synthesises a `Review phase '<name>' manually` placeholder
  task with `PLACEHOLDER_ACCEPTANCE_CRITERIA = "Human verification"`
  (`plan_parser.py:1374-1391`). This silently produces an "executable"
  contract from a plan that doesn't actually have executable content.
- **ID format.** Contract ids are `slice-<N>` and `task-<N>-<M>` (lowercase
  on the contract; the planner's display form is uppercase `TASK-<N>-<M>`).
  Spec ids are `S-<n>` and `S-<n>-T-<m>`.
- **Cross-field validation.** `validate_forest` and
  `validate_task_role_alignment` are the only structural checks. There is
  no AC↔slice cross-reference check, no blocker-resolution check, no
  `Slice count` matches the slice total check, no unique-id-shape check
  beyond what pydantic / `validate_forest` happen to catch.

### Runtime primitives the plan phase will need to touch

(per #2594 — naming every primitive the plan phase downstream will depend on
so the Primitive-Existence and Trust-Boundary audits are cheap)

- `parse_plan(content: str) -> ParseResult` (`shared/egg_contracts/plan_parser.py:1262`) — entry point. **scope: trusted-CI-runner + in-sandbox-agent** (the plugin's `emit-contract` script runs in-sandbox; the orchestrator's `_populate_contract_from_plan` runs in trusted infra).
- `Slice`, `Task` models (`shared/egg_contracts/models.py`) — id-shape change requires pydantic-pattern updates.
- `Contract`, `Contract.model_validate` (`shared/egg_contracts/models.py`) — every consumer of the on-disk contract validates through this entry point.
- `validate_forest(slices)` (`shared/egg_contracts/plan_parser.py:1485`).
- `validate_task_role_alignment(slices, repo)` (`shared/egg_contracts/plan_parser.py:1700`).
- `_populate_contract_from_plan` (`orchestrator/routes/pipelines.py:~18473`) — **trusted-CI-runner**.
- `_build_pr_body` (`orchestrator/routes/pipelines.py:~9292`) — **trusted-CI-runner**.
- `slice_scheduler.py::DependencyGraph` (lines 13–36) — reads `Slice.dependencies`.
- `impasse_routing.py` (lines 85, 185, 437) — writes `Task.role`.
- `plugins/refine-plan/skills/refine-plan/bin/emit-contract` — **in-sandbox**.
- `plugins/refine-plan/skills/refine-plan/bin/validate-yaml-tasks` — **in-sandbox**.
- `plugins/refine-plan/skills/refine-plan/agents/task-planner.md` — planner prompt, in-sandbox.
- `plugins/refine-plan/skills/refine-plan/agents/reviewer-plan.md` — plan reviewer prompt, in-sandbox.
- `docs/templates/plan.md` — template, read by planner subagents.
- `.egg/schemas/yaml-tasks.schema.json`, `.egg/schemas/contract.schema.json` — schemas referenced by the plugin's validator and (transitively) by IDE / CI lints.
- `.github/scripts/checks/plan_yaml_check.py` — CI guard; **trusted-CI-runner**.
- `egg-contract` CLI (`shared/egg_contracts/cli.py`) — operator surface for the contract; reads / writes through `Contract.model_validate`.

## Constraints

- **Backward compatibility with in-flight pipelines.** As of HEAD on `main`
  there are pipelines with `.egg-state/contracts/*.json` already in the legacy
  shape and active branches whose plan documents use the `# yaml-tasks`
  appendix. Cutting the legacy parser invalidates them. (See `cq-2`.)
- **Spec is not in the repo.** The issue references
  `docs/design/planning-contract-framework.md` and
  `docs/design/planning-contract-framework-review.md` as the working copy of
  the spec, but neither file exists (no git history under `docs/design/`),
  and the upstream `Khan/actionable-plans` repo is private (verified via
  `gh repo view`). This blocks downstream phases that need the canonical
  grammar + JSON Schema to implement against. (See `cq-1` and feedback Q1.)
- **`ExpectedImpact` source unknown.** The issue says egg already produces
  this JSON via an `/impact-analysis` skill, but no such skill, type, or
  producer exists in the repo. (See `cq-5` and feedback Q1.)
- **Forest constraint and role-files alignment are load-bearing.** The
  implement-phase slice scheduler and the stacked-PR reconciler depend on
  `validate_forest` and on the gateway's per-role file restrictions
  (`shared/egg_restrictions/patterns.py`). New grammar must preserve these
  checks (or replace them with equivalent ones) so #2137 and #2527 don't
  regress.
- **Egg-specific extensions are load-bearing.** `serialized_chain_order`
  (#2137 forest serialization), `pr.context_title` /
  `pr.context_description` (#2548 context PRs), and `jira_key` /
  `jira_action` / `jira_action_status` (#1557 Jira-epic SDLC) all live on
  the contract and are referenced by shipped code. None of them have a
  natural home in the framework's grammar; the spec has no equivalent
  field-headers. (See `cq-4`.)
- **The refine-plan plugin is portable.** `bin/emit-contract` deliberately
  doesn't import `egg_contracts.models` (it re-implements the contract
  shape inline so it runs wherever the plugin is installed). Any
  contract-shape change must update both code paths in lock-step.
- **The `# yaml-tasks` parser has a thick history of bug fixes** (#1974,
  #1988, #2137, #2503, #2527, #2530, #2548, #2743, #2756) — many corner
  cases (block-scalar-with-inner-fence, bool-as-dep, anchored-task-id,
  block-exempt vs. block, repo-overrides, role-vs-files mismatches) that
  a new parser cannot lose without regressing those issues.
- **Reviewer prompts are tightly coupled to the rubric.** The plan
  reviewer's eight-key rubric and the implement-phase code-review
  criteria reference today's slice/task/role shape directly. New
  grammar requires prompt updates.
- **Trust boundary.** The planner and reviewer agents run **in-sandbox**;
  the orchestrator ingestion runs **trusted-CI-runner**; the gateway's
  pre-push hooks (which run `validate_forest` / `validate_task_role_alignment`)
  also run **trusted-CI-runner**. The new parser must be safe to invoke
  from both contexts and must not import sandbox-only modules.

## Options Considered

### Option A: Big-bang conformance (single slice, replace everything at once)

**Approach**: One slice that (a) lands the spec + `plan-contract-1.0.json` in
`docs/design/` and `.egg/schemas/`, (b) rewrites `plan_parser.py` to the
strict structured-markdown grammar with no YAML appendix and no markdown
fallback, (c) updates the contract model (ID shape, new typed lists for
AC / BL / OQ, new `Impact` block), (d) rewrites `docs/templates/plan.md`,
(e) rewrites the `task-planner.md` and `reviewer-plan.md` prompts, (f)
updates every downstream consumer (`_populate_contract_from_plan`,
`_build_pr_body`, `_pr_metadata_from_plan_draft`, signals.py, plugin's
`emit-contract` and `validate-yaml-tasks`, the plan-yaml CI check). Ships
as one PR.

**Pros**:
- No intermediate "half-conformant" state where the parser accepts a grammar
  no template emits, or where the contract has fields no producer writes.
- One review cycle covers the whole change; no cross-slice drift risk.
- Smallest amount of compat shim code.

**Cons**:
- Massive blast radius; one PR touches schemas, parser, models, prompts,
  template, orchestrator, plugin scripts, CI checks.
- Hard to review thoroughly; reviewer fatigue likely.
- Hard to roll back; if any one piece regresses the others come back with it.
- In-flight pipelines on legacy contracts break the moment the PR lands
  unless a migrator ships with it.

### Option B: Spec-first + staged consumer migration (multi-slice, framework lands first)

**Approach**: Decompose along the dependency seams of the contract:
1. Land the spec + freeze `plan-contract-1.0.json` in `.egg/schemas/`. No
   code changes that affect runtime.
2. Land the new contract model + new parser side-by-side with the old
   one (a `plan_parser_v2.py` or rewrite-in-place — see `cq-11`),
   exporting the new types but not yet wired to any consumer. Update
   `docs/templates/plan.md` and the planner prompt to emit the new
   grammar in parallel.
3. Migrate downstream consumers wave-by-wave (orchestrator ingestion,
   PR-body composer, plugin scripts, planner / reviewer prompts) so each
   wave is reviewable in isolation.
4. Optionally a final cleanup slice that removes the legacy parser, the
   `# yaml-tasks` schema, and the markdown-regex fallback once nothing
   reads from them.

**Pros**:
- Each slice is independently reviewable and revertable.
- Intermediate state is "the new parser exists, the old one is still
  authoritative", which is a safe rollback target.
- Matches egg's stacked-PR DAG pattern; the slice scheduler can run
  later waves in parallel where dependencies permit.
- Lets the cutover question (`cq-2`) be settled per-consumer rather
  than as one global flag.

**Cons**:
- More code in flight at once: the dual-parser state exists across
  multiple PRs, and a NACK on a late slice may leave the codebase
  half-migrated for the duration of the cycle.
- More cycles of BRC review (one per slice).
- The "single source of truth" benefit lands only after the final
  cleanup slice — the YAML appendix and the markdown fallback live
  alongside the new grammar for the duration of the migration.

### Option C: Compat-layer-forever (dual grammar permanently)

**Approach**: Land the new grammar as a first-class alternative; keep the
YAML appendix and markdown fallback indefinitely as accepted inputs.
The parser dispatches on which form the document uses.

**Pros**:
- Zero migration cost for in-flight pipelines.
- External consumers (other egg-style tools that may reuse the parser)
  aren't forced to update.
- Lowest risk to the implement / pr phases — they keep seeing the
  same contract shape they always have.

**Cons**:
- **Does not satisfy the issue.** The spec explicitly inverts the
  "two acceptable grammars" stance; conformance means a single grammar.
- Doubles maintenance: every parser change must work on two grammars.
- The `task-planner` agent's prompt has to teach both grammars; the
  reviewer's rubric has to check both.
- Defeats the cross-tool sharing argument — a downstream tool that wants
  to read egg plans still has to handle both shapes.

### Option D: Adopt the grammar, drop the framework-specific extras (minimal conformance)

**Approach**: Adopt the spec's document grammar (no YAML appendix, no
markdown fallback, structured `## Acceptance Criteria` / `## Blockers` /
`## Open Questions` / `## Impact & Risk` sections, `S-<n>` ids) but defer
the field-header / `**Bold label**` discipline and the cross-field
validation rules (every AC referenced, blocker resolution, slice-count
match) to a later issue. Conformance is "structural", not "behavioural".

**Pros**:
- Smaller scope; one slice is plausible.
- Gets egg's plan document into the shape other Khan tooling expects
  without committing to the full validation surface.

**Cons**:
- **Also doesn't really satisfy the issue.** The spec's whole point is
  the validation contract — the grammar without the cross-field checks
  is just a renaming exercise.
- Surfaces "looks conformant, isn't" as a failure mode; a third-party
  Conformance Bot that runs against egg plans would fail.
- Two cycles of disruption (renaming now, validation later) instead of
  one.

## Recommended Approach

**Option B — spec-first + staged consumer migration**, with two refinements
that need plan-phase input rather than refine-phase guesswork:

1. **The spec must land first as its own slice** (option B step 1). The
   downstream slices cannot be implemented without the canonical grammar
   and JSON Schema. This makes `cq-1` the most urgent operator decision.
2. **The cutover question (`cq-2`) is genuinely a plan-phase decomposition
   concern**, not a refine one. The right answer depends on the slice
   DAG the plan phase chooses: a "dual-parse window" works in option B
   step 2's intermediate state; a "hard cutover" works if the final
   cleanup slice is fast-followed by a migrator. Refine should surface
   the option but plan should pick.

Justification:

- **Conformance is structurally a parser + model + schema change with a
  large tail of consumer updates.** Option B's seam (model + parser first,
  consumers later) matches egg's existing stacked-PR DAG model exactly.
- **The spec is missing.** Option A and Option D can't be implemented
  blindly; both need the canonical grammar in-hand. Option B's first
  slice unblocks every later wave.
- **The intermediate state of Option B is safe.** "New parser exists,
  old one is still authoritative" is the same compat shape egg used
  for #2137 (slices vs. phases) and #1557 (epic mode) — both
  migrations the plan parser has already survived.
- **Option C is rejected** because it doesn't satisfy the issue's
  conformance criterion.
- **Option D is rejected** because partial conformance is worse than
  none — the validation rules ARE the conformance.

Critical open questions that block plan-phase decomposition:

- `cq-1` (spec landing) — every other slice depends on the spec being in
  the repo.
- `cq-2` (cutover strategy) — drives whether the final slice is a hard
  flip or a deprecation window.
- `cq-4` (egg-specific extensions) — drives whether the new contract has
  one schema or two.
- `cq-9` (ID format) — drives the size of the orchestrator-side change.

Lower-priority but still decision-blocking: `cq-3`, `cq-6`, `cq-7`, `cq-8`,
`cq-10`, `cq-11`, plus the five open-ended feedback questions on enum
sets (Q1), AC/task-acceptance coexistence (Q2), human-review-reason
semantics (Q3), blocker-vs-deferred-action overlap (Q4), and implement-phase
conformance checks (Q5).

## Open Questions

> Twelve multiple-choice decisions and five open-ended feedback questions
> have been registered via `mcp__sdlc__register_open_question` and
> `mcp__sdlc__request_feedback`. Markdown for each is below.

### cq-1 — Where does the canonical spec live?

The issue references `docs/design/planning-contract-framework.md` as the
in-repo working copy of the spec, but no such file exists (no git history
under `docs/design/`). Where should plan/refine teams source the canonical
grammar, JSON Schema (`plan-contract-1.0.json`), and Appendix C field
crosswalk?

- [ ] Land the spec + review notes into `docs/design/` as a prerequisite
      slice before any parser/template work (recommended — gives plan /
      implement agents an in-repo source of truth and pins the schema
      version egg conforms to)
- [ ] Pull the spec from `Khan/actionable-plans` on demand each cycle (no
      in-repo copy; depends on Khan repo access — currently private per `gh`)
- [ ] Embed the relevant grammar + JSON Schema verbatim into this issue's
      body / the analysis document so downstream phases have local context
- [ ] Other (explain in reply)

### cq-2 — How does the cutover handle legacy contracts?

How should the cutover handle in-flight pipelines and pre-existing contracts
(the legacy `slice-N` / `task-N-M` ID shape, the `# yaml-tasks` YAML appendix,
and existing `.egg-state/contracts/*.json` files)?

- [ ] Hard cutover: new pipelines only use the new grammar; in-flight
      pipelines finish on the legacy parser; old contracts remain in
      their legacy shape and aren't migrated
- [ ] Dual-parse transition window: parser accepts both grammars for one
      egg release, with a deprecation warning on the YAML appendix;
      existing contracts continue to work
- [ ] Big-bang migration: write a one-shot migrator that rewrites every
      `.egg-state/contracts/*.json` and in-flight pipeline state to the
      new shape on upgrade
- [ ] Defer the decision to plan phase: refine just flags the migration
      concern; plan decides between hard cutover vs. transition based on
      slice decomposition
- [ ] Other (explain in reply)

### cq-3 — Strict-parse posture

What is the conformance bar for the new grammar's strict-parse stance? The
spec says "a plan that does not parse is 'not executable', reported with a
structured error rather than silently degraded".

- [ ] Hard fail on any deviation: parser raises a structured error; no
      markdown-regex fallback; no "placeholder task" fill-in for empty
      slices; reviewer NACKs the planner
- [ ] Hard fail on schema deviations, warn on style/order: structural
      rules (missing required field-headers, unresolved refs, duplicate
      ids) are errors; field-order and casing drift surface as warnings
- [ ] Warn-and-emit-best-effort: keep today's posture of best-effort
      extraction with `warnings[]` so a malformed plan still produces a
      partial contract reviewers can fix incrementally
- [ ] Other (explain in reply)

### cq-4 — Egg-specific contract fields

How should egg-specific contract fields that have no counterpart in the
framework's JSON Schema be handled (e.g. `serialized_chain_order`,
`pr.context_title`, `pr.context_description`, `pr.context_branch`,
`pr.context_pr_number`, `jira_key`, `jira_action`, `jira_action_status`)?
These are load-bearing for #2137 (forest serialization), #2548 (context PRs),
and #1557 (Jira-epic SDLC).

- [ ] Keep them as egg-specific extensions outside the framework-conformant
      region: the framework section of the contract validates against
      `plan-contract-1.0.json`; egg-specific fields live in a sibling
      `egg_extensions:` block so the conformance bot can still validate
      the conformant part
- [ ] Subsume them into the framework grammar: write spec-conformant
      analogues (e.g. `serialized_chain_order` becomes a `Coordination`
      block on the slice; Jira-mode tasks use the spec's `Decision` /
      `External` blocker types) and drop the egg-specific names
- [ ] Propose upstream additions to the framework spec for fields that
      have legitimate cross-tool value (e.g. `serialized_chain_order` for
      forest-DAG planners); keep narrow egg-internal fields as extensions
- [ ] Drop the features that have no spec home: gate #1557/#2137/#2548
      work on either upstreaming or losing the feature — not a real
      option for #1557 since it's shipped, surfaced for completeness
- [ ] Other (explain in reply)

### cq-5 — Where does the `/impact-analysis` skill live?

The issue says egg "already produces `ExpectedImpact` JSON via the
`/impact-analysis` skill — conformance should align the two and reuse the
enum set, not reinvent it." A repo search finds no `impact-analysis` skill,
no `ExpectedImpact` type, no producer of impact JSON. Where does this
artifact actually come from today?

*(Free-form decision — see also feedback Q1 for the enum set.)*

### cq-6 — Where do AC, BL, OQ, R records live?

Where should AC, BL, OQ, and R records live relative to the slice/task scope?
The spec describes them as top-level `##` sections, but egg today also
surfaces (a) refine-phase Open Questions registered with
`egg-contract add-decision` / `add-feedback` and (b) plan-phase risk_analyst
output as a sibling JSON.

- [ ] The plan document's `## Acceptance Criteria` / `## Blockers` /
      `## Open Questions` are the single source of truth for the plan
      phase; refine-phase HITL decisions feed into them but are not the
      same records, and `risk_analyst-output.json` is rolled up into
      `## Impact & Risk` `R-<n>` records by `task_planner`
- [ ] Carry refine-phase HITL decisions forward as OQ records on the
      plan document (with their resolution status), so the operator sees
      pending refine decisions on the plan-HITL gate too
- [ ] Keep records strictly in the plan document and drop the sibling
      JSON artifacts (`risk_analyst-output.json`, `egg-contract` decisions
      in refine) once the plan is produced
- [ ] Mirror records bidirectionally between the plan markdown and the
      contract JSON; the JSON is the authority for downstream consumers
- [ ] Other (explain in reply)

### cq-7 — `### Validation` vs `pr.test_plan` / `pr.manual_steps`

Should the new per-slice `### Validation` block (`Automated checks` /
`Manual verification` / `Pre-merge steps` / `Post-merge steps`) replace or
extend the existing per-PR `pr.test_plan` + `pr.manual_steps` fields and
the top-level acceptance-criteria rollup, given there's a 1:1 slice↔PR
mapping?

- [ ] Replace: drop `pr.test_plan` and `pr.manual_steps` entirely; the PR
      body is composed from the slice's `### Validation` subsection.
      `pr.title` and `pr.description` remain (they're per-PR concerns)
- [ ] Extend: keep `pr.test_plan` / `pr.manual_steps` as a slice-level
      rollup the PR-body composer reads; `### Validation` is structured
      detail under it. Less churn for the PR composer but two places to
      write tests
- [ ] Replace `pr.test_plan` / `pr.manual_steps`, but also auto-render
      the `### Validation` block into the PR body verbatim so reviewers
      see structured checks (no separate composer logic needed)
- [ ] Other (explain in reply)

### cq-8 — Schema file layout

Should the new schema replace today's `.egg/schemas/yaml-tasks.schema.json`
and `.egg/schemas/contract.schema.json`, vend the framework's
`plan-contract-1.0.json` alongside them, or roll up into a single egg-owned
schema that imports the framework schema as a `$ref`?

- [ ] Vendor `plan-contract-1.0.json` into `.egg/schemas/` as a frozen
      copy pinned to a schema version; delete `yaml-tasks.schema.json`;
      update `contract.schema.json` to `$ref` the new plan-contract
      schema for its plan-shaped subtree
- [ ] Vendor the schema and also keep a separate
      `egg-contract-extensions.schema.json` for egg-specific fields
      (cq-4 option A); `contract.schema.json` references both via `allOf`
- [ ] Replace `yaml-tasks.schema.json` and `contract.schema.json` with a
      single `egg-plan-contract.schema.json` that fully owns the shape
      (the framework spec is the design source, but egg owns the schema
      file so we can evolve it independently)
- [ ] Other (explain in reply)

### cq-9 — Contract ID format

The id format shifts from `slice-N` / `TASK-N-M` (egg-canonical, lowercased
on the contract) to `S-<n>` / `S-<n>-T-<m>`. Should the contract `Slice.id` /
`Task.id` strings change, or only the in-document display form?

- [ ] Update `Slice.id` to `S-<n>` and `Task.id` to `S-<n>-T-<m>` so the
      contract and the document use one id everywhere; pydantic
      validators, audit-log strings, message bodies, and the orchestrator
      state-machine all flip together
- [ ] Only change the document's display form; the contract continues to
      use `slice-N` / `task-N-M` internally; the parser maps `S-<n>` ↔
      `slice-N` on read/write — a thin shim that minimises orchestrator
      churn but reintroduces a parser-only translation that's exactly
      what the spec wants to eliminate
- [ ] Update both, but lowercase the contract ids to `s-<n>` / `s-<n>-t-<m>`
      so the existing pydantic patterns (`^task-…` etc.) just need a
      single-letter prefix flip
- [ ] Other (explain in reply)

### cq-10 — Cross-field validation timing

Cross-field validation timing: where do the new spec validators run — at
plan ingestion only, or also at PR / implement-time so a slice that drifts
from the plan during implementation is caught before merge?

- [ ] Plan-time only: validators run when the planner proposes; the
      existing reviewer NACK loop catches misses. PR-time conformance is
      the framework's Plan Conformance Bot job (out of scope per the issue)
- [ ] Plan-time + PR-time on the egg side: validators run again on every
      BRC propose during implement and pr phases so reviewers see drift
      early; egg's NACK loop catches drift even without the framework's
      conformance bot
- [ ] Plan-time + implement-time pre-flight only: validators run on plan
      ingestion AND when the orchestrator spawns each slice's agent team
      (so a stale plan that has lost its AC reference doesn't even start
      work)
- [ ] Other (explain in reply)

### cq-11 — Parser refactor strategy

How aggressively should `plan_parser.py` change shape? Today's `ParsedPhase`,
`ParsedTask`, `ParseResult`, `parse_plan_file`, `validate_forest`,
`validate_task_role_alignment` are exported as part of the public API in
`shared/egg_contracts/__init__.py` and consumed by the orchestrator and the
`refine-plan` plugin's `validate-yaml-tasks` / `emit-contract` scripts.

- [ ] Rewrite in place: keep the same module + function names; rewrite
      the parser internals; ParseResult grows new fields
      (acceptance_criteria, blockers, open_questions, impact); ParsedPhase
      keeps its name but its semantics shift to spec-conformant slices
- [ ] New module + dataprep migration: introduce
      `shared/egg_contracts/plan_parser_v2.py` with new types; keep
      `plan_parser.py` as a deprecated shim that delegates; orchestrator
      and plugin callers cut over piecemeal
- [ ] Aggressive rename + breakage: rename the module to
      `plan_contract.py`, drop the `ParsedPhase` alias, and force every
      caller to update. Cleanest but biggest blast radius in one slice
- [ ] Other (explain in reply)

### cq-12 — Work decomposition for plan phase

How should this work be decomposed into slices for the plan phase to
consider? (Refine is only flagging shape options; plan owns the final DAG.)
Conformance touches: the spec landing, the schema, the parser, the contract
model, the planner prompt, the plan-template, the plan-time validators, the
orchestrator ingestion, the slice scheduler, the PR-body composer, and the
`refine-plan` plugin's emit-contract/validate-yaml-tasks scripts.

- [ ] Two slices in parallel: [land the spec + freeze the JSON Schema] ||
      [stub the new contract model with no consumers yet] (2 PRs),
      followed by serialised waves the plan phase shapes once cq-1…cq-11
      are resolved
- [ ] Three slices with dependency: [land the spec + schema] -> [new
      parser + contract model + plan template, all wired together] ->
      [migrate downstream consumers: orchestrator ingestion, PR-body
      composer, plugin scripts, planner prompt] (3 PRs)
- [ ] Four slices: [spec + schema] -> [new parser + model] || [new
      template + planner prompt] -> [downstream consumers] (4 PRs) —
      the parser and the template can't merge in parallel because the
      planner prompt has to learn the new grammar at the same time
- [ ] Single slice: ship everything together to avoid a half-conformant
      transition window (1 PR) — large blast radius but no intermediate
      state where the parser accepts a grammar no template emits
- [ ] Other (explain in reply)

### feedback-1 — Open-ended decisions

Five open-ended questions are registered for human reply (see
`feedback-1` in the contract):

- **Q1**: What is the authoritative enum set for `## Impact & Risk` —
  risk levels, maturity values, product-area taxonomy?
- **Q2**: AC↔task-acceptance coexistence — do per-task `acceptance_criteria`
  strings stay, get rolled up into `AC-<n>`, or both with a forced
  `Satisfies: AC-1` reference?
- **Q3**: What counts as a "human-review reason" in the egg context — HITL
  decisions, `OQ-<n>` records, conditional-ACK `pre_merge_condition`, or a
  new field?
- **Q4**: Do egg's pre-merge obligations (#1998, `deferred_actions`) fit
  any of the spec's blocker categories (`dependency` / `decision` /
  `external` / `infra` / `data`), or are they orthogonal?
- **Q5**: Should the implement-phase reviewer rubric verify every changed
  file is covered by some task's `Files` field and every AC referenced by
  the slice is touched by the diff? (Beyond issue scope, but the natural
  next step.)

---

*Authored-by: egg*
