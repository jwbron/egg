"""Tests for the Kubernetes startup guard in gateway.main().

When KUBERNETES_SERVICE_HOST is set (i.e. running inside a k8s pod) but
EGG_ORCHESTRATOR_URL is missing, the gateway must exit immediately with
code 1 so the misconfiguration surfaces at deploy time (#1803).
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.parametrize(
    "k8s_host, orch_url, should_exit",
    [
        # In k8s without orchestrator URL → must exit
        ("10.0.0.1", None, True),
        ("10.0.0.1", "", True),
        # In k8s with orchestrator URL → must NOT exit
        ("10.0.0.1", "http://orchestrator.egg-system.svc.cluster.local:9849", False),
        # Not in k8s (no KUBERNETES_SERVICE_HOST) → must NOT exit regardless
        (None, None, False),
        ("", None, False),
    ],
    ids=[
        "k8s-no-url-exits",
        "k8s-empty-url-exits",
        "k8s-with-url-ok",
        "not-k8s-no-url-ok",
        "not-k8s-empty-host-ok",
    ],
)
def test_k8s_orchestrator_url_guard(k8s_host, orch_url, should_exit):
    """Startup guard exits when in k8s without EGG_ORCHESTRATOR_URL."""
    import gateway

    env = {}
    if k8s_host is not None:
        env["KUBERNETES_SERVICE_HOST"] = k8s_host
    if orch_url is not None:
        env["EGG_ORCHESTRATOR_URL"] = orch_url

    with (
        patch.object(gateway, "get_github_client") as mock_ghc,
        patch.object(gateway, "get_session_manager") as mock_sm,
        patch.object(gateway, "get_active_docker_containers", return_value=set()),
        patch.object(gateway, "startup_cleanup", return_value=0),
        patch.object(gateway, "get_launcher_secret", return_value="secret"),
        patch.object(gateway, "serve"),
        patch("signal.signal"),
        patch("argparse.ArgumentParser.parse_args") as mock_args,
        patch.object(gateway.os, "getuid", return_value=1000),
        patch.dict(gateway.os.environ, env, clear=False),
    ):
        # Remove keys that should be absent (None means unset)
        if k8s_host is None:
            gateway.os.environ.pop("KUBERNETES_SERVICE_HOST", None)
        if orch_url is None:
            gateway.os.environ.pop("EGG_ORCHESTRATOR_URL", None)

        mock_args.return_value = MagicMock(host="0.0.0.0", port=9848, debug=False)
        mock_ghc.return_value = MagicMock(
            validate_user_mode_config=MagicMock(return_value=(True, "ok"))
        )
        mock_sm.return_value = MagicMock(
            prune_expired_sessions=MagicMock(return_value=0),
            list_sessions=MagicMock(return_value=[]),
        )

        if should_exit:
            with pytest.raises(SystemExit) as exc_info:
                gateway.main()
            assert exc_info.value.code == 1
        else:
            gateway.main()
