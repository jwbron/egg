# Migration Audit Issues Checklist

**Source:** [2026-02-04 Migration Audit](2026-02-migration-audit.md)

Use this checklist to track remediation progress.

---

## Vestigial Naming Issues

### jib_config References (8 files) - FIXED in PR #65

- [x] `shared/egg_config/README.md` - Replace "# jib_config" with "# egg_config", update all examples
- [x] `shared/pyproject.toml:13` - Update include list from `jib_config*`, `jib_logging*`
- [x] `tests/egg_config/conftest.py:1` - Update docstring
- [x] `tests/egg_config/test_base.py:2` - Update docstring
- [x] `tests/egg_config/test_cli.py:2` - Update docstring
- [x] `tests/egg_config/test_registry.py:2` - Update docstring

### jib_exec References (4 files) - FIXED in PR #65

- [x] `scripts/check-claude-imports.py:11,159` - Update jib_exec references
- [x] `scripts/check-container-host-boundary.py:64` - Update jib_path comment
- [x] `scripts/check-gh-cli-usage.py:14,16,401-407` - Update jib_exec references
- [x] `scripts/check-container-paths.py` - Change error code JIB001 → EGG001

---

## Broken Documentation Links

### README.md (6 links) - FIXED in PR #65

- [x] `docs/architecture.md` → Changed to `docs/architecture/README.md`
- [x] `docs/security.md` → Changed to `docs/FEATURES.md#security-features`
- [x] `docs/configuration.md` → Consolidated into Setup Guide
- [x] `docs/setup.md` → Changed to `docs/setup/README.md`
- [x] `docs/api.md` → Changed to `docs/reference/README.md`
- [x] `docs/troubleshooting.md` → Changed to `docs/troubleshooting/`

### Other Broken Links

- [ ] `config/README.md:100` - Fix `../docs/features/github-integration.md` reference
- [ ] `docs/index.md:30` - Fix `setup/slack-quickstart.md` reference
- [ ] `docs/README.md:15,17,19,22,54` - Fix multiple broken references
- [ ] `docs/setup/README.md:47,57` - Fix slack references
- [ ] `docs/development/STRUCTURE.md:156` - Fix user-guide reference

---

## Missing Documentation Files

### Critical (Create First) - FIXED in PR #65

- [x] `docs/reference/beads.md` - Task tracking system (referenced in CLAUDE.md as CRITICAL)

### High Priority

- [ ] `docs/setup/slack-quickstart.md` - Quick 10-minute setup
- [ ] `docs/setup/slack-bidirectional.md` - Two-way communication setup
- [ ] `docs/reference/slack-quick-reference.md` - Common operations
- [ ] `docs/development/beads-integration.md` - Integration guide

### Medium Priority

- [ ] `shared/egg_logging/README.md` - Module documentation
- [ ] `shared/egg_git/README.md` - Module documentation
- [ ] `scripts/README.md` - Script inventory and usage

### Low Priority (Nice to Have)

- [ ] `docs/reference/gateway-api.md` - REST endpoint reference
- [ ] `docs/reference/environment-variables.md` - Centralized env var documentation
- [ ] `docs/user-guide/README.md` - Day-to-day operations guide

---

## Missing ADRs

- [ ] `docs/adr/not-implemented/ADR-Message-Queue-Slack-Integration.md`
- [ ] `docs/adr/not-implemented/ADR-Slack-Bot-GCP-Integration.md`
- [ ] `docs/adr/not-implemented/ADR-GCP-Deployment-Terraform.md`
- [ ] `docs/adr/not-implemented/ADR-Egg-Repo-Onboarding.md`

---

## Configuration Issues

### Terminology Standardization

- [ ] Decide on "user_mode" vs "incognito" terminology
- [ ] Update `config/repositories.yaml.example` with consistent terminology
- [ ] Update `config/repo_config.py` function names if needed
- [ ] Update `secrets.template.env` variable names if needed

### Port Number Inconsistency - FIXED in PR #65

- [x] `gateway/README.md:19` - Changed port 9847 → 9848
- [x] `gateway/tests/README-integration.md:43,80,104` - Changed port 9847 → 9848
- [x] `gateway/allowed_domains.txt:11,21` - Changed port 9847 → 9848

---

## CI/CD Integration

### Security Linters to Add to Makefile

- [ ] `scripts/check-claude-imports.py` - Add to `make lint`
- [ ] `scripts/check-container-host-boundary.py` - Add to `make lint`
- [ ] `scripts/check-gh-cli-usage.py` - Add to `make lint`
- [ ] `scripts/validate-config.py` - Add to `make lint` or `make test`

### Missing Script

- [ ] `scripts/check-bin-symlinks.py` - Referenced in Makefile but doesn't exist (implement or remove reference)

---

## Test Coverage Gaps

### Gateway Module (Security Critical)

- [ ] Add tests for `gateway/policy.py` (27 KB)
- [ ] Add tests for `gateway/private_repo_policy.py` (18 KB)
- [ ] Add tests for `gateway/github_client.py` (26 KB)
- [ ] Add tests for `gateway/session_manager.py`
- [ ] Add tests for `gateway/rate_limiter.py`
- [ ] Add tests for `gateway/token_refresher.py`

### Sandbox Module

- [ ] Add tests for `sandbox/egg_lib/` modules
- [ ] Add tests for `sandbox/statusbar.py`

### Config Module

- [ ] Add tests for `config/repo_config.py`

---

## Documentation Consistency

- [ ] Decide if `docs/README.md` or `docs/index.md` is primary navigation
- [ ] Update the non-primary one to redirect to the primary
- [ ] Resolve setup.sh vs setup.py references (which is current?)

---

## Progress Tracking

| Category | Total | Fixed | Remaining |
|----------|-------|-------|-----------|
| Vestigial Naming | 12 | 12 | 0 |
| Broken Links | 13 | 6 | 7 |
| Missing Docs | 11 | 1 | 10 |
| Missing ADRs | 4 | 0 | 4 |
| Config Issues | 6 | 3 | 3 |
| CI/CD Integration | 5 | 0 | 5 |
| Test Coverage | 9 | 0 | 9 |
| **Total** | **60** | **22** | **38** |

---

*Last updated: 2026-02-05*
