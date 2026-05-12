"""Unit tests for ``orchestrator/routes/deployment.py``.

Covers the five MCP-facing HTTP endpoints introduced for issue #1759
(``get_deployment_context``, ``validate_deployment_manifests``,
``prune_stale_worktrees`` proxy, ``validate_network_isolation``,
``rebuild_and_rollout`` + its progress-stream reader) plus the
``@require_lifecycle_secret`` regression guards so the #1769 auth
pattern doesn't silently drop on any of the new routes.

The tests drive the Blueprint directly via a tiny Flask app so we can
inspect every route in isolation without booting the full orchestrator
service.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure orchestrator and shared are importable whether tests run under
# the orchestrator harness or the repo-wide ``make test`` invocation.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_config.constants import GATEWAY_PORT, TEST_GATEWAY_PORT  # noqa: E402


@pytest.fixture
def app():
    """Create a Flask test app with the deployment blueprint registered."""
    from flask import Flask
    from routes.deployment import deployment_bp

    app = Flask(__name__)
    app.register_blueprint(deployment_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_rebuild_globals():
    """Clear ``rebuild_and_rollout`` module-level state between tests.

    The module keeps an in-process lock + active-stream-id + stream
    buffers. Tests mutate these, so we snapshot/restore around each
    test to keep them isolated.
    """
    from routes import deployment as dep_mod

    dep_mod._REBUILD_IN_PROGRESS = False
    dep_mod._REBUILD_ACTIVE_STREAM_ID = None
    dep_mod._STREAM_BUFFERS.clear()
    dep_mod._STREAM_TERMINATED.clear()
    # _STREAM_TERMINATION_TS is the retention-reap bookkeeping dict
    # introduced by the MEDIUM-3 NACK fix. Clear between tests so
    # retention counts start fresh.
    if hasattr(dep_mod, "_STREAM_TERMINATION_TS"):
        dep_mod._STREAM_TERMINATION_TS.clear()
    yield
    dep_mod._REBUILD_IN_PROGRESS = False
    dep_mod._REBUILD_ACTIVE_STREAM_ID = None
    dep_mod._STREAM_BUFFERS.clear()
    dep_mod._STREAM_TERMINATED.clear()
    if hasattr(dep_mod, "_STREAM_TERMINATION_TS"):
        dep_mod._STREAM_TERMINATION_TS.clear()


# ---------------------------------------------------------------------------
# get_deployment_context
# ---------------------------------------------------------------------------


class TestGetDeploymentContext:
    """GET /api/v1/deployment/context."""

    def test_docker_runtime_returns_placeholder_payload(self, client, monkeypatch):
        """On Docker the route returns a structured placeholder, not an error."""
        monkeypatch.setenv("EGG_RUNTIME", "docker")
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        response = client.get("/api/v1/deployment/context")
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        data = body["data"]
        assert data["runtime"] == "docker"
        # #1850: the payload records how runtime was resolved so the
        # operator can tell env-configured from auto-detected.
        assert data["detection_source"] == "env"
        # Degraded payload still carries every key the operator expects.
        for key in (
            "namespace",
            "cluster_info",
            "cni",
            "network_policy_enforcement",
            "images",
            "is_k3s",
            "k3s_flavor_hint",
        ):
            assert key in data, f"missing key: {key}"
        assert data["cluster_info"]["nodes"] == 0
        assert data["network_policy_enforcement"] is False
        assert data["is_k3s"] is False
        assert data["images"] == {}

    def test_unset_env_with_k8s_service_host_autodetects_kubernetes(self, client, monkeypatch):
        """EGG_RUNTIME unset + KUBERNETES_SERVICE_HOST set → auto-detect k8s (#1850)."""
        monkeypatch.delenv("EGG_RUNTIME", raising=False)
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")

        fake_k8s = MagicMock()
        fake_k8s.core_api.list_node.return_value.items = []
        fake_version_info = MagicMock()
        fake_version_info.git_version = "v1.30.2+k3s1"

        with (
            patch("routes.deployment._detect_k3s", return_value=(True, "v1.30.2+k3s1")),
            patch("routes.deployment._detect_cni", return_value=("calico", True)),
            patch(
                "routes.deployment._collect_egg_image_tags",
                return_value={"orchestrator": "egg-orchestrator:dev"},
            ),
            patch("kubernetes.client.VersionApi") as mock_version_api_cls,
        ):
            mock_version_api_cls.return_value.get_code.return_value = fake_version_info
            with patch.dict(
                "sys.modules",
                {
                    "kubernetes_client": MagicMock(
                        get_kubernetes_client=MagicMock(return_value=fake_k8s),
                    )
                },
            ):
                response = client.get("/api/v1/deployment/context")

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["runtime"] == "kubernetes"
        assert data["detection_source"] == "auto:k8s-service-host"

    def test_unset_env_with_no_signals_defaults_to_docker(self, client, monkeypatch):
        """EGG_RUNTIME unset + no KUBERNETES_SERVICE_HOST → auto-default docker."""
        monkeypatch.delenv("EGG_RUNTIME", raising=False)
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        response = client.get("/api/v1/deployment/context")
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["runtime"] == "docker"
        assert data["detection_source"] == "auto:default"

    def test_cluster_unreachable_demotes_to_unknown(self, client, monkeypatch):
        """Kubernetes env var set but both apiserver probes fail → runtime=unknown (#1850)."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        fake_k8s = MagicMock()
        fake_k8s.core_api.list_node.side_effect = RuntimeError("apiserver down")

        with (
            patch("kubernetes.client.VersionApi") as mock_version_api_cls,
            patch.dict(
                "sys.modules",
                {
                    "kubernetes_client": MagicMock(
                        get_kubernetes_client=MagicMock(return_value=fake_k8s),
                    )
                },
            ),
        ):
            mock_version_api_cls.return_value.get_code.side_effect = RuntimeError("apiserver down")
            response = client.get("/api/v1/deployment/context")

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["runtime"] == "unknown"
        assert data["detection_error"] == "cluster_unreachable"
        # Operator sees the env-configured value via detection_source.
        assert data["detection_source"] == "env"
        # Both probes failed — nodes count is unknown, not zero.
        assert data["cluster_info"]["nodes_unavailable"] is True

    def test_empty_images_flags_images_unavailable(self, client, monkeypatch):
        """Cluster reachable but image listing came back empty → flag it (#1850)."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        fake_k8s = MagicMock()
        fake_k8s.core_api.list_node.return_value.items = []
        fake_version_info = MagicMock()
        fake_version_info.git_version = "v1.30.2+k3s1"

        with (
            patch("routes.deployment._detect_k3s", return_value=(True, "v1.30.2+k3s1")),
            patch("routes.deployment._detect_cni", return_value=("calico", True)),
            patch("routes.deployment._collect_egg_image_tags", return_value={}),
            patch("kubernetes.client.VersionApi") as mock_version_api_cls,
        ):
            mock_version_api_cls.return_value.get_code.return_value = fake_version_info
            with patch.dict(
                "sys.modules",
                {
                    "kubernetes_client": MagicMock(
                        get_kubernetes_client=MagicMock(return_value=fake_k8s),
                    )
                },
            ):
                response = client.get("/api/v1/deployment/context")

        data = response.get_json()["data"]
        assert data["runtime"] == "kubernetes"
        assert data["images"] == {}
        assert data["images_unavailable"] is True

    def test_nodes_unavailable_on_partial_probe_failure(self, client, monkeypatch):
        """Version probe ok but node-list probe fails → nodes_unavailable: true."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        fake_k8s = MagicMock()
        fake_k8s.core_api.list_node.side_effect = RuntimeError("RBAC denied")
        fake_version_info = MagicMock()
        fake_version_info.git_version = "v1.30.2+k3s1"

        with (
            patch("routes.deployment._detect_k3s", return_value=(True, "v1.30.2+k3s1")),
            patch("routes.deployment._detect_cni", return_value=("calico", True)),
            patch(
                "routes.deployment._collect_egg_image_tags",
                return_value={"orchestrator": "egg-orchestrator:dev"},
            ),
            patch("kubernetes.client.VersionApi") as mock_version_api_cls,
        ):
            mock_version_api_cls.return_value.get_code.return_value = fake_version_info
            with patch.dict(
                "sys.modules",
                {
                    "kubernetes_client": MagicMock(
                        get_kubernetes_client=MagicMock(return_value=fake_k8s),
                    )
                },
            ):
                response = client.get("/api/v1/deployment/context")

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["runtime"] == "kubernetes"
        assert data["cluster_info"]["nodes"] == 0
        assert data["cluster_info"]["nodes_unavailable"] is True
        assert data["cluster_info"]["server_version"] == "v1.30.2+k3s1"

    def test_kubernetes_runtime_aggregates_cluster_info(self, client, monkeypatch):
        """On Kubernetes the route aggregates version/cni/images/k3s flags."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        monkeypatch.setenv("EGG_K8S_NAMESPACE", "egg-test")

        fake_k8s = MagicMock()
        # Configure core_api.list_node() to return an object with a plain list
        # so len() yields an int, not a MagicMock.
        fake_k8s.core_api.list_node.return_value.items = []

        fake_version_info = MagicMock()
        fake_version_info.git_version = "v1.29.0"

        with (
            patch(
                "routes.deployment._detect_k3s",
                return_value=(True, "v1.29.0+k3s1"),
            ),
            patch(
                "routes.deployment._detect_cni",
                return_value=("calico", True),
            ),
            patch(
                "routes.deployment._collect_egg_image_tags",
                return_value={"orchestrator": "egg-orchestrator:dev"},
            ),
            patch(
                "kubernetes.client.VersionApi",
            ) as mock_version_api_cls,
        ):
            mock_version_api_cls.return_value.get_code.return_value = fake_version_info
            with patch.dict(
                "sys.modules",
                {
                    "kubernetes_client": MagicMock(
                        get_kubernetes_client=MagicMock(return_value=fake_k8s),
                    )
                },
            ):
                response = client.get("/api/v1/deployment/context")

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["runtime"] == "kubernetes"
        assert data["namespace"] == "egg-test"
        assert data["cni"] == "calico"
        assert data["network_policy_enforcement"] is True
        assert data["is_k3s"] is True
        assert data["k3s_flavor_hint"] == "v1.29.0+k3s1"
        assert data["images"] == {"orchestrator": "egg-orchestrator:dev"}

    def test_kubernetes_client_init_failure_is_reported(self, client, monkeypatch):
        """Client-init exceptions demote runtime to ``unknown`` with detection_error (#1850)."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        with patch.dict(
            "sys.modules",
            {
                "kubernetes_client": MagicMock(
                    get_kubernetes_client=MagicMock(side_effect=RuntimeError("boom"))
                )
            },
        ):
            response = client.get("/api/v1/deployment/context")
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["runtime"] == "unknown"
        assert data["detection_error"] == "kubernetes_client_init_failed"
        assert "boom" in data["detail"]


# ---------------------------------------------------------------------------
# validate_deployment_manifests
# ---------------------------------------------------------------------------


class TestValidateDeploymentManifestsRoute:
    """POST /api/v1/deployment/validate-manifests."""

    def test_docker_runtime_returns_not_available(self, client, monkeypatch):
        """Docker is not supported — route degrades with a structured payload."""
        monkeypatch.setenv("EGG_RUNTIME", "docker")
        response = client.post("/api/v1/deployment/validate-manifests", json={})
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["error"] == "not_available_on_runtime"
        assert data["runtime"] == "docker"

    def test_missing_overlay_returns_404(self, client, monkeypatch, tmp_path):
        """Non-existent overlay path returns 404."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        response = client.post(
            "/api/v1/deployment/validate-manifests",
            json={"overlay_path": "k8s/does-not-exist"},
        )
        assert response.status_code == 404
        body = response.get_json()
        assert body["success"] is False
        assert "not found" in body["message"]

    def test_valid_overlay_reports_warnings(self, client, monkeypatch, tmp_path):
        """A real overlay directory returns the static-validation warnings list."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        # Make a fake overlay so the existence check passes; mock kustomize
        # and the k3s detector so we control the warning output.
        overlay = tmp_path / "k8s" / "overlays" / "local"
        overlay.mkdir(parents=True)
        (overlay / "kustomization.yaml").write_text("bases: []\n")
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))

        with (
            patch(
                "routes.deployment._run_kustomize",
                return_value=[
                    {
                        "kind": "Deployment",
                        "metadata": {"name": "orchestrator"},
                        "spec": {
                            "template": {
                                "metadata": {"labels": {"app": "orchestrator"}},
                                "spec": {
                                    "containers": [
                                        {"name": "main", "image": "egg-orchestrator:dev"},
                                    ],
                                    "volumes": [],
                                },
                            }
                        },
                    }
                ],
            ),
            patch(
                "routes.deployment._detect_k3s",
                return_value=(False, None),
            ),
            patch.dict(
                "sys.modules",
                {"kubernetes_client": MagicMock(get_kubernetes_client=MagicMock())},
            ),
        ):
            response = client.post(
                "/api/v1/deployment/validate-manifests",
                json={"overlay_path": "k8s/overlays/local"},
            )

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["is_k3s"] is False
        # Non-k3s environment: the image-missing-tag rule should be skipped,
        # represented as a "skipped" sentinel entry in warnings.
        assert any(w.get("skipped") == "not_k3s" for w in data["warnings"])

    def test_kustomize_failure_returns_500(self, client, monkeypatch, tmp_path):
        """A ``kustomize build`` failure is surfaced as a 500 error."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        overlay = tmp_path / "k8s" / "overlays" / "local"
        overlay.mkdir(parents=True)
        (overlay / "kustomization.yaml").write_text("bases: []\n")
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))

        with (
            patch(
                "routes.deployment._run_kustomize",
                side_effect=RuntimeError("kustomize broke"),
            ),
            patch(
                "routes.deployment._detect_k3s",
                return_value=(False, None),
            ),
            patch.dict(
                "sys.modules",
                {"kubernetes_client": MagicMock(get_kubernetes_client=MagicMock())},
            ),
        ):
            response = client.post(
                "/api/v1/deployment/validate-manifests",
                json={"overlay_path": "k8s/overlays/local"},
            )
        assert response.status_code == 500
        body = response.get_json()
        assert body["success"] is False
        assert "kustomize" in body["message"].lower()


class TestValidateDeploymentDocsRules:
    """Direct coverage of the ``_validate_deployment_docs`` rule matrix.

    Exercising the pure function is much cheaper than driving each rule
    through an overlay + kustomize + route round-trip.
    """

    def test_missing_secret_ref_triggers_error(self):
        from routes.deployment import _validate_deployment_docs

        docs = [
            {
                "kind": "Deployment",
                "metadata": {"name": "orchestrator"},
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": "orchestrator"}},
                        "spec": {
                            "containers": [{"name": "main", "image": "egg:dev"}],
                            "volumes": [
                                {
                                    "name": "creds",
                                    "secret": {"secretName": "ghost-secret"},
                                }
                            ],
                        },
                    }
                },
            }
        ]
        warnings = _validate_deployment_docs(docs, is_k3s=True)
        secret_warnings = [w for w in warnings if w.get("rule") == "secret-missing"]
        assert len(secret_warnings) == 1
        assert secret_warnings[0]["severity"] == "error"
        assert "ghost-secret" in secret_warnings[0]["message"]

    def test_hostpath_missing_on_local_overlay(self):
        from routes.deployment import _validate_deployment_docs

        # One deployment has hostPath (so "local overlay" is inferred);
        # another gateway/orchestrator deployment is missing it.
        docs = [
            {
                "kind": "Deployment",
                "metadata": {"name": "gateway"},
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": "gateway"}},
                        "spec": {
                            "containers": [{"name": "gw", "image": "egg-gateway:dev"}],
                            "volumes": [{"name": "repo", "hostPath": {"path": "/host/repo"}}],
                        },
                    }
                },
            },
            {
                "kind": "Deployment",
                "metadata": {"name": "orchestrator"},
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": "orchestrator"}},
                        "spec": {
                            "containers": [{"name": "orch", "image": "egg-orch:dev"}],
                            "volumes": [],
                        },
                    }
                },
            },
        ]
        warnings = _validate_deployment_docs(docs, is_k3s=False)
        rule_warnings = [w for w in warnings if w.get("rule") == "hostpath-missing"]
        assert len(rule_warnings) == 1
        assert rule_warnings[0]["resource"] == "Deployment/orchestrator"

    def test_image_missing_tag_only_fires_on_k3s(self):
        """Missing tag is a warning on k3s and skipped elsewhere."""
        from routes.deployment import _validate_deployment_docs

        docs = [
            {
                "kind": "Deployment",
                "metadata": {"name": "orchestrator"},
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": "orchestrator"}},
                        "spec": {
                            "containers": [{"name": "main", "image": "egg-orch"}],
                            "volumes": [],
                        },
                    }
                },
            }
        ]

        k3s_warnings = _validate_deployment_docs(docs, is_k3s=True)
        fired = [w for w in k3s_warnings if w.get("rule") == "image-missing-tag"]
        assert len(fired) == 1
        assert fired[0]["severity"] == "warn"

        non_k3s = _validate_deployment_docs(docs, is_k3s=False)
        skipped = [w for w in non_k3s if w.get("skipped") == "not_k3s"]
        assert skipped, "non-k3s run must emit the skipped sentinel"

    def test_service_selector_mismatch(self):
        from routes.deployment import _validate_deployment_docs

        docs = [
            {
                "kind": "Deployment",
                "metadata": {"name": "orchestrator"},
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": "orchestrator"}},
                        "spec": {
                            "containers": [{"name": "main", "image": "egg-orch:dev"}],
                            "volumes": [],
                        },
                    }
                },
            },
            {
                "kind": "Service",
                "metadata": {"name": "orchestrator"},
                "spec": {"selector": {"app": "orchestator"}},  # typo
            },
        ]
        warnings = _validate_deployment_docs(docs, is_k3s=False)
        mism = [w for w in warnings if w.get("rule") == "selector-label-mismatch"]
        assert len(mism) == 1
        assert mism[0]["resource"] == "Service/orchestrator"

    def test_env_var_name_collision(self):
        from routes.deployment import _validate_deployment_docs

        docs = [
            {
                "kind": "Deployment",
                "metadata": {"name": "orchestrator"},
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": "orchestrator"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "main",
                                    "image": "egg-orch:dev",
                                    "env": [
                                        {"name": "LOG_LEVEL", "value": "INFO"},
                                        {"name": "LOG_LEVEL", "value": "DEBUG"},
                                    ],
                                }
                            ],
                            "volumes": [],
                        },
                    }
                },
            },
        ]
        warnings = _validate_deployment_docs(docs, is_k3s=False)
        collisions = [w for w in warnings if w.get("rule") == "env-var-collision"]
        assert len(collisions) == 1
        assert "LOG_LEVEL" in collisions[0]["message"]
        assert collisions[0]["severity"] == "error"

    def test_clean_manifest_emits_no_error_warnings(self):
        """A well-formed k3s overlay produces no error-severity warnings."""
        from routes.deployment import _validate_deployment_docs

        docs = [
            {
                "kind": "Secret",
                "metadata": {"name": "creds"},
            },
            {
                "kind": "Deployment",
                "metadata": {"name": "gateway"},
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": "gateway"}},
                        "spec": {
                            "containers": [
                                {"name": "main", "image": "egg-gateway:dev"},
                            ],
                            "volumes": [
                                {"name": "repo", "hostPath": {"path": "/host/repo"}},
                                {"name": "creds", "secret": {"secretName": "creds"}},
                            ],
                        },
                    }
                },
            },
            {
                "kind": "Service",
                "metadata": {"name": "gateway"},
                "spec": {"selector": {"app": "gateway"}},
            },
        ]
        warnings = _validate_deployment_docs(docs, is_k3s=True)
        errors = [w for w in warnings if w.get("severity") == "error"]
        assert errors == []


# ---------------------------------------------------------------------------
# prune_stale_worktrees
# ---------------------------------------------------------------------------


class TestPruneWorktreesProxy:
    """POST /api/v1/deployment/prune-worktrees."""

    def test_happy_path_proxies_to_gateway(self, client):
        """The route delegates to ``GatewayClient._make_request``."""
        fake_result = {
            "success": True,
            "data": {
                "dry_run": True,
                "git_worktree_prune": [],
                "orphan_dirs": [],
                "removed_count": 0,
                "removed_paths": [],
            },
        }

        fake_client = MagicMock()
        fake_client._make_request.return_value = fake_result

        with patch.dict(
            "sys.modules",
            {
                "gateway_client": MagicMock(
                    get_gateway_client=MagicMock(return_value=fake_client),
                    GatewayError=type("GatewayError", (Exception,), {}),
                )
            },
        ):
            response = client.post(
                "/api/v1/deployment/prune-worktrees",
                json={"dry_run": True},
            )

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["data"]["dry_run"] is True

        # The proxy must use launcher auth and the canonical endpoint.
        kwargs = fake_client._make_request.call_args.kwargs
        args = fake_client._make_request.call_args.args
        assert args[0] == "/api/v1/worktrees/prune"
        assert kwargs["method"] == "POST"
        assert kwargs["data"] == {"dry_run": True}
        assert kwargs["use_launcher_auth"] is True

    def test_dry_run_defaults_to_true_when_unspecified(self, client):
        """Omitted body → dry_run=True."""
        fake_client = MagicMock()
        fake_client._make_request.return_value = {"data": {"dry_run": True}}

        with patch.dict(
            "sys.modules",
            {
                "gateway_client": MagicMock(
                    get_gateway_client=MagicMock(return_value=fake_client),
                    GatewayError=type("GatewayError", (Exception,), {}),
                )
            },
        ):
            response = client.post("/api/v1/deployment/prune-worktrees", json={})

        assert response.status_code == 200
        kwargs = fake_client._make_request.call_args.kwargs
        assert kwargs["data"] == {"dry_run": True}

    def test_explicit_dry_run_false_is_forwarded(self, client):
        fake_client = MagicMock()
        fake_client._make_request.return_value = {"data": {"dry_run": False}}

        with patch.dict(
            "sys.modules",
            {
                "gateway_client": MagicMock(
                    get_gateway_client=MagicMock(return_value=fake_client),
                    GatewayError=type("GatewayError", (Exception,), {}),
                )
            },
        ):
            response = client.post(
                "/api/v1/deployment/prune-worktrees",
                json={"dry_run": False},
            )
        assert response.status_code == 200
        kwargs = fake_client._make_request.call_args.kwargs
        assert kwargs["data"] == {"dry_run": False}

    def test_gateway_error_with_status_code_propagates(self, client):
        """GatewayError with ``status_code`` is forwarded on the proxy response."""

        class _GwErr(Exception):
            def __init__(self, msg, *, status_code=None):
                super().__init__(msg)
                self.status_code = status_code

        fake_client = MagicMock()
        fake_client._make_request.side_effect = _GwErr("locked", status_code=409)

        with patch.dict(
            "sys.modules",
            {
                "gateway_client": MagicMock(
                    get_gateway_client=MagicMock(return_value=fake_client),
                    GatewayError=_GwErr,
                )
            },
        ):
            response = client.post(
                "/api/v1/deployment/prune-worktrees",
                json={"dry_run": True},
            )
        assert response.status_code == 409
        body = response.get_json()
        assert body["success"] is False
        assert "locked" in body["message"]

    def test_unexpected_exception_becomes_502(self, client):
        """Any non-GatewayError is mapped to a 502 upstream error."""

        class _GwErr(Exception):
            pass

        fake_client = MagicMock()
        fake_client._make_request.side_effect = RuntimeError("network segfault")

        with patch.dict(
            "sys.modules",
            {
                "gateway_client": MagicMock(
                    get_gateway_client=MagicMock(return_value=fake_client),
                    GatewayError=_GwErr,
                )
            },
        ):
            response = client.post(
                "/api/v1/deployment/prune-worktrees",
                json={"dry_run": True},
            )
        assert response.status_code == 502
        assert response.get_json()["success"] is False


# ---------------------------------------------------------------------------
# validate_network_isolation
# ---------------------------------------------------------------------------


class TestValidateNetworkIsolationRoute:
    """POST /api/v1/deployment/validate-network-isolation."""

    def test_docker_runtime_returns_not_available(self, client, monkeypatch):
        monkeypatch.setenv("EGG_RUNTIME", "docker")
        response = client.post(
            "/api/v1/deployment/validate-network-isolation",
            json={"pipeline_id": "p1", "role": "coder"},
        )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["error"] == "not_available_on_runtime"

    def test_non_enforcing_cni_short_circuits(self, client, monkeypatch):
        """When the CNI doesn't enforce NetworkPolicy we refuse to run the probe."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        with (
            patch("routes.deployment._detect_cni", return_value=("flannel", False)),
            patch.dict(
                "sys.modules",
                {"kubernetes_client": MagicMock(get_kubernetes_client=MagicMock())},
            ),
        ):
            response = client.post(
                "/api/v1/deployment/validate-network-isolation",
                json={"pipeline_id": "p1", "role": "coder"},
            )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["error"] == "network_policy_enforcement_not_detected"
        assert data["cni"] == "flannel"

    def test_enforcing_cni_submits_probe_and_returns_result(self, client, monkeypatch):
        """With enforcement detected the route launches + reaps a probe pod."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        fake_pod = MagicMock()
        fake_pod.metadata.name = "egg-probe-abc123"
        fake_log = (
            '{"gateway_reachable": true, "internet_blocked": true, '
            '"agent_pods_unreachable": true, "orchestrator_api_reachable": true}'
        )

        with (
            patch("routes.deployment._detect_cni", return_value=("calico", True)),
            patch("routes.deployment._submit_probe_job") as submit_mock,
            patch(
                "routes.deployment._wait_for_probe_pod",
                return_value=fake_pod,
            ),
            patch(
                "routes.deployment._read_probe_log",
                return_value=fake_log,
            ),
            patch("routes.deployment._delete_probe_job") as delete_mock,
            patch.dict(
                "sys.modules",
                {"kubernetes_client": MagicMock(get_kubernetes_client=MagicMock())},
            ),
        ):
            response = client.post(
                "/api/v1/deployment/validate-network-isolation",
                json={"pipeline_id": "p1", "role": "coder"},
            )

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert "probe_id" in data
        assert data["result"]["gateway_reachable"] is True
        assert data["result"]["internet_blocked"] is True
        # Finally-block cleanup must run.
        assert delete_mock.called
        assert submit_mock.called

    def test_probe_timeout_still_returns_structured_payload(self, client, monkeypatch):
        """If the pod never finishes, the route reports ``probe_timeout``."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        with (
            patch("routes.deployment._detect_cni", return_value=("calico", True)),
            patch("routes.deployment._submit_probe_job"),
            patch("routes.deployment._wait_for_probe_pod", return_value=None),
            patch("routes.deployment._delete_probe_job") as delete_mock,
            patch.dict(
                "sys.modules",
                {"kubernetes_client": MagicMock(get_kubernetes_client=MagicMock())},
            ),
        ):
            response = client.post(
                "/api/v1/deployment/validate-network-isolation",
                json={"pipeline_id": "p1"},
            )
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["error"] == "probe_timeout"
        assert delete_mock.called

    def test_probe_submit_failure_returns_500(self, client, monkeypatch):
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        with (
            patch("routes.deployment._detect_cni", return_value=("calico", True)),
            patch(
                "routes.deployment._submit_probe_job",
                side_effect=RuntimeError("api down"),
            ),
            patch.dict(
                "sys.modules",
                {"kubernetes_client": MagicMock(get_kubernetes_client=MagicMock())},
            ),
        ):
            response = client.post(
                "/api/v1/deployment/validate-network-isolation",
                json={"pipeline_id": "p1"},
            )
        assert response.status_code == 500
        body = response.get_json()
        assert body["success"] is False


class TestProbeManifestAndEnv:
    """Unit coverage of the probe-job builders (no network)."""

    def test_probe_env_excludes_lifecycle_secret(self, monkeypatch):
        """The probe pod must never receive the lifecycle or session secrets."""
        from routes.deployment import _build_probe_env

        monkeypatch.setenv("EGG_LIFECYCLE_SECRET", "do-not-leak")
        monkeypatch.setenv("EGG_SESSION_TOKEN", "also-do-not-leak")
        monkeypatch.setenv("GATEWAY_URL", f"http://gw:{TEST_GATEWAY_PORT}")
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orch:9849")

        env = _build_probe_env()
        assert "EGG_LIFECYCLE_SECRET" not in env
        assert "EGG_SESSION_TOKEN" not in env
        assert env["GATEWAY_URL"] == f"http://gw:{TEST_GATEWAY_PORT}"
        assert env["EGG_ORCHESTRATOR_URL"] == "http://orch:9849"

    def test_probe_env_missing_urls_default_to_empty(self, monkeypatch):
        from routes.deployment import _build_probe_env

        monkeypatch.delenv("GATEWAY_URL", raising=False)
        monkeypatch.delenv("EGG_ORCHESTRATOR_URL", raising=False)
        env = _build_probe_env()
        assert env == {"GATEWAY_URL": "", "EGG_ORCHESTRATOR_URL": ""}

    def test_probe_manifest_has_expected_labels_and_safety(self):
        from routes.deployment import _build_probe_job_manifest

        manifest = _build_probe_job_manifest(
            pipeline_id="p42",
            role="coder",
            probe_id="abc123",
            image="egg:latest",
        )
        labels = manifest["metadata"]["labels"]
        assert labels["egg.probe"] == "true"
        assert labels["egg.io/probe-id"] == "abc123"
        assert labels["egg.pipeline.id"] == "p42"
        assert labels["egg.agent.role"] == "coder"

        spec = manifest["spec"]
        assert spec["ttlSecondsAfterFinished"] == 0
        assert spec["activeDeadlineSeconds"] == 30
        assert spec["backoffLimit"] == 0

        pod_spec = spec["template"]["spec"]
        assert pod_spec["restartPolicy"] == "Never"
        assert pod_spec["automountServiceAccountToken"] is False

        container = pod_spec["containers"][0]
        assert container["imagePullPolicy"] == "IfNotPresent"
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]


# ---------------------------------------------------------------------------
# rebuild_and_rollout + stream reader
# ---------------------------------------------------------------------------


class TestRebuildAndRolloutRoute:
    """POST /api/v1/deployment/rebuild-and-rollout and its stream endpoint."""

    def test_docker_runtime_returns_not_available(self, client, monkeypatch):
        monkeypatch.setenv("EGG_RUNTIME", "docker")
        response = client.post("/api/v1/deployment/rebuild-and-rollout", json={})
        assert response.status_code == 200
        assert response.get_json()["data"]["error"] == "not_available_on_runtime"

    def test_missing_repo_path_returns_500(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path / "nope"))
        with patch(
            "routes.deployment._probe_kubernetes_reachable",
            return_value=(True, None),
        ):
            response = client.post("/api/v1/deployment/rebuild-and-rollout", json={})
        assert response.status_code == 500
        assert "not found" in response.get_json()["message"]

    def test_unreachable_cluster_returns_runtime_detection_failed(
        self, client, monkeypatch, tmp_path
    ):
        """Refuse rebuild when apiserver is unreachable (#1850)."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
        with patch(
            "routes.deployment._probe_kubernetes_reachable",
            return_value=(False, "apiserver_unreachable: connection refused"),
        ):
            response = client.post("/api/v1/deployment/rebuild-and-rollout", json={})
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["error"] == "runtime_detection_failed"
        assert data["runtime"] == "unknown"
        assert "apiserver_unreachable" in data["detail"]

    def test_first_call_returns_202_with_stream_id(self, client, monkeypatch, tmp_path):
        """Initial call returns 202 and a progress_stream_id."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))

        started = threading.Event()

        def _fake_runner(stream_id, cwd, *, runner=None):  # noqa: ARG001
            started.set()
            # Minimal terminal record so the worker releases the lock.
            from routes import deployment as dep_mod

            dep_mod._stream_append(
                stream_id,
                {"phase": "done", "exit_code": 0, "rolled_out_images": {}},
            )
            dep_mod._stream_mark_done(stream_id)
            from routes.deployment import _REBUILD_LOCK

            with _REBUILD_LOCK:
                dep_mod._REBUILD_IN_PROGRESS = False
                dep_mod._REBUILD_ACTIVE_STREAM_ID = None

        with (
            patch(
                "routes.deployment._run_redeploy_subprocess",
                side_effect=_fake_runner,
            ),
            patch(
                "routes.deployment._probe_kubernetes_reachable",
                return_value=(True, None),
            ),
        ):
            response = client.post("/api/v1/deployment/rebuild-and-rollout", json={})
            assert response.status_code == 202
            body = response.get_json()
            assert body["success"] is True
            assert "progress_stream_id" in body["data"]
            started.wait(timeout=2.0)

    def test_concurrent_call_returns_409_with_existing_stream_id(
        self, client, monkeypatch, tmp_path
    ):
        """A second call while a rollout is live returns 409 with the active stream id."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))

        from routes import deployment as dep_mod

        # Simulate an already-running rollout.
        dep_mod._REBUILD_IN_PROGRESS = True
        dep_mod._REBUILD_ACTIVE_STREAM_ID = "existing-stream"

        with patch(
            "routes.deployment._probe_kubernetes_reachable",
            return_value=(True, None),
        ):
            response = client.post("/api/v1/deployment/rebuild-and-rollout", json={})
        assert response.status_code == 409
        body = response.get_json()
        assert body["success"] is False
        assert body["data"]["error"] == "rollout_already_in_progress"
        assert body["data"]["progress_stream_id"] == "existing-stream"

    def test_stream_read_returns_404_for_unknown_id(self, client):
        response = client.get("/api/v1/deployment/rebuild-and-rollout/streams/nope")
        assert response.status_code == 404
        assert response.get_json()["success"] is False

    def test_stream_read_returns_events_with_next_since(self, client):
        """GET /streams/<id> returns buffered events and a monotonically-increasing cursor."""
        from routes import deployment as dep_mod

        dep_mod._STREAM_BUFFERS["s1"] = deque(
            [
                {"phase": "line", "line": "build step 1"},
                {"phase": "line", "line": "build step 2"},
                {"phase": "done", "exit_code": 0, "rolled_out_images": {}},
            ]
        )
        dep_mod._STREAM_TERMINATED.add("s1")

        response = client.get("/api/v1/deployment/rebuild-and-rollout/streams/s1")
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["stream_id"] == "s1"
        assert data["done"] is True
        assert data["next_since"] == 3
        assert len(data["events"]) == 3

    def test_stream_read_honors_since_cursor(self, client):
        """``?since=N`` slices the buffer and bumps ``next_since`` correctly."""
        from routes import deployment as dep_mod

        dep_mod._STREAM_BUFFERS["s2"] = deque(
            [
                {"phase": "line", "line": "a"},
                {"phase": "line", "line": "b"},
                {"phase": "line", "line": "c"},
            ]
        )
        # Not terminated yet.
        response = client.get("/api/v1/deployment/rebuild-and-rollout/streams/s2?since=1")
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["events"] == [
            {"phase": "line", "line": "b"},
            {"phase": "line", "line": "c"},
        ]
        assert data["next_since"] == 3
        assert data["done"] is False

    def test_stream_read_tolerates_garbage_since_value(self, client):
        from routes import deployment as dep_mod

        dep_mod._STREAM_BUFFERS["s3"] = deque([{"phase": "line", "line": "x"}])
        response = client.get("/api/v1/deployment/rebuild-and-rollout/streams/s3?since=abc")
        # Bad ``since`` coerces to 0, so we get every event.
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert len(data["events"]) == 1


class TestRunRedeploySubprocess:
    """Direct coverage of the worker thread helper.

    We inject a fake ``runner`` so no real ``make redeploy`` is spawned;
    the point is to verify the stream-buffer contract and lock release.
    """

    def test_lines_and_terminal_done_record_are_appended(self, tmp_path):
        from routes import deployment as dep_mod
        from routes.deployment import _run_redeploy_subprocess

        dep_mod._REBUILD_IN_PROGRESS = True
        dep_mod._REBUILD_ACTIVE_STREAM_ID = "test-stream"

        class _FakePopen:
            def __init__(self, *args, **kwargs):
                self.stdout = iter(
                    [
                        "==> compiling orchestrator\n",
                        "==> unpacking docker.io/library/egg-orchestrator:dev sha256:abc\n",
                        "==> done\n",
                    ]
                )

            def wait(self):
                return 0

            def poll(self):
                return 0

        _run_redeploy_subprocess("test-stream", str(tmp_path), runner=_FakePopen)

        events, done = dep_mod._stream_snapshot("test-stream")
        assert done is True
        phases = [e["phase"] for e in events]
        assert phases.count("line") == 3
        assert phases[-1] == "done"
        terminal = events[-1]
        assert terminal["exit_code"] == 0
        # Lock must be released so the next call can proceed.
        assert dep_mod._REBUILD_IN_PROGRESS is False
        assert dep_mod._REBUILD_ACTIVE_STREAM_ID is None

    def test_subprocess_exception_still_marks_done(self, tmp_path):
        from routes import deployment as dep_mod
        from routes.deployment import _run_redeploy_subprocess

        dep_mod._REBUILD_IN_PROGRESS = True
        dep_mod._REBUILD_ACTIVE_STREAM_ID = "error-stream"

        def _broken_popen(*args, **kwargs):  # noqa: ARG001
            raise OSError("make: not found")

        _run_redeploy_subprocess("error-stream", str(tmp_path), runner=_broken_popen)

        events, done = dep_mod._stream_snapshot("error-stream")
        assert done is True
        # Error + terminal done, in that order.
        phases = [e["phase"] for e in events]
        assert "error" in phases
        assert phases[-1] == "done"
        # Global lock released even on failure.
        assert dep_mod._REBUILD_IN_PROGRESS is False
        assert dep_mod._REBUILD_ACTIVE_STREAM_ID is None

    def test_stream_helpers_are_thread_safe(self):
        """Concurrent _stream_append writers don't drop events."""
        from routes import deployment as dep_mod

        stream_id = "concurrent"

        def _writer(i: int) -> None:
            for j in range(20):
                dep_mod._stream_append(stream_id, {"phase": "line", "line": f"{i}-{j}"})

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events, _done = dep_mod._stream_snapshot(stream_id)
        assert len(events) == 100


# ---------------------------------------------------------------------------
# get_service_logs (#1853)
# ---------------------------------------------------------------------------


class TestGetServiceLogsRoute:
    """GET /api/v1/deployment/logs."""

    def test_docker_runtime_returns_not_available(self, client, monkeypatch):
        monkeypatch.setenv("EGG_RUNTIME", "docker")
        response = client.get("/api/v1/deployment/logs?service=gateway")
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["error"] == "not_available_on_runtime"

    def test_missing_service_returns_400(self, client, monkeypatch):
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        response = client.get("/api/v1/deployment/logs")
        assert response.status_code == 400
        assert response.get_json()["success"] is False

    def test_unknown_service_returns_400(self, client, monkeypatch):
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        response = client.get("/api/v1/deployment/logs?service=etcd")
        assert response.status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert "gateway" in body["message"]
        assert "orchestrator" in body["message"]

    def test_invalid_since_seconds_returns_400(self, client, monkeypatch):
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        response = client.get("/api/v1/deployment/logs?service=gateway&since_seconds=not-a-number")
        assert response.status_code == 400

    def test_happy_path_returns_pod_logs(self, client, monkeypatch):
        """When the Deployment exists the route returns its pod logs."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")
        monkeypatch.setenv("EGG_K8S_NAMESPACE", "egg-test")

        fake_k8s = MagicMock()
        fake_k8s.get_service_logs.return_value = {
            "service": "gateway",
            "namespace": "egg-test",
            "pods": [{"pod": "gateway-abc", "logs": f"listening on :{GATEWAY_PORT}\n"}],
        }

        with patch.dict(
            "sys.modules",
            {
                "kubernetes_client": MagicMock(
                    get_kubernetes_client=MagicMock(return_value=fake_k8s),
                    PodNotFoundError=type("PodNotFoundError", (Exception,), {}),
                    JobOperationError=type("JobOperationError", (Exception,), {}),
                )
            },
        ):
            response = client.get(
                "/api/v1/deployment/logs?service=gateway&lines=50&since_seconds=120"
            )

        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["service"] == "gateway"
        assert data["namespace"] == "egg-test"
        assert data["pods"][0]["logs"].startswith("listening")
        # The handler must have forwarded the parsed integer arguments.
        kwargs = fake_k8s.get_service_logs.call_args.kwargs
        assert kwargs["service"] == "gateway"
        assert kwargs["namespace"] == "egg-test"
        assert kwargs["tail_lines"] == 50
        assert kwargs["since_seconds"] == 120

    def test_lines_is_capped_at_max(self, client, monkeypatch):
        """A huge ``lines`` value is clamped to the module cap."""
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        from routes.deployment import _MAX_LOG_LINES

        fake_k8s = MagicMock()
        fake_k8s.get_service_logs.return_value = {
            "service": "gateway",
            "namespace": "egg-system",
            "pods": [],
        }

        with patch.dict(
            "sys.modules",
            {
                "kubernetes_client": MagicMock(
                    get_kubernetes_client=MagicMock(return_value=fake_k8s),
                    PodNotFoundError=type("PodNotFoundError", (Exception,), {}),
                    JobOperationError=type("JobOperationError", (Exception,), {}),
                )
            },
        ):
            client.get(f"/api/v1/deployment/logs?service=gateway&lines={_MAX_LOG_LINES * 10}")

        assert fake_k8s.get_service_logs.call_args.kwargs["tail_lines"] == _MAX_LOG_LINES

    def test_pod_not_found_returns_404(self, client, monkeypatch):
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        PodNotFoundError = type("PodNotFoundError", (Exception,), {})
        fake_k8s = MagicMock()
        fake_k8s.get_service_logs.side_effect = PodNotFoundError(
            "Deployment gateway not found in egg-system"
        )

        with patch.dict(
            "sys.modules",
            {
                "kubernetes_client": MagicMock(
                    get_kubernetes_client=MagicMock(return_value=fake_k8s),
                    PodNotFoundError=PodNotFoundError,
                    JobOperationError=type("JobOperationError", (Exception,), {}),
                )
            },
        ):
            response = client.get("/api/v1/deployment/logs?service=gateway")

        assert response.status_code == 404
        assert response.get_json()["success"] is False

    def test_job_operation_error_returns_500(self, client, monkeypatch):
        monkeypatch.setenv("EGG_RUNTIME", "kubernetes")

        JobOperationError = type("JobOperationError", (Exception,), {})
        fake_k8s = MagicMock()
        fake_k8s.get_service_logs.side_effect = JobOperationError("api down")

        with patch.dict(
            "sys.modules",
            {
                "kubernetes_client": MagicMock(
                    get_kubernetes_client=MagicMock(return_value=fake_k8s),
                    PodNotFoundError=type("PodNotFoundError", (Exception,), {}),
                    JobOperationError=JobOperationError,
                )
            },
        ):
            response = client.get("/api/v1/deployment/logs?service=orchestrator")

        assert response.status_code == 500
        assert "api down" in response.get_json()["message"]


# ---------------------------------------------------------------------------
# Lifecycle-secret auth regression coverage (parity with #1769)
# ---------------------------------------------------------------------------


class TestDeploymentLifecycleSecretAuth:
    """All five routes must reject unauthenticated callers.

    Agents in the pipeline never hold the lifecycle secret, so this is
    the practical guard against a rogue agent pod hitting the diagnostic
    endpoints directly.
    """

    def test_get_context_requires_bearer_token(self, client):
        response = client.get("/api/v1/deployment/context", _lifecycle_auth=False)
        assert response.status_code == 401

    def test_validate_manifests_requires_bearer_token(self, client):
        response = client.post(
            "/api/v1/deployment/validate-manifests",
            json={},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_prune_worktrees_requires_bearer_token(self, client):
        response = client.post(
            "/api/v1/deployment/prune-worktrees",
            json={"dry_run": True},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_validate_network_isolation_requires_bearer_token(self, client):
        response = client.post(
            "/api/v1/deployment/validate-network-isolation",
            json={"pipeline_id": "p1"},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_rebuild_and_rollout_requires_bearer_token(self, client):
        response = client.post(
            "/api/v1/deployment/rebuild-and-rollout",
            json={},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_stream_reader_requires_bearer_token(self, client):
        response = client.get(
            "/api/v1/deployment/rebuild-and-rollout/streams/anything",
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_get_service_logs_requires_bearer_token(self, client):
        response = client.get(
            "/api/v1/deployment/logs?service=gateway",
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_wrong_bearer_token_is_rejected(self, client):
        response = client.get(
            "/api/v1/deployment/context",
            headers={"Authorization": "Bearer nope"},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_correct_bearer_token_passes_through(self, client, lifecycle_auth_headers):
        """The happy-path header reaches the handler (Docker degrade payload is fine)."""
        response = client.get(
            "/api/v1/deployment/context",
            headers=lifecycle_auth_headers,
            _lifecycle_auth=False,
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# PROBE_COMMAND_TEMPLATE shape guard
# ---------------------------------------------------------------------------


class TestProbeCommandTemplate:
    """The probe script contract is referenced by both the Job manifest and
    the log parser — pin its shape so a rename doesn't silently break
    ``_parse_probe_output``.
    """

    def test_template_references_expected_env_vars(self):
        from routes.deployment import PROBE_COMMAND_TEMPLATE

        assert "GATEWAY_URL" in PROBE_COMMAND_TEMPLATE
        assert "EGG_ORCHESTRATOR_URL" in PROBE_COMMAND_TEMPLATE
        # All four probe keys show up in the JSON printer.
        for key in (
            "gateway_reachable",
            "internet_blocked",
            "agent_pods_unreachable",
            "orchestrator_api_reachable",
        ):
            assert key in PROBE_COMMAND_TEMPLATE

    def test_template_does_not_reference_secrets(self):
        from routes.deployment import PROBE_COMMAND_TEMPLATE

        assert "EGG_LIFECYCLE_SECRET" not in PROBE_COMMAND_TEMPLATE
        assert "EGG_SESSION_TOKEN" not in PROBE_COMMAND_TEMPLATE

    def test_template_contains_no_backticks(self):
        """Backticks in the unquoted ``<<PY`` heredoc body would be
        evaluated by ``/bin/sh`` as command substitution before
        ``python3`` ever sees the source — at best emitting
        ``sh: ...: not found`` to the pod log, at worst substituting
        non-empty command output into the Python literal itself.
        Guard against the regression here so a future edit (e.g.
        adding ``\\`hostname\\``` somewhere) can't sneak in silently.
        """
        from routes.deployment import PROBE_COMMAND_TEMPLATE

        assert "`" not in PROBE_COMMAND_TEMPLATE, (
            "PROBE_COMMAND_TEMPLATE contains a backtick — under /bin/sh "
            "this is command substitution, not a markdown formatting hint"
        )

    def test_template_is_shell_syntax_valid(self):
        """``/bin/sh -n`` parses the template without error.

        Catches stray quoting / heredoc-delimiter mistakes that would
        otherwise only surface when a probe Job runs in-cluster.
        """
        import shutil
        import subprocess

        from routes.deployment import PROBE_COMMAND_TEMPLATE

        sh = shutil.which("sh") or "/bin/sh"
        result = subprocess.run(
            [sh, "-n"],
            input=PROBE_COMMAND_TEMPLATE,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, (
            f"sh -n rejected PROBE_COMMAND_TEMPLATE: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# module sanity
# ---------------------------------------------------------------------------


def test_expected_public_symbols_are_exported():
    """Tests rely on the helpers in ``__all__`` remaining public."""
    from routes import deployment as dep_mod

    for sym in (
        "deployment_bp",
        "PROBE_COMMAND_TEMPLATE",
        "_build_probe_env",
        "_build_probe_job_manifest",
        "_validate_deployment_docs",
        "_build_deployment_context_payload",
        "_run_redeploy_subprocess",
        "_stream_snapshot",
        "_stream_append",
        "_stream_mark_done",
        "_STREAM_BUFFERS",
        "_STREAM_TERMINATED",
        "_SERVICE_LOG_ALLOWLIST",
        "_MAX_LOG_LINES",
    ):
        assert hasattr(dep_mod, sym), f"missing public symbol: {sym}"


def test_stream_snapshot_returns_empty_for_unknown_stream():
    """Internal snapshot helper returns an empty list for an unknown id."""
    from routes.deployment import _stream_snapshot

    events, done = _stream_snapshot("never-existed")
    assert events == []
    assert done is False


def test_time_helpers_dont_deadlock_under_load():
    """The rebuild-lock is a regular Lock — make sure we don't hold it on read."""
    from routes.deployment import _stream_append, _stream_mark_done, _stream_snapshot

    _stream_append("race", {"phase": "line", "line": "hello"})
    _stream_mark_done("race")

    start = time.time()
    for _ in range(50):
        _stream_snapshot("race")
    assert time.time() - start < 1.0


# ---------------------------------------------------------------------------
# NACK-fix coverage for coder commit ac5c4900f
# ---------------------------------------------------------------------------


class TestStreamRetentionReaper:
    """MEDIUM-3 NACK fix: ``_STREAM_BUFFERS`` must not grow unbounded.

    Finished streams beyond ``_STREAM_RETENTION`` are evicted FIFO when
    ``_stream_mark_done`` runs. The live in-flight stream is never
    touched because only terminated streams are in the eviction pool.
    """

    def test_retention_cap_evicts_oldest_terminated_streams(self):
        from routes import deployment as dep_mod

        # Reduce the cap to a tiny number so we don't have to create
        # hundreds of streams. Restore at the end.
        original_cap = dep_mod._STREAM_RETENTION
        dep_mod._STREAM_RETENTION = 3
        try:
            for i in range(6):
                dep_mod._stream_append(f"s{i}", {"phase": "line", "line": str(i)})
                dep_mod._stream_mark_done(f"s{i}")

            # Only the last 3 must survive (FIFO eviction by termination ts).
            surviving = [sid for sid in dep_mod._STREAM_BUFFERS.keys() if sid.startswith("s")]
            assert len(surviving) <= 3
            # s0, s1, s2 were the oldest, so they should be gone.
            assert "s0" not in dep_mod._STREAM_BUFFERS
            assert "s0" not in dep_mod._STREAM_TERMINATED
            assert "s0" not in dep_mod._STREAM_TERMINATION_TS
            # The most recent ones must remain.
            assert "s5" in dep_mod._STREAM_BUFFERS
            assert "s5" in dep_mod._STREAM_TERMINATED
        finally:
            dep_mod._STREAM_RETENTION = original_cap

    def test_retention_does_not_evict_live_streams(self):
        """Streams that haven't been marked done must never be reaped."""
        from routes import deployment as dep_mod

        original_cap = dep_mod._STREAM_RETENTION
        dep_mod._STREAM_RETENTION = 2
        try:
            dep_mod._stream_append("live", {"phase": "line", "line": "still running"})
            # ... and a bunch of terminated streams.
            for i in range(5):
                dep_mod._stream_append(f"done-{i}", {"phase": "line"})
                dep_mod._stream_mark_done(f"done-{i}")

            assert "live" in dep_mod._STREAM_BUFFERS, (
                "live stream must never be reaped -- only terminated streams are eligible"
            )
            assert "live" not in dep_mod._STREAM_TERMINATED
        finally:
            dep_mod._STREAM_RETENTION = original_cap


class TestRedeployWatchdog:
    """MEDIUM-1 NACK fix: a hung ``make redeploy`` must not pin
    ``_REBUILD_IN_PROGRESS`` forever. The watchdog kills the subprocess
    after ``_REDEPLOY_SUBPROCESS_TIMEOUT_SEC`` and emits a
    ``phase: "timeout"`` event.
    """

    def test_watchdog_kills_long_running_subprocess(self):
        from routes import deployment as dep_mod

        killed = threading.Event()

        class SlowProc:
            def __init__(self):
                # Never-ending stdout generator.
                self.stdout = iter(())
                self._killed = False

            def wait(self):
                # Simulate the real Popen.wait blocking until proc is killed.
                killed.wait(timeout=5.0)
                return -9  # SIGKILL exit code

            def poll(self):
                return -9 if self._killed else None

            def kill(self):
                self._killed = True
                killed.set()

        proc_instance = SlowProc()

        def fake_popen(*_args, **_kwargs):
            return proc_instance

        dep_mod._REBUILD_IN_PROGRESS = True

        # Run with a 0.1s timeout so the watchdog fires almost instantly.
        dep_mod._run_redeploy_subprocess(
            "watchdog-test",
            cwd="/tmp",
            runner=fake_popen,
            timeout_sec=0,  # expire immediately
        )

        # After the run returns, the rebuild flag must be cleared so the
        # next caller isn't pinned at 409.
        assert dep_mod._REBUILD_IN_PROGRESS is False

        # The stream must contain the timeout event + terminal done.
        events, done = dep_mod._stream_snapshot("watchdog-test")
        assert done is True
        phases = [e["phase"] for e in events]
        assert "timeout" in phases, f"expected timeout event, got phases: {phases}"
        assert phases[-1] == "done"
        # The done event marks timed_out=True so callers see the cause.
        done_event = next(e for e in events if e["phase"] == "done")
        assert done_event.get("timed_out") is True


class TestValidateDeploymentManifestsOverlayGuard:
    """Defense-in-depth NACK fix: ``overlay_path`` must not escape the
    known repo roots. An authenticated caller otherwise could probe
    arbitrary filesystem paths via 200/404 differentiation.
    """

    def test_absolute_path_outside_repo_root_is_rejected(
        self, client, lifecycle_auth_headers, monkeypatch
    ):
        monkeypatch.setattr(
            "routes.deployment._current_runtime", lambda: "kubernetes", raising=False
        )
        # A path outside any repo root must come back as a 400 (not a
        # 404 that would leak filesystem shape).
        resp = client.post(
            "/api/v1/deployment/validate-manifests",
            json={"overlay_path": "/etc/shadow"},
            headers=lifecycle_auth_headers,
        )
        assert resp.status_code == 400, (
            f"path traversal must be rejected with 400 (got {resp.status_code})"
        )
        body = resp.get_json()
        assert body["success"] is False
        assert "repo root" in (body.get("message") or "").lower()

    def test_relative_path_inside_repo_root_resolves(
        self, client, lifecycle_auth_headers, monkeypatch
    ):
        """Relative paths that resolve inside the repo root are allowed —
        the guard is narrow and doesn't break happy-path callers."""
        monkeypatch.setattr(
            "routes.deployment._current_runtime", lambda: "kubernetes", raising=False
        )

        # Stub _run_kustomize so we exercise just the guard (not kustomize).
        with (
            patch("routes.deployment._run_kustomize", return_value=[]),
            patch("routes.deployment._detect_k3s", return_value=(False, "unknown")),
        ):
            # Use the default overlay which must exist under one of the
            # known repo roots in the test environment.
            resp = client.post(
                "/api/v1/deployment/validate-manifests",
                json={},  # no overlay_path → default k8s/overlays/local
                headers=lifecycle_auth_headers,
            )
        # Either 200 (overlay exists) or 404 (overlay not present in this
        # checkout) — what we want to verify is: NOT 400, i.e. the guard
        # let it through.
        assert resp.status_code != 400, (
            f"default overlay_path was mistakenly flagged as out-of-scope: {resp.json}"
        )


class TestKustomizeUnavailable:
    """Defense-in-depth NACK fix: when neither ``kustomize`` nor ``kubectl``
    is on PATH, ``_run_kustomize`` surfaces a structured
    ``kustomize_unavailable`` error instead of bubbling FileNotFoundError.
    """

    def test_kustomize_unavailable_raises_structured_error(self, tmp_path, monkeypatch):
        from routes.deployment import _run_kustomize

        # Ensure the env binary doesn't exist.
        monkeypatch.setenv("EGG_KUSTOMIZE_BIN", "/tmp/definitely-does-not-exist-kustomize-xyz")

        def fake_run(cmd, *_a, **_kw):
            raise FileNotFoundError(f"no such binary: {cmd[0]}")

        monkeypatch.setattr("routes.deployment.subprocess.run", fake_run)

        overlay = tmp_path / "overlay"
        overlay.mkdir()

        with pytest.raises(RuntimeError, match="kustomize_unavailable"):
            _run_kustomize(overlay)


class TestValidateNetworkIsolationLabelValueGuard:
    """Defense-in-depth NACK fix: invalid K8s label values are rejected
    with a structured 400 instead of an opaque apiserver 422.
    """

    def test_invalid_pipeline_id_label_returns_400(
        self, client, lifecycle_auth_headers, monkeypatch
    ):
        monkeypatch.setattr(
            "routes.deployment._current_runtime", lambda: "kubernetes", raising=False
        )
        resp = client.post(
            "/api/v1/deployment/validate-network-isolation",
            # Slashes aren't valid in label values.
            json={"pipeline_id": "invalid/pipeline-id", "role": "coder"},
            headers=lifecycle_auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "pipeline_id" in body["message"].lower()

    def test_invalid_role_label_returns_400(self, client, lifecycle_auth_headers, monkeypatch):
        monkeypatch.setattr(
            "routes.deployment._current_runtime", lambda: "kubernetes", raising=False
        )
        resp = client.post(
            "/api/v1/deployment/validate-network-isolation",
            # Leading dash isn't valid.
            json={"pipeline_id": "ok", "role": "-bad-role"},
            headers=lifecycle_auth_headers,
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "role" in body["message"].lower()

    def test_valid_label_passes_guard(self, client, lifecycle_auth_headers, monkeypatch):
        """Label values that match the K8s regex must pass the guard.

        We stub the CNI probe so the route short-circuits before making
        actual k8s API calls — the only thing under test here is that
        the label validation doesn't misclassify a valid value.
        """
        monkeypatch.setattr(
            "routes.deployment._current_runtime", lambda: "kubernetes", raising=False
        )
        # CNI-without-enforcement short-circuit returns 200 early.
        fake_k8s = MagicMock()
        with (
            patch("kubernetes_client.get_kubernetes_client", return_value=fake_k8s),
            patch("routes.deployment._detect_cni", return_value=("flannel", False)),
        ):
            resp = client.post(
                "/api/v1/deployment/validate-network-isolation",
                json={"pipeline_id": "issue-1759-v3", "role": "coder"},
                headers=lifecycle_auth_headers,
            )
        # Valid labels must not trip the 400.
        assert resp.status_code != 400, f"valid labels were rejected as invalid: {resp.json}"


class TestK8sLabelValueRegex:
    """Unit coverage for the shared label-value regex. The 63-char cap
    matters: going over it elsewhere in the code path would 422 at the
    apiserver, which the guard is meant to prevent.
    """

    def test_regex_accepts_normal_values(self):
        from routes.deployment import _K8S_LABEL_VALUE_RE

        for value in ("coder", "issue-1759-v3", "v1.2.3", "a", "test_underscore"):
            assert _K8S_LABEL_VALUE_RE.match(value), f"must accept: {value!r}"

    def test_regex_rejects_obvious_bad_values(self):
        from routes.deployment import _K8S_LABEL_VALUE_RE

        for value in ("with/slash", "-leading-dash", "trailing-dash-", " space", ""):
            assert not _K8S_LABEL_VALUE_RE.match(value), f"must reject: {value!r}"

    def test_regex_caps_length_at_63(self):
        from routes.deployment import _K8S_LABEL_VALUE_RE

        assert _K8S_LABEL_VALUE_RE.match("a" * 63), "63-char value must be accepted"
        assert not _K8S_LABEL_VALUE_RE.match("a" * 64), "64-char value must be rejected"
