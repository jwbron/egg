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

3. **Check consensus health** — see [Consensus Monitoring](#consensus-monitoring) below. Use `concurrent.consensus` if present; otherwise fall back to message-based tracking (see [Consensus Fallback](#consensus-fallback-when-concurrentconsensus-is-missing)).

4. Check for state transitions:
   - If `pending_decisions` is non-empty → move to Phase 4 (HITL)
   - If `status` is `complete` → exit the loop, move to Phase 5
   - If `status` is `failed` → apply the **failed status grace period** (see below) before exiting

5. **Track elapsed time** — Record the wall-clock time when the current phase started. Use this for [Long-Running Phase Detection](#long-running-phase-detection).

**Important: Run polling sleeps in the foreground (blocking).** Do not use background sleeps or `run_in_background` for the 60-second poll interval. Background sleeps provide no benefit since the next action (polling) depends on the sleep completing, and they cause notification spam if the user interrupts.

Keep the dashboard output concise. Only show changes from the previous poll when possible.

### Failed Status Grace Period

During phase cycle transitions (e.g., plan phase review cycles), the orchestrator may briefly report `status: failed` while spawning new containers. Treating this as terminal prematurely ends monitoring.

**Before treating `failed` as terminal, apply these checks:**

1. If `status` is `failed` but `running_agents` is non-empty → treat as "transitioning", not failed. Log: `"Status shows failed but agents still running — treating as cycle transition."` Continue polling.
2. If `status` is `failed` and `running_agents` is empty → run `egg-pipeline-watch <task_id> --once --compact` to confirm actual state before exiting. If the pipeline watch shows active work, continue polling.
3. Only exit to Phase 5 when `status` is `failed`, `running_agents` is empty, **and** the secondary check confirms the pipeline is genuinely stopped.

### Post-Consensus Reviewer Behavior

After BRC consensus completes in a phase, the orchestrator may spawn a **post-consensus reviewer** for a final review pass. If this reviewer requests changes, it triggers a new review cycle (new containers are spawned). This is a known pattern — track it as a cycle transition, not a failure. To detect this, compare the `running_agents` count between consecutive polls — if new agents appear after consensus was complete, a post-consensus review cycle has started. Update the dashboard:

```
Note: Post-consensus review triggered — new review cycle started.
```

### Consensus Monitoring

When the pipeline uses concurrent agents (BRC protocol), the `get_status` response may include a `concurrent.consensus` object. On each poll cycle, check this data for red flags and surface problems to the user before they escalate.

**Enhanced dashboard** — When consensus data is present, extend the status display:

```
--- Pipeline Status ---
Phase: <current_phase> | Status: <status>
Agents: <running count> running, <completed count> completed
Consensus: <N>/<total> confirmed | Blocking: <role1>, <role2>
Recent: <latest message subject>
```

If `has_unresolved_nacks` is true, add:
```
NACKs: <reviewer> → <producer>: "<reason>"
```

### Consensus Fallback (when `concurrent.consensus` is missing)

The `concurrent.consensus` object may not be present in all `get_status` responses. When it is absent, **fall back to message-based consensus tracking** by classifying entries in `recent_messages`:

1. **Classify messages using the `type` field** (primary) — each `recent_messages` entry includes a `type` field with reliable enum values: `CONSENSUS_PROPOSE`, `CONSENSUS_ACK`, `CONSENSUS_NACK`, `CONSENSUS_CONFIRMED`. Use these for classification, not subject parsing.
2. **Identify roles using the `from_role` field** — each message includes `from_role` indicating which agent sent it.
3. Maintain an in-memory map of `{role: {last_message_type, last_message_time, message_count}}` built from `recent_messages`
4. Infer consensus state: if all roles listed in `running_agents` have sent `CONSENSUS_CONFIRMED` messages, consensus is likely complete
5. For the enhanced dashboard, approximate the fields:
   - Confirmed count: roles with `CONSENSUS_CONFIRMED` messages
   - Blocking: roles with no `CONSENSUS_CONFIRMED` message
   - Unresolved NACKs: `CONSENSUS_NACK` messages not followed by a `CONSENSUS_PROPOSE` from the producer
6. Use `subject` only for supplementary detail (e.g., extracting NACK reasons or human-readable context for the dashboard)

**Stall detection** — Track agent phase progression across consecutive polls. Flag an agent as potentially stalled when:
- It has been in `producer_phase: WORKING` for 3+ consecutive polls (~3 minutes) while other agents have progressed
- It has been in `producer_phase: PROPOSED` for 3+ consecutive polls with no reviewer activity (reviewers still in `WORKING`)
- A NACK has been unresolved for 3+ consecutive polls (producer hasn't re-proposed)

Note: 3 polls × 60s = ~3 minutes is a baseline threshold. Code generation, test execution, and large diffs can legitimately exceed this. Adjust the threshold based on pipeline complexity — for pipelines with heavy test suites or large codebases, consider using 5+ polls before flagging. The "Wait longer" option mitigates false positives.

**Silent agent detection** — Separately from phase-based stall detection, track agents that never enter the consensus protocol at all. Flag an agent as "silent" when:
- It has been in `running_agents` for 10+ polls (~10 minutes)
- It has **zero messages** in `recent_messages` (no proposals, ACKs, NACKs, or confirmations)
- This catches agents that are running but not participating in BRC — a different failure mode from agents stuck in a specific phase

When a silent agent is detected, include it in the stall alert with distinct framing:

```
### Silent Agent Detected

**<role>** has been running for ~<N> minutes with no BRC messages.
This agent may have failed to initialize or enter the consensus protocol.
```

When a stall is detected, alert the user with context:

```
### Potential Stall Detected

**<role>** has been in <phase> for ~<N> minutes with no progress.
```

Then use `AskUserQuestion` to offer options:
- **Question**: "Agent '<role>' appears stalled in <phase>. How would you like to proceed?"
- **Header**: "Stall"
- **Options**:
  - **"Check agent logs"** — description: "View recent logs to diagnose the issue"
  - **"Wait longer"** — description: "Give it more time — may be doing legitimate long-running work"
  - **"Nudge agent"** — description: "Send a message asking the agent to report status"

Handle each response:
- **Check agent logs** → Run `egg-orch container list <task_id>` to find the container ID, then `egg-orch container logs <task_id> <container_id> --lines 50`. Show the user the last 50 lines and let them decide next steps.
- **Wait longer** → Reset the stall counter for this agent. Resume monitoring.
- **Nudge agent** → Run `egg-orch message send <task_id> --role overseer --to <role> --type STATUS --body "Overseer check: you appear stalled in <phase>. Please send a heartbeat or progress update."` and resume monitoring. If the agent remains stalled for another 3 polls after the nudge, re-alert the user with stronger options (see escalation below).

**NACK escalation** — When an unresolved NACK persists for 3+ polls, surface it prominently:

```
### Unresolved NACK

**<reviewer>** NACKed **<producer>**: "<reason>"
This has been unresolved for ~<N> minutes. The producer has not re-proposed.
```

Then use `AskUserQuestion` to offer options:
- **Question**: "Unresolved NACK from <reviewer> → <producer> has persisted for ~<N> minutes. How would you like to proceed?"
- **Header**: "NACK"
- **Options**:
  - **"Check producer logs"** — description: "View the producer's recent logs to see if it's working on fixes"
  - **"Check reviewer logs"** — description: "View the reviewer's full reasoning for the NACK"
  - **"Nudge producer"** — description: "Send a message asking the producer to address the NACK and re-propose"
  - **"Wait longer"** — description: "The producer may be working on fixes — give it more time"

Handle each response:
- **Check producer logs** → Run `egg-orch container list <task_id>`, find the producer's container, then `egg-orch container logs <task_id> <container_id> --lines 50`. Show the output and let the user decide next steps.
- **Check reviewer logs** → Same approach, but for the reviewer's container.
- **Nudge producer** → Run `egg-orch message send <task_id> --role overseer --to <producer_role> --type STATUS --body "Overseer check: unresolved NACK from <reviewer> — please address and re-propose."` and resume monitoring.
- **Wait longer** → Reset the NACK stall counter. Resume monitoring.

**Post-nudge escalation** — If an agent remains stalled after a nudge (3+ more polls with no change), use `AskUserQuestion` to offer stronger actions:
- **Question**: "Agent '<role>' is still unresponsive after nudge (~<N> minutes total). How would you like to proceed?"
- **Header**: "Escalate"
- **Options**:
  - **"View full agent logs"** — description: "Show extended logs (`egg-orch container logs` with `--lines 200`) to diagnose the issue"
  - **"Restart pipeline"** — description: "Cancel this pipeline and re-submit the task to get a fresh agent"
  - **"Continue waiting"** — description: "Reset the counter and keep monitoring"

Handle each response:
- **View full agent logs** → Run `egg-orch container list <task_id>` to find the container, then `egg-orch container logs <task_id> <container_id> --lines 200`. Show the output and let the user decide next steps.
- **Restart pipeline** → Confirm with the user, then call `cancel_task` with `task_id` and `cleanup: true`, followed by `submit_task` with the original parameters. Resume from Phase 3 with the new `task_id`.
- **Continue waiting** → Reset the stall counter. Resume monitoring.

**State tracking** — Maintain a simple in-memory map of `{role: {phase, polls_in_phase, nudged, total_polls_seen, has_any_messages}}` across poll cycles, plus a top-level `running_agent_count` to track the number of running agents between polls (for detecting post-consensus reviewer spawns). Reset a role's `polls_in_phase` counter whenever its phase changes or new messages appear from it in `recent_messages`. Increment `total_polls_seen` on every poll. Set `has_any_messages` to true when any message from the role appears in `recent_messages`. This is lightweight — no persistence needed since it only matters during the active monitoring session.

### Long-Running Phase Detection

Track elapsed wall-clock time for each phase. When the **implement phase** has been running for 60+ minutes and consensus appears mostly complete (majority of agents confirmed), proactively offer the user an early exit:

```
### Long-Running Implement Phase

The implement phase has been running for ~<N> minutes.
Consensus status: <confirmed_count>/<total> agents confirmed.
```

Then use `AskUserQuestion`:
- **Question**: "The implement phase has been running for ~<N> minutes. Most agents have confirmed consensus. How would you like to proceed?"
- **Header**: "Long run"
- **Options**:
  - **"Keep monitoring"** — description: "Continue waiting for full completion"
  - **"Open PR with current work"** — description: "Extract completed work and create a draft PR"
  - **"Check what's blocking"** — description: "Investigate which agents haven't confirmed and why"

Handle each response:
- **Keep monitoring** → Resume polling. Reset the timer threshold (don't re-alert for another 30 minutes).
- **Open PR with current work** → Proceed to [Stuck Pipeline Rescue](#stuck-pipeline-rescue).
- **Check what's blocking** → Run `egg-orch consensus status <task_id>` and `egg-orch container list <task_id>`, then show blocking agents and their recent logs. Let the user decide next steps.

This threshold is configurable — adjust based on task complexity. The 60-minute default balances patience for legitimate long-running work against catching stuck pipelines.

### Stuck Pipeline Rescue

When monitoring detects a stuck pipeline (no progress for 10+ polls after consensus appears complete, or the user selects "Open PR with current work"), follow this workflow to extract completed work:

**Step 1: Check for committed work on the branch**

The branch name can be found in the `get_status` response's pipeline details (look for `branch` in the response), or derive it from the pipeline's task description using the `egg/<description>` naming convention.

```bash
git fetch origin
git log --oneline origin/egg/<branch> ^origin/main
```
If commits exist, the branch has usable work.

**Step 2: Check containers for uncommitted work**
```bash
egg-orch container list <task_id>
```
For each running container with agent work:
```bash
egg-orch container logs <task_id> <container_id> --lines 50
```
Look for signs of uncommitted changes (agents mention "modified files" or "working on" in logs).

**Step 3: Offer rescue options via `AskUserQuestion`**
- **Question**: "Pipeline appears stuck. How would you like to proceed with the completed work?"
- **Header**: "Rescue"
- **Options**:
  - **"Open PR with committed work"** — description: "Create a draft PR from commits already on the branch"
  - **"Cancel and retry"** — description: "Kill this pipeline and re-submit the task"
  - **"Keep waiting"** — description: "Continue monitoring — the pipeline may still recover"

Handle each response:
- **Open PR with committed work** →
  1. Verify branch has commits: `git log --oneline origin/egg/<branch> ^origin/main`
  2. Create a draft PR:
     ```bash
     gh pr create --head egg/<branch> --title "<task summary>" \
       --body "Draft PR with work completed before pipeline stall. Manual review recommended." \
       --base main --draft
     ```
  3. Inform the user of the PR link and that manual review is recommended since not all agents completed.
  4. Call `cancel_task` with `task_id` and `cleanup: true` to clean up the pipeline. If `cancel_task` fails, inform the user and offer to retry — the draft PR is already created so work is preserved.

- **Cancel and retry** → Confirm with the user, then call `cancel_task` with `task_id` and `cleanup: true`, followed by `submit_task` with the original parameters. Resume from Phase 3 with the new `task_id`. If `cancel_task` fails, inform the user and offer to retry. If `cancel_task` succeeds but `submit_task` fails, inform the user that the previous pipeline was cancelled and offer to retry the submission.

- **Keep waiting** → Resume monitoring. Reset the rescue counter.

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

   **Deduplication** — Some questions in the draft may also appear as separate `pending_decisions` (choice/feedback type) that you'll handle individually in the next sections. To avoid double-prompting, compare question text (case-insensitive, trimmed) against the `question` field of all `pending_decisions` in the current batch. If a draft question matches a pending decision, skip it here — it will be handled when you process that decision type below.

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

7. **Submit the response** — Use structured JSON payloads so the orchestrator's `_parse_resolution` can properly route the resolution. The `response` parameter to `provide_input` is always a **string** — serialize JSON payloads before passing them. Build the JSON based on the user's choice:

   **7a. Build the `context` string** from step 5 responses (if any). Include the "Resolved Questions" summary as a readable string. If no questions were asked in step 5, omit this field. Note: `_parse_resolution` only extracts `action` and `feedback` — the `context` is preserved in the raw resolution but not actively routed to agents yet.

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

After the user selects, call `provide_input` with:
```json
{"action": "select", "selected": "<chosen option text>"}
```

If the user types custom text via "Other", send:
```json
{"action": "select", "selected": "<user's custom text>"}
```

### For `feedback` type decisions:

Feedback decisions include a `questions` array — each entry has `id`, `question`, and an empty `answer` field. Present each question to the user:

- If there is a **single question**: show it with `AskUserQuestion` using options like "N/A" and "Not sure / skip", with the user typing their answer in "Other"
- If there are **multiple questions**: group them into `AskUserQuestion` calls (up to 4 questions per call). For each question, present it clearly and collect the response.

After collecting all answers, call `provide_input` with:
```json
{"action": "submit_feedback", "answers": {"<question_id>": "<answer>", "<question_id>": "<answer>"}}
```

Use the `id` field from each question entry as the key (e.g., `"Q1"`, `"Q2"` from `egg-contract add-feedback`, or fallback `"q-1"`, `"q-2"` from `sdlc_hitl.py` for questions missing an `id`). If a question has no `id`, use `"q-<1-based index>"` as the fallback key.

### Submitting choice/feedback responses:

Call the `provide_input` MCP tool (`phase_gate` decisions already handle this in step 7 above):

```
Tool: provide_input
Arguments:
  task_id: <task_id>
  decision_id: <decision_id>
  response: <JSON string — the stringified JSON payload from above>
```

**Important**: The `response` parameter is a string. Serialize the JSON payload to a string before passing it (e.g., `response: '{"action": "select", "selected": "Option A"}'`).

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

## Troubleshooting

When the pipeline is stuck, failing, or behaving unexpectedly, use these tools to investigate before asking the user to re-run:

| Scenario | Command | Notes |
|----------|---------|-------|
| Pipeline stuck or unclear state | `egg-orch pipeline status <task_id>` | Shows current phase, status, pending decisions |
| Check orchestrator + gateway health | `egg-orch health` | Verifies both services are reachable |
| View agent logs | `egg-orch container logs <task_id> <container_id>` | Add `--lines 100` for last N lines |
| List containers in pipeline | `egg-orch container list <task_id>` | Find container IDs for log viewing |
| Live pipeline visualization | `egg-pipeline-watch <task_id>` | Real-time DAG; use `--once` for snapshot |
| Review prior agent sessions | `egg-checkpoint list --pipeline <task_id>` | Browse transcripts, tool calls, token usage |
| Check what agents did | `egg-checkpoint context --pipeline <task_id>` | Cross-agent context summary by phase |
| Token usage / cost | `egg-checkpoint cost --pipeline <task_id>` | Breakdown by agent |
| SDLC contract state | `egg-contract show` | Task progress, pending decisions |
| BRC consensus state | `egg-orch consensus status <task_id>` | Agent phases, blocking agents, completion |
| Agent messages | `egg-orch message poll <task_id> --wait 0` | Check message bus (proposals, ACKs, NACKs) |
| Message bus stats | Via `get_status` MCP tool | `concurrent.consensus` field in response |

**When to use these during the workflow:**
- **Phase 3 (Monitor)**: If status appears stuck for multiple polls, run `egg-orch pipeline status` and `egg-orch container list` to check for failed containers. If the pipeline uses concurrent agents (`EGG_CONCURRENT_MODE`), check `egg-orch consensus status` to see which agents are blocking — a stuck agent may be waiting on a NACK resolution or hasn't proposed yet. Show the user a summary of what you find.
- **Phase 4 (HITL)**: If `provide_input` fails, check `egg-orch health` first. If the orchestrator is healthy, verify the `decision_id` is still valid with `egg-orch decision list`.
- **Phase 5 (Failure)**: Before offering re-run options, check `egg-orch container logs` for the failed agent to give the user context on what went wrong.

**Reading consensus state**: The `get_status` response includes a `concurrent.consensus` object when agents are running in BRC mode. Key fields:
- `is_complete`: Whether all agents have confirmed
- `blocking_agents`: Roles not yet confirmed (tells you who's holding things up)
- `agents.<role>.producer_phase`: `WORKING` → `PROPOSED` → `CONFIRMED`
- `agents.<role>.reviewer_phase`: `WORKING` → `REVIEWING` → `CONFIRMED`
- `has_unresolved_nacks`: Whether any reviewer has NACKed without the producer re-proposing
- `unresolved_nacks`: List with `reviewer`, `producer`, `reason`, and `version` — surface these to the user when consensus is stuck

## Critical Rules

- **Always use MCP tools** (`submit_task`, `get_status`, `provide_input`, `cancel_task`) — never call orchestrator APIs directly
- **Always serialize JSON payloads as strings** for `provide_input` — the `response` parameter is a string, not an object. Pass `'{"action": "approve"}'` not `{"action": "approve"}`
- **Never skip HITL** — always present decisions to the user and wait for their response
- **Stop polling on exit** — always exit the monitoring loop when the workflow ends
- **Handle errors gracefully** — if an MCP tool call fails, inform the user and offer to retry
- **Keep output concise** — don't flood the user with raw JSON; format status as a readable dashboard
