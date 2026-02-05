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
   - Creates a virtual environment with all dependencies (via uv)
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
| `make setup` | Install dependencies and pre-commit hooks | Run once after cloning |
| `make lint` | Run all linters | Ruff, shellcheck, yamllint, hadolint, custom checks |
| `make lint-fix` | Auto-fix lint issues | Ruff format, shfmt, YAML whitespace |
| `make test` | Run all tests | Main tests + gateway tests |
| `make security` | Run security scan | Bandit |
| `make ci` | Run full CI pipeline | Via act, same as GitHub Actions |
| `make ci-lint` | Run lint job | Via act |
| `make ci-test` | Run unit test job | Via act |

Run `make help` for the full list of targets.

### CI/Local Parity

**GitHub Actions workflows are the single source of truth.** Use `make ci` to run the full CI pipeline locally via [act](https://github.com/nektos/act), guaranteeing that what passes locally will pass in CI.

For fast iteration, native targets (`make lint`, `make test`) run checks directly without Docker overhead.

### Code Style

- **Python**: We use ruff for linting and formatting
- **Type hints**: Required for all public APIs
- **Docstrings**: Required for all modules and public functions
- **Line length**: 100 characters max

The pre-commit hooks will automatically check and fix most style issues.

### Testing

- **Unit tests**: `tests/` - Fast, isolated tests
- **Gateway tests**: `gateway/tests/` - Gateway-specific tests
- **Integration tests**: `tests/integration/` - Tests requiring Docker/containers

Coverage requirements:
- Minimum 80% overall coverage
- 95%+ for security-critical code (policy.py, session_manager.py)

Run tests:
```bash
# All tests
make test

# Via act (CI parity)
make ci-test

# Specific test file
.venv/bin/pytest tests/test_python_syntax.py -v
```

## Pull Request Process

1. **Create a branch** from main
2. **Make your changes** with clear, focused commits
3. **Run the full CI locally**: `make ci`
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
