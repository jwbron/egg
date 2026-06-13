#!/usr/bin/env python3
"""Apply egg's LiteLLM prompt-cache and reasoning-stream patches at image-build time.

LiteLLM's stock Anthropic->OpenAI translation (the path Claude Code's
``/v1/messages`` requests take when routed at a non-Claude OpenRouter
backend) drops prompt-cache hits for Qwen/DeepSeek and mis-streams
reasoning models. Five independent gaps cause it; this script closes all
five by editing the installed ``litellm`` package in place, then
``config/litellm/Dockerfile`` bakes the result into the ``egg-litellm``
image.

The cache patches mirror the host-side ``relitellm`` script
(jwbron/dotfiles), which was validated empirically against
``litellm==1.86.2``: cache hit rate on Qwen via OpenRouter went 0% ->
~99.99%, ~10x input-cost cut on identical-prefix turns. The reasoning
patches mirror jwbron/litellm#4, validated against Kimi K2.7-Code via
OpenRouter. The image pins that same version (see the Dockerfile
``FROM``), so the needles below are known to match.

  1. ``CacheControlSupportedModels`` (openrouter/chat/transformation.py)
     add QWEN + DEEPSEEK so ``cache_control`` survives the OpenRouter
     handler's strip step. Without it every turn pays full input rate.
  2. ``_add_cache_control_to_target`` cache_control gate (anthropic adapter
     transformation.py) broaden ONLY that gate to qwen + deepseek so
     ``cache_control`` survives the Anthropic->OpenAI translation the
     ``/v1/messages`` endpoint forces. Without it patch 1 never sees
     ``cache_control`` (stripped earlier). We deliberately do NOT widen the
     shared ``is_anthropic_claude_model`` predicate: it also gates the
     thinking->reasoning_effort translation (``translate_thinking_for_model``
     / ``_translate_thinking_to_openai``), and widening it would forward an
     Anthropic-shaped ``thinking`` object verbatim to OpenRouter (which
     expects ``reasoning``). ``drop_params`` can't strip it because OpenRouter
     advertises ``thinking`` as a supported param, so the request would 400 or
     silently lose reasoning. Scoping the change to the cache_control call
     site keeps thinking translation correct.
  3. ``_add_system_message_to_messages`` billing-header filter (same
     adapter file) drops ``x-anthropic-billing-header:`` text blocks.
     Claude Code injects this as the FIRST system text block, ahead of
     the ``cache_control`` marker, carrying a per-request ``cch=<hash>``;
     that hash invalidates the prefix-cache key every turn, so
     ``cache_read_input_tokens`` stays 0 forever. The sibling
     ``messages/transformation.py`` path already filters it via
     ``_filter_billing_headers_from_system``; the adapter path missed it.
  4. ``_translate_streaming_openai_chunk_to_anthropic_content_block``
     (anthropic adapter transformation.py) add a ``reasoning_content``
     branch that opens a proper ``thinking`` content block. OpenRouter-style
     reasoning models stream ``delta.reasoning_content`` rather than
     Anthropic-native ``delta.thinking_blocks``; without this patch the
     block start is typed ``text`` while the deltas are
     ``thinking_delta`` — a malformed stream that clients (e.g. Claude
     Code) render as visible assistant text and feed back as assistant
     content on later turns.
  5. ``AnthropicStreamWrapper`` block-transition requeue (anthropic adapter
     streaming_iterator.py) preserve the trigger chunk's first delta on
     text/thinking block transitions, not just on ``input_json_delta``
     tool transitions. Without it the first reasoning token of every
     thinking block and the first answer token after thinking are silently
     dropped; a one-chunk answer can vanish entirely. Applied to both the
     sync ``__next__`` and async ``__anext__`` paths.

Idempotent: each patch detects whether it is already applied. Fails
loudly (non-zero exit) if a needle is missing, so a LiteLLM version bump
that moves the code surfaces at build time instead of silently shipping
an unpatched image that bills full input rate or drops reasoning tokens.
"""

import importlib.util
import os
import sys


def _litellm_roots() -> list[str]:
    """Every litellm package tree to patch.

    The official image ships TWO copies: the installed package in the venv
    (``/app/.venv/.../site-packages/litellm`` — what the ``litellm`` console
    script the entrypoint runs imports) and a source tree at ``/app/litellm``
    that would shadow it under a CWD-relative import (``python -m litellm``
    from ``/app``). Patch both so no launch path can silently fall back to an
    unpatched, full-input-rate copy. Bounded to find_spec's result plus the
    WORKDIR — no filesystem-wide scan."""
    roots: list[str] = []
    spec = importlib.util.find_spec("litellm")
    if spec is not None and spec.origin:
        roots.append(os.path.dirname(spec.origin))
    for base in ("/app", os.getcwd()):
        cand = os.path.join(base, "litellm")
        if os.path.isfile(os.path.join(cand, "__init__.py")):
            roots.append(cand)
    # De-dup by realpath, preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for r in roots:
        rp = os.path.realpath(r)
        if rp not in seen:
            seen.add(rp)
            out.append(r)
    if not out:
        raise SystemExit("litellm package not found — wrong base image?")
    return out


def _apply(path: str, present: str, needle: str, replacement: str, label: str) -> None:
    if not os.path.isfile(path):
        raise SystemExit(f"{label}: file not found: {path}")
    with open(path) as fh:
        src = fh.read()
    if present in src:
        print(f"{label}: already applied")
        return
    if needle not in src:
        raise SystemExit(f"{label}: marker not found in {path} — LiteLLM version drift?")
    with open(path, "w") as fh:
        fh.write(src.replace(needle, replacement, 1))
    print(f"{label}: applied")


F1 = "llms/openrouter/chat/transformation.py"
F2 = "llms/anthropic/experimental_pass_through/adapters/transformation.py"
F3 = "llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py"

# Every patch as a self-contained spec: (file, present marker, needle,
# replacement, label). Module-level so tests can apply them to a checked-in
# fixture and assert the resulting source without re-declaring the needles
# (no fixture/needle drift). ``_patch_root`` just walks this list.
PATCHES: list[dict[str, str]] = [
    # Patch 1 — CacheControlSupportedModels: add QWEN + DEEPSEEK.
    # Idempotency marker is the egg-specific comment, not ``QWEN = "qwen"``:
    # if a future LiteLLM adds QWEN natively but not DEEPSEEK, a generic marker
    # would see it "already applied" and silently ship without DEEPSEEK.
    {
        "file": F1,
        "present": "# egg cache patch. OpenRouter natively supports cache_control",
        "needle": '    ZAI = "z-ai"\n',
        "replacement": (
            '    ZAI = "z-ai"\n'
            "    # egg cache patch. OpenRouter natively supports cache_control\n"
            "    # for these providers — see\n"
            "    # https://openrouter.ai/docs/guides/best-practices/prompt-caching.\n"
            '    QWEN = "qwen"\n'
            '    DEEPSEEK = "deepseek"\n'
        ),
        "label": "Patch 1/5 (CacheControlSupportedModels)",
    },
    # Patch 2 — broaden ONLY the cache_control gate (not the shared
    # is_anthropic_claude_model predicate, which also gates thinking
    # translation — see module docstring). Touches the single call site in
    # _add_cache_control_to_target.
    {
        "file": F2,
        "present": "# egg cache patch. Broaden ONLY the cache_control gate",
        "needle": "        if cache_control and model and self.is_anthropic_claude_model(model):\n",
        "replacement": (
            "        # egg cache patch. Broaden ONLY the cache_control gate to\n"
            "        # cover the OpenRouter qwen/deepseek routes. Do NOT widen\n"
            "        # is_anthropic_claude_model itself: it also gates the\n"
            "        # thinking->reasoning_effort translation, and a global\n"
            "        # widen would forward Anthropic-shaped `thinking` verbatim\n"
            "        # to OpenRouter (which expects `reasoning`) — drop_params\n"
            "        # can't strip it because OpenRouter advertises `thinking`\n"
            "        # as supported, so the request would 400 / lose reasoning.\n"
            '        _model_lower = model.lower() if model else ""\n'
            "        if (\n"
            "            cache_control\n"
            "            and model\n"
            "            and (\n"
            "                self.is_anthropic_claude_model(model)\n"
            '                or "qwen" in _model_lower\n'
            '                or "deepseek" in _model_lower\n'
            "            )\n"
            "        ):\n"
        ),
        "label": "Patch 2/5 (cache_control gate)",
    },
    # Patch 3 — drop x-anthropic-billing-header during Anthropic->OpenAI translation.
    {
        "file": F2,
        "present": '"x-anthropic-billing-header:" filter (egg cache patch)',
        "needle": (
            "            for block in system_content:\n"
            '                if isinstance(block, dict) and block.get("type") == "text":\n'
            "                    text_block: Dict[str, Any] = {\n"
            '                        "type": "text",\n'
            '                        "text": block.get("text", ""),\n'
            "                    }\n"
        ),
        "replacement": (
            "            for block in system_content:\n"
            '                if isinstance(block, dict) and block.get("type") == "text":\n'
            '                    text = block.get("text", "")\n'
            '                    # "x-anthropic-billing-header:" filter (egg cache patch).\n'
            "                    # Claude Code injects this block ahead of the\n"
            "                    # cache_control marker with a per-request `cch=` hash;\n"
            "                    # leaving it in invalidates the prefix-cache key on\n"
            "                    # every turn, so cache_read_input_tokens stays 0\n"
            "                    # forever on OpenRouter Qwen/DeepSeek routes. The\n"
            "                    # sibling messages/transformation.py path filters it\n"
            "                    # via _filter_billing_headers_from_system; this\n"
            "                    # adapter path missed it.\n"
            '                    if text.startswith("x-anthropic-billing-header:"):\n'
            "                        continue\n"
            "                    text_block: Dict[str, Any] = {\n"
            '                        "type": "text",\n'
            '                        "text": text,\n'
            "                    }\n"
        ),
        "label": "Patch 3/5 (x-anthropic-billing-header filter)",
    },
    # Patch 4 — OpenRouter-style reasoning_content must open a thinking
    # content block, not fall through to a text block. The bare
    # ``thinking_blocks`` elif appears in two sibling functions; we anchor the
    # needle on the preceding text-block elif (``choice.delta.content is not
    # None ...``), which is unique to
    # _translate_streaming_openai_chunk_to_anthropic_content_block — the other
    # function's preceding branch is the ``tool_calls`` block. Without this
    # anchor, an upstream reorder could silently retarget the str.replace.
    {
        "file": F2,
        "present": "# egg reasoning patch: OpenRouter-style reasoning_content",
        "needle": (
            "            elif choice.delta.content is not None and len(choice.delta.content) > 0:\n"
            '                return "text", TextBlock(type="text", text="")\n'
            "            elif isinstance(choice, StreamingChoices) and hasattr(\n"
            '                choice.delta, "thinking_blocks"\n'
            "            ):\n"
        ),
        "replacement": (
            "            elif choice.delta.content is not None and len(choice.delta.content) > 0:\n"
            '                return "text", TextBlock(type="text", text="")\n'
            "            elif isinstance(choice, StreamingChoices) and getattr(\n"
            '                choice.delta, "reasoning_content", None\n'
            "            ):\n"
            "                # egg reasoning patch: OpenRouter-style reasoning_content\n"
            "                # streams must open a thinking content block. Without\n"
            "                # this, thinking_delta events are emitted inside a\n"
            "                # text-typed block, which clients render as visible\n"
            "                # assistant text and feed back as assistant content.\n"
            '                return "thinking", ChatCompletionThinkingBlock(\n'
            '                    type="thinking", thinking="", signature=""\n'
            "                )\n"
            "            elif isinstance(choice, StreamingChoices) and hasattr(\n"
            '                choice.delta, "thinking_blocks"\n'
            "            ):\n"
        ),
        "label": "Patch 4/5 (reasoning_content thinking block)",
    },
    # Patch 5a — sync __next__: don't drop the first delta on text or
    # thinking block transitions.
    {
        "file": F3,
        "present": "# egg reasoning patch (sync first-delta)",
        "needle": (
            "                    # Queue the sequence: content_block_stop -> content_block_start\n"
            "                    # For text blocks the trigger chunk is not emitted as a separate\n"
            "                    # delta because content_block_start carries the information.\n"
            "                    # For tool_use blocks we must also emit the trigger chunk's delta\n"
            "                    # when it carries input_json_delta data, because some providers\n"
            "                    # (e.g. xAI, Gemini) include tool arguments in the same streaming\n"
            "                    # chunk as the function name/id.\n"
            "\n"
            "                    # 1. Stop current content block\n"
            "                    self.chunk_queue.append(\n"
            "                        {\n"
            '                            "type": "content_block_stop",\n'
            '                            "index": max(self.current_content_block_index - 1, 0),\n'
            "                        }\n"
            "                    )\n"
            "\n"
            "                    # 2. Start new content block\n"
            "                    self.chunk_queue.append(\n"
            "                        {\n"
            '                            "type": "content_block_start",\n'
            '                            "index": self.current_content_block_index,\n'
            '                            "content_block": self.current_content_block_start,\n'
            "                        }\n"
            "                    )\n"
            "\n"
            "                    # 3. If the trigger chunk carries tool argument data, queue it\n"
            "                    # so the input_json_delta is not silently dropped.\n"
            "                    if (\n"
            '                        processed_chunk.get("type") == "content_block_delta"\n'
            '                        and isinstance(processed_chunk.get("delta"), dict)\n'
            '                        and processed_chunk["delta"].get("type") == "input_json_delta"\n'
            '                        and processed_chunk["delta"].get("partial_json")\n'
            "                    ):\n"
            "                        self.chunk_queue.append(processed_chunk)\n"
        ),
        "replacement": (
            "                    # Queue the sequence: content_block_stop -> content_block_start,\n"
            "                    # then re-queue the trigger chunk's delta when it carries payload\n"
            "                    # the new content_block_start doesn't already include (see step 3).\n"
            "                    # egg reasoning patch (sync first-delta)\n"
            "\n"
            "                    # 1. Stop current content block\n"
            "                    self.chunk_queue.append(\n"
            "                        {\n"
            '                            "type": "content_block_stop",\n'
            '                            "index": max(self.current_content_block_index - 1, 0),\n'
            "                        }\n"
            "                    )\n"
            "\n"
            "                    # 2. Start new content block\n"
            "                    self.chunk_queue.append(\n"
            "                        {\n"
            '                            "type": "content_block_start",\n'
            '                            "index": self.current_content_block_index,\n'
            '                            "content_block": self.current_content_block_start,\n'
            "                        }\n"
            "                    )\n"
            "\n"
            "                    # 3. If the trigger chunk itself carries delta payload not\n"
            "                    # already embedded in the new content_block_start, queue it\n"
            "                    # so the first delta of the block isn't silently dropped:\n"
            "                    # - input_json_delta: some providers (e.g. xAI, Gemini)\n"
            "                    #   include tool arguments in the same streaming chunk as\n"
            "                    #   the function name/id.\n"
            "                    # - text_delta: the text block start is always empty, so\n"
            "                    #   the trigger chunk's text is the block's first token.\n"
            "                    # - thinking_delta: same, but only when the block start\n"
            "                    #   doesn't already embed the thinking content (it does\n"
            "                    #   for providers that send Anthropic-native\n"
            "                    #   thinking_blocks).\n"
            '                    if processed_chunk.get("type") == "content_block_delta" and isinstance(\n'
            '                        processed_chunk.get("delta"), dict\n'
            "                    ):\n"
            '                        trigger_delta = processed_chunk["delta"]\n'
            '                        trigger_delta_type = trigger_delta.get("type")\n'
            "                        if (\n"
            "                            (\n"
            '                                trigger_delta_type == "input_json_delta"\n'
            '                                and trigger_delta.get("partial_json")\n'
            "                            )\n"
            "                            or (\n"
            '                                trigger_delta_type == "text_delta"\n'
            '                                and trigger_delta.get("text")\n'
            "                            )\n"
            "                            or (\n"
            '                                trigger_delta_type == "thinking_delta"\n'
            '                                and trigger_delta.get("thinking")\n'
            '                                and not self.current_content_block_start.get("thinking")\n'
            "                            )\n"
            "                        ):\n"
            "                            self.chunk_queue.append(processed_chunk)\n"
        ),
        "label": "Patch 5/5a (sync first-delta requeue)",
    },
    # Patch 5b — async __anext__: same first-delta preservation.
    {
        "file": F3,
        "present": "# egg reasoning patch (async first-delta)",
        "needle": (
            "                        # Queue the sequence: content_block_stop -> content_block_start\n"
            "                        # For text blocks the trigger chunk is not emitted as a separate\n"
            "                        # delta because content_block_start carries the information.\n"
            "                        # For tool_use blocks we must also emit the trigger chunk's delta\n"
            "                        # when it carries input_json_delta data, because some providers\n"
            "                        # (e.g. xAI, Gemini) include tool arguments in the same streaming\n"
            "                        # chunk as the function name/id.\n"
            "\n"
            "                        # 1. Stop current content block\n"
            "                        self.chunk_queue.append(\n"
            "                            {\n"
            '                                "type": "content_block_stop",\n'
            '                                "index": max(self.current_content_block_index - 1, 0),\n'
            "                            }\n"
            "                        )\n"
            "                        self.chunk_queue.append(\n"
            "                            {\n"
            '                                "type": "content_block_start",\n'
            '                                "index": self.current_content_block_index,\n'
            '                                "content_block": self.current_content_block_start,\n'
            "                            }\n"
            "                        )\n"
            "\n"
            "                        # 3. If the trigger chunk carries tool argument data, queue it\n"
            "                        # so the input_json_delta is not silently dropped.\n"
            "                        if (\n"
            '                            processed_chunk.get("type") == "content_block_delta"\n'
            '                            and isinstance(processed_chunk.get("delta"), dict)\n'
            '                            and processed_chunk["delta"].get("type")\n'
            '                            == "input_json_delta"\n'
            '                            and processed_chunk["delta"].get("partial_json")\n'
            "                        ):\n"
            "                            self.chunk_queue.append(processed_chunk)\n"
        ),
        "replacement": (
            "                        # Queue the sequence: content_block_stop -> content_block_start,\n"
            "                        # then re-queue the trigger chunk's delta when it carries payload\n"
            "                        # the new content_block_start doesn't already include (see step 3).\n"
            "                        # egg reasoning patch (async first-delta)\n"
            "\n"
            "                        # 1. Stop current content block\n"
            "                        self.chunk_queue.append(\n"
            "                            {\n"
            '                                "type": "content_block_stop",\n'
            '                                "index": max(self.current_content_block_index - 1, 0),\n'
            "                            }\n"
            "                        )\n"
            "                        self.chunk_queue.append(\n"
            "                            {\n"
            '                                "type": "content_block_start",\n'
            '                                "index": self.current_content_block_index,\n'
            '                                "content_block": self.current_content_block_start,\n'
            "                            }\n"
            "                        )\n"
            "\n"
            "                        # 3. If the trigger chunk itself carries delta payload\n"
            "                        # not already embedded in the new content_block_start,\n"
            "                        # queue it so the first delta of the block isn't\n"
            "                        # silently dropped:\n"
            "                        # - input_json_delta: some providers (e.g. xAI, Gemini)\n"
            "                        #   include tool arguments in the same streaming chunk\n"
            "                        #   as the function name/id.\n"
            "                        # - text_delta: the text block start is always empty,\n"
            "                        #   so the trigger chunk's text is the block's first\n"
            "                        #   token.\n"
            "                        # - thinking_delta: same, but only when the block start\n"
            "                        #   doesn't already embed the thinking content (it does\n"
            "                        #   for providers that send Anthropic-native\n"
            "                        #   thinking_blocks).\n"
            '                        if processed_chunk.get("type") == "content_block_delta" and isinstance(\n'
            '                            processed_chunk.get("delta"), dict\n'
            "                        ):\n"
            '                            trigger_delta = processed_chunk["delta"]\n'
            '                            trigger_delta_type = trigger_delta.get("type")\n'
            "                            if (\n"
            "                                (\n"
            '                                    trigger_delta_type == "input_json_delta"\n'
            '                                    and trigger_delta.get("partial_json")\n'
            "                                )\n"
            "                                or (\n"
            '                                    trigger_delta_type == "text_delta"\n'
            '                                    and trigger_delta.get("text")\n'
            "                                )\n"
            "                                or (\n"
            '                                    trigger_delta_type == "thinking_delta"\n'
            '                                    and trigger_delta.get("thinking")\n'
            '                                    and not self.current_content_block_start.get("thinking")\n'
            "                                )\n"
            "                            ):\n"
            "                                self.chunk_queue.append(processed_chunk)\n"
        ),
        "label": "Patch 5/5b (async first-delta requeue)",
    },
]


def _patch_root(root: str) -> None:
    print(f"== patching {root}")
    for spec in PATCHES:
        _apply(
            os.path.join(root, spec["file"]),
            present=spec["present"],
            needle=spec["needle"],
            replacement=spec["replacement"],
            label=spec["label"],
        )


def main() -> None:
    for root in _litellm_roots():
        _patch_root(root)
    print("egg LiteLLM cache patches: done")


if __name__ == "__main__":
    sys.exit(main())
