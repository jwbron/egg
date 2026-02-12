# Analysis: Update README.md

## Problem Statement

The README.md needs to be reviewed and refreshed to reflect the current state of the egg project. This means checking for outdated setup instructions, missing sections, incorrect information, and ensuring it accurately describes the project purpose, setup steps, usage, and all other relevant sections.

## Current State Assessment

The existing README.md (337 lines) is well-structured and covers the core project narrative effectively. It includes:

- Project description and metaphor
- Pipeline phases diagram and explanation
- Gateway architecture diagram and enforcement table
- Phase permissions and isolation details
- Multi-agent orchestration roles
- GitHub automation workflows
- Quick Start (Local, GitHub Actions, Docker Compose)
- CLI reference tables
- Documentation links
- Versioning scheme
- Development commands

**Overall quality**: High. The architecture diagrams, enforcement tables, and narrative flow are strong. Most content is accurate against the current codebase.

## Issues Found

### 1. CLI Reference: Missing `--build` flag (Inaccurate)

**Location**: README.md lines 237-249 (Flags table)

**Problem**: The `--build` flag exists in `sandbox/egg_lib/cli.py` (lines 108-112) but is not documented in the README's Flags table. It rebuilds compose images before starting and is used as `egg --compose --build`.

**Evidence**: The flag is already referenced in the Quick Start Docker Compose section (line 192 area is close, but line 47 of `cli.py` shows `--compose --build`), and in `docs/guides/local-quickstart.md`, but the Flags table omits it.

**Fix**: Add `--build` to the Flags table: `| --build | Rebuild compose images before starting (use with --compose) |`

### 2. CLI Reference: `--compose` description is misleading (Inaccurate)

**Location**: README.md line 221

**Problem**: The table entry says `egg --compose` means "Use Docker Compose for gateway management", suggesting it starts a standalone sandbox session. But in the actual CLI (`cli.py` lines 129-133), `--compose` only has an effect when combined with `--down` or `--build`. The default `egg` path already uses compose — `--compose` alone is a no-op (falls through to normal run).

**Evidence**: `cli.py` line 100-101: `"Explicit compose control (use with --down or --build). Default egg path already uses compose."`

**Fix**: Update the description to: `| egg --compose --down | Stop the Docker Compose stack |` and `| egg --compose --build | Rebuild compose images before starting |`. Remove the standalone `egg --compose` row, or clarify it's implicit.

### 3. Quick Start: Docker Compose section references `bin/egg-deploy` (Outdated/Confusing)

**Location**: README.md lines 177-194

**Problem**: The Quick Start shows a `bin/egg-deploy` workflow (`bin/egg-deploy init`, edit `.env`, `bin/egg-deploy up`, then `egg --compose`). However, the primary local quickstart workflow described in `docs/guides/local-quickstart.md` is simpler: `pip install -e ./sandbox`, `egg --setup`, then `egg --compose --build` / `egg`. The `egg-deploy` path is for advanced/production deployments, not the recommended quickstart path.

The section is labeled "Docker Compose (Advanced)" which is appropriate, but the Quick Start for local use (`pip install ./sandbox` then `egg`) doesn't mention that `egg` already uses compose automatically (the user doesn't need to think about compose at all for local use).

**Fix**: Add a note to the Local quickstart that `egg` handles docker compose automatically. Consider adding a reference to the local-quickstart guide for PAT-based setup.

### 4. Local Quickstart Guide not linked from README (Missing)

**Location**: Not in README at all

**Problem**: `docs/guides/local-quickstart.md` is a comprehensive step-by-step guide for getting egg running locally with a GitHub PAT. It covers setup, PAT configuration, `egg --compose --build`, SDLC pipeline usage (local and issue-driven), monitoring, and troubleshooting. This is arguably the most useful guide for new users, but is not linked from the README's Documentation section.

**Fix**: Add to the Documentation section under Guides or Quick Start: `- [Local Quickstart](docs/guides/local-quickstart.md) — Step-by-step local setup with PAT authentication`

### 5. Agent Development Guide not linked from README (Missing)

**Location**: Not in README at all

**Problem**: `docs/guides/agent-development.md` exists but is not linked from the README Documentation section.

**Fix**: Add to the Documentation section if relevant to external users.

### 6. Deploy Migration Guide not linked from README (Missing)

**Location**: Not in README at all

**Problem**: `docs/guides/deploy-migration.md` exists but is not linked from the README Documentation section.

**Fix**: Add to the Documentation section if relevant, or skip if it's purely internal.

### 7. HITL Decisions Guide not linked from README (Missing)

**Location**: Not in README at all

**Problem**: `docs/hitl-decisions.md` documents the human-in-the-loop decision workflow (formal decisions, feedback comments, phase approvals). This is referenced in `docs/index.md` but not in the README. The README has a brief HITL section (lines 143-151) but doesn't link to this deeper guide.

**Fix**: Add a link from the HITL section or from the Documentation section.

### 8. GitHub Action version reference inconsistency (Minor)

**Location**: README.md line 201 vs line 295

**Problem**: The GitHub Action usage example (line 201) shows `jwbron/egg@main`, while the versioning section (line 295) recommends `jwbron/egg/action@v0`. The note on line 289 explains this (`Use @main until first release`), but the two examples use different path formats: `jwbron/egg@main` vs `jwbron/egg/action@v0`. The `/action` subdirectory reference in the versioning section is correct for composite actions in subdirectories, while the top-level reference may also work due to GitHub's action resolution.

**Fix**: Standardize to `jwbron/egg@main` in the quick example (which is correct for pre-v1), and clarify the `/action` path in the versioning section if needed, or make both consistent.

### 9. CHANGELOG CLI commands don't match actual CLI (Outdated in CHANGELOG)

**Location**: CHANGELOG.md line 20

**Problem**: The CHANGELOG lists CLI commands as `egg start`, `egg stop`, `egg exec`, `egg logs`, `egg status`, `egg config validate`. The actual CLI is flag-based: `egg`, `egg --exec`, `egg --compose --down`, etc. There are no subcommands like `egg start` or `egg stop`.

**Fix**: This is a CHANGELOG issue, not a README issue, but worth noting. The CHANGELOG should be updated to match the actual flag-based CLI.

### 10. Versioning note may be outdated (Check needed)

**Location**: README.md line 289

**Problem**: `> Note: Use @main until the first release (v0.1.0) is published, which will create the @v0 tag.` — The project version in `pyproject.toml` is `0.1.0`, and there's a release script at `.github/scripts/create-release.sh`. It's unclear whether v0.1.0 has been published as a GitHub release yet. If it has, this note should be removed and the example updated to `@v0`.

**Fix**: Verify release status. If v0.1.0 is published, update the action reference to `@v0` and remove the note. If not, leave as-is.

### 11. No "Requirements/Prerequisites" section (Missing)

**Location**: README.md Quick Start section

**Problem**: The Quick Start jumps straight to `git clone` / `pip install` without listing prerequisites. The actual requirements include:
- Python 3.11+
- Docker (Docker Desktop or Docker Engine with Compose v2)
- `uv` (for development, used by `make setup`)
- `gh` CLI (for GitHub operations)
- Anthropic credentials (OAuth token or API key)
- GitHub credentials (PAT or GitHub App)

The local-quickstart guide lists prerequisites; the README does not.

**Fix**: Add a brief prerequisites list before the Quick Start, or at the top of the Local section.

### 12. SDLC Pipeline Guide link in Documentation section (Duplicate)

**Location**: README.md lines 253-256 and 267

**Problem**: The SDLC Pipeline ADR is linked twice — once under "SDLC Pipeline" subsection and again under "Architecture Decision Records" subsection. Minor redundancy.

**Fix**: Keep both if intentional (different contexts), or consolidate.

## Recommended Approach

### Scope: Targeted refresh (not a rewrite)

The README is fundamentally sound. A full rewrite is unnecessary and would risk losing the strong narrative structure. Instead, make targeted fixes:

**Priority 1 — Accuracy fixes:**
1. Fix CLI reference table: add `--build`, fix `--compose` description
2. Add prerequisites to Quick Start section

**Priority 2 — Missing content:**
3. Link local-quickstart guide from README (most impactful for new users)
4. Link HITL decisions guide from the HITL section
5. Link agent-development guide if appropriate

**Priority 3 — Minor cleanup:**
6. Verify and update versioning note (check if v0.1.0 is released)
7. Standardize GitHub Action reference format
8. Note CHANGELOG CLI discrepancy (separate fix)

### Implementation Approach

Edit `README.md` in place, making the following specific changes:

1. **CLI Reference section** (lines 209-249):
   - Update `egg --compose` row to clarify it's used with `--down`/`--build`
   - Add `--build` to both the command table and flags table
   - Reword `--compose` description in flags table

2. **Quick Start > Local section** (lines 155-167):
   - Add a brief prerequisites note (Python 3.11+, Docker, Anthropic credentials)
   - Add link to local-quickstart guide for detailed PAT setup

3. **Documentation section** (lines 251-283):
   - Add `local-quickstart.md` link under Guides
   - Add `hitl-decisions.md` link under SDLC Pipeline or Other
   - Consider adding `agent-development.md` link

4. **HITL section** (lines 143-151):
   - Add link to `docs/hitl-decisions.md` for details

5. **Versioning section** (lines 286-321):
   - Verify release status and update note if needed

### What NOT to change

- The architecture diagrams (accurate and effective)
- The enforcement tables (correct)
- The overall narrative structure and flow
- The Multi-Agent Orchestration section (accurate)
- The GitHub Automation section (accurate)

## Constraints

- Local pipeline only — no push, no PR, no GitHub operations
- The README should remain under ~400 lines (currently 337)
- Must maintain the existing strong narrative flow
- All linked documents must exist (verified above)

## Dependencies

- None — all changes are to `README.md` and possibly `CHANGELOG.md`
- No code changes required
- No test changes required
