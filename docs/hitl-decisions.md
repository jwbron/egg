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

In prompt-driven mode, decisions carry a `decision_type` field (`phase_gate`, `choice`, or `feedback`) that drives type-specific terminal rendering. The orchestrator's decision queue supports a "request changes" option at phase gates, with a circuit breaker (`max_hitl_review_cycles`, default 3) to prevent unbounded revision loops. See [Prompt-Driven Mode: Type-Aware Terminal Rendering](#prompt-driven-mode-type-aware-terminal-rendering) for details.

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

- the decision does not appear in `get_status(...).pending_decisions`;
- calling `provide_input(decision_id="cq-N", ...)` previously returned
  **HTTP 404** (not in the queue), leaving the operator with no resolution
  channel and the pipeline deadlocking (#3071, observed on
  pipeline-c2faf164).

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

In **prompt-driven mode**, the orchestrator handles phase approval via its decision queue with `decision_type="phase_gate"`. The terminal displays the full document in a pager (default: `less -R`) and offers view, edit, approve, and request-changes options. A circuit breaker (`max_hitl_review_cycles`, default 3) prevents unbounded revision loops.

When an operator selects `request_changes` or `change_approach`, the feedback text is stored as a timestamped `OperatorDirective` on the phase. Directives **accumulate** — they are never cleared. Whenever producer or reviewer prompts are subsequently built and the directive list is non-empty, agents receive a `## Phase Iteration Context` prompt section that lists all prior directives in chronological order with explicit precedence prose (later directives override earlier ones), plus a snapshot of each prior iteration's BRC verdict matrix and NACK reasons. This ensures reviewer agents do not unwittingly NACK a directive-driven change against a stale default rubric.

### Key Differences from Decisions

| Aspect | Formal Decisions | Phase Approval |
|--------|-----------------|----------------|
| Marker | `<!-- egg-hitl-decision id=... -->` | `<!-- egg-phase-approval -->` |
| Purpose | Choose between options | Advance to next phase |
| Multiple options | Yes (with "Other") | No (single checkbox) |
| Workflow job | `handle-decision` | `handle-approval` |

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

The pipeline runner (`orchestrator/routes/pipelines.py`) parses JSON payloads first, falling back to legacy bare-string keyword matching for backward compatibility.

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

## Related Files

- `orchestrator/mcp_tools.py` — MCP `get_status` tool; enriches all pending decisions with `draft_content`; enriches `phase_gate` decisions additionally with `completed_agents_summary` and `reviewer_feedback`
- `orchestrator/models.py` — `HITLDecision` model with `decision_type`, `questions`, `phase`, and `content_changed` fields; `content_changed` is set by the orchestrator on re-run phase gates to indicate whether the draft changed since the previous resolved decision (literal string comparison; `None` on first decision, `True`/`False` on subsequent ones). Also contains `OperatorDirective` (a single timestamped operator directive stored on kickback) and `IterationSummary` (BRC verdict snapshot for a kicked-back iteration), both accumulated on `PhaseExecution.operator_directives` / `PhaseExecution.iteration_history`.
- `orchestrator/decision_queue.py` — Decision queue handling typed decisions
- `orchestrator/routes/decisions.py` — Decision API endpoints (create, list, resolve), the `POST .../feedback/answer` route for contract-scoped feedback (`answer_feedback` MCP tool; #3007), the contract-decision fallback in `resolve_decision` that writes pre-gate `cq-N` resolutions directly to the contract when the id is not in the queue (#3071), and the executable task-completion dispatch (`_maybe_complete_task_from_resolution`) that auto-executes `complete_task_as_operator` when the resolution matches "Mark task `<id>` complete" (#3124)
- `orchestrator/operator_actions.py` — Operator-grade contract task mutations; `complete_task_as_operator` applies task-status mutations as `Role.HUMAN`, bypassing the implementer/reviewer field-ownership restriction (#3124)
- `orchestrator/mcp_tools.py` — `answer_feedback` MCP tool (`_handle_answer_feedback`) for host-side answering of pre-proposal contract feedback
- `orchestrator/routes/pipelines.py` — Phase gate resolution with JSON payload parsing
- `sandbox/egg_lib/sdlc_hitl.py` — Type-aware terminal HITL handler
- `skills/sdlc/SKILL.md` — `/sdlc` Claude Code skill defining Phase 4 HITL handling: **two-wave surfacing** (phase_gate alone in Wave 1, deferred `choice`/`feedback` in Wave 2 after approval) and the session-scoped `resolved_questions_map` that handles cross-wave deduplication
- `sandbox/egg_lib/orch_client.py` — `OrchClient.create_decision()` for typed decisions
- `sandbox/egg_lib/contract_cli.py` — CLI for creating decisions and feedback
- `shared/egg_contracts/feedback.py` — Feedback generation and parsing
- `docs/templates/analysis.md` — Template showing decision usage
- `docs/templates/phase-completion.md` — Template for approval format
