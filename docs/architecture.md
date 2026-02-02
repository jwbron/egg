# Architecture

This document describes the architecture of the egg sandbox environment.

## Overview

egg uses a two-container architecture:

1. **Gateway Container** - Runs the proxy, REST API, and policy enforcement
2. **Sandbox Container** - Where the LLM (Claude) runs, isolated from credentials

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              egg                                            │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        Gateway Sidecar                               │   │
│  │                                                                      │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────┐  │   │
│  │  │ REST API    │  │ Policy      │  │ Session     │  │ Audit      │  │   │
│  │  │ Server      │  │ Engine      │  │ Manager     │  │ Logger     │  │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └────────────┘  │   │
│  │                                                                      │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────┐  │   │
│  │  │ Git Client  │  │ GitHub      │  │ Worktree    │  │ Rate       │  │   │
│  │  │             │  │ Client      │  │ Manager     │  │ Limiter    │  │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └────────────┘  │   │
│  │                                                                      │   │
│  │  ┌─────────────────────────────────────────────────────────────┐    │   │
│  │  │ HTTP Proxy (Squid) - Domain Allowlist                       │    │   │
│  │  └─────────────────────────────────────────────────────────────┘    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      Sandbox Container                               │   │
│  │                                                                      │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────────┐  │   │
│  │  │ git wrapper │  │ gh wrapper  │  │ LLM CLI (Claude)            │  │   │
│  │  │ → gateway   │  │ → gateway   │  │                             │  │   │
│  │  └─────────────┘  └─────────────┘  └─────────────────────────────┘  │   │
│  │                                                                      │   │
│  │  NO: GitHub tokens, SSH keys, direct network access                  │   │
│  │  YES: Workspace files, LLM API access (via proxy)                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Security Model

**Core principle:** Security through infrastructure, not instructions.

The sandbox container has no direct access to:
- GitHub tokens or credentials
- SSH keys
- Direct network access (all traffic goes through the proxy)

The gateway container:
- Enforces branch ownership policies
- Validates all git operations
- Manages session tokens
- Provides audit logging

## Credential Isolation

The sandbox container never receives credentials directly. Instead:

1. Gateway runs an auth proxy endpoint
2. Sandbox has `ANTHROPIC_BASE_URL` pointing to the gateway
3. Gateway injects authentication headers before forwarding requests
4. This ensures credentials never exist in the sandbox environment
