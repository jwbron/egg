# Network Isolation

This document describes the network isolation architecture for the egg platform, covering credential isolation, gateway-mediated operations, proxy-based traffic control, and the full network lockdown design.

## Industry Standards Alignment

This architecture aligns with the **OWASP Top 10 for Agentic Applications (2026)**:

| OWASP Risk | Description | Mitigation |
|------------|-------------|------------|
| **ASI01** - Agentic Excessive Authority | Agents granted overly broad permissions | Credential isolation — egg has no credentials; gateway exposes minimal API |
| **ASI02** - Tool Misuse & Exploitation | Agents misusing available tools | Gateway enforces policies; no merge endpoint; force push blocked |
| **ASI03** - Identity & Privilege Abuse | Credential theft or misuse | Credentials never enter egg container; gateway holds GITHUB_TOKEN |
| **ASI04** - Supply Chain Vulnerabilities | Compromised dependencies/images | Partially addressed — see [Supply Chain Considerations](#supply-chain-considerations) |
| **ASI10** - Rogue Agents | Agent operating outside intended behavior | Infrastructure controls (not instructions) prevent unauthorized operations |

**Reference:** [OWASP Top 10 for Agentic Applications](https://genai.owasp.org/)

## Background

The egg container relies on behavioral instructions (CLAUDE.md) to prevent unauthorized actions like merging PRs or overwriting branches. While the container has network isolation (bridge mode, outbound only), the agent has unrestricted access to raw CLI tools (`git`, `gh`, `curl`, `wget`), full internet access, and the full permissions of any injected `GITHUB_TOKEN`.

Behavioral instructions are not enforceable. A sufficiently sophisticated prompt injection or model misbehavior could bypass soft constraints. **We need defense-in-depth that does not rely solely on the agent following instructions.**

Specific threats:

1. **Unauthorized PR merges** — agent could merge its own PRs without human approval
2. **Destructive git operations** — force push, branch deletion, history rewriting
3. **Credential abuse** — using GITHUB_TOKEN for unintended operations
4. **Data exfiltration** — sending data to arbitrary external endpoints

## Core Principles

1. **Credential isolation** — egg container has NO credentials (no GITHUB_TOKEN, no SSH keys)
2. **Gateway as single choke point** — all authenticated operations go through the gateway
3. **Network proxy for visibility** — all egg traffic proxied through gateway for audit logging
4. **Fail closed** — gateway enforces policies; egg physically cannot bypass

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Kubernetes Cluster (k3s)                              │
│                                                                               │
│  egg-agents namespace              egg-system namespace                       │
│  ┌───────────────────────────────┐ ┌───────────────────────────────┐         │
│  │     agent pod (sandbox)       │ │      gateway pod              │         │
│  │                               │ │                               │         │
│  │  - Claude Code agent          │ │  - GITHUB_TOKEN               │         │
│  │  - No GITHUB_TOKEN            │ │  - git push capability        │         │
│  │  - No git push capability     │ │  - gh CLI                     │         │
│  │  - Egress only to gateway  ───┼─►  - HTTP/HTTPS proxy          │         │
│  │  - git (no auth)              │ │  - Ownership checks           │         │
│  │                               │ │  - Audit logging              │         │
│  │  HTTP_PROXY=gateway:3129     │ │  - Policy enforcement         │         │
│  │                               │ │                               │         │
│  └───────────────────────────────┘ └───────────────────────────────┘         │
│                                                     │                        │
│  NetworkPolicies (Cilium):                          │ All traffic proxied    │
│  - Default deny ingress/egress                      ▼                        │
│  - Allow egress to gateway only         ┌─────────────┐                     │
│                                         │  Internet   │                     │
│                                         │  (filtered) │                     │
│                                         └─────────────┘                     │
│                                                                               │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Component Summary

| Component | Purpose | Implementation |
|-----------|---------|----------------|
| Agent pod | Run Claude Code agent | k8s Job in `egg-agents` namespace, no credentials |
| Gateway pod | Handle authenticated ops + proxy all traffic | k8s Deployment in `egg-system` with credentials |
| HTTP Proxy | Route all agent traffic through gateway | Squid in gateway pod |
| REST API | Controlled interface for git/gh operations | Python service in gateway pod |
| Audit Logger | Log all traffic and operations | Gateway component |
| NetworkPolicies | Enforce network isolation | Cilium CNI in k3s |

### Key Security Properties

1. **egg cannot push to GitHub** — it has no credentials. Network rules block direct GitHub access.
2. **egg cannot merge PRs** — gateway API does not expose a merge operation.
3. **All traffic is auditable** — everything goes through gateway proxy.
4. **Credentials never enter egg** — GITHUB_TOKEN only exists in gateway.
5. **GitHub domains excluded from proxy allowlist** — all GitHub access must go through the gateway sidecar's git/gh wrappers, ensuring policy enforcement (branch ownership, merge blocking) cannot be bypassed by direct API calls through the proxy.

### Gateway REST API

The gateway exposes a controlled API for git/gh operations:

- `POST /api/git/push` — push to remote (blocks force push, protected branches)
- `POST /api/gh/pr/create` — create pull request
- `POST /api/gh/pr/comment` — add comment to PR
- `POST /api/gh/pr/close` — close PR (only egg's own PRs)
- **No merge endpoint** — human must merge via GitHub UI

**Jira read endpoints (`/api/v1/jira/*`) — private-mode only, fail closed in public mode:**

- `POST /api/v1/jira/ticket/get` — read a single ticket (returns ADF-rendered body via `expand=renderedBody,renderedFields`)
- `POST /api/v1/jira/search` — JQL search against `/rest/api/3/search/jql` with a conservative static project-scope extractor (deny-on-ambiguity)
- `POST /api/v1/jira/ticket/comments` — read comments for a ticket
- `POST /api/v1/jira/execute` — GET-only passthrough, regex-allowlisted paths; write verbs (`transitions`, `worklog`, `attachments`, `watchers`, `DELETE`, `PUT`, `PATCH`) are permanently denied

All four routes compose `@require_session_auth` → `@require_private_mode` → project-allowlist check → fields/JQL validation → `JiraClient` call → structured audit log. In **public mode**, `@require_private_mode` short-circuits every call with a 403 and a `private_mode_required` audit entry **before** any upstream request is issued — no Atlassian traffic ever leaves the gateway in public mode. See [Jira wrapper reference](../reference/jira-wrapper.md).

**Confluence read endpoints (`/api/v1/confluence/*`) — private-mode only, fail closed in public mode:**

- `POST /api/v1/confluence/page/get` — read a single page; default `body-format=storage`
- `POST /api/v1/confluence/page/descendants` — list pages under a page (depth-bounded by default)
- `POST /api/v1/confluence/page/footer-comments` — read footer comments; optional v1 nested-reply merge
- `POST /api/v1/confluence/page/inline-comments` — read inline comments with transparent v1 fallback on the known v2 404 bug
- `POST /api/v1/confluence/space/pages` — list pages in a space
- `POST /api/v1/confluence/space/list` — list spaces, response filtered to allowlisted spaces (agents cannot enumerate the full tenant set)
- `POST /api/v1/confluence/search` — CQL search via Atlassian's v1-only `/wiki/rest/api/search` with a conservative static space-scope extractor (deny-on-ambiguity)
- `POST /api/v1/confluence/execute` — GET-only passthrough, regex-allowlisted paths; permanently denied verbs are `restrictions`, `permissions`, `space.admin`, `users`, `attachments`, plus HTTP `DELETE` / `PUT` / `PATCH`

All eight routes compose `@require_session_auth` → `@require_private_mode` → space-allowlist check → fields/CQL validation → `ConfluenceClient` call → response redaction → structured audit log. A route-enumeration regression test asserts every `/api/v1/confluence/*` view function carries `__egg_requires_private_mode__ = True` so newly-added routes cannot accidentally drop the gate. In **public mode**, `@require_private_mode` short-circuits every call with a 403 and a `private_mode_required` audit entry **before** any upstream request is issued — no Atlassian traffic ever leaves the gateway in public mode. See [Confluence wrapper reference](../reference/confluence-wrapper.md).

### CLI Wrappers

The egg container uses `git` and `gh` CLI wrappers that:

- Intercept authenticated operations and route through gateway
- Block dangerous operations (force push, merge, push to protected branches)
- Send Slack notifications for key operations
- Pass through read-only operations unchanged

## Gateway Authentication

The gateway API authenticates requests to prevent abuse from unauthorized containers on the Docker network.

**Container Identity Token:**

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

- Secret generated via `openssl rand -hex 32` in compose startup
- Constant-time comparison to prevent timing attacks
- Requests without valid Authorization header return 401 Unauthorized
- All gateway API endpoints require authentication (no public endpoints)

**Future enhancement — mTLS:** For production deployments, upgrade to mutual TLS for cryptographic identity verification. Recommended for GCP Cloud Run deployment.

## Token Lifecycle Management

The `GITHUB_TOKEN` in the gateway requires careful lifecycle management.

**Token acquisition:**
- GitHub App installation token (preferred) — automatically rotated by GitHub
- Personal Access Token (fallback) — requires manual rotation

**Token properties:**

| Property | Requirement | Rationale |
|----------|-------------|-----------|
| Expiration | ≤1 hour (GitHub App) or manual rotation schedule | Limit window of exposure |
| Scope | Minimum required: `contents:write`, `pull_requests:write` | Principle of least privilege |
| Audit trail | Log all token usage in gateway | Detect misuse patterns |

**Rotation strategy:** Gateway requests installation token from the GitHub App, valid for 1 hour (GitHub enforced). The gateway refreshes the token 10 minutes before expiration. Old tokens naturally expire — no revocation needed.

**Audit logging for token usage:** Every operation using the token logs timestamp (ISO 8601), operation type, target repository, target ref/PR number, success/failure status, and request origin (egg container IP).

## Phase 2: Full Network Lockdown

Phase 1 established credential isolation and gateway-mediated git operations. Phase 2 extends this to **complete network isolation**: egg can only reach the gateway sidecar, and the gateway enforces a strict allowlist of external destinations.

### Motivation

Phase 1 still allows egg to reach arbitrary internet endpoints. Web search could be used for data exfiltration, package installation could pull malicious dependencies, and any HTTP endpoint could receive exfiltrated code or secrets.

For truly unsupervised operation with `--dangerously-skip-permissions`, infrastructure-level guarantees are needed that egg cannot communicate with unauthorized endpoints.

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
│  │  │    Via gateway REST API / HTTP endpoint (not Squid):            │   │  │
│  │  │    ✓ api.anthropic.com        (Claude API)                      │   │  │
│  │  │    ✓ api.github.com           (GitHub API)                      │   │  │
│  │  │    ✓ github.com               (git operations)                  │   │  │
│  │  │    ✓ *.githubusercontent.com  (GitHub raw content)              │   │  │
│  │  │    Via Squid SNI allowlist (allowed_domains.txt):               │   │  │
│  │  │    ✓ registry.npmjs.org       (npm/pnpm, lockfile-pinned)       │   │  │
│  │  │                                                                 │   │  │
│  │  │  BLOCKED (everything else):                                     │   │  │
│  │  │  ✗ pypi.org                   (no Python package installs)      │   │  │
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

### Docker Network Configuration

```yaml
networks:
  egg-isolated:
    internal: true  # No external connectivity
  external:
    # Standard bridge network with internet access

services:
  egg:
    networks:
      - egg-isolated  # ONLY internal network

  gateway:
    networks:
      - egg-isolated  # Can receive from egg
      - external      # Can reach internet
```

Docker's `internal: true` network has no gateway to the outside world. egg physically cannot route packets to the internet — there is no route in its network namespace.

### Network Topology

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
│  │    │ NO EXTERNAL │   Port 9848  │                 │      │   │
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

Subnets are configurable via `EGG_ISOLATED_SUBNET` and `EGG_EXTERNAL_SUBNET` environment variables to avoid conflicts with existing networks.

### Domain Allowlist

The gateway maintains a strict allowlist of permitted domains:

| Domain | Purpose | Required For |
|--------|---------|--------------|
| `api.anthropic.com` | Claude API | Claude Code operation |
| `github.com` | Git operations | Push, fetch, clone |
| `api.github.com` | GitHub REST API | PR creation, issue management |
| `raw.githubusercontent.com` | Raw file content | File downloads |
| `objects.githubusercontent.com` | Release assets, artifacts | Binary downloads |
| `codeload.github.com` | Archive downloads | Zip/tarball downloads |
| `uploads.github.com` | File uploads | Release asset uploads |
| `avatars.githubusercontent.com` | Avatars | GitHub user images |
| `user-images.githubusercontent.com` | User content | GitHub user images |
| `registry.npmjs.org` | Public npm registry | Runtime lockfile-pinned JS dependency installs (npm/pnpm) |

> **Enforcement path.** Only `registry.npmjs.org` is enforced through the Squid SNI allowlist (`gateway/allowed_domains.txt`). The Anthropic and GitHub rows above are **not** in that file — Anthropic traffic flows through the gateway's HTTP endpoint (credential injection) and GitHub traffic through the gateway's git/gh REST API (policy enforcement). They are reachable *via the gateway*, not spliced through Squid. GitHub Packages (`npm.pkg.github.com`) and Yarn Classic (`registry.yarnpkg.com`) are intentionally out of scope — see the `allowed_domains.txt` comment for why.

**Allowlist properties:**
- **Exhaustive** — only listed domains are permitted; all others blocked
- **Enforced at proxy** — Squid validates destination before forwarding
- **SNI-based validation** — for HTTPS, the proxy inspects the Server Name Indication (SNI) in the TLS ClientHello to determine the destination domain. This does **not** require MITM CA certificates or decrypting traffic — the proxy reads the plaintext hostname from the CONNECT request and SNI extension, then either tunnels or rejects.

**Explicitly excluded:** `*.actions.githubusercontent.com`, `ghcr.io`, `*.github.io`, `copilot-*.githubusercontent.com`, **`*.atlassian.net` / `*.atlassian.com` / `api.atlassian.com` / `jira.atlassian.com`**

**Why Atlassian domains are excluded from the Squid allowlist:** all Jira and Confluence traffic **must** flow through the gateway REST endpoints (`/api/v1/jira/*` and `/api/v1/confluence/*`). Adding `*.atlassian.net` to Squid would let a compromised sandbox reach Jira or Confluence directly through the proxy, bypassing the private-mode gate, the project / space allowlist, the verb allowlist, and the audit log. A dedicated regression test (`gateway/tests/test_allowed_domains.py`) asserts that none of the substrings `atlassian.net`, `atlassian.com`, `api.atlassian.com`, or `jira.atlassian.com` appear on any non-comment line of `gateway/allowed_domains.txt`. Because the assertion is a substring match, Confluence-shaped hostnames such as `wiki.atlassian.net` and `confluence.atlassian.com` are caught automatically by the broader `atlassian.net` / `atlassian.com` parametrize entries — no separate Confluence-specific row is needed.

### What Gets Blocked

| Category | Examples | Impact | Mitigation |
|----------|----------|--------|------------|
| Python package managers | pypi.org | Cannot install Python packages | Pre-install required packages in image |
| Web search | google.com, bing.com | Cannot search web | Use GitHub search, local docs |
| Documentation | docs.python.org | Cannot fetch docs | Bundle offline docs in image |
| Arbitrary APIs | any other endpoint | Cannot exfiltrate data | **This is the security goal** |

### Impact on Claude Code Tools

Phase 2 lockdown disables Claude Code's network-dependent tools:

| Tool | Status | Reason |
|------|--------|--------|
| `WebFetch` | Blocked | Cannot reach arbitrary URLs |
| `WebSearch` | Blocked | Cannot reach search engines |
| `Bash` (curl, wget) | Blocked | Cannot reach arbitrary endpoints |
| GitHub MCP tools | Works | Routed through gateway |

This is intentional. Blocked tools return HTTP 403 immediately (no retry loops). Claude Code adapts by falling back to local resources: GitHub search via API, local file search, and pre-loaded documentation. The CLAUDE.md instructions include lockdown mode awareness via the `EGG_NETWORK_MODE=lockdown` environment variable.

### DNS Resolution

egg cannot perform external DNS lookups:

```yaml
# egg container
dns: []  # No DNS servers configured
extra_hosts:
  - "gateway:172.30.0.2"  # Static entry for gateway
```

DNS resolution is handled by the proxy, not the egg container. When egg sends a request through the proxy, it sends the hostname in the CONNECT request. Squid resolves the hostname internally (the gateway has normal DNS on the external network) and validates it against the allowlist **before** resolving DNS.

**Key security property:** The proxy validates hostnames from CONNECT/Host headers, not IP addresses. Even if egg learns an IP address from conversation context, it cannot use it — direct IP connections are blocked by the Squid `direct_ip` ACL, and the internal network has no route to external IPs.

### Proxy Configuration

egg routes all HTTP/HTTPS traffic through the gateway proxy:

```bash
HTTP_PROXY=http://gateway:3128
HTTPS_PROXY=http://gateway:3128
http_proxy=http://gateway:3128
https_proxy=http://gateway:3128
NO_PROXY=localhost,127.0.0.1,gateway,egg-gateway
```

**Proxy behavior:**
1. egg sends CONNECT request to gateway for HTTPS destinations
2. Gateway checks destination against allowlist
3. If allowed: gateway establishes tunnel to destination
4. If blocked: gateway returns 403 Forbidden

**Tool-specific proxy support:**

| Tool/Library | Proxy Support | Notes |
|--------------|---------------|-------|
| curl/wget | `HTTP_PROXY`/`HTTPS_PROXY` | Standard env vars |
| Python requests/httpx | `HTTP_PROXY`/`HTTPS_PROXY` | Standard env vars |
| Anthropic SDK | `HTTP_PROXY`/`HTTPS_PROXY` | Uses httpx, respects proxy env vars |
| git | Routes through gateway API | Git wrapper calls gateway REST API |
| gh CLI | Routes through gateway API | gh wrapper calls gateway REST API |
| npm/pnpm | `HTTP_PROXY`/`HTTPS_PROXY` | Public registry (`registry.npmjs.org`) allowlisted; installs must be lockfile-pinned. Yarn Classic / GitHub Packages out of scope |
| pip | N/A in lockdown mode | PyPI blocked; Python deps pre-installed in image |

### Squid Configuration Approach

The gateway runs Squid with the following key configuration elements:

- **Port:** 3128 with SSL bump in peek/splice mode (reads SNI without MITM decryption)
- **ACL `allowed_domains`:** loaded from an external `allowed_domains.txt` file using `dstdomain`
- **ACL `direct_ip`:** blocks `url_regex ^https?://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+` to prevent IP-based proxy bypass
- **SSL bump:** `peek` at step 1 to read SNI, `splice` for allowed domains, `terminate` for all others
- **HTTP access:** allow `localnet` (172.30.0.0/24) to `allowed_domains`; deny all else
- **Caching:** disabled (not needed for API calls)
- **Self-signed CA:** generated at build time for peek/splice operations only; never trusted externally

### GitHub Packages npm Read-Through (Token-Gated)

The npm registry entry in the allowlist covers `registry.npmjs.org` only. Packages hosted on the GitHub Packages npm registry (`npm.pkg.github.com`) can't ride the same splice path, for two structural reasons ([#3456](https://github.com/jwbron/egg/issues/3456)):

1. **Reads require auth.** GitHub Packages returns 401 on unauthenticated requests, and lockfiles pin absolute tarball URLs on the registry host — so package managers fetch it directly and a registry-endpoint indirection (pointing the scope's registry config at a gateway route) can never intercept a frozen-lockfile install. Verified empirically: with the scope registry reconfigured, pnpm still fetches the lockfile-pinned URL.
2. **Downloads redirect cross-host.** Tarball requests 302-redirect to GitHub's npm blob host (`npmregistryv2prod.blob.core.windows.net`, published under `domains.packages` in `https://api.github.com/meta`) with SAS-signed URLs.

The read-through solves both at the proxy layer, and is **no-op by default** — it activates only when the operator provisions an `npm-packages-token` secret (see `config/README.md`):

- **Registry host is TLS-bumped, not spliced.** Squid decrypts `npm.pkg.github.com` using the same gateway CA that sandboxes already trust, strips any client-supplied `Authorization` header (the sandbox cannot choose its own token), and injects the operator's token. The token never enters the sandbox.
- **GET/HEAD only.** `http_access` denies every other method on the decrypted registry requests, so `npm publish`/unpublish is structurally impossible regardless of the token's scopes. Operators should still provision a minimal read-only packages token.
- **Blob host is spliced.** The SAS-signed download URLs need no credential injection.
- **Real per-package audit.** Because the registry host is bumped, the Squid access log records the full request path — exactly which package and version each sandbox fetched. Spliced hosts only ever produce host-level connection records.
- **Trust distribution.** Clients validating the bumped host need the gateway CA: the gateway serves it at `GET /api/v1/proxy/ca-cert` (public key material, no auth), e.g. `curl -sf "$GATEWAY_URL/api/v1/proxy/ca-cert" -o /tmp/gateway-ca.crt && NODE_EXTRA_CA_CERTS=/tmp/gateway-ca.crt pnpm install --frozen-lockfile`.

Mechanically, `entrypoint.sh` renders `gateway/squid-npm-packages-{ssl,access}.conf.template` into `/etc/squid/conf.d/` when the token secret is present, and truncates the includes to empty files when it isn't. A malformed token (characters outside `[A-Za-z0-9_-]`, or empty after trimming whitespace) leaves the read-through **disabled** with a loud `ERROR` in the gateway log rather than crashlooping the container — the feature is opt-in and no-op-by-default, so a bad secret must not take down git/gh/Anthropic traffic for every pipeline. `gateway/tests/test_npm_packages_readthrough.py` pins the include ordering, the strip-then-inject header rules, the GET/HEAD-only shape, and that neither host ever appears in `allowed_domains.txt`; when a `squid` binary with TLS-bump support is available it also runs `squid -k parse` over the rendered config to catch template syntax regressions (skipped otherwise).

#### Blob host is a hardcoded dependency on `api.github.com/meta`

The blob host (`npmregistryv2prod.blob.core.windows.net`) is **hardcoded** in `squid-npm-packages-ssl.conf.template` and the tests, mirroring the current `domains.packages` value published in `https://api.github.com/meta`. It is not discovered at runtime. If GitHub rotates or regionalizes that host, tarball downloads for the new host silently fall through to `ssl_bump terminate all` — the install fails with a generic TLS/connection error that does **not** name this feature.

**Diagnosing a broken read-through after a GitHub-side change:** if metadata resolves (200 on `npm.pkg.github.com`) but tarball downloads fail, check the Squid access log — a `TCP_DENIED`/terminate against a `*.blob.core.windows.net` host you don't recognize means the published blob host has changed. Re-fetch `https://api.github.com/meta`, compare `domains.packages`, and update the hardcoded host to match: the `acl npm_pkg_blob` and `acl npm_pkg_blob_dst` lines in `squid-npm-packages-ssl.conf.template` (the value is literal there — there is no `BLOB_HOST` placeholder to substitute), and the `BLOB_HOST` constant in `test_npm_packages_readthrough.py`. This is the one part of the mechanism that can break without a code change on our side, so it is called out here rather than left as a silent constant.

### Troubleshooting: Corepack Failures That Look Like Blocked Egress

A failing `pnpm` / `yarn` / `npx` inside the sandbox is **not** necessarily a
proxy or allowlist problem — the most common failure never reaches Squid at
all. When a repo's `build_commands` install a Node toolchain whose `pnpm` is
a **Corepack shim** (`corepack enable` creates shims instead of real
binaries), the shim must fetch the actual package manager from
`registry.npmjs.org` on first use. Old Corepack releases (e.g. 0.25.x, as
bundled with node 20.12.0) cannot perform that fetch through an HTTP proxy:
they build a `ProxyAgent` from their *bundled* undici but pass it as the
`dispatcher` to Node's *builtin* `fetch`, which dies client-side with
`TypeError: this[kClient].connect is not a function`, surfaced as:

```
Internal Error: Error when performing the request to https://registry.npmjs.org/pnpm; for
troubleshooting help, see https://github.com/nodejs/corepack#troubleshooting
```

Because agent pods always run with `HTTPS_PROXY` set, every bare `pnpm`
invocation through such a shim fails this way — in **both** public and
private session modes — and the error text reads exactly like a blocked
domain, which is how it gets misdiagnosed as an egress problem
([#3463](https://github.com/jwbron/egg/issues/3463)). Two aggravators make
the shim always need the network at runtime: `corepack prepare` during the
image build caches under `/root/.cache`, which is not persisted into the
final image, and a repo `packageManager` pin can name a version that was
never prepared at build time anyway.

**Disambiguate in one command** from inside the sandbox:

```bash
curl --proxy "$HTTPS_PROXY" -sS -o /dev/null -w '%{http_code}\n' https://registry.npmjs.org/
```

`200` means the egress path (proxy → SNI allowlist → splice) is healthy and
the failure is client-side toolchain, not network. A Squid denial looks
different: a 403 with Squid's error page, or a TLS termination with the
proxy's self-signed certificate.

**Fixes** (in `build_commands` for the repo that installs the toolchain —
see the Node toolchain note in `config/repositories.yaml.example`):

- Upgrade the shim after unpacking Node: `npm install -g corepack@latest`.
  Current Corepack fetches through `HTTPS_PROXY` correctly and honors
  `packageManager` pins; the runtime fetch is permitted because
  `registry.npmjs.org` is allowlisted ([#3455](https://github.com/jwbron/egg/issues/3455)).
- Or avoid the shim entirely: install the real package manager with
  `npm install -g pnpm@<version>`.

### Breakout Prevention

| Attack Vector | Mitigation |
|---------------|------------|
| Direct IP connection | **Two-layer:** (1) No route — internal network has no external gateway; (2) Squid `direct_ip` ACL blocks IP-based requests through the proxy |
| DNS tunneling | No DNS servers configured in egg container |
| Proxy bypass | No alternate route exists; proxy is the only path out |
| IP-based proxy bypass | Squid `direct_ip` ACL blocks requests by IP address; hostname validation uses `dstdomain` ACL |
| Learned IP from context | Even if egg learns GitHub's IP from conversation, `http://140.82.114.3` is blocked by the `direct_ip` ACL |
| Container escape | Defense in depth; not in scope for network layer |

### Relationship to Private Repo Mode

Phase 2 Network Lockdown and Private Repo Mode are **independent but complementary**:

| Configuration | Network Access | Repository Access |
|---------------|----------------|-------------------|
| Phase 2 only | Anthropic + GitHub only | All repos (public + private) |
| Private Repo Mode only | Full internet | Private repos only |
| Phase 2 + Private Repo Mode | Anthropic + GitHub only | Private repos only |
| Neither (supervised) | Full internet | All repos |

**Recommendation for autonomous operation:** Enable both Phase 2 and Private Repo Mode for unsupervised `--dangerously-skip-permissions` sessions.

### Pre-installed Dependencies

Since egg cannot install packages at runtime in lockdown mode, the container image must include all required dependencies. Python packages, Node.js packages, and system tools are all installed at build time. Adding new packages requires an image rebuild, which provides an opportunity for review and scanning.

### Supervised Mode Fallback

For tasks requiring web access (research, package updates), egg can operate in **supervised mode** by adding the external network to the egg container. In supervised mode, full internet access is available but human must actively monitor the session.

### Migration Path

| Phase | Description |
|-------|-------------|
| **2a: Infrastructure Setup** | Create networks, add Squid to gateway, test proxy independently |
| **2b: Opt-in Testing** | Add `--network-lockdown` flag, test, gather feedback, adjust allowlist |
| **2c: Default Enabled** | Make lockdown the default, add `--supervised` flag for full internet |
| **2d: Remove Legacy** | Remove legacy network configuration; supervised mode remains for special cases |

## Security Analysis

### Threat Model

| Threat | Mitigation | Residual Risk |
|--------|------------|---------------|
| Agent merges own PR | Gateway API does not expose merge | VERY LOW |
| Force push to branches | Gateway blocks force flag | VERY LOW |
| Direct GitHub access | Network isolation, proxy required | LOW |
| Data exfiltration | Audit logging of all traffic | MEDIUM |
| Credential theft | No credentials in egg container | VERY LOW |
| Prompt injection bypass | Controls are infrastructure, not instructions | LOW |

### Residual Risks

**Phase 1 residual risks:**
- Data exfiltration to allowed endpoints (logged but not blocked)
- Malicious code in PRs (human review required)
- Social engineering via Slack

**Phase 2 residual risks:**
- **Exfiltration via GitHub** — agent could encode data in branch names, commit messages, or PR descriptions. This is accepted because arbitrary limits would interfere with legitimate development. Mitigated by audit logging, PR review, and private repo restrictions.
- **Exfiltration via Claude API** — agent could encode data in prompts; addressed by Anthropic's usage logging.
- **Malicious code in PRs** — same as Phase 1; human review required.
- **Supply chain via pre-installed packages** — mitigated by pinned versions and image scanning.
- **Supply chain via runtime npm installs** — `registry.npmjs.org` is reachable so agents can install JS dependencies in repos too large to pre-bake. Mitigated by committed lockfiles (`pnpm install --frozen-lockfile` / `npm ci` resolve only pinned, integrity-checked artifacts). Because Squid splices these connections (no MITM decryption), its access log provides only host-level connection records — the registry host and byte counts, not the package/version fetched — so it is a coarse egress audit, not a per-package manifest. An agent can still fetch an arbitrary published npm package by name; this is accepted because the same code could be vendored into a branch through the git path, and PR review remains the control for what lands.
- **Exfiltration via `npm publish`** — because splice hides the request body, a `publish` POST to `registry.npmjs.org` is an unaudited egress channel just like a package fetch. Risk is low: publishing requires an npm auth token, which the sandbox does not hold by default. If a repo's workflow ever provisions one, treat it as it would any other outbound credential.

### Rate Limiting

The gateway delegates rate limiting to GitHub rather than implementing its own layer. GitHub enforces its own rate limits (5,000 requests/hour for authenticated users, 5,000-15,000 for GitHub App installations). The gateway logs warnings when GitHub returns rate limit errors.

### Audit Log Specification

All gateway operations produce structured audit logs in JSON format including: timestamp (ISO 8601), event type, operation, source IP, source container, auth validity, request details (repository, ref, force flag), response details (status, duration), and policy check results.

**Log retention:** 90 days (configurable).

**Alert triggers:** denied operations, authentication failures, GitHub rate limit errors, unusual patterns (e.g., >10 PRs in 10 minutes).

**Storage:** Gateway runs as a systemd service; logs are captured by journald and accessible via `journalctl -u gateway`. Squid access logs can be persisted by mounting `/var/log/squid` to the host.

### Supply Chain Considerations

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
Layer 2: Credential Isolation
    ↓ egg has no credentials — cannot push/merge even if instructed
Layer 3: Gateway Policy Enforcement
    ↓ Gateway validates all operations
Layer 4: Network Isolation
    ↓ egg cannot reach GitHub directly
Layer 5: Audit Logging
    ↓ All traffic visible for review
Layer 6: Human Review
    ↓ Final safety net — human must approve all PRs
```

### Trade-offs

| Aspect | Single Container | Gateway Architecture |
|--------|------------------|---------------------|
| Setup complexity | Simple | Moderate |
| Credential exposure | Full access | Zero in egg |
| Policy enforcement | Wrappers (bypassable) | Gateway (not bypassable from egg) |
| Flexibility | High | Constrained by API |
| Audit visibility | Wrapper logs | Full traffic logs |

## Alternatives Considered

**Tool wrappers in single container:** Wrapper scripts intercepting git/gh commands. Rejected because credentials remain accessible and wrappers are bypassable by calling real binaries.

**Token scoping only:** Using GitHub tokens with minimal permissions. Rejected because token scoping cannot prevent PR merge (`pull_requests:write` grants both create and merge).

**Full domain allowlist (Phase 2):** Initially rejected as too restrictive. Reconsidered after evaluating the risk profile for unsupervised autonomous operation — for `--dangerously-skip-permissions` mode, the security benefits outweigh the operational constraints.

## MCP Considerations

When MCP is adopted for GitHub operations, two options preserve credential isolation:

- **MCP Server in Gateway:** egg's MCP client calls the gateway REST API instead of direct GitHub API.
- **MCP Server IS the Gateway:** The GitHub MCP server runs in the gateway container with credentials.

Both options preserve the key principle — credentials never enter the egg container.

## Kubernetes Network Isolation

> **As of [#1553](https://github.com/jwbron/egg/issues/1553)**, the container runtime has migrated from Docker to Kubernetes (k3s). The network isolation model is preserved using Cilium NetworkPolicies instead of Docker networks. (Cilium replaced Calico in [#2703](https://github.com/jwbron/egg/issues/2703); the policies themselves use only standard `networking.k8s.io/v1` features and are CNI-agnostic.)

### Architecture

The Docker dual-network model (`egg-isolated` + `egg-external`) is replaced by Kubernetes namespace separation with NetworkPolicies (enforced by Cilium):

| Docker Concept | Kubernetes Equivalent |
|---------------|----------------------|
| `egg-isolated` network (`internal: true`) | `egg-agents` namespace with default-deny egress NetworkPolicy |
| `egg-external` network (bridge) | `egg-system` namespace (gateway has internet access) |
| Fixed IPs (172.32.0.x) | Kubernetes Service DNS (`gateway.egg-system.svc.cluster.local`) |
| Container on isolated-only network | Pod in `egg-agents` with egress restricted to gateway Service |
| Gateway dual-homed (both networks) | Gateway Deployment in `egg-system` with Service exposed to both namespaces |

### NetworkPolicy Rules

Five policies in `k8s/base/network-policies.yaml` enforce isolation:

```yaml
# 1. Default deny all ingress in egg-agents
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: egg-agents
spec:
  podSelector: {}
  policyTypes: ["Ingress"]

# 2. Default deny all egress in egg-agents
kind: NetworkPolicy
metadata:
  name: default-deny-egress
  namespace: egg-agents
spec:
  podSelector: {}
  policyTypes: ["Egress"]

# 3. Allow agent pods to reach gateway (API + proxy)
kind: NetworkPolicy
metadata:
  name: allow-agent-to-gateway
  namespace: egg-agents
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/component: agent
  policyTypes: ["Egress"]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: egg-system
          podSelector:
            matchLabels:
              app.kubernetes.io/component: gateway
      ports:
        - port: 9848   # Gateway API
        - port: 3129   # Squid proxy

# 4. Allow orchestrator to reach agent pods (health checks, logs)
kind: NetworkPolicy
metadata:
  name: allow-orchestrator-to-agent
  namespace: egg-agents
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/component: agent
  policyTypes: ["Ingress"]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: egg-system
          podSelector:
            matchLabels:
              app.kubernetes.io/component: orchestrator

# 5. Allow agent pods to reach kube-dns for DNS resolution
kind: NetworkPolicy
metadata:
  name: allow-agent-dns
  namespace: egg-agents
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/component: agent
  policyTypes: ["Egress"]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

### CNI Requirement

**A NetworkPolicy-capable CNI is required.** k3s ships with Flannel as the default CNI; Flannel does **not** support NetworkPolicies. egg uses **Cilium** — k3s is installed with Flannel and its built-in policy controller both disabled:

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--flannel-backend=none --disable-network-policy" sh -
scripts/install-cilium.sh   # downloads the cilium-cli and runs `cilium install`
```

This is handled automatically by `make k3s-setup`. The CNI choice is local to the install path — the policies in `k8s/base/network-policies.yaml` are standard `networking.k8s.io/v1` and work on any NetworkPolicy-capable CNI (Calico, Cilium, Antrea, Weave, kube-router).

### Security Properties Preserved

All security properties from the Docker model are preserved:

| Property | Docker Implementation | Kubernetes Implementation |
|----------|----------------------|--------------------------|
| Agents cannot reach internet | `internal: true` network (no gateway route) | Default-deny egress NetworkPolicy |
| Agents can only reach gateway | Single network with gateway | Egress allowed only to gateway Service |
| Agents cannot reach each other | Separate containers on isolated network | Default-deny ingress NetworkPolicy |
| All traffic auditable | Gateway proxy is only egress path | Same — Squid proxy in gateway pod |
| Credentials never enter agents | No `GITHUB_TOKEN` in container env | No `GITHUB_TOKEN` in pod env |

### DNS Resolution in Kubernetes

Agent pods use Kubernetes cluster DNS to resolve the gateway Service name. Unlike the Docker model (which used static `/etc/hosts` entries), agents resolve `gateway.egg-system.svc.cluster.local` via CoreDNS. The NetworkPolicy restricts which services the DNS-resolved addresses can actually reach — even if an agent resolves an external IP, the default-deny egress policy blocks the connection.

For full migration details, see [Kubernetes Migration](kubernetes-migration.md).

## Cloud Deployment Considerations

| Component | Local (k3s) | GCP (GKE) | GCP (Cloud Run) |
|-----------|-------------|-----------|-----------------|
| Network isolation | Cilium NetworkPolicies | GKE NetworkPolicies (Dataplane V2) | VPC Service Controls |
| Gateway sidecar | k8s Deployment + Service | Same | Cloud Run sidecar |
| Audit logs | File/stdout | Cloud Logging | Cloud Logging |
| Proxy | Squid in gateway pod | Same or Serverless VPC | Same or Serverless VPC |
| Storage | hostPath volumes | PVCs with ReadWriteMany | Managed storage |

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `EGG_NETWORK_LOCKDOWN` | `true` | Enable Phase 2 network lockdown |
| `EGG_NETWORK_MODE` | `lockdown` | Set by launcher, read by container (`lockdown` or `supervised`) |
| `HTTP_PROXY` | `http://gateway:3128` | Proxy for HTTP traffic |
| `HTTPS_PROXY` | `http://gateway:3128` | Proxy for HTTPS traffic |
| `SQUID_ALLOWED_DOMAINS_FILE` | `/etc/squid/allowed_domains.txt` | Domain allowlist file path |
| `PRIVATE_REPO_MODE` | `false` | Restrict to private repos only |
| `VISIBILITY_CACHE_TTL_READ` | `60` | Cache TTL for read ops (seconds) |
| `VISIBILITY_CACHE_TTL_WRITE` | `0` | Cache TTL for write ops (seconds) |

## Related Documentation

- [Git Isolation](git-isolation.md) — git operation policies and branch protection
- [Credential Injection](credential-injection.md) — how credentials are managed and injected
- [Architecture Overview](README.md) — overall system architecture and security model
