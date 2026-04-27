# Confluence Wrapper Reference

> Last reviewed against Atlassian docs: 2026-04-27

> Gateway REST surface that gives sandboxed agents **read-only** access to Confluence. Mirrors the `/api/v1/jira/*` pattern from [#1556](https://github.com/jwbron/egg/issues/1556): Atlassian credentials live in the gateway, the sandbox posts session-authenticated JSON, and every call is funneled through a private-mode gate, a **space allowlist**, a verb allowlist, and structured audit logs.

v1 is read-only — eight `POST /api/v1/confluence/*` routes covering page reads, descendants, footer/inline comments, space listings, CQL search, and a regex-allowlisted GET-only `/execute` escape hatch. Write verbs (`page/create`, `page/update`, `comment/create`) are scoped as a follow-up and drop in as three additional narrow routes under the same decorators and policy plumbing — no re-architecting. Attachments, restrictions, permissions, space-admin verbs, user enumeration, and `PUT` / `PATCH` / `DELETE` methods are **permanently out of scope** and are enforced at the path validator.

The wrapper is a **v2-first hybrid** with transparent v1 fallbacks for two known v2 quirks:

1. CQL search uses Atlassian's v1-only `/wiki/rest/api/search` endpoint — there is no v2 CQL search.
2. Inline-comments reads automatically retry against the v1 `child/comment?location=inline` endpoint when v2 returns 404 (the well-known v2 inline-comment routing bug). The route response carries a `used_fallback` flag so operators can monitor when the v1 path is exercised.

## Endpoint surface

All eight routes are `POST /api/v1/confluence/...`, require a session token via `@require_session_auth`, and are gated by `@require_private_mode` (from `gateway/mode_gate.py`). In public mode every route returns 403 `private_mode_required` **before** any upstream call — no Atlassian credential is loaded and no network egress happens. A route-enumeration regression test in `gateway/tests/test_confluence_routes.py` iterates `app.url_map` and asserts `__egg_requires_private_mode__ = True` on every `/api/v1/confluence/*` view function so newly-added routes cannot accidentally drop the gate.

| Endpoint | Purpose | Upstream |
|----------|---------|----------|
| `POST /api/v1/confluence/page/get` | Read a single page; default `body-format=storage` | `GET /wiki/api/v2/pages/{id}` |
| `POST /api/v1/confluence/page/descendants` | List pages under a page (depth-bounded by default) | `GET /wiki/api/v2/pages/{id}/descendants` |
| `POST /api/v1/confluence/page/footer-comments` | Read footer comments; optional v1 nested-reply merge | `GET /wiki/api/v2/pages/{id}/footer-comments` (+ `/wiki/api/v2/footer-comments` for replies) |
| `POST /api/v1/confluence/page/inline-comments` | Read inline comments; transparent v1 fallback on v2 404 | `GET /wiki/api/v2/pages/{id}/inline-comments` (+ `/wiki/rest/api/content/{id}/child/comment` v1 fallback) |
| `POST /api/v1/confluence/space/pages` | List pages in a space | `GET /wiki/api/v2/spaces/{space-id}/pages` |
| `POST /api/v1/confluence/space/list` | List spaces (filtered to allowlist) | `GET /wiki/api/v2/spaces` |
| `POST /api/v1/confluence/search` | CQL search with conservative static space-scope extraction | `GET /wiki/rest/api/search` |
| `POST /api/v1/confluence/execute` | GET-only regex-allowlisted passthrough | `GET /wiki/api/v2/...` or `GET /wiki/rest/api/...` |

### `POST /api/v1/confluence/page/get`

**Request body:**
```json
{
  "pageId": "12345",
  "bodyFormat": ["storage"],          // optional; default ["storage"]
  "expand": null                       // optional passthrough
}
```

**Validation:**
- `pageId` must match `^\d+$`.
- `bodyFormat` entries (when supplied) must each be one of `"storage"`, `"atlas_doc_format"`, `"view"`, `"export_view"`. Comma-joined into the v2 `body-format` query parameter.
- The space-allowlist check runs **after** the upstream fetch using the response's `spaceId` (resolved to `spaceKey` via the in-process bidirectional cache; see [Page → space resolution caching](#page--space-resolution-caching)). If the resolved `spaceKey` is not in `confluence.spaces`, the gateway returns HTTP 403 `confluence_space_denied` and **does not forward the response body** — allowlist denials never leak page bodies.

**Response:** the Atlassian page JSON with `body.storage` populated by default, post-redaction (see [Response redaction](#response-redaction)). On upstream 404, see the [`not_found` envelope](#not_found-envelope).

### `POST /api/v1/confluence/page/descendants`

**Request body:**
```json
{
  "pageId": "12345",
  "depth": null,        // optional; default depth=1 when caller omits
  "limit": null,        // optional; default limit=25 when caller omits
  "cursor": null
}
```

When the caller omits `depth` and `limit`, the route applies sensible defaults (`depth=1`, `limit=25`) to bound runaway responses on deeply nested space trees. Caller-supplied values are passed through verbatim — there is **no gateway-imposed cap** in v1. Same `pageId` shape validation and post-fetch space-allowlist check as `/page/get`.

### `POST /api/v1/confluence/page/footer-comments`

**Request body:**
```json
{
  "pageId": "12345",
  "bodyFormat": ["storage"],
  "includeReplies": false,      // optional; when true, merges nested replies via v1 fallback
  "limit": null,
  "cursor": null
}
```

Calls `ConfluenceClient.get_page_footer_comments(...)`. When `includeReplies=true`, the client follows up with `GET /wiki/api/v2/footer-comments?page-id={id}&depth=all` and merges the nested replies into the response under a normalized envelope (`{"results": [...], "_replies": {...}}`). The v2 endpoint alone returns only top-level footer comments; the secondary call closes that gap. Same post-fetch space-allowlist check as `/page/get` — the route fetches the parent page first to resolve `spaceKey`, and **fails closed** with `confluence_space_denied` if the parent fetch errors (transient 5xx, upstream 403, missing credentials) or returns the `not_found` envelope, so a comment body is never shipped without an allowlist match.

### `POST /api/v1/confluence/page/inline-comments`

**Request body:**
```json
{
  "pageId": "12345",
  "bodyFormat": ["storage"],
  "limit": null,
  "cursor": null
}
```

Calls `ConfluenceClient.get_page_inline_comments(...)`. The client targets `GET /wiki/api/v2/pages/{id}/inline-comments` first; on v2 404 it transparently retries the v1 endpoint `GET /wiki/rest/api/content/{id}/child/comment?location=inline&expand=body.view` (the well-known v2 inline-comment routing bug). The route response contains a `used_fallback` boolean so operators can see how often v1 is being exercised. The fallback distinguishes three cases:

| v2 response | v1 response | Wrapper response |
|-------------|-------------|------------------|
| 200 | (not called) | v2 payload, `used_fallback=false` |
| 404 | 200 with comments | v1 payload normalized, `used_fallback=true` |
| 404 | 200 empty `results` | `{"results": [], "used_fallback": true}` (page exists, has no inline comments) |
| 404 | 404 | [`not_found` envelope](#not_found-envelope) with `used_fallback=true` (page genuinely missing) |

Each fallback also emits a `confluence_v1_fallback` audit entry with `{endpoint, v2_status, page_id}` so operators can monitor whether Atlassian has fixed the v2 bug and we can retire the fallback later. Same post-fetch space-allowlist check as `/page/get` — the route fetches the parent page first to resolve `spaceKey`, and **fails closed** with `confluence_space_denied` if the parent fetch errors or returns the `not_found` envelope. The space-allowlist gate runs against the resolved `spaceKey` regardless of which Atlassian API version answered the inline-comments request.

### `POST /api/v1/confluence/space/pages`

**Request body:**
```json
{
  "spaceKey": "ENG",
  "bodyFormat": ["storage"],
  "limit": null,
  "cursor": null
}
```

`spaceKey` is validated against `^[a-zA-Z][a-zA-Z0-9_]*$` and the allowlist check runs **before** any upstream call (the agent supplied the key directly, no risk of bypass via response shape). The route then resolves `spaceKey → spaceId` via `ConfluenceClient.list_spaces(allowed_spaces=...)`; if no match, HTTP 404 `{"status": "not_found", "spaceKey": "..."}`. On match, calls `get_space_pages(space_id, ...)`.

### `POST /api/v1/confluence/space/list`

**Request body:**
```json
{
  "limit": null,
  "cursor": null
}
```

Calls `ConfluenceClient.list_spaces(allowed_spaces=allowed_spaces(), ...)`. The response is **filtered to allowlisted spaces only** — agents cannot enumerate the full tenant space set. The Atlassian cursor is preserved if any allowlisted spaces were filtered out so callers can paginate. Audit entry: `confluence_space_list` with `spaces_returned: N` (count after filtering).

### `POST /api/v1/confluence/search`

**Request body:**
```json
{
  "cql": "space = ENG AND text ~ \"RFC\"",
  "limit": null,           // optional; clamped to 100, default 50
  "cursor": null
}
```

`limit` is clamped to **100** (default 50). CQL search uses Atlassian's v1-only `/wiki/rest/api/search` endpoint — there is no v2 CQL search.

**Conservative static CQL space-scope extractor.** To keep the route safe even against adversarial CQL, the gateway does **not** pass arbitrary queries through. It statically extracts the space scope and **denies on ambiguity**. The CQL is accepted only if it matches one of:

- `space = KEY` (case-sensitive `space` keyword) at top level, ANDed only.
- `space IN (K1, K2, ...)` at top level with every key in `confluence.spaces`, ANDed only.

…optionally AND-combined at top level with arbitrary additional clauses (e.g., `space = ENG AND text ~ "rfc"`).

Rejected (403 `confluence_search_rejected` with the matched reason):

- No `space` clause at all.
- `space` under any `OR` — including `space = ENG OR space = SEC` and `space = ENG OR id = "12345"`.
- Case-variant keywords (`SPACE = ENG`).
- Quoted space keys (`space = "ENG"`) are rejected unconditionally — the static extractor requires bare keys, even when the quoted key decodes to an allowlisted space. Rationale: deny-on-ambiguity, mirrors Jira's stance for the same reason.
- CQL functions (`currentUser()`, `recentlyViewedContent()`, `now()`, etc.) inside the `space` operand.
- Bare `id =` / `content =` / `title ~` clauses without a `space` clause.
- Semicolons, CQL comments (`/* */`, `--`, `//`), or other injection patterns.
- Unicode homoglyph / mixed-script space keys (e.g., `ＥＮＧ`).
- `IN` lists containing any non-allowlisted key.

The extractor is the hard boundary — if it cannot prove the query is scoped to allowlisted spaces, the request is denied. The audit entry records `spaces_extracted` on acceptance and the rejection reason on denial.

### `POST /api/v1/confluence/execute`

**Request body:**
```json
{
  "method": "GET",
  "path": "api/v2/pages/12345",
  "query": { "body-format": "storage" },
  "body": null
}
```

Only `GET` is accepted. The `path` is validated against a hardened regex allowlist in `validate_confluence_api_path`:

- Leading/trailing slashes are stripped, query strings are stripped, `..` segments are rejected, duplicate slashes are rejected, non-ASCII / non-normalized Unicode is rejected, URL-encoded smuggling (e.g., `%61ttachments`) is rejected.
- Allowed path families (GET-only, both carry an inline id): `^api/v2/pages/\d+$`, `^api/v2/spaces/\d+/pages$`. The four flat-v2 endpoints (`api/v2/footer-comments`, `api/v2/inline-comments`, `api/v2/spaces`, `rest/api/search`) and the page-scoped descendants / comment paths (`api/v2/pages/\d+/descendants`, `api/v2/pages/\d+/footer-comments`, `api/v2/pages/\d+/inline-comments`, `rest/api/content/\d+/child/comment`) are **deliberately excluded** — see the [Anti-bypass invariant](#anti-bypass-invariant) below.
- Any path containing `restrictions`, `permissions`, `space.admin`, `users`, or `attachments` is rejected — these are the permanent "out of scope ever" verbs (decision 12). The `CONFLUENCE_DENIED_VERBS` frozenset checks for the term in any path position so `pages/123/attachments` is refused as well.

All path families that the `/execute` allowlist accepts target a specific resource — either a page (`pages/{id}`) or a space (`spaces/{id}/pages`). The post-fetch space-allowlist check resolves each request's `spaceKey` once the upstream response arrives, identical to the narrow routes. The descendants / footer-comments / inline-comments / v1-comment paths were dropped from the `/execute` allowlist because the dedicated narrow routes (`POST /api/v1/confluence/page/descendants`, `/page/footer-comments`, `/page/inline-comments`) already cover those reads with the right policy hooks (depth defaults, v1 fallback bookkeeping, post-fetch allowlist) — `/execute` is a generic escape hatch and should not duplicate them.

**Anti-bypass invariant.** `/execute` does **not** accept the four "flat-v2" path families that would skip narrow-route policy:

| Removed path | Bypass that would have been possible |
|--------------|--------------------------------------|
| `rest/api/search` | Arbitrary CQL bypassing `extract_search_spaces` (the static space-scope extractor) |
| `api/v2/spaces` | Full tenant-space enumeration bypassing `list_spaces`'s allowlist filter (defeats decision-11) |
| `api/v2/footer-comments` (flat) | `page-id`-in-query with no upstream `spaceKey` filter — post-fetch allowlist cannot resolve the targeted page |
| `api/v2/inline-comments` (flat) | Same flat-endpoint shape as footer-comments |
| `api/v2/pages/{id}/descendants` | Duplicates `POST /api/v1/confluence/page/descendants`, which already enforces depth defaults and post-fetch allowlist |
| `api/v2/pages/{id}/footer-comments` | Duplicates `POST /api/v1/confluence/page/footer-comments`, which already merges nested replies and applies the allowlist |
| `api/v2/pages/{id}/inline-comments` | Duplicates `POST /api/v1/confluence/page/inline-comments`, which owns the v2→v1 fallback flag |
| `rest/api/content/{id}/child/comment` | The v1 inline-comment fallback is an internal-only retry path; agents must use the `/page/inline-comments` route |

The flat-v2 paths and the page-scoped comment / descendants / v1-comment paths remain reachable **internally** by `ConfluenceClient` methods that construct them directly (`get_page_descendants`, `get_page_footer_comments`'s `include_replies` side-call against `api/v2/footer-comments?page-id=...&depth=all`, `get_page_inline_comments` against `api/v2/pages/{id}/inline-comments` with a `rest/api/content/{id}/child/comment` v1 fallback, and `list_spaces` against `api/v2/spaces`) — those internal calls do **not** go through `validate_confluence_api_path`. Only the agent-facing `/execute` escape hatch refuses them; agents reach the same data through the dedicated narrow routes (`/page/descendants`, `/page/footer-comments`, `/page/inline-comments`, `/space/list`). A regression test in `gateway/tests/test_confluence_client.py` parametrizes the removed paths and asserts they fail the validator; an end-to-end regression test in `gateway/tests/test_confluence_routes.py` asserts the same paths return 403 `confluence_execute_denied` through the Flask test client. Mirrors `gateway/jira_client.py`'s permanent denylist of `search/jql` + bare `project` for the same anti-bypass reason (PR #1964).

`/execute` is a pragmatic escape hatch for future read verbs not yet promoted to narrow routes. It is **not** a general passthrough — the regex allowlist (page- and space-scoped paths only), the `CONFLUENCE_DENIED_VERBS` frozenset, and the anti-bypass invariant together are the fence.

## `not_found` envelope

The Atlassian v2 API returns 404 for missing pages/spaces with error-message JSON that varies across instances and rarely maps cleanly to a sandbox flow. To give agents a stable shape, the gateway **intercepts upstream 404 on the read methods** (`page/get`, `page/descendants`, `page/footer-comments`, `page/inline-comments`, `space/pages`) and returns HTTP 200 with:

```json
{
  "status": "not_found",
  "id": "12345",
  "upstream_status": 404
}
```

For the inline-comments route specifically, the envelope additionally carries `"used_fallback": true` when the v1 fallback also returned 404 (so callers can tell the page genuinely doesn't exist, not just "v2 bug + page exists").

`/search` and `/execute` do **not** use the envelope — their upstream 404 is a real API error (wrong path, deleted space, etc.) and is surfaced as `ConfluenceUpstreamError` and translated to the original upstream status by the route handler.

## Response redaction

Every successful response body is sanitised by `redact_response(payload)` in `gateway/confluence_client.py` before it leaves the gateway. The recursive walker:

- Replaces every `accountId` value (at any depth) with `"<redacted>"`.
- Replaces every `emailAddress` value (at any depth) with `"<redacted>"`.
- Strips `_links.webui` user-profile URLs — any URL whose path begins with `/people/` or matches an Atlassian user-profile shape. **Page and space `_links.webui` URLs are preserved** — those are addressable resources the agent legitimately needs.
- Strips `_links.self` URLs that match the Atlassian v2 user-profile shape (regex `/api/v\d+/users/`, so `/api/v2/users/{accountId}` and any future v3+ users endpoint shape are both covered). v2 user objects expose `_links.self` pointing at the user-profile API endpoint; this is **defense-in-depth** so a future Atlassian schema change that drops the `accountId` field but keeps the link does not silently start leaking identifiers. Page and space `_links.self` URLs (which point at `/api/vN/pages/...` or `/api/vN/spaces/...`) are preserved.

The walker handles nested ADF mention nodes and `body.atlas_doc_format.content` trees, so the redaction holds for storage-format, ADF, and view-format bodies alike.

If a tenant carries custom Confluence macros, page properties, or fields known to hold PII or secrets beyond the four default keys, file a follow-up to extend the redaction list — the v1 design ships defaults only (refine-phase Q3).

## Error cases

| HTTP | Condition | Audit event |
|------|-----------|-------------|
| 400 | Malformed `pageId` / `spaceKey`, invalid `bodyFormat`, missing required body field | `confluence_<verb>_rejected` with reason |
| 401 | Session token invalid / missing | Standard gateway auth rejection |
| 403 | Public mode (private-mode gate) | `private_mode_required` |
| 403 | Resolved space not in `confluence.spaces` | `confluence_space_denied` |
| 403 | `/search` CQL fails the static scope extractor | `confluence_search_rejected` with the specific reason |
| 403 | `/execute` denied verb, non-GET method, path traversal, disallowed path family (including the four flat-v2 anti-bypass paths), duplicate slash, non-ASCII | `confluence_execute_denied` with reason |
| 403 | Atlassian returned 403 (bot lacks read access on the resource) | `confluence_upstream_403` (distinct from generic `confluence_upstream_error`) — body: `{"status": "forbidden", "reason": "bot_account_lacks_read_access", "pageId" \| "spaceKey": "..."}` |
| 413 | Response body exceeds `CONFLUENCE_RESPONSE_MAX_BYTES` (5 MiB) post-redaction | `confluence_response_too_large` |
| 503 | Atlassian credentials not configured (`ConfluenceCredentialsUnavailable`) | `confluence_credentials_unavailable` |
| *upstream* | Atlassian 4xx/5xx other than the 404 envelope paths and the 403 escalation | Upstream status passed through, `confluence_upstream_error` audit entry |

**429 handling.** Atlassian rate-limit responses are retried exactly once, sleeping `min(Retry-After, 30)` seconds; retry is GET-only, write verbs never retry (future-proofing). Both the initial and retry 429 emit a `confluence_upstream_rate_limited` audit entry including the `Retry-After` value, the path, and an `attempt: 1|2` field so operators can tell whether the retry succeeded. After the second 429, the response is passed through verbatim. Identical semantics to the Jira client.

**Why the 403 escalation has its own audit category.** Atlassian's permission model is per-page (and per-space), so a bot that has read access to a space can still hit 403 on a specific page whose hierarchy is restricted. Splitting `confluence_upstream_403` from generic `confluence_upstream_error` (refine-phase Q7) lets operators distinguish:

- `confluence_space_denied` — gateway-side allowlist denial (operator-facing config issue).
- `confluence_upstream_403` — Atlassian-side permission denial (bot needs more access in Atlassian's UI).
- `confluence_upstream_error` — generic upstream error (Atlassian outage, malformed request, etc.).

Every audit entry includes `session_mode`, `pipeline_id`, `agent_role`, and (where applicable) `pageId` / `spaceKey` so operators can reconcile Confluence calls with the pipeline they came from. **Per [refine-phase decision 13](#related-documentation), there is no per-pipeline `EGG_CONFLUENCE_*` env var** — the audit recovers `pageId` / `spaceKey` from each request body or response. Confluence is reference material, not a unit of work.

## Space-allowlist semantics

The allowlist lives in `config/context-filters.yaml` alongside the existing Jira section:

```yaml
confluence:
  # Atlassian Confluence space keys agents are allowed to read through
  # the /api/v1/confluence/* endpoints. Space keys are case-sensitive.
  spaces: ["ENG", "DOCS"]
```

- The authoritative key is `spaces` (parallel to Jira's `projects`).
- Default is an **empty list** — every Confluence call is rejected until an operator populates it. This is the "installed but inert" state for v1 rollout (refine-phase Q1).
- Fail-closed: if the file is missing, the `confluence:` section is absent, the `spaces:` key is missing, the YAML is malformed, or the value isn't a list, `allowed_spaces()` returns an empty set and no error is raised. Operators must see 403s on every Confluence call rather than a crashed gateway. Schema mismatches (e.g., `spaces: "ENG"` instead of a list) emit ERROR-level audit entries on first read so the misconfiguration is visible in the gateway logs immediately.
- Mixed-case keys are preserved verbatim. Atlassian space keys are conventionally uppercase but the API accepts mixed case; the allowlist intersects strictly case-sensitive.
- Reloaded on mtime change; `POST /api/v1/config/reload` calls `reload_confluence_policy()` and `reload_confluence_credentials()` as part of the existing `_reload_all_config()` hook.

**Why `context-filters.yaml` and not a dedicated file?** Operators already edit this file for GitHub context filtering and Jira project allowlisting; keeping Confluence policy in the same place means one allowlist surface to review. The Confluence section is self-contained and does not interact with the GitHub or Jira sections.

**No `EGG_CONFLUENCE_*` env vars.** Unlike Jira (where `EGG_JIRA_TICKET` is exported as the ticket the pipeline is scoped to), Confluence has **no per-pipeline env var** in v1 (refine-phase decision 13). Confluence is consulted as reference material from ticket/epic links, not as the pipeline's primary unit of work. Audits recover `pageId` / `spaceKey` from each request body or response.

## Default `body-format=storage`

Atlassian Confluence stores page bodies in three primary formats:

- **`storage`** — Confluence's internal XHTML-like format. Compact, lossy on rich macros but readable as a string.
- **`atlas_doc_format`** (ADF) — JSON tree. Most structured; necessary for ADF-aware traversal.
- **`view`** — rendered HTML. Lossy on macros / layout but immediately consumable.
- **`export_view`** — rendered HTML for export. Larger payloads than `view`.

The gateway's `ConfluenceClient.get_page` and the comment methods default to `body-format=storage` (refine-phase decision 5, operator tweak). Callers may override per-call by passing `bodyFormat: ["atlas_doc_format"]` (or any combination of the four), and the client comma-joins the entries into the v2 query. The default exists so the common case "just read me this page" works without additional ceremony and produces the smallest payload on the wire.

## Page → space resolution caching

The post-fetch space-allowlist check needs each page's `spaceKey`, which the v2 page response carries as a numeric `spaceId`. To avoid double-fetching (or refetching the page just to verify the space on the comment routes), the client maintains a single bidirectional `spaceId ↔ spaceKey` LRU cache with a 60-second TTL, populated by both `list_spaces` and `get_page`. The comment routes (`page/footer-comments`, `page/inline-comments`) reuse the cache so they do not refetch the page. `/space/pages` (`spaceKey → spaceId` cold start) reuses the cache so the cold-start lookup does not always cost a double round-trip.

## Atlassian rate-limit runbook

Atlassian Cloud uses a points-based throttling model (enforced site-wide as of March 2026). The wrapper retries once on 429 honouring `Retry-After`; persistent throttling surfaces as `confluence_upstream_rate_limited` audit events with `attempt: 1|2` fields. Operators seeing routine throttling should:

1. Provision a dedicated Atlassian bot account (separate from human users) so points contention does not happen against interactive Confluence usage on the same tenant.
2. Note that the bot account is **shared with Jira read scope** (refine-phase decision 9), so the points-based quota is **pooled across `/api/v1/jira/*` and `/api/v1/confluence/*` traffic**. Two unrelated pipelines reading from Jira and Confluence simultaneously can throttle each other.
3. Consider provisioning a Confluence-only bot in a follow-up ticket if pooled-quota throttling becomes a recurring issue. The shared-bot decision optimises for credential simplicity in v1; splitting later is a per-tenant call.

## Bot-vs-human access caveat

The gateway authenticates as the dedicated Atlassian bot account. The host-side `mcp__confluence__*` MCP authenticates as the consenting human user. **There can be pages a human reads but the bot cannot** (or vice versa) when Atlassian's per-page or per-space permission scheme excludes the bot. Operators must verify the bot's effective access at deploy time before enabling the feature in production:

- Confirm the bot has at least "View" permission on every space listed in `confluence.spaces` (Atlassian → Space settings → Permissions).
- For pages restricted by hierarchy, confirm the bot has read access on the restricted ancestor (Atlassian → Page restrictions UI).

A diagnostic command that surfaces the bot's effective space / page access from inside the gateway is **deferred to a follow-up ticket** (refine-phase Q9) — v1 ships the documentation only. In the meantime, operators can manually verify by issuing test requests against `/api/v1/confluence/page/get` for canonical pages in each allowlisted space. Hits that 403 with `confluence_upstream_403` indicate the bot lacks permission; hits that 403 with `confluence_space_denied` indicate the gateway-side allowlist is incomplete.

## Prompt-injection caveat

**Confluence content is untrusted input.** Pages and comments are written by humans (and increasingly by bots), and may carry instructions that target a downstream agent. Reviewers and operators should treat anything returned by `/api/v1/confluence/*` as **data, not directives** — wrap it in clear delimiters when feeding it to a model, and never let an agent execute instructions read from a Confluence body without an additional explicit human-approved step.

The wrapper does not attempt to sanitise content for prompt-injection; that's the consumer's responsibility. The gateway's contribution is to keep the surface narrow (space allowlist, verb allowlist, response redaction) so that the universe of attacker-controllable content is bounded and auditable.

## Future-verb extension points

The v1 shape is deliberately the base case for the follow-up write verbs (not in this ticket):

- **`POST /api/v1/confluence/page/create`** — new narrow route. Adds `POST /wiki/api/v2/pages` to `validate_confluence_api_path` (POST-allowed list keyed to the same space-allowlist gate). `ConfluenceClient.create_page(space_id, title, body, body_format)` added; existing `_request` 429-retry does not retry writes (already enforced for future safety).
- **`POST /api/v1/confluence/page/update`** — new narrow route. Adds `PUT /wiki/api/v2/pages/{id}` (Atlassian's update endpoint is PUT). `ConfluenceClient.update_page(page_id, title, body, version, body_format)`.
- **`POST /api/v1/confluence/comment/create`** — new narrow route. Adds `POST /wiki/api/v2/footer-comments` and `POST /wiki/api/v2/inline-comments`. `ConfluenceClient.add_footer_comment(page_id, body)` / `add_inline_comment(page_id, body, anchor)`.

All three land under the same `@require_session_auth` → `@require_private_mode` → space-allowlist chain. None of them extends the `/execute` passthrough — the regex allowlist there stays GET-only. The `CONFLUENCE_DENIED_VERBS` frozenset permanently refuses `restrictions`, `permissions`, `space.admin`, `users`, `attachments`, `DELETE`, `PUT`, and `PATCH` at the path validator (the v1 path validator's `PUT` denial is lifted only for the explicit `pages/{id}` write route, behind a new POST/PUT-allowlist).

**Idempotency for `comment/create`** is **deferred to the future-writes phase** (refine-phase Q4). Atlassian's create-comment endpoint is not naturally idempotent. The gateway design will be made consistent across Jira and Confluence (Jira write verbs are also out of scope today) so the design-once decision is made once.

**Deferred to v1.1 (explicit, not silently dropped):**

- `page/resolve-by-url` — given a Confluence URL, return the canonical `pageId` and `spaceKey` (refine-phase Q5). v1 expects callers to parse `pageId` from URLs themselves.
- `?version=` parameter on `/page/get` — read historical revisions (refine-phase Q8). v1 returns current-version only.
- Per-verb rate-limit config under `confluence.rate_limits:` — v1 inherits Jira's defaults.
- `EGG_CONFLUENCE_ENABLED` kill-switch env var.
- Custom-macro / page-property PII redaction beyond the three default keys (refine-phase Q3).

## Migration: shared `ATLASSIAN_*` credentials

The Confluence wrapper credentials and the Jira wrapper credentials both prefer a shared **`ATLASSIAN_BASE_URL` / `ATLASSIAN_USERNAME` / `ATLASSIAN_API_TOKEN`** triple, with per-key fall-back to the legacy `JIRA_*` and `CONFLUENCE_*` blocks for back-compat (refine-phase decision F1). Per-key precedence means a value set under `ATLASSIAN_*` wins for that key; missing keys fall back to the per-service prefix. This is what makes "shared Atlassian credential" portable — operators can copy values from the legacy blocks to `ATLASSIAN_*` and remove the legacy blocks once the shared triple is fully populated, without breaking either service.

Base-URL derivation:

- If `ATLASSIAN_BASE_URL` is set, the Confluence loader uses it and **appends `/wiki`** automatically — Confluence Cloud lives at `<tenant>/wiki/...` while Jira lives at the bare origin, so the same `ATLASSIAN_BASE_URL` value covers both services without operator-side suffix juggling.
- If `ATLASSIAN_BASE_URL` is unset and `CONFLUENCE_BASE_URL` is set, the loader uses `CONFLUENCE_BASE_URL` verbatim — operators must include the `/wiki` suffix in this legacy form.

This precedence (ATLASSIAN-wins, CONFLUENCE as per-key back-compat fallback) is consistent with the Jira loader's per-key behaviour. The same precedence applies to `USERNAME` and `API_TOKEN` independently — `ATLASSIAN_USERNAME` + `CONFLUENCE_BASE_URL` is a valid combination during partial migrations.

The Confluence loader (`gateway/confluence_credentials.py`) and the Jira loader (`gateway/jira_credentials.py`, updated as part of #1931 task 1-5) duplicate the loader skeleton in v1 — extracting a shared `atlassian_credentials.py` helper is tracked as a follow-up backlog item (architect Q4).

## Related documentation

- [Credential Injection — Atlassian / Confluence](../architecture/credential-injection.md#atlassian--confluence) — where credentials live, mtime refresh, zero-credential invariant
- [Network Isolation — Gateway REST API](../architecture/network-isolation.md#gateway-rest-api) — endpoint summary and Squid allowlist exclusion
- [Sandbox environment rules](../../sandbox/agent-config/rules/environment.md) — `confluence` wrapper verbs, no per-pipeline env var
- [Jira Wrapper Reference](jira-wrapper.md) — sister wrapper this design mirrors
- Gateway source: `gateway/confluence_credentials.py`, `gateway/confluence_client.py`, `gateway/confluence_policy.py`, `gateway/confluence_search.py`, `gateway/mode_gate.py`
- Sandbox wrapper: `sandbox/scripts/confluence`
- Config: `config/context-filters.yaml` (`confluence.spaces:`), `config/secrets.template.env` (`ATLASSIAN_*` shared block + legacy `CONFLUENCE_*` block)
