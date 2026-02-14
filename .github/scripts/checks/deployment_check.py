"""
Deployment validation check runner.

Coordinates with the orchestrator-managed devserver to validate agent
changes against locally running services. The orchestrator manages the
Docker infrastructure (network, containers); this check runner makes
HTTP requests to the running services and reports results.

Unique among check runners: the orchestrator manages infrastructure
while the sandbox runs validation. The check runner:
1. Signals orchestrator to start the devserver
2. Polls status until healthy or timeout
3. Runs health checks against each service endpoint
4. Runs validation tests from DeploymentConfig
5. Signals teardown
6. Returns CheckResult
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

# Add shared directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_contracts import CheckResult, CheckStatus, Contract
from egg_contracts.deployment import DeploymentConfig, load_deployment_config

from .base import CheckRunner

# Defensive parsing constants
MAX_RESPONSE_SIZE = 1024 * 1024  # 1MB max response body
REQUEST_TIMEOUT = 10  # seconds per individual request
POLL_INTERVAL = 5  # seconds between status polls
MAX_POLL_ATTEMPTS = 120  # max polling attempts (120 * 5s = 10 min)


class DeploymentCheck(CheckRunner):
    """Check runner for deployment validation.

    Coordinates with the orchestrator's devserver management API to
    start a target application's devserver stack, run HTTP validation
    checks, and tear down the stack.
    """

    @property
    def check_id(self) -> str:
        return "check-deployment"

    def __init__(self, contract: Contract, repo_root: Path) -> None:
        super().__init__(contract, repo_root)
        self._orchestrator_url = self._get_orchestrator_url()

    def _get_orchestrator_url(self) -> str:
        """Get the orchestrator API base URL.

        Uses the ORCHESTRATOR_URL environment variable if set,
        otherwise defaults to the standard orchestrator address.
        """
        return os.environ.get(
            "ORCHESTRATOR_URL",
            "http://egg-orchestrator:9849",
        )

    def _get_pipeline_id(self) -> str:
        """Get the pipeline ID from the contract.

        Returns:
            Pipeline ID string.
        """
        if self.contract.pipeline_id:
            return self.contract.pipeline_id
        if self.contract.issue:
            return f"issue-{self.contract.issue.number}"
        return "unknown"

    def _safe_request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response | None:
        """Make an HTTP request with defensive handling.

        Applies timeout, max response size, and blocks external redirects.

        Args:
            method: HTTP method (GET, POST, etc.).
            url: Request URL.
            **kwargs: Additional requests kwargs.

        Returns:
            Response object, or None on error.
        """
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        kwargs["stream"] = True  # Stream to enforce size limits
        kwargs["allow_redirects"] = False  # Handle redirects manually

        try:
            resp = requests.request(method, url, **kwargs)

            # Check for redirects to external hosts (attacker-controlled)
            if resp.is_redirect:
                redirect_url = resp.headers.get("Location", "")
                original_host = urlparse(url).hostname
                redirect_host = urlparse(redirect_url).hostname
                if redirect_host and redirect_host != original_host:
                    return None  # Block external redirect

            # Enforce max response size
            content = b""
            for chunk in resp.iter_content(chunk_size=8192):
                content += chunk
                if len(content) > MAX_RESPONSE_SIZE:
                    # Truncate and return what we have
                    resp._content = content[:MAX_RESPONSE_SIZE]
                    return resp

            resp._content = content
            return resp

        except requests.exceptions.Timeout:
            return None
        except requests.exceptions.ConnectionError:
            return None
        except Exception:
            return None

    def _safe_json(self, response: requests.Response) -> dict[str, Any] | None:
        """Safely parse JSON from a response.

        Handles malformed JSON, oversized bodies, and other parse errors.

        Args:
            response: HTTP response to parse.

        Returns:
            Parsed JSON dict, or None on error.
        """
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return None

    def _start_devserver(self, pipeline_id: str) -> dict[str, Any] | None:
        """Signal the orchestrator to start the devserver.

        Args:
            pipeline_id: Pipeline identifier.

        Returns:
            Response data dict with status, or None on failure.
        """
        url = f"{self._orchestrator_url}/api/v1/pipelines/{pipeline_id}/deployment-check/start"
        resp = self._safe_request("POST", url)
        if resp is None or resp.status_code >= 500:
            return None
        return self._safe_json(resp)

    def _poll_status(self, pipeline_id: str) -> dict[str, Any] | None:
        """Poll the devserver status from the orchestrator.

        Args:
            pipeline_id: Pipeline identifier.

        Returns:
            Status data dict, or None on failure.
        """
        url = f"{self._orchestrator_url}/api/v1/pipelines/{pipeline_id}/deployment-check/status"
        resp = self._safe_request("GET", url)
        if resp is None:
            return None
        return self._safe_json(resp)

    def _teardown_devserver(self, pipeline_id: str) -> None:
        """Signal the orchestrator to tear down the devserver.

        Args:
            pipeline_id: Pipeline identifier.
        """
        url = f"{self._orchestrator_url}/api/v1/pipelines/{pipeline_id}/deployment-check/teardown"
        self._safe_request("POST", url)

    def _wait_for_healthy(self, pipeline_id: str) -> dict[str, Any] | None:
        """Wait for the devserver to become healthy.

        Polls the orchestrator's status endpoint until the devserver
        reports healthy or we hit the polling limit.

        Args:
            pipeline_id: Pipeline identifier.

        Returns:
            Final status data, or None on timeout.
        """
        for _ in range(MAX_POLL_ATTEMPTS):
            data = self._poll_status(pipeline_id)
            if data is None:
                time.sleep(POLL_INTERVAL)
                continue

            status_info = data.get("status", {})
            status = status_info.get("status", "")

            if status == "healthy":
                return data
            elif status in ("error", "stopped"):
                return data
            elif status == "unhealthy":
                return data

            time.sleep(POLL_INTERVAL)

        return None

    def _run_health_checks(
        self,
        deployment_config: DeploymentConfig,
        service_endpoints: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Run health checks against devserver services.

        Args:
            deployment_config: Configuration with health endpoint paths.
            service_endpoints: Service status info from orchestrator.

        Returns:
            List of health check results.
        """
        results: list[dict[str, Any]] = []

        for service_name, health_path in deployment_config.health_endpoints.items():
            svc_info = service_endpoints.get(service_name, {})
            ip = svc_info.get("ip", "")
            port = svc_info.get("port", 0)

            if not ip:
                # Try using service name as hostname (Docker DNS)
                ip = service_name

            url = f"http://{ip}:{port}{health_path}" if port else f"http://{ip}{health_path}"
            resp = self._safe_request("GET", url)

            result = {
                "service": service_name,
                "health_path": health_path,
                "url": url,
                "passed": False,
            }

            if resp is not None and resp.status_code == 200:
                result["passed"] = True
                result["status_code"] = resp.status_code
            elif resp is not None:
                result["status_code"] = resp.status_code
                result["error"] = f"Unexpected status {resp.status_code}"
            else:
                result["error"] = "Request failed or timed out"

            results.append(result)

        return results

    def _run_validation_tests(
        self,
        deployment_config: DeploymentConfig,
        service_endpoints: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Run validation tests against devserver services.

        Args:
            deployment_config: Configuration with validation tests.
            service_endpoints: Service status info from orchestrator.

        Returns:
            List of validation test results.
        """
        results: list[dict[str, Any]] = []

        for test in deployment_config.validation_tests:
            svc_info = service_endpoints.get(test.service, {})
            ip = svc_info.get("ip", "")
            port = svc_info.get("port", 0)

            if not ip:
                ip = test.service

            url = f"http://{ip}:{port}{test.path}" if port else f"http://{ip}{test.path}"
            resp = self._safe_request(test.method, url)

            result: dict[str, Any] = {
                "service": test.service,
                "method": test.method,
                "path": test.path,
                "description": test.description,
                "passed": False,
            }

            if resp is None:
                result["error"] = "Request failed or timed out"
            elif resp.status_code != test.expected_status:
                result["error"] = (
                    f"Expected status {test.expected_status}, got {resp.status_code}"
                )
                result["status_code"] = resp.status_code
            elif test.expected_body_contains:
                body = resp.text or ""
                if test.expected_body_contains not in body:
                    result["error"] = (
                        f"Response body does not contain '{test.expected_body_contains}'"
                    )
                    result["status_code"] = resp.status_code
                else:
                    result["passed"] = True
                    result["status_code"] = resp.status_code
            else:
                result["passed"] = True
                result["status_code"] = resp.status_code

            results.append(result)

        return results

    def run(self) -> CheckResult:
        """Execute the deployment validation check.

        Returns:
            CheckResult with PASS/FAIL/SKIP status.
        """
        # Check if target repo has opted in
        deployment_config = load_deployment_config(self.repo_root)
        if deployment_config is None:
            return self.create_result(
                CheckStatus.SKIP,
                message="No deployment config found (.egg/deployment.yml) — target app not opted in",
            )

        pipeline_id = self._get_pipeline_id()

        try:
            # Step 1: Start devserver via orchestrator
            start_data = self._start_devserver(pipeline_id)
            if start_data is None:
                return self.create_result(
                    CheckStatus.FAIL,
                    message="Failed to communicate with orchestrator to start devserver",
                    details={"pipeline_id": pipeline_id},
                )

            if not start_data.get("success", False):
                msg = start_data.get("message", "Unknown error starting devserver")
                return self.create_result(
                    CheckStatus.FAIL,
                    message=f"Orchestrator refused to start devserver: {msg}",
                    details={"pipeline_id": pipeline_id, "response": start_data},
                )

            # Step 2: Wait for healthy
            status_data = self._wait_for_healthy(pipeline_id)
            if status_data is None:
                return self.create_result(
                    CheckStatus.FAIL,
                    message="Timed out waiting for devserver to become healthy",
                    details={"pipeline_id": pipeline_id},
                )

            status_info = status_data.get("status", {})
            devserver_status = status_info.get("status", "unknown")

            if devserver_status == "error":
                return self.create_result(
                    CheckStatus.FAIL,
                    message=f"Devserver errored: {status_info.get('error_message', 'unknown')}",
                    details={"pipeline_id": pipeline_id, "status": status_info},
                )

            service_endpoints = status_info.get("services", {})

            # Step 3: Run health checks
            health_results = self._run_health_checks(deployment_config, service_endpoints)
            health_failures = [r for r in health_results if not r["passed"]]

            # Step 4: Run validation tests
            validation_results = self._run_validation_tests(deployment_config, service_endpoints)
            validation_failures = [r for r in validation_results if not r["passed"]]

            # Compile results
            all_passed = not health_failures and not validation_failures
            details: dict[str, Any] = {
                "pipeline_id": pipeline_id,
                "devserver_status": devserver_status,
                "health_checks": health_results,
                "validation_tests": validation_results,
                "health_passed": len(health_results) - len(health_failures),
                "health_total": len(health_results),
                "validation_passed": len(validation_results) - len(validation_failures),
                "validation_total": len(validation_results),
            }

            if all_passed:
                return self.create_result(
                    CheckStatus.PASS,
                    message=(
                        f"Deployment validation passed: "
                        f"{len(health_results)} health checks, "
                        f"{len(validation_results)} validation tests"
                    ),
                    details=details,
                )
            else:
                failures: list[str] = []
                for f in health_failures:
                    failures.append(f"Health check {f['service']}: {f.get('error', 'failed')}")
                for f in validation_failures:
                    failures.append(
                        f"Validation {f['service']} {f['method']} {f['path']}: "
                        f"{f.get('error', 'failed')}"
                    )

                return self.create_result(
                    CheckStatus.FAIL,
                    message=f"Deployment validation failed: {'; '.join(failures[:5])}",
                    details=details,
                )

        finally:
            # Always tear down
            self._teardown_devserver(pipeline_id)
