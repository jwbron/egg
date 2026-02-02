"""Configuration validation utilities for egg."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ValidationResult:
    """Result of a validation check."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.valid


def validate_config(config: dict[str, Any]) -> ValidationResult:
    """Validate an egg configuration dictionary.

    Args:
        config: Configuration dictionary to validate

    Returns:
        ValidationResult with any errors/warnings
    """
    errors: list[str] = []
    warnings: list[str] = []

    egg = config.get("egg", {})

    # Validate git settings
    git = egg.get("git", {})
    branch_prefix = git.get("branch_prefix", "egg/")
    if not branch_prefix:
        errors.append("git.branch_prefix cannot be empty")
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9_/-]*$", branch_prefix):
        errors.append(f"git.branch_prefix has invalid characters: {branch_prefix}")

    protected = git.get("protected_branches", [])
    if not protected:
        warnings.append("git.protected_branches is empty - no branches are protected")

    # Validate repositories
    repos = egg.get("repositories", {})
    allowed = repos.get("allowed", [])
    if not allowed:
        warnings.append("repositories.allowed is empty - no repos allowed")
    for repo in allowed:
        if "/" not in repo and "*" not in repo:
            errors.append(f"Invalid repository format: {repo} (expected owner/repo or owner/*)")

    # Validate secrets if present
    secrets = config.get("secrets", {})
    if secrets:
        # Check for at least one auth method
        has_github_auth = bool(secrets.get("github_app") or secrets.get("pats"))
        has_anthropic_auth = bool(secrets.get("anthropic"))

        if not has_github_auth:
            warnings.append("No GitHub authentication configured (github_app or pats)")
        if not has_anthropic_auth:
            warnings.append("No Anthropic authentication configured")

        # Validate GitHub App config
        github_app = secrets.get("github_app", {})
        if github_app:
            if not github_app.get("app_id"):
                errors.append("secrets.github_app.app_id is required")
            key_path = github_app.get("private_key_path")
            if not key_path:
                errors.append("secrets.github_app.private_key_path is required")
            elif not Path(key_path).exists():
                errors.append(f"GitHub App private key not found: {key_path}")

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


def mask_secret(value: str, visible_chars: int = 4) -> str:
    """Mask a secret value for safe logging.

    Args:
        value: Secret value to mask
        visible_chars: Number of characters to show at start

    Returns:
        Masked string like "sk-an****"
    """
    if not value:
        return ""
    if len(value) <= visible_chars:
        return "*" * len(value)
    return value[:visible_chars] + "*" * (len(value) - visible_chars)
