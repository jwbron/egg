# Analysis: Add write operations to Jira gateway (create/update/comment/link)

> Issue: #1924 | Phase: refine

## Problem Statement

Issue [#1556](https://github.com/jwbron/egg/issues/1556) landed the v1 read-only Jira gateway: sandboxed agents can fetch tickets, search via JQL, and read comments through `/api/v1/jira/*`, with Atlassian credentials held exclusively in the gateway, project allowlist enforcement, private-mode-only access, conservative JQL scope extraction, and structured audit logging. Writes were explicitly future-scope.

Issue [#1557](https://github.com/jwbron/egg/issues/1557) (Jira-epic SDLC pipelines) is now blocked on those writes. The refine phase needs to push the agent-authored analysis to the **epic's Description field** (`editJiraIssue`); the plan phase needs to create **child tickets under an epic** with parent / Epic Link set (`createJiraIssue`), update existing children when scope shifts (`editJiraIssue`), drop comments pointing at survivors during consolidation (`addCommentToJiraIssue`), and wire cross-task dependency edges (`createIssueLink`).

#1924 is the **bounded write extension** of #1556. It must:

1. Add four narrow write verbs: `createJiraIssue`, `editJiraIssue`, `addCommentToJiraIssue`, `createIssueLink`.
2. Inherit every v1 invariant: private-mode only, project allowlist, gateway-held credentials, structured audit, fail-closed on missing config.
3. Keep transitions, worklogs, attachments, and deletions **permanently denied** at the path / method validator (matching #1556's `JIRA_WRITE_VERBS_DENIED`). The write extension does **not** widen that escape hatch.
4. Land as a **pure extension** of the existing modules (`gateway/jira_client.py`, `gateway/jira_policy.py`, `gateway/gateway.py` route layer, `sandbox/scripts/jira`, `gateway/tests/test_jira_*`) — no re-architecting.
5. Preserve the zero-credential invariant: Atlassian creds never enter the sandbox; the new sandbox wrapper subcommands shell to the gateway over `EGG_SESSION_TOKEN`.

This analysis surveys the existing v1 surface, enumerates each new verb's Atlassian shape, names the design choices that need a human answer, and recommends an approach that keeps the surface tight, auditable, and reusable for #1557.

## Current Behavior

### v1 read surface (delivered by #1556)

The gateway exposes four read routes, all decorated with `@require_session_auth` + `@require_private_mode`:

| Route | Atlassian endpoint | Notes |
|-------|-------------------|-------|
| `POST /api/v1/jira/ticket/get` | `GET /rest/api/3/issue/{key}` | Default `expand=renderedBody,renderedFields`; 404 envelope; field allowlist (≤32 entries). |
| `POST /api/v1/jira/search` | `POST /rest/api/3/search/jql` | Static JQL scope extractor (`gateway/jira_search.py`); cursor pagination via `nextPageToken`; `maxResults` clamped to 100. |
| `POST /api/v1/jira/ticket/comments` | `GET /rest/api/3/issue/{key}/comment` | 404 envelope; `expand=renderedBody`. |
| `POST /api/v1/jira/execute` | passthrough | Regex-allowlisted, GET-only, never writes. |

Key building blocks the write extension reuses verbatim:

| Mechanism | File | Role |
|-----------|------|------|
| Per-container session auth | `gateway/auth.py` `require_session_auth` | Loads `Session` (`session_mode`, `pipeline_id`, `agent_role`, `jira_ticket`) into `g`. |
| Mode gate | `gateway/mode_gate.py` `@require_private_mode` | Stamps `__egg_requires_private_mode__ = True`; route-enumeration test in `test_jira_routes.py` proves every Jira route carries it. |
| Project allowlist | `gateway/jira_policy.py` (`JiraPolicy`) | Mtime-cached YAML from `config/context-filters.yaml` (`jira.projects: [...]`); fail-closed on empty/malformed. |
| Credential injection | `gateway/jira_credentials.py` (`JiraCredentialsManager`) | `ATLASSIAN_*` preferred / `JIRA_*` fallback; mtime-cached; raises `JiraCredentialsUnavailable` → 503. |
| Path / method validator | `gateway/jira_client.py` `validate_jira_api_path` + `JIRA_WRITE_VERBS_DENIED` | Hard denylist for `transitions`, `worklog`, `attachments`, `watchers`, and HTTP `DELETE`/`PUT`/`PATCH`. |
| HTTPX wrapper | `gateway/jira_client.py` `JiraClient._request` | Basic auth + 429 retry **only on GET**; non-GET methods get one-shot semantics (deliberately future-safe per `JiraClient._request` lines 322–331). |
| Audit | `gateway/gateway.py` `audit_log` + `_session_jira_context` | Uniform `event_type="gateway_operation"` with per-verb operation name and session context (`pipeline_id`, `agent_role`, `jira_ticket`). |
| Sandbox wrapper | `sandbox/scripts/jira` | Bash → gateway POST; `EGG_SESSION_TOKEN` Bearer auth; gateway-health pre-check; verb dispatch. |

### Atlassian REST API v3 shapes the write extension must front

| Verb | Atlassian endpoint | Method | Body shape (abridged) | Response |
|------|-------------------|--------|------------------------|----------|
| Create issue | `POST /rest/api/3/issue` | POST | `{"fields": {"project": {"key":"FOO"}, "issuetype": {"name":"Task"}, "summary": "...", "description": ADF, "labels":[...], "parent":{"key":"FOO-1"} or "customfield_10014":"FOO-1"}}` | `{"id": "...", "key": "FOO-123", "self": "..."}` |
| Update issue | `PUT /rest/api/3/issue/{key}` | PUT | `{"fields": {"summary":"...", "description": ADF, "labels":["a","b"]}, "update": {"labels": [{"add":"x"},{"remove":"y"}]}}` | `204 No Content` (no body) |
| Add comment | `POST /rest/api/3/issue/{key}/comment` | POST | `{"body": ADF, "visibility": {...}}` | full comment object |
| Create issue link | `POST /rest/api/3/issueLink` | POST | `{"type": {"name":"Blocks"}, "inwardIssue": {"key":"FOO-1"}, "outwardIssue": {"key":"FOO-2"}, "comment": {...optional}}` | `201 Created` (no body in v3) |

Two facts shape the design:

- **Method denylist conflict.** v1's `JIRA_WRITE_VERBS_DENIED` lumps HTTP `DELETE`/`PUT`/`PATCH` with the path-segment denylist (`transitions`, `worklog`, `attachments`, `watchers`). `editJiraIssue` is `PUT` upstream — we cannot keep the method denylist verbatim for writes. We need a clean separation between (a) the `/execute` passthrough's method allowlist (stays GET-only forever — never bypasses dedicated routes) and (b) the per-route method permissions (POST/PUT for the dedicated write routes only).
- **ADF body format.** Description and comment bodies are Atlassian Document Format JSON, not plain text. Wire format is `{"type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"..."}]}]}`. Bash-string construction of ADF in `sandbox/scripts/jira` is awkward; the wrapper needs an ergonomic input path.

### What #1556's v1 left "future-safe" for writes

The v1 architect notes (visible in `jira_client.py` lines 30–31, 86–108, and the `JIRA_WRITE_VERBS_DENIED` comment) explicitly anticipated writes:

- `_request()` retries 429 only for GET, matching the issue's "writes never auto-retry" invariant.
- `JIRA_API_ALLOWED_PATHS` is the regex extension point — write paths (`issue` for create, `issue/<KEY>` for edit, `issue/<KEY>/comment` for comment-create, `issueLink` for link-create) need new patterns or, preferably, **dedicated narrow handlers that skip the generic path validator entirely** because each write verb has a tighter shape than a regex captures.
- `JIRA_WRITE_VERBS_DENIED` was framed as "permanent" — `transitions`, `worklog`, `attachments`, `watchers`, `DELETE`. We keep the segment denylist permanent. We loosen the method denylist **only inside the dedicated write client methods**; `/execute` and `validate_jira_api_path` stay GET-only.

### #1557's exact ask of #1924

From [#1557's body](https://github.com/jwbron/egg/issues/1557):

- **Refine sink** — `editJiraIssue` to write the refined analysis to the epic's Description field. Single-field update.
- **Plan sink, fresh epic** — `createJiraIssue` with parent / Epic Link to place each plan-node child under the epic.
- **Plan sink, reassess** — mix of `editJiraIssue` (scope changes), `createJiraIssue` (net-new), `addCommentToJiraIssue` (consolidation survivors point at the merged-away keys), and `createIssueLink` for cross-task dependency edges.
- **Won't-Done transitions** — explicitly out of scope for #1924; the orchestrator does these directly with its own creds (transitions never touch the agent-facing gateway).

That ask is satisfied by exactly the four verbs in this issue.

## Constraints

**Security / architectural (hard invariants — no exceptions):**

- **Zero credentials in the sandbox.** Atlassian creds live only in the gateway. New write routes follow the same `creds_provider=get_jira_credentials` pattern.
- **Private-mode only.** Every new write route gets `@require_session_auth` + `@require_private_mode`. The route-enumeration regression test in `test_jira_routes.py` must extend to assert `__egg_requires_private_mode__` on each new route.
- **`*.atlassian.net` stays out of the Squid allowlist** so containers cannot bypass the gateway via direct REST. v1's regression test in `test_allowed_domains.py` continues to enforce this.
- **Permanent denylist preserved.** `transitions`, `worklog`, `attachments`, `watchers`, `DELETE` remain rejected by `validate_jira_api_path`. The write client methods bypass the path validator (they hardcode their own paths), but the `/execute` passthrough stays GET-only forever.
- **Project allowlist enforced on every write call.** For `createJiraIssue`, the gate is `request.body.fields.project.key` (not a ticket key — the ticket doesn't exist yet). For `editJiraIssue` / `addCommentToJiraIssue` / `createIssueLink`, the gate is the ticket key(s) parsed from the request, exactly as in v1 read paths.
- **Verb allowlist by construction.** New writes are per-verb routes — one Python handler per Atlassian verb. No generic "issue update via /execute" path.
- **Body shape validation is a hard gate.** Each write route runs request-body validation (shape + size + field allowlist) before calling Atlassian. Pass-through of arbitrary `fields` dicts is a security risk (custom fields may grant escalation; see Open Questions).
- **Audit on success and on rejection.** Every write call emits an audit record, including rejected calls (with reason). For reject cases, no Atlassian call is made.
- **Writes never auto-retry on 429** (already true in `JiraClient._request`). A failed write surfaces the 429 to the caller; the caller's idempotency story (see below) decides what to do.

**Operational:**

- **Idempotency is the new sharp edge.** A retried `createJiraIssue` could create duplicate tickets; a retried `addCommentToJiraIssue` could double-post. v1 GETs are naturally idempotent; writes are not. The pipeline orchestrator (or the agent caller) must be responsible for "did this already succeed?" reasoning, OR the gateway grows an idempotency-key cache. (See Open Questions.)
- **Per-instance Jira customization** — Epic Link mechanism varies per project (legacy `customfield_10014` Epic Link vs. `parent.key` next-gen hierarchy). The gateway must not encode a single hardcoded customfield ID; either (a) the caller specifies the field shape directly, or (b) the gateway has an instance-level config knob.
- **ADF handling.** Comments and descriptions are ADF JSON. Bash construction of ADF is painful; sandbox wrapper needs ergonomic input (plain-text → ADF wrapping, or stdin/file reading, or both).
- **Test strategy unchanged.** `httpx.MockTransport` already used for read endpoints handles writes too. Per-route 403 grid extends naturally.
- **Write audit retention.** Writes are higher-stakes than reads; audit records must persist to the same sink with no truncation. Body content (description/comment) is **not** logged verbatim — only length and structural metadata (number of fields, label count). Avoid leaking PII.
- **Rate limits** — Atlassian's per-user write quota is tighter than the read quota. The gateway should surface 429s without retrying writes; the orchestrator can re-issue at its discretion with idempotency keys.

**Dependencies / coupling:**

- **#1556 must remain green.** The write extension is purely additive: new files are isolated; modifications to `gateway.py` and `jira_client.py` add functions/routes without altering existing ones (the only edit to existing code is the path/method validator's separation between `/execute` enforcement and write-path enforcement).
- **#1557 is a hard consumer.** Its plan-phase apply step calls all four new verbs. Its refine-phase apply calls `editJiraIssue` for the epic Description. Whatever shape the gateway exposes here, #1557's apply step has to match exactly.
- **No bash → bin/jira-cli detour.** Writes stay REST-only via the existing wrapper, same as reads.

**External (Atlassian):**

- Issue creation REST docs: [POST /rest/api/3/issue](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-post). Body uses `fields` (replace) and `update` (add/remove). The `parent` field is the modern hierarchy; `customfield_10014` (Epic Link) is legacy and only on classic projects.
- Issue edit: [PUT /rest/api/3/issue/{issueIdOrKey}](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-put). Returns 204; supports `?notifyUsers=false` to suppress email; supports `?returnIssue=true` to get the post-edit body. Both flags relevant.
- Add comment: [POST /rest/api/3/issue/{issueIdOrKey}/comment](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/#api-rest-api-3-issue-issueidorkey-comment-post). Supports comment visibility restriction (role/group). Probably out of scope for v1.1 writes.
- Create link: [POST /rest/api/3/issueLink](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-links/#api-rest-api-3-issuelink-post). Link type names are per-instance (`Blocks`, `Relates`, `Cloners`, `Duplicate`, …). The endpoint accepts an optional `comment` payload to drop a comment on the inward issue at link time.
- Available link types come from [GET /rest/api/3/issueLinkType](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-link-types/) — useful for v1 validation if we need to gate `type.name` against a known set without hardcoding.
- ADF schema: [Atlassian Document Format reference](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/). Minimal valid doc is `{"type":"doc","version":1,"content":[]}`.
- Atlassian's recommendation against API tokens for production integrations applies equally to writes; #1556 chose API token + bot account for clean attribution and that decision carries through.

## Options Considered

The five biggest design axes are: (1) endpoint surface shape, (2) method-allowlist separation, (3) request-body validation depth, (4) idempotency, and (5) sandbox wrapper UX for ADF. Each has a clear recommendation; the open questions below name the residual decisions for the human.

### Endpoint surface shape

#### A1. Four dedicated narrow routes, no `/execute` involvement — **preferred**

**Approach**: New routes mirroring the v1 verb-noun shape:

- `POST /api/v1/jira/ticket/create` → upstream `POST /rest/api/3/issue`
- `POST /api/v1/jira/ticket/edit` → upstream `PUT /rest/api/3/issue/{key}`
- `POST /api/v1/jira/ticket/comment/add` → upstream `POST /rest/api/3/issue/{key}/comment`
- `POST /api/v1/jira/issue-link/create` → upstream `POST /rest/api/3/issueLink`

`/api/v1/jira/execute` stays GET-only. The new routes call new `JiraClient` methods (`create_issue`, `edit_issue`, `add_comment`, `create_issue_link`) that bypass `validate_jira_api_path` and hardcode their upstream paths.

**Pros**:
- Each verb has its own audit operation name, its own body validator, its own per-route test grid.
- `/execute`'s GET-only invariant is preserved by construction (no risk of method-list creep).
- Path validator stays read-only; the permanent segment denylist (`transitions`, `worklog`, `attachments`, `watchers`) is unchanged.
- Mirrors `gh` wrapper structure (`/api/v1/gh/pr/create`, `/api/v1/gh/pr/comment`, etc.) — operators see the same convention.
- Future writes (visibility-restricted comments, link-deletion if ever in scope) drop in as additional narrow routes.

**Cons**:
- Four new routes to write, test, and document — but each is small (≤120 lines including audit and validation).

#### A2. Generalize `/execute` to allow whitelisted write paths

**Approach**: Add `POST` and `PUT` to `ALLOWED_METHODS`, expand `JIRA_API_ALLOWED_PATHS` to include `^issue$`, `^issue/<KEY>$`, `^issue/<KEY>/comment$`, `^issueLink$`. Send writes through `/execute`.

**Pros**: Single endpoint to extend; less code.

**Cons**:
- Defeats the "verb allowlist by construction" principle. Body validation now lives in a generic dispatcher, not a per-verb handler.
- Audit operation name collapses to `jira_execute` for everything — coarse-grained logs.
- Couples `/execute`'s read-only invariant to the write-verb logic. One mistake widens both.
- Per-verb tests harder; more state in the dispatcher.
- **Rejected.**

#### A3. RESTful HTTP-method paths (`POST /api/v1/jira/ticket`, `PUT /api/v1/jira/ticket/<KEY>`, …)

**Approach**: One path per resource, multiple methods.

**Pros**: HTTP-purist.

**Cons**:
- Inconsistent with v1 (everything is `POST` regardless of upstream verb).
- Bash wrapper has to deduce the right method per command — more error-prone.
- Audit naming collapses unless we add per-method operation suffixes anyway.
- **Rejected** for inconsistency with the rest of the gateway.

### Method-allowlist separation

#### B1. Split the allowlist into "execute-passthrough" vs "write-client" — **preferred**

**Approach**:
- `validate_jira_api_path` (used by `/execute`) keeps `ALLOWED_METHODS = {"GET"}`. Path-segment denylist is unchanged.
- Each write `JiraClient` method (`create_issue`, etc.) issues its hardcoded method (`POST`/`PUT`) directly through `_request`. They bypass the path validator entirely because the path is fixed in code, not user-supplied.
- The path-segment denylist (`transitions`, `worklog`, `attachments`, `watchers`) still applies to **paths within write client methods** — verified by hardcoding paths that don't include any of these segments.

**Pros**: Clean separation. No code path can reach `transitions` etc. The path validator's contract stays narrow.

**Cons**: None significant.

#### B2. Widen `validate_jira_api_path` to know about write methods

**Approach**: Add `POST`/`PUT` to `ALLOWED_METHODS`, gate them with method-specific allowed-path tables.

**Pros**: Single source of method+path truth.

**Cons**: Couples `/execute` exposure to write-method config. A future maintainer who widens `ALLOWED_METHODS` for a read use case accidentally exposes a write surface. **Rejected**.

### Request-body validation depth

#### C1. Curated field allowlist + structural validation — **preferred**

**Approach**: Each write route validates its body against an explicit schema:

- **createJiraIssue body**: `{ projectKey: <ALLOWED-KEY>, issuetype: <NAME from {Task, Story, Bug, Epic, Sub-task} or numeric ID>, summary: <str ≤ 255>, description?: <ADF dict | str>, labels?: <list[str ≤ 25]>, parent?: { key: <TICKET-KEY> }, epicLink?: <TICKET-KEY> }` plus an optional `customFields: { customfield_NNNN: <value> }` map keyed by validated field IDs.
- **editJiraIssue body**: `{ ticket: <TICKET-KEY in allowlist>, summary?, description?: <ADF | str>, labels?: <list[str]>, addLabels?: <list[str]>, removeLabels?: <list[str]>, customFields?: {...}, notifyUsers?: bool (default true) }`. Translated to Atlassian's `fields` + `update` body shape inside the client.
- **addCommentToJiraIssue body**: `{ ticket: <TICKET-KEY in allowlist>, body: <ADF | str> }` (visibility intentionally not exposed in v1).
- **createIssueLink body**: `{ type: <NAME from allowlist>, inwardIssue: <TICKET-KEY in allowlist>, outwardIssue: <TICKET-KEY in allowlist>, comment?: <ADF | str> }`.

The gateway translates plain-text `description`/`body` to ADF using a minimal paragraph wrapper (`{type:"doc",version:1,content:[{type:"paragraph",content:[{type:"text",text:S}]}]}`), and accepts pre-built ADF dicts pass-through. No HTML or Markdown parsing in v1.

**Pros**:
- Tight body shape; rejects garbage at the route layer before any Atlassian call.
- Custom fields explicitly opt-in (via the `customFields` map and `epicLink` shorthand) — agents can't accidentally smuggle in `Security Level`, `Components`, or other escalation-prone fields.
- ADF wrapping makes the bash wrapper trivial — agents pass `--description "Some text"` and the gateway handles the JSON.
- `notifyUsers` exposed as a single bool so agents can suppress email storms during plan-apply.

**Cons**:
- More schema code than a passthrough.
- Choice of issuetype name vs ID has per-instance variation (some instances use ID).

#### C2. Pure passthrough of Atlassian's body

**Approach**: Forward whatever body the agent sends.

**Pros**: Maximum flexibility.

**Cons**:
- Agents can set arbitrary custom fields, including `Security Level`, `Components`, `Sprint`, watchers — fields the operator may not have intended to expose.
- ADF burden falls on bash wrapper.
- No invariant on summary length, label cap, etc.
- **Rejected** as too permissive for v1.

#### C3. Minimal validation (project + ticket + length only)

**Approach**: Validate just the project allowlist and a hard length cap; pass everything else through.

**Pros**: Less code.

**Cons**: Same custom-field exposure risk as C2. **Rejected**.

### Idempotency for create / comment

#### D1. Caller-supplied idempotency key + gateway-level dedup cache (in-memory, TTL ≤ 5 min) — **preferred**

**Approach**: Each `createJiraIssue` and `addCommentToJiraIssue` request may include `idempotency_key: <opaque-string>` in the body. The gateway maintains a per-process in-memory dict keyed by `(verb, project, idempotency_key)` → `(timestamp, response)` with a 5-minute TTL. A duplicate request inside the window short-circuits to the cached response. `editJiraIssue` and `createIssueLink` are not idempotency-keyed (edit is naturally idempotent on a stable ticket; link is unique per `inward+outward+type`).

The TTL is short to keep memory bounded and to reflect the realistic retry window (orchestrator retries within seconds of a transient 5xx, not hours later).

**Pros**:
- Solves the "duplicate ticket on retry" problem without involving Atlassian.
- Caller-controlled key — the orchestrator can derive it from `(pipeline_id, plan_node_id)` for stable retries.
- Falls back gracefully: missing key → no dedup, behavior identical to v1's read paths.
- Memory bounded (small dict, short TTL, one process — gateway runs single-replica per session).

**Cons**:
- New gateway state. Must be cleared on gateway restart (acceptable — restarts are rare and short retry windows mean little surface).
- Cache eviction logic to write and test.

#### D2. Atlassian-native idempotency

**Approach**: Atlassian's REST API does not natively support idempotency keys. **Not an option.**

#### D3. Orchestrator-only responsibility — gateway is stateless

**Approach**: Document non-idempotency. Orchestrator is responsible for "did I already create this ticket?" via its own state.

**Pros**: Simplest gateway code.

**Cons**:
- Pushes the burden onto every consumer. #1557's plan-apply is one consumer; future ones (impact-analysis skill, manual scripts, future SDLC phases) all repeat the same logic.
- Gateway audit log already records "created FOO-N" but the orchestrator doesn't necessarily see the audit log between retries.
- Easier to ship in v1, harder to retrofit later.

### Sandbox wrapper UX for ADF

#### E1. Plain-text + flag-based ergonomic input (`--summary`, `--description`, `--description-file`, `--labels a,b`) — **preferred**

**Approach**: New `sandbox/scripts/jira` subcommands:

```
jira ticket create --project KEY --type Task --summary "..." [--description "..."|--description-file path|--description-stdin] [--labels a,b] [--parent FOO-1] [--epic-link FOO-2] [--idempotency-key K]
jira ticket edit  TICKET [--summary "..."] [--description "..."|--description-file path] [--labels a,b] [--add-labels x] [--remove-labels y] [--no-notify]
jira ticket comment add TICKET [--body "..."|--body-file path|--body-stdin] [--idempotency-key K]
jira link create --type Blocks --inward FOO-1 --outward FOO-2 [--comment "..."] [--idempotency-key K]
```

The wrapper builds the JSON request body with Python (already used in v1 wrapper) and posts to the gateway. Body content arrives as plain text; the gateway wraps it in ADF.

**Pros**:
- Agent ergonomics: no ADF construction in agent code.
- File / stdin path supports multi-line descriptions naturally.
- Aligns with `gh pr create --body-file` ergonomics from the existing `gh` wrapper.

**Cons**:
- Wrapper is bigger than v1's. Manageable (≤500 LoC bash + Python heredocs).

#### E2. Raw ADF passthrough only

**Approach**: Wrapper accepts only pre-built ADF JSON.

**Pros**: Smaller wrapper.

**Cons**: Pushes ADF construction into agent prompts / code. Painful and bug-prone.

#### E3. Markdown → ADF in the wrapper

**Approach**: Agent passes Markdown; wrapper converts.

**Pros**: Familiar input shape.

**Cons**: Markdown → ADF is a non-trivial library dependency (e.g., `mdx_to_adf`) with surprising edge cases (tables, code blocks). Wrap in v1.x if needed.

## Recommended Approach

**Adopt A1 + B1 + C1 + D1 + E1.** The combined design preserves every v1 invariant, keeps `/execute` GET-only, names a per-verb route per Atlassian write, validates request bodies up front, solves idempotency at the gateway, and makes the sandbox wrapper agent-friendly.

Concretely the surface becomes:

| New gateway route | Atlassian endpoint | Audit operation | Notes |
|---|---|---|---|
| `POST /api/v1/jira/ticket/create` | `POST /rest/api/3/issue` | `jira_ticket_create` | projectKey-allowlisted; idempotency-keyed; ADF wrapping; returns Atlassian's `{id, key, self}`. |
| `POST /api/v1/jira/ticket/edit` | `PUT /rest/api/3/issue/{key}` | `jira_ticket_edit` | ticket-allowlisted; supports `notifyUsers=false`; translates `addLabels`/`removeLabels` to Atlassian `update` block. |
| `POST /api/v1/jira/ticket/comment/add` | `POST /rest/api/3/issue/{key}/comment` | `jira_comment_add` | ticket-allowlisted; idempotency-keyed; ADF wrapping; **no visibility restriction in v1**. |
| `POST /api/v1/jira/issue-link/create` | `POST /rest/api/3/issueLink` | `jira_issue_link_create` | both `inwardIssue` and `outwardIssue` projects allowlisted; link `type.name` against a configurable allowlist (default `Blocks`, `Relates`). |

Permanent denylist unchanged (`transitions`, `worklog`, `attachments`, `watchers`, `DELETE`). `/execute` stays GET-only forever. `validate_jira_api_path` is unchanged.

`JiraClient` gains four methods: `create_issue`, `edit_issue`, `add_comment`, `create_issue_link`. They use `_request` directly with hardcoded paths; they do **not** auto-retry on 429 (already enforced by `_request` for non-GET). They translate the route-layer body schema to Atlassian's wire shape (e.g., `addLabels`/`removeLabels` → `update.labels: [{add},{remove}]`).

Idempotency cache lives in `gateway/jira_client.py` (or a thin `gateway/idempotency.py` if a Confluence write surface in the future would reuse it). Module-level dict, threading lock, 5-minute TTL, evicted lazily.

`sandbox/scripts/jira` grows the four new subcommands per E1, sharing the `call_gateway` helper. The wrapper validates flag presence client-side; semantic validation lives at the gateway.

Tests:

- `gateway/tests/test_jira_client.py` — per-method shape tests with `httpx.MockTransport`, ADF wrapping, label-update translation, idempotency hit/miss.
- `gateway/tests/test_jira_routes.py` — per-route 403 grid (public mode → 403; missing creds → 503; non-allowlisted project / ticket → 403; malformed body → 400; success path → 2xx with audit assertions). **Route-enumeration regression** updated to include the four new routes.
- `gateway/tests/test_jira_idempotency.py` — new file; cache hit, TTL expiry, distinct keys, distinct verbs sharing keys.
- `tests/sandbox/test_jira_wrapper.py` — new subcommand smoke tests (mocked gateway).
- Adversarial body tests: oversized summary/description, custom-field smuggling, unknown issuetype, non-allowlisted link type.

Documentation:

- Extend `docs/reference/jira-wrapper.md` with the four new verbs and ADF wrapping rules.
- Update `config/context-filters.yaml` documentation if a link-type allowlist is added (Open Question 5).

## Open Questions

> Every question below MUST be registered as a contract decision or feedback item — see commands at end of section. Any item not registered is invisible to the human reviewer.

1. **Custom field exposure scope.** Beyond `summary`, `description`, `labels`, `parent`, and an `epicLink` shorthand, do we expose a generic `customFields: {customfield_NNNN: value}` map in the create / edit body, and if so under what allowlist? (Risk: agents could set `Security Level`, `Sprint`, `Components`, watchers, story points, etc.)

2. **Epic Link mechanism.** Use `parent.key` (next-gen / company-managed projects) only, `customfield_10014` (Epic Link, classic / team-managed) only, or auto-detect / accept either via the `epicLink` shorthand?

3. **Idempotency key requirement.** Should `idempotency_key` be **required** for `createJiraIssue` and `addCommentToJiraIssue`, or **optional with a documented warning**?

4. **Link type allowlist source.** Hardcode the v1 allowed `type.name` set (`Blocks`, `Relates`), pull from a new `config/context-filters.yaml` `jira.link_types: [...]` section (operator-configurable), or accept any name the upstream Jira instance recognizes?

5. **`notifyUsers` default.** When the caller of `editJiraIssue` doesn't pass `notifyUsers`, should the gateway default to `true` (Atlassian's default — sends email) or `false` (matches automation-style "quiet" updates)?

6. **`addCommentToJiraIssue` visibility.** v1 hides the `visibility` field entirely. Should v1 expose role/group visibility restrictions, or defer until a concrete consumer needs it?

7. **Body input format for description / comment.** Accept plain text (gateway wraps to ADF), accept pre-built ADF dicts (passthrough), or both?

8. **Issuetype identifier.** Accept issuetype by **name** (`"Task"`), by **numeric ID** (Atlassian's stable identifier), or both?

9. **createIssueLink project allowlist semantics.** Require **both** `inwardIssue` and `outwardIssue` projects to be allowlisted (strict, prevents existence-leak), require **only one** (looser, allows linking to "external" projects the agent can read about), or one with the other in a separate "linkable-only" config list?

10. **Per-role write gating.** Should writes be restricted to specific agent roles (e.g., orchestrator-only, or `refiner` for description edits and `planner` for ticket creation), or unrestricted within an allowlisted project?

11. **Phase gating for writes.** Should writes only be callable in specific SDLC phases (refine for description edits, plan for ticket creation), enforced via the existing `gateway/phase_filter.py` style, or unrestricted?

12. **Write rate-limit policy.** Agree on "no auto-retry on 429 for writes" (matches v1's `_request` already)? Confirm whether a 429 on a write should still emit `jira_upstream_rate_limited` audit (currently only emitted from inside `_request`'s retry loop, which writes skip)?

13. **`createJiraIssue` response shape.** Return Atlassian's raw `{id, key, self}` body, OR a normalized envelope (`{key, id, browse_url, status: "created"}`)? Affects #1557's plan-draft mapping format.

14. **`editJiraIssue` response shape.** Atlassian returns 204 (no body). Return `{status: "updated", key}` envelope, or echo back the post-edit ticket via Atlassian's `?returnIssue=true` flag (extra read latency, but caller has the new state)?

15. **Body-size limits.** Pick caps for `summary` (Atlassian limit is 255), `description` length (Atlassian has no published limit but huge bodies waste audit log + memory), `labels` count and per-label length (Atlassian: 255 chars, no count limit), `comment body` length, and `customFields` map size.

16. **Idempotency cache scope.** Per-gateway-process (in-memory, TTL 5 min) — confirm; or persist to disk / Redis for multi-replica gateway deployments? (egg currently runs single-replica gateway per session; in-memory is plausibly sufficient.)

17. **Cross-project parent.** Reject `parent.key` whose project differs from the new ticket's project (strict — Atlassian sometimes allows cross-project epics, sometimes not), or accept and let Atlassian decide?

18. **Existing-issue probe before create / edit.** For idempotency-keyed retries, do we need to also do an existence check (`is there already a ticket with summary "Foo" in project ENG"`) for safety, or is the idempotency key sufficient?

19. **Sandbox wrapper input ergonomics.** Confirm flags: `--description`, `--description-file`, `--description-stdin` for create / edit; analogous for comment body. Any of these you specifically want or don't want?

20. **Audit body redaction.** Confirm: log only structural metadata (field names changed, content lengths, label counts), never body content. Any exception (e.g., labels — usually safe to log)?

21. **Test coverage requirements.** Should the per-route 403 grid include adversarial bodies (oversized summary, unknown issuetype, custom-field smuggling, unicode in keys) as part of the same file, or a separate `test_jira_writes_adversarial.py`?

22. **Multi-tenant hooks.** v1 reads are single-tenant (one Atlassian site). Writes inherit that. Confirm we leave the multi-tenant seam exactly where #1556 left it, with no further work in #1924?

23. **`createIssueLink` comment payload.** Atlassian allows attaching a comment when creating a link. Surface that in v1 (via `comment` field in body), or require a separate `addCommentToJiraIssue` call?

24. **Dry-run / validation mode.** Do we expose a `?dry_run=true` parameter that validates the body and policy gates without calling Atlassian, useful during plan-phase preview? Or is the gateway always live, and the orchestrator does its own preview?

25. **Failure recovery for batched plan apply.** The orchestrator's plan-apply step calls many writes in sequence (create N children, edit M, link K). The gateway is stateless. Confirm: rollback is the orchestrator's responsibility; the gateway will not implement transactions or compensating actions.

26. **Error envelope for write upstream errors.** Reuse `_jira_error_from_upstream` (passes 4xx through as 4xx, 5xx as 502)? Or richer error mapping for known write-failure modes (e.g., 400 with `errorMessages: ["Field 'parent' cannot be set ..."]` — surface the field name to the caller)?

27. **Documentation home.** Where do the new verbs live in docs — extend `docs/reference/jira-wrapper.md`, add a new `docs/reference/jira-writes.md`, or both?

To register these:

```bash
# Multiple-choice questions (markdown checkbox surface)
egg-contract add-decision \
  --question "Custom field exposure scope (Open Q1)" \
  --options "Generic customFields map (any customfield_NNNN, only structural validation)" \
            "Operator-configured customField allowlist (config/context-filters.yaml: jira.custom_fields: [...])" \
            "No customFields map; only summary/description/labels/parent/epicLink shorthand exposed in v1"

egg-contract add-decision \
  --question "Epic Link mechanism (Open Q2)" \
  --options "parent.key only (next-gen / company-managed)" \
            "customfield_10014 only (classic / team-managed)" \
            "Auto-detect or accept either via the epicLink shorthand"

egg-contract add-decision \
  --question "idempotency_key requirement for createJiraIssue / addCommentToJiraIssue (Open Q3)" \
  --options "Required (gateway 400 if missing)" \
            "Optional with documented warning" \
            "Not implemented — caller responsibility"

egg-contract add-decision \
  --question "Link type allowlist source (Open Q4)" \
  --options "Hardcoded {Blocks, Relates}" \
            "config/context-filters.yaml jira.link_types: [...]" \
            "Accept any name upstream Jira recognizes"

egg-contract add-decision \
  --question "notifyUsers default for editJiraIssue (Open Q5)" \
  --options "true (Atlassian default — sends email)" \
            "false (quiet update; opt-in to notify)"

egg-contract add-decision \
  --question "addCommentToJiraIssue visibility field (Open Q6)" \
  --options "Hide entirely in v1" \
            "Expose role/group restrictions"

egg-contract add-decision \
  --question "Body input format for description / comment (Open Q7)" \
  --options "Plain text only (gateway wraps to ADF)" \
            "Pre-built ADF dict only (passthrough)" \
            "Both — accept whichever the caller passes"

egg-contract add-decision \
  --question "Issuetype identifier accepted by createJiraIssue (Open Q8)" \
  --options "Name only (Task / Story / Bug / Epic / Sub-task)" \
            "Numeric ID only" \
            "Both"

egg-contract add-decision \
  --question "createIssueLink project allowlist semantics (Open Q9)" \
  --options "Both inwardIssue and outwardIssue projects must be allowlisted (strict)" \
            "Only one needs to be allowlisted (loose)" \
            "One in main allowlist, the other in a separate 'linkable-only' config list"

egg-contract add-decision \
  --question "Per-role write gating (Open Q10)" \
  --options "Unrestricted within allowlisted projects" \
            "Per-verb role allowlist (e.g., refiner=editJiraIssue, planner=createJiraIssue/createIssueLink)" \
            "Defer to a follow-up issue"

egg-contract add-decision \
  --question "Phase gating for writes (Open Q11)" \
  --options "Unrestricted across phases" \
            "Per-phase verb allowlist via gateway/phase_filter.py" \
            "Defer to a follow-up issue"

egg-contract add-decision \
  --question "createJiraIssue response shape (Open Q13)" \
  --options "Atlassian raw {id, key, self}" \
            "Normalized envelope {key, id, browse_url, status: 'created'}"

egg-contract add-decision \
  --question "editJiraIssue response shape (Open Q14)" \
  --options "Envelope {status: 'updated', key} (no extra Atlassian call)" \
            "Echo post-edit ticket via Atlassian ?returnIssue=true (extra read latency)"

egg-contract add-decision \
  --question "Idempotency cache scope (Open Q16)" \
  --options "In-memory per-gateway-process, 5-minute TTL" \
            "Persisted (disk or Redis) for multi-replica deployments"

egg-contract add-decision \
  --question "Cross-project parent on createJiraIssue (Open Q17)" \
  --options "Reject if parent.key project differs from new ticket project" \
            "Accept; let Atlassian decide"

egg-contract add-decision \
  --question "createIssueLink optional comment payload (Open Q23)" \
  --options "Surface via 'comment' field in body" \
            "Require a separate addCommentToJiraIssue call"

egg-contract add-decision \
  --question "Dry-run / validation mode (Open Q24)" \
  --options "Expose ?dry_run=true on each write route" \
            "Live calls only; orchestrator handles preview"

egg-contract add-decision \
  --question "Documentation home for new write verbs (Open Q27)" \
  --options "Extend existing docs/reference/jira-wrapper.md" \
            "New docs/reference/jira-writes.md plus link from jira-wrapper.md" \
            "Both — high-level in jira-wrapper.md, deep dive in jira-writes.md"

# Open-ended questions
egg-contract add-feedback \
  --question "Open Q12 — write rate-limit policy: confirm 'no auto-retry on 429 for writes' is correct (matches v1 _request); should writes still emit jira_upstream_rate_limited audit on 429?" \
  --question "Open Q15 — body size caps: pick numeric limits for summary (default 255 from Atlassian), description, labels (count + per-label length), comment body, customFields map size." \
  --question "Open Q18 — existing-issue probe before create / edit: is the idempotency key sufficient, or do we add a 'is there already a ticket matching this' safety check?" \
  --question "Open Q19 — sandbox wrapper input flags: confirm --description / --description-file / --description-stdin (and the analogous comment body flags) match expected agent ergonomics." \
  --question "Open Q20 — audit body redaction: confirm we log only structural metadata (field names, content lengths, label counts) and never body content; any exceptions (labels, link type names)?" \
  --question "Open Q21 — adversarial body tests location: same file as the route 403 grid, or a separate test_jira_writes_adversarial.py?" \
  --question "Open Q22 — multi-tenant hooks: confirm v1 stays single-tenant exactly as #1556 left it, with no further work in #1924?" \
  --question "Open Q25 — failure recovery for batched plan apply: confirm rollback is the orchestrator's responsibility (gateway implements no transactions or compensating actions)." \
  --question "Open Q26 — error envelope for write upstream errors: reuse _jira_error_from_upstream verbatim, or surface field-level Atlassian error messages (e.g., 'Field parent cannot be set...') in a richer shape?"
```

## Complexity Assessment

**medium** — four narrow per-verb routes added to a well-established pattern, plus a small idempotency cache and ADF wrapping helper. Each verb is a self-contained slice (route + client method + body validator + sandbox subcommand + tests) and could be implemented in parallel after a small foundation step (method-allowlist separation, idempotency cache module, ADF wrapper helper). The implementation surface is bounded (~1,200 LoC total: ~600 in `gateway/`, ~300 in `sandbox/scripts/jira`, ~300 in tests + docs), but the open-question count is large because each new verb has its own per-instance Atlassian quirks (Epic Link mechanism, link types, custom fields) that need explicit human decisions before plan can write tasks.

---

*Authored-by: egg*
