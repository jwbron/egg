"""Shared container-launch command builder.

Provides ``build_sandbox_docker_cmd()`` — a pure function that constructs the
base ``docker run`` argument list used by production launchers and integration
tests alike.  Centralising this logic prevents divergence bugs (missing
``--add-host``, wrong env-var names, absent proxy config, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass

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
    proxy_url: str | None = None  # e.g. "http://egg-gateway:3129"


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
    cmd: list[str] = [
        "docker",
        "run",
        "--security-opt",
        "label=disable",
        "--name",
        container_name,
        "--network",
        network.network_name,
    ]

    # Static IP for session binding
    if container_ip:
        cmd.extend(["--ip", container_ip])

    # Gateway hostname resolution
    cmd.extend(
        [
            "--add-host",
            f"{network.gateway_hostname}:{network.gateway_ip}",
        ]
    )

    # --- Environment variables ---

    # Gateway API URL
    cmd.extend(
        [
            "-e",
            f"GATEWAY_URL=http://{network.gateway_hostname}:{network.gateway_port}",
        ]
    )

    # Container identity
    cmd.extend(["-e", f"CONTAINER_ID={container_name}"])

    # Runtime UID/GID for entry-point user mapping
    if runtime_uid is not None:
        cmd.extend(["-e", f"RUNTIME_UID={runtime_uid}"])
    if runtime_gid is not None:
        cmd.extend(["-e", f"RUNTIME_GID={runtime_gid}"])

    # Session token for gateway authentication
    if session_token:
        cmd.extend(["-e", f"EGG_SESSION_TOKEN={session_token}"])

    # --- Mode-specific network settings ---

    if network.repo_mode == "private":
        proxy = network.proxy_url or f"http://{network.gateway_hostname}:{GATEWAY_PROXY_PORT}"
        no_proxy = f"localhost,127.0.0.1,{network.gateway_hostname}"
        cmd.extend(
            [
                # Disable DNS — fail closed
                "--dns",
                "0.0.0.0",
                "-e",
                "PRIVATE_MODE=true",
                "-e",
                f"HTTP_PROXY={proxy}",
                "-e",
                f"HTTPS_PROXY={proxy}",
                "-e",
                f"http_proxy={proxy}",
                "-e",
                f"https_proxy={proxy}",
                "-e",
                f"NO_PROXY={no_proxy}",
                "-e",
                f"no_proxy={no_proxy}",
            ]
        )
    else:
        cmd.extend(["-e", "PRIVATE_MODE=false"])

    # Caller-specific extras
    if extra_env:
        for key, value in extra_env.items():
            cmd.extend(["-e", f"{key}={value}"])

    if extra_args:
        cmd.extend(extra_args)

    # Image is always last — callers insert mounts before it, append
    # commands after it.
    cmd.append(image)

    return cmd
