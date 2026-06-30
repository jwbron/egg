"""Classification of non-retryable agent credential / quota failures (#3373).

An agent invocation that fails because its Claude credential is unusable — a
subscription weekly / usage limit, an expired or invalid OAuth token / API
key, an authentication (401) error, or an exhausted credit balance — cannot
recover by being retried: every respawn re-uses the same rejected credential.
Left unclassified, these failures look identical to a transient crash, so the
orchestrator burns its whole agent-invocation retry budget (the streak-to-10
in ``orchestrator.supervision_policy``) per role before it stops — turning a
fix-the-credential problem into a multi-hour silent stall.

This module is the single source of truth for that contract:

  * :data:`EX_AUTH_FATAL` — the process exit code the agent CLI
    (``python3 -m egg_agent``) returns on such a failure. It is distinct from
    the generic non-zero return code and is passed straight through the
    consensus wrapper to the k8s Job, so the orchestrator event loop can
    fast-fail the dedupe key (and raise a named, actionable alert) instead of
    retrying it.
  * :func:`is_auth_fatal_error` — the text classifier over an
    ``AgentResult.error`` string.

Deliberately conservative: only errors that are non-retryable *for the
credential's sake* match. Transient throttling — HTTP 429 / "rate limit" /
"overloaded" — is intentionally **absent** from the patterns below: those
recover on a retry and must stay on the normal backoff-and-respawn path. A
weekly / usage limit is matched on its own wording regardless of any HTTP
status it rides on, so it is caught even when the API delivers it as a 429.
"""

from __future__ import annotations

import re

# POSIX ``EX_NOPERM`` (sysexits.h): "permission denied". Reused here as the
# agent CLI's auth-fatal exit code. Must not collide with the consensus
# wrapper's reserved codes (64 ``EX_USAGE`` for a caller bug / 75 ``EX_TEMPFAIL``
# for an inconclusive freshness re-check — see ``orchestrator/consensus_wrapper.py``)
# nor the SIGTERM code (143) the kubernetes monitor treats as a clean stop.
EX_AUTH_FATAL = 77

# Case-insensitive patterns that mark a credential / quota failure a retry
# cannot fix. Kept narrow and specific on purpose (see the module docstring):
# every entry names an unambiguous non-retryable cause, so there is no need to
# suppress transient throttling — its signatures (rate limit / 429 /
# overloaded) simply do not appear here.
_AUTH_FATAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Subscription weekly / usage caps (the #3373 repro: "You've hit your
    # weekly limit · resets Jul 3, 5am (UTC)"). Anchored to the characteristic
    # stop phrasing ("hit your weekly limit", "weekly limit reached / exceeded
    # / resets") rather than the bare words — on the ``ResultMessage.is_error``
    # path ``result.error`` is the agent's own final text, so incidental prose
    # that merely mentions a "weekly limit" (e.g. an agent editing this very
    # file, #3373 re-review note 3) is far less likely to trip the classifier
    # than a real CLI stop message. NOT a plain "rate limit".
    re.compile(
        r"\b(?:hit|reached|exceeded|resets?)\b[^.\n]{0,40}\b(?:weekly|usage) limit\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:weekly|usage) limit\b[^.\n]{0,40}\b(?:reached|exceeded|resets?)\b",
        re.IGNORECASE,
    ),
    # Authentication / credential rejection (the Anthropic API error shapes).
    re.compile(r"authentication[_ ]error", re.IGNORECASE),
    re.compile(r"\binvalid[\s_-]*(?:x-)?api[\s_-]*key\b", re.IGNORECASE),
    re.compile(r"invalid bearer token", re.IGNORECASE),
    re.compile(r"oauth token (?:has )?expired", re.IGNORECASE),
    re.compile(r"could not resolve authentication", re.IGNORECASE),
    # Bare HTTP 401 (#3373 re-review note 4): the structured patterns above
    # only catch 401s carrying "authentication_error" / "could not resolve
    # authentication", so a plain ``HTTP 401 Unauthorized`` from the API would
    # slip through. A 401 status is unambiguously a credential rejection;
    # requiring the "unauthorized" word or an http/status lead-in keeps an
    # incidental "401" in agent prose from matching.
    re.compile(r"\b401\s+unauthorized\b", re.IGNORECASE),
    re.compile(r"\b(?:http|status)[\s_/]*401\b", re.IGNORECASE),
    # Exhausted billing — non-retryable until the account is topped up.
    re.compile(r"credit balance is too low", re.IGNORECASE),
)


def is_auth_fatal_error(text: str | None) -> bool:
    """Return ``True`` iff *text* names a non-retryable credential/quota failure.

    *text* is normally an :class:`egg_agent.result.AgentResult.error`. Returns
    ``False`` for an empty / ``None`` message and for any error whose wording
    does not match one of the narrow auth-fatal patterns — in particular a
    transient rate-limit / overload, which must keep retrying.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _AUTH_FATAL_PATTERNS)
