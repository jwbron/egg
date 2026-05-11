# Jira Hierarchy Config Reference

`~/.config/egg/jira-hierarchy.yaml` maps each Jira project key to the
field the orchestrator uses to attach a child ticket to its parent
epic. Required for the [epic SDLC pipeline](../guides/sdlc-epic-pipeline.md):
the apply step refuses to create children under projects that aren't in
the YAML (decision-2 from [#1557](https://github.com/jwbron/egg/issues/1557)).

> See also
> [SDLC Epic Pipeline Guide](../guides/sdlc-epic-pipeline.md),
> [Jira Wrapper Reference](jira-wrapper.md).

## Why this file exists

Jira projects fall into two shapes that disagree on epic linkage:

| Shape | Hierarchy field | Where you'll see it |
|-------|-----------------|---------------------|
| Company-managed (Classic) | `"Epic Link"` custom field | Older Jira projects; child tickets carry an `Epic Link` field pointing at the epic. |
| Team-managed (Next-gen) | Standard `parent` field | Newer Jira projects; child tickets carry a standard `parent` field pointing at the epic. |

There is **no reliable way** to detect this per-project at runtime from
the issue payload alone — team-managed projects often don't expose
`"Epic Link"` at all (a search clause against `"Epic Link"` returns HTTP
400 on those projects). Rather than guess, the operator declares the
choice once per project.

## Location

The loader (`orchestrator/jira_hierarchy_config.py`) reads
`~/.config/egg/jira-hierarchy.yaml` from the orchestrator's home
directory and caches the parsed config by mtime (mirroring the
credentials loader pattern at `gateway/jira_credentials.py`). Edits to
the YAML are picked up on the next call without a process restart.

## Schema

```yaml
projects:
  <PROJECT_KEY>: parent | epic_link
  ...
```

- **Top-level key**: `projects` — a mapping of Jira project key
  (uppercase, matches `^[A-Z][A-Z0-9_]*$`) to the hierarchy field
  choice.
- **Value**: one of two literals — `parent` (team-managed) or
  `epic_link` (company-managed).

Values outside `{parent, epic_link}` are rejected by the Pydantic
validator at load time.

## Worked example

```yaml
# ~/.config/egg/jira-hierarchy.yaml
projects:
  # Engineering — team-managed (Next-gen)
  ENG: parent

  # Knowledge platform — team-managed
  KORE: parent

  # Security — company-managed (Classic)
  SEC: epic_link

  # Customer success — company-managed
  CS: epic_link
```

When the plan apply step creates a child under epic `ENG-100`, the
loader returns `parent`; the `jira_ticket_create` call sets
`fields.parent = {"key": "ENG-100"}`. For an epic under `SEC-50`, the
loader returns `epic_link`; the call sets the project's `Epic Link`
custom field instead.

## Unmapped projects

If an epic's project is not in the YAML, the apply step **refuses**
the create (decision-2). The error surfaces in the operator HITL as a
`JiraHierarchyUnmappedError` with the project key and the path to the
config file. The pipeline halts mid-apply rather than silently shipping
children under the wrong field.

Recovery:

1. Add the missing project to `~/.config/egg/jira-hierarchy.yaml`.
2. Re-run the plan apply step. The orchestrator's per-call idempotency
   (`epic_apply.applied_edits[]`) means already-applied non-failing
   entries are not re-attempted; only the failed creates retry.

## How to tell which field a project uses

1. Open a sample child ticket of any epic in the project.
2. In the right-hand panel, look for either:
   - **`Parent`** — team-managed; use `parent`.
   - **`Epic Link`** — company-managed; use `epic_link`.

If both appear, the project is company-managed with the Next-gen
display in effect — use `epic_link`. (The reverse — team-managed
projects exposing `Epic Link` — does not occur.)

A faster programmatic check is to issue
`POST /api/v1/jira/search` with `jql = "Epic Link" = "<KEY>"` against a
known epic key. A HTTP 400 with `"Field 'Epic Link' does not exist or
you do not have permission to view it"` proves the project is
team-managed (`parent`); a 200 with results proves it's
company-managed (`epic_link`).

## Validation

The loader applies Pydantic validation at load:

- **Top-level shape**: `{projects: {...}}`. Missing `projects` → load
  error.
- **Project key shape**: must match `^[A-Z][A-Z0-9_]*$`. Lowercase or
  mixed-case keys are rejected.
- **Value enum**: only `parent` or `epic_link`. Other strings (including
  `Parent`, `EpicLink`, `epicLink`) are rejected.

Errors surface as `JiraHierarchyConfigError` with the offending field
path so operators can fix the YAML quickly.

## Reload semantics

The loader caches the parsed config keyed by file mtime. On the next
`resolve_hierarchy_field()` call after the YAML changes, the loader
detects the mtime bump and reloads — no process restart needed. This
mirrors the credentials loader pattern at
`gateway/jira_credentials.py`.

## See also

- [SDLC Epic Pipeline Guide](../guides/sdlc-epic-pipeline.md) —
  end-to-end flow, including where this config plugs in.
- [Jira Wrapper Reference](jira-wrapper.md) — the gateway routes the
  apply step calls (`ticket/create`, `ticket/edit`,
  `ticket/comment/add`, `issue-link/create`).
- [Submit-Task MCP Reference](submit-task-mcp.md) — the `mode`
  parameter that triggers the epic flow.
