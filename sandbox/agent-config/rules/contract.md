# SDLC Contract

Use `egg-contract` to track SDLC pipeline progress. Full reference: `$EGG_REPO_PATH/docs/reference/sdlc-contract.md`

**Commands:**

| Command | Purpose |
|---------|---------|
| `egg-contract show` | View current contract state |
| `egg-contract add-commit --task <id> --commit <sha>` | Link commit to task |
| `egg-contract update-notes --task <id> --notes <text>` | Add implementation notes |
| `egg-contract add-decision --question <text> --options "A" "B"` | Create HITL decision (multiple choice) |
| `egg-contract add-feedback --question <text> --format markdown` | Create feedback request (open-ended) |

**Workflow**: `egg-contract show` → work on tasks → `add-commit` as you go → `add-decision` or `add-feedback` if blocked.

**Env**: `EGG_ISSUE_NUMBER`, `EGG_REPO_PATH` (auto-set).
