"""Live capability lookup for OpenRouter models.

LiteLLM decides which optional params a provider accepts by consulting the
bundled model-cost map. For OpenRouter that map is wrong by construction:
OpenRouter publishes new slugs continuously, the bundled map lags behind, and
``litellm.supports_reasoning`` answers ``False`` for anything it has not caught
up to yet. Because ``OpenrouterConfig.get_supported_openai_params`` uses that
answer as a bare gate, the failure is closed and silent: a ``reasoning_effort``
set on a current model is discarded before the request body is built, with no
exception and (before the ``drop_params`` warning) no log line.

OpenRouter publishes the authoritative answer itself. ``GET /api/v1/models``
returns every model with a ``supported_parameters`` list and requires no API
key. This module reads that, caches it for the life of the process, and hands
callers a set of parameter names for a given slug.

Design constraints, because this sits behind a hot, synchronous code path:

* **Fail soft.** Any error, timeout, non-200, or malformed payload returns
  ``None``, and every caller is expected to fall back to the existing
  ``supports_reasoning`` behaviour. This module can make param handling more
  accurate; it must never make a request fail.
* **Fetch at most once per TTL**, including after a failure. A negative cache
  entry keeps an offline or firewalled deployment from attempting a network
  call on every single request.
* **One fetch, not N.** A lock serialises refreshes so concurrent first
  requests do not stampede the endpoint.
* **Opt-out.** ``LITELLM_OPENROUTER_CAPABILITY_FETCH=0`` disables the lookup
  entirely and restores the previous model-map-only behaviour.
"""

import json
import os
import threading
import time

from litellm._logging import verbose_logger

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# The endpoint is served without authentication, so no key is read here on
# purpose: capability data must be available to a proxy that has not yet been
# handed credentials, and sending a key would make this lookup fail differently
# depending on which key happened to be in scope.
DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_TIMEOUT_SECONDS = 5.0

# Cache of slug -> supported parameter names. ``None`` means "not populated".
# An empty dict is a real, meaningful state: it records a failed fetch, so the
# negative cache below can suppress retries without conflating "we asked and
# got nothing" with "we never asked".
_CACHE: dict[str, set[str]] | None = None
_CACHE_STAMP: float = 0.0
_LOCK = threading.Lock()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _ttl_seconds() -> float:
    return _env_float("LITELLM_OPENROUTER_CAPABILITY_TTL", DEFAULT_TTL_SECONDS)


def _fetch() -> dict[str, set[str]]:
    """Fetch the model list. Returns ``{}` on any failure."""
    # Imported here rather than at module scope: http_handler pulls in a large
    # slice of litellm, and this module is imported from a transformation that
    # is itself imported during litellm's own startup.
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    timeout = _env_float("LITELLM_OPENROUTER_CAPABILITY_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    try:
        response = HTTPHandler(timeout=timeout).get(OPENROUTER_MODELS_URL)
        if response.status_code != 200:
            verbose_logger.debug(
                "openrouter capabilities: %s returned HTTP %s; falling back to the bundled model-cost map",
                OPENROUTER_MODELS_URL,
                response.status_code,
            )
            return {}
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - never propagate into a request
        verbose_logger.debug(
            "openrouter capabilities: fetch failed (%s: %s); falling back to the bundled model-cost map",
            type(exc).__name__,
            exc,
        )
        return {}

    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            return {}

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}

    capabilities: dict[str, set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        params = entry.get("supported_parameters")
        if not isinstance(model_id, str) or not isinstance(params, list):
            continue
        capabilities[model_id] = {p for p in params if isinstance(p, str)}
    return capabilities


def _get_cache() -> dict[str, set[str]]:
    global _CACHE, _CACHE_STAMP

    now = time.monotonic()
    cache = _CACHE
    if cache is not None and (now - _CACHE_STAMP) < _ttl_seconds():
        return cache

    with _LOCK:
        # Re-check under the lock: another thread may have refreshed while this
        # one waited, and a second fetch would be pure waste.
        now = time.monotonic()
        if _CACHE is not None and (now - _CACHE_STAMP) < _ttl_seconds():
            return _CACHE
        # Stamped even on failure, so an unreachable endpoint costs one attempt
        # per TTL rather than one per request.
        _CACHE = _fetch()
        _CACHE_STAMP = time.monotonic()
        return _CACHE


def _candidate_slugs(model: str) -> list:
    """Spellings of ``model`` that may appear as an OpenRouter model id.

    Callers reach this from several directions: a bare slug
    (``qwen/qwen3-max``), a provider-prefixed one (``openrouter/qwen/qwen3-max``
    from a ``litellm_params.model``), or a slug carrying an OpenRouter variant
    suffix (``qwen/qwen3-max:free``). The ids returned by the API are bare
    slugs, with ``:free`` published as its own id.
    """
    candidates = []

    def add(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    add(model)
    if model.startswith("openrouter/"):
        add(model[len("openrouter/") :])
    # Variant suffixes (:free, :nitro, :floor, ...) are sometimes their own id
    # and sometimes only a routing hint on the base model, so try both.
    for candidate in list(candidates):
        if ":" in candidate:
            add(candidate.split(":", 1)[0])
    return candidates


def get_supported_parameters(model: str) -> set[str] | None:
    """Parameter names OpenRouter advertises for ``model``.

    Returns ``None`` when the answer is unknown for any reason: the lookup is
    disabled, the fetch failed, or the slug is not in the published list. A
    ``None`` return means "no opinion" and callers must fall back to whatever
    they did before.
    """
    if not _env_flag("LITELLM_OPENROUTER_CAPABILITY_FETCH", True):
        return None
    if not model:
        return None

    cache = _get_cache()
    if not cache:
        return None

    for candidate in _candidate_slugs(model):
        params = cache.get(candidate)
        if params is not None:
            return params
    return None


def reset_cache() -> None:
    """Drop cached capability data. Intended for tests."""
    global _CACHE, _CACHE_STAMP
    with _LOCK:
        _CACHE = None
        _CACHE_STAMP = 0.0
