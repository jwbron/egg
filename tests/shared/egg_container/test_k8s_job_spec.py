"""Tests for egg_container.to_k8s_job_kwargs() and build_sandbox_job_spec()."""

from egg_config import GATEWAY_PORT
from egg_container import (
    ContainerNetworkConfig,
    MountSpec,
    SandboxContainerConfig,
    build_sandbox_job_spec,
    to_k8s_job_kwargs,
)


def _make_config(**overrides):
    """Create a minimal SandboxContainerConfig for testing."""
    defaults = {
        "container_name": "test-agent",
        "image": "egg:latest",
        "network": ContainerNetworkConfig(
            network_name="egg-isolated",
            gateway_hostname="egg-gateway",
            gateway_ip="172.32.0.2",
            gateway_port=GATEWAY_PORT,
            repo_mode="private",
        ),
        "environment": {"FOO": "bar", "BAZ": "qux"},
        "mounts": (),
        "labels": {"egg.pipeline.id": "test-pipeline"},
        "extra_hosts": {},
        "security_opt": (),
        "dns": (),
    }
    defaults.update(overrides)
    return SandboxContainerConfig(**defaults)


class TestToK8sJobKwargs:
    """Tests for to_k8s_job_kwargs()."""

    def test_basic_structure(self):
        """Job spec has apiVersion, kind, metadata, and spec."""
        config = _make_config()
        result = to_k8s_job_kwargs(config)

        assert result["apiVersion"] == "batch/v1"
        assert result["kind"] == "Job"
        assert "metadata" in result
        assert "spec" in result

    def test_namespace(self):
        """Namespace is set correctly on metadata."""
        config = _make_config()
        result = to_k8s_job_kwargs(config, namespace="egg-agents")

        assert result["metadata"]["namespace"] == "egg-agents"

    def test_default_namespace(self):
        """Default namespace is egg-system."""
        config = _make_config()
        result = to_k8s_job_kwargs(config)

        assert result["metadata"]["namespace"] == "egg-system"

    def test_environment_variables(self):
        """Environment variables are converted to V1EnvVar-style dicts."""
        config = _make_config(environment={"KEY1": "val1", "KEY2": "val2"})
        result = to_k8s_job_kwargs(config)

        container = result["spec"]["template"]["spec"]["containers"][0]
        env_names = {e["name"] for e in container["env"]}
        assert "KEY1" in env_names
        assert "KEY2" in env_names

    def test_bind_mount(self):
        """Bind mounts produce hostPath volumes."""
        mounts = (
            MountSpec(
                mount_type="bind",
                source="/host/path",
                destination="/container/path",
                readonly=True,
            ),
        )
        config = _make_config(mounts=mounts)
        result = to_k8s_job_kwargs(config)

        pod_spec = result["spec"]["template"]["spec"]
        assert len(pod_spec["volumes"]) == 1
        assert "hostPath" in pod_spec["volumes"][0]
        assert pod_spec["volumes"][0]["hostPath"]["path"] == "/host/path"

        container = pod_spec["containers"][0]
        assert len(container["volumeMounts"]) == 1
        assert container["volumeMounts"][0]["mountPath"] == "/container/path"
        assert container["volumeMounts"][0]["readOnly"] is True

    def test_tmpfs_mount(self):
        """Tmpfs mounts produce emptyDir with Memory medium."""
        mounts = (
            MountSpec(
                mount_type="tmpfs",
                source=None,
                destination="/tmp/scratch",
            ),
        )
        config = _make_config(mounts=mounts)
        result = to_k8s_job_kwargs(config)

        pod_spec = result["spec"]["template"]["spec"]
        assert len(pod_spec["volumes"]) == 1
        assert pod_spec["volumes"][0]["emptyDir"]["medium"] == "Memory"

    def test_labels_include_managed_by(self):
        """Labels include app.kubernetes.io/managed-by: egg."""
        config = _make_config(labels={"custom": "label"})
        result = to_k8s_job_kwargs(config)

        labels = result["metadata"]["labels"]
        assert labels["app.kubernetes.io/managed-by"] == "egg"
        assert labels["custom"] == "label"

    def test_job_name_lowercased(self):
        """Job name is lowercased and underscores replaced with hyphens."""
        config = _make_config(container_name="My_Test_Agent")
        result = to_k8s_job_kwargs(config)

        assert result["metadata"]["name"] == "my-test-agent"

    def test_job_name_truncated_at_63_chars(self):
        """Job names longer than 63 chars are truncated."""
        long_name = "a" * 80
        config = _make_config(container_name=long_name)
        result = to_k8s_job_kwargs(config)

        assert len(result["metadata"]["name"]) <= 63

    def test_backoff_limit(self):
        """backoffLimit is set correctly."""
        config = _make_config()
        result = to_k8s_job_kwargs(config, backoff_limit=3)

        assert result["spec"]["backoffLimit"] == 3

    def test_active_deadline_seconds(self):
        """activeDeadlineSeconds is included when set."""
        config = _make_config()
        result = to_k8s_job_kwargs(config, active_deadline_seconds=3600)

        assert result["spec"]["activeDeadlineSeconds"] == 3600

    def test_active_deadline_seconds_omitted_when_none(self):
        """activeDeadlineSeconds is not in spec when None."""
        config = _make_config()
        result = to_k8s_job_kwargs(config, active_deadline_seconds=None)

        assert "activeDeadlineSeconds" not in result["spec"]

    def test_restart_policy(self):
        """restartPolicy defaults to Never."""
        config = _make_config()
        result = to_k8s_job_kwargs(config)

        pod_spec = result["spec"]["template"]["spec"]
        assert pod_spec["restartPolicy"] == "Never"

    def test_service_account(self):
        """serviceAccountName is set correctly."""
        config = _make_config()
        result = to_k8s_job_kwargs(config, service_account="custom-sa")

        pod_spec = result["spec"]["template"]["spec"]
        assert pod_spec["serviceAccountName"] == "custom-sa"

    def test_command(self):
        """Container command is passed through."""
        config = _make_config(command=("/bin/sh", "-c", "echo hello"))
        result = to_k8s_job_kwargs(config)

        container = result["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["/bin/sh", "-c", "echo hello"]

    def test_security_opt_label_disable(self):
        """label=disable maps to SELinux spc_t."""
        config = _make_config(security_opt=("label=disable",))
        result = to_k8s_job_kwargs(config)

        container = result["spec"]["template"]["spec"]["containers"][0]
        assert container["securityContext"]["seLinuxOptions"]["type"] == "spc_t"

    def test_dns_config(self):
        """DNS servers are set in dnsConfig."""
        config = _make_config(dns=("8.8.8.8", "8.8.4.4"))
        result = to_k8s_job_kwargs(config)

        pod_spec = result["spec"]["template"]["spec"]
        assert pod_spec["dnsConfig"]["nameservers"] == ["8.8.8.8", "8.8.4.4"]

    def test_extra_hosts(self):
        """Extra hosts map to hostAliases."""
        config = _make_config(extra_hosts={"myhost": "10.0.0.1"})
        result = to_k8s_job_kwargs(config)

        pod_spec = result["spec"]["template"]["spec"]
        assert len(pod_spec["hostAliases"]) == 1
        assert pod_spec["hostAliases"][0]["ip"] == "10.0.0.1"
        assert pod_spec["hostAliases"][0]["hostnames"] == ["myhost"]

    def test_container_image(self):
        """Container image is set from config."""
        config = _make_config(image="custom:v2")
        result = to_k8s_job_kwargs(config)

        container = result["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "custom:v2"


class TestBuildSandboxJobSpec:
    """Tests for the build_sandbox_job_spec() convenience wrapper."""

    def test_returns_valid_job_spec(self):
        """build_sandbox_job_spec returns a dict with Job structure."""
        network = ContainerNetworkConfig(
            network_name="egg-isolated",
            gateway_hostname="egg-gateway",
            gateway_ip="172.32.0.2",
            gateway_port=GATEWAY_PORT,
            repo_mode="private",
        )
        result = build_sandbox_job_spec(
            container_name="test-agent",
            image="egg:latest",
            network=network,
        )

        assert result["apiVersion"] == "batch/v1"
        assert result["kind"] == "Job"
        assert result["metadata"]["name"] == "test-agent"

    def test_namespace_passed_through(self):
        """Namespace parameter is forwarded to to_k8s_job_kwargs."""
        network = ContainerNetworkConfig(
            network_name="egg-isolated",
            gateway_hostname="egg-gateway",
            gateway_ip="172.32.0.2",
            gateway_port=GATEWAY_PORT,
            repo_mode="private",
        )
        result = build_sandbox_job_spec(
            container_name="test-agent",
            image="egg:latest",
            network=network,
            namespace="egg-agents",
        )

        assert result["metadata"]["namespace"] == "egg-agents"

    def test_extra_env_included(self):
        """Extra env vars are passed through to the container spec."""
        network = ContainerNetworkConfig(
            network_name="egg-isolated",
            gateway_hostname="egg-gateway",
            gateway_ip="172.32.0.2",
            gateway_port=GATEWAY_PORT,
            repo_mode="private",
        )
        result = build_sandbox_job_spec(
            container_name="test-agent",
            image="egg:latest",
            network=network,
            extra_env={"CUSTOM_VAR": "custom_value"},
        )

        container = result["spec"]["template"]["spec"]["containers"][0]
        env_map = {e["name"]: e["value"] for e in container["env"]}
        assert env_map["CUSTOM_VAR"] == "custom_value"

    def test_active_deadline_seconds(self):
        """active_deadline_seconds is forwarded."""
        network = ContainerNetworkConfig(
            network_name="egg-isolated",
            gateway_hostname="egg-gateway",
            gateway_ip="172.32.0.2",
            gateway_port=GATEWAY_PORT,
            repo_mode="private",
        )
        result = build_sandbox_job_spec(
            container_name="test-agent",
            image="egg:latest",
            network=network,
            active_deadline_seconds=7200,
        )

        assert result["spec"]["activeDeadlineSeconds"] == 7200
