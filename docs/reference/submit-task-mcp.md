# Submit-Task MCP Reference

`submit_task` is the MCP tool that creates a new SDLC pipeline. It is
the canonical surface for hosts (Claude Code sessions, ops scripts) to
kick off `refine` → `plan` → `implement` → `pr` work — the legacy
interactive CLI (`bin/egg`) was removed in
[#1762](https://github.com/jwbron/egg/issues/1762), so all pipeline
creation flows through this tool (or the underlying REST endpoint
`POST /api/v1/pipelines`).

> See also
> [SDLC Pipeline Guide](../guides/sdlc-pipeline.md),
> [SDLC Epic Pipeline Guide](../guides/sdlc-epic-pipeline.md),
> [Custom-Phase Guide](../guides/custom-phase.md),
> [Babysit-PR Guide](../guides/babysit-pr.md).

## What it does

`submit_task` creates a new ISSUE-mode pipeline:

1. Validates the input (issue number / Jira key / qualifier).
2. Translates inputs into a `pipeline_id` and `branch` (e.g.
   `issue-123` → `egg/issue-123`; `KORE-1234` → `egg/KORE-1234`).
3. POSTs to `/api/v1/pipelines` to start the pipeline.
4. Returns the orchestrator's structured response with the pipeline
   ID, branch, and initial phase state.

The pipeline then runs autonomously (with HITL approval gates at
refine and plan). Status can be polled via the SDK MCP tools (e.g.
`mcp__progress__query_status`) or the underlying REST API.

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `issue_number` | int | — | GitHub issue number (mutually exclusive with `jira_ticket`). |
| `jira_ticket` | str | — | Jira key matching `^[A-Z][A-Z0-9_]*-\d+$` — either a ticket or an **epic** key. |
| `repo` | str | (caller's repo) | `owner/repo` slug; defaults to the host's current repo. |
| `description` | str | — | Free-form problem statement passed to the refine phase. |
| `qualifier` | str | — | Optional pipeline qualifier — appended to `pipeline_id` and `branch` (e.g. `qualifier="backend"` produces `issue-123-backend`). |
| `mode` | enum | `auto` | **Epic-flow only** ([#1557](https://github.com/jwbron/egg/issues/1557)). One of `auto`, `reassess`, `fresh`. See [`mode`](#mode-parameter-epic-flow). |
| `analysis` | str | — | Pre-generated refine analysis content (skips refine phase when paired with `start_phase: implement`). |
| `plan` | str | — | Pre-generated plan content (skips plan phase). |
| `source_branch` | str | — | Path to a prior run's branch; the orchestrator reads `.egg-state/drafts/*.md` and `.egg-state/contracts/*.json` server-side instead of requiring inline `analysis` / `plan`. |
| `source_artifact_prefix` | str | — | Explicit override for the prefix used to look up artifacts on `source_branch`. |
| `config` | dict | — | Free-form pipeline config (e.g. `{"start_phase": "implement", "hitl_gates": false}`). |

`issue_number` and `jira_ticket` are mutually exclusive — pass exactly
one. Unknown parameter values are rejected with HTTP 400 (mirroring the
existing `qualifier` regex check).

## `mode` parameter (epic flow)

Added in [#1557](https://github.com/jwbron/egg/issues/1557) so operators
can drive Jira **epics** through the same `submit_task` pipeline as
single tickets. The parameter only has an effect when `jira_ticket`
resolves to an **Epic** (the orchestrator dispatches
`detect_jira_issuetype` against the gateway up front; the issuetype is
read from `fields.issuetype.name`). For ticket keys (non-Epic), `mode`
is a no-op — today's single-ticket flow is unchanged.

### Accepted values

| Value | Effect on an Epic key |
|-------|-----------------------|
| `auto` (default) | Auto-detect: `reassess` if the epic has any non-Done children; `fresh` if it has none. |
| `reassess` | Force reassess. Degrades to `fresh` with a logged warning when no children exist. |
| `fresh` | Force fresh. Logs a warning when children exist (operator asked for it explicitly). |

When the value is anything else, the handler returns HTTP 400 — parity
with the existing `qualifier` regex rejection.

### What `auto` decides on

Reassess auto-detection issues **two independent JQL queries**
(`parent = "<KEY>"` and `"Epic Link" = "<KEY>"`) and merges results by
`key`. A single-OR disjunctive fails with HTTP 400 on team-managed
projects that lack the `"Epic Link"` field; the two-query approach
tolerates per-query 400s by logging
`jira_epic_search_field_missing` and treating that query's result set
as empty. Merged result set non-empty → `reassess`; empty → `fresh`.

When `~/.config/egg/jira-hierarchy.yaml` maps the project to `parent`
(team-managed), query B (`"Epic Link" = "<KEY>"`) is skipped as a
tautology.

### Worked examples

```python
# 1. Fresh epic, no children yet — auto resolves to fresh
submit_task(
    jira_ticket="KORE-100",
    description="Roll out feature X across services A and B",
)

# 2. Same epic later, after children have been created — auto detects
# reassess based on the existing children
submit_task(
    jira_ticket="KORE-100",
    description="Reassess after H1 deliverable shift",
)

# 3. Force fresh on an epic with children (operator wants to start over)
submit_task(
    jira_ticket="KORE-100",
    description="Reboot the epic — H2 scope is unrelated to the prior plan",
    mode="fresh",
)

# 4. Force reassess (no-op on a childless epic — logs and degrades)
submit_task(
    jira_ticket="KORE-100",
    description="Pull in the new compliance requirements",
    mode="reassess",
)
```

## Latency expectations

The epic flow adds a synchronous detection probe to the critical path
of `submit_task`. On a healthy gateway, total `submit_task` latency
remains around 2 s for an epic key (the same shape as today's single
ticket: handler validation + pipeline creation POST + the new
`jira_ticket_get` probe). The orchestrator emits a `STATUS` message when
total `submit_task` latency exceeds 5 s so operators can tune the
gateway / Atlassian round-trip if it drifts (architect oq-6 /
risk_analyst R15 from the #1557 refine analysis).

For non-Epic Jira keys the detection result is `Task` (or whatever the
issuetype reports) and the handler falls through to the existing
ticket-keyed flow — no extra cost beyond the one `jira_ticket_get` call.

For `issue_number` pipelines (GitHub issues, no Jira key), the
detection probe is skipped entirely.

## Pipeline ID + branch format

`submit_task` derives `pipeline_id` and `branch` from the input:

| Input | `pipeline_id` | `branch` |
|-------|---------------|----------|
| `issue_number=123` | `issue-123` | `egg/issue-123` |
| `issue_number=123, qualifier="backend"` | `issue-123-backend` | `egg/issue-123-backend` |
| `jira_ticket="KORE-1234"` (Task) | `KORE-1234` | `egg/KORE-1234` |
| `jira_ticket="KORE-100"` (Epic, fresh) | `KORE-100` | `egg/KORE-100` |
| `jira_ticket="KORE-100"` (Epic, reassess) | `KORE-100` | `egg/KORE-100` |

The epic flow does **not** introduce a new identifier shape — epic
pipelines re-use the Jira-key naming convention. The reassess /
fresh distinction lives on the pipeline record as
`Pipeline.jira_effective_mode` (`reassess` | `fresh` | `None`) so
downstream phases read the resolved mode without re-running detection.

## Related fields populated on `Pipeline`

| Field | Set when | Used by |
|-------|----------|---------|
| `Pipeline.jira_ticket` | `jira_ticket` is a non-Epic | Existing single-ticket flow. |
| `Pipeline.jira_epic_key` | `jira_ticket` is an Epic | Epic flow branches in refine / plan prompts; `apply_epic` agent spawn predicate. |
| `Pipeline.jira_effective_mode` | Epic flow, after auto-detection | Refine / plan prompt branches; reassess-specific instructions. |
| `Pipeline.jira_parent_epic_key` | Child pipeline created by plan-gate Continue-to-implement fan-out | PR-link writeback (feedback Q4 from #1557). |

## Errors

| Code | Cause |
|------|-------|
| HTTP 400 — `mode` value invalid | `mode` is not one of `{auto, reassess, fresh}`. |
| HTTP 400 — both `issue_number` and `jira_ticket` set | Mutually exclusive. |
| HTTP 400 — Jira key shape invalid | `jira_ticket` does not match `^[A-Z][A-Z0-9_]*-\d+$`. |
| HTTP 409 — pipeline already active | A pipeline with the same `pipeline_id` is already running. Pass `qualifier` to disambiguate. |

Gateway / Atlassian errors during the detection probe surface as the
upstream status code with a structured envelope; see the
[Jira Wrapper Reference](jira-wrapper.md) for the gateway error
contract.

## Legacy CLI removal

The legacy interactive-mode CLI (`bin/egg`) that previously offered the
`submit_task` equivalent on the host was removed in
[#1762](https://github.com/jwbron/egg/issues/1762). All pipeline
creation now flows through this MCP tool or the underlying REST endpoint
`POST /api/v1/pipelines`. The new `mode` parameter is therefore an
MCP-only addition — there is no `--mode` CLI flag.

## See also

- [SDLC Pipeline Guide](../guides/sdlc-pipeline.md) — operational guide
  to the full pipeline.
- [SDLC Epic Pipeline Guide](../guides/sdlc-epic-pipeline.md) —
  end-to-end epic flow, where `mode` is the trigger.
- [Jira Hierarchy Config](jira-hierarchy-config.md) — per-project
  `parent` / `epic_link` map required for the epic flow.
- [Custom-Phase Guide](../guides/custom-phase.md) — the sibling
  `run_agent_task` MCP tool for single-phase work.
- [Babysit-PR Guide](../guides/babysit-pr.md) — the sibling
  `babysit_pr` MCP tool for one-off PR review.
