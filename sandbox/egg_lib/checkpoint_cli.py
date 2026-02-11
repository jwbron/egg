#!/usr/bin/env python3
"""
Checkpoint CLI for browsing and querying agent checkpoints.

This module re-exports the CLI from shared/egg_contracts to avoid code duplication.
The implementation lives in shared/egg_contracts/checkpoint_cli.py.
"""

from egg_contracts.checkpoint_cli import (
    CHECKPOINT_BRANCH,
    checkout_checkpoint_branch,
    cleanup_worktree,
    cmd_browse,
    cmd_list,
    cmd_show,
    create_parser,
    format_timestamp,
    format_tokens,
    get_repo_path,
    main,
    print_checkpoint_details,
    print_checkpoint_summary,
    run_git,
)

__all__ = [
    "CHECKPOINT_BRANCH",
    "checkout_checkpoint_branch",
    "cleanup_worktree",
    "cmd_browse",
    "cmd_list",
    "cmd_show",
    "create_parser",
    "format_timestamp",
    "format_tokens",
    "get_repo_path",
    "main",
    "print_checkpoint_details",
    "print_checkpoint_summary",
    "run_git",
]

if __name__ == "__main__":
    import sys

    sys.exit(main())
