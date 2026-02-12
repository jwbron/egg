# Plan: Update README.md

## Summary

Refresh the egg repository README.md to accurately reflect the current state of the project. The README is largely accurate and well-structured, but has specific areas that need correction, clarification, or expansion based on review of the actual codebase.

## Findings from Analysis

### Issues Identified

1. **Quick Start: `pip install ./sandbox` is misleading** — The sandbox has a `pyproject.toml` but is not a standalone pip-installable package in the traditional sense. The `egg` CLI entry point is defined in the root `pyproject.toml` via `egg_lib.cli:main`. The Quick Start should reflect the actual installation flow (which uses `egg` directly or Docker).

2. **Versioning note is stale** — "Use `@main` until the first release (v0.1.0) is published" suggests v0.1.0 hasn't been released, but `pyproject.toml` already declares `version = "0.1.0"`. No git tags exist yet. This section should clarify the actual release status.

3. **CHANGELOG CLI commands don't match README CLI reference** — The CHANGELOG mentions `egg start`, `egg stop`, `egg exec`, `egg logs`, `egg status`, `egg config validate` as CLI commands. The README documents `egg`, `egg --public`, `egg --private`, `egg --setup`, `egg --reset`, `egg --exec`, `egg --compose`. The README appears to be the accurate reflection. The CHANGELOG is out of scope for this task but worth noting.

4. **Orchestrator service not mentioned** — The `docker-compose.yml` includes an orchestrator service (port 9849) that coordinates pipeline state, but the README doesn't mention it at all. This is a gap for users doing Docker Compose deployments.

5. **Workflow count** — README says nothing about count, but there are 23 workflows (not 24 as initially thought). The README's workflow table is correct but only lists 6 of 23. This is fine as a summary, but users may want to know about additional workflows like conflict resolution, doc updating, and contract verification.

6. **Missing: Reusable Workflows guide** — The docs index references a `guides/reusable-workflows.md` for using egg's SDLC workflows in external repos. The README's Documentation section doesn't link to this.

7. **Missing: `--down` flag documentation** — The CLI reference shows `egg --compose --down` but the Flags table doesn't include `--down`.

8. **Missing: `--setup` and `--reset` in Flags table** — These are in the CLI Reference command table but not in the Flags table below it.

9. **GitHub Action section is thin** — Only shows a basic usage snippet. Could benefit from mentioning key inputs (prompt types, auth methods) and linking to the action README more prominently.

10. **`bin/egg` is a symlink** — `bin/egg` symlinks to `../sandbox/egg`. The Quick Start `pip install ./sandbox` instruction doesn't reflect this.

11. **Multi-agent section could reference agent modes** — The sandbox includes slash commands for coder-mode, tester-mode, documenter-mode, and integrator-mode. These are how roles are activated in practice.

12. **Strategy/philosophy docs missing from README** — The docs index references `agentic-feedback-loop.md` and `collaboration-effectiveness.md` as strategy documents. Only the latter is linked in the README.

### What's Accurate (No Changes Needed)

- Pipeline diagram and phase descriptions
- Gateway architecture diagram and description
- Enforcement table (What's Enforced)
- Phase permissions table
- Isolation description
- Multi-agent role table (accurate at high level)
- GitHub automation workflow table
- SDLC pipeline triggering instructions
- Human-in-the-loop decisions section
- Docker Compose deployment instructions
- egg-deploy CLI reference
- ADR documentation links
- License section

## Implementation Phases

### Phase 1: Fix Inaccurate Content

**Tasks:**

1. **Fix Quick Start local instructions** — Replace `pip install ./sandbox` with the correct setup flow. The `egg` CLI is installed via the root package. Update to reflect that `make setup` or `pip install -e .` is the correct local install path, followed by running `egg`.

2. **Update Versioning section** — Clarify the current release status. v0.1.0 is declared in pyproject.toml but no git tags/releases exist yet. The note about using `@main` should be updated or the status clarified.

3. **Add `--down` flag to Flags table** — Include the `--down` flag used with `--compose`.

4. **Add `--setup` and `--reset` to Flags table** — These flags are documented in the command table but missing from the flags section.

**Acceptance Criteria:**
- Quick Start local section shows accurate installation commands
- Versioning section accurately reflects release status
- All documented CLI flags appear in the Flags table

### Phase 2: Add Missing Content

**Tasks:**

5. **Add Orchestrator mention to Docker Compose section** — Briefly mention that `bin/egg-deploy up` starts both the gateway and orchestrator services, and describe the orchestrator's role in pipeline state management.

6. **Add Reusable Workflows link to Documentation section** — Add the reusable workflows guide to the documentation section for external repo users.

7. **Add Agentic Feedback Loop link** — Add the strategy doc to the Documentation section alongside the existing "Why egg Works" link.

8. **Expand GitHub Action section slightly** — Add a note about available auth methods and key configuration options without duplicating the action README.

**Acceptance Criteria:**
- Docker Compose section mentions orchestrator service
- Documentation section includes reusable workflows and feedback loop links
- GitHub Action section provides slightly more context

### Phase 3: Minor Improvements

**Tasks:**

9. **Consolidate CLI Reference** — The Flags table duplicates information from the command table. Merge the two so flags aren't listed separately from their commands, or ensure they're consistent and cross-reference each other.

10. **Add Prerequisites section or note** — The current Quick Start assumes users have Python 3.11+, Docker, and uv installed. Add a brief prerequisites note before the Quick Start sections.

**Acceptance Criteria:**
- CLI Reference section is internally consistent (no duplicate/conflicting info)
- Prerequisites are stated before Quick Start

## Test Strategy

Since this is a documentation-only change:

1. **Link verification** — Check that all relative links in the README resolve to existing files in the repository.
2. **Markdown rendering** — Verify tables and diagrams render correctly (no broken formatting).
3. **Accuracy spot-check** — Cross-reference updated claims against actual files (pyproject.toml, Makefile, bin/, docker-compose.yml).
4. **No broken ASCII art** — Ensure the pipeline and gateway diagrams still display correctly.

Automated tests are not applicable. Manual review is the verification method.

## Risks and Rollback

**Risks:**
- **Low**: Incorrect information introduced during update. Mitigated by cross-referencing all claims against source code.
- **Low**: Formatting breakage. Mitigated by keeping changes minimal and preserving existing structure.
- **Low**: Scope creep into restructuring. Mitigated by focusing only on accuracy and completeness, not reorganization.

**Rollback:**
- Single commit, easily reverted with `git revert`.
- No code changes, so no risk of functional regression.

## Out of Scope

- Restructuring the README layout or information architecture
- Updating CHANGELOG.md (separate concern)
- Updating component READMEs (gateway, sandbox, shared, etc.)
- Creating new documentation pages
- Updating docs/index.md (already accurate)

Authored-by: egg
