# Jira Wrapper Reference

> Gateway REST surface that gives sandboxed agents bounded access to Jira. Mirrors the `/api/v1/gh/*` pattern: Atlassian credentials live in the gateway, the sandbox posts session-authenticated JSON, and every call is funneled through a private-mode gate, a project allowlist, and structured audit logs.

The original v1 surface ([#1556](https://github.com/jwbron/egg/issues/1556)) was read-only. The bounded write extension ([#1924](https://github.com/jwbron/egg/issues/1924)) adds four narrow write routes — `ticket/create`, `ticket/edit`, `ticket/comment/add`, `issue-link/create` — under the same decorators and policy plumbing. All write verbs share the read verbs' private-mode + project-allowlist + audit chain; see [Write verbs](#write-verbs) for the per-route body schema, ADF wrapping rules, idempotency-key semantics, and the audit-log redaction contract for write payloads. Transitions, worklogs, attachments, watchers, deletions, and `DELETE` methods are **permanently out of scope** and are enforced at the path validator. `PUT` / `PATCH` are blocked from the `/execute` passthrough but used internally by `JiraClient.edit_issue` against `PUT /rest/api/3/issue/{key}` (the validator is bypassed for hardcoded write methods).

## Endpoint surface

All four routes are `POST /api/v1/jira/...`, require a session token via `@require_session_auth`, and are gated by `@require_private_mode` (from `gateway/mode_gate.py`). In public mode every route returns 403 `private_mode_required` **before** any upstream call — no Atlassian credential is loaded and no network egress happens.

| Endpoint | Purpose | Upstream |
|----------|---------|----------|
| `POST /api/v1/jira/ticket/get` | Read a single ticket, default `expand=renderedBody,renderedFields` so `fields.description` is ADF-rendered | `GET /rest/api/3/issue/{key}` |
| `POST /api/v1/jira/search` | JQL search with conservative static project-scope extraction | `POST /rest/api/3/search/jql` |
| `POST /api/v1/jira/ticket/comments` | Read comments for a ticket, default `expand=renderedBody,renderedFields` | `GET /rest/api/3/issue/{key}/comment` |
| `POST /api/v1/jira/execute` | GET-only regex-allowlisted passthrough | `GET /rest/api/3/...` |

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

> Added in [#1924](https://github.com/jwbron/egg/issues/1924) as the bounded write extension that unblocks Jira-epic SDLC pipelines ([#1557](https://github.com/jwbron/egg/issues/1557)). The four write routes share the read verbs' private-mode + project-allowlist + audit chain. Atlassian credentials still live only in the gateway, `*.atlassian.net` is still excluded from the Squid allowlist, and `/execute` stays GET-only forever — agents can only mutate Jira through the four narrow routes documented below.

### Endpoint surface (write)

All four routes are `POST /api/v1/jira/...`, require `@require_session_auth`, and are gated by `@require_private_mode`. In public mode every route returns 403 `private_mode_required` **before** any upstream call — no Atlassian credential is loaded and no network egress happens.

| Endpoint | Purpose | Upstream |
|----------|---------|----------|
| `POST /api/v1/jira/ticket/create` | Create a new ticket; returns normalized envelope `{key, id, browse_url, status: "created"}` | `POST /rest/api/3/issue` |
| `POST /api/v1/jira/ticket/edit` | Edit an existing ticket; replace-mode or incremental-mode label edits, optional `notifyUsers=false` | `PUT /rest/api/3/issue/{key}` |
| `POST /api/v1/jira/ticket/comment/add` | Add a comment to a ticket; ADF wrap or pass-through | `POST /rest/api/3/issue/{key}/comment` |
| `POST /api/v1/jira/issue-link/create` | Create an issue link (e.g. `Blocks`, `Relates`) between two allowlisted tickets, with optional inline comment | `POST /rest/api/3/issueLink` |

The write verbs do **not** extend the `/execute` passthrough — its regex allowlist stays GET-only. Internally they bypass `validate_jira_api_path` and call `_request` directly with hardcoded paths. The path validator's permanent denylist (`transitions`, `worklog`, `attachments`, `watchers`, `DELETE`) is unchanged; the validator's GET-only constraint applies only to `/execute`. `PUT` and `PATCH` are still rejected on `/execute`, even though `JiraClient.edit_issue` issues a `PUT` against Atlassian — the validator is bypassed for hardcoded write methods.

### `POST /api/v1/jira/ticket/create`

**Request body:**
```json
{
  "projectKey": "ENG",
  "issuetype": "Task",
  "summary": "Investigate login latency spike",
  "description": "Users in EU regions report 4s P95 login times since rollout 2026-04-26. Investigate downstream service dependencies.",
  "labels": ["latency", "p1"],
  "parent": "ENG-1200",
  "epicLink": "ENG-900",
  "idempotencyKey": "investigate-login-latency-2026-04-28"
}
```

**Validation:**
- `projectKey` must match `^[A-Z][A-Z0-9_]*$` and must be in `jira.projects` (otherwise 403 `jira_ticket_create_denied`, reason `"project not allowlisted"`).
- `issuetype` accepts either a name (`"Task"`, `"Story"`, `"Bug"`, `"Epic"`, `"Sub-task"`) or a numeric ID. Unknown name → 400.
- `summary` is required; ≤ 255 chars (Atlassian's hard limit).
- `description` may be a plain text string or a pre-built [ADF](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/) dict. Plain text is wrapped via `gateway/jira_adf.py`'s `wrap_text_as_adf`; ADF dicts pass through unchanged. ≤ 32 KiB (32 768 chars) regardless of shape.
- `labels` is optional; ≤ 30 entries, each ≤ 50 chars.
- `parent` and `epicLink` are mutually exclusive — passing both returns 400 with audit reason `parent_and_epic_link`. Pass `parent` for next-gen / company-managed projects; pass `epicLink` for the operator-configured shorthand (see [Epic-link dispatch](#jiralink_types-and-jiraepic_link_field-config-knobs)).
- `parent.key` must belong to the same project as `projectKey` — the gateway rejects cross-project parents with 400 and audit reason `cross_project_parent` (decision-17). Atlassian also rejects this server-side, but the gateway returns the error before the upstream call to keep the audit log honest.
- `epicLink.key` is held to the **same** allowlist + cross-project rules as `parent` (decision-9 + decision-17 symmetry). When `jira.epic_link_field == "parent"` the `epicLink` shorthand is a literal alias for `parent` at the Atlassian wire level, so allowing `epicLink` to target a non-allowlisted or cross-project epic would let an agent slip past the parent-side gate. The gateway runs `is_project_allowed` on `epicLink`'s extracted project and 400-rejects with audit reason `cross_project_epic_link` (distinct from `cross_project_parent`, so operators can grep both) if it differs from `projectKey`.
- **Custom fields are not exposed in v1** — any `customFields` map (or `customfield_NNNN` keys at the body root) returns 400; the gateway uses a write-keys allowlist (`_validate_jira_write_keys`) so unknown keys are caught up front. The shorthand surface is `summary` / `description` / `labels` / `parent` / `epicLink` only.
- `idempotencyKey` is optional but **strongly recommended** for transient-5xx retry safety. See [Idempotency keys](#idempotency-keys).

> **Field-name asymmetry:** `/ticket/create` uses `projectKey` (no existing ticket yet) while `/ticket/edit` and `/ticket/comment/add` use `ticket` (the project key is extracted from the existing ticket). The wire shape mirrors Atlassian's own create-vs-edit asymmetry; agents that compose write payloads need to remember which slot to fill on each verb.

**Response:** normalized envelope (decision-13).
```json
{
  "key": "ENG-1234",
  "id": "10001",
  "browse_url": "https://example.atlassian.net/browse/ENG-1234",
  "status": "created"
}
```
On idempotency-cache hit the same envelope is replayed verbatim with no upstream call.

### `POST /api/v1/jira/ticket/edit`

**Request body:**
```json
{
  "ticket": "ENG-1234",
  "summary": "Investigate login latency spike (EU)",
  "description": "Updated narrative...",
  "labels": ["latency", "p1", "eu-only"],
  "addLabels": null,
  "removeLabels": null,
  "notifyUsers": false
}
```

**Validation:**
- `ticket` must match `^[A-Z][A-Z0-9_]*-\d+$`; `extract_project_key` must be in `jira.projects`.
- All non-ticket fields are optional. The body must contain **at least one** mutating field (`summary` / `description` / `labels` / `addLabels` / `removeLabels`); otherwise 400 (`edit requires at least one of summary/description/labels/addLabels/removeLabels`).
- Size caps are identical to `/ticket/create`: `summary` ≤ 255, `description` ≤ 32 KiB, labels ≤ 30 × 50 chars.
- **Labels modes are mutually exclusive:** either `labels` (replace mode — overwrites the existing label set) or `addLabels` / `removeLabels` (incremental mode — applies set deltas). Mixing returns 400. The replace+incremental separation is enforced both at the gateway and at `JiraClient.edit_issue` (which raises `ValueError` if both arrive).
- `customFields` and any raw `customfield_NNNN` keys remain rejected by the write-keys allowlist (400).
- `notifyUsers` defaults to `false` (decision-5 — quiet update; opt-in to notify). When `false`, the gateway sends `notifyUsers=false` as a query string. Pass `true` explicitly to fall back to Atlassian's email-everyone default.

**Response:**
```json
{
  "status": "updated",
  "key": "ENG-1234"
}
```
The gateway does **not** issue an extra `?returnIssue=true` round-trip (decision-14) — callers who need the post-edit ticket call `/ticket/get` themselves.

### `POST /api/v1/jira/ticket/comment/add`

**Request body:**
```json
{
  "ticket": "ENG-1234",
  "body": "Confirmed regression — bisecting against the rollout commit range now.",
  "idempotencyKey": "comment-bisect-start-2026-04-28T10:14"
}
```

**Validation:**
- `ticket` shape + project-allowlist check identical to `/ticket/edit`.
- `body` is required; same dual-shape contract as `/ticket/create`'s `description` (plain text → ADF wrap; ADF dict → passthrough). ≤ 32 KiB.
- **`visibility` field is rejected** in v1 (decision-6). The body's write-keys allowlist excludes `visibility` (and `restrictions`); passing either returns 400.
- `idempotencyKey` is optional. See [Idempotency keys](#idempotency-keys).

**Response:** the Atlassian comment object verbatim (no envelope wrap — comments already carry `id`, `author`, `created`, `updated`, `body`).

### `POST /api/v1/jira/issue-link/create`

**Request body:**
```json
{
  "type": "Blocks",
  "inwardIssue": "ENG-1200",
  "outwardIssue": "ENG-1234",
  "comment": "Blocking on the latency fix landing first.",
  "idempotencyKey": "link-blocks-1200-1234"
}
```

**Validation:**
- `type` must be in the operator-configured `jira.link_types` allowlist (default `["Blocks", "Relates"]`). The lookup is **case-sensitive** — `"blocks"` does not match `"Blocks"`. Mismatch → 400.
- **Strict project allowlist** (decision-9): both `inwardIssue` and `outwardIssue` projects must be in `jira.projects`. If either fails, 403 with audit reason naming the offending side.
- `comment` is optional and travels in the same Atlassian payload (decision-23 — single round-trip, no separate `/comment/add` call). Plain text gets ADF-wrapped; ≤ 32 KiB.
- `idempotencyKey` is optional. The cache key namespaces the opaque key under a synthetic `"<inward>__<outward>__<type>"` tag, so passing the same opaque key against a different triple does not return a stale link — see [Idempotency keys](#idempotency-keys).

**Response:** normalized envelope.
```json
{
  "status": "created",
  "inwardIssue": "ENG-1200",
  "outwardIssue": "ENG-1234",
  "type": "Blocks"
}
```

### ADF wrapping

[Atlassian Document Format](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/) (ADF) is Atlassian's structured JSON tree for rich-text fields. Atlassian's REST API requires ADF for `description`, comment `body`, and the inline `comment` on issue links. The gateway accepts **both** plain text and pre-built ADF (decision-7) so agents do not have to learn ADF for the common case:

- **Plain text** (`"body": "regression confirmed"`) → wrapped via `gateway/jira_adf.py`'s `wrap_text_as_adf` into the minimal doc shape, splitting on `\n` so each line becomes its own paragraph node (matches how Atlassian renders pasted plain text in the web UI):
  ```json
  {
    "type": "doc",
    "version": 1,
    "content": [
      {"type": "paragraph", "content": [{"type": "text", "text": "regression confirmed"}]}
    ]
  }
  ```
  Empty strings produce a doc with one empty paragraph (still a valid ADF doc). Multi-line strings produce **one paragraph per line**: `"a\nb"` becomes two paragraphs. Empty lines (consecutive newlines) emit empty-paragraph nodes so blank lines round-trip through Atlassian's renderer. Agents needing finer layout (lists, headings, marks) pass ADF directly.
- **Pre-built ADF dict** (any `dict` matching `is_adf_dict` — `type == "doc"`, `version == 1`, `content` is a list) → passes through verbatim. The gateway does not re-validate the tree structure beyond the doc-shape sniff. Body content is not sanitised — size caps in the route layer are the only post-shape check.

This dual-shape contract is uniform across `/ticket/create.description`, `/ticket/edit.description`, `/ticket/comment/add.body`, and `/issue-link/create.comment`.

### Idempotency keys

Atlassian's create / comment / link endpoints are **not** naturally idempotent — a transient 5xx followed by a client retry produces a duplicate ticket / comment / link. To make agent retry safe without involving the orchestrator, the gateway exposes a caller-supplied `idempotencyKey` (decision-3, decision-28) backed by an in-process cache (`gateway/jira_idempotency.py`).

**Cache scope** (decision-16): per-gateway-process Python dict, 5-minute TTL (`IDEMPOTENCY_TTL_SECONDS = 300`), threading-lock guarded, lazy eviction at lookup time. Not persisted across gateway restarts and not shared between gateway replicas — both are deliberate (decision-16) because the realistic transient-5xx retry window is seconds-to-minutes; cross-process / cross-replica idempotency belongs to the orchestrator. The `fn` callable that performs the actual upstream call runs **outside** the lock, so two concurrent callers with the same key may both miss the cache and both invoke the upstream call; whichever writes its result last wins (still a valid response for the logical operation).

**Cache key shape** — internally the cache is keyed on a `(verb, project, key)` triple where `verb` is the operation tag and `project` carries verb-specific scoping:

| Verb | `verb` tag | `project` slot | Notes |
|------|-----------|----------------|-------|
| `ticket/create` | `"jira_ticket_create"` | `projectKey` (e.g. `"ENG"`) | First call computes the envelope; subsequent hits within TTL replay it. |
| `ticket/comment/add` | `"jira_comment_add"` | full ticket key (e.g. `"ENG-1234"`) | Keyed per ticket so the same opaque key against two different tickets stores two entries. (v1 keyed by project, but that allowed silent cross-ticket replay between two tickets in the same project; v2 keys per ticket.) |
| `issue-link/create` | `"jira_issue_link_create"` | synthetic tag `"<inward>__<outward>__<type>"` (e.g. `"ENG-1200__ENG-1234__Blocks"`) | Atlassian does not dedupe identical `(inward, outward, type)` triples (Open Q28). The synthetic tag namespaces the opaque key so the same key against a different triple stores a distinct entry — preventing aliasing while still de-duping retries on the same triple (decision-28 + the test's `link_cache_aliasing` case). |
| `ticket/edit` | n/a | not cached | Edits are naturally idempotent at Atlassian — same body re-applied is a no-op. The cache adds no safety here and would mask intentional sequential edits. |

**Caller obligations:**

- Pick an `idempotencyKey` deterministic per logical intent — re-deriving it from a stable input means a retry produces the same key. Random UUIDs defeat the purpose.
- Keep keys narrow — same key against two distinct intents (e.g. two different summaries) is a caller bug; the cache cannot prevent it for `/ticket/create` and `/ticket/comment/add` because the body is not part of the cache key.
- Treat `idempotencyKey` as advisory: when omitted, the cache is bypassed and the gateway forwards the call directly. Missing key never raises a 400.

**Hit semantics:** `get_or_run` returns a `(status_code, response_json, cache_hit)` 3-tuple — internal storage is `(monotonic_seconds, status_code, response_json)` per `gateway/jira_idempotency.py`. Each route handler emits its `{operation}_ok` success audit with the `idempotency_*` booleans wired through, so operators can distinguish the three states directly from audit logs:

| State | `idempotency_key_present` | `idempotency_hit` | Upstream call? |
|-------|---------------------------|-------------------|----------------|
| Caller omitted `idempotencyKey` (cache bypassed) | `false` | `false` | yes |
| Cache miss with key supplied (entry stored on success) | `true` | `false` | yes |
| Cache hit with key supplied (replayed within TTL) | `true` | `true` | **no** |

`ticket/edit` is naturally idempotent at Atlassian, so it never consults the cache and always emits `idempotency_hit: false` (`idempotency_key_present` reflects what the caller passed, but is otherwise inert) for grammar parity with the other write verbs.

### `jira.link_types` and `jira.epic_link_field` config knobs

`gateway/jira_policy.py` exposes two new config knobs (decision-2, decision-4) loaded from `config/context-filters.yaml` and refreshed on `mtime` change alongside the existing `jira.projects` allowlist:

```yaml
jira:
  projects: [ENG, DEVOPS]      # existing read+write allowlist

  # Operator-configurable link-type allowlist for /issue-link/create.
  # Default: ["Blocks", "Relates"]. Case-sensitive lookup.
  link_types: [Blocks, Relates, "Causes"]

  # Per-instance Epic Link dispatch for the epicLink shorthand.
  # "parent"             → next-gen / company-managed projects (default)
  # "customfield_10014"  → classic / team-managed projects
  epic_link_field: parent
```

- **`jira.link_types`** controls the allowlist for `/api/v1/jira/issue-link/create.type`. The default `["Blocks", "Relates"]` covers the common cross-issue dependency cases without exposing the long Atlassian default catalogue (e.g. `Cloners`, `Duplicate`, `Polish`). Operators add types deliberately.
- **`jira.epic_link_field`** controls how the gateway translates the `epicLink` shorthand on `/ticket/create` into Atlassian's body. With `"parent"` the gateway emits `{"fields": {"parent": {"key": "ENG-900"}}}`. With `"customfield_10014"` (the classic Jira Software Cloud field) it emits `{"fields": {"customfield_10014": "ENG-900"}}`. The gateway never emits both fields simultaneously — that's a Jira config error, not something callers can mix.
- Both knobs are **fail-closed-on-malformed** — a malformed YAML, missing `jira:` section, or non-string entry leaves the policy at its safe default (link_types = `["Blocks", "Relates"]`, epic_link_field = `"parent"`). Operators see a 400 / 403 instead of a crashed gateway.

The config is reloaded on file `mtime` change (same mechanism `jira.projects` uses) and explicitly via `POST /api/v1/config/reload`.

### Body size caps

| Field | Cap | Rationale |
|-------|-----|-----------|
| `summary` | 255 chars | Atlassian's hard limit; matched here to surface 400 before upstream rejects. |
| `description` (text or ADF stringified) | 32 KiB (32 768 chars / bytes) | Generous for design docs; bounds memory the gateway holds during ADF-wrap. |
| `comment` body (text or ADF) | 32 KiB | Symmetric with `description`. |
| `labels` count | 30 | Matches Atlassian's UI cap. |
| `labels` per-entry length | 50 chars | Atlassian rejects longer labels at the API level. |
| `customFields` map | **disabled** in v1 (size cap N/A) | Decision-1 — only the shorthand surface is exposed. The cap is documented for symmetry with v1.1 if it ever lifts. |

Oversized fields return 400 with the offending field name and the cap in the error message.

### Cross-project parent reject

`createJiraIssue` allows the caller to set `parent.key` for sub-tasks and stories under epics. Atlassian itself rejects most cross-project parent assignments at the API level, but the rejection is a generic 400 with a translated error message that varies across instances. To give agents a stable shape, the gateway extracts the project from `parent.key` and rejects 400 `cross_project_parent` **before** the upstream call when:

```text
extract_project_key(body.parent.key)  !=  body.projectKey
```

This is a strict equality check — no separate "linkable parent" allowlist exists in v1. The audit entry records both project keys so operators can see which call was rejected. Cross-project **issue links** are still allowed (and constrained by the strict allowlist on `/issue-link/create`); only the parent relation is project-scoped.

### Audit-log redaction for writes

Audit entries for write verbs preserve the same envelope as the read verbs (`session_mode`, `pipeline_id`, `agent_role`, `session.jira_ticket`) plus structured **metadata** about the call. They never log body content (Q20):

| Field | Logged? | Notes |
|-------|---------|-------|
| `operation` (`jira_ticket_create` / `jira_ticket_edit` / `jira_comment_add` / `jira_issue_link_create`) + `success=True` for hits, `_rejected` / `_denied` / `_upstream_error` for failures | ✅ | Each route emits a single audit entry with operation tag + `_ok` suffix on success. |
| `project` (and audit reason that names the offending side for cross-project rejections) | ✅ | Drives the allowlist decision. Set on every route. |
| `ticket` (for `edit` / `comment_add`; `new_key` recorded in `create`'s success audit) | ✅ | The ticket key being mutated. |
| `fields_present` (list of body keys actually set, e.g. `["summary","description","labels","parent"]`) | ✅ | Tells operators **which** body keys were used without leaking values. Surfaced by `_jira_write_audit_meta`. |
| `summary_length`, `description_length`, `body_length` (-1 marks an ADF passthrough; `description_kind` / `body_kind = "adf"` accompanies it) | ✅ | Lengths only — never values. |
| `labels` / `add_labels` / `remove_labels` values | ✅ | Operator-controlled enumerated strings, low-PII (Q20). |
| `link_type` name | ✅ | Operator-controlled allowlist; needed for audit. |
| `issuetype_name`, `issuetype_id` (create only) | ✅ | Whichever shape the caller passed. |
| `notify_users` (edit only) | ✅ | Boolean — set on the success audit of `ticket/edit`. |
| `idempotency_key_present` (bool), `idempotency_hit` (bool), `upstream_status` (int) | ✅ | Cache + transport metadata; lets operators distinguish hits from misses. |
| `summary` text, `description` text, `comment` body, ADF tree | ❌ | **Never logged verbatim or in any form.** The gateway only retains the size and structural fingerprints. |
| `customFields` keys | ❌ | Body is rejected by the write-keys allowlist before audit; nothing to log. |
| `idempotencyKey` raw value | ❌ | The idempotency key is **never** logged in audit entries (only the boolean `idempotency_key_present`) — callers may safely embed user identifiers in the key without PII leakage. |

Each route emits structured audit entries keyed by the `operation` tag. Successful calls emit `{operation}_ok` (e.g., `jira_ticket_create_ok`) with `success=True`. Body-shape rejections emit `{operation}_rejected`; policy-allowlist rejections emit `{operation}_denied`. Both rejection variants carry a machine-readable `reason` field inside the `details` dict; the reasons used by the write verbs include `not_allowlisted` (project allowlist), `cross_project_parent` (parent.key in different project from projectKey), `cross_project_epic_link` (epicLink.key in different project), `parent_and_epic_link` (both fields set), and `unknown_body_keys` (custom-field smuggling / typo'd keys caught by `_validate_jira_write_keys`). Upstream 4xx/5xx surface as `{operation}_upstream_error`; 429 emits `jira_upstream_rate_limited` for **all** verbs, write or read (Q12 — the audit emit was lifted out of the GET-only retry loop in `_request` into `_emit_rate_limited_audit()` so write 429s record too).

### Sandbox wrapper subcommands

`sandbox/scripts/jira` extends with four new subcommands that POST to the corresponding gateway routes. Atlassian credentials never enter the sandbox — the wrapper only carries the session token plus the JSON payload.

```bash
# Create a ticket
jira ticket create \
    --project ENG \
    --type Task \
    --summary "Investigate login latency spike" \
    --description "Users in EU regions report 4s P95 login times..." \
    --labels latency,p1 \
    --parent ENG-1200 \
    --idempotency-key investigate-login-latency-2026-04-28

# Edit a ticket — replace mode
jira ticket edit ENG-1234 \
    --summary "Investigate login latency spike (EU)" \
    --labels latency,p1,eu-only \
    --no-notify

# Edit a ticket — incremental labels (mutually exclusive with --labels)
jira ticket edit ENG-1234 \
    --add-labels eu-only \
    --remove-labels triage

# Add a comment
jira ticket comment add ENG-1234 \
    --body "Confirmed regression — bisecting now." \
    --idempotency-key comment-bisect-start

# Create an issue link
jira link create \
    --type Blocks \
    --inward ENG-1200 \
    --outward ENG-1234 \
    --comment "Blocking on the latency fix landing first."
```

**Body input ergonomics** (Q19) — `--description` (and the analogous `--body` for `ticket comment add` and `--comment` for `link create`) accepts a literal string. For long bodies, three mutually-exclusive forms are supported across all verbs that take a body:

| Flag | Source |
|------|--------|
| `--description "..."` / `--body "..."` / `--comment "..."` | Inline literal. |
| `--description-file path/to/body.md` | Read from file. |
| `--description-stdin` | Read from `stdin`. Useful for piping `git log --format=...` or ADF JSON from `jq`. |

Mixing two body-source flags returns a non-zero exit with a usage error before the gateway is called. Likewise for `--labels` vs `--add-labels` / `--remove-labels` on `ticket edit`.

**`notifyUsers` default differs between the wrapper and the HTTP route.** The HTTP route (`/api/v1/jira/ticket/edit`) defaults `notifyUsers=false` (decision-5 — quiet update). The `jira ticket edit` wrapper inverts that and defaults to `--notify` (sends `notifyUsers=true`) so the CLI matches Atlassian's UI behavior. Pass `--no-notify` to suppress notifications, or call the route directly if you want the gateway-side default.

`jira help` (and `jira --help`) lists all eight subcommands (`ticket get | comments | create | edit`, `ticket comment add`, `search`, `execute`, `link create`).

### Sandbox tools index

There is **no** `docs/reference/sandbox-tools.md` in the repository today (and no equivalent index keyed by sandbox shell wrapper) — `docs/reference/agent-tools.md` documents the in-process MCP tool surface, not the `sandbox/scripts/*` shell wrappers. So task-6-2's "if it exists" branch is a no-op for #1924; the canonical write-verb wrapper documentation lives in this section. If a sandbox-tools index is added later, it should cross-link this section by anchor (`#sandbox-wrapper-subcommands`).

### Phase rollback

The implementation was planned as six logical phases inside a single PR (#1924) — Foundation modules (phase 1), JiraClient write methods (phase 2), gateway routes (phase 3), sandbox wrapper subcommands (phase 4), tests (phase 5), and this documentation (phase 6). The coder squashed phases 1–5 (code + tests in `gateway/`, `sandbox/scripts/jira`, and `config/`) into a single commit; phase 6 (this doc + `docs/index.md`) is a separate commit. The phase split below describes the **logical revert effects** per layer; for the squashed code commit, reverts are an all-or-nothing operation across phases 1–5:

| Phase | Revert effect |
|-------|---------------|
| 1 — Foundation modules (`jira_idempotency.py`, `jira_adf.py`) | Reverts the two new modules. Phases 2–5 import them, so reverting phase 1 alone leaves the tree broken; revert phases 2–5 first or revert all six together. |
| 2 — `JiraClient` write methods | Reverts `create_issue`, `edit_issue`, `add_comment`, `create_issue_link` and the `__all__` update. The four gateway routes in phase 3 dispatch into these methods, so reverting phase 2 alone breaks the routes. The `validate_jira_api_path` GET-only constraint and the permanent denylist are unchanged on revert. |
| 3 — Gateway routes (`ticket/create`, `ticket/edit`, `ticket/comment/add`, `issue-link/create`) | Reverts the four routes and the `jira.link_types` / `jira.epic_link_field` config wiring. The wrapper subcommands in phase 4 still parse args but every call returns `404` from the gateway — agents see a stable error envelope. The read verbs are untouched. |
| 4 — Sandbox wrapper subcommands | Reverts the four new `jira ticket create`, `jira ticket edit`, `jira ticket comment add`, and `jira link create` subcommands. The gateway routes from phase 3 stay live and are still reachable via `curl` / `call_gateway` for operator-driven smoke tests; agent-side write paths are removed. |
| 5 — Tests | Reverts the per-route 403 grid extension, `test_jira_idempotency.py`, `test_jira_adf.py`, and the per-method `test_jira_client.py` additions. The implementation behavior is unchanged — only coverage drops — so reverting phase 5 alone is safe but leaves a regression-detection hole. |
| 6 — Documentation (this section) | Reverts the "Write verbs" section. The implementation in phases 1–4 keeps working but is undocumented; agent prompts that reference this section break. Re-add the doc before re-introducing the implementation if the team wants a documentation-first redo. |

**Single-PR caveat:** because all six phases land in one PR, a `git revert <merge-commit>` removes everything. To revert a single phase post-merge, identify its commit (`git log --oneline -- gateway/jira_idempotency.py` etc.) and `git revert <phase-commit>`. The plan author included a `--first-parent`-friendly commit message convention so phase reverts compose cleanly. **If the project squashes PRs on merge** (the squash-merge flow collapses every phase into one commit), the per-phase rollback table loses its surgical-revert utility — operators have to revert the whole PR or hand-craft a patch. The coder squashed phases 1–5 into a single commit before merge; phase 6 (this doc) is a separate commit, so doc-only rollback remains surgical even after a squash.

**Deferred to a follow-up issue (explicit, not silently dropped):**

- Per-role write gating (decision-10) and per-phase write gating (decision-11) — v1 is unrestricted within allowlisted projects.
- Field-level Atlassian error surfacing (Q26) — v1 reuses `_jira_error_from_upstream` verbatim.
- Multi-tenant gateway support (Q22) — v1 stays single-tenant exactly as #1556 left it.
- Per-verb rate-limit config under `jira.rate_limits:` and an `EGG_JIRA_ENABLED` kill-switch env var — both still beyond v1 scope.

## Related documentation

- [Credential Injection — Atlassian / Jira](../architecture/credential-injection.md#atlassian--jira) — where credentials live, mtime refresh, zero-credential invariant
- [Network Isolation — Gateway REST API](../architecture/network-isolation.md#gateway-rest-api) — endpoint summary and Squid allowlist exclusion
- [Sandbox environment rules](../../sandbox/agent-config/rules/environment.md) — `jira` wrapper verbs, `EGG_JIRA_TICKET` semantics
- Gateway source: `gateway/jira_credentials.py`, `gateway/jira_client.py`, `gateway/jira_policy.py`, `gateway/jira_idempotency.py`, `gateway/jira_adf.py`, `gateway/mode_gate.py`
- Sandbox wrapper: `sandbox/scripts/jira`
- Config: `config/context-filters.yaml` (`jira.projects`, `jira.link_types`, `jira.epic_link_field`), `config/secrets.template.env`
