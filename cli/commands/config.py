"""Configuration commands for egg CLI."""

from pathlib import Path

from shared.egg_config.loader import find_config_file, load_config
from shared.egg_config.validators import ValidationResult, validate_config


def validate_config_files(
    config_path: str | None = None,
    secrets_path: str | None = None,
) -> ValidationResult:
    """Validate configuration files.

    Args:
        config_path: Path to egg.yaml (auto-discovered if None)
        secrets_path: Path to secrets.yaml (auto-discovered if None)

    Returns:
        ValidationResult with any errors/warnings
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Resolve config path
    resolved_config_path: Path | None = None
    if config_path:
        resolved_config_path = Path(config_path)
        if not resolved_config_path.exists():
            errors.append(f"Config file not found: {config_path}")
    else:
        resolved_config_path = find_config_file("egg.yaml", "EGG_CONFIG")
        if not resolved_config_path:
            errors.append(
                "No egg.yaml found. Searched: current directory, ~/.config/egg/, $EGG_CONFIG"
            )

    # Resolve secrets path
    resolved_secrets_path: Path | None = None
    if secrets_path:
        resolved_secrets_path = Path(secrets_path)
        if not resolved_secrets_path.exists():
            errors.append(f"Secrets file not found: {secrets_path}")
    else:
        resolved_secrets_path = find_config_file("secrets.yaml", "EGG_SECRETS")
        if not resolved_secrets_path:
            warnings.append(
                "No secrets.yaml found. Some features may not work without credentials."
            )

    # If we can't find the config, return early
    if not resolved_config_path or not resolved_config_path.exists():
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    # Load and validate
    try:
        config = load_config(
            config_path=resolved_config_path,
            secrets_path=resolved_secrets_path,
        )
    except Exception as e:
        errors.append(f"Failed to load configuration: {e}")
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    # Run validation
    result = validate_config(config)

    # Merge our errors/warnings with validation result
    all_errors = errors + result.errors
    all_warnings = warnings + result.warnings

    return ValidationResult(
        valid=len(all_errors) == 0,
        errors=all_errors,
        warnings=all_warnings,
    )


def get_config_info(config_path: str | None = None) -> dict[str, str]:
    """Get information about config files.

    Args:
        config_path: Optional explicit config path

    Returns:
        Dict with config file locations and status
    """
    info: dict[str, str] = {}

    # Find config file
    if config_path:
        explicit_path = Path(config_path)
        info["config_path"] = str(explicit_path)
        info["config_exists"] = "yes" if explicit_path.exists() else "no"
    else:
        discovered_path = find_config_file("egg.yaml", "EGG_CONFIG")
        if discovered_path:
            info["config_path"] = str(discovered_path)
            info["config_exists"] = "yes"
        else:
            info["config_path"] = "not found"
            info["config_exists"] = "no"

    # Find secrets file
    secrets_file = find_config_file("secrets.yaml", "EGG_SECRETS")
    if secrets_file:
        info["secrets_path"] = str(secrets_file)
        info["secrets_exists"] = "yes"
    else:
        info["secrets_path"] = "not found"
        info["secrets_exists"] = "no"

    return info
