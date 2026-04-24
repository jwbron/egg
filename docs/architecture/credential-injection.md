# Credential Injection

The gateway sidecar injects credentials at the proxy layer, ensuring the sandbox container has zero credential access. This covers GitHub (via git wrappers), Anthropic API (via `ANTHROPIC_BASE_URL`), and Atlassian/Jira (via the `/api/v1/jira/*` REST endpoints) credentials.

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
| Atlassian / Jira | `JIRA_BASE_URL` + `JIRA_USERNAME` + `JIRA_API_TOKEN` in secrets.env | `Authorization: Basic <base64(username:token)>` |

OAuth takes precedence over API key if both are configured. OAuth tokens may expire; the user runs `claude auth status` to generate a new token, and the gateway hot-reloads via mtime-based cache refresh.

### Atlassian / Jira

Sandboxed agents reach Jira exclusively through the gateway's `/api/v1/jira/*` REST endpoints. Atlassian credentials are held by the gateway and injected per-request; they never enter the sandbox.

**Credential storage:**
```bash
# ~/.config/egg/secrets.env
JIRA_BASE_URL="https://your-site.atlassian.net"
JIRA_USERNAME="bot@example.com"   # Atlassian account email
JIRA_API_TOKEN="ATATT3x..."       # Atlassian Cloud API token
```

**Loader:** `gateway/jira_credentials.py` mirrors `gateway/anthropic_credentials.py` — mtime-based cache refresh of `~/.config/egg/secrets.env` (override with `EGG_SECRETS_PATH`). `get_jira_credentials()` returns a `JiraCredentials` dataclass with `base_url`, `username`, `api_token`, and a `basic_auth_header()` helper that emits the base64-encoded `Basic` header. Missing values raise `JiraCredentialsUnavailable`, which the route layer translates to HTTP 503. `reload_jira_credentials()` is wired into the gateway's `_reload_all_config()` hook, so `POST /api/v1/config/reload` picks up rotated tokens without a process restart.

**Zero-credential invariant:** The orchestrator's sandbox-launch env builder (`orchestrator/routes/pipelines.py`) is forbidden from exporting `JIRA_BASE_URL`, `JIRA_USERNAME`, or `JIRA_API_TOKEN` to the agent container. A regression test in `orchestrator/tests/test_start_pipeline.py` iterates the sandbox env and asserts those three keys are absent. The only Jira-related variables the agent sees are `EGG_JIRA_TICKET` (the ticket the pipeline is scoped to) and `EGG_JIRA_PROJECT` (optional, advisory) — neither is a credential.

**Private-mode only + project allowlist:** Every `/api/v1/jira/*` route is decorated with `@require_private_mode` (`gateway/mode_gate.py`). In public mode, the decorator returns 403 and emits a `private_mode_required` audit entry **before** any credential is loaded or any upstream request is issued. After the mode gate, each route checks the extracted project key against the allowlist in `config/context-filters.yaml` (`jira.projects`) — see [Jira wrapper reference](../reference/jira-wrapper.md) for the full policy semantics.

**Squid allowlist excludes Atlassian domains:** `*.atlassian.net`, `*.atlassian.com`, `api.atlassian.com`, and `jira.atlassian.com` are intentionally **not** in `gateway/allowed_domains.txt`. All Jira traffic flows through the gateway REST endpoints, never through the Squid proxy, so the private-mode gate and the project allowlist cannot be bypassed by a direct `CONNECT` to Atlassian through the proxy. A regression test (`gateway/tests/test_allowed_domains.py`) enforces this invariant.

## Files

| File | Purpose |
|------|---------|
| `gateway/gateway.py` | Anthropic proxy endpoints, `/api/v1/jira/*` routes, credential injection, tool filtering, `_reload_all_config()` hot-reload hook |
| `gateway/anthropic_credentials.py` | Anthropic credential loading from secrets.env |
| `gateway/jira_credentials.py` | Atlassian credential loading from secrets.env (mtime refresh, basic-auth header helper) |
| `gateway/jira_client.py` | Jira REST client + `validate_jira_api_path` regex allowlist + 429 retry + 404 envelope |
| `gateway/jira_policy.py` | Project allowlist loader for `config/context-filters.yaml` (`jira.projects`) |
| `gateway/mode_gate.py` | `@require_private_mode` decorator (fails closed in public mode, marks view for regression test) |
| `gateway/session_manager.py` | `Session.jira_ticket` audit field (observational; project allowlist is the only hard boundary) |
| `gateway/allowed_domains.txt` | Domain allowlist (api.anthropic.com and all Atlassian domains intentionally absent) |
| `sandbox/entrypoint.py` | Set ANTHROPIC_BASE_URL, remove creds from env, set disallowedTools in private mode |
| `sandbox/scripts/jira` | Sandbox CLI wrapper — POSTs to `/api/v1/jira/*` with `EGG_SESSION_TOKEN` |
| `shared/egg_agent/client.py` | Pass `disallowed_tools` via SDK options for headless agents in private mode |
| `config/secrets.template.env` | Template for Anthropic and Atlassian credentials |
| `config/context-filters.yaml` | Operator-facing Jira project allowlist (`jira.projects:`) |

## Related Documentation

- [Git Isolation Architecture](git-isolation.md) — Worktree isolation via gateway
- [Network Isolation](network-isolation.md) — Full network lockdown design
- [Jira Wrapper Reference](../reference/jira-wrapper.md) — `/api/v1/jira/*` endpoint surface, JQL scope extractor, not-found envelope, future-verb extension points
- [Architecture Overview](README.md) — System design
