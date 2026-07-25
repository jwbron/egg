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


@pytest.fixture
def logger(monkeypatch):
    """Install a stub ``litellm._logging.verbose_logger``."""
    recorder = _RecordingLogger()
    litellm = sys.modules.get("litellm") or types.ModuleType("litellm")
    logging_mod = types.ModuleType("litellm._logging")
    logging_mod.verbose_logger = recorder
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm._logging", logging_mod)
    return recorder


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


@pytest.mark.parametrize("raw,expected", [("0", False), ("off", False), ("no", False), ("1", True)])
def test_env_flag(caps, monkeypatch, raw, expected):
    monkeypatch.setenv("LITELLM_OPENROUTER_CAPABILITY_FETCH", raw)
    assert caps._env_flag("LITELLM_OPENROUTER_CAPABILITY_FETCH", True) is expected


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


def test_non_200_is_no_opinion(caps, logger, monkeypatch):
    _install_http_stub(monkeypatch, payload=_PAYLOAD, status_code=503)
    assert caps.get_supported_parameters("moonshotai/kimi-k3") is None
    assert any("HTTP 503" in m for m in logger.messages("warning"))


def test_payload_without_data_list_is_no_opinion(caps, logger, monkeypatch):
    _install_http_stub(monkeypatch, payload={"error": "nope"})
    assert caps.get_supported_parameters("moonshotai/kimi-k3") is None


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
