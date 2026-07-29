#!/usr/bin/env python3
"""Apply egg's LiteLLM cache, reasoning and cost patches at image-build time.

LiteLLM's stock Anthropic->OpenAI translation (the path Claude Code's
``/v1/messages`` requests take when routed at a non-Claude OpenRouter
backend) drops prompt-cache hits for Qwen/DeepSeek, reads a model-cost map
that carries neither the parameters nor the prices of current OpenRouter
slugs, discards caller-specified params in total silence, manufactures a
reasoning ceiling nobody asked for, never sends prior-turn reasoning back,
loses the BYOK half of the provider's bill during stream reassembly, and —
new at 1.94.0 — hands its own cost calculator a BYOK zero that suppresses
the estimate entirely. Ten independent gaps cause it; this script closes
all ten by editing the installed ``litellm`` package in place (and
installing five new modules), then ``config/litellm/Dockerfile`` bakes the
result into the ``egg-litellm`` image.

Pinned to ``litellm==1.94.0`` (see the Dockerfile ``FROM``), so the needles
below are known to match. The bump from 1.86.2 retired four patches whose
fixes upstream absorbed — the streaming ``reasoning_content`` thinking
block, the first-delta requeue on block transitions (both sync and async),
and the ``prompt_tokens_details.cached_tokens`` fallback — narrowed a
fifth (8) to the half upstream still does not carry, and ADDED one (10) for
a regression the bump itself brings in. Patch 7 arrived separately, from
#3698, and its needle was re-verified against the 1.94.0 wheel rather than
assumed to survive the bump. See issue #3697 for the audit, and read its
warning before assuming the next bump retires anything: TWO of the patches
below (2 and 5) had their needles stop matching at 1.94.0 purely because
upstream reflowed the surrounding code, and retiring them on the strength
of that miss would have silently shipped an image paying full input rate on
every Qwen turn.

  1. ``CacheControlSupportedModels`` (openrouter/chat/transformation.py)
     add QWEN + DEEPSEEK so ``cache_control`` survives the OpenRouter
     handler's strip step. Without it every turn pays full input rate.
     1.94.0 still lists only claude/gemini/minimax/glm/z-ai. NOTE the
     jwbron/litellm fork adds QWEN but deliberately omits DEEPSEEK, on the
     grounds that DeepSeek caching is automatic/prefix-based and ignores
     ``cache_control`` — if that holds, egg's DEEPSEEK arm is inert rather
     than wrong, and is a candidate for removal once measured.
  2. ``_add_cache_control_if_applicable`` cache_control gate (anthropic
     adapter transformation.py) broaden ONLY that gate to qwen + deepseek so
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
     ``cache_read_input_tokens`` stays 0 forever. 1.94.0 filters it in
     ``anthropic/chat/transformation.py`` and
     ``messages/transformation.py`` but still not in this adapter path.
  4. ``OpenrouterConfig.get_supported_openai_params``
     (openrouter/chat/transformation.py) consult OpenRouter's published
     per-model ``supported_parameters`` instead of only the bundled
     model-cost map. The stock gate asks ``litellm.supports_reasoning``,
     which reads ``model_prices_and_context_window.json``; OpenRouter
     ships new slugs faster than that map tracks them, so a current
     model answers False. The gate is a bare ``if``, so it fails CLOSED,
     and ``drop_params: true`` discards the parameter with no exception
     and no log line. The companion module
     ``llms/openrouter/_egg_capabilities.py`` (installed by
     ``NEW_MODULES``) reads OpenRouter's unauthenticated
     ``/api/v1/models`` and is UNIONED with the map answer, never
     subtractive: ``supported_parameters`` under-reports
     ``reasoning_effort`` (deepseek-r1 advertises only ``reasoning``,
     treating the OpenAI spelling as an alias), so reading its absence as
     a denial would drop a working param. Only ``reasoning_effort`` is
     admitted — OpenRouter's ``reasoning`` field is a different wire shape
     from Anthropic's ``thinking``, not a spelling of it. Fails soft: any
     fetch error yields no opinion and the stock path runs unchanged.
     Mirrors jwbron/litellm#8 and jwbron/egg#3624. Patch 6 is the other
     half of this one: read them together.
  5. ``get_optional_params`` drop site (utils.py) log what
     ``drop_params`` discards. Stock pops unsupported params in a bare
     loop with no logging, so a param set in a proxy config that never
     reaches the provider is a real behavioural difference with no signal
     attached — the condition that made patch 4's bug take a full
     investigation to find. Patch 4 removes the OpenRouter
     false-negative; this covers the rest, including drops that are
     CORRECT: laguna-s-2.1 genuinely does not accept ``reasoning_effort``,
     so it is dropped on purpose and the operator otherwise has no way to
     learn why their config line does nothing. Deduped per
     (provider, model, param-set) and bounded. NOTE the needle: 1.94.0
     still has two ``drop_params`` branches in utils.py sharing one
     spelling, so the needle includes the pop loop.
     Mirrors jwbron/litellm#7, merged into the fork the HOST proxy runs;
     BerriAI has not taken it. The message offers the
     ``allowed_openai_params`` remedy GATED on the params having come from
     this model's ``litellm_params``, and names the synthesized case
     alongside it: the param most often dropped here is one litellm
     manufactured from the request itself (see 6), so an unconditional
     config edit would send that operator hunting for a line that does not
     exist.
  6. ``_translate_thinking_to_openai`` (anthropic adapter
     transformation.py) stop synthesizing ``reasoning_effort`` from the
     caller's ``thinking`` block for non-Claude models. On ``/v1/messages``
     — egg's primary route — Claude Code sends
     ``thinking: {"type": "enabled", "budget_tokens": N}``, and because
     ``is_anthropic_claude_model`` is a substring test for
     ``anthropic``/``claude``, every OpenRouter slug egg routes takes the
     non-Claude branch where the adapter REPLACES that block with a
     bucketed ``reasoning_effort``. Nothing in ``litellm-models.yaml`` is
     involved: the value is manufactured per request. That was harmless
     only because patch 4's bug dropped it; the measurements in
     jwbron/egg#3624 show the bucket is a CAP BELOW the model default
     (kimi-k3: 3130 reasoning tokens with no param, 340 with
     ``reasoning_effort: high``, non-overlapping), so shipping patch 4
     without this would cut reasoning ~9x per agent turn with no config
     file to point at and nothing logged — patch 5 fires only on drops,
     and this param would no longer be dropped. Gating the synthesis (off
     by default, ``LITELLM_ANTHROPIC_THINKING_TO_REASONING_EFFORT=1`` to
     restore stock) keeps patch 4's actual goal: a knob an operator
     configured reaches the wire, one nobody configured does not. The
     Claude branch is untouched, and so is an effort the CALLER stated
     outright: on an adaptive request (``thinking: {"type": "adaptive"}``
     plus ``output_config: {"effort": ...}``) the gate sits after stock's
     override, so that value still reaches the provider with the policy
     off. Only the DERIVED bucket is suppressed — the distinction is
     structural, not a special case. A ``thinking.summary`` request goes
     with the derived effort, because stock carries the summary only as a
     field of the ``reasoning_effort`` dict and there is no wire shape for
     "summary, no effort".
  7. ``OpenrouterConfig.transform_request``
     (openrouter/chat/transformation.py) carry prior-turn assistant
     reasoning back to the provider. The Anthropic adapter converts
     incoming ``thinking`` content blocks into
     ``assistant_message["thinking_blocks"]``, and NOTHING on the
     OpenRouter request path consumes that field: stock
     openrouter/chat/transformation.py names reasoning exactly once, on
     the response side (``reasoning`` -> ``reasoning_content`` on
     streaming deltas), and ``reasoning_details`` appears nowhere in
     ``llms/`` or ``litellm_core_utils/``. The parent
     ``transform_request`` puts ``messages`` straight into the body, so
     ``thinking_blocks`` reaches OpenRouter as a field no one reads and
     every historical assistant turn arrives with no reasoning attached.
     For a model whose chat template re-renders prior thinking that is a
     malformed history rather than a lost optimisation: Poolside Laguna
     renders ``'<think>' + message.reasoning|message.reasoning_content +
     '</think>'`` for every previous assistant turn, so each one arrives
     as a literal empty ``<think></think>``, which Poolside's model card
     warns degrades follow-up behaviour; the measured cost in egg was a
     livelock whose per-turn prompt growth was exactly +243 tokens,
     leaving no budget for reasoning content. The companion module
     ``llms/openrouter/_egg_reasoning_roundtrip.py`` (installed by
     ``NEW_MODULES``) maps the blocks onto ``reasoning_content``, the
     plain string form OpenRouter documents for exactly this multi-turn
     tool-calling case and the field Poolside's template reads;
     ``reasoning_details`` exists to carry encrypted or summarised
     blocks and we have neither. Blocks join on a newline (they are
     separate thoughts, and "" runs the last word of one into the first
     of the next), ``redacted_thinking`` blocks contribute nothing (no
     plaintext to send), a ``reasoning_content`` the caller already set
     is never overwritten, and ``thinking_blocks`` is removed either way
     — including for the adapter's ``thinking_blocks: None`` sentinel,
     which it sets on every assistant turn that reasoned about nothing
     and which is therefore the DOMINANT input, not an edge case — so no
     unknown field is transmitted. Two whole-request opt-outs: the
     module declines any slug whose upstream re-verifies replayed
     reasoning (``anthropic``/``google``, where the discarded
     ``signature`` and the flattened block order are load-bearing and
     OpenRouter's ``reasoning_details`` is the right shape — a separate
     change), and ``LITELLM_OPENROUTER_REASONING_ROUNDTRIP=0`` backs the
     patch out of a live cluster without an image rebuild, as for
     patches 4 and 6. Fails soft per message: a block it cannot parse
     leaves that message exactly as it arrived, and says so once.
     NOTE the DIRECTION — this is request-path (client -> provider),
     where the three patches the 1.94.0 bump retired (the streaming
     ``reasoning_content`` thinking block and the two first-delta
     requeues) were response-path (provider -> client). Adjacent, not
     the same thing, which is why upstream absorbing those says nothing
     about this one: its needle was checked against the 1.94.0 wheel
     directly and still matches, once. The gap is upstream's and
     predates every egg and fork change: jwbron/litellm#8 touches only
     ``get_supported_openai_params``, which acts on ``optional_params``
     and cannot reach a message field.
  8. ``ChunkProcessor.calculate_usage``
     (litellm_core_utils/streaming_chunk_builder_utils.py) carry the
     provider's ``cost_details`` across stream reassembly. 1.86.2 dropped
     the whole bill here, which put ``cost: null`` on 1252 of 1252 sampled
     calls (#3691); 1.94.0 carries ``cost`` natively and feeds it to the
     cost calculator, but still not ``cost_details`` — which is where the
     BYOK number lives, since OpenRouter's top-level ``cost`` is 0 when
     billing routes past them. The companion module
     ``litellm_core_utils/_egg_stream_cost.py`` never overwrites a value
     litellm already set, so it no-ops on ``cost`` and supplies only the
     missing half.
  9. ``_get_model_info_helper`` (utils.py) price OpenRouter slugs the
     bundled map has never heard of. Same root cause as 4, second
     symptom: the map does not carry current slugs, so the lookup raises
     "This model isn't mapped yet", ``response_cost`` is never computed,
     and ``cost_estimated`` reads null. The hook sits at the raise site,
     after every stock lookup has failed, so a mapped slug keeps the
     bundled answer and the live card can add a model but never reprice
     one. ``_egg_capabilities`` grows a second entry point for this off
     the roster it already caches; it answers for OpenRouter alone,
     carries cost fields only (a ``supports_*`` flag through this door
     would change parameter admission, which is 4's job), and translates
     a prompt-length surcharge only into the rate slots LiteLLM actually
     has, declining the whole card when one does not fit rather than
     registering a base tier that would silently under-report the long
     prompts agent traffic is made of. Proposed upstream as
     jwbron/litellm#10.
 10. ``CustomStreamWrapper._propagate_usage_cost_to_hidden_params``
     (litellm_core_utils/streaming_handler.py) require the provider's
     ``cost`` to be POSITIVE and finite before it is allowed to stand in
     for the cost calculator. New at 1.94.0 and a regression the bump
     itself brings in: the method did not exist in 1.86.2, and it copies
     ``usage.cost`` into ``_hidden_params`` on both streaming completion
     paths, where ``response_cost_calculator`` returns it verbatim and
     ``completion_cost()`` never runs. Its guard is ``is not None``, and
     under BYOK OpenRouter's ``cost`` is a literal ``0.0`` — so on egg's
     route (Claude Code streams every request) the calculator short-circuits
     on a zero, ``cost_estimated`` reads null on every streamed call, and
     patch 9 is dead code, because its hook sits inside ``completion_cost``.
     Requiring a positive figure restores both for the BYOK case and leaves
     the real-charge case alone. NOTE what that leaves standing: where a
     provider DOES report a positive charge, 1.94.0 seeds its own calculator
     with it, so ``cost_estimated`` mirrors ``cost`` rather than being an
     independent rate-card read — see ``cost_callback._extract_estimated_cost``.
     Non-finite is excluded too: ``+inf`` clears a bare ``> 0`` and would be
     written into a spend header as ``Infinity``.

Idempotent: each patch detects whether it is already applied. Fails
loudly (non-zero exit) if a needle is missing, so a LiteLLM version bump
that moves the code surfaces at build time instead of silently shipping
an unpatched image that bills full input rate or drops reasoning tokens.
A needle miss means "look at this", NOT "upstream fixed it" — see the
1.94.0 note above.
"""

import ast
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


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
    except SyntaxError:
        return False
    return True


def _check_parses(source: str, path: str, label: str, detail: str) -> None:
    """Fail the build if we are about to write source Python cannot import.

    A needle miss already exits non-zero, but a replacement with wrong
    indentation applies *cleanly* — the result would pass the build, ship in
    the image, and surface as a pod CrashLoopBackOff at litellm import time,
    long after the only thing that could have caught it. Same fail-loud
    discipline as the needle check, one step later.

    ``detail`` names what is actually broken, because the two callers arrive
    here from different directions: ``_apply`` has substituted a replacement
    into an upstream file, while ``_install_module`` has read a staged module of
    ours with no replacement involved at all. ``source`` must be the text whose
    line numbering matches ``path``, so the reported line sends the operator to
    the right one.
    """
    try:
        ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise SystemExit(f"{label}: {detail} ({path}:{exc.lineno}: {exc.msg})") from exc


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
    patched = src.replace(needle, replacement, 1)
    # Checked only when the input was valid Python to begin with: a patch can
    # break a file, it cannot be blamed for one that never parsed. Every real
    # litellm source does; the concatenated-needle fixtures in tests/config
    # deliberately do not, and holding them to it would test the fixture rather
    # than the patch.
    if _parses(src):
        _check_parses(
            patched,
            path,
            label,
            "patched source does not parse — the replacement is malformed",
        )
    with open(path, "w") as fh:
        fh.write(patched)
    print(f"{label}: applied")


F1 = "llms/openrouter/chat/transformation.py"
F2 = "llms/anthropic/experimental_pass_through/adapters/transformation.py"
F3 = "llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py"
F4 = "utils.py"
F5 = "litellm_core_utils/streaming_chunk_builder_utils.py"
F6 = "litellm_core_utils/streaming_handler.py"

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
        "label": "Patch 1/10 (CacheControlSupportedModels)",
    },
    # Patch 2 — broaden ONLY the cache_control gate (not the shared
    # is_anthropic_claude_model predicate, which also gates thinking
    # translation — see module docstring). Touches the single call site in
    # _add_cache_control_if_applicable.
    #
    # NOT retired by the 1.94.0 bump, despite looking like it was. Upstream
    # rewrote this gate — collapsed it to one line and added
    # ``is_bedrock_arn_model`` — so the old needle stopped matching, and a
    # needle miss is indistinguishable from "upstream fixed it" if you only
    # look at the miss. Upstream added no qwen/deepseek arm: 1.94.0's
    # ``CacheControlSupportedModels`` still lists only claude/gemini/minimax/
    # glm/z-ai. Retiring this on the strength of the miss would have shipped an
    # image that pays FULL INPUT RATE on every Qwen turn — the most expensive
    # single regression available here, and a silent one.
    {
        "file": F2,
        "present": "# egg cache patch. Broaden ONLY the cache_control gate",
        "needle": (
            "        if cache_control and model and "
            "(self.is_anthropic_claude_model(model) or self.is_bedrock_arn_model(model)):\n"
        ),
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
            "                or self.is_bedrock_arn_model(model)\n"
            '                or "qwen" in _model_lower\n'
            '                or "deepseek" in _model_lower\n'
            "            )\n"
            "        ):\n"
        ),
        "label": "Patch 2/10 (cache_control gate)",
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
        "label": "Patch 3/10 (x-anthropic-billing-header filter)",
    },
    # Patch 4 — OpenrouterConfig.get_supported_openai_params: consult
    # OpenRouter's published capabilities instead of only the bundled
    # model-cost map.
    #
    # The stock gate asks ``litellm.supports_reasoning``, which reads
    # ``model_prices_and_context_window.json``. For OpenRouter that map is
    # wrong by construction: OpenRouter ships new slugs continuously and the
    # bundled map lags, so a current model answers False. The gate is a bare
    # ``if``, so it fails CLOSED, and ``drop_params: true`` then discards the
    # parameter with no exception and no log line. Every OpenRouter slug egg
    # routes is absent from the bundled map (kimi-k3, glm-5.2, laguna-s-2.1,
    # deepseek-v4-*), so any reasoning knob set on them never reached the wire.
    #
    # The companion module (installed by ``NEW_MODULES`` below) reads
    # OpenRouter's unauthenticated /api/v1/models and is UNIONED with the
    # existing map answer rather than replacing it. Live data can admit a knob
    # the map does not know about but never withholds one the map allows,
    # because ``supported_parameters`` under-reports ``reasoning_effort``:
    # deepseek/deepseek-r1 is flagged supports_reasoning in the map and is
    # plainly a reasoning model, yet OpenRouter advertises only ``reasoning``
    # for it, treating the OpenAI spelling as an alias. Reading that absence as
    # a denial would drop a working param — the very failure this fixes.
    #
    # Only ``reasoning_effort`` is admitted. OpenRouter also advertises a
    # ``reasoning`` param, but that is its own request field
    # (``{"effort": ...}`` / ``{"max_tokens": ...}``) and is NOT a spelling of
    # Anthropic's ``thinking`` (``{"type", "budget_tokens"}``). Mapping one to
    # the other by name similarity would let a raw Anthropic-shaped ``thinking``
    # dict through to a non-Anthropic provider on the /chat/completions route —
    # the same spelling conflation patch 2's notes are careful to avoid.
    #
    # Mirrors jwbron/litellm#8 and jwbron/egg#3624. Fails soft throughout: any
    # fetch error yields no opinion and the stock path runs, so the worst case
    # is exactly the unpatched behaviour.
    {
        "file": F1,
        "present": "# egg openrouter capability patch",
        "needle": (
            "    def get_supported_openai_params(self, model: str) -> list:\n"
            '        """\n'
            "        Allow reasoning parameters for models flagged as reasoning-capable.\n"
            '        """\n'
            "        supported_params = super().get_supported_openai_params(model=model)\n"
            "        try:\n"
        ),
        "replacement": (
            "    def get_supported_openai_params(self, model: str) -> list:\n"
            '        """\n'
            "        Allow reasoning parameters for models flagged as reasoning-capable.\n"
            '        """\n'
            "        supported_params = super().get_supported_openai_params(model=model)\n"
            "        # egg openrouter capability patch. OpenRouter publishes per-model\n"
            "        # supported_parameters over an unauthenticated endpoint; the bundled\n"
            "        # model-cost map does not carry current slugs, so the stock gate\n"
            "        # below fails closed and the knob is dropped in silence. Unioned,\n"
            "        # never subtractive — see patch 4 notes in patch_litellm_cache.py.\n"
            "        try:\n"
            "            from litellm.llms.openrouter._egg_capabilities import (\n"
            "                get_supported_parameters as _egg_openrouter_capabilities,\n"
            "            )\n"
            "\n"
            "            _advertised = _egg_openrouter_capabilities(model)\n"
            "            if _advertised is not None:\n"
            '                if "reasoning_effort" in _advertised:\n'
            '                    supported_params.append("reasoning_effort")\n'
            "        except Exception:\n"
            "            pass\n"
            "        try:\n"
        ),
        "label": "Patch 4/10 (openrouter live capabilities)",
    },
    # Patch 5 — get_optional_params: log what ``drop_params`` discards.
    #
    # Patch 4 removes the OpenRouter false-negative, but a drop can still be
    # correct and still worth knowing about: laguna-s-2.1 genuinely does not
    # accept ``reasoning_effort``, so the knob is dropped on purpose and the
    # operator has no way to learn why their config line does nothing. Stock
    # pops unsupported params in a bare loop with no logging at all.
    #
    # NOT retired by the 1.94.0 bump. Upstream reflowed the condition onto one
    # line, so the old needle stopped matching — but nothing upstream logs the
    # drop; the warn-once bookkeeping exists only in jwbron/litellm (PR #7),
    # which BerriAI has not taken. Repointed, not deleted.
    #
    # NEEDLE DISAMBIGUATION: 1.94.0 still has TWO ``if litellm.drop_params is
    # True or (...)`` sites in utils.py with this exact one-line spelling — the
    # other (the embeddings path) is followed by a bare ``pass``, this one by
    # the pop loop. Verified: the bare condition matches twice, the condition
    # plus the loop matches once. The needle therefore keeps the loop lines;
    # matching on the condition alone would patch whichever came first.
    {
        "file": F4,
        "present": "# egg drop_params visibility patch",
        "needle": (
            "            if litellm.drop_params is True or "
            "(drop_params is not None and drop_params is True):\n"
            "                for k in unsupported_params.keys():\n"
            "                    non_default_params.pop(k, None)\n"
        ),
        "replacement": (
            "            if litellm.drop_params is True or "
            "(drop_params is not None and drop_params is True):\n"
            "                # egg drop_params visibility patch. Dropping a param changes\n"
            "                # generation behaviour; stock does it with no signal at all.\n"
            "                try:\n"
            "                    from litellm._egg_drop_params_visibility import (\n"
            "                        warn_dropped_params as _egg_warn_dropped_params,\n"
            "                    )\n"
            "\n"
            "                    _egg_warn_dropped_params(\n"
            "                        unsupported_params=unsupported_params,\n"
            "                        model=model,\n"
            "                        custom_llm_provider=custom_llm_provider,\n"
            "                    )\n"
            "                except Exception:\n"
            "                    pass\n"
            "                for k in unsupported_params.keys():\n"
            "                    non_default_params.pop(k, None)\n"
        ),
        "label": "Patch 5/10 (drop_params visibility)",
    },
    # Patch 6 — _translate_thinking_to_openai: stop synthesizing
    # ``reasoning_effort`` from the caller's ``thinking`` block for non-Claude
    # models.
    #
    # This is the other half of patch 4, and without it patch 4 is a
    # regression on egg's primary route. On /v1/messages (Claude Code ->
    # gateway -> litellm -> OpenRouter) the request body carries
    # ``thinking: {"type": "enabled", "budget_tokens": N}``.
    # ``is_anthropic_claude_model`` is a substring test for
    # ``anthropic``/``claude``, so every OpenRouter slug egg routes takes the
    # non-Claude branch, where the adapter REPLACES the block with a bucketed
    # ``reasoning_effort`` (>=10000 -> "high", >=5000 -> "medium",
    # >=2000 -> "low"). Nothing in litellm-models.yaml is involved: the value
    # is manufactured per request.
    #
    # Until now that param was silently dropped (the model-cost map does not
    # carry these slugs), which is exactly why these models have been running
    # at full reasoning depth. Patch 4 unblocks the param — correct for an
    # operator-configured value, wrong for this one, because the measurements
    # in jwbron/egg#3624 show the bucket is a CAP BELOW the model default:
    # kimi-k3 means 3130 reasoning tokens with no param vs 340 with
    # ``reasoning_effort: high``, distributions non-overlapping. Shipping
    # patch 4 alone would cut reasoning ~9x on every agent turn with nothing
    # logged (patch 5 only fires on drops, and this is no longer dropped) and
    # no config file to point at.
    #
    # Gating the synthesis keeps patch 4's actual goal — a configured knob
    # reaches the wire — without letting the adapter's bucket become the
    # effective setting. ``LITELLM_ANTHROPIC_THINKING_TO_REASONING_EFFORT=1``
    # restores stock behaviour. The Claude branch above is untouched.
    #
    # The gate sits AFTER the adaptive-thinking override, not before the whole
    # block, and that placement is the contract. Stock reaches the
    # ``reasoning_effort`` assignment two ways: derived from ``budget_tokens``
    # (the manufactured ceiling this patch exists to stop) or stated outright by
    # the caller as ``output_config.effort`` on an adaptive request. Gating the
    # whole function would discard the second — an explicit instruction, not an
    # invented cap — so only the derived value is suppressed. egg's own route
    # never sends the adaptive shape (Claude Code sends
    # ``thinking.type == "enabled"``), so this is about the patch matching its
    # own stated scope rather than a live behaviour today.
    #
    # A ``thinking.summary`` request is still suppressed along with the derived
    # effort, and that is deliberate: stock carries the summary only as a field
    # of the ``reasoning_effort`` dict, so honouring it would require sending
    # the manufactured ceiling. There is no wire shape for "summary, no effort".
    {
        "file": F2,
        "present": "# egg thinking-synthesis patch",
        "needle": (
            "        # For adaptive thinking, override with output_config.effort if available\n"
            '        if isinstance(thinking, dict) and thinking.get("type") == "adaptive":\n'
            '            output_config = anthropic_message_request.get("output_config")\n'
            '            if isinstance(output_config, dict) and output_config.get("effort"):\n'
            '                reasoning_effort = output_config["effort"]\n'
            "\n"
            '        summary = thinking.get("summary") if isinstance(thinking, dict) else None\n'
        ),
        "replacement": (
            "        # For adaptive thinking, override with output_config.effort if available\n"
            "        _egg_effort_is_explicit = False\n"
            '        if isinstance(thinking, dict) and thinking.get("type") == "adaptive":\n'
            '            output_config = anthropic_message_request.get("output_config")\n'
            '            if isinstance(output_config, dict) and output_config.get("effort"):\n'
            '                reasoning_effort = output_config["effort"]\n'
            "                _egg_effort_is_explicit = True\n"
            "\n"
            "        # egg thinking-synthesis patch. Everything above DERIVES an effort\n"
            "        # from the caller's thinking budget, and that bucket is a cap BELOW\n"
            "        # the model default on every model egg routes, so sending it\n"
            "        # silently shallows reasoning. Off by default; see the patch 6 notes\n"
            "        # in patch_litellm_cache.py. An effort the caller stated outright\n"
            "        # (output_config.effort) is an instruction rather than a\n"
            "        # manufactured ceiling, and is never suppressed.\n"
            "        if not _egg_effort_is_explicit:\n"
            "            try:\n"
            "                from litellm._egg_anthropic_thinking_policy import (\n"
            "                    should_synthesize_reasoning_effort as _egg_should_synthesize,\n"
            "                )\n"
            "\n"
            "                _egg_synthesize = _egg_should_synthesize()\n"
            "            except Exception:\n"
            "                # The module is installed by the same build step as this\n"
            "                # patch, so this is unreachable in a built image; fall back\n"
            "                # to the policy's own default, not to stock behaviour.\n"
            "                _egg_synthesize = False\n"
            "            if not _egg_synthesize:\n"
            "                return\n"
            "\n"
            '        summary = thinking.get("summary") if isinstance(thinking, dict) else None\n'
        ),
        "label": "Patch 6/10 (thinking->reasoning_effort synthesis gate)",
    },
    # Patch 7 — OpenrouterConfig.transform_request: carry prior-turn assistant
    # reasoning back to the provider.
    #
    # The Anthropic adapter converts incoming ``thinking`` content blocks into
    # ``assistant_message["thinking_blocks"]``, and nothing on the OpenRouter
    # REQUEST path consumes that field: stock openrouter/chat/transformation.py
    # names reasoning once, on the response side, and ``reasoning_details``
    # appears nowhere in llms/ or litellm_core_utils/. The parent
    # ``transform_request`` drops ``messages`` straight into the body, so
    # ``thinking_blocks`` reaches OpenRouter as a field no one reads and every
    # historical assistant turn arrives with no reasoning attached.
    #
    # For a model whose chat template re-renders prior thinking that is a
    # malformed history rather than a missed optimisation: Poolside Laguna
    # renders ``'<think>' + message.reasoning|message.reasoning_content +
    # '</think>'`` per historical assistant turn, so each one becomes a literal
    # empty ``<think></think>``, which Poolside's model card warns degrades
    # follow-up behaviour.
    #
    # DIRECTION: this is request-path (client -> provider); the three patches
    # the 1.94.0 bump retired (the streaming ``reasoning_content`` thinking
    # block and the two first-delta requeues) were response-path
    # (provider -> client). Adjacent, not the same thing — which is why
    # upstream absorbing those says nothing about this one.
    #
    # CARRIED ACROSS THE 1.94.0 BUMP: this patch arrived on the 1.86.2 base
    # (#3698) and its needle was re-checked against the 1.94.0 wheel rather
    # than assumed to still fit. It matches, once, unmodified.
    #
    # NEEDLE ANCHORING: ``_supports_cache_control_in_content`` appears THREE
    # times in 1.94.0 (its own ``def``, the call in
    # ``remove_cache_control_flag_from_messages_and_tools``, and the call in
    # ``transform_request``), and ``_move_cache_control_to_content`` twice.
    # No single call is a safe anchor on its own. The needle therefore spans
    # the cache_control pair AND the following ``extra_body`` pop, a sequence
    # that occurs only in ``transform_request``.
    {
        "file": F1,
        "present": "# egg reasoning round-trip patch",
        "needle": (
            "        if self._supports_cache_control_in_content(model):\n"
            "            messages = self._move_cache_control_to_content(messages)\n"
            "\n"
            '        extra_body = optional_params.pop("extra_body", {})\n'
        ),
        "replacement": (
            "        if self._supports_cache_control_in_content(model):\n"
            "            messages = self._move_cache_control_to_content(messages)\n"
            "\n"
            "        # egg reasoning round-trip patch. The Anthropic adapter parks\n"
            "        # prior-turn assistant reasoning on `thinking_blocks`, which no\n"
            "        # OpenRouter request-path code reads; map it onto the field\n"
            "        # OpenRouter documents for multi-turn tool calling so models whose\n"
            "        # template re-renders prior thinking stop seeing empty <think></think>.\n"
            "        # `model` is passed so the module can decline the routes whose\n"
            "        # upstream re-verifies replayed reasoning (anthropic/*, google/*).\n"
            "        # See patch 7 notes in patch_litellm_cache.py.\n"
            "        try:\n"
            "            from litellm.llms.openrouter._egg_reasoning_roundtrip import (\n"
            "                map_thinking_blocks_to_reasoning_content as _egg_map_reasoning,\n"
            "            )\n"
            "\n"
            "            messages = _egg_map_reasoning(messages, model)\n"
            "        except Exception as _egg_roundtrip_exc:\n"
            "            # The module is installed by the same build step as this patch,\n"
            "            # so this is unreachable in a built image. Say so once rather\n"
            "            # than reverting to stock in silence — an invisible no-op here\n"
            "            # shows up only as a model quietly reasoning worse, which is\n"
            "            # the condition patch 5 exists to stop repeating.\n"
            "            try:\n"
            "                from litellm._logging import verbose_logger\n"
            "\n"
            "                if not getattr(\n"
            '                    verbose_logger, "_egg_reasoning_roundtrip_warned", False\n'
            "                ):\n"
            "                    verbose_logger.warning(\n"
            '                        "egg reasoning round-trip module unavailable (%s); "\n'
            '                        "prior-turn assistant reasoning will not reach "\n'
            '                        "OpenRouter. Logged once per proxy process.",\n'
            "                        _egg_roundtrip_exc,\n"
            "                    )\n"
            "                    verbose_logger._egg_reasoning_roundtrip_warned = True\n"
            "            except Exception:\n"
            "                pass\n"
            "\n"
            '        extra_body = optional_params.pop("extra_body", {})\n'
        ),
        "label": "Patch 7/10 (assistant reasoning round-trip)",
    },
    # Patch 8 — ChunkProcessor.calculate_usage: carry the provider's
    # ``cost_details`` across stream reassembly.
    #
    # SCOPE CHANGED at the 1.94.0 bump, and the half that remains is the
    # subtler one. 1.86.2 dropped BOTH ``cost`` and ``cost_details`` here: the
    # rebuild enumerates token counts and re-constructs ``Usage`` from its own
    # ``model_dump()``, so anything the provider attached that litellm did not
    # name was gone. Claude Code streams every /v1/messages request, so that
    # seam was ~100% of egg's routed traffic — run 6 sampled 1252
    # ``cost_callback`` lines and 1252 carried ``cost: null`` (#3691).
    #
    # 1.94.0 carries ``cost`` natively (upstream 8a49423, a port of
    # BerriAI/litellm#16162) and goes further than egg did, feeding the figure
    # into litellm's own cost calculator via ``_hidden_params``. Verified
    # empirically against the stock 1.94.0 wheel: ``cost`` survives the rebuild,
    # ``cost_details`` does not.
    #
    # ``cost_details`` is where the BYOK bill lives. Under BYOK OpenRouter's
    # top-level ``cost`` is a literal 0 because billing routes past them, and
    # the real number is ``cost_details.upstream_inference_cost``. Upstream
    # records the zero and stops, so without this the BYOK path still reports no
    # spend — a null that looks identical to the bug #3691 fixed. Nothing
    # upstream carries this field; see the note at the end of
    # jwbron/litellm#10 for the shape a BerriAI PR would take.
    #
    # Placed AFTER the ``Usage(**model_dump())`` rebuild, not before: the
    # constructor deletes a ``cost`` attribute it is handed as None, so setting
    # it first would put the value somewhere the rebuild is entitled to discard.
    # The companion module (``litellm_core_utils/_egg_stream_cost.py``, in
    # NEW_MODULES) never overwrites a value litellm already carried, which is
    # exactly why the scope narrowing needed no code change: it now no-ops on
    # ``cost`` and still supplies ``cost_details``.
    {
        "file": F5,
        "present": "# egg cost patch. Carry the provider's cost_details",
        "needle": (
            "        # Return a new usage object with the new values\n"
            "\n"
            "        returned_usage = Usage(**returned_usage.model_dump())\n"
            "\n"
            "        return returned_usage\n"
        ),
        "replacement": (
            "        # Return a new usage object with the new values\n"
            "\n"
            "        returned_usage = Usage(**returned_usage.model_dump())\n"
            "\n"
            "        # egg cost patch. Carry the provider's cost_details across this\n"
            "        # rebuild. Upstream carries `cost` but not `cost_details`, which\n"
            "        # is where the BYOK bill lives (`cost` is 0 there). The helper\n"
            "        # never overwrites what litellm already set, so it no-ops on\n"
            "        # `cost`. See patch 7 notes in patch_litellm_cache.py.\n"
            "        try:\n"
            "            from litellm.litellm_core_utils._egg_stream_cost import (\n"
            "                carry_upstream_cost as _egg_carry_upstream_cost,\n"
            "            )\n"
            "\n"
            "            returned_usage = _egg_carry_upstream_cost(chunks, returned_usage)\n"
            "        except Exception as _egg_exc:\n"
            "            # Swallowed, because a cost figure must never break a\n"
            "            # response — but not silently. A failed import here looks\n"
            "            # from the outside exactly like `the provider reported no\n"
            "            # cost`, which is the symptom this patch exists to remove.\n"
            "            # Warned once per process, and the latch is set only after\n"
            "            # the emit succeeded; verbose_logger is imported here rather\n"
            "            # than read from module scope because this file does not\n"
            "            # import it. The message names `cost_details`, not `cost`:\n"
            "            # since 1.94.0 upstream carries `cost` itself, so what a\n"
            "            # failure here costs is the BYOK figure alone, and sending\n"
            "            # the operator to a field that reads correctly would be\n"
            "            # worse than not warning at all.\n"
            "            try:\n"
            "                if not globals().get('_egg_warned_stream_cost'):\n"
            "                    from litellm._logging import verbose_logger\n"
            "\n"
            "                    verbose_logger.warning(\n"
            "                        'egg cost patch: streamed cost_details preservation '\n"
            "                        'is inactive (%s: %s); `cost_details` will read null '\n"
            "                        'on every streamed call, so BYOK routes (where the '\n"
            "                        'provider-billed `cost` is 0) will report no spend.',\n"
            "                        type(_egg_exc).__name__,\n"
            "                        _egg_exc,\n"
            "                    )\n"
            "                    globals()['_egg_warned_stream_cost'] = True\n"
            "            except Exception:\n"
            "                pass\n"
            "\n"
            "        return returned_usage\n"
        ),
        "label": "Patch 8/10 (streamed cost_details preservation)",
    },
    # Patch 9 — _get_model_info_helper: price OpenRouter slugs the bundled map
    # has never heard of.
    #
    # Same root cause as patch 4, second symptom. LiteLLM's own
    # ``response_cost`` is computed from ``model_prices_and_context_window.json``,
    # which does not carry current OpenRouter slugs, so the lookup raises "This
    # model isn't mapped yet" and egg's ``cost_estimated`` reads null on every
    # routed call (#3691). Patch 7 recovers the BYOK *billed* figure; this one
    # restores the independent estimate beside it, which is what remains
    # readable if a provider ever stops reporting cost.
    #
    # PATCH 9 IS LOAD-BEARING FOR THIS ONE on the streaming path. The hook below
    # sits inside ``_get_model_info_helper``, reachable only via
    # ``completion_cost()`` — and 1.94.0's ``_propagate_usage_cost_to_hidden_params``
    # makes ``response_cost_calculator`` return before it ever calls that,
    # whenever the provider reported any ``cost`` at all including a BYOK zero.
    # Without patch 9 this patch applies cleanly, passes every direct test of
    # ``_get_model_info_helper``, and is dead code on ~100% of egg's traffic.
    #
    # Placed at the raise site, so it is reached only once every stock lookup
    # has failed: a slug the bundled map DOES carry keeps the bundled answer,
    # and the live rate card can add a model but never reprice one. The
    # companion module answers for OpenRouter alone (it checks
    # ``custom_llm_provider``) and holds a prompt-length surcharge only in the
    # rate slots LiteLLM has, declining the whole card when a published boundary
    # or component does not fit rather than registering rates that would
    # under-report long prompts — see ``openrouter_capabilities``.
    #
    # NEEDLE DISAMBIGUATION: utils.py carries the "isn't mapped yet" string
    # twice. The other one (~line 5999) is the outer handler's re-raise, with a
    # different message body and indentation; the needle pins the ValueError
    # form together with the ``if _model_info is None or key is None:`` guard
    # that precedes only this one.
    {
        "file": F4,
        "present": "# egg pricing patch. Consult OpenRouter's published rate card",
        "needle": (
            "            if _model_info is None or key is None:\n"
            "                raise ValueError(\n"
            "                    \"This model isn't mapped yet. Add it here - "
            'https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json"\n'
            "                )\n"
        ),
        "replacement": (
            "            if _model_info is None or key is None:\n"
            "                # egg pricing patch. Consult OpenRouter's published rate card\n"
            "                # before giving up — the bundled map lags its slugs by\n"
            "                # construction, so every route egg uses lands here and every\n"
            "                # routed call reports a null cost estimate. Cost fields only;\n"
            "                # see patch 8 notes in patch_litellm_cache.py.\n"
            "                try:\n"
            "                    from litellm.llms.openrouter._egg_capabilities import (\n"
            "                        get_model_cost_entry as _egg_openrouter_cost_entry,\n"
            "                    )\n"
            "\n"
            "                    _egg_entry = _egg_openrouter_cost_entry(\n"
            "                        model, custom_llm_provider\n"
            "                    )\n"
            "                except Exception as _egg_exc:\n"
            "                    _egg_entry = None\n"
            "                    # Same reasoning as patch 7's handler: swallowed so a\n"
            "                    # rate-card lookup can never break a request, warned once\n"
            "                    # so its absence is not indistinguishable from a slug the\n"
            "                    # roster simply does not carry. verbose_logger is already\n"
            "                    # module-scope in utils.py; the warning is still wrapped\n"
            "                    # because raising from an except block would propagate.\n"
            "                    try:\n"
            "                        if not globals().get('_egg_warned_pricing'):\n"
            "                            verbose_logger.warning(\n"
            "                                'egg pricing patch: OpenRouter rate-card '\n"
            "                                'lookup is inactive (%s: %s); cost_estimated '\n"
            "                                'will read null for unmapped slugs.',\n"
            "                                type(_egg_exc).__name__,\n"
            "                                _egg_exc,\n"
            "                            )\n"
            "                            globals()['_egg_warned_pricing'] = True\n"
            "                    except Exception:\n"
            "                        pass\n"
            "                if _egg_entry is not None:\n"
            "                    _model_info = _egg_entry\n"
            '                    key = _egg_entry.get("key") or model\n'
            "            if _model_info is None or key is None:\n"
            "                raise ValueError(\n"
            "                    \"This model isn't mapped yet. Add it here - "
            'https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json"\n'
            "                )\n"
        ),
        "label": "Patch 9/10 (openrouter live pricing)",
    },
    # Patch 10 — _propagate_usage_cost_to_hidden_params: require a POSITIVE,
    # finite provider cost before it stands in for the cost calculator.
    #
    # This one is not a gap in 1.86.2 that survived the bump; it is a
    # regression the bump BRINGS IN. The method does not exist in 1.86.2
    # (``grep -rn _propagate_usage_cost_to_hidden_params`` on that wheel returns
    # nothing). 1.94.0 calls it on ``complete_streaming_response`` from both
    # streaming completion paths (``__next__`` and ``__anext__``), immediately
    # before ``logging_obj.success_handler`` sees the copy, and it copies
    # ``usage.cost`` into ``_hidden_params["additional_headers"]``. Downstream,
    # ``response_cost_calculator`` returns that header verbatim and
    # ``completion_cost()`` never runs.
    #
    # Its guard is ``_usage.cost is not None``, and
    # ``get_response_cost_from_hidden_params`` only declines on a literal None —
    # ``0.0`` is returned as ``0.0``. Under BYOK OpenRouter's top-level ``cost``
    # IS ``0.0`` (billing routes past them; the real number lives in
    # ``cost_details``, which is patch 7's job). Claude Code streams every
    # /v1/messages request, so on egg's route the stock behaviour is: calculator
    # short-circuits on a zero -> ``response_cost`` is 0.0 -> egg's
    # ``cost_estimated`` reads null on EVERY streamed call (it is gated on
    # ``_positive``), and patch 8 never fires at all, because its hook sits in
    # ``_get_model_info_helper``'s unmapped branch, reachable only through
    # ``completion_cost()``. Both of those are things this image exists to
    # provide, and both would fail silently: a null cost_estimated is
    # indistinguishable from an unpriceable model.
    #
    # Requiring a positive figure restores ``completion_cost()`` (and with it
    # patch 8) for the BYOK case and leaves the real-charge case alone — the
    # narrow fix, not a blanket suppression: where a provider reports a genuine
    # charge, letting litellm bill from it rather than from a rate card is
    # upstream's improvement and worth keeping. The cost of that choice is
    # recorded in ``cost_callback._extract_estimated_cost``: on such a route
    # ``cost_estimated`` mirrors ``cost`` instead of being an independent read.
    #
    # Finiteness is checked too, for the reason ``cost_callback._positive``
    # gives: ``+inf`` clears a bare ``> 0``, and this value is written into a
    # spend header where it would serialize as the non-standard ``Infinity``.
    # ``bool`` is excluded for the same reason it is there — ``isinstance(True,
    # int)`` is True, and a True is not a dollar.
    #
    # The needle spans the assignment and the guard together: the guard line
    # alone is what changes, but the assignment above it is what makes the
    # match unambiguous, and ``_egg_cost`` has to be computed before it.
    {
        "file": F6,
        "present": "# egg cost-propagation patch",
        "needle": (
            '        _usage = getattr(response, "usage", None)\n'
            '        if _usage is not None and hasattr(_usage, "cost") '
            "and _usage.cost is not None:\n"
        ),
        "replacement": (
            '        _usage = getattr(response, "usage", None)\n'
            "        # egg cost-propagation patch. Upstream's guard is `is not None`,\n"
            "        # and this value short-circuits litellm's cost calculator. Under\n"
            "        # BYOK OpenRouter reports `cost: 0.0` (the real figure is in\n"
            "        # cost_details), so the stock guard hands the calculator a zero,\n"
            "        # `response_cost` reads 0.0, egg's `cost_estimated` reads null,\n"
            "        # and the OpenRouter rate-card patch never runs — it lives inside\n"
            "        # completion_cost(), which this return skips. Require a positive,\n"
            "        # finite number; a real charge still propagates unchanged. See\n"
            "        # patch 9 notes in patch_litellm_cache.py.\n"
            '        _egg_cost = getattr(_usage, "cost", None) if _usage is not None else None\n'
            "        _egg_cost_is_billable = (\n"
            "            isinstance(_egg_cost, (int, float))\n"
            "            and not isinstance(_egg_cost, bool)\n"
            "            and _egg_cost > 0\n"
            '            and _egg_cost < float("inf")\n'
            "        )\n"
            "        if _egg_cost_is_billable:\n"
        ),
        "label": "Patch 10/10 (billable-cost gate on hidden params)",
    },
]

# Whole modules to drop into each litellm tree, sourced from files the
# Dockerfile stages under /egg. Unlike PATCHES these are additive: there is no
# stock file to collide with, so installation is a copy guarded by a content
# check rather than a needle match.
#
# Every destination carries the ``_egg_`` prefix. It is not decoration: it
# keeps a future upstream module from colliding with ours, since an unprefixed
# name (``capabilities.py``) is one upstream could plausibly take.
EGG_MODULE_PREFIX = "_egg_"

# Provenance header written ahead of every installed module. The prefix above
# makes a collision unlikely; this is what makes the clobber guard *real*.
# Checking the prefix told us only that our own ``NEW_MODULES`` literals were
# spelled the way we spelled them — it could never fire on upstream drift,
# because it never looked at the file on disk. This does: a file at one of our
# destinations that does not carry this header is not ours, whoever put it
# there, and overwriting it would break litellm in a way nothing else in this
# script would report.
EGG_MODULE_MARKER = "egg-managed module (config/litellm/patch_litellm_cache.py)"
EGG_MODULE_HEADER = f"# {EGG_MODULE_MARKER} — do not edit in place.\n"

NEW_MODULES: list[dict[str, str]] = [
    {
        "source": "openrouter_capabilities.py",
        "dest": "llms/openrouter/_egg_capabilities.py",
        "label": "Module 1/5 (openrouter capabilities + pricing)",
    },
    {
        "source": "drop_params_visibility.py",
        "dest": "_egg_drop_params_visibility.py",
        "label": "Module 2/5 (drop_params visibility)",
    },
    {
        "source": "anthropic_thinking_policy.py",
        "dest": "_egg_anthropic_thinking_policy.py",
        "label": "Module 3/5 (thinking synthesis policy)",
    },
    {
        "source": "openrouter_reasoning_roundtrip.py",
        "dest": "llms/openrouter/_egg_reasoning_roundtrip.py",
        "label": "Module 4/5 (openrouter reasoning round-trip)",
    },
    {
        "source": "stream_cost_preservation.py",
        "dest": "litellm_core_utils/_egg_stream_cost.py",
        "label": "Module 5/5 (streamed cost preservation)",
    },
]


def _module_source(name: str, label: str) -> str:
    """Locate a staged module by basename.

    In the image the Dockerfile drops it beside this script under /egg; in the
    repo (and in tests) it sits beside this script in config/litellm. Checking
    both means the same script runs in either place without a path flag, and it
    still fails loudly rather than silently skipping the install."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join("/egg", name), os.path.join(here, name)):
        if os.path.isfile(candidate):
            return candidate
    raise SystemExit(f"{label}: staged source not found: {name} (looked in /egg and {here})")


def _install_module(root: str, spec: dict[str, str]) -> None:
    label = spec["label"]
    # A lint of this file's own constants rather than drift detection (the
    # provenance check below is that): every destination must carry the prefix,
    # so an upstream module can never occupy one of our paths to begin with.
    if not os.path.basename(spec["dest"]).startswith(EGG_MODULE_PREFIX):
        raise SystemExit(
            f"{label}: destination {spec['dest']} must be prefixed {EGG_MODULE_PREFIX!r}"
        )
    source = _module_source(spec["source"], label)
    with open(source) as fh:
        body = fh.read()
    # The header goes on disk, not in the repo copy: it is the provenance the
    # guard below reads. A leading comment leaves the module docstring as the
    # first statement, so nothing about the module changes.
    payload = EGG_MODULE_HEADER + body
    # Same reason as in ``_apply``: a truncated COPY or a half-written staged
    # file would install without complaint and only fail at litellm import.
    # Checked against the un-headered text, whose line numbers match the file
    # the operator will open — the header is a comment, so it cannot change
    # whether the rest parses, but it does shift every reported line by one.
    _check_parses(body, source, label, "staged module source does not parse")
    dest = os.path.join(root, spec["dest"])
    dest_dir = os.path.dirname(dest)
    if not os.path.isdir(dest_dir):
        raise SystemExit(
            f"{label}: destination package missing: {dest_dir} — LiteLLM version drift?"
        )
    if os.path.isfile(dest):
        with open(dest) as fh:
            existing = fh.read()
        if existing == payload:
            print(f"{label}: already installed")
            return
        # Differing content that still carries our header is a stale install
        # from an earlier image layer — overwrite it. Differing content WITHOUT
        # the header means somebody else owns this path (upstream took the
        # name, an operator dropped a file in), and clobbering it would break
        # litellm in a way nothing else in this script would report. Every
        # other operation here is fail-loud on drift; so is this.
        if EGG_MODULE_MARKER not in existing:
            raise SystemExit(
                f"{label}: refusing to overwrite {dest} — it exists with different "
                "content and no egg provenance header, so it is not ours. "
                "LiteLLM version drift?"
            )
    with open(dest, "w") as fh:
        fh.write(payload)
    print(f"{label}: installed")


def _patch_root(root: str) -> None:
    print(f"== patching {root}")
    for spec in NEW_MODULES:
        _install_module(root, spec)
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
