# Run Workflow

You are guiding the user through a complete egg pipeline lifecycle using MCP tools. Walk through 5 phases: Seed, Submit, Monitor, HITL, and Complete.

## Phase 1 — Seed

Gather task parameters from the user. You need:

- **Task description** (required) — what should be done
- **Repository** (required) — in `owner/name` format
- **Issue number** (optional) — GitHub issue to work from
- **Workflow hint** (optional) — `bug_fix`, `feature`, or `refactor`
- **Urgency** (optional) — `low`, `normal` (default), or `high`

Use `AskUserQuestion` if the user's description is vague or ambiguous. Clarify scope, expected behavior, or acceptance criteria before proceeding.

If the user provided arguments after `/run-workflow`, parse them:
- First argument: task description or issue number
- `--repo owner/name`: repository
- `--issue N`: issue number

If the user provided a clear description and repo, skip straight to Phase 2.

## Phase 2 — Submit

Call the `submit_task` MCP tool with the gathered parameters:

```
Tool: submit_task
Arguments:
  description: <task description>
  repo: <owner/name>
  issue_number: <number, if provided>
  workflow_hint: <hint, if provided>
  urgency: <level, if provided>
```

Store the returned `task_id`. Confirm submission to the user:

> Task submitted successfully.
> **Task ID**: `<task_id>`
> **Description**: <description>
> **Repository**: <repo>

## Phase 3 — Monitor

Poll the pipeline status in a loop. Wait 60 seconds between each poll. On each poll:

1. Call the `get_status` MCP tool with the `task_id`
2. Display a compact status dashboard:

```
--- Pipeline Status ---
Phase: <current_phase> | Status: <status>
Agents: <running count> running, <completed count> completed
Recent: <latest message subject from recent_messages>
```

3. Check for state transitions:
   - If `pending_decisions` is non-empty → move to Phase 4 (HITL)
   - If `status` is `complete` → exit the loop, move to Phase 5
   - If `status` is `failed` → exit the loop, move to Phase 5

Keep the dashboard output concise. Only show changes from the previous poll when possible.

## Phase 4 — HITL (Human-in-the-Loop)

When `get_status` returns `pending_decisions`, handle each decision:

### For `choice` type decisions:
Use `AskUserQuestion` to present the options:
- Question: the decision's `question` field
- Options: the decision's `options` array

### For `feedback` type decisions:
Use `AskUserQuestion` with a free-text option:
- Present the question to the user
- Collect their response

After getting the user's answer, call the `provide_input` MCP tool:

```
Tool: provide_input
Arguments:
  task_id: <task_id>
  decision_id: <decision_id>
  response: <user's answer>
```

Confirm the input was submitted, then resume monitoring (Phase 3).

## Phase 5 — Complete

The monitoring loop has exited. Summarize:

### On success:
```
Pipeline Complete
Status: Success
Phase: <final phase>
```

- Show PR link if available in the pipeline data
- List agents that ran (from `completed_agents`)
- Note any agents that failed

### On failure:
```
Pipeline Failed
Status: Failed
Phase: <phase where failure occurred>
```

- Show error information if available
- List what completed before failure
- Offer to show agent logs or re-run the task

## Critical Rules

- **Always use MCP tools** (`submit_task`, `get_status`, `provide_input`) — never call orchestrator APIs directly
- **Never skip HITL** — always present decisions to the user and wait for their response
- **Stop polling on exit** — always exit the monitoring loop when the workflow ends
- **Handle errors gracefully** — if an MCP tool call fails, inform the user and offer to retry
- **Keep output concise** — don't flood the user with raw JSON; format status as a readable dashboard
