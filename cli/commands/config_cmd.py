"""Config command implementation."""

import argparse
import sys
from pathlib import Path

from ..config import DEFAULT_CONFIG_FILE, load_config, validate_config


def run_config(args: argparse.Namespace) -> int:
    """Configuration management.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    config_command = getattr(args, "config_command", None)

    if config_command == "validate":
        return _validate_config(args)
    elif config_command == "show":
        return _show_config(args)
    elif config_command == "init":
        return _init_config(args)
    else:
        print("Usage: egg config <subcommand>")
        print()
        print("Subcommands:")
        print("  validate  - Validate configuration files")
        print("  show      - Show current configuration")
        print("  init      - Create a new configuration file")
        return 0


def _validate_config(args: argparse.Namespace) -> int:
    """Validate configuration files."""
    config_path = getattr(args, "config", None)

    try:
        config = load_config(Path(config_path) if config_path else None)
    except FileNotFoundError as e:
        print(f"Configuration file not found: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error loading configuration: {e}", file=sys.stderr)
        return 1

    is_valid, errors = validate_config(config)

    if is_valid:
        print("Configuration is valid")
        return 0
    else:
        print("Configuration validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1


def _show_config(args: argparse.Namespace) -> int:
    """Show current configuration."""
    config_path = getattr(args, "config", None)

    try:
        config = load_config(Path(config_path) if config_path else None)
    except FileNotFoundError:
        print("No configuration file found")
        print("Create one with: egg config init")
        return 0
    except Exception as e:
        print(f"Error loading configuration: {e}", file=sys.stderr)
        return 1

    print("Egg Configuration")
    print("=" * 40)
    print()
    print("Directories:")
    print(f"  Config:   {config.config_dir}")
    print(f"  Data:     {config.data_dir}")
    print(f"  Worktree: {config.worktree_dir}")
    print()
    print("Mode:")
    print(f"  Private Mode: {config.private_mode}")
    print()
    print("Docker Images:")
    print(f"  Sandbox: {config.sandbox_image}")
    print(f"  Gateway: {config.gateway_image}")
    print()
    print("Runtime:")
    print(f"  UID: {config.runtime_uid}")
    print(f"  GID: {config.runtime_gid}")
    print()
    print(f"Repositories ({len(config.repositories)}):")
    if config.repositories:
        for repo in config.repositories:
            name = repo.name or repo.path.name
            print(f"  - {name}: {repo.path}")
    else:
        print("  (none configured)")
    print()

    if config.github_app_id:
        print("GitHub App:")
        print(f"  App ID: {config.github_app_id}")
        print(f"  Installation ID: {config.github_installation_id}")
        print(f"  Private Key: {config.github_app_private_key_path}")

    return 0


def _init_config(args: argparse.Namespace) -> int:
    """Create a new configuration file."""
    output = getattr(args, "output", None) or DEFAULT_CONFIG_FILE

    if Path(output).exists():
        print(f"Configuration file already exists: {output}", file=sys.stderr)
        print("Remove it first or specify a different path with --output", file=sys.stderr)
        return 1

    template = '''# Egg Sandbox Configuration
# See documentation at: https://github.com/jwbron/egg

# Repositories to mount in the sandbox
# Each repository should be a path to a git repository
repositories:
  # - path: ~/repos/my-project
  #   name: my-project  # optional, defaults to directory name
  #   branch: main      # optional, defaults to current branch

# Network mode
# private: network lockdown (only api.anthropic.com) + private repos only
# public (default): full internet access + public repos only
private_mode: false

# GitHub App configuration (optional)
# Required for push access with fine-grained permissions
# github:
#   app_id: "123456"
#   installation_id: "12345678"
#   private_key_path: ~/.config/egg/github-app-key.pem

# Advanced settings (usually don't need to change these)
# config_dir: ~/.config/egg
# data_dir: ~/.local/share/egg
# worktree_dir: ~/.egg-worktrees
'''

    try:
        with open(output, "w") as f:
            f.write(template)
        print(f"Created configuration file: {output}")
        print()
        print("Next steps:")
        print("  1. Edit the file to add your repositories")
        print("  2. Run 'egg config validate' to check your configuration")
        print("  3. Run 'egg start' to start the sandbox")
        return 0
    except Exception as e:
        print(f"Error creating configuration file: {e}", file=sys.stderr)
        return 1
