# Analysis: Improve overseer escalation/issue opening behavior

> Issue: #1962 | Phase: refine

## Problem Statement

The overseer agent is "pretty good" at escalating to humans via
`OVERSEER_ALERT`, but the issue author flags three related gaps:

1. **Escalation reliability** — the overseer may not always escalate
   when it should. Tightening *"when appropriate"* is part of the ask.
2. **Autonomous issue filing** — when the overseer observes a real egg
   bug (not just a flaky agent), it should open a GitHub issue
   automatically so the bug is tracked. Today the agent-side overseer
   rules in `sandbox/agent-config/rules/overseer.md:179` explicitly
   say *"you do not file issues yourself"*, so this behavior is
   forbidden by policy even though the underlying plumbing exists.
3. **Host → overseer migration** — the `/sdlc` skill currently does a
   lot of mid-pipeline debugging (stall detection, agent nudging,
   `get_container_logs` on demand, NACK escalation, 60-min long-run
   rescue). The issue's desired end state is that the host session
   *"does nothing other than report what's going on in the pipeline
   based on events sent to it (after #1932) and manage HITL
   decisions"*. All investigative and nudge logic should migrate into
   the overseer.

The author also mentions a *possible* future capability: letting the
overseer launch sub-agents to investigate asynchronously. This is
called out in the issue as speculative (*"one thing we might want to
support"*), so it belongs in a follow-up rather than this pipeline's
required scope.

## Current Behavior

### Two coexisting overseer implementations

The codebase already contains **two** things named "overseer", and this
is central to the problem space:

1. **Sandbox LLM overseer (deployed)**
   - Spawned per-phase by `orchestrator/routes/pipelines.py:10935-10964`
     calling `spawn_overseer_container` in
     `orchestrator/kubernetes_spawner.py:1182-1254`.
   - Runs inside a sandbox container as a Claude Code agent using the
     prompt at `kubernetes_spawner.py:1220-1234` and the rules at
     `sandbox/agent-config/rules/overseer.md`.
   - Polls by repeatedly running
     `python3 /opt/egg-runtime/sandbox/overseer_monitor.py --once`
     (see `sandbox/overseer_monitor.py:143-189`).
   - Escalates via `egg-orch overseer alert` (CLI wrapper defined in
     `sandbox/bin/egg-orch:2629+`).
   - **Cannot file GitHub issues today.** The rules explicitly forbid
     it (`overseer.md:179`).

2. **Orchestrator-side `OverseerMonitor` class (not deployed to
   pipelines)**
   - `orchestrator/overseer/monitor.py` (2005 LOC) with supporting
     `classifier.py`, `decision_maker.py`, `issue_filer.py`,
     `self_monitor.py`.
   - `decide_corrective_action` in `decision_maker.py:99-155` defines
     a ladder: `nudge` → `redirect` → `restart_agent` → `hitl` →
     `restart_phase` → `issue` → `slack`.
   - The `issue` branch of `_execute_action`
     (`orchestrator/overseer/monitor.py:620-629`) calls
     `file_diagnostic_issue` (`orchestrator/overseer/issue_filer.py`),
     which invokes `gh issue create` with a structured
     `## Pipeline Diagnostic:` body and labels
     `["egg:diagnostic", "pipeline-health"]`.
   - A grep for callers confirms `OverseerMonitor(` and
     `file_diagnostic_issue(` are referenced **only in
     `orchestrator/tests/`** — there is no production instantiation.
   - The labels `egg:diagnostic` and `pipeline-health` do not exist in
     `gh label list --repo jwbron/egg`.
   - Documentation in `docs/guides/pipeline-health-monitoring.md:409-442`
     already describes "Autonomous Issue Filing", including the
     structured template and an `overseer-alert` label convention —
     but the running overseer doesn't use any of it.

In other words: the *infrastructure* for autonomous issue filing was
built for an overseer architecture that was later superseded by the
LLM-agent design. The new design never picked the capability back up.

### Escalation path today

The deployed overseer's entire remediation vocabulary is
`OVERSEER_ALERT`. Rules at `overseer.md:116-127, 140-153` enumerate:

- Stuck phase transition (BRC confirmed, no advance in ~60s)
- Orchestrator silent on consensus
- Repeated 401 from orchestrator endpoints
- Heartbeat stall on an active agent
- Persistent agent loop (Haiku classifier confidence > 0.8, two
  consecutive cycles)
- Same anomaly across N cycles without resolution (escalate priority)

Peripheral allowed actions: send a low-stakes peer STATUS message for
clarification, resolve its own health alerts, hand off to a mediator.
Nothing else — and explicitly **not** phase advancement, consensus
mutation, decision creation, or container spawn.

### Host-side logic that would migrate

`skills/sdlc/SKILL.md` today owns (sections cited inline):

- **Phase-based stall detection** (3 min) — `SKILL.md:489-494`
- **Silent-agent detection** (10 min in `running_agents`, zero
  messages) — `SKILL.md:496-508`
- **NACK escalation** (3 min unresolved) — `SKILL.md:531-553`
- **Post-nudge escalation** (3 min after a nudge) — `SKILL.md:555-566`
- **Long-running phase detection** (60 min, implement phase) —
  `SKILL.md:572-596`
- **Stuck pipeline rescue** (open draft PR, cancel, retry) —
  `SKILL.md:598-639`
- **Check-agent-logs** and **nudge-agent** side flows on user choice —
  multiple spots including `SKILL.md:527-552`, `SKILL.md:1371-1383`.
- State tracking for `{role: {phase, phase_entered_at, nudged_at,
  first_seen_at, has_any_messages}}` — `SKILL.md:568-570`.

After #1971 (merged — `wait_for_status_change`), the host can wake
event-driven on `OVERSEER_ALERT` / `PHASE_STARTED` /
`PHASE_COMPLETED` / `DECISION_CREATED` / `PIPELINE_*` / `CONSENSUS_*`.
The plumbing required for the "host is a pure reporter" end state is
therefore in place.

### Existing related infrastructure that partially overlaps

- **Tier-1 deterministic tripwires** already run inside the
  orchestrator (`orchestrator/health_monitor.py`) — heartbeat
  timeouts, container exits, repeated errors, progress stalls. Their
  output becomes health-alerts that the overseer classifier picks up.
- **Tier-2 classifier/decision-maker** definitions exist in
  `orchestrator/overseer/{classifier,decision_maker}.py` — Haiku
  classify, Sonnet decide — but live inside the unused orchestrator
  overseer class. Some of this logic would need to move into the
  agent-side overseer (prompted) or be called by it.
- **`egg-orch overseer alert`** — the dedicated CLI for
  `OVERSEER_ALERT` emission. No corresponding
  `egg-orch overseer file-issue` today.
- **GitHub label inventory** — `agent:overseer` exists;
  `egg:diagnostic`, `pipeline-health`, `overseer-alert`,
  `overseer-opened` do not.

### Interaction with other open issues

Several in-flight issues touch this area and should inform scope:

- **#1971** — event-driven host wait. **Merged.** Dependency cleared.
- **#1806** (p2) — "Investigate overlap between overseer agent and
  proposed `deployment-diagnose` / `agent-diagnose` skills". Directly
  relevant to auto-issue filing policy (what qualifies as an
  "anomaly worth an issue" vs. a "skill invocation").
- **#1786** (p2) — "Per-role PATH restriction for sandbox agents".
  Whatever new CLI/MCP verb the overseer uses to file issues must fit
  the role-restriction model.
- **#1902** (p3) — "Overseer file-boundary policy". Today
  `OVERSEER_PATTERNS` in `shared/egg_restrictions/patterns.py` is
  oversight-only. Any code change that has the overseer spawning
  sub-agents or writing to new artifact paths interacts with this.
- **#1722** (p1) — "Overseer misdiagnoses deadlock after phase
  restart due to stale AGENT_FAILED messages". A false-positive case
  that is directly relevant to tightening *"when appropriate"*.
- **#1727** (p3) — "Overseer exits with code 0 early in fresh
  pipeline". Another false-positive class.

## Constraints

- **Scope-of-action constraint.** Per `overseer.md:11-28` and #1786,
  the overseer cannot perform pipeline-lifecycle mutations. A new
  "file issue" action sits at the edge of this boundary: it writes to
  GitHub, not to the pipeline, so it's not strictly
  pipeline-mutating. But dedup / race conditions between the overseer
  and a human filing the same issue need a clear policy.
- **Phase-scoped lifetime.** The overseer is spawned per-phase and
  torn down at phase completion/advance/failure
  (`sandbox/agent-config/rules/overseer.md:5`). Any state the overseer
  needs to persist across phases (e.g., "I already filed issue #X for
  this anomaly") must live outside the sandbox container.
- **File-boundary policy.** `OVERSEER_PATTERNS` only allows writes to
  `.egg-state/oversight/` and `.egg-state/agent-outputs/`
  (#1902). Any new logs or issue-filing state must fit inside those
  prefixes.
- **Gateway & authentication.** The overseer calls `gh` via the
  gateway, which enforces credential and command policy. Issue
  creation is already allowed in other roles (refiner creates
  decisions, coder creates PRs), but the gateway needs an explicit
  policy for the overseer role if it's going to call `gh issue
  create`.
- **Duplicate-issue avoidance.** No existing dedup mechanism. A
  phase-scoped overseer that re-evaluates the same anomaly each cycle
  will file multiple issues unless dedup is explicit (e.g., search
  open issues by `overseer-opened` + anomaly type + pipeline id
  before filing; or persist filed-issue state in
  `.egg-state/oversight/filed-issues.json`).
- **LLM cost budget.** The overseer already has
  `max_llm_cost_per_hour` (default $5/hr per `overseer.md:183-189`).
  Any new classification work (e.g., "is this an egg bug or a flake")
  must fit inside that envelope.
- **Human-trust constraint.** Auto-filing issues that turn out to be
  noise creates toil (human has to close/triage). Conservative policy
  bias is important: when in doubt, alert but don't file.
- **Inversion of control with `/sdlc`.** Migrating stall/NACK/log
  logic into the overseer means the *host* stops deciding "check the
  agent logs" — the overseer has to do it proactively and include its
  findings in `OVERSEER_ALERT`. This changes the alert payload shape
  and the host's surfacing logic.

## Options Considered

The scope-split question is the biggest fork, so each option below
packages a different answer to it. All options assume #1971 has
landed (which it has).

### Option A: One pipeline covering all three threads (escalation tuning + auto-issue filing + host migration)

**Approach**: Treat the issue as a single, tightly-scoped overseer
refactor. In one pipeline:

1. Wire the existing `file_diagnostic_issue` plumbing into the
   *agent-side* overseer via a new `egg-orch overseer file-issue` CLI
   verb and corresponding MCP tool.
2. Update `overseer.md` rules: replace *"you do not file issues
   yourself"* with an `overseer_may_file_issue` policy (criteria to
   be chosen by the human — see Open Questions).
3. Add dedup: search existing open issues by anomaly-type label
   before filing; persist a filed-issues cache in
   `.egg-state/oversight/`.
4. Create labels `egg:diagnostic`, `overseer-opened`,
   `pipeline-health` (or whichever naming wins — see Open Questions).
5. Move host-side logic from `/sdlc` into the overseer:
   - Stall detection → move the per-role phase timing, silent-agent
     tracking, NACK-age tracking from `SKILL.md:489-570` into
     equivalent overseer checks.
   - Nudge-agent → overseer sends peer STATUS messages instead of the
     host doing it.
   - Log-check → overseer fetches `get_container_logs` proactively
     when classifying, and attaches findings to `--detail`.
   - Long-running phase detection + stuck pipeline rescue → surface
     via `OVERSEER_ALERT` with clear `--recommend` guidance so the
     host's only job is to surface the alert and offer the choice.
6. Update `/sdlc` skill: delete the moved detection logic, keep only
   alert-surfacing, HITL handling, and Phase-5 failure rescue.

**Pros**:
- Single coherent story — lands escalation, auto-issue, and host
  migration together.
- Keeps documentation and rollout consistent.
- Auto-issue policy gets validated by the same pipeline that migrates
  the stall logic, reducing risk of mismatched behavior.

**Cons**:
- Large surface area. Likely touches `sandbox/agent-config/rules/
  overseer.md`, `sandbox/overseer_monitor.py`, the orchestrator-side
  overseer Python files, `sandbox/bin/egg-orch`, `skills/sdlc/SKILL.md`,
  `shared/egg_restrictions/patterns.py`, gateway policy, label
  creation, docs.
- Harder to review safely in BRC — a single implement phase would
  have cross-cutting concerns that a reviewer must hold in head at
  once.
- Longer time-to-first-value: users don't see the auto-issue benefit
  until the host migration is also done.

### Option B: Split into two pipelines — (escalation tuning + auto-issue) now, host migration later

**Approach**: First pipeline lands:

1. Escalation-trigger tightening (see Open Questions for policy).
2. Auto-issue filing: wire `file_diagnostic_issue` into the
   agent-side overseer, add CLI verb, add dedup, flip `overseer.md`
   policy, create labels.
3. Do **not** migrate stall/NACK/log/long-run logic from the host.

Follow-up pipeline (separate issue) migrates `/sdlc` host logic into
the overseer.

**Pros**:
- Smaller, easier-to-review diff for each pipeline.
- Auto-issue benefit ships faster.
- Each pipeline has a single clear story: "teach overseer to file
  issues" vs. "move host babysitting into overseer".
- Each pipeline's reviewer set can be scoped tighter.

**Cons**:
- The host keeps its current behavior a while longer — the original
  end-state ("host does nothing other than report") isn't reached
  until the follow-up lands.
- Risk that the follow-up gets deprioritized, leaving the two-layer
  architecture permanently.

### Option C: Three separate pipelines — one thread per pipeline

**Approach**:

1. Pipeline 1: escalation tuning only (tighten triggers, fix
   #1722-style false positives, improve rule doc guidance).
2. Pipeline 2: auto-issue filing.
3. Pipeline 3: host → overseer migration.

**Pros**:
- Smallest review surface per PR.
- Each thread can have distinct success criteria and reviewer sets.
- Escalation tuning and auto-issue can release independently.

**Cons**:
- Coordination cost across three pipelines. Decisions made in
  pipeline 1 (what does "same anomaly" dedup look like) directly
  affect pipeline 2 (what does "file issue" dedup look like) — so
  they'll either repeat work or wait on each other.
- Three sets of contracts / reviews / plan docs to maintain.
- Minor overlap: the agent-side overseer prompt is touched by all
  three, leading to rebase friction.

### Option D: Narrow this pipeline to just auto-issue filing; handle the other threads as follow-ups

**Approach**: This pipeline delivers only:

1. A new `egg-orch overseer file-issue` verb + policy in rules.
2. Dedup logic.
3. Label creation.
4. The minimum rule-doc change: replace the forbidding language with
   "file issues when <policy criteria>, dedup before filing".

Escalation tuning and host migration become separate issues/pipelines.

**Pros**:
- Smallest scope; fastest ship.
- Clear, testable contract: overseer can file issues; it files them
  when the policy says to; it dedups.

**Cons**:
- Doesn't address thread 1 ("overseer doesn't always escalate") —
  that problem is what the human pointed at *first* in the issue.
- Doesn't address the "host does nothing but report" end state.
- The issue as written asks for three things; picking only one feels
  like half an answer.

## Recommended Approach

**Recommendation: Option B — escalation tuning + auto-issue filing in
this pipeline, host migration as a follow-up.**

Rationale:

- Threads 1 and 2 (escalation tuning + auto-issue) share the same
  decision-maker: *when is an anomaly worth surfacing, and at what
  level?* Both need clear policy on "severe/persistent enough" and on
  dedup. Doing them together means one consistent policy is
  articulated once, rather than tuning triggers and then re-visiting
  them when auto-issue lands.
- Thread 3 (host migration) is structurally different. It's a
  refactor-style change: move existing logic across a process
  boundary with minimal behavior change. It can be validated
  independently by running existing `/sdlc` regression flows and
  comparing behavior before/after. It also has a different risk
  profile — bugs in the host migration produce *fewer* alerts
  (silent failure); bugs in auto-issue produce *more* issues (noisy
  failure). Different mitigations are appropriate, which is easier in
  a dedicated pipeline.
- The infrastructure for auto-issue filing already exists in dead
  code (`orchestrator/overseer/issue_filer.py`). Rewiring it is
  cheaper than rebuilding. This is a strong argument for doing
  threads 1+2 now while the wiring is still visible.
- `/sdlc` already works. Deferring the migration is a safe
  incremental posture — we don't regress a working host flow while we
  untangle the overseer's alert and issue policy.
- The speculative sub-agent-launching capability can be a separate
  follow-up issue (it's explicitly optional in the ticket).

The plan and implement phases of *this* pipeline would be sized
around (a) policy + rule-doc changes, (b) CLI/MCP verb, (c) dedup
implementation, (d) label creation, (e) tightening existing escalation
triggers based on #1722-style false positives and the Open Questions
below. A follow-up issue would carry the `/sdlc` migration.

## Open Questions

Every question below has been registered as either a contract
`choice` decision (single-select, `decision-N`) or an entry inside
the open-ended `feedback-1` bundle (`Q1` … `Q7`). Decision IDs are
stable across this pipeline; options are shown verbatim as
registered. Questions are grouped by theme for readability.

### Scope & pipeline structure

- **decision-1** — Scope split: one pipeline, two pipelines, three
  pipelines, or narrow to auto-issue only?
- **decision-2** — Sub-agent launching (the *"one thing we might
  want to support"* in the issue body): in scope or deferred to a
  follow-up?
- **decision-3** — Consolidation with related overseer issues
  (#1722 stale AGENT_FAILED misdiagnosis, #1727 early-exit respawn):
  absorb, partially absorb, or leave in their own pipelines?

### Auto-issue filing policy

- **decision-4** — What qualifies for auto-issue filing?
  (Systemic-only / all-high-priority / Sonnet-gated / hybrid.)
- **decision-5** — Dedup window & scope (per pipeline / per repo /
  global / none).
- **decision-6** — Dedup state storage (in-process / local file /
  orchestrator-central / hybrid).
- **decision-7** — Label convention (existing dead-code
  `egg:diagnostic` + `pipeline-health` / docs' `overseer-alert` +
  category / pre-refine-notes' `overseer-opened` / new naming).
- **decision-8** — Issue body template (keep existing / extend /
  redesign).
- **decision-9** — Who runs `gh issue create` (agent-side CLI verb /
  orchestrator REST endpoint / hybrid / reuse dead-code
  OverseerMonitor in production).
- **decision-15** — Should `OVERSEER_PATTERNS` be expanded for
  dedup-state files under `.egg-state/oversight/`?
- **feedback-1 Q3** — Per-pipeline cap on auto-filed issues (e.g.,
  1/phase, 3/pipeline) before the overseer stops filing and
  escalates.
- **feedback-1 Q4** — Gateway policy constraints for
  `gh issue create` from overseer role.

### Escalation tuning (thread 1)

- **feedback-1 Q1** — Which specific escalation triggers need
  tightening (false-positive / false-negative patterns, including
  #1722 staleness, legitimate-long-run misclassification, 401
  handling).
- **feedback-1 Q2** — Baseline threshold review (`~60s`
  stuck-transition, `0.8` loop confidence, `3` cycles before
  re-alert).

### Host → overseer migration (thread 3)

- **decision-11** — Defer thread 3 to a follow-up issue (confirming
  the recommended Option B), migrate in this pipeline, or migrate
  partially?
- **decision-12** — `/sdlc` wall-clock thresholds (3 min stall, 10
  min silent, 60 min long-run) on migration: keep identical /
  configurable / revise / defer.
- **decision-16** — State-map home for agent timing (overseer only /
  orchestrator health_monitor / `.egg-state/oversight/`).
- **feedback-1 Q5** — What must stay in `/sdlc` if thread 3 is in
  scope (e.g., which `AskUserQuestion` options).

### Interaction with existing issues

- **decision-13** — Coordination with #1806 (overseer vs.
  `deployment-diagnose` / `agent-diagnose` skills).
- **decision-14** — Coordination with #1786 (per-role PATH
  restriction) for any new `egg-orch overseer file-issue` verb.
- **decision-15** — (also listed above) Coordination with #1902
  (overseer file-boundary patterns).

### Testing & rollout

- **decision-10** — Rollout mode (shadow / live / feature-flag).
- **feedback-1 Q6** — Success criteria / observable signals.
- **feedback-1 Q7** — Any additional constraints or preferences not
  covered above (filing mechanism, approvers, notification routing).

---

*Authored-by: egg*

## Complexity Assessment

**Complexity: high.**

Even under the recommended scope (Option B — escalation tuning +
auto-issue in this pipeline, host migration deferred), the change is
cross-cutting:

- Agent-side overseer rules (`sandbox/agent-config/rules/overseer.md`)
- Agent-side monitor script (`sandbox/overseer_monitor.py`) if new
  data needs to flow
- New CLI verb in `sandbox/bin/egg-orch` (or orchestrator route)
- Dead-code revival + wiring of `orchestrator/overseer/issue_filer.py`
- Possible MCP tool addition
- Gateway policy for `gh issue create` from overseer role
- File-boundary changes (`shared/egg_restrictions/patterns.py`) for
  oversight state
- GitHub label creation
- Documentation updates across `docs/guides/pipeline-health-monitoring.md`,
  `docs/reference/agent-roles.md`, `docs/architecture/orchestrator.md`,
  `sandbox/agent-config/rules/overseer.md`
- Rule-doc drift gate (merged recently in #1981) will need alignment

If the human picks Option A (one pipeline for all three threads), the
complexity scales up further — `skills/sdlc/SKILL.md` restructuring is
its own large surface. Complexity remains **high** either way.
