# Plan: Fix validate_checks silently dropping/str()-coercing malformed fix values (#3630)

## Problem

`validate_checks` in `shared/egg_config/validators.py` handles the optional `fix` key with:

```python
if c.get("fix"):
    entry["fix"] = str(c["fix"])
```

Two operator-hostile behaviors:

1. **Silent drop on falsy values**: `fix: ""`, `fix: false`, `fix: 0` are dropped with no warning.
2. **`str()` coercion of non-strings**: A YAML list like `fix: [make fmt, make lint-fix]` is coerced to the literal string `"['make fmt', 'make lint-fix']"` and handed to the shell as a command, failing with an opaque subprocess error.

## Fix

Validate that `fix`, when present (key exists in the dict), is a non-empty string. Log a warning when it is not, so config errors surface at load time.

### Implementation

Replace:
```python
if c.get("fix"):
    entry["fix"] = str(c["fix"])
```

With:
```python
if "fix" in c:
    fix = c["fix"]
    if isinstance(fix, str) and fix:
        entry["fix"] = fix
    else:
        logger.warning(
            "validate_checks: check %r has invalid fix %r "
            "(expected non-empty string); dropping fix",
            c.get("name"),
            fix,
        )
```

### Files to change

1. **`shared/egg_config/validators.py`** (primary):
   - Add `import logging` and module-level `logger = logging.getLogger("egg_config.validators")`
   - Replace `fix` handling in `validate_checks` with validation + warning
   - Update docstring

2. **`config/repo_config.py`** (parallel fallback):
   - Align the fallback `validate_checks` (inside `except ImportError`) with the same fix
   - Use the existing module-level `logger`
   - Update docstring

3. **`orchestrator/routes/pipelines/__init__.py`** (parallel fallback):
   - Align the fallback `validate_checks` (inside `except ImportError`) with the same fix
   - Use the existing module-level `logger`

4. **`tests/egg_config/test_validators.py`**:
   - Update `test_values_coerced_to_strings`: remove `fix: 3` (now rejected, not coerced)
   - Update `test_empty_fix_dropped` → `test_empty_fix_dropped_with_warning`: also assert warning logged
   - Add `test_fix_false_rejected_with_warning`
   - Add `test_fix_zero_rejected_with_warning`
   - Add `test_fix_non_string_rejected_with_warning`
   - Add `test_fix_list_rejected_with_warning`
   - Add `test_fix_absent_unchanged`

### Scope

- Only `fix` key handling changed. `full_command` handling left untouched (same `str()` coercion pattern retained, per "tightly scoped to the fix key handling" directive).
- No refactoring of surrounding validators.

## Test Plan

- `tests/egg_config/test_validators.py`: 61 tests pass (including 5 new + 2 updated)
- `orchestrator/tests/test_propose_check_gate.py::TestValidateChecksFullCommand` + `TestGateChecks`: 9 tests pass
- `ruff check` on all 4 modified files: All checks passed

## Risks

- Low: The change only affects the `fix` key handling. Entries with invalid `fix` values are now dropped with a warning instead of being silently dropped or `str()`-coerced. This is strictly an improvement in observability.
- The `full_command` key retains the same `str()` coercion behavior — this is intentional per scope notes.

```yaml
# yaml-tasks
pr:
  title: "Fix validate_checks silently dropping/str()-coercing malformed fix values (#3630)"
  description: |
    validate_checks used `if c.get("fix"): entry["fix"] = str(c["fix"])` which
    silently dropped falsy fix values (empty string, false, 0) and str()-coerced
    non-string values (e.g. YAML lists) into broken shell commands. Fix: validate
    that fix, when present, is a non-empty string; log a warning when it is not.
    Aligned parallel fallbacks in config/repo_config.py and orchestrator pipelines.
  test_plan: |
    - Automated: 61 tests in tests/egg_config/test_validators.py pass (5 new + 2 updated)
    - Automated: 9 tests in orchestrator/tests/test_propose_check_gate.py pass
    - Automated: ruff check passes on all 4 modified files
  manual_steps: |
    Pre-merge: None
    Post-merge: Monitor config load logs for new "invalid fix" warnings
phases:
  - id: 1
    name: Implement
    goal: "Validate fix key as non-empty string with warning in validate_checks"
    tasks:
      - id: task-1-1
        description: "In shared/egg_config/validators.py, add import logging and module-level logger; replace fix handling in validate_checks to check 'fix' in c, validate isinstance(fix, str) and fix, log warning on invalid; update docstring."
        acceptance: "validate_checks logs a warning and drops fix when it is not a non-empty string; valid non-empty string fix is retained"
        files:
          - shared/egg_config/validators.py
      - id: task-1-2
        description: "In config/repo_config.py, align the fallback validate_checks (inside except ImportError) with the same fix validation and warning using the module-level logger; update docstring."
        acceptance: "Fallback validate_checks in repo_config.py validates fix as non-empty string with warning"
        files:
          - config/repo_config.py
      - id: task-1-3
        description: "In orchestrator/routes/pipelines/__init__.py, align the fallback validate_checks (inside except ImportError) with the same fix validation and warning using the module-level logger."
        acceptance: "Fallback validate_checks in pipelines __init__.py validates fix as non-empty string with warning"
        files:
          - orchestrator/routes/pipelines/__init__.py
      - id: task-1-4
        description: "In tests/egg_config/test_validators.py, update test_values_coerced_to_strings (remove fix:3 coercion), update test_empty_fix_dropped to test_empty_fix_dropped_with_warning (assert warning), add test_fix_false_rejected_with_warning, test_fix_zero_rejected_with_warning, test_fix_non_string_rejected_with_warning, test_fix_list_rejected_with_warning, test_fix_absent_unchanged."
        acceptance: "All tests pass: valid string accepted, empty/false/0/list/non-string rejected with warning, absent key unchanged"
        files:
          - tests/egg_config/test_validators.py
```
