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
| `egg-contract add-decision --question <text>` | Create HITL decision |

## Workflow

1. Check current phase and tasks: `egg-contract show`
2. Work on assigned tasks
3. Link commits as you complete work: `egg-contract add-commit --task task-1 --commit abc1234`
4. Add notes if needed: `egg-contract update-notes --task task-1 --notes "Implemented X"`
5. If blocked, create a decision point: `egg-contract add-decision --question "How should X be handled?"`

## Environment

- `EGG_ISSUE_NUMBER` — current issue (auto-set)
- `EGG_REPO_PATH` — repository path (auto-set)

See contract schema docs at `.egg/schemas/contract.schema.json`.
