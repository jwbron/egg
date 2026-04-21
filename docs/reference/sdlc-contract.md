# SDLC Contract

When working in the SDLC pipeline, use the `egg-contract` CLI to track progress.

## Commands

| Command | Purpose |
|---------|---------|
| `egg-contract show` | View current contract state |
| `egg-contract complete-task --task <id> [--commit <sha>]` | Mark task done (optionally link commit) |
| `egg-contract complete-phase --phase <id> [--commit <sha>]` | Mark phase done (optionally link commit) |
| `egg-contract add-commit --task <id> --commit <sha>` | Link commit to task without marking done |
| `egg-contract update-notes --task <id> --notes <text>` | Add implementation notes |
| `egg-contract add-decision --question <text> [--phase <phase>]` | Create HITL decision (multiple choice); scoped to current phase by default |
| `egg-contract add-feedback --question <text>...` | Create feedback comment (open-ended) |

## Workflow

Update the contract incrementally after each commit — do not batch updates at the end.

1. Check current phase and tasks: `egg-contract show`
2. Work on assigned tasks
3. After each commit, mark the task done: `egg-contract complete-task --task task-1 --commit abc1234`
4. After completing all tasks in a phase, mark the phase done: `egg-contract complete-phase --phase phase-1 --commit abc1234`
5. Add notes if needed: `egg-contract update-notes --task task-1 --notes "Implemented X"`
6. If blocked on a choice, create a decision: `egg-contract add-decision --question "Which approach?" --options "A" "B"`
7. If you need open-ended input: `egg-contract add-feedback --question "What is expected volume?" --format markdown`

## HITL: Decisions vs Feedback

### Decisions (Multiple Choice)

Use `add-decision` when you need the human to **choose between discrete options**:

```bash
egg-contract add-decision \
  --question "Which database should we use?" \
  --options "PostgreSQL" "MongoDB" "SQLite"
```

The `--phase` flag (optional) scopes the decision to a specific pipeline phase (`refine`, `plan`, `implement`, `pr`). Defaults to the contract's `current_phase`. Phase-scoped decisions block `complete_phase` until resolved — the endpoint returns 409 if any decisions for the current phase remain open.

Use for: architecture choices, go/no-go decisions, implementation strategy.

### Feedback (Open-Ended)

Use `add-feedback` when you need **free-form text answers**:

```bash
egg-contract add-feedback \
  --question "What is the expected traffic volume?" \
  --question "Any specific performance requirements?" \
  --format markdown
```

Use for: requirements clarification, context gathering, technical specifications.

### Processing

Both mechanisms use a 30-second debounce to allow humans to edit before processing.
When submitted, the pipeline resumes with the human input available in your prompt
context via `get_submitted_feedback()` or the selected decision option.

## Contract State in Concurrent Mode

In concurrent execution mode, contracts are stored in the **shared pipeline worktree** (`/home/egg/.egg-worktrees/<pipeline_id>/<repo>/`) rather than in per-agent worktrees. All `egg-contract` commands route through the gateway, which proxies to the orchestrator's `/api/v1/contracts/` endpoints. The orchestrator is the single source of truth for contract state, ensuring all agents (producer and reviewers) observe the same contract regardless of which per-agent worktree they run in.

No additional configuration is needed — `EGG_PIPELINE_ID` is set automatically in pipeline containers and is used to locate the shared worktree.

## Environment

- `EGG_ISSUE_NUMBER` — current GitHub issue number (auto-set in issue-driven pipelines)
- `EGG_PIPELINE_ID` — pipeline ID string for JIRA-ticket pipelines (auto-set)
- `EGG_REPO_PATH` — repository path (auto-set)
- `CONTAINER_ID` — container identifier, passed to gateway for request context (auto-set in pipeline containers)

When neither `EGG_ISSUE_NUMBER` nor `EGG_PIPELINE_ID` is set, all commands require `--issue` or `--pipeline-id`.

See contract schema docs at `.egg/schemas/contract.schema.json`.
