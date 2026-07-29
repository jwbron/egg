# BRC Memory — reviewer_code — issue #3630 — laguna-run7

## Role: reviewer_code

## Current state
- Phase: implement
- Verdicts:
  - documenter v1 (e07498830): NACKED — None incorrectly grouped with absent key as "silently omitted"
  - documenter v2 (89117f58d): ACKED — None correctly grouped with rejected values; absent key correctly separated
  - coder v1 (89117f58d): ACKED — code fix is correct, tightly scoped, all tests pass

## Summary of assessment

### Code fix (shared/egg_config/validators.py, lines 214-224)
Replaced `if c.get("fix"): entry["fix"] = str(c["fix"])` with:
```python
if "fix" in c:
    fix = c["fix"]
    if isinstance(fix, str) and fix.strip():
        entry["fix"] = fix
    else:
        logger.warning(
            "validate_checks: check %r has invalid fix %r "
            "(expected non-empty string); dropping fix",
            c.get("name"),
            fix,
        )
```
- `fix.strip()` rejects whitespace-only while storing verbatim
- Non-strings (list, dict, int, bool, None) hit else → warning + drop
- Absent key → no action, silently omitted
- Added `import logging` + module-level logger

### Parallel implementations aligned
- config/repo_config.py: same fix block (fallback)
- orchestrator/routes/pipelines/__init__.py: same fix block + full_command added

### Tests (118 pass)
- test_validators.py: 14 tests (valid, empty, None, false, 0, int, list, whitespace, absent, verbatim)
- test_validate_checks_parity.py: 55 tests (3 copies × 9 bad_fix values + absent + full_command + AST-identical)

### Other artifacts
- config/repositories.yaml.example: comment updated
- docs/guides/sdlc-pipeline.md: documentation accurate (ACKed as documenter v2)

### Issue requirements
- fix validated as non-empty string when present ✓
- warning logged in same shape as other validate_* rejections ✓
- parallel fix path in repo_config.py aligned ✓
- unit tests cover all required cases ✓
- scoped to fix key only, full_command left as-is ✓
- #3629 schema gap closed ✓

## Files reviewed
- shared/egg_config/validators.py
- config/repo_config.py
- orchestrator/routes/pipelines/__init__.py
- tests/egg_config/test_validators.py
- tests/egg_config/test_validate_checks_parity.py
- config/repositories.yaml.example
- docs/guides/sdlc-pipeline.md

## Status: COMPLETE — both documenter v2 and coder v1 ACKED
Awaiting other reviewers (fully_acked: false).
