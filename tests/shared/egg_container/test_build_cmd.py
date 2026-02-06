"""Tests for egg_container.build_sandbox_docker_cmd()."""

from egg_container import (
    LIFECYCLE_FLAGS_INDEX,
    ContainerNetworkConfig,
    build_sandbox_docker_cmd,
)


def _private_config(**overrides):
    defaults = {
        "network_name": "egg-isolated",
        "gateway_hostname": "egg-gateway",
        "gateway_ip": "172.32.0.2",
        "gateway_port": 9848,
        "repo_mode": "private",
        "proxy_url": "http://egg-gateway:3129",
    }
    defaults.update(overrides)
    return ContainerNetworkConfig(**defaults)


def _public_config(**overrides):
    defaults = {
        "network_name": "egg-external",
        "gateway_hostname": "egg-gateway",
        "gateway_ip": "172.33.0.2",
        "gateway_port": 9848,
        "repo_mode": "public",
    }
    defaults.update(overrides)
    return ContainerNetworkConfig(**defaults)


class TestBuildSandboxDockerCmd:
    """Core build_sandbox_docker_cmd tests."""

    def test_basic_structure(self):
        """Command starts with 'docker run' and ends with image name."""
        cmd = build_sandbox_docker_cmd(
            container_name="test-1",
            image="egg-sandbox:latest",
            network=_public_config(),
        )
        assert cmd[0] == "docker"
        assert cmd[1] == "run"
        assert cmd[-1] == "egg-sandbox:latest"

    def test_security_opt_present(self):
        cmd = build_sandbox_docker_cmd(
            container_name="test-1",
            image="egg-sandbox:latest",
            network=_public_config(),
        )
        idx = cmd.index("--security-opt")
        assert cmd[idx + 1] == "label=disable"

    def test_container_name(self):
        cmd = build_sandbox_docker_cmd(
            container_name="my-container",
            image="img",
            network=_public_config(),
        )
        idx = cmd.index("--name")
        assert cmd[idx + 1] == "my-container"

    def test_network_set(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(network_name="my-net"),
        )
        idx = cmd.index("--network")
        assert cmd[idx + 1] == "my-net"

    def test_add_host_present(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(gateway_hostname="gw", gateway_ip="10.0.0.1"),
        )
        idx = cmd.index("--add-host")
        assert cmd[idx + 1] == "gw:10.0.0.1"

    def test_gateway_url_env(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(gateway_hostname="gw", gateway_port=1234),
        )
        assert "GATEWAY_URL=http://gw:1234" in cmd

    def test_container_id_env(self):
        cmd = build_sandbox_docker_cmd(
            container_name="my-id",
            image="img",
            network=_public_config(),
        )
        assert "CONTAINER_ID=my-id" in cmd


class TestOptionalParams:
    """Tests for optional parameters."""

    def test_no_ip_by_default(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert "--ip" not in cmd

    def test_ip_when_provided(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
            container_ip="172.32.0.50",
        )
        idx = cmd.index("--ip")
        assert cmd[idx + 1] == "172.32.0.50"

    def test_session_token(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
            session_token="tok-123",
        )
        assert "EGG_SESSION_TOKEN=tok-123" in cmd

    def test_no_session_token_by_default(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert not any("EGG_SESSION_TOKEN" in arg for arg in cmd)

    def test_runtime_uid_gid(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
            runtime_uid=1000,
            runtime_gid=1001,
        )
        assert "RUNTIME_UID=1000" in cmd
        assert "RUNTIME_GID=1001" in cmd

    def test_no_uid_gid_by_default(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert not any("RUNTIME_UID" in arg for arg in cmd)
        assert not any("RUNTIME_GID" in arg for arg in cmd)

    def test_extra_env(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
            extra_env={"FOO": "bar", "BAZ": "qux"},
        )
        assert "FOO=bar" in cmd
        assert "BAZ=qux" in cmd

    def test_extra_args(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
            extra_args=["--log-driver", "json-file"],
        )
        assert "--log-driver" in cmd
        assert "json-file" in cmd


class TestPrivateMode:
    """Private mode should set DNS lockdown and proxy env vars."""

    def test_dns_disabled(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_private_config(),
        )
        idx = cmd.index("--dns")
        assert cmd[idx + 1] == "0.0.0.0"

    def test_private_mode_env(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_private_config(),
        )
        assert "PRIVATE_MODE=true" in cmd

    def test_proxy_env_vars(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_private_config(proxy_url="http://gw:3129"),
        )
        assert "HTTP_PROXY=http://gw:3129" in cmd
        assert "HTTPS_PROXY=http://gw:3129" in cmd
        assert "http_proxy=http://gw:3129" in cmd
        assert "https_proxy=http://gw:3129" in cmd

    def test_no_proxy_env_vars(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_private_config(gateway_hostname="gw"),
        )
        assert "NO_PROXY=localhost,127.0.0.1,gw" in cmd
        assert "no_proxy=localhost,127.0.0.1,gw" in cmd

    def test_proxy_default_when_no_url(self):
        """When proxy_url is None, falls back to gateway_hostname:3129."""
        cfg = _private_config(proxy_url=None, gateway_hostname="my-gw")
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=cfg,
        )
        assert "HTTP_PROXY=http://my-gw:3129" in cmd


class TestPublicMode:
    """Public mode should NOT set DNS lockdown or proxy env vars."""

    def test_public_mode_env(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert "PRIVATE_MODE=false" in cmd

    def test_no_dns_lockdown(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert "--dns" not in cmd

    def test_no_proxy_vars(self):
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert not any("HTTP_PROXY" in arg for arg in cmd)
        assert not any("HTTPS_PROXY" in arg for arg in cmd)


class TestCallerConventions:
    """Test that the output supports the documented caller conventions."""

    def test_lifecycle_flags_index_constant(self):
        """LIFECYCLE_FLAGS_INDEX should be 2 (after 'docker run')."""
        assert LIFECYCLE_FLAGS_INDEX == 2

    def test_lifecycle_flags_insertion(self):
        """Callers insert --rm/-it at LIFECYCLE_FLAGS_INDEX."""
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        idx = LIFECYCLE_FLAGS_INDEX
        cmd[idx:idx] = ["--rm", "-it"]
        assert cmd[:4] == ["docker", "run", "--rm", "-it"]
        assert cmd[-1] == "img"

    def test_mount_insertion(self):
        """Callers insert mounts at cmd[-1:-1] (before image)."""
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="my-image",
            network=_public_config(),
        )
        cmd[-1:-1] = ["-v", "/host:/container"]
        assert cmd[-1] == "my-image"
        assert "-v" in cmd
        assert "/host:/container" in cmd

    def test_command_append(self):
        """Callers append commands after the image."""
        cmd = build_sandbox_docker_cmd(
            container_name="c",
            image="my-image",
            network=_public_config(),
        )
        cmd.extend(["claude", "--print", "hello"])
        assert cmd[-3:] == ["claude", "--print", "hello"]
