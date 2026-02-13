"""Shared container-launch command builder.

Provides ``build_sandbox_docker_cmd()`` — a pure function that constructs the
base ``docker run`` argument list used by production launchers and integration
tests alike.  Centralising this logic prevents divergence bugs (missing
``--add-host``, wrong env-var names, absent proxy config, etc.).

Also provides ``build_sandbox_config()`` — a framework-agnostic config builder
that both CLI (via ``build_sandbox_docker_cmd``) and orchestrator (via
``to_dockerpy_kwargs``) use to assemble container configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from egg_config import GATEWAY_PROXY_PORT

# Index where lifecycle flags (--rm, -it, -d) should be inserted.
# The returned command always has ["docker", "run", ...], so index 2 is
# immediately after "run". This constant makes the convention explicit
# and allows callers to reference it instead of hardcoding the magic number.
LIFECYCLE_FLAGS_INDEX = 2


@dataclass(frozen=True)
class ContainerNetworkConfig:
    """Network parameters needed to wire a sandbox container to its gateway."""

    network_name: str
    gateway_hostname: str
    gateway_ip: str
    gateway_port: int
    repo_mode: str  # "private" or "public"
    proxy_url: str | None = None  # Full proxy URL if provided; default uses GATEWAY_PROXY_PORT


@dataclass(frozen=True)
class MountSpec:
    """Specification for a single container mount."""

    mount_type: str  # "bind", "tmpfs", "volume"
    source: str | None  # host path or volume name (None for tmpfs)
    destination: str  # container path
    readonly: bool = False


@dataclass(frozen=True)
class SandboxContainerConfig:
    """Framework-agnostic sandbox container configuration.

    Produced by ``build_sandbox_config()`` and consumed by
    ``build_sandbox_docker_cmd()`` (CLI) or ``to_dockerpy_kwargs()``
    (docker-py SDK).
    """

    container_name: str
    image: str
    network: ContainerNetworkConfig
    environment: dict[str, str]
    mounts: tuple[MountSpec, ...]
    labels: dict[str, str]
    extra_hosts: dict[str, str]  # hostname -> IP
    security_opt: tuple[str, ...]
    dns: tuple[str, ...]
    container_ip: str | None = None
    command: tuple[str, ...] | None = None


def git_shadow_mounts(
    repo_volumes: dict[str, str],
    container_base: str = "/home/egg/repos",
    assume_worktree: bool = False,
) -> list[MountSpec]:
    """Create .git shadow mounts to prevent local git operations.

    Each repo gets its ``.git`` path hidden so that ``git`` commands fail
    inside the container, forcing all git operations through the gateway.

    Args:
        repo_volumes: Mapping of repo_name -> host_path.
        container_base: Base path in container for repos.
        assume_worktree: If True, always use tmpfs (orchestrator can't stat
            host paths from inside its own container). If False, check whether
            .git is a file (worktree) or directory and choose the mount type.

    Returns:
        List of MountSpec for .git shadow mounts.
    """
    mounts: list[MountSpec] = []
    for repo_name, host_path in repo_volumes.items():
        git_dest = f"{container_base}/{repo_name}/.git"
        if assume_worktree:
            # Orchestrator path: repos are always gateway-created worktrees,
            # so .git is a file (gitdir link), not a directory.  We must use
            # a /dev/null bind mount (file-over-file) rather than tmpfs
            # (directory-over-file), otherwise Docker fails with
            # "not a directory" at container start.
            mounts.append(
                MountSpec(
                    mount_type="bind",
                    source="/dev/null",
                    destination=git_dest,
                    readonly=True,
                )
            )
        else:
            # CLI path: inspect actual .git type
            git_path = Path(host_path) / ".git"
            if git_path.exists() and git_path.is_file():
                # Worktree: .git is a file, shadow with /dev/null bind
                mounts.append(
                    MountSpec(
                        mount_type="bind",
                        source="/dev/null",
                        destination=git_dest,
                        readonly=True,
                    )
                )
            else:
                # Regular repo or missing .git: shadow with tmpfs
                mounts.append(MountSpec(mount_type="tmpfs", source=None, destination=git_dest))
    return mounts


def mount_spec_to_cli_args(mount: MountSpec) -> list[str]:
    """Convert a MountSpec to docker CLI mount arguments.

    Returns:
        List of CLI arguments (e.g., ``["--mount", "type=bind,..."]``).
    """
    if mount.mount_type == "bind" and mount.source is not None:
        parts = f"type=bind,source={mount.source},destination={mount.destination}"
        if mount.readonly:
            parts += ",readonly"
        return ["--mount", parts]
    elif mount.mount_type == "tmpfs":
        return ["--mount", f"type=tmpfs,destination={mount.destination}"]
    return []


def build_sandbox_config(
    *,
    container_name: str,
    image: str,
    network: ContainerNetworkConfig,
    container_ip: str | None = None,
    session_token: str | None = None,
    runtime_uid: int | None = None,
    runtime_gid: int | None = None,
    extra_env: dict[str, str] | None = None,
    mounts: list[MountSpec] | None = None,
    labels: dict[str, str] | None = None,
    command: list[str] | None = None,
) -> SandboxContainerConfig:
    """Build a framework-agnostic sandbox container configuration.

    Assembles the same environment, networking, and security settings
    used by both the CLI launcher and the orchestrator.  Callers convert
    the result to their transport format:

    * CLI → ``build_sandbox_docker_cmd()`` (produces ``docker run`` args)
    * Orchestrator → ``to_dockerpy_kwargs()`` (produces docker-py kwargs)

    Args:
        container_name: Docker ``--name`` value (also default CONTAINER_ID).
        image: Docker image reference.
        network: Network wiring parameters.
        container_ip: Optional static IP for session binding.
        session_token: If set, passed as ``EGG_SESSION_TOKEN``.
        runtime_uid: Host UID forwarded to the container entry-point.
        runtime_gid: Host GID forwarded to the container entry-point.
        extra_env: Caller-specific env vars (applied last, can override).
        mounts: Additional mount specifications.
        labels: Container labels.
        command: Command to execute in the container.
    """
    env: dict[str, str] = {}

    # Gateway API URL (hostname-based so --add-host / extra_hosts resolves it)
    env["GATEWAY_URL"] = f"http://{network.gateway_hostname}:{network.gateway_port}"

    # Container identity
    env["CONTAINER_ID"] = container_name

    # Runtime UID/GID for entry-point user mapping
    if runtime_uid is not None:
        env["RUNTIME_UID"] = str(runtime_uid)
    if runtime_gid is not None:
        env["RUNTIME_GID"] = str(runtime_gid)

    # Session token for gateway authentication
    if session_token:
        env["EGG_SESSION_TOKEN"] = session_token

    # --- Mode-specific network settings ---
    dns: tuple[str, ...] = ()
    if network.repo_mode == "private":
        proxy = network.proxy_url or f"http://{network.gateway_hostname}:{GATEWAY_PROXY_PORT}"
        no_proxy = f"localhost,127.0.0.1,{network.gateway_hostname}"
        dns = ("0.0.0.0",)
        env["PRIVATE_MODE"] = "true"
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    else:
        env["PRIVATE_MODE"] = "false"

    # Caller-specific extras (applied last so they can override defaults)
    if extra_env:
        env.update(extra_env)

    return SandboxContainerConfig(
        container_name=container_name,
        image=image,
        network=network,
        environment=env,
        mounts=tuple(mounts) if mounts else (),
        labels=dict(labels) if labels else {},
        extra_hosts={network.gateway_hostname: network.gateway_ip},
        security_opt=("label=disable",),
        dns=dns,
        container_ip=container_ip,
        command=tuple(command) if command else None,
    )


def to_dockerpy_kwargs(config: SandboxContainerConfig) -> dict:
    """Convert SandboxContainerConfig to docker-py ``containers.create()`` kwargs.

    Returns a dict suitable for passing to ``DockerClient.create_container()``.
    All mounts use the ``mounts`` list (Docker API format) to avoid key
    collisions when multiple mounts share the same source (e.g. multiple
    /dev/null .git shadow mounts).
    """
    mount_list: list[dict] = []

    for mount in config.mounts:
        if mount.mount_type in ("bind", "volume") and mount.source is not None:
            mount_list.append(
                {
                    "Type": mount.mount_type,
                    "Source": mount.source,
                    "Target": mount.destination,
                    "ReadOnly": mount.readonly,
                }
            )
        elif mount.mount_type == "tmpfs":
            mount_list.append(
                {
                    "Type": "tmpfs",
                    "Target": mount.destination,
                }
            )

    kwargs: dict = {
        "name": config.container_name,
        "image": config.image,
        "environment": dict(config.environment),
        "network": config.network.network_name,
        "labels": dict(config.labels),
        "security_opt": list(config.security_opt),
    }

    if mount_list:
        kwargs["mounts"] = mount_list
    if config.extra_hosts:
        kwargs["extra_hosts"] = dict(config.extra_hosts)
    if config.dns:
        kwargs["dns"] = list(config.dns)
    if config.command:
        kwargs["command"] = list(config.command)

    return kwargs


def build_sandbox_docker_cmd(
    *,
    container_name: str,
    image: str,
    network: ContainerNetworkConfig,
    container_ip: str | None = None,
    session_token: str | None = None,
    runtime_uid: int | None = None,
    runtime_gid: int | None = None,
    extra_env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build the base ``docker run`` argument list for a sandbox container.

    Uses ``build_sandbox_config`` internally to ensure the CLI and
    orchestrator share identical configuration logic.

    The returned list starts with ``["docker", "run"]`` and ends with the
    *image* name.  Callers typically:

    * Insert lifecycle flags (``--rm``, ``-it``) at ``cmd[LIFECYCLE_FLAGS_INDEX:LIFECYCLE_FLAGS_INDEX]``.
      Use the module constant to avoid hardcoding the index.
    * Insert mount arguments before the image: ``cmd[-1:-1] = mount_args``.
    * Append the command to execute after the image name.

    Args:
        container_name: Docker ``--name`` value (also used for CONTAINER_ID).
        image: Docker image reference (always the last element).
        network: Network wiring parameters.
        container_ip: Optional static ``--ip`` for session binding.
        session_token: If set, passed as ``EGG_SESSION_TOKEN``.
        runtime_uid: Host UID forwarded to the container entry-point.
        runtime_gid: Host GID forwarded to the container entry-point.
        extra_env: Caller-specific environment variables.
        extra_args: Caller-specific raw docker arguments.
    """
    config = build_sandbox_config(
        container_name=container_name,
        image=image,
        network=network,
        container_ip=container_ip,
        session_token=session_token,
        runtime_uid=runtime_uid,
        runtime_gid=runtime_gid,
        extra_env=extra_env,
    )

    cmd: list[str] = ["docker", "run"]

    # Security options
    for opt in config.security_opt:
        cmd.extend(["--security-opt", opt])

    # Container name and network
    cmd.extend(["--name", config.container_name])
    cmd.extend(["--network", config.network.network_name])

    # Static IP for session binding
    if config.container_ip:
        cmd.extend(["--ip", config.container_ip])

    # Gateway hostname resolution
    for hostname, ip in config.extra_hosts.items():
        cmd.extend(["--add-host", f"{hostname}:{ip}"])

    # Environment variables
    for key, value in config.environment.items():
        cmd.extend(["-e", f"{key}={value}"])

    # DNS lockdown (private mode)
    for dns_server in config.dns:
        cmd.extend(["--dns", dns_server])

    # Mounts from config
    for mount in config.mounts:
        cmd.extend(mount_spec_to_cli_args(mount))

    # Caller-specific extras
    if extra_args:
        cmd.extend(extra_args)

    # Image is always last — callers insert mounts before it, append
    # commands after it.
    cmd.append(config.image)

    return cmd
