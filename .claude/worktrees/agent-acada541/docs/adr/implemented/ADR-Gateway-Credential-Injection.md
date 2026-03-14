# ADR: Gateway Credential Injection for Anthropic API

**Status:** Implemented
**Date:** 2026-02-02
**PRs:** #695 (proposal), #701 (implementation), #705 (tool filtering)

---

## Executive Summary

The gateway sidecar injects Anthropic API credentials at the proxy layer, ensuring the sandbox container has zero credential access. This extends the security model established in [ADR-Git-Isolation-Architecture](./ADR-Git-Isolation-Architecture.md) to cover API authentication.

**Key properties:**
- **Zero credential exposure**: Container never sees API keys or OAuth tokens
- **Infrastructure enforcement**: Cannot be bypassed by prompt injection or container compromise
- **Single audit point**: All API traffic logged through gateway
- **Tool filtering**: WebSearch/WebFetch blocked in private mode to prevent data exfiltration

---

## Motivation

Before this change, the sandbox container received Anthropic credentials via:
- Environment variables (`ANTHROPIC_API_KEY` or `ANTHROPIC_OAUTH_TOKEN`)
- Mounted config files (`~/.claude`, `~/.claude.json`)

This created security risks:
1. **Credential exposure**: If the sandbox is compromised, credentials are immediately available
2. **Exfiltration risk**: Claude could inadvertently log or transmit credentials
3. **Inconsistent model**: Git credentials were isolated in the gateway, but API credentials were exposed

The gateway already handles all git/GitHub authentication. Extending this to Anthropic API authentication creates a consistent security model where **all credentials live in the gateway**.

---

## Architecture

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

### Key Insight: ANTHROPIC_BASE_URL

Claude Code officially supports custom API endpoints via `ANTHROPIC_BASE_URL` ([docs](https://code.claude.com/docs/en/llm-gateway)). This enables a clean architecture:

- Container sets `ANTHROPIC_BASE_URL=http://egg-gateway:9848`
- Claude Code sends requests to gateway over HTTP (internal network)
- Gateway adds credentials and forwards over HTTPS to api.anthropic.com
- **No SSL bump needed** - gateway receives plaintext, handles TLS outbound

This approach is simpler than the originally proposed SSL MITM approach:
- No CA certificate trust required in container
- No Squid SSL bump configuration needed for Anthropic traffic
- HTTP between container and gateway is inspectable for debugging

---

## Implementation

### Gateway Proxy Endpoints

The gateway exposes HTTP endpoints that proxy to Anthropic with credential injection:

| Endpoint | Purpose |
|----------|---------|
| `POST /v1/messages` | Main messages API with streaming SSE support |
| `POST /v1/messages/count_tokens` | Token counting API |

**Key implementation details:**
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

With ANTHROPIC_BASE_URL routing, `api.anthropic.com` is **not** in the Squid allowlist:
- Prevents container from bypassing gateway to make direct API requests
- All API traffic must flow through gateway proxy endpoints
- Enforces credential injection at infrastructure level

---

## Tool Filtering (Private Mode)

In private mode, WebSearch and WebFetch are filtered at the gateway level.

### Threat Model

WebSearch and WebFetch bypass container network controls because they're processed by Anthropic's infrastructure. A compromised agent could encode sensitive data in search queries, creating a data exfiltration vector.

### Implementation

The gateway's `_filter_blocked_tools()` function:
1. Checks session mode (private vs public)
2. Parses request JSON to find tool definitions
3. Removes `web_search`, `WebSearch`, `web_fetch`, `WebFetch` in private mode
4. Logs filtered tool attempts for security auditing

```python
BLOCKED_TOOLS_PRIVATE_MODE = {"web_search", "WebSearch", "web_fetch", "WebFetch"}
```

### Why Gateway Enforcement

Client-side controls (Claude Code's `permissions.deny`) can be bypassed:
```bash
# A prompt injection could do this
echo '{"permissions":{}}' > ~/.claude/settings.json
```

Gateway enforcement cannot be bypassed because:
- Container cannot reach api.anthropic.com directly (not in Squid allowlist)
- All API traffic MUST flow through gateway
- Gateway runs outside container's control

---

## Security Properties

| Property | Mechanism |
|----------|-----------|
| Zero credential exposure | Credentials only in gateway, never in container |
| Infrastructure enforcement | Cannot bypass via instructions, config changes, or container escape |
| Single audit point | All API auth logged through gateway |
| Tool restriction | WebSearch/WebFetch blocked in private mode at gateway |
| Consistent model | Same security as git credential isolation |

### Threat Mitigations

| Threat | Mitigation |
|--------|------------|
| Credential theft from container | Credentials never enter container |
| Credential exfiltration via logs | Gateway doesn't log credential values |
| Direct API access bypassing gateway | api.anthropic.com not in Squid allowlist |
| Data exfiltration via web tools | Tools filtered at gateway in private mode |
| Prompt injection disabling controls | Gateway enforcement is infrastructure-level |

---

## Authentication Types

| Type | Source | Header Injected |
|------|--------|-----------------|
| API Key | `ANTHROPIC_API_KEY` in secrets.env | `x-api-key: <key>` |
| OAuth Token | `ANTHROPIC_OAUTH_TOKEN` in secrets.env | `Authorization: Bearer <token>` |

OAuth takes precedence if both are configured.

### OAuth Token Lifecycle

OAuth tokens (from Claude Max subscriptions) may expire. The gateway:
- Passes through 401 responses for expired tokens
- User runs `claude auth status` to generate new token
- Gateway hot-reloads credentials via mtime-based cache refresh

---

## Files Modified

| File | Change |
|------|--------|
| `gateway/gateway.py` | Anthropic proxy endpoints, credential injection, tool filtering |
| `gateway/anthropic_credentials.py` | Credential loading from secrets.env |
| `gateway/allowed_domains.txt` | Removed api.anthropic.com (intentionally) |
| `sandbox/entrypoint.py` | Set ANTHROPIC_BASE_URL, remove creds from env |
| `bin/egg` | Remove credential mounting into container |
| `config/secrets.template.env` | Template for Anthropic credentials |

---

## Alternatives Considered

### SSL Bump with ICAP (Original Proposal)

The original proposal used Squid SSL bump to MITM api.anthropic.com traffic:
- Required CA certificate generation and trust store management
- Complex ICAP or external ACL helper for header injection
- More moving parts with potential failure modes

**Rejected in favor of ANTHROPIC_BASE_URL approach** which is:
- Officially supported by Claude Code
- Simpler (no SSL MITM complexity)
- Easier to debug (HTTP between container and gateway)

### Client-Side Credential Injection

Injecting credentials via environment variable or config file in container:
- Would expose credentials to compromised container
- Could be exfiltrated via prompts or logs

**Rejected** because it violates the zero-credential-exposure principle.

---

## References

- [Claude Code LLM Gateway docs](https://code.claude.com/docs/en/llm-gateway)
- PR #695: Original proposal and planning documents
- PR #701: Implementation using ANTHROPIC_BASE_URL
- PR #705: Tool filtering for private mode
- [ADR-Git-Isolation-Architecture](./ADR-Git-Isolation-Architecture.md): Security model this builds on
