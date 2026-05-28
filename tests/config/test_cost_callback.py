"""Unit tests for the egg-litellm cost/cache observability callback.

``config/litellm/cost_callback.py`` runs inside the egg-litellm proxy
container, where it subclasses litellm's ``CustomLogger``. litellm is not a
project dependency (and 1.86.2 cannot run on the repo's Python 3.14), so we
stub the single symbol the module imports — ``CustomLogger`` — before
loading it from its on-disk path.

The regression these tests lock in: on the streaming path (all real Claude
Code agent traffic) LiteLLM's chunk reassembly drops the upstream
``cost`` / ``cost_details`` while preserving the token/cache counts. The
callback must therefore report ``cost`` as ``null`` — never a misleading
``0.0`` that reads as "this route is free" — while still emitting working
cache stats. See the ``cost_callback`` module docstring for the full trace.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_cost_callback():
    """Load ``cost_callback`` from disk with a stubbed ``litellm`` so it
    imports without the real (py<3.14) package."""
    litellm = types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class _CustomLogger:  # minimal stand-in; the module only subclasses it
        pass

    custom_logger.CustomLogger = _CustomLogger
    integrations.custom_logger = custom_logger
    litellm.integrations = integrations
    # setdefault: don't clobber a real litellm if one is importable.
    sys.modules.setdefault("litellm", litellm)
    sys.modules.setdefault("litellm.integrations", integrations)
    sys.modules.setdefault("litellm.integrations.custom_logger", custom_logger)

    path = Path(__file__).resolve().parents[2] / "config" / "litellm" / "cost_callback.py"
    spec = importlib.util.spec_from_file_location("egg_cost_callback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cc = _load_cost_callback()


def _streaming_usage() -> dict:
    """The shape ``ChunkProcessor.calculate_usage`` produces on the streaming
    path: token + cache fields present, ``cost`` / ``cost_details`` absent."""
    return {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cache_read_input_tokens": 600,
        "cache_creation_input_tokens": 100,
        "completion_tokens_details": {"reasoning_tokens": 50},
    }


def _nonstreaming_usage() -> dict:
    """Raw OpenRouter ``usage`` (non-streaming path) carrying a real
    upstream-billed cost."""
    return {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cost": 0.0123,
        "prompt_tokens_details": {"cached_tokens": 600},
    }


def _mcd(session_id: str, *, raw_usage: dict | None = None) -> dict:
    """Build a ``model_call_details`` dict the callback understands."""
    mcd: dict = {
        "model": "openrouter/qwen/qwen3-max",
        "proxy_server_request": {"headers": {"x-claude-code-session-id": session_id}},
    }
    if raw_usage is not None:
        mcd["original_response"] = json.dumps({"usage": raw_usage})
    return mcd


class TestExtractCost:
    def test_streaming_usage_has_no_recoverable_cost(self):
        # Reassembled streaming usage has no cost field -> must be None,
        # which the recorder treats as "unknown", not "$0".
        assert cc._extract_cost(_streaming_usage()) is None

    def test_nonstreaming_usage_cost_is_extracted(self):
        assert cc._extract_cost(_nonstreaming_usage()) == 0.0123

    def test_byok_falls_back_to_upstream_inference_cost(self):
        # Under BYOK the top-level cost is 0; the real spend is in
        # cost_details.upstream_inference_cost.
        usage = {"cost": 0, "cost_details": {"upstream_inference_cost": 0.05}}
        assert cc._extract_cost(usage) == 0.05


class TestRecordCostReporting:
    def setup_method(self):
        cc._session_totals.clear()

    def _capture(self, monkeypatch) -> list[dict]:
        emitted: list[dict] = []
        monkeypatch.setattr(cc, "_emit", lambda payload: emitted.append(payload))
        return emitted

    def test_streaming_call_reports_null_cost_not_zero(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(_mcd("s1"), types.SimpleNamespace(usage=_streaming_usage()))
        assert len(emitted) == 1
        payload = emitted[0]
        # The bug this guards: cost must be null, never coerced to 0.0.
        assert payload["call"]["cost"] is None
        assert payload["session"]["cost"] is None
        assert payload["session"]["cost_known_calls"] == 0
        # Cache stats survive reassembly, so they must still be emitted.
        assert payload["call"]["cached_tokens"] == 600
        assert payload["call"]["cache_write_tokens"] == 100
        assert payload["call"]["reasoning_tokens"] == 50
        assert payload["cache_hit_rate_pct"] == 60.0

    def test_nonstreaming_call_records_real_cost(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(_mcd("s2", raw_usage=_nonstreaming_usage()), None)
        payload = emitted[0]
        assert payload["call"]["cost"] == 0.0123
        assert payload["session"]["cost"] == 0.0123
        assert payload["session"]["cost_known_calls"] == 1

    def test_mixed_session_sums_only_known_costs(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        logger = cc.LiteLLMCostLogger()
        # A streaming turn (unknown cost) followed by a non-streaming turn
        # (real cost) in the same session.
        logger._record(_mcd("s3"), types.SimpleNamespace(usage=_streaming_usage()))
        logger._record(_mcd("s3", raw_usage=_nonstreaming_usage()), None)
        session = emitted[-1]["session"]
        assert session["calls"] == 2
        assert session["cost_known_calls"] == 1
        # Only the known cost is summed; the unknown turn contributes nothing.
        assert session["cost"] == 0.0123
