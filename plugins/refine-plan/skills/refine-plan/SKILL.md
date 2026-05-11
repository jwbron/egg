---
name: refine-plan
description: "Local Claude-Code mirror of egg's refine + plan phases: role-typed subagents, evidence-backed multi-agent review with a filesystem BRC bus, artifacts consistent with egg's .egg-state/ layout. Portable; no orchestrator, no experimental flags."
disable-model-invocation: true
argument-hint: "[JIRA-1234 | issue# | description] [--repo owner/name]"
allowed-tools: Agent Read Write Edit AskUserQuestion Bash(gh issue view:*) Bash(gh issue list:*) Bash(git remote:*) Bash(git -C * remote:*) Bash(mkdir:*) Bash(ls:*) Bash(test:*) Bash(python3:*) Bash(cat:*) Bash(cp:*)
---

# Refine + Plan (local mirror of egg's refine/plan phases)

A faithful local analogue of [egg's refine and plan phases](https://github.com/jwbron/egg/blob/main/skills/sdlc/SKILL.md), using Claude Code subagents and a filesystem-based BRC bus. No orchestrator, no Redis, no experimental flags.

**Mirrored from egg:**
- Refine team: `refiner` + `reviewer_refine` (+ `reviewer_agent_design` for the egg repo only)
- Plan team: `architect` → (`task_planner` ∥ `risk_analyst`) → `reviewer_plan`
- Evidence-backed verdicts: every ACK and NACK requires non-empty `artifact_references`
- Cycle bound: 3 producer revisions per phase (matches `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES`)
- Artifact layout under `.refine-plan-state/<id>/` mirrors `.egg-state/` subdirectories
- BRC deliberation persisted as files in `bus/cycle-<N>/`
- YAML appendix validated against `.egg/schemas/yaml-tasks.schema.json` shape

**Intentionally simplified:**
- No Redis message bus; reviewers don't exchange in-flight NACKs within a cycle (they do see prior-cycle NACKs on revision)
- No version-bumping or open-NACK barrier — every cycle reviews the latest draft fresh
- No automatic implement-phase handoff — produces artifacts you can hand to `/sdlc` or to a human

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

`<skill-root>` resolves to the directory containing this `SKILL.md`. Compute it once at start (the SKILL.md path is known to the running session).

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
  bus/                   # filesystem BRC bus (per-cycle)
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

### The BRC bus

For each cycle, the orchestrator writes/reads files at `bus/cycle-<N>/`:

| Filename pattern | Meaning | Written by |
|---|---|---|
| `PROPOSE-<producer>.json` | Producer has emitted its artifact for this cycle | Orchestrator (after producer subagent completes) |
| `ACK-<reviewer>.json` | Reviewer accepts the current proposal | Reviewer subagent (final action) AND orchestrator (validates the reviewer's response, copies to `reviews/`) |
| `NACK-<reviewer>.json` | Reviewer rejects | Same as ACK |
| `CONFIRMED-<phase>.json` | All critical reviewers ACKed in this cycle | Orchestrator |

The bus is the deliberation source of truth. `brc-history/<id>-<phase>.md` is generated from the bus contents at the end of each cycle for human readability.

### Verdict validation

Every reviewer must return a single JSON object as its final response. The orchestrator:

1. Parses the JSON. If malformed → re-prompt that single reviewer once with "Your previous response was invalid JSON. Re-emit the verdict JSON only, no surrounding prose."
2. Checks `artifact_references` is a non-empty array of strings. If empty → re-prompt that reviewer once with "Your `artifact_references` was empty. Include at least one specific file:line citation you verified before re-emitting."
3. Writes the validated verdict to `bus/cycle-<N>/<verdict>-<reviewer>.json` AND to `reviews/<id>-<phase>-<reviewer>-review.json`.

### Cycle bound

Each producer may be re-spawned at most **3 times per phase** (cycles 1–4 inclusive). On cycle 4 NACK, halt the auto-loop and ask via `AskUserQuestion`:

```
Header:  "Cycle limit"
Question: "Cycle 4 still has unresolved NACKs. What now?"
Options:
  - "Force-accept current draft"        → record override, advance
  - "One more revision with my guidance" → take free-text feedback, run cycle 5
  - "Abort phase"                       → exit, leave artifacts on disk
```

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

## Cycle loop (max 4 cycles)

For cycle N = 1..4:

1. **Spawn refiner** (single Agent call). Task context:
   - `task_brief`: <issue body / JIRA summary / free-text>
   - `analysis_path`: `<abs>/.refine-plan-state/<id>/drafts/<id>-analysis.md`
   - `handoff_path`: `<abs>/.refine-plan-state/<id>/agent-outputs/<id>-refiner-output.json`
   - `repo`: `<owner/name>`
   - if N > 1: `prior_nacks` from previous cycle's bus

2. **Wait for completion**, read both output files. Write `bus/cycle-<N>/PROPOSE-refiner.json`:
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
   - `verdict_path`: `bus/cycle-<N>/<verdict>-<reviewer-role>.json` (reviewer writes here as its final action)
   - if N > 1: `prior_nacks` from this reviewer's previous-cycle NACK (if any)

4. **Wait for both/all reviewers**, validate each verdict per [Verdict validation](#verdict-validation).

5. **Aggregate**:
   - All ACK → write `bus/cycle-<N>/CONFIRMED-refine.json`, append BRC history with `resolution: advance`, exit loop.
   - Any NACK and N ≤ 3 → append BRC history with `resolution: revise`, continue loop.
   - Any NACK and N == 4 → trigger [cycle-limit AskUserQuestion](#cycle-bound). Record outcome.

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

## Cycle loop (max 4 cycles)

For cycle N = 1..4:

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

2. **Wait for both**. Write `bus/cycle-<N>/PROPOSE-task_planner.json` and `PROPOSE-risk_analyst.json`.

3. **Validate the YAML appendix** with the validator below. On FAIL, treat as an implicit NACK from a synthetic reviewer `yaml-validator`:
   - Write `bus/cycle-<N>/NACK-yaml-validator.json` with the validator's error output as `feedback` and `["<plan_path>:#yaml-tasks"]` as `artifact_references`.
   - Skip step 4 for this cycle; jump to step 5.

   Validator (run via Bash):

   ```bash
   python3 - "<plan_path>" <<'PY'
   import json, re, sys, yaml
   plan = open(sys.argv[1]).read()
   m = re.search(r"```(?:yaml|yml)\s*\n\s*#\s*yaml-tasks\s*\n(.*?)```", plan, re.DOTALL)
   if not m:
       print("FAIL: no '# yaml-tasks' fenced block found"); sys.exit(1)
   try:
       data = yaml.safe_load(m.group(1))
   except yaml.YAMLError as e:
       print(f"FAIL: YAML parse error: {e}"); sys.exit(1)
   if not isinstance(data, dict):
       print("FAIL: top-level not a mapping"); sys.exit(1)
   tlk = "slices" if "slices" in data else ("phases" if "phases" in data else None)
   if tlk is None:
       print("FAIL: missing 'slices' or 'phases' key"); sys.exit(1)
   TASK_RE = re.compile(r"^TASK-\d+-\d+$", re.IGNORECASE)
   VALID_ROLES = {"coder", "tester", "documenter"}
   errors = []
   for sl in data[tlk]:
       for t in sl.get("tasks", []):
           if not TASK_RE.match(str(t.get("id", ""))):
               errors.append(f"bad task id: {t.get('id')!r}")
           if t.get("role") and t["role"] not in VALID_ROLES:
               errors.append(f"bad role {t['role']!r} on {t.get('id')}")
           for fld in ("description", "acceptance"):
               if not t.get(fld):
                   errors.append(f"missing {fld} on {t.get('id')}")
   pr = data.get("pr", {})
   for k in ("title", "description", "test_plan", "manual_steps"):
       if k not in pr:
           errors.append(f"pr.{k} missing")
   if errors:
       print("FAIL:")
       [print(f"  - {e}") for e in errors]
       sys.exit(1)
   print(f"OK: yaml-tasks valid (key={tlk}, slices={len(data[tlk])})")
   PY
   ```

4. **Spawn reviewer_plan** (single Agent call). Task context:
   - `plan_path`, `analysis_path`, `architect_output_path`, `task_planner_output_path`, `risk_analyst_output_path`
   - `verdict_path`: `bus/cycle-<N>/<verdict>-reviewer-plan.json`
   - if N > 1: `prior_nacks`

   Wait, validate verdict per [Verdict validation](#verdict-validation).

5. **Aggregate**:
   - reviewer_plan ACK and no yaml-validator NACK → write `bus/cycle-<N>/CONFIRMED-plan.json`, append BRC history, exit loop.
   - NACK present and N ≤ 3 → revise. **Re-run policy**:
     - YAML validator NACK or task_planner-specific NACK → re-run `task_planner` only
     - risk_analyst-specific NACK → re-run `risk_analyst` only
     - NACK that calls out architect's decisions → re-run `architect` (Step 2.0), then both parallel producers
     - Otherwise → re-run both parallel producers
   - NACK and N == 4 → trigger cycle-limit AskUserQuestion.

## Emit contract

On convergence, parse the plan's YAML appendix into a Task list at `contracts/<id>.json`:

```bash
python3 - "<plan_path>" "<contract_path>" "<id>" <<'PY'
import json, re, sys, yaml
plan = open(sys.argv[1]).read()
out_path = sys.argv[2]
identifier = sys.argv[3]
m = re.search(r"```(?:yaml|yml)\s*\n\s*#\s*yaml-tasks\s*\n(.*?)```", plan, re.DOTALL)
data = yaml.safe_load(m.group(1))
tlk = "slices" if "slices" in data else "phases"
tasks = []
for sl in data[tlk]:
    for t in sl.get("tasks", []):
        tasks.append({
            "id": t["id"],
            "slice_id": sl["id"],
            "description": t["description"],
            "acceptance": t["acceptance"],
            "role": t.get("role", "coder"),
            "files": t.get("files", []),
            "status": "PENDING",
        })
contract = {
    "identifier": identifier,
    "tasks": tasks,
    "pr": data.get("pr", {}),
    "source": sys.argv[1],
}
json.dump(contract, open(out_path, "w"), indent=2)
print(f"wrote contract: {len(tasks)} tasks across {len(data[tlk])} slices")
PY
```

This is a portable analogue of `shared/egg_contracts/plan_parser.py` and emits the same shape downstream consumers can read.

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
  bus/cycle-*/                   ← full BRC deliberation record
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

**Don't pass producer self-attestation to reviewers.** Producers' `agent-outputs/*.json` contains self-summary. Reviewers read the *artifact* (`drafts/*.md`) and the handoff content they need for their rubric (e.g., reviewer_plan reads risk_analyst's output). They do not read the producer-being-reviewed's own self-rating. This is Delphi redaction; falls out because we pick what each reviewer reads.

**Force-accept is recorded.** Cycle-4 force-accept goes into the HITL decision JSON and BRC history `resolution: hitl-force-accept`. Don't silently advance.

**State directory is namespaced.** `.refine-plan-state/` mirrors `.egg-state/` subdirectories on purpose; it is **not** ingested by the orchestrator. `/sdlc` re-runs refine+plan from scratch.

**Failure modes.**
- Subagent times out / writes no file → respawn once with the same prompt. If second attempt fails, surface to user.
- Reviewer returns malformed JSON or empty `artifact_references` → re-prompt that reviewer once with the specific error.
- YAML validator FAILs three task_planner revisions in a row → HITL escalate; don't keep looping.
- User picks "Other" on a HITL gate → treat as Request-Changes with their text as feedback.
