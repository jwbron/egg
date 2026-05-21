# Orchestrator CLI

Use `egg-orch` to interact with the orchestrator and gateway APIs.

Run `egg-orch --help` for full usage. All commands support `--json` for machine-readable output.

## Quick Reference

| Command | Purpose |
|---------|---------|
| `egg-orch health` | Check orchestrator + gateway health |
| `egg-orch env` | Show orchestrator environment variables |
| `egg-orch pipeline list` | List all pipelines |
| `egg-orch pipeline get <id>` | Get pipeline details |
| `egg-orch pipeline status <id>` | Get pipeline status |
| `egg-orch pipeline wait-status <id> [--since <cursor>]` | **Canonical host monitor idiom** — long-poll the pipeline for events, JSON-lines on stdout, exit codes per §3 contract. See [Agent Wait Patterns §7](agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status). |
| `egg-orch pipeline create --repo <owner/name>` | Create a pipeline |
| `egg-orch pipeline delete <id>` | Delete a pipeline |
| `egg-orch signal complete [<id>] --role <role>` | Signal agent completion |
| `egg-orch signal progress [<id>] --role <role> --percent <n>` | Signal progress |
| `egg-orch signal error [<id>] --role <role> --error <msg>` | Signal error |
| `egg-orch signal heartbeat [<id>] --role <role>` | Send heartbeat |
| `egg-orch phase get [<id>]` | Get current pipeline phase |
| `egg-orch phase advance [<id>]` | Advance to next phase |
| `egg-orch phase start [<id>]` | Start current phase |
| `egg-orch phase complete [<id>]` | Complete current phase |
| `egg-orch decision list [<id>]` | List HITL decisions |
| `egg-orch decision create [<id>] --question <text>` | Queue a decision |
| `egg-orch decision resolve [<id>] <did> --resolution <text>` | Resolve a decision |
| `egg-orch decision status [<id>]` | Decision queue summary |
| `egg-orch container list [<id>]` | List containers |
| `egg-orch container spawn [<id>] --role <role>` | Spawn a container |
| `egg-orch container logs [<id>] <cid>` | Get container logs |
| `egg-orch container stop [<id>] <cid>` | Stop a container |
| `egg-orch agent restart [<id>] <role> [--reason "..."]` | Restart a single stuck agent (stop, reset consensus, respawn). *CLI pending — use the REST API directly for now* |
| `egg-orch phase restart [<id>] <phase> [--reason "..."] [--context "..."]` | Restart an entire phase (stop all containers, reset consensus, respawn all). *CLI pending — use the REST API directly for now* |
| `egg-orch gateway health` | Check gateway health |
| `egg-orch gateway phase --issue <n>` | Get current phase from gateway |
| `egg-orch gateway permissions <phase>` | Get allowed ops for a phase |
| `egg-orch message send [<id>] --to <role\|all> --type <type> --subject "..." --body "..."` | Send directed or broadcast message. Types: `HANDOFF`, `STATUS`, `PROGRESS`, `HEARTBEAT`. (`QUESTION` was removed in [#1897](https://github.com/jwbron/egg/issues/1897).) |
| `egg-orch overseer alert [<id>] --anomaly <type> --priority <low\|medium\|high> --summary "..." [--detail "..."] [--recommend "..."] [--recommendation file_issue] [--recommendation-payload-file <path>]` | Broadcast `OVERSEER_ALERT` to human operator (overseer use only — always sets `message_type=OVERSEER_ALERT` and `to_role=all`). `--recommendation file_issue` attaches a structured advisor verdict; requires `--recommendation-payload-file` (JSON path with `issue_title`/`issue_body`/`priority`/`anomaly_signature`). |
| `egg-orch overseer file-issue [<id>] --anomaly-type <type> --priority <p0\|p1\|p2\|p3> --agent-role <role> --anomaly-signature <hex16> --issue-title-file <path> --issue-body-file <path> [--parent-alert-message-id <id>] [--dry-run] [--json]` | File a GitHub issue from the overseer role (advisor-gated). Checks `filed-issues.jsonl` + `gh issue list` for deduplication; skips filing if a matching open issue exists. Default stdout is plain text (`Filed issue #N (...)` or `Existing issue #N already covers ...`); with `--json`, prints JSON `{"issue_number": int, "filed": bool, "dedup_match": int\|null}`; with `--dry-run` (no `gh` invocation either way): if a dedup match is found, prints the same `--json` shape (with `issue_number`/`dedup_match` populated); otherwise prints `{"issue_number": null, "filed": false, "dedup_match": null, "dry_run": true, "argv": [...], "title": str, "body_bytes": int}`. Requires `EGG_PIPELINE_REPO` env var. |
| `egg-orch overseer consult-advisor [<id>] --inputs-file <path> [--output-file <path>] [--recent-log-bytes-cap <n>] [--json]` | Consult the advisor for a structured `AdvisorVerdict` (sandbox-side LLM call). Reads a JSON inputs file with `classification`, `health_alerts`, `progress_events`, and `recent_log_lines`. Returns `decision` (`alert`\|`file_issue`\|`watch`), `priority`, `alert_summary`, `alert_detail`, `issue_title`, `issue_body`, and `reasoning`. Without `--output-file`, the JSON verdict is written to stdout; with `--output-file`, the verdict is written to that path and stdout shows a confirmation message (pass `--json` to additionally echo the verdict to stdout). When `[<id>]` (or `EGG_PIPELINE_ID`) is set, the verb reads `PipelineConfig.overseer_advisor_model` and `PipelineConfig.overseer_advisor_recent_log_bytes_cap` from the orchestrator status endpoint and passes the configured values to `consult_advisor`; falls back to the `opus` model default and 256 KiB byte cap if the pipeline ID is absent or the lookup fails. `--recent-log-bytes-cap` overrides the `PipelineConfig.overseer_advisor_recent_log_bytes_cap` value; oldest lines are dropped first when the block exceeds the cap. `0` disables the cap. |
| `egg-orch message poll [<id>] [--since <id>] [--limit <n>]` | Poll for messages from other agents (concurrent mode) |
| `egg-orch message wait [<id>] --for <TYPE>... [--timeout N] [--since <id>] [--from-producer <role>]... [--slice <id>]` | Block until a **new** typed BRC event arrives (cursor-less calls start at stream tip — already-seen events are skipped). Cursor threading across re-entries is automatic when `EGG_AGENT_ROLE` is set (issue #2323). Pass `--since <id>` to resume from a specific anchor when needed. `--from-producer` (repeatable; also accepts comma-separated values, e.g. `--from-producer coder,tester`) restricts wakes to messages from the named senders; defaults to `$EGG_WAIT_PRODUCER_ALLOWLIST`. `--slice` restricts wakes to messages for the named slice (messages with null `slice_id` always pass through); defaults to `$EGG_SLICE_ID`. Exit 0 = matched, 1 = timeout, 2 = transient (retry-safe), 3 = permanent. See [Agent Wait Patterns §3](agent-wait-patterns.md#3-exit-code-contract-for-egg-orch-message-wait) |
| `egg-orch message wait-loop [<id>] --for <TYPE>... [--since <id>] [--from-producer <role>]... [--slice <id>]` | **Canonical STAY ALIVE idiom** — loops `message wait` server-side until a new matching event arrives (defaults to stream-tip; cursor threading across re-entries is automatic when `EGG_AGENT_ROLE` is set — issue #2323). `--from-producer` and `--slice` auto-apply from `$EGG_WAIT_PRODUCER_ALLOWLIST` / `$EGG_SLICE_ID` when set by the spawner (#2725). Do not wrap in an outer shell loop. See [Agent Wait Patterns §1](agent-wait-patterns.md#1-the-canonical-idiom) |
| `egg-orch message heartbeat [<id>] --state <WORKING\|WAITING_ON_ROLE\|WAITING_FOR_EVENT\|PROPOSED\|IDLE> [--waiting-on <role>] [--since <ts>]` | Emit a structured `HEARTBEAT` message on state transitions. `WAITING_ON_ROLE` requires `--waiting-on`. `WAITING_FOR_EVENT` is emitted automatically by `egg-orch message wait-loop` while blocked — agents don't need to emit it manually. Rate-limited by `EGG_HEARTBEAT_RATE_LIMIT` (per-role, 429 on exceed). See [Agent Wait Patterns §4](agent-wait-patterns.md#4-heartbeat-message-type) |
| `egg-orch message status [<id>]` | Get message bus status (concurrent mode) |
| `egg-orch signal readiness [<id>] --state <WORKING\|READY\|BLOCKED\|OBJECTING> [--reason "..."]` | Signal readiness state (concurrent mode) |
| `egg-orch push` | Push current branch via the gateway. The gateway rejects pushes that modify restricted paths (`403 restricted_path_modified`); drop the offending edits and re-propose with `--pre-merge-condition` ([#2039](https://github.com/jwbron/egg/issues/2039)) |
| `egg-orch progress emit --step <text> --state <working\|blocked\|complete> [--detail <text>] [--blocker <text>]` | Emit structured progress event |
| `egg-orch progress query [--agent <role>] [--since <timestamp>] [--limit <n>]` | Query structured progress events |
| `egg-orch health alerts [--pipeline <id>]` | List active deterministic health alerts |
| `egg-orch health resolve [<id>] --agent-id <id> --alert-type <type>` | Resolve (remove) health alerts for an agent |
| `egg-orch anchor init --task <text>` | Create initial anchor for current agent |
| `egg-orch anchor update [--status <s>] [--progress <json>] [--decision <json>] [--key-context <text>] [--error <text>] [--file <path>]` | Update agent anchor (atomic, all-or-nothing) |
| `egg-orch anchor show [--agent <id>] [--team]` | Show own anchor, another agent's, or team anchor |
| `egg-orch anchor validate` | Validate anchor schema and size limits |
| `egg-orch anchor cleanup` | Remove orphaned anchor files |

Pipeline ID can be omitted when `EGG_PIPELINE_ID` is set (auto-set in orchestrated mode).
Agent role can be omitted when `EGG_AGENT_ROLE` is set.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `EGG_ORCHESTRATOR_URL` | Orchestrator URL (default: `http://egg-orchestrator:9849`) |
| `EGG_PIPELINE_ID` | Current pipeline ID (auto-set in orchestrated mode) |
| `EGG_AGENT_ROLE` | Current agent role (auto-set in orchestrated mode) |
| `EGG_ISSUE_NUMBER` | Current issue number |
| `EGG_BRANCH` | Target branch for the agent's worktree (auto-set; defaults to `egg/{pipeline_id}/work`) |
| `EGG_REPO_PATH` | Repository path (auto-set; points to specific repo when one exists, otherwise `~/repos/`) |
| `EGG_AUTHORSHIP_REPO` | Override which repo the commit-authorship store uses in multi-repo deployments. Accepts an absolute path or a repo directory name relative to `EGG_REPO_PATH`. When unset, the store prefers a repo named `egg`; falls back to the first repo alphabetically. |
| `GATEWAY_URL` | Gateway URL (default: `http://egg-gateway:9848`) |
| `EGG_CONCURRENT_MODE` | `true` when running in concurrent execution mode |
| `EGG_MESSAGE_POLL_INTERVAL` | Suggested message polling interval in seconds (default: 30) |
| `EGG_MESSAGE_POLL_MAX_WAIT` | Server-side cap (seconds) on `message wait --timeout`. Default `60`, minimum `1`. Values `> 90` trigger a startup `warnings.warn` + WARNING log because the gateway's baked-in Squid `read_timeout` / `request_timeout` directives cap backend long-polls at ~60s — raising the cap above that requires a gateway image rebuild, not a ConfigMap edit. See [Agent Wait Patterns §6](agent-wait-patterns.md#6-egg_message_poll_max_wait--long-poll-cap-coupling). |
| `EGG_SLICE_ID` | Set by `kubernetes_spawner` for every slice-scoped agent. When set, `message wait` / `message wait-loop` only match messages whose `metadata.slice_id` equals this value OR is null (pipeline-level passthrough — OVERSEER_ALERT and global phase signals continue to wake every waiter). Override with `--slice`. See [Agent Wait Patterns — Auto-scoping](agent-wait-patterns.md#auto-scoping-by-slice-and-producer-allowlist-2725). |
| `EGG_WAIT_PRODUCER_ALLOWLIST` | Set by `kubernetes_spawner` for agents that participate in the BRC review graph. Comma-separated list of sender roles; when set, `message wait` / `message wait-loop` only match messages from the listed senders. Spawner derives the set from the review-graph neighbors plus system senders `overseer` and `orchestrator`. Override with `--from-producer`. See [Agent Wait Patterns — Auto-scoping](agent-wait-patterns.md#auto-scoping-by-slice-and-producer-allowlist-2725). |
| `EGG_ORCH_STATE_STORE_PROBE_INTERVAL` | Cadence (seconds) of the background state-store self-heal probe. Default `15`. Lowering tightens wedge-detection at the cost of more frequent `git` calls; raising it does the inverse. The staleness watchdog flips `/api/v1/ready` to 503 when cache age exceeds `interval × 2`, so this setting also controls the readiness-flap window. Values above ~30s can exceed the readinessProbe's boot tolerance (`initialDelaySeconds + periodSeconds × failureThreshold = 35s`). |
| `EGG_ORCH_WAITRESS_THREADS` | Waitress WSGI thread pool size. Default `16`, minimum `4`. Values `< 4` cause the orchestrator to `sys.exit(78)` (EX_CONFIG) at boot with an ERROR log. Each blocking long-poll occupies one thread; size the pool above the concurrent-agent count plus short-request headroom. The `egg_inflight_long_polls` Prometheus gauge exposes saturation. See [Agent Wait Patterns §7](agent-wait-patterns.md#7-egg_orch_waitress_threads--thread-pool--long-poll-coupling). |
| `EGG_HEARTBEAT_RATE_LIMIT` | Per-`(pipeline_id, agent_role)` `HEARTBEAT` rate cap (messages per minute). Default `20`. Exceeding returns HTTP 429 with a `Retry-After` header; the CLI surfaces 429 as exit 3 (permanent). See [Agent Wait Patterns §5](agent-wait-patterns.md#5-egg_heartbeat_rate_limit--per-role-heartbeat-cap). |
| `EGG_ORCH_MAX_PARALLEL_SLICES` | Maximum number of implement-phase slices that may run concurrently within a **single pipeline** wave. Default `2`. Backed by `SliceScheduler.max_parallel_slices`; reduce when container or gateway resources are constrained. |
| `EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES` | Orchestrator-process-wide cap on slices in flight across **all** running pipelines (#2241). Default `4` (each slice spawns ~8 containers; host saturates beyond this). Slices that exceed the cap stay READY and re-yield next 5 s poll tick — per-pipeline `iter_ready` accounting is unaffected. Per-process: HA replicas each maintain their own counter, they do not coordinate across processes. The current state is exposed at `/api/v1/pipelines/{id}` under `slice_admit: {cap, admitted, admitted_keys}`. |
| `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` | Per-slice BRC re-proposal ceiling before HITL escalation. Default `3`. Part of the two-tier cycle cap model (#2137 decision-9). *API live, not yet wired in the run loop — see [Slice-DAG Implement Phase](../architecture/slice-dag.md); #2199.* |
| `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` | Pipeline-wide cap on the summed total of slice re-proposal cycles. Default `10`. Either the local or global cap tripping escalates to HITL. *API live, not yet wired in the run loop — see [Slice-DAG Implement Phase](../architecture/slice-dag.md); #2199.* |
| `EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS` | Grace window (seconds) between a slice failure and the orchestrator marking the downstream subtree `BLOCKED_ON_FAILED_DEPENDENCY`. Default `60`. Allows HITL resolution before the cascade fires. |
| `EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS` | Polling cadence (seconds) of the stacked-PR reconciler that detects child slice PRs whose base branch was deleted after a parent merge. Default `30`. |
| `AGENT_ANCHOR_ID` | Agent anchor ID (`{role}-{short_container_id}`), auto-set by container spawner |
| `EGG_LIFECYCLE_SECRET` | Bearer token required for lifecycle-control endpoints (HITL resolve/cancel, pipeline CRUD, phase overrides, container spawn/stop). Stored at `~/.config/egg/lifecycle-secret`. Must be exported in the human's shell to run `egg-orch decision resolve`, `egg-orch pipeline delete`, etc. Agent pods never receive it (see #1769). |

## Common Workflows

**Check system health:**
```bash
egg-orch health
```

**Monitor a pipeline:**
```bash
egg-orch pipeline status <pipeline-id>
egg-orch phase get <pipeline-id>
egg-orch decision list <pipeline-id>
```

**Signal from within agent execution:**
```bash
egg-orch signal progress --percent 50 --task "Running tests"
egg-orch signal complete --commit abc1234
egg-orch signal error --error "Test failure" --recoverable
```

**Send directed messages to peers (concurrent mode):**
```bash
# HANDOFF: Signal a role-boundary artifact for another agent
egg-orch message send --to tester --type HANDOFF \
  --subject "Test files for auth module" \
  --body "Test scaffolding ready — see commit abc1234"

# STATUS: Inform a peer of your current state
egg-orch message send --to reviewer_code --type STATUS \
  --subject "Docs in progress" \
  --body "Documentation not ready for review yet, finishing API docs"
```

> `QUESTION` was removed in [#1897](https://github.com/jwbron/egg/issues/1897). To advertise state without a reply handler, use `egg-orch message heartbeat` (structured, typed, rate-limited). To ask a clarifying question of a producer you are reviewing, put the question in your `egg-orch consensus nack --reason "..."` so it lands in the BRC history where the producer will address it on re-propose.

See [Directed Coordination](../guides/concurrent-execution.md#directed-coordination) for detailed usage guidance and worked examples.

**Wait for BRC events (canonical STAY ALIVE idiom):**
```bash
# Producer STAY ALIVE — wakes on consensus, re-review, or overseer alert
egg-orch message wait-loop \
  --for CONSENSUS_CONFIRMED \
  --for CONSENSUS_RE_REVIEW \
  --for OVERSEER_ALERT

# Reviewer STAY ALIVE — additionally wakes on new proposals
egg-orch message wait-loop \
  --for CONSENSUS_PROPOSE \
  --for CONSENSUS_RE_REVIEW \
  --for CONSENSUS_CONFIRMED \
  --for OVERSEER_ALERT

# One-shot block — used inside scripts that want a single match (exit 0 = matched, 1 = timeout, 2 = transient, 3 = permanent)
egg-orch message wait --for CONSENSUS_PROPOSE --from coder --timeout 60
```

Do not wrap `wait-loop` in an outer shell `for`-loop or `sleep` — it is already the outer loop, server-side. See [Agent Wait Patterns](agent-wait-patterns.md) for the full contract, the five anti-patterns to avoid, and the exit-code table.

**Emit a structured heartbeat (state transitions only):**
```bash
# Entering WORKING after ORIENT
egg-orch message heartbeat --state WORKING

# Transitioning to blocked-on-peer
egg-orch message heartbeat --state WAITING_ON_ROLE --waiting-on coder

# After submitting a proposal
egg-orch message heartbeat --state PROPOSED

# Between tasks
egg-orch message heartbeat --state IDLE
```

`message heartbeat` POSTs to a dedicated `/api/v1/pipelines/{id}/heartbeat` endpoint with schema validation, server-side dedup (consecutive identical `(state, waiting_on)` tuples are silently dropped), and a per-`(pipeline_id, agent_role)` rate limit (`EGG_HEARTBEAT_RATE_LIMIT`, default 20/minute). It is the supported successor to `egg-orch signal heartbeat`, which remains for legacy scripts but does not carry typed state.

**Emit structured progress (health monitoring):**
```bash
egg-orch progress emit --step "running tests" --state working --detail "pytest suite 3/5"
egg-orch progress emit --step "blocked on dependency" --state blocked --blocker "waiting for coder"
egg-orch progress query --agent coder
```

**Check health monitoring alerts:**
```bash
egg-orch health alerts
egg-orch health alerts --pipeline issue-123

# Resolve (remove) alerts after an issue is addressed
egg-orch health resolve --agent-id coder --alert-type heartbeat_timeout

# Or specify an explicit pipeline ID
egg-orch health resolve issue-123 --agent-id coder --alert-type heartbeat_timeout
```

**Restart a stuck agent or phase:**
```bash
# Restart a single agent (preserves worktree, resets consensus state)
egg-orch agent restart coder --reason "Agent hung after Edit tool error"

# Restart with explicit pipeline ID
egg-orch agent restart issue-1551 coder --reason "No heartbeat for 10 minutes"

# Restart an entire phase (stops all containers, resets consensus + review cycles)
egg-orch phase restart implement --reason "Multiple agents stalled"

# Restart phase with additional context injected into respawned agents
egg-orch phase restart implement \
  --reason "Consensus corrupted after restart cascade" \
  --context "Previous attempt stalled during BRC convergence — focus on completing reviews first"
```

Agent restart preserves the agent's existing worktree (including committed work on the branch) and resets only that agent's consensus state (proposals, ACKs, NACKs, confirmations). Phase restart resets all consensus state and review cycle counters for the phase, then respawns all agents from scratch while preserving prior phase artifacts and branch commits. Before deleting worktrees, phase restart enumerates all per-agent worktrees from disk (including slice-scoped worktrees) and auto-salvages any committed-but-unpushed work to `egg/recovered/…` refs — the same salvage path as `cleanup_pipeline`.

> **Note:** CLI commands for restart are pending implementation. In the meantime, use the REST API directly or the MCP tools:
> ```bash
> # Agent restart via REST API
> curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/agents/<role>/restart \
>   -H "Content-Type: application/json" -d '{"reason": "Agent hung"}'
>
> # Slice-scoped agent restart (pass slice_id via query param or body)
> curl -X POST "http://egg-orchestrator:9849/api/v1/pipelines/<id>/agents/<role>/restart?slice_id=slice-2" \
>   -H "Content-Type: application/json" -d '{"reason": "Slice agent hung"}'
>
> # Omitting slice_id for a per-slice agent: the endpoint derives the slice
> # from the phase's agent records (#2759). If exactly one slice has a
> # non-complete record for the role, that slice is used; otherwise the
> # request is rejected with HTTP 400 reason "slice_id_required" and a
> # "details" object listing known_slices and restart_candidates — re-issue
> # with an explicit slice_id. This prevents a slice-mode restart from
> # silently spawning an unscoped agent whose BRC signals route to the bare
> # pipeline tracker, wedging the slice's consensus.
>
> # If slice_id is unknown, the endpoint returns HTTP 404 with a "success: false"
> # envelope and a "details" object containing slice_id and known_slices:
> # {"success": false, "message": "slice_id 'slice-99' does not match any slice ...",
> #  "details": {"slice_id": "slice-99", "known_slices": ["slice-1", "slice-2"]}}
> # Pipelines without a contract (CUSTOM+PR, and CUSTOM without inline
> # analysis/plan or issue-mode contract file) reject any slice_id with 404 and
> # known_slices: []; the message reads "... is invalid for pipeline <id>
> # (pipeline has no contract; not slice-aware)" instead.
>
> # Phase restart via REST API
> curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phases/<phase>/restart \
>   -H "Content-Type: application/json" -d '{"reason": "Phase stalled", "context": "Focus on X"}'
> ```

**Manage agent anchors (post-compaction recovery):**
```bash
# Initialize anchor for current task
egg-orch anchor init --task "Fix auth bypass in gateway/auth.py"

# Update anchor with progress, decisions, context
egg-orch anchor update --status in_progress \
  --progress '{"state":"current","description":"Fixing token validation"}' \
  --decision '{"with_agent":"tester-def67890","decided":"Use parametrized tests"}'

# View own anchor, another agent's, or team view
egg-orch anchor show
egg-orch anchor show --agent coder-abc12345
egg-orch anchor show --team

# Validate schema and size budget
egg-orch anchor validate
```

## Phase Management MCP Tools

Five MCP tools expose phase- and pipeline-level recovery operations, eliminating the need for raw `curl` calls during stuck pipeline scenarios (see [#1570](https://github.com/jwbron/egg/issues/1570) for motivation; pipeline-level recovery added in [#2411](https://github.com/jwbron/egg/issues/2411)).

| MCP Tool | REST Endpoint | Description |
|----------|---------------|-------------|
| `start_pipeline` | `POST /pipelines/{id}/start` | Recover a non-RUNNING pipeline (FAILED, AWAITING_HUMAN with all decisions resolved, or PENDING — the route has no early-return for PENDING). Resets the current phase to PENDING (clears `containers`, `agents`, `artifacts`), bumps `run_epoch`, sets `pipeline.status = RUNNING`, and re-launches the `_run_pipeline` thread. **Distinct from `start_phase`** — targets pipeline-level state. Before the reset, the route label-queries k8s for pods carrying `egg.pipeline.id=<id>` and refuses with 409 (`live_pods_present`) if any are alive, to avoid orphaning live work (#2420). Pass `force=true` (with an optional `force_reason` audit note) to override after `cancel_task(cleanup=true)` |
| `advance_phase` | `POST /pipelines/{id}/phase` | Advance pipeline to a target phase. With `force=true`, stops running containers first to prevent SIGTERM cascading. When leaving the plan phase, automatically populates the contract from the plan draft |
| `start_phase` | `POST /pipelines/{id}/phase/start` | Mark the current phase RUNNING. Does **not** spawn agents — agent spawning is driven by the `_run_pipeline` loop. Use for operator recovery when a phase needs to be re-marked RUNNING |
| `complete_phase` | `POST /pipelines/{id}/phase/complete` | Mark a phase COMPLETE. Does **not** advance the pipeline — call `advance_phase` next. Response includes `current_phase` (unchanged) and `next_phase` (suggested transition). Returns 409 if unresolved HITL decisions exist; pass `force=true` to abandon them |
| `populate_contract` | `POST /pipelines/{id}/phase/populate-contract` | Populate contract from plan artifacts. Parses yaml-tasks from the plan draft into contract phases/tasks |

**Parameters:**

All tools require `task_id` (the pipeline ID). Additional parameters:

- **`start_pipeline`**: `force` (boolean, optional, default `false`) — skip the live-pod orphan guard and reset the phase even if pods labeled to the pipeline are still alive (#2420). `force_reason` (string, optional) — audit note explaining why `force=true` was used; recorded in the orchestrator log.
- **`advance_phase`**: `target_phase` (string, required) — the phase to advance to (e.g., `"plan"`, `"implement"`, `"pr"`). `force` (boolean, optional, default `false`) — skip validation and stop running containers before advancing. **Important:** When `force=true`, containers from the current phase are stopped before the transition to prevent their SIGTERM signals from being misinterpreted as failures in the new phase. When the current phase is `plan`, `advance_phase` automatically runs the contract populate step (parsing the plan's `yaml-tasks` appendix into the contract (phases, tasks, and `contract.pr` metadata)), so a separate `populate_contract` call is not needed for plan→implement transitions. It also triggers the context PR hook (#2593) — the same hook that fires on the inline auto-advance path — so the doc-only base PR is opened regardless of which transition path the operator uses.
- **`start_phase`**: No additional parameters.
- **`complete_phase`**: `artifacts` (object, optional) — phase completion artifacts to store (e.g., commit SHAs, PR URLs).
  - Returns 409 when the current phase has unresolved HITL decisions (both orchestrator-side and contract-side decisions scoped to the phase are checked).
  - `force` (boolean, optional, default `false`) — skip the unresolved-decision guard and complete the phase anyway; abandoned decision IDs are recorded in the phase's artifacts for audit.
  - `force_reason` (string, optional) — audit note explaining why `force=true` was used.
- **`populate_contract`**: No additional parameters. Resolves the pipeline's worktree path, reads the plan document, extracts task structure, and writes tasks and acceptance criteria to the contract. Returns phase and task counts on success.

**Error reason codes** (#1939): The four endpoints normally surface a stable, machine-readable `reason` field in error responses — switch on `reason` rather than parsing the human-readable `message`. One case breaks this convention and is noted inline in the table: `populate_contract`'s `forest_violation` 422 ships the structured errors under an `error` key (no `reason`). Key codes:

| Endpoint | `reason` | HTTP | Meaning / fix |
|----------|----------|------|---------------|
| `advance_phase` | `missing_target_phase` | 400 | `target_phase` omitted from request |
| `advance_phase` | `invalid_phase` | 400 | `target_phase` is not a known phase value |
| `advance_phase` | `invalid_phase_transition` | 400 | Not a valid transition from the current phase; change target or pass `force=true` |
| `advance_phase` | `previous_phase_not_complete` | 400 | Current phase still running or failed; call `complete_phase` first, or pass `force=true` |
| `advance_phase` | `health_checks_failed` | 409 | Tier 1/2 health checks returned `FAIL_PIPELINE`; `details.health_results` lists failing checks. Resolve the underlying issue or pass `force=true` |
| `start_pipeline` | `live_pods_present` | 409 | Pods labeled to the pipeline are in a live phase (`Pending` / `Running`, plus the orchestrator-internal `Creating` transient — never observed on k8s, where `_pod_phase_to_status` maps `Pending`/`Running`/`Failed`/`Succeeded`/`Unknown` only; pods in terminal `Failed` / `Succeeded` phases are excluded since they have already exited and the reset orphans no work tied to them); the reset would orphan them. Cancel them first (`cancel_task(cleanup=true)`) or pass `force=true`. `details.live_pod_count` carries the count of pods in live phases. Note: pods on unreachable nodes report phase `Unknown` and are mapped to `FAILED`, so they are **excluded** from the live count — the reset will proceed silently for these. If a pipeline's pods are on an unreachable node, manually verify before relying on a zero count |
| `start_pipeline` | `live_pod_check_failed` | 409 | Label query for live pods failed (k8s API error); pass `force=true` after manual verification. **No `details.live_pod_count`** is included — the count is unknown by definition |
| `start_pipeline` | `invalid_force_reason` | 400 | `force_reason` must be a string |
| `start_phase` | `phase_already_running` | 400 | Phase is already in `RUNNING` status; no action needed |
| `complete_phase` | `unresolved_hitl_decisions` | 409 | Phase has pending HITL decisions; `details.unresolved_decision_ids` lists them. Resolve or pass `force=true` |
| `complete_phase` | `invalid_artifacts` | 400 | `artifacts` must be a JSON object with string values |
| `complete_phase` | `invalid_force_reason` | 400 | `force_reason` must be a non-empty string |
| `populate_contract` | `draft_missing` | 404 | Plan draft missing from the pipeline's local worktree at the configured draft path; re-run the plan phase or restore the file before retrying. The HTTP endpoint calls `_populate_contract_from_plan` directly, so this is a local-only check — the safe wrapper's extra origin lookup (which raises `PlanDraftMissingOnLocal{,AndOrigin}Error` instead) only applies to internal `source="plan_complete"` callers, not HTTP |
| `populate_contract` | `no_draft_path` | 404 | No draft path configured for this pipeline |
| `populate_contract` | `parse_failed` | 422 | `parse_plan` returned `success=False` — e.g., empty plan document, missing or malformed `yaml-tasks` appendix, or other parser-rejected input |
| `populate_contract` | `empty_result` | 422 | Parse succeeded but produced no slices/tasks **and** no PR metadata (`changed=False`); a draft yielding only a `pr_title` would still come back as `POPULATED` |
| `populate_contract` | `forest_violation` | 422 | Plan emitted a multi-parent slice DAG (#2137). **Response body shape is `{"error": "forest_violation", "errors": [...]}` — note the key is `error`, not `reason`** (clients switching only on `reason` will miss this case). The structured errors are also stashed on `contract.plan_review_feedback` so the plan reviewer NACKs the planner |
| `populate_contract` | `contract_load_failed` | 500 | Existing contract on disk could not be loaded prior to population (the load happens first; if it fails the populator never runs) |
| `populate_contract` | `egg_contracts_unavailable` | 500 | `egg_contracts` package failed to import in the orchestrator process (the endpoint runs orchestrator-side, not in the agent sandbox) |
| `populate_contract` | `unexpected_exception` | 500 | Unexpected internal error inside the populator helper (`_populate_contract_from_plan`'s catch-all) |
| `populate_contract` | `populate_contract_failed` | 500 | Residual catch-all for exceptions that escape the populator helper itself — e.g., `get_state_store_for_pipeline` / `resolve_worktree_path` raise, or the dynamic `from routes.pipelines import …` fails. Normally pre-empted by one of the specific codes above |
| `advance_phase`, `start_phase`, `complete_phase`, `fail_phase` | `version_conflict` | 409 | Concurrent modification detected; retry the request |
| all | `invalid_pipeline_id` | 400 | Pipeline ID format is invalid |
| all | `pipeline_not_found` | 404 | No pipeline with that ID exists |

The REST-only endpoints `fail_phase` and `get_current_phase` also include `reason` in error responses (e.g., `missing_error_message` for `fail_phase`) plus the shared `invalid_pipeline_id` and `pipeline_not_found` codes. `fail_phase` additionally emits `version_conflict`.

Note: reason codes are present in the raw HTTP response. The MCP handler layer does not yet surface them to tool callers.

**Recovery workflow example (stuck pipeline):**
```bash
# 1. Check current state
egg-orch phase get <pipeline-id>

# 1a. If pipeline is FAILED with the current phase still RUNNING (a state startup
#     reconciliation can produce on partial agent-state loss after an orch
#     restart — see #2411), use start_pipeline. It resets the failed phase to
#     PENDING and re-launches the runner. Returns 409 with reason=live_pods_present
#     if pods labeled to the pipeline are still alive — cancel them first via
#     cancel_task(cleanup=true) or pass force=true to override (#2420).
# Via MCP tool: start_pipeline(task_id="<id>")
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/start
# Override the live-pod guard after manual cleanup:
# Via MCP tool: start_pipeline(task_id="<id>", force=true, force_reason="Cleaned up via cancel_task")
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/start \
  -H "Content-Type: application/json" \
  -d '{"force": true, "force_reason": "Cleaned up via cancel_task"}'

# 2. Force-advance past a stuck phase (stops running containers first)
# Via MCP tool: advance_phase(task_id="<id>", target_phase="implement", force=true)
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phase \
  -H "Content-Type: application/json" \
  -d '{"target_phase": "implement", "force": true}'

# 3. Populate contract if it's still empty (automatic when advancing from plan;
#    needed for other phase transitions where the plan was set up externally)
# Via MCP tool: populate_contract(task_id="<id>")
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phase/populate-contract

# 4. Mark the phase running (does not spawn agents — the _run_pipeline loop handles that)
# Via MCP tool: start_phase(task_id="<id>")
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phase/start

# 5. If needed, manually complete a phase
# Via MCP tool: complete_phase(task_id="<id>")
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phase/complete
# Returns 409 if unresolved HITL decisions exist — resolve them or use force=true:
# Via MCP tool: complete_phase(task_id="<id>", force=true, force_reason="Manual recovery")
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phase/complete \
  -H "Content-Type: application/json" \
  -d '{"force": true, "force_reason": "Manual recovery: decision stale"}'
```

## Salvage MCP Tools

Two MCP tools recover unpushed agent commits before `cleanup_pipeline` deletes the worktree (see [#2429](https://github.com/jwbron/egg/issues/2429); architecture in [Agent Recovery: Salvaging Unpushed Local Commits](agent-recovery.md#salvaging-unpushed-local-commits)).

| MCP Tool | REST Endpoint | Description |
|----------|---------------|-------------|
| `list_agent_local_commits` | `GET /pipelines/{id}/local-commits` | Read-only enumeration of commits on each per-agent worktree's local `egg/{worktree_id}/work` branch that are not reachable from `origin/<assigned_branch>` (with `origin/<base_branch>` fallback). No fetch, no push. Optional `agent_role` and `slice_id` filters narrow the scope to one worktree |
| `salvage_agent_commits` | `POST /pipelines/{id}/salvage` | Push the worktree's HEAD to `egg/recovered/{pipeline_id}/{scope}/{short_sha}` via launcher auth. Launcher auth bypasses the agent-targeted branch-allowlist check, so this works to recover work even when the agent's own pushes were the thing that wedged. Returns per-worktree results — failure of one worktree never blocks others. The recovery-ref name embeds the HEAD short SHA so re-salvages produce immutable refs instead of force-overwriting earlier ones |

`cleanup_pipeline` runs salvage automatically before deleting any worktree (best-effort; failures are logged and never block cleanup). The MCP tools are for explicit operator-driven recovery — typically before `cancel_task(cleanup=true)` on a pipeline whose pushes are wedged.

After salvage, recover the work with `git ls-remote origin 'refs/heads/egg/recovered/<pipeline-id>/*'`, then `git fetch` + `git cherry-pick`.

## BRC Consensus Protocol

The BRC (Broadcast-Review-Converge) protocol is used during concurrent execution for multi-agent consensus. All protocol actions are gated by formal **action guards** defined in `orchestrator/action_guards.py` — see [Concurrent Execution — Action Guards](../guides/concurrent-execution.md#action-guards) for the complete guard table.

**Consensus commands:**
```bash
# Producer: propose work for review (commit SHA defaults to HEAD if omitted)
# --summary must be ≥50 chars of substantive content (what was built, tested, which tasks satisfied)
# --files-changed, --tests-run, --tasks are optional but recommended for traceability
egg-orch consensus propose --summary "Implemented feature X with JWT validation and session management. All contract tasks satisfied." \
  --artifacts src/feature.py --files-changed src/feature.py --tests-run tests/test_feature.py \
  --tasks task-1-1 task-1-2 --commit-sha $(git rev-parse HEAD)

# Producer: push and propose atomically (required for all pipeline sessions, suppresses auto re-propose)
# Sets consensus_push marker so the gateway allows the push through pipeline-session enforcement
egg-orch consensus propose --push --summary "Implemented feature X with JWT validation and session management. All contract tasks satisfied." \
  --artifacts src/feature.py --files-changed src/feature.py --tests-run tests/test_feature.py \
  --tasks task-1-1 task-1-2 --commit-sha $(git rev-parse HEAD)

# Reviewer: ACK a producer's proposal
# --reason is required and must be ≥50 chars: what was read, what was checked, why the verdict follows
egg-orch consensus ack coder --files-reviewed src/feature.py tests/test_feature.py \
  --reason "Reviewed src/feature.py lines 10-85: token validation handles expiry and invalid signatures. Tests cover all branches."

# Reviewer: conditional ACK — work approved but requires a human action at merge time
# Use when the work is correct but agents cannot perform the required action (e.g. a git mv, secret rotation).
# The obligation surfaces as a "Pre-merge Obligations" section on the auto-created PR — do NOT use
# this to smuggle blocking issues past the producer; NACK if the producer can fix it.
# --pre-merge-condition is validated like --reason: must be specific and non-boilerplate.
egg-orch consensus ack coder --files-reviewed src/feature.py tests/test_feature.py \
  --reason "Reviewed src/feature.py lines 10-85: token validation is correct. One rename is required before merge." \
  --pre-merge-condition "A human must \`git mv legacy/auth.py src/auth.py\` before merging — agents cannot push renames through the gateway"

# Reviewer: re-ACK after the obligation was satisfied within the same PR's diff (#2336)
# Pass --pre-merge-condition-resolved-in-diff <sha> to demote the obligation from the
# merge-blocking section to a "Resolved within this PR" subsection. Requires --pre-merge-condition.
egg-orch consensus ack coder --files-reviewed src/feature.py tests/test_feature.py \
  --reason "Rename completed in commit abc1234; obligation satisfied." \
  --pre-merge-condition "A human must \`git mv legacy/auth.py src/auth.py\` before merging — agents cannot push renames through the gateway" \
  --pre-merge-condition-resolved-in-diff abc1234

# Reviewer: NACK a producer's proposal
egg-orch consensus nack coder --reason "Missing error handling in edge case on line 42 of src/feature.py" --files-reviewed src/feature.py

# Producer: withdraw proposal (requires reason citing new information)
egg-orch consensus withdraw --reason "Addressing NACK: adding retry logic for transient HTTP failures in src/feature.py"

# Agent: confirm after all reviews complete
# Exit 0 = confirmed. Exit 1 = error. Exit 2 = waiting for reviewer re-ACKs (retry after polling).
egg-orch consensus confirmed

# Check overall consensus status
egg-orch consensus status
```

**Exit-2 rejections (#2142):** `consensus propose` (re-propose), `consensus ack`, `consensus nack`, and `consensus confirmed` all return exit 2 with structured rejection details on the orchestrator-side concurrency-control paths. Producers see exit 2 + an `open_nacks_blocked` envelope on a re-propose attempt while ≥2 reviewers have NACKed the current version and the producer hasn't been informed of the full set yet — the response inlines every NACK so the producer can aggregate findings into one re-propose. Reviewers see exit 2 + a `stale_version` envelope when their ACK / NACK targets a superseded proposal — the response inlines the producer's current proposal snapshot so they can re-fetch and re-review without a separate status query. Both rejections are transient: act on the inlined details and retry. See [Concurrent Execution — BRC Protocol Flow](../guides/concurrent-execution.md#brc-protocol-flow) for the underlying race semantics.

**Signal types for consensus:**

| Signal type | Purpose |
|-------------|---------|
| `consensus_propose` | Producer proposes artifacts for review |
| `consensus_ack` | Reviewer ACKs a producer's proposal |
| `consensus_nack` | Reviewer NACKs a producer's proposal |
| `consensus_withdraw` | Producer withdraws proposal (cooldown + flip-flop limits apply) |
| `consensus_confirmed` | Agent confirms consensus (action guards enforced) |
| `consensus_producer_push` | Triggers auto re-proposal when a producer pushes new commits after proposing — invalidates stale ACKs and notifies reviewers |

The `consensus_producer_push` signal accepts `agent_role`, `commit_sha`, and optional `changed_files` parameters. When the producer is still in `WORKING` state, the signal is a no-op. See [Auto Re-Propose on Push/Commit](../guides/concurrent-execution.md#auto-re-propose-on-pushcommit).

## Context PR Surfaces ([#2548](https://github.com/jwbron/egg/issues/2548))

Slice-aware pipelines (issue-mode pipelines with `contract.slices`) open a **Context PR** before any slice spawns — see [Orchestrator Architecture: Context PR (slice-aware mode)](../architecture/orchestrator.md#context-pr-slice-aware-mode-2548) and the [Concurrent Execution Slice PR Stack](../guides/concurrent-execution.md#slice-pr-stack) section for the full mechanics. The Context PR is orchestrator-authored; `egg-orch` does **not** ship dedicated `--context-branch` / `--context-pr` flags. Operators inspect the Context PR through the same surfaces used for any other contract metadata:

```bash
# Inspect the contract's pr.context_* fields. The top-level --pipeline-id flag
# goes BEFORE the subcommand; bare `egg-contract show` works when EGG_PIPELINE_ID
# is exported.
egg-contract --pipeline-id <pipeline-id> show
# Look for: pr.context_title, pr.context_description, pr.context_branch, pr.context_pr_number

# Pipeline status (current_phase / pending_decisions; does not include context-PR-specific fields)
egg-orch pipeline status <pipeline-id>

# Locate the open Context PR on GitHub once contract.pr.context_pr_number is set
gh pr view <context_pr_number>
gh pr list --head egg/<id>/context
```

The four `pr.context_*` fields are added in contract schema 1.1 (#2548 — pre-1.1 contracts auto-promote on load). The planner authors `context_title` / `context_description` during the plan phase (before plan_gate); the orchestrator populates `context_branch` and `context_pr_number` after plan_gate approval, when it creates the branch and opens the PR:

| Field | Author | Description |
|-------|--------|-------------|
| `pr.context_title` | Planner | Title for the Context PR (program-level framing). |
| `pr.context_description` | Planner | Body for the Context PR (program-level narrative). |
| `pr.context_branch` | Orchestrator | Branch name (`egg/<id>/context`) — populated when the orchestrator creates the branch. |
| `pr.context_pr_number` | Orchestrator | GitHub PR number — populated when the PR is opened. |

The orchestrator manages the Context PR end-to-end (create branch, commit refine/plan artifacts + BRC history + agent transcripts, open PR via `GatewayClient.create_pr()` with `context_title` / `context_description`). There are no `egg-orch` verbs for opening or closing it manually.

**Observability:** If the context PR hook runs but does not open a PR, the wrapper at `_maybe_open_base_pr_for_plan_to_implement` surfaces a `context_pr.failed` event (the inner hook raised) or `context_pr.skipped` event (the inner hook returned without raising, but the post-hook contract still records `contract.pr.context_pr_number = null`) on three sinks (#2611): the in-process `MessageStore` (so the event shows up as a `CONTEXT_PR_FAILED` / `CONTEXT_PR_SKIPPED` entry in `recent_messages` and `/pipelines/<id>/messages`), the `EventBus` (so `/status/wait` long-pollers wake — both event types are in `_STATUS_WAIT_EVENT_TYPES`, and the message types are in `_STATUS_WAIT_MESSAGE_TYPES`), and the legacy `StatusReporter` handler chain (no production handler is registered today, but the call is preserved for any future console/file handler). The emit branch is gated on `pipeline.repo` and `pipeline.base_branch` both being truthy; local-mode pipelines (no remote, no base branch) skip the hook silently and produce neither event (#2593).

The wrapper also writes structured log lines on every invocation: `Context PR hook entered (#2548)` on entry, followed by either a short-circuit log line from the inner hook (e.g. `Context PR hook: pipeline has no remote repo, skipping`) or the wrapper's `Context PR hook raised at plan→implement transition (continuing) (#2548)` warning when the inner hook raised. (CUSTOM-mode pipelines short-circuit in the wrapper before the inner hook runs and instead emit `Context PR hook skipped (CUSTOM mode) (#2548)`; this only matters for log forensics on stuck pipelines, since CUSTOM-mode pipelines are single-phase (#1762) and never traverse plan→implement in normal operation.) The `context_pr.skipped` case corresponds to an inner contract-side short-circuit that did not raise — for example, no `pr` block on the contract, the post-hook contract reload failed, or a late `save_contract` failure swallowed the success after the gateway already opened the PR on GitHub. A missing `WORKTREE_BASE_DIR` volume mount is another `context_pr.skipped` trigger: the hook falls back to `/tmp` (gateway-rejected path), logs `Context PR hook: WORKTREE_BASE_DIR missing — falling back to system temp (likely a broken volume mount in production…) (#2684)` at WARNING level, and the gateway silently rejects the push (#2684).

In any of these cases, check `contract.pr.context_pr_number` (via `egg-contract show`) and the remote PR list before concluding the PR is missing — that last short-circuit can leave a PR open on GitHub while the contract still records `null`. **Pipeline deletion does not clean up Context PRs:** `egg-orch pipeline delete <id>` only removes the pipeline tip branch (`egg/<id>/work`) and per-container worktree branches; the Context PR branch (`egg/<id>/context`) is a sibling of the pipeline tip — same convention as the slice integration branches `egg/<id>/slice-N` — and is **not** deleted (see `_cleanup_remote_branches` in `orchestrator/routes/pipelines.py`). To remove a Context PR opened by an unwanted run the operator must close the PR and delete the branch manually:

```bash
gh pr close <context_pr_number>
git push origin --delete egg/<pipeline-id>/context  # (gateway-mediated push)
```

Per-slice implement-phase BRC history files written by the orchestrator (`.egg-state/brc-history/<id>-implement-slice-<N>.{md,json}` plus `<id>-implement-unattributed.{md,json}`) are visible in each slice PR's diff; the aggregate `<id>-implement.{md,json}` file is **not** produced in slice-aware mode. CUSTOM+PR and other non-slice runs continue to emit the single content-addressed file (`pr-<N>-<short-sha>-implement.{md,json}` for CUSTOM+PR). See [Orchestrator Architecture: BRC-history file naming](../architecture/orchestrator.md#brc-history-file-naming) for the full file-pattern table. If the BRC history file is absent from a slice PR's diff, check orchestrator logs for `Per-slice BRC commit: WORKTREE_BASE_DIR missing — falling back to system temp…` — this WARNING indicates the `/home/egg/.egg-worktrees` volume is not mounted on the orchestrator pod, causing the gateway to reject the push (#2684).

## Related CLIs

- `egg-contract` — SDLC contract operations (tasks, decisions, feedback)
- `egg-pipeline-watch` — Live pipeline status polling
- `egg-checkpoint` — Browse agent checkpoints
