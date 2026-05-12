---
name: refine-plan
description: "BRC-inspired iterated parallel review of an issue's refine + plan phases, locally and portably. Produces analysis.md, plan.md, and an egg-compatible Contract via role-typed subagents with evidence-backed verdicts."
disable-model-invocation: true
argument-hint: "[JIRA-1234 | issue# | description] [--repo owner/name]"
allowed-tools: Agent Read Write Edit AskUserQuestion Bash(gh issue view:*) Bash(gh issue list:*) Bash(git remote:*) Bash(git -C * remote:*) Bash(mkdir:*) Bash(ls:*) Bash(test:*) Bash(find:*) Bash(python3 *bin/validate-yaml-tasks:*) Bash(python3 *bin/emit-contract:*) Bash(cat:*) Bash(cp:*)
---

# Refine + Plan (BRC-inspired local approximation of egg's refine/plan phases)

A local analogue of [egg's refine and plan phases](https://github.com/jwbron/egg/blob/main/skills/sdlc/SKILL.md), using Claude Code subagents and a filesystem verdict journal. **Not** real BRC — see [What this is vs. what egg's BRC is](#what-this-is-vs-what-eggs-brc-is) for the honest framing. Portable; no orchestrator, no Redis, no experimental flags.

**What this gets you (egg's BRC value, not its protocol):**
- Refine team: `refiner` + `reviewer_refine` (+ `reviewer_agent_design` for the egg repo only)
- Plan team: `architect` → (`task_planner` ∥ `risk_analyst`) → `reviewer_plan`
- Evidence-backed verdicts: every ACK and NACK requires non-empty `artifact_references`
- Bounded revision loop: max **3 cycles per phase** (matches `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES = 3`; cycle 3's NACK escalates to HITL — see [Cycle bound](#cycle-bound))
- Artifact layout under `.refine-plan-state/<id>/` mirrors `.egg-state/` subdirectories
- Per-cycle verdict journal in `verdicts/cycle-<N>/`
- Contract output matches `shared/egg_contracts/models.py::Contract` schema (loads cleanly through `Contract.model_validate`)
- YAML appendix validated against `.egg/schemas/yaml-tasks.schema.json` shape (only `pr.title` is required; `pr.test_plan` is recommended-only and surfaces as a warning, not a failure — same split as `shared/egg_contracts/plan_parser.py::extract_pr_metadata_from_yaml`)

## What this is vs. what egg's BRC is

Egg's BRC is a real protocol: producer broadcasts on a Redis bus, reviewers concurrently judge, an open-NACK barrier blocks producer convergence while NACKs are unresolved, version bumps un-confirm stale ACKs, agents see each other's in-flight messages. The Python orchestrator drives the state machine deterministically.

This skill captures the **value** of that protocol — independent multi-reviewer perspectives, evidence discipline, bounded revision — without implementing the **mechanics**. Specifically:

- **Iterated, not concurrent.** Each cycle is producer-then-parallel-reviewers-then-aggregate, serialized by the orchestrating skill. Reviewers within a cycle run in parallel (via batched `Agent` tool calls), but they don't see each other's verdicts mid-flight.
- **No real bus.** `verdicts/cycle-<N>/` is a passive journal of cycle outcomes, not a live message bus. Filenames borrow BRC vocabulary (`PROPOSE`, `ACK`, `NACK`, `CONFIRMED`) for legibility; the mechanism is just "parent writes after observing each subagent's output."
- **No version tracking.** Every cycle reviews the latest draft fresh; no stale-ACK un-confirmation, no open-NACK barrier.
- **No mid-cycle producer revision.** Producer is spawn-and-wait; it cannot react to a reviewer's mid-flight NACK.

Real BRC over genuinely concurrent agent teams would need either Claude Code's experimental Agent Teams feature (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) or the `SendMessage` tool for resuming long-lived background agents. The latter is platform-broken as of Claude Code 2.1.79 (see [anthropics/claude-code#36196](https://github.com/anthropics/claude-code/issues/36196) — `SendMessage` is referenced in the Agent tool output but isn't actually exposed to the model). Upgrade-path work is tracked in [egg#2612](https://github.com/jwbron/egg/issues/2612).

## Compatibility with egg's full pipeline

The goal is that artifacts from this skill are interchangeable with the same-named artifacts egg's orchestrator writes to `.egg-state/`. Each artifact's compat status:

| Artifact | Egg writes? | Schema source | Compat status |
|---|---|---|---|
| `drafts/<id>-analysis.md` | yes | `docs/templates/analysis.md` | ✅ same template |
| `drafts/<id>-plan.md` | yes | `docs/templates/plan.md` | ✅ same template + `# yaml-tasks` appendix |
| `contracts/<id>.json` | yes | `shared/egg_contracts/models.py::Contract` (schemaVersion 1.1) | ✅ `emit-contract` output loads cleanly through `Contract.model_validate` and round-trips identical (see [Emit contract](#emit-contract)) |
| `brc-history/<id>-<phase>.{md,json}` | yes | JSON is `[Message.to_dict(), …]` per `orchestrator/message_store.py::Message` | ⚠️ skill produces a deliberation log of similar intent but **shape is skill-specific**, not byte-compatible. The verdict files under `verdicts/cycle-<N>/` are the authoritative per-cycle record. |
| `agent-outputs/<id>-<role>-output.json` | yes | None — egg writes them but downstream agents read them as advisory context only, not validated | 🟰 shape is consumer-chosen; this skill's shapes (defined in `agents/<role>.md`) are skill-specific extensions |
| `reviews/<id>-<phase>-<reviewer>-review.json` | **no** | `orchestrator/models.py::ReviewVerdict` exists but is in-memory only in egg; never persisted to disk | 🆕 skill-internal scaffolding; egg's pipeline has no equivalent disk artifact |
| `verdicts/cycle-<N>/<verdict>-<role>.json` | **no** | n/a — egg uses a Redis Streams message bus | 🆕 skill-internal verdict journal (BRC-inspired, not a real bus) |

**Wire-compatible path**: if you run `/refine-plan` and want to feed the result into `/sdlc`'s implement phase, copy `contracts/<id>.json` into the target repo's `.egg-state/contracts/<id>.json`. Egg will load it via the same `Contract.model_validate` call.

## Role definitions

Each role's identity, rubric, and output schema lives in its own file under `<skill-root>/agents/`:

| File | Role | Phase | Kind |
|------|------|-------|------|
| `agents/refiner.md` | refiner | refine | producer |
| `agents/reviewer-refine.md` | reviewer_refine | refine | reviewer |
| `agents/reviewer-agent-design.md` | reviewer_agent_design | refine | reviewer (egg repo only) |
| `agents/architect.md` | architect | plan | producer (runs first, solo) |
| `agents/task-planner.md` | task_planner | plan | producer (parallel) |
| `agents/risk-analyst.md` | risk_analyst | plan | producer (parallel) |
| `agents/reviewer-plan.md` | reviewer_plan | plan | reviewer |

For each subagent invocation, the orchestrator (you, executing this skill) **reads the role file**, prepends it to a task-context block, and passes the result as the `prompt` to an `Agent` call with `subagent_type: "general-purpose"`. The role file is the stable rubric; the task-context block is what changes between invocations.

### Resolving `<skill-root>`

Claude Code does not expose the SKILL.md install path to the running session, and the Bash tool runs each invocation in a fresh shell — shell variables do **not** persist across `Bash` calls. Resolve `<skill-root>` once at start by running the discovery snippet below; capture the printed path as a literal string in the model's context and substitute it into the argv of every subsequent `Bash`, `Read`, or other tool call that needs it.

```bash
SKILL_ROOT=""
for candidate in \
  "$HOME/.claude/plugins"/*/refine-plan/skills/refine-plan \
  "$HOME/.claude/plugins/cache"/*/refine-plan/skills/refine-plan \
  "$HOME/.claude/plugins/cache"/*/plugins/refine-plan/skills/refine-plan \
  "$PWD/plugins/refine-plan/skills/refine-plan" \
  "$PWD/skills/refine-plan"; do
  if [ -f "$candidate/SKILL.md" ]; then
    SKILL_ROOT="$candidate"
    break
  fi
done
if [ -z "$SKILL_ROOT" ]; then
  SKILL_ROOT="$(find "$HOME/.claude/plugins" -maxdepth 6 -type f -name SKILL.md -path '*refine-plan*' 2>/dev/null | head -1)"
  SKILL_ROOT="${SKILL_ROOT%/SKILL.md}"
fi
test -n "$SKILL_ROOT" || { echo "could not resolve skill-root" >&2; exit 1; }
echo "$SKILL_ROOT"
```

The candidate list covers (a) the plugin install paths Claude Code uses (with and without the `cache/` and `plugins/` segments, since the marketplace-id segment is installer-internal), (b) a local-checkout install (running the skill directly out of an `egg/` clone), and (c) the legacy symlink install under the user's `~/.claude/skills/`. Each subsequent shellout must inline the resolved value rather than relying on `$SKILL_ROOT` to still be set — e.g. if discovery printed `/home/me/.claude/plugins/egg-tools/refine-plan/skills/refine-plan`, the next `Bash` calls become:

```bash
python3 /home/me/.claude/plugins/egg-tools/refine-plan/skills/refine-plan/bin/validate-yaml-tasks <plan_path>
python3 /home/me/.claude/plugins/egg-tools/refine-plan/skills/refine-plan/bin/emit-contract <plan_path> <contract_path> <pipeline_id>
```

Read calls for role files (`Read` tool) likewise take the inlined absolute `<resolved-skill-root>/agents/<role>.md` as the file path. If a phase needs several shellouts that legitimately can share a shell, collapse discovery + use into a single `Bash` invocation so `$SKILL_ROOT` stays in scope for that one call.

---

## Argument Parsing

| Input | Meaning |
|---|---|
| `/refine-plan 1059` | GitHub issue number |
| `/refine-plan #1059` | GitHub issue number |
| `/refine-plan KORE-1234` | JIRA ticket (`^[A-Z][A-Z0-9]+-\d+$`) |
| `/refine-plan Add retry logic` | Free-text task description |
| `/refine-plan --repo owner/repo 1059` | Repo override |
| `/refine-plan` (no args) | One `AskUserQuestion` |

**Auto-detect repo**: `git -C "$EGG_REPO_PATH" remote get-url origin`, fall back to cwd. Only ask if both fail and `--repo` not given.

**Identifier**:
- Issue → `issue-1059`
- JIRA → lowercase the key (`kore-1234`)
- Free text → first ~4 significant words kebab-cased

**Output root**: `.refine-plan-state/<id>/`. Create the layout before any subagent runs:

```
.refine-plan-state/<id>/
  drafts/                # analysis.md, plan.md
  agent-outputs/         # *-output.json handoff JSONs
  reviews/               # canonical per-reviewer verdict copies
  verdicts/              # per-cycle verdict journal (BRC-inspired; not a live bus)
    cycle-1/
  brc-history/           # derived human-readable summaries
  contracts/             # parsed Task records from plan.md yaml-tasks
```

**Issue context**: if issue number given, fetch once: `gh issue view <N> --repo <owner/repo> --json title,body,comments,labels,assignees`. Reuse the JSON.

---

## Reusable scaffolding

### Spawning a role

For every role invocation:

1. **Read** `<skill-root>/agents/<role>.md` (the role file). Strip the YAML frontmatter; keep the markdown body as `role_body`.
2. **Compose** the prompt:

   ```
   <role_body>

   ---

   # Task context

   Cycle: <n>
   Repo: <owner/name>
   Identifier: <id>

   <role-specific paths and inputs — see each Phase section below>

   <if cycle > 1>
   ## Prior NACKs to address
   <bulleted list of prior cycles' NACK feedback + artifact_references>
   </if>
   ```

3. **Call** `Agent` with `subagent_type: "general-purpose"`, the composed prompt, and a `description` like `"refiner cycle 1"` or `"reviewer-plan cycle 2"`.
4. **Parallel fan-out**: when multiple roles can run in parallel (both refine reviewers; both plan parallel producers), send **one message with multiple `Agent` tool calls** so they execute concurrently.

### The verdict journal

For each cycle, the orchestrator writes/reads files at `verdicts/cycle-<N>/`:

| Filename pattern | Meaning | Written by |
|---|---|---|
| `PROPOSE-<producer>.json` | Producer has emitted its artifact for this cycle | Orchestrator (after producer subagent completes) |
| `ACK-<reviewer>.json` | Reviewer accepts the current proposal | Reviewer subagent (final action) AND orchestrator (validates the reviewer's response, copies to `reviews/`) |
| `NACK-<reviewer>.json` | Reviewer rejects | Same as ACK |
| `CONFIRMED-<phase>.json` | All critical reviewers ACKed in this cycle | Orchestrator |

The journal is the per-cycle deliberation record. Filename verbs (`PROPOSE`, `ACK`, `NACK`, `CONFIRMED`) borrow BRC vocabulary for legibility — but this is **not** a live message bus; nothing reads or reacts to these files mid-cycle. The orchestrating skill writes them after observing each subagent's output. `brc-history/<id>-<phase>.md` is generated from the journal contents at the end of each cycle for human readability.

### Verdict validation

Every reviewer must return a single JSON object as its final response. The orchestrator:

1. Parses the JSON. If malformed → re-prompt that single reviewer once with "Your previous response was invalid JSON. Re-emit the verdict JSON only, no surrounding prose."
2. Checks `artifact_references` is a non-empty array of strings. If empty → re-prompt that reviewer once with "Your `artifact_references` was empty. Include at least one specific file:line citation you verified before re-emitting."
3. Writes the validated verdict to `verdicts/cycle-<N>/<verdict>-<reviewer>.json` AND to `reviews/<id>-<phase>-<reviewer>-review.json`.

### Cycle bound

A "cycle" is one **producer-then-reviewers** round: producer emits, reviewers verdict. The first cycle is cycle 1 (initial draft); each subsequent cycle is a producer revision driven by NACKs from the prior cycle.

The local cap is **3 cycles per phase** total — matching `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES = 3` in `orchestrator/env_config.py:271` and the `local_cycles >= self._local_max_cycles` escalation check in `orchestrator/slice_scheduler.py:323`. Concretely: cycles 1, 2, and 3 are auto-driven; a NACK on cycle 3 halts the auto-loop and triggers HITL escalation rather than spawning a cycle 4.

On cycle-3 NACK, ask via `AskUserQuestion`:

```
Header:  "Cycle limit"
Question: "Cycle 3 still has unresolved NACKs. What now?"
Options:
  - "Force-accept current draft"        → record override, advance
  - "One more revision with my guidance" → take free-text feedback, run cycle 4 (operator-extended)
  - "Abort phase"                       → exit, leave artifacts on disk
```

Operator-extended cycles past 3 are explicitly opt-in and recorded in the HITL decision JSON (see [Force-accept is recorded](#operating-notes)) — the auto-loop never crosses the cap on its own.

### BRC history append

After each cycle, append to `brc-history/<id>-<phase>.md`:

```markdown
## Cycle <n> — <ISO-8601 UTC>

### Producer: <role>
- Output: <relative path>
- Self-attestation: <2-3 bullets from producer's report-back>

### Reviewer verdicts
- **<reviewer-role>**: <ACK | NACK> — <summary>
  - Refs: <comma-separated artifact_references>
  - <if NACK> Feedback: <one-line>

### Resolution
<advance | revise | hitl-escalate | hitl-force-accept>
```

Also append a structured JSON record to `brc-history/<id>-<phase>.json`.

---

# Phase 1 — Refine

**Team**: `refiner` + `reviewer_refine` + (`reviewer_agent_design` if `origin == jwbron/egg`)

**Goal**: produce `drafts/<id>-analysis.md` matching the analysis template, with both (or all three) reviewers ACKing on evidence.

## Cycle loop (max 3 cycles — see [Cycle bound](#cycle-bound))

For cycle N = 1..3:

1. **Spawn refiner** (single Agent call). Task context:
   - `task_brief`: <issue body / JIRA summary / free-text>
   - `analysis_path`: `<abs>/.refine-plan-state/<id>/drafts/<id>-analysis.md`
   - `handoff_path`: `<abs>/.refine-plan-state/<id>/agent-outputs/<id>-refiner-output.json`
   - `repo`: `<owner/name>`
   - if N > 1: `prior_nacks` from previous cycle's verdict files

2. **Wait for completion**, read both output files. Write `verdicts/cycle-<N>/PROPOSE-refiner.json`:
   ```json
   {
     "cycle": <N>, "producer": "refiner",
     "draft_path": "<rel>",
     "attestation_summary": "<from refiner's report-back>",
     "timestamp": "<ISO-8601>"
   }
   ```

3. **Spawn refine reviewers in parallel** (one message, multiple Agent calls):
   - `reviewer-refine` always
   - `reviewer-agent-design` only if repo == `jwbron/egg`

   Task context for each reviewer:
   - `analysis_path`: same as above
   - `verdict_path`: `verdicts/cycle-<N>/<verdict>-<reviewer-role>.json` (reviewer writes here as its final action)
   - if N > 1: `prior_nacks` from this reviewer's previous-cycle NACK (if any)

4. **Wait for both/all reviewers**, validate each verdict per [Verdict validation](#verdict-validation).

5. **Aggregate**:
   - All ACK → write `verdicts/cycle-<N>/CONFIRMED-refine.json`, append BRC history with `resolution: advance`, exit loop.
   - Any NACK and N < 3 → append BRC history with `resolution: revise`, continue loop.
   - Any NACK and N == 3 → trigger [cycle-limit AskUserQuestion](#cycle-bound). Record outcome.

## HITL gate

After convergence, summarize for the user (Problem Statement, Recommended Approach, top 3 Open Questions). Ask:

```
Header:   "Refine gate"
Question: "The refine analysis is ready. Approve?"
Options:
  - "Approve and continue to plan"          → Phase 2
  - "Request changes"                       → collect free-text, return to cycle N+1 with it as forced revision
  - "Change approach (full reset)"          → discard draft, return to cycle 1 with cleared state + user's redirection
  - "Stop here"                             → exit
```

Record outcome in `reviews/<id>-refine-hitl-decision.json`.

---

# Phase 2 — Plan

**Team**: `architect` → (`task_planner` ∥ `risk_analyst`) → `reviewer_plan`

**Goal**: produce `drafts/<id>-plan.md` (with a valid `# yaml-tasks` appendix), `agent-outputs/<id>-{architect,task_planner,risk_analyst}-output.json`, and `contracts/<id>.json`.

## Step 2.0 — Architect (solo, before the cycle loop)

Single Agent call. Task context:
- `analysis_path`: `<abs>/.refine-plan-state/<id>/drafts/<id>-analysis.md`
- `architect_output_path`: `<abs>/.refine-plan-state/<id>/agent-outputs/<id>-architect-output.json`
- `repo`: `<owner/name>`

Wait for completion, read `architect-output.json`. The architect runs **once per phase**, not once per cycle — only re-run if a reviewer NACK specifically targets `key_design_decisions` or `ordering_constraints`.

## Cycle loop (max 3 cycles — see [Cycle bound](#cycle-bound))

For cycle N = 1..3:

1. **Spawn task_planner + risk_analyst in parallel** (one message, two Agent calls).

   Common context for both:
   - `analysis_path`, `architect_output_path` from Step 2.0

   task_planner-specific:
   - `plan_path`: `<abs>/.../drafts/<id>-plan.md`
   - `task_planner_output_path`: `.../agent-outputs/<id>-task_planner-output.json`
   - `risk_analyst_output_path`: `.../agent-outputs/<id>-risk_analyst-output.json` (so it can read once available)

   risk_analyst-specific:
   - `risk_analyst_output_path`: same as above

   if N > 1: each producer gets its own `prior_nacks` filtered to NACKs about its own output.

2. **Wait for both**. Write `verdicts/cycle-<N>/PROPOSE-task_planner.json` and `PROPOSE-risk_analyst.json`.

3. **Validate the YAML appendix** by running the validator script:

   ```bash
   python3 "$SKILL_ROOT/bin/validate-yaml-tasks" <plan_path>
   ```

   - Exit 0 / stdout starts with `OK:` → proceed to step 4.
   - Exit 1 / stdout starts with `FAIL:` → treat the FAIL lines as an implicit NACK from a synthetic reviewer `yaml-validator`:
     - Write `verdicts/cycle-<N>/NACK-yaml-validator.json` with the FAIL output as `feedback` and `["<plan_path>:#yaml-tasks"]` as `artifact_references`.
     - Skip step 4 for this cycle; jump to step 5.

   The validator checks the same constraints described in `agents/task-planner.md`: presence of the `# yaml-tasks` fenced block, top-level `slices:` (or `phases:` legacy alias), TASK-ID pattern (with duplicate-id detection), role enum, required task fields, and `pr.title` (with `pr.test_plan` recommended-but-not-required, surfaced as a warning). It is portable — depends only on `python3` + PyYAML — and does not require the egg repo to be present.

4. **Spawn reviewer_plan** (single Agent call). Task context:
   - `plan_path`, `analysis_path`, `architect_output_path`, `task_planner_output_path`, `risk_analyst_output_path`
   - `verdict_path`: `verdicts/cycle-<N>/<verdict>-reviewer-plan.json`
   - if N > 1: `prior_nacks`

   Wait, validate verdict per [Verdict validation](#verdict-validation).

5. **Aggregate**:
   - reviewer_plan ACK and no yaml-validator NACK → write `verdicts/cycle-<N>/CONFIRMED-plan.json`, append BRC history, exit loop.
   - NACK present and N < 3 → revise. **Re-run policy**:
     - YAML validator NACK or task_planner-specific NACK → re-run `task_planner` only
     - risk_analyst-specific NACK → re-run `risk_analyst` only
     - NACK that calls out architect's decisions → re-run `architect` (Step 2.0), then both parallel producers
     - Otherwise → re-run both parallel producers
   - NACK and N == 3 → trigger cycle-limit AskUserQuestion.

## Emit contract

On convergence, parse the plan's YAML appendix into a Contract at `contracts/<id>.json`:

```bash
python3 "$SKILL_ROOT/bin/emit-contract" <plan_path> <contract_path> <pipeline_id> [--current-phase plan]
```

The emitter writes a Contract JSON matching `shared/egg_contracts/models.py::Contract` (schemaVersion 1.1) — egg-canonical field names (`pipeline_id`, `slices[].tasks[]`), `slice-<N>` / `task-<P>-<N>` lowercase IDs, lowercase `"pending"` status enum, `acceptance_criteria` (not `acceptance`), `files_affected` (not `files`), and the full set of optional fields egg expects (defaulted appropriately). Output loads cleanly through `Contract.model_validate` and round-trips identical. It runs without importing egg.

`<pipeline_id>` should match egg's pipeline-key convention: `issue-<N>` (optionally `issue-<N>-<qualifier>`) for issue-driven runs, or the JIRA key (e.g. `KORE-1234`) for ticket-driven runs.

## HITL gate

Summarize for the user: plan summary, slice/task counts, top 3 risks, reviewer verdict. Ask:

```
Header:   "Plan gate"
Question: "The plan is ready. Approve?"
Options:
  - "Approve and finalize"                       → Phase 3
  - "Request changes"                            → free-text feedback, return to cycle N+1
  - "Change approach (re-do plan from architect)" → return to Step 2.0
  - "Stop here"                                  → exit
```

Record in `reviews/<id>-plan-hitl-decision.json`.

---

# Phase 3 — Finalize

Print summary:

```
Refine + Plan complete.

Repo:        <owner/name>
Identifier:  <id>
State dir:   .refine-plan-state/<id>/

Refine team: refiner + reviewer_refine[ + reviewer_agent_design]
  cycles: <n>   final: <ACK | force-accepted>

Plan team:   architect → (task_planner ∥ risk_analyst) → reviewer_plan
  cycles: <n>   final: <ACK | force-accepted>

Artifacts:
  drafts/<id>-{analysis,plan}.md
  agent-outputs/<id>-{refiner,architect,task_planner,risk_analyst}-output.json
  reviews/<id>-{refine-*,plan-*}-review.json
  reviews/<id>-{refine,plan}-hitl-decision.json
  verdicts/cycle-*/              ← per-cycle verdict journal
  brc-history/<id>-{refine,plan}.{md,json}
  contracts/<id>.json            ← <n> tasks across <n> slices

Next steps:
  • Hand contracts/<id>.json to an implementer (human or agent)
  • Run /sdlc <id> to submit to the full egg pipeline (it re-runs refine+plan;
    these local artifacts are reference, not orchestrator inputs)
  • Read brc-history/<id>-{refine,plan}.md for the full deliberation log
```

This skill does **not** create PRs, push branches, or update GitHub issues. Local-only by design.

---

# Operating Notes

**Parallel fan-out is the only place we recover wall-time.** Always batch:
- Both/all refine reviewers in one message
- `task_planner` + `risk_analyst` in one message
- Never spawn them sequentially when they can go in parallel.

**Evidence is the line.** Reviewers with empty `artifact_references` are the same failure mode as egg reviewers rubber-stamping without attestations. Re-prompt; don't soften.

**Don't pass producer self-attestation to reviewers.** Producers' `agent-outputs/*.json` contains self-summary. Reviewers should read the *artifact* (`drafts/*.md`) and the handoff content they need for their rubric (e.g., reviewer_plan reads risk_analyst's output). They should not read the producer-being-reviewed's own self-rating. This is Delphi redaction *by convention*: the orchestrator names exactly which paths each reviewer is told to read in its task-context block, and the role files instruct reviewers to limit themselves to those. Reviewers are general-purpose subagents with the `Read` tool, so a reviewer that ignores its instructions could in principle read the producer's self-attestation (or the verdict-journal files) on its own — the redaction is enforced by prompt discipline, not by capability sandboxing. If you spot a reviewer citing producer self-attestation, NACK and re-prompt.

**Force-accept is recorded.** Cycle-limit force-accept (on cycle-3 NACK, or any operator-extended cycle past 3) goes into the HITL decision JSON and BRC history `resolution: hitl-force-accept`. Don't silently advance.

**State directory is namespaced.** `.refine-plan-state/` mirrors `.egg-state/` subdirectories on purpose; it is **not** ingested by the orchestrator. `/sdlc` re-runs refine+plan from scratch.

**Failure modes.**
- Subagent times out / writes no file → respawn once with the same prompt. If second attempt fails, surface to user.
- Reviewer returns malformed JSON or empty `artifact_references` → re-prompt that reviewer once with the specific error.
- YAML validator FAILs three task_planner revisions in a row → HITL escalate; don't keep looping.
- User picks "Other" on a HITL gate → treat as Request-Changes with their text as feedback.
