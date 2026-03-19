# Credential Injection

The gateway sidecar injects credentials at the proxy layer, ensuring the sandbox container has zero credential access. This covers both GitHub (via git wrappers) and Anthropic API (via `ANTHROPIC_BASE_URL`) credentials.

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

In private mode, WebSearch and WebFetch are filtered at the gateway level.

WebSearch and WebFetch bypass container network controls because they're processed by Anthropic's infrastructure. A compromised agent could encode sensitive data in search queries, creating a data exfiltration vector.

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
| Tool restriction | WebSearch/WebFetch blocked in private mode at gateway |
| Consistent model | Same security as git credential isolation |

### Authentication Types

| Type | Source | Header Injected |
|------|--------|-----------------|
| API Key | `ANTHROPIC_API_KEY` in secrets.env | `x-api-key: <key>` |
| OAuth Token | `ANTHROPIC_OAUTH_TOKEN` in secrets.env | `Authorization: Bearer <token>` |

OAuth takes precedence if both are configured. OAuth tokens may expire; the user runs `claude auth status` to generate a new token, and the gateway hot-reloads via mtime-based cache refresh.

## Files

| File | Purpose |
|------|---------|
| `gateway/gateway.py` | Anthropic proxy endpoints, credential injection, tool filtering |
| `gateway/anthropic_credentials.py` | Credential loading from secrets.env |
| `gateway/allowed_domains.txt` | Domain allowlist (api.anthropic.com intentionally absent) |
| `sandbox/entrypoint.py` | Set ANTHROPIC_BASE_URL, remove creds from env |
| `config/secrets.template.env` | Template for Anthropic credentials |

## Related Documentation

- [Git Isolation Architecture](git-isolation.md) — Worktree isolation via gateway
- [Network Isolation](network-isolation.md) — Full network lockdown design
- [Architecture Overview](README.md) — System design
