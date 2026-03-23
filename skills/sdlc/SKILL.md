---
name: sdlc
description: "Run an egg SDLC pipeline: full lifecycle (default) or lightweight coder+reviewer with --short."
disable-model-invocation: true
argument-hint: "[--short] [--qualifier <name>] [JIRA-1234 or issue# or description] [--repo owner/name]"
---

# SDLC Pipeline

You are guiding the user through an egg SDLC pipeline using MCP tools.

## Argument Parsing (before any phase)

Parse the arguments provided after `/sdlc`. Check for the `--short` flag first:

- If `--short` is present, remove it from the arguments and branch into the **[Short Flow](#short-flow)** below.
- Otherwise, continue with the **Full Flow** (default) — walk through 6 phases: Seed, Pre-Refine, Submit, Monitor, HITL, and Complete.

### JIRA Ticket Detection

Any argument matching the pattern `<LETTER><ALPHANUMERIC>-<DIGITS>` (e.g., `KORE-1234`, `ENG-42`, `PLAT-999`) is a **JIRA ticket identifier**. This applies to both the Full Flow and Short Flow. When detected:

1. The ticket ID is extracted and stored as `jira_ticket_id`
2. JIRA and Confluence context is fetched automatically (see [JIRA & Confluence Context Gathering](#jira--confluence-context-gathering) below)
3. The ticket summary becomes the task description, enriched with JIRA context

The regex pattern for detection: `^[A-Z][A-Z0-9]+-\d+$` (case-insensitive match, then uppercase for API calls).

---

# Full Flow

The full pipeline lifecycle with HITL gates, multi-phase execution, and comprehensive monitoring. Phases: Seed → Pre-Refine → Submit → Monitor → HITL → Complete.

## Phase 1 — Seed

Collect the **repository**, **task description**, and optionally a **GitHub issue number**. Your goal is **zero questions** on the happy path and **at most one question to get started** otherwise (the "Browse recent" flow may need a second to present the issue list).

### Step 1: Auto-detect the repository (NEVER ask if detectable)

Before asking the user anything, try to detect the repo automatically:

1. Run `git -C "$EGG_REPO_PATH" remote get-url origin 2>/dev/null` (or fall back to `git remote -v` from the working directory)
2. Parse the `owner/name` from the URL (e.g. `https://github.com/jwbron/egg.git` → `jwbron/egg`)
3. If a `--repo` flag was passed, use that instead

Only ask for the repo if detection fails AND no `--repo` flag was provided.

### Step 2: Parse arguments (skip questions when possible)

If the user provided arguments after `/sdlc`, parse them:

| Input | Interpretation |
|-------|---------------|
| `/sdlc 1059` | Issue number (bare integer) |
| `/sdlc #1059` | Issue number (with hash) |
| `/sdlc KORE-1234` | JIRA ticket (matches `<LETTER><ALPHANUMERIC>-<DIGITS>` pattern) |
| `/sdlc Add retry logic for API calls` | Free-text task description |
| `/sdlc --repo jwbron/egg 1059` | Repo override + issue number |
| `/sdlc --issue 1059` | Issue number (legacy flag, same as bare integer) |
| `/sdlc --repo jwbron/egg KORE-1234` | Repo override + JIRA ticket |
| `/sdlc KORE-1234 --qualifier backend` | JIRA ticket + qualifier (pipeline: `KORE-1234-backend`, branch: `egg/KORE-1234-backend`) |
| `/sdlc 1059 --qualifier frontend` | Issue number + qualifier (pipeline: `issue-1059-frontend`, branch: `egg/issue-1059-frontend`) |

When `--qualifier <name>` is provided, it is appended to the pipeline ID and branch name. This allows multiple pipelines for the same ticket or issue. Store the qualifier value as `pipeline_qualifier` for use in Phase 2 (Submit).

When an issue number is provided, fetch it immediately with `gh issue view <N> --repo <repo> --json title,body,comments,labels,assignees` and use the title+body as the task description. Proceed directly to Phase 1.5 (Pre-Refine) — no questions needed. Retain the full response (including comments, labels, and assignees) for use in Phase 1.5.

When a JIRA ticket ID is provided (matches `^[A-Z][A-Z0-9]+-\d+$` case-insensitive), run the [JIRA & Confluence Context Gathering](#jira--confluence-context-gathering) procedure. Use the ticket summary as the task description, enriched with the gathered context. Proceed directly to Phase 1.5 (Pre-Refine) — no questions needed.

When a free-text description is provided and the repo was auto-detected, proceed directly to Phase 1.5 (Pre-Refine).

### Step 3: Ask only what's missing

If the user ran `/sdlc` with no arguments, ask a **single** `AskUserQuestion`:

- **Question**: "What should the pipeline work on? Type an issue number, JIRA ticket (e.g. KORE-1234), or task description below, or browse recent issues."
- **Header**: "Task"
- **Options**:
  - **"Browse recent issues"** — description: "List recent open issues to pick from"
  - **"Help me scope the task"** — description: "Ask clarifying questions about requirements before submitting"

The user will select an option or type in the auto-added "Other" field.

Handle each response:

- **Other (matches `<LETTER><ALPHANUMERIC>-<DIGITS>`)** → Treat as a JIRA ticket ID. Run [JIRA & Confluence Context Gathering](#jira--confluence-context-gathering) and proceed to Phase 1.5 (Pre-Refine).
- **Other (integer)** → Treat as an issue number. Fetch with `gh issue view <N> --repo <repo> --json title,body,comments,labels,assignees` and proceed to Phase 1.5 (Pre-Refine).
- **Other (text)** → Treat as a free-text task description. Proceed to Phase 1.5 (Pre-Refine).
- **Browse recent issues** → Run `gh issue list --repo <repo> --state open --limit 10 --json number,title` and present the results as a second `AskUserQuestion` with each issue as an option. Once the user selects an issue, fetch it with `gh issue view <N> --repo <repo> --json title,body,comments,labels,assignees` and use the title+body as the task description. Then proceed to Phase 1.5 (Pre-Refine).
- **Help me scope the task** → Ask 1–2 follow-up questions about scope and acceptance criteria. Synthesize the user's answers into a refined task description (incorporating scope boundaries and acceptance criteria) before proceeding to Phase 1.5 (Pre-Refine).

**Never ask for the repo and the task in separate questions.** If the repo could not be auto-detected, include a repo question in the same `AskUserQuestion` call (multi-question mode).

## JIRA & Confluence Context Gathering

When a JIRA ticket ID is detected (e.g., `KORE-1234`), gather context from JIRA and Confluence before proceeding. This runs automatically — no user interaction needed.

### Step 1: Fetch the JIRA ticket

Check for local context-sync files first, then fall back to the API:

1. **Local files** — Check `~/context-sync/jira/` for a cached ticket file (e.g., `KORE-1234.json` or `KORE-1234.md`). If found, read it and use the content.

2. **JIRA API** — If no local file exists, fetch via the JIRA REST API:
   ```bash
   curl -s -u "$JIRA_USERNAME:$JIRA_API_TOKEN" \
     "$JIRA_BASE_URL/rest/api/3/issue/<TICKET_ID>?expand=renderedFields" \
     2>/dev/null
   ```
   Extract from the response:
   - `fields.summary` — ticket title
   - `fields.description` (or `renderedFields.description`) — full description
   - `fields.status.name` — current status
   - `fields.priority.name` — priority
   - `fields.labels` — labels
   - `fields.components` — components
   - `fields.assignee.displayName` — assignee
   - `fields.comment.comments` — comments (last 10)
   - `fields.issuelinks` — linked issues (blockers, relates-to, etc.)
   - `fields.subtasks` — subtasks if any
   - `fields.parent` — parent epic/story if this is a subtask

3. **Fallback** — If both local files and API fail (e.g., no credentials configured, private mode), inform the user:
   ```
   Could not fetch JIRA ticket <TICKET_ID>. JIRA credentials may not be configured.
   Proceeding with the ticket ID as the task description.
   ```
   Use the raw ticket ID as the task description and continue — do not block the pipeline.

### Step 2: Search for related Confluence documentation

Use the JIRA ticket's project key, summary, and labels to find relevant Confluence docs:

1. **Local files** — Search `~/context-sync/confluence/` for files matching the project key or ticket keywords:
   ```bash
   find ~/context-sync/confluence/ -name "*.md" -o -name "*.json" | head -20
   ```
   Then grep for the project key (e.g., `KORE`) and key terms from the ticket summary.

2. **Confluence API** — If local files are insufficient, search via the Confluence REST API:
   ```bash
   curl -s -u "$CONFLUENCE_USERNAME:$CONFLUENCE_API_TOKEN" \
     "$CONFLUENCE_BASE_URL/rest/api/content/search?cql=text~\"<TICKET_ID>\" OR text~\"<key terms from summary>\"&limit=5" \
     2>/dev/null
   ```
   For each matching page, fetch its body:
   ```bash
   curl -s -u "$CONFLUENCE_USERNAME:$CONFLUENCE_API_TOKEN" \
     "$CONFLUENCE_BASE_URL/rest/api/content/<page_id>?expand=body.storage" \
     2>/dev/null
   ```

3. **Fallback** — If Confluence is unavailable, skip silently. Confluence context is supplementary, not required.

### Step 3: Build enriched task description

Compose the task description from the gathered context:

```
## JIRA Ticket: <TICKET_ID>

**Summary**: <ticket summary>
**Status**: <status> | **Priority**: <priority>
**Labels**: <labels> | **Components**: <components>
**Assignee**: <assignee>

### Description

<ticket description — rendered as markdown>

### Key Comments

<last 3-5 substantive comments, with author and date>

### Linked Issues

<linked issues with relationship type, key, summary, and status>

## Confluence Context

<relevant Confluence page excerpts, if found — include page title and a concise summary of each>
```

This enriched description replaces the raw ticket ID as the task description for all downstream phases.

### Step 4: Determine the repository (if not already known)

If `--repo` was not provided and the repo was not auto-detected, try to infer it from the JIRA ticket:

1. Check the ticket's `components` or `labels` for a repo name
2. Check if the project key maps to a known repo (e.g., project metadata or custom fields)
3. If still unknown, ask the user via `AskUserQuestion`

## Phase 1.5 — Pre-Refine

> **Why "1.5"?** Phases 2–5 are referenced throughout this document, the orchestrator, and external docs. Renumbering them would cascade across many files for no functional benefit. "1.5" signals that this phase was inserted between Seed and Submit without breaking existing phase references.

A quick local triage pass to ensure the task description is clear and complete before submitting to the remote refiner. This is NOT a full code analysis (the remote refiner handles that) — it's a lightweight check focused on task clarity, scope, and acceptance criteria.

### Step 1: Review issue context (if available)

If an issue number was provided, use the data already fetched in Phase 1 (which includes `title,body,comments,labels,assignees`). Do **not** re-fetch the issue.

If a JIRA ticket was provided, the enriched description from [JIRA & Confluence Context Gathering](#jira--confluence-context-gathering) is already available. Use the JIRA ticket's linked issues, comments, and Confluence context to inform the code scan in Step 2. Do **not** re-fetch the ticket.

Note any linked PRs or referenced issues mentioned in the body or comments — these provide useful context for the refiner.

### Step 2: Quick code scan

Based on the task description, do a lightweight search (2–3 `Glob` + `Grep` queries) to identify the general area of the codebase affected. This is just enough to check feasibility and ask informed questions — NOT the full analysis the short flow's S2 phase does.

Examples:
- If the task mentions "health checks", search for health-related files
- If the task mentions a specific component, confirm it exists and note its location
- If the task mentions an API endpoint, find the route definition

### Step 3: Evaluate task clarity

**Skip this step** if the task came through Phase 1's "Help me scope the task" path — scope and clarity were already evaluated there.

Check the task description for:

- **Clear problem statement** — Is it clear what's wrong or what's needed?
- **Defined scope** — Is it clear what should change and what shouldn't?
- **Acceptance criteria** — How will we know it's done? Are there success conditions?
- **Ambiguous terms** — Are there vague phrases like "improve performance", "clean up", or "fix the issue" without specifics?

### Step 4: Ask clarifying questions (if needed)

**Skip this step** if the task came through Phase 1's "Help me scope the task" path, or if the task is already well-defined with clear goals and scope.

If the task is ambiguous or missing key information, present 1–3 targeted questions via a single `AskUserQuestion` call. Examples:

- "The issue mentions 'improve performance' — what specific metric or threshold?"
- "Should this change be backwards-compatible with the existing API?"
- "The issue references both X and Y — should both be addressed in this pipeline?"

### Step 5: Present summary and confirm (conditional)

**Auto-proceed**: If (a) Step 3 evaluated clarity as "Good" and Step 4 was skipped (no clarification was needed), OR (b) Steps 3 and 4 were both skipped because the task came through Phase 1's "Help me scope the task" path, skip the confirmation dialog and proceed directly to Step 6 → Phase 2. There is no value in prompting the user when nothing was surfaced or when scoping was already completed.

**Otherwise**, show a brief pre-refine summary:

```
### Pre-Refine Summary

**Task**: <1-sentence summary>
**Scope**: <general area — e.g., "orchestrator health checks", "gateway auth middleware">
**Clarity**: Good / Needs clarification
**Notes**: <any context added from clarification, or "None">
```

Then use `AskUserQuestion` to confirm:
- **Question**: "Ready to submit to the refiner?"
- **Header**: "Pre-Refine"
- **Options**:
  - **"Submit"** — description: "Proceed to submit the task to the remote refiner"
  - **"Add more context"** — description: "Provide additional context to append to the description"
  - **"Skip pre-refine"** — description: "Proceed directly with the original description unchanged"

Handle each response:
- **Submit** → Proceed to Step 6, then Phase 2 (Submit) with the enriched description.
- **Add more context** → Collect the user's additional context via a follow-up question, then proceed to Step 6.
- **Skip pre-refine** → Proceed to Phase 2 with the original description unchanged (skip Step 6).

### Step 6: Enrich description and transition to Phase 2

This is the single exit point from Phase 1.5 (except for "Skip pre-refine" which bypasses directly to Phase 2). If clarifications were collected in Steps 4 or 5, append them to the task description as an `## Additional Context` section before submission. This gives the remote refiner the benefit of the user's answers without requiring another HITL round.

```
<original task description>

## Additional Context

<clarifications and additional context collected during pre-refine>
```

If no clarifications were needed (task was already clear), pass the description through unchanged. For the "Help me scope" path specifically, scoping answers are already incorporated into the task description during Phase 1 synthesis — no additional appending is needed here. Then proceed to Phase 2.

## Phase 2 — Submit

Call the `submit_task` MCP tool with the gathered parameters:

```
Tool: submit_task
Arguments:
  description: <task description — enriched with JIRA/Confluence context if a JIRA ticket was provided>
  repo: <owner/name>
  issue_number: <number, if provided>
  jira_ticket: <TICKET_ID, if source is a JIRA ticket>
  qualifier: <qualifier, if --qualifier was provided>
```

When a JIRA ticket was the source, the `description` field should contain the full enriched description built in [JIRA & Confluence Context Gathering](#jira--confluence-context-gathering) Step 3 (including the JIRA ticket details, comments, linked issues, and any Confluence context). This ensures the pipeline agents have full context without needing JIRA access themselves.

The `jira_ticket` field drives pipeline naming: the pipeline ID and branch are derived from the ticket ID (e.g., `KORE-1234` → pipeline `KORE-1234`, branch `egg/KORE-1234`). When a `qualifier` is provided, it is appended (e.g., `KORE-1234-backend` / `egg/KORE-1234-backend`). The same qualifier logic applies to issue-driven pipelines (e.g., `issue-123-backend` / `egg/issue-123-backend`).

### Branch conflict handling

If `submit_task` returns a **409 error** indicating the branch already exists on the remote:

1. Inform the user: "Branch `egg/<name>` already exists. A qualifier is needed to create a separate pipeline."
2. Ask the user to provide a qualifier via `AskUserQuestion`:
   - **Question**: "Branch `egg/<name>` already exists. Provide a qualifier to differentiate this pipeline (e.g. 'backend', 'v2', 'fix'):"
   - **Header**: "Qualifier"
   - **Options**: 2-3 contextual suggestions based on the task description + "Other" (always available)
3. Retry `submit_task` with the qualifier appended.

Store the returned `task_id`. Confirm submission to the user:

> Task submitted successfully.
> **Task ID**: `<task_id>`
> **Source**: JIRA `<TICKET_ID>` (or GitHub Issue `#<N>`, or free-text)
> **Pipeline**: `<pipeline_id>` | **Branch**: `<branch>`
> **Description**: <description summary — first line of the enriched description>
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
2. If `status` is `failed` and `running_agents` is empty → call `get_pipeline_snapshot` MCP tool with the `task_id` to confirm actual state before exiting. If the snapshot shows active containers or recent messages, continue polling.
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
- **Check agent logs** → Call the `get_container_logs` MCP tool with `task_id` and `agent_role` set to the stalled agent's role (lines: 50). Show the user the output and let them decide next steps.
- **Wait longer** → Reset the stall counter for this agent. Resume monitoring.
- **Nudge agent** → Call the `send_message` MCP tool with `task_id`, `to_role` set to the stalled role, `message_type: "STATUS"`, and `body: "Overseer check: you appear stalled in <phase>. Please send a heartbeat or progress update."` Resume monitoring. If the agent remains stalled for another 3 polls after the nudge, re-alert the user with stronger options (see escalation below).

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
- **Check producer logs** → Call the `get_container_logs` MCP tool with `task_id` and `agent_role` set to the producer's role (lines: 50). Show the output and let the user decide next steps.
- **Check reviewer logs** → Call the `get_container_logs` MCP tool with `task_id` and `agent_role` set to the reviewer's role (lines: 50). Show the output and let the user decide next steps.
- **Nudge producer** → Call the `send_message` MCP tool with `task_id`, `to_role` set to the producer role, `message_type: "STATUS"`, and `body: "Overseer check: unresolved NACK from <reviewer> — please address and re-propose."` Resume monitoring.
- **Wait longer** → Reset the NACK stall counter. Resume monitoring.

**Post-nudge escalation** — If an agent remains stalled after a nudge (3+ more polls with no change), use `AskUserQuestion` to offer stronger actions:
- **Question**: "Agent '<role>' is still unresponsive after nudge (~<N> minutes total). How would you like to proceed?"
- **Header**: "Escalate"
- **Options**:
  - **"View full agent logs"** — description: "Show extended logs (`egg-orch container logs` with `--lines 200`) to diagnose the issue"
  - **"Restart pipeline"** — description: "Cancel this pipeline and re-submit the task to get a fresh agent"
  - **"Continue waiting"** — description: "Reset the counter and keep monitoring"

Handle each response:
- **View full agent logs** → Call the `get_container_logs` MCP tool with `task_id` and `agent_role` set to the stalled agent's role (lines: 200). Show the output and let the user decide next steps.
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
- **Check what's blocking** → Call `get_consensus_status` and `list_containers` MCP tools with the `task_id`, then show blocking agents and their recent logs (via `get_container_logs`). Let the user decide next steps.

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

Call the `list_containers` MCP tool with the `task_id`. For each running container with agent work, call `get_container_logs` with `task_id` and the container's `agent_role` (lines: 50). Look for signs of uncommitted changes (agents mention "modified files" or "working on" in logs).

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

When the pipeline is stuck, failing, or behaving unexpectedly, use MCP tools to investigate before asking the user to re-run:

| Scenario | MCP Tool | Notes |
|----------|----------|-------|
| Pipeline stuck or unclear state | `get_pipeline_snapshot` | Comprehensive view: pipeline state, containers, messages, decisions |
| Check orchestrator + gateway health | `check_health` | Verifies both services are reachable |
| View agent logs | `get_container_logs` | Auto-selects container by role; set `lines` for more output |
| List containers in pipeline | `list_containers` | Find container IDs, statuses, and agent roles |
| BRC consensus state | `get_consensus_status` | Agent phases, blocking agents, unresolved NACKs |
| Review prior agent sessions | `list_checkpoints` | Browse transcripts, tool calls, token usage |
| Search agent sessions | `search_checkpoints` | Search checkpoint metadata for keywords |
| SDLC contract state | `get_contract` | Task progress, pending decisions |
| Send message to agent | `send_message` | Nudge agents, request status updates |
| Phase details | `get_phase` | Current phase, execution timing, review cycles |
| Message bus stats | Via `get_status` MCP tool | `concurrent.consensus` field in response |

**When to use these during the workflow:**
- **Phase 3 (Monitor)**: If status appears stuck for multiple polls, call `get_pipeline_snapshot` to check for failed containers and consensus state. If the pipeline uses concurrent agents (`EGG_CONCURRENT_MODE`), call `get_consensus_status` to see which agents are blocking — a stuck agent may be waiting on a NACK resolution or hasn't proposed yet. Show the user a summary of what you find.
- **Phase 4 (HITL)**: If `provide_input` fails, call `check_health` first. If the orchestrator is healthy, verify the decision state with `get_status`.
- **Phase 5 (Failure)**: Before offering re-run options, call `get_container_logs` for the failed agent to give the user context on what went wrong.

**Reading consensus state**: The `get_status` response includes a `concurrent.consensus` object when agents are running in BRC mode. Key fields:
- `is_complete`: Whether all agents have confirmed
- `blocking_agents`: Roles not yet confirmed (tells you who's holding things up)
- `agents.<role>.producer_phase`: `WORKING` → `PROPOSED` → `CONFIRMED`
- `agents.<role>.reviewer_phase`: `WORKING` → `REVIEWING` → `CONFIRMED`
- `has_unresolved_nacks`: Whether any reviewer has NACKed without the producer re-proposing
- `unresolved_nacks`: List with `reviewer`, `producer`, `reason`, and `version` — surface these to the user when consensus is stuck

## Critical Rules

- **Always use MCP tools** — never call orchestrator/gateway APIs or CLIs directly
- **Always serialize JSON payloads as strings** for `provide_input` — the `response` parameter is a string, not an object. Pass `'{"action": "approve"}'` not `{"action": "approve"}`
- **Never skip HITL** — always present decisions to the user and wait for their response
- **Stop polling on exit** — always exit the monitoring loop when the workflow ends
- **Handle errors gracefully** — if an MCP tool call fails, inform the user and offer to retry
- **Keep output concise** — don't flood the user with raw JSON; format status as a readable dashboard

---

# Short Flow

When the `--short` flag is detected, run this lightweight flow instead of the full pipeline. This flow has the Claude Code session itself walk the user through refine → plan → contract generation → validation before submitting to a coder+reviewer pair. Walk through 6 phases: Seed, Refine, Plan & Contract, Submit, Monitor, and Complete.

## Phase S1 — Seed

Collect the **repository** and **task description**. Bare integers are treated as free-text descriptions, not issue lookups (use the Full Flow for GitHub issue-based workflows). However, **JIRA ticket IDs are supported** — any argument matching `<LETTER><ALPHANUMERIC>-<DIGITS>` triggers automatic JIRA context gathering.

### Step 1: Auto-detect the repository (NEVER ask if detectable)

Before asking the user anything, try to detect the repo automatically:

1. Run `git -C "$EGG_REPO_PATH" remote get-url origin 2>/dev/null` (or fall back to `git remote -v` from the working directory)
2. Parse the `owner/name` from the URL (e.g. `https://github.com/jwbron/egg.git` → `jwbron/egg`)
3. If a `--repo` flag was passed, use that instead

Only ask for the repo if detection fails AND no `--repo` flag was provided.

### Step 2: Parse arguments (skip questions when possible)

After stripping the `--short` flag, parse remaining arguments:

| Input | Interpretation |
|-------|---------------|
| `/sdlc --short Add retry logic to the API client` | Free-text task description |
| `/sdlc --short --repo jwbron/egg Fix flaky test` | Repo override + task description |
| `/sdlc --short KORE-1234` | JIRA ticket (matches `<LETTER><ALPHANUMERIC>-<DIGITS>` pattern) |
| `/sdlc --short --repo jwbron/egg ENG-42` | Repo override + JIRA ticket |
| `/sdlc --short KORE-1234 --qualifier backend` | JIRA ticket + qualifier |

When a JIRA ticket ID is detected, run the [JIRA & Confluence Context Gathering](#jira--confluence-context-gathering) procedure and use the enriched description as the task description. If `--qualifier` is provided, store it as `pipeline_qualifier`. Proceed directly to Phase S2.

When a free-text description is provided and the repo was auto-detected, proceed directly to Phase S2.

### Step 3: Ask only what's missing

If no task description was provided, ask a **single** `AskUserQuestion`:

- **Question**: "What task should the agent implement? Enter a JIRA ticket (e.g. KORE-1234) or describe the task."
- **Header**: "Task"
- **Options**:
  - **"Help me scope the task"** — description: "Ask clarifying questions about requirements before submitting"

The user will type their description in the auto-added "Other" field, or select the scoping option.

Handle each response:

- **Other (matches `<LETTER><ALPHANUMERIC>-<DIGITS>`)** → Treat as a JIRA ticket ID. Run [JIRA & Confluence Context Gathering](#jira--confluence-context-gathering) and proceed to Phase S2.
- **Other (text)** → Treat as a free-text task description. Proceed to Phase S2.
- **Help me scope the task** → Ask 1–2 follow-up questions about scope and acceptance criteria, then proceed to Phase S2.

**Never ask for the repo and the task in separate questions.** If the repo could not be auto-detected, include a repo question in the same `AskUserQuestion` call (multi-question mode).

## Phase S2 — Lightweight Refine

Analyze the task locally — no remote agents. Your goal is to understand scope and surface ambiguity before planning.

1. **Read relevant code files** — Based on the task description, identify and read the most relevant source files (up to 5–10 files). Use `Glob` and `Grep` to find them. Focus on files that will need modification and their immediate dependencies.

2. **Analyze scope** — Determine:
   - Which files need to change
   - What the changes involve (new code, modifications, deletions)
   - Any risks or edge cases (breaking changes, test coverage gaps, dependencies)

3. **Clarify ambiguity** — If the task is ambiguous or underspecified, ask the user 2–3 clarifying questions via `AskUserQuestion` (group into a single call). Skip this if the task is clear and well-scoped.

4. **Present analysis** — Show the user a brief analysis:

```
### Task Analysis

**Scope**: <1-2 sentence summary of what needs to change>

**Files affected**:
- `path/to/file1.py` — <what changes>
- `path/to/file2.ts` — <what changes>

**Risks**: <any concerns, or "None identified">
```

5. **Confirm** — Ask the user to confirm the analysis is correct before proceeding to planning. Use `AskUserQuestion`:
   - **Question**: "Does this analysis look correct? Any adjustments before I create the plan?"
   - **Header**: "Confirm"
   - **Options**:
     - **"Looks good, proceed"** — description: "Continue to plan generation"
     - **"Adjust scope"** — description: "I'll clarify what should change"

If the user adjusts scope, incorporate their feedback and re-present. Then proceed to Phase S3.

## Phase S3 — Lightweight Plan & Contract

Generate a concrete plan with tasks and acceptance criteria, validate it, and produce a plan document with a `yaml-tasks` appendix that the remote pipeline can parse into a formal contract.

### Step 1: Generate the plan

Create a single-phase plan with 1–5 concrete tasks. Each task should be:
- Specific and actionable (e.g., "Add `retry_with_backoff()` to `api_client.py`", not "Implement retry logic")
- Scoped to a single logical change
- Ordered by dependency (tasks that depend on others come later)

Also generate 1–3 acceptance criteria that describe how to verify the work is complete.

### Step 2: Build and validate a Contract

Construct a `Contract` object using the Pydantic model from `egg_contracts.models`. The contract should have:

```python
from egg_contracts.models import (
    Contract, Phase, Task, AcceptanceCriterion,
    PhaseStatus, TaskStatus, PipelinePhase,
)

contract = Contract(
    pipeline_id="short-<timestamp>",
    current_phase=PipelinePhase.IMPLEMENT,
    phases=[
        Phase(
            id="phase-1",
            name="Implement",
            status=PhaseStatus.PENDING,
            tasks=[
                Task(
                    id="task-1",
                    description="<task description>",
                    status=TaskStatus.PENDING,
                    acceptance_criteria="<acceptance criteria for this task>",
                ),
                # ... more tasks (use task-1-1, task-1-2 format for yaml-tasks compatibility)
            ],
        )
    ],
    acceptance_criteria=[
        AcceptanceCriterion(
            id="ac-1",
            description="<criterion>",
        ),
        # ... more criteria
    ],
)
```

Run the validation by constructing the object. If Pydantic validation fails, fix the errors and retry.

### Step 2b: Generate the plan document with yaml-tasks appendix

Generate a markdown plan document that includes a `yaml-tasks` structured appendix. This is the same format used by the plan agent in the normal flow — the remote pipeline parses it to populate the contract.

````markdown
# Plan: <short title>

## Summary

<2-3 sentences describing the approach>

## Implementation

### Phase 1: Implement

<brief description of what this phase covers>

**Tasks**:
1. [TASK-1-1] <task description> — Acceptance: <criteria>
2. [TASK-1-2] <task description> — Acceptance: <criteria>

```yaml
# yaml-tasks
pr:
  title: "<imperative summary, ≤70 chars>"
  description: |
    <2-3 sentence PR description>
phases:
  - id: 1
    name: Implement
    goal: "<what this phase achieves>"
    tasks:
      - id: TASK-1-1
        description: "<task description>"
        acceptance: "<acceptance criteria>"
        files:
          - <path/to/file>
      - id: TASK-1-2
        description: "<task description>"
        acceptance: "<acceptance criteria>"
        files:
          - <path/to/file>
```
````

The `yaml-tasks` appendix **must** be present — it is machine-parsed by the remote pipeline to create the formal contract. The prose section above it provides human-readable context.

### Step 3: Present to user

Display the plan and contract summary:

```
### Plan

**Phase**: Implement (single phase)

**Tasks**:
1. `task-1`: <description>
2. `task-2`: <description>
3. `task-3`: <description>

**Acceptance Criteria**:
- `ac-1`: <description>
- `ac-2`: <description>

---

**Contract Summary**
Tasks: <N>
Acceptance Criteria: <N>
Phase: implement (single phase)
Validation: Passed
```

### Step 4: Ask for approval

Use `AskUserQuestion`:
- **Question**: "Approve this plan to submit to the coder+reviewer pipeline?"
- **Header**: "Approve"
- **Options**:
  - **"Approve"** — description: "Submit to the pipeline"
  - **"Request changes"** — description: "I want to adjust the plan"
  - **"Cancel"** — description: "Abort — don't submit"

Handle each response:
- **Approve** → Proceed to Phase S4
- **Request changes** → Ask what to change, update the plan/contract, re-validate, and re-present
- **Cancel** → Stop the workflow entirely

## Phase S4 — Submit

Call the `submit_task` MCP tool with the gathered parameters, the lightweight config, and the pre-generated artifacts:

```
Tool: submit_task
Arguments:
  description: <original task description>
  repo: <owner/name>
  jira_ticket: <TICKET_ID, if source is a JIRA ticket>
  qualifier: <qualifier, if --qualifier was provided>
  config: {"start_phase": "implement", "hitl_gates": false, "overseer_enabled": true}
  analysis: <the analysis from Phase S2, if any>
  plan: |
    <the full plan document from Phase S3, including the yaml-tasks appendix>
```

The `plan` field is parsed by the remote pipeline to populate the formal contract with tasks and acceptance criteria. The `analysis` field provides additional context for the agents. The `description` contains only the original task description. The `jira_ticket` and `qualifier` fields drive pipeline and branch naming (see Phase 2 for details).

If `submit_task` returns a **409 error** indicating the branch already exists, follow the same [branch conflict handling](#branch-conflict-handling) procedure as the full flow: inform the user, ask for a qualifier, and retry.

Store the returned `task_id`. Confirm submission to the user:

> Task submitted — coder + reviewer, no gates.
> **Task ID**: `<task_id>`
> **Description**: <description summary>
> **Repository**: <repo>

## Phase S5 — Monitor

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
   - If `status` is `complete` → exit the loop, move to Phase S6
   - If `status` is `failed` → apply the **failed status grace period** (see below) before exiting
   - If `pending_decisions` is non-empty → handle inline (see below)

Keep the dashboard output concise. Only show changes from the previous poll when possible.

**Important: Run polling sleeps in the foreground (blocking).** Do not use background sleeps or `run_in_background` for the 60-second poll interval.

### Failed Status Grace Period

During phase cycle transitions (e.g., review cycles), the orchestrator may briefly report `status: failed` while spawning new containers. Treating this as terminal prematurely ends monitoring.

**Before treating `failed` as terminal, apply these checks:**

1. If `status` is `failed` but `running_agents` is non-empty → treat as "transitioning", not failed. Log: `"Status shows failed but agents still running — treating as cycle transition."` Continue polling.
2. If `status` is `failed` and `running_agents` is empty → call `get_pipeline_snapshot` MCP tool with the `task_id` to confirm actual state before exiting. If the snapshot shows active containers or recent messages, continue polling.
3. Only exit to Phase S6 when `status` is `failed`, `running_agents` is empty, **and** the secondary check confirms the pipeline is genuinely stopped.

### Stall detection

Track the `current_phase` and latest `recent_messages` entry across polls. If **10 consecutive polls** (~10 minutes) pass with no phase change and no new messages, surface a warning:

```
### Potential Stall Detected

Pipeline has shown no progress for ~10 minutes.
```

Then offer three options via `AskUserQuestion`:

- **"Check logs"** — description: "View agent logs to diagnose the issue" — call the `get_container_logs` MCP tool with `task_id` and the agent's role (lines: 50). Show the user the output.
- **"Wait longer"** — description: "Give the agent more time (resets the stall counter)"
- **"Cancel"** — description: "Cancel this pipeline"

If "Wait longer" is selected, reset the stall counter and resume monitoring. If "Cancel", call `cancel_task` and move to Phase S6 failure handling.

### NACK handling

If the status shows unresolved NACKs in the consensus data, surface them to the user:

> **Reviewer raised concerns** — the coder is iterating on feedback. This is normal BRC behavior.

Only escalate if NACKs persist across 5+ consecutive polls with no progress, at which point offer the same stall detection options.

### Handling unexpected decisions

With `hitl_gates: false`, decisions should not appear. But if they do, handle them gracefully:

- For `choice` type: present the options via `AskUserQuestion`, then call `provide_input` with `{"action": "select", "selected": "<chosen option>"}` serialized as a JSON string.
- For `feedback` type: present the questions, collect answers, then call `provide_input` with `{"action": "submit_feedback", "answers": {"<id>": "<answer>"}}` serialized as a JSON string.
- For `phase_gate` type: auto-approve by calling `provide_input` with `{"action": "approve"}` serialized as a JSON string. Log the phase and decision ID, and inform the user.

After resolving any decisions, resume monitoring.

## Phase S6 — Complete

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
  3. Call `submit_task` with the description (original or updated) and same repo, plus the same `config`, `analysis`, and `plan`
  4. Resume from Phase S5 (Monitor) with the new `task_id`

  **Error handling**: If `cancel_task` or `submit_task` fails, inform the user and offer to retry the failed step.

## Short Flow Critical Rules

- **Always use MCP tools** (`submit_task`, `get_status`, `provide_input`, `cancel_task`) — never call orchestrator APIs directly
- **Always serialize JSON payloads as strings** for `provide_input`
- **Always pass `config`** with `{"start_phase": "implement", "hitl_gates": false, "overseer_enabled": true}` when calling `submit_task`
- **Auto-approve phase gates** — this is a no-HITL flow; if a gate appears, approve it automatically and inform the user
- **Validate the contract** — always construct and validate a `Contract` Pydantic model in Phase S3. The remote pipeline uses the `plan` field's `yaml-tasks` appendix to populate its own formal contract
- **Pass plan via the `plan` field** — the plan document (with `yaml-tasks` appendix) must be passed as the `plan` argument to `submit_task`, not embedded in `description`. The `analysis` field carries the Phase S2 analysis. This ensures the remote pipeline creates a proper structured contract
- **Stop polling on exit** — always exit the monitoring loop when the workflow ends
- **Handle errors gracefully** — if an MCP tool call fails, inform the user and offer to retry
- **Keep output concise** — don't flood the user with raw JSON; format status as a readable dashboard
