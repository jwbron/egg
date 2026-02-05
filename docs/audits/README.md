# Audit Reports

This directory contains audit reports for the egg repository.

## Available Audits

| Date | Audit | Status | Summary |
|------|-------|--------|---------|
| 2026-02-04 | [Migration Audit](2026-02-migration-audit.md) | Complete | Post-migration review from james-in-a-box |

## Quick Reference: Open Issues

### Critical (Fix Immediately)

1. **Vestigial "jib_config" naming** - 10+ files need updates
   - See: [Migration Audit - Critical Issues](2026-02-migration-audit.md#critical-issues-must-fix)

2. **13 broken documentation links** - Users cannot navigate docs
   - See: [Migration Audit - Broken Links](2026-02-migration-audit.md#2-broken-documentation-links-13-total)

3. **Missing `docs/reference/beads.md`** - Referenced as CRITICAL in CLAUDE.md
   - See: [Migration Audit - Documentation Gaps](2026-02-migration-audit.md#documentation-gaps)

### High Priority

4. **Security linters not in CI** - 4 of 5 scripts not enforced
5. **Gateway test coverage gaps** - policy.py, private_repo_policy.py untested
6. **Terminology mismatch** - user_mode vs incognito inconsistency

## Audit Schedule

| Audit Type | Frequency | Last Run | Next Due |
|------------|-----------|----------|----------|
| Migration/Naming | One-time | 2026-02-04 | N/A |
| Documentation | Quarterly | 2026-02-04 | 2026-05-04 |
| Security | Monthly | - | TBD |
| Test Coverage | Monthly | 2026-02-04 | 2026-03-04 |
