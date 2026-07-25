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
    path: token + cache fields present, ``cost`` / ``cost_details`` absent.

    NOTE: this fixture is hand-built to mirror ``calculate_usage``'s output in
    the pinned ``litellm==1.86.2`` (the version baked into the egg-litellm
    image). litellm can't run on the repo's Python 3.14, so the test can't
    assert this shape against the real reassembler — revisit this fixture on a
    litellm bump in case a newer version starts carrying ``cost`` through chunk
    reassembly (which would make the ``cost: null`` behavior under test stale).
    The build-time patcher already fails loudly on needle drift; this fixture
    has no equivalent tripwire."""
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


def _streaming_optional_params() -> dict:
    """``model_call_details['optional_params']`` as litellm 1.86.2 populates it
    on a streamed ``anthropic_messages`` call to an OpenRouter backend — the
    route and mode of all real Claude Code agent traffic.

    Captured from a live litellm 1.86.2 run (HTTP mocked at the transport
    layer, not via ``mock_response``: that short-circuits in
    ``anthropic_messages_handler`` before the adapter path runs and leaves
    ``optional_params`` empty, which would make this fixture a lie). Note what
    is NOT here: no ``temperature``, no ``top_p``, no ``top_k``, no penalty —
    egg pins none of them, so the provider's server-side defaults applied.
    That absence is the finding #3599 exists to make visible."""
    return {
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 32000,
        "extra_body": {"provider": {"order": ["Alibaba"], "allow_fallbacks": False}},
    }


def _mcd(
    session_id: str,
    *,
    raw_usage: dict | None = None,
    extra_headers: dict | None = None,
    response_cost: float | None = None,
    optional_params: dict | None = None,
) -> dict:
    """Build a ``model_call_details`` dict the callback understands."""
    headers = {"x-claude-code-session-id": session_id, **(extra_headers or {})}
    mcd: dict = {
        "model": "openrouter/qwen/qwen3-max",
        "proxy_server_request": {"headers": headers},
    }
    if raw_usage is not None:
        mcd["original_response"] = json.dumps({"usage": raw_usage})
    if response_cost is not None:
        mcd["response_cost"] = response_cost
    if optional_params is not None:
        mcd["optional_params"] = optional_params
    return mcd


_ATTRIBUTION_HEADERS = {
    "x-egg-pipeline-id": "pipeline-20260612-abc",
    "x-egg-agent-role": "reviewer_code",
    "x-egg-phase": "implement",
}


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
        # Counts render as int (not 1.0) so the "N of M" log framing reads
        # cleanly; cost stays float/None.
        assert isinstance(payload["session"]["cost_known_calls"], int)
        assert isinstance(payload["session"]["calls"], int)
        assert isinstance(payload["call"]["cached_tokens"], int)

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


class TestEstimatedCost:
    """LiteLLM's pricing-map ``response_cost`` survives streaming and is
    surfaced as ``cost_estimated``, strictly separate from the billed
    ``cost`` and under the same null-not-zero discipline (#3175)."""

    def setup_method(self):
        cc._session_totals.clear()

    def _capture(self, monkeypatch) -> list[dict]:
        emitted: list[dict] = []
        monkeypatch.setattr(cc, "_emit", lambda payload: emitted.append(payload))
        return emitted

    def test_streaming_call_carries_estimate_while_cost_stays_null(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("e1", response_cost=0.021),
            types.SimpleNamespace(usage=_streaming_usage()),
        )
        payload = emitted[0]
        # The billed cost is still unrecoverable on streaming — must stay null.
        assert payload["call"]["cost"] is None
        assert payload["call"]["cost_estimated"] == 0.021
        assert payload["session"]["cost"] is None
        assert payload["session"]["cost_estimated"] == 0.021
        assert payload["session"]["cost_estimated_known_calls"] == 1

    def test_unpriceable_model_reports_null_estimate(self, monkeypatch):
        # No response_cost (model absent from LiteLLM's pricing map) must
        # read as "unknown", never "$0".
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(_mcd("e2"), types.SimpleNamespace(usage=_streaming_usage()))
        payload = emitted[0]
        assert payload["call"]["cost_estimated"] is None
        assert payload["session"]["cost_estimated"] is None
        assert payload["session"]["cost_estimated_known_calls"] == 0

    def test_estimate_accumulates_only_known_calls(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        logger = cc.LiteLLMCostLogger()
        logger._record(
            _mcd("e3", response_cost=0.01), types.SimpleNamespace(usage=_streaming_usage())
        )
        logger._record(_mcd("e3"), types.SimpleNamespace(usage=_streaming_usage()))
        logger._record(
            _mcd("e3", response_cost=0.02), types.SimpleNamespace(usage=_streaming_usage())
        )
        session = emitted[-1]["session"]
        assert session["calls"] == 3
        assert session["cost_estimated_known_calls"] == 2
        assert round(session["cost_estimated"], 6) == 0.03

    def test_non_positive_or_garbage_estimates_are_unknown(self):
        assert cc._extract_estimated_cost({"response_cost": 0}) is None
        assert cc._extract_estimated_cost({"response_cost": -0.1}) is None
        assert cc._extract_estimated_cost({"response_cost": "0.01"}) is None
        assert cc._extract_estimated_cost({}) is None
        assert cc._extract_estimated_cost(None) is None

    def test_falls_back_to_standard_logging_object(self):
        # A LiteLLM version that omits the top-level key but populates the
        # finalized metrics object must still yield the estimate.
        assert (
            cc._extract_estimated_cost({"standard_logging_object": {"response_cost": 0.05}}) == 0.05
        )
        # Top-level takes precedence when present.
        assert (
            cc._extract_estimated_cost(
                {"response_cost": 0.02, "standard_logging_object": {"response_cost": 0.05}}
            )
            == 0.02
        )
        # A non-dict standard_logging_object is ignored, not raised on.
        assert cc._extract_estimated_cost({"standard_logging_object": "nope"}) is None


class TestAttribution:
    """Gateway-stamped ``x-egg-*`` headers land as per-line attribution
    fields; absence (pre-#3175 gateway, non-gateway client) reads as None."""

    def setup_method(self):
        cc._session_totals.clear()

    def _capture(self, monkeypatch) -> list[dict]:
        emitted: list[dict] = []
        monkeypatch.setattr(cc, "_emit", lambda payload: emitted.append(payload))
        return emitted

    def test_attribution_headers_are_emitted(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("a1", extra_headers=_ATTRIBUTION_HEADERS),
            types.SimpleNamespace(usage=_streaming_usage()),
        )
        payload = emitted[0]
        assert payload["pipeline_id"] == "pipeline-20260612-abc"
        assert payload["agent_role"] == "reviewer_code"
        assert payload["phase"] == "implement"

    def test_missing_attribution_reads_as_none(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(_mcd("a2"), types.SimpleNamespace(usage=_streaming_usage()))
        payload = emitted[0]
        assert payload["pipeline_id"] is None
        assert payload["agent_role"] is None
        assert payload["phase"] is None

    def test_header_casing_is_normalized(self, monkeypatch):
        # A non-lowercase hop must not blind the attribution lookup.
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("a3", extra_headers={"X-Egg-Agent-Role": "coder"}),
            types.SimpleNamespace(usage=_streaming_usage()),
        )
        assert emitted[0]["agent_role"] == "coder"


class TestExtractRequestParams:
    """The decoding config a call ran under, read from LiteLLM's post-mapping
    ``optional_params`` (#3599). Absence of a key is the load-bearing signal:
    it means the parameter was never sent and the provider's own default
    applied."""

    def test_streaming_agent_call_shape(self):
        params = cc._extract_request_params(_mcd("r", optional_params=_streaming_optional_params()))
        assert params["stream"] is True
        assert params["max_tokens"] == 32000
        # Nothing egg pins => the sampling knobs are simply absent, and the
        # provider default was in force. Never emit them as null: that would
        # read as "explicitly unset" rather than "never sent".
        for key in ("temperature", "top_p", "top_k", "frequency_penalty"):
            assert key not in params

    def test_explicit_sampling_params_are_recorded(self):
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    "temperature": 0.3,
                    "top_p": 0.9,
                    "top_k": 40,
                    "seed": 42,
                    "frequency_penalty": 0.2,
                    "presence_penalty": 0.1,
                    "repetition_penalty": 1.05,
                    "reasoning_effort": "high",
                },
            )
        )
        assert params["temperature"] == 0.3
        assert params["top_p"] == 0.9
        assert params["top_k"] == 40
        assert params["seed"] == 42
        assert params["frequency_penalty"] == 0.2
        assert params["presence_penalty"] == 0.1
        assert params["repetition_penalty"] == 1.05
        assert params["reasoning_effort"] == "high"

    def test_prompt_bearing_params_are_excluded(self):
        # optional_params also carries the translated tool schemas (Claude Code
        # sends a dozen-plus per request). Dumping it whole would bloat every
        # line and spill task text into a cost stream. The allowlist is the
        # guard; this pins it.
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    "temperature": 0.3,
                    "tools": [{"name": "Bash", "input_schema": {"x": "y" * 5000}}],
                    "tool_choice": "auto",
                    "messages": [{"role": "user", "content": "secret task text"}],
                    "user": "someone",
                },
            )
        )
        assert params == {"temperature": 0.3}

    def test_extra_body_hoists_known_knobs_and_keeps_the_provider_pin(self):
        # LiteLLM relocates knobs a provider doesn't declare into extra_body
        # rather than dropping them, so the same knob lands at different
        # depths per model. Hoisting keeps one query surface; the provider pin
        # (which backend, so which quantization, served the turn) is kept.
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    "temperature": 0.4,
                    "extra_body": {
                        "top_k": 40,
                        "reasoning": {"effort": "high"},
                        "provider": {"order": ["Alibaba"], "allow_fallbacks": False},
                    },
                },
            )
        )
        assert params["temperature"] == 0.4
        assert params["top_k"] == 40
        assert params["reasoning"] == {"effort": "high"}
        assert params["extra_body"] == {
            "provider": {"order": ["Alibaba"], "allow_fallbacks": False}
        }

    def test_top_level_wins_over_extra_body_duplicate(self):
        params = cc._extract_request_params(
            _mcd("r", optional_params={"top_k": 10, "extra_body": {"top_k": 40}})
        )
        assert params["top_k"] == 10
        # The losing copy is preserved rather than silently dropped, so a
        # genuine divergence is still visible in the line.
        assert params["extra_body"] == {"top_k": 40}

    def test_extra_body_omitted_when_fully_hoisted(self):
        params = cc._extract_request_params(
            _mcd("r", optional_params={"extra_body": {"top_k": 40}})
        )
        assert params == {"top_k": 40}

    def test_missing_or_malformed_optional_params_reads_as_unknown(self):
        # None means "we could not tell", distinct from {} ("reported, nothing
        # decoding-relevant") — the same unknown-is-not-zero discipline the
        # cost fields follow.
        assert cc._extract_request_params({}) is None
        assert cc._extract_request_params({"optional_params": "nope"}) is None
        assert cc._extract_request_params(None) is None
        assert cc._extract_request_params({"optional_params": {}}) == {}


class TestBoundedParam:
    """``logit_bias`` / ``stop`` are client-supplied and unbounded, and a
    serialization failure inside ``_emit`` costs the entire line, not just the
    field. Both hazards are clamped here."""

    def test_scalars_pass_through(self):
        assert cc._bounded_param(0.3) == 0.3
        assert cc._bounded_param(True) is True
        assert cc._bounded_param(None) is None
        assert cc._bounded_param("high") == "high"

    def test_oversized_values_become_size_markers(self):
        assert "chars omitted" in cc._bounded_param("x" * 5000)
        assert "chars omitted" in cc._bounded_param({str(i): i for i in range(500)})

    def test_unserializable_values_degrade_to_repr_not_raise(self):
        # A pydantic/enum-ish value must not take the log line down with it.
        value = {"effort": types.SimpleNamespace(name="high")}
        assert json.dumps(cc._bounded_param(value))


class TestRequestParamsInPayload:
    def setup_method(self):
        cc._session_totals.clear()

    def _capture(self, monkeypatch) -> list[dict]:
        emitted: list[dict] = []
        monkeypatch.setattr(cc, "_emit", lambda payload: emitted.append(payload))
        return emitted

    def test_emitted_line_carries_request_params(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("p1", optional_params=_streaming_optional_params()),
            types.SimpleNamespace(usage=_streaming_usage()),
        )
        payload = emitted[0]
        assert payload["request_params"]["max_tokens"] == 32000
        assert payload["request_params"]["extra_body"] == {
            "provider": {"order": ["Alibaba"], "allow_fallbacks": False}
        }
        # The whole line must stay serializable — _emit swallows failures, so
        # an unserializable param field would silently drop cost data too.
        assert json.dumps(payload)

    def test_absent_optional_params_does_not_suppress_the_line(self, monkeypatch):
        # Cost/cache visibility must not regress on a LiteLLM shape that
        # carries no optional_params.
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(_mcd("p2"), types.SimpleNamespace(usage=_streaming_usage()))
        payload = emitted[0]
        assert payload["request_params"] is None
        assert payload["call"]["cached_tokens"] == 600
