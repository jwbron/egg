"""RuntimeContext for environment-varying configuration.

Captures all parameters that differ between local development and
GitHub Actions (network names, image names, config paths, etc.).
Each module reads from the context instead of hardcoded constants,
eliminating the duplication between egg_lib and action/entrypoint.sh.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .config import (
    EGG_EXTERNAL_NETWORK,
    EGG_EXTERNAL_SUBNET,
    EGG_ISOLATED_NETWORK,
    EGG_ISOLATED_SUBNET,
    GATEWAY_CONTAINER_NAME,
    GATEWAY_EXTERNAL_IP,
    GATEWAY_IMAGE_NAME,
    GATEWAY_ISOLATED_IP,
    GATEWAY_PORT,
    GATEWAY_PROXY_PORT,
    ORCHESTRATOR_CONTAINER_NAME,
    ORCHESTRATOR_EXTERNAL_IP,
    ORCHESTRATOR_IMAGE_NAME,
    ORCHESTRATOR_ISOLATED_IP,
    ORCHESTRATOR_PORT,
    Config,
)

# Sentinel for "allocate dynamically at network creation time"
AUTO = "auto"


@dataclass
class RuntimeContext:
    """All environment-varying parameters for egg orchestration.

    Defaults match current hardcoded values so the local flow is
    unchanged (zero behavior change for ``egg`` / ``egg --exec``).
    """

    # -- Network --
    isolated_network: str = EGG_ISOLATED_NETWORK
    external_network: str = EGG_EXTERNAL_NETWORK
    isolated_subnet: str = EGG_ISOLATED_SUBNET
    external_subnet: str = EGG_EXTERNAL_SUBNET
    gateway_isolated_ip: str = GATEWAY_ISOLATED_IP
    gateway_external_ip: str = GATEWAY_EXTERNAL_IP

    # -- Images --
    gateway_image: str = GATEWAY_IMAGE_NAME
    sandbox_image: str = Config.IMAGE_NAME
    skip_build: bool = False

    # -- Gateway --
    gateway_container_name: str = GATEWAY_CONTAINER_NAME
    ephemeral: bool = False
    publish_ports: bool = True

    # -- Config --
    config_dir: Path = field(default_factory=lambda: Config.USER_CONFIG_DIR)
    launcher_secret: str | None = None

    # -- Orchestrator --
    orchestrator_container_name: str = ORCHESTRATOR_CONTAINER_NAME
    orchestrator_image: str = ORCHESTRATOR_IMAGE_NAME
    orchestrator_port: int = ORCHESTRATOR_PORT
    orchestrator_isolated_ip: str = ORCHESTRATOR_ISOLATED_IP
    orchestrator_external_ip: str = ORCHESTRATOR_EXTERNAL_IP

    # -- API --
    gateway_port: int = GATEWAY_PORT
    gateway_proxy_port: int = GATEWAY_PROXY_PORT

    # -- Env var prefix used by from_environment() --
    _ENV_PREFIX: ClassVar[str] = "EGG_"

    @classmethod
    def from_environment(cls) -> RuntimeContext:
        """Build a RuntimeContext from ``EGG_*`` environment variables.

        Unset variables keep their defaults (local-dev values).
        """
        ctx = cls()

        def _env(name: str) -> str | None:
            return os.environ.get(f"{cls._ENV_PREFIX}{name}")

        def _env_bool(name: str) -> bool | None:
            val = _env(name)
            if val is None:
                return None
            return val.lower() in ("true", "1", "yes")

        def _env_int(name: str) -> int | None:
            val = _env(name)
            if val is None:
                return None
            return int(val)

        # Network
        if v := _env("ISOLATED_NETWORK"):
            ctx.isolated_network = v
        if v := _env("EXTERNAL_NETWORK"):
            ctx.external_network = v
        if v := _env("ISOLATED_SUBNET"):
            ctx.isolated_subnet = v
        if v := _env("EXTERNAL_SUBNET"):
            ctx.external_subnet = v
        if v := _env("GATEWAY_ISOLATED_IP"):
            ctx.gateway_isolated_ip = v
        if v := _env("GATEWAY_EXTERNAL_IP"):
            ctx.gateway_external_ip = v

        # Images
        if v := _env("GATEWAY_IMAGE"):
            ctx.gateway_image = v
        if v := _env("SANDBOX_IMAGE"):
            ctx.sandbox_image = v
        if (b := _env_bool("SKIP_BUILD")) is not None:
            ctx.skip_build = b

        # Gateway
        if v := _env("GATEWAY_CONTAINER_NAME"):
            ctx.gateway_container_name = v
        if (b := _env_bool("EPHEMERAL")) is not None:
            ctx.ephemeral = b
        if (b := _env_bool("PUBLISH_GATEWAY_PORTS")) is not None:
            ctx.publish_ports = b

        # Orchestrator
        if v := _env("ORCHESTRATOR_CONTAINER_NAME"):
            ctx.orchestrator_container_name = v
        if v := _env("ORCHESTRATOR_IMAGE"):
            ctx.orchestrator_image = v
        if (n := _env_int("ORCHESTRATOR_PORT")) is not None:
            ctx.orchestrator_port = n
        if v := _env("ORCHESTRATOR_ISOLATED_IP"):
            ctx.orchestrator_isolated_ip = v
        if v := _env("ORCHESTRATOR_EXTERNAL_IP"):
            ctx.orchestrator_external_ip = v

        # Config
        if v := _env("CONFIG_DIR"):
            ctx.config_dir = Path(v)
        if v := _env("LAUNCHER_SECRET"):
            ctx.launcher_secret = v

        # API
        if (n := _env_int("GATEWAY_PORT")) is not None:
            ctx.gateway_port = n
        if (n := _env_int("GATEWAY_PROXY_PORT")) is not None:
            ctx.gateway_proxy_port = n

        return ctx


# ---------------------------------------------------------------------------
# Module-level context singleton
# ---------------------------------------------------------------------------

_context: RuntimeContext | None = None


def get_context() -> RuntimeContext:
    """Return the active RuntimeContext, creating a default if needed."""
    global _context
    if _context is None:
        _context = RuntimeContext()
    return _context


def set_context(ctx: RuntimeContext) -> None:
    """Set the active RuntimeContext (call early in the entry point)."""
    global _context
    _context = ctx
