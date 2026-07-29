# egg-litellm — stock LiteLLM proxy + egg's prompt-cache patches.
#
# egg routes non-Claude agents (the cq-6 OpenRouter/Qwen pilot, #2799)
# through this proxy: Claude Code -> gateway -> LiteLLM -> OpenRouter. The
# stock image's Anthropic->OpenAI translation drops prompt-cache hits on
# Qwen/DeepSeek, mis-streams reasoning models, silently drops params it does
# not recognise, manufactures a reasoning ceiling from the caller's thinking
# budget, never sends prior-turn reasoning back, loses the BYOK half of the
# provider's bill during stream reassembly while its rate card cannot price
# the route either, and seeds its own cost calculator with a BYOK zero that
# suppresses the estimate.
# patch_litellm_cache.py closes the ten gaps at build time
# (see that script for the diagnosis);
# cost_callback.py surfaces the resulting cost/cache stats into the pod log
# stream. Both mirror the host-side dotfiles setup that took Qwen cache hit
# rate from 0% to ~99.99%.
#
# Pinned to v1.94.0 — the newest stable published release, and the version
# the patches and cost_callback are validated against (#3697). The bump from
# v1.86.2 retired four patches upstream absorbed, narrowed a fifth, and added
# a tenth for a regression the bump itself brings in (1.94.0 hands its cost
# calculator the provider's `cost` even when that is a BYOK zero, which
# suppresses the independent estimate and makes the rate-card patch dead code
# on the streaming path).
#
# The patch script fails the build loudly if a needle stops matching, so a
# future bump can't silently ship an unpatched (full-input-rate) image. Read
# that failure as "look at this", not as "upstream fixed it": at this bump,
# two needles stopped matching purely because upstream reflowed the code
# around them, and deleting those patches would have shipped an image paying
# full input rate on every Qwen turn. Confirm each miss against the new
# source before retiring anything.
FROM ghcr.io/berriai/litellm:v1.94.0

# Patches edit the installed litellm package (root-owned site-packages); the
# build runs as root. The runtime pod still drops to UID 1000 via the
# Deployment's securityContext.
USER root

COPY patch_litellm_cache.py /egg/patch_litellm_cache.py
# Staged, not baked directly into the patch script: these are whole modules the
# script installs into every litellm tree (see NEW_MODULES). Keeping them real
# files means they stay lintable and testable in the egg repo rather than
# living as string literals — which is why none of them imports litellm at
# module scope (tests/config/ imports all five directly).
COPY openrouter_capabilities.py /egg/openrouter_capabilities.py
COPY drop_params_visibility.py /egg/drop_params_visibility.py
COPY anthropic_thinking_policy.py /egg/anthropic_thinking_policy.py
COPY openrouter_reasoning_roundtrip.py /egg/openrouter_reasoning_roundtrip.py
COPY stream_cost_preservation.py /egg/stream_cost_preservation.py
RUN python3 /egg/patch_litellm_cache.py

# Custom cost/cache logger, registered as `cost_callback.cost_logger` under
# litellm_settings.callbacks (litellm-configmap.yaml). LiteLLM resolves a
# config-registered callback by FILE PATH relative to the config directory
# (litellm/proxy/types_utils/utils.py::get_instance_fn loads
# `<dirname(--config)>/cost_callback.py`) — NOT via PYTHONPATH/import. The
# Deployment passes `--config /app/config.yaml`, so this must sit at
# /app/cost_callback.py, alongside the mounted config. If that mount path
# ever changes, move this with it. Logs to stdout only — no file writes, so
# it's compatible with the read-only root filesystem.
COPY cost_callback.py /app/cost_callback.py

# Drop back to non-root UID (also set via podSpec securityContext.runAsUser: 1000).
# Ending the image on a non-root USER provides defense-in-depth.
USER 1000

# ENTRYPOINT/CMD inherited from the base image (docker/prod_entrypoint.sh);
# the Deployment supplies --config and --port as args.
