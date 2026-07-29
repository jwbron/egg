"""Unit tests for the three modules the patch script installs into litellm.

``config/litellm/{openrouter_capabilities,drop_params_visibility,
anthropic_thinking_policy}.py`` are staged by the Dockerfile and copied into
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


_PAYLOAD = {
    "data": [
        {"id": "moonshotai/kimi-k3", "supported_parameters": ["reasoning", "reasoning_effort"]},
        {"id": "poolside/laguna-s-2.1", "supported_parameters": ["reasoning"]},
        {"id": "qwen/qwen3-max:free", "supported_parameters": ["temperature"]},
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
def roundtrip():
    return _load("openrouter_reasoning_roundtrip")


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
                "thinking_blocks": [_thinking("first "), _thinking("second "), _thinking("third")],
            }
        ]
    )

    assert out[0]["reasoning_content"] == "first second third"


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
    original = {"role": "assistant", "content": "plain"}

    out = roundtrip.map_thinking_blocks_to_reasoning_content([original])

    assert out[0] is original, "no copy, no rewrite, nothing to do"


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
                    _thinking("kept "),
                    None,
                    {"type": "thinking"},
                    _thinking("too"),
                ],
            }
        ]
    )

    assert out[0]["reasoning_content"] == "kept too"


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


def test_module_imports_without_litellm_installed(roundtrip):
    """Same contract as the other three staged modules: no litellm import at
    module scope, or it could not be tested here at all."""
    source = (CONFIG_DIR / "openrouter_reasoning_roundtrip.py").read_text()
    assert "import litellm" not in source
    assert "from litellm" not in source
