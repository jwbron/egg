#!/usr/bin/env python3
"""Apply egg's LiteLLM prompt-cache patches at image-build time.

LiteLLM's stock Anthropic->OpenAI translation (the path Claude Code's
``/v1/messages`` requests take when routed at a non-Claude OpenRouter
backend) drops prompt-cache hits for Qwen/DeepSeek. Three independent
gaps cause it; this script closes all three by editing the installed
``litellm`` package in place, then ``config/litellm/Dockerfile`` bakes
the result into the ``egg-litellm`` image.

The patches mirror the host-side ``relitellm`` script (jwbron/dotfiles),
which was validated empirically against ``litellm==1.86.2``: cache hit
rate on Qwen via OpenRouter went 0% -> ~99.99%, ~10x input-cost cut on
identical-prefix turns. The image pins that same version (see the
Dockerfile ``FROM``), so the needles below are known to match.

  1. ``CacheControlSupportedModels`` (openrouter/chat/transformation.py)
     add QWEN + DEEPSEEK so ``cache_control`` survives the OpenRouter
     handler's strip step. Without it every turn pays full input rate.
  2. ``is_anthropic_claude_model`` (anthropic adapter transformation.py)
     extend the gate to qwen + deepseek so ``cache_control`` survives the
     Anthropic->OpenAI translation the ``/v1/messages`` endpoint forces.
     Without it patch 1 never sees ``cache_control`` (stripped earlier).
  3. ``_add_system_message_to_messages`` billing-header filter (same
     adapter file) drops ``x-anthropic-billing-header:`` text blocks.
     Claude Code injects this as the FIRST system text block, ahead of
     the ``cache_control`` marker, carrying a per-request ``cch=<hash>``;
     that hash invalidates the prefix-cache key every turn, so
     ``cache_read_input_tokens`` stays 0 forever. The sibling
     ``messages/transformation.py`` path already filters it via
     ``_filter_billing_headers_from_system``; the adapter path missed it.

Idempotent: each patch detects whether it is already applied. Fails
loudly (non-zero exit) if a needle is missing, so a LiteLLM version bump
that moves the code surfaces at build time instead of silently shipping
an unpatched image that bills full input rate.
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


def _patch_root(root: str) -> None:
    f1 = os.path.join(root, "llms/openrouter/chat/transformation.py")
    f2 = os.path.join(root, "llms/anthropic/experimental_pass_through/adapters/transformation.py")
    print(f"== patching {root}")

    # Patch 1 — CacheControlSupportedModels: add QWEN + DEEPSEEK.
    _apply(
        f1,
        present='QWEN = "qwen"',
        needle='    ZAI = "z-ai"\n',
        replacement=(
            '    ZAI = "z-ai"\n'
            "    # egg cache patch. OpenRouter natively supports cache_control\n"
            "    # for these providers — see\n"
            "    # https://openrouter.ai/docs/guides/best-practices/prompt-caching.\n"
            '    QWEN = "qwen"\n'
            '    DEEPSEEK = "deepseek"\n'
        ),
        label="Patch 1/3 (CacheControlSupportedModels)",
    )

    # Patch 2 — is_anthropic_claude_model: allow qwen + deepseek.
    _apply(
        f2,
        present='"qwen" in model_lower',
        needle='return "anthropic" in model_lower or "claude" in model_lower',
        replacement=(
            "return (\n"
            '            "anthropic" in model_lower\n'
            '            or "claude" in model_lower\n'
            '            or "qwen" in model_lower\n'
            '            or "deepseek" in model_lower\n'
            "        )"
        ),
        label="Patch 2/3 (is_anthropic_claude_model)",
    )

    # Patch 3 — drop x-anthropic-billing-header during Anthropic->OpenAI translation.
    _apply(
        f2,
        present='"x-anthropic-billing-header:" filter (egg cache patch)',
        needle=(
            "            for block in system_content:\n"
            '                if isinstance(block, dict) and block.get("type") == "text":\n'
            "                    text_block: Dict[str, Any] = {\n"
            '                        "type": "text",\n'
            '                        "text": block.get("text", ""),\n'
            "                    }\n"
        ),
        replacement=(
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
        label="Patch 3/3 (x-anthropic-billing-header filter)",
    )


def main() -> None:
    for root in _litellm_roots():
        _patch_root(root)
    print("egg LiteLLM cache patches: done")


if __name__ == "__main__":
    sys.exit(main())
