# Redaction Reference

The redactor (`shared/egg_contracts/redactor.py`) scrubs sensitive data from checkpoint transcripts, tool call outputs, and command logs before they are stored in the `egg/checkpoints/v2` git branch.

## What Is Redacted

The redactor operates on three types of content:

| Content type | Method | Behavior |
|-------------|--------|----------|
| Free text (transcripts, command output) | `redact_text()` | Pattern matching against sensitive value patterns |
| Dictionaries (JSON data, tool results) | `redact_dict()` | Recursive traversal; key-based + value-based redaction |
| Shell commands | `redact_command()` | Environment variable assignments + secret flag patterns + text patterns |
| File paths | `redact_path()` | Path pattern matching against sensitive file patterns |

Redacted values are replaced with `[REDACTED]`.

## Environment Variable Name Patterns

The following environment variable name patterns are considered sensitive. When a key in a dict matches one of these patterns (case-insensitive), its value is replaced regardless of the value's content:

| Pattern | Examples matched |
|---------|----------------|
| `.*_TOKEN$` | `GITHUB_TOKEN`, `AUTH_TOKEN`, `API_TOKEN` |
| `.*_SECRET$` | `CLIENT_SECRET`, `WEBHOOK_SECRET` |
| `.*_KEY$` | `PRIVATE_KEY`, `SSH_KEY` |
| `.*_PASSWORD$` | `DB_PASSWORD`, `ADMIN_PASSWORD` |
| `.*_CREDENTIAL.*` | `CREDENTIAL_JSON`, `AWS_CREDENTIALS` |
| `.*_API_KEY$` | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` |
| `^API_KEY$` | `API_KEY` |
| `^AUTH_TOKEN$` | `AUTH_TOKEN` |
| `^GITHUB_TOKEN$` | `GITHUB_TOKEN` |
| `^ANTHROPIC_API_KEY$` | `ANTHROPIC_API_KEY` |
| `^OPENAI_API_KEY$` | `OPENAI_API_KEY` |
| `^AWS_.*` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| `^GCP_.*` | `GCP_SERVICE_ACCOUNT` |
| `^AZURE_.*` | `AZURE_CLIENT_SECRET` |
| `^DATABASE_URL$` | `DATABASE_URL` |
| `^REDIS_URL$` | `REDIS_URL` |
| `^POSTGRES_.*` | `POSTGRES_PASSWORD`, `POSTGRES_URL` |
| `^MYSQL_.*` | `MYSQL_ROOT_PASSWORD` |
| `^MONGO.*_URI$` | `MONGODB_URI`, `MONGO_URI` |

## Sensitive Value Patterns

These patterns are applied to text content regardless of key name:

| Pattern | Description |
|---------|-------------|
| `sk-[a-zA-Z0-9]{20,}` | OpenAI API keys |
| `sk-ant-[a-zA-Z0-9-]{20,}` | Anthropic API keys |
| `ghp_[a-zA-Z0-9]{36}` | GitHub personal access tokens |
| `gho_[a-zA-Z0-9]{36}` | GitHub OAuth tokens |
| `ghs_[a-zA-Z0-9]{36}` | GitHub server tokens |
| `ghu_[a-zA-Z0-9]{36}` | GitHub user tokens |
| `github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}` | GitHub fine-grained PATs |
| `xox[baprs]-[a-zA-Z0-9-]{10,}` | Slack tokens |
| `ya29\.[a-zA-Z0-9_-]+` | Google OAuth tokens |
| `Bearer [a-zA-Z0-9_.-]+` | Bearer tokens in headers |
| `Basic [a-zA-Z0-9+/=]+` | Basic auth headers |
| `password['"]?\s*[:=]\s*['"][^'"]+['"]` | Password assignments in code/config |
| `secret['"]?\s*[:=]\s*['"][^'"]+['"]` | Secret assignments |
| `AKIA[A-Z0-9]{16}` | AWS access key IDs |
| `aws_secret_access_key\s*=\s*[^\s]+` | AWS secret keys |
| `-----BEGIN [A-Z ]+ PRIVATE KEY-----` | Private key headers |
| `session[_-]?token['"]?\s*[:=]\s*['"][^'"]+['"]` | Session token assignments |

Additionally, quoted strings longer than 100 characters that are more than 90% alphanumeric (with `_` and `-`) are heuristically treated as tokens and redacted.

## Sensitive File Path Patterns

When `redact_sensitive_paths=True` (default), file paths matching these patterns are replaced with `[REDACTED]`:

| Pattern | Matches |
|---------|---------|
| `\.env` | `.env`, `.env.local` |
| `\.env\.[a-z]+` | `.env.production`, `.env.staging` |
| `credentials\.json` | `credentials.json` |
| `secrets\.json` | `secrets.json` |
| `\.aws/credentials` | `.aws/credentials` |
| `\.ssh/` | Any file under `.ssh/` |
| `\.gnupg/` | Any file under `.gnupg/` |
| `\.netrc` | `.netrc` |
| `id_rsa` | `id_rsa`, `id_rsa.pub` |
| `id_ed25519` | `id_ed25519` |
| `\.pem$` | Files ending in `.pem` |
| `\.key$` | Files ending in `.key` |

## Shell Command Redaction

`redact_command()` applies additional logic for shell commands:

1. **Environment variable assignments**: `VAR=value` or `export VAR=value` where the variable name matches a sensitive pattern. The value is replaced; the variable name is preserved.
2. **Secret flags**: `--password`, `--token`, `--secret`, `--api-key`, `--auth` flags with values, and `-p <value>` for MySQL commands only (not psql, where `-p` is the port flag).
3. **General value patterns**: Applied as a final pass.

## Security Model

The redactor protects against credential leakage in checkpoint data stored in the git repository. The threat it addresses: an agent receives a credential (e.g., in a response body, error message, or environment) and that credential ends up in the checkpoint transcript, which is then committed to a branch that may be accessed by humans or other systems.

The redactor is applied when writing checkpoint data to the `egg/checkpoints/v2` branch. It is a best-effort, defense-in-depth control — it does not prevent credentials from being used by the agent during its session, only from being persisted in the audit trail.

**Limitations:**
- The redactor is conservative but not exhaustive. Custom credential formats or obfuscated secrets may not be caught.
- Heuristic long-string detection may produce false positives (redacting non-sensitive long tokens) or false negatives (missing unusual formats).
- The redactor operates on serialized text; structured data that looks like a token when serialized but isn't may be incorrectly redacted.

## Accessing Unredacted Data

Unredacted checkpoint data is not separately stored. The redaction is applied at write time; the only copy in the git repository is the redacted version.

To access the original session context, you would need to re-run the session or reconstruct it from other sources. The agent's actual git commits and file changes in the worktree are not redacted.

For audit purposes where unredacted transcripts are needed, the checkpoint capture process in the gateway would need to be modified to write an additional copy to a secure, access-controlled storage location. This is not currently implemented.

## Related Documentation

- [Architecture Overview](../architecture/README.md) — Checkpoint system overview
- [Checkpoint Access Guide](../guides/checkpoint-access.md) — Querying the checkpoint store
