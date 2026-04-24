# Analysis: Add Jira gateway support with credential injection

> Issue: #1556 | Phase: refine

## Problem Statement

Sandboxed egg agents today have no way to read Jira tickets. The host-side `mcp__confluence__*` MCP (which bundles Jira) is unusable from the sandbox because (1) the MCP runs in the host Claude Code process and is unreachable from isolated agent containers, and (2) it exposes the owning human's full Atlassian API surface (writes, transitions, worklogs) with no project or verb allowlist — a direct contradiction of egg's "infrastructure beats config" security thesis.

Issue #1557 (Jira-epic SDLC pipelines) and any future workflow that needs to cite a ticket, read its description, or search across a project is blocked on this. #1556 is the infrastructure-only v1: **read-only** Jira access for sandboxed agents, delivered through the existing gateway sidecar, mirroring the `gh` wrapper pattern, with Atlassian credentials held exclusively in the gateway.

Desired outcome:

1. Sandboxed agents can view a ticket, search by JQL, and read comments via the gateway.
2. Atlassian credentials **never** enter the agent container (zero-credential invariant preserved — see [`docs/architecture/credential-injection.md`](../../docs/architecture/credential-injection.md)).
3. Jira endpoints are only reachable when the agent session is in **private network mode** (see [`docs/architecture/network-isolation.md`](../../docs/architecture/network-isolation.md)); in public mode the gateway fails closed.
4. Policy is keyed on project allowlist + verb allowlist — agents cannot hit projects or verbs they were not granted.
5. v1 endpoints, policy, and credential scopes are shaped so the future write verbs (create ticket, update ticket, create comment) drop in as pure extensions. Transitions, worklogs, attachments, and deletions are **out of scope ever**.
6. An `EGG_JIRA_TICKET` env var identifies the ticket the agent is operating on, analogous to `EGG_REPO`.

## Current Behavior

### Gateway sidecar as choke point

The gateway (Python Flask app, [`gateway/gateway.py`](../../gateway/gateway.py)) is the single authenticated exit point for sandboxed agents. Today it fronts:

- `/v1/messages` — Anthropic API proxy with credential injection (`gateway/anthropic_credentials.py`).
- `/api/v1/git/*` — git push + branch management with ownership / protected-branch policy.
- `/api/v1/gh/*` — PR create / comment / edit / close / execute with per-repo private-mode, auth-mode, and PR-ownership checks ([gateway.py:2385–3262](../../gateway/gateway.py)).
- `/api/v1/checkpoints/*` — checkpoint read/write.
- `/api/v1/phase/*`, `/api/v1/contract/*`, `/api/v1/progress/*` — SDLC state-machine APIs.

Key building blocks we would reuse:

| Mechanism | File | What it does |
|-----------|------|--------------|
| Per-container session auth | `gateway/auth.py` `require_session_auth` | Validates `Authorization: Bearer <EGG_SESSION_TOKEN>`, loads `Session` into Flask `g`, exposes `g.session_mode`, `g.session_phase`. |
| Session model | `gateway/session_manager.py` `Session` | `mode: Literal["private","public"]`, `phase`, `issue_number`, `agent_role`, `pipeline_id`. |
| Private-mode gate | `gateway/private_repo_policy.py` `check_private_repo_access` | Per-operation repo visibility check. `session_mode == "private"` ⇒ locked-down network + private repos only. |
| Phase filtering | `gateway/phase_filter.py` `filter_operation` | Blocks ops by phase (e.g. `gh pr create` only in `pr` phase). |
| Audit logging | `gateway/gateway.py` `audit_log(...)` | Structured JSON logs per op with outcome, reason, session mode. |
| Credential loading | `gateway/anthropic_credentials.py` (pattern) | Reads `~/.config/egg/secrets.env` with mtime-based cache refresh. |
| Sandbox CLI wrapper | `sandbox/scripts/gh` | `curl $GATEWAY_URL/api/v1/gh/...` with `EGG_SESSION_TOKEN`, path-translation, JSON parsing. |

### Existing Jira footprint

Partial scaffolding is already in place but unused:

- `config/secrets.template.env:106-109` defines placeholder `JIRA_BASE_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`, `JIRA_JQL_QUERY`. Nothing reads them today.
- `sandbox/agent-config/rules/environment.md:41` mentions `~/context-sync/` as an optional RO cache of Confluence/JIRA content (not a live API).
- `context-filters.yaml` (referenced in `config/README.md:252`) is described as an allowlist of Confluence spaces, JIRA projects, and repositories that get synced — the syncer is out of scope here, but the file is a natural home for a Jira **project allowlist**.
- `orchestrator/routes/pipelines.py:10351` sets `EGG_REPO` in the sandbox env from `pipeline.repo`. `EGG_JIRA_TICKET` would follow the same pattern.

### How `session_mode` encodes "private network mode"

The issue distinguishes "private network mode" for the Jira route. In the current codebase this is **not** a separate concept: the single `PRIVATE_MODE` flag (`gateway/private_repo_policy.py:74-77`) couples (a) network lockdown (Anthropic-only egress, Squid allowlist) and (b) private-repo-only access. `session_mode == "private"` therefore means the container is already in the locked-down network posture. That is the natural gate for Jira — Jira carries internal KA data and must not be reachable from agents running untrusted external work (public mode).

### How Confluence MCP fits today

`mcp__confluence__*` runs on the host, inside the human's Claude Code session, with OAuth 2.0 creds stored in `~/.claude.json`. It is available for host-side interactive work and for issue-triage scripts run by the human, but it is **not** available to sandboxed agents — those containers have no credentials, the MCP process is not reachable across the k8s NetworkPolicies, and running the MCP inside the sandbox would violate the zero-credential invariant. The issue explicitly notes that proxying the host MCP through the gateway is functionally equivalent to building this wrapper.

## Constraints

**Security / architectural:**

- Zero credentials in the sandbox container (hard invariant — see `docs/architecture/credential-injection.md`). Atlassian creds must live only in the gateway process.
- `GITHUB_TOKEN` is already excluded from the sandbox; same rule applies to `JIRA_API_TOKEN`.
- All requests from the sandbox must flow through the gateway — `*.atlassian.net` must **not** be added to the Squid domain allowlist, or containers could bypass policy (same reasoning as GitHub in `docs/architecture/network-isolation.md:86`).
- Read-only in v1. The gateway must refuse Jira write verbs even if the upstream API would accept them. Enforcement is at the gateway (infrastructure), not in agent instructions.
- Private-mode-only: public-mode sessions must get a 403 on any `/api/v1/jira/*` endpoint. This must be enforced at the route layer, not left to downstream policy.
- Project allowlist + verb allowlist. Agents can only query projects the operator has sanctioned and only with verbs from the configured set.
- Future-verb compatibility: v1 policy/endpoint/credential design must support `ticket create`, `ticket update`, `comment create` as drop-in additions — no re-architecting.
- Policy must explicitly and permanently deny transitions, worklogs, attachments, deletions (even after writes land).

**Operational:**

- Credential lifecycle: Atlassian API tokens don't auto-rotate. Whichever auth style we pick, the gateway needs a reload story (the existing `secrets.env` mtime-based cache refresh is usable if we reuse that pattern).
- Single-tenant for v1 is acceptable but multi-tenant should not be architected out (egg is increasingly run across multiple repos and operators may have multiple Atlassian sites).
- Test strategy: there is no Atlassian API fixture library in-tree. Gateway tests today mock upstream GitHub with `responses` / `pytest` monkeypatching; we would need equivalent fixtures for Jira.
- Rate limiting: the gateway currently defers to GitHub's rate limiter. Atlassian REST has per-site per-user quotas that are less predictable; logging + backoff should be in scope for v1.
- k8s deployment: `k8s/base/gateway-deployment.yaml` mounts `secrets.env`; adding Jira secrets is a config-only change if we reuse that volume.

**Dependencies / coupling:**

- Issue [#1557](https://github.com/jwbron/egg/issues/1557) depends on this ticket (and hints at `editJiraIssue` / `createJiraIssue` verbs for the future-writes scope). The v1 endpoint names should not collide with the verbs #1557 expects.
- Issue [#1554](https://github.com/jwbron/egg/issues/1554) (closed; split origin) framed the broader Jira-triggered SDLC flow.

**External (Atlassian):**

- `/rest/api/3/search` was removed from Jira Cloud; the current search verb is `/rest/api/3/search/jql` (GET or POST), with cursor pagination via `nextPageToken` ([Atlassian docs — Issue Search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)). There are known pagination defects in the new endpoint that we should be aware of when designing list endpoints.
- Read-only granular OAuth scopes exist (`read:issue-details:jira`, `read:jira-work`, `read:jira-user`) that would match v1 ([Atlassian docs — REST API intro](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/)).
- Basic auth with API token uses `email:token` BASE64'd; endpoint is `https://<tenant>.atlassian.net/rest/api/3/...` ([Atlassian docs — Basic Auth](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/)).
- OAuth 2.0 3LO uses `https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3/...`, supports rotating refresh tokens, supports granular scopes, but requires a consent flow and — importantly — acts on behalf of the consenting **user**, with all friction that implies for a bot identity ([Atlassian docs — OAuth 2.0 3LO](https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/)).
- Atlassian's own current docs steer integrations toward Forge / Connect apps and explicitly caution about custom 3LO apps / API tokens for production. Neither Forge nor Connect is a fit for a gateway sidecar talking REST.

## Options Considered

### Client shape

#### A1. REST-only gateway endpoints (mirrors `/api/v1/gh/*`) — **preferred**

**Approach**: `gateway/jira_client.py` talks directly to the Atlassian REST API with `httpx`. Gateway exposes `/api/v1/jira/ticket/get`, `/api/v1/jira/search`, `/api/v1/jira/ticket/comments`, and a filtered `/api/v1/jira/execute` passthrough (path/verb-allowlisted). Sandbox ships a thin `sandbox/scripts/jira` wrapper that `curl`s those endpoints with `EGG_SESSION_TOKEN`.

**Pros**:
- Exact mirror of `/api/v1/gh/*` — reviewers, policy filters, audit logging, session-mode/phase plumbing already fit.
- No external binary to bundle / scan / sign. Supply chain surface is `httpx` (already in gateway).
- Response shape is under our control — we can redact fields (assignee emails, attachment URLs) before they ever reach the sandbox.
- Trivial to extend for the three future verbs without changing the wire protocol.

**Cons**:
- We hand-roll request/response marshalling instead of leaning on a library. Low risk for the v1 surface (three read verbs) but grows with scope.
- Atlassian's search pagination quirks (nextPageToken bugs) land on us, not a library maintainer.

#### A2. Bundle a Jira CLI (`jira-cli`, `go-jira`) in `sandbox/scripts/jira`

**Approach**: Ship a CLI binary in the sandbox image. The wrapper shells out to it and relays stdout. Gateway still holds creds, so either the binary reaches the gateway (duplicate of A1) or we break the zero-credential rule.

**Pros**:
- Someone else maintains the argument parsing and response formatting.

**Cons**:
- Either credentials end up in the sandbox (rejected — violates the zero-credential invariant), or the CLI must be modified to call the gateway (which defeats the "use an existing CLI" argument).
- Supply-chain burden: new binary to pin, scan, and update inside the container image.
- Binary argument surface is wider than we want to expose; we'd still need a verb allowlist on top.

### Auth flavor

#### B1. Atlassian Cloud API token (email + token, Basic) — **preferred for v1**

**Approach**: Gateway reads `JIRA_BASE_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` from `~/.config/egg/secrets.env` (placeholders already exist). Per-request header: `Authorization: Basic <base64(email:token)>`. Target endpoint: `https://<tenant>.atlassian.net/rest/api/3/...`.

**Pros**:
- Matches the shape already scaffolded in `secrets.template.env`.
- No user-consent flow; creds are bootstrappable from a headless / CI context.
- Dedicated bot Atlassian account yields clean audit attribution (all gateway actions attributed to the bot user, not a human).
- Compatible with the existing `secrets.env` mtime-based hot-reload used for Anthropic creds.

**Cons**:
- No granular scopes — the token inherits the bot account's permissions. Mitigated by making the bot account low-privilege (read-only role on the allowlisted projects).
- Manual rotation (we can reuse the same "edit secrets.env, kick reload" pattern).
- Atlassian current docs mildly discourage API tokens for production integrations.

#### B2. OAuth 2.0 3LO

**Approach**: Gateway maintains an app registration in the Atlassian developer console; stores `access_token` + `refresh_token`; refreshes via the OAuth token endpoint. Scopes = `read:jira-work`, `read:jira-user` (plus granular `read:issue-details:jira` variants) for v1.

**Pros**:
- Granular, revocable scopes per Atlassian best practice.
- Rotating refresh tokens reduce compromise window.
- Same auth model as the host `mcp__confluence__*` MCP; consistency when operators debug.

**Cons**:
- OAuth acts on behalf of a user, not a bot. Harder to get a dedicated bot identity; attribution is to the consenting human.
- Requires a consent UI flow at setup — awkward for CI / headless deployment.
- More moving pieces for v1 (token store, refresh scheduler, failure handling on expired refresh tokens).
- JQL entity-properties caveat (noted in Atlassian docs) and some scope ergonomics issues.

#### B3. Both — pluggable auth

**Approach**: Put auth behind a strategy interface; ship B1 in v1, leave the door open for B2.

**Pros**:
- Low extra code at v1 if we keep the seam narrow.
- Lets operators pick per deployment.

**Cons**:
- Premature abstraction risk if we don't actually ship B2 soon.
- Two code paths to test and document.

### Network-mode gating

#### C1. Per-route inspection of `g.session_mode` — **preferred**

**Approach**: Each `/api/v1/jira/*` handler starts with:
```python
if getattr(g, "session_mode", None) != "private":
    audit_log("jira_denied_public_mode", ...)
    return make_error("Jira endpoints are private-mode only", status_code=403)
```
Matches how `GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE` and the existing gh endpoints check `session_mode`.

**Pros**: Uniform with existing endpoints; straightforward to unit-test; fails closed.

**Cons**: Must remember to add the check to every new Jira route — easy to forget. Mitigated by a helper decorator `@require_private_mode` and by test coverage.

#### C2. Module-level on Blueprint/sub-app

**Approach**: Register Jira routes under a Flask blueprint whose `before_request` rejects non-private sessions.

**Pros**: One enforcement point; can't be forgotten per-route.

**Cons**: Gateway doesn't use blueprints today (`gateway.py` is a flat route file). Introducing one only for Jira adds structural inconsistency.

#### C3. Deny at `require_session_auth` via route tagging

**Approach**: Extend `require_session_auth` to accept `required_mode="private"` and apply it to the Jira routes.

**Pros**: Single decorator captures both auth and network-mode gate. Reusable for future private-only endpoints.

**Cons**: Mild refactor of an existing decorator that is used widely. Worth doing only if we expect more private-only endpoints.

### Endpoint surface

#### D1. Verb-specific narrow routes + generic `execute` — **preferred**

- `POST /api/v1/jira/ticket/get` — `{"ticket": "FOO-123", "fields": [...]}` → GET `/rest/api/3/issue/{key}`
- `POST /api/v1/jira/search` — `{"jql": "...", "fields": [...], "nextPageToken": "..."}` → `/rest/api/3/search/jql` (POST)
- `POST /api/v1/jira/ticket/comments` — `{"ticket": "FOO-123"}` → GET `/rest/api/3/issue/{key}/comment`
- `POST /api/v1/jira/execute` — bounded passthrough that parses `{method, path, query, body}` and validates: method in `{GET}`, path matches a regex allowlist (mirrors `validate_gh_api_path` in `gateway/github_client.py`).

**Pros**: Verb allowlist lives in code (narrow routes) and in data (passthrough regex). Easy to reason about. Extensible: add `ticket/create`, `ticket/update`, `comment/create` as three new narrow routes when writes land.

**Cons**: More endpoints than "one big passthrough." Intentional — we want narrow contracts.

#### D2. Single `/api/v1/jira/execute` passthrough

**Pros**: Smallest code diff.

**Cons**: Harder to audit / reason about / limit ADF payload handling / redact sensitive fields. Also makes v2 verb-extension ambiguous. Rejected.

### Tenant config & project allowlist

#### E1. Repurpose `context-filters.yaml` + gateway env — **preferred**

**Approach**:
- **Instance URL**: `JIRA_BASE_URL` in `secrets.env` (already there).
- **Project allowlist**: reuse `config/context-filters.yaml` Jira section (that file already exists for syncer-style filtering per `config/README.md:252`) or add a `projects:` key under a new `jira:` section. Loaded into the gateway at startup with mtime reload.
- **Verb allowlist**: hard-coded v1 (three narrow routes + path regex for `execute`). Future writes add three more verbs.
- **Sandbox env**: launcher exports `EGG_JIRA_TICKET` (and optionally `EGG_JIRA_PROJECT`) when the pipeline was started from a Jira trigger. Orchestrator already has the slot (`orchestrator/routes/pipelines.py:10347-10351` for EGG_REPO) — mirror that.

**Pros**: Reuses a file operators already edit; unifies Confluence/Jira/repo filtering.

**Cons**: `context-filters.yaml` was authored for a syncer; we'd be overloading it. Tolerable if we scope to a dedicated `jira:` section.

#### E2. New `config/jira.yaml`

**Pros**: Clean separation.

**Cons**: Yet another config file operators must know about.

## Recommended Approach

Adopt **A1 + B1 + C1 + D1 + E1**:

1. **Client shape (A1)** — REST-only gateway endpoints in `gateway/jira_client.py`, with a thin `sandbox/scripts/jira` wrapper. Mirrors the `gh` pattern exactly.
2. **Auth (B1)** — Atlassian Cloud API token (email + token), loaded from `secrets.env`. Optimises for a bot identity, a headless setup flow, and parity with the scaffolding already in `secrets.template.env`. B2 (OAuth 2.0 3LO) remains the likely v2 — we will keep the client's auth plumbing narrow enough that a strategy swap is a single-file change.
3. **Network-mode gate (C1)** — per-route `session_mode == "private"` check, with a small `@require_private_mode` decorator to prevent regressions and centralise the audit-log format. All Jira routes fail closed in public mode. Add negative tests that assert 403 in every non-private mode.
4. **Endpoint surface (D1)** — three narrow verbs (`ticket/get`, `search`, `ticket/comments`) plus a tightly-regex'd `execute` passthrough. Use `/rest/api/3/search/jql` (the non-deprecated endpoint) for search. Redact fields with a known-small denylist (`accountId`, `emailAddress`, attachment URLs) before returning to the sandbox to reduce incidental PII leakage.
5. **Tenant config (E1)** — `JIRA_BASE_URL` in `secrets.env`; project allowlist under a new `jira:` section in `config/context-filters.yaml`. Reload via mtime (same pattern used for Anthropic creds).

Ancillary:

- **Sandbox env**: `orchestrator/routes/pipelines.py` sets `EGG_JIRA_TICKET` (and optionally `EGG_JIRA_PROJECT`) from whatever trigger populated the pipeline. The gateway already has a slot for this metadata in `Session` (`issue_number`), but a dedicated `jira_ticket` field may be cleaner — open question below.
- **Audit**: every Jira op produces a structured log entry including `ticket`, `project`, `verb`, `session_mode`, `pipeline_id`, `agent_role`, and the gateway's bot-account identity.
- **Squid**: do **not** add `*.atlassian.net` to the allowlist. Force all traffic through the gateway REST endpoints.
- **Tests**: gateway unit tests with `httpx` mocks + private-mode enforcement tests + policy-allowlist tests; sandbox wrapper tests assert `EGG_SESSION_TOKEN` path and path translation.
- **Docs**: update `docs/architecture/network-isolation.md` (add `/api/v1/jira/*` to the endpoint table), `docs/architecture/credential-injection.md` (add Atlassian row), and `sandbox/agent-config/rules/environment.md` (mention `jira` wrapper alongside `gh`).

Future-write readiness is explicitly designed in: `ticket/create`, `ticket/update`, `comment/create` plug in as three more narrow routes under the same decorator + the same allowlist plumbing. Transitions / worklogs / attachments / deletions are denied in the `execute` regex and will stay out of the narrow-route list.

## Complexity Assessment

**medium** — multi-file change across `gateway/`, `sandbox/scripts/`, `orchestrator/routes/pipelines.py`, `config/`, and docs, but with a clear analogue (`/api/v1/gh/*`) to follow. Roughly comparable in scope to an additional gh endpoint family, plus the network-mode decorator, plus the project-allowlist wiring. No architectural departure.

## Open Questions

**All questions below are registered as contract decisions / feedback items via the `mcp__sdlc__*` MCP tools (see "Registered decisions" section below). They are reproduced here for reviewer legibility; the authoritative copy is in the contract.**

### Registered decisions (multiple-choice)

- [ ] **Option A (Recommended)**: Client shape — REST-only gateway endpoints (mirror `/api/v1/gh/*`).
- [ ] **Option B**: Bundle a Jira CLI (`jira-cli` / `go-jira`) in the sandbox.

- [ ] **Option A (Recommended)**: Auth flavor — Atlassian Cloud API token (Basic) for v1.
- [ ] **Option B**: OAuth 2.0 3LO for v1.
- [ ] **Option C**: Pluggable auth from day one (both strategies).

- [ ] **Option A (Recommended)**: Network-mode gate — per-route `session_mode == "private"` check with a `@require_private_mode` decorator.
- [ ] **Option B**: Flask blueprint-level `before_request` rejection.
- [ ] **Option C**: Extend `@require_session_auth` with a `required_mode="private"` kwarg.

- [ ] **Option A (Recommended)**: Endpoint surface — three narrow verbs + a regex-filtered `execute` passthrough.
- [ ] **Option B**: Single `execute` passthrough only.
- [ ] **Option C**: Three narrow verbs only; no `execute`.

- [ ] **Option A (Recommended)**: Project allowlist lives in a new `jira:` section of `config/context-filters.yaml`.
- [ ] **Option B**: New dedicated `config/jira.yaml`.
- [ ] **Option C**: Env var (`JIRA_PROJECT_ALLOWLIST`) on the gateway.

- [ ] **Option A (Recommended)**: Search endpoint — use `/rest/api/3/search/jql` (the only non-deprecated search verb on Jira Cloud).
- [ ] **Option B**: Ship our own index over synced ticket data (avoids Atlassian API quirks, much bigger scope).

- [ ] **Option A (Recommended)**: Bot identity — a dedicated Atlassian bot account owns the API token. Audit attribution is clean.
- [ ] **Option B**: Reuse the operator's personal Atlassian account. Simpler bootstrap, uglier audit.

- [ ] **Option A (Recommended)**: Redact `accountId`, `emailAddress`, and attachment URLs from responses before returning to the sandbox.
- [ ] **Option B**: Pass responses through verbatim; rely on the sandbox + egg's data-handling guarantees.

- [ ] **Option A (Recommended)**: `EGG_JIRA_TICKET` is set by the launcher from the Jira trigger; the gateway does not enforce it at the policy layer (agents can still query other tickets inside the project allowlist).
- [ ] **Option B**: `EGG_JIRA_TICKET` is enforced — agents can only access that specific ticket and its comments.

- [ ] **Option A (Recommended)**: Multi-tenant — single Atlassian site for v1, architect the client so a second site can be added later without refactor.
- [ ] **Option B**: Multi-site from day one (multiple `JIRA_BASE_URL` values, key-scoped routing).

### Registered open-ended questions (free-form)

- Which Atlassian projects should be on the v1 allowlist (project keys, comma-separated)?
- What is the expected request volume per pipeline (peak JQL searches/min, ticket reads/min)? This feeds rate-limiting defaults.
- Is there a preferred Atlassian bot-account naming / identity convention (for display name, email, and avatar) we should align with?
- Are there fields beyond `accountId`, `emailAddress`, and attachment URLs we should redact before sandbox-visible responses (e.g., certain custom fields known to hold PII)?
- How should the gateway handle Atlassian API errors that imply rate limiting (429 with `Retry-After`)? Pass through verbatim, or swallow + retry once with backoff?
- Should audit logs for Jira ops ship to the same sink as the existing gateway audit logs, or a separate Jira-scoped sink?
- For the `execute` passthrough, is there any path pattern outside `GET /rest/api/3/issue/...`, `GET /rest/api/3/search/...`, `GET /rest/api/3/project/...` we want to permit in v1? (The default stance is: no — only those three families.)
- For the future write phase (out of scope for this ticket but informing design now): should the gateway enforce idempotency (e.g., refuse a duplicate `comment create` within N seconds) or leave that to Atlassian's own semantics?
- Should Jira be reachable only in private mode (recommended, matches the issue text) or also available in a hypothetical "internal-dev" mode where agents are trusted but network is still locked down? (No such mode exists today — asking in case the intent is to add one.)
- How should the gateway handle deleted / archived tickets in responses — 404 passthrough, or synthesize a `"status":"not_found"` envelope for consistency with our other endpoints?

---

*Authored-by: egg*
