# Analysis: Fix #3630 — validate_checks silently drops/str-coerces malformed fix values

## Summary
`validate_checks` in `shared/egg_config/validators.py` (and its two import-fallback
copies) silently drops falsy `fix` values and `str()`-coerces non-string values,
producing operator-hostile behavior: green gates that never self-heal with no
explanation, or broken shell commands from coerced lists.

## Root Cause
```python
if c.get("fix"):
    entry["fix"] = str(c["fix"])
```
- `c.get("fix")` returns falsy for `""`, `False`, `0`, `None` → silently dropped
- `str(c["fix"])` coerces lists/ints to string repr → broken shell command

## Proposed Change
Replace with explicit validation:
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

## Files to Modify
1. `shared/egg_config/validators.py` (canonical) — add logger, replace fix handling
2. `config/repo_config.py` (fallback) — align fix handling
3. `orchestrator/routes/pipelines/__init__.py` (fallback) — align fix handling
4. `tests/egg_config/test_validators.py` — update tests

## Test Plan
- Valid non-empty string → accepted, no warning
- `""`, `None`, `False`, `0`, int, list → rejected with WARNING
- Absent key → unchanged, no warning

## Risk
Low. The change only affects the `fix` key validation. `name` and `command`
coercion is unchanged. `full_command` handling is unchanged (out of scope).
Downstream consumer `slice_green_gate.py` is compatible (checks `if rc != 0
and fix_cmd:`).
