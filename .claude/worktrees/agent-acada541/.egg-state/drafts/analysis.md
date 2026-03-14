# Analysis: Audit and Update All Markdown Documentation

## Problem Statement

Audit all markdown (.md) documentation files in the egg repository against the actual current codebase. For each file: compare documented behavior, commands, file paths, architecture descriptions, and code examples against reality. Fix outdated content inline — no TODO markers.

## Current State Assessment

The egg repository contains ~70 markdown files across:
- Root-level docs: `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `RELEASING.md`
- Documentation hub: `docs/` (index, architecture, ADRs, guides, templates, strategy)
- Component READMEs: `gateway/`, `sandbox/`, `shared/`, `config/`, `bin/`, `action/`
- Claude Code config: `sandbox/.claude/` (rules, commands)
- Draft state: `.egg-state/drafts/` (not auditable — ephemeral per-task)

Overall documentation quality is high. The architecture diagrams, enforcement tables, and narrative structure are strong. Most content is accurate. However, the codebase has evolved faster than the documentation in several areas, creating gaps between documented and actual state.

## Issues Found

Issues are organized by severity and grouped by file.

---

### CRITICAL: Content that will actively mislead or cause errors

#### C1. `config/README.md` — References non-existent `host_config.py` module

**Lines affected:** 63-89

The README documents a `config/host_config.py` module with a `HostConfig` class, CLI interface (`python config/host_config.py --list`), and `config/host-config.template.yaml` template. None of these exist. The actual `config/` directory contains only `repo_config.py`, `repositories.yaml.example`, and `secrets.template.env`.

Code that imports `HostConfig` (e.g., `sandbox/egg_lib/auth.py`) uses try/except to gracefully degrade. The ADR-Declarative-Setup-Architecture is still "Proposed" status — this module was designed but never implemented.

**Fix:** Remove the `host_config.py` documentation section. Document what actually exists: `repo_config.py` for repository configuration, `secrets.template.env` for secrets templating. Note that the unified config system is planned but not yet implemented.

#### C2. `shared/egg_config/README.md` — Code example uses unimplemented `SlackConfig`

**Location:** "Secret Masking" section

The README shows a code example using `SlackConfig.from_env()`. The same README explicitly states "SlackConfig, JiraConfig, and ConfluenceConfig are planned but not yet implemented." Users copying the example will get an ImportError.

**Fix:** Replace the `SlackConfig` example with an implemented config class (e.g., `GitHubConfig` or `GatewayConfig`).

#### C3. `CHANGELOG.md` — CLI commands don't match actual CLI

**Line 20**

Lists CLI as: `egg start`, `egg stop`, `egg exec`, `egg logs`, `egg status`, `egg config validate`. The actual CLI is flag-based: `egg`, `egg --exec`, `egg --setup`, `egg --compose --down`. There are no subcommands like `egg start` or `egg stop`. The `config validate` functionality exists as a separate `egg-config validate` tool, not as an `egg` subcommand.

**Fix:** Update to: `CLI tool (egg, egg --setup, egg --exec, egg --compose)` and separately mention `egg-deploy` and `egg-config`.

#### C4. `CHANGELOG.md` — Test count outdated

**Line 26**

Claims "43+ test files". Actual count is 153 test files across `tests/`, `gateway/tests/`, and `integration_tests/`.

**Fix:** Update to "153 test files" or "150+ test files".

---

### HIGH: Significant gaps in documented vs actual functionality

#### H1. `gateway/README.md` — Undocumented API endpoints

The API Endpoints section is missing several endpoint groups that exist in `gateway.py`:

- **Worktree Management:** `POST /api/v1/worktree/create`, `POST /api/v1/worktree/delete`, `GET /api/v1/worktree/list`
- **Session Management:** `POST /api/v1/sessions/create`, `DELETE /api/v1/sessions/<token>`, `GET /api/v1/sessions/<token>`, `POST /api/v1/sessions/<token>/heartbeat`, `PATCH /api/v1/sessions/<token>`, `PATCH /api/v1/sessions/<token>/phase`, `GET /api/v1/sessions`
- **Repository info:** `GET /api/v1/repos/visibility`
- **Anthropic Proxy:** `POST /v1/messages`, `POST /v1/messages/count_tokens`
- **Git execute:** `POST /api/v1/git/execute`

**Fix:** Add sections for Worktree Operations, Session Management, Repository Operations, and Anthropic Proxy endpoints.

#### H2. `gateway/README.md` — Undocumented source files

Missing from the Files listing: `agent_restrictions.py`, `checkpoint_handler.py`, `transcript_buffer.py`. Also missing 8 test files: `test_checkpoint_handler.py`, `test_concurrency.py`, `test_config_validator.py`, `test_edge_cases.py`, `test_error_paths.py`, `test_fork_policy.py`, `test_proxy_monitor.py`, `test_transcript_buffer.py`.

**Fix:** Add these files to the Files section with descriptions.

#### H3. `sandbox/README.md` — Missing egg_lib modules

The README documents 12 files in `egg_lib/`, but 18 exist. Missing: `checkpoint_cli.py`, `compose.py`, `contract_cli.py`, `orchestration.py`, and the entire `self_improvement/` subdirectory (with `collect.py`, `config.py`, `collectors/base.py`, `collectors/gha.py`, `collectors/local.py`).

**Fix:** Add the missing modules to the egg_lib file listing.

#### H4. `sandbox/README.md` — Missing bin/ symlinks

Documents 3 symlinks but 5 exist. Missing: `egg-checkpoint -> ../egg_lib/checkpoint_cli.py` and `egg-contract -> ../egg_lib/contract_cli.py`.

**Fix:** Add the two missing symlinks.

#### H5. `sandbox/README.md` — Incomplete .claude/ documentation

- `commands/` shows only `show-metrics.md` but 7 files exist (missing: `coder-mode.md`, `documenter-mode.md`, `integrator-mode.md`, `sdlc.md`, `tester-mode.md`, `README.md`)
- `rules/` shows 5 files but 7 exist (missing: `contract.md`, `README.md`)

The `contract.md` rule is particularly important as it documents the SDLC contract workflow with `egg-contract` CLI commands.

**Fix:** Update the directory tree to show all actual files.

#### H6. `shared/README.md` — Missing `egg_container` package

The entire `egg_container` package is undocumented. It contains `build_sandbox_docker_cmd()` (core function used by launchers and tests) and `ContainerNetworkConfig` dataclass.

**Fix:** Add an `egg_container` section to the shared README.

#### H7. `shared/README.md` — 15 undocumented egg_contracts modules

The "Key modules" section lists a few modules but omits 15 active modules including `agent_roles.py`, `checkpoint_cli.py`, `checkpoint_loader.py`, `checkpoints.py`, `dependency_graph.py`, `loader.py`, `orchestration.py`, `orchestrator.py`, `phase_defaults.py`, `redactor.py`, `transcript_extractor.py`, `usage.py`, `usage_cli.py`, `usage_loader.py`, `validator.py`.

**Fix:** Expand the Key modules section to cover all modules.

#### H8. `action/README.md` — Incomplete file listing

Documents 11 files but 25 exist. Missing 14 files including role-specific prompt builders (`build-coder-prompt.sh`, `build-tester-prompt.sh`, `build-documenter-prompt.sh`, `build-integrator-prompt.sh`), workloop variants, and convention files.

**Fix:** Add all missing files to the listing.

#### H9. `docs/guides/sdlc-pipeline.md` — References non-existent prompt builders

Lines 730-731 reference `action/build-refine-review-prompt.sh` and `action/build-plan-review-prompt.sh`. These files do not exist. They were consolidated into `action/build-unified-review-prompt.sh` which handles all SDLC phases.

**Fix:** Replace the two stale entries with the actual `build-unified-review-prompt.sh` script.

#### H10. `docs/adr/implemented/ADR-Declarative-Setup-Architecture.md` — Claims completion but implementation differs

The ADR claims Phase 5 "✓ COMPLETED" (December 16, 2025) with deliverables including `setup.py` and `config/setup/` module. Neither exists. The actual implementation is a simpler interactive wizard in `sandbox/egg_lib/setup_flow.py`. The `--setup` flag works, but the modular architecture, service management (`--enable-services`), and `host_config.py` were never built.

**Fix:** Update ADR status to reflect actual state. Mark the completed aspects (interactive setup via `egg --setup`, secrets.env/config.yaml creation) and note the deferred aspects (modular config/setup module, service management, HostConfig class).

---

### MODERATE: Incomplete information that could confuse developers

#### M1. `shared/egg_config/README.md` — Incomplete constants list

Lists selected constants but omits orchestrator-related ones: `ORCHESTRATOR_CONTAINER_NAME`, `ORCHESTRATOR_IMAGE_NAME`, `ORCHESTRATOR_PORT`, `ORCHESTRATOR_ISOLATED_IP`, `ORCHESTRATOR_EXTERNAL_IP`, `TEST_GATEWAY_PORT`, `TEST_GATEWAY_PROXY_PORT`.

**Fix:** Add the missing constants or expand the "See constants.py for the full list" reference.

#### M2. `shared/egg_config/README.md` — GatewayConfig fields undersold

Documents "Key Fields: `secret`, `port`" but actual class also includes `host` (bind address) and `rate_limits` (RateLimitConfig with 7 fields).

**Fix:** Update the Key Fields to include `host` and `rate_limits`.

#### M3. `sandbox/.claude/commands/README.md` — Lists only 2 commands

Documents `/sdlc` and `/show-metrics` but 6 commands exist (missing: `/coder-mode`, `/documenter-mode`, `/integrator-mode`, `/tester-mode`).

**Fix:** Add all commands to the listing.

#### M4. `sandbox/.claude/rules/README.md` — Missing `contract.md` rule

Lists 5 rules but `contract.md` exists and is important (documents SDLC contract CLI commands).

**Fix:** Add `contract.md` to the listing.

---

### LOW: Minor inconsistencies

#### L1. `README.md` — Versioning note may be outdated

Line 289: "Use `@main` until the first release (v0.1.0) is published". The `pyproject.toml` version is `0.1.0` but it's unclear if the GitHub release was actually published. The release script exists.

**Action:** Verify release status; update if v0.1.0 has been published.

#### L2. `docs/adr/README.md` — Minor ADR naming inconsistency

The README index entry says "Anthropic API Credential Injection" but the ADR file is actually about gateway credential injection more broadly. This is a minor label-vs-content mismatch.

**Action:** No change needed — the content is correct, just a naming nuance.

---

## Files NOT Requiring Changes (Verified Accurate)

The following files were audited and found to be accurate:

- `README.md` — Mostly accurate (minor issues noted in prior analysis, already addressed)
- `CONTRIBUTING.md` — Accurate
- `RELEASING.md` — Accurate
- `bin/README.md` — Accurate
- `sandbox/.claude/README.md` — Accurate
- `docs/index.md` — Accurate (all links verified)
- `docs/architecture/README.md` — Accurate
- `docs/hitl-decisions.md` — Accurate
- `docs/agentic-feedback-loop.md` — Conceptual; no code claims to verify
- `docs/collaboration-effectiveness.md` — Conceptual; no code claims to verify
- `.egg/contract-rules.md` — Operational guidance; accurate
- `docs/templates/*.md` — Templates; no code claims
- `docs/adr/implemented/ADR-SDLC-Pipeline.md` — Fully verified against implementation
- `docs/adr/implemented/ADR-Gateway-Credential-Injection.md` — Verified
- `docs/adr/implemented/ADR-Git-Isolation-Architecture.md` — Core verified (private repo mode documented as proposed)
- `docs/adr/implemented/ADR-Standardized-Logging-Interface.md` — Verified
- `docs/adr/in-progress/*.md` — Correctly marked as in-progress
- `docs/adr/not-implemented/*.md` — Correctly marked
- `docs/guides/deployment.md` — Verified
- `docs/guides/local-quickstart.md` — Verified
- `docs/guides/github-automation.md` — Verified
- `docs/guides/reusable-workflows.md` — Verified
- `docs/guides/agent-mode-design.md` — Verified
- `docs/guides/agent-development.md` — Verified
- `docs/guides/deploy-migration.md` — Verified
- `docs/development/STRUCTURE.md` — Verified
- `docs/development/TEST_COVERAGE_PLAN.md` — Verified
- `gateway/tests/README-integration.md` — Not auditable (test docs)

## Recommended Approach

### Scope: Targeted inline fixes across ~15 files

The documentation is fundamentally sound. No rewrites needed. Fix discrepancies inline, preserving existing structure and tone.

### Priority Order

1. **Critical fixes first** (C1-C4): Remove references to non-existent modules, fix misleading code examples and CLI claims
2. **High-priority gaps** (H1-H10): Add missing endpoints, modules, files to existing documentation sections
3. **Moderate gaps** (M1-M4): Expand incomplete listings
4. **Low-priority** (L1-L2): Minor label/status updates

### Files Requiring Changes (17 files)

| File | Issues | Severity |
|------|--------|----------|
| `config/README.md` | C1 | Critical |
| `shared/egg_config/README.md` | C2, M1, M2 | Critical + Moderate |
| `CHANGELOG.md` | C3, C4 | Critical |
| `gateway/README.md` | H1, H2 | High |
| `sandbox/README.md` | H3, H4, H5 | High |
| `shared/README.md` | H6, H7 | High |
| `action/README.md` | H8 | High |
| `docs/guides/sdlc-pipeline.md` | H9 | High |
| `docs/adr/implemented/ADR-Declarative-Setup-Architecture.md` | H10 | High |
| `sandbox/.claude/commands/README.md` | M3 | Moderate |
| `sandbox/.claude/rules/README.md` | M4 | Moderate |

### What NOT to Change

- Architecture diagrams (accurate and effective)
- Enforcement/permissions tables (correct)
- Narrative structure and tone of all documents
- Strategy/conceptual documents (accurate)
- ADRs that accurately reflect their implementation status
- Template files (structural, not code-referencing)

## Constraints

- Local pipeline only — no push, no PR, no GitHub operations
- Preserve existing structure and tone of each document
- Fix content inline — no TODO markers
- Do not add new documentation files
- Focus on accuracy of code references, CLI commands, file paths, API descriptions

## Dependencies

- No code changes required
- No test changes required
- All changes are documentation-only edits to existing markdown files
