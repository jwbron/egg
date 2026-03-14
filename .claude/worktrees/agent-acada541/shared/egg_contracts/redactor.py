"""
Sensitive data redaction for checkpoint transcripts.

Provides pattern-based redaction of sensitive information including:
- Environment variables (tokens, secrets, API keys)
- File paths with sensitive patterns
- Common secret patterns (API keys, tokens, passwords)

The redactor is configurable and uses a combination of exact pattern
matching and heuristic detection.
"""

import re
from dataclasses import dataclass, field
from typing import Any

# Default patterns for sensitive environment variables
DEFAULT_SENSITIVE_ENV_PATTERNS = [
    r".*_TOKEN$",
    r".*_SECRET$",
    r".*_KEY$",
    r".*_PASSWORD$",
    r".*_CREDENTIAL.*",
    r".*_API_KEY$",
    r"^API_KEY$",
    r"^AUTH_TOKEN$",
    r"^GITHUB_TOKEN$",
    r"^ANTHROPIC_API_KEY$",
    r"^OPENAI_API_KEY$",
    r"^AWS_.*",
    r"^GCP_.*",
    r"^AZURE_.*",
    r"^DATABASE_URL$",
    r"^REDIS_URL$",
    r"^POSTGRES_.*",
    r"^MYSQL_.*",
    r"^MONGO.*_URI$",
]

# Patterns for sensitive values in text
DEFAULT_SENSITIVE_VALUE_PATTERNS = [
    # API keys and tokens (various formats)
    r"sk-[a-zA-Z0-9]{20,}",  # OpenAI API keys
    r"sk-ant-[a-zA-Z0-9-]{20,}",  # Anthropic API keys
    r"ghp_[a-zA-Z0-9]{36}",  # GitHub personal access tokens
    r"gho_[a-zA-Z0-9]{36}",  # GitHub OAuth tokens
    r"ghs_[a-zA-Z0-9]{36}",  # GitHub server tokens
    r"ghu_[a-zA-Z0-9]{36}",  # GitHub user tokens
    r"github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}",  # GitHub fine-grained PATs
    r"xox[baprs]-[a-zA-Z0-9-]{10,}",  # Slack tokens
    r"ya29\.[a-zA-Z0-9_-]+",  # Google OAuth tokens
    # Generic patterns
    r"Bearer [a-zA-Z0-9_.-]+",  # Bearer tokens
    r"Basic [a-zA-Z0-9+/=]+",  # Basic auth headers
    r"password['\"]?\s*[:=]\s*['\"][^'\"]+['\"]",  # Password assignments
    r"secret['\"]?\s*[:=]\s*['\"][^'\"]+['\"]",  # Secret assignments
    # AWS
    r"AKIA[A-Z0-9]{16}",  # AWS access key IDs
    r"aws_secret_access_key\s*=\s*[^\s]+",  # AWS secret keys
    # Private keys
    r"-----BEGIN [A-Z ]+ PRIVATE KEY-----",  # Private key headers
    # Session tokens (generic)
    r"session[_-]?token['\"]?\s*[:=]\s*['\"][^'\"]+['\"]",
]

# Placeholder for redacted content
REDACTED_PLACEHOLDER = "[REDACTED]"


@dataclass
class RedactorConfig:
    """Configuration for the redactor."""

    # Patterns for sensitive environment variable names
    env_patterns: list[str] = field(default_factory=lambda: DEFAULT_SENSITIVE_ENV_PATTERNS.copy())

    # Patterns for sensitive values in text
    value_patterns: list[str] = field(
        default_factory=lambda: DEFAULT_SENSITIVE_VALUE_PATTERNS.copy()
    )

    # Placeholder text for redacted content
    placeholder: str = REDACTED_PLACEHOLDER

    # Whether to redact file paths containing sensitive patterns
    redact_sensitive_paths: bool = True

    # File path patterns to redact
    sensitive_path_patterns: list[str] = field(
        default_factory=lambda: [
            r"\.env",
            r"\.env\.[a-z]+",
            r"credentials\.json",
            r"secrets\.json",
            r"\.aws/credentials",
            r"\.ssh/",
            r"\.gnupg/",
            r"\.netrc",
            r"id_rsa",
            r"id_ed25519",
            r"\.pem$",
            r"\.key$",
        ]
    )

    # Maximum length for values before considering them potentially sensitive
    # (very long base64-like strings are often tokens)
    max_safe_value_length: int = 100


class Redactor:
    """
    Redacts sensitive information from text and structured data.

    The redactor uses pattern matching to identify and replace sensitive
    information with a placeholder. It's designed to be conservative -
    it will redact anything that looks like it might be sensitive.
    """

    def __init__(self, config: RedactorConfig | None = None):
        """
        Initialize the redactor.

        Args:
            config: Optional configuration (uses defaults if not provided)
        """
        self.config = config or RedactorConfig()

        # Compile patterns for efficiency
        self._env_patterns = [re.compile(p, re.IGNORECASE) for p in self.config.env_patterns]
        self._value_patterns = [re.compile(p, re.IGNORECASE) for p in self.config.value_patterns]
        self._path_patterns = [
            re.compile(p, re.IGNORECASE) for p in self.config.sensitive_path_patterns
        ]

    def is_sensitive_env_name(self, name: str) -> bool:
        """
        Check if an environment variable name is sensitive.

        Args:
            name: Environment variable name

        Returns:
            True if the name matches a sensitive pattern
        """
        return any(pattern.match(name) for pattern in self._env_patterns)

    def is_sensitive_path(self, path: str) -> bool:
        """
        Check if a file path is sensitive.

        Args:
            path: File path

        Returns:
            True if the path matches a sensitive pattern
        """
        if not self.config.redact_sensitive_paths:
            return False
        return any(pattern.search(path) for pattern in self._path_patterns)

    def redact_text(self, text: str) -> str:
        """
        Redact sensitive patterns from text.

        Args:
            text: Text to redact

        Returns:
            Text with sensitive patterns replaced with placeholder
        """
        if not text:
            return text

        result = text

        # Apply value patterns
        for pattern in self._value_patterns:
            result = pattern.sub(self.config.placeholder, result)

        # Redact long base64-like strings that might be tokens
        # Look for strings that are mostly alphanumeric and longer than threshold
        def is_suspicious_token(match: re.Match[str]) -> str:
            value = match.group(0)
            if len(value) > self.config.max_safe_value_length:
                # Check if it looks like a token (alphanumeric with some special chars)
                alnum_ratio = sum(c.isalnum() or c in "_-" for c in value) / len(value)
                if alnum_ratio > 0.9:
                    return self.config.placeholder
            return value

        # Match quoted strings and check if they look like tokens
        quoted_pattern = re.compile(r"['\"][a-zA-Z0-9_+/=-]{50,}['\"]")
        result = quoted_pattern.sub(is_suspicious_token, result)

        return result

    def redact_dict(self, data: dict[str, Any], parent_key: str = "") -> dict[str, Any]:
        """
        Recursively redact sensitive values from a dictionary.

        Args:
            data: Dictionary to redact
            parent_key: Parent key path for nested dicts

        Returns:
            Dictionary with sensitive values redacted
        """
        result: dict[str, Any] = {}
        for key, value in data.items():
            full_key = f"{parent_key}.{key}" if parent_key else key

            # Check if key itself indicates sensitive data
            if self.is_sensitive_env_name(key.upper()):
                result[key] = self.config.placeholder
                continue

            if isinstance(value, dict):
                result[key] = self.redact_dict(value, full_key)
            elif isinstance(value, list):
                result[key] = self.redact_list(value, full_key)
            elif isinstance(value, str):
                # Redact the string value
                result[key] = self.redact_text(value)
            else:
                result[key] = value

        return result

    def redact_list(self, data: list[Any], parent_key: str = "") -> list[Any]:
        """
        Recursively redact sensitive values from a list.

        Args:
            data: List to redact
            parent_key: Parent key path for context

        Returns:
            List with sensitive values redacted
        """
        result: list[Any] = []
        for i, item in enumerate(data):
            item_key = f"{parent_key}[{i}]"
            if isinstance(item, dict):
                result.append(self.redact_dict(item, item_key))
            elif isinstance(item, list):
                result.append(self.redact_list(item, item_key))
            elif isinstance(item, str):
                result.append(self.redact_text(item))
            else:
                result.append(item)
        return result

    def redact_path(self, path: str) -> str:
        """
        Redact a file path if it's sensitive.

        Args:
            path: File path

        Returns:
            Original path or placeholder if sensitive
        """
        if self.is_sensitive_path(path):
            return self.config.placeholder
        return path

    def redact_command(self, command: str) -> str:
        """
        Redact sensitive information from a shell command.

        This handles common patterns like:
        - Environment variable assignments
        - Command line flags with secret values
        - Inline credentials

        Args:
            command: Shell command string

        Returns:
            Command with sensitive values redacted
        """
        if not command:
            return command

        result = command

        # Redact environment variable assignments
        # Match: VAR_NAME=value or export VAR_NAME=value
        env_assign_pattern = re.compile(
            r"((?:export\s+)?([A-Z_][A-Z0-9_]*)=)(['\"]?)([^'\"\s]+|[^']+|[^\"]+)\3",
            re.IGNORECASE,
        )

        def redact_env_match(match: re.Match[str]) -> str:
            prefix = match.group(1)
            var_name = match.group(2)
            quote = match.group(3)
            if self.is_sensitive_env_name(var_name):
                return f"{prefix}{quote}{self.config.placeholder}{quote}"
            return match.group(0)

        result = env_assign_pattern.sub(redact_env_match, result)

        # Redact common secret flags
        # Note: -p is context-specific. For MySQL tools (mysql, mysqldump), -p is the
        # password flag. For PostgreSQL tools (psql, pg_dump), -p is the PORT flag.
        # Only redact -p for MySQL commands.
        secret_flag_patterns = [
            (r"(--password[=\s]+)(\S+)", r"\1" + self.config.placeholder),
            (r"((mysql|mysqldump)\s+.*-p\s+)(\S+)", r"\1" + self.config.placeholder),
            (r"(--token[=\s]+)(\S+)", r"\1" + self.config.placeholder),
            (r"(--secret[=\s]+)(\S+)", r"\1" + self.config.placeholder),
            (r"(--api-key[=\s]+)(\S+)", r"\1" + self.config.placeholder),
            (r"(--auth[=\s]+)(\S+)", r"\1" + self.config.placeholder),
        ]

        for pattern, replacement in secret_flag_patterns:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        # Apply general value patterns
        result = self.redact_text(result)

        return result


# Default redactor instance
_default_redactor: Redactor | None = None


def get_default_redactor() -> Redactor:
    """Get the default redactor instance."""
    global _default_redactor
    if _default_redactor is None:
        _default_redactor = Redactor()
    return _default_redactor


def redact_text(text: str) -> str:
    """Redact sensitive patterns from text using the default redactor."""
    return get_default_redactor().redact_text(text)


def redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive values from a dictionary using the default redactor."""
    return get_default_redactor().redact_dict(data)


def redact_command(command: str) -> str:
    """Redact sensitive information from a command using the default redactor."""
    return get_default_redactor().redact_command(command)
