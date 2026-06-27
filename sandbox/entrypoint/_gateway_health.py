"""Gateway readiness / network-lockdown health check."""

from __future__ import annotations

import os
import subprocess
import time

from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT

from ._config import Config, Logger


def check_gateway_health(config: Config, logger: Logger) -> bool:
    """Wait for gateway readiness before starting.

    In network lockdown mode, the container cannot reach the internet directly.
    All traffic must go through the gateway's proxy. This function ensures
    the gateway and proxy are ready before the agent starts.

    Returns:
        True if gateway is ready, False on timeout
    """
    import socket

    import requests
    from requests.exceptions import RequestException

    gateway_url = os.environ.get("GATEWAY_URL", f"http://egg-gateway:{GATEWAY_PORT}")
    proxy_url = os.environ.get("HTTPS_PROXY")

    # Parse gateway hostname from URL (supports dynamic names in GHA)
    from urllib.parse import urlparse

    parsed = urlparse(gateway_url)
    gateway_host = parsed.hostname or "egg-gateway"

    # Detect network mode from EGG_PRIVATE_MODE env var (set by orchestrator/gateway)
    # Fallback: if EGG_PRIVATE_MODE is not set, assume private when proxy is configured
    private_mode_env = os.environ.get("EGG_PRIVATE_MODE", "").lower()
    if private_mode_env in ("true", "1"):
        is_private_mode = True
    elif private_mode_env in ("false", "0"):
        is_private_mode = False
    else:
        # Legacy fallback: infer from proxy presence
        is_private_mode = proxy_url is not None
    if is_private_mode:
        logger.info("Network mode: PRIVATE (lockdown, proxy filtering)")
    else:
        logger.info("Network mode: PUBLIC (direct internet access)")

    # Log configuration for debugging
    logger.info("Gateway configuration:")
    logger.info(f"  GATEWAY_URL: {gateway_url}")
    if is_private_mode:
        logger.info(f"  HTTPS_PROXY: {proxy_url}")
    else:
        logger.info("  HTTPS_PROXY: (not set - direct internet access)")

    # Check hostname resolution
    try:
        resolved_ip = socket.gethostbyname(gateway_host)
        logger.info(f"  {gateway_host} resolves to: {resolved_ip}")
    except socket.gaierror as e:
        logger.error(f"  DNS resolution failed for {gateway_host}: {e}")
        logger.error("  Check --add-host configuration in container startup")

    # Show /etc/hosts entry for gateway
    try:
        with open("/etc/hosts") as f:
            hosts_content = f.read()
            for line in hosts_content.splitlines():
                if gateway_host in line:
                    logger.info(f"  /etc/hosts entry: {line.strip()}")
                    break
            else:
                logger.warn(f"  No /etc/hosts entry found for {gateway_host}")
    except Exception as e:
        logger.warn(f"  Could not read /etc/hosts: {e}")

    # Show network interfaces and verify container is on expected subnet
    # Private mode: egg-isolated (172.32.0.x), Public mode: egg-external (172.33.0.x)
    expected_subnet = "172.32.0." if is_private_mode else "172.33.0."
    network_name = "egg-isolated" if is_private_mode else "egg-external"
    found_expected_subnet = False
    container_ip = None

    try:
        result = subprocess.run(
            ["ip", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            logger.info("  Network interfaces:")
            for line in lines:
                if "inet " in line and "127.0.0.1" not in line:
                    logger.info(f"    {line.strip()}")
                    if expected_subnet in line:
                        found_expected_subnet = True
                        # Extract IP address from line like "inet 172.32.0.5/24 ..."
                        parts = line.strip().split()
                        for i, part in enumerate(parts):
                            if part == "inet" and i + 1 < len(parts):
                                container_ip = parts[i + 1].split("/")[0]
                                break

            if found_expected_subnet:
                logger.info(f"  ✓ Container on {network_name} network ({container_ip})")
            else:
                logger.warn(f"  ✗ Not on {network_name} subnet ({expected_subnet}x)!")
                logger.warn("    Container may not be on the correct network")
    except Exception as e:
        logger.warn(f"  Could not get network interfaces: {e}")

    # Test basic TCP connectivity to gateway ports
    logger.info("Testing TCP connectivity to gateway...")

    def test_tcp_port(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
        """Test TCP connectivity to a host:port."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                return True, "connected"
            else:
                return False, f"connection refused (errno {result})"
        except TimeoutError:
            return False, "timeout"
        except socket.gaierror as e:
            return False, f"DNS error: {e}"
        except Exception as e:
            return False, f"error: {e}"

    api_port = parsed.port or GATEWAY_PORT
    proxy_port = GATEWAY_PROXY_PORT

    api_tcp_ok, api_tcp_msg = test_tcp_port(gateway_host, api_port)
    proxy_tcp_ok, proxy_tcp_msg = test_tcp_port(gateway_host, proxy_port)

    logger.info(
        f"  TCP {gateway_host}:{api_port} (API): {'✓' if api_tcp_ok else '✗'} {api_tcp_msg}"
    )
    logger.info(
        f"  TCP {gateway_host}:{proxy_port} (Proxy): {'✓' if proxy_tcp_ok else '✗'} {proxy_tcp_msg}"
    )

    if not api_tcp_ok and not proxy_tcp_ok:
        logger.error("  Cannot reach gateway on either port!")
        logger.error("  This indicates a network configuration issue.")
        logger.error("  Verify egg container and egg-gateway are on the same network.")

    logger.info("Waiting for gateway readiness...")

    # Timeout is configurable via EGG_GATEWAY_TIMEOUT for faster test feedback.
    # Default 300s for production to survive orchestrator/gateway restarts.
    # Tests can set lower values (e.g. EGG_GATEWAY_TIMEOUT=5).
    timeout_str = os.environ.get("EGG_GATEWAY_TIMEOUT", "300")
    try:
        timeout = int(timeout_str)
    except ValueError:
        logger.warn(f"Invalid EGG_GATEWAY_TIMEOUT '{timeout_str}', using default 300s")
        timeout = 300
    interval: float = 2  # seconds — initial backoff interval
    max_interval = 30  # cap for exponential backoff
    elapsed: float = 0

    # Track which checks have passed for final diagnostic
    api_health_passed = False
    api_health_error = None
    proxy_check_passed = False
    proxy_check_error = None
    tcp_api_ok = api_tcp_ok
    tcp_proxy_ok = proxy_tcp_ok

    while elapsed < timeout:
        # Check 1: Gateway API health endpoint
        try:
            health_url = f"{gateway_url}/api/v1/health"
            health_response = requests.get(
                health_url,
                timeout=5,
                proxies={"http": "", "https": ""},
            )
            if health_response.status_code == 200:
                # Parse health response to check actual status
                try:
                    health_data = health_response.json()
                    health_status = health_data.get("status", "unknown")
                    github_token_valid = health_data.get("github_token_valid", False)
                    auth_configured = health_data.get("auth_configured", False)

                    if not api_health_passed:
                        logger.success(
                            f"  Gateway API responding (HTTP {health_response.status_code})"
                        )
                        logger.info(f"    Status: {health_status}")
                        logger.info(f"    GitHub token valid: {github_token_valid}")
                        logger.info(f"    Auth configured: {auth_configured}")

                    if health_status == "healthy":
                        api_health_passed = True
                    else:
                        # Gateway is responding but not fully healthy
                        api_health_error = f"Status: {health_status} (github_token={github_token_valid}, auth={auth_configured})"
                        if not api_health_passed and not config.quiet:
                            logger.warn(f"  Gateway degraded: {api_health_error}")
                        # Still proceed to proxy check - degraded might still work
                        api_health_passed = True
                except (ValueError, KeyError) as e:
                    # Could not parse JSON response
                    api_health_error = f"Invalid JSON response: {e}"
                    if not config.quiet:
                        logger.warn(
                            f"  Gateway API returned non-JSON: {health_response.text[:100]}"
                        )
                    api_health_passed = True  # Proceed anyway - API is responding
            else:
                api_health_error = (
                    f"HTTP {health_response.status_code}: {health_response.text[:100]}"
                )
                if not config.quiet:
                    logger.info(f"  Gateway API returned: {api_health_error}")

        except RequestException as e:
            api_health_error = f"{type(e).__name__}: {e}"
            if not config.quiet and elapsed % 10 < interval:  # Log every ~10 seconds
                logger.info(f"  Gateway API check failed: {api_health_error}")

        # Check 2: Proxy connectivity (only in private mode, only if API is healthy)
        # In public mode, the container has direct internet access and doesn't use the proxy
        if api_health_passed:
            if not is_private_mode:
                # Public mode: no proxy check needed, gateway API is sufficient
                logger.success("Gateway ready! (public mode - direct internet access)")
                return True

            # Private mode: verify proxy (Squid) is reachable and responding.
            # Use plain HTTP (not HTTPS) so this check doesn't depend on Squid's
            # CA cert for SSL termination. api.anthropic.com is intentionally NOT
            # in the Squid allowlist — actual Anthropic traffic goes via
            # ANTHROPIC_BASE_URL directly to the gateway, bypassing Squid.
            # Squid returns 403 for the blocked domain, proving it's reachable.
            try:
                if proxy_url is None:
                    raise RuntimeError("proxy_url must be set in private mode")
                proxies = {"http": proxy_url, "https": proxy_url}
                api_response = requests.get(
                    "http://api.anthropic.com/",
                    proxies=proxies,
                    timeout=10,
                )
                # 403 from Squid proves proxy is reachable (domain is blocked by design).
                # 200/401/404 would be unexpected but also indicate connectivity.
                if api_response.status_code in (200, 401, 403, 404):
                    logger.success(
                        f"  Proxy connectivity verified (Squid returned HTTP {api_response.status_code})"
                    )
                    logger.success("Gateway ready!")
                    return True
                else:
                    proxy_check_error = f"Unexpected HTTP {api_response.status_code}"
                    if not config.quiet:
                        logger.info(f"  Proxy check: {proxy_check_error}")

            except RequestException as e:
                proxy_check_error = f"{type(e).__name__}: {e}"
                if not config.quiet and elapsed % 10 < interval:
                    logger.info(f"  Proxy check failed: {proxy_check_error}")

        if not config.quiet and elapsed > 0 and elapsed % 10 < interval:
            logger.info(f"  Still waiting... ({elapsed}/{timeout}s)")

        time.sleep(interval)
        elapsed += interval
        # Exponential backoff: grow interval by 1.5x, capped at max_interval.
        # Reset to 2s if the API health check has passed (partial progress).
        if api_health_passed:
            interval = 2
        else:
            interval = min(interval * 1.5, max_interval)

    # Final diagnostic output
    logger.error(f"Gateway not ready after {timeout} seconds")
    logger.error("")
    logger.error("Diagnostic summary:")
    logger.error(f"  TCP connectivity to {gateway_host}:")
    logger.error(
        f"    Port {api_port} (API): {'✓ connected' if tcp_api_ok else '✗ ' + api_tcp_msg}"
    )
    logger.error(
        f"    Port {proxy_port} (Proxy): {'✓ connected' if tcp_proxy_ok else '✗ ' + proxy_tcp_msg}"
    )
    logger.error(f"  Gateway API ({gateway_url}/api/v1/health):")
    if api_health_passed:
        logger.error("    ✓ Responding")
    else:
        logger.error(f"    ✗ Failed: {api_health_error}")
    if is_private_mode:
        logger.error(f"  Proxy ({proxy_url} → api.anthropic.com):")
        if proxy_check_passed:
            logger.error("    ✓ Working")
        else:
            logger.error(
                f"    ✗ Failed: {proxy_check_error or 'Not tested (API health check failed first)'}"
            )
    else:
        logger.error("  Proxy: (not used in public mode)")
    logger.error("")

    # Provide targeted troubleshooting based on what failed
    logger.error("Troubleshooting steps:")
    if not tcp_api_ok and not tcp_proxy_ok:
        logger.error("  [Network issue] Cannot reach gateway - check container networking:")
        logger.error("    1. Verify egg-gateway is running: docker ps | grep egg-gateway")
        network_to_check = "egg-isolated" if is_private_mode else "egg-external"
        logger.error(f"    2. Check both containers are on {network_to_check} network:")
        logger.error(f"       docker network inspect {network_to_check}")
        expected_ip = "172.32.0.2" if is_private_mode else "172.33.0.2"
        logger.error(f"    3. Verify gateway has IP {expected_ip} in {network_to_check} network")
        logger.error("    4. Check /etc/hosts has correct egg-gateway entry")
    elif not api_health_passed:
        logger.error("  [API issue] TCP works but HTTP fails - gateway may be starting:")
        logger.error("    1. Check gateway logs: docker logs egg-gateway")
        logger.error(f"    2. Test from host: curl http://localhost:{GATEWAY_PORT}/api/v1/health")
        logger.error("    3. Verify gateway.py is running in container")
    elif is_private_mode:
        logger.error("  [Proxy issue] Gateway API works but proxy check failed:")
        logger.error("    1. Check Squid is running: docker exec egg-gateway squid -k check")
        logger.error(
            "    2. Check Squid logs: docker exec egg-gateway cat /var/log/squid/cache.log"
        )
        logger.error("    3. Test proxy from host (HTTP, not HTTPS):")
        logger.error(
            f"       curl -x http://localhost:{GATEWAY_PROXY_PORT} http://api.anthropic.com/"
        )
        logger.error("    4. If CA cert expired, restart the gateway to regenerate it:")
        logger.error("       docker compose restart gateway")
    return False
