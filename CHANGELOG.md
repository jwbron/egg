# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Core Infrastructure (Phase 1)**
  - Repository structure with Python packaging (pyproject.toml, hatchling)
  - GitHub Actions CI/CD workflow (lint, test, security scan)
  - Pre-commit hooks for code quality
  - Development tooling (Makefile, dev script)

- **Documentation (Phase 1.5)**
  - Architecture documentation with security model
  - Configuration reference with examples
  - API documentation for gateway endpoints
  - Setup and troubleshooting guides
  - ADR (Architecture Decision Record) for design decisions

- **Gateway Module (Phase 2)**
  - Flask REST API server with Waitress production server
  - Session management with token-based authentication
  - Git/GitHub client wrappers with policy enforcement
  - Repository visibility checking (public/private mode enforcement)
  - Worktree management for isolated per-session workspaces
  - Rate limiting and audit logging
  - Squid HTTP proxy integration for network filtering
  - Private repository policy enforcement

- **Container Infrastructure (Phase 3)**
  - Gateway container (Squid proxy + Flask API)
  - Sandbox container (development environment)
  - Git/gh binary wrappers routing through gateway
  - Session token credential helper
  - Parameterized for standalone deployment

- **CLI Tool (Phase 4)**
  - `egg start` - Start sandbox environment with network setup
  - `egg stop` - Stop and cleanup containers
  - `egg status` - Show container and network status
  - `egg exec` - Execute commands in sandbox
  - `egg logs` - View container logs
  - `egg config` - Configuration management (validate, show, init)
  - YAML configuration with repository and GitHub App support

### Changed
- N/A

### Fixed
- N/A
