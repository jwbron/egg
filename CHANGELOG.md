# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Gateway sidecar with policy enforcement (branch ownership, merge blocking, push policies)
- Sandbox container with credential isolation and git/gh wrappers
- Anthropic API credential injection via gateway proxy
- GitHub App token management with automatic refresh
- Git worktree isolation for agent sessions
- Public and private network modes (Squid proxy integration)
- GitHub Action for CI/CD automation (`action/`)
- @mention trigger workflow for GitHub issues and PRs
- Shared libraries: `egg_config` (configuration), `egg_logging` (structured logging), `egg_git` (git utilities)
- CLI tool (`egg start`, `egg stop`, `egg exec`, `egg logs`, `egg status`, `egg config validate`)
- Claude Code integration with custom rules and slash commands
- Container log persistence and correlation system
- Rate limiting for gateway operations
- Fork and private repository access policies
- CI infrastructure (GitHub Actions, pre-commit hooks, act-based local CI)
- Comprehensive test suite (43+ test files)
- Architecture Decision Records (ADRs) for all major design choices

### Changed
- N/A

### Fixed
- N/A
