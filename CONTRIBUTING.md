# Contributing to egg

Thank you for your interest in contributing to egg!

## Development Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/your-org/egg.git
   cd egg
   ```

2. **Run the setup script**
   ```bash
   ./dev setup
   ```

   This automatically:
   - Installs uv (Python package manager) if missing
   - Installs act (GitHub Actions runner) if missing
   - Creates a virtual environment with all dependencies
   - Installs pre-commit hooks

3. **Verify the setup**
   ```bash
   ./dev native lint
   ./dev native test
   ```

## Development Workflow

### Running Commands

The `./dev` script is the primary interface for all development tasks:

| Command | What it does | Notes |
|---------|--------------|-------|
| `./dev setup` | Install all dependencies | Auto-runs on first use |
| `./dev lint` | Run lint workflow | Same as CI (via act) |
| `./dev test` | Run unit tests | Same as CI (via act) |
| `./dev test-integration` | Run integration tests | Same as CI (via act) |
| `./dev ci` | Run full CI pipeline | Same as CI (via act) |
| `./dev native lint` | Run linters natively | Fast mode, no Docker |
| `./dev native test` | Run tests natively | Fast mode, no Docker |

### CI/Local Parity

**GitHub Actions workflows are the single source of truth.** The `./dev` script runs them locally via act, guaranteeing that what passes locally will pass in CI.

### Code Style

- **Python**: We use ruff for linting and formatting
- **Type hints**: Required for all public APIs
- **Docstrings**: Required for all modules and public functions
- **Line length**: 100 characters max

The pre-commit hooks will automatically check and fix most style issues.

### Testing

- **Unit tests**: `tests/unit/` - Fast, isolated tests
- **Integration tests**: `tests/integration/` - Tests requiring Docker/containers
- **Security tests**: `tests/security/` - Security-focused tests

Coverage requirements:
- Minimum 80% overall coverage
- 95%+ for security-critical code (policy.py, session_manager.py)

Run tests:
```bash
# Via act (CI parity)
./dev test

# Native (faster iteration)
./dev native test

# Specific test file
.venv/bin/pytest tests/unit/test_policy.py -v
```

## Pull Request Process

1. **Create a branch** from main
2. **Make your changes** with clear, focused commits
3. **Run the full CI locally**: `./dev ci`
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
