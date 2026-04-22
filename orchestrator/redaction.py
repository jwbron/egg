"""Secret redaction helpers for operator-facing diagnostic output.

Importable by the new deployment/agent diagnose skills and any tool that
emits raw environment or log-tail content. The goal is to keep skill
output usable for debugging while guaranteeing that known-sensitive
keys and credential-shaped tokens never leave the orchestrator.

Two public helpers:

- :func:`redact_env`: takes a ``{name: value}`` env dict and returns a
  copy where values for protected names are replaced with ``***``.
- :func:`redact_log_tail`: takes a log string and scrubs any Bearer JWT
  or generic API-key-shape substring.

The protected-name set is defined here (with a fallback when
:data:`orchestrator.kubernetes_spawner._PROTECTED_ENV_KEYS` is
unavailable) and exported as :data:`PROTECTED_ENV_KEYS` so other
modules share a single source of truth.  On top of that base set
the module adds:

- the four "well-known" credentials agents may receive
  (``GITHUB_TOKEN``, ``GH_TOKEN``, ``ANTHROPIC_API_KEY``,
  ``CLAUDE_API_KEY``);
- any name ending in ``_TOKEN``, ``_SECRET`` or ``_KEY`` (case-insensitive).

The module is deliberately free of external dependencies so it imports
cleanly from skills that may run before the full orchestrator python
path is configured.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# Import the orchestrator's master denylist. Guarded because this module
# is imported both from the orchestrator (where ``kubernetes_spawner`` is
# available) and from skills / tests that may run under a bare Python
# path.
try:
    from kubernetes_spawner import _PROTECTED_ENV_KEYS  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - fallback for stripped test envs
    _PROTECTED_ENV_KEYS = frozenset(
        {
            "EGG_SESSION_TOKEN",
            "GATEWAY_URL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "EGG_ORCHESTRATOR_URL",
            "EGG_LIFECYCLE_SECRET",
        }
    )


# Public alias so other modules can import the denylist from here
# instead of duplicating the kubernetes_spawner fallback.
PROTECTED_ENV_KEYS: frozenset[str] = _PROTECTED_ENV_KEYS

# Additional named credentials that the denylist does not already cover.
# Names are matched case-insensitively by :func:`redact_env`.
_EXTRA_PROTECTED_NAMES: frozenset[str] = frozenset(
    {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
    }
)

# Suffix patterns that mark a value as secret-shaped. Applied
# case-insensitively.
_PROTECTED_SUFFIXES: tuple[str, ...] = ("_TOKEN", "_SECRET", "_KEY")

# Placeholder used for redacted values. Short on purpose so multi-line
# diagnostic output stays legible.
REDACTION_PLACEHOLDER: str = "***"

# Regex for Bearer JWTs and bare JWTs (three base64url segments).  The
# ``Bearer`` prefix is optional because the tokens sometimes show up bare
# in log lines (``Authorization: ey...``).  Five+ chars per segment avoid
# overlapping ids.
_BEARER_JWT_RE = re.compile(
    r"(?:Bearer\s+)?ey[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}",
    re.IGNORECASE,
)

# Heuristic for key-shaped tokens in log lines. Matches an ``sk-``,
# ``ghp_``, ``ghu_``, ``gho_``, ``ghs_`` or ``sk_live_`` prefix followed
# by a long base64-ish run. Chosen to be narrow: operators still want
# to see short commit SHAs, container IDs, etc. in their logs.
_API_KEY_SHAPE_RE = re.compile(
    r"""
    \b
    (?:
        sk-[A-Za-z0-9_\-]{20,}
      | sk_live_[A-Za-z0-9]{20,}
      | ghp_[A-Za-z0-9]{20,}
      | ghu_[A-Za-z0-9]{20,}
      | gho_[A-Za-z0-9]{20,}
      | ghs_[A-Za-z0-9]{20,}
      | ghr_[A-Za-z0-9]{20,}
    )
    """,
    re.VERBOSE,
)


def _name_is_protected(name: str, extra: Iterable[str] = ()) -> bool:
    """Return True if *name* should have its value redacted.

    Matches exact, case-sensitive entries from
    :data:`_PROTECTED_ENV_KEYS` (they intentionally pin case for the
    lowercase HTTP_PROXY variants) plus case-insensitive extras and
    the ``_TOKEN``/``_SECRET``/``_KEY`` suffix heuristics.
    """
    if name in _PROTECTED_ENV_KEYS:
        return True

    upper = name.upper()
    if upper in _EXTRA_PROTECTED_NAMES:
        return True
    for protected in extra:
        if upper == protected.upper():
            return True

    return any(upper.endswith(suffix) for suffix in _PROTECTED_SUFFIXES)


def redact_env(
    env: Mapping[str, Any],
    *,
    extra_protected: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a copy of *env* with sensitive values replaced.

    Args:
        env: Mapping of env var names → values. Values are stringified
            before the redaction check so callers can pass mixed types.
        extra_protected: Optional additional names (case-insensitive)
            that should also have their values masked. Useful when the
            caller knows a custom key carries a secret.

    Returns:
        New dict where protected names have their value replaced by
        :data:`REDACTION_PLACEHOLDER`.  Non-protected names are passed
        through unchanged.  Original mapping is not mutated.
    """
    out: dict[str, Any] = {}
    for name, value in env.items():
        if _name_is_protected(name, extra_protected):
            out[name] = REDACTION_PLACEHOLDER
        else:
            out[name] = value
    return out


def redact_log_tail(text: str) -> str:
    """Return *text* with Bearer JWTs and known key shapes replaced.

    This is a best-effort pass aimed at log tails that the operator
    skills will emit into diagnostic reports.  It does not try to
    detect arbitrary high-entropy strings; callers should continue
    to rely on :func:`redact_env` for named credentials.
    """
    if not text:
        return text

    scrubbed = _BEARER_JWT_RE.sub(REDACTION_PLACEHOLDER, text)
    scrubbed = _API_KEY_SHAPE_RE.sub(REDACTION_PLACEHOLDER, scrubbed)
    return scrubbed


__all__ = [
    "PROTECTED_ENV_KEYS",
    "REDACTION_PLACEHOLDER",
    "redact_env",
    "redact_log_tail",
]
