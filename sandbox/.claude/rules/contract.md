# Contract CLI Usage

## Purpose

The `egg-contract` CLI tracks progress through the SDLC pipeline. Use it to:
- View current contract state
- Link commits to tasks
- Add implementation notes
- Request human decisions

## Commands

### View contract state
```bash
egg-contract show              # Human-readable summary
egg-contract show --json       # Full JSON output
```

### Link commit to task (implementer)
```bash
egg-contract add-commit --task task-1 --commit abc1234
```

### Add implementation notes (implementer)
```bash
egg-contract update-notes --task task-1 --notes "Implemented via facade pattern"
```

### Request human decision
```bash
egg-contract add-decision --question "Approve the implementation approach?"
egg-contract add-decision --question "Which option?" --options "Option A,Option B,Option C"
```

## Role Restrictions

- **Implementer**: Can add commits and notes, cannot mark tasks complete
- **Reviewer**: Can mark tasks complete/incomplete with feedback
- **Human**: Can resolve decisions and advance phases

If a command fails with "not authorized", it requires a different role.

## When to Use

1. After completing a task: `egg-contract add-commit --task task-X --commit $(git rev-parse HEAD)`
2. When needing human input: `egg-contract add-decision --question "..."`
3. To check current state: `egg-contract show`
