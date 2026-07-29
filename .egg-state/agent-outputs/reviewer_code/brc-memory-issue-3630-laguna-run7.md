# BRC Memory — reviewer_code — issue #3630 — laguna-run7

## Role: reviewer_code

## Current state
- Phase: implement
- Producer under review: documenter
- Producer proposal: v1, commit e07498830f3a14f7044c99cbcaee73e457f9163d
- Artifact: docs/guides/sdlc-pipeline.md
- Verdict: NACKED (revision 1)

## Summary of assessment
The documenter's proposal updates the `fix` key documentation paragraph in
docs/guides/sdlc-pipeline.md to reflect the new validate_checks behavior
(#3630). The code fix itself (validators.py, repo_config.py, routes/pipelines/__init__.py)
and its tests are already committed in earlier commits.

### Discrepancy found — NACK reason
The doc states: "A bare `fix:` (None) or an absent `fix` key is treated as
'not configured' and silently omitted from the entry."

This is inaccurate. `None` is NOT silently omitted — it is rejected with a
warning and dropped, same as `""`, `false`, `0`, list, etc. Only the absent
key is silently omitted.

Evidence (all tests pass):
- test_empty_fix_dropped_with_warning: iterates ("", None), asserts "invalid fix"
  in caplog.text for both
- test_invalid_fix_dropped_with_warning[canonical-none / repo_config-none /
  pipelines-none]: parity test includes None in bad_fix parametrize matrix
  across all three copies, asserts warning logged
- test_absent_fix_does_not_warn / test_fix_absent_unchanged: absent key does
  NOT warn

### Requested correction
Group `None` with the other rejected values ("rejected with a warning and
dropped"), not with the absent key. Only an absent `fix` key is silently
omitted.

## Files reviewed
- docs/guides/sdlc-pipeline.md (the proposal)
- shared/egg_config/validators.py (lines 167-228, validate_checks)
- config/repo_config.py (lines 360-406, fallback validate_checks)
- orchestrator/routes/pipelines/__init__.py (lines ~470-497, fallback validate_checks)
- tests/egg_config/test_validators.py (TestValidateChecks)
- tests/egg_config/test_validate_checks_parity.py (TestValidateChecksParity, TestFixBlockIsIdentical)

## Next action
Await documenter revision. Re-review when documenter re-proposes with
corrected None/absent-key distinction.
