# BRC Memory — simplifier — issue-3630-laguna-run7

## Role
simplifier

## Pipeline
issue-3630-laguna-run7

## Phase
refine

## Issue
#3630: `validate_checks` in `shared/egg_config/validators.py` silently drops or
`str()`-coerces malformed check `fix` values.

## Analysis

### Problem
The `validate_checks` function (canonical in `shared/egg_config/validators.py`,
with import-fallback copies in `config/repo_config.py` and
`orchestrator/routes/pipelines/__init__.py`) handles the optional `fix` key with:

```python
if c.get("fix"):
    entry["fix"] = str(c["fix"])
```

This has two operator-hostile behaviors:

1. **Silent drop on falsy values**: `fix: ""`, `fix: false`, `fix: 0`, `fix: null`
   are all dropped with no warning and no log line. The operator sees a configured
   `fix:` in repositories.yaml and a green gate that never self-heals, with nothing
   explaining why.

2. **`str()` coercion of non-strings**: a YAML list such as
   `fix: [make fmt, make lint-fix]` is coerced to the literal string
   `"['make fmt', 'make lint-fix']"` and handed to the check runner as a shell
   command, failing as a subprocess with a message that does not point back at
   the config.

### Proposed Fix
Validate that `fix`, when present (key exists in the dict), is a non-empty string.
Log a WARNING when it is not, so the config error surfaces at load time.

New logic:
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

### Files Changed
1. `shared/egg_config/validators.py` — canonical `validate_checks`: added logger,
   replaced `if c.get("fix"): entry["fix"] = str(c["fix"])` with validated block.
2. `config/repo_config.py` — fallback `validate_checks`: aligned `fix` handling
   (already has logger).
3. `orchestrator/routes/pipelines/__init__.py` — fallback `validate_checks`:
   aligned `fix` handling (uses module-level `logger`).
4. `tests/egg_config/test_validators.py` — updated `test_values_coerced_to_strings`
   (removed `fix: 3` since fix is no longer str-coerced), replaced
   `test_empty_fix_dropped` with 8 new tests covering all cases.

### Test Coverage
- `test_fix_non_empty_string_accepted` — valid string retained, no warning
- `test_fix_empty_string_rejected_with_warning` — `""` dropped + warning
- `test_fix_none_rejected_with_warning` — `None` dropped + warning
- `test_fix_false_rejected_with_warning` — `False` dropped + warning
- `test_fix_zero_rejected_with_warning` — `0` dropped + warning
- `test_fix_non_string_rejected_with_warning` — `3` (int) dropped + warning
- `test_fix_list_rejected_with_warning` — list dropped + warning (not str-coerced)
- `test_fix_absent_key_unchanged` — absent key: no entry, no warning

### Scope
- Only the `fix` key handling is changed. `full_command` handling is left as-is
  per the issue's "keep tightly scoped" directive.
- The `name` and `command` keys are still coerced with `str()` (unchanged).
- Downstream consumer `slice_green_gate.py:557` does `fix_cmd = check.get("fix")`
  and `if rc != 0 and fix_cmd:` — compatible with the new behavior (fix is either
  a non-empty string or absent).

### Test Results
All 63 tests in `tests/egg_config/test_validators.py` pass.
All 131 non-git-dependent tests in `orchestrator/tests/test_propose_check_gate.py` pass.
Ruff lint and format checks pass.

### Phase Constraint
Currently in `refine` phase. Code changes to source files cannot be committed
during this phase (gateway policy: refine phase only allows `.egg-state/` paths).
The code changes are made in the working directory and will be committed during
the `implement` phase. This proposal documents the proposed changes for review.
