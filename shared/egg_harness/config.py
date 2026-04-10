"""Configuration for the egg harness."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Literal

# Model alias mapping (HITL decision: haiku -> claude-haiku-4-5, NOT 3.5)
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-20250514",
    "sonnet": "claude-sonnet-4-20250514",
    "haiku": "claude-haiku-4-5-20250414",
}

# Model context window sizes (tokens)
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-20250514": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-haiku-4-5-20250414": 200_000,
}

# Extended context (1M beta) models
EXTENDED_CONTEXT_MODELS: dict[str, int] = {
    "claude-opus-4-20250514": 1_000_000,
}

# Regex to parse model[NNk] or model[NNm] suffix
_SUFFIX_RE = re.compile(r"^(.+)\[(\d+)([km])\]$")


@dataclass
class ProviderConfig:
    """LLM provider configuration."""

    provider: Literal["anthropic", "openai-compatible"] = "anthropic"
    model: str = "opus"
    endpoint: str | None = None  # For openai-compatible
    api_key_env: str | None = None  # Env var name for API key
    max_tokens: int = 16384

    def resolve_model(self) -> str:
        """Resolve model alias to full model ID. Handles suffix syntax like 'opus[1m]'."""
        model = self.model
        max_tokens_override = None

        # Parse suffix syntax: opus[1m], sonnet[200k]
        match = _SUFFIX_RE.match(model)
        if match:
            base, num, unit = match.group(1), int(match.group(2)), match.group(3)
            multiplier = 1_000_000 if unit == "m" else 1_000
            max_tokens_override = num * multiplier
            model = base

        # Resolve alias
        resolved = MODEL_ALIASES.get(model, model)

        if max_tokens_override is not None:
            self.max_tokens = max_tokens_override

        return resolved

    def get_context_window(self) -> int:
        """Get context window size for resolved model."""
        resolved = self.resolve_model()
        # Check extended context first
        if self.max_tokens > 200_000 and resolved in EXTENDED_CONTEXT_MODELS:
            return EXTENDED_CONTEXT_MODELS[resolved]
        return MODEL_CONTEXT_WINDOWS.get(resolved, 200_000)


@dataclass
class HarnessConfig:
    """Top-level harness configuration."""

    provider: ProviderConfig = field(default_factory=ProviderConfig)
    max_turns: int | None = None
    timeout: int = 7200  # 2 hours
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    system_prompt: str | None = None

    # Compaction
    compaction_threshold: float = 0.80  # Trigger at 80% context usage
    max_compactions_per_n_turns: int = 2  # Max compactions in N turns
    compaction_window_turns: int = 5  # The N in above

    # Session persistence
    session_file: str | None = None  # Path for session persistence
    auto_save_interval: int = 60  # seconds

    # Permission / interception
    disallowed_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> HarnessConfig:
        """Create config from environment variables."""
        private_mode = os.environ.get("EGG_PRIVATE_MODE", "").lower() in ("true", "1")
        disallowed = ["WebFetch", "WebSearch"] if private_mode else []

        model = os.environ.get("EGG_MODEL", "opus")
        endpoint = os.environ.get("EGG_LLM_ENDPOINT")
        provider_type = (
            "openai-compatible" if endpoint and "anthropic" not in endpoint else "anthropic"
        )

        return cls(
            provider=ProviderConfig(
                provider=provider_type,
                model=model,
                endpoint=endpoint,
            ),
            disallowed_tools=disallowed,
        )
