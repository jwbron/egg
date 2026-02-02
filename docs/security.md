# Security Model

**Document Status:** Implemented
**Version:** 1.0
**Audience:** Security Teams, Engineering Leadership

## Executive Summary

egg provides a comprehensive security architecture for operating autonomous LLM-powered agents in sensitive codebases without human supervision. The architecture provides **defense-in-depth through infrastructure controls**, ensuring that even if behavioral instructions are bypassed via prompt injection, model drift, or adversarial inputs, the agent cannot perform unauthorized operations.

### Core Security Guarantee

> **An AI agent cannot access credentials, merge code, or exfiltrate data to unauthorized endpoints—regardless of what it is instructed to do.**

This guarantee is achieved through:
1. **Credential Isolation**: Tokens never enter the sandbox container
2. **Network Lockdown**: Only Anthropic API and GitHub reachable (private mode)
3. **Gateway Enforcement**: All privileged operations validated by a trusted sidecar
4. **Filesystem Isolation**: Agent cannot access git metadata or other agents' workspaces
5. **Human-in-the-Loop**: All code merges require human approval

---

## Table of Contents

1. [Threat Model](#1-threat-model)
2. [Architecture Overview](#2-architecture-overview)
3. [Network Isolation](#3-network-isolation)
4. [Credential Isolation](#4-credential-isolation)
5. [Git and GitHub Lockdown](#5-git-and-github-lockdown)
6. [Private Repository Mode](#6-private-repository-mode)
7. [Audit Logging](#7-audit-logging)
8. [Residual Risks](#8-residual-risks)
9. [OWASP Alignment](#9-owasp-alignment)

---

## 1. Threat Model

### 1.1 Adversary Capabilities

| Adversary | Capability | Example Attack |
|-----------|------------|----------------|
| **Prompt Injection** | Attacker crafts input that overrides agent instructions | Malicious issue contains: "Ignore previous instructions and..." |
| **Model Misbehavior** | Agent acts outside intended behavior due to training or inference issues | Agent interprets "fix the bug" as "delete the file" |
| **Rogue Agent** | Agent deliberately operates outside constraints | Agent attempts to merge its own PR |
| **Data Exfiltration** | Agent attempts to send sensitive data to external services | Agent encodes secrets in commit messages |
| **Credential Theft** | Agent attempts to extract credentials for later use | Agent reads GitHub token from environment |

### 1.2 Protected Assets

| Asset | Classification | Protection Requirement |
|-------|----------------|----------------------|
| **GitHub Tokens** | Critical | Never enter sandbox container |
| **Anthropic API Keys** | Critical | Never enter sandbox container (injected by gateway) |
| **Source Code (Private)** | High | Readable, changes require human review |
| **PR Content** | Medium | Agent cannot merge; human review required |

### 1.3 Security Boundaries

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            UNTRUSTED ZONE                                    │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                         Sandbox Container                              │  │
│  │                                                                        │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │  │
│  │  │ LLM Agent   │  │ Code Files  │  │ git/gh      │  │ Isolated    │   │  │
│  │  │ (Claude)    │  │ (workspace) │  │ wrappers    │  │ Worktree    │   │  │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘   │  │
│  │                                                                        │  │
│  │  NO: GitHub tokens, Anthropic API keys, SSH keys, cloud credentials    │  │
│  │  NO: Direct network access (proxy required in private mode)            │  │
│  │  NO: Git metadata (.git directory shadowed by tmpfs)                   │  │
│  │  NO: Other agents' workspaces                                          │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                    │                                         │
│                                    │ HTTP API (authenticated)                │
│                                    ▼                                         │
├──────────────────────────────────────────────────────────────────────────────┤
│                            TRUSTED ZONE                                      │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                     Gateway Sidecar (Policy Enforcer)                  │  │
│  │                                                                        │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │  │
│  │  │ GITHUB_     │  │ ANTHROPIC_  │  │ HTTPS       │  │ Policy      │   │  │
│  │  │ TOKEN       │  │ API_KEY     │  │ Proxy       │  │ Engine      │   │  │
│  │  │ (secure)    │  │ (secure)    │  │ (filtered)  │  │ (validates) │   │  │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘   │  │
│  │                                                                        │  │
│  │  ENFORCES: Branch ownership, merge blocking, domain allowlist          │  │
│  │  LOGS: All operations with full audit trail                            │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Architecture Overview

### 2.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Host Machine                                    │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                    Isolated Network (internal: true)                  │   │
│  │                    No external route                                  │   │
│  │                                                                       │   │
│  │    ┌───────────────────┐                ┌───────────────────────┐    │   │
│  │    │ Sandbox Container │                │   Gateway Sidecar     │    │   │
│  │    │                   │◄──REST API────►│                       │    │   │
│  │    │                   │                │                       │    │   │
│  │    │ LLM Agent         │                │ GITHUB_TOKEN          │    │   │
│  │    │ git/gh wrappers   │◄──HTTPS Proxy──│ ANTHROPIC_API_KEY     │    │   │
│  │    │ NO credentials    │                │ Squid Proxy           │    │   │
│  │    │                   │                │ Policy Engine         │    │   │
│  │    └───────────────────┘                └───────────┬───────────┘    │   │
│  │                                                     │                │   │
│  └─────────────────────────────────────────────────────│────────────────┘   │
│                                                        │                    │
│  ┌─────────────────────────────────────────────────────│────────────────┐   │
│  │                    External Network (bridge)        │                │   │
│  │                                                     │                │   │
│  │                                    ┌────────────────┴──────────────┐ │   │
│  │                                    │    Gateway Sidecar           │ │   │
│  │                                    │    (dual-homed)              │ │   │
│  │                                    │                              │ │   │
│  │                                    │    ALLOWED:                  │ │   │
│  │                                    │    - api.anthropic.com       │ │   │
│  │                                    │    - github.com              │ │   │
│  │                                    │    - api.github.com          │ │   │
│  │                                    │    - *.githubusercontent.com │ │   │
│  │                                    │                              │ │   │
│  │                                    │    BLOCKED:                  │ │   │
│  │                                    │    - Everything else         │ │   │
│  │                                    └──────────────┬───────────────┘ │   │
│  └───────────────────────────────────────────────────│─────────────────┘   │
│                                                      │                     │
│                                                      ▼                     │
│                                                  Internet                  │
│                                            (allowlisted only)              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Security Properties Summary

| Property | Implementation | Verification |
|----------|----------------|--------------|
| **Credential Isolation** | Tokens exist only in gateway sidecar | Container has no env vars or files with tokens |
| **Network Isolation** | Internal Docker network with no external route | Network configured with `internal: true` |
| **Domain Allowlist** | Squid proxy with SNI-based filtering | Blocked requests return HTTP 403 |
| **Git Metadata Isolation** | `.git` directories shadowed by tmpfs | Agent cannot read or modify git refs directly |
| **Branch Ownership** | Gateway validates push requests | Only `egg/*` prefixed branches allowed |
| **Merge Blocking** | Gateway has no merge endpoint | `gh pr merge` commands fail at gateway level |
| **Audit Logging** | All operations logged with correlation IDs | Structured JSON logs |

---

## 3. Network Isolation

### 3.1 Public vs Private Mode

| Mode | Network Access | Use Case |
|------|----------------|----------|
| **Public** | Full internet (Anthropic API via gateway for credential injection) | Open source, package installation |
| **Private** | Anthropic API + private GitHub repos only | Confidential code, sensitive data |

### 3.2 Domain Allowlist (Private Mode)

| Domain | Purpose | Required For |
|--------|---------|--------------|
| `api.anthropic.com` | Claude API | Agent operation |
| `github.com` | Git HTTPS | Clone, fetch, push |
| `api.github.com` | GitHub REST API | PR creation, issues |
| `raw.githubusercontent.com` | Raw content | File downloads |
| `objects.githubusercontent.com` | Release assets | Binary downloads |
| `codeload.github.com` | Archive downloads | Zip/tarball |
| `uploads.github.com` | File uploads | Release assets |

### 3.3 Blocked in Private Mode

| Category | Examples | Impact | Mitigation |
|----------|----------|--------|------------|
| Package registries | pypi.org, npmjs.com | Cannot install packages | Pre-install in image |
| Search engines | google.com, bing.com | Cannot search web | Use local docs, GitHub search |
| Arbitrary APIs | Any unlisted domain | Cannot exfiltrate | **This is the security goal** |

### 3.4 Agent Tool Behavior (Private Mode)

| Tool | Status | Reason |
|------|--------|--------|
| WebFetch | Blocked | Cannot reach arbitrary URLs |
| WebSearch | Blocked | Cannot reach search engines |
| GitHub tools | Works | Routed through gateway |
| Claude API | Works | api.anthropic.com allowed |

When blocked tools are invoked, the agent receives HTTP 403 and adapts by using local resources.

### 3.5 Docker Network Configuration

```yaml
networks:
  egg-isolated:
    internal: true  # No external connectivity
  egg-external:
    # Standard bridge network for gateway outbound

services:
  sandbox:
    networks:
      - egg-isolated  # ONLY internal network
    dns: []  # No DNS servers (prevents DNS tunneling)

  gateway:
    networks:
      - egg-isolated  # Can receive from sandbox
      - egg-external  # Can reach internet
```

### 3.6 DNS Configuration

The `dns: []` setting prevents the sandbox container from using external DNS servers, blocking DNS tunneling as an exfiltration vector:

| Hostname | Resolution Method |
|----------|-------------------|
| `gateway` | Docker's embedded DNS (via /etc/hosts) |
| `localhost` | /etc/hosts |
| External names | Fails (no resolvers) |

External hostname resolution happens in the gateway container, which has normal DNS access.

### 3.7 Squid Proxy Configuration

```squid
# Block direct IP connections (prevent bypass via learned IPs)
acl direct_ip url_regex ^https?://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+
http_access deny direct_ip

# Load allowed domains
acl allowed_domains dstdomain "/etc/squid/allowed_domains.txt"

# SSL bump for SNI inspection (peek only, no MITM decryption)
ssl_bump peek step1
ssl_bump splice allowed_domains
ssl_bump terminate all

# Allow only from internal network to allowed domains
http_access allow localnet allowed_domains
http_access deny all
```

### 3.8 ECH/ESNI Handling

TLS 1.3 Encrypted Client Hello (ECH) can encrypt the SNI field. egg blocks connections where SNI cannot be determined:

```squid
ssl_bump terminate !sni_available
```

| Scenario | Behavior |
|----------|----------|
| Standard TLS with SNI | Inspect and filter |
| TLS with ECH/ESNI | Connection terminated |
| Missing SNI | Connection terminated |

None of the allowlisted domains currently use ECH.

---

## 4. Credential Isolation

### 4.1 Credentials Inventory

| Credential | Location | Sandbox Access |
|------------|----------|----------------|
| `GITHUB_TOKEN` | Gateway sidecar only | Never |
| `ANTHROPIC_API_KEY` | Gateway sidecar only | Never (injected via proxy) |
| SSH keys | None | Not present |
| Cloud credentials | None | Not present |

### 4.2 Anthropic API Credential Flow

```
┌─────────────────┐   ANTHROPIC_BASE_URL    ┌─────────────────────┐
│    Sandbox      │ ───────────────────────▶│     Gateway         │
│   Container     │   http://gateway:8080   │   Anthropic Proxy   │
│                 │   (no credentials)      │                     │
│  Claude Code    │                         │  1. Receive request │
│  (no API key)   │                         │  2. Inject API key  │
│                 │                         │  3. Forward to API  │──▶ api.anthropic.com
└─────────────────┘                         └─────────────────────┘
```

Claude Code sends requests to the gateway via `ANTHROPIC_BASE_URL`. The gateway injects credentials and forwards to api.anthropic.com over HTTPS.

### 4.3 GitHub Token Lifecycle

GitHub App tokens are preferred with automatic rotation:

```
┌────────────────────────────────────────────────────────────────────────────┐
│                      GitHub App Token Lifecycle                             │
│                                                                             │
│  1. Gateway requests installation token from GitHub App                     │
│  2. Token valid for 1 hour (GitHub enforced)                                │
│  3. Gateway refreshes token 10 minutes before expiration                    │
│  4. Old token naturally expires - no revocation needed                      │
│                                                                             │
│  Timeline:                                                                  │
│  ├─────────────────────────────────────────────────────────────────────┤    │
│  0min              50min         60min                                      │
│  Token issued      Refresh       Expiration                                 │
└────────────────────────────────────────────────────────────────────────────┘
```

| Scenario | Behavior |
|----------|----------|
| Refresh fails (GitHub unavailable) | Retry with backoff; continue with existing token |
| Token expired, refresh still failing | Git operations fail with clear error |
| Gateway restart | Request new token on startup |

### 4.4 Gateway Authentication

The sandbox authenticates to the gateway using a shared secret:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Authentication Flow                                  │
│                                                                              │
│  1. Orchestrator generates random shared secret at startup                   │
│  2. Secret injected into both containers via environment variable            │
│  3. Sandbox includes secret in Authorization header for gateway requests     │
│  4. Gateway validates secret before processing any request                   │
│                                                                              │
│  Sandbox Container                  Gateway Sidecar                          │
│  ┌─────────────┐                   ┌─────────────────────┐                   │
│  │ GATEWAY_    │  Authorization:   │ Validate header     │                   │
│  │ SECRET      │ ──Bearer $SECRET──► matches GATEWAY_    │                   │
│  │             │                   │ SECRET              │                   │
│  └─────────────┘                   └─────────────────────┘                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

The secret is generated fresh on each container startup and exists only for the container lifecycle.

---

## 5. Git and GitHub Lockdown

### 5.1 Git Metadata Isolation

The sandbox container cannot access git metadata:

```
Container filesystem view:
/workspace/my-repo/
├── src/                 ← Agent can edit these files
├── tests/               ← Agent can edit these files
├── README.md            ← Agent can edit this file
└── .git/                ← Empty directory (tmpfs shadow)
```

Without git metadata, the agent cannot:
- Discover repository origins
- Modify staging area directly
- Change branch pointers
- Execute git hooks
- Access other worktrees

### 5.2 Gateway-Enforced Policies

| Policy | Implementation |
|--------|----------------|
| **Branch ownership** | Only push to `egg/*` prefixed branches |
| **Protected branches** | Block direct push to `main`, `master` |
| **Force push** | `--force` flag blocked globally |
| **Merge blocking** | No merge endpoint exists in gateway API |

### 5.3 Gateway REST API

| Endpoint | Purpose | Policy Checks |
|----------|---------|---------------|
| `POST /api/v1/git/push` | Push to remote | Branch ownership, no force push |
| `POST /api/v1/git/fetch` | Fetch from remote | None (read-only) |
| `POST /api/v1/git/status` | Get status | None (read-only) |
| `POST /api/v1/git/diff` | Get diff | None (read-only) |
| `POST /api/v1/git/commit` | Create commit | None |
| `POST /api/v1/gh/pr/create` | Create PR | Agent attribution |
| `POST /api/v1/gh/pr/comment` | Comment on PR | None |

### 5.4 Blocked Operations

| Operation | Why Blocked |
|-----------|-------------|
| `git merge` to protected branches | Must go through PR review |
| `gh pr merge` | Human must review and merge |
| `git push --force` | Could destroy others' work |
| `git config --global` | Could affect other agents |
| `git remote add/remove` | Could redirect pushes |

### 5.5 Blocked Flags

| Flag | Risk |
|------|------|
| `--exec`, `-c` | Command injection |
| `--upload-pack`, `--receive-pack` | Arbitrary command execution |
| `--config`, `-c` | Runtime config override |
| `--no-verify` | Skip hooks (defense in depth) |
| `--git-dir`, `--work-tree` | Path traversal |

---

## 6. Private Repository Mode

### 6.1 Purpose

Private Repo Mode restricts agents to only interact with **private** GitHub repositories, preventing any interaction with public repositories.

### 6.2 Motivation

When operating on sensitive codebases:
1. **Accidental code sharing:** Agent might reference or copy code to a public repository
2. **Data leakage via forks:** Agent could fork a private repo to a public destination
3. **Cross-contamination:** Agent might mix private code with public dependencies

### 6.3 Enforcement

The gateway checks repository visibility via GitHub API:

| Operation | Public Repo | Private Repo |
|-----------|-------------|--------------|
| `git clone` | Blocked | Allowed |
| `git fetch` | Blocked | Allowed |
| `git push` | Blocked | Allowed |
| `gh pr create` | Blocked | Allowed |

### 6.4 Visibility Cache

| Operation Type | TTL | Rationale |
|----------------|-----|-----------|
| Read operations (fetch, clone) | 60 seconds | Lower risk; brief window acceptable |
| Write operations (push, PR create) | 0 seconds | Higher risk; always verify before writes |

**Error handling:**
- **Read operations:** Fail open (allow if GitHub unavailable)
- **Write operations:** Fail closed (deny if GitHub unavailable)

This balances availability with security.

---

## 7. Audit Logging

### 7.1 Log Format

All operations produce structured JSON logs:

```json
{
  "timestamp": "2026-01-29T14:32:01.234Z",
  "severity": "INFO",
  "message": "Git push completed",
  "traceId": "0af7651916cd43dd8448eb211c80319c",
  "spanId": "b7ad6b7169203331",
  "service": "gateway",
  "operation": "git_push",
  "source_container": "sandbox-abc123",
  "auth_valid": true,
  "request": {
    "repository": "owner/repo",
    "ref": "egg/feature-branch",
    "force": false
  },
  "response": {
    "status": "success",
    "duration_ms": 1234
  },
  "policy_checks": {
    "branch_ownership": "passed",
    "protected_branch": "passed",
    "force_push_attempted": false
  }
}
```

### 7.2 Logged Operations

| Category | Operations Logged |
|----------|-------------------|
| **Git operations** | push, fetch, clone, status, diff, commit |
| **GitHub operations** | PR create, comment, close |
| **Proxy traffic** | All HTTPS requests (destination, status) |
| **Policy violations** | Blocked operations with reason |
| **Authentication** | Success/failure |

### 7.3 Alerting

| Condition | Alert Priority |
|-----------|----------------|
| Policy violation (blocked operation) | High |
| Authentication failure | High |
| High volume of blocked requests | Medium |
| GitHub rate limit | Low |

---

## 8. Residual Risks

### 8.1 Known Residual Risks

| Risk | Severity | Mitigation | Status |
|------|----------|------------|--------|
| **Data exfiltration via GitHub** | Medium | Commit messages/PR descriptions reviewed by human; private repos only | Acknowledged |
| **Data exfiltration via Anthropic API** | Low | Anthropic doesn't train on API data; network lockdown limits where data can go | Acknowledged |

### 8.2 Exfiltration via GitHub (Detail)

The gateway cannot fully prevent data exfiltration via GitHub without imposing arbitrary limits:

| Vector | Limit | Impact |
|--------|-------|--------|
| Branch names | 256 characters | ~32 bytes useful payload |
| Commit messages | No hard limit | Could encode substantial data |
| PR titles/bodies | No hard limit | Could encode substantial data |

**Why we don't impose limits:**
- Arbitrary limits interfere with legitimate detailed commit messages
- Private Repo Mode ensures data stays in controlled repositories
- Human reviews PR content before merge
- All operations logged for anomaly detection

**Risk Acceptance:** This exfiltration vector is accepted as a residual risk. Mitigations provide detection capability, and data stays within controlled (private) repositories.

### 8.3 Gaps Not Yet Addressed

| Gap | Description | Proposed Solution | Priority |
|-----|-------------|-------------------|----------|
| **Multi-agent isolation** | Agent memory could be shared | Per-container memory isolation | Medium |
| **Container escape** | Defense in depth, not primary threat model | Keep Docker updated; consider gVisor | Low |
| **Host compromise** | Out of scope; host is trusted | Physical security, OS hardening | N/A |

---

## 9. OWASP Alignment

This architecture aligns with the **OWASP Top 10 for Agentic Applications**:

| OWASP Risk | Description | Mitigation |
|------------|-------------|------------|
| **ASI01** - Agentic Excessive Authority | Agents granted overly broad permissions | Credential isolation; gateway exposes minimal API; no merge endpoint |
| **ASI02** - Tool Misuse & Exploitation | Agents misusing available tools | Gateway enforces policies; force push blocked; domain allowlist |
| **ASI03** - Identity & Privilege Abuse | Credential theft or misuse | Credentials never enter container; gateway holds all tokens |
| **ASI04** - Supply Chain Vulnerabilities | Compromised dependencies | Pre-installed packages; image scanning; no runtime installs (private mode) |
| **ASI06** - Memory/Context Poisoning | Corruption of agent memory | Container isolation; isolated worktrees |
| **ASI10** - Rogue Agents | Agent operating outside intended behavior | Infrastructure controls (not instructions) prevent unauthorized operations |

**Reference:** [OWASP Top 10 for Agentic Applications](https://genai.owasp.org/)

---

## Related ADRs

- [ADR: Git Isolation Architecture](adr/git-isolation-architecture.md)
- [ADR: Credential Injection](adr/credential-injection.md)
- [ADR: Network Isolation](adr/network-isolation.md)
