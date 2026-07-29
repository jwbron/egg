# Plan: Fix #3630 — validate_checks fix key validation

## Problem
`validate_checks` in `shared/egg_config/validators.py` silently drops falsy `fix`
values (`""`, `False`, `0`) and `str()`-coerces non-string values (e.g. YAML lists)
into broken shell commands. Operators see a configured `fix:` in repositories.yaml
with a green gate that never self-heals, and no explanation why.

## Proposed Implementation

### 1. `shared/egg_config/validators.py` (canonical)
- Add `import logging` and `logger = logging.getLogger(__name__)`
- Replace `if c.get("fix"): entry["fix"] = str(c["fix"])` with:
  ```python
  if "fix" in c:
      fix = c["fix"]
      if isinstance(fix, str) and fix:
          entry["fix"] = fix
      else:
          logger.warning(
              "check %r: fix must be a non-empty string, got %s %r; dropping fix",
              c.get("name"),
              type(fix).__name__,
              fix,
          )
  ```
- Update docstring to document the new validation behavior

### 2. `config/repo_config.py` (ImportError fallback)
- Align the fallback `validate_checks` with the same validation logic
- Update docstring

### 3. `orchestrator/routes/pipelines/__init__.py` (ImportError fallback)
- Align the inline fallback `validate_checks` with the same validation logic

### 4. `tests/egg_config/test_validators.py`
- Update `test_values_coerced_to_strings` (remove `fix: 3` from coercion test)
- Replace `test_empty_fix_dropped` with 8 comprehensive tests:
  - Valid non-empty string accepted (no warning)
  - Empty string rejected with warning
  - `None` rejected with warning
  - `False` rejected with warning
  - `0` rejected with warning
  - Non-string int rejected with warning
  - List rejected with warning (no `str()` coercion)
  - Absent key unchanged (no warning)

## Test Plan
- Run `tests/egg_config/test_validators.py` — expect 63 passed
- Run `orchestrator/tests/test_propose_check_gate.py` (ValidateChecks/GateChecks) — expect 9 passed
- Run lint and format checks

## Risk Assessment
- **Low risk**: The change only affects the `fix` key validation. `name` and `command`
  coercion is unchanged. `full_command` handling is unchanged (out of scope).
- **Breaking change**: Configs that relied on `str()` coercion of non-string `fix`
  values (e.g. YAML lists) will now get a warning and the fix will be dropped. But the
  coercion produced invalid shell commands that failed at runtime, so this is a strict
  improvement.
- **False positive risk**: String `"0"` is non-empty and truthy, so it is correctly
  retained. Only actual falsy values (`""`, `False`, `0`, `None`) and non-strings
  (lists, ints, etc.) are rejected.

## Design Decisions
1. **None handling**: `fix: null` in YAML produces `None`. Since the key IS present
   but not a non-empty string, we warn and drop it (consistent with the issue's
   requirement: "validate that fix, when present, is a non-empty string").
2. **full_command**: Out of scope per the issue directive. Left unchanged.
3. **name/command**: Out of scope per the issue directive. Left unchanged.
4. **Logging**: Uses `logging.getLogger(__name__)` matching the pattern in
   `config/repo_config.py`.
