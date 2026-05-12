# Analysis: Add SDLC pipeline support for Jira epics

> Issue: #1557 | Phase: refine

## Problem Statement

Egg's existing SDLC pipeline (`submit_task`) treats one Jira **ticket** as the unit of work: refine analyzes a single ticket, plan produces a single plan document, implement produces one PR per ticket. Jira **epics** — the natural container for multi-ticket work — have no first-class path. An operator wanting to use egg to refine an epic-shaped scope, decompose it into child tickets, and then drive implementation of those children today must drive every step by hand: manually copy the refined analysis into the epic Description, manually create each child ticket, manually wire up Epic Link / parent relationships, manually set up Blocks / Is-blocked-by edges, and manually kick off `submit_task <CHILD-KEY>` for each one.

The desired outcome is to treat an epic as the SDLC unit of work using the **same `submit_task` pipeline** that exists today — same refine/plan phases, same agent roster, same draft-based HITL surface (`.egg-state/drafts/<id>-*.md`), same contract store. Only the **IO sinks** change:

1. **Refine output** → written to the **epic's Description field** on approval (via gateway `editJiraIssue`).
2. **Plan output** → **decomposed into child Jira tickets** under the epic on approval (via gateway `createJiraIssue` + `createIssueLink`), with each plan node delivered as a fully-formed Jira ticket body (problem statement, scope, acceptance criteria, out-of-scope notes, cross-links).

The pipeline must support two paths:

- **Fresh-epic**: epic has no children. Plan creates new children from scratch.
- **Reassess**: epic already has children. Plan **must plan every pre-existing child as a node** — same full-ticket-description treatment as net-new nodes — and may consolidate (N → 1), split (1 → N), update in place, flag obsolete tickets for Won't Do, or leave Done children alone. The plan draft surfaces a diff (updated / closed / untouched / net-new / consolidated / split / in-flight) and records the existing-key → plan-node mapping so the apply step produces the right edit/create/Won't-Done set.

The implement phase is **out of scope** beyond definition: after plan apply, `submit_task <CHILD-KEY>` on any created child works the same as today's Jira-ticket pipeline (each child = its own independent implement pipeline, producing its own PR — possibly a stack of PRs along the slice DAG now that [#2137](https://github.com/jwbron/egg/issues/2137) has landed).

## Current Behavior

### `submit_task` Jira input handling

[`orchestrator/mcp_tools.py:1272–1381`](../../orchestrator/mcp_tools.py) (handler body) accepts an optional `jira_ticket` parameter alongside `issue_number`. Validation is a regex (`^[A-Za-z][A-Za-z0-9]+-[0-9]+$`) at `:1287–1300`, the key is uppercased, and pipeline-ID derivation runs through `:1301–1307` as `<TICKET>` (or `<TICKET>-<qualifier>` if disambiguation is needed). The key is then used as:

- Pipeline ID (matches the `<EPIC-KEY>[-<qualifier>]` convention the issue describes — **already there**).
- Branch name (`egg/{pipeline_id}`).
- `EGG_JIRA_TICKET` env var exported into the sandbox ([`orchestrator/routes/pipelines.py:18993–18998`](../../orchestrator/routes/pipelines.py)) so agents can call the gateway with it.

Crucially, **`submit_task` does not fetch ticket content at pipeline creation time** — the key is "advisory" labelling only. No issue-type detection (`fields.issuetype.name`) happens anywhere in the orchestrator today. The agents are expected to call `jira ticket get` themselves via the gateway during refine, if they need the content.

### Refine and plan phase prompts

Refine and plan phase prompts are built inline in `_run_refine` / `_run_plan` in [`orchestrator/routes/pipelines.py`](../../orchestrator/routes/pipelines.py). There is **no Jira-specific prompt template** today. The `description` parameter to `submit_task` is passed through unchanged; ticket content injection is the agent's responsibility. Plan output goes into `.egg-state/drafts/<id>-plan.md` with a YAML-tasks appendix parsed by `egg_contracts/plan_parser.py`; the plan operates on the assumption that the unit of work is a single ticket and is delivered as a single PR.

`_read_phase_draft` at [`pipelines.py:5512–5560`](../../orchestrator/routes/pipelines.py) reads pre-existing drafts from `.egg-state/drafts/{identifier}-{phase}.md`, with sensible fallbacks (issue-number variant, generic, `git show`). This will not need invasive changes for the epic case — only the *contents* of those drafts shift.

### Jira gateway state (issues #1556 + #1924, both closed)

Reads ([`gateway/jira_client.py:399–422`](../../gateway/jira_client.py) and surrounding):

- `get_ticket(key, fields, expand)` — fetches a ticket; the caller can request `fields=["status", "issuetype", "parent", ...]`. `fields.issuetype.name` and `fields.status.name` are accessible from the raw JSON response — **no client-side filtering** needs to be added for epic-type detection or in-flight status checks; the caller just has to know to inspect those fields.
- `search(jql, fields, next_page_token, max_results)` — JQL search with cursor pagination. Used to discover existing children in the reassess path — but see Constraints below: a *single* disjunctive JQL of the form `parent = <EPIC> OR "Epic Link" = <EPIC>` will return HTTP 400 on a project where `Epic Link` is not configured, so child-discovery must issue **two separate queries** (one per hierarchy field, using the configured `JiraPolicy.epic_link_field` value rather than hardcoded `"Epic Link"`) and merge the results.
- `get_comments(key)` — comment retrieval.

Writes ([`gateway/jira_client.py`](../../gateway/jira_client.py), shipped in #1924):

- `create_issue(project_key, issuetype, summary, description, labels, parent, epic_link, epic_link_field=...)` — `parent` and `epic_link` are mutually exclusive ([`jira_client.py:557–569`](../../gateway/jira_client.py)). The `epic_link_field` parameter at `:505–527` defaults to `"parent"` and is sourced from [`JiraPolicy.epic_link_field`](../../gateway/jira_policy.py) (`:98–193`) — a **per-site config flag** in `JIRA_POLICY_YAML`, allowlisted in `_VALID_EPIC_LINK_FIELDS`. **Important**: this is operator-configured per Atlassian site, not auto-detected per project. There is **no `get_project_metadata` verb** in the gateway today; auto-detection in the sense of decision-2 opt-1 would require a new gateway verb.
- `edit_issue(key, summary, description, labels, idempotency_key)` — for both the refine sink (write epic Description) and the reassess update-in-place path.
- `add_comment(key, body, idempotency_key)` — for redirect comments on consolidated / split tickets.
- `create_issue_link(link_type, inward_key, outward_key, comment, idempotency_key)` — for cross-task DAG edges (Blocks / Is blocked by). Note: link types are project-scoped, so the chosen `link_type` may not exist in every project. Apply step needs a fail-soft path.

Hard denylist (permanent, [`gateway/jira_client.py:133–146`](../../gateway/jira_client.py) — `JIRA_WRITE_VERBS_DENIED`): `transitions`, `worklog`, `attachments`, `watchers`. **`transitions` is permanently off-limits to agents.** This is the architectural reason the issue's reassess apply step says "**Orchestrator** directly transitions flagged-obsolete children to Won't Do using its own Jira credentials" — there is no gateway path, and there is not going to be one.

### Confluence gateway state (issue #1931, closed)

[`gateway/confluence_client.py`](../../gateway/confluence_client.py) — production-ready, read-only: `get_page`, `get_page_descendants`, `get_page_inline_comments`, `get_page_footer_comments`, `list_spaces`, `search_cql`, plus a GET-only `execute_raw` passthrough. **No changes needed** for the linked-Confluence-pages refine input — the agent just calls the gateway.

### Independent implement phases (issue #2137, closed)

[#2137](https://github.com/jwbron/egg/issues/2137) landed the stacked-slice-PR delivery model. A single child ticket's implement can therefore produce a stack of PRs along its slice DAG. The MVP for #1557 works under either model (monolithic-implement child = one PR; sliced-implement child = stack), so this is no longer a soft dependency in practice — it is settled infrastructure.

### Orchestrator credentials & out-of-band writes

The orchestrator today maintains the **zero-credential invariant** — all third-party credentials live in the gateway process and are never reachable from the orchestrator. The orchestrator forwards `jira_ticket` as an advisory session label so audit trails record it, but it does **not** hold Jira credentials. There is **no out-of-band write path** to Jira at all today.

The Won't-Do transitions in the reassess apply step require either:

1. A new orchestrator-side Jira credential store (env vars / config file) plus a thin transition helper that calls `POST /rest/api/3/issue/{key}/transitions` directly. This breaks the "all third-party creds live in the gateway" symmetry — but only for a verb-set the gateway has permanently denylisted, so it's a real-not-symbolic line.
2. Lifting the denylist for `transitions` in the gateway. **Off the table** per #1556's resolved scope decisions.

Option 1 is the issue's stated direction. This is a non-trivial new credential surface and the operator-facing setup story is registered as feedback Q1.

### Pipeline state & open-PR artifacts

The orchestrator already tracks open PRs per pipeline in [`.egg-state/pipelines/<id>.json`](../../.egg-state/pipelines/) under `phases.pr.artifacts.pr_url`, populated by `_auto_create_pr` ([`pipelines.py:8277`](../../orchestrator/routes/pipelines.py)). `_get_pr_info` ([`pipelines.py:4142–4160`](../../orchestrator/routes/pipelines.py)) parses the PR number from the URL. This is the canonical source for "does this child have an open PR?" in the in-flight detection logic: indexable by `pipeline_id == <CHILD-KEY>`.

### `/impact-analysis` skill

The skill is host-side and referenced in [`.egg-state/drafts/1931-analysis.md`](1931-analysis.md) as having demonstrated the `parent = <KEY> OR "Epic Link" = <KEY>` JQL pattern via `mcp__confluence__*`. The query shape is documented in the issue itself; no live sandboxed implementation exists, and #1557 should re-implement the JQL through the now-landed Jira gateway rather than try to call into the host-side skill.

## Runtime Primitives Surfaced for the Plan Phase

Per [#2594](https://github.com/jwbron/egg/issues/2594), the plan phase will perform Primitive-Existence and Trust-Boundary audits against the assumptions below. Each entry names the primitive, the file:line evidence, and the execution-context scope so plan-time review is cheap.

| Primitive | File:line | Execution context | Notes |
|---|---|---|---|
| `submit_task(jira_ticket=...)` MCP tool | [`orchestrator/mcp_tools.py:66–127, 1272–1381`](../../orchestrator/mcp_tools.py) | trusted-CI-runner (orchestrator) | Exists; needs new param branch for epics + issue-type detection. |
| `Pipeline.jira_ticket` field | [`orchestrator/models.py:981–1004`](../../orchestrator/models.py) | trusted-CI-runner | Exists; reused as-is for epic key. |
| `EGG_JIRA_TICKET` sandbox env var | [`orchestrator/routes/pipelines.py:18993–18998`](../../orchestrator/routes/pipelines.py) | in-sandbox-agent | Exists; reused. |
| Gateway `get_ticket(key, fields, expand)` | [`gateway/jira_client.py:399–422`](../../gateway/jira_client.py) | in-sandbox-agent (HTTP) | Exists; caller reads `fields.issuetype.name`, `fields.status.name`, `fields.description` from raw JSON. |
| Gateway `search(jql, fields, ...)` | [`gateway/jira_client.py`](../../gateway/jira_client.py) | in-sandbox-agent (HTTP) | Exists; reassess issues **two separate JQL queries** (`parent = <EPIC>` and `<epic_link_field> = <EPIC>`) and merges — single disjunctive query fails with HTTP 400 on projects missing one field. |
| Gateway `edit_issue(key, summary, description, ...)` | [`gateway/jira_client.py`](../../gateway/jira_client.py) | in-sandbox-agent (HTTP) — **but apply step likely runs orchestrator-side** | Exists; sink for both refine (epic Description) and reassess (child update). See trust-boundary note below. |
| Gateway `create_issue(project_key, issuetype, summary, description, parent, epic_link, epic_link_field, ...)` | [`gateway/jira_client.py:505–569`](../../gateway/jira_client.py) | in-sandbox-agent (HTTP) — **see note** | Exists; mutually-exclusive `parent` vs `epic_link` choice driven by decision-2; `epic_link_field` itself sourced from `JiraPolicy.epic_link_field` per-site config. |
| `JiraPolicy.epic_link_field` (per-site config) | [`gateway/jira_policy.py:98–193`](../../gateway/jira_policy.py) | gateway policy | Exists; operator-configured allowlisted field name (default `"parent"`). |
| Gateway `add_comment(key, body, ...)` | [`gateway/jira_client.py`](../../gateway/jira_client.py) | in-sandbox-agent (HTTP) | Exists; used for redirect comments on consolidate / split. |
| Gateway `create_issue_link(link_type, inward, outward, ...)` | [`gateway/jira_client.py`](../../gateway/jira_client.py) | in-sandbox-agent (HTTP) | Exists; for Blocks edges (decision-driven link-type per feedback Q3). |
| `JIRA_WRITE_VERBS_DENIED` denylist | [`gateway/jira_client.py:133–146`](../../gateway/jira_client.py) | gateway policy | **Hard primitive**: `transitions` not addable. Forces orchestrator-direct path for Won't Do. |
| `_run_refine` / `_run_plan` prompt builders | [`orchestrator/routes/pipelines.py`](../../orchestrator/routes/pipelines.py) (`_run_refine`, `_run_plan`) | trusted-CI-runner (orchestrator) | Exists; needs new epic-shaped prompt branches. |
| `_read_phase_draft` | [`orchestrator/routes/pipelines.py:5512–5560`](../../orchestrator/routes/pipelines.py) | trusted-CI-runner | Exists; reusable for `<EPIC-KEY>-analysis.md` / `<EPIC-KEY>-plan.md`. |
| Pipeline PR artifact lookup | [`orchestrator/routes/pipelines.py:4142–4160, 8277`](../../orchestrator/routes/pipelines.py) | trusted-CI-runner | Exists; canonical source for "child has open PR" in in-flight detection. |
| Confluence gateway client | [`gateway/confluence_client.py`](../../gateway/confluence_client.py) | in-sandbox-agent (HTTP) | Exists; refine uses `get_page` + `search_cql` for linked pages. |
| **(new)** Epic-input branch in `submit_task` | _to be added_ | trusted-CI-runner | Detect `issuetype.name == "Epic"` after first `get_ticket` call. |
| **(new)** Reassess child-discovery + classification | _to be added_ | trusted-CI-runner OR in-sandbox-agent | Reads via gateway; classifies into `to_do` / `done` / `in_flight`. Trust-boundary: see below. |
| **(new)** Plan apply step (epic case) | _to be added_ | trusted-CI-runner | Calls gateway `create_issue` / `edit_issue` / `create_issue_link`. |
| **(new)** Orchestrator-direct Jira creds + transition helper | _to be added_ | trusted-CI-runner only (separate cred store) | For Won't-Do transitions. Operator setup story is feedback Q1. |
| **(new)** Plan-node → Jira-key mapping store | _to be added_ | trusted-CI-runner | Persistence location is decision-10. |
| **(new)** In-flight HITL gate | _to be added_ | trusted-CI-runner | Per-ticket gate for mutations on `in_flight` children; signals per decision-8. |

**Trust-boundary note**: there are two natural homes for the **non-transition** apply step writes (write epic Description, create children, link, comment):

1. **In-sandbox-agent** via the gateway, executed by a dedicated `apply-epic` agent at the end of refine and end of plan. This matches today's "agents write through the gateway" pattern, preserves the gateway audit identity (session token), and keeps the orchestrator out of the data path for tickets/descriptions.
2. **Trusted-CI-runner (orchestrator)** calling the gateway directly. Avoids spinning up an extra agent for what is largely deterministic; but means the orchestrator needs a gateway client and an HTTP session token, which raises questions about which session and which audit identity the writes get attributed to.

The Won't-Do transition is unambiguously **trusted-CI-runner only** (because it has no gateway path). The remaining writes are registered as **[decision-11]** — orthogonal to the slice-decomposition (decision-1) choice. Plan phase cannot author the apply step without decision-11 resolved.

## Constraints

**Architectural / security:**

- Zero-credential invariant for the sandbox container persists. Agents must continue to reach Jira only through the gateway. The `transitions` denylist stays.
- The new orchestrator-direct Jira credential surface is **only** for transitions — it must not be reused for ticket create / edit / comment / link, which all have gateway-mediated paths. This keeps audit, project-allowlist, and rate-limit policy applied consistently to the bulk of writes.
- The plan apply step (regardless of where it runs per decision-11) must be **idempotent**: re-running plan apply on the same plan draft must not duplicate child tickets, double-link, or re-fire Won't-Do transitions. The plan-node → key mapping (decision-10) is the durable record.
- **Resume-from-partial**: the apply step must resume from the mapping store on re-run. Any plan node whose `existing_key` (reassess) or `created_key` (fresh) is already present in the mapping skips re-creation/re-edit; any cross-task link whose both endpoints exist in the mapping is re-issued via `create_issue_link`'s `idempotency_key` (which the gateway already supports). Idempotence ≠ resume — both must be designed for explicitly because a partial-apply (5/13 created) is a probable failure mode given Jira rate limiting + multi-call apply.
- **Two-pass execution**: cross-task `create_issue_link` calls must follow *all* `create_issue` calls — both endpoints must exist before linking. The apply step is therefore a two-pass model: (a) create-or-edit all plan nodes; (b) link all cross-task edges. Bulk-creating Won't-Do transitions and consolidation redirect comments can happen in pass (b) or a third pass — design is open per plan-phase.
- The `in_flight` HITL gate is non-bypassable: the plan apply step (regardless of where it runs per decision-11) must refuse edit / Won't-Do / consolidate-away mutations on any child marked `in_flight` without a per-ticket operator confirmation (per the issue's resolved sub-section). Creates that *depend on* an in-flight child go through.
- In-flight detection is a property of the apply step, not the agent. The agent reads the same signals and writes the `in_flight` annotation into the plan draft, but the apply step re-checks at apply time (status / open-PR can change between plan approval and apply).
- **Concurrent-edit on the epic Description**: the refine apply step writes the epic Description via `edit_issue`. If an operator manually edits the Description between refine kick-off and apply (or during a long re-review cycle), the apply will silently overwrite their edit. The apply step **must** fetch-and-diff the current Description against the version the agent saw, and surface a divergence-confirmation HITL prompt if they differ. If the gateway can support optimistic concurrency via an `If-Match` ETag on `edit_issue` in a future iteration, that is the preferred long-term path; for v1, the fetch-and-diff guard at apply time is the minimum bar.

**Operational:**

- The reassess child-discovery query **cannot** be a single disjunctive JQL of the form `parent = <EPIC> OR "Epic Link" = <EPIC>`. Jira returns HTTP 400 (`"Field 'Epic Link' does not exist or you do not have permission to view it"`) when a referenced custom field isn't configured on the searched project — the OR clause does not gracefully no-match, the entire request fails. The apply step / discovery code must therefore issue **two separate JQL queries** (`parent = <EPIC>` and `<epic_link_field_value> = <EPIC>`, where `<epic_link_field_value>` is sourced from `JiraPolicy.epic_link_field` rather than hardcoded — `parent` projects yield the second query as a tautology that we should skip, `Epic Link` projects make the first either empty or 400), tolerate 400s per query, and merge the results.
- The plan draft can grow large for reassess (every pre-existing child plus net-new). `_read_phase_draft` reads the full file; no streaming. Should be fine in practice (epics with 50 children produce drafts comparable to today's larger plan files).
- Per-child-ticket PR shape means the epic does **not** produce a single epic-level PR. The orchestrator's existing per-pipeline `pr_url` artifact remains per-child; there is no aggregate "epic PR" object.

**External (Atlassian):**

- Custom fields for Epic Link vary by Jira site — the gateway already supports a per-site Epic Link field-ID override via `JiraPolicy.epic_link_field` ([`gateway/jira_policy.py:98–193`](../../gateway/jira_policy.py)). The discovery + apply code must read this config value rather than hardcode `"Epic Link"` — see the JQL note above.
- Jira rate limiting on bulk creates: an epic that decomposes into 30+ children generates a `create_issue` burst at apply time. The gateway has rate-limit handling; the apply step should serialize creates (or use Jira's bulk create endpoint if the gateway supports it — needs plan-phase check).
- Issue link types are project-scoped — `Blocks` / `Is blocked by` are near-universal but not guaranteed. The apply step should fail soft (log warning + skip the link, plan node still applies) if a configured link type is missing.

**Dependencies / prereqs:**

| Issue | Status | What it provides |
|---|---|---|
| [#1556](https://github.com/jwbron/egg/issues/1556) | **closed** | Jira gateway read verbs. |
| [#1924](https://github.com/jwbron/egg/issues/1924) | **closed** | Jira gateway write verbs (`create_issue`, `edit_issue`, `add_comment`, `create_issue_link`). |
| [#1931](https://github.com/jwbron/egg/issues/1931) | **closed** | Confluence gateway read verbs. |
| [#2137](https://github.com/jwbron/egg/issues/2137) | **closed** | Independent implement phases / stacked slice PRs. Soft-dep per issue text; MVP works under either model. |
| [#2289](https://github.com/jwbron/egg/issues/2289) | **closed** | In-flight / has-open-PR child handling spec — folded into #1557 per the resolved sub-section. |

All hard prerequisites are landed.

## Explicitly out of scope

Listed explicitly so plan-phase reviewers do not have to grep prose for scope boundaries. From the issue's `## What this issue is not` and `## Out of scope (future follow-ups)` sections, plus deferrals surfaced by this analysis:

- **Jira-label-driven state machine** (`egg-sdlc` / `egg-awaiting-response`). MVP uses the existing MCP / draft HITL surface.
- **Implement-phase cross-child coordination, ordering, and scheduling.** Per decision-6's recommended secondary (manual `submit_task <CHILD-KEY>`), MVP stops at child-ticket creation; downstream auto-spawning is a future follow-up.
- **Optimistic-concurrency `If-Match` ETag enforcement on `edit_issue`.** v1 mitigates concurrent-edit via the fetch-and-diff guard at apply time only. ETag-based protection can land later if it becomes necessary.
- **Forge / Connect-app integration for Jira workflow transitions.** Out of scope per #1556; transitions stay orchestrator-direct only.
- **A new gateway `get_project_metadata` verb to auto-detect `parent` vs `Epic Link` per project.** The current `JiraPolicy.epic_link_field` per-site config is the v1 mechanism (decision-2 opt-3 path). If decision-2 lands on opt-1 (true auto-detect), adding that verb becomes in-scope.
- **Aggregate "epic PR".** Per-child-ticket PRs (issue's resolved sub-section) explicitly preclude this.

## Options Considered

The big shape questions for this issue are (1) how the work is decomposed into slices (decision-1, options A–D below), (2) where the apply step runs (decision-11, orthogonal to slicing), (3) how much epic-specific structure the prompts mandate (feedback Q2), (4) how the plan-node → key mapping is persisted (decision-10), and (5) how aggressively the orchestrator auto-spawns implement pipelines for created children (decision-6). The slice-decomposition choice (decision-1), the apply-step location (decision-11), and the mapping-persistence choice (decision-10) are the headline ones; the rest are mostly closed by the issue itself.

The four options below are slicing options only — they are the option set for **decision-1**. The apply-step-location options are enumerated under decision-11 and not duplicated here.

### Option A: Single-slice end-to-end (1 PR)

**Approach**: Ship all of (A) `submit_task` epic-input handling, (B) refine prompt + epic-Description sink, (C) plan prompt + per-node ticket-shaped bodies, (D) plan apply + linking, (E) orchestrator-direct Jira creds + Won't-Do transitions, (F) reassess-specific planning (consolidate/split/diff), (G) in-flight detection + HITL gating, in one slice.

**Pros**:

- No partial / no-op intermediate states for an operator. The epic pipeline either works or doesn't.
- Reassess and in-flight detection share so much code with the fresh path (same plan apply, same gateway calls) that splitting them adds non-trivial scaffolding cost.
- One PR is reviewable end-to-end against one set of acceptance criteria.

**Cons**:

- Largest single review surface. The fresh-epic path and the reassess + in-flight gating are independently testable but would share a single CI run + a single approval.
- If anything blocks (e.g. the orchestrator-direct cred surface needs an unexpected design loop), nothing ships.

### Option B: Two slices with dependency — fresh-epic foundation → reassess + in-flight

**Approach**: Slice 1 ships A + B + C + D + E for the **fresh-epic path only** (no reassess code path, no in-flight gating, no Won't-Do transitions — fresh epics with no children only). Slice 2 ships F + G on top: reassess discovery + diff, in-flight detection + HITL gate, Won't-Do transitions.

**Pros**:

- Slice 1 is the smaller, lower-risk delivery; an operator can use it immediately on fresh epics.
- Reassess and in-flight gating are the riskiest parts (most novel decisions, most plan-prompt scaffolding) and can iterate independently.
- Two natural review surfaces map well to the two natural test surfaces.

**Cons**:

- Slice 2 must avoid breaking slice 1's fresh-epic path; some refactoring of the plan apply step is likely needed when reassess is added, which means slice 1's apply step lives a relatively short shelf life as-built.
- Slice 1 cannot ship until the orchestrator-direct cred path lands (since #1557's stated direction is to put creds in the orchestrator), so slice 1 still depends on the most-novel cross-cutting piece.

### Option C: Three slices — fresh refine, fresh plan-and-apply, then reassess + in-flight

**Approach**: Slice 1 ships A + B + C (just `submit_task` epic-input + refine prompt + refine apply that writes the epic Description). Slice 2 adds D + E (fresh plan-and-apply that creates children + links them; orchestrator-direct creds shipped here because slice 2 is the first slice that mutates more than one Jira ticket). Slice 3 adds F + G.

**Pros**:

- Smallest individual slices. Slice 1 in particular is a tightly-scoped refine-only delivery.
- Each slice has a clear, demoable user-visible outcome.

**Cons**:

- Three approvals + three CI rounds + three rebase cycles inflates the calendar cost.
- Slice 1 is only partially useful in isolation (an operator who refines an epic and gets the Description written but cannot decompose it is in a weird halfway state).
- Cross-cutting refactors (e.g. the orchestrator-direct cred path) span slice boundaries awkwardly.

### Option D: Two parallel slices — fresh-epic path || reassess + in-flight infra

**Approach**: Slices run **in parallel** (no DAG edge). Slice α delivers the fresh-epic happy path end-to-end (A+B+C+D+E). Slice β delivers the reassess + in-flight infrastructure (F+G plus the reassess-specific parts of D + E).

**Pros**:

- Faster wall-clock delivery if both slices can land in the same window.

**Cons**:

- B / D / E are touched by both slices. Merge conflicts at the plan-prompt + apply-step level are predictable. The slice scheduler does not currently coordinate per-file edits across parallel slices, so this option costs more than it saves.

## Recommended Approach

**Option A (single slice, 1 PR)** is the recommended path, with **Option B (foundation → reassess)** as the natural fallback if the orchestrator-direct cred design loop runs longer than expected.

Reasoning:

1. **The reassess + in-flight code paths share scaffolding with the fresh path, not extend it.** The fresh-epic plan apply already needs the plan-node → key mapping store (decision-10), the gateway create / link calls (decisions 2, 6, 9, 11), and the per-node ticket-description structure (feedback Q2). Reassess adds child-discovery, the diff annotation, and the Won't-Do transition — all of which slot into the same apply step. Splitting them creates rework at the seam.
2. **The orchestrator-direct cred surface (E) is the most architecturally novel piece** but it is also the smallest one — a credential file read, a transition helper, and audit logging. It is the kind of thing that benefits from being landed in the same PR as its only caller (the reassess apply step), so the cred surface and its enforcement live together.
3. **In-flight detection (G) is genuinely small** given the orchestrator already tracks `pr_url` and the gateway already exposes `fields.status.name`. The HITL gate reuses the existing decision-registration plumbing.
4. **Option B is a safety net, not a goal.** If the orchestrator-direct cred design becomes contentious in plan phase, peeling F + G off into slice 2 is mechanical: the fresh-epic path doesn't transition anything and doesn't gate on in-flight, so slice 1 has a clean closing surface.

The work-decomposition decision (decision-1) is registered for the operator with these trade-offs explicitly named.

The recommended secondary choices (the operator overrides via the registered decisions):

- **decision-2 (hierarchy mechanism)**: **opt-3 — operator-configurable per Jira site via the existing `JiraPolicy.epic_link_field` config** ([`gateway/jira_policy.py:98–193`](../../gateway/jira_policy.py)). The gateway already has this primitive; v1 reuses it instead of introducing a new `get_project_metadata` verb (opt-1, which would add a hardware-existence primitive). Opt-3's per-project map is the natural extension if multiple projects per site need different fields; opt-4 (`submit_task` param) and opt-1 (true auto-detect) are forward-compatible upgrades.
- **decision-3 (Won't Do batch vs per-ticket)**: **batch on single plan approval** for non-in-flight children. Per-ticket gates exist for in-flight already; adding a second per-ticket gate on stale-To-Do children inflates HITL surface for low-risk transitions.
- **decision-4 (Done children in plan prompt)**: **exclude Done children entirely**. Cleanest signal; the diff annotation in the plan draft can still surface "Done children left untouched: X, Y, Z" as a one-line audit note without feeding them into the prompt.
- **decision-5 (consolidation survivor)**: **agent picks per-consolidation with rationale, operator overrides per-node before approval**. Heuristic-only options (oldest / most-linked) miss too many real-world cases; full operator picking is high-friction. The agent-with-rationale path matches the rest of the plan draft's information-dense + operator-editable model.
- **decision-6 (implement trigger)**: **manual** — operator runs `submit_task <CHILD-KEY>` per child when ready. MVP scope; auto-spawn introduces ordering + parallelism + rollback questions that warrant their own follow-up issue. The issue itself defines this as out-of-scope.
- **decision-7 (Confluence link discovery)**: **Jira remoteLinks only** for v1 (structured + reliable). URL-scraping the Description for Confluence URLs can land as a follow-up.
- **decision-8 (in-flight signal precedence)**: **OR semantics with signal-source logged on the HITL gate** (option 4). Conservative + transparent — the operator sees which signal fired.
- **decision-9 (reassess refine output)**: **wholesale rewrite**. The refine prompt is specifically instructed to produce a self-contained epic Description; preserving the prior Description below it makes the field grow unboundedly across re-runs. Operators wanting history can read the Description's revision history.
- **decision-10 (plan-node → key mapping)**: **inline yaml-frontmatter in the plan draft**. Human-readable, version-controlled in the work branch, survives draft rewrites because it's part of the draft itself; matches the operator-editable model of the rest of the plan doc.
- **decision-11 (apply-step execution location)**: **opt-3 — hybrid: gateway-mediated writes from a dedicated `apply-epic` agent for everything except `transitions`; orchestrator-only for transitions** (the gateway-blocked verb). Maximises symmetry with today's "agents write through the gateway" pattern, preserves the gateway audit identity for non-transition writes, and confines the new orchestrator-direct credential surface to its narrowly-justified single caller.
- **decision-12 (reassess-trigger override)**: **opt-2 — `--reassess` / `--fresh` mutex flags on `submit_task`** with confirmation prompts on `--fresh` against an epic that has children. Matches the explicit-override pattern operators expect; auto-detect (opt-1) is the default; the explicit-`--mode` flag (opt-3) is a less idiomatic shape; the admin-subcommand (opt-4) splits the surface unnecessarily.

## Open Questions

The questions below are registered as contract decisions and feedback items via the SDLC contract API. The operator can resolve them in the HITL surface; the plan phase reads the resolutions and acts accordingly.

### Resolved in the issue (no re-registration needed)

These were settled in the issue's `## Resolved` section and are not registered as decisions:

- **Implement-shape**: per-child-ticket PRs (each Jira child = its own independent implement pipeline). With #2137, a single child's implement may itself produce a stack of PRs along its slice DAG.
- **In-flight / has-open-PR handling**: in-flight children carry a `do-not-modify-without-confirmation` marker; mutations require per-ticket HITL. Creates that merely depend on an in-flight child apply normally. (Originally tracked in [#2289](https://github.com/jwbron/egg/issues/2289), closed; folded into this issue's plan-phase sub-bullet.)

### Multiple-choice decisions

- **[decision-1]** How should this work be decomposed into slices? See Options A–D above.
- **[decision-2]** Hierarchy mechanism for child tickets — Epic Link vs parent field, auto-detect vs configured vs supplied.
- **[decision-3]** Reassess Won't Do transitions — batch on single plan approval vs per-ticket HITL.
- **[decision-4]** Done children handling in the plan prompt — exclude entirely vs include with marker vs include with redacted scope.
- **[decision-5]** Consolidation survivor selection — oldest / most-linked / agent-picks / deterministic-first.
- **[decision-6]** Implement-phase trigger for created children — manual / auto-spawn / topological auto-spawn / opt-in.
- **[decision-7]** Confluence link discovery scope — remoteLinks only / + URL scrape / + recursive / operator-supplied.
- **[decision-8]** In-flight detection signal precedence — OR / orchestrator-wins / Jira-wins / OR-with-source-logged.
- **[decision-9]** Reassess refine-output strategy — wholesale rewrite / merge / append / per-run HITL.
- **[decision-10]** Plan-node → Jira-key mapping persistence — inline yaml-frontmatter / separate yaml file / pipeline artifact / hidden Jira custom field.
- **[decision-11]** Apply-step execution location — sandbox-agent / orchestrator / hybrid / all-orchestrator-no-gateway. Orthogonal to decision-1's slicing; needed by plan phase regardless.
- **[decision-12]** Reassess-trigger override on `submit_task` — auto-only / `--reassess`/`--fresh` flags / `--mode={auto,reassess,fresh}` / admin subcommand. (Promoted from feedback-1 Q5 in response to reviewer NACK; Q5 itself is now superseded by this decision — operator should ignore Q5 in the feedback bundle.)

### Open-ended feedback

Bundled under **[feedback-1]**:

- **Q1** Orchestrator-direct Jira credentials configuration (where the creds live, rotation story, audit/logging expectations).
- **Q2** Plan-node ticket-description structure — fixed sections vs agent-discretion.
- **Q3** Cross-task dependency representation — Jira link types supported beyond Blocks / Is-blocked-by.
- **Q4** PR-link writeback to Jira ticket — automatic remote-link / comment when a child's implement opens a PR.
- **Q5** ~~`submit_task --reassess` / `--fresh` override flag — should auto-detection be overridable?~~ **Superseded by [decision-12]** (promoted in response to NACK feedback; structurally a multi-choice decision, not open-ended feedback). Operator: please answer decision-12 and ignore Q5.

## Complexity Assessment

**high.** This is an architectural change touching:

- `submit_task` MCP tool input handling + issue-type detection.
- Two phase prompts (refine + plan) with new epic-shaped templates.
- A new refine apply step (epic Description sink).
- A new plan apply step (multi-ticket create + link + transition).
- A new orchestrator-direct Jira credential surface (with its own rotation, audit, and operator-setup story).
- New in-flight detection logic crossing pipeline state + Jira API + remote-link parsing.
- A new HITL gate (per-ticket in-flight confirmation) integrated with the existing decision surface.
- Idempotent state across re-runs (the plan-node → key mapping).

The MVP is well-bounded and prerequisites are all landed, but the surface area is wide enough that a single slice will produce a non-trivial PR. Plan-phase decomposition is itself the headline decision (decision-1).

---

*Authored-by: egg*
