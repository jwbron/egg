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
summarised blocks, and we have neither. Anthropic ``signature`` values are
meaningless to OpenRouter and are not forwarded.

The gap is upstream's, not egg's: it predates every egg and ``jwbron/litellm``
change, and the fork's OpenRouter work (jwbron/litellm#8) is confined to
``get_supported_openai_params``, which acts on ``optional_params`` and cannot
reach a message field.

Note the direction. Patches 4, 5a and 5b are response-path (provider ->
client): they fix how reasoning is *streamed back*. This is request-path
(client -> provider). They are adjacent, not the same thing.
"""

from typing import Any

# Blocks the adapter can put on ``thinking_blocks``. ``redacted_thinking``
# carries an opaque ``data`` payload rather than text (see
# ``ChatCompletionRedactedThinkingBlock``); there is no plaintext to hand a
# provider that wants a string, so those blocks contribute nothing.
_THINKING_BLOCK_TYPE = "thinking"

# The field the adapter writes and the field OpenRouter reads.
_SOURCE_FIELD = "thinking_blocks"
_TARGET_FIELD = "reasoning_content"


def _extract_reasoning_text(blocks: Any) -> str:
    """Concatenate the plaintext of ``blocks``, in order.

    Non-dict entries, entries of any type other than ``thinking``, and entries
    whose ``thinking`` is not a string are skipped individually rather than
    failing the whole message: one malformed block should not cost us the
    reasoning from its well-formed siblings. A ``blocks`` that is not a list at
    all is a shape we do not understand, and the caller leaves the message
    untouched.
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
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def map_thinking_blocks_to_reasoning_content(messages: Any) -> Any:
    """Return ``messages`` with assistant reasoning moved to the wire field.

    For each assistant message carrying ``thinking_blocks``: drop that field so
    no unknown key is transmitted, and set ``reasoning_content`` to the
    concatenated plaintext when there is any. A message whose blocks yield only
    whitespace (or nothing at all, e.g. ``redacted_thinking`` only) loses the
    field and gains nothing, which is what OpenRouter would have received
    anyway minus the noise.

    User, system and tool messages are returned untouched, as is any assistant
    message with no ``thinking_blocks`` key. Input is not mutated: touched
    messages are shallow-copied.

    Never raises. A message this function cannot make sense of is passed
    through exactly as it arrived, so the worst case is stock behaviour.
    """
    if not isinstance(messages, list):
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

            text = _extract_reasoning_text(message.get(_SOURCE_FIELD))

            updated: dict[str, Any] = dict(message)
            updated.pop(_SOURCE_FIELD, None)
            # Whitespace-only reasoning is not reasoning; emitting it would put
            # an empty <think></think> in the rendered prompt, which is the
            # exact failure this patch exists to remove.
            if text.strip():
                updated[_TARGET_FIELD] = text
            out.append(updated)
        except Exception:  # noqa: BLE001 - a request must survive a bad block
            out.append(message)
    return out


__all__ = ["map_thinking_blocks_to_reasoning_content"]
