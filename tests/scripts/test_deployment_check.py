"""
Unit tests for DeploymentCheck check runner.

Tests PASS/FAIL/SKIP scenarios, defensive parsing (oversized response,
malformed JSON, timeout), and orchestrator communication errors.
All orchestrator HTTP API calls are mocked.
"""

import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / ".github" / "scripts"))

from egg_contracts import CheckStatus, Contract, IssueInfo
from egg_contracts.deployment import DeploymentConfig


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

        with patch.object(check, "_start_devserver", return_value={"success": False, "message": "No config"}):
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
        original_teardown = check._teardown_devserver

        def tracking_teardown(pid):
            nonlocal teardown_called
            teardown_called = True

        with patch.object(check, "_teardown_devserver", side_effect=tracking_teardown):
            with patch.object(check, "_start_devserver", return_value=None):
                result = check.run()

        assert teardown_called
        assert result.status == CheckStatus.FAIL
