# Jira Wrapper Reference

> Gateway REST surface that gives sandboxed agents bounded read **and write** access to Jira. Mirrors the `/api/v1/gh/*` pattern: Atlassian credentials live in the gateway, the sandbox posts session-authenticated JSON, and every call is funneled through a private-mode gate, a project allowlist, and structured audit logs.

The gateway exposes two surface families:

- **Read verbs** (v1, [#1556](https://github.com/jwbron/egg/issues/1556)): `ticket get`, `ticket comments`, `search`, and the GET-only `execute` passthrough.
- **Write verbs** (v1.1, [#1924](https://github.com/jwbron/egg/issues/1924)): `ticket create`, `ticket edit`, `ticket comment add`, and `issue-link create` — see [Write verbs](#write-verbs) below.

Transitions, worklogs, attachments, watchers, deletions, and `PUT` / `PATCH` / `DELETE` methods on `/execute` are **permanently out of scope** and are enforced at the path validator. The write surface bypasses `/execute` entirely (each verb is its own narrow route with a hardcoded upstream path), so the GET-only `/execute` invariant is preserved by construction.

## Endpoint surface

All routes are `POST /api/v1/jira/...`, require a session token via `@require_session_auth`, and are gated by `@require_private_mode` (from `gateway/mode_gate.py`). In public mode every route returns 403 `private_mode_required` **before** any upstream call — no Atlassian credential is loaded and no network egress happens.

| Endpoint | Purpose | Upstream |
|----------|---------|----------|
| `POST /api/v1/jira/ticket/get` | Read a single ticket, default `expand=renderedBody,renderedFields` so `fields.description` is ADF-rendered | `GET /rest/api/3/issue/{key}` |
| `POST /api/v1/jira/search` | JQL search with conservative static project-scope extraction | `POST /rest/api/3/search/jql` |
| `POST /api/v1/jira/ticket/comments` | Read comments for a ticket, default `expand=renderedBody,renderedFields` | `GET /rest/api/3/issue/{key}/comment` |
| `POST /api/v1/jira/execute` | GET-only regex-allowlisted passthrough | `GET /rest/api/3/...` |
| `POST /api/v1/jira/ticket/create` | [Write] Create a ticket; supports `parent` / `epicLink` shorthand and idempotency-key dedup | `POST /rest/api/3/issue` |
| `POST /api/v1/jira/ticket/edit` | [Write] Update a ticket's `summary` / `description` / `labels` (replace or add/remove) | `PUT /rest/api/3/issue/{key}` |
| `POST /api/v1/jira/ticket/comment/add` | [Write] Add a comment; ADF wrap on plain text; idempotency-key dedup | `POST /rest/api/3/issue/{key}/comment` |
| `POST /api/v1/jira/issue-link/create` | [Write] Create a typed link (`Blocks` / `Relates` / operator-allowlisted) between two tickets | `POST /rest/api/3/issueLink` |

### `POST /api/v1/jira/ticket/get`

**Request body:**
```json
{
  "ticket": "ENG-1234",
  "fields": ["summary", "status", "description"]   // optional; cap 32 entries
}
```

**Validation:**
- `ticket` must match `^[A-Z][A-Z0-9_]*-\d+$`.
- `extract_project_key(ticket)` must be in `jira.projects` (otherwise 403 `jira_ticket_get_denied`, reason `"project not allowlisted"`).
- `fields` entries must each match `^[a-zA-Z_][a-zA-Z0-9_.-]*$` (otherwise 400 with validation error). Caller may pass at most 32 entries.

**Response:** the Atlassian ticket JSON with `renderedBody` / `renderedFields` populated, returned verbatim. On upstream 404, see the [`not_found` envelope](#not_found-envelope).

### `POST /api/v1/jira/search`

**Request body:**
```json
{
  "jql": "project = ENG AND status = \"Open\"",
  "fields": ["summary", "status"],
  "nextPageToken": "...",
  "maxResults": 50
}
```

`maxResults` is clamped to 100; default 50. `nextPageToken` is passed through to Atlassian's cursor-based pagination.

**Conservative static JQL project-scope extractor.** To keep the route safe even against adversarial JQL, the gateway does **not** pass arbitrary queries through. It statically extracts the project scope and **denies on ambiguity**. The JQL is accepted only if it matches one of:

- `project = KEY` (unquoted, lowercase `project` keyword) at top level, ANDed only.
- `project IN (K1, K2, ...)` at top level with every key in `jira.projects`, ANDed only.

Rejected (403 `jira_search_rejected` with the matched reason):

- No `project` clause at all.
- `project` under any `OR` — including `project = ENG OR project = SEC` and `project = ENG OR key = SEC-1`.
- Case-variant keywords (`PROJECT = ENG`).
- Quoted project keys (e.g. `project = "ENG"`) are rejected unconditionally — the static extractor requires bare keys, even when the quoted key decodes to an allowlisted project. Rationale: deny-on-ambiguity; a quoted form signals that the query was constructed dynamically and the extractor cannot prove its intent.
- JQL functions (`projectsLeadByUser()`, `issuekey()`, etc.).
- `key =` clauses mixed in with `project =`.
- Semicolons, JQL comments (`#`, `//`, `/* */`), SQL-like comment tokens (`--`, rejected as a defensive precaution), or other injection patterns.
- Unicode homoglyph / mixed-script project keys.
- `IN` lists containing any non-allowlisted key.

The extractor is the hard boundary — if it cannot prove the query is scoped to allowlisted projects, the request is denied. The audit entry records `projects_extracted` on acceptance and the rejection reason on denial.

### `POST /api/v1/jira/ticket/comments`

**Request body:**
```json
{ "ticket": "ENG-1234" }
```

Same ticket-shape + project-allowlist check as `/ticket/get`. Returns the Atlassian comment-list JSON with `renderedBody` on each comment. Upstream 404 → [`not_found` envelope](#not_found-envelope).

### `POST /api/v1/jira/execute`

**Request body:**
```json
{
  "method": "GET",
  "path": "issue/ENG-1234",
  "query": { "expand": "renderedBody" },
  "body": null
}
```

Only `GET` is accepted. The `path` is validated against a hardened regex allowlist in `validate_jira_api_path`:

- Leading/trailing slashes are stripped, query strings are stripped, `..` segments are rejected, duplicate slashes are rejected, non-ASCII / non-normalized Unicode is rejected.
- Allowed path families (GET-only): `^issue/[A-Z][A-Z0-9_]*-\d+$`, `^issue/[A-Z][A-Z0-9_]*-\d+/comment$`, `^search/jql$`, `^project$`, `^project/[A-Z][A-Z0-9_]*$`.
- Any path containing `transitions`, `worklog`, `attachments`, or `watchers` is rejected — these are the permanent "out of scope ever" verbs.

After the path is accepted, the extracted project key must be in `jira.projects`, otherwise 403 `jira_execute_denied` with reason `"project not allowlisted"`. On acceptance, the call is proxied verbatim and the response returned.

`/execute` is a pragmatic escape hatch for future read verbs not yet promoted to narrow routes. It is **not** a general passthrough — the regex allowlist is the fence.

## `not_found` envelope

The Atlassian API returns 404 for a missing ticket with error-message JSON that varies across instances and rarely maps cleanly to a sandbox flow. To give agents a stable shape, the gateway **intercepts upstream 404 on `/ticket/get` and `/ticket/comments`** and returns HTTP 200 with:

```json
{
  "status": "not_found",
  "key": "ENG-1234",
  "upstream_status": 404
}
```

This lets callers branch cleanly on `status == "not_found"` without inspecting error messages. `/search` and `/execute` do **not** use the envelope — their upstream 404 is a real API error (wrong path, deleted project, etc.) and is surfaced as `JiraUpstreamError` and translated to the original upstream status by the route handler.

## Error cases

| HTTP | Condition | Audit event |
|------|-----------|-------------|
| 400 | Malformed ticket key, invalid `fields` (non-matching regex, >32 entries), missing required body field | `jira_<verb>_rejected` with reason |
| 401 | Session token invalid / missing | Standard gateway auth rejection |
| 403 | Public mode (private-mode gate) | `private_mode_required` |
| 403 | Project not in `jira.projects` | `jira_<verb>_denied`, reason `"project not allowlisted"` |
| 403 | `/search` JQL fails the static scope extractor | `jira_search_rejected` with the specific reason |
| 403 | `/execute` denied verb, non-GET method, path traversal, disallowed path family, duplicate slash, non-ASCII | `jira_execute_denied` with reason |
| 503 | Atlassian credentials not configured (`JiraCredentialsUnavailable`) | `jira_credentials_unavailable` |
| *upstream* | Atlassian 4xx/5xx other than the 404 envelope paths | Upstream status passed through, `jira_upstream_error` audit entry |

**429 handling.** Atlassian rate-limit responses are retried exactly once, sleeping `min(Retry-After, 30)` seconds; retry is GET-only, write verbs never retry (future-proofing). Both the initial and retry 429 emit a `jira_upstream_rate_limited` audit entry including the `Retry-After` value and path. After the second 429, the response is passed through verbatim.

Every audit entry includes `session_mode`, `pipeline_id`, `agent_role`, and (when available) `session.jira_ticket`, per [Session.jira_ticket plumbing](../architecture/credential-injection.md#atlassian--jira). The `jira_ticket` field is **observational only** — it is recorded in every audit entry so operators can reconcile Jira calls with the pipeline's scoped ticket, but the project allowlist remains the only hard boundary.

## Project-allowlist semantics

The allowlist lives in `config/context-filters.yaml`:

```yaml
jira:
  projects: [ENG, DEVOPS]   # Jira project keys allowed for read access
```

- The authoritative key is `projects` (not `project_allowlist`).
- Default is an empty list — every Jira call is rejected until an operator populates it. This is the "installed but inert" state for v1 rollout.
- Fail-closed: if the file is missing, the `jira:` section is absent, or the YAML is malformed, `allowed_projects()` returns an empty set and no error is raised. Operators must see 403s on every Jira call rather than a crashed gateway.
- Reloaded on mtime change; `POST /api/v1/config/reload` also calls `reload_jira_policy()` alongside `reload_jira_credentials()`.

**Why `context-filters.yaml` and not a dedicated file?** Operators already edit this file for GitHub context filtering; keeping Jira policy in the same place means one allowlist surface to review. The Jira section is self-contained and does not interact with the GitHub filters.

**`EGG_JIRA_TICKET` is advisory, not enforcement.** The orchestrator exports `EGG_JIRA_TICKET` (and optional `EGG_JIRA_PROJECT`) to the agent so it knows which ticket it's scoped to without being told in-prompt. These are audited by the gateway for reconciliation but are **not** used as a policy gate — a ticket value from the sandbox cannot widen or narrow access. Only `jira.projects` in `context-filters.yaml` does that.

## Default `expand=renderedBody,renderedFields`

Atlassian stores ticket and comment bodies in [Atlassian Document Format](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/) (ADF) — a JSON tree, not plain text or HTML. Asking for a ticket without an `expand` parameter returns the ADF JSON, which is typically unusable directly for an agent.

The gateway's `JiraClient.get_ticket` and `JiraClient.get_comments` therefore default to `expand=renderedBody,renderedFields`, so `fields.description.renderedBody` contains HTML the agent can parse directly. Callers may override `expand` by passing a different value via the `fields` parameter; the default exists so the common case "just read me this ticket" works without additional ceremony.

## Write verbs

The four write verbs ([#1924](https://github.com/jwbron/egg/issues/1924)) extend the v1 read surface with a **bounded** mutation surface — just enough for the SDLC pipeline to push agent-authored analysis into an epic Description, create child tickets under an epic, drop comments, and wire cross-task links ([#1557](https://github.com/jwbron/egg/issues/1557) is the first consumer). Every write inherits the v1 invariants: private-mode-only, project allowlist, audit-on-every-call, and Atlassian credentials never leave the gateway.

The write verbs **do not** extend `/api/v1/jira/execute`. Each has its own narrow route with a hardcoded upstream path — the `/execute` regex allowlist stays GET-only forever. The `JiraClient` write methods bypass the `validate_jira_api_path` validator (their paths are constants, not caller input), but the path-segment denylist (`transitions`, `worklog`, `attachments`, `watchers`, plus `DELETE` / `PUT` / `PATCH` on `/execute`) is unchanged.

### Sandbox wrapper subcommands

The sandbox wrapper (`sandbox/scripts/jira`) gains four new subcommands. Each shells to the matching gateway route over `EGG_SESSION_TOKEN`; Atlassian credentials never enter the sandbox.

```text
jira ticket create   --project KEY --type Task --summary "..."
                     [--description "..." | --description-file PATH | --description-stdin]
                     [--labels a,b]
                     [--parent FOO-1] [--epic-link FOO-2]
                     [--idempotency-key K]

jira ticket edit     TICKET
                     [--summary "..."]
                     [--description "..." | --description-file PATH | --description-stdin]
                     [--labels a,b]                # replace mode (mutually exclusive with add/remove)
                     [--add-labels x] [--remove-labels y]   # incremental mode
                     [--no-notify]

jira ticket comment add TICKET
                     [--body "..." | --body-file PATH | --body-stdin]
                     [--idempotency-key K]

jira link create     --type Blocks --inward FOO-1 --outward FOO-2
                     [--comment "..."]
                     [--idempotency-key K]
```

The `--description-*` / `--body-*` flag groups are mutually exclusive — pass at most one. Mixing replace-mode `--labels` with incremental `--add-labels` / `--remove-labels` is rejected client-side and (as a defence in depth) at the gateway with HTTP 400.

### `POST /api/v1/jira/ticket/create`

**Request body:**
```json
{
  "projectKey": "ENG",
  "issuetype": "Task",                       // or numeric ID
  "summary": "Investigate flaky pipeline",   // required, ≤ 255 chars
  "description": "Plain text or ADF dict",   // optional, ≤ 32 KiB
  "labels": ["triage", "p2"],                // optional, ≤ 30 entries × ≤ 255 chars each
  "parent": "ENG-1234",                      // optional; used for next-gen / company-managed parent
  "epicLink": "ENG-9999",                    // optional; routed per jira.epic_link_field
  "idempotencyKey": "create-eng-2026-04-27-001"  // optional, see Idempotency below
}
```

**Validation:**

1. JSON parse, then field allowlist — only the keys above are accepted; any unknown field (including `customfield_*` smuggling) is rejected 400. Custom fields are not exposed in v1.
2. Size caps: `summary` ≤ 255 chars (matches Atlassian's hard limit), `description` ≤ 32 KiB, `labels` ≤ 30 entries with each ≤ 255 chars.
3. `projectKey` must be in `jira.projects` (otherwise 403 `jira_ticket_create_denied`, reason `"project not allowlisted"`).
4. **Cross-project parent reject.** If `parent.<key>` is from a different project than `projectKey`, the request is rejected 400. The gateway will not implicitly widen the project allowlist via a parent reference.
5. **`parent` and `epicLink` are mutually exclusive.** Passing both returns 400 — Atlassian's two epic-link mechanisms cannot be set simultaneously and the gateway refuses to silently choose.
6. Plain-text `description` is wrapped to ADF; an ADF dict (i.e. `{"type": "doc", "version": 1, "content": [...]}`) passes through verbatim.

**Epic-link dispatch.** Atlassian has two mechanisms for placing a ticket under an epic, depending on the project type:

- Next-gen / company-managed: `parent.key`.
- Classic / team-managed: `customfield_10014`.

The gateway selects between them via `jira.epic_link_field` in `config/context-filters.yaml` (default `"parent"`). When `epicLink` is supplied, the gateway emits **only** the configured field — never both — because Atlassian rejects the un-configured field with `cannot be set, it is not on the appropriate screen, or unknown`. If you pass an explicit `parent`, it goes through verbatim regardless of the config knob.

**Response:** normalized envelope (always 200 on success):
```json
{
  "key": "ENG-4242",
  "id": "10042",
  "browse_url": "https://acme.atlassian.net/browse/ENG-4242",
  "status": "created"
}
```

### `POST /api/v1/jira/ticket/edit`

**Request body:**
```json
{
  "ticket": "ENG-4242",
  "summary": "...",                  // optional, ≤ 255 chars
  "description": "...",              // optional, str or ADF dict, ≤ 32 KiB
  "labels": ["a", "b"],              // optional REPLACE mode
  "addLabels": ["new"],              // optional INCREMENTAL mode
  "removeLabels": ["old"],           // optional INCREMENTAL mode
  "notifyUsers": false               // optional, default false (quiet update)
}
```

**Validation:**

1. `ticket` must match `^[A-Z][A-Z0-9_]*-\d+$` and its project must be in `jira.projects`.
2. Field allowlist + size caps as above.
3. **Replace and incremental label modes are mutually exclusive.** If `labels` is set together with any of `addLabels` / `removeLabels`, the request is rejected 400 — Atlassian's update body cannot express both at once and the gateway refuses to silently pick a winner.
4. `notifyUsers` defaults to `false` (decision-5). When `false`, the gateway adds `?notifyUsers=false` to the upstream URL; when `true`, the query param is omitted (Atlassian's default is to notify, so `notifyUsers=true` would just bloat the URL).

**Response:**
```json
{ "status": "updated", "key": "ENG-4242" }
```

The gateway does not synthesize an extra read after the edit — if you need the post-edit ticket, follow with `POST /api/v1/jira/ticket/get`.

### `POST /api/v1/jira/ticket/comment/add`

**Request body:**
```json
{
  "ticket": "ENG-4242",
  "body": "Plan-apply succeeded — ticket linked to epic ENG-9999.",
  "idempotencyKey": "comment-eng-4242-plan-apply-2026-04-27"
}
```

`body` may also be an ADF dict for rich formatting. Cap is 32 KiB (mirrors `description` — Atlassian publishes no upper limit, but 32 KiB bounds audit-log size and easily covers realistic plan-apply commentary).

**Visibility is intentionally not exposed in v1.** The Atlassian `visibility` field (project-role / group restrictions on a comment) is rejected 400 if present in the body. Comments are public to anyone with read access to the ticket.

**Response:** the Atlassian comment object verbatim.

### `POST /api/v1/jira/issue-link/create`

**Request body:**
```json
{
  "type": "Blocks",
  "inwardIssue": "ENG-1234",
  "outwardIssue": "ENG-5678",
  "comment": "Optional explanation",
  "idempotencyKey": "link-eng-1234-blocks-eng-5678"
}
```

**Validation:**

1. `type` must be in the operator-configurable `jira.link_types` allowlist (default `["Blocks", "Relates"]`). Atlassian link-type names are case-sensitive — `blocks` will be rejected.
2. **Both** `inwardIssue` and `outwardIssue` projects must be in `jira.projects` (strict — decision-9). The gateway will not link tickets across the allowlist boundary even when the operator has read access to both projects.
3. `comment`, when provided, is ADF-wrapped and surfaced via the body's `comment` field — a single round-trip rather than two (decision-23). Idempotency cache covers the link too, so the comment cannot be duplicated by retries.

**Response.** Atlassian returns `201 Created` with no body for `POST /rest/api/3/issueLink`. The gateway therefore synthesizes a stable response envelope:
```json
{
  "status": "created",
  "inwardIssue": "ENG-1234",
  "outwardIssue": "ENG-5678",
  "type": "Blocks"
}
```

Including the triple in the response lets the orchestrator's plan-apply correlate the call against its retry record.

### Atlassian Document Format (ADF) wrapping

Atlassian stores ticket descriptions and comment bodies as a JSON tree ([Atlassian Document Format](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/)), not plain text or HTML. Agents almost always work with plain text, so the gateway's write verbs accept either:

- **Plain text** — wrapped to a single-paragraph ADF document by `gateway/jira_adf.py:wrap_text_as_adf`.
- **ADF dict** — passes through verbatim. The gateway recognizes ADF via `is_adf_dict`: a dict with `type == "doc"`, an integer `version`, and a list `content`.

Any other shape (string, list, dict missing required keys) is rejected 400 with a validation error.

### Idempotency keys

`createJiraIssue`, `addCommentToJiraIssue`, and `createIssueLink` accept an optional `idempotencyKey`. When supplied, the gateway caches the upstream response in an in-memory per-process map (`gateway/jira_idempotency.py`) for **5 minutes**. A second call with the same `(verb, project, idempotencyKey)` triple within the TTL returns the cached response without re-hitting Atlassian. Without the key, every call is dispatched fresh.

The cache lives in the gateway process. If the gateway is restarted, the cache is lost — the orchestrator owns higher-level retry semantics and must be prepared for transient duplicates after a restart.

`editJiraIssue` is naturally idempotent (a PUT against the same key with the same body is a no-op) and does not accept `idempotencyKey`.

**Link-cache aliasing safety.** For `createIssueLink`, the cache key is composed as `(verb="link", canonical_link_id(inward, outward, type), idempotencyKey)`. Two callers re-using the same opaque `idempotencyKey` against different `(inwardIssue, outwardIssue, type)` triples produce **distinct** cache entries — no aliasing across triples.

**Optional with documented warning (decision-3).** Omitting `idempotencyKey` is not an error, but at-least-once retry semantics are then the caller's responsibility. The gateway logs a `jira_<verb>_no_idempotency_key` audit entry when the key is omitted so operators can spot agents that retry without dedup.

### Audit-log redaction

Every write call emits a structured audit entry with operation `jira_ticket_create` / `jira_ticket_edit` / `jira_comment_add` / `jira_issue_link_create`. The redaction rules (Q20 default):

| What is logged | What is **not** logged |
|----------------|------------------------|
| Operation name, caller identity, pipeline / contract context | Description text |
| Project key, ticket key (where applicable) | Comment body text |
| Field names changed | Summary text |
| Content lengths (`summary_length`, `description_length`, `comment_length`) | |
| Label values (small enumerated strings — policy-relevant for who-tagged-what audit) | |
| Link-type name (e.g. `"Blocks"`) | |
| Epic-link field used (`"parent"` vs `"customfield_10014"`) | |
| Outcome (`allowed` / `denied` / 429) | |

The rationale: structural metadata is enough for the operator to reconstruct what changed, and labels are short enumerated strings that operators legitimately need for audit ("who tagged this incident `p0`?"). Free-text body content is treated as customer-impacting data and never written to disk.

**429 audit on writes.** Per Q12 default, every Atlassian 429 emits a `jira_upstream_rate_limited` audit entry — including writes. The retry loop in `JiraClient._request` still retries only GETs (writes never auto-retry, to preserve the v1 invariant), but the audit emit is unconditional on 429. Operators can therefore alert on rate-limit pressure across reads and writes uniformly.

### Configuration knobs

`config/context-filters.yaml` gains two optional keys under `jira:`. Both have sensible defaults; both fail closed on malformed input.

```yaml
jira:
  projects: [ENG, DEVOPS]
  # Optional. Default ["Blocks", "Relates"]. Atlassian link-type names
  # are case-sensitive.
  link_types: ["Blocks", "Relates"]
  # Optional. Default "parent". Selects which Atlassian field carries
  # the epic relationship for the `epicLink` shorthand. Use
  # "customfield_10014" for classic / team-managed projects.
  epic_link_field: "parent"
```

Both knobs are reloaded on `POST /api/v1/config/reload` and on file mtime change. Invalid values (unknown enum for `epic_link_field`, non-list for `link_types`) trigger a startup warning and fall back to the default — the gateway does not crash.

### Error cases (write surface)

In addition to the [shared error cases](#error-cases) above:

| HTTP | Condition | Audit event |
|------|-----------|-------------|
| 400 | Unknown field in body (e.g. `customfield_*` smuggling) | `jira_<verb>_rejected`, reason `"unknown field"` |
| 400 | Both `parent` and `epicLink` set on create | `jira_ticket_create_rejected`, reason `"parent and epicLink mutually exclusive"` |
| 400 | Both replace `labels` and incremental `addLabels` / `removeLabels` set on edit | `jira_ticket_edit_rejected`, reason `"label modes mutually exclusive"` |
| 400 | Cross-project `parent` (parent's project ≠ new ticket's `projectKey`) | `jira_ticket_create_rejected`, reason `"cross-project parent"` |
| 400 | Oversized `summary` / `description` / `labels` count | `jira_<verb>_rejected`, reason names the cap |
| 400 | Unknown issuetype | `jira_ticket_create_rejected`, reason `"unknown issuetype"` |
| 400 | `comment` body shape is neither plain text nor a valid ADF dict | `jira_<verb>_rejected`, reason `"invalid body shape"` |
| 400 | `visibility` field present on comment | `jira_comment_add_rejected`, reason `"visibility not supported"` |
| 403 | Either side of `createIssueLink` not in `jira.projects` | `jira_issue_link_create_denied`, reason `"project not allowlisted"` |
| 403 | Link `type` not in `jira.link_types` | `jira_issue_link_create_denied`, reason `"link type not allowlisted"` |
| 503 | Atlassian credentials not configured | `jira_credentials_unavailable` |
| *upstream* | Atlassian 4xx/5xx other than rate-limit | Upstream status passed through, `jira_upstream_error` |

### Phase rollback

The implementation lands as six commits inside a single PR (foundation modules → JiraClient methods → gateway routes → sandbox wrapper → tests → docs). Each phase is independently revertible without breaking the v1 read surface — the BRC implement-phase reviewer should verify by running `git revert --no-commit <phase-N-commit>` on each phase and confirming that `pytest gateway/tests/test_jira_routes.py gateway/tests/test_jira_client.py gateway/tests/test_allowed_domains.py` stays green before discarding the revert.

| Reverting … | Effect |
|-------------|--------|
| Phase 6 (this doc) | Harmless. The runtime is unchanged; only the user-facing reference disappears. |
| Phase 5 (tests) | Harmless. The runtime is unchanged; coverage drops on the four new modules. |
| Phase 4 (sandbox wrapper) | Gateway routes remain reachable from any HTTP client with the session token, but the sandbox `jira` CLI loses the `ticket create` / `ticket edit` / `ticket comment add` / `link create` subcommands. Agents lose the friendly surface; the gateway is otherwise inert. |
| Phase 3 (gateway routes) | The four `/api/v1/jira/...` write routes return 404. `JiraClient` write methods exist on the class but are not callable from outside the gateway process. |
| Phase 2 (`JiraClient` writes) | The four routes return 500 if hit (`AttributeError` on missing methods). The route-enumeration regression in `test_jira_routes.py` will start failing — revert Phase 3 alongside Phase 2. |
| Phase 1 (foundations: idempotency cache + ADF wrapper) | Phase 2 does not import; revert Phase 2 alongside Phase 1. The v1 read surface is unaffected — read verbs do not depend on `jira_idempotency.py` or `jira_adf.py`. |

This is a one-time implement-phase verification, not a CI gate. The phase ordering is documented here so operators have a written record of how to peel the surface back if a downstream issue forces a partial rollback.

### Cross-references

- [Read verbs](#endpoint-surface) — the v1 read surface this section extends.
- [Project-allowlist semantics](#project-allowlist-semantics) — `jira.projects`, the hard policy boundary that the write verbs inherit verbatim.
- [`not_found` envelope](#not_found-envelope) — write verbs do **not** use this envelope; an Atlassian 404 on edit / comment-add / issue-link surfaces as `JiraUpstreamError` and is translated to the upstream status.

## Future-verb extension points

The bounded write surface above is intentionally narrow. Several adjacent capabilities were considered and explicitly **deferred** rather than silently dropped:

- **Per-role write gating** (decision-D10). Within an allowlisted project, every authenticated agent can call every write verb. Per-role policy (e.g. only `coder` can create tickets, only `reviewer_*` can comment) is a follow-up. Filed: TBD-write-policy.
- **Per-phase write gating** (decision-D11). Writes are accepted from any phase. Restricting to e.g. plan-only `createIssue` is a follow-up under the same write-policy umbrella.
- **Generic `customFields` map** (decision-D1). Only the curated `summary` / `description` / `labels` / `parent` / `epicLink` shorthand is exposed in v1.1. Operators with downstream Jira integrations needing arbitrary custom-field writes should file a follow-up.
- **Transitions, worklogs, attachments, watchers, deletions** — permanently out of scope. The path validator's denylist refuses these even on `/execute`, even if a future verb tried to widen the surface.
- **Per-verb rate-limit config** (`jira.rate_limits:`) and `EGG_JIRA_ENABLED` kill-switch — deferred from the original v1 read scope; still deferred. Both are beyond v1.1; this section calls them out so reviewers don't search the codebase for them.

The `JIRA_WRITE_VERBS_DENIED` frozenset permanently refuses `transitions`, `worklog`, `attachments`, `watchers`, `DELETE`, `PUT`, and `PATCH` at the path validator. The four write verbs above sidestep the validator by hardcoding their upstream paths — they cannot reach the denylisted segments by construction.

## Related documentation

- [Credential Injection — Atlassian / Jira](../architecture/credential-injection.md#atlassian--jira) — where credentials live, mtime refresh, zero-credential invariant
- [Network Isolation — Gateway REST API](../architecture/network-isolation.md#gateway-rest-api) — endpoint summary and Squid allowlist exclusion
- [Sandbox environment rules](../../sandbox/agent-config/rules/environment.md) — `jira` wrapper verbs, `EGG_JIRA_TICKET` semantics
- Gateway source: `gateway/jira_credentials.py`, `gateway/jira_client.py`, `gateway/jira_policy.py`, `gateway/jira_idempotency.py`, `gateway/jira_adf.py`, `gateway/mode_gate.py`
- Sandbox wrapper: `sandbox/scripts/jira`
- Config: `config/context-filters.yaml`, `config/secrets.template.env`
