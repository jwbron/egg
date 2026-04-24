# Jira Wrapper Reference

> Gateway REST surface that gives sandboxed agents **read-only** access to Jira. Mirrors the `/api/v1/gh/*` pattern: Atlassian credentials live in the gateway, the sandbox posts session-authenticated JSON, and every call is funneled through a private-mode gate, a project allowlist, and structured audit logs.

v1 is read-only. Write verbs (`ticket create`, `ticket update`, `comment create`) are scoped as a follow-up and drop in as three additional narrow routes under the same decorators and policy plumbing — no re-architecting. Transitions, worklogs, attachments, watchers, deletions, and `PUT` / `PATCH` / `DELETE` methods are **permanently out of scope** and are enforced at the path validator.

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

## Future-verb extension points

The v1 shape is deliberately the base case for the follow-up write verbs (not in this ticket):

- **`POST /api/v1/jira/ticket/create`** — new narrow route. Adds `POST /rest/api/3/issue` to `validate_jira_api_path` (POST-allowed list keyed to the same project-allowlist gate). `JiraClient.create_ticket(project, issuetype, fields)` added; existing `_request` 429-retry does not retry writes (already enforced for future safety).
- **`POST /api/v1/jira/ticket/update`** — new narrow route. Adds `POST /rest/api/3/issue/{key}` (Atlassian's edit endpoint is POST, not PUT). `JiraClient.update_ticket(key, fields)`.
- **`POST /api/v1/jira/comment/create`** — new narrow route. Adds `POST /rest/api/3/issue/{key}/comment`. `JiraClient.add_comment(key, body)`.

All three land under the same `@require_session_auth` → `@require_private_mode` → project-allowlist chain. None of them extends the `/execute` passthrough — the regex allowlist there stays GET-only. The `JIRA_WRITE_VERBS_DENIED` frozenset permanently refuses `transitions`, `worklog`, `attachments`, `watchers`, `DELETE`, `PUT`, and `PATCH` at the path validator, even in the write-verb follow-up.

**Deferred to v1.1 (explicit, not silently dropped):** per-verb rate-limit config under `jira.rate_limits:` and an `EGG_JIRA_ENABLED` kill-switch env var. Both are beyond v1 scope; this section calls them out so reviewers don't search the codebase for them.

## Related documentation

- [Credential Injection — Atlassian / Jira](../architecture/credential-injection.md#atlassian--jira) — where credentials live, mtime refresh, zero-credential invariant
- [Network Isolation — Gateway REST API](../architecture/network-isolation.md#gateway-rest-api) — endpoint summary and Squid allowlist exclusion
- [Sandbox environment rules](../../sandbox/agent-config/rules/environment.md) — `jira` wrapper verbs, `EGG_JIRA_TICKET` semantics
- Gateway source: `gateway/jira_credentials.py`, `gateway/jira_client.py`, `gateway/jira_policy.py`, `gateway/mode_gate.py`
- Sandbox wrapper: `sandbox/scripts/jira`
- Config: `config/context-filters.yaml`, `config/secrets.template.env`
