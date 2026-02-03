"""CLI commands for egg.

This module contains the individual command implementations.
"""

from cli.commands.config import get_config_info, validate_config_files
from cli.commands.docker import (
    ContainerStatus,
    DockerError,
    check_docker_installed,
    check_docker_running,
    compose_down,
    compose_up,
    exec_in_container,
    get_container_status,
    list_egg_containers,
    start_container,
    stop_container,
    stream_logs,
)
from cli.commands.handlers import (
    handle_config,
    handle_exec,
    handle_logs,
    handle_start,
    handle_status,
    handle_stop,
)

__all__ = [
    # Docker utilities
    "ContainerStatus",
    "DockerError",
    "check_docker_installed",
    "check_docker_running",
    "compose_down",
    "compose_up",
    "exec_in_container",
    "get_container_status",
    "list_egg_containers",
    "start_container",
    "stop_container",
    "stream_logs",
    # Config utilities
    "get_config_info",
    "validate_config_files",
    # Command handlers
    "handle_config",
    "handle_exec",
    "handle_logs",
    "handle_start",
    "handle_status",
    "handle_stop",
]
