#!/usr/bin/env python3
"""
Egg Container Entrypoint

Sets up the sandboxed container environment for the autonomous AI agent.
Handles user setup, git configuration, service initialization, and launches
the appropriate LLM interface.

Converted from entrypoint.sh for better maintainability.

Sub-package barrel (#3312, slice 9): the explicit per-symbol re-exports
below form the stable public API. External importers and
``unittest.mock.patch`` targets resolve through this module
(e.g. ``patch("entrypoint.setup_git")``); the underscore-prefixed
submodules are package-private. ``main()`` lives here so that patching a
re-exported setup helper (``patch("entrypoint.setup_user")``) reaches the
call site. The earliest container-start timestamp is captured in ``_core``.
See docs/guides/decomposition-pattern.md.
"""

from __future__ import annotations

import os
import signal
import sys
from typing import Any

from ._claude import setup_agent_rules, setup_bashrc, setup_claude
from ._command_timeout import setup_command_timeout
from ._completion import cleanup_on_exit, signal_orchestrator_completion
from ._config import Config, Logger
from ._core import chown_recursive, run_cmd
from ._environment import (
    setup_anthropic_api,
    setup_environment,
    setup_gateway_ca,
    setup_git,
)
from ._exec import _chdir_to_single_repo, _exclude_from_git, run_exec
from ._gateway_health import check_gateway_health
from ._timing import StartupTimer, timed_phase
from ._user import (
    _find_free_uid,
    _resolve_gid_conflict,
    _resolve_uid_conflict,
    setup_repo_permissions,
    setup_user,
)
from ._worktrees import restore_prebuilt_deps, setup_egg_symlink, setup_worktrees

__all__ = [
    "Config",
    "Logger",
    "StartupTimer",
    "_chdir_to_single_repo",
    "_exclude_from_git",
    "_find_free_uid",
    "_resolve_gid_conflict",
    "_resolve_uid_conflict",
    "check_gateway_health",
    "chown_recursive",
    "cleanup_on_exit",
    "main",
    "restore_prebuilt_deps",
    "run_cmd",
    "run_exec",
    "setup_agent_rules",
    "setup_anthropic_api",
    "setup_bashrc",
    "setup_claude",
    "setup_command_timeout",
    "setup_egg_symlink",
    "setup_environment",
    "setup_gateway_ca",
    "setup_git",
    "setup_repo_permissions",
    "setup_user",
    "setup_worktrees",
    "signal_orchestrator_completion",
    "timed_phase",
]


def main() -> None:
    """Main entry point."""
    config = Config()
    logger = Logger(config.quiet, config.debug)

    if config.debug:
        logger.phase_start("entrypoint_init")

    # Log orchestrator mode if enabled
    if config.is_orchestrator_mode:
        logger.info(
            f"Running in orchestrator mode: {config.orchestrator_mode}, "
            f"pipeline={config.pipeline_id}, role={config.agent_role}"
        )

    # Track subprocess completion state for signal handling
    # If SIGTERM arrives before subprocess completes, we signal interrupted (128+signum)
    # If it arrives after, the subprocess already signaled its exit code
    subprocess_completed = [False]  # Use list to allow modification from nested function

    # Register cleanup handler
    def signal_handler(signum: int, frame: Any) -> None:
        # If subprocess hasn't completed, this is an interruption - use signal-based exit code
        # SIGTERM = 128+15=143, SIGINT = 128+2=130
        if not subprocess_completed[0]:
            cleanup_on_exit(config, logger, exit_code=128 + signum)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Run setup with timing instrumentation
    # Debug logging goes to stderr for capture even on container hang
    with timed_phase("setup_user", logger):
        setup_user(config, logger)

    with timed_phase("setup_repo_permissions", logger):
        setup_repo_permissions(config, logger)

    with timed_phase("setup_environment", logger):
        setup_environment(config)

    with timed_phase("setup_egg_symlink", logger):
        setup_egg_symlink(config, logger)

    with timed_phase("setup_git", logger):
        setup_git(config, logger)

    with timed_phase("setup_gateway_ca", logger):
        setup_gateway_ca(config, logger)

    with timed_phase("setup_worktrees", logger):
        if not setup_worktrees(config, logger):
            logger.error("")
            logger.error("Container startup aborted due to worktree configuration failure.")
            logger.error("Please check your egg setup and try again.")
            sys.exit(1)

    with timed_phase("restore_prebuilt_deps", logger):
        restore_prebuilt_deps(config, logger)

    with timed_phase("setup_agent_rules", logger):
        setup_agent_rules(config, logger)

    with timed_phase("setup_claude", logger):
        setup_claude(config, logger)

    with timed_phase("setup_bashrc", logger):
        setup_bashrc(config, logger)

    with timed_phase("setup_command_timeout", logger):
        setup_command_timeout(config, logger)

    # Wait for gateway readiness (network lockdown mode)
    with timed_phase("check_gateway", logger):
        if not check_gateway_health(config, logger):
            logger.error("")
            logger.error("Container startup aborted: gateway not ready.")
            logger.error("Ensure the gateway sidecar is running.")
            sys.exit(1)

    # Configure Anthropic API to route through gateway
    with timed_phase("setup_anthropic_api", logger):
        setup_anthropic_api(config, logger)

    # Remove launcher secret from process environment before launching Claude.
    os.environ.pop("EGG_LAUNCHER_SECRET", None)

    # Run appropriate mode (timing summary is printed inside each mode).
    # Interactive mode was removed in #1762; this container now only
    # supports --exec / orchestrator mode.
    if len(sys.argv) == 1:
        if config.is_orchestrator_mode:
            error_msg = (
                "No command provided but container is in pipeline mode "
                f"(EGG_PIPELINE_ID={config.pipeline_id}). "
                "This likely indicates a bug in prompt reconstruction during restart."
            )
            logger.error("")
            logger.error(f"ERROR: {error_msg}")
            logger.error("")
            signal_orchestrator_completion(config, logger, exit_code=1, error_message=error_msg)
            sys.exit(1)
        error_msg = (
            "No command provided; this container only supports --exec or "
            "orchestrator mode. Interactive mode was removed in #1762 — "
            "submit work through the egg orchestrator's MCP server "
            "(submit_task) from a host (Claude Code or any MCP client)."
        )
        logger.error("")
        logger.error(f"ERROR: {error_msg}")
        logger.error("")
        sys.exit(2)
    else:
        exit_code = run_exec(config, logger, sys.argv[1:])

    # Mark subprocess as completed - signal handler should not override exit code now
    subprocess_completed[0] = True

    # Signal completion to orchestrator (if in orchestrator mode)
    # This runs after subprocess exits, thanks to subprocess.run() instead of os.execvpe()
    cleanup_on_exit(config, logger, exit_code)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
