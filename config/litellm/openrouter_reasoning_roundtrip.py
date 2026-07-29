"""Carry prior-turn assistant reasoning back to OpenRouter.

egg's primary route is ``/v1/messages``: Claude Code -> egg-gateway -> LiteLLM
-> OpenRouter. On the way in, LiteLLM's Anthropic adapter turns each assistant
``thinking`` content block into an entry on
``assistant_message["thinking_blocks"]`` (``_translate_anthropic_messages_to_openai``
in ``llms/anthropic/experimental_pass_through/adapters/transformation.py``).
Nothing on the OpenRouter **request** path then reads that field: stock
``llms/openrouter/chat/transformation.py`` mentions reasoning exactly once, on
the *response* side (``reasoning`` -> ``reasoning_content`` on streaming
deltas), and ``reasoning_details`` appears nowhere in ``llms/`` or
``litellm_core_utils/`` at all. So ``thinking_blocks`` rides to the provider as
a field no one reads, and every historical assistant turn arrives carrying no
reasoning.

For a model whose chat template re-renders prior thinking that is not merely a
lost optimisation, it is a malformed history. Poolside Laguna renders
``'<think>' + message.reasoning|message.reasoning_content + '</think>'`` for
every previous assistant turn, so each one arrives as a literal empty
``<think></think>``; Poolside's model card warns that this degrades follow-up
behaviour. The measured cost in egg was a livelock whose per-turn prompt growth
was exactly +243 tokens, leaving no budget for reasoning content.

This module maps the blocks the adapter produced onto the field OpenRouter
actually reads. OpenRouter accepts ``reasoning``, ``reasoning_content`` and
``reasoning_details`` interchangeably on an assistant message and documents
this for exactly this multi-turn tool-calling case; Poolside's template reads
``message.reasoning`` / ``message.reasoning_content``. We send the plain string
form, which satisfies both: ``reasoning_details`` exists to carry encrypted or
summarised blocks, and we have neither.

Three boundaries are deliberate and worth stating outright.

**The adapter's "no thinking" sentinel is not a bad shape.**
``ChatCompletionAssistantMessage`` is a ``TypedDict``, so the adapter's
``thinking_blocks=(blocks if len(blocks) > 0 else None)`` leaves the key
*present and None* on every assistant turn that reasoned about nothing — the
majority of turns on any route, and every turn on a route that returns no
reasoning at all. That is the dominant input to this module, not an edge case:
it is read as "nothing to map", the key is dropped, and no field is emitted.

**Signature-verifying providers are out of scope.** For an Anthropic or Google
model reached *through* OpenRouter, the ``signature`` on a thinking block is
not decoration — it is what the upstream verifies when prior thinking is
replayed on a tool-calling turn, and OpenRouter's own docs carry it inside
``reasoning_details`` with "pass back unmodified; you cannot rearrange or
modify the sequence of these blocks". Flattening N ordered blocks into one
string and discarding their signatures is precisely what those routes must not
receive, so this module declines to rewrite them at all and leaves the message
byte-identical to what stock LiteLLM would have sent — a known-working state,
since stock has never mapped this field. egg pins no such slug today, but
``CacheControlSupportedModels`` in the patched file already carries ``CLAUDE``
and ``GEMINI``, so the route is anticipated rather than hypothetical. Emitting
a proper ``reasoning_details`` for them is a separate change with a separate
wire shape to verify.

**A ``reasoning_content`` the caller already set wins.** litellm's own response
objects carry both fields, so any client echoing an assistant message back
sends both. One rule covers both branches: a non-empty ``reasoning_content``
already on the message is never overwritten, whatever the blocks yield.

The gap is upstream's, not egg's: it predates every egg and ``jwbron/litellm``
change, and the fork's OpenRouter work (jwbron/litellm#8) is confined to
``get_supported_openai_params``, which acts on ``optional_params`` and cannot
reach a message field.

Note the direction. Patches 4, 5a and 5b are response-path (provider ->
client): they fix how reasoning is *streamed back*. This is request-path
(client -> provider). They are adjacent, not the same thing.

Set ``LITELLM_OPENROUTER_REASONING_ROUNDTRIP=0`` to restore stock behaviour
(``thinking_blocks`` transmitted, no reasoning field) without rebuilding the
image — the same runtime escape hatch Patches 7 and 9 carry, and for the same
reason: this one changes the outgoing body on every OpenRouter call, and its
correctness rests on provider-side behaviour verified against two models. A
value that is neither a recognised on nor off spelling warns once and takes the
default rather than being read as off.
"""

import os
from typing import Any

ENV_VAR = "LITELLM_OPENROUTER_REASONING_ROUNDTRIP"

_TRUTHY = ("1", "true", "yes", "on")
# An empty value counts as off, matching ``anthropic_thinking_policy``: an env
# var set to nothing is an operator clearing a knob, not a typo.
_FALSY = ("0", "false", "no", "off", "")

# Blocks the adapter can put on ``thinking_blocks``. ``redacted_thinking``
# carries an opaque ``data`` payload rather than text (see
# ``ChatCompletionRedactedThinkingBlock``); there is no plaintext to hand a
# provider that wants a string, so those blocks contribute nothing.
_THINKING_BLOCK_TYPE = "thinking"

# The field the adapter writes and the field OpenRouter reads.
_SOURCE_FIELD = "thinking_blocks"
_TARGET_FIELD = "reasoning_content"

# Blocks are separate thoughts, not a single stream that happens to be split:
# the adapter emits one per ``thinking`` content block, and Claude Code
# interleaves them with text and tool_use inside one assistant turn. Joining on
# "" runs the last word of one into the first word of the next, which is how a
# ``<think>`` re-render would then read it.
_BLOCK_SEPARATOR = "\n"

# Substrings that mark a provider whose reasoning is signature-verified when
# replayed. Matched against the slug OpenRouter routes on
# (``anthropic/claude-...``, ``google/gemini-...``), so the test is a substring
# rather than a prefix: litellm may or may not have stripped its own
# ``openrouter/`` prefix by the time ``transform_request`` runs.
_SIGNATURE_VERIFYING_MARKERS = ("anthropic", "claude", "google", "gemini")

# Diagnostics already emitted. Bounded by construction: one entry per distinct
# key below, and the keys are literals in this file. Needed because everything
# here sits on the per-request path.
_WARNED: set[str] = set()


def _log(level: str, message: str, *args: object) -> bool:
    """Log via litellm's ``verbose_logger``, deferring the import.

    Kept out of module scope so this file stays importable — and therefore unit
    testable — where litellm is not installed. Never raises: a diagnostic must
    not be able to break a request.

    Returns whether the call completed without raising, not whether a record
    reached a handler; see the same note in ``openrouter_capabilities._log``.
    """
    try:
        from litellm._logging import verbose_logger

        getattr(verbose_logger, level)(message, *args)
        return True
    except Exception:  # noqa: BLE001 - diagnostics must never break a request
        return False


def _warn_once(key: str, message: str, *args: object) -> None:
    """Warn at most once per ``key`` for the life of the process.

    Recorded only once the emit did not raise, for the reason given in
    ``openrouter_capabilities._warn_env_once``: ``_log`` swallows its own
    failure, so recording first would let a logger that is not yet in place on
    the first request suppress the warning permanently.
    """
    if key in _WARNED:
        return
    if _log("warning", message, *args):
        _WARNED.add(key)


def is_enabled() -> bool:
    """Whether the round-trip mapping should run at all.

    Defaults to True — the mapping is the patch. ``0`` (and the other off
    spellings) backs it out of a live cluster without an image rebuild.
    """
    raw = os.getenv(ENV_VAR)
    if raw is None:
        return True
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    _warn_once(
        f"env:{value}",
        "openrouter reasoning round-trip: %s=%r is neither an on (%s) nor an "
        "off (%s) spelling; leaving the mapping enabled, which is also the "
        "default — set %s=0 if you meant to disable it.",
        ENV_VAR,
        raw,
        ", ".join(_TRUTHY),
        ", ".join(v for v in _FALSY if v),
        ENV_VAR,
    )
    return True


def provider_verifies_reasoning_signatures(model: Any) -> bool:
    """Whether ``model``'s upstream re-verifies replayed reasoning.

    See the docstring above: for those routes the signature and the block
    boundaries are load-bearing, and the plain-string form this module emits
    would destroy both. A non-string model is not a slug we can reason about,
    so it is treated as ordinary rather than as a match.
    """
    if not isinstance(model, str):
        return False
    slug = model.lower()
    return any(marker in slug for marker in _SIGNATURE_VERIFYING_MARKERS)


def _extract_reasoning_text(blocks: Any) -> str:
    """Concatenate the plaintext of ``blocks``, in order.

    Non-dict entries, entries of any type other than ``thinking``, entries
    whose ``thinking`` is not a string, and entries whose text is blank are
    skipped individually rather than failing the whole message: one malformed
    block should not cost us the reasoning from its well-formed siblings, and a
    blank one should not contribute a bare separator. A ``blocks`` that is not
    a list at all is a shape we do not understand, and the caller leaves the
    message untouched.
    """
    if not isinstance(blocks, list):
        raise TypeError(f"{_SOURCE_FIELD} is {type(blocks).__name__}, expected list")
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != _THINKING_BLOCK_TYPE:
            continue
        text = block.get(_THINKING_BLOCK_TYPE)
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return _BLOCK_SEPARATOR.join(parts)


def map_thinking_blocks_to_reasoning_content(messages: Any, model: Any = None) -> Any:
    """Return ``messages`` with assistant reasoning moved to the wire field.

    For each assistant message carrying ``thinking_blocks``: drop that field so
    no unknown key is transmitted, and set ``reasoning_content`` to the
    concatenated plaintext when there is any and the message does not already
    carry a non-empty ``reasoning_content`` of its own. A message whose blocks
    yield only whitespace — or nothing at all, which is the adapter's
    ``thinking_blocks: None`` sentinel and its ``redacted_thinking``-only case
    — loses the field and gains nothing, which is what OpenRouter would have
    received anyway minus the noise.

    User, system and tool messages are returned untouched, as is any assistant
    message with no ``thinking_blocks`` key. ``messages`` is returned unchanged
    in full when the knob is off or when ``model`` names a provider that
    re-verifies replayed reasoning. Input is not mutated: touched messages are
    shallow-copied.

    Never raises. A message this function cannot make sense of is passed
    through exactly as it arrived, so the worst case is stock behaviour.
    """
    if not isinstance(messages, list):
        return messages
    if not is_enabled():
        return messages
    if provider_verifies_reasoning_signatures(model):
        return messages

    out: list[Any] = []
    for message in messages:
        try:
            if not isinstance(message, dict):
                out.append(message)
                continue
            if message.get("role") != "assistant" or _SOURCE_FIELD not in message:
                out.append(message)
                continue

            blocks = message.get(_SOURCE_FIELD)
            if blocks is None:
                # The adapter's own "this turn produced no thinking" sentinel,
                # not a shape we failed to parse. Reading it as the latter
                # would leave ``thinking_blocks: null`` on the wire for the
                # majority of assistant turns AND route ordinary traffic
                # through the branch reserved for corruption below.
                blocks = []
            text = _extract_reasoning_text(blocks)

            updated: dict[str, Any] = dict(message)
            updated.pop(_SOURCE_FIELD, None)
            existing = updated.get(_TARGET_FIELD)
            if isinstance(existing, str) and existing.strip():
                # The caller stated this field outright; the blocks are a
                # second rendering of the same reasoning. One rule, applied
                # whether or not the blocks yield text: we do not overwrite it.
                pass
            elif text.strip():
                # Whitespace-only reasoning is not reasoning; emitting it would
                # put an empty <think></think> in the rendered prompt, which is
                # the exact failure this patch exists to remove.
                updated[_TARGET_FIELD] = text
            out.append(updated)
        except Exception:  # noqa: BLE001 - a request must survive a bad block
            # With the sentinel handled above this branch means a genuinely
            # unrecognised shape, so it is worth exactly one line: silence here
            # is how a mapping that quietly stopped working would look.
            _warn_once(
                "unmappable-message",
                "openrouter reasoning round-trip: could not map %r on an "
                "assistant message; sending it as it arrived (stock "
                "behaviour). Reasoning from prior turns may not reach the "
                "provider. Logged once per proxy process.",
                _SOURCE_FIELD,
            )
            out.append(message)
    return out


__all__ = [
    "ENV_VAR",
    "is_enabled",
    "map_thinking_blocks_to_reasoning_content",
    "provider_verifies_reasoning_signatures",
]
