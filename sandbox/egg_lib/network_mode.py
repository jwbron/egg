"""Private mode configuration for egg.

This module manages per-container repository visibility modes:

- private: Private/internal repos only, container on isolated network with proxy
- public: Public repos only, container on external network with direct internet

Mode is determined solely by CLI flags (--private or --public), with no
persistent state between invocations. Default is public mode.

The gateway sidecar always runs with locked-down Squid. Only private containers
route through the proxy; public containers bypass it. This allows private and
public containers to run simultaneously without gateway restarts.
"""

import urllib.request
from enum import Enum

from .config import GATEWAY_PORT
from .output import info


class PrivateMode(Enum):
    """Private mode options for egg.

    PRIVATE: Private repos only, container on isolated network with proxy
    PUBLIC: Public repos only, container on external network (direct internet)
    """

    PRIVATE = "private"
    PUBLIC = "public"


def is_gateway_running() -> bool:
    """Check if the gateway sidecar is running and healthy.

    Returns:
        True if gateway is running and reachable, False otherwise.
    """
    try:
        with urllib.request.urlopen(
            f"http://localhost:{GATEWAY_PORT}/api/v1/health", timeout=2
        ) as response:
            return response.status == 200
    except Exception:
        return False


def ensure_gateway_mode(mode: PrivateMode, quiet: bool = False) -> bool:
    """Verify gateway is running. Mode is per-container, not gateway-wide.

    The gateway always runs with locked-down Squid. Per-container mode
    determines whether the container uses the proxy (private) or has
    direct internet access (public). No gateway restart is needed when
    switching modes.

    Args:
        mode: The desired PrivateMode (for informational logging only).
        quiet: Suppress output messages.

    Returns:
        True (gateway will be started by start_gateway_container() if needed).
    """
    gateway_running = is_gateway_running()

    if not gateway_running:
        # Gateway not running - will be started by start_gateway_container()
        if not quiet:
            info("Gateway not running - will be started")
        return True

    # Gateway is running - no restart needed regardless of requested mode
    # Mode is enforced per-container via network selection
    if not quiet:
        if mode == PrivateMode.PRIVATE:
            info("Mode: PRIVATE (isolated network + proxy + private repos)")
        else:
            info("Mode: PUBLIC (external network + direct internet + public repos)")

    return True
