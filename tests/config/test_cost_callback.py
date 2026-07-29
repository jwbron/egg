"""Unit tests for the egg-litellm cost/cache observability callback.

``config/litellm/cost_callback.py`` runs inside the egg-litellm proxy
container, where it subclasses litellm's ``CustomLogger``. litellm is not a
project dependency (and 1.86.2 cannot run on the repo's Python 3.14), so we
stub the single symbol the module imports — ``CustomLogger`` — before
loading it from its on-disk path.

The regression these tests lock in is the callback's reading of cost, on both
sides of the #3691 fix. Stock LiteLLM's chunk reassembly drops the provider's
``cost`` / ``cost_details`` while preserving the token/cache counts, which put
``cost: null`` on 1252 of 1252 sampled calls; the egg-litellm image's patch 10
carries them through, so a streamed call now arrives with the bill attached
and the callback reads it with no change of its own. Both states must work:
a cost that arrives is recorded, and one that does not is reported as ``null``
— never a misleading ``0.0`` that reads as "this route is free" — with the
cache stats emitted either way. See the ``cost_callback`` module docstring for
the full trace.
"""

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest


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


def _usage_without_cost() -> dict:
    """Usage carrying token + cache fields but no cost of any kind.

    This is what stock ``ChunkProcessor.calculate_usage`` produces on the
    streaming path, where the rebuild enumerates counts and discards the
    provider's ``cost`` / ``cost_details`` (#3691). On the egg-litellm image
    patch 10 carries them through, so this shape now stands for the residual
    cases — an unpatched LiteLLM under this callback, or a provider that
    reports no cost at all — rather than for streaming as such. Either way the
    callback's contract is the same and is what these tests pin: unknown is
    reported as ``null``, never coerced to ``0.0``.

    Hand-built rather than captured: litellm can't run on the repo's Python
    3.14. The build-time patcher fails loudly on needle drift, but this fixture
    has no equivalent tripwire, so revisit it on a litellm bump."""
    return {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cache_read_input_tokens": 600,
        "cache_creation_input_tokens": 100,
        "completion_tokens_details": {"reasoning_tokens": 50},
    }


def _usage_with_cost() -> dict:
    """Raw OpenRouter ``usage`` carrying a real provider-billed cost.

    Reaches the callback two ways: as ``original_response`` on the
    non-streaming path, and — since patch 10 — on the reassembled usage object
    of a streamed call. ``_extract_cost`` cannot tell the two apart, which is
    the point: the transport was the bug, not the reading."""
    return {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cost": 0.0123,
        "prompt_tokens_details": {"cached_tokens": 600},
    }


def _reassembled_usage_with_cost() -> dict:
    """What ``calculate_usage`` hands the success hook once patch 10 has run:
    the enumerated counts it always kept, plus the two cost fields it used to
    drop. ``cost_details`` rides along as a pydantic extra."""
    return {
        **_usage_without_cost(),
        "cost": 0.0123,
        "cost_details": {"upstream_inference_cost": 0.0456},
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
    def test_usage_without_a_cost_field_reads_as_unknown(self):
        # No cost of any kind -> must be None, which the recorder treats as
        # "unknown", not "$0".
        assert cc._extract_cost(_usage_without_cost()) is None

    def test_usage_cost_is_extracted(self):
        assert cc._extract_cost(_usage_with_cost()) == 0.0123

    def test_byok_falls_back_to_upstream_inference_cost(self):
        # Under BYOK the top-level cost is 0; the real spend is in
        # cost_details.upstream_inference_cost.
        usage = {"cost": 0, "cost_details": {"upstream_inference_cost": 0.05}}
        assert cc._extract_cost(usage) == 0.05

    def test_non_finite_cost_reads_as_unknown_not_as_spend(self):
        # `inf > 0` is True, so a bare positivity check would accept it — and
        # then accumulate it into the session total, which stays Infinity for
        # the pod's lifetime and emits a non-standard token that invalidates
        # every subsequent line's JSON. (NaN is already excluded: nan > 0 is
        # False.) Unknown, not spend.
        assert cc._extract_cost({"cost": float("inf")}) is None
        assert cc._extract_cost({"cost": float("nan")}) is None
        assert (
            cc._extract_cost({"cost": 0, "cost_details": {"upstream_inference_cost": float("inf")}})
            is None
        )
        # The fallback still fires when only the top-level value is poisoned.
        usage = {"cost": float("inf"), "cost_details": {"upstream_inference_cost": 0.05}}
        assert cc._extract_cost(usage) == 0.05

    def test_boolean_cost_is_not_a_cost(self):
        # JSON `"cost": true` deserializes to Python True, and isinstance(True,
        # int) is True — so a bare numeric check accepts it and float(True)
        # records a $1.00 charge that was never billed. Unknown, not spend.
        assert cc._extract_cost({"cost": True}) is None
        assert (
            cc._extract_cost({"cost": 0, "cost_details": {"upstream_inference_cost": True}}) is None
        )
        assert cc._extract_estimated_cost({"response_cost": True}) is None

    def test_non_finite_estimated_cost_reads_as_unknown(self):
        assert cc._extract_estimated_cost({"response_cost": float("inf")}) is None
        assert cc._extract_estimated_cost({"response_cost": 0.004}) == 0.004


class TestExtractCacheStats:
    """Token counts get the same guard the cost fields get, for a sharper
    reason: they are emitted through ``int(...)``, where a non-finite raises
    rather than merely misreporting."""

    def setup_method(self):
        cc._session_totals.clear()

    def test_boolean_and_non_finite_token_counts_are_not_counts(self):
        # Same shapes that make a boolean cost a $1.00 charge: isinstance(True,
        # int) is True, so a bare numeric check reads `True` as one token that
        # was never sent, and `inf` as an infinity of them.
        for bad in (True, float("inf"), float("nan"), -5, "1200", None):
            assert cc._extract_cache_stats({"prompt_tokens": bad}) == (0.0, 0.0, 0.0, 0.0)
        assert cc._extract_cache_stats({"prompt_tokens": 1200}) == (1200.0, 0.0, 0.0, 0.0)
        # The nested schemas are read through the same helper.
        assert cc._extract_cache_stats(
            {"prompt_tokens_details": {"cached_tokens": float("inf")}}
        ) == (0.0, 0.0, 0.0, 0.0)
        assert cc._extract_cache_stats(
            {"completion_tokens_details": {"reasoning_tokens": True}}
        ) == (0.0, 0.0, 0.0, 0.0)

    def test_a_non_finite_count_does_not_silence_the_rest_of_the_session(self, monkeypatch):
        # The consequence is worse than for cost: `int(inf)` raises OverflowError
        # inside _record, whose outer handler drops the whole line — and the
        # value has already landed in the session total, so it stays inf and
        # every LATER call in that session is dropped too, for the pod's
        # lifetime (the LRU only evicts under 4096-session pressure).
        emitted: list[dict] = []
        monkeypatch.setattr(cc, "_emit", lambda payload: emitted.append(payload))
        logger = cc.LiteLLMCostLogger()
        logger._record(
            _mcd("poisoned"),
            types.SimpleNamespace(
                usage={"prompt_tokens": float("inf"), "cache_read_input_tokens": 600}
            ),
        )
        logger._record(
            _mcd("poisoned"),
            types.SimpleNamespace(usage={"prompt_tokens": 1000, "cache_read_input_tokens": 600}),
        )
        assert len(emitted) == 2
        assert emitted[-1]["call"]["prompt_tokens"] == 1000
        assert cc._session_totals["poisoned"]["prompt_tokens"] == 1000.0


class TestRecordCostReporting:
    def setup_method(self):
        cc._session_totals.clear()

    def _capture(self, monkeypatch) -> list[dict]:
        emitted: list[dict] = []
        monkeypatch.setattr(cc, "_emit", lambda payload: emitted.append(payload))
        return emitted

    def test_call_without_a_reported_cost_reports_null_not_zero(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("s1"), types.SimpleNamespace(usage=_usage_without_cost())
        )
        assert len(emitted) == 1
        payload = emitted[0]
        # The bug this guards: cost must be null, never coerced to 0.0.
        assert payload["call"]["cost"] is None
        assert payload["session"]["cost"] is None
        assert payload["session"]["cost_known_calls"] == 0
        # An unknown cost must not take the cache stats down with it.
        assert payload["call"]["cached_tokens"] == 600
        assert payload["call"]["cache_write_tokens"] == 100
        assert payload["call"]["reasoning_tokens"] == 50
        assert payload["cache_hit_rate_pct"] == 60.0
        # Counts render as int (not 1.0) so the "N of M" log framing reads
        # cleanly; cost stays float/None.
        assert isinstance(payload["session"]["cost_known_calls"], int)
        assert isinstance(payload["session"]["calls"], int)
        assert isinstance(payload["call"]["cached_tokens"], int)

    def test_raw_upstream_usage_records_real_cost(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(_mcd("s2", raw_usage=_usage_with_cost()), None)
        payload = emitted[0]
        assert payload["call"]["cost"] == 0.0123
        assert payload["session"]["cost"] == 0.0123
        assert payload["session"]["cost_known_calls"] == 1

    def test_reassembled_streaming_usage_records_real_cost(self, monkeypatch):
        """The #3691 fix, read from this side of the seam.

        Patch 10 carries ``cost`` / ``cost_details`` across
        ``calculate_usage``'s rebuild, so a streamed call — which is
        essentially all agent traffic — now arrives with the bill attached and
        the callback needs no change to read it. This test is the regression
        that would catch the transport being lost again on a litellm bump.
        """
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("s4"), types.SimpleNamespace(usage=_reassembled_usage_with_cost())
        )
        payload = emitted[0]
        assert payload["call"]["cost"] == 0.0123
        assert payload["session"]["cost_known_calls"] == 1
        # The counts the rebuild always kept are still there beside it.
        assert payload["call"]["cached_tokens"] == 600
        assert payload["call"]["cache_write_tokens"] == 100

    def test_reassembled_byok_usage_falls_through_to_upstream_cost(self, monkeypatch):
        """Under BYOK the top-level ``cost`` is a literal 0 and the real number
        is in ``cost_details``. Patch 10 transports both without judging
        either, so the fall-through ``_extract_cost`` already implements is
        what turns them into a figure — and a 0 must not be recorded as spend.
        """
        emitted = self._capture(monkeypatch)
        usage = {
            **_usage_without_cost(),
            "cost": 0,
            "cost_details": {"upstream_inference_cost": 0.05},
        }
        cc.LiteLLMCostLogger()._record(_mcd("s5"), types.SimpleNamespace(usage=usage))
        assert emitted[0]["call"]["cost"] == 0.05

    def test_mixed_session_sums_only_known_costs(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        logger = cc.LiteLLMCostLogger()
        # A turn whose cost never arrived, followed by one carrying a real cost.
        logger._record(_mcd("s3"), types.SimpleNamespace(usage=_usage_without_cost()))
        logger._record(_mcd("s3", raw_usage=_usage_with_cost()), None)
        session = emitted[-1]["session"]
        assert session["calls"] == 2
        assert session["cost_known_calls"] == 1
        # Only the known cost is summed; the unknown turn contributes nothing.
        assert session["cost"] == 0.0123


class TestPromptTokensDelta:
    """Per-call growth of the prompt within a session (#3595) — the livelock
    signature, made readable from one line instead of by differencing a whole
    session's lines against each other after the fact."""

    def setup_method(self):
        cc._session_totals.clear()

    def _capture(self, monkeypatch) -> list[dict]:
        emitted: list[dict] = []
        monkeypatch.setattr(cc, "_emit", lambda payload: emitted.append(payload))
        return emitted

    def _usage(self, prompt_tokens: int) -> dict:
        return {"prompt_tokens": prompt_tokens, "completion_tokens": 10, "cost": 0.001}

    def test_first_call_has_no_delta(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(_mcd("d1", raw_usage=self._usage(1000)), None)
        # No predecessor to difference against. That is not a delta of zero,
        # which would read as "the prompt did not grow".
        assert emitted[0]["call"]["prompt_tokens_delta"] is None

    def test_repetition_trap_shows_a_small_constant_delta(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        logger = cc.LiteLLMCostLogger()
        # The observed #3595 shape: each turn re-sends the whole context plus
        # one near-identical increment.
        for prompt in (460_000, 460_527, 461_054, 461_581):
            logger._record(_mcd("d2", raw_usage=self._usage(prompt)), None)
        deltas = [e["call"]["prompt_tokens_delta"] for e in emitted]
        assert deltas == [None, 527, 527, 527]

    def test_deltas_are_tracked_per_session_not_globally(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        logger = cc.LiteLLMCostLogger()
        logger._record(_mcd("a", raw_usage=self._usage(1000)), None)
        logger._record(_mcd("b", raw_usage=self._usage(50_000)), None)
        logger._record(_mcd("a", raw_usage=self._usage(1200)), None)
        # Session b's much larger prompt must not contaminate session a's
        # delta; interleaved sessions are the normal case on a busy proxy.
        assert emitted[1]["call"]["prompt_tokens_delta"] is None
        assert emitted[2]["call"]["prompt_tokens_delta"] == 200

    def test_a_shrinking_prompt_reads_as_negative_not_as_zero(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        logger = cc.LiteLLMCostLogger()
        logger._record(_mcd("c", raw_usage=self._usage(400_000)), None)
        # A compaction / reseed drops the prompt. Recording that as a real
        # negative is what distinguishes it from a stalled-but-flat session.
        logger._record(_mcd("c", raw_usage=self._usage(12_000)), None)
        assert emitted[-1]["call"]["prompt_tokens_delta"] == -388_000


class TestBuildAttribution:
    """Which physical build served the turn (#3692): provider per call off the
    response, endpoint name + quantization per model from cached metadata."""

    def setup_method(self):
        cc._session_totals.clear()
        cc._build_cache.clear()
        cc._build_pending.clear()

    def _capture(self, monkeypatch) -> list[dict]:
        emitted: list[dict] = []
        monkeypatch.setattr(cc, "_emit", lambda payload: emitted.append(payload))
        return emitted

    def test_no_network_without_a_configured_key(self, monkeypatch):
        # The logging hook must never reach for the network uninvited; without
        # the credential the proxy runs with, the lookup is not attempted.
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            cc, "_fetch_build_info", lambda slug: pytest.fail("fetched without a key")
        )
        assert cc._build_info("openrouter/poolside/laguna-s-2.1") is None

    def test_slug_strips_the_openrouter_prefix(self):
        assert cc._upstream_slug("openrouter/poolside/laguna-s-2.1") == "poolside/laguna-s-2.1"
        assert cc._upstream_slug("poolside/laguna-s-2.1") == "poolside/laguna-s-2.1"
        assert cc._upstream_slug(None) is None

    def test_metadata_is_fetched_once_per_model_then_cached(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        calls = []

        def fake_fetch(slug):
            calls.append(slug)
            return {
                "name": "Poolside | poolside/laguna-s-2.1-20260720",
                "quantization": "bf16",
                "context_length": 1048576,
            }

        monkeypatch.setattr(cc, "_fetch_build_info", fake_fetch)
        # Runs on a daemon thread, so the first sighting returns None; join by
        # polling the cache rather than sleeping a fixed interval.
        assert cc._build_info("openrouter/poolside/laguna-s-2.1") is None
        for _ in range(200):
            if "poolside/laguna-s-2.1" in cc._build_cache:
                break
            time.sleep(0.01)
        info = cc._build_info("openrouter/poolside/laguna-s-2.1")
        assert info["quantization"] == "bf16"
        assert info["name"].endswith("20260720")
        # A second and third sighting must not re-fetch.
        cc._build_info("openrouter/poolside/laguna-s-2.1")
        assert calls == ["poolside/laguna-s-2.1"]

    def test_a_failed_lookup_is_cached_so_it_is_not_retried_per_call(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        calls = []
        monkeypatch.setattr(cc, "_fetch_build_info", lambda slug: calls.append(slug) or None)
        cc._build_info("openrouter/m")
        for _ in range(200):
            if "m" in cc._build_cache:
                break
            time.sleep(0.01)
        cc._build_info("openrouter/m")
        cc._build_info("openrouter/m")
        assert calls == ["m"]

    def test_provider_is_read_off_the_nonstreaming_response_body(self):
        mcd = {"original_response": json.dumps({"provider": "Poolside", "usage": {}})}
        assert cc._extract_provider(mcd, None) == "Poolside"

    def test_provider_falls_back_to_the_assembled_object(self):
        obj = types.SimpleNamespace(provider="Poolside")
        assert cc._extract_provider({}, obj) == "Poolside"

    def test_unknown_provider_is_none_not_a_guess(self):
        assert cc._extract_provider({}, None) is None

    def test_endpoint_block_is_emitted_with_nulls_before_the_lookup_lands(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(_mcd("b1", raw_usage=_usage_with_cost()), None)
        ep = emitted[0]["endpoint"]
        # Null means "not yet known", never a fabricated default.
        assert set(ep) == {"provider", "name", "quantization", "context_length"}
        assert ep["name"] is None and ep["quantization"] is None


class TestEstimatedCost:
    """LiteLLM's pricing-map ``response_cost``, surfaced as ``cost_estimated``
    — strictly separate from the billed ``cost`` and under the same
    null-not-zero discipline (#3175). The two are independent measurements of
    the same turn, so each must be readable when the other is missing."""

    def setup_method(self):
        cc._session_totals.clear()

    def _capture(self, monkeypatch) -> list[dict]:
        emitted: list[dict] = []
        monkeypatch.setattr(cc, "_emit", lambda payload: emitted.append(payload))
        return emitted

    def test_estimate_is_carried_when_the_billed_cost_is_missing(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("e1", response_cost=0.021),
            types.SimpleNamespace(usage=_usage_without_cost()),
        )
        payload = emitted[0]
        assert payload["call"]["cost"] is None
        assert payload["call"]["cost_estimated"] == 0.021
        assert payload["session"]["cost"] is None
        assert payload["session"]["cost_estimated"] == 0.021
        assert payload["session"]["cost_estimated_known_calls"] == 1

    def test_both_figures_are_reported_side_by_side(self, monkeypatch):
        """With patches 10 and 11 both in place this is the ordinary line, and
        a persistent gap between the two is a signal (stale rate card,
        unexpected provider, surcharge tier) rather than an error."""
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("e4", response_cost=0.0119),
            types.SimpleNamespace(usage=_reassembled_usage_with_cost()),
        )
        payload = emitted[0]
        assert payload["call"]["cost"] == 0.0123
        assert payload["call"]["cost_estimated"] == 0.0119
        assert payload["session"]["cost_known_calls"] == 1
        assert payload["session"]["cost_estimated_known_calls"] == 1

    def test_unpriceable_model_reports_null_estimate(self, monkeypatch):
        # No response_cost (model absent from LiteLLM's pricing map) must
        # read as "unknown", never "$0".
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("e2"), types.SimpleNamespace(usage=_usage_without_cost())
        )
        payload = emitted[0]
        assert payload["call"]["cost_estimated"] is None
        assert payload["session"]["cost_estimated"] is None
        assert payload["session"]["cost_estimated_known_calls"] == 0

    def test_estimate_accumulates_only_known_calls(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        logger = cc.LiteLLMCostLogger()
        logger._record(
            _mcd("e3", response_cost=0.01), types.SimpleNamespace(usage=_usage_without_cost())
        )
        logger._record(_mcd("e3"), types.SimpleNamespace(usage=_usage_without_cost()))
        logger._record(
            _mcd("e3", response_cost=0.02), types.SimpleNamespace(usage=_usage_without_cost())
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

    def test_present_but_unusable_top_level_does_not_suppress_the_fallback(self):
        # The gate is positivity, not presence. `response_cost: 0.0` is what
        # LiteLLM writes when it cannot price the model — precisely the case
        # the fallback exists for — so a presence check would let that zero
        # mask a perfectly good estimate in the finalized metrics object, and
        # the call would read as unpriceable when it wasn't.
        assert (
            cc._extract_estimated_cost(
                {"response_cost": 0.0, "standard_logging_object": {"response_cost": 0.004}}
            )
            == 0.004
        )
        assert (
            cc._extract_estimated_cost(
                {
                    "response_cost": float("inf"),
                    "standard_logging_object": {"response_cost": 0.004},
                }
            )
            == 0.004
        )
        # Symmetric with _extract_cost, whose BYOK `cost: 0` falls through to
        # cost_details in exactly the same way. Pin the value, not truthiness:
        # a bare `assert` here passes on any non-zero return, including a wrong
        # one, which is no assertion at all.
        assert (
            cc._extract_cost({"cost": 0, "cost_details": {"upstream_inference_cost": 0.05}}) == 0.05
        )


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
            types.SimpleNamespace(usage=_usage_without_cost()),
        )
        payload = emitted[0]
        assert payload["pipeline_id"] == "pipeline-20260612-abc"
        assert payload["agent_role"] == "reviewer_code"
        assert payload["phase"] == "implement"

    def test_missing_attribution_reads_as_none(self, monkeypatch):
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("a2"), types.SimpleNamespace(usage=_usage_without_cost())
        )
        payload = emitted[0]
        assert payload["pipeline_id"] is None
        assert payload["agent_role"] is None
        assert payload["phase"] is None

    def test_header_casing_is_normalized(self, monkeypatch):
        # A non-lowercase hop must not blind the attribution lookup.
        emitted = self._capture(monkeypatch)
        cc.LiteLLMCostLogger()._record(
            _mcd("a3", extra_headers={"X-Egg-Agent-Role": "coder"}),
            types.SimpleNamespace(usage=_usage_without_cost()),
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
        #
        # ``tools`` is recorded by ARITY only (``tools_count``): tool presence
        # is the strongest observed lever on whether the model reasons, so the
        # integer is load-bearing while the schemas it counts are exactly the
        # bloat the allowlist exists to keep out.
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
        assert params == {"temperature": 0.3, "tools_count": 1, "tool_choice": "auto"}
        # The schema body and the prompt text are the things that must never
        # reach this stream, whatever else the allowlist grows to carry.
        rendered = json.dumps(params)
        assert "y" * 100 not in rendered
        assert "secret task text" not in rendered

    def test_tools_arity_is_recorded_without_the_schemas(self):
        # 0 vs many is the signal the 2x2 turned on, so the count has to
        # survive a realistic dozen-plus-tool Claude Code request intact.
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    "tools": [
                        {"name": f"tool{i}", "input_schema": {"x": "y" * 400}} for i in range(14)
                    ],
                    "tool_choice": "none",
                },
            )
        )
        assert params["tools_count"] == 14
        assert params["tool_choice"] == "none"
        assert "y" * 100 not in json.dumps(params)

    def test_absent_tools_key_is_not_reported_as_zero(self):
        # "we could not tell" and "no callable tools were offered" are
        # different claims; only the latter may read as 0.
        params = cc._extract_request_params(_mcd("r", optional_params={"temperature": 0.3}))
        assert "tools_count" not in params
        empty = cc._extract_request_params(_mcd("r", optional_params={"tools": []}))
        assert empty["tools_count"] == 0

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

    def test_bulky_extra_body_sibling_does_not_collapse_the_provider_pin(self):
        # extra_body values are bounded individually, so a large sibling knob
        # degrades to a size marker on its own without taking the small,
        # load-bearing provider pin down with it under one shared cap.
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    "extra_body": {
                        "provider": {"order": ["Alibaba"]},
                        "junk": "z" * 600,
                    }
                },
            )
        )
        assert params["extra_body"]["provider"] == {"order": ["Alibaba"]}
        assert "chars omitted" in params["extra_body"]["junk"]

    def test_a_real_sized_provider_pin_is_not_collapsed_by_the_value_cap(self):
        # Hoisting the pin ahead of the COUNT cap doesn't exempt it from the
        # SIZE cap, and a pin with an `ignore` list of a few dozen backends
        # clears 512 chars on its own — so under the general cap the one entry
        # naming which backend served the turn is the one that degrades, in the
        # incident reconstruction it exists for. Priority keys get a wider cap.
        pin = {
            "order": [f"Provider{i}" for i in range(6)],
            "ignore": [f"Ignored{i}" for i in range(30)],
            "allow_fallbacks": False,
            "quantizations": ["fp8", "bf16"],
        }
        assert len(json.dumps(pin)) > cc._MAX_PARAM_JSON_CHARS
        params = cc._extract_request_params(
            _mcd("r", optional_params={"extra_body": {"provider": pin, "junk": "z" * 600}})
        )
        assert params["extra_body"]["provider"] == pin
        # Still a cap, not an exemption — one entry cannot run away either.
        assert "chars omitted" in cc._bounded_param(
            {"order": ["p" * cc._MAX_PRIORITY_PARAM_JSON_CHARS]},
            cc._MAX_PRIORITY_PARAM_JSON_CHARS,
        )
        # And a non-priority sibling keeps the tighter budget.
        assert "chars omitted" in params["extra_body"]["junk"]

    def test_extra_body_stays_bounded_in_aggregate(self):
        # Per-value bounding leaves the key COUNT and the key NAMES unbounded.
        # extra_body is config-supplied and usually pinned in litellm_params, so
        # an unbounded remainder is not one bad line — it is every line for the
        # life of the config.
        #
        # The fixture is the stated worst case rather than a merely large one:
        # every key name past its cap (so each emits the 64-char prefix AND the
        # size suffix), every value exactly at its cap, and the priority key
        # drawing on the wider budget. A fixture whose pin is two words leaves
        # the priority slack term in the bound below as pure headroom — the
        # assertion then means whatever it meant before that term existed.
        pin = {"order": [f"backend-{i:03d}" for i in range(130)], "allow_fallbacks": False}
        # Near the priority cap, not merely over the general one — that is what
        # makes the slack term below load-bearing rather than headroom.
        assert cc._MAX_PARAM_JSON_CHARS < len(json.dumps(pin)) <= cc._MAX_PRIORITY_PARAM_JSON_CHARS
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    "extra_body": {
                        "provider": pin,
                        **{
                            f"knob{i}" + "k" * 100000: "v" * cc._MAX_PARAM_JSON_CHARS
                            for i in range(200)
                        },
                    }
                },
            )
        )
        emitted = params["extra_body"]
        assert len(emitted) == cc._MAX_EXTRA_BODY_KEYS + 1  # + the truncation marker
        assert "more keys omitted" in emitted[cc._EXTRA_BODY_TRUNCATED_KEY]
        assert emitted["provider"] == pin
        assert all(len(k) <= cc._MAX_EXTRA_BODY_KEY_CHARS + 32 for k in emitted)
        # The bound stated as it is derived — per key, a name at its cap plus
        # the size suffix, a value at its cap, and JSON punctuation — with the
        # one priority key drawing on the wider value budget. Asserted from
        # below as well as above: a maximal fixture that fits under the general
        # term alone would leave the slack term unexercised, which is how a
        # loose bound comes to look like a pinned one.
        general = cc._MAX_EXTRA_BODY_KEYS * (
            cc._MAX_EXTRA_BODY_KEY_CHARS + 32 + cc._MAX_PARAM_JSON_CHARS + 8
        )
        slack = cc._MAX_PRIORITY_PARAM_JSON_CHARS - cc._MAX_PARAM_JSON_CHARS
        assert general < len(json.dumps(emitted)) < general + slack

    def test_the_whole_params_block_is_bounded_against_the_log_line_split(self):
        # Each dimension's cap holds and they still compose: 19 allowlisted keys
        # at the value cap alongside a maximal extra_body clears 20KB. A line
        # that long is split into unparseable partials by the container runtime,
        # so `jq -Rc 'fromjson?'` drops it and the cost data with it — the
        # outcome allow_nan=False exists to prevent, reached by size instead.
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    **dict.fromkeys(cc._REQUEST_PARAM_KEYS, "v" * cc._MAX_PARAM_JSON_CHARS),
                    "extra_body": {
                        "provider": {"order": ["Alibaba"]},
                        **{
                            f"knob{i}" + "k" * 100000: "v" * cc._MAX_PARAM_JSON_CHARS
                            for i in range(100)
                        },
                    },
                },
            )
        )
        assert len(json.dumps(params)) <= cc._MAX_REQUEST_PARAMS_CHARS
        # What survives is what the field exists to answer: the sampling knobs
        # (a handful of bytes each — never what pushed the block over) and the
        # provider pin, which collapses out of extra_body rather than with it.
        assert params["temperature"] == "v" * cc._MAX_PARAM_JSON_CHARS
        assert params["extra_body"]["provider"] == {"order": ["Alibaba"]}
        assert "more keys omitted" in params["extra_body"][cc._EXTRA_BODY_TRUNCATED_KEY]

    def test_a_stock_line_is_left_alone_by_the_aggregate_bound(self):
        # The guard is a backstop, not a filter: nothing a real route emits goes
        # near the budget, and a trim that fired on stock traffic would be a
        # regression in exactly the data this PR adds.
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    "max_tokens": 32000,
                    "stream": True,
                    "temperature": 0.3,
                    "extra_body": {"provider": {"order": ["Alibaba"], "allow_fallbacks": False}},
                },
            )
        )
        assert params == {
            "temperature": 0.3,
            "max_tokens": 32000,
            "stream": True,
            "extra_body": {"provider": {"order": ["Alibaba"], "allow_fallbacks": False}},
        }
        assert len(json.dumps(params)) < cc._MAX_REQUEST_PARAMS_CHARS // 10

    def test_oversized_extra_body_key_name_is_bounded(self):
        # The key COUNT cap alone doesn't bound the key NAMES: a long key inside
        # the count cap is emitted verbatim on every line for the life of the
        # config. (The aggregate test above cannot catch this — its long key
        # sits past index 32, so the count slice removes it before the name
        # bound is ever reached.)
        params = cc._extract_request_params(
            _mcd("r", optional_params={"extra_body": {"K" * 600: 1}})
        )
        [key] = params["extra_body"]
        assert len(key) <= cc._MAX_EXTRA_BODY_KEY_CHARS + 32
        assert "600 chars omitted" in key
        assert params["extra_body"][key] == 1

    def test_two_oversized_keys_do_not_collapse_into_one_entry(self):
        # A bare size marker would be IDENTICAL for both keys, so one value
        # would silently overwrite the other — a size problem turned into a
        # data-loss problem, and a lost knob reads as one that was never sent.
        params = cc._extract_request_params(
            _mcd("r", optional_params={"extra_body": {"A" * 600: 1, "B" * 600: 2}})
        )
        assert len(params["extra_body"]) == 2
        assert sorted(params["extra_body"].values()) == [1, 2]

    def test_colliding_normalized_keys_are_disambiguated(self):
        # Distinct source keys can normalize to the same emitted name (here via
        # str() on the int). Suffix rather than overwrite.
        params = cc._extract_request_params(
            _mcd("r", optional_params={"extra_body": {1: "int", "1": "str"}})
        )
        assert len(params["extra_body"]) == 2
        assert sorted(params["extra_body"].values()) == ["int", "str"]

    def test_truncation_marker_does_not_overwrite_a_real_key(self):
        # An operator key named exactly like the sentinel must not be clobbered
        # by the marker, and must not consume the marker's slot either. The
        # sentinel's own name is what has to be used here: a test written
        # against some *other* name passes no matter what the marker collides
        # with, which makes the `_MAX_EXTRA_BODY_KEYS + 1` invariant below
        # fixture-specific rather than general.
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    "extra_body": {
                        cc._EXTRA_BODY_TRUNCATED_KEY: "real value",
                        **{f"knob{i}": i for i in range(40)},
                    }
                },
            )
        )
        emitted = params["extra_body"]
        assert emitted[cc._EXTRA_BODY_TRUNCATED_KEY] == "real value"
        assert "more keys omitted" in emitted[f"{cc._EXTRA_BODY_TRUNCATED_KEY}<2>"]
        assert len(emitted) == cc._MAX_EXTRA_BODY_KEYS + 1

    def test_provider_pin_survives_a_late_position_under_the_count_cap(self):
        # The count cap is a slice, so without hoisting it is POSITIONAL: a
        # config that happens to place the pin after 32 other knobs would lose
        # exactly the entry that says which backend served the turn.
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={
                    "extra_body": {
                        **{f"pad{i}": i for i in range(40)},
                        "provider": {"order": ["Alibaba"]},
                    }
                },
            )
        )
        assert params["extra_body"]["provider"] == {"order": ["Alibaba"]}

    def test_non_str_extra_body_key_degrades_instead_of_costing_the_line(self):
        # json.dumps' default= hook applies to values only, so an unserializable
        # KEY raises out in _emit — where the failure is swallowed and the entire
        # line, cost data included, is dropped. Stringify before emitting.
        #
        # The equality is the load-bearing assertion: `json.dumps(params)` alone
        # passes on `None` too (it returns the truthy string "null"), so it can't
        # tell a graceful degrade from _extract_request_params' "we could not
        # tell" sentinel — which would take temperature/top_p down with it.
        params = cc._extract_request_params(
            _mcd(
                "r",
                optional_params={"temperature": 0.3, "extra_body": {("a", "b"): 1}},
            )
        )
        assert params["temperature"] == 0.3
        assert params["extra_body"] == {"('a', 'b')": 1}
        assert json.dumps(params)

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

    def test_the_cap_is_caller_settable_on_both_branches(self):
        # Priority extra_body entries draw on a wider budget, and the cap has to
        # reach BOTH exits — the raw-string shortcut as well as the JSON
        # round-trip — or a string-valued priority knob silently keeps the
        # tighter cap while a dict-valued one gets the wider one.
        long_str = "z" * (cc._MAX_PARAM_JSON_CHARS + 1)
        assert "chars omitted" in cc._bounded_param(long_str)
        assert cc._bounded_param(long_str, cc._MAX_PRIORITY_PARAM_JSON_CHARS) == long_str
        assert "chars omitted" in cc._bounded_param({"order": long_str})
        assert cc._bounded_param({"order": long_str}, cc._MAX_PRIORITY_PARAM_JSON_CHARS) == {
            "order": long_str
        }

    def test_unserializable_values_degrade_to_repr_not_raise(self):
        # A pydantic/enum-ish value must not take the log line down with it.
        value = {"effort": types.SimpleNamespace(name="high")}
        assert json.dumps(cc._bounded_param(value))

    def test_non_finite_floats_become_markers(self):
        # json.dumps emits the non-standard NaN/Infinity tokens for these,
        # which a downstream `jq 'fromjson?'` silently drops — taking the cost
        # data with it. Map them to a marker so the line stays valid JSON.
        for bad in (float("nan"), float("inf"), float("-inf")):
            bounded = cc._bounded_param(bad)
            assert isinstance(bounded, str)
            assert "non-finite" in bounded
        # A whole line carrying such a param survives as STRICT JSON. allow_nan
        # is what makes this assertion mean anything: json.dumps/json.loads both
        # accept the non-standard NaN token by default, so without it the check
        # passes identically with the guard reverted.
        assert json.dumps({"temperature": cc._bounded_param(float("nan"))}, allow_nan=False)

    def test_nested_non_finite_floats_degrade_rather_than_emit_invalid_json(self):
        # The top-level guard doesn't see these: a -inf logit bias (a real idiom
        # for banning a token) or a NaN inside `stop` reaches the JSON round-trip,
        # which without allow_nan=False would happily emit `-Infinity`/`NaN` and
        # take the whole line out at the `jq 'fromjson?'` on the other end.
        for bad in ({"5": float("-inf")}, [float("nan")], {"weird": {"x": float("inf")}}):
            assert json.dumps(cc._bounded_param(bad), allow_nan=False)

    def test_nested_non_finite_keeps_its_finite_siblings(self):
        # Rejecting the whole value would throw away the other 99 entries of a
        # logit_bias, and `<unserializable>` would conflate "an operator set an
        # insane value" with "we couldn't encode this". Scrub in place instead:
        # the marker keeps the diagnostic, the siblings keep the evidence.
        bounded = cc._bounded_param({"5": float("-inf"), "6": 1.5, "7": -2})
        assert bounded == {"5": "<non-finite: -inf>", "6": 1.5, "7": -2}
        assert json.dumps(bounded, allow_nan=False)

    def test_genuinely_unserializable_still_degrades_to_a_marker(self):
        # The non-finite retry must not swallow the OTHER failure mode: a value
        # that cannot be encoded at all (here a non-str dict key, which
        # `default=` does not cover) still has to become a marker rather than
        # raise out into _emit and cost the whole line.
        assert cc._bounded_param({("a", "b"): float("nan")}) == "<unserializable>"


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
            types.SimpleNamespace(usage=_usage_without_cost()),
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
        cc.LiteLLMCostLogger()._record(
            _mcd("p2"), types.SimpleNamespace(usage=_usage_without_cost())
        )
        payload = emitted[0]
        assert payload["request_params"] is None
        assert payload["call"]["cached_tokens"] == 600
