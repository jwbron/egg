"""Tests for egg_container config builder, docker-py adapter, and git shadow mounts."""

import tempfile
from pathlib import Path

from egg_container import (
    ContainerNetworkConfig,
    MountSpec,
    SandboxContainerConfig,
    build_sandbox_config,
    git_shadow_mounts,
    mount_spec_to_cli_args,
    to_dockerpy_kwargs,
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


class TestBuildSandboxConfig:
    """Tests for build_sandbox_config()."""

    def test_returns_config_dataclass(self):
        config = build_sandbox_config(
            container_name="test-1",
            image="egg:latest",
            network=_public_config(),
        )
        assert isinstance(config, SandboxContainerConfig)

    def test_gateway_url_hostname_based(self):
        """GATEWAY_URL must use the gateway hostname, not a raw IP."""
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(gateway_hostname="my-gw", gateway_port=1234),
        )
        assert config.environment["GATEWAY_URL"] == "http://my-gw:1234"

    def test_container_id_defaults_to_name(self):
        config = build_sandbox_config(
            container_name="my-container",
            image="img",
            network=_public_config(),
        )
        assert config.environment["CONTAINER_ID"] == "my-container"

    def test_extra_env_overrides_defaults(self):
        """Caller's extra_env should override builder defaults."""
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            extra_env={"CONTAINER_ID": "override-id", "CUSTOM": "val"},
        )
        assert config.environment["CONTAINER_ID"] == "override-id"
        assert config.environment["CUSTOM"] == "val"

    def test_session_token_set(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            session_token="tok-abc",
        )
        assert config.environment["EGG_SESSION_TOKEN"] == "tok-abc"

    def test_no_session_token_by_default(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert "EGG_SESSION_TOKEN" not in config.environment

    def test_runtime_uid_gid(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            runtime_uid=1000,
            runtime_gid=1001,
        )
        assert config.environment["RUNTIME_UID"] == "1000"
        assert config.environment["RUNTIME_GID"] == "1001"

    def test_extra_hosts_for_gateway(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(gateway_hostname="gw", gateway_ip="10.0.0.1"),
        )
        assert config.extra_hosts == {"gw": "10.0.0.1"}

    def test_security_opt(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert "label=disable" in config.security_opt

    def test_mounts_preserved(self):
        mounts = [
            MountSpec(mount_type="bind", source="/host", destination="/container"),
            MountSpec(mount_type="tmpfs", source=None, destination="/tmp"),
        ]
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            mounts=mounts,
        )
        assert len(config.mounts) == 2
        assert config.mounts[0].source == "/host"
        assert config.mounts[1].mount_type == "tmpfs"

    def test_labels_preserved(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            labels={"egg.pipeline.id": "test-123"},
        )
        assert config.labels["egg.pipeline.id"] == "test-123"

    def test_command_preserved(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            command=["claude", "--print", "hello"],
        )
        assert config.command == ("claude", "--print", "hello")


class TestBuildSandboxConfigPrivateMode:
    """Private mode should enable proxy, DNS lockdown, etc."""

    def test_private_mode_env(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_private_config(),
        )
        assert config.environment["PRIVATE_MODE"] == "true"

    def test_dns_lockdown(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_private_config(),
        )
        assert config.dns == ("0.0.0.0",)

    def test_proxy_vars(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_private_config(proxy_url="http://gw:3129"),
        )
        assert config.environment["HTTP_PROXY"] == "http://gw:3129"
        assert config.environment["HTTPS_PROXY"] == "http://gw:3129"
        assert config.environment["http_proxy"] == "http://gw:3129"
        assert config.environment["https_proxy"] == "http://gw:3129"

    def test_no_proxy_vars(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_private_config(gateway_hostname="gw"),
        )
        assert config.environment["NO_PROXY"] == "localhost,127.0.0.1,gw"
        assert config.environment["no_proxy"] == "localhost,127.0.0.1,gw"


class TestBuildSandboxConfigPublicMode:
    """Public mode should NOT enable proxy or DNS lockdown."""

    def test_public_mode_env(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert config.environment["PRIVATE_MODE"] == "false"

    def test_no_dns_lockdown(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert config.dns == ()

    def test_no_proxy_vars(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        assert "HTTP_PROXY" not in config.environment
        assert "HTTPS_PROXY" not in config.environment


class TestToDockerpyKwargs:
    """Tests for to_dockerpy_kwargs() adapter."""

    def test_basic_fields(self):
        config = build_sandbox_config(
            container_name="test-c",
            image="egg:latest",
            network=_public_config(),
        )
        kwargs = to_dockerpy_kwargs(config)
        assert kwargs["name"] == "test-c"
        assert kwargs["image"] == "egg:latest"
        assert kwargs["network"] == "egg-external"
        assert kwargs["security_opt"] == ["label=disable"]

    def test_environment_dict(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            extra_env={"FOO": "bar"},
        )
        kwargs = to_dockerpy_kwargs(config)
        assert kwargs["environment"]["FOO"] == "bar"
        assert kwargs["environment"]["GATEWAY_URL"] == "http://egg-gateway:9848"

    def test_extra_hosts(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(gateway_hostname="gw", gateway_ip="10.0.0.1"),
        )
        kwargs = to_dockerpy_kwargs(config)
        assert kwargs["extra_hosts"] == {"gw": "10.0.0.1"}

    def test_dns_for_private_mode(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_private_config(),
        )
        kwargs = to_dockerpy_kwargs(config)
        assert kwargs["dns"] == ["0.0.0.0"]

    def test_no_dns_for_public_mode(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
        )
        kwargs = to_dockerpy_kwargs(config)
        assert "dns" not in kwargs

    def test_bind_mounts_in_mounts_list(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            mounts=[
                MountSpec(mount_type="bind", source="/host/repo", destination="/container/repo"),
            ],
        )
        kwargs = to_dockerpy_kwargs(config)
        mounts = kwargs["mounts"]
        assert len(mounts) == 1
        assert mounts[0]["Type"] == "bind"
        assert mounts[0]["Source"] == "/host/repo"
        assert mounts[0]["Target"] == "/container/repo"
        assert mounts[0]["ReadOnly"] is False

    def test_readonly_bind_mount(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            mounts=[
                MountSpec(
                    mount_type="bind",
                    source="/dev/null",
                    destination="/path/.git",
                    readonly=True,
                ),
            ],
        )
        kwargs = to_dockerpy_kwargs(config)
        mounts = kwargs["mounts"]
        assert mounts[0]["ReadOnly"] is True

    def test_tmpfs_mounts(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            mounts=[
                MountSpec(mount_type="tmpfs", source=None, destination="/path/.git"),
            ],
        )
        kwargs = to_dockerpy_kwargs(config)
        mounts = kwargs["mounts"]
        assert len(mounts) == 1
        assert mounts[0]["Type"] == "tmpfs"
        assert mounts[0]["Target"] == "/path/.git"

    def test_named_volume_mounts(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            mounts=[
                MountSpec(
                    mount_type="volume",
                    source="egg-certs",
                    destination="/shared/certs",
                    readonly=True,
                ),
            ],
        )
        kwargs = to_dockerpy_kwargs(config)
        mounts = kwargs["mounts"]
        assert len(mounts) == 1
        assert mounts[0]["Type"] == "volume"
        assert mounts[0]["Source"] == "egg-certs"
        assert mounts[0]["Target"] == "/shared/certs"
        assert mounts[0]["ReadOnly"] is True

    def test_multi_repo_devnull_mounts_no_collision(self):
        """Multiple /dev/null bind mounts must all be present (no key collision)."""
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            mounts=[
                MountSpec(
                    mount_type="bind",
                    source="/dev/null",
                    destination="/repos/a/.git",
                    readonly=True,
                ),
                MountSpec(
                    mount_type="bind",
                    source="/dev/null",
                    destination="/repos/b/.git",
                    readonly=True,
                ),
                MountSpec(
                    mount_type="bind",
                    source="/dev/null",
                    destination="/repos/c/.git",
                    readonly=True,
                ),
            ],
        )
        kwargs = to_dockerpy_kwargs(config)
        mounts = kwargs["mounts"]
        assert len(mounts) == 3
        targets = {m["Target"] for m in mounts}
        assert targets == {"/repos/a/.git", "/repos/b/.git", "/repos/c/.git"}

    def test_command_forwarded(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            command=["claude", "--print"],
        )
        kwargs = to_dockerpy_kwargs(config)
        assert kwargs["command"] == ["claude", "--print"]

    def test_labels_forwarded(self):
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(),
            labels={"egg.pipeline.id": "test-1"},
        )
        kwargs = to_dockerpy_kwargs(config)
        assert kwargs["labels"]["egg.pipeline.id"] == "test-1"

    def test_gateway_url_not_overwritten_by_extra_env(self):
        """GATEWAY_URL from the config builder must survive — callers that
        want to override it must pass it explicitly in extra_env."""
        config = build_sandbox_config(
            container_name="c",
            image="img",
            network=_public_config(gateway_hostname="gw", gateway_port=9848),
        )
        kwargs = to_dockerpy_kwargs(config)
        assert kwargs["environment"]["GATEWAY_URL"] == "http://gw:9848"


class TestGitShadowMounts:
    """Tests for git_shadow_mounts()."""

    def test_assume_worktree_returns_devnull_bind(self):
        """With assume_worktree=True, all mounts should be /dev/null bind (file-over-file)."""
        mounts = git_shadow_mounts(
            {"repo1": "/host/repo1", "repo2": "/host/repo2"},
            assume_worktree=True,
        )
        assert len(mounts) == 2
        assert all(m.mount_type == "bind" for m in mounts)
        assert all(m.source == "/dev/null" for m in mounts)
        assert all(m.readonly is True for m in mounts)
        assert mounts[0].destination == "/home/egg/repos/repo1/.git"
        assert mounts[1].destination == "/home/egg/repos/repo2/.git"

    def test_custom_container_base(self):
        mounts = git_shadow_mounts(
            {"myrepo": "/host/myrepo"},
            container_base="/workspace",
            assume_worktree=True,
        )
        assert mounts[0].destination == "/workspace/myrepo/.git"
        assert mounts[0].mount_type == "bind"
        assert mounts[0].source == "/dev/null"

    def test_regular_repo_uses_tmpfs(self):
        """Regular repo (.git is a directory) should get tmpfs shadow."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()  # .git is a directory

            mounts = git_shadow_mounts(
                {"repo": str(repo)},
                assume_worktree=False,
            )
            assert len(mounts) == 1
            assert mounts[0].mount_type == "tmpfs"

    def test_worktree_repo_uses_devnull_bind(self):
        """Worktree repo (.git is a file) should get /dev/null bind mount."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").write_text("gitdir: /some/path")  # .git is a file

            mounts = git_shadow_mounts(
                {"repo": str(repo)},
                assume_worktree=False,
            )
            assert len(mounts) == 1
            assert mounts[0].mount_type == "bind"
            assert mounts[0].source == "/dev/null"
            assert mounts[0].readonly is True

    def test_missing_git_uses_tmpfs(self):
        """Missing .git should still get tmpfs shadow."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            # No .git at all

            mounts = git_shadow_mounts(
                {"repo": str(repo)},
                assume_worktree=False,
            )
            assert len(mounts) == 1
            assert mounts[0].mount_type == "tmpfs"

    def test_empty_repo_volumes(self):
        mounts = git_shadow_mounts({}, assume_worktree=True)
        assert mounts == []


class TestMountSpecToCliArgs:
    """Tests for mount_spec_to_cli_args()."""

    def test_bind_mount(self):
        mount = MountSpec(
            mount_type="bind",
            source="/host/path",
            destination="/container/path",
        )
        args = mount_spec_to_cli_args(mount)
        assert args == ["--mount", "type=bind,source=/host/path,destination=/container/path"]

    def test_bind_mount_readonly(self):
        mount = MountSpec(
            mount_type="bind",
            source="/dev/null",
            destination="/path/.git",
            readonly=True,
        )
        args = mount_spec_to_cli_args(mount)
        assert args == ["--mount", "type=bind,source=/dev/null,destination=/path/.git,readonly"]

    def test_tmpfs_mount(self):
        mount = MountSpec(
            mount_type="tmpfs",
            source=None,
            destination="/path/.git",
        )
        args = mount_spec_to_cli_args(mount)
        assert args == ["--mount", "type=tmpfs,destination=/path/.git"]

    def test_bind_without_source_returns_empty(self):
        mount = MountSpec(mount_type="bind", source=None, destination="/path")
        args = mount_spec_to_cli_args(mount)
        assert args == []
