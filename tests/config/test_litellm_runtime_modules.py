"""Unit tests for the five modules the patch script installs into litellm.

``config/litellm/{openrouter_capabilities,drop_params_visibility,
anthropic_thinking_policy,openrouter_reasoning_roundtrip,
stream_cost_preservation}.py`` are staged by the Dockerfile and copied into
every litellm tree by ``patch_litellm_cache.py``. They are kept as real files
rather than string literals inside the patch script precisely so they can be
linted and tested here — but that only works if they import without litellm
installed, which is why each of them defers its ``litellm`` imports into the
function that needs them. These tests lock in both halves: the behaviour, and
the importability that makes the behaviour testable.

litellm itself is not a project dependency (and 1.86.2 cannot run on the
repo's Python), so the handful of symbols the modules reach for at call time —
``verbose_logger`` and ``HTTPHandler`` — are stubbed into ``sys.modules``.
"""

import ast
import importlib.util
import sys
import types
from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "litellm"


def _load(name: str):
    """Load a staged module from disk under a test-local module name."""
    path = CONFIG_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"egg_staged_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingLogger:
    """Stand-in for litellm's ``verbose_logger``, capturing formatted calls."""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _record(self, level, message, *args):
        self.records.append((level, message % args if args else message))

    def warning(self, message, *args):
        self._record("warning", message, *args)

    def debug(self, message, *args):
        self._record("debug", message, *args)

    def info(self, message, *args):
        self._record("info", message, *args)

    def messages(self, level=None):
        return [text for lvl, text in self.records if level is None or lvl == level]


class _FlakyLogger(_RecordingLogger):
    """Raises on the first ``n`` emits, then records normally.

    Stands in for a logger that is not yet wired up when the first diagnostic
    fires. The modules swallow that failure by design; the question these
    tests ask is whether they also swallow the *signal*.
    """

    def __init__(self, failures=1):
        super().__init__()
        self.remaining = failures

    def warning(self, message, *args):
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("logging subsystem not ready")
        super().warning(message, *args)


def _install_logger(monkeypatch, recorder):
    litellm = sys.modules.get("litellm") or types.ModuleType("litellm")
    logging_mod = types.ModuleType("litellm._logging")
    logging_mod.verbose_logger = recorder
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm._logging", logging_mod)
    return recorder


@pytest.fixture
def logger(monkeypatch):
    """Install a stub ``litellm._logging.verbose_logger``."""
    return _install_logger(monkeypatch, _RecordingLogger())


# --------------------------------------------------------------------------
# openrouter_capabilities
# --------------------------------------------------------------------------


@pytest.fixture
def caps(monkeypatch):
    module = _load("openrouter_capabilities")
    module.reset_cache()
    # Never let a test inherit a stray value from the ambient environment.
    for var in (
        "LITELLM_OPENROUTER_CAPABILITY_FETCH",
        "LITELLM_OPENROUTER_CAPABILITY_TTL",
        "LITELLM_OPENROUTER_CAPABILITY_TIMEOUT",
        "LITELLM_OPENROUTER_PRICING",
    ):
        monkeypatch.delenv(var, raising=False)
    return module


def _install_http_stub(monkeypatch, *, payload=None, status_code=200, boom=None):
    """Stub ``litellm.llms.custom_httpx.http_handler.HTTPHandler``.

    Returns a list that records one entry per constructed handler, so a test
    can assert how many fetches actually happened.
    """
    calls: list[float] = []

    class _Response:
        status_code = None

        def json(self):
            return payload

    class _Handler:
        def __init__(self, timeout=None):
            calls.append(timeout)

        def get(self, url):
            if boom is not None:
                raise boom
            response = _Response()
            response.status_code = status_code
            return response

    handler_mod = types.ModuleType("litellm.llms.custom_httpx.http_handler")
    handler_mod.HTTPHandler = _Handler
    litellm = sys.modules.get("litellm") or types.ModuleType("litellm")
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm.llms", types.ModuleType("litellm.llms"))
    monkeypatch.setitem(
        sys.modules, "litellm.llms.custom_httpx", types.ModuleType("litellm.llms.custom_httpx")
    )
    monkeypatch.setitem(sys.modules, "litellm.llms.custom_httpx.http_handler", handler_mod)
    return calls


# Shapes taken from live `GET /api/v1/models` responses: rates are decimal
# STRINGS in USD per token, `input_cache_write` is absent on plenty of models,
# and a long-context surcharge appears as `pricing.overrides` keyed by an
# arbitrary `min_prompt_tokens` (qwen3-max really does publish 32000 and
# 128000, which is what makes it untranslatable).
_PAYLOAD = {
    "data": [
        {
            "id": "moonshotai/kimi-k3",
            "supported_parameters": ["reasoning", "reasoning_effort"],
            "pricing": {
                "prompt": "0.0000006",
                "completion": "0.0000025",
                "input_cache_read": "0.00000015",
            },
        },
        {
            "id": "poolside/laguna-s-2.1",
            "supported_parameters": ["reasoning"],
            "pricing": {
                "prompt": "0.0000001",
                "completion": "0.0000002",
                "input_cache_read": "0.00000001",
                "input_cache_write": "0.0000005",
            },
        },
        {"id": "qwen/qwen3-max:free", "supported_parameters": ["temperature"]},
        {
            "id": "qwen/qwen3-max",
            "pricing": {
                "prompt": "0.00000078",
                "completion": "0.0000039",
                "overrides": [
                    {"min_prompt_tokens": 32000, "prompt": "0.00000156"},
                    {"min_prompt_tokens": 128000, "prompt": "0.00000195"},
                ],
            },
        },
        {"id": "malformed-no-params"},
        "not-a-dict",
    ]
}


def test_env_float_warns_on_unparseable_value(caps, logger, monkeypatch):
    """A value the operator deliberately set must not vanish in silence."""
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_TIMEOUT", "5s")
    assert caps._env_float("LITELLM_OPENROUTER_CAPABILITY_TIMEOUT", 5.0) == 5.0
    assert any("is not a number" in m for m in logger.messages("warning"))


def test_env_float_warns_on_out_of_range_value(caps, logger, monkeypatch):
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_TIMEOUT", "0")
    assert caps._env_float("LITELLM_OPENROUTER_CAPABILITY_TIMEOUT", 5.0) == 5.0
    assert any("must be > 0" in m for m in logger.messages("warning"))


def test_env_warning_is_deduplicated_not_emitted_per_request(caps, logger, monkeypatch):
    """``_ttl_seconds`` runs on every lookup, ahead of the freshness check.

    Warning unconditionally there trades a silent fallback for one WARNING per
    proxied request forever, burying the log stream egg's per-call cost
    observability reads — the same unbounded-noise failure the fetch-failure
    and drop_params dedups exist to avoid.
    """
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_TTL", "1h")
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    for _ in range(200):
        caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(logger.messages("warning")) == 1

    # A *different* bad value is a different mistake and is still reported.
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_TTL", "-3")
    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(logger.messages("warning")) == 2


def test_env_warning_is_not_lost_to_a_swallowed_emit_failure(caps, monkeypatch):
    """``_log`` never propagates, so recording the dedup key before the emit
    would let one failure on the *first* call suppress the warning forever:
    every later call finds the key already there."""
    recorder = _install_logger(monkeypatch, _FlakyLogger())
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_TTL", "1h")

    assert caps._ttl_seconds() == caps.DEFAULT_TTL_SECONDS
    assert recorder.messages("warning") == [], "first emit raised, and was swallowed"

    assert caps._ttl_seconds() == caps.DEFAULT_TTL_SECONDS
    assert len(recorder.messages("warning")) == 1, "the signal must survive the failure"

    assert caps._ttl_seconds() == caps.DEFAULT_TTL_SECONDS
    assert len(recorder.messages("warning")) == 1, "and dedup still holds once it is out"


def test_fetch_failure_warning_is_not_lost_to_a_swallowed_emit_failure(caps, monkeypatch):
    """Same reasoning as above for the other warn-once latch."""
    recorder = _install_logger(monkeypatch, _FlakyLogger())
    _install_http_stub(monkeypatch, boom=OSError("no route to host"))

    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert recorder.messages("warning") == []
    assert caps._WARNED_FETCH_FAILURE is False, "nothing was emitted, so nothing is latched"

    caps._CACHE = None
    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(recorder.messages("warning")) == 1
    assert caps._WARNED_FETCH_FAILURE is True


def test_ttl_zero_is_accepted_and_disables_caching(caps, logger, monkeypatch):
    """``TTL=0`` is the natural spelling of "always re-fetch", not an error."""
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_TTL", "0")
    assert caps._ttl_seconds() == 0.0
    assert logger.messages("warning") == []

    calls = _install_http_stub(monkeypatch, payload=_PAYLOAD)
    caps.get_supported_parameters("moonshotai/kimi-k3")
    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(calls) == 2, "TTL=0 must re-fetch on every lookup"


def test_ttl_negative_warns_and_falls_back(caps, logger, monkeypatch):
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_TTL", "-1")
    assert caps._ttl_seconds() == caps.DEFAULT_TTL_SECONDS
    assert any("must be >= 0" in m for m in logger.messages("warning"))


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", False),
        ("off", False),
        ("no", False),
        ("FALSE", False),
        ("1", True),
        ("true", True),
        ("On", True),
        ("yes", True),
    ],
)
def test_env_flag(caps, logger, monkeypatch, raw, expected):
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_FETCH", raw)
    assert caps._env_flag("LITELLM_OPENROUTER_CAPABILITY_FETCH", True) is expected
    assert logger.messages("warning") == [], "a recognised spelling is not a complaint"


@pytest.mark.parametrize("raw", ["disabled", "n", "off ish", "2"])
def test_env_flag_warns_rather_than_inverting_a_near_miss(caps, logger, monkeypatch, raw):
    """``not in _FALSY`` read every unrecognized value as *enable*, so a
    near-miss disable spelling did not fall back to the default — it inverted
    the operator's instruction, silently. The default here is True, so the
    observable behaviour is unchanged; what must not be silent is the typo."""
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_FETCH", raw)
    assert caps._env_flag("LITELLM_OPENROUTER_CAPABILITY_FETCH", True) is True
    assert caps._env_flag("LITELLM_OPENROUTER_CAPABILITY_FETCH", False) is False, (
        "an unrecognized value takes the caller's default, not a guess"
    )
    (message,) = logger.messages("warning")
    assert "is not a boolean" in message
    assert raw in message

    # Dedup: this is read on every lookup, so an unconditional warning would be
    # one WARNING line per proxied request forever.
    caps._env_flag("LITELLM_OPENROUTER_CAPABILITY_FETCH", True)
    assert len(logger.messages("warning")) == 1


def test_candidate_slugs_covers_prefix_and_variant_spellings(caps):
    assert caps._candidate_slugs("openrouter/qwen/qwen3-max:free") == [
        "openrouter/qwen/qwen3-max:free",
        "qwen/qwen3-max:free",
        "openrouter/qwen/qwen3-max",
        "qwen/qwen3-max",
    ]


def test_lookup_reads_advertised_parameters(caps, logger, monkeypatch):
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    assert caps.get_supported_parameters("openrouter/moonshotai/kimi-k3") == {
        "reasoning",
        "reasoning_effort",
    }
    # Advertising `reasoning` but not `reasoning_effort` is a real answer, and
    # the caller (Patch 7) must be able to tell the two apart.
    assert caps.get_supported_parameters("poolside/laguna-s-2.1") == {"reasoning"}


def test_unknown_slug_is_no_opinion_not_a_denial(caps, monkeypatch):
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    assert caps.get_supported_parameters("some/model-the-api-never-heard-of") is None


def test_malformed_entries_are_skipped_not_fatal(caps, monkeypatch):
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    assert caps.get_supported_parameters("malformed-no-params") is None
    assert caps.get_supported_parameters("qwen/qwen3-max:free") == {"temperature"}


def test_returned_set_is_a_copy(caps, monkeypatch):
    """The cache is process-wide; a caller's ``add`` must not poison it."""
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    first = caps.get_supported_parameters("moonshotai/kimi-k3")
    first.add("invented_param")
    assert "invented_param" not in caps.get_supported_parameters("moonshotai/kimi-k3")


def test_result_is_cached_within_the_ttl(caps, monkeypatch):
    calls = _install_http_stub(monkeypatch, payload=_PAYLOAD)
    for _ in range(5):
        caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(calls) == 1


def test_failed_fetch_is_negatively_cached(caps, logger, monkeypatch):
    """An offline deployment costs one attempt per TTL, not one per request."""
    calls = _install_http_stub(monkeypatch, boom=OSError("no route to host"))
    for _ in range(5):
        assert caps.get_supported_parameters("moonshotai/kimi-k3") is None
    assert len(calls) == 1


def test_first_fetch_failure_warns_and_repeats_go_to_debug(caps, logger, monkeypatch):
    """INFO is litellm's default level, so a debug-only line is invisible in
    exactly the case where behaviour silently reverts to the model-cost map."""
    _install_http_stub(monkeypatch, boom=OSError("no route to host"))
    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(logger.messages("warning")) == 1
    assert len(logger.messages("debug")) == 0

    caps.reset_cache()
    # reset_cache clears the warned flag, so re-arm it explicitly to exercise
    # the repeat path.
    caps._WARNED_FETCH_FAILURE = True
    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(logger.messages("warning")) == 1
    assert len(logger.messages("debug")) == 1


def test_successful_fetch_rearms_the_failure_warning(caps, logger, monkeypatch):
    """Latching the flag for the process lifetime would let one blip at pod
    startup permanently demote every later outage to debug — the exact silence
    the warning exists to break."""
    _install_http_stub(monkeypatch, boom=OSError("no route to host"))
    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(logger.messages("warning")) == 1

    # Endpoint recovers...
    caps._CACHE = None
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    assert caps.get_supported_parameters("moonshotai/kimi-k3") is not None
    assert caps._WARNED_FETCH_FAILURE is False

    # ...and a genuinely new outage later is visible again.
    caps._CACHE = None
    _install_http_stub(monkeypatch, boom=OSError("no route to host"))
    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(logger.messages("warning")) == 2


def test_non_200_is_no_opinion(caps, logger, monkeypatch):
    _install_http_stub(monkeypatch, payload=_PAYLOAD, status_code=503)
    assert caps.get_supported_parameters("moonshotai/kimi-k3") is None
    assert any("HTTP 503" in m for m in logger.messages("warning"))


def test_payload_without_data_list_is_no_opinion(caps, logger, monkeypatch):
    _install_http_stub(monkeypatch, payload={"error": "nope"})
    assert caps.get_supported_parameters("moonshotai/kimi-k3") is None


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": ["not-a-dict", {"id": 5}, {"id": "x", "supported_parameters": "nope"}]},
    ],
    ids=["empty-list", "every-entry-malformed"],
)
def test_a_200_that_parses_to_nothing_is_reported(caps, logger, monkeypatch, payload):
    """Otherwise an OpenRouter schema change is indistinguishable from a fetch
    that simply had no opinion, and the module goes back to the model-cost map
    with nothing said."""
    _install_http_stub(monkeypatch, payload=payload)
    assert caps.get_supported_parameters("moonshotai/kimi-k3") is None
    assert any("no usable entries" in m for m in logger.messages("warning"))


def test_a_200_that_parses_to_nothing_does_not_rearm_the_failure_warning(caps, logger, monkeypatch):
    """Re-arming there would report a recovery that did not happen: the point
    of the flag is that the *first* line of a real outage is visible."""
    _install_http_stub(monkeypatch, boom=OSError("no route to host"))
    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert caps._WARNED_FETCH_FAILURE is True
    assert len(logger.messages("warning")) == 1

    caps._CACHE = None
    _install_http_stub(monkeypatch, payload={"data": []})
    assert caps.get_supported_parameters("moonshotai/kimi-k3") is None
    assert caps._WARNED_FETCH_FAILURE is True, "an empty parse is not a recovery"
    assert len(logger.messages("warning")) == 1, "and it is a repeat, so it goes to debug"
    assert any("no usable entries" in m for m in logger.messages("debug"))


def test_fetch_disabled_skips_the_network_entirely(caps, monkeypatch):
    """The kill switch must not merely discard the answer — it must not ask."""
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_FETCH", "0")
    calls = _install_http_stub(monkeypatch, payload=_PAYLOAD)
    assert caps.get_supported_parameters("moonshotai/kimi-k3") is None
    assert calls == []


def test_concurrent_refresh_serves_stale_rather_than_queueing(caps, monkeypatch):
    """A thread that finds the refresh lock held must not block on HTTP.

    httpx timeouts are per-phase, not total, so queueing behind someone else's
    fetch puts unbounded latency on a live request once per TTL.
    """
    calls = _install_http_stub(monkeypatch, payload=_PAYLOAD)
    caps.get_supported_parameters("moonshotai/kimi-k3")
    assert len(calls) == 1

    # Force the cache stale, then hold the lock as a competing refresher would.
    caps._CACHE_STAMP -= caps.DEFAULT_TTL_SECONDS * 2
    caps._LOCK.acquire()
    try:
        assert caps.get_supported_parameters("moonshotai/kimi-k3") == {
            "reasoning",
            "reasoning_effort",
        }
    finally:
        caps._LOCK.release()
    assert len(calls) == 1, "stale read must not have triggered a second fetch"


def test_module_imports_without_litellm(monkeypatch):
    """Regression: a module-scope ``from litellm...`` import made this file
    unimportable in the repo, which is what left it untested."""
    for name in [n for n in sys.modules if n == "litellm" or n.startswith("litellm.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    assert _load("openrouter_capabilities") is not None
    assert _load("drop_params_visibility") is not None
    assert _load("anthropic_thinking_policy") is not None
    assert _load("openrouter_reasoning_roundtrip") is not None
    assert _load("stream_cost_preservation") is not None


# --------------------------------------------------------------------------
# openrouter_capabilities — pricing half (#3691)
# --------------------------------------------------------------------------


def test_pricing_translates_the_published_rate_card(caps, monkeypatch):
    """The whole point: a slug LiteLLM's bundled map has never heard of gets a
    usable model-cost entry, so ``cost_estimated`` stops reading null."""
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    entry = caps.get_model_cost_entry("openrouter/poolside/laguna-s-2.1")
    assert entry == {
        "key": "poolside/laguna-s-2.1",
        "litellm_provider": "openrouter",
        "mode": "chat",
        "input_cost_per_token": 1e-07,
        "output_cost_per_token": 2e-07,
        "cache_read_input_token_cost": 1e-08,
        # OpenRouter's `input_cache_write` is LiteLLM's `cache_creation_*`.
        # Swapping the pair would price cache writes at the read rate — a ~5x
        # understatement of the most expensive turn in a session.
        "cache_creation_input_token_cost": 5e-07,
    }


def test_pricing_carries_cost_fields_only(caps, monkeypatch):
    """Capabilities must not arrive through the pricing door.

    ``supports_reasoning: true`` alone makes stock
    ``get_supported_openai_params`` admit ``thinking``, which Patch 2's notes
    explain would forward an Anthropic-shaped block to a provider expecting
    ``reasoning``. Patch 7 stays the only path by which a param is admitted.
    """
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    entry = caps.get_model_cost_entry("moonshotai/kimi-k3")
    assert entry is not None
    assert not [k for k in entry if k.startswith("supports_")]
    assert not [k for k in entry if "tokens" in k], "no context lengths either"


def test_pricing_omits_a_rate_the_provider_did_not_publish(caps, monkeypatch):
    """A missing cache-write rate degrades the estimate; inventing one (or
    reusing the read rate for it) would misprice the turns that cost most."""
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    entry = caps.get_model_cost_entry("moonshotai/kimi-k3")
    assert "cache_read_input_token_cost" in entry
    assert "cache_creation_input_token_cost" not in entry


def test_tiered_rate_card_is_declined_rather_than_under_reported(caps, logger, monkeypatch):
    """qwen3-max prices by prompt length at boundaries LiteLLM cannot express.

    Registering the base tier anyway would under-report by 2-2.5x on exactly
    the long-prompt turns agent traffic is made of, silently, under a field an
    operator would use to choose a model. Null is the honest answer, and the
    reason has to be findable — so it is a warning, and it names the tiers.
    """
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    assert caps.get_model_cost_entry("openrouter/qwen/qwen3-max") is None

    (message,) = logger.messages("warning")
    assert "qwen/qwen3-max" in message
    assert "32000, 128000" in message

    # Once per slug: this runs on the per-call cost path, so repeating it would
    # bury the log stream the cost figures themselves land in.
    for _ in range(50):
        caps.get_model_cost_entry("openrouter/qwen/qwen3-max")
    assert len(logger.messages("warning")) == 1


def _tiered_payload(model_id, override, base=None):
    pricing = dict(base or {"prompt": "0.000003", "completion": "0.000015"})
    pricing["overrides"] = [override] if isinstance(override, dict) else list(override)
    return {"data": [{"id": model_id, "pricing": pricing}]}


def test_a_tier_landing_on_a_litellm_slot_is_translated_not_declined(caps, logger, monkeypatch):
    """Declining every tiered card left most of them unpriced for no reason.

    LiteLLM has real rate slots at 128000/200000/272000, and ``x-ai/grok-4.5``
    and friends publish a single boundary that lands exactly on one. Declining
    those cost ``cost_estimated`` on ~7% of the live roster — including several
    likely routing targets — while the warning told the operator LiteLLM could
    not express a boundary it can.
    """
    _install_http_stub(
        monkeypatch,
        payload=_tiered_payload(
            "x-ai/grok-4.5",
            {
                "min_prompt_tokens": 200000,
                "prompt": "0.000004",
                "completion": "0.000012",
                "input_cache_read": "0.0000006",
            },
            base={
                "prompt": "0.000002",
                "completion": "0.000006",
                "input_cache_read": "0.0000003",
            },
        ),
    )
    assert caps.get_model_cost_entry("x-ai/grok-4.5") == {
        "key": "x-ai/grok-4.5",
        "litellm_provider": "openrouter",
        "mode": "chat",
        "input_cost_per_token": 2e-06,
        "output_cost_per_token": 6e-06,
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token_above_200k_tokens": 4e-06,
        "output_cost_per_token_above_200k_tokens": 1.2e-05,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
    }
    assert logger.messages("warning") == [], "nothing was declined, so nothing to explain"


def test_a_tier_is_declined_when_one_published_component_has_no_slot(caps, logger, monkeypatch):
    """Slot coverage is uneven *per component*, not just per boundary.

    ``openai/gpt-5.6-luna-pro`` really does surcharge ``input_cache_write`` at
    272000, and LiteLLM has no ``cache_creation_input_token_cost_above_272k_tokens``
    — the field is absent from the explicit enumeration ``_get_model_info_helper``
    builds its return from, so a key by that name is dropped on the way out.
    Emitting the three components that *do* fit would leave cache writes billed
    at the base rate above the boundary: a silent under-report on exactly the
    long-prompt turns the surcharge exists for, which is the failure the
    all-or-nothing rule exists to prevent.
    """
    _install_http_stub(
        monkeypatch,
        payload=_tiered_payload(
            "openai/gpt-5.6-luna-pro",
            {
                "min_prompt_tokens": 272000,
                "prompt": "0.000001",
                "completion": "0.0000045",
                "input_cache_read": "0.0000001",
                "input_cache_write": "0.00000125",
            },
        ),
    )
    assert caps.get_model_cost_entry("openai/gpt-5.6-luna-pro") is None
    (message,) = logger.messages("warning")
    assert "272000" in message
    # The old wording asserted LiteLLM "cannot express those boundaries", which
    # is now false for most cards and would send an operator after the wrong
    # limit. 272000 *is* an expressible boundary; the component is not.
    assert "cannot express those boundaries" not in message


def test_a_tier_publishing_only_a_component_we_never_price_is_still_translated(caps, monkeypatch):
    """An unmapped component is out of scope at the base tier too.

    ``image`` has no per-token slot LiteLLM's chat-path calculation reads, so it
    is already absent from the base entry. Declining the whole card over it
    would withhold a rate card that is no less complete above the boundary than
    below it — the all-or-nothing rule is about components we *do* price.
    """
    _install_http_stub(
        monkeypatch,
        payload=_tiered_payload(
            "some/multimodal",
            {"min_prompt_tokens": 128000, "prompt": "0.000006", "image": "0.003"},
        ),
    )
    entry = caps.get_model_cost_entry("some/multimodal")
    assert entry["input_cost_per_token_above_128k_tokens"] == 6e-06
    assert not [k for k in entry if "image" in k]


def test_a_tier_with_no_prompt_surcharge_is_declined_as_unreachable(caps, monkeypatch):
    """LiteLLM finds the applicable boundary by scanning for
    ``input_cost_per_token_above_*`` keys, so a tier publishing only a
    completion surcharge would contribute a key nothing ever reads — and bill
    the whole turn at base while looking translated."""
    _install_http_stub(
        monkeypatch,
        payload=_tiered_payload(
            "some/completion-only",
            {"min_prompt_tokens": 200000, "completion": "0.00003"},
        ),
    )
    assert caps.get_model_cost_entry("some/completion-only") is None


def test_a_string_tier_boundary_is_parsed_like_every_other_number(caps, monkeypatch):
    """``pricing.prompt`` right beside it arrives as a decimal string, so
    OpenRouter demonstrably does serialize numbers as strings in this block. An
    ``isinstance(raw, int)`` gate would report a perfectly expressible boundary
    as "unparseable" the day ``min_prompt_tokens`` follows suit."""
    _install_http_stub(
        monkeypatch,
        payload=_tiered_payload(
            "some/stringy",
            {"min_prompt_tokens": "272000", "prompt": "0.00001", "completion": "0.000045"},
        ),
    )
    entry = caps.get_model_cost_entry("some/stringy")
    assert entry["input_cost_per_token_above_272k_tokens"] == 1e-05
    assert entry["output_cost_per_token_above_272k_tokens"] == 4.5e-05


@pytest.mark.parametrize("boundary", [32000, 256000, 128000.5, True, None])
def test_an_inexpressible_boundary_is_still_declined(caps, monkeypatch, boundary):
    """The narrowing is to the boundaries LiteLLM actually has slots for, not
    to "anything that looks like a number". A fractional token count means the
    field is not what this thinks it is; a bool is not a count at all."""
    _install_http_stub(
        monkeypatch,
        payload=_tiered_payload(
            "some/arbitrary",
            {"min_prompt_tokens": boundary, "prompt": "0.000006", "completion": "0.00003"},
        ),
    )
    assert caps.get_model_cost_entry("some/arbitrary") is None


def test_the_1h_cache_write_and_reasoning_rates_are_carried(caps, monkeypatch):
    """Two more components with an exact LiteLLM destination that is read on
    the chat cost path: ``generic_cost_per_token`` prices reasoning tokens off
    ``output_cost_per_reasoning_token``, and ``calculate_cache_writing_cost``
    prices a 1h TTL block off ``cache_creation_input_token_cost_above_1hr``.

    ``input_cache_write_1h`` is 2x the 5m rate on every Anthropic route. Claude
    Code defaults to 5m so it does not fire today — but dropping it meant that
    the moment anything sets ``ttl: "1h"``, cache writes get priced at half of
    actual with no signal, which is the same under-report this module declines
    a whole rate card to avoid.
    """
    _install_http_stub(
        monkeypatch,
        payload={
            "data": [
                {
                    "id": "anthropic/claude-opus-4.8",
                    "pricing": {
                        "prompt": "0.000005",
                        "completion": "0.000025",
                        "input_cache_write": "0.00000625",
                        "input_cache_write_1h": "0.00001",
                        "internal_reasoning": "0.000025",
                        # Published, but per-request rather than per-token, and
                        # LiteLLM's chat cost path never reads a query rate.
                        "web_search": "0.01",
                    },
                }
            ]
        },
    )
    entry = caps.get_model_cost_entry("anthropic/claude-opus-4.8")
    assert entry["cache_creation_input_token_cost"] == 6.25e-06
    assert entry["cache_creation_input_token_cost_above_1hr"] == 1e-05
    assert entry["output_cost_per_reasoning_token"] == 2.5e-05
    assert "input_cost_per_query" not in entry


def test_a_pricing_only_entry_does_not_shadow_a_parameter_answer(caps, monkeypatch):
    """The candidate loop must skip a record that cannot answer the asked half.

    Before the pricing half existed, ``_fetch`` dropped entries with no
    ``supported_parameters`` outright and the loop fell through them by
    accident. Keeping them for their rates turned that accident into a silent
    regression of Patch 7: ``reasoning_effort`` dropped for a variant slug
    whose roster entry happens to carry only a rate card.
    """
    _install_http_stub(
        monkeypatch,
        payload={
            "data": [
                {
                    "id": "some/model:beta",
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                },
                {"id": "some/model", "supported_parameters": ["reasoning_effort"]},
            ]
        },
    )
    assert caps.get_supported_parameters("some/model:beta") == {"reasoning_effort"}
    # ...and the reverse direction still resolves against the variant itself.
    assert caps.get_model_cost_entry("some/model:beta")["key"] == "some/model:beta"


def test_a_variant_slug_never_inherits_its_base_models_rate_card(caps, monkeypatch):
    """Union-only is safe for parameters and wrong for pricing.

    A variant accepts at least what its base does, so inheriting a parameter
    list can admit a param and never withdraw one. A variant suffix is
    frequently *what changes the rate* — ``:free`` is 0 against a paid base,
    ``:batch`` is half — so inheriting would report a confident, authoritative
    number that is wrong by construction. OpenRouter retires ``:free`` variants
    routinely, which is exactly when the fallback would have fired.
    """
    _install_http_stub(
        monkeypatch,
        payload={
            "data": [
                {
                    "id": "poolside/laguna-s-2.1",
                    "supported_parameters": ["reasoning"],
                    "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
                }
            ]
        },
    )
    assert caps.get_model_cost_entry("poolside/laguna-s-2.1:free") is None
    # The parameter half keeps the fallback, and keeps it deliberately.
    assert caps.get_supported_parameters("poolside/laguna-s-2.1:free") == {"reasoning"}


def test_the_decline_latch_is_rearmed_by_a_refetch_not_only_by_reset_cache(caps, monkeypatch):
    """``reset_cache`` is test-only, so a latch cleared solely there is latched
    for the life of the pod — the exact outcome the comment beside it claimed
    to prevent. A model that acquires or drops a surcharge tier has to be
    reported against the roster the change was read from."""
    _install_http_stub(
        monkeypatch,
        payload=_tiered_payload("some/tiered", {"min_prompt_tokens": 32000, "prompt": "0.000006"}),
    )
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_TTL", "0")
    recorder = _install_logger(monkeypatch, _RecordingLogger())

    assert caps.get_model_cost_entry("some/tiered") is None
    assert len(recorder.messages("warning")) == 1
    assert caps.get_model_cost_entry("some/tiered") is None
    assert len(recorder.messages("warning")) == 2, "each refetch re-arms the per-slug latch"


def test_a_tiered_card_with_unparseable_boundaries_is_still_explained(caps, logger, monkeypatch):
    """A tiered card whose boundaries do not parse is still a tiered card.

    Collapsing "declined for tiering" into the truthiness of the threshold
    tuple would leave exactly these models unpriced AND unexplained — the one
    combination an operator cannot debug.
    """
    _install_http_stub(
        monkeypatch,
        payload={
            "data": [
                {
                    "id": "some/tiered",
                    "pricing": {
                        "prompt": "0.000001",
                        "completion": "0.000002",
                        "overrides": [{"min_prompt_tokens": "128k"}],
                    },
                }
            ]
        },
    )
    assert caps.get_model_cost_entry("some/tiered") is None
    (message,) = logger.messages("warning")
    assert "some/tiered" in message
    assert "unparseable" in message


def test_declined_pricing_warning_is_not_lost_to_a_swallowed_emit_failure(caps, monkeypatch):
    """Same latch discipline as the module's other three warn-once sites."""
    recorder = _install_logger(monkeypatch, _FlakyLogger())
    _install_http_stub(monkeypatch, payload=_PAYLOAD)

    assert caps.get_model_cost_entry("qwen/qwen3-max") is None
    assert recorder.messages("warning") == [], "first emit raised, and was swallowed"
    assert caps._WARNED_DECLINED_PRICING == set(), "nothing emitted, so nothing latched"

    assert caps.get_model_cost_entry("qwen/qwen3-max") is None
    assert len(recorder.messages("warning")) == 1, "the signal must survive the failure"

    assert caps.get_model_cost_entry("qwen/qwen3-max") is None
    assert len(recorder.messages("warning")) == 1, "and dedup still holds once it is out"


def test_pricing_answers_for_openrouter_only(caps, logger, monkeypatch):
    """The call site is a generic lookup every provider reaches. Answering for
    a same-named slug on another provider would attach OpenRouter's rate card
    to somebody else's bill."""
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    assert caps.get_model_cost_entry("poolside/laguna-s-2.1", "bedrock") is None
    assert caps.get_model_cost_entry("poolside/laguna-s-2.1", "openrouter") is not None
    # None is "the caller could not attribute it", which a bare slug legitimately is.
    assert caps.get_model_cost_entry("poolside/laguna-s-2.1", None) is not None


def test_unknown_slug_has_no_price_opinion(caps, monkeypatch):
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    assert caps.get_model_cost_entry("some/model-the-api-never-heard-of") is None


def test_entry_without_pricing_is_still_a_parameter_answer(caps, monkeypatch):
    """The two halves are independent: a roster entry carrying one and not the
    other is a real answer for the half it has."""
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    assert caps.get_supported_parameters("qwen/qwen3-max:free") == {"temperature"}
    assert caps.get_model_cost_entry("qwen/qwen3-max:free") is None
    # ...and the reverse: qwen3-max publishes pricing but no parameter list.
    assert caps.get_supported_parameters("qwen/qwen3-max") is None


def test_returned_cost_entry_is_a_copy(caps, monkeypatch):
    """The cache is process-wide and LiteLLM's model-info path mutates what it
    is handed."""
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    first = caps.get_model_cost_entry("poolside/laguna-s-2.1")
    first["input_cost_per_token"] = 999.0
    assert caps.get_model_cost_entry("poolside/laguna-s-2.1")["input_cost_per_token"] == 1e-07


def test_pricing_can_be_disabled_without_disabling_capabilities(caps, monkeypatch):
    """For an operator who wants LiteLLM's bundled map to be the sole authority
    on cost while keeping Patch 7's parameter fix."""
    _install_http_stub(monkeypatch, payload=_PAYLOAD)
    monkeypatch.setenv("LITELLM_OPENROUTER_PRICING", "0")
    assert caps.get_model_cost_entry("poolside/laguna-s-2.1") is None
    assert caps.get_supported_parameters("poolside/laguna-s-2.1") == {"reasoning"}


def test_fetch_disabled_disables_pricing_too(caps, monkeypatch):
    """One fetch serves both halves, so the master switch governs both."""
    calls = _install_http_stub(monkeypatch, payload=_PAYLOAD)
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_FETCH", "0")
    assert caps.get_model_cost_entry("poolside/laguna-s-2.1") is None
    assert calls == [], "no network call may be made when the lookup is off"


@pytest.mark.parametrize(
    "pricing",
    [
        {"completion": "0.000002"},  # no prompt rate
        {"prompt": "0.000001"},  # no completion rate
        {"prompt": "not-a-number", "completion": "0.000002"},
        {"prompt": "-0.000001", "completion": "0.000002"},
        {"prompt": "Infinity", "completion": "0.000002"},
        "not-a-dict",
    ],
)
def test_unusable_rate_card_is_no_opinion(caps, monkeypatch, pricing):
    """Without both the prompt and completion rate the entry cannot price a
    chat turn at all, and a negative or non-finite rate is not a rate — an
    ``inf`` would propagate into egg's session total and into the emitted JSON
    as a token that makes the log line unparseable."""
    _install_http_stub(monkeypatch, payload={"data": [{"id": "some/model", "pricing": pricing}]})
    assert caps.get_model_cost_entry("some/model") is None


def test_zero_is_a_real_rate(caps, monkeypatch):
    """A ``:free`` variant really is priced at zero. Filtering the resulting
    zero estimate is ``cost_callback``'s job, not this module's."""
    _install_http_stub(
        monkeypatch,
        payload={
            "data": [{"id": "some/model:free", "pricing": {"prompt": "0", "completion": "0"}}]
        },
    )
    entry = caps.get_model_cost_entry("some/model:free")
    assert entry["input_cost_per_token"] == 0.0
    assert entry["output_cost_per_token"] == 0.0


# --------------------------------------------------------------------------
# drop_params_visibility
# --------------------------------------------------------------------------


@pytest.fixture
def dropwarn():
    module = _load("drop_params_visibility")
    module._SEEN.clear()
    return module


def test_warns_once_per_provider_model_paramset(dropwarn, logger):
    for _ in range(4):
        dropwarn.warn_dropped_params(
            {"reasoning_effort": "high"}, "kimi-k3", custom_llm_provider="openrouter"
        )
    assert len(logger.messages("warning")) == 1
    dropwarn.warn_dropped_params({"temperature": 0.2}, "kimi-k3", custom_llm_provider="openrouter")
    assert len(logger.messages("warning")) == 2


def test_no_dropped_params_is_silent(dropwarn, logger):
    dropwarn.warn_dropped_params({}, "kimi-k3", custom_llm_provider="openrouter")
    assert logger.messages() == []


def test_message_names_the_params_and_does_not_only_prescribe_a_config_edit(dropwarn, logger):
    """The param most often dropped here is one litellm synthesized from the
    request, so an unconditional "edit config.yaml" remedy sends the operator
    looking for a line that does not exist."""
    dropwarn.warn_dropped_params(
        {"reasoning_effort": "high"}, "laguna-s-2.1", custom_llm_provider="openrouter"
    )
    (message,) = logger.messages("warning")
    assert "reasoning_effort" in message
    assert "laguna-s-2.1" in message
    assert "openrouter" in message
    assert "If they came from" in message
    assert "synthesized from the request" in message


def test_seen_set_is_bounded_and_keeps_deduplicating(dropwarn, logger, monkeypatch):
    """Freezing a full set would stop dedup and re-warn on every request; the
    cap must clear instead, keeping both memory and log volume bounded."""
    monkeypatch.setattr(dropwarn, "_MAX_WARNINGS", 3)
    for i in range(10):
        dropwarn.warn_dropped_params({f"param_{i}": 1}, "m", custom_llm_provider="openrouter")
    assert len(dropwarn._SEEN) <= 3
    assert len(logger.messages("warning")) == 10

    # The key just recorded is still deduplicated — we did not go chatty.
    before = len(logger.messages("warning"))
    dropwarn.warn_dropped_params({"param_9": 1}, "m", custom_llm_provider="openrouter")
    assert len(logger.messages("warning")) == before


def test_drop_warning_is_not_lost_to_a_swallowed_emit_failure(dropwarn, monkeypatch):
    """The third of this changeset's three warn-once latches, held to the same
    rule as the other two.

    ``warn_dropped_params`` swallows a logging failure by design, so recording
    the dedup key before the emit would let one failure on the *first* call mute
    that route for the life of the process — in the one module whose entire
    purpose is to stop a drop being silent."""
    recorder = _install_logger(monkeypatch, _FlakyLogger())

    dropwarn.warn_dropped_params(
        {"reasoning_effort": "high"}, "kimi-k3", custom_llm_provider="openrouter"
    )
    assert recorder.messages("warning") == [], "first emit raised, and was swallowed"
    assert dropwarn._SEEN == set(), "nothing was emitted, so nothing is deduplicated"

    dropwarn.warn_dropped_params(
        {"reasoning_effort": "high"}, "kimi-k3", custom_llm_provider="openrouter"
    )
    assert len(recorder.messages("warning")) == 1, "the signal must survive the failure"

    dropwarn.warn_dropped_params(
        {"reasoning_effort": "high"}, "kimi-k3", custom_llm_provider="openrouter"
    )
    assert len(recorder.messages("warning")) == 1, "and dedup still holds once it is out"


def test_diagnostic_never_raises(dropwarn, logger):
    """A logging failure must not be able to fail a request."""

    class _Explode:
        def keys(self):
            raise RuntimeError("boom")

        def __bool__(self):
            return True

    dropwarn.warn_dropped_params(_Explode(), "m", custom_llm_provider="openrouter")


# --------------------------------------------------------------------------
# anthropic_thinking_policy
# --------------------------------------------------------------------------


@pytest.fixture
def policy(monkeypatch):
    module = _load("anthropic_thinking_policy")
    monkeypatch.delenv(module.ENV_VAR, raising=False)
    return module


def test_synthesis_is_off_by_default(policy):
    """Patch 9's whole point: the adapter's bucketed reasoning_effort is a cap
    below the model default on every model egg routes (kimi-k3: 3130 reasoning
    tokens with no param vs 340 with ``high``)."""
    assert policy.should_synthesize_reasoning_effort() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_synthesis_opt_in(policy, monkeypatch, raw):
    monkeypatch.setenv(policy.ENV_VAR, raw)
    assert policy.should_synthesize_reasoning_effort() is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no", "", "maybe"])
def test_synthesis_stays_off_for_anything_else(policy, monkeypatch, raw):
    monkeypatch.setenv(policy.ENV_VAR, raw)
    assert policy.should_synthesize_reasoning_effort() is False


@pytest.mark.parametrize("raw", ["0", "false", "OFF", "no", ""])
def test_recognised_off_spellings_do_not_complain(policy, logger, monkeypatch, raw):
    monkeypatch.setenv(policy.ENV_VAR, raw)
    assert policy.should_synthesize_reasoning_effort() is False
    assert logger.messages("warning") == []


@pytest.mark.parametrize("raw", ["enabled", "y", "maybe", "2"])
def test_unrecognised_value_warns_once(policy, logger, monkeypatch, raw):
    """False is also the default, so without a warning an operator who typed
    ``=enabled`` cannot tell "ignored" from "working as configured" — on the
    knob with the ~9x measured effect on reasoning depth."""
    monkeypatch.setenv(policy.ENV_VAR, raw)
    assert policy.should_synthesize_reasoning_effort() is False
    (message,) = logger.messages("warning")
    assert policy.ENV_VAR in message
    assert raw in message

    # Read once per translated request, so the complaint must not repeat.
    for _ in range(4):
        policy.should_synthesize_reasoning_effort()
    assert len(logger.messages("warning")) == 1


def test_unrecognised_value_warning_survives_a_swallowed_emit_failure(policy, monkeypatch):
    """Same latch discipline as the other three warn-once sites: the key is
    recorded only once the emit did not raise, so a logger that is not yet in
    place on the first request cannot mute the complaint permanently."""
    recorder = _install_logger(monkeypatch, _FlakyLogger())
    monkeypatch.setenv(policy.ENV_VAR, "enabled")

    assert policy.should_synthesize_reasoning_effort() is False
    assert recorder.messages("warning") == [], "first emit raised, and was swallowed"
    assert policy._WARNED_VALUES == set(), "nothing was emitted, so nothing is deduplicated"

    assert policy.should_synthesize_reasoning_effort() is False
    assert len(recorder.messages("warning")) == 1, "the signal must survive the failure"

    assert policy.should_synthesize_reasoning_effort() is False
    assert len(recorder.messages("warning")) == 1, "and dedup still holds once it is out"


# --- Module 4: openrouter reasoning round-trip -----------------------------
#
# The adapter parks prior-turn assistant reasoning on ``thinking_blocks``, a
# field no OpenRouter request-path code reads. These lock in the mapping onto
# ``reasoning_content`` and, just as importantly, the cases that must NOT be
# rewritten: a request has to survive a block this never anticipated.


@pytest.fixture
def roundtrip(monkeypatch):
    module = _load("openrouter_reasoning_roundtrip")
    module._WARNED.clear()
    # Never let a test inherit a stray value from the ambient environment.
    monkeypatch.delenv(module.ENV_VAR, raising=False)
    return module


def _thinking(text, signature="sig"):
    return {"type": "thinking", "thinking": text, "signature": signature}


def test_assistant_thinking_becomes_reasoning_content(roundtrip):
    """The whole point: the field the adapter writes reaches the field
    OpenRouter reads, and the one it does not read stops being transmitted."""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "answer",
            "thinking_blocks": [_thinking("because X")],
        },
    ]

    out = roundtrip.map_thinking_blocks_to_reasoning_content(messages)

    assert out[1]["reasoning_content"] == "because X"
    assert "thinking_blocks" not in out[1], "no unknown field may reach the provider"
    assert out[1]["content"] == "answer", "the rest of the message is untouched"


def test_anthropic_signature_is_not_forwarded(roundtrip):
    """``signature`` is an Anthropic re-verification token; it means nothing to
    OpenRouter, so it must not ride along in any shape."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [{"role": "assistant", "thinking_blocks": [_thinking("t", signature="deadbeef")]}]
    )

    assert out[0]["reasoning_content"] == "t"
    assert "deadbeef" not in repr(out[0])


def test_multiple_blocks_concatenate_in_order(roundtrip):
    """Order is the content: reasoning read back out of sequence is worse than
    no reasoning at all."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [
            {
                "role": "assistant",
                "thinking_blocks": [_thinking("first"), _thinking("second"), _thinking("third")],
            }
        ]
    )

    assert out[0]["reasoning_content"] == "first\nsecond\nthird"


def test_blocks_are_separated_rather_than_run_together(roundtrip):
    """The adapter emits one block per ``thinking`` content block, so adjacent
    blocks are separate thoughts. Joining on "" runs the last word of one into
    the first word of the next — ``...decided.We then...`` — which is exactly
    how a ``<think>`` re-render would read it back."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [{"role": "assistant", "thinking_blocks": [_thinking("decided."), _thinking("We then")]}]
    )

    assert out[0]["reasoning_content"] == "decided.\nWe then"
    assert "decided.We" not in out[0]["reasoning_content"]


def test_a_blank_block_contributes_no_bare_separator(roundtrip):
    """A block with nothing in it must not turn into a leading or doubled
    newline, which would render as an empty thought."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [
            {
                "role": "assistant",
                "thinking_blocks": [_thinking("   "), _thinking("real"), _thinking("")],
            }
        ]
    )

    assert out[0]["reasoning_content"] == "real"


def test_redacted_thinking_is_skipped(roundtrip):
    """A ``redacted_thinking`` block carries opaque ``data``, not text. There is
    nothing to send, and inventing an empty string would put the very
    ``<think></think>`` this patch exists to remove back into the prompt."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [
            {
                "role": "assistant",
                "thinking_blocks": [
                    {"type": "redacted_thinking", "data": "opaque"},
                    _thinking("visible"),
                ],
            }
        ]
    )

    assert out[0]["reasoning_content"] == "visible", "redacted contributes nothing"
    assert "opaque" not in repr(out[0])


def test_only_redacted_blocks_emit_no_field_at_all(roundtrip):
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [{"role": "assistant", "thinking_blocks": [{"type": "redacted_thinking", "data": "d"}]}]
    )

    assert "reasoning_content" not in out[0]
    assert "thinking_blocks" not in out[0], "still stripped: no unknown field on the wire"


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_whitespace_only_reasoning_emits_nothing(roundtrip, text):
    """Empty reasoning is not reasoning. Emitting it renders as an empty
    ``<think></think>`` on templates that re-render prior thinking."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [{"role": "assistant", "thinking_blocks": [_thinking(text)]}]
    )

    assert "reasoning_content" not in out[0]
    assert "thinking_blocks" not in out[0]


@pytest.mark.parametrize("role", ["user", "system", "tool"])
def test_non_assistant_messages_are_never_touched(roundtrip, role):
    """Assistant turns only. A ``thinking_blocks`` key on any other role is not
    ours to reinterpret, so it passes through byte-identical."""
    original = {"role": role, "content": "c", "thinking_blocks": [_thinking("t")]}

    out = roundtrip.map_thinking_blocks_to_reasoning_content([dict(original)])

    assert out[0] == original
    assert "reasoning_content" not in out[0]


def test_assistant_without_thinking_blocks_is_returned_as_is(roundtrip):
    """The key genuinely absent — a message this module never saw the adapter
    build. Contrast ``thinking_blocks: None`` below, which is what the adapter
    actually emits for a turn that produced no thinking."""
    original = {"role": "assistant", "content": "plain"}

    out = roundtrip.map_thinking_blocks_to_reasoning_content([original])

    assert out[0] is original, "no copy, no rewrite, nothing to do"


def test_adapter_none_sentinel_is_stripped_not_treated_as_malformed(roundtrip):
    """``ChatCompletionAssistantMessage`` is a ``TypedDict``, so the adapter's
    ``thinking_blocks=(blocks if len(blocks) > 0 else None)`` leaves the key
    PRESENT with value ``None`` on every assistant turn that reasoned about
    nothing. That is the dominant input on any route — and every turn on a
    route that returns no reasoning at all — so reading it as an unparseable
    shape would ship ``"thinking_blocks": null`` to OpenRouter on the majority
    of turns, exactly the unknown field this patch promises to remove."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [{"role": "assistant", "content": "hi", "thinking_blocks": None}]
    )

    assert "thinking_blocks" not in out[0], "the sentinel must be stripped, not passed through"
    assert "reasoning_content" not in out[0], "no reasoning to emit"
    assert out[0]["content"] == "hi"


def test_none_sentinel_does_not_fire_the_fail_soft_diagnostic(roundtrip, logger):
    """The last-resort branch must stay last-resort. If the commonest message
    shape routes through it, a later log line or counter there fires on ~100%
    of traffic and a genuinely malformed block becomes unfindable."""
    roundtrip.map_thinking_blocks_to_reasoning_content(
        [{"role": "assistant", "content": "hi", "thinking_blocks": None}]
    )

    assert logger.messages() == []


@pytest.mark.parametrize(
    "blocks",
    [
        "not-a-list",
        {"type": "thinking"},
        17,
        [None],
        ["bare string"],
        [{"type": "thinking", "thinking": None}],
        [{"type": "thinking"}],
        [{}],
    ],
)
def test_malformed_blocks_never_raise(roundtrip, blocks):
    """Fail soft. A shape this did not anticipate must cost the reasoning, not
    the request."""
    messages = [{"role": "assistant", "content": "c", "thinking_blocks": blocks}]

    out = roundtrip.map_thinking_blocks_to_reasoning_content(messages)

    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "c"
    assert "reasoning_content" not in out[0], "nothing parseable, so nothing emitted"


def test_a_malformed_sibling_does_not_cost_a_good_block(roundtrip):
    """One bad entry in the list should not discard the reasoning that parsed
    fine beside it."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [
            {
                "role": "assistant",
                "thinking_blocks": [
                    _thinking("kept"),
                    None,
                    {"type": "thinking"},
                    _thinking("too"),
                ],
            }
        ]
    )

    assert out[0]["reasoning_content"] == "kept\ntoo"


def test_unparseable_blocks_field_leaves_the_message_untouched(roundtrip):
    """``thinking_blocks`` that is not a list at all is a shape we do not
    understand; the message goes to the provider exactly as it arrived rather
    than being half-rewritten."""
    original = {"role": "assistant", "content": "c", "thinking_blocks": "not-a-list"}

    out = roundtrip.map_thinking_blocks_to_reasoning_content([dict(original)])

    assert out[0] == original, "untouched, including the field we could not read"


def test_input_messages_are_not_mutated(roundtrip):
    """litellm hands us the caller's list; rewriting it in place would leak
    into logging and retries."""
    block = _thinking("t")
    messages = [{"role": "assistant", "content": "a", "thinking_blocks": [block]}]
    before = [{"role": "assistant", "content": "a", "thinking_blocks": [dict(block)]}]

    roundtrip.map_thinking_blocks_to_reasoning_content(messages)

    assert messages == before


def test_mapping_is_idempotent(roundtrip):
    """Applying twice is a no-op: after the first pass there is no
    ``thinking_blocks`` left to map, and ``reasoning_content`` is preserved."""
    messages = [{"role": "assistant", "content": "a", "thinking_blocks": [_thinking("why")]}]

    once = roundtrip.map_thinking_blocks_to_reasoning_content(messages)
    twice = roundtrip.map_thinking_blocks_to_reasoning_content(once)

    assert twice == once
    assert twice[0]["reasoning_content"] == "why"


def test_non_list_messages_pass_through(roundtrip):
    assert roundtrip.map_thinking_blocks_to_reasoning_content(None) is None
    assert roundtrip.map_thinking_blocks_to_reasoning_content("nope") == "nope"


def test_an_unmappable_shape_says_so_once(roundtrip, logger):
    """Once the ``None`` sentinel is handled, reaching the fail-soft branch
    means a shape nobody anticipated. Silence there is how a mapping that
    quietly stopped working would look — patch 8's whole lesson."""
    messages = [{"role": "assistant", "thinking_blocks": "not-a-list"}] * 5

    roundtrip.map_thinking_blocks_to_reasoning_content(messages)

    warnings = logger.messages("warning")
    assert len(warnings) == 1, "bounded: this sits on the per-request path"
    assert "thinking_blocks" in warnings[0]


def test_diagnostic_is_retried_if_the_first_emit_raises(roundtrip, monkeypatch):
    """Same discipline as the other three modules: record the warning as sent
    only once the emit did not raise, or a logger that is not yet in place on
    the first request suppresses it permanently."""
    flaky = _install_logger(monkeypatch, _FlakyLogger(failures=1))

    for _ in range(3):
        roundtrip.map_thinking_blocks_to_reasoning_content(
            [{"role": "assistant", "thinking_blocks": 17}]
        )

    assert len(flaky.messages("warning")) == 1


# --- signature-verifying providers -----------------------------------------
#
# For an Anthropic or Google model reached THROUGH OpenRouter, the block
# signature is what the upstream verifies when prior thinking is replayed on a
# tool-calling turn, and OpenRouter's docs say to pass those blocks back
# unmodified and unreordered. The plain-string form destroys both, so those
# routes are declined outright rather than half-served.


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-sonnet-4.5",
        "openrouter/anthropic/claude-opus-4",
        "google/gemini-3-pro",
        "OpenRouter/Google/Gemini-3-Pro",
    ],
)
def test_signature_verifying_routes_are_left_exactly_as_they_arrived(roundtrip, model):
    original = {"role": "assistant", "content": "a", "thinking_blocks": [_thinking("why")]}

    out = roundtrip.map_thinking_blocks_to_reasoning_content([dict(original)], model)

    assert out[0] == original, "stock behaviour is the known-working state for these routes"
    assert "reasoning_content" not in out[0]


@pytest.mark.parametrize(
    "model",
    ["deepseek/deepseek-v4-pro", "qwen/qwen3-max", "poolside/laguna-s-2.1", None, 17],
)
def test_ordinary_routes_still_map(roundtrip, model):
    """The gate must not be so broad that it swallows the routes this patch
    exists for. A non-string model is not a slug we can reason about, so it is
    treated as ordinary rather than as a match."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [{"role": "assistant", "thinking_blocks": [_thinking("why")]}], model
    )

    assert out[0]["reasoning_content"] == "why"


# --- a reasoning_content the caller already set ----------------------------


def test_existing_reasoning_content_is_never_overwritten(roundtrip):
    """litellm's own response objects carry both fields, so a client echoing an
    assistant message back sends both. One rule for both branches: what the
    caller stated wins, whether or not the blocks yield text."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [
            {
                "role": "assistant",
                "reasoning_content": "the caller's own",
                "thinking_blocks": [_thinking("from blocks")],
            }
        ]
    )

    assert out[0]["reasoning_content"] == "the caller's own"
    assert "thinking_blocks" not in out[0]


def test_a_blank_existing_reasoning_content_does_not_block_the_mapping(roundtrip):
    """Set means set to something. An empty string is the absence of reasoning
    wearing the field's name, so the blocks still win over it."""
    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [
            {
                "role": "assistant",
                "reasoning_content": "  ",
                "thinking_blocks": [_thinking("from blocks")],
            }
        ]
    )

    assert out[0]["reasoning_content"] == "from blocks"


# --- runtime escape hatch --------------------------------------------------


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_knob_off_restores_stock_behaviour(roundtrip, monkeypatch, value):
    """Patches 7 and 9 are both revertable on a live cluster without an image
    rebuild; this one changes the outgoing body on every OpenRouter call, so it
    carries the same hatch."""
    monkeypatch.setenv(roundtrip.ENV_VAR, value)
    original = {"role": "assistant", "content": "a", "thinking_blocks": [_thinking("why")]}

    out = roundtrip.map_thinking_blocks_to_reasoning_content([dict(original)])

    assert out[0] == original


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_knob_on_is_the_default_spelled_out(roundtrip, monkeypatch, value):
    monkeypatch.setenv(roundtrip.ENV_VAR, value)

    out = roundtrip.map_thinking_blocks_to_reasoning_content(
        [{"role": "assistant", "thinking_blocks": [_thinking("why")]}]
    )

    assert out[0]["reasoning_content"] == "why"


def test_unrecognized_knob_value_warns_once_and_keeps_the_default(roundtrip, monkeypatch, logger):
    """Off is also the default, so reading a near-miss as off would leave an
    operator unable to tell "ignored" from "working as configured"."""
    monkeypatch.setenv(roundtrip.ENV_VAR, "disabled")

    for _ in range(3):
        out = roundtrip.map_thinking_blocks_to_reasoning_content(
            [{"role": "assistant", "thinking_blocks": [_thinking("why")]}]
        )

    assert out[0]["reasoning_content"] == "why", "unrecognized is not off"
    warnings = logger.messages("warning")
    assert len(warnings) == 1
    assert roundtrip.ENV_VAR in warnings[0]


# --------------------------------------------------------------------------
# stream_cost_preservation (#3691)
# --------------------------------------------------------------------------


@pytest.fixture
def streamcost():
    return _load("stream_cost_preservation")


class _Usage:
    """Stand-in for litellm's ``Usage``: attribute access, arbitrary extras.

    ``cost`` is a declared field there and ``cost_details`` survives only as a
    pydantic extra, so both must work through plain ``setattr`` — which is what
    this models. Deliberately NOT a dict: the object the patch amends is the
    one ``calculate_usage`` just rebuilt.
    """

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class _Chunk:
    """Stand-in for ``ModelResponseStream``: mapping-style access over
    attributes, which is how ``ChunkProcessor`` reads a chunk's usage."""

    def __init__(self, usage=None, hidden_usage=None):
        if usage is not None:
            self.usage = usage
        if hidden_usage is not None:
            self._hidden_params = {"usage": hidden_usage}

    def __contains__(self, key):
        return hasattr(self, key)

    def __getitem__(self, key):
        return getattr(self, key)


def test_cost_is_carried_off_the_final_usage_chunk(streamcost):
    """The fix itself: 1252 of 1252 sampled calls reported ``cost: null``
    because this value was dropped between the chunk and the rebuilt usage."""
    chunks = [
        _Chunk(),
        _Chunk(usage=_Usage(prompt_tokens=100, cost=0.00123)),
    ]
    usage = _Usage(prompt_tokens=100, completion_tokens=10)
    streamcost.carry_upstream_cost(chunks, usage)
    assert usage.cost == 0.00123


def test_cost_details_is_carried_too(streamcost):
    """Under BYOK ``cost`` is 0 and the real number lives here, so carrying one
    without the other would leave the BYOK bill unrecoverable."""
    chunks = [_Chunk(usage=_Usage(cost=0.0, cost_details={"upstream_inference_cost": 0.0045}))]
    usage = _Usage(prompt_tokens=100)
    streamcost.carry_upstream_cost(chunks, usage)
    assert usage.cost == 0.0
    assert usage.cost_details == {"upstream_inference_cost": 0.0045}


def test_a_zero_cost_is_transported_not_filtered(streamcost):
    """This module transports; ``cost_callback._extract_cost`` interprets. A
    "positive only" filter here would delete the evidence that the
    ``cost``->``cost_details`` fall-through is the right reading of a BYOK
    turn, leaving the callback unable to tell it from a missing field."""
    chunks = [_Chunk(usage=_Usage(cost=0.0))]
    usage = _Usage()
    streamcost.carry_upstream_cost(chunks, usage)
    assert usage.cost == 0.0


def test_hidden_params_usage_is_read_as_well(streamcost):
    """``ChunkProcessor`` reads both sources; reading a different set of chunks
    than the counts came from would let cost and tokens describe different
    turns."""
    chunks = [_Chunk(hidden_usage={"cost": 0.5})]
    usage = _Usage()
    streamcost.carry_upstream_cost(chunks, usage)
    assert usage.cost == 0.5


def test_the_last_reported_value_wins(streamcost):
    """A provider that revises its usage block mid-stream is stating a
    correction."""
    chunks = [_Chunk(usage=_Usage(cost=0.1)), _Chunk(usage=_Usage(cost=0.2))]
    usage = _Usage()
    streamcost.carry_upstream_cost(chunks, usage)
    assert usage.cost == 0.2


def test_an_existing_value_is_never_overwritten(streamcost):
    """If a future LiteLLM carries cost through reassembly itself, its answer
    wins and this becomes a no-op rather than a competing second opinion."""
    chunks = [_Chunk(usage=_Usage(cost=0.2, cost_details={"upstream_inference_cost": 9.0}))]
    usage = _Usage(cost=0.1, cost_details={"upstream_inference_cost": 1.0})
    streamcost.carry_upstream_cost(chunks, usage)
    assert usage.cost == 0.1
    assert usage.cost_details == {"upstream_inference_cost": 1.0}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "0.5", None])
def test_a_non_measurement_is_refused(streamcost, bad):
    """``NaN``/``Inf`` on a cost field is worse than absent: it accumulates into
    egg's per-session total and poisons it for the pod's lifetime, and
    ``json.dumps`` renders it as a non-standard token that makes the whole log
    line invalid JSON. ``True`` is excluded for the same reason
    ``_finite_number`` excludes it in ``cost_callback`` — one dollar that was
    never billed."""
    usage = _Usage()
    streamcost.carry_upstream_cost([_Chunk(usage=_Usage(cost=bad))], usage)
    assert not hasattr(usage, "cost")


def test_an_empty_cost_details_is_not_carried(streamcost):
    """``{}`` says nothing, and writing it would make the field look answered."""
    usage = _Usage()
    streamcost.carry_upstream_cost([_Chunk(usage=_Usage(cost_details={}))], usage)
    assert not hasattr(usage, "cost_details")


def test_chunks_without_usage_are_a_no_op(streamcost):
    usage = _Usage(prompt_tokens=100)
    streamcost.carry_upstream_cost([_Chunk(), _Chunk(usage=None)], usage)
    assert not hasattr(usage, "cost")


@pytest.mark.parametrize("chunks", [None, [], [object()], ["not-a-chunk"], [{"usage": None}]])
def test_a_shape_we_do_not_understand_never_raises(streamcost, chunks):
    """A cost figure is observability; it must never break a response."""
    usage = _Usage(prompt_tokens=100)
    assert streamcost.carry_upstream_cost(chunks, usage) is usage


def test_a_dict_usage_chunk_is_read(streamcost):
    """Provider iterators hand back plain dicts on some paths."""
    usage = _Usage()
    streamcost.carry_upstream_cost([{"usage": {"cost": 0.75}}], usage)
    assert usage.cost == 0.75


def test_a_hostile_usage_object_cannot_break_the_response(streamcost):
    """Every read is guarded, including the ones on the usage being amended."""

    class _Exploding:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    usage = _Usage()
    assert streamcost.carry_upstream_cost([_Chunk(usage=_Exploding())], usage) is usage


def test_an_undeclared_field_survives_on_a_real_pydantic_usage(streamcost):
    """``cost_details`` is not a declared field on litellm's ``Usage``.

    It exists only because ``Usage`` inherits openai's ``BaseModel``, which sets
    ``ConfigDict(extra="allow")``; under the default ``extra`` the same
    ``setattr`` raises ``ValueError: "Usage" object has no field "cost_details"``.
    ``carry_upstream_cost`` swallows that — correctly, a cost must never break a
    response — so if a litellm bump ever tightens the config, the ``cost_details``
    half becomes a completely silent no-op. The plain-Python ``_Usage`` double
    above cannot express that failure: ``setattr`` always works on it.

    This models the real shape (declared ``cost``, undeclared ``cost_details``)
    with an actual pydantic model, and asserts the round-trip through
    ``model_dump()`` as well — which is how ``cost_callback._coerce_usage``
    reads it, so an extra that survives ``setattr`` but is dropped by the dump
    would still be a silent loss.
    """
    pydantic = pytest.importorskip("pydantic")

    class _PydanticUsage(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")

        prompt_tokens: int = 0
        cost: float | None = None

    usage = _PydanticUsage(prompt_tokens=100)
    chunks = [_Chunk(usage=_Usage(cost=0.0, cost_details={"upstream_inference_cost": 0.0045}))]
    streamcost.carry_upstream_cost(chunks, usage)

    assert usage.cost == 0.0
    assert usage.cost_details == {"upstream_inference_cost": 0.0045}
    dumped = usage.model_dump()
    assert dumped["cost"] == 0.0
    assert dumped["cost_details"] == {"upstream_inference_cost": 0.0045}


def test_the_carried_cost_is_readable_by_the_callback_that_consumes_it(streamcost, monkeypatch):
    """Joins the producer to the consumer, which prose alone was doing.

    ``test_cost_callback.py`` asserts against a hand-built dict that *describes*
    what patch 10 is supposed to leave behind; nothing ran the real
    ``carry_upstream_cost`` output through the real ``_coerce_usage`` /
    ``_extract_cost``. This does, on the BYOK shape — ``cost`` 0, the money in
    ``cost_details.upstream_inference_cost`` — which is the one where the two
    modules have to agree on a nested key name to produce a number at all.
    """
    litellm = sys.modules.get("litellm") or types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger_mod = types.ModuleType("litellm.integrations.custom_logger")

    class _CustomLogger:  # the module only subclasses it
        pass

    custom_logger_mod.CustomLogger = _CustomLogger
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm.integrations", integrations)
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", custom_logger_mod)
    cc = _load("cost_callback")

    pydantic = pytest.importorskip("pydantic")

    class _PydanticUsage(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")

        prompt_tokens: int = 0
        completion_tokens: int = 0
        cost: float | None = None

    usage = _PydanticUsage(prompt_tokens=1000, completion_tokens=50)
    chunks = [
        _Chunk(usage=_Usage(cost=0.0, cost_details={"upstream_inference_cost": 0.0045})),
    ]
    carried = streamcost.carry_upstream_cost(chunks, usage)

    assert cc._extract_cost(cc._coerce_usage(carried)) == 0.0045


# --------------------------------------------------------------------------
# Image-interpreter compatibility
# --------------------------------------------------------------------------


# The Python the egg-litellm base image ships (ghcr.io/berriai/litellm:v1.86.2).
# Every file under config/litellm/ runs there, not on the repo's interpreter.
_IMAGE_PYTHON = (3, 11)

_IMAGE_SOURCES = (
    "cost_callback.py",
    "openrouter_capabilities.py",
    "drop_params_visibility.py",
    "anthropic_thinking_policy.py",
    "openrouter_reasoning_roundtrip.py",
    "stream_cost_preservation.py",
)


@pytest.mark.parametrize("name", _IMAGE_SOURCES)
def test_image_sources_parse_on_the_image_interpreter(name):
    """These files must be valid Python 3.11, not just valid on the repo's 3.14.

    They are the only Python in this repo that runs on a different interpreter,
    and nothing else notices: ruff formats for the repo's ``target-version``,
    mypy checks against ``python_version = "3.14"``, and the tests import them
    on 3.14 too. The formatter is the sharp edge — under ``py314`` it rewrites
    ``except (TypeError, ValueError):`` into the PEP 758 unparenthesized form,
    a hard SyntaxError on 3.11, and a formatter rewrite has no ``noqa``
    escape. ``config/litellm/.ruff.toml`` pins that directory to ``py311`` so
    it cannot happen; this asserts the outcome rather than the mechanism, so a
    future config reshuffle that loses the pin fails here.

    Without this the failure surfaces as a Docker build error at the patch
    script's parse check (fail-loud, but only once someone builds the image) or
    — for ``cost_callback.py``, which the patch script never parses — as a pod
    CrashLoopBackOff at proxy startup.
    """
    source = (CONFIG_DIR / name).read_text()
    ast.parse(source, filename=str(CONFIG_DIR / name), feature_version=_IMAGE_PYTHON)
