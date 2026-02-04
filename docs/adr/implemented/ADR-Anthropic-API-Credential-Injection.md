# ADR: Anthropic API Credential Injection via ANTHROPIC_BASE_URL

**Status**: Implemented (PR #701)

## Summary

Use Claude Code's native `ANTHROPIC_BASE_URL` environment variable to route API traffic through the gateway for credential injection in both public and private modes. This supersedes the analysis in PR #699.

## Context

Claude Code officially supports custom API endpoints via `ANTHROPIC_BASE_URL` ([docs](https://code.claude.com/docs/en/llm-gateway)). This enables a clean architecture where:

1. **Public mode**: Container has direct internet access, but Anthropic API calls route through gateway
2. **Private mode**: All traffic routes through gateway proxy (existing behavior)

Both modes use the same credential injection mechanism, simplifying the architecture.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           PUBLIC MODE                                    │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────────┐    ANTHROPIC_BASE_URL     ┌─────────────────────┐  │
│  │  sandbox  │ ─────────────────────────▶│    egg-gateway      │  │
│  │                 │   http://egg-gateway:9847 │                     │  │
│  │  Claude Code    │   /v1/messages            │  Gateway API :9847  │──┼──▶ api.anthropic.com
│  │                 │                           │  - Inject creds     │  │
│  └────────┬────────┘                           │  - Forward request  │  │
│           │                                    └─────────────────────┘  │
│           │ Direct internet                                              │
│           ▼                                                              │
│     npm, pypi, github, web search, etc.                                  │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                          PRIVATE MODE                                    │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────────┐    ANTHROPIC_BASE_URL     ┌─────────────────────┐  │
│  │  sandbox  │ ─────────────────────────▶│    egg-gateway      │  │
│  │                 │   http://egg-gateway:9847 │                     │  │
│  │  Claude Code    │   /v1/messages            │  Gateway API :9847  │──┼──▶ api.anthropic.com
│  │                 │                           │  - Inject creds     │  │
│  └────────┬────────┘                           │  - Forward request  │  │
│           │                                    │                     │  │
│           │ HTTPS_PROXY                        │  Squid Proxy :3128  │──┼──▶ allowlist only
│           ▼                                    │  - Domain filtering │  │
│     ┌─────────────────────────────────────────▶│  - Audit logging    │  │
│     │  All other traffic                       └─────────────────────┘  │
│     │                                                                    │
└─────────────────────────────────────────────────────────────────────────┘
```

## Key Insight: No More SSL Bump for Anthropic

With `ANTHROPIC_BASE_URL`, Claude Code sends requests directly to our gateway over HTTP (internal network), not to `api.anthropic.com` over HTTPS. This means:

- **No SSL bump needed** for Anthropic API traffic
- **No CA certificate trust** required in container for Anthropic traffic
- **Simpler Squid config** - only needs to allow traffic, not MITM it
- **Gateway handles TLS** outbound to real api.anthropic.com

The gateway receives the plaintext request, adds credentials, then makes an authenticated HTTPS request to Anthropic.

## Relationship to PR #700 (ICAP)

This plan **supersedes** the ICAP credential injection approach from PR #700. The ANTHROPIC_BASE_URL approach is simpler and sufficient for our use case:

- **ICAP (PR #700)**: General-purpose HTTPS interception with credential injection via Squid's ICAP protocol. More complex, requires SSL bump, useful if we need to intercept arbitrary HTTPS traffic.
- **ANTHROPIC_BASE_URL (this plan)**: Claude Code-specific, officially supported, simpler architecture. Sufficient since our only credential injection target is the Anthropic API.

**Recommendation**: Close PR #700 after this implementation is complete. If future requirements emerge for intercepting other HTTPS traffic with credential injection, the ICAP approach can be revisited.

## Implementation Plan

### Phase 1: Gateway Anthropic Proxy Endpoint

Add HTTP endpoints to the gateway that proxy requests to Anthropic with credential injection. The gateway currently uses Flask (synchronous), so we'll use synchronous httpx with streaming support.

**File: `gateway/gateway.py`**

```python
import httpx
from flask import Response, stream_with_context

# Singleton client with connection pooling for performance
_anthropic_client = httpx.Client(
    base_url='https://api.anthropic.com',
    timeout=httpx.Timeout(120.0, connect=10.0),
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
)

# Headers to block - forward everything else for maximum compatibility
BLOCKED_HEADERS = {
    'host', 'content-length', 'transfer-encoding',
    'authorization', 'x-api-key', 'connection'
}

def _get_forwarded_headers(request_headers):
    """Forward all headers except blocked ones (blocklist approach)."""
    return {
        k: v for k, v in request_headers
        if k.lower() not in BLOCKED_HEADERS
    }

@app.route('/v1/messages', methods=['POST'])
def proxy_anthropic_messages():
    """Proxy messages API with credential injection and streaming support."""
    # Get credentials from existing anthropic_credentials.py
    cred = credentials_manager.get_credential()

    # Build headers with injected auth
    headers = _get_forwarded_headers(request.headers)
    if cred.token_type == 'oauth':
        headers['Authorization'] = f'Bearer {cred.token}'
    else:
        headers['x-api-key'] = cred.token

    # Check if streaming requested
    request_body = request.get_data()
    is_streaming = b'"stream":true' in request_body or b'"stream": true' in request_body

    if is_streaming:
        # Stream SSE response without buffering
        def generate():
            with _anthropic_client.stream(
                'POST',
                '/v1/messages',
                headers=headers,
                content=request_body,
            ) as response:
                # Yield headers via Flask's response mechanism (handled below)
                for chunk in response.iter_bytes():
                    yield chunk

        # Create streaming response, forwarding status and headers
        with _anthropic_client.stream(
            'POST',
            '/v1/messages',
            headers=headers,
            content=request_body,
        ) as upstream:
            response_headers = _filter_response_headers(upstream.headers)
            return Response(
                stream_with_context(_stream_response(upstream)),
                status=upstream.status_code,
                headers=response_headers,
                content_type='text/event-stream',
            )
    else:
        # Non-streaming: simple request/response
        response = _anthropic_client.post(
            '/v1/messages',
            headers=headers,
            content=request_body,
        )
        return Response(
            response.content,
            status=response.status_code,
            headers=_filter_response_headers(response.headers),
        )

def _stream_response(upstream_response):
    """Generator that yields chunks from upstream response."""
    for chunk in upstream_response.iter_bytes():
        yield chunk

def _filter_response_headers(headers):
    """Filter response headers for passthrough."""
    # Preserve important headers like x-request-id for debugging
    skip = {'content-encoding', 'transfer-encoding', 'connection'}
    return {k: v for k, v in headers.items() if k.lower() not in skip}

@app.route('/v1/messages/count_tokens', methods=['POST'])
def proxy_count_tokens():
    """Proxy token counting API (non-streaming)."""
    cred = credentials_manager.get_credential()

    headers = _get_forwarded_headers(request.headers)
    if cred.token_type == 'oauth':
        headers['Authorization'] = f'Bearer {cred.token}'
    else:
        headers['x-api-key'] = cred.token

    response = _anthropic_client.post(
        '/v1/messages/count_tokens',
        headers=headers,
        content=request.get_data(),
    )
    return Response(
        response.content,
        status=response.status_code,
        headers=_filter_response_headers(response.headers),
    )
```

**Key implementation details:**

1. **Streaming support**: Uses `httpx.stream()` with Flask's `stream_with_context` for SSE responses. This is critical as most Claude Code interactions use streaming.

2. **Header forwarding**: Uses a blocklist approach - forwards all headers except known problematic ones. This ensures compatibility with future Claude Code headers.

3. **Error passthrough**: Returns the full upstream response including status code and headers (like `x-request-id`) for debugging. No transformation or wrapping of error responses.

4. **Connection pooling**: Uses a singleton `httpx.Client` with connection pooling to reduce latency and connection overhead.

5. **No response buffering**: Streaming responses are yielded chunk-by-chunk without buffering entire responses in memory. This handles large responses (10MB+ for image tool outputs) efficiently.

### Phase 2: Container Environment Configuration

**File: `sandbox/entrypoint.py`**

```python
def setup_anthropic_api(config: Config, logger: Logger) -> None:
    """Configure Claude Code to use gateway for Anthropic API."""
    gateway_url = "http://egg-gateway:9847"

    # Set ANTHROPIC_BASE_URL to route API calls through gateway
    os.environ["ANTHROPIC_BASE_URL"] = gateway_url

    # Remove any ANTHROPIC_API_KEY from container environment
    # Credentials are held by gateway only
    if "ANTHROPIC_API_KEY" in os.environ:
        del os.environ["ANTHROPIC_API_KEY"]
    if "ANTHROPIC_OAUTH_TOKEN" in os.environ:
        del os.environ["ANTHROPIC_OAUTH_TOKEN"]

    logger.success(f"Anthropic API routed through gateway: {gateway_url}")
```

### Phase 3: Simplify Squid Configuration

**Private mode (`squid.conf`):**
- Remove SSL bump for api.anthropic.com (no longer needed)
- Keep domain allowlist filtering
- Keep SSL bump for peek/splice (SNI inspection)

**Public mode (`squid-allow-all.conf`):**
- No changes needed (already allows all traffic)
- Anthropic API traffic goes directly to gateway, not through Squid

### Phase 4: Remove Container CA Trust (Cleanup)

With ANTHROPIC_BASE_URL, the container no longer needs to trust the gateway CA for Anthropic traffic:

- Keep CA trust for private mode (other HTTPS traffic through proxy)
- Can simplify public mode (no proxy, no CA trust needed)

## Files to Modify

| File | Change |
|------|--------|
| `gateway/gateway.py` | Add `/v1/messages` and `/v1/messages/count_tokens` proxy endpoints |
| `sandbox/entrypoint.py` | Set `ANTHROPIC_BASE_URL`, remove API key from container env |
| `gateway/squid.conf` | Remove SSL bump for api.anthropic.com |
| `docs/adr/` | Document the credential injection architecture |

## Benefits Over Previous Approach

1. **Simpler**: No SSL MITM complexity for Anthropic traffic
2. **Officially supported**: Uses Claude Code's documented configuration
3. **More secure**: Credentials never in container environment
4. **Unified**: Same approach works for both public and private modes
5. **No NET_ADMIN**: Doesn't require special container capabilities
6. **Better debugging**: HTTP traffic between container and gateway is inspectable

## Claude Code Requirements

Based on [LLM Gateway docs](https://code.claude.com/docs/en/llm-gateway):

### Header Forwarding Strategy

The gateway uses a **blocklist approach** rather than an allowlist. This ensures compatibility with future Claude Code headers without code changes.

**Blocked headers** (replaced or managed by gateway):
- `host` - Replaced with api.anthropic.com
- `content-length` - Recalculated by httpx
- `transfer-encoding` - Managed by transport
- `authorization` - Injected by gateway
- `x-api-key` - Injected by gateway
- `connection` - Managed by transport

**All other headers are forwarded**, including but not limited to:
- `anthropic-version`
- `anthropic-beta`
- `content-type`
- Any future headers Claude Code may add

### Endpoints to Implement

1. `POST /v1/messages` - Main messages API
2. `POST /v1/messages/count_tokens` - Token counting

### Authentication

Gateway injects either:
- `Authorization: Bearer <token>` (for OAuth)
- `x-api-key: <key>` (for API key)

## Testing Plan

### Unit Tests
- Gateway proxy endpoint with mocked Anthropic responses
- Header filtering (blocklist behavior)
- Credential injection for both OAuth and API key modes
- Error response passthrough with preserved headers

### Streaming Tests (Critical Path)
- SSE message handling with `stream: true`
- Verify chunks are forwarded without buffering
- Test interruption/cancellation mid-stream
- Validate event stream format integrity

### Large Response Tests
- 10MB+ responses (image tool outputs, base64 images)
- Memory usage monitoring during large transfers
- Timeout behavior with slow responses

### Error Handling Tests
- Anthropic API 4xx errors (400 bad request, 401 unauthorized, 429 rate limited)
- Anthropic API 5xx errors (500, 502, 503)
- `x-request-id` header preservation for debugging
- Gateway internal errors (credentials unavailable, connection failures)

### Integration Tests
- Public mode: Claude Code can call API through gateway
- Private mode: Same behavior
- Credentials not visible in container environment
- Gateway logs show injected authentication
- Connection timeout and retry behavior

### E2E Tests
- Full conversation with Claude Code in both modes
- Verify responses are received correctly
- Rate limiting passthrough (429 responses honored by client)

## Migration Path

### From Current State (PR #698 merged)

1. Add gateway proxy endpoints (non-breaking)
2. Set ANTHROPIC_BASE_URL in container (takes precedence over direct API)
3. Verify both modes work
4. Remove SSL bump for api.anthropic.com from squid.conf
5. Clean up unused CA trust code for public mode

### Rollback

If issues arise:
1. Remove ANTHROPIC_BASE_URL from container env
2. Restore SSL bump in squid.conf
3. Container falls back to direct API calls with injected creds via MITM

## Resolved Questions

1. **Streaming responses**: ✅ Resolved. Using synchronous httpx with `stream()` method and Flask's `stream_with_context`. This yields chunks without buffering and works with Flask's sync model.

2. **Error handling**: ✅ Resolved. Gateway passes through Anthropic error responses unchanged, including status codes and headers (especially `x-request-id`). For gateway-internal errors (credentials unavailable, connection failures), return 502 Bad Gateway with a JSON error body.

3. **Flask vs async**: ✅ Resolved. Staying with Flask (sync) and using synchronous httpx. This avoids a migration to Quart/FastAPI. The blocking nature is acceptable since each request gets its own thread via the WSGI server (Gunicorn).

## Open Questions

1. **Health checks**: Should gateway health include Anthropic API reachability? Recommendation: No - keep health checks fast and local. Anthropic reachability can be a separate diagnostic endpoint.

## References

- [Claude Code LLM Gateway docs](https://code.claude.com/docs/en/llm-gateway)
- [Claude Code Network Config](https://code.claude.com/docs/en/network-config)
- [Claude Code Settings](https://code.claude.com/docs/en/settings)
- PR #698: Phase 1 SSL bump (merged)
- PR #699: Original public mode analysis (superseded by this plan)
- PR #700: ICAP credential injection (superseded by this plan - recommend closing)
