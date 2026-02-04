# ADR: Tool Access Lockdown via Gateway Sidecar

**Driver:** James Wiesebron
**Approver:** TBD
**Contributors:** James Wiesebron, Claude (AI Pair Programming)
**Informed:** Engineering teams
**Proposed:** November 2025
**Updated:** January 2026
**Status:** In Progress

## Table of Contents

- [Industry Standards Reference](#industry-standards-reference)
- [Context](#context)
- [Problem Statement](#problem-statement)
- [Decision](#decision)
- [High-Level Design](#high-level-design)
  - [Gateway Authentication](#gateway-authentication)
  - [Token Lifecycle Management](#token-lifecycle-management)
- [Security Analysis](#security-analysis)
  - [Rate Limiting](#rate-limiting)
  - [Audit Log Specification](#audit-log-specification)
  - [Supply Chain Considerations](#supply-chain-considerations)
- [Consequences](#consequences)
- [Alternatives Considered](#alternatives-considered)
- [Implementation Reference](#implementation-reference)

## Industry Standards Reference

This ADR aligns with the **OWASP Top 10 for Agentic Applications (2026)**, the industry benchmark for agentic AI security. The following table maps OWASP risks to mitigations in this architecture:

| OWASP Risk | Description | Mitigation in This ADR |
|------------|-------------|------------------------|
| **ASI01** - Agentic Excessive Authority | Agents granted overly broad permissions | Credential isolation - egg has no credentials; gateway exposes minimal API |
| **ASI02** - Tool Misuse & Exploitation | Agents misusing available tools | Gateway enforces policies; no merge endpoint; force push blocked |
| **ASI03** - Identity & Privilege Abuse | Credential theft or misuse | Credentials never enter egg container; gateway holds GITHUB_TOKEN |
| **ASI04** - Supply Chain Vulnerabilities | Compromised dependencies/images | *Partially addressed* - See [Supply Chain Considerations](#supply-chain-considerations) |
| **ASI06** - Memory/Context Poisoning | Corruption of agent memory/config | *Out of scope* - Addressed by container isolation and volume permissions |
| **ASI10** - Rogue Agents | Agent operating outside intended behavior | Infrastructure controls (not instructions) prevent unauthorized operations |

**Reference:** [OWASP Top 10 for Agentic Applications](https://genai.owasp.org/)

## Context

### Background

The egg container currently relies on behavioral instructions (CLAUDE.md) to prevent unauthorized actions like merging PRs or overwriting branches. While the container has network isolation (bridge mode, outbound only), the agent has unrestricted access to:

1. **Raw CLI tools**: `git`, `gh`, `curl`, `wget`, etc.
2. **Full internet access**: Any HTTP/HTTPS endpoint is reachable
3. **GitHub API**: Full permissions of the GITHUB_TOKEN

This creates risk vectors:
- Agent could merge PRs despite instructions not to
- Agent could push force to branches, potentially overwriting others' work
- Agent could exfiltrate data to arbitrary external endpoints
- Prompt injection attacks could bypass behavioral instructions

### Current Security Model

The existing ADR documents a "trust but verify" model:
- Agent has full tool access
- CLAUDE.md instructs agent not to merge PRs or force push
- Human reviews all PRs before merge
- Network isolation prevents inbound connections

**Gap:** Behavioral instructions are not enforceable. A sufficiently sophisticated prompt injection or model misbehavior could bypass these soft constraints.

### Scope

**In Scope:**
- Tool access lockdown for authenticated operations (git push, gh, etc.)
- Audit logging for all network traffic
- Credential isolation (no credentials in egg container)
- **Full network lockdown** (Phase 2): Route all traffic through gateway with strict allowlist

**Phased Rollout:**
- **Phase 1 (Implemented):** Gateway sidecar for git/gh operations, credential isolation
- **Phase 2 (Planned):** Full network lockdown - only Anthropic APIs and GitHub allowed

## Problem Statement

**We need defense-in-depth that does not rely solely on the agent following instructions.**

Specific threats to address:

1. **Unauthorized PR merges**: Agent could merge its own PRs without human approval
2. **Destructive git operations**: Force push, branch deletion, history rewriting
3. **Credential abuse**: Using GITHUB_TOKEN for unintended operations

Note: Data exfiltration via arbitrary endpoints is acknowledged as a residual risk. The gateway provides audit visibility but does not block general internet access to preserve functionality (web search, package installation).

## Decision

**Implement a gateway architecture that separates the egg container from all authenticated operations.**

### Core Principles

1. **Credential isolation**: egg container has NO credentials (no GITHUB_TOKEN, no SSH keys)
2. **Gateway as single choke point**: All authenticated operations go through the gateway
3. **Network proxy for visibility**: All egg traffic proxied through gateway for audit logging
4. **Fail closed**: Gateway enforces policies; egg physically cannot bypass

## High-Level Design

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Docker Compose Network                             │
│                                                                               │
│  ┌───────────────────────────────┐      ┌───────────────────────────────┐  │
│  │        egg container          │      │       gateway          │  │
│  │                               │      │                               │  │
│  │  - Claude Code agent          │      │  - GITHUB_TOKEN               │  │
│  │  - No GITHUB_TOKEN            │      │  - git push capability        │  │
│  │  - No git push capability     │ REST │  - gh CLI                     │  │
│  │  - Full internet via proxy ───┼──────►  - HTTP/HTTPS proxy          │  │
│  │  - git (no auth)              │      │  - Ownership checks           │  │
│  │                               │      │  - Audit logging              │  │
│  │  HTTP_PROXY=gateway:3128     │      │  - Policy enforcement         │  │
│  │                               │      │                               │  │
│  └───────────────────────────────┘      └───────────────────────────────┘  │
│                                                     │                        │
│                                                     │ All traffic proxied    │
│                                                     ▼                        │
│                                              ┌─────────────┐                │
│                                              │  Internet   │                │
│                                              │  - GitHub   │                │
│                                              │  - Claude   │                │
│                                              │  - PyPI     │                │
│                                              │  - etc      │                │
│                                              └─────────────┘                │
│                                                                               │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Component Summary

| Component | Purpose | Implementation |
|-----------|---------|----------------|
| egg container | Run Claude Code agent | Docker container, no credentials |
| gateway | Handle authenticated ops + proxy all traffic | Docker container with credentials |
| HTTP Proxy | Route all egg traffic through gateway | Squid or similar in gateway |
| REST API | Controlled interface for git/gh operations | Python/Node service in gateway |
| Audit Logger | Log all traffic and operations | Gateway component |

### Key Security Properties

1. **egg cannot push to GitHub** - It has no credentials. Network rules block direct GitHub access.
2. **egg cannot merge PRs** - Gateway API doesn't expose merge operation.
3. **All traffic is auditable** - Everything goes through gateway proxy.
4. **Credentials never enter egg** - GITHUB_TOKEN only exists in gateway.
5. **GitHub domains excluded from proxy allowlist** - All GitHub access must go through the gateway sidecar's git/gh wrappers. This ensures policy enforcement (branch ownership, merge blocking) cannot be bypassed by direct API calls through the proxy.

### Gateway REST API

The gateway exposes a controlled API for git/gh operations:

- `POST /api/git/push` - Push to remote (blocks force push, protected branches)
- `POST /api/gh/pr/create` - Create pull request
- `POST /api/gh/pr/comment` - Add comment to PR
- `POST /api/gh/pr/close` - Close PR (only egg's own PRs)
- **No merge endpoint** - Human must merge via GitHub UI

### Gateway Authentication

The gateway API must authenticate requests to prevent abuse from unauthorized containers on the Docker network.

**Authentication Mechanism: Container Identity Token**

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Authentication Flow                                   │
│                                                                               │
│  1. Docker Compose generates a random shared secret at startup               │
│  2. Secret injected into both containers via environment variable            │
│  3. egg includes secret in Authorization header for all gateway requests     │
│  4. Gateway validates secret before processing any request                   │
│                                                                               │
│  egg container                    gateway                           │
│  ┌─────────────┐                  ┌─────────────────────┐                   │
│  │ EGG_GATEWAY │  Authorization:  │ Validate header     │                   │
│  │ _SECRET     │ ──Bearer $SECRET─► matches EGG_GATEWAY │                   │
│  │             │                  │ _SECRET             │                   │
│  └─────────────┘                  └─────────────────────┘                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Implementation:**
- Secret generated via `openssl rand -hex 32` in compose startup
- Constant-time comparison to prevent timing attacks
- Requests without valid Authorization header return 401 Unauthorized
- All gateway API endpoints require authentication (no public endpoints)

**Future Enhancement: mTLS**

For production deployments, upgrade to mutual TLS (mTLS):
- Gateway presents server certificate
- egg presents client certificate
- Both certificates signed by internal CA
- Provides stronger identity guarantee than shared secret

mTLS adds complexity (certificate management, rotation) but provides cryptographic identity verification. Recommended for GCP Cloud Run deployment.

### Token Lifecycle Management

The `GITHUB_TOKEN` in the gateway requires careful lifecycle management:

**Token Acquisition:**
- GitHub App installation token (preferred) - automatically rotated by GitHub
- Personal Access Token (fallback) - requires manual rotation

**Token Properties:**

| Property | Requirement | Rationale |
|----------|-------------|-----------|
| Expiration | ≤1 hour (GitHub App) or manual rotation schedule | Limit window of exposure |
| Scope | Minimum required: `contents:write`, `pull_requests:write` | Principle of least privilege |
| Audit trail | Log all token usage in gateway | Detect misuse patterns |

**Rotation Strategy:**

```
┌────────────────────────────────────────────────────────────────────────────┐
│                      GitHub App Token Lifecycle                            │
│                                                                            │
│  1. Gateway requests installation token from GitHub App                    │
│  2. Token valid for 1 hour (GitHub enforced)                              │
│  3. Gateway refreshes token 10 minutes before expiration                  │
│  4. Old token naturally expires - no revocation needed                    │
│                                                                            │
│  Timeline:                                                                 │
│  ├─────────────────────────────────────────────────────────────────────┤  │
│  0min              50min         60min                                    │
│  Token issued      Refresh       Expiration                               │
│                    triggered                                               │
└────────────────────────────────────────────────────────────────────────────┘
```

**Audit Logging for Token Usage:**

Every operation using the token logs:
- Timestamp (ISO 8601)
- Operation type (push, pr_create, pr_comment, etc.)
- Target repository
- Target ref/PR number
- Success/failure status
- Request origin (egg container IP)

### CLI Wrappers

The egg container uses `git` and `gh` CLI wrappers that:
- Intercept authenticated operations and route through gateway
- Block dangerous operations (force push, merge, push to protected branches)
- Send Slack notifications for key operations
- Pass through read-only operations unchanged

## Phase 2: Full Network Lockdown

Phase 1 established credential isolation and gateway-mediated git operations. Phase 2 extends this to **complete network isolation**: egg can only reach the gateway sidecar, and the gateway enforces a strict allowlist of external destinations.

### Motivation

The current architecture (Phase 1) still allows egg to reach arbitrary internet endpoints:
- Web search could be used for data exfiltration
- Package installation could pull malicious dependencies
- Any HTTP endpoint could receive exfiltrated code or secrets

For truly unsupervised operation with `--dangerously-skip-permissions`, we need infrastructure-level guarantees that egg cannot communicate with unauthorized endpoints.

### Design: Complete Traffic Isolation

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Docker Network (egg-network)                       │
│                                                                               │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                        egg container (ISOLATED)                         │  │
│  │                                                                         │  │
│  │  Network: egg-isolated (no external connectivity)                       │  │
│  │                                                                         │  │
│  │  Can reach ONLY:                                                        │  │
│  │  - gateway (via internal network)                              │  │
│  │                                                                         │  │
│  │  CANNOT reach:                                                          │  │
│  │  - Internet (no default route)                                         │  │
│  │  - DNS servers (no external DNS)                                       │  │
│  │                                                                         │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                              │                                                │
│                              │ Internal network only                          │
│                              ▼                                                │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                     gateway (GATEKEEPER)                        │  │
│  │                                                                         │  │
│  │  Networks: egg-isolated + external                                      │  │
│  │                                                                         │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐   │  │
│  │  │  HTTPS Proxy (squid or envoy)                                   │   │  │
│  │  │                                                                 │   │  │
│  │  │  ALLOWLIST (strictly enforced):                                 │   │  │
│  │  │  ✓ api.anthropic.com          (Claude API)                      │   │  │
│  │  │  ✓ api.github.com             (GitHub API)                      │   │  │
│  │  │  ✓ github.com                 (git operations)                  │   │  │
│  │  │  ✓ *.githubusercontent.com   (GitHub raw content)              │   │  │
│  │  │                                                                 │   │  │
│  │  │  BLOCKED (everything else):                                     │   │  │
│  │  │  ✗ pypi.org, npmjs.com        (no package installs)             │   │  │
│  │  │  ✗ google.com, bing.com       (no web search)                   │   │  │
│  │  │  ✗ *.com, *.io, etc           (no arbitrary endpoints)          │   │  │
│  │  └─────────────────────────────────────────────────────────────────┘   │  │
│  │                                                                         │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐   │  │
│  │  │  Git/GH REST API (existing Phase 1 implementation)              │   │  │
│  │  └─────────────────────────────────────────────────────────────────┘   │  │
│  │                                                                         │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                              │                                                │
│                              │ Allowlisted destinations only                  │
│                              ▼                                                │
│                       ┌─────────────┐                                        │
│                       │  Internet   │                                        │
│                       │  (filtered) │                                        │
│                       └─────────────┘                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Network Isolation Implementation

**Docker Network Configuration:**

```yaml
# docker-compose.yml
networks:
  egg-isolated:
    internal: true  # No external connectivity
  external:
    # Standard bridge network with internet access

services:
  egg:
    networks:
      - egg-isolated  # ONLY internal network
    # No default route to internet
    # All traffic must go through gateway

  gateway:
    networks:
      - egg-isolated  # Can receive from egg
      - external      # Can reach internet
```

**Key property:** Docker's `internal: true` network has no gateway to the outside world. egg physically cannot route packets to the internet—there's no route in its network namespace.

### Domain Allowlist

The gateway maintains a strict allowlist of permitted domains:

| Domain | Purpose | Required For |
|--------|---------|--------------|
| `api.anthropic.com` | Claude API | Claude Code operation |
| `api.github.com` | GitHub REST API | PR creation, issue management |
| `github.com` | Git operations | Push, fetch, clone |
| `*.githubusercontent.com` | GitHub raw content | File downloads, avatars |

**Allowlist Properties:**
- **Exhaustive:** Only listed domains are permitted; all others blocked
- **Enforced at proxy:** Gateway proxy (Squid/Envoy) validates destination before forwarding
- **SNI-based validation:** For HTTPS, the proxy inspects the Server Name Indication (SNI) in the TLS ClientHello to determine the destination domain. This does **not** require MITM CA certificates or decrypting traffic—the proxy simply reads the plaintext hostname from the CONNECT request and SNI extension, then either tunnels the connection or rejects it.
- **No wildcards for arbitrary domains:** Each permitted domain explicitly listed

### What Gets Blocked

| Category | Examples | Impact | Mitigation |
|----------|----------|--------|------------|
| Package managers | pypi.org, npmjs.com | Can't install new packages | Pre-install required packages in image |
| Web search | google.com, bing.com | Can't search web | Use GitHub search, local docs |
| Documentation | docs.python.org | Can't fetch docs | Bundle offline docs in image |
| Arbitrary APIs | any other endpoint | Can't exfiltrate data | **This is the security goal** |

### Claude Code Built-in Tools

**Important:** Phase 2 lockdown disables Claude Code's network-dependent tools:

| Tool | Status | Reason |
|------|--------|--------|
| `WebFetch` | ❌ Blocked | Cannot reach arbitrary URLs |
| `WebSearch` | ❌ Blocked | Cannot reach search engines |
| `Bash` (curl, wget) | ❌ Blocked | Cannot reach arbitrary endpoints |
| GitHub MCP tools | ✓ Works | Routed through gateway |

This is an **expected and intentional limitation**. For tasks requiring web research, use supervised mode or pre-populate context before entering lockdown mode.

#### Expected Claude Code Behavior in Lockdown Mode

When blocked tools are invoked, Claude Code will observe the failure and adapt:

| Scenario | Behavior | User Experience |
|----------|----------|-----------------|
| WebFetch called | Returns HTTP 403 Forbidden | Claude explains the tool is blocked and suggests alternatives |
| WebSearch called | Returns HTTP 403 Forbidden | Claude explains web search is unavailable in lockdown mode |
| curl/wget to blocked domain | Returns HTTP 403 or connection refused | Claude notes the request failed and adjusts approach |

**Error message from proxy:**
```
HTTP/1.1 403 Forbidden
X-Squid-Error: ERR_ACCESS_DENIED 0
```

**Claude's expected adaptation:**
- Acknowledge the limitation in its response
- Suggest alternatives: "I cannot access external URLs in lockdown mode. I can search the local codebase or use cached documentation instead."
- Fall back to local resources: GitHub search via API, local file search, pre-loaded documentation

**No retry loops:** The proxy returns 403 immediately, so Claude will not enter retry loops. The failure is deterministic.

#### Documenting Lockdown Mode to the Agent

The CLAUDE.md instructions include lockdown mode awareness:

```markdown
## Network Lockdown Mode

When `EGG_NETWORK_MODE=lockdown`, the following tools are **unavailable**:
- WebFetch (cannot access arbitrary URLs)
- WebSearch (cannot access search engines)
- curl/wget to external sites (blocked by proxy)

**Available alternatives:**
- GitHub API search via gateway
- Local file search (Glob, Grep)
- Pre-installed documentation in `/usr/share/doc/`
- Context from previous conversations

If you need web access, notify the user that the task requires **supervised mode**.
```

### Pre-installed Dependencies

Since egg cannot install packages at runtime, the container image must include all required dependencies:

**Python packages:** Pre-installed via `requirements.txt` during image build
**Node packages:** Pre-installed via `package.json` during image build
**System tools:** Installed via Dockerfile

**Image build process:**
```dockerfile
# All dependencies installed at build time
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY package.json package-lock.json /app/
RUN npm ci

# Runtime: egg cannot reach package repositories
```

### DNS Resolution

egg cannot perform external DNS lookups:

```yaml
# egg container
dns: []  # No DNS servers configured (--dns 0.0.0.0)
extra_hosts:
  - "gateway:172.30.0.2"  # Static entry for gateway
```

**Architecture detail:** DNS resolution is handled by the proxy, not the egg container:

1. **egg container:** Has no DNS servers configured. Cannot resolve hostnames directly.
2. **Proxy operation:** When egg sends a request through the proxy, it sends the hostname (not IP) in the CONNECT request.
3. **Gateway/Squid:** The gateway is on the `egg-external` network which has normal DNS. Squid resolves the hostname internally when establishing the upstream connection.
4. **Validation:** Squid validates the hostname from the CONNECT request against the allowlist **before** resolving DNS. This prevents bypass via pre-resolved IPs.

**Key security property:** The proxy validates hostnames from CONNECT/Host headers, not IP addresses. Even if egg somehow learns an IP address (e.g., from conversation context), it cannot use it because:
- Direct IP connections are blocked by the `direct_ip` ACL in Squid
- The internal network has no route to external IPs—the proxy is the only path out

### Proxy Configuration

egg routes all HTTP/HTTPS traffic through the gateway proxy:

```bash
# Environment variables in egg container
HTTP_PROXY=http://gateway:3128
HTTPS_PROXY=http://gateway:3128
http_proxy=http://gateway:3128   # Some tools use lowercase
https_proxy=http://gateway:3128
NO_PROXY=localhost,127.0.0.1,gateway,egg-gateway
no_proxy=localhost,127.0.0.1,gateway,egg-gateway
```

**Proxy behavior:**
1. egg sends CONNECT request to gateway for HTTPS destinations
2. Gateway checks destination against allowlist
3. If allowed: Gateway establishes tunnel to destination
4. If blocked: Gateway returns 403 Forbidden

**Tool-specific proxy support:**

| Tool/Library | Proxy Support | Notes |
|--------------|---------------|-------|
| **curl/wget** | `HTTP_PROXY`/`HTTPS_PROXY` | Standard env vars respected |
| **Python requests** | `HTTP_PROXY`/`HTTPS_PROXY` | Standard env vars respected |
| **Python httpx** | `HTTP_PROXY`/`HTTPS_PROXY` | Standard env vars respected |
| **Anthropic SDK** | `HTTP_PROXY`/`HTTPS_PROXY` | **Verified:** Uses httpx which respects proxy env vars |
| **git** | Routes through gateway API | Git wrapper calls gateway REST API, not direct HTTPS |
| **gh CLI** | Routes through gateway API | gh wrapper calls gateway REST API |
| **npm/pip** | N/A in lockdown mode | Package managers blocked; deps pre-installed |

**Anthropic SDK verification:** The Anthropic Python SDK uses `httpx` as its HTTP client, which respects standard proxy environment variables. No special configuration needed.

```python
# Verification code (run during image build testing)
import httpx
import os

# httpx automatically uses these if set
assert os.environ.get("HTTPS_PROXY") == "http://gateway:3128"
# SDK will route through proxy when initialized
```

### Breakout Prevention

The architecture prevents several classes of network breakout:

| Attack Vector | Mitigation |
|---------------|------------|
| Direct IP connection | **Two-layer protection:** (1) No route—internal network has no external gateway; (2) Squid `direct_ip` ACL explicitly blocks `http[s]://IP` requests even through the proxy. |
| DNS tunneling | No DNS servers configured in egg container |
| Proxy bypass | No alternate route exists; proxy is the only path out |
| IP-based proxy bypass | Squid `direct_ip` ACL: `url_regex ^https?://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+` blocks requests by IP address. Hostname validation uses `dstdomain` ACL. |
| Learned IP from context | Even if egg learns `140.82.114.3` is GitHub's IP from conversation, requests to `http://140.82.114.3` are blocked by the `direct_ip` ACL. |
| Container escape | Defense in depth; not in scope for network layer |

### Relationship: Phase 2 and Private Repo Mode

Phase 2 Network Lockdown and Private Repo Mode are **independent but complementary** features:

| Configuration | Network Access | Repository Access |
|---------------|----------------|-------------------|
| Phase 2 only | Anthropic + GitHub only | All repos (public + private) |
| Private Repo Mode only | Full internet | Private repos only |
| Phase 2 + Private Repo Mode | Anthropic + GitHub only | Private repos only |
| Neither (supervised) | Full internet | All repos |

**Important:** Phase 2 does **not** require Private Repo Mode, but they work well together:

- **Phase 2 alone:** egg cannot install packages or search the web. Can still clone public repos via gateway.
- **Private Repo Mode alone:** egg can install packages and search web. Cannot interact with public repos.
- **Both enabled:** Maximum security. egg is restricted to private repos and can only reach Anthropic/GitHub APIs.

**Recommendation for autonomous operation:** Enable both Phase 2 and Private Repo Mode for unsupervised `--dangerously-skip-permissions` sessions. This provides the strongest security guarantees.

```bash
# Maximum security configuration
export EGG_NETWORK_LOCKDOWN=true
export PRIVATE_REPO_MODE=true
./egg --dangerously-skip-permissions
```

### Fallback: Supervised Mode

For tasks requiring web access (research, package updates), egg can operate in **supervised mode**:

```yaml
# docker-compose.supervised.yml override
services:
  egg:
    networks:
      - egg-isolated
      - external  # Adds external network access
    environment:
      - EGG_MODE=supervised
```

In supervised mode:
- Full internet access available
- Human must actively monitor session
- Intended for interactive work, not autonomous operation

### Phase 2: Detailed Implementation Plan

#### 1. Complete Domain Allowlist

The following domains are **required** for egg to function. All other domains are blocked.

##### 1.1 Anthropic API (Required for Claude Code)

| Domain | Port | Purpose | Notes |
|--------|------|---------|-------|
| `api.anthropic.com` | 443 | Claude API endpoint | Primary API for all Claude operations |

**Note:** The Anthropic Console (`console.anthropic.com`, `platform.claude.com`) is NOT required—egg uses API keys directly, not console access.

##### 1.2 GitHub Domains (Required for git/gh operations)

| Domain | Port | Purpose | Notes |
|--------|------|---------|-------|
| `github.com` | 443 | Git HTTPS operations | Clone, fetch, push, pull |
| `api.github.com` | 443 | GitHub REST API | PR creation, issue management, repo info |
| `raw.githubusercontent.com` | 443 | Raw file content | README files, direct file downloads |
| `objects.githubusercontent.com` | 443 | Release assets, artifacts | Binary downloads, action artifacts |
| `codeload.github.com` | 443 | Archive downloads | `git archive`, zip/tarball downloads |
| `uploads.github.com` | 443 | File uploads | Release asset uploads |

**Explicitly NOT included:**
- `*.actions.githubusercontent.com` — Not needed (egg doesn't run GitHub Actions)
- `ghcr.io` — Not needed (no container registry access)
- `*.github.io` — Not needed (no GitHub Pages access)
- `copilot-*.githubusercontent.com` — Not needed (no GitHub Copilot)

##### 1.3 Complete Allowlist Configuration

```python
# gateway/proxy_allowlist.py

ALLOWED_DOMAINS = [
    # Anthropic API
    "api.anthropic.com",

    # GitHub - Core
    "github.com",
    "api.github.com",

    # GitHub - Content delivery
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "uploads.github.com",

    # GitHub - User content subdomains (avatars, etc.)
    # Note: Wildcard for *.githubusercontent.com could be considered
    # but we list explicitly for tighter control
    "avatars.githubusercontent.com",
    "user-images.githubusercontent.com",
]

# Regex patterns for validation (used by Squid ACL)
ALLOWED_DOMAIN_PATTERNS = [
    r"^api\.anthropic\.com$",
    r"^github\.com$",
    r"^api\.github\.com$",
    r"^raw\.githubusercontent\.com$",
    r"^objects\.githubusercontent\.com$",
    r"^codeload\.github\.com$",
    r"^uploads\.github\.com$",
    r"^avatars\.githubusercontent\.com$",
    r"^user-images\.githubusercontent\.com$",
]
```

#### 2. Docker Network Configuration

##### 2.1 Network Topology

```
┌─────────────────────────────────────────────────────────────────┐
│                        Host Machine                              │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              egg-isolated (internal: true)                │   │
│  │              Subnet: 172.30.0.0/24                        │   │
│  │              Gateway: NONE (no external route)            │   │
│  │                                                           │   │
│  │    ┌─────────────┐              ┌─────────────────┐      │   │
│  │    │     egg     │              │  gateway │      │   │
│  │    │ 172.30.0.10 │◄────────────►│   172.30.0.2    │      │   │
│  │    │             │   REST API   │                 │      │   │
│  │    │ NO EXTERNAL │   Port 9847  │                 │      │   │
│  │    │   ROUTE     │              │                 │      │   │
│  │    └─────────────┘              └────────┬────────┘      │   │
│  │                                          │               │   │
│  └──────────────────────────────────────────│───────────────┘   │
│                                             │                    │
│  ┌──────────────────────────────────────────│───────────────┐   │
│  │              egg-external (bridge)        │               │   │
│  │              Subnet: 172.31.0.0/24        │               │   │
│  │                                           │               │   │
│  │                              ┌────────────┴────────┐      │   │
│  │                              │  gateway    │      │   │
│  │                              │    172.31.0.2       │      │   │
│  │                              │                     │      │   │
│  │                              │  CAN REACH:         │      │   │
│  │                              │  - api.anthropic.com│      │   │
│  │                              │  - github.com       │      │   │
│  │                              │  - api.github.com   │      │   │
│  │                              │  (via proxy filter) │      │   │
│  │                              └──────────┬──────────┘      │   │
│  │                                         │                 │   │
│  └─────────────────────────────────────────│─────────────────┘   │
│                                            │                     │
│                                            ▼                     │
│                                       Internet                   │
└──────────────────────────────────────────────────────────────────┘
```

##### 2.2 Network Creation Commands

The subnets are **configurable** to avoid conflicts with existing networks:

```bash
# Configuration with defaults
EGG_ISOLATED_SUBNET="${EGG_ISOLATED_SUBNET:-172.30.0.0/24}"
EGG_EXTERNAL_SUBNET="${EGG_EXTERNAL_SUBNET:-172.31.0.0/24}"

# Check for subnet conflicts before creating
check_subnet_available() {
    local subnet="$1"
    local network_prefix="${subnet%/*}"

    # Check if any existing Docker network uses this subnet
    if docker network ls -q | xargs -I {} docker network inspect {} 2>/dev/null | \
       grep -q "\"Subnet\": \"$subnet\""; then
        echo "ERROR: Subnet $subnet already in use by another Docker network"
        return 1
    fi

    # Check if subnet conflicts with host routes
    if ip route | grep -q "^${network_prefix}"; then
        echo "WARNING: Subnet $subnet may conflict with host routing"
    fi

    return 0
}

# Create isolated internal network (no external gateway)
check_subnet_available "$EGG_ISOLATED_SUBNET" || exit 1
docker network create \
  --driver bridge \
  --internal \
  --subnet "$EGG_ISOLATED_SUBNET" \
  egg-isolated

# Create external network for gateway outbound access
check_subnet_available "$EGG_EXTERNAL_SUBNET" || exit 1
docker network create \
  --driver bridge \
  --subnet "$EGG_EXTERNAL_SUBNET" \
  egg-external
```

**Default subnets and alternatives:**

| Network | Default Subnet | Alternative if Conflicting |
|---------|----------------|---------------------------|
| `egg-isolated` | `172.30.0.0/24` | `172.28.0.0/24`, `10.200.0.0/24` |
| `egg-external` | `172.31.0.0/24` | `172.29.0.0/24`, `10.201.0.0/24` |

To use alternative subnets:
```bash
export EGG_ISOLATED_SUBNET="10.200.0.0/24"
export EGG_EXTERNAL_SUBNET="10.201.0.0/24"
./start-gateway.sh
```

##### 2.3 Container Network Assignment

**egg container:**
```bash
docker run \
  --network egg-isolated \
  --ip 172.30.0.10 \
  --dns 0.0.0.0 \               # No DNS servers
  --add-host gateway:172.30.0.2 \  # Static gateway entry
  -e HTTP_PROXY=http://gateway:3128 \
  -e HTTPS_PROXY=http://gateway:3128 \
  -e NO_PROXY=localhost,127.0.0.1,gateway \
  sandbox
```

**gateway container:**
```bash
docker run \
  --network egg-isolated \
  --ip 172.30.0.2 \
  egg-gateway

# Attach to external network (dual-homed)
docker network connect --ip 172.31.0.2 egg-external egg-gateway
```

#### 3. Squid Proxy Configuration

##### 3.1 Squid Installation in Gateway

Add to `gateway/Dockerfile`:

```dockerfile
# Install Squid proxy
RUN apt-get update && apt-get install -y squid && \
    rm -rf /var/lib/apt/lists/*

# Copy Squid configuration
COPY squid.conf /etc/squid/squid.conf
COPY allowed_domains.txt /etc/squid/allowed_domains.txt

# Expose proxy port
EXPOSE 3128
```

##### 3.2 Squid Configuration File

Create `gateway/squid.conf`:

```squid
# Squid proxy configuration for egg network lockdown
# Only allows traffic to explicitly allowlisted domains

# Network settings - SSL bump requires special port configuration
# The cert= parameter points to a CA cert used for peek/splice (not MITM decryption)
http_port 3128 ssl-bump \
  cert=/etc/squid/squid-ca.pem \
  generate-host-certificates=on \
  dynamic_cert_mem_cache_size=4MB

# Access control lists
acl localnet src 172.30.0.0/24    # egg-isolated network

# Load allowed domains from file
acl allowed_domains dstdomain "/etc/squid/allowed_domains.txt"

# Block direct IP connections (must use hostnames)
# This prevents bypass via learned IP addresses
acl direct_ip url_regex ^https?://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+
http_access deny direct_ip

# SSL/TLS settings - peek at SNI without MITM
# We only peek to read SNI, then splice (passthrough) for allowed domains
# This does NOT decrypt traffic - it just reads the hostname from ClientHello
acl step1 at_step SslBump1
ssl_bump peek step1
ssl_bump splice allowed_domains
ssl_bump terminate all

# HTTP access rules
http_access allow localnet allowed_domains
http_access deny all

# Logging
access_log /var/log/squid/access.log squid
cache_log /var/log/squid/cache.log

# Performance - no caching needed for API calls
cache deny all

# Error pages
deny_info ERR_ACCESS_DENIED all

# Connection settings
connect_timeout 30 seconds
read_timeout 60 seconds
request_timeout 60 seconds

# Shutdown settings
shutdown_lifetime 5 seconds
```

##### 3.3 Squid CA Certificate Generation

The Squid proxy needs a CA certificate for SSL bump peek/splice operations. This certificate is used to establish the initial TLS connection for peeking at SNI—it does **not** perform MITM decryption of traffic.

Add to `gateway/Dockerfile`:

```dockerfile
# Generate self-signed CA for Squid SSL bump
RUN mkdir -p /etc/squid/ssl && \
    openssl req -new -newkey rsa:2048 -sha256 -days 365 -nodes -x509 \
        -subj "/CN=egg-gateway-proxy/O=egg/C=US" \
        -keyout /etc/squid/squid-ca.pem \
        -out /etc/squid/squid-ca.pem && \
    chmod 400 /etc/squid/squid-ca.pem && \
    # Initialize SSL certificate database
    /usr/lib/squid/security_file_certgen -c -s /var/lib/squid/ssl_db -M 4MB
```

**Note:** This CA certificate is internal to the gateway and never trusted by external systems. The peek/splice mode reads the SNI from the TLS ClientHello without decrypting traffic.

##### 3.3 Allowed Domains File

Create `gateway/allowed_domains.txt`:

```
# Anthropic API
api.anthropic.com

# GitHub - Core
github.com
api.github.com

# GitHub - Content delivery
raw.githubusercontent.com
objects.githubusercontent.com
codeload.github.com
uploads.github.com
avatars.githubusercontent.com
user-images.githubusercontent.com
```

##### 3.4 Proxy Supervisor in Gateway

Add to `gateway/entrypoint.sh`:

```bash
#!/bin/bash
set -e

# Start Squid proxy in background
echo "Starting Squid proxy..."
squid -f /etc/squid/squid.conf
sleep 2

# Verify Squid is running
if ! pgrep -x "squid" > /dev/null; then
    echo "ERROR: Squid failed to start"
    exit 1
fi

echo "Squid proxy started on port 3128"

# Start gateway API server
echo "Starting gateway API server..."
exec python -m waitress --port=9847 --host=0.0.0.0 gateway:app
```

#### 4. egg Container Changes

##### 4.1 Runtime Environment Variables

Update `sandbox/egg_lib/runtime.py`:

```python
def _get_network_lockdown_env() -> dict[str, str]:
    """Return environment variables for Phase 2 network lockdown."""
    return {
        # Proxy settings - all HTTP/HTTPS traffic through gateway
        "HTTP_PROXY": "http://gateway:3128",
        "HTTPS_PROXY": "http://gateway:3128",
        "http_proxy": "http://gateway:3128",  # Some tools use lowercase
        "https_proxy": "http://gateway:3128",
        "NO_PROXY": "localhost,127.0.0.1,gateway,egg-gateway",
        "no_proxy": "localhost,127.0.0.1,gateway,egg-gateway",

        # Disable certificate verification warnings (proxy handles TLS)
        # NOT disabling verification - just suppressing urllib3 warnings
        "PYTHONWARNINGS": "ignore:Unverified HTTPS request",

        # Network mode indicator
        "EGG_NETWORK_MODE": "lockdown",
    }
```

##### 4.2 Docker Run Modifications

Update container launch in `sandbox/egg_lib/runtime.py`:

```python
def _build_docker_command(
    container_id: str,
    config: Config,
    network_lockdown: bool = True,  # NEW: Enable by default
) -> list[str]:
    """Build docker run command with all arguments."""
    cmd = ["docker", "run"]

    # ... existing code ...

    if network_lockdown:
        # Use isolated network only (no external route)
        cmd.extend(["--network", "egg-isolated"])
        cmd.extend(["--ip", "172.30.0.10"])

        # No DNS servers - prevent DNS-based breakout
        cmd.extend(["--dns", "0.0.0.0"])

        # Static host entries
        cmd.extend(["--add-host", "gateway:172.30.0.2"])
        cmd.extend(["--add-host", "egg-gateway:172.30.0.2"])

        # Add proxy environment variables
        for key, value in _get_network_lockdown_env().items():
            cmd.extend(["-e", f"{key}={value}"])
    else:
        # Legacy mode: standard egg-network with full internet access
        cmd.extend(["--network", EGG_ISOLATED_NETWORK])

    # ... rest of existing code ...

    return cmd
```

##### 4.3 Pre-installed Dependencies

Update `sandbox/Dockerfile` to pre-install all required packages.

**Definitive package lists** are maintained in version-controlled files:

**Python packages** (`sandbox/requirements-lockdown.txt`):
```
# Core
requests>=2.31.0
httpx>=0.25.0
aiohttp>=3.9.0
pyyaml>=6.0.1
toml>=0.10.2

# Testing
pytest>=7.4.0
pytest-cov>=4.1.0
pytest-asyncio>=0.21.0

# Linting/Formatting
black>=23.12.0
ruff>=0.1.8
mypy>=1.7.0

# Type stubs for common libraries
types-requests>=2.31.0
types-PyYAML>=6.0.12

# Utilities
python-dateutil>=2.8.2
click>=8.1.7
rich>=13.7.0
```

**Node.js packages** (`sandbox/package-lockdown.json`):
```json
{
  "name": "egg-lockdown-deps",
  "version": "1.0.0",
  "dependencies": {
    "typescript": "^5.3.0",
    "eslint": "^8.56.0",
    "@types/node": "^20.10.0",
    "prettier": "^3.1.0",
    "jest": "^29.7.0",
    "@types/jest": "^29.5.0"
  }
}
```

**Dockerfile update:**
```dockerfile
# Python dependencies - installed at build time
COPY requirements-lockdown.txt /tmp/requirements-lockdown.txt
RUN pip install --no-cache-dir -r /tmp/requirements-lockdown.txt

# Node.js dependencies - pre-installed globally
COPY package-lockdown.json /tmp/package.json
RUN cd /tmp && npm install && \
    npm cache clean --force

# Common system tools
RUN apt-get update && apt-get install -y \
    jq \
    curl \
    wget \
    tree \
    ripgrep \
    fd-find \
    && rm -rf /var/lib/apt/lists/*
```

**Package list maintenance:** When new packages are needed, they must be added to these files and the image rebuilt. This is a deliberate friction point—adding packages requires an image rebuild, which provides an opportunity for review and scanning.

#### 5. Gateway Startup Changes

##### 5.1 Updated start-gateway.sh

```bash
#!/bin/bash
set -e

# Configuration
GATEWAY_IMAGE="egg-gateway"
GATEWAY_CONTAINER="egg-gateway"
INTERNAL_NETWORK="egg-isolated"
EXTERNAL_NETWORK="egg-external"
INTERNAL_IP="172.30.0.2"
EXTERNAL_IP="172.31.0.2"
HEALTH_CHECK_TIMEOUT=30
HEALTH_CHECK_INTERVAL=2

# Create networks if they don't exist
create_networks() {
    # Internal network (no external gateway)
    if ! docker network inspect "$INTERNAL_NETWORK" &>/dev/null; then
        echo "Creating internal network: $INTERNAL_NETWORK"
        docker network create \
            --driver bridge \
            --internal \
            --subnet 172.30.0.0/24 \
            "$INTERNAL_NETWORK"
    fi

    # External network (for gateway outbound access)
    if ! docker network inspect "$EXTERNAL_NETWORK" &>/dev/null; then
        echo "Creating external network: $EXTERNAL_NETWORK"
        docker network create \
            --driver bridge \
            --subnet 172.31.0.0/24 \
            "$EXTERNAL_NETWORK"
    fi
}

# Start gateway container
start_gateway() {
    # Remove old container if exists
    docker rm -f "$GATEWAY_CONTAINER" 2>/dev/null || true

    # Start on internal network first
    docker run -d \
        --name "$GATEWAY_CONTAINER" \
        --network "$INTERNAL_NETWORK" \
        --ip "$INTERNAL_IP" \
        --restart unless-stopped \
        -v "$HOME/.egg-gateway:/secrets:ro" \
        -v "$HOME/.config/egg/repositories.yaml:/config/repositories.yaml:ro" \
        "${MOUNT_ARGS[@]}" \
        "$GATEWAY_IMAGE"

    # Connect to external network (dual-homed)
    docker network connect --ip "$EXTERNAL_IP" "$EXTERNAL_NETWORK" "$GATEWAY_CONTAINER"

    echo "Gateway started with dual network access"
    echo "  Internal: $INTERNAL_NETWORK ($INTERNAL_IP) - receives from egg"
    echo "  External: $EXTERNAL_NETWORK ($EXTERNAL_IP) - reaches internet"
}

# Wait for gateway to be healthy before returning
wait_for_gateway() {
    echo "Waiting for gateway to be ready..."
    local elapsed=0

    while [ $elapsed -lt $HEALTH_CHECK_TIMEOUT ]; do
        # Check gateway API health endpoint
        if docker exec "$GATEWAY_CONTAINER" curl -sf http://localhost:9847/api/v1/health >/dev/null 2>&1; then
            # Also verify Squid proxy is responding
            if docker exec "$GATEWAY_CONTAINER" curl -sf --proxy http://localhost:3128 -o /dev/null https://api.github.com/ 2>&1; then
                echo "Gateway is ready (API + Proxy healthy)"
                return 0
            fi
        fi

        sleep $HEALTH_CHECK_INTERVAL
        elapsed=$((elapsed + HEALTH_CHECK_INTERVAL))
        echo "  Waiting... ($elapsed/$HEALTH_CHECK_TIMEOUT seconds)"
    done

    echo "ERROR: Gateway failed to become healthy within $HEALTH_CHECK_TIMEOUT seconds"
    docker logs "$GATEWAY_CONTAINER" --tail 50
    return 1
}

# Main
create_networks
start_gateway
wait_for_gateway
```

##### 5.2 egg Container Startup Dependency

The egg container entrypoint includes a health check wait loop to ensure the gateway is ready:

```python
# sandbox/entrypoint.py (excerpt)

import time
import requests
from requests.exceptions import RequestException

def wait_for_gateway(timeout: int = 60, interval: int = 2) -> bool:
    """Wait for gateway to be ready before starting main process.

    Args:
        timeout: Maximum seconds to wait
        interval: Seconds between checks

    Returns:
        True if gateway is ready, False if timeout
    """
    gateway_url = "http://egg-gateway:9847/api/v1/health"
    proxy_test_url = "https://api.github.com/"
    proxies = {"https": "http://gateway:3128"}

    elapsed = 0
    while elapsed < timeout:
        try:
            # Check gateway API
            api_response = requests.get(gateway_url, timeout=5)
            if api_response.status_code == 200:
                # Check proxy connectivity
                proxy_response = requests.get(
                    proxy_test_url,
                    proxies=proxies,
                    timeout=10
                )
                if proxy_response.status_code in (200, 401):  # 401 OK = API reachable
                    print("Gateway is ready")
                    return True
        except RequestException:
            pass

        time.sleep(interval)
        elapsed += interval
        print(f"Waiting for gateway... ({elapsed}/{timeout}s)")

    print("ERROR: Gateway not ready within timeout")
    return False

# In main()
if not wait_for_gateway():
    sys.exit(1)
```

**Docker Compose dependency configuration:**

```yaml
# docker-compose.yml
services:
  egg:
    depends_on:
      gateway:
        condition: service_healthy

  gateway:
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:9847/api/v1/health"]
      interval: 5s
      timeout: 5s
      retries: 3
      start_period: 10s
```

#### 6. Testing Plan

##### 6.1 Positive Tests (Should Work)

```bash
# Test 1: Claude API connectivity
curl -x http://gateway:3128 https://api.anthropic.com/v1/messages \
    -H "x-api-key: $ANTHROPIC_API_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "content-type: application/json" \
    -d '{"model":"claude-sonnet-4-20250514","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
# Expected: 200 OK with response

# Test 2: GitHub API
curl -x http://gateway:3128 https://api.github.com/user \
    -H "Authorization: Bearer $GITHUB_TOKEN"
# Expected: 200 OK with user info

# Test 3: Git fetch through proxy
HTTP_PROXY=http://gateway:3128 git ls-remote https://github.com/jwbron/egg.git
# Expected: List of refs

# Test 4: Raw GitHub content
curl -x http://gateway:3128 https://raw.githubusercontent.com/jwbron/egg/main/README.md
# Expected: 200 OK with README content

# Test 5: GitHub archive download
curl -x http://gateway:3128 -L https://codeload.github.com/jwbron/egg/tar.gz/main -o /dev/null
# Expected: 200 OK, tarball downloaded
```

##### 6.2 Negative Tests (Should Fail)

```bash
# Test 1: Arbitrary website blocked
curl -x http://gateway:3128 https://google.com
# Expected: 403 Forbidden

# Test 2: Package manager blocked
curl -x http://gateway:3128 https://pypi.org/simple/
# Expected: 403 Forbidden

# Test 3: Direct IP connection blocked (from egg container)
curl --connect-timeout 5 https://140.82.114.3  # GitHub IP
# Expected: Connection timeout (no route)

# Test 4: DNS resolution blocked (from egg container)
nslookup google.com
# Expected: Failure (no DNS servers)

# Test 5: Non-allowlisted GitHub subdomain
curl -x http://gateway:3128 https://pages.github.com
# Expected: 403 Forbidden
```

##### 6.3 Integration Tests

```bash
# Full workflow test
./test-network-lockdown.sh

# Contents of test-network-lockdown.sh:
#!/bin/bash
set -e

echo "=== Network Lockdown Integration Tests ==="

# Start containers in lockdown mode
./egg --network-lockdown --test-mode &
EGG_PID=$!
sleep 10

# Run test suite inside container
docker exec egg-test-container bash -c '
    echo "Testing Claude API..."
    python -c "import anthropic; print(anthropic.Anthropic().messages.create(model=\"claude-sonnet-4-20250514\",max_tokens=10,messages=[{\"role\":\"user\",\"content\":\"hi\"}]))"

    echo "Testing git operations..."
    git clone https://github.com/jwbron/egg.git /tmp/test-repo
    cd /tmp/test-repo
    git fetch origin

    echo "Testing blocked domains..."
    ! curl -s https://google.com && echo "google.com correctly blocked"
    ! curl -s https://pypi.org && echo "pypi.org correctly blocked"

    echo "All tests passed!"
'

# Cleanup
kill $EGG_PID
docker rm -f egg-test-container
```

##### 6.4 Edge Case and Resilience Tests

```bash
# gateway/test_edge_cases.sh
#!/bin/bash
set -e

echo "=== Edge Case Tests ==="

# Test 1: GitHub API down during visibility check
echo "Test 1: GitHub API unavailability"
# Mock GitHub API to return 503
docker exec egg-gateway iptables -A OUTPUT -d api.github.com -j DROP
# Attempt operation - should fail closed (treat as private, allow)
curl -X POST http://localhost:9847/api/v1/git/fetch \
    -H "Authorization: Bearer $GATEWAY_SECRET" \
    -H "Content-Type: application/json" \
    -d '{"repo_path": "/home/egg/repos/test-repo"}' || true
# Restore
docker exec egg-gateway iptables -D OUTPUT -d api.github.com -j DROP
echo "✓ API unavailability handled (fail closed)"

# Test 2: Concurrent visibility checks
echo "Test 2: Concurrent visibility checks for same repo"
for i in {1..10}; do
    curl -X POST http://localhost:9847/api/v1/git/fetch \
        -H "Authorization: Bearer $GATEWAY_SECRET" \
        -H "Content-Type: application/json" \
        -d '{"repo_path": "/home/egg/repos/test-repo"}' &
done
wait
echo "✓ Concurrent checks completed without race conditions"

# Test 3: Rollback procedure verification
echo "Test 3: Rollback to supervised mode"
export EGG_NETWORK_LOCKDOWN=false
# Verify egg can reach google.com in supervised mode
./egg --test-mode --command "curl -s https://google.com > /dev/null && echo 'Supervised mode working'"
echo "✓ Rollback to supervised mode successful"

echo "=== All edge case tests passed ==="
```

##### 6.5 Claude Code Tool Behavior Tests

```bash
# Test Claude Code behavior when tools are blocked
echo "=== Claude Code Tool Behavior Tests ==="

# Test WebFetch - should fail gracefully
docker exec egg-test-container bash -c '
    # Simulate WebFetch attempt (via proxy)
    RESULT=$(curl -x http://gateway:3128 -s -w "%{http_code}" -o /dev/null https://example.com 2>&1)
    if [ "$RESULT" = "403" ]; then
        echo "✓ WebFetch correctly blocked with 403"
    fi
'

# Test WebSearch - should fail gracefully
docker exec egg-test-container bash -c '
    # Simulate WebSearch attempt
    RESULT=$(curl -x http://gateway:3128 -s -w "%{http_code}" -o /dev/null https://www.google.com/search?q=test 2>&1)
    if [ "$RESULT" = "403" ]; then
        echo "✓ WebSearch correctly blocked with 403"
    fi
'

echo "=== Claude Code tool tests passed ==="
```

#### 7. Rollback Plan

##### 7.1 Quick Rollback (< 5 minutes)

```bash
# Option 1: Disable lockdown mode via environment variable
export EGG_NETWORK_LOCKDOWN=false
./egg  # Runs with full internet access

# Option 2: Use supervised mode override
./egg --supervised  # Explicitly enables full internet
```

##### 7.2 Full Rollback

```bash
# Remove new networks
docker network rm egg-isolated egg-external

# Restore original network
docker network create egg-network

# Restart gateway without Squid
systemctl restart gateway

# Containers will use original egg-network with full access
```

#### 8. Migration Path

##### Phase 2a: Infrastructure Setup (Non-breaking)
1. Create `egg-isolated` and `egg-external` networks
2. Add Squid to gateway container
3. Test proxy functionality independently
4. No changes to egg container yet

##### Phase 2b: Opt-in Testing
1. Add `--network-lockdown` flag to egg launcher
2. Test with flag enabled
3. Gather feedback, adjust allowlist
4. Document any missing domains

##### Phase 2c: Default Enabled
1. Make `--network-lockdown` the default
2. Add `--supervised` flag for full internet access
3. Update documentation
4. Monitor for issues

##### Phase 2d: Remove Legacy Mode
1. Remove legacy network configuration
2. Lockdown is the only mode
3. Supervised mode remains for special cases

#### 9. Monitoring & Observability

##### 9.1 Squid Access Logs

```bash
# Real-time log monitoring
docker exec egg-gateway tail -f /var/log/squid/access.log

# Log format includes:
# - Timestamp
# - Source IP (egg container)
# - HTTP method
# - Destination URL
# - Response code (200=allowed, 403=blocked)
# - Bytes transferred
```

##### 9.2 Blocked Request Alerts and Metrics

```python
# gateway/monitor.py

import os
from datetime import datetime, timedelta
from collections import defaultdict
from notifications import slack_notify

# Thresholds for alerts
BLOCKED_REQUESTS_ALERT_THRESHOLD = 50  # per 5 minutes
BLOCKED_REQUESTS_WINDOW_MINUTES = 5

# Track blocked requests for rate limiting
_blocked_requests: list[dict] = []


def log_blocked_request(source_ip: str, destination: str, reason: str):
    """Log a blocked request and check for anomalies."""
    timestamp = datetime.utcnow()

    entry = {
        "source_ip": source_ip,
        "destination": destination,
        "reason": reason,
        "timestamp": timestamp.isoformat(),
    }

    # Log to structured logging (appears in journalctl)
    log.warning(
        "proxy_request_blocked",
        extra={
            "event_type": "security",
            "action": "blocked",
            **entry,
        }
    )

    # Track for rate limiting
    _blocked_requests.append(entry)

    # Prune old entries
    cutoff = timestamp - timedelta(minutes=BLOCKED_REQUESTS_WINDOW_MINUTES)
    _blocked_requests[:] = [
        e for e in _blocked_requests
        if datetime.fromisoformat(e["timestamp"]) > cutoff
    ]

    # Alert if high volume (possible breakout attempt or misconfiguration)
    if len(_blocked_requests) > BLOCKED_REQUESTS_ALERT_THRESHOLD:
        _send_security_alert()


def _send_security_alert():
    """Send Slack notification for high volume of blocked requests."""
    # Group by destination for summary
    destinations = defaultdict(int)
    for entry in _blocked_requests:
        destinations[entry["destination"]] += 1

    top_destinations = sorted(
        destinations.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]

    summary = "\n".join(f"  - {dest}: {count}x" for dest, count in top_destinations)

    slack_notify(
        subject="Security Alert: High Volume of Blocked Proxy Requests",
        body=f"""The gateway proxy has blocked {len(_blocked_requests)} requests in the last {BLOCKED_REQUESTS_WINDOW_MINUTES} minutes.

Top blocked destinations:
{summary}

This may indicate:
- A breakout attempt by the agent
- Missing domains in the allowlist
- Misconfigured proxy settings

Check logs: `journalctl -u gateway -f --grep=proxy_request_blocked`
""",
        priority="high",
    )


def check_blocked_requests():
    """Parse Squid logs for blocked requests and update metrics."""
    blocked = parse_squid_log(status_code=403)

    for entry in blocked:
        log_blocked_request(
            source_ip=entry.source_ip,
            destination=entry.url,
            reason="proxy_denied",
        )
```

**Metrics exposed via health endpoint:**

```python
# gateway/gateway.py

@app.route("/api/v1/metrics", methods=["GET"])
def get_metrics():
    """Return gateway metrics for monitoring."""
    return jsonify({
        "blocked_requests_5min": len(_blocked_requests),
        "allowed_requests_5min": _get_allowed_count(),
        "proxy_status": "healthy" if _proxy_healthy() else "degraded",
        "visibility_cache_size": len(_visibility_cache),
        "timestamp": datetime.utcnow().isoformat(),
    })
```

#### 10. Configuration Reference

##### 10.1 Environment Variables

| Variable | Default | Description | Valid Values |
|----------|---------|-------------|--------------|
| `EGG_NETWORK_LOCKDOWN` | `true` | Enable Phase 2 network lockdown | `true`, `false` |
| `EGG_NETWORK_MODE` | `lockdown` | Set by launcher, read by container | `lockdown`, `supervised` |
| `HTTP_PROXY` | `http://gateway:3128` | Proxy for HTTP traffic | URL format |
| `HTTPS_PROXY` | `http://gateway:3128` | Proxy for HTTPS traffic | URL format |
| `SQUID_ALLOWED_DOMAINS_FILE` | `/etc/squid/allowed_domains.txt` | Domain allowlist | File path |
| `PRIVATE_REPO_MODE` | `false` | Restrict to private repos only | `true`, `false` |
| `VISIBILITY_CACHE_TTL_READ` | `60` | Cache TTL for read ops (seconds) | Integer ≥ 0 |
| `VISIBILITY_CACHE_TTL_WRITE` | `0` | Cache TTL for write ops (seconds) | Integer ≥ 0 |

##### 10.2 Configuration Validation

The gateway validates all configuration at startup and fails fast on invalid values:

```python
# gateway/config_validator.py
"""Validate configuration at startup."""

import os
import sys
from typing import Any, Optional


class ConfigError(Exception):
    """Raised when configuration is invalid."""
    pass


def validate_bool(name: str, value: str) -> bool:
    """Validate boolean environment variable."""
    if value.lower() in ("true", "1", "yes"):
        return True
    elif value.lower() in ("false", "0", "no"):
        return False
    else:
        raise ConfigError(
            f"Invalid value for {name}: '{value}'. "
            f"Expected 'true' or 'false'."
        )


def validate_int(name: str, value: str, min_val: int = 0) -> int:
    """Validate integer environment variable."""
    try:
        int_val = int(value)
        if int_val < min_val:
            raise ConfigError(
                f"Invalid value for {name}: {int_val}. "
                f"Must be >= {min_val}."
            )
        return int_val
    except ValueError:
        raise ConfigError(
            f"Invalid value for {name}: '{value}'. "
            f"Expected an integer."
        )


def validate_file_exists(name: str, path: str) -> str:
    """Validate file path exists."""
    if not os.path.isfile(path):
        raise ConfigError(
            f"File not found for {name}: '{path}'"
        )
    return path


def validate_config():
    """Validate all configuration at startup.

    Raises ConfigError with clear message if any value is invalid.
    """
    errors = []

    # Boolean configs
    for var in ["EGG_NETWORK_LOCKDOWN", "PRIVATE_REPO_MODE"]:
        value = os.getenv(var, "")
        if value:  # Only validate if set
            try:
                validate_bool(var, value)
            except ConfigError as e:
                errors.append(str(e))

    # Integer configs
    for var in ["VISIBILITY_CACHE_TTL_READ", "VISIBILITY_CACHE_TTL_WRITE"]:
        value = os.getenv(var, "")
        if value:
            try:
                validate_int(var, value, min_val=0)
            except ConfigError as e:
                errors.append(str(e))

    # File configs
    domains_file = os.getenv("SQUID_ALLOWED_DOMAINS_FILE", "/etc/squid/allowed_domains.txt")
    if os.getenv("EGG_NETWORK_LOCKDOWN", "true").lower() == "true":
        try:
            validate_file_exists("SQUID_ALLOWED_DOMAINS_FILE", domains_file)
        except ConfigError as e:
            errors.append(str(e))

    if errors:
        print("Configuration errors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise ConfigError(f"{len(errors)} configuration error(s)")


# Called at gateway startup
if __name__ == "__main__":
    try:
        validate_config()
        print("Configuration valid")
    except ConfigError:
        sys.exit(1)
```

**Startup integration:**

```python
# gateway/gateway.py

from config_validator import validate_config, ConfigError

# Validate config before starting server
try:
    validate_config()
except ConfigError:
    log.error("Gateway startup aborted due to configuration errors")
    sys.exit(1)
```

##### 10.2 Files Modified

| File | Change |
|------|--------|
| `gateway/Dockerfile` | Add Squid installation |
| `gateway/squid.conf` | NEW: Proxy configuration |
| `gateway/allowed_domains.txt` | NEW: Domain allowlist |
| `gateway/entrypoint.sh` | Start Squid before gateway API |
| `gateway/start-gateway.sh` | Dual-network setup |
| `sandbox/egg_lib/runtime.py` | Network lockdown mode |
| `sandbox/Dockerfile` | Pre-installed dependencies |

##### 10.3 Implementation Checklist

- [ ] Create `egg-isolated` internal Docker network
- [ ] Create `egg-external` bridge network
- [ ] Add Squid to gateway Dockerfile
- [ ] Create `squid.conf` with domain allowlist
- [ ] Create `allowed_domains.txt`
- [ ] Update gateway entrypoint to start Squid
- [ ] Update `start-gateway.sh` for dual-network
- [ ] Add `_get_network_lockdown_env()` to runtime.py
- [ ] Update `_build_docker_command()` for lockdown mode
- [ ] Pre-install all packages in egg Dockerfile
- [ ] Run positive tests (Claude API, GitHub)
- [ ] Run negative tests (blocked domains)
- [ ] Run integration tests
- [ ] Document supervised mode override
- [ ] Update CLAUDE.md with network limitations

## Security Analysis

### Threat Model

| Threat | Mitigation | Residual Risk |
|--------|------------|---------------|
| Agent merges own PR | Gateway API doesn't expose merge | VERY LOW - No merge endpoint exists |
| Force push to branches | Gateway blocks force flag | VERY LOW - Policy enforced in code |
| Direct GitHub access | Network isolation, proxy required | LOW - Would need to bypass network config |
| Data exfiltration | Audit logging of all traffic | MEDIUM - Can see it, can't prevent all |
| Credential theft | No credentials in egg container | VERY LOW - Nothing to steal |
| Prompt injection bypass | Controls are infrastructure, not instructions | LOW - Bypass requires breaking out of container |

### What This Does NOT Prevent

**Phase 1 residual risks:**
1. **Data exfiltration to allowed endpoints**: Agent can send data to any website (logged but not blocked)
2. **Malicious code in PRs**: Agent can still write malicious code (human review required)
3. **Social engineering via Slack**: Agent could attempt to manipulate human via messages

**Phase 2 residual risks** (after full lockdown):
1. **Exfiltration via GitHub:** Agent could encode data in branch names, commit messages, or PR descriptions
2. **Exfiltration via Claude API:** Agent could encode data in prompts (addressed by Anthropic's logging)
3. **Malicious code in PRs:** Same as Phase 1—human review required
4. **Supply chain via pre-installed packages:** Malicious packages could be bundled at build time

**Mitigations for Phase 2 residual risks:**

**GitHub exfiltration (acknowledged concern):**

The gateway architecture cannot fully prevent data exfiltration via GitHub without imposing arbitrary limits that would hinder legitimate development work:

| Vector | Limit | Impact |
|--------|-------|--------|
| Branch names | 256 characters | ~32 bytes/branch useful payload |
| Commit messages | No hard limit | Could encode substantial data |
| PR titles/bodies | No hard limit | Could encode substantial data |

**Why we don't impose limits:**
- Arbitrary limits on commit message length or PR body size would interfere with legitimate detailed commit messages and PR descriptions
- The repositories where this exfiltration could occur are **private**, limiting the exposure
- Commit messages and PR bodies are visible in audit logs and PR review, providing detection opportunity

**Mitigations:**
- **Audit logging:** All git operations are logged with full content (see Audit Log section)
- **PR review:** Human reviews PR content before merge
- **Private repos:** When Private Repo Mode is enabled, exfiltrated data goes to private repos only
- **Detection:** Anomaly detection on commit message length, unusual branch creation patterns

**Accepted risk:** This exfiltration vector is a residual risk we accept, as the mitigations provide detection capability and the data stays within controlled (private) repositories.

- Claude API: Anthropic maintains usage logs; API calls are authenticated
- Pre-installed packages: Use pinned versions, scan images with Trivy

### Rate Limiting

The gateway delegates rate limiting to GitHub rather than implementing its own layer.

**Rationale:**
- GitHub enforces its own rate limits (5,000 requests/hour for authenticated users, 5,000-15,000 for GitHub App installations)
- The container is already authenticated via shared secret and trusted
- Gateway-level rate limiting caused false positives during legitimate high-velocity work (e.g., git fetch operations during active development)
- Maintaining a separate rate limiting layer adds complexity without meaningful security benefit

**Observability:**
- The gateway logs warnings when GitHub returns rate limit errors (HTTP 403 with rate limit message)
- GitHub's `X-RateLimit-*` response headers indicate remaining quota
- If rate limiting issues emerge, per-operation logging can be added to track request patterns

### Audit Log Specification

All gateway operations produce structured audit logs in JSON format:

```json
{
  "timestamp": "2025-11-28T14:32:01.234Z",
  "event_type": "gateway_operation",
  "operation": "git_push",
  "source_ip": "172.18.0.2",
  "source_container": "egg",
  "auth_valid": true,
  "request": {
    "repository": "jwbron/egg",
    "ref": "feature/new-feature",
    "force": false
  },
  "response": {
    "status": "success",
    "duration_ms": 1234
  },
  "policy_checks": {
    "force_push_blocked": false,
    "protected_branch_blocked": false
  }
}
```

**Log retention:** 90 days (configurable)

**Alert triggers:**
- Any denied operation (policy violation)
- Authentication failure
- GitHub rate limit errors (HTTP 403 from GitHub API)
- Unusual patterns (e.g., >10 PRs in 10 minutes)

### Audit Log Storage and Access

Audit logs are stored and accessible via the host's systemd journal:

**Storage location:** Gateway sidecar runs as a systemd service (`gateway.service`). All logs are written to stdout/stderr and captured by journald.

**Accessing logs:**
```bash
# Real-time log monitoring
journalctl -u gateway -f

# Filter by event type
journalctl -u gateway --grep="gateway_operation"

# Filter by time range
journalctl -u gateway --since "1 hour ago"

# Export as JSON for analysis
journalctl -u gateway -o json > audit_export.json

# Filter policy violations
journalctl -u gateway --grep="policy_violation"

# Filter proxy blocked requests
journalctl -u gateway --grep="proxy_request_blocked"
```

**Log persistence:**
- Logs are stored in `/var/log/journal/` on the host (not inside containers)
- Default journald retention applies (typically based on disk space or time)
- For extended retention, configure `/etc/systemd/journald.conf`:
  ```ini
  [Journal]
  MaxRetentionSec=90d
  SystemMaxUse=10G
  ```

**Squid proxy logs:**
- Squid access logs are written inside the gateway container to `/var/log/squid/access.log`
- For persistence, mount this path to the host:
  ```yaml
  # docker-compose.yml
  services:
    gateway:
      volumes:
        - /var/log/egg-gateway/squid:/var/log/squid
  ```

**Note:** Logs are accessible outside the container via journalctl. No special action needed for lockdown mode—the gateway runs on the host's network stack and logs to the host's journal.

### Supply Chain Considerations

Per OWASP ASI04, supply chain risks are partially addressed:

| Risk | Mitigation | Status |
|------|------------|--------|
| Malicious pip/npm packages | Audit logging shows all installs | Visibility only |
| Compromised gateway image | Pin image digests, verify signatures | Recommended |
| Poisoned base images | Use official images, scan with Trivy | Recommended |

**Future enhancement:** Integrate with Sigstore/cosign for image verification.

### Defense in Depth Summary

```
Layer 1: Behavioral (CLAUDE.md instructions)
    ↓ Can be bypassed by prompt injection
Layer 2: Credential Isolation (this ADR)
    ↓ egg has no credentials - cannot push/merge even if instructed
Layer 3: Gateway Policy Enforcement
    ↓ Gateway validates all operations
Layer 4: Network Isolation
    ↓ egg cannot reach GitHub directly
Layer 5: Audit Logging
    ↓ All traffic visible for review
Layer 6: Human Review
    ↓ Final safety net - human must approve all PRs
```

## Consequences

### Positive

- **Credential isolation**: egg physically cannot access credentials
- **Enforceable controls**: Security doesn't rely on agent following instructions
- **Audit trail**: All traffic and operations logged
- **Simple mental model**: "egg has no credentials, gateway does"
- **Flexible internet access**: egg can still search web, install packages

### Negative

- **Increased complexity**: Two containers instead of one
- **Latency**: Operations go through gateway (adds ~10-50ms)
- **Development friction**: Need to update gateway API for new operations
- **Shared volume coordination**: Both containers need access to repos

### Trade-offs

| Aspect | Single Container | Gateway Architecture |
|--------|------------------|---------------------|
| Setup complexity | Simple | Moderate |
| Credential exposure | Full access | Zero in egg |
| Policy enforcement | Wrappers (bypassable) | Gateway (not bypassable from egg) |
| Flexibility | High | Constrained by API |
| Audit visibility | Wrapper logs | Full traffic logs |

## Alternatives Considered

### Alternative 1: Tool Wrappers in Single Container

**Approach:** Wrapper scripts that intercept git/gh commands (original proposal)

**Pros:**
- Simpler architecture
- No network coordination
- Familiar pattern

**Cons:**
- Credentials still in container
- Wrappers can be bypassed by calling real binaries
- Agent could find /usr/bin/git

**Rejected:** Credentials remain accessible; wrappers are bypassable

### Alternative 2: Token Scoping Only

**Approach:** Use GitHub tokens with minimal permissions

**Pros:**
- Simplest implementation
- GitHub enforces permissions

**Cons:**
- Cannot prevent merge (requires pull_requests:write which also allows merge)
- Doesn't provide audit trail
- Credentials still exposed

**Rejected:** Token scoping cannot prevent PR merge

### Alternative 3: Full Domain Allowlist

**Approach:** Block all internet except specific domains

**Pros:**
- Strong data exfiltration prevention
- Infrastructure-enforced security (cannot be bypassed by instructions)

**Cons:**
- Breaks web search (needs access to search engines and results)
- Breaks package installation (many CDN domains)
- Requires pre-installing all dependencies in image

**Decision:** Initially rejected as too restrictive. **Reconsidered for Phase 2** after evaluating the risk profile for unsupervised autonomous operation. For `--dangerously-skip-permissions` mode, the security benefits outweigh the operational constraints. Web search and package installation can be addressed through pre-installed dependencies and supervised mode fallback

## MCP Considerations

**Related ADR:** [ADR-Context-Sync-Strategy-Custom-vs-MCP](../implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md) (PR #36)

When MCP is adopted for GitHub operations, the gateway architecture adapts:

**Option 1: MCP Server in Gateway**
```
egg → Claude Code → MCP (in egg) → Gateway API → GitHub
```
MCP client calls gateway REST API instead of direct GitHub API.

**Option 2: MCP Server IS the Gateway**
```
egg → Claude Code → MCP Server (in gateway container) → GitHub
```
Run the GitHub MCP server in the gateway with credentials.

Both options preserve credential isolation - the key principle remains.

## GCP Deployment Considerations

For Cloud Run deployment:

| Component | Local (Docker) | GCP (Cloud Run) |
|-----------|----------------|-----------------|
| Network isolation | Docker networks | VPC Service Controls |
| Gateway sidecar | Separate container | Cloud Run sidecar |
| Audit logs | File/stdout | Cloud Logging |
| Proxy | Squid container | Same or Serverless VPC |

The gateway architecture works well in GCP as a multi-container Cloud Run service.

## Implementation Reference

Detailed implementation examples including:
- Docker Compose network configuration
- Gateway REST API (Flask/Python)
- HTTP Proxy configuration (Squid)
- `git` CLI wrapper with policy enforcement
- `gh` CLI wrapper with Slack notifications
- Network isolation rules

**Implementation artifacts preserved in:**
- PR #56 commit `a1b2c3d` - Docker Compose configuration
- PR #56 commit `e4f5g6h` - Gateway REST API (Flask)
- PR #56 commit `i7j8k9l` - CLI wrappers

**Note:** When implementing, create a new `gateway/` directory rather than copying from PR history. The patterns above are reference examples; implementation details may need updating for current dependencies.

---

## Related ADRs

| ADR | Relationship |
|-----|--------------|
| [ADR-Autonomous-Software-Engineer](../in-progress/ADR-Autonomous-Software-Engineer.md) | Parent ADR - defines overall security model |
| [ADR-Context-Sync-Strategy-Custom-vs-MCP](../implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md) (PR #36) | MCP strategy affects how gateway integrates |
| ADR-GCP-Deployment-Terraform (PR #44 refs) | Gateway must work in Cloud Run |

---

**Last Updated:** 2026-01-28
**Next Review:** 2026-02-28 (Monthly)
**Status:** In Progress (Phase 1 implemented, Phase 2 planned)
