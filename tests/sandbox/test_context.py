"""Tests for RuntimeContext dataclass and module-level accessors."""

import sys
from pathlib import Path
from unittest.mock import patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.context import AUTO, RuntimeContext, get_context, set_context


class TestRuntimeContextDefaults:
    """Verify that default values match the current hardcoded constants."""

    def test_default_isolated_network(self):
        ctx = RuntimeContext()
        assert ctx.isolated_network == "egg-isolated"

    def test_default_external_network(self):
        ctx = RuntimeContext()
        assert ctx.external_network == "egg-external"

    def test_default_isolated_subnet(self):
        ctx = RuntimeContext()
        assert ctx.isolated_subnet == "172.32.0.0/24"

    def test_default_external_subnet(self):
        ctx = RuntimeContext()
        assert ctx.external_subnet == "172.33.0.0/24"

    def test_default_gateway_ips(self):
        ctx = RuntimeContext()
        assert ctx.gateway_isolated_ip == "172.32.0.2"
        assert ctx.gateway_external_ip == "172.33.0.2"

    def test_default_gateway_container_name(self):
        ctx = RuntimeContext()
        assert ctx.gateway_container_name == "egg-gateway"

    def test_default_skip_build_false(self):
        ctx = RuntimeContext()
        assert ctx.skip_build is False

    def test_default_ephemeral_false(self):
        ctx = RuntimeContext()
        assert ctx.ephemeral is False

    def test_default_publish_ports_true(self):
        ctx = RuntimeContext()
        assert ctx.publish_ports is True

    def test_default_launcher_secret_none(self):
        ctx = RuntimeContext()
        assert ctx.launcher_secret is None

    def test_default_ports(self):
        ctx = RuntimeContext()
        assert ctx.gateway_port == 9848
        assert ctx.gateway_proxy_port == 3129


class TestRuntimeContextFromEnvironment:
    """Verify from_environment() reads EGG_* env vars."""

    def test_reads_network_names(self):
        env = {
            "EGG_ISOLATED_NETWORK": "test-isolated",
            "EGG_EXTERNAL_NETWORK": "test-external",
        }
        with patch.dict("os.environ", env, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.isolated_network == "test-isolated"
        assert ctx.external_network == "test-external"

    def test_reads_subnets(self):
        env = {
            "EGG_ISOLATED_SUBNET": "auto",
            "EGG_EXTERNAL_SUBNET": "10.0.0.0/24",
        }
        with patch.dict("os.environ", env, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.isolated_subnet == AUTO
        assert ctx.external_subnet == "10.0.0.0/24"

    def test_reads_images(self):
        env = {
            "EGG_GATEWAY_IMAGE": "ghcr.io/test/gw:v1",
            "EGG_SANDBOX_IMAGE": "ghcr.io/test/sb:v1",
        }
        with patch.dict("os.environ", env, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.gateway_image == "ghcr.io/test/gw:v1"
        assert ctx.sandbox_image == "ghcr.io/test/sb:v1"

    def test_reads_bool_flags(self):
        env = {
            "EGG_SKIP_BUILD": "true",
            "EGG_EPHEMERAL": "1",
            "EGG_PUBLISH_GATEWAY_PORTS": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.skip_build is True
        assert ctx.ephemeral is True
        assert ctx.publish_ports is False

    def test_reads_config_dir(self):
        env = {"EGG_CONFIG_DIR": "/tmp/test-config"}
        with patch.dict("os.environ", env, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.config_dir == Path("/tmp/test-config")

    def test_reads_launcher_secret(self):
        env = {"EGG_LAUNCHER_SECRET": "s3cr3t"}
        with patch.dict("os.environ", env, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.launcher_secret == "s3cr3t"

    def test_reads_ports(self):
        env = {
            "EGG_GATEWAY_PORT": "8080",
            "EGG_GATEWAY_PROXY_PORT": "3130",
        }
        with patch.dict("os.environ", env, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.gateway_port == 8080
        assert ctx.gateway_proxy_port == 3130

    def test_unset_vars_keep_defaults(self):
        """Unset EGG_* vars should not override defaults."""
        with patch.dict("os.environ", {}, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.isolated_network == "egg-isolated"
        assert ctx.skip_build is False
        assert ctx.publish_ports is True

    def test_reads_gateway_ips(self):
        env = {
            "EGG_GATEWAY_ISOLATED_IP": "10.0.0.2",
            "EGG_GATEWAY_EXTERNAL_IP": "10.1.0.2",
        }
        with patch.dict("os.environ", env, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.gateway_isolated_ip == "10.0.0.2"
        assert ctx.gateway_external_ip == "10.1.0.2"

    def test_reads_gateway_container_name(self):
        env = {"EGG_GATEWAY_CONTAINER_NAME": "my-gw"}
        with patch.dict("os.environ", env, clear=False):
            ctx = RuntimeContext.from_environment()
        assert ctx.gateway_container_name == "my-gw"


class TestModuleLevelContext:
    """Verify get_context() and set_context()."""

    def test_set_and_get_context(self):
        import egg_lib.context as ctx_mod

        original = ctx_mod._context

        try:
            custom = RuntimeContext(isolated_network="test-net")
            set_context(custom)
            assert get_context() is custom
            assert get_context().isolated_network == "test-net"
        finally:
            ctx_mod._context = original

    def test_get_context_creates_default(self):
        import egg_lib.context as ctx_mod

        original = ctx_mod._context

        try:
            ctx_mod._context = None
            ctx = get_context()
            assert ctx is not None
            assert ctx.isolated_network == "egg-isolated"
        finally:
            ctx_mod._context = original
