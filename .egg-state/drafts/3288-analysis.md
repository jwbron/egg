# Refine analysis — issue #3288

**Documenter agent + docs: snapshot of current state, not a ledger of changes.**

Live issue body fetched and grounded against the tree on 2026-06-26. This is an
*analysis*, not an implementation plan — slicing/sequencing is the planner's call
(the issue explicitly delegates the DAG).

## Problem (restated)

egg's documentation has drifted into a **ledger of changes** instead of a
**snapshot of current state**. Two coupled causes:

1. The **documenter agent's task instructions** frame its job as documenting
   *the change* ("update documentation for the changes made by the CODER agent",
   "changes from plan phase {id}"), which nudges toward change-log prose and the
   leakage of SDLC artifacts (slice numbers, TASK-N ids, phase/HITL iteration
   numbers) into durable docs/docstrings/comments.
2. **By accretion**, the existing corpus now carries that ledger framing: ~260
   files under `docs/`, `gateway/`, `orchestrator/`, `shared/` contain
   `slice-N` / `TASK-N` references (verified by grep, 2026-06-26).

Goal: docs describe the current code as if the pipeline machinery never existed;
historical context appears only where *tangibly valuable to a reader of the
current system* (rationale over chronology). Not "delete all issue references" —
issue links that justify *why the system is shaped this way* stay.

## Grounded facts (verified 2026-06-26)

### Work stream 1 — documenter agent (structural change, low risk)

The documenter's behavior is shaped in two source locations, both confirmed:

- **`orchestrator/routes/pipelines.py`** — the "## Your Task" assembly:
  - `~14781` (`role_value == "documenter"` branch): the implement-phase task
    block opens with *"Update documentation for the changes made by the CODER
    agent:"* and a "Focus on: accurate descriptions of new features or changes …
    breaking changes" list. This is the primary ledger-nudging text.
  - `~6761`: the per-phase task summary injects *"Focus your documentation on
    changes from plan phase `{phase_obj.id}`."*
  - `~14157`: the documenter's plan-phase orientation/no-op branch.
  - **No-op / no-doc-impact propose path** is present and MUST be preserved:
    the `### When the slice warrants no doc updates (#3027)` block (~14800) and
    the plan-phase variant (~14157) both instruct a generic
    `consensus propose --no-changes-needed`. The change must keep this intact.
  - Caveat (minor, planner/coder note): the no-op example reason string itself
    reads *"e.g. slice-3 is a pure decomposition…"* — that is an ephemeral CLI
    arg, not a durable doc, so strictly out of the issue's target; but rewording
    it to a non-slice example would model the desired behavior. Low priority.
- **`shared/egg_contracts/agent_roles.py:306`** (`DOCUMENTER_ROLE`):
  `description="Updates documentation for the changes"`, responsibilities are
  change-oriented ("Update relevant documentation", "Ensure README files are
  current"). `file_access.allowed_write = [docs/, **/README.md, **/*.md,
  .egg-state/agent-outputs/]`; `blocked_write` blocks code/tests/`.github/`.
  **These gateway file boundaries are a hard constraint to preserve** (issue +
  CLAUDE directive) — the prompt/role wording changes, the write boundaries do
  not.

The required prompt revision (per issue): instruct the documenter to (a) write
**current-state** docs as if slice/pipeline machinery did not exist; (b) **never**
reference SDLC artifacts (slice/TASK/phase/HITL iteration numbers) in any
doc/docstring/comment; (c) include historical context only when tangibly valuable,
preferring rationale over chronology; (d) when editing an existing doc, **fold new
state into the snapshot and remove** stale ledger entries rather than append;
(e) keep the no-op propose path.

### Work stream 2 — corpus cleanup (large, planner-sliced)

Representative high-value targets (all confirmed to exist):
- Architecture pages reading as change-logs: `docs/architecture/brc-memory.md`,
  `orchestrator.md`, `slice-dag.md`, `gateway-auto-filter.md` (has an explicit
  "what was removed" / "preserved for context" section for removed #1882 code),
  `coordination-state.md` ("final shape of the #3077 invariant… landed in six
  slices, all shipped"; Threat/Mitigation/Status tables with "Retired surface").
- Docstring/inline ledgers: `gateway/artifact_api.py:1`, `gateway/jira_client.py`,
  `orchestrator/kubernetes_spawner.py` (30+ `# #3064 slice-N:` markers),
  `shared/egg_anchor/protected_root.py:1`, `shared/egg_agent/context_discipline.py:1`,
  `shared/egg_agent/__main__.py`.
- CLAUDE.md decomposition ledgers: `gateway/CLAUDE.md`, `orchestrator/CLAUDE.md`
  (submodule seam tables keyed by *which slice landed each decomposition*).

**Scale:** ~260 files contain `slice-N` / `TASK-N` / `slice N` references across
the four trees. No automated guardrail exists today (checked `scripts/`,
`.pre-commit-config.yaml`, `Makefile`).

## Recommended approach (for the planner)

- **Sequence work stream 1 first (or in its own early slice).** It is small,
  self-contained (two files: `pipelines.py` prompt blocks + `agent_roles.py`
  description/responsibilities), and establishes the snapshot principle as the
  go-forward standard before/while the corpus is cleaned. Risk: low — text-only
  edits to prompt assembly; the existing prompt-assembly tests should be checked
  for snapshot-string assertions that need updating.
- **Slice work stream 2 by doc-area / package** (e.g. `docs/architecture/*` as
  one or more slices; `gateway/` docstrings; `orchestrator/` docstrings;
  `shared/` docstrings; the CLAUDE.md seam tables) so slices are independently
  reviewable and don't collide on the same files. Pages where the ledger framing
  is *load-bearing* (gateway-auto-filter "what was removed", coordination-state
  "final shape of #3077") need a **total refactor into current-state prose**, not
  a line-edit; the planner should call those out explicitly.
- **Distinguish chronology from rationale on every edit:** strip
  "slice-N added… / used to… now…" narration; rewrite to present-tense current
  behavior; *keep* issue links that explain why the current design exists
  (reframed as rationale, isolated into a clearly-marked note where substantial).

## Open questions (registered as HITL on the contract)

- **cq-1 — Cleanup completeness target.** ~260 files carry ledger refs. Options:
  (opt-1, **recommended**) enumerated targets + bounded sweep of highest-density
  files, defer the long tail to follow-ups; (opt-2) exhaustive repo-wide;
  (opt-3) ship work stream 1 only now, split the whole corpus cleanup to a
  follow-up pipeline. Recommend opt-1: it satisfies the issue's named evidence,
  keeps the pipeline bounded, and avoids 260-file merge churn.
- **cq-2 — Durability guardrail.** Should this pipeline add an automated
  lint/CI check that flags `slice-N`/`TASK-N` in committed docs? Options:
  (opt-1, **recommended**) no — out of scope, note as follow-up; (opt-2) yes,
  add a lightweight advisory guard. Recommend opt-1: the issue scopes a prompt
  change + corpus pass, not new CI surface; a guard has its own false-positive
  tuning cost and is cleanly separable.

## Constraints / out of scope (binding)

- **Not** "delete all issue references" — issue links justifying current design
  are in scope to keep (reframed as rationale).
- **Preserve** the documenter's gateway file boundaries (`docs/`, `**/*.md`,
  `.egg-state/agent-outputs/`) and the **BRC no-op propose path** — both
  unchanged by this work.
- Docs are a **snapshot of current state**, never a ledger of how the work was
  sequenced — including this pipeline's own slices (the cleanup's commits/PRs
  carry process context in their messages, but the docs they touch must not).
