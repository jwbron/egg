# HITL (Human-In-The-Loop) Decision Workflow

This document explains how human decisions are captured and processed in the SDLC pipeline.

## Overview

The SDLC pipeline includes phases where human input is required before proceeding:
- **Refine phase**: Human approves the analysis before planning
- **Plan phase**: Human approves the implementation plan before coding

Three mechanisms exist for gathering human input:
1. **Formal HITL decisions** — Multiple-choice questions with checkboxes
2. **Feedback comments** — Open-ended questions in an editable comment
3. **Phase approval** — Single checkbox to approve and advance to the next phase

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
<!-- egg-hitl-decision id=decision-1 -->

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

Contract decisions created by agents via `egg-contract add-decision` are automatically bridged to the phase gate menu in prompt-driven mode. When unanswered decisions exist in the contract JSON, the phase gate displays a `[q] Answer open questions` option that lets humans respond directly from the terminal. Approving a phase gate with unanswered questions triggers a warning prompt.

### Draft Document Display

When multiple HITL decisions are pending (e.g., agent-created choice/feedback questions plus the phase gate approval), the CLI presents them in FIFO order. To ensure humans have context when answering agent questions before seeing the phase gate:

- The analysis/plan draft document is automatically displayed in a pager before the first non-phase_gate decision
- Choice and feedback handlers include a `[v] View full document` option to re-display the draft at any time
- The draft is shown only once per decision queue to avoid repetitive pager displays

This ensures the human has access to the full analysis or plan context when answering agent questions, not just at the final phase gate approval.

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

## Related Files

- `orchestrator/mcp_tools.py` — MCP `get_status` tool; enriches all pending decisions with `draft_content`; enriches `phase_gate` decisions additionally with `completed_agents_summary` and `reviewer_feedback`
- `orchestrator/models.py` — `HITLDecision` model with `decision_type`, `questions`, `phase`, and `content_changed` fields; `content_changed` is set by the orchestrator on re-run phase gates to indicate whether the draft changed since the previous resolved decision (literal string comparison; `None` on first decision, `True`/`False` on subsequent ones)
- `orchestrator/decision_queue.py` — Decision queue handling typed decisions
- `orchestrator/routes/decisions.py` — Decision API endpoints (create, list, resolve)
- `orchestrator/routes/pipelines.py` — Phase gate resolution with JSON payload parsing
- `sandbox/egg_lib/sdlc_hitl.py` — Type-aware terminal HITL handler
- `sandbox/egg_lib/orch_client.py` — `OrchClient.create_decision()` for typed decisions
- `sandbox/egg_lib/contract_cli.py` — CLI for creating decisions and feedback
- `shared/egg_contracts/feedback.py` — Feedback generation and parsing
- `docs/templates/analysis.md` — Template showing decision usage
- `docs/templates/phase-completion.md` — Template for approval format
