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
  multiple spots including `SKILL.md:527-552` and the Phase S5
  short-flow copies at `SKILL.md:1359-1383`
  (stall detection + NACK handling).
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

- **#1932** — "Event-driven wake for SDLC skill's monitor loop
  (host-side)". **Closed** (the issue the refine-phase notes cite as
  *"based on events sent to it (after #1932)"*). The server-side
  plumbing shipped via #1919 and the host-side plumbing via #1971 —
  both prerequisites for a host-is-a-pure-reporter end state are
  therefore in place.
- **#1971** — event-driven host wait (`wait_for_status_change` MCP
  tool). **Merged.** Dependency cleared.
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

Every question below is registered on the SDLC contract (verified
via `mcp__sdlc__show_contract`): 16 `hitl` choice decisions
(`decision-1` … `decision-16`) plus one open-ended feedback bundle
`feedback-1` with seven questions (`Q1` … `Q7`). Each decision is
reproduced inline with its registered options; the checkbox syntax
matches the contract-gateway's markdown format. The "(Recommended)"
suffix on an option reflects the refiner's preference surfaced in
the **Recommended Approach** section above.

**Note on refine vs. plan scope:** reviewer_refine's feedback
correctly flags that several decisions (notably decision-12,
decision-13, decision-14, decision-16, and feedback-1 Q5 / Q6 / Q7)
are planning-phase concerns. They are registered here so the contract
carries the full question set into plan; the human may leave them
unanswered at the refine gate and the planner will re-surface them
once scope (decision-1) is known.

### Scope & pipeline structure

<!-- egg-hitl-decision id=decision-1 -->

**Scope split: how should the three threads in this issue
(escalation tuning, auto-issue filing, host→overseer migration) be
packaged?**

- [ ] Option B (recommended) — one pipeline for escalation tuning + auto-issue filing; host migration as a follow-up issue
- [ ] Option A — single pipeline covering all three threads
- [ ] Option C — three separate pipelines, one per thread
- [ ] Option D — narrow this pipeline to auto-issue filing only; both other threads become follow-ups
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-2 -->

**Sub-agent launching by the overseer (the speculative capability in
the issue body): in scope for this pipeline or deferred?**

- [ ] Deferred — file a follow-up issue, out of scope here (Recommended — the issue body marks it as speculative)
- [ ] In scope — design a sub-agent-launch capability in this pipeline
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-3 -->

**Should this pipeline also address related overseer bugs (#1722 stale
AGENT_FAILED misdiagnosis, #1727 early-exit respawn), or leave them to
their own pipelines?**

- [ ] Leave them in their own pipelines — link as context only
- [ ] Absorb #1722 into this pipeline (it is a p1 false-positive case)
- [ ] Absorb both #1722 and #1727 into this pipeline
- [ ] Absorb only #1727
- [ ] Other (explain in reply)

### Auto-issue filing policy

<!-- egg-hitl-decision id=decision-4 -->

**Auto-issue filing policy: what qualifies for the overseer to
auto-file a GitHub issue?**

Design note: the "Sonnet-gated" option means the overseer agent
applies Sonnet-tier reasoning *in its existing polling loop* to
decide whether to file — not a separate orchestrator-side classifier
service. Implementing it as a separate service would re-introduce
the non-agent decision pipeline this pipeline is trying to simplify
(see decision-9 opt-4 caveat).

- [ ] Persistent / systemic only — same anomaly across N cycles AND clear orchestrator/agent-code bug signals
- [ ] All priority=high OVERSEER_ALERTs with dedup on anomaly type within pipeline
- [ ] Sonnet-gated per-anomaly rubric — LLM decides case-by-case (in-loop; see design note above)
- [ ] Hybrid — systemic criteria as the floor, Sonnet gate above it
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-5 -->

**Auto-issue dedup scope: when the overseer considers filing, what
window should it search to avoid duplicates?**

- [ ] Per pipeline — dedup only against this pipeline's prior filings
- [ ] Per repo — search all open overseer-filed issues across the repo by anomaly label
- [ ] Global — search across all open issues, including human-filed ones, by label + title fuzzy match
- [ ] No dedup — file every time; accept some duplication risk
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-6 -->

**Dedup state storage: where does the overseer remember issues it has
already filed?**

- [ ] In-process only — each phase's overseer starts fresh; rely on GitHub search each time
- [ ] Persisted to `.egg-state/oversight/filed-issues.json` so respawns/new phases remember
- [ ] Persisted via orchestrator REST endpoint (central store); overseer asks "have I filed this?" before filing
- [ ] Hybrid — persist locally AND verify via GitHub search before filing
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-7 -->

**Label convention for overseer-filed issues: which label set should
we standardize on?**

- [ ] Use existing dead-code convention: `egg:diagnostic` + `pipeline-health` + anomaly-type label
- [ ] Use docs convention: `overseer-alert` + category label (e.g., `stall`, `repeated-error`)
- [ ] Use pre-refine-notes convention: `overseer-opened` + anomaly-type label + pipeline-id link in body
- [ ] New naming — propose in 'Other' (explain in reply)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-8 -->

**Issue body template: keep the existing `## Pipeline Diagnostic: …`
template at `orchestrator/overseer/issue_filer.py:86-107`, or
redesign?**

- [ ] Keep existing template (pipeline + phase + agent + timeline + classification + actions + logs + remediation)
- [ ] Extend the existing template with explicit links (pipeline ID, phase, branch, commit SHA, parent-alert message ID) (Recommended — minimal change that improves triage)
- [ ] Redesign — draft a new template in the plan phase
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-9 -->

**Who actually runs `gh issue create` for an overseer-filed issue?**

Design note: option 4 ("reuse the dead-code `file_diagnostic_issue`
path by instantiating `OverseerMonitor` in production") is **not
recommended** — it would re-introduce an orchestrator-side
classifier + decision_maker pipeline that duplicates judgment the
LLM overseer already makes, reversing the agent-mode direction of
the current architecture. Kept as an option for completeness, but
the hybrid (opt-3) is the agent-mode-friendly middle ground.

- [ ] Agent-side overseer runs it in its sandbox via a new `egg-orch overseer file-issue` CLI verb (Recommended if going live directly; simplest surface)
- [ ] Orchestrator-side — new REST endpoint; server runs `gh` with its own credentials; overseer POSTs payload
- [ ] Hybrid — overseer composes body, orchestrator files it; orchestrator enforces central rate-limit / dedup policy (Recommended if going the feature-flag route — central policy enforcement is easier here)
- [ ] Reuse the existing dead-code `file_diagnostic_issue` path by instantiating OverseerMonitor in production — **not recommended** (see design note above)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-10 -->

**Rollout mode for auto-issue filing: should it start in shadow mode
or go live directly?**

- [ ] Shadow mode first — overseer composes issue body but raises a HITL decision "should I file this?"; human approves; flip to live after a trial period
- [ ] Live directly — ship with dedup + per-pipeline cap; iterate on policy via actual issues filed
- [ ] Feature flag — ship live but gated by config `overseer_auto_file_issues` (default off) for a release (Recommended — easy revert path while validating policy)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-15 -->

**Coordination with #1902 (overseer file-boundary policy): should
this pipeline expand `OVERSEER_PATTERNS` to allow writes under
`.egg-state/oversight/` for filed-issue dedup state?**

- [ ] Yes — expand OVERSEER_PATTERNS allowlist to include the chosen dedup state file
- [ ] No — use an orchestrator-side store instead; overseer never writes dedup state locally
- [ ] Defer — decide during plan phase based on the chosen dedup-storage option (decision-6) (Recommended — this decision is downstream of decision-6)
- [ ] Other (explain in reply)

(Also relevant to the "Interaction with existing issues" theme — this
is the sole instance of decision-15.)

### Escalation tuning (thread 1)

Open-ended questions (no discrete options) are bundled in
`feedback-1 Q1 … Q7`. The full text of each question is in the
feedback comment created on the issue; refiner-paraphrased headlines
for this theme:

- `feedback-1 Q1` — which specific escalation triggers need
  tightening (false-positive / false-negative patterns — e.g., #1722
  stale `AGENT_FAILED`, legitimate long test runs mis-classified as
  stalls, 401 patterns that should / shouldn't escalate).
- `feedback-1 Q2` — baseline threshold review (~60 s
  stuck-transition, 0.8 loop-detection confidence, 3 cycles before
  re-alert).

### Host → overseer migration (thread 3)

<!-- egg-hitl-decision id=decision-11 -->

**If thread 3 is in scope (decision-1 ≠ Option B and ≠ Option D),
should the migration be full or partial?**

Dependency: this decision is only meaningful if `decision-1`
resolves to Option A or Option C. If `decision-1` = Option B
(recommended) or Option D, treat this as automatically answered
"Yes — defer" and proceed.

- [ ] Yes — file a follow-up issue; keep this pipeline scoped to escalation tuning + auto-issue filing (Recommended — consistent with Option B on decision-1)
- [ ] No — migrate host logic in this pipeline
- [ ] Partial — migrate only stall/NACK detection; leave long-running-phase + stuck-pipeline-rescue with /sdlc
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-12 -->

**/sdlc wall-clock thresholds (3 min stall, 10 min silent agent, 60
min long-running phase): when / if they migrate to the overseer,
should the thresholds change?** *(Plan-phase candidate — may be left
unanswered at the refine gate.)*

- [ ] Keep identical — same numeric thresholds, just moved across the process boundary
- [ ] Make configurable per-pipeline via PipelineConfig (default to current values) (Recommended — matches the existing `overseer_*` config pattern in `orchestrator/models.py:343-379`)
- [ ] Revise numbers as part of migration — human to specify in plan phase
- [ ] Defer until the migration pipeline
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-16 -->

**If host→overseer migration is in scope (decision-11 = No or
Partial), where does the cross-cycle agent-timing state live
(currently /sdlc's in-memory map of `{role: {phase,
phase_entered_at, nudged_at, first_seen_at, has_any_messages}}`)?**
*(Plan-phase candidate — only relevant if thread 3 is in scope.)*

- [ ] In the overseer only — phase-scoped, lost on phase transition / overseer respawn
- [ ] In the orchestrator's health_monitor — persistent across phases / respawns
- [ ] In `.egg-state/oversight/agent-timing.json` — persistent, phase-scoped, overseer-owned
- [ ] N/A — thread 3 deferred per decision-11
- [ ] Other (explain in reply)

Also see open-ended `feedback-1 Q5` — what must stay in `/sdlc` if
thread 3 is in scope (e.g., which `AskUserQuestion` options).

### Interaction with existing issues

<!-- egg-hitl-decision id=decision-13 -->

**Coordination with #1806 (overseer vs. `deployment-diagnose` /
`agent-diagnose` skills overlap)?** *(Plan-phase candidate — the
auto-issue policy chosen here will influence this.)*

- [ ] Proceed independently — let #1806 adapt to whatever auto-issue policy lands here (Recommended — decouple scope)
- [ ] Coordinate — resolve the overlap question in #1806 first (or as a blocking decision) before finalizing auto-issue scope
- [ ] Absorb part of #1806 into this pipeline — pick a consolidation stance now
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-14 -->

**Coordination with #1786 (per-role PATH restriction): should this
pipeline also bake in overseer PATH restrictions for any new
`egg-orch overseer file-issue` verb?** *(Plan-phase candidate.)*

- [ ] No — wait for #1786 to ship on its own; use server-side gateway enforcement only
- [ ] Yes — pre-allocate a role-specific PATH entry as part of this pipeline
- [ ] Add a gateway allowlist rule now, defer PATH restructuring to #1786 (Recommended — gateway enforcement is belt-and-suspenders against bad prompt changes)
- [ ] Other (explain in reply)

### Open-ended feedback (`feedback-1`)

Seven open-ended questions are bundled in `feedback-1`. The human
edits the feedback comment directly; no per-question markers are
needed inline. Refiner-paraphrased headlines:

- `Q1` — specific escalation-trigger false positives / negatives
  needing tightening (includes #1722 patterns).
- `Q2` — baseline threshold review (60 s / 0.8 / 3 cycles).
- `Q3` — per-pipeline cap on auto-filed issues (e.g., 1 / phase).
- `Q4` — gateway policy constraints for `gh issue create` from the
  overseer role (label injection, size limits, rate-limit, allowed
  repos). *Note: this is partially a code-research question — if
  the human leaves it blank, the plan phase should answer it from
  the `gateway/` and `shared/egg_restrictions/patterns.py`
  inspection.*
- `Q5` — if thread 3 is in scope, what must stay in `/sdlc` (which
  `AskUserQuestion` options, etc.).
- `Q6` — success criteria / observable signals for "overseer
  escalating well".
- `Q7` — any additional constraints (GitHub App vs. `gh` CLI,
  approvers, notification routing).

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

**Tests / regression risk.** Several existing test suites will need
updating or extension — flagged here so planners size for them:

- `orchestrator/tests/test_overseer_monitor.py` — exercises the dead-code
  `OverseerMonitor` + its `_execute_action` `"issue"` branch. If we
  revive that path (decision-9 opt-4) these tests become load-bearing;
  if we don't, they may need pruning or marking as reference-only to
  avoid drift.
- `orchestrator/tests/test_overseer_issue_filer.py` — directly tests
  the dead-code `file_diagnostic_issue` builder; template changes
  (decision-8) land here.
- `orchestrator/tests/test_overseer_alert_isolation.py`,
  `test_overseer_hitl_integration.py`, `test_two_tier_integration.py`,
  `test_infra_error_escalation.py` — all reference
  `OverseerMonitor`. Policy changes that alter the action vocabulary
  (e.g., adding a `file-issue` verb to the agent-side overseer CLI)
  need complementary agent-side test coverage that doesn't exist yet.
- `integration_tests/` — no existing end-to-end coverage of
  auto-issue filing (there is no production caller). A new
  integration test simulating "overseer observes anomaly → files
  issue → verifies dedup" is a likely deliverable.
- `gateway/` tests — if `gh issue create` is newly allowed for the
  overseer role (decision-14), gateway policy tests need an added
  allow case and a matching deny case for non-overseer roles.

If the human picks Option A (one pipeline for all three threads), the
complexity scales up further — `skills/sdlc/SKILL.md` restructuring is
its own large surface. Complexity remains **high** either way.
