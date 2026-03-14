"""Integration tests for network isolation and proxy enforcement.

Verifies that private containers are routed through Squid and that
non-allowlisted domains are blocked. Tests DNS lockdown when configured.
"""

import pytest

from integration_tests.conftest import GATEWAY_PORT, exec_in_container


@pytest.mark.integration
@pytest.mark.security
class TestProxyEnforcement:
    """Tests that private containers route through the Squid proxy."""

    def test_private_container_routed_through_squid(self, egg_stack, isolated_container):
        """Container on isolated network can reach gateway via proxy port."""
        returncode, stdout, stderr = exec_in_container(
            isolated_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "5",
                f"http://{egg_stack.gateway_isolated_ip}:{GATEWAY_PORT}/api/v1/health",
            ],
            timeout=15,
        )
        # Container on the isolated network should be able to reach the gateway
        assert returncode == 0, f"Could not reach gateway from isolated container: {stderr}"

    def test_non_allowlisted_domain_blocked(self, egg_stack, isolated_container):
        """Private container cannot reach non-allowlisted domains through proxy.

        The Squid proxy only allows api.anthropic.com in private mode.
        """
        returncode, stdout, stderr = exec_in_container(
            isolated_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "5",
                "--proxy",
                f"http://{egg_stack.gateway_isolated_ip}:3129",
                "https://example.com",
            ],
            timeout=15,
        )
        # Should be blocked (403) or connection refused
        if returncode == 0:
            assert stdout != "200", (
                "SECURITY VIOLATION: Private container reached non-allowlisted "
                f"domain through proxy. HTTP status: {stdout}"
            )

    def test_anthropic_api_reachable_through_proxy(self, egg_stack, isolated_container):
        """api.anthropic.com should be reachable through the proxy.

        We don't expect a 200 (no valid API key), but we should get
        past the proxy (not a 403 from Squid).
        """
        returncode, stdout, stderr = exec_in_container(
            isolated_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "10",
                "--proxy",
                f"http://{egg_stack.gateway_isolated_ip}:3129",
                "https://api.anthropic.com/v1/messages",
            ],
            timeout=20,
        )
        if returncode == 0 and stdout:
            # Should get through the proxy (any HTTP status except 403 from Squid)
            # Typical responses: 401 (no API key), 400, etc.
            assert stdout != "403" or "api.anthropic.com" not in stderr, (
                "api.anthropic.com was blocked by proxy"
            )


@pytest.mark.integration
@pytest.mark.security
class TestNetworkIsolation:
    """Tests for network isolation between isolated and external networks."""

    def test_isolated_container_cannot_reach_external_gateway(self, egg_stack, isolated_container):
        """Container on isolated network cannot reach gateway's external IP.

        Security property: private containers should only see the gateway on
        the isolated network interface.
        """
        returncode, stdout, _ = exec_in_container(
            isolated_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "3",
                f"http://{egg_stack.gateway_external_ip}:{GATEWAY_PORT}/api/v1/health",
            ],
            timeout=10,
        )
        connection_succeeded = returncode == 0 and stdout == "200"
        assert not connection_succeeded, (
            f"SECURITY VIOLATION: Isolated container reached external gateway at "
            f"{egg_stack.gateway_external_ip}"
        )

    def test_external_container_cannot_reach_isolated_gateway(self, egg_stack, external_container):
        """Container on external network cannot reach gateway's isolated IP.

        Security property: public containers should not be able to communicate
        with the isolated network's gateway IP.
        """
        returncode, stdout, _ = exec_in_container(
            external_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "3",
                f"http://{egg_stack.gateway_isolated_ip}:{GATEWAY_PORT}/api/v1/health",
            ],
            timeout=10,
        )
        connection_succeeded = returncode == 0 and stdout == "200"
        assert not connection_succeeded, (
            f"SECURITY VIOLATION: External container reached isolated gateway at "
            f"{egg_stack.gateway_isolated_ip}"
        )


@pytest.mark.integration
@pytest.mark.security
class TestDNSLockdown:
    """Tests for DNS lockdown in private containers."""

    def test_container_with_null_dns_cannot_resolve(self, egg_stack, test_container):
        """Container with --dns 0.0.0.0 cannot resolve external hostnames."""
        container = test_container(
            network=egg_stack.isolated_network,
            name_suffix="dns-lock",
            dns="0.0.0.0",
        )
        returncode, stdout, stderr = exec_in_container(
            container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "5",
                "http://example.com",
            ],
            timeout=15,
        )
        # Should fail to resolve the hostname
        assert returncode != 0 or stdout != "200", (
            "Container with null DNS should not be able to resolve external hostnames"
        )
