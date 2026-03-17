---
name: run-task
description: Submit a task with a coder+reviewer pair — plans, implements, and reviews via BRC consensus — then surfaces the resulting PR link.
disable-model-invocation: true
argument-hint: "<task description> [--repo owner/name]"
---

# Run Task

You are guiding the user through a lightweight pipeline that implements and reviews code using two concurrent agents (coder + reviewer) coordinating via BRC consensus. Walk through 4 phases: Seed, Submit, Monitor, and Complete.

## Phase 1 — Seed

Collect the **repository** and **task description**. Your goal is **zero questions** on the happy path and **at most one question** otherwise.

### Step 1: Auto-detect the repository (NEVER ask if detectable)

Before asking the user anything, try to detect the repo automatically:

1. Run `git -C "$EGG_REPO_PATH" remote get-url origin 2>/dev/null` (or fall back to `git remote -v` from the working directory)
2. Parse the `owner/name` from the URL (e.g. `https://github.com/jwbron/egg.git` → `jwbron/egg`)
3. If a `--repo` flag was passed, use that instead

Only ask for the repo if detection fails AND no `--repo` flag was provided.

### Step 2: Parse arguments (skip questions when possible)

If the user provided arguments after `/run-task`, parse them:

| Input | Interpretation |
|-------|---------------|
| `/run-task Add retry logic to the API client` | Free-text task description |
| `/run-task --repo jwbron/egg Fix flaky test` | Repo override + task description |

When a free-text description is provided and the repo was auto-detected, proceed directly to Phase 2.

### Step 3: Ask only what's missing

If the user ran `/run-task` with no arguments, ask a **single** `AskUserQuestion`:

- **Question**: "What task should the agent implement and open a PR for?"
- **Header**: "Task"
- **Options**:
  - **"Help me scope the task"** — description: "Ask clarifying questions about requirements before submitting"

The user will type their description in the auto-added "Other" field, or select the scoping option.

Handle each response:

- **Other (text)** → Treat as a free-text task description. Proceed to Phase 2.
- **Help me scope the task** → Ask 1–2 follow-up questions about scope and acceptance criteria, then proceed to Phase 2.

**Never ask for the repo and the task in separate questions.** If the repo could not be auto-detected, include a repo question in the same `AskUserQuestion` call (multi-question mode).

## Phase 2 — Submit

Call the `submit_task` MCP tool with the gathered parameters and a config that skips to implement with a coder+reviewer pair:

```
Tool: submit_task
Arguments:
  description: <task description>
  repo: <owner/name>
  config: {"start_phase": "implement", "implement_roles": ["coder", "reviewer_code"], "hitl_gates": false, "overseer_enabled": false}
```

The `config` tells the orchestrator to skip refine/plan, run only a coder and reviewer_code in BRC consensus, and disable HITL gates and the overseer.

Store the returned `task_id`. Confirm submission to the user:

> Task submitted — coder + reviewer, no gates.
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
Agents: <agent statuses from concurrent.agents if available>
Consensus: <confirmed count>/<total> confirmed
Recent: <latest message subject from recent_messages>
```

When `concurrent` data is available in the status response, use it to show agent states and consensus progress. When not available, fall back to basic status:

```
--- Pipeline Status ---
Phase: <current_phase> | Status: <status>
Recent: <latest message subject from recent_messages>
```

3. Check for state transitions:
   - If `status` is `complete` → exit the loop, move to Phase 4
   - If `status` is `failed` → exit the loop, move to Phase 4
   - If `pending_decisions` is non-empty → handle inline (see below)

Keep the dashboard output concise. Only show changes from the previous poll when possible.

### Stall detection

Track the `current_phase` and latest `recent_messages` entry across polls. If **10 consecutive polls** (~10 minutes) pass with no phase change and no new messages, surface a warning:

```
### Potential Stall Detected

Pipeline has shown no progress for ~10 minutes.
```

Then offer three options via `AskUserQuestion`:

- **"Check logs"** — description: "Run diagnostic commands to investigate" — run `egg-orch container list <task_id>` followed by `egg-orch container logs <task_id> <container_id> --lines 50` for the agent container
- **"Wait longer"** — description: "Give the agent more time (resets the stall counter)"
- **"Cancel"** — description: "Cancel this pipeline"

If "Wait longer" is selected, reset the stall counter and resume monitoring. If "Cancel", call `cancel_task` and move to Phase 4 failure handling.

### NACK handling

If the status shows unresolved NACKs in the consensus data (e.g. `concurrent.consensus.has_objections` is true), surface them to the user:

> **Reviewer raised concerns** — the coder is iterating on feedback. This is normal BRC behavior.

Only escalate if NACKs persist across 5+ consecutive polls with no progress, at which point offer the same stall detection options.

### Handling unexpected decisions

With `hitl_gates: false`, decisions should not appear. But if they do, handle them gracefully:

- For `choice` type: present the options via `AskUserQuestion`, then call `provide_input` with `{"action": "select", "selected": "<chosen option>"}` serialized as a JSON string.
- For `feedback` type: present the questions, collect answers, then call `provide_input` with `{"action": "submit_feedback", "answers": {"<id>": "<answer>"}}` serialized as a JSON string.
- For `phase_gate` type: auto-approve by calling `provide_input` with `{"action": "approve"}` serialized as a JSON string. Log the phase and decision ID, and inform the user:

  > **Auto-approved phase gate** — Phase: `<phase>`, Decision ID: `<decision_id>`. This should not occur with `hitl_gates: false`; if it recurs, investigate the pipeline configuration.

After resolving any decisions, resume monitoring.

## Phase 4 — Complete

The monitoring loop has exited. Summarize:

### On success:
```
Pipeline Complete
Status: Success
```

- Show PR link if available in the pipeline data
- If no PR link is found, check `gh pr list --repo <repo> --state open --json number,title,url --limit 5` to find a recently created PR

### On failure:
```
Pipeline Failed
Phase: <phase where failure occurred>
```

- Show error information if available
- Offer the user a choice via `AskUserQuestion`:
  - **"Re-run"** — description: "Cancel this pipeline and start a new one with the same task"
  - **"Re-run with changes"** — description: "Cancel and start a new pipeline with modified description"
  - **"Done"** — description: "No further action needed"

  If re-running:
  1. Call `cancel_task` with the failed `task_id` and `cleanup: true`
  2. If "Re-run with changes", ask the user for the updated description
  3. Call `submit_task` with the description (original or updated) and same repo, plus the same `config`
  4. Resume from Phase 3 (Monitor) with the new `task_id`

  **Error handling**: If `cancel_task` or `submit_task` fails, inform the user and offer to retry the failed step.

## Troubleshooting

When investigating a stuck or failed pipeline, use these diagnostic commands:

| Scenario | Command |
|----------|---------|
| Pipeline status / current phase | `egg-orch pipeline status <task_id>` |
| List agent containers | `egg-orch container list <task_id>` |
| View agent logs | `egg-orch container logs <task_id> <container_id> --lines 100` |
| Orchestrator + gateway health | `egg-orch health` |
| BRC consensus status | `egg-orch consensus status <task_id>` |
| Live pipeline visualization | `egg-pipeline-watch <task_id> --once` |
| Prior agent sessions | `egg-checkpoint list --pipeline <task_id>` |

## Critical Rules

- **Always use MCP tools** (`submit_task`, `get_status`, `provide_input`, `cancel_task`) — never call orchestrator APIs directly
- **Always serialize JSON payloads as strings** for `provide_input` — the `response` parameter is a string, not an object
- **Always pass `config`** with `{"start_phase": "implement", "implement_roles": ["coder", "reviewer_code"], "hitl_gates": false, "overseer_enabled": false}` when calling `submit_task`
- **Auto-approve phase gates** — this is a no-HITL flow; if a gate appears, approve it automatically and inform the user
- **Stop polling on exit** — always exit the monitoring loop when the workflow ends
- **Handle errors gracefully** — if an MCP tool call fails, inform the user and offer to retry
- **Keep output concise** — don't flood the user with raw JSON; format status as a readable dashboard
