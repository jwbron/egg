# Testing Guide

## Quick Start

```bash
# First time? Setup installs everything automatically:
./dev setup

# Run tests (same as CI):
./dev test              # Unit tests
./dev test-integration  # Integration tests (requires Docker)
./dev security          # Security scan
./dev ci                # Full CI pipeline

# Fast mode (no Docker overhead):
./dev native test       # Unit tests, runs natively
./dev native lint       # Linting, runs natively
```

## How It Works

**`./dev` runs GitHub Actions workflows locally via act.** This guarantees that what passes locally will pass in CI.

```
./dev test  ->  act -j unit  ->  .github/workflows/test.yml (unit job)
```

## Test Organization

| Directory | Purpose | Command |
|-----------|---------|---------|
| `tests/unit/` | Fast, isolated unit tests | `./dev test` |
| `tests/integration/` | Tests requiring Docker/containers | `./dev test-integration` |
| `tests/security/` | Security-focused tests | `./dev security` |

## Coverage Requirements

| Module | Target | Priority |
|--------|--------|----------|
| `gateway/policy.py` | 95% | Critical (security) |
| `gateway/session_manager.py` | 95% | Critical (security) |
| `gateway/gateway.py` | 85% | High |
| Overall | 80% | Required for CI pass |

## Running Specific Tests

For specific tests, use native mode (faster):

```bash
# Run a specific test file
.venv/bin/pytest tests/unit/test_policy.py -v

# Run tests matching a pattern
.venv/bin/pytest tests/ -k "test_session" -v

# Run with verbose output and no coverage
.venv/bin/pytest tests/unit/ -v --no-cov
```

## Adding New Tests

1. Place unit tests in `tests/unit/test_<module>.py`
2. Place integration tests in `tests/integration/test_<feature>.py`
3. Use fixtures from `tests/conftest.py`
4. Follow existing test patterns in the codebase

## CI/Local Parity

**GitHub Actions workflows are the source of truth.** The `./dev` script runs them locally via act:

| Command | What it runs |
|---------|--------------|
| `./dev lint` | `act -j lint` -> `.github/workflows/lint.yml` |
| `./dev test` | `act -j unit` -> `.github/workflows/test.yml` (unit job) |
| `./dev ci` | `act push` -> All workflows |

If `./dev ci` passes locally, it will pass in GitHub Actions.

## Writing Tests

### Unit Tests

Unit tests should be fast and isolated:

```python
# tests/unit/test_policy.py
import pytest
from gateway.policy import PolicyEngine

def test_branch_ownership_check():
    """Test that branch ownership is correctly validated."""
    engine = PolicyEngine(branch_prefix="egg/")
    assert engine.is_branch_owned("egg/my-feature", "egg-bot")
    assert not engine.is_branch_owned("main", "egg-bot")
```

### Integration Tests

Integration tests may spin up Docker containers:

```python
# tests/integration/test_gateway_api.py
import pytest
import requests

@pytest.fixture
def gateway_container():
    """Start gateway container for testing."""
    # Setup container
    yield container_url
    # Teardown container

def test_health_endpoint(gateway_container):
    """Test gateway health endpoint."""
    response = requests.get(f"{gateway_container}/api/v1/health")
    assert response.status_code == 200
```

### Security Tests

Security tests verify isolation and policy enforcement:

```python
# tests/security/test_credential_isolation.py
def test_sandbox_has_no_credentials():
    """Verify sandbox container has no direct credential access."""
    # Test that credentials are not accessible from sandbox
    pass
```
