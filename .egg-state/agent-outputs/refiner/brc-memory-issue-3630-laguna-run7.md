# BRC Memory: Issue #3630 — validate_checks fix key validation

## Pipeline
- **Pipeline ID:** issue-3630-laguna-run7
- **Phase:** refine
- **Role:** refiner
- **Current commit:** f9ce6708abb6553491dba70f8e680ca5be30d4c4

## Analysis summary
Issue #3630 asks to fix `validate_checks` in `shared/egg_config/validators.py`
so that the optional `fix` key, when present, must be a non-empty string.
Invalid values (empty string, `false`, `0`, lists) should be dropped with a
warning log, not silently dropped or `str()`-coerced.

## Codebase findings
- Primary: `shared/egg_config/validators.py::validate_checks` (lines 167-220)
- Parallel: `config/repo_config.py` has a fallback `validate_checks` in
  `try/except ImportError` block (lines 359-401) with identical `fix` handling.
  Must be aligned.
- `orchestrator/routes/pipelines/__init__.py` has its own inline copy (line 465)
  — separate code path, not imported from egg_config.validators, out of scope.
- `full_command` key has same pattern but is explicitly out of scope per issue.

## Proposed approach
1. Add `import logging` + `logger` to `shared/egg_config/validators.py`
2. Replace `if c.get("fix"): entry["fix"] = str(c["fix"])` with key-presence
   check + isinstance validation + warning log
3. Apply identical change to `config/repo_config.py` fallback
4. Update tests: `test_values_coerced_to_strings` (fix:3 now rejected),
   `test_empty_fix_dropped` (add warning assertion), add new tests for
   false/0/list/absent/valid-string cases

## Working prototype
Code changes are in the working tree (uncommitted — refine phase blocks
code commits). All 62 tests pass. Ruff and mypy clean on validators.py.

## Decisions
- No HITL decisions registered (issue is prescriptive).
- Candidates considered: full_command scope (not_operator_grade),
  str()-coerce vs reject (not_operator_grade), repo_config.py alignment
  (not_operator_grade).

## Status
- Analysis draft committed at `.egg-state/drafts/issue-3630-laguna-run7-analysis.md`
- BRC proposal sent, awaiting reviewer ACK/NACK.
