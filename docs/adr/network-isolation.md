# ADR: Network Isolation via Gateway Proxy

**Status:** Implemented
**Origin:** Extracted from james-in-a-box

## Summary

The egg gateway provides two network modes for sandbox containers:

1. **Public mode** (default): Full internet access, with Anthropic API routed through gateway for credential injection
2. **Private mode**: Complete network lockdown - only Anthropic API and private GitHub repos are accessible

Private mode provides infrastructure-enforced security guarantees that cannot be bypassed by prompt injection or agent misbehavior.

## Motivation

In public mode, the sandbox container can reach arbitrary internet endpoints:
- Web search could be used for data exfiltration
- Package installation could pull malicious dependencies
- Any HTTP endpoint could receive exfiltrated code or secrets

For truly unsupervised operation, we need infrastructure-level guarantees that the sandbox cannot communicate with unauthorized endpoints.

## Architecture

### Public Mode

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           PUBLIC MODE                                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────┐    ANTHROPIC_BASE_URL     ┌─────────────────────┐  │
│  │  Sandbox        │ ─────────────────────────▶│    Gateway          │  │
│  │  Container      │   http://gateway:8080     │                     │  │
│  │                 │   /v1/messages            │  - Inject creds     │──┼──▶ api.anthropic.com
│  │  Claude Code    │                           │  - Forward request  │  │
│  └────────┬────────┘                           └─────────────────────┘  │
│           │                                                             │
│           │ Direct internet                                             │
│           ▼                                                             │
│     npm, pypi, github, web search, etc.                                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Private Mode

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          PRIVATE MODE                                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────┐    ANTHROPIC_BASE_URL     ┌─────────────────────┐  │
│  │  Sandbox        │ ─────────────────────────▶│    Gateway          │  │
│  │  Container      │   http://gateway:8080     │                     │  │
│  │                 │   /v1/messages            │  - Inject creds     │──┼──▶ api.anthropic.com
│  │  Claude Code    │                           │  - Forward request  │  │
│  └────────┬────────┘                           │                     │  │
│           │                                    │  Squid Proxy :3128  │──┼──▶ allowlist only
│           │ HTTPS_PROXY                        │  - Domain filtering │  │
│           ▼                                    │  - Audit logging    │  │
│     ┌─────────────────────────────────────────▶│                     │  │
│     │  All other traffic                       └─────────────────────┘  │
│     │                                                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Network Topology (Private Mode)

```
┌─────────────────────────────────────────────────────────────────┐
│                        Host Machine                             │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              egg-isolated (internal: true)               │   │
│  │              Subnet: 172.30.0.0/24                       │   │
│  │              Gateway: NONE (no external route)           │   │
│  │                                                          │   │
│  │    ┌─────────────┐              ┌─────────────────┐      │   │
│  │    │   Sandbox   │              │  egg-gateway    │      │   │
│  │    │ 172.30.0.10 │◄────────────►│   172.30.0.2    │      │   │
│  │    │             │   REST API   │                 │      │   │
│  │    │ NO EXTERNAL │   Port 9847  │                 │      │   │
│  │    │   ROUTE     │              │                 │      │   │
│  │    └─────────────┘              └────────┬────────┘      │   │
│  │                                          │               │   │
│  └──────────────────────────────────────────│───────────────┘   │
│                                             │                   │
│  ┌──────────────────────────────────────────│───────────────┐   │
│  │              egg-external (bridge)       │               │   │
│  │              Subnet: 172.31.0.0/24       │               │   │
│  │                                          │               │   │
│  │                              ┌───────────┴─────────┐     │   │
│  │                              │  egg-gateway        │     │   │
│  │                              │    172.31.0.2       │     │   │
│  │                              │                     │     │   │
│  │                              │  CAN REACH:         │     │   │
│  │                              │  - api.anthropic.com│     │   │
│  │                              │  - github.com       │     │   │
│  │                              │  - api.github.com   │     │   │
│  │                              │  (via proxy filter) │     │   │
│  │                              └──────────┬──────────┘     │   │
│  │                                         │                │   │
│  └─────────────────────────────────────────│────────────────┘   │
│                                            │                    │
│                                            ▼                    │
│                                       Internet                  │
└─────────────────────────────────────────────────────────────────┘
```

**Key property:** Docker's `internal: true` network has no gateway to the outside world. The sandbox physically cannot route packets to the internet—there's no route in its network namespace.

## Domain Allowlist (Private Mode)

The gateway maintains a strict allowlist of permitted domains:

| Domain | Purpose | Required For |
|--------|---------|--------------|
| `api.anthropic.com` | Claude API | Claude Code operation |
| `api.github.com` | GitHub REST API | PR creation, issue management |
| `github.com` | Git operations | Push, fetch, clone |
| `raw.githubusercontent.com` | GitHub raw content | File downloads |
| `objects.githubusercontent.com` | Release assets | Binary downloads |
| `codeload.github.com` | Archive downloads | Zip/tarball downloads |
| `uploads.github.com` | File uploads | Release asset uploads |
| `avatars.githubusercontent.com` | User avatars | GitHub UI elements |

**Allowlist Properties:**
- **Exhaustive:** Only listed domains are permitted; all others blocked
- **Enforced at proxy:** Squid proxy validates destination before forwarding
- **SNI-based validation:** For HTTPS, the proxy inspects Server Name Indication (SNI) in TLS ClientHello—no MITM decryption required

## What Gets Blocked (Private Mode)

| Category | Examples | Impact | Mitigation |
|----------|----------|--------|------------|
| Package managers | pypi.org, npmjs.com | Can't install new packages | Pre-install required packages in image |
| Web search | google.com, bing.com | Can't search web | Use GitHub search, local docs |
| Documentation | docs.python.org | Can't fetch docs | Bundle offline docs in image |
| Arbitrary APIs | any other endpoint | Can't exfiltrate data | **This is the security goal** |

### Claude Code Tools in Private Mode

| Tool | Status | Reason |
|------|--------|--------|
| `WebFetch` | ❌ Blocked | Cannot reach arbitrary URLs |
| `WebSearch` | ❌ Blocked | Cannot reach search engines |
| `Bash` (curl, wget) | ❌ Blocked | Cannot reach arbitrary endpoints |
| GitHub MCP tools | ✓ Works | Routed through gateway |

This is an **intentional limitation**. For tasks requiring web research, use public mode or pre-populate context.

## Container Configuration (Private Mode)

### Environment Variables

```bash
# Proxy settings - all HTTP/HTTPS traffic through gateway
HTTP_PROXY=http://gateway:3128
HTTPS_PROXY=http://gateway:3128
http_proxy=http://gateway:3128
https_proxy=http://gateway:3128
NO_PROXY=localhost,127.0.0.1,gateway,egg-gateway
no_proxy=localhost,127.0.0.1,gateway,egg-gateway

# Network mode indicator
EGG_NETWORK_MODE=private
```

### DNS Resolution

The sandbox cannot perform external DNS lookups in private mode:

```yaml
# Sandbox container
dns: []  # No DNS servers configured
extra_hosts:
  - "gateway:172.30.0.2"  # Static entry for gateway
```

DNS resolution is handled by the proxy:
1. Sandbox sends hostname in CONNECT request
2. Gateway/Squid resolves hostname internally
3. Squid validates hostname against allowlist **before** resolving DNS

## Squid Proxy Configuration

```squid
# Network settings
http_port 3128 ssl-bump \
  cert=/etc/squid/squid-ca.pem \
  generate-host-certificates=on \
  dynamic_cert_mem_cache_size=4MB

# Access control lists
acl localnet src 172.30.0.0/24    # egg-isolated network
acl allowed_domains dstdomain "/etc/squid/allowed_domains.txt"

# Block direct IP connections (must use hostnames)
acl direct_ip url_regex ^https?://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+
http_access deny direct_ip

# SSL/TLS settings - peek at SNI without MITM
acl step1 at_step SslBump1
ssl_bump peek step1
ssl_bump splice allowed_domains
ssl_bump terminate all

# HTTP access rules
http_access allow localnet allowed_domains
http_access deny all

# Logging
access_log /var/log/squid/access.log squid

# No caching needed for API calls
cache deny all
```

## Security Properties

### Breakout Prevention

| Attack Vector | Mitigation |
|---------------|------------|
| Direct IP connection | No route—internal network has no external gateway; Squid blocks IP-based URLs |
| DNS tunneling | No DNS servers configured in sandbox container |
| Proxy bypass | No alternate route exists; proxy is the only path out |
| IP-based proxy bypass | Squid `direct_ip` ACL blocks `http[s]://IP` requests |
| Learned IP from context | Even if sandbox learns an IP, requests are blocked by ACL |

### Defense in Depth

```
Layer 1: Behavioral (CLAUDE.md instructions)
    ↓ Can be bypassed by prompt injection
Layer 2: Credential Isolation
    ↓ Sandbox has no credentials
Layer 3: Gateway Policy Enforcement
    ↓ Gateway validates all operations
Layer 4: Network Isolation (this ADR)
    ↓ Sandbox cannot reach unauthorized endpoints
Layer 5: Audit Logging
    ↓ All traffic visible for review
Layer 6: Human Review
    ↓ Human must approve all PRs
```

## Residual Risks (Private Mode)

Even with network lockdown, some exfiltration vectors remain:

| Vector | Mitigation |
|--------|------------|
| GitHub exfiltration (branch names, commit messages, PR bodies) | Audit logging, PR review, private repos |
| Claude API exfiltration (data in prompts) | Anthropic's logging |
| Supply chain (pre-installed packages) | Use pinned versions, scan images |

**Accepted risk:** These vectors are acknowledged. Mitigations provide detection capability and data stays within controlled repositories.

## CLI Flags

```bash
# Default: public mode
egg start --config egg.yaml

# Private mode: network locked down
egg start --config egg.yaml --private

# Explicit public mode
egg start --config egg.yaml --public
```

## Related ADRs

- [ADR: Git Isolation Architecture](git-isolation-architecture.md)
- [ADR: Credential Injection](credential-injection.md)
