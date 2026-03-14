"""Security tests for network lockdown proxy.

These tests verify that the Squid proxy correctly:
1. Allows access to allowlisted domains
2. Blocks access to non-allowlisted domains
3. Blocks direct IP address connections (various formats)
4. Implements fail-closed behavior

Note: These are unit tests that verify the ACL configuration is correct.
For full integration testing, use integration_test.sh in a lockdown container.
"""

import re
from pathlib import Path

import pytest

# ACL patterns from squid.conf
# These are the patterns we expect to be configured
EXPECTED_IP_BLOCKING_PATTERNS = {
    "direct_ipv4": r"^https?://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+",
    "direct_ipv6": r"^https?://\[",
    "direct_ip_octal": r"^https?://0[0-7]+\.",
    "direct_ip_hex": r"^https?://0x[0-9a-fA-F]+",
    "direct_ip_int": r"^https?://[0-9]{9,10}(/|$|:)",
}


@pytest.fixture
def squid_conf_path() -> Path:
    """Path to the squid.conf file."""
    return Path(__file__).parent.parent / "squid.conf"


@pytest.fixture
def allowed_domains_path() -> Path:
    """Path to the allowed_domains.txt file."""
    return Path(__file__).parent.parent / "allowed_domains.txt"


class TestSquidConfig:
    """Tests for Squid proxy configuration."""

    def test_squid_conf_exists(self, squid_conf_path: Path):
        """squid.conf should exist."""
        assert squid_conf_path.exists(), f"squid.conf not found at {squid_conf_path}"

    def test_allowed_domains_exists(self, allowed_domains_path: Path):
        """allowed_domains.txt should exist."""
        assert allowed_domains_path.exists()

    def test_uses_docker_dns(self, squid_conf_path: Path):
        """Squid should use Docker's embedded DNS, not external DNS."""
        content = squid_conf_path.read_text()
        # Should use Docker's DNS
        assert "127.0.0.11" in content, "Should use Docker embedded DNS"
        # Should NOT use Google DNS
        assert "8.8.8.8" not in content, "Should not use Google DNS"
        assert "8.8.4.4" not in content, "Should not use Google DNS"


class TestIPBlockingACLs:
    """Tests for IP address blocking ACLs."""

    def test_ipv4_blocking_acl_exists(self, squid_conf_path: Path):
        """IPv4 direct connection blocking ACL should be configured."""
        content = squid_conf_path.read_text()
        assert "direct_ipv4" in content
        assert "url_regex" in content

    def test_ipv6_blocking_acl_exists(self, squid_conf_path: Path):
        """IPv6 direct connection blocking ACL should be configured."""
        content = squid_conf_path.read_text()
        assert "direct_ipv6" in content

    def test_octal_ip_blocking_acl_exists(self, squid_conf_path: Path):
        """Octal IP notation blocking ACL should be configured."""
        content = squid_conf_path.read_text()
        assert "direct_ip_octal" in content

    def test_hex_ip_blocking_acl_exists(self, squid_conf_path: Path):
        """Hexadecimal IP notation blocking ACL should be configured."""
        content = squid_conf_path.read_text()
        assert "direct_ip_hex" in content

    def test_integer_ip_blocking_acl_exists(self, squid_conf_path: Path):
        """Integer IP notation blocking ACL should be configured."""
        content = squid_conf_path.read_text()
        assert "direct_ip_int" in content

    def test_all_ip_acls_are_denied(self, squid_conf_path: Path):
        """All IP blocking ACLs should have http_access deny rules."""
        content = squid_conf_path.read_text()
        for acl_name in EXPECTED_IP_BLOCKING_PATTERNS:
            assert f"http_access deny {acl_name}" in content, f"Missing deny rule for {acl_name}"


class TestIPPatternMatching:
    """Tests that verify IP blocking patterns match expected URLs."""

    @pytest.mark.parametrize(
        ("url", "should_block"),
        [
            # Standard IPv4 - should block
            ("https://192.168.1.1/", True),
            ("http://10.0.0.1:8080/path", True),
            ("https://127.0.0.1/", True),
            ("http://1.2.3.4/", True),
            # Valid domain names - should NOT block
            ("https://api.github.com/", False),
            ("https://github.com/owner/repo", False),
            ("https://api.anthropic.com/v1/messages", False),
        ],
    )
    def test_ipv4_pattern(self, url: str, should_block: bool):
        """IPv4 pattern should match direct IP URLs."""
        pattern = EXPECTED_IP_BLOCKING_PATTERNS["direct_ipv4"]
        matches = re.match(pattern, url) is not None
        assert matches == should_block, f"Pattern match failed for {url}"

    @pytest.mark.parametrize(
        ("url", "should_block"),
        [
            # IPv6 addresses in brackets - should block
            ("https://[::1]/", True),
            ("https://[2607:f8b0:4004:800::200e]/", True),
            ("http://[fe80::1]:8080/", True),
            # Not IPv6 - should NOT block
            ("https://github.com/", False),
            ("https://api.github.com/", False),
        ],
    )
    def test_ipv6_pattern(self, url: str, should_block: bool):
        """IPv6 pattern should match bracketed IPv6 URLs."""
        pattern = EXPECTED_IP_BLOCKING_PATTERNS["direct_ipv6"]
        matches = re.match(pattern, url) is not None
        assert matches == should_block, f"Pattern match failed for {url}"

    @pytest.mark.parametrize(
        ("url", "should_block"),
        [
            # Octal notation - should block (starts with 0 followed by 0-7)
            ("https://0177.0.0.1/", True),  # 127.0.0.1 in octal
            ("http://0127.0.0.1/", True),
            ("https://0100.0250.0377.0001/", True),
            # Not octal - should NOT block
            ("https://github.com/", False),
            ("https://100.200.300.1/", False),  # Starts with 1, not 0
        ],
    )
    def test_octal_pattern(self, url: str, should_block: bool):
        """Octal IP pattern should match octal notation URLs."""
        pattern = EXPECTED_IP_BLOCKING_PATTERNS["direct_ip_octal"]
        matches = re.match(pattern, url) is not None
        assert matches == should_block, f"Pattern match failed for {url}"

    @pytest.mark.parametrize(
        ("url", "should_block"),
        [
            # Hex notation - should block
            ("https://0x7f.0x00.0x00.0x01/", True),  # 127.0.0.1 in hex
            ("http://0xC0.0xA8.0x01.0x01/", True),  # 192.168.1.1
            # Not hex - should NOT block
            ("https://github.com/", False),
            ("https://0example.com/", False),  # Domain starting with 0
        ],
    )
    def test_hex_pattern(self, url: str, should_block: bool):
        """Hex IP pattern should match hexadecimal notation URLs."""
        pattern = EXPECTED_IP_BLOCKING_PATTERNS["direct_ip_hex"]
        matches = re.match(pattern, url) is not None
        assert matches == should_block, f"Pattern match failed for {url}"

    @pytest.mark.parametrize(
        ("url", "should_block"),
        [
            # Integer notation (9-10 digits) - should block
            ("https://2130706433/", True),  # 127.0.0.1 as integer
            ("http://3232235521/", True),  # 192.168.0.1 as integer
            ("https://2130706433:8080/path", True),
            # Not integer IP - should NOT block
            ("https://github.com/", False),
            ("https://123456789.example.com/", False),  # Subdomain with digits
            ("https://12345678/", False),  # Only 8 digits
        ],
    )
    def test_integer_pattern(self, url: str, should_block: bool):
        """Integer IP pattern should match integer notation URLs."""
        pattern = EXPECTED_IP_BLOCKING_PATTERNS["direct_ip_int"]
        matches = re.match(pattern, url) is not None
        assert matches == should_block, f"Pattern match failed for {url}"


class TestAllowedDomains:
    """Tests for allowed domains configuration."""

    def test_anthropic_api_allowed(self, allowed_domains_path: Path):
        """Anthropic API should be in allowed domains."""
        content = allowed_domains_path.read_text()
        assert "api.anthropic.com" in content

    def test_github_domains_not_allowed(self, allowed_domains_path: Path):
        """GitHub domains should NOT be in allowed domains.

        GitHub access MUST go through the gateway sidecar's git/gh wrappers,
        not directly through the proxy. This ensures policy enforcement
        (branch ownership, merge blocking, etc.) cannot be bypassed.
        """
        content = allowed_domains_path.read_text()
        # Extract non-comment lines
        lines = [
            line.strip()
            for line in content.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]
        blocked_domains = [
            "github.com",
            "api.github.com",
            "raw.githubusercontent.com",
            "objects.githubusercontent.com",
            "codeload.github.com",
            "uploads.github.com",
        ]
        for domain in blocked_domains:
            assert domain not in lines, (
                f"GitHub domain '{domain}' found in proxy allowlist. "
                "GitHub access must go through gateway sidecar wrappers."
            )

    def test_no_wildcard_domains(self, allowed_domains_path: Path):
        """Allowed domains should not use wildcards (explicit is safer)."""
        content = allowed_domains_path.read_text()
        # Check for wildcard patterns
        lines = [
            line.strip()
            for line in content.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]
        for line in lines:
            # Squid uses .domain.com for subdomains, not *.domain.com
            assert not line.startswith("*"), f"Wildcard domain found: {line}"


class TestSecurityHardening:
    """Tests for security hardening in Squid config."""

    def test_via_header_disabled(self, squid_conf_path: Path):
        """Via header should be disabled (don't leak proxy info)."""
        content = squid_conf_path.read_text()
        assert "via off" in content

    def test_forwarded_for_deleted(self, squid_conf_path: Path):
        """X-Forwarded-For should be deleted (privacy)."""
        content = squid_conf_path.read_text()
        assert "forwarded_for delete" in content

    def test_version_hidden(self, squid_conf_path: Path):
        """Squid version should be hidden in error pages."""
        content = squid_conf_path.read_text()
        assert "httpd_suppress_version_string on" in content

    def test_caching_disabled(self, squid_conf_path: Path):
        """Caching should be disabled (we're a filtering proxy)."""
        content = squid_conf_path.read_text()
        assert "cache deny all" in content

    def test_ssl_bump_configured(self, squid_conf_path: Path):
        """SSL bump should be configured for SNI inspection."""
        content = squid_conf_path.read_text()
        assert "ssl-bump" in content
        assert "ssl_bump peek" in content
        assert "ssl_bump splice" in content
        assert "ssl_bump terminate" in content
