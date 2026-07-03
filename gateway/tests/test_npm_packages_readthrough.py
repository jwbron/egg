"""
Tests for the GitHub Packages npm read-through (#3456).

The feature is a token-gated Squid overlay: when the operator provisions
an ``npm-packages-token`` secret, ``entrypoint.sh`` renders two include
files that (1) TLS-bump the GitHub Packages npm registry host and inject
the operator's read-only token, and (2) allow GET/HEAD-only access to the
registry plus spliced access to the SAS-signed blob host tarball
downloads redirect to. Without the secret, the includes are empty files
and Squid behavior is byte-identical to the splice-only baseline.

These tests pin the structural invariants that make that design safe:
include ordering inside squid.conf, the strip-then-inject header rules,
the GET/HEAD-only access shape, and the hosts staying out of the general
``allowed_domains.txt`` (they must only ever ride the token-gated path).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).parent.parent
SQUID_CONF = GATEWAY_DIR / "squid.conf"
SSL_TEMPLATE = GATEWAY_DIR / "squid-npm-packages-ssl.conf.template"
ACCESS_TEMPLATE = GATEWAY_DIR / "squid-npm-packages-access.conf.template"
ALLOWED_DOMAINS = GATEWAY_DIR / "allowed_domains.txt"

TOKEN_PLACEHOLDER = "@NPM_PACKAGES_TOKEN@"
REGISTRY_HOST = "npm.pkg.github.com"
# GitHub's npm blob host, from `domains.packages` in
# https://api.github.com/meta — tarball downloads 302-redirect here with
# SAS-signed URLs.
BLOB_HOST = "npmregistryv2prod.blob.core.windows.net"


def _non_comment_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestSquidConfIncludes:
    """squid.conf must include the overlay files at the right positions."""

    def test_ssl_include_between_peek_and_splice(self):
        """The bump include must come after peek and before the general splice.

        ssl_bump rules are first-match: if the include came after
        ``ssl_bump splice allowed_domains`` the bump could never win for
        the registry host, and before ``peek step1`` the SNI wouldn't be
        read yet.
        """
        lines = _non_comment_lines(SQUID_CONF)
        peek_idx = lines.index("ssl_bump peek step1")
        include_idx = lines.index("include /etc/squid/conf.d/npm-packages-ssl.conf")
        splice_idx = lines.index("ssl_bump splice allowed_domains")
        assert peek_idx < include_idx < splice_idx, (
            "npm-packages-ssl.conf include must sit between 'ssl_bump peek "
            "step1' and 'ssl_bump splice allowed_domains' — first-match "
            "ordering decides whether the registry host gets bumped."
        )

    def test_access_include_before_generic_allows(self):
        """Access rules must precede the generic allowed_domains allows.

        http_access is also first-match; the overlay's GET/HEAD-only allow
        and explicit deny must be evaluated before the generic rules and
        the final ``http_access deny all``.
        """
        lines = _non_comment_lines(SQUID_CONF)
        include_idx = lines.index("include /etc/squid/conf.d/npm-packages-access.conf")
        generic_idx = lines.index("http_access allow CONNECT localnet allowed_domains")
        deny_all_idx = lines.index("http_access deny all")
        assert include_idx < generic_idx < deny_all_idx


class TestSslTemplate:
    """The bump/credential-injection template must keep its safety shape."""

    def test_placeholder_present_exactly_once(self):
        """entrypoint.sh renders with a single sed substitution."""
        assert SSL_TEMPLATE.read_text().count(TOKEN_PLACEHOLDER) == 1

    def test_bumps_only_the_registry_host(self):
        """Exactly one bump rule, scoped to the registry host ACL."""
        bump_lines = [
            line for line in _non_comment_lines(SSL_TEMPLATE) if line.startswith("ssl_bump bump")
        ]
        assert bump_lines == ["ssl_bump bump npm_pkg_registry"]

    def test_blob_host_is_spliced_not_bumped(self):
        lines = _non_comment_lines(SSL_TEMPLATE)
        assert "ssl_bump splice npm_pkg_blob" in lines

    def test_client_authorization_stripped_before_injection(self):
        """The sandbox must never choose its own token.

        The deny (strip) rule must appear before the add rule so a
        client-supplied Authorization header can't reach the registry or
        duplicate the injected one.
        """
        lines = _non_comment_lines(SSL_TEMPLATE)
        deny_idx = lines.index("request_header_access Authorization deny npm_pkg_registry_dst")
        allow_idx = lines.index("request_header_access Authorization allow all")
        add_idx = next(
            i for i, line in enumerate(lines) if line.startswith("request_header_add Authorization")
        )
        assert deny_idx < allow_idx, "deny must precede the allow-all fallback (first-match)"
        assert f'"Bearer {TOKEN_PLACEHOLDER}"' in lines[add_idx]
        assert lines[add_idx].endswith("npm_pkg_registry_dst"), (
            "token injection must be scoped to the registry host ACL"
        )

    def test_rendering_replaces_placeholder(self):
        """Simulate the entrypoint's sed: rendered output carries the token."""
        rendered = SSL_TEMPLATE.read_text().replace(TOKEN_PLACEHOLDER, "ghp_exampletoken")
        assert TOKEN_PLACEHOLDER not in rendered
        assert 'request_header_add Authorization "Bearer ghp_exampletoken" ' in rendered


class TestAccessTemplate:
    """GET/HEAD-only enforcement for the decrypted registry requests."""

    def test_no_token_placeholder(self):
        """The access include is world-readable; it must never embed the token."""
        assert TOKEN_PLACEHOLDER not in ACCESS_TEMPLATE.read_text()

    def test_read_only_methods_then_deny(self):
        lines = _non_comment_lines(ACCESS_TEMPLATE)
        allow_idx = lines.index(
            "http_access allow localnet npm_pkg_registry_dst npm_pkg_read_methods"
        )
        deny_idx = lines.index("http_access deny npm_pkg_registry_dst")
        assert allow_idx < deny_idx, (
            "read-methods allow must precede the registry deny (first-match); "
            "the deny makes publish/unpublish structurally impossible."
        )

    def test_read_methods_acl_is_get_head_only(self):
        lines = _non_comment_lines(SSL_TEMPLATE)
        assert "acl npm_pkg_read_methods method GET HEAD" in lines


class TestHostsStayOutOfGeneralAllowlist:
    """Both hosts must only ever ride the token-gated include.

    In allowed_domains.txt they would be spliced (no credential injection,
    no method restriction, host-level-only audit) and reachable without
    the operator opting in.
    """

    def test_registry_and_blob_hosts_absent_from_allowed_domains(self):
        for host in (REGISTRY_HOST, BLOB_HOST):
            for line in _non_comment_lines(ALLOWED_DOMAINS):
                assert host not in line.lower(), (
                    f"{host!r} found in allowed_domains.txt: {line!r}. The "
                    "GitHub Packages read-through hosts belong exclusively "
                    "in the token-gated conf.d includes (#3456)."
                )


class TestCaCertRoute:
    """The /api/v1/proxy/ca-cert route serves the current gateway CA."""

    def test_ca_cert_served(self, tmp_path, monkeypatch):
        os.environ.setdefault("EGG_LAUNCHER_SECRET", "test-launcher-secret-12345")
        import gateway

        gateway.app.config["TESTING"] = True
        pem = "-----BEGIN CERTIFICATE-----\nMIIFAKE\n-----END CERTIFICATE-----\n"
        ca = tmp_path / "gateway-ca.crt"
        ca.write_text(pem)

        real_is_file = Path.is_file
        real_read_text = Path.read_text

        def fake_is_file(self):
            if str(self) == "/etc/squid/certs/gateway-ca.crt":
                return True
            return real_is_file(self)

        def fake_read_text(self, *args, **kwargs):
            if str(self) == "/etc/squid/certs/gateway-ca.crt":
                return real_read_text(ca)
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", fake_is_file)
        monkeypatch.setattr(Path, "read_text", fake_read_text)

        with gateway.app.test_client() as client:
            resp = client.get("/api/v1/proxy/ca-cert")
        assert resp.status_code == 200
        assert resp.data.decode() == pem
        assert resp.mimetype == "application/x-pem-file"

    def test_ca_cert_missing_returns_404(self, monkeypatch):
        os.environ.setdefault("EGG_LAUNCHER_SECRET", "test-launcher-secret-12345")
        import gateway

        gateway.app.config["TESTING"] = True

        real_is_file = Path.is_file

        def fake_is_file(self):
            if str(self) == "/etc/squid/certs/gateway-ca.crt":
                return False
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", fake_is_file)

        with gateway.app.test_client() as client:
            resp = client.get("/api/v1/proxy/ca-cert")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "ca_cert_unavailable"


def _squid_supports_ssl_bump(squid_bin: str) -> bool:
    """True if the squid binary was built with TLS-bump (``ssl::server_name``) support."""
    try:
        out = subprocess.run([squid_bin, "-v"], capture_output=True, text=True, timeout=10)
    except OSError, subprocess.SubprocessError:
        return False
    blob = (out.stdout + out.stderr).lower()
    return "--enable-ssl" in blob or "--with-openssl" in blob


class TestRenderedConfigParses:
    """`squid -k parse` over the rendered config catches template syntax regressions.

    The text-level tests above pin ordering and rule shape but can't catch a
    genuine Squid syntax error introduced by a future template edit. This
    runs the real parser over the rendered includes wired into a minimal
    harness that reproduces the surrounding ``squid.conf`` context.

    Skipped unless a ``squid`` binary with TLS-bump support *and* ``openssl``
    are available — the pure-Python test image has neither, so this is a
    bonus guard for environments that do (the gateway container, a dev box).
    The deploy-time backstop is ``entrypoint.sh``'s ``squid -k check``, which
    already fails container startup on an invalid rendered config.
    """

    def test_rendered_includes_parse(self, tmp_path):
        squid_bin = shutil.which("squid") or shutil.which("squid3")
        if not squid_bin:
            pytest.skip("squid binary not available")
        if not _squid_supports_ssl_bump(squid_bin):
            pytest.skip("squid built without TLS-bump support")
        openssl = shutil.which("openssl")
        if not openssl:
            pytest.skip("openssl not available to generate a bump cert")

        # Throwaway self-signed cert for the ssl-bump port (parse-time only).
        cert = tmp_path / "bump.pem"
        key = tmp_path / "bump.key"
        gen = subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=egg-test",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if gen.returncode != 0:
            pytest.skip(f"openssl cert generation failed: {gen.stderr.strip()}")

        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        ssl_include = conf_d / "npm-packages-ssl.conf"
        access_include = conf_d / "npm-packages-access.conf"
        # Render exactly as entrypoint.sh does: sed-substitute the token into
        # the ssl include; copy the access include verbatim.
        ssl_include.write_text(
            SSL_TEMPLATE.read_text().replace(TOKEN_PLACEHOLDER, "ghp_exampletoken")
        )
        access_include.write_text(ACCESS_TEMPLATE.read_text())

        # Minimal harness: the ssl-bump port plus the ACLs the includes
        # reference from the surrounding config (localnet, step1,
        # allowed_domains, CONNECT), with the includes wired in at the same
        # relative positions as squid.conf (ssl include between peek and
        # splice; access include before the generic allows). CONNECT is not a
        # Squid built-in — squid.conf defines it with `acl CONNECT method
        # CONNECT`, and the access include's `http_access allow CONNECT ...`
        # rules reference it, so the harness must define it too or `squid -k
        # parse` aborts with "ACL name 'CONNECT' not found".
        harness = tmp_path / "squid.conf"
        harness.write_text(
            "\n".join(
                [
                    "http_port 3128",
                    f"https_port 3130 ssl-bump generate-host-certificates=on cert={cert} key={key}",
                    "acl localnet src 10.0.0.0/8",
                    "acl step1 at_step SslBump1",
                    "acl allowed_domains ssl::server_name .example.org",
                    "acl CONNECT method CONNECT",
                    "ssl_bump peek step1",
                    f"include {ssl_include}",
                    "ssl_bump splice allowed_domains",
                    "ssl_bump terminate all",
                    f"include {access_include}",
                    "http_access allow localnet",
                    "http_access deny all",
                    "",
                ]
            )
        )

        result = subprocess.run(
            [squid_bin, "-k", "parse", "-f", str(harness)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"squid -k parse rejected the rendered npm read-through config:\n{result.stderr}"
        )
