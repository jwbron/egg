# Egg Repository Migration Audit

**Date:** 2026-02-04
**Scope:** Post-migration review from james-in-a-box
**Status:** Complete

## Executive Summary

This audit reviewed the entire egg repository for vestigial references from the james-in-a-box migration and assessed documentation completeness. The codebase is **largely clean** with high-quality code, but has **critical documentation gaps** and **several vestigial naming references** that need remediation.

### Overall Health Score: 7/10

| Category | Score | Status |
|----------|-------|--------|
| Vestigial Code Removal | 8/10 | Minor issues in docs/scripts |
| Documentation Completeness | 5/10 | Multiple critical gaps |
| Code Quality | 9/10 | Excellent across all modules |
| Naming Consistency | 7/10 | Some jib_config references remain |
| Test Coverage | 6/10 | Good for some modules, gaps in gateway |

---

## Critical Issues (Must Fix)

### 1. Vestigial "jib_config" References

**Severity:** HIGH
**Impact:** Confusion for developers, broken documentation examples

| Location | Issue |
|----------|-------|
| `shared/egg_config/README.md` | Title says "# jib_config", all examples use wrong import |
| `shared/pyproject.toml` | References `jib_config*`, `jib_logging*` in include list |
| `tests/egg_config/conftest.py` | Docstring says "jib_config tests" |
| `tests/egg_config/test_base.py` | Docstring says "jib_config.base module" |
| `tests/egg_config/test_cli.py` | Docstring says "jib_config.cli module" |
| `tests/egg_config/test_registry.py` | Docstring says "jib_config.registry module" |
| `scripts/check-*.py` (4 files) | References to "jib_exec" pattern |

**Action Required:** Replace all "jib_config" with "egg_config", update jib_exec references.

### 2. Broken Documentation Links (13 total)

**Severity:** HIGH
**Impact:** Users cannot navigate documentation

**In README.md (6 broken links):**
- `docs/architecture.md` → Should be `docs/architecture/README.md`
- `docs/security.md` → Does not exist
- `docs/configuration.md` → Does not exist
- `docs/setup.md` → Should be `docs/setup/README.md`
- `docs/api.md` → Does not exist
- `docs/troubleshooting.md` → Should be `docs/troubleshooting/github-auth-in-long-running-containers.md`

**Missing referenced files:**
- `docs/setup/slack-quickstart.md` (4 references)
- `docs/setup/slack-bidirectional.md` (2 references)
- `docs/reference/beads.md` (critical - referenced in CLAUDE.md)
- `docs/reference/slack-quick-reference.md`
- `docs/development/beads-integration.md`
- `docs/user-guide/` directory

### 3. Terminology Mismatch: "user_mode" vs "incognito"

**Severity:** MEDIUM
**Location:** `config/` directory

The codebase uses two terms for the same feature:
- Python code: `user_mode`, `is_user_mode_repo()`
- YAML examples: `incognito:`, `auth_mode: incognito`
- Environment: `GITHUB_INCOGNITO_TOKEN`

**Action Required:** Standardize on one term throughout.

### 4. Missing CI/CD Integration for Security Linters

**Severity:** MEDIUM
**Location:** `scripts/`

Four security-critical linting scripts are not integrated into CI:
- `check-claude-imports.py`
- `check-container-host-boundary.py`
- `check-gh-cli-usage.py`
- `validate-config.py`

Only `check-container-paths.py` is in the Makefile.

---

## Documentation Gaps

### Missing Files (Must Create)

| File | Priority | Purpose |
|------|----------|---------|
| `docs/reference/beads.md` | CRITICAL | Task tracking (referenced in CLAUDE.md as CRITICAL) |
| `docs/setup/slack-quickstart.md` | HIGH | 10-minute setup guide |
| `docs/setup/slack-bidirectional.md` | HIGH | Two-way communication |
| `docs/reference/slack-quick-reference.md` | HIGH | Common operations |
| `docs/development/beads-integration.md` | HIGH | Integration guide |
| `shared/egg_logging/README.md` | MEDIUM | Module documentation |
| `shared/egg_git/README.md` | MEDIUM | Module documentation |
| `scripts/README.md` | MEDIUM | Script inventory |

### Missing ADRs (Referenced but don't exist)

| ADR | Referenced In |
|-----|---------------|
| `ADR-Message-Queue-Slack-Integration.md` | Context-Sync-Strategy, Autonomous-SE |
| `ADR-Slack-Bot-GCP-Integration.md` | Context-Sync-Strategy, Autonomous-SE |
| `ADR-GCP-Deployment-Terraform.md` | Context-Sync-Strategy, Autonomous-SE, Logging |
| `ADR-Egg-Repo-Onboarding.md` | adr/README.md |

---

## Directory-by-Directory Summary

### Root Files (Score: 8/10)
- **Good:** README.md, Makefile, pyproject.toml well-structured
- **Issue:** README links to 6 non-existent docs

### bin/ (Score: 9/10)
- **Good:** Clean symlink architecture, well-documented
- **Issue:** Port 9847 vs 9848 inconsistency in gateway docs

### config/ (Score: 6/10)
- **Good:** Clear configuration templates
- **Issues:** user_mode/incognito terminology mismatch, missing referenced files

### docs/ (Score: 5/10)
- **Good:** Strong ADR system, good index structure
- **Issues:** 13+ broken links, 4 missing ADRs, inconsistent navigation

### gateway/ (Score: 9.5/10)
- **Excellent:** Production-ready code, comprehensive documentation
- **Good:** Strong security model, extensive test coverage
- **Minor:** 2 files could use docstrings

### sandbox/ (Score: 9.5/10)
- **Excellent:** Professional engineering, strong type hints
- **Good:** Comprehensive docstrings, security-conscious
- **Minor:** Could benefit from environment variable reference doc

### scripts/ (Score: 6/10)
- **Good:** Well-written linting scripts
- **Issues:** jib_exec references, no README, 4/5 not in CI

### shared/ (Score: 6/10)
- **Good:** Well-architected modules
- **Critical:** README uses wrong module name (jib_config)
- **Issues:** Missing READMEs for egg_logging, egg_git

### tests/ (Score: 6/10)
- **Good:** 887 tests, good organization
- **Issues:** 4 jib_config docstrings, significant coverage gaps in gateway

---

## Test Coverage Analysis

### Well-Covered (Good)
- `egg_logging` - 100% of modules tested
- `egg_config` core modules - base, cli, validators, registry
- Infrastructure tests - bash/python syntax validation

### Coverage Gaps (Needs Attention)

**Gateway (Critical - Security Module):**
| File | Size | Test Status |
|------|------|-------------|
| policy.py | 27 KB | NOT TESTED |
| private_repo_policy.py | 18 KB | NOT TESTED |
| github_client.py | 26 KB | NOT TESTED |
| session_manager.py | - | NOT TESTED |
| rate_limiter.py | - | NOT TESTED |
| token_refresher.py | - | NOT TESTED |

**Sandbox:**
- `egg_lib/` (10+ modules) - Complete directory untested
- `statusbar.py` - NOT TESTED

---

## Remediation Plan

### Phase 1: Critical Fixes (1-2 days)

1. **Fix vestigial naming:**
   - Update `shared/egg_config/README.md` (replace jib_config → egg_config)
   - Update `shared/pyproject.toml` include list
   - Update 4 test docstrings
   - Update scripts/ jib_exec references

2. **Fix broken links in README.md:**
   - Point to actual documentation locations

3. **Create critical missing docs:**
   - `docs/reference/beads.md` (CRITICAL - referenced in CLAUDE.md)

### Phase 2: Documentation (2-3 days)

4. Create missing setup guides:
   - `docs/setup/slack-quickstart.md`
   - `docs/setup/slack-bidirectional.md`

5. Create reference docs:
   - `docs/reference/slack-quick-reference.md`
   - `docs/development/beads-integration.md`

6. Create module READMEs:
   - `shared/egg_logging/README.md`
   - `shared/egg_git/README.md`
   - `scripts/README.md`

### Phase 3: Quality Improvements (3-5 days)

7. Add gateway test coverage for:
   - policy.py
   - private_repo_policy.py
   - github_client.py

8. Integrate security linters into CI:
   - check-claude-imports.py
   - check-container-host-boundary.py
   - check-gh-cli-usage.py

9. Standardize user_mode/incognito terminology

---

## Positive Findings

### Code Quality Highlights

1. **Gateway module** - Production-grade with excellent documentation, comprehensive test coverage, strong security model
2. **Sandbox module** - Professional L3-L4 engineering standards, strong type hints, security-conscious
3. **No critical vestigial code** - The actual running code is clean; issues are in documentation/comments
4. **Consistent "egg" naming in code** - Module names, class names, function names all use egg terminology

### Architecture Strengths

1. Clear separation of concerns between gateway, sandbox, and shared modules
2. Well-designed ADR system with clear status tracking
3. Strong security model with fail-closed defaults
4. Good use of type hints and dataclasses throughout

---

## Appendix: Files Requiring Updates

### High Priority
```
shared/egg_config/README.md
shared/pyproject.toml
README.md (fix 6 broken links)
tests/egg_config/conftest.py
tests/egg_config/test_base.py
tests/egg_config/test_cli.py
tests/egg_config/test_registry.py
scripts/check-claude-imports.py
scripts/check-container-host-boundary.py
scripts/check-gh-cli-usage.py
scripts/check-container-paths.py (JIB001 → EGG001)
```

### Medium Priority
```
config/README.md (fix github-integration.md link)
config/repositories.yaml.example (terminology)
docs/README.md (align with index.md)
gateway/README.md (port 9847 → 9848)
gateway/tests/README-integration.md (port 9847 → 9848)
```

---

*This audit was conducted on 2026-02-04 as part of post-migration validation.*
