# SDLC Contract

Use `egg-contract` to track SDLC pipeline progress. Full reference: `$EGG_REPO_PATH/docs/reference/sdlc-contract.md`

**Commands:**

| Command | Purpose |
|---------|---------|
| `egg-contract show` | View current contract state |
| `egg-contract add-commit --task <id> --commit <sha>` | Link commit to task |
| `egg-contract complete-task --task <id> [--commit <sha>]` | Mark task as complete (optionally link commit) |
| `egg-contract complete-phase --phase <id> [--commit <sha>]` | Mark phase as complete (optionally link commit) |
| `egg-contract update-notes --task <id> --notes <text>` | Add implementation notes |
| `egg-contract add-decision --question <text> --options "A" "B"` | Create HITL decision (multiple choice) |
| `egg-contract add-feedback --question <text> --format markdown` | Create feedback request (open-ended) |

**Workflow**: `egg-contract show` → work on tasks → `complete-task` after each task → `complete-phase` after each phase → `add-decision` or `add-feedback` if blocked.

**Env**: `EGG_ISSUE_NUMBER`, `EGG_REPO_PATH` (auto-set).
