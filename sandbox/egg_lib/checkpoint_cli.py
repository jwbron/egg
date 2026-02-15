#!/usr/bin/env python3
"""
Checkpoint CLI for browsing and querying agent checkpoints.

This module re-exports the CLI from shared/egg_contracts to avoid code duplication.
The implementation lives in shared/egg_contracts/checkpoint_cli.py.
"""

from egg_contracts.checkpoint_cli import (
    CHECKPOINT_BRANCH,
    cmd_browse,
    cmd_list,
    cmd_show,
    create_parser,
    ensure_checkpoint_ref,
    format_timestamp,
    format_tokens,
    get_repo_path,
    load_checkpoint_from_ref,
    load_index_from_ref,
    main,
    print_checkpoint_details,
    print_checkpoint_summary,
    read_git_file,
    run_git,
)

__all__ = [
    "CHECKPOINT_BRANCH",
    "cmd_browse",
    "cmd_list",
    "cmd_show",
    "create_parser",
    "ensure_checkpoint_ref",
    "format_timestamp",
    "format_tokens",
    "get_repo_path",
    "load_checkpoint_from_ref",
    "load_index_from_ref",
    "main",
    "print_checkpoint_details",
    "print_checkpoint_summary",
    "read_git_file",
    "run_git",
]

if __name__ == "__main__":
    import sys

    sys.exit(main())
