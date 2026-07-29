# Analysis: validate_checks silently drops/coerces malformed `fix` values (#3630)

## Goal

Fix `validate_checks` in `shared/egg_config/validators.py` so that a check's
optional `fix` key — when present — must be a non-empty string. Invalid values
(empty string, `false`, `0`, lists, etc.) are dropped with a warning log line
instead of being silently dropped or `str()`-coerced into broken shell commands.

## Issue summary (from gh issue #3630)

Two operator-hostile behaviors in the current `fix` handling:

1. **Silent drop on falsy values.** `fix: ""`, `fix: false`, `fix: 0` are
   dropped with no warning. The operator sees a configured `fix:` in
   `repositories.yaml` and a green gate that never self-heals, with nothing
   explaining why.
2. **`str()` coercion of non-strings.** A YAML list like
   `fix: [make fmt, make lint-fix]` is coerced to the literal string
   `"['make fmt', 'make lint-fix']"` and handed to the check runner as a shell
   command, failing as a subprocess with a message that doesn't point back at
   the config.

## Codebase analysis

### Primary location: `shared/egg_config/validators.py`

`validate_checks` (lines 167–220) iterates over check entries. Each entry
requires `name` and `command` (both coerced via `str()`). The `fix` key is
handled at lines 203–204 (original):

```python
if c.get("fix"):
    entry["fix"] = str(c["fix"])
```

Problems:
- `c.get("fix")` is falsy for `""`, `False`, `0`, `None` → silently dropped.
- `str(c["fix"])` coerces any truthy non-string (list, dict, int) into a
  string representation that is not a valid shell command.

The `full_command` key (lines 205–206) has the same `c.get("full_command")`
pattern, but the issue scope is explicitly limited to `fix`. `full_command`
is left unchanged.

### Parallel path: `config/repo_config.py`

Lines 359–401 contain a **fallback** `validate_checks` inside a
`try/except ImportError` block. When `egg_config.validators` cannot be
imported (e.g. in environments where the `shared/` package isn't on the
path), this inline copy is used instead. It has the identical `fix` handling
pattern and must be aligned.

The module already has a `logger` (`logging.getLogger("egg.repo_config")`)
available for the warning.

### Callers

- `config/repo_config.py::get_repo_checks` (line 603) calls `validate_checks`
  on checks loaded from `repositories.yaml`.
- `orchestrator/routes/pipelines/__init__.py` (line 465) has its own inline
  copy of `validate_checks` for the propose-time check gate — this one is
  separate and uses a different code path; it is not in scope.
- `orchestrator/tests/test_propose_check_gate.py` has tests that exercise
  `validate_checks` via the shared module — these use string `fix` values and
  are unaffected.

### Existing tests

`tests/egg_config/test_validators.py::TestValidateChecks`:
- `test_values_coerced_to_strings` — tests `fix: 3` coerced to `"3"`. This
  test **must be updated**: `fix: 3` is now rejected with a warning, not
  coerced. `name` and `command` coercion is unchanged.
- `test_empty_fix_dropped` — tests `fix: ""` and `fix: None` are dropped.
  Behavior unchanged (still dropped), but now a warning is logged.

## Proposed approach

### Change 1: `shared/egg_config/validators.py`

1. Add `import logging` and `logger = logging.getLogger(__name__)` at module
   level.
2. Replace the `fix` handling block:

```python
if "fix" in c:
    fix = c["fix"]
    if isinstance(fix, str) and fix:
        entry["fix"] = fix
    else:
        logger.warning(
            "check %r has invalid fix value %r (expected a non-empty "
            "string); dropping fix",
            c.get("name", "<unnamed>"),
            fix,
        )
```

Key behavioral changes:
- Uses `"fix" in c` (key presence) instead of `c.get("fix")` (truthiness), so
  `fix: 0` and `fix: False` are detected as present-but-invalid rather than
  silently absent.
- Validates `isinstance(fix, str) and fix` — must be a string AND non-empty.
- Logs a warning naming the check and the offending value.
- Does NOT `str()`-coerce — non-string values are rejected, not mangled.

### Change 2: `config/repo_config.py` (fallback)

Apply the identical change to the fallback `validate_checks` in the
`except ImportError` block. The module already has `logger` available.

### Change 3: Tests (`tests/egg_config/test_validators.py`)

- Update `test_values_coerced_to_strings`: `fix: 3` is now rejected with a
  warning (not coerced). `name` and `command` are still coerced.
- Update `test_empty_fix_dropped`: behavior unchanged (still dropped), but
  add a warning assertion.
- Add `test_empty_fix_logs_warning`: verifies warning for `fix: ""`.
- Add `test_fix_false_rejected_with_warning`: verifies warning for `fix: False`.
- Add `test_fix_zero_rejected_with_warning`: verifies warning for `fix: 0`.
- Add `test_fix_list_rejected_with_warning`: verifies warning for `fix: [...]`.
- Add `test_fix_valid_string_accepted`: verifies valid string is accepted.
- Add `test_fix_absent_unchanged`: verifies absent `fix` key produces no
  warning and no `fix` in the entry.

## Scope boundaries

- **In scope:** `fix` key validation in both `validate_checks` copies + tests.
- **Out of scope:** `full_command` handling (same pattern but not mentioned in
  the issue); `name`/`command` coercion (unchanged); the inline copy in
  `orchestrator/routes/pipelines/__init__.py` (separate code path, not
  imported from `egg_config.validators`).
- **No decisions needed** — the issue is prescriptive. The only judgment call
  is whether to also fix `full_command`, but the issue explicitly says "keep
  the change tightly scoped to the `fix` key."

## Verification

- `pytest tests/egg_config/test_validators.py` — all tests pass (62 tests).
- `pytest orchestrator/tests/test_propose_check_gate.py::TestValidateChecksFullCommand`
  — confirms `full_command` tests still pass.
- `ruff check` and `mypy` on changed files — clean.
