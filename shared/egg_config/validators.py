"""
Reusable validation functions for configuration values.

This module provides validators for common configuration patterns:
- URLs (HTTP/HTTPS)
- Email addresses
- Token formats (GitHub, Anthropic)
- Secret masking utilities
"""

import re
from urllib.parse import urlparse


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


def validate_port(port: int | str) -> tuple[bool, str | None]:
    """Validate a port number.

    Args:
        port: The port number to validate (can be int or string)

    Returns:
        Tuple of (is_valid, error_message). error_message is None if valid.
    """
    try:
        port_int = int(port)
    except (ValueError, TypeError):
        return False, f"Port must be a number, got: {port}"

    if port_int < 1 or port_int > 65535:
        return False, f"Port must be between 1 and 65535, got: {port_int}"

    return True, None
