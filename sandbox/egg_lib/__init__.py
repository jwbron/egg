"""egg_lib - Modular package for the egg container launcher.

This package provides the core functionality for running Claude Code
in an isolated Docker container.

All public symbols are re-exported here for backward compatibility
with tests that use SourceFileLoader to import the egg module.
"""

# Auth module exports
from .auth import (
    get_anthropic_api_key as get_anthropic_api_key,
)
from .auth import (
    get_anthropic_auth_method as get_anthropic_auth_method,
)
from .auth import (
    get_github_readonly_token as get_github_readonly_token,
)
from .auth import (
    get_github_token as get_github_token,
)

# CLI module exports
from .cli import gha_exec as gha_exec
from .cli import main as main
from .config import (
    EGG_ISOLATED_NETWORK as EGG_ISOLATED_NETWORK,
)

# Config module exports
from .config import (
    GATEWAY_CONTAINER_NAME as GATEWAY_CONTAINER_NAME,
)
from .config import (
    GATEWAY_IMAGE_NAME as GATEWAY_IMAGE_NAME,
)
from .config import (
    GATEWAY_PORT as GATEWAY_PORT,
)
from .config import (
    Colors as Colors,
)
from .config import (
    Config as Config,
)
from .config import (
    get_local_repos as get_local_repos,
)
from .config import (
    get_platform as get_platform,
)

# Container logging module exports
from .container_logging import (
    CONTAINER_LOGS_DIR as CONTAINER_LOGS_DIR,
)
from .container_logging import (
    extract_task_id_from_command as extract_task_id_from_command,
)
from .container_logging import (
    extract_thread_ts_from_task_file as extract_thread_ts_from_task_file,
)
from .container_logging import (
    generate_container_id as generate_container_id,
)
from .container_logging import (
    get_docker_log_config as get_docker_log_config,
)
from .container_logging import (
    save_container_logs as save_container_logs,
)
from .container_logging import (
    update_log_index as update_log_index,
)

# Context module exports
from .context import RuntimeContext as RuntimeContext
from .context import get_context as get_context
from .context import set_context as set_context

# Docker module exports
from .docker import (
    BUILD_HASH_LABEL as BUILD_HASH_LABEL,
)
from .docker import (
    build_image as build_image,
)
from .docker import (
    check_claude_update as check_claude_update,
)
from .docker import (
    check_docker as check_docker,
)
from .docker import (
    check_docker_permissions as check_docker_permissions,
)
from .docker import (
    compute_build_hash as compute_build_hash,
)
from .docker import (
    create_dockerfile as create_dockerfile,
)
from .docker import (
    ensure_egg_network as ensure_egg_network,
)
from .docker import (
    get_installed_claude_version as get_installed_claude_version,
)
from .docker import (
    get_latest_claude_version as get_latest_claude_version,
)
from .docker import (
    image_exists as image_exists,
)
from .docker import (
    is_dangerous_dir as is_dangerous_dir,
)
from .docker import (
    set_force_rebuild as set_force_rebuild,
)
from .docker import (
    should_rebuild_image as should_rebuild_image,
)
from .docker import (
    teardown_networks as teardown_networks,
)

# Gateway module exports
from .gateway import (
    build_gateway_image as build_gateway_image,
)
from .gateway import (
    cleanup_gateway as cleanup_gateway,
)
from .gateway import (
    gateway_image_exists as gateway_image_exists,
)
from .gateway import (
    is_gateway_running as is_gateway_running,
)
from .gateway import (
    start_gateway_container as start_gateway_container,
)
from .gateway import (
    wait_for_gateway_health as wait_for_gateway_health,
)

# Output module exports
from .output import (
    error as error,
)
from .output import (
    get_quiet_mode as get_quiet_mode,
)
from .output import (
    info as info,
)
from .output import (
    set_quiet_mode as set_quiet_mode,
)
from .output import (
    success as success,
)
from .output import (
    warn as warn,
)

# Runtime module exports
from .runtime import (
    exec_in_new_container as exec_in_new_container,
)
from .runtime import (
    run_claude as run_claude,
)

# Setup flow module exports
from .setup_flow import (
    add_standard_mounts as add_standard_mounts,
)
from .setup_flow import (
    check_host_setup as check_host_setup,
)
from .setup_flow import (
    setup as setup,
)

# Timing module exports
from .timing import (
    StartupTimer as StartupTimer,
)
from .timing import (
    _host_timer as _host_timer,
)

# Version info (matches egg script)
__version__ = "1.0.0"
