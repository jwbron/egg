---
name: run-workflow
description: Guide a full egg pipeline lifecycle — seed prompt, submit, monitor, HITL handling, and completion — using MCP tools (submit_task, get_status, provide_input).
disable-model-invocation: true
argument-hint: "[issue# or description] [--repo owner/name]"
---

# Run Workflow

You are guiding the user through a complete egg pipeline lifecycle using MCP tools. Walk through 5 phases: Seed, Submit, Monitor, HITL, and Complete.

## Phase 1 — Seed

Collect the **repository**, **task description**, and optionally a **GitHub issue number**. Your goal is **zero questions** on the happy path and **at most one** otherwise.

### Step 1: Auto-detect the repository (NEVER ask if detectable)

Before asking the user anything, try to detect the repo automatically:

1. Run `git -C "$EGG_REPO_PATH" remote get-url origin 2>/dev/null` (or fall back to `git remote -v` from the working directory)
2. Parse the `owner/name` from the URL (e.g. `https://github.com/jwbron/egg.git` → `jwbron/egg`)
3. If a `--repo` flag was passed, use that instead

Only ask for the repo if detection fails AND no `--repo` flag was provided.

### Step 2: Parse arguments (skip questions when possible)

If the user provided arguments after `/run-workflow`, parse them:

| Input | Interpretation |
|-------|---------------|
| `/run-workflow 1059` | Issue number (bare integer) |
| `/run-workflow #1059` | Issue number (with hash) |
| `/run-workflow Add retry logic for API calls` | Free-text task description |
| `/run-workflow --repo jwbron/egg 1059` | Repo override + issue number |

When an issue number is provided, fetch it immediately with `gh issue view <N> --repo <repo> --json title,body` and use the title+body as the task description. Proceed directly to Phase 2 — no questions needed.

When a free-text description is provided and the repo was auto-detected, proceed directly to Phase 2.

### Step 3: Ask only what's missing (one question, max)

If the user ran `/run-workflow` with no arguments, ask a **single** `AskUserQuestion`:

- **Question**: "What should the pipeline work on?"
- **Header**: "Task"
- **Options**:
  - **"Issue number"** — description: "Enter a GitHub issue number (e.g. 1059)"
  - **"Describe task"** — description: "Type a free-text task description"
  - **"Browse recent"** — description: "List recent open issues to pick from"

Handle each response:

- **Issue number** → The user types a number in the "Other" text field. Fetch the issue with `gh issue view` and proceed to Phase 2.
- **Describe task** → The user types a description. Proceed to Phase 2.
- **Browse recent** → Run `gh issue list --repo <repo> --state open --limit 10 --json number,title` and present the results as a second `AskUserQuestion` with each issue as an option. Then proceed to Phase 2.

**Never ask for the repo and the task in separate questions.** If the repo could not be auto-detected, include a repo question in the same `AskUserQuestion` call (multi-question mode).

## Phase 2 — Submit

Call the `submit_task` MCP tool with the gathered parameters:

```
Tool: submit_task
Arguments:
  description: <task description>
  repo: <owner/name>
  issue_number: <number, if provided>
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
