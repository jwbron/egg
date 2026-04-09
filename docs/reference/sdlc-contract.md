# SDLC Contract

When working in the SDLC pipeline, use the `egg-contract` CLI to track progress.

## Commands

| Command | Purpose |
|---------|---------|
| `egg-contract show` | View current contract state |
| `egg-contract add-commit --task <id> --commit <sha>` | Link commit to task |
| `egg-contract update-notes --task <id> --notes <text>` | Add implementation notes |
| `egg-contract add-decision --question <text>` | Create HITL decision (multiple choice) |
| `egg-contract add-feedback --question <text>...` | Create feedback comment (open-ended) |

## Workflow

1. Check current phase and tasks: `egg-contract show`
2. Work on assigned tasks
3. Link commits as you complete work: `egg-contract add-commit --task task-1 --commit abc1234`
4. Add notes if needed: `egg-contract update-notes --task task-1 --notes "Implemented X"`
5. If blocked on a choice, create a decision: `egg-contract add-decision --question "Which approach?" --options "A" "B"`
6. If you need open-ended input: `egg-contract add-feedback --question "What is expected volume?" --format markdown`

## HITL: Decisions vs Feedback

### Decisions (Multiple Choice)

Use `add-decision` when you need the human to **choose between discrete options**:

```bash
egg-contract add-decision \
  --question "Which database should we use?" \
  --options "PostgreSQL" "MongoDB" "SQLite"
```

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

## Per-Agent Worktree Support

All `egg-contract` commands work correctly from per-agent worktrees in concurrent execution mode. The CLI automatically sends the `CONTAINER_ID` environment variable to the gateway, which maps the container's repo path to the correct worktree path before loading or saving contracts.

No additional configuration is needed — the `CONTAINER_ID` env var is set automatically in pipeline containers.

## Environment

- `EGG_ISSUE_NUMBER` — current GitHub issue number (auto-set in issue-driven pipelines)
- `EGG_PIPELINE_ID` — pipeline ID string for JIRA-ticket pipelines (auto-set)
- `EGG_REPO_PATH` — repository path (auto-set)
- `CONTAINER_ID` — container identifier for worktree path resolution (auto-set in pipeline containers)

When neither `EGG_ISSUE_NUMBER` nor `EGG_PIPELINE_ID` is set, all commands require `--issue` or `--pipeline-id`.

See contract schema docs at `.egg/schemas/contract.schema.json`.
