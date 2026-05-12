"""Orchestrator MCP contract integration tests (#2639).

The orchestrator runs an MCP sidecar (``orchestrator/mcp_server.py``,
streamable-HTTP at ``/mcp`` on port 9850) that exposes the
``submit_task`` / ``run_agent_task`` / ``babysit_pr`` pipeline-control
verbs to external Claude Code sessions.  ``test_sandbox_mcp_tools_e2e``
covers the sandbox-side MCP wire-up; the orchestrator-side MCP contract
had no end-to-end coverage before this file.

The tests below drive the live MCP server over its streamable-HTTP
transport (matching what Claude Code does in production) and assert
the contract surface that the unit tests in
``orchestrator/tests/test_mcp_tools.py`` mock around:

* Tool discovery — the three target tools are advertised.
* Argument validation — invalid inputs short-circuit before any HTTP
  call to ``/api/v1/pipelines`` and surface the structured ``error``
  field the schema documents.
* Route-level validation — ``run_agent_task`` reviewer-only roster /
  cross-phase role rejections from the orchestrator route survive the
  MCP boundary with their ``reason`` codes intact.
* ``validate_config`` — pure validation tool, side-effect free, exercises
  the FastMCP↔handler glue against the live ``PipelineConfig`` model.
* ``get_status`` — unknown task_id returns a structured error rather
  than a transport-level failure.

Coverage explicitly *not* attempted here (tracked separately):

* Full ``submit_task`` round-trip to ``PR_READY`` / ``run_agent_task``
  single-phase / ``babysit_pr`` against a real PR — all three need
  pod-level LLM-response injection (per #2474) before they can be
  driven deterministically from CI.  Tracked: #2668.
* Pydantic-vs-handler error envelope mismatch — FastMCP schema-layer
  rejections surface as raw "Error executing tool ..." text rather
  than the documented ``{"error": "..."}`` JSON envelope.  The tests
  here only assert the JSON envelope.  Tracked: #2665.
* Rate limiting — the in-process ``RateLimiter`` is shared across
  all MCP tools.  Driving 30+ rapid calls from CI would pollute the
  sliding window for downstream tests; the threaded-burst exactness
  invariant (#2669, now lock-guarded) is instead verified by
  ``orchestrator/tests/test_mcp_server.py::TestRateLimiter``.
* Production reachability of the MCP server via a k8s ``Service``
  (it currently runs on the orchestrator pod's hostPort, local-dev
  overlay only).  Tracked: #2667.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import secrets
from typing import Any

import pytest

pytestmark = pytest.mark.integration


# Pipelines created by idempotency tests target the canned test repo
# from ``integration_tests/conftest.py::_write_test_config``
# (``test-owner/test-repo``).  Each test mints a fresh qualifier so the
# resulting ``pipeline_id`` (``issue-<N>-<qualifier>``) is unique per
# run — important because the orchestrator's state-store rejects
# duplicate IDs and we don't want this test to fail because a previous
# run left state behind.  The orchestrator's start step may fail when
# the test repo isn't reachable (no real GH in CI), but the
# state-store row is still created, so the subsequent duplicate-create
# call still hits the 409 path we want to assert.
_TEST_REPO = "test-owner/test-repo"


# ---------------------------------------------------------------------------
# MCP client helpers
# ---------------------------------------------------------------------------


def _call_tool(url: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a single MCP tool over streamable HTTP and return the parsed
    handler result.

    ``orchestrator/mcp_server.py`` wraps each handler in
    ``json.dumps(result, indent=2)`` before returning it as the tool's
    text content (`FastMCP` with ``json_response=True``).  Tests parse
    that JSON back into a dict.
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


def _list_tool_names(url: str) -> list[str]:
    async def _run() -> list[str]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
        return [t.name for t in listed.tools]

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tool discovery — MCP server advertises the three target verbs
# ---------------------------------------------------------------------------


class TestMCPDiscovery:
    """Verifies the three target tools are advertised over MCP."""

    def test_target_tools_advertised(self, orchestrator_mcp_url: str) -> None:
        names = _list_tool_names(orchestrator_mcp_url)
        for tool in ("submit_task", "run_agent_task", "babysit_pr"):
            assert tool in names, (
                f"{tool!r} not advertised by orchestrator MCP server. Advertised: {sorted(names)}"
            )

    def test_supporting_tools_advertised(self, orchestrator_mcp_url: str) -> None:
        # ``get_status`` and ``validate_config`` round out the contract
        # that the three target verbs depend on (polling + dry-run
        # config validation).  Asserted separately so a regression in
        # the supporting surface stays distinguishable from a regression
        # in the headline tools.
        names = _list_tool_names(orchestrator_mcp_url)
        for tool in ("get_status", "validate_config"):
            assert tool in names, f"{tool!r} not advertised. Advertised: {sorted(names)}"


# ---------------------------------------------------------------------------
# submit_task — argument-validation contract
# ---------------------------------------------------------------------------


class TestSubmitTaskValidation:
    """submit_task short-circuits invalid args before any HTTP call.

    These cases all return the handler's structured ``{"error": "..."}``
    payload without touching the orchestrator's state store, so they
    leave no pipelines behind to clean up.
    """

    def test_invalid_qualifier_rejected(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(
            orchestrator_mcp_url,
            "submit_task",
            {
                "description": "test",
                "repo": "owner/repo",
                "issue_number": 999_999_001,
                "qualifier": "Bad Qualifier!",  # uppercase + space + bang
            },
        )
        assert "error" in result, result
        assert "qualifier" in result["error"].lower()

    def test_invalid_jira_ticket_rejected(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(
            orchestrator_mcp_url,
            "submit_task",
            {
                "description": "test",
                "repo": "owner/repo",
                "jira_ticket": "not-a-ticket",
            },
        )
        assert "error" in result, result
        assert "jira" in result["error"].lower() or "ticket" in result["error"].lower()


# ---------------------------------------------------------------------------
# run_agent_task — argument validation + route-level rejection survives MCP
# ---------------------------------------------------------------------------


class TestRunAgentTaskValidation:
    """run_agent_task handler validates before hitting the route, and
    surfaces route-level ``details.reason`` codes for the validation
    cases the route owns.
    """

    def test_invalid_phase_rejected(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(
            orchestrator_mcp_url,
            "run_agent_task",
            {
                "phase": "deploy",  # not in {refine,plan,implement}
                "repo": "owner/repo",
                "description": "test",
            },
        )
        assert "error" in result, result
        assert "phase" in result["error"].lower()

    def test_invalid_repo_format_rejected(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(
            orchestrator_mcp_url,
            "run_agent_task",
            {
                "phase": "plan",
                "repo": "not-a-valid-repo-shape",
                "description": "test",
            },
        )
        assert "error" in result, result
        assert "repo" in result["error"].lower()

    def test_missing_description_rejected(self, orchestrator_mcp_url: str) -> None:
        # The FastMCP layer fills in ``None`` for omitted optional args,
        # but ``description`` is required by the schema; check the
        # explicit-empty case at the handler level so a schema-side
        # bypass would still be caught.
        result = _call_tool(
            orchestrator_mcp_url,
            "run_agent_task",
            {"phase": "plan", "repo": "owner/repo", "description": ""},
        )
        assert "error" in result, result
        assert "description" in result["error"].lower()

    def test_reviewer_only_roster_surfaces_reason(self, orchestrator_mcp_url: str) -> None:
        # Route returns 400 with details.reason='reviewer_only_roster'.
        # The MCP handler must propagate the reason code so callers can
        # branch on it (documented in the tool description).  Uses a
        # randomized pipeline_id qualifier so two parallel CI runs don't
        # race on the same pipeline_id when this test creates state.
        # (It does not — the route validates roles before the state-
        # store write, see orchestrator/routes/pipelines.py:1905-1918.)
        result = _call_tool(
            orchestrator_mcp_url,
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo",
                "description": "reviewer-only roster test",
                "roles": ["reviewer_code"],
                "qualifier": f"mcp-contract-{secrets.token_hex(4)}",
            },
        )
        assert "error" in result, result
        assert result.get("reason") == "reviewer_only_roster", result

    def test_cross_phase_role_surfaces_reason(self, orchestrator_mcp_url: str) -> None:
        # Cross-phase roles (overseer / autofixer / conflict_resolver /
        # inspector) are rejected by the route with reason='cross_phase_role'.
        # Same no-state-write guarantee as reviewer-only roster.
        result = _call_tool(
            orchestrator_mcp_url,
            "run_agent_task",
            {
                "phase": "implement",
                "repo": "owner/repo",
                "description": "cross-phase role test",
                "roles": ["coder", "overseer"],
                "qualifier": f"mcp-contract-{secrets.token_hex(4)}",
            },
        )
        assert "error" in result, result
        assert result.get("reason") == "cross_phase_role", result


# ---------------------------------------------------------------------------
# babysit_pr — handler-level argument validation
# ---------------------------------------------------------------------------


class TestBabysitPRValidation:
    """babysit_pr rejects missing/malformed PR identifiers before any
    GitHub or orchestrator call.

    The full PR-state validation path (fork, merged, empty diff) is
    owned by the route handler and requires real ``gh pr view`` access;
    those scenarios are exercised by the in-process Flask tests under
    ``integration_tests/test_babysit_pr/`` and the unit tests in
    ``orchestrator/tests/test_mcp_tools.py::TestBabysitPr``.  We only
    cover the MCP-side argument gates here.
    """

    def test_missing_pr_number_rejected(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(orchestrator_mcp_url, "babysit_pr", {"repo": "owner/repo"})
        assert "error" in result, result
        assert "pr_number" in result["error"]

    def test_negative_pr_number_rejected(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(
            orchestrator_mcp_url,
            "babysit_pr",
            {"pr_number": -1, "repo": "owner/repo"},
        )
        assert "error" in result, result
        assert "positive integer" in result["error"]

    def test_invalid_repo_format_rejected(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(
            orchestrator_mcp_url,
            "babysit_pr",
            {"pr_number": 1, "repo": "not-owner-slash-repo"},
        )
        assert "error" in result, result
        assert "owner/name" in result["error"]


# ---------------------------------------------------------------------------
# validate_config — pure validation MCP tool
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """The validate_config tool runs ``PipelineConfig`` through the
    Pydantic model without creating a pipeline.  Useful as a smoke
    test of the FastMCP↔handler glue: it requires no state, no auth,
    and no proxied HTTP call.
    """

    def test_valid_config(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(
            orchestrator_mcp_url,
            "validate_config",
            {"config": {"hitl_gates": False}},
        )
        assert result.get("valid") is True, result
        assert "config" in result

    def test_invalid_config_returns_errors(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(
            orchestrator_mcp_url,
            "validate_config",
            {"config": {"start_phase": "bogus_phase"}},
        )
        assert result.get("valid") is False, result
        assert result.get("errors"), result


# ---------------------------------------------------------------------------
# get_status — unknown task_id surfaces as a structured error
# ---------------------------------------------------------------------------


class TestGetStatusUnknownTask:
    """``get_status`` for a non-existent pipeline returns the handler's
    generic ``{"error": "..."}`` envelope (the orchestrator route 404s,
    the handler's broad ``except Exception`` wraps it).  Callers rely
    on the envelope shape — a regression that surfaced the HTTPError
    as a transport-level failure would break polling clients.
    """

    def test_unknown_task_id_returns_error_envelope(self, orchestrator_mcp_url: str) -> None:
        result = _call_tool(
            orchestrator_mcp_url,
            "get_status",
            {"task_id": "definitely-not-a-real-pipeline-id-9876543210"},
        )
        assert "error" in result, result


# ---------------------------------------------------------------------------
# submit_task idempotency — duplicate create returns existing-pipeline shape
# ---------------------------------------------------------------------------


def _unique_submit_args(qualifier_prefix: str) -> dict[str, Any]:
    """Build a submit_task arg bundle with a unique pipeline_id.

    Issue numbers come from the >999_000_000 range so they cannot
    collide with any real GitHub issue.  The qualifier suffix
    randomizes the pipeline_id (``issue-<N>-<qualifier>``) per call
    so concurrent CI shards don't race each other.
    """
    issue_number = 999_000_000 + secrets.randbelow(900_000)
    qualifier = f"{qualifier_prefix}-{secrets.token_hex(4)}"
    return {
        "description": "MCP contract idempotency test",
        "repo": _TEST_REPO,
        "issue_number": issue_number,
        "qualifier": qualifier,
    }


def _is_duplicate_response(result: dict[str, Any]) -> bool:
    """Return True iff the result is a 409-style duplicate-pipeline error.

    The handler at ``mcp_tools._handle_submit_task`` returns
    ``{"error": "...", "existing_pipeline_id": "...", ...}`` for
    409 responses; we key off ``existing_pipeline_id`` because the
    orchestrator route doesn't set ``reason`` for the bare-duplicate
    case (only the enrichment fields are populated).
    """
    return "error" in result and "existing_pipeline_id" in result


class TestSubmitTaskIdempotency:
    """Duplicate-create idempotency — both the sequential and the
    racing cases.  These tests do create state in the orchestrator's
    state store (one pipeline per ``qualifier``); the qualifier is
    randomized so leftover rows don't break subsequent runs.
    """

    def test_duplicate_create_returns_existing_pipeline_metadata(
        self, orchestrator_mcp_url: str
    ) -> None:
        args = _unique_submit_args("mcp-idem-seq")
        first = _call_tool(orchestrator_mcp_url, "submit_task", args)
        # The first call may report ``started`` (orchestrator started
        # the pipeline cleanly) or ``created_not_started`` (start
        # failed because the test repo isn't actually clone-able in
        # CI).  Either way the pipeline row exists in state-store and
        # the duplicate call must hit the 409 path.
        assert first.get("task_id"), first
        first_id = first["task_id"]

        second = _call_tool(orchestrator_mcp_url, "submit_task", args)
        assert _is_duplicate_response(second), second
        assert second.get("existing_pipeline_id") == first_id, second

    def test_concurrent_duplicate_create_serializes(self, orchestrator_mcp_url: str) -> None:
        """Two MCP clients submit the same ``issue_number`` + ``qualifier``
        simultaneously.  The state-store's create-or-fail semantics
        must serialize them: at most one row exists at the end, and
        the losing caller must see the 409 envelope (not a transport
        error, not a duplicate-row write).
        """
        args = _unique_submit_args("mcp-idem-race")

        def _submit() -> dict[str, Any]:
            return _call_tool(orchestrator_mcp_url, "submit_task", args)

        # 2 workers is enough to exercise the race — the orchestrator
        # serializes at the state-store layer, not in the MCP server,
        # so adding more workers just adds CI cost without adding
        # signal.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_submit) for _ in range(2)]
            results = [f.result(timeout=60) for f in futures]

        # Partition by outcome.  A success is "got a task_id and no
        # existing_pipeline_id"; a duplicate-loser is the 409 envelope.
        successes = [r for r in results if r.get("task_id") and not _is_duplicate_response(r)]
        duplicates = [r for r in results if _is_duplicate_response(r)]
        # Exactly one creator + exactly one loser is the contract.
        # (We don't assert "len == 1" each because a third outcome
        # — both succeed-then-409 — could indicate a state-store
        # serialization bug, and we want the failure message to show
        # the actual partition.)
        assert len(successes) == 1, f"successes={successes!r}, duplicates={duplicates!r}"
        assert len(duplicates) == 1, f"successes={successes!r}, duplicates={duplicates!r}"
        # The duplicate-loser's existing_pipeline_id must match the
        # winner's task_id.
        assert duplicates[0]["existing_pipeline_id"] == successes[0]["task_id"]


# ---------------------------------------------------------------------------
# Auth boundary — MCP server itself is unauthenticated by design
# ---------------------------------------------------------------------------


class TestAuthBoundary:
    """The MCP server explicitly documents "no authentication required —
    localhost-only access is enforced via Docker port mapping"
    (``mcp_server.py:104-106``).  These tests pin that contract so a
    regression that, say, started rejecting unauthenticated calls would
    fail loudly before reaching Claude Code clients in production.

    The downstream call from the MCP server to the orchestrator API
    *does* require ``EGG_LIFECYCLE_SECRET``; the server reads it from
    its own pod env.  That path is covered by the existing 401/503
    regression suite in ``test_k8s_deployment_tools.py``.
    """

    def test_health_endpoint_requires_no_auth(self, orchestrator_mcp_url: str) -> None:
        import urllib.request

        health_url = orchestrator_mcp_url.rsplit("/mcp", 1)[0] + "/health"
        with urllib.request.urlopen(health_url, timeout=10) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode())
        assert body.get("status") == "healthy", body
        assert body.get("service") == "egg-mcp-server", body

    def test_tool_call_requires_no_auth(self, orchestrator_mcp_url: str) -> None:
        # The streamable-HTTP client used by every other test in this
        # file sends no Authorization header.  A successful tool call
        # therefore demonstrates the no-auth contract; we use a
        # side-effect-free validation path so the assertion isn't
        # coupled to any pipeline state.
        result = _call_tool(
            orchestrator_mcp_url,
            "validate_config",
            {"config": {"hitl_gates": True}},
        )
        assert result.get("valid") is True, result
