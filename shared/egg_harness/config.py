"""Configuration dataclasses and model resolution for the egg harness.

Provides ProviderConfig and HarnessConfig for configuring agent sessions,
along with model alias resolution and context window lookup utilities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Model alias table
# ---------------------------------------------------------------------------

MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-6",  # noqa: EGG201 - canonical alias definition
    "sonnet": "claude-sonnet-4-5-20250514",  # noqa: EGG201 - canonical alias definition
    "haiku": "claude-haiku-4-5",  # noqa: EGG201 - canonical alias definition
}

# ---------------------------------------------------------------------------
# Context window sizes (tokens)
# ---------------------------------------------------------------------------

CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-6": 200_000,  # noqa: EGG201 - canonical model ID as dict key
    "claude-sonnet-4-5-20250514": 200_000,  # noqa: EGG201 - canonical model ID as dict key
    "claude-haiku-4-5": 200_000,  # noqa: EGG201 - canonical model ID as dict key
}

_DEFAULT_CONTEXT_WINDOW: int = 128_000

# ---------------------------------------------------------------------------
# Regex for parsing model specs like "opus[1m]" or "sonnet[200k]"
# ---------------------------------------------------------------------------

_MODEL_SPEC_RE = re.compile(r"^(?P<model>[A-Za-z0-9._-]+?)(?:\[(?P<size>\d+)(?P<unit>[km])\])?$")


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
    """Parse a model specification that may include an optional context size.  # noqa: EGG201

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


_HARNESS_UNSET = object()  # sentinel for detecting bare HarnessConfig()


@dataclass(init=False)
class HarnessConfig:
    """Top-level configuration for an egg harness session.

    Attributes:
        provider: LLM provider settings.
        max_turns: Maximum number of agent turns before the session is
            terminated.
        timeout: Hard wall-clock timeout for the session in seconds.
            Also accepted as ``timeout_seconds`` for backward compatibility.
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
        system_prompt: Optional system-level instructions for the agent.
    """

    provider: ProviderConfig | None
    max_turns: int
    timeout: int
    cwd: str | None
    env: dict[str, str] | None
    disallowed_tools: list[str] | None
    intercept_tools: bool
    compaction_threshold: float
    keep_recent_tokens: int
    system_prompt: str | None

    def __init__(
        self,
        provider: ProviderConfig | None = _HARNESS_UNSET,  # type: ignore[assignment]
        max_turns: int = 200,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        disallowed_tools: list[str] | None = None,
        intercept_tools: bool = True,
        compaction_threshold: float = 0.8,
        keep_recent_tokens: int = 20_000,
        system_prompt: str | None = None,
        *,
        timeout_seconds: int | None = None,
    ) -> None:
        # HarnessConfig() with no arguments at all is a programming error.
        if provider is _HARNESS_UNSET:
            # Check whether the caller passed *any* explicit keyword.  If all
            # positional/keyword args are still at their defaults we treat
            # this as a bare HarnessConfig() call and raise.
            _any_explicit = (
                max_turns != 200
                or timeout is not None
                or cwd is not None
                or env is not None
                or disallowed_tools is not None
                or intercept_tools is not True
                or compaction_threshold != 0.8
                or keep_recent_tokens != 20_000
                or system_prompt is not None
                or timeout_seconds is not None
            )
            if not _any_explicit:
                raise TypeError(
                    "HarnessConfig() requires at least one argument.  "
                    "Pass provider=... or other configuration parameters."
                )
            provider = None

        self.provider = provider
        self.max_turns = max_turns
        # timeout_seconds is an alias for timeout
        if timeout is not None:
            self.timeout = timeout
        elif timeout_seconds is not None:
            self.timeout = timeout_seconds
        else:
            self.timeout = 7200
        self.cwd = cwd
        self.env = env
        self.disallowed_tools = disallowed_tools
        self.intercept_tools = intercept_tools
        if not (0.0 < compaction_threshold <= 1.0):
            raise ValueError(
                f"compaction_threshold must be in (0.0, 1.0], got {compaction_threshold}"
            )
        self.compaction_threshold = compaction_threshold
        if max_turns < 1:
            raise ValueError(f"max_turns must be >= 1, got {max_turns}")
        if self.timeout < 1:
            raise ValueError(f"timeout must be >= 1, got {self.timeout}")
        self.keep_recent_tokens = keep_recent_tokens
        self.system_prompt = system_prompt

    @property
    def timeout_seconds(self) -> int:
        """Alias for :attr:`timeout`."""
        return self.timeout
