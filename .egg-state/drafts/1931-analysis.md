# Analysis: Add Confluence gateway support (read-only v1)

> Issue: #1931 | Phase: refine

## Problem Statement

Sandboxed egg agents today have no way to read Confluence pages. The host-side `mcp__confluence__*` MCP that bundles Atlassian (Jira + Confluence) is unusable from the sandbox for the same reasons #1556 enumerated for Jira: (1) the MCP runs in the host Claude Code process and is unreachable from isolated agent containers, and (2) it exposes the owning human's full Atlassian API surface (writes, deletes, permission changes) with no space or verb allowlist — a contradiction of egg's "infrastructure beats config" security thesis.

#1556 has already landed the Jira gateway. Issue #1557 (Jira-epic SDLC pipelines) and the [`/impact-analysis` skill](../../docs/index.md) need to read Confluence pages **linked from Jira tickets** during the refine phase. Today they cannot, because Confluence remains host-only. #1931 is the infrastructure-only v1: **read-only** Confluence access for sandboxed agents, delivered through the existing gateway sidecar, **mirroring the Jira wrapper one-for-one**, with Atlassian credentials held exclusively in the gateway and shared with the Jira gateway (same Atlassian tenant).

Desired outcome:

1. Sandboxed agents can fetch a page, list pages in a space, fetch page descendants, read footer + inline comments, list spaces, and search via CQL — all via the gateway.
2. Atlassian credentials **never** enter the agent container (zero-credential invariant preserved — see [`docs/architecture/credential-injection.md`](../../docs/architecture/credential-injection.md)).
3. Confluence endpoints are only reachable when the agent session is in **private network mode** (see [`docs/architecture/network-isolation.md`](../../docs/architecture/network-isolation.md)); in public mode the gateway fails closed.
4. Policy is keyed on **space allowlist + verb allowlist** — agents cannot read spaces or call verbs the operator has not granted.
5. v1 endpoints, policy, and credential scopes are shaped so the future write verbs (page create / update, comment create) drop in as pure extensions. Deletions, space admin, and permission/restriction changes are **out of scope ever**.
6. **No per-agent env var** is required (unlike `EGG_JIRA_TICKET`). Confluence is consulted as reference material from ticket / epic links, not as a primary unit of work.
7. Future write verbs are designed in but not implemented: same private-mode restriction, same allowlist plumbing, same audit shape.

## Current Behavior

### Gateway sidecar as choke point — Jira pattern is now the template

The gateway ([`gateway/gateway.py`](../../gateway/gateway.py)) already exposes a Jira read-only surface delivered by #1556 ([reference doc](../../docs/reference/jira-wrapper.md)). Confluence v1 will be a **structural copy**:

| Jira primitive (extant) | Confluence primitive (to add) | Reuse strategy |
|-------------------------|-------------------------------|----------------|
| `gateway/jira_client.py` (HTTPX, Basic auth, 429 retry, 404-envelope) | `gateway/confluence_client.py` | Same shape; same retry / envelope conventions |
| `gateway/jira_credentials.py` (mtime-cached, thread-safe) | `gateway/confluence_credentials.py` | Same `parse_env_file` import from `anthropic_credentials.py`; same `JiraCredentialsManager` pattern |
| `gateway/jira_policy.py` (project allowlist) | `gateway/confluence_policy.py` (space allowlist) | Same YAML mtime cache, same fail-closed semantics |
| `gateway/jira_search.py` (conservative JQL extractor) | `gateway/confluence_search.py` (CQL extractor) | New module, but same deny-on-ambiguity stance |
| `gateway/mode_gate.py` `@require_private_mode` | (unchanged) | Decorator already generic — apply to every Confluence route |
| `config/context-filters.yaml` — `jira.projects: [...]` | New `confluence.spaces: [...]` section in same file | Operators already edit this file for Jira and GitHub filtering |
| `config/secrets.template.env` `JIRA_BASE_URL` / `JIRA_USERNAME` / `JIRA_API_TOKEN` | Existing `CONFLUENCE_BASE_URL` / `CONFLUENCE_USERNAME` / `CONFLUENCE_API_TOKEN` placeholders | Already scaffolded; question is whether to share or split (see Open Questions) |
| `sandbox/scripts/jira` (bash → gateway POST) | `sandbox/scripts/confluence` (bash → gateway POST) | Same `call_gateway()` shape, same `EGG_SESSION_TOKEN` Bearer auth |
| Squid `allowed_domains.txt` excludes `*.atlassian.net` | (unchanged) | Already excluded; Confluence cannot bypass via direct egress for the same reason |
| `gateway/tests/test_jira_routes.py` (route enumeration, private-mode regression, allowlist tests) | `gateway/tests/test_confluence_routes.py` | Same fixtures, same per-route 403/200 grid |

The **mode-gate decorator and Squid allowlist** are already generic and need no change — the same fence that blocks public-mode Jira blocks public-mode Confluence, and the same Squid exclusion that prevents direct Atlassian egress applies.

### Existing Confluence footprint in the codebase

Partial scaffolding is already in place but unused for live API:

- [`config/secrets.template.env`](../../config/secrets.template.env) lines 92–99 define `CONFLUENCE_BASE_URL`, `CONFLUENCE_USERNAME`, `CONFLUENCE_API_TOKEN`, `CONFLUENCE_SPACE_KEYS`. Nothing reads them today (the existing context-sync syncer is out of tree / out of scope).
- [`sandbox/agent-config/rules/environment.md`](../../sandbox/agent-config/rules/environment.md) line 80 mentions `~/context-sync/confluence/` as an optional read-only cache (snapshot, not live API).
- [`sandbox/agent-config/commands/show-metrics.md`](../../sandbox/agent-config/commands/show-metrics.md) line 12 references `ls ~/context-sync/confluence/` for diagnostic purposes.
- The Atlassian MCP referenced in the issue (`mcp__confluence__*`) runs **only on the host**. It is irrelevant to sandbox plumbing.

### What `mcp__confluence__*` looks like (shape we are asked to align with)

The issue explicitly asks the v1 verb surface to match the host MCP tool names so consumers port cleanly:

| MCP tool name (host) | Atlassian endpoint family |
|-----------------------|----------------------------|
| `getConfluencePage` | `GET /wiki/api/v2/pages/{id}` |
| `getPagesInConfluenceSpace` | `GET /wiki/api/v2/spaces/{space-id}/pages` |
| `getConfluencePageDescendants` | `GET /wiki/api/v2/pages/{id}/descendants` |
| `getConfluencePageFooterComments` | `GET /wiki/api/v2/pages/{id}/footer-comments` |
| `getConfluencePageInlineComments` | `GET /wiki/api/v2/pages/{id}/inline-comments` |
| `getConfluenceSpaces` | `GET /wiki/api/v2/spaces` |
| `searchConfluenceUsingCql` | `GET /wiki/rest/api/search` (v1 — see API-version note) |

The verb names appear in MCP tool surface. Whether the **gateway URL paths** mirror that shape literally (`/api/v1/confluence/getConfluencePage`) or follow the Jira convention (`/api/v1/confluence/page/get`) is an open question (see below — the wrappers preserve the consumer-facing names regardless).

### Atlassian Confluence API — the v1 / v2 split

This is the largest external constraint and the place v1 must make a concrete decision. The Confluence Cloud REST API is split across two coexisting versions:

- **v2** (`/wiki/api/v2/...`): preferred by Atlassian for new development. Cleaner cursor pagination, distinct content types per endpoint, ADF body format by default. v2 covers `pages`, `spaces`, `descendants`, `footer-comments`, `inline-comments`. ([Atlassian REST v2 intro](https://developer.atlassian.com/cloud/confluence/rest/v2/intro/))
- **v1** (`/wiki/rest/api/...`): older, broader. **CQL search remains on v1 with no v2 equivalent**, and Atlassian has stated [no plans to deprecate](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/) the v1 search endpoint. v1 also retains a few endpoints v2 hasn't reached parity on.

Known v2 quirks that affect v1:

- `/wiki/api/v2/pages/{id}/footer-comments` **does not return nested replies** — only top-level. Workaround is a separate `/wiki/api/v2/footer-comments` query or v1 fall-back. ([community report](https://community.developer.atlassian.com/t/confluence-rest-api-v2-get-footer-nested-comments-for-page/82487))
- v2 inline-comments have a known bug returning 404 where v1 succeeds. ([community report](https://community.developer.atlassian.com/t/confluence-rest-api-v2-doesnt-return-inline-comment-404-instead-v1-works-bug/86668))
- v2 has cursor pagination; v1 has both cursor and limit-based; the gateway will need to wrap both shapes.

Atlassian commits to ≥6 months notice before any v1 endpoint is removed and currently has no firm sunset date. ([Confluence v2 vs v1 community thread](https://community.atlassian.com/forums/Confluence-questions/Confluence-API-v1-versus-v2/qaq-p/2978171))

**Implication**: v1 of our wrapper is **inherently a hybrid** — read endpoints prefer v2, but CQL search must use v1. Comments may need v1 fallback for inline / nested. We should pin every endpoint per-verb in the wrapper rather than declare a single API version.

### How `session_mode == "private"` already encodes the gate we want

`session_mode == "private"` already couples (a) Squid network lockdown to Anthropic-only egress, (b) private-repo-only access, and (c) the Jira route gate. Adding Confluence to (c) is a one-line decorator addition per route. The decorator stamps `__egg_requires_private_mode__ = True` so the Jira-style **route-enumeration regression test** can prove every `/api/v1/confluence/*` route carries the gate.

### How Confluence MCP fits today (host-side reality)

`mcp__confluence__*` runs on the host, inside the human's Claude Code session, with OAuth 2.0 creds stored in `~/.claude.json`. It is available for host-side interactive work and for issue-triage scripts run by the human, but it is **not** available to sandboxed agents — the MCP process is not reachable across the k8s NetworkPolicies, and running it inside the sandbox would violate the zero-credential invariant. The argument is identical to #1556's "Why not just use the Atlassian MCP?" and does not need to be re-litigated.

## Constraints

**Security / architectural:**

- Zero credentials in the sandbox container (hard invariant). Atlassian credentials must live only in the gateway process.
- All requests from the sandbox must flow through the gateway. `*.atlassian.net` is **already excluded** from the Squid allowlist; do not add a Confluence-specific entry, or containers could bypass the space allowlist via direct REST calls. The existing Jira regression test in [`gateway/tests/test_allowed_domains.py`](../../gateway/tests/test_allowed_domains.py) already enforces this and covers Confluence by extension.
- Read-only in v1. The gateway must refuse Confluence write verbs even if the upstream API would accept them. Enforcement is at the gateway (infrastructure), not in agent instructions.
- Private-mode-only: public-mode sessions get a 403 on any `/api/v1/confluence/*` endpoint. This must be enforced at the route layer via `@require_private_mode`, not left to downstream policy.
- **Space allowlist + verb allowlist**. Agents can only query spaces the operator has sanctioned and only with verbs from the configured set.
- Future-verb compatibility: v1 design must support `page create`, `page update`, `comment create` as drop-in narrow routes — no re-architecting. Deletions, space admin, permission/restriction changes are **permanently** denied at the path validator (the analogue of Jira's `JIRA_WRITE_VERBS_DENIED`).
- Permanent denylist (path-validator level): `restrictions`, `permissions`, `space.admin`, `attachments` (debatable — see Open Questions), and any HTTP `DELETE` / `PUT` / `PATCH` (matching the Jira convention). Update verbs in the future use `POST` against Atlassian's edit endpoints.

**Operational:**

- Credential lifecycle: Atlassian API tokens don't auto-rotate. Mtime-based reload (already used for Anthropic + Jira) extends to Confluence with no new mechanism.
- Single-tenant for v1 is acceptable; multi-tenant should not be architected out.
- Test strategy: gateway tests today mock upstream Atlassian with `responses` / `pytest` monkeypatching for Jira; same harness applies to Confluence.
- Rate limiting: Atlassian Cloud applies per-site, per-user quotas. Reuse the Jira client's "retry once on 429 with `min(Retry-After, 30)` and emit `confluence_upstream_rate_limited` audit on both attempts" approach.
- Body format: ADF JSON is unusable for an agent without rendering. Reuse Jira's `expand=renderedBody,renderedFields` pattern — for Confluence v2 the analogous parameter is `body-format=storage` or `body-format=atlas_doc_format`; for full HTML we likely want both `storage` and the rendered view. (Decision below.)

**Dependencies / coupling:**

- **Hard dependency on #1556** (Jira gateway v1, merged). Confluence reuses the credential / mode-gate / context-filter / Squid plumbing established there. No new infrastructure layer is introduced.
- **Consumer #1557** (Jira-epic SDLC pipelines) — its refine phase pulls linked Confluence pages from sandboxed agents. #1557 cannot land its refine path until #1931 is merged.
- **Consumer `/impact-analysis` skill** — currently runs host-side and uses `mcp__confluence__*`. Once #1931 lands, the sandboxed-agent variant of impact analysis can call the gateway. The host-side use is not affected.
- Confluence shares Atlassian credentials with Jira (same tenant). Open question: share secret keys or duplicate.

**External (Atlassian):**

- v1 + v2 hybrid (above). CQL is v1-only.
- v2 is **better for pagination** but has known bugs in inline / nested comments.
- Body formats: ADF (`atlas_doc_format`), `storage` (Confluence's XHTML-like markup), `view` (rendered HTML), `export_view` (HTML for export). [Storage format ref](https://developer.atlassian.com/cloud/confluence/storage-format/).
- CQL has a 200-result hard limit per query; pagination required beyond that. Quirks: `text ~ "..."` is contains-search, not regex. ([CQL quirks summary](https://cotera.co/articles/confluence-api-integration-guide))
- Auth: Same Atlassian Cloud API token works for both Confluence and Jira. Granular OAuth scopes for read-only would be `read:page:confluence`, `read:space:confluence`, `read:comment:confluence` (v2) — but #1556's B1 (Basic auth + bot account) was the chosen direction and the same applies here.
- v1 search endpoint: `GET /wiki/rest/api/search?cql=...` returns paginated results with `_links.next`.

## Options Considered

### Endpoint surface — gateway path naming

#### A1. Path-based shape mirroring Jira (`/api/v1/confluence/page/get`, `/api/v1/confluence/search`, etc.) — **preferred**

**Approach**: The gateway URL paths use the Jira-style verb-noun shape. The sandbox wrapper script (`sandbox/scripts/confluence`) translates user-friendly subcommands (`confluence page get <ID>`, `confluence search '<CQL>'`, `confluence space pages <SPACE-ID>`) into POSTs against these paths. The wrapper-level subcommand names can mirror the `mcp__confluence__*` shape (`getConfluencePage`-style aliases) so call sites that currently use the host MCP can be ported with minimal text edits.

Concrete v1 routes:

- `POST /api/v1/confluence/page/get` → `GET /wiki/api/v2/pages/{id}`
- `POST /api/v1/confluence/page/descendants` → `GET /wiki/api/v2/pages/{id}/descendants`
- `POST /api/v1/confluence/page/footer-comments` → `GET /wiki/api/v2/pages/{id}/footer-comments` (with v1 fallback for nested replies, see below)
- `POST /api/v1/confluence/page/inline-comments` → `GET /wiki/api/v2/pages/{id}/inline-comments` (with v1 fallback for known 404 bug)
- `POST /api/v1/confluence/space/pages` → `GET /wiki/api/v2/spaces/{space-id}/pages`
- `POST /api/v1/confluence/space/list` → `GET /wiki/api/v2/spaces`
- `POST /api/v1/confluence/search` → `GET /wiki/rest/api/search` (v1; CQL)
- `POST /api/v1/confluence/execute` → bounded `GET`-only regex-allowlisted passthrough (mirrors Jira)

**Pros**:
- Exact mirror of `/api/v1/jira/*` shape. Reviewers, tests, audit logs, and operators see the same nouns.
- Path validator regex is straightforward and aligns with Jira's `validate_jira_api_path`.
- Future write verbs (`page/create`, `page/update`, `comment/create`) drop in symmetrically.
- The MCP-style names live in the **wrapper subcommand layer**, where consumer porting is easy — they don't bleed into URL design.

**Cons**:
- Two name shapes (gateway path vs wrapper subcommand) to keep aligned in docs.

#### A2. URL paths that literally mirror MCP names (`/api/v1/confluence/getConfluencePage`, `/api/v1/confluence/searchConfluenceUsingCql`, …)

**Approach**: One route per MCP tool, with the URL path equal to the MCP name.

**Pros**:
- One mental model: the URL path is the MCP tool name.

**Cons**:
- Diverges from `/api/v1/jira/*` which uses verb-noun. Inconsistent gateway URL conventions.
- camelCase URLs are unusual for REST and complicate path-validator regexes.
- Future write verbs would have to match the (yet-to-exist) MCP write tool names; harder to plan.

#### A3. Verb-as-payload (`POST /api/v1/confluence` with `{verb: "getConfluencePage", ...}`)

**Approach**: Single endpoint dispatching on `verb` field.

**Pros**: Smallest URL diff.

**Cons**: Loses route-level audit / mode-gate granularity that the Jira surface relies on; harder to reason about per-verb tests; rejected.

### Atlassian API version

#### B1. v2-first hybrid: v2 for reads, v1 for CQL search and known-buggy comment endpoints — **preferred**

**Approach**: Pin every endpoint per-verb in the client (`get_page` → v2, `search_cql` → v1, `get_inline_comments` → v2 with v1 fallback if 404). The wrapper exposes a single API to the agent regardless of which Atlassian version backs each verb.

**Pros**:
- Best response shape per verb (v2 for cursor pagination, v1 for the only working CQL).
- Migration-friendly: when Atlassian closes the v1 search gap, we flip a single line.
- Matches the [community-recommended hybrid](https://community.atlassian.com/forums/Confluence-questions/Confluence-API-v1-versus-v2/qaq-p/2978171).

**Cons**:
- Two response shapes to wrap (v1's `_links.next` vs v2's cursor + Link header).

#### B2. v1-only

**Pros**: One shape, fewer code paths.

**Cons**: Atlassian is steering integrations toward v2. v1 read endpoints are slated for eventual deprecation (≥6 months notice but still on the roadmap). Locks us into legacy.

#### B3. v2-only

**Pros**: Future-proof on read endpoints.

**Cons**: Breaks `searchConfluenceUsingCql` — there is no v2 search. Rejected for v1.

### CQL scope extraction

#### C1. Conservative static space-scope extractor — **preferred** (mirrors Jira's JQL extractor)

**Approach**: `gateway/confluence_search.py` accepts CQL only if it matches a narrow shape that statically proves the space is allowlisted. Accepted forms (top-level, AND-combined only):

- `space = KEY` (bare uppercase key)
- `space IN (K1, K2, ...)` with every key in `confluence.spaces`

Rejected (deny-on-ambiguity, with a recorded reason):

- No `space` clause.
- `space` under `OR`.
- Quoted space keys.
- CQL functions (`currentUser()`, `recentlyViewedContent()`, etc.).
- `id =` clauses without `space =`.
- Unicode homoglyph / mixed-script keys.

**Pros**:
- Direct port of Jira's `extract_search_projects()` logic — single semantic model for operators ("scope must be statically provable").
- Hard-rejects adversarial CQL.
- Audit entry records `spaces_extracted` on acceptance.

**Cons**:
- Some legitimate CQL patterns (e.g., `(space = ENG OR space = DOCS)` for a known multi-space search) will be rejected. Workaround: agent issues two single-space searches and merges.

#### C2. Permissive CQL with post-hoc filter

**Approach**: Pass arbitrary CQL through; filter results so only allowlisted-space hits return.

**Pros**: Full CQL flexibility.

**Cons**: Rejected — Atlassian's response counts and pagination would still leak result counts from non-allowlisted spaces; the JQL analogue was rejected in #1556 for the same reason.

### Comment retrieval — handling v2 quirks

#### D1. v2-first with v1 fallback on 404 / nested-reply gap — **preferred**

**Approach**:
- `getConfluencePageFooterComments` calls v2; if the response is missing nested replies and the agent asked for them (a `?include_replies=true` flag), the gateway fetches `/wiki/api/v2/footer-comments` filtered by `pageId` to get the full tree.
- `getConfluencePageInlineComments` calls v2; if v2 returns 404 (the known bug), the gateway transparently retries v1 (`/wiki/rest/api/content/{id}/child/comment?location=inline`) and returns the v1 response under a normalized envelope.

**Pros**: Agents get correct data without knowing about Atlassian bugs.

**Cons**: Two endpoint families per comment verb to maintain. Test coverage doubles.

#### D2. v2-only, document the gaps

**Pros**: Simpler.

**Cons**: Pushes the bug onto agents; refine/plan workflows that quote inline comments will silently miss content.

#### D3. v1-only for comments

**Pros**: One endpoint family, no fallback complexity.

**Cons**: Locks comments into legacy when v2 is fixed. We'd have to migrate later.

### Body format default

#### E1. Both `storage` (Confluence XHTML) and `atlas_doc_format` (ADF JSON) — **preferred**

**Approach**: Set default `body-format=storage,atlas_doc_format` (Confluence v2 accepts a comma list) so the agent gets both: storage format for HTML-like display / parsing, ADF for structured tree access. Mirrors Jira's default-`expand=renderedBody,renderedFields`. Caller may override.

**Pros**:
- Agent doesn't need to make a second call for the alternate format.
- Storage is human-readable HTML-ish; ADF is the structured form. Both have legitimate uses.

**Cons**:
- Larger response payloads. If body size becomes a problem, callers can override.

#### E2. Storage only

**Pros**: Smallest payload.

**Cons**: Loses programmatic access for agents that want to traverse the document tree.

#### E3. ADF only

**Pros**: Most-structured.

**Cons**: Hard to read in transcripts / logs; rendering work pushed to the agent.

#### E4. View (rendered HTML) only

**Pros**: Easiest to render directly in human-facing UI.

**Cons**: Lossy — view doesn't always preserve macro inputs / layout structure that storage and ADF do.

### Tenant config — credential sharing with Jira

#### F1. Share Atlassian credentials between Jira and Confluence (single set of secrets) — **preferred**

**Approach**: Add a shared `ATLASSIAN_BASE_URL` / `ATLASSIAN_USERNAME` / `ATLASSIAN_API_TOKEN` triple. `gateway/jira_credentials.py` and `gateway/confluence_credentials.py` both read from it. Existing `JIRA_*` env names stay supported for back-compat with #1556 deployments (loader prefers `ATLASSIAN_*` if present, else falls back to `JIRA_*` or `CONFLUENCE_*` per-service).

The Jira and Confluence base URLs differ slightly (`https://co.atlassian.net` vs `https://co.atlassian.net/wiki`) — the gateway can derive the Confluence base by appending `/wiki` if `CONFLUENCE_BASE_URL` is unset, or accept both as overrides.

**Pros**:
- Operators provision one Atlassian bot account and one API token, not two.
- Reflects reality (same Atlassian Cloud tenant, same OAuth/API-token surface).
- Aligns with the issue text: "Shares credential infrastructure with the Jira gateway (same Atlassian tenant)."

**Cons**:
- Slightly more loader logic (prefer-shared-then-fall-back).
- Migration path required for installs that already populated `JIRA_*`-only.

#### F2. Independent credential blocks (status quo of the template file)

**Approach**: Keep `JIRA_*` and `CONFLUENCE_*` triples fully independent. Operators populate both with the same value if their tenant is shared.

**Pros**: Smallest code change. Allows fully separate Atlassian accounts per service if any operator wants that (e.g., split read-only bots).

**Cons**:
- Duplicates secrets in `secrets.env`; operators must remember to update both on token rotation.
- Doesn't reflect the issue text's intent.

### Network-mode gating — same decorator

#### G1. Apply `@require_private_mode` to every `/api/v1/confluence/*` route — **preferred (and effectively mandatory)**

**Approach**: Identical to Jira. Each handler chains `@require_session_auth → @require_private_mode → handler body`. The route-enumeration regression test in `gateway/tests/test_confluence_routes.py` walks `app.url_map` and asserts every `/api/v1/confluence/*` view has `__egg_requires_private_mode__ = True`.

**Pros**: Uniform. Already proven in #1556. The decorator is generic — no Confluence-specific change needed.

**Cons**: None — this is the "same as Jira" case.

(Alternatives like blueprint-level `before_request` were rejected in #1556 and for the same reasons here: gateway doesn't use blueprints today.)

### Space allowlist location

#### H1. New `confluence:` section in `config/context-filters.yaml` — **preferred** (mirrors Jira's `jira:` section)

**Approach**:

```yaml
confluence:
  spaces: ["ENG", "PLATFORM"]   # Atlassian space keys agents may read
```

- Authoritative key is `spaces` (parallel to Jira's `projects`).
- Default is empty list — every Confluence call rejected until an operator populates. "Installed but inert" v1 rollout posture.
- Fail-closed: missing file / missing section / malformed YAML → empty set, no crash.
- Mtime reload + `POST /api/v1/config/reload` hook (same as Jira).

**Pros**: Operators already edit this file for Jira and GitHub filtering — one allowlist surface. Self-contained from Jira.

**Cons**: `context-filters.yaml` was authored for a syncer; we'd be adding to it. Tolerable, given Jira already did so.

#### H2. New `config/confluence.yaml`

**Pros**: Clean separation.

**Cons**: Yet another config file. Rejected for the same reason as the Jira analogue.

#### H3. Env var (`CONFLUENCE_SPACE_KEYS`) — already in the secrets template

**Approach**: Use the existing `CONFLUENCE_SPACE_KEYS` placeholder as the allowlist source.

**Pros**: Already scaffolded.

**Cons**: Mixing secrets and policy in one file. Jira chose YAML for a reason; Confluence should follow.

### Future-write extension shape

The v1 design needs to leave room for the future write verbs without re-architecting. Recommended layout (informational — not implemented in v1):

- `POST /api/v1/confluence/page/create` → `POST /wiki/api/v2/pages` (new narrow route).
- `POST /api/v1/confluence/page/update` → `PUT /wiki/api/v2/pages/{id}` (Confluence's edit endpoint is `PUT`, distinct from Jira's POST-edit). Path validator must allow `PUT` for this single path family in the future-writes phase, while keeping `DELETE` permanently denied.
- `POST /api/v1/confluence/comment/create` → `POST /wiki/api/v2/footer-comments` and / or `POST /wiki/api/v2/inline-comments`.

All three land under the same `@require_session_auth → @require_private_mode → space-allowlist` chain. The `/execute` regex stays GET-only forever — future writes always go through narrow routes.

Permanent denylist (path validator) — never permitted, even when writes land:

- Anything matching `restrictions`, `permissions`, `space.admin`, `users` (we're not editing principals).
- HTTP `DELETE` (no archive / no purge from agents).
- Page move / lineage-changing endpoints.

## Recommended Approach

Adopt **A1 + B1 + C1 + D1 + E1 + F1 + G1 + H1**:

1. **Endpoint surface (A1)** — `/api/v1/confluence/{page,space,search,execute}/...` paths in the gateway, `mcp__confluence__*`-aligned subcommand names in the sandbox wrapper. Mirrors Jira; provides porting ergonomics for `mcp__confluence__*` callers without warping URL conventions.
2. **API version (B1)** — v2-first hybrid; pin per-verb. `searchConfluenceUsingCql` stays on v1 (no v2 equivalent). Inline / nested comments use v1 fallback for the known v2 bugs.
3. **CQL scope (C1)** — conservative static `space =` / `space IN (...)` extractor; deny-on-ambiguity. Direct port of `gateway/jira_search.py`.
4. **Comment quirks (D1)** — v2-first with transparent v1 fallback when v2 misses nested replies / 404s on inline comments. The gateway hides the bug; agents get correct data.
5. **Body format (E1)** — default `body-format=storage,atlas_doc_format`. Caller may override.
6. **Credential sharing (F1)** — shared `ATLASSIAN_BASE_URL` / `_USERNAME` / `_API_TOKEN` with backward-compat fall-back to existing `JIRA_*` / `CONFLUENCE_*` placeholders. One bot account for the tenant.
7. **Network-mode gate (G1)** — `@require_private_mode` on every Confluence route, plus a route-enumeration regression test asserting the marker.
8. **Space allowlist (H1)** — new `confluence.spaces:` section in `config/context-filters.yaml`, fail-closed, mtime-reloaded, hooked into `POST /api/v1/config/reload`.

Ancillary:

- **No `EGG_CONFLUENCE_*` env var in v1.** The issue is explicit. Rationale: Confluence is reference material; the agent doesn't operate on a single page as its unit of work. The audit log can still record `pageId` / `spaceKey` from each request body for reconciliation.
- **Audit**: every Confluence op logs `verb`, `pageId` (when present), `spaceKey` (extracted), `session_mode`, `pipeline_id`, `agent_role`, `bot_account`. Same shape as Jira's audit entries.
- **Squid**: do **not** add `*.atlassian.net` (already excluded; verified by existing regression test).
- **404 envelope**: page-get / page-descendants / comment-get all return `{"status": "not_found", "id": "...", "upstream_status": 404}` on upstream 404, mirroring Jira's `not_found` envelope. CQL search and `/execute` surface upstream 404 as real errors.
- **Tests**: gateway unit tests with `httpx` mocks + private-mode enforcement tests + space-allowlist tests + route-enumeration regression + adversarial CQL suite + v2-comment-fallback test. Sandbox wrapper smoke tests assert `EGG_SESSION_TOKEN` and gateway URL fall-through.
- **Docs**: new `docs/reference/confluence-wrapper.md` (mirrors `docs/reference/jira-wrapper.md`); update `docs/architecture/network-isolation.md` (add `/api/v1/confluence/*` to endpoint table); update `docs/architecture/credential-injection.md` (extend the Atlassian section to cover Confluence); update `sandbox/agent-config/rules/environment.md` (mention `confluence` wrapper alongside `jira` and `gh`).
- **Future-write readiness**: `page/create`, `page/update`, `comment/create` plug in as three new narrow routes under the same plumbing. `PUT` allowed only for `pages/{id}` in the writes phase. `DELETE`, restrictions, permissions, space-admin verbs permanently denied.

## Complexity Assessment

**medium** — multi-file change across `gateway/`, `sandbox/scripts/`, `config/`, and docs, with a clean analogue (`/api/v1/jira/*`) to follow line-by-line. Slightly more nuance than the Jira wrapper because of the Confluence v1 / v2 hybrid and the comment-fallback logic, but no architectural departure — all reused infrastructure was generalised by #1556.

## Open Questions

**All questions below are registered as contract decisions / feedback items via the `mcp__sdlc__*` MCP tools (see "Registered decisions" section below). They are reproduced here for reviewer legibility; the authoritative copy is in the contract.**

### Registered decisions (multiple-choice)

- [ ] **Option A (Recommended)**: Endpoint surface — Jira-style verb-noun paths (`/api/v1/confluence/page/get`, `/api/v1/confluence/search`, …); MCP-aligned names live in the sandbox wrapper subcommand layer.
- [ ] **Option B**: Literal MCP-name URL paths (`/api/v1/confluence/getConfluencePage`, `/api/v1/confluence/searchConfluenceUsingCql`, …).
- [ ] **Option C**: Single endpoint with `verb` payload field.

- [ ] **Option A (Recommended)**: API version strategy — v2-first hybrid (v2 reads + v1 CQL search + v1 fallback for known v2 comment bugs).
- [ ] **Option B**: v1-only across the board (simpler shape but legacy).
- [ ] **Option C**: v2-only across the board (no CQL search would be possible — would require shipping our own search index).

- [ ] **Option A (Recommended)**: CQL scope extraction — conservative static `space =` / `space IN (...)` extractor; deny-on-ambiguity.
- [ ] **Option B**: Permissive CQL with post-hoc result filter.

- [ ] **Option A (Recommended)**: Comment-quirk handling — v2-first with transparent v1 fallback for nested replies / inline-404 bugs.
- [ ] **Option B**: v2-only; document the gaps and let agents retry.
- [ ] **Option C**: v1-only for comments; lock in legacy behaviour until Atlassian fixes v2.

- [ ] **Option A (Recommended)**: Body format default — both `storage` and `atlas_doc_format`.
- [ ] **Option B**: `storage` only.
- [ ] **Option C**: `atlas_doc_format` only.
- [ ] **Option D**: `view` (rendered HTML) only.

- [ ] **Option A (Recommended)**: Credential sharing — shared `ATLASSIAN_BASE_URL` / `_USERNAME` / `_API_TOKEN` triple, with backward-compat fall-back to `JIRA_*` and `CONFLUENCE_*` placeholders.
- [ ] **Option B**: Independent `JIRA_*` and `CONFLUENCE_*` triples (status quo).

- [ ] **Option A (Recommended)**: Network-mode gate — `@require_private_mode` per route, with a route-enumeration regression test.
- [ ] **Option B**: Move to a Flask blueprint with `before_request` (would also require migrating Jira; out of scope here).

- [ ] **Option A (Recommended)**: Space allowlist location — new `confluence.spaces:` section in `config/context-filters.yaml`.
- [ ] **Option B**: Dedicated new `config/confluence.yaml`.
- [ ] **Option C**: `CONFLUENCE_SPACE_KEYS` env var in `secrets.env` (the placeholder already exists).

- [ ] **Option A (Recommended)**: Bot identity — same dedicated Atlassian bot account used for Jira (single principal owns both `read:jira-work` and `read:page:confluence`-equivalent access).
- [ ] **Option B**: Separate bot accounts per service.
- [ ] **Option C**: Reuse the operator's personal Atlassian account.

- [ ] **Option A (Recommended)**: Audit-log redaction — strip `accountId`, `emailAddress`, and `_links.webui` user-profile URLs from responses before they reach the sandbox (parallels Jira's redaction stance).
- [ ] **Option B**: Pass responses through verbatim.

- [ ] **Option A (Recommended)**: `getConfluenceSpaces` — filter response so only allowlisted spaces are returned (agents cannot enumerate the full tenant space set).
- [ ] **Option B**: Pass through Atlassian's full space list and rely on per-page allowlist to deny later access.

- [ ] **Option A (Recommended)**: `attachments` endpoints — keep them on the **permanent denylist** for v1 and future writes (Confluence attachments are arbitrary-file uploads / downloads with an unbounded payload surface; if needed later, scope them as a separate ticket).
- [ ] **Option B**: Defer the decision to the future-writes phase.
- [ ] **Option C**: Allow read-only attachment metadata (no body) in v1.

- [ ] **Option A (Recommended)**: `EGG_CONFLUENCE_*` env vars — none in v1 (matches issue text). Audit recovers `pageId` / `spaceKey` from each request body.
- [ ] **Option B**: Ship `EGG_CONFLUENCE_PAGE` / `EGG_CONFLUENCE_SPACE` as observational env vars (parallel to `EGG_JIRA_TICKET`).

- [ ] **Option A (Recommended)**: `/execute` passthrough — include it (GET-only, regex-allowlisted), parallel to Jira's escape hatch for read verbs not yet promoted to narrow routes.
- [ ] **Option B**: Skip `/execute` entirely; add narrow routes only.

### Registered open-ended questions (free-form)

- Which Atlassian Confluence spaces should be on the v1 allowlist (space keys, comma-separated)?
- What is the expected request volume per pipeline (peak CQL searches/min, page reads/min, comment reads/min)? This informs rate-limit defaults and 429 retry behaviour.
- Are there custom Confluence fields, macros, or page properties known to hold PII or secrets that should be redacted before sandbox-visible responses (in addition to the `accountId` / `emailAddress` defaults)?
- For the future write phase (out of scope here), should the gateway enforce idempotency on `comment/create` (refuse duplicate within N seconds) or leave that to Atlassian's own semantics? (Same question as Jira; surfacing now so the design-once decision is consistent.)
- What pageId / page-link patterns should the orchestrator extract from Jira tickets to feed Confluence reads? (E.g., should it scan ticket descriptions for `https://<tenant>.atlassian.net/wiki/spaces/<KEY>/pages/<ID>/...` and pre-resolve, or leave URL parsing to the agent?) — This informs whether we need a `page/resolve-by-url` verb in v1.
- Should `getConfluencePageDescendants` enforce a maximum depth (e.g., 3) to prevent runaway responses on deeply nested page trees, or pass Atlassian's parameter through verbatim?
- Should the audit log treat reads of pages whose space is *technically* in the allowlist but whose page hierarchy is restricted at the Atlassian permission layer as a special audit category (e.g., `confluence_upstream_403` separate from `confluence_upstream_error`) — useful for operators tuning the bot account's access?
- For long-lived pages with extensive version history: should `getConfluencePage` default to `version=current` only (recommended) or expose a `version` query parameter? Either way, history-listing endpoints stay out of scope for v1.
- The host-side `mcp__confluence__*` MCP authenticates as the consenting human user; the gateway will authenticate as a bot. Are there documents in operator workspaces that the human reads but the bot would not be able to (or vice versa)? If so, do we need a "diagnostic" command that surfaces the bot's effective access per space?
- Should the sandbox `confluence` wrapper expose the MCP-style alias names (`confluence get-page`, `confluence search-cql`) **in addition to** Jira-style subcommands (`confluence page get`, `confluence search`), or pick one shape only?

---

*Authored-by: egg*
