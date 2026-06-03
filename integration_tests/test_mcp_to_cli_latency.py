"""MCP→CLI per-event latency comparison (#2908 TASK-6-6).

Drives a 5-role consensus on the **post-deletion CLI-only surface**
(the in-process MCP tools were retired in slice-6 — see
``shared/egg_agent/client.py`` and ``sandbox/egg_agent_tools/`` for the
deletion landing point) and asserts the per-event wall-clock latency
has not regressed > 5% versus the slice-5 baseline captured by
``integration_tests/test_mcp_baseline_capture.py``.

Baseline source
---------------

The slice-5 baseline lives at
``.egg-state/agent-outputs/latency-mcp-baseline.json`` and is committed
to the repo by TASK-5-7.  This test consumes the ``aggregate.p95``
field and re-runs the same 5-role consensus shape on the CLI surface.
On a real-LLM run on the ``egg_stack`` cluster, the per-agent timing
comes back through the orchestrator's
``/api/v1/pipelines/<id>/status`` ``concurrent.agents`` block (same
extraction helper the baseline-capture test uses); on the post-
deletion code the agents reach for ``egg-orch consensus
ack/nack --reason-file`` etc. through the shell CLI, not the MCP
tool surface.

Baselines from earlier slices may be marked ``_meta.synthetic =
true`` (slice-5 committed a placeholder so slice-6 could author the
comparison plumbing before a real capture lands).  When that flag is
set, this test treats the comparison as informational — the comparison
JSON is written, and the assertion is skipped rather than failing —
because comparing a real measurement against a placeholder would
produce a false signal.

Output
------

The post-deletion measurement is written to
``.egg-state/agent-outputs/latency-mcp-vs-cli.json`` so the operator
can inspect both numbers side by side.  Schema is the same as the
slice-5 baseline plus a ``comparison`` block with the ratio and the
regression verdict.

On regression > 5%
------------------

The test surfaces an OVERSEER_ALERT priority ``medium`` with the
measured delta (rendered into stderr as a structured JSON envelope —
the orchestrator's overseer-alert ingest is unavailable from a
collection-time pytest run, so the alert is logged for the operator
to forward manually if it fires in CI).  The fallback decision
(per architect od-5) is whether to ship the persistent ``egg-orch``
daemon — that's a follow-up, not a slice-6 blocker.

Trust-boundary scope
--------------------

- No ``ScriptedProvider`` import / reference — that class does not
  exist in this codebase (verified at
  ``integration_tests/regression/conftest.py:45``); real-LLM samples
  come back through the cluster.
- No vendored MCP source tarball — the baseline is the committed
  JSON file, so slice-6 does not have to pin the old MCP code to
  re-run it.
- ``egg_stack``-gated — skips with a clear message when
  ``_kubectl_available()`` returns False.  See
  ``docs/guides/testing.md`` for the k3s-on-host setup.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_INPUT = _REPO_ROOT / ".egg-state" / "agent-outputs" / "latency-mcp-baseline.json"
_COMPARISON_OUTPUT = _REPO_ROOT / ".egg-state" / "agent-outputs" / "latency-mcp-vs-cli.json"
_COMPARISON_SCHEMA_VERSION = "1"
_REGRESSION_BUDGET = 0.05  # 5%

_PIPELINE_POLL_TIMEOUT_SEC = 25 * 60
_PIPELINE_POLL_INTERVAL_SEC = 5
_PIPELINE_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "PR_READY", "FAILED", "CANCELLED"}
)


# ---------------------------------------------------------------------------
# Baseline reader
# ---------------------------------------------------------------------------


def _read_baseline() -> dict[str, Any]:
    """Load the slice-5 baseline JSON.

    Returns the parsed payload.  Raises if missing — the baseline is a
    hard prerequisite for the comparison and must exist at slice-6
    entry per TASK-6-6 acceptance.
    """
    if not _BASELINE_INPUT.exists():
        raise FileNotFoundError(
            f"Slice-5 baseline {_BASELINE_INPUT} is missing — slice-6 "
            f"TASK-6-6 cannot run a comparison without it.  Run "
            f"`pytest integration_tests/test_mcp_baseline_capture.py` "
            f"first on a kubectl-backed host."
        )
    return json.loads(_BASELINE_INPUT.read_text())


# ---------------------------------------------------------------------------
# CLI tool helper — mirrors the MCP one in the baseline capture
# ---------------------------------------------------------------------------


def _call_tool(url: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke an operator-side orchestrator MCP tool over streamable
    HTTP.  This is the *operator-facing* MCP surface
    (``orchestrator/mcp_server.py``) which is out of scope for the
    slice-6 deletion — it is what we use to kick the pipeline.  The
    agent-facing MCP surface (the one slice-6 retired) is NOT what
    this helper exercises."""

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
    url = f"{orchestrator_url.rstrip('/')}/api/v1/pipelines/{pipeline_id}/status"
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 - test cluster
        body = resp.read().decode()
    return json.loads(body)


def _egg_git_sha() -> str:
    """Best-effort git SHA for the capturing checkout.  Empty on failure."""
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
# Sample extraction — same shape as the baseline-capture helper
# ---------------------------------------------------------------------------


def _agents_to_samples(
    agents_block: list[dict[str, Any]],
    fallback_now: datetime,
) -> list[dict[str, Any]]:
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
    if len(durations_sorted) >= 2:
        quants = statistics.quantiles(durations_sorted, n=20, method="inclusive")
        p50 = quants[9]
        p95 = quants[18]
    else:
        p50 = p95 = durations_sorted[0]
    return {
        "n": len(durations_sorted),
        "p50_seconds": float(p50),
        "p95_seconds": float(p95),
        "max_seconds": float(max(durations_sorted)),
        "sum_seconds": float(sum(durations_sorted)),
    }


# ---------------------------------------------------------------------------
# Comparison + alert
# ---------------------------------------------------------------------------


def _compute_comparison(
    baseline: dict[str, Any],
    measured: dict[str, Any],
) -> dict[str, Any]:
    """Compute the p95 ratio and regression verdict.

    Ratio is ``measured.p95 / baseline.p95``.  A ratio of 1.0 means no
    change; 1.05 is exactly at the budget; > 1.05 is a regression that
    fails the test.
    """
    baseline_p95 = float(baseline.get("p95_seconds") or 0.0)
    measured_p95 = float(measured.get("p95_seconds") or 0.0)
    if baseline_p95 <= 0.0:
        ratio = None
        regression = False
    else:
        ratio = measured_p95 / baseline_p95
        regression = ratio > (1.0 + _REGRESSION_BUDGET)
    return {
        "baseline_p95_seconds": baseline_p95,
        "measured_p95_seconds": measured_p95,
        "ratio": ratio,
        "regression_budget": _REGRESSION_BUDGET,
        "regression_detected": regression,
    }


def _emit_overseer_alert(comparison: dict[str, Any]) -> None:
    """Render the OVERSEER_ALERT envelope to stderr.

    The orchestrator's overseer-alert ingest is not reachable from a
    pytest collection environment — the alert is emitted as a
    structured JSON line so CI logs surface it for the operator to
    forward manually.  This matches the slice-6 acceptance:
    "the test surfaces a structured OVERSEER_ALERT priority medium
    with the measured delta".
    """
    envelope = {
        "anomaly": "slice-6-mcp-cli-latency-regression",
        "priority": "medium",
        "summary": (
            "MCP→CLI per-event latency regressed beyond the 5% budget. "
            "Architect od-5 fallback: consider shipping the persistent "
            "egg-orch daemon."
        ),
        "detail": {
            "baseline_p95_seconds": comparison["baseline_p95_seconds"],
            "measured_p95_seconds": comparison["measured_p95_seconds"],
            "ratio": comparison["ratio"],
            "regression_budget": comparison["regression_budget"],
        },
        "recommend": (
            "Review .egg-state/agent-outputs/latency-mcp-vs-cli.json and "
            "decide whether to land the egg-orch daemon (od-5) or accept "
            "the regression."
        ),
    }
    sys.stderr.write("OVERSEER_ALERT " + json.dumps(envelope) + "\n")


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------


def _write_comparison(
    *,
    pipeline_id: str,
    samples: list[dict[str, Any]],
    baseline: dict[str, Any],
    comparison: dict[str, Any],
) -> None:
    """Emit the post-deletion measurement + comparison JSON."""
    _COMPARISON_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "schema_version": _COMPARISON_SCHEMA_VERSION,
            "captured_at": datetime.now(UTC).isoformat(),
            "issue": 2908,
            "slice": "slice-6",
            "pipeline_id": pipeline_id,
            "egg_git_sha": _egg_git_sha(),
            "baseline_source": str(_BASELINE_INPUT.relative_to(_REPO_ROOT)),
            "baseline_meta": baseline.get("_meta", {}),
        },
        "samples": samples,
        "aggregate": _aggregate(samples),
        "comparison": comparison,
    }
    _COMPARISON_OUTPUT.write_text(json.dumps(payload, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Fixture: gateway healthy or skip
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _healthy_gateway_or_skip(egg_stack) -> None:  # noqa: ANN001
    """Skip when the gateway is unhealthy.

    Mirrors the same guard the baseline-capture test uses
    (``test_mcp_baseline_capture.py``).  The orchestrator's
    ``/api/v1/pipelines`` route blocks on gateway readiness, and
    dummy-credentialed CI gateways report ``status=degraded``
    indefinitely.  Skipping keeps the suite green without sacrificing
    local-LLM coverage.
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
            "MCP→CLI latency comparison needs a real LLM-backed run."
        )


# ---------------------------------------------------------------------------
# The comparison test
# ---------------------------------------------------------------------------


class TestMCPToCLILatency:
    """Drive a 5-role consensus on the post-deletion CLI surface and
    assert per-event latency has not regressed > 5% vs the slice-5
    baseline.
    """

    def test_baseline_file_exists(self) -> None:
        """The slice-5 baseline is a hard prerequisite per TASK-6-6
        acceptance.  This guard fires loudly if the file is missing
        rather than burying the failure inside the more expensive
        cluster-run test below.
        """
        if not _BASELINE_INPUT.exists():
            pytest.fail(
                f"Slice-5 baseline file {_BASELINE_INPUT} is missing; "
                "TASK-5-7 must capture it before TASK-6-6 can compare."
            )

    def test_capture_post_deletion_run_and_compare(
        self,
        orchestrator_mcp_url: str,
        orchestrator_url: str,
        _healthy_gateway_or_skip: None,  # noqa: PT019 - fixture used for side effect
    ) -> None:
        baseline = _read_baseline()

        # 1) Kick a pipeline via the operator-facing MCP surface (the
        #    orchestrator sidecar, NOT the deleted agent-facing one).
        qualifier = f"mcp-vs-cli-{os.getpid()}-{int(time.time())}"
        result = _call_tool(
            orchestrator_mcp_url,
            "submit_task",
            {
                "description": "MCP-vs-CLI latency comparison (TASK-6-6)",
                "repo": "test-owner/test-repo",
                "issue_number": 999_999_006,
                "qualifier": qualifier,
            },
        )
        if "error" in result:
            pytest.skip(
                "submit_task rejected comparison pipeline "
                f"(needs real GH credentials on the test cluster): {result['error']}"
            )

        pipeline_id = result.get("pipeline_id") or result.get("data", {}).get("pipeline_id")
        if not pipeline_id:
            pytest.fail(f"submit_task returned no pipeline_id; got {result!r}")

        # 2) Poll status; capture the last concurrent.agents snapshot
        #    (same shape as the baseline-capture test).
        last_agents: list[dict[str, Any]] = []
        deadline = time.monotonic() + _PIPELINE_POLL_TIMEOUT_SEC
        terminal = False
        last_status = "<no-status>"
        while time.monotonic() < deadline:
            try:
                status = _get_pipeline_status(orchestrator_url, pipeline_id)
            except urllib.error.URLError, TimeoutError, ConnectionError:
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

        # 3) Build the measurement + comparison.  Always emit the
        #    comparison JSON, even on partial / synthetic baseline data
        #    — the operator inspects the file to decide.
        samples = _agents_to_samples(last_agents, datetime.now(UTC))
        measured_aggregate = _aggregate(samples)
        comparison = _compute_comparison(
            baseline=baseline.get("aggregate", {}),
            measured=measured_aggregate,
        )
        _write_comparison(
            pipeline_id=pipeline_id,
            samples=samples,
            baseline=baseline,
            comparison=comparison,
        )

        # 4) If the baseline is synthetic (slice-5 placeholder), treat
        #    this run as informational — comparing real timings against
        #    a placeholder is meaningless.  Surface a clear skip so the
        #    operator knows to re-run after a real baseline lands.
        baseline_meta = baseline.get("_meta", {}) or {}
        if baseline_meta.get("synthetic"):
            pytest.skip(
                "Baseline at "
                f"{_BASELINE_INPUT.relative_to(_REPO_ROOT)} is marked "
                "_meta.synthetic=true (slice-5 placeholder); comparison "
                "written to "
                f"{_COMPARISON_OUTPUT.relative_to(_REPO_ROOT)} for "
                "operator review.  Re-run after a real-LLM baseline "
                "lands per TASK-5-7."
            )

        # 5) Required preconditions for a meaningful assertion.
        assert terminal, (
            f"pipeline {pipeline_id} did not reach a terminal status within "
            f"{_PIPELINE_POLL_TIMEOUT_SEC // 60} min (last status={last_status!r}); "
            f"comparison at {_COMPARISON_OUTPUT} captured partial data"
        )
        assert samples, (
            f"pipeline {pipeline_id} reached terminal status {last_status!r} "
            f"but the /status endpoint reported no agent timing — "
            f"comparison is empty"
        )

        # 6) Regression budget.  On failure, surface the structured
        #    OVERSEER_ALERT before asserting so CI logs carry the
        #    delta even when the test fails.
        if comparison["regression_detected"]:
            _emit_overseer_alert(comparison)

        assert not comparison["regression_detected"], (
            f"MCP→CLI p95 latency regressed beyond {_REGRESSION_BUDGET:.0%} budget: "
            f"baseline_p95={comparison['baseline_p95_seconds']:.2f}s, "
            f"measured_p95={comparison['measured_p95_seconds']:.2f}s, "
            f"ratio={comparison['ratio']:.3f}.  See "
            f"{_COMPARISON_OUTPUT} and the OVERSEER_ALERT envelope on "
            f"stderr for the architect od-5 fallback decision."
        )


# ---------------------------------------------------------------------------
# Unit-level coverage for the comparison helper (no cluster required)
# ---------------------------------------------------------------------------


class TestCompareComparisonHelper:
    """The cluster-driven test above only fires under kubectl, which
    means the comparison helper itself goes unverified in PR CI.  Add a
    small unit-level coverage block here so the regression-budget
    arithmetic is covered every run."""

    def test_no_regression_when_under_budget(self) -> None:
        result = _compute_comparison(
            baseline={"p95_seconds": 100.0},
            measured={"p95_seconds": 104.99},
        )
        assert result["ratio"] is not None
        assert not result["regression_detected"]

    def test_regression_when_over_budget(self) -> None:
        result = _compute_comparison(
            baseline={"p95_seconds": 100.0},
            measured={"p95_seconds": 110.0},
        )
        assert result["ratio"] is not None
        assert result["regression_detected"]

    def test_no_regression_at_exact_budget(self) -> None:
        # 1.05 is the boundary — equal-to-budget is NOT a regression.
        result = _compute_comparison(
            baseline={"p95_seconds": 100.0},
            measured={"p95_seconds": 105.0},
        )
        assert result["ratio"] is not None
        assert not result["regression_detected"]

    def test_zero_baseline_short_circuits(self) -> None:
        # A degenerate baseline produces ratio=None and regression=False
        # so the test does not falsely fail on an empty baseline.
        result = _compute_comparison(
            baseline={"p95_seconds": 0.0},
            measured={"p95_seconds": 5.0},
        )
        assert result["ratio"] is None
        assert not result["regression_detected"]

    def test_overseer_alert_envelope_shape(self, capsys) -> None:
        comparison = {
            "baseline_p95_seconds": 100.0,
            "measured_p95_seconds": 200.0,
            "ratio": 2.0,
            "regression_budget": _REGRESSION_BUDGET,
            "regression_detected": True,
        }
        _emit_overseer_alert(comparison)
        captured = capsys.readouterr()
        assert "OVERSEER_ALERT " in captured.err
        body = captured.err.split("OVERSEER_ALERT ", 1)[1].strip()
        envelope = json.loads(body)
        assert envelope["priority"] == "medium"
        assert envelope["anomaly"] == "slice-6-mcp-cli-latency-regression"
        assert envelope["detail"]["ratio"] == 2.0
