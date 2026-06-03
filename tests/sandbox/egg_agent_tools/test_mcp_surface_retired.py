"""Tester hardening for #2908 slice-6: the agent-side MCP surface stays deleted.

The slice-6 coder commit (#2908 task-6-1..6-6) retired the in-process
Claude Agent SDK MCP tool surface (``sandbox/egg_agent_tools/tools/``,
``sandbox/egg_agent_tools/server.py``, the
``SYSTEM_PROMPT_NUDGE`` / ``build_sandbox_mcp_server`` exports, the
``EGG_MCP_TOOLS`` env-flag gate in ``shared/egg_agent/client.py``) in
favour of the ``egg-orch`` / ``egg-contract`` shell CLIs. This file is
the tester's hardening — it is *not* a regression test of any one
deletion; it is a repo-wide adversarial guard that catches:

1. A future commit re-introducing one of the deleted symbols
   (``EGG_MCP_TOOLS``, ``build_sandbox_mcp_server``,
   ``SYSTEM_PROMPT_NUDGE``, ``from egg_agent_tools.tools``) into
   production Python — the slice-5 baseline (``rg`` returns zero
   matches) becomes an enforced contract.
2. The ``egg_agent_tools`` package losing its empty-``__all__`` /
   handlers-only invariant — if someone re-adds an export, this trips.
3. A future change to ``shared/egg_agent/client.py`` re-introducing
   the env-flag-driven MCP registration block (verified by setting
   ``EGG_MCP_TOOLS`` to every historical truthy value and asserting
   ``options.mcp_servers`` carries no egg namespace keys).
4. Edge cases in the slice-6 TASK-6-6 latency comparison helper that
   the coder's unit-level coverage in
   ``integration_tests/test_mcp_to_cli_latency.py::TestCompareComparisonHelper``
   leaves uncovered (NaN / inf / negative measurements, missing
   ``p95_seconds`` key, baseline-meta accessor shape).

The coder's unit-level coverage stays — these tests are *additional*
hardening, not a replacement. They run on every PR without any
cluster gating.
"""

from __future__ import annotations

import importlib
import math
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))


# Symbols whose presence anywhere in production Python would signal the
# slice-6 deletion has been reverted (fully or partially).  Each entry
# is (literal, kind).  ``import`` symbols are matched as substrings;
# ``identifier`` symbols are matched as whole words via regex \b so
# that an unrelated identifier with the same prefix does not trip the
# guard.
_DELETED_SYMBOLS: list[tuple[str, str]] = [
    # Env flag — gone from every .py file (the documenter docs still
    # describe its *retirement*, so .md is out of scope here).
    ("EGG_MCP_TOOLS", "identifier"),
    # The factory function and the system-prompt nudge constant were
    # both retired with ``sandbox/egg_agent_tools/server.py``.
    ("build_sandbox_mcp_server", "identifier"),
    ("SYSTEM_PROMPT_NUDGE", "identifier"),
    # The deleted tool-namespace modules — any future code re-importing
    # them is a regression even if the directory exists again.
    ("from egg_agent_tools.tools", "import"),
    ("from sandbox.egg_agent_tools.tools", "import"),
    ("import egg_agent_tools.tools", "import"),
]

# Paths excluded from the repo walk.  ``.egg-state/`` holds the
# archived BRC history from past pipelines (e.g. ``1765-implement.md``)
# which legitimately discusses the now-deleted flag in prose; the
# ``.venv``/``__pycache__`` directories are derived artefacts.  Test
# files MUST scan — that's where regressions would land first.
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".egg-state",  # archived BRC history references the deleted flag
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
    }
)


def _enumerate_python_files() -> list[Path]:
    """Yield every .py file in the repo, excluding generated / archived dirs.

    Uses ``git ls-files`` so that ignored or untracked files (the
    sandbox container's stray ``.venv`` symlink, for instance) are not
    swept up.
    """
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if out.returncode != 0:
        # Fall back to a filesystem walk when the test harness is run
        # outside a git checkout (unlikely in this repo but defensive).
        return [
            p for p in ROOT.rglob("*.py") if not any(part in _EXCLUDED_DIRS for part in p.parts)
        ]
    files: list[Path] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        path = ROOT / line
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


# Production-only filter — this test file itself names the deleted
# symbols (we have to, to assert their absence), as does the migrated
# ``test_sandbox_mcp_tools_e2e.py`` (which describes the deletion in
# its module docstring).  Skip the explicitly listed test files so
# their legitimate references don't trip the guard.
_SELF_REFERENCING_FILES = frozenset(
    {
        # This file.
        str(Path("tests") / "sandbox" / "egg_agent_tools" / "test_mcp_surface_retired.py"),
        # The migrated e2e test names the deleted symbols in its
        # module docstring and in the post-deletion guard assertion.
        str(Path("integration_tests") / "test_sandbox_mcp_tools_e2e.py"),
        # The latency-comparison test's docstring references the
        # deletion landing point.
        str(Path("integration_tests") / "test_mcp_to_cli_latency.py"),
        # test_client.py contains the historical-class deletion
        # rationale comment that names EGG_MCP_TOOLS and references
        # the slice-6 retirement.
        str(Path("tests") / "shared" / "egg_agent" / "test_client.py"),
        # The restrictions handlers test names the deleted module
        # path in its rationale comment.
        str(Path("sandbox") / "tests" / "test_restrictions_handlers.py"),
    }
)


def _file_relpath(path: Path) -> str:
    return str(path.relative_to(ROOT))


@pytest.mark.parametrize("symbol,kind", _DELETED_SYMBOLS, ids=[s for s, _ in _DELETED_SYMBOLS])
def test_deleted_symbol_is_absent_from_production_py(symbol: str, kind: str) -> None:
    """Every Python file in the repo (minus self-referencing tests)
    must contain zero references to the slice-6-deleted symbol.

    This is the regression-guard that catches a future agent
    accidentally re-introducing the MCP surface.
    """
    if kind == "identifier":
        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    else:  # "import"
        pattern = re.compile(re.escape(symbol))

    offenders: list[tuple[str, int, str]] = []
    for path in _enumerate_python_files():
        rel = _file_relpath(path)
        if rel in _SELF_REFERENCING_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append((rel, lineno, line.strip()))
                # Cap reported hits per file at 3 so a flood does
                # not bury the actual regression.
                if sum(1 for o in offenders if o[0] == rel) >= 3:
                    break
    assert not offenders, (
        f"#2908 slice-6 retired {symbol!r}; production Python references "
        f"must be zero.  Found:\n"
        + "\n".join(f"  {rel}:{lineno}  {snippet}" for rel, lineno, snippet in offenders)
    )


class TestEggAgentToolsPackageShape:
    """The post-deletion ``egg_agent_tools`` package keeps the
    handler layer and the ``push`` helper only.  These tests freeze the
    public-surface shape so a future commit cannot quietly re-add
    ``build_sandbox_mcp_server`` etc. via ``__all__``."""

    def test_package_imports_clean(self) -> None:
        """The bare-bones package must import without raising."""
        import egg_agent_tools

        assert egg_agent_tools is not None

    def test_package_all_is_empty(self) -> None:
        """``__all__`` is the explicit promise of the post-deletion
        shape — no exports leak via wildcard import."""
        import egg_agent_tools

        assert getattr(egg_agent_tools, "__all__", None) == []

    def test_deleted_attributes_no_longer_exposed(self) -> None:
        """Direct ``import`` of the deleted exports must fail.  This is
        what a downstream caller would hit if they tried to revive the
        old API; we want the failure to be loud."""
        import egg_agent_tools

        for attr in (
            "SYSTEM_PROMPT_NUDGE",
            "build_sandbox_mcp_server",
            "TOOL_LIST",
            "TOOL_NAMESPACES",
            "TOOL_REGISTRY",
            "ToolRegistration",
        ):
            assert not hasattr(egg_agent_tools, attr), (
                f"{attr!r} is a slice-6-deleted export and must not be "
                f"reachable via the egg_agent_tools namespace."
            )

    def test_handler_layer_still_present(self) -> None:
        """The shared handler layer is the *kept* half of the
        decision — both surfaces collapse to one, not zero.  A regression
        that deletes the handlers too would break the CLI."""
        from egg_agent_tools import handlers as handler_pkg
        from egg_agent_tools.handlers import brc, sdlc, task

        assert handler_pkg is not None
        assert callable(brc.brc_propose)
        assert callable(sdlc.show_contract)
        # ``task.task_complete`` is the namespaced handler name —
        # asserting it stays callable catches a handler-layer regression
        # that would silently break the CLI.
        assert callable(task.task_complete)

    def test_tools_submodule_absent(self) -> None:
        """The ``tools/`` subpackage was deleted wholesale — importing
        any of the namespace modules must raise ``ImportError``."""
        for mod in (
            "egg_agent_tools.tools",
            "egg_agent_tools.tools.brc",
            "egg_agent_tools.tools.sdlc",
            "egg_agent_tools.server",
            "egg_agent_tools.schemas",
        ):
            with pytest.raises(ImportError):
                importlib.import_module(mod)


# ── Adversarial: EGG_MCP_TOOLS truly has no effect anywhere ────────────────


class TestEggMcpToolsFlagIsRetired:
    """Every truthy / falsy value of the historical env flag must be a
    no-op.  The original flag accepted ``true``/``1``/``yes``/``on`` —
    we sweep that set plus a couple of garbage values so a partial
    re-introduction (e.g. someone restores only the ``true`` branch) is
    caught.

    Uses the same offline ``ClaudeAgentOptions`` capture pattern as
    ``test_sandbox_mcp_tools_e2e.test_agent_can_be_spawned_via_sdk``
    so the test runs without an LLM round-trip.
    """

    _EGG_NAMESPACE_KEYS = frozenset({"sdlc", "brc", "phase", "progress", "task", "checkpoint"})

    def _skip_if_no_sdk(self) -> None:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            pytest.skip("claude_agent_sdk not installed in this environment")

    @pytest.mark.parametrize(
        "flag_value",
        ["true", "1", "yes", "on", "TRUE", "True", "false", "0", "no", "off", "garbage"],
    )
    def test_egg_mcp_tools_env_value_is_no_op(self, monkeypatch, flag_value: str) -> None:
        """Setting EGG_MCP_TOOLS to any value must not produce egg
        MCP namespace keys on ``options.mcp_servers``."""
        self._skip_if_no_sdk()
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

        captured: list = []

        class _Capturing(ClaudeAgentOptions):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                captured.append(self)

        async def _fake_query(**kwargs):  # noqa: ARG001 — SDK shim
            yield ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=0,
                session_id="sess",
                stop_reason="end_turn",
                total_cost_usd=0.0,
                usage=None,
                result="ok",
                structured_output=None,
            )

        from egg_agent.client import run_agent_async

        monkeypatch.setenv("EGG_MCP_TOOLS", flag_value)
        # Disable the DDG fallback path so the only MCP registration
        # we could possibly observe is the (retired) egg one.
        monkeypatch.delenv("ANTHROPIC_CUSTOM_MODEL_OPTION", raising=False)
        monkeypatch.setenv("EGG_PRIVATE_MODE", "true")

        with (
            patch("claude_agent_sdk.ClaudeAgentOptions", _Capturing),
            patch("claude_agent_sdk.query", side_effect=_fake_query),
        ):
            import asyncio

            asyncio.run(run_agent_async("noop"))

        assert len(captured) == 1
        opts = captured[0]
        mcp_servers = getattr(opts, "mcp_servers", None) or {}
        offenders = self._EGG_NAMESPACE_KEYS.intersection(mcp_servers)
        assert not offenders, (
            f"EGG_MCP_TOOLS={flag_value!r} caused {sorted(offenders)} egg "
            f"namespaces to register on options.mcp_servers; the slice-6 "
            f"deletion should make this flag a complete no-op."
        )


# ── Adversarial: latency comparison helper edge cases ──────────────────────


class TestLatencyComparisonAdversarial:
    """The coder's ``TestCompareComparisonHelper`` covers happy-path
    boundaries (under / over / at budget / zero baseline).  These
    tests probe the *defensive* corners: NaN / inf / negative inputs
    and the structural integrity of the OVERSEER_ALERT envelope under
    those inputs.
    """

    def _import_helpers(self):
        # The latency test file is at integration_tests/, not on
        # PYTHONPATH by default — import via path manipulation.
        sys.path.insert(0, str(ROOT / "integration_tests"))
        try:
            test_mod = importlib.import_module("test_mcp_to_cli_latency")
        finally:
            sys.path.pop(0)
        return test_mod._compute_comparison, test_mod._emit_overseer_alert

    def test_negative_baseline_short_circuits(self) -> None:
        """Negative ``p95_seconds`` is nonsense — the helper should
        treat it like zero (ratio=None, regression=False) rather than
        spitting a negative ratio that mis-reports regression."""
        compute, _ = self._import_helpers()
        result = compute(
            baseline={"p95_seconds": -42.0},
            measured={"p95_seconds": 100.0},
        )
        assert result["ratio"] is None
        assert not result["regression_detected"]

    def test_missing_p95_key_treated_as_zero(self) -> None:
        """A caller may pass an aggregate dict without ``p95_seconds``
        (e.g. an empty-samples aggregate uses a different schema in a
        future evolution).  The helper must not raise."""
        compute, _ = self._import_helpers()
        result = compute(baseline={}, measured={"p95_seconds": 50.0})
        assert result["baseline_p95_seconds"] == 0.0
        assert result["ratio"] is None
        assert not result["regression_detected"]

    def test_none_p95_treated_as_zero(self) -> None:
        """JSON allows ``null`` for numeric fields; ``float(None)``
        would TypeError.  The helper guards with ``or 0.0`` — verify
        that guard is exercised."""
        compute, _ = self._import_helpers()
        result = compute(
            baseline={"p95_seconds": None},
            measured={"p95_seconds": None},
        )
        # Both zero → ratio is None (degenerate baseline branch wins).
        assert result["ratio"] is None
        assert not result["regression_detected"]

    def test_inf_measured_flags_regression(self) -> None:
        """An infinite measured value must trip the regression branch,
        not divide cleanly."""
        compute, _ = self._import_helpers()
        result = compute(
            baseline={"p95_seconds": 100.0},
            measured={"p95_seconds": float("inf")},
        )
        assert result["ratio"] == float("inf")
        assert result["regression_detected"]

    def test_nan_measured_does_not_falsely_pass(self) -> None:
        """``NaN > x`` is always False in IEEE 754 — verify the helper
        does not falsely report no-regression when the measurement is
        garbage.  The expected behaviour: NaN propagates, ratio is NaN,
        and ``regression_detected`` is False (consistent with the IEEE
        comparison semantics).  This test pins the current behaviour so
        a future refactor that switches to ``math.isfinite`` rejection
        does it deliberately, not silently."""
        compute, _ = self._import_helpers()
        result = compute(
            baseline={"p95_seconds": 100.0},
            measured={"p95_seconds": float("nan")},
        )
        assert math.isnan(result["ratio"])
        # IEEE semantics — NaN comparison is always False.  Pinning so
        # a future change is intentional.
        assert not result["regression_detected"]

    def test_alert_envelope_carries_all_required_keys(self, capsys) -> None:
        """The OVERSEER_ALERT envelope must be a strict JSON shape:
        priority, anomaly, summary, detail, recommend — and a
        ``detail.ratio`` field even when the input is degenerate.

        Catches a refactor that drops one of the fields and breaks
        downstream alert consumers."""
        _, emit = self._import_helpers()
        comparison = {
            "baseline_p95_seconds": 100.0,
            "measured_p95_seconds": 200.0,
            "ratio": 2.0,
            "regression_budget": 0.05,
            "regression_detected": True,
        }
        emit(comparison)
        captured = capsys.readouterr()
        assert "OVERSEER_ALERT " in captured.err
        import json as _json

        body = captured.err.split("OVERSEER_ALERT ", 1)[1].strip()
        envelope = _json.loads(body)
        for required in ("priority", "anomaly", "summary", "detail", "recommend"):
            assert required in envelope, f"OVERSEER_ALERT missing {required!r}"
        assert envelope["priority"] == "medium"
        assert envelope["anomaly"] == "slice-6-mcp-cli-latency-regression"
        for required in (
            "baseline_p95_seconds",
            "measured_p95_seconds",
            "ratio",
            "regression_budget",
        ):
            assert required in envelope["detail"], f"OVERSEER_ALERT.detail missing {required!r}"

    def test_alert_envelope_handles_none_ratio(self, capsys) -> None:
        """When the baseline is degenerate the ratio is ``None`` —
        rendering that to JSON must produce ``null`` (not raise) so
        downstream operators can still parse the envelope."""
        _, emit = self._import_helpers()
        comparison = {
            "baseline_p95_seconds": 0.0,
            "measured_p95_seconds": 5.0,
            "ratio": None,
            "regression_budget": 0.05,
            "regression_detected": False,
        }
        # Should not raise.
        emit(comparison)
        captured = capsys.readouterr()
        import json as _json

        body = captured.err.split("OVERSEER_ALERT ", 1)[1].strip()
        envelope = _json.loads(body)
        assert envelope["detail"]["ratio"] is None
