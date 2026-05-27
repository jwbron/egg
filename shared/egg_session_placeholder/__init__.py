"""Session-token placeholder codec for the gateway's ``/v1/messages`` proxy.

Claude Code controls its own ``x-api-key`` / ``Authorization`` headers and
will not send the gateway's ``EGG_SESSION_TOKEN``. To keep ``/v1/messages``
token-authed like every other gateway endpoint, the orchestrator wraps the
real session token in a ``sk-ant-oat01-`` placeholder that satisfies Claude
Code's local format check; the gateway parses the placeholder back into the
session token before injecting upstream credentials. See issue #2829.

The placeholder shape is internal contract between the sandbox and the
gateway. The prefix carries enough self-description that a reader (or a
redaction scanner) recognises it on sight; the suffix is the verbatim
session token. Format::

    sk-ant-oat01-PROXY-INJECTED-egg-session-<session_token>
"""

from __future__ import annotations

PLACEHOLDER_PREFIX = "sk-ant-oat01-PROXY-INJECTED-egg-session-"


def to_placeholder(session_token: str) -> str:
    """Wrap *session_token* in the placeholder envelope Claude Code accepts."""
    return f"{PLACEHOLDER_PREFIX}{session_token}"


def from_placeholder(value: str | None) -> str | None:
    """Return the session token embedded in *value*, or ``None`` if absent.

    Accepts the raw header value (``x-api-key``) or a ``Bearer <value>``
    string (``Authorization``). Any value that doesn't carry the
    placeholder prefix returns ``None`` so callers can fall through to
    the legacy (non-placeholder) path.
    """
    if not value:
        return None
    # RFC 7235 §2.1: the auth scheme name is case-insensitive. Claude Code
    # always sends ``Bearer `` capitalized, but accept any case so a future
    # client that ships lowercase ``bearer `` doesn't silently fall through
    # to IP-keyed lookup (and then fail closed on a non-IP-registered agent).
    if value[:7].lower() == "bearer ":
        value = value[7:]
    if value.startswith(PLACEHOLDER_PREFIX):
        return value[len(PLACEHOLDER_PREFIX) :] or None
    return None


__all__ = ["PLACEHOLDER_PREFIX", "to_placeholder", "from_placeholder"]
