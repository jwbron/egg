"""Shared helper functions for local pipeline integration tests.

These utilities are used across test files to interact with the orchestrator
API for creating, managing, and monitoring pipelines.
"""

import time

import requests


def create_pipeline(
    orchestrator_url: str,
    *,
    mode: str = "local",
    prompt: str = "Test pipeline",
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    config: dict | None = None,
) -> tuple[dict, int]:
    """Create a pipeline via the orchestrator API."""
    body: dict = {"mode": mode, "prompt": prompt}
    if issue_number is not None:
        body["issue_number"] = issue_number
    if repo is not None:
        body["repo"] = repo
    if branch is not None:
        body["branch"] = branch
    if config is not None:
        body["config"] = config
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines",
        json=body,
        timeout=10,
    )
    return resp.json(), resp.status_code


def get_pipeline(orchestrator_url: str, pipeline_id: str) -> tuple[dict, int]:
    """GET a pipeline by ID."""
    resp = requests.get(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
        timeout=10,
    )
    return resp.json(), resp.status_code


def delete_pipeline(orchestrator_url: str, pipeline_id: str) -> tuple[dict, int]:
    """DELETE a pipeline by ID."""
    resp = requests.delete(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}",
        timeout=10,
    )
    return resp.json(), resp.status_code


def start_pipeline(orchestrator_url: str, pipeline_id: str) -> tuple[dict, int]:
    """POST to start a pipeline."""
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/start",
        timeout=10,
    )
    return resp.json(), resp.status_code


def wait_for_pipeline_terminal(
    orchestrator_url: str,
    pipeline_id: str,
    timeout: int = 120,
    poll_interval: float = 2.0,
) -> dict:
    """Poll GET /api/v1/pipelines/<id>/status until terminal state.

    Returns the final status response data, or raises TimeoutError.
    """
    terminal_statuses = {"complete", "failed", "cancelled"}
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("data", {}).get("status", "")
                if status in terminal_statuses:
                    return data
        except requests.ConnectionError:
            pass
        time.sleep(poll_interval)

    raise TimeoutError(f"Pipeline {pipeline_id} did not reach terminal state within {timeout}s")


def wait_for_awaiting_human(
    orchestrator_url: str,
    pipeline_id: str,
    timeout: int = 120,
    poll_interval: float = 2.0,
) -> dict:
    """Poll GET /status until status == 'awaiting_human' or terminal."""
    terminal_statuses = {"complete", "failed", "cancelled"}
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("data", {}).get("status", "")
                if status == "awaiting_human":
                    return data
                if status in terminal_statuses:
                    raise AssertionError(
                        f"Pipeline reached terminal state '{status}' before awaiting_human"
                    )
        except requests.ConnectionError:
            pass
        time.sleep(poll_interval)

    raise TimeoutError(f"Pipeline {pipeline_id} did not reach awaiting_human within {timeout}s")


def resolve_decision(
    orchestrator_url: str,
    pipeline_id: str,
    decision_id: str,
    resolution: str = "approve",
    custom_input: str | None = None,
) -> tuple[dict, int]:
    """POST to resolve a pending decision."""
    body: dict = {"resolution": resolution}
    if custom_input is not None:
        body["custom_input"] = custom_input
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/resolve",
        json=body,
        timeout=10,
    )
    return resp.json(), resp.status_code


def send_signal(
    orchestrator_url: str,
    pipeline_id: str,
    signal_type: str,
    **kwargs,
) -> tuple[dict | None, int]:
    """Send a signal to a pipeline via the orchestrator API."""
    body = {"type": signal_type, **kwargs}
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/signals",
        json=body,
        timeout=10,
    )
    try:
        return resp.json(), resp.status_code
    except requests.JSONDecodeError:
        return None, resp.status_code


def check_signals_api_exists(orchestrator_url: str, pipeline_id: str) -> bool:
    """Check if the signals API endpoint exists."""
    resp = requests.post(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/signals",
        json={"type": "ping"},
        timeout=10,
    )
    # If we get 404 on the endpoint itself, signals API doesn't exist
    # If we get 400/422 (bad request), the endpoint exists but rejected our payload
    return resp.status_code != 404
