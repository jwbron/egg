"""Private mode configuration for jib.

This module manages per-container repository visibility modes:

- private: Private/internal repos only, container on isolated network with proxy
- public: Public repos only, container on external network with direct internet

Mode is determined solely by CLI flags (--private or --public), with no
persistent state between invocations. Default is public mode.

The gateway sidecar always runs with locked-down Squid (PRIVATE_MODE=true).
Only private containers route through the proxy; public containers bypass it.
This allows private and public containers to run simultaneously without
gateway restarts.
"""

import json
import urllib.request
from enum import Enum

from .output import info


class PrivateMode(Enum):
    """Private mode options for jib.

    PRIVATE: Private repos only, container on isolated network with proxy
    PUBLIC: Public repos only, container on external network (direct internet)
    """

    PRIVATE = "private"
    PUBLIC = "public"


def get_private_mode_env_vars(mode: PrivateMode) -> dict[str, str]:
    """Get environment variables for the given private mode.

    Note: These are no longer used for gateway configuration (gateway always
    runs locked). Kept for backward compatibility with any code that calls this.

    Args:
        mode: The private mode.

    Returns:
        Dict of environment variable names to values.
    """
    if mode == PrivateMode.PRIVATE:
        return {"PRIVATE_MODE": "true"}
    else:
        return {"PRIVATE_MODE": "false"}


def get_gateway_current_mode() -> PrivateMode | None:
    """Query the gateway's status via health endpoint.

    Note: The gateway now always runs in locked mode (PRIVATE_MODE=true).
    This function is kept for backward compatibility and health checking.

    Returns:
        PrivateMode based on health response, or None if gateway is not reachable.
    """
    try:
        with urllib.request.urlopen("http://localhost:9847/api/v1/health", timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
            # Gateway always reports private_mode=true now (locked Squid)
            # But we still parse the response for backward compatibility
            if data.get("private_mode"):
                return PrivateMode.PRIVATE
            else:
                return PrivateMode.PUBLIC
    except Exception:
        return None


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
        True if gateway is running, False if not reachable.
    """
    current_mode = get_gateway_current_mode()

    if current_mode is None:
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
