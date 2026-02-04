# Test Discovery

**ALWAYS run `discover-tests ~/repos/<repo>` first** - don't assume test commands.

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
