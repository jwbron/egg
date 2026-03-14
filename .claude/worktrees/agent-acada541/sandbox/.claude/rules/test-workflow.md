# Test Workflow

**Check for test infrastructure before assuming test commands.** Look for Makefile targets, pytest config, jest config, etc.

1. Run existing tests before changes (baseline)
2. Run tests frequently during development
3. Follow existing test patterns (framework, naming, structure)
4. Ensure tests pass before PR

| Framework | Run All | Run Specific |
|-----------|---------|--------------|
| pytest | `pytest` | `pytest test_file.py` |
| jest | `npm test` | `jest file.test.js` |
| make | `make test` | varies |

**Test failures**: Fix if your changes caused them. Document pre-existing failures in PR.
