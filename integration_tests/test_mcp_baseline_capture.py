"""MCP-surface latency baseline capture (#2908 TASK-5-7).

Captures per-event wall-clock samples for a real-LLM 5-role BRC
consensus run driven through the **still-live MCP surface** (slice-5
is additive only — the MCP tools are still registered). The output
file ``.egg-state/agent-outputs/latency-mcp-baseline.json`` is the
baseline that slice-6's TASK-6-6 consumes to show the MCP→CLI
collapse has not materially regressed agent-process latency.

This test is **kubectl-gated**: it skips with a clear message when
``_kubectl_available()`` returns False (the test stack is k3s-backed
and capturing on a stub would defeat the purpose). See
``docs/guides/testing.md`` for the k3s-on-host setup.

JSON schema written to ``latency-mcp-baseline.json``::

    {
      "_meta": {
        "schema_version": "1",
        "captured_at": "ISO-8601 UTC",
        "issue": 2908,
        "slice": "slice-5",
        "pipeline_id": "<the pipeline this came from>",
        "synthetic": false,
        "egg_git_sha": "<commit at capture time>"
      },
      "samples": [
        {
          "role": "coder",
          "event_type": "agent.completed",
          "start_ts": "ISO-8601 UTC",
          "end_ts":   "ISO-8601 UTC",
          "duration_seconds": float,
          "exit_code": int | null     // null when not surfaced by /status
        },
        ...
      ],
      "aggregate": {
        "n": int,
        "p50_seconds": float,
        "p95_seconds": float,
        "max_seconds": float,
        "sum_seconds": float
      }
    }

slice-6's TASK-6-6 reads only the ``samples`` list and the
``aggregate`` block, so additive ``_meta`` fields are forward-safe.

No ``ScriptedProvider`` import / reference: per the slice-5 plan
re-scope, this baseline runs against the real LLM route configured
for the test stack (egg-litellm) — there is no in-process provider
swap.

Why a side-effect-producing integration test rather than a script
under ``scripts/``: keeping the capture inside the pytest harness
means it runs under the same kubectl-gated session-scoped fixture
``egg_stack`` (``integration_tests/conftest.py:340``) that the rest
of the integration suite uses — the same skip rules, the same
namespace-cleanup teardown, and (when slice-6's comparison test
arrives) the same fixture so the two timings are taken under
identical cluster conditions.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Output location & schema constants
# ---------------------------------------------------------------------------

# The committed baseline file lives at this path. slice-6 TASK-6-6
# reads from the same path.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_OUTPUT = _REPO_ROOT / ".egg-state" / "agent-outputs" / "latency-mcp-baseline.json"
_BASELINE_SCHEMA_VERSION = "1"

# Hard wall-clock cap on the polling loop. Real-LLM 5-role consensus
# normally lands in 3–8 min; capping at 25 min protects CI from a
# wedged pipeline turning into an open-ended hang. When the cap is
# hit, the test writes whatever samples it captured and fails with a
# clear "did not reach terminal status" error.
_PIPELINE_POLL_TIMEOUT_SEC = 25 * 60
_PIPELINE_POLL_INTERVAL_SEC = 5

# Terminal pipeline statuses that end the poll loop.
_PIPELINE_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "PR_READY", "FAILED", "CANCELLED"}
)


# ---------------------------------------------------------------------------
# MCP client helper (mirrors test_orchestrator_mcp_contract.py)
# ---------------------------------------------------------------------------


def _call_tool(url: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a single MCP tool over streamable HTTP, return the parsed
    handler result.

    Mirrors the helper in ``test_orchestrator_mcp_contract.py`` —
    kept inline (rather than imported) so the baseline-capture test
    has no test-to-test coupling.
    """

    async def _run() -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
        if not result.content:
            return {}
        first = result.content[0]
        text = getattr(first, "text", None)
        if text is None:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}
        if not isinstance(parsed, dict):
            return {"_raw": parsed}
        return parsed

    return asyncio.run(_run())


def _get_pipeline_status(orchestrator_url: str, pipeline_id: str) -> dict[str, Any]:
    """Fetch the orchestrator's ``/api/v1/pipelines/<id>/status`` payload.

    Used to walk the ``concurrent.agents`` block which surfaces the
    per-agent ``started_at`` / ``elapsed_seconds`` timing the baseline
    captures (orchestrator/routes/pipelines.py:_get_concurrent_status).
    """
    url = f"{orchestrator_url.rstrip('/')}/api/v1/pipelines/{pipeline_id}/status"
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 — http to test cluster
        body = resp.read().decode()
    return json.loads(body)


def _egg_git_sha() -> str:
    """Best-effort git SHA for the capturing checkout. Empty string on failure."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip()
    except FileNotFoundError, subprocess.TimeoutExpired:
        return ""


# ---------------------------------------------------------------------------
# Sample extraction
# ---------------------------------------------------------------------------


def _agents_to_samples(
    agents_block: list[dict[str, Any]],
    fallback_now: datetime,
) -> list[dict[str, Any]]:
    """Convert the status payload's ``concurrent.agents`` block into
    baseline ``samples``.

    The orchestrator surfaces per-agent ``started_at`` (ISO-8601) and
    ``elapsed_seconds`` (server-computed). When ``elapsed_seconds`` is
    present we compute ``end_ts`` as ``started_at + elapsed_seconds`` —
    that pins the sample to the orchestrator's clock rather than the
    test client's, which matters when the test stack and capture host
    are not synchronised.
    """
    samples: list[dict[str, Any]] = []
    for agent in agents_block:
        if not isinstance(agent, dict):
            continue
        role = agent.get("role")
        if not role:
            continue
        started_at = agent.get("started_at")
        elapsed = agent.get("elapsed_seconds")
        if not started_at:
            # An agent that never reported a start time is not a useful
            # latency sample; skip rather than emit a synthetic one.
            continue
        try:
            start_dt = datetime.fromisoformat(started_at)
        except TypeError, ValueError:
            continue
        if isinstance(elapsed, (int, float)) and elapsed >= 0:
            duration = float(elapsed)
        else:
            duration = max(0.0, (fallback_now - start_dt).total_seconds())
        end_dt = datetime.fromtimestamp(start_dt.timestamp() + duration, tz=UTC)
        samples.append(
            {
                "role": role,
                "event_type": "agent.completed",
                "start_ts": start_dt.isoformat(),
                "end_ts": end_dt.isoformat(),
                "duration_seconds": duration,
                "exit_code": agent.get("exit_code"),
            }
        )
    return samples


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute p50/p95/max/sum across sample durations."""
    durations = [float(s.get("duration_seconds", 0.0)) for s in samples]
    if not durations:
        return {
            "n": 0,
            "p50_seconds": 0.0,
            "p95_seconds": 0.0,
            "max_seconds": 0.0,
            "sum_seconds": 0.0,
        }
    durations_sorted = sorted(durations)
    # statistics.quantiles needs n>=2 for q=20 (p95 across 20 buckets).
    if len(durations_sorted) >= 2:
        # ``n=20`` gives 19 cut-points; index 9 ≈ p50, index 18 ≈ p95.
        quants = statistics.quantiles(durations_sorted, n=20, method="inclusive")
        p50 = quants[9]
        p95 = quants[18]
    else:
        # Single sample: every quantile collapses to that value.
        p50 = p95 = durations_sorted[0]
    return {
        "n": len(durations_sorted),
        "p50_seconds": float(p50),
        "p95_seconds": float(p95),
        "max_seconds": float(max(durations_sorted)),
        "sum_seconds": float(sum(durations_sorted)),
    }


def _write_baseline(
    *,
    pipeline_id: str,
    samples: list[dict[str, Any]],
) -> None:
    """Emit the baseline JSON to the committed path.

    Always creates the parent directory; the file is one of the few
    paths under ``.egg-state/agent-outputs/`` that committers actually
    push (it's an analysis artefact, not transient agent log).
    """
    _BASELINE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "schema_version": _BASELINE_SCHEMA_VERSION,
            "captured_at": datetime.now(UTC).isoformat(),
            "issue": 2908,
            "slice": "slice-5",
            "pipeline_id": pipeline_id,
            "synthetic": False,
            "egg_git_sha": _egg_git_sha(),
        },
        "samples": samples,
        "aggregate": _aggregate(samples),
    }
    _BASELINE_OUTPUT.write_text(json.dumps(payload, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Fixture: gateway healthy or skip
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _healthy_gateway_or_skip(egg_stack) -> None:  # noqa: ANN001
    """Skip when the gateway is unhealthy.

    Mirrors the same guard ``test_orchestrator_mcp_contract.py`` uses:
    the orchestrator's ``/api/v1/pipelines`` route blocks on gateway
    readiness, and dummy-credentialed CI gateways report
    ``status=degraded`` indefinitely. Skipping keeps the suite green
    without sacrificing local-LLM coverage.
    """
    health_url = f"{egg_stack.gateway_url}/api/v1/health"
    try:
        with urllib.request.urlopen(health_url, timeout=10) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
        pytest.skip(f"Gateway /api/v1/health unreachable: {exc}")
    if payload.get("status") != "healthy":
        pytest.skip(
            f"Gateway is not healthy (status={payload.get('status')!r}); "
            "baseline capture needs a real LLM-backed run."
        )


# ---------------------------------------------------------------------------
# The capture test
# ---------------------------------------------------------------------------


class TestMCPBaselineCapture:
    """Capture wall-clock latency for one 5-role consensus run.

    The captured JSON is committed back to
    ``.egg-state/agent-outputs/latency-mcp-baseline.json`` and consumed
    by slice-6 TASK-6-6's MCP→CLI comparison test.
    """

    def test_capture_one_run(
        self,
        orchestrator_mcp_url: str,
        orchestrator_url: str,
        _healthy_gateway_or_skip: None,  # noqa: PT019 — fixture used for side effect
    ) -> None:
        # 1) Kick a tiny pipeline via the still-live MCP submit_task tool.
        #    The qualifier mints a unique pipeline_id per run so the
        #    state-store does not 409 if a prior baseline left a row.
        qualifier = f"mcp-baseline-{os.getpid()}-{int(time.time())}"
        result = _call_tool(
            orchestrator_mcp_url,
            "submit_task",
            {
                "description": "MCP-surface latency baseline (TASK-5-7)",
                "repo": "test-owner/test-repo",
                "issue_number": 999_999_002,
                "qualifier": qualifier,
            },
        )
        if "error" in result:
            pytest.skip(
                "submit_task rejected baseline pipeline "
                f"(needs real GH credentials on the test cluster): {result['error']}"
            )

        pipeline_id = result.get("pipeline_id") or result.get("data", {}).get("pipeline_id")
        if not pipeline_id:
            pytest.fail(f"submit_task returned no pipeline_id; got {result!r}")

        # 2) Poll status until terminal or timeout. Each poll captures
        #    the current concurrent.agents block; we keep the LAST
        #    snapshot's agents (they accumulate timing).
        last_agents: list[dict[str, Any]] = []
        deadline = time.monotonic() + _PIPELINE_POLL_TIMEOUT_SEC
        terminal = False
        last_status = "<no-status>"
        while time.monotonic() < deadline:
            try:
                status = _get_pipeline_status(orchestrator_url, pipeline_id)
            except urllib.error.URLError, TimeoutError, ConnectionError:
                # Transient — keep polling.
                time.sleep(_PIPELINE_POLL_INTERVAL_SEC)
                continue
            data = status.get("data", {}) if isinstance(status, dict) else {}
            last_status = data.get("status", "<unknown>")
            concurrent = data.get("concurrent") or {}
            agents = concurrent.get("agents") or []
            if isinstance(agents, list):
                last_agents = agents
            if last_status in _PIPELINE_TERMINAL_STATUSES:
                terminal = True
                break
            time.sleep(_PIPELINE_POLL_INTERVAL_SEC)

        # 3) Transform the captured agents block into samples and write
        #    the baseline file regardless of terminal outcome — a
        #    partial baseline is still useful to slice-6 (and the
        #    failure assertion below names the situation explicitly).
        samples = _agents_to_samples(last_agents, datetime.now(UTC))
        _write_baseline(pipeline_id=pipeline_id, samples=samples)

        # 4) Assert the run hit a terminal status AND produced at least
        #    one sample. Both conditions matter for slice-6's
        #    comparison — an empty samples list means the comparison
        #    has nothing to compare.
        assert terminal, (
            f"pipeline {pipeline_id} did not reach a terminal status within "
            f"{_PIPELINE_POLL_TIMEOUT_SEC // 60} min (last status={last_status!r}); "
            f"baseline at {_BASELINE_OUTPUT} captured partial data"
        )
        assert samples, (
            f"pipeline {pipeline_id} reached terminal status {last_status!r} but "
            f"the /status endpoint reported no agent timing — baseline is empty"
        )
