# BRC Memory — coder — issue-3630-laguna-run7

## Phase: implement

## Status: PROPOSED at b55945e5 (version 1) — reviewers dispatched

Proposal recorded successfully. Reviewers dispatched: reviewer_code,
reviewer_code_holistic, reviewer_contract, tester, reviewer_security,
reviewer_concurrency.

## Summary of assessment

Fixed `validate_checks` in `shared/egg_config/validators.py` (and two parallel
fallback copies in `config/repo_config.py` and `orchestrator/routes/pipelines/__init__.py`)
to validate that the optional `fix` key, when present, is a non-empty string.

### Change model

**Before:**
```python
if c.get("fix"):
    entry["fix"] = str(c["fix"])
```

**After (current HEAD e07498830):**
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

### Behavior matrix (final)

| `fix` value        | Key present? | Result                          | Warning? |
|--------------------|--------------|---------------------------------|----------|
| `"make lint-fix"`  | yes          | kept as-is (verbatim)           | no       |
| `"  make lint  "`  | yes          | kept as-is (verbatim)           | no       |
| `""`               | yes          | dropped                         | yes      |
| `"   "`            | yes          | dropped (whitespace-only)       | yes      |
| `False`            | yes          | dropped                         | yes      |
| `0`                | yes          | dropped                         | yes      |
| `3` (int)          | yes          | dropped                         | yes      |
| `["make fmt", ...]`| yes          | dropped                         | yes      |
| `None` (YAML null) | yes          | dropped                         | yes      |
| (absent)           | no           | no `fix` key in entry           | no       |

### Design decisions (final, after orchestrator follow-up commits)

- **Whitespace-only rejection**: `fix.strip()` gates the decision (not the stored value),
  so `"   "` is rejected but `"  make lint-fix  "` is kept verbatim. This prevents
  no-op "remediations" that report success without changing anything.
- **None warns**: All non-string/empty values when the key is present warn and are dropped,
  including `None`. Consistent with the issue directive.
- **Logger**: `logging.getLogger(__name__)` (idiomatic).
- **full_command**: Re-synced in all three copies (pipelines fallback was missing it).
- **Parity tests**: `test_validate_checks_parity.py` verifies all three copies are
  AST-identical for the `fix` block and behaviorally identical.
- `full_command` handling retains the same `str()` coercion pattern (out of scope).

### Files changed (current HEAD e07498830)

1. `shared/egg_config/validators.py` — primary fix: added `import logging` + module logger,
   replaced `fix` handling with validation + warning (including `.strip()`), updated docstring.
2. `config/repo_config.py` — aligned the `except ImportError` fallback.
3. `orchestrator/routes/pipelines/__init__.py` — aligned the `except ImportError` fallback,
   including `full_command` handling.
4. `tests/egg_config/test_validators.py` — updated tests, added whitespace-only tests.
5. `tests/egg_config/test_validate_checks_parity.py` — new parity test file verifying
   all three copies agree.
6. `config/repositories.yaml.example` — updated documentation comment.
7. `docs/guides/sdlc-pipeline.md` — updated fix key documentation.

### Test results

- `tests/egg_config/test_validators.py`: 62 passed
- `tests/egg_config/test_validate_checks_parity.py`: 56 passed
- `tests/config/test_repo_config.py`: 51 passed
- ruff: All checks passed
- mypy: `validators.py` clean

### Commit

`e07498830` — "docs: update fix key validation behavior in sdlc-pipeline guide (#3630)"
`b55945e5` — "Update BRC memory for #3630 after orchestrator follow-up commits"
