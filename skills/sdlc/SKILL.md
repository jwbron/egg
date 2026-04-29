---
name: sdlc
description: "Run an egg SDLC pipeline: full lifecycle (default) or lightweight coder+reviewer with --short."
disable-model-invocation: true
argument-hint: "[--short] [--qualifier <name>] [JIRA-1234 or issue# or description] [--repo owner/name]"
allowed-tools: Monitor Bash(skills/sdlc/bin/wait-status:*) Bash(gh issue view:*) Bash(gh issue list:*) Bash(gh pr list:*) Bash(gh pr view:*) Bash(git remote:*) Bash(git -C *:remote:*) Bash(git log:*) Bash(git show:*) Bash(git ls-tree:*) Read Grep AskUserQuestion mcp__egg__submit_task mcp__egg__get_status mcp__egg__provide_input mcp__egg__list_tasks mcp__egg__cancel_task mcp__egg__check_health mcp__egg__list_containers mcp__egg__get_container_logs mcp__egg__send_message mcp__egg__get_consensus_status mcp__egg__get_phase mcp__egg__get_pipeline_snapshot mcp__egg__get_contract mcp__egg__list_checkpoints mcp__egg__search_checkpoints
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
| `/sdlc --repo owner/repo 1059` | Repo override + issue number |
| `/sdlc --issue 1059` | Issue number (legacy flag, same as bare integer) |
| `/sdlc --repo owner/repo KORE-1234` | Repo override + JIRA ticket |
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

Fetch the ticket via the JIRA REST API:

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

**Fallback** — If the API fails (e.g., no credentials configured, private mode), inform the user:
```
Could not fetch JIRA ticket <TICKET_ID>. JIRA credentials may not be configured.
Proceeding with the ticket ID as the task description.
```
Use the raw ticket ID as the task description and continue — do not block the pipeline.

### Step 2: Search for related Confluence documentation

Use the JIRA ticket's project key, summary, and labels to find relevant Confluence docs:

Search via the Confluence REST API:

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

**Fallback** — If Confluence is unavailable, skip silently. Confluence context is supplementary, not required.

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

Drive the pipeline through one Monitor invocation per quiet stretch. On entry:

1. **First poll** — call the `get_status(task_id)` MCP tool to render the initial dashboard. `get_status` returns the full snapshot. It does **not** return a `cursor`; the cursor is produced by `wait-status` only. Cache the snapshot in conversation context as `last_status`. Initialize `last_cursor = ""` (empty — the first `wait-status` call snaps to the tip of both event sources).

2. **Blocking wait** — invoke `skills/sdlc/bin/wait-status` through the **Monitor** tool, not Bash. The Monitor tool delivers each stdout line as a separate notification, so the LLM wakes on every emitted event in real time — exactly what the JSON-line streaming model assumes. Run from the repo root:

   ```
   Monitor(
     description: "wait-status <task_id>",
     command: "skills/sdlc/bin/wait-status <task_id> --since \"<last_cursor>\"",
     timeout_ms: 3600000,
   )
   ```

   The launcher wraps `sandbox/bin/egg-orch pipeline wait-status`, setting `PYTHONPATH` and `EGG_ORCHESTRATOR_URL` so no host-side configuration is required beyond `make deps`. It loops the orchestrator's `/status/wait` route server-side, threading the cursor between calls. Stdout is **JSON-lines** — one line per pipeline-relevant event, surfaced to the LLM as one notification per line. The CLI is silent on `no_change`, so the LLM only wakes when something happened. Exit codes (Monitor reports them as the watch's exit code):

   | Exit code | Meaning | Skill action |
   |-----------|---------|--------------|
   | `0` | Pipeline reached terminal state (`complete` / `failed` / `cancelled`) | Exit the monitor loop, move to Phase 5 |
   | `2` | Transient error budget exceeded after backoff | Re-invoke Monitor with the same `last_cursor` |
   | `3` | Permanent error (4xx, malformed cursor, unknown pipeline) | Surface stderr to user; do NOT silently retry |
   | (timeout) | Monitor `timeout_ms` reached | Re-invoke Monitor with the updated `last_cursor` |

   **Why Monitor and not Bash?** `wait-status` is designed to emit one JSON-line per event over the lifetime of a single CLI invocation. Foreground Bash blocks the LLM until the command exits and batches all events emitted in that window into one wake — so a `decision.created` that lands 30 seconds in won't be visible until the next event flushes the buffer. Background Bash sends a single completion notification when the whole CLI exits and forces file-polling for stdout. Monitor's per-line notification semantics match the streaming-stdout contract directly.

   **Bash fallback:** If Monitor is unavailable in the harness, fall back to a foreground Bash invocation (`skills/sdlc/bin/wait-status <task_id> --since "<last_cursor>"`) — but be aware that events emitted within a single 10-minute Bash window will be batched at exit, not surfaced as they arrive. On Bash-cap timeout, re-invoke with the latest `last_cursor` from the batched output.

3. **Read each emitted JSON line** as it arrives. The line shape is:

   ```json
   {
     "trigger": "event",
     "event_type": "phase.started",        // wire value — phase.started / decision.created / pipeline.completed / etc.
     "cursor": "msg:1738012734-0|evt:142",
     "current_phase": "plan",
     "status": "running",
     "phase_elapsed_seconds": 127,
     "concurrent": { "consensus": { ... } }
   }
   ```

   For `trigger: "message"` the line carries `messages: [...]` instead of `event_type`. Update `last_cursor` from each line's `cursor` field. The cursor is **opaque** (shape `msg:<id>|evt:<seq>`) — treat it as a string and thread it through `--since` on the next Monitor invocation.

   **Trigger allowlist:** `OVERSEER_ALERT`, `CONSENSUS_CONFIRMED`, `CONSENSUS_NACK`, `CONSENSUS_RE_REVIEW`, `phase.started`, `phase.completed`, `pipeline.completed`, `pipeline.failed`, `pipeline.cancelled`, `decision.created`. `decision.resolved` is **deliberately excluded** so the host doesn't self-wake on a `provide_input` it just submitted.

4. **Render the dashboard** on each line:

   ```
   --- Pipeline Status ---
   Phase: <current_phase> | Status: <status> | Elapsed: <phase_elapsed_seconds>s
   Recent: <event_type or first messages[] entry>
   ```

   Use the server-computed `phase_elapsed_seconds` from the line. The line carries only the dashboard-relevant subset (`current_phase`, `status`, `phase_elapsed_seconds`, `concurrent.consensus`) — it does **not** include the full snapshot (running_agents, completed_agents, recent_messages, pipeline metadata, `pending_decisions`). When you need the full envelope — for example to enrich an `OVERSEER_ALERT` with `recent_messages`, or to render `pending_decisions` ahead of HITL on a `decision.created` line — call `get_status(task_id)` again as a one-shot snapshot and refresh `last_status`.

5. **Check for overseer alerts** on each `trigger: "message"` line where any entry's `type` is `OVERSEER_ALERT` — see [Overseer Alert Detection](#overseer-alert-detection) below.

6. **Check consensus health** on each line carrying `concurrent.consensus` — see [Consensus Monitoring](#consensus-monitoring) below. The wait-status JSON-line ships `concurrent.consensus` whenever the route saw it, so consensus drift never goes invisible during quiet phases on BRC pipelines.

7. **State transitions:**
   - On `event_type: "decision.created"` → re-fetch the full snapshot via `get_status(task_id)` (the JSON-line does not carry `pending_decisions`) and move to Phase 4 (HITL).
   - On `status: "complete"` or `event_type: "pipeline.completed"` → exit the monitor loop and move to Phase 5.
   - On `status: "failed"` or `event_type: "pipeline.failed"` → apply the **failed status grace period** (see below) before exiting.

8. **Track elapsed time** using each line's `phase_elapsed_seconds` (server-computed). Fall back to local wall-clock only when this field is absent (phase boundaries, pending phases). Used for [Long-Running Phase Detection](#long-running-phase-detection).

**Important: `wait-status` blocks server-side and emits events as they arrive. Do NOT wrap the Monitor invocation in an outer `for`-loop or `sleep` — the CLI is already the loop, server-side, and Monitor surfaces each emitted line as its own notification. The skill's liveness guarantee comes from the CLI re-issuing the route call with the threaded cursor on every Path-B no-change return; intra-process loop, no LLM turn.** When Monitor's `timeout_ms` (or the Bash 10-min cap, if you're on the fallback path) forces the CLI to terminate, simply re-invoke with the latest `last_cursor` from your conversation context. The overseer is the primary deadlock detector and emits `OVERSEER_ALERT` on stalls, which is in the trigger allowlist. See [Host-Side Waits](../../docs/reference/agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status) for the full event allowlist, exit-code contract, and concurrency model.

Keep the dashboard output concise. Only show changes from the previous emit when possible.

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

### Overseer Alert Detection

When the pipeline has an overseer agent enabled, it broadcasts `OVERSEER_ALERT` messages to the message bus whenever it detects an anomaly. These appear in `recent_messages` with `type: "OVERSEER_ALERT"` and `from_role: "overseer"`.

On each poll cycle, scan `recent_messages` for entries with `type: "OVERSEER_ALERT"`. When found:

1. Display the alert prominently:

```
### Overseer Alert

**<subject>**
<body — full text>
```

2. Use `AskUserQuestion` to let the user decide next steps:
   - **Question**: "The overseer detected an anomaly: '<subject>'. How would you like to proceed?"
   - **Header**: "Alert"
   - **Options**:
     - **"Check agent logs"** — description: "View recent logs for the affected agent"
     - **"Acknowledge"** — description: "Note the alert and continue monitoring"
     - **"Cancel pipeline"** — description: "Stop the pipeline if the issue is critical"

Handle each response:
- **Check agent logs** → Extract the agent role from the alert subject (format: `<anomaly_type>: <agent_role> [<priority>]`). Call the `get_container_logs` MCP tool with `task_id` and `agent_role`. Show the output and let the user decide next steps.
- **Acknowledge** → Resume monitoring. Track acknowledged alerts to avoid re-prompting for the same alert.
- **Cancel pipeline** → Confirm with the user, then call `cancel_task` with `task_id` and `cleanup: true`.

**Deduplication** — Maintain a set of seen alert message `id` values (UUIDs from the `Message` model) across poll cycles. Only prompt the user for alerts not previously seen or acknowledged. Do not use subject strings for deduplication — distinct alerts may share the same anomaly type, role, and priority.

### Host detector migration (issue #1962)

The five host-side detection blocks below — **Stall detection**, **Silent agent detection**, **NACK escalation**, **Long-Running Phase Detection**, **Stuck Pipeline Rescue** — are currently the active source of these alerts. They are being migrated into the overseer agent (`sandbox/overseer_monitor.py::run_migrated_detectors`) under the `overseer_owns_host_detection` `PipelineConfig` flag.

**Gating semantics** (read at the start of every poll cycle from the `PipelineConfig` block on the cached `last_status`, refreshed via `get_status` whenever a one-shot snapshot is needed):

```
if not config.overseer_owns_host_detection:
    # default — host runs these detectors as today
    run host-side stall / silent-agent / NACK / long-run / rescue checks
else:
    # calibration-window opt-in — overseer is the sole source
    skip all five blocks; rely on incoming OVERSEER_ALERT messages
    (which the host still surfaces via the existing alert flow above)
```

The default is `False` so existing pipelines see no behavior change. After the calibration window concludes, a follow-up PR flips the default to `True` and deletes the dormant host blocks. **Without this gate the detection would fire from both sides simultaneously and double-alert the user.**

The overseer's per-agent timing state moves from this skill's in-memory `{role: {phase, phase_entered_at, …}}` map (described in [State tracking](#state-tracking) below) into `.egg-state/oversight/agent-timing.json` (schema `egg_overseer.state.AgentTimingState`; flock-guarded). When `overseer_owns_host_detection=True` the host stops maintaining its in-memory map and reads `OVERSEER_ALERT.metadata` for the migrated anomaly types instead.

#### Overseer-Absent Fallback

When `overseer_owns_host_detection=True` and the host sees **no `OVERSEER_ALERT` messages** for `2 × overseer_agent_stall_seconds` (default 360s) **while running agents are present**, the overseer may itself be unresponsive. Surface a single `AskUserQuestion` (at most once per phase, gated by the sentinel file `.egg-state/oversight/sdlc-fallback-fired-{pipeline_id}-{phase}.flag`):

- **Question**: "Overseer appears unresponsive — no OVERSEER_ALERT in the last <N> minutes despite running agents. How would you like to proceed?"
- **Header**: "Overseer"
- **Options**:
  - **"Check the overseer container logs"** — description: "Inspect why the overseer is not emitting alerts"
  - **"Restart the overseer"** — description: "Stop and respawn the overseer container"
  - **"Continue with host detection only for this pipeline"** — description: "Treat `overseer_owns_host_detection` as `False` for the remainder of this pipeline"
  - **"Cancel"** — description: "Stop the pipeline"

Handle each response:
- **Check the overseer container logs** → Call `get_container_logs` MCP tool with `agent_role="overseer"` and `lines: 200`.
- **Restart the overseer** → (no host-side restart verb today; surface the recommendation as an issue or operator action).
- **Continue with host detection only for this pipeline** → Treat the flag as effectively `False` for the rest of this monitoring session; resume host-side detection.
- **Cancel** → Confirm with the user, then call `cancel_task` with `task_id` and `cleanup: true`.

After firing, write the sentinel file so the fallback does not fire again this phase. The orchestrator's per-phase `.egg-state/oversight/` cleanup removes the sentinel at phase boundary.

### Consensus Monitoring

When the pipeline uses concurrent agents (BRC protocol), each `wait-status` JSON-line and the cached `last_status` may include a `concurrent.consensus` object. The CLI ships `concurrent.consensus` on every emitted line whenever the route saw it, so consensus drift never goes invisible during quiet phases on BRC pipelines. On each emitted line, check this data for red flags and surface problems to the user before they escalate.

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

The `concurrent.consensus` object may not be present in all status responses (e.g., for non-BRC pipelines). When it is absent, **fall back to message-based consensus tracking** by classifying entries in `recent_messages`. (The `wait-status` JSON-line does not ship `recent_messages`; combine the cached `last_status.recent_messages` with any `messages` array ferried by a `trigger: "message"` JSON-line.):

1. **Classify messages using the `type` field** (primary) — each `recent_messages` entry includes a `type` field with reliable enum values: `CONSENSUS_PROPOSE`, `CONSENSUS_ACK`, `CONSENSUS_NACK`, `CONSENSUS_CONFIRMED`. Use these for classification, not subject parsing.
2. **Identify roles using the `from_role` field** — each message includes `from_role` indicating which agent sent it.
3. Maintain an in-memory map of `{role: {last_message_type, last_message_time, message_count}}` built from `recent_messages`
4. Infer consensus state: if all roles listed in `running_agents` have sent `CONSENSUS_CONFIRMED` messages, consensus is likely complete
5. For the enhanced dashboard, approximate the fields:
   - Confirmed count: roles with `CONSENSUS_CONFIRMED` messages
   - Blocking: roles with no `CONSENSUS_CONFIRMED` message
   - Unresolved NACKs: `CONSENSUS_NACK` messages not followed by a `CONSENSUS_PROPOSE` from the producer
6. Use `subject` only for supplementary detail (e.g., extracting NACK reasons or human-readable context for the dashboard)

**Stall detection** — *Skip this block when `config.overseer_owns_host_detection` is `True` (issue #1962): the overseer's `agent-stall` / `agent-nack-unresolved` migrated detectors fire and the host receives them as `OVERSEER_ALERT` messages.* Track agent phase progression using wall-clock time (not poll counts, since poll interval varies). Flag an agent as potentially stalled when:
- It has been in `producer_phase: WORKING` for 3+ minutes while other agents have progressed
- It has been in `producer_phase: PROPOSED` for 3+ minutes with no reviewer activity (reviewers still in `WORKING`)
- A NACK has been unresolved for 3+ minutes (producer hasn't re-proposed)

Note: 3 minutes is a baseline threshold. Code generation, test execution, and large diffs can legitimately exceed this. Adjust the threshold based on pipeline complexity — for pipelines with heavy test suites or large codebases, consider using 5+ minutes before flagging. The "Wait longer" option mitigates false positives.

**Silent agent detection** — *Skip this block when `config.overseer_owns_host_detection` is `True` (issue #1962): the overseer's `agent-silent` migrated detector handles this.* Separately from phase-based stall detection, track agents that never enter the consensus protocol at all. Flag an agent as "silent" when:
- It has been in `running_agents` for 10+ minutes of elapsed time (use the agent's server-computed `elapsed_seconds` field when available; fall back to `now - first_seen_at`)
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
- **Wait longer** → Reset `phase_entered_at` to the current time for this agent. Resume monitoring.
- **Nudge agent** → Call the `send_message` MCP tool with `task_id`, `to_role` set to the stalled role, `message_type: "STATUS"`, and `body: "Overseer check: you appear stalled in <phase>. Please send a heartbeat or progress update."` Record the nudge timestamp (`nudged_at`). Resume monitoring. If the agent remains stalled for another 3+ minutes after the nudge, re-alert the user with stronger options (see escalation below).

**NACK escalation** — *Skip this block when `config.overseer_owns_host_detection` is `True` (issue #1962): the overseer's `agent-nack-unresolved` migrated detector handles this. When the host receives an `OVERSEER_ALERT` with `subject` starting `agent-nack-unresolved`, render the existing `### Unresolved NACK` `AskUserQuestion` flow below using the alert's `metadata` rather than re-deriving it.* When an unresolved NACK persists for 3+ minutes, surface it prominently:

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
- **Wait longer** → Reset `phase_entered_at` to the current time for the NACK tracking. Resume monitoring.

**Post-nudge escalation** — If an agent remains stalled after a nudge (3+ minutes since the nudge with no change, computed from `now - nudged_at`), use `AskUserQuestion` to offer stronger actions:
- **Question**: "Agent '<role>' is still unresponsive after nudge (~<N> minutes total). How would you like to proceed?"
- **Header**: "Escalate"
- **Options**:
  - **"View full agent logs"** — description: "Show extended logs (`egg-orch container logs` with `--lines 200`) to diagnose the issue"
  - **"Restart pipeline"** — description: "Cancel this pipeline and re-submit the task to get a fresh agent"
  - **"Continue waiting"** — description: "Reset the stall timer and keep monitoring"

Handle each response:
- **View full agent logs** → Call the `get_container_logs` MCP tool with `task_id` and `agent_role` set to the stalled agent's role (lines: 200). Show the output and let the user decide next steps.
- **Restart pipeline** → Confirm with the user, then call `cancel_task` with `task_id` and `cleanup: true`, followed by `submit_task` with the original parameters. Resume from Phase 3 with the new `task_id`.
- **Continue waiting** → Reset `phase_entered_at` to the current time. Resume monitoring.

**State tracking** — When `config.overseer_owns_host_detection` is `False` (the default), maintain a simple in-memory map of `{role: {phase, phase_entered_at, nudged_at, first_seen_at, has_any_messages}}` across poll cycles, plus a top-level `running_agent_count` to track the number of running agents between polls (for detecting post-consensus reviewer spawns). All timestamps are wall-clock times. Set `first_seen_at` when a role first appears in `running_agents`. Set `phase_entered_at` to the current time when the role is first tracked or when its phase changes. Reset `phase_entered_at` whenever a role's phase changes or new messages appear from it in `recent_messages`. Set `nudged_at` when a nudge is sent (null otherwise). Set `has_any_messages` to true when any message from the role appears in `recent_messages`. This is lightweight — no persistence needed since it only matters during the active monitoring session.

When `config.overseer_owns_host_detection` is `True` (issue #1962), the overseer owns this state in `.egg-state/oversight/agent-timing.json` (`egg_overseer.state.AgentTimingState` schema; flock-guarded read/modify/write). The host stops maintaining its in-memory map, and reads the migrated anomaly types from incoming `OVERSEER_ALERT` messages instead.

**Server-computed timing** — When available, prefer server-computed timing fields over client-side tracking: use `phase_elapsed_seconds` for phase-level elapsed time and each agent's `elapsed_seconds` for per-agent elapsed time. These fields are computed server-side and are unaffected by client-side blocking (e.g., `AskUserQuestion` dialogs that pause the poll loop) or client-server clock skew. Fall back to `phase_entered_at`-based tracking only when these fields are absent.

### Long-Running Phase Detection

*Skip this block when `config.overseer_owns_host_detection` is `True` (issue #1962): the overseer's `phase-long-running` migrated detector handles the trigger. The host still renders the `### Long-Running Implement Phase` `AskUserQuestion` flow below when it receives the matching `OVERSEER_ALERT`.*

Track elapsed time for each phase using the server-computed `phase_elapsed_seconds` field from the latest source — emitted on each `wait-status` JSON-line and on the `get_status` snapshot. Fall back to wall-clock tracking only when this field is unavailable. When the **implement phase** has been running for 60+ minutes and consensus appears mostly complete (majority of agents confirmed), proactively offer the user an early exit:

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

*Skip the host-side detection trigger of this section when `config.overseer_owns_host_detection` is `True` (issue #1962): the overseer's migrated detectors will surface the stall via `OVERSEER_ALERT`. The rescue workflow itself (Steps 1–3 below) remains user-initiated and is invoked either from the overseer-driven alert flow above or when the user picks "Open PR with current work" from the Long-Running Phase prompt — that path stays in the host even with the migration in effect.*

When monitoring detects a stuck pipeline (no progress for 10+ minutes after consensus appears complete, or the user selects "Open PR with current work"), follow this workflow to extract completed work:

**Step 1: Check for committed work on the branch**

The branch name can be found in the `pipeline` block of the cached `last_status` (returned by `get_status` — look for `branch`), or derive it from the pipeline's task description using the `egg/<description>` naming convention.

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

- **Keep waiting** → Resume monitoring. Reset `phase_entered_at` to the current time.

## Phase 4 — HITL (Human-in-the-Loop)

When the cached `last_status` (sourced from `get_status`, re-fetched after a `wait-status` line emits `event_type: "decision.created"`) carries a non-empty `pending_decisions` list, partition the batch by `decision_type` and handle each group as described below. `wait-status` wakes immediately on `decision.created`, so a freshly-created decision is visible on the very next emitted line — re-fetch the full snapshot via `get_status` to get the enriched `pending_decisions` envelope. A single snapshot can surface multiple pending decisions at once (e.g. a refiner that registered 10 `choice` decisions via `register_open_question`); when that happens, group them so the user sees up to 4 per `AskUserQuestion` call rather than one prompt per decision.

### Two-wave surfacing

A phase that registers agent-level `choice` / `feedback` decisions (via `register_open_question` / `register_feedback_request`) surfaces them to the operator in **two waves**, not a single batch:

1. **Wave 1 — phase_gate only.** When the phase first reaches `awaiting_human`, `pending_decisions` contains exactly one entry: the `phase_gate`. The agent-registered choice/feedback decisions are **deferred behind the gate** and are not yet in `pending_decisions`, even if the draft document enumerates them.

   The operator resolves the `phase_gate` via `provide_input` (`approve` / `request_changes` / `change_approach`).

2. **Wave 2 — deferred decisions.** On `approve`, the pipeline **stays in `awaiting_human`** and the orchestrator moves the deferred choice/feedback decisions into `pending_decisions`. They wake the next `wait-status` Monitor invocation via `decision.created`. The next phase does not start until all of them are resolved. On `request_changes` / `change_approach`, the deferred decisions are discarded with the phase reset — no Wave 2.

**Operator messaging implications** — when narrating a `phase_gate` approval to the user, do not say "approves and moves to the next phase". The accurate framing is: "approves the draft; if the phase registered deferred decisions, they will surface next for you to resolve before `<next phase>` starts." When the draft lists open questions that are not in the current `pending_decisions` snapshot, frame them as "these will surface as `<phase>`-phase decisions once the gate is approved", not "these will come up in the `<next phase>` phase".

**Handling rules by `decision_type`**:

- **`phase_gate`** — always alone. Handle individually per the section below.
- **`choice`** — may arrive in multiples. Apply the `resolved_questions_map` auto-resolution check (see below) to each one first; auto-resolved decisions are submitted immediately via `provide_input` and omitted from the prompt. For the remaining decisions, **group up to 4 into a single multi-question `AskUserQuestion` call** (one question per decision, that decision's `options` as the choices). After the user answers, call `provide_input` once per `decision_id` with `{"action": "select", "selected": "<chosen option>"}`. Repeat in groups of 4 until every choice decision is resolved. This collapses what was previously N prompts and N polling cycles into ~⌈N/4⌉ prompts and one cycle (#1956).
- **`feedback`** — typically at most one per phase. Handle individually; within a single feedback decision, continue to batch its `questions[]` array up to 4 per `AskUserQuestion` call (existing behavior, see the `feedback` subsection below).

For the rest of this section, "the decision" refers to a single entry being processed. When multiple `choice` decisions are pending, apply the batching rule above rather than prompting one at a time.

### Resolved Questions Map

Maintain a single session-scoped, in-memory dict named `resolved_questions_map` for the lifetime of the current `/sdlc` session. It maps `normalized_question_text → answer`, where:

- **Normalization rule**: apply `question.strip().lower()` — trim leading/trailing whitespace and lowercase. Use the same rule on every read and every write so lookups are symmetric. Do not normalize the stored answer value; keep the user's answer verbatim so downstream handlers can compare it against option lists exactly as the user gave it.
- **Scope**: the map lives in memory for the session only (no persistence to disk). Across multiple `phase_gate` events in the same pipeline, newer answers overwrite older ones at the same normalized key — no explicit clearing is needed.
- **Writers**: Step 5 of the `phase_gate` decision handler (below) populates this map as it collects answers to draft-embedded questions.
- **Readers**: the `choice` and `feedback` decision handlers (below) consult this map before prompting the user, so that questions the user already answered in a prior `phase_gate` are auto-resolved instead of re-prompted. Every auto-resolution prints a user-visible one-line note so an incorrect match is catchable.

If `resolved_questions_map` does not yet exist when a handler tries to read it, treat it as empty and fall through to the normal prompt flow.

### For `phase_gate` decisions (phase approval gates):

The full status snapshot from `get_status` enriches phase_gate decisions with `draft_content` (the phase's output document), `completed_agents_summary` (role + status for each completed agent), and `reviewer_feedback` (list of reviewer verdicts). `wait-status` JSON-lines do not carry these fields — re-fetch the snapshot via `get_status` whenever the line surfaces a phase_gate decision.

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

   **Deduplication** — Some questions in the draft may also appear as separate `pending_decisions` (choice/feedback type) that you'll handle individually in the next sections. To avoid double-prompting, compare question text (case-insensitive, trimmed) against the `question` field of all `pending_decisions` in the current batch. If a draft question matches a pending decision, skip it here — it will be handled when you process that decision type below. (Under two-wave surfacing, this check is a no-op during Wave 1 since choice/feedback decisions are deferred; `resolved_questions_map` handles cross-wave deduplication.)

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

   **As you collect each answer, also store it in `resolved_questions_map`** keyed by the normalized question text (`question.strip().lower()`) with the user's answer as the value. This happens in addition to building the Resolved Questions display block — both must be populated. Later `choice` and `feedback` decisions in the same session will consult this map to auto-resolve follow-up questions the user already answered here.

   If nothing in the draft requires user input beyond the approval itself, skip this step entirely — do not manufacture questions.

6. **Ask for approval** — Use `AskUserQuestion`:
   - **Question**: "Phase '<phase>' is complete. Do you approve the output above to proceed?"
   - **Header**: "Approval"
   - **Options**:
     - **"Approve"** — description: "Accept the draft and proceed (if the phase registered deferred decisions, they surface before the next phase begins)"
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

After resolving this decision, move to the next pending decision (if any) before resuming monitoring. **On `approve`**, resume monitoring — do not announce the next phase has started yet. If the phase had deferred choice/feedback decisions, the pipeline stays in `awaiting_human` and Wave 2 surfaces them (see [Two-wave surfacing](#two-wave-surfacing)); tell the user: "Approved. The phase's deferred decisions will surface next." If no deferred decisions exist, the pipeline transitions to the next phase normally — you will see the phase change on the next emitted `wait-status` JSON-line.

### For `choice` type decisions:

**Before prompting — check `resolved_questions_map` for a captured answer**:

1. Compute `normalized_q = decision.question.strip().lower()`.
2. Look up `resolved_questions_map[normalized_q]`. If the key is absent (or the map doesn't exist yet), fall through to the normal prompt flow below.
3. If a stored answer is present, compare it against each entry of `decision.options` using the same normalization (`option.strip().lower() == stored_answer.strip().lower()`). Pick the first matching option if any.
4. **On a compatible match**: skip `AskUserQuestion` entirely and auto-resolve the decision. Call `provide_input` with the matched option verbatim (use the option text from `decision.options`, not the normalized form):
   ```json
   {"action": "select", "selected": "<matched option verbatim>"}
   ```
   Then print a one-line user-visible note:
   ```
   Auto-resolved <decision_id>: selected '<option>' from captured context.
   ```
   Proceed to the next pending decision.
5. **On no match, or if the stored answer is a free-text / "Other" value that doesn't correspond to any option in `decision.options`**: fall through to the normal prompt flow below. Do not force an invalid selection.

**When multiple `choice` decisions are pending in the same batch, group up to 4 into a single multi-question `AskUserQuestion` call** (see the Phase 4 intro above). The per-decision formatting rules below still apply — each decision contributes one question (its `question` field) and a set of options (its `options` array) to the batched prompt. After collecting the user's answers, call `provide_input` once per `decision_id`.

If the decision includes a `draft_content` field, display it to the user first as context for the decision. If the content is long, show a summary of the key sections (headings and first paragraph of each) followed by the full content. This is especially important for decisions from the refine and plan phases, where the draft contains the analysis or plan that motivates the decision.

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

**Before prompting — consult `resolved_questions_map` for each question**:

1. Initialize an empty `prefilled_answers` dict and an empty `unmatched_questions` list.
2. For each entry in the decision's `questions` array:
   - Determine the answer key: use the question's `id` field if present, otherwise fall back to `q-<1-based index>` (as described in the paragraph below).
   - Compute `normalized_q = question.question.strip().lower()`.
   - Look up `resolved_questions_map[normalized_q]`. If present, add `prefilled_answers[<answer_key>] = <stored answer verbatim>`. Otherwise, append the question entry to `unmatched_questions`.
3. **All-matched fast path**: if `unmatched_questions` is empty, skip `AskUserQuestion` entirely. Call `provide_input` with the prefilled answers:
   ```json
   {"action": "submit_feedback", "answers": { ...prefilled_answers }}
   ```
   Then print a one-line user-visible note naming the decision ID and the question IDs that were auto-resolved, for example:
   ```
   Auto-resolved <decision_id>: answers for [q-1, q-2] prefilled from captured context.
   ```
   Proceed to the next pending decision.
4. **Partial match**: if `unmatched_questions` is non-empty, present only those questions via `AskUserQuestion` (using the normal grouping rules below — up to 4 questions per call). Collect the user's answers into a `new_answers` dict keyed the same way (question `id` or `q-<1-based index>`, preserving each question's original index in the full `questions` array). Merge: `answers = {...prefilled_answers, ...new_answers}`. Then call `provide_input` with the single merged payload:
   ```json
   {"action": "submit_feedback", "answers": { ...merged answers ... }}
   ```
   Print a one-line user-visible note naming the decision ID and listing which question IDs were auto-resolved from captured context (and, implicitly, which were prompted), for example:
   ```
   Auto-resolved <decision_id>: prefilled [q-1] from captured context; prompted for [q-2, q-3].
   ```
5. **No matches**: if `prefilled_answers` is empty after the scan, fall through to the normal prompt flow below without any auto-resolution note.

If the decision includes a `draft_content` field, display it to the user first as context for the feedback request. If the content is long, show a summary of the key sections (headings and first paragraph of each) followed by the full content. This is especially important for decisions from the refine and plan phases, where the draft contains the analysis or plan that motivates the questions.

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
| Message bus stats | Via `get_status` snapshot or `wait-status` JSON-line | `concurrent.consensus` field |

**When to use these during the workflow:**
- **Phase 3 (Monitor)**: If `wait-status` runs for an extended stretch with no emitted JSON-lines (genuine quiet — silence on no_change is normal), call `get_pipeline_snapshot` to check for failed containers and consensus state. If the pipeline uses concurrent agents (`EGG_CONCURRENT_MODE`), call `get_consensus_status` to see which agents are blocking — a stuck agent may be waiting on a NACK resolution or hasn't proposed yet. Show the user a summary of what you find.
- **Phase 4 (HITL)**: If `provide_input` fails, call `check_health` first. If the orchestrator is healthy, verify the decision state with a one-shot `get_status`. (The `wait-status` trigger allowlist deliberately excludes `decision.resolved`, so the CLI will not self-wake on the resolution we just submitted — re-fetch the snapshot directly.)
- **Phase 5 (Failure)**: Before offering re-run options, call `get_container_logs` for the failed agent to give the user context on what went wrong.

**Reading consensus state**: The `concurrent.consensus` object is present on every `wait-status` JSON-line where the route saw it, and on the full snapshot from `get_status`. It is populated only when agents are running in BRC mode. Key fields:
- `is_complete`: Whether all agents have confirmed
- `blocking_agents`: Roles not yet confirmed (tells you who's holding things up)
- `agents.<role>.producer_phase`: `WORKING` → `PROPOSED` → `CONFIRMED`
- `agents.<role>.reviewer_phase`: `WORKING` → `REVIEWING` → `CONFIRMED`
- `has_unresolved_nacks`: Whether any reviewer has NACKed without the producer re-proposing
- `unresolved_nacks`: List with `reviewer`, `producer`, `reason`, and `version` — surface these to the user when consensus is stuck

## MCP Tools Reference

All orchestrator and gateway interactions use the MCP tool surface. Never call REST APIs or CLIs directly.

| Tool | Purpose |
|------|---------|
| `submit_task` | Submit a new pipeline task |
| `get_status` | One-shot status snapshot (no cursor) — use for first poll and after `provide_input` |
| `skills/sdlc/bin/wait-status` (via Monitor) | Long-poll for status changes; emits JSON-lines on stdout — Monitor surfaces each line as its own notification, so the LLM wakes on every event. Host-side launcher around `sandbox/bin/egg-orch pipeline wait-status`. Replaces the prior `wait_for_status_change` MCP tool (#2211). Bash is a fallback only (events batch at exit). |
| `provide_input` | Respond to HITL decisions (serialize JSON payload as string) |
| `list_tasks` | List tasks for a repository |
| `cancel_task` | Cancel a running task |
| `check_health` | Verify orchestrator + gateway health |
| `list_containers` | List containers in a pipeline |
| `get_container_logs` | View agent logs (auto-selects container by role) |
| `send_message` | Send a message to an agent on the message bus |
| `get_consensus_status` | BRC consensus state: agent phases, blocking agents, unresolved NACKs |
| `get_phase` | Current phase, execution timing, review cycles |
| `get_pipeline_snapshot` | Comprehensive view: pipeline state, containers, messages, decisions |
| `get_contract` | SDLC contract state: task progress, pending decisions (gateway-backed) |
| `list_checkpoints` | Browse prior agent session transcripts (gateway-backed) |
| `search_checkpoints` | Search checkpoint metadata for keywords (gateway-backed) |

**Polling protocol:** First poll uses `get_status(task_id)` (MCP). Every subsequent quiet stretch uses one **Monitor** invocation wrapping `skills/sdlc/bin/wait-status <task_id> --since "<last_cursor>"` so each emitted JSON-line wakes the LLM individually. Bash is a fallback (events batch at the 10-min Bash cap). See [Host-Side Waits](../../docs/reference/agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status) for the full envelope contract and trigger allowlist.

## Critical Rules

- **Always use MCP tools** — never call orchestrator/gateway APIs or CLIs directly
- **First poll uses `get_status(task_id)` (MCP); every subsequent quiet stretch wraps `skills/sdlc/bin/wait-status <task_id> --since "<last_cursor>"` in the Monitor tool** — Monitor turns each emitted JSON-line into its own notification, which is what the per-event wake semantics of this skill require. Bash is a fallback only (events emitted in a single 10-min Bash window batch at exit). Thread the `cursor` from each emitted JSON-line into the next Monitor invocation's `--since`. The first `wait-status` call after the `get_status` snapshot uses an empty `--since` (route snaps to tip); `get_status` itself does NOT return a cursor. See [Host-Side Waits](../../docs/reference/agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status) for the trigger allowlist, envelope, and exit-code contract.
- **Read each emitted JSON-line as it arrives.** The CLI is silent on `no_change` — it only emits when something happened. Re-fetch the full snapshot via `get_status` whenever the dashboard needs fields not on the JSON-line (running_agents, completed_agents, recent_messages, enriched pending_decisions).
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
| `/sdlc --short --repo owner/repo Fix flaky test` | Repo override + task description |
| `/sdlc --short KORE-1234` | JIRA ticket (matches `<LETTER><ALPHANUMERIC>-<DIGITS>` pattern) |
| `/sdlc --short --repo owner/repo ENG-42` | Repo override + JIRA ticket |
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

Analyze the task locally — no remote agents. Your goal is to understand the full context, scope, and any ambiguity before planning. Draw from **all available context** — including JIRA ticket details, comments, linked issues, and Confluence docs gathered in Phase S1 — not just the code.

1. **Understand the problem context** — Before reading code, review the task description and any enriched context from Phase S1:
   - **User-facing symptom**: What is the observable problem? How does it manifest for users?
   - **Source context**: If from a JIRA ticket, note key details from the description, reporter comments, and linked issues/PRs. If from a GitHub issue, note the reporter's description and relevant comment thread.
   - **Workarounds**: Are there any known workarounds mentioned in the ticket or comments? These help scope the fix and inform urgency.

2. **Read relevant code files** — Based on the task description, identify and read the most relevant source files (up to 5–10 files). Use `Glob` and `Grep` to find them. Focus on files that will need modification and their immediate dependencies.

3. **Analyze scope** — Go deep enough that the plan writes itself. Determine:
   - How the affected component fits into the broader system (its role, callers, dependencies) — trace the call chain, not just the immediate file
   - Which files need to change and what specifically changes in each
   - The technical root cause (if this is a bug fix) — identify the exact code path that produces the incorrect behavior, not just the symptom
   - Edge cases and interactions — what existing behavior could break, what callers/consumers depend on the current behavior
   - Test coverage — are there existing tests for the affected code? Will they need updating?

4. **Clarify ambiguity** — If the task is ambiguous or underspecified, ask the user 2–3 clarifying questions via `AskUserQuestion` (group into a single call). Skip this if the task is clear and well-scoped.

5. **Present analysis** — Show the user a structured analysis that captures the full picture. The analysis should be thorough enough that someone unfamiliar with the code could understand the problem and the shape of the fix. Include all sections that are relevant — omit sections that don't apply (e.g., omit "Workarounds" for a new feature request):

```
### Task Analysis

**Problem statement**: <what the user experiences — the observable symptom, not the code-level bug>

**Source context**: <key details from the JIRA ticket, GitHub issue, or user description — reporter comments, linked PRs, related issues that inform the fix>

**Workarounds**: <any known workarounds mentioned in the ticket/comments, or "None known">

**System context**: <how the affected component works — its role, key call paths, and how it interacts with the rest of the system. Name the relevant functions/classes and describe the flow, not just "it handles X">

**Technical root cause**: <trace the exact code path that produces the bug or the gap that needs filling. Reference specific functions, conditions, and data flow. For bug fixes: what input/state triggers the issue, what the code does wrong, and why>

**Files affected**:
- `path/to/file1.py` — <what changes and why>
- `path/to/file2.ts` — <what changes and why>
- `path/to/test_file.py` — <new or updated tests>

**Risks / edge cases**: <what existing behavior must be preserved, what callers depend on the current interface, any non-obvious interactions — or "None identified" with brief reasoning>
```

6. **Confirm** — Ask the user to confirm the analysis is correct before proceeding to planning. Use `AskUserQuestion`:
   - **Question**: "Does this analysis look correct? Any adjustments before I create the plan?"
   - **Header**: "Confirm"
   - **Options**:
     - **"Looks good, proceed"** — description: "Continue to plan generation"
     - **"Adjust scope"** — description: "I'll clarify what should change"

If the user adjusts scope, incorporate their feedback and re-present. Then proceed to Phase S3.

## Phase S3 — Lightweight Plan & Contract

Generate a concrete plan with tasks and acceptance criteria, and produce a plan document with a `yaml-tasks` appendix that the remote pipeline can parse into a formal contract.

### Step 1: Generate the plan

Create a single-phase plan with 1–5 concrete tasks. The plan should give a coder enough context to implement without re-analyzing the problem from scratch.

**Plan-level context** (goes in the Summary section of the plan document):
- **Approach**: What strategy are we taking? If there were meaningful alternatives, briefly note why this one was chosen — skip this for straightforward fixes where the approach is obvious
- **Root cause** (for bug fixes): What's actually wrong and why does the current code behave incorrectly?
- **Risks / edge cases**: What could break, what existing behavior must be preserved, non-obvious interactions — "None identified" if genuinely none

**Per-task detail** — each task should include:
- **What to change**: Name the specific file(s), function(s), or class(es) being modified or created
- **How it changes**: A 1–3 sentence description of the implementation approach — specific enough that a coder knows what code to write (e.g., name the fields to add, the conditions to check, the error to raise)
- **Why** (if non-obvious): Brief rationale connecting the change to the root cause or goal

Each task should be:
- Specific and actionable — calibrate detail to complexity:
  - Simple: "In `gateway/auth.py:validate_token()`, add expiry check before the signature verification — tokens with `exp` in the past currently pass validation"
  - Complex: "In `orchestrator/consensus.py:handle_propose()`, add a version-match guard that rejects ACKs whose `proposal_version` is older than the current proposal. When an outdated ACK is found, remove it from `pending_acks` and send a `RE_ACK_REQUIRED` message to the affected reviewer — pre-proposal ACKs currently cause a deadlock because `check_consensus()` counts them as valid even though they reference a stale proposal"
  - Not: "Fix ACK race condition"
- Scoped to a single logical change
- Ordered by dependency (tasks that depend on others come later)

Also generate 1–3 acceptance criteria that describe how to verify the work is complete. Each criterion should be testable — describe the observable behavior, not the implementation.

### Step 2: Generate the plan document with yaml-tasks appendix

Generate a markdown plan document that includes a `yaml-tasks` structured appendix. This is the same format used by the plan agent in the normal flow — the remote pipeline parses it to populate the contract.

````markdown
# Plan: <short title>

## Summary

<1 paragraph: What is the approach and why this strategy? For bug fixes, what is the root cause?>

**Risks / edge cases**: <What could break, what existing behavior must be preserved, non-obvious interactions — "None identified" if genuinely none.>

## Implementation

### Phase 1: Implement

<brief description of what this phase covers>

**Tasks**:
1. **[task-1-1]** In `path/to/file.py:function_name()`, <what to change, how, and why>. Acceptance: <criteria>
2. **[task-1-2]** In `path/to/other.py`, <what to change and how>. Acceptance: <criteria>

```yaml
# yaml-tasks
pr:
  title: "<imperative summary, ≤70 chars>"
  description: |
    <2-3 sentence PR description>
  test_plan: |
    - Automated: <which tests cover the changes>
    - Manual: <specific steps a reviewer should take to verify>
  manual_steps: |
    Pre-merge: <any required steps before merging>
    Post-merge: <any required steps after merging>
phases:
  - id: 1
    name: Implement
    goal: "<what this phase achieves>"
    tasks:
      - id: task-1-1
        description: "In `path/to/file.py:function_name()`, <what to change and how> — <brief rationale>"
        acceptance: "<testable acceptance criteria>"
        files:
          - <path/to/file>
      - id: task-1-2
        description: "In `path/to/other.py`, <what to change and how>"
        acceptance: "<testable acceptance criteria>"
        files:
          - <path/to/file>
```
````

The `yaml-tasks` appendix **must** be present — it is machine-parsed by the remote pipeline to create the formal contract. The prose section above it provides human-readable context.

### Step 3: Present to user

Display the full plan document (the prose section from Step 2, excluding the yaml-tasks code block) followed by a contract summary:

```
### Plan

**Summary**: <approach paragraph from the plan document>

**Risks / edge cases**: <from the plan document>

**Phase**: Implement (single phase)

**Tasks**:
1. `task-1-1`: In `path/to/file.py:function()`, <what changes, how, and why>
2. `task-1-2`: In `path/to/other.py`, <what changes and how>
3. `task-1-3`: Add tests in `path/to/test_file.py` for <scenario>

**Acceptance Criteria**:
- `ac-1`: <testable observable behavior>
- `ac-2`: <testable observable behavior>

---

**Contract Summary**
Tasks: <N> | Acceptance Criteria: <N> | Phase: implement (single phase)
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
- **Request changes** → Ask what to change, update the plan, and re-present
- **Cancel** → Stop the workflow entirely

## Phase S4 — Submit

### Step 1: Validate plan and analysis content

Before calling `submit_task`, verify that the `plan` and `analysis` values contain **actual document content**, not references to files on other branches. This is critical — the remote pipeline cannot resolve cross-branch file references.

**Check for reference strings**: If either value looks like a pointer rather than content (e.g., contains phrases like "See the full plan at", "on the ... branch", or is under ~200 characters and references a file path), you must resolve it to actual content:

1. Extract the branch and file path from the reference
2. Read the full content: `git show origin/<branch>:<path>`
3. Use the retrieved content as the field value

This situation can arise when reusing artifacts from a previous pipeline run. The `plan` field **must** contain the full markdown plan document including the `yaml-tasks` code block. The `analysis` field **must** contain the full analysis text.

### Step 2: Call submit_task

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

Drive the pipeline through one Monitor invocation per quiet stretch. On entry:

1. **First poll** — call the `get_status(task_id)` MCP tool to render the initial dashboard and cache the snapshot as `last_status`. `get_status` does not return a `cursor`; the cursor is produced by `wait-status` only. Initialize `last_cursor = ""` (empty — the first `wait-status` call snaps to the tip).

2. **Blocking wait** — invoke `skills/sdlc/bin/wait-status` through the **Monitor** tool, not Bash. Monitor delivers each stdout line as its own notification, so the LLM wakes on every emitted event. Run from the repo root:

   ```
   Monitor(
     description: "wait-status <task_id>",
     command: "skills/sdlc/bin/wait-status <task_id> --since \"<last_cursor>\"",
     timeout_ms: 3600000,
   )
   ```

   The launcher wraps `sandbox/bin/egg-orch pipeline wait-status` and loops `/status/wait` server-side, threading the cursor between calls. Stdout is **JSON-lines** — one line per pipeline-relevant event, silent on `no_change`, surfaced as one notification per line. Exit codes:

   | Exit code | Meaning | Skill action |
   |-----------|---------|--------------|
   | `0` | Pipeline reached terminal state | Exit, move to Phase S6 |
   | `2` | Transient error budget exceeded | Re-invoke Monitor with same `last_cursor` |
   | `3` | Permanent error (4xx, malformed cursor, unknown pipeline) | Surface stderr; do NOT silently retry |
   | (timeout) | Monitor `timeout_ms` reached | Re-invoke Monitor with updated `last_cursor` |

   **Why Monitor and not Bash?** Foreground Bash blocks until the CLI exits and batches every event in that window into one wake; background Bash emits only a single completion notification. Both break the per-event wake semantics this skill relies on (e.g. surfacing `decision.created` the moment the gate is created). Monitor's per-line notifications match the streaming-stdout contract.

   **Bash fallback:** If Monitor isn't available, fall back to foreground Bash (`skills/sdlc/bin/wait-status <task_id> --since "<last_cursor>"`) — events will batch at the 10-min Bash cap, not stream as they arrive. On cap-elapsed exit, re-invoke with the latest `last_cursor` from the batched output.

3. **Read each emitted JSON line** — same shape as Phase 3:

   ```json
   {
     "trigger": "event",
     "event_type": "phase.started",        // wire values: phase.started / decision.created / pipeline.completed / etc.
     "cursor": "msg:1738012734-0|evt:142",
     "current_phase": "implement",
     "status": "running",
     "phase_elapsed_seconds": 127,
     "concurrent": { "consensus": { ... } }
   }
   ```

   For `trigger: "message"` the line carries `messages: [...]` instead of `event_type`. Update `last_cursor` from each line's `cursor` field — the cursor is opaque (`msg:<id>|evt:<seq>`); thread it through `--since` on the next Monitor invocation.

   **Trigger allowlist:** `OVERSEER_ALERT`, `CONSENSUS_CONFIRMED`, `CONSENSUS_NACK`, `CONSENSUS_RE_REVIEW`, `phase.started`, `phase.completed`, `pipeline.completed`, `pipeline.failed`, `pipeline.cancelled`, `decision.created`. `decision.resolved` is excluded so the host doesn't self-wake on a `provide_input` it just submitted.

4. **Render the dashboard** on each line:

   ```
   --- Pipeline Status ---
   Phase: <current_phase> | Status: <status> | Elapsed: <phase_elapsed_seconds>s
   Consensus: <confirmed count>/<total> confirmed   (when concurrent.consensus is present)
   Recent: <event_type or first messages[] entry>
   ```

   The JSON-line ships only the dashboard-relevant subset (`current_phase`, `status`, `phase_elapsed_seconds`, `concurrent.consensus`) — it does **not** include the full snapshot (agent list, recent_messages, pipeline metadata, `pending_decisions`). When you need the full envelope (e.g. on `decision.created` to render `pending_decisions` ahead of HITL), call `get_status(task_id)` as a one-shot and refresh `last_status`.

5. **State transitions:**
   - On `event_type: "decision.created"` → re-fetch the full snapshot via `get_status(task_id)` (the JSON-line does not carry `pending_decisions`) and handle the decision inline (see below).
   - On `status: "complete"` or `event_type: "pipeline.completed"` → exit, move to Phase S6.
   - On `status: "failed"` or `event_type: "pipeline.failed"` → apply the **failed status grace period** (see below) before exiting.

Keep the dashboard output concise. Only show changes from the previous emit when possible.

**Important: `wait-status` blocks server-side and emits events as they arrive. Do NOT wrap the Monitor invocation in an outer `for`-loop or `sleep` — the CLI is already the loop, server-side, and Monitor surfaces each emitted line as its own notification. The skill's liveness guarantee comes from the CLI re-issuing the route call with the threaded cursor on every Path-B no-change return; intra-process loop, no LLM turn.** When Monitor's `timeout_ms` (or the 10-min Bash cap, if you're on the fallback path) forces the CLI to terminate, simply re-invoke with the latest `last_cursor` from your conversation context. See [Host-Side Waits](../../docs/reference/agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status) for the event allowlist, exit-code contract, and concurrency model.

### Failed Status Grace Period

During phase cycle transitions (e.g., review cycles), the orchestrator may briefly report `status: failed` while spawning new containers. Treating this as terminal prematurely ends monitoring.

**Before treating `failed` as terminal, apply these checks:**

1. If `status` is `failed` but `running_agents` is non-empty → treat as "transitioning", not failed. Log: `"Status shows failed but agents still running — treating as cycle transition."` Continue polling.
2. If `status` is `failed` and `running_agents` is empty → call `get_pipeline_snapshot` MCP tool with the `task_id` to confirm actual state before exiting. If the snapshot shows active containers or recent messages, continue polling.
3. Only exit to Phase S6 when `status` is `failed`, `running_agents` is empty, **and** the secondary check confirms the pipeline is genuinely stopped.

### Stall detection

*Skip this block when `config.overseer_owns_host_detection` is `True` (issue #1962): the overseer's `phase-long-running` and `agent-stall` migrated detectors handle the trigger; the host renders the matching `OVERSEER_ALERT` via the existing alert flow.*

Track the `current_phase`, latest `recent_messages` entry, and elapsed time across polls. Use the server-computed `phase_elapsed_seconds` field for accurate timing when available; fall back to wall-clock tracking (`now - phase_entered_at`) when it is absent. If **10 minutes of elapsed time** pass with no phase change and no new messages, surface a warning:

```
### Potential Stall Detected

Pipeline has shown no progress for ~10 minutes.
```

Then offer three options via `AskUserQuestion`:

- **"Check logs"** — description: "View agent logs to diagnose the issue" — call the `get_container_logs` MCP tool with `task_id` and the agent's role (lines: 50). Show the user the output.
- **"Wait longer"** — description: "Give the agent more time (resets the stall timer)"
- **"Cancel"** — description: "Cancel this pipeline"

If "Wait longer" is selected, reset `phase_entered_at` to the current time and resume monitoring. If "Cancel", call `cancel_task` and move to Phase S6 failure handling.

### NACK handling

If the status shows unresolved NACKs in the consensus data, surface them to the user:

> **Reviewer raised concerns** — the coder is iterating on feedback. This is normal BRC behavior.

Only escalate if NACKs persist for 5+ minutes with no progress, at which point offer the same stall detection options.

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

- **Always use MCP tools for state-query operations** (`submit_task`, `get_status` for the first poll and one-shot snapshots, `provide_input`, `cancel_task`) — never call orchestrator APIs directly. **For blocking waits, wrap the host-side launcher `skills/sdlc/bin/wait-status` (host wrapper around `egg-orch pipeline wait-status`, issue #2211) in the Monitor tool** — the MCP transport caps tool calls below typical quiet-phase intervals, so an MCP-driven wait would burn an LLM turn on every cap-elapsed return; and Bash batches per-event JSON-lines into a single wake at exit, defeating the streaming-stdout design. Monitor surfaces each emitted line as its own notification. Thread the JSON-line `cursor` into the next Monitor invocation's `--since`. Bash is a fallback only when Monitor is unavailable. See [Host-Side Waits](../../docs/reference/agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status) for the contract.
- **Always serialize JSON payloads as strings** for `provide_input`
- **Always pass `config`** with `{"start_phase": "implement", "hitl_gates": false, "overseer_enabled": true}` when calling `submit_task`
- **Auto-approve phase gates** — this is a no-HITL flow; if a gate appears, approve it automatically and inform the user
- **Include yaml-tasks appendix** — the remote pipeline parses the `yaml-tasks` appendix from the plan to populate its own formal contract; local Pydantic validation is not needed
- **Pass plan via the `plan` field** — the plan document (with `yaml-tasks` appendix) must be passed as the `plan` argument to `submit_task`, not embedded in `description`. The `analysis` field carries the Phase S2 analysis. This ensures the remote pipeline creates a proper structured contract
- **Never pass file references as content** — the `plan` and `analysis` fields must contain the actual document text, not pointers like "See the full plan at .egg-state/drafts/... on the egg/... branch". The remote pipeline cannot resolve cross-branch file references. If reusing artifacts from a prior run, read the file content with `git show origin/<branch>:<path>` first
- **Stop polling on exit** — always exit the monitoring loop when the workflow ends
- **Handle errors gracefully** — if an MCP tool call fails, inform the user and offer to retry
- **Keep output concise** — don't flood the user with raw JSON; format status as a readable dashboard
