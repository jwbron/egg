# Credential Injection

The gateway sidecar injects credentials at the proxy layer, ensuring the sandbox container has zero credential access. This covers GitHub (via git wrappers), Anthropic API (via `ANTHROPIC_BASE_URL`), and Atlassian/Jira/Confluence (via the `/api/v1/jira/*` and `/api/v1/confluence/*` REST endpoints) credentials.

**Key properties:**
- **Zero credential exposure**: Container never sees API keys, OAuth tokens, or GitHub tokens
- **Infrastructure enforcement**: Cannot be bypassed by prompt injection or container compromise
- **Single audit point**: All authenticated traffic logged through gateway
- **Tool filtering**: WebSearch/WebFetch blocked in private mode to prevent data exfiltration

## Architecture

### Anthropic API Credential Injection

Claude Code supports custom API endpoints via `ANTHROPIC_BASE_URL` ([docs](https://code.claude.com/docs/en/llm-gateway)). The container sets `ANTHROPIC_BASE_URL=http://egg-gateway:9848`, routing all API traffic through the gateway over HTTP (internal network). The gateway adds credentials and forwards over HTTPS to `api.anthropic.com`.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           CREDENTIAL FLOW                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────────┐    ANTHROPIC_BASE_URL     ┌─────────────────────┐  │
│  │  sandbox  │ ─────────────────────────▶│    egg-gateway      │  │
│  │                 │   http://egg-gateway:9848 │                     │  │
│  │  Claude Code    │   /v1/messages            │  1. Receive request │  │
│  │                 │   (no credentials)        │  2. Inject creds    │  │
│  │  No API key     │                           │  3. Filter tools    │──┼──▶ api.anthropic.com
│  │  No OAuth token │                           │  4. Forward to API  │  │
│  └─────────────────┘                           │                     │  │
│                                                │  Credentials from:  │  │
│                                                │  ~/.config/egg/     │  │
│                                                │    secrets.env      │  │
│                                                └─────────────────────┘  │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

This approach requires no SSL bump — the gateway receives plaintext HTTP, handles TLS outbound. No CA certificate trust is needed in the container for Anthropic traffic.

### Public vs Private Mode

Both modes use the same `ANTHROPIC_BASE_URL` mechanism:

- **Public mode**: Container has direct internet access for non-API traffic, but Anthropic API calls route through gateway
- **Private mode**: All traffic routes through gateway proxy with domain allowlist

### Gateway Proxy Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /v1/messages` | Main messages API with streaming SSE support |
| `POST /v1/messages/count_tokens` | Token counting API |

**Implementation details:**
- Uses `httpx` with connection pooling for performance
- Streaming responses via Flask's `stream_with_context` (no buffering)
- Header blocklist approach: forwards all headers except auth-related ones
- Full error passthrough including `x-request-id` for debugging
- **Upstream stream-reset resilience** (see [Upstream Stream Resilience](#upstream-stream-resilience) below)

### Upstream Stream Resilience

Long-running Anthropic `/v1/messages` SSE responses occasionally terminate with an upstream TCP reset (ECONNRESET) — the LB/edge idle-times the connection, the proxy rebalances, or a middlebox closes the socket mid-stream. Without mitigation, `httpx` surfaces this as `httpx.ReadError` / `httpx.RemoteProtocolError`, the downstream SDK sees a truncated SSE stream with no terminating event, and the agent dies on a fatal `socket connection was closed unexpectedly` — losing all in-flight work (see [#1907](https://github.com/jwbron/egg/issues/1907)).

The gateway's `proxy_anthropic_messages()` handles this in two complementary ways:

| Reset timing | Gateway behavior | Agent-visible effect |
|--------------|------------------|----------------------|
| **Pre-stream** (on `client.send()` or before the first downstream byte) | Close the failed upstream, rebuild the request, retry once. If the retry succeeds, proceed normally; if it also fails, fall through to the existing 502 error path. | None — the retry is transparent. |
| **Mid-stream** (after downstream bytes have already flowed) | Catch the `ReadError` / `RemoteProtocolError` inside the `iter_bytes()` loop, emit a well-formed synthetic SSE `event: error` frame with an Anthropic-style payload, feed it through the accumulator so the transcript still captures the failure, and close the stream cleanly. `finally: upstream.close()` + `_capture_streaming_response` continue to run. A `logger.warning` records the reset with `container_id` and `bytes_seen`. | A clean SSE `error` event instead of a truncated socket. The SDK reports a recoverable error rather than a fatal hang-up, and the `aclose()` cleanup bug is avoided. |

**Why not full stream resumption?** Anthropic's API exposes no resume tokens, and the partial generation on the wire is orphaned once the upstream socket dies. Mid-stream retry would risk double-charging and interleaving two divergent generations on the downstream wire. Pre-stream retry is safe because by definition no downstream bytes have been committed yet.

**Bounded retry.** The pre-stream retry is capped at one attempt and is gated on the first chunk not yet having been yielded downstream (enforced structurally by `_send_and_prime()`, which raises before `generate()` begins). Second-failure cases fall through to the pre-existing `except httpx.ConnectError / TimeoutException / Exception` handlers, preserving their 502/504 error contracts.

**Scope.** This fix lives entirely inside the gateway. It is distinct from [#1883](https://github.com/jwbron/egg/issues/1883) (gateway pod restart — gateway *process* is gone) and [#1873](https://github.com/jwbron/egg/issues/1873) (turn-1 transient retry in `consensus-wrapper`). Those handle cases where the gateway itself cannot re-issue the upstream request; this handles the far more common case where the gateway is healthy and only a single upstream connection died.

### Container Configuration

The container entrypoint:
1. Sets `ANTHROPIC_BASE_URL=http://egg-gateway:9848`
2. Removes `ANTHROPIC_API_KEY` from environment (if present)
3. Removes `ANTHROPIC_OAUTH_TOKEN` from environment (if present)
4. Removes proxy environment variables for Node.js (Claude Code)

### Credential Storage

Credentials are stored on the host machine and mounted into the gateway:

```bash
# ~/.config/egg/secrets.env
# Choose ONE authentication method:

# Option 1: API Key (for Anthropic API accounts)
ANTHROPIC_API_KEY="sk-ant-api03-..."

# Option 2: OAuth Token (for Claude Max subscriptions)
ANTHROPIC_OAUTH_TOKEN="..."
```

The gateway reads credentials with mtime-based cache refresh for hot reloading.

### Squid Configuration

With `ANTHROPIC_BASE_URL` routing, `api.anthropic.com` is **not** in the Squid allowlist:
- Prevents container from bypassing gateway to make direct API requests
- All API traffic must flow through gateway proxy endpoints
- Enforces credential injection at infrastructure level

## Tool Filtering (Private Mode)

In private mode, WebSearch and WebFetch are blocked at three independent layers.

WebSearch and WebFetch bypass container network controls because they're processed by Anthropic's infrastructure. A compromised agent could encode sensitive data in search queries, creating a data exfiltration vector.

**Layer 1 — Agent settings (`settings.json`):**
The sandbox entrypoint sets `disallowedTools: ["WebFetch", "WebSearch"]` in Claude Code's `settings.json` when `EGG_PRIVATE_MODE=true`. This prevents the tools from ever being sent to the API in the first place for interactive Claude Code sessions.

**Layer 2 — SDK options (headless agents):**
`egg_agent.client` checks `EGG_PRIVATE_MODE` at runtime and passes `disallowed_tools=["WebFetch", "WebSearch"]` in `ClaudeAgentOptions` when running headless agents via the Claude Agent SDK. This is more reliable than `settings.json` for SDK-based agents, since `settings.json` is a Claude Code concept that doesn't apply to SDK usage. It also eliminates gateway log noise from stripping tools on every request.

**Layer 3 — Gateway filtering (defense-in-depth):**
The gateway's `_filter_blocked_tools()` function:
1. Checks session mode (private vs public)
2. Parses request JSON to find tool definitions
3. Removes `web_search`, `WebSearch`, `web_fetch`, `WebFetch` in private mode
4. Logs filtered tool attempts for security auditing

Gateway enforcement cannot be bypassed because:
- Container cannot reach `api.anthropic.com` directly (not in Squid allowlist)
- All API traffic must flow through gateway
- Gateway runs outside container's control

## Security Properties

| Property | Mechanism |
|----------|-----------|
| Zero credential exposure | Credentials only in gateway, never in container |
| Infrastructure enforcement | Cannot bypass via instructions, config changes, or container escape |
| Single audit point | All API auth logged through gateway |
| Tool restriction | WebSearch/WebFetch blocked in private mode: agent settings + SDK options + gateway |
| Consistent model | Same security as git credential isolation |

### Authentication Types

| Type | Source | Header Injected |
|------|--------|-----------------|
| API Key | `ANTHROPIC_API_KEY` in secrets.env | `x-api-key: <key>` |
| OAuth Token | `ANTHROPIC_OAUTH_TOKEN` in secrets.env | `Authorization: Bearer <token>` |
| Atlassian / Jira | `ATLASSIAN_*` (preferred) or `JIRA_*` triple in secrets.env | `Authorization: Basic <base64(username:token)>` |
| Atlassian / Confluence | `ATLASSIAN_*` (preferred) or `CONFLUENCE_*` triple in secrets.env | `Authorization: Basic <base64(username:token)>` |

OAuth takes precedence over API key if both are configured. OAuth tokens may expire; the user runs `claude auth status` to generate a new token, and the gateway hot-reloads via mtime-based cache refresh.

### Atlassian / Jira

Sandboxed agents reach Jira exclusively through the gateway's `/api/v1/jira/*` REST endpoints. Atlassian credentials are held by the gateway and injected per-request; they never enter the sandbox.

**Credential storage:**
```bash
# ~/.config/egg/secrets.env
# Shared Atlassian triple (preferred — also covers Confluence per-key with /wiki derivation)
ATLASSIAN_BASE_URL="https://your-site.atlassian.net"
ATLASSIAN_USERNAME="bot@example.com"
ATLASSIAN_API_TOKEN="ATATT3x..."

# Legacy per-service blocks (back-compat fall-back per key)
JIRA_BASE_URL="https://your-site.atlassian.net"   # used if ATLASSIAN_BASE_URL absent
JIRA_USERNAME="bot@example.com"                    # used if ATLASSIAN_USERNAME absent
JIRA_API_TOKEN="ATATT3x..."                        # used if ATLASSIAN_API_TOKEN absent
```

**Credential precedence:** Per-key — for each of `BASE_URL`, `USERNAME`, `API_TOKEN`, the loader prefers the `ATLASSIAN_*` value and falls back to `JIRA_*`. The two name shapes can be mixed (e.g., `ATLASSIAN_USERNAME` + `JIRA_BASE_URL` is a valid combination — Atlassian accounts are tenant-wide). This makes the shared-credential migration safe: operators can copy values to `ATLASSIAN_*` and remove the legacy `JIRA_*` block once the shared triple is fully populated, without breaking Jira.

**Loader:** `gateway/jira_credentials.py` mirrors `gateway/anthropic_credentials.py` — mtime-based cache refresh of `~/.config/egg/secrets.env` (override with `EGG_SECRETS_PATH`). `get_jira_credentials()` returns a `JiraCredentials` dataclass with `base_url`, `username`, `api_token`, and a `basic_auth_header()` helper that emits the base64-encoded `Basic` header. Missing values raise `JiraCredentialsUnavailable`, which the route layer translates to HTTP 503. `reload_jira_credentials()` is wired into the gateway's `_reload_all_config()` hook, so `POST /api/v1/config/reload` picks up rotated tokens without a process restart.

**Zero-credential invariant:** The orchestrator's sandbox-launch env builder (`orchestrator/routes/pipelines.py`) is forbidden from exporting `JIRA_BASE_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`, or any `ATLASSIAN_*` key to the agent container. A regression test in `orchestrator/tests/test_start_pipeline.py` iterates the sandbox env and asserts those keys are absent. The only Jira-related variables the agent sees are `EGG_JIRA_TICKET` (the ticket the pipeline is scoped to) and `EGG_JIRA_PROJECT` (optional, advisory) — neither is a credential.

**Private-mode only + project allowlist:** Every `/api/v1/jira/*` route is decorated with `@require_private_mode` (`gateway/mode_gate.py`). In public mode, the decorator returns 403 and emits a `private_mode_required` audit entry **before** any credential is loaded or any upstream request is issued. After the mode gate, each route checks the extracted project key against the allowlist in `config/context-filters.yaml` (`jira.projects`) — see [Jira wrapper reference](../reference/jira-wrapper.md) for the full policy semantics.

**Squid allowlist excludes Atlassian domains:** `*.atlassian.net`, `*.atlassian.com`, `api.atlassian.com`, `jira.atlassian.com`, `wiki.atlassian.net`, and `confluence.atlassian.com` are intentionally **not** in `gateway/allowed_domains.txt`. All Jira and Confluence traffic flows through the gateway REST endpoints, never through the Squid proxy, so the private-mode gate and the project / space allowlists cannot be bypassed by a direct `CONNECT` to Atlassian through the proxy. A regression test (`gateway/tests/test_allowed_domains.py`) enforces this invariant.

### Atlassian / Confluence

Sandboxed agents reach Confluence exclusively through the gateway's `/api/v1/confluence/*` REST endpoints. Atlassian credentials are held by the gateway and injected per-request; they never enter the sandbox. The Confluence wrapper shares the dedicated Atlassian bot account with the Jira wrapper (single principal owns both services' read scopes).

**Credential storage:**
```bash
# ~/.config/egg/secrets.env
# Shared Atlassian triple (preferred)
ATLASSIAN_BASE_URL="https://your-site.atlassian.net"   # NO trailing /wiki — loader appends it
ATLASSIAN_USERNAME="bot@example.com"
ATLASSIAN_API_TOKEN="ATATT3x..."

# Legacy per-service block (back-compat fall-back per key)
CONFLUENCE_BASE_URL="https://your-site.atlassian.net/wiki"   # /wiki required when set explicitly
CONFLUENCE_USERNAME="bot@example.com"
CONFLUENCE_API_TOKEN="ATATT3x..."
```

**Credential precedence:** Per-key — `ATLASSIAN_*` wins; missing keys fall back to `CONFLUENCE_*`. The two name shapes can be mixed at the per-key level.

**Base-URL derivation.** Confluence lives under `/wiki` on Atlassian Cloud:

- If `ATLASSIAN_BASE_URL` is set, the loader uses it and **appends `/wiki`** automatically — `ATLASSIAN_BASE_URL` is the bare Atlassian origin shared with Jira (which uses it verbatim).
- If `ATLASSIAN_BASE_URL` is unset and `CONFLUENCE_BASE_URL` is set, the loader uses `CONFLUENCE_BASE_URL` verbatim — operators must include the `/wiki` suffix when setting the legacy block directly.

This precedence (ATLASSIAN-wins, CONFLUENCE as back-compat fallback) matches the Jira loader's per-key precedence and keeps the two services consistent.

**Loader:** `gateway/confluence_credentials.py` mirrors `gateway/jira_credentials.py` exactly (mtime-based cache refresh, thread-safe singleton, override via `EGG_SECRETS_PATH`). `get_confluence_credentials()` returns a `ConfluenceCredentials` dataclass with `base_url`, `username`, `api_token`, and a `basic_auth_header()` helper that emits the base64-encoded `Basic` header. Missing values raise `ConfluenceCredentialsUnavailable`, which the route layer translates to HTTP 503. `reload_confluence_credentials()` is wired into the gateway's `_reload_all_config()` hook so `POST /api/v1/config/reload` picks up rotated tokens without a process restart, alongside the Jira reload.

**Why two loader files instead of one shared helper.** v1 deliberately duplicates the loader skeleton across `gateway/jira_credentials.py` and `gateway/confluence_credentials.py` for review clarity (architect Q4). Extracting a shared `atlassian_credentials.py` helper is tracked as a follow-up backlog item — the duplication makes the per-service precedence rules easier to audit at v1 review time.

**Zero-credential invariant:** No `ATLASSIAN_*` or `CONFLUENCE_*` key is ever exported to the agent container. The orchestrator-side regression test that gates Jira keys is extended to cover both Atlassian and Confluence prefixes.

**Private-mode only + space allowlist:** Every `/api/v1/confluence/*` route is decorated with `@require_private_mode`. A route-enumeration regression test (`gateway/tests/test_confluence_routes.py`) asserts every Confluence view function carries `__egg_requires_private_mode__ = True` so a future contributor cannot accidentally drop the gate. After the mode gate, each route checks the resolved space key against the allowlist in `config/context-filters.yaml` (`confluence.spaces`). The check runs **after** the upstream fetch for routes that take a `pageId` (the response carries the `spaceId`), and **before** the upstream fetch for routes that take a `spaceKey` directly. See [Confluence wrapper reference](../reference/confluence-wrapper.md) for the full policy semantics, the conservative CQL extractor, the v1 inline-comment fallback, and the response-redaction walker.

**No per-pipeline `EGG_CONFLUENCE_*` env var.** Unlike Jira (`EGG_JIRA_TICKET`), Confluence has no orchestrator-exported observational env var (refine-phase decision 13 of #1931). Confluence is consulted as reference material from ticket/epic links, not as the pipeline's primary unit of work; audits recover `pageId` / `spaceKey` from each request body or response.

## Files

| File | Purpose |
|------|---------|
| `gateway/gateway.py` | Anthropic proxy endpoints, `/api/v1/jira/*` and `/api/v1/confluence/*` routes, credential injection, tool filtering, `_reload_all_config()` hot-reload hook |
| `gateway/anthropic_credentials.py` | Anthropic credential loading from secrets.env |
| `gateway/jira_credentials.py` | Atlassian credential loading from secrets.env for Jira (mtime refresh, basic-auth header helper, `ATLASSIAN_*` precedence) |
| `gateway/confluence_credentials.py` | Atlassian credential loading from secrets.env for Confluence (mtime refresh, `ATLASSIAN_*` precedence with `/wiki` derivation) |
| `gateway/jira_client.py` | Jira REST client + `validate_jira_api_path` regex allowlist + 429 retry + 404 envelope |
| `gateway/confluence_client.py` | Confluence REST client + `validate_confluence_api_path` regex allowlist + 429 retry + 404 envelope + 403 escalation + v1 fallback for inline comments + response redaction |
| `gateway/jira_policy.py` | Project allowlist loader for `config/context-filters.yaml` (`jira.projects`) |
| `gateway/confluence_policy.py` | Space allowlist loader for `config/context-filters.yaml` (`confluence.spaces`) |
| `gateway/jira_search.py` | JQL static project-scope extractor (deny-on-ambiguity) |
| `gateway/confluence_search.py` | CQL static space-scope extractor (deny-on-ambiguity) |
| `gateway/mode_gate.py` | `@require_private_mode` decorator (fails closed in public mode, marks view for regression test) |
| `gateway/session_manager.py` | `Session.jira_ticket` audit field (observational; project allowlist is the only hard boundary) |
| `gateway/allowed_domains.txt` | Domain allowlist (api.anthropic.com and all Atlassian domains intentionally absent) |
| `sandbox/entrypoint.py` | Set ANTHROPIC_BASE_URL, remove creds from env, set disallowedTools in private mode |
| `sandbox/scripts/jira` | Sandbox CLI wrapper — POSTs to `/api/v1/jira/*` with `EGG_SESSION_TOKEN` |
| `sandbox/scripts/confluence` | Sandbox CLI wrapper — POSTs to `/api/v1/confluence/*` with `EGG_SESSION_TOKEN` |
| `shared/egg_agent/client.py` | Pass `disallowed_tools` via SDK options for headless agents in private mode |
| `config/secrets.template.env` | Template for Anthropic and Atlassian credentials (shared `ATLASSIAN_*` block + legacy `JIRA_*` / `CONFLUENCE_*` blocks) |
| `config/context-filters.yaml` | Operator-facing Jira project allowlist (`jira.projects:`) and Confluence space allowlist (`confluence.spaces:`) |

## Related Documentation

- [Git Isolation Architecture](git-isolation.md) — Worktree isolation via gateway
- [Network Isolation](network-isolation.md) — Full network lockdown design
- [Jira Wrapper Reference](../reference/jira-wrapper.md) — `/api/v1/jira/*` endpoint surface, JQL scope extractor, not-found envelope, future-verb extension points
- [Confluence Wrapper Reference](../reference/confluence-wrapper.md) — `/api/v1/confluence/*` endpoint surface, CQL scope extractor, v1 fallback for inline comments, response redaction, future-verb extension points
- [Architecture Overview](README.md) — System design
