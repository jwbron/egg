"""Live capability and pricing lookup for OpenRouter models.

LiteLLM decides which optional params a provider accepts, and what a call cost,
by consulting the bundled model-cost map. For OpenRouter that map is wrong by
construction: OpenRouter publishes new slugs continuously, the bundled map lags
behind, and it answers for neither question on a slug it has not caught up to.
Two distinct silent failures follow from the one root cause:

* **Parameters.** ``litellm.supports_reasoning`` answers ``False`` for an
  unmapped slug, and ``OpenrouterConfig.get_supported_openai_params`` uses that
  answer as a bare gate, so the failure is closed: a ``reasoning_effort`` set
  on a current model is discarded before the request body is built, with no
  exception and (before the ``drop_params`` warning) no log line.
* **Pricing.** ``_get_model_info_helper`` raises "This model isn't mapped yet"
  for an unmapped slug, so LiteLLM's own ``response_cost`` is never computed
  and egg's ``cost_estimated`` reads null on every routed call (#3691). Every
  OpenRouter slug egg routes is absent from the pinned 1.86.2 map, so this is
  100% of routed traffic, not an edge case.

OpenRouter publishes the authoritative answer to both itself. ``GET
/api/v1/models`` returns every model with a ``supported_parameters`` list and a
``pricing`` block, and requires no API key. This module reads that once, caches
it for the life of the process, and hands callers either the parameter-name set
(``get_supported_parameters``) or a LiteLLM-shaped model-cost entry
(``get_model_cost_entry``) for a given slug.

Two deliberate limits on the pricing half, both about not trading a known
unknown for a confident wrong number:

* **Cost fields only.** The entry carries the rate card, ``litellm_provider``
  and ``mode`` — not context lengths, not ``supports_*`` flags. Registering a
  model's capabilities through this door would change behaviour well beyond
  cost: ``supports_reasoning: true`` alone makes stock
  ``get_supported_openai_params`` admit ``thinking``, which Patch 2's notes
  explain would forward an Anthropic-shaped block verbatim to a provider that
  expects ``reasoning``. Patch 4 remains the only path by which a parameter
  becomes admissible, and it admits exactly ``reasoning_effort``.
* **Tiered rate cards are translated only where LiteLLM can hold them.**
  OpenRouter expresses a long-context surcharge as ``pricing.overrides`` — a
  list keyed by an arbitrary ``min_prompt_tokens`` (32000 and 128000 on
  qwen3-max, for instance). LiteLLM's model-info schema has *named* slots at
  three fixed boundaries — ``*_above_128k_tokens``, ``*_above_200k_tokens``,
  ``*_above_272k_tokens`` — and ``_get_model_info_helper`` builds its return by
  enumerating those field names explicitly, so a tier at any other boundary is
  not merely unlikely to survive, it is inexpressible. Slot coverage is also
  uneven *per component*: there is no ``cache_read_input_token_cost`` slot at
  128k, and no ``cache_creation_input_token_cost`` slot at 128k or 272k. So the
  rule is all-or-nothing per model: every published boundary must have slots,
  and every priced component published in each override must have a slot at
  that boundary, or the whole card is declined. Translating the tiers we *can*
  hold and dropping a component we cannot would under-report the dropped one on
  exactly the long-prompt turns the tier exists to charge for — the same silent
  understatement that registering a base-tier-only entry would produce, which is
  why neither is done. A declined model is left unpriced, its ``cost_estimated``
  stays null, and the reason is logged once. The provider-billed ``cost`` (see
  ``stream_cost_preservation``) is the number to read for those models, and it
  is exact.

Design constraints, because this sits behind a hot, synchronous code path:

* **Fail soft.** Any error, timeout, non-200, or malformed payload returns
  ``None``, and every caller is expected to fall back to the existing
  ``supports_reasoning`` behaviour. This module can make param handling more
  accurate; it must never make a request fail.
* **Fetch at most once per TTL**, including after a failure. A negative cache
  entry keeps an offline or firewalled deployment from attempting a network
  call on every single request.
* **One fetch, not N**, and never a queue. A lock serialises refreshes so
  concurrent requests do not stampede the endpoint; a thread that finds the
  lock held serves the stale cache rather than waiting behind a network call.
* **Importable without litellm.** ``verbose_logger`` is imported inside
  ``_log`` rather than at module scope so this file can be imported — and
  therefore unit tested — in the egg repo, where litellm is not a dependency.

Operator knobs (all optional; see ``docs/guides/per-agent-models.md``):

* ``LITELLM_OPENROUTER_CAPABILITY_FETCH=0`` disables the lookup entirely and
  restores the previous model-map-only behaviour — parameters and pricing
  both, since one fetch serves both.
* ``LITELLM_OPENROUTER_PRICING=0`` disables only the pricing half, leaving the
  parameter lookup running. For an operator who wants LiteLLM's bundled map to
  be the sole authority on cost while keeping Patch 4's parameter fix.
* ``LITELLM_OPENROUTER_CAPABILITY_TTL`` seconds between refreshes
  (default 3600). ``0`` disables caching and re-fetches on every lookup —
  a debugging aid, not a production setting.
* ``LITELLM_OPENROUTER_CAPABILITY_TIMEOUT`` per-phase HTTP timeout in seconds
  (default 5).

An unparseable, unrecognized or out-of-range value for any of these is logged
and ignored rather than silently swallowed: an operator reaching for these vars
is very likely already debugging something. That covers a near miss on the
boolean too — ``FETCH=disabled`` is neither a recognised on nor off spelling, so
it warns and takes the default instead of being read as the opposite of what was
meant.
"""

import json
import math
import os
import threading
import time

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# The endpoint is served without authentication, so no key is read here on
# purpose: capability data must be available to a proxy that has not yet been
# handed credentials, and sending a key would make this lookup fail differently
# depending on which key happened to be in scope.
DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_TIMEOUT_SECONDS = 5.0

# Cache of slug -> record. ``None`` means "not populated". An empty dict is a
# real, meaningful state: it records a failed fetch, so the negative cache below
# can suppress retries without conflating "we asked and got nothing" with "we
# never asked".
#
# Each record is ``{"id": str, "parameters": set[str] | None, "cost_entry": dict
# | None, "declined_thresholds": tuple[int, ...] | None}``. Both payloads are optional and
# independent: OpenRouter publishes entries carrying one and not the other, and
# a slug that answers for parameters but not pricing (or the reverse) is a real
# answer for the half it has rather than a reason to drop the model. The
# thresholds are kept only so the declined-pricing warning can name them.
_CACHE: dict[str, dict] | None = None
_CACHE_STAMP: float = 0.0
_LOCK = threading.Lock()

# Whether a fetch failure has already been reported at warning level. The first
# failure is the one worth seeing (behaviour has silently reverted to the
# model-cost map); repeats are noise. Cleared again by a successful fetch, so a
# blip at startup does not permanently mute a real outage hours later.
_WARNED_FETCH_FAILURE = False

# Slugs whose tiered rate card has already been reported as declined. Bounded by
# the roster size, and cleared by every successful refetch — not only by
# ``reset_cache`` — so a pricing change upstream is reported against the roster
# it was read from rather than muted for the life of the pod.
_WARNED_DECLINED_PRICING: set[str] = set()

# Env-var complaints already emitted, keyed by ``(name, raw value)``.
# ``_env_float`` is reached from ``_ttl_seconds`` on *every* lookup, ahead of
# the freshness check, so an unconditional warning there is one WARNING line per
# proxied request forever — a misconfigured TTL would bury the log stream egg's
# per-call cost observability reads. Bounded by construction: the environment
# does not change mid-process, so this holds at most one entry per knob.
_WARNED_ENV: set[tuple[str, str]] = set()


def _log(level: str, message: str, *args: object) -> bool:
    """Log via litellm's ``verbose_logger``, deferring the import.

    Kept out of module scope so this file stays importable where litellm is
    not installed. Never raises: a diagnostic must not be able to break a
    request, and must not stop the capability lookup either.

    Returns whether the call completed without raising — *not* whether a record
    reached a handler, since ``verbose_logger.debug(...)`` on a logger set to
    INFO also returns normally. The distinction does not matter to either
    caller: what the warn-once bookkeeping below must not do is mark a line
    "already warned" when the attempt *raised* (litellm's logger not yet in
    place, say), because swallowing the exception would then also swallow the
    signal. Level filtering is the operator's own choice and is not a lost
    signal.
    """
    try:
        from litellm._logging import verbose_logger

        getattr(verbose_logger, level)(message, *args)
        return True
    except Exception:  # noqa: BLE001 - diagnostics must never break a request
        return False


def _warn_env_once(name: str, raw: str, message: str, *args: object) -> None:
    """Warn about a bad env value once per distinct ``(name, value)``.

    Same discipline as ``_log_fetch_failure`` here and ``_SEEN`` in
    ``drop_params_visibility``: the first occurrence is the one that carries
    information, and this one sits on the request path.
    """
    key = (name, raw)
    if key in _WARNED_ENV:
        return
    # Recorded only once the emit did not raise. ``_log`` deliberately never
    # propagates, so recording first would mean a failure on the *first* call —
    # litellm's logger not yet in place, say — permanently suppresses the
    # warning: every later call would find the key already there.
    if _log("warning", message, *args):
        _WARNED_ENV.add(key)


_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env var, warning rather than guessing at a near miss.

    A bare ``raw not in _FALSY`` reads every unrecognized value as *enable*, so
    a near-miss disable spelling (``=disabled``, ``=n``) does not fall back to
    the default — it inverts the operator's instruction, silently. Anything
    matching neither list warns once and takes the default, which is the same
    discipline ``_env_float`` applies and the behaviour this module's docstring
    promises for all three knobs.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    _warn_env_once(
        name,
        raw,
        "openrouter capabilities: %s=%r is not a boolean (expected one of %s "
        "or %s); using the default %s",
        name,
        raw,
        ", ".join(_TRUTHY),
        ", ".join(_FALSY),
        default,
    )
    return default


def _env_float(name: str, default: float, *, allow_zero: bool = False) -> float:
    """Read a float env var, warning rather than silently falling back.

    A deliberately-set-but-unusable value (``TTL=0`` when zero is not allowed,
    ``TIMEOUT=5s`` pasted with a unit suffix) previously became the default
    with no signal at all, so an operator could set a knob, restart, observe
    nothing change, and have no way to learn the proxy ignored them.

    The warning is deduplicated per ``(name, value)``: this is called from
    ``_ttl_seconds`` on the per-request path, so warning unconditionally would
    trade a silent fallback for an unbounded log flood.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        _warn_env_once(
            name,
            raw,
            "openrouter capabilities: %s=%r is not a number; using the default %s",
            name,
            raw,
            default,
        )
        return default
    if value < 0 or (value == 0 and not allow_zero):
        _warn_env_once(
            name,
            raw,
            "openrouter capabilities: %s=%r must be %s; using the default %s",
            name,
            raw,
            ">= 0" if allow_zero else "> 0",
            default,
        )
        return default
    return value


def _ttl_seconds() -> float:
    # Zero is allowed and meaningful: it is the natural spelling of "never
    # cache, always re-fetch", and rejecting it was the least discoverable of
    # this module's silent fallbacks. It makes every lookup a network call, so
    # it is a debugging setting rather than a production one.
    return _env_float("LITELLM_OPENROUTER_CAPABILITY_TTL", DEFAULT_TTL_SECONDS, allow_zero=True)


# OpenRouter pricing key -> LiteLLM model-cost key. Every value is USD per
# token, published as a decimal *string*, which is why ``_price`` parses rather
# than casts.
#
# ``input_cache_write`` maps to LiteLLM's ``cache_creation_*`` spelling: the two
# name the same thing (what you pay to put a prefix in the cache), and getting
# this pair backwards would silently price cache writes at the read rate, which
# on these routes is a ~5x understatement of the turn that costs the most.
#
# The last two pairs were added after review: both are per-token rates LiteLLM
# actually consumes on the chat path (``generic_cost_per_token`` reads
# ``output_cost_per_reasoning_token`` for reasoning tokens, and
# ``calculate_cache_writing_cost`` reads the ``_above_1hr`` write rate for a 1h
# TTL block), so omitting them under-reported those components rather than
# leaving them unknown.
#
# Components OpenRouter publishes that are deliberately NOT mapped, because
# LiteLLM 1.86.2 has no per-token slot that its chat-path cost calculation
# reads: ``request`` (per-request, not per-token), ``web_search`` (per-request;
# ``search_context_cost_per_query`` is a responses-API surface, not a chat one),
# ``image`` / ``audio`` / ``input_audio_cache`` (modality rates that egg's routed
# traffic does not exercise), and ``discount``. A model whose bill is dominated
# by one of those will read low under ``cost_estimated``; the provider-billed
# ``cost`` remains exact.
_PRICE_KEYS = (
    ("prompt", "input_cost_per_token"),
    ("completion", "output_cost_per_token"),
    ("input_cache_read", "cache_read_input_token_cost"),
    ("input_cache_write", "cache_creation_input_token_cost"),
    ("input_cache_write_1h", "cache_creation_input_token_cost_above_1hr"),
    ("internal_reasoning", "output_cost_per_reasoning_token"),
)

# Reverse index, used by the tier translation to tell "a component we price and
# cannot express at this boundary" (fatal — it would under-report) from "a
# component we do not price at the base tier either" (already out of scope, and
# no more wrong at 200k than at 0).
_MAPPED_PRICE_KEYS = frozenset(published for published, _ in _PRICE_KEYS)

# The two rates without which an entry cannot price a chat turn at all. A
# missing cache rate degrades the estimate; a missing prompt or completion rate
# would make it meaningless, so the entry is declined instead.
_REQUIRED_PRICE_KEYS = ("prompt", "completion")

# OpenRouter ``min_prompt_tokens`` boundary -> the LiteLLM model-cost keys that
# exist at that boundary, per published component. Transcribed from the explicit
# field enumeration in ``_get_model_info_helper`` (litellm/utils.py, 1.86.2):
# a key absent from that enumeration is dropped on the way out of the lookup, so
# the gaps below are the real shape of the schema and not a conservative guess.
_TIER_SLOTS: dict[int, dict[str, str]] = {
    128000: {
        "prompt": "input_cost_per_token_above_128k_tokens",
        "completion": "output_cost_per_token_above_128k_tokens",
    },
    200000: {
        "prompt": "input_cost_per_token_above_200k_tokens",
        "completion": "output_cost_per_token_above_200k_tokens",
        "input_cache_read": "cache_read_input_token_cost_above_200k_tokens",
        "input_cache_write": "cache_creation_input_token_cost_above_200k_tokens",
    },
    272000: {
        "prompt": "input_cost_per_token_above_272k_tokens",
        "completion": "output_cost_per_token_above_272k_tokens",
        "input_cache_read": "cache_read_input_token_cost_above_272k_tokens",
    },
}


def _price(raw: object) -> float | None:
    """Parse one published rate. ``None`` when it is not a usable number.

    Zero is usable and is kept: a ``:free`` variant really is priced at zero,
    and the resulting zero estimate is filtered by ``cost_callback``'s own
    ``_positive`` gate rather than being invented here. Negative and non-finite
    values are refused — neither is a rate, and an ``inf`` would propagate into
    a session total and into the emitted JSON as a token that makes the line
    unparseable.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or not math.isfinite(value):
        return None
    return value


def _boundary(raw: object) -> int | None:
    """Parse one ``min_prompt_tokens`` value. ``None`` when it is not a count.

    Accepts the decimal-*string* spelling as well as the integer one. Every
    other number in this payload is published as a string
    (``"prompt": "0.0000012"``), so a schema that switched ``min_prompt_tokens``
    to ``"128000"`` for consistency would be entirely in character — and an
    ``isinstance(raw, int)`` test would have read that as "unparseable", turning
    an expressible tier into a declined model with a warning blaming
    OpenRouter's schema. A float that is not a whole number is refused rather
    than truncated: a fractional token boundary is not a thing, so it means the
    field is not what this thinks it is.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value != int(value):
        return None
    return int(value)


def _tier_rates(overrides: list) -> tuple[dict[str, float] | None, tuple[int, ...]]:
    """Translate ``pricing.overrides`` into LiteLLM tiered-rate keys.

    Returns ``(rates, thresholds)``. ``rates`` is None when the card cannot be
    held faithfully — see the module docstring — and ``thresholds`` always names
    whatever boundaries parsed, so the decline warning can be specific even when
    the reason for declining was one of them failing to parse.

    All-or-nothing on purpose. Emitting the tiers that fit and dropping the rest
    is the failure mode this whole path exists to avoid: LiteLLM applies at most
    one boundary per call and falls back to the *base* rate for any component
    with no key at that boundary, so a partial translation reports a surcharged
    turn at the un-surcharged rate for the dropped component — silently, and
    only on long prompts.
    """
    parsed: list[tuple[int, dict]] = []
    readable = True
    for override in overrides:
        raw = override.get("min_prompt_tokens") if isinstance(override, dict) else None
        boundary = _boundary(raw)
        if boundary is None:
            readable = False
            continue
        parsed.append((boundary, override))

    thresholds = tuple(sorted(boundary for boundary, _ in parsed))
    if not readable or not parsed:
        return None, thresholds

    rates: dict[str, float] = {}
    for boundary, override in parsed:
        slots = _TIER_SLOTS.get(boundary)
        if slots is None:
            return None, thresholds
        # A surcharge with no ``prompt`` rate is unreachable, not merely
        # incomplete: LiteLLM finds the applicable boundary by scanning for
        # ``input_cost_per_token_above_*`` keys, so a tier that publishes only a
        # completion surcharge would contribute a key nothing ever reads and
        # bill the whole turn at base.
        if _price(override.get("prompt")) is None:
            return None, thresholds
        for published, raw in override.items():
            if published == "min_prompt_tokens" or published not in _MAPPED_PRICE_KEYS:
                # An unmapped component is out of scope at the base tier too, so
                # declining the model over it would withhold a rate card that is
                # no less complete above the boundary than below it.
                continue
            price = _price(raw)
            slot = slots.get(published)
            if price is None or slot is None:
                return None, thresholds
            rates[slot] = price
    return rates, thresholds


def _cost_entry(model_id: str, pricing: object) -> tuple[dict | None, tuple[int, ...] | None]:
    """Translate an OpenRouter ``pricing`` block to a LiteLLM model-cost entry.

    Returns ``(entry, declined_thresholds)``. ``entry`` is None when no faithful
    translation exists; ``declined_thresholds`` is None unless the reason was a
    tiered rate card LiteLLM cannot hold, in which case it names the
    ``min_prompt_tokens`` boundaries — the module docstring has the reasoning.

    The two are separate returns rather than one truthiness test because a
    tiered card whose boundaries do not parse is still a tiered card: an empty
    tuple must keep meaning "declined for tiering, boundaries unknown", so the
    operator still gets told why the model is unpriced instead of only the
    models whose overrides happened to be well-formed.

    ``key`` is set to the OpenRouter slug rather than left to the caller: it is
    what ``get_model_info`` reports as the entry's identity, and an operator
    reading ``/model/info`` should see the slug the rate actually came from.
    """
    if not isinstance(pricing, dict):
        return None, None

    tier_rates: dict[str, float] = {}
    overrides = pricing.get("overrides")
    if isinstance(overrides, list) and overrides:
        translated, thresholds = _tier_rates(overrides)
        if translated is None:
            return None, thresholds
        tier_rates = translated

    if any(_price(pricing.get(key)) is None for key in _REQUIRED_PRICE_KEYS):
        return None, None

    entry: dict = {"key": model_id, "litellm_provider": "openrouter", "mode": "chat"}
    for published, litellm_key in _PRICE_KEYS:
        value = _price(pricing.get(published))
        if value is not None:
            entry[litellm_key] = value
    entry.update(tier_rates)
    return entry, None


def _fetch() -> dict[str, dict]:
    """Fetch the model list. Returns ``{}`` on any failure."""
    global _WARNED_FETCH_FAILURE

    # Imported here rather than at module scope: http_handler pulls in a large
    # slice of litellm, and this module is imported from a transformation that
    # is itself imported during litellm's own startup.
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    timeout = _env_float("LITELLM_OPENROUTER_CAPABILITY_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    try:
        response = HTTPHandler(timeout=timeout).get(OPENROUTER_MODELS_URL)
        if response.status_code != 200:
            _log_fetch_failure(
                "%s returned HTTP %s; falling back to the bundled model-cost map",
                OPENROUTER_MODELS_URL,
                response.status_code,
            )
            return {}
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - never propagate into a request
        _log_fetch_failure(
            "fetch failed (%s: %s); falling back to the bundled model-cost map",
            type(exc).__name__,
            exc,
        )
        return {}

    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            _log_fetch_failure("response body was not JSON; falling back to the model-cost map")
            return {}

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        _log_fetch_failure("response had no `data` list; falling back to the model-cost map")
        return {}

    capabilities: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        params = entry.get("supported_parameters")
        parameters = {p for p in params if isinstance(p, str)} if isinstance(params, list) else None
        cost_entry, declined = _cost_entry(model_id, entry.get("pricing"))
        # An entry that answers neither question carries no information, and
        # keeping it would make a roster of such entries look like a successful
        # fetch to the "no usable entries" check below. A declined rate card is
        # an answer — "we saw this model and will not price it" — so it keeps
        # the record alive for the warning.
        if parameters is None and cost_entry is None and declined is None:
            continue
        capabilities[model_id] = {
            # The published slug, not the spelling the caller looked up: a
            # record reached via an ``openrouter/``-prefixed or ``:free``-suffixed
            # candidate must still name itself the way OpenRouter does.
            "id": model_id,
            "parameters": parameters,
            "cost_entry": cost_entry,
            "declined_thresholds": declined,
        }

    if not capabilities:
        # A 200 whose `data` list is empty, or every entry of which is
        # unparseable, is an OpenRouter schema change or an empty roster — not
        # the ordinary "we asked and got nothing usable" fallback it otherwise
        # looks like from the outside. Reporting it keeps `{}` meaning exactly
        # one thing (see the _CACHE comment) and makes a schema drift visible
        # instead of indistinguishable from a fetch that simply had no opinion.
        _log_fetch_failure(
            "`data` list contained no usable entries; falling back to the bundled model-cost map"
        )
        return {}

    # Re-arm the failure warning, and only on a fetch that actually produced
    # capabilities. Latching it for the life of the process would mean a single
    # blip during pod startup permanently demotes every later outage to debug —
    # silencing exactly the case _log_fetch_failure exists to surface — but
    # re-arming on an empty parse would report a recovery that did not happen.
    _WARNED_FETCH_FAILURE = False
    # Re-arm the per-slug decline warning against the roster it was derived
    # from. Without this the latch outlives every refetch, so a model that
    # dropped its surcharge tiers — or acquired them — reports the change once
    # per pod lifetime at most, which for a long-lived proxy means never. The
    # roster this replaces is the only thing the old keys described.
    _WARNED_DECLINED_PRICING.clear()
    return capabilities


def _log_fetch_failure(message: str, *args: object) -> None:
    """First failure at warning, the rest at debug.

    A permanently unreachable endpoint is the exact case where behaviour
    silently reverts to the model-cost map, and litellm's default log level is
    INFO — so debug-only reporting reproduces, one file over, the silence
    Patch 8 exists to remove. Warning once is enough to be findable without
    turning an offline deployment into a log flood.
    """
    global _WARNED_FETCH_FAILURE

    level = "debug" if _WARNED_FETCH_FAILURE else "warning"
    # Latched only once the emit did not raise, for the reason in ``_warn_env_once``:
    # ``_log`` swallows its own failures, and marking "already warned" for a
    # line that was never emitted would demote every later outage to debug
    # without anyone having seen the first one.
    if _log(level, "openrouter capabilities: " + message, *args):
        _WARNED_FETCH_FAILURE = True


def _get_cache() -> dict[str, dict]:
    global _CACHE, _CACHE_STAMP

    ttl = _ttl_seconds()
    cache = _CACHE
    if cache is not None and (time.monotonic() - _CACHE_STAMP) < ttl:
        return cache

    # Exactly one thread refreshes. A thread that finds the lock held serves
    # the cache it already has rather than queueing behind someone else's HTTP
    # call: httpx timeouts are per-phase, not total, so a pathological
    # connection can exceed the configured seconds and that latency would land
    # on a live request once per TTL. Stale capability data is cheap; added
    # request latency is not. Only the very first fetch — nothing cached to
    # serve — actually blocks.
    if not _LOCK.acquire(blocking=cache is None):
        return cache  # type: ignore[return-value]
    try:
        # Re-check under the lock: another thread may have refreshed while this
        # one waited, and a second fetch would be pure waste.
        if _CACHE is not None and (time.monotonic() - _CACHE_STAMP) < ttl:
            return _CACHE
        # Stamped even on failure, so an unreachable endpoint costs one attempt
        # per TTL rather than one per request.
        _CACHE = _fetch()
        _CACHE_STAMP = time.monotonic()
        return _CACHE
    finally:
        _LOCK.release()


def _candidate_slugs(model: str, *, strip_variant: bool = True) -> list[str]:
    """Spellings of ``model`` that may appear as an OpenRouter model id.

    Callers reach this from several directions: a bare slug
    (``qwen/qwen3-max``), a provider-prefixed one (``openrouter/qwen/qwen3-max``
    from a ``litellm_params.model``), or a slug carrying an OpenRouter variant
    suffix (``qwen/qwen3-max:free``). The ids returned by the API are bare
    slugs, with ``:free`` published as its own id.

    ``strip_variant=False`` drops the base-slug fallback for a ``:``-bearing
    model. The two halves want different answers here. For parameters the
    fallback is safe because the lookup is union-only and never subtractive: a
    variant accepts at least what its base does, so inheriting the base's list
    can admit a parameter, never withdraw one. For pricing it is not, because
    the variant suffix is frequently *what changes the rate* — a ``:free``
    variant is 0 against a paid base, and a ``:batch`` variant is half its base
    on every model that publishes one — so inheriting would report a confident,
    authoritative-looking number that is wrong by construction, which is the one
    outcome this module refuses everywhere else.
    Stripping the ``openrouter/`` prefix is kept for both: that names the same
    model, not a different rate card.
    """
    candidates: list[str] = []

    def add(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    add(model)
    if model.startswith("openrouter/"):
        add(model[len("openrouter/") :])
    # Variant suffixes (:free, :nitro, :floor, ...) are sometimes their own id
    # and sometimes only a routing hint on the base model, so try both.
    if strip_variant:
        for candidate in list(candidates):
            if ":" in candidate:
                add(candidate.split(":", 1)[0])
    return candidates


def _lookup(model: str, field: str, *, strip_variant: bool = True) -> dict | None:
    """The published record answering for ``field``, or None for no opinion.

    "No opinion" covers every way the answer can be unknown: the lookup is
    disabled, the fetch failed, or no candidate spelling is in the published
    list.

    ``field`` is taken rather than assumed because a record can answer one half
    and not the other: OpenRouter publishes entries with a rate card and no
    ``supported_parameters``, and stopping at the first record found would let
    such an entry shadow a later candidate that does answer. Before this
    module's pricing half existed, ``_fetch`` dropped those entries outright and
    the candidate loop fell through them by accident; keeping them for their
    pricing turned that accident into a silent regression of the parameter fix
    — ``reasoning_effort`` dropped for a variant slug whose entry happens to
    carry only rates. So candidates that cannot answer are skipped, and the
    first record seen is still returned as a fallback so
    ``get_model_cost_entry`` can report *why* a model it has definitely seen is
    going unpriced.
    """
    if not _env_flag("LITELLM_OPENROUTER_CAPABILITY_FETCH", True):
        return None
    if not model:
        return None

    cache = _get_cache()
    if not cache:
        return None

    fallback: dict | None = None
    for candidate in _candidate_slugs(model, strip_variant=strip_variant):
        record = cache.get(candidate)
        if record is None:
            continue
        if record.get(field) is not None:
            return record
        if fallback is None:
            fallback = record
    return fallback


def get_supported_parameters(model: str) -> set[str] | None:
    """Parameter names OpenRouter advertises for ``model``.

    Returns ``None`` when the answer is unknown for any reason (see ``_lookup``)
    or when the roster entry carried no ``supported_parameters`` list. A
    ``None`` return means "no opinion" and callers must fall back to whatever
    they did before.

    The returned set is a copy — the cache is process-wide and lives for the
    TTL, so handing out the stored object would let one caller's ``add`` change
    what every later caller sees.
    """
    record = _lookup(model, "parameters")
    if record is None:
        return None
    params = record.get("parameters")
    return set(params) if params is not None else None


def get_model_cost_entry(model: str, custom_llm_provider: str | None = None) -> dict | None:
    """A LiteLLM-shaped model-cost entry for ``model``, or None (#3691).

    None means "no opinion", exactly as in ``get_supported_parameters``, and the
    caller must fall back to whatever it did before — which for
    ``_get_model_info_helper`` is the stock "This model isn't mapped yet"
    ValueError. There are four ways to get it: the lookup is off, the pricing
    half specifically is off, the slug is unknown, or its rate card is tiered at
    a boundary LiteLLM cannot hold (see the module docstring; that case is
    warned about once per slug, because unlike the others it is a model egg
    *can* see and still will not price).

    Unlike the parameter half, this does **not** fall back from a variant slug
    to its base — ``_candidate_slugs``' docstring has the reasoning. So a
    ``:free`` or ``:batch`` route that OpenRouter has retired from the roster
    reads null here rather than inheriting the base model's paid rate.
    ``entry["key"]`` is therefore always the slug the rates were published
    under, and ``/model/info`` can be read as naming the real source.

    ``custom_llm_provider`` is accepted and checked rather than ignored: this
    module speaks only for OpenRouter, and the call site sits on a generic
    lookup that every provider reaches. Answering there for, say, a Bedrock slug
    that happens to share a name would attach OpenRouter's rate card to someone
    else's bill. None is permitted because the caller resolves the provider from
    the model string, and a bare ``qwen/qwen3-max`` legitimately arrives
    unattributed.

    The returned dict is a copy: the cache is process-wide, and LiteLLM's
    model-info path is free to mutate what it is handed.

    Note on freshness: LiteLLM memoizes ``_get_model_info_helper`` behind an
    ``lru_cache``, so the FIRST successful answer for a slug is what the
    process uses until it restarts — this module's TTL governs how often the
    roster is re-read, not how often a priced model is re-priced. That is the
    same staleness every entry in the bundled map already has, and rates move
    on a scale where it does not matter; the provider-billed ``cost`` is
    unaffected either way. ``lru_cache`` does not memoize exceptions, so a
    lookup that failed while the roster was unreachable is retried rather than
    latched.
    """
    if custom_llm_provider is not None and custom_llm_provider != "openrouter":
        return None
    if not _env_flag("LITELLM_OPENROUTER_PRICING", True):
        return None

    record = _lookup(model, "cost_entry", strip_variant=False)
    if record is None:
        return None

    entry = record.get("cost_entry")
    if entry is not None:
        return dict(entry)

    declined = record.get("declined_thresholds")
    if declined is not None:
        _warn_declined_pricing(record, declined)
    return None


def _warn_declined_pricing(record: dict, thresholds: tuple[int, ...]) -> None:
    """Report a tiered rate card we will not translate, once per slug.

    Warn-level and once: an operator looking at a null ``cost_estimated`` for a
    model that plainly *has* a published price needs to find this, and this runs
    on the per-call cost path, so repeating it would bury the very log stream
    the cost figures land in.
    """
    slug = record.get("id") or "<unknown>"
    if slug in _WARNED_DECLINED_PRICING:
        return
    where = (
        f"tiers at {', '.join(str(t) for t in thresholds)} tokens"
        if thresholds
        else "tier boundaries unparseable"
    )
    # Deliberately says "this card", not "tiered cards": most of them are now
    # translated (see ``_TIER_SLOTS``), so a message asserting that LiteLLM
    # cannot express tier boundaries would be false in the general case and
    # would send an operator looking for a limit that is not the one they hit.
    # The boundaries are named so the specific reason is checkable against
    # ``_TIER_SLOTS`` without reading the roster.
    if _log(
        "warning",
        "openrouter capabilities: %s prices by prompt length (%s) in a shape LiteLLM "
        "cannot hold — it has rate slots only at 128000/200000/272000 tokens, and not "
        "for every component at each. cost_estimated stays null for this model rather "
        "than under-reporting long prompts. Read the provider-billed `cost` field "
        "instead; it is exact.",
        slug,
        where,
    ):
        _WARNED_DECLINED_PRICING.add(slug)


def reset_cache() -> None:
    """Drop cached capability data. Intended for tests."""
    global _CACHE, _CACHE_STAMP, _WARNED_FETCH_FAILURE
    with _LOCK:
        _CACHE = None
        _CACHE_STAMP = 0.0
        _WARNED_FETCH_FAILURE = False
        _WARNED_ENV.clear()
        _WARNED_DECLINED_PRICING.clear()
