"""egg_lib - Modular package for the egg container launcher.

This package provides the core functionality for running Claude Code
in an isolated Docker container.

Public symbols are re-exported here for backward compatibility with the
``egg`` launcher script and its tests (which use SourceFileLoader).

These re-exports are guarded by ImportError so that lightweight subpackages
like ``egg_lib.self_improvement`` can be imported in environments where
heavy dependencies (PyYAML, statusbar, egg_container, etc.) are not
installed — for example, in CI workflows that only need the data
collection utilities.
"""

# Version info (matches egg script)
__version__ = "1.0.0"

try:
    # Auth module exports
    from .auth import (
        get_anthropic_api_key as get_anthropic_api_key,
    )
    from .auth import (
        get_anthropic_auth_method as get_anthropic_auth_method,
    )
    from .auth import (
        get_github_app_token as get_github_app_token,
    )
    from .auth import (
        get_github_readonly_token as get_github_readonly_token,
    )
    from .auth import (
        get_github_token as get_github_token,
    )

    # Config module exports
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

    # Docker build-context module exports
    from .docker import (
        is_dangerous_dir as is_dangerous_dir,
    )
    from .docker import (
        populate_build_context as populate_build_context,
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

    # Setup flow module exports
    from .setup_flow import (
        add_standard_mounts as add_standard_mounts,
    )
    from .setup_flow import (
        check_host_setup as check_host_setup,
    )

    # Timing module exports
    from .timing import (
        StartupTimer as StartupTimer,
    )
    from .timing import (
        _host_timer as _host_timer,
    )
except ImportError as e:
    # Some submodules have heavy dependencies (PyYAML, statusbar,
    # egg_container) that may not be installed in lightweight CI
    # environments. The package still works — callers just import
    # submodules directly (e.g., ``from egg_lib.config import Config``).
    #
    # We only suppress ImportErrors for known optional dependencies.
    # Unexpected import failures are re-raised to surface real bugs.
    import logging as _logging

    _OPTIONAL_DEPS = {"yaml", "statusbar", "egg_container"}
    _missing = getattr(e, "name", None) or str(e)

    if any(dep in str(_missing) for dep in _OPTIONAL_DEPS):
        _logging.debug(
            "egg_lib: Optional dependency not available, some exports unavailable: %s",
            e,
        )
    else:
        raise
