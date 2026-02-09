# SDLC Contract

When working in the SDLC pipeline, use the `egg-contract` CLI to track progress.

## Commands

| Command | Purpose |
|---------|---------|
| `egg-contract show` | View current contract state |
| `egg-contract add-commit --task <id> --commit <sha>` | Link commit to task |
| `egg-contract update-notes --task <id> --notes <text>` | Add implementation notes |
| `egg-contract mark-task --task <id> --status <status>` | Mark task status (deprecated) |
| `egg-contract mark-phase --phase <id> --passed <bool>` | Mark phase status (deprecated) |
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

Use the right HITL mechanism for the type of input needed:

### Decisions (Multiple Choice)

Use `add-decision` when you need the human to **choose between discrete options**:

```bash
egg-contract add-decision \
  --question "Which database should we use?" \
  --options "PostgreSQL" "MongoDB" "SQLite"
```

Creates a checkbox-style comment. Human selects one option. Use for:
- Architecture choices (e.g., "REST vs GraphQL")
- Go/no-go decisions (e.g., "Proceed with breaking change?")
- Implementation strategy (e.g., "Option A vs B vs C")

### Feedback (Open-Ended)

Use `add-feedback` when you need **free-form text answers**:

```bash
egg-contract add-feedback \
  --question "What is the expected traffic volume?" \
  --question "Any specific performance requirements?" \
  --format markdown
```

Creates an editable comment with answer sections. Human fills in answers and checks
"Submit feedback" when done. Use for:
- Requirements clarification (e.g., "What edge cases to handle?")
- Context gathering (e.g., "What's the business reason for this?")
- Technical specifications (e.g., "What API rate limits apply?")

### Processing

Both mechanisms use a 30-second debounce to allow humans to edit before processing.
When submitted, the pipeline resumes with the human input available in your prompt
context via `get_submitted_feedback()` or the selected decision option.

## Environment

- `EGG_ISSUE_NUMBER` — current issue (auto-set)
- `EGG_REPO_PATH` — repository path (auto-set)

See contract schema docs at `.egg/schemas/contract.schema.json`.
