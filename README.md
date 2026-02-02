# egg

Sandboxed LLM code execution environment with infrastructure-level security controls.

**The core principle:** Security through infrastructure, not instructions. An LLM cannot bypass controls that don't exist in its environment.

## Quick Start

```bash
# Clone and set up
git clone https://github.com/jwbron/egg.git
cd egg
./dev setup

# Start the sandbox
egg start --config egg.yaml
```

## Architecture

```
                       ┌─────────────────────────────────────────────────┐
                       │                    egg                          │
                       │                                                 │
┌──────────────────────┼───────────────────┐    ┌───────────────────────┤
│    Gateway Container │                   │    │   Sandbox Container   │
│                      │                   │    │                       │
│  ┌─────────────┐     │                   │    │  ┌─────────────┐      │
│  │ REST API    │◄────┼───────────────────┼────┼──│ git wrapper │      │
│  │ Server      │     │                   │    │  └─────────────┘      │
│  └─────────────┘     │                   │    │                       │
│                      │                   │    │  ┌─────────────┐      │
│  ┌─────────────┐     │                   │    │  │ gh wrapper  │      │
│  │ Policy      │     │                   │    │  └─────────────┘      │
│  │ Engine      │     │                   │    │                       │
│  └─────────────┘     │                   │    │  ┌─────────────┐      │
│                      │                   │    │  │ LLM CLI     │      │
│  ┌─────────────┐     │                   │    │  │ (Claude)    │      │
│  │ HTTP Proxy  │◄────┼───────────────────┼────┼──│             │      │
│  │ (Squid)     │     │                   │    │  └─────────────┘      │
│  └─────────────┘     │                   │    │                       │
│                      │                   │    │  NO: GitHub tokens    │
└──────────────────────┼───────────────────┘    │  NO: SSH keys         │
                       │                        │  NO: Direct network   │
                       │                        │  YES: Workspace files │
                       │                        │  YES: LLM API (proxy) │
                       │                        └───────────────────────┤
                       │                                                 │
                       └─────────────────────────────────────────────────┘
```

## Features

- **Gateway Sidecar**: All git/gh operations routed through a policy-enforcing gateway
- **Network Isolation**: Configurable domain allowlists for both public and private modes
- **Session-based Access**: Per-container session tokens for multi-container environments
- **Audit Logging**: Structured logs for all operations with correlation IDs
- **Worktree Isolation**: Per-container git worktrees preventing cross-contamination
- **Credential Isolation**: Sandbox container never has direct credential access

## CLI Commands

| Command | Description |
|---------|-------------|
| `egg start` | Start the sandbox environment (gateway + container) |
| `egg stop` | Stop the sandbox environment |
| `egg exec <cmd>` | Execute a command inside the sandbox container |
| `egg logs [--follow]` | View container logs |
| `egg status` | Show running containers and health status |
| `egg config validate` | Validate configuration files |

### CLI Flags for `egg start`

| Flag | Description |
|------|-------------|
| `--config <path>` | Path to egg.yaml config file (default: `./egg.yaml`) |
| `--private` | Enable private network mode (blocks all external network except Claude API) |
| `--headless` | Run in non-interactive/headless mode (for automation, CI, scripted workflows) |

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Security Model](docs/security.md)
- [API Reference](docs/api.md)
- [Setup Guide](docs/setup.md)
- [Testing Guide](docs/testing.md)
- [Troubleshooting](docs/troubleshooting.md)

### Architecture Decision Records (ADRs)

- [ADR: Git Isolation Architecture](docs/adr/git-isolation-architecture.md)
- [ADR: Credential Injection](docs/adr/credential-injection.md)
- [ADR: Network Isolation](docs/adr/network-isolation.md)

## Development

```bash
# Set up development environment
./dev setup

# Run linters (same as CI)
./dev lint

# Run tests (same as CI)
./dev test

# Run full CI pipeline locally
./dev ci

# Fast mode (native, no Docker overhead)
./dev native lint
./dev native test
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

## License

MIT License - see [LICENSE](LICENSE) for details.
