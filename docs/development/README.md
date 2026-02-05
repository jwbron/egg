# Development Documentation

For contributors and developers working on egg.

## Available Guides

- **[Project Structure](STRUCTURE.md)** - Directory conventions and code organization
- **[Contributing](../../CONTRIBUTING.md)** - Development setup, workflow, testing, and PR process

## Development Setup

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/egg.git
cd egg

# Set up development environment (venv + pre-commit hooks)
make setup

# Verify the setup
make lint
make test
```

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for full setup instructions including `act` installation.

## Key Commands

| Command | What it does |
|---------|--------------|
| `make setup` | Install dependencies and pre-commit hooks |
| `make lint` | Run all linters (via act) |
| `make test` | Run all tests (via act) |
| `make security` | Run security scan (via act) |
| `make ci` | Run full CI pipeline (via act) |
| `make lint-fix` | Auto-fix lint issues (native) |
| `make build` | Build Docker images (gateway + sandbox) |

For direct tool access without act:
```bash
.venv/bin/ruff check .                     # Python lint
.venv/bin/pytest tests/ -v                  # Run tests
.venv/bin/bandit -r gateway shared sandbox  # Security scan
```

## Testing

- **Unit tests**: `tests/` - Fast, isolated tests
- **Gateway tests**: `gateway/tests/` - Gateway-specific tests
- **Integration tests**: `tests/integration/` - Tests requiring Docker

Coverage requirements:
- Minimum 80% overall coverage
- 95%+ for security-critical code (`policy.py`, `session_manager.py`)

## Code Standards

- **Python**: PEP 8, type hints, docstrings, explicit imports
- **Formatting**: ruff (100 char line length)
- **Shell**: shfmt (2-space indent)
- **Config files**: `.yaml` (not `.yml`)

## See Also

- [Architecture](../architecture/) - System design
- [Main README](../../README.md) - Project overview
- [Project Structure](STRUCTURE.md) - Directory layout
