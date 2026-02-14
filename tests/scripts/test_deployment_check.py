"""
Unit tests for DeploymentCheck check runner.

Tests PASS/FAIL/SKIP scenarios, defensive parsing (oversized response,
malformed JSON, timeout), and orchestrator communication errors.
All orchestrator HTTP API calls are mocked.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / ".github" / "scripts"))

from egg_contracts import CheckStatus, Contract, IssueInfo


def _make_contract(**kwargs) -> Contract:
    """Create a minimal contract for testing."""
    defaults = {
        "issue": IssueInfo(number=645, title="Test", url="https://example.com"),
    }
    defaults.update(kwargs)
    return Contract(**defaults)


def _make_deployment_config_file(repo_root: Path, **kwargs):
    """Write a deployment config file to the repo root."""
    egg_dir = repo_root / ".egg"
    egg_dir.mkdir(exist_ok=True)
    config = {
        "services": [{"source_dir": "src/", "service_name": "api"}],
        "health_endpoints": {"api": "/health"},
    }
    config.update(kwargs)
    import yaml

    (egg_dir / "deployment.yml").write_text(yaml.dump(config))


class TestDeploymentCheckSkip:
    """Tests for SKIP scenario (no deployment config)."""

    def test_skip_when_no_config(self, tmp_path):
        from checks.deployment_check import DeploymentCheck

        contract = _make_contract()
        check = DeploymentCheck(contract, tmp_path)
        result = check.run()

        assert result.status == CheckStatus.SKIP
        assert "not opted in" in result.message.lower()


class TestDeploymentCheckFail:
    """Tests for FAIL scenarios."""

    def test_fail_when_orchestrator_unreachable(self, tmp_path):
        from checks.deployment_check import DeploymentCheck

        _make_deployment_config_file(tmp_path)
        contract = _make_contract()
        check = DeploymentCheck(contract, tmp_path)

        with patch.object(check, "_safe_request", return_value=None):
            result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "communicate" in result.message.lower() or "orchestrator" in result.message.lower()

    def test_fail_when_orchestrator_refuses_start(self, tmp_path):
        from checks.deployment_check import DeploymentCheck

        _make_deployment_config_file(tmp_path)
        contract = _make_contract()
        check = DeploymentCheck(contract, tmp_path)

        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.json.return_value = {"success": False, "message": "No config"}
        mock_resp.iter_content.return_value = iter([b'{"success": false, "message": "No config"}'])
        mock_resp.is_redirect = False
        mock_resp._content = b'{"success": false, "message": "No config"}'

        with patch.object(
            check, "_start_devserver", return_value={"success": False, "message": "No config"}
        ):
            with patch.object(check, "_teardown_devserver"):
                result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "refused" in result.message.lower()

    def test_fail_when_health_check_timeout(self, tmp_path):
        from checks.deployment_check import DeploymentCheck

        _make_deployment_config_file(tmp_path)
        contract = _make_contract()
        check = DeploymentCheck(contract, tmp_path)

        with patch.object(
            check,
            "_start_devserver",
            return_value={"success": True, "status": {"status": "starting"}},
        ):
            with patch.object(check, "_wait_for_healthy", return_value=None):
                with patch.object(check, "_teardown_devserver"):
                    result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "timed out" in result.message.lower()

    def test_fail_when_devserver_errors(self, tmp_path):
        from checks.deployment_check import DeploymentCheck

        _make_deployment_config_file(tmp_path)
        contract = _make_contract()
        check = DeploymentCheck(contract, tmp_path)

        with patch.object(
            check,
            "_start_devserver",
            return_value={"success": True, "status": {"status": "starting"}},
        ):
            with patch.object(
                check,
                "_wait_for_healthy",
                return_value={
                    "status": {
                        "status": "error",
                        "error_message": "compose up failed",
                    },
                },
            ):
                with patch.object(check, "_teardown_devserver"):
                    result = check.run()

        assert result.status == CheckStatus.FAIL
        assert "errored" in result.message.lower()


class TestDeploymentCheckPass:
    """Tests for PASS scenario."""

    def test_pass_when_all_checks_pass(self, tmp_path):
        from checks.deployment_check import DeploymentCheck

        _make_deployment_config_file(
            tmp_path,
            validation_tests=[
                {"service": "api", "path": "/test", "expected_status": 200},
            ],
        )
        contract = _make_contract()
        check = DeploymentCheck(contract, tmp_path)

        with patch.object(
            check,
            "_start_devserver",
            return_value={"success": True, "status": {"status": "starting"}},
        ):
            with patch.object(
                check,
                "_wait_for_healthy",
                return_value={
                    "status": {
                        "status": "healthy",
                        "services": {
                            "api": {"ip": "172.34.0.5", "port": 8080, "healthy": True},
                        },
                    },
                },
            ):
                # Mock successful health check and validation responses
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.is_redirect = False
                mock_resp.text = "OK"
                mock_resp.iter_content.return_value = iter([b"OK"])
                mock_resp._content = b"OK"

                with patch.object(check, "_safe_request", return_value=mock_resp):
                    with patch.object(check, "_teardown_devserver"):
                        result = check.run()

        assert result.status == CheckStatus.PASS
        assert "passed" in result.message.lower()


class TestDefensiveParsing:
    """Tests for defensive HTTP response parsing."""

    def test_safe_json_handles_malformed(self):
        from checks.deployment_check import DeploymentCheck

        contract = _make_contract()
        check = DeploymentCheck(contract, Path("/tmp"))

        mock_resp = MagicMock()
        mock_resp.json.side_effect = json.JSONDecodeError("bad", "", 0)

        result = check._safe_json(mock_resp)
        assert result is None

    def test_safe_json_handles_valid(self):
        from checks.deployment_check import DeploymentCheck

        contract = _make_contract()
        check = DeploymentCheck(contract, Path("/tmp"))

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"key": "value"}

        result = check._safe_json(mock_resp)
        assert result == {"key": "value"}

    def test_pipeline_id_from_issue(self):
        from checks.deployment_check import DeploymentCheck

        contract = _make_contract()
        check = DeploymentCheck(contract, Path("/tmp"))
        assert check._get_pipeline_id() == "issue-645"

    def test_pipeline_id_from_pipeline_id(self):
        from checks.deployment_check import DeploymentCheck

        contract = Contract(pipeline_id="local-123")
        check = DeploymentCheck(contract, Path("/tmp"))
        assert check._get_pipeline_id() == "local-123"

    def test_always_tears_down(self, tmp_path):
        """Verify teardown is called even when checks fail."""
        from checks.deployment_check import DeploymentCheck

        _make_deployment_config_file(tmp_path)
        contract = _make_contract()
        check = DeploymentCheck(contract, tmp_path)

        teardown_called = False

        def tracking_teardown(pid):
            nonlocal teardown_called
            teardown_called = True

        with patch.object(check, "_teardown_devserver", side_effect=tracking_teardown):
            with patch.object(check, "_start_devserver", return_value=None):
                result = check.run()

        assert teardown_called
        assert result.status == CheckStatus.FAIL


class TestSafeRequestRedirects:
    """Tests for _safe_request redirect handling."""

    def _make_check(self):
        from checks.deployment_check import DeploymentCheck

        contract = _make_contract()
        return DeploymentCheck(contract, Path("/tmp"))

    def _mock_response(self, status_code=200, is_redirect=False, location=None, body=b"ok"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.is_redirect = is_redirect
        resp.headers = {"Location": location} if location else {}
        resp.iter_content.return_value = iter([body])
        resp._content = body
        return resp

    @patch("checks.deployment_check.requests.request")
    def test_follows_same_host_absolute_redirect(self, mock_request):
        redirect_resp = self._mock_response(
            301, is_redirect=True, location="http://localhost:8080/healthz"
        )
        final_resp = self._mock_response(200, body=b"healthy")
        mock_request.side_effect = [redirect_resp, final_resp]

        check = self._make_check()
        result = check._safe_request("GET", "http://localhost:8080/health")

        assert result is not None
        assert result.status_code == 200
        assert mock_request.call_count == 2

    @patch("checks.deployment_check.requests.request")
    def test_follows_relative_redirect(self, mock_request):
        redirect_resp = self._mock_response(301, is_redirect=True, location="/healthz")
        final_resp = self._mock_response(200, body=b"healthy")
        mock_request.side_effect = [redirect_resp, final_resp]

        check = self._make_check()
        result = check._safe_request("GET", "http://172.20.0.2:8080/health")

        assert result is not None
        assert result.status_code == 200
        # Verify the resolved URL was used for the second request
        second_call_url = mock_request.call_args_list[1][0][1]
        assert second_call_url == "http://172.20.0.2:8080/healthz"

    @patch("checks.deployment_check.requests.request")
    def test_blocks_cross_host_redirect(self, mock_request):
        redirect_resp = self._mock_response(301, is_redirect=True, location="http://evil.com/steal")
        mock_request.return_value = redirect_resp

        check = self._make_check()
        result = check._safe_request("GET", "http://172.20.0.2:8080/health")

        assert result is None
        assert mock_request.call_count == 1

    @patch("checks.deployment_check.requests.request")
    def test_enforces_redirect_depth_limit(self, mock_request):
        # Every response is a same-host redirect, exceeding the 5-hop limit
        redirect_resp = self._mock_response(301, is_redirect=True, location="http://localhost/loop")
        mock_request.return_value = redirect_resp

        check = self._make_check()
        result = check._safe_request("GET", "http://localhost/start")

        assert result is None
        # 1 original + 5 redirects = 6, then depth check returns None
        assert mock_request.call_count == 6

    @patch("checks.deployment_check.requests.request")
    def test_non_redirect_returned_directly(self, mock_request):
        resp = self._mock_response(200, body=b'{"status": "ok"}')
        mock_request.return_value = resp

        check = self._make_check()
        result = check._safe_request("GET", "http://172.20.0.2:8080/health")

        assert result is not None
        assert result.status_code == 200
        assert mock_request.call_count == 1

    @patch("checks.deployment_check.requests.request")
    def test_connection_error_returns_none(self, mock_request):
        import requests as req_lib

        mock_request.side_effect = req_lib.exceptions.ConnectionError("refused")

        check = self._make_check()
        result = check._safe_request("GET", "http://172.20.0.2:8080/health")

        assert result is None
