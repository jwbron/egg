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
├── egg_config/           # Configuration utilities
├── sandbox/              # Sandbox container tests
├── shared/               # Shared library tests (egg_logging, egg_container)
├── gateway/              # Gateway unit tests
├── llm/claude/           # Claude integration tests
├── config/               # Repository config tests
└── tools/                # Tool discovery tests

gateway/tests/            # Gateway-specific tests (~13 files)
├── conftest.py           # Module loader with import rewriting
├── test_gateway.py       # Main gateway API server
├── test_policy.py        # Branch ownership, push policies
├── test_session_manager.py # Session management
└── ...                   # Additional gateway modules

integration_tests/        # Docker-based integration tests (~11 files)
├── conftest.py           # EggStack fixture, Docker helpers
├── docker-compose.yml    # Test stack definition
├── test_network_isolation.py
├── test_credential_security.py
├── test_policy_enforcement.py
└── ...
```

### Test Markers (pytest.ini)

- `integration` — Docker/container-dependent tests
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

### Phase 1: Foundation (20% → 40%)

**Objective**: Establish shared test utilities and cover core unit test gaps.

#### Deliverables

1. **Create `tests/utils/` shared test utilities module**
   - `factories.py` — Factory functions for creating test objects:
     - `make_session()` — Create session with configurable parameters
     - `make_policy_context()` — Create policy evaluation context
     - `make_git_command()` — Create parsed git commands
   - `assertions.py` — Custom assertions for common patterns:
     - `assert_blocked()` — Verify operation was blocked with correct error
     - `assert_allowed()` — Verify operation was permitted
   - `fixtures.py` — Additional pytest fixtures to complement `conftest.py`

2. **Gateway unit test expansion**
   - `policy.py` — Add tests for:
     - Push to main/master blocked for all users
     - Force push blocked unless branch owner
     - PR merge always blocked (gateway-enforced)
     - Branch prefix validation (`egg/` required)
   - `session_manager.py` — Add tests for:
     - Token uniqueness validation
     - IP binding enforcement
     - Session expiration
     - Invalid launcher secret rejection
     - Concurrent session limits

3. **Sandbox unit test expansion**
   - `entrypoint.py` — Process lifecycle, environment setup
   - Git/gh wrapper routing verification
   - Network mode detection (private vs public)

#### Coverage Targets by Module (Phase 1)

| Module | Current Est. | Phase 1 Target |
|--------|--------------|----------------|
| `gateway/policy.py` | 60% | 90% |
| `gateway/session_manager.py` | 50% | 85% |
| `sandbox/entrypoint.py` | 30% | 60% |
| `shared/egg_config/` | 70% | 85% |

---

### Phase 2: Functional Testing Framework (40% → 50%)

**Objective**: Build Docker-based functional tests for integration points.

#### Deliverables

1. **Create `tests/functional/` directory**
   - Dedicated Docker Compose templates for isolated testing
   - Faster startup than full `integration_tests/` stack
   - Focus on component pairs rather than full system

2. **Git/GH wrapper functional tests**
   - Test actual command interception and routing
   - Verify credential helper integration
   - Validate error messages match gateway responses

3. **Network isolation functional tests**
   - Container-to-gateway communication
   - Proxy enforcement in private mode
   - DNS resolution in both modes

4. **Session lifecycle functional tests**
   - Create → heartbeat → delete flow
   - Session timeout behavior
   - Stale session cleanup

#### Test Infrastructure Additions

```python
# tests/functional/conftest.py

@pytest.fixture(scope="module")
def minimal_gateway():
    """Lightweight gateway container for functional tests.

    Unlike egg_stack (session-scoped, full stack), this is:
    - Module-scoped for faster test isolation
    - Minimal config (no proxy, no squid)
    - Faster startup (~5s vs ~30s)
    """
    ...

@pytest.fixture
def git_command_tester(minimal_gateway):
    """Factory for testing git command handling."""
    def _test(command: list[str], expected_result: str) -> None:
        ...
    return _test
```

---

### Phase 3: Security Test Suite (60% → 70%)

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
    # Should either accept valid names or reject with clear error
    ...

@given(command=st.lists(st.text(), min_size=1, max_size=20))
def test_git_command_parsing_never_crashes(command: list[str]):
    """Parser should handle arbitrary input without exceptions."""
    ...
```

#### Security Test Categories

| Category | Tests | Priority |
|----------|-------|----------|
| Credential isolation | env var sanitization, log redaction | P0 |
| Policy bypass | injection, encoding, race conditions | P0 |
| Session security | token validation, IP binding, expiry | P0 |
| Network isolation | DNS bypass, proxy enforcement | P1 |
| Input validation | fuzzing parsers, edge cases | P1 |

---

### Phase 4: Comprehensive Coverage (70% → 80%+)

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

### Milestone 1: Test Utilities & Gateway Coverage (→ 40%)

**Work Items**:
1. Create `tests/utils/` module structure
2. Implement factory functions for sessions, policies, commands
3. Add missing `policy.py` unit tests (push/force-push/merge blocking)
4. Add missing `session_manager.py` unit tests (token/IP/expiry)
5. Update CI threshold: 20% → 40%

**Success Criteria**:
- `make test` passes at 40% threshold
- `policy.py` at 90%+ coverage
- `session_manager.py` at 85%+ coverage

---

### Milestone 2: Functional Test Framework (→ 50%)

**Work Items**:
1. Create `tests/functional/` structure with lightweight fixtures
2. Implement git wrapper command tests
3. Implement session lifecycle tests
4. Document functional test patterns in this file

**Success Criteria**:
- Functional tests run in under 60 seconds
- Git/gh wrapper behavior fully covered
- Update CI threshold: 40% → 50%

---

### Milestone 3: Security Test Suite (→ 60%)

**Work Items**:
1. Create `tests/security/` structure
2. Implement credential isolation tests
3. Implement policy bypass tests
4. Add hypothesis-based fuzzing
5. Document security test requirements

**Success Criteria**:
- All P0 security tests passing
- Fuzzing covers parsers and validators
- Update CI threshold: 50% → 60%

---

### Milestone 4: Coverage Completion (→ 80%)

**Work Items**:
1. Systematic error path coverage
2. Concurrency and race condition tests
3. Edge case enumeration
4. Integration test expansion
5. Final CI threshold update: 60% → 80%

**Success Criteria**:
- 80%+ overall coverage
- 95%+ on `policy.py` and `session_manager.py`
- All security tests passing
- No flaky tests in CI

---

## Directory Structure (Final State)

```
tests/
├── utils/               # NEW: Shared test utilities
│   ├── __init__.py
│   ├── factories.py     # Object creation helpers
│   ├── assertions.py    # Custom assertions
│   └── fixtures.py      # Additional pytest fixtures
├── functional/          # NEW: Docker-based component tests
│   ├── conftest.py      # Lightweight gateway fixture
│   ├── test_git_wrappers.py
│   ├── test_session_lifecycle.py
│   └── test_network_modes.py
├── security/            # NEW: Security-focused tests
│   ├── conftest.py
│   ├── test_credential_isolation.py
│   ├── test_policy_bypass.py
│   └── test_input_fuzzing.py
├── sandbox/             # Existing: Sandbox unit tests
├── egg_config/          # Existing: Config tests
├── gateway/             # Existing: Gateway unit tests
├── shared/              # Existing: Shared library tests
├── conftest.py          # Root fixtures
└── ...

gateway/tests/           # Gateway-specific tests (keep separate)
integration_tests/       # Full E2E tests (existing)
├── local_pipeline/      # Local orchestrator integration tests
│   ├── helpers.py       # Shared API helpers
│   ├── test_api_validation.py
│   ├── test_concurrent_pipelines.py
│   ├── test_error_recovery.py
│   ├── test_hitl_edge_cases.py
│   ├── test_local_pipeline.py
│   ├── test_signals.py
│   └── test_unified_pipeline_behavior.py
```

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

### Coverage Reporting

Add per-module coverage thresholds for security-critical code:

```toml
# pyproject.toml
[tool.coverage.report]
fail_under = 80

[tool.coverage.html]
directory = "htmlcov"

# Future: per-file thresholds when coverage.py supports it
# policy.py: 95%
# session_manager.py: 95%
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

### Security Test Requirements

All security tests must:
1. Document the attack vector being tested
2. Include a "defense validation" assertion
3. Be tagged with `@pytest.mark.security`
4. Reference relevant CWE/OWASP categories where applicable

---

## References

- [Issue #166](https://github.com/jwbron/egg/issues/166) — Original coverage improvement request
- [PR #54](https://github.com/jwbron/egg/pull/54) — jib→egg extraction with initial threshold
- [CONTRIBUTING.md](../../CONTRIBUTING.md) — Coverage requirements
- [pytest-cov documentation](https://pytest-cov.readthedocs.io/)
- [hypothesis documentation](https://hypothesis.readthedocs.io/)
