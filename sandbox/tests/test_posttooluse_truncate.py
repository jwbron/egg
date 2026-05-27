"""Tests for the PostToolUse truncation hook (issue #2804)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Import the script as a module (it has a .py suffix but lives in
# sandbox/hooks/ which is not a Python package).
_HOOK_PATH = Path(__file__).parent.parent / "hooks" / "posttooluse_truncate.py"
_spec = importlib.util.spec_from_file_location("posttooluse_truncate", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
posttooluse_truncate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(posttooluse_truncate)


class TestSerializedSize:
    """``_serialized_size`` must match the byte length the SDK reader sees."""

    def test_none_is_zero(self):
        assert posttooluse_truncate._serialized_size(None) == 0

    def test_str_is_utf8_length(self):
        assert posttooluse_truncate._serialized_size("hello") == 5
        # Multi-byte characters count their UTF-8 length, not codepoints
        assert posttooluse_truncate._serialized_size("é") == 2

    def test_dict_is_json_encoded(self):
        payload = {"key": "value"}
        encoded = json.dumps(payload).encode("utf-8")
        assert posttooluse_truncate._serialized_size(payload) == len(encoded)

    def test_list_of_blocks(self):
        # Matches the Claude Code tool_result shape: list of content blocks
        payload = [{"type": "text", "text": "x" * 1000}]
        size = posttooluse_truncate._serialized_size(payload)
        # Size should be at least the inner text length
        assert size >= 1000

    def test_falls_back_to_str_for_unserializable(self):
        class Weird:
            def __repr__(self) -> str:
                return "WEIRD-OBJECT"

        # default=str in json.dumps catches most; we just need this not to raise
        result = posttooluse_truncate._serialized_size(Weird())
        assert result > 0


class TestCapBytes:
    """Env override must apply and reject garbage values cleanly."""

    def test_default_cap(self):
        with patch.dict("os.environ", {}, clear=False):
            # Ensure unset
            import os

            os.environ.pop("EGG_TOOL_RESULT_CAP_BYTES", None)
            assert posttooluse_truncate._cap_bytes() == 200_000

    def test_env_override(self):
        with patch.dict("os.environ", {"EGG_TOOL_RESULT_CAP_BYTES": "50000"}):
            assert posttooluse_truncate._cap_bytes() == 50_000

    def test_env_zero_falls_back_to_default(self):
        """Zero or negative caps are nonsensical — fall back to default."""
        with patch.dict("os.environ", {"EGG_TOOL_RESULT_CAP_BYTES": "0"}):
            assert posttooluse_truncate._cap_bytes() == 200_000

    def test_env_negative_falls_back_to_default(self):
        with patch.dict("os.environ", {"EGG_TOOL_RESULT_CAP_BYTES": "-1"}):
            assert posttooluse_truncate._cap_bytes() == 200_000

    def test_env_garbage_falls_back_to_default(self):
        with patch.dict("os.environ", {"EGG_TOOL_RESULT_CAP_BYTES": "not-a-number"}):
            assert posttooluse_truncate._cap_bytes() == 200_000


class TestEvaluate:
    """``evaluate`` is the pure decision function — under cap = None
    (allow), over cap = block with reason.
    """

    def test_under_cap_returns_none(self):
        event = {"tool_name": "Read", "tool_response": "small payload"}
        assert posttooluse_truncate.evaluate(event, cap=200_000) is None

    def test_at_exact_cap_returns_none(self):
        # Equal to cap is still allowed; only strictly greater blocks.
        payload = "x" * 100
        event = {"tool_name": "Read", "tool_response": payload}
        assert posttooluse_truncate.evaluate(event, cap=100) is None

    def test_over_cap_returns_block(self):
        payload = "x" * 10_000
        event = {"tool_name": "Read", "tool_response": payload}
        result = posttooluse_truncate.evaluate(event, cap=5_000)
        assert result is not None
        assert result["decision"] == "block"
        assert "Read" in result["reason"]

    def test_missing_tool_response_allows(self):
        event = {"tool_name": "Read"}
        assert posttooluse_truncate.evaluate(event, cap=100) is None

    def test_reason_cites_issue(self):
        event = {"tool_name": "Bash", "tool_response": "x" * 1000}
        result = posttooluse_truncate.evaluate(event, cap=500)
        assert result is not None
        assert "#2804" in result["reason"]

    def test_reason_reports_size_and_cap(self):
        event = {"tool_name": "Read", "tool_response": "x" * 1000}
        result = posttooluse_truncate.evaluate(event, cap=500)
        assert result is not None
        # Both numbers should appear so the agent (and the operator
        # debugging logs) can see how badly the call overshot.
        assert "1000" in result["reason"] or "1,000" in result["reason"]
        assert "500" in result["reason"]


class TestMutatingTools:
    """Edit/Write/MultiEdit/NotebookEdit already mutated disk; the
    agent must be told NOT to retry the call.
    """

    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
    def test_mutating_tool_reason_warns_against_retry(self, tool: str) -> None:
        event = {"tool_name": tool, "tool_response": "x" * 10_000}
        result = posttooluse_truncate.evaluate(event, cap=1_000)
        assert result is not None
        # The reason must explicitly tell the model NOT to retry — re-running
        # an Edit that already landed on disk would either fail (string-not-found)
        # or worse, double-apply.
        assert "do NOT retry" in result["reason"] or "do not retry" in result["reason"].lower()

    @pytest.mark.parametrize("tool", ["Read", "Bash", "Grep", "Glob"])
    def test_idempotent_tool_reason_offers_retry_guidance(self, tool: str) -> None:
        event = {"tool_name": tool, "tool_response": "x" * 10_000}
        result = posttooluse_truncate.evaluate(event, cap=1_000)
        assert result is not None
        # For idempotent tools the agent SHOULD retry, just with narrower scope.
        # Reason must contain actionable guidance (offset/limit, head, narrow pattern, etc.)
        reason_lower = result["reason"].lower()
        assert any(
            hint in reason_lower
            for hint in ("offset", "limit", "head", "narrow", "smaller", "scope")
        ), f"Reason for {tool} should contain retry guidance: {result['reason']!r}"

    def test_unknown_tool_gets_fallback_guidance(self):
        event = {"tool_name": "FutureTool", "tool_response": "x" * 10_000}
        result = posttooluse_truncate.evaluate(event, cap=1_000)
        assert result is not None
        assert "FutureTool" in result["reason"]
        assert "narrower" in result["reason"].lower()


class TestMain:
    """End-to-end stdin/stdout protocol that the CLI hook actually
    invokes.
    """

    @staticmethod
    def _run_main(payload: Any) -> tuple[int, str]:
        stdin = io.StringIO(json.dumps(payload) if payload is not None else "")
        stdout = io.StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
            rc = posttooluse_truncate.main()
        return rc, stdout.getvalue()

    def test_under_cap_writes_nothing(self):
        rc, out = self._run_main(
            {"tool_name": "Read", "tool_response": "small"},
        )
        assert rc == 0
        assert out == ""

    def test_over_cap_writes_block_json(self):
        # Cap is 200KB by default; payload > 200KB to force block
        rc, out = self._run_main(
            {"tool_name": "Read", "tool_response": "x" * 300_000},
        )
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["decision"] == "block"
        assert "Read" in parsed["reason"]

    def test_empty_stdin_allows(self):
        rc, out = self._run_main(None)
        assert rc == 0
        assert out == ""

    def test_malformed_json_allows(self):
        """Unparseable input should fall through, not crash.

        The buffer-bump + clean-error path in client.py will catch any
        oversize payload that slips past as a structured failure; the
        hook should never amplify a parse error into an agent crash.
        """
        stdin = io.StringIO("not json at all")
        stdout = io.StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
            rc = posttooluse_truncate.main()
        assert rc == 0
        assert stdout.getvalue() == ""

    def test_non_dict_input_allows(self):
        rc, out = self._run_main(["not", "a", "dict"])
        assert rc == 0
        assert out == ""

    def test_respects_env_cap(self):
        """Lower the cap via env so a small payload triggers block."""
        import os

        with patch.dict("os.environ", {"EGG_TOOL_RESULT_CAP_BYTES": "50"}):
            os.environ["EGG_TOOL_RESULT_CAP_BYTES"] = "50"
            rc, out = self._run_main(
                {"tool_name": "Bash", "tool_response": "x" * 200},
            )
            assert rc == 0
            parsed = json.loads(out)
            assert parsed["decision"] == "block"
