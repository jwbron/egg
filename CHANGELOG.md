# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Gateway now allows `git merge -X` / `--strategy-option` with safe conflict resolution values: `ours`, `theirs`, `patience`, `ignore-space-change`, `ignore-all-space`, `ignore-space-at-eol`. Values are validated against an allowlist — unknown values are rejected.
- Type-aware HITL rendering in local mode: `decision_type` field (`phase_gate`, `choice`, `feedback`) on `HITLDecision` drives context-specific terminal UIs. Phase gates show draft previews with edit/approve/request-changes. Choices render numbered options. Feedback prompts per-question with review-before-submit. Universal options (general feedback, change approach, cancel) available on all types. JSON resolution payloads replace bare strings for structured intent parsing, with backward-compatible legacy keyword matching.
- Gateway sidecar with policy enforcement (branch ownership, merge blocking, push policies)
- Sandbox container with credential isolation and git/gh wrappers
- Anthropic API credential injection via gateway proxy
- GitHub App token management with automatic refresh
- Git worktree isolation for agent sessions
- Public and private network modes (Squid proxy integration)
- GitHub Action for CI/CD automation (`action/`)
- @mention trigger workflow for GitHub issues and PRs
- Shared libraries: `egg_config` (configuration), `egg_logging` (structured logging), `egg_git` (git utilities)
- CLI tool (`egg`, `egg --setup`, `egg --exec`, `egg --compose --down`) with separate `egg-config` and `egg-deploy` utilities
- Claude Code integration with custom rules and slash commands
- Container log persistence and correlation system
- Rate limiting for gateway operations
- Fork and private repository access policies
- CI infrastructure (GitHub Actions, pre-commit hooks, native make targets)
- Comprehensive test suite (130+ test files)
- Architecture Decision Records (ADRs) for all major design choices

### Changed
- `.egg-state` files now use per-pipeline namespacing to prevent merge conflicts. Agent output files use `{identifier}-{role}-output.json` (e.g., `871-coder-output.json`) instead of `{role}-output.json`. Check results use `{identifier}-implement-results.json`. Backward compatibility maintained via fallback to old paths when namespaced files don't exist.

### Fixed
- N/A
