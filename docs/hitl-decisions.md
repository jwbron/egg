# HITL (Human-In-The-Loop) Decision Workflow

This document explains how human decisions are captured and processed in the SDLC pipeline.

## Overview

The SDLC pipeline includes phases where human input is required before proceeding:
- **Refine phase**: Human approves the analysis before planning
- **Plan phase**: Human approves the implementation plan before coding

Four mechanisms exist for gathering human input:
1. **Formal HITL decisions** — Multiple-choice questions with checkboxes
2. **Feedback comments** — Open-ended questions in an editable comment
3. **Phase approval** — Single checkbox to approve and advance to the next phase
4. **Orchestrator-emitted decisions** — Automatically created by the orchestrator during pipeline recovery scenarios (e.g., sync divergence); require operator acknowledgment before the pipeline can continue. See [Orchestrator-Emitted Decisions](#orchestrator-emitted-decisions).

In prompt-driven mode, decisions carry a `decision_type` field (`phase_gate`, `choice`, or `feedback`) that drives type-specific terminal rendering. The orchestrator's decision queue supports a "request changes" option at phase gates. Revision rounds are **not capped** (#3392): the converge-before-advance loop re-runs the phase after each round of resolved decisions and only advances once a round resolves nothing new, so refine/plan never force-advance with feedback or decisions unaddressed. `max_hitl_review_cycles` (default 3) is now the threshold at which a non-fatal overseer non-convergence alert is surfaced, not a force-advance budget. See [Prompt-Driven Mode: Type-Aware Terminal Rendering](#prompt-driven-mode-type-aware-terminal-rendering) and [Converge-Before-Advance Gate](#converge-before-advance-gate) for details.

**Decision sync to contract**: Resolved decisions made during refine and plan phases are automatically synced to the contract (`.egg-state/contracts/{identifier}.json`) after each phase completes, so implement-phase agents can see substantive choices (database selection, API style, config handling, etc.) made earlier. Plain phase gate approvals (without context) are excluded from sync as they are process control. However, when a human approves a phase gate with additional context or feedback, that context is persisted to the contract and appended to the phase draft file as a `## HITL Resolution` section, so next-phase agents can see the human's guidance. See [SDLC Pipeline Guide § Decision Sync to Contract](sdlc-pipeline.md#decision-sync-to-contract) for details.

## Formal HITL Decisions

Use formal decisions when you need the human to choose between predefined options.

### Creating a Decision

```bash
egg-contract add-decision \
  --question "Which caching strategy should we use?" \
  --options "Redis" "In-memory LRU" "File-based" \
  --format markdown
```

Output:
```markdown
<!-- egg-hitl-decision id=cq-1 -->

**Which caching strategy should we use?**

- [ ] Redis
- [ ] In-memory LRU
- [ ] File-based
- [ ] Other (explain in reply)
```

### How It Works

1. The agent includes this markdown in a GitHub comment
2. The `<!-- egg-hitl-decision id=... -->` marker identifies the decision
3. When the human checks a checkbox, the orchestrator's decision queue detects the change
4. The decision is resolved and the contract is updated
5. If this was the last pending decision, the pipeline advances to the next phase

> **Phase completion is gated on resolved decisions.** The `complete_phase` endpoint
> returns 409 if the current phase has any unresolved decisions (both orchestrator-side
> and contract-side decisions scoped to that phase). Resolve all pending decisions before
> completing a phase, or pass `force=true` to abandon them (abandoned IDs are recorded
> in the phase's artifacts for audit). See
> [Orchestrator CLI § complete_phase](reference/orchestrator-cli.md) for details.

### Auto-appended "Other" Option

When you provide `--options`, an "Other (explain in reply)" option is automatically
appended. If the human selects this, they can explain their preference in a follow-up
comment, which the agent will parse.

### Open-ended Questions

For open-ended questions, use a dedicated feedback comment (see next section).

## Feedback Comments

Use feedback comments when you need free-form answers to open-ended questions.

### Creating Feedback

```bash
egg-contract add-feedback \
  --question "What is the expected request volume?" \
  --question "Should we support legacy browsers?" \
  --format markdown
```

Output:
```markdown
<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: What is the expected request volume?**

> _Your answer here_

**Q2: Should we support legacy browsers?**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

---

*Authored-by: egg*
```

### How It Works

1. The agent includes this markdown in a GitHub comment
2. The `<!-- egg-feedback id=... -->` marker identifies the feedback comment
3. The human edits the comment to fill in answers (replacing placeholder text)
4. When the human checks `[x] Submit feedback`, the orchestrator detects the change
5. The feedback is parsed, the contract is updated, and the pipeline resumes
6. The agent receives the feedback in its next invocation

### Key Differences from Decisions

| Aspect | Formal Decisions | Feedback Comments |
|--------|-----------------|-------------------|
| Marker | `<!-- egg-hitl-decision id=... -->` | `<!-- egg-feedback id=... -->` |
| Purpose | Choose between options | Collect free-form answers |
| Input format | Checkboxes | Editable blockquotes |
| Multiple questions | No (one per decision) | Yes (consolidated comment) |
| Workflow job | `handle-decision` | `handle-feedback` |

### Answering contract feedback from the host (`answer_feedback`)

Agents register feedback by writing `contract.feedback` (id `feedback-N`) via
`register_feedback_request` (`mcp__sdlc__request_feedback`). That write touches
only the gateway-backed contract — it is **not** an orchestrator decision. It
is promoted into the orchestrator decision queue only **after the phase_gate is
approved**, by the server-side bridge `_queue_and_await_contract_decisions`
(see [Contract Decision Bridge](#contract-decision-bridge)).

That post-gate promotion is fine for the normal flow (refiner embeds questions
in its draft, operator approves the draft, then answers the deferred
questions). But an agent can also register feedback **pre-proposal** and block
on the answer before producing any draft — e.g. a refiner asking the operator
to supply a goal on an empty contract. No phase_gate is ever reached, so:

- the feedback never appears in `get_status(...).pending_decisions`;
- `provide_input(decision_id="feedback-N", ...)` returns **HTTP 404** (no such
  orchestrator decision);
- the pipeline deadlocks (the overseer emits a `stuck-phase-transition` alert).

The host operator answers this via the **`answer_feedback` MCP tool**, which
posts to `POST /api/v1/pipelines/<pipeline_id>/feedback/answer`
(`orchestrator/routes/decisions.py`). The route writes the answers straight
into `contract.feedback` and marks it submitted — mirroring the bridge's
write-back — so the blocked agent unblocks on its next contract poll:

```
answer_feedback(
  task_id="issue-1059",
  answers={"Q1": "Add retry logic to the API client", "Q2": "p99 < 200ms"},
  feedback_id="feedback-1",   # optional staleness guard
)
```

The endpoint is lifecycle-secret guarded, so only the operator/MCP can answer —
an agent cannot submit answers to its own feedback (parity with decision
resolve; see #1769). Inspect the pending questions first with
`get_contract(task_id).feedback`. See #3007.

### Resolving pre-gate contract HITL decisions (`provide_input` fallback)

Agents register multiple-choice HITL questions on the contract (`cq-N`) via
`mcp__sdlc__register_open_question` or the orchestrator's impasse-escalation
router. Like `feedback-N`, those decisions are bridged into the orchestrator
queue only *after* the phase gate is approved. An agent blocked on such a
question before proposing never reaches the gate, so:

- the decision does not appear in `get_status(...).pending_decisions` (which
  lists only orchestrator-queue decisions);
- calling `provide_input(decision_id="cq-N", ...)` previously returned
  **HTTP 404** (not in the queue), leaving the operator with no resolution
  channel and the pipeline deadlocking (#3071, observed on
  pipeline-c2faf164).

**Surfacing (#3374).** Unresolved `cq-N` HITL decisions that are not yet
bridged into the queue are surfaced in their own `get_status(...)` field,
`pending_contract_decisions` — each entry carries `id`, `question`, `phase`,
`options`, and `scope: "contract"`, plus `type: "hitl"` (mirroring the
queue-decision shape) and a `note` string pointing the operator at the
`provide_input` resolution flow. It is kept distinct from `pending_decisions`
so the two-wave resolve flow is unaffected; an operator driving via
`get_status` no longer has to call `get_contract` out of band to discover them.
Already-bridged questions are filtered out of this field so a mirrored `cq-N`
is not listed twice.

**Consensus-timeout suspension (#3426).** While an unresolved `cq-N` is
tagged to the running phase, the concurrent-mode consensus timeout is
suspended rather than firing — a reviewer withholding its ACK pending this
ruling is expected, not a stall. The clock resets once the decision is
resolved. Only phase-tagged `cq-N` decisions gate — a phase-less or legacy
decision is skipped, so it can never suspend the timeout indefinitely. See
[Concurrent Execution: Timeout Handling](guides/concurrent-execution.md#timeout-handling).

**Deduplication (#3374).** A re-registration of a question already open and
unresolved *under the same phase* adopts the existing `cq-N` rather than
minting a duplicate: `register_open_question` and the impasse-escalation
router both dedupe on the normalized question text **keyed by phase** via
`egg_contracts.decisions.find_duplicate_open_question`. This covers the
observed repro — a re-run agent, or a re-escalated impasse, re-asking within a
single phase — and any re-ask explicitly tagged for that same phase. A genuine
*cross-phase* re-ask (a different phase tag) is treated as a distinct question
and mints a fresh `cq-N` by design, because its decision context differs: in
the agent path the phase defaults to the contract's `current_phase`, so a
question first posed in `refine` and re-posed in `plan` does **not** dedupe.
The dedup key is (question, phase) only — the option set is not part of it, so
a re-registration carrying a *different* option set adopts the stored options
and logs a warning (the new options are not merged). The registration is
idempotent — the response carries `deduped: true` and no second contract
write occurs.

### Append-Only Guard and Write-Time Durability (#3427, #3470)

Contract `decisions[]` entries are append-only: a whole-entry mutation
(`decisions.<idx>`) targeting an index that already exists is rejected with
HTTP 409 (`error_kind="conflict"`) instead of silently overwriting the
existing entry — including a *resolved* one. This closes a TOCTOU window
where two writers (e.g. a re-run agent and a concurrent Layer-C escalation)
mint the same index against a stale read and one clobbers the other's
decision. `register_open_question` treats the 409 as retryable: it re-reads
the contract and re-mints against the fresh `len(decisions)`.

Decision writes (registration and resolution) are also committed and pushed
to the pipeline's work branch at write time, not only at phase/slice
checkpoints. Previously a `cq-N` registered or resolved between checkpoints
lived only on the shared worktree's file; the `git reset --hard
origin/<branch>` that runs at phase-(re)start silently reverted it, letting
the next bootstrap re-mint reuse the same id and clobber an
already-resolved decision. The best-effort commit+push
(`persist_contract_statefiles`, `orchestrator/routes/pipelines.py`) runs
inline with the resolution route
(`orchestrator/routes/decisions/_resolve.py`) and the Layer-C HITL
escalation path (`orchestrator/routes/pipelines.py`) — both calling it
directly — and with the contract mutate route via the
`_persist_durable_mutation` wrapper (`orchestrator/routes/contracts.py`,
#3470), which covers decision registration and contract task-row mutations
alike; a failure there is logged and swallowed since the write is already
live on the worktree file and the next checkpoint commit will pick it up.

The same write-time persist covers contract task-row `status`/`commit`
mutations (`phases.*.tasks.*.status` / `.commit`, #3470): without it, a
task marked `complete` between checkpoints was silently reverted by the
same phase-restart reset, flipping it back to pending and causing the
[Contract Completeness Gate](guides/concurrent-execution.md#contract-completeness-gate-3114)
to re-reject a reviewer's ACK/CONFIRM against work that had already
landed. Unlike the decision path, a failed persist here also broadcasts a
`contract_persist_failed` `OVERSEER_ALERT` (still best-effort — the
mutation itself never fails) since a silently swallowed failure on a task
row reintroduces the same deadlock invisibly.

`provide_input` now falls back to the contract when the id is not found in
the queue. It writes the resolution fields straight onto the contract
(`resolved=True`, `resolved_by="human"`, stripped resolution string), so the
blocked agent unblocks on its next contract poll:

```
provide_input(
  task_id="issue-1059",
  decision_id="cq-1",
  resolution="Resolve the underlying blocker manually",
)
```

The endpoint is lifecycle-secret guarded (parity with queue decisions, #1769),
so agents cannot resolve their own questions.

**Post-gate guard.** Once the server-side bridge has mirrored `cq-N` into the
orchestrator queue as `decision-M`, the pipeline thread is blocked on
`wait_for_decision(decision-M)` with no timeout. Resolving the contract `cq-N`
here would unblock the agent on its next poll but strand the bridge thread
indefinitely. The endpoint detects this via the bridge's context-string
fingerprint and returns HTTP 409 with the mirror id — resolve `decision-M`
instead.

**`feedback-N` is not covered by this fallback.** For open-ended contract
feedback requests, use `answer_feedback` as described above.

See #3071.

## Phase Approval

Phase approval is a simpler mechanism for advancing the pipeline at HITL gates.

### Format

```markdown
### Ready for Review

<!-- egg-phase-approval -->
- [ ] Approve and advance to plan phase

---

*Authored-by: egg*
```

### How It Works

1. The agent includes this at the end of phase completion comments (refine and plan phases)
2. The `<!-- egg-phase-approval -->` marker identifies the approval section
3. When the human checks the `[x] Approve` checkbox, the orchestrator detects the change
4. The contract phase is updated and the next pipeline phase is triggered

In **prompt-driven mode**, the orchestrator handles phase approval via its decision queue with `decision_type="phase_gate"`. The terminal displays the full document in a pager (default: `less -R`) and offers view, edit, approve, and request-changes options. Revision rounds are not capped (#3392) — see [Converge-Before-Advance Gate](#converge-before-advance-gate); `max_hitl_review_cycles` only sets the non-convergence alert threshold.

When an operator selects `request_changes` or `change_approach`, the feedback text is stored as a timestamped `OperatorDirective` on the phase. Directives **accumulate** — they are never cleared. Whenever producer or reviewer prompts are subsequently built and the directive list is non-empty, agents receive a `## Phase Iteration Context` prompt section that lists all prior directives in chronological order with explicit precedence prose (later directives override earlier ones), plus a snapshot of each prior iteration's BRC verdict matrix and NACK reasons. This ensures reviewer agents do not unwittingly NACK a directive-driven change against a stale default rubric.

### Key Differences from Decisions

| Aspect | Formal Decisions | Phase Approval |
|--------|-----------------|----------------|
| Marker | `<!-- egg-hitl-decision id=... -->` | `<!-- egg-phase-approval -->` |
| Purpose | Choose between options | Advance to next phase |
| Multiple options | Yes (with "Other") | No (single checkbox) |
| Workflow job | `handle-decision` | `handle-approval` |

## Converge-Before-Advance Gate

The refine and plan phase gates iterate to a fixpoint before advancing (#3392).
A phase does not advance the moment its gate is approved; it advances only once
a re-run produces no new decisions and the operator approves. The loop:

1. The phase runs and registers its HITL decisions on the contract.
2. The gate surfaces those decisions (the [two-wave](#contract-decision-bridge)
   bridge promotes them into `pending_decisions` with a `decision.created`
   event) and the pipeline stays `AWAITING_HUMAN` until **every** one is
   resolved.
3. When a round resolves one or more decisions, the phase **re-runs** so the
   draft reflects the resolutions, then re-surfaces the gate. Re-runs are
   expected to converge quickly because the resolution-driven delta is small.
4. Any decision a resolution **induces** surfaces in the next round and must
   also be resolved.
5. The phase advances only on a round that resolved nothing new and the
   operator approves the `phase_gate`.

**Carry-forward.** A re-run's agents may re-register a question that was
already answered in a prior round. The registration paths
(`register_open_question`, the impasse-escalation router) adopt the prior
**resolved** `cq-N` via `egg_contracts.decisions.find_resolved_question`
instead of minting a new decision, so answered questions are never re-asked and
the open-decision set shrinks toward zero. Open-ended **feedback** carries
forward the same way: `request_feedback` adopts an already-**submitted**
feedback request for the same phase and question set
(`egg_contracts.feedback.find_carry_forward_feedback`) instead of replacing the
slot with a fresh unsubmitted entry, so an answered feedback request is never
re-surfaced either. Because resolved feedback also drives a re-run (the
convergence count includes it), this feedback-side carry-forward is required for
the guarantee to hold for feedback as well as `cq-N`. This is what guarantees
the loop terminates.

**No force-advance.** The pre-#3392 circuit breaker advanced the phase after
`max_hitl_review_cycles` rounds, leaving operator feedback unaddressed. That is
removed: every round is human-gated, so the loop never advances with decisions
or feedback outstanding. After `max_hitl_review_cycles` rounds a non-fatal
`OVERSEER_ALERT` (`reason="hitl_nonconvergence"`) is surfaced for visibility — a
pathological non-convergence (a real bug, or a genuinely churning design) — but
the phase is never force-advanced or failed.

**HITL for refine/plan.** Because the loop resolves decisions with a human each
round, refine and plan run the human-gated converge loop **whenever a human is
in the loop** (`hitl_gates: true`, the default) — and the force-advance backstop
can be dropped precisely because a human is then always present to resolve and
approve. For those phases `hitl_gates` selects the gate *mode* rather than
disabling the gate outright: when `hitl_gates: false`, the converge loop cannot
run (it would block forever on `wait_for_decision` with no human to answer), so
the gate is **surfaced as a non-blocking event and the phase advances
autonomously** — mirroring the unresolved-gap gate's autonomous escape (#3300).
A fully-autonomous pipeline (`hitl_gates: false` with no `start_phase`, i.e.
starting at refine) therefore advances through refine/plan without hanging,
exactly as it did before #3392; for phases outside refine/plan the flag toggles
the post-phase approval pause as before.

## Registration Guarantee (Decision Ledger)

The converge loop above can only surface decisions that were **registered**;
before #3390 nothing verified that a phase which should raise decisions
actually did, so a producer that silently skipped `egg-contract add-decision`
advanced the pipeline with `decisions: []` — indistinguishable from
"deliberately none". Two complementary layers close that gap: one
deterministic, one judgment-based.

### Deterministic: ledger attestation at propose time

Every refine/plan producer (`refiner`, `task_planner`, `architect`,
`risk_analyst` — the `simplifier`'s companion summary owns no decision
surface) must attest its **decision ledger** when proposing consensus. The
proposal `attestation` carries exactly one of:

- `decisions_registered: ["cq-1", …]` — every decision the producer
  registered this phase, or
- `no_decisions_rationale: "<why>"` — the **explicit empty ledger**: the
  phase deliberately raises no operator decisions, recorded as a claim
  rather than an omission. Requires `candidates_considered` (#3526, below).

The explicit-none form must additionally carry **`candidates_considered`**
(#3526): at least one `{question, disposition, why}` entry per open choice
the producer weighed and dispositioned away, with `disposition` one of:

- `not_operator_grade`: a design call the planner/implementer owns;
- `deferred_to_plan`: potentially operator-grade, better asked once the
  plan phase has made the design concrete. **Deferral is a handoff, not a
  disappearance**: the orchestrator injects refine's `deferred_to_plan`
  candidates into the plan-phase prompts as pre-seeded candidates the
  planner must register or explicitly disposition, and a **plan-phase**
  attestation may not use `deferred_to_plan` at all (plan is the last
  decision surface).

The handoff is coverage-gated at plan propose time (#3564). Each deferred
candidate is rendered in the plan prompt with a stable `dq-<hash>` id
(first 8 hex chars of the SHA-256 of the normalized question;
`egg_contracts.decisions.deferred_question_id`). The plan producer that
received the section (the architect in concurrent mode) must echo every id
in its attestation's **`deferred_resolutions`**: `{deferred_id,
resolution: "registered", cq: "cq-N"}` when the question was registered
(reframing the text as the design firms up is fine — identity rides on the
id, not the wording), or `{deferred_id, resolution: "not_operator_grade",
why}` when the design dissolved it. The orchestrator recomputes the ids
from refine's attestation and rejects the propose when any deferred
question is unaccounted, when an echoed id matches nothing, or when a
`registered` echo cites a cq-N absent from the attestation's own
`decisions_registered`. CLI: repeatable `--deferred
"<dq-id> :: registered :: <cq-N>"` / `--deferred
"<dq-id> :: not_operator_grade :: <why>"`.

A single free-form rationale paragraph proved trivially satisfiable; #3526's
backfill showed refine-surfaced decisions collapsing from ~8 per pipeline to
~0 within weeks of the rationale form landing, with deferrals to plan never
materializing. So the empty ledger must now name what was considered.
`candidates_considered` may also accompany `decisions_registered` (some
choices registered, others dispositioned away).

CLI: `egg-orch consensus propose --decisions-registered cq-1 cq-2 …` or
`--no-decisions-rationale "<why>" --considered
"<disposition> :: <question> :: <why>" …` (repeatable). MCP: the
`attestation` argument of `mcp__brc__propose`.

The orchestrator **hard-rejects** the propose (HTTP 400, tracker untouched)
when:

- the attestation is missing or malformed (neither field, both fields, a
  non-`cq-N` id, an explicit-none with no candidates, a malformed candidate,
  a malformed `deferred_resolutions` entry, a non-plan attestation carrying
  `deferred_resolutions`, or a plan-phase `deferred_to_plan` disposition);
  `attestation_schemas.DecisionSurfacingAttestation` and
  `routes/signals/_validation.py::_validate_decision_attestation`, both built
  on the shared `egg_contracts.decisions.decision_attestation_errors` shape
  check;
- a refine-deferred question is unaccounted in the plan producer's
  `deferred_resolutions` (#3564, `_validate_deferred_candidate_coverage` —
  see the handoff paragraph above);
- an attested id is not registered on the contract, or is registered for a
  different phase (cross-check against `contract.decisions`);
- the draft does not **cite** an attested `cq-N`
  (`_validate_decision_citations`). The `--format markdown` output of
  `egg-contract add-decision` embeds the id, so copying it into the draft's
  Open Questions section satisfies the citation automatically.

A producer therefore cannot reach consensus — and the phase cannot reach its
gate — without a well-formed ledger claim. "0 decisions at the gate" becomes
trustworthy as *deliberately none*.

### Gate-side backstop and auditability

At the phase gate, the orchestrator summarizes the ledger on the `phase_gate`
question so the operator can read it without a `get_contract` round-trip:
"N decision(s) registered this phase (cq-…), M resolved" or "explicitly none —
&lt;role&gt; attested: &lt;rationale&gt; (K candidate(s) considered)"
(recovered from the phase's `CONSENSUS_PROPOSE` messages). The same summary is
persisted as a structured snapshot on `PhaseExecution.decision_ledger`
(#3526) (registered ids, explicit-none flag, considered candidates) so
decisions-surfaced-per-phase is queryable from pipeline state over time and a
future decline in surfacing shows up in data rather than operator feel.

When the phase reaches the gate with **zero registered decisions and no
explicit-none attestation** — possible only on paths that bypass consensus
(force-advance, resume) — the gate does not silently proceed: a dedicated
backstop HITL is surfaced whose default remedy is a **phase re-run** (the
converge loop's standard corrective, with a directive telling producers to
register or explicitly attest), with an operator override to proceed to the
normal gate. On autonomous pipelines (`hitl_gates: false`) the missing ledger
is surfaced as a loud `phase.decision_ledger_missing` event but never blocks,
mirroring the autonomous gate-skip posture.

### Explicit-none attestations are confirmed, not trusted (#3462)

An explicit-none attestation is itself a judgment call about what *is* a
judgment call — exactly the class of decision the HITL contract assigns to
the operator, and the one escape hatch through which an agent under
convergence pressure can bypass the whole register → bridge → resolve chain.
So the gate does not fold it into the `phase_gate` question as prose: when a
refine/plan phase reaches its gate with an explicit-none attestation standing
in for a ledger, the orchestrator first surfaces a dedicated **confirmable
`choice` decision** quoting the role, the rationale, and the enumerated
`candidates_considered` with their dispositions (#3526), so the operator
confirms specific dispositions, not a paragraph ("the &lt;role&gt; attests
this phase deliberately raises no operator decisions — confirm?").

- **Confirm** (the bare keyword or the full option label — anything else is
  conservatively treated as a rejection, mirroring the phase_gate's
  "bare approve advances" posture) proceeds to the normal phase gate, and the
  gate's ledger note records "Operator confirmed the attestation".
- **Re-run** (or any free-text reply, which rides along as an operator note)
  kicks the phase back via the converge loop's standard re-run, with a
  directive telling producers to register each decision — including ones they
  believe prior context already resolves, registered with the recommended
  answer as the first option rather than attested away.

The confirmation is idempotent across converge rounds: a re-entered gate with
the *same* attestation reuses the operator's prior confirmation (or a pending
confirmation decision) instead of re-asking; a changed rationale is a new
claim and is asked again. On autonomous pipelines the unconfirmed attestation
is surfaced as a `phase.decision_ledger_explicit_none` event but never
blocks.

Prompt-side, the same issue closes the loophole at the source: decisions the
task description names as operator-owned (or covered by any
"surface as HITL" directive) **must be registered** even when the producer
believes they are already resolved, non-blocking, or deferred — belief about
resolution is a *recommended disposition* (recommended option citing the
resolving context), never a reason to skip registration — and
`reviewer_refine` (§7) / `reviewer_plan` (§14) NACK an explicit-none ledger
on a task that names decisions to surface.

### Judgment: reviewer obligation against un-surfaced decisions

A draft that quietly **commits** to an operator-grade choice ("we will drop
the legacy filter") without registering it can't be caught by a regex. That
half is enforced through consensus: `reviewer_refine` (§7) and
`reviewer_plan` (§14) carry an explicit obligation to **NACK** a draft that
commits to a decision not backed by a registered `cq-N` — and the open-NACK
barrier already prevents consensus from closing over an open NACK. The rubric
includes calibration guidance so implementation choices the planner
legitimately owns are not over-NACKed: the bar is answers only the operator
owns (product intent, scope boundaries, external commitments, user-visible
behavior). The `first_principles_reviewer` is deliberately **not** the home
for this obligation — it never NACKs by design (premise/direction concerns
escalate to the operator as HITL decisions, not NACKs).

## Detection Mechanism

The orchestrator's decision queue (`orchestrator/decision_queue.py`) monitors for changes. It checks:

1. **For decisions**: Comment contains `<!-- egg-hitl-decision` and a checkbox changed
2. **For approvals**: Comment contains `<!-- egg-phase-approval` AND `[x] Approve`

### Security

- Only authorized users can trigger phase transitions
- The bot cannot approve its own comments
- Debounce logic prevents rapid-fire updates when multiple boxes are checked quickly
- The decision resolve and cancel API endpoints (`POST .../decisions/{id}/resolve` and `.../cancel`) require `Authorization: Bearer <EGG_LIFECYCLE_SECRET>`. Agent pods never receive this env var, so agents cannot auto-approve HITL decisions via the API (see #1769).

## Best Practices

1. **Keep decisions focused**: One question per decision, with 2-4 clear options
2. **Always include "Other"**: The CLI does this automatically when using `--options`
3. **Separate concerns**: Use one comment for analysis/plan, another for approval
4. **Use descriptive questions**: Be specific about what you're asking

## Troubleshooting

### "Approval checkbox doesn't trigger workflow"

Check that:
- The `<!-- egg-phase-approval -->` marker is present
- The marker is on the line immediately before the checkbox
- The checkbox format is exactly `- [ ] Approve...` (spaces matter)
- The comment was edited (not a new comment)

### "Decision not detected"

Check that:
- The `<!-- egg-hitl-decision id=... -->` marker is present
- The decision ID uses only lowercase letters, numbers, and hyphens
- The checkbox format is standard markdown: `- [ ] Option` or `- [x] Option`

### "Feedback not detected"

Check that:
- The `<!-- egg-feedback id=... -->` marker is present
- The feedback ID uses only lowercase letters, numbers, and hyphens
- The submit checkbox is checked: `- [x] Submit feedback`
- Answers are in blockquote format: `> Answer text`

## Prompt-Driven Mode: Type-Aware Terminal Rendering

In prompt-driven mode (`egg-sdlc`), the HITL checkpoint handler (`sandbox/egg_lib/sdlc_hitl.py`) dispatches to type-specific terminal UIs based on the `decision_type` field on `HITLDecision`.

### Decision Types

| Type | Field Value | Terminal Behavior |
|------|-------------|-------------------|
| Phase gate | `phase_gate` | Displays full document in pager, offers view/edit/approve/request-changes options, and surfaces pending contract decisions via `[q]` option |
| Choice | `choice` | Renders numbered options for selection; shows draft document before first non-phase_gate decision, `[v]` option to re-view draft |
| Feedback | `feedback` | Prompts for each question individually, supports review-before-submit; shows draft document before first non-phase_gate decision, `[v]` option to re-view draft |

### Contract Decision Bridge

Two complementary bridges ensure contract-scoped decisions created by agents via `egg-contract add-decision` / `egg-contract add-feedback` are surfaced to humans:

**Server-side bridge (all modes):** After a phase gate is approved, `_queue_and_await_contract_decisions()` in `orchestrator/routes/pipelines.py` promotes any unresolved contract HITL decisions and feedback into the orchestrator's decision queue. HTTP/MCP callers (e.g., the `/sdlc` skill's Phase 4 handler) receive them as individual `choice` or `feedback` decisions. Once resolved, answers are written back to the contract so implement-phase agents see the human's choices. Without this bridge, contract questions registered via `egg-contract` would be silently dropped when a phase gate was approved, leaving the next phase's agents without the answers they need.

**Client-side bridge (prompt-driven mode only):** In prompt-driven mode, the phase gate menu displays a `[q] Answer open questions` option when unanswered decisions exist in the contract JSON, letting humans respond from the terminal before approving. Approving a phase gate with unanswered questions triggers a warning prompt.

**Gate-approval guard (#3374):** The bridge only promotes the *current* phase's `cq-N`; questions tagged for a later phase remain unanswered and otherwise invisible until that phase's gate. To avoid narrating an approval as "nothing else pending" while such questions sit outstanding, the `provide_input` response for a resolved `phase_gate` includes an `outstanding_contract_decisions` list of those later-phase unresolved `cq-N` (`id` / `question` / `phase`). They also remain visible in every `get_status` snapshot under `pending_contract_decisions`.

### Draft Document Display

When multiple HITL decisions are pending (e.g., agent-created choice/feedback questions plus the phase gate approval), the CLI presents them in FIFO order. To ensure humans have context when answering agent questions before seeing the phase gate:

- The analysis/plan draft document is automatically displayed in a pager before the first non-phase_gate decision
- Choice and feedback handlers include a `[v] View full document` option to re-display the draft at any time
- The draft is shown only once per decision queue to avoid repetitive pager displays

This ensures the human has access to the full analysis or plan context when answering agent questions, not just at the final phase gate approval.

### Draft Path Resolution

Phase gates display draft content (analysis or plan documents) to the human reviewer. The draft is resolved from the worktree using a two-step fallback:

1. **Issue-specific path** (primary): `.egg-state/drafts/{identifier}-analysis.md` or `{identifier}-plan.md`
2. **Generic path** (fallback): `.egg-state/drafts/analysis.md` or `plan.md`

If neither path exists, the phase gate displays a warning: *"No draft was found on the work branch."* When the fallback path is used, a debug log is emitted for diagnostics.

See [Orchestrator Architecture § Draft path resolution](architecture/orchestrator.md#per-pipeline-worktrees) for details on how draft files are stored and resolved.

Every decision type also includes universal options:
- **General feedback** (`[f]`) — free-text input attached alongside the primary resolution
- **Change approach** (`[a]`) — signals the agent to re-run the current phase differently
- **Cancel pipeline** (`[c]`) — terminates the pipeline

### JSON Resolution Payloads

Resolutions are sent as JSON objects so the pipeline can parse the human's intent:

| Action | Payload | Meaning |
|--------|---------|---------|
| Approve | `{"action": "approve"}` | Advance to next phase |
| Approve with context | `{"action": "approve", "context": "..."}` or `{"action": "approve", "feedback": "..."}` | Advance with human guidance persisted to contract and draft |
| Select option | `{"action": "select", "selected": "MongoDB"}` | Choice selection |
| Request changes | `{"action": "request_changes", "feedback": "..."}` | Re-run phase with feedback |
| Change approach | `{"action": "change_approach", "feedback": "..."}` | Re-run with different direction |
| Submit feedback | `{"action": "submit_feedback", "answers": {...}}` | Structured answers |

The pipeline runner (`orchestrator/routes/pipelines.py`) parses JSON payloads first, falling back to bare-string keyword matching for backward compatibility.

### Bare-string resolutions

Not every caller sends JSON: the MCP `provide_input` tool takes a free-text
`response`, and a phase gate's own options are the bare words `approve` /
`request changes`. Bare strings are classified on their **first line**, with
everything after it carried as context (`_classify_bare_gate_resolution`, #3636):

| Resolution | Read as |
|------------|---------|
| `approve` / `approved` / `lgtm` / `yes` / empty | Approve, no context |
| `approve`<br>&nbsp;<br>`Approved. The analysis is sound; advance.` | Approve, with the remainder as the operator's note (equivalent to `{"action": "approve", "context": "..."}`) |
| `request changes` | Request changes with no specifics; the gate asks a follow-up |
| `request changes`<br>&nbsp;<br>`The risk section omits rollback.` | Request changes, with the remainder as the feedback |
| `approve the rewrite but drop slice 3` | Request changes (the first line is a sentence, not a bare option word) |

Matching is on the whole first line, so only the option-word-plus-justification
shape is treated as a selection. Trailing sentence punctuation on the option
word (`Approved.`, `LGTM!`) is ignored.

Whichever branch the parser takes is logged (`HITL gate: resolution parsed`,
with `parse_path` and `outcome`) and persisted on the decision record as
`resolution_outcome` (`approved` / `needs_revision`) alongside the raw
`resolution`, so the decision API shows how the gate read the operator's text
rather than only the text itself.

### Creating Typed Decisions from Agents

Agents can create typed decisions via the `OrchClient.create_decision()` method:

```python
client.create_decision(
    pipeline_id="issue-123",
    question="Which database should we use?",
    options=["PostgreSQL", "MongoDB", "SQLite"],
    decision_type="choice",
    phase="plan",  # Optional: tracks which phase created the decision
)

client.create_decision(
    pipeline_id="issue-123",
    question="Feedback needed",
    decision_type="feedback",
    questions=[
        {"id": "q1", "question": "What is the expected traffic volume?"},
        {"id": "q2", "question": "Any specific performance requirements?"},
    ],
    phase="refine",  # Optional: helps sandbox locate correct draft paths
)
```

Both `OrchClient.create_decision()` and the underlying orchestrator API (`POST /api/v1/pipelines/{id}/decisions`) accept `decision_type`, `questions`, and `phase` fields. The `phase` field is optional but recommended — it tracks which pipeline phase created the decision and helps the HITL handler locate the correct draft paths (e.g., `.egg-state/drafts/900-plan.md` instead of `.egg-state/drafts/900-unknown.md`).

## `/sdlc` Skill: Auto-Resolving Repeated Questions

The `/sdlc` Claude Code skill (defined by `skills/sdlc/SKILL.md`) handles HITL via MCP calls to `get_status` / `provide_input` (orchestrator decisions, plus pre-gate contract `cq-N` decisions via the contract fallback — see [Resolving pre-gate contract HITL decisions](#resolving-pre-gate-contract-hitl-decisions-provide_input-fallback)) plus `answer_feedback` (contract-scoped pre-proposal `feedback-N` that never enters the decision queue — see [Answering contract feedback from the host](#answering-contract-feedback-from-the-host-answer_feedback)). Decisions surface in **two waves**: when a phase first reaches `awaiting_human`, `pending_decisions` contains only the `phase_gate`; after it is approved, the [server-side bridge](#contract-decision-bridge) promotes any deferred `choice`/`feedback` decisions into `pending_decisions` and the pipeline stays in `awaiting_human` until they are resolved (see [Two-wave surfacing](../skills/sdlc/SKILL.md#two-wave-surfacing)). Because the refiner commonly embeds those same questions directly in the analysis/plan draft as `<!-- egg-hitl-decision id=cq-N -->` markers, the answers given during the phase_gate step would otherwise be re-asked in Wave 2.

Without special handling the skill would re-prompt the user for every draft-embedded question a second time once those standalone decisions arrive — the user answers each question twice. Phase 4 of the skill avoids this via a session-scoped **`resolved_questions_map`**.

### Resolved Questions Map

`resolved_questions_map` is an in-memory dict maintained by the `/sdlc` skill for the lifetime of a single `/sdlc` invocation:

| Key | Value |
|-----|-------|
| Normalized question text (`question.strip().lower()`) | The user's verbatim answer |

It is populated by the `phase_gate` handler's Step 5 as each draft-embedded question is answered, and consulted by both the `choice` and `feedback` handlers before they prompt. Normalization is intentionally conservative (case-insensitive, whitespace-trimmed) — punctuation or rewording differences are treated as misses and fall through to the existing prompt flow. This is by design: too-permissive matching risks silently submitting wrong answers.

### Auto-Resolution Flow

**`choice` decisions.** Before prompting, the skill normalizes the decision's `question`, looks it up in `resolved_questions_map`, and compares the stored answer against each entry of `decision.options` using the same normalization. On a match, it skips `AskUserQuestion` and submits the option verbatim:

```json
{"action": "select", "selected": "<matched option>"}
```

It then prints a one-line note:

```
Auto-resolved <decision_id>: selected '<option>' from captured context.
```

If the captured answer doesn't correspond to any option (e.g., it was free-text typed into the "Other" field during the phase gate), the handler falls through to the existing prompt flow — the user is asked again with the registered option list.

**`feedback` decisions.** Before prompting, the skill normalizes each question in the `questions` array and looks it up in `resolved_questions_map`, collecting matches into a prefilled `answers` dict keyed by the question's `id` (or the `q-<1-based index>` fallback). If all questions are prefilled, `AskUserQuestion` is skipped entirely; otherwise only the unmatched questions are presented, and the new answers are merged into the prefilled dict. A single merged `provide_input` call then submits:

```json
{"action": "submit_feedback", "answers": {"<id>": "<answer>", ...}}
```

followed by a one-line note naming the decision ID and which question IDs were auto-resolved from captured context.

### Transparency

Every auto-resolution prints a user-visible one-line note identifying the decision ID and the chosen value. This is a hard requirement, not a convenience: it is the only feedback loop a user has to catch an incorrect match (e.g., two draft questions that happened to normalize to the same text). Users who see an unexpected auto-resolution can intervene at the next phase gate using `request_changes` or `change_approach`.

### Scope and Non-Goals

- **Skill-only change.** The orchestrator's `_parse_resolution` and the contract decision registration path are unchanged — the phase_gate resolution's `context` string is still preserved in the raw resolution but is not routed to downstream decisions by the orchestrator.
- **Refiner-side question rephrasing is not handled.** If the refiner rewords the question between the draft marker and the registered contract decision, normalized-exact match will miss and the user is prompted normally. Fuzzy matching is an explicit non-goal.
- **Map is session-scoped, not persisted.** A fresh `/sdlc` invocation starts with an empty map. Across multiple phase_gates in the same session the map accumulates; newer answers for a duplicate normalized question overwrite older ones.
- **Map is not cleared on `change_approach`.** When a user selects `change_approach`, the phase restarts and new decisions may arrive with the same question text but different intent. The map still holds old answers, so if the restarted phase re-registers the same question text, the old answer may auto-resolve. The user-visible transparency note makes this catchable — an unexpected auto-resolution can be corrected at the next phase gate.
- **Prompt-driven mode (`egg-sdlc`) is unaffected.** The terminal UI in `sandbox/egg_lib/sdlc_hitl.py` does not use `resolved_questions_map` — this is strictly a `/sdlc` Claude Code skill optimization.

## Orchestrator-Emitted Decisions

Some HITL decisions are created directly by the orchestrator — not by agents — in response to internal recovery scenarios. These decisions appear in `/sdlc` and the decision queue the same way agent-created decisions do, but they surface pipeline-level recovery choices rather than design questions.

### Sync Divergence: Non-Destructive Reconcile (#2979)

When a pipeline branch's worktree diverges from its remote and the rebase autoresolve at a phase boundary cannot reconcile the divergence, the orchestrator pauses for a manual reconcile. This is **non-destructive**: the worktree is left at the local HEAD with all committed work intact (the autoresolve aborts back to clean state without modifying the working tree). Steps 1–3 happen inside `_sync_worktree_with_remote`; step 4 happens in one of two callers — `_sync_worktree_reconciling_divergence` (blocking, used by the `_run_pipeline` loop) or `_emit_divergence_reconcile_hitl` (non-blocking, used by the `populate_contract` route):

1. Enumerates local-only commits present on the worktree but not on origin
2. Creates a backup ref pinning the current local HEAD: `refs/egg-backup/sync-recovery/<pipeline-id>/<unix-ts-ns>`, where `<unix-ts-ns>` is `time.time_ns()` — a 19-digit nanoseconds-since-epoch value, not conventional Unix seconds. To derive the wall-clock time: `date -d @$((<unix-ts-ns>/1000000000))`.
3. Leaves the worktree at its current local HEAD (nothing is discarded)
4. Pins the pipeline to `AWAITING_HUMAN` (not `FAILED`) and emits a HITL decision (context: `divergence_reconcile_unacked`)

The pipeline stays paused (`pipeline.status=AWAITING_HUMAN` with a pending decision whose context is `divergence_reconcile_unacked`) until the operator reconciles the worktree and resolves the decision.

**Options:**

| Option | Effect |
|--------|--------|
| `Reconciled — resume` | Orchestrator re-runs the worktree sync and resumes the phase's post-processing from where it paused (no full phase re-run). **Auto-resume only applies to the two `_run_pipeline` fire sites** (phase start, post-phase), which block on `wait_for_decision` inside `_sync_worktree_reconciling_divergence`. The `populate_contract` site uses the non-blocking `_emit_divergence_reconcile_hitl` and returns immediately — resolving `Reconciled — resume` there is inert; the operator must re-POST `populate_contract` against the reconciled worktree. |
| `Abort pipeline` | Orchestrator marks the pipeline `FAILED`; the backup ref preserves commits for offline inspection |

After 3 unresolved `Reconciled — resume` attempts the pipeline is marked `FAILED` with `reason="…the reconcile pause budget was exhausted"` (`_MAX_DIVERGENCE_RECONCILE_PAUSES = 3` in `pipelines.py`). The backup ref is preserved either way.

**Recovery steps for operators:**

1. Open `/sdlc` and find the pending decision
2. On the orchestrator host, navigate to the pipeline's worktree and manually reconcile the divergence — e.g. rebase the local commits onto `origin/<branch>` and resolve the conflict. The backup ref shows the local commits: `git log refs/egg-backup/sync-recovery/<pipeline-id>/<unix-ts-ns>`
3. Choose **Reconciled — resume** once the worktree is reconciled; the orchestrator re-runs the sync and continues the phase. Choose **Abort** to fail the pipeline and clean up manually.

This recovery fires at three sites:
- **Phase start** — when the pre-phase rebase fails
- **Post-phase** — when the post-phase sync fails
- **`populate_contract`** — when the pre-populate sync fails. HTTP 409 with `reason="divergence_reconcile_unacked"` (#2792, #2979). The route is idempotent: if a reconcile HITL is already pending from a prior call, it returns 409 immediately without emitting a duplicate decision.

### Implement-Start Plan Pre-Flight Rejection (#3100)

When `start_phase=implement` is submitted and the plan draft is missing required `pr:` metadata, the orchestrator fails the pipeline immediately with a dedicated HITL rather than running the implement phase with no openable context PR. This gate fires after the empty-contract gate so the #2627 HITL routing is unchanged.

**Scope:** Remote pipelines only — a `repo` or `base_branch` is set on the pipeline. Local-mode pipelines never open a context PR and are exempt.

**Trigger condition:** The plan draft's `yaml-tasks` fence is missing one or more of: `pr.title`, `pr.description`, `pr.test_plan`, or the `pr.manual_steps` key. The HITL question names the specific missing fields.

The pipeline is set to `FAILED` (not `AWAITING_HUMAN`) and the HITL surfaces for operator action.

**Options:**

| Option | Effect |
|--------|--------|
| `Fix the plan draft's pr: block and restart implement` | Operator manually adds a top-level `pr:` block (title, description, test_plan, manual_steps) to the draft's `# yaml-tasks` fence on the work branch, then calls `restart_phase implement`. |
| `Restart plan phase` | Calls `restart_phase plan` to regenerate the draft from scratch. |
| `Abort pipeline` | Calls `cancel_task` to abort the pipeline. |

**Recovery steps for operators:**

1. Open `/sdlc` and find the pending decision — the question text names the draft path and the missing fields.
2. Choose an option:
   - To fix the draft in place: edit the plan draft named in the HITL question (a path of the form `.egg-state/drafts/<prefix>-plan.md`, where `<prefix>` is the issue number or pipeline id — do not edit the unprefixed `.egg-state/drafts/plan.md`, which is a stale legacy path that gets reaped) on the work branch to add a `pr:` block with all four required keys, commit the change, then select **Fix the plan draft's pr: block and restart implement**.
   - To regenerate: select **Restart plan phase** — the plan agent will rerun and produce a new draft.
   - To abandon: select **Abort pipeline**.

**Infra failures** (parser import unavailable, draft file unreadable) warn and continue rather than triggering this gate, so the pre-existing populate-path outcomes handle those cases.

See also: [Plan pre-flight on implement-start resumes](guides/sdlc-pipeline.md#plan-pre-flight-on-implement-start-resumes) in the SDLC pipeline guide.

### Executable Task-Completion Resolution (#3124)

When an operator resolves any HITL decision with a resolution string beginning with
"Mark task `<task-id>` complete" (case-insensitive; backticks around the task id are
optional), the orchestrator auto-executes `complete_task_as_operator` — immediately
marking the task complete as an audited operator action under `Role.HUMAN`. This
replaces the previous workaround of `kubectl exec` into an agent pod and impersonating
the agent's role.

Optional commit evidence: append "commit <sha>" immediately after the completion
clause (e.g. `Mark task TASK-1-2 complete, commit abc1234`) to link the commit to the
task row. Write the SHA bare — the regex matches hex digits directly, not a
backticked span. Commits embedded later in the reply (with intervening prose) are
not captured, to prevent unintended evidence attachment.

**Primary use case:** A task is (re)assigned to a producer that has already CONFIRMED.
The `#3114` completeness gate holds the slice open over the undelivered row, while the
producer's CONFIRMED state blocks it from re-proposing. An HITL decision with a
"Mark task `<task-id>` complete" option (created by `mcp__sdlc__register_open_question`
or the impasse-routing escalation) lets the operator unblock the slice without
impersonating an agent role.

After the task is marked complete, the #3114 completeness gate releases over that row
and the slice can finalize without the producer re-entering WORKING — the producer
stays CONFIRMED. The HITL dispatch and the direct REST path (below) are two equivalent
surfaces to the same `complete_task_as_operator` mutation; use REST when there is no
live HITL decision to attach the dispatch to (e.g. the operator wants to act directly
without first creating a decision).

```bash
curl -X POST http://egg-orchestrator:9849/api/v1/contracts/<pipeline-id>/tasks/<task-id>/complete \
  -H "Authorization: Bearer $EGG_LIFECYCLE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"commit": "<sha>", "reason": "task reassigned post-confirm"}'
```

Auth: lifecycle-secret guarded (operator/host surface only — not proxied by the gateway
to sandbox agents).

Note: #3129 also adds a *separate* auto-reopen mechanism that fires when a task is
**reassigned** to a confirmed producer — see `_maybe_reopen_confirmed_producer` in
`routes/consensus.py`. That path lets the producer re-propose against the new task. It
does **not** fire after the operator-completion path above, because the completion flips
the row to `complete` and `incomplete_tasks` then returns nothing to reopen for.

### Executable `adds_task` Option (#3428)

A `register_open_question` option that mandates a contract mutation — "Add a new
task/slice to wire X as a dependency" — used to be silently inert: agents have no
task-add verb, so resolving the decision recorded the choice and materialized
nothing. The reviewer that raised the question kept withholding ACK (correctly —
the mandated task still didn't exist), the orchestrator kept re-spawning the
producer at an unchanged contract, and the slice re-deadlocked *after* the human
answered.

Such options now carry a structured `adds_task` payload, attached at registration
time:

```json
{
  "question": "Slice-4 needs the secondary-repo worktree wired. How should we proceed?",
  "options": ["Add a task to wire it as a slice-4 dependency", "Defer to a follow-up"],
  "adds_task": {
    "option": 1,
    "slice_id": "slice-4",
    "description": "Wire secondary-repo worktree + per-repo work/integration branch creation",
    "acceptance_criteria": "...",
    "files_affected": ["..."],
    "role": "coder"
  }
}
```

When the operator resolves the decision by selecting that option (by label, `opt-N`
id, or positional `option N` / `N` — free-form prose never fires the executor), the
orchestrator materializes the task via `add_task_as_operator`: an audited
`Role.HUMAN` mutation that appends the task to the named slice with a
lock-allocated `task-<P>-<N>` id. The blocked agents see the new task on their next
contract poll and the reviewer's precondition becomes satisfiable. Both resolve
paths dispatch this — the pre-bridge contract fallback (`cq-N` resolved directly)
and the post-gate bridged queue path (the contract decision is recovered via the
bridge's context fingerprint). Execution failure is surfaced in the resolve
response's `executed_action` payload and logged as an error, never silent.

The direct REST surface (no live decision required) mirrors the task-completion
route:

```bash
curl -X POST http://egg-orchestrator:9849/api/v1/contracts/<pipeline-id>/tasks \
  -H "Authorization: Bearer $EGG_LIFECYCLE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"slice_id": "slice-4", "description": "<task>", "reason": "materialize cq-4 opt-1"}'
```

**Registration guidance for agents:** if an option means "add a task", it MUST carry
`adds_task` — without the payload the option is unactionable by anyone. Options that
only gate agent behavior ("proceed with approach A") need no payload; the blocked
agent reads the resolution and acts. Structural changes beyond a task append (new
slices, DAG edits) still go through a replan, not a decision option.

### Executable Consensus-Timeout Retry (#3421)

The `consensus_timeout_incomplete` HITL decision (raised by
`_incomplete_consensus_decision_text` when concurrent-phase consensus times out
with unresolved NACKs, or no agents ever confirm — see [Concurrent Execution:
Timeout Handling](guides/concurrent-execution.md#timeout-handling)) is persisted
moments before the driver marks the pipeline `failed` and exits. Nothing else
ever waits on it, so resolving "Retry phase" used to be a silent no-op: the
decision flipped to resolved, the pipeline stayed `failed`, and no agents
respawned.

Resolving the decision now dispatches on its label:

- **Retry phase** — calls the `restart_phase` route in-process (the documented
  manual workaround), which tears down the failed phase, flips the pipeline
  back to `running`, and respawns a fresh driver. As with any `restart_phase`
  call, per-role branch tips are **not** preserved: fresh worktrees re-fork
  from the shared work branch tip, and only committed-but-unpushed work is
  salvaged (best-effort) to `egg/recovered/*` refs — see [Orchestrator CLI
  Reference](reference/orchestrator-cli.md) for the full restart semantics.
  The decision's question text now states this directly instead of promising
  artifacts are preserved intact on a per-role branch.
- **Abort phase** — no action: the driver already failed the pipeline when it
  escalated the decision, so the state already matches the intent. The
  resolve response's `executed_action` payload says so instead of resolving
  silently.
- **Accept current state** — not automated (force-advancing past a
  non-converged phase is an operator judgment); the payload names
  `advance_phase` / `restart_phase` as the manual follow-ups instead of
  resolving silently.

`_maybe_dispatch_consensus_timeout_resolution` (`orchestrator/routes/decisions/_handlers.py`)
keys the dispatch on the decision's `context` field
(`_CONSENSUS_TIMEOUT_HITL_CONTEXT = "consensus_timeout_incomplete"` in
`routes/pipelines.py`), not the prose question text — mirroring the
context-keyed `failed_role:` discriminator (set in
`orchestrator/concurrent_executor.py`, consumed in
`routes/decisions/_resolve.py` / `routes/signals/_consensus_confirm.py`),
matching the code comment at `_handlers.py` (note the sibling
`_maybe_complete_task_from_resolution` (#3124) keys on a regex over the
*resolution label* instead, not `context`).
Dispatch failure (e.g. `restart_phase`
raising, or returning non-200) is logged and surfaced in `executed_action`
rather than swallowed, since the decision is already resolved by the time
dispatch runs.

### Executable Arms-Exhausted Retry (#3496)

An exhausted dedupe key is terminal — only `record_success` clears it (unreachable,
since an exhausted key can no longer spawn) — so when *every* spawn arm a slice
needs in order to advance is exhausted at once, the event loop used to sit in a
silent livelock: pipeline `running`, `pending_decisions` empty, "spawn blocked"
logged every poll until the consensus timeout hard-failed the slice hours later.
See [Agent Recovery: Exhausted-key escalation and in-band
reset](reference/agent-recovery.md#exhausted-key-escalation-and-in-band-reset-3496)
for the detection mechanics.

The event loop now detects the wedge and persists an `event_arms_exhausted` HITL
decision (deduped — one pending decision covers every wedged slice of the
pipeline) with three options:

- **Retry arms (reset spawn budgets)** — clears the exhausted keys on the
  pipeline's live event loop(s) in-band via the in-process live-loop registry
  (`event_loop.get_live_event_loops`), giving each blocked arm a fresh spawn
  budget with nothing torn down. If the underlying failure persists, the arms
  re-exhaust and the decision re-fires. Reports an error instead of resolving
  silently when no live loop exists for the pipeline (e.g. after an
  orchestrator restart, which already resets all supervision state) — resolve
  with "Restart phase" instead in that case.
- **Restart phase** — dispatches through the same `_execute_restart_phase`
  executor as the consensus-timeout "Retry phase" above (`restart_phase`
  in-process; per-role branch tips are not preserved).
- **Abort (manual — recorded only)** — no action is taken; the payload points
  at `cancel_task` as the manual follow-up, since resolving this option does
  not by itself stop the still-wedged phase.

`_maybe_dispatch_arms_exhausted_resolution` (`orchestrator/routes/decisions/_handlers.py`)
keys the dispatch on the decision's `context` field
(`ARMS_EXHAUSTED_HITL_CONTEXT = "event_arms_exhausted"` in
`orchestrator/concurrent_executor.py`), mirroring the consensus-timeout
dispatch's `context`-keyed discriminator. If the wedge clears by another route
before the operator resolves the decision — a fresh key derived, a spawn
succeeded, or an unrelated decision re-keyed the arms — the event loop
auto-withdraws the now-stale decision (`_withdraw_arms_exhausted_decisions` in
`routes/pipelines.py`), guarded so a still-wedged sibling slice holds the
shared decision in place.

### Executable Arms-Parked Retry (#3548)

A no-op-parked dedupe key (#3425) self-releases — but only for a single probe
spawn per fingerprint change or per `SUPERVISION_NOOP_PARK_RETRY_SECONDS`
heartbeat — so when *every* spawn arm a slice needs is blocked on a
no-op-park (or exhausted) key at once, a round that is one verdict away from
converging can sit silently for the full heartbeat window with
`pending_decisions` empty: the no-op-park sibling of the arms-exhausted wedge
above. See [Agent Recovery: All-arms-parked
escalation](reference/agent-recovery.md#all-arms-parked-escalation-3548) for
the detection mechanics (`_check_arms_parked`, `event_loop/_loop.py`).

The event loop persists an `event_arms_parked` HITL decision (deduped the
same way as `event_arms_exhausted`) with three options:

- **Retry arms (release no-op parks)** — clears the no-op-parked keys on the
  pipeline's live event loop(s) in-band (`event_loop.get_live_event_loops`),
  so the blocked arms respawn on the next poll instead of waiting out the
  park retry heartbeat. If the agents keep no-oping, the arms re-park and the
  decision re-fires. Reports an error instead of resolving silently when no
  live loop exists for the pipeline — resolve with "Restart phase" instead in
  that case.
- **Restart phase** — the same `_execute_restart_phase` executor shared by
  the consensus-timeout and arms-exhausted retry dispatches.
- **Abort (manual — recorded only)** — no action is taken; the payload points
  at `cancel_task` as the manual follow-up.

`_maybe_dispatch_arms_parked_resolution` (`orchestrator/routes/decisions/_handlers.py`)
keys the dispatch on `ARMS_PARKED_HITL_CONTEXT = "event_arms_parked"` (in
`orchestrator/concurrent_executor.py`). If the stall clears by another route
before the operator resolves the decision, the event loop auto-withdraws it
(`_withdraw_arms_parked_decisions` in `routes/pipelines/_decisions.py`), with
the same multi-slice guard as the arms-exhausted withdrawal.

## Related Files

- `orchestrator/mcp_tools.py` — MCP `get_status` tool; enriches all pending decisions with `draft_content`; enriches `phase_gate` decisions additionally with `completed_agents_summary` and `reviewer_feedback`
- `orchestrator/models.py` — `HITLDecision` model with `decision_type`, `questions`, `phase`, and `content_changed` fields; `content_changed` is set by the orchestrator on re-run phase gates to indicate whether the draft changed since the previous resolved decision (literal string comparison; `None` on first decision, `True`/`False` on subsequent ones). Also contains `OperatorDirective` (a single timestamped operator directive stored on kickback) and `IterationSummary` (BRC verdict snapshot for a kicked-back iteration), both accumulated on `PhaseExecution.operator_directives` / `PhaseExecution.iteration_history`.
- `orchestrator/decision_queue.py` — Decision queue handling typed decisions
- `orchestrator/routes/decisions/` — Decision API endpoints (create, list, resolve), the `POST .../feedback/answer` route for contract-scoped feedback (`answer_feedback` MCP tool; #3007), the contract-decision fallback in `resolve_decision` that writes pre-gate `cq-N` resolutions directly to the contract when the id is not in the queue (#3071), the executable task-completion dispatch (`_maybe_complete_task_from_resolution`) that auto-executes `complete_task_as_operator` when the resolution matches "Mark task `<id>` complete" (#3124), orphaned-driver revival on `phase_gate` resolution: when no live `_run_pipeline` driver thread owns an `AWAITING_HUMAN` pipeline (e.g. after an orchestrator restart), the resolve path re-launches the driver via `start_pipeline`'s recovery branch so the resolution self-heals rather than hanging silently; an `OVERSEER_ALERT` is broadcast on the bus when the orphaned park is detected (before the `start_pipeline` re-launch, so it fires even if that re-launch returns non-200 or raises) (#3233), and the first-principles redirect accept-path (`_maybe_apply_first_principles_redirect`, in `_handlers.py` and re-exported through the package barrel `__init__.py`): when the operator resolves a `first_principles_reviewer` refine-phase decision with "Adopt the redirect", this handler rewrites the pipeline seed via `rewrite_task_description_as_operator` and re-runs the refine phase; "Don't build this" cancels the pipeline; "Proceed as-is" is a no-op (#3385); the consensus-timeout retry dispatch (`_maybe_dispatch_consensus_timeout_resolution`, in `_handlers.py`) that auto-executes `restart_phase` when a `consensus_timeout_incomplete` decision is resolved with "Retry phase" (#3421); and the arms-exhausted retry dispatch (`_maybe_dispatch_arms_exhausted_resolution`, sharing the `_execute_restart_phase` helper with the consensus-timeout path) that clears exhausted spawn budgets in-band, restarts the phase, or records an abort when an `event_arms_exhausted` decision is resolved (#3496); its no-op-park sibling (`_maybe_dispatch_arms_parked_resolution`) that releases no-op-parked keys in-band, restarts the phase, or records an abort when an `event_arms_parked` decision is resolved (#3548)
- `orchestrator/operator_actions.py` — Operator-grade contract mutations; `complete_task_as_operator` applies task-status mutations as `Role.HUMAN`, bypassing the implementer/reviewer field-ownership restriction (#3124); `rewrite_task_description_as_operator` rewrites `contract.task_description` as `Role.HUMAN` for the first-principles redirect accept-path (#3385); `add_task_as_operator` appends a task to a slice as `Role.HUMAN` for the executable `adds_task` decision option and the direct `POST /api/v1/contracts/<id>/tasks` route (#3428)
- `orchestrator/mcp_tools.py` — `answer_feedback` MCP tool (`_handle_answer_feedback`) for host-side answering of pre-proposal contract feedback
- `orchestrator/routes/pipelines.py` — Phase gate resolution with JSON payload parsing; `persist_contract_statefiles` commits and pushes a contract decision write to the work branch at write time so a phase-(re)start worktree reset cannot revert it (#3427)
- `shared/egg_contracts/validator.py` — `apply_mutation`'s append-only guard rejects a whole-entry write to an existing `decisions[]` index with `error_kind="conflict"` (mapped to HTTP 409 in `orchestrator/routes/contracts.py`) rather than overwriting it (#3427)
- `sandbox/egg_lib/sdlc_hitl.py` — Type-aware terminal HITL handler
- `skills/sdlc/SKILL.md` — `/sdlc` Claude Code skill defining Phase 4 HITL handling: **two-wave surfacing** (phase_gate alone in Wave 1, deferred `choice`/`feedback` in Wave 2 after approval) and the session-scoped `resolved_questions_map` that handles cross-wave deduplication
- `sandbox/egg_lib/orch_client.py` — `OrchClient.create_decision()` for typed decisions
- `sandbox/egg_lib/contract_cli/` — CLI for creating decisions and feedback
- `shared/egg_contracts/feedback.py` — Feedback generation and parsing
- `docs/templates/analysis.md` — Template showing decision usage
- `docs/templates/phase-completion.md` — Template for approval format
