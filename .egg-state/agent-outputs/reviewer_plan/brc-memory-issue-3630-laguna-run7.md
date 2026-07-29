# BRC Memory — reviewer_plan — issue-3630-laguna-run7

## Phase: plan

### Verdict: ACK (architect proposal v1)

**Proposal commit:** f8447af786f6f4e6d65a91f86ac82f35233cfd5e

**Review summary:**
Reviewed architect proposal v1 for #3630 (validate check `fix` key is non-empty string).
All claims verified against the live codebase.

**Verified facts:**
1. Three copies of `validate_checks` confirmed with identical buggy pattern:
   - `shared/egg_config/validators.py:203-204` — primary, no logger (needs `import logging` + `logger = logging.getLogger(__name__)`)
   - `config/repo_config.py:387-388` — ImportError fallback, has module-level logger at line 42
   - `orchestrator/routes/pipelines/__init__.py:476-477` — ImportError fallback, has module-level logger at line 339
2. Test `test_values_coerced_to_strings` (line 42-45) needs update — `fix: 3` no longer coerced to `"3"`, now rejected with warning.
3. Test `test_empty_fix_dropped` (line 60-64) needs extension to verify warnings.
4. Recommended logic (`if "fix" in c:` → `isinstance(fix_val, str) and fix_val` → retain; else `logger.warning(...)`) is sound and handles all edge cases correctly.
5. Scope correctly bounded to `fix` key only — `name`, `command`, `full_command` coercion unchanged.
6. Test plan covers all required cases from the issue.
7. Both fallback copies correctly identified and aligned.

**Minor observation (not a blocker):** Orchestrator fallback at `orchestrator/routes/pipelines/__init__.py:468-479` lacks `full_command` handling (pre-existing discrepancy), but out of scope for the `fix` key change.

**ACK recorded at:** 2026-07-29T21:02:00Z
