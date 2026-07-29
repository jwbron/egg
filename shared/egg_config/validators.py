"""
Reusable validation functions for configuration values.

This module provides validators for common configuration patterns:
- URLs (HTTP/HTTPS)
- Email addresses
- Token formats (GitHub, Anthropic)
- Secret masking utilities
"""

import logging
import re
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def validate_url(url: str, *, require_https: bool = True) -> tuple[bool, str | None]:
    """Validate a URL.

    Args:
        url: The URL to validate
        require_https: If True, only HTTPS URLs are valid (default: True)

    Returns:
        Tuple of (is_valid, error_message). error_message is None if valid.
    """
    if not url:
        return False, "URL is empty"

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"Invalid URL format: {e}"

    if not parsed.scheme:
        return False, "URL missing scheme (http:// or https://)"

    if not parsed.netloc:
        return False, "URL missing host"

    if require_https and parsed.scheme != "https":
        return False, f"URL must use HTTPS, got {parsed.scheme}://"

    if parsed.scheme not in ("http", "https"):
        return False, f"URL scheme must be http or https, got {parsed.scheme}"

    return True, None


def validate_email(email: str) -> tuple[bool, str | None]:
    """Validate an email address.

    Args:
        email: The email address to validate

    Returns:
        Tuple of (is_valid, error_message). error_message is None if valid.
    """
    if not email:
        return False, "Email is empty"

    # Basic email regex - not exhaustive but catches common issues
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, email):
        return False, "Invalid email format"

    return True, None


def validate_github_token(token: str) -> tuple[bool, str | None]:
    """Validate a GitHub token format.

    Valid GitHub token prefixes:
    - ghp_: Personal access tokens (fine-grained or classic)
    - github_pat_: Personal access tokens (newer format)
    - ghs_: GitHub App installation tokens
    - gho_: OAuth tokens
    - ghu_: User-to-server tokens

    Args:
        token: The GitHub token to validate

    Returns:
        Tuple of (is_valid, error_message). error_message is None if valid.
    """
    if not token:
        return False, "GitHub token is empty"

    valid_prefixes = ("ghp_", "github_pat_", "ghs_", "gho_", "ghu_")
    if not token.startswith(valid_prefixes):
        return False, f"GitHub token must start with one of: {', '.join(valid_prefixes)}"

    # GitHub tokens have a minimum length
    if len(token) < 20:
        return False, "GitHub token appears too short"

    return True, None


def validate_anthropic_key(key: str) -> tuple[bool, str | None]:
    """Validate an Anthropic API key format.

    Valid Anthropic key prefix:
    - sk-ant-: Anthropic API keys

    Args:
        key: The Anthropic API key to validate

    Returns:
        Tuple of (is_valid, error_message). error_message is None if valid.
    """
    if not key:
        return False, "Anthropic API key is empty"

    if not key.startswith("sk-ant-"):
        return False, "Anthropic API key must start with 'sk-ant-'"

    # Anthropic keys are typically long
    if len(key) < 20:
        return False, "Anthropic API key appears too short"

    return True, None


def mask_secret(value: str | None, *, visible_chars: int = 4) -> str:
    """Mask a secret value for safe display.

    Shows the first few characters followed by asterisks.

    Args:
        value: The secret value to mask (can be None)
        visible_chars: Number of characters to show at the start (default: 4)

    Returns:
        Masked string like "xoxb-****" or "[EMPTY]" if value is empty/None
    """
    if value is None or not value:
        return "[EMPTY]"

    if len(value) <= visible_chars:
        return "*" * len(value)

    return value[:visible_chars] + "*" * (len(value) - visible_chars)


def validate_non_empty(value: str | None, field_name: str) -> tuple[bool, str | None]:
    """Validate that a value is not empty or None.

    Args:
        value: The value to check
        field_name: Name of the field for error messages

    Returns:
        Tuple of (is_valid, error_message). error_message is None if valid.
    """
    if value is None:
        return False, f"{field_name} is not set"

    if not value.strip():
        return False, f"{field_name} is empty"

    return True, None


def validate_checks(checks: list[Any]) -> list[dict[str, str]]:
    """Validate and normalize a list of check command entries.

    Filters out malformed entries and coerces values to strings.
    Used by config, orchestrator, and compose to validate check
    definitions from YAML config or JSON env vars.

    Each entry requires ``name`` and ``command``. An optional ``fix``
    key names a shell command that auto-remediates a failing check
    (e.g. ``make lint-fix`` for a ``lint`` check); the per-slice green
    gate runs it at the slice tip and commits the result (#3409). A
    ``fix`` that is present but is not a non-empty string — empty,
    whitespace-only, ``None``, falsy (``false`` / ``0``), or a
    non-string such as a YAML list — is dropped from the entry and a
    warning is logged (#3630). It is never ``str()``-coerced, since
    coercing a list would hand the shell a command like
    ``"['make fmt', 'make lint-fix']"``. Whitespace-only is rejected
    for the same reason the other cases are: handing the green gate a
    no-op command produces exactly the silent-pass confusion #3630 set
    out to eliminate.

    An optional ``full_command`` key names the **ground-truth** form of
    the same check, for repos whose ``command`` is deliberately narrowed
    (#3669). egg's ``test`` check is ``make test``, which is
    changeset-aware by design and selects only the tests statically
    reachable from the diff; ``make test-all`` is the CI ground truth.
    The propose-time check gate runs ``full_command`` when present and
    records the exact string it ran, so a narrowed run can never be
    mistaken for a full one. A ``full_command`` that is present but
    empty/None is dropped from the entry, leaving ``command`` as the
    only form of the check.

    Args:
        checks: Raw list of check entries (e.g. from YAML or JSON).

    Returns:
        List of {"name": "...", "command": "..."} dicts (plus "fix" /
        "full_command" when configured) with only valid entries
        retained.
    """
    if not isinstance(checks, list):
        return []
    result = []
    for c in checks:
        if not (isinstance(c, dict) and "name" in c and "command" in c):
            continue
        entry = {"name": str(c["name"]), "command": str(c["command"])}
        if "fix" in c:
            fix = c["fix"]
            if isinstance(fix, str) and fix.strip():
                entry["fix"] = fix
            else:
                logger.warning(
                    "validate_checks: check %r has invalid fix %r "
                    "(expected non-empty string); dropping fix",
                    c.get("name"),
                    fix,
                )
        if c.get("full_command"):
            entry["full_command"] = str(c["full_command"])
        result.append(entry)
    return result


def validate_port(port: int | str) -> tuple[bool, str | None]:
    """Validate a port number.

    Args:
        port: The port number to validate (can be int or string)

    Returns:
        Tuple of (is_valid, error_message). error_message is None if valid.
    """
    try:
        port_int = int(port)
    except ValueError, TypeError:
        return False, f"Port must be a number, got: {port}"

    if port_int < 1 or port_int > 65535:
        return False, f"Port must be between 1 and 65535, got: {port_int}"

    return True, None
