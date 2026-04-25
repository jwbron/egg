# Analysis: Improve overseer escalation/issue opening behavior (advisor-strategy framing)

> Issue: #1962 | Phase: refine

## Problem Statement

The overseer agent is "pretty good" at escalating to humans via
`OVERSEER_ALERT`, but the issue author flags three coupled gaps:

1. **Escalation reliability** — the deployed agent-side overseer
   (`sandbox/agent-config/rules/overseer.md`) does not always escalate
   when it should. Tightening *"when appropriate"* is part of the ask.
2. **Autonomous issue filing** — when the overseer observes a real egg
   bug (not just a flaky agent), it should file a GitHub issue
   automatically. Today the agent-side overseer rules at
   `sandbox/agent-config/rules/overseer.md:206` explicitly forbid it
   (*"the human will file it -- you do not file issues yourself"*),
   so this behavior is policy-blocked even though the underlying
   plumbing exists in dead code at `orchestrator/overseer/issue_filer.py`.
3. **Host → overseer migration** — `/sdlc` (`skills/sdlc/SKILL.md`)
   currently does mid-pipeline debugging (stall detection, agent
   nudging, NACK escalation, 60-min long-run rescue, etc.). The issue's
   desired end state is that the host *"does nothing other than report
   what's going on in the pipeline based on events sent to it (after
   #1932) and manage HITL decisions"*. All investigative and nudge
   logic should migrate into the overseer.

The issue body also mentions overseer-launched investigator sub-agents
as a *speculative* future capability; this is tracked in #2000 and is
explicitly out of scope here.

### Pre-refine framing (must read)

A `/sdlc` triage on 2026-04-24 reframed this issue under the
[**advisor strategy**](https://claude.com/blog/the-advisor-strategy)
(Anthropic launched it on 2026-04-09): keep a small/fast model as the
**executor** that drives the loop, and call a heavier-tier model as
the **advisor** only when the executor flags a candidate. The
existing overseer architecture maps onto this naturally:

- **Haiku tier** (executor): `model="haiku"`, `max_turns=1` — runs
  every cycle, classifies anomalies (stall / error / loop /
  alignment). Already deployed at `sandbox/agent-config/rules/overseer.md:87-93`.
- **Sonnet/Opus tier** (advisor): runs only when Haiku says "this
  might be an alert" — decides escalation level, alert composition,
  and (new) issue-filing. Already partly deployed at
  `sandbox/agent-config/rules/overseer.md:95-102`.

The advisor lens turns the auto-issue question into *"Opus, should
this Haiku-flagged anomaly become a GitHub issue?"* with `max_uses` +
the existing `overseer_max_cycles_before_re_alert` bounding cost.

The pre-refine notes also locked in these resolved preferences (carry
forward as constraints, no longer open questions):

| Topic | Resolved value | Source |
|---|---|---|
| Pipeline scope | All three threads in this pipeline (escalation tuning + auto-issue + host migration) | pre-refine notes; supersedes old `decision-1` (Option A) and old `decision-11` (host migration in scope) |
| Auto-filing policy | Advisor-gated per-anomaly rubric (Opus only on Haiku flag) | pre-refine notes; matches old `decision-4` opt-3 |
| Labels | Existing `agent:overseer` + matching `p0`/`p1`/`p2`/`p3` priority label. **No new labels** (no `egg:diagnostic`, `pipeline-health`, `overseer-alert`, `overseer-opened`) | pre-refine notes; supersedes all options of old `decision-7` |
| Dedup window | Per-repo: search open issues by `agent:overseer` label + anomaly-type signature before filing | pre-refine notes; matches old `decision-5` opt-2 |
| Sub-agent launching | Out of scope; tracked in #2000 | pre-refine notes; matches old `decision-2` opt-1 |
| Related-bugs scope | #1722 / #1727 stay in their own pipelines, link as context | pre-refine notes; matches old `decision-3` opt-1 |

The advisor framing is the **starting point**, not a mandate — if a
better architecture surfaces, swap it in. The resolved preferences
above hold regardless.

## Current Behavior

### Two coexisting overseer implementations

The codebase contains **two** things named "overseer":

1. **Sandbox LLM overseer (deployed, in production today)**
   - Spawned per phase by `orchestrator/routes/pipelines.py` →
     `spawn_overseer_container` (`orchestrator/kubernetes_spawner.py`).
   - Runs as a Claude Code agent inside a sandbox container with the
     prompt rules at `sandbox/agent-config/rules/overseer.md`.
   - Polls by repeatedly invoking
     `python3 /opt/egg-runtime/sandbox/overseer_monitor.py --once`
     (`sandbox/overseer_monitor.py`, 314 LOC).
   - Escalates **only** via `egg-orch overseer alert` (CLI wrapper).
   - **Cannot file GitHub issues** — explicitly forbidden at
     `overseer.md:206`.
   - Already implements a Haiku-classify / Sonnet-decide split
     described in the rule doc (`overseer.md:83-102`).
   - Phase-scoped lifetime: `overseer.md:5` — fresh instance per
     phase, no accumulated state.

2. **Orchestrator-side `OverseerMonitor` class (dead code in
   production)**
   - `orchestrator/overseer/monitor.py` (2005 LOC) plus
     `classifier.py` (341 LOC), `decision_maker.py` (249 LOC),
     `issue_filer.py` (204 LOC), `self_monitor.py`.
   - `decide_corrective_action`
     (`orchestrator/overseer/decision_maker.py:99-155`) defines a
     ladder: `nudge → redirect → restart_agent → hitl → restart_phase
     → issue → slack`.
   - The `issue` action calls `file_diagnostic_issue`
     (`orchestrator/overseer/issue_filer.py:111-204`), which runs
     `gh issue create` with labels `["egg:diagnostic", "pipeline-health"]`
     and a `## Pipeline Diagnostic:` template
     (`issue_filer.py:86-107`).
   - `OverseerMonitor(` is referenced only in `orchestrator/tests/`;
     `file_diagnostic_issue(` has one caller at
     `orchestrator/overseer/monitor.py:624` (inside the dead class
     itself) plus tests — no production instantiation.
   - Labels `egg:diagnostic` / `pipeline-health` do **not** exist in
     the repo (`gh label list --repo jwbron/egg`); the only relevant
     label that does exist is `agent:overseer`. Priorities `p0`–`p3`
     also exist.

The infrastructure for autonomous issue filing was built for an
overseer architecture that was later superseded by the LLM-agent
design. The new design never wired up the capability.

### Today's escalation surface

The deployed overseer's only remediation verb is
`OVERSEER_ALERT`. Triggers enumerated at `overseer.md:171-180`:

- `stuck-phase-transition` (BRC confirmed but no advance in ~60s)
- `orchestrator-consensus-silent`
- `unauthorized-overseer-action` (repeated 401)
- `agent-heartbeat-stall` (gated on Tier-1 health alert per #2012)
- `agent-loop` (Haiku confidence >0.8 across two consecutive cycles)
- Re-alert on same anomaly across `overseer_max_cycles_before_re_alert`
  (default: 3) cycles, priority bumped one level

Allowed peripheral actions (`overseer.md:130-140`): low-stakes peer
STATUS message for clarification; resolve own health alerts; hand off
to mediator. Forbidden: phase advancement, decision creation,
consensus mutation, container spawn, **issue filing**.

### Host-side `/sdlc` logic that thread-3 would migrate

`skills/sdlc/SKILL.md` (cited inline by section/line):

- **Per-role phase-stall detection**: 3 min (`SKILL.md:489-494`)
- **Silent-agent detection**: 10 min in `running_agents` with zero
  messages (`SKILL.md:496-508`)
- **NACK escalation**: 3 min unresolved (`SKILL.md:531-553`)
- **Post-nudge escalation**: 3 min after a nudge
  (`SKILL.md:555-566`)
- **Long-running phase detection**: 60 min implement-phase
  (`SKILL.md:572-596`)
- **Stuck pipeline rescue**: open draft PR / cancel / retry
  (`SKILL.md:598-639`)
- **AskUserQuestion side flows** ("Check agent logs" / "Nudge agent" /
  "Restart pipeline") — multiple call sites
- **In-memory state map**:
  `{role: {phase, phase_entered_at, nudged_at, first_seen_at, has_any_messages}}`
  (`SKILL.md:568-570`)
- Phase-5 short-flow copies (`SKILL.md:1359-1383`) duplicate stall +
  NACK detection

After **#1932 closed** (server-side event plumbing via #1919) and
**#1971 merged** (host-side `wait_for_status_change` MCP tool), the
host can already wake event-driven on `OVERSEER_ALERT` /
`PHASE_STARTED` / `PHASE_COMPLETED` / `DECISION_CREATED` /
`PIPELINE_*` / `CONSENSUS_*`. The plumbing required for "host is a
pure reporter" is therefore in place — what remains is *moving the
detection logic*.

### Existing related infrastructure that overlaps

- **Tier-1 deterministic tripwires** in
  `orchestrator/health_monitor.py` (heartbeat timeouts, container
  exits, repeated errors, progress stalls). Their output becomes
  health alerts the agent-side overseer reads through
  `query_health_alerts`. The recent #2012 fix gated
  `agent-heartbeat-stall` on a corresponding Tier-1 alert — the
  pattern of *"only escalate when Tier-1 has already tripped"* is
  load-bearing for the auto-issue policy too.
- **Tier-2 classifier / decision-maker** exists in
  `orchestrator/overseer/{classifier,decision_maker}.py` but lives
  inside the dead `OverseerMonitor` class. Some of this prompt logic
  is candidate for migration into agent-side prompts.
- **`egg-orch overseer alert`** — the dedicated CLI for
  `OVERSEER_ALERT` emission
  (`sandbox/bin/egg-orch:2549-2597`, subparser declared at 2553,
  `alert` parser at 2556). No corresponding
  `egg-orch overseer file-issue` today.
- **`OVERSEER_PATTERNS`** in `shared/egg_restrictions/patterns.py:522-545`
  — file-boundary policy: only `.egg-state/oversight/` and
  `.egg-state/agent-outputs/` writeable.

### Existing config knobs (`orchestrator/models.py:343-389`)

```
overseer_enabled: bool = True
overseer_poll_interval_seconds: int = 30
overseer_max_redirects_before_escalation: int = 2
overseer_decision_maker_model: str = "sonnet"   # ← advisor model lever
overseer_max_turns: int                         # SDK turn cap
overseer_max_respawns: int
overseer_rerun_min_work_seconds: int = 60
overseer_hitl_propagation_timeout_seconds: int = 300
```

Plus the rule-doc-level cost knob: `max_llm_cost_per_hour` ($5.00
default per `overseer.md:214`).

### Anthropic advisor tool — capability check

The advisor strategy launched 2026-04-09 as a beta API feature:

- Beta header: `anthropic-beta: advisor-tool-2026-03-01`
- Tool type: `advisor_20260301`
- Executor: Haiku 4.5 or Sonnet 4.6; advisor: Opus 4.6
- `max_uses` parameter caps consultations per request
- Anthropic publishes BrowseComp results: Haiku + Opus advisor scored
  41.2% vs. Haiku-solo 19.7%, at 85% lower per-task cost than Sonnet
  solo (per Anthropic's launch announcement at
  <https://claude.com/blog/the-advisor-strategy>; numbers cited in
  prose only — verify against the live blog post before quoting in a
  PR description)

Sandbox agents call models via `egg_agent.client.run_agent_async`
(`shared/egg_agent/client.py:65-203`), which delegates to
`claude-agent-sdk`'s `query()`. **It is unverified whether the
currently-vendored `claude-agent-sdk` exposes the
`advisor_20260301` tool type or the `max_uses` parameter.** A
plan-phase capability spike resolves this in seconds via
`pip show claude-agent-sdk` plus `python -c "from claude_agent_sdk
import ...; help(...)"` introspection. This matters for option
selection in the recommended approach below — see **Option A** vs.
**Option B** below.

### Interaction with other in-flight issues

- **#1932** — closed; event-driven host wait shipped via #1919.
- **#1971** — merged; `wait_for_status_change` MCP tool lives.
- **#1806** (p2) — overseer vs. `deployment-diagnose` /
  `agent-diagnose` skills overlap. Auto-issue policy here will
  influence its scope.
- **#1786** (p2) — per-role PATH restriction. Any new
  `egg-orch overseer file-issue` verb (or MCP tool) needs gateway
  policy aligned with this.
- **#1902** (p3) — overseer file-boundary policy. `OVERSEER_PATTERNS`
  already permits `.egg-state/oversight/` and
  `.egg-state/agent-outputs/`
  (`shared/egg_restrictions/patterns.py:526-527`). Concretely: dedup
  storage option `decision-6` opt-2 (`.egg-state/oversight/filed-
  issues.json`) requires **zero** file-boundary work; only opt-3
  (orchestrator REST endpoint) or a non-`.egg-state/oversight/` local
  store would need a `OVERSEER_PATTERNS` change.
- **#1722** (p1) — overseer misdiagnoses deadlock after phase
  restart due to stale `AGENT_FAILED`. False-positive case directly
  relevant to escalation tuning.
- **#1727** (p3) — overseer exits with code 0 early in fresh
  pipeline. Another false-positive class.
- **#2000** — overseer-launched investigator sub-agents (deferred,
  blocked on this issue).
- **#2012** — recent fix that gated `agent-heartbeat-stall` on
  Tier-1 signal. The "gate the heavy decision on a deterministic
  signal first" pattern is a precedent for the advisor gate here.

## Constraints

- **Scope-of-action.** The overseer cannot mutate pipeline lifecycle
  state (`overseer.md:11-28`). A new "file issue" action sits at the
  edge of this boundary: it writes to GitHub, not to the pipeline,
  but dedup / race conditions between the overseer and a human filing
  the same issue need a clear policy.
- **Phase-scoped lifetime.** Each phase gets a fresh overseer
  (`overseer.md:5`); any cross-phase memory ("I already filed issue
  #X for this anomaly") must live outside the sandbox container.
  This makes per-pipeline / per-repo dedup search the only viable
  primary mechanism.
- **File-boundary policy.** `OVERSEER_PATTERNS`
  (`shared/egg_restrictions/patterns.py:522-545`) only allows writes
  to `.egg-state/oversight/` and `.egg-state/agent-outputs/`.
- **Gateway policy.** The overseer calls `gh` via the gateway
  (`gateway/`). `gh issue create` is allowed for refiners (decisions)
  and coders (PRs), but the overseer role currently has no policy for
  it. The gateway needs an explicit allow rule + label injection +
  rate limit before this ships.
- **Cost budget.** `max_llm_cost_per_hour=$5.00` default
  (`overseer.md:214`). Adding an advisor (Opus) call must fit inside
  this envelope, with explicit `max_uses` per phase / per pipeline.
- **Human-trust.** Auto-filing noise creates triage toil. Bias must
  be conservative: when in doubt, alert but don't file.
- **Inversion of control with `/sdlc`.** Migrating
  stall/NACK/log/long-run logic into the overseer means the host
  stops *deciding* "check the agent logs" — the overseer must do it
  proactively and put the findings in `OVERSEER_ALERT --detail`.
  This changes alert payload shape and `/sdlc`'s surfacing logic.
- **Carry-over preferences (locked).** See the table in
  *Pre-refine framing* — scope, labels, dedup window, sub-agent
  scope, related-bugs scope, advisor-gated policy are all already
  resolved.
- **SDK capability uncertainty.** Whether the vendored
  `claude-agent-sdk` exposes the `advisor_20260301` tool natively is
  unverified. If it doesn't, the executor → advisor handoff has to be
  implemented as two separate `run_agent_async` calls (Haiku
  classify, then Sonnet/Opus decide on Haiku's flag) rather than via
  the native advisor tool. This is a plan-phase implementation
  detail but it changes effort sizing.
- **Recent calibration.** PR #2011 (Fix #2010) just calibrated the
  overseer prompt for refine-phase false positives, and PR #2016
  (Fix #2012) gated `agent-heartbeat-stall` on Tier-1 signal. The
  changes here must layer on top of those, not regress them.

## Options Considered

The advisor framing is the starting point. The fork is *how to
implement the executor → advisor handoff* and *how aggressively to
ship the host migration*.

### Option A: Native advisor tool (if SDK supports it)

**Approach**: Use the Anthropic beta `advisor_20260301` tool inside
a single `run_agent_async` call. Haiku 4.5 drives the cycle; the
advisor (Opus 4.6) is invoked by the executor when it needs deeper
reasoning. Set `max_uses` to bound the budget. Auto-issue filing is
gated on the advisor's verdict via a tool-call result the executor
acts on.

**Pros**:
- Single API request — Anthropic handles context routing; no extra
  round trips.
- Shared context: advisor sees everything the executor sees, no
  manual prompt construction.
- Anthropic-published cost data: Haiku + Opus-advisor is 85% cheaper
  per task than Sonnet solo on BrowseComp.
- Idiomatic per Anthropic's launch guidance.

**Cons**:
- Requires the vendored `claude-agent-sdk` to support
  `advisor_20260301` and `max_uses`. **Unverified** — needs a
  capability spike in plan phase.
- Beta API; behavior may shift.
- Couples the overseer to a specific Anthropic-hosted feature; if
  Anthropic deprecates, we're back to manual two-call orchestration.
- Less fine-grained control over *when* the advisor is invoked
  (executor decides) — the gate must be in the executor's prompt,
  not in our Python.

### Option B: Two-call advisor pattern (always works)

**Approach**: Keep the existing structure where the overseer rule
doc tells the agent to "call Haiku to classify, then call Sonnet to
decide". For auto-issue filing, the Sonnet decision call is the
advisor: it answers *"is this anomaly worth a GitHub issue right
now?"*. The model knob already exists
(`overseer_decision_maker_model`); add `overseer_advisor_model`
(default Opus 4.6) and `overseer_advisor_max_uses_per_phase` (e.g.,
default 3) for the issue-filing gate. The two calls are separate
`run_agent_async` invocations; we control the prompt contract
explicitly.

**Pros**:
- Zero new SDK dependency — works with what we have today.
- Fully explicit prompt contract: we choose what Haiku hands to
  Opus (raw cycle output, classification only, or a distilled
  summary).
- Easy to budget: `max_uses` is enforced in our Python, not in
  Anthropic's API.
- Easy to feature-flag and shadow-test.

**Cons**:
- Two sequential network round-trips per advisor invocation.
- Context duplication: the advisor prompt has to be constructed
  manually — risk of token waste vs. the native tool's shared-context
  routing.
- We re-implement what the advisor tool offers natively.

### Option C: Hybrid — try Option A, fall back to Option B

**Approach**: Plan-phase capability spike on the SDK. If
`advisor_20260301` is supported, ship Option A; otherwise ship
Option B. The advisor gate (Haiku flag → heavy decision) is the same
contract either way; only the implementation differs.

**Pros**:
- De-risks the SDK uncertainty without locking the design.
- Same observable behavior either way; the choice is a wiring
  detail.

**Cons**:
- Plan phase has a larger spike to absorb.
- Maintenance: we'd need to keep both code paths working if the SDK
  acquires support mid-flight or loses it.

### Option D: Don't use the advisor framing — flip
`overseer_decision_maker_model` to Opus globally

**Approach**: Skip the gate entirely. Make every Sonnet-tier call an
Opus call by default.

**Pros**:
- Trivial to implement (one config default change).
- No new architecture.

**Cons**:
- Runs Opus on every cycle (cost regression — pre-refine notes
  flagged this is the framing the advisor strategy was meant to
  *avoid*).
- Doesn't address the "when to file an issue" question — that gate
  is what the advisor framing exists to provide.
- Doesn't match the explicit pre-refine recommendation.

## Recommended Approach

**Recommendation: Option C (hybrid) — capability-spike the SDK in
the plan phase, then ship Option A (native advisor tool) if
supported, otherwise ship Option B (two-call pattern).** The
*observable* contract is identical:

- Haiku 4.5 (already deployed) classifies every cycle with
  `max_turns=1`.
- On Haiku flag, invoke Opus 4.6 advisor with the classification
  result + a compact context bundle. Bound by `max_uses` per phase.
- Advisor returns one of: *"alert at priority X with body Y"*,
  *"file GitHub issue with template Z"*, or *"keep watching, here's
  why"*.
- Auto-issue filing is gated on a `decision: "file"` verdict from
  the advisor.
- Dedup: before filing, search open issues with `agent:overseer` +
  matching anomaly-type signature; skip if a live match exists.
- Labels: only `agent:overseer` + matching `p0`/`p1`/`p2`/`p3`.
- Issue body: extend the existing `## Pipeline Diagnostic:` template
  at `orchestrator/overseer/issue_filer.py:86-107` with explicit
  links (pipeline ID, phase, branch, commit SHA, parent
  `OVERSEER_ALERT` message ID) — the dead-code template is the
  closest existing artifact and should be revived rather than
  redesigned.
- Host migration (thread 3): in scope per pre-refine notes —
  migrate stall / silent-agent / NACK / long-running-phase / stuck-
  pipeline-rescue / AskUserQuestion-side-flow / state-map logic out
  of `skills/sdlc/SKILL.md` into the overseer. `/sdlc` keeps:
  surfacing alerts, HITL handling, Phase-5 final-handoff rescue
  prompts.

Rationale for the hybrid:

- The SDK uncertainty is the only remaining technical risk. A spike
  in plan phase is cheap and decisively de-risks the choice.
- Option A is structurally cleaner if available — Anthropic's own
  cost data (Haiku + Opus advisor) is the strongest argument that
  this is the right shape for *"classify cheap, decide expensive
  only on demand"*.
- Option B is a known-good fallback. We already have the two-tier
  prompt structure deployed; Option B is incremental.
- Option D was explicitly considered and rejected by the pre-refine
  notes for cost reasons.

For the plan / implement phases (sized for sequencing only — not
prescribed here):

- **Plan phase deliverables:** SDK capability check; advisor budget
  knobs; `egg-orch overseer file-issue` (or MCP tool) surface;
  dedup spec; gateway policy spec; rule-doc rewrite outline; host-
  migration sequencing (single PR vs. layered); regression-test
  plan.
- **Implement phase deliverables:** wire the chosen advisor pattern;
  rule-doc edits to `overseer.md` (lift the issue-filing prohibition,
  add advisor + dedup rules); wire `file_diagnostic_issue` (or its
  CLI/MCP equivalent) into the agent-side path; gateway allow-rule;
  GitHub label work (none — pre-refine resolved); host migration in
  `skills/sdlc/SKILL.md`; tests across `orchestrator/tests/`,
  `gateway/tests/`, `integration_tests/`, `sandbox/tests/`.

## Open Questions

The contract carried over **16** `decision-N` items and **7**
`feedback-1.QN` items from the prior refine attempt. The
advisor-strategy framing adds **7 new** decisions (decision-17 …
decision-23), all already registered via
`mcp__sdlc__register_open_question` and verified present via
`mcp__sdlc__show_contract`. Total contract surface for this phase:
**23 decisions + 7 feedback questions**.

The status table below classifies every carry-over item against the
pre-refine framing. Items marked *Resolved by pre-refine* still need
the human's checkbox to formally close them on the contract — the
analysis cannot resolve them itself.

| Item | Status under the advisor framing |
|---|---|
| `decision-1` (scope split) | **Resolved by pre-refine** — Option A (all three threads in this pipeline). Check opt-2. |
| `decision-2` (sub-agent launching) | **Resolved by pre-refine** — Deferred to #2000. Check opt-1. |
| `decision-3` (related bugs #1722/#1727) | **Resolved by pre-refine** — Leave in their own pipelines. Check opt-1. |
| `decision-4` (auto-issue filing policy) | **Resolved by pre-refine** — Advisor-gated per-anomaly rubric. Check opt-3. |
| `decision-5` (dedup scope) | **Resolved by pre-refine** — Per repo. Check opt-2. |
| `decision-6` (dedup state storage) | **Open** — see inline below. |
| `decision-7` (label convention) | **Resolved by pre-refine, none of the listed options match.** Use `decision-17` to confirm "Other: existing `agent:overseer` + priority labels". The cleanest UX path: have the orchestrator resolve `decision-7` as superseded so the human only checks `decision-17`. |
| `decision-8` (issue body template) | **Open** — recommendation opt-2 (extend existing). |
| `decision-9` (who runs `gh issue create`) | **Open** — see inline below. |
| `decision-10` (rollout mode) | **Open** — distinct from `decision-22`. Rollout = "shadow vs. live vs. feature-flag for auto-issue filing". `decision-22` = "host-migration sequencing in this pipeline". They are not the same question. |
| `decision-11` (defer host migration) | **Resolved by pre-refine** — No; migration is in scope. Check opt-2. |
| `decision-12` (`/sdlc` thresholds in migration) | **Open** (plan-phase). |
| `decision-13` (#1806 coordination) | **Open** (plan-phase). |
| `decision-14` (#1786 coordination) | **Open** (plan-phase). |
| `decision-15` (#1902 `OVERSEER_PATTERNS`) | **Open** (plan-phase). |
| `decision-16` (host-migration timing-state location) | **Open** (plan-phase). |
| `feedback-1` (Q1–Q7) | **Open** — Q1/Q2 directly inform the advisor gate. |

### Carry-over decisions inline (with markers for HITL surfacing)

Each open carry-over is reproduced below with its `<!-- egg-hitl-decision -->` marker so the host-side HITL surface can pair prose with the contract decision ID.

<!-- egg-hitl-decision id=decision-6 -->

**Dedup state storage: where does the overseer remember issues it
has already filed?**

- [ ] In-process only — each phase's overseer starts fresh; rely on GitHub search each time
- [ ] Persisted to `.egg-state/oversight/filed-issues.json` so respawns/new phases remember (Recommended — zero file-boundary work; `OVERSEER_PATTERNS` already permits this prefix)
- [ ] Persisted via orchestrator REST endpoint (central store); overseer asks "have I filed this?" before filing
- [ ] Hybrid — persist locally AND verify via GitHub search before filing
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

Design note: opt-1 (agent-side CLI verb) is the most agent-mode-
friendly per the agent-mode design guide — the agent acts via the
gateway, which already mediates `gh`. opt-2 (orchestrator REST
endpoint) introduces an extra hop where the server parses agent
intent and acts on its behalf. opt-3 (hybrid) is borderline. opt-4
(reuse the dead-code path by instantiating `OverseerMonitor` in
production) re-introduces an orchestrator-side classifier pipeline
the advisor framing is trying to simplify and is **not recommended**.

- [ ] Agent-side overseer runs it in its sandbox via a new `egg-orch overseer file-issue` CLI verb (Recommended — most agent-mode-friendly)
- [ ] Orchestrator-side — new REST endpoint; server runs `gh` with its own credentials; overseer POSTs payload
- [ ] Hybrid — overseer composes body, orchestrator files it; orchestrator enforces central rate-limit / dedup policy
- [ ] Reuse the existing dead-code `file_diagnostic_issue` path by instantiating `OverseerMonitor` in production — **not recommended**
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

<!-- egg-hitl-decision id=decision-12 -->

**`/sdlc` wall-clock thresholds (3 min stall, 10 min silent agent,
60 min long-running phase): when migrated to the overseer, should
the thresholds change?** *(Plan-phase candidate — may be left
unanswered at the refine gate.)*

- [ ] Keep identical — same numeric thresholds, just moved across the process boundary
- [ ] Make configurable per-pipeline via PipelineConfig (default to current values) (Recommended)
- [ ] Revise numbers as part of migration — human to specify in plan phase
- [ ] Defer until the migration pipeline
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-13 -->

**Coordination with #1806 (overseer vs. `deployment-diagnose` /
`agent-diagnose` skills overlap)?** *(Plan-phase candidate.)*

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
- [ ] Add a gateway allowlist rule now, defer PATH restructuring to #1786 (Recommended)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-15 -->

**Coordination with #1902 (overseer file-boundary policy): expand
`OVERSEER_PATTERNS` to allow writes under `.egg-state/oversight/`
for filed-issue dedup state?**

Implementation note: `OVERSEER_PATTERNS` already permits both
`.egg-state/oversight/` and `.egg-state/agent-outputs/`
(`shared/egg_restrictions/patterns.py:526-527`). If `decision-6`
resolves to opt-2 (`filed-issues.json` under `.egg-state/oversight/`)
this is a no-op.

- [ ] Yes — expand `OVERSEER_PATTERNS` allowlist to include the chosen dedup state file
- [ ] No — use an orchestrator-side store instead; overseer never writes dedup state locally
- [ ] Defer — decide during plan phase based on the chosen dedup-storage option (`decision-6`) (Recommended — downstream of `decision-6`)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-16 -->

**Where does the cross-cycle agent-timing state live (currently
`/sdlc`'s in-memory map of `{role: {phase, phase_entered_at,
nudged_at, first_seen_at, has_any_messages}}`)?** *(Plan-phase
candidate; resolved-by-pre-refine `decision-11` puts host migration
in scope, so this is now genuinely open.)*

- [ ] In the overseer only — phase-scoped, lost on phase transition / overseer respawn
- [ ] In the orchestrator's `health_monitor` — persistent across phases / respawns
- [ ] In `.egg-state/oversight/agent-timing.json` — persistent, phase-scoped, overseer-owned (Recommended — overseer-owned, no orchestrator schema change)
- [ ] N/A — thread 3 deferred (no longer applies; `decision-11` resolved opposite)
- [ ] Other (explain in reply)

### New advisor-strategy decisions (decision-17 … decision-23)

Seven new decisions, registered via
`mcp__sdlc__register_open_question` and verified present on the
contract via `mcp__sdlc__show_contract`. Each is reproduced inline
with its marker.

<!-- egg-hitl-decision id=decision-17 -->

**Confirm the resolved label preference for overseer-filed issues:
use the existing `agent:overseer` label + the matching priority
label (`p0`/`p1`/`p2`/`p3`) only, with NO new labels (no
`egg:diagnostic`, `pipeline-health`, `overseer-alert`,
`overseer-opened`)?** *(Pre-refine notes locked this in; this
decision exists because none of `decision-7`'s options match.)*

- [ ] Confirm — `agent:overseer` + priority label only, no new labels (Recommended — matches pre-refine)
- [ ] Override — also create new labels (specify in 'Other')
- [ ] Override — different label set entirely (specify in 'Other')
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-18 -->

**Advisor trigger calibration: what signal(s) should gate
invocation of the heavy-tier (Opus) advisor inside the overseer's
existing Haiku-classify loop?** *(The advisor strategy bounds cost
by only calling Opus when Haiku flags a candidate.)*

- [ ] Haiku confidence > threshold (e.g., 0.8) on any anomaly classification
- [ ] Specific anomaly types only (e.g., agent-loop, persistent-error, stuck-phase-transition) regardless of confidence
- [ ] N consecutive cycles with the same Haiku flag (e.g., 2 of 2) — matches existing `agent-loop` precedent
- [ ] Intersection: Haiku confidence > threshold AND Tier-1 health alert present (Recommended — matches the #2012 gate pattern)
- [ ] Defer to plan phase (spike calibration data first)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-19 -->

**Advisor budget: what cap should bound Opus advisor calls (and how
should it interact with the existing `max_llm_cost_per_hour=$5.00`
budget at `sandbox/agent-config/rules/overseer.md:214`)?**

- [ ] Per-phase cap: e.g., `overseer_advisor_max_uses_per_phase=3` (Recommended — mirrors `overseer_max_cycles_before_re_alert` precedent)
- [ ] Per-pipeline cap: aggregate across all phases of the pipeline (e.g., 5)
- [ ] Daily cap: per overseer-role consumer-id per UTC day (e.g., 50)
- [ ] Hybrid: per-phase floor + dollar-budget ceiling (cap stays inside the existing `max_llm_cost_per_hour`)
- [ ] Defer to plan phase — measure first, set cap second
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-20 -->

**Prompt contract: what exactly does the executor (Haiku) hand the
advisor (Opus) when it invokes?** *(This affects token cost AND
advisor accuracy. Reviewer-flagged agent-mode lens: prefer
"classification + a pointer to where the advisor can fetch more"
over a fully pre-digested summary that constrains the advisor's
exploration.)*

- [ ] Raw cycle output: the full JSON line from `overseer_monitor.py --once` plus container logs snapshot
- [ ] Classification result only: just the Haiku verdict (type, confidence, reasoning) (Recommended — leaves the advisor free to fetch more via existing tools)
- [ ] Distilled summary: classification + last N progress events + active health alerts + last K log lines
- [ ] Native `advisor_20260301` shared-context (Anthropic auto-routes; only viable if Option A on `decision-23`)
- [ ] Defer to plan phase (spec depends on Option A vs B on `decision-23`)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-21 -->

**Auto-issue gate placement: where does the final "file an issue
Y/N" decision live?**

- [ ] Advisor decides directly: Opus returns `file=true|false`; overseer files immediately if true (subject to dedup + budget)
- [ ] Advisor recommends: Opus returns `recommendation=file_issue` inside an `OVERSEER_ALERT`; the existing alert flow surfaces it; the human gates the actual filing (Recommended for shadow / feature-flag rollouts; ties to `decision-10`)
- [ ] Advisor decides for a subset (e.g., systemic / repeated anomalies); for novel anomalies, the advisor recommends only and the human gates
- [ ] Defer to plan phase — ties together with rollout (`decision-10`)
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-22 -->

**Host-migration sequencing within this pipeline (host → overseer
migration is in scope per pre-refine notes): should the implement
phase ship one PR with everything, or split?**

- [ ] One PR — architecture change + auto-issue + host migration land together (highest review cohesion, biggest review burden)
- [ ] Two PRs — (1) advisor + auto-issue, (2) host migration (Recommended — smaller per-PR diff, clear story per PR)
- [ ] Three PRs — (1) advisor wiring + escalation tuning, (2) auto-issue filing, (3) host migration (smallest per-PR diff; coordination overhead)
- [ ] Defer the sequencing decision to the planner once full scope is known
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-23 -->

**Native `advisor_20260301` tool vs. two-call pattern: which
implementation should the executor → advisor handoff use?** *(See
Recommended Approach: Option A = native advisor tool, Option B =
two `run_agent_async` calls, Option C = capability-spike then
choose.)*

- [ ] Option C (Recommended) — capability-spike vendored `claude-agent-sdk` in plan phase; if `advisor_20260301` is supported, ship Option A; else ship Option B
- [ ] Option A — commit now to the native advisor tool; require SDK upgrade if needed
- [ ] Option B — commit now to the two-call pattern; ignore the native tool
- [ ] Defer to plan phase entirely — don't even pre-spike
- [ ] Other (explain in reply)

### Open-ended feedback (`feedback-1`)

The 7 carry-over questions remain open. Refiner-paraphrased
headlines:

<!-- egg-hitl-feedback id=feedback-1.Q1 -->

- **Q1** — Which specific escalation triggers in
  `sandbox/agent-config/rules/overseer.md:171-180` need tightening?
  Concrete false-positive / false-negative patterns to fix
  (#1722-style stale `AGENT_FAILED`, long legitimate test runs
  flagged as stalls, specific 401 patterns).

<!-- egg-hitl-feedback id=feedback-1.Q2 -->

- **Q2** — Baseline thresholds (~60 s `stuck-phase-transition`,
  Haiku loop-detection confidence > 0.8 across 2 consecutive cycles
  for `agent-loop`, `overseer_max_cycles_before_re_alert=3`) — are
  any of these wrong, and what should they be?

<!-- egg-hitl-feedback id=feedback-1.Q3 -->

- **Q3** — Per-pipeline cap on auto-filed issues (e.g., 1 / phase,
  3 / pipeline) beyond which the overseer escalates via HITL
  instead?

<!-- egg-hitl-feedback id=feedback-1.Q4 -->

- **Q4** — Gateway constraints on `gh issue create` from the
  overseer role (label injection, title/body size limits, rate
  limit, allowed repos only).

<!-- egg-hitl-feedback id=feedback-1.Q5 -->

- **Q5** — With host migration in scope (`decision-11` resolved
  No), what stays in `/sdlc`? (Specifically: which
  `AskUserQuestion` options like "Check agent logs" / "Nudge agent"
  / "Restart pipeline" become overseer-initiated vs. host-owned?)

<!-- egg-hitl-feedback id=feedback-1.Q6 -->

- **Q6** — Success criteria for "the overseer is now escalating
  well" — what observable signal? (Zero repeat same-anomaly alerts
  per pipeline, > X% of `OVERSEER_ALERT`s acknowledged, auto-filed
  issues accepted at > Y rate.)

<!-- egg-hitl-feedback id=feedback-1.Q7 -->

- **Q7** — Any additional constraints (GitHub App vs. `gh` CLI,
  required approvers for auto-filed issues, notification routing).

### Why these are the load-bearing ones

The pre-refine notes flagged six "actually-open questions under the
advisor framing": *advisor calibration, budget, prompt contract,
gate placement, rollout, host-migration sequencing.* Mapping (each
maps to exactly one decision; rollout and host-migration sequencing
are distinct questions, not folded together):

| Pre-refine load-bearing item | Decision |
|---|---|
| Advisor trigger calibration | `decision-18` |
| Advisor budget | `decision-19` |
| Prompt contract | `decision-20` |
| Auto-issue gate placement | `decision-21` |
| Rollout | `decision-10` (carry-over, still open) |
| Host-migration sequencing | `decision-22` |

`decision-23` (native vs. two-call) is added because the SDK-
capability question shapes implementation and is the one piece of
new technical uncertainty the advisor framing introduces.
`decision-17` is a sanity-check: the contract still has
`decision-7` with three options that are *all* wrong per pre-
refine; rather than relying on the human to pick "Other", a
confirming decision makes the resolved preference explicit.

## Complexity Assessment

**Complexity: high.**

Even with the advisor framing reducing architectural ambiguity, the
in-scope surface (all three threads, per pre-refine) is large:

- Agent-side rule rewrite (`sandbox/agent-config/rules/overseer.md`,
  ~250 lines today; includes lifting the issue-filing prohibition
  and adding advisor / dedup / migrated-from-host trigger rules)
- Agent-side monitor script
  (`sandbox/overseer_monitor.py`, 314 LOC) if new data needs to
  flow (e.g., proactive `get_container_logs` queries)
- New CLI verb `egg-orch overseer file-issue` in
  `sandbox/bin/egg-orch` — or a corresponding MCP tool, depending on
  decision-9
- Dead-code revival + wiring of `orchestrator/overseer/issue_filer.py`
  (template extension per decision-8)
- Possible new orchestrator REST endpoint (decision-9 opt-2/opt-3)
- Gateway policy for `gh issue create` from overseer role
  (`gateway/agent_restrictions.py`, gateway tests)
- File-boundary tweak in `shared/egg_restrictions/patterns.py` if
  dedup state lives on disk (decision-15)
- Config additions in `orchestrator/models.py`
  (`overseer_advisor_model`, `overseer_advisor_max_uses_per_phase`,
  possibly `overseer_auto_file_issues` flag for rollout)
- **Host migration** in `skills/sdlc/SKILL.md` — moving stall /
  silent-agent / NACK / long-run / rescue logic out of the host;
  this is the single biggest LOC item
- Documentation updates across
  `docs/guides/pipeline-health-monitoring.md`,
  `docs/reference/agent-roles.md`,
  `docs/architecture/orchestrator.md`, plus the rule-doc itself

**Tests / regression risk** (sized so the planner can plan for them):

- `orchestrator/tests/test_overseer_monitor.py` — exercises the dead-
  code `OverseerMonitor`. If decision-9 keeps that path dead, these
  tests need pruning or marking reference-only to avoid drift.
- `orchestrator/tests/test_overseer_issue_filer.py` — directly tests
  the dead-code `_build_issue_body`. Template changes (decision-8)
  land here.
- `orchestrator/tests/test_overseer_alert_isolation.py`,
  `test_overseer_hitl_integration.py`,
  `test_two_tier_integration.py`,
  `test_infra_error_escalation.py` — all reference `OverseerMonitor`
  and need updating for any policy changes that alter the action
  vocabulary.
- `gateway/tests/` — new allow case for overseer-role `gh issue
  create`; matching deny case for non-overseer roles.
- `sandbox/tests/` — new agent-side coverage for the
  `egg-orch overseer file-issue` verb (or MCP tool).
- `integration_tests/` — no end-to-end coverage of auto-issue
  filing today (no production caller). A new integration test
  ("overseer observes anomaly → advisor approves → file → verify
  dedup on next cycle") is a likely deliverable.
- `/sdlc` skill regression — the existing thresholds (3 min stall,
  10 min silent agent, 60 min long-run) have a long history of
  calibration; the migration must preserve current behavior at first
  (decision-12 opt-1 or opt-2) and let the human revisit numbers
  after.

**Parallelism for the implement phase.** Three reasonable parallel
tracks emerge: (a) agent-side rule + advisor wiring + auto-issue
plumbing, (b) gateway + config + dedup state, (c) host migration.
Tracks (a) and (c) touch the rule doc; serialize the rule edits or
plan a merge.

The complexity stays at **high** under either Option A or Option B
(the SDK choice is a wiring detail, not a scope change). It would
**not** be reduced by deferring host migration — the auto-issue +
escalation tuning portion alone is already cross-cutting, and the
pre-refine notes lock host migration in scope.

---

*Authored-by: egg*
