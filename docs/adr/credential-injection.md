# ADR: Credential Injection via Gateway

**Status:** Implemented
**Origin:** Extracted from james-in-a-box

## Summary

The gateway injects credentials (GitHub tokens, Anthropic API keys) at the proxy layer, ensuring the sandbox container has zero credential access. This extends the security model established in the git isolation architecture to cover all authentication.

**Key properties:**
- **Zero credential exposure**: Container never sees API keys or OAuth tokens
- **Infrastructure enforcement**: Cannot be bypassed by prompt injection or container compromise
- **Single audit point**: All authenticated traffic logged through gateway

## Motivation

Without credential injection, the sandbox container would receive credentials via:
- Environment variables (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`)
- Mounted config files

This creates security risks:
1. **Credential exposure**: If the sandbox is compromised, credentials are immediately available
2. **Exfiltration risk**: Agent could inadvertently log or transmit credentials
3. **Inconsistent model**: Different treatment of different credential types

With credential injection, **all credentials live in the gateway**.

## Architecture

### Anthropic API Credential Injection

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           CREDENTIAL FLOW                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────┐    ANTHROPIC_BASE_URL     ┌─────────────────────┐  │
│  │  Sandbox        │ ─────────────────────────▶│    Gateway          │  │
│  │                 │   http://gateway:8080     │                     │  │
│  │  Claude Code    │   /v1/messages            │  1. Receive request │  │
│  │                 │   (no credentials)        │  2. Inject creds    │──┼──▶ api.anthropic.com
│  │  No API key     │                           │  3. Forward to API  │  │
│  │  No OAuth token │                           │                     │  │
│  └─────────────────┘                           │  Credentials from:  │  │
│                                                │  ~/.egg/secrets.yaml│  │
│                                                └─────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Key Insight: ANTHROPIC_BASE_URL

Claude Code officially supports custom API endpoints via `ANTHROPIC_BASE_URL`. This enables a clean architecture:

- Container sets `ANTHROPIC_BASE_URL=http://gateway:8080`
- Claude Code sends requests to gateway over HTTP (internal network)
- Gateway adds credentials and forwards over HTTPS to api.anthropic.com
- **No SSL bump needed** - gateway receives plaintext, handles TLS outbound

### GitHub Credential Injection

Git and gh CLI commands are intercepted by wrapper scripts that call the gateway API:

```
Container                    Gateway                      GitHub
   │                           │                            │
   │  git push                 │                            │
   │ ─────────────────────────▶│                            │
   │  (via wrapper, no creds)  │  git push (with token)     │
   │                           │ ──────────────────────────▶│
   │                           │                            │
   │  <──────────────────────  │  <─────────────────────────│
   │        result             │        result              │
```

## Implementation

### Gateway Proxy Endpoints

The gateway exposes HTTP endpoints that proxy to Anthropic with credential injection:

| Endpoint | Purpose |
|----------|---------|
| `POST /v1/messages` | Main messages API with streaming SSE support |
| `POST /v1/messages/count_tokens` | Token counting API |

**Key implementation details:**
- Uses connection pooling for performance
- Streaming responses via SSE (no buffering)
- Header blocklist approach: forwards all headers except auth-related ones
- Full error passthrough including `x-request-id` for debugging

### Container Configuration

The container entrypoint:
1. Sets `ANTHROPIC_BASE_URL=http://gateway:8080`
2. Removes `ANTHROPIC_API_KEY` from environment (if present)
3. Removes `ANTHROPIC_OAUTH_TOKEN` from environment (if present)

### Credential Storage

Credentials are stored on the host machine in `~/.egg/secrets.yaml`:

```yaml
secrets:
  github_app:
    app_id: "123456"
    private_key_path: "/path/to/key.pem"

  anthropic:
    # For API users
    api_key: "sk-ant-xxxxxxxxxxxx"
    # OR for Pro/Max subscribers
    oauth_token: "oauth-xxxxxxxxxxxx"
```

The gateway reads credentials with mtime-based cache refresh for hot reloading.

## Security Properties

| Property | Mechanism |
|----------|-----------|
| Zero credential exposure | Credentials only in gateway, never in container |
| Infrastructure enforcement | Cannot bypass via instructions or config changes |
| Single audit point | All authenticated traffic logged through gateway |
| Consistent model | Same security for git and API credentials |

### Threat Mitigations

| Threat | Mitigation |
|--------|------------|
| Credential theft from container | Credentials never enter container |
| Credential exfiltration via logs | Gateway doesn't log credential values |
| Direct API access bypassing gateway | api.anthropic.com not in proxy allowlist |
| Prompt injection disabling controls | Gateway enforcement is infrastructure-level |

## Authentication Types

| Type | Source | Header Injected |
|------|--------|-----------------|
| API Key | `api_key` in secrets.yaml | `x-api-key: <key>` |
| OAuth Token | `oauth_token` in secrets.yaml | `Authorization: Bearer <token>` |
| GitHub App | `github_app` in secrets.yaml | GitHub App installation token |
| GitHub PAT | `pats` in secrets.yaml | `Authorization: token <pat>` |

OAuth takes precedence if both OAuth and API key are configured.

## Benefits

1. **Simpler**: No SSL MITM complexity for Anthropic traffic
2. **Officially supported**: Uses Claude Code's documented configuration
3. **More secure**: Credentials never in container environment
4. **Unified**: Same approach works for both public and private modes
5. **Better debugging**: HTTP traffic between container and gateway is inspectable

## Related ADRs

- [ADR: Git Isolation Architecture](git-isolation-architecture.md)
- [ADR: Network Isolation](network-isolation.md)
