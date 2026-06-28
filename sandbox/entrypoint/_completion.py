"""Orchestrator completion signalling and exit cleanup."""

from __future__ import annotations

from ._config import Config, Logger
from ._core import _read_subprocess_stderr_tail


def signal_orchestrator_completion(
    config: Config,
    logger: Logger,
    exit_code: int = 0,
    error_message: str | None = None,
) -> None:
    """Signal completion to orchestrator if running in orchestrator mode.

    Uses the OrchestratorClient from egg_orchestrator package for consistency
    with other orchestrator communication.

    Args:
        config: Container configuration
        logger: Logger instance
        exit_code: Process exit code (0 = success)
        error_message: Optional error message if failed
    """
    if not config.is_orchestrator_mode:
        return

    if not config.orchestrator_url or not config.pipeline_id:
        logger.warn("Orchestrator mode enabled but missing URL or pipeline_id")
        return

    if not config.agent_role:
        logger.warn("Orchestrator mode enabled but missing agent_role")
        return

    try:
        from egg_orchestrator import OrchestratorClient

        client = OrchestratorClient(orchestrator_url=config.orchestrator_url)

        if exit_code == 0:
            # Success - signal completion
            response = client.signal_complete(
                pipeline_id=config.pipeline_id,
                agent_role=config.agent_role,
            )
            signal_type = "complete"
        else:
            # Failure - signal error with stderr context for debugging
            error_msg = error_message or f"Container exited with code {exit_code}"
            stderr_tail = _read_subprocess_stderr_tail(20)
            if stderr_tail:
                error_msg += f"\n--- subprocess stderr (last 20 lines) ---\n{stderr_tail}"
            response = client.signal_error(
                pipeline_id=config.pipeline_id,
                agent_role=config.agent_role,
                error=error_msg,
                recoverable=False,
            )
            signal_type = "error"

        if response.success:
            logger.info(f"Signaled {signal_type} to orchestrator")
        else:
            logger.warn(f"Orchestrator signal failed: {response.message}")

    except Exception as e:
        # Don't fail the exit process if signaling fails
        logger.warn(f"Failed to signal orchestrator: {e}")


def cleanup_on_exit(config: Config, logger: Logger, exit_code: int = 0) -> None:
    """Cleanup handler for container shutdown.

    In the gateway-managed worktree architecture, the container doesn't
    have access to git metadata, so there's minimal cleanup needed.
    The gateway handles worktree cleanup when containers exit.

    If running in orchestrator mode, signals completion/error to orchestrator.
    """
    # Signal completion to orchestrator if in orchestrator mode
    signal_orchestrator_completion(config, logger, exit_code)

    if not config.quiet:
        print("")
        print("Cleaning up on container exit...")
        print("✓ Cleanup complete")
