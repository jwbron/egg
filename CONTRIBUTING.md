# Contributing to egg

Thank you for your interest in contributing to egg!

## Development Setup

1. **Fork and clone the repository**
   ```bash
   # First, fork the repo on GitHub, then clone your fork:
   git clone https://github.com/YOUR_USERNAME/egg.git
   cd egg
   ```

2. **Run the setup**
   ```bash
   make setup
   ```

   This:
   - Installs `uv` if not present, then creates a virtual environment with all dependencies
   - Installs pre-commit hooks

3. **Verify the setup**
   ```bash
   make lint
   make test
   ```

## Development Workflow

### Running Commands

The `Makefile` is the single entry point for all development tasks:

| Command | What it does | Notes |
|---------|--------------|-------|
| `make deps` | Install dependencies only (installs `uv` if needed) | Use when you don't need pre-commit hooks |
| `make setup` | Install dependencies + pre-commit hooks | Run once after cloning |
| `make lint` | Run all linters | Same checks as GitHub Actions |
| `make test` | Run all tests | Same checks as GitHub Actions |
| `make security` | Run security scan | Same checks as GitHub Actions |
| `make lint-fix` | Auto-fix lint issues | ruff format, shfmt, YAML whitespace |

Run `make help` for the full list of targets.

### CI/Local Parity

**The Makefile runs the same lint and test commands used in GitHub Actions workflows.** What passes locally will pass in CI — no additional tooling required.

For quick one-off checks, you can also use the venv directly:
```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/test_python_syntax.py -v
```

### Code Style

- **Python**: We use ruff for linting and formatting
- **Type hints**: Required for all public APIs
- **Docstrings**: Required for all modules and public functions
- **Line length**: 100 characters max

The pre-commit hooks will automatically check and fix most style issues.

### Testing

- **Unit tests**: `tests/` - Fast, isolated tests
- **Gateway tests**: `gateway/tests/` - Gateway-specific tests
- **Orchestrator tests**: `orchestrator/tests/` - Orchestrator-specific tests
- **Shared library tests**: `shared/tests/` - Tests for shared packages (egg_anchor, egg_agent, etc.)
- **Integration tests**: `integration_tests/` - Tests requiring k3s/Kubernetes cluster

Coverage requirements:
- Minimum 80% overall coverage
- 95%+ for security-critical code (policy.py, session_manager.py)

Run tests:
```bash
# All tests (full suite — what CI runs)
make test-all

# Changeset-aware (narrows to tests reachable from your diff; the inner-loop default)
make test

# Specific test file
.venv/bin/pytest tests/test_python_syntax.py -v
```

See [`docs/guides/testing.md`](docs/guides/testing.md) for changeset-aware test selection, LKG semantics, and fallback triggers.

## Pull Request Process

1. **Create a branch** from main
2. **Make your changes** with clear, focused commits
3. **Run lint and tests locally**: `make lint && make test`
4. **Create a PR** with a clear description
5. **Address review feedback**

### PR Guidelines

- Keep PRs focused on a single concern
- Include tests for new functionality
- Update documentation as needed
- Ensure all CI checks pass

## Security

If you discover a security vulnerability, please do NOT open a public issue. Instead, please report it privately.

## Questions?

Open an issue for questions about contributing or the codebase.
