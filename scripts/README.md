# Scripts

Validation, lint, and utility scripts for the egg project. These scripts run in CI and locally to enforce code quality, architectural boundaries, and repository conventions.

## Lint Checks

All `check-*.py` scripts are pre-commit / CI lint checks. They exit `0` on success, `1` on failure.

| Script | Purpose |
|--------|---------|
| `check-bin-symlinks.py` | Validate `bin/` symlinks point to existing targets |
| `check-claude-imports.py` | Ensure Claude/Anthropic SDK is not imported in host-services |
| `check-container-host-boundary.py` | Ensure sandbox does not import from host-services |
| `check-container-paths.py` | Detect `sys.path` patterns that fail in container environments |
| `check-docker-and-claude-invocations.py` | Ensure `docker run` and `claude` CLI invocations are explicitly justified |
| `check-gh-cli-usage.py` | Ensure `gh` CLI write operations are not used directly in host-services |
| `check-hardcoded-ports.py` | Detect hardcoded gateway/proxy port numbers |
| `check-llm-api-calls.py` | Ensure LLM API calls only happen inside the sandbox |
| `check-model-versions.py` | Enforce Claude model alias form (no pinned versions) |
| `check-reviewer-job-names.py` | Ensure reviewer workflows use standard job naming conventions |
| `check-workflow-secrets.py` | Detect untrusted script execution with secrets in GitHub Actions |

## Validation Scripts

| Script | Purpose |
|--------|---------|
| `validate-config.py` | Validate egg configuration files and optionally test API connectivity |
| `validate_harness_parity.py` | Compare egg harness vs claude-sdk on identical tasks for parity validation |

## Utility Scripts

| Script | Purpose |
|--------|---------|
| `hello_world.py` | Print "hello world" to stdout |

## Usage

Run any script directly:

```bash
python3 scripts/<script-name>.py
```

Or run all lint checks at once via the Makefile:

```bash
make lint
```

## Tests

Script-specific tests are in `scripts/tests/`.
