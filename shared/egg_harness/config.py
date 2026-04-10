"""Configuration dataclasses and model resolution for the egg harness.

Provides ProviderConfig and HarnessConfig for configuring agent sessions,
along with model alias resolution and context window lookup utilities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Model alias table
# ---------------------------------------------------------------------------

MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-5-20250514",
    "haiku": "claude-haiku-4-5",
}

# ---------------------------------------------------------------------------
# Context window sizes (tokens)
# ---------------------------------------------------------------------------

CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-5-20250514": 200_000,
    "claude-haiku-4-5": 200_000,
}

_DEFAULT_CONTEXT_WINDOW: int = 128_000

# ---------------------------------------------------------------------------
# Regex for parsing model specs like "opus[1m]" or "sonnet[200k]"
# ---------------------------------------------------------------------------

_MODEL_SPEC_RE = re.compile(
    r"^(?P<model>[A-Za-z0-9._-]+?)(?:\[(?P<size>\d+)(?P<unit>[km])\])?$"
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def resolve_model(model: str) -> str:
    """Resolve a short model alias to its canonical name.

    Known aliases (e.g. ``"opus"``, ``"sonnet"``, ``"haiku"``) are expanded to
    their full model identifiers.  Unknown names pass through unchanged.

    Args:
        model: A model alias or full model name.

    Returns:
        The canonical model identifier.
    """
    return MODEL_ALIASES.get(model, model)


def parse_model_spec(spec: str) -> tuple[str, int | None]:
    """Parse a model specification that may include an optional context size.

    Accepted formats::

        "opus"                           -> ("claude-opus-4-6", None)
        "opus[1m]"                       -> ("claude-opus-4-6", 1_000_000)
        "opus[200k]"                     -> ("claude-opus-4-6", 200_000)
        "claude-sonnet-4-5-20250514[500k]" -> ("claude-sonnet-4-5-20250514", 500_000)

    Args:
        spec: A model name or alias, optionally followed by ``[<n>k]`` or
            ``[<n>m]`` to specify context window size.

    Returns:
        A tuple of (resolved_model_name, context_window_tokens_or_None).

    Raises:
        ValueError: If *spec* does not match the expected pattern.
    """
    match = _MODEL_SPEC_RE.match(spec)
    if match is None:
        raise ValueError(f"Invalid model spec: {spec!r}")

    raw_model = match.group("model")
    resolved = resolve_model(raw_model)

    size_digits = match.group("size")
    unit = match.group("unit")

    if size_digits is None:
        return resolved, None

    multiplier = 1_000_000 if unit == "m" else 1_000
    return resolved, int(size_digits) * multiplier


def get_context_window(model: str) -> int:
    """Return the context window size (in tokens) for the given model.

    Args:
        model: A canonical model identifier (not an alias).

    Returns:
        The known context window size, or 128 000 for unrecognised models.
    """
    return CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT_WINDOW)


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    """Configuration for the LLM provider used by the harness.

    Attributes:
        provider_type: Provider backend identifier (e.g. ``"anthropic"``,
            ``"openai_compatible"``).
        model: The resolved model name to use.
        endpoint: Optional API endpoint URL override.
        api_key_env: Name of the environment variable that holds the API key.
            The key itself is **never** stored here.
        extra_headers: Optional additional HTTP headers to include in requests.
    """

    provider_type: str
    model: str
    endpoint: str | None = None
    api_key_env: str | None = None
    extra_headers: dict[str, str] | None = None


@dataclass
class HarnessConfig:
    """Top-level configuration for an egg harness session.

    Attributes:
        provider: LLM provider settings.
        max_turns: Maximum number of agent turns before the session is
            terminated.
        timeout: Hard wall-clock timeout for the session in seconds.
        cwd: Working directory for the agent process.  ``None`` means use the
            current directory.
        env: Extra environment variables to inject into the agent process.
        disallowed_tools: Tool names the agent must not invoke.
        intercept_tools: Whether the harness intercepts tool calls for
            policy enforcement.
        compaction_threshold: Fraction of the context window at which the
            harness triggers automatic context compaction.
        keep_recent_tokens: Number of recent tokens to preserve verbatim
            during compaction.
    """

    provider: ProviderConfig
    max_turns: int = 200
    timeout: int = 7200
    cwd: str | None = None
    env: dict[str, str] | None = field(default=None)
    disallowed_tools: list[str] | None = field(default=None)
    intercept_tools: bool = True
    compaction_threshold: float = 0.8
    keep_recent_tokens: int = 20_000
