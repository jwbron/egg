---
name: run-workflow
description: Guide a full egg pipeline lifecycle — seed prompt, submit, monitor, HITL handling, and completion — using MCP tools (submit_task, get_status, provide_input).
disable-model-invocation: true
argument-hint: "[issue# or description] [--repo owner/name]"
---

# Run Workflow

You are guiding the user through a complete egg pipeline lifecycle using MCP tools. Walk through 5 phases: Seed, Submit, Monitor, HITL, and Complete.

## Phase 1 — Seed

Collect the **repository**, **task description**, and optionally a **GitHub issue number**. Your goal is **zero questions** on the happy path and **at most one question to get started** otherwise (the "Browse recent" flow may need a second to present the issue list).

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
| `/run-workflow --issue 1059` | Issue number (legacy flag, same as bare integer) |

When an issue number is provided, fetch it immediately with `gh issue view <N> --repo <repo> --json title,body` and use the title+body as the task description. Proceed directly to Phase 2 — no questions needed.

When a free-text description is provided and the repo was auto-detected, proceed directly to Phase 2.

### Step 3: Ask only what's missing

If the user ran `/run-workflow` with no arguments, ask a **single** `AskUserQuestion`:

- **Question**: "What should the pipeline work on? Type an issue number or task description below, or browse recent issues."
- **Header**: "Task"
- **Options**:
  - **"Browse recent issues"** — description: "List recent open issues to pick from"
  - **"Help me scope the task"** — description: "Ask clarifying questions about requirements before submitting"

The user will select an option or type in the auto-added "Other" field.

Handle each response:

- **Other (integer)** → Treat as an issue number. Fetch with `gh issue view` and proceed to Phase 2.
- **Other (text)** → Treat as a free-text task description. Proceed to Phase 2.
- **Browse recent issues** → Run `gh issue list --repo <repo> --state open --limit 10 --json number,title` and present the results as a second `AskUserQuestion` with each issue as an option. Then proceed to Phase 2.
- **Help me scope the task** → Ask 1–2 follow-up questions about scope and acceptance criteria, then proceed to Phase 2.

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

When `get_status` returns `pending_decisions`, handle each decision based on its `decision_type`:

Iterate through `pending_decisions` and handle each one individually before resuming monitoring. For each decision, check its `decision_type`:

### For `phase_gate` decisions (phase approval gates):

The `get_status` response enriches phase_gate decisions with `draft_content` (the phase's output document), `completed_agents_summary` (role + status for each completed agent), and `reviewer_feedback` (list of reviewer verdicts).

1. **Show the draft document** — Display the `draft_content` field from the decision. If the content is long, show a summary of the key sections (headings and first paragraph of each) followed by the full content in a collapsed format. If `draft_content` is missing, note that no draft was found.

2. **Show completed agents** — Display `completed_agents_summary` as a compact table:
   ```
   Agents: refiner (complete), reviewer_refine (complete), reviewer_agent_design (complete)
   ```

3. **Show reviewer feedback** — Display each entry from the `reviewer_feedback` list. For each reviewer:
   - Show the reviewer role, verdict, and summary
   - Show analysis if present (this contains the detailed reasoning and is typically the most substantive field)
   - If verdict is NOT "approved", prominently flag it with a warning prefix
   - Show suggestions if present
   - Show blocking feedback if present (verdict "needs_revision")

   Format each reviewer as:
   ```
   ### Reviewer Feedback

   **reviewer_refine** — Approved
   > [summary]
   Analysis: [analysis]
   Suggestions: [suggestions]

   **reviewer_agent_design** — Needs Revision
   > [summary]
   Analysis: [analysis]
   Blocking concerns: [feedback]
   Suggestions: [suggestions]
   ```

   If `reviewer_feedback` is empty or missing, skip this section.

4. **Highlight key disagreements** — If any reviewer has a verdict other than "approved", present a prominent "Key Concerns" section before asking for approval:
   ```
   ### Key Concerns (require attention)
   - **reviewer_agent_design** (needs_revision): [core blocking concern from feedback field]
   ```
   This ensures the human sees blockers before deciding.

5. **Present open questions to the user** — After reviewing the draft content, reviewer feedback, and key concerns, determine whether there are unresolved questions, decisions, or areas where the user's input would be valuable before proceeding. This is a judgment call — use the full context of the draft document, not pattern matching.

   Common situations where you should prompt the user:
   - The draft proposes multiple options/approaches without a clear recommendation
   - The draft explicitly asks for human input on trade-offs or priorities
   - Reviewers raised concerns that require a human judgment call (not just a code fix)
   - The draft mentions risks, unknowns, or assumptions that the user should validate
   - There are scope or strategy decisions that affect downstream phases

   **Deduplication** — Some questions in the draft may also appear as separate `pending_decisions` (choice/feedback type) that you'll handle individually. To avoid double-prompting, compare question text (case-insensitive, ignoring leading/trailing whitespace) against the `question` field of already-resolved decisions and skip matches.

   For each question or decision you identify, present an `AskUserQuestion`:
   - For **decisions with discrete options**: list the options from the draft as choices
   - For **open-ended questions**: use options like "Not sure / skip" and "N/A", letting the user type their answer in the "Other" field
   - Group related questions into a single multi-question `AskUserQuestion` call when possible (up to 4 questions per call)

   **Collect all responses** into a structured summary:
   ```
   ## Resolved Questions

   **<question>**
   Answer: <user's response>
   ```

   If nothing in the draft requires user input beyond the approval itself, skip this step entirely — do not manufacture questions.

6. **Ask for approval** — Use `AskUserQuestion`:
   - **Question**: "Phase '<phase>' is complete. Do you approve the output above to proceed?"
   - **Header**: "Approval"
   - **Options**:
     - **"Approve"** — description: "Proceed to the next phase"
     - **"Request changes"** — description: "Send feedback for agents to address, then re-review"
     - **"Change approach"** — description: "Reject this approach entirely and re-run the phase from scratch with new direction"
     - **"Cancel and re-run pipeline"** — description: "Fundamental issues — cancel this pipeline and start over"
   - The user can also type custom feedback in the "Other" field

7. **Submit the response** — Use structured JSON payloads so the orchestrator can properly route the resolution. Build the JSON based on the user's choice:

   **7a. Build the `context` string** from step 5 responses (if any). Include the "Resolved Questions" summary as a readable string. If no questions were asked in step 5, omit this field.

   **Note**: The `context` field is persisted as part of the raw resolution string in the decision store, but the orchestrator's `_parse_resolution` currently only extracts `action` and `feedback`. The context data is preserved for future use but is not actively routed to agents.

   **7b. Submit based on the user's approval choice**:

   - If **"Approve"** → call `provide_input` with:
     ```json
     {"action": "approve", "context": "<resolved questions from 7a, or omit>"}
     ```

   - If **"Request changes"** → ask a follow-up `AskUserQuestion`:
     - **Question**: "What changes should the agents address?"
     - **Header**: "Feedback"
     - **Options**:
       - **"Address reviewer concerns"** — description: "Fix the blocking issues flagged by reviewers above"
       - **"See my notes below"** — description: "I'll type specific feedback"
     Then call `provide_input` with:
     ```json
     {"action": "request_changes", "feedback": "<user's feedback text>", "context": "<resolved questions from 7a, or omit>"}
     ```

   - If **"Change approach"** → ask a follow-up `AskUserQuestion`:
     - **Question**: "What direction should the agents take instead?"
     - **Header**: "Direction"
     - **Options**:
       - **"Try a completely different approach"** — description: "Let agents explore alternatives"
       - **"See my notes below"** — description: "I'll describe the approach I want"
     Then call `provide_input` with:
     ```json
     {"action": "change_approach", "feedback": "<user's direction text>", "context": "<resolved questions from 7a, or omit>"}
     ```
     This resets the current phase and re-runs it with the new direction. Note: in the current orchestrator, `change_approach` and `request_changes` both result in `is_approved=False` and trigger a phase reset. The distinct UX framing encourages users to provide different types of feedback (incremental fixes vs. directional pivots), but the orchestrator processing is the same.

   - If **"Cancel and re-run pipeline"** → confirm with the user ("This will cancel the current pipeline and start a new one. Proceed?"), then:
     1. Call `cancel_task` with `task_id` and `cleanup: true`
     2. Ask the user if they want to modify the original task description
     3. Call `submit_task` with the (possibly updated) description
     4. Resume from Phase 3 (Monitor) with the new `task_id`

     **Error handling**: If `cancel_task` fails, inform the user and offer to retry. If `cancel_task` succeeds but `submit_task` fails, inform the user that the previous pipeline was cancelled and offer to retry the submission — do not leave the user stranded with a cancelled pipeline and no replacement.

   - If **custom text (Other)** → treat as request_changes feedback. Call `provide_input` with:
     ```json
     {"action": "request_changes", "feedback": "<user's text>", "context": "<resolved questions from 7a, or omit>"}
     ```

After resolving this decision, move to the next pending decision (if any) before resuming monitoring.

### For `choice` type decisions:
Show the decision's `question` and `context` (if non-empty) prominently, then use `AskUserQuestion` to present the options:
- Question: the decision's `question` field
- Options: the decision's `options` array (each as a label with empty description)

### For `feedback` type decisions:
Show the decision's `question` and `context` (if non-empty) prominently, then use `AskUserQuestion` with a free-text option:
- Present the question to the user
- Collect their response

### After handling each `choice` or `feedback` decision:

Call the `provide_input` MCP tool (`phase_gate` decisions already handle this in step 7 above):

```
Tool: provide_input
Arguments:
  task_id: <task_id>
  decision_id: <decision_id>
  response: <structured JSON — see below>
```

For `choice` decisions, send:
```json
{"action": "select", "selected": "<chosen option>"}
```

For `feedback` decisions, send:
```json
{"action": "submit_feedback", "answers": {"q1": "<answer>", "q2": "<answer>"}}
```

Confirm the input was submitted, then proceed to the next pending decision. Once all decisions are resolved, resume monitoring (Phase 3).

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
- Offer the user a choice via `AskUserQuestion`:
  - **"Re-run pipeline"** — description: "Cancel this pipeline and start a new one with the same task"
  - **"Re-run with changes"** — description: "Cancel and start a new pipeline with modified description"
  - **"Done"** — description: "No further action needed"

  If re-running:
  1. Call `cancel_task` with the failed `task_id` and `cleanup: true`
  2. If "Re-run with changes", ask the user for the updated description
  3. Call `submit_task` with the description (original or updated) and same repo/issue
  4. Resume from Phase 3 (Monitor) with the new `task_id`

  **Error handling**: If `cancel_task` or `submit_task` fails, inform the user and offer to retry the failed step.

## Critical Rules

- **Always use MCP tools** (`submit_task`, `get_status`, `provide_input`) — never call orchestrator APIs directly
- **Never skip HITL** — always present decisions to the user and wait for their response
- **Stop polling on exit** — always exit the monitoring loop when the workflow ends
- **Handle errors gracefully** — if an MCP tool call fails, inform the user and offer to retry
- **Keep output concise** — don't flood the user with raw JSON; format status as a readable dashboard
