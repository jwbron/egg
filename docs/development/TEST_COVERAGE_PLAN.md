# Test Coverage Improvement Plan

This document outlines the strategy for increasing test coverage from 20% to 80%+, as discussed in [Issue #166](https://github.com/jwbron/egg/issues/166).

## Current State

| Metric | Value |
|--------|-------|
| Coverage threshold (CI) | 20% |
| Stated goal (CONTRIBUTING.md) | 80% overall, 95% for security-critical |
| Modules tracked | `gateway`, `shared`, `sandbox` |

### Existing Test Infrastructure

```
tests/                    # Unit tests (~29 files)
├── conftest.py           # Shared fixtures (temp_dir, mock_home, mock_env)
├── utils/                # Phase 1: Shared test utilities
│   ├── factories.py      # Factory functions for test objects
│   ├── assertions.py     # Custom assertions
│   └── fixtures.py       # Additional pytest fixtures
├── functional/           # Phase 2: Docker-based functional tests
│   ├── conftest.py       # Lightweight gateway fixture
│   ├── test_git_wrappers.py
│   ├── test_session_lifecycle.py
│   └── test_network_modes.py
├── sandbox/              # Sandbox container tests
├── shared/               # Shared library tests
└── gateway/              # Gateway unit tests

gateway/tests/            # Gateway-specific tests (~13 files)
├── conftest.py           # Module loader with import rewriting
└── ...

integration_tests/        # Docker-based integration tests (~11 files)
├── conftest.py           # EggStack fixture, Docker helpers
├── docker-compose.yml    # Test stack definition
└── ...
```

### Test Markers (pytest.ini)

- `integration` — Docker/container-dependent tests (full stack)
- `functional` — Docker-based component tests (lightweight)
- `e2e` — Requires real API keys (ANTHROPIC_OAUTH_TOKEN)
- `security` — Security/pentesting tests
- `agent_flaky` — Non-deterministic agent behavior (non-blocking in CI)

---

## Guiding Principles

1. **Minimize mocking, maximize real code** — Mock only at true system boundaries (GitHub API, Anthropic API). Test actual policy logic, session management, and git command handling with real code paths.

2. **Layered test architecture** — Unit tests for isolated logic → Docker-based functional tests for component integration → E2E tests for full stack validation.

3. **Reusable test frameworks** — Centralized fixtures, factory functions, and Docker helpers to reduce boilerplate and ensure consistency.

4. **Security-critical code gets extra scrutiny** — `policy.py` and `session_manager.py` require 95%+ coverage with explicit attack vector testing.

---

## Phased Approach

### Phase 1: Foundation (20% → 40%) ✅ COMPLETE

**Objective**: Establish shared test utilities and cover core unit test gaps.

#### Deliverables

1. **Created `tests/utils/` shared test utilities module**
   - `factories.py` — Factory functions for creating test objects
   - `assertions.py` — Custom assertions for common patterns
   - `fixtures.py` — Additional pytest fixtures

2. **Gateway unit test expansion**
   - `policy.py` — Tests for push/force-push/merge blocking
   - `session_manager.py` — Tests for token/IP/expiry validation

3. **Sandbox unit test expansion**
   - `entrypoint.py` — Process lifecycle, environment setup

---

### Phase 2: Functional Testing Framework (40% → 50%) ✅ COMPLETE

**Objective**: Build Docker-based functional tests for integration points.

#### Deliverables

1. **Created `tests/functional/` directory**
   - `conftest.py` — Lightweight `minimal_gateway` fixture (module-scoped)
   - Faster startup than full `integration_tests/` stack (~5-10s vs ~30s)
   - Focus on component pairs rather than full system

2. **Git/GH wrapper functional tests** (`test_git_wrappers.py`)
   - Command interception and routing validation
   - Allowlist enforcement for git operations
   - Network operation redirection to dedicated endpoints
   - Argument validation and sanitization
   - Error message quality verification

3. **Session lifecycle functional tests** (`test_session_lifecycle.py`)
   - Create → heartbeat → delete flow
   - Token uniqueness and isolation
   - Duplicate container ID handling
   - Authentication requirements

4. **Network mode functional tests** (`test_network_modes.py`)
   - Private vs public mode creation
   - Mode validation and defaults
   - Mode isolation between sessions

#### Test Infrastructure Additions

```python
# tests/functional/conftest.py

@pytest.fixture(scope="module")
def minimal_gateway():
    """Lightweight gateway container for functional tests.

    Unlike egg_stack (session-scoped, full stack), this is:
    - Module-scoped for faster test isolation
    - Single container (no docker-compose)
    - Faster startup (~5-10s vs ~30s)
    """
    ...

@pytest.fixture
def git_command_tester(minimal_gateway, functional_session):
    """Factory for testing git command handling."""
    def _test(operation: str, *, args: list[str] = None) -> GitCommandResult:
        ...
    return _test

@pytest.fixture
def session_lifecycle_tester(minimal_gateway):
    """Factory for testing session lifecycle operations."""
    def _test(operation: str, *, token: str = None) -> dict:
        ...
    return _test
```

---

### Phase 3: Security Test Suite (50% → 60%)

**Objective**: Systematic security testing with attack vector coverage.

#### Deliverables

1. **Create `tests/security/` directory**
   - Organized by attack category (injection, bypass, escalation)
   - Property-based testing with hypothesis for fuzzing
   - Explicit CVE/CWE mapping in test docstrings

2. **Credential isolation tests**
   - Environment variables sanitized before sandbox entry
   - Credentials never appear in logs or error messages
   - Token refresh doesn't leak to sandbox

3. **Policy bypass attempt tests**
   - Command injection in git arguments
   - Unicode/encoding tricks in branch names
   - Race conditions in session validation
   - Header injection attempts

4. **Input fuzzing with hypothesis**

```python
# tests/security/test_input_fuzzing.py
from hypothesis import given, strategies as st

@given(branch_name=st.text(min_size=1, max_size=100))
def test_branch_name_sanitization(branch_name: str):
    """Verify all branch names are properly sanitized."""
    ...

@given(command=st.lists(st.text(), min_size=1, max_size=20))
def test_git_command_parsing_never_crashes(command: list[str]):
    """Parser should handle arbitrary input without exceptions."""
    ...
```

---

### Phase 4: Comprehensive Coverage (60% → 80%+)

**Objective**: Fill remaining gaps with systematic coverage.

#### Deliverables

1. **Error path coverage**
   - All exception handlers exercised
   - Error messages validated
   - Graceful degradation paths

2. **Concurrency testing**
   - Parallel session creation
   - Race conditions in state updates
   - Deadlock detection

3. **Edge case enumeration**
   - Empty inputs
   - Maximum length inputs
   - Special characters
   - Timezone/locale variations

4. **Integration test expansion**
   - Additional E2E workflows
   - Failure recovery scenarios
   - Performance regression tests

---

## Implementation Roadmap

### Milestone 1: Test Utilities & Gateway Coverage (→ 40%) ✅

**Status**: Complete (PR #181)

### Milestone 2: Functional Test Framework (→ 50%) ✅

**Status**: Complete (this PR)

**Work Items**:
1. ✅ Create `tests/functional/` structure with lightweight fixtures
2. ✅ Implement git wrapper command tests
3. ✅ Implement session lifecycle tests
4. ✅ Implement network mode tests
5. ⏳ Update CI threshold: 40% → 50% (follow-up PR after functional tests verified)

---

### Milestone 3: Security Test Suite (→ 60%)

**Work Items**:
1. Create `tests/security/` structure
2. Implement credential isolation tests
3. Implement policy bypass tests
4. Add hypothesis-based fuzzing
5. Document security test requirements

---

### Milestone 4: Coverage Completion (→ 80%)

**Work Items**:
1. Systematic error path coverage
2. Concurrency and race condition tests
3. Edge case enumeration
4. Integration test expansion
5. Final CI threshold update: 60% → 80%

---

## CI/CD Integration

### Workflow Updates

**`.github/workflows/test.yml`**:

```yaml
# Phase 1: 40%
--cov-fail-under=40

# Phase 2: 50%
--cov-fail-under=50

# Phase 3: 60%
--cov-fail-under=60

# Phase 4 (final): 80%
--cov-fail-under=80
```

### Running Functional Tests

```bash
# Run all functional tests
pytest tests/functional/ -v -m functional

# Run with coverage
pytest tests/functional/ -v --cov=gateway --cov=shared --cov-report=term-missing

# Run specific test file
pytest tests/functional/test_git_wrappers.py -v
```

---

## Testing Best Practices

### Do

- Test behavior, not implementation
- Use descriptive test names: `test_push_to_main_blocked_for_all_users`
- Include docstrings explaining test rationale
- Use parametrized tests for similar cases
- Clean up resources in fixtures

### Don't

- Mock internal implementation details
- Share state between tests
- Use `time.sleep()` in unit tests (use proper waits in integration tests)
- Ignore flaky tests — fix root causes

---

## References

- [Issue #166](https://github.com/jwbron/egg/issues/166) — Original coverage improvement request
- [PR #181](https://github.com/jwbron/egg/pull/181) — Phase 1 implementation
- [CONTRIBUTING.md](../../CONTRIBUTING.md) — Coverage requirements
- [pytest-cov documentation](https://pytest-cov.readthedocs.io/)
- [hypothesis documentation](https://hypothesis.readthedocs.io/)
